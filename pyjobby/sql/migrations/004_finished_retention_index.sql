-- ============================================================================
-- 004 -- jorb_finished_retention_idx: a tight index for checkpoint retention
-- ============================================================================
-- WHAT THIS IS. Checkpoint retention (`--checkpoint-retention-days`, default
-- 1 day) reaps the bulkiest rows -- jorb_step checkpoints -- sooner than the
-- full-job window. It applies to `finished` jobs ONLY: `crashed`/`cancelled`
-- jobs are retryable and `retry_job` resumes from their checkpoints, so
-- reaping those early would make a DLQ retry re-execute every completed
-- step's side effects. The all-terminal `jorb_retention_idx` would hand the
-- finished-only sweep the crashed/cancelled rows too, which it then walks
-- past and discards -- a cost proportional to the terminal backlog on a
-- crash-heavy install. This partial index, on the same retention expression
-- but restricted to `state = 'finished'`, keeps that sweep bounded by its
-- own batch regardless of how many crashed jobs are parked.
--
-- WHAT IT IS NOT is a file a fresh install ever executes: schema.sql already
-- contains the index, and migrate() RECORDS this version without running it.
-- tests/test_migrations.py holds the two paths together by requiring an
-- upgraded catalog to equal a fresh install's exactly.
--
-- IDEMPOTENCY: IF NOT EXISTS makes re-running a no-op.
--
-- OPERATIONAL NOTE. The build takes a write lock on jorb for its duration
-- (CREATE INDEX CONCURRENTLY cannot run inside a migration's transaction).
-- On a large jorb this is a maintenance-window operation.

CREATE INDEX IF NOT EXISTS jorb_finished_retention_idx
    ON jorb (COALESCE(finished, updated))
    WHERE state = 'finished';
