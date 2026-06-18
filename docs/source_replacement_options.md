# Source Replacement Options

## Goal

Find a source that is more stable than Apple RSS while returning the same order of magnitude or more review rows for public third-party apps.

## Current Evidence

| Source | Result | Decision |
| --- | --- | --- |
| Apple iTunes customer reviews RSS | Stable enough for daily public recent-review ingestion, structured JSON, but practical window is about 10 pages x 50 reviews per app-country scope. | Keep as primary baseline. |
| App Store HTML / Playwright | Public pages expose rating signals and a small visible review set. Scrolling did not load deeper review rows in browser checks. | Diagnostic only. |
| Apple public web catalog reviews | Structured JSON and better than visible HTML. Early deep pagination repeatedly hit unrecovered `429`, but the newer conservative profile with `Retry-After` support and configurable backoff passed local 1-app and 3-app RSS parity checks. | Candidate supplemental/replacement path, but not production default until a larger GitHub-hosted canary repeats the result. |
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
| Appfigures | `/reviews` resource covers app reviews from supported platforms; public data add-on is required for products not owned by the account. Product lookup by Apple ID is available through `/products/apple/{id}`. | Strong candidate for third-party public apps if public-data add-on is approved. A token-gated probe and RSS comparison harness is now available. | Requires paid/add-on access. Need validate review row fields, pagination, countries, refresh cadence. |
| AppTweak | Review search endpoint returns review entries for an app/country with `limit` up to 500 and offset pagination; App Store API covers 100+ countries. | Strong candidate for large batches and country-aware analysis. A token-gated probe and RSS comparison harness is now available. | API availability is plan-based. Docs say reviews AppTweak has been able to gather, so completeness needs POC validation. |
| AppFollow | Reviews API endpoint returns reviews for an application or collection with pagination when using `ext_id`. | Useful candidate for review-management workflow and exports. | Public third-party coverage and pricing/credit limits need vendor confirmation. |
| Appbot | RESTful JSON API supports iOS, Google Play, Windows; API access is an add-on for larger plans. | Possible candidate if review-management product fits analyst workflow. | Public third-party/competitor coverage is less explicit in public docs; confirm before integration. |

References:

- 42matters: https://42matters.com/docs/app-market-data/ios/apps/reviews
- Appfigures: https://docs.appfigures.com/api/reference/v2/reviews
- AppTweak reviews search: https://developers.apptweak.com/reference/app-reviews-search
- AppFollow reviews API: https://docs.api.appfollow.io/reference/reviews_api_v2_reviews_get-1
- Appbot API: https://appbot.co/features/api/

## Recommendation

Do not keep searching for a hidden public Apple endpoint as the main plan. We have enough evidence that public HTML and web catalog paths are either shallow or unstable under deep pagination. The web catalog canary now writes `source_decision.status`; only `web_catalog_replacement_candidate` should trigger repeated promotion testing. `needs_deeper_web_catalog_run`, `same_order_but_not_replacement`, and `web_catalog_unstable_after_retry` keep web catalog in diagnostic or supplemental territory.

The next realistic path is a licensed-provider POC:

1. Start with 42matters because its public docs most directly match the current requirement.
2. Run a 10-app, 30-day probe with `limit=100` and enough pages to exceed the RSS 500-review window when available.
3. Compare against the same targets in RSS:
   - provider page success rate
   - provider review count vs RSS unique reviews
   - total reviews available
   - configured provider fetch ceiling and whether the POC page cap, not the provider source, caused a lower fetched count
   - stable review identity or deterministic dedupe quality
   - country/language semantics
   - runtime and rate-limit behavior
4. If 42matters passes, add a provider ingestion mode into Postgres with `source='provider_42matters_ios_reviews_api'`.
5. If it fails or pricing is not acceptable, evaluate Appfigures and AppTweak next using the same report gates.

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

The report is written under `data/reports/provider_compare/{run_id}/provider_comparison_report.json`. Use the replacement gate only after checking provider page success rate, review volume vs RSS, per-app ratios, capacity diagnostics, and runtime.

Important fields:

