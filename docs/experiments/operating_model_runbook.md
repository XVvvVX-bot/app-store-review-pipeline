# Operating Model Experiment Runbook

This runbook records the controlled procedure for validating the App Store review daily incremental operating model. It is intentionally operational: the final evidence and recommendation live in `docs/operating_limits.md`, while this file explains how each experiment should be run and verified.

## Guardrails

- Use `workflow_dispatch` only for controlled experiments.
- Do not run new strategy experiments against all 200 apps unless the goal is a baseline/control observation.
- For strategy comparisons, use fixed randomized 25-app groups and wait the intended gap only for that group.
- Do not start a new experiment if an `App Store Review Pipeline` run is active.
- Stop frequency escalation if any run has failed jobs, HTTP 429 rate >= 0.5%, repeated source non-200 errors, fetch error rate >= 1%, or abnormal runtime growth.
- Keep the twice-daily schedule unchanged while experiments run.
- Record every experimental run in `docs/experiments/operating_model_run_ledger.json`.
- Use randomized 25-app experiment groups for strategy comparisons instead of running every strategy on all 200 apps. The group manifest is `docs/experiments/operating_model_target_groups.json`.
- Run a capped depth pass and its uncapped audit on the same group. Run different strategy families on different groups so one experiment does not consume the next experiment's incremental signal.
- For grouped frequency tests, do not allow any full-scope scheduled or manual run between the seed/control pass and the treatment pass. A full-scope run would refresh the same apps first and consume the very incremental-review signal the treatment is supposed to measure.
- Treat Postgres as the ingestion source of truth. Raw artifacts are best-effort diagnostics; an `upload-artifact` timeout after complete Postgres writes is a GitHub artifact issue, not an Apple source-ingestion failure.

## Preflight Checks

```bash
gh run list \
  --repo XVvvVX-bot/app-store-review-pipeline \
  --workflow app-store-daily-pipeline.yml \
  --limit 30 \
  --json databaseId,event,status,conclusion,createdAt,updatedAt,url \
  --jq '.[] | select(.status!="completed")'
```

```bash
gh api repos/XVvvVX-bot/app-store-review-pipeline/actions/runners \
  --jq '.runners[] | select(.labels[].name == "app-store-review-pipeline") | [.name,.status,.busy] | @tsv'
```

```sql
select
  max(fetched_at::timestamptz) as last_page_at,
  count(*) filter (where fetched_at::timestamptz >= now() - interval '12 hours') as pages_12h,
  count(*) filter (where status_code = 429 and fetched_at::timestamptz >= now() - interval '12 hours') as http_429_12h,
  count(*) filter (
    where status_code is not null
      and status_code <> 200
      and fetched_at::timestamptz >= now() - interval '12 hours'
  ) as non_200_12h
from app_store_review_pages
where source = 'apple_app_store_web_catalog_reviews';
```

## Full-Scope Calibration Evidence

The completed F1/F2 full-scope runs are kept as calibration/control evidence, not as the template for future strategy tests. They were useful because they showed that shorter gaps can remain source-pressure clean, but they also consumed the incremental signal across all 200 apps. Repeating that pattern would force every later experiment to wait much longer for fresh reviews.

Use all 200 apps only for:

- scheduled F0 baseline observations;
- occasional full-scope control runs;
- final production smoke checks after a recommendation is chosen.

## Grouped Frequency Tests

Purpose: test whether shorter refresh gaps add useful fresh rows without consuming the newest incremental-review signal across the full target list.

Use a same-group pair for each frequency test:

1. Run a grouped uncapped seed/control pass.
2. Wait the intended gap for that group.
3. Run the same grouped uncapped treatment pass.
4. Compare treatment inserted rows per page, duplicate rate, runtime, and source-pressure metrics.

Schedule isolation matters for these tests. The treatment run is only interpretable if no full-scope scheduled or manual run touched the same app group after the seed. If a full-scope run lands in the middle, mark the pair as contaminated, keep it as operational evidence only, and restart that frequency test on a fresh randomized group or after the next scheduled baseline.

When possible, choose seed times so the treatment finishes before the next twice-daily schedule. For example, a three-hour grouped test can fit between afternoon manual work and the 20:07 PDT scheduled baseline; a six-hour grouped test should normally start shortly after a scheduled baseline, not shortly before one.

Planned grouped frequency tests:

- `FG1_six_hour_grouped_frequency` on `om_group_03`.
- `FG2_three_hour_grouped_frequency` on `om_group_04`.

Run a grouped uncapped frequency pass by setting `experiment_group` and keeping `max_pages_per_app_country=0`.

