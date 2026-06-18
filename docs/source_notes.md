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
- Adaptive RSS-parity stopping was added on June 18, 2026 so each web catalog app-country crawl can stop as soon as it matches that scope's RSS review count. This makes deeper parity tests less wasteful than a fixed-depth crawl.
- A GitHub-hosted 1-app adaptive parity run on June 18, 2026 with 25 max web pages, `limit=20`, 0.2s web delay, and short 429 retry settings reached RSS parity: 500 RSS reviews vs 500 web catalog reviews, 25/25 web pages at final `200`, and stop reason `target_review_count_reached`.
- A GitHub-hosted 3-app adaptive parity run on June 18, 2026 with 25 max web pages, `limit=20`, 0.5s web delay, and short 5s 429 retry settings did not pass replacement: 1,500 RSS reviews vs 1,120 web catalog reviews, 56 final `200` pages, 2 final `429` pages, 17 recovered 429 pages, and stop reasons `target_review_count_reached: 1` plus `non_200_page: 2`.
- A second GitHub-hosted 3-app adaptive parity run on June 18, 2026 with more conservative settings passed replacement: 1,500 RSS reviews vs 1,500 web catalog reviews, 75/75 web pages at final `200`, 5 recovered 429 pages, 0 unrecovered 429 pages, all 3 scopes stopped on `target_review_count_reached`, and runtime was about 4m7s.
- A GitHub-hosted 5-app adaptive parity run on June 18, 2026 without a canary-level time budget was still in the comparison step after about 25 minutes and was canceled. This showed that max page depth alone is not enough; deep web catalog tests also need a runtime budget so the workflow can upload a useful report instead of waiting for the job timeout.
- The web catalog comparison now supports `--web-time-budget-seconds`. If the budget is exhausted, the report records planned, completed, and skipped scopes; the source decision becomes `web_catalog_time_budget_exceeded` instead of allowing a replacement decision from an incomplete target window.
- A local 1-app adaptive parity smoke on June 18, 2026 with `--web-time-budget-seconds 15`, 25 max pages, `limit=20`, `--web-skip-html`, and `--web-stop-at-rss-parity` reached parity in about 13 seconds: 500 RSS reviews vs 500 web catalog reviews, 25/25 web pages at final `200`, 1 recovered `429`, and no skipped scopes.
- After adding the time budget guard, a GitHub-hosted 5-app adaptive parity canary on June 18, 2026 completed successfully in about 5 minutes: 2,500 RSS reviews vs 2,500 web catalog reviews, 125/125 web pages at final `200`, 6 recovered `429` pages, 0 unrecovered `429` pages, 0 skipped scopes, and all 5 scopes stopped on `target_review_count_reached`.
- A later scheduled 5-app canary for the same first target window also passed replacement, but runtime rose to about 10m48s and recovered `429` pages rose to 13. That still reached 2,500 RSS reviews vs 2,500 web catalog reviews with 125/125 final `200` pages, but showed rate-limit pressure increasing across repeated deep runs.
- A GitHub-hosted 5-app canary on the next target window, `target_offset=5`, did not pass replacement: it hit the 900-second web time budget after only 2 of 5 planned scopes, with 320 web reviews vs 2,500 RSS reviews, final status counts `200: 16` and `429: 2`, stop reasons `non_200_page: 1` and `time_budget_exceeded: 1`.
- The same `target_offset=5` window became stable when reduced to a single app. A DoorDash-only canary with 5-second page delay, 60-second 429 retry delay, and 1,200-second budget reached 500 RSS reviews vs 500 web catalog reviews, 25/25 final `200` pages, 0 retries, and stop reason `target_review_count_reached`.
- The canary schedule now uses a rotating single-app profile by default: one active target per run, auto-computed target offset, 25 max web pages, `limit=20`, 5-second page delay, 60-second 429 retry delay, 1.5x backoff, RSS-parity stopping, and a 1,200-second web budget. Manual workflow dispatch can still run larger fixed windows as stress tests.
- A manual full-profile `target_offset=auto` canary verified the scheduled-style rotation path: it selected American Airlines at offset 90 and reached 500 RSS reviews vs 500 web catalog reviews, 25/25 final `200` pages, 2 recovered `429` pages, and stop reason `target_review_count_reached` in about 3m23s.
- A source-comparison history summarizer now aggregates `source_comparison_report.json` artifacts across runs. On nine downloaded GitHub web canary reports from June 18, 2026, the all-run summary was still `not_ready`: seven replacement-candidate runs were offset by one `web_catalog_time_budget_exceeded` run, one shallow `needs_deeper_web_catalog_run` smoke, 32 recovered `429` pages, and 2 unrecovered `429` pages. Filtering to the full single-app profile (`--single-app-only --min-web-max-pages 25`) produced `ready_for_promotion`: 5/5 full single-app runs were replacement candidates, 2,479 RSS reviews vs 2,500 web catalog reviews, 8 recovered `429` pages, and 0 unrecovered `429` pages.
- A live web catalog payload check confirmed that review rows contain `id`, `date`, `rating`, `review`, `title`, and `userName`. The repository now includes `fetch-web-catalog` and `daily-web-catalog` commands that normalize these rows into the existing Postgres schema under `source='apple_app_store_web_catalog_reviews'`.
- A local `daily-web-catalog` smoke run against a temporary Postgres database loaded 20 PayPal web catalog review rows with 0 fetch errors and stored the run source correctly as `apple_app_store_web_catalog_reviews`.
- A second local `daily-web-catalog` overlap smoke against the same temporary database inserted 20 rows on the first run, then skipped 20 duplicates on the second run and left the cumulative review row count at 20. The second page row stopped with `caught_up_to_existing_reviews`, confirming incremental duplicate handling for the web catalog source.
- A repeat local Playwright rendered-HTML probe on Amazon Shopping on June 18, 2026 found 6 rendered review title IDs before scrolling and 6 after scrolling. Scrolling triggered 0 review-related requests and 0 review API requests, so Playwright-rendered HTML still did not expose deeper review rows than the initial visible cards.
- The HTML shape is less stable than the RSS JSON structure.
- The aggregate rating count proves review presence, but does not provide a complete review-row feed.

