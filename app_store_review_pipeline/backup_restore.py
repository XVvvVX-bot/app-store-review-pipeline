from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app_store_review_pipeline.files import write_json
from app_store_review_pipeline.postgres_database import mask_database_url, validate_postgres


DEFAULT_BACKUP_DIRECTORY = Path.home() / ".local/share/app-store-review-pipeline/backups"
DEFAULT_BACKUP_RESTORE_MARKDOWN = Path("docs/backup_restore_latest.md")
DEFAULT_BACKUP_RESTORE_JSON = Path("docs/backup_restore_latest.json")
APPLICATION_TABLE_PREFIX = "app_store_"
RESTORE_DATABASE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

PG_ENVIRONMENT_KEYS = {
    "dbname": "PGDATABASE",
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "passfile": "PGPASSFILE",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "sslmode": "PGSSLMODE",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslrootcert": "PGSSLROOTCERT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "application_name": "PGAPPNAME",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def database_name(database_url: str) -> str:
    name = str(conninfo_to_dict(database_url).get("dbname") or "").strip()
    if not name:
        raise ValueError("The Postgres URL must identify a source database.")
    return name


def generated_restore_database_name(source_name: str, *, now: datetime | None = None) -> str:
    timestamp = (now or utc_now()).strftime("%Y%m%d%H%M%S")
    safe_source = re.sub(r"[^A-Za-z0-9_]", "_", source_name).strip("_") or "postgres"
    suffix = f"_restore_{timestamp}_{uuid.uuid4().hex[:8]}"
    return f"{safe_source[: 63 - len(suffix)]}{suffix}"


def validate_restore_database_name(source_name: str, restore_name: str) -> None:
    if not RESTORE_DATABASE_PATTERN.fullmatch(restore_name):
        raise ValueError("Restore database names may contain only letters, digits, and underscores.")
    expected_prefix = re.sub(r"[^A-Za-z0-9_]", "_", source_name).strip("_") or "postgres"
    expected_prefix = f"{expected_prefix}_restore_"
    if not restore_name.startswith(expected_prefix):
        raise ValueError(f"Restore database name must start with {expected_prefix!r}.")
    if restore_name == source_name:
        raise ValueError("The restore database must be different from the source database.")


def postgres_environment(database_url: str, *, override_database: str | None = None) -> dict[str, str]:
    params = conninfo_to_dict(database_url)
    if override_database:
        params["dbname"] = override_database
    environment = dict(os.environ)
    for key, environment_key in PG_ENVIRONMENT_KEYS.items():
        value = params.get(key)
        if value is not None:
            environment[environment_key] = str(value)
    return environment


def build_pg_dump_command(
    backup_path: Path,
    *,
    snapshot_id: str,
    compression: str = "zstd:3",
) -> list[str]:
    return [
        "pg_dump",
        "--format=custom",
        f"--compress={compression}",
        "--no-owner",
        "--no-privileges",
        "--lock-wait-timeout=60s",
        f"--snapshot={snapshot_id}",
        f"--file={backup_path}",
    ]


def build_pg_restore_command(
    backup_path: Path,
    *,
    database_name: str,
    jobs: int = 4,
) -> list[str]:
    return [
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        f"--jobs={max(1, int(jobs))}",
        f"--dbname={database_name}",
        str(backup_path),
    ]


def _run_command(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner = command_runner or subprocess.run
    return runner(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _require_postgres_tools() -> None:
    missing = [name for name in ("pg_dump", "pg_restore") if shutil.which(name) is None]
    if missing:
        raise FileNotFoundError(f"Missing required Postgres tools: {', '.join(missing)}")


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (str(value) if isinstance(value, (Decimal, datetime)) else value)
        for key, value in dict(row).items()
    }


def collect_database_manifest(connection: psycopg.Connection) -> dict[str, Any]:
    database = _plain_row(
        connection.execute(
            """
            SELECT
                current_database() AS name,
                current_setting('server_version') AS server_version,
                pg_database_size(current_database())::bigint AS database_bytes
            """
        ).fetchone()
    )
    table_rows = connection.execute(
        """
        SELECT c.relname AS table_name, pg_total_relation_size(c.oid)::bigint AS total_bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname LIKE %s
        ORDER BY c.relname
        """,
        (f"{APPLICATION_TABLE_PREFIX}%",),
    ).fetchall()
    tables: dict[str, dict[str, Any]] = {}
    for table_row in table_rows:
        table_name = str(table_row["table_name"])
        fingerprint = connection.execute(
            sql.SQL(
                """
                SELECT
                    COUNT(*)::bigint AS row_count,
                    COALESCE(SUM(hashtextextended(row_to_json(t)::text, 0)::numeric), 0)::text AS hash_sum,
                    COALESCE(bit_xor(hashtextextended(row_to_json(t)::text, 0)), 0)::text AS hash_xor
                FROM {}.{} AS t
                """
            ).format(sql.Identifier("public"), sql.Identifier(table_name))
        ).fetchone()
        tables[table_name] = {
            "row_count": int(fingerprint["row_count"] or 0),
            "content_hash_sum": str(fingerprint["hash_sum"]),
            "content_hash_xor": str(fingerprint["hash_xor"]),
            "total_bytes": int(table_row["total_bytes"] or 0),
        }

    columns = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT
                table_name,
                column_name,
                ROW_NUMBER() OVER (
                    PARTITION BY table_name ORDER BY ordinal_position
                )::integer AS logical_position,
                data_type,
                udt_name,
                is_nullable,
                column_default,
                is_identity,
                identity_generation
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name LIKE %s
            ORDER BY table_name, ordinal_position
            """,
            (f"{APPLICATION_TABLE_PREFIX}%",),
        ).fetchall()
    ]
    constraints = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT
                c.conrelid::regclass::text AS table_name,
                c.conname AS constraint_name,
                c.contype AS constraint_type,
                c.convalidated AS validated,
                pg_get_constraintdef(c.oid, true) AS definition
            FROM pg_constraint c
            JOIN pg_class rel ON rel.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            WHERE n.nspname = 'public' AND rel.relname LIKE %s
            ORDER BY rel.relname, c.conname
            """,
            (f"{APPLICATION_TABLE_PREFIX}%",),
        ).fetchall()
    ]
    indexes = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT tablename AS table_name, indexname AS index_name, indexdef AS definition
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename LIKE %s
            ORDER BY tablename, indexname
            """,
            (f"{APPLICATION_TABLE_PREFIX}%",),
        ).fetchall()
    ]
    migrations = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT version, checksum, applied_at::text AS applied_at
            FROM app_store_schema_migrations
            ORDER BY version
            """
        ).fetchall()
    ]
    sequences = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT
                sequencename AS sequence_name, data_type, start_value, min_value,
                max_value, increment_by, cycle, cache_size, last_value
            FROM pg_sequences
            WHERE schemaname = 'public'
            ORDER BY sequencename
            """
        ).fetchall()
    ]
    extensions = [
        _plain_row(row)
        for row in connection.execute(
            """
            SELECT extname AS name, extversion AS version
            FROM pg_extension
            ORDER BY extname
            """
        ).fetchall()
    ]
    operational = _plain_row(
        connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM app_store_targets WHERE active = 1)::bigint AS active_targets,
                (SELECT COUNT(*) FROM app_store_sync_state)::bigint AS sync_scopes,
                (SELECT COUNT(*) FROM app_store_sync_state WHERE backlogged = 1)::bigint AS backlogged_scopes,
                (SELECT COUNT(DISTINCT app_id) FROM app_store_reviews)::bigint AS review_apps,
                (SELECT COUNT(*) FROM app_store_executions WHERE status = 'running')::bigint AS running_executions,
                (SELECT MAX(loaded_at_ts)::text FROM app_store_runs) AS latest_run_loaded_at,
                (SELECT MAX(last_successful_at)::text FROM app_store_sync_state) AS latest_scope_success_at
            """
        ).fetchone()
    )
    for key in ("active_targets", "sync_scopes", "backlogged_scopes", "review_apps", "running_executions"):
        operational[key] = int(operational[key] or 0)

    schema = {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "columns_sha256": _stable_digest(columns),
        "constraints_sha256": _stable_digest(constraints),
        "indexes_sha256": _stable_digest(indexes),
    }
    return {
        "database": database,
        "tables": tables,
        "schema": schema,
        "migrations": migrations,
        "migrations_sha256": _stable_digest(migrations),
        "sequences": sequences,
        "sequences_sha256": _stable_digest(sequences),
        "extensions": extensions,
        "extensions_sha256": _stable_digest(extensions),
        "operational": operational,
    }