```bash
gh workflow run app-store-daily-pipeline.yml \
  --repo XVvvVX-bot/app-store-review-pipeline \
  --ref main \
  -f limit=0 \
  -f target_offset=0 \
  -f experiment_group=om_group_03 \
  -f max_parallel=4 \
  -f max_pages_per_app_country=0 \
  -f pressure_ramp_mode=fixed \
  -f start_page=1 \
  -f review_limit=20 \
  -f request_delay_seconds=10 \
  -f request_delay_jitter_seconds=5 \
  -f web_429_retries=2 \
  -f web_429_retry_seconds=300 \
  -f web_429_backoff_multiplier=1.5 \
  -f web_429_retry_jitter_seconds=60 \
  -f web_time_budget_seconds=3600 \
  -f web_scope_time_budget_seconds=3600 \
  -f web_429_cooldown_minutes=0 \
  -f web_429_circuit_breaker_lookback_minutes=720 \
  -f web_429_circuit_breaker_min_pages=4 \
  -f web_429_circuit_breaker_max_rate=0.5
```

Ledger fields:

- `label`: `FG1 six-hour grouped frequency experiment` or `FG2 three-hour grouped frequency experiment`
- `comparison_group`: `FG1_six_hour_grouped_frequency` or `FG2_three_hour_grouped_frequency`
- `experiment_group`: `om_group_03` or `om_group_04`
- `event`: `workflow_dispatch`
- `inputs`: same as command above, with `overlap_stop` set to `enabled`

## Randomized Experiment Groups

The controlled depth/scope tests use fixed randomized groups from `docs/experiments/operating_model_target_groups.json`.

Do not use all 200 apps for every strategy test. Full-scope runs are useful as baseline/control observations, but they also consume the newest incremental-review signal across the entire target list. If D1, D2, hybrid, and frequency tests all use the same 200 apps, each later test has to wait much longer for fresh reviews to accumulate. Fixed randomized groups preserve comparability while allowing multiple strategy tests to run in shorter windows.

- `om_group_01`: D1 one-page cap and D1 uncapped audit.
- `om_group_02`: D2 three-page cap and D2 uncapped audit.
- `om_group_03`: FG1 six-hour grouped frequency test.
- `om_group_04`: FG2 three-hour grouped frequency test.
- `om_group_05` through `om_group_08`: reserved for hybrid, replication, or follow-up tests.

The manifest is generated from active targets using a fixed seed. Apps are bucketed by category, shuffled inside each category, then assigned to the smallest eligible group with a category-count tie breaker. This keeps all eight groups at 25 apps while preserving reproducibility and a reasonably balanced category mix.

## D1: One-Page Cap With Uncapped Audit

Purpose: test whether a one-page cap misses too many rows compared with a follow-up uncapped audit on the same randomized 25-app group.

Run the capped pass:

```bash
gh workflow run app-store-daily-pipeline.yml \
  --repo XVvvVX-bot/app-store-review-pipeline \
  --ref main \
  -f limit=0 \
  -f target_offset=0 \
  -f experiment_group=om_group_01 \
  -f max_parallel=4 \
  -f max_pages_per_app_country=1 \
  -f pressure_ramp_mode=fixed \
  -f start_page=1 \
  -f review_limit=20 \
  -f request_delay_seconds=10 \
  -f request_delay_jitter_seconds=5 \
  -f web_429_retries=2 \
  -f web_429_retry_seconds=300 \
  -f web_429_backoff_multiplier=1.5 \
  -f web_429_retry_jitter_seconds=60 \
  -f web_time_budget_seconds=3600 \
  -f web_scope_time_budget_seconds=3600 \
  -f web_429_cooldown_minutes=0 \
  -f web_429_circuit_breaker_lookback_minutes=720 \
  -f web_429_circuit_breaker_min_pages=4 \
  -f web_429_circuit_breaker_max_rate=0.5
```

Ledger fields for capped pass:

- `label`: `D1 one-page cap experiment`
- `comparison_group`: `D1_one_page_cap`
- `experiment_group`: `om_group_01`

After the capped pass finishes, record it in the ledger with the GitHub run id:

```bash
.venv/bin/python app_store_pipeline.py operating-ledger-upsert-run \
  --github-run-id RUN_ID \
  --label "D1 one-page cap experiment" \
  --comparison-group D1_one_page_cap \
  --experiment-group om_group_01 \
  --input limit=0 \
  --input target_offset=0 \
  --input experiment_group=om_group_01 \
  --input max_parallel=4 \
  --input max_pages_per_app_country=1 \
  --input pressure_ramp_mode=fixed \
  --input start_page=1 \
  --input review_limit=20 \
  --input request_delay_seconds=10 \
  --input request_delay_jitter_seconds=5 \
  --input web_429_retries=2 \
  --input web_429_retry_seconds=300 \
  --input web_429_backoff_multiplier=1.5 \
  --input web_429_retry_jitter_seconds=60 \
  --input web_time_budget_seconds=3600 \
  --input web_scope_time_budget_seconds=3600 \
  --input web_429_cooldown_minutes=0 \
  --input web_429_circuit_breaker_min_pages=4 \
  --input web_429_circuit_breaker_max_rate=0.5 \
  --input overlap_stop=enabled \
  --notes "D1 capped pass on om_group_01."
```

