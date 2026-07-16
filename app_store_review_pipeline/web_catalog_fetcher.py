from __future__ import annotations

import json
import random
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
    can_sleep_before_deadline,
    deadline_exceeded,
    final_attempt_stopped_for_time_budget,
    get_with_429_retries,
    jittered_delay_seconds,
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
    request_delay_jitter_seconds: float = 0.0,
    web_429_retries: int = 5,
    web_429_retry_seconds: float = 60.0,
    web_429_backoff_multiplier: float = 1.5,
    web_429_retry_jitter_seconds: float = 0.0,
    web_soft_retries: int = 2,
    web_soft_retry_seconds: float = 5.0,
    max_consecutive_sparse_fetch_errors: int = 3,
    time_budget_seconds: float = 0.0,
    scope_time_budget_seconds: float = 0.0,
    known_review_ids_by_scope: dict[tuple[str, str, str], set[str]] | None = None,
    target_review_counts_by_scope: dict[tuple[str, str, str], int] | None = None,
    use_overlap_stop: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    random_fn: Callable[[], float] = random.random,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    start_page = max(1, start_page)
    page_cap = (
        start_page + max_pages_per_app_country - 1
        if max_pages_per_app_country > 0
        else None
    )
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
    overall_time_budget_exceeded = False

    try:
        for target_index, target in enumerate(targets):
            if deadline_exceeded(deadline_monotonic, monotonic_fn):
                overall_time_budget_exceeded = True
                break
            if target_index and request_delay_seconds:
                target_delay_seconds = jittered_delay_seconds(
                    request_delay_seconds,
                    request_delay_jitter_seconds,
                    random_fn=random_fn,
                )
                if not sleep_with_deadline(
                    target_delay_seconds,
                    deadline_monotonic,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                ):
                    overall_time_budget_exceeded = True
                    break
            for country in target.countries:
                scope_deadline_monotonic = (
                    monotonic_fn() + scope_time_budget_seconds
                    if scope_time_budget_seconds and scope_time_budget_seconds > 0
                    else None
                )
                effective_deadline = earliest_deadline(deadline_monotonic, scope_deadline_monotonic)
                scope = (target.apple_app_id, country.lower(), sort_by)
                known_review_ids = known_review_ids_by_scope.get(scope, set())
                target_review_count = target_review_counts_by_scope.get(scope)
                scope_review_total = 0
                consecutive_sparse_fetch_errors = 0
                next_href: str | None = None
                page_number = start_page
                while page_cap is None or page_number <= page_cap:
                    stop_reason = deadline_stop_reason(deadline_monotonic, scope_deadline_monotonic, monotonic_fn)
                    if stop_reason:
                        warning_scopes.append(
                            {
                                "app_id": target.apple_app_id,
                                "app_name": target.app_name,
                                "country": country.lower(),
                                "sort_by": sort_by,
                                "reason": stop_reason,
                            }
                        )
                        mark_last_scope_page_terminal(
                            page_reports,
                            app_id=target.apple_app_id,
                            country=country.lower(),
                            sort_by=sort_by,
                            terminal_reason=stop_reason,
                        )
                        if stop_reason == "time_budget_exceeded":
                            overall_time_budget_exceeded = True
                        break
                    if page_number > start_page and request_delay_seconds:
                        page_delay_seconds = jittered_delay_seconds(
                            request_delay_seconds,
                            request_delay_jitter_seconds,
                            random_fn=random_fn,
                        )
                        if not sleep_with_deadline(
                            page_delay_seconds,
                            effective_deadline,
                            sleep_fn=sleep_fn,
                            monotonic_fn=monotonic_fn,
                        ):
                            stop_reason = deadline_sleep_stop_reason(
                                page_delay_seconds,
                                deadline_monotonic,
                                scope_deadline_monotonic,
                                monotonic_fn,
                            )
                            warning_scopes.append(
                                {
                                    "app_id": target.apple_app_id,
                                    "app_name": target.app_name,
                                    "country": country.lower(),
                                    "sort_by": sort_by,
                                    "reason": stop_reason,
                                }
                            )
                            mark_last_scope_page_terminal(
                                page_reports,
                                app_id=target.apple_app_id,
                                country=country.lower(),
                                sort_by=sort_by,
                                terminal_reason=stop_reason,
                            )
                            if stop_reason == "time_budget_exceeded":
                                overall_time_budget_exceeded = True
                            break
                    retry_budget_stop = request_retry_budget_stop_reason(
                        minimum_request_retry_budget_seconds(
                            timeout_seconds=timeout_seconds,
                            web_soft_retries=web_soft_retries,
                            web_soft_retry_seconds=web_soft_retry_seconds,
                        ),
                        deadline_monotonic,
                        scope_deadline_monotonic,
                        monotonic_fn,
                    )
                    if page_number > start_page and retry_budget_stop:
                        warning_scopes.append(
                            {
                                "app_id": target.apple_app_id,
                                "app_name": target.app_name,
                                "country": country.lower(),
                                "sort_by": sort_by,
                                "reason": retry_budget_stop,
                            }
                        )
                        mark_last_scope_page_terminal(
                            page_reports,
                            app_id=target.apple_app_id,
                            country=country.lower(),
                            sort_by=sort_by,
                            terminal_reason=retry_budget_stop,
                        )
                        if retry_budget_stop == "time_budget_retry_window_exceeded":
                            overall_time_budget_exceeded = True
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
                        web_429_retry_jitter_seconds=web_429_retry_jitter_seconds,
                        web_soft_retries=web_soft_retries,
                        web_soft_retry_seconds=web_soft_retry_seconds,
                        deadline_monotonic=effective_deadline,
                        monotonic_fn=monotonic_fn,
                        sleep_fn=sleep_fn,
                        random_fn=random_fn,
                    )
                    overlap_count = sum(1 for review in reviews if review.review_id in known_review_ids)
                    scope_review_total += len(reviews)
                    terminal_reason = web_terminal_reason_for_page(
                        page_report,
                        page_number=page_number,
                        start_page=start_page,
                        max_pages_per_app_country=page_cap,
                        overlap_count=overlap_count,
                        known_review_count=len(known_review_ids),
                        page_review_total=scope_review_total,
                        target_review_count=target_review_count,
                        next_href=next_href,
                        review_limit=review_limit,
                        use_overlap_stop=use_overlap_stop,
                        consecutive_sparse_fetch_errors=(
                            consecutive_sparse_fetch_errors + 1
                            if is_sparse_web_catalog_fetch_error(page_report)
                            else 0
                        ),
                        max_consecutive_sparse_fetch_errors=max_consecutive_sparse_fetch_errors,
                    )
                    page_report = replace(
                        page_report,
                        terminal_reason=terminal_reason,
                        overlap_review_count=overlap_count,
                    )
                    page_reports.append(asdict(page_report))
                    review_rows.extend(asdict(review) for review in reviews)

                    if is_sparse_web_catalog_fetch_error(page_report):
                        consecutive_sparse_fetch_errors += 1
                    else:
                        consecutive_sparse_fetch_errors = 0

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
                        if terminal_reason in {
                            "page_cap",
                            "fetch_error",
                            "sparse_fetch_error_threshold",
                            "time_budget_exceeded",
                            "scope_time_budget_exceeded",
                            "time_budget_retry_window_exceeded",
                            "scope_time_budget_retry_window_exceeded",
                        }:
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
                if overall_time_budget_exceeded:
                    break
            if overall_time_budget_exceeded:
                break
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
        "last_allowed_page_number": page_cap,
        "page_cap_enabled": page_cap is not None,
        "request_delay_seconds": request_delay_seconds,
        "request_delay_jitter_seconds": request_delay_jitter_seconds,
        "web_429_retries": web_429_retries,
        "web_429_retry_seconds": web_429_retry_seconds,
        "web_429_retry_jitter_seconds": web_429_retry_jitter_seconds,
        "time_budget_seconds": time_budget_seconds,
        "scope_time_budget_seconds": scope_time_budget_seconds,
        "max_consecutive_sparse_fetch_errors": max_consecutive_sparse_fetch_errors,
        "overall_time_budget_exceeded": overall_time_budget_exceeded,
        "page_reports": page_reports,
        "reviews": review_rows,
        "fetched_pages": sum(1 for page in page_reports if page.get("status") == "ok"),
        "fetch_errors": sum(1 for page in page_reports if page.get("status") == "error"),
        "sparse_fetch_error_pages": sum(
            1
            for page in page_reports
            if page.get("status") == "error"
            and page.get("status_code") == 404
            and not page.get("terminal_reason")
        ),
        "empty_pages": sum(1 for page in page_reports if page.get("status") == "ok" and page.get("review_count") == 0),
        "review_count": len(review_rows),
        "unique_review_count": len({row.get("review_key") for row in review_rows if row.get("review_key")}),
        "capped_scopes": capped_scopes,
        "warning_scopes": warning_scopes,
        "target_review_counts_enabled": bool(target_review_counts_by_scope),
        "target_review_count_scopes": len(target_review_counts_by_scope),
        "target_reached_scopes": target_reached_scopes,
    }


