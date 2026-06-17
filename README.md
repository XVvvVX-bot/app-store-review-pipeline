# App Store Review Source Test

Local feasibility repo for testing whether public Google Play or Apple App Store review data can support a future live review pipeline.

This repo is intentionally separate from the main Steam pipeline. It is for source evaluation only.

## Boundaries

- Use public app detail pages only for no-login smoke tests.
- Do not use login state, personal cookies, CAPTCHA solving, proxy rotation, hidden endpoints, or anti-bot bypass behavior.
- Do not build a production ingestion pipeline until a source passes the feasibility gate.
- Do not treat visible ratings/review snippets as proof of complete review-row access.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Commands

Summarize the local target list:

```bash
.venv/bin/python -m app_store_source_probe targets
```

Run a conservative public storefront smoke test:

```bash
.venv/bin/python -m app_store_source_probe storefront-smoke \
  --limit 3 \
  --output data/reports/storefront_smoke.json
```

Run the public Apple iTunes customer reviews RSS probe:

```bash
.venv/bin/python -m app_store_source_probe apple-rss-probe \
  --limit 5 \
  --max-pages 10 \
  --output data/reports/apple_rss_limit5_pages10_us.json
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

## What A Passing Source Must Prove

A future source should pass only if it supports:

- Public third-party apps, not only apps we own.
- Full written review text, rating, date, app identity, platform, country or locale where available, and stable review identity or a reliable dedupe key.
- Enough depth for downstream analytics: at least thousands of reviews for popular apps, not just top-visible snippets.
- Daily incremental refresh without repeatedly re-ingesting full history.
- Clean operation with clear access terms.
- Practical Postgres-backed production path.

## Current Hypothesis

Official Google Play and Apple APIs are strong for owned or authorized apps, but they are not enough for public third-party app review collection. Apple also exposes a public iTunes customer reviews RSS feed that can return structured recent review rows for public apps, but early evidence shows a practical cap of 10 pages x 50 reviews per app per country. Google Play public pages are reachable but have not proven a clean public review-row API.

See `docs/feasibility_report.md` for the current full recommendation.
