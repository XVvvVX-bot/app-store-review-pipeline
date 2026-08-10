# Postgres Backup And Restore Validation

**Status: HEALTHY**

## Drill Summary

- Generated at: `2026-08-10T00:30:14Z`
- Source database: `postgresql:///app_store_reviews`
- Restore database: `app_store_reviews_restore_20260810003014_63d1249b`
- Backup archive: `~/.local/share/app-store-review-pipeline/backups/app_store_reviews-20260810T003014Z.dump`
- Backup size: `332,085,576` bytes
- Backup SHA-256: `a3e51a69453874e2f5a5baf1179c9efc0ca2931b33215e8952ebaa714766e043`
- Archive TOC entries: `84`
- Restore cleanup: `dropped`
- Total runtime: `207.992` seconds

The source manifest and `pg_dump` used the same exported read-only MVCC snapshot. The restore was created in a separate temporary database; the source database was never written to.

## Data Reconciliation

| Table | Source rows | Restored rows | Full-content fingerprint |
|---|---:|---:|---|
| `app_store_executions` | 55 | 55 | match |
| `app_store_monitor_snapshots` | 53 | 53 | match |
| `app_store_pressure_state` | 1 | 1 | match |
| `app_store_review_changes` | 2,501,252 | 2,501,252 | match |
| `app_store_review_pages` | 143,580 | 143,580 | match |
| `app_store_reviews` | 2,500,801 | 2,500,801 | match |
| `app_store_run_scopes` | 8,804 | 8,804 | match |
| `app_store_runs` | 19,262 | 19,262 | match |
| `app_store_schema_migrations` | 6 | 6 | match |
| `app_store_sync_state` | 400 | 400 | match |
| `app_store_targets` | 200 | 200 | match |

## Structural Validation

- Checks passed: `41/41`
- Column definitions: `match`
- Constraints and foreign keys: `match`
- Index definitions: `match`
- Migration ledger: `match`
- Sequence state: `match`
- Existing data-integrity validation: `passed`

## Safety And Retention

- The archive is local-only, is never uploaded by the GitHub workflow, and is written with mode `0600`.
- The temporary restore database is dropped by default, including on failure paths.
- A retained local archive is useful for restore drills but is not an off-host disaster-recovery copy.
- Production use still needs encrypted off-host replication and an explicit retention policy.
