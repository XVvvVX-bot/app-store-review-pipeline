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
- Playwright checks on June 18, 2026 found 6 visible review cards on the Peacock `see-all=reviews` page. Scrolling did not add more review cards or trigger a review-pagination network request.
- The HTML serialized server data and public web catalog app lookup expose a `next` review href. A direct public next-page request must include `platform=iphone`; without it the API returns a missing-parameter error.
- The public web catalog reviews endpoint accepts `sort=recent`; `sortBy=recent`, `orderBy=recent`, and similar guesses were rejected or ignored. `sort=recent` returned review dates in recent order during live checks.
- The web catalog `next` href omits `sort=recent`, so a client must preserve the sort parameter while following pagination.
- A bounded `probe-web --limit 20 --attempt-pagination --max-web-pages 2` check on June 18, 2026 found that the first web catalog page usually returned 6 reviews and the next page often returned another 6, but some scopes returned `429 API capacity exceeded`.
- A single-app depth check against Amazon Shopping on June 18, 2026 reached 15 successful web catalog review pages at a fast delay and 17 successful pages with a 1-second delay before `429 API capacity exceeded`. That is materially deeper than visible HTML but still below the RSS 10-page window in review volume.
- A later single-app backoff check reached 20 successful recent-sort web catalog review pages and 120 reviews for Amazon Shopping. Three pages initially returned `429`, then recovered after a 45-second retry delay. The 429 responses did not include a `Retry-After` header in captured response headers.
- A 20-app recent-sort backoff check reached 40 successful web catalog pages and 240 reviews. Three pages initially returned `429`, then recovered after a 30-second retry delay. A same-target RSS fetch during the same investigation returned 50 reviews across 21 RSS pages.
- The public web catalog path is now a serious candidate for a richer recent-review acquisition path, but it is still an undocumented web surface and not yet the default production source.
- The HTML shape is less stable than the RSS JSON structure.
- The aggregate rating count proves review presence, but does not provide a complete review-row feed.

Use HTML, Playwright, and `probe-web` checks as diagnostics for source health and coverage, not as the main ingestion path.

Run a bounded web probe with:

```bash
.venv/bin/python app_store_pipeline.py probe-web --limit 20 --web-sort recent --attempt-pagination --max-web-pages 2 --web-429-retries 1 --web-429-retry-seconds 30
```

## Web Catalog Canary Promotion Gate

The `App Store Web Catalog Canary` workflow runs the candidate web catalog path on GitHub-hosted Ubuntu and uploads `data/reports/apple_web/{run_id}/web_probe_report.json`.

Do not promote web catalog reviews into the production ingestion path until several scheduled canary runs show:

- `web_catalog_page_status_counts` is dominated by `200` responses after bounded retry.
- `recovered_429_page_count` stays small enough that the workflow runtime remains predictable.
- `web_catalog_page_reviews_total` is consistently at or above the RSS volume for the same target window.
- `web_sort` is `recent`, and page date ranges move backward in time as offsets increase.
- The source still works without login, CAPTCHA solving, proxy rotation, or private App Store Connect credentials.

If the canary passes those gates, the next implementation step is a separate web-catalog ingestion mode with its own `source` value, not a silent replacement of RSS rows.
