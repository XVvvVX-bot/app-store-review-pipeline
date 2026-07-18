from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, WEB_CATALOG_SORT_BY, WEB_CATALOG_SOURCE
from app_store_review_pipeline.eda import convert_json, markdown_table
from app_store_review_pipeline.notifications import build_monitoring_notification
from app_store_review_pipeline.operating import fetch_all, fetch_one, parse_utc
from app_store_review_pipeline.postgres_database import (
    connect_postgres,
    finalize_execution_postgres,
    mask_database_url,
    record_monitor_snapshot_postgres,
)


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
    github_event_name: str = "",
    github_run_attempt: int = 1,
    execution_id: str = "",
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
        github_event_name=github_event_name,
        github_run_attempt=github_run_attempt,
        execution_id=execution_id,
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
    github_event_name: str,
    github_run_attempt: int,
    jobs_payload: Any,
    runs_payload: Any,
    require_recent_scheduled_run: bool,
    schedule_lookback_minutes: int,
    execution_id: str = "",
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    since_dt = parse_utc(since)
    if since_dt is None:
        raise ValueError(f"Invalid --since timestamp: {since!r}")

    with connect_postgres(database_url) as connection:
        execution = fetch_execution(connection, source=source, execution_id=execution_id)
        if execution_id and not execution:
            raise ValueError(f"Unknown execution_id: {execution_id}")
        run_metrics = fetch_run_metrics(
            connection,
            source=source,
            since=since_dt,
            until=generated_at,
            execution_id=execution_id,
        )
        app_metrics = fetch_app_metrics(
            connection,
            source=source,
            since=since_dt,
            until=generated_at,
            execution_id=execution_id,
        )
        source_frontier = fetch_source_frontier_comparison(
            connection,
            source=source,
            since=since_dt,
            until=generated_at,
            execution_id=execution_id,
        )
        accounting = fetch_change_accounting(
            connection,
            source=source,
            since=since_dt,
            until=generated_at,
            execution_id=execution_id,
        )
        stale_apps = fetch_stale_apps(connection, source=source, generated_at=generated_at)
        database_snapshot = fetch_database_snapshot(connection)
        history = fetch_recent_history(
            connection,
            source=source,
            before=since_dt,
            execution_id=execution_id,
        )
        database_growth = fetch_database_growth(connection, database_snapshot=database_snapshot)

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
        source_frontier=source_frontier,
        accounting=accounting,
        stale_apps=stale_apps,
        history=history,
        database_growth=database_growth,
        github=github,
        selected_count=selected_count,
        workflow_result=workflow_result,
        require_recent_scheduled_run=require_recent_scheduled_run,
    )
    status = overall_status(alerts)
    if execution:
        for key in (
            "completed_scope_count",
            "caught_up_scope_count",
            "backlogged_scope_count",
            "hard_failure_scope_count",
        ):
            execution[key] = int(run_metrics.get(key) or 0)
        execution["status"] = status
        execution["completed_at"] = generated_at
    summary = {
        "metadata": {
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "database_url": mask_database_url(database_url),
            "source": source,
            "since": since_dt.isoformat().replace("+00:00", "Z"),
            "selected_count": int(selected_count),
            "workflow_result": workflow_result,
            "github_run_id": str(github_run_id or ""),
            "github_run_url": str(github_run_url or ""),
            "github_event_name": str(github_event_name or ""),
            "github_run_attempt": max(1, int(github_run_attempt or 1)),
            "execution_id": str(execution_id or ""),
        },
        "status": status,
        "execution": execution,
        "alerts": alerts,
        "github": github,
        "run_metrics": run_metrics,
        "app_metrics": app_metrics,
        "source_frontier": source_frontier,
        "accounting": accounting,
        "stale_apps": stale_apps,
        "history": history,
        "database_snapshot": database_snapshot,
        "database_growth": database_growth,
    }
    summary["notification"] = build_monitoring_notification(summary)
    summary = convert_json(summary)
    if execution_id:
        finalize_execution_postgres(
            database_url,
            execution_id=execution_id,
            status=status,
            completed_at=generated_at.isoformat().replace("+00:00", "Z"),
            initialize_schema=False,
        )
        record_monitor_snapshot_postgres(
            database_url,
            execution_id=execution_id,
            status=status,
            metrics=summary,
            initialize_schema=False,
        )
    return summary


def fetch_execution(connection: Any, *, source: str, execution_id: str) -> dict[str, Any]:
    if not execution_id:
        return {}
    return fetch_one(
        connection,
        """
        SELECT execution_id, github_run_id, github_run_attempt, workflow_name,
            event_name, git_sha, source, scope_signature, config_signature,
            intended_target_count, intended_scope_count, completed_scope_count,
            caught_up_scope_count, backlogged_scope_count, hard_failure_scope_count,
            status, started_at, completed_at
        FROM app_store_executions
        WHERE execution_id = %s AND source = %s
        """,
        (execution_id, source),
    )


