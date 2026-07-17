from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app_store_review_pipeline.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_TARGETS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WEB_CATALOG_RAW_ROOT,
    DEFAULT_WEB_CATALOG_REPORTS_ROOT,
    SOURCE,
    WEB_CATALOG_SORT_BY,
    WEB_CATALOG_SOURCE,
)
from app_store_review_pipeline.eda import DEFAULT_EDA_HTML, DEFAULT_EDA_JSON, DEFAULT_EDA_MARKDOWN, generate_eda_report
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.monitoring import (
    DEFAULT_MONITORING_JSON,
    DEFAULT_MONITORING_MARKDOWN,
    emit_github_annotations,
    generate_monitoring_report,
)
from app_store_review_pipeline.notifications import (
    DEFAULT_NOTIFICATION_PREVIEW,
    DEFAULT_NOTIFICATION_RESULT,
    send_monitoring_email,
    write_fallback_failure_report,
)
from app_store_review_pipeline.operating import (
    DEFAULT_OPERATING_JSON,
    DEFAULT_OPERATING_LEDGER,
    DEFAULT_OPERATING_MARKDOWN,
    build_operating_ledger_run_entry,
    fetch_github_run_payload,
    generate_operating_report,
    upsert_operating_ledger_run,
)
from app_store_review_pipeline.postgres_database import (
    backfill_typed_timestamps_postgres,
    backlogged_resume_start_pages_by_scope,
    finalize_execution_postgres,
    initialize_postgres,
    load_pipeline_run_postgres,
    mask_database_url,
    record_run_scopes_postgres,
    record_web_catalog_pressure_result,
    review_counts_by_scope,
    sync_targets_postgres,
    trusted_existing_review_ids_by_scope,
    upsert_execution_postgres,
    update_sync_states_postgres,
    validate_postgres,
    web_catalog_429_circuit_breaker_status,
    web_catalog_429_cooldown_status,
    web_catalog_pressure_status,
)
from app_store_review_pipeline.targets import active_targets, load_targets
from app_store_review_pipeline.utils import make_run_id, utc_timestamp
from app_store_review_pipeline.web_catalog_fetcher import fetch_web_catalog_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple App Store public web-catalog review ingestion pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    targets = subparsers.add_parser("targets", help="Summarize target apps and app-country scopes.")
    targets.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    targets.set_defaults(func=command_targets)

    sync_targets = subparsers.add_parser("sync-targets", help="Sync the repository target list into Postgres.")
    sync_targets.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    sync_targets.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    sync_targets.set_defaults(func=command_sync_targets)

    fetch_web_catalog = subparsers.add_parser(
        "fetch-web-catalog",
        help="Fetch full review rows from the public App Store web catalog JSON endpoint.",
    )
    add_web_catalog_fetch_arguments(fetch_web_catalog)
    fetch_web_catalog.set_defaults(func=command_fetch_web_catalog)

    init_postgres = subparsers.add_parser("init-postgres", help="Create or update the Postgres schema.")
    init_postgres.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    init_postgres.set_defaults(func=command_init_postgres)

    typed_timestamps = subparsers.add_parser(
        "backfill-typed-timestamps",
        help="Backfill typed timestamp columns in short committed batches.",
    )
    typed_timestamps.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    typed_timestamps.add_argument("--batch-size", type=int, default=5_000)
    typed_timestamps.add_argument("--max-batches", type=int, default=0, help="Use 0 to continue until complete.")
    typed_timestamps.set_defaults(func=command_backfill_typed_timestamps)

    start_execution = subparsers.add_parser("start-execution", help="Create or update one ingestion execution row.")
    start_execution.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    add_execution_arguments(start_execution, require_execution_id=True)
    start_execution.set_defaults(func=command_start_execution)

    finalize_execution = subparsers.add_parser("finalize-execution", help="Finalize one ingestion execution row.")
    finalize_execution.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    finalize_execution.add_argument("--execution-id", required=True)
    finalize_execution.add_argument("--status", choices=["healthy", "degraded", "failing", "cancelled"], required=True)
    finalize_execution.add_argument("--completed-at")
    finalize_execution.set_defaults(func=command_finalize_execution)

    web_429_breaker = subparsers.add_parser(
        "check-web-429-circuit-breaker",
        help="Fail fast when recent App Store web catalog requests are mostly HTTP 429.",
    )
    web_429_breaker.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_429_breaker.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_429_breaker.add_argument("--since")
    web_429_breaker.add_argument("--lookback-minutes", type=int, default=60)
    web_429_breaker.add_argument("--min-pages", type=int, default=4)
    web_429_breaker.add_argument("--max-rate", type=float, default=0.5)
    web_429_breaker.set_defaults(func=command_check_web_429_circuit_breaker)

    web_429_cooldown = subparsers.add_parser(
        "check-web-429-cooldown",
        help="Fail fast when the most recent App Store web catalog HTTP 429 is still inside the cooldown window.",
    )
    web_429_cooldown.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_429_cooldown.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_429_cooldown.add_argument("--cooldown-minutes", type=int, default=180)
    web_429_cooldown.set_defaults(func=command_check_web_429_cooldown)

    web_pressure = subparsers.add_parser(
        "select-web-catalog-pressure",
        help="Choose the scheduled web catalog page cap from recent clean request history.",
    )
    web_pressure.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_pressure.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_pressure.add_argument("--lookback-minutes", type=int, default=720)
    web_pressure.add_argument("--base-pages", type=int, default=5)
    web_pressure.add_argument("--max-pages", type=int, default=25)
    web_pressure.add_argument("--base-parallel", type=int, default=1)
    web_pressure.add_argument("--max-parallel", type=int, default=4)
    web_pressure.add_argument("--base-scope-time-budget-seconds", type=int, default=1800)
    web_pressure.add_argument("--max-scope-time-budget-seconds", type=int, default=7200)
    web_pressure.add_argument("--selection-mode", choices=["safe", "candidate"], default="safe")
    web_pressure.set_defaults(func=command_select_web_catalog_pressure)

    web_pressure_record = subparsers.add_parser(
        "record-web-catalog-pressure-result",
        help="Record the latest scheduled web catalog pressure result and choose the next page cap.",
    )
    web_pressure_record.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_pressure_record.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_pressure_record.add_argument("--since", required=True)
    web_pressure_record.add_argument("--used-pages", type=int, required=True)
    web_pressure_record.add_argument("--used-parallel", type=int, default=1)
    web_pressure_record.add_argument("--used-scope-time-budget-seconds", type=int, default=1800)
    web_pressure_record.add_argument("--base-pages", type=int, default=5)
    web_pressure_record.add_argument("--max-pages", type=int, default=25)
    web_pressure_record.add_argument("--base-parallel", type=int, default=1)
    web_pressure_record.add_argument("--max-parallel", type=int, default=4)
    web_pressure_record.add_argument("--base-scope-time-budget-seconds", type=int, default=1800)
    web_pressure_record.add_argument("--max-scope-time-budget-seconds", type=int, default=7200)
    web_pressure_record.add_argument("--cooldown-minutes", type=int, default=30)
    web_pressure_record.set_defaults(func=command_record_web_catalog_pressure_result)

    load = subparsers.add_parser("load", aliases=["load-postgres"], help="Load raw web-catalog run files into Postgres.")
    load.add_argument("--raw-dir", type=Path, required=True)
    load.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    load.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    load.set_defaults(func=command_load_postgres)

    validate = subparsers.add_parser("validate", aliases=["validate-postgres"], help="Validate the Postgres database.")
    validate.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    validate.add_argument("--run-id")
    validate.add_argument("--output", type=Path)
    validate.set_defaults(func=command_validate_postgres)

    eda = subparsers.add_parser("eda-report", help="Generate the App Store review EDA and data-quality report from Postgres.")
    eda.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    eda.add_argument("--source", default=WEB_CATALOG_SOURCE)
    eda.add_argument("--markdown-output", type=Path, default=DEFAULT_EDA_MARKDOWN)
    eda.add_argument("--json-output", type=Path, default=DEFAULT_EDA_JSON)
    eda.add_argument("--html-output", type=Path, default=DEFAULT_EDA_HTML)
    eda.set_defaults(func=command_eda_report)

    operating = subparsers.add_parser(
        "operating-report",
        help="Generate the daily incremental operating-limits report from Postgres and a GitHub run ledger.",
    )
    operating.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    operating.add_argument("--source", default=WEB_CATALOG_SOURCE)
    operating.add_argument("--ledger", type=Path, default=DEFAULT_OPERATING_LEDGER)
    operating.add_argument("--markdown-output", type=Path, default=DEFAULT_OPERATING_MARKDOWN)
    operating.add_argument("--json-output", type=Path, default=DEFAULT_OPERATING_JSON)
    operating.add_argument("--grace-minutes", type=int, default=5)
    operating.set_defaults(func=command_operating_report)

    ledger_run = subparsers.add_parser(
        "operating-ledger-upsert-run",
        help="Insert or update one GitHub Actions run in the operating-model experiment ledger.",
    )
    ledger_run.add_argument("--ledger", type=Path, default=DEFAULT_OPERATING_LEDGER)
    ledger_run.add_argument("--repo", default="XVvvVX-bot/app-store-review-pipeline")
    ledger_run.add_argument("--github-run-id", required=True)
    ledger_run.add_argument("--label", required=True)
    ledger_run.add_argument("--comparison-group", required=True)
    ledger_run.add_argument("--experiment-group", default="")
    ledger_run.add_argument("--status")
    ledger_run.add_argument("--notes", default="")
    ledger_run.add_argument("--input", action="append", default=[], help="Run input as key=value. May be repeated.")
    ledger_run.add_argument("--run-json", type=Path, help="Optional gh run view JSON payload for offline/test use.")
    ledger_run.set_defaults(func=command_operating_ledger_upsert_run)

    monitor = subparsers.add_parser(
        "monitoring-report",
        help="Generate a GitHub-native health report for daily incremental ingestion.",
    )
    monitor.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    monitor.add_argument("--source", default=WEB_CATALOG_SOURCE)
    monitor.add_argument("--since", required=True)
    monitor.add_argument("--selected-count", type=int, required=True)
    monitor.add_argument("--workflow-result", required=True)
    monitor.add_argument("--github-run-id", default="")
    monitor.add_argument("--github-run-url", default="")
    monitor.add_argument("--github-event-name", default="")
    monitor.add_argument("--github-run-attempt", type=int, default=1)
    monitor.add_argument("--execution-id", default="")
    monitor.add_argument("--github-jobs-json", type=Path)
    monitor.add_argument("--github-runs-json", type=Path)
    monitor.add_argument("--markdown-output", type=Path, default=DEFAULT_MONITORING_MARKDOWN)
    monitor.add_argument("--json-output", type=Path, default=DEFAULT_MONITORING_JSON)
    monitor.add_argument("--fail-on", choices=["never", "degraded", "failing"], default="failing")
    monitor.add_argument("--require-recent-scheduled-run", action="store_true")
    monitor.add_argument("--schedule-lookback-minutes", type=int, default=2160)
    monitor.set_defaults(func=command_monitoring_report)

    monitor_email = subparsers.add_parser(
        "send-monitoring-email",
        help="Send a short SMTP email for an eligible failing monitoring report.",
    )
    monitor_email.add_argument("--report-json", type=Path, required=True)
    monitor_email.add_argument("--result-json", type=Path, default=DEFAULT_NOTIFICATION_RESULT)
    monitor_email.add_argument("--preview-output", type=Path, default=DEFAULT_NOTIFICATION_PREVIEW)
    monitor_email.add_argument("--dry-run", action="store_true")
    monitor_email.add_argument("--force", action="store_true")
    monitor_email.set_defaults(func=command_send_monitoring_email)

    fallback_monitor = subparsers.add_parser(
        "fallback-monitoring-report",
        help="Write a minimal failing report when the primary monitor cannot produce one.",
    )
    fallback_monitor.add_argument("--json-output", type=Path, default=DEFAULT_MONITORING_JSON)
    fallback_monitor.add_argument("--failure-code", required=True)
    fallback_monitor.add_argument("--failure-message", required=True)
    fallback_monitor.add_argument("--github-run-id", default="")
    fallback_monitor.add_argument("--github-run-url", default="")
    fallback_monitor.add_argument("--github-event-name", default="")
    fallback_monitor.add_argument("--github-run-attempt", type=int, default=1)
    fallback_monitor.add_argument("--workflow-result", default="failure")
    fallback_monitor.set_defaults(func=command_fallback_monitoring_report)

    daily_web_catalog = subparsers.add_parser(
        "daily-web-catalog",
        help="Fetch, load, validate, and report public App Store web catalog reviews.",
    )
    add_web_catalog_fetch_arguments(daily_web_catalog)
    daily_web_catalog.add_argument("--reports-root", type=Path, default=DEFAULT_WEB_CATALOG_REPORTS_ROOT)
    daily_web_catalog.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    daily_web_catalog.add_argument(
        "--disable-overlap-stop",
        action="store_true",
        help="Fetch to page cap even after already-known web catalog review IDs are seen.",
    )
    daily_web_catalog.add_argument(
        "--assume-postgres-schema",
        action="store_true",
        help="Skip schema initialization because an upstream preflight already initialized Postgres.",
    )
    daily_web_catalog.add_argument(
        "--skip-target-sync",
        action="store_true",
        help="Skip syncing repository targets because an upstream preflight already synced them.",
    )
    daily_web_catalog.add_argument(
        "--resume-backlogged-scopes",
        action="store_true",
        help="Resume recent incomplete app-country scopes from a safety-overlapped page checkpoint.",
    )
    daily_web_catalog.add_argument(
        "--backlog-resume-overlap-pages",
        type=int,
        default=25,
        help="Number of already-fetched pages to revisit before a backlog checkpoint.",
    )
    daily_web_catalog.add_argument(
        "--backlog-resume-lookback-attempts",
        type=int,
        default=4,
        help="Number of recent incomplete attempts considered when choosing a backlog checkpoint.",
    )
    daily_web_catalog.add_argument(
        "--backlog-resume-max-age-hours",
        type=int,
        default=36,
        help="Maximum age of an incomplete attempt eligible for backlog recovery.",
    )
    add_execution_arguments(daily_web_catalog)
    daily_web_catalog.set_defaults(func=command_daily_web_catalog)

    return parser


