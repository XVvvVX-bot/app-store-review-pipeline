# Apple Review Pipeline Operating Limits

Generated at: `2026-06-30T23:29:59+00:00`
Database: `postgresql:///app_store_reviews`
Source: `apple_app_store_web_catalog_reviews`
Ledger: `docs/experiments/operating_model_run_ledger.json`

## Recommendation

Keep the twice-daily full-scope incremental schedule as the production baseline while remaining controlled tests are completed.

Evidence status: **interim**. Pending controlled experiments: FG1, FG2.

Rationale:
- Recent successful full-scope runs show clean source-pressure metrics.
- There are enough successful baseline observations to compare against experiments.
- High-activity apps account for 72.0% of recent inserts and 52.7% of recent pages.

## Experiment Target Groups

Strategy comparisons use fixed randomized 25-app groups instead of running every strategy on all 200 apps. This keeps each experiment fast and prevents one strategy test from consuming the incremental-review signal needed by the next strategy test.

| group | app_count | category_count | top_categories | example_apps |
| --- | --- | --- | --- | --- |
| om_group_01 | 25 | 15 | games:7, entertainment:3, shopping:2, social_networking:2 | Netflix, Vinted: Pre-loved marketplace, Depop - Buy & Sell Clothes, Fubo: Watch Live TV & Sports |
| om_group_02 | 25 | 16 | games:6, entertainment:3, productivity:2, shopping:2 | Spotify, Duolingo, Love Island USA, Google Gemini |
| om_group_03 | 25 | 16 | games:7, entertainment:3, productivity:2, business:1 | Walmart, Uber, Instagram, MyFitnessPal |
| om_group_04 | 25 | 17 | games:6, entertainment:3, shopping:2, books:1 | Target, DoorDash, Shop: All your favorite brands, Tubi: Movies & Live TV |
| om_group_05 | 25 | 15 | games:6, entertainment:4, shopping:2, social_networking:2 | Amazon Shopping, Airbnb, PayPal, Planet Fitness |
| om_group_06 | 25 | 16 | games:6, entertainment:3, finance:2, shopping:2 | Expedia, Cash App, FOX One: Live News, Sports, TV, Telemundo: Series y TV en vivo |
| om_group_07 | 25 | 14 | games:7, entertainment:4, shopping:2, social_networking:2 | Uber Eats, Booking.com, Venmo, ChatGPT |
| om_group_08 | 25 | 15 | games:7, entertainment:4, shopping:2, business:1 | Lyft, TikTok, Peacock TV: Stream TV & Movies, Trump Accounts: Official App |

## Controlled Experiment Findings

| experiment_id | status | experiment_group | frequency_isolation_status | contaminating_run_count | matching_run_count | successful_run_count | source_pressure_clean_run_count | page_count | review_rows | inserted | skipped | duplicate_skip_rate | inserted_per_page | http_429 | non_200 | fetch_errors | retried_pages | median_runtime_minutes | finding |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | completed |  |  | 0 | 1 | 1 | 1 | 283 | 5,640 | 2,826 | 2,811 | 0.4987 | 9.986 | 0 | 1 | 1 | 11 | 38.45 | Clean. The six-hour full-scope run passed source-pressure thresholds; its marginal yield was 9.986 inserts/page with 49.9% duplicate skips. |
| F2 | completed_source_clean_github_artifact_failure |  |  | 0 | 1 | 0 | 1 | 203 | 4,060 | 136 | 3,924 | 0.9665 | 0.67 | 0 | 0 | 0 | 14 | 41.35 | Source-clean but not GitHub-clean. The run passed source-pressure checks, but at least one matching job failed after ingestion. |
| FG1 | planned | om_group_03 | pending_seed | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | Pending. No matching run has been recorded in the ledger yet. |
| FG2 | seed_completed | om_group_04 | pending_treatment | 0 | 1 | 1 | 1 | 27 | 540 | 107 | 433 | 0.8019 | 3.963 | 0 | 0 | 0 | 0 | 3.1 | Seed clean. Treatment is pending, and no tracked full-scope run has contaminated the pair yet. |
| D1 | completed_rejected | om_group_01 |  | 0 | 1 | 1 | 1 | 25 | 500 | 0 | 500 | 1 | 0 | 0 | 0 | 0 | 0 | 2.78 | Source-pressure clean, but rejected by the paired audit or strategy-specific decision rule. |
| D2 | completed_rejected | om_group_02 |  | 0 | 1 | 1 | 1 | 29 | 580 | 188 | 392 | 0.6759 | 6.483 | 0 | 0 | 0 | 0 | 3.02 | Source-pressure clean, but rejected by the paired audit or strategy-specific decision rule. |