def fetch_run_metrics(
    connection: Any,
    *,
    source: str,
    since: datetime,
    until: datetime,
    execution_id: str = "",
) -> dict[str, Any]:
    if execution_id:
        page_where = "p.source = %s AND r.execution_id = %s"
        page_params: tuple[Any, ...] = (source, execution_id)
        run_where = "source = %s AND execution_id = %s"
        run_params: tuple[Any, ...] = (source, execution_id)
        scope_where = "source = %s AND execution_id = %s"
        scope_params: tuple[Any, ...] = (source, execution_id)
    else:
        page_where = "p.source = %s AND p.fetched_at_ts >= %s AND p.fetched_at_ts <= %s"
        page_params = (source, since, until)
        run_where = "source = %s AND loaded_at_ts >= %s AND loaded_at_ts <= %s"
        run_params = (source, since, until)
        scope_where = "source = %s AND completed_at >= %s AND completed_at <= %s"
        scope_params = (source, since, until)

    page = fetch_one(
        connection,
        f"""
        SELECT
            COUNT(*)::bigint AS page_count,
            COUNT(DISTINCT p.app_id)::bigint AS app_count,
            COALESCE(SUM(p.review_count), 0)::bigint AS review_rows,
            COALESCE(SUM(p.unique_review_count), 0)::bigint AS unique_review_rows,
            COALESCE(SUM(p.overlap_review_count), 0)::bigint AS overlap_rows,
            COUNT(*) FILTER (WHERE p.status_code = 200)::bigint AS http_200_pages,
            COUNT(*) FILTER (WHERE p.status_code = 429)::bigint AS http_429_pages,
            COALESCE(SUM(p.http_429_attempt_count), 0)::bigint AS http_429_attempts,
            COALESCE(SUM(p.soft_retry_count), 0)::bigint AS soft_retry_count,
            COUNT(*) FILTER (
                WHERE p.status_code IS NOT NULL AND p.status_code <> 200 AND p.status_code <> 429
            )::bigint AS other_non_200_pages,
            COUNT(*) FILTER (WHERE p.attempt_count > 1)::bigint AS retried_pages,
            COALESCE(MAX(p.attempt_count), 0)::bigint AS max_attempt_count,
            MIN(p.fetched_at_ts) AS first_page_at,
            MAX(p.fetched_at_ts) AS last_page_at
        FROM app_store_review_pages p
        JOIN app_store_runs r ON r.run_id = p.run_id
        WHERE {page_where}
        """,
        page_params,
    )
    load = fetch_one(
        connection,
        f"""
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
        WHERE {run_where}
        """,
        run_params,
    )
    scope = fetch_one(
        connection,
        f"""
        SELECT
            COUNT(*)::bigint AS completed_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'caught_up')::bigint AS caught_up_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'backlogged')::bigint AS backlogged_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'hard_failure')::bigint AS hard_failure_scope_count,
            COALESCE(SUM(fetch_errors) FILTER (WHERE outcome = 'hard_failure'), 0)::bigint
                AS scope_fetch_errors
        FROM app_store_run_scopes
        WHERE {scope_where}
        """,
        scope_params,
    )
    terminal_reasons = fetch_all(
        connection,
        f"""
        SELECT COALESCE(NULLIF(terminal_reason, ''), 'none') AS terminal_reason,
            COUNT(*)::bigint AS scope_count
        FROM app_store_run_scopes
        WHERE {scope_where}
        GROUP BY COALESCE(NULLIF(terminal_reason, ''), 'none')
        ORDER BY scope_count DESC, terminal_reason
        LIMIT 12
        """,
        scope_params,
    )
    attempt_counts = fetch_all(
        connection,
        f"""
        SELECT p.attempt_count, COUNT(*)::bigint AS page_count
        FROM app_store_review_pages p
        JOIN app_store_runs r ON r.run_id = p.run_id
        WHERE {page_where}
        GROUP BY p.attempt_count
        ORDER BY p.attempt_count
        """,
        page_params,
    )
    output = {**page, **load, **scope}
    page_count = int(output.get("page_count") or 0)
    completed_scope_count = int(output.get("completed_scope_count") or 0)
    review_rows = int(output.get("review_rows") or 0)
    inserted = int(output.get("reviews_inserted") or 0)
    updated = int(output.get("reviews_updated") or 0)
    skipped = int(output.get("duplicates_skipped") or 0)
    observed_rows = inserted + updated + skipped
    output["http_429_rate"] = (
        round(int(output.get("http_429_attempts") or 0) / page_count, 4) if page_count else 0
    )
    output["final_http_429_rate"] = (
        round(int(output.get("http_429_pages") or 0) / page_count, 4) if page_count else 0
    )
    output["non_200_rate"] = (
        round((int(output.get("http_429_pages") or 0) + int(output.get("other_non_200_pages") or 0)) / page_count, 4)
        if page_count
        else 0
    )
    output["other_non_200_rate"] = (
        round(int(output.get("other_non_200_pages") or 0) / page_count, 4) if page_count else 0
    )
    output["retry_rate"] = round(int(output.get("retried_pages") or 0) / page_count, 4) if page_count else 0
    output["fetch_error_rate"] = (
        round(int(output.get("scope_fetch_errors") or 0) / completed_scope_count, 4)
        if completed_scope_count
        else 0
    )
    output["backlog_terminal_rate"] = (
        round(int(output.get("backlogged_scope_count") or 0) / completed_scope_count, 4)
        if completed_scope_count
        else 0
    )
    output["duplicate_rate"] = round(skipped / observed_rows, 4) if observed_rows else 0
    output["inserted_per_page"] = round(inserted / page_count, 3) if page_count else 0
    output["inserted_per_row"] = round(inserted / review_rows, 4) if review_rows else 0
    output["inserted_per_scope"] = round(inserted / completed_scope_count, 3) if completed_scope_count else 0
    intended_scope_count = 0
    if execution_id:
        execution = fetch_one(
            connection,
            "SELECT intended_scope_count, started_at, completed_at FROM app_store_executions WHERE execution_id = %s",
            (execution_id,),
        )
        intended_scope_count = int(execution.get("intended_scope_count") or 0)
    output["intended_scope_count"] = intended_scope_count
    output["missing_scope_count"] = max(0, intended_scope_count - completed_scope_count)
    output["scope_completion_rate"] = (
        round(completed_scope_count / intended_scope_count, 4) if intended_scope_count else 0
    )
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


