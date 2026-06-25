from __future__ import annotations

import html
import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from app_store_review_pipeline.config import PLATFORM, WEB_CATALOG_SOURCE
from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.models import AppReview, AppTarget, make_review_key
from app_store_review_pipeline.utils import clean_text, iso_to_epoch_seconds, utc_timestamp


WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)


def app_store_reviews_page_url(target: AppTarget, country: str) -> str:
    slug = quote(target.apple_slug, safe="-")
    return f"https://apps.apple.com/{country.lower()}/app/{slug}/id{target.apple_app_id}?see-all=reviews&platform=iphone"


def app_store_web_catalog_url(app_id: str, country: str, *, language: str = "en-US", review_limit: int = 20) -> str:
    return (
        f"https://apps.apple.com/api/apps/v1/catalog/{country.lower()}/apps/{app_id}"
        f"?platform=iphone&include=developer%2Creviews"
        f"&sparseLimit%5Bapps%3Areviews%5D={review_limit}&l={language}"
    )


def app_store_web_reviews_url(
    app_id: str,
    country: str,
    *,
    language: str = "en-US",
    offset: int = 0,
    sort: str = "recent",
    platform: str = "iphone",
    limit: int | None = 20,
) -> str:
    params = {
        "l": language,
        "offset": str(offset),
        "platform": platform,
        "sort": sort,
    }
    if limit is not None:
        params["limit"] = str(limit)
    return f"https://apps.apple.com/api/apps/v1/catalog/{country.lower()}/apps/{app_id}/reviews?{urlencode(params)}"


def app_store_web_catalog_next_url(
    next_href: str,
    *,
    platform: str = "iphone",
    sort: str = "recent",
    limit: int | None = 20,
) -> str:
    url = next_href if next_href.startswith("https://") else f"https://apps.apple.com/api/apps{next_href}"
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params.setdefault("platform", platform)
    params.setdefault("sort", sort)
    if limit is not None:
        params.setdefault("limit", str(limit))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def parse_html_review_ids(page_html: str) -> list[str]:
    return sorted(set(re.findall(r'id=["\']review-([0-9]+)-title["\']', page_html)))


