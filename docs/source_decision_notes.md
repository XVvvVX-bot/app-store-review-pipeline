# Source Decision Notes

## Current Decision

Use Apple public App Store web catalog reviews as the current primary source for public third-party app-review ingestion.

This decision is based on the current project goal: collect commercially relevant public app reviews with full text, ratings, timestamps, app identity, country storefront, and stable review identity for downstream analytics and modeling.

## Why This Source

The web catalog source currently provides:

- public third-party app access
- structured JSON review rows
- full written review text
- rating, title, author name, timestamp, and app identity
- country storefront
- stable observed review IDs
- pagination through returned `next` hrefs
- substantially deeper observed review depth than Apple's legacy RSS feed
- practical upsert behavior into the current Postgres schema

## What This Source Is Not

This is not the official App Store Connect Customer Reviews API. App Store Connect is appropriate for owned or authorized apps, but this pipeline is evaluating public third-party app reviews.

This is also not HTML scraping as the production path. Rendered HTML and source-health probes were useful research tools, but the production fetcher reads structured web catalog JSON.

## Known Limits

- The source is public structured catalog data, not a contractual API.
- Historical completeness is only proven per app-country scope when a backfill reaches `no_next_href`.
- Large deep backfills can trigger HTTP 429 pressure; the active backfill path now handles isolated 429 responses with a 5-minute per-request retry delay plus jitter, and relies on the current-run circuit breaker when the aggregate 429 rate becomes unhealthy.
- Version and review-vote fields are not currently available from the public web catalog payload and are intentionally excluded from the production review schema.
- Local Postgres is the current development store; managed Postgres remains a later production decision.

## Current Evidence Package

The current evidence package is:

- production pipeline structure in `docs/architecture.md`
- reproducible EDA report in `docs/eda/apple_review_data_quality.md`
- machine-readable EDA summary in `docs/eda/apple_review_data_quality_summary.json`
- archived source research in `docs/archive/research/`
- archived old workflows in `docs/archive/workflows/`

## Deferred Alternatives

Legacy RSS, licensed provider probes, rendered HTML probing, and canary/pressure-search workflows are no longer active production paths. They remain archived because they explain how the source decision was reached and can be revived deliberately if the source strategy changes.