- `comparison.candidate_passes_replacement_gate`: provider fetched at least as many reviews as RSS, with no provider non-200 pages and no RSS fetch errors.
- `comparison.provider_volume_gap_likely_configuration_limited`: provider returned fewer reviews than RSS, but the report suggests the POC page/request cap or remaining provider inventory caused the gap.
- `comparison.provider_reported_total_reviews`: provider-reported available review inventory across sampled rows, when available.
- `comparison.provider_additional_pages_per_row_needed_for_rss_parity`: approximate extra provider pages per app or app-country row needed to match the RSS window.
- `per_app[].provider_more_available`: this app or app-country has provider-reported inventory beyond what the POC fetched.

Do not commit provider API tokens or raw credentials. Use environment variables or GitHub Actions secrets only.

## Provider Matrix

Use the provider matrix runner when one or more licensed-provider tokens are configured and we want one comparable POC artifact:

```bash
.venv/bin/python scripts/run_provider_matrix.py \
  --limit 10 \
  --provider-page-limit 2 \
  --provider-42matters-request-limit 100 \
  --provider-large-request-limit 500 \
  --rss-request-delay-seconds 0.5
```

The matrix runner detects `APP_STORE_42MATTERS_TOKEN`, `APP_STORE_APPTWEAK_TOKEN`, and `APP_STORE_APPFIGURES_TOKEN`. It writes `data/reports/provider_matrix/provider_matrix_summary.json` and `data/reports/provider_matrix/provider_matrix_report.md`, records missing-token providers as `missing_secret`, and captures each configured provider's comparison stdout/stderr. The matching GitHub Actions workflow is `App Store Provider Matrix Compare`.

Read `source_decision.status` first:

- `replacement_candidate_found`: at least one provider beat the RSS window with clean pages.
- `needs_deeper_provider_run`: a provider may beat RSS, but the POC page cap was too shallow.
- `same_order_but_not_replacement`: provider volume is useful, but not enough to replace RSS.
- `needs_provider_secret`: no provider token is configured yet.
- `configured_provider_runs_failed`: one or more configured provider runs failed.
- `no_provider_met_gate`: do not replace RSS from this matrix run.

## AppTweak Probe

The repository includes token-gated AppTweak probe and RSS comparison commands. They do not load Postgres.

Single-source probe:

```bash
APP_STORE_APPTWEAK_TOKEN=... \
.venv/bin/python app_store_pipeline.py probe-apptweak \
  --limit 10 \
  --page-limit 2 \
  --request-limit 500 \
  --request-delay-seconds 1
```

The report is written under `data/reports/provider_apptweak/{run_id}/provider_probe_report.json`.

RSS-vs-provider comparison:

```bash
APP_STORE_APPTWEAK_TOKEN=... \
.venv/bin/python app_store_pipeline.py compare-apptweak \
  --limit 10 \
  --provider-page-limit 2 \
  --provider-request-limit 500 \
  --provider-request-delay-seconds 1 \
  --rss-request-delay-seconds 0.5
```

The report is written under `data/reports/provider_compare/{run_id}/provider_comparison_report.json`. Judge it with the same replacement gate as 42matters: provider page success rate, provider review volume vs RSS, per-app or app-country ratios, capacity diagnostics, and runtime.

## Appfigures Probe

The repository includes token-gated Appfigures Public Data probe and RSS comparison commands. They do not load Postgres. Appfigures uses its own product IDs, so the probe first calls `/products/apple/{id}` for each Apple app ID and then calls `/reviews`.

Single-source probe:

```bash
APP_STORE_APPFIGURES_TOKEN=... \
.venv/bin/python app_store_pipeline.py probe-appfigures \
  --limit 10 \
  --page-limit 2 \
  --request-limit 500 \
  --request-delay-seconds 1
```

The report is written under `data/reports/provider_appfigures/{run_id}/provider_probe_report.json`.

RSS-vs-provider comparison:

```bash
APP_STORE_APPFIGURES_TOKEN=... \
.venv/bin/python app_store_pipeline.py compare-appfigures \
  --limit 10 \
  --provider-page-limit 2 \
  --provider-request-limit 500 \
  --provider-request-delay-seconds 1 \
  --rss-request-delay-seconds 0.5
```

The report is written under `data/reports/provider_compare/{run_id}/provider_comparison_report.json`. Judge it with the same replacement gate as 42matters and AppTweak: provider page success rate, provider review volume vs RSS, per-app or app-country ratios, capacity diagnostics, and runtime.
