# Apple App Store Review Data Quality Report

Generated at: `2026-06-22T21:33:09+00:00`
Database: `postgresql:///app_store_reviews`
Primary source: `apple_app_store_web_catalog_reviews`

## Executive Summary

The current primary-source dataset contains **1,259,204** deduplicated web-catalog reviews across **200** apps, **26** categories, and **1** country storefronts.
Top-app concentration is material but not extreme: top 1 app share is **5.7%**, top 5 share is **22.9%**, and top 10 share is **36.8%**.
Operationally, the stored page history includes **221** HTTP 429 pages, **2,171** retried pages, and **344** final non-200 pages.

## Inventory

| source | review_count | app_count | country_count | min_updated_epoch_seconds | max_updated_epoch_seconds |
| --- | --- | --- | --- | --- | --- |
| apple_app_store_web_catalog_reviews | 1,259,204 | 200 | 1 | 1,289,776,286 | 1,781,920,486 |
| apple_itunes_customerreviews_rss | 66,953 | 200 | 1 | 1,557,790,841 | 1,781,723,829 |

## Volume Distribution

### Top Apps By Review Count

| app_name | category | review_count | reviews_last_30_days | min_updated_epoch_seconds | max_updated_epoch_seconds |
| --- | --- | --- | --- | --- | --- |
| Amazon Shopping | shopping | 72,303 | 1,206 | 1,612,624,173 | 1,781,877,450 |
| Walmart | shopping | 68,776 | 2,850 | 1,671,666,073 | 1,781,878,575 |
| Target | shopping | 59,905 | 358 | 1,293,713,404 | 1,781,870,926 |
| Uber | travel | 50,433 | 2,572 | 1,737,125,589 | 1,781,878,676 |
| Lyft | travel | 37,592 | 1,207 | 1,688,881,175 | 1,781,878,776 |
| Expedia | travel | 35,301 | 446 | 1,462,368,917 | 1,781,876,451 |
| Venmo | finance | 35,290 | 996 | 1,675,024,056 | 1,781,878,132 |
| Peacock TV: Stream TV & Movies | entertainment | 34,471 | 560 | 1,609,512,446 | 1,781,749,709 |
| Netflix | entertainment | 34,466 | 1,704 | 1,730,567,010 | 1,781,878,238 |
| Uber Eats | food_delivery | 34,348 | 1,078 | 1,711,386,035 | 1,781,878,560 |
| MyFitnessPal | health | 33,960 | 383 | 1,664,671,650 | 1,781,878,771 |
| DoorDash | food_delivery | 33,911 | 3,504 | 1,740,457,863 | 1,781,878,340 |
| PayPal | finance | 33,744 | 194 | 1,623,611,285 | 1,781,872,209 |
| Cash App | finance | 33,685 | 892 | 1,686,940,861 | 1,781,877,953 |
| Booking.com | travel | 33,634 | 399 | 1,570,304,118 | 1,781,874,015 |
| Duolingo | education | 33,317 | 6,501 | 1,768,787,399 | 1,781,878,748 |
| ChatGPT | ai_tools | 33,204 | 7,440 | 1,770,609,195 | 1,781,878,746 |
| TikTok | social | 31,513 | 2,647 | 1,753,325,426 | 1,781,876,909 |
| Spotify | entertainment | 26,893 | 11,638 | 1,774,462,334 | 1,781,878,675 |
| Airbnb | travel | 24,318 | 106 | 1,289,776,286 | 1,781,858,414 |
| Instagram | social | 21,669 | 3,303 | 1,765,630,634 | 1,781,877,454 |
| Google Gemini | productivity | 14,947 | 1,425 | 1,735,137,529 | 1,781,750,481 |
| Google | utilities | 14,614 | 907 | 1,738,170,885 | 1,781,749,699 |
| Threads | social_networking | 14,532 | 559 | 1,723,196,806 | 1,781,746,512 |
| CapCut: Photo & Video Editor | photo_and_video | 14,185 | 1,055 | 1,751,291,858 | 1,781,750,127 |

### Category Coverage

