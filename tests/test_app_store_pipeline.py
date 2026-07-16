from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

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
from app_store_review_pipeline.cli import (
    command_check_web_429_circuit_breaker,
    command_check_web_429_cooldown,
    command_select_web_catalog_pressure,
    command_daily_web_catalog,
    command_fallback_monitoring_report,
    command_monitoring_report,
    command_send_monitoring_email,
    command_operating_ledger_upsert_run,
    command_operating_report,
    select_target_window,
    summarize_fetch_cli,
)
from app_store_review_pipeline.config import WEB_CATALOG_SOURCE
from app_store_review_pipeline.eda import add_rates, calculate_concentration, render_eda_html, render_eda_markdown
from app_store_review_pipeline.experiment_groups import build_daily_matrix_rows
from app_store_review_pipeline.fetcher import fetch_targets, terminal_reason_for_page
from app_store_review_pipeline.models import AppTarget, ReviewPage
from app_store_review_pipeline.monitoring import (
    evaluate_alerts,
    extract_jobs,
    extract_runs,
    fetch_stale_apps,
    is_monitor_job,
    monitor_exit_code,
    overall_status,
    render_monitoring_markdown,
    summarize_github_payloads,
)
from app_store_review_pipeline.notifications import (
    build_monitoring_notification,
    parse_recipients,
    send_monitoring_email,
    write_fallback_failure_report,
)
from app_store_review_pipeline.operating import (
    build_aggregate_summary,
    build_depth_audit_findings,
    build_experiment_findings,
    build_metric_windows,
    build_operating_recommendation,
    is_source_pressure_clean_run,
    load_operating_ledger,
    schedule_delay_minutes,
)
from app_store_review_pipeline import postgres_database
from app_store_review_pipeline.postgres_database import (
    infer_field_value,
    mask_database_url,
    review_changed_fields,
    scope_key,
    scope_outcome,
)
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
from scripts.summarize_source_coverage import choose_next_web_catalog_scope, summarize_scope_records
from scripts.summarize_web_catalog_ingestion import (
    render_markdown_summary as render_web_ingestion_markdown_summary,
    summarize_web_catalog_depth_rows,
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


def test_initialize_postgres_serializes_schema_creation(monkeypatch):
    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))
            return FakeResult(params[0] if params and "RETURNING version" in query else None)

        def commit(self):
            calls.append(("commit", None))

    class FakeResult:
        def __init__(self, version):
            self.version = version

        def fetchone(self):
            return {"version": self.version} if self.version else None

    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    postgres_database.initialize_postgres("postgresql:///fixture")

    assert calls[0] == (
        "SELECT pg_advisory_xact_lock(%s)",
        (postgres_database.POSTGRES_SCHEMA_ADVISORY_LOCK_ID,),
    )
    assert calls[1] == (postgres_database.POSTGRES_SCHEMA, None)
    assert any(call[0].startswith("ALTER TABLE app_store_pressure_state") for call in calls)
    assert calls[-1] == ("commit", None)


def test_insert_pages_keeps_typed_timestamp_placeholders_aligned():
    class FakeResult:
        rowcount = 1

    class FakeConnection:
        def execute(self, query, params=None):
            assert query.count("%s") == len(params or ())
            return FakeResult()

    row = {
        "page_key": "run:source:123:us:recent:1",
        "run_id": "run",
        "platform": "apple_app_store",
        "source": WEB_CATALOG_SOURCE,
        "app_id": "123",
        "app_name": "Fixture",
        "country": "us",
        "sort_by": "recent",
        "page_number": 1,
        "request_url": "https://example.test",
        "status": "ok",
        "status_code": 200,
        "fetched_at": "2026-07-16T12:00:00Z",
        "raw_json_path": "/tmp/page.json",
        "review_count": 1,
        "unique_review_count": 1,
        "terminal_reason": "caught_up_to_existing_reviews",
    }

    postgres_database.insert_pages(FakeConnection(), [row])


def test_sync_state_typed_timestamp_placeholders_are_aligned(monkeypatch):
    class FakeResult:
        def __init__(self, row=None):
            self.row = row
            self.rowcount = 1

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            assert query.count("%s") == len(params or ())
            if "SELECT complete_through_updated_epoch_seconds" in query:
                return FakeResult(None)
            if "SELECT COALESCE(MAX(updated_epoch_seconds)" in query:
                return FakeResult({"high_water": 123})
            return FakeResult()

        def commit(self):
            return None

    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())
    result = postgres_database._update_sync_states_postgres_once(
        "postgresql:///fixture",
        {
            ("123", "us", "recent"): [
                {
                    "app_id": "123",
                    "country": "us",
                    "sort_by": "recent",
                    "terminal_reason": "caught_up_to_existing_reviews",
                    "overlap_review_count": 1,
                }
            ]
        },
        {("123", "us", "recent"): [{"review_id": "1"}]},
        run_id="run",
        sort_by="recent",
        started_at="2026-07-16T12:00:00Z",
        completed_at="2026-07-16T12:01:00Z",
        source=WEB_CATALOG_SOURCE,
    )

    assert result["scope_count"] == 1
    assert result["scopes"][0]["high_water"] == 123


def test_scope_outcome_and_review_change_fields_are_explicit():
    assert scope_outcome(terminal_reason="caught_up_to_existing_reviews", page_count=1, fetch_errors=0, other_non_200_pages=0) == "caught_up"
    assert scope_outcome(terminal_reason="page_cap", page_count=2, fetch_errors=0, other_non_200_pages=0) == "backlogged"
    assert scope_outcome(terminal_reason=None, page_count=0, fetch_errors=0, other_non_200_pages=0) == "hard_failure"
    assert review_changed_fields(
        {"title": "Before", "content": "Same", "rating": 5},
        {"title": "After", "content": "Same", "rating": 5},
    ) == ["title"]


