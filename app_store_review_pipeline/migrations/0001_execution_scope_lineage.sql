CREATE TABLE IF NOT EXISTS app_store_executions (
    execution_id TEXT PRIMARY KEY,
    github_run_id TEXT,
    github_run_attempt INTEGER NOT NULL DEFAULT 1,
    workflow_name TEXT,
    event_name TEXT,
    git_sha TEXT,
    source TEXT NOT NULL,
    scope_signature TEXT,
    config_signature TEXT,
    intended_target_count INTEGER NOT NULL DEFAULT 0,
    intended_scope_count INTEGER NOT NULL DEFAULT 0,
    completed_scope_count INTEGER NOT NULL DEFAULT 0,
    caught_up_scope_count INTEGER NOT NULL DEFAULT 0,
    backlogged_scope_count INTEGER NOT NULL DEFAULT 0,
    hard_failure_scope_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'healthy', 'degraded', 'failing', 'cancelled')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (github_run_id, github_run_attempt, source)
);

ALTER TABLE app_store_runs ADD COLUMN IF NOT EXISTS execution_id TEXT REFERENCES app_store_executions(execution_id);
ALTER TABLE app_store_runs ADD COLUMN IF NOT EXISTS github_run_id TEXT;
ALTER TABLE app_store_runs ADD COLUMN IF NOT EXISTS github_run_attempt INTEGER;
ALTER TABLE app_store_runs ADD COLUMN IF NOT EXISTS worker_key TEXT;
ALTER TABLE app_store_runs ADD COLUMN IF NOT EXISTS loaded_at_ts TIMESTAMPTZ;

UPDATE app_store_runs
SET loaded_at_ts = loaded_at::timestamptz
WHERE loaded_at_ts IS NULL AND loaded_at IS NOT NULL;

WITH actual_targets AS (
    SELECT run_id, COUNT(DISTINCT app_id)::integer AS target_count
    FROM (
        SELECT run_id, app_id FROM app_store_review_pages
        UNION ALL
        SELECT last_seen_run_id AS run_id, app_id FROM app_store_reviews
    ) observed
    GROUP BY run_id
)
UPDATE app_store_runs run
SET target_count = actual.target_count
FROM actual_targets actual
WHERE run.run_id = actual.run_id
  AND run.target_count IS DISTINCT FROM actual.target_count;

CREATE TABLE IF NOT EXISTS app_store_run_scopes (
    scope_run_key TEXT PRIMARY KEY,
    execution_id TEXT REFERENCES app_store_executions(execution_id),
    run_id TEXT NOT NULL REFERENCES app_store_runs(run_id),
    app_id TEXT NOT NULL REFERENCES app_store_targets(app_id),
    app_name TEXT,
    country TEXT NOT NULL,
    source TEXT NOT NULL,
    sort_by TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    reviews_inserted INTEGER NOT NULL DEFAULT 0,
    reviews_updated INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    http_429_pages INTEGER NOT NULL DEFAULT 0,
    other_non_200_pages INTEGER NOT NULL DEFAULT 0,
    retried_pages INTEGER NOT NULL DEFAULT 0,
    fetch_errors INTEGER NOT NULL DEFAULT 0,
    overlap_review_count INTEGER NOT NULL DEFAULT 0,
    terminal_reason TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('caught_up', 'backlogged', 'hard_failure')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, app_id, country, source, sort_by)
);

CREATE TABLE IF NOT EXISTS app_store_monitor_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    execution_id TEXT REFERENCES app_store_executions(execution_id),
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'failing')),
    review_row_count BIGINT NOT NULL,
    page_row_count BIGINT NOT NULL,
    run_row_count BIGINT NOT NULL,
    change_row_count BIGINT NOT NULL,
    database_bytes BIGINT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_app_store_executions_github
    ON app_store_executions(github_run_id, github_run_attempt);
CREATE INDEX IF NOT EXISTS idx_app_store_executions_source_started
    ON app_store_executions(source, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_store_runs_execution
    ON app_store_runs(execution_id);
CREATE INDEX IF NOT EXISTS idx_app_store_runs_source_loaded_ts
    ON app_store_runs(source, loaded_at_ts DESC);
CREATE INDEX IF NOT EXISTS idx_app_store_run_scopes_execution
    ON app_store_run_scopes(execution_id, outcome);
CREATE INDEX IF NOT EXISTS idx_app_store_run_scopes_app_completed
    ON app_store_run_scopes(source, app_id, country, sort_by, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_store_monitor_snapshots_captured
    ON app_store_monitor_snapshots(captured_at DESC);
