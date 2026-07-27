-- ============================================================================
-- 001 -- bring a pre-migration database up to the current schema.sql
-- ============================================================================
-- WHAT THIS IS. Every pyjobby release before this one installed schema.sql
-- and nothing else, so the only databases that exist in the wild are "some
-- revision of schema.sql, whole". This file is the delta from the OLDEST such
-- revision (frozen at tests/sql/schema_before_001.sql) to the current one, and
-- because every statement below is conditional it is equally the delta from
-- any intermediate revision: a database that already has half of this gets
-- the other half and nothing else.
--
-- WHAT IT IS NOT is a file a fresh install ever executes. A fresh install runs
-- schema.sql -- which already contains everything here -- and migrate() then
-- RECORDS this version without running it. The two paths are held together by
-- tests/test_migrations.py, which upgrades the frozen old schema with this
-- file and requires the result to be catalog-identical to a fresh install.
--
-- IDEMPOTENCY IS A REQUIREMENT, NOT A COURTESY. Applying this to a database
-- that already has all of it must change nothing at all (a test asserts
-- exactly that against a fresh install), because that is the only thing that
-- makes "run migrate on every deploy" safe.
--
-- OPERATIONAL NOTE. The index builds below take ACCESS EXCLUSIVE-equivalent
-- write locks on the tables they touch for the duration of the build, and
-- CREATE INDEX CONCURRENTLY is not available to a migration runner (it cannot
-- run inside a transaction, and a migration that half-applies is worse than
-- one that blocks). On a large jorb this is a maintenance-window operation:
-- see docs/deployment-guide.md.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. New columns. All of them exist in the current schema.sql; each is either
--    nullable or NOT NULL with a default, so no table rewrite is needed to
--    backfill and existing rows get the same value a fresh row would.
-- ----------------------------------------------------------------------------
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS awaited BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '{}';

ALTER TABLE jorb_worker ADD COLUMN IF NOT EXISTS idle BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jorb_worker
    ADD COLUMN IF NOT EXISTS job_threads INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jorb_worker
    ADD COLUMN IF NOT EXISTS job_threads_abandoned INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN jorb.claimed_at IS 'When this attempt was admitted by a worker. Rate limits count admissions, not execution starts: started is written after the claim commits, so counting it lets a claim miss the claim before it.';
COMMENT ON COLUMN jorb.awaited IS 'Someone has waited on this job (wait_for_result/get_event). Set by the waiter BEFORE it looks at the state, and never cleared: it is the demand signal that switches the jorb_done and jorb_event notifications on for this job, and it costs one HOT update per job that is ever awaited.';
COMMENT ON COLUMN jorb.tags IS 'The CALLER''s labels (customer, region, batch), flat key -> scalar, for filtering jobs by something the application means.';
COMMENT ON COLUMN jorb_worker.idle IS 'This worker found nothing to claim and is now parked on the jorb_enqueued wakeup.';
COMMENT ON COLUMN jorb_worker.job_threads IS 'Size of this worker''s own job-thread pool (--job-threads). 0 means nothing here was written by a pyjobby worker heartbeat, and both counts should be ignored.';
COMMENT ON COLUMN jorb_worker.job_threads_abandoned IS 'Job threads this worker started that are STILL RUNNING while no job of its own is: threads left behind by synchronous jobs that exceeded their deadline, which nothing can interrupt.';

-- ----------------------------------------------------------------------------
-- 2. Indexes that did not exist before. Named exactly as schema.sql names
--    them, so IF NOT EXISTS is a reliable "already done" test.
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS jorb_claimed_at_idx ON jorb (queue, claimed_at)
    WHERE claimed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS jorb_created_idx ON jorb (created);
CREATE INDEX IF NOT EXISTS jorb_retention_idx ON jorb (COALESCE(finished, updated))
    WHERE state IN ('finished', 'crashed', 'cancelled');
CREATE INDEX IF NOT EXISTS jorb_tags_idx ON jorb USING GIN (tags) WHERE tags <> '{}';
CREATE INDEX IF NOT EXISTS jorb_worker_idle_idx ON jorb_worker (queue)
    WHERE idle AND shutdown_at IS NULL;
CREATE INDEX IF NOT EXISTS jorb_worker_retention_idx ON jorb_worker (shutdown_at)
    WHERE shutdown_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS jorb_mailbox_consumed_idx ON jorb_mailbox (consumed_at)
    WHERE consumed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS jorb_dag_retention_idx ON jorb_dag (created);