def test_load_pipeline_run_postgres_retries_retryable_write_conflicts(monkeypatch, tmp_path):
    raw_dir = tmp_path / "run-1"
    raw_dir.mkdir()
    (raw_dir / "review_pages.jsonl").write_text("", encoding="utf-8")
    (raw_dir / "reviews.jsonl").write_text("", encoding="utf-8")
    targets_path = tmp_path / "targets.csv"
    targets_path.write_text(
        "app_name,category,apple_app_id,apple_slug,countries,active,notes\n"
        "Fixture,shopping,123,fixture,us,true,\n",
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: calls.append("initialize"))
    monkeypatch.setattr(postgres_database.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    def fake_load_once(**kwargs):
        calls.append(("load_once", kwargs["run_id"]))
        if sum(1 for call in calls if call == ("load_once", "run-1")) == 1:
            raise postgres_database.psycopg.errors.DeadlockDetected("deadlock detected")
        return {
            "run_id": kwargs["run_id"],
            "page_rows": 0,
            "review_rows": 0,
            "inserted": 0,
            "updated": 0,
            "duplicates_skipped": 0,
            "fetch_errors": 0,
            "capped_scopes": 0,
        }

    monkeypatch.setattr(postgres_database, "_load_pipeline_run_postgres_once", fake_load_once)

    summary = postgres_database.load_pipeline_run_postgres("postgresql:///fixture", raw_dir, targets_path)

    assert summary["run_id"] == "run-1"
    assert calls == ["initialize", ("load_once", "run-1"), ("sleep", 2.0), ("load_once", "run-1")]


def test_update_sync_states_postgres_retries_retryable_write_conflicts(monkeypatch):
    calls = []
    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: calls.append("initialize"))
    monkeypatch.setattr(postgres_database.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    def fake_update_once(*args, **kwargs):
        calls.append(("update_once", kwargs["run_id"]))
        if sum(1 for call in calls if call == ("update_once", "run-1")) == 1:
            raise postgres_database.psycopg.errors.DeadlockDetected("deadlock detected")
        return {"scope_count": 1, "scopes": []}

    monkeypatch.setattr(postgres_database, "_update_sync_states_postgres_once", fake_update_once)

    summary = postgres_database.update_sync_states_postgres(
        "postgresql:///fixture",
        [
            {
                "app_id": "123",
                "country": "us",
                "sort_by": "recent",
                "terminal_reason": "caught_up_to_existing_reviews",
            }
        ],
        [],
        run_id="run-1",
        sort_by="recent",
        started_at="2026-06-30T00:00:00Z",
        completed_at="2026-06-30T00:01:00Z",
    )

    assert summary == {"scope_count": 1, "scopes": []}
    assert calls == ["initialize", ("update_once", "run-1"), ("sleep", 1.0), ("update_once", "run-1")]


def test_retryable_postgres_write_error_classifies_transient_conflicts():
    assert postgres_database.retryable_postgres_write_error(
        postgres_database.psycopg.errors.DeadlockDetected("deadlock detected")
    )
    assert postgres_database.retryable_postgres_write_error(
        postgres_database.psycopg.errors.SerializationFailure("could not serialize")
    )
    assert postgres_database.retryable_postgres_write_error(
        postgres_database.psycopg.errors.LockNotAvailable("lock not available")
    )
    assert not postgres_database.retryable_postgres_write_error(ValueError("not a postgres conflict"))


def test_web_catalog_429_circuit_breaker_trips_on_high_rate(monkeypatch):
    queries = []

    class FakeResult:
        def fetchone(self):
            return {
                "page_count": 4,
                "http_429_page_count": 3,
                "ok_page_count": 1,
                "error_page_count": 3,
                "first_page_at": "2026-06-20 00:00:00+00",
                "last_page_at": "2026-06-20 00:10:00+00",
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            return FakeResult()

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_429_circuit_breaker_status(
        "postgresql:///fixture",
        since="2026-06-20T00:00:00Z",
        min_pages=4,
        max_rate=0.5,
    )

    assert status["tripped"] is True
    assert status["page_count"] == 4
    assert status["http_429_page_count"] == 3
    assert status["http_429_rate"] == 0.75
    assert queries[0][1] == (WEB_CATALOG_SOURCE, "2026-06-20T00:00:00Z")


def test_web_catalog_429_circuit_breaker_waits_for_min_pages(monkeypatch):
    class FakeResult:
        def fetchone(self):
            return {
                "page_count": 3,
                "http_429_page_count": 3,
                "ok_page_count": 0,
                "error_page_count": 3,
                "first_page_at": None,
                "last_page_at": None,
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult()

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_429_circuit_breaker_status(
        "postgresql:///fixture",
        min_pages=4,
        max_rate=0.5,
    )

    assert status["tripped"] is False
    assert status["http_429_rate"] == 1.0


def test_trusted_existing_review_ids_use_successful_frontier(monkeypatch):
    queries = []

    class FakeResult:
        def fetchall(self):
            return [{"review_id": "old-review"}]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            return FakeResult()

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    result = postgres_database.trusted_existing_review_ids_by_scope(
        "postgresql:///fixture",
        [("123456789", "us", "recent")],
        source=WEB_CATALOG_SOURCE,
    )

    assert result == {("123456789", "us", "recent"): {"old-review"}}
    assert queries[0][1] == (
        "recent",
        WEB_CATALOG_SOURCE,
        "123456789",
        "us",
        "recent",
        WEB_CATALOG_SOURCE,
        "123456789",
        "us",
        "recent",
        WEB_CATALOG_SOURCE,
        "123456789",
        "us",
        WEB_CATALOG_SOURCE,
    )
    assert "s.last_successful_run_id" in queries[0][0]
    assert "inferred_incomplete" in queries[0][0]
    assert "COALESCE(BOOL_OR" in queries[0][0]
    assert "first_run.loaded_at <= trusted_success_run.loaded_at" in queries[0][0]
    assert "first_run.loaded_at < inferred_incomplete.loaded_at" in queries[0][0]


def test_check_web_429_circuit_breaker_command_returns_two_when_tripped(monkeypatch, capsys):
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.web_catalog_429_circuit_breaker_status",
        lambda *args, **kwargs: {
            "source": WEB_CATALOG_SOURCE,
            "page_count": 4,
            "http_429_page_count": 4,
            "http_429_rate": 1.0,
            "tripped": True,
        },
    )
    args = argparse.Namespace(
        database_url="postgresql:///fixture",
        source=WEB_CATALOG_SOURCE,
        since="2026-06-20T00:00:00Z",
        lookback_minutes=60,
        min_pages=4,
        max_rate=0.5,
    )

    assert command_check_web_429_circuit_breaker(args) == 2
    assert json.loads(capsys.readouterr().out)["tripped"] is True


def test_web_catalog_429_cooldown_trips_on_recent_429(monkeypatch):
    class FakeResult:
        def fetchone(self):
            return {
                "last_http_429_at": "2026-06-20 06:17:49+00",
                "minutes_since_last_http_429": 12.5,
                "http_429_count_in_cooldown": 4,
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult()

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_429_cooldown_status(
        "postgresql:///fixture",
        cooldown_minutes=720,
    )

    assert status["tripped"] is True
    assert status["minutes_since_last_http_429"] == 12.5
    assert status["http_429_count_in_cooldown"] == 4


def test_web_catalog_429_cooldown_allows_after_window(monkeypatch):
    class FakeResult:
        def fetchone(self):
            return {
                "last_http_429_at": "2026-06-19 12:00:00+00",
                "minutes_since_last_http_429": 900.0,
                "http_429_count_in_cooldown": 0,
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult()

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_429_cooldown_status(
        "postgresql:///fixture",
        cooldown_minutes=720,
    )

    assert status["tripped"] is False
    assert status["http_429_count_in_cooldown"] == 0


def test_check_web_429_cooldown_command_returns_two_when_tripped(monkeypatch, capsys):
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.web_catalog_429_cooldown_status",
        lambda *args, **kwargs: {
            "source": WEB_CATALOG_SOURCE,
            "cooldown_minutes": 720,
            "last_http_429_at": "2026-06-20 06:17:49+00",
            "minutes_since_last_http_429": 10.0,
            "http_429_count_in_cooldown": 4,
            "tripped": True,
        },
    )
    args = argparse.Namespace(
        database_url="postgresql:///fixture",
        source=WEB_CATALOG_SOURCE,
        cooldown_minutes=720,
    )

    assert command_check_web_429_cooldown(args) == 2
    assert json.loads(capsys.readouterr().out)["tripped"] is True


def test_web_catalog_pressure_uses_stored_next_page_cap_after_clean_recent_pages(monkeypatch):
    rows = [
        {
            "page_count": 22,
            "ok_page_count": 22,
            "error_page_count": 0,
            "http_429_page_count": 0,
            "final_non_200_page_count": 0,
            "retried_page_count": 0,
            "first_page_at": "2026-06-20 19:00:00+00",
            "last_page_at": "2026-06-20 20:00:00+00",
        },
        {
            "source": WEB_CATALOG_SOURCE,
            "next_max_pages_per_app_country": 12,
            "safe_max_pages_per_app_country": 12,
            "candidate_max_pages_per_app_country": 12,
            "safe_max_parallel": 2,
            "candidate_max_parallel": 3,
            "safe_scope_time_budget_seconds": 1800,
            "candidate_scope_time_budget_seconds": 1800,
            "clean_run_count": 3,
        },
    ]

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult(rows.pop(0))

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_pressure_status("postgresql:///fixture", base_pages=5, max_pages=25)

    assert status["clean_for_ramp"] is True
    assert status["reason"] == "stored_pressure_state"
    assert status["selected_max_pages_per_app_country"] == 12
    assert status["selected_max_parallel"] == 2
    assert status["candidate_max_parallel"] == 3


def test_eda_helpers_render_core_sections():
    rows = [{"review_count": 70}, {"review_count": 20}, {"review_count": 10}]
    assert calculate_concentration(rows) == {
        "review_count": 100,
        "app_count": 3,
        "top_1_share": 0.7,
        "top_5_share": 1.0,
        "top_10_share": 1.0,
        "hhi": 0.54,
    }
    assert add_rates({"review_count": 100, "missing_title": 25})["missing_title_rate"] == 0.25

    summary = {
            "metadata": {
                "generated_at": "2026-06-22T00:00:00+00:00",
                "database_url": "postgresql:///app_store_reviews",
                "source": WEB_CATALOG_SOURCE,
            },
            "inventory": {
                "primary_source": {
                    "review_count": 100,
                    "app_count": 3,
                    "category_count": 2,
                    "country_count": 1,
                },
                "rows_by_source": [{"source": WEB_CATALOG_SOURCE, "review_count": 100, "app_count": 3}],
            },
            "volume": {
                "concentration": calculate_concentration(rows),
                "by_app_top_50": [{"app_name": "Fixture", "category": "shopping", "review_count": 70}],
                "by_category": [{"category": "shopping", "app_count": 1, "review_count": 70}],
            },
            "ratings": {
                "overall": [{"rating": 5, "review_count": 60}],
                "by_category": [{"category": "shopping", "review_count": 70, "avg_rating": 4.5}],
            },
            "text_quality": {
                "length_summary": {"review_count": 100, "avg_chars": 120},
                "low_signal": {"review_count": 100, "blank_content": 0},
                "duplicate_summary": {
                    "review_count": 100,
                    "distinct_review_keys": 100,
                    "normalized_duplicate_group_count": 1,
                },
                "duplicate_examples": [{"row_count": 2, "app_count": 1, "sample": "great app"}],
            },
            "time_coverage": {
                "monthly_recent_24": [{"month": "2026-06", "review_count": 100, "app_count": 3}],
                "freshness_by_app_stalest_50": [{"app_name": "Fixture", "category": "shopping", "review_count": 70}],
            },
            "missingness": {"review_count": 100, "missing_title": 25},
            "pipeline_behavior": {
                "runs_summary": {"run_count": 1, "page_count": 5},
                "status_codes": [{"status_code": "200", "page_count": 5}],
                "terminal_reasons": [{"terminal_reason": "none", "page_count": 5}],
                "attempt_counts": [{"attempt_count": 1, "page_count": 5}],
                "empty_and_error_pages": {
                    "empty_pages": 0,
                    "empty_pages_with_next_link": 0,
                    "empty_pages_without_next_link": 0,
                    "http_429_pages": 0,
                    "final_non_200_pages": 0,
                    "retried_pages": 0,
                    "error_pages": 0,
                },
                "by_app_top_50_pages": [{"app_name": "Fixture", "category": "shopping", "page_count": 5}],
            },
        }
    markdown = render_eda_markdown(summary)
    html = render_eda_html(summary)

    assert "# Apple App Store Review Data Quality Report" in markdown
    assert "## Pipeline Behavior" in markdown
    assert "Fixture" in markdown
    assert "Apple App Store Review Data Quality Dashboard" in html
    assert "categoryVolume" in html


def test_operating_schedule_delay_uses_previous_cron_slot():
    created_at = datetime(2026, 6, 28, 7, 7, 53, tzinfo=timezone.utc)

    assert schedule_delay_minutes(created_at) == 240.88


def test_load_operating_ledger_supports_missing_and_legacy_list(tmp_path):
    missing_path = tmp_path / "missing.json"
    assert load_operating_ledger(missing_path)["runs"] == []

    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps([{"github_run_id": "1"}]), encoding="utf-8")
    assert load_operating_ledger(legacy_path)["runs"] == [{"github_run_id": "1"}]


def test_operating_metric_windows_do_not_overlap_adjacent_runs():
    runs = [
        {
            "github_run_id": "cap",
            "created_at": "2026-06-30T22:39:16Z",
            "updated_at": "2026-06-30T22:42:03Z",
        },
        {
            "github_run_id": "audit",
            "created_at": "2026-06-30T22:43:05Z",
            "updated_at": "2026-06-30T22:46:04Z",
        },
    ]

    windows = build_metric_windows(runs, grace_minutes=5)

    assert windows[0]["end"] == datetime(2026, 6, 30, 22, 43, 4, 999999, tzinfo=timezone.utc)
    assert windows[1]["end"] == datetime(2026, 6, 30, 22, 51, 4, tzinfo=timezone.utc)


def test_operating_source_pressure_clean_allows_intentional_page_caps():
    run = {
        "page_metrics": {"page_count": 25, "http_429_rate": 0},
        "load_metrics": {"fetch_errors": 0, "capped_scopes": 1},
    }

    assert is_source_pressure_clean_run(run) is True


def test_operating_experiment_findings_summarize_completed_f1():
    runs = [
        {
            "comparison_group": "F1_six_hour_full_scope",
            "conclusion": "success",
            "runtime_minutes": 38.45,
            "page_metrics": {
                "page_count": 283,
                "review_rows": 5640,
                "http_429_pages": 0,
                "other_non_200_pages": 1,
                "retried_pages": 11,
            },
            "load_metrics": {
                "reviews_inserted": 2826,
                "duplicates_skipped": 2811,
                "fetch_errors": 1,
                "capped_scopes": 0,
            },
        }
    ]
    experiments = [
        {
            "experiment_id": "F1",
            "status": "completed",
            "comparison_group": "F1_six_hour_full_scope",
        }
    ]

    findings = build_experiment_findings(runs, experiments)

    assert findings[0]["experiment_id"] == "F1"
    assert findings[0]["inserted"] == 2826
    assert findings[0]["http_429_rate"] == 0
    assert findings[0]["fetch_error_rate"] < 0.01
    assert "Clean" in findings[0]["finding"]


def test_operating_source_pressure_clean_counts_artifact_only_failure():
    runs = [
        {
            "comparison_group": "F2_three_hour_full_scope",
            "conclusion": "failure",
            "runtime_minutes": 41.35,
            "page_metrics": {
                "page_count": 203,
                "review_rows": 4060,
                "http_429_pages": 0,
                "http_429_rate": 0,
                "other_non_200_pages": 0,
                "retried_pages": 14,
            },
            "load_metrics": {
                "reviews_inserted": 136,
                "duplicates_skipped": 3924,
                "fetch_errors": 0,
                "capped_scopes": 0,
            },
        }
    ]
    experiments = [
        {
            "experiment_id": "F2",
            "status": "completed_source_clean_github_artifact_failure",
            "comparison_group": "F2_three_hour_full_scope",
        }
    ]

    findings = build_experiment_findings(runs, experiments)
    aggregate = build_aggregate_summary(runs)

    assert findings[0]["successful_run_count"] == 0
    assert findings[0]["source_pressure_clean_run_count"] == 1
    assert "Source-clean" in findings[0]["finding"]
    assert aggregate["successful_run_count"] == 0
    assert aggregate["source_pressure_clean_run_count"] == 1
    assert aggregate["source_pressure_clean_pages"] == 203


def test_operating_depth_audit_rejects_caps_that_miss_too_many_rows():
    runs = [
        {
            "comparison_group": "D1_one_page_cap",
            "conclusion": "success",
            "page_metrics": {"page_count": 200, "http_429_pages": 0},
            "load_metrics": {"reviews_inserted": 1000},
        },
        {
            "comparison_group": "D1_one_page_uncapped_audit",
            "conclusion": "success",
            "page_metrics": {"page_count": 40, "http_429_pages": 0},
            "load_metrics": {"reviews_inserted": 100},
        },
    ]
    experiments = [
        {
            "experiment_id": "D1",
            "comparison_group": "D1_one_page_cap",
            "audit_comparison_group": "D1_one_page_uncapped_audit",
            "audit_missed_insert_threshold": 0.05,
        }
    ]

    findings = build_depth_audit_findings(runs, experiments)

    assert findings[0]["missed_insert_rate_vs_uncapped_audit"] == 0.0909
    assert "Rejected" in findings[0]["finding"]


def test_operating_experiment_finding_marks_rejected_strategy():
    runs = [
        {
            "comparison_group": "D1_one_page_cap",
            "conclusion": "success",
            "runtime_minutes": 2.78,
            "page_metrics": {
                "page_count": 25,
                "review_rows": 500,
                "http_429_pages": 0,
                "http_429_rate": 0,
            },
            "load_metrics": {
                "reviews_inserted": 0,
                "duplicates_skipped": 500,
                "fetch_errors": 0,
                "capped_scopes": 1,
            },
        }
    ]
    experiments = [
        {
            "experiment_id": "D1",
            "status": "completed_rejected",
            "comparison_group": "D1_one_page_cap",
        }
    ]

    findings = build_experiment_findings(runs, experiments)

    assert "rejected" in findings[0]["finding"]


def test_operating_grouped_frequency_finding_tracks_pending_treatment():
    runs = [
        {
            "github_run_id": "seed",
            "comparison_group": "FG2_three_hour_grouped_frequency",
            "experiment_group": "om_group_04",
            "conclusion": "success",
            "created_at": "2026-06-30T23:11:44Z",
            "updated_at": "2026-06-30T23:14:50Z",
            "runtime_minutes": 3.1,
            "page_metrics": {
                "page_count": 27,
                "review_rows": 540,
                "http_429_pages": 0,
                "http_429_rate": 0,
            },
            "load_metrics": {
                "reviews_inserted": 107,
                "duplicates_skipped": 433,
                "fetch_errors": 0,
                "capped_scopes": 0,
            },
        }
    ]
    experiments = [
        {
            "experiment_id": "FG2",
            "status": "seed_completed",
            "comparison_group": "FG2_three_hour_grouped_frequency",
            "experiment_group": "om_group_04",
        }
    ]

    findings = build_experiment_findings(runs, experiments)

    assert findings[0]["frequency_isolation_status"] == "pending_treatment"
    assert findings[0]["seed_run_id"] == "seed"
    assert findings[0]["treatment_run_id"] == ""
    assert findings[0]["contaminating_run_count"] == 0
    assert "Treatment is pending" in findings[0]["finding"]


def test_operating_grouped_frequency_finding_detects_full_scope_contamination():
    runs = [
        {
            "github_run_id": "seed",
            "comparison_group": "FG2_three_hour_grouped_frequency",
            "experiment_group": "om_group_04",
            "conclusion": "success",
            "created_at": "2026-06-30T23:11:44Z",
            "updated_at": "2026-06-30T23:14:50Z",
            "runtime_minutes": 3.1,
            "page_metrics": {"page_count": 27, "review_rows": 540, "http_429_pages": 0, "http_429_rate": 0},
            "load_metrics": {"reviews_inserted": 107, "duplicates_skipped": 433, "fetch_errors": 0},
        },
        {
            "github_run_id": "full-scope",
            "comparison_group": "F0_twice_daily_baseline",
            "conclusion": "success",
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:45:00Z",
            "job_total": 202,
            "page_metrics": {"page_count": 260, "http_429_pages": 0, "http_429_rate": 0},
            "load_metrics": {"reviews_inserted": 100, "duplicates_skipped": 5100, "fetch_errors": 0},
        },
        {
            "github_run_id": "treatment",
            "comparison_group": "FG2_three_hour_grouped_frequency",
            "experiment_group": "om_group_04",
            "conclusion": "success",
            "created_at": "2026-07-01T02:15:00Z",
            "updated_at": "2026-07-01T02:18:00Z",
            "runtime_minutes": 3.0,
            "page_metrics": {"page_count": 25, "review_rows": 500, "http_429_pages": 0, "http_429_rate": 0},
            "load_metrics": {"reviews_inserted": 1, "duplicates_skipped": 499, "fetch_errors": 0},
        },
    ]
    experiments = [
        {
            "experiment_id": "FG2",
            "status": "completed",
            "comparison_group": "FG2_three_hour_grouped_frequency",
            "experiment_group": "om_group_04",
        }
    ]

    findings = build_experiment_findings(runs, experiments)

    assert findings[0]["frequency_isolation_status"] == "contaminated"
    assert findings[0]["contaminating_run_count"] == 1
    assert findings[0]["contaminating_run_ids"] == "full-scope"
    assert "Contaminated" in findings[0]["finding"]


def test_operating_recommendation_finalizes_after_grouped_tests():
    aggregate = {
        "successful_http_429_rate": 0,
        "successful_baseline_run_count": 3,
    }
    app_segments = {
        "segments": [
            {
                "segment": "high",
                "insert_share": 0.72,
                "page_share": 0.52,
            }
        ]
    }
    ledger = {
        "planned_experiments": [
            {"experiment_id": "FG1", "status": "completed_supported_for_hybrid_candidate"},
            {"experiment_id": "FG2", "status": "completed_rejected"},
            {"experiment_id": "D1", "status": "completed_rejected"},
            {"experiment_id": "D2", "status": "completed_rejected"},
        ]
    }
    experiment_findings = [
        {"experiment_id": "FG1", "inserted": 531, "page_count": 68},
        {"experiment_id": "FG2", "inserted": 107, "page_count": 52},
    ]
    depth_audit_findings = [
        {"experiment_id": "D1", "finding": "Rejected. The cap missed more than the configured audit threshold."},
        {"experiment_id": "D2", "finding": "Rejected. The cap missed more than the configured audit threshold."},
    ]

    recommendation = build_operating_recommendation(
        aggregate,
        app_segments,
        ledger,
        experiment_findings=experiment_findings,
        depth_audit_findings=depth_audit_findings,
    )

    assert recommendation["confidence"] == "ready_for_review"
    assert recommendation["pending_experiments"] == []
    assert "remaining controlled tests" not in recommendation["current_recommendation"]
    assert "twice-daily full-scope uncapped overlap-stop" in recommendation["current_recommendation"]
    assert "six-hour grouped refresh" in recommendation["current_recommendation"]


def test_command_operating_report_passes_paths(tmp_path, monkeypatch):
    observed = {}

    def fake_generate_operating_report(*args, **kwargs):
        observed["database_url"] = args[0]
        observed.update(kwargs)
        return {
            "database_url": "postgresql:///fixture",
            "generated_at": "2026-06-29T00:00:00+00:00",
            "observed_run_count": 1,
            "successful_baseline_run_count": 1,
        }

    monkeypatch.setattr("app_store_review_pipeline.cli.generate_operating_report", fake_generate_operating_report)
    args = argparse.Namespace(
        database_url="postgresql:///fixture",
        source=WEB_CATALOG_SOURCE,
        ledger=tmp_path / "ledger.json",
        markdown_output=tmp_path / "operating.md",
        json_output=tmp_path / "operating.json",
        grace_minutes=7,
    )

    assert command_operating_report(args) == 0
    assert observed["database_url"] == "postgresql:///fixture"
    assert observed["ledger_path"] == tmp_path / "ledger.json"
    assert observed["markdown_path"] == tmp_path / "operating.md"
    assert observed["json_path"] == tmp_path / "operating.json"
    assert observed["grace_minutes"] == 7


def monitoring_alert_status(
    *,
    run_metrics: dict | None = None,
    stale_apps: list[dict] | None = None,
    history: dict | None = None,
    github: dict | None = None,
    source_frontier: dict | None = None,
    accounting: dict | None = None,
    app_metrics: dict | None = None,
    selected_count: int = 200,
    workflow_result: str = "success",
    require_recent_scheduled_run: bool = False,
) -> tuple[str, list[dict]]:
    base_run_metrics = {
        "page_count": 100,
        "reviews_inserted": 50,
        "http_429_pages": 0,
        "http_429_rate": 0,
        "other_non_200_pages": 0,
        "non_200_rate": 0,
        "fetch_error_rate": 0,
        "retry_rate": 0,
        "duplicate_rate": 0.5,
        "backlog_terminal_rate": 0,
        "runtime_minutes": 30,
    }
    base_github = {
        "job_failure": 0,
        "recent_schedule_run_count": 1,
        "recent_failed_schedule_run_count": 0,
    }
    base_run_metrics.update(run_metrics or {})
    base_github.update(github or {})
    alerts = evaluate_alerts(
        run_metrics=base_run_metrics,
        app_metrics=app_metrics or {},
        source_frontier=source_frontier or {},
        accounting=accounting or {"consistent": True},
        stale_apps=stale_apps or [],
        history=history or {"median_inserted_per_execution": 0, "comparable_execution_count": 0},
        github=base_github,
        selected_count=selected_count,
        workflow_result=workflow_result,
        require_recent_scheduled_run=require_recent_scheduled_run,
    )
    return overall_status(alerts), alerts


def alert_codes(alerts: list[dict]) -> set[str]:
    return {alert["code"] for alert in alerts}


def test_monitoring_alerts_clean_run_is_healthy():
    status, alerts = monitoring_alert_status()

    assert status == "healthy"
    assert alert_codes(alerts) == {"all_clear"}


def test_monitoring_workflow_and_schedule_failures_are_failing():
    status, alerts = monitoring_alert_status(workflow_result="failure")
    assert status == "failing"
    assert "workflow_failure" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(
        github={"recent_schedule_run_count": 0},
        require_recent_scheduled_run=True,
    )
    assert status == "failing"
    assert "missing_scheduled_run" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(github={"recent_failed_schedule_run_count": 2})
    assert status == "failing"
    assert "repeated_scheduled_failures" in alert_codes(alerts)


def test_monitoring_http_429_thresholds_map_to_degraded_and_failing():
    status, alerts = monitoring_alert_status(run_metrics={"http_429_pages": 1, "http_429_rate": 0.004})
    assert status == "degraded"
    assert "http_429_present" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(run_metrics={"http_429_pages": 3, "http_429_rate": 0.03})
    assert status == "failing"
    assert "excessive_http_429" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(
        run_metrics={
            "http_429_pages": 0,
            "http_429_attempts": 1,
            "http_429_rate": 0.004,
        }
    )
    assert status == "degraded"
    assert "http_429_present" in alert_codes(alerts)


def test_monitoring_429_does_not_duplicate_other_non_200_warning():
    status, alerts = monitoring_alert_status(
        run_metrics={
            "http_429_pages": 1,
            "http_429_rate": 0.004,
            "other_non_200_pages": 0,
            "other_non_200_rate": 0,
        }
    )

    assert status == "degraded"
    assert alert_codes(alerts) == {"http_429_present"}


def test_monitoring_any_other_non_200_is_degraded():
    status, alerts = monitoring_alert_status(
        run_metrics={"other_non_200_pages": 2, "other_non_200_rate": 0.02}
    )

    assert status == "degraded"
    assert "other_non_200_present" in alert_codes(alerts)


def test_monitoring_fetch_stale_duplicate_insert_and_backlog_alerts():
    status, alerts = monitoring_alert_status(run_metrics={"fetch_error_rate": 0.01})
    assert status == "failing"
    assert "fetch_error_rate" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(stale_apps=[{"hours_since_completed": 25}])
    assert status == "degraded"
    assert "stale_apps_24h" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(stale_apps=[{"hours_since_completed": None}])
    assert status == "failing"
    assert "stale_apps_36h" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(run_metrics={"duplicate_rate": 0.95})
    assert status == "degraded"
    assert "high_duplicate_rate" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(
        run_metrics={"reviews_inserted": 2},
        history={"median_inserted_per_execution": 10, "comparable_execution_count": 3},
    )
    assert status == "degraded"
    assert "insert_drop" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(run_metrics={"backlog_terminal_rate": 0.051})
    assert status == "failing"
    assert "backlog_terminal_rate" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(run_metrics={"missing_scope_count": 1})
    assert status == "failing"
    assert "missing_execution_scopes" in alert_codes(alerts)

    status, alerts = monitoring_alert_status(run_metrics={"hard_failure_scope_count": 1})
    assert status == "failing"
    assert "hard_failure_scopes" in alert_codes(alerts)


def test_monitoring_freshness_uses_latest_completed_attempt_not_catchup_frontier():
    observed = {}

    class Result:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, query, params):
            observed["query"] = query
            observed["params"] = params
            return Result()

    generated_at = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)

    assert fetch_stale_apps(Connection(), source=WEB_CATALOG_SOURCE, generated_at=generated_at) == []
    assert "COALESCE(s.last_attempt_completed_at, s.last_successful_at) AS freshness_at" in observed["query"]
    assert "AS hours_since_successful_catchup" in observed["query"]
    assert observed["params"] == (
        generated_at,
        generated_at,
        "recent",
        WEB_CATALOG_SOURCE,
        generated_at,
    )


