# Apple App Store Review Data Quality Report

Generated at: `2026-07-16T13:15:18+00:00`
Database: `postgresql:///app_store_reviews`
Primary source: `apple_app_store_web_catalog_reviews`

## Executive Summary

The current primary-source dataset contains **2,295,799** deduplicated web-catalog reviews across **200** apps, **26** categories, and **1** country storefronts.
Top-app concentration is material but not extreme: top 1 app share is **4.5%**, top 5 share is **18.3%**, and top 10 share is **31.9%**.
Operationally, the stored page history includes **1,596** HTTP 429 attempts (**410** final 429 pages), **3,778** retried pages, and **1,180** final non-200 pages.

## Inventory

| source | review_count | app_count | country_count | min_updated_epoch_seconds | max_updated_epoch_seconds |
| --- | --- | --- | --- | --- | --- |
| apple_app_store_web_catalog_reviews | 2,295,799 | 200 | 1 | 1,289,674,307 | 1,784,082,973 |
| apple_itunes_customerreviews_rss | 66,953 | 200 | 1 | 1,557,790,841 | 1,781,723,829 |

## Volume Distribution

### Top Apps By Review Count

| app_name | category | review_count | reviews_last_30_days | min_updated_epoch_seconds | max_updated_epoch_seconds |
| --- | --- | --- | --- | --- | --- |
| Amazon Shopping | shopping | 102,264 | 1,067 | 1,503,609,363 | 1,784,081,047 |
| Walmart | shopping | 101,299 | 3,320 | 1,628,853,578 | 1,784,082,207 |
| Uber | travel | 81,082 | 3,090 | 1,716,049,588 | 1,784,082,173 |
| Lyft | travel | 69,648 | 1,382 | 1,597,193,969 | 1,784,082,819 |
| ChatGPT | ai_tools | 65,386 | 8,032 | 1,756,664,084 | 1,784,082,797 |
| Duolingo | education | 64,236 | 6,531 | 1,758,313,561 | 1,784,082,837 |
| Venmo | finance | 62,932 | 1,094 | 1,627,074,984 | 1,784,082,624 |
| DoorDash | food_delivery | 61,759 | 3,988 | 1,704,987,598 | 1,784,082,940 |
| Cash App | finance | 61,431 | 848 | 1,616,523,821 | 1,784,082,313 |
| Booking.com | travel | 61,268 | 502 | 1,503,514,980 | 1,784,078,284 |
| Uber Eats | food_delivery | 61,130 | 1,290 | 1,653,857,891 | 1,784,081,564 |
| Netflix | entertainment | 60,946 | 1,749 | 1,613,133,166 | 1,784,082,778 |
| Spotify | entertainment | 60,682 | 10,607 | 1,763,964,288 | 1,784,082,952 |
| Target | shopping | 60,655 | 400 | 1,289,674,307 | 1,784,080,326 |
| TikTok | social | 59,839 | 3,479 | 1,737,242,250 | 1,784,082,838 |
| PayPal | finance | 58,690 | 179 | 1,577,752,643 | 1,784,066,099 |
| MyFitnessPal | health | 56,798 | 297 | 1,499,352,745 | 1,784,081,495 |
| Expedia | travel | 39,493 | 529 | 1,302,169,621 | 1,784,080,831 |
| Peacock TV: Stream TV & Movies | entertainment | 38,643 | 881 | 1,594,777,995 | 1,784,082,419 |
| Instagram | social | 27,588 | 3,957 | 1,763,669,855 | 1,784,082,810 |
| WhatsApp Messenger | social_networking | 27,404 | 1,218 | 1,720,564,983 | 1,784,080,737 |
| CapCut: Photo & Video Editor | photo_and_video | 27,129 | 1,303 | 1,740,628,529 | 1,784,080,854 |
| Google | utilities | 25,453 | 998 | 1,681,037,995 | 1,784,080,928 |
| Airbnb | travel | 24,422 | 120 | 1,289,776,286 | 1,784,054,634 |
| Threads | social_networking | 22,793 | 716 | 1,688,598,366 | 1,784,080,637 |

### Category Coverage

