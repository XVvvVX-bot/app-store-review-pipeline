# Source Replacement Options

## Goal

Find a source that is more stable than Apple RSS while returning the same order of magnitude or more review rows for public third-party apps.

## Current Evidence

| Source | Result | Decision |
| --- | --- | --- |
| Apple iTunes customer reviews RSS | Stable enough for daily public recent-review ingestion, structured JSON, but practical window is about 10 pages x 50 reviews per app-country scope. | Keep as primary baseline. |
| App Store HTML / Playwright | Public pages expose rating signals and a small visible review set. Scrolling did not load deeper review rows in browser checks. | Diagnostic only. |
| Apple public web catalog reviews | Structured JSON and better than visible HTML, but deep pagination repeatedly hit unrecovered `429`; a 10-app 25-page canary returned 1,220 web reviews vs 5,000 RSS reviews in 47 minutes. | Supplemental/canary only, not an RSS replacement. |
| App Store Connect API | Official and stable, but scoped to apps in the authenticated developer account. | Strong for owned/partnered apps only. Not a public third-party source. |

## Official Apple Path

Apple's App Store Connect API is the cleanest technical path when we own an app or have account access from a partner. Apple frames customer reviews as reviews for "your app", and App Store Connect API access is through role-based API keys and JWT authentication.

References:

- https://developer.apple.com/app-store-connect/api/
- https://developer.apple.com/documentation/appstoreconnectapi/customer-reviews
- https://developer.apple.com/documentation/appstoreconnectapi/get-v1-apps-_id_-customerreviews
- https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api/

This does not satisfy the current public third-party-app requirement unless we change the business scope to owned apps or partner-authorized apps.

## Licensed Provider Candidates

Licensed app-intelligence providers are the only path found so far that plausibly satisfies all requirements: public third-party apps, structured review rows, pagination, daily refresh, and production-grade access without login bypass, CAPTCHA solving, proxy rotation, or brittle hidden endpoints.

| Provider | Evidence From Public Docs | Fit | Caveats |
| --- | --- | --- | --- |
| 42matters / Similarweb | Documents an iOS reviews endpoint for any Apple App Store app, 3 QPS rate limit, `limit` 1-100, page pagination, default 30-day history, and up to 10 years with historical package. | Best first POC candidate because docs explicitly mention any iOS app and pagination. | Requires access token and paid Small plan or above for production use. Country/storefront semantics need live validation. |
| Appfigures | `/reviews` resource covers app reviews from supported platforms; public data add-on is required for products not owned by the account. | Strong candidate for third-party public apps if public-data add-on is approved. | Requires paid/add-on access. Need validate review row fields, pagination, countries, refresh cadence. |
| AppTweak | Review search endpoint returns review entries for an app/country with `limit` up to 500 and offset pagination; App Store API covers 100+ countries. | Strong candidate for large batches and country-aware analysis. | API availability is plan-based. Docs say reviews AppTweak has been able to gather, so completeness needs POC validation. |
| AppFollow | Reviews API endpoint returns reviews for an application or collection with pagination when using `ext_id`. | Useful candidate for review-management workflow and exports. | Public third-party coverage and pricing/credit limits need vendor confirmation. |
| Appbot | RESTful JSON API supports iOS, Google Play, Windows; API access is an add-on for larger plans. | Possible candidate if review-management product fits analyst workflow. | Public third-party/competitor coverage is less explicit in public docs; confirm before integration. |

References:

- 42matters: https://42matters.com/docs/app-market-data/ios/apps/reviews
- Appfigures: https://docs.appfigures.com/api/reference/v2/reviews
- AppTweak reviews search: https://developers.apptweak.com/reference/app-reviews-search
- AppFollow reviews API: https://docs.api.appfollow.io/reference/reviews_api_v2_reviews_get-1
- Appbot API: https://appbot.co/features/api/

## Recommendation

Do not keep searching for a hidden public Apple endpoint as the main plan. We have enough evidence that public HTML and web catalog paths are either shallow or unstable under deep pagination.

The next realistic path is a licensed-provider POC:

1. Start with 42matters because its public docs most directly match the current requirement.
2. Run a 10-app, 30-day probe with `limit=100` and enough pages to exceed the RSS 500-review window when available.
3. Compare against the same targets in RSS:
   - provider page success rate
   - provider review count vs RSS unique reviews
   - total reviews available
   - stable review identity or deterministic dedupe quality
   - country/language semantics
   - runtime and rate-limit behavior
4. If 42matters passes, add a provider ingestion mode into Postgres with `source='provider_42matters_ios_reviews_api'`.
5. If it fails or pricing is not acceptable, evaluate Appfigures and AppTweak next.

## 42matters Probe

The repository includes token-gated probe and comparison commands. They do not load Postgres and redact the token from saved report URLs.

Single-source probe:

```bash
APP_STORE_42MATTERS_TOKEN=... \
.venv/bin/python app_store_pipeline.py probe-42matters \
  --limit 10 \
  --days 30 \
  --page-limit 5 \
  --request-limit 100 \
  --request-delay-seconds 0.4
```

The report is written under `data/reports/provider_42matters/{run_id}/provider_probe_report.json`.

RSS-vs-provider comparison:

```bash
APP_STORE_42MATTERS_TOKEN=... \
.venv/bin/python app_store_pipeline.py compare-42matters \
  --limit 10 \
  --provider-days 30 \
  --provider-page-limit 5 \
  --provider-request-limit 100 \
  --provider-request-delay-seconds 0.4 \
  --rss-request-delay-seconds 0.5
```

The report is written under `data/reports/provider_compare/{run_id}/provider_comparison_report.json`. Use the replacement gate only after checking provider page success rate, review volume vs RSS, per-app ratios, and runtime.

Do not commit provider API tokens or raw credentials. Use environment variables or GitHub Actions secrets only.