def test_monitoring_insert_drop_requires_three_comparable_complete_executions():
    status, alerts = monitoring_alert_status(
        run_metrics={"reviews_inserted": 1},
        history={"median_inserted_per_execution": 100, "comparable_execution_count": 2},
    )
    assert status == "healthy"
    assert "insert_drop" not in alert_codes(alerts)

    status, alerts = monitoring_alert_status(
        run_metrics={"reviews_inserted": 1},
        history={"median_inserted_per_execution": 100, "comparable_execution_count": 3},
    )
    assert status == "degraded"
    assert "insert_drop" in alert_codes(alerts)


def test_monitoring_zero_insert_uses_frontier_and_accounting_evidence():
    status, alerts = monitoring_alert_status(
        run_metrics={"page_count": 200, "reviews_inserted": 0, "duplicate_rate": 1.0},
        source_frontier={"comparable_scopes": 200, "unchanged_scopes": 200, "unchanged_rate": 1.0},
        accounting={"consistent": True},
    )
    assert status == "degraded"
    assert "source_snapshot_unchanged" in alert_codes(alerts)
    assert "zero_inserts_full_scope" not in alert_codes(alerts)

    status, alerts = monitoring_alert_status(accounting={"consistent": False})
    assert status == "failing"
    assert "change_accounting_mismatch" in alert_codes(alerts)


