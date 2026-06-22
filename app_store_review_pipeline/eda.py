from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app_store_review_pipeline.config import DEFAULT_DATABASE_URL, WEB_CATALOG_SOURCE
from app_store_review_pipeline.postgres_database import connect_postgres, mask_database_url


DEFAULT_EDA_MARKDOWN = Path("docs/eda/apple_review_data_quality.md")
DEFAULT_EDA_JSON = Path("docs/eda/apple_review_data_quality_summary.json")
DEFAULT_EDA_HTML = Path("docs/eda/apple_review_data_quality_dashboard.html")


def generate_eda_report(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    source: str = WEB_CATALOG_SOURCE,
    markdown_path: Path = DEFAULT_EDA_MARKDOWN,
    json_path: Path = DEFAULT_EDA_JSON,
    html_path: Path = DEFAULT_EDA_HTML,
) -> dict[str, Any]:
    summary = build_eda_summary(database_url, source=source)
    markdown = render_eda_markdown(summary)
    html = render_eda_html(summary)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return {
        "source": source,
        "database_url": mask_database_url(database_url),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "generated_at": summary["metadata"]["generated_at"],
        "review_count": summary["inventory"]["primary_source"]["review_count"],
        "app_count": summary["inventory"]["primary_source"]["app_count"],
    }


