--
-- Migration: Add 'cancelled' state to jorbstate enum
-- Date: 2025-11-18
-- Purpose: Allow jobs to be cancelled before they are claimed
--

-- Add 'cancelled' to the jorbstate enum
ALTER TYPE jorbstate ADD VALUE IF NOT EXISTS 'cancelled';

-- Verify the new state was added
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_enum e
        JOIN pg_type t ON e.enumtypid = t.oid
        WHERE t.typname = 'jorbstate'
          AND e.enumlabel = 'cancelled'
    ) THEN
        RAISE NOTICE 'Successfully added cancelled state to jorbstate enum';
    ELSE
        RAISE EXCEPTION 'Failed to add cancelled state to jorbstate enum';
    END IF;
END $$;
