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

Official Google Play and Apple APIs are strong for owned or authorized apps, but they are probably not enough for public third-party app review collection. If public storefront pages cannot provide clean full-review pagination, the likely next serious path is a licensed app-intelligence provider.

