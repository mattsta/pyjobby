-- Migration 006: Add Retry Strategy Support
--
-- Purpose: Enable configurable retry strategies (exponential, linear, fibonacci)
--          instead of fixed retry intervals.
--
-- Implementation: Uses existing admin_data JSONB column, no schema changes needed.
--                 This migration just adds documentation and helper functions.

BEGIN;

-- Update admin_data column comment to document retry configuration
COMMENT ON COLUMN jorb.admin_data IS
    'Admin metadata (JSONB). Supported fields:
    - retry_strategy: "exponential" (default), "linear", "fibonacci", "fixed"
    - max_retries: Maximum retry attempts (default: 10)
    - initial_retry_delay: Starting delay in seconds (default: 1)
    - max_retry_delay: Maximum delay cap in seconds (default: 3600)
    - timeout_seconds: Job execution timeout in seconds (default: null)
    - on_timeout: Action on timeout - "retry" or "fail" (default: "retry")
    - save_result: Store job result in database (default: false)';

-- Create helper function to calculate retry delay
-- This is a pure SQL implementation that can be used in queries
CREATE OR REPLACE FUNCTION calculate_retry_delay(
    error_count INT,
    strategy TEXT DEFAULT 'exponential',
    initial_delay INT DEFAULT 1,
    max_delay INT DEFAULT 3600,
    multiplier FLOAT DEFAULT 2.0
) RETURNS INT AS $$
DECLARE
    delay INT;
    jitter FLOAT;
BEGIN
    -- Calculate base delay based on strategy
    CASE strategy
        WHEN 'fixed' THEN
            -- Quadratic with base jitter (original behavior)
            delay := 2 * (error_count * error_count);

        WHEN 'exponential' THEN
            -- Exponential backoff: initial * (multiplier ^ attempts)
            delay := LEAST(initial_delay * (multiplier ^ (error_count - 1)), max_delay);

        WHEN 'linear' THEN
            -- Linear backoff: initial * attempts
            delay := initial_delay * error_count;

        WHEN 'fibonacci' THEN
            -- Fibonacci sequence
            DECLARE
                a INT := 0;
                b INT := 1;
                temp INT;
                i INT;
            BEGIN
                FOR i IN 1..error_count LOOP
                    temp := a + b;
                    a := b;
                    b := temp;
                END LOOP;
                delay := initial_delay * b;
            END;

        ELSE
            -- Default to exponential
            delay := LEAST(initial_delay * (multiplier ^ (error_count - 1)), max_delay);
    END CASE;

    -- Add jitter (0-10% of delay, max 5 seconds)
    -- Prevents thundering herd when many jobs fail simultaneously
    jitter := random() * LEAST(delay * 0.1, 5);
    delay := delay + jitter::INT;

    -- Cap at max_delay
    RETURN LEAST(delay, max_delay);
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION calculate_retry_delay IS
    'Calculate retry delay in seconds based on strategy.
    Strategies: exponential (default), linear, fibonacci, fixed.
    Includes automatic jitter to prevent thundering herd.';

COMMIT;
