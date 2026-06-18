from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, DEFAULT_TARGETS, SOURCE, WEB_CATALOG_SOURCE
from app_store_review_pipeline.postgres_database import connect_postgres, initialize_postgres, mask_database_url
from app_store_review_pipeline.targets import active_targets, load_targets


def load_source_counts(database_url: str) -> dict[tuple[str, str, str], int]:
    initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        rows = connection.execute(
            """
            SELECT app_id, country, source, COUNT(DISTINCT review_id) AS review_count
            FROM app_store_reviews
            WHERE source IN (%s, %s)
            GROUP BY app_id, country, source
            """,
            (SOURCE, WEB_CATALOG_SOURCE),
        ).fetchall()
    return {
        (str(row["app_id"]), str(row["country"]).lower(), str(row["source"])): int(row["review_count"] or 0)
        for row in rows
    }


def build_scope_records(targets_path: Path, counts: dict[tuple[str, str, str], int]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for target_index, target in enumerate(active_targets(load_targets(targets_path))):
        for country in target.countries:
            country = country.lower()
            rss_reviews = counts.get((target.apple_app_id, country, SOURCE), 0)
            web_reviews = counts.get((target.apple_app_id, country, WEB_CATALOG_SOURCE), 0)
            ratio = web_reviews / rss_reviews if rss_reviews else None
            records.append(
                {
                    "target_index": target_index,
                    "app_id": target.apple_app_id,
                    "app_name": target.app_name,
                    "category": target.category,
                    "country": country,
                    "rss_reviews": rss_reviews,
                    "web_catalog_reviews": web_reviews,
                    "web_to_rss_ratio": ratio,
                    "web_at_or_above_rss": bool(rss_reviews and web_reviews >= rss_reviews),
                    "web_has_rows": web_reviews > 0,
                    "rss_has_rows": rss_reviews > 0,
                    "web_review_gap_to_rss": max(0, rss_reviews - web_reviews),
                }
            )
    return records


def summarize_scope_records(records: list[dict[str, Any]], *, min_parity_scopes: int) -> dict[str, Any]:
    rss_scopes = [record for record in records if record["rss_has_rows"]]
    web_scopes = [record for record in records if record["web_has_rows"]]
    parity_scopes = [record for record in records if record["web_at_or_above_rss"]]
    below_rss_scopes = [
        record for record in records if record["rss_has_rows"] and record["web_has_rows"] and not record["web_at_or_above_rss"]
    ]
    missing_web_scopes = [record for record in records if record["rss_has_rows"] and not record["web_has_rows"]]
    web_scope_ratios = [
        float(record["web_to_rss_ratio"])
        for record in records
        if record["web_has_rows"] and isinstance(record.get("web_to_rss_ratio"), (int, float))
    ]
    rss_total = sum(int(record["rss_reviews"]) for record in records)
    web_total = sum(int(record["web_catalog_reviews"]) for record in records)

    blocking_reasons: list[str] = []
    if len(parity_scopes) < min_parity_scopes:
        blocking_reasons.append(f"needs_at_least_{min_parity_scopes}_parity_scopes")
    if below_rss_scopes:
        blocking_reasons.append("one_or_more_web_scopes_below_rss")
    if not web_scopes:
        blocking_reasons.append("no_web_catalog_scopes")

    if not web_scopes:
        status = "no_web_catalog_evidence"
    elif not blocking_reasons:
        status = "ready_for_controlled_promotion"
    elif len(parity_scopes) >= min_parity_scopes:
        status = "needs_cleanup_of_below_rss_scopes"
    else:
        status = "needs_more_evidence"

    return {
        "promotion_gate": {
            "status": status,
            "ready_for_controlled_promotion": not blocking_reasons and bool(web_scopes),
            "min_parity_scopes": min_parity_scopes,
            "blocking_reasons": blocking_reasons,
        },
        "aggregate": {
            "target_scope_count": len(records),
            "rss_scope_count": len(rss_scopes),
            "web_catalog_scope_count": len(web_scopes),
            "parity_scope_count": len(parity_scopes),
            "below_rss_scope_count": len(below_rss_scopes),
            "missing_web_scope_count": len(missing_web_scopes),
            "rss_reviews_total": rss_total,
            "web_catalog_reviews_total": web_total,
            "web_to_rss_total_ratio": web_total / rss_total if rss_total else None,
            "web_scope_parity_rate": len(parity_scopes) / len(web_scopes) if web_scopes else None,
            "target_scope_parity_rate": len(parity_scopes) / len(rss_scopes) if rss_scopes else None,
            "average_web_to_rss_ratio_for_web_scopes": (
                sum(web_scope_ratios) / len(web_scope_ratios) if web_scope_ratios else None
            ),
            "minimum_web_to_rss_ratio_for_web_scopes": min(web_scope_ratios) if web_scope_ratios else None,
            "web_review_gap_to_rss_total": sum(int(record["web_review_gap_to_rss"]) for record in records),
        },
        "below_rss_scopes": below_rss_scopes,
        "missing_web_scopes": missing_web_scopes,
    }


def summarize_source_coverage(
    *,
    database_url: str,
    targets_path: Path,
    min_parity_scopes: int,
) -> dict[str, Any]:
    counts = load_source_counts(database_url)
    records = build_scope_records(targets_path, counts)
    summary = summarize_scope_records(records, min_parity_scopes=min_parity_scopes)
    summary.update(
        {
            "database_url": mask_database_url(database_url),
            "targets_path": str(targets_path),
            "rss_source": SOURCE,
            "web_catalog_source": WEB_CATALOG_SOURCE,
            "scopes": records,
        }
    )
    return summary


def render_markdown_summary(summary: dict[str, Any]) -> str:
    gate = summary.get("promotion_gate") or {}
    aggregate = summary.get("aggregate") or {}
    lines = [
        "# App Store Source Coverage Scorecard",
        "",
        f"Promotion status: **{gate.get('status', 'unknown')}**",
        "",
        f"- Database: `{summary.get('database_url')}`",
        f"- Targets: `{summary.get('targets_path')}`",
        f"- RSS source: `{summary.get('rss_source')}`",
        f"- Web catalog source: `{summary.get('web_catalog_source')}`",
        f"- Minimum parity scopes required: `{gate.get('min_parity_scopes')}`",
        f"- Blocking reasons: `{gate.get('blocking_reasons') or []}`",
        "",
        "## Aggregate",
        "",
        f"- Target scopes: `{aggregate.get('target_scope_count', 0)}`",
        f"- RSS scopes with rows: `{aggregate.get('rss_scope_count', 0)}`",
        f"- Web catalog scopes with rows: `{aggregate.get('web_catalog_scope_count', 0)}`",
        f"- Web catalog scopes at or above RSS: `{aggregate.get('parity_scope_count', 0)}`",
        f"- Web catalog scopes below RSS: `{aggregate.get('below_rss_scope_count', 0)}`",
        f"- RSS scopes missing web catalog rows: `{aggregate.get('missing_web_scope_count', 0)}`",
        f"- RSS reviews total: `{aggregate.get('rss_reviews_total', 0)}`",
        f"- Web catalog reviews total: `{aggregate.get('web_catalog_reviews_total', 0)}`",
        f"- Web/RSS total ratio: `{format_ratio(aggregate.get('web_to_rss_total_ratio'))}`",
        f"- Web-scope parity rate: `{format_ratio(aggregate.get('web_scope_parity_rate'))}`",
        "",
        "## Web Catalog Scopes",
        "",
        "| Offset | App | Country | RSS | Web Catalog | Ratio | Status |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for record in sorted(
        [record for record in summary.get("scopes") or [] if record.get("web_has_rows")],
        key=lambda row: (not row.get("web_at_or_above_rss"), -int(row.get("web_catalog_reviews") or 0), row.get("app_name") or ""),
    ):
        lines.append(scope_markdown_row(record))

    missing_web = summary.get("missing_web_scopes") or []
    if missing_web:
        lines.extend(["", "## Missing Web Catalog Coverage", "", "| Offset | App | Country | RSS |", "| ---: | --- | --- | ---: |"])
        for record in missing_web[:50]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(record.get("target_index")),
                        markdown_escape(str(record.get("app_name") or "")),
                        markdown_escape(str(record.get("country") or "")),
                        str(record.get("rss_reviews") or 0),
                    ]
                )
                + " |"
            )
        if len(missing_web) > 50:
            lines.append(f"\nOnly the first 50 of {len(missing_web)} missing scopes are shown.\n")
    lines.append("")
    return "\n".join(lines)


