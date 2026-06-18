# Source Decision Notes

## Current Decision

Use Apple App Store public RSS as the complete pipeline source for this repository.

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

The current best replacement-source candidate is a licensed provider API, not App Store HTML or the public web catalog endpoint. See [source_replacement_options.md](source_replacement_options.md).
