from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, WEB_CATALOG_SOURCE
from app_store_review_pipeline.eda import convert_json, markdown_table
from app_store_review_pipeline.postgres_database import connect_postgres, mask_database_url


DEFAULT_OPERATING_LEDGER = Path("docs/experiments/operating_model_run_ledger.json")
DEFAULT_OPERATING_MARKDOWN = Path("docs/operating_limits.md")
DEFAULT_OPERATING_JSON = Path("docs/operating_limits_summary.json")
DEFAULT_GRACE_MINUTES = 5
SCHEDULE_HOURS_UTC = (3, 15)
SCHEDULE_MINUTE_UTC = 7


def generate_operating_report(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    source: str = WEB_CATALOG_SOURCE,
    ledger_path: Path = DEFAULT_OPERATING_LEDGER,
    markdown_path: Path = DEFAULT_OPERATING_MARKDOWN,
    json_path: Path = DEFAULT_OPERATING_JSON,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> dict[str, Any]:
    ledger = load_operating_ledger(ledger_path)
    summary = build_operating_summary(
        database_url,
        source=source,
        ledger=ledger,
        ledger_path=ledger_path,
        grace_minutes=grace_minutes,
    )
    markdown = render_operating_markdown(summary)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "source": source,
        "database_url": mask_database_url(database_url),
        "ledger_path": str(ledger_path),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "generated_at": summary["metadata"]["generated_at"],
        "observed_run_count": len(summary["runs"]),
        "successful_baseline_run_count": summary["aggregate"]["successful_baseline_run_count"],
    }


def load_operating_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "runs": [], "planned_experiments": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"schema_version": 1, "runs": payload, "planned_experiments": []}
    payload.setdefault("runs", [])
    payload.setdefault("planned_experiments", [])
    return payload