def compare_database_manifests(source: dict[str, Any], restored: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(name: str, expected: Any, actual: Any) -> None:
        checks.append(
            {
                "name": name,
                "passed": expected == actual,
                "expected": expected,
                "actual": actual,
            }
        )

    source_tables = source.get("tables") or {}
    restored_tables = restored.get("tables") or {}
    add_check("application_table_set", sorted(source_tables), sorted(restored_tables))
    for table_name in sorted(set(source_tables) | set(restored_tables)):
        source_table = source_tables.get(table_name) or {}
        restored_table = restored_tables.get(table_name) or {}
        add_check(
            f"{table_name}.row_count",
            source_table.get("row_count"),
            restored_table.get("row_count"),
        )
        add_check(
            f"{table_name}.content_hash_sum",
            source_table.get("content_hash_sum"),
            restored_table.get("content_hash_sum"),
        )
        add_check(
            f"{table_name}.content_hash_xor",
            source_table.get("content_hash_xor"),
            restored_table.get("content_hash_xor"),
        )
    for key in ("columns_sha256", "constraints_sha256", "indexes_sha256"):
        add_check(f"schema.{key}", source.get("schema", {}).get(key), restored.get("schema", {}).get(key))
    add_check("migration_ledger", source.get("migrations"), restored.get("migrations"))
    add_check("sequence_state", source.get("sequences"), restored.get("sequences"))
    add_check("extensions", source.get("extensions"), restored.get("extensions"))
    add_check("operational_snapshot", source.get("operational"), restored.get("operational"))
    failed = [check for check in checks if not check["passed"]]
    return {
        "healthy": not failed,
        "check_count": len(checks),
        "passed_check_count": len(checks) - len(failed),
        "failed_check_count": len(failed),
        "checks": checks,
        "failed_checks": failed,
    }


def _maintenance_database_url(database_url: str) -> str:
    return make_conninfo(database_url, dbname="postgres")


def _restore_database_url(database_url: str, restore_name: str) -> str:
    return make_conninfo(database_url, dbname=restore_name)


def create_restore_database(database_url: str, restore_name: str) -> None:
    with psycopg.connect(
        _maintenance_database_url(database_url),
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (restore_name,),
        ).fetchone()
        if exists:
            raise ValueError(f"Restore database already exists: {restore_name}")
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(restore_name))
        )