def test_monitoring_dominant_scope_is_visible_but_not_failing():
    status, alerts = monitoring_alert_status(
        app_metrics={
            "pressure_scopes": [
                {
                    "app_id": "6443467666",
                    "app_name": "Love and Deepspace",
                    "country": "us",
                    "page_share": 0.30,
                    "http_429_pages": 1,
                    "http_429_share": 1.0,
                }
            ]
        }
    )

    assert status == "degraded"
    assert "dominant_backlogged_scope" in alert_codes(alerts)


def test_monitoring_markdown_and_json_helpers_render_expected_fields():
    summary = {
        "metadata": {
            "generated_at": "2026-07-01T00:00:00Z",
            "source": WEB_CATALOG_SOURCE,
            "since": "2026-07-01T00:00:00Z",
            "github_run_id": "123",
            "workflow_result": "success",
            "selected_count": 200,
        },
        "status": "degraded",
        "alerts": [{"severity": "degraded", "code": "http_429_present", "message": "fixture"}],
        "github": {"job_total": 2, "job_success": 2, "job_failure": 0},
        "run_metrics": {
            "page_count": 10,
            "app_count": 2,
            "review_rows": 200,
            "reviews_inserted": 10,
            "reviews_updated": 1,
            "duplicates_skipped": 189,
            "duplicate_rate": 0.945,
            "http_429_pages": 1,
            "non_200_rate": 0.1,
            "retried_pages": 1,
            "fetch_errors": 0,
            "terminal_reasons": [{"terminal_reason": "caught_up_to_existing_reviews", "page_count": 2}],
        },
        "app_metrics": {
            "long_tail_apps": [{"app_name": "Fixture", "page_count": 5}],
            "top_inserted_apps": [{"app_name": "Fixture", "inserted": 10}],
        },
        "stale_apps": [],
        "database_snapshot": [{"table_name": "app_store_reviews", "row_count": 10, "total_size": "1 MB"}],
    }

    markdown = render_monitoring_markdown(summary)

    assert "Status: **degraded**" in markdown
    assert "Top Inserted Apps" in markdown
    assert extract_jobs({"jobs": [{"name": "daily"}]}) == [{"name": "daily"}]
    assert extract_runs({"workflow_runs": [{"id": 1}]}) == [{"id": 1}]
    assert monitor_exit_code("healthy", "failing") == 0
    assert monitor_exit_code("failing", "failing") == 1


def test_monitoring_github_summary_excludes_monitor_only_failures():
    github = summarize_github_payloads(
        jobs_payload={
            "jobs": [
                {"name": "daily 0 Fixture", "conclusion": "success"},
                {"name": "monitor", "conclusion": "failure"},
            ]
        },
        runs_payload={
            "workflow_runs": [
                {
                    "event": "schedule",
                    "status": "completed",
                    "conclusion": "failure",
                    "ingestion_conclusion": "success",
                    "createdAt": "2026-07-01T00:00:00Z",
                    "updatedAt": "2026-07-01T01:00:00Z",
                },
                {
                    "event": "schedule",
                    "status": "completed",
                    "conclusion": "failure",
                    "ingestion_conclusion": "failure",
                    "createdAt": "2026-06-30T12:00:00Z",
                    "updatedAt": "2026-06-30T13:00:00Z",
                },
            ]
        },
        workflow_result="success",
        generated_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
        schedule_lookback_minutes=2160,
    )

    assert is_monitor_job({"name": "monitor"}) is True
    assert is_monitor_job({"name": "notify"}) is True
    assert github["job_failure"] == 0
    assert github["recent_failed_schedule_run_count"] == 1
    assert github["recent_median_runtime_minutes"] == 60


def failing_monitoring_summary() -> dict:
    summary = {
        "metadata": {
            "generated_at": "2026-07-16T00:00:00Z",
            "github_run_id": "123",
            "github_run_url": "https://github.com/example/repo/actions/runs/123",
            "github_event_name": "schedule",
            "github_run_attempt": 1,
        },
        "status": "failing",
        "alerts": [
            {"severity": "failing", "code": "excessive_http_429", "message": "HTTP 429 threshold crossed."}
        ],
        "run_metrics": {
            "page_count": 200,
            "review_rows": 4000,
            "reviews_inserted": 20,
            "duplicates_skipped": 3980,
            "http_429_pages": 3,
            "other_non_200_pages": 0,
            "fetch_errors": 1,
        },
        "app_metrics": {
            "pressure_scopes": [
                {
                    "app_id": "6443467666",
                    "app_name": "Love and Deepspace",
                    "country": "us",
                    "page_count": 100,
                    "http_429_pages": 3,
                    "fetch_error_pages": 1,
                    "terminal_reason": "fetch_error",
                }
            ]
        },
        "stale_apps": [],
    }
    summary["notification"] = build_monitoring_notification(summary)
    return summary


def test_monitoring_notification_is_short_scoped_and_failing_only():
    summary = failing_monitoring_summary()
    notification = summary["notification"]

    assert notification["eligible"] is True
    assert notification["primary_code"] == "excessive_http_429"
    assert "Love and Deepspace" in notification["body"]
    assert summary["metadata"]["github_run_url"] in notification["body"]

    summary["status"] = "degraded"
    summary["alerts"][0]["severity"] = "degraded"
    notification = build_monitoring_notification(summary)
    assert notification["eligible"] is False
    assert "DEGRADED" in notification["subject"]
    assert "FAILING" not in notification["subject"]

    summary["status"] = "healthy"
    summary["alerts"] = [{"severity": "healthy", "code": "all_clear", "message": "No thresholds tripped."}]
    notification = build_monitoring_notification(summary)
    assert notification["eligible"] is False
    assert notification["primary_code"] == "all_clear"
    assert "HEALTHY" in notification["subject"]
    assert "status: HEALTHY" in notification["body"]
    assert "FAILING" not in notification["subject"]
    assert "FAILING" not in notification["body"]

    summary = failing_monitoring_summary()
    summary["alerts"] = [{"severity": "failing", "code": "workflow_failure", "message": "job failed"}]
    summary["github"] = {
        "failed_jobs": [{"name": "daily (6443467666, Love and Deepspace)", "conclusion": "failure"}]
    }
    notification = build_monitoring_notification(summary)
    assert notification["affected_scopes"][0]["reason"] == "failure"
    assert "daily (6443467666, Love and Deepspace)" in notification["body"]


def test_active_workflows_keep_watchdog_removed_and_backfill_hard_capped():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    backfill = (workflows / "app-store-web-catalog-backfill.yml").read_text(encoding="utf-8")

    assert not (workflows / "app-store-monitor.yml").exists()
    assert 'integer("INPUT_LIMIT", 1, 5)' in backfill
    assert 'integer("INPUT_MAX_PARALLEL", 1, 1)' in backfill
    assert 'integer("INPUT_MAX_PAGES_PER_APP_COUNTRY", 1, 25)' in backfill
    assert "I_UNDERSTAND_BACKFILL_PRESSURE" in backfill
    assert "auto_continue" not in backfill


def test_fallback_failure_report_remains_email_eligible_for_scheduled_run(tmp_path):
    report_path = tmp_path / "fallback.json"

    summary = write_fallback_failure_report(
        report_path,
        failure_code="monitor_report_unavailable",
        failure_message="Self-hosted monitor did not produce a report.",
        github_run_id="456",
        github_run_url="https://github.com/example/repo/actions/runs/456",
        github_event_name="schedule",
        github_run_attempt=1,
    )

    assert report_path.exists()
    assert summary["status"] == "failing"
    assert summary["notification"]["eligible"] is True
    assert summary["notification"]["primary_code"] == "monitor_report_unavailable"