Then run the uncapped audit with the same settings except:

- `max_pages_per_app_country=0`
- `comparison_group`: `D1_one_page_uncapped_audit`
- `label`: `D1 one-page uncapped audit`
- `experiment_group`: `om_group_01`

Record the audit with the same command shape, replacing the label, comparison group, run id, and `max_pages_per_app_country=0`.

The operating report accepts D1 only if the audit-captured insert share is <= 5%:

```text
audit_inserted_after_cap / (cap_inserted + audit_inserted_after_cap)
```

## D2: Three-Page Cap With Uncapped Audit

Purpose: test whether a three-page cap is a better shallow refresh candidate than one page on a separate randomized 25-app group.

Run the capped pass with the same D1 command except:

- `max_pages_per_app_country=3`
- `experiment_group=om_group_02`
- `comparison_group`: `D2_three_page_cap`
- `label`: `D2 three-page cap experiment`

Then run the uncapped audit with:

- `max_pages_per_app_country=0`
- `comparison_group`: `D2_three_page_uncapped_audit`
- `label`: `D2 three-page uncapped audit`
- `experiment_group`: `om_group_02`

Record the capped pass in the ledger:

```bash
.venv/bin/python app_store_pipeline.py operating-ledger-upsert-run \
  --github-run-id RUN_ID \
  --label "D2 three-page cap experiment" \
  --comparison-group D2_three_page_cap \
  --experiment-group om_group_02 \
  --input limit=0 \
  --input target_offset=0 \
  --input experiment_group=om_group_02 \
  --input max_parallel=4 \
  --input max_pages_per_app_country=3 \
  --input pressure_ramp_mode=fixed \
  --input start_page=1 \
  --input review_limit=20 \
  --input request_delay_seconds=10 \
  --input request_delay_jitter_seconds=5 \
  --input web_429_retries=2 \
  --input web_429_retry_seconds=300 \
  --input web_429_backoff_multiplier=1.5 \
  --input web_429_retry_jitter_seconds=60 \
  --input web_time_budget_seconds=3600 \
  --input web_scope_time_budget_seconds=3600 \
  --input web_429_cooldown_minutes=0 \
  --input web_429_circuit_breaker_min_pages=4 \
  --input web_429_circuit_breaker_max_rate=0.5 \
  --input overlap_stop=enabled \
  --notes "D2 capped pass on om_group_02."
```

Record the audit with the same command shape, replacing the label with `D2 three-page uncapped audit`, the comparison group with `D2_three_page_uncapped_audit`, and `max_pages_per_app_country=0`.

The same 5% missed-insert threshold applies.

## Post-Run SQL Cross-Checks

Replace `RUN_CREATED_AT` and `RUN_UPDATED_AT` with the GitHub run timestamps.

```sql
with w as (
  select
    timestamptz 'RUN_CREATED_AT' as start_at,
    timestamptz 'RUN_UPDATED_AT' + interval '5 minutes' as end_at
)
select
  count(*) as pages,
  count(distinct app_id) as apps,
  coalesce(sum(review_count), 0) as review_rows,
  count(*) filter (where status_code = 429) as http_429,
  count(*) filter (where status_code is not null and status_code <> 200) as non_200,
  count(*) filter (where attempt_count > 1) as retried_pages
from app_store_review_pages, w
where source = 'apple_app_store_web_catalog_reviews'
  and fetched_at::timestamptz >= w.start_at
  and fetched_at::timestamptz <= w.end_at;
```

```sql
with w as (
  select
    timestamptz 'RUN_CREATED_AT' as start_at,
    timestamptz 'RUN_UPDATED_AT' + interval '5 minutes' as end_at
)
select
  count(*) as run_rows,
  coalesce(sum(reviews_inserted), 0) as inserted,
  coalesce(sum(reviews_updated), 0) as updated,
  coalesce(sum(duplicates_skipped), 0) as skipped,
  coalesce(sum(fetch_errors), 0) as fetch_errors,
  coalesce(sum(capped_scopes), 0) as capped_scopes
from app_store_runs, w
where source = 'apple_app_store_web_catalog_reviews'
  and loaded_at::timestamptz >= w.start_at
  and loaded_at::timestamptz <= w.end_at;
```

## Regenerate Evidence

After updating the ledger:

```bash
.venv/bin/python app_store_pipeline.py operating-report \
  --database-url postgresql:///app_store_reviews \
  --ledger docs/experiments/operating_model_run_ledger.json \
  --markdown-output docs/operating_limits.md \
  --json-output docs/operating_limits_summary.json
```

Validate:

```bash
.venv/bin/python -m pytest -q
git diff --check
.venv/bin/python -m json.tool docs/experiments/operating_model_run_ledger.json >/tmp/operating_model_run_ledger.validated.json
.venv/bin/python -m json.tool docs/operating_limits_summary.json >/tmp/operating_limits_summary.validated.json
```
