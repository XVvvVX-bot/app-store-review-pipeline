UPDATE app_store_sync_state
SET backlog_started_at = NULL,
    consecutive_incomplete_runs = 0
WHERE backlogged = 0
  AND (backlog_started_at IS NOT NULL OR consecutive_incomplete_runs <> 0);

CREATE INDEX IF NOT EXISTS idx_app_store_reviews_source_collected_ts
    ON app_store_reviews(source, collected_at_ts DESC);

CREATE INDEX IF NOT EXISTS idx_app_store_changes_changed_ts
    ON app_store_review_changes(changed_at_ts DESC);