Use HTML, Playwright, and `probe-web` checks as diagnostics for source health and coverage, not as the main ingestion path.

Run a bounded web probe with:

```bash
.venv/bin/python app_store_pipeline.py probe-web --limit 20 --web-sort recent --attempt-pagination --max-web-pages 5 --review-limit 20 --request-delay-seconds 2 --web-429-retries 3 --web-429-retry-seconds 45 --web-429-backoff-multiplier 1 --skip-html
```

For conservative deep-pagination tests, raise `--web-429-backoff-multiplier` above `1`. The retry helper honors `Retry-After` if Apple returns it; otherwise each additional 429 retry waits `web_429_retry_seconds * web_429_backoff_multiplier^(attempt-1)`.

Add `--web-time-budget-seconds <seconds>` to manual depth tests so a throttled run can stop cleanly and upload a partial but interpretable report. A budget-exceeded report is evidence that the profile is too heavy for routine automation, even if some earlier apps reached parity.

Summarize downloaded canary artifacts with:

```bash
.venv/bin/python scripts/summarize_source_comparisons.py \
  --root /path/to/downloaded/canary/artifacts \
  --output-json /tmp/web_catalog_history.json \
  --output-markdown /tmp/web_catalog_history.md \
  --min-runs 5
```

For the scheduled-style conservative profile, filter to full single-app parity runs:

```bash
.venv/bin/python scripts/summarize_source_comparisons.py \
  --root /path/to/downloaded/canary/artifacts \
  --single-app-only \
  --min-web-max-pages 25 \
  --min-runs 5
```

Run a controlled web catalog ingestion trial with:

```bash
.venv/bin/python app_store_pipeline.py daily-web-catalog \
  --database-url postgresql:///app_store_reviews \
  --limit 1 \
  --target-offset 10 \
  --max-pages-per-app-country 25 \
  --review-limit 20 \
  --request-delay-seconds 5 \
  --web-429-retries 5 \
  --web-429-retry-seconds 60 \
  --web-429-backoff-multiplier 1.5
```

The matching `App Store Web Catalog Ingestion` workflow runs this conservative profile on the self-hosted macOS ARM64 runner every 6 hours at `15 3,9,15,21 * * *`. It writes to the local Postgres database, stores rows with `source='apple_app_store_web_catalog_reviews'`, and uploads `data/raw/apple_web_catalog/` plus `data/reports/apple_web_catalog/` as audit artifacts. Keep `limit=1` and `target_offset=auto` as the routine setting until the web catalog path has more operational history.

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
- `web_catalog.web_catalog_target_reached_scopes` increases during deeper tests with RSS-parity stopping enabled, and `web_catalog.web_catalog_stop_reasons` distinguishes parity stops from `max_pages` or `non_200_page` stops.
- `source_decision.status` is `web_catalog_replacement_candidate` across repeated canary windows. Treat `rss_baseline_empty`, `needs_deeper_web_catalog_run`, `same_order_but_not_replacement`, and `web_catalog_unstable_after_retry` as evidence against immediate promotion.
- `web_catalog.web_catalog_page_reviews_total` is nonzero and commercially useful for the sampled target set.
- `comparison.web_all_pages_ok_after_retry` is consistently true.
- `web_sort` is `recent`, and page date ranges move backward in time as offsets increase.
- The source still works without login, CAPTCHA solving, proxy rotation, or private App Store Connect credentials.

If the canary passes those gates, the next implementation step is a separate web-catalog ingestion mode with its own `source` value, not a silent replacement of RSS rows.
