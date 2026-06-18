# Source Decision Notes

## Current Decision

Use Apple App Store public web catalog reviews as the current scheduled pipeline source for this repository.

Apple public web catalog reviews have passed the current conservative single-app promotion gate. The scheduled workflow now uses the web catalog path on a single-app rotating profile, while RSS remains manual-only as a recent-window baseline and diagnostic source.

## Why Web Catalog

Apple web catalog gives us:

- public third-party app access
- structured JSON
- full written review text
- rating
- title
- updated timestamp
- country storefront
- stable observed review ID
- pagination via returned `next` href
- substantially deeper observed review depth than RSS for tested apps

This is enough to build a useful public app-review analytics pipeline and to test deeper backfill behavior app by app.

## Limits

Apple web catalog should not be treated as a contractual production API. It is a public Apple-hosted catalog JSON path, not the formal App Store Connect customer reviews API. Complete historical coverage is only proven for a scope when a backfill run reaches `no_next_href`; page-cap, RSS-parity, overlap, error, or time-budget stops are lower-bound or incremental results.

Apple RSS also remains limited. Its practical source window is about 10 pages x 50 reviews per app-country-sort scope, so it is useful as a baseline but not as a full historical source.

## Production Caveat

For stronger completeness, cross-platform coverage, contractual access, or historical backfill, evaluate a licensed app-review provider. This repo is designed so a provider source could later plug into the same target, normalize, Postgres upsert, validate, and report architecture.

The current best public path is Apple public web catalog reviews with single-app rotating fetch plus controlled manual backfills. The current best contractual production path is still a licensed provider API. See [source_replacement_options.md](source_replacement_options.md).

## Web Catalog Primary Path

The web catalog endpoint returns structured JSON review pages and can match or exceed the RSS recent-review window for tested apps when run conservatively:

- one app per run
- `limit=20`
- up to 35 web pages per app-country scope for controlled ingestion
- 5-second page delay
- 60-second 429 retry delay with 1.5x backoff
- stop once the web catalog reaches RSS parity
- hard web time budget

This profile has passed replacement or parity gates for 20 sampled app-country scopes, including Amazon Shopping, Walmart, Target, Uber, Uber Eats, TikTok, DoorDash, Instagram, YouTube, Netflix, Life360, SHEIN, Cash App, PayPal, Venmo, ReelShort, Lyft, Freecash, Pokemon GO, and Rips by Triumph. Larger multi-app deep-pagination batches are not stable enough yet because they hit 429 pressure and time budgets.

Manual depth probes show that web catalog can go beyond the RSS-sized 500-review window for some apps: Amazon Shopping has reached 5,042 distinct web catalog reviews through page 253, and the terminal page still had a `next` link. The current scorecard also has 20/20 tested web catalog scopes above 500 reviews, all at or above their RSS counts. Treat the 5,042-row result as a lower-bound depth proof, not a full historical-completeness claim.

Use `scripts/summarize_source_comparisons.py` to judge promotion from repeated canary artifacts. As of the June 18, 2026 downloaded canary set, the full single-app profile has 5 clean replacement-candidate runs and passes the current promotion gate: 2,479 RSS reviews vs 2,500 web catalog reviews, 8 recovered `429` pages, 0 unrecovered `429` pages, and 0 incomplete scopes. The mixed all-run history is still not ready because it includes an incomplete multi-app run.

The web catalog endpoint has been verified to return full review text rows, not only review IDs. A local smoke run fetched PayPal web catalog rows with review ID, user name, rating, title, review text, and date, then loaded 20 rows into a temporary Postgres database under `source='apple_app_store_web_catalog_reviews'`.

As of the June 18, 2026 Postgres scorecard, web catalog controlled ingestion is `ready_for_controlled_promotion`: 20 web catalog scopes have data, all 20 are at or above RSS parity, no tested web scope is below RSS, and the cumulative web catalog table holds 15,962 reviews. This is enough to keep proving the source in controlled production-style runs, but it is not yet a claim that the undocumented web catalog endpoint is a contractual production source or that all 200 target scopes have been covered.

The downloaded workflow-artifact history also supports the controlled-promotion call: 21 clean full single-app runs, all 21 at or above 500 reviews, 673 final `200` pages, 0 final non-200 pages, 0 fetch errors, 0 missing text/rating, and 8 recovered retry pages. This is stronger operational evidence than the RSS path for the tested scopes because the web catalog runs reached equal or higher per-app volume while preserving complete text/rating rows and clean final page status.

The next source decision question is whether complete backfill can be proven beyond RSS parity. Use the manual `App Store Web Catalog Backfill` workflow with `max_pages_per_app_country=0` for selected scopes. A complete result requires `no_next_href`; if the run stops on budget or throttling, continue later from the reported `start_page` and treat the total as a lower bound until the terminal no-next page is observed.
