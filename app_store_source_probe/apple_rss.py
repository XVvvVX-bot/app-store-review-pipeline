from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from app_store_source_probe.probe import USER_AGENT
from app_store_source_probe.targets import AppTarget, active_targets, load_targets


@dataclass(frozen=True)
class AppleReview:
    review_id: str
    app_name: str
    app_id: str
    country: str
    page: int
    author_name: str | None
    updated_at: str | None
    rating: int | None
    version: str | None
    title: str | None
    content: str | None
    vote_sum: int | None
    vote_count: int | None


@dataclass(frozen=True)
class AppleRssPageResult:
    app_name: str
    app_id: str
    country: str
    page: int
    url: str
    status_code: int | None
    ok: bool
    response_bytes: int
    fetched_at: str
    review_count: int
    unique_review_count: int
    duplicate_count: int
    missing_text_count: int
    missing_rating_count: int
    missing_updated_count: int
    has_next_link: bool
    access_error: str | None


def apple_rss_url(app_id: str, *, country: str, page: int, sort_by: str = "mostrecent") -> str:
    return f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby={sort_by}/json"


def run_apple_rss_probe(
    targets_path: Path,
    output_path: Path | None,
    *,
    limit: int | None = None,
    country: str = "us",
    max_pages: int = 10,
    timeout_seconds: float = 20,
    delay_seconds: float = 1,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    targets = active_targets(load_targets(targets_path))
    if limit is not None:
        targets = targets[:limit]

    owned_session = session is None
    http = session or requests.Session()
    pages: list[AppleRssPageResult] = []
    reviews: list[AppleReview] = []
    try:
        for target_index, target in enumerate(targets):
            if target_index and delay_seconds:
                time.sleep(delay_seconds)
            for page in range(1, max_pages + 1):
                if page > 1 and delay_seconds:
                    time.sleep(delay_seconds)
                page_result, page_reviews = fetch_apple_rss_page(
                    target,
                    country=country,
                    page=page,
                    session=http,
                    timeout_seconds=timeout_seconds,
                )
                pages.append(page_result)
                reviews.extend(page_reviews)
                if not page_result.ok or page_result.review_count == 0:
                    break
    finally:
        if owned_session:
            http.close()

    report = build_apple_rss_report(targets_path, pages, reviews)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def fetch_apple_rss_page(
    target: AppTarget,
    *,
    country: str,
    page: int,
    session: requests.Session,
    timeout_seconds: float,
) -> tuple[AppleRssPageResult, list[AppleReview]]:
    url = apple_rss_url(target.apple_app_id, country=country, page=page)
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        response = session.get(
            url,
            headers={
                "Accept": "application/json,text/javascript",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        return (
            AppleRssPageResult(
                app_name=target.app_name,
                app_id=target.apple_app_id,
                country=country,
                page=page,
                url=url,
                status_code=None,
                ok=False,
                response_bytes=0,
                fetched_at=fetched_at,
                review_count=0,
                unique_review_count=0,
                duplicate_count=0,
                missing_text_count=0,
                missing_rating_count=0,
                missing_updated_count=0,
                has_next_link=False,
                access_error=str(exc),
            ),
            [],
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return (
            AppleRssPageResult(
                app_name=target.app_name,
                app_id=target.apple_app_id,
                country=country,
                page=page,
                url=url,
                status_code=response.status_code,
                ok=False,
                response_bytes=len(response.content or b""),
                fetched_at=fetched_at,
                review_count=0,
                unique_review_count=0,
                duplicate_count=0,
                missing_text_count=0,
                missing_rating_count=0,
                missing_updated_count=0,
                has_next_link=False,
                access_error=f"invalid json: {exc}",
            ),
            [],
        )

    entries = normalize_entries(payload.get("feed", {}).get("entry", []))
    reviews = [parse_apple_review(entry, target, country=country, page=page) for entry in entries]
    review_ids = [review.review_id for review in reviews if review.review_id]
    unique_review_ids = set(review_ids)
    duplicate_count = len(review_ids) - len(unique_review_ids)
    links = normalize_entries(payload.get("feed", {}).get("link", []))
    has_next_link = any(item.get("attributes", {}).get("rel") == "next" and item.get("attributes", {}).get("href") for item in links)

    result = AppleRssPageResult(
        app_name=target.app_name,
        app_id=target.apple_app_id,
        country=country,
        page=page,
        url=url,
        status_code=response.status_code,
        ok=200 <= response.status_code < 300,
        response_bytes=len(response.content or b""),
        fetched_at=fetched_at,
        review_count=len(reviews),
        unique_review_count=len(unique_review_ids),
        duplicate_count=duplicate_count,
        missing_text_count=sum(1 for review in reviews if not review.content),
        missing_rating_count=sum(1 for review in reviews if review.rating is None),
        missing_updated_count=sum(1 for review in reviews if not review.updated_at),
        has_next_link=has_next_link,
        access_error=None,
    )
    return result, reviews


def normalize_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def parse_apple_review(entry: dict[str, Any], target: AppTarget, *, country: str, page: int) -> AppleReview:
    return AppleReview(
        review_id=field_label(entry, "id") or "",
        app_name=target.app_name,
        app_id=target.apple_app_id,
        country=country,
        page=page,
        author_name=field_label(entry.get("author", {}), "name"),
        updated_at=field_label(entry, "updated"),
        rating=parse_int(field_label(entry, "im:rating")),
        version=field_label(entry, "im:version"),
        title=field_label(entry, "title"),
        content=field_label(entry, "content"),
        vote_sum=parse_int(field_label(entry, "im:voteSum")),
        vote_count=parse_int(field_label(entry, "im:voteCount")),
    )


def field_label(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, dict):
        label = value.get("label")
        if isinstance(label, str):
            return label
    return None


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def build_apple_rss_report(
    targets_path: Path,
    pages: list[AppleRssPageResult],
    reviews: list[AppleReview],
) -> dict[str, Any]:
    all_ids = [review.review_id for review in reviews if review.review_id]
    unique_ids = set(all_ids)
    app_summaries: dict[str, dict[str, Any]] = {}
    for page in pages:
        summary = app_summaries.setdefault(
            page.app_id,
            {
                "app_name": page.app_name,
                "country": page.country,
                "pages_requested": 0,
                "ok_pages": 0,
                "review_rows": 0,
                "unique_review_rows": 0,
                "missing_text": 0,
                "missing_rating": 0,
                "missing_updated": 0,
                "stopped_at_page": page.page,
            },
        )
        summary["pages_requested"] += 1
        summary["ok_pages"] += int(page.ok)
        summary["review_rows"] += page.review_count
        summary["missing_text"] += page.missing_text_count
        summary["missing_rating"] += page.missing_rating_count
        summary["missing_updated"] += page.missing_updated_count
        summary["stopped_at_page"] = max(summary["stopped_at_page"], page.page)

    for app_id, summary in app_summaries.items():
        ids = {review.review_id for review in reviews if review.app_id == app_id and review.review_id}
        summary["unique_review_rows"] = len(ids)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets_path": str(targets_path),
        "source": "apple_itunes_customerreviews_rss",
        "ethical_boundary": (
            "Uses the public iTunes customerreviews RSS JSON feed only; no login, cookies, "
            "CAPTCHA solving, proxy rotation, hidden endpoints, or App Store Connect credentials."
        ),
        "interpretation": (
            "Apple RSS can expose structured public review rows, but feasibility still depends on "
            "depth limits, country coverage, rate limits, terms, and whether 500-ish recent reviews "
            "per country is enough for the product goal."
        ),
        "summary": {
            "pages_requested": len(pages),
            "ok_pages": sum(1 for page in pages if page.ok),
            "review_rows": len(reviews),
            "unique_review_rows": len(unique_ids),
            "duplicate_review_rows": len(all_ids) - len(unique_ids),
            "missing_text": sum(1 for review in reviews if not review.content),
            "missing_rating": sum(1 for review in reviews if review.rating is None),
            "missing_updated": sum(1 for review in reviews if not review.updated_at),
            "apps": list(app_summaries.values()),
        },
        "pages": [asdict(page) for page in pages],
        "sample_reviews": [asdict(review) for review in reviews[:10]],
    }