Interpretation:
- Full-scope F1/F2 runs are calibration/control evidence; future frequency strategy tests use randomized 25-app groups so one test does not consume the full 200-app incremental signal.
- `successful_run_count` is GitHub-clean; `source_pressure_clean_run_count` is source-pressure clean and can include post-ingestion artifact-only failures.
- Depth tests (D1/D2) use randomized 25-app groups and measure whether page caps miss more than 5% of rows later captured by a same-group uncapped audit.
- A final recommendation should wait for the pending tests unless source-pressure thresholds stop the ladder early.

### Depth Audit Comparisons

| experiment_id | cap_run_count | audit_run_count | cap_pages | audit_pages | cap_inserted | audit_inserted_after_cap | missed_insert_rate_vs_uncapped_audit | threshold | cap_http_429 | audit_http_429 | finding |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 1 | 1 | 25 | 26 | 0 | 12 | 1 | 0.05 | 0 | 0 | Rejected. The cap missed more than the configured audit threshold. |
| D2 | 1 | 1 | 29 | 32 | 188 | 25 | 0.1174 | 0.05 | 0 | 0 | Rejected. The cap missed more than the configured audit threshold. |

## Aggregate Observations

| observed_run_count | successful_run_count | source_pressure_clean_run_count | source_pressure_clean_pages | source_pressure_clean_review_rows | source_pressure_clean_reviews_inserted | source_pressure_clean_duplicates_skipped | source_pressure_clean_http_429_rate | failed_or_cancelled_run_count | successful_pages | successful_review_rows | successful_reviews_inserted | successful_duplicates_skipped | successful_http_429_rate | successful_retried_pages | successful_fetch_errors | successful_capped_scopes | median_successful_runtime_minutes | median_successful_pages | median_successful_inserted_per_page | max_schedule_delay_minutes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 19 | 15 | 18 | 4,796 | 95,820 | 54,812 | 40,991 | 0 | 4 | 4,231 | 84,540 | 50,531 | 33,993 | 0 | 128 | 4 | 3 | 40.7 | 227 | 5.811 | 315.5 |

### Successful Run Attempt Distribution

| attempt_count | page_count |
| --- | --- |
| 1 | 4,103 |
| 2 | 113 |
| 3 | 15 |

### Successful Run Terminal Reasons

| terminal_reason | page_count |
| --- | --- |
| caught_up_to_existing_reviews | 2,121 |
| none | 2,107 |
| page_cap | 3 |

## Observed Runs

