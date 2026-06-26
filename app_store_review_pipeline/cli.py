from __future__ import annotations

import argparse
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
from app_store_review_pipeline.postgres_database import (
    initialize_postgres,
    load_pipeline_run_postgres,
    mask_database_url,
    record_web_catalog_pressure_result,
    review_counts_by_scope,
    sync_targets_postgres,
    trusted_existing_review_ids_by_scope,
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
    daily_web_catalog.set_defaults(func=command_daily_web_catalog)

    return parser


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
        "all_pages_ok_after_retry": bool(page_reports) and final_non_200_pages == 0 and report.get("fetch_errors", 0) == 0,
    }


def command_init_postgres(args: argparse.Namespace) -> int:
    initialize_postgres(args.database_url)
    print(json.dumps({"database_url": mask_database_url(args.database_url), "initialized": True}, indent=2, sort_keys=True))
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


def select_target_window(targets: list, *, limit: int, offset: int = 0) -> list:
    start = max(0, offset)
    window = targets[start:]
    return window[:limit] if limit > 0 else window


def command_daily_web_catalog(args: argparse.Namespace) -> int:
    started_at = utc_timestamp()
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    reports_dir = args.reports_root / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    target_sync_summary = sync_targets_postgres(args.database_url, args.targets, run_id)
    targets = select_target_window(active_targets(load_targets(args.targets)), limit=args.limit, offset=args.target_offset)
    scopes = [(target.apple_app_id, country, args.sort_by) for target in targets for country in target.countries]
    use_overlap_stop = not getattr(args, "disable_overlap_stop", False)
    known_ids = (
        trusted_existing_review_ids_by_scope(args.database_url, scopes, source=WEB_CATALOG_SOURCE)
        if use_overlap_stop
        else {}
    )
    target_review_counts = (
        review_counts_by_scope(args.database_url, scopes, source=SOURCE)
        if getattr(args, "stop_at_rss_parity", False)
        else {}
    )
    fetch_report = fetch_web_catalog_targets(
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
        known_review_ids_by_scope=known_ids,
        target_review_counts_by_scope=target_review_counts,
        use_overlap_stop=use_overlap_stop,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", fetch_report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", fetch_report["reviews"])
    write_json(raw_dir / "fetch_report.json", fetch_report)
    load_summary = load_pipeline_run_postgres(args.database_url, raw_dir, args.targets)
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
    )
    validation_report = validate_postgres(args.database_url, run_id)
    validation_path = reports_dir / "validation_report.json"
    write_json(validation_path, validation_report)
    report = {
        "run_id": run_id,
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
        "validation_report_path": str(validation_path),
        "report_path": str(reports_dir / "daily_report.json"),
    }
    write_json(reports_dir / "daily_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
