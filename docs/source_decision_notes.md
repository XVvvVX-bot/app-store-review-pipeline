# Source Decision Notes

## Current Decision

Use Apple App Store public RSS as the current production pipeline source for this repository.

Continue evaluating Apple public web catalog reviews as the strongest public replacement candidate, using the rotating single-app canary profile. Do not silently replace RSS until repeated scheduled canaries show clean parity across more target windows.

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
- up to 25 web pages per app-country scope
- 5-second page delay
- 60-second 429 retry delay with 1.5x backoff
- stop once the web catalog reaches RSS parity
- hard web time budget

This profile has passed replacement gates for sampled apps, including Amazon Shopping, Walmart, Target, Uber, Lyft, DoorDash, and American Airlines. Larger multi-app deep-pagination batches are not stable enough yet because they hit 429 pressure and time budgets.
