# App Store / Google Play Review Source Feasibility Report

Date: 2026-06-17  
Repo: `App-Store-Review-Source-Test`  
Scope: public third-party app reviews only, no login or owner credentials

## Executive Summary

Apple App Store reviews are **conditionally viable** as a v1 public review source if our product goal can work with **recent written reviews by country**, rather than complete all-time review history. The public iTunes customer reviews RSS JSON feed returned structured review rows for public apps with stable review IDs, title, body text, rating, updated timestamp, app version, author display name, and vote fields. In live tests, 5 mainstream apps returned 2,500 unique review rows across 50 pages with no missing text, rating, or timestamp fields.

Google Play is **not viable as a direct public third-party review source** under our current ethical and maintainability constraints. The official Google Play reviews API is for authorized developer apps, while public Google Play pages only proved page reachability and review/rating markers. I did not find or validate a clean documented public Google Play review-row API that provides full text, stable review IDs, and pagination without using hidden endpoints or scraper-style behavior.

Recommended next direction:

1. Use Apple public RSS as a limited local prototype source for recent iOS reviews.
2. Do not build direct Google Play public ingestion from storefront pages.
3. If the project needs cross-platform app coverage, especially Android, evaluate licensed providers such as Appfigures, AppFollow, AppTweak, or Sensor Tower before writing production ingestion.
4. Treat Apple RSS as a research-grade public source until terms, rate limits, and long-running daily refresh behavior are reviewed.

## Evaluation Criteria

A source should pass only if it supports:

- Public third-party apps, not only owned apps.
- Full written review text, rating, date, app identity, platform, country/locale where available, and a stable review ID or reliable dedupe key.
- Enough depth for analytics: ideally thousands of reviews per popular subject, not only top snippets.
- Daily incremental refresh without repeatedly re-ingesting full history.
- Clean operation: no login bypass, cookies, CAPTCHA solving, proxy rotation, hidden endpoints, or anti-bot evasion.
- A clear path into a Postgres-backed cumulative dataset.

## Evidence Collected

### Public storefront smoke test

Command:

```bash
.venv/bin/python -m app_store_source_probe storefront-smoke \
  --limit 20 \
  --delay-seconds 0.25 \
  --output data/reports/storefront_smoke_limit20.json
```

Result:

- Google Play: 20/20 app detail pages fetched successfully.
- Apple App Store: 20/20 app detail pages fetched successfully.
- Access-control markers: 0 on both platforms.
- Review/rating markers: 20/20 on both platforms.
- Production candidates: 0, because page-level markers do not prove full review-row access, stable review IDs, or clean pagination.

Interpretation: both stores expose public app pages, but storefront HTML alone is not enough for a production review pipeline.

### Apple public RSS depth test

Command:

```bash
.venv/bin/python -m app_store_source_probe apple-rss-probe \
  --limit 5 \
  --max-pages 10 \
  --delay-seconds 0.25 \
  --output data/reports/apple_rss_limit5_pages10_us.json
```

Result:

- Apps tested: Amazon Shopping, Walmart, Target, Uber, Lyft.
- Pages requested: 50.
- OK pages: 50.
- Review rows: 2,500.
- Unique review rows: 2,500.
- Duplicate rows: 0.
- Missing text: 0.
- Missing rating: 0.
- Missing updated timestamp: 0.
- Each app returned 500 unique rows in the US storefront.

Fields observed:

- `review_id`
- `author_name`
- `updated_at`
- `rating`
- `version`
- `title`
- `content`
- `vote_sum`
- `vote_count`
- `country`
- `app_id`
- `page`

Interpretation: Apple RSS provides real structured review rows suitable for local prototype ingestion.

### Apple RSS page limit test

Manual check:

- Amazon Shopping page 10: HTTP 200, 50 entries.
- Amazon Shopping page 11: HTTP 400.
- Walmart page 10: HTTP 200, 50 entries.
- Walmart page 11: HTTP 400.
- ChatGPT page 10: HTTP 200, 50 entries.
- ChatGPT page 11: HTTP 400.

Interpretation: current practical cap is 10 pages x 50 reviews = 500 reviews per app per country per sort order.

### Apple RSS country test

Manual check for Amazon Shopping page 1:

- `us`: HTTP 200, 50 entries.
- `ca`: HTTP 200, 50 entries.
- `gb`: HTTP 200, 0 entries.
- `au`: HTTP 200, 50 entries.
- `de`: HTTP 200, 0 entries.
- `jp`: HTTP 200, 0 entries.

Interpretation: the feed is country-specific. Global coverage would require country-by-country collection, and some country/app combinations may have no returned entries.

### Local test suite

Command:

```bash
.venv/bin/python -m pytest -q
```

Result:

- 9 tests passed.

## Source-By-Source Assessment

### Apple App Store Connect API

Assessment: strong for owned or partner-authorized apps, not for public third-party competitive review collection.

Pros:

- Official structured API.
- Rich review fields.
- Authenticated and maintainable.

Cons:

- Requires App Store Connect credentials.
- Framed around reviews for apps in the authorized account.
- Does not meet the current public-third-party requirement.

Use if: we later work with owned apps or partner apps.

### Apple iTunes Customer Reviews RSS

Assessment: best direct public-source candidate found so far.

Pros:

- No login required.
- Works for public third-party apps.
- Structured JSON.
- Stable review IDs observed.
- Full written text, rating, title, timestamp, version, and vote fields observed.
- Simple pagination up to 10 pages.
- Can support daily incremental sync by polling recent pages and stopping when known review IDs are reached.

Cons:

- Practical cap appears to be 500 reviews per app per country per sort.
- Not complete all-time history for high-volume apps.
- Country-specific; global coverage multiplies request volume.
- Does not include star-only ratings without written reviews.
- Long-term rate limits and terms need review before production.
- Apple may change or remove this RSS behavior more easily than a formal authenticated API.

Production fit: conditional. Good for recent-review analytics and trend monitoring; weak for full historical backfill.

### Google Play Developer Reviews API

Assessment: strong for owned or partner-authorized apps, not for public third-party competitive review collection.

Pros:

- Official structured API.
- Review ID, review text, timestamp, rating, app version, device metadata, helpful votes, and developer reply fields.
- Pagination and recent-review workflow.

Cons:

- Requires OAuth/service-account authorization for a developer app.
- Public third-party apps are out of scope.
- Documentation notes comment/recent-review constraints.

Use if: we later collect from owned apps or authorized partner apps.

### Google Play Public Storefront

Assessment: not viable as direct production source under current constraints.

Pros:

- Public app pages are reachable.
- Page-level review/rating signals are visible.
- App-level rating counts are useful metadata.

Cons:

- No clean documented public review-row API validated.
- No stable public review IDs or review cursor validated.
- Full review text/pagination would likely require hidden endpoints or scraper-specific behavior, which is outside project boundaries.

Production fit: no, unless a documented public source or licensed provider is used.

### Licensed Providers

Assessment: likely best option for cross-platform app-review coverage.

Candidates to evaluate:

- Appfigures Public Data API.
- AppFollow Reviews API.
- AppTweak App Reviews API.
- Sensor Tower Connect / data feed.

Pros:

- Designed for public third-party app intelligence.
- Likely to support both Apple and Google.
- Better chance of historical depth, stable schemas, and commercial terms.

Cons:

- Paid.
- Terms and redistribution rights need review.
- Provider coverage and field-level completeness must be tested before committing.

Production fit: likely best if we need both iOS and Android app reviews.

## Recommended Architecture If We Continue With Apple RSS Prototype

Use the existing staged pipeline pattern, but keep it in this separate repo until source viability is accepted:

1. Targets:
   - `app_id`
   - `app_name`
   - `country`
   - `sort_order`
   - `active`
2. Fetch:
   - Poll pages 1..10 for each active app/country.
   - Save raw RSS JSON per run.
   - Stop early if a page has no entries or if all reviews on the page are already known.
3. Normalize:
   - Use `review_id` as the primary review identity.
   - Store title, content, rating, updated timestamp, version, vote fields, app ID, country, source.
4. Load:
   - Upsert into Postgres by `(source, platform, country, app_id, review_id)`.
5. Incremental:
   - Daily fetch recent pages.
   - Stop when the current page is older than the app/country watermark or when all IDs are already known.
6. Validation:
   - Row count, duplicate count, missing text/rating/date, app/country coverage, newest timestamp, oldest timestamp.

## Recommendation To John

Do not use Steam as the primary business source. It is still useful as an ingestion testbed.

Do not use direct Google Play public storefront scraping as the v1 source. It does not meet the clean-source requirement.

Apple public RSS is worth a prototype because it gives real public third-party review rows without login. However, it should be presented honestly as a recent-review source with a likely 500-review-per-country cap, not as complete historical coverage.

For a commercially strong cross-platform pipeline, the highest-confidence next step is to trial a licensed app-review data provider and compare it against the Apple RSS prototype on:

- review depth,
- historical backfill,
- Google Play coverage,
- daily update latency,
- stable IDs,
- terms,
- pricing,
- field completeness.

## Final Feasibility Verdict

| Source | Public third-party? | Full text rows? | Pagination/depth | Incremental path | Production recommendation |
| --- | --- | --- | --- | --- | --- |
| Apple RSS | Yes | Yes | Medium, likely 500/app/country | Yes, poll recent pages and dedupe by review ID | Prototype candidate |
| Apple App Store Connect | No, owned/authorized only | Yes | Strong for authorized apps | Yes | Use only for owned/partner apps |
| Google Play public pages | Yes | Not proven | Weak | Not proven | Do not use directly |
| Google Play Developer API | No, owned/authorized only | Yes | Recent/comment-focused authorized access | Yes | Use only for owned/partner apps |
| Licensed providers | Yes, depending on terms | Likely | Likely strongest | Likely | Best production candidate if budget allows |

Bottom line: **Apple RSS can support a limited public-review prototype; Google Play needs either owned-app credentials or a licensed provider; a complete commercial v1 should probably use a paid provider if Android coverage and historical depth are required.**

## References

- Google Play Developer API review list: https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/list
- Google Play Reply to Reviews API: https://developers.google.com/android-publisher/reply-to-reviews
- Google Play review resource fields: https://developers.google.com/android-publisher/api-ref/rest/v3/reviews
- Apple App Store Connect customer reviews: https://developer.apple.com/documentation/appstoreconnectapi/customer-reviews
- Apple customer review fields: https://developer.apple.com/documentation/appstoreconnectapi/customerreview/attributes-data.dictionary
- Apple App Store Connect API authentication: https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api
- Observed Apple public RSS pattern: `https://itunes.apple.com/us/rss/customerreviews/page=1/id={app_id}/sortby=mostrecent/json`
- Appfigures Reviews API: https://docs.appfigures.com/api/reference/v2/reviews
- AppFollow Reviews API: https://docs.api.appfollow.io/reference/reviews_api_v2_reviews_get-1
- AppTweak App Reviews API: https://developers.apptweak.com/reference/app-reviews
- Sensor Tower Connect: https://sensortower.com/product/connect
