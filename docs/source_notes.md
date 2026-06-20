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
- A self-hosted `App Store Web Catalog Ingestion` smoke run on June 18, 2026 verified the workflow wiring on GitHub: `target_offset=10`, `max_pages_per_app_country=1`, PayPal, 1 final `200` page, 20 reviews inserted into local Postgres, 0 fetch errors, and a healthy validation report.
- A full single-app self-hosted `App Store Web Catalog Ingestion` trial on June 18, 2026 used the conservative scheduled profile on Venmo: `target_offset=11`, 25 max pages, `limit=20`, 5-second page delay, five 429 retries, and 1.5x backoff. It completed in 2m25s with 25/25 final `200` pages, 500 unique reviews, 0 empty pages, 0 fetch errors, 0 missing text/rating, no retries needed, and 500 rows inserted under `source='apple_app_store_web_catalog_reviews'`.
- The web catalog daily report now surfaces stability metrics directly in `fetch_summary`: final status code counts, attempt counts, retried pages, successful-after-retry pages, final non-200 page count, missing text/rating counts, terminal reasons, and `all_pages_ok_after_retry`.
- Additional self-hosted controlled ingestion trials on June 18, 2026 added cross-app evidence. Walmart with 25 pages reached 25/25 final `200` pages, 500 unique reviews, 0 missing text/rating, 0 final non-200 pages, and 2 pages recovered after retry; its current RSS distinct count was 556, so 25 pages was slightly below parity. Target with a 30-page cap reached 30/30 final `200` pages, 600 unique reviews, 0 retries, 0 missing text/rating, and exceeded its current RSS distinct count of 509. This suggests the endpoint can match or beat RSS for another mainstream app, but parity page caps should be dynamic rather than fixed at 25 for every app.
- A manual Amazon Shopping depth probe on June 18, 2026 verified that web catalog can exceed the RSS-sized 500-review window. With `target_offset=0`, `max_pages_per_app_country=50`, `limit=20`, 5-second delay, `disable_overlap_stop=true`, and bounded 429 retry/backoff, the self-hosted workflow completed in 5m40s with 50/50 final `200` pages, 1,000 unique reviews, 0 empty pages, 0 fetch errors, 0 missing text/rating, and 1 page recovered after retry. The run stopped at the configured page cap, so the public web catalog depth limit was not reached. The same app's RSS rows in Postgres were 535.
- Follow-up Amazon Shopping depth runs on June 18, 2026 extended the lower-bound proof to 150 pages / 3,000 reviews. The page 1-100 run reached 100/100 final `200` pages and 2,000 unique reviews in 9m54s, with 0 empty pages, 0 fetch errors, 0 missing text/rating, and 1 page recovered after retry. After fixing the `start_page` handoff, the page 101-150 continuation reached 50/50 final `200` pages in 9m37s, inserted 1,000 additional rows, and had 5 pages recovered after retry. Postgres held 3,000 distinct web catalog reviews for Amazon Shopping versus 535 RSS reviews. Page 150 requested offset 2,980, returned 20 reviews, and still had a next link, so the observed web catalog limit is still higher than 3,000 rows for this app.
- A further Amazon Shopping continuation on June 18, 2026 tested pages 151-175. It reached 25/25 final `200` pages, inserted 500 more unique rows, had 1 recovered retry, 0 final non-200 pages, 0 empty pages, and 0 missing text/rating. Postgres then held 3,500 distinct web catalog reviews versus 535 RSS reviews. Page 175 requested offset 3,480, returned 20 reviews, and still advertised a next link, so the observed web catalog depth limit is still higher than 3,500 rows for this app.
- Later Amazon Shopping depth continuations on June 18, 2026 pushed the observed lower bound to 5,042 distinct Postgres reviews. GitHub Actions run `27790388846` first exposed a workflow-input bug: manually passing `stop_at_rss_parity=false` was coerced back to `true`, so the run stopped after 28 pages and inserted only 542 new rows. Commit `c0a312e` fixed the manual parity toggle. The follow-up run `27790604015` used `start_page=204`, `max_pages_per_app_country=253`, `disable_overlap_stop=true`, and `stop_at_rss_parity=false`; it fetched 50/50 final `200` pages, inserted 1,000 new rows, recovered 1 retried page, had 0 final non-200 pages, 0 empty pages, and 0 missing text/rating. Page 253 requested offset 5,040, returned 20 reviews, and still advertised a next link to offset 5,060, so the observed web catalog depth limit is still higher than 5,000 rows for this app.
- The `daily-web-catalog` ingestion path now supports `--stop-at-rss-parity`, matching the comparison probe behavior. When enabled, the fetcher uses current RSS counts in Postgres as per-scope targets and will not let overlap-stop end the run early while web catalog coverage is still below that target. The scheduled ingestion profile now uses a 35-page safety cap with RSS-parity stopping, so apps with RSS counts above 500 can fetch enough pages to prove parity while apps already at parity stop on overlap.
- A Walmart production-style parity smoke on June 18, 2026 verified the new ingestion behavior. Starting from web catalog 500 vs RSS 556, `daily-web-catalog --stop-at-rss-parity --max-pages-per-app-country 35` fetched 28/28 final `200` pages, 560 review rows, skipped 500 duplicates, inserted 60 new rows, and stopped with `target_review_count_reached`. Postgres then held 560 distinct Walmart web catalog reviews versus 556 RSS reviews.
- GitHub Actions run `27787209304` validated the new workflow wiring on the pushed commit `9aa46d2`. The self-hosted `App Store Web Catalog Ingestion` workflow ran Uber with target offset 3, 35 max pages, RSS-parity stopping enabled, and local Postgres. It completed in 3m52s, fetched 30/30 final `200` pages, recovered 1 retried page, inserted 600 web catalog rows from a 0-row web baseline, and stopped with `target_review_count_reached` against an RSS target of 587. Postgres then held 600 Uber web catalog reviews versus 587 RSS reviews.
- After the scheduled RSS daily run refreshed Uber to 633 RSS rows, GitHub Actions run `27787766595` verified catch-up behavior. The same web catalog ingestion profile fetched 32/32 final `200` pages, skipped 600 existing rows, inserted 40 new rows, and stopped with `target_review_count_reached`; Postgres then held 640 Uber web catalog reviews versus 633 RSS reviews. This confirms web catalog can re-catch parity after RSS advances.
- A Postgres coverage scorecard script now tracks cumulative RSS vs web catalog coverage. After the Uber catch-up run, current Postgres coverage was 200 RSS scopes, 7 web catalog scopes, 4 web-at-or-above-RSS scopes, 3 web-below-RSS scopes, and 193 RSS scopes without web catalog rows. This keeps the overall decision honest: the web catalog path has proven parity behavior on sampled apps, but it does not yet have broad enough evidence for full promotion.
- The scheduled web catalog ingestion workflow now uses that coverage scorecard when `target_offset=auto`. Instead of rotating blindly by clock slot, it selects the highest-priority active app-country scope below RSS parity, preferring scopes that can reach parity within the configured page cap. This should accumulate replacement evidence much faster and avoid spending scheduled runs on already-covered apps.
- GitHub Actions run `27788241871` validated coverage-aware `target_offset=auto` on pushed commit `6872b45`. The workflow selected TikTok at offset 15, fetched 35/35 final `200` web catalog pages, recovered 1 retried page on the third attempt, inserted 700 web catalog rows, and stopped with `target_review_count_reached` against a current RSS target of 682. The terminal page still had a next link, so this is an RSS-parity stop, not a catalog-exhaustion limit. Postgres then held 700 TikTok web catalog reviews versus 682 RSS reviews.
- GitHub Actions run `27788962289` continued the same coverage-aware path on pushed commit `7507ffa`. The workflow selected DoorDash at offset 5, fetched 33/33 final `200` web catalog pages, inserted 660 web catalog rows with no retries, and stopped with `target_review_count_reached` against a current RSS target of 646. The terminal page still had a next link, so this is another RSS-parity stop rather than a catalog-exhaustion limit.
- GitHub Actions run `27789339671` added a clean social-app sample. The workflow selected Instagram at offset 16, fetched 32/32 final `200` web catalog pages, inserted 640 web catalog rows with no retries, and stopped with `target_review_count_reached` against a current RSS target of 638. The terminal page still had a next link, so this is another RSS-parity stop rather than a catalog-exhaustion limit.
- GitHub Actions run `27789725316` continued the same coverage-aware path on pushed commit `5168056`. The workflow selected YouTube at offset 64, fetched 28/28 final `200` web catalog pages, inserted 560 web catalog rows with no retries, and stopped with `target_review_count_reached` against a current RSS target of 560. The terminal page still had a next link, so this is another RSS-parity stop rather than a catalog-exhaustion limit.
- GitHub Actions run `27790167231` continued the same coverage-aware path on pushed commit `5e45698`. The workflow selected Netflix at offset 14, fetched 28/28 final `200` web catalog pages, inserted 560 web catalog rows, recovered 1 retried page, and stopped with `target_review_count_reached` against a current RSS target of 556. The terminal page still had a next link, so this is another RSS-parity stop rather than a catalog-exhaustion limit.
- GitHub Actions runs `27791111575`, `27791293982`, and `27791476301` added three more clean coverage-aware samples: Uber Eats fetched 560 web catalog rows vs 547 RSS rows, Life360 fetched 560 vs 545, and SHEIN fetched 540 vs 536. All three runs had final status counts of only `200`, 0 final non-200 pages, 0 empty pages, 0 missing text/rating, and stopped with `target_review_count_reached`.
- After the Uber Eats, Life360, and SHEIN runs, current Postgres coverage was 200 RSS scopes, 15 web catalog scopes, 12 web-at-or-above-RSS scopes, 3 web-below-RSS scopes, and 185 RSS scopes without web catalog rows. The next coverage-aware target was ReelShort, which had 535 RSS rows and no web catalog rows yet.
- GitHub Actions runs `27791868280`, `27792054735`, and `27792252940` verified below-RSS cleanup after the coverage-aware selector was changed to prioritize existing partial scopes before entirely missing scopes. Cash App moved to 560 web catalog rows vs 545 RSS rows, PayPal moved to 520 vs 510, and Venmo moved to 560 vs 558. All three runs stopped with `target_review_count_reached`, had 0 final non-200 pages, 0 missing text/rating, and final status counts of only `200`; PayPal recovered 1 retried page, while Cash App and Venmo needed no retries.
- GitHub Actions run `27792406230` selected ReelShort at offset 133 and fetched 27/27 final `200` pages, 540 unique reviews, 0 retries, 0 empty pages, 0 missing text/rating, and stopped with `target_review_count_reached` against 535 RSS rows. Postgres then held 540 ReelShort web catalog reviews vs 535 RSS reviews.
- GitHub Actions runs `27792630656`, `27792758260`, `27792880685`, and `27793044026` pushed the scorecard across the 20-scope controlled promotion gate. The runs selected Lyft, Freecash, Pokemon GO, and Rips by Triumph; each fetched 27/27 final `200` pages, 540 unique reviews, 0 final non-200 pages, 0 empty pages, 0 missing text/rating, and stopped with `target_review_count_reached`. Pokemon GO recovered 1 retried page; the other three had no retries.
- The current source coverage scorecard is `ready_for_controlled_promotion`: 20 web catalog scopes have rows, all 20 are at or above RSS parity, 0 tested web scopes are below RSS, 180 RSS scopes still have no web catalog rows, and cumulative web catalog rows total 15,962 against 66,953 RSS rows. This supports controlled web catalog ingestion trials, not a claim of complete 200-scope replacement.
- `scripts/summarize_web_catalog_ingestion.py` now adds a Postgres-backed web catalog depth section. The current scorecard shows 20 app-country scopes above 500 unique web catalog reviews, a maximum of 5,042 unique reviews for Amazon Shopping through page 253, and tested scopes stopping while the terminal page still had a next link. Treat these as observed lower bounds, not full historical limits.
- `scripts/summarize_web_catalog_ingestion.py` now treats `target_review_count_reached` as a successful completion mode for routine parity-stopping runs. This matters because a clean scheduled-style run should stop after reaching the current RSS count instead of always fetching to the configured page ceiling. A downloaded-artifact summary of 21 recent full single-app workflow runs produced `ready_for_controlled_promotion`: 21 clean runs, 13,460 reviews fetched, 673 final `200` pages, 8 recovered retry pages, 0 final non-200 pages, 0 fetch errors, and 0 missing text/rating.
- A local Playwright rendered-HTML probe on TikTok on June 18, 2026 saw 6 rendered review title IDs before scrolling and 6 after scrolling. It found 0 new review IDs after scroll, 0 review API requests, and 0 review-related requests after initial load. This matches the Amazon Shopping browser probes and reinforces that rendered HTML is diagnostic only; the structured web catalog JSON path is the deeper public path.
- `daily-web-catalog` and the workflow now support `start_page` for manual depth probes. This allows follow-up tests such as page 51-100 without re-fetching page 1-50. Keep scheduled runs at `start_page=1`. A June 18 regression check fixed a missing `start_page` handoff in the `daily-web-catalog` CLI path and added test coverage so workflow depth probes honor the dispatch input.
- `scripts/summarize_web_catalog_ingestion.py` summarizes web catalog ingestion artifacts, optionally joins Postgres source/app row counts and web catalog depth evidence, and gates promotion evidence by repeated clean full single-app runs.
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
  --max-pages-per-app-country 35 \
  --review-limit 20 \
  --request-delay-seconds 5 \
  --web-429-retries 5 \
  --web-429-retry-seconds 60 \
  --web-429-backoff-multiplier 1.5 \
  --stop-at-rss-parity
