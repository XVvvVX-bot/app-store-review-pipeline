# Daily Incremental Ingestion

This document explains the current daily incremental operating mode for the Apple App Store review pipeline.

The purpose of this mode is to keep the Postgres review dataset fresh across all tracked apps without repeatedly walking deep historical pages. Historical backfill remains available as a manual workflow, but it is not the default operating mode while we evaluate stable refresh behavior.

## Current Schedule

The active workflow is `App Store Review Pipeline` in `.github/workflows/app-store-daily-pipeline.yml`.

Current cadence:

- 08:07 America/Los_Angeles during PDT
- 20:07 America/Los_Angeles during PDT
- GitHub Actions cron: `7 3,15 * * *`

GitHub schedules run in UTC and can start later than the exact cron minute. Treat the cron as the requested cadence, not a strict service-level guarantee.

## Why Daily Incremental Replaced Routine Backfill

Deep historical backfill is useful for building historical depth, but it requires walking many old pages for high-volume apps. That access pattern creates more pressure on Apple's public web catalog source and has produced less stable behavior during testing.

Daily incremental ingestion is a safer production baseline:

- it starts from page 1, where the newest reviews appear;
- it fetches only as far as needed to reach trusted reviews already stored in Postgres;
- it updates freshness across all 200 target apps;
- it avoids repeated deep historical walks unless there is a deliberate backfill test;
- it directly validates the operational question: can the pipeline reliably capture new review activity over time?

The current stored dataset already supports short-term EDA and modeling. As of the latest local Postgres check, the web-catalog source has rows for 200 apps, with an app-level lower quartile of about 1,988 reviews and a median of about 3,914 reviews. Deep backfill can be resumed later in controlled batches if additional historical depth becomes a priority.

## End-To-End Schema Flow

The daily incremental run writes directly into the reusable storage schema:

1. Select active targets from `app_store_targets` and `data/targets/apple_apps.csv`.
2. Create one `app_store_runs` row for each app job loaded into Postgres.
3. Fetch Apple web catalog review pages and store page status in `app_store_review_pages`.
4. Normalize review rows and upsert into `app_store_reviews`.
5. Record inserted or updated review observations in `app_store_review_changes`.
6. Update `app_store_sync_state` so future incremental runs know which historical overlap is trusted.

Postgres is the source of truth. Raw JSON and GitHub artifacts are audit/debug outputs, not the cumulative store.

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

The daily path is intentionally different from backfill. Backfill can be resumed manually with separate settings when historical depth testing is needed.

## Current Operating Recommendation

The current recommendation is to keep the twice-daily full-scope incremental schedule as the production baseline while controlled F1/F2/D1/D2 operating-limit tests are completed.

The reproducible operating-limits report is the source of truth for run evidence:

- Markdown report: `docs/operating_limits.md`
- Machine-readable summary: `docs/operating_limits_summary.json`
- GitHub run ledger: `docs/experiments/operating_model_run_ledger.json`

As of the latest generated report, the successful full-scope baseline/control observations show clean source-pressure behavior: 0 HTTP 429 pages across successful observed runs, median successful runtime around 51 minutes, and median successful page volume around 276 pages. The high-activity app segment currently accounts for most newly inserted rows, which is why a hybrid model remains a candidate after the planned controlled tests.

## Monitoring Checklist

For each scheduled run, check:

- GitHub job count: all matrix jobs should finish successfully.
- `app_store_review_pages`: page count, app count, status code distribution, terminal reasons.
- `app_store_runs`: inserted rows, updated rows, duplicates skipped, fetch errors, capped scopes.
- HTTP 429 count and rate.
- Long-tail apps with unusually high page counts.
- Apps that stop by time budget, fetch error, or final non-200 instead of trusted overlap.

Useful Postgres checks:

```sql
select
  count(*) as pages,
  count(distinct app_id) as apps,
  count(*) filter (where status_code = 200) as ok_pages,
  count(*) filter (where status_code = 429) as http_429,
  count(*) filter (where status_code is not null and status_code <> 200 and status_code <> 429) as other_non_200,
  coalesce(sum(review_count), 0) as review_rows,
  max(fetched_at::timestamptz) as last_page_at
from app_store_review_pages
where fetched_at::timestamptz >= now() - interval '24 hours';
```

```sql
select
  coalesce(sum(fetch_errors), 0) as fetch_errors,
  coalesce(sum(capped_scopes), 0) as capped_scopes,
  coalesce(sum(reviews_inserted), 0) as reviews_inserted,
  coalesce(sum(reviews_updated), 0) as reviews_updated,
  coalesce(sum(duplicates_skipped), 0) as duplicates_skipped
from app_store_runs
where loaded_at::timestamptz >= now() - interval '24 hours';
```

## Known Boundaries

- The source is public Apple-hosted web catalog data, not the App Store Connect Customer Reviews API.
- Daily incremental freshness is not the same as historical exhaustion.
- Historical completeness is proven only when a backfill reaches `no_next_href`.
- A daily run that stops by trusted overlap is considered caught up for incremental freshness, even if older historical pages remain uncollected.
- GitHub schedule timing can be delayed.
- Local Postgres is the current development source of truth; managed Postgres remains a later production decision.
