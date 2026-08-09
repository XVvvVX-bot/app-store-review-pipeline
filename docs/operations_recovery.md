# Operations And Recovery Runbook

This runbook covers schema deployment, production validation, alert recovery, and guarded backfill handling.

## Availability Contract

The current Mac and local Postgres remain one physical failure domain. The pipeline cannot ingest while the Mac is powered off, offline, or logged out of the runner-owning user session. The recovery contract is therefore automatic restart and safe incremental catch-up after the host returns, not continuous high availability.

The outage path has four independent controls:

1. GitHub-hosted `runner-gate` rejects a schedule immediately when the requested runner capacity is unavailable.
2. Healthchecks.io remains the external dead-man signal when a schedule never completes.
3. The launchd supervisor restarts local runner services and waits for a five-minute stable window.
4. A bounded recovery state machine supersedes stale runs, catches up current reviews, resumes small long-tail backlogs, and verifies all scopes.

The GitHub-hosted capacity gate uses the encrypted repository secret `APP_STORE_RUNNER_MONITOR_TOKEN`. GitHub's default workflow token cannot list repository runners. Use a fine-grained token limited to this repository with Actions read access; without it, the gate fails closed before ingestion.

## Install The Runner Supervisor

```bash
.venv/bin/python app_store_pipeline.py install-runner-supervisor \
  --repo-path "$PWD"
```

This creates:

- `~/.config/app-store-review-pipeline/runner-supervisor.env` with mode `600`;
- `~/.local/share/app-store-review-pipeline-supervisor/`, an unprotected runtime copy with its own virtualenv so launchd does not depend on macOS Documents-folder permissions;
- `~/.local/state/app-store-review-pipeline/runner-supervisor.json`;
- `~/Library/LaunchAgents/com.sciencia.app-store-runner-supervisor.plist`;
- `~/Library/Logs/app-store-runner-supervisor.log`.

The installer copies the active `gh` token into `GH_TOKEN` in this mode-`600` local file because a background launchd process cannot reliably read the interactive macOS keychain. Prefer a fine-grained token limited to this repository with Actions read/write and metadata access. Put the existing Healthchecks base ping URL in `APP_STORE_HEARTBEAT_URL`. Do not commit either value. Reload after editing:

```bash
launchctl kickstart -k "gui/$(id -u)/com.sciencia.app-store-runner-supervisor"
```

Inspect one tick without waiting for launchd:

```bash
.venv/bin/python app_store_pipeline.py runner-supervisor
```

After fixing the recorded root cause of a bounded `manual_attention` stop, explicitly reopen recovery without editing JSON:

```bash
.venv/bin/python app_store_pipeline.py runner-supervisor --reset-manual-attention
launchctl kickstart -k "gui/$(id -u)/com.sciencia.app-store-runner-supervisor"
```

The reset preserves the previous reason and reset time in the state file, clears stale run pointers, restores the bounded attempt budget, and starts a new stability window.

Healthy means Postgres responds, GitHub is reachable, at least four eligible runners are online, and at least four launchd runner services are loaded.

## Automatic Outage Recovery

The supervisor uses these fixed limits:

- health check every 60 seconds;
- outage declaration after 10 unhealthy minutes;
- at most three runner restart attempts;
- at most three full/verification recovery attempts when no monitor-verifiable execution was created;
- five healthy minutes before dispatch;
- execution/workflow stale boundary of six hours;
- full-scope recovery at `max_parallel=4`, page 1, uncapped trusted-overlap stop, and 3600-second scope budget;
- at most 10 backlogged apps, processed serially with 7200-second scope budget;
- 25-page checkpoint overlap, four recent incomplete attempts, and 36-hour checkpoint age;
- at most three checkpoint passes per app, 30 minutes apart.

Each automatic dispatch sets `outage_recovery=true`. This is the only mode that enables `cancel-in-progress`, so it supersedes stale scheduled work without changing ordinary twice-daily concurrency behavior. The recovery token is carried in `experiment_group` for traceability but is not interpreted as an operating-model target group.

The first full-scope run starts every app at page 1. Existing review keys and trusted overlap make partial writes from interrupted runs safe to revisit. If no app remains backlogged, this run resolves the incident. Otherwise the supervisor runs targeted checkpoint recovery, then one final 200-scope verification. An infrastructure run that creates no execution, or leaves one `running`/`cancelled`, gets a fresh five-minute stability window and at most three attempts. More than 10 backlog apps, a monitor-confirmed incomplete execution, a final monitor status that cannot be verified, source-pressure failure, or exhausted retries moves the supervisor to `manual_attention` and stops automatic dispatch. A GitHub workflow may be red during an intermediate backlog pass; the state machine uses persisted scope outcomes and the final Postgres monitor status instead of treating that expected intermediate signal as data loss.

Use these commands to inspect or repair lineage:

```bash
.venv/bin/python app_store_pipeline.py outage-recovery-status \
  --database-url postgresql:///app_store_reviews

.venv/bin/python app_store_pipeline.py reconcile-stale-executions \
  --database-url postgresql:///app_store_reviews \
  --stale-hours 6 \
  --termination-reason runner_outage_superseded
```

Reconciliation marks stale execution metadata `cancelled`; it never deletes pages, reviews, changes, or successful scope outcomes.