```

At this point in the investigation, the primary `App Store Review Pipeline` workflow ran a conservative web catalog profile on the self-hosted macOS ARM64 runner every 6 hours with `limit=1`, `target_offset=auto`, and RSS-parity stopping. This profile proved useful for early migration testing, but it was later replaced by a cooldown-safe web-only schedule after sustained backfill tests triggered Apple HTTP 429 throttling.

The manual `App Store Web Catalog Backfill` workflow is for chunked backfill probes by default. Its default `max_pages_per_app_country=5` advances each selected app-country scope in small bounded batches. Set `max_pages_per_app_country=0` only for explicit no-cap exhaustion probes after repeated clean chunked batches. A `no_next_href` stop is the strongest public-path evidence that the observed catalog pagination is exhausted for that app-country scope; any other stop reason is a lower-bound result.

Use the web catalog ingestion `daily_report.json` stability fields to judge each scheduled trial: `status_code_counts`, `attempt_counts`, `retried_pages`, `final_non_200_pages`, `missing_text`, `missing_rating`, and `all_pages_ok_after_retry`.

Summarize ingestion artifacts with:

```bash
.venv/bin/python scripts/summarize_web_catalog_ingestion.py \
  --root /path/to/downloaded/artifacts \
  --database-url postgresql:///app_store_reviews \
  --full-single-app-only
