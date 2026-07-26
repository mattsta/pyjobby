-- Migration: Add Recurring Scheduler Tables
-- Version: 2.0.0
-- Date: 2025-11-18

-- ============================================================================
-- jorb_schedule: Recurring job schedules
-- ============================================================================

CREATE TABLE IF NOT EXISTS jorb_schedule (
    -- Identity
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,

    -- Job definition
    job_class TEXT NOT NULL,
    kwargs JSONB NOT NULL DEFAULT '{}'::jsonb,
    queue TEXT NOT NULL DEFAULT 'default',
    prio INTEGER NOT NULL DEFAULT 100,
    capability TEXT,

    -- Schedule configuration
    cron_expr TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled BOOLEAN NOT NULL DEFAULT true,

    -- Safety features
    max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
    jitter_seconds INTEGER NOT NULL DEFAULT 0,
    backpressure_threshold INTEGER DEFAULT 1000,
    circuit_breaker_threshold INTEGER NOT NULL DEFAULT 5,

    -- Execution tracking
    next_run TIMESTAMPTZ NOT NULL,
    last_run TIMESTAMPTZ,
    last_success TIMESTAMPTZ,
    last_failure TIMESTAMPTZ,

    -- Statistics
    run_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    skip_count BIGINT NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    -- Metadata
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT,

    -- Constraints
    CONSTRAINT valid_cron_expr CHECK (cron_expr ~ '^\S+ \S+ \S+ \S+ \S+$'),
    CONSTRAINT positive_max_concurrent CHECK (max_concurrent_jobs > 0),
    CONSTRAINT positive_jitter CHECK (jitter_seconds >= 0),
    CONSTRAINT positive_circuit_breaker CHECK (circuit_breaker_threshold > 0)
);

-- Indexes for efficient scheduler polling
CREATE INDEX IF NOT EXISTS jorb_schedule_next_run_idx
    ON jorb_schedule(next_run)
    WHERE enabled = true;

CREATE INDEX IF NOT EXISTS jorb_schedule_enabled_idx
    ON jorb_schedule(enabled);

CREATE INDEX IF NOT EXISTS jorb_schedule_name_idx
    ON jorb_schedule(name);

-- ============================================================================
-- jorb_schedule_log: Execution history for debugging and metrics
-- ============================================================================

CREATE TABLE IF NOT EXISTS jorb_schedule_log (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES jorb_schedule(id) ON DELETE CASCADE,
    schedule_name TEXT NOT NULL,

    -- Execution details
    scheduled_time TIMESTAMPTZ NOT NULL,
    actual_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result TEXT NOT NULL,  -- 'success', 'failure', 'skipped'
    skip_reason TEXT,      -- 'max_concurrent', 'backpressure', 'circuit_breaker', 'duplicate'

    -- Job created
    job_id BIGINT REFERENCES jorb(id) ON DELETE SET NULL,

    -- Error details
    error_message TEXT,

    -- Metrics
    duration_ms INTEGER,
    queue_depth_at_run INTEGER,
    concurrent_jobs_at_run INTEGER,
    jitter_applied_seconds INTEGER,

    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT valid_result CHECK (result IN ('success', 'failure', 'skipped'))
);

-- Indexes for log queries
CREATE INDEX IF NOT EXISTS jorb_schedule_log_schedule_idx
    ON jorb_schedule_log(schedule_id, created DESC);

CREATE INDEX IF NOT EXISTS jorb_schedule_log_result_idx
    ON jorb_schedule_log(result, created DESC);

CREATE INDEX IF NOT EXISTS jorb_schedule_log_created_idx
    ON jorb_schedule_log(created DESC);

-- ============================================================================
-- Comments for documentation
-- ============================================================================

COMMENT ON TABLE jorb_schedule IS 'Recurring job schedules with cron expressions';
COMMENT ON TABLE jorb_schedule_log IS 'Execution history for scheduled jobs';

COMMENT ON COLUMN jorb_schedule.name IS 'Unique human-readable schedule name';
COMMENT ON COLUMN jorb_schedule.cron_expr IS 'Standard cron expression (minute hour day month weekday)';
COMMENT ON COLUMN jorb_schedule.max_concurrent_jobs IS 'Maximum jobs from this schedule running simultaneously';
COMMENT ON COLUMN jorb_schedule.jitter_seconds IS 'Random delay (0 to N seconds) before creating job';
COMMENT ON COLUMN jorb_schedule.backpressure_threshold IS 'Skip execution if queue depth exceeds this';
COMMENT ON COLUMN jorb_schedule.circuit_breaker_threshold IS 'Disable schedule after N consecutive failures';
COMMENT ON COLUMN jorb_schedule.consecutive_failures IS 'Current failure streak (resets on success)';

COMMENT ON COLUMN jorb_schedule_log.result IS 'Execution outcome: success, failure, or skipped';
COMMENT ON COLUMN jorb_schedule_log.skip_reason IS 'Why execution was skipped (if result=skipped)';
COMMENT ON COLUMN jorb_schedule_log.duration_ms IS 'Time taken to create job (milliseconds)';
COMMENT ON COLUMN jorb_schedule_log.queue_depth_at_run IS 'Number of jobs in queue when executed';
COMMENT ON COLUMN jorb_schedule_log.concurrent_jobs_at_run IS 'Number of jobs from this schedule running';

-- ============================================================================
-- Success!
-- ============================================================================

-- Verify tables were created
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'jorb_schedule') THEN
        RAISE NOTICE 'Successfully created jorb_schedule table';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'jorb_schedule_log') THEN
        RAISE NOTICE 'Successfully created jorb_schedule_log table';
    END IF;
END $$;
