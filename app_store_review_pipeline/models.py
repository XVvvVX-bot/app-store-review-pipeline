from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from app_store_review_pipeline.config import PLATFORM, SOURCE


@dataclass(frozen=True)
class AppTarget:
    app_name: str
    category: str
    apple_app_id: str
    apple_slug: str
    countries: tuple[str, ...]
    active: bool
    notes: str | None

    @property
    def apple_app_store_url(self) -> str:
        slug = quote(self.apple_slug, safe="-")
        return f"https://apps.apple.com/us/app/{slug}/id{self.apple_app_id}"


@dataclass(frozen=True)
class AppReview:
    review_key: str
    platform: str
    source: str
    app_id: str
    app_name: str
    country: str
    review_id: str
    author_name: str | None
    updated_at: str | None
    updated_epoch_seconds: int | None
    rating: int | None
    title: str | None
    content: str | None
    page_number: int
    source_page_key: str
    collected_at: str


@dataclass(frozen=True)
class ReviewPage:
    page_key: str
    run_id: str
    platform: str
    source: str
    app_id: str
    app_name: str
    country: str
    sort_by: str
    page_number: int
    request_url: str
    status: str
    status_code: int | None
    fetched_at: str
    raw_json_path: str | None
    response_bytes: int
    review_count: int
    unique_review_count: int
    duplicate_count: int
    missing_text_count: int
    missing_rating_count: int
    missing_updated_count: int
    max_updated_epoch_seconds: int | None
    min_updated_epoch_seconds: int | None
    has_next_link: bool
    attempt_count: int
    error_message: str | None
    terminal_reason: str | None
    overlap_review_count: int


def make_review_key(app_id: str, country: str, review_id: str, *, source: str = SOURCE) -> str:
    return f"{PLATFORM}:{source}:{country.lower()}:{app_id}:{review_id}"


def make_page_key(run_id: str, app_id: str, country: str, sort_by: str, page_number: int) -> str:
    return f"{run_id}:{app_id}:{country.lower()}:{sort_by}:{page_number}"
