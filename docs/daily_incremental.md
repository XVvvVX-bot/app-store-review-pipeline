# Daily Incremental Ingestion

This document explains the current daily incremental operating mode for the Apple App Store review pipeline.

The purpose of this mode is to keep the Postgres review dataset fresh across all tracked apps without repeatedly walking deep historical pages. Historical backfill is disabled by default and guarded for deliberate, capped, single-runner use only.

## Current Schedule

The active workflow is `App Store Review Pipeline` in `.github/workflows/app-store-daily-pipeline.yml`.

Production cadence:

- 08:07 America/Los_Angeles during PDT
- 20:07 America/Los_Angeles during PDT
- GitHub Actions cron: `7 3,15 * * *`

GitHub schedules run in UTC and can start later than the exact cron minute. After cron is restored, treat the cron as the requested cadence, not a strict service-level guarantee.

## Why Daily Incremental Replaced Routine Backfill

Deep historical backfill is useful for building historical depth, but it requires walking many old pages for high-volume apps. That access pattern creates more pressure on Apple's public web catalog source and has produced less stable behavior during testing.

Daily incremental ingestion is a safer production baseline:

- it starts from page 1, where the newest reviews appear;
- it fetches only as far as needed to reach trusted reviews already stored in Postgres;
- it updates freshness across all 200 target apps;
- it avoids repeated deep historical walks unless there is a deliberate backfill test;
- it directly validates the operational question: can the pipeline reliably capture new review activity over time?

The current stored dataset already supports short-term EDA and modeling. As of the 2026-07-16 local Postgres check, the web-catalog source has rows for 200 apps, with an app-level lower quartile of 2,192.8 reviews and a median of 4,418.5 reviews. Deep backfill can be resumed later in controlled batches if additional historical depth becomes a priority.

## End-To-End Schema Flow

The daily incremental run writes directly into the reusable storage schema:

1. Select active targets from `app_store_targets` and `data/targets/apple_apps.csv`, then compute intended scope/config signatures.
2. Create one `app_store_executions` row for the GitHub run attempt.
3. Create one `app_store_runs` row for each app worker loaded into Postgres.
4. Fetch Apple web catalog review pages and store final status plus attempt-level retry/429 evidence in `app_store_review_pages`.
5. Normalize review rows and upsert into `app_store_reviews`.
6. Record inserted or updated fields, including before/after values, in `app_store_review_changes`.
7. Persist one explicit `app_store_run_scopes` outcome for every intended app-country scope.
8. Update `app_store_sync_state`; only a caught-up scope advances the trusted successful frontier.
9. Generate one exact-execution health report, persist `app_store_monitor_snapshots`, and complete notification/heartbeat lifecycle handling.

Postgres is the source of truth. Raw JSON and GitHub artifacts are audit/debug outputs, not the cumulative store. Artifact upload failures after Postgres run/page/review writes complete should be treated as post-ingestion infrastructure noise, not as Apple-source ingestion failures.

## Review Identity And Deduplication

The active review identity is:

```text
platform + source + country + app_id + review_id
```

The stored `review_key` follows the same pattern:

```text
apple_app_store:{source}:{country}:{app_id}:{review_id}
```

Repeated runs therefore do not append duplicate review rows. They either:

- insert a review that has not been seen before;
- update the existing review row if the source returns changed metadata;
- skip the row as already known and use it as overlap evidence.

## Trusted Overlap Stop Logic

Daily incremental starts at page 1 and fetches newest-to-older pages.

It stops when it reaches trusted historical overlap, `no_next_href`, a time budget, or a fetch stop.

The overlap rule intentionally uses trusted history, not every review ever inserted. Reviews from an earlier incomplete daily run are not enough to stop the next run, because that could leave a gap between the newly inserted rows and the older historical dataset. The pipeline uses `app_store_sync_state.last_successful_run_id` to decide which existing review IDs are safe overlap anchors.

This distinction matters for long-tail catch-up cases. For example, Spotify had a larger freshness gap than most apps in one validation run. The pipeline fetched 103 pages, saw 2,040 review rows, inserted 207 genuinely new reviews, skipped 1,833 already-known reviews, and then stopped with `caught_up_to_existing_reviews`. The 103 pages did not mean Spotify had 2,040 new reviews; it meant the pipeline had to walk far enough to bridge back to trusted historical rows.

## Current Safe Settings

The scheduled workflow uses an app-level GitHub Actions matrix and self-hosted Mac runners.

Current defaults:

- active targets: all 200 tracked apps
- `max_parallel`: 4 app jobs
- `start_page`: 1
- `review_limit`: 20 reviews per page
- `max_pages_per_app_country`: 0, meaning no page cap before overlap/no-next/time budget
- normal request delay: 10 seconds
- normal request jitter: up to 5 seconds
- HTTP 429 retries: 2
- HTTP 429 retry delay: 300 seconds
- HTTP 429 retry jitter: up to 60 seconds
- per-app time budget: 3,600 seconds
- per-scope time budget: 3,600 seconds
- pre-run HTTP 429 cooldown: disabled for routine daily incremental
- current-run HTTP 429 circuit breaker: enabled

