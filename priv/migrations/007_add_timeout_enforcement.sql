-- Migration 007: Add Job Timeout Enforcement
--
-- Purpose: Enable maximum execution time limits for jobs.
--          Long-running jobs that exceed their timeout are automatically
--          terminated and retried or marked as failed.
--
-- Features:
-- - timeout_at column to track when job should timeout
-- - Sparse index for efficient timeout monitoring
-- - Background timeout monitor process

BEGIN;

-- Add timeout tracking column
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS timeout_at TIMESTAMPTZ DEFAULT NULL;

-- Add sparse index for finding timed-out jobs
-- Only indexes running jobs with a timeout set
CREATE INDEX IF NOT EXISTS jorb_timeout_idx
    ON jorb (timeout_at)
    WHERE state = 'running' AND timeout_at IS NOT NULL;

-- Add helpful comment
COMMENT ON COLUMN jorb.timeout_at IS
    'When this job should timeout (NULL = no timeout). '
    'Set automatically based on admin_data.timeout_seconds. '
    'Monitored by timeout_monitor process.';

-- Create view for monitoring timeout violations
CREATE OR REPLACE VIEW jorb_timeout_violations AS
SELECT
    id,
    job_class,
    state,
    started,
    timeout_at,
    NOW() - timeout_at AS overdue_by,
    admin_data->>'on_timeout' AS timeout_action,
    error_count
FROM jorb
WHERE state = 'running'
  AND timeout_at IS NOT NULL
  AND timeout_at < NOW()
ORDER BY timeout_at;

COMMENT ON VIEW jorb_timeout_violations IS
    'Jobs currently violating their timeout. '
    'The timeout monitor process uses this to identify jobs to terminate.';

-- Create function to check for timed-out jobs
CREATE OR REPLACE FUNCTION check_timed_out_jobs()
RETURNS TABLE(
    job_id BIGINT,
    job_class TEXT,
    overdue_seconds INT,
    action TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        id,
        jorb.job_class,
        EXTRACT(EPOCH FROM (NOW() - timeout_at))::INT,
        COALESCE(admin_data->>'on_timeout', 'retry')
    FROM jorb
    WHERE state = 'running'
      AND timeout_at IS NOT NULL
      AND timeout_at < NOW()
    ORDER BY timeout_at
    LIMIT 100;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION check_timed_out_jobs IS
    'Find jobs that have exceeded their timeout deadline. '
    'Returns up to 100 timed-out jobs ordered by timeout_at.';

COMMIT;