def mark_last_scope_page_terminal(
    page_reports: list[dict[str, Any]],
    *,
    app_id: str,
    country: str,
    sort_by: str,
    terminal_reason: str,
) -> bool:
    for row in reversed(page_reports):
        if (
            str(row.get("app_id")) == str(app_id)
            and str(row.get("country") or "").lower() == country.lower()
            and str(row.get("sort_by") or "") == sort_by
        ):
            if not row.get("terminal_reason"):
                row["terminal_reason"] = terminal_reason
            return True
    return False


def earliest_deadline(*deadlines: float | None) -> float | None:
    active_deadlines = [deadline for deadline in deadlines if deadline is not None]
    if not active_deadlines:
        return None
    return min(active_deadlines)


def deadline_stop_reason(
    overall_deadline_monotonic: float | None,
    scope_deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
) -> str | None:
    if deadline_exceeded(overall_deadline_monotonic, monotonic_fn):
        return "time_budget_exceeded"
    if deadline_exceeded(scope_deadline_monotonic, monotonic_fn):
        return "scope_time_budget_exceeded"
    return None


def deadline_sleep_stop_reason(
    seconds: float,
    overall_deadline_monotonic: float | None,
    scope_deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
) -> str:
    if not can_sleep_before_deadline(seconds, overall_deadline_monotonic, monotonic_fn):
        return "time_budget_exceeded"
    if not can_sleep_before_deadline(seconds, scope_deadline_monotonic, monotonic_fn):
        return "scope_time_budget_exceeded"
    return deadline_stop_reason(overall_deadline_monotonic, scope_deadline_monotonic, monotonic_fn) or "time_budget_exceeded"


