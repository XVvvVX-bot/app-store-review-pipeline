# Source Decision Notes

## Goal

Evaluate whether public Google Play or Apple App Store review data can support downstream analytics better than the Steam testbed.

## Why This Is Separate

The main pipeline should stay stable while we test data-source viability. This repo lets us inspect access, metadata, and review-depth signals locally without changing production workflow code.

## Initial Source Read

Official owner APIs:

- Google Play Developer API can retrieve structured review data for authorized developer apps. This is useful for owned or partner apps, but it does not solve public third-party review collection.
- App Store Connect API can retrieve structured customer reviews for apps in an App Store Connect account. This is also useful for owned or partner apps, not broad public app coverage.

Public storefront pages:

- The local smoke test can confirm that app pages are reachable and contain review/rating signals.
- This does not prove complete review-row access, stable review IDs, or clean pagination.

Licensed providers:

- If storefronts do not expose a clean, documented path, evaluate providers such as Appfigures, AppFollow, AppTweak, or Sensor Tower before building production ingestion.

## Evidence To Collect

- Reachability by app and platform.
- Access-control markers.
- Review/rating markers.
- Structured-data markers.
- Any visible evidence of full-review pagination.
- Whether full review text and stable review IDs can be obtained without forbidden behavior.
- Whether daily incremental refresh is possible without full-history rework.