The daily path is intentionally different from backfill. The backfill workflow is manually disabled. If historical depth becomes necessary, it must be explicitly re-enabled and requires the confirmation string `I_UNDERSTAND_BACKFILL_PRESSURE`, one runner, an explicit numeric start page, 1-5 apps, and 1-25 pages per scope. Automatic continuation has been removed.

## Current Operating Recommendation

The current recommendation is to keep the twice-daily full-scope, uncapped overlap-stop incremental schedule as the production baseline. Controlled grouped operating-limit tests found no general benefit that justified moving all apps to a three-hour cadence, while one-page and three-page caps could miss incremental rows.

The reproducible operating-limits report is the source of truth for run evidence:

- Markdown report: `docs/operating_limits.md`
- Machine-readable summary: `docs/operating_limits_summary.json`
- GitHub run ledger: `docs/experiments/operating_model_run_ledger.json`

As of the latest generated report, successful full-scope baseline/control observations showed clean final source status, median successful runtime around 51 minutes, and median successful page volume around 276 pages. The high-activity app segment accounts for most new inserts, so a hybrid model remains a future option, not the current production default.

Controlled strategy tests should not repeatedly use all 200 apps. A full-scope run consumes the available incremental-review signal for the next few hours, so frequency, page-cap, and hybrid experiments use fixed randomized 25-app groups from `docs/experiments/operating_model_target_groups.json`. Full-scope 200-app runs remain the production baseline/control path; grouped tests are the faster experimental path for comparing depth and operating patterns without waiting a day for fresh review activity.

The completed full-scope F1/F2 runs are retained as calibration evidence. Future strategy tests should use `FG1/FG2/D1/D2`-style grouped runs instead of repeating 200-app manual experiments.

## Monitoring Checklist

Automated monitoring is now the primary run-health surface:

- Command: `python app_store_pipeline.py monitoring-report`
- Daily workflow monitor job: `.github/workflows/app-store-daily-pipeline.yml`
- Failing-only email command: `python app_store_pipeline.py send-monitoring-email`
- Optional missing-run detection: external dead-man heartbeat through `APP_STORE_HEARTBEAT_URL`
- Detailed design and runbook: `docs/monitoring.md`

For each scheduled run, the monitor checks:

- GitHub job count: all matrix jobs should finish successfully.
- `app_store_review_pages`: page count, app count, status code distribution, terminal reasons.
- `app_store_runs`: inserted rows, updated rows, duplicates skipped, fetch errors, capped scopes.
- final HTTP 429 pages, recovered/final 429 attempt count, and attempts per fetched page.
- Long-tail apps with unusually high page counts.
- Apps that stop by time budget, fetch error, or final non-200 instead of trusted overlap.
- App-country freshness from `app_store_sync_state`.
- Database row counts and table sizes.

The monitor appends a Markdown health summary to the GitHub Actions run summary and uploads Markdown/JSON artifacts. It queries the exact `execution_id`, classifies the run as `healthy`, `degraded`, or `failing`, reconciles review changes, stores a monitor snapshot, and finalizes execution status. Only an eligible first-attempt scheduled `failing` status sends external email. The independent GitHub-hosted notify job creates a fallback failing report when the self-hosted monitor is unavailable. Healthy and degraded results remain in GitHub.

The optional external heartbeat receives `/start`, success, or `/fail` lifecycle pings. It detects a scheduled workflow that never starts or never completes, which an in-workflow monitor cannot observe.

Manual SQL checks remain useful for investigation:

Useful Postgres checks:

```sql
select
  count(*) as pages,
  count(distinct app_id) as apps,
  count(*) filter (where status_code = 200) as ok_pages,
  count(*) filter (where status_code = 429) as final_http_429_pages,
  coalesce(sum(http_429_attempt_count), 0) as http_429_attempts,
  coalesce(sum(soft_retry_count), 0) as soft_retries,
  count(*) filter (where status_code is not null and status_code <> 200 and status_code <> 429) as other_non_200,
  coalesce(sum(review_count), 0) as review_rows,
  max(fetched_at_ts) as last_page_at
from app_store_review_pages
where fetched_at_ts >= now() - interval '24 hours';
```

```sql
select
  coalesce(sum(fetch_errors), 0) as fetch_errors,
  coalesce(sum(capped_scopes), 0) as capped_scopes,
  coalesce(sum(reviews_inserted), 0) as reviews_inserted,
  coalesce(sum(reviews_updated), 0) as reviews_updated,
  coalesce(sum(duplicates_skipped), 0) as duplicates_skipped
from app_store_runs
where loaded_at_ts >= now() - interval '24 hours';
```

## Known Boundaries

- The source is public Apple-hosted web catalog data, not the App Store Connect Customer Reviews API.
- Daily incremental freshness is not the same as historical exhaustion.
- Historical completeness is proven only when a backfill reaches `no_next_href`.
- A daily run that stops by trusted overlap is considered caught up for incremental freshness, even if older historical pages remain uncollected.
- GitHub schedule timing can be delayed.
- Local Postgres is the current development source of truth; managed Postgres remains a later production decision.
- Detailed schema deployment and failure recovery steps are in `docs/operations_recovery.md`.
