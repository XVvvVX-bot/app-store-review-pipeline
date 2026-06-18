from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_store_review_pipeline.source_compare import build_web_source_decision


DEFAULT_ROOT = Path("data/reports/source_compare")


def find_report_paths(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if root.is_file() and root.name == "source_comparison_report.json":
            paths.append(root)
        elif root.exists():
            paths.extend(root.rglob("source_comparison_report.json"))
    return sorted(set(paths))


def load_report(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return report


def summarize_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    decision = report.get("source_decision") or build_web_source_decision(report)
    settings = report.get("settings") or {}
    rss = report.get("rss") or {}
    web = report.get("web_catalog") or {}
    metrics = report.get("comparison") or {}
    per_scope = report.get("per_scope") or []
    app_names = unique_non_empty(scope.get("app_name") for scope in per_scope if isinstance(scope, dict))
    page_status_counts = web.get("web_catalog_page_status_counts") or {}
    stop_reasons = web.get("web_catalog_stop_reasons") or {}
    target_count = int_or_none(report.get("target_count"))
    scope_count = int_or_none(report.get("scope_count"))
    rss_reviews = int_or_zero(metrics.get("rss_unique_reviews_seen", rss.get("unique_reviews_seen")))
    web_reviews = int_or_zero(metrics.get("web_catalog_page_reviews_total", web.get("web_catalog_page_reviews_total")))
    ratio = metrics.get("web_to_rss_review_ratio")
    if ratio is None and rss_reviews:
        ratio = web_reviews / rss_reviews

    return {
        "path": str(path),
        "run_id": report.get("run_id") or path.parent.name,
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "status": decision.get("status", "unknown"),
        "selected_source": decision.get("selected_source"),
        "target_count": target_count,
        "scope_count": scope_count,
        "target_offset": settings.get("target_offset"),
        "app_names": app_names,
        "rss_unique_reviews": rss_reviews,
        "web_reviews": web_reviews,
        "web_to_rss_ratio": ratio,
        "web_non_200_pages_after_retry": int_or_zero(metrics.get("web_non_200_page_count_after_retry")),
        "web_unrecovered_429_pages": int_or_zero(metrics.get("web_unrecovered_429_page_count")),
        "web_recovered_429_pages": int_or_zero(metrics.get("web_recovered_429_page_count")),
        "web_retried_pages": int_or_zero(metrics.get("web_retried_page_count")),
        "web_all_pages_ok_after_retry": bool(metrics.get("web_all_pages_ok_after_retry")),
        "web_time_budget_exceeded": bool(metrics.get("web_time_budget_exceeded")),
        "web_planned_scope_count": metrics.get("web_planned_scope_count"),
        "web_completed_scope_count": metrics.get("web_completed_scope_count"),
        "web_skipped_scope_count": metrics.get("web_skipped_scope_count"),
        "web_all_scopes_completed": metrics.get("web_all_scopes_completed") is not False,
        "web_page_status_counts": page_status_counts,
        "web_stop_reasons": stop_reasons,
        "web_max_pages": settings.get("web_max_pages"),
        "web_review_limit": settings.get("web_review_limit"),
        "web_request_delay_seconds": settings.get("web_request_delay_seconds"),
        "web_429_retries": settings.get("web_429_retries"),
        "web_429_retry_seconds": settings.get("web_429_retry_seconds"),
        "web_429_backoff_multiplier": settings.get("web_429_backoff_multiplier"),
        "web_stop_at_rss_parity": settings.get("web_stop_at_rss_parity"),
        "web_time_budget_seconds": settings.get("web_time_budget_seconds"),
    }


def summarize_history_from_reports(
    paths: list[Path],
    *,
    min_runs: int = 5,
    single_app_only: bool = False,
    min_web_max_pages: int | None = None,
) -> dict[str, Any]:
    records = [summarize_report(path, load_report(path)) for path in paths]
    if single_app_only:
        records = [
            record
            for record in records
            if (record.get("target_count") in (None, 1)) and (record.get("scope_count") in (None, 1))
        ]
    if min_web_max_pages is not None:
        records = [
            record
            for record in records
            if int_or_zero(record.get("web_max_pages")) >= min_web_max_pages
        ]
    records.sort(key=lambda record: (str(record.get("started_at") or ""), str(record.get("run_id") or "")))

    status_counts = Counter(record["status"] for record in records)
    candidate_records = [
        record for record in records if record.get("status") == "web_catalog_replacement_candidate"
    ]
    budget_records = [record for record in records if record.get("web_time_budget_exceeded")]
    non_200_records = [
        record for record in records if int_or_zero(record.get("web_non_200_pages_after_retry")) > 0
    ]
    incomplete_scope_records = [
        record for record in records if record.get("web_all_scopes_completed") is False
    ]
    ratios = [
        float(record["web_to_rss_ratio"])
        for record in records
        if isinstance(record.get("web_to_rss_ratio"), (int, float))
    ]

    blocking_reasons: list[str] = []
    if not records:
        blocking_reasons.append("no_matching_reports")
    if len(records) < min_runs:
        blocking_reasons.append(f"needs_at_least_{min_runs}_runs")
    if len(candidate_records) != len(records):
        blocking_reasons.append("not_all_runs_are_replacement_candidates")
    if budget_records:
        blocking_reasons.append("one_or_more_runs_exceeded_time_budget")
    if non_200_records:
        blocking_reasons.append("one_or_more_runs_have_final_non_200_pages")
    if incomplete_scope_records:
        blocking_reasons.append("one_or_more_runs_did_not_complete_planned_scopes")

    ready = bool(records) and not blocking_reasons
    if not records:
        promotion_status = "no_matching_reports"
    elif ready:
        promotion_status = "ready_for_promotion"
    elif len(records) < min_runs and len(candidate_records) == len(records) and not (
        budget_records or non_200_records or incomplete_scope_records
    ):
        promotion_status = "needs_more_evidence"
    else:
        promotion_status = "not_ready"

    return {
        "generated_from_report_count": len(records),
        "single_app_only": single_app_only,
        "min_web_max_pages": min_web_max_pages,
        "promotion_gate": {
            "status": promotion_status,
            "ready_for_promotion": ready,
            "min_runs": min_runs,
            "blocking_reasons": blocking_reasons,
        },
        "aggregate": {
            "status_counts": dict(sorted(status_counts.items())),
            "replacement_candidate_runs": len(candidate_records),
            "time_budget_exceeded_runs": len(budget_records),
            "runs_with_final_non_200_pages": len(non_200_records),
            "runs_with_incomplete_scopes": len(incomplete_scope_records),
            "rss_unique_reviews_total": sum(int_or_zero(record.get("rss_unique_reviews")) for record in records),
            "web_reviews_total": sum(int_or_zero(record.get("web_reviews")) for record in records),
            "web_recovered_429_pages_total": sum(
                int_or_zero(record.get("web_recovered_429_pages")) for record in records
            ),
            "web_unrecovered_429_pages_total": sum(
                int_or_zero(record.get("web_unrecovered_429_pages")) for record in records
            ),
            "web_retried_pages_total": sum(int_or_zero(record.get("web_retried_pages")) for record in records),
            "average_web_to_rss_ratio": sum(ratios) / len(ratios) if ratios else None,
            "minimum_web_to_rss_ratio": min(ratios) if ratios else None,
        },
        "runs": records,
    }


def render_markdown_summary(summary: dict[str, Any]) -> str:
    gate = summary.get("promotion_gate") or {}
    aggregate = summary.get("aggregate") or {}
    lines = [
        "# App Store Source Comparison History",
        "",
        f"Promotion status: **{gate.get('status', 'unknown')}**",
        "",
        f"- Reports summarized: `{summary.get('generated_from_report_count', 0)}`",
        f"- Single-app only filter: `{bool_label(summary.get('single_app_only'))}`",
        f"- Minimum web max pages filter: `{summary.get('min_web_max_pages')}`",
        f"- Minimum clean runs required: `{gate.get('min_runs')}`",
        f"- Blocking reasons: `{gate.get('blocking_reasons') or []}`",
        "",
        "## Aggregate",
        "",
        f"- Status counts: `{aggregate.get('status_counts', {})}`",
        f"- Replacement candidate runs: `{aggregate.get('replacement_candidate_runs', 0)}`",
        f"- Time-budget exceeded runs: `{aggregate.get('time_budget_exceeded_runs', 0)}`",
        f"- Runs with final non-200 pages: `{aggregate.get('runs_with_final_non_200_pages', 0)}`",
        f"- Total RSS reviews: `{aggregate.get('rss_unique_reviews_total', 0)}`",
        f"- Total web catalog reviews: `{aggregate.get('web_reviews_total', 0)}`",
        f"- Recovered 429 pages: `{aggregate.get('web_recovered_429_pages_total', 0)}`",
        f"- Unrecovered 429 pages: `{aggregate.get('web_unrecovered_429_pages_total', 0)}`",
        f"- Average web/RSS ratio: `{format_ratio(aggregate.get('average_web_to_rss_ratio'))}`",
        "",
        "## Runs",
        "",
        "| Run | Apps | Offset | Targets | Decision | RSS | Web | Ratio | Final Non-200 | 429 Recovered / Unrecovered | Budget | Stop Reasons |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for record in summary.get("runs") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(str(record.get("run_id") or "")),
                    markdown_escape(", ".join(record.get("app_names") or [])[:80]),
                    markdown_escape(str(record.get("target_offset") if record.get("target_offset") is not None else "")),
                    markdown_escape(str(record.get("target_count") or "")),
                    markdown_escape(str(record.get("status") or "")),
                    str(record.get("rss_unique_reviews") or 0),
                    str(record.get("web_reviews") or 0),
                    format_ratio(record.get("web_to_rss_ratio")),
                    str(record.get("web_non_200_pages_after_retry") or 0),
                    f"{record.get('web_recovered_429_pages') or 0} / {record.get('web_unrecovered_429_pages') or 0}",
                    "yes" if record.get("web_time_budget_exceeded") else "no",
                    markdown_escape(str(record.get("web_stop_reasons") or {})),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(summary: dict[str, Any], output_json: Path | None, output_markdown: Path | None) -> None:
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if output_markdown:
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_markdown_summary(summary), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize App Store RSS-vs-web-catalog comparison reports.")
    parser.add_argument(
        "--root",
        type=Path,
        nargs="+",
        default=[DEFAULT_ROOT],
        help="Report root(s) or source_comparison_report.json file(s) to summarize.",
    )
    parser.add_argument("--output-json", type=Path, help="Optional path for the JSON history summary.")
    parser.add_argument("--output-markdown", type=Path, help="Optional path for the Markdown history report.")
    parser.add_argument("--min-runs", type=int, default=5, help="Clean replacement runs required for promotion.")
    parser.add_argument(
        "--single-app-only",
        action="store_true",
        help="Only include reports whose target and scope counts indicate the scheduled single-app profile.",
    )
    parser.add_argument(
        "--min-web-max-pages",
        type=int,
        help="Only include reports configured with at least this many web catalog pages per scope.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = find_report_paths(args.root)
    summary = summarize_history_from_reports(
        paths,
        min_runs=args.min_runs,
        single_app_only=args.single_app_only,
        min_web_max_pages=args.min_web_max_pages,
    )
    write_outputs(summary, args.output_json, args.output_markdown)
    print(json.dumps(summary["promotion_gate"], indent=2, sort_keys=True))
    print(json.dumps(summary["aggregate"], indent=2, sort_keys=True))
    if not paths:
        return 1
    return 0


def unique_non_empty(values: Any) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def int_or_zero(value: Any) -> int:
    parsed = int_or_none(value)
    return parsed if parsed is not None else 0


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_ratio(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


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
