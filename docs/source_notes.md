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
- A bounded `probe-web --limit 20 --attempt-pagination --max-web-pages 2` check on June 18, 2026 found that the first web catalog page usually returned 6 reviews and the next page often returned another 6, but some scopes returned `429 API capacity exceeded`.
- A single-app depth check against Amazon Shopping on June 18, 2026 reached 15 successful web catalog review pages at a fast delay and 17 successful pages with a 1-second delay before `429 API capacity exceeded`. That is materially deeper than visible HTML but still below the RSS 10-page window in review volume.
- The public web catalog path may be useful for continued feasibility testing, but it is still an undocumented web surface and not yet a proven production replacement for RSS.
- The HTML shape is less stable than the RSS JSON structure.
- The aggregate rating count proves review presence, but does not provide a complete review-row feed.

Use HTML, Playwright, and `probe-web` checks as diagnostics for source health and coverage, not as the main ingestion path.

Run a bounded web probe with:

```bash
.venv/bin/python app_store_pipeline.py probe-web --limit 20 --attempt-pagination --max-web-pages 2
```
