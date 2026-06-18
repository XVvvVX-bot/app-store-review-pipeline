import csv
from pathlib import Path

import pytest

from app_store_review_pipeline.apple_rss import apple_rss_url, normalize_entries, parse_apple_review
from app_store_review_pipeline.apple_web import (
    app_store_web_catalog_next_url,
    app_store_web_catalog_url,
    app_store_web_reviews_url,
    parse_html_review_ids,
    parse_json_ld_aggregate_rating,
    parse_serialized_next_href,
    parse_web_catalog_review_page,
    parse_web_catalog_reviews,
)
from app_store_review_pipeline.fetcher import fetch_targets, terminal_reason_for_page
from app_store_review_pipeline.models import AppTarget, ReviewPage
from app_store_review_pipeline.postgres_database import mask_database_url, scope_key
from app_store_review_pipeline.provider_42matters import (
    build_42matters_reviews_url,
    parse_42matters_reviews_payload,
    redact_access_token,
)
from app_store_review_pipeline.source_compare import compare_per_scope, summarize_comparison
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


def page(status="ok", review_count=50, has_next_link=False):
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
        has_next_link=has_next_link,
        attempt_count=1,
        error_message=None,
        terminal_reason=None,
        overlap_review_count=0,
    )


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.content = str(payload).encode("utf-8")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)

    def get(self, *args, **kwargs):
        if not self.payloads:
            raise AssertionError("No fake response payloads remaining")
        return FakeResponse(self.payloads.pop(0))

    def close(self):
        pass


def empty_payload(has_next=True):
    links = [{"attributes": {"rel": "next", "href": "https://example.test/next"}}] if has_next else []
    return {"feed": {"title": {"label": "iTunes Store: Customer Reviews"}, "link": links}}


def review_payload(review_id="review-1", has_next=True):
    links = [{"attributes": {"rel": "next", "href": "https://example.test/next"}}] if has_next else []
    return {
        "feed": {
            "title": {"label": "iTunes Store: Customer Reviews"},
            "link": links,
            "entry": {
                "author": {"name": {"label": "Reviewer"}},
                "updated": {"label": "2026-06-17T01:02:03-07:00"},
                "im:rating": {"label": "5"},
                "id": {"label": review_id},
                "title": {"label": "Useful"},
                "content": {"label": "Useful review text"},
            },
        }
    }


