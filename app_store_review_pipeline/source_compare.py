from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from app_store_review_pipeline.apple_web import probe_web_reviews
from app_store_review_pipeline.config import DEFAULT_SORT_BY
from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.utils import utc_timestamp


def compare_sources(
    targets: list[AppTarget],
    *,
    run_id: str,
    raw_root: Path,
    reports_root: Path,
    sort_by: str = DEFAULT_SORT_BY,
    rss_max_pages_per_app_country: int = 10,
    rss_max_consecutive_empty_pages: int = 10,
    rss_request_delay_seconds: float = 0.5,
    rss_max_attempts: int = 3,
    rss_retry_delay_seconds: float = 5.0,
    web_max_pages: int = 5,
    web_review_limit: int = 20,
    web_request_delay_seconds: float = 2.0,
    web_429_retries: int = 3,
    web_429_retry_seconds: float = 45.0,
    timeout_seconds: float = 20.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    raw_dir = raw_root / run_id
    report_dir = reports_root / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    started_at = utc_timestamp()
    rss_report = fetch_targets(
        targets,
        raw_dir / "rss",
        run_id,
        sort_by=sort_by,
        max_pages_per_app_country=rss_max_pages_per_app_country,
        max_consecutive_empty_pages=rss_max_consecutive_empty_pages,
        timeout_seconds=timeout_seconds,
        request_delay_seconds=rss_request_delay_seconds,
        max_attempts=rss_max_attempts,
        retry_delay_seconds=rss_retry_delay_seconds,
        known_review_ids_by_scope={},
        use_overlap_stop=False,
        sleep_fn=sleep_fn,
    )
    write_jsonl(raw_dir / "rss" / "review_pages.jsonl", rss_report["page_reports"])
    write_jsonl(raw_dir / "rss" / "reviews.jsonl", rss_report["reviews"])
    write_json(raw_dir / "rss" / "fetch_report.json", rss_report)

    web_report_path = report_dir / "web_probe_report.json"
    web_report = probe_web_reviews(
        targets,
        web_report_path,
        limit=0,
        timeout_seconds=timeout_seconds,
        request_delay_seconds=web_request_delay_seconds,
        web_sort="recent",
        attempt_pagination=True,
        max_web_pages=web_max_pages,
        review_limit=web_review_limit,
        web_429_retries=web_429_retries,
        web_429_retry_seconds=web_429_retry_seconds,
        sleep_fn=sleep_fn,
    )

    comparison = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": utc_timestamp(),
        "target_count": len(targets),
        "scope_count": sum(len(target.countries) for target in targets),
        "settings": {
            "sort_by": sort_by,
            "rss_max_pages_per_app_country": rss_max_pages_per_app_country,
            "rss_max_consecutive_empty_pages": rss_max_consecutive_empty_pages,
            "rss_request_delay_seconds": rss_request_delay_seconds,
            "rss_max_attempts": rss_max_attempts,
            "rss_retry_delay_seconds": rss_retry_delay_seconds,
            "web_sort": "recent",
            "web_max_pages": web_max_pages,
            "web_review_limit": web_review_limit,
            "web_request_delay_seconds": web_request_delay_seconds,
            "web_429_retries": web_429_retries,
            "web_429_retry_seconds": web_429_retry_seconds,
            "timeout_seconds": timeout_seconds,
        },
        "rss": summarize_rss_report(rss_report),
        "web_catalog": summarize_web_report(web_report),
        "comparison": summarize_comparison(rss_report, web_report),
        "per_scope": compare_per_scope(rss_report, web_report),
        "paths": {
            "rss_raw_dir": str(raw_dir / "rss"),
            "web_report_path": str(web_report_path),
            "comparison_report_path": str(report_dir / "source_comparison_report.json"),
        },
    }
    write_json(report_dir / "source_comparison_report.json", comparison)
    return comparison


