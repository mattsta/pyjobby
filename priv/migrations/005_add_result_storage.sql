-- Migration 005: Add Result Storage for Job Pipelines
--
-- Purpose: Enable jobs to store their execution results in the database
--          for downstream consumption in pipelines.
--
-- Features:
-- - Result column for storing job outputs (JSONB, up to 10MB)
-- - Sparse index for efficient result queries
-- - Size constraint to prevent database bloat

BEGIN;

-- Add result column to jorb table
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS result JSONB DEFAULT NULL;

-- Add sparse index for jobs that have results
-- This only indexes rows where result IS NOT NULL, minimizing overhead
CREATE INDEX IF NOT EXISTS jorb_result_exists_idx
    ON jorb (id)
    WHERE result IS NOT NULL;

-- Add result size limit check (prevent abuse)
-- 10MB limit is generous but prevents runaway storage
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'result_size_check'
    ) THEN
        ALTER TABLE jorb ADD CONSTRAINT result_size_check
            CHECK (pg_column_size(result) < 10485760);
    END IF;
END $$;

-- Add helpful comment
COMMENT ON COLUMN jorb.result IS
    'Job execution result (optional). Max 10MB. Use for pipeline data passing. '
    'Set save_result=True in job options to enable.';

COMMIT;
