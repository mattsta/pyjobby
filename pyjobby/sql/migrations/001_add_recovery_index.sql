--
-- Migration: Add recovery index for abandoned job recovery
-- Date: 2025-11-18
-- Purpose: Optimize the recover-abandoned query which filters on worker_host, state, and updated
--

-- Add index for efficient recovery queries
CREATE INDEX IF NOT EXISTS jorb_recovery_idx
ON jorb (worker_host, state, updated)
WHERE state IN ('claimed', 'running');

-- Verify index was created
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'jorb'
          AND indexname = 'jorb_recovery_idx'
    ) THEN
        RAISE NOTICE 'Successfully created jorb_recovery_idx';
    ELSE
        RAISE EXCEPTION 'Failed to create jorb_recovery_idx';
    END IF;
END $$;