def summarize_rss_report(report: dict[str, Any]) -> dict[str, Any]:
    page_reports = report.get("page_reports", [])
    terminal_reasons: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for page in page_reports:
        status = str(page.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        reason = page.get("terminal_reason")
        if reason:
            terminal_reasons[reason] = terminal_reasons.get(reason, 0) + 1
    return {
        "page_count": len(page_reports),
        "fetched_pages": report.get("fetched_pages", 0),
        "fetch_errors": report.get("fetch_errors", 0),
        "empty_pages": report.get("empty_pages", 0),
        "sparse_empty_pages": report.get("sparse_empty_pages", 0),
        "reviews_seen": report.get("review_count", 0),
        "unique_reviews_seen": report.get("unique_review_count", 0),
        "status_counts": status_counts,
        "terminal_reasons": terminal_reasons,
        "warning_scope_count": len(report.get("warning_scopes") or []),
        "capped_scope_count": len(report.get("capped_scopes") or []),
    }


def summarize_web_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary") or {})
    page_status_counts = summary.get("web_catalog_page_status_counts") or {}
    page_total = sum(int(value) for value in page_status_counts.values())
    ok_pages = int(page_status_counts.get("200") or 0)
    summary["web_catalog_page_success_rate"] = ok_pages / page_total if page_total else None
    return summary


def summarize_comparison(rss_report: dict[str, Any], web_report: dict[str, Any]) -> dict[str, Any]:
    rss_summary = summarize_rss_report(rss_report)
    web_summary = summarize_web_report(web_report)
    rss_reviews = int(rss_summary["unique_reviews_seen"] or 0)
    web_reviews = int(web_summary.get("web_catalog_page_reviews_total") or 0)
    web_to_rss_ratio = web_reviews / rss_reviews if rss_reviews else None
    web_same_order_as_rss = web_reviews > 0 and (
        rss_reviews == 0 or (web_to_rss_ratio is not None and web_to_rss_ratio >= 0.1)
    )
    web_page_status_counts = web_summary.get("web_catalog_page_status_counts") or {}
    web_non_200_pages = sum(
        int(count)
        for status, count in web_page_status_counts.items()
        if str(status) != "200"
    )
    return {
        "web_reviews_minus_rss_reviews": web_reviews - rss_reviews,
        "web_to_rss_review_ratio": web_to_rss_ratio,
        "web_reviews_same_order_as_rss": web_same_order_as_rss,
        "web_reviews_at_or_above_rss": web_reviews >= rss_reviews,
        "rss_fetch_error_count": rss_summary["fetch_errors"],
        "web_non_200_page_count_after_retry": web_non_200_pages,
        "web_all_pages_ok_after_retry": web_non_200_pages == 0,
        "web_recovered_429_page_count": web_summary.get("recovered_429_page_count", 0),
        "web_retried_page_count": web_summary.get("retried_page_count", 0),
        "candidate_passes_single_run_gate": (
            web_reviews > 0
            and web_reviews >= rss_reviews
            and web_non_200_pages == 0
            and rss_summary["fetch_errors"] == 0
        ),
        "candidate_passes_same_order_stability_gate": (
            web_same_order_as_rss
            and web_non_200_pages == 0
            and rss_summary["fetch_errors"] == 0
        ),
    }


def compare_per_scope(rss_report: dict[str, Any], web_report: dict[str, Any]) -> list[dict[str, Any]]:
    rss_by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for page in rss_report.get("page_reports") or []:
        key = (str(page.get("app_id")), str(page.get("country", "")).lower())
        scope = rss_by_scope.setdefault(
            key,
            {
                "rss_page_count": 0,
                "rss_fetch_errors": 0,
                "rss_review_count": 0,
                "rss_empty_pages": 0,
                "rss_terminal_reasons": {},
            },
        )
        scope["rss_page_count"] += 1
        if page.get("status") == "error":
            scope["rss_fetch_errors"] += 1
        if page.get("status") == "ok" and int(page.get("review_count") or 0) == 0:
            scope["rss_empty_pages"] += 1
        scope["rss_review_count"] += int(page.get("review_count") or 0)
        reason = page.get("terminal_reason")
        if reason:
            reasons = scope["rss_terminal_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1

    rows: list[dict[str, Any]] = []
    for row in web_report.get("results") or []:
        key = (str(row.get("app_id")), str(row.get("country", "")).lower())
        rss = rss_by_scope.get(key, {})
        web_pages = row.get("web_catalog_pages") or []
        web_status_counts: dict[str, int] = {}
        retried_pages = 0
        recovered_429_pages = 0
        for page in web_pages:
            status = str(page.get("status_code") or "unknown")
            web_status_counts[status] = web_status_counts.get(status, 0) + 1
            attempts = page.get("attempts") or []
            if len(attempts) > 1:
                retried_pages += 1
                if any(attempt.get("status_code") == 429 for attempt in attempts[:-1]) and page.get("status_code") == 200:
                    recovered_429_pages += 1
        rows.append(
            {
                "app_id": row.get("app_id"),
                "app_name": row.get("app_name"),
                "country": row.get("country"),
                "rss_page_count": rss.get("rss_page_count", 0),
                "rss_fetch_errors": rss.get("rss_fetch_errors", 0),
                "rss_empty_pages": rss.get("rss_empty_pages", 0),
                "rss_review_count": rss.get("rss_review_count", 0),
                "rss_terminal_reasons": rss.get("rss_terminal_reasons", {}),
                "web_page_count": row.get("web_catalog_pages_fetched", 0),
                "web_review_count": row.get("web_catalog_page_reviews_total", 0),
                "web_status_counts": web_status_counts,
                "web_retried_pages": retried_pages,
                "web_recovered_429_pages": recovered_429_pages,
                "web_min_date": min(
                    [page.get("min_date") for page in web_pages if page.get("min_date")],
                    default=None,
                ),
                "web_max_date": max(
                    [page.get("max_date") for page in web_pages if page.get("max_date")],
                    default=None,
                ),
            }
        )
    return rows
