# Operations And Recovery Runbook

This runbook covers schema deployment, production validation, alert recovery, and guarded backfill handling.

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

## Backfill Safety

Historical backfill is manually disabled and is not part of routine recovery. To re-enable it, an operator must explicitly decide to do so, supply the exact confirmation string `I_UNDERSTAND_BACKFILL_PRESSURE`, use one runner, an explicit numeric start page, 1-5 apps, and 1-25 pages per scope. The workflow also enforces conservative delay, retry, cooldown, and time-budget bounds. Automatic continuation is removed.

Do not modify or delete a migration file after it has been applied. Add a new numbered migration instead. The checksum ledger will reject modified history.

## Rollback Principle

Migrations in this repair are additive. If application code must be rolled back, leave the new nullable/defaulted columns and tables in place; deploy the previous application revision without dropping production data. Correct forward with a new migration. Never use `git reset --hard`, truncate review tables, or delete the migration ledger as an operational shortcut.
