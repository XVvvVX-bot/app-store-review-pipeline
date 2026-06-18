# Architecture Notes

## Source

The scheduled primary source is Apple's public App Store web catalog reviews JSON path. It returns structured public review rows, including review ID, date, rating, title, text, and user name. The web catalog source is stored as:

```text
apple_app_store_web_catalog_reviews
```

Apple's legacy public iTunes customer reviews RSS JSON feed remains in the repository as a manual-only baseline. It returns structured recent-review rows for an app, country storefront, page number, and sort order, but it should not be treated as a complete historical source.

This project does not use Apple App Store Connect API credentials. That official API is useful for owned or authorized apps, but this pipeline is for public third-party app-review monitoring.

## Target List

Targets live at `data/targets/apple_apps.csv`.

Columns:

- `app_name`
- `category`
- `apple_app_id`
- `apple_slug`
- `countries`
- `active`
- `notes`

`countries` accepts `|` or comma-separated country codes. Each app-country pair becomes a separate incremental scope.

## Fetch

For each active scope, the scheduled fetcher reads web catalog pages from page 1 with `sort=recent`, `limit=20`, bounded 429 retry/backoff, and a wall-clock budget. It saves raw JSON files under:

```text
data/raw/apple_web_catalog/{run_id}/
```

The manual legacy RSS fetcher saves raw JSON files under:

```text
data/raw/apple_rss/{run_id}/
```

The web catalog fetcher also writes:

- `review_pages.jsonl`
- `reviews.jsonl`
- `fetch_report.json`

## Load

The loader upserts into Postgres. Review identity is:

```text
platform + source + country + app_id + review_id
```

This means repeated daily runs do not duplicate old reviews. If Apple changes a review's timestamp, text, rating, version, title, or vote fields, the row is updated and an audit row is written to `app_store_review_changes`.

## Incremental Completeness

The primary web catalog path is sorted by recent reviews. The project treats completeness in two separate ways:

- Daily incremental completeness: once the run has reached its configured coverage target and then sees already-known web catalog review IDs, the scope is considered caught up.
- Backfill completeness: a manual backfill can disable page cap with `--max-pages-per-app-country 0` and disable overlap stop. The strongest completion signal is `no_next_href`, meaning Apple's returned page did not advertise another page.

Other stop reasons are intentionally weaker:

- `target_review_count_reached`: enough for migration parity, not historical exhaustion.
- `caught_up_to_existing_reviews`: enough for daily incremental, not historical exhaustion.
- `page_cap`: lower-bound depth only.
- `time_budget_exceeded`: profile too heavy for that budget.
- `non_200_page` or `fetch_error`: incomplete and should be retried or continued later.

For high-volume apps, the safest response is a controlled backfill plan with continuations via `start_page`, or a licensed provider with stronger historical guarantees if the public path becomes unstable.

The legacy RSS path still uses review-ID overlap as a recent-window monitor. If RSS reaches page 10 without overlap, the scope is marked `backlogged`.

The `App Store Review Pipeline` workflow runs the scheduled web catalog profile on the self-hosted runner. The `App Store Web Catalog Backfill` workflow is manual-only and is used for complete-backfill probes.

## Storage

Postgres is the cumulative store. Raw JSON and daily reports are artifacts for audit/debugging; they are not the source of truth after loading.