| github_run_id | label | experiment_group | event | conclusion | runtime_minutes | schedule_delay_minutes | job_result | apps | pages | review_rows | inserted | updated | skipped | duplicate_skip_rate | http_429 | non_200 | fetch_errors | capped_scopes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 28200344910 | manual full-scope validation |  | workflow_dispatch | success | 99.73 |  | 202/202 | 200 | 1,588 | 31,740 | 29,561 | 7 | 2,172 | 0.0684 | 0 | 1 | 1 | 0 |
| 28215541622 | manual full-scope validation |  | workflow_dispatch | success | 40.97 |  | 202/202 | 200 | 322 | 6,420 | 1,425 | 0 | 4,995 | 0.778 | 0 | 1 | 1 | 0 |
| 28253196497 | scheduled full-scope baseline |  | schedule | success | 51.07 | 118.2 | 202/202 | 200 | 276 | 5,520 | 2,690 | 0 | 2,830 | 0.5127 | 0 | 0 | 0 | 0 |
| 28281339396 | scheduled full-scope baseline |  | schedule | success | 54.33 | 207.5 | 202/202 | 200 | 393 | 7,860 | 5,264 | 3 | 2,593 | 0.3299 | 0 | 0 | 0 | 0 |
| 28294714167 | scheduled full-scope baseline |  | schedule | success | 40.7 | 69.3 | 202/202 | 200 | 227 | 4,540 | 1,319 | 0 | 3,221 | 0.7095 | 0 | 0 | 0 | 0 |
| 28313382547 | manual fallback after delayed schedule |  | workflow_dispatch | success | 25.93 |  | 202/202 | 200 | 339 | 6,760 | 3,986 | 3 | 2,771 | 0.4099 | 0 | 1 | 1 | 0 |
| 28314674192 | late scheduled full-scope baseline |  | schedule | success | 76.87 | 240.9 | 202/202 | 200 | 214 | 4,280 | 620 | 0 | 3,660 | 0.8551 | 0 | 0 | 0 | 0 |
| 28328447489 | late scheduled full-scope baseline |  | schedule | success | 40.82 | 74.28 | 202/202 | 200 | 230 | 4,600 | 1,455 | 0 | 3,145 | 0.6837 | 0 | 0 | 0 | 0 |
| 28358536459 | scheduled run with one failed app job |  | schedule | failure | 87.88 | 315.5 | 201/202 | 200 | 337 | 6,720 | 4,046 | 1 | 2,673 | 0.3978 | 0 | 1 | 1 | 0 |
| 28391589916 | scheduled full-scope baseline |  | schedule | success | 54.35 | 159 | 202/202 | 200 | 220 | 4,400 | 1,053 | 0 | 3,347 | 0.7607 | 0 | 0 | 0 | 0 |
| 28417322081 | F1 six-hour full-scope experiment |  | workflow_dispatch | success | 38.45 |  | 202/202 | 200 | 283 | 5,640 | 2,826 | 3 | 2,811 | 0.4984 | 0 | 1 | 1 | 0 |
| 28473639075 | F2 three-hour full-scope experiment |  | workflow_dispatch | failure | 41.35 |  | 201/202 | 200 | 203 | 4,060 | 136 | 0 | 3,924 | 0.9665 | 0 | 0 | 0 | 0 |
| 28476830652 | abandoned full-scope D1 attempt |  | workflow_dispatch | cancelled | 2.57 |  | 18/202 | 16 | 16 | 320 | 154 | 0 | 166 | 0.5188 | 0 | 0 | 0 | 4 |
| 28479893917 | D1 one-page grouped cap deadlock observation | om_group_01 | workflow_dispatch | failure | 2.73 |  | 26/27 | 25 | 25 | 500 | 99 | 0 | 401 | 0.802 | 0 | 0 | 0 | 1 |
| 28480629280 | D1 one-page grouped uncapped audit | om_group_01 | workflow_dispatch | success | 2.98 |  | 27/27 | 25 | 26 | 520 | 12 | 0 | 508 | 0.9769 | 0 | 0 | 0 | 0 |
| 28480461263 | D1 one-page grouped cap | om_group_01 | workflow_dispatch | success | 2.78 |  | 27/27 | 25 | 25 | 500 | 0 | 0 | 500 | 1 | 0 | 0 | 0 | 1 |
| 28481422182 | D2 three-page grouped uncapped audit | om_group_02 | workflow_dispatch | success | 3 |  | 27/27 | 25 | 32 | 640 | 25 | 0 | 615 | 0.9609 | 0 | 0 | 0 | 0 |
| 28481235165 | D2 three-page grouped cap | om_group_02 | workflow_dispatch | success | 3.02 |  | 27/27 | 25 | 29 | 580 | 188 | 0 | 392 | 0.6759 | 0 | 0 | 0 | 2 |
| 28481871844 | FG2 three-hour grouped seed/control | om_group_04 | workflow_dispatch | success | 3.1 |  | 27/27 | 25 | 27 | 540 | 107 | 0 | 433 | 0.8019 | 0 | 0 | 0 | 0 |

## Activity Segments

Segments are computed from successful ledger runs by app-level inserted rows and page load.

| segment | app_count | page_count | inserted | page_share | insert_share | inserted_per_page |
| --- | --- | --- | --- | --- | --- | --- |
| high | 50 | 2,228 | 36,408 | 0.5266 | 0.7205 | 16.34 |
| normal | 100 | 1,433 | 12,537 | 0.3387 | 0.2481 | 8.749 |
| low | 50 | 570 | 1,586 | 0.1347 | 0.0314 | 2.782 |

### Top Recent Activity Apps

