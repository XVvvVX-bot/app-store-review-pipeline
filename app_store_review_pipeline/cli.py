from __future__ import annotations

import argparse
import json
from pathlib import Path

from app_store_review_pipeline.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_CONSECUTIVE_EMPTY_PAGES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_PAGES_PER_APP_COUNTRY,
    DEFAULT_RAW_ROOT,
    DEFAULT_REPORTS_ROOT,
    DEFAULT_REQUEST_DELAY_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_SORT_BY,
    DEFAULT_TARGETS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WEB_REPORTS_ROOT,
)
from app_store_review_pipeline.apple_web import probe_web_reviews
from app_store_review_pipeline.daily import run_daily_pipeline
from app_store_review_pipeline.fetcher import fetch_targets
from app_store_review_pipeline.files import write_json, write_jsonl
from app_store_review_pipeline.postgres_database import (
    initialize_postgres,
    load_pipeline_run_postgres,
    mask_database_url,
    validate_postgres,
)
from app_store_review_pipeline.targets import active_targets, load_targets
from app_store_review_pipeline.utils import make_run_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple App Store public review ingestion pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    targets = subparsers.add_parser("targets", help="Summarize target apps and app-country scopes.")
    targets.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    targets.set_defaults(func=command_targets)

    fetch = subparsers.add_parser("fetch", help="Fetch raw Apple RSS review pages.")
    add_fetch_arguments(fetch)
    fetch.set_defaults(func=command_fetch)

    init_postgres = subparsers.add_parser("init-postgres", help="Create or update the Postgres schema.")
    init_postgres.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    init_postgres.set_defaults(func=command_init_postgres)

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
        help="Requested sparse review limit for the public web catalog app lookup.",
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
    probe_web.set_defaults(func=command_probe_web)

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


def summarize_fetch_cli(report: dict) -> dict:
    return {
        "pages": len(report.get("page_reports", [])),
        "reviews": report.get("review_count", 0),
        "unique_reviews": report.get("unique_review_count", 0),
        "fetch_errors": report.get("fetch_errors", 0),
        "capped_scopes": len(report.get("capped_scopes", [])),
        "sparse_empty_pages": report.get("sparse_empty_pages", 0),
    }


def command_init_postgres(args: argparse.Namespace) -> int:
    initialize_postgres(args.database_url)
    print(json.dumps({"database_url": mask_database_url(args.database_url), "initialized": True}, indent=2, sort_keys=True))
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
        attempt_pagination=args.attempt_pagination,
        max_web_pages=args.max_web_pages,
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


def command_daily(args: argparse.Namespace) -> int:
    report = run_daily_pipeline(args)
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