def add_execution_arguments(parser: argparse.ArgumentParser, *, require_execution_id: bool = False) -> None:
    parser.add_argument("--execution-id", required=require_execution_id, default="")
    parser.add_argument("--execution-started-at", default="")
    parser.add_argument("--github-run-id", default="")
    parser.add_argument("--github-run-attempt", type=int, default=1)
    parser.add_argument("--github-workflow", default="")
    parser.add_argument("--github-event-name", default="")
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--worker-key", default="")
    parser.add_argument("--scope-signature", default="")
    parser.add_argument("--config-signature", default="")
    parser.add_argument("--intended-target-count", type=int, default=0)
    parser.add_argument("--intended-scope-count", type=int, default=0)


def add_web_catalog_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_WEB_CATALOG_RAW_ROOT)
    parser.add_argument("--sort-by", default=WEB_CATALOG_SORT_BY, choices=["recent", "helpful", "favorable", "critical"])
    parser.add_argument("--limit", type=int, default=1, help="Maximum active targets to fetch. Use 0 for all.")
    parser.add_argument("--target-offset", type=int, default=0, help="Number of active targets to skip before applying limit.")
    parser.add_argument("--max-pages-per-app-country", type=int, default=25)
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First web catalog page number to fetch; use values above 1 for manual depth-limit probes.",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="Requested web catalog reviews per page; Apple currently caps this at 20.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--request-delay-seconds", type=float, default=5.0)
    parser.add_argument(
        "--request-delay-jitter-seconds",
        type=float,
        default=0.0,
        help="Positive random jitter added to each normal web catalog request delay.",
    )
    parser.add_argument("--web-429-retries", type=int, default=5)
    parser.add_argument("--web-429-retry-seconds", type=float, default=300.0)
    parser.add_argument("--web-429-backoff-multiplier", type=float, default=1.5)
    parser.add_argument(
        "--web-429-retry-jitter-seconds",
        type=float,
        default=0.0,
        help="Positive random jitter added to each HTTP 429 retry sleep.",
    )
    parser.add_argument(
        "--web-soft-retries",
        type=int,
        default=2,
        help="Retries for transient empty, malformed, or otherwise unparsable 2xx web catalog responses.",
    )
    parser.add_argument(
        "--web-soft-retry-seconds",
        type=float,
        default=5.0,
        help="Delay between transient web catalog soft-error retries.",
    )
    parser.add_argument(
        "--web-time-budget-seconds",
        type=float,
        default=0.0,
        help="Optional wall-clock budget for web catalog fetching. Use 0 for no budget.",
    )
    parser.add_argument(
        "--web-scope-time-budget-seconds",
        type=float,
        default=0.0,
        help="Optional per app-country wall-clock budget for web catalog fetching. Use 0 for no per-scope budget.",
    )
    parser.add_argument(
        "--stop-at-rss-parity",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def command_targets(args: argparse.Namespace) -> int:
    targets = load_targets(args.targets)
    active = active_targets(targets)
    print(
        json.dumps(
            {
                "targets_path": str(args.targets),
                "target_count": len(targets),
                "active_target_count": len(active),
                "app_country_scope_count": sum(len(target.countries) for target in active),
                "categories": sorted({target.category for target in active}),
                "source": WEB_CATALOG_SOURCE,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_sync_targets(args: argparse.Namespace) -> int:
    report = sync_targets_postgres(args.database_url, args.targets, make_run_id())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_fetch_web_catalog(args: argparse.Namespace) -> int:
    targets = select_target_window(active_targets(load_targets(args.targets)), limit=args.limit, offset=args.target_offset)
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    report = fetch_web_catalog_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        start_page=args.start_page,
        review_limit=args.review_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        request_delay_jitter_seconds=getattr(args, "request_delay_jitter_seconds", 0.0),
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        web_429_retry_jitter_seconds=getattr(args, "web_429_retry_jitter_seconds", 0.0),
        web_soft_retries=args.web_soft_retries,
        web_soft_retry_seconds=args.web_soft_retry_seconds,
        time_budget_seconds=args.web_time_budget_seconds,
        scope_time_budget_seconds=args.web_scope_time_budget_seconds,
        known_review_ids_by_scope={},
        use_overlap_stop=False,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", report["reviews"])
    write_json(raw_dir / "fetch_report.json", report)
    print(json.dumps({"raw_dir": str(raw_dir), "summary": summarize_fetch_cli(report)}, indent=2, sort_keys=True))
    return 0


def summarize_fetch_cli(report: dict) -> dict:
    page_reports = report.get("page_reports", [])
    warning_scopes = report.get("warning_scopes", []) or []
    status_counts: dict[str, int] = {}
    status_code_counts: dict[str, int] = {}
    attempt_counts: dict[str, int] = {}
    terminal_reasons: dict[str, int] = {}
    warning_scope_reasons: dict[str, int] = {}
    retried_pages = 0
    successful_after_retry_pages = 0
    final_non_200_pages = 0
    missing_text = 0
    missing_rating = 0
    for row in page_reports:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        status_code = row.get("status_code")
        if status_code is not None:
            key = str(status_code)
            status_code_counts[key] = status_code_counts.get(key, 0) + 1
            if not (200 <= int(status_code) < 300):
                final_non_200_pages += 1
        attempt_count = int(row.get("attempt_count") or 0)
        if attempt_count:
            key = str(attempt_count)
            attempt_counts[key] = attempt_counts.get(key, 0) + 1
        if attempt_count > 1:
            retried_pages += 1
            if row.get("status") == "ok":
                successful_after_retry_pages += 1
        reason = row.get("terminal_reason")
        if reason:
            terminal_reasons[str(reason)] = terminal_reasons.get(str(reason), 0) + 1
        missing_text += int(row.get("missing_text_count") or 0)
        missing_rating += int(row.get("missing_rating_count") or 0)
    for scope in warning_scopes:
        reason = str(scope.get("reason") or "unknown")
        warning_scope_reasons[reason] = warning_scope_reasons.get(reason, 0) + 1

    return {
        "pages": len(page_reports),
        "reviews": report.get("review_count", 0),
        "unique_reviews": report.get("unique_review_count", 0),
        "fetch_errors": report.get("fetch_errors", 0),
        "capped_scopes": len(report.get("capped_scopes", [])),
        "warning_scope_count": len(warning_scopes),
        "warning_scope_reasons": warning_scope_reasons,
        "sparse_empty_pages": report.get("sparse_empty_pages", 0),
        "status_counts": status_counts,
        "status_code_counts": status_code_counts,
        "attempt_counts": attempt_counts,
        "retried_pages": retried_pages,
        "successful_after_retry_pages": successful_after_retry_pages,
        "final_non_200_pages": final_non_200_pages,
        "terminal_reasons": terminal_reasons,
        "missing_text": missing_text,
        "missing_rating": missing_rating,
        "overall_time_budget_exceeded": bool(report.get("overall_time_budget_exceeded", False)),
        "scope_time_budget_seconds": report.get("scope_time_budget_seconds", 0),
        "resumed_scope_count": len(report.get("resumed_scopes", [])),
        "resumed_scopes": report.get("resumed_scopes", []),
        "all_pages_ok_after_retry": bool(page_reports) and final_non_200_pages == 0 and report.get("fetch_errors", 0) == 0,
    }


def command_init_postgres(args: argparse.Namespace) -> int:
    initialize_postgres(args.database_url)
    print(json.dumps({"database_url": mask_database_url(args.database_url), "initialized": True}, indent=2, sort_keys=True))
    return 0


def command_backfill_typed_timestamps(args: argparse.Namespace) -> int:
    result = backfill_typed_timestamps_postgres(
        args.database_url,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_start_execution(args: argparse.Namespace) -> int:
    started_at = args.execution_started_at or utc_timestamp()
    result = upsert_execution_postgres(
        args.database_url,
        execution_id=args.execution_id,
        source=WEB_CATALOG_SOURCE,
        started_at=started_at,
        github_run_id=args.github_run_id,
        github_run_attempt=args.github_run_attempt,
        workflow_name=args.github_workflow,
        event_name=args.github_event_name,
        git_sha=args.git_sha,
        scope_signature=args.scope_signature,
        config_signature=args.config_signature,
        intended_target_count=args.intended_target_count,
        intended_scope_count=args.intended_scope_count,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_finalize_execution(args: argparse.Namespace) -> int:
    result = finalize_execution_postgres(
        args.database_url,
        execution_id=args.execution_id,
        status=args.status,
        completed_at=args.completed_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_check_web_429_circuit_breaker(args: argparse.Namespace) -> int:
    status = web_catalog_429_circuit_breaker_status(
        args.database_url,
        source=args.source,
        since=args.since,
        lookback_minutes=args.lookback_minutes,
        min_pages=args.min_pages,
        max_rate=args.max_rate,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 2 if status["tripped"] else 0


def command_check_web_429_cooldown(args: argparse.Namespace) -> int:
    status = web_catalog_429_cooldown_status(
        args.database_url,
        source=args.source,
        cooldown_minutes=args.cooldown_minutes,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 2 if status["tripped"] else 0


def command_select_web_catalog_pressure(args: argparse.Namespace) -> int:
    status = web_catalog_pressure_status(
        args.database_url,
        source=args.source,
        lookback_minutes=args.lookback_minutes,
        base_pages=args.base_pages,
        max_pages=args.max_pages,
        base_parallel=getattr(args, "base_parallel", 1),
        max_parallel=getattr(args, "max_parallel", 4),
        base_scope_time_budget_seconds=getattr(args, "base_scope_time_budget_seconds", 1800),
        max_scope_time_budget_seconds=getattr(args, "max_scope_time_budget_seconds", 7200),
        selection_mode=getattr(args, "selection_mode", "safe"),
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def command_record_web_catalog_pressure_result(args: argparse.Namespace) -> int:
    status = record_web_catalog_pressure_result(
        args.database_url,
        source=args.source,
        since=args.since,
        used_pages=args.used_pages,
        used_parallel=args.used_parallel,
        used_scope_time_budget_seconds=args.used_scope_time_budget_seconds,
        base_pages=args.base_pages,
        max_pages=args.max_pages,
        base_parallel=args.base_parallel,
        max_parallel=args.max_parallel,
        base_scope_time_budget_seconds=args.base_scope_time_budget_seconds,
        max_scope_time_budget_seconds=args.max_scope_time_budget_seconds,
        cooldown_minutes=args.cooldown_minutes,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def command_load_postgres(args: argparse.Namespace) -> int:
    summary = load_pipeline_run_postgres(args.database_url, args.raw_dir, args.targets)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_validate_postgres(args: argparse.Namespace) -> int:
    report = validate_postgres(args.database_url, args.run_id)
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def command_eda_report(args: argparse.Namespace) -> int:
    report = generate_eda_report(
        args.database_url,
        source=args.source,
        markdown_path=args.markdown_output,
        json_path=args.json_output,
        html_path=args.html_output,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_operating_report(args: argparse.Namespace) -> int:
    report = generate_operating_report(
        args.database_url,
        source=args.source,
        ledger_path=args.ledger,
        markdown_path=args.markdown_output,
        json_path=args.json_output,
        grace_minutes=args.grace_minutes,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def parse_key_value_pairs(values: list[str]) -> dict[str, str]:
    output = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected key=value input, got {value!r}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Expected non-empty key in {value!r}")
        output[key] = raw
    return output


def command_operating_ledger_upsert_run(args: argparse.Namespace) -> int:
    run_payload = (
        json.loads(args.run_json.read_text(encoding="utf-8"))
        if args.run_json
        else fetch_github_run_payload(args.github_run_id, repo=args.repo)
    )
    entry = build_operating_ledger_run_entry(
        run_payload,
        label=args.label,
        comparison_group=args.comparison_group,
        experiment_group=args.experiment_group,
        status=args.status,
        inputs=parse_key_value_pairs(args.input),
        notes=args.notes,
    )
    result = upsert_operating_ledger_run(args.ledger, entry)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_monitoring_report(args: argparse.Namespace) -> int:
    result = generate_monitoring_report(
        args.database_url,
        source=args.source,
        since=args.since,
        selected_count=args.selected_count,
        workflow_result=args.workflow_result,
        github_run_id=args.github_run_id,
        github_run_url=args.github_run_url,
        github_event_name=args.github_event_name,
        github_run_attempt=args.github_run_attempt,
        execution_id=getattr(args, "execution_id", ""),
        github_jobs_json=args.github_jobs_json,
        github_runs_json=args.github_runs_json,
        markdown_path=args.markdown_output,
        json_path=args.json_output,
        fail_on=args.fail_on,
        require_recent_scheduled_run=args.require_recent_scheduled_run,
        schedule_lookback_minutes=args.schedule_lookback_minutes,
    )
    summary = json.loads(args.json_output.read_text(encoding="utf-8"))
    emit_github_annotations(summary)
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result["exit_code"])


def command_send_monitoring_email(args: argparse.Namespace) -> int:
    try:
        result = send_monitoring_email(
            args.report_json,
            result_path=args.result_json,
            preview_path=args.preview_output,
            dry_run=args.dry_run,
            force=args.force,
        )
    except Exception as exc:
        if args.result_json.exists():
            result = json.loads(args.result_json.read_text(encoding="utf-8"))
        else:
            result = {
                "status": "failed",
                "reason": "notification_generation_failed",
                "error_type": type(exc).__name__,
            }
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            args.result_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("::error title=monitoring_email_delivery_failed::SMTP delivery failed; inspect notification_result.json.")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    if result.get("status") == "not_configured":
        print("::error title=monitoring_email_not_configured::Failing email was eligible but SMTP secrets are missing.")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_fallback_monitoring_report(args: argparse.Namespace) -> int:
    summary = write_fallback_failure_report(
        args.json_output,
        failure_code=args.failure_code,
        failure_message=args.failure_message,
        github_run_id=args.github_run_id,
        github_run_url=args.github_run_url,
        github_event_name=args.github_event_name,
        github_run_attempt=args.github_run_attempt,
        workflow_result=args.workflow_result,
    )
    print(json.dumps({"status": summary["status"], "json_path": str(args.json_output)}, indent=2, sort_keys=True))
    return 0


def select_target_window(targets: list, *, limit: int, offset: int = 0) -> list:
    start = max(0, offset)
    window = targets[start:]
    return window[:limit] if limit > 0 else window


def stable_signature(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def command_daily_web_catalog(args: argparse.Namespace) -> int:
    started_at = utc_timestamp()
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    reports_dir = args.reports_root / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    initialize_schema = not bool(getattr(args, "assume_postgres_schema", False))
    if bool(getattr(args, "skip_target_sync", False)):
        target_sync_summary = {
            "run_id": run_id,
            "targets_path": str(args.targets),
            "skipped": True,
            "reason": "upstream_preflight",
        }
    else:
        target_sync_summary = sync_targets_postgres(
            args.database_url,
            args.targets,
            run_id,
            initialize_schema=initialize_schema,
        )
    targets = select_target_window(active_targets(load_targets(args.targets)), limit=args.limit, offset=args.target_offset)
    scopes = [(target.apple_app_id, country, args.sort_by) for target in targets for country in target.countries]
    expected_scopes = [
        {
            "app_id": target.apple_app_id,
            "app_name": target.app_name,
            "country": country,
            "sort_by": args.sort_by,
        }
        for target in targets
        for country in target.countries
    ]
    github_run_id = str(getattr(args, "github_run_id", "") or "")
    github_run_attempt = max(1, int(getattr(args, "github_run_attempt", 1) or 1))
    execution_id = str(getattr(args, "execution_id", "") or f"local:{run_id}")
    execution_started_at = str(getattr(args, "execution_started_at", "") or started_at)
    scope_signature = str(getattr(args, "scope_signature", "") or stable_signature(scopes))
    config_signature = str(
        getattr(args, "config_signature", "")
        or stable_signature(
            {
                "sort_by": args.sort_by,
                "max_pages": args.max_pages_per_app_country,
                "start_page": args.start_page,
                "review_limit": args.review_limit,
                "request_delay": args.request_delay_seconds,
                "request_jitter": getattr(args, "request_delay_jitter_seconds", 0.0),
                "scope_time_budget": args.web_scope_time_budget_seconds,
                "overlap_stop": not getattr(args, "disable_overlap_stop", False),
                "resume_backlogged_scopes": bool(getattr(args, "resume_backlogged_scopes", False)),
                "backlog_resume_overlap_pages": int(getattr(args, "backlog_resume_overlap_pages", 25)),
            }
        )
    )
    intended_target_count = max(0, int(getattr(args, "intended_target_count", 0) or len(targets)))
    intended_scope_count = max(0, int(getattr(args, "intended_scope_count", 0) or len(scopes)))
    upsert_execution_postgres(
        args.database_url,
        execution_id=execution_id,
        source=WEB_CATALOG_SOURCE,
        started_at=execution_started_at,
        github_run_id=github_run_id,
        github_run_attempt=github_run_attempt,
        workflow_name=str(getattr(args, "github_workflow", "") or "local"),
        event_name=str(getattr(args, "github_event_name", "") or "local"),
        git_sha=str(getattr(args, "git_sha", "") or ""),
        scope_signature=scope_signature,
        config_signature=config_signature,
        intended_target_count=intended_target_count,
        intended_scope_count=intended_scope_count,
        initialize_schema=initialize_schema,
    )
    use_overlap_stop = not getattr(args, "disable_overlap_stop", False)
    known_ids = (
        trusted_existing_review_ids_by_scope(
            args.database_url,
            scopes,
            source=WEB_CATALOG_SOURCE,
            initialize_schema=initialize_schema,
        )
        if use_overlap_stop
        else {}
    )
    target_review_counts = (
        review_counts_by_scope(
            args.database_url,
            scopes,
            source=SOURCE,
            initialize_schema=initialize_schema,
        )
        if getattr(args, "stop_at_rss_parity", False)
        else {}
    )
    resume_backlogged_scopes = bool(getattr(args, "resume_backlogged_scopes", False))
    if resume_backlogged_scopes and int(args.start_page) != 1:
        raise ValueError("--resume-backlogged-scopes requires --start-page 1")
    scope_start_pages = (
        backlogged_resume_start_pages_by_scope(
            args.database_url,
            scopes,
            source=WEB_CATALOG_SOURCE,
            overlap_pages=getattr(args, "backlog_resume_overlap_pages", 25),
            lookback_attempts=getattr(args, "backlog_resume_lookback_attempts", 4),
            max_age_hours=getattr(args, "backlog_resume_max_age_hours", 36),
            initialize_schema=initialize_schema,
        )
        if resume_backlogged_scopes
        else {}
    )
    fetch_report = fetch_web_catalog_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        start_page=args.start_page,
        start_pages_by_scope=scope_start_pages,
        review_limit=args.review_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        request_delay_jitter_seconds=getattr(args, "request_delay_jitter_seconds", 0.0),
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        web_429_retry_jitter_seconds=getattr(args, "web_429_retry_jitter_seconds", 0.0),
        web_soft_retries=args.web_soft_retries,
        web_soft_retry_seconds=args.web_soft_retry_seconds,
        time_budget_seconds=args.web_time_budget_seconds,
        scope_time_budget_seconds=args.web_scope_time_budget_seconds,
        known_review_ids_by_scope=known_ids,
        target_review_counts_by_scope=target_review_counts,
        use_overlap_stop=use_overlap_stop,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", fetch_report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", fetch_report["reviews"])
    write_json(raw_dir / "fetch_report.json", fetch_report)
    load_summary = load_pipeline_run_postgres(
        args.database_url,
        raw_dir,
        args.targets,
        execution_id=execution_id,
        github_run_id=github_run_id or None,
        github_run_attempt=github_run_attempt if github_run_id else None,
        worker_key=str(getattr(args, "worker_key", "") or "local"),
        selected_target_count=len(targets),
        initialize_schema=initialize_schema,
    )
    completed_at = utc_timestamp()
    sync_summary = update_sync_states_postgres(
        args.database_url,
        fetch_report["page_reports"],
        fetch_report["reviews"],
        run_id=run_id,
        sort_by=args.sort_by,
        started_at=started_at,
        completed_at=completed_at,
        source=WEB_CATALOG_SOURCE,
        initialize_schema=initialize_schema,
    )
    scope_summary = record_run_scopes_postgres(
        args.database_url,
        run_id=run_id,
        execution_id=execution_id,
        source=WEB_CATALOG_SOURCE,
        sort_by=args.sort_by,
        started_at=started_at,
        completed_at=completed_at,
        expected_scopes=expected_scopes,
        page_rows=fetch_report["page_reports"],
        review_summary=load_summary,
        initialize_schema=initialize_schema,
    )
    validation_report = validate_postgres(args.database_url, run_id, initialize_schema=initialize_schema)
    validation_path = reports_dir / "validation_report.json"
    write_json(validation_path, validation_report)
    report = {
        "run_id": run_id,
        "execution_id": execution_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "targets_path": str(args.targets),
        "raw_dir": str(raw_dir),
        "database_url": mask_database_url(args.database_url),
        "storage_backend": "postgres",
        "platform": "apple_app_store",
        "source": WEB_CATALOG_SOURCE,
        "sort_by": args.sort_by,
        "target_count": len(targets),
        "scope_count": len(scopes),
        "target_sync_summary": target_sync_summary,
        "target_offset": max(0, args.target_offset),
        "max_pages_per_app_country": args.max_pages_per_app_country,
        "start_page": args.start_page,
        "resume_backlogged_scopes": resume_backlogged_scopes,
        "backlog_resume_overlap_pages": getattr(args, "backlog_resume_overlap_pages", 25),
        "backlog_resume_lookback_attempts": getattr(args, "backlog_resume_lookback_attempts", 4),
        "backlog_resume_max_age_hours": getattr(args, "backlog_resume_max_age_hours", 36),
        "effective_start_pages_by_scope": fetch_report.get("start_pages_by_scope", {}),
        "resumed_scopes": fetch_report.get("resumed_scopes", []),
        "review_limit": args.review_limit,
        "request_delay_seconds": args.request_delay_seconds,
        "request_delay_jitter_seconds": getattr(args, "request_delay_jitter_seconds", 0.0),
        "web_429_retries": args.web_429_retries,
        "web_429_retry_seconds": args.web_429_retry_seconds,
        "web_429_retry_jitter_seconds": getattr(args, "web_429_retry_jitter_seconds", 0.0),
        "web_time_budget_seconds": args.web_time_budget_seconds,
        "web_scope_time_budget_seconds": args.web_scope_time_budget_seconds,
        "overlap_stop_enabled": use_overlap_stop,
        "stop_at_rss_parity": bool(getattr(args, "stop_at_rss_parity", False)),
        "rss_parity_source": SOURCE if getattr(args, "stop_at_rss_parity", False) else None,
        "rss_parity_target_scope_count": len(target_review_counts),
        "fetch_summary": summarize_fetch_cli(fetch_report),
        "load_summary": load_summary,
        "sync_summary": sync_summary,
        "scope_summary": scope_summary,
        "validation_report_path": str(validation_path),
        "report_path": str(reports_dir / "daily_report.json"),
    }
    write_json(reports_dir / "daily_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not github_run_id:
        local_status = "failing" if scope_summary["hard_failure_scope_count"] else (
            "degraded" if scope_summary["backlogged_scope_count"] else "healthy"
        )
        finalize_execution_postgres(
            args.database_url,
            execution_id=execution_id,
            status=local_status,
            completed_at=completed_at,
            initialize_schema=initialize_schema,
        )
    return 1 if scope_summary["hard_failure_scope_count"] else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