def drop_restore_database(database_url: str, restore_name: str) -> None:
    with psycopg.connect(
        _maintenance_database_url(database_url),
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(restore_name))
        )


def _display_path(path: Path) -> str:
    try:
        return f"~/{path.resolve().relative_to(Path.home().resolve())}"
    except ValueError:
        return str(path)


def render_backup_restore_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status") or "failing").upper()
    backup = report.get("backup") or {}
    restore = report.get("restore") or {}
    comparison = report.get("comparison") or {}
    source_manifest = report.get("source_manifest") or {}
    restored_manifest = report.get("restored_manifest") or {}
    lines = [
        "# Postgres Backup And Restore Validation",
        "",
        f"**Status: {status}**",
        "",
        "## Drill Summary",
        "",
        f"- Generated at: `{report.get('generated_at', '')}`",
        f"- Source database: `{report.get('source_database', '')}`",
        f"- Restore database: `{restore.get('database_name', '')}`",
        f"- Backup archive: `{backup.get('path', '')}`",
        f"- Backup size: `{backup.get('size_bytes', 0):,}` bytes",
        f"- Backup SHA-256: `{backup.get('sha256', '')}`",
        f"- Archive TOC entries: `{backup.get('toc_entry_count', 0)}`",
        f"- Restore cleanup: `{restore.get('cleanup_status', '')}`",
        f"- Total runtime: `{report.get('durations_seconds', {}).get('total', 0)}` seconds",
        "",
        "The source manifest and `pg_dump` used the same exported read-only MVCC snapshot. The restore was created in a separate temporary database; the source database was never written to.",
        "",
        "## Data Reconciliation",
        "",
        "| Table | Source rows | Restored rows | Full-content fingerprint |",
        "|---|---:|---:|---|",
    ]
    source_tables = source_manifest.get("tables") or {}
    restored_tables = restored_manifest.get("tables") or {}
    for table_name in sorted(source_tables):
        source_table = source_tables[table_name]
        restored_table = restored_tables.get(table_name) or {}
        fingerprint_match = (
            source_table.get("content_hash_sum") == restored_table.get("content_hash_sum")
            and source_table.get("content_hash_xor") == restored_table.get("content_hash_xor")
        )
        lines.append(
            f"| `{table_name}` | {int(source_table.get('row_count') or 0):,} | "
            f"{int(restored_table.get('row_count') or 0):,} | {'match' if fingerprint_match else 'MISMATCH'} |"
        )
    lines.extend(
        [
            "",
            "## Structural Validation",
            "",
            f"- Checks passed: `{comparison.get('passed_check_count', 0)}/{comparison.get('check_count', 0)}`",
            f"- Column definitions: `{'match' if source_manifest.get('schema', {}).get('columns_sha256') == restored_manifest.get('schema', {}).get('columns_sha256') else 'MISMATCH'}`",
            f"- Constraints and foreign keys: `{'match' if source_manifest.get('schema', {}).get('constraints_sha256') == restored_manifest.get('schema', {}).get('constraints_sha256') else 'MISMATCH'}`",
            f"- Index definitions: `{'match' if source_manifest.get('schema', {}).get('indexes_sha256') == restored_manifest.get('schema', {}).get('indexes_sha256') else 'MISMATCH'}`",
            f"- Migration ledger: `{'match' if source_manifest.get('migrations') == restored_manifest.get('migrations') else 'MISMATCH'}`",
            f"- Sequence state: `{'match' if source_manifest.get('sequences') == restored_manifest.get('sequences') else 'MISMATCH'}`",
            f"- Existing data-integrity validation: `{'passed' if report.get('restored_validation', {}).get('healthy') else 'FAILED'}`",
            "",
            "## Safety And Retention",
            "",
            "- The archive is local-only, is never uploaded by the GitHub workflow, and is written with mode `0600`.",
            "- The temporary restore database is dropped by default, including on failure paths.",
            "- A retained local archive is useful for restore drills but is not an off-host disaster-recovery copy.",
            "- Production use still needs encrypted off-host replication and an explicit retention policy.",
        ]
    )
    failed_checks = comparison.get("failed_checks") or []
    if failed_checks:
        lines.extend(["", "## Failed Checks", ""])
        for check in failed_checks:
            lines.append(f"- `{check.get('name')}`")
    if report.get("error"):
        lines.extend(["", "## Error", "", f"`{report['error']}`"])
    return "\n".join(lines) + "\n"


