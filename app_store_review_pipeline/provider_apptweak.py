from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.utils import utc_timestamp


REVIEWS_SEARCH_URL = "https://public-api.apptweak.com/api/public/store/apps/reviews/search.json"


def build_apptweak_reviews_url(
    app_id: str,
    *,
    country: str = "us",
    language: str = "us",
    device: str = "iphone",
    limit: int = 500,
    offset: int = 0,
    replied: str = "nil",
    term: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    params = {
        "apps": app_id,
        "country": country.lower(),
        "language": language.lower(),
        "device": device,
        "limit": str(limit),
        "offset": str(offset),
        "replied": replied,
    }
    if term:
        params["term"] = term
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return f"{REVIEWS_SEARCH_URL}?{urlencode(params)}"


def apptweak_headers(api_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Apptweak-Key": api_token,
    }


def parse_apptweak_reviews_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rows = extract_apptweak_review_rows(payload)
    dates = [review_date(row) for row in rows]
    dates = [value for value in dates if value]
    return {
        "review_count": len(rows),
        "total_reviews": extract_apptweak_total_reviews(payload),
        "missing_content_count": sum(1 for row in rows if not review_content(row)),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "review_fingerprints": [review_fingerprint(row) for row in rows],
    }


def extract_apptweak_review_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content")
    if isinstance(content, list):
        return [row for row in content if isinstance(row, dict)]
    if isinstance(content, dict):
        for key in ("reviews", "data", "results", "items"):
            rows = content.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    for key in ("reviews", "data", "results", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def extract_apptweak_total_reviews(payload: dict[str, Any]) -> int | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        content_meta = metadata.get("content")
        if isinstance(content_meta, dict):
            for key in ("total_size", "total", "total_reviews", "count"):
                value = parse_int(content_meta.get(key))
                if value is not None:
                    return value
    for key in ("total_size", "total", "total_reviews", "count"):
        value = parse_int(payload.get(key))
        if value is not None:
            return value
    return None


def review_content(row: dict[str, Any]) -> str | None:
    for key in ("content", "body", "text", "review", "message"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def review_date(row: dict[str, Any]) -> str | None:
    for key in ("date", "posted_at", "updated_at", "created_at"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def review_fingerprint(row: dict[str, Any]) -> str:
    fields = [
        str(row.get("id") or row.get("review_id") or ""),
        str(row.get("author") or row.get("author_name") or row.get("author_hash") or ""),
        str(review_date(row) or ""),
        str(row.get("rating") or row.get("score") or ""),
        str(row.get("version") or row.get("app_version") or ""),
        str(row.get("title") or ""),
        str(review_content(row) or ""),
    ]
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def probe_apptweak_reviews(
    targets: list[AppTarget],
    output_path: Path,
    *,
    api_token: str,
    limit: int = 5,
    country_fallback: str = "us",
    language: str = "us",
    device: str = "iphone",
    start_date: str | None = None,
    end_date: str | None = None,
    term: str | None = None,
    page_limit: int = 2,
    request_limit: int = 500,
    timeout_seconds: float = 20.0,
    request_delay_seconds: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    selected = targets[:limit] if limit > 0 else targets
    owned_session = session is None
    http = session or requests.Session()
    rows: list[dict[str, Any]] = []
    try:
        for target_index, target in enumerate(selected):
            if target_index and request_delay_seconds:
                sleep_fn(request_delay_seconds)
            countries = target.countries or (country_fallback,)
            for country_index, country in enumerate(countries):
                if country_index and request_delay_seconds:
                    sleep_fn(request_delay_seconds)
                rows.append(
                    probe_apptweak_reviews_for_scope(
                        target,
                        country.lower(),
                        session=http,
                        api_token=api_token,
                        language=language,
                        device=device,
                        start_date=start_date,
                        end_date=end_date,
                        term=term,
                        page_limit=page_limit,
                        request_limit=request_limit,
                        timeout_seconds=timeout_seconds,
                        request_delay_seconds=request_delay_seconds,
                        sleep_fn=sleep_fn,
                    )
                )
    finally:
        if owned_session:
            http.close()

    report = {
        "generated_at": utc_timestamp(),
        "source": "provider_apptweak_reviews_search_api",
        "target_count": len(selected),
        "scope_count": len(rows),
        "settings": {
            "language": language,
            "device": device,
            "start_date": start_date,
            "end_date": end_date,
            "term": term,
            "page_limit": page_limit,
            "request_limit": request_limit,
            "timeout_seconds": timeout_seconds,
            "request_delay_seconds": request_delay_seconds,
        },
        "summary": summarize_apptweak_probe(rows),
        "results": rows,
    }
    write_json(output_path, report)
    return report


def probe_apptweak_reviews_for_scope(
    target: AppTarget,
    country: str,
    *,
    session: requests.Session,
    api_token: str,
    language: str,
    device: str,
    start_date: str | None,
    end_date: str | None,
    term: str | None,
    page_limit: int,
    request_limit: int,
    timeout_seconds: float,
    request_delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    page_reports: list[dict[str, Any]] = []
    headers = apptweak_headers(api_token)
    for page_number in range(1, page_limit + 1):
        if page_number > 1 and request_delay_seconds:
            sleep_fn(request_delay_seconds)
        offset = (page_number - 1) * request_limit
        url = build_apptweak_reviews_url(
            target.apple_app_id,
            country=country,
            language=language,
            device=device,
            limit=request_limit,
            offset=offset,
            start_date=start_date,
            end_date=end_date,
            term=term,
        )
        response = session.get(url, headers=headers, timeout=timeout_seconds)
        summary = {
            "review_count": 0,
            "total_reviews": None,
            "missing_content_count": 0,
            "min_date": None,
            "max_date": None,
            "review_fingerprints": [],
        }
        error = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                summary = parse_apptweak_reviews_payload(payload)
            else:
                error = "response JSON was not an object"
        except ValueError as exc:
            error = str(exc)
        page_reports.append(
            {
                "page": page_number,
                "offset": offset,
                "request_url": url,
                "status_code": response.status_code,
                "response_bytes": len(response.content or b""),
                "content_type": response.headers.get("content-type"),
                "summary": summary,
                "error": error,
            }
        )
        if response.status_code != 200:
            break
        if int(summary.get("review_count") or 0) < request_limit:
            break
    return {
        "app_id": target.apple_app_id,
        "app_name": target.app_name,
        "category": target.category,
        "country": country,
        "pages": page_reports,
        "status_counts": status_counts(page_reports),
        "review_count": sum(int(page["summary"].get("review_count") or 0) for page in page_reports),
        "total_reviews": first_int_summary_value(page_reports, "total_reviews"),
        "min_date": min(
            [page["summary"].get("min_date") for page in page_reports if page["summary"].get("min_date")],
            default=None,
        ),
        "max_date": max(
            [page["summary"].get("max_date") for page in page_reports if page["summary"].get("max_date")],
            default=None,
        ),
    }


def summarize_apptweak_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    page_count = sum(len(row.get("pages") or []) for row in rows)
    status: dict[str, int] = {}
    for row in rows:
        for key, value in (row.get("status_counts") or {}).items():
            status[key] = status.get(key, 0) + int(value)
    ok_pages = int(status.get("200") or 0)
    return {
        "scope_count": len(rows),
        "page_count": page_count,
        "status_counts": status,
        "page_success_rate": ok_pages / page_count if page_count else None,
        "reviews_seen": sum(int(row.get("review_count") or 0) for row in rows),
        "scopes_with_reviews": sum(1 for row in rows if int(row.get("review_count") or 0) > 0),
    }


def status_counts(page_reports: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in page_reports:
        status = str(page.get("status_code") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def first_int_summary_value(page_reports: list[dict[str, Any]], key: str) -> int | None:
    for page in page_reports:
        value = page["summary"].get(key)
        parsed = parse_int(value)
        if parsed is not None:
            return parsed
    return None


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
