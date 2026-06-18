from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

import requests

from app_store_review_pipeline.apple_rss import (
    apple_rss_url,
    parse_apple_review,
    payload_entries,
    payload_has_next_link,
)
from app_store_review_pipeline.config import DEFAULT_SORT_BY, PLATFORM, SOURCE
from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.models import AppReview, AppTarget, ReviewPage, make_page_key
from app_store_review_pipeline.utils import safe_name, utc_timestamp


USER_AGENT = "ScienciaAI-AppStoreReviewPipeline/0.1"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def fetch_targets(
    targets: list[AppTarget],
    raw_dir: Path,
    run_id: str,
    *,
    sort_by: str = DEFAULT_SORT_BY,
    max_pages_per_app_country: int = 10,
    timeout_seconds: float = 20.0,
    request_delay_seconds: float = 1.0,
    max_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
    known_review_ids_by_scope: dict[tuple[str, str, str], set[str]] | None = None,
    use_overlap_stop: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    known_review_ids_by_scope = known_review_ids_by_scope or {}
    page_reports: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    capped_scopes: list[dict[str, str]] = []
    warning_scopes: list[dict[str, str]] = []
    owned_session = session is None
    http = session or requests.Session()

    try:
        for target_index, target in enumerate(targets):
            if target_index and request_delay_seconds:
                sleep_fn(request_delay_seconds)
            for country in target.countries:
                scope = (target.apple_app_id, country.lower(), sort_by)
                known_review_ids = known_review_ids_by_scope.get(scope, set())
                pages_for_scope = 0
                for page_number in range(1, max_pages_per_app_country + 1):
                    if pages_for_scope and request_delay_seconds:
                        sleep_fn(request_delay_seconds)
                    page_report, reviews = fetch_apple_rss_page(
                        target,
                        raw_dir,
                        run_id,
                        country=country,
                        sort_by=sort_by,
                        page_number=page_number,
                        session=http,
                        timeout_seconds=timeout_seconds,
                        max_attempts=max_attempts,
                        retry_delay_seconds=retry_delay_seconds,
                        sleep_fn=sleep_fn,
                    )
                    overlap_count = sum(1 for review in reviews if review.review_id in known_review_ids)
                    terminal_reason = terminal_reason_for_page(
                        page_report,
                        page_number=page_number,
                        max_pages_per_app_country=max_pages_per_app_country,
                        overlap_count=overlap_count,
                        known_review_count=len(known_review_ids),
                        use_overlap_stop=use_overlap_stop,
                    )
                    page_report = replace(
                        page_report,
                        terminal_reason=terminal_reason,
                        overlap_review_count=overlap_count,
                    )
                    page_reports.append(asdict(page_report))
                    review_rows.extend(asdict(review) for review in reviews)
                    pages_for_scope += 1

                    if terminal_reason:
                        warning_reasons = {"page_cap", "empty_page_before_overlap"}
                        if terminal_reason in warning_reasons:
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
    finally:
        if owned_session:
            http.close()

    return {
        "run_id": run_id,
        "source": SOURCE,
        "platform": PLATFORM,
        "sort_by": sort_by,
        "page_reports": page_reports,
        "reviews": review_rows,
        "fetched_pages": sum(1 for page in page_reports if page.get("status") == "ok"),
        "fetch_errors": sum(1 for page in page_reports if page.get("status") == "error"),
        "empty_pages": sum(1 for page in page_reports if page.get("status") == "ok" and page.get("review_count") == 0),
        "review_count": len(review_rows),
        "unique_review_count": len({row.get("review_key") for row in review_rows if row.get("review_key")}),
        "capped_scopes": capped_scopes,
        "warning_scopes": warning_scopes,
    }


def terminal_reason_for_page(
    page_report: ReviewPage,
    *,
    page_number: int,
    max_pages_per_app_country: int,
    overlap_count: int,
    known_review_count: int,
    use_overlap_stop: bool,
) -> str | None:
    if page_report.status != "ok":
        return "fetch_error"
    if page_report.review_count == 0:
        if use_overlap_stop and known_review_count > 0 and overlap_count == 0:
            return "empty_page_before_overlap"
        return "empty_page"
    if use_overlap_stop and overlap_count > 0:
        return "caught_up_to_existing_reviews"
    if page_number >= max_pages_per_app_country:
        return "page_cap"
    return None


def fetch_apple_rss_page(
    target: AppTarget,
    raw_dir: Path,
    run_id: str,
    *,
    country: str,
    sort_by: str,
    page_number: int,
    session: requests.Session,
    timeout_seconds: float,
    max_attempts: int,
    retry_delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> tuple[ReviewPage, list[AppReview]]:
    country = country.lower()
    page_key = make_page_key(run_id, target.apple_app_id, country, sort_by, page_number)
    request_url = apple_rss_url(target.apple_app_id, country=country, page=page_number, sort_by=sort_by)
    raw_json_path = raw_dir / f"{safe_name(target.apple_app_id)}_{country}_{safe_name(sort_by)}_{page_number:03d}.json"
    fetched_at = utc_timestamp()
    last_error: str | None = None
    last_status_code: int | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(
                request_url,
                headers={
                    "Accept": "application/json,text/javascript",
                    "User-Agent": USER_AGENT,
                },
                timeout=timeout_seconds,
            )
            last_status_code = response.status_code
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_attempts:
                sleep_fn(retry_delay_seconds)
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("RSS response JSON was not an object")
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                sleep_fn(retry_delay_seconds)
                continue
            return error_page(
                target,
                page_key,
                run_id,
                country=country,
                sort_by=sort_by,
                page_number=page_number,
                request_url=request_url,
                status_code=last_status_code,
                fetched_at=fetched_at,
                attempt_count=attempt,
                error_message=last_error,
            ), []

        write_json(raw_json_path, payload)
        entries = payload_entries(payload)
        reviews = [
            parse_apple_review(
                entry,
                target,
                country=country,
                page_number=page_number,
                page_key=page_key,
                collected_at=fetched_at,
            )
            for entry in entries
        ]
        review_ids = [review.review_id for review in reviews if review.review_id]
        updated_epochs = [review.updated_epoch_seconds for review in reviews if review.updated_epoch_seconds is not None]
        page = ReviewPage(
            page_key=page_key,
            run_id=run_id,
            platform=PLATFORM,
            source=SOURCE,
            app_id=target.apple_app_id,
            app_name=target.app_name,
            country=country,
            sort_by=sort_by,
            page_number=page_number,
            request_url=request_url,
            status="ok" if 200 <= response.status_code < 300 else "error",
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
            has_next_link=payload_has_next_link(payload),
            attempt_count=attempt,
            error_message=None if 200 <= response.status_code < 300 else f"HTTP {response.status_code}",
            terminal_reason=None,
            overlap_review_count=0,
        )
        return page, reviews

    raise RuntimeError("unreachable fetch loop state")


def error_page(
    target: AppTarget,
    page_key: str,
    run_id: str,
    *,
    country: str,
    sort_by: str,
    page_number: int,
    request_url: str,
    status_code: int | None,
    fetched_at: str,
    attempt_count: int,
    error_message: str | None,
) -> ReviewPage:
    return ReviewPage(
        page_key=page_key,
        run_id=run_id,
        platform=PLATFORM,
        source=SOURCE,
        app_id=target.apple_app_id,
        app_name=target.app_name,
        country=country,
        sort_by=sort_by,
        page_number=page_number,
        request_url=request_url,
        status="error",
        status_code=status_code,
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
        attempt_count=attempt_count,
        error_message=error_message,
        terminal_reason=None,
        overlap_review_count=0,
    )