def test_command_fallback_monitoring_report_writes_machine_readable_failure(tmp_path, capsys):
    output = tmp_path / "fallback.json"
    args = argparse.Namespace(
        json_output=output,
        failure_code="workflow_failure",
        failure_message="Required ingestion job failed.",
        github_run_id="789",
        github_run_url="https://github.com/example/repo/actions/runs/789",
        github_event_name="schedule",
        github_run_attempt=1,
        workflow_result="failure",
    )

    assert command_fallback_monitoring_report(args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "failing"
    assert payload["alerts"][0]["code"] == "workflow_failure"
    assert json.loads(capsys.readouterr().out)["json_path"] == str(output)


def test_send_monitoring_email_supports_dry_run_and_fake_smtp(tmp_path):
    report_path = tmp_path / "monitor.json"
    result_path = tmp_path / "result.json"
    preview_path = tmp_path / "preview.eml"
    report_path.write_text(json.dumps(failing_monitoring_summary()), encoding="utf-8")
    env = {
        "APP_STORE_ALERT_SMTP_USERNAME": "alerts@example.com",
        "APP_STORE_ALERT_SMTP_APP_PASSWORD": "fixture-password",
        "APP_STORE_ALERT_EMAIL_FROM": "alerts@example.com",
        "APP_STORE_ALERT_EMAIL_TO": "victor@example.com; john@example.com",
    }

    dry_run = send_monitoring_email(
        report_path,
        result_path=result_path,
        preview_path=preview_path,
        dry_run=True,
        environ=env,
    )
    assert dry_run["status"] == "dry_run"
    assert dry_run["recipient_count"] == 2
    assert preview_path.exists()
    assert parse_recipients("a@example.com,b@example.com; c@example.com") == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]

    class FakeSmtp:
        sent = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, **kwargs):
            pass

        def login(self, username, password):
            assert username == "alerts@example.com"
            assert password == "fixture-password"

        def send_message(self, message):
            self.sent.append(message)

    sent = send_monitoring_email(
        report_path,
        result_path=result_path,
        environ=env,
        smtp_factory=FakeSmtp,
    )
    assert sent["status"] == "sent"
    assert len(FakeSmtp.sent) == 1
    assert "victor@example.com" not in result_path.read_text(encoding="utf-8")


def test_command_send_monitoring_email_writes_preview_and_result(tmp_path, monkeypatch, capsys):
    report_path = tmp_path / "monitor.json"
    result_path = tmp_path / "notification.json"
    preview_path = tmp_path / "notification.eml"
    report_path.write_text(json.dumps(failing_monitoring_summary()), encoding="utf-8")
    monkeypatch.setenv("APP_STORE_ALERT_SMTP_USERNAME", "alerts@example.com")
    monkeypatch.setenv("APP_STORE_ALERT_SMTP_APP_PASSWORD", "fixture-password")
    monkeypatch.setenv("APP_STORE_ALERT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv("APP_STORE_ALERT_EMAIL_TO", "operator@example.com")
    args = argparse.Namespace(
        report_json=report_path,
        result_json=result_path,
        preview_output=preview_path,
        dry_run=True,
        force=False,
    )

    assert command_send_monitoring_email(args) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "dry_run"
    assert result["recipient_count"] == 1
    assert preview_path.exists()
    assert "operator@example.com" not in result_path.read_text(encoding="utf-8")
    assert '"status": "dry_run"' in capsys.readouterr().out


def test_command_monitoring_report_writes_outputs(tmp_path, monkeypatch, capsys):
    observed = {}

    def fake_generate_monitoring_report(*args, **kwargs):
        observed["database_url"] = args[0]
        observed.update(kwargs)
        kwargs["markdown_path"].write_text("# Fixture monitoring\n", encoding="utf-8")
        kwargs["json_path"].write_text(
            json.dumps(
                {
                    "status": "degraded",
                    "alerts": [{"severity": "degraded", "code": "http_429_present", "message": "fixture"}],
                }
            ),
            encoding="utf-8",
        )
        return {"status": "degraded", "exit_code": 0}

    monkeypatch.setattr("app_store_review_pipeline.cli.generate_monitoring_report", fake_generate_monitoring_report)
    args = argparse.Namespace(
        database_url="postgresql:///fixture",
        source=WEB_CATALOG_SOURCE,
        since="2026-07-01T00:00:00Z",
        selected_count=200,
        workflow_result="success",
        github_run_id="123",
        github_run_url="https://github.com/example/repo/actions/runs/123",
        github_event_name="schedule",
        github_run_attempt=1,
        github_jobs_json=tmp_path / "jobs.json",
        github_runs_json=tmp_path / "runs.json",
        markdown_output=tmp_path / "monitor.md",
        json_output=tmp_path / "monitor.json",
        fail_on="failing",
        require_recent_scheduled_run=True,
        schedule_lookback_minutes=180,
    )

    assert command_monitoring_report(args) == 0
    output = capsys.readouterr().out
    assert "::warning title=http_429_present::fixture" in output
    assert observed["database_url"] == "postgresql:///fixture"
    assert observed["markdown_path"] == tmp_path / "monitor.md"
    assert observed["json_path"] == tmp_path / "monitor.json"
    assert observed["require_recent_scheduled_run"] is True


def test_command_operating_ledger_upsert_run_writes_github_metadata(tmp_path):
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "databaseId": 123,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-06-30T18:00:00Z",
                "updatedAt": "2026-06-30T18:05:00Z",
                "url": "https://github.com/example/repo/actions/runs/123",
                "headSha": "abc123",
                "jobs": [
                    {"conclusion": "success"},
                    {"conclusion": "failure"},
                    {"conclusion": "cancelled"},
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.json"
    args = argparse.Namespace(
        ledger=ledger,
        repo="example/repo",
        github_run_id="123",
        label="D1 one-page cap experiment",
        comparison_group="D1_one_page_cap",
        experiment_group="om_group_01",
        status=None,
        notes="fixture",
        input=["max_pages_per_app_country=1", "experiment_group=om_group_01"],
        run_json=run_json,
    )

    assert command_operating_ledger_upsert_run(args) == 0
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    entry = payload["runs"][0]

    assert entry["github_run_id"] == "123"
    assert entry["comparison_group"] == "D1_one_page_cap"
    assert entry["experiment_group"] == "om_group_01"
    assert entry["job_total"] == 3
    assert entry["job_success"] == 1
    assert entry["job_failure"] == 1
    assert entry["job_cancelled"] == 1
    assert entry["inputs"]["max_pages_per_app_country"] == "1"


def test_web_catalog_pressure_starts_at_base_without_state(monkeypatch):
    rows = [
        {
                "page_count": 22,
                "ok_page_count": 22,
                "error_page_count": 0,
                "http_429_page_count": 0,
                "final_non_200_page_count": 0,
                "retried_page_count": 0,
                "first_page_at": "2026-06-20 19:00:00+00",
                "last_page_at": "2026-06-20 20:00:00+00",
        },
        None,
    ]

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult(rows.pop(0))

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_pressure_status("postgresql:///fixture", base_pages=5, max_pages=25)

    assert status["clean_for_ramp"] is True
    assert status["reason"] == "no_pressure_state"
    assert status["selected_max_pages_per_app_country"] == 5


def test_web_catalog_pressure_allows_recovered_retries(monkeypatch):
    rows = [
        {
            "page_count": 100,
            "ok_page_count": 100,
            "error_page_count": 0,
            "http_429_page_count": 0,
            "final_non_200_page_count": 0,
            "retried_page_count": 1,
            "first_page_at": "2026-06-20 19:00:00+00",
            "last_page_at": "2026-06-20 20:00:00+00",
        },
        {
            "source": WEB_CATALOG_SOURCE,
            "next_max_pages_per_app_country": 20,
            "safe_max_pages_per_app_country": 5,
            "candidate_max_pages_per_app_country": 20,
            "safe_max_parallel": 1,
            "candidate_max_parallel": 4,
            "safe_scope_time_budget_seconds": 1800,
            "candidate_scope_time_budget_seconds": 1800,
            "clean_run_count": 5,
        },
    ]

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult(rows.pop(0))

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_pressure_status("postgresql:///fixture", base_pages=5, max_pages=25)

    assert status["clean_for_ramp"] is True
    assert status["reason"] == "stored_pressure_state"
    assert status["selected_max_pages_per_app_country"] == 5
    assert status["retried_page_count"] == 1


def test_web_catalog_pressure_blocks_on_clustered_soft_errors(monkeypatch):
    rows = [
        {
            "page_count": 100,
            "ok_page_count": 98,
            "error_page_count": 2,
            "http_429_page_count": 0,
            "final_non_200_page_count": 0,
            "soft_error_page_count": 2,
            "retried_page_count": 0,
            "first_page_at": "2026-06-20 19:00:00+00",
            "last_page_at": "2026-06-20 20:00:00+00",
        },
        {
            "source": WEB_CATALOG_SOURCE,
            "next_max_pages_per_app_country": 20,
            "safe_max_pages_per_app_country": 5,
            "candidate_max_pages_per_app_country": 20,
            "safe_max_parallel": 1,
            "candidate_max_parallel": 4,
            "safe_scope_time_budget_seconds": 1800,
            "candidate_scope_time_budget_seconds": 1800,
            "clean_run_count": 5,
        },
    ]

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            return FakeResult(rows.pop(0))

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.web_catalog_pressure_status("postgresql:///fixture", base_pages=5, max_pages=25)

    assert status["clean_for_ramp"] is False
    assert status["reason"] == "recent_pressure_errors"
    assert status["soft_error_threshold_exceeded"] is True
    assert status["soft_error_page_count"] == 2


def test_record_web_catalog_pressure_result_raises_parallel_after_clean_run(monkeypatch):
    rows = [
        {
                "page_count": 100,
                "ok_page_count": 100,
                "error_page_count": 0,
                "http_429_page_count": 0,
                "final_non_200_page_count": 0,
                "retried_page_count": 0,
                "first_page_at": "2026-06-20 19:00:00+00",
                "last_page_at": "2026-06-20 20:00:00+00",
        },
        {
            "source": WEB_CATALOG_SOURCE,
            "next_max_pages_per_app_country": 5,
            "safe_max_pages_per_app_country": 5,
            "candidate_max_pages_per_app_country": 5,
            "safe_max_parallel": 1,
            "candidate_max_parallel": 1,
            "safe_scope_time_budget_seconds": 1800,
            "candidate_scope_time_budget_seconds": 1800,
            "clean_run_count": 2,
        },
    ]
    executed = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            executed.append((query, params))
            if query.lstrip().upper().startswith("INSERT"):
                return None
            return FakeResult(rows.pop(0))

        def commit(self):
            executed.append(("commit", None))

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    monkeypatch.setattr(postgres_database, "initialize_postgres", lambda database_url: None)
    monkeypatch.setattr(postgres_database, "connect_postgres", lambda database_url: FakeConnection())

    status = postgres_database.record_web_catalog_pressure_result(
        "postgresql:///fixture",
        since="2026-06-20T19:00:00Z",
        used_pages=5,
        base_pages=5,
        max_pages=25,
    )

    assert status["result"] == "clean_increase_pressure"
    assert status["next_max_pages_per_app_country"] == 5
    assert status["next_max_parallel"] == 2
    assert status["next_scope_time_budget_seconds"] == 1800
    assert status["next_action"] == "continue_now"
    assert status["clean_run_count"] == 3
    assert executed[-1] == ("commit", None)


def test_select_web_catalog_pressure_command_outputs_selected_page_cap(monkeypatch, capsys):
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.web_catalog_pressure_status",
        lambda *args, **kwargs: {
            "source": WEB_CATALOG_SOURCE,
            "selected_max_pages_per_app_country": 10,
            "reason": "clean_recent_pages",
        },
    )
    args = argparse.Namespace(
        database_url="postgresql:///fixture",
        source=WEB_CATALOG_SOURCE,
        lookback_minutes=720,
        base_pages=5,
        max_pages=25,
    )

    assert command_select_web_catalog_pressure(args) == 0
    assert json.loads(capsys.readouterr().out)["selected_max_pages_per_app_country"] == 10


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


class RequestsJsonDecodeWebResponse(FakeWebResponse):
    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)


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