| category | app_count | review_count | avg_rating | avg_content_chars | reviews_last_30_days |
| --- | --- | --- | --- | --- | --- |
| shopping | 15 | 351,689 | 3.433 | 173.3 | 20,871 |
| travel | 8 | 313,872 | 3.688 | 167.8 | 6,406 |
| entertainment | 27 | 282,757 | 3.154 | 155.9 | 25,629 |
| finance | 9 | 222,204 | 2.915 | 176.2 | 6,659 |
| games | 52 | 179,056 | 3.32 | 165.8 | 29,555 |
| food_delivery | 2 | 122,889 | 2.555 | 212.6 | 5,278 |
| social_networking | 11 | 105,758 | 3.175 | 131.8 | 9,358 |
| productivity | 10 | 88,139 | 3.394 | 154.6 | 4,970 |
| social | 2 | 87,427 | 2.637 | 170.7 | 7,436 |
| education | 5 | 80,479 | 4.024 | 171.2 | 7,591 |
| photo_and_video | 7 | 73,379 | 2.807 | 166.2 | 10,186 |
| ai_tools | 1 | 65,386 | 3.896 | 135.9 | 8,032 |
| business | 7 | 60,578 | 2.904 | 152.5 | 1,297 |
| health | 1 | 56,798 | 2.908 | 226.3 | 297 |
| utilities | 7 | 40,797 | 3.351 | 120.4 | 2,346 |
| news | 3 | 31,598 | 2.488 | 192.4 | 2,882 |
| lifestyle | 8 | 23,362 | 2.349 | 226.1 | 3,384 |
| food_and_drink | 2 | 22,123 | 2.786 | 163.4 | 1,063 |
| health_and_fitness | 5 | 21,917 | 3.996 | 216.1 | 1,950 |
| sports | 7 | 20,629 | 2.601 | 150.9 | 1,861 |
| navigation | 2 | 20,000 | 2.461 | 162.5 | 757 |
| music | 4 | 12,697 | 3.779 | 145.8 | 1,980 |
| medical | 1 | 5,467 | 3.634 | 177.8 | 929 |
| books | 1 | 2,453 | 3.946 | 219.5 | 569 |
| weather | 2 | 2,209 | 3.89 | 263.5 | 26 |
| graphics_and_design | 1 | 2,136 | 3.358 | 223.6 | 18 |

## Rating Distribution

| rating | review_count |
| --- | --- |
| 1 | 788,907 |
| 2 | 153,800 |
| 3 | 148,675 |
| 4 | 144,859 |
| 5 | 1,059,558 |

### Rating By Category

| category | review_count | avg_rating | rating_1 | rating_2 | rating_3 | rating_4 | rating_5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| shopping | 351,689 | 3.433 | 101,401 | 24,489 | 24,101 | 23,656 | 178,042 |
| travel | 313,872 | 3.688 | 82,917 | 13,690 | 12,476 | 13,991 | 190,798 |
| entertainment | 282,757 | 3.154 | 97,962 | 21,418 | 21,987 | 21,975 | 119,415 |
| finance | 222,204 | 2.915 | 98,212 | 13,021 | 11,014 | 9,426 | 90,531 |
| games | 179,056 | 3.32 | 53,521 | 15,167 | 13,684 | 13,891 | 82,793 |
| food_delivery | 122,889 | 2.555 | 64,237 | 8,200 | 7,119 | 4,652 | 38,681 |
| social_networking | 105,758 | 3.175 | 37,747 | 6,434 | 7,431 | 7,810 | 46,336 |
| productivity | 88,139 | 3.394 | 26,975 | 5,524 | 5,764 | 5,571 | 44,305 |
| social | 87,427 | 2.637 | 41,495 | 6,905 | 7,072 | 5,768 | 26,187 |
| education | 80,479 | 4.024 | 11,431 | 3,976 | 5,747 | 9,389 | 49,936 |
| photo_and_video | 73,379 | 2.807 | 31,184 | 6,069 | 6,246 | 5,493 | 24,387 |
| ai_tools | 65,386 | 3.896 | 14,638 | 2,038 | 2,368 | 2,774 | 43,568 |
| business | 60,578 | 2.904 | 24,720 | 5,208 | 4,319 | 3,859 | 22,472 |
| health | 56,798 | 2.908 | 20,287 | 6,799 | 6,202 | 4,850 | 18,660 |
| utilities | 40,797 | 3.351 | 13,279 | 2,273 | 2,430 | 2,494 | 20,321 |
| news | 31,598 | 2.488 | 16,628 | 2,612 | 1,854 | 1,329 | 9,175 |
| lifestyle | 23,362 | 2.349 | 12,225 | 2,648 | 1,933 | 1,212 | 5,344 |
| food_and_drink | 22,123 | 2.786 | 9,607 | 2,023 | 1,668 | 1,141 | 7,684 |
| health_and_fitness | 21,917 | 3.996 | 3,643 | 1,169 | 1,213 | 1,493 | 14,399 |
| sports | 20,629 | 2.601 | 10,743 | 1,249 | 943 | 874 | 6,820 |
| navigation | 20,000 | 2.461 | 11,234 | 1,094 | 935 | 695 | 6,042 |
| music | 12,697 | 3.779 | 2,399 | 840 | 1,027 | 1,336 | 7,095 |
| medical | 5,467 | 3.634 | 1,155 | 473 | 534 | 362 | 2,943 |
| books | 2,453 | 3.946 | 430 | 144 | 136 | 162 | 1,581 |
| weather | 2,209 | 3.89 | 380 | 137 | 160 | 201 | 1,331 |
| graphics_and_design | 2,136 | 3.358 | 457 | 200 | 312 | 455 | 712 |