CREATE INDEX IF NOT EXISTS jorb_dependencies_depends_on_idx
    ON jorb_dependencies (depends_on);
CREATE INDEX IF NOT EXISTS jorb_schedule_log_retention_idx
    ON jorb_schedule_log (actual_time);

-- ----------------------------------------------------------------------------
-- 3. Indexes whose DEFINITION changed: jorb.run_group and jorb.uid are NULL on
--    almost every row, so both indexes became partial (see schema.sql for the
--    write-amplification argument). A name-only IF NOT EXISTS cannot see that,
--    so the old shape is detected by its missing predicate and only then
--    rebuilt -- a database already carrying the partial index is left alone
--    rather than made to rebuild an index on the hottest table in the system.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'jorb_run_group_idx' AND i.indpred IS NULL) THEN
        DROP INDEX jorb_run_group_idx;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'jorb_uid_idx' AND i.indpred IS NULL) THEN
        DROP INDEX jorb_uid_idx;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS jorb_run_group_idx ON jorb (run_group)
    WHERE run_group IS NOT NULL;
CREATE INDEX IF NOT EXISTS jorb_uid_idx ON jorb (uid) WHERE uid IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 4. jorb_history.job_id gained its foreign key. History is the biggest child
--    of jorb, so without the key retention frees the small tables and leaves
--    the largest one growing forever.
--
--    The DELETE is the price of adding it late: under the current schema a
--    history row whose job is gone CANNOT EXIST -- the cascade would have
--    removed it -- so rows that reference a deleted job are exactly the drift
--    this migration is closing, and ADD CONSTRAINT would refuse to validate
--    while they remain. It is bounded by the jobs an old install deleted by
--    hand or by an older retention sweep.
-- ----------------------------------------------------------------------------
DELETE FROM jorb_history h
 WHERE NOT EXISTS (SELECT 1 FROM jorb j WHERE j.id = h.job_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'jorb_history_job_id_fkey'
                      AND conrelid = 'jorb_history'::regclass) THEN
        ALTER TABLE jorb_history ADD CONSTRAINT jorb_history_job_id_fkey
            FOREIGN KEY (job_id) REFERENCES jorb (id) ON DELETE CASCADE;
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 5. Storage parameters. Part of the install, not an operator step: a job
--    costs ~4 row versions, so the server defaults let jorb accumulate
--    garbage far past the point where the claim index bloats.
-- ----------------------------------------------------------------------------
ALTER TABLE jorb SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_vacuum_threshold     = 1000,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_vacuum_cost_limit    = 2000,
    fillfactor                      = 85
);

ALTER TABLE jorb_history SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_vacuum_threshold    = 5000,
    autovacuum_vacuum_cost_limit   = 2000
);

-- ----------------------------------------------------------------------------
-- 6. The claim path moved into the database. Bodies are byte-identical to
--    schema.sql (a test compares their md5 against a fresh install), and the
--    reasoning behind every line of them lives there, next to the definition
--    an operator actually reads.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claim_queue_lock(p_queue TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql
SET lock_timeout = '50ms'
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('pyjobby.claim:' || p_queue));
    -- Acquired inside this block's implicit subtransaction, which commits
    -- with the block: PostgreSQL transfers the lock to the parent
    -- transaction, so it is held for the rest of the claim exactly as a
    -- lock taken at the top level would be.
    RETURN TRUE;
EXCEPTION WHEN lock_not_available THEN
    -- Timed out: nothing was acquired, so there is nothing to release.
    RETURN FALSE;
END;
$$;

COMMENT ON FUNCTION claim_queue_lock IS 'Serialise claims for one controlled queue, waiting at most lock_timeout (50ms). TRUE = held for the rest of the transaction; FALSE = timed out, treat the queue as busy.';

CREATE OR REPLACE FUNCTION claim_jorb(
    p_queue        TEXT,
    p_capabilities TEXT[],
    p_max_prio     INTEGER,
    p_worker_pid   INTEGER,
    p_worker_host  TEXT,
    p_worker_id    BIGINT
) RETURNS SETOF jorb
LANGUAGE plpgsql AS $$
DECLARE
    q jorb_queue%ROWTYPE;
