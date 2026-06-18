from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Callable

from app_store_review_pipeline.config import DEFAULT_SORT_BY
from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.provider_apptweak import probe_apptweak_reviews
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


def compare_rss_with_apptweak(
    targets: list[AppTarget],
    *,
    run_id: str,
    raw_root: Path,
    reports_root: Path,
    api_token: str,
    sort_by: str = DEFAULT_SORT_BY,
    rss_max_pages_per_app_country: int = 10,
    rss_max_consecutive_empty_pages: int = 10,
    rss_request_delay_seconds: float = 0.5,
    rss_max_attempts: int = 3,
    rss_retry_delay_seconds: float = 5.0,
    provider_country_fallback: str = "us",
    provider_language: str = "us",
    provider_device: str = "iphone",
    provider_start_date: str | None = None,
    provider_end_date: str | None = None,
    provider_term: str | None = None,
    provider_page_limit: int = 2,
    provider_request_limit: int = 500,
    provider_request_delay_seconds: float = 1.0,
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
    provider_report = probe_apptweak_reviews(
        targets,
        provider_report_path,
        api_token=api_token,
        limit=0,
        country_fallback=provider_country_fallback,
        language=provider_language,
        device=provider_device,
        start_date=provider_start_date,
        end_date=provider_end_date,
        term=provider_term,
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
            "provider": "apptweak",
            "provider_country_fallback": provider_country_fallback,
            "provider_language": provider_language,
            "provider_device": provider_device,
            "provider_start_date": provider_start_date,
            "provider_end_date": provider_end_date,
            "provider_term": provider_term,
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
    summary = {
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
    summary.update(summarize_provider_capacity(rss_reviews, provider_report))
    return summary


def summarize_provider_capacity(rss_reviews: int, provider_report: dict[str, Any]) -> dict[str, Any]:
    settings = provider_report.get("settings") or {}
    rows = provider_report.get("results") or []
    row_count = len(rows)
    page_limit = parse_positive_int(settings.get("page_limit"))
    request_limit = parse_positive_int(settings.get("request_limit"))
    provider_reviews = sum(int(row.get("review_count") or 0) for row in rows)
    reported_totals = [
        int(row.get("total_reviews"))
        for row in rows
        if row.get("total_reviews") is not None and parse_positive_int(row.get("total_reviews")) is not None
    ]
    reported_total_reviews = sum(reported_totals) if reported_totals else None
    rows_with_more_available = sum(
        1
        for row in rows
        if row.get("total_reviews") is not None
        and int(row.get("total_reviews") or 0) > int(row.get("review_count") or 0)
    )
    remaining_reported_reviews = (
        sum(
            max(0, int(row.get("total_reviews") or 0) - int(row.get("review_count") or 0))
            for row in rows
            if row.get("total_reviews") is not None
        )
        if reported_totals
        else None
    )

    empty = {
        "provider_configured_review_ceiling": None,
        "provider_configured_ceiling_usage_ratio": None,
        "provider_configured_ceiling_hit": None,
        "provider_pages_per_row_needed_for_rss_parity": None,
        "provider_additional_pages_per_row_needed_for_rss_parity": None,
        "provider_page_depth_can_reach_rss_parity": None,
        "provider_volume_gap_likely_configuration_limited": None,
        "provider_reported_total_reviews": reported_total_reviews,
        "provider_reported_total_reviews_at_or_above_rss": (
            reported_total_reviews >= rss_reviews if reported_total_reviews is not None else None
        ),
        "provider_rows_with_more_available": rows_with_more_available,
        "provider_reported_reviews_remaining": remaining_reported_reviews,
    }
    if not row_count or not page_limit or not request_limit:
        return empty

    ceiling = row_count * page_limit * request_limit
    pages_for_parity = math.ceil(rss_reviews / (row_count * request_limit)) if rss_reviews > 0 else 0
    ceiling_hit = provider_reviews >= ceiling if ceiling > 0 else False
    can_reach_parity = page_limit >= pages_for_parity
    empty.update(
        {
            "provider_configured_review_ceiling": ceiling,
            "provider_configured_ceiling_usage_ratio": provider_reviews / ceiling if ceiling else None,
            "provider_configured_ceiling_hit": ceiling_hit,
            "provider_pages_per_row_needed_for_rss_parity": pages_for_parity,
            "provider_additional_pages_per_row_needed_for_rss_parity": max(0, pages_for_parity - page_limit),
            "provider_page_depth_can_reach_rss_parity": can_reach_parity,
            "provider_volume_gap_likely_configuration_limited": (
                provider_reviews < rss_reviews
                and (
                    (ceiling_hit and not can_reach_parity)
                    or rows_with_more_available > 0
                    or (
                        reported_total_reviews is not None
                        and reported_total_reviews >= rss_reviews
                        and provider_reviews < reported_total_reviews
                    )
                )
            ),
        }
    )
    return empty


def compare_provider_per_app(rss_report: dict[str, Any], provider_report: dict[str, Any]) -> list[dict[str, Any]]:
    rss_reviews_by_app: dict[str, int] = {}
    rss_pages_by_app: dict[str, int] = {}
    rss_errors_by_app: dict[str, int] = {}
    rss_reviews_by_scope: dict[tuple[str, str], int] = {}
    rss_pages_by_scope: dict[tuple[str, str], int] = {}
    rss_errors_by_scope: dict[tuple[str, str], int] = {}
    for page in rss_report.get("page_reports") or []:
        app_id = str(page.get("app_id") or "")
        country = str(page.get("country") or "").lower()
        scope = (app_id, country)
        rss_pages_by_app[app_id] = rss_pages_by_app.get(app_id, 0) + 1
        rss_reviews_by_app[app_id] = rss_reviews_by_app.get(app_id, 0) + int(page.get("review_count") or 0)
        rss_pages_by_scope[scope] = rss_pages_by_scope.get(scope, 0) + 1
        rss_reviews_by_scope[scope] = rss_reviews_by_scope.get(scope, 0) + int(page.get("review_count") or 0)
        if page.get("status") == "error":
            rss_errors_by_app[app_id] = rss_errors_by_app.get(app_id, 0) + 1
            rss_errors_by_scope[scope] = rss_errors_by_scope.get(scope, 0) + 1

    rows: list[dict[str, Any]] = []
    for row in provider_report.get("results") or []:
        app_id = str(row.get("app_id") or "")
        country = str(row.get("country") or "").lower()
        has_country_scope = bool(country)
        scope = (app_id, country)
        provider_reviews = int(row.get("review_count") or 0)
        provider_total_reviews = row.get("total_reviews")
        rss_reviews = int(
            rss_reviews_by_scope.get(scope, 0) if has_country_scope else rss_reviews_by_app.get(app_id, 0)
        )
        page_count = len(row.get("pages") or [])
        page_limit = parse_positive_int((provider_report.get("settings") or {}).get("page_limit"))
        request_limit = parse_positive_int((provider_report.get("settings") or {}).get("request_limit"))
        provider_ceiling = page_limit * request_limit if page_limit and request_limit else None
        provider_more_available = (
            provider_total_reviews is not None
            and int(provider_total_reviews or 0) > provider_reviews
        )
        rows.append(
            {
                "app_id": app_id,
                "app_name": row.get("app_name"),
                "category": row.get("category"),
                "country": country or None,
                "rss_page_count": (
                    rss_pages_by_scope.get(scope, 0) if has_country_scope else rss_pages_by_app.get(app_id, 0)
                ),
                "rss_fetch_errors": (
                    rss_errors_by_scope.get(scope, 0) if has_country_scope else rss_errors_by_app.get(app_id, 0)
                ),
                "rss_review_count": rss_reviews,
                "provider_page_count": page_count,
                "provider_review_count": provider_reviews,
                "provider_to_rss_review_ratio": provider_reviews / rss_reviews if rss_reviews else None,
                "provider_status_counts": row.get("status_counts") or {},
                "provider_total_reviews": provider_total_reviews,
                "provider_reported_reviews_remaining": (
                    max(0, int(provider_total_reviews or 0) - provider_reviews)
                    if provider_total_reviews is not None
                    else None
                ),
                "provider_more_available": provider_more_available,
                "provider_configured_review_ceiling": provider_ceiling,
                "provider_configured_ceiling_hit": (
                    provider_reviews >= provider_ceiling if provider_ceiling is not None else None
                ),
                "provider_reviews_at_or_above_rss": provider_reviews >= rss_reviews,
                "provider_min_date": row.get("min_date"),
                "provider_max_date": row.get("max_date"),
            }
        )
    return rows


def parse_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed if parsed > 0 else None
