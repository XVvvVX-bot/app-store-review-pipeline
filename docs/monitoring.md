# App Store Review Pipeline Monitoring And Email Alerts

This document describes the operational monitoring and failing-only email layer for Apple App Store daily incremental ingestion.

## Architecture

GitHub remains the complete evidence surface:

- Postgres is the source of truth for pages, runs, review changes, sync state, and database growth.
- GitHub Actions provides required-job status, runtime, logs, summaries, and downloadable artifacts.
- `python app_store_pipeline.py monitoring-report` classifies a completed run as `healthy`, `degraded`, or `failing`.
- `python app_store_pipeline.py send-monitoring-email` reads the JSON report and sends a short email only for an eligible `failing` scheduled run.

The former GitHub-scheduled watchdog was removed. It depended on the same delayed GitHub cron as ingestion and could evaluate a still-running daily job. Missing-run detection is now an optional external dead-man heartbeat: each scheduled workflow pings `APP_STORE_HEARTBEAT_URL` when it starts. The external service should expect a ping every 12 hours with a 6-hour grace period.

The monitor does not mutate ingestion data. SMTP delivery and heartbeat checks are operational side effects only.

## Commands

Generate a report:

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
  --github-jobs-json data/reports/monitoring/github_jobs.json \
  --github-runs-json data/reports/monitoring/github_runs.json \
  --markdown-output data/reports/monitoring/current_run_health.md \
  --json-output data/reports/monitoring/current_run_health.json \
  --fail-on failing
```

Preview a failing email without sending it:

```bash
python app_store_pipeline.py send-monitoring-email \
  --report-json data/reports/monitoring/current_run_health.json \
  --result-json data/reports/monitoring/notification_result.json \
  --preview-output data/reports/monitoring/notification_preview.eml \
  --dry-run \
  --force
```

## Evidence And Metrics

The report includes:

- Required GitHub job outcomes, excluding the final monitor job from ingestion-failure counts.
- Pages, apps, fetched rows, inserts, updates, duplicates, retries, HTTP status, fetch errors, and terminal reasons.
- App-country source-pressure scopes, including page share, HTTP 429 share, and fetch-error pages.
- Page-one frontier movement compared with the preceding fetch for the same scope.
- Insert/update totals cross-checked against `app_store_review_changes`.
- Active-scope freshness and relation row/size snapshots.

## Health And Alert Logic

Failing conditions:

- A required `prepare-matrix`, `preflight`, or `daily` job fails.
- Two or more recent scheduled runs have actual ingestion-job failures; monitor-only failures do not count.
- A non-empty selected target set produces zero fetched pages.
- HTTP 429 pages are at least 3, or the HTTP 429 rate is at least 0.5%.
- Fetch error rate is at least 1%.
- Any active app-country scope has not completed in 36 hours.
- More than 5% of pages end with backlog-style terminal reasons.
- `app_store_runs` insert/update totals disagree with the persisted review-change ledger.

Degraded conditions:

- Any HTTP 429 occurs below the failing threshold.
- Any non-429 non-200 response occurs. A 429 does not also create this warning; fetch errors still escalate separately at 1%.
- Runtime exceeds 90 minutes or twice the recent comparable GitHub-run median.
- Retry rate exceeds 10%.
- Duplicate rate is at least 95%.
- Inserts per selected scope fall below 30% of the recent per-scope median.
- Any active app-country scope is between 24 and 36 hours stale.
- One scope supplies at least 25% of fetched pages or at least 50% of observed HTTP 429 pages.
- A full-scope run inserts zero rows. If at least 95% of comparable page-one frontiers are unchanged, the report records `source_snapshot_unchanged` instead of treating it as a storage failure.

`healthy` means no degraded or failing threshold is present. Database growth remains informational in this version.

## Email Behavior

Email is a thin alert, not a replacement for GitHub evidence. It contains:

- The primary failing code and short reason.
- Up to three affected app-country scopes.
- Pages, fetched rows, inserts, duplicates, HTTP 429s, other non-200s, and fetch errors.
- A link to the GitHub Actions run containing the complete report and artifacts.

Email is sent only when all conditions hold:

- Report status is `failing`.
- The workflow event is `schedule`.
- This is GitHub run attempt 1.

Healthy and degraded reports never send email. Manual dispatches and reruns do not send unless an operator explicitly uses `--force`. Missing SMTP configuration records `not_configured` and emits a GitHub warning without exposing secrets or failing an otherwise completed workflow.

Configure these repository secrets:

- `APP_STORE_ALERT_SMTP_USERNAME`
- `APP_STORE_ALERT_SMTP_APP_PASSWORD`
- `APP_STORE_ALERT_EMAIL_FROM`
- `APP_STORE_ALERT_EMAIL_TO` as a comma- or semicolon-separated list

Optional repository variables are `APP_STORE_ALERT_SMTP_HOST` and `APP_STORE_ALERT_SMTP_PORT`; defaults are Gmail SMTP at `smtp.gmail.com:587` with STARTTLS. For Gmail, use an App Password rather than an account password.

The workflow uploads `notification_result.json`, which contains delivery status, recipient count, subject, and alert fingerprint, but no addresses or credentials.

### Controlled SMTP Test

Use `.github/workflows/app-store-alert-email-test.yml` after first-time setup or credential rotation. It is manual-only, sends a message whose subject starts with `[TEST]`, and uses a synthetic failing report with `--force` solely to exercise SMTP delivery. It does not start ingestion or read or modify Postgres. The workflow summary and seven-day artifact record the delivery result without storing email addresses or credentials.

## Missing Scheduled Runs

Set `APP_STORE_HEARTBEAT_URL` to a secret ping URL from an external dead-man service. Configure the check for:

- Expected interval: 12 hours.
- Grace period: 6 hours, covering observed GitHub cron delay plus normal runtime.
- Alert channel: email.
- Evidence link: the repository Actions page.

The heartbeat runs at workflow start, so ingestion failures remain the responsibility of the daily monitor and SMTP email. If the secret is absent, ingestion continues and GitHub records that missing-run detection is not configured.

## Simulated Failure Coverage

Automated fixtures cover:

- Missing scheduled-run evidence.
- Repeated actual ingestion failures with monitor-only failures excluded.
- Active-scope/Postgres staleness at 24-hour and 36-hour thresholds.
- HTTP 429 and fetch-error rates below and above failing thresholds.
- Zero inserts with unchanged source frontiers.
- Change-ledger accounting mismatch.
- Dominant source-pressure scope identification.
- Healthy/degraded reports skipping SMTP, failing reports producing a short message, dry-run `.eml` output, and fake-SMTP delivery.

## Runbook

For degraded runs, inspect the alert table and source-pressure scopes before changing concurrency or cadence. `source_snapshot_unchanged` means Apple exposed no new page-one frontier; it is not evidence of a Postgres failure.

For failing runs, follow the email link, inspect required failed jobs first, then review accounting, HTTP/fetch-error metrics, stale scopes, and terminal reasons. Email delivery failures are reported separately in `notification_result.json`.

## Known Limitations

- GitHub scheduled start time is not deterministic. The external heartbeat detects absence but does not make GitHub start on time.
- The heartbeat service must be configured separately before missing-run email detection is active.
- SMTP secrets are intentionally not stored in the repository.
- Runtime comparison uses GitHub job/run timestamps; page-fetch runtime remains visible separately.
- Source-frontier equality proves that the visible page-one timestamp did not move, not that Apple has no unpublished reviews.
- Historical completeness is separate from incremental freshness.
