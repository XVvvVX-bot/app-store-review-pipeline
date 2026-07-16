ALTER TABLE app_store_review_pages
    ADD COLUMN IF NOT EXISTS http_429_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE app_store_review_pages
    ADD COLUMN IF NOT EXISTS soft_retry_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE app_store_run_scopes
    ADD COLUMN IF NOT EXISTS http_429_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE app_store_run_scopes
    ADD COLUMN IF NOT EXISTS soft_retry_count INTEGER NOT NULL DEFAULT 0;

UPDATE app_store_review_pages
SET http_429_attempt_count = GREATEST(attempt_count, 1)
WHERE status_code = 429
  AND http_429_attempt_count = 0;

UPDATE app_store_run_scopes scopes
SET http_429_attempt_count = pages.http_429_attempt_count
FROM (
    SELECT run_id, app_id, country, sort_by,
        SUM(http_429_attempt_count)::integer AS http_429_attempt_count
    FROM app_store_review_pages
    GROUP BY run_id, app_id, country, sort_by
) pages
WHERE scopes.run_id = pages.run_id
  AND scopes.app_id = pages.app_id
  AND scopes.country = pages.country
  AND scopes.sort_by = pages.sort_by
  AND scopes.http_429_attempt_count = 0;
