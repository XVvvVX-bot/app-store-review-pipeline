# App Store Review Pipeline Monitoring And Email Alerts

This document defines the production monitoring contract for Apple App Store daily incremental ingestion.

## Architecture

GitHub remains the complete operational evidence surface, while Postgres remains the ingestion source of truth.

1. `prepare-matrix` selects the intended targets and app-country scopes, computes stable scope/config signatures, and assigns an execution ID based on the GitHub run and attempt.
2. `preflight` creates the `app_store_executions` row before any app worker starts.
3. Every matrix worker attaches its `app_store_runs`, pages, reviews, changes, and `app_store_run_scopes` outcome to that execution.
4. `monitor` queries the exact execution ID, rather than an approximate time window, writes Markdown/JSON artifacts, persists one `app_store_monitor_snapshots` row, and finalizes the execution health status.
5. The independent GitHub-hosted `notify` job runs with `if: always()`. It downloads the primary report or creates a minimal failing fallback report when the self-hosted monitor never produced one.
6. Email is sent only for an eligible failing scheduled run. Healthy and degraded evidence stays in GitHub.

The monitor never changes review facts or page outcomes. It does update monitoring metadata: the execution status/counts and the execution's monitor snapshot.

The former GitHub-scheduled watchdog was removed. A watchdog driven by the same delayed GitHub scheduler cannot reliably detect a missing scheduled run and can race a still-running ingestion. Missing-run detection is delegated to an optional external dead-man heartbeat.

## Commands

Generate an exact-execution report:

```bash
python app_store_pipeline.py monitoring-report \
  --database-url postgresql:///app_store_reviews \
  --source apple_app_store_web_catalog_reviews \
  --since 2026-07-16T05:46:54Z \
  --selected-count 200 \
  --workflow-result success \
  --github-run-id 123456789 \
  --github-run-url https://github.com/example/repo/actions/runs/123456789 \
  --github-event-name schedule \
  --github-run-attempt 1 \
  --execution-id github:123456789:1:apple-web-catalog \
  --github-jobs-json data/reports/monitoring/github_jobs.json \
  --github-runs-json data/reports/monitoring/github_runs.json \
  --markdown-output data/reports/monitoring/current_run_health.md \
  --json-output data/reports/monitoring/current_run_health.json \
  --fail-on failing
```

Preview a synthetic email without sending it:

```bash
python app_store_pipeline.py send-monitoring-email \
  --report-json data/reports/monitoring/current_run_health.json \
  --result-json data/reports/monitoring/notification_result.json \
  --preview-output data/reports/monitoring/notification_preview.eml \
  --dry-run \
  --force
```

## Evidence And Metrics

Each report contains:

- intended, completed, caught-up, backlogged, hard-failure, and missing scope counts;
- required GitHub job outcomes, excluding `monitor` and `notify` from ingestion-job failure counts;
- fetched pages/rows, inserted and updated reviews, skipped duplicates, and change-ledger reconciliation;
- final HTTP status counts and attempt-level pressure signals;
- final 429 pages, all 429 attempts including recovered attempts, soft retries, retried pages, and maximum attempts;
- terminal reasons and exact per-scope outcomes;
- page-one frontier movement compared with the previous fetch for each scope;
- active app-country data freshness based on the most recent completed page-fetch attempt, with successful catch-up age and backlog shown separately;
- database row counts, relation sizes, and growth compared with prior monitor snapshots;
- high-volume, high-pressure, and long-tail app scopes.

`http_429_pages` counts pages whose final response remained 429. `http_429_attempts` counts every 429 inside the retry chain, including a page that later recovered to 200. `final_http_429_rate` is the failing-threshold rate; the attempt rate remains visible as source-pressure evidence and produces a degraded warning when all affected pages recover.

## Health And Alert Logic

### Failing

- A required prepare, preflight, or daily matrix job fails.
- Two or more recent scheduled ingestion runs fail.
- A non-empty target set produces zero pages.
- Any intended scope has no persisted scope outcome.
- Any scope ends in `hard_failure`.
- Final HTTP 429 pages are at least 3, or final 429 pages per fetched page are at least 0.5%.
- Fetch-error scopes are at least 1% of completed scopes.
- Any active app-country-source scope has no completed collection attempt in 36 hours.
- More than 5% of completed scopes remain backlogged in a full-scope run (at least 100 selected targets).
- Run insert/update totals disagree with `app_store_review_changes`.

### Degraded