| app_name | category | activity_segment | page_count | review_rows | inserted | updated | observed_runs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Spotify | entertainment | high | 274 | 5,460 | 3,404 | 0 | 12 |
| ChatGPT | ai_tools | high | 127 | 2,540 | 2,450 | 0 | 10 |
| YouTube | photo_and_video | high | 117 | 2,340 | 2,185 | 0 | 10 |
| Duolingo | education | high | 114 | 2,260 | 2,083 | 0 | 12 |
| Vinted: Pre-loved marketplace | shopping | high | 85 | 1,700 | 1,557 | 0 | 12 |
| Facebook | social_networking | high | 73 | 1,460 | 1,340 | 0 | 10 |
| DoorDash | food_delivery | high | 69 | 1,380 | 1,243 | 0 | 11 |
| TikTok | social | high | 66 | 1,320 | 1,218 | 0 | 10 |
| Instagram | social | high | 64 | 1,280 | 1,194 | 1 | 10 |
| Walmart | shopping | high | 61 | 1,220 | 1,118 | 0 | 10 |
| Rips by Triumph | shopping | high | 63 | 1,260 | 1,101 | 0 | 10 |
| Uber | travel | high | 56 | 1,120 | 1,024 | 0 | 10 |
| ReelShort - Stream Drama & TV | entertainment | high | 51 | 1,020 | 898 | 0 | 11 |
| SHEIN - Shopping Online | shopping | high | 47 | 940 | 816 | 0 | 10 |
| Life360: Family Safety & GPS | social_networking | high | 46 | 920 | 779 | 0 | 12 |

## Database Footprint

| table_name | row_count | total_size | total_bytes |
| --- | --- | --- | --- |
| app_store_review_changes | 2,270,287 | 1159 MB | 1,215,217,664 |
| app_store_review_pages | 117,861 | 84 MB | 88,440,832 |
| app_store_reviews | 2,270,217 | 2465 MB | 2,584,256,512 |
| app_store_runs | 4,583 | 1504 kB | 1,540,096 |

## Planned Controlled Tests

| experiment_id | status | comparison_group | experiment_group | description | success_criteria |
| --- | --- | --- | --- | --- | --- |
| F1 | completed | F1_six_hour_full_scope |  | Completed full-scope six-hour calibration run. Kept as control evidence; do not repeat as the default strategy-test pattern. | 202/202 jobs success, HTTP 429 rate below 0.5%, fetch error rate below 1%, no abnormal runtime growth. |
| F2 | completed_source_clean_github_artifact_failure | F2_three_hour_full_scope |  | Completed full-scope three-hour calibration run. Source ingestion was clean, but the workflow ended with a post-ingestion GitHub artifact failure. Kept as control evidence; do not repeat as the default strategy-test pattern. | Source-pressure metrics clean enough to inform grouped frequency-test design. |
| FG1 | planned | FG1_six_hour_grouped_frequency | om_group_03 | Randomized 25-app group uncapped incremental treatment run six hours after a clean same-group seed/control pass. | Clean source-pressure metrics, no abnormal runtime growth, and enough marginal inserted rows per page to justify a six-hour grouped refresh. |
| FG2 | seed_completed | FG2_three_hour_grouped_frequency | om_group_04 | Seed/control run completed for randomized 25-app group. Treatment run should execute three hours after the seed to measure marginal inserts. | Clean source-pressure metrics, no abnormal runtime growth, and enough marginal inserted rows per page to justify a three-hour grouped refresh. |
| D1 | completed_rejected | D1_one_page_cap | om_group_01 | Completed randomized 25-app group capped at one page per app, followed by an uncapped audit on the same group. Rejected because the audit found missed inserts beyond the threshold. | Audit inserts after the capped pass are no more than 5% of total capped-plus-audit inserts, with clean source-pressure metrics. |
| D2 | completed_rejected | D2_three_page_cap | om_group_02 | Completed cap=3 randomized 25-app test. Rejected: audit inserted 25 additional Spotify reviews, an 11.7% missed-insert rate. | Audit inserts after the capped pass are no more than 5% of total capped-plus-audit inserts, with clean source-pressure metrics. |

## Operating Decision Rules

- Keep twice-daily full-scope incremental if shorter-frequency runs stay clean but have low marginal inserts per page, or if capped runs miss more than 5% of audit-captured rows.
- Recommend higher-frequency shallow refresh only if source pressure remains clean and capped runs miss no more than 5% of audit-captured rows.
- Recommend hybrid refresh only if high-activity apps account for most new rows and can be refreshed with fewer total pages than full-scope high-frequency runs.

## Notes

- GitHub schedule delay is tracked separately from ingestion reliability.
- `app_store_runs` is per app job, so GitHub workflow run metrics are joined by ledger time window.
- Historical backfill remains paused while this operating-model test is active.
