CREATE UNIQUE INDEX IF NOT EXISTS idx_app_store_monitor_snapshots_execution_unique
    ON app_store_monitor_snapshots(execution_id)
    WHERE execution_id IS NOT NULL;

ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS last_started_at_ts TIMESTAMPTZ;
ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS last_completed_at_ts TIMESTAMPTZ;

UPDATE app_store_sync_state
SET last_started_at_ts = last_started_at::timestamptz
WHERE last_started_at_ts IS NULL AND last_started_at IS NOT NULL;

UPDATE app_store_sync_state
SET last_completed_at_ts = last_completed_at::timestamptz
WHERE last_completed_at_ts IS NULL AND last_completed_at IS NOT NULL;

ALTER TABLE app_store_reviews ADD COLUMN IF NOT EXISTS collected_at_ts TIMESTAMPTZ;

ALTER TABLE app_store_review_changes
    ALTER COLUMN changed_at_ts SET DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_app_store_changes_run_type
    ON app_store_review_changes(run_id, change_type);
