# Source Decision Notes

## Current Decision

Use Apple App Store public RSS as the current production pipeline source for this repository.

Apple public web catalog reviews have passed the current conservative single-app promotion gate and are now available as an experimental separate ingestion mode. Do not silently replace the scheduled RSS workflow yet; keep RSS as the production baseline while running web catalog as a controlled single-app rotating path.

## Why Apple RSS

Apple RSS gives us:

- public third-party app access
- structured JSON
- full written review text
- rating
- title
- updated timestamp
- app version
- country storefront
- stable observed review ID

This is enough to build a useful recent-review analytics pipeline.

## Limits

Apple RSS should not be treated as full historical coverage. The practical source window is about 10 pages x 50 reviews per app-country-sort scope. The pipeline therefore measures overlap and marks scopes as backlogged when the source window may have moved too quickly.

## Production Caveat

For stronger completeness, cross-platform coverage, contractual access, or historical backfill, evaluate a licensed app-review provider. This repo is designed so a provider source could later plug into the same target, normalize, Postgres upsert, validate, and report architecture.

The current best public replacement-source candidate is Apple public web catalog reviews with single-app rotating fetch. The current best contractual production path is still a licensed provider API. See [source_replacement_options.md](source_replacement_options.md).

## Web Catalog Candidate

The web catalog endpoint returns structured JSON review pages and can match the RSS 500-review recent window for tested apps when run conservatively:

- one app per run
- `limit=20`
- up to 35 web pages per app-country scope for controlled ingestion
- 5-second page delay
- 60-second 429 retry delay with 1.5x backoff
- stop once the web catalog reaches RSS parity
- hard web time budget

This profile has passed replacement or parity gates for sampled apps including Amazon Shopping, Walmart, Target, Uber, Uber Eats, TikTok, DoorDash, Instagram, YouTube, Netflix, Life360, and SHEIN. Larger multi-app deep-pagination batches are not stable enough yet because they hit 429 pressure and time budgets.

Manual depth probes show that web catalog can go beyond the RSS-sized 500-review window for some apps: Amazon Shopping has reached 5,042 distinct web catalog reviews through page 253, and the terminal page still had a `next` link. Treat that as a lower-bound depth proof, not a full historical-completeness claim.

Use `scripts/summarize_source_comparisons.py` to judge promotion from repeated canary artifacts. As of the June 18, 2026 downloaded canary set, the full single-app profile has 5 clean replacement-candidate runs and passes the current promotion gate: 2,479 RSS reviews vs 2,500 web catalog reviews, 8 recovered `429` pages, 0 unrecovered `429` pages, and 0 incomplete scopes. The mixed all-run history is still not ready because it includes an incomplete multi-app run.

The web catalog endpoint has been verified to return full review text rows, not only review IDs. A local smoke run fetched PayPal web catalog rows with review ID, user name, rating, title, review text, and date, then loaded 20 rows into a temporary Postgres database under `source='apple_app_store_web_catalog_reviews'`.