def scope_markdown_row(record: dict[str, Any]) -> str:
    if record.get("web_at_or_above_rss"):
        status = "at_or_above_rss"
    elif record.get("web_has_rows"):
        status = "below_rss"
    else:
        status = "missing_web"
    return (
        "| "
        + " | ".join(
            [
                str(record.get("target_index")),
                markdown_escape(str(record.get("app_name") or "")),
                markdown_escape(str(record.get("country") or "")),
                str(record.get("rss_reviews") or 0),
                str(record.get("web_catalog_reviews") or 0),
                format_ratio(record.get("web_to_rss_ratio")),
                status,
            ]
        )
        + " |"
    )


def write_outputs(summary: dict[str, Any], output_json: Path | None, output_markdown: Path | None) -> None:
    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if output_markdown:
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_markdown_summary(summary), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize cumulative RSS vs web catalog coverage in Postgres.")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument(
        "--min-parity-scopes",
        type=int,
        default=20,
        help="Minimum app-country scopes at or above RSS before reporting controlled-promotion readiness.",
    )
    return parser


def format_ratio(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return f"{value:.3f}"


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = summarize_source_coverage(
        database_url=args.database_url,
        targets_path=args.targets,
        min_parity_scopes=args.min_parity_scopes,
    )
    write_outputs(summary, args.output_json, args.output_markdown)
    print(json.dumps(summary["promotion_gate"], indent=2, sort_keys=True))
    print(json.dumps(summary["aggregate"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
