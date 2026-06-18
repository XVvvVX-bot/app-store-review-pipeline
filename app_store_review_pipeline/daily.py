from __future__ import annotations

import argparse
import time
from typing import Callable

from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.postgres_database import (
    existing_review_ids_by_scope,
    load_pipeline_run_postgres,
    mask_database_url,
    update_sync_states_postgres,
    validate_postgres,
)
from app_store_review_pipeline.targets import active_targets, load_targets
from app_store_review_pipeline.utils import make_run_id, utc_timestamp


def run_daily_pipeline(args: argparse.Namespace, sleep_fn: Callable[[float], None] = time.sleep) -> dict:
    started_at = utc_timestamp()
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    reports_dir = args.reports_root / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    targets = active_targets(load_targets(args.targets))
    scopes = [(target.apple_app_id, country, args.sort_by) for target in targets for country in target.countries]
    use_overlap_stop = not getattr(args, "disable_overlap_stop", False)
    known_ids = existing_review_ids_by_scope(args.database_url, scopes) if use_overlap_stop else {}

    fetch_report = fetch_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        max_attempts=args.max_attempts,
        retry_delay_seconds=args.retry_delay_seconds,
        known_review_ids_by_scope=known_ids,
        use_overlap_stop=use_overlap_stop,
        sleep_fn=sleep_fn,
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
        "source": "apple_itunes_customerreviews_rss",
        "sort_by": args.sort_by,
        "target_count": len(targets),
        "scope_count": len(scopes),
        "max_pages_per_app_country": args.max_pages_per_app_country,
        "overlap_stop_enabled": use_overlap_stop,
        "fetch_summary": summarize_fetch(fetch_report),
        "load_summary": load_summary,
        "sync_summary": sync_summary,
        "validation_report_path": str(validation_path),
        "report_path": str(reports_dir / "daily_report.json"),
    }
    write_json(reports_dir / "daily_report.json", report)
    return report


def summarize_fetch(fetch_report: dict) -> dict:
    page_reports = fetch_report.get("page_reports", [])
    terminal_reasons: dict[str, int] = {}
    for row in page_reports:
        reason = row.get("terminal_reason")
        if reason:
            terminal_reasons[reason] = terminal_reasons.get(reason, 0) + 1
    return {
        "page_count": len(page_reports),
        "fetched_pages": fetch_report.get("fetched_pages", 0),
        "empty_pages": fetch_report.get("empty_pages", 0),
        "fetch_errors": fetch_report.get("fetch_errors", 0),
        "reviews_seen": fetch_report.get("review_count", 0),
        "unique_reviews_seen": fetch_report.get("unique_review_count", 0),
        "capped_scopes": fetch_report.get("capped_scopes", []),
        "warning_scopes": fetch_report.get("warning_scopes", []),
        "terminal_reasons": terminal_reasons,
        "gap_warning": bool(fetch_report.get("capped_scopes") or fetch_report.get("warning_scopes")),
    }