def write_backup_restore_report(
    report: dict[str, Any],
    *,
    markdown_output: Path,
    json_output: Path,
) -> None:
    write_json(json_output, report)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(render_backup_restore_markdown(report), encoding="utf-8")


def run_backup_restore_drill(
    database_url: str,
    *,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
    restore_database_name: str = "",
    keep_restore_database: bool = False,
    restore_jobs: int = 4,
    compression: str = "zstd:3",
    markdown_output: Path = DEFAULT_BACKUP_RESTORE_MARKDOWN,
    json_output: Path = DEFAULT_BACKUP_RESTORE_JSON,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_postgres_tools()
    started_clock = time.monotonic()
    started_at = now or utc_now()
    source_name = database_name(database_url)
    restore_name = restore_database_name or generated_restore_database_name(source_name, now=started_at)
    validate_restore_database_name(source_name, restore_name)
    backup_directory = backup_directory.expanduser()
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_directory.chmod(0o700)
    safe_archive_source = re.sub(r"[^A-Za-z0-9_.-]", "_", source_name)
    archive_name = f"{safe_archive_source}-{started_at.strftime('%Y%m%dT%H%M%SZ')}.dump"
    backup_path = backup_directory / archive_name
    partial_path = backup_path.with_suffix(f"{backup_path.suffix}.partial")
    if backup_path.exists() or partial_path.exists():
        raise ValueError(f"Backup archive already exists: {backup_path}")

    report: dict[str, Any] = {
        "generated_at": isoformat_utc(started_at),
        "status": "failing",
        "source_database": mask_database_url(database_url),
        "source_database_name": source_name,
        "source_snapshot_id": "redacted",
        "backup": {
            "path": _display_path(backup_path),
            "format": "custom",
            "compression": compression,
            "retained": False,
        },
        "restore": {
            "database_name": restore_name,
            "kept": bool(keep_restore_database),
            "cleanup_status": "not_created",
        },
        "safety": {
            "source_read_only_snapshot": True,
            "source_mutated": False,
            "archive_uploaded": False,
            "archive_mode": "0600",
        },
        "durations_seconds": {},
    }
    created_restore_database = False
    caught_error: Exception | None = None
    try:
        partial_path.touch(exist_ok=False)
        partial_path.chmod(0o600)
        tool_started = time.monotonic()
        report["tools"] = {
            "pg_dump": _run_command(["pg_dump", "--version"], command_runner=command_runner).stdout.strip(),
            "pg_restore": _run_command(["pg_restore", "--version"], command_runner=command_runner).stdout.strip(),
        }
        report["durations_seconds"]["tool_check"] = round(time.monotonic() - tool_started, 3)

        source_manifest_started = time.monotonic()
        with psycopg.connect(database_url, row_factory=dict_row) as source_connection:
            source_connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_row = source_connection.execute("SELECT pg_export_snapshot() AS snapshot_id").fetchone()
            snapshot_id = str(snapshot_row["snapshot_id"])
            source_manifest = collect_database_manifest(source_connection)
            report["source_manifest"] = source_manifest
            report["durations_seconds"]["source_manifest"] = round(
                time.monotonic() - source_manifest_started,
                3,
            )

            dump_started = time.monotonic()
            _run_command(
                build_pg_dump_command(
                    partial_path,
                    snapshot_id=snapshot_id,
                    compression=compression,
                ),
                environment=postgres_environment(database_url),
                command_runner=command_runner,
            )
            report["durations_seconds"]["pg_dump"] = round(time.monotonic() - dump_started, 3)

        partial_path.replace(backup_path)
        backup_path.chmod(0o600)
        report["backup"].update(
            {
                "size_bytes": backup_path.stat().st_size,
                "sha256": _sha256_file(backup_path),
                "retained": True,
            }
        )
        toc_result = _run_command(
            ["pg_restore", "--list", str(backup_path)],
            command_runner=command_runner,
        )
        toc_lines = [line for line in toc_result.stdout.splitlines() if line and not line.startswith(";")]
        report["backup"].update(
            {
                "toc_entry_count": len(toc_lines),
                "toc_sha256": _stable_digest(toc_lines),
            }
        )
        if not toc_lines:
            raise RuntimeError("The backup archive has no restore TOC entries.")

        create_started = time.monotonic()
        create_restore_database(database_url, restore_name)
        created_restore_database = True
        report["restore"]["cleanup_status"] = "pending"
        report["durations_seconds"]["create_database"] = round(time.monotonic() - create_started, 3)

        restore_started = time.monotonic()
        _run_command(
            build_pg_restore_command(
                backup_path,
                database_name=restore_name,
                jobs=restore_jobs,
            ),
            environment=postgres_environment(database_url, override_database=restore_name),
            command_runner=command_runner,
        )
        report["durations_seconds"]["pg_restore"] = round(time.monotonic() - restore_started, 3)

        restored_manifest_started = time.monotonic()
        restored_url = _restore_database_url(database_url, restore_name)
        with psycopg.connect(restored_url, row_factory=dict_row) as restored_connection:
            restored_connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            restored_manifest = collect_database_manifest(restored_connection)
        report["restored_manifest"] = restored_manifest
        report["durations_seconds"]["restored_manifest"] = round(
            time.monotonic() - restored_manifest_started,
            3,
        )
        report["comparison"] = compare_database_manifests(source_manifest, restored_manifest)
        report["restored_validation"] = validate_postgres(restored_url, initialize_schema=False)
        report["status"] = (
            "healthy"
            if report["comparison"]["healthy"] and report["restored_validation"]["healthy"]
            else "failing"
        )
    except Exception as exc:  # The report is the operational evidence for all failure paths.
        caught_error = exc
        report["status"] = "failing"
        report["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, subprocess.CalledProcessError):
            report["command_failure"] = {
                "returncode": exc.returncode,
                "stderr": str(exc.stderr or "")[-4000:],
            }
    finally:
        if partial_path.exists():
            partial_path.unlink()
        if created_restore_database and not keep_restore_database:
            try:
                cleanup_started = time.monotonic()
                drop_restore_database(database_url, restore_name)
                report["restore"]["cleanup_status"] = "dropped"
                report["durations_seconds"]["cleanup"] = round(time.monotonic() - cleanup_started, 3)
            except Exception as cleanup_error:  # pragma: no cover - requires a live server failure.
                report["restore"]["cleanup_status"] = "failed"
                report["restore"]["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                report["status"] = "failing"
        elif created_restore_database:
            report["restore"]["cleanup_status"] = "retained"
        report["durations_seconds"]["total"] = round(time.monotonic() - started_clock, 3)
        write_backup_restore_report(
            report,
            markdown_output=markdown_output,
            json_output=json_output,
        )
    if caught_error:
        return report
    return report
