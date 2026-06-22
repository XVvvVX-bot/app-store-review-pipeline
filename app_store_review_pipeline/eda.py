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


def generate_eda_report(
    database_url: str = DEFAULT_DATABASE_URL,
    *,
    source: str = WEB_CATALOG_SOURCE,
    markdown_path: Path = DEFAULT_EDA_MARKDOWN,
    json_path: Path = DEFAULT_EDA_JSON,
) -> dict[str, Any]:
    summary = build_eda_summary(database_url, source=source)
    markdown = render_eda_markdown(summary)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "source": source,
        "database_url": mask_database_url(database_url),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
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
    missingness = summary["missingness"]
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
        "## Missingness",
        "",
        markdown_table([missingness], [
            "review_count",
            "missing_version",
            "missing_vote_sum",
            "missing_vote_count",
            "missing_author_name",
            "missing_title",
            "missing_content",
            "missing_updated_at",
            "missing_updated_epoch_seconds",
            "missing_rating",
        ]),
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
        "- Apple public web catalog reviews are public structured catalog data, not a contractual App Store Connect API.",
        "- A scope is only historically exhausted when a backfill reaches `no_next_href`; page cap, time budget, overlap, and error stops are lower-bound evidence.",
        "- `vote_sum` and `vote_count` availability depends on the fields Apple returns in the public catalog response.",
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
