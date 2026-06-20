from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app_store_review_pipeline.config import (
    DEFAULT_42MATTERS_REPORTS_ROOT,
    DEFAULT_APPFIGURES_REPORTS_ROOT,
    DEFAULT_APPTWEAK_REPORTS_ROOT,
    DEFAULT_COMPARE_RAW_ROOT,
    DEFAULT_COMPARE_REPORTS_ROOT,
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_PAGES_PER_APP_COUNTRY,
    DEFAULT_PROVIDER_COMPARE_RAW_ROOT,
    DEFAULT_PROVIDER_COMPARE_REPORTS_ROOT,
    DEFAULT_RAW_ROOT,
    DEFAULT_REPORTS_ROOT,
    DEFAULT_REQUEST_DELAY_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_SORT_BY,
    DEFAULT_TARGETS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WEB_CATALOG_RAW_ROOT,
    DEFAULT_WEB_CATALOG_REPORTS_ROOT,
    DEFAULT_WEB_REPORTS_ROOT,
    SOURCE,
    WEB_CATALOG_SORT_BY,
    WEB_CATALOG_SOURCE,
)
from app_store_review_pipeline.apple_web import probe_web_reviews
from app_store_review_pipeline.daily import run_daily_pipeline
from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.postgres_database import (
    existing_review_ids_by_scope,
    initialize_postgres,
    load_pipeline_run_postgres,
    mask_database_url,
    review_counts_by_scope,
    record_web_catalog_pressure_result,
    validate_postgres,
    web_catalog_429_circuit_breaker_status,
    web_catalog_429_cooldown_status,
    web_catalog_pressure_status,
)
from app_store_review_pipeline.provider_apptweak import probe_apptweak_reviews
from app_store_review_pipeline.provider_appfigures import probe_appfigures_reviews
from app_store_review_pipeline.provider_compare import (
    compare_rss_with_42matters,
    compare_rss_with_appfigures,
    compare_rss_with_apptweak,
)
from app_store_review_pipeline.provider_42matters import probe_42matters_reviews
from app_store_review_pipeline.targets import active_targets, load_targets
from app_store_review_pipeline.source_compare import compare_sources
from app_store_review_pipeline.utils import make_run_id, utc_timestamp
from app_store_review_pipeline.web_catalog_fetcher import fetch_web_catalog_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple App Store public review ingestion pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    targets = subparsers.add_parser("targets", help="Summarize target apps and app-country scopes.")
    targets.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    targets.set_defaults(func=command_targets)

    fetch = subparsers.add_parser("fetch", help="Fetch raw Apple RSS review pages.")
    add_fetch_arguments(fetch)
    fetch.set_defaults(func=command_fetch)

    fetch_web_catalog = subparsers.add_parser(
        "fetch-web-catalog",
        help="Fetch full review rows from the public App Store web catalog JSON endpoint.",
    )
    add_web_catalog_fetch_arguments(fetch_web_catalog)
    fetch_web_catalog.set_defaults(func=command_fetch_web_catalog)

    init_postgres = subparsers.add_parser("init-postgres", help="Create or update the Postgres schema.")
    init_postgres.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    init_postgres.set_defaults(func=command_init_postgres)

    web_429_breaker = subparsers.add_parser(
        "check-web-429-circuit-breaker",
        help="Fail fast when recent App Store web catalog requests are mostly HTTP 429.",
    )
    web_429_breaker.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_429_breaker.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_429_breaker.add_argument("--since")
    web_429_breaker.add_argument("--lookback-minutes", type=int, default=60)
    web_429_breaker.add_argument("--min-pages", type=int, default=4)
    web_429_breaker.add_argument("--max-rate", type=float, default=0.5)
    web_429_breaker.set_defaults(func=command_check_web_429_circuit_breaker)

    web_429_cooldown = subparsers.add_parser(
        "check-web-429-cooldown",
        help="Fail fast when the most recent App Store web catalog HTTP 429 is still inside the cooldown window.",
    )
    web_429_cooldown.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_429_cooldown.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_429_cooldown.add_argument("--cooldown-minutes", type=int, default=720)
    web_429_cooldown.set_defaults(func=command_check_web_429_cooldown)

    web_pressure = subparsers.add_parser(
        "select-web-catalog-pressure",
        help="Choose the scheduled web catalog page cap from recent clean request history.",
    )
    web_pressure.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_pressure.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_pressure.add_argument("--lookback-minutes", type=int, default=720)
    web_pressure.add_argument("--base-pages", type=int, default=5)
    web_pressure.add_argument("--max-pages", type=int, default=25)
    web_pressure.add_argument("--base-parallel", type=int, default=1)
    web_pressure.add_argument("--max-parallel", type=int, default=4)
    web_pressure.add_argument("--base-scope-time-budget-seconds", type=int, default=1800)
    web_pressure.add_argument("--max-scope-time-budget-seconds", type=int, default=7200)
    web_pressure.add_argument("--selection-mode", choices=["safe", "candidate"], default="safe")
    web_pressure.set_defaults(func=command_select_web_catalog_pressure)

    web_pressure_record = subparsers.add_parser(
        "record-web-catalog-pressure-result",
        help="Record the latest scheduled web catalog pressure result and choose the next page cap.",
    )
    web_pressure_record.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    web_pressure_record.add_argument("--source", default=WEB_CATALOG_SOURCE)
    web_pressure_record.add_argument("--since", required=True)
    web_pressure_record.add_argument("--used-pages", type=int, required=True)
    web_pressure_record.add_argument("--used-parallel", type=int, default=1)
    web_pressure_record.add_argument("--used-scope-time-budget-seconds", type=int, default=1800)
    web_pressure_record.add_argument("--base-pages", type=int, default=5)
    web_pressure_record.add_argument("--max-pages", type=int, default=25)
    web_pressure_record.add_argument("--base-parallel", type=int, default=1)
    web_pressure_record.add_argument("--max-parallel", type=int, default=4)
    web_pressure_record.add_argument("--base-scope-time-budget-seconds", type=int, default=1800)
    web_pressure_record.add_argument("--max-scope-time-budget-seconds", type=int, default=7200)
    web_pressure_record.add_argument("--cooldown-minutes", type=int, default=30)
    web_pressure_record.set_defaults(func=command_record_web_catalog_pressure_result)

    load = subparsers.add_parser("load", aliases=["load-postgres"], help="Load raw Apple RSS pages into Postgres.")
    load.add_argument("--raw-dir", type=Path, required=True)
    load.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    load.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    load.set_defaults(func=command_load_postgres)

    validate = subparsers.add_parser("validate", aliases=["validate-postgres"], help="Validate the Postgres database.")
    validate.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    validate.add_argument("--run-id")
    validate.add_argument("--output", type=Path)
    validate.set_defaults(func=command_validate_postgres)

    probe_web = subparsers.add_parser(
        "probe-web",
        help="Probe public App Store HTML and web JSON review surfaces without loading Postgres.",
    )
    probe_web.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    probe_web.add_argument("--reports-root", type=Path, default=DEFAULT_WEB_REPORTS_ROOT)
    probe_web.add_argument("--output", type=Path)
    probe_web.add_argument("--limit", type=int, default=20, help="Maximum active targets to probe. Use 0 for all.")
    probe_web.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    probe_web.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    probe_web.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="Requested review page size for the public web catalog reviews endpoint. Apple currently caps this at 20.",
    )
    probe_web.add_argument(
        "--web-sort",
        default="recent",
        choices=["recent", "helpful", "favorable", "critical"],
        help="Sort order for the public web catalog reviews endpoint.",
    )
    probe_web.add_argument(
        "--attempt-pagination",
        action="store_true",
        help="Follow web catalog review next hrefs up to --max-web-pages. This is diagnostic only.",
    )
    probe_web.add_argument(
        "--max-web-pages",
        type=int,
        default=2,
        help="Maximum web catalog review pages to follow when --attempt-pagination is enabled.",
    )
    probe_web.add_argument(
        "--web-429-retries",
        type=int,
        default=0,
        help="Number of retry attempts for a web catalog page that returns 429. Default is no retry.",
    )
    probe_web.add_argument(
        "--web-429-retry-seconds",
        type=float,
        default=30.0,
        help="Delay before retrying a web catalog page after HTTP 429.",
    )
    probe_web.add_argument(
        "--web-429-backoff-multiplier",
        type=float,
        default=1.0,
        help="Multiplier applied to the fixed HTTP 429 retry delay on each additional retry.",
    )
    probe_web.add_argument(
        "--skip-html",
        action="store_true",
        help="Skip the public App Store HTML page request and probe only the web catalog JSON review endpoint.",
    )
    probe_web.set_defaults(func=command_probe_web)

    compare = subparsers.add_parser(
        "compare-sources",
        help="Run the same target window through RSS and web catalog recent reviews and write a comparison report.",
    )
    compare.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    compare.add_argument("--raw-root", type=Path, default=DEFAULT_COMPARE_RAW_ROOT)
    compare.add_argument("--reports-root", type=Path, default=DEFAULT_COMPARE_REPORTS_ROOT)
    compare.add_argument("--limit", type=int, default=20, help="Maximum active targets to compare. Use 0 for all.")
    compare.add_argument(
        "--target-offset",
        type=int,
        default=0,
        help="Number of active targets to skip before applying --limit. Useful for rotating canary windows.",
    )
    compare.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    compare.add_argument("--rss-request-delay-seconds", type=float, default=0.5)
    compare.add_argument("--rss-max-pages-per-app-country", type=int, default=DEFAULT_MAX_PAGES_PER_APP_COUNTRY)
    compare.add_argument("--rss-max-consecutive-empty-pages", type=int, default=DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES)
    compare.add_argument("--rss-max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    compare.add_argument("--rss-retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)
    compare.add_argument("--web-request-delay-seconds", type=float, default=2.0)
    compare.add_argument("--web-max-pages", type=int, default=5)
    compare.add_argument("--web-review-limit", type=int, default=20)
    compare.add_argument("--web-429-retries", type=int, default=3)
    compare.add_argument("--web-429-retry-seconds", type=float, default=45.0)
    compare.add_argument("--web-429-backoff-multiplier", type=float, default=1.0)
    compare.add_argument(
        "--web-time-budget-seconds",
        type=float,
        default=0.0,
        help="Optional wall-clock budget for the web catalog comparison path. Use 0 for no budget.",
    )
    compare.add_argument(
        "--web-skip-html",
        action="store_true",
        help="Skip the HTML page request in the web catalog comparison path.",
    )
    compare.add_argument(
        "--web-stop-at-rss-parity",
        action="store_true",
        help="Stop each web catalog scope once fetched web reviews match that scope's RSS review count.",
    )
    compare.set_defaults(func=command_compare_sources)

    provider_42matters = subparsers.add_parser(
        "probe-42matters",
        help="Probe the licensed 42matters iOS app reviews API without loading Postgres.",
    )
    provider_42matters.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    provider_42matters.add_argument("--reports-root", type=Path, default=DEFAULT_42MATTERS_REPORTS_ROOT)
    provider_42matters.add_argument("--output", type=Path)
    provider_42matters.add_argument(
        "--access-token",
        default=os.environ.get("APP_STORE_42MATTERS_TOKEN"),
        help="42matters access token. Defaults to APP_STORE_42MATTERS_TOKEN.",
    )
    provider_42matters.add_argument("--limit", type=int, default=5, help="Maximum active targets to probe. Use 0 for all.")
    provider_42matters.add_argument("--days", type=int, default=30)
    provider_42matters.add_argument("--start-date")
    provider_42matters.add_argument("--end-date")
    provider_42matters.add_argument("--lang")
    provider_42matters.add_argument("--rating", type=int)
    provider_42matters.add_argument("--page-limit", type=int, default=2)
    provider_42matters.add_argument("--request-limit", type=int, default=100)
    provider_42matters.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    provider_42matters.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    provider_42matters.set_defaults(func=command_probe_42matters)

    compare_42matters = subparsers.add_parser(
        "compare-42matters",
        help="Compare RSS with the licensed 42matters iOS app reviews API on the same targets.",
    )
    compare_42matters.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    compare_42matters.add_argument("--raw-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_RAW_ROOT)
    compare_42matters.add_argument("--reports-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_REPORTS_ROOT)
    compare_42matters.add_argument(
        "--access-token",
        default=os.environ.get("APP_STORE_42MATTERS_TOKEN"),
        help="42matters access token. Defaults to APP_STORE_42MATTERS_TOKEN.",
    )
    compare_42matters.add_argument("--limit", type=int, default=10, help="Maximum active targets to compare. Use 0 for all.")
    compare_42matters.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    compare_42matters.add_argument("--rss-request-delay-seconds", type=float, default=0.5)
    compare_42matters.add_argument("--rss-max-pages-per-app-country", type=int, default=DEFAULT_MAX_PAGES_PER_APP_COUNTRY)
    compare_42matters.add_argument("--rss-max-consecutive-empty-pages", type=int, default=DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES)
    compare_42matters.add_argument("--rss-max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    compare_42matters.add_argument("--rss-retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)
    compare_42matters.add_argument("--provider-days", type=int, default=30)
    compare_42matters.add_argument("--provider-start-date")
    compare_42matters.add_argument("--provider-end-date")
    compare_42matters.add_argument("--provider-lang")
    compare_42matters.add_argument("--provider-rating", type=int)
    compare_42matters.add_argument("--provider-page-limit", type=int, default=5)
    compare_42matters.add_argument("--provider-request-limit", type=int, default=100)
    compare_42matters.add_argument("--provider-request-delay-seconds", type=float, default=0.4)
    compare_42matters.set_defaults(func=command_compare_42matters)

    provider_apptweak = subparsers.add_parser(
        "probe-apptweak",
        help="Probe the licensed AppTweak app reviews search API without loading Postgres.",
    )
    provider_apptweak.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    provider_apptweak.add_argument("--reports-root", type=Path, default=DEFAULT_APPTWEAK_REPORTS_ROOT)
    provider_apptweak.add_argument("--output", type=Path)
    provider_apptweak.add_argument(
        "--api-token",
        default=os.environ.get("APP_STORE_APPTWEAK_TOKEN"),
        help="AppTweak API token. Defaults to APP_STORE_APPTWEAK_TOKEN.",
    )
    provider_apptweak.add_argument("--limit", type=int, default=5, help="Maximum active targets to probe. Use 0 for all.")
    provider_apptweak.add_argument("--country-fallback", default="us")
    provider_apptweak.add_argument("--language", default="us")
    provider_apptweak.add_argument("--device", default="iphone")
    provider_apptweak.add_argument("--start-date")
    provider_apptweak.add_argument("--end-date")
    provider_apptweak.add_argument("--term")
    provider_apptweak.add_argument("--page-limit", type=int, default=2)
    provider_apptweak.add_argument("--request-limit", type=int, default=500)
    provider_apptweak.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    provider_apptweak.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    provider_apptweak.set_defaults(func=command_probe_apptweak)

    compare_apptweak = subparsers.add_parser(
        "compare-apptweak",
        help="Compare RSS with the licensed AppTweak app reviews search API on the same targets.",
    )
    compare_apptweak.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    compare_apptweak.add_argument("--raw-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_RAW_ROOT)
    compare_apptweak.add_argument("--reports-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_REPORTS_ROOT)
    compare_apptweak.add_argument(
        "--api-token",
        default=os.environ.get("APP_STORE_APPTWEAK_TOKEN"),
        help="AppTweak API token. Defaults to APP_STORE_APPTWEAK_TOKEN.",
    )
    compare_apptweak.add_argument("--limit", type=int, default=10, help="Maximum active targets to compare. Use 0 for all.")
    compare_apptweak.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    compare_apptweak.add_argument("--rss-request-delay-seconds", type=float, default=0.5)
    compare_apptweak.add_argument("--rss-max-pages-per-app-country", type=int, default=DEFAULT_MAX_PAGES_PER_APP_COUNTRY)
    compare_apptweak.add_argument("--rss-max-consecutive-empty-pages", type=int, default=DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES)
    compare_apptweak.add_argument("--rss-max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    compare_apptweak.add_argument("--rss-retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)
    compare_apptweak.add_argument("--provider-country-fallback", default="us")
    compare_apptweak.add_argument("--provider-language", default="us")
    compare_apptweak.add_argument("--provider-device", default="iphone")
    compare_apptweak.add_argument("--provider-start-date")
    compare_apptweak.add_argument("--provider-end-date")
    compare_apptweak.add_argument("--provider-term")
    compare_apptweak.add_argument("--provider-page-limit", type=int, default=2)
    compare_apptweak.add_argument("--provider-request-limit", type=int, default=500)
    compare_apptweak.add_argument("--provider-request-delay-seconds", type=float, default=1.0)
    compare_apptweak.set_defaults(func=command_compare_apptweak)

    provider_appfigures = subparsers.add_parser(
        "probe-appfigures",
        help="Probe the licensed Appfigures Public Data reviews API without loading Postgres.",
    )
    provider_appfigures.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    provider_appfigures.add_argument("--reports-root", type=Path, default=DEFAULT_APPFIGURES_REPORTS_ROOT)
    provider_appfigures.add_argument("--output", type=Path)
    provider_appfigures.add_argument(
        "--access-token",
        default=os.environ.get("APP_STORE_APPFIGURES_TOKEN"),
        help="Appfigures personal access token. Defaults to APP_STORE_APPFIGURES_TOKEN.",
    )
    provider_appfigures.add_argument("--limit", type=int, default=5, help="Maximum active targets to probe. Use 0 for all.")
    provider_appfigures.add_argument("--country-fallback", default="us")
    provider_appfigures.add_argument("--start-date")
    provider_appfigures.add_argument("--end-date")
    provider_appfigures.add_argument("--lang")
    provider_appfigures.add_argument("--stars")
    provider_appfigures.add_argument("--sort", default="-date")
    provider_appfigures.add_argument("--page-limit", type=int, default=2)
    provider_appfigures.add_argument("--request-limit", type=int, default=500)
    provider_appfigures.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    provider_appfigures.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    provider_appfigures.set_defaults(func=command_probe_appfigures)

    compare_appfigures = subparsers.add_parser(
        "compare-appfigures",
        help="Compare RSS with the licensed Appfigures Public Data reviews API on the same targets.",
    )
    compare_appfigures.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    compare_appfigures.add_argument("--raw-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_RAW_ROOT)
    compare_appfigures.add_argument("--reports-root", type=Path, default=DEFAULT_PROVIDER_COMPARE_REPORTS_ROOT)
    compare_appfigures.add_argument(
        "--access-token",
        default=os.environ.get("APP_STORE_APPFIGURES_TOKEN"),
        help="Appfigures personal access token. Defaults to APP_STORE_APPFIGURES_TOKEN.",
    )
    compare_appfigures.add_argument("--limit", type=int, default=10, help="Maximum active targets to compare. Use 0 for all.")
    compare_appfigures.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    compare_appfigures.add_argument("--rss-request-delay-seconds", type=float, default=0.5)
    compare_appfigures.add_argument("--rss-max-pages-per-app-country", type=int, default=DEFAULT_MAX_PAGES_PER_APP_COUNTRY)
    compare_appfigures.add_argument("--rss-max-consecutive-empty-pages", type=int, default=DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES)
    compare_appfigures.add_argument("--rss-max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    compare_appfigures.add_argument("--rss-retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)
    compare_appfigures.add_argument("--provider-country-fallback", default="us")
    compare_appfigures.add_argument("--provider-start-date")
    compare_appfigures.add_argument("--provider-end-date")
    compare_appfigures.add_argument("--provider-lang")
    compare_appfigures.add_argument("--provider-stars")
    compare_appfigures.add_argument("--provider-sort", default="-date")
    compare_appfigures.add_argument("--provider-page-limit", type=int, default=2)
    compare_appfigures.add_argument("--provider-request-limit", type=int, default=500)
    compare_appfigures.add_argument("--provider-request-delay-seconds", type=float, default=1.0)
    compare_appfigures.set_defaults(func=command_compare_appfigures)

    daily = subparsers.add_parser("daily", help="Fetch, load, validate, and report Apple App Store reviews.")
    add_fetch_arguments(daily)
    daily.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT)
    daily.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    daily.add_argument(
        "--disable-overlap-stop",
        action="store_true",
        help="Fetch to the page cap even after already-known review IDs are seen.",
    )
    daily.set_defaults(func=command_daily)

    daily_web_catalog = subparsers.add_parser(
        "daily-web-catalog",
        help="Fetch, load, validate, and report public App Store web catalog reviews.",
    )
    add_web_catalog_fetch_arguments(daily_web_catalog)
    daily_web_catalog.add_argument("--reports-root", type=Path, default=DEFAULT_WEB_CATALOG_REPORTS_ROOT)
    daily_web_catalog.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    daily_web_catalog.add_argument(
        "--disable-overlap-stop",
        action="store_true",
        help="Fetch to page cap even after already-known web catalog review IDs are seen.",
    )
    daily_web_catalog.set_defaults(func=command_daily_web_catalog)

    return parser


def add_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--sort-by", default=DEFAULT_SORT_BY)
    parser.add_argument("--max-pages-per-app-country", type=int, default=DEFAULT_MAX_PAGES_PER_APP_COUNTRY)
    parser.add_argument(
        "--max-consecutive-empty-pages",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES,
        help="Continue across RSS pages that are empty but still advertise a next page, up to this streak length.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--retry-delay-seconds", type=float, default=DEFAULT_RETRY_DELAY_SECONDS)


def add_web_catalog_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_WEB_CATALOG_RAW_ROOT)
    parser.add_argument("--sort-by", default=WEB_CATALOG_SORT_BY, choices=["recent", "helpful", "favorable", "critical"])
    parser.add_argument("--limit", type=int, default=1, help="Maximum active targets to fetch. Use 0 for all.")
    parser.add_argument("--target-offset", type=int, default=0, help="Number of active targets to skip before applying limit.")
    parser.add_argument("--max-pages-per-app-country", type=int, default=25)
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First web catalog page number to fetch; use values above 1 for manual depth-limit probes.",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="Requested web catalog reviews per page; Apple currently caps this at 20.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--request-delay-seconds", type=float, default=5.0)
    parser.add_argument("--web-429-retries", type=int, default=5)
    parser.add_argument("--web-429-retry-seconds", type=float, default=60.0)
    parser.add_argument("--web-429-backoff-multiplier", type=float, default=1.5)
    parser.add_argument(
        "--web-time-budget-seconds",
        type=float,
        default=0.0,
        help="Optional wall-clock budget for web catalog fetching. Use 0 for no budget.",
    )
    parser.add_argument(
        "--web-scope-time-budget-seconds",
        type=float,
        default=0.0,
        help="Optional per app-country wall-clock budget for web catalog fetching. Use 0 for no per-scope budget.",
    )
    parser.add_argument(
        "--stop-at-rss-parity",
        action="store_true",
        help=(
            "Use the current RSS review count in Postgres as a per-app-country target. "
            "This lets web catalog fill past fixed shallow caps without endlessly deep-fetching."
        ),
    )


def command_targets(args: argparse.Namespace) -> int:
    targets = load_targets(args.targets)
    active = active_targets(targets)
    print(
        json.dumps(
            {
                "targets_path": str(args.targets),
                "target_count": len(targets),
                "active_target_count": len(active),
                "app_country_scope_count": sum(len(target.countries) for target in active),
                "categories": sorted({target.category for target in active}),
                "source": "apple_itunes_customerreviews_rss",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    targets = active_targets(load_targets(args.targets))
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    report = fetch_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        max_consecutive_empty_pages=args.max_consecutive_empty_pages,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        max_attempts=args.max_attempts,
        retry_delay_seconds=args.retry_delay_seconds,
        known_review_ids_by_scope={},
        use_overlap_stop=False,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", report["reviews"])
    write_json(raw_dir / "fetch_report.json", report)
    print(json.dumps({"raw_dir": str(raw_dir), "summary": summarize_fetch_cli(report)}, indent=2, sort_keys=True))
    return 0


def command_fetch_web_catalog(args: argparse.Namespace) -> int:
    targets = select_target_window(active_targets(load_targets(args.targets)), limit=args.limit, offset=args.target_offset)
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    report = fetch_web_catalog_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        start_page=args.start_page,
        review_limit=args.review_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        known_review_ids_by_scope={},
        use_overlap_stop=False,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", report["reviews"])
    write_json(raw_dir / "fetch_report.json", report)
    print(json.dumps({"raw_dir": str(raw_dir), "summary": summarize_fetch_cli(report)}, indent=2, sort_keys=True))
    return 0


def summarize_fetch_cli(report: dict) -> dict:
    page_reports = report.get("page_reports", [])
    warning_scopes = report.get("warning_scopes", []) or []
    status_counts: dict[str, int] = {}
    status_code_counts: dict[str, int] = {}
    attempt_counts: dict[str, int] = {}
    terminal_reasons: dict[str, int] = {}
    warning_scope_reasons: dict[str, int] = {}
    retried_pages = 0
    successful_after_retry_pages = 0
    final_non_200_pages = 0
    missing_text = 0
    missing_rating = 0
    for row in page_reports:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        status_code = row.get("status_code")
        if status_code is not None:
            key = str(status_code)
            status_code_counts[key] = status_code_counts.get(key, 0) + 1
            if not (200 <= int(status_code) < 300):
                final_non_200_pages += 1
        attempt_count = int(row.get("attempt_count") or 0)
        if attempt_count:
            key = str(attempt_count)
            attempt_counts[key] = attempt_counts.get(key, 0) + 1
        if attempt_count > 1:
            retried_pages += 1
            if row.get("status") == "ok":
                successful_after_retry_pages += 1
        reason = row.get("terminal_reason")
        if reason:
            terminal_reasons[str(reason)] = terminal_reasons.get(str(reason), 0) + 1
        missing_text += int(row.get("missing_text_count") or 0)
        missing_rating += int(row.get("missing_rating_count") or 0)
    for scope in warning_scopes:
        reason = str(scope.get("reason") or "unknown")
        warning_scope_reasons[reason] = warning_scope_reasons.get(reason, 0) + 1

    return {
        "pages": len(page_reports),
        "reviews": report.get("review_count", 0),
        "unique_reviews": report.get("unique_review_count", 0),
        "fetch_errors": report.get("fetch_errors", 0),
        "capped_scopes": len(report.get("capped_scopes", [])),
        "warning_scope_count": len(warning_scopes),
        "warning_scope_reasons": warning_scope_reasons,
        "sparse_empty_pages": report.get("sparse_empty_pages", 0),
        "status_counts": status_counts,
        "status_code_counts": status_code_counts,
        "attempt_counts": attempt_counts,
        "retried_pages": retried_pages,
        "successful_after_retry_pages": successful_after_retry_pages,
        "final_non_200_pages": final_non_200_pages,
        "terminal_reasons": terminal_reasons,
        "missing_text": missing_text,
        "missing_rating": missing_rating,
        "overall_time_budget_exceeded": bool(report.get("overall_time_budget_exceeded", False)),
        "scope_time_budget_seconds": report.get("scope_time_budget_seconds", 0),
        "all_pages_ok_after_retry": bool(page_reports) and final_non_200_pages == 0 and report.get("fetch_errors", 0) == 0,
    }


def command_init_postgres(args: argparse.Namespace) -> int:
    initialize_postgres(args.database_url)
    print(json.dumps({"database_url": mask_database_url(args.database_url), "initialized": True}, indent=2, sort_keys=True))
    return 0


def command_check_web_429_circuit_breaker(args: argparse.Namespace) -> int:
    status = web_catalog_429_circuit_breaker_status(
        args.database_url,
        source=args.source,
        since=args.since,
        lookback_minutes=args.lookback_minutes,
        min_pages=args.min_pages,
        max_rate=args.max_rate,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 2 if status["tripped"] else 0


def command_check_web_429_cooldown(args: argparse.Namespace) -> int:
    status = web_catalog_429_cooldown_status(
        args.database_url,
        source=args.source,
        cooldown_minutes=args.cooldown_minutes,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 2 if status["tripped"] else 0


def command_select_web_catalog_pressure(args: argparse.Namespace) -> int:
    status = web_catalog_pressure_status(
        args.database_url,
        source=args.source,
        lookback_minutes=args.lookback_minutes,
        base_pages=args.base_pages,
        max_pages=args.max_pages,
        base_parallel=getattr(args, "base_parallel", 1),
        max_parallel=getattr(args, "max_parallel", 4),
        base_scope_time_budget_seconds=getattr(args, "base_scope_time_budget_seconds", 1800),
        max_scope_time_budget_seconds=getattr(args, "max_scope_time_budget_seconds", 7200),
        selection_mode=getattr(args, "selection_mode", "safe"),
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def command_record_web_catalog_pressure_result(args: argparse.Namespace) -> int:
    status = record_web_catalog_pressure_result(
        args.database_url,
        source=args.source,
        since=args.since,
        used_pages=args.used_pages,
        used_parallel=getattr(args, "used_parallel", 1),
        used_scope_time_budget_seconds=getattr(args, "used_scope_time_budget_seconds", 1800),
        base_pages=args.base_pages,
        max_pages=args.max_pages,
        base_parallel=getattr(args, "base_parallel", 1),
        max_parallel=getattr(args, "max_parallel", 4),
        base_scope_time_budget_seconds=getattr(args, "base_scope_time_budget_seconds", 1800),
        max_scope_time_budget_seconds=getattr(args, "max_scope_time_budget_seconds", 7200),
        cooldown_minutes=getattr(args, "cooldown_minutes", 30),
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def command_load_postgres(args: argparse.Namespace) -> int:
    summary = load_pipeline_run_postgres(args.database_url, args.raw_dir, args.targets)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_validate_postgres(args: argparse.Namespace) -> int:
    report = validate_postgres(args.database_url, args.run_id)
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def command_probe_web(args: argparse.Namespace) -> int:
    targets = active_targets(load_targets(args.targets))
    output_path = args.output or (args.reports_root / make_run_id() / "web_probe_report.json")
    report = probe_web_reviews(
        targets,
        output_path,
        limit=args.limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        review_limit=args.review_limit,
        web_sort=args.web_sort,
        attempt_pagination=args.attempt_pagination,
        max_web_pages=args.max_web_pages,
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        include_html=not args.skip_html,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_compare_sources(args: argparse.Namespace) -> int:
    targets = active_targets(load_targets(args.targets))
    selected = select_target_window(targets, limit=args.limit, offset=args.target_offset)
    run_id = make_run_id()
    report = compare_sources(
        selected,
        run_id=run_id,
        raw_root=args.raw_root,
        reports_root=args.reports_root,
        target_offset=max(0, args.target_offset),
        rss_max_pages_per_app_country=args.rss_max_pages_per_app_country,
        rss_max_consecutive_empty_pages=args.rss_max_consecutive_empty_pages,
        rss_request_delay_seconds=args.rss_request_delay_seconds,
        rss_max_attempts=args.rss_max_attempts,
        rss_retry_delay_seconds=args.rss_retry_delay_seconds,
        web_max_pages=args.web_max_pages,
        web_review_limit=args.web_review_limit,
        web_request_delay_seconds=args.web_request_delay_seconds,
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        web_include_html=not args.web_skip_html,
        web_stop_at_rss_parity=args.web_stop_at_rss_parity,
        web_time_budget_seconds=args.web_time_budget_seconds if args.web_time_budget_seconds > 0 else None,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "output": report["paths"]["comparison_report_path"],
                "comparison": report["comparison"],
                "source_decision": report["source_decision"],
                "rss": report["rss"],
                "web_catalog": report["web_catalog"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def select_target_window(targets: list, *, limit: int, offset: int = 0) -> list:
    start = max(0, offset)
    window = targets[start:]
    return window[:limit] if limit > 0 else window


def command_probe_42matters(args: argparse.Namespace) -> int:
    if not args.access_token:
        print("error: missing 42matters token; pass --access-token or set APP_STORE_42MATTERS_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    output_path = args.output or (args.reports_root / make_run_id() / "provider_probe_report.json")
    report = probe_42matters_reviews(
        targets,
        output_path,
        access_token=args.access_token,
        limit=args.limit,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        lang=args.lang,
        rating=args.rating,
        page_limit=args.page_limit,
        request_limit=args.request_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_compare_42matters(args: argparse.Namespace) -> int:
    if not args.access_token:
        print("error: missing 42matters token; pass --access-token or set APP_STORE_42MATTERS_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    selected = targets[: args.limit] if args.limit > 0 else targets
    run_id = make_run_id()
    report = compare_rss_with_42matters(
        selected,
        run_id=run_id,
        raw_root=args.raw_root,
        reports_root=args.reports_root,
        access_token=args.access_token,
        rss_max_pages_per_app_country=args.rss_max_pages_per_app_country,
        rss_max_consecutive_empty_pages=args.rss_max_consecutive_empty_pages,
        rss_request_delay_seconds=args.rss_request_delay_seconds,
        rss_max_attempts=args.rss_max_attempts,
        rss_retry_delay_seconds=args.rss_retry_delay_seconds,
        provider_days=args.provider_days,
        provider_start_date=args.provider_start_date,
        provider_end_date=args.provider_end_date,
        provider_lang=args.provider_lang,
        provider_rating=args.provider_rating,
        provider_page_limit=args.provider_page_limit,
        provider_request_limit=args.provider_request_limit,
        provider_request_delay_seconds=args.provider_request_delay_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "output": report["paths"]["comparison_report_path"],
                "comparison": report["comparison"],
                "rss": report["rss"],
                "provider": report["provider"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_probe_apptweak(args: argparse.Namespace) -> int:
    if not args.api_token:
        print("error: missing AppTweak token; pass --api-token or set APP_STORE_APPTWEAK_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    output_path = args.output or (args.reports_root / make_run_id() / "provider_probe_report.json")
    report = probe_apptweak_reviews(
        targets,
        output_path,
        api_token=args.api_token,
        limit=args.limit,
        country_fallback=args.country_fallback,
        language=args.language,
        device=args.device,
        start_date=args.start_date,
        end_date=args.end_date,
        term=args.term,
        page_limit=args.page_limit,
        request_limit=args.request_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_compare_apptweak(args: argparse.Namespace) -> int:
    if not args.api_token:
        print("error: missing AppTweak token; pass --api-token or set APP_STORE_APPTWEAK_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    selected = targets[: args.limit] if args.limit > 0 else targets
    run_id = make_run_id()
    report = compare_rss_with_apptweak(
        selected,
        run_id=run_id,
        raw_root=args.raw_root,
        reports_root=args.reports_root,
        api_token=args.api_token,
        rss_max_pages_per_app_country=args.rss_max_pages_per_app_country,
        rss_max_consecutive_empty_pages=args.rss_max_consecutive_empty_pages,
        rss_request_delay_seconds=args.rss_request_delay_seconds,
        rss_max_attempts=args.rss_max_attempts,
        rss_retry_delay_seconds=args.rss_retry_delay_seconds,
        provider_country_fallback=args.provider_country_fallback,
        provider_language=args.provider_language,
        provider_device=args.provider_device,
        provider_start_date=args.provider_start_date,
        provider_end_date=args.provider_end_date,
        provider_term=args.provider_term,
        provider_page_limit=args.provider_page_limit,
        provider_request_limit=args.provider_request_limit,
        provider_request_delay_seconds=args.provider_request_delay_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "output": report["paths"]["comparison_report_path"],
                "comparison": report["comparison"],
                "rss": report["rss"],
                "provider": report["provider"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_probe_appfigures(args: argparse.Namespace) -> int:
    if not args.access_token:
        print("error: missing Appfigures token; pass --access-token or set APP_STORE_APPFIGURES_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    output_path = args.output or (args.reports_root / make_run_id() / "provider_probe_report.json")
    report = probe_appfigures_reviews(
        targets,
        output_path,
        access_token=args.access_token,
        limit=args.limit,
        country_fallback=args.country_fallback,
        page_limit=args.page_limit,
        request_limit=args.request_limit,
        sort=args.sort,
        start_date=args.start_date,
        end_date=args.end_date,
        lang=args.lang,
        stars=args.stars,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_compare_appfigures(args: argparse.Namespace) -> int:
    if not args.access_token:
        print("error: missing Appfigures token; pass --access-token or set APP_STORE_APPFIGURES_TOKEN")
        return 2
    targets = active_targets(load_targets(args.targets))
    selected = targets[: args.limit] if args.limit > 0 else targets
    run_id = make_run_id()
    report = compare_rss_with_appfigures(
        selected,
        run_id=run_id,
        raw_root=args.raw_root,
        reports_root=args.reports_root,
        access_token=args.access_token,
        rss_max_pages_per_app_country=args.rss_max_pages_per_app_country,
        rss_max_consecutive_empty_pages=args.rss_max_consecutive_empty_pages,
        rss_request_delay_seconds=args.rss_request_delay_seconds,
        rss_max_attempts=args.rss_max_attempts,
        rss_retry_delay_seconds=args.rss_retry_delay_seconds,
        provider_country_fallback=args.provider_country_fallback,
        provider_page_limit=args.provider_page_limit,
        provider_request_limit=args.provider_request_limit,
        provider_sort=args.provider_sort,
        provider_start_date=args.provider_start_date,
        provider_end_date=args.provider_end_date,
        provider_lang=args.provider_lang,
        provider_stars=args.provider_stars,
        provider_request_delay_seconds=args.provider_request_delay_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "output": report["paths"]["comparison_report_path"],
                "comparison": report["comparison"],
                "rss": report["rss"],
                "provider": report["provider"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_daily(args: argparse.Namespace) -> int:
    report = run_daily_pipeline(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_daily_web_catalog(args: argparse.Namespace) -> int:
    started_at = utc_timestamp()
    run_id = make_run_id()
    raw_dir = args.raw_root / run_id
    reports_dir = args.reports_root / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    targets = select_target_window(active_targets(load_targets(args.targets)), limit=args.limit, offset=args.target_offset)
    scopes = [(target.apple_app_id, country, args.sort_by) for target in targets for country in target.countries]
    use_overlap_stop = not getattr(args, "disable_overlap_stop", False)
    known_ids = (
        existing_review_ids_by_scope(args.database_url, scopes, source=WEB_CATALOG_SOURCE)
        if use_overlap_stop
        else {}
    )
    target_review_counts = (
        review_counts_by_scope(args.database_url, scopes, source=SOURCE)
        if getattr(args, "stop_at_rss_parity", False)
        else {}
    )
    fetch_report = fetch_web_catalog_targets(
        targets,
        raw_dir,
        run_id,
        sort_by=args.sort_by,
        max_pages_per_app_country=args.max_pages_per_app_country,
        start_page=args.start_page,
        review_limit=args.review_limit,
        timeout_seconds=args.timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        web_429_retries=args.web_429_retries,
        web_429_retry_seconds=args.web_429_retry_seconds,
        web_429_backoff_multiplier=args.web_429_backoff_multiplier,
        time_budget_seconds=args.web_time_budget_seconds,
        scope_time_budget_seconds=args.web_scope_time_budget_seconds,
        known_review_ids_by_scope=known_ids,
        target_review_counts_by_scope=target_review_counts,
        use_overlap_stop=use_overlap_stop,
    )
    write_jsonl(raw_dir / "review_pages.jsonl", fetch_report["page_reports"])
    write_jsonl(raw_dir / "reviews.jsonl", fetch_report["reviews"])
    write_json(raw_dir / "fetch_report.json", fetch_report)
    load_summary = load_pipeline_run_postgres(args.database_url, raw_dir, args.targets)
    validation_report = validate_postgres(args.database_url, run_id)
    completed_at = utc_timestamp()
    validation_path = reports_dir / "validation_report.json"
    write_json(validation_path, validation_report)
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "targets_path": str(args.targets),
        "raw_dir": str(raw_dir),
        "database_url": mask_database_url(args.database_url),
        "storage_backend": "postgres",
        "platform": "apple_app_store",
        "source": WEB_CATALOG_SOURCE,
        "sort_by": args.sort_by,
        "target_count": len(targets),
        "scope_count": len(scopes),
        "target_offset": max(0, args.target_offset),
        "max_pages_per_app_country": args.max_pages_per_app_country,
        "start_page": args.start_page,
        "review_limit": args.review_limit,
        "web_time_budget_seconds": args.web_time_budget_seconds,
        "web_scope_time_budget_seconds": args.web_scope_time_budget_seconds,
        "overlap_stop_enabled": use_overlap_stop,
        "stop_at_rss_parity": bool(getattr(args, "stop_at_rss_parity", False)),
        "rss_parity_source": SOURCE if getattr(args, "stop_at_rss_parity", False) else None,
        "rss_parity_target_scope_count": len(target_review_counts),
        "fetch_summary": summarize_fetch_cli(fetch_report),
        "load_summary": load_summary,
        "validation_report_path": str(validation_path),
        "report_path": str(reports_dir / "daily_report.json"),
    }
    write_json(reports_dir / "daily_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