def fetch_app_metrics(
    connection: Any,
    *,
    source: str,
    since: datetime,
    until: datetime,
    execution_id: str = "",
) -> dict[str, Any]:
    if execution_id:
        where = "source = %s AND execution_id = %s"
        params: tuple[Any, ...] = (source, execution_id)
    else:
        where = "source = %s AND completed_at >= %s AND completed_at <= %s"
        params = (source, since, until)
    long_tail = fetch_all(
        connection,
        f"""
        SELECT app_id, MAX(app_name) AS app_name,
            SUM(page_count)::bigint AS page_count,
            SUM(review_count)::bigint AS review_rows,
            SUM(overlap_review_count)::bigint AS overlap_rows,
            SUM(http_429_pages)::bigint AS http_429_pages,
            SUM(http_429_attempt_count)::bigint AS http_429_attempts,
            SUM(soft_retry_count)::bigint AS soft_retry_count,
            SUM(retried_pages)::bigint AS retried_pages,
            COUNT(*) FILTER (WHERE outcome <> 'caught_up')::bigint AS incomplete_scopes,
            STRING_AGG(DISTINCT COALESCE(NULLIF(terminal_reason, ''), 'none'), ', ' ORDER BY COALESCE(NULLIF(terminal_reason, ''), 'none'))
                AS terminal_reason
        FROM app_store_run_scopes
        WHERE {where}
        GROUP BY app_id
        ORDER BY page_count DESC, review_rows DESC, app_name
        LIMIT 15
        """,
        params,
    )
    top_inserted = fetch_all(
        connection,
        f"""
        SELECT app_id, MAX(app_name) AS app_name,
            SUM(reviews_inserted)::bigint AS inserted,
            SUM(reviews_updated)::bigint AS updated,
            SUM(page_count)::bigint AS page_count
        FROM app_store_run_scopes
        WHERE {where}
        GROUP BY app_id
        ORDER BY inserted DESC, page_count DESC, app_name
        LIMIT 15
        """,
        params,
    )
    pressure_scopes = fetch_all(
        connection,
        f"""
        WITH grouped AS (
            SELECT app_id, MAX(app_name) AS app_name, country, sort_by,
                SUM(page_count)::bigint AS page_count,
                SUM(http_429_pages)::bigint AS http_429_pages,
                SUM(http_429_attempt_count)::bigint AS http_429_attempts,
                SUM(soft_retry_count)::bigint AS soft_retry_count,
                SUM(other_non_200_pages)::bigint AS other_non_200_pages,
                SUM(fetch_errors)::bigint AS fetch_error_pages,
                SUM(retried_pages)::bigint AS retried_pages,
                MAX(COALESCE(NULLIF(terminal_reason, ''), 'none')) AS terminal_reason,
                MAX(outcome) AS outcome
            FROM app_store_run_scopes
            WHERE {where}
            GROUP BY app_id, country, sort_by
        )
        SELECT *,
            COALESCE(ROUND(page_count::numeric / NULLIF(SUM(page_count) OVER (), 0), 4), 0) AS page_share,
            COALESCE(ROUND(http_429_attempts::numeric / NULLIF(SUM(http_429_attempts) OVER (), 0), 4), 0)
                AS http_429_share
        FROM grouped
        ORDER BY http_429_attempts DESC, fetch_error_pages DESC,
            CASE outcome WHEN 'hard_failure' THEN 0 WHEN 'backlogged' THEN 1 ELSE 2 END,
            page_count DESC, app_name
        LIMIT 15
        """,
        params,
    )
    return {
        "long_tail_apps": long_tail,
        "top_inserted_apps": top_inserted,
        "pressure_scopes": pressure_scopes,
    }


