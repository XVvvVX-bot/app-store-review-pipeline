from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import requests

from app_store_review_pipeline.apple_web import (
    WEB_USER_AGENT,
    app_store_reviews_page_url,
    app_store_web_catalog_next_url,
    app_store_web_reviews_url,
    deadline_exceeded,
    final_attempt_stopped_for_time_budget,
    get_with_429_retries,
    parse_web_catalog_review_page,
    parse_web_catalog_review_rows,
    sleep_with_deadline,
)
from app_store_review_pipeline.config import PLATFORM, WEB_CATALOG_SORT_BY, WEB_CATALOG_SOURCE
from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.models import AppReview, AppTarget, ReviewPage, make_page_key
from app_store_review_pipeline.utils import safe_name, utc_timestamp


def fetch_web_catalog_targets(
    targets: list[AppTarget],
    raw_dir: Path,
    run_id: str,
    *,
    sort_by: str = WEB_CATALOG_SORT_BY,
    max_pages_per_app_country: int = 25,
    start_page: int = 1,
    review_limit: int = 20,
    timeout_seconds: float = 20.0,
    request_delay_seconds: float = 5.0,
    web_429_retries: int = 5,
    web_429_retry_seconds: float = 60.0,
    web_429_backoff_multiplier: float = 1.5,
    time_budget_seconds: float = 0.0,
    known_review_ids_by_scope: dict[tuple[str, str, str], set[str]] | None = None,
    target_review_counts_by_scope: dict[tuple[str, str, str], int] | None = None,
    use_overlap_stop: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    start_page = max(1, start_page)
    page_cap = max_pages_per_app_country if max_pages_per_app_country > 0 else None
    deadline_monotonic = monotonic_fn() + time_budget_seconds if time_budget_seconds and time_budget_seconds > 0 else None
    known_review_ids_by_scope = known_review_ids_by_scope or {}
    target_review_counts_by_scope = target_review_counts_by_scope or {}
    page_reports: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    capped_scopes: list[dict[str, str]] = []
    warning_scopes: list[dict[str, str]] = []
    target_reached_scopes: list[dict[str, Any]] = []
    owned_session = session is None
    http = session or requests.Session()

    try:
        for target_index, target in enumerate(targets):
            if target_index and request_delay_seconds:
                sleep_fn(request_delay_seconds)
            for country in target.countries:
                scope = (target.apple_app_id, country.lower(), sort_by)
                known_review_ids = known_review_ids_by_scope.get(scope, set())
                target_review_count = target_review_counts_by_scope.get(scope)
                scope_review_total = 0
                next_href: str | None = None
                page_number = start_page
                while page_cap is None or page_number <= page_cap:
                    if deadline_exceeded(deadline_monotonic, monotonic_fn):
                        warning_scopes.append(
                            {
                                "app_id": target.apple_app_id,
                                "app_name": target.app_name,
                                "country": country.lower(),
                                "sort_by": sort_by,
                                "reason": "time_budget_exceeded",
                            }
                        )
                        break
                    if page_number > start_page and request_delay_seconds:
                        if not sleep_with_deadline(
                            request_delay_seconds,
                            deadline_monotonic,
                            sleep_fn=sleep_fn,
                            monotonic_fn=monotonic_fn,
                        ):
                            warning_scopes.append(
                                {
                                    "app_id": target.apple_app_id,
                                    "app_name": target.app_name,
                                    "country": country.lower(),
                                    "sort_by": sort_by,
                                    "reason": "time_budget_exceeded",
                                }
                            )
                            break
                    page_report, reviews, next_href = fetch_web_catalog_page(
                        target,
                        raw_dir,
                        run_id,
                        country=country,
                        sort_by=sort_by,
                        page_number=page_number,
                        start_page=start_page,
                        next_href=next_href,
                        review_limit=review_limit,
                        session=http,
                        timeout_seconds=timeout_seconds,
                        web_429_retries=web_429_retries,
                        web_429_retry_seconds=web_429_retry_seconds,
                        web_429_backoff_multiplier=web_429_backoff_multiplier,
                        deadline_monotonic=deadline_monotonic,
                        monotonic_fn=monotonic_fn,
                        sleep_fn=sleep_fn,
                    )
                    overlap_count = sum(1 for review in reviews if review.review_id in known_review_ids)
                    scope_review_total += len(reviews)
                    terminal_reason = web_terminal_reason_for_page(
                        page_report,
                        page_number=page_number,
                        max_pages_per_app_country=page_cap,
                        overlap_count=overlap_count,
                        known_review_count=len(known_review_ids),
                        page_review_total=scope_review_total,
                        target_review_count=target_review_count,
                        next_href=next_href,
                        use_overlap_stop=use_overlap_stop,
                    )
                    page_report = replace(
                        page_report,
                        terminal_reason=terminal_reason,
                        overlap_review_count=overlap_count,
                    )
                    page_reports.append(asdict(page_report))
                    review_rows.extend(asdict(review) for review in reviews)

                    if terminal_reason:
                        if terminal_reason == "target_review_count_reached":
                            target_reached_scopes.append(
                                {
                                    "app_id": target.apple_app_id,
                                    "app_name": target.app_name,
                                    "country": country.lower(),
                                    "sort_by": sort_by,
                                    "target_review_count": target_review_count,
                                    "fetched_review_count": scope_review_total,
                                }
                            )
                        if terminal_reason in {"page_cap", "fetch_error"}:
                            warning_scopes.append(
                                {
                                    "app_id": target.apple_app_id,
                                    "app_name": target.app_name,
                                    "country": country.lower(),
                                    "sort_by": sort_by,
                                    "reason": terminal_reason,
                                }
                            )
                        if terminal_reason == "page_cap":
                            capped_scopes.append(
                                {
                                    "app_id": target.apple_app_id,
                                    "app_name": target.app_name,
                                    "country": country.lower(),
                                    "sort_by": sort_by,
                                }
                            )
                        break
                    page_number += 1
    finally:
        if owned_session:
            http.close()

    return {
        "run_id": run_id,
        "source": WEB_CATALOG_SOURCE,
        "platform": PLATFORM,
        "sort_by": sort_by,
        "start_page": start_page,
        "max_pages_per_app_country": max_pages_per_app_country,
        "page_cap_enabled": page_cap is not None,
        "time_budget_seconds": time_budget_seconds,
        "page_reports": page_reports,
        "reviews": review_rows,
        "fetched_pages": sum(1 for page in page_reports if page.get("status") == "ok"),
        "fetch_errors": sum(1 for page in page_reports if page.get("status") == "error"),
        "empty_pages": sum(1 for page in page_reports if page.get("status") == "ok" and page.get("review_count") == 0),
        "review_count": len(review_rows),
        "unique_review_count": len({row.get("review_key") for row in review_rows if row.get("review_key")}),
        "capped_scopes": capped_scopes,
        "warning_scopes": warning_scopes,
        "target_review_counts_enabled": bool(target_review_counts_by_scope),
        "target_review_count_scopes": len(target_review_counts_by_scope),
        "target_reached_scopes": target_reached_scopes,
    }


def fetch_web_catalog_page(
    target: AppTarget,
    raw_dir: Path,
    run_id: str,
    *,
    country: str,
    sort_by: str,
    page_number: int,
    start_page: int,
    next_href: str | None,
    review_limit: int,
    session: requests.Session,
    timeout_seconds: float,
    web_429_retries: int,
    web_429_retry_seconds: float,
    web_429_backoff_multiplier: float,
    deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[ReviewPage, list[AppReview], str | None]:
    country = country.lower()
    page_key = make_page_key(run_id, target.apple_app_id, country, sort_by, page_number)
    if page_number == start_page:
        request_url = app_store_web_reviews_url(
            target.apple_app_id,
            country,
            offset=(page_number - 1) * review_limit,
            sort=sort_by,
            limit=review_limit,
        )
    else:
        request_url = app_store_web_catalog_next_url(str(next_href), sort=sort_by, limit=review_limit)
    raw_json_path = raw_dir / f"{safe_name(target.apple_app_id)}_{country}_{safe_name(sort_by)}_{page_number:03d}.json"
    fetched_at = utc_timestamp()
    headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": app_store_reviews_page_url(target, country),
        "Origin": "https://apps.apple.com",
    }

    try:
        response, attempts = get_with_429_retries(
            session,
            request_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            web_429_retries=web_429_retries,
            web_429_retry_seconds=web_429_retry_seconds,
            web_429_backoff_multiplier=web_429_backoff_multiplier,
            deadline_monotonic=deadline_monotonic,
            monotonic_fn=monotonic_fn,
            sleep_fn=sleep_fn,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("web catalog response JSON was not an object")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        return web_error_page(
            target,
            page_key,
            run_id,
            country=country,
            sort_by=sort_by,
            page_number=page_number,
            request_url=request_url,
            fetched_at=fetched_at,
            error_message=str(exc),
        ), [], None

    write_json(raw_json_path, payload)
    summary = parse_web_catalog_review_page(payload)
    reviews = parse_web_catalog_review_rows(
        payload,
        target,
        country=country,
        page_number=page_number,
        page_key=page_key,
        collected_at=fetched_at,
    )
    review_ids = [review.review_id for review in reviews if review.review_id]
    updated_epochs = [review.updated_epoch_seconds for review in reviews if review.updated_epoch_seconds is not None]
    status_ok = 200 <= response.status_code < 300
    page = ReviewPage(
        page_key=page_key,
        run_id=run_id,
        platform=PLATFORM,
        source=WEB_CATALOG_SOURCE,
        app_id=target.apple_app_id,
        app_name=target.app_name,
        country=country,
        sort_by=sort_by,
        page_number=page_number,
        request_url=request_url,
        status="ok" if status_ok else "error",
        status_code=response.status_code,
        fetched_at=fetched_at,
        raw_json_path=str(raw_json_path),
        response_bytes=len(response.content or b""),
        review_count=len(reviews),
        unique_review_count=len(set(review_ids)),
        duplicate_count=len(review_ids) - len(set(review_ids)),
        missing_text_count=sum(1 for review in reviews if not review.content),
        missing_rating_count=sum(1 for review in reviews if review.rating is None),
        missing_updated_count=sum(1 for review in reviews if review.updated_epoch_seconds is None),
        max_updated_epoch_seconds=max(updated_epochs) if updated_epochs else None,
        min_updated_epoch_seconds=min(updated_epochs) if updated_epochs else None,
        has_next_link=bool(summary["next_href"]),
        attempt_count=len(attempts),
        error_message=None if status_ok else f"HTTP {response.status_code}",
        terminal_reason=None,
        overlap_review_count=0,
    )
    if final_attempt_stopped_for_time_budget([{"attempts": attempts}]):
        page = replace(page, terminal_reason="time_budget_exceeded")
    return page, reviews, summary["next_href"]


def web_terminal_reason_for_page(
    page_report: ReviewPage,
    *,
    page_number: int,
    max_pages_per_app_country: int | None,
    overlap_count: int,
    known_review_count: int,
    page_review_total: int,
    target_review_count: int | None,
    next_href: str | None,
    use_overlap_stop: bool,
) -> str | None:
    if page_report.terminal_reason:
        return page_report.terminal_reason
    if page_report.status != "ok":
        return "fetch_error"
    if target_review_count is not None and target_review_count <= 0:
        return "target_review_count_zero"
    if (
        target_review_count is not None
        and target_review_count > 0
        and page_review_total >= target_review_count
    ):
        return "target_review_count_reached"
    if use_overlap_stop and known_review_count > 0 and overlap_count > 0:
        if target_review_count is None or known_review_count >= target_review_count:
            return "caught_up_to_existing_reviews"
    if max_pages_per_app_country is not None and page_number >= max_pages_per_app_country:
        return "page_cap"
    if not next_href:
        return "no_next_href"
    if page_report.review_count == 0:
        return "empty_page"
    return None


def web_error_page(
    target: AppTarget,
    page_key: str,
    run_id: str,
    *,
    country: str,
    sort_by: str,
    page_number: int,
    request_url: str,
    fetched_at: str,
    error_message: str,
) -> ReviewPage:
    return ReviewPage(
        page_key=page_key,
        run_id=run_id,
        platform=PLATFORM,
        source=WEB_CATALOG_SOURCE,
        app_id=target.apple_app_id,
        app_name=target.app_name,
        country=country,
        sort_by=sort_by,
        page_number=page_number,
        request_url=request_url,
        status="error",
        status_code=None,
        fetched_at=fetched_at,
        raw_json_path=None,
        response_bytes=0,
        review_count=0,
        unique_review_count=0,
        duplicate_count=0,
        missing_text_count=0,
        missing_rating_count=0,
        missing_updated_count=0,
        max_updated_epoch_seconds=None,
        min_updated_epoch_seconds=None,
        has_next_link=False,
        attempt_count=1,
        error_message=error_message,
        terminal_reason=None,
        overlap_review_count=0,
    )
