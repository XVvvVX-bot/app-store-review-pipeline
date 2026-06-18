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


API_ROOT = "https://api.appfigures.com/v2"


def appfigures_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "ScienciaAI-AppStoreReviewPipeline/0.1",
    }


def build_appfigures_product_lookup_url(app_id: str) -> str:
    return f"{API_ROOT}/products/apple/{app_id}"


def build_appfigures_reviews_url(
    product_id: str | int,
    *,
    country: str | None = "us",
    page: int = 1,
    count: int = 500,
    sort: str = "-date",
    start_date: str | None = None,
    end_date: str | None = None,
    lang: str | None = None,
    stars: str | None = None,
) -> str:
    params = {
        "products": str(product_id),
        "page": str(page),
        "count": str(count),
        "sort": sort,
    }
    if country:
        params["countries"] = country.lower()
    if start_date:
        params["start"] = start_date
    if end_date:
        params["end"] = end_date
    if lang:
        params["lang"] = lang
    if stars:
        params["stars"] = stars
    return f"{API_ROOT}/reviews?{urlencode(params)}"


def parse_appfigures_product_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": parse_int(payload.get("id")),
        "name": payload.get("name"),
        "developer": payload.get("developer"),
        "ref_no": parse_int(payload.get("ref_no")),
        "store": payload.get("store"),
        "vendor_identifier": payload.get("vendor_identifier"),
    }


def parse_appfigures_reviews_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("reviews")
    if not isinstance(rows, list):
        rows = []
    dates = [row.get("date") for row in rows if isinstance(row, dict) and row.get("date")]
    return {
        "total_reviews": parse_int(payload.get("total")),
        "total_pages": parse_int(payload.get("pages")),
        "page": parse_int(payload.get("this_page")),
        "review_count": len(rows),
        "missing_content_count": sum(1 for row in rows if isinstance(row, dict) and not row.get("review")),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "review_fingerprints": [
            review_fingerprint(row)
            for row in rows
            if isinstance(row, dict)
        ],
    }


def review_fingerprint(row: dict[str, Any]) -> str:
    fields = [
        str(row.get("id") or ""),
        str(row.get("author") or ""),
        str(row.get("date") or ""),
        str(row.get("stars") or ""),
        str(row.get("version") or ""),
        str(row.get("title") or ""),
        str(row.get("review") or ""),
        str(row.get("iso") or ""),
    ]
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def probe_appfigures_reviews(
    targets: list[AppTarget],
    output_path: Path,
    *,
    access_token: str,
    limit: int = 5,
    country_fallback: str = "us",
    page_limit: int = 2,
    request_limit: int = 500,
    sort: str = "-date",
    start_date: str | None = None,
    end_date: str | None = None,
    lang: str | None = None,
    stars: str | None = None,
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
                    probe_appfigures_reviews_for_scope(
                        target,
                        country.lower(),
                        session=http,
                        access_token=access_token,
                        page_limit=page_limit,
                        request_limit=request_limit,
                        sort=sort,
                        start_date=start_date,
                        end_date=end_date,
                        lang=lang,
                        stars=stars,
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
        "source": "provider_appfigures_public_data_reviews_api",
        "target_count": len(selected),
        "scope_count": len(rows),
        "settings": {
            "page_limit": page_limit,
            "request_limit": request_limit,
            "sort": sort,
            "start_date": start_date,
            "end_date": end_date,
            "lang": lang,
            "stars": stars,
            "timeout_seconds": timeout_seconds,
            "request_delay_seconds": request_delay_seconds,
        },
        "summary": summarize_appfigures_probe(rows),
        "results": rows,
    }
    write_json(output_path, report)
    return report


def probe_appfigures_reviews_for_scope(
    target: AppTarget,
    country: str,
    *,
    session: requests.Session,
    access_token: str,
    page_limit: int,
    request_limit: int,
    sort: str,
    start_date: str | None,
    end_date: str | None,
    lang: str | None,
    stars: str | None,
    timeout_seconds: float,
    request_delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    headers = appfigures_headers(access_token)
    lookup_url = build_appfigures_product_lookup_url(target.apple_app_id)
    lookup_response = session.get(lookup_url, headers=headers, timeout=timeout_seconds)
    product_summary = {
        "product_id": None,
        "name": None,
        "developer": None,
        "ref_no": None,
        "store": None,
        "vendor_identifier": None,
    }
    lookup_error = None
    try:
        payload = lookup_response.json()
        if isinstance(payload, dict):
            product_summary = parse_appfigures_product_payload(payload)
        else:
            lookup_error = "product lookup JSON was not an object"
    except ValueError as exc:
        lookup_error = str(exc)

    page_reports: list[dict[str, Any]] = []
    product_id = product_summary.get("product_id")
    if lookup_response.status_code == 200 and product_id is not None:
        for page_number in range(1, page_limit + 1):
            if page_number > 1 and request_delay_seconds:
                sleep_fn(request_delay_seconds)
            url = build_appfigures_reviews_url(
                product_id,
                country=country,
                page=page_number,
                count=request_limit,
                sort=sort,
                start_date=start_date,
                end_date=end_date,
                lang=lang,
                stars=stars,
            )
            response = session.get(url, headers=headers, timeout=timeout_seconds)
            summary = {
                "total_reviews": None,
                "total_pages": None,
                "page": page_number,
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
                    summary = parse_appfigures_reviews_payload(payload)
                else:
                    error = "reviews JSON was not an object"
            except ValueError as exc:
                error = str(exc)
            page_reports.append(
                {
                    "page": page_number,
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
            total_pages = summary.get("total_pages")
            if total_pages is not None and page_number >= int(total_pages):
                break
            if int(summary.get("review_count") or 0) < request_limit:
                break

    return {
        "app_id": target.apple_app_id,
        "app_name": target.app_name,
        "category": target.category,
        "country": country,
        "product_lookup": {
            "request_url": lookup_url,
            "status_code": lookup_response.status_code,
            "response_bytes": len(lookup_response.content or b""),
            "content_type": lookup_response.headers.get("content-type"),
            "summary": product_summary,
            "error": lookup_error,
        },
        "provider_product_id": product_id,
        "pages": page_reports,
        "status_counts": status_counts(page_reports, lookup_status=lookup_response.status_code),
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


def summarize_appfigures_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    page_count = sum(len(row.get("pages") or []) for row in rows)
    status: dict[str, int] = {}
    lookup_status: dict[str, int] = {}
    for row in rows:
        lookup = row.get("product_lookup") or {}
        lookup_key = str(lookup.get("status_code") or "unknown")
        lookup_status[lookup_key] = lookup_status.get(lookup_key, 0) + 1
        for key, value in (row.get("status_counts") or {}).items():
            if key.startswith("lookup_"):
                continue
            status[key] = status.get(key, 0) + int(value)
    ok_pages = int(status.get("200") or 0)
    return {
        "scope_count": len(rows),
        "lookup_status_counts": lookup_status,
        "lookup_success_rate": (int(lookup_status.get("200") or 0) / len(rows)) if rows else None,
        "page_count": page_count,
        "status_counts": status,
        "page_success_rate": ok_pages / page_count if page_count else None,
        "reviews_seen": sum(int(row.get("review_count") or 0) for row in rows),
        "scopes_with_reviews": sum(1 for row in rows if int(row.get("review_count") or 0) > 0),
    }


def status_counts(page_reports: list[dict[str, Any]], *, lookup_status: int | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if lookup_status is not None:
        counts[f"lookup_{lookup_status}"] = 1
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
