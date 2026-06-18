# Architecture Notes

## Source

The scheduled production source is Apple's public iTunes customer reviews RSS JSON feed. It returns structured public review rows for an app, country storefront, page number, and sort order.

The experimental public-source candidate is Apple's App Store web catalog reviews JSON endpoint. It also returns structured public review rows, including review ID, date, rating, title, text, and user name. The web catalog source is stored separately as:

```text
apple_app_store_web_catalog_reviews
```

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

For each active scope, the fetcher reads pages `1..10` by default. It saves raw JSON files under:

```text
data/raw/apple_rss/{run_id}/
```

The web catalog fetcher uses conservative single-app windows by default and saves raw JSON files under:

```text
data/raw/apple_web_catalog/{run_id}/
```

It also writes:

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

The RSS feed is recent-window based. The project treats completeness as a monitored property:

- If overlap with known review IDs is found before page 10, the scope is considered caught up.
- If page 10 is reached with no overlap, the scope is marked `backlogged`.
- Backlogged scopes are warnings that the schedule may be too slow for that app-country volume.

For high-volume apps, the safest response is a shorter schedule cadence or a licensed provider with stronger historical guarantees.

The web catalog ingestion path uses the same review-ID overlap idea, but source identity is separate from RSS. A web catalog run can stop when it sees an already-known web catalog review ID for that app-country-sort scope. The currently validated public profile is one app per run, up to 25 web pages, `limit=20`, 5-second request delay, and bounded 429 retry/backoff.

The `App Store Web Catalog Ingestion` workflow runs that profile as a controlled self-hosted Postgres ingestion trial. It is intentionally separate from the RSS daily workflow, so web catalog rows can be evaluated without changing the production baseline.

## Storage

Postgres is the cumulative store. Raw JSON and daily reports are artifacts for audit/debugging; they are not the source of truth after loading.
