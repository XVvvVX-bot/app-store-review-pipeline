# App Store Source Notes

## Apple RSS Sparse Empty Pages

Apple's iTunes customer reviews RSS can return pages with no `feed.entry` values while still including a `next` link. A later page in the same `1..10` window can contain review rows.

The pipeline therefore treats empty pages with `next` links as sparse pages and continues until the page cap or `--max-consecutive-empty-pages` is reached. This avoids stopping on page 1 when page 2, page 3, or later pages still contain reviews.

## App Store HTML Pages

Public App Store product pages are useful for verification. The HTML commonly includes JSON-LD aggregate rating metadata and a visible `Ratings & Reviews` section.

Live checks on June 18, 2026 showed examples where RSS returned an empty page but HTML clearly had review signals:

- Peacock: App Store HTML showed about 3.4M ratings and visible review cards.
- WhatsApp: App Store HTML showed about 18M ratings and visible review cards.
- YouTube: App Store HTML showed about 47.7M ratings.

However, HTML pages are not a better primary bulk review source:

- The product page and `see-all=reviews` page expose only a small visible set of review cards in browser checks.
- Playwright checks on June 18, 2026 found 6 visible user review cards on the Amazon Shopping `see-all=reviews` page, plus the editorial card. Scrolling to the bottom did not add more review cards or trigger a review-pagination network request. The HTML page is therefore useful as a diagnostic surface, not a deeper ingestion path.
- The HTML serialized server data and public web catalog app lookup expose a `next` review href. A direct public next-page request must include `platform=iphone`; without it the API returns a missing-parameter error.
- The public web catalog reviews endpoint accepts `sort=recent`; `sortBy=recent`, `orderBy=recent`, and similar guesses were rejected or ignored. `sort=recent` returned review dates in recent order during live checks.
- The public web catalog reviews endpoint accepts `limit=20`, returning 20 reviews per page for tested apps. `limit=50` and `limit=100` returned `400 Invalid Parameter Value` with a message that the value must be less than or equal to 20.
- The web catalog `next` href omits `sort=recent` and `limit=20`, so a client must preserve both parameters while following pagination.
- A bounded `probe-web --limit 20 --attempt-pagination --max-web-pages 2` check on June 18, 2026 found that the first web catalog page usually returned 6 reviews and the next page often returned another 6, but some scopes returned `429 API capacity exceeded`.
- A single-app depth check against Amazon Shopping on June 18, 2026 reached 15 successful web catalog review pages at a fast delay and 17 successful pages with a 1-second delay before `429 API capacity exceeded`. That is materially deeper than visible HTML but still below the RSS 10-page window in review volume.
- A later single-app backoff check reached 20 successful recent-sort web catalog review pages and 120 reviews for Amazon Shopping. Three pages initially returned `429`, then recovered after a 45-second retry delay. The 429 responses did not include a `Retry-After` header in captured response headers.
- A 20-app recent-sort backoff check reached 40 successful web catalog pages and 240 reviews. Three pages initially returned `429`, then recovered after a 30-second retry delay. A same-target RSS fetch during the same investigation returned 50 reviews across 21 RSS pages.
- A GitHub-hosted 5-app comparison run with 1-second web delay, one 30-second 429 retry, and 2 web pages per scope failed the single-run gate: RSS returned 50 reviews, while web catalog returned 42 reviews with 3 pages still at `429` after retry.
- A second GitHub-hosted 5-app comparison run with 2-second web delay, three 45-second 429 retries, and the same 2 web pages per scope passed the single-run gate: RSS returned 50 reviews, while web catalog returned 60 reviews with all 10 web pages at `200`.
- A GitHub-hosted 20-app comparison run with 2-second web delay, three 45-second 429 retries, and only 2 web pages per scope completed with all 40 web pages at `200`, including 2 recovered `429` pages. It did not pass the volume gate because RSS returned 400 reviews while web catalog was capped at 240 page-level reviews by configuration. That confirmed the canary must run as a capacity test, not only a liveness probe.
- A GitHub-hosted manual 20-app comparison with 10 web pages per scope was still running after 21 minutes and was canceled as too heavy for the scheduled canary default. Keep 10-page runs as manual depth tests unless later evidence shows stable runtimes.
- A GitHub-hosted 20-app comparison with 5 web pages per scope and the old implicit 6-review page size completed successfully in 6m57s. All 100 web catalog pages finished at `200` after bounded retry, with 2 recovered `429` pages. However, RSS returned 3,400 reviews in the same target window while web catalog returned 600 page-level reviews. The scheduled canary now requests `limit=20`, which should raise the 5-page web ceiling from about 600 to about 2,000 reviews for 20 app-country scopes.
- A GitHub-hosted 20-app comparison with 5 web pages per scope and `limit=20` completed successfully in 11m22s. All 100 web catalog pages finished at `200` after bounded retry, with 5 recovered `429` pages. RSS returned 9,939 unique reviews in the same target window; web catalog returned 2,000 page-level reviews. That is lower than RSS, but still the same order of magnitude for the 20-app canary window.
- A GitHub-hosted 10-app deep comparison with 25 web pages per scope and `limit=20` completed successfully in about 47 minutes, but it did not pass the replacement or stability gates. RSS returned 5,000 unique reviews with 0 fetch errors. Web catalog had enough configured capacity to reach 5,000 reviews, but returned only 1,220 reviews, with 61 final `200` pages and 13 final `429` pages after bounded retry. Every tested app-country scope eventually stopped on unrecovered `429`, so the volume gap was caused by deep-pagination stability, not the configured page cap.
- A local Playwright rendered-HTML probe on Amazon Shopping on June 18, 2026 saw 6 rendered review title IDs before scrolling and 6 after scrolling. It found no new review IDs after scroll and no review API requests after the initial page load.
- A local 2-app RSS-vs-web-catalog check on June 18, 2026 with 3 web pages per scope and `limit=20` returned 6/6 web pages at `200`, but RSS returned 1,000 unique reviews while web catalog returned 120 page-level reviews. The lower web count was configuration-limited and did not pass the replacement gate.
- After adding `Retry-After` support and configurable 429 backoff on June 18, 2026, a local 1-app deep check with 25 web pages per scope returned 25/25 web pages at `200`, 500 web reviews, and 500 RSS reviews in about 57 seconds. It passed the replacement gate for Amazon Shopping.
- The same conservative deep profile on 3 apps returned 75/75 web pages at final `200`, including 2 recovered `429` pages, 1,500 web reviews, and 1,500 RSS reviews in about 3m30s. It passed the replacement gate for Amazon Shopping, Walmart, and Target, but still needs a larger GitHub-hosted canary before promotion.
- A later GitHub-hosted 10-app deep run with 25 web pages per scope, `limit=20`, five 429 retries, and 1.5x backoff exceeded the 60-minute workflow timeout in the comparison step. This confirms that deep web catalog parity tests are too heavy for routine automation even with conservative retry behavior.
- The web catalog probe can now skip the separate HTML page request. Use that mode for stability and volume comparisons because it measures the structured JSON review endpoint directly and reduces one request per app-country scope.
- A local 2-app `--web-skip-html` comparison on June 18, 2026 with 5 web pages per scope returned 10/10 web catalog JSON pages at `200`, 0 recovered or unrecovered `429` pages, and 200 web reviews. RSS returned 1,000 unique reviews in the same target window, so the web catalog volume gap was configuration-limited: the report estimated 25 web pages per scope would be needed for RSS parity.
- A GitHub-hosted 5-app `--web-skip-html` canary on June 18, 2026 completed in 1m37s with 25/25 web catalog JSON pages at `200`, no retries, no unrecovered `429` pages, 500 web reviews, and 2,495 RSS unique reviews. It passed the same-order stability gate but not the replacement gate because the 5-page web cap was intentionally too low; the report again estimated 25 web pages per scope for RSS parity.
- The public web catalog path is now a serious candidate for a richer-than-HTML diagnostic or supplemental path, but it is still an undocumented web surface and not yet the default production source.
- The HTML shape is less stable than the RSS JSON structure.
- The aggregate rating count proves review presence, but does not provide a complete review-row feed.