## Text Quality

### Review Length

| review_count | avg_chars | p10_chars | p25_chars | p50_chars | p75_chars | p90_chars | p95_chars | max_chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2,295,799 | 168.8 | 15 | 41 | 102 | 217 | 390 | 544 | 6,000 |

### Low-Signal And Formatting Patterns

| review_count | blank_content | content_1_to_20_chars | content_21_to_50_chars | blank_title | angle_bracket_content | multiline_content | non_ascii_content |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2,295,799 | 0 | 310,651 | 368,678 | 0 | 77 | 0 | 1,030,881 |

### Duplicate Patterns

| review_count | distinct_review_keys | normalized_duplicate_group_count | normalized_duplicate_row_count | largest_normalized_duplicate_group |
| --- | --- | --- | --- | --- |
| 2,295,799 | 2,295,799 | 26,160 | 223,586 | 6,600 |

Top normalized duplicate examples:

| row_count | app_count | sample |
| --- | --- | --- |
| 6,600 | 182 | good |
| 4,368 | 169 | love it |
| 4,227 | 164 | great |
| 2,574 | 161 | awesome |
| 2,350 | 126 | excelente |
| 2,341 | 132 | great app |
| 2,303 | 139 | excellent |
| 2,038 | 158 | i love it |
| 1,924 | 158 | amazing |
| 1,652 | 141 | very good |
| 1,592 | 141 | nice |
| 1,282 | 131 | the best |
| 1,164 | 107 | thank you |
| 1,071 | 110 | thanks |
| 1,030 | 106 | good app |

## Freshness And Time Coverage

### Recent Monthly Density

| month | review_count | app_count |
| --- | --- | --- |
| 2026-07 | 82,111 | 200 |
| 2026-06 | 157,826 | 199 |
| 2026-05 | 129,518 | 199 |
| 2026-04 | 96,926 | 193 |
| 2026-03 | 100,928 | 185 |
| 2026-02 | 84,589 | 172 |
| 2026-01 | 87,517 | 165 |
| 2025-12 | 75,373 | 156 |
| 2025-11 | 57,745 | 150 |
| 2025-10 | 58,556 | 146 |
| 2025-09 | 48,303 | 137 |
| 2025-08 | 37,569 | 130 |
| 2025-07 | 37,687 | 123 |
| 2025-06 | 39,680 | 118 |
| 2025-05 | 39,299 | 114 |
| 2025-04 | 33,348 | 109 |
| 2025-03 | 39,658 | 108 |
| 2025-02 | 35,039 | 104 |
| 2025-01 | 37,919 | 99 |
| 2024-12 | 27,345 | 97 |
| 2024-11 | 27,062 | 94 |
| 2024-10 | 27,116 | 93 |
| 2024-09 | 27,290 | 89 |
| 2024-08 | 31,446 | 86 |

### Stalest Apps By Newest Review