| category | app_count | review_count | avg_rating | avg_content_chars | reviews_last_30_days |
| --- | --- | --- | --- | --- | --- |
| shopping | 15 | 242,376 | 3.502 | 171.4 | 15,686 |
| travel | 8 | 185,438 | 3.584 | 181.5 | 5,444 |
| entertainment | 27 | 153,757 | 3.13 | 148.8 | 22,933 |
| finance | 9 | 115,741 | 2.673 | 186.2 | 5,159 |
| games | 52 | 88,876 | 3.282 | 172.5 | 15,897 |
| food_delivery | 2 | 68,259 | 2.486 | 207 | 4,582 |
| social_networking | 11 | 55,505 | 2.997 | 132.8 | 9,372 |
| social | 2 | 53,182 | 2.372 | 172.7 | 5,950 |
| productivity | 10 | 47,522 | 3.348 | 158.5 | 4,861 |
| education | 5 | 40,061 | 4.071 | 172.1 | 7,354 |
| health | 1 | 33,960 | 2.866 | 217.7 | 383 |
| ai_tools | 1 | 33,204 | 3.731 | 127.2 | 7,440 |
| photo_and_video | 7 | 30,444 | 2.937 | 172.2 | 6,889 |
| utilities | 7 | 28,110 | 3.345 | 115.1 | 2,094 |
| lifestyle | 8 | 20,232 | 2.333 | 227.2 | 3,128 |
| business | 7 | 13,520 | 2.636 | 175.4 | 977 |
| health_and_fitness | 5 | 9,375 | 3.823 | 190.7 | 1,694 |
| sports | 7 | 8,348 | 2.65 | 149.6 | 1,571 |
| music | 4 | 8,200 | 3.781 | 137.4 | 1,994 |
| news | 3 | 6,595 | 2.852 | 168.8 | 2,773 |
| navigation | 2 | 5,800 | 3.321 | 190.6 | 638 |
| food_and_drink | 2 | 3,300 | 1.804 | 205.6 | 1,066 |
| graphics_and_design | 1 | 2,119 | 3.353 | 224.2 | 15 |
| books | 1 | 2,020 | 3.927 | 215.9 | 533 |
| weather | 2 | 1,860 | 3.789 | 268.5 | 10 |
| medical | 1 | 1,400 | 3.598 | 185.2 | 876 |

## Rating Distribution

| rating | review_count |
| --- | --- |
| 1 | 444,970 |
| 2 | 83,260 |
| 3 | 81,101 |
| 4 | 76,475 |
| 5 | 573,398 |

### Rating By Category

| category | review_count | avg_rating | rating_1 | rating_2 | rating_3 | rating_4 | rating_5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| shopping | 242,376 | 3.502 | 66,379 | 15,978 | 16,645 | 16,394 | 126,980 |
| travel | 185,438 | 3.584 | 54,023 | 7,998 | 7,264 | 7,920 | 108,233 |
| entertainment | 153,757 | 3.13 | 54,756 | 11,798 | 11,401 | 10,370 | 65,432 |
| finance | 115,741 | 2.673 | 58,143 | 7,029 | 5,551 | 4,576 | 40,442 |
| games | 88,876 | 3.282 | 27,315 | 7,043 | 7,441 | 7,406 | 39,671 |
| food_delivery | 68,259 | 2.486 | 37,056 | 4,456 | 3,831 | 2,345 | 20,571 |
| social_networking | 55,505 | 2.997 | 22,565 | 3,429 | 3,603 | 3,403 | 22,505 |
| social | 53,182 | 2.372 | 28,515 | 4,556 | 4,360 | 3,330 | 12,421 |
| productivity | 47,522 | 3.348 | 15,544 | 2,686 | 2,772 | 2,721 | 23,799 |
| education | 40,061 | 4.071 | 5,217 | 1,925 | 2,892 | 4,786 | 25,241 |
| health | 33,960 | 2.866 | 12,500 | 4,293 | 3,580 | 2,416 | 11,171 |
| ai_tools | 33,204 | 3.731 | 8,952 | 968 | 1,078 | 1,260 | 20,946 |
| photo_and_video | 30,444 | 2.937 | 11,681 | 2,548 | 2,895 | 2,659 | 10,661 |
| utilities | 28,110 | 3.345 | 9,299 | 1,481 | 1,636 | 1,618 | 14,076 |
| lifestyle | 20,232 | 2.333 | 10,651 | 2,318 | 1,690 | 1,026 | 4,547 |
| business | 13,520 | 2.636 | 6,270 | 1,312 | 1,057 | 830 | 4,051 |
| health_and_fitness | 9,375 | 3.823 | 1,882 | 571 | 574 | 643 | 5,705 |
| sports | 8,348 | 2.65 | 4,185 | 544 | 420 | 406 | 2,793 |
| music | 8,200 | 3.781 | 1,545 | 559 | 678 | 784 | 4,634 |
| news | 6,595 | 2.852 | 2,969 | 415 | 380 | 282 | 2,549 |
| navigation | 5,800 | 3.321 | 1,858 | 403 | 384 | 329 | 2,826 |
| food_and_drink | 3,300 | 1.804 | 2,185 | 396 | 249 | 121 | 349 |
| graphics_and_design | 2,119 | 3.353 | 456 | 198 | 311 | 449 | 705 |
| books | 2,020 | 3.927 | 370 | 109 | 116 | 129 | 1,296 |
| weather | 1,860 | 3.789 | 351 | 125 | 149 | 175 | 1,060 |
| medical | 1,400 | 3.598 | 303 | 122 | 144 | 97 | 734 |

