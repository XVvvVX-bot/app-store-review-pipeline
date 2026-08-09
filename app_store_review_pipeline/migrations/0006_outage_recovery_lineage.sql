ALTER TABLE app_store_executions
    ADD COLUMN IF NOT EXISTS termination_reason TEXT;

ALTER TABLE app_store_executions
    ADD COLUMN IF NOT EXISTS recovery_of_execution_id TEXT
        REFERENCES app_store_executions(execution_id);

ALTER TABLE app_store_executions
    ADD COLUMN IF NOT EXISTS incident_key TEXT;

CREATE INDEX IF NOT EXISTS idx_app_store_executions_incident
    ON app_store_executions(incident_key, started_at DESC)
    WHERE incident_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_app_store_executions_running_started
    ON app_store_executions(started_at)
    WHERE status = 'running';
