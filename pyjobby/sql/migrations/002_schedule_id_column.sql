-- ============================================================================
-- 002 -- jorb.schedule_id: give the scheduler's concurrency check an index
-- ============================================================================
-- WHAT THIS IS. The recurring scheduler's max_concurrent_jobs check asks "how
-- many of MY jobs are still in flight?" once per firing of every schedule. It
-- asked it as `admin_data->>'schedule_id' = $1`, and no index on jorb could
-- serve that expression -- so every firing sequentially scanned the whole job
-- table, at a cost that grew with the table rather than with the schedule's
-- own load. This file moves that fact out of the jsonb blob into a column and
-- indexes it partially, so the check reads only the schedule's live jobs.
--
-- WHAT IT IS NOT is a file a fresh install ever executes: schema.sql already
-- contains the column and the index, and migrate() RECORDS this version
-- without running it. tests/test_migrations.py holds the two paths together by
-- requiring an upgraded catalog to equal a fresh install's exactly.
--
-- IDEMPOTENCY IS A REQUIREMENT, NOT A COURTESY. Every statement below is
-- conditional, and a test asserts that running this file against a fresh
-- install changes nothing at all -- that is what makes "run migrate on every
-- deploy" safe.
--
-- OPERATIONAL NOTE. ADD COLUMN of a nullable column is catalog-only: it does
-- not rewrite jorb and does not block on its size. The backfill and the index
-- build do take write locks on jorb for their duration (CREATE INDEX
-- CONCURRENTLY cannot run inside a migration's transaction), and the backfill
-- touches only rows that carry the old jsonb key -- jobs some schedule
-- created, i.e. cron-rate rather than job-rate. On a large jorb this is still
-- a maintenance-window operation: see docs/deployment-guide.md.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. The column. Nullable, so existing rows get the same value a fresh row
--    gets (NULL: "no schedule made this job") without a table rewrite.
-- ----------------------------------------------------------------------------
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS schedule_id BIGINT;

COMMENT ON COLUMN jorb.schedule_id IS 'The jorb_schedule row that fired this job, NULL for every job enqueued directly. This is the SOLE source of that fact -- it used to live in admin_data->>''schedule_id'', which no index could serve, so the scheduler''s max_concurrent_jobs check scanned the whole job table on every firing. Deliberately NOT a foreign key: jobs outlive the schedules that made them (deleting a schedule must not delete or rewrite its history), and a REFERENCES here would need an index over EVERY schedule-created job to serve the cascade, undoing the point of the partial index below.';

-- ----------------------------------------------------------------------------
-- 2. Move the fact, in ONE pass. The backfill is a CORRECTNESS step rather
--    than a tidiness one: the concurrency check reads the column from the
--    moment this deploy lands, so a schedule whose jobs are still in flight --
--    carrying the old jsonb key and a NULL column -- would count ZERO of them
--    and fire again despite already being at its limit. That is exactly the
--    runaway max_concurrent_jobs exists to prevent, so the in-flight jobs have
--    to bring their provenance with them.
--
--    The key is DROPPED as the column is written, in the same UPDATE, and
--    both halves of that matter. Dropped, because two copies of one fact
--    disagree eventually and the only thing keeping the jsonb copy buys is
--    readers nobody updated -- the compatibility shim this project does not
--    ship. In the same UPDATE, because two passes over the same rows is two
--    row versions each for no reason. The schedule's NAME and the scheduled
--    TIME stay in admin_data: nothing filters on those, which is what
--    admin_data is for.
--
--    Bounded by jobs some schedule created -- cron rate, not job rate.
--    Idempotent: once it has run, no row carries the key, so the predicate
--    matches nothing.
--
--    The digits test is not decoration. admin_data is free-form jsonb, so one
--    hand-edited row with a non-numeric schedule_id would abort the whole
--    upgrade on a cast error. Such a row is left ENTIRELY alone -- key and
--    all -- rather than having its column set to NULL and its key deleted:
--    that would be this migration silently destroying the only copy of
--    something it could not understand.
-- ----------------------------------------------------------------------------
UPDATE jorb
   SET schedule_id = COALESCE(schedule_id, (admin_data->>'schedule_id')::BIGINT),
       admin_data  = admin_data - 'schedule_id'
 WHERE admin_data->>'schedule_id' ~ '^[0-9]+$';

-- ----------------------------------------------------------------------------
-- 3. The index. Named exactly as schema.sql names it, so IF NOT EXISTS is a
--    reliable "already done" test; the reasoning for both halves of the
--    predicate lives there, next to the definition an operator reads.
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS jorb_schedule_id_idx ON jorb (schedule_id)
    WHERE schedule_id IS NOT NULL
      AND state IN ('queued', 'claimed', 'running', 'waiting');

-- ----------------------------------------------------------------------------
-- 4. The jorb.tags comment argued that nothing filters admin_data, which was
--    the justification for leaving it unindexed. schedule_id was the
--    counterexample; now that it has left, the comment is true again and says
--    so. Comments are part of the catalog, so a fresh install and an upgraded
--    database must carry the same text.
-- ----------------------------------------------------------------------------
COMMENT ON COLUMN jorb.tags IS 'The CALLER''s labels (customer, region, batch), flat key -> scalar, for filtering jobs by something the application means. Deliberately NOT admin_data: that column is the platform''s own execution config (max_retries, timeout_seconds, save_result, schedule metadata), which nobody filters on, so indexing it would tax every enqueue to make no query faster. The one thing anybody DID filter admin_data by is now jorb.schedule_id: a fact worth querying by is worth a column, because the alternative is an expression index over a jsonb blob written by every job to answer a question asked about a handful of them.';