def minimum_request_retry_budget_seconds(
    *,
    timeout_seconds: float,
    web_soft_retries: int,
    web_soft_retry_seconds: float,
) -> float:
    if web_soft_retries <= 0:
        return 0.0
    return max(1.0, float(timeout_seconds)) + max(0.0, float(web_soft_retry_seconds))


def request_retry_budget_stop_reason(
    seconds: float,
    overall_deadline_monotonic: float | None,
    scope_deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
) -> str | None:
    if seconds <= 0:
        return None
    if not can_sleep_before_deadline(seconds, overall_deadline_monotonic, monotonic_fn):
        return "time_budget_retry_window_exceeded"
    if not can_sleep_before_deadline(seconds, scope_deadline_monotonic, monotonic_fn):
        return "scope_time_budget_retry_window_exceeded"
    return None


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
    web_429_retry_jitter_seconds: float,
    web_soft_retries: int,
    web_soft_retry_seconds: float,
    deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
    random_fn: Callable[[], float],
) -> tuple[ReviewPage, list[AppReview], str | None]:
    country = country.lower()
    page_key = make_page_key(run_id, target.apple_app_id, country, sort_by, page_number)
    if page_number == start_page or not next_href:
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

    response: requests.Response | None = None
    payload: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    last_status_code: int | None = None
    last_response_bytes = 0
    last_body_preview = ""
    max_soft_attempts = max(1, int(web_soft_retries) + 1)
    soft_retry_delay = max(0.0, float(web_soft_retry_seconds))
    soft_attempts_made = 0

    for soft_attempt_number in range(1, max_soft_attempts + 1):
        soft_attempts_made = soft_attempt_number
        try:
            response, current_attempts = get_with_429_retries(
                session,
                request_url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                web_429_retries=web_429_retries,
                web_429_retry_seconds=web_429_retry_seconds,
                web_429_backoff_multiplier=web_429_backoff_multiplier,
                web_429_retry_jitter_seconds=web_429_retry_jitter_seconds,
                deadline_monotonic=deadline_monotonic,
                monotonic_fn=monotonic_fn,
                sleep_fn=sleep_fn,
                random_fn=random_fn,
            )
            for attempt in current_attempts:
                attempt["soft_attempt_number"] = soft_attempt_number
            attempts.extend(current_attempts)
            last_status_code = response.status_code
            last_response_bytes = len(response.content or b"")
            last_body_preview = response.text[:160] if response.text else ""
            payload_candidate = response.json()
            if not isinstance(payload_candidate, dict):
                raise ValueError("web catalog response JSON was not an object")
            payload = payload_candidate
            break
        except requests.exceptions.JSONDecodeError as exc:
            last_error = exc
        except requests.RequestException as exc:
            last_error = exc
            last_status_code = None
            last_response_bytes = 0
            last_body_preview = ""
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc

        is_soft_error = last_status_code is None or 200 <= last_status_code < 300
        if not is_soft_error or soft_attempt_number >= max_soft_attempts:
            break
        if soft_retry_delay and not can_sleep_before_deadline(
            soft_retry_delay,
            deadline_monotonic,
            monotonic_fn,
        ):
            if attempts:
                attempts[-1]["retry_skipped_reason"] = "soft_retry_time_budget_exceeded"
            break
        if soft_retry_delay:
            sleep_fn(soft_retry_delay)

    if payload is None or response is None:
        page = web_error_page(
            target,
            page_key,
            run_id,
            country=country,
            sort_by=sort_by,
            page_number=page_number,
            request_url=request_url,
            fetched_at=fetched_at,
            status_code=last_status_code,
            response_bytes=last_response_bytes,
            attempt_count=max(1, len(attempts), soft_attempts_made),
            error_message=web_catalog_error_message(
                last_error,
                status_code=last_status_code,
                response_bytes=last_response_bytes,
                body_preview=last_body_preview,
            ),
        )
        return replace(
            page,
            http_429_attempt_count=sum(
                int(attempt.get("status_code") == 429) for attempt in attempts
            ),
            soft_retry_count=max(0, soft_attempts_made - 1),
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
        http_429_attempt_count=sum(
            int(attempt.get("status_code") == 429) for attempt in attempts
        ),
        soft_retry_count=max(0, soft_attempts_made - 1),
    )
    if final_attempt_stopped_for_time_budget([{"attempts": attempts}]):
        page = replace(page, terminal_reason="time_budget_exceeded")
    return page, reviews, summary["next_href"]