BEGIN
    SELECT * INTO q FROM jorb_queue WHERE name = p_queue;

    IF COALESCE(q.paused, FALSE) THEN
        RETURN;
    END IF;

    IF q.max_concurrency IS NOT NULL OR q.rate_limit IS NOT NULL THEN
        -- Bounded on purpose (see claim_queue_lock): wait a little to be
        -- served in order, but never longer than the timeout, so a claim held
        -- open by a slow or stuck transaction can never freeze the queue.
        IF NOT claim_queue_lock(p_queue) THEN
            RETURN;
        END IF;

        IF q.max_concurrency IS NOT NULL AND q.max_concurrency <= (
               SELECT count(*) FROM jorb
               WHERE queue = p_queue AND state IN ('claimed', 'running')) THEN
            RETURN;
        END IF;

        IF q.rate_limit IS NOT NULL AND q.rate_limit <= (
               SELECT count(*) FROM jorb
               WHERE queue = p_queue
                 AND claimed_at > now()
                     - make_interval(secs => q.rate_period_seconds)) THEN
            RETURN;
        END IF;
    END IF;

    RETURN QUERY
    UPDATE jorb
       SET state      = 'claimed',
           worker_pid = p_worker_pid,
           worker_host= p_worker_host,
           claimed_by = p_worker_id,
           claimed_at = now(),
           run_count  = run_count + 1,
           run_epoch  = run_epoch + 1,
           updated    = now()
     WHERE id = (
           SELECT j.id FROM jorb j
            WHERE j.queue = p_queue
              AND (j.capability = ANY(p_capabilities) OR j.capability IS NULL)
              AND j.prio <= p_max_prio
              AND j.run_after <= now()
              AND j.state = 'queued'
            ORDER BY j.prio, j.run_after
              FOR UPDATE OF j SKIP LOCKED
            LIMIT 1)
    RETURNING *;
END;
$$;

COMMENT ON FUNCTION claim_jorb IS 'Atomically admit at most one queued job for a worker, enforcing the queue pause/concurrency/rate controls. Returns zero rows when nothing is claimable.';

-- ----------------------------------------------------------------------------
-- 7. Notifications: seven ungated per-channel trigger functions collapsed into
--    one demand-gated jorb_notify(). This is the change that matters most to
--    an upgraded database: until it lands, every enqueue and every state
--    transition takes the global NOTIFY commit lock for a consumer that is not
--    listening. schema.sql carries the measurements and the policy.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION jorb_notify() RETURNS trigger AS $$
DECLARE
    channel CONSTANT TEXT := TG_ARGV[0];
    demand  CONSTANT TEXT := TG_ARGV[1];
    topic   TEXT;
    wanted  BOOLEAN;
    payload TEXT;
BEGIN
    -- 1. The topic: the finest-grained thing a consumer can register demand
    --    for. One column read, and nothing else, because everything after
    --    this point is work the write path may turn out not to need.
    CASE channel
        WHEN 'jorb_enqueued'     THEN topic := NEW.queue;
        WHEN 'jorb_done'         THEN topic := NEW.id::TEXT;
        WHEN 'jorb_cancel'       THEN topic := NEW.id::TEXT;
        WHEN 'jorb_event'        THEN topic := NEW.job_id::TEXT;
        WHEN 'schedule_executed' THEN topic := NEW.schedule_id::TEXT;
        ELSE RAISE EXCEPTION 'jorb_notify: unknown channel %', channel;
    END CASE;

    -- 2. The gate. Every demand predicate in the system is enumerated here.
    CASE demand
        -- The trigger's WHEN clause IS the gate: the demand signal lives on
        -- the very row being written, so the executor evaluates it for free
        -- and this function is not even reached when nobody is waiting.
        WHEN 'row_local' THEN
            wanted := TRUE;
        -- No gate, and that is a decision, not an oversight: this channel's
        -- consumer has no polling fallback.
        WHEN 'ungated' THEN
            wanted := TRUE;
        -- Some worker on this queue is parked on the wakeup. Indexed by
        -- jorb_worker_idle_idx, over a table with one row per worker
        -- process, whose idle subset is empty exactly when we are busy.
        WHEN 'idle_worker' THEN
            wanted := EXISTS (SELECT 1 FROM jorb_worker w
                               WHERE w.queue = topic
                                 AND w.idle
                                 AND w.shutdown_at IS NULL);
        -- A client is (or was) blocked waiting on this job. One primary-key
        -- probe on jorb.
        WHEN 'job_awaited' THEN
            wanted := EXISTS (SELECT 1 FROM jorb j
                               WHERE j.id = topic::BIGINT AND j.awaited);
        ELSE RAISE EXCEPTION 'jorb_notify: unknown demand kind %', demand;
    END CASE;

    IF NOT wanted THEN
        RETURN NULL;
    END IF;

    -- 3. The payload. Built only once the gate has said somebody wants it.
    CASE channel
        WHEN 'jorb_enqueued' THEN
            payload := NEW.queue;
        WHEN 'jorb_cancel' THEN
            payload := NEW.id::TEXT;
        WHEN 'jorb_done' THEN
            payload := json_build_object(
                'id', NEW.id, 'state', NEW.state)::TEXT;
        WHEN 'jorb_event' THEN
            payload := json_build_object(
                'job_id', NEW.job_id, 'key', NEW.key)::TEXT;
        WHEN 'schedule_executed' THEN
            payload := json_build_object(
                'schedule_id', NEW.schedule_id,
                'schedule_name', NEW.schedule_name,
                'result', NEW.result,
                'job_id', NEW.job_id)::TEXT;
    END CASE;

    PERFORM pg_notify(channel, payload);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION jorb_notify IS 'The one notification trigger. TG_ARGV = (channel, demand kind); topic, gate and payload for every channel are declared in its body.';