## Text Quality

### Review Length

| review_count | avg_chars | p10_chars | p25_chars | p50_chars | p75_chars | p90_chars | p95_chars | max_chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,259,204 | 171.4 | 16 | 43 | 105 | 220 | 394 | 551 | 6,000 |

### Low-Signal And Formatting Patterns

| review_count | blank_content | content_1_to_20_chars | content_21_to_50_chars | blank_title | url_like_content | html_like_content | multiline_content | non_ascii_content |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,259,204 | 0 | 163,161 | 199,719 | 0 | 49 | 42 | 0 | 568,473 |

### Duplicate Patterns

| review_count | distinct_review_keys | normalized_duplicate_group_count | normalized_duplicate_row_count | largest_normalized_duplicate_group |
| --- | --- | --- | --- | --- |
| 1,259,204 | 1,259,204 | 13,300 | 107,374 | 3,197 |

Top normalized duplicate examples:

| row_count | app_count | sample |
| --- | --- | --- |
| 3,197 | 170 | good |
| 2,163 | 160 | love it |
| 2,110 | 153 | great |
| 1,359 | 139 | awesome |
| 1,274 | 108 | excelente |
| 1,253 | 123 | excellent |
| 1,215 | 121 | great app |
| 1,027 | 137 | amazing |
| 987 | 138 | i love it |
| 853 | 115 | very good |
| 774 | 122 | nice |
| 719 | 115 | the best |
| 606 | 86 | thank you |
| 573 | 54 | great service |
| 534 | 97 | love this app |

## Freshness And Time Coverage

### Recent Monthly Density

| month | review_count | app_count |
| --- | --- | --- |
| 2026-06 | 89,410 | 189 |
| 2026-05 | 124,346 | 194 |
| 2026-04 | 87,834 | 184 |
| 2026-03 | 83,643 | 175 |
| 2026-02 | 63,786 | 155 |
| 2026-01 | 58,508 | 140 |
| 2025-12 | 44,467 | 128 |
| 2025-11 | 33,879 | 119 |
| 2025-10 | 35,632 | 116 |
| 2025-09 | 29,904 | 106 |
| 2025-08 | 28,022 | 100 |
| 2025-07 | 26,583 | 93 |
| 2025-06 | 22,976 | 86 |
| 2025-05 | 20,276 | 79 |
| 2025-04 | 19,625 | 76 |
| 2025-03 | 21,437 | 73 |
| 2025-02 | 20,339 | 70 |
| 2025-01 | 19,878 | 68 |
| 2024-12 | 14,872 | 66 |
| 2024-11 | 15,316 | 61 |
| 2024-10 | 12,471 | 61 |
| 2024-09 | 12,228 | 55 |
| 2024-08 | 12,651 | 52 |
| 2024-07 | 12,214 | 51 |

### Stalest Apps By Newest Review

