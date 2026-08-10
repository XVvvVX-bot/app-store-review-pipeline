from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app_store_review_pipeline.backup_restore import (
    build_pg_dump_command,
    build_pg_restore_command,
    compare_database_manifests,
    generated_restore_database_name,
    postgres_environment,
    render_backup_restore_markdown,
    validate_restore_database_name,
)
from app_store_review_pipeline.cli import build_parser


def manifest(*, row_count: int = 2, hash_sum: str = "10", hash_xor: str = "4") -> dict:
    return {
        "database": {"name": "fixture", "server_version": "16.14", "database_bytes": 100},
        "tables": {
            "app_store_reviews": {
                "row_count": row_count,
                "content_hash_sum": hash_sum,
                "content_hash_xor": hash_xor,
                "total_bytes": 100,
            }
        },
        "schema": {
            "columns_sha256": "columns",
            "constraints_sha256": "constraints",
            "indexes_sha256": "indexes",
        },
        "migrations": [{"version": "0001.sql", "checksum": "abc", "applied_at": "now"}],
        "sequences": [{"sequence_name": "changes_id_seq", "last_value": 2}],
        "extensions": [{"name": "plpgsql", "version": "1.0"}],
        "operational": {
            "active_targets": 1,
            "sync_scopes": 1,
            "backlogged_scopes": 0,
            "review_apps": 1,
            "running_executions": 0,
            "latest_run_loaded_at": "now",
            "latest_scope_success_at": "now",
        },
    }


def test_generated_restore_database_name_is_guarded_and_bounded():
    restore_name = generated_restore_database_name(
        "app-store-reviews",
        now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert restore_name.startswith("app_store_reviews_restore_20260809120000_")
    assert len(restore_name) <= 63
    validate_restore_database_name("app-store-reviews", restore_name)


@pytest.mark.parametrize(
    "restore_name",
    [
        "app_store_reviews",
        "unrelated_restore_20260809",
        "app_store_reviews_restore_bad-name",
    ],
)
def test_restore_database_name_rejects_unsafe_targets(restore_name):
    with pytest.raises(ValueError):
        validate_restore_database_name("app_store_reviews", restore_name)


def test_postgres_commands_do_not_include_database_credentials(tmp_path):
    backup_path = tmp_path / "backup.dump"

    dump = build_pg_dump_command(backup_path, snapshot_id="00000003-0000001B-1")
    restore = build_pg_restore_command(
        backup_path,
        database_name="source_restore_20260809_abcdef12",
        jobs=3,
    )

    assert "postgresql://" not in " ".join(dump + restore)
    assert "--snapshot=00000003-0000001B-1" in dump
    assert "--jobs=3" in restore
    assert "--dbname=source_restore_20260809_abcdef12" in restore


def test_postgres_environment_passes_connection_secrets_outside_arguments(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)

    environment = postgres_environment(
        "postgresql://fixture:secret@localhost:5433/source?sslmode=require",
        override_database="source_restore_20260809_abcdef12",
    )

    assert environment["PGUSER"] == "fixture"
    assert environment["PGPASSWORD"] == "secret"
    assert environment["PGDATABASE"] == "source_restore_20260809_abcdef12"
    assert environment["PGSSLMODE"] == "require"


def test_compare_database_manifests_accepts_exact_restore():
    source = manifest()
    restored = manifest()

    result = compare_database_manifests(source, restored)

    assert result["healthy"] is True
    assert result["failed_check_count"] == 0
    assert result["passed_check_count"] == result["check_count"]


def test_compare_database_manifests_reports_row_and_content_mismatch():
    source = manifest()
    restored = manifest(row_count=1, hash_sum="8")

    result = compare_database_manifests(source, restored)

    assert result["healthy"] is False
    assert {check["name"] for check in result["failed_checks"]} == {
        "app_store_reviews.row_count",
        "app_store_reviews.content_hash_sum",
    }


def test_render_backup_restore_markdown_contains_operational_evidence():
    source = manifest()
    restored = manifest()
    report = {
        "status": "healthy",
        "generated_at": "2026-08-09T12:00:00Z",
        "source_database": "postgresql:///app_store_reviews",
        "backup": {
            "path": "~/.local/share/app-store-review-pipeline/backups/fixture.dump",
            "size_bytes": 123,
            "sha256": "abc",
            "toc_entry_count": 10,
        },
        "restore": {"database_name": "app_store_reviews_restore_fixture", "cleanup_status": "dropped"},
        "source_manifest": source,
        "restored_manifest": restored,
        "comparison": compare_database_manifests(source, restored),
        "restored_validation": {"healthy": True},
        "durations_seconds": {"total": 4.2},
    }

    markdown = render_backup_restore_markdown(report)

    assert "**Status: HEALTHY**" in markdown
    assert "app_store_reviews" in markdown
    assert "2 | 2 | match" in markdown
    assert "Restore cleanup: `dropped`" in markdown


def test_backup_restore_cli_defaults_are_safe():
    args = build_parser().parse_args(["backup-restore-drill"])

    assert args.restore_database_name == ""
    assert args.restore_jobs == 4
    assert args.compression == "zstd:3"
    assert args.keep_restore_database is False
    assert isinstance(args.backup_directory, Path)