-- Re-point every surviving channel at jorb_notify. DROP + CREATE rather than
-- CREATE OR REPLACE because three of the five changed shape, not just their
-- function: jorb_enqueued_notify became a DEFERRABLE CONSTRAINT TRIGGER (the
-- demand signal lives on another table, so the gate has to be evaluated at
-- COMMIT), and jorb_done_notify gained `AND NEW.awaited` in its WHEN clause.
DROP TRIGGER IF EXISTS jorb_enqueued_notify ON jorb;
CREATE CONSTRAINT TRIGGER jorb_enqueued_notify
    AFTER INSERT OR UPDATE OF state ON jorb
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.state = 'queued')
    EXECUTE FUNCTION jorb_notify('jorb_enqueued', 'idle_worker');

DROP TRIGGER IF EXISTS jorb_done_notify ON jorb;
CREATE TRIGGER jorb_done_notify
    AFTER UPDATE OF state ON jorb
    FOR EACH ROW WHEN (NEW.state IN ('finished', 'crashed', 'cancelled')
                       AND NEW.awaited)
    EXECUTE FUNCTION jorb_notify('jorb_done', 'row_local');

DROP TRIGGER IF EXISTS jorb_cancel_notify ON jorb;
CREATE TRIGGER jorb_cancel_notify
    AFTER UPDATE OF cancel_requested ON jorb
    FOR EACH ROW WHEN (NEW.cancel_requested AND NEW.state = 'running')
    EXECUTE FUNCTION jorb_notify('jorb_cancel', 'row_local');

DROP TRIGGER IF EXISTS jorb_event_notify ON jorb_event;
CREATE TRIGGER jorb_event_notify
    AFTER INSERT OR UPDATE ON jorb_event
    FOR EACH ROW EXECUTE FUNCTION jorb_notify('jorb_event', 'job_awaited');

DROP TRIGGER IF EXISTS schedule_executed_notify ON jorb_schedule_log;
CREATE TRIGGER schedule_executed_notify
    AFTER INSERT ON jorb_schedule_log
    FOR EACH ROW EXECUTE FUNCTION jorb_notify('schedule_executed', 'ungated');

-- The two channels that were DELETED rather than gated or renamed.
-- job_state_change fired on every state transition (four per job) and pushed
-- each one to the dashboard, which now polls aggregates instead;
-- jorb_mailbox never had a listener at all -- Job.recv() polls the table.
-- Both were a global commit lock per write, paid for nobody.
DROP TRIGGER IF EXISTS job_state_change_notify ON jorb;
DROP TRIGGER IF EXISTS jorb_mailbox_notify ON jorb_mailbox;

-- Now that nothing references them. Plain DROP, not CASCADE: if some object
-- this migration does not know about still depends on one of these, the
-- migration must fail loudly rather than silently delete it.
DROP FUNCTION IF EXISTS notify_job_state_change();
DROP FUNCTION IF EXISTS notify_jorb_enqueued();
DROP FUNCTION IF EXISTS notify_jorb_done();
DROP FUNCTION IF EXISTS notify_jorb_cancel();
DROP FUNCTION IF EXISTS notify_jorb_event();
DROP FUNCTION IF EXISTS notify_jorb_mailbox();
DROP FUNCTION IF EXISTS notify_schedule_executed();