def fetch_source_frontier_comparison(
    connection: Any,
    *,
    source: str,
    since: datetime,
    until: datetime,
    execution_id: str = "",
) -> dict[str, Any]:
    if execution_id:
        current_where = "p.source = %s AND r.execution_id = %s"
        current_params: tuple[Any, ...] = (source, execution_id)
    else:
        current_where = "p.source = %s AND p.fetched_at_ts >= %s AND p.fetched_at_ts <= %s"
        current_params = (source, since, until)
    result = fetch_one(
        connection,
        f"""
        WITH current_frontier AS (
            SELECT DISTINCT ON (app_id, country, sort_by)
                p.app_id,
                p.country,
                p.sort_by,
                p.max_updated_epoch_seconds,
                p.fetched_at_ts AS fetched_at
            FROM app_store_review_pages p
            JOIN app_store_runs r ON r.run_id = p.run_id
            WHERE {current_where}
                AND p.page_number = 1
            ORDER BY p.app_id, p.country, p.sort_by, p.fetched_at_ts DESC
        ),
        previous_frontier AS (
            SELECT DISTINCT ON (p.app_id, p.country, p.sort_by)
                p.app_id,
                p.country,
                p.sort_by,
                p.max_updated_epoch_seconds,
                p.fetched_at_ts AS fetched_at
            FROM app_store_review_pages p
            JOIN current_frontier c USING (app_id, country, sort_by)
            WHERE p.source = %s
                AND p.page_number = 1
                AND p.fetched_at_ts < %s
            ORDER BY p.app_id, p.country, p.sort_by, p.fetched_at_ts DESC
        )
        SELECT
            COUNT(*)::bigint AS current_scopes,
            COUNT(p.app_id)::bigint AS comparable_scopes,
            COUNT(*) FILTER (
                WHERE c.max_updated_epoch_seconds IS NOT DISTINCT FROM p.max_updated_epoch_seconds
                    AND p.app_id IS NOT NULL
            )::bigint AS unchanged_scopes,
            COUNT(*) FILTER (WHERE c.max_updated_epoch_seconds > p.max_updated_epoch_seconds)::bigint AS advanced_scopes,
            COUNT(*) FILTER (WHERE c.max_updated_epoch_seconds < p.max_updated_epoch_seconds)::bigint AS regressed_scopes,
            COUNT(*) FILTER (WHERE p.app_id IS NULL)::bigint AS missing_previous_scopes
        FROM current_frontier c
        LEFT JOIN previous_frontier p USING (app_id, country, sort_by)
        """,
        (*current_params, source, since),
    )
    comparable = int(result.get("comparable_scopes") or 0)
    unchanged = int(result.get("unchanged_scopes") or 0)
    result["unchanged_rate"] = round(unchanged / comparable, 4) if comparable else 0
    return result


def fetch_change_accounting(
    connection: Any,
    *,
    source: str,
    since: datetime,
    until: datetime,
    execution_id: str = "",
) -> dict[str, Any]:
    if execution_id:
        run_where = "source = %s AND execution_id = %s"
        run_params: tuple[Any, ...] = (source, execution_id)
        change_where = "run.source = %s AND run.execution_id = %s"
        change_params: tuple[Any, ...] = (source, execution_id)
    else:
        run_where = "source = %s AND loaded_at_ts >= %s AND loaded_at_ts <= %s"
        run_params = (source, since, until)
        change_where = (
            "run.source = %s AND c.changed_at_ts >= %s "
            "AND c.changed_at_ts <= %s"
        )
        change_params = (source, since, until)
    result = fetch_one(
        connection,
        f"""
        SELECT
            (SELECT COALESCE(SUM(reviews_inserted), 0)::bigint
                FROM app_store_runs
                WHERE {run_where}) AS reported_inserts,
            (SELECT COALESCE(SUM(reviews_updated), 0)::bigint
                FROM app_store_runs
                WHERE {run_where}) AS reported_updates,
            (SELECT COUNT(*)::bigint
                FROM app_store_review_changes c
                JOIN app_store_runs run ON run.run_id = c.run_id
                WHERE {change_where}
                    AND c.change_type = 'inserted'
            ) AS recorded_inserts,
            (SELECT COUNT(*)::bigint
                FROM app_store_review_changes c
                JOIN app_store_runs run ON run.run_id = c.run_id
                WHERE {change_where}
                    AND c.change_type = 'updated'
            ) AS recorded_updates
        """,
        (*run_params, *run_params, *change_params, *change_params),
    )
    result["insert_delta"] = int(result.get("reported_inserts") or 0) - int(result.get("recorded_inserts") or 0)
    result["update_delta"] = int(result.get("reported_updates") or 0) - int(result.get("recorded_updates") or 0)
    result["consistent"] = result["insert_delta"] == 0 and result["update_delta"] == 0
    return result