def fixture_target():
    return AppTarget(
        app_name="Fixture",
        category="test",
        apple_app_id="123456789",
        apple_slug="fixture",
        countries=("us",),
        active=True,
        notes=None,
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
    assert (
        terminal_reason_for_page(
            page(status="error"),
            page_number=1,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=0,
            known_review_count=0,
            consecutive_empty_pages=0,
            use_overlap_stop=True,
        )
        == "fetch_error"
    )
    assert (
        terminal_reason_for_page(
            page(review_count=0),
            page_number=1,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=0,
            known_review_count=0,
            consecutive_empty_pages=1,
            use_overlap_stop=True,
        )
        == "empty_page"
    )
    assert (
        terminal_reason_for_page(
            page(review_count=0),
            page_number=1,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=0,
            known_review_count=100,
            consecutive_empty_pages=1,
            use_overlap_stop=True,
        )
        == "empty_page_before_overlap"
    )
    assert (
        terminal_reason_for_page(
            page(review_count=0, has_next_link=True),
            page_number=1,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=0,
            known_review_count=100,
            consecutive_empty_pages=1,
            use_overlap_stop=True,
        )
        is None
    )
    assert (
        terminal_reason_for_page(
            page(review_count=0, has_next_link=True),
            page_number=3,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=3,
            overlap_count=0,
            known_review_count=0,
            consecutive_empty_pages=3,
            use_overlap_stop=True,
        )
        == "empty_page_after_sparse_scan"
    )
    assert (
        terminal_reason_for_page(
            page(),
            page_number=1,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=1,
            known_review_count=100,
            consecutive_empty_pages=0,
            use_overlap_stop=True,
        )
        == "caught_up_to_existing_reviews"
    )
    assert (
        terminal_reason_for_page(
            page(),
            page_number=10,
            max_pages_per_app_country=10,
            max_consecutive_empty_pages=10,
            overlap_count=0,
            known_review_count=100,
            consecutive_empty_pages=0,
            use_overlap_stop=True,
        )
        == "page_cap"
    )


def test_fetch_targets_continues_across_sparse_empty_pages(tmp_path):
    report = fetch_targets(
        [fixture_target()],
        tmp_path / "raw",
        "run",
        max_pages_per_app_country=3,
        max_consecutive_empty_pages=10,
        request_delay_seconds=0,
        retry_delay_seconds=0,
        sleep_fn=lambda _: None,
        session=FakeSession([empty_payload(has_next=True), review_payload(), empty_payload(has_next=False)]),
    )

    assert len(report["page_reports"]) == 3
    assert report["page_reports"][0]["terminal_reason"] is None
    assert report["page_reports"][0]["has_next_link"] is True
    assert report["page_reports"][1]["review_count"] == 1
    assert report["page_reports"][2]["terminal_reason"] == "page_cap"
    assert report["review_count"] == 1
    assert report["sparse_empty_pages"] == 1


def test_fetch_targets_stops_after_empty_page_threshold(tmp_path):
    report = fetch_targets(
        [fixture_target()],
        tmp_path / "raw",
        "run",
        max_pages_per_app_country=10,
        max_consecutive_empty_pages=2,
        request_delay_seconds=0,
        retry_delay_seconds=0,
        sleep_fn=lambda _: None,
        session=FakeSession([empty_payload(has_next=True), empty_payload(has_next=True), review_payload()]),
    )

    assert len(report["page_reports"]) == 2
    assert report["page_reports"][0]["terminal_reason"] is None
    assert report["page_reports"][1]["terminal_reason"] == "empty_page_after_sparse_scan"
    assert report["review_count"] == 0
    assert report["warning_scopes"][0]["reason"] == "empty_page_after_sparse_scan"


def test_database_helpers():
    assert mask_database_url("postgresql://user:secret@localhost:5432/app_store_reviews") == "postgresql://user:***@localhost:5432/app_store_reviews"
    assert scope_key("123", "US") == "123:us:mostrecent"


def test_web_probe_url_helpers():
    assert app_store_web_catalog_url("1508186374", "US").startswith(
        "https://apps.apple.com/api/apps/v1/catalog/us/apps/1508186374?"
    )
    assert (
        app_store_web_catalog_next_url("/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=6")
        == "https://apps.apple.com/api/apps/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=6&platform=iphone&sort=recent&limit=20"
    )
    assert (
        app_store_web_reviews_url("1508186374", "US")
        == "https://apps.apple.com/api/apps/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=0&platform=iphone&sort=recent&limit=20"
    )


def test_web_probe_parses_html_review_signals():
    page_html = """
    <html>
      <head>
        <script type="application/ld+json">
          {"@type": "SoftwareApplication", "aggregateRating": {
            "ratingValue": "4.7", "ratingCount": "3400000", "reviewCount": "12345"
          }}
        </script>
        <script id="serialized-server-data" type="application/json">
          {"page":{"nextHref":"/v1/catalog/us/apps/1508186374/reviews?l=en-US&amp;offset=6"}}
        </script>
      </head>
      <body>
        <h2 id="review-14119272497-title">Always having to update</h2>
        <h2 id="review-14152889552-title">Better than Netflix</h2>
        <h2 id="review-14119272497-title">Duplicate DOM ref</h2>
      </body>
    </html>
    """

    assert parse_html_review_ids(page_html) == ["14119272497", "14152889552"]
    assert parse_serialized_next_href(page_html) == "/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=6"
    assert parse_json_ld_aggregate_rating(page_html) == {
        "rating_value": 4.7,
        "rating_count": 3400000,
        "review_count": 12345,
    }


def test_web_probe_parses_catalog_review_relationship():
    payload = {
        "data": [
            {
                "id": "1508186374",
                "type": "apps",
                "relationships": {
                    "reviews": {
                        "next": "/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=6",
                        "data": [
                            {
                                "id": "review-1",
                                "type": "user-reviews",
                                "attributes": {
                                    "date": "2026-06-10T18:50:44Z",
                                    "rating": 3,
                                    "review": "Useful text",
                                },
                            },
                            {
                                "id": "review-2",
                                "type": "user-reviews",
                                "attributes": {"date": "2026-06-12T01:00:00Z", "rating": 5},
                            },
                        ],
                    }
                },
            }
        ]
    }

    summary = parse_web_catalog_reviews(payload)

    assert summary["review_count"] == 2
    assert summary["review_ids"] == ["review-1", "review-2"]
    assert summary["next_href"] == "/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=6"
    assert summary["min_date"] == "2026-06-10T18:50:44Z"
    assert summary["max_date"] == "2026-06-12T01:00:00Z"


def test_web_probe_parses_catalog_review_page():
    payload = {
        "next": "/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=12",
        "data": [
            {"id": "review-3", "type": "user-reviews", "attributes": {"date": "2026-06-13T01:00:00Z"}},
            {"id": "review-4", "type": "user-reviews", "attributes": {"date": "2026-06-14T01:00:00Z"}},
        ],
    }

    summary = parse_web_catalog_review_page(payload)

    assert summary["review_count"] == 2
    assert summary["review_ids"] == ["review-3", "review-4"]
    assert summary["next_href"] == "/v1/catalog/us/apps/1508186374/reviews?l=en-US&offset=12"
    assert summary["min_date"] == "2026-06-13T01:00:00Z"
    assert summary["max_date"] == "2026-06-14T01:00:00Z"


def test_source_comparison_summary_gate():
    rss_report = {
        "page_reports": [
            {"status": "ok", "review_count": 50, "terminal_reason": "empty_page"},
        ],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 50,
        "unique_review_count": 50,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    web_report = {
        "summary": {
            "web_catalog_page_reviews_total": 120,
            "web_catalog_page_status_counts": {"200": 20},
            "recovered_429_page_count": 3,
            "retried_page_count": 3,
        }
    }

    summary = summarize_comparison(rss_report, web_report)

    assert summary["web_reviews_minus_rss_reviews"] == 70
    assert summary["web_to_rss_review_ratio"] == 2.4
    assert summary["web_reviews_same_order_as_rss"] is True
    assert summary["web_reviews_at_or_above_rss"] is True
    assert summary["web_all_pages_ok_after_retry"] is True
    assert summary["web_unrecovered_429_page_count"] == 0
    assert summary["candidate_passes_same_order_stability_gate"] is True
    assert summary["candidate_passes_single_run_gate"] is True
    assert summary["web_429_recovery_rate_after_retry"] == 1.0
    assert summary["web_configured_review_ceiling"] is None


def test_source_comparison_gate_requires_web_reviews():
    rss_report = {
        "page_reports": [{"status": "ok", "review_count": 0}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 1,
        "sparse_empty_pages": 0,
        "review_count": 0,
        "unique_review_count": 0,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    web_report = {
        "summary": {
            "web_catalog_page_reviews_total": 0,
            "web_catalog_page_status_counts": {"200": 1},
            "recovered_429_page_count": 0,
            "retried_page_count": 0,
        }
    }

    summary = summarize_comparison(rss_report, web_report)

    assert summary["web_reviews_at_or_above_rss"] is True
    assert summary["web_reviews_same_order_as_rss"] is False
    assert summary["candidate_passes_same_order_stability_gate"] is False
    assert summary["candidate_passes_single_run_gate"] is False


def test_source_comparison_same_order_gate_allows_lower_same_magnitude_volume():
    rss_report = {
        "page_reports": [{"status": "ok", "review_count": 50}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 10000,
        "unique_review_count": 10000,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    web_report = {
        "summary": {
            "web_catalog_page_reviews_total": 2000,
            "web_catalog_page_status_counts": {"200": 100},
            "recovered_429_page_count": 5,
            "retried_page_count": 5,
        }
    }

    summary = summarize_comparison(
        rss_report,
        web_report,
        scope_count=20,
        web_max_pages=5,
        web_review_limit=20,
    )

    assert summary["web_to_rss_review_ratio"] == 0.2
    assert summary["web_reviews_same_order_as_rss"] is True
    assert summary["web_reviews_at_or_above_rss"] is False
    assert summary["candidate_passes_same_order_stability_gate"] is True
    assert summary["candidate_passes_single_run_gate"] is False
    assert summary["web_configured_review_ceiling"] == 2000
    assert summary["web_configured_ceiling_usage_ratio"] == 1.0
    assert summary["web_configured_ceiling_hit"] is True
    assert summary["web_pages_per_scope_needed_for_rss_parity"] == 25
    assert summary["web_additional_pages_per_scope_needed_for_rss_parity"] == 20
    assert summary["web_page_depth_can_reach_rss_parity"] is False
    assert summary["web_volume_gap_likely_configuration_limited"] is True


def test_source_comparison_capacity_marks_depth_that_can_reach_parity():
    rss_report = {
        "page_reports": [{"status": "ok", "review_count": 50}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 10000,
        "unique_review_count": 10000,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    web_report = {
        "summary": {
            "web_catalog_page_reviews_total": 10000,
            "web_catalog_page_status_counts": {"200": 500},
            "recovered_429_page_count": 0,
            "retried_page_count": 0,
        }
    }

    summary = summarize_comparison(
        rss_report,
        web_report,
        scope_count=20,
        web_max_pages=25,
        web_review_limit=20,
    )

    assert summary["web_configured_review_ceiling"] == 10000
    assert summary["web_pages_per_scope_needed_for_rss_parity"] == 25
    assert summary["web_additional_pages_per_scope_needed_for_rss_parity"] == 0
    assert summary["web_page_depth_can_reach_rss_parity"] is True
    assert summary["web_reviews_at_or_above_rss"] is True
    assert summary["candidate_passes_single_run_gate"] is True
    assert summary["web_volume_gap_likely_configuration_limited"] is False


def test_source_comparison_reports_unrecovered_429_rate():
    rss_report = {
        "page_reports": [{"status": "ok", "review_count": 50}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 500,
        "unique_review_count": 500,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    web_report = {
        "summary": {
            "web_catalog_page_reviews_total": 120,
            "web_catalog_page_status_counts": {"200": 6, "429": 3},
            "recovered_429_page_count": 2,
            "retried_page_count": 5,
        }
    }

    summary = summarize_comparison(rss_report, web_report)

    assert summary["web_unrecovered_429_page_count"] == 3
    assert summary["web_non_200_page_count_after_retry"] == 3
    assert summary["web_recovered_429_page_count"] == 2
    assert summary["web_429_recovery_rate_after_retry"] == 0.4
    assert summary["web_all_pages_ok_after_retry"] is False


def test_source_comparison_per_scope():
    rss_report = {
        "page_reports": [
            {
                "app_id": "123",
                "country": "US",
                "status": "ok",
                "review_count": 50,
                "terminal_reason": "empty_page",
            }
        ]
    }
    web_report = {
        "results": [
            {
                "app_id": "123",
                "app_name": "Fixture",
                "country": "us",
                "web_catalog_pages_fetched": 2,
                "web_catalog_page_reviews_total": 12,
                "web_catalog_pages": [
                    {"status_code": 429, "review_count": 0, "attempts": [{"status_code": 429}]},
                    {
                        "status_code": 200,
                        "review_count": 6,
                        "attempts": [{"status_code": 429}, {"status_code": 200}],
                        "min_date": "2026-06-10T00:00:00Z",
                        "max_date": "2026-06-10T01:00:00Z",
                    },
                ],
            }
        ]
    }

    rows = compare_per_scope(rss_report, web_report)

    assert rows == [
        {
            "app_id": "123",
            "app_name": "Fixture",
            "country": "us",
            "rss_page_count": 1,
            "rss_fetch_errors": 0,
            "rss_empty_pages": 0,
            "rss_review_count": 50,
            "rss_terminal_reasons": {"empty_page": 1},
            "web_page_count": 2,
            "web_review_count": 12,
            "web_status_counts": {"429": 1, "200": 1},
            "web_retried_pages": 1,
            "web_recovered_429_pages": 1,
            "web_min_date": "2026-06-10T00:00:00Z",
            "web_max_date": "2026-06-10T01:00:00Z",
        }
    ]


def test_42matters_reviews_url_and_redaction():
    url = build_42matters_reviews_url(
        "284882215",
        access_token="secret-token",
        days=30,
        lang="en",
        rating=5,
        limit=100,
        page=2,
    )

    assert url.startswith("https://data.42matters.com/api/v5.0/ios/apps/reviews.json?")
    assert "id=284882215" in url
    assert "access_token=secret-token" in url
    assert "days=30" in url
    assert "lang=en" in url
    assert "rating=5" in url
    assert "limit=100" in url
    assert "page=2" in url
    assert "access_token=%2A%2A%2A" in redact_access_token(url)
    assert "secret-token" not in redact_access_token(url)


def test_42matters_reviews_payload_summary():
    payload = {
        "number_reviews": 2,
        "total_reviews": 2500,
        "number_reviews_remaining": 2498,
        "page": 1,
        "limit": 100,
        "total_pages": 25,
        "reviews": [
            {
                "author_hash": "author-a",
                "title": "Useful",
                "rating": 5,
                "content": "Works well",
                "date": "2026-06-17",
                "app_version": "1.0",
            },
            {
                "author_hash": "author-b",
                "title": "Bug",
                "rating": 2,
                "date": "2026-06-18",
                "app_version": "1.1",
            },
        ],
    }

    summary = parse_42matters_reviews_payload(payload)

    assert summary["number_reviews"] == 2
    assert summary["total_reviews"] == 2500
    assert summary["number_reviews_remaining"] == 2498
    assert summary["page"] == 1
    assert summary["limit"] == 100
    assert summary["total_pages"] == 25
    assert summary["review_count"] == 2
    assert summary["missing_content_count"] == 1
    assert summary["min_date"] == "2026-06-17"
    assert summary["max_date"] == "2026-06-18"
    assert len(summary["review_fingerprints"]) == 2
    assert summary["review_fingerprints"][0] != summary["review_fingerprints"][1]