def web_terminal_reason_for_page(
    page_report: ReviewPage,
    *,
    page_number: int,
    start_page: int,
    max_pages_per_app_country: int | None,
    overlap_count: int,
    known_review_count: int,
    page_review_total: int,
    target_review_count: int | None,
    next_href: str | None,
    use_overlap_stop: bool,
    review_limit: int | None = None,
    consecutive_sparse_fetch_errors: int = 0,
    max_consecutive_sparse_fetch_errors: int = 3,
) -> str | None:
    if page_report.terminal_reason:
        return page_report.terminal_reason
    if page_report.status != "ok":
        if is_sparse_web_catalog_fetch_error(page_report):
            threshold = max(1, max_consecutive_sparse_fetch_errors)
            if consecutive_sparse_fetch_errors < threshold:
                return None
            return "sparse_fetch_error_threshold"
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
        if start_page > 1 and page_number == start_page and page_report.review_count > 0:
            return None
        if review_limit is not None and page_report.review_count >= review_limit:
            return None
        return "no_next_href"
    if page_report.review_count == 0:
        return "empty_page"
    return None


def is_sparse_web_catalog_fetch_error(page_report: ReviewPage) -> bool:
    return page_report.status == "error" and page_report.status_code == 404


def web_catalog_error_message(
    error: Exception | None,
    *,
    status_code: int | None,
    response_bytes: int,
    body_preview: str,
) -> str:
    parts = [str(error) if error else "web catalog response could not be parsed"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    parts.append(f"response_bytes={response_bytes}")
    if body_preview:
        preview = " ".join(body_preview.split())
        parts.append(f"body_preview={preview[:160]!r}")
    return "; ".join(parts)


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
    status_code: int | None = None,
    response_bytes: int = 0,
    attempt_count: int = 1,
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
        status_code=status_code,
        fetched_at=fetched_at,
        raw_json_path=None,
        response_bytes=response_bytes,
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
