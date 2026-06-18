from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app_store_review_pipeline.apple_rss import apple_rss_url, normalize_entries, parse_apple_review
from app_store_review_pipeline.apple_web import (
    app_store_web_catalog_next_url,
    app_store_web_catalog_url,
    app_store_web_reviews_url,
    get_with_429_retries,
    parse_html_review_ids,
    parse_json_ld_aggregate_rating,
    parse_retry_after_seconds,
    parse_serialized_next_href,
    parse_web_catalog_review_page,
    parse_web_catalog_review_rows,
    parse_web_catalog_reviews,
    probe_web_reviews,
    probe_web_reviews_for_scope,
)
from app_store_review_pipeline.cli import select_target_window, summarize_fetch_cli
from app_store_review_pipeline.config import WEB_CATALOG_SOURCE
from app_store_review_pipeline.fetcher import fetch_targets, terminal_reason_for_page
from app_store_review_pipeline.models import AppTarget, ReviewPage
from app_store_review_pipeline.postgres_database import infer_field_value, mask_database_url, scope_key
from app_store_review_pipeline.provider_apptweak import (
    apptweak_headers,
    build_apptweak_reviews_url,
    parse_apptweak_reviews_payload,
)
from app_store_review_pipeline.provider_appfigures import (
    appfigures_headers,
    build_appfigures_product_lookup_url,
    build_appfigures_reviews_url,
    parse_appfigures_product_payload,
    parse_appfigures_reviews_payload,
    probe_appfigures_reviews_for_scope,
)
from app_store_review_pipeline.provider_42matters import (
    build_42matters_reviews_url,
    parse_42matters_reviews_payload,
    redact_access_token,
)
from app_store_review_pipeline.provider_compare import (
    compare_provider_per_app,
    summarize_provider_comparison,
)
from app_store_review_pipeline.source_compare import (
    build_web_source_decision,
    compare_per_scope,
    render_source_markdown_report,
    rss_review_counts_by_scope,
    summarize_comparison,
)
from app_store_review_pipeline.targets import active_targets, load_targets, parse_countries
from app_store_review_pipeline.web_catalog_fetcher import fetch_web_catalog_targets
from scripts.run_provider_matrix import build_source_decision, render_markdown_report
from scripts.summarize_source_comparisons import (
    render_markdown_summary,
    summarize_history_from_reports,
)
from scripts.summarize_web_catalog_ingestion import (
    render_markdown_summary as render_web_ingestion_markdown_summary,
)
from scripts.summarize_web_catalog_ingestion import (
    summarize_history_from_reports as summarize_web_ingestion_history_from_reports,
)


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
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None):
        self.payload = payload
        self.status_code = status_code
        self.content = str(payload).encode("utf-8")
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, *args, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self.payloads:
            raise AssertionError("No fake response payloads remaining")
        return FakeResponse(self.payloads.pop(0))

    def close(self):
        pass


class FakeWebResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict | None = None,
        content: bytes = b"{}",
        payload: dict | None = None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else content
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        if self.payload is not None:
            return self.payload
        return json.loads(self.text or "{}")


class FakeWebSession:
    def __init__(self, responses: list[FakeWebResponse]):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *args, **kwargs):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("No fake web responses remaining")
        return self.responses.pop(0)


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


