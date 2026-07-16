from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app_store_review_pipeline.config import DEFAULT_SORT_BY, PLATFORM, SOURCE, WEB_CATALOG_SOURCE
from app_store_review_pipeline.files import read_jsonl
from app_store_review_pipeline.targets import load_targets
from app_store_review_pipeline.utils import utc_timestamp


PRESSURE_SOFT_ERROR_MIN_PAGES = 100
PRESSURE_SOFT_ERROR_MAX_RATE = 0.01
PRESSURE_SOFT_ERROR_MAX_COUNT = 3


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_store_runs (
    run_id TEXT PRIMARY KEY,
    raw_dir TEXT NOT NULL,
    targets_path TEXT,
    loaded_at TEXT NOT NULL,
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    target_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    reviews_inserted INTEGER NOT NULL DEFAULT 0,
    reviews_updated INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    fetch_errors INTEGER NOT NULL DEFAULT 0,
    capped_scopes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_store_targets (
    app_id TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    category TEXT,
    apple_slug TEXT,
    countries TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    first_seen_run_id TEXT,
    last_seen_run_id TEXT
);

CREATE TABLE IF NOT EXISTS app_store_review_pages (
    page_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES app_store_runs(run_id),
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    app_id TEXT NOT NULL REFERENCES app_store_targets(app_id),
    app_name TEXT,
    country TEXT NOT NULL,
    sort_by TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    request_url TEXT,
    status TEXT NOT NULL,
    status_code INTEGER,
    fetched_at TEXT,
    raw_json_path TEXT,
    response_bytes BIGINT NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    unique_review_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    missing_text_count INTEGER NOT NULL DEFAULT 0,
    missing_rating_count INTEGER NOT NULL DEFAULT 0,
    missing_updated_count INTEGER NOT NULL DEFAULT 0,
    max_updated_epoch_seconds BIGINT,
    min_updated_epoch_seconds BIGINT,
    has_next_link INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    terminal_reason TEXT,
    overlap_review_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, app_id, country, sort_by, page_number)
);

CREATE TABLE IF NOT EXISTS app_store_reviews (
    review_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    app_id TEXT NOT NULL REFERENCES app_store_targets(app_id),
    app_name TEXT,
    country TEXT NOT NULL,
    review_id TEXT NOT NULL,
    author_name TEXT,
    updated_at TEXT,
    updated_epoch_seconds BIGINT,
    rating INTEGER,
    title TEXT,
    content TEXT,
    first_seen_run_id TEXT NOT NULL REFERENCES app_store_runs(run_id),
    last_seen_run_id TEXT NOT NULL REFERENCES app_store_runs(run_id),
    source_page_key TEXT REFERENCES app_store_review_pages(page_key),
    collected_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    row_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, source, country, app_id, review_id)
);

CREATE TABLE IF NOT EXISTS app_store_review_changes (
    change_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES app_store_runs(run_id),
    review_key TEXT NOT NULL REFERENCES app_store_reviews(review_key),
    app_id TEXT NOT NULL REFERENCES app_store_targets(app_id),
    country TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('inserted', 'updated')),
    previous_updated_epoch_seconds BIGINT,
    new_updated_epoch_seconds BIGINT,
    source_page_key TEXT REFERENCES app_store_review_pages(page_key),
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, review_key)
);