def load_experiment_group_summary(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_value = ledger.get("experiment_group_manifest")
    if not manifest_value:
        return []
    manifest_path = Path(str(manifest_value))
    if not manifest_path.exists():
        return [
            {
                "group": "missing_manifest",
                "app_count": 0,
                "category_count": 0,
                "top_categories": "",
                "example_apps": "",
            }
        ]

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = []
    for group_name, group in sorted(payload.get("groups", {}).items()):
        category_counts = sorted(
            group.get("category_counts", {}).items(),
            key=lambda item: (-int(item[1]), item[0]),
        )
        top_categories = ", ".join(f"{category}:{count}" for category, count in category_counts[:4])
        example_apps = ", ".join(app.get("app_name", "") for app in group.get("apps", [])[:4])
        output.append(
            {
                "group": group_name,
                "app_count": int(group.get("app_count") or 0),
                "category_count": len(group.get("category_counts", {})),
                "top_categories": top_categories,
                "example_apps": example_apps,
            }
        )
    return output


def is_experiment_status_done(status: Any) -> bool:
    status_text = str(status or "planned")
    return status_text in {"complete", "completed", "skipped"} or status_text.startswith("completed_")


def is_source_pressure_clean_run(run: dict[str, Any]) -> bool:
    if run.get("conclusion") == "cancelled":
        return False
    page_metrics = run.get("page_metrics", {})
    load_metrics = run.get("load_metrics", {})
    page_count = int(page_metrics.get("page_count") or 0)
    if page_count <= 0:
        return False
    http_429_rate = float(page_metrics.get("http_429_rate") or 0)
    fetch_errors = int(load_metrics.get("fetch_errors") or 0)
    fetch_error_rate = round(fetch_errors / page_count, 4) if page_count else 0
    capped_scopes = int(load_metrics.get("capped_scopes") or 0)
    return http_429_rate < 0.005 and fetch_error_rate < 0.01 and capped_scopes == 0


def build_operating_summary(
    database_url: str,
    *,
    source: str,
    ledger: dict[str, Any],
    ledger_path: Path,
    grace_minutes: int,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    grace_minutes = max(0, int(grace_minutes))
    with connect_postgres(database_url) as connection:
        database_snapshot = fetch_database_snapshot(connection)
        runs = [
            enrich_run_from_postgres(connection, run, source=source, grace_minutes=grace_minutes)
            for run in ledger.get("runs", [])
        ]
        app_segments = build_app_activity_segments(connection, runs, source=source, grace_minutes=grace_minutes)

    aggregate = build_aggregate_summary(runs)
    experiment_findings = build_experiment_findings(runs, ledger.get("planned_experiments", []))
    depth_audit_findings = build_depth_audit_findings(runs, ledger.get("planned_experiments", []))
    recommendation = build_operating_recommendation(aggregate, app_segments, ledger)
    summary = {
        "metadata": {
            "generated_at": generated_at,
            "database_url": mask_database_url(database_url),
            "source": source,
            "ledger_path": str(ledger_path),
            "grace_minutes": grace_minutes,
        },
        "aggregate": aggregate,
        "runs": runs,
        "app_activity_segments": app_segments,
        "experiment_findings": experiment_findings,
        "depth_audit_findings": depth_audit_findings,
        "experiment_groups": load_experiment_group_summary(ledger),
        "database_snapshot": database_snapshot,
        "recommendation": recommendation,
        "planned_experiments": ledger.get("planned_experiments", []),
        "decision_rules": ledger.get("decision_rules", {}),
    }
    return convert_json(summary)


def build_experiment_findings(
    runs: list[dict[str, Any]],
    planned_experiments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings = []
    for experiment in planned_experiments:
        comparison_group = experiment.get("comparison_group")
        matching_runs = [run for run in runs if run.get("comparison_group") == comparison_group]
        successful_runs = [run for run in matching_runs if run.get("conclusion") == "success"]
        source_pressure_clean_runs = [run for run in matching_runs if is_source_pressure_clean_run(run)]
        page_count = sum(int(run.get("page_metrics", {}).get("page_count") or 0) for run in matching_runs)
        review_rows = sum(int(run.get("page_metrics", {}).get("review_rows") or 0) for run in matching_runs)
        inserted = sum(int(run.get("load_metrics", {}).get("reviews_inserted") or 0) for run in matching_runs)
        skipped = sum(int(run.get("load_metrics", {}).get("duplicates_skipped") or 0) for run in matching_runs)
        http_429 = sum(int(run.get("page_metrics", {}).get("http_429_pages") or 0) for run in matching_runs)
        non_200 = sum(
            int(run.get("page_metrics", {}).get("http_429_pages") or 0)
            + int(run.get("page_metrics", {}).get("other_non_200_pages") or 0)
            for run in matching_runs
        )
        fetch_errors = sum(int(run.get("load_metrics", {}).get("fetch_errors") or 0) for run in matching_runs)
        capped_scopes = sum(int(run.get("load_metrics", {}).get("capped_scopes") or 0) for run in matching_runs)
        retried_pages = sum(int(run.get("page_metrics", {}).get("retried_pages") or 0) for run in matching_runs)
        runtime_minutes = median([float(run.get("runtime_minutes") or 0) for run in matching_runs])
        duplicate_skip_rate = round(skipped / (inserted + skipped), 4) if inserted + skipped else 0
        inserted_per_page = round(inserted / page_count, 3) if page_count else 0
        http_429_rate = round(http_429 / page_count, 4) if page_count else 0
        fetch_error_rate = round(fetch_errors / page_count, 4) if page_count else 0
        status = experiment.get("status", "planned")
        finding = describe_experiment_finding(
            experiment,
            matching_runs=matching_runs,
            successful_runs=successful_runs,
            page_count=page_count,
            http_429_rate=http_429_rate,
            fetch_error_rate=fetch_error_rate,
            inserted_per_page=inserted_per_page,
            duplicate_skip_rate=duplicate_skip_rate,
        )
        findings.append(
            {
                "experiment_id": experiment.get("experiment_id"),
                "status": status,
                "comparison_group": comparison_group,
                "experiment_group": experiment.get("experiment_group")
                or experiment.get("inputs", {}).get("experiment_group", ""),
                "matching_run_count": len(matching_runs),
                "successful_run_count": len(successful_runs),
                "source_pressure_clean_run_count": len(source_pressure_clean_runs),
                "page_count": page_count,
                "review_rows": review_rows,
                "inserted": inserted,
                "skipped": skipped,
                "duplicate_skip_rate": duplicate_skip_rate,
                "inserted_per_page": inserted_per_page,
                "http_429": http_429,
                "http_429_rate": http_429_rate,
                "non_200": non_200,
                "fetch_errors": fetch_errors,
                "fetch_error_rate": fetch_error_rate,
                "retried_pages": retried_pages,
                "capped_scopes": capped_scopes,
                "median_runtime_minutes": runtime_minutes,
                "finding": finding,
            }
        )
    return findings


def build_depth_audit_findings(
    runs: list[dict[str, Any]],
    planned_experiments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for experiment in planned_experiments:
        audit_group = experiment.get("audit_comparison_group")
        if not audit_group:
            continue
        cap_group = experiment.get("comparison_group")
        cap_runs = [run for run in runs if run.get("comparison_group") == cap_group]
        audit_runs = [run for run in runs if run.get("comparison_group") == audit_group]
        cap_inserted = sum(int(run.get("load_metrics", {}).get("reviews_inserted") or 0) for run in cap_runs)
        audit_inserted = sum(int(run.get("load_metrics", {}).get("reviews_inserted") or 0) for run in audit_runs)
        cap_pages = sum(int(run.get("page_metrics", {}).get("page_count") or 0) for run in cap_runs)
        audit_pages = sum(int(run.get("page_metrics", {}).get("page_count") or 0) for run in audit_runs)
        cap_429 = sum(int(run.get("page_metrics", {}).get("http_429_pages") or 0) for run in cap_runs)
        audit_429 = sum(int(run.get("page_metrics", {}).get("http_429_pages") or 0) for run in audit_runs)
        denominator = cap_inserted + audit_inserted
        missed_insert_rate = round(audit_inserted / denominator, 4) if denominator else 0
        threshold = float(experiment.get("audit_missed_insert_threshold", 0.05))
        output.append(
            {
                "experiment_id": experiment.get("experiment_id"),
                "cap_group": cap_group,
                "audit_group": audit_group,
                "cap_run_count": len(cap_runs),
                "audit_run_count": len(audit_runs),
                "cap_pages": cap_pages,
                "audit_pages": audit_pages,
                "cap_inserted": cap_inserted,
                "audit_inserted_after_cap": audit_inserted,
                "missed_insert_rate_vs_uncapped_audit": missed_insert_rate,
                "threshold": threshold,
                "cap_http_429": cap_429,
                "audit_http_429": audit_429,
                "finding": describe_depth_audit_finding(
                    cap_runs=cap_runs,
                    audit_runs=audit_runs,
                    missed_insert_rate=missed_insert_rate,
                    threshold=threshold,
                ),
            }
        )
    return output


def describe_depth_audit_finding(
    *,
    cap_runs: list[dict[str, Any]],
    audit_runs: list[dict[str, Any]],
    missed_insert_rate: float,
    threshold: float,
) -> str:
    if not cap_runs:
        return "Pending. The capped run has not been recorded yet."
    if not audit_runs:
        return "Pending. The uncapped audit run has not been recorded yet."
    if any(run.get("conclusion") != "success" for run in cap_runs + audit_runs):
        return "Not clean. The capped run or its audit did not complete successfully."
    if missed_insert_rate <= threshold:
        return "Accepted. The cap missed no more than the configured audit threshold."
    return "Rejected. The cap missed more than the configured audit threshold."


def describe_experiment_finding(
    experiment: dict[str, Any],
    *,
    matching_runs: list[dict[str, Any]],
    successful_runs: list[dict[str, Any]],
    page_count: int,
    http_429_rate: float,
    fetch_error_rate: float,
    inserted_per_page: float,
    duplicate_skip_rate: float,
) -> str:
    experiment_id = experiment.get("experiment_id", "experiment")
    status = experiment.get("status", "planned")
    if not matching_runs:
        return "Pending. No matching run has been recorded in the ledger yet."
    if len(successful_runs) != len(matching_runs):
        if "source_clean" in str(status):
            return (
                "Source-clean but not GitHub-clean. The run passed source-pressure checks, "
                "but at least one matching job failed after ingestion."
            )
        return "Not clean. At least one matching run did not complete successfully."
    if http_429_rate >= 0.005:
        return "Not clean. HTTP 429 rate crossed the conservative stop threshold."
    if fetch_error_rate >= 0.01:
        return "Not clean. Fetch error rate crossed the conservative stop threshold."
    if is_experiment_status_done(status):
        if experiment_id == "F1":
            return (
                "Clean. The six-hour full-scope run passed source-pressure thresholds; "
                f"its marginal yield was {inserted_per_page} inserts/page with "
                f"{format_percent(duplicate_skip_rate)} duplicate skips."
            )
        return "Clean. The completed experiment passed the current source-pressure thresholds."
    return "In progress or pending completion. Existing matching runs are clean, but the experiment is not marked complete."


def enrich_run_from_postgres(connection: Any, run: dict[str, Any], *, source: str, grace_minutes: int) -> dict[str, Any]:
    created_at = parse_utc(run.get("created_at"))
    updated_at = parse_utc(run.get("updated_at")) or created_at
    if created_at is None:
        output = dict(run)
        output["metrics_error"] = "missing_created_at"
        return output
    window_end = updated_at + timedelta(minutes=grace_minutes)
    page_metrics = fetch_one(
        connection,
        """
        SELECT
            COUNT(*)::bigint AS page_count,
            COUNT(DISTINCT app_id)::bigint AS app_count,
            COALESCE(SUM(review_count), 0)::bigint AS review_rows,
            COALESCE(SUM(unique_review_count), 0)::bigint AS unique_review_rows,
            COALESCE(SUM(duplicate_count), 0)::bigint AS page_duplicate_rows,
            COUNT(*) FILTER (WHERE status = 'ok')::bigint AS ok_pages,
            COUNT(*) FILTER (WHERE status = 'error')::bigint AS error_pages,
            COUNT(*) FILTER (WHERE status_code = 200)::bigint AS http_200_pages,
            COUNT(*) FILTER (WHERE status_code = 429)::bigint AS http_429_pages,
            COUNT(*) FILTER (
                WHERE status_code IS NOT NULL AND status_code <> 200 AND status_code <> 429
            )::bigint AS other_non_200_pages,
            COUNT(*) FILTER (WHERE status_code IS NULL)::bigint AS null_status_pages,
            COUNT(*) FILTER (WHERE attempt_count > 1)::bigint AS retried_pages,
            COALESCE(MAX(attempt_count), 0)::bigint AS max_attempt_count,
            COUNT(*) FILTER (WHERE terminal_reason IS NOT NULL AND terminal_reason <> '')::bigint AS terminal_pages,
            MIN(fetched_at::timestamptz) AS first_page_at,
            MAX(fetched_at::timestamptz) AS last_page_at
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at::timestamptz >= %s
            AND fetched_at::timestamptz <= %s
        """,
        (source, created_at, window_end),
    )
    load_metrics = fetch_one(
        connection,
        """
        SELECT
            COUNT(*)::bigint AS run_rows,
            COALESCE(SUM(page_count), 0)::bigint AS loaded_pages,
            COALESCE(SUM(review_count), 0)::bigint AS loaded_review_rows,
            COALESCE(SUM(reviews_inserted), 0)::bigint AS reviews_inserted,
            COALESCE(SUM(reviews_updated), 0)::bigint AS reviews_updated,
            COALESCE(SUM(duplicates_skipped), 0)::bigint AS duplicates_skipped,
            COALESCE(SUM(fetch_errors), 0)::bigint AS fetch_errors,
            COALESCE(SUM(capped_scopes), 0)::bigint AS capped_scopes
        FROM app_store_runs
        WHERE source = %s
            AND loaded_at::timestamptz >= %s
            AND loaded_at::timestamptz <= %s
        """,
        (source, created_at, window_end),
    )
    attempt_counts = fetch_all(
        connection,
        """
        SELECT attempt_count, COUNT(*)::bigint AS page_count
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at::timestamptz >= %s
            AND fetched_at::timestamptz <= %s
        GROUP BY attempt_count
        ORDER BY attempt_count
        """,
        (source, created_at, window_end),
    )
    terminal_reasons = fetch_all(
        connection,
        """
        SELECT COALESCE(NULLIF(terminal_reason, ''), 'none') AS terminal_reason, COUNT(*)::bigint AS page_count
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at::timestamptz >= %s
            AND fetched_at::timestamptz <= %s
        GROUP BY COALESCE(NULLIF(terminal_reason, ''), 'none')
        ORDER BY page_count DESC, terminal_reason
        LIMIT 12
        """,
        (source, created_at, window_end),
    )
    long_tail_apps = fetch_all(
        connection,
        """
        SELECT
            app_id,
            MAX(app_name) AS app_name,
            COUNT(*)::bigint AS page_count,
            COALESCE(SUM(review_count), 0)::bigint AS review_rows,
            COALESCE(SUM(overlap_review_count), 0)::bigint AS overlap_rows,
            COUNT(*) FILTER (WHERE attempt_count > 1)::bigint AS retried_pages,
            COUNT(*) FILTER (WHERE status_code = 429)::bigint AS http_429_pages,
            MAX(page_number)::bigint AS max_page_number,
            MAX(COALESCE(NULLIF(terminal_reason, ''), 'none')) AS terminal_reason
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at::timestamptz >= %s
            AND fetched_at::timestamptz <= %s
        GROUP BY app_id
        ORDER BY page_count DESC, review_rows DESC, app_name
        LIMIT 15
        """,
        (source, created_at, window_end),
    )
    output = dict(run)
    output["created_at"] = created_at.isoformat().replace("+00:00", "Z")
    output["updated_at"] = updated_at.isoformat().replace("+00:00", "Z")
    output["runtime_minutes"] = round((updated_at - created_at).total_seconds() / 60.0, 2)
    output["schedule_delay_minutes"] = schedule_delay_minutes(created_at) if run.get("event") == "schedule" else None
    output["page_metrics"] = add_run_rates(page_metrics, load_metrics)
    output["load_metrics"] = load_metrics
    output["attempt_counts"] = attempt_counts
    output["terminal_reasons"] = terminal_reasons
    output["long_tail_apps"] = long_tail_apps
    return output


def add_run_rates(page_metrics: dict[str, Any], load_metrics: dict[str, Any]) -> dict[str, Any]:
    output = dict(page_metrics)
    page_count = int(output.get("page_count") or 0)
    review_rows = int(output.get("review_rows") or 0)
    inserted = int(load_metrics.get("reviews_inserted") or 0)
    updated = int(load_metrics.get("reviews_updated") or 0)
    skipped = int(load_metrics.get("duplicates_skipped") or 0)
    observed_rows = inserted + updated + skipped
    output["http_429_rate"] = round(int(output.get("http_429_pages") or 0) / page_count, 4) if page_count else 0
    output["non_200_rate"] = (
        round((int(output.get("http_429_pages") or 0) + int(output.get("other_non_200_pages") or 0)) / page_count, 4)
        if page_count
        else 0
    )
    output["retried_page_rate"] = round(int(output.get("retried_pages") or 0) / page_count, 4) if page_count else 0
    output["duplicate_skip_rate"] = round(skipped / observed_rows, 4) if observed_rows else 0
    output["inserted_per_page"] = round(inserted / page_count, 3) if page_count else 0
    output["inserted_per_fetched_row"] = round(inserted / review_rows, 4) if review_rows else 0
    first_page_at = parse_utc(output.get("first_page_at"))
    last_page_at = parse_utc(output.get("last_page_at"))
    output["page_window_minutes"] = (
        round((last_page_at - first_page_at).total_seconds() / 60.0, 2)
        if first_page_at and last_page_at
        else None
    )
    return output


def build_aggregate_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    observed_runs = [run for run in runs if int(run.get("page_metrics", {}).get("page_count") or 0) > 0]
    successful_runs = [run for run in observed_runs if run.get("conclusion") == "success"]
    source_pressure_clean_runs = [run for run in observed_runs if is_source_pressure_clean_run(run)]
    baseline_runs = [
        run
        for run in successful_runs
        if str(run.get("comparison_group") or "").startswith("F0")
        or str(run.get("comparison_group") or "") in {"D3_uncapped_control", "manual_fallback_full_scope"}
    ]
    source_pressure_pages = sum(int(run.get("page_metrics", {}).get("page_count") or 0) for run in successful_runs)
    source_pressure_429 = sum(int(run.get("page_metrics", {}).get("http_429_pages") or 0) for run in successful_runs)
    attempts = merge_count_rows(successful_runs, "attempt_counts", "attempt_count")
    terminal_reasons = merge_count_rows(successful_runs, "terminal_reasons", "terminal_reason")
    return {
        "observed_run_count": len(observed_runs),
        "successful_run_count": len(successful_runs),
        "failed_or_cancelled_run_count": len(runs) - len(successful_runs),
        "successful_baseline_run_count": len(baseline_runs),
        "source_pressure_clean_run_count": len(source_pressure_clean_runs),
        "source_pressure_clean_pages": sum(
            int(run.get("page_metrics", {}).get("page_count") or 0) for run in source_pressure_clean_runs
        ),
        "source_pressure_clean_http_429_rate": round(
            sum(int(run.get("page_metrics", {}).get("http_429_pages") or 0) for run in source_pressure_clean_runs)
            / sum(int(run.get("page_metrics", {}).get("page_count") or 0) for run in source_pressure_clean_runs),
            4,
        )
        if source_pressure_clean_runs
        else 0,
        "source_pressure_clean_review_rows": sum(
            int(run.get("page_metrics", {}).get("review_rows") or 0) for run in source_pressure_clean_runs
        ),
        "source_pressure_clean_reviews_inserted": sum(
            int(run.get("load_metrics", {}).get("reviews_inserted") or 0) for run in source_pressure_clean_runs
        ),
        "source_pressure_clean_duplicates_skipped": sum(
            int(run.get("load_metrics", {}).get("duplicates_skipped") or 0) for run in source_pressure_clean_runs
        ),
        "successful_pages": source_pressure_pages,
        "successful_http_429_pages": source_pressure_429,
        "successful_http_429_rate": round(source_pressure_429 / source_pressure_pages, 4) if source_pressure_pages else 0,
        "successful_retried_pages": sum(int(run.get("page_metrics", {}).get("retried_pages") or 0) for run in successful_runs),
        "successful_fetch_errors": sum(int(run.get("load_metrics", {}).get("fetch_errors") or 0) for run in successful_runs),
        "successful_capped_scopes": sum(int(run.get("load_metrics", {}).get("capped_scopes") or 0) for run in successful_runs),
        "successful_review_rows": sum(int(run.get("page_metrics", {}).get("review_rows") or 0) for run in successful_runs),
        "successful_reviews_inserted": sum(int(run.get("load_metrics", {}).get("reviews_inserted") or 0) for run in successful_runs),
        "successful_duplicates_skipped": sum(int(run.get("load_metrics", {}).get("duplicates_skipped") or 0) for run in successful_runs),
        "median_successful_runtime_minutes": median([float(run.get("runtime_minutes") or 0) for run in successful_runs]),
        "median_successful_pages": median([int(run.get("page_metrics", {}).get("page_count") or 0) for run in successful_runs]),
        "median_successful_inserted_per_page": median(
            [float(run.get("page_metrics", {}).get("inserted_per_page") or 0) for run in successful_runs]
        ),
        "max_schedule_delay_minutes": max(
            [float(run.get("schedule_delay_minutes") or 0) for run in runs if run.get("schedule_delay_minutes") is not None]
            or [0]
        ),
        "attempt_distribution": attempts,
        "terminal_reason_distribution": terminal_reasons,
    }


def build_app_activity_segments(
    connection: Any,
    runs: list[dict[str, Any]],
    *,
    source: str,
    grace_minutes: int,
) -> dict[str, Any]:
    successful_runs = [
        run
        for run in runs
        if run.get("conclusion") == "success"
        and parse_utc(run.get("created_at")) is not None
        and parse_utc(run.get("updated_at")) is not None
    ]
    if not successful_runs:
        return {"segments": [], "top_apps": []}

    app_rows: dict[str, dict[str, Any]] = {}
    for run in successful_runs:
        start = parse_utc(run.get("created_at"))
        end = parse_utc(run.get("updated_at")) + timedelta(minutes=grace_minutes)
        pages_by_app = fetch_all(
            connection,
            """
            SELECT
                p.app_id,
                MAX(p.app_name) AS app_name,
                MAX(COALESCE(NULLIF(t.category, ''), 'unknown')) AS category,
                COUNT(*)::bigint AS page_count,
                COALESCE(SUM(p.review_count), 0)::bigint AS review_rows
            FROM app_store_review_pages p
            LEFT JOIN app_store_targets t ON t.app_id = p.app_id
            WHERE p.source = %s
                AND p.fetched_at::timestamptz >= %s
                AND p.fetched_at::timestamptz <= %s
            GROUP BY p.app_id
            """,
            (source, start, end),
        )
        changes_by_app = {
            row["app_id"]: row
            for row in fetch_all(
                connection,
                """
                SELECT
                    app_id,
                    COUNT(*) FILTER (WHERE change_type = 'inserted')::bigint AS inserted,
                    COUNT(*) FILTER (WHERE change_type = 'updated')::bigint AS updated
                FROM app_store_review_changes
                WHERE changed_at::timestamptz >= %s
                    AND changed_at::timestamptz <= %s
                GROUP BY app_id
                """,
                (start, end),
            )
        }
        for row in pages_by_app:
            app_id = row["app_id"]
            output = app_rows.setdefault(
                app_id,
                {
                    "app_id": app_id,
                    "app_name": row.get("app_name") or app_id,
                    "category": row.get("category") or "unknown",
                    "page_count": 0,
                    "review_rows": 0,
                    "inserted": 0,
                    "updated": 0,
                    "observed_runs": 0,
                },
            )
            output["page_count"] += int(row.get("page_count") or 0)
            output["review_rows"] += int(row.get("review_rows") or 0)
            output["inserted"] += int(changes_by_app.get(app_id, {}).get("inserted") or 0)
            output["updated"] += int(changes_by_app.get(app_id, {}).get("updated") or 0)
            output["observed_runs"] += 1

    apps = sorted(app_rows.values(), key=lambda row: (row["inserted"], row["page_count"], row["review_rows"]), reverse=True)
    if not apps:
        return {"segments": [], "top_apps": []}
    total_inserted = sum(int(row["inserted"]) for row in apps)
    total_pages = sum(int(row["page_count"]) for row in apps)
    n = len(apps)
    high_cut = max(1, n // 4)
    low_cut = max(1, n // 4)
    for index, row in enumerate(apps):
        if index < high_cut:
            row["activity_segment"] = "high"
        elif index >= n - low_cut:
            row["activity_segment"] = "low"
        else:
            row["activity_segment"] = "normal"
    segments = []
    for segment in ("high", "normal", "low"):
        rows = [row for row in apps if row["activity_segment"] == segment]
        pages = sum(int(row["page_count"]) for row in rows)
        inserted = sum(int(row["inserted"]) for row in rows)
        segments.append(
            {
                "segment": segment,
                "app_count": len(rows),
                "page_count": pages,
                "inserted": inserted,
                "page_share": round(pages / total_pages, 4) if total_pages else 0,
                "insert_share": round(inserted / total_inserted, 4) if total_inserted else 0,
                "inserted_per_page": round(inserted / pages, 3) if pages else 0,
            }
        )
    return {"segments": segments, "top_apps": apps[:25]}


def build_operating_recommendation(
    aggregate: dict[str, Any],
    app_segments: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    pending_experiments = [
        row.get("experiment_id")
        for row in ledger.get("planned_experiments", [])
        if not is_experiment_status_done(row.get("status", "planned"))
    ]
    high_segment = next((row for row in app_segments.get("segments", []) if row.get("segment") == "high"), {})
    source_pressure_clean = float(aggregate.get("successful_http_429_rate") or 0) == 0
    enough_success = int(aggregate.get("successful_baseline_run_count") or 0) >= 2
    return {
        "current_recommendation": "Keep the twice-daily full-scope incremental schedule as the production baseline while remaining controlled tests are completed.",
        "confidence": "interim" if pending_experiments else "ready_for_review",
        "why": [
            "Recent successful full-scope runs show clean source-pressure metrics." if source_pressure_clean else "Recent runs include source-pressure signals that need review.",
            "There are enough successful baseline observations to compare against experiments." if enough_success else "More baseline observations are needed before final recommendation.",
            (
                f"High-activity apps account for {format_percent(high_segment.get('insert_share'))} of recent inserts "
                f"and {format_percent(high_segment.get('page_share'))} of recent pages."
                if high_segment
                else "Hybrid segmentation is pending more successful run observations."
            ),
        ],
        "pending_experiments": pending_experiments,
        "stop_thresholds": {
            "http_429_rate": 0.005,
            "fetch_error_rate": 0.01,
            "missed_insert_rate_for_capped_runs": 0.05,
        },
    }


def fetch_database_snapshot(connection: Any) -> list[dict[str, Any]]:
    return fetch_all(
        connection,
        """
        SELECT 'app_store_reviews' AS table_name,
            COUNT(*)::bigint AS row_count,
            pg_total_relation_size('app_store_reviews')::bigint AS total_bytes,
            pg_size_pretty(pg_total_relation_size('app_store_reviews')) AS total_size
        FROM app_store_reviews
        UNION ALL
        SELECT 'app_store_review_pages' AS table_name,
            COUNT(*)::bigint AS row_count,
            pg_total_relation_size('app_store_review_pages')::bigint AS total_bytes,
            pg_size_pretty(pg_total_relation_size('app_store_review_pages')) AS total_size
        FROM app_store_review_pages
        UNION ALL
        SELECT 'app_store_runs' AS table_name,
            COUNT(*)::bigint AS row_count,
            pg_total_relation_size('app_store_runs')::bigint AS total_bytes,
            pg_size_pretty(pg_total_relation_size('app_store_runs')) AS total_size
        FROM app_store_runs
        UNION ALL
        SELECT 'app_store_review_changes' AS table_name,
            COUNT(*)::bigint AS row_count,
            pg_total_relation_size('app_store_review_changes')::bigint AS total_bytes,
            pg_size_pretty(pg_total_relation_size('app_store_review_changes')) AS total_size
        FROM app_store_review_changes
        ORDER BY table_name
        """,
    )


def render_operating_markdown(summary: dict[str, Any]) -> str:
    metadata = summary["metadata"]
    aggregate = summary["aggregate"]
    recommendation = summary["recommendation"]
    run_rows = [run_table_row(run) for run in summary["runs"]]
    lines = [
        "# Apple Review Pipeline Operating Limits",
        "",
        f"Generated at: `{metadata['generated_at']}`",
        f"Database: `{metadata['database_url']}`",
        f"Source: `{metadata['source']}`",
        f"Ledger: `{metadata['ledger_path']}`",
        "",
        "## Recommendation",
        "",
        recommendation["current_recommendation"],
        "",
        "Evidence status: "
        f"**{recommendation['confidence']}**. Pending controlled experiments: "
        f"{', '.join(recommendation['pending_experiments']) if recommendation['pending_experiments'] else 'none'}.",
        "",
        "Rationale:",
        *[f"- {item}" for item in recommendation["why"]],
        "",
        "## Experiment Target Groups",
        "",
        "Strategy comparisons use fixed randomized 25-app groups instead of running every strategy on all 200 apps. This keeps each experiment fast and prevents one strategy test from consuming the incremental-review signal needed by the next strategy test.",
        "",
        markdown_table(
            summary.get("experiment_groups", []),
            ["group", "app_count", "category_count", "top_categories", "example_apps"],
        ),
        "",
        "## Controlled Experiment Findings",
        "",
        markdown_table(
            summary.get("experiment_findings", []),
            [
                "experiment_id",
                "status",
                "experiment_group",
                "matching_run_count",
                "successful_run_count",
                "source_pressure_clean_run_count",
                "page_count",
                "review_rows",
                "inserted",
                "skipped",
                "duplicate_skip_rate",
                "inserted_per_page",
                "http_429",
                "non_200",
                "fetch_errors",
                "retried_pages",
                "median_runtime_minutes",
                "finding",
            ],
        ),
        "",
        "Interpretation:",
        "- Frequency tests (F1/F2) measure whether shorter gaps add useful fresh rows without increasing source pressure.",
        "- `successful_run_count` is GitHub-clean; `source_pressure_clean_run_count` is source-pressure clean and can include post-ingestion artifact-only failures.",
        "- Depth tests (D1/D2) use randomized 25-app groups and measure whether page caps miss more than 5% of rows later captured by a same-group uncapped audit.",
        "- A final recommendation should wait for the pending tests unless source-pressure thresholds stop the ladder early.",
        "",
        "### Depth Audit Comparisons",
        "",
        markdown_table(
            summary.get("depth_audit_findings", []),
            [
                "experiment_id",
                "cap_run_count",
                "audit_run_count",
                "cap_pages",
                "audit_pages",
                "cap_inserted",
                "audit_inserted_after_cap",
                "missed_insert_rate_vs_uncapped_audit",
                "threshold",
                "cap_http_429",
                "audit_http_429",
                "finding",
            ],
        ),
        "",
        "## Aggregate Observations",
        "",
        markdown_table(
            [aggregate],
            [
                "observed_run_count",
                "successful_run_count",
                "source_pressure_clean_run_count",
                "source_pressure_clean_pages",
                "source_pressure_clean_review_rows",
                "source_pressure_clean_reviews_inserted",
                "source_pressure_clean_duplicates_skipped",
                "source_pressure_clean_http_429_rate",
                "failed_or_cancelled_run_count",
                "successful_pages",
                "successful_review_rows",
                "successful_reviews_inserted",
                "successful_duplicates_skipped",
                "successful_http_429_rate",
                "successful_retried_pages",
                "successful_fetch_errors",
                "successful_capped_scopes",
                "median_successful_runtime_minutes",
                "median_successful_pages",
                "median_successful_inserted_per_page",
                "max_schedule_delay_minutes",
            ],
        ),
        "",
        "### Successful Run Attempt Distribution",
        "",
        markdown_table(aggregate.get("attempt_distribution", []), ["attempt_count", "page_count"]),
        "",
        "### Successful Run Terminal Reasons",
        "",
        markdown_table(aggregate.get("terminal_reason_distribution", []), ["terminal_reason", "page_count"]),
        "",
        "## Observed Runs",
        "",
        markdown_table(
            run_rows,
            [
                "github_run_id",
                "label",
                "experiment_group",
                "event",
                "conclusion",
                "runtime_minutes",
                "schedule_delay_minutes",
                "job_result",
                "apps",
                "pages",
                "review_rows",
                "inserted",
                "updated",
                "skipped",
                "duplicate_skip_rate",
                "http_429",
                "non_200",
                "fetch_errors",
                "capped_scopes",
            ],
        ),
        "",
        "## Activity Segments",
        "",
        "Segments are computed from successful ledger runs by app-level inserted rows and page load.",
        "",
        markdown_table(
            summary["app_activity_segments"].get("segments", []),
            ["segment", "app_count", "page_count", "inserted", "page_share", "insert_share", "inserted_per_page"],
        ),
        "",
        "### Top Recent Activity Apps",
        "",
        markdown_table(
            summary["app_activity_segments"].get("top_apps", [])[:15],
            ["app_name", "category", "activity_segment", "page_count", "review_rows", "inserted", "updated", "observed_runs"],
        ),
        "",
        "## Database Footprint",
        "",
        markdown_table(summary["database_snapshot"], ["table_name", "row_count", "total_size", "total_bytes"]),
        "",
        "## Planned Controlled Tests",
        "",
        markdown_table(
            summary.get("planned_experiments", []),
            [
                "experiment_id",
                "status",
                "comparison_group",
                "experiment_group",
                "description",
                "success_criteria",
            ],
        ),
        "",
        "## Operating Decision Rules",
        "",
        "- Keep twice-daily full-scope incremental if shorter-frequency runs stay clean but have low marginal inserts per page, or if capped runs miss more than 5% of audit-captured rows.",
        "- Recommend higher-frequency shallow refresh only if source pressure remains clean and capped runs miss no more than 5% of audit-captured rows.",
        "- Recommend hybrid refresh only if high-activity apps account for most new rows and can be refreshed with fewer total pages than full-scope high-frequency runs.",
        "",
        "## Notes",
        "",
        "- GitHub schedule delay is tracked separately from ingestion reliability.",
        "- `app_store_runs` is per app job, so GitHub workflow run metrics are joined by ledger time window.",
        "- Historical backfill remains paused while this operating-model test is active.",
        "",
    ]
    return "\n".join(lines)


def run_table_row(run: dict[str, Any]) -> dict[str, Any]:
    page = run.get("page_metrics", {})
    load = run.get("load_metrics", {})
    return {
        "github_run_id": run.get("github_run_id"),
        "label": run.get("label"),
        "experiment_group": run.get("inputs", {}).get("experiment_group", ""),
        "event": run.get("event"),
        "conclusion": run.get("conclusion"),
        "runtime_minutes": run.get("runtime_minutes"),
        "schedule_delay_minutes": run.get("schedule_delay_minutes"),
        "job_result": f"{run.get('job_success', '')}/{run.get('job_total', '')}",
        "apps": page.get("app_count"),
        "pages": page.get("page_count"),
        "review_rows": page.get("review_rows"),
        "inserted": load.get("reviews_inserted"),
        "updated": load.get("reviews_updated"),
        "skipped": load.get("duplicates_skipped"),
        "duplicate_skip_rate": page.get("duplicate_skip_rate"),
        "http_429": page.get("http_429_pages"),
        "non_200": int(page.get("http_429_pages") or 0) + int(page.get("other_non_200_pages") or 0),
        "fetch_errors": load.get("fetch_errors"),
        "capped_scopes": load.get("capped_scopes"),
    }


def fetch_all(connection: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def fetch_one(connection: Any, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    row = connection.execute(query, params).fetchone()
    return dict(row or {})


def merge_count_rows(runs: list[dict[str, Any]], row_key: str, group_key: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        for row in run.get(row_key, []):
            key = str(row.get(group_key) if row.get(group_key) is not None else "unknown")
            counts[key] += int(row.get("page_count") or 0)
    return [
        {group_key: key, "page_count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def schedule_delay_minutes(created_at: datetime) -> float:
    expected = previous_expected_schedule(created_at)
    return round((created_at - expected).total_seconds() / 60.0, 2)


def previous_expected_schedule(created_at: datetime) -> datetime:
    created_at = created_at.astimezone(timezone.utc)
    candidates = []
    for day_offset in (0, 1):
        day = created_at.date() - timedelta(days=day_offset)
        for hour in SCHEDULE_HOURS_UTC:
            candidate = datetime(day.year, day.month, day.day, hour, SCHEDULE_MINUTE_UTC, tzinfo=timezone.utc)
            if candidate <= created_at:
                candidates.append(candidate)
    return max(candidates)


def median(values: list[float | int]) -> float:
    cleaned = sorted(float(value) for value in values if value is not None)
    if not cleaned:
        return 0
    midpoint = len(cleaned) // 2
    if len(cleaned) % 2:
        return round(cleaned[midpoint], 3)
    return round((cleaned[midpoint - 1] + cleaned[midpoint]) / 2, 3)


def format_percent(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"
