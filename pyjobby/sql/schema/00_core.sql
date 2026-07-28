-- ============================================================================
-- pyjobby unified schema v1
-- ============================================================================
-- Forward-only canonical schema. Conventions:
--   * every timestamp is TIMESTAMPTZ (UTC instants; asyncpg treats naive
--     datetimes bound to timestamptz parameters as UTC)
--   * every json payload is JSONB, NOT NULL, defaulting to '{}'
--   * priorities: LOWER numbers are MORE urgent; workers claim prio <= ceiling
--   * job identity is stable across retries: a retry requeues the SAME row
--     and advances run_epoch (checkpoints and history reference one job id
--     forever; run_count, not run_epoch, counts attempts)
--   * 'crashed' is TERMINAL: the job exhausted its retries (the DLQ is
--     `WHERE state = 'crashed'`, not a heuristic)
-- ============================================================================

CREATE TYPE jorbstate AS ENUM (
    'queued',     -- claimable (includes retry-waiting and durable sleep)
    'claimed',    -- taken by a worker, not yet executing
    'running',    -- executing on a worker
    'waiting',    -- blocked on waitfor_job / waitfor_group
    'finished',   -- terminal success
    'crashed',    -- terminal failure after retries exhausted (the DLQ)
    'cancelled'   -- terminal, cancelled by operator or client
);