def build_eda_summary(database_url: str, *, source: str = WEB_CATALOG_SOURCE) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with connect_postgres(database_url) as connection:
        rows_by_source = fetch_all(
            connection,
            """
            SELECT
                source,
                COUNT(*)::bigint AS review_count,
                COUNT(DISTINCT app_id)::bigint AS app_count,
                COUNT(DISTINCT country)::bigint AS country_count,
                MIN(updated_epoch_seconds)::bigint AS min_updated_epoch_seconds,
                MAX(updated_epoch_seconds)::bigint AS max_updated_epoch_seconds
            FROM app_store_reviews
            GROUP BY source
            ORDER BY review_count DESC
            """,
        )
        primary_source = fetch_one(
            connection,
            """
            SELECT
                COUNT(*)::bigint AS review_count,
                COUNT(DISTINCT r.app_id)::bigint AS app_count,
                COUNT(DISTINCT COALESCE(NULLIF(t.category, ''), 'unknown'))::bigint AS category_count,
                COUNT(DISTINCT r.country)::bigint AS country_count,
                MIN(r.updated_epoch_seconds)::bigint AS min_updated_epoch_seconds,
                MAX(r.updated_epoch_seconds)::bigint AS max_updated_epoch_seconds,
                MIN(r.collected_at) AS first_collected_at,
                MAX(r.collected_at) AS last_collected_at
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            """,
            (source,),
        )
        target_snapshot = {
            "active_by_category": fetch_all(
                connection,
                """
                SELECT COALESCE(NULLIF(category, ''), 'unknown') AS category, COUNT(*)::bigint AS app_count
                FROM app_store_targets
                WHERE active = 1
                GROUP BY COALESCE(NULLIF(category, ''), 'unknown')
                ORDER BY app_count DESC, category
                """,
            ),
            "counts": fetch_all(
                connection,
                """
                SELECT active, COUNT(*)::bigint AS app_count
                FROM app_store_targets
                GROUP BY active
                ORDER BY active DESC
                """,
            ),
        }

        volume_by_app = fetch_all(
            connection,
            """
            SELECT
                r.app_id,
                COALESCE(MAX(r.app_name), MAX(t.app_name), r.app_id) AS app_name,
                COALESCE(NULLIF(MAX(t.category), ''), 'unknown') AS category,
                COUNT(*)::bigint AS review_count,
                COUNT(*) FILTER (WHERE r.updated_epoch_seconds >= EXTRACT(EPOCH FROM now() - interval '30 days'))::bigint AS reviews_last_30_days,
                MIN(r.updated_epoch_seconds)::bigint AS min_updated_epoch_seconds,
                MAX(r.updated_epoch_seconds)::bigint AS max_updated_epoch_seconds
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            GROUP BY r.app_id
            ORDER BY review_count DESC, app_name
            """,
            (source,),
        )
        volume_by_category = fetch_all(
            connection,
            """
            SELECT
                COALESCE(NULLIF(t.category, ''), 'unknown') AS category,
                COUNT(DISTINCT r.app_id)::bigint AS app_count,
                COUNT(*)::bigint AS review_count,
                ROUND(AVG(r.rating)::numeric, 3) AS avg_rating,
                ROUND(AVG(LENGTH(COALESCE(r.content, '')))::numeric, 1) AS avg_content_chars,
                COUNT(*) FILTER (WHERE r.updated_epoch_seconds >= EXTRACT(EPOCH FROM now() - interval '30 days'))::bigint AS reviews_last_30_days
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            GROUP BY COALESCE(NULLIF(t.category, ''), 'unknown')
            ORDER BY review_count DESC, category
            """,
            (source,),
        )
        rating_overall = fetch_all(
            connection,
            """
            SELECT rating, COUNT(*)::bigint AS review_count
            FROM app_store_reviews
            WHERE source = %s
            GROUP BY rating
            ORDER BY rating
            """,
            (source,),
        )
        rating_by_category = fetch_all(
            connection,
            """
            SELECT
                COALESCE(NULLIF(t.category, ''), 'unknown') AS category,
                COUNT(*)::bigint AS review_count,
                ROUND(AVG(r.rating)::numeric, 3) AS avg_rating,
                COUNT(*) FILTER (WHERE r.rating = 1)::bigint AS rating_1,
                COUNT(*) FILTER (WHERE r.rating = 2)::bigint AS rating_2,
                COUNT(*) FILTER (WHERE r.rating = 3)::bigint AS rating_3,
                COUNT(*) FILTER (WHERE r.rating = 4)::bigint AS rating_4,
                COUNT(*) FILTER (WHERE r.rating = 5)::bigint AS rating_5
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            GROUP BY COALESCE(NULLIF(t.category, ''), 'unknown')
            ORDER BY review_count DESC, category
            """,
            (source,),
        )
        rating_by_app = fetch_all(
            connection,
            """
            SELECT
                r.app_id,
                COALESCE(MAX(r.app_name), MAX(t.app_name), r.app_id) AS app_name,
                COALESCE(NULLIF(MAX(t.category), ''), 'unknown') AS category,
                COUNT(*)::bigint AS review_count,
                ROUND(AVG(r.rating)::numeric, 3) AS avg_rating,
                COUNT(*) FILTER (WHERE r.rating = 1)::bigint AS rating_1,
                COUNT(*) FILTER (WHERE r.rating = 5)::bigint AS rating_5
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            GROUP BY r.app_id
            ORDER BY review_count DESC, app_name
            LIMIT 50
            """,
            (source,),
        )
        length_summary = fetch_one(
            connection,
            """
            SELECT
                COUNT(*)::bigint AS review_count,
                MIN(LENGTH(COALESCE(content, '')))::bigint AS min_chars,
                MAX(LENGTH(COALESCE(content, '')))::bigint AS max_chars,
                ROUND(AVG(LENGTH(COALESCE(content, '')))::numeric, 1) AS avg_chars,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p10_chars,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p25_chars,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p50_chars,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p75_chars,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p90_chars,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY LENGTH(COALESCE(content, ''))) AS p95_chars
            FROM app_store_reviews
            WHERE source = %s
            """,
            (source,),
        )
        low_signal = fetch_one(
            connection,
            """
            SELECT
                COUNT(*)::bigint AS review_count,
                COUNT(*) FILTER (WHERE content IS NULL OR BTRIM(content) = '')::bigint AS blank_content,
                COUNT(*) FILTER (WHERE LENGTH(BTRIM(COALESCE(content, ''))) BETWEEN 1 AND 20)::bigint AS content_1_to_20_chars,
                COUNT(*) FILTER (WHERE LENGTH(BTRIM(COALESCE(content, ''))) BETWEEN 21 AND 50)::bigint AS content_21_to_50_chars,
                COUNT(*) FILTER (WHERE BTRIM(COALESCE(title, '')) = '')::bigint AS blank_title,
                COUNT(*) FILTER (WHERE COALESCE(content, '') ~* '(https?://|www\\.)')::bigint AS url_like_content,
                COUNT(*) FILTER (WHERE COALESCE(content, '') ~ '<[^>]+>')::bigint AS html_like_content,
                COUNT(*) FILTER (WHERE POSITION(CHR(10) IN COALESCE(content, '')) > 0)::bigint AS multiline_content,
                COUNT(*) FILTER (WHERE COALESCE(content, '') ~ '[^[:ascii:]]')::bigint AS non_ascii_content
            FROM app_store_reviews
            WHERE source = %s
            """,
            (source,),
        )
        time_monthly = fetch_all(
            connection,
            """
            SELECT
                TO_CHAR(date_trunc('month', to_timestamp(updated_epoch_seconds)), 'YYYY-MM') AS month,
                COUNT(*)::bigint AS review_count,
                COUNT(DISTINCT app_id)::bigint AS app_count
            FROM app_store_reviews
            WHERE source = %s AND updated_epoch_seconds IS NOT NULL
            GROUP BY date_trunc('month', to_timestamp(updated_epoch_seconds))
            ORDER BY month DESC
            LIMIT 24
            """,
            (source,),
        )
        freshness_by_app = fetch_all(
            connection,
            """
            SELECT
                r.app_id,
                COALESCE(MAX(r.app_name), MAX(t.app_name), r.app_id) AS app_name,
                COALESCE(NULLIF(MAX(t.category), ''), 'unknown') AS category,
                COUNT(*)::bigint AS review_count,
                MAX(r.updated_epoch_seconds)::bigint AS max_updated_epoch_seconds,
                ROUND(EXTRACT(EPOCH FROM (now() - to_timestamp(MAX(r.updated_epoch_seconds)))) / 86400.0, 1) AS newest_review_age_days,
                COUNT(*) FILTER (WHERE r.updated_epoch_seconds >= EXTRACT(EPOCH FROM now() - interval '7 days'))::bigint AS reviews_last_7_days,
                COUNT(*) FILTER (WHERE r.updated_epoch_seconds >= EXTRACT(EPOCH FROM now() - interval '30 days'))::bigint AS reviews_last_30_days
            FROM app_store_reviews r
            LEFT JOIN app_store_targets t ON t.app_id = r.app_id
            WHERE r.source = %s
            GROUP BY r.app_id
            ORDER BY newest_review_age_days DESC NULLS LAST, review_count DESC
            LIMIT 50
            """,
            (source,),
        )
        missingness = fetch_one(
            connection,
            """
            SELECT
                COUNT(*)::bigint AS review_count,
                COUNT(*) FILTER (WHERE version IS NULL OR BTRIM(version) = '')::bigint AS missing_version,
                COUNT(*) FILTER (WHERE vote_sum IS NULL)::bigint AS missing_vote_sum,
                COUNT(*) FILTER (WHERE vote_count IS NULL)::bigint AS missing_vote_count,
                COUNT(*) FILTER (WHERE author_name IS NULL OR BTRIM(author_name) = '')::bigint AS missing_author_name,
                COUNT(*) FILTER (WHERE title IS NULL OR BTRIM(title) = '')::bigint AS missing_title,
                COUNT(*) FILTER (WHERE content IS NULL OR BTRIM(content) = '')::bigint AS missing_content,
                COUNT(*) FILTER (WHERE updated_at IS NULL OR BTRIM(updated_at) = '')::bigint AS missing_updated_at,
                COUNT(*) FILTER (WHERE updated_epoch_seconds IS NULL)::bigint AS missing_updated_epoch_seconds,
                COUNT(*) FILTER (WHERE rating IS NULL)::bigint AS missing_rating
            FROM app_store_reviews
            WHERE source = %s
            """,
            (source,),
        )
        duplicates = fetch_one(
            connection,
            """
            WITH normalized AS (
                SELECT
                    md5(regexp_replace(lower(btrim(COALESCE(content, ''))), '\\s+', ' ', 'g')) AS normalized_hash,
                    NULLIF(btrim(COALESCE(content, '')), '') AS content
                FROM app_store_reviews
                WHERE source = %s
            ),
            duplicate_groups AS (
                SELECT normalized_hash, COUNT(*)::bigint AS row_count, MIN(LEFT(content, 160)) AS sample
                FROM normalized
                WHERE content IS NOT NULL
                GROUP BY normalized_hash
                HAVING COUNT(*) > 1
            )
            SELECT
                (SELECT COUNT(*)::bigint FROM app_store_reviews WHERE source = %s) AS review_count,
                (SELECT COUNT(DISTINCT review_key)::bigint FROM app_store_reviews WHERE source = %s) AS distinct_review_keys,
                COUNT(*)::bigint AS normalized_duplicate_group_count,
                COALESCE(SUM(row_count), 0)::bigint AS normalized_duplicate_row_count,
                COALESCE(MAX(row_count), 0)::bigint AS largest_normalized_duplicate_group
            FROM duplicate_groups
            """,
            (source, source, source),
        )
        duplicate_examples = fetch_all(
            connection,
            """
            WITH normalized AS (
                SELECT
                    md5(regexp_replace(lower(btrim(COALESCE(content, ''))), '\\s+', ' ', 'g')) AS normalized_hash,
                    NULLIF(btrim(COALESCE(content, '')), '') AS content,
                    app_id
                FROM app_store_reviews
                WHERE source = %s
            )
            SELECT
                COUNT(*)::bigint AS row_count,
                COUNT(DISTINCT app_id)::bigint AS app_count,
                MIN(LEFT(content, 180)) AS sample
            FROM normalized
            WHERE content IS NOT NULL
            GROUP BY normalized_hash
            HAVING COUNT(*) > 1
            ORDER BY row_count DESC, app_count DESC
            LIMIT 15
            """,
            (source,),
        )
        pipeline_status_codes = fetch_all(
            connection,
            """
            SELECT
                COALESCE(status_code::text, 'null') AS status_code,
                COUNT(*)::bigint AS page_count,
                SUM(review_count)::bigint AS review_rows
            FROM app_store_review_pages
            WHERE source = %s
            GROUP BY COALESCE(status_code::text, 'null')
            ORDER BY page_count DESC, status_code
            """,
            (source,),
        )
        pipeline_terminal_reasons = fetch_all(
            connection,
            """
            SELECT
                COALESCE(terminal_reason, 'none') AS terminal_reason,
                COUNT(*)::bigint AS page_count,
                SUM(review_count)::bigint AS review_rows
            FROM app_store_review_pages
            WHERE source = %s
            GROUP BY COALESCE(terminal_reason, 'none')
            ORDER BY page_count DESC, terminal_reason
            """,
            (source,),
        )
        pipeline_attempts = fetch_all(
            connection,
            """
            SELECT
                attempt_count,
                COUNT(*)::bigint AS page_count,
                SUM(review_count)::bigint AS review_rows
            FROM app_store_review_pages
            WHERE source = %s
            GROUP BY attempt_count
            ORDER BY attempt_count
            """,
            (source,),
        )
        pipeline_empty_pages = fetch_one(
            connection,
            """
            SELECT
                COUNT(*) FILTER (WHERE review_count = 0)::bigint AS empty_pages,
                COUNT(*) FILTER (WHERE review_count = 0 AND has_next_link = 1)::bigint AS empty_pages_with_next_link,
                COUNT(*) FILTER (WHERE review_count = 0 AND has_next_link = 0)::bigint AS empty_pages_without_next_link,
                COUNT(*) FILTER (WHERE status_code = 429)::bigint AS http_429_pages,
                COUNT(*) FILTER (WHERE status_code IS NOT NULL AND status_code <> 200)::bigint AS final_non_200_pages,
                COUNT(*) FILTER (WHERE attempt_count > 1)::bigint AS retried_pages,
                COUNT(*) FILTER (WHERE status = 'error')::bigint AS error_pages
            FROM app_store_review_pages
            WHERE source = %s
            """,
            (source,),
        )
        pipeline_by_app = fetch_all(
            connection,
            """
            WITH app_runs AS (
                SELECT
                    p.run_id,
                    p.app_id,
                    COALESCE(MAX(p.app_name), MAX(t.app_name), p.app_id) AS app_name,
                    COALESCE(NULLIF(MAX(t.category), ''), 'unknown') AS category,
                    COUNT(*)::bigint AS page_count,
                    SUM(p.review_count)::bigint AS page_review_rows,
                    COUNT(*) FILTER (WHERE p.status_code = 429)::bigint AS http_429_pages,
                    COUNT(*) FILTER (WHERE p.status_code IS NOT NULL AND p.status_code <> 200)::bigint AS final_non_200_pages,
                    COUNT(*) FILTER (WHERE p.attempt_count > 1)::bigint AS retried_pages,
                    MIN(p.fetched_at)::text AS first_page_at,
                    MAX(p.fetched_at)::text AS last_page_at
                FROM app_store_review_pages p
                LEFT JOIN app_store_targets t ON t.app_id = p.app_id
                WHERE p.source = %s
                GROUP BY p.run_id, p.app_id
            )
            SELECT
                app_id,
                MAX(app_name) AS app_name,
                MAX(category) AS category,
                COUNT(*)::bigint AS run_count,
                SUM(page_count)::bigint AS page_count,
                SUM(page_review_rows)::bigint AS page_review_rows,
                SUM(http_429_pages)::bigint AS http_429_pages,
                SUM(final_non_200_pages)::bigint AS final_non_200_pages,
                SUM(retried_pages)::bigint AS retried_pages,
                ROUND(AVG(EXTRACT(EPOCH FROM (last_page_at::timestamptz - first_page_at::timestamptz)) / 60.0)::numeric, 2) AS avg_run_page_window_minutes,
                ROUND(MAX(EXTRACT(EPOCH FROM (last_page_at::timestamptz - first_page_at::timestamptz)) / 60.0)::numeric, 2) AS max_run_page_window_minutes
            FROM app_runs
            GROUP BY app_id
            ORDER BY page_count DESC, app_name
            LIMIT 50
            """,
            (source,),
        )
        runs_summary = fetch_one(
            connection,
            """
            SELECT
                COUNT(*)::bigint AS run_count,
                MIN(loaded_at) AS first_loaded_at,
                MAX(loaded_at) AS last_loaded_at,
                SUM(target_count)::bigint AS target_count_sum,
                SUM(page_count)::bigint AS page_count,
                SUM(review_count)::bigint AS raw_review_rows,
                SUM(reviews_inserted)::bigint AS reviews_inserted,
                SUM(reviews_updated)::bigint AS reviews_updated,
                SUM(fetch_errors)::bigint AS fetch_errors,
                SUM(capped_scopes)::bigint AS capped_scopes
            FROM app_store_runs
            WHERE source = %s
            """,
            (source,),
        )

    concentration = calculate_concentration(volume_by_app)
    summary = {
        "metadata": {
            "generated_at": generated_at,
            "database_url": mask_database_url(database_url),
            "source": source,
        },
        "inventory": {
            "rows_by_source": rows_by_source,
            "primary_source": primary_source,
            "target_snapshot": target_snapshot,
        },
        "volume": {
            "concentration": concentration,
            "by_app_top_50": volume_by_app[:50],
            "by_category": volume_by_category,
        },
        "ratings": {
            "overall": rating_overall,
            "by_category": rating_by_category,
            "by_app_top_50": rating_by_app,
        },
        "text_quality": {
            "length_summary": length_summary,
            "low_signal": add_rates(low_signal),
            "duplicate_summary": add_rates(duplicates),
            "duplicate_examples": duplicate_examples,
        },
        "time_coverage": {
            "monthly_recent_24": time_monthly,
            "freshness_by_app_stalest_50": freshness_by_app,
        },
        "missingness": add_rates(missingness),
        "pipeline_behavior": {
            "runs_summary": runs_summary,
            "status_codes": pipeline_status_codes,
            "terminal_reasons": pipeline_terminal_reasons,
            "attempt_counts": pipeline_attempts,
            "empty_and_error_pages": pipeline_empty_pages,
            "by_app_top_50_pages": pipeline_by_app,
        },
    }
    return convert_json(summary)