CREATE TABLE IF NOT EXISTS app_store_sync_state (
    scope_key TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES app_store_targets(app_id),
    country TEXT NOT NULL,
    sort_by TEXT NOT NULL DEFAULT 'mostrecent',
    complete_through_updated_epoch_seconds BIGINT NOT NULL DEFAULT 0,
    backlogged INTEGER NOT NULL DEFAULT 1,
    last_started_at TEXT,
    last_completed_at TEXT,
    last_run_id TEXT,
    last_successful_run_id TEXT,
    last_terminal_reason TEXT,
    last_page_count INTEGER NOT NULL DEFAULT 0,
    last_review_count INTEGER NOT NULL DEFAULT 0,
    last_overlap_review_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_store_pressure_state (
    source TEXT PRIMARY KEY,
    next_max_pages_per_app_country INTEGER NOT NULL DEFAULT 5,
    safe_max_pages_per_app_country INTEGER NOT NULL DEFAULT 5,
    candidate_max_pages_per_app_country INTEGER NOT NULL DEFAULT 5,
    next_max_parallel INTEGER NOT NULL DEFAULT 1,
    safe_max_parallel INTEGER NOT NULL DEFAULT 1,
    candidate_max_parallel INTEGER NOT NULL DEFAULT 1,
    next_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800,
    safe_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800,
    candidate_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800,
    clean_run_count INTEGER NOT NULL DEFAULT 0,
    search_phase TEXT NOT NULL DEFAULT 'probe',
    cooldown_until TEXT,
    confirmed_429_count INTEGER NOT NULL DEFAULT 0,
    last_result TEXT,
    last_started_at TEXT,
    last_completed_at TEXT,
    last_used_pages INTEGER NOT NULL DEFAULT 0,
    last_safe_pages INTEGER NOT NULL DEFAULT 5,
    last_used_parallel INTEGER NOT NULL DEFAULT 1,
    last_safe_parallel INTEGER NOT NULL DEFAULT 1,
    last_used_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800,
    last_safe_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800,
    last_page_count INTEGER NOT NULL DEFAULT 0,
    last_ok_page_count INTEGER NOT NULL DEFAULT 0,
    last_http_429_page_count INTEGER NOT NULL DEFAULT 0,
    last_error_page_count INTEGER NOT NULL DEFAULT 0,
    last_final_non_200_page_count INTEGER NOT NULL DEFAULT 0,
    last_retried_page_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_app_store_reviews_app_country_updated
    ON app_store_reviews(app_id, country, updated_epoch_seconds DESC);
CREATE INDEX IF NOT EXISTS idx_app_store_reviews_run
    ON app_store_reviews(last_seen_run_id);
CREATE INDEX IF NOT EXISTS idx_app_store_review_pages_run
    ON app_store_review_pages(run_id);
CREATE INDEX IF NOT EXISTS idx_app_store_changes_run
    ON app_store_review_changes(run_id);
CREATE INDEX IF NOT EXISTS idx_app_store_sync_backlogged
    ON app_store_sync_state(backlogged);
"""

POSTGRES_SCHEMA_MIGRATIONS = (
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS safe_max_pages_per_app_country INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS candidate_max_pages_per_app_country INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS next_max_parallel INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS safe_max_parallel INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS candidate_max_parallel INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS next_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS safe_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS candidate_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS search_phase TEXT NOT NULL DEFAULT 'probe'",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS cooldown_until TEXT",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS confirmed_429_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS last_safe_pages INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS last_used_parallel INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS last_safe_parallel INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS last_used_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800",
    "ALTER TABLE app_store_pressure_state ADD COLUMN IF NOT EXISTS last_safe_scope_time_budget_seconds INTEGER NOT NULL DEFAULT 1800",
    "ALTER TABLE app_store_reviews DROP COLUMN IF EXISTS version",
    "ALTER TABLE app_store_reviews DROP COLUMN IF EXISTS vote_sum",
    "ALTER TABLE app_store_reviews DROP COLUMN IF EXISTS vote_count",
)

POSTGRES_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
POSTGRES_MIGRATION_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_store_schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

POSTGRES_SCHEMA_ADVISORY_LOCK_ID = 63206438020260619
POSTGRES_SCHEMA_MAX_ATTEMPTS = 5
POSTGRES_SCHEMA_RETRY_SECONDS = 0.5
POSTGRES_LOAD_MAX_ATTEMPTS = 4
POSTGRES_LOAD_RETRY_SECONDS = 2.0
POSTGRES_SYNC_STATE_MAX_ATTEMPTS = 4
POSTGRES_SYNC_STATE_RETRY_SECONDS = 1.0
POSTGRES_RETRYABLE_WRITE_ERRORS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    psycopg.errors.LockNotAvailable,
)

SUCCESS_TERMINAL_REASONS = {
    "caught_up_to_existing_reviews",
    "no_next_href",
    "target_review_count_reached",
    "target_review_count_zero",
}
BACKLOG_TERMINAL_REASONS = {
    "page_cap",
    "empty_page_before_overlap",
    "empty_page_after_sparse_scan",
    "time_budget_exceeded",
    "scope_time_budget_exceeded",
    "time_budget_retry_window_exceeded",
    "scope_time_budget_retry_window_exceeded",
}
HARD_FAILURE_TERMINAL_REASONS = {
    "fetch_error",
    "sparse_fetch_error_threshold",
}


def connect_postgres(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def initialize_postgres(database_url: str) -> None:
    for attempt in range(1, POSTGRES_SCHEMA_MAX_ATTEMPTS + 1):
        try:
            with connect_postgres(database_url) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (POSTGRES_SCHEMA_ADVISORY_LOCK_ID,),
                )
                connection.execute(POSTGRES_SCHEMA)
                for statement in POSTGRES_SCHEMA_MIGRATIONS:
                    connection.execute(statement)
                connection.execute(POSTGRES_MIGRATION_LEDGER_SCHEMA)
                apply_versioned_migrations(connection)
                connection.commit()
            return
        except psycopg.errors.DeadlockDetected:
            if attempt >= POSTGRES_SCHEMA_MAX_ATTEMPTS:
                raise
            time.sleep(POSTGRES_SCHEMA_RETRY_SECONDS * attempt)


def apply_versioned_migrations(connection: psycopg.Connection) -> list[str]:
    applied = []
    for path in sorted(POSTGRES_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        inserted = connection.execute(
            """
            INSERT INTO app_store_schema_migrations (version, checksum)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING
            RETURNING version
            """,
            (path.name, checksum),
        ).fetchone()
        if inserted:
            connection.execute(sql)
            applied.append(path.name)
            continue
        existing = connection.execute(
            "SELECT checksum FROM app_store_schema_migrations WHERE version = %s",
            (path.name,),
        ).fetchone()
        if not existing or existing["checksum"] != checksum:
            raise RuntimeError(f"Postgres migration checksum mismatch: {path.name}")
    return applied


def backfill_typed_timestamps_postgres(
    database_url: str,
    *,
    batch_size: int = 5_000,
    max_batches: int = 0,
    initialize_schema: bool = True,
) -> dict[str, int]:
    """Backfill large typed timestamp columns in short committed batches."""
    if initialize_schema:
        initialize_postgres(database_url)
    batch_size = max(1, int(batch_size))
    max_batches = max(0, int(max_batches))
    totals = {"review_changes": 0, "reviews": 0, "batches": 0}
    while max_batches == 0 or totals["batches"] < max_batches:
        with connect_postgres(database_url) as connection:
            changed_rows = connection.execute(
                """
                WITH batch AS (
                    SELECT change_id
                    FROM app_store_review_changes
                    WHERE changed_at_ts IS NULL AND changed_at IS NOT NULL
                    ORDER BY change_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE app_store_review_changes changes
                SET changed_at_ts = changes.changed_at::timestamptz
                FROM batch
                WHERE changes.change_id = batch.change_id
                """,
                (batch_size,),
            ).rowcount
            review_rows = connection.execute(
                """
                WITH batch AS (
                    SELECT review_key
                    FROM app_store_reviews
                    WHERE collected_at_ts IS NULL AND collected_at IS NOT NULL
                    ORDER BY review_key
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE app_store_reviews reviews
                SET collected_at_ts = reviews.collected_at::timestamptz
                FROM batch
                WHERE reviews.review_key = batch.review_key
                """,
                (batch_size,),
            ).rowcount
            connection.commit()
        totals["review_changes"] += int(changed_rows or 0)
        totals["reviews"] += int(review_rows or 0)
        totals["batches"] += 1
        if not changed_rows and not review_rows:
            break
    return totals


def retryable_postgres_write_error(error: BaseException) -> bool:
    return isinstance(error, POSTGRES_RETRYABLE_WRITE_ERRORS)


def mask_database_url(database_url: str) -> str:
    if "://" not in database_url:
        return database_url
    parsed = urlsplit(database_url)
    if not parsed.password:
        return database_url
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{username}:***@{host}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def upsert_execution_postgres(
    database_url: str,
    *,
    execution_id: str,
    source: str,
    started_at: str,
    github_run_id: str = "",
    github_run_attempt: int = 1,
    workflow_name: str = "",
    event_name: str = "",
    git_sha: str = "",
    scope_signature: str = "",
    config_signature: str = "",
    intended_target_count: int = 0,
    intended_scope_count: int = 0,
    initialize_schema: bool = True,
) -> dict[str, Any]:
    if initialize_schema:
        initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        connection.execute(
            """
            INSERT INTO app_store_executions (
                execution_id, github_run_id, github_run_attempt, workflow_name,
                event_name, git_sha, source, scope_signature, config_signature,
                intended_target_count, intended_scope_count, started_at
            )
            VALUES (%s, NULLIF(%s, ''), %s, NULLIF(%s, ''), NULLIF(%s, ''),
                    NULLIF(%s, ''), %s, NULLIF(%s, ''), NULLIF(%s, ''), %s, %s, %s)
            ON CONFLICT (execution_id) DO UPDATE
            SET github_run_id = COALESCE(EXCLUDED.github_run_id, app_store_executions.github_run_id),
                github_run_attempt = EXCLUDED.github_run_attempt,
                workflow_name = COALESCE(EXCLUDED.workflow_name, app_store_executions.workflow_name),
                event_name = COALESCE(EXCLUDED.event_name, app_store_executions.event_name),
                git_sha = COALESCE(EXCLUDED.git_sha, app_store_executions.git_sha),
                scope_signature = COALESCE(EXCLUDED.scope_signature, app_store_executions.scope_signature),
                config_signature = COALESCE(EXCLUDED.config_signature, app_store_executions.config_signature),
                intended_target_count = GREATEST(
                    app_store_executions.intended_target_count,
                    EXCLUDED.intended_target_count
                ),
                intended_scope_count = GREATEST(
                    app_store_executions.intended_scope_count,
                    EXCLUDED.intended_scope_count
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                execution_id,
                github_run_id,
                max(1, int(github_run_attempt or 1)),
                workflow_name,
                event_name,
                git_sha,
                source,
                scope_signature,
                config_signature,
                max(0, int(intended_target_count or 0)),
                max(0, int(intended_scope_count or 0)),
                started_at,
            ),
        )
        connection.commit()
    return {
        "execution_id": execution_id,
        "github_run_id": github_run_id,
        "github_run_attempt": max(1, int(github_run_attempt or 1)),
        "intended_target_count": max(0, int(intended_target_count or 0)),
        "intended_scope_count": max(0, int(intended_scope_count or 0)),
    }


def finalize_execution_postgres(
    database_url: str,
    *,
    execution_id: str,
    status: str,
    completed_at: str | None = None,
    initialize_schema: bool = True,
) -> dict[str, Any]:
    if status not in {"healthy", "degraded", "failing", "cancelled"}:
        raise ValueError(f"Unsupported execution status: {status}")
    if initialize_schema:
        initialize_postgres(database_url)
    completed_at = completed_at or utc_timestamp()
    with connect_postgres(database_url) as connection:
        counts = refresh_execution_scope_counts(connection, execution_id)
        updated = connection.execute(
            """
            UPDATE app_store_executions
            SET status = %s,
                completed_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE execution_id = %s
            RETURNING execution_id
            """,
            (status, completed_at, execution_id),
        ).fetchone()
        connection.commit()
    if not updated:
        raise ValueError(f"Unknown execution_id: {execution_id}")
    return {"execution_id": execution_id, "status": status, "completed_at": completed_at, **counts}


def record_monitor_snapshot_postgres(
    database_url: str,
    *,
    execution_id: str,
    status: str,
    metrics: dict[str, Any],
    initialize_schema: bool = True,
) -> dict[str, Any]:
    if status not in {"healthy", "degraded", "failing"}:
        raise ValueError(f"Unsupported monitor status: {status}")
    if initialize_schema:
        initialize_postgres(database_url)
    database_snapshot = metrics.get("database_snapshot") or []
    counts = {
        str(row.get("table_name")): {
            "row_count": int(row.get("row_count") or 0),
            "total_bytes": int(row.get("total_bytes") or 0),
        }
        for row in database_snapshot
    }
    review_count = counts.get("app_store_reviews", {}).get("row_count", 0)
    page_count = counts.get("app_store_review_pages", {}).get("row_count", 0)
    run_count = counts.get("app_store_runs", {}).get("row_count", 0)
    change_count = counts.get("app_store_review_changes", {}).get("row_count", 0)
    database_bytes = sum(row["total_bytes"] for row in counts.values())
    with connect_postgres(database_url) as connection:
        row = connection.execute(
            """
            INSERT INTO app_store_monitor_snapshots (
                execution_id, status, review_row_count, page_row_count,
                run_row_count, change_row_count, database_bytes, metrics
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (execution_id) WHERE execution_id IS NOT NULL DO UPDATE
            SET status = EXCLUDED.status,
                review_row_count = EXCLUDED.review_row_count,
                page_row_count = EXCLUDED.page_row_count,
                run_row_count = EXCLUDED.run_row_count,
                change_row_count = EXCLUDED.change_row_count,
                database_bytes = EXCLUDED.database_bytes,
                metrics = EXCLUDED.metrics,
                captured_at = CURRENT_TIMESTAMP
            RETURNING snapshot_id, captured_at
            """,
            (
                execution_id,
                status,
                review_count,
                page_count,
                run_count,
                change_count,
                database_bytes,
                Jsonb(metrics),
            ),
        ).fetchone()
        connection.commit()
    return {
        "snapshot_id": int(row["snapshot_id"]),
        "execution_id": execution_id,
        "status": status,
        "captured_at": row["captured_at"],
        "database_bytes": database_bytes,
    }


def refresh_execution_scope_counts(connection: psycopg.Connection, execution_id: str) -> dict[str, int]:
    counts = connection.execute(
        """
        SELECT
            COUNT(*)::integer AS completed_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'caught_up')::integer AS caught_up_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'backlogged')::integer AS backlogged_scope_count,
            COUNT(*) FILTER (WHERE outcome = 'hard_failure')::integer AS hard_failure_scope_count
        FROM app_store_run_scopes
        WHERE execution_id = %s
        """,
        (execution_id,),
    ).fetchone()
    values = {
        key: int(counts[key] or 0)
        for key in (
            "completed_scope_count",
            "caught_up_scope_count",
            "backlogged_scope_count",
            "hard_failure_scope_count",
        )
    }
    connection.execute(
        """
        UPDATE app_store_executions
        SET completed_scope_count = %s,
            caught_up_scope_count = %s,
            backlogged_scope_count = %s,
            hard_failure_scope_count = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE execution_id = %s
        """,
        (
            values["completed_scope_count"],
            values["caught_up_scope_count"],
            values["backlogged_scope_count"],
            values["hard_failure_scope_count"],
            execution_id,
        ),
    )
    return values


def web_catalog_429_circuit_breaker_status(
    database_url: str,
    *,
    source: str = WEB_CATALOG_SOURCE,
    since: str | None = None,
    lookback_minutes: int = 60,
    min_pages: int = 4,
    max_rate: float = 0.5,
) -> dict:
    initialize_postgres(database_url)
    min_pages = max(0, int(min_pages))
    max_rate = max(0.0, float(max_rate))
    lookback_minutes = max(0, int(lookback_minutes))

    where_clauses = ["source = %s", "fetched_at_ts IS NOT NULL"]
    params: list = [source]
    if since:
        where_clauses.append("fetched_at_ts >= %s::timestamptz")
        params.append(since)
        window = {"since": since}
    elif lookback_minutes:
        where_clauses.append("fetched_at_ts >= now() - (%s * INTERVAL '1 minute')")
        params.append(lookback_minutes)
        window = {"lookback_minutes": lookback_minutes}
    else:
        window = {"lookback_minutes": 0}

    with connect_postgres(database_url) as connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS page_count,
                COUNT(*) FILTER (WHERE status_code = 429) AS http_429_page_count,
                COUNT(*) FILTER (
                    WHERE http_429_attempt_count > 0 OR status_code = 429
                ) AS http_429_affected_page_count,
                COALESCE(SUM(http_429_attempt_count), 0) AS http_429_attempt_count,
                COUNT(*) FILTER (WHERE status = 'ok') AS ok_page_count,
                COUNT(*) FILTER (WHERE status = 'error') AS error_page_count,
                MIN(fetched_at_ts) AS first_page_at,
                MAX(fetched_at_ts) AS last_page_at
            FROM app_store_review_pages
            WHERE {" AND ".join(where_clauses)}
            """,
            tuple(params),
        ).fetchone()

    page_count = int(row["page_count"] or 0)
    http_429_page_count = int(row["http_429_page_count"] or 0)
    http_429_affected_page_count = int(
        row.get("http_429_affected_page_count", http_429_page_count) or 0
    )
    http_429_rate = http_429_affected_page_count / page_count if page_count else 0.0
    tripped = page_count >= min_pages and max_rate > 0 and http_429_rate >= max_rate
    return {
        "source": source,
        "window": window,
        "page_count": page_count,
        "http_429_page_count": http_429_page_count,
        "http_429_affected_page_count": http_429_affected_page_count,
        "http_429_attempt_count": int(row.get("http_429_attempt_count", http_429_page_count) or 0),
        "ok_page_count": int(row["ok_page_count"] or 0),
        "error_page_count": int(row["error_page_count"] or 0),
        "http_429_rate": http_429_rate,
        "min_pages": min_pages,
        "max_rate": max_rate,
        "tripped": tripped,
        "first_page_at": str(row["first_page_at"]) if row["first_page_at"] is not None else None,
        "last_page_at": str(row["last_page_at"]) if row["last_page_at"] is not None else None,
    }


def web_catalog_429_cooldown_status(
    database_url: str,
    *,
    source: str = WEB_CATALOG_SOURCE,
    cooldown_minutes: int = 180,
) -> dict:
    initialize_postgres(database_url)
    cooldown_minutes = max(0, int(cooldown_minutes))

    with connect_postgres(database_url) as connection:
        row = connection.execute(
            """
            SELECT
                MAX(fetched_at_ts) FILTER (
                    WHERE http_429_attempt_count > 0 OR status_code = 429
                ) AS last_http_429_at,
                EXTRACT(EPOCH FROM (
                    now() - MAX(fetched_at_ts) FILTER (
                        WHERE http_429_attempt_count > 0 OR status_code = 429
                    )
                )) / 60.0
                    AS minutes_since_last_http_429,
                COUNT(*) FILTER (
                    WHERE fetched_at_ts >= now() - (%s * INTERVAL '1 minute')
                        AND (http_429_attempt_count > 0 OR status_code = 429)
                ) AS http_429_count_in_cooldown,
                COALESCE(SUM(http_429_attempt_count) FILTER (
                    WHERE fetched_at_ts >= now() - (%s * INTERVAL '1 minute')
                ), 0) AS http_429_attempt_count_in_cooldown
            FROM app_store_review_pages
            WHERE source = %s
                AND fetched_at_ts IS NOT NULL
            """,
            (cooldown_minutes, cooldown_minutes, source),
        ).fetchone()

    last_http_429_at = row["last_http_429_at"]
    minutes_since_last_http_429 = row["minutes_since_last_http_429"]
    if minutes_since_last_http_429 is not None:
        minutes_since_last_http_429 = float(minutes_since_last_http_429)
    tripped = (
        cooldown_minutes > 0
        and last_http_429_at is not None
        and minutes_since_last_http_429 is not None
        and minutes_since_last_http_429 < cooldown_minutes
    )
    return {
        "source": source,
        "cooldown_minutes": cooldown_minutes,
        "last_http_429_at": str(last_http_429_at) if last_http_429_at is not None else None,
        "minutes_since_last_http_429": minutes_since_last_http_429,
        "http_429_count_in_cooldown": int(row["http_429_count_in_cooldown"] or 0),
        "http_429_attempt_count_in_cooldown": int(
            row.get("http_429_attempt_count_in_cooldown", row["http_429_count_in_cooldown"]) or 0
        ),
        "tripped": tripped,
    }


WEB_CATALOG_PRESSURE_RAMP: tuple[tuple[int, int], ...] = (
    (0, 5),
    (1, 7),
    (2, 10),
    (3, 12),
    (4, 15),
    (5, 20),
    (6, 25),
)

WEB_CATALOG_SCOPE_TIME_RAMP_SECONDS: tuple[int, ...] = (
    1800,
    2700,
    3600,
    5400,
    7200,
)


def clamp_pressure_pages(value: int, *, base_pages: int, max_pages: int) -> int:
    if int(value) <= 0 or int(max_pages) <= 0:
        return 0
    base_pages = max(1, int(base_pages))
    return min(max_pages, max(base_pages, int(value)))


def next_pressure_pages(current_pages: int, *, base_pages: int, max_pages: int) -> int:
    current_pages = clamp_pressure_pages(current_pages, base_pages=base_pages, max_pages=max_pages)
    if current_pages == 0:
        return 0
    for _, candidate_pages in WEB_CATALOG_PRESSURE_RAMP:
        candidate_pages = clamp_pressure_pages(candidate_pages, base_pages=base_pages, max_pages=max_pages)
        if candidate_pages > current_pages:
            return candidate_pages
    return current_pages


def previous_pressure_pages(current_pages: int, *, base_pages: int, max_pages: int) -> int:
    current_pages = clamp_pressure_pages(current_pages, base_pages=base_pages, max_pages=max_pages)
    if current_pages == 0:
        return 0
    previous_pages = base_pages
    for _, candidate_pages in WEB_CATALOG_PRESSURE_RAMP:
        candidate_pages = clamp_pressure_pages(candidate_pages, base_pages=base_pages, max_pages=max_pages)
        if candidate_pages >= current_pages:
            return previous_pages
        previous_pages = candidate_pages
    return previous_pages


def next_scope_time_budget_seconds(current_seconds: int, *, base_seconds: int, max_seconds: int) -> int:
    current_seconds = min(max_seconds, max(base_seconds, int(current_seconds)))
    for candidate_seconds in WEB_CATALOG_SCOPE_TIME_RAMP_SECONDS:
        candidate_seconds = min(max_seconds, max(base_seconds, candidate_seconds))
        if candidate_seconds > current_seconds:
            return candidate_seconds
    return current_seconds


def previous_scope_time_budget_seconds(current_seconds: int, *, base_seconds: int, max_seconds: int) -> int:
    current_seconds = min(max_seconds, max(base_seconds, int(current_seconds)))
    previous_seconds = base_seconds
    for candidate_seconds in WEB_CATALOG_SCOPE_TIME_RAMP_SECONDS:
        candidate_seconds = min(max_seconds, max(base_seconds, candidate_seconds))
        if candidate_seconds >= current_seconds:
            return previous_seconds
        previous_seconds = candidate_seconds
    return previous_seconds


def next_pressure_config(
    *,
    used_pages: int,
    used_parallel: int,
    used_scope_time_budget_seconds: int,
    base_pages: int,
    max_pages: int,
    base_parallel: int,
    max_parallel: int,
    base_scope_time_budget_seconds: int,
    max_scope_time_budget_seconds: int,
) -> dict:
    safe_pages = clamp_pressure_pages(used_pages, base_pages=base_pages, max_pages=max_pages)
    safe_parallel = min(max_parallel, max(base_parallel, int(used_parallel)))
    safe_scope_time_budget_seconds = min(
        max_scope_time_budget_seconds,
        max(base_scope_time_budget_seconds, int(used_scope_time_budget_seconds)),
    )
    if safe_parallel < max_parallel:
        return {
            "max_pages_per_app_country": safe_pages,
            "max_parallel": safe_parallel + 1,
            "scope_time_budget_seconds": safe_scope_time_budget_seconds,
        }
    next_scope_seconds = next_scope_time_budget_seconds(
        safe_scope_time_budget_seconds,
        base_seconds=base_scope_time_budget_seconds,
        max_seconds=max_scope_time_budget_seconds,
    )
    return {
        "max_pages_per_app_country": safe_pages,
        "max_parallel": safe_parallel,
        "scope_time_budget_seconds": next_scope_seconds,
    }


def previous_pressure_config(
    *,
    used_pages: int,
    used_parallel: int,
    used_scope_time_budget_seconds: int,
    base_pages: int,
    max_pages: int,
    base_parallel: int,
    max_parallel: int,
    base_scope_time_budget_seconds: int,
    max_scope_time_budget_seconds: int,
) -> dict:
    safe_pages = clamp_pressure_pages(used_pages, base_pages=base_pages, max_pages=max_pages)
    used_parallel = min(max_parallel, max(base_parallel, int(used_parallel)))
    used_scope_time_budget_seconds = min(
        max_scope_time_budget_seconds,
        max(base_scope_time_budget_seconds, int(used_scope_time_budget_seconds)),
    )
    if used_scope_time_budget_seconds > base_scope_time_budget_seconds:
        return {
            "max_pages_per_app_country": safe_pages,
            "max_parallel": used_parallel,
            "scope_time_budget_seconds": previous_scope_time_budget_seconds(
                used_scope_time_budget_seconds,
                base_seconds=base_scope_time_budget_seconds,
                max_seconds=max_scope_time_budget_seconds,
            ),
        }
    return {
        "max_pages_per_app_country": safe_pages,
        "max_parallel": max(base_parallel, used_parallel - 1),
        "scope_time_budget_seconds": base_scope_time_budget_seconds,
    }


def pressure_cooldown_timestamp(minutes: int) -> str | None:
    minutes = max(0, int(minutes))
    if minutes <= 0:
        return None
    cooldown_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return cooldown_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pressure_state_int(state: dict | None, key: str, default: int) -> int:
    if not state:
        return default
    value = state.get(key)
    return int(value) if value is not None else default


def pressure_state_text(state: dict | None, key: str, default: str) -> str:
    if not state:
        return default
    value = state.get(key)
    return str(value) if value is not None else default


def json_safe_database_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def json_safe_database_row(row: dict | None) -> dict | None:
    if not row:
        return None
    return {key: json_safe_database_value(value) for key, value in dict(row).items()}


def web_catalog_recent_pressure_metrics(
    connection: psycopg.Connection,
    *,
    source: str,
    lookback_minutes: int = 720,
    since: str | None = None,
) -> dict:
    where_clauses = ["source = %s", "fetched_at_ts IS NOT NULL"]
    params: list = [source]
    if since:
        where_clauses.append("fetched_at_ts >= %s::timestamptz")
        params.append(since)
    elif lookback_minutes:
        where_clauses.append("fetched_at_ts >= now() - (%s * INTERVAL '1 minute')")
        params.append(lookback_minutes)

    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS page_count,
            COUNT(*) FILTER (WHERE status = 'ok') AS ok_page_count,
            COUNT(*) FILTER (WHERE status = 'error') AS error_page_count,
            COUNT(*) FILTER (WHERE status_code = 429) AS http_429_page_count,
            COUNT(*) FILTER (
                WHERE http_429_attempt_count > 0 OR status_code = 429
            ) AS http_429_affected_page_count,
            COALESCE(SUM(http_429_attempt_count), 0) AS http_429_attempt_count,
            COUNT(*) FILTER (
                WHERE status_code IS NOT NULL
                    AND (status_code < 200 OR status_code >= 300)
            ) AS final_non_200_page_count,
            COUNT(*) FILTER (
                WHERE status = 'error'
                    AND (
                        status_code IS NULL
                        OR (status_code >= 200 AND status_code < 300)
                    )
            ) AS soft_error_page_count,
            COUNT(*) FILTER (WHERE attempt_count > 1) AS retried_page_count,
            MIN(fetched_at_ts) AS first_page_at,
            MAX(fetched_at_ts) AS last_page_at
        FROM app_store_review_pages
        WHERE {" AND ".join(where_clauses)}
        """,
        tuple(params),
    ).fetchone()
    return {
        "page_count": int(row["page_count"] or 0),
        "ok_page_count": int(row["ok_page_count"] or 0),
        "error_page_count": int(row["error_page_count"] or 0),
        "http_429_page_count": int(row["http_429_page_count"] or 0),
        "http_429_affected_page_count": int(
            row.get("http_429_affected_page_count", row["http_429_page_count"]) or 0
        ),
        "http_429_attempt_count": int(
            row.get("http_429_attempt_count", row["http_429_page_count"]) or 0
        ),
        "final_non_200_page_count": int(row["final_non_200_page_count"] or 0),
        "soft_error_page_count": int(row.get("soft_error_page_count") or 0),
        "retried_page_count": int(row["retried_page_count"] or 0),
        "first_page_at": row["first_page_at"],
        "last_page_at": row["last_page_at"],
    }


def pressure_soft_error_threshold_exceeded(metrics: dict) -> bool:
    page_count = int(metrics["page_count"] or 0)
    soft_error_page_count = int(metrics.get("soft_error_page_count") or 0)
    if soft_error_page_count <= 0:
        return False
    if soft_error_page_count > PRESSURE_SOFT_ERROR_MAX_COUNT:
        return True
    return (
        page_count >= PRESSURE_SOFT_ERROR_MIN_PAGES
        and soft_error_page_count / max(page_count, 1) > PRESSURE_SOFT_ERROR_MAX_RATE
    )


def pressure_metrics_are_clean(metrics: dict) -> bool:
    return (
        int(metrics["page_count"] or 0) > 0
        and int(metrics.get("http_429_attempt_count") or metrics["http_429_page_count"] or 0) == 0
        and int(metrics["final_non_200_page_count"] or 0) == 0
        and not pressure_soft_error_threshold_exceeded(metrics)
    )


def web_catalog_pressure_status(
    database_url: str,
    *,
    source: str = WEB_CATALOG_SOURCE,
    lookback_minutes: int = 720,
    base_pages: int = 5,
    max_pages: int = 25,
    base_parallel: int = 1,
    max_parallel: int = 4,
    base_scope_time_budget_seconds: int = 1800,
    max_scope_time_budget_seconds: int = 7200,
    selection_mode: str = "safe",
) -> dict:
    initialize_postgres(database_url)
    lookback_minutes = max(0, int(lookback_minutes))
    base_pages = max(0, int(base_pages))
    max_pages = max(0, int(max_pages))
    base_parallel = max(1, int(base_parallel))
    max_parallel = max(base_parallel, int(max_parallel))
    base_scope_time_budget_seconds = max(1, int(base_scope_time_budget_seconds))
    max_scope_time_budget_seconds = max(base_scope_time_budget_seconds, int(max_scope_time_budget_seconds))
    selection_mode = selection_mode if selection_mode in {"safe", "candidate"} else "safe"

    with connect_postgres(database_url) as connection:
        metrics = web_catalog_recent_pressure_metrics(
            connection,
            source=source,
            lookback_minutes=lookback_minutes,
        )
        state = connection.execute(
            """
            SELECT *,
                CASE
                    WHEN cooldown_until IS NULL THEN 0
                    ELSE GREATEST(0, EXTRACT(EPOCH FROM (cooldown_until::timestamptz - now())))
                END AS cooldown_seconds_remaining
            FROM app_store_pressure_state
            WHERE source = %s
            """,
            (source,),
        ).fetchone()

    page_count = int(metrics["page_count"] or 0)
    clean_for_ramp = pressure_metrics_are_clean(metrics)
    soft_error_page_count = int(metrics.get("soft_error_page_count") or 0)
    soft_error_rate = soft_error_page_count / page_count if page_count else 0.0
    soft_error_threshold_exceeded = pressure_soft_error_threshold_exceeded(metrics)
    safe_pages = clamp_pressure_pages(
        pressure_state_int(state, "safe_max_pages_per_app_country", base_pages),
        base_pages=base_pages,
        max_pages=max_pages,
    )
    candidate_pages = clamp_pressure_pages(
        pressure_state_int(state, "candidate_max_pages_per_app_country", safe_pages),
        base_pages=base_pages,
        max_pages=max_pages,
    )
    safe_parallel = min(
        max_parallel,
        max(base_parallel, pressure_state_int(state, "safe_max_parallel", base_parallel)),
    )
    candidate_parallel = min(
        max_parallel,
        max(base_parallel, pressure_state_int(state, "candidate_max_parallel", safe_parallel)),
    )
    safe_scope_time_budget_seconds = min(
        max_scope_time_budget_seconds,
        max(
            base_scope_time_budget_seconds,
            pressure_state_int(state, "safe_scope_time_budget_seconds", base_scope_time_budget_seconds),
        ),
    )
    candidate_scope_time_budget_seconds = min(
        max_scope_time_budget_seconds,
        max(
            base_scope_time_budget_seconds,
            pressure_state_int(state, "candidate_scope_time_budget_seconds", safe_scope_time_budget_seconds),
        ),
    )

    if not state:
        reason = "no_pressure_state"
        safe_pages = clamp_pressure_pages(base_pages, base_pages=base_pages, max_pages=max_pages)
        candidate_pages = safe_pages
        safe_parallel = base_parallel
        candidate_parallel = base_parallel
        safe_scope_time_budget_seconds = base_scope_time_budget_seconds
        candidate_scope_time_budget_seconds = base_scope_time_budget_seconds
    elif not page_count:
        reason = "no_recent_pages"
    elif not clean_for_ramp:
        reason = "recent_pressure_errors"
    else:
        reason = "stored_pressure_state"

    if selection_mode == "candidate":
        selected_pages = candidate_pages
        selected_parallel = candidate_parallel
        selected_scope_time_budget_seconds = candidate_scope_time_budget_seconds
    else:
        selected_pages = safe_pages
        selected_parallel = safe_parallel
        selected_scope_time_budget_seconds = safe_scope_time_budget_seconds

    selected_level = max(0, sum(1 for _, pages in WEB_CATALOG_PRESSURE_RAMP if pages <= selected_pages) - 1)
    cooldown_seconds_remaining = float((state.get("cooldown_seconds_remaining") if state else 0) or 0)

    return {
        "source": source,
        "lookback_minutes": lookback_minutes,
        "base_pages": base_pages,
        "max_pages": max_pages,
        "base_parallel": base_parallel,
        "max_parallel": max_parallel,
        "base_scope_time_budget_seconds": base_scope_time_budget_seconds,
        "max_scope_time_budget_seconds": max_scope_time_budget_seconds,
        "ramp": [
            {"level": level, "pages": pages}
            for level, pages in WEB_CATALOG_PRESSURE_RAMP
        ],
        "scope_time_ramp_seconds": list(WEB_CATALOG_SCOPE_TIME_RAMP_SECONDS),
        "selection_mode": selection_mode,
        "selected_level": selected_level,
        "selected_max_pages_per_app_country": selected_pages,
        "selected_max_parallel": selected_parallel,
        "selected_scope_time_budget_seconds": selected_scope_time_budget_seconds,
        "safe_max_pages_per_app_country": safe_pages,
        "safe_max_parallel": safe_parallel,
        "safe_scope_time_budget_seconds": safe_scope_time_budget_seconds,
        "candidate_max_pages_per_app_country": candidate_pages,
        "candidate_max_parallel": candidate_parallel,
        "candidate_scope_time_budget_seconds": candidate_scope_time_budget_seconds,
        "search_phase": pressure_state_text(state, "search_phase", "probe"),
        "cooldown_until": state.get("cooldown_until") if state else None,
        "cooldown_seconds_remaining": cooldown_seconds_remaining,
        "cooldown_active": cooldown_seconds_remaining > 0,
        "reason": reason,
        "clean_for_ramp": clean_for_ramp,
        "state": json_safe_database_row(state),
        "page_count": page_count,
        "ok_page_count": int(metrics["ok_page_count"] or 0),
        "error_page_count": int(metrics["error_page_count"] or 0),
        "http_429_page_count": int(metrics["http_429_page_count"] or 0),
        "http_429_affected_page_count": int(metrics.get("http_429_affected_page_count") or 0),
        "http_429_attempt_count": int(metrics.get("http_429_attempt_count") or 0),
        "final_non_200_page_count": int(metrics["final_non_200_page_count"] or 0),
        "soft_error_page_count": soft_error_page_count,
        "soft_error_rate": soft_error_rate,
        "soft_error_threshold_exceeded": soft_error_threshold_exceeded,
        "retried_page_count": int(metrics["retried_page_count"] or 0),
        "first_page_at": str(metrics["first_page_at"]) if metrics["first_page_at"] is not None else None,
        "last_page_at": str(metrics["last_page_at"]) if metrics["last_page_at"] is not None else None,
    }


def record_web_catalog_pressure_result(
    database_url: str,
    *,
    source: str = WEB_CATALOG_SOURCE,
    since: str,
    used_pages: int,
    used_parallel: int = 1,
    used_scope_time_budget_seconds: int = 1800,
    base_pages: int = 5,
    max_pages: int = 25,
    base_parallel: int = 1,
    max_parallel: int = 4,
    base_scope_time_budget_seconds: int = 1800,
    max_scope_time_budget_seconds: int = 7200,
    cooldown_minutes: int = 30,
) -> dict:
    initialize_postgres(database_url)
    base_pages = max(0, int(base_pages))
    max_pages = max(0, int(max_pages))
    base_parallel = max(1, int(base_parallel))
    max_parallel = max(base_parallel, int(max_parallel))
    base_scope_time_budget_seconds = max(1, int(base_scope_time_budget_seconds))
    max_scope_time_budget_seconds = max(base_scope_time_budget_seconds, int(max_scope_time_budget_seconds))
    cooldown_minutes = max(0, int(cooldown_minutes))
    used_pages = clamp_pressure_pages(int(used_pages), base_pages=base_pages, max_pages=max_pages)
    used_parallel = min(max_parallel, max(base_parallel, int(used_parallel)))
    used_scope_time_budget_seconds = min(
        max_scope_time_budget_seconds,
        max(base_scope_time_budget_seconds, int(used_scope_time_budget_seconds)),
    )

    with connect_postgres(database_url) as connection:
        metrics = web_catalog_recent_pressure_metrics(
            connection,
            source=source,
            lookback_minutes=0,
            since=since,
        )
        previous_state = connection.execute(
            """
            SELECT *
            FROM app_store_pressure_state
            WHERE source = %s
            """,
            (source,),
        ).fetchone()
        clean_for_ramp = pressure_metrics_are_clean(metrics)
        required_pages = used_pages if used_pages > 0 else 1
        proved_selected_pressure = clean_for_ramp and int(metrics["page_count"] or 0) >= required_pages
        previous_safe_pages = clamp_pressure_pages(
            pressure_state_int(previous_state, "safe_max_pages_per_app_country", base_pages),
            base_pages=base_pages,
            max_pages=max_pages,
        )
        previous_safe_parallel = min(
            max_parallel,
            max(base_parallel, pressure_state_int(previous_state, "safe_max_parallel", base_parallel)),
        )
        previous_safe_scope_time_budget_seconds = min(
            max_scope_time_budget_seconds,
            max(
                base_scope_time_budget_seconds,
                pressure_state_int(
                    previous_state,
                    "safe_scope_time_budget_seconds",
                    base_scope_time_budget_seconds,
                ),
            ),
        )
        phase = pressure_state_text(previous_state, "search_phase", "probe")
        confirmed_429_count = pressure_state_int(previous_state, "confirmed_429_count", 0)
        next_action = "stop"
        next_after_seconds = 0
        cooldown_until = None
        page_count = int(metrics["page_count"] or 0)

        if page_count == 0:
            safe_pages = previous_safe_pages
            safe_parallel = previous_safe_parallel
            safe_scope_time_budget_seconds = previous_safe_scope_time_budget_seconds
            candidate_pages = clamp_pressure_pages(
                pressure_state_int(previous_state, "candidate_max_pages_per_app_country", safe_pages),
                base_pages=base_pages,
                max_pages=max_pages,
            )
            candidate_parallel = min(
                max_parallel,
                max(base_parallel, pressure_state_int(previous_state, "candidate_max_parallel", safe_parallel)),
            )
            candidate_scope_time_budget_seconds = min(
                max_scope_time_budget_seconds,
                max(
                    base_scope_time_budget_seconds,
                    pressure_state_int(
                        previous_state,
                        "candidate_scope_time_budget_seconds",
                        safe_scope_time_budget_seconds,
                    ),
                ),
            )
            clean_run_count = pressure_state_int(previous_state, "clean_run_count", 0)
            result = "no_pages_no_change"
            search_phase = phase
        elif proved_selected_pressure:
            safe_pages = used_pages
            safe_parallel = used_parallel
            safe_scope_time_budget_seconds = used_scope_time_budget_seconds
            clean_run_count = pressure_state_int(previous_state, "clean_run_count", 0) + 1
            next_config = next_pressure_config(
                used_pages=used_pages,
                used_parallel=used_parallel,
                used_scope_time_budget_seconds=used_scope_time_budget_seconds,
                base_pages=base_pages,
                max_pages=max_pages,
                base_parallel=base_parallel,
                max_parallel=max_parallel,
                base_scope_time_budget_seconds=base_scope_time_budget_seconds,
                max_scope_time_budget_seconds=max_scope_time_budget_seconds,
            )
            at_max_parallel = safe_parallel >= max_parallel
            at_max_time = safe_scope_time_budget_seconds >= max_scope_time_budget_seconds
            if at_max_parallel and at_max_time:
                result = "stable_at_configured_max"
                search_phase = "stable"
                candidate_pages = safe_pages
                candidate_parallel = safe_parallel
                candidate_scope_time_budget_seconds = safe_scope_time_budget_seconds
            elif phase == "rollback_verify":
                result = "stable_safe_strategy"
                search_phase = "stable"
                candidate_pages = safe_pages
                candidate_parallel = safe_parallel
                candidate_scope_time_budget_seconds = safe_scope_time_budget_seconds
            else:
                result = "clean_increase_pressure"
                search_phase = "probe"
                candidate_pages = int(next_config["max_pages_per_app_country"])
                candidate_parallel = int(next_config["max_parallel"])
                candidate_scope_time_budget_seconds = int(next_config["scope_time_budget_seconds"])
                next_action = "continue_now"
            confirmed_429_count = 0
        elif clean_for_ramp:
            safe_pages = used_pages
            safe_parallel = used_parallel
            safe_scope_time_budget_seconds = used_scope_time_budget_seconds
            clean_run_count = pressure_state_int(previous_state, "clean_run_count", 0)
            result = "clean_but_short"
            search_phase = "stable"
            candidate_pages = safe_pages
            candidate_parallel = safe_parallel
            candidate_scope_time_budget_seconds = safe_scope_time_budget_seconds
        else:
            clean_run_count = 0
            safe_pages = previous_safe_pages
            safe_parallel = previous_safe_parallel
            safe_scope_time_budget_seconds = previous_safe_scope_time_budget_seconds
            if phase in {"retry_after_429", "retry_after_pressure_error"}:
                rollback_config = {
                    "max_pages_per_app_country": previous_safe_pages,
                    "max_parallel": previous_safe_parallel,
                    "scope_time_budget_seconds": previous_safe_scope_time_budget_seconds,
                }
                if (
                    int(rollback_config["max_parallel"]) >= used_parallel
                    and int(rollback_config["scope_time_budget_seconds"]) >= used_scope_time_budget_seconds
                ):
                    rollback_config = previous_pressure_config(
                        used_pages=used_pages,
                        used_parallel=used_parallel,
                        used_scope_time_budget_seconds=used_scope_time_budget_seconds,
                        base_pages=base_pages,
                        max_pages=max_pages,
                        base_parallel=base_parallel,
                        max_parallel=max_parallel,
                        base_scope_time_budget_seconds=base_scope_time_budget_seconds,
                        max_scope_time_budget_seconds=max_scope_time_budget_seconds,
                    )
                safe_pages = int(rollback_config["max_pages_per_app_country"])
                safe_parallel = int(rollback_config["max_parallel"])
                safe_scope_time_budget_seconds = int(rollback_config["scope_time_budget_seconds"])
                candidate_pages = safe_pages
                candidate_parallel = safe_parallel
                candidate_scope_time_budget_seconds = safe_scope_time_budget_seconds
                result = "confirmed_pressure_rollback"
                search_phase = "rollback_verify"
                confirmed_429_count += 1
                next_action = "cooldown_retry_rollback"
                next_after_seconds = cooldown_minutes * 60
                cooldown_until = pressure_cooldown_timestamp(cooldown_minutes)
            elif phase == "rollback_verify":
                rollback_config = previous_pressure_config(
                    used_pages=used_pages,
                    used_parallel=used_parallel,
                    used_scope_time_budget_seconds=used_scope_time_budget_seconds,
                    base_pages=base_pages,
                    max_pages=max_pages,
                    base_parallel=base_parallel,
                    max_parallel=max_parallel,
                    base_scope_time_budget_seconds=base_scope_time_budget_seconds,
                    max_scope_time_budget_seconds=max_scope_time_budget_seconds,
                )
                safe_pages = int(rollback_config["max_pages_per_app_country"])
                safe_parallel = int(rollback_config["max_parallel"])
                safe_scope_time_budget_seconds = int(rollback_config["scope_time_budget_seconds"])
                candidate_pages = safe_pages
                candidate_parallel = safe_parallel
                candidate_scope_time_budget_seconds = safe_scope_time_budget_seconds
                result = "rollback_still_unstable_lower_pressure"
                search_phase = "rollback_verify"
                confirmed_429_count += 1
                next_action = "cooldown_retry_rollback"
                next_after_seconds = cooldown_minutes * 60
                cooldown_until = pressure_cooldown_timestamp(cooldown_minutes)
            else:
                candidate_pages = used_pages
                candidate_parallel = used_parallel
                candidate_scope_time_budget_seconds = used_scope_time_budget_seconds
                result = "first_pressure_error_cooldown_retry"
                search_phase = "retry_after_pressure_error"
                confirmed_429_count = max(confirmed_429_count, 1)
                next_action = "cooldown_retry_same_pressure"
                next_after_seconds = cooldown_minutes * 60
                cooldown_until = pressure_cooldown_timestamp(cooldown_minutes)

        next_pages = candidate_pages
        next_parallel = candidate_parallel
        next_scope_time_budget_seconds = candidate_scope_time_budget_seconds
        if next_action == "stop":
            next_after_seconds = 0
            cooldown_until = None

        completed_at = utc_timestamp()
        connection.execute(
            """
            INSERT INTO app_store_pressure_state (
                source,
                next_max_pages_per_app_country,
                safe_max_pages_per_app_country,
                candidate_max_pages_per_app_country,
                next_max_parallel,
                safe_max_parallel,
                candidate_max_parallel,
                next_scope_time_budget_seconds,
                safe_scope_time_budget_seconds,
                candidate_scope_time_budget_seconds,
                clean_run_count,
                search_phase,
                cooldown_until,
                confirmed_429_count,
                last_result,
                last_started_at,
                last_completed_at,
                last_used_pages,
                last_safe_pages,
                last_used_parallel,
                last_safe_parallel,
                last_used_scope_time_budget_seconds,
                last_safe_scope_time_budget_seconds,
                last_page_count,
                last_ok_page_count,
                last_http_429_page_count,
                last_error_page_count,
                last_final_non_200_page_count,
                last_retried_page_count,
                updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
            )
            ON CONFLICT (source) DO UPDATE SET
                next_max_pages_per_app_country = EXCLUDED.next_max_pages_per_app_country,
                safe_max_pages_per_app_country = EXCLUDED.safe_max_pages_per_app_country,
                candidate_max_pages_per_app_country = EXCLUDED.candidate_max_pages_per_app_country,
                next_max_parallel = EXCLUDED.next_max_parallel,
                safe_max_parallel = EXCLUDED.safe_max_parallel,
                candidate_max_parallel = EXCLUDED.candidate_max_parallel,
                next_scope_time_budget_seconds = EXCLUDED.next_scope_time_budget_seconds,
                safe_scope_time_budget_seconds = EXCLUDED.safe_scope_time_budget_seconds,
                candidate_scope_time_budget_seconds = EXCLUDED.candidate_scope_time_budget_seconds,
                clean_run_count = EXCLUDED.clean_run_count,
                search_phase = EXCLUDED.search_phase,
                cooldown_until = EXCLUDED.cooldown_until,
                confirmed_429_count = EXCLUDED.confirmed_429_count,
                last_result = EXCLUDED.last_result,
                last_started_at = EXCLUDED.last_started_at,
                last_completed_at = EXCLUDED.last_completed_at,
                last_used_pages = EXCLUDED.last_used_pages,
                last_safe_pages = EXCLUDED.last_safe_pages,
                last_used_parallel = EXCLUDED.last_used_parallel,
                last_safe_parallel = EXCLUDED.last_safe_parallel,
                last_used_scope_time_budget_seconds = EXCLUDED.last_used_scope_time_budget_seconds,
                last_safe_scope_time_budget_seconds = EXCLUDED.last_safe_scope_time_budget_seconds,
                last_page_count = EXCLUDED.last_page_count,
                last_ok_page_count = EXCLUDED.last_ok_page_count,
                last_http_429_page_count = EXCLUDED.last_http_429_page_count,
                last_error_page_count = EXCLUDED.last_error_page_count,
                last_final_non_200_page_count = EXCLUDED.last_final_non_200_page_count,
                last_retried_page_count = EXCLUDED.last_retried_page_count,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                source,
                next_pages,
                safe_pages,
                candidate_pages,
                next_parallel,
                safe_parallel,
                candidate_parallel,
                next_scope_time_budget_seconds,
                safe_scope_time_budget_seconds,
                candidate_scope_time_budget_seconds,
                clean_run_count,
                search_phase,
                cooldown_until,
                confirmed_429_count,
                result,
                since,
                completed_at,
                used_pages,
                safe_pages,
                used_parallel,
                safe_parallel,
                used_scope_time_budget_seconds,
                safe_scope_time_budget_seconds,
                int(metrics["page_count"] or 0),
                int(metrics["ok_page_count"] or 0),
                int(metrics.get("http_429_affected_page_count") or metrics["http_429_page_count"] or 0),
                int(metrics["error_page_count"] or 0),
                int(metrics["final_non_200_page_count"] or 0),
                int(metrics["retried_page_count"] or 0),
            ),
        )
        connection.commit()

    return {
        "source": source,
        "since": since,
        "used_pages": used_pages,
        "used_parallel": used_parallel,
        "used_scope_time_budget_seconds": used_scope_time_budget_seconds,
        "next_max_pages_per_app_country": next_pages,
        "safe_max_pages_per_app_country": safe_pages,
        "candidate_max_pages_per_app_country": candidate_pages,
        "next_max_parallel": next_parallel,
        "safe_max_parallel": safe_parallel,
        "candidate_max_parallel": candidate_parallel,
        "next_scope_time_budget_seconds": next_scope_time_budget_seconds,
        "safe_scope_time_budget_seconds": safe_scope_time_budget_seconds,
        "candidate_scope_time_budget_seconds": candidate_scope_time_budget_seconds,
        "result": result,
        "search_phase": search_phase,
        "next_action": next_action,
        "next_after_seconds": next_after_seconds,
        "cooldown_until": cooldown_until,
        "confirmed_429_count": confirmed_429_count,
        "clean_for_ramp": clean_for_ramp,
        "proved_selected_pressure": proved_selected_pressure,
        "clean_run_count": clean_run_count,
        "page_count": int(metrics["page_count"] or 0),
        "ok_page_count": int(metrics["ok_page_count"] or 0),
        "http_429_page_count": int(metrics["http_429_page_count"] or 0),
        "error_page_count": int(metrics["error_page_count"] or 0),
        "final_non_200_page_count": int(metrics["final_non_200_page_count"] or 0),
        "soft_error_page_count": int(metrics.get("soft_error_page_count") or 0),
        "soft_error_rate": (
            int(metrics.get("soft_error_page_count") or 0) / int(metrics["page_count"] or 1)
            if int(metrics["page_count"] or 0)
            else 0.0
        ),
        "soft_error_threshold_exceeded": pressure_soft_error_threshold_exceeded(metrics),
        "retried_page_count": int(metrics["retried_page_count"] or 0),
    }


def scope_key(
    app_id: str,
    country: str,
    sort_by: str = DEFAULT_SORT_BY,
    *,
    source: str = SOURCE,
) -> str:
    return f"{source}:{app_id}:{country.lower()}:{sort_by}"


def infer_field_value(page_rows: list[dict], review_rows: list[dict], field: str, default: str) -> str:
    for row in [*page_rows, *review_rows]:
        value = row.get(field)
        if value:
            return str(value)
    return default


def existing_review_ids_by_scope(
    database_url: str,
    scopes: Iterable[tuple[str, str, str]],
    *,
    source: str = SOURCE,
    initialize_schema: bool = True,
) -> dict[tuple[str, str, str], set[str]]:
    scope_list = [(str(app_id), country.lower(), sort_by) for app_id, country, sort_by in scopes]
    results = {scope: set() for scope in scope_list}
    if not scope_list:
        return results
    if initialize_schema:
        initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        for app_id, country, sort_by in scope_list:
            rows = connection.execute(
                """
                SELECT review_id
                FROM app_store_reviews
                WHERE app_id = %s AND country = %s AND source = %s
                """,
                (app_id, country, source),
            ).fetchall()
            results[(app_id, country, sort_by)] = {str(row["review_id"]) for row in rows}
    return results


def trusted_existing_review_ids_by_scope(
    database_url: str,
    scopes: Iterable[tuple[str, str, str]],
    *,
    source: str = SOURCE,
    initialize_schema: bool = True,
) -> dict[tuple[str, str, str], set[str]]:
    scope_list = [(str(app_id), country.lower(), sort_by) for app_id, country, sort_by in scopes]
    results = {scope: set() for scope in scope_list}
    if not scope_list:
        return results
    if initialize_schema:
        initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        for app_id, country, sort_by in scope_list:
            rows = connection.execute(
                """
                SELECT r.review_id
                FROM app_store_reviews r
                LEFT JOIN app_store_sync_state s
                  ON s.app_id = r.app_id
                 AND s.country = r.country
                 AND s.sort_by = %s
                 AND s.source = %s
                LEFT JOIN app_store_runs first_run
                  ON first_run.run_id = r.first_seen_run_id
                LEFT JOIN app_store_runs success_run
                  ON success_run.run_id = s.last_successful_run_id
                LEFT JOIN LATERAL (
                  SELECT p.run_id, page_run.loaded_at
                  FROM app_store_review_pages p
                  JOIN app_store_runs page_run
                    ON page_run.run_id = p.run_id
                  WHERE p.app_id = %s
                    AND p.country = %s
                    AND p.sort_by = %s
                    AND p.source = %s
                    AND p.terminal_reason IN (
                      'caught_up_to_existing_reviews',
                      'no_next_href',
                      'target_review_count_reached',
                      'target_review_count_zero'
                    )
                  ORDER BY page_run.loaded_at DESC
                  LIMIT 1
                ) inferred_success ON TRUE
                LEFT JOIN LATERAL (
                  SELECT p.run_id, page_run.loaded_at
                  FROM app_store_review_pages p
                  JOIN app_store_runs page_run
                    ON page_run.run_id = p.run_id
                  WHERE p.app_id = %s
                    AND p.country = %s
                    AND p.sort_by = %s
                    AND p.source = %s
                  GROUP BY p.run_id, page_run.loaded_at
                  HAVING COALESCE(BOOL_OR(p.terminal_reason IN (
                    'caught_up_to_existing_reviews',
                    'no_next_href',
                    'target_review_count_reached',
                    'target_review_count_zero'
                  )), FALSE) = FALSE
                  ORDER BY page_run.loaded_at DESC
                  LIMIT 1
                ) inferred_incomplete ON TRUE
                LEFT JOIN app_store_runs trusted_success_run
                  ON trusted_success_run.run_id = COALESCE(success_run.run_id, inferred_success.run_id)
                WHERE r.app_id = %s
                  AND r.country = %s
                  AND r.source = %s
                  AND (
                    (
                      trusted_success_run.loaded_at IS NOT NULL
                      AND first_run.loaded_at <= trusted_success_run.loaded_at
                    )
                    OR (
                      trusted_success_run.loaded_at IS NULL
                      AND inferred_incomplete.loaded_at IS NOT NULL
                      AND first_run.loaded_at < inferred_incomplete.loaded_at
                    )
                  )
                """,
                (
                    sort_by,
                    source,
                    app_id,
                    country,
                    sort_by,
                    source,
                    app_id,
                    country,
                    sort_by,
                    source,
                    app_id,
                    country,
                    source,
                ),
            ).fetchall()
            results[(app_id, country, sort_by)] = {str(row["review_id"]) for row in rows}
    return results


def review_counts_by_scope(
    database_url: str,
    scopes: Iterable[tuple[str, str, str]],
    *,
    source: str = SOURCE,
    initialize_schema: bool = True,
) -> dict[tuple[str, str, str], int]:
    scope_list = [(str(app_id), country.lower(), sort_by) for app_id, country, sort_by in scopes]
    results: dict[tuple[str, str, str], int] = {}
    if not scope_list:
        return results
    if initialize_schema:
        initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        for app_id, country, sort_by in scope_list:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT review_id) AS review_count
                FROM app_store_reviews
                WHERE app_id = %s AND country = %s AND source = %s
                """,
                (app_id, country, source),
            ).fetchone()
            review_count = int(row["review_count"] or 0)
            if review_count > 0:
                results[(app_id, country, sort_by)] = review_count
    return results


def sync_targets_postgres(
    database_url: str,
    targets_path: Path,
    run_id: str,
    *,
    initialize_schema: bool = True,
) -> dict:
    targets = load_targets(targets_path)
    target_ids = {str(target.apple_app_id) for target in targets}
    active_target_count = sum(1 for target in targets if target.active)

    if initialize_schema:
        initialize_postgres(database_url)
    with connect_postgres(database_url) as connection:
        upsert_targets(connection, targets, run_id)
        if target_ids:
            deleted = connection.execute(
                """
                DELETE FROM app_store_targets t
                WHERE NOT (t.app_id = ANY(%s))
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_reviews r WHERE r.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_review_pages p WHERE p.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_review_changes c WHERE c.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_sync_state s WHERE s.app_id = t.app_id
                  )
                """,
                (list(target_ids),),
            ).rowcount
            deactivated = connection.execute(
                """
                UPDATE app_store_targets
                SET active = 0,
                    last_seen_run_id = %s
                WHERE NOT (app_id = ANY(%s))
                """,
                (run_id, list(target_ids)),
            ).rowcount
        else:
            deleted = connection.execute(
                """
                DELETE FROM app_store_targets t
                WHERE NOT EXISTS (
                      SELECT 1 FROM app_store_reviews r WHERE r.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_review_pages p WHERE p.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_review_changes c WHERE c.app_id = t.app_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_store_sync_state s WHERE s.app_id = t.app_id
                  )
                """
            ).rowcount
            deactivated = connection.execute(
                """
                UPDATE app_store_targets
                SET active = 0,
                    last_seen_run_id = %s
                """,
                (run_id,),
            ).rowcount
        db_counts = connection.execute(
            """
            SELECT
                COUNT(*)::int AS target_count,
                COALESCE(SUM(active), 0)::int AS active_target_count
            FROM app_store_targets
            """
        ).fetchone()
        connection.commit()

    return {
        "run_id": run_id,
        "targets_path": str(targets_path),
        "file_target_count": len(targets),
        "file_active_target_count": active_target_count,
        "postgres_target_count": int(db_counts["target_count"] or 0),
        "postgres_active_target_count": int(db_counts["active_target_count"] or 0),
        "deleted_missing_unreferenced_targets": int(deleted or 0),
        "deactivated_missing_referenced_targets": int(deactivated or 0),
    }


def load_pipeline_run_postgres(
    database_url: str,
    raw_dir: Path,
    targets_path: Path,
    *,
    execution_id: str | None = None,
    github_run_id: str | None = None,
    github_run_attempt: int | None = None,
    worker_key: str | None = None,
    selected_target_count: int | None = None,
    initialize_schema: bool = True,
) -> dict:
    run_id = raw_dir.name
    page_rows = read_jsonl(raw_dir / "review_pages.jsonl")
    review_rows = read_jsonl(raw_dir / "reviews.jsonl")
    targets = load_targets(targets_path)
    loaded_at = utc_timestamp()
    platform = infer_field_value(page_rows, review_rows, "platform", PLATFORM)
    source = infer_field_value(page_rows, review_rows, "source", SOURCE)

    if initialize_schema:
        initialize_postgres(database_url)
    for attempt in range(1, POSTGRES_LOAD_MAX_ATTEMPTS + 1):
        try:
            return _load_pipeline_run_postgres_once(
                database_url=database_url,
                run_id=run_id,
                raw_dir=raw_dir,
                targets_path=targets_path,
                targets=targets,
                page_rows=page_rows,
                review_rows=review_rows,
                loaded_at=loaded_at,
                platform=platform,
                source=source,
                execution_id=execution_id,
                github_run_id=github_run_id,
                github_run_attempt=github_run_attempt,
                worker_key=worker_key,
                selected_target_count=selected_target_count,
            )
        except POSTGRES_RETRYABLE_WRITE_ERRORS:
            if attempt >= POSTGRES_LOAD_MAX_ATTEMPTS:
                raise
            time.sleep(POSTGRES_LOAD_RETRY_SECONDS * attempt)
    raise RuntimeError("Postgres load retry loop exited unexpectedly")


def _load_pipeline_run_postgres_once(
    *,
    database_url: str,
    run_id: str,
    raw_dir: Path,
    targets_path: Path,
    targets: list,
    page_rows: list[dict],
    review_rows: list[dict],
    loaded_at: str,
    platform: str,
    source: str,
    execution_id: str | None = None,
    github_run_id: str | None = None,
    github_run_attempt: int | None = None,
    worker_key: str | None = None,
    selected_target_count: int | None = None,
) -> dict:
    target_ids = {str(row.get("app_id")) for row in [*page_rows, *review_rows] if row.get("app_id")}
    targets_to_upsert = [target for target in targets if str(target.apple_app_id) in target_ids]
    actual_target_count = (
        max(0, int(selected_target_count))
        if selected_target_count is not None
        else len(target_ids)
    )
    with connect_postgres(database_url) as connection:
        upsert_run(
            connection,
            run_id,
            raw_dir,
            targets_path,
            loaded_at,
            target_count=actual_target_count,
            platform=platform,
            source=source,
            execution_id=execution_id,
            github_run_id=github_run_id,
            github_run_attempt=github_run_attempt,
            worker_key=worker_key,
        )
        upsert_targets(connection, targets_to_upsert, run_id)
        insert_pages(connection, page_rows)
        review_summary = upsert_reviews(connection, review_rows, run_id)
        fetch_errors = sum(1 for row in page_rows if row.get("status") == "error")
        capped_scopes = sum(1 for row in page_rows if row.get("terminal_reason") == "page_cap")
        connection.execute(
            """
            UPDATE app_store_runs
            SET page_count = %s,
                review_count = %s,
                reviews_inserted = %s,
                reviews_updated = %s,
                duplicates_skipped = %s,
                fetch_errors = %s,
                capped_scopes = %s
            WHERE run_id = %s
            """,
            (
                len(page_rows),
                len(review_rows),
                review_summary["inserted"],
                review_summary["updated"],
                review_summary["duplicates_skipped"],
                fetch_errors,
                capped_scopes,
                run_id,
            ),
        )
        connection.commit()

    return {
        "run_id": run_id,
        "page_rows": len(page_rows),
        "review_rows": len(review_rows),
        **review_summary,
        "fetch_errors": fetch_errors,
        "capped_scopes": capped_scopes,
        "target_count": actual_target_count,
        "execution_id": execution_id,
    }


def upsert_run(
    connection: psycopg.Connection,
    run_id: str,
    raw_dir: Path,
    targets_path: Path,
    loaded_at: str,
    *,
    target_count: int,
    platform: str = PLATFORM,
    source: str = SOURCE,
    execution_id: str | None = None,
    github_run_id: str | None = None,
    github_run_attempt: int | None = None,
    worker_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO app_store_runs (
            run_id, raw_dir, targets_path, loaded_at, loaded_at_ts, platform, source,
            target_count, execution_id, github_run_id, github_run_attempt, worker_key
        )
        VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE
        SET raw_dir = EXCLUDED.raw_dir,
            targets_path = EXCLUDED.targets_path,
            loaded_at = EXCLUDED.loaded_at,
            loaded_at_ts = EXCLUDED.loaded_at_ts,
            target_count = EXCLUDED.target_count,
            execution_id = COALESCE(EXCLUDED.execution_id, app_store_runs.execution_id),
            github_run_id = COALESCE(EXCLUDED.github_run_id, app_store_runs.github_run_id),
            github_run_attempt = COALESCE(EXCLUDED.github_run_attempt, app_store_runs.github_run_attempt),
            worker_key = COALESCE(EXCLUDED.worker_key, app_store_runs.worker_key)
        """,
        (
            run_id,
            str(raw_dir),
            str(targets_path),
            loaded_at,
            loaded_at,
            platform,
            source,
            target_count,
            execution_id,
            github_run_id,
            github_run_attempt,
            worker_key,
        ),
    )


def upsert_targets(connection: psycopg.Connection, targets: Iterable, run_id: str) -> None:
    for target in targets:
        connection.execute(
            """
            INSERT INTO app_store_targets (
                app_id, app_name, category, apple_slug, countries,
                active, notes, first_seen_run_id, last_seen_run_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (app_id) DO UPDATE
            SET app_name = EXCLUDED.app_name,
                category = EXCLUDED.category,
                apple_slug = EXCLUDED.apple_slug,
                countries = EXCLUDED.countries,
                active = EXCLUDED.active,
                notes = EXCLUDED.notes,
                last_seen_run_id = EXCLUDED.last_seen_run_id
            """,
            (
                target.apple_app_id,
                target.app_name,
                target.category,
                target.apple_slug,
                "|".join(target.countries),
                int(target.active),
                target.notes,
                run_id,
                run_id,
            ),
        )


def insert_pages(connection: psycopg.Connection, rows: Iterable[dict]) -> None:
    for row in rows:
        connection.execute(
            """
            INSERT INTO app_store_review_pages (
                page_key, run_id, platform, source, app_id, app_name, country,
                sort_by, page_number, request_url, status, status_code, fetched_at, fetched_at_ts,
                raw_json_path, response_bytes, review_count, unique_review_count,
                duplicate_count, missing_text_count, missing_rating_count,
                missing_updated_count, max_updated_epoch_seconds,
                min_updated_epoch_seconds, has_next_link, attempt_count,
                http_429_attempt_count, soft_retry_count,
                error_message, terminal_reason, overlap_review_count
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (page_key) DO UPDATE
            SET request_url = EXCLUDED.request_url,
                status = EXCLUDED.status,
                status_code = EXCLUDED.status_code,
                fetched_at = EXCLUDED.fetched_at,
                fetched_at_ts = EXCLUDED.fetched_at_ts,
                raw_json_path = EXCLUDED.raw_json_path,
                response_bytes = EXCLUDED.response_bytes,
                review_count = EXCLUDED.review_count,
                unique_review_count = EXCLUDED.unique_review_count,
                duplicate_count = EXCLUDED.duplicate_count,
                missing_text_count = EXCLUDED.missing_text_count,
                missing_rating_count = EXCLUDED.missing_rating_count,
                missing_updated_count = EXCLUDED.missing_updated_count,
                max_updated_epoch_seconds = EXCLUDED.max_updated_epoch_seconds,
                min_updated_epoch_seconds = EXCLUDED.min_updated_epoch_seconds,
                has_next_link = EXCLUDED.has_next_link,
                attempt_count = EXCLUDED.attempt_count,
                http_429_attempt_count = EXCLUDED.http_429_attempt_count,
                soft_retry_count = EXCLUDED.soft_retry_count,
                terminal_reason = EXCLUDED.terminal_reason,
                overlap_review_count = EXCLUDED.overlap_review_count,
                error_message = EXCLUDED.error_message
            """,
            (
                row.get("page_key"),
                row.get("run_id"),
                row.get("platform"),
                row.get("source"),
                row.get("app_id"),
                row.get("app_name"),
                row.get("country"),
                row.get("sort_by"),
                row.get("page_number"),
                row.get("request_url"),
                row.get("status"),
                row.get("status_code"),
                row.get("fetched_at"),
                row.get("fetched_at"),
                row.get("raw_json_path"),
                row.get("response_bytes") or 0,
                row.get("review_count") or 0,
                row.get("unique_review_count") or 0,
                row.get("duplicate_count") or 0,
                row.get("missing_text_count") or 0,
                row.get("missing_rating_count") or 0,
                row.get("missing_updated_count") or 0,
                row.get("max_updated_epoch_seconds"),
                row.get("min_updated_epoch_seconds"),
                int(bool(row.get("has_next_link"))),
                row.get("attempt_count") or 1,
                row.get("http_429_attempt_count") or 0,
                row.get("soft_retry_count") or 0,
                row.get("error_message"),
                row.get("terminal_reason"),
                row.get("overlap_review_count") or 0,
            ),
        )


def upsert_reviews(connection: psycopg.Connection, rows: Iterable[dict], run_id: str) -> dict:
    summary: dict[str, Any] = {"inserted": 0, "updated": 0, "duplicates_skipped": 0}
    by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        scope = (str(row.get("app_id") or ""), str(row.get("country") or "").lower())
        scope_summary = by_scope.setdefault(
            scope,
            {
                "app_id": scope[0],
                "country": scope[1],
                "inserted": 0,
                "updated": 0,
                "duplicates_skipped": 0,
            },
        )
        review_key = row.get("review_key")
        if not review_key:
            summary["duplicates_skipped"] += 1
            scope_summary["duplicates_skipped"] += 1
            continue
        existing = connection.execute(
            """
            SELECT app_name, author_name, updated_at, updated_epoch_seconds, rating, title, content
            FROM app_store_reviews
            WHERE review_key = %s
            """,
            (review_key,),
        ).fetchone()
        if existing is None:
            insert_review(connection, row, run_id)
            insert_review_change(connection, row, run_id, "inserted", None, [])
            summary["inserted"] += 1
            scope_summary["inserted"] += 1
        elif changed_fields := review_changed_fields(existing, row):
            previous_updated = existing.get("updated_epoch_seconds")
            update_review(connection, row, run_id)
            insert_review_change(connection, row, run_id, "updated", previous_updated, changed_fields, existing)
            summary["updated"] += 1
            scope_summary["updated"] += 1
        else:
            connection.execute(
                """
                UPDATE app_store_reviews
                SET last_seen_run_id = %s,
                    source_page_key = %s,
                    collected_at = %s,
                    collected_at_ts = %s::timestamptz,
                    row_updated_at = CURRENT_TIMESTAMP
                WHERE review_key = %s
                """,
                (
                    run_id,
                    row.get("source_page_key"),
                    row.get("collected_at"),
                    row.get("collected_at"),
                    review_key,
                ),
            )
            summary["duplicates_skipped"] += 1
            scope_summary["duplicates_skipped"] += 1
    summary["by_scope"] = list(by_scope.values())
    return summary


def review_changed(existing: dict, row: dict) -> bool:
    return bool(review_changed_fields(existing, row))


def review_changed_fields(existing: dict, row: dict) -> list[str]:
    fields = (
        "app_name",
        "author_name",
        "updated_at",
        "updated_epoch_seconds",
        "rating",
        "title",
        "content",
    )
    return [field for field in fields if existing.get(field) != row.get(field)]


def insert_review(connection: psycopg.Connection, row: dict, run_id: str) -> None:
    connection.execute(
        """
        INSERT INTO app_store_reviews (
            review_key, platform, source, app_id, app_name, country, review_id,
            author_name, updated_at, updated_epoch_seconds, rating, title,
            content, first_seen_run_id, last_seen_run_id, source_page_key,
            collected_at, collected_at_ts
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s::timestamptz
        )
        """,
        review_values(row, run_id),
    )


def update_review(connection: psycopg.Connection, row: dict, run_id: str) -> None:
    connection.execute(
        """
        UPDATE app_store_reviews
        SET app_name = %s,
            author_name = %s,
            updated_at = %s,
            updated_epoch_seconds = %s,
            rating = %s,
            title = %s,
            content = %s,
            last_seen_run_id = %s,
            source_page_key = %s,
            collected_at = %s,
            collected_at_ts = %s::timestamptz,
            row_updated_at = CURRENT_TIMESTAMP
        WHERE review_key = %s
        """,
        (
            row.get("app_name"),
            row.get("author_name"),
            row.get("updated_at"),
            row.get("updated_epoch_seconds"),
            row.get("rating"),
            row.get("title"),
            row.get("content"),
            run_id,
            row.get("source_page_key"),
            row.get("collected_at"),
            row.get("collected_at"),
            row.get("review_key"),
        ),
    )


def review_values(row: dict, run_id: str) -> tuple:
    return (
        row.get("review_key"),
        row.get("platform"),
        row.get("source"),
        row.get("app_id"),
        row.get("app_name"),
        row.get("country"),
        row.get("review_id"),
        row.get("author_name"),
        row.get("updated_at"),
        row.get("updated_epoch_seconds"),
        row.get("rating"),
        row.get("title"),
        row.get("content"),
        run_id,
        run_id,
        row.get("source_page_key"),
        row.get("collected_at"),
        row.get("collected_at"),
    )


def insert_review_change(
    connection: psycopg.Connection,
    row: dict,
    run_id: str,
    change_type: str,
    previous_updated_epoch_seconds: int | None,
    changed_fields: list[str],
    existing: dict | None = None,
) -> None:
    previous_values = {field: existing.get(field) for field in changed_fields} if existing else None
    new_values = {field: row.get(field) for field in changed_fields} if changed_fields else None
    connection.execute(
        """
        INSERT INTO app_store_review_changes (
            run_id, review_key, app_id, country, change_type,
            previous_updated_epoch_seconds, new_updated_epoch_seconds, source_page_key,
            changed_at_ts, changed_fields, previous_values, new_values
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s)
        ON CONFLICT (run_id, review_key) DO NOTHING
        """,
        (
            run_id,
            row.get("review_key"),
            row.get("app_id"),
            row.get("country"),
            change_type,
            previous_updated_epoch_seconds,
            row.get("updated_epoch_seconds"),
            row.get("source_page_key"),
            changed_fields or None,
            Jsonb(previous_values) if previous_values is not None else None,
            Jsonb(new_values) if new_values is not None else None,
        ),
    )


def record_run_scopes_postgres(
    database_url: str,
    *,
    run_id: str,
    execution_id: str | None,
    source: str,
    sort_by: str,
    started_at: str,
    completed_at: str,
    expected_scopes: Iterable[dict[str, str]],
    page_rows: list[dict],
    review_summary: dict[str, Any],
    initialize_schema: bool = True,
) -> dict[str, Any]:
    if initialize_schema:
        initialize_postgres(database_url)
    pages_by_scope: dict[tuple[str, str, str], list[dict]] = {}
    for row in page_rows:
        key = (
            str(row.get("app_id") or ""),
            str(row.get("country") or "").lower(),
            str(row.get("sort_by") or sort_by),
        )
        pages_by_scope.setdefault(key, []).append(row)
    reviews_by_scope = {
        (str(row.get("app_id") or ""), str(row.get("country") or "").lower()): row
        for row in review_summary.get("by_scope", [])
    }
    scopes = []
    with connect_postgres(database_url) as connection:
        for expected in expected_scopes:
            app_id = str(expected["app_id"])
            app_name = str(expected.get("app_name") or "")
            country = str(expected["country"]).lower()
            scope_sort = str(expected.get("sort_by") or sort_by)
            pages = sorted(pages_by_scope.get((app_id, country, scope_sort), []), key=lambda row: int(row.get("page_number") or 0))
            load = reviews_by_scope.get((app_id, country), {})
            terminal_reason = next(
                (str(row["terminal_reason"]) for row in reversed(pages) if row.get("terminal_reason")),
                None,
            )
            http_429_pages = sum(int(row.get("status_code") == 429) for row in pages)
            http_429_attempt_count = sum(
                int(row.get("http_429_attempt_count") or 0) for row in pages
            )
            soft_retry_count = sum(int(row.get("soft_retry_count") or 0) for row in pages)
            other_non_200_pages = sum(
                int(row.get("status_code") is not None and int(row["status_code"]) not in {200, 429})
                for row in pages
            )
            fetch_errors = sum(int(row.get("status") == "error") for row in pages)
            outcome = scope_outcome(
                terminal_reason=terminal_reason,
                page_count=len(pages),
                fetch_errors=fetch_errors,
                other_non_200_pages=other_non_200_pages,
            )
            values = {
                "scope_run_key": f"{run_id}:{source}:{app_id}:{country}:{scope_sort}",
                "execution_id": execution_id,
                "run_id": run_id,
                "app_id": app_id,
                "app_name": app_name,
                "country": country,
                "source": source,
                "sort_by": scope_sort,
                "page_count": len(pages),
                "review_count": sum(
                    int(load.get(key) or 0) for key in ("inserted", "updated", "duplicates_skipped")
                ),
                "reviews_inserted": int(load.get("inserted") or 0),
                "reviews_updated": int(load.get("updated") or 0),
                "duplicates_skipped": int(load.get("duplicates_skipped") or 0),
                "http_429_pages": http_429_pages,
                "http_429_attempt_count": http_429_attempt_count,
                "soft_retry_count": soft_retry_count,
                "other_non_200_pages": other_non_200_pages,
                "retried_pages": sum(int(row.get("attempt_count") or 1) > 1 for row in pages),
                "fetch_errors": fetch_errors,
                "overlap_review_count": sum(int(row.get("overlap_review_count") or 0) for row in pages),
                "terminal_reason": terminal_reason,
                "outcome": outcome,
            }
            connection.execute(
                """
                INSERT INTO app_store_run_scopes (
                    scope_run_key, execution_id, run_id, app_id, app_name, country,
                    source, sort_by, page_count, review_count, reviews_inserted,
                    reviews_updated, duplicates_skipped, http_429_pages,
                    http_429_attempt_count, soft_retry_count,
                    other_non_200_pages, retried_pages, fetch_errors,
                    overlap_review_count, terminal_reason, outcome, started_at, completed_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (scope_run_key) DO UPDATE
                SET execution_id = EXCLUDED.execution_id,
                    page_count = EXCLUDED.page_count,
                    review_count = EXCLUDED.review_count,
                    reviews_inserted = EXCLUDED.reviews_inserted,
                    reviews_updated = EXCLUDED.reviews_updated,
                    duplicates_skipped = EXCLUDED.duplicates_skipped,
                    http_429_pages = EXCLUDED.http_429_pages,
                    http_429_attempt_count = EXCLUDED.http_429_attempt_count,
                    soft_retry_count = EXCLUDED.soft_retry_count,
                    other_non_200_pages = EXCLUDED.other_non_200_pages,
                    retried_pages = EXCLUDED.retried_pages,
                    fetch_errors = EXCLUDED.fetch_errors,
                    overlap_review_count = EXCLUDED.overlap_review_count,
                    terminal_reason = EXCLUDED.terminal_reason,
                    outcome = EXCLUDED.outcome,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    values["scope_run_key"], execution_id, run_id, app_id, app_name,
                    country, source, scope_sort, values["page_count"], values["review_count"],
                    values["reviews_inserted"], values["reviews_updated"],
                    values["duplicates_skipped"], http_429_pages,
                    http_429_attempt_count, soft_retry_count, other_non_200_pages,
                    values["retried_pages"], fetch_errors, values["overlap_review_count"],
                    terminal_reason, outcome, started_at, completed_at,
                ),
            )
            scopes.append(values)
        if execution_id:
            refresh_execution_scope_counts(connection, execution_id)
        connection.commit()
    return {
        "scope_count": len(scopes),
        "caught_up_scope_count": sum(row["outcome"] == "caught_up" for row in scopes),
        "backlogged_scope_count": sum(row["outcome"] == "backlogged" for row in scopes),
        "hard_failure_scope_count": sum(row["outcome"] == "hard_failure" for row in scopes),
        "scopes": scopes,
    }


def scope_outcome(
    *,
    terminal_reason: str | None,
    page_count: int,
    fetch_errors: int,
    other_non_200_pages: int,
) -> str:
    if page_count <= 0 or fetch_errors > 0 or other_non_200_pages > 0:
        return "hard_failure"
    if terminal_reason in HARD_FAILURE_TERMINAL_REASONS:
        return "hard_failure"
    if terminal_reason in SUCCESS_TERMINAL_REASONS:
        return "caught_up"
    return "backlogged"


def update_sync_states_postgres(
    database_url: str,
    page_rows: list[dict],
    review_rows: list[dict],
    *,
    run_id: str,
    sort_by: str,
    started_at: str,
    completed_at: str,
    source: str = SOURCE,
    initialize_schema: bool = True,
) -> dict:
    grouped_pages: dict[tuple[str, str, str], list[dict]] = {}
    grouped_reviews: dict[tuple[str, str, str], list[dict]] = {}
    for row in page_rows:
        key = (row.get("app_id"), row.get("country"), row.get("sort_by") or sort_by)
        grouped_pages.setdefault(key, []).append(row)
    for row in review_rows:
        key = (row.get("app_id"), row.get("country"), sort_by)
        grouped_reviews.setdefault(key, []).append(row)

    if initialize_schema:
        initialize_postgres(database_url)
    for attempt in range(1, POSTGRES_SYNC_STATE_MAX_ATTEMPTS + 1):
        try:
            return _update_sync_states_postgres_once(
                database_url,
                grouped_pages,
                grouped_reviews,
                run_id=run_id,
                sort_by=sort_by,
                started_at=started_at,
                completed_at=completed_at,
                source=source,
            )
        except POSTGRES_RETRYABLE_WRITE_ERRORS:
            if attempt >= POSTGRES_SYNC_STATE_MAX_ATTEMPTS:
                raise
            time.sleep(POSTGRES_SYNC_STATE_RETRY_SECONDS * attempt)
    raise RuntimeError("Postgres sync-state retry loop exited unexpectedly")


def _update_sync_states_postgres_once(
    database_url: str,
    grouped_pages: dict[tuple[str, str, str], list[dict]],
    grouped_reviews: dict[tuple[str, str, str], list[dict]],
    *,
    run_id: str,
    sort_by: str,
    started_at: str,
    completed_at: str,
    source: str = SOURCE,
) -> dict:
    summaries = []
    with connect_postgres(database_url) as connection:
        for key, pages in grouped_pages.items():
            app_id, country, scope_sort = key
            reviews = grouped_reviews.get(key, [])
            terminal_reason = pages[-1].get("terminal_reason") if pages else None
            overlap = sum(int(page.get("overlap_review_count") or 0) for page in pages)
            backlogged = (
                terminal_reason in (BACKLOG_TERMINAL_REASONS | HARD_FAILURE_TERMINAL_REASONS)
                and overlap == 0
            )
            state_row = connection.execute(
                """
                SELECT complete_through_updated_epoch_seconds, last_successful_run_id,
                       last_successful_at, backlog_started_at,
                       consecutive_incomplete_runs, backlogged
                FROM app_store_sync_state
                WHERE scope_key = %s
                """,
                (scope_key(app_id, country, scope_sort, source=source),),
            ).fetchone()
            current_high_water = connection.execute(
                """
                SELECT COALESCE(MAX(updated_epoch_seconds), 0) AS high_water
                FROM app_store_reviews
                WHERE app_id = %s AND country = %s AND source = %s
                """,
                (app_id, country, source),
            ).fetchone()["high_water"]
            if terminal_reason in SUCCESS_TERMINAL_REASONS:
                high_water = int(current_high_water or 0)
                last_successful_run_id = run_id
                last_successful_at = completed_at
                backlog_started_at = None
                consecutive_incomplete_runs = 0
            else:
                high_water = int(
                    state_row["complete_through_updated_epoch_seconds"]
                    if state_row
                    else 0
                )
                last_successful_run_id = state_row["last_successful_run_id"] if state_row else None
                last_successful_at = state_row["last_successful_at"] if state_row else None
                was_backlogged = bool(state_row and state_row["backlogged"])
                backlog_started_at = (
                    state_row["backlog_started_at"]
                    if was_backlogged and state_row.get("backlog_started_at")
                    else completed_at
                ) if backlogged else None
                consecutive_incomplete_runs = (
                    int(state_row.get("consecutive_incomplete_runs") or 0) + 1
                    if backlogged
                    else 0
                )
            connection.execute(
                """
                INSERT INTO app_store_sync_state (
                    scope_key, app_id, country, sort_by, source,
                    complete_through_updated_epoch_seconds, backlogged,
                    last_started_at, last_completed_at, last_started_at_ts, last_completed_at_ts, last_run_id,
                    last_successful_run_id, last_terminal_reason,
                    last_page_count, last_review_count, last_overlap_review_count,
                    last_successful_at, last_attempt_completed_at, backlog_started_at,
                    consecutive_incomplete_runs, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (scope_key) DO UPDATE
                SET source = EXCLUDED.source,
                    complete_through_updated_epoch_seconds = EXCLUDED.complete_through_updated_epoch_seconds,
                    backlogged = EXCLUDED.backlogged,
                    last_started_at = EXCLUDED.last_started_at,
                    last_completed_at = EXCLUDED.last_completed_at,
                    last_started_at_ts = EXCLUDED.last_started_at_ts,
                    last_completed_at_ts = EXCLUDED.last_completed_at_ts,
                    last_run_id = EXCLUDED.last_run_id,
                    last_successful_run_id = EXCLUDED.last_successful_run_id,
                    last_terminal_reason = EXCLUDED.last_terminal_reason,
                    last_page_count = EXCLUDED.last_page_count,
                    last_review_count = EXCLUDED.last_review_count,
                    last_overlap_review_count = EXCLUDED.last_overlap_review_count,
                    last_successful_at = EXCLUDED.last_successful_at,
                    last_attempt_completed_at = EXCLUDED.last_attempt_completed_at,
                    backlog_started_at = EXCLUDED.backlog_started_at,
                    consecutive_incomplete_runs = EXCLUDED.consecutive_incomplete_runs,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    scope_key(app_id, country, scope_sort, source=source),
                    app_id,
                    country,
                    scope_sort,
                    source,
                    high_water,
                    int(backlogged),
                    started_at,
                    completed_at,
                    started_at,
                    completed_at,
                    run_id,
                    last_successful_run_id,
                    terminal_reason,
                    len(pages),
                    len(reviews),
                    overlap,
                    last_successful_at,
                    completed_at,
                    backlog_started_at,
                    consecutive_incomplete_runs,
                ),
            )
            summaries.append(
                {
                    "app_id": app_id,
                    "country": country,
                    "sort_by": scope_sort,
                    "high_water": high_water,
                    "backlogged": backlogged,
                    "terminal_reason": terminal_reason,
                    "pages": len(pages),
                    "reviews": len(reviews),
                    "overlap_review_count": overlap,
                }
            )
        connection.commit()
    return {"scope_count": len(summaries), "scopes": summaries}


def validate_postgres(database_url: str, run_id: str | None = None, *, initialize_schema: bool = True) -> dict:
    if initialize_schema:
        initialize_postgres(database_url)
    where_reviews = "WHERE last_seen_run_id = %s" if run_id else ""
    where_pages = "WHERE run_id = %s" if run_id else ""
    where_runs = "WHERE run_id = %s" if run_id else ""
    where_changes = "WHERE run_id = %s" if run_id else ""
    where_sync = "WHERE last_run_id = %s" if run_id else ""
    params = (run_id,) if run_id else ()
    with connect_postgres(database_url) as connection:
        review_counts = connection.execute(
            f"""
            SELECT
                COUNT(*) AS reviews,
                COUNT(*) FILTER (WHERE content IS NULL OR content = '') AS missing_text,
                COUNT(*) FILTER (WHERE rating IS NULL) AS missing_rating,
                COUNT(DISTINCT app_id) AS apps,
                COUNT(DISTINCT country) AS countries
            FROM app_store_reviews
            {where_reviews}
            """,
            params,
        ).fetchone()
        page_counts = connection.execute(
            f"""
            SELECT
                COUNT(*) AS pages,
                COUNT(*) FILTER (WHERE status = 'error') AS errors,
                COUNT(*) FILTER (WHERE terminal_reason = 'page_cap') AS capped_pages
            FROM app_store_review_pages
            {where_pages}
            """,
            params,
        ).fetchone()
        sync_counts = connection.execute(
            f"""
            SELECT
                COUNT(*) AS scopes,
                COUNT(*) FILTER (WHERE backlogged = 1) AS backlogged_scopes
            FROM app_store_sync_state
            {where_sync}
            """,
            params,
        ).fetchone()
        typed_timestamp_counts = {
            "reviews_missing_collected_at_ts": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM app_store_reviews {where_reviews} "
                    f"{'AND' if where_reviews else 'WHERE'} collected_at IS NOT NULL AND collected_at_ts IS NULL",
                    params,
                ).fetchone()["count"]
            ),
            "pages_missing_fetched_at_ts": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM app_store_review_pages {where_pages} "
                    f"{'AND' if where_pages else 'WHERE'} fetched_at IS NOT NULL AND fetched_at_ts IS NULL",
                    params,
                ).fetchone()["count"]
            ),
            "runs_missing_loaded_at_ts": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM app_store_runs {where_runs} "
                    f"{'AND' if where_runs else 'WHERE'} loaded_at IS NOT NULL AND loaded_at_ts IS NULL",
                    params,
                ).fetchone()["count"]
            ),
            "changes_missing_changed_at_ts": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM app_store_review_changes {where_changes} "
                    f"{'AND' if where_changes else 'WHERE'} changed_at IS NOT NULL AND changed_at_ts IS NULL",
                    params,
                ).fetchone()["count"]
            ),
        }
    data_integrity_healthy = (
        int(review_counts["missing_text"] or 0) == 0
        and int(review_counts["missing_rating"] or 0) == 0
        and all(count == 0 for count in typed_timestamp_counts.values())
    )
    operational_clean = (
        int(page_counts["errors"] or 0) == 0
        and int(page_counts["capped_pages"] or 0) == 0
        and int(sync_counts["backlogged_scopes"] or 0) == 0
    )
    return {
        "run_id": run_id,
        "review_counts": dict(review_counts),
        "page_counts": dict(page_counts),
        "sync_counts": dict(sync_counts),
        "typed_timestamp_counts": typed_timestamp_counts,
        "data_integrity_healthy": data_integrity_healthy,
        "operational_clean": operational_clean,
        "healthy": data_integrity_healthy and (int(page_counts["errors"] or 0) == 0 if run_id else True),
    }