```

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

Do not promote web catalog reviews into the production ingestion path until several controlled canary or scheduled ingestion runs show:

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

## Cooldown And RSS Pause Decision - June 20, 2026

After testing 4, 6, 8, and 10 local self-hosted runner configurations, local runner capacity was not the limiting factor. The 8-runner profile was mechanically stable on the Mac, but a later 200-target backfill run returned zero new reviews because Apple web catalog requests had shifted into HTTP 429 responses. A follow-up 4-runner canary with one page per app also returned 4/4 HTTP 429 pages, confirming that the endpoint was still throttling the current local access pattern.

Current operating decision:

- Pause scheduled RSS usage. The legacy RSS ingestion workflow remains manual-only, and the RSS-vs-web canary is manual-only while RSS value is considered too low for routine automation.
- Keep web catalog as the primary candidate because it has already proven materially deeper coverage than visible HTML and can exceed the RSS 500-row/app window when Apple allows sustained pagination.
- Use cooldown-aware web-only automation while finding a safe rate. The scheduled primary workflow now runs every 30 minutes with a base 5-page cap, an automatic clean-history pressure ramp up to 25 pages, 10-second request delay, 2 HTTP 429 retries, and RSS-parity stopping disabled.
- Use the Postgres-backed HTTP 429 cooldown gate before any scheduled or backfill ingestion. If the latest stored web catalog HTTP 429 is less than 720 minutes old, the workflow exits before making new Apple requests.
- Keep the HTTP 429 rate circuit breakers as a second layer. Recent-lookback protection catches high-rate windows, and current-run protection marks 429-heavy runs as failures instead of green no-data runs.
- Keep scheduled pressure increases page-based rather than concurrency-based. The stateful ramp is stored in Postgres table `app_store_pressure_state` and moves through `5 -> 7 -> 10 -> 12 -> 15 -> 20 -> 25` pages only after clean scheduled runs while the recent 720-minute window has clean final statuses and no retried pages. Any retry, HTTP 429, final non-200 page, or fetch error resets the selected cap to 5 pages.
- Backfill defaults to bounded chunks: `max_parallel=4`, `max_pages_per_app_country=5`, `request_delay_seconds=10`, and `web_429_retries=1`. Full no-cap exhaustion should not be attempted until repeated chunked batches finish with clean 200-page rates.

Post-cooldown evidence from June 20, 2026:

- The scheduled `18:30` UTC workflow did not appear by `19:00` UTC, so a manual run was used to preserve the post-cooldown source test. The workflow was active and the remote cron was `30 6,18 * * *`; this is treated as a GitHub schedule delay/miss rather than an Apple-source result.
- Manual daily liveness run `27880858230` used the scheduled defaults and completed successfully: 5/5 web catalog pages returned HTTP 200, 0 pages returned HTTP 429, and 100 reviews were loaded.
- Small 4-runner canary `27880917037` fetched 8 apps x 1 page with `max_parallel=4`: 8/8 pages returned HTTP 200, 0 pages returned HTTP 429.
- Modest 4-runner depth canary `27880973587` fetched 4 apps x 5 pages with `max_parallel=4` and `request_delay_seconds=10`: 20/20 pages returned HTTP 200, 0 pages returned HTTP 429.
- Larger chunk canary `27881027486` fetched 8 apps x 5 pages with the same rate: 40/40 pages returned HTTP 200, 0 pages returned HTTP 429.
- Combined post-cooldown evidence from `19:01:47` to `19:10:45` UTC was 73/73 HTTP 200 pages, 0 HTTP 429 pages, and 1,460 fetched review rows. This supports `max_parallel=4`, 5-page chunks, and 10-second per-job page delay as the current provisional safe mode, not no-cap full backfill.

Recommended post-cooldown sequence:

1. Wait at least 12 hours after a 429-heavy run before testing again.
2. Run a one-runner liveness check: `limit=4`, `max_parallel=1`, `max_pages_per_app_country=1`.
3. If clean, run a small 4-runner canary: `limit=8`, `max_parallel=4`, `max_pages_per_app_country=1`.
4. If clean, test modest depth: `limit=8`, `max_parallel=4`, `max_pages_per_app_country=5`, `request_delay_seconds=10`.
5. Only after repeated clean runs should continuation backfill resume with `max_parallel=4` and `start_page=auto`.