## Normal Deployment

1. Keep `.github/workflows/app-store-web-catalog-backfill.yml` disabled.
2. Run the full test suite and whitespace check.
3. Apply migrations before dispatching a workflow that depends on new columns.
4. Run schema validation.
5. Use a one-app manual daily incremental smoke run.
6. Inspect its exact execution row, scope outcome, monitoring artifact, and notification result.
7. Let the next scheduled full-scope run proceed only after the smoke run is clean.

```bash
.venv/bin/python -m pytest -q
git diff --check
.venv/bin/python app_store_pipeline.py init-postgres \
  --database-url postgresql:///app_store_reviews
.venv/bin/python app_store_pipeline.py validate-postgres \
  --database-url postgresql:///app_store_reviews
```

## Typed Timestamp Backfill

Legacy timestamp conversion runs in committed batches:

```bash
.venv/bin/python app_store_pipeline.py backfill-typed-timestamps \
  --database-url postgresql:///app_store_reviews \
  --batch-size 25000 \
  --max-batches 0
```

Verify completion:

```sql
select count(*) from app_store_reviews where collected_at_ts is null;
select count(*) from app_store_review_changes where changed_at_ts is null;
select count(*) from app_store_review_pages where fetched_at_ts is null;
select count(*) from app_store_runs where loaded_at_ts is null;
```

## Failing Scheduled Run

1. Follow the email link to GitHub Actions.
2. Inspect required ingestion jobs before monitor/notify failures.
3. Compare intended versus completed scopes and inspect hard-failure/missing scopes.
4. Check recovered and final 429 evidence separately.
5. Confirm the review-change reconciliation and successful freshness frontier.
6. Correct the fault, then run one target through the daily workflow manually.
7. Do not use historical backfill to repair a daily operational failure.

### Long-tail incremental backlog

When one high-volume app repeatedly exhausts its incremental time budget before reaching trusted overlap, use the daily workflow's explicit backlog recovery mode rather than historical backfill:

- select only the affected app with `limit=1` and its `target_offset`;
- keep `start_page=1` and set `resume_backlogged_scopes=true`;
- keep `max_pages_per_app_country=0` and overlap stop enabled;
- use the guarded workflow defaults: 25 overlap pages, 4 recent attempts, and a 36-hour maximum checkpoint age;
- temporarily use `web_time_budget_seconds=7200` and `web_scope_time_budget_seconds=7200` for the controlled recovery.

The checkpoint query only considers incomplete attempts newer than the scope's last successful catch-up. It chooses the recent attempt that reached the oldest review frontier, then moves 25 pages toward page 1 before resuming. The safety overlap absorbs normal page drift while trusted review IDs still control the final catch-up stop. After the scope reports `caught_up_to_existing_reviews`, leave routine scheduled runs in their default page-1 mode.

If the primary monitor artifact is absent, use the fallback report in the notification artifact. A missing SMTP configuration on an eligible failing run is itself an operational failure and must be corrected before relying on email.

## External Heartbeat

The scheduled workflow sends `/start`, base success, or `/fail` lifecycle pings. If a check remains in started/late state:

1. inspect whether GitHub created the scheduled run;
2. inspect whether `notify` ran;
3. verify `APP_STORE_HEARTBEAT_URL` still points to the service's base ping URL;
4. use GitHub logs as evidence, because the external service is only the dead-man signal.

For a runner incident, Healthchecks owns the one failure/one recovery email pair. GitHub keeps the detailed `pipeline-incident` Issue and suppresses duplicate SMTP for subsequent schedules. A targeted backlog pass sends `/fail`; only a complete full-scope verification sends the success ping.

## Controlled Outage Drill

1. Start a full-scope manual run and wait for approximately 20 persisted scope outcomes.
2. Stop all app-store runner launchd services while leaving the Mac and network available.
3. Confirm the capacity gate or interrupted-run fallback opens one GitHub incident and Healthchecks sends one failure alert.
4. Restore the services, or let the supervisor kickstart them.
5. Confirm the supervisor waits five stable minutes and dispatches one `Outage recovery ... full` run.
6. Confirm stale execution rows become `cancelled` with `runner_outage_superseded` and partial review facts remain present.
7. Confirm any checkpoint passes are serial and bounded, followed by a 200-scope verification.
8. Confirm the Issue closes, Healthchecks returns up, and exactly one recovery alert is received.

## Backfill Safety

Historical backfill is manually disabled and is not part of routine recovery. To re-enable it, an operator must explicitly decide to do so, supply the exact confirmation string `I_UNDERSTAND_BACKFILL_PRESSURE`, use one runner, an explicit numeric start page, 1-5 apps, and 1-25 pages per scope. The workflow also enforces conservative delay, retry, cooldown, and time-budget bounds. Automatic continuation is removed.

Do not modify or delete a migration file after it has been applied. Add a new numbered migration instead. The checksum ledger will reject modified history.

## Rollback Principle

Migrations in this repair are additive. If application code must be rolled back, leave the new nullable/defaulted columns and tables in place; deploy the previous application revision without dropping production data. Correct forward with a new migration. Never use `git reset --hard`, truncate review tables, or delete the migration ledger as an operational shortcut.