| app_name | category | review_count | newest_review_age_days | reviews_last_7_days | reviews_last_30_days |
| --- | --- | --- | --- | --- | --- |
| SkyView® | education | 2,281 | 12.4 | 0 | 2 |
| WeatherWise.app | weather | 212 | 7.2 | 0 | 14 |
| ViX: TV, Sports and News | entertainment | 2,174 | 7 | 1 | 22 |
| Papa's Freezeria To Go! | games | 1,406 | 6.2 | 1 | 2 |
| FWC2026 Mobile Tickets | sports | 158 | 6.2 | 1 | 38 |
| Backyard Baseball '01 | games | 250 | 5 | 3 | 8 |
| AnkiMobile Flashcards | education | 1,211 | 4.1 | 2 | 9 |
| RadarScope | weather | 1,997 | 3 | 3 | 12 |
| Heads Up! | games | 7,608 | 2.8 | 7 | 29 |
| Scritchy Scratchy | games | 126 | 2.5 | 2 | 35 |
| Stardew Valley | games | 1,831 | 2.4 | 6 | 40 |
| Shadowrocket | utilities | 2,025 | 2.3 | 9 | 25 |
| HotSchedules | business | 3,261 | 2.2 | 1 | 4 |
| MONOPOLY: The Board Game | games | 2,073 | 2.2 | 7 | 39 |
| Blink Home Monitor | utilities | 2,039 | 2.1 | 29 | 144 |
| Candy Crush Soda Saga | games | 1,960 | 2.1 | 13 | 48 |
| Cashman Casino Slots Games | games | 19,493 | 2 | 13 | 73 |
| Bloons TD 6 | games | 3,527 | 2 | 15 | 106 |
| Telemundo: Series y TV en vivo | entertainment | 3,410 | 2 | 9 | 62 |
| Homescapes: Match 3 Games | games | 2,441 | 2 | 24 | 181 |
| Facetune: Photo & Video Editor | photo_and_video | 2,234 | 2 | 19 | 84 |
| Raya | lifestyle | 820 | 2 | 1 | 7 |
| YouTube TV | entertainment | 11,482 | 1.9 | 7 | 46 |
| Procreate Pocket | graphics_and_design | 2,136 | 1.9 | 3 | 18 |
| Cleaner Guru: Clean Up Storage | utilities | 2,031 | 1.9 | 3 | 17 |

## Pipeline Behavior

### Run Summary

| run_count | first_loaded_at | last_loaded_at | page_count | raw_review_rows | reviews_inserted | reviews_updated | fetch_errors | capped_scopes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10,444 | 2026-06-18T19:00:24.473852+00:00 | 2026-07-16T07:07:46.383691+00:00 | 127,261 | 2,517,689 | 2,295,799 | 228 | 1,230 | 46 |

### Page Status Codes

| status_code | page_count | review_rows |
| --- | --- | --- |
| 200 | 126,031 | 2,517,689 |
| 404 | 766 | 0 |
| 429 | 410 | 0 |
| null | 50 | 0 |
| 502 | 2 | 0 |
| 504 | 2 | 0 |

### Terminal Reasons

| terminal_reason | page_count | review_rows |
| --- | --- | --- |
| none | 117,624 | 2,339,575 |
| caught_up_to_existing_reviews | 8,798 | 175,936 |
| fetch_error | 477 | 0 |
| sparse_fetch_error_threshold | 213 | 0 |
| no_next_href | 71 | 650 |
| page_cap | 46 | 888 |
| target_review_count_reached | 21 | 420 |
| time_budget_retry_window_exceeded | 9 | 180 |
| time_budget_exceeded | 2 | 40 |

### Retry Attempts

| attempt_count | page_count | review_rows |
| --- | --- | --- |
| 1 | 123,483 | 2,450,007 |
| 2 | 3,045 | 57,682 |
| 3 | 424 | 8,060 |
| 4 | 84 | 1,640 |
| 5 | 12 | 240 |
| 6 | 213 | 60 |

### Empty And Error Page Summary

| empty_pages | empty_pages_with_next_link | empty_pages_without_next_link | http_429_attempts | http_429_pages | soft_retries | final_non_200_pages | retried_pages | error_pages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,230 | 0 | 1,230 | 1,596 | 410 | 0 | 1,180 | 3,778 | 1,230 |

### Apps With The Most Fetched Pages

