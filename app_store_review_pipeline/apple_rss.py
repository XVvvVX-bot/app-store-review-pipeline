from __future__ import annotations

from typing import Any

from app_store_review_pipeline.config import DEFAULT_SORT_BY, PLATFORM, SOURCE
from app_store_review_pipeline.models import AppReview, AppTarget, make_review_key
from app_store_review_pipeline.utils import clean_text, iso_to_epoch_seconds


def apple_rss_url(app_id: str, *, country: str, page: int, sort_by: str = DEFAULT_SORT_BY) -> str:
    return f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby={sort_by}/json"


def normalize_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        return [value]
    return []


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


def payload_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    feed = payload.get("feed")
    if not isinstance(feed, dict):
        return []
    return normalize_entries(feed.get("entry", []))


def payload_has_next_link(payload: dict[str, Any]) -> bool:
    feed = payload.get("feed")
    if not isinstance(feed, dict):
        return False
    links = normalize_entries(feed.get("link", []))
    return any(item.get("attributes", {}).get("rel") == "next" and item.get("attributes", {}).get("href") for item in links)


def parse_apple_review(
    entry: dict[str, Any],
    target: AppTarget,
    *,
    country: str,
    page_number: int,
    page_key: str,
    collected_at: str,
) -> AppReview:
    review_id = field_label(entry, "id") or ""
    updated_at = field_label(entry, "updated")
    return AppReview(
        review_key=make_review_key(target.apple_app_id, country, review_id),
        platform=PLATFORM,
        source=SOURCE,
        app_id=target.apple_app_id,
        app_name=target.app_name,
        country=country.lower(),
        review_id=review_id,
        author_name=clean_text(field_label(entry.get("author", {}), "name")),
        updated_at=updated_at,
        updated_epoch_seconds=iso_to_epoch_seconds(updated_at),
        rating=parse_int(field_label(entry, "im:rating")),
        version=clean_text(field_label(entry, "im:version")),
        title=clean_text(field_label(entry, "title")),
        content=clean_text(field_label(entry, "content")),
        vote_sum=parse_int(field_label(entry, "im:voteSum")),
        vote_count=parse_int(field_label(entry, "im:voteCount")),
        page_number=page_number,
        source_page_key=page_key,
        collected_at=collected_at,
    )
