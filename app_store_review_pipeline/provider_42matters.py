from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.models import AppTarget
from app_store_review_pipeline.utils import utc_timestamp


REVIEWS_URL = "https://data.42matters.com/api/v5.0/ios/apps/reviews.json"


def build_42matters_reviews_url(
    app_id: str,
    *,
    access_token: str,
    days: int | None = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    lang: str | None = None,
    rating: int | None = None,
    limit: int = 100,
    page: int = 1,
) -> str:
    params: dict[str, str] = {
        "id": app_id,
        "access_token": access_token,
        "limit": str(limit),
        "page": str(page),
    }
    if days is not None:
        params["days"] = str(days)
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if lang:
        params["lang"] = lang
    if rating is not None:
        params["rating"] = str(rating)
    return f"{REVIEWS_URL}?{urlencode(params)}"


def redact_access_token(url: str) -> str:
    parts = urlsplit(url)
    params = [
        (key, "***" if key == "access_token" else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def parse_42matters_reviews_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        reviews = []
    dates = [review.get("date") for review in reviews if isinstance(review, dict) and review.get("date")]
    return {
        "number_reviews": parse_int(payload.get("number_reviews")),
        "total_reviews": parse_int(payload.get("total_reviews")),
        "number_reviews_remaining": parse_int(payload.get("number_reviews_remaining")),
        "page": parse_int(payload.get("page")),
        "limit": parse_int(payload.get("limit")),
        "total_pages": parse_int(payload.get("total_pages")),
        "review_count": len(reviews),
        "missing_content_count": sum(
            1 for review in reviews if isinstance(review, dict) and not review.get("content")
        ),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "review_fingerprints": [
            review_fingerprint(review)
            for review in reviews
            if isinstance(review, dict)
        ],
    }


def review_fingerprint(review: dict[str, Any]) -> str:
    fields = [
        str(review.get("author_hash") or ""),
        str(review.get("date") or ""),
        str(review.get("rating") or ""),
        str(review.get("app_version") or ""),
        str(review.get("title") or ""),
        str(review.get("content") or ""),
    ]
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def probe_42matters_reviews(
    targets: list[AppTarget],
    output_path: Path,
    *,
    access_token: str,
    limit: int = 5,
    days: int | None = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    lang: str | None = None,
    rating: int | None = None,
    page_limit: int = 2,
    request_limit: int = 100,
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
            rows.append(
                probe_42matters_reviews_for_target(
                    target,
                    session=http,
                    access_token=access_token,
                    days=days,
                    start_date=start_date,
                    end_date=end_date,
                    lang=lang,
                    rating=rating,
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
        "source": "provider_42matters_ios_reviews_api",
        "target_count": len(selected),
        "settings": {
            "days": days,
            "start_date": start_date,
            "end_date": end_date,
            "lang": lang,
            "rating": rating,
            "page_limit": page_limit,
            "request_limit": request_limit,
            "timeout_seconds": timeout_seconds,
            "request_delay_seconds": request_delay_seconds,
        },
        "summary": summarize_42matters_probe(rows),
        "results": rows,
    }
    write_json(output_path, report)
    return report


def probe_42matters_reviews_for_target(
    target: AppTarget,
    *,
    session: requests.Session,
    access_token: str,
    days: int | None,
    start_date: str | None,
    end_date: str | None,
    lang: str | None,
    rating: int | None,
    page_limit: int,
    request_limit: int,
    timeout_seconds: float,
    request_delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    page_reports: list[dict[str, Any]] = []
    for page_number in range(1, page_limit + 1):
        if page_number > 1 and request_delay_seconds:
            sleep_fn(request_delay_seconds)
        url = build_42matters_reviews_url(
            target.apple_app_id,
            access_token=access_token,
            days=days,
            start_date=start_date,
            end_date=end_date,
            lang=lang,
            rating=rating,
            limit=request_limit,
            page=page_number,
        )
        response = session.get(url, timeout=timeout_seconds)
        summary = {
            "number_reviews": None,
            "total_reviews": None,
            "number_reviews_remaining": None,
            "page": page_number,
            "limit": request_limit,
            "total_pages": None,
            "review_count": 0,
            "missing_content_count": 0,
            "min_date": None,
            "max_date": None,
            "review_fingerprints": [],
        }
        error = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                summary = parse_42matters_reviews_payload(payload)
            else:
                error = "response JSON was not an object"
        except ValueError as exc:
            error = str(exc)
        page_reports.append(
            {
                "page": page_number,
                "request_url": redact_access_token(url),
                "status_code": response.status_code,
                "response_bytes": len(response.content or b""),
                "content_type": response.headers.get("content-type"),
                "summary": summary,
                "error": error,
            }
        )
        if response.status_code != 200:
            break
        total_pages = summary.get("total_pages")
        if total_pages is not None and page_number >= int(total_pages):
            break
        if int(summary.get("review_count") or 0) == 0:
            break
    return {
        "app_id": target.apple_app_id,
        "app_name": target.app_name,
        "category": target.category,
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


def summarize_42matters_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    page_count = sum(len(row.get("pages") or []) for row in rows)
    status: dict[str, int] = {}
    for row in rows:
        for key, value in (row.get("status_counts") or {}).items():
            status[key] = status.get(key, 0) + int(value)
    ok_pages = int(status.get("200") or 0)
    return {
        "app_count": len(rows),
        "page_count": page_count,
        "status_counts": status,
        "page_success_rate": ok_pages / page_count if page_count else None,
        "reviews_seen": sum(int(row.get("review_count") or 0) for row in rows),
        "apps_with_reviews": sum(1 for row in rows if int(row.get("review_count") or 0) > 0),
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
