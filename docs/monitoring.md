# App Store Review Pipeline Monitoring

This document describes the GitHub-native monitoring layer for the Apple App Store daily incremental ingestion pipeline.

## Architecture

Monitoring v1 uses three evidence sources:

- Postgres is the source of truth for ingestion behavior: `app_store_runs`, `app_store_review_pages`, `app_store_sync_state`, `app_store_reviews`, and `app_store_review_changes`.
- GitHub Actions metadata is used for workflow and job status.
- The repository target list is used to know how many active apps should be refreshed.

The monitor is intentionally read-only. It does not mutate production data.

There are two monitoring entrypoints:

- `python app_store_pipeline.py monitoring-report`: generates one Markdown health report and one JSON health summary.
- `.github/workflows/app-store-monitor.yml`: scheduled watchdog that checks whether the expected daily scheduled run appeared and wrote recent Postgres rows.

The daily ingestion workflow also has a final `monitor` job. It runs with `if: always() && !cancelled()`, appends the Markdown report to `$GITHUB_STEP_SUMMARY`, uploads the Markdown/JSON artifacts, and fails only when the health status is `failing`.

## Command

Example:

```bash
python app_store_pipeline.py monitoring-report \
  --database-url postgresql:///app_store_reviews \
  --source apple_app_store_web_catalog_reviews \
  --since 2026-07-01T03:07:00Z \
  --selected-count 200 \
  --workflow-result success \
  --github-run-id 123456789 \
  --github-run-url https://github.com/XVvvVX-bot/app-store-review-pipeline/actions/runs/123456789 \
  --github-jobs-json data/reports/monitoring/github_jobs.json \
  --github-runs-json data/reports/monitoring/github_runs.json \
  --markdown-output data/reports/monitoring/current_run_health.md \
  --json-output data/reports/monitoring/current_run_health.json \
  --fail-on failing
```

The command prints GitHub annotation lines for degraded and failing findings. It returns exit code `1` only when the status meets `--fail-on`; the workflow uses `--fail-on failing` so degraded states warn without failing the run.

## Metrics

The report tracks:

- Workflow result, job count, and failed jobs.
- Pages fetched, apps touched, review rows fetched, inserted reviews, updated reviews, and skipped duplicates.
- Duplicate rate, inserts per page, and inserts per fetched row.
- HTTP 429 pages, non-200 pages, retried pages, retry rate, and fetch error rate.
- Terminal reasons, including backlog-style reasons such as `fetch_error`, `page_cap`, and time-budget stops.
- Stale active app-country scopes from `app_store_sync_state`.
- Top apps by inserted reviews and long-tail apps by page count.
- Database row counts and relation sizes for core ingestion tables.

## Health States

`healthy` means the workflow succeeded, Postgres has current ingestion evidence, and no degraded or failing thresholds were tripped.

`degraded` means the pipeline completed but showed a warning signal that should be watched. Degraded does not fail the workflow in v1.

`failing` means the pipeline likely needs operator attention. Failing status fails the monitor job so GitHub Actions becomes the alert surface.

## Alert Logic

Failing conditions:

- Current workflow or required matrix jobs failed.
- No scheduled daily run appears in the monitor grace window.
- Two or more recent scheduled runs failed.
- Current run has zero fetched pages for a non-empty target set.
- HTTP 429 pages are at least 3, or HTTP 429 rate is at least 0.5%.
- Fetch error rate is at least 1%.
- Any active app-country scope has not completed in 36 hours, including scopes with no recorded completion.
- A full-scope-sized run fetched more than 100 pages and inserted zero reviews.
- More than 5% of pages ended with backlog-style terminal reasons.

Degraded conditions:

- Any HTTP 429 occurred but stayed below the failing threshold.
- Any non-200 response occurred but stayed below the failing threshold.
- Runtime estimate exceeded 90 minutes.
- Duplicate rate is at least 95%.
- Inserted reviews fall below 30% of recent app-run median.
- Any active app-country scope has not completed in 24 hours.
- Retried pages exceeded 10% of fetched pages.

Database growth is reported in v1 as visibility, not as a failing threshold.

## Workflows

Daily ingestion workflow:

- File: `.github/workflows/app-store-daily-pipeline.yml`
- Schedule: `7 3,15 * * *`
- Monitor artifact: `app-store-monitoring-${{ github.run_id }}`
- Report files:
  - `data/reports/monitoring/current_run_health.md`
  - `data/reports/monitoring/current_run_health.json`

Scheduled watchdog workflow:

- File: `.github/workflows/app-store-monitor.yml`
- Schedule: `37 4,16 * * *`
- Purpose: detect missing, delayed, or repeatedly failing scheduled daily runs.
- Monitor artifact: `app-store-scheduled-monitor-${{ github.run_id }}`

## Runbook

When the monitor is degraded:

1. Open the GitHub Actions summary and read the alert table first.
2. Check HTTP 429, non-200, retry, and duplicate-rate rows in the current-run metrics table.
3. Look at long-tail apps to see whether one app drove most of the pages or retries.
4. If stale apps appear, check whether their last terminal reason is a source issue, time budget, or normal overlap stop.
5. Keep the twice-daily schedule unless the same warning repeats across multiple runs.

When the monitor is failing:

1. Open the failing monitor job summary and artifact JSON.
2. If workflow jobs failed, inspect the failed matrix jobs first.
3. If the scheduled watchdog failed, confirm whether GitHub created the expected daily run and whether it reached Postgres.
4. If HTTP 429s crossed the threshold, pause manual runs and wait before dispatching another ingestion test.
5. If stale apps crossed 36 hours, inspect the listed apps and decide whether to run a targeted manual daily refresh.
6. If backlog terminal reasons exceeded 5%, inspect page-level terminal reasons before changing concurrency or time budgets.

## Known Limitations

- GitHub cron start time can be delayed; the watchdog uses a grace window rather than exact minute matching.
- The monitor reads GitHub metadata from JSON files produced by `gh api`; Python does not call the live GitHub API directly.
- Runtime is estimated from first and last fetched page timestamps, not from the full GitHub workflow wall-clock duration.
- The current freshness check is based on active app-country sync state. It does not prove historical completeness.
- External Slack or email notification is intentionally deferred; GitHub Actions status, annotations, summaries, and artifacts are the v1 alert surface.
