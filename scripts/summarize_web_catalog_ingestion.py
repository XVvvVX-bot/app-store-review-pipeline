from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_store_review_pipeline.config import WEB_CATALOG_SOURCE
from app_store_review_pipeline.postgres_database import connect_postgres, mask_database_url


DEFAULT_ROOT = Path("data/reports/apple_web_catalog")


def find_daily_report_paths(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if root.is_file() and root.name == "daily_report.json":
            paths.append(root)
        elif root.exists():
            paths.extend(root.rglob("daily_report.json"))
    return sorted(set(paths))


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return value


def summarize_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    fetch = enrich_fetch_summary(report.get("fetch_summary") or {}, find_related_fetch_report(path, report))
    load = report.get("load_summary") or {}
    status_code_counts = fetch.get("status_code_counts") or {}
    attempt_counts = fetch.get("attempt_counts") or {}
    terminal_reasons = fetch.get("terminal_reasons") or {}
    pages = int_or_zero(fetch.get("pages"))
    reviews = int_or_zero(fetch.get("unique_reviews", fetch.get("reviews")))
    target_count = int_or_zero(report.get("target_count"))
    scope_count = int_or_zero(report.get("scope_count"))
    max_pages = int_or_zero(report.get("max_pages_per_app_country"))
    start_page = int_or_zero(report.get("start_page")) or 1
    review_limit = int_or_zero(report.get("review_limit"))
    configured_ceiling = scope_count * max_pages * review_limit if scope_count and max_pages and review_limit else 0
    review_ratio_to_ceiling = reviews / configured_ceiling if configured_ceiling else None
    all_pages_ok = bool(fetch.get("all_pages_ok_after_retry"))
    final_non_200 = int_or_zero(fetch.get("final_non_200_pages"))
    fetch_errors = int_or_zero(fetch.get("fetch_errors"))
    missing_text = int_or_zero(fetch.get("missing_text"))
    missing_rating = int_or_zero(fetch.get("missing_rating"))
    inserted = int_or_zero(load.get("inserted"))
    updated = int_or_zero(load.get("updated"))
    duplicates = int_or_zero(load.get("duplicates_skipped"))

    return {
        "path": str(path),
        "run_id": report.get("run_id") or path.parent.name,
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "source": report.get("source"),
        "target_count": target_count,
        "scope_count": scope_count,
        "target_offset": report.get("target_offset"),
        "max_pages_per_app_country": max_pages,
        "start_page": start_page,
        "review_limit": review_limit,
        "configured_review_ceiling": configured_ceiling,
        "reviews": reviews,
        "review_ratio_to_ceiling": review_ratio_to_ceiling,
        "pages": pages,
        "status_code_counts": status_code_counts,
        "attempt_counts": attempt_counts,
        "terminal_reasons": terminal_reasons,
        "retried_pages": int_or_zero(fetch.get("retried_pages")),
        "successful_after_retry_pages": int_or_zero(fetch.get("successful_after_retry_pages")),
        "final_non_200_pages": final_non_200,
        "fetch_errors": fetch_errors,
        "missing_text": missing_text,
        "missing_rating": missing_rating,
        "all_pages_ok_after_retry": all_pages_ok,
        "inserted": inserted,
        "updated": updated,
        "duplicates_skipped": duplicates,
        "is_full_single_app_profile": target_count == 1
        and scope_count == 1
        and start_page == 1
        and max_pages >= 25
        and review_limit >= 20,
        "is_clean": all_pages_ok and final_non_200 == 0 and fetch_errors == 0 and missing_text == 0 and missing_rating == 0,
        "reached_configured_ceiling": configured_ceiling > 0 and reviews >= configured_ceiling,
        "loaded_any_rows": (inserted + updated + duplicates) > 0,
    }


def find_related_fetch_report(daily_report_path: Path, report: dict[str, Any]) -> dict[str, Any] | None:
    run_id = str(report.get("run_id") or daily_report_path.parent.name)
    candidates: list[Path] = []
    for ancestor in daily_report_path.parents:
        candidates.extend(
            [
                ancestor / "raw" / "apple_web_catalog" / run_id / "fetch_report.json",
                ancestor / "data" / "raw" / "apple_web_catalog" / run_id / "fetch_report.json",
            ]
        )
    raw_dir = report.get("raw_dir")
    if raw_dir:
        candidates.append(Path(str(raw_dir)) / "fetch_report.json")
    for candidate in candidates:
        if candidate.exists():
            return load_json_object(candidate)
    return None


def enrich_fetch_summary(fetch_summary: dict[str, Any], fetch_report: dict[str, Any] | None) -> dict[str, Any]:
    summary = dict(fetch_summary)
    if not fetch_report:
        return summary

    page_reports = fetch_report.get("page_reports") or []
    if "pages" not in summary:
        summary["pages"] = len(page_reports)
    if "reviews" not in summary:
        summary["reviews"] = fetch_report.get("review_count", 0)
    if "unique_reviews" not in summary:
        summary["unique_reviews"] = fetch_report.get("unique_review_count", summary.get("reviews", 0))
    if "fetch_errors" not in summary:
        summary["fetch_errors"] = fetch_report.get("fetch_errors", 0)
    if "sparse_empty_pages" not in summary:
        summary["sparse_empty_pages"] = fetch_report.get("sparse_empty_pages", 0)

    derived = derive_page_stability_metrics(page_reports, int_or_zero(summary.get("fetch_errors")))
    for key, value in derived.items():
        summary.setdefault(key, value)
    return summary


def derive_page_stability_metrics(page_reports: list[dict[str, Any]], fetch_errors: int) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    status_code_counts: Counter[str] = Counter()
    attempt_counts: Counter[str] = Counter()
    terminal_reasons: Counter[str] = Counter()
    retried_pages = 0
    successful_after_retry_pages = 0
    final_non_200_pages = 0
    missing_text = 0
    missing_rating = 0
    for row in page_reports:
        status_counts[str(row.get("status") or "unknown")] += 1
        status_code = row.get("status_code")
        if status_code is not None:
            status_code_counts[str(status_code)] += 1
            if not (200 <= int(status_code) < 300):
                final_non_200_pages += 1
        attempt_count = int_or_zero(row.get("attempt_count"))
        if attempt_count:
            attempt_counts[str(attempt_count)] += 1
        if attempt_count > 1:
            retried_pages += 1
            if row.get("status") == "ok":
                successful_after_retry_pages += 1
        reason = row.get("terminal_reason")
        if reason:
            terminal_reasons[str(reason)] += 1
        missing_text += int_or_zero(row.get("missing_text_count"))
        missing_rating += int_or_zero(row.get("missing_rating_count"))
    return {
        "status_counts": dict(sorted(status_counts.items())),
        "status_code_counts": dict(sorted(status_code_counts.items())),
        "attempt_counts": dict(sorted(attempt_counts.items())),
        "retried_pages": retried_pages,
        "successful_after_retry_pages": successful_after_retry_pages,
        "final_non_200_pages": final_non_200_pages,
        "terminal_reasons": dict(sorted(terminal_reasons.items())),
        "missing_text": missing_text,
        "missing_rating": missing_rating,
        "all_pages_ok_after_retry": bool(page_reports) and final_non_200_pages == 0 and fetch_errors == 0,
    }


def summarize_history_from_reports(
    paths: list[Path],
    *,
    min_runs: int = 5,
    full_single_app_only: bool = False,
    min_reviews_per_run: int = 500,
    database_url: str | None = None,
) -> dict[str, Any]:
    records = [summarize_report(path, load_json_object(path)) for path in paths]
    records = [record for record in records if record.get("source") == WEB_CATALOG_SOURCE]
    if full_single_app_only:
        records = [record for record in records if record.get("is_full_single_app_profile")]
    records.sort(key=lambda record: (str(record.get("started_at") or ""), str(record.get("run_id") or "")))

    clean_records = [record for record in records if record.get("is_clean")]
    volume_records = [record for record in records if int_or_zero(record.get("reviews")) >= min_reviews_per_run]
    full_ceiling_records = [record for record in records if record.get("reached_configured_ceiling")]
    failed_records = [record for record in records if not record.get("is_clean")]

    blocking_reasons: list[str] = []
    if not records:
        blocking_reasons.append("no_matching_reports")
    if len(records) < min_runs:
        blocking_reasons.append(f"needs_at_least_{min_runs}_runs")
    if len(clean_records) != len(records):
        blocking_reasons.append("one_or_more_runs_not_clean")
    if len(volume_records) != len(records):
        blocking_reasons.append(f"one_or_more_runs_below_{min_reviews_per_run}_reviews")
    if len(full_ceiling_records) != len(records):
        blocking_reasons.append("one_or_more_runs_did_not_reach_configured_review_ceiling")

    ready = bool(records) and not blocking_reasons
    if not records:
        status = "no_matching_reports"
    elif ready:
        status = "ready_for_controlled_promotion"
    elif clean_records and len(clean_records) == len(records) and len(records) < min_runs:
        status = "needs_more_evidence"
    else:
        status = "not_ready"

    database_summary = summarize_database(database_url) if database_url else None
    return {
        "generated_from_report_count": len(records),
        "full_single_app_only": full_single_app_only,
        "min_reviews_per_run": min_reviews_per_run,
        "promotion_gate": {
            "status": status,
            "ready_for_controlled_promotion": ready,
            "min_runs": min_runs,
            "blocking_reasons": blocking_reasons,
        },
        "aggregate": {
            "clean_runs": len(clean_records),
            "failed_or_partial_runs": len(failed_records),
            "runs_at_or_above_min_reviews": len(volume_records),
            "runs_reaching_configured_ceiling": len(full_ceiling_records),
            "reviews_total": sum(int_or_zero(record.get("reviews")) for record in records),
            "inserted_total": sum(int_or_zero(record.get("inserted")) for record in records),
            "updated_total": sum(int_or_zero(record.get("updated")) for record in records),
            "duplicates_skipped_total": sum(int_or_zero(record.get("duplicates_skipped")) for record in records),
            "pages_total": sum(int_or_zero(record.get("pages")) for record in records),
            "retried_pages_total": sum(int_or_zero(record.get("retried_pages")) for record in records),
            "successful_after_retry_pages_total": sum(
                int_or_zero(record.get("successful_after_retry_pages")) for record in records
            ),
            "final_non_200_pages_total": sum(int_or_zero(record.get("final_non_200_pages")) for record in records),
            "fetch_errors_total": sum(int_or_zero(record.get("fetch_errors")) for record in records),
            "missing_text_total": sum(int_or_zero(record.get("missing_text")) for record in records),
            "missing_rating_total": sum(int_or_zero(record.get("missing_rating")) for record in records),
            "status_code_counts": merge_counter(record.get("status_code_counts") for record in records),
            "attempt_counts": merge_counter(record.get("attempt_counts") for record in records),
            "terminal_reasons": merge_counter(record.get("terminal_reasons") for record in records),
        },
        "database": database_summary,
        "runs": records,
    }


def summarize_database(database_url: str) -> dict[str, Any]:
    with connect_postgres(database_url) as connection:
        source_rows = connection.execute(
            """
            SELECT
                source,
                COUNT(*) AS review_rows,
                COUNT(DISTINCT app_id) AS app_count,
                MIN(updated_at) AS oldest_review,
                MAX(updated_at) AS newest_review
            FROM app_store_reviews
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()
        web_app_rows = connection.execute(
            """
            SELECT app_id, app_name, COUNT(*) AS review_rows
            FROM app_store_reviews
            WHERE source = %s
            GROUP BY app_id, app_name
            ORDER BY review_rows DESC, app_name
            """,
            (WEB_CATALOG_SOURCE,),
        ).fetchall()
    return {
        "database_url": mask_database_url(database_url),
        "source_rows": [dict(row) for row in source_rows],
        "web_catalog_apps": [dict(row) for row in web_app_rows],
    }


def merge_counter(values: Any) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, count in value.items():
            counter[str(key)] += int_or_zero(count)
    return dict(sorted(counter.items()))


def render_markdown_summary(summary: dict[str, Any]) -> str:
    gate = summary.get("promotion_gate") or {}
    aggregate = summary.get("aggregate") or {}
    database = summary.get("database") or {}
    lines = [
        "# App Store Web Catalog Ingestion History",
        "",
        f"Promotion status: **{gate.get('status', 'unknown')}**",
        "",
        f"- Reports summarized: `{summary.get('generated_from_report_count', 0)}`",
        f"- Full single-app only filter: `{bool_label(summary.get('full_single_app_only'))}`",
        f"- Minimum reviews per run: `{summary.get('min_reviews_per_run')}`",
        f"- Minimum clean runs required: `{gate.get('min_runs')}`",
        f"- Blocking reasons: `{gate.get('blocking_reasons') or []}`",
        "",
        "## Aggregate",
        "",
        f"- Clean runs: `{aggregate.get('clean_runs', 0)}`",
        f"- Runs at or above review floor: `{aggregate.get('runs_at_or_above_min_reviews', 0)}`",
        f"- Runs reaching configured ceiling: `{aggregate.get('runs_reaching_configured_ceiling', 0)}`",
        f"- Reviews fetched: `{aggregate.get('reviews_total', 0)}`",
        f"- Rows inserted: `{aggregate.get('inserted_total', 0)}`",
        f"- Rows updated: `{aggregate.get('updated_total', 0)}`",
        f"- Duplicate rows skipped: `{aggregate.get('duplicates_skipped_total', 0)}`",
        f"- Pages fetched: `{aggregate.get('pages_total', 0)}`",
        f"- Retried pages: `{aggregate.get('retried_pages_total', 0)}`",
        f"- Final non-200 pages: `{aggregate.get('final_non_200_pages_total', 0)}`",
        f"- Fetch errors: `{aggregate.get('fetch_errors_total', 0)}`",
        f"- Missing text/rating: `{aggregate.get('missing_text_total', 0)} / {aggregate.get('missing_rating_total', 0)}`",
        f"- Status code counts: `{aggregate.get('status_code_counts', {})}`",
        f"- Attempt counts: `{aggregate.get('attempt_counts', {})}`",
        f"- Terminal reasons: `{aggregate.get('terminal_reasons', {})}`",
        "",
    ]
    if database:
        lines.extend(
            [
                "## Database",
                "",
                f"- Database URL: `{database.get('database_url')}`",
                "",
                "| Source | Rows | Apps | Oldest Review | Newest Review |",
                "| --- | ---: | ---: | --- | --- |",
            ]
        )
        for row in database.get("source_rows") or []:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(str(row.get("source") or "")),
                        str(row.get("review_rows") or 0),
                        str(row.get("app_count") or 0),
                        markdown_escape(str(row.get("oldest_review") or "")),
                        markdown_escape(str(row.get("newest_review") or "")),
                    ]
                )
                + " |"
            )
        lines.extend(["", "### Web Catalog Apps", "", "| App | App ID | Rows |", "| --- | --- | ---: |"])
        for row in database.get("web_catalog_apps") or []:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(str(row.get("app_name") or "")),
                        markdown_escape(str(row.get("app_id") or "")),
                        str(row.get("review_rows") or 0),
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(
        [
            "## Runs",
            "",
            "| Run | Offset | Start | Max Page | Targets | Pages | Reviews | Inserted | Updated | Duplicates | Status Codes | Attempts | Final Non-200 | Clean | Ceiling |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- | --- |",
        ]
    )
    for record in summary.get("runs") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(str(record.get("run_id") or "")),
                    markdown_escape(str(record.get("target_offset") if record.get("target_offset") is not None else "")),
                    str(record.get("start_page") or 1),
                    str(record.get("max_pages_per_app_country") or 0),
                    str(record.get("target_count") or 0),
                    str(record.get("pages") or 0),
                    str(record.get("reviews") or 0),
                    str(record.get("inserted") or 0),
                    str(record.get("updated") or 0),
                    str(record.get("duplicates_skipped") or 0),
                    markdown_escape(str(record.get("status_code_counts") or {})),
                    markdown_escape(str(record.get("attempt_counts") or {})),
                    str(record.get("final_non_200_pages") or 0),
                    bool_label(record.get("is_clean")),
                    bool_label(record.get("reached_configured_ceiling")),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(summary: dict[str, Any], output_json: Path | None, output_markdown: Path | None) -> None:
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if output_markdown:
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_markdown_summary(summary), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize App Store web catalog ingestion daily reports.")
    parser.add_argument(
        "--root",
        type=Path,
        nargs="+",
        default=[DEFAULT_ROOT],
        help="Report root(s), artifact root(s), or daily_report.json file(s) to summarize.",
    )
    parser.add_argument("--output-json", type=Path, help="Optional path for the JSON ingestion history summary.")
    parser.add_argument("--output-markdown", type=Path, help="Optional path for the Markdown ingestion history report.")
    parser.add_argument("--min-runs", type=int, default=5, help="Clean ingestion runs required for promotion.")
    parser.add_argument(
        "--full-single-app-only",
        action="store_true",
        help="Only include reports matching the conservative 25-page single-app profile.",
    )
    parser.add_argument(
        "--min-reviews-per-run",
        type=int,
        default=500,
        help="Minimum unique reviews required for each included run.",
    )
    parser.add_argument(
        "--database-url",
        help="Optional Postgres URL to include cumulative source/app row counts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = find_daily_report_paths(args.root)
    summary = summarize_history_from_reports(
        paths,
        min_runs=args.min_runs,
        full_single_app_only=args.full_single_app_only,
        min_reviews_per_run=args.min_reviews_per_run,
        database_url=args.database_url,
    )
    write_outputs(summary, args.output_json, args.output_markdown)
    print(json.dumps(summary["promotion_gate"], indent=2, sort_keys=True))
    print(json.dumps(summary["aggregate"], indent=2, sort_keys=True))
    if not paths:
        return 1
    return 0


def int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def bool_label(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