def parse_serialized_next_href(page_html: str) -> str | None:
    match = re.search(r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>', page_html, re.S)
    if not match:
        return None
    raw = html.unescape(match.group(1))
    next_match = re.search(r'"nextHref"\s*:\s*"([^"]+)"', raw)
    if next_match:
        return next_match.group(1).replace("\\/", "/")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return first_value_for_key(payload, "nextHref")


def first_value_for_key(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
        for child in value.values():
            found = first_value_for_key(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_value_for_key(child, key)
            if found:
                return found
    return None


def parse_json_ld_aggregate_rating(page_html: str) -> dict[str, Any]:
    scripts = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.S,
    )
    for script in scripts:
        try:
            payload = json.loads(html.unescape(script))
        except json.JSONDecodeError:
            continue
        rating = find_aggregate_rating(payload)
        if rating:
            return rating
    return {}


def find_aggregate_rating(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        aggregate = value.get("aggregateRating")
        if isinstance(aggregate, dict):
            return {
                "rating_value": parse_float(aggregate.get("ratingValue")),
                "rating_count": parse_int(aggregate.get("ratingCount")),
                "review_count": parse_int(aggregate.get("reviewCount")),
            }
        for child in value.values():
            found = find_aggregate_rating(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_aggregate_rating(child)
            if found:
                return found
    return {}


def parse_web_catalog_reviews(payload: dict[str, Any]) -> dict[str, Any]:
    app_data = payload.get("data")
    if not isinstance(app_data, list) or not app_data:
        return {"review_count": 0, "review_ids": [], "next_href": None, "min_date": None, "max_date": None}
    relationships = app_data[0].get("relationships")
    if not isinstance(relationships, dict):
        return {"review_count": 0, "review_ids": [], "next_href": None, "min_date": None, "max_date": None}
    reviews = relationships.get("reviews")
    if not isinstance(reviews, dict):
        return {"review_count": 0, "review_ids": [], "next_href": None, "min_date": None, "max_date": None}
    rows = reviews.get("data")
    if not isinstance(rows, list):
        rows = []
    review_ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
    dates = [
        attrs.get("date")
        for row in rows
        if isinstance(row, dict)
        for attrs in [row.get("attributes")]
        if isinstance(attrs, dict) and attrs.get("date")
    ]
    return {
        "review_count": len(rows),
        "review_ids": review_ids,
        "next_href": reviews.get("next") if isinstance(reviews.get("next"), str) else None,
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
    }


def parse_web_catalog_review_page(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        rows = []
    review_ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
    dates = [
        attrs.get("date")
        for row in rows
        if isinstance(row, dict)
        for attrs in [row.get("attributes")]
        if isinstance(attrs, dict) and attrs.get("date")
    ]
    next_href = payload.get("next")
    return {
        "review_count": len(rows),
        "review_ids": review_ids,
        "next_href": next_href if isinstance(next_href, str) else None,
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
    }


def parse_web_catalog_review_rows(
    payload: dict[str, Any],
    target: AppTarget,
    *,
    country: str,
    page_number: int,
    page_key: str,
    collected_at: str,
) -> list[AppReview]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    reviews: list[AppReview] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        review = parse_web_catalog_review_row(
            row,
            target,
            country=country,
            page_number=page_number,
            page_key=page_key,
            collected_at=collected_at,
        )
        if review:
            reviews.append(review)
    return reviews


def parse_web_catalog_review_row(
    row: dict[str, Any],
    target: AppTarget,
    *,
    country: str,
    page_number: int,
    page_key: str,
    collected_at: str,
) -> AppReview | None:
    review_id = row.get("id")
    if review_id is None:
        return None
    attrs = row.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    review_id = str(review_id)
    updated_at = string_or_none(attrs.get("date"))
    return AppReview(
        review_key=make_review_key(target.apple_app_id, country, review_id, source=WEB_CATALOG_SOURCE),
        platform=PLATFORM,
        source=WEB_CATALOG_SOURCE,
        app_id=target.apple_app_id,
        app_name=target.app_name,
        country=country.lower(),
        review_id=review_id,
        author_name=clean_text(string_or_none(attrs.get("userName"))),
        updated_at=updated_at,
        updated_epoch_seconds=iso_to_epoch_seconds(updated_at),
        rating=parse_int(attrs.get("rating")),
        title=clean_text(string_or_none(attrs.get("title"))),
        content=clean_text(string_or_none(attrs.get("review"))),
        page_number=page_number,
        source_page_key=page_key,
        collected_at=collected_at,
    )


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def probe_web_reviews(
    targets: list[AppTarget],
    output_path: Path,
    *,
    limit: int = 20,
    timeout_seconds: float = 20.0,
    request_delay_seconds: float = 0.5,
    review_limit: int = 20,
    web_sort: str = "recent",
    attempt_pagination: bool = False,
    max_web_pages: int = 2,
    web_429_retries: int = 0,
    web_429_retry_seconds: float = 30.0,
    web_429_backoff_multiplier: float = 1.0,
    include_html: bool = True,
    target_review_counts_by_scope: dict[tuple[str, str], int] | None = None,
    time_budget_seconds: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    selected = targets[:limit] if limit > 0 else targets
    planned_scope_count = sum(len(target.countries) for target in selected)
    deadline_monotonic = (
        monotonic_fn() + time_budget_seconds
        if time_budget_seconds is not None and time_budget_seconds > 0
        else None
    )
    owned_session = session is None
    http = session or requests.Session()
    rows: list[dict[str, Any]] = []
    time_budget_exceeded = False

    try:
        for target_index, target in enumerate(selected):
            if deadline_exceeded(deadline_monotonic, monotonic_fn):
                time_budget_exceeded = True
                break
            if target_index and request_delay_seconds:
                if not sleep_with_deadline(
                    request_delay_seconds,
                    deadline_monotonic,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                ):
                    time_budget_exceeded = True
                    break
            for country in target.countries:
                if deadline_exceeded(deadline_monotonic, monotonic_fn):
                    time_budget_exceeded = True
                    break
                scope_key = (target.apple_app_id, country.lower())
                row = probe_web_reviews_for_scope(
                    target,
                    country,
                    session=http,
                    timeout_seconds=timeout_seconds,
                    review_limit=review_limit,
                    web_sort=web_sort,
                    attempt_pagination=attempt_pagination,
                    max_web_pages=max_web_pages if attempt_pagination else 1,
                    request_delay_seconds=request_delay_seconds,
                    web_429_retries=web_429_retries,
                    web_429_retry_seconds=web_429_retry_seconds,
                    web_429_backoff_multiplier=web_429_backoff_multiplier,
                    include_html=include_html,
                    target_review_count=(
                        target_review_counts_by_scope.get(scope_key)
                        if target_review_counts_by_scope is not None
                        else None
                    ),
                    deadline_monotonic=deadline_monotonic,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )
                rows.append(row)
                if row.get("web_catalog_stop_reason") == "time_budget_exceeded":
                    time_budget_exceeded = True
                    break
            if time_budget_exceeded:
                break
    finally:
        if owned_session:
            http.close()

    summary = summarize_web_probe(rows)
    summary.update(
        {
            "planned_scope_count": planned_scope_count,
            "completed_scope_count": len(rows),
            "skipped_scope_count": max(0, planned_scope_count - len(rows)),
            "time_budget_seconds": time_budget_seconds,
            "time_budget_exceeded": time_budget_exceeded,
        }
    )
    report = {
        "generated_at": utc_timestamp(),
        "source": "apple_app_store_public_web_probe",
        "target_count": len(selected),
        "planned_scope_count": planned_scope_count,
        "scope_count": len(rows),
        "attempt_pagination": attempt_pagination,
        "web_sort": web_sort,
        "web_review_limit": review_limit,
        "max_web_pages": max_web_pages if attempt_pagination else 1,
        "web_429_retries": web_429_retries,
        "web_429_retry_seconds": web_429_retry_seconds,
        "web_429_backoff_multiplier": web_429_backoff_multiplier,
        "include_html": include_html,
        "target_review_counts_enabled": target_review_counts_by_scope is not None,
        "time_budget_seconds": time_budget_seconds,
        "time_budget_exceeded": time_budget_exceeded,
        "summary": summary,
        "results": rows,
    }
    write_json(output_path, report)
    return report


def probe_web_reviews_for_scope(
    target: AppTarget,
    country: str,
    *,
    session: requests.Session,
    timeout_seconds: float,
    review_limit: int,
    web_sort: str,
    attempt_pagination: bool,
    max_web_pages: int,
    request_delay_seconds: float,
    web_429_retries: int,
    web_429_retry_seconds: float,
    web_429_backoff_multiplier: float,
    include_html: bool,
    sleep_fn: Callable[[float], None],
    target_review_count: int | None = None,
    deadline_monotonic: float | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    country = country.lower()
    page_url = app_store_reviews_page_url(target, country)
    catalog_url = app_store_web_reviews_url(target.apple_app_id, country, sort=web_sort, limit=review_limit)
    page_status_code = None
    page_response_bytes = 0
    aggregate = {}
    html_review_ids: list[str] = []
    serialized_next_href = None
    if include_html:
        headers = {"User-Agent": WEB_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        page_response = session.get(page_url, headers=headers, timeout=timeout_seconds)
        page_status_code = page_response.status_code
        page_response_bytes = len(page_response.content or b"")
        page_text = page_response.text if page_response.text else ""
        aggregate = parse_json_ld_aggregate_rating(page_text)
        html_review_ids = parse_html_review_ids(page_text)
        serialized_next_href = parse_serialized_next_href(page_text)

    catalog_headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": page_url,
        "Origin": "https://apps.apple.com",
    }
    catalog_status_code = None
    catalog_response_bytes = 0
    catalog_review_summary = {"review_count": 0, "review_ids": [], "next_href": None, "min_date": None, "max_date": None}
    catalog_error = None
    catalog_response, catalog_attempts = get_with_429_retries(
        session,
        catalog_url,
        headers=catalog_headers,
        timeout_seconds=timeout_seconds,
        web_429_retries=web_429_retries,
        web_429_retry_seconds=web_429_retry_seconds,
        web_429_backoff_multiplier=web_429_backoff_multiplier,
        deadline_monotonic=deadline_monotonic,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    catalog_status_code = catalog_response.status_code
    catalog_response_bytes = len(catalog_response.content or b"")
    try:
        catalog_payload = catalog_response.json()
        if isinstance(catalog_payload, dict):
            catalog_review_summary = parse_web_catalog_review_page(catalog_payload)
        else:
            catalog_error = "catalog JSON was not an object"
    except (ValueError, json.JSONDecodeError) as exc:
        catalog_error = str(exc)

    web_catalog_pages = [
        {
            "page_index": 1,
            "request_url": catalog_url,
            "status_code": catalog_status_code,
            "response_bytes": catalog_response_bytes,
            "content_type": catalog_response.headers.get("content-type"),
            "response_headers": selected_response_headers(catalog_response),
            "attempt_count": len(catalog_attempts),
            "attempts": catalog_attempts,
            "review_count": catalog_review_summary["review_count"],
            "review_ids": catalog_review_summary["review_ids"],
            "next_href": catalog_review_summary["next_href"],
            "min_date": catalog_review_summary["min_date"],
            "max_date": catalog_review_summary["max_date"],
            "body_preview": catalog_response.text[:120] if catalog_response.text and catalog_status_code != 200 else "",
        }
    ]
    next_href = catalog_review_summary.get("next_href") or serialized_next_href
    page_index = 2
    time_budget_exceeded = final_attempt_stopped_for_time_budget(web_catalog_pages)
    while attempt_pagination and next_href and page_index <= max_web_pages:
        if deadline_exceeded(deadline_monotonic, monotonic_fn):
            time_budget_exceeded = True
            break
        if target_review_count is not None and (
            target_review_count <= 0 or web_catalog_page_review_total(web_catalog_pages) >= target_review_count
        ):
            break
        if request_delay_seconds:
            if not sleep_with_deadline(
                request_delay_seconds,
                deadline_monotonic,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            ):
                time_budget_exceeded = True
                break
        next_url = app_store_web_catalog_next_url(str(next_href), sort=web_sort, limit=review_limit)
        next_response, next_attempts = get_with_429_retries(
            session,
            next_url,
            headers=catalog_headers,
            timeout_seconds=timeout_seconds,
            web_429_retries=web_429_retries,
            web_429_retry_seconds=web_429_retry_seconds,
            web_429_backoff_multiplier=web_429_backoff_multiplier,
            deadline_monotonic=deadline_monotonic,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        next_summary = {"review_count": 0, "review_ids": [], "next_href": None, "min_date": None, "max_date": None}
        next_error = None
        try:
            next_payload = next_response.json()
            if isinstance(next_payload, dict):
                next_summary = parse_web_catalog_review_page(next_payload)
            else:
                next_error = "next-page JSON was not an object"
        except (ValueError, json.JSONDecodeError) as exc:
            next_error = str(exc)
        web_catalog_pages.append(
            {
                "page_index": page_index,
                "request_url": next_url,
                "status_code": next_response.status_code,
                "response_bytes": len(next_response.content or b""),
                "content_type": next_response.headers.get("content-type"),
                "response_headers": selected_response_headers(next_response),
                "attempt_count": len(next_attempts),
                "attempts": next_attempts,
                "review_count": next_summary["review_count"],
                "review_ids": next_summary["review_ids"],
                "next_href": next_summary["next_href"],
                "min_date": next_summary["min_date"],
                "max_date": next_summary["max_date"],
                "error": next_error,
                "body_preview": next_response.text[:120] if next_response.text and next_response.status_code != 200 else "",
            }
        )
        if next_response.status_code != 200 or not next_summary["next_href"]:
            if final_attempt_stopped_for_time_budget(web_catalog_pages):
                time_budget_exceeded = True
            break
        next_href = next_summary["next_href"]
        page_index += 1

    page_review_total = web_catalog_page_review_total(web_catalog_pages)
    target_reached = (
        target_review_count is not None and target_review_count > 0 and page_review_total >= target_review_count
    )
    next_probe = None
    if len(web_catalog_pages) > 1:
        second_page = web_catalog_pages[1]
        next_probe = {
            "request_url": second_page["request_url"],
            "status_code": second_page["status_code"],
            "response_bytes": second_page["response_bytes"],
            "content_type": second_page["content_type"],
            "review_count": second_page["review_count"],
            "next_href": second_page["next_href"],
            "body_preview": second_page["body_preview"],
        }

    return {
        "app_id": target.apple_app_id,
        "app_name": target.app_name,
        "country": country,
        "html_page_url": page_url,
        "html_probe_enabled": include_html,
        "html_status_code": page_status_code,
        "html_response_bytes": page_response_bytes,
        "html_review_card_count": len(html_review_ids),
        "html_review_ids": html_review_ids,
        "html_aggregate_rating": aggregate,
        "html_serialized_next_href": serialized_next_href,
        "web_catalog_url": catalog_url,
        "web_catalog_status_code": catalog_status_code,
        "web_catalog_response_bytes": catalog_response_bytes,
        "web_catalog_error": catalog_error,
        "web_catalog_review_count": catalog_review_summary["review_count"],
        "web_catalog_review_ids": catalog_review_summary["review_ids"],
        "web_catalog_next_href": catalog_review_summary["next_href"],
        "web_catalog_min_date": catalog_review_summary["min_date"],
        "web_catalog_max_date": catalog_review_summary["max_date"],
        "web_catalog_pages": web_catalog_pages,
        "web_catalog_pages_fetched": len(web_catalog_pages),
        "web_catalog_page_reviews_total": page_review_total,
        "web_catalog_target_review_count": target_review_count,
        "web_catalog_target_reached": target_reached,
        "web_catalog_stop_reason": web_catalog_stop_reason(
            attempt_pagination=attempt_pagination,
            max_web_pages=max_web_pages,
            next_href=next_href,
            pages=web_catalog_pages,
            target_review_count=target_review_count,
            target_reached=target_reached,
            time_budget_exceeded=time_budget_exceeded,
        ),
        "web_catalog_next_probe": next_probe,
    }


def web_catalog_page_review_total(pages: list[dict[str, Any]]) -> int:
    return sum(int(page.get("review_count") or 0) for page in pages)


def web_catalog_stop_reason(
    *,
    attempt_pagination: bool,
    max_web_pages: int,
    next_href: str | None,
    pages: list[dict[str, Any]],
    target_review_count: int | None,
    target_reached: bool,
    time_budget_exceeded: bool = False,
) -> str:
    if not attempt_pagination:
        return "not_paginated"
    if time_budget_exceeded:
        return "time_budget_exceeded"
    if target_review_count is not None and target_review_count <= 0:
        return "target_review_count_zero"
    if target_reached:
        return "target_review_count_reached"
    if pages and pages[-1].get("status_code") != 200:
        return "non_200_page"
    if not next_href:
        return "no_next_href"
    if len(pages) >= max_web_pages:
        return "max_pages"
    return "unknown"


def summarize_web_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pagination_status_counts: dict[str, int] = {}
    page_status_counts: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    retried_page_count = 0
    recovered_429_page_count = 0
    for row in rows:
        stop_reason = str(row.get("web_catalog_stop_reason") or "unknown")
        stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1
        probe = row.get("web_catalog_next_probe") or {}
        status = probe.get("status_code")
        if status is not None:
            key = str(status)
            pagination_status_counts[key] = pagination_status_counts.get(key, 0) + 1
        for page in row.get("web_catalog_pages") or []:
            page_status = page.get("status_code")
            if page_status is not None:
                key = str(page_status)
                page_status_counts[key] = page_status_counts.get(key, 0) + 1
            attempts = page.get("attempts") or []
            if len(attempts) > 1:
                retried_page_count += 1
                if any(attempt.get("status_code") == 429 for attempt in attempts[:-1]) and page_status == 200:
                    recovered_429_page_count += 1

    return {
        "html_ok_scopes": sum(1 for row in rows if row.get("html_status_code") == 200),
        "html_review_card_scopes": sum(1 for row in rows if row.get("html_review_card_count", 0) > 0),
        "html_review_cards_total": sum(int(row.get("html_review_card_count") or 0) for row in rows),
        "web_catalog_ok_scopes": sum(1 for row in rows if row.get("web_catalog_status_code") == 200),
        "web_catalog_review_scopes": sum(1 for row in rows if row.get("web_catalog_review_count", 0) > 0),
        "web_catalog_reviews_total": sum(int(row.get("web_catalog_review_count") or 0) for row in rows),
        "web_catalog_pages_total": sum(int(row.get("web_catalog_pages_fetched") or 0) for row in rows),
        "web_catalog_page_reviews_total": sum(int(row.get("web_catalog_page_reviews_total") or 0) for row in rows),
        "web_catalog_targeted_scopes": sum(1 for row in rows if row.get("web_catalog_target_review_count") is not None),
        "web_catalog_target_reached_scopes": sum(1 for row in rows if row.get("web_catalog_target_reached") is True),
        "web_catalog_stop_reasons": stop_reasons,
        "next_href_scopes": sum(
            1 for row in rows if row.get("web_catalog_next_href") or row.get("html_serialized_next_href")
        ),
        "pagination_status_counts": pagination_status_counts,
        "web_catalog_page_status_counts": page_status_counts,
        "retried_page_count": retried_page_count,
        "recovered_429_page_count": recovered_429_page_count,
    }


def get_with_429_retries(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    web_429_retries: int,
    web_429_retry_seconds: float,
    web_429_backoff_multiplier: float,
    sleep_fn: Callable[[float], None],
    web_429_retry_jitter_seconds: float = 0.0,
    deadline_monotonic: float | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    random_fn: Callable[[], float] = random.random,
) -> tuple[requests.Response, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    max_attempts = max(1, web_429_retries + 1)
    response = None
    for attempt_number in range(1, max_attempts + 1):
        response = session.get(url, headers=headers, timeout=timeout_seconds)
        attempts.append(
            {
                "attempt_number": attempt_number,
                "status_code": response.status_code,
                "response_bytes": len(response.content or b""),
                "response_headers": selected_response_headers(response),
            }
        )
        if response.status_code != 429 or attempt_number >= max_attempts:
            break
        delay_seconds = retry_delay_seconds(
            response,
            attempt_number,
            web_429_retry_seconds,
            web_429_backoff_multiplier,
            jitter_seconds=web_429_retry_jitter_seconds,
            random_fn=random_fn,
        )
        attempts[-1]["retry_delay_seconds"] = delay_seconds
        if not can_sleep_before_deadline(delay_seconds, deadline_monotonic, monotonic_fn):
            attempts[-1]["retry_skipped_reason"] = "time_budget_exceeded"
            break
        sleep_fn(delay_seconds)
    if response is None:
        raise RuntimeError("unreachable web request state")
    return response, attempts


def deadline_exceeded(deadline_monotonic: float | None, monotonic_fn: Callable[[], float]) -> bool:
    return deadline_monotonic is not None and monotonic_fn() >= deadline_monotonic


def sleep_with_deadline(
    seconds: float,
    deadline_monotonic: float | None,
    *,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> bool:
    if seconds <= 0:
        return not deadline_exceeded(deadline_monotonic, monotonic_fn)
    if not can_sleep_before_deadline(seconds, deadline_monotonic, monotonic_fn):
        return False
    sleep_fn(seconds)
    return not deadline_exceeded(deadline_monotonic, monotonic_fn)


def can_sleep_before_deadline(
    seconds: float,
    deadline_monotonic: float | None,
    monotonic_fn: Callable[[], float],
) -> bool:
    if deadline_monotonic is None:
        return True
    remaining = deadline_monotonic - monotonic_fn()
    return remaining > seconds


def final_attempt_stopped_for_time_budget(pages: list[dict[str, Any]]) -> bool:
    if not pages:
        return False
    attempts = pages[-1].get("attempts") or []
    return bool(attempts and attempts[-1].get("retry_skipped_reason") == "time_budget_exceeded")


def retry_delay_seconds(
    response: requests.Response,
    attempt_number: int,
    base_delay_seconds: float,
    backoff_multiplier: float,
    *,
    jitter_seconds: float = 0.0,
    random_fn: Callable[[], float] = random.random,
) -> float:
    retry_after = parse_retry_after_seconds(response.headers.get("retry-after"))
    if retry_after is not None:
        return jittered_delay_seconds(retry_after, jitter_seconds, random_fn=random_fn)
    multiplier = max(1.0, backoff_multiplier)
    delay = base_delay_seconds * (multiplier ** max(0, attempt_number - 1))
    return jittered_delay_seconds(delay, jitter_seconds, random_fn=random_fn)


def jittered_delay_seconds(
    base_delay_seconds: float,
    jitter_seconds: float = 0.0,
    *,
    random_fn: Callable[[], float] = random.random,
) -> float:
    base = max(0.0, float(base_delay_seconds))
    jitter = max(0.0, float(jitter_seconds))
    if not jitter:
        return base
    return base + (max(0.0, min(1.0, float(random_fn()))) * jitter)


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delay)


def selected_response_headers(response: requests.Response) -> dict[str, str]:
    interesting = {
        "retry-after",
        "x-cache",
        "x-cache-remote",
        "x-apple-jingle-correlation-key",
        "content-type",
        "date",
    }
    return {key: value for key, value in response.headers.items() if key.lower() in interesting}
