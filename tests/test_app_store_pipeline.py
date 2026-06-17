import csv
from pathlib import Path

import pytest

from app_store_review_pipeline.apple_rss import apple_rss_url, normalize_entries, parse_apple_review
from app_store_review_pipeline.fetcher import terminal_reason_for_page
from app_store_review_pipeline.models import ReviewPage
from app_store_review_pipeline.postgres_database import mask_database_url, scope_key
from app_store_review_pipeline.targets import active_targets, load_targets, parse_countries


def write_targets(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "app_name",
                "category",
                "apple_app_id",
                "apple_slug",
                "countries",
                "active",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "app_name": "ChatGPT",
                "category": "ai_tools",
                "apple_app_id": "6448311069",
                "apple_slug": "chatgpt",
                "countries": "us|ca",
                "active": "true",
                "notes": "fixture",
            }
        )
        writer.writerow(
            {
                "app_name": "Inactive",
                "category": "test",
                "apple_app_id": "123456789",
                "apple_slug": "inactive",
                "countries": "us",
                "active": "false",
                "notes": "",
            }
        )


def page(status="ok", review_count=50):
    return ReviewPage(
        page_key="run:app:us:mostrecent:1",
        run_id="run",
        platform="apple_app_store",
        source="apple_itunes_customerreviews_rss",
        app_id="123",
        app_name="Fixture",
        country="us",
        sort_by="mostrecent",
        page_number=1,
        request_url="https://example.test",
        status=status,
        status_code=200 if status == "ok" else 500,
        fetched_at="2026-06-17T00:00:00+00:00",
        raw_json_path=None,
        response_bytes=10,
        review_count=review_count,
        unique_review_count=review_count,
        duplicate_count=0,
        missing_text_count=0,
        missing_rating_count=0,
        missing_updated_count=0,
        max_updated_epoch_seconds=100,
        min_updated_epoch_seconds=50,
        has_next_link=False,
        attempt_count=1,
        error_message=None,
        terminal_reason=None,
        overlap_review_count=0,
    )


def test_load_targets_and_url(tmp_path):
    path = tmp_path / "targets.csv"
    write_targets(path)

    targets = load_targets(path)
    active = active_targets(targets)

    assert len(targets) == 2
    assert len(active) == 1
    assert active[0].countries == ("us", "ca")
    assert active[0].apple_app_store_url == "https://apps.apple.com/us/app/chatgpt/id6448311069"


def test_load_targets_rejects_bad_apple_id(tmp_path):
    path = tmp_path / "targets.csv"
    write_targets(path)
    path.write_text(path.read_text(encoding="utf-8").replace("6448311069", "bad-id"), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid apple_app_id"):
        load_targets(path)


def test_parse_countries_defaults_and_splits():
    assert parse_countries("") == ("us",)
    assert parse_countries("us, ca|AU") == ("us", "ca", "au")


def test_apple_rss_url_and_entry_parsing(tmp_path):
    path = tmp_path / "targets.csv"
    write_targets(path)
    target = load_targets(path)[0]
    entry = {
        "author": {"name": {"label": "Reviewer"}},
        "updated": {"label": "2026-06-17T01:02:03-07:00"},
        "im:rating": {"label": "4"},
        "im:version": {"label": "1.2.3"},
        "id": {"label": "12345"},
        "title": {"label": "Good app"},
        "content": {"label": "Useful review text"},
        "im:voteSum": {"label": "3"},
        "im:voteCount": {"label": "5"},
    }

    review = parse_apple_review(
        entry,
        target,
        country="us",
        page_number=1,
        page_key="page-key",
        collected_at="2026-06-17T00:00:00+00:00",
    )

    assert apple_rss_url("6448311069", country="us", page=2).endswith("/page=2/id=6448311069/sortby=mostrecent/json")
    assert review.review_id == "12345"
    assert review.review_key.endswith(":us:6448311069:12345")
    assert review.rating == 4
    assert review.version == "1.2.3"
    assert review.content == "Useful review text"
    assert review.vote_count == 5
    assert review.updated_epoch_seconds is not None


def test_normalize_entries_accepts_single_or_list():
    assert normalize_entries({"a": 1}) == [{"a": 1}]
    assert normalize_entries([{"a": 1}, "bad"]) == [{"a": 1}]
    assert normalize_entries(None) == []


def test_terminal_reason_for_page():
    assert terminal_reason_for_page(page(status="error"), page_number=1, max_pages_per_app_country=10, overlap_count=0, use_overlap_stop=True) == "fetch_error"
    assert terminal_reason_for_page(page(review_count=0), page_number=1, max_pages_per_app_country=10, overlap_count=0, use_overlap_stop=True) == "empty_page"
    assert terminal_reason_for_page(page(), page_number=1, max_pages_per_app_country=10, overlap_count=1, use_overlap_stop=True) == "caught_up_to_existing_reviews"
    assert terminal_reason_for_page(page(), page_number=10, max_pages_per_app_country=10, overlap_count=0, use_overlap_stop=True) == "page_cap"


def test_database_helpers():
    assert mask_database_url("postgresql://user:secret@localhost:5432/app_store_reviews") == "postgresql://user:***@localhost:5432/app_store_reviews"
    assert scope_key("123", "US") == "123:us:mostrecent"