def fetch_stale_apps(connection: Any, *, source: str, generated_at: datetime) -> list[dict[str, Any]]:
    return fetch_all(
        connection,
        """
        SELECT
            t.app_id,
            t.app_name,
            t.category,
            target_country.country,
            s.last_successful_at,
            s.last_attempt_completed_at,
            COALESCE(s.last_attempt_completed_at, s.last_successful_at) AS freshness_at,
            s.last_terminal_reason,
            s.backlogged,
            s.backlog_started_at,
            s.consecutive_incomplete_runs,
            ROUND(EXTRACT(EPOCH FROM (
                %s::timestamptz - COALESCE(s.last_attempt_completed_at, s.last_successful_at)
            )) / 3600.0, 2) AS hours_since_completed,
            ROUND(EXTRACT(EPOCH FROM (%s::timestamptz - s.last_successful_at)) / 3600.0, 2)
                AS hours_since_successful_catchup
        FROM app_store_targets t
        LEFT JOIN LATERAL regexp_split_to_table(COALESCE(NULLIF(t.countries, ''), 'us'), '\\|')
            AS target_country(country) ON TRUE
        LEFT JOIN app_store_sync_state s
            ON s.app_id = t.app_id
            AND s.country = lower(target_country.country)
            AND s.sort_by = %s
            AND s.source = %s
        WHERE t.active = 1
            AND (
                COALESCE(s.last_attempt_completed_at, s.last_successful_at) IS NULL
                OR COALESCE(s.last_attempt_completed_at, s.last_successful_at)
                    < %s::timestamptz - INTERVAL '24 hours'
            )
        ORDER BY freshness_at NULLS FIRST, t.app_name
        LIMIT 30
        """,
        (generated_at, generated_at, WEB_CATALOG_SORT_BY, source, generated_at),
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


def fetch_recent_history(
    connection: Any,
    *,
    source: str,
    before: datetime,
    execution_id: str = "",
) -> dict[str, Any]:
    current = fetch_execution(connection, source=source, execution_id=execution_id)
    signatures_available = bool(current.get("scope_signature") and current.get("config_signature"))
    signature_filter = ""
    params: tuple[Any, ...] = (source, before, before)
    if signatures_available:
        signature_filter = "AND e.scope_signature = %s AND e.config_signature = %s"
        params = (
            source,
            before,
            before,
            current["scope_signature"],
            current["config_signature"],
        )
    rows = fetch_all(
        connection,
        f"""
        SELECT e.execution_id, e.status, e.started_at, e.completed_at,
            e.intended_scope_count, e.completed_scope_count,
            COALESCE(SUM(s.page_count), 0)::bigint AS page_count,
            COALESCE(SUM(s.review_count), 0)::bigint AS review_count,
            COALESCE(SUM(s.reviews_inserted), 0)::bigint AS reviews_inserted,
            COALESCE(SUM(s.duplicates_skipped), 0)::bigint AS duplicates_skipped,
            ROUND(EXTRACT(EPOCH FROM (e.completed_at - e.started_at)) / 60.0, 2) AS runtime_minutes
        FROM app_store_executions e
        LEFT JOIN app_store_run_scopes s ON s.execution_id = e.execution_id
        WHERE e.source = %s
            AND e.completed_at IS NOT NULL
            AND e.completed_at < %s
            AND e.completed_at >= %s - INTERVAL '14 days'
            AND e.status IN ('healthy', 'degraded')
            AND e.intended_scope_count > 0
            AND e.completed_scope_count = e.intended_scope_count
            {signature_filter}
        GROUP BY e.execution_id
        ORDER BY e.completed_at DESC
        LIMIT 20
        """,
        params,
    )
    inserted = [int(row.get("reviews_inserted") or 0) for row in rows]
    runtimes = [float(row.get("runtime_minutes") or 0) for row in rows if row.get("runtime_minutes") is not None]
    duplicate_rates = []
    for row in rows:
        observed = int(row.get("reviews_inserted") or 0) + int(row.get("duplicates_skipped") or 0)
        if observed:
            duplicate_rates.append(int(row.get("duplicates_skipped") or 0) / observed)
    return {
        "comparable_execution_count": len(rows),
        "median_inserted_per_execution": median(inserted),
        "median_runtime_minutes": median(runtimes),
        "median_duplicate_rate": median(duplicate_rates),
        "comparable_executions": rows,
    }


def fetch_database_growth(connection: Any, *, database_snapshot: list[dict[str, Any]]) -> dict[str, Any]:
    current_bytes = sum(int(row.get("total_bytes") or 0) for row in database_snapshot)
    rows = fetch_all(
        connection,
        """
        SELECT database_bytes, captured_at
        FROM app_store_monitor_snapshots
        ORDER BY captured_at DESC
        LIMIT 8
        """,
    )
    if not rows:
        return {
            "current_bytes": current_bytes,
            "previous_bytes": None,
            "growth_bytes": None,
            "growth_rate_per_hour": None,
            "recent_median_growth_bytes": 0,
        }
    previous = rows[0]
    previous_at = parse_utc(previous.get("captured_at"))
    now = datetime.now(timezone.utc)
    elapsed_hours = max((now - previous_at).total_seconds() / 3600.0, 0.001) if previous_at else 0
    growth = current_bytes - int(previous.get("database_bytes") or 0)
    historical_growth = []
    for newer, older in zip(rows, rows[1:]):
        delta = int(newer.get("database_bytes") or 0) - int(older.get("database_bytes") or 0)
        if delta >= 0:
            historical_growth.append(delta)
    return {
        "current_bytes": current_bytes,
        "previous_bytes": int(previous.get("database_bytes") or 0),
        "growth_bytes": growth,
        "growth_rate_per_hour": round(growth / elapsed_hours, 2) if elapsed_hours else None,
        "recent_median_growth_bytes": median(historical_growth),
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
    required_jobs = [job for job in jobs if not is_monitor_job(job)]
    failed_jobs = [
        job
        for job in required_jobs
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
        if str(run.get("ingestion_conclusion") or "").lower() in {"failure", "cancelled", "timed_out"}
    ]
    current_started = [parse_utc(job.get("started_at")) for job in required_jobs]
    current_completed = [parse_utc(job.get("completed_at")) for job in required_jobs]
    current_started = [value for value in current_started if value is not None]
    current_completed = [value for value in current_completed if value is not None]
    current_runtime = (
        (max(current_completed) - min(current_started)).total_seconds() / 60.0
        if current_started and current_completed and max(current_completed) >= min(current_started)
        else 0.0
    )
    recent_runtimes = []
    for run in recent_completed_schedule:
        created_at = parse_utc(run.get("createdAt") or run.get("created_at"))
        updated_at = parse_utc(run.get("updatedAt") or run.get("updated_at"))
        if created_at and updated_at and updated_at >= created_at:
            recent_runtimes.append((updated_at - created_at).total_seconds() / 60.0)
    last_scheduled_at = max(
        (parse_utc(run.get("createdAt") or run.get("created_at")) for run in scheduled_runs),
        default=None,
    )
    return {
        "workflow_result": workflow_result,
        "job_total": len(required_jobs),
        "job_success": sum(1 for job in required_jobs if job.get("conclusion") == "success"),
        "job_failure": len(failed_jobs),
        "failed_jobs": [
            {"name": job.get("name"), "conclusion": job.get("conclusion"), "url": job.get("html_url") or job.get("url")}
            for job in failed_jobs[:20]
        ],
        "recent_schedule_run_count": len(scheduled_runs),
        "recent_failed_schedule_run_count": len(recent_failed_schedule),
        "current_runtime_minutes": round(current_runtime, 2),
        "recent_median_runtime_minutes": median(recent_runtimes),
        "last_scheduled_run_at": last_scheduled_at.isoformat().replace("+00:00", "Z") if last_scheduled_at else "",
    }


def is_monitor_job(job: dict[str, Any]) -> bool:
    name = str(job.get("name") or "").strip().lower()
    return (
        name in {"monitor", "notify"}
        or name.startswith("monitor ")
        or name.startswith("notify ")
    )


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
    source_frontier: dict[str, Any],
    accounting: dict[str, Any],
    stale_apps: list[dict[str, Any]],
    history: dict[str, Any],
    github: dict[str, Any],
    selected_count: int,
    workflow_result: str,
    require_recent_scheduled_run: bool,
    database_growth: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    database_growth = database_growth or {}
    page_count = int(run_metrics.get("page_count") or 0)
    inserted = int(run_metrics.get("reviews_inserted") or 0)
    http_429_attempts = int(run_metrics.get("http_429_attempts") or run_metrics.get("http_429_pages") or 0)
    final_http_429_pages = int(run_metrics.get("http_429_pages") or 0)
    final_http_429_rate = float(run_metrics.get("final_http_429_rate") or 0)
    other_non_200 = int(run_metrics.get("other_non_200_pages") or 0)
    fetch_error_rate = float(run_metrics.get("fetch_error_rate") or 0)
    retry_rate = float(run_metrics.get("retry_rate") or 0)
    duplicate_rate = float(run_metrics.get("duplicate_rate") or 0)
    backlog_terminal_rate = float(run_metrics.get("backlog_terminal_rate") or 0)
    median_inserted = float(history.get("median_inserted_per_execution") or 0)
    comparable_execution_count = int(history.get("comparable_execution_count") or 0)
    runtime_minutes = float(github.get("current_runtime_minutes") or run_metrics.get("runtime_minutes") or 0)
    recent_runtime_median = float(
        history.get("median_runtime_minutes")
        or github.get("recent_median_runtime_minutes")
        or 0
    )
    workflow_failed = str(workflow_result or "").lower() in {"failure", "cancelled", "timed_out"}

    if workflow_failed or int(github.get("job_failure") or 0) > 0:
        add_alert(alerts, "failing", "workflow_failure", "Current workflow or one or more required jobs failed.")
    if require_recent_scheduled_run and int(github.get("recent_schedule_run_count") or 0) == 0:
        add_alert(alerts, "failing", "missing_scheduled_run", "No scheduled App Store Review Pipeline run was found in the monitor lookback window.")
    if require_recent_scheduled_run and int(github.get("recent_failed_schedule_run_count") or 0) >= 2:
        add_alert(alerts, "failing", "repeated_scheduled_failures", "Two or more recent scheduled runs failed.")
    if int(selected_count or 0) > 0 and page_count == 0:
        add_alert(alerts, "failing", "zero_pages", "Current run has zero fetched pages for a non-empty target set.")
    missing_scope_count = int(run_metrics.get("missing_scope_count") or 0)
    if missing_scope_count > 0:
        add_alert(
            alerts,
            "failing",
            "missing_execution_scopes",
            f"{missing_scope_count} intended app-country scopes produced no persisted scope outcome.",
        )
    hard_failure_scope_count = int(run_metrics.get("hard_failure_scope_count") or 0)
    if hard_failure_scope_count > 0:
        add_alert(
            alerts,
            "failing",
            "hard_failure_scopes",
            f"{hard_failure_scope_count} app-country scopes ended in a hard failure.",
        )
    if final_http_429_pages >= 3 or final_http_429_rate >= 0.005:
        add_alert(alerts, "failing", "excessive_http_429", "Final HTTP 429 page volume or rate crossed the failing threshold.")
    elif http_429_attempts > 0:
        add_alert(alerts, "degraded", "http_429_present", "An HTTP 429 attempt occurred but recovered or final 429 pressure stayed below the failing threshold.")
    if other_non_200 > 0:
        add_alert(alerts, "degraded", "other_non_200_present", "One or more non-429 error responses occurred.")
    if fetch_error_rate >= 0.01:
        add_alert(alerts, "failing", "fetch_error_rate", "Fetch error rate crossed the 1% failing threshold.")
    if retry_rate > 0.10:
        add_alert(alerts, "degraded", "high_retry_rate", "Retried pages exceeded 10% of fetched pages.")
    max_stale_hours = max((stale_hours(app) for app in stale_apps), default=0.0)
    if stale_apps and max_stale_hours >= 36:
        add_alert(alerts, "failing", "stale_apps_36h", "At least one active app has no completed collection attempt in 36 hours.")
    elif stale_apps:
        add_alert(alerts, "degraded", "stale_apps_24h", "At least one active app has no completed collection attempt in 24 hours.")
    if runtime_minutes > 90 or (recent_runtime_median > 0 and runtime_minutes > 2 * recent_runtime_median):
        add_alert(alerts, "degraded", "long_runtime", "Current run runtime exceeded 90 minutes or twice the recent median.")
    if int(selected_count or 0) >= 100 and page_count > 100 and inserted == 0:
        comparable = int(source_frontier.get("comparable_scopes") or 0)
        unchanged_rate = float(source_frontier.get("unchanged_rate") or 0)
        enough_comparisons = comparable >= max(1, int(0.8 * int(selected_count or 0)))
        if enough_comparisons and unchanged_rate >= 0.95:
            add_alert(
                alerts,
                "degraded",
                "source_snapshot_unchanged",
                "The full-scope run inserted zero reviews because at least 95% of comparable page-one frontiers were unchanged.",
            )
        else:
            add_alert(
                alerts,
                "degraded",
                "zero_inserts_full_scope",
                "The full-scope run inserted zero reviews; source-frontier evidence did not prove a storage failure.",
            )
    if page_count > 0 and not bool(accounting.get("consistent", True)):
        add_alert(
            alerts,
            "failing",
            "change_accounting_mismatch",
            "Run insert/update totals do not match the persisted review-change ledger.",
        )
    if int(selected_count or 0) >= 100 and backlog_terminal_rate > 0.05:
        add_alert(alerts, "failing", "backlog_terminal_rate", "More than 5% of completed scopes remained backlogged.")
    elif int(run_metrics.get("backlogged_scope_count") or 0) > 0:
        add_alert(
            alerts,
            "degraded",
            "backlogged_scopes",
            "One or more completed scopes remain backlogged; targeted runs stay degraded so recovery can continue without a production failure alert.",
        )
    if page_count > 0 and int(selected_count or 0) >= 100 and duplicate_rate >= 0.95:
        add_alert(alerts, "degraded", "high_duplicate_rate", "Duplicate rate is at or above 95% for the current run.")
    if comparable_execution_count >= 3 and median_inserted > 0 and inserted < 0.30 * median_inserted:
        add_alert(
            alerts,
            "degraded",
            "insert_drop",
            "Inserted reviews are below 30% of the median from comparable complete executions.",
        )
    growth_bytes = database_growth.get("growth_bytes")
    median_growth = float(database_growth.get("recent_median_growth_bytes") or 0)
    if growth_bytes is not None and int(growth_bytes) > 100 * 1024 * 1024 and median_growth > 0:
        if int(growth_bytes) > 3 * median_growth:
            add_alert(
                alerts,
                "degraded",
                "unusual_database_growth",
                "Database growth exceeded 100 MiB and three times the recent monitoring median.",
            )
    pressure_scopes = (app_metrics or {}).get("pressure_scopes") or []
    if pressure_scopes and int(selected_count or 0) >= 10:
        top_scope = pressure_scopes[0]
        if (
            float(top_scope.get("page_share") or 0) >= 0.25
            or (
                int(top_scope.get("http_429_attempts") or top_scope.get("http_429_pages") or 0) > 0
                and float(top_scope.get("http_429_share") or 0) >= 0.50
            )
        ):
            add_alert(
                alerts,
                "degraded",
                "dominant_backlogged_scope",
                f"{top_scope.get('app_name') or top_scope.get('app_id')} dominated recent page or HTTP 429 volume.",
            )
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
        f"Execution: `{metadata.get('execution_id') or 'legacy time window'}`",
        f"GitHub run: `{metadata.get('github_run_id') or 'n/a'}`",
        f"GitHub event: `{metadata.get('github_event_name') or 'n/a'}`",
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
                    "intended_scopes": run.get("intended_scope_count"),
                    "completed_scopes": run.get("completed_scope_count"),
                    "caught_up_scopes": run.get("caught_up_scope_count"),
                    "backlogged_scopes": run.get("backlogged_scope_count"),
                    "hard_failure_scopes": run.get("hard_failure_scope_count"),
                    "missing_scopes": run.get("missing_scope_count"),
                    "pages": run.get("page_count"),
                    "apps": run.get("app_count"),
                    "rows": run.get("review_rows"),
                    "inserted": run.get("reviews_inserted"),
                    "updated": run.get("reviews_updated"),
                    "duplicates": run.get("duplicates_skipped"),
                    "duplicate_rate": run.get("duplicate_rate"),
                    "final_http_429_pages": run.get("http_429_pages"),
                    "http_429_attempts": run.get("http_429_attempts"),
                    "soft_retries": run.get("soft_retry_count"),
                    "other_non_200": run.get("other_non_200_pages"),
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
                "intended_scopes",
                "completed_scopes",
                "caught_up_scopes",
                "backlogged_scopes",
                "hard_failure_scopes",
                "missing_scopes",
                "pages",
                "apps",
                "rows",
                "inserted",
                "updated",
                "duplicates",
                "duplicate_rate",
                "final_http_429_pages",
                "http_429_attempts",
                "soft_retries",
                "other_non_200",
                "retried_pages",
                "fetch_errors",
            ],
        ),
        "",
        "## Terminal Reasons",
        "",
        markdown_table(run.get("terminal_reasons", []), ["terminal_reason", "scope_count"]),
        "",
        "## Long-Tail Apps",
        "",
        markdown_table(
            summary.get("app_metrics", {}).get("long_tail_apps", []),
            [
                "app_name",
                "page_count",
                "review_rows",
                "overlap_rows",
                "retried_pages",
                "http_429_attempts",
                "http_429_pages",
                "soft_retry_count",
                "terminal_reason",
            ],
        ),
        "",
        "## Source-Pressure Scopes",
        "",
        markdown_table(
            summary.get("app_metrics", {}).get("pressure_scopes", []),
            [
                "app_name",
                "country",
                "page_count",
                "page_share",
                "http_429_attempts",
                "http_429_pages",
                "http_429_share",
                "soft_retry_count",
                "fetch_error_pages",
                "terminal_reason",
            ],
        ),
        "",
        "## Source Frontier And Change Accounting",
        "",
        markdown_table(
            [summary.get("source_frontier", {})],
            [
                "current_scopes",
                "comparable_scopes",
                "unchanged_scopes",
                "advanced_scopes",
                "regressed_scopes",
                "unchanged_rate",
            ],
        ),
        "",
        markdown_table(
            [summary.get("accounting", {})],
            ["reported_inserts", "recorded_inserts", "reported_updates", "recorded_updates", "consistent"],
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
            [
                "app_name",
                "category",
                "country",
                "hours_since_completed",
                "hours_since_successful_catchup",
                "last_terminal_reason",
                "backlogged",
            ],
        ),
        "",
        "## Database Snapshot",
        "",
        markdown_table(summary.get("database_snapshot", []), ["table_name", "row_count", "total_size"]),
        "",
        "## Database Growth",
        "",
        markdown_table(
            [summary.get("database_growth", {})],
            ["current_bytes", "previous_bytes", "growth_bytes", "growth_rate_per_hour", "recent_median_growth_bytes"],
        ),
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


def median(values: list[int | float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 3)