- Any HTTP 429 attempt occurs and all final 429 pressure remains below the failing threshold.
- Any non-429 final non-200 response occurs.
- Retried pages exceed 10% of fetched pages.
- Runtime exceeds 90 minutes or twice the recent comparable median.
- A full-scale run has at least 95% duplicates.
- Inserts fall below 30% of the median only after at least three comparable completed executions exist.
- Any active scope has no completed collection attempt for 24 to 36 hours.
- One or more scopes remain backlogged but the full-scope rate is no more than 5%; targeted recovery runs also remain degraded rather than failing so they can continue from their next checkpoint without an external production-failure alert.
- A full-scope run inserts zero rows; source-frontier evidence distinguishes an unchanged Apple snapshot from an unexplained zero-insert run.
- A scope dominates at least 25% of page volume or at least 50% of 429 attempts in a run with at least 10 selected targets.
- Database growth exceeds 100 MiB and three times the recent snapshot median.

`healthy` means no degraded or failing condition is present.

## Email Behavior

Email is a short failing alert, not the evidence archive. It includes the primary reason, affected scopes, key metrics, and the GitHub Actions run link.

Automatic email requires all of the following:

- report status is `failing`;
- event is `schedule`;
- GitHub run attempt is `1`.

Healthy and degraded runs do not send email. Manual runs and reruns do not send unless an operator deliberately supplies `--force`.

The `notify` job runs on GitHub-hosted Ubuntu and is independent of the self-hosted ingestion runners. If the primary report is missing, it creates a fallback `monitor_report_unavailable` or `workflow_failure` report and still attempts notification. For an eligible failing scheduled report, missing or invalid SMTP configuration fails the notify job; silently losing the only external alert is not treated as success.

Required repository secrets:

- `APP_STORE_ALERT_SMTP_USERNAME`
- `APP_STORE_ALERT_SMTP_APP_PASSWORD`
- `APP_STORE_ALERT_EMAIL_FROM`
- `APP_STORE_ALERT_EMAIL_TO`, comma- or semicolon-separated

Optional variables:

- `APP_STORE_ALERT_SMTP_HOST`, default `smtp.gmail.com`
- `APP_STORE_ALERT_SMTP_PORT`, default `587`

Gmail requires a 16-character App Password, not the account password. `notification_result.json` stores delivery status, recipient count, subject, and alert fingerprint, but never addresses or credentials.

### Controlled SMTP Test

Run `.github/workflows/app-store-alert-email-test.yml` manually after initial setup or credential rotation:

- `smtp_connectivity` sends a synthetic test with `--force`.
- `automatic_failure` simulates threshold-crossing 429 pressure and exercises normal eligibility without `--force`.

The test does not run ingestion or mutate Postgres. Its summary and seven-day artifact preserve the non-secret delivery result.

## External Dead-Man Heartbeat

Set the `APP_STORE_HEARTBEAT_URL` repository secret to the base ping URL of a service that supports start/success/fail lifecycle pings.

For scheduled runs the workflow calls:

- `${APP_STORE_HEARTBEAT_URL}/start` after matrix preparation;
- `${APP_STORE_HEARTBEAT_URL}` after a healthy or degraded completion;
- `${APP_STORE_HEARTBEAT_URL}/fail` after a failing completion.

Configure the external check for a 12-hour expected interval and an initial 6-hour grace period. This detects a workflow that never starts, a workflow that starts but never reaches notification, and an explicitly failing monitor. The heartbeat is best-effort and never blocks ingestion; email remains the direct failing-status alert.

## Failure Simulation Coverage

Automated tests cover:

- missing and repeated failed scheduled runs;
- missing intended scopes and hard-failure scope outcomes;
- staleness at 24-hour and 36-hour thresholds;
- final and recovered 429 pressure below and above thresholds;
- fetch-error, retry, duplicate, insert-drop, and backlog thresholds;
- unchanged source frontiers and change-ledger mismatches;
- monitor-job exclusion from ingestion failure counts;
- fallback reports, dry-run email, fake SMTP delivery, and missing configuration behavior;
- Markdown and JSON rendering.

## Runbook

1. Open the failing email's GitHub run link.
2. Check `prepare-matrix`, `preflight`, and failed `daily (...)` jobs before the monitor/notify jobs.
3. Compare intended, completed, hard-failure, backlogged, and missing scope counts.
4. Inspect 429 attempts, final non-200 pages, retries, fetch errors, and the pressure-scope table.
5. Check latest-attempt data freshness, then inspect `last_successful_at` and backlog separately for catch-up completeness.
6. Do not restart deep backfill as a first response. Correct the fault, then use a one-app manual incremental smoke run.
7. Follow `docs/operations_recovery.md` for migration, rollback, and validation commands.

## Known Limitations

- GitHub cron start time is not deterministic; the external heartbeat detects absence but cannot start the job.
- The heartbeat service and its email destination are configured outside this repository.
- Runtime comparison depends on GitHub timestamps; source-fetch duration remains separately visible through page timestamps.
- A stable page-one frontier proves only that Apple's visible snapshot did not advance.
- Historical completeness remains separate from incremental freshness.