def fetch_all(connection: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def fetch_one(connection: Any, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    row = connection.execute(query, params).fetchone()
    return dict(row or {})


def calculate_concentration(volume_by_app: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [int(row.get("review_count") or 0) for row in volume_by_app]
    total = sum(counts)
    if total <= 0:
        return {"review_count": 0, "app_count": 0, "top_1_share": 0, "top_5_share": 0, "top_10_share": 0, "hhi": 0}
    shares = [count / total for count in counts]
    return {
        "review_count": total,
        "app_count": len(counts),
        "top_1_share": round(sum(counts[:1]) / total, 4),
        "top_5_share": round(sum(counts[:5]) / total, 4),
        "top_10_share": round(sum(counts[:10]) / total, 4),
        "hhi": round(sum(share * share for share in shares), 4),
    }


def add_rates(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    total = int(output.get("review_count") or 0)
    if total <= 0:
        return output
    for key, value in list(output.items()):
        if key == "review_count" or not isinstance(value, int):
            continue
        output[f"{key}_rate"] = round(value / total, 4)
    return output


def render_eda_markdown(summary: dict[str, Any]) -> str:
    metadata = summary["metadata"]
    primary = summary["inventory"]["primary_source"]
    concentration = summary["volume"]["concentration"]
    low_signal = summary["text_quality"]["low_signal"]
    duplicate_summary = summary["text_quality"]["duplicate_summary"]
    pipeline = summary["pipeline_behavior"]
    page_health = pipeline["empty_and_error_pages"]
    lines = [
        "# Apple App Store Review Data Quality Report",
        "",
        f"Generated at: `{metadata['generated_at']}`",
        f"Database: `{metadata['database_url']}`",
        f"Primary source: `{metadata['source']}`",
        "",
        "## Executive Summary",
        "",
        (
            f"The current primary-source dataset contains **{fmt_int(primary.get('review_count'))}** deduplicated "
            f"web-catalog reviews across **{fmt_int(primary.get('app_count'))}** apps, "
            f"**{fmt_int(primary.get('category_count'))}** categories, and "
            f"**{fmt_int(primary.get('country_count'))}** country storefronts."
        ),
        (
            f"Top-app concentration is material but not extreme: top 1 app share is "
            f"**{fmt_pct(concentration.get('top_1_share'))}**, top 5 share is "
            f"**{fmt_pct(concentration.get('top_5_share'))}**, and top 10 share is "
            f"**{fmt_pct(concentration.get('top_10_share'))}**."
        ),
        (
            f"Operationally, the stored page history includes **{fmt_int(page_health.get('http_429_pages'))}** "
            f"HTTP 429 pages, **{fmt_int(page_health.get('retried_pages'))}** retried pages, and "
            f"**{fmt_int(page_health.get('final_non_200_pages'))}** final non-200 pages."
        ),
        "",
        "## Inventory",
        "",
        markdown_table(
            summary["inventory"]["rows_by_source"],
            ["source", "review_count", "app_count", "country_count", "min_updated_epoch_seconds", "max_updated_epoch_seconds"],
        ),
        "",
        "## Volume Distribution",
        "",
        "### Top Apps By Review Count",
        "",
        markdown_table(
            summary["volume"]["by_app_top_50"][:25],
            ["app_name", "category", "review_count", "reviews_last_30_days", "min_updated_epoch_seconds", "max_updated_epoch_seconds"],
        ),
        "",
        "### Category Coverage",
        "",
        markdown_table(
            summary["volume"]["by_category"],
            ["category", "app_count", "review_count", "avg_rating", "avg_content_chars", "reviews_last_30_days"],
        ),
        "",
        "## Rating Distribution",
        "",
        markdown_table(summary["ratings"]["overall"], ["rating", "review_count"]),
        "",
        "### Rating By Category",
        "",
        markdown_table(
            summary["ratings"]["by_category"],
            ["category", "review_count", "avg_rating", "rating_1", "rating_2", "rating_3", "rating_4", "rating_5"],
        ),
        "",
        "## Text Quality",
        "",
        "### Review Length",
        "",
        markdown_table([summary["text_quality"]["length_summary"]], [
            "review_count",
            "avg_chars",
            "p10_chars",
            "p25_chars",
            "p50_chars",
            "p75_chars",
            "p90_chars",
            "p95_chars",
            "max_chars",
        ]),
        "",
        "### Low-Signal And Formatting Patterns",
        "",
        markdown_table([low_signal], [
            "review_count",
            "blank_content",
            "content_1_to_20_chars",
            "content_21_to_50_chars",
            "blank_title",
            "url_like_content",
            "html_like_content",
            "multiline_content",
            "non_ascii_content",
        ]),
        "",
        "### Duplicate Patterns",
        "",
        markdown_table([duplicate_summary], [
            "review_count",
            "distinct_review_keys",
            "normalized_duplicate_group_count",
            "normalized_duplicate_row_count",
            "largest_normalized_duplicate_group",
        ]),
        "",
        "Top normalized duplicate examples:",
        "",
        markdown_table(summary["text_quality"]["duplicate_examples"], ["row_count", "app_count", "sample"]),
        "",
        "## Freshness And Time Coverage",
        "",
        "### Recent Monthly Density",
        "",
        markdown_table(summary["time_coverage"]["monthly_recent_24"], ["month", "review_count", "app_count"]),
        "",
        "### Stalest Apps By Newest Review",
        "",
        markdown_table(
            summary["time_coverage"]["freshness_by_app_stalest_50"][:25],
            ["app_name", "category", "review_count", "newest_review_age_days", "reviews_last_7_days", "reviews_last_30_days"],
        ),
        "",
        "## Pipeline Behavior",
        "",
        "### Run Summary",
        "",
        markdown_table([pipeline["runs_summary"]], [
            "run_count",
            "first_loaded_at",
            "last_loaded_at",
            "page_count",
            "raw_review_rows",
            "reviews_inserted",
            "reviews_updated",
            "fetch_errors",
            "capped_scopes",
        ]),
        "",
        "### Page Status Codes",
        "",
        markdown_table(pipeline["status_codes"], ["status_code", "page_count", "review_rows"]),
        "",
        "### Terminal Reasons",
        "",
        markdown_table(pipeline["terminal_reasons"], ["terminal_reason", "page_count", "review_rows"]),
        "",
        "### Retry Attempts",
        "",
        markdown_table(pipeline["attempt_counts"], ["attempt_count", "page_count", "review_rows"]),
        "",
        "### Empty And Error Page Summary",
        "",
        markdown_table([page_health], [
            "empty_pages",
            "empty_pages_with_next_link",
            "empty_pages_without_next_link",
            "http_429_pages",
            "final_non_200_pages",
            "retried_pages",
            "error_pages",
        ]),
        "",
        "### Apps With The Most Fetched Pages",
        "",
        markdown_table(
            pipeline["by_app_top_50_pages"][:25],
            [
                "app_name",
                "category",
                "run_count",
                "page_count",
                "page_review_rows",
                "http_429_pages",
                "final_non_200_pages",
                "retried_pages",
                "avg_run_page_window_minutes",
                "max_run_page_window_minutes",
            ],
        ),
        "",
        "## Known Limitations",
        "",
        "- The pipeline reads Apple-hosted public web catalog review payloads exposed to the App Store web experience. This is not the App Store Connect Customer Reviews API, does not use owner credentials, and does not carry an Apple SLA for third-party bulk ingestion.",
        "- Completeness is empirical per app, country, and source scope. A scope is only treated as historically exhausted when pagination reaches `no_next_href`; page cap, time budget, overlap, final non-200, and fetch-error stops mean the current row count is a lower bound.",
        "- Daily/incremental interpretation depends on stable review keys and Postgres upserts. Repeated runs can add new rows or update existing rows, but source-side ordering, removed reviews, and Apple response changes should be monitored through page and terminal-reason metrics.",
        "- Public web catalog payloads do not currently provide every owner-API field. Version, vote sum, vote count, and similar App Store Connect-style review metadata should be treated as unavailable unless Apple exposes them in the public response.",
        "- Normalized duplicate detection uses lowercased whitespace-normalized content hashes; it is useful for triage, not semantic near-duplicate modeling.",
        "- Runtime by app is a page-window proxy based on stored page timestamps, not full GitHub job wall-clock time.",
        "",
    ]
    return "\n".join(lines)


def markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No rows._"
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(format_cell(row.get(column)) for column in columns) + " |")
    return "\n".join([header, separator, *body])


def render_eda_html(summary: dict[str, Any]) -> str:
    data_json = json.dumps(summary, sort_keys=True).replace("</", "<\\/")
    template = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apple App Store Review Data Quality Dashboard</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #5e6978;
      --border: #d9dee7;
      --grid: #e9edf3;
      --blue: #2f6fed;
      --teal: #168b7a;
      --amber: #b97805;
      --red: #cf3f3f;
      --purple: #7556c2;
      --green: #2f8f46;
      --slate: #536173;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }
    header {
      padding: 24px 28px 14px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 24px;
      font-weight: 720;
      letter-spacing: 0;
    }
    .subtitle {
      margin-top: 6px;
      color: var(--muted);
      max-width: 980px;
    }
    main {
      padding: 20px 28px 32px;
      max-width: 1480px;
      margin: 0 auto;
    }
    section { margin-top: 22px; }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }
    h3 {
      margin: 0 0 8px;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .grid {
      display: grid;
      gap: 14px;
    }
    .kpis { grid-template-columns: repeat(5, minmax(160px, 1fr)); }
    .two { grid-template-columns: repeat(2, minmax(320px, 1fr)); }
    .three { grid-template-columns: repeat(3, minmax(260px, 1fr)); }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .metric .value {
      margin-top: 6px;
      font-size: 26px;
      font-weight: 760;
    }
    .metric .note {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    .chart { width: 100%; min-height: 260px; }
    .chart.tall { min-height: 380px; }
    .chart.short { min-height: 190px; }
    .chart.roomy { min-height: 290px; height: 290px; }
    svg { width: 100%; height: 100%; display: block; }
    .axis text, .label-text { fill: var(--muted); font-size: 11px; }
    .axis line, .axis path { stroke: var(--grid); }
    .bar-label { fill: var(--text); font-size: 11px; }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 10px 14px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid var(--grid);
      padding: 7px 6px;
      text-align: right;
      vertical-align: top;
    }
    th:first-child, td:first-child { text-align: left; }
    th {
      color: var(--muted);
      font-weight: 700;
      background: #fafbfc;
      position: sticky;
      top: 0;
    }
    .table-wrap { max-height: 360px; overflow: auto; border: 1px solid var(--grid); border-radius: 8px; }
    .note {
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
    }
    .limitation-list {
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
      font-size: 12px;
    }
    .limitation-list li { margin: 0 0 8px; }
    .pill {
      display: inline-block;
      border: 1px solid var(--border);
      background: #fafbfc;
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
      margin-right: 6px;
    }
    @media (max-width: 1100px) {
      .kpis, .two, .three { grid-template-columns: 1fr; }
      main, header { padding-left: 16px; padding-right: 16px; }
    }
  </style>
</head>
<body>
  <script id="eda-data" type="application/json">__SUMMARY_JSON__</script>
  <header>
    <h1>Apple App Store Review Data Quality Dashboard</h1>
    <div class="subtitle" id="subtitle"></div>
  </header>
  <main>
    <section>
      <div class="grid kpis" id="kpis"></div>
    </section>

    <section>
      <h2>Dataset Shape And Concentration</h2>
      <div class="grid two">
        <div class="panel">
          <h3>Review Volume By Category</h3>
          <div id="categoryVolume" class="chart tall"></div>
          <div class="note">Bars show cumulative deduplicated review rows by target category.</div>
        </div>
        <div class="panel">
          <h3>Top Apps By Review Volume</h3>
          <div id="topApps" class="chart tall"></div>
          <div class="note">Includes the top 20 apps by cumulative review count.</div>
        </div>
      </div>
    </section>

    <section>
      <h2>Rating Signal</h2>
      <div class="grid two">
        <div class="panel">
          <h3>Overall Rating Distribution</h3>
          <div id="ratingOverall" class="chart short"></div>
          <div class="legend" id="ratingLegend"></div>
        </div>
        <div class="panel">
          <h3>Rating Mix By Category</h3>
          <div id="ratingCategory" class="chart tall"></div>
          <div class="legend" id="ratingCategoryLegend"></div>
        </div>
      </div>
    </section>

    <section>
      <h2>Freshness And Time Coverage</h2>
      <div class="grid two">
        <div class="panel">
          <h3>Monthly Review Density</h3>
          <div id="monthlyDensity" class="chart"></div>
          <div class="note">Last 24 months in the collected dataset, ordered by review timestamp.</div>
        </div>
        <div class="panel">
          <h3>Apps With Stale Newest Reviews</h3>
          <div id="staleApps" class="chart"></div>
          <div class="note">Higher values mean the newest collected review for that app is older.</div>
        </div>
      </div>
    </section>

    <section>
      <h2>Text Quality</h2>
      <div class="grid two">
        <div class="panel">
          <h3>Review Length Quantiles</h3>
          <div id="lengthBox" class="chart roomy"></div>
          <div class="note">Distribution is measured in characters per review.</div>
        </div>
        <div class="panel">
          <h3>Low-Signal And Formatting Flags</h3>
          <div id="lowSignal" class="chart roomy"></div>
        </div>
      </div>
    </section>

    <section>
      <h2>Pipeline Health</h2>
      <div class="grid three">
        <div class="panel">
          <h3>Page Status Health</h3>
          <div id="statusCodes" class="chart roomy"></div>
        </div>
        <div class="panel">
          <h3>Fetch Attempts</h3>
          <div id="attempts" class="chart roomy"></div>
        </div>
        <div class="panel">
          <h3>Terminal Stop Reasons</h3>
          <div id="terminalReasons" class="chart roomy"></div>
        </div>
      </div>
    </section>

    <section>
      <h2>Analyst Tables</h2>
      <div class="grid two">
        <div class="panel">
          <h3>Top Apps</h3>
          <div class="table-wrap" id="topAppsTable"></div>
        </div>
        <div class="panel">
          <h3>Duplicate Text Examples</h3>
          <div class="table-wrap" id="duplicatesTable"></div>
        </div>
      </div>
      <div class="grid two" style="margin-top:14px">
        <div class="panel">
          <h3>Pipeline Load By App</h3>
          <div class="table-wrap" id="pipelineAppTable"></div>
        </div>
        <div class="panel">
          <h3>Known Limitations</h3>
          <p class="note">This dashboard separates what the pipeline has collected from what the public source can prove.</p>
          <ul class="limitation-list">
            <li>The source is Apple-hosted public web catalog review data exposed to the App Store web experience. It is not the App Store Connect Customer Reviews API, does not use app-owner credentials, and does not come with a third-party bulk-ingestion SLA.</li>
            <li>Historical completeness is empirical per app-country-source scope. A scope is only treated as exhausted when pagination reaches <code>no_next_href</code>; page cap, time budget, overlap, final non-200, or fetch-error stops mean the stored review count is still a lower bound.</li>
            <li>Incremental runs rely on stable review keys and Postgres upserts. New rows and updated rows can be captured across repeated runs, but source ordering, removed reviews, and Apple response-shape changes should be monitored through page, retry, and terminal-reason metrics.</li>
            <li>The public payload does not currently include every owner-API field. Version, vote sum, vote count, and similar App Store Connect-style fields should be treated as unavailable unless they appear in the public response.</li>
          </ul>
          <p><span class="pill">No login</span><span class="pill">No proxies</span><span class="pill">No CAPTCHA bypass</span><span class="pill">Postgres source of truth</span></p>
        </div>
      </div>
    </section>
  </main>
  <script>
    const summary = JSON.parse(document.getElementById("eda-data").textContent);
    const palette = {
      blue: "#2f6fed",
      teal: "#168b7a",
      amber: "#b97805",
      red: "#cf3f3f",
      purple: "#7556c2",
      green: "#2f8f46",
      slate: "#536173",
      grid: "#e9edf3",
      text: "#18212f",
      muted: "#5e6978"
    };
    const ratingColors = {
      rating_1: "#cf3f3f",
      rating_2: "#d97a2b",
      rating_3: "#b97805",
      rating_4: "#168b7a",
      rating_5: "#2f6fed"
    };

    function fmtInt(value) {
      return Number(value || 0).toLocaleString("en-US");
    }
    function fmtDecimal(value, digits = 1) {
      const number = Number(value || 0);
      return number.toLocaleString("en-US", { maximumFractionDigits: digits });
    }
    function fmtPct(value) {
      return `${(Number(value || 0) * 100).toFixed(1)}%`;
    }
    function byId(id) {
      return document.getElementById(id);
    }
    function value(row, key) {
      return Number(row && row[key] ? row[key] : 0);
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\"": "&quot;",
        "'": "&#39;"
      }[ch]));
    }
    function svgEl(width, height) {
      return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
    }
    function text(x, y, content, cls = "label-text", anchor = "start") {
      return `<text x="${x}" y="${y}" class="${cls}" text-anchor="${anchor}">${escapeHtml(content)}</text>`;
    }
    function truncate(label, max = 24) {
      const s = String(label || "");
      return s.length > max ? `${s.slice(0, max - 1)}.` : s;
    }

    function renderKpis() {
      const primary = summary.inventory.primary_source;
      const concentration = summary.volume.concentration;
      const page = summary.pipeline_behavior.empty_and_error_pages;
      const low = summary.text_quality.low_signal;
      const items = [
        ["Reviews", fmtInt(primary.review_count), `${fmtInt(primary.app_count)} apps, ${fmtInt(primary.category_count)} categories`],
        ["Top 10 Share", fmtPct(concentration.top_10_share), `HHI ${fmtDecimal(concentration.hhi, 3)}`],
        ["Last 30 Days", fmtInt(summary.volume.by_category.reduce((a, r) => a + value(r, "reviews_last_30_days"), 0)), "recent review rows"],
        ["HTTP 429 Pages", fmtInt(page.http_429_pages), `${fmtInt(page.retried_pages)} retried pages`],
        ["Short Reviews", fmtPct(value(low, "content_1_to_20_chars") / Math.max(value(low, "review_count"), 1)), "1 to 20 characters"]
      ];
      byId("kpis").innerHTML = items.map(([label, main, note]) => `
        <div class="panel metric">
          <div class="label">${escapeHtml(label)}</div>
          <div class="value">${escapeHtml(main)}</div>
          <div class="note">${escapeHtml(note)}</div>
        </div>
      `).join("");
      byId("subtitle").textContent = `Generated ${summary.metadata.generated_at} from ${summary.metadata.database_url}. Primary source: ${summary.metadata.source}.`;
    }

    function horizontalBarChart(id, rows, options) {
      const data = rows.slice(0, options.limit || rows.length);
      const width = 900;
      const rowH = options.rowHeight || 24;
      const margin = { top: 18, right: options.right || 110, bottom: options.bottom || 24, left: options.left || 180 };
      const height = margin.top + margin.bottom + data.length * rowH;
      const dataMax = Math.max(...data.map(r => value(r, options.valueKey)), 0);
      const max = options.scaleMax || Math.max(dataMax, 1);
      const plotW = width - margin.left - margin.right;
      const scale = x => (Math.min(x, max) / max) * plotW;
      let out = svgEl(width, height);
      out += `<line x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}" stroke="${palette.grid}"/>`;
      (options.tickValues || []).forEach(tick => {
        const tx = margin.left + scale(tick);
        out += `<line x1="${tx}" y1="${margin.top - 6}" x2="${tx}" y2="${height - margin.bottom}" stroke="${palette.grid}"/>`;
        out += text(tx, height - 8, options.tickFormat ? options.tickFormat(tick) : fmtInt(tick), "label-text", "middle");
      });
      data.forEach((row, i) => {
        const y = margin.top + i * rowH;
        const w = Math.max(1, scale(value(row, options.valueKey)));
        out += `<line x1="${margin.left}" y1="${y + rowH - 5}" x2="${width - margin.right}" y2="${y + rowH - 5}" stroke="${palette.grid}" opacity="0.55"/>`;
        out += `<rect x="${margin.left}" y="${y}" width="${w}" height="${Math.max(10, rowH - 8)}" rx="3" fill="${options.color || palette.blue}"/>`;
        out += text(margin.left - 8, y + rowH - 12, truncate(row[options.labelKey], options.labelMax || 28), "label-text", "end");
        const valueLabel = options.format ? options.format(value(row, options.valueKey), row) : fmtInt(value(row, options.valueKey));
        const valueX = options.valueColumn ? width - margin.right + 12 : margin.left + w + 7;
        out += text(valueX, y + rowH - 12, valueLabel, "bar-label");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function verticalBarChart(id, rows, options) {
      const data = rows.slice();
      const width = 900;
      const height = 260;
      const margin = { top: 18, right: 18, bottom: 48, left: 64 };
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      const max = Math.max(...data.map(r => value(r, options.valueKey)), 1);
      const barW = plotW / Math.max(data.length, 1) * 0.72;
      let out = svgEl(width, height);
      for (let i = 0; i <= 4; i += 1) {
        const y = margin.top + plotH * i / 4;
        out += `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" stroke="${palette.grid}"/>`;
      }
      data.forEach((row, i) => {
        const x = margin.left + (plotW / data.length) * i + (plotW / data.length - barW) / 2;
        const h = (value(row, options.valueKey) / max) * plotH;
        const y = margin.top + plotH - h;
        out += `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="3" fill="${options.color || palette.blue}"/>`;
        out += text(x + barW / 2, height - 24, truncate(row[options.labelKey], 8), "label-text", "middle");
      });
      out += text(8, margin.top + 8, fmtInt(max), "label-text");
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function lineChart(id, rows, options) {
      const data = rows.slice().reverse();
      const width = 900;
      const height = 280;
      const margin = { top: 18, right: 24, bottom: 42, left: 70 };
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      const max = Math.max(...data.map(r => value(r, options.valueKey)), 1);
      const x = i => margin.left + (plotW * i / Math.max(data.length - 1, 1));
      const y = v => margin.top + plotH - (v / max) * plotH;
      let out = svgEl(width, height);
      for (let i = 0; i <= 4; i += 1) {
        const gy = margin.top + plotH * i / 4;
        out += `<line x1="${margin.left}" y1="${gy}" x2="${width - margin.right}" y2="${gy}" stroke="${palette.grid}"/>`;
      }
      const points = data.map((r, i) => `${x(i)},${y(value(r, options.valueKey))}`).join(" ");
      const area = `${margin.left},${margin.top + plotH} ${points} ${width - margin.right},${margin.top + plotH}`;
      out += `<polygon points="${area}" fill="${options.area || "#dce8ff"}" opacity="0.65"/>`;
      out += `<polyline points="${points}" fill="none" stroke="${options.color || palette.blue}" stroke-width="3"/>`;
      data.forEach((row, i) => {
        if (i % Math.ceil(data.length / 8) === 0 || i === data.length - 1) {
          out += text(x(i), height - 20, row[options.labelKey], "label-text", "middle");
        }
      });
      out += text(8, margin.top + 8, fmtInt(max), "label-text");
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function statusCodeChart(id, rows) {
      const data = rows.map(row => ({
        status: String(row.status_code ?? "null"),
        count: value(row, "page_count")
      }));
      const total = data.reduce((acc, row) => acc + row.count, 0) || 1;
      const success = data.find(row => row.status === "200") || { status: "200", count: 0 };
      const nonSuccess = data
        .filter(row => row.status !== "200")
        .sort((a, b) => b.count - a.count);
      const nonSuccessTotal = nonSuccess.reduce((acc, row) => acc + row.count, 0);
      const width = 900;
      const height = 290;
      const margin = { top: 28, right: 150, bottom: 28, left: 96 };
      const plotW = width - margin.left - margin.right;
      const successRate = success.count / total;
      const rowH = 42;
      const y0 = 126;
      const nonMax = Math.max(...nonSuccess.map(row => row.count), 1);
      const labelFor = status => status === "null" ? "No status" : `HTTP ${status}`;
      const colorFor = status => ({
        "429": palette.amber,
        "404": palette.red,
        "null": palette.slate
      }[status] || palette.purple);
      let out = svgEl(width, height);
      out += text(margin.left, margin.top, `200 OK: ${fmtInt(success.count)} pages (${fmtPct(successRate)})`, "bar-label");
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${plotW}" height="30" rx="5" fill="${palette.grid}"/>`;
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${Math.max(2, plotW * successRate)}" height="30" rx="5" fill="${palette.green}"/>`;
      out += text(width - margin.right + 12, margin.top + 39, fmtPct(successRate), "bar-label");
      out += text(margin.left, 94, `Non-200 pages: ${fmtInt(nonSuccessTotal)} (${fmtPct(nonSuccessTotal / total)})`, "bar-label");
      nonSuccess.forEach((row, i) => {
        const y = y0 + i * rowH;
        const w = (row.count / nonMax) * plotW;
        out += `<line x1="${margin.left}" y1="${y + rowH - 8}" x2="${width - margin.right}" y2="${y + rowH - 8}" stroke="${palette.grid}"/>`;
        out += text(margin.left - 10, y + 22, labelFor(row.status), "label-text", "end");
        out += `<rect x="${margin.left}" y="${y}" width="${Math.max(2, w)}" height="24" rx="4" fill="${colorFor(row.status)}"/>`;
        out += text(width - margin.right + 12, y + 18, `${fmtInt(row.count)} (${fmtPct(row.count / total)})`, "bar-label");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function attemptHealthChart(id, rows) {
      const data = rows
        .map(row => ({
          attempt: Number(row.attempt_count || 0),
          count: value(row, "page_count")
        }))
        .sort((a, b) => a.attempt - b.attempt);
      const total = data.reduce((acc, row) => acc + row.count, 0) || 1;
      const first = data.find(row => row.attempt === 1) || { attempt: 1, count: 0 };
      const retries = data.filter(row => row.attempt > 1);
      const retryTotal = retries.reduce((acc, row) => acc + row.count, 0);
      const width = 900;
      const height = 290;
      const margin = { top: 28, right: 150, bottom: 28, left: 104 };
      const plotW = width - margin.left - margin.right;
      const firstRate = first.count / total;
      const rowH = Math.min(34, Math.max(26, 154 / Math.max(retries.length, 1)));
      const y0 = 128;
      const retryMax = Math.max(...retries.map(row => row.count), 1);
      let out = svgEl(width, height);
      out += text(margin.left, margin.top, `First attempt: ${fmtInt(first.count)} pages (${fmtPct(firstRate)})`, "bar-label");
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${plotW}" height="30" rx="5" fill="${palette.grid}"/>`;
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${Math.max(2, plotW * firstRate)}" height="30" rx="5" fill="${palette.green}"/>`;
      out += text(width - margin.right + 12, margin.top + 39, fmtPct(firstRate), "bar-label");
      out += text(margin.left, 94, `Retried pages: ${fmtInt(retryTotal)} (${fmtPct(retryTotal / total)})`, "bar-label");
      retries.forEach((row, i) => {
        const y = y0 + i * rowH;
        const w = (row.count / retryMax) * plotW;
        out += `<line x1="${margin.left}" y1="${y + rowH - 7}" x2="${width - margin.right}" y2="${y + rowH - 7}" stroke="${palette.grid}"/>`;
        out += text(margin.left - 10, y + 21, `Attempt ${row.attempt}`, "label-text", "end");
        out += `<rect x="${margin.left}" y="${y}" width="${Math.max(2, w)}" height="${Math.max(16, rowH - 10)}" rx="4" fill="${palette.teal}"/>`;
        out += text(width - margin.right + 12, y + 18, `${fmtInt(row.count)} (${fmtPct(row.count / total)})`, "bar-label");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function terminalReasonChart(id, rows) {
      const data = rows.map(row => ({
        reason: String(row.terminal_reason ?? "none"),
        count: value(row, "page_count")
      }));
      const total = data.reduce((acc, row) => acc + row.count, 0) || 1;
      const normal = data.find(row => row.reason === "none") || { reason: "none", count: 0 };
      const stops = data
        .filter(row => row.reason !== "none")
        .sort((a, b) => b.count - a.count);
      const stopTotal = stops.reduce((acc, row) => acc + row.count, 0);
      const width = 900;
      const height = 290;
      const margin = { top: 28, right: 150, bottom: 24, left: 210 };
      const plotW = width - margin.left - margin.right;
      const normalRate = normal.count / total;
      const rowH = Math.min(32, Math.max(23, 154 / Math.max(stops.length, 1)));
      const y0 = 128;
      const stopMax = Math.max(...stops.map(row => row.count), 1);
      const labelMap = {
        fetch_error: "Fetch error",
        page_cap: "Page cap",
        no_next_href: "No next page",
        sparse_fetch_error_threshold: "Sparse fetch threshold",
        target_review_count_reached: "Review target reached",
        caught_up_to_existing_review: "Caught up existing",
        caught_up_to_existing_reviews: "Caught up existing"
      };
      const labelFor = reason => labelMap[reason] || reason.replaceAll("_", " ");
      let out = svgEl(width, height);
      out += text(margin.left, margin.top, `No terminal stop: ${fmtInt(normal.count)} pages (${fmtPct(normalRate)})`, "bar-label");
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${plotW}" height="30" rx="5" fill="${palette.grid}"/>`;
      out += `<rect x="${margin.left}" y="${margin.top + 18}" width="${Math.max(2, plotW * normalRate)}" height="30" rx="5" fill="${palette.green}"/>`;
      out += text(width - margin.right + 12, margin.top + 39, fmtPct(normalRate), "bar-label");
      out += text(margin.left, 94, `Terminal stops: ${fmtInt(stopTotal)} (${fmtPct(stopTotal / total)})`, "bar-label");
      stops.forEach((row, i) => {
        const y = y0 + i * rowH;
        const w = (row.count / stopMax) * plotW;
        out += `<line x1="${margin.left}" y1="${y + rowH - 6}" x2="${width - margin.right}" y2="${y + rowH - 6}" stroke="${palette.grid}"/>`;
        out += text(margin.left - 10, y + 18, labelFor(row.reason), "label-text", "end");
        out += `<rect x="${margin.left}" y="${y}" width="${Math.max(2, w)}" height="${Math.max(14, rowH - 9)}" rx="4" fill="${palette.purple}"/>`;
        out += text(width - margin.right + 12, y + 16, `${fmtInt(row.count)} (${fmtPct(row.count / total)})`, "bar-label");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function stackedRatingBars(id, rows, options) {
      const data = rows.slice(0, options.limit || rows.length);
      const width = 900;
      const rowH = 27;
      const margin = { top: 18, right: 70, bottom: 24, left: options.left || 170 };
      const height = margin.top + margin.bottom + data.length * rowH;
      const keys = ["rating_1", "rating_2", "rating_3", "rating_4", "rating_5"];
      const plotW = width - margin.left - margin.right;
      let out = svgEl(width, height);
      data.forEach((row, i) => {
        const y = margin.top + i * rowH;
        const total = keys.reduce((a, key) => a + value(row, key), 0) || value(row, "review_count") || 1;
        let x = margin.left;
        keys.forEach(key => {
          const w = plotW * value(row, key) / total;
          out += `<rect x="${x}" y="${y}" width="${w}" height="${rowH - 8}" fill="${ratingColors[key]}"/>`;
          x += w;
        });
        out += text(margin.left - 8, y + rowH - 12, truncate(row[options.labelKey], 24), "label-text", "end");
        out += text(width - margin.right + 8, y + rowH - 12, fmtDecimal(row.avg_rating, 2), "bar-label");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function singleStackedRating(id, rows) {
      const total = rows.reduce((a, r) => a + value(r, "review_count"), 0) || 1;
      const width = 900;
      const height = 190;
      const margin = { top: 48, right: 28, bottom: 44, left: 28 };
      const plotW = width - margin.left - margin.right;
      let x = margin.left;
      let out = svgEl(width, height);
      rows.forEach(row => {
        const key = `rating_${row.rating}`;
        const w = plotW * value(row, "review_count") / total;
        out += `<rect x="${x}" y="${margin.top}" width="${w}" height="56" fill="${ratingColors[key]}"/>`;
        if (w > 58) {
          out += text(x + w / 2, margin.top + 34, `${row.rating} star`, "bar-label", "middle");
        }
        out += text(x + w / 2, margin.top + 78, fmtPct(value(row, "review_count") / total), "label-text", "middle");
        x += w;
      });
      out += "</svg>";
      byId(id).innerHTML = out;
      byId("ratingLegend").innerHTML = [1,2,3,4,5].map(n => `<span><i class="swatch" style="background:${ratingColors[`rating_${n}`]}"></i>${n} star</span>`).join("");
      byId("ratingCategoryLegend").innerHTML = byId("ratingLegend").innerHTML;
    }

    function lengthBoxPlot(id, length) {
      const width = 900;
      const height = 230;
      const margin = { top: 54, right: 54, bottom: 88, left: 54 };
      const plotW = width - margin.left - margin.right;
      const p95 = value(length, "p95_chars");
      const scaleMax = Math.max(p95 * 1.15, value(length, "avg_chars") * 1.8, 1);
      const x = v => margin.left + Math.min(Number(v || 0), scaleMax) / scaleMax * plotW;
      const y = 92;
      const stats = [
        ["p10", value(length, "p10_chars")],
        ["p25", value(length, "p25_chars")],
        ["p50", value(length, "p50_chars")],
        ["p75", value(length, "p75_chars")],
        ["p95", p95]
      ];
      let out = svgEl(width, height);
      out += `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" stroke="${palette.grid}" stroke-width="2"/>`;
      out += `<line x1="${x(length.p10_chars)}" y1="${y}" x2="${x(length.p95_chars)}" y2="${y}" stroke="${palette.slate}" stroke-width="4" stroke-linecap="round"/>`;
      out += `<rect x="${x(length.p25_chars)}" y="${y - 30}" width="${Math.max(4, x(length.p75_chars) - x(length.p25_chars))}" height="60" rx="6" fill="#dce8ff" stroke="${palette.blue}" stroke-width="2"/>`;
      out += `<line x1="${x(length.p50_chars)}" y1="${y - 34}" x2="${x(length.p50_chars)}" y2="${y + 34}" stroke="${palette.blue}" stroke-width="4" stroke-linecap="round"/>`;
      stats.forEach(([label, v]) => {
        out += `<circle cx="${x(v)}" cy="${y}" r="5" fill="${palette.text}"/>`;
      });
      const avgX = x(length.avg_chars);
      const avgLabelX = Math.min(Math.max(avgX, margin.left + 86), width - margin.right - 86);
      out += `<line x1="${avgX}" y1="${margin.top - 14}" x2="${avgX}" y2="${y + 42}" stroke="${palette.teal}" stroke-dasharray="5 5"/>`;
      out += `<circle cx="${avgX}" cy="${y}" r="6" fill="${palette.teal}" stroke="#ffffff" stroke-width="2"/>`;
      out += text(avgLabelX, 28, `Average ${fmtDecimal(length.avg_chars, 1)} chars`, "bar-label", "middle");
      out += `<line x1="${margin.left}" y1="${height - 70}" x2="${width - margin.right}" y2="${height - 70}" stroke="${palette.grid}"/>`;
      const cellW = plotW / stats.length;
      stats.forEach(([label, v], i) => {
        const cellX = margin.left + cellW * i + cellW / 2;
        if (i > 0) {
          out += `<line x1="${margin.left + cellW * i}" y1="${height - 66}" x2="${margin.left + cellW * i}" y2="${height - 18}" stroke="${palette.grid}"/>`;
        }
        out += text(cellX, height - 45, label, "label-text", "middle");
        out += text(cellX, height - 22, `${fmtInt(v)} chars`, "bar-label", "middle");
      });
      out += "</svg>";
      byId(id).innerHTML = out;
    }

    function qualityRateBars(id, row, fields, options) {
      const total = value(row, "review_count") || 1;
      const rows = fields.map(([key, label]) => ({ label, rate: value(row, key) / total, count: value(row, key) }));
      horizontalBarChart(id, rows, {
        labelKey: "label",
        valueKey: "rate",
        limit: rows.length,
        left: options.left || 170,
        right: options.right || 110,
        bottom: options.bottom || 24,
        rowHeight: options.rowHeight || 26,
        labelMax: options.labelMax || 30,
        scaleMax: options.scaleMax,
        valueColumn: options.valueColumn,
        tickValues: options.tickValues,
        tickFormat: options.tickFormat,
        color: options.color,
        format: (rate, r) => `${fmtPct(rate)} (${fmtInt(r.count)})`
      });
    }

    function renderTable(id, rows, columns) {
      const head = columns.map(([key, label]) => `<th>${escapeHtml(label)}</th>`).join("");
      const body = rows.map(row => `<tr>${columns.map(([key, label, format]) => `<td>${escapeHtml(format ? format(row[key], row) : row[key])}</td>`).join("")}</tr>`).join("");
      byId(id).innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function renderAll() {
      renderKpis();
      horizontalBarChart("categoryVolume", summary.volume.by_category, {
        labelKey: "category",
        valueKey: "review_count",
        limit: 26,
        left: 165,
        rowHeight: 24,
        color: palette.teal
      });
      horizontalBarChart("topApps", summary.volume.by_app_top_50, {
        labelKey: "app_name",
        valueKey: "review_count",
        limit: 20,
        left: 190,
        rowHeight: 26,
        color: palette.blue
      });
      singleStackedRating("ratingOverall", summary.ratings.overall);
      stackedRatingBars("ratingCategory", summary.ratings.by_category, { labelKey: "category", limit: 20, left: 160 });
      lineChart("monthlyDensity", summary.time_coverage.monthly_recent_24, {
        labelKey: "month",
        valueKey: "review_count",
        color: palette.purple,
        area: "#ece7fb"
      });
      horizontalBarChart("staleApps", summary.time_coverage.freshness_by_app_stalest_50, {
        labelKey: "app_name",
        valueKey: "newest_review_age_days",
        limit: 10,
        left: 190,
        rowHeight: 25,
        color: palette.amber,
        format: v => `${fmtDecimal(v, 1)} days`
      });
      lengthBoxPlot("lengthBox", summary.text_quality.length_summary);
      qualityRateBars("lowSignal", summary.text_quality.low_signal, [
        ["content_1_to_20_chars", "1 to 20 chars"],
        ["content_21_to_50_chars", "21 to 50 chars"],
        ["non_ascii_content", "Non-ASCII"],
        ["url_like_content", "URL-like"],
        ["html_like_content", "HTML-like"],
        ["blank_content", "Blank content"]
      ], {
        color: palette.amber,
        left: 150,
        right: 210,
        bottom: 42,
        rowHeight: 38,
        scaleMax: 0.5,
        valueColumn: true,
        tickValues: [0.25, 0.5],
        tickFormat: fmtPct
      });
      statusCodeChart("statusCodes", summary.pipeline_behavior.status_codes);
      attemptHealthChart("attempts", summary.pipeline_behavior.attempt_counts);
      terminalReasonChart("terminalReasons", summary.pipeline_behavior.terminal_reasons);
      renderTable("topAppsTable", summary.volume.by_app_top_50.slice(0, 30), [
        ["app_name", "App"],
        ["category", "Category"],
        ["review_count", "Reviews", fmtInt],
        ["reviews_last_30_days", "Last 30d", fmtInt]
      ]);
      renderTable("duplicatesTable", summary.text_quality.duplicate_examples, [
        ["sample", "Normalized text"],
        ["row_count", "Rows", fmtInt],
        ["app_count", "Apps", fmtInt]
      ]);
      renderTable("pipelineAppTable", summary.pipeline_behavior.by_app_top_50_pages.slice(0, 30), [
        ["app_name", "App"],
        ["page_count", "Pages", fmtInt],
        ["page_review_rows", "Rows", fmtInt],
        ["http_429_pages", "429", fmtInt],
        ["retried_pages", "Retries", fmtInt],
        ["max_run_page_window_minutes", "Max min", v => fmtDecimal(v, 1)]
      ]);
    }
    renderAll();
  </script>
</body>
</html>
"""
    return template.replace("__SUMMARY_JSON__", data_json)


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.4g}"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text[:240]


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def convert_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): convert_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [convert_json(item) for item in value]
    if isinstance(value, tuple):
        return [convert_json(item) for item in value]
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