def web_catalog_payload(start=1, count=2, has_next=True):
    rows = []
    for index in range(start, start + count):
        rows.append(
            {
                "id": f"web-review-{index}",
                "type": "user-reviews",
                "attributes": {
                    "date": f"2026-06-17T10:0{index}:00Z",
                    "rating": 5 - (index % 2),
                    "review": f"Web catalog review text {index}",
                    "title": f"Web title {index}",
                    "userName": f"web-user-{index}",
                },
            }
        )
    return {
        "next": "/v1/catalog/us/apps/123456789/reviews?l=en-US&offset=2" if has_next else None,
        "data": rows,
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


def test_select_target_window_supports_offset_and_limit():
    targets = [
        AppTarget(
            app_name=f"Fixture {index}",
            category="test",
            apple_app_id=str(index),
            apple_slug=f"fixture-{index}",
            countries=("us",),
            active=True,
            notes=None,
        )
        for index in range(6)
    ]

    assert [target.apple_app_id for target in select_target_window(targets, limit=2, offset=3)] == ["3", "4"]
    assert [target.apple_app_id for target in select_target_window(targets, limit=0, offset=4)] == ["4", "5"]
    assert [target.apple_app_id for target in select_target_window(targets, limit=2, offset=-5)] == ["0", "1"]


def write_source_comparison_report(
    path: Path,
    *,
    run_id: str,
    status: str,
    app_name: str = "Fixture",
    target_count: int = 1,
    rss_reviews: int = 500,
    web_reviews: int = 500,
    final_non_200: int = 0,
    recovered_429: int = 0,
    unrecovered_429: int = 0,
    time_budget_exceeded: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "run_id": run_id,
        "started_at": "2026-06-18T00:00:00+00:00",
        "completed_at": "2026-06-18T00:05:00+00:00",
        "target_count": target_count,
        "scope_count": target_count,
        "settings": {
            "target_offset": 0,
            "web_max_pages": 25,
            "web_review_limit": 20,
            "web_request_delay_seconds": 5,
            "web_429_retries": 5,
            "web_429_retry_seconds": 60,
            "web_429_backoff_multiplier": 1.5,
            "web_stop_at_rss_parity": True,
            "web_time_budget_seconds": 1200,
        },
        "rss": {"unique_reviews_seen": rss_reviews},
        "web_catalog": {
            "web_catalog_page_reviews_total": web_reviews,
            "web_catalog_page_status_counts": {"200": 25},
            "web_catalog_stop_reasons": {"target_review_count_reached": target_count},
        },
        "comparison": {
            "rss_unique_reviews_seen": rss_reviews,
            "web_catalog_page_reviews_total": web_reviews,
            "web_to_rss_review_ratio": web_reviews / rss_reviews,
            "web_non_200_page_count_after_retry": final_non_200,
            "web_unrecovered_429_page_count": unrecovered_429,
            "web_recovered_429_page_count": recovered_429,
            "web_retried_page_count": recovered_429 + unrecovered_429,
            "web_all_pages_ok_after_retry": final_non_200 == 0,
            "web_time_budget_exceeded": time_budget_exceeded,
            "web_planned_scope_count": target_count,
            "web_completed_scope_count": target_count,
            "web_skipped_scope_count": 0,
            "web_all_scopes_completed": not time_budget_exceeded,
        },
        "per_scope": [{"app_name": app_name, "app_id": "123", "country": "us"}],
        "source_decision": {"status": status, "selected_source": "apple_web_catalog_reviews"},
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_source_comparison_history_promotes_clean_single_app_runs(tmp_path):
    paths = []
    for index in range(2):
        path = tmp_path / f"run-{index}" / "source_comparison_report.json"
        write_source_comparison_report(
            path,
            run_id=f"run-{index}",
            status="web_catalog_replacement_candidate",
            app_name=f"Fixture {index}",
            recovered_429=index,
        )
        paths.append(path)

    summary = summarize_history_from_reports(paths, min_runs=2, single_app_only=True)
    markdown = render_markdown_summary(summary)

    assert summary["promotion_gate"]["status"] == "ready_for_promotion"
    assert summary["aggregate"]["replacement_candidate_runs"] == 2
    assert summary["aggregate"]["web_recovered_429_pages_total"] == 1
    assert "Fixture 0" in markdown


def test_source_comparison_history_blocks_mixed_or_incomplete_runs(tmp_path):
    clean_path = tmp_path / "clean" / "source_comparison_report.json"
    budget_path = tmp_path / "budget" / "source_comparison_report.json"
    write_source_comparison_report(
        clean_path,
        run_id="clean",
        status="web_catalog_replacement_candidate",
    )
    write_source_comparison_report(
        budget_path,
        run_id="budget",
        status="web_catalog_time_budget_exceeded",
        web_reviews=320,
        final_non_200=2,
        unrecovered_429=2,
        time_budget_exceeded=True,
    )

    summary = summarize_history_from_reports([clean_path, budget_path], min_runs=2)

    assert summary["promotion_gate"]["status"] == "not_ready"
    assert "not_all_runs_are_replacement_candidates" in summary["promotion_gate"]["blocking_reasons"]
    assert "one_or_more_runs_exceeded_time_budget" in summary["promotion_gate"]["blocking_reasons"]
    assert summary["aggregate"]["runs_with_final_non_200_pages"] == 1


def test_parse_web_catalog_review_rows_returns_full_review_rows():
    collected_at = "2026-06-18T00:00:00+00:00"
    reviews = parse_web_catalog_review_rows(
        web_catalog_payload(start=1, count=1, has_next=False),
        fixture_target(),
        country="US",
        page_number=1,
        page_key="run:123456789:us:recent:1",
        collected_at=collected_at,
    )

    assert len(reviews) == 1
    review = reviews[0]
    assert review.source == WEB_CATALOG_SOURCE
    assert review.review_key == "apple_app_store:apple_app_store_web_catalog_reviews:us:123456789:web-review-1"
    assert review.author_name == "web-user-1"
    assert review.rating == 4
    assert review.title == "Web title 1"
    assert review.content == "Web catalog review text 1"
    assert review.updated_epoch_seconds is not None
    assert review.version is None


def test_fetch_web_catalog_targets_follows_next_pages_and_preserves_source(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=2, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=2,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        session=session,
    )

    assert report["source"] == WEB_CATALOG_SOURCE
    assert report["fetched_pages"] == 2
    assert report["start_page"] == 1
    assert report["review_count"] == 4
    assert report["unique_review_count"] == 4
    assert report["page_reports"][0]["source"] == WEB_CATALOG_SOURCE
    assert report["page_reports"][1]["terminal_reason"] == "page_cap"
    assert report["reviews"][0]["content"] == "Web catalog review text 1"
    assert report["reviews"][0]["source_page_key"] == "run:123456789:us:recent:1"
    assert len(session.calls) == 2


def test_fetch_web_catalog_targets_can_start_from_deeper_page(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=101, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=103, count=2, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        start_page=51,
        max_pages_per_app_country=52,
        review_limit=20,
        request_delay_seconds=0,
        web_429_retries=0,
        session=session,
    )

    assert report["fetched_pages"] == 2
    assert report["start_page"] == 51
    assert report["review_count"] == 4
    assert "offset=1000" in session.calls[0]
    assert report["page_reports"][0]["page_number"] == 51
    assert report["page_reports"][0]["raw_json_path"].endswith("_051.json")
    assert report["page_reports"][1]["terminal_reason"] == "page_cap"


def test_summarize_fetch_cli_includes_stability_metrics():
    summary = summarize_fetch_cli(
        {
            "review_count": 2,
            "unique_review_count": 2,
            "fetch_errors": 1,
            "capped_scopes": [{"app_id": "123"}],
            "sparse_empty_pages": 0,
            "page_reports": [
                {
                    "status": "ok",
                    "status_code": 200,
                    "attempt_count": 2,
                    "terminal_reason": None,
                    "missing_text_count": 0,
                    "missing_rating_count": 0,
                },
                {
                    "status": "error",
                    "status_code": 429,
                    "attempt_count": 6,
                    "terminal_reason": "fetch_error",
                    "missing_text_count": 1,
                    "missing_rating_count": 1,
                },
            ],
        }
    )

    assert summary["status_counts"] == {"ok": 1, "error": 1}
    assert summary["status_code_counts"] == {"200": 1, "429": 1}
    assert summary["attempt_counts"] == {"2": 1, "6": 1}
    assert summary["retried_pages"] == 2
    assert summary["successful_after_retry_pages"] == 1
    assert summary["final_non_200_pages"] == 1
    assert summary["terminal_reasons"] == {"fetch_error": 1}
    assert summary["missing_text"] == 1
    assert summary["missing_rating"] == 1
    assert summary["all_pages_ok_after_retry"] is False


def test_load_source_inference_prefers_raw_row_source():
    assert (
        infer_field_value(
            [{"source": WEB_CATALOG_SOURCE}],
            [{"source": "apple_itunes_customerreviews_rss"}],
            "source",
            "fallback",
        )
        == WEB_CATALOG_SOURCE
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


def test_web_429_retry_uses_backoff_and_retry_after():
    sleeps = []
    response, attempts = get_with_429_retries(
        FakeWebSession(
            [
                FakeWebResponse(429),
                FakeWebResponse(429, headers={"retry-after": "7"}),
                FakeWebResponse(200),
            ]
        ),
        "https://apps.apple.com/api/apps/v1/catalog/us/apps/123/reviews",
        headers={},
        timeout_seconds=1,
        web_429_retries=2,
        web_429_retry_seconds=10,
        web_429_backoff_multiplier=2,
        sleep_fn=sleeps.append,
    )

    assert response.status_code == 200
    assert [attempt["status_code"] for attempt in attempts] == [429, 429, 200]
    assert sleeps == [10, 7.0]
    assert parse_retry_after_seconds("3") == 3.0
    assert parse_retry_after_seconds("bad") is None


def test_web_429_retry_stops_when_time_budget_cannot_fit_next_sleep():
    sleeps = []
    response, attempts = get_with_429_retries(
        FakeWebSession([FakeWebResponse(429), FakeWebResponse(200)]),
        "https://apps.apple.com/api/apps/v1/catalog/us/apps/123/reviews",
        headers={},
        timeout_seconds=1,
        web_429_retries=1,
        web_429_retry_seconds=10,
        web_429_backoff_multiplier=1,
        deadline_monotonic=105,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: 100,
    )

    assert response.status_code == 429
    assert [attempt["status_code"] for attempt in attempts] == [429]
    assert attempts[-1]["retry_skipped_reason"] == "time_budget_exceeded"
    assert sleeps == []


def test_web_probe_can_skip_html_request():
    session = FakeWebSession([FakeWebResponse(200)])

    report = probe_web_reviews_for_scope(
        fixture_target(),
        "us",
        session=session,
        timeout_seconds=1,
        review_limit=20,
        web_sort="recent",
        attempt_pagination=False,
        max_web_pages=1,
        request_delay_seconds=0,
        web_429_retries=0,
        web_429_retry_seconds=0,
        web_429_backoff_multiplier=1,
        include_html=False,
        sleep_fn=lambda _seconds: None,
    )

    assert session.calls == ["https://apps.apple.com/api/apps/v1/catalog/us/apps/123456789/reviews?l=en-US&offset=0&platform=iphone&sort=recent&limit=20"]
    assert report["html_probe_enabled"] is False
    assert report["html_status_code"] is None
    assert report["web_catalog_status_code"] == 200


def test_web_probe_scope_reports_time_budget_stop_before_next_page():
    session = FakeWebSession(
        [
            FakeWebResponse(
                200,
                payload={
                    "next": "/v1/catalog/us/apps/123456789/reviews?l=en-US&offset=20",
                    "data": [{"id": "review-1", "attributes": {"date": "2026-06-18T00:00:00Z"}}],
                },
            )
        ]
    )

    report = probe_web_reviews_for_scope(
        fixture_target(),
        "us",
        session=session,
        timeout_seconds=1,
        review_limit=20,
        web_sort="recent",
        attempt_pagination=True,
        max_web_pages=5,
        request_delay_seconds=0,
        web_429_retries=0,
        web_429_retry_seconds=0,
        web_429_backoff_multiplier=1,
        include_html=False,
        deadline_monotonic=0,
        sleep_fn=lambda _seconds: None,
        monotonic_fn=lambda: 0,
    )

    assert report["web_catalog_pages_fetched"] == 1
    assert report["web_catalog_stop_reason"] == "time_budget_exceeded"


def test_web_probe_summary_records_skipped_scopes_after_time_budget(tmp_path):
    second_target = AppTarget(
        app_name="Second Fixture",
        category="test",
        apple_app_id="987654321",
        apple_slug="second-fixture",
        countries=("us",),
        active=True,
        notes=None,
    )

    report = probe_web_reviews(
        [fixture_target(), second_target],
        tmp_path / "web_probe.json",
        limit=0,
        request_delay_seconds=2,
        attempt_pagination=False,
        include_html=False,
        time_budget_seconds=1,
        session=FakeWebSession([FakeWebResponse(200)]),
        sleep_fn=lambda _seconds: None,
        monotonic_fn=lambda: 0,
    )

    assert report["time_budget_exceeded"] is True
    assert report["summary"]["planned_scope_count"] == 2
    assert report["summary"]["completed_scope_count"] == 1
    assert report["summary"]["skipped_scope_count"] == 1
    assert report["summary"]["time_budget_exceeded"] is True


def test_web_probe_stops_after_target_review_count_is_reached():
    session = FakeWebSession(
        [
            FakeWebResponse(
                200,
                payload={
                    "next": "/v1/catalog/us/apps/123456789/reviews?l=en-US&offset=20",
                    "data": [
                        {"id": "review-1", "attributes": {"date": "2026-06-18T00:00:00Z"}},
                        {"id": "review-2", "attributes": {"date": "2026-06-18T00:00:01Z"}},
                    ],
                },
            ),
            FakeWebResponse(
                200,
                payload={
                    "next": "/v1/catalog/us/apps/123456789/reviews?l=en-US&offset=40",
                    "data": [
                        {"id": "review-3", "attributes": {"date": "2026-06-18T00:00:02Z"}},
                        {"id": "review-4", "attributes": {"date": "2026-06-18T00:00:03Z"}},
                    ],
                },
            ),
            FakeWebResponse(
                200,
                payload={
                    "next": None,
                    "data": [{"id": "review-5", "attributes": {"date": "2026-06-18T00:00:04Z"}}],
                },
            ),
        ]
    )

    report = probe_web_reviews_for_scope(
        fixture_target(),
        "us",
        session=session,
        timeout_seconds=1,
        review_limit=2,
        web_sort="recent",
        attempt_pagination=True,
        max_web_pages=5,
        request_delay_seconds=0,
        web_429_retries=0,
        web_429_retry_seconds=0,
        web_429_backoff_multiplier=1,
        include_html=False,
        target_review_count=4,
        sleep_fn=lambda _seconds: None,
    )

    assert len(session.calls) == 2
    assert report["web_catalog_pages_fetched"] == 2
    assert report["web_catalog_page_reviews_total"] == 4
    assert report["web_catalog_target_review_count"] == 4
    assert report["web_catalog_target_reached"] is True
    assert report["web_catalog_stop_reason"] == "target_review_count_reached"


def test_rss_review_counts_by_scope():
    report = {
        "page_reports": [
            {"app_id": "123", "country": "US", "review_count": 20},
            {"app_id": "123", "country": "us", "review_count": 30},
            {"app_id": "456", "country": "CA", "review_count": 10},
        ]
    }

    counts = rss_review_counts_by_scope(report)

    assert counts == {("123", "us"): 50, ("456", "ca"): 10}


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


def test_source_comparison_empty_rss_baseline_cannot_pass_replacement_gate():
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
            "web_catalog_page_reviews_total": 20,
            "web_catalog_page_status_counts": {"200": 1},
            "recovered_429_page_count": 0,
            "retried_page_count": 0,
        }
    }

    summary = summarize_comparison(
        rss_report,
        web_report,
        scope_count=1,
        web_max_pages=1,
        web_review_limit=20,
    )
    decision = build_web_source_decision(summary)

    assert summary["rss_unique_reviews_seen"] == 0
    assert summary["web_catalog_page_reviews_total"] == 20
    assert summary["web_reviews_at_or_above_rss"] is True
    assert summary["web_reviews_same_order_as_rss"] is False
    assert summary["candidate_passes_same_order_stability_gate"] is False
    assert summary["candidate_passes_single_run_gate"] is False
    assert decision["status"] == "rss_baseline_empty"
    assert decision["blocking_metric"] == "rss_unique_reviews_seen"


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


def test_web_source_decision_selects_replacement_candidate():
    decision = build_web_source_decision(
        {
            "candidate_passes_single_run_gate": True,
            "candidate_passes_same_order_stability_gate": True,
            "rss_fetch_error_count": 0,
            "rss_unique_reviews_seen": 100,
            "web_non_200_page_count_after_retry": 0,
            "web_to_rss_review_ratio": 1.1,
        }
    )

    assert decision["status"] == "web_catalog_replacement_candidate"
    assert decision["selected_source"] == "apple_web_catalog_reviews"


def test_web_source_decision_requests_deeper_configuration_limited_run():
    decision = build_web_source_decision(
        {
            "candidate_passes_single_run_gate": False,
            "candidate_passes_same_order_stability_gate": True,
            "rss_fetch_error_count": 0,
            "rss_unique_reviews_seen": 1000,
            "web_non_200_page_count_after_retry": 0,
            "web_volume_gap_likely_configuration_limited": True,
            "web_to_rss_review_ratio": 0.2,
            "web_additional_pages_per_scope_needed_for_rss_parity": 20,
        }
    )

    assert decision["status"] == "needs_deeper_web_catalog_run"
    assert decision["web_additional_pages_per_scope_needed_for_rss_parity"] == 20


def test_web_source_decision_blocks_unstable_final_pages():
    decision = build_web_source_decision(
        {
            "candidate_passes_single_run_gate": False,
            "candidate_passes_same_order_stability_gate": False,
            "rss_fetch_error_count": 0,
            "rss_unique_reviews_seen": 1000,
            "web_non_200_page_count_after_retry": 3,
            "web_unrecovered_429_page_count": 3,
            "web_volume_gap_likely_configuration_limited": True,
        }
    )

    assert decision["status"] == "web_catalog_unstable_after_retry"
    assert decision["selected_source"] is None
    assert decision["blocking_metric"] == "web_non_200_page_count_after_retry"


def test_web_source_decision_blocks_time_budget_exceeded_runs():
    decision = build_web_source_decision(
        {
            "candidate_passes_single_run_gate": False,
            "candidate_passes_same_order_stability_gate": False,
            "rss_fetch_error_count": 0,
            "rss_unique_reviews_seen": 1000,
            "web_non_200_page_count_after_retry": 0,
            "web_time_budget_exceeded": True,
            "web_all_scopes_completed": False,
            "web_completed_scope_count": 3,
            "web_planned_scope_count": 5,
        }
    )

    assert decision["status"] == "web_catalog_time_budget_exceeded"
    assert decision["selected_source"] is None
    assert decision["web_completed_scope_count"] == 3
    assert decision["web_planned_scope_count"] == 5


def test_web_source_markdown_report_includes_decision_and_gates():
    report = {
        "source_decision": {
            "status": "same_order_but_not_replacement",
            "selected_source": "apple_web_catalog_reviews",
            "recommended_next_action": "Keep monitoring.",
        },
        "rss": {"unique_reviews_seen": 1000},
        "web_catalog": {"web_catalog_page_reviews_total": 200},
        "comparison": {
            "web_to_rss_review_ratio": 0.2,
            "web_reviews_minus_rss_reviews": -800,
            "candidate_passes_single_run_gate": False,
            "candidate_passes_same_order_stability_gate": True,
            "web_all_pages_ok_after_retry": True,
            "rss_fetch_error_count": 0,
            "web_non_200_page_count_after_retry": 0,
            "web_unrecovered_429_page_count": 0,
            "web_configured_review_ceiling": 200,
            "web_configured_ceiling_hit": True,
            "web_pages_per_scope_needed_for_rss_parity": 25,
            "web_additional_pages_per_scope_needed_for_rss_parity": 20,
            "web_page_depth_can_reach_rss_parity": False,
            "web_volume_gap_likely_configuration_limited": True,
        },
        "settings": {
            "web_max_pages": 5,
            "web_review_limit": 20,
            "web_request_delay_seconds": 2,
            "web_429_retries": 3,
            "web_429_retry_seconds": 45,
            "web_include_html": False,
        },
    }

    markdown = render_source_markdown_report(report)

    assert "Decision: **same_order_but_not_replacement**" in markdown
    assert "Selected source: `apple_web_catalog_reviews`" in markdown
    assert "| Replacement gate | no |" in markdown
    assert "| Same-order stability gate | yes |" in markdown
    assert "Web/RSS ratio: `0.200`" in markdown


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


def write_web_ingestion_report(path: Path, *, run_id: str, reviews: int, clean: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    final_non_200 = 0 if clean else 1
    status_code_counts = {"200": 25} if clean else {"200": 24, "429": 1}
    report = {
        "run_id": run_id,
        "started_at": f"2026-06-18T19:00:0{run_id[-1]}Z",
        "completed_at": f"2026-06-18T19:02:0{run_id[-1]}Z",
        "source": WEB_CATALOG_SOURCE,
        "target_count": 1,
        "scope_count": 1,
        "target_offset": 10,
        "max_pages_per_app_country": 25,
        "review_limit": 20,
        "fetch_summary": {
            "pages": 25,
            "reviews": reviews,
            "unique_reviews": reviews,
            "fetch_errors": 0,
            "status_code_counts": status_code_counts,
            "attempt_counts": {"1": 25},
            "retried_pages": 0,
            "successful_after_retry_pages": 0,
            "final_non_200_pages": final_non_200,
            "terminal_reasons": {"page_cap": 1},
            "missing_text": 0,
            "missing_rating": 0,
            "all_pages_ok_after_retry": clean,
        },
        "load_summary": {
            "inserted": reviews,
            "updated": 0,
            "duplicates_skipped": 0,
        },
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def test_web_catalog_ingestion_history_gate_requires_repeated_clean_full_runs(tmp_path):
    paths = []
    for index in range(5):
        path = tmp_path / f"run-{index}" / "daily_report.json"
        write_web_ingestion_report(path, run_id=f"run-{index}", reviews=500)
        paths.append(path)

    summary = summarize_web_ingestion_history_from_reports(paths, min_runs=5, full_single_app_only=True)

    assert summary["promotion_gate"]["status"] == "ready_for_controlled_promotion"
    assert summary["aggregate"]["reviews_total"] == 2500
    assert summary["aggregate"]["status_code_counts"] == {"200": 125}
    assert summary["aggregate"]["final_non_200_pages_total"] == 0


def test_web_catalog_ingestion_history_blocks_partial_runs(tmp_path):
    good_path = tmp_path / "good" / "daily_report.json"
    bad_path = tmp_path / "bad" / "daily_report.json"
    write_web_ingestion_report(good_path, run_id="run-1", reviews=500)
    write_web_ingestion_report(bad_path, run_id="run-2", reviews=480, clean=False)

    summary = summarize_web_ingestion_history_from_reports(
        [good_path, bad_path],
        min_runs=2,
        full_single_app_only=True,
        min_reviews_per_run=500,
    )

    assert summary["promotion_gate"]["status"] == "not_ready"
    assert "one_or_more_runs_not_clean" in summary["promotion_gate"]["blocking_reasons"]
    assert "one_or_more_runs_below_500_reviews" in summary["promotion_gate"]["blocking_reasons"]
    assert summary["aggregate"]["final_non_200_pages_total"] == 1


def test_web_catalog_ingestion_markdown_summary_includes_gate(tmp_path):
    path = tmp_path / "run" / "daily_report.json"
    write_web_ingestion_report(path, run_id="run-1", reviews=500)
    summary = summarize_web_ingestion_history_from_reports([path], min_runs=1, full_single_app_only=True)

    markdown = render_web_ingestion_markdown_summary(summary)

    assert "Promotion status: **ready_for_controlled_promotion**" in markdown
    assert "| run-1 |" in markdown


def test_web_catalog_ingestion_history_falls_back_to_raw_fetch_report(tmp_path):
    run_id = "run-1"
    daily_report = {
        "run_id": run_id,
        "source": WEB_CATALOG_SOURCE,
        "target_count": 1,
        "scope_count": 1,
        "target_offset": 10,
        "max_pages_per_app_country": 25,
        "review_limit": 20,
        "fetch_summary": {
            "pages": 25,
            "reviews": 500,
            "unique_reviews": 500,
            "fetch_errors": 0,
        },
        "load_summary": {"inserted": 500, "updated": 0, "duplicates_skipped": 0},
    }
    daily_path = tmp_path / "artifact" / "reports" / "apple_web_catalog" / run_id / "daily_report.json"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text(json.dumps(daily_report), encoding="utf-8")
    raw_path = tmp_path / "artifact" / "raw" / "apple_web_catalog" / run_id / "fetch_report.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(
        json.dumps(
            {
                "page_reports": [
                    {
                        "status": "ok",
                        "status_code": 200,
                        "attempt_count": 1,
                        "terminal_reason": "page_cap",
                        "missing_text_count": 0,
                        "missing_rating_count": 0,
                    }
                ]
                * 25,
                "review_count": 500,
                "unique_review_count": 500,
                "fetch_errors": 0,
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_web_ingestion_history_from_reports([daily_path], min_runs=1, full_single_app_only=True)

    assert summary["promotion_gate"]["status"] == "ready_for_controlled_promotion"
    assert summary["aggregate"]["status_code_counts"] == {"200": 25}
    assert summary["runs"][0]["all_pages_ok_after_retry"] is True


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


def test_apptweak_reviews_url_headers_and_payload_summary():
    url = build_apptweak_reviews_url(
        "284882215",
        country="US",
        language="US",
        device="iphone",
        limit=500,
        offset=500,
        start_date="2026-06-01",
        end_date="2026-06-18",
        term="crash",
    )

    assert url.startswith("https://public-api.apptweak.com/api/public/store/apps/reviews/search.json?")
    assert "apps=284882215" in url
    assert "country=us" in url
    assert "language=us" in url
    assert "device=iphone" in url
    assert "limit=500" in url
    assert "offset=500" in url
    assert "start_date=2026-06-01" in url
    assert "end_date=2026-06-18" in url
    assert "term=crash" in url
    assert apptweak_headers("secret-token") == {
        "Accept": "application/json",
        "X-Apptweak-Key": "secret-token",
    }

    payload = {
        "metadata": {"content": {"total_size": 2500}},
        "content": [
            {
                "id": "review-a",
                "author": "author-a",
                "title": "Useful",
                "rating": 5,
                "content": "Works well",
                "date": "2026-06-17",
                "version": "1.0",
            },
            {
                "id": "review-b",
                "author": "author-b",
                "title": "Bug",
                "rating": 2,
                "date": "2026-06-18",
                "version": "1.1",
            },
        ],
    }

    summary = parse_apptweak_reviews_payload(payload)

    assert summary["review_count"] == 2
    assert summary["total_reviews"] == 2500
    assert summary["missing_content_count"] == 1
    assert summary["min_date"] == "2026-06-17"
    assert summary["max_date"] == "2026-06-18"
    assert len(summary["review_fingerprints"]) == 2
    assert summary["review_fingerprints"][0] != summary["review_fingerprints"][1]


def test_appfigures_urls_headers_and_payload_summaries():
    lookup_url = build_appfigures_product_lookup_url("284882215")
    reviews_url = build_appfigures_reviews_url(
        "123456",
        country="US",
        page=2,
        count=500,
        sort="-date",
        start_date="2026-06-01",
        end_date="2026-06-18",
        lang="en",
        stars="1,5",
    )

    assert lookup_url == "https://api.appfigures.com/v2/products/apple/284882215"
    assert reviews_url.startswith("https://api.appfigures.com/v2/reviews?")
    assert "products=123456" in reviews_url
    assert "countries=us" in reviews_url
    assert "page=2" in reviews_url
    assert "count=500" in reviews_url
    assert "sort=-date" in reviews_url
    assert "start=2026-06-01" in reviews_url
    assert "end=2026-06-18" in reviews_url
    assert "lang=en" in reviews_url
    assert "stars=1%2C5" in reviews_url
    assert appfigures_headers("pat_test")["Authorization"] == "Bearer pat_test"

    product_summary = parse_appfigures_product_payload(
        {
            "id": 123456,
            "name": "Fixture",
            "developer": "Developer",
            "ref_no": "284882215",
            "store": "apple",
            "vendor_identifier": "284882215",
        }
    )
    assert product_summary["product_id"] == 123456
    assert product_summary["ref_no"] == 284882215

    reviews_summary = parse_appfigures_reviews_payload(
        {
            "total": 1200,
            "pages": 3,
            "this_page": 1,
            "reviews": [
                {
                    "id": "review-a",
                    "author": "author-a",
                    "title": "Useful",
                    "review": "Works well",
                    "stars": "5.00",
                    "iso": "US",
                    "version": "1.0",
                    "date": "2026-06-17T12:00:00",
                },
                {
                    "id": "review-b",
                    "title": "Bug",
                    "stars": "2.00",
                    "iso": "US",
                    "date": "2026-06-18T12:00:00",
                },
            ],
        }
    )
    assert reviews_summary["total_reviews"] == 1200
    assert reviews_summary["total_pages"] == 3
    assert reviews_summary["page"] == 1
    assert reviews_summary["review_count"] == 2
    assert reviews_summary["missing_content_count"] == 1
    assert reviews_summary["min_date"] == "2026-06-17T12:00:00"
    assert reviews_summary["max_date"] == "2026-06-18T12:00:00"
    assert len(reviews_summary["review_fingerprints"]) == 2
    assert reviews_summary["review_fingerprints"][0] != reviews_summary["review_fingerprints"][1]


def test_appfigures_probe_lookup_then_reviews():
    session = FakeSession(
        [
            {"id": 123456, "name": "Fixture", "ref_no": "123456789", "store": "apple"},
            {
                "total": 2,
                "pages": 1,
                "this_page": 1,
                "reviews": [
                    {"id": "review-a", "review": "Works", "date": "2026-06-17T12:00:00", "stars": "5.00"},
                    {"id": "review-b", "review": "Bug", "date": "2026-06-18T12:00:00", "stars": "2.00"},
                ],
            },
        ]
    )

    report = probe_appfigures_reviews_for_scope(
        fixture_target(),
        "us",
        session=session,
        access_token="pat_test",
        page_limit=2,
        request_limit=500,
        sort="-date",
        start_date=None,
        end_date=None,
        lang=None,
        stars=None,
        timeout_seconds=1,
        request_delay_seconds=0,
        sleep_fn=lambda _seconds: None,
    )

    assert session.calls[0]["url"] == "https://api.appfigures.com/v2/products/apple/123456789"
    assert session.calls[1]["url"].startswith("https://api.appfigures.com/v2/reviews?")
    assert session.calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer pat_test"
    assert report["provider_product_id"] == 123456
    assert report["country"] == "us"
    assert report["review_count"] == 2
    assert report["total_reviews"] == 2
    assert report["status_counts"] == {"lookup_200": 1, "200": 1}


def test_provider_comparison_replacement_gate():
    rss_report = {
        "page_reports": [{"app_id": "123", "status": "ok", "review_count": 50}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 100,
        "unique_review_count": 100,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    provider_report = {
        "settings": {"page_limit": 2, "request_limit": 100},
        "summary": {
            "reviews_seen": 125,
            "page_success_rate": 1.0,
            "status_counts": {"200": 2},
        }
    }

    summary = summarize_provider_comparison(rss_report, provider_report)

    assert summary["provider_reviews_minus_rss_reviews"] == 25
    assert summary["provider_to_rss_review_ratio"] == 1.25
    assert summary["provider_reviews_same_order_as_rss"] is True
    assert summary["provider_reviews_at_or_above_rss"] is True
    assert summary["provider_non_200_page_count"] == 0
    assert summary["provider_all_pages_ok"] is True
    assert summary["candidate_passes_same_order_stability_gate"] is True
    assert summary["candidate_passes_replacement_gate"] is True
    assert summary["provider_configured_review_ceiling"] is None
    assert summary["provider_reported_total_reviews"] is None


def test_provider_comparison_blocks_non_200_pages():
    rss_report = {
        "page_reports": [{"app_id": "123", "status": "ok", "review_count": 50}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 100,
        "unique_review_count": 100,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    provider_report = {
        "summary": {
            "reviews_seen": 200,
            "page_success_rate": 0.5,
            "status_counts": {"200": 1, "429": 1},
        }
    }

    summary = summarize_provider_comparison(rss_report, provider_report)

    assert summary["provider_reviews_at_or_above_rss"] is True
    assert summary["provider_non_200_page_count"] == 1
    assert summary["provider_all_pages_ok"] is False
    assert summary["candidate_passes_same_order_stability_gate"] is False
    assert summary["candidate_passes_replacement_gate"] is False


def test_provider_comparison_flags_configuration_limited_gap():
    rss_report = {
        "page_reports": [{"app_id": "123", "status": "ok", "review_count": 500}],
        "fetched_pages": 1,
        "fetch_errors": 0,
        "empty_pages": 0,
        "sparse_empty_pages": 0,
        "review_count": 500,
        "unique_review_count": 500,
        "warning_scopes": [],
        "capped_scopes": [],
    }
    provider_report = {
        "settings": {"page_limit": 2, "request_limit": 100},
        "summary": {
            "reviews_seen": 200,
            "page_success_rate": 1.0,
            "status_counts": {"200": 2},
        },
        "results": [
            {
                "app_id": "123",
                "pages": [{"page": 1}, {"page": 2}],
                "review_count": 200,
                "total_reviews": 900,
                "status_counts": {"200": 2},
            }
        ],
    }

    summary = summarize_provider_comparison(rss_report, provider_report)

    assert summary["provider_reviews_at_or_above_rss"] is False
    assert summary["provider_configured_review_ceiling"] == 200
    assert summary["provider_configured_ceiling_hit"] is True
    assert summary["provider_pages_per_row_needed_for_rss_parity"] == 5
    assert summary["provider_additional_pages_per_row_needed_for_rss_parity"] == 3
    assert summary["provider_page_depth_can_reach_rss_parity"] is False
    assert summary["provider_reported_total_reviews"] == 900
    assert summary["provider_reported_total_reviews_at_or_above_rss"] is True
    assert summary["provider_rows_with_more_available"] == 1
    assert summary["provider_reported_reviews_remaining"] == 700
    assert summary["provider_volume_gap_likely_configuration_limited"] is True


def test_provider_matrix_decision_needs_secret():
    decision = build_source_decision(
        {
            "providers": [
                {"provider": "42matters", "secret_env": "APP_STORE_42MATTERS_TOKEN", "configured": False, "status": "missing_secret"},
                {"provider": "apptweak", "secret_env": "APP_STORE_APPTWEAK_TOKEN", "configured": False, "status": "missing_secret"},
            ]
        }
    )

    assert decision["status"] == "needs_provider_secret"
    assert decision["selected_provider"] is None
    assert decision["missing_secret_envs"] == ["APP_STORE_42MATTERS_TOKEN", "APP_STORE_APPTWEAK_TOKEN"]


def test_provider_matrix_decision_selects_replacement_candidate():
    decision = build_source_decision(
        {
            "providers": [
                {
                    "provider": "42matters",
                    "configured": True,
                    "status": "success",
                    "candidate_passes_replacement_gate": True,
                    "provider_to_rss_review_ratio": 1.2,
                },
                {
                    "provider": "apptweak",
                    "configured": True,
                    "status": "success",
                    "candidate_passes_replacement_gate": True,
                    "provider_to_rss_review_ratio": 1.8,
                },
            ]
        }
    )

    assert decision["status"] == "replacement_candidate_found"
    assert decision["selected_provider"] == "apptweak"
    assert decision["replacement_candidate_count"] == 2


def test_provider_matrix_decision_requests_deeper_run():
    decision = build_source_decision(
        {
            "providers": [
                {
                    "provider": "appfigures",
                    "configured": True,
                    "status": "success",
                    "candidate_passes_replacement_gate": False,
                    "provider_to_rss_review_ratio": 0.6,
                    "provider_volume_gap_likely_configuration_limited": True,
                    "provider_additional_pages_per_row_needed_for_rss_parity": 3,
                }
            ]
        }
    )

    assert decision["status"] == "needs_deeper_provider_run"
    assert decision["selected_provider"] == "appfigures"


def test_provider_matrix_markdown_report_lists_missing_secrets():
    matrix = {
        "configured_provider_count": 0,
        "successful_provider_count": 0,
        "failed_provider_count": 0,
        "missing_secret_provider_count": 1,
        "providers": [
            {
                "provider": "42matters",
                "secret_env": "APP_STORE_42MATTERS_TOKEN",
                "configured": False,
                "status": "missing_secret",
            }
        ],
    }
    matrix["source_decision"] = build_source_decision(matrix)

    report = render_markdown_report(matrix)

    assert "Decision: **needs_provider_secret**" in report
    assert "`APP_STORE_42MATTERS_TOKEN`" in report
    assert "| 42matters | missing_secret | no |" in report


def test_provider_matrix_markdown_report_formats_success_metrics():
    matrix = {
        "configured_provider_count": 1,
        "successful_provider_count": 1,
        "failed_provider_count": 0,
        "missing_secret_provider_count": 0,
        "providers": [
            {
                "provider": "apptweak",
                "configured": True,
                "status": "success",
                "candidate_passes_replacement_gate": True,
                "provider_to_rss_review_ratio": 1.23456,
                "provider_all_pages_ok": True,
                "provider_volume_gap_likely_configuration_limited": False,
                "comparison_report_path": "data/reports/provider_apptweak_comparison/report.json",
            }
        ],
    }
    matrix["source_decision"] = build_source_decision(matrix)

    report = render_markdown_report(matrix)

    assert "Decision: **replacement_candidate_found**" in report
    assert "Selected provider: `apptweak`" in report
    assert "| apptweak | success | yes | yes | 1.235 | yes | no |" in report


def test_provider_comparison_per_app_summary():
    rss_report = {
        "page_reports": [
            {"app_id": "123", "status": "ok", "review_count": 50},
            {"app_id": "123", "status": "ok", "review_count": 25},
            {"app_id": "456", "status": "error", "review_count": 0},
        ]
    }
    provider_report = {
        "settings": {"page_limit": 2, "request_limit": 50},
        "results": [
            {
                "app_id": "123",
                "app_name": "Fixture",
                "category": "shopping",
                "pages": [{"page": 1}, {"page": 2}],
                "review_count": 100,
                "status_counts": {"200": 2},
                "total_reviews": 300,
                "min_date": "2026-06-01",
                "max_date": "2026-06-18",
            }
        ]
    }

    rows = compare_provider_per_app(rss_report, provider_report)

    assert rows == [
        {
            "app_id": "123",
            "app_name": "Fixture",
            "category": "shopping",
            "country": None,
            "rss_page_count": 2,
            "rss_fetch_errors": 0,
            "rss_review_count": 75,
            "provider_page_count": 2,
            "provider_review_count": 100,
            "provider_to_rss_review_ratio": 100 / 75,
            "provider_status_counts": {"200": 2},
            "provider_total_reviews": 300,
            "provider_reported_reviews_remaining": 200,
            "provider_more_available": True,
            "provider_configured_review_ceiling": 100,
            "provider_configured_ceiling_hit": True,
            "provider_reviews_at_or_above_rss": True,
            "provider_min_date": "2026-06-01",
            "provider_max_date": "2026-06-18",
        }
    ]


def test_provider_comparison_uses_country_scope_when_available():
    rss_report = {
        "page_reports": [
            {"app_id": "123", "country": "us", "status": "ok", "review_count": 50},
            {"app_id": "123", "country": "ca", "status": "ok", "review_count": 20},
        ]
    }
    provider_report = {
        "results": [
            {
                "app_id": "123",
                "app_name": "Fixture",
                "category": "shopping",
                "country": "ca",
                "pages": [{"page": 1}],
                "review_count": 30,
                "status_counts": {"200": 1},
            }
        ]
    }

    rows = compare_provider_per_app(rss_report, provider_report)

    assert rows[0]["country"] == "ca"
    assert rows[0]["rss_page_count"] == 1
    assert rows[0]["rss_review_count"] == 20
    assert rows[0]["provider_review_count"] == 30
    assert rows[0]["provider_to_rss_review_ratio"] == 1.5
