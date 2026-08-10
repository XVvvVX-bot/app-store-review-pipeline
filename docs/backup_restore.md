# Postgres Backup And Restore Runbook

## Purpose

The backup/restore drill proves that the production App Store review database can be exported and reconstructed without modifying the source database. A successful archive alone is not sufficient evidence: the restored database must match the exact source snapshot at the schema, row-count, full-content, migration, sequence, and operational-state levels.

The latest generated evidence is stored in:

- `docs/backup_restore_latest.md`
- `docs/backup_restore_latest.json`

## Design

1. Open a read-only `REPEATABLE READ` transaction on the source database.
2. Export its MVCC snapshot with `pg_export_snapshot()`.
3. Build the source manifest and run `pg_dump --snapshot=...` while that transaction remains open.
4. Store a custom-format, compressed archive locally with mode `0600` and calculate its SHA-256 digest.
5. Create a new database whose guarded name starts with `<source>_restore_`.
6. Restore with `pg_restore --exit-on-error --no-owner --no-privileges`.
7. Build an independent manifest from the restored database and reconcile it with the source manifest.
8. Run the existing Postgres data-integrity validation against the restored database without initializing or altering its schema.
9. Drop only the temporary database created by the current drill. Retain the valid archive for recovery use.

The exported snapshot matters because ordinary source queries before or after `pg_dump` can observe different committed data when ingestion is active. Sharing one snapshot makes the manifest an exact baseline for the archive.

## Validation Contract

The command fails unless all of these match:

- complete `app_store_*` table set;
- exact row count for every application table;
- two independent full-table, order-independent 64-bit content aggregates for every row in every table;
- column definitions and defaults;
- primary keys, unique constraints, checks, and foreign keys, including validation state;
- index definitions;
- migration versions, checksums, and application records;
- sequence definitions and current values;
- installed extensions;
- active-target, sync-state, backlog, app-coverage, execution, and latest-ingestion snapshot;
- existing typed-timestamp and review-field integrity checks.

Relation sizes are reported but are not required to match. A fresh restore is normally packed differently from a long-running production database.

## Local Drill

```bash
.venv/bin/python app_store_pipeline.py backup-restore-drill \
  --database-url postgresql:///app_store_reviews \
  --backup-directory "$HOME/.local/share/app-store-review-pipeline/backups" \
  --restore-jobs 4 \
  --markdown-output docs/backup_restore_latest.md \
  --json-output docs/backup_restore_latest.json
```

The source role must be able to read all application tables and create a temporary database on the same Postgres server. `pg_dump` and `pg_restore` must be at least as new as the server major version.

To retain the temporary restore for investigation, add `--keep-restore-database`. This is intentionally off by default. The command refuses to overwrite an existing database and refuses restore names outside the guarded source-specific prefix.

## GitHub Workflow

`App Store Postgres Backup Restore Drill` is manual only and requires the confirmation text `VALIDATE_BACKUP_RESTORE`. It shares the `app-store-postgres-writer` concurrency group with daily ingestion, so a drill and ingestion cannot write or consume restore resources at the same time.

The workflow uploads only Markdown and JSON evidence. It never uploads the database archive to GitHub. The archive stays under `~/.local/share/app-store-review-pipeline/backups` on the self-hosted Mac.

## Failure Handling

- A partial archive is deleted.
- A completed archive is retained for investigation even if restore reconciliation fails.
- The temporary database is dropped in the `finally` path unless explicitly retained.
- A failed cleanup changes the drill status to `failing` and records the temporary database name.
- The production source URL is masked in reports, and connection credentials are passed to Postgres tools through environment variables rather than command arguments.

## Recovery Procedure

For a real recovery, do not restore directly over the production database.

1. Select an archive with a previously recorded healthy restore drill and verify its SHA-256 digest.
2. Restore into a new isolated database.
3. Run the same reconciliation and application integrity checks.
4. Point a disposable application process at the restored database for a read-only smoke test.
5. Promote only through an explicit database cutover plan with a rollback target.

## Current Limitation

This validates recoverability but does not yet provide host-independent disaster recovery. A backup retained on the same Mac can be lost with that host. The next infrastructure step is encrypted off-host replication with a documented retention policy, recovery-point objective, and recovery-time objective.