Use HTML, Playwright, and `probe-web` checks as diagnostics for source health and coverage, not as the main ingestion path.

Run a bounded web probe with:

```bash
.venv/bin/python app_store_pipeline.py probe-web --limit 20 --web-sort recent --attempt-pagination --max-web-pages 5 --review-limit 20 --request-delay-seconds 2 --web-429-retries 3 --web-429-retry-seconds 45 --web-429-backoff-multiplier 1 --skip-html
```

For conservative deep-pagination tests, raise `--web-429-backoff-multiplier` above `1`. The retry helper honors `Retry-After` if Apple returns it; otherwise each additional 429 retry waits `web_429_retry_seconds * web_429_backoff_multiplier^(attempt-1)`.

Run a rendered HTML probe with Playwright when we need browser-level evidence:

```bash
npm install
npx playwright install chromium
npm run probe:rendered-html -- \
  --url "https://apps.apple.com/us/app/amazon-shopping/id297606951?see-all=reviews&platform=iphone" \
  --output data/reports/rendered_html/amazon-shopping.json \
  --scrolls 8 \
  --wait-ms 1000
```

If `new_review_ids_after_scroll` is empty and no review-related network requests appear, the browser-rendered page did not expose deeper review data beyond the initial visible cards for that test case.

## Web Catalog Canary Promotion Gate

The `App Store Web Catalog Canary` workflow runs both the RSS path and the candidate web catalog path on the same bounded target window using GitHub-hosted Ubuntu. It uploads `data/reports/source_compare/{run_id}/source_comparison_report.json` and the readable `source_comparison_report.md`.

Do not promote web catalog reviews into the production ingestion path until several scheduled canary runs show:

- `web_catalog.web_catalog_page_status_counts` is dominated by `200` responses after bounded retry.
- `comparison.web_unrecovered_429_page_count` is consistently `0`, or low enough that the source still completes within the intended runtime budget.
- `recovered_429_page_count` stays small enough that the workflow runtime remains predictable.
- `comparison.web_reviews_same_order_as_rss` is consistently true for the same target window. `comparison.web_reviews_at_or_above_rss` remains the stronger bar for a true RSS replacement.
- `comparison.web_page_depth_can_reach_rss_parity` is true for any run used to judge RSS replacement, or `comparison.web_volume_gap_likely_configuration_limited` is false when web volume is lower than RSS.
- `source_decision.status` is `web_catalog_replacement_candidate` across repeated canary windows. Treat `needs_deeper_web_catalog_run`, `same_order_but_not_replacement`, and `web_catalog_unstable_after_retry` as evidence against immediate promotion.
- `web_catalog.web_catalog_page_reviews_total` is nonzero and commercially useful for the sampled target set.
- `comparison.web_all_pages_ok_after_retry` is consistently true.
- `web_sort` is `recent`, and page date ranges move backward in time as offsets increase.
- The source still works without login, CAPTCHA solving, proxy rotation, or private App Store Connect credentials.

If the canary passes those gates, the next implementation step is a separate web-catalog ingestion mode with its own `source` value, not a silent replacement of RSS rows.
