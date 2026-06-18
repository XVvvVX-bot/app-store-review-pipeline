from __future__ import annotations

import time
import math
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
    target_offset: int = 0,
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
    web_429_backoff_multiplier: float = 1.0,
    web_include_html: bool = True,
    web_stop_at_rss_parity: bool = False,
    web_time_budget_seconds: float | None = None,
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
    rss_counts_by_scope = rss_review_counts_by_scope(rss_report)
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
        web_429_backoff_multiplier=web_429_backoff_multiplier,
        include_html=web_include_html,
        target_review_counts_by_scope=rss_counts_by_scope if web_stop_at_rss_parity else None,
        time_budget_seconds=web_time_budget_seconds,
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
            "target_offset": target_offset,
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
            "web_429_backoff_multiplier": web_429_backoff_multiplier,
            "web_include_html": web_include_html,
            "web_stop_at_rss_parity": web_stop_at_rss_parity,
            "web_time_budget_seconds": web_time_budget_seconds,
            "timeout_seconds": timeout_seconds,
        },
        "rss": summarize_rss_report(rss_report),
        "web_catalog": summarize_web_report(web_report),
        "comparison": summarize_comparison(
            rss_report,
            web_report,
            scope_count=sum(len(target.countries) for target in targets),
            web_max_pages=web_max_pages,
            web_review_limit=web_review_limit,
        ),
        "per_scope": compare_per_scope(rss_report, web_report),
        "paths": {
            "rss_raw_dir": str(raw_dir / "rss"),
            "web_report_path": str(web_report_path),
            "comparison_report_path": str(report_dir / "source_comparison_report.json"),
            "markdown_report_path": str(report_dir / "source_comparison_report.md"),
        },
    }
    comparison["source_decision"] = build_web_source_decision(comparison)
    write_json(report_dir / "source_comparison_report.json", comparison)
    (report_dir / "source_comparison_report.md").write_text(
        render_source_markdown_report(comparison),
        encoding="utf-8",
    )
    return comparison


