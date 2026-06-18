from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from app_store_review_pipeline.config import DEFAULT_SORT_BY
from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.provider_42matters import probe_42matters_reviews
from app_store_review_pipeline.source_compare import summarize_rss_report
from app_store_review_pipeline.utils import utc_timestamp


def compare_rss_with_42matters(
    targets: list[AppTarget],
    *,
    run_id: str,
    raw_root: Path,
    reports_root: Path,
    access_token: str,
    sort_by: str = DEFAULT_SORT_BY,
    rss_max_pages_per_app_country: int = 10,
    rss_max_consecutive_empty_pages: int = 10,
    rss_request_delay_seconds: float = 0.5,
    rss_max_attempts: int = 3,
    rss_retry_delay_seconds: float = 5.0,
    provider_days: int | None = 30,
    provider_start_date: str | None = None,
    provider_end_date: str | None = None,
    provider_lang: str | None = None,
    provider_rating: int | None = None,
    provider_page_limit: int = 5,
    provider_request_limit: int = 100,
    provider_request_delay_seconds: float = 0.4,
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

    provider_report_path = report_dir / "provider_probe_report.json"
    provider_report = probe_42matters_reviews(
        targets,
        provider_report_path,
        access_token=access_token,
        limit=0,
        days=provider_days,
        start_date=provider_start_date,
        end_date=provider_end_date,
        lang=provider_lang,
        rating=provider_rating,
        page_limit=provider_page_limit,
        request_limit=provider_request_limit,
        timeout_seconds=timeout_seconds,
        request_delay_seconds=provider_request_delay_seconds,
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
            "provider": "42matters",
            "provider_days": provider_days,
            "provider_start_date": provider_start_date,
            "provider_end_date": provider_end_date,
            "provider_lang": provider_lang,
            "provider_rating": provider_rating,
            "provider_page_limit": provider_page_limit,
            "provider_request_limit": provider_request_limit,
            "provider_request_delay_seconds": provider_request_delay_seconds,
            "timeout_seconds": timeout_seconds,
        },
        "rss": summarize_rss_report(rss_report),
        "provider": provider_report["summary"],
        "comparison": summarize_provider_comparison(rss_report, provider_report),
        "per_app": compare_provider_per_app(rss_report, provider_report),
        "paths": {
            "rss_raw_dir": str(raw_dir / "rss"),
            "provider_report_path": str(provider_report_path),
            "comparison_report_path": str(report_dir / "provider_comparison_report.json"),
        },
    }
    write_json(report_dir / "provider_comparison_report.json", comparison)
    return comparison


def summarize_provider_comparison(rss_report: dict[str, Any], provider_report: dict[str, Any]) -> dict[str, Any]:
    rss_summary = summarize_rss_report(rss_report)
    provider_summary = provider_report.get("summary") or {}
    rss_reviews = int(rss_summary.get("unique_reviews_seen") or 0)
    provider_reviews = int(provider_summary.get("reviews_seen") or 0)
    provider_to_rss_ratio = provider_reviews / rss_reviews if rss_reviews else None
    provider_same_order_as_rss = provider_reviews > 0 and (
        rss_reviews == 0 or (provider_to_rss_ratio is not None and provider_to_rss_ratio >= 0.1)
    )
    status_counts = provider_summary.get("status_counts") or {}
    provider_non_200_pages = sum(
        int(count)
        for status, count in status_counts.items()
        if str(status) != "200"
    )
    return {
        "provider_reviews_minus_rss_reviews": provider_reviews - rss_reviews,
        "provider_to_rss_review_ratio": provider_to_rss_ratio,
        "provider_reviews_same_order_as_rss": provider_same_order_as_rss,
        "provider_reviews_at_or_above_rss": provider_reviews >= rss_reviews,
        "rss_fetch_error_count": int(rss_summary.get("fetch_errors") or 0),
        "provider_non_200_page_count": provider_non_200_pages,
        "provider_all_pages_ok": provider_non_200_pages == 0,
        "provider_page_success_rate": provider_summary.get("page_success_rate"),
        "candidate_passes_same_order_stability_gate": (
            provider_same_order_as_rss
            and provider_non_200_pages == 0
            and int(rss_summary.get("fetch_errors") or 0) == 0
        ),
        "candidate_passes_replacement_gate": (
            provider_reviews > 0
            and provider_reviews >= rss_reviews
            and provider_non_200_pages == 0
            and int(rss_summary.get("fetch_errors") or 0) == 0
        ),
    }


def compare_provider_per_app(rss_report: dict[str, Any], provider_report: dict[str, Any]) -> list[dict[str, Any]]:
    rss_reviews_by_app: dict[str, int] = {}
    rss_pages_by_app: dict[str, int] = {}
    rss_errors_by_app: dict[str, int] = {}
    for page in rss_report.get("page_reports") or []:
        app_id = str(page.get("app_id") or "")
        rss_pages_by_app[app_id] = rss_pages_by_app.get(app_id, 0) + 1
        rss_reviews_by_app[app_id] = rss_reviews_by_app.get(app_id, 0) + int(page.get("review_count") or 0)
        if page.get("status") == "error":
            rss_errors_by_app[app_id] = rss_errors_by_app.get(app_id, 0) + 1

    rows: list[dict[str, Any]] = []
    for row in provider_report.get("results") or []:
        app_id = str(row.get("app_id") or "")
        provider_reviews = int(row.get("review_count") or 0)
        rss_reviews = int(rss_reviews_by_app.get(app_id, 0))
        rows.append(
            {
                "app_id": app_id,
                "app_name": row.get("app_name"),
                "category": row.get("category"),
                "rss_page_count": rss_pages_by_app.get(app_id, 0),
                "rss_fetch_errors": rss_errors_by_app.get(app_id, 0),
                "rss_review_count": rss_reviews,
                "provider_page_count": len(row.get("pages") or []),
                "provider_review_count": provider_reviews,
                "provider_to_rss_review_ratio": provider_reviews / rss_reviews if rss_reviews else None,
                "provider_status_counts": row.get("status_counts") or {},
                "provider_total_reviews": row.get("total_reviews"),
                "provider_min_date": row.get("min_date"),
                "provider_max_date": row.get("max_date"),
            }
        )
    return rows