| app_name | category | review_count | newest_review_age_days | reviews_last_7_days | reviews_last_30_days |
| --- | --- | --- | --- | --- | --- |
| Papa's Freezeria To Go! | games | 1,384 | 219.7 | 0 | 0 |
| SkyView® | education | 2,260 | 119.9 | 0 | 0 |
| RadarScope | weather | 1,660 | 67.9 | 0 | 0 |
| AnkiMobile Flashcards | education | 1,184 | 62.2 | 0 | 0 |
| Raya | lifestyle | 794 | 39.8 | 0 | 0 |
| HotSchedules | business | 1,400 | 38.7 | 0 | 0 |
| Backyard Baseball '01 | games | 223 | 33.1 | 0 | 0 |
| onX Hunt: GPS Hunting Maps | navigation | 1,900 | 33 | 0 | 0 |
| Shadowrocket | utilities | 1,420 | 29.8 | 0 | 1 |
| STARZ | entertainment | 1,900 | 25.1 | 0 | 5 |
| Heads Up! | games | 1,180 | 23 | 0 | 8 |
| Candy Crush Soda Saga | games | 1,900 | 19.8 | 0 | 12 |
| Balatro | games | 1,900 | 17.6 | 0 | 26 |
| Clapper: Video, Live, Chat | social_networking | 1,879 | 16.3 | 0 | 16 |
| GroupMe | social_networking | 1,400 | 15.9 | 0 | 32 |
| Stardew Valley | games | 1,780 | 15.2 | 0 | 14 |
| MONOPOLY: The Board Game | games | 2,020 | 15.1 | 0 | 29 |
| Cleaner Guru: Clean Up Storage | utilities | 2,000 | 15.1 | 0 | 36 |
| Plague Inc. | games | 1,660 | 14.2 | 0 | 35 |
| Google Authenticator | utilities | 1,780 | 13.4 | 0 | 35 |
| Last War:Survival | games | 1,780 | 12.3 | 0 | 53 |
| Red's First Flight | games | 1,780 | 11.9 | 0 | 52 |
| Evony | games | 1,780 | 11.2 | 0 | 19 |
| ViX: TV, Sports and News | entertainment | 2,140 | 11.1 | 0 | 37 |
| BIGO LIVE-Live Stream, Go Live | social_networking | 1,899 | 11 | 0 | 49 |

## Pipeline Behavior

### Run Summary

| run_count | first_loaded_at | last_loaded_at | page_count | raw_review_rows | reviews_inserted | reviews_updated | fetch_errors | capped_scopes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 823 | 2026-06-18T19:00:24.473852+00:00 | 2026-06-22T20:44:24.366383+00:00 | 64,177 | 1,275,137 | 1,259,204 | 1 | 392 | 38 |

### Page Status Codes

| status_code | page_count | review_rows |
| --- | --- | --- |
| 200 | 63,785 | 1,275,137 |
| 429 | 221 | 0 |
| 404 | 123 | 0 |
| null | 48 | 0 |

### Terminal Reasons

| terminal_reason | page_count | review_rows |
| --- | --- | --- |
| none | 63,779 | 1,273,677 |
| fetch_error | 282 | 0 |
| page_cap | 38 | 728 |
| no_next_href | 31 | 296 |
| sparse_fetch_error_threshold | 24 | 0 |
| target_review_count_reached | 21 | 420 |
| caught_up_to_existing_reviews | 2 | 16 |

### Retry Attempts

| attempt_count | page_count | review_rows |
| --- | --- | --- |
| 1 | 62,006 | 1,236,518 |
| 2 | 1,543 | 30,519 |
| 3 | 319 | 6,160 |
| 4 | 84 | 1,640 |
| 5 | 12 | 240 |
| 6 | 213 | 60 |

### Empty And Error Page Summary

| empty_pages | empty_pages_with_next_link | empty_pages_without_next_link | http_429_pages | final_non_200_pages | retried_pages | error_pages |
| --- | --- | --- | --- | --- | --- | --- |
| 392 | 0 | 392 | 221 | 344 | 2,171 | 392 |

### Apps With The Most Fetched Pages

