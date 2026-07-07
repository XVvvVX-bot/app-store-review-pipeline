from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, WEB_CATALOG_SORT_BY, WEB_CATALOG_SOURCE
from app_store_review_pipeline.eda import convert_json, markdown_table
from app_store_review_pipeline.operating import fetch_all, fetch_one, parse_utc
from app_store_review_pipeline.postgres_database import connect_postgres, mask_database_url


DEFAULT_MONITORING_MARKDOWN = Path("data/reports/monitoring/current_run_health.md")
DEFAULT_MONITORING_JSON = Path("data/reports/monitoring/current_run_health.json")
BACKLOG_TERMINAL_REASONS = {
    "fetch_error",
    "page_cap",
    "time_budget_exceeded",
    "scope_time_budget_exceeded",
    "time_budget_retry_window_exceeded",
    "scope_time_budget_retry_window_exceeded",
    "sparse_fetch_error_threshold",
    "empty_page_before_overlap",
    "empty_page_after_sparse_scan",
}
SUCCESS_TERMINAL_REASONS = {
    "caught_up_to_existing_reviews",
    "no_next_href",
    "target_review_count_reached",
    "target_review_count_zero",
}


def generate_monitoring_report(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    source: str = WEB_CATALOG_SOURCE,
    since: str,
    selected_count: int,
    workflow_result: str,
    github_run_id: str = "",
    github_run_url: str = "",
    github_jobs_json: Path | None = None,
    github_runs_json: Path | None = None,
    markdown_path: Path = DEFAULT_MONITORING_MARKDOWN,
    json_path: Path = DEFAULT_MONITORING_JSON,
    fail_on: str = "failing",
    require_recent_scheduled_run: bool = False,
    schedule_lookback_minutes: int = 180,
) -> dict[str, Any]:
    jobs_payload = load_json_path(github_jobs_json)
    runs_payload = load_json_path(github_runs_json)
    summary = build_monitoring_summary(
        database_url,
        source=source,
        since=since,
        selected_count=selected_count,
        workflow_result=workflow_result,
        github_run_id=github_run_id,
        github_run_url=github_run_url,
        jobs_payload=jobs_payload,
        runs_payload=runs_payload,
        require_recent_scheduled_run=require_recent_scheduled_run,
        schedule_lookback_minutes=schedule_lookback_minutes,
    )
    markdown = render_monitoring_markdown(summary)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(convert_json(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": summary["status"],
        "database_url": mask_database_url(database_url),
        "source": source,
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "generated_at": summary["metadata"]["generated_at"],
        "exit_code": monitor_exit_code(summary["status"], fail_on),
    }


def load_json_path(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def monitor_exit_code(status: str, fail_on: str) -> int:
    fail_on = str(fail_on or "failing").lower()
    if fail_on == "never":
        return 0
    if fail_on == "degraded":
        return 1 if status in {"degraded", "failing"} else 0
    return 1 if status == "failing" else 0


def build_monitoring_summary(
    database_url: str,
    *,
    source: str,
    since: str,
    selected_count: int,
    workflow_result: str,
    github_run_id: str,
    github_run_url: str,
    jobs_payload: Any,
    runs_payload: Any,
    require_recent_scheduled_run: bool,
    schedule_lookback_minutes: int,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    since_dt = parse_utc(since)
    if since_dt is None:
        raise ValueError(f"Invalid --since timestamp: {since!r}")

    with connect_postgres(database_url) as connection:
        run_metrics = fetch_run_metrics(connection, source=source, since=since_dt)
        app_metrics = fetch_app_metrics(connection, source=source, since=since_dt)
        stale_apps = fetch_stale_apps(connection, source=source, generated_at=generated_at)
        database_snapshot = fetch_database_snapshot(connection)
        history = fetch_recent_history(connection, source=source, before=since_dt)

    github = summarize_github_payloads(
        jobs_payload=jobs_payload,
        runs_payload=runs_payload,
        workflow_result=workflow_result,
        generated_at=generated_at,
        schedule_lookback_minutes=schedule_lookback_minutes,
    )
    alerts = evaluate_alerts(
        run_metrics=run_metrics,
        app_metrics=app_metrics,
        stale_apps=stale_apps,
        history=history,
        github=github,
        selected_count=selected_count,
        workflow_result=workflow_result,
        require_recent_scheduled_run=require_recent_scheduled_run,
    )
    status = overall_status(alerts)
    return convert_json(
        {
            "metadata": {
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "database_url": mask_database_url(database_url),
                "source": source,
                "since": since_dt.isoformat().replace("+00:00", "Z"),
                "selected_count": int(selected_count),
                "workflow_result": workflow_result,
                "github_run_id": str(github_run_id or ""),
                "github_run_url": str(github_run_url or ""),
            },
            "status": status,
            "alerts": alerts,
            "github": github,
            "run_metrics": run_metrics,
            "app_metrics": app_metrics,
            "stale_apps": stale_apps,
            "history": history,
            "database_snapshot": database_snapshot,
        }
    )


def fetch_run_metrics(connection: Any, *, source: str, since: datetime) -> dict[str, Any]:
    page = fetch_one(
        connection,
        """
        SELECT
            COUNT(*)::bigint AS page_count,
            COUNT(DISTINCT app_id)::bigint AS app_count,
            COALESCE(SUM(review_count), 0)::bigint AS review_rows,
            COALESCE(SUM(unique_review_count), 0)::bigint AS unique_review_rows,
            COALESCE(SUM(overlap_review_count), 0)::bigint AS overlap_rows,
            COUNT(*) FILTER (WHERE status_code = 200)::bigint AS http_200_pages,
            COUNT(*) FILTER (WHERE status_code = 429)::bigint AS http_429_pages,
            COUNT(*) FILTER (
                WHERE status_code IS NOT NULL AND status_code <> 200 AND status_code <> 429
            )::bigint AS other_non_200_pages,
            COUNT(*) FILTER (WHERE attempt_count > 1)::bigint AS retried_pages,
            COALESCE(MAX(attempt_count), 0)::bigint AS max_attempt_count,
            COUNT(*) FILTER (WHERE terminal_reason = ANY(%s))::bigint AS backlog_terminal_pages,
            COUNT(*) FILTER (WHERE terminal_reason = ANY(%s))::bigint AS success_terminal_pages,
            MIN(fetched_at::timestamptz) AS first_page_at,
            MAX(fetched_at::timestamptz) AS last_page_at
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at IS NOT NULL
            AND fetched_at::timestamptz >= %s
        """,
        (list(BACKLOG_TERMINAL_REASONS), list(SUCCESS_TERMINAL_REASONS), source, since),
    )
    load = fetch_one(
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
        """,
        (source, since),
    )
    terminal_reasons = fetch_all(
        connection,
        """
        SELECT COALESCE(NULLIF(terminal_reason, ''), 'none') AS terminal_reason,
            COUNT(*)::bigint AS page_count
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at IS NOT NULL
            AND fetched_at::timestamptz >= %s
        GROUP BY COALESCE(NULLIF(terminal_reason, ''), 'none')
        ORDER BY page_count DESC, terminal_reason
        LIMIT 12
        """,
        (source, since),
    )
    attempt_counts = fetch_all(
        connection,
        """
        SELECT attempt_count, COUNT(*)::bigint AS page_count
        FROM app_store_review_pages
        WHERE source = %s
            AND fetched_at IS NOT NULL
            AND fetched_at::timestamptz >= %s
        GROUP BY attempt_count
        ORDER BY attempt_count
        """,
        (source, since),
    )
    output = {**page, **load}
    page_count = int(output.get("page_count") or 0)
    review_rows = int(output.get("review_rows") or 0)
    inserted = int(output.get("reviews_inserted") or 0)
    updated = int(output.get("reviews_updated") or 0)
    skipped = int(output.get("duplicates_skipped") or 0)
    observed_rows = inserted + updated + skipped
    output["http_429_rate"] = round(int(output.get("http_429_pages") or 0) / page_count, 4) if page_count else 0
    output["non_200_rate"] = (
        round((int(output.get("http_429_pages") or 0) + int(output.get("other_non_200_pages") or 0)) / page_count, 4)
        if page_count
        else 0
    )
    output["retry_rate"] = round(int(output.get("retried_pages") or 0) / page_count, 4) if page_count else 0
    output["fetch_error_rate"] = round(int(output.get("fetch_errors") or 0) / page_count, 4) if page_count else 0
    output["backlog_terminal_rate"] = round(int(output.get("backlog_terminal_pages") or 0) / page_count, 4) if page_count else 0
    output["duplicate_rate"] = round(skipped / observed_rows, 4) if observed_rows else 0
    output["inserted_per_page"] = round(inserted / page_count, 3) if page_count else 0
    output["inserted_per_row"] = round(inserted / review_rows, 4) if review_rows else 0
    first_page_at = parse_utc(output.get("first_page_at"))
    last_page_at = parse_utc(output.get("last_page_at"))
    output["runtime_minutes"] = (
        round((last_page_at - first_page_at).total_seconds() / 60.0, 2)
        if first_page_at and last_page_at and last_page_at >= first_page_at
        else 0
    )
    output["terminal_reasons"] = terminal_reasons
    output["attempt_counts"] = attempt_counts
    return output


def fetch_app_metrics(connection: Any, *, source: str, since: datetime) -> dict[str, Any]:
    long_tail = fetch_all(
        connection,
        """
        SELECT
            p.app_id,
            MAX(p.app_name) AS app_name,
            COUNT(*)::bigint AS page_count,
            COALESCE(SUM(p.review_count), 0)::bigint AS review_rows,
            COALESCE(SUM(p.overlap_review_count), 0)::bigint AS overlap_rows,
            COUNT(*) FILTER (WHERE p.status_code = 429)::bigint AS http_429_pages,
            COUNT(*) FILTER (WHERE p.attempt_count > 1)::bigint AS retried_pages,
            MAX(p.page_number)::bigint AS max_page_number,
            MAX(COALESCE(NULLIF(p.terminal_reason, ''), 'none')) AS terminal_reason
        FROM app_store_review_pages p
        WHERE p.source = %s
            AND p.fetched_at IS NOT NULL
            AND p.fetched_at::timestamptz >= %s
        GROUP BY p.app_id
        ORDER BY page_count DESC, review_rows DESC, app_name
        LIMIT 15
        """,
        (source, since),
    )
    top_inserted = fetch_all(
        connection,
        """
        WITH change_counts AS (
            SELECT
                c.app_id,
                MAX(r.app_name) AS app_name,
                COUNT(*) FILTER (WHERE c.change_type = 'inserted')::bigint AS inserted,
                COUNT(*) FILTER (WHERE c.change_type = 'updated')::bigint AS updated
            FROM app_store_review_changes c
            JOIN app_store_reviews r
                ON r.review_key = c.review_key
            WHERE r.source = %s
                AND c.changed_at::timestamptz >= %s
            GROUP BY c.app_id
        ),
        page_counts AS (
            SELECT
                app_id,
                COUNT(*)::bigint AS page_count
            FROM app_store_review_pages
            WHERE source = %s
                AND fetched_at IS NOT NULL
                AND fetched_at::timestamptz >= %s
            GROUP BY app_id
        )
        SELECT
            c.app_id,
            c.app_name,
            c.inserted,
            c.updated,
            COALESCE(p.page_count, 0)::bigint AS page_count
        FROM change_counts c
        LEFT JOIN page_counts p
            ON p.app_id = c.app_id
        ORDER BY c.inserted DESC, page_count DESC, c.app_name
        LIMIT 15
        """,
        (source, since, source, since),
    )
    return {"long_tail_apps": long_tail, "top_inserted_apps": top_inserted}


def fetch_stale_apps(connection: Any, *, source: str, generated_at: datetime) -> list[dict[str, Any]]:
    return fetch_all(
        connection,
        """
        SELECT
            t.app_id,
            t.app_name,
            t.category,
            target_country.country,
            s.last_completed_at,
            s.last_terminal_reason,
            s.backlogged,
            ROUND(EXTRACT(EPOCH FROM (%s::timestamptz - s.last_completed_at::timestamptz)) / 3600.0, 2)
                AS hours_since_completed
        FROM app_store_targets t
        LEFT JOIN LATERAL regexp_split_to_table(COALESCE(NULLIF(t.countries, ''), 'us'), '\\|')
            AS target_country(country) ON TRUE
        LEFT JOIN app_store_sync_state s
            ON s.app_id = t.app_id
            AND s.country = target_country.country
            AND s.sort_by = %s
        WHERE t.active = 1
            AND (s.last_completed_at IS NULL OR s.last_completed_at::timestamptz < %s::timestamptz - INTERVAL '24 hours')
        ORDER BY s.last_completed_at NULLS FIRST, t.app_name
        LIMIT 30
        """,
        (generated_at, WEB_CATALOG_SORT_BY, generated_at),
    )


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


def fetch_recent_history(connection: Any, *, source: str, before: datetime) -> dict[str, Any]:
    rows = fetch_all(
        connection,
        """
        SELECT
            run_id,
            loaded_at::timestamptz AS loaded_at,
            page_count,
            review_count,
            reviews_inserted,
            duplicates_skipped,
            fetch_errors
        FROM app_store_runs
        WHERE source = %s
            AND loaded_at::timestamptz < %s
            AND loaded_at::timestamptz >= %s - INTERVAL '14 days'
        ORDER BY loaded_at DESC
        LIMIT 500
        """,
        (source, before, before),
    )
    recent_inserted = [int(row.get("reviews_inserted") or 0) for row in rows[:200]]
    recent_runtime = []
    return {
        "recent_app_run_count": len(rows),
        "median_inserted_per_app_run": median(recent_inserted),
        "recent_runtime_minutes": recent_runtime,
    }


def summarize_github_payloads(
    *,
    jobs_payload: Any,
    runs_payload: Any,
    workflow_result: str,
    generated_at: datetime,
    schedule_lookback_minutes: int,
) -> dict[str, Any]:
    jobs = extract_jobs(jobs_payload)
    runs = extract_runs(runs_payload)
    failed_jobs = [
        job
        for job in jobs
        if str(job.get("conclusion") or "").lower() in {"failure", "cancelled", "timed_out"}
    ]
    scheduled_runs = [
        run
        for run in runs
        if str(run.get("event") or "").lower() == "schedule"
        and parse_utc(run.get("createdAt") or run.get("created_at")) is not None
        and parse_utc(run.get("createdAt") or run.get("created_at")) >= generated_at - timedelta(minutes=schedule_lookback_minutes)
    ]
    recent_completed_schedule = [run for run in scheduled_runs if str(run.get("status") or "").lower() == "completed"]
    recent_failed_schedule = [
        run
        for run in recent_completed_schedule
        if str(run.get("conclusion") or "").lower() not in {"success", "skipped"}
    ]
    last_scheduled_at = max(
        (parse_utc(run.get("createdAt") or run.get("created_at")) for run in scheduled_runs),
        default=None,
    )
    return {
        "workflow_result": workflow_result,
        "job_total": len(jobs),
        "job_success": sum(1 for job in jobs if job.get("conclusion") == "success"),
        "job_failure": len(failed_jobs),
        "failed_jobs": [
            {"name": job.get("name"), "conclusion": job.get("conclusion"), "url": job.get("html_url") or job.get("url")}
            for job in failed_jobs[:20]
        ],
        "recent_schedule_run_count": len(scheduled_runs),
        "recent_failed_schedule_run_count": len(recent_failed_schedule),
        "last_scheduled_run_at": last_scheduled_at.isoformat().replace("+00:00", "Z") if last_scheduled_at else "",
    }


def extract_jobs(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("jobs") or []
    return []


def extract_runs(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("workflow_runs") or payload.get("runs") or []
    return []


def evaluate_alerts(
    *,
    run_metrics: dict[str, Any],
    app_metrics: dict[str, Any],
    stale_apps: list[dict[str, Any]],
    history: dict[str, Any],
    github: dict[str, Any],
    selected_count: int,
    workflow_result: str,
    require_recent_scheduled_run: bool,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    page_count = int(run_metrics.get("page_count") or 0)
    inserted = int(run_metrics.get("reviews_inserted") or 0)
    http_429 = int(run_metrics.get("http_429_pages") or 0)
    non_200 = http_429 + int(run_metrics.get("other_non_200_pages") or 0)
    fetch_error_rate = float(run_metrics.get("fetch_error_rate") or 0)
    retry_rate = float(run_metrics.get("retry_rate") or 0)
    duplicate_rate = float(run_metrics.get("duplicate_rate") or 0)
    backlog_terminal_rate = float(run_metrics.get("backlog_terminal_rate") or 0)
    median_inserted = float(history.get("median_inserted_per_app_run") or 0)
    runtime_minutes = float(run_metrics.get("runtime_minutes") or 0)
    workflow_failed = str(workflow_result or "").lower() in {"failure", "cancelled", "timed_out"}

    if workflow_failed or int(github.get("job_failure") or 0) > 0:
        add_alert(alerts, "failing", "workflow_failure", "Current workflow or one or more required jobs failed.")
    if require_recent_scheduled_run and int(github.get("recent_schedule_run_count") or 0) == 0:
        add_alert(alerts, "failing", "missing_scheduled_run", "No scheduled App Store Review Pipeline run was found in the monitor lookback window.")
    if int(github.get("recent_failed_schedule_run_count") or 0) >= 2:
        add_alert(alerts, "failing", "repeated_scheduled_failures", "Two or more recent scheduled runs failed.")
    if int(selected_count or 0) > 0 and page_count == 0:
        add_alert(alerts, "failing", "zero_pages", "Current run has zero fetched pages for a non-empty target set.")
    if http_429 >= 3 or float(run_metrics.get("http_429_rate") or 0) >= 0.005:
        add_alert(alerts, "failing", "excessive_http_429", "HTTP 429 volume or rate crossed the failing threshold.")
    elif http_429 > 0:
        add_alert(alerts, "degraded", "http_429_present", "HTTP 429 occurred but stayed below the failing threshold.")
    if non_200 > 0 and http_429 < 3 and float(run_metrics.get("non_200_rate") or 0) < 0.005:
        add_alert(alerts, "degraded", "non_200_present", "Non-200 responses occurred but stayed below the failing threshold.")
    if fetch_error_rate >= 0.01:
        add_alert(alerts, "failing", "fetch_error_rate", "Fetch error rate crossed the 1% failing threshold.")
    if retry_rate > 0.10:
        add_alert(alerts, "degraded", "high_retry_rate", "Retried pages exceeded 10% of fetched pages.")
    max_stale_hours = max((stale_hours(app) for app in stale_apps), default=0.0)
    if stale_apps and max_stale_hours >= 36:
        add_alert(alerts, "failing", "stale_apps_36h", "At least one active app has not completed successfully in 36 hours.")
    elif stale_apps:
        add_alert(alerts, "degraded", "stale_apps_24h", "At least one active app has not completed successfully in 24 hours.")
    if runtime_minutes > 90:
        add_alert(alerts, "degraded", "long_runtime", "Current run runtime estimate exceeded 90 minutes.")
    if int(selected_count or 0) >= 100 and page_count > 100 and inserted == 0:
        add_alert(alerts, "failing", "zero_inserts_full_scope", "Full-scope-sized run fetched more than 100 pages but inserted zero reviews.")
    if backlog_terminal_rate > 0.05:
        add_alert(alerts, "failing", "backlog_terminal_rate", "More than 5% of pages ended with backlog-style terminal reasons.")
    if page_count > 0 and duplicate_rate >= 0.95:
        add_alert(alerts, "degraded", "high_duplicate_rate", "Duplicate rate is at or above 95% for the current run.")
    if median_inserted > 0 and inserted < 0.30 * median_inserted:
        add_alert(alerts, "degraded", "insert_drop", "Inserted reviews are below 30% of recent app-run median.")
    if not alerts:
        add_alert(alerts, "healthy", "all_clear", "No monitoring thresholds were tripped.")
    return alerts


def stale_hours(app: dict[str, Any]) -> float:
    hours = app.get("hours_since_completed")
    if hours is None:
        return float("inf")
    return float(hours)


def add_alert(alerts: list[dict[str, Any]], severity: str, code: str, message: str) -> None:
    alerts.append({"severity": severity, "code": code, "message": message})


def overall_status(alerts: list[dict[str, Any]]) -> str:
    severities = {alert.get("severity") for alert in alerts}
    if "failing" in severities:
        return "failing"
    if "degraded" in severities:
        return "degraded"
    return "healthy"


def render_monitoring_markdown(summary: dict[str, Any]) -> str:
    metadata = summary["metadata"]
    run = summary["run_metrics"]
    github = summary["github"]
    lines = [
        "# App Store Review Pipeline Monitoring",
        "",
        f"Generated at: `{metadata['generated_at']}`",
        f"Source: `{metadata['source']}`",
        f"Run window since: `{metadata['since']}`",
        f"GitHub run: `{metadata.get('github_run_id') or 'n/a'}`",
        "",
        "## Health",
        "",
        f"Status: **{summary['status']}**",
        "",
        markdown_table(summary.get("alerts", []), ["severity", "code", "message"]),
        "",
        "## Current Run Metrics",
        "",
        markdown_table(
            [
                {
                    "workflow_result": metadata.get("workflow_result"),
                    "selected_count": metadata.get("selected_count"),
                    "job_total": github.get("job_total"),
                    "job_success": github.get("job_success"),
                    "job_failure": github.get("job_failure"),
                    "pages": run.get("page_count"),
                    "apps": run.get("app_count"),
                    "rows": run.get("review_rows"),
                    "inserted": run.get("reviews_inserted"),
                    "updated": run.get("reviews_updated"),
                    "duplicates": run.get("duplicates_skipped"),
                    "duplicate_rate": run.get("duplicate_rate"),
                    "http_429": run.get("http_429_pages"),
                    "non_200_rate": run.get("non_200_rate"),
                    "retried_pages": run.get("retried_pages"),
                    "fetch_errors": run.get("fetch_errors"),
                }
            ],
            [
                "workflow_result",
                "selected_count",
                "job_total",
                "job_success",
                "job_failure",
                "pages",
                "apps",
                "rows",
                "inserted",
                "updated",
                "duplicates",
                "duplicate_rate",
                "http_429",
                "non_200_rate",
                "retried_pages",
                "fetch_errors",
            ],
        ),
        "",
        "## Terminal Reasons",
        "",
        markdown_table(run.get("terminal_reasons", []), ["terminal_reason", "page_count"]),
        "",
        "## Long-Tail Apps",
        "",
        markdown_table(
            summary.get("app_metrics", {}).get("long_tail_apps", []),
            ["app_name", "page_count", "review_rows", "overlap_rows", "retried_pages", "http_429_pages", "terminal_reason"],
        ),
        "",
        "## Top Inserted Apps",
        "",
        markdown_table(
            summary.get("app_metrics", {}).get("top_inserted_apps", []),
            ["app_name", "inserted", "updated", "page_count"],
        ),
        "",
        "## Stale Apps",
        "",
        markdown_table(
            summary.get("stale_apps", []),
            ["app_name", "category", "country", "hours_since_completed", "last_terminal_reason", "backlogged"],
        ),
        "",
        "## Database Snapshot",
        "",
        markdown_table(summary.get("database_snapshot", []), ["table_name", "row_count", "total_size"]),
        "",
    ]
    return "\n".join(lines)


def emit_github_annotations(summary: dict[str, Any]) -> None:
    for alert in summary.get("alerts", []):
        severity = alert.get("severity")
        if severity == "healthy":
            continue
        command = "error" if severity == "failing" else "warning"
        title = str(alert.get("code") or "monitoring_alert")
        message = str(alert.get("message") or "")
        print(f"::{command} title={escape_annotation(title)}::{escape_annotation(message)}")


def escape_annotation(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")


def median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 3)
