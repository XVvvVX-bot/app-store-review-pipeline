ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS last_successful_at TIMESTAMPTZ;
ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS last_attempt_completed_at TIMESTAMPTZ;
ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS backlog_started_at TIMESTAMPTZ;
ALTER TABLE app_store_sync_state ADD COLUMN IF NOT EXISTS consecutive_incomplete_runs INTEGER NOT NULL DEFAULT 0;

UPDATE app_store_sync_state state
SET source = COALESCE(
        run.source,
        CASE
            WHEN state.sort_by = 'recent' THEN 'apple_app_store_web_catalog_reviews'
            ELSE 'apple_itunes_customerreviews_rss'
        END
    )
FROM app_store_runs run
WHERE state.source IS NULL
  AND run.run_id = COALESCE(state.last_run_id, state.last_successful_run_id);

UPDATE app_store_sync_state
SET source = CASE
        WHEN sort_by = 'recent' THEN 'apple_app_store_web_catalog_reviews'
        ELSE 'apple_itunes_customerreviews_rss'
    END
WHERE source IS NULL;

ALTER TABLE app_store_sync_state ALTER COLUMN source SET NOT NULL;

UPDATE app_store_sync_state state
SET last_successful_at = run.loaded_at_ts
FROM app_store_runs run
WHERE state.last_successful_at IS NULL
  AND run.run_id = state.last_successful_run_id;

UPDATE app_store_sync_state
SET last_attempt_completed_at = last_completed_at::timestamptz
WHERE last_attempt_completed_at IS NULL AND last_completed_at IS NOT NULL;

UPDATE app_store_sync_state
SET backlog_started_at = COALESCE(last_successful_at, last_attempt_completed_at),
    consecutive_incomplete_runs = CASE WHEN backlogged = 1 THEN 1 ELSE 0 END
WHERE backlog_started_at IS NULL OR consecutive_incomplete_runs = 0;

UPDATE app_store_sync_state
SET scope_key = source || ':' || app_id || ':' || lower(country) || ':' || sort_by
WHERE scope_key IS DISTINCT FROM source || ':' || app_id || ':' || lower(country) || ':' || sort_by;

CREATE UNIQUE INDEX IF NOT EXISTS idx_app_store_sync_scope_identity
    ON app_store_sync_state(source, app_id, country, sort_by);
CREATE INDEX IF NOT EXISTS idx_app_store_sync_source_success
    ON app_store_sync_state(source, last_successful_at);

ALTER TABLE app_store_review_pages ADD COLUMN IF NOT EXISTS fetched_at_ts TIMESTAMPTZ;
UPDATE app_store_review_pages
SET fetched_at_ts = fetched_at::timestamptz
WHERE fetched_at_ts IS NULL AND fetched_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_app_store_pages_source_fetched_ts
    ON app_store_review_pages(source, fetched_at_ts DESC);

ALTER TABLE app_store_review_changes ADD COLUMN IF NOT EXISTS changed_at_ts TIMESTAMPTZ;
ALTER TABLE app_store_review_changes ADD COLUMN IF NOT EXISTS changed_fields TEXT[];
ALTER TABLE app_store_review_changes ADD COLUMN IF NOT EXISTS previous_values JSONB;
ALTER TABLE app_store_review_changes ADD COLUMN IF NOT EXISTS new_values JSONB;

UPDATE app_store_review_changes
SET changed_fields = ARRAY['legacy_unknown_diff']
WHERE change_type = 'updated' AND changed_fields IS NULL;