def fixture_target(app_id: str = "123456789", app_name: str = "Fixture"):
    return AppTarget(
        app_name=app_name,
        category="test",
        apple_app_id=app_id,
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


def test_build_daily_matrix_rows_supports_experiment_group(tmp_path):
    targets_path = tmp_path / "targets.csv"
    targets_path.write_text(
        "\n".join(
            [
                "app_name,category,apple_app_id,apple_slug,countries,active,notes",
                "App A,test,111,app-a,us,true,",
                "App B,test,222,app-b,us,true,",
                "App C,test,333,app-c,us,false,",
                "App D,test,444,app-d,us,true,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    groups_path = tmp_path / "groups.json"
    groups_path.write_text(
        json.dumps({"groups": {"sample": {"app_ids": ["444", "111"]}}}),
        encoding="utf-8",
    )

    rows = build_daily_matrix_rows(targets_path, experiment_group="sample", group_path=groups_path)

    assert rows == [
        {"target_offset": 0, "app_id": "111", "app_name": "App A"},
        {"target_offset": 2, "app_id": "444", "app_name": "App D"},
    ]


def test_build_daily_matrix_rows_applies_offset_and_limit_after_group_filter(tmp_path):
    targets_path = tmp_path / "targets.csv"
    targets_path.write_text(
        "\n".join(
            [
                "app_name,category,apple_app_id,apple_slug,countries,active,notes",
                "App A,test,111,app-a,us,true,",
                "App B,test,222,app-b,us,true,",
                "App C,test,333,app-c,us,true,",
                "App D,test,444,app-d,us,true,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    groups_path = tmp_path / "groups.json"
    groups_path.write_text(
        json.dumps({"groups": {"sample": {"app_ids": ["111", "222", "333", "444"]}}}),
        encoding="utf-8",
    )

    rows = build_daily_matrix_rows(
        targets_path,
        limit=2,
        target_offset=1,
        experiment_group="sample",
        group_path=groups_path,
    )

    assert [row["app_id"] for row in rows] == ["222", "333"]
    assert [row["target_offset"] for row in rows] == [1, 2]


def test_build_daily_matrix_rows_rejects_inactive_group_member(tmp_path):
    targets_path = tmp_path / "targets.csv"
    targets_path.write_text(
        "\n".join(
            [
                "app_name,category,apple_app_id,apple_slug,countries,active,notes",
                "App A,test,111,app-a,us,true,",
                "App B,test,222,app-b,us,false,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    groups_path = tmp_path / "groups.json"
    groups_path.write_text(
        json.dumps({"groups": {"sample": {"app_ids": ["111", "222"]}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inactive or missing"):
        build_daily_matrix_rows(targets_path, experiment_group="sample", group_path=groups_path)


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


def test_fetch_web_catalog_targets_adds_positive_request_delay_jitter(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=2, has_next=False)),
        ]
    )
    sleeps = []

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=2,
        review_limit=2,
        request_delay_seconds=10,
        request_delay_jitter_seconds=4,
        web_429_retries=0,
        session=session,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )

    assert report["fetched_pages"] == 2
    assert sleeps == [12.0]


def test_fetch_web_catalog_targets_retries_malformed_json_soft_error(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, content=b"not json"),
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=1,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        web_soft_retries=1,
        web_soft_retry_seconds=0,
        session=session,
    )

    assert report["fetch_errors"] == 0
    assert report["fetched_pages"] == 1
    assert report["review_count"] == 2
    assert report["page_reports"][0]["attempt_count"] == 2
    assert report["page_reports"][0]["soft_retry_count"] == 1
    assert report["page_reports"][0]["http_429_attempt_count"] == 0
    assert len(session.calls) == 2


def test_fetch_web_catalog_targets_records_recovered_429_attempt(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(429),
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=1,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=1,
        web_429_retry_seconds=0,
        session=session,
    )

    page_report = report["page_reports"][0]
    assert page_report["status_code"] == 200
    assert page_report["attempt_count"] == 2
    assert page_report["http_429_attempt_count"] == 1
    assert page_report["soft_retry_count"] == 0


def test_fetch_web_catalog_targets_continues_across_sparse_404(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(404, content=b"not found"),
            FakeWebResponse(200, payload=web_catalog_payload(start=5, count=1, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        max_consecutive_sparse_fetch_errors=3,
        session=session,
    )

    assert len(report["page_reports"]) == 3
    assert report["fetched_pages"] == 2
    assert report["fetch_errors"] == 1
    assert report["sparse_fetch_error_pages"] == 1
    assert report["review_count"] == 3
    assert report["page_reports"][1]["status_code"] == 404
    assert report["page_reports"][1]["terminal_reason"] is None
    assert report["page_reports"][2]["terminal_reason"] == "no_next_href"
    assert "offset=4" in session.calls[2]


def test_fetch_web_catalog_targets_stops_after_sparse_404_threshold(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(404, content=b"not found"),
            FakeWebResponse(404, content=b"not found"),
            FakeWebResponse(404, content=b"not found"),
            FakeWebResponse(200, payload=web_catalog_payload(start=9, count=1, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        max_consecutive_sparse_fetch_errors=3,
        session=session,
    )

    assert len(report["page_reports"]) == 4
    assert report["fetched_pages"] == 1
    assert report["fetch_errors"] == 3
    assert report["sparse_fetch_error_pages"] == 2
    assert report["review_count"] == 2
    assert report["page_reports"][-1]["status_code"] == 404
    assert report["page_reports"][-1]["terminal_reason"] == "sparse_fetch_error_threshold"
    assert report["warning_scopes"][0]["reason"] == "sparse_fetch_error_threshold"
    assert len(session.calls) == 4


def test_fetch_web_catalog_targets_records_final_soft_error_status(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, content=b"not json"),
            FakeWebResponse(200, content=b"still not json"),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=1,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        web_soft_retries=1,
        web_soft_retry_seconds=0,
        session=session,
    )

    page = report["page_reports"][0]
    assert report["fetch_errors"] == 1
    assert page["status"] == "error"
    assert page["status_code"] == 200
    assert page["response_bytes"] > 0
    assert page["attempt_count"] == 2
    assert page["terminal_reason"] == "fetch_error"
    assert "status_code=200" in page["error_message"]
    assert "response_bytes=" in page["error_message"]


def test_fetch_web_catalog_targets_preserves_requests_json_decode_status(tmp_path):
    session = FakeWebSession([RequestsJsonDecodeWebResponse(200, content=b"")])

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=1,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        web_soft_retries=0,
        session=session,
    )

    page = report["page_reports"][0]
    assert report["fetch_errors"] == 1
    assert page["status_code"] == 200
    assert page["response_bytes"] == 0
    assert page["terminal_reason"] == "fetch_error"
    assert "status_code=200" in page["error_message"]
    assert "response_bytes=0" in page["error_message"]


def test_fetch_web_catalog_targets_stops_before_request_without_retry_window(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=2, has_next=False)),
        ]
    )
    calls = {"count": 0}

    def monotonic():
        calls["count"] += 1
        return 1176.0 if calls["count"] >= 5 else 0.0

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        timeout_seconds=20,
        request_delay_seconds=0,
        web_429_retries=0,
        web_soft_retries=1,
        web_soft_retry_seconds=5,
        time_budget_seconds=1200,
        monotonic_fn=monotonic,
        session=session,
    )

    assert len(session.calls) == 1
    assert report["fetched_pages"] == 1
    assert report["fetch_errors"] == 0
    assert report["overall_time_budget_exceeded"] is True
    assert report["warning_scopes"] == [
        {
            "app_id": "123456789",
            "app_name": "Fixture",
            "country": "us",
            "sort_by": "recent",
            "reason": "time_budget_retry_window_exceeded",
        }
    ]
    assert report["page_reports"][0]["terminal_reason"] == "time_budget_retry_window_exceeded"


def test_fetch_web_catalog_targets_zero_page_cap_follows_until_no_next(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=5, count=1, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        session=session,
    )

    assert report["page_cap_enabled"] is False
    assert report["fetched_pages"] == 3
    assert report["review_count"] == 5
    assert report["page_reports"][-1]["terminal_reason"] == "no_next_href"
    assert len(session.calls) == 3


def test_fetch_web_catalog_targets_continues_when_full_page_omits_next_href(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=False)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=1, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        session=session,
    )

    assert report["fetched_pages"] == 2
    assert report["review_count"] == 3
    assert report["page_reports"][0]["review_count"] == 2
    assert report["page_reports"][0]["has_next_link"] is False
    assert report["page_reports"][0]["terminal_reason"] is None
    assert report["page_reports"][1]["terminal_reason"] == "no_next_href"
    assert "offset=2" in session.calls[1]


def test_fetch_web_catalog_targets_reprobes_deep_partial_tail_page(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=213, count=1, has_next=False)),
            FakeWebResponse(200, payload=web_catalog_payload(start=215, count=1, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        start_page=107,
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        session=session,
    )

    assert report["fetched_pages"] == 2
    assert report["page_reports"][0]["page_number"] == 107
    assert report["page_reports"][0]["terminal_reason"] is None
    assert report["page_reports"][1]["page_number"] == 108
    assert report["page_reports"][1]["terminal_reason"] == "no_next_href"
    assert "offset=212" in session.calls[0]
    assert "offset=214" in session.calls[1]


def test_fetch_web_catalog_targets_scope_time_budget_stops_one_scope(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=1, has_next=False)),
        ]
    )
    targets = [
        fixture_target(app_id="111111111", app_name="Slow App"),
        fixture_target(app_id="222222222", app_name="Next App"),
    ]

    report = fetch_web_catalog_targets(
        targets,
        tmp_path,
        "run",
        max_pages_per_app_country=0,
        review_limit=2,
        request_delay_seconds=1,
        web_429_retries=0,
        scope_time_budget_seconds=0.5,
        session=session,
    )

    assert len(session.calls) == 2
    assert report["overall_time_budget_exceeded"] is False
    assert report["warning_scopes"] == [
        {
            "app_id": "111111111",
            "app_name": "Slow App",
            "country": "us",
            "sort_by": "recent",
            "reason": "scope_time_budget_exceeded",
        }
    ]
    assert report["page_reports"][0]["app_id"] == "111111111"
    assert report["page_reports"][0]["terminal_reason"] == "scope_time_budget_exceeded"
    assert report["page_reports"][1]["app_id"] == "222222222"
    assert report["page_reports"][1]["terminal_reason"] == "no_next_href"


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
        max_pages_per_app_country=2,
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


def test_fetch_web_catalog_targets_stops_after_target_review_count(tmp_path):
    session = FakeWebSession(
        [
            FakeWebResponse(200, payload=web_catalog_payload(start=1, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=3, count=2, has_next=True)),
            FakeWebResponse(200, payload=web_catalog_payload(start=5, count=2, has_next=False)),
        ]
    )

    report = fetch_web_catalog_targets(
        [fixture_target()],
        tmp_path,
        "run",
        max_pages_per_app_country=3,
        review_limit=2,
        request_delay_seconds=0,
        web_429_retries=0,
        known_review_ids_by_scope={("123456789", "us", "recent"): {"web-review-1"}},
        target_review_counts_by_scope={("123456789", "us", "recent"): 4},
        session=session,
    )

    assert len(session.calls) == 2
    assert report["fetched_pages"] == 2
    assert report["review_count"] == 4
    assert report["target_review_counts_enabled"] is True
    assert report["target_review_count_scopes"] == 1
    assert report["target_reached_scopes"] == [
        {
            "app_id": "123456789",
            "app_name": "Fixture",
            "country": "us",
            "sort_by": "recent",
            "target_review_count": 4,
            "fetched_review_count": 4,
        }
    ]
    assert report["page_reports"][1]["terminal_reason"] == "target_review_count_reached"


def test_daily_web_catalog_passes_start_page_to_fetcher(tmp_path, monkeypatch):
    targets_path = tmp_path / "targets.csv"
    write_targets(targets_path)
    observed = {}

    def fake_fetch_web_catalog_targets(*args, **kwargs):
        observed["start_page"] = kwargs["start_page"]
        observed["time_budget_seconds"] = kwargs["time_budget_seconds"]
        observed["scope_time_budget_seconds"] = kwargs["scope_time_budget_seconds"]
        return {
            "page_reports": [],
            "reviews": [],
            "review_count": 0,
            "unique_review_count": 0,
            "fetch_errors": 0,
            "capped_scopes": [],
            "sparse_empty_pages": 0,
        }

    monkeypatch.setattr("app_store_review_pipeline.cli.fetch_web_catalog_targets", fake_fetch_web_catalog_targets)
    monkeypatch.setattr("app_store_review_pipeline.cli.sync_targets_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.load_pipeline_run_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.update_sync_states_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.validate_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.upsert_execution_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.record_run_scopes_postgres",
        lambda *args, **kwargs: {
            "scope_count": 1,
            "caught_up_scope_count": 1,
            "backlogged_scope_count": 0,
            "hard_failure_scope_count": 0,
            "scopes": [],
        },
    )
    monkeypatch.setattr("app_store_review_pipeline.cli.finalize_execution_postgres", lambda *args, **kwargs: {})

    args = argparse.Namespace(
        raw_root=tmp_path / "raw",
        reports_root=tmp_path / "reports",
        targets=targets_path,
        database_url="postgresql:///app_store_reviews",
        limit=1,
        target_offset=0,
        sort_by="recent",
        max_pages_per_app_country=100,
        start_page=51,
        review_limit=20,
        timeout_seconds=1.0,
        request_delay_seconds=0.0,
        web_429_retries=0,
        web_429_retry_seconds=0.0,
        web_429_backoff_multiplier=1.0,
        web_soft_retries=2,
        web_soft_retry_seconds=5.0,
        web_time_budget_seconds=120.0,
        web_scope_time_budget_seconds=30.0,
        disable_overlap_stop=True,
    )

    assert command_daily_web_catalog(args) == 0
    assert observed["start_page"] == 51
    assert observed["time_budget_seconds"] == 120.0
    assert observed["scope_time_budget_seconds"] == 30.0


def test_daily_web_catalog_can_skip_matrix_postgres_init_and_target_sync(tmp_path, monkeypatch):
    targets_path = tmp_path / "targets.csv"
    write_targets(targets_path)
    observed = {}

    def fake_fetch_web_catalog_targets(*args, **kwargs):
        return {
            "page_reports": [],
            "reviews": [],
            "review_count": 0,
            "unique_review_count": 0,
            "fetch_errors": 0,
            "capped_scopes": [],
            "sparse_empty_pages": 0,
        }

    def fail_sync_targets(*args, **kwargs):
        pytest.fail("daily matrix job should not sync targets when preflight already synced them")

    def fake_trusted_ids(*args, **kwargs):
        observed["trusted_initialize_schema"] = kwargs["initialize_schema"]
        return {}

    def fake_review_counts(*args, **kwargs):
        observed["counts_initialize_schema"] = kwargs["initialize_schema"]
        return {}

    def fake_load(*args, **kwargs):
        observed["load_initialize_schema"] = kwargs["initialize_schema"]
        return {}

    def fake_sync_states(*args, **kwargs):
        observed["sync_initialize_schema"] = kwargs["initialize_schema"]
        return {}

    def fake_validate(*args, **kwargs):
        observed["validate_initialize_schema"] = kwargs["initialize_schema"]
        return {}

    monkeypatch.setattr("app_store_review_pipeline.cli.fetch_web_catalog_targets", fake_fetch_web_catalog_targets)
    monkeypatch.setattr("app_store_review_pipeline.cli.sync_targets_postgres", fail_sync_targets)
    monkeypatch.setattr("app_store_review_pipeline.cli.trusted_existing_review_ids_by_scope", fake_trusted_ids)
    monkeypatch.setattr("app_store_review_pipeline.cli.review_counts_by_scope", fake_review_counts)
    monkeypatch.setattr("app_store_review_pipeline.cli.load_pipeline_run_postgres", fake_load)
    monkeypatch.setattr("app_store_review_pipeline.cli.update_sync_states_postgres", fake_sync_states)
    monkeypatch.setattr("app_store_review_pipeline.cli.validate_postgres", fake_validate)
    monkeypatch.setattr("app_store_review_pipeline.cli.upsert_execution_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.record_run_scopes_postgres",
        lambda *args, **kwargs: {
            "scope_count": 1,
            "caught_up_scope_count": 1,
            "backlogged_scope_count": 0,
            "hard_failure_scope_count": 0,
            "scopes": [],
        },
    )
    monkeypatch.setattr("app_store_review_pipeline.cli.finalize_execution_postgres", lambda *args, **kwargs: {})

    args = argparse.Namespace(
        raw_root=tmp_path / "raw",
        reports_root=tmp_path / "reports",
        targets=targets_path,
        database_url="postgresql:///app_store_reviews",
        limit=1,
        target_offset=0,
        sort_by="recent",
        max_pages_per_app_country=1,
        start_page=1,
        review_limit=20,
        timeout_seconds=1.0,
        request_delay_seconds=0.0,
        web_429_retries=0,
        web_429_retry_seconds=0.0,
        web_429_backoff_multiplier=1.0,
        web_soft_retries=2,
        web_soft_retry_seconds=5.0,
        web_time_budget_seconds=120.0,
        web_scope_time_budget_seconds=30.0,
        disable_overlap_stop=False,
        stop_at_rss_parity=True,
        assume_postgres_schema=True,
        skip_target_sync=True,
    )

    assert command_daily_web_catalog(args) == 0
    assert observed == {
        "trusted_initialize_schema": False,
        "counts_initialize_schema": False,
        "load_initialize_schema": False,
        "sync_initialize_schema": False,
        "validate_initialize_schema": False,
    }


def test_daily_web_catalog_can_pass_rss_parity_targets_to_fetcher(tmp_path, monkeypatch):
    targets_path = tmp_path / "targets.csv"
    write_targets(targets_path)
    observed = {}

    def fake_fetch_web_catalog_targets(*args, **kwargs):
        observed["target_review_counts_by_scope"] = kwargs["target_review_counts_by_scope"]
        observed["known_review_ids_by_scope"] = kwargs["known_review_ids_by_scope"]
        return {
            "page_reports": [],
            "reviews": [],
            "review_count": 0,
            "unique_review_count": 0,
            "fetch_errors": 0,
            "capped_scopes": [],
            "target_review_counts_enabled": True,
            "target_review_count_scopes": 1,
            "target_reached_scopes": [],
        }

    monkeypatch.setattr("app_store_review_pipeline.cli.fetch_web_catalog_targets", fake_fetch_web_catalog_targets)
    monkeypatch.setattr("app_store_review_pipeline.cli.sync_targets_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.trusted_existing_review_ids_by_scope",
        lambda *args, **kwargs: {("123456789", "us", "recent"): {"web-review-1"}},
    )
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.review_counts_by_scope",
        lambda *args, **kwargs: {("123456789", "us", "recent"): 535},
    )
    monkeypatch.setattr("app_store_review_pipeline.cli.load_pipeline_run_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.update_sync_states_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.validate_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr("app_store_review_pipeline.cli.upsert_execution_postgres", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "app_store_review_pipeline.cli.record_run_scopes_postgres",
        lambda *args, **kwargs: {
            "scope_count": 1,
            "caught_up_scope_count": 1,
            "backlogged_scope_count": 0,
            "hard_failure_scope_count": 0,
            "scopes": [],
        },
    )
    monkeypatch.setattr("app_store_review_pipeline.cli.finalize_execution_postgres", lambda *args, **kwargs: {})

    args = argparse.Namespace(
        raw_root=tmp_path / "raw",
        reports_root=tmp_path / "reports",
        targets=targets_path,
        database_url="postgresql:///app_store_reviews",
        limit=1,
        target_offset=0,
        sort_by="recent",
        max_pages_per_app_country=35,
        start_page=1,
        review_limit=20,
        timeout_seconds=1.0,
        request_delay_seconds=0.0,
        web_429_retries=0,
        web_429_retry_seconds=0.0,
        web_429_backoff_multiplier=1.0,
        web_soft_retries=2,
        web_soft_retry_seconds=5.0,
        web_time_budget_seconds=0.0,
        web_scope_time_budget_seconds=0.0,
        disable_overlap_stop=False,
        stop_at_rss_parity=True,
    )

    assert command_daily_web_catalog(args) == 0
    assert observed["known_review_ids_by_scope"] == {("123456789", "us", "recent"): {"web-review-1"}}
    assert observed["target_review_counts_by_scope"] == {("123456789", "us", "recent"): 535}


def test_summarize_fetch_cli_includes_stability_metrics():
    summary = summarize_fetch_cli(
        {
            "review_count": 2,
            "unique_review_count": 2,
            "fetch_errors": 1,
            "capped_scopes": [{"app_id": "123"}],
            "warning_scopes": [{"reason": "scope_time_budget_exceeded"}],
            "overall_time_budget_exceeded": False,
            "scope_time_budget_seconds": 30.0,
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
    assert summary["warning_scope_reasons"] == {"scope_time_budget_exceeded": 1}
    assert summary["warning_scope_count"] == 1
    assert summary["scope_time_budget_seconds"] == 30.0
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
    assert review.content == "Useful review text"
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
    assert scope_key("123", "US") == "apple_itunes_customerreviews_rss:123:us:mostrecent"


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


def test_web_429_retry_adds_positive_jitter_to_retry_sleep():
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
        web_429_retry_jitter_seconds=4,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.25,
    )

    assert response.status_code == 200
    assert [attempt["status_code"] for attempt in attempts] == [429, 429, 200]
    assert [attempt.get("retry_delay_seconds") for attempt in attempts[:2]] == [11.0, 8.0]
    assert sleeps == [11.0, 8.0]


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


def test_web_catalog_ingestion_history_accepts_parity_stop_before_ceiling(tmp_path):
    path = tmp_path / "parity" / "daily_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": "parity-stop",
                "source": WEB_CATALOG_SOURCE,
                "target_count": 1,
                "scope_count": 1,
                "target_offset": 4,
                "max_pages_per_app_country": 35,
                "review_limit": 20,
                "fetch_summary": {
                    "pages": 27,
                    "reviews": 540,
                    "unique_reviews": 540,
                    "fetch_errors": 0,
                    "status_code_counts": {"200": 27},
                    "attempt_counts": {"1": 27},
                    "retried_pages": 0,
                    "successful_after_retry_pages": 0,
                    "final_non_200_pages": 0,
                    "terminal_reasons": {"target_review_count_reached": 1},
                    "missing_text": 0,
                    "missing_rating": 0,
                    "all_pages_ok_after_retry": True,
                },
                "load_summary": {"inserted": 540, "updated": 0, "duplicates_skipped": 0},
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_web_ingestion_history_from_reports([path], min_runs=1, full_single_app_only=True)

    assert summary["promotion_gate"]["status"] == "ready_for_controlled_promotion"
    assert summary["aggregate"]["runs_reaching_configured_ceiling"] == 0
    assert summary["aggregate"]["runs_reaching_target_review_count"] == 1
    assert summary["aggregate"]["runs_with_successful_completion"] == 1
    assert summary["runs"][0]["reached_configured_ceiling"] is False
    assert summary["runs"][0]["reached_target_review_count"] is True


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


def test_source_coverage_scorecard_marks_parity_and_gaps():
    records = [
        {
            "app_name": "Amazon Shopping",
            "rss_has_rows": True,
            "web_has_rows": True,
            "rss_reviews": 535,
            "web_catalog_reviews": 3500,
            "web_at_or_above_rss": True,
            "web_to_rss_ratio": 3500 / 535,
            "web_review_gap_to_rss": 0,
        },
        {
            "app_name": "Walmart",
            "rss_has_rows": True,
            "web_has_rows": True,
            "rss_reviews": 556,
            "web_catalog_reviews": 500,
            "web_at_or_above_rss": False,
            "web_to_rss_ratio": 500 / 556,
            "web_review_gap_to_rss": 56,
        },
        {
            "app_name": "Lyft",
            "rss_has_rows": True,
            "web_has_rows": False,
            "rss_reviews": 533,
            "web_catalog_reviews": 0,
            "web_at_or_above_rss": False,
            "web_to_rss_ratio": 0,
            "web_review_gap_to_rss": 533,
        },
    ]

    summary = summarize_scope_records(records, min_parity_scopes=2)

    assert summary["promotion_gate"]["status"] == "needs_more_evidence"
    assert summary["promotion_gate"]["blocking_reasons"] == [
        "needs_at_least_2_parity_scopes",
        "one_or_more_web_scopes_below_rss",
    ]
    assert summary["aggregate"]["target_scope_count"] == 3
    assert summary["aggregate"]["web_catalog_scope_count"] == 2
    assert summary["aggregate"]["parity_scope_count"] == 1
    assert summary["aggregate"]["below_rss_scope_count"] == 1
    assert summary["aggregate"]["missing_web_scope_count"] == 1
    assert summary["aggregate"]["web_review_gap_to_rss_total"] == 589
    assert summary["aggregate"]["web_scopes_at_or_above_500_count"] == 2
    assert summary["aggregate"]["web_scopes_above_500_count"] == 1
    assert summary["aggregate"]["max_web_catalog_reviews_for_scope"] == 3500
    assert summary["aggregate"]["minimum_web_to_rss_ratio_for_web_scopes"] == pytest.approx(500 / 556)


def test_source_coverage_selector_prioritizes_reachable_cleanup_scope():
    records = [
        {
            "target_index": 1,
            "app_name": "Already Covered",
            "rss_has_rows": True,
            "web_has_rows": True,
            "rss_reviews": 500,
            "web_catalog_reviews": 520,
            "web_at_or_above_rss": True,
            "web_review_gap_to_rss": 0,
        },
        {
            "target_index": 2,
            "app_name": "Huge But Over Capacity",
            "rss_has_rows": True,
            "web_has_rows": False,
            "rss_reviews": 1000,
            "web_catalog_reviews": 0,
            "web_at_or_above_rss": False,
            "web_review_gap_to_rss": 1000,
        },
        {
            "target_index": 3,
            "app_name": "Reachable Missing",
            "rss_has_rows": True,
            "web_has_rows": False,
            "rss_reviews": 650,
            "web_catalog_reviews": 0,
            "web_at_or_above_rss": False,
            "web_review_gap_to_rss": 650,
        },
        {
            "target_index": 4,
            "app_name": "Partial",
            "rss_has_rows": True,
            "web_has_rows": True,
            "rss_reviews": 620,
            "web_catalog_reviews": 500,
            "web_at_or_above_rss": False,
            "web_review_gap_to_rss": 120,
        },
    ]

    selected = choose_next_web_catalog_scope(records, max_pages_per_app_country=35, review_limit=20)

    assert selected is not None
    assert selected["target_index"] == 4


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


def test_web_catalog_ingestion_history_uses_effective_start_page_from_raw_report(tmp_path):
    run_id = "run-start-mismatch"
    daily_report = {
        "run_id": run_id,
        "source": WEB_CATALOG_SOURCE,
        "target_count": 1,
        "scope_count": 1,
        "target_offset": 0,
        "max_pages_per_app_country": 100,
        "start_page": 51,
        "review_limit": 20,
        "fetch_summary": {
            "pages": 100,
            "reviews": 2000,
            "unique_reviews": 2000,
            "fetch_errors": 0,
            "status_code_counts": {"200": 100},
            "all_pages_ok_after_retry": True,
        },
        "load_summary": {"inserted": 1000, "updated": 0, "duplicates_skipped": 1000},
    }
    daily_path = tmp_path / "artifact" / "reports" / "apple_web_catalog" / run_id / "daily_report.json"
    daily_path.parent.mkdir(parents=True)
    daily_path.write_text(json.dumps(daily_report), encoding="utf-8")
    raw_path = tmp_path / "artifact" / "raw" / "apple_web_catalog" / run_id / "fetch_report.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(
        json.dumps(
            {
                "start_page": 1,
                "page_reports": [
                    {
                        "page_number": 1,
                        "status": "ok",
                        "status_code": 200,
                        "attempt_count": 1,
                        "terminal_reason": None,
                        "missing_text_count": 0,
                        "missing_rating_count": 0,
                    }
                ],
                "review_count": 2000,
                "unique_review_count": 2000,
                "fetch_errors": 0,
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_web_ingestion_history_from_reports([daily_path], min_runs=1, min_reviews_per_run=2000)

    assert summary["promotion_gate"]["status"] == "ready_for_controlled_promotion"
    assert summary["aggregate"]["start_page_mismatch_runs"] == 1
    assert summary["runs"][0]["start_page"] == 1
    assert summary["runs"][0]["requested_start_page"] == 51
    assert summary["runs"][0]["configured_review_ceiling"] == 2000
    assert summary["runs"][0]["reached_configured_ceiling"] is True


def test_web_catalog_depth_summary_marks_over_500_and_open_page_cap():
    summary = summarize_web_catalog_depth_rows(
        [
            {
                "app_id": "297606951",
                "app_name": "Amazon Shopping",
                "country": "us",
                "min_page_number": 1,
                "max_page_number": 175,
                "page_rows": 225,
                "page_observed_review_rows": 4500,
                "unique_review_rows": 3500,
                "retried_pages": 8,
                "final_429_pages": 0,
                "final_non_200_pages": 0,
                "fetch_error_pages": 0,
                "has_next_link_on_any_page": 1,
                "terminal_page_had_next_link": 1,
                "terminal_page_number": 175,
                "terminal_reasons": "page_cap",
            },
            {
                "app_id": "351727428",
                "app_name": "Venmo",
                "country": "us",
                "min_page_number": 1,
                "max_page_number": 25,
                "page_rows": 25,
                "page_observed_review_rows": 500,
                "unique_review_rows": 500,
                "retried_pages": 0,
                "final_429_pages": 0,
                "final_non_200_pages": 0,
                "fetch_error_pages": 0,
                "has_next_link_on_any_page": 1,
                "terminal_page_had_next_link": 1,
                "terminal_page_number": 25,
                "terminal_reasons": "page_cap",
            },
        ]
    )

    assert summary["app_scope_count"] == 2
    assert summary["apps_at_or_above_500_unique_reviews"] == 2
    assert summary["apps_over_500_unique_reviews"] == 1
    assert summary["max_unique_reviews_for_one_app"] == 3500
    assert summary["max_page_reached"] == 175
    assert summary["page_cap_with_next_link_scopes"] == 2
    assert summary["stopped_before_catalog_exhaustion_scopes"] == 2
    assert summary["max_depth_app"]["app_name"] == "Amazon Shopping"
    assert summary["apps"][0]["hit_page_cap_with_next_link"] is True
    assert summary["apps"][0]["stopped_before_catalog_exhaustion"] is True


def test_web_catalog_depth_markdown_includes_database_depth_section():
    summary = {
        "promotion_gate": {"status": "needs_more_evidence", "min_runs": 1, "blocking_reasons": []},
        "generated_from_report_count": 0,
        "full_single_app_only": False,
        "min_reviews_per_run": 500,
        "aggregate": {},
        "database": {
            "database_url": "postgresql:///app_store_reviews",
            "source_rows": [],
            "web_catalog_apps": [],
            "web_catalog_depth": summarize_web_catalog_depth_rows(
                [
                    {
                        "app_id": "297606951",
                        "app_name": "Amazon Shopping",
                        "country": "us",
                        "min_page_number": 1,
                        "max_page_number": 175,
                        "page_rows": 225,
                        "page_observed_review_rows": 4500,
                        "unique_review_rows": 3500,
                        "retried_pages": 8,
                        "final_429_pages": 0,
                        "final_non_200_pages": 0,
                        "fetch_error_pages": 0,
                        "has_next_link_on_any_page": 1,
                        "terminal_page_had_next_link": 1,
                        "terminal_page_number": 175,
                        "terminal_reasons": "page_cap",
                    }
                ]
            ),
        },
        "runs": [],
    }

    markdown = render_web_ingestion_markdown_summary(summary)

    assert "### Web Catalog Depth Evidence" in markdown
    assert "Amazon Shopping" in markdown
    assert "Max page reached: `175`" in markdown


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