def rss_review_counts_by_scope(report: dict[str, Any]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for page in report.get("page_reports") or []:
        key = (str(page.get("app_id")), str(page.get("country", "")).lower())
        counts[key] = counts.get(key, 0) + int(page.get("review_count") or 0)
    return counts


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


def summarize_comparison(
    rss_report: dict[str, Any],
    web_report: dict[str, Any],
    *,
    scope_count: int | None = None,
    web_max_pages: int | None = None,
    web_review_limit: int | None = None,
) -> dict[str, Any]:
    rss_summary = summarize_rss_report(rss_report)
    web_summary = summarize_web_report(web_report)
    rss_reviews = int(rss_summary["unique_reviews_seen"] or 0)
    web_reviews = int(web_summary.get("web_catalog_page_reviews_total") or 0)
    web_to_rss_ratio = web_reviews / rss_reviews if rss_reviews else None
    web_same_order_as_rss = (
        rss_reviews > 0
        and web_reviews > 0
        and web_to_rss_ratio is not None
        and web_to_rss_ratio >= 0.1
    )
    web_page_status_counts = web_summary.get("web_catalog_page_status_counts") or {}
    web_unrecovered_429_pages = int(web_page_status_counts.get("429") or 0)
    web_non_200_pages = sum(
        int(count)
        for status, count in web_page_status_counts.items()
        if str(status) != "200"
    )
    web_recovered_429_pages = int(web_summary.get("recovered_429_page_count", 0) or 0)
    web_429_attempted_recovery_pages = web_recovered_429_pages + web_unrecovered_429_pages
    web_time_budget_exceeded = bool(web_summary.get("time_budget_exceeded"))
    planned_scope_count = web_summary.get("planned_scope_count")
    completed_scope_count = web_summary.get("completed_scope_count")
    web_all_scopes_completed = (
        True
        if planned_scope_count in (None, 0)
        else int(completed_scope_count or 0) >= int(planned_scope_count or 0)
    )
    summary = {
        "web_reviews_minus_rss_reviews": web_reviews - rss_reviews,
        "rss_unique_reviews_seen": rss_reviews,
        "web_catalog_page_reviews_total": web_reviews,
        "web_to_rss_review_ratio": web_to_rss_ratio,
        "web_reviews_same_order_as_rss": web_same_order_as_rss,
        "web_reviews_at_or_above_rss": web_reviews >= rss_reviews,
        "rss_fetch_error_count": rss_summary["fetch_errors"],
        "web_non_200_page_count_after_retry": web_non_200_pages,
        "web_unrecovered_429_page_count": web_unrecovered_429_pages,
        "web_all_pages_ok_after_retry": web_non_200_pages == 0,
        "web_recovered_429_page_count": web_recovered_429_pages,
        "web_429_recovery_rate_after_retry": (
            web_recovered_429_pages / web_429_attempted_recovery_pages
            if web_429_attempted_recovery_pages
            else None
        ),
        "web_time_budget_exceeded": web_time_budget_exceeded,
        "web_planned_scope_count": planned_scope_count,
        "web_completed_scope_count": completed_scope_count,
        "web_skipped_scope_count": web_summary.get("skipped_scope_count"),
        "web_all_scopes_completed": web_all_scopes_completed,
        "web_retried_page_count": web_summary.get("retried_page_count", 0),
        "candidate_passes_single_run_gate": (
            rss_reviews > 0
            and web_reviews > 0
            and web_reviews >= rss_reviews
            and web_non_200_pages == 0
            and rss_summary["fetch_errors"] == 0
            and not web_time_budget_exceeded
            and web_all_scopes_completed
        ),
        "candidate_passes_same_order_stability_gate": (
            rss_reviews > 0
            and web_same_order_as_rss
            and web_non_200_pages == 0
            and rss_summary["fetch_errors"] == 0
            and not web_time_budget_exceeded
            and web_all_scopes_completed
        ),
    }
    summary.update(
        summarize_web_capacity(
            rss_reviews,
            web_reviews,
            scope_count=scope_count,
            web_max_pages=web_max_pages,
            web_review_limit=web_review_limit,
        )
    )
    return summary


def summarize_web_capacity(
    rss_reviews: int,
    web_reviews: int,
    *,
    scope_count: int | None,
    web_max_pages: int | None,
    web_review_limit: int | None,
) -> dict[str, Any]:
    empty = {
        "web_configured_review_ceiling": None,
        "web_configured_ceiling_usage_ratio": None,
        "web_configured_ceiling_hit": None,
        "web_pages_per_scope_needed_for_rss_parity": None,
        "web_additional_pages_per_scope_needed_for_rss_parity": None,
        "web_page_depth_can_reach_rss_parity": None,
        "web_volume_gap_likely_configuration_limited": None,
    }
    if not scope_count or not web_max_pages or not web_review_limit:
        return empty

    ceiling = scope_count * web_max_pages * web_review_limit
    pages_for_parity = math.ceil(rss_reviews / (scope_count * web_review_limit)) if rss_reviews > 0 else 0
    ceiling_hit = web_reviews >= ceiling if ceiling > 0 else False
    can_reach_parity = web_max_pages >= pages_for_parity
    empty.update(
        {
            "web_configured_review_ceiling": ceiling,
            "web_configured_ceiling_usage_ratio": web_reviews / ceiling if ceiling else None,
            "web_configured_ceiling_hit": ceiling_hit,
            "web_pages_per_scope_needed_for_rss_parity": pages_for_parity,
            "web_additional_pages_per_scope_needed_for_rss_parity": max(0, pages_for_parity - web_max_pages),
            "web_page_depth_can_reach_rss_parity": can_reach_parity,
            "web_volume_gap_likely_configuration_limited": (
                web_reviews < rss_reviews and ceiling_hit and not can_reach_parity
            ),
        }
    )
    return empty


def build_web_source_decision(report_or_metrics: dict[str, Any]) -> dict[str, Any]:
    metrics = report_or_metrics.get("comparison") if "comparison" in report_or_metrics else report_or_metrics
    metrics = metrics or {}
    rss = report_or_metrics.get("rss") if "comparison" in report_or_metrics else {}
    rss_errors = int(metrics.get("rss_fetch_error_count") or 0)
    rss_reviews = int(metrics.get("rss_unique_reviews_seen") or (rss or {}).get("unique_reviews_seen") or 0)
    web_non_200 = int(metrics.get("web_non_200_page_count_after_retry") or 0)
    unrecovered_429 = int(metrics.get("web_unrecovered_429_page_count") or 0)
    web_ratio = metrics.get("web_to_rss_review_ratio")
    selected_source = "apple_web_catalog_reviews"

    if rss_errors:
        return {
            "status": "rss_baseline_unreliable",
            "selected_source": None,
            "recommended_next_action": (
                "Rerun the comparison after RSS fetch errors clear; the baseline window is not reliable enough "
                "to judge a replacement source."
            ),
            "blocking_metric": "rss_fetch_error_count",
            "blocking_value": rss_errors,
        }
    if rss_reviews <= 0:
        return {
            "status": "rss_baseline_empty",
            "selected_source": None,
            "recommended_next_action": (
                "Rerun with a target window where RSS returns nonzero reviews; a zero-review RSS baseline cannot "
                "prove that web catalog has matched or exceeded RSS."
            ),
            "blocking_metric": "rss_unique_reviews_seen",
            "blocking_value": rss_reviews,
        }
    if metrics.get("web_time_budget_exceeded") is True or metrics.get("web_all_scopes_completed") is False:
        return {
            "status": "web_catalog_time_budget_exceeded",
            "selected_source": None,
            "recommended_next_action": (
                "Do not promote this web catalog profile yet; the comparison did not complete the intended "
                "target window within its runtime budget. Reduce target count, reduce retry delay, or keep this "
                "profile as a manual depth test."
            ),
            "blocking_metric": "web_time_budget_exceeded",
            "blocking_value": bool(metrics.get("web_time_budget_exceeded")),
            "web_completed_scope_count": metrics.get("web_completed_scope_count"),
            "web_planned_scope_count": metrics.get("web_planned_scope_count"),
        }
    if metrics.get("candidate_passes_single_run_gate") is True:
        return {
            "status": "web_catalog_replacement_candidate",
            "selected_source": selected_source,
            "recommended_next_action": (
                "Repeat this web catalog profile across several scheduled canary windows before promoting it "
                "into a separate ingestion mode."
            ),
            "web_to_rss_review_ratio": web_ratio,
        }
    if web_non_200:
        return {
            "status": "web_catalog_unstable_after_retry",
            "selected_source": None,
            "recommended_next_action": (
                "Do not promote web catalog as the primary source; final non-200 pages after retry show that "
                "deep pagination is not stable enough for routine ingestion."
            ),
            "blocking_metric": "web_non_200_page_count_after_retry",
            "blocking_value": web_non_200,
            "web_unrecovered_429_page_count": unrecovered_429,
        }
    if metrics.get("web_volume_gap_likely_configuration_limited") is True:
        return {
            "status": "needs_deeper_web_catalog_run",
            "selected_source": selected_source,
            "recommended_next_action": (
                "Rerun manually with higher web_max_pages; this run's web volume gap is likely caused by the "
                "configured page cap rather than proven source insufficiency."
            ),
            "web_to_rss_review_ratio": web_ratio,
            "web_additional_pages_per_scope_needed_for_rss_parity": metrics.get(
                "web_additional_pages_per_scope_needed_for_rss_parity"
            ),
        }
    if metrics.get("candidate_passes_same_order_stability_gate") is True:
        return {
            "status": "same_order_but_not_replacement",
            "selected_source": selected_source,
            "recommended_next_action": (
                "Keep web catalog as a supplemental diagnostic source, but do not replace RSS without stronger "
                "volume parity across repeated canary runs."
            ),
            "web_to_rss_review_ratio": web_ratio,
        }
    return {
        "status": "no_public_web_replacement_candidate",
        "selected_source": None,
        "recommended_next_action": (
            "Do not replace RSS from this run; continue with RSS plus licensed-provider evaluation."
        ),
        "web_to_rss_review_ratio": web_ratio,
    }


def render_source_markdown_report(report: dict[str, Any]) -> str:
    decision = report.get("source_decision") or build_web_source_decision(report)
    metrics = report.get("comparison") or {}
    rss = report.get("rss") or {}
    web = report.get("web_catalog") or {}
    settings = report.get("settings") or {}
    lines = [
        "# App Store Web Catalog Source Report",
        "",
        f"Decision: **{decision.get('status', 'unknown')}**",
        "",
        f"Selected source: `{decision.get('selected_source') or 'none'}`",
        "",
        f"Recommended next action: {decision.get('recommended_next_action') or 'Review the comparison metrics.'}",
        "",
        "## Volume",
        "",
        f"- RSS unique reviews: `{rss.get('unique_reviews_seen', 0)}`",
        f"- Web catalog page reviews: `{web.get('web_catalog_page_reviews_total', 0)}`",
        f"- Web/RSS ratio: `{format_ratio(metrics.get('web_to_rss_review_ratio'))}`",
        f"- Web minus RSS reviews: `{metrics.get('web_reviews_minus_rss_reviews', 0)}`",
        "",
        "## Gates",
        "",
        "| Gate | Value |",
        "| --- | --- |",
        f"| Replacement gate | {bool_label(metrics.get('candidate_passes_single_run_gate'))} |",
        f"| Same-order stability gate | {bool_label(metrics.get('candidate_passes_same_order_stability_gate'))} |",
        f"| Web all pages OK after retry | {bool_label(metrics.get('web_all_pages_ok_after_retry'))} |",
        f"| RSS fetch error count | {metrics.get('rss_fetch_error_count', 0)} |",
        f"| Web non-200 pages after retry | {metrics.get('web_non_200_page_count_after_retry', 0)} |",
        f"| Web unrecovered 429 pages | {metrics.get('web_unrecovered_429_page_count', 0)} |",
        f"| Web time budget exceeded | {bool_label(metrics.get('web_time_budget_exceeded'))} |",
        f"| Web all scopes completed | {bool_label(metrics.get('web_all_scopes_completed'))} |",
        "",
        "## Capacity",
        "",
        f"- Web configured review ceiling: `{metrics.get('web_configured_review_ceiling')}`",
        f"- Web configured ceiling hit: `{bool_label(metrics.get('web_configured_ceiling_hit'))}`",
        f"- Pages per scope needed for RSS parity: `{metrics.get('web_pages_per_scope_needed_for_rss_parity')}`",
        f"- Additional pages per scope needed for RSS parity: `{metrics.get('web_additional_pages_per_scope_needed_for_rss_parity')}`",
        f"- Web page depth can reach RSS parity: `{bool_label(metrics.get('web_page_depth_can_reach_rss_parity'))}`",
        f"- Web volume gap likely configuration-limited: `{bool_label(metrics.get('web_volume_gap_likely_configuration_limited'))}`",
        f"- Web targeted scopes: `{web.get('web_catalog_targeted_scopes', 0)}`",
        f"- Web target reached scopes: `{web.get('web_catalog_target_reached_scopes', 0)}`",
        f"- Web planned scopes: `{metrics.get('web_planned_scope_count')}`",
        f"- Web completed scopes: `{metrics.get('web_completed_scope_count')}`",
        f"- Web skipped scopes: `{metrics.get('web_skipped_scope_count')}`",
        f"- Web stop reasons: `{web.get('web_catalog_stop_reasons', {})}`",
        "",
        "## Settings",
        "",
        f"- Web max pages per app-country: `{settings.get('web_max_pages')}`",
        f"- Web review limit per page: `{settings.get('web_review_limit')}`",
        f"- Web request delay seconds: `{settings.get('web_request_delay_seconds')}`",
        f"- Web 429 retries: `{settings.get('web_429_retries')}`",
        f"- Web 429 retry seconds: `{settings.get('web_429_retry_seconds')}`",
        f"- Web HTML probe included: `{bool_label(settings.get('web_include_html'))}`",
        f"- Web stop at RSS parity: `{bool_label(settings.get('web_stop_at_rss_parity'))}`",
        f"- Web time budget seconds: `{settings.get('web_time_budget_seconds')}`",
        "",
        "## Decision Status Meaning",
        "",
        "- `web_catalog_replacement_candidate`: web catalog matched or exceeded RSS volume with clean final pages.",
        "- `needs_deeper_web_catalog_run`: the configured web page cap was too shallow to judge replacement.",
        "- `same_order_but_not_replacement`: web catalog is stable enough to monitor but did not match RSS volume.",
        "- `web_catalog_unstable_after_retry`: final non-200 pages remain after retry, usually from deep-pagination throttling.",
        "- `web_catalog_time_budget_exceeded`: the intended target window did not finish within the canary time budget.",
        "- `rss_baseline_unreliable`: RSS fetch errors make this comparison inconclusive.",
        "- `rss_baseline_empty`: RSS returned zero reviews, so web/RSS replacement cannot be judged.",
        "- `no_public_web_replacement_candidate`: this run does not support replacing RSS.",
        "",
    ]
    return "\n".join(lines)


def bool_label(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def format_ratio(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


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