| app_name | category | run_count | page_count | page_review_rows | http_429_pages | final_non_200_pages | retried_pages | avg_run_page_window_minutes | max_run_page_window_minutes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Amazon Shopping | shopping | 32 | 3,686 | 73,580 | 3 | 5 | 44 | 18.65 | 59.97 |
| Walmart | shopping | 27 | 3,495 | 69,800 | 2 | 3 | 37 | 20.93 | 59.99 |
| Target | shopping | 24 | 3,011 | 60,120 | 2 | 3 | 31 | 20.64 | 59.97 |
| Uber | travel | 22 | 2,595 | 51,800 | 3 | 4 | 37 | 19.11 | 59.85 |
| Lyft | travel | 17 | 1,901 | 37,960 | 2 | 2 | 22 | 16.52 | 30 |
| Venmo | finance | 16 | 1,806 | 36,080 | 2 | 2 | 20 | 16.4 | 29.96 |
| Expedia | travel | 14 | 1,780 | 35,560 | 1 | 2 | 18 | 18.46 | 29.98 |
| Netflix | entertainment | 15 | 1,744 | 34,860 | 1 | 1 | 23 | 17.44 | 29.98 |
| Peacock TV: Stream TV & Movies | entertainment | 13 | 1,735 | 34,680 | 1 | 1 | 26 | 19.89 | 29.96 |
| Uber Eats | food_delivery | 15 | 1,734 | 34,640 | 1 | 2 | 25 | 17.39 | 29.98 |
| DoorDash | food_delivery | 15 | 1,733 | 34,620 | 1 | 1 | 26 | 17.44 | 29.94 |
| ChatGPT | ai_tools | 14 | 1,720 | 34,380 | 1 | 1 | 28 | 18.52 | 29.98 |
| Duolingo | education | 14 | 1,715 | 34,260 | 1 | 1 | 24 | 18.46 | 29.98 |
| MyFitnessPal | health | 14 | 1,712 | 34,200 | 1 | 1 | 24 | 18.41 | 29.97 |
| PayPal | finance | 16 | 1,702 | 34,000 | 2 | 2 | 30 | 16.21 | 29.92 |
| Cash App | finance | 16 | 1,701 | 33,980 | 2 | 2 | 20 | 15.27 | 29.98 |
| Booking.com | travel | 14 | 1,696 | 33,880 | 1 | 2 | 26 | 18.52 | 30 |
| TikTok | social | 15 | 1,607 | 32,040 | 1 | 4 | 26 | 17.38 | 29.99 |
| Spotify | entertainment | 14 | 1,423 | 28,400 | 2 | 3 | 27 | 16.6 | 29.92 |
| Airbnb | travel | 14 | 1,235 | 24,475 | 1 | 10 | 14 | 11.97 | 29.94 |
| Instagram | social | 15 | 1,150 | 22,240 | 1 | 37 | 30 | 14.28 | 19.94 |
| Google Gemini | productivity | 4 | 752 | 15,020 | 1 | 1 | 10 | 19.95 | 30 |
| Google | utilities | 4 | 734 | 14,660 | 1 | 1 | 10 | 19.94 | 29.95 |
| Threads | social_networking | 4 | 729 | 14,560 | 1 | 1 | 12 | 19.97 | 29.97 |
| CapCut: Photo & Video Editor | photo_and_video | 4 | 713 | 14,240 | 1 | 1 | 16 | 19.97 | 29.99 |

## Known Limitations

- The pipeline reads Apple-hosted public web catalog review payloads exposed to the App Store web experience. This is not the App Store Connect Customer Reviews API, does not use owner credentials, and does not carry an Apple SLA for third-party bulk ingestion.
- Completeness is empirical per app, country, and source scope. A scope is only treated as historically exhausted when pagination reaches `no_next_href`; page cap, time budget, overlap, final non-200, and fetch-error stops mean the current row count is a lower bound.
- Daily/incremental interpretation depends on stable review keys and Postgres upserts. Repeated runs can add new rows or update existing rows, but source-side ordering, removed reviews, and Apple response changes should be monitored through page and terminal-reason metrics.
- Public web catalog payloads do not currently provide every owner-API field. Version, vote sum, vote count, and similar App Store Connect-style review metadata should be treated as unavailable unless Apple exposes them in the public response.
- Normalized duplicate detection uses lowercased whitespace-normalized content hashes; it is useful for triage, not semantic near-duplicate modeling.
- Runtime by app is a page-window proxy based on stored page timestamps, not full GitHub job wall-clock time.