| app_name | category | run_count | page_count | page_review_rows | http_429_attempts | http_429_pages | soft_retries | final_non_200_pages | retried_pages | avg_run_page_window_minutes | max_run_page_window_minutes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Amazon Shopping | shopping | 94 | 5,236 | 104,480 | 18 | 7 | 0 | 10 | 76 | 9.38 | 59.97 |
| Walmart | shopping | 90 | 5,179 | 103,380 | 16 | 6 | 0 | 8 | 72 | 9.85 | 59.99 |
| Uber | travel | 85 | 4,186 | 83,500 | 22 | 9 | 0 | 10 | 69 | 8.68 | 59.85 |
| Lyft | travel | 79 | 3,554 | 70,940 | 16 | 6 | 0 | 6 | 59 | 7.43 | 30 |
| ChatGPT | ai_tools | 71 | 3,388 | 67,680 | 12 | 4 | 0 | 4 | 58 | 9.09 | 29.98 |
| Duolingo | education | 73 | 3,322 | 66,320 | 12 | 4 | 0 | 5 | 55 | 8.33 | 29.98 |
| Spotify | entertainment | 74 | 3,281 | 65,460 | 14 | 5 | 0 | 8 | 67 | 9.4 | 38.42 |
| Venmo | finance | 74 | 3,234 | 64,580 | 13 | 5 | 0 | 5 | 38 | 6.89 | 29.96 |
| Love and Deepspace | games | 45 | 3,223 | 64,060 | 29 | 19 | 0 | 20 | 169 | 36.26 | 59.55 |
| DoorDash | food_delivery | 76 | 3,190 | 63,620 | 19 | 7 | 0 | 8 | 49 | 7.07 | 29.94 |
| Cash App | finance | 74 | 3,131 | 62,540 | 12 | 4 | 0 | 4 | 34 | 6.64 | 29.98 |
| Booking.com | travel | 72 | 3,123 | 62,360 | 12 | 4 | 0 | 5 | 45 | 7.08 | 30 |
| Uber Eats | food_delivery | 73 | 3,122 | 62,320 | 15 | 5 | 0 | 6 | 44 | 6.97 | 29.98 |
| Netflix | entertainment | 75 | 3,112 | 62,180 | 10 | 3 | 0 | 3 | 32 | 6.63 | 29.98 |
| Target | shopping | 78 | 3,111 | 61,581 | 8 | 2 | 0 | 24 | 32 | 6.45 | 59.97 |
| TikTok | social | 73 | 3,076 | 61,300 | 11 | 4 | 0 | 10 | 52 | 7.82 | 29.99 |
| PayPal | finance | 74 | 3,005 | 59,980 | 13 | 5 | 0 | 6 | 54 | 6.67 | 29.92 |
| MyFitnessPal | health | 71 | 2,903 | 57,960 | 10 | 3 | 0 | 4 | 37 | 6.53 | 29.97 |
| Expedia | travel | 66 | 2,043 | 40,436 | 6 | 1 | 0 | 17 | 22 | 4.48 | 29.98 |
| Peacock TV: Stream TV & Movies | entertainment | 64 | 1,997 | 39,533 | 6 | 1 | 0 | 19 | 28 | 4.62 | 29.96 |
| Instagram | social | 74 | 1,572 | 28,780 | 12 | 4 | 0 | 132 | 46 | 4.51 | 19.94 |
| WhatsApp Messenger | social_networking | 53 | 1,408 | 28,140 | 6 | 1 | 0 | 1 | 27 | 4.17 | 29.98 |
| CapCut: Photo & Video Editor | photo_and_video | 55 | 1,399 | 27,920 | 8 | 2 | 0 | 3 | 20 | 3.66 | 29.99 |
| Google | utilities | 54 | 1,317 | 26,280 | 11 | 3 | 0 | 3 | 16 | 3.23 | 29.95 |
| Airbnb | travel | 66 | 1,308 | 25,387 | 8 | 2 | 0 | 32 | 16 | 2.59 | 29.94 |

## Known Limitations

- The pipeline reads Apple-hosted public web catalog review payloads exposed to the App Store web experience. This is not the App Store Connect Customer Reviews API, does not use owner credentials, and does not carry an Apple SLA for third-party bulk ingestion.
- Completeness is empirical per app, country, and source scope. A scope is only treated as historically exhausted when pagination reaches `no_next_href`; page cap, time budget, overlap, final non-200, and fetch-error stops mean the current row count is a lower bound.
- Daily/incremental interpretation depends on stable review keys and Postgres upserts. Repeated runs can add new rows or update existing rows, but source-side ordering, removed reviews, and Apple response changes should be monitored through page and terminal-reason metrics.
- Public web catalog payloads do not currently provide every owner-API field. Version, vote sum, vote count, and similar App Store Connect-style review metadata are intentionally excluded from the production review schema until they are available in the public response.
- Normalized duplicate detection uses lowercased whitespace-normalized content hashes; it is useful for triage, not semantic near-duplicate modeling.
- Runtime by app is a page-window proxy based on stored page timestamps, not full GitHub job wall-clock time.
