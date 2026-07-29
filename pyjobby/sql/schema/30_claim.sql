-- ============================================================================
-- The claim path
-- ============================================================================
-- Claiming lives in the database, not in the worker, so that the queue's
-- controls are enforced for EVERY claimer rather than only for clients that
-- remember to enforce them.
--
-- Why the advisory lock: under READ COMMITTED a statement sees the snapshot
-- taken when it began, so two simultaneous claims cannot see each other's
-- uncommitted rows and a cap of 1 would admit both. Serializing claims for a
-- controlled queue fixes that -- and because PL/pgSQL takes a new snapshot per
-- statement, the counts below run AFTER the lock and therefore see every claim
-- that has already committed. Uncontrolled queues (the common case) never take
-- the lock and keep the lock-free fast path.

-- Take the per-queue claim lock, or give up after a BOUNDED wait.
--
-- The serialising lock has to be bounded: a claim held open by a slow or
-- stuck transaction must never freeze the whole queue. An immediate try-lock
-- bounds it at zero, and that is what used to be here -- but losing a
-- try-lock is not free for the thing that actually loses it. A CLIENT that
-- retries in a loop pays one wasted round trip; a WORKER (see pj.py's run
-- loop) treats an empty claim as "this queue has no work", publishes idle
-- demand -- switching this queue's enqueue notifications back on for every
-- producer -- and then parks for checkInterval, five seconds by default,
-- waiting for a wakeup that is not coming because the work it wanted was
-- enqueued before it went to sleep.
--
-- Measured with four real workers on a queue holding 40 jobs under a cap that
-- could never bind: the try-lock let ONE of the four ever claim anything --
-- whoever won kept winning and the other three slept on a full queue. Waiting
-- for the lock puts claimers in the lock manager's FIFO queue instead, and
-- all four work. lock_timeout keeps the give-up guarantee: the wait is capped,
-- the timeout arrives as 55P03 (lock_not_available), and it is caught here and
-- reported as "nothing claimable" exactly as a lost try-lock was.
--
-- What this is NOT is a throughput fix, and it must not be sold as one. A
-- claimer that lost the try-lock held no lock, so its retries were wasted
-- BESIDE the critical section, not inside it: capped throughput is
-- 1/(critical section) either way. Measured against the try-lock in the same
-- run it lands at 0.96x on an idle box and 2.1x on a saturated one, with 98%+
-- of the round trips gone in both -- which is the shape you get when the
-- removed work was never on the critical path but was still competing for CPU
-- with the thing that was. Neither number is the ceiling moving. Raising the
-- capped ceiling means a cheaper critical section, or more than one claim per
-- acquisition (batching) -- a worker-model change, not a lock change.
--
-- Batching was then measured and REJECTED, and the numbers are in
-- docs/SCALE.md ("Claiming a batch per lock acquisition"): the shape where
-- this lock is the only constraint left -- a cap too high to refuse, short
-- jobs, claimers to spare -- sustains 3,211 claims/s, 11.6x the 278/s
-- reference workload, and a LOW cap is bounded by cap/job-duration where no
-- claim strategy reaches. Do not reopen this without a capped queue that
-- needs 11M jobs/hour on its own, or a `pj-bench plans` run showing the
-- concurrency-cap count below has stopped using an index.
--
-- Why its own function rather than SET LOCAL inside claim_jorb: a function's
-- SET clause is applied on entry and restored by PostgreSQL on exit, error or
-- not, so the timeout covers the lock acquisition and NOTHING else. Putting
-- it on claim_jorb would also arm it on the claiming UPDATE, whose
-- SKIP LOCKED probe can briefly re-contend for the row it picked, turning a
-- momentary wait into an error the worker never asked to handle. SET LOCAL
-- would instead leak the timeout into the caller's remaining transaction.
--
-- Why 50 ms: it is the maximum a claimer stalls, so it trades claim latency
-- against wasted round trips, and the sweep in tests/test_claim_contention.py
-- puts the knee well below it -- eight claimers waste 2,710 round trips per
-- 2,000 claims at 1 ms (the try-lock's regime), 86 at 5 ms, 7 at 20 ms, 2 at
-- 50 ms, 0 at 200 ms, with claim rate flat from 5 ms up. 50 ms buys the last
-- of the waste at a stall a busy worker can absorb -- a claim wedged open by a
-- stuck transaction degrades it to 20 attempts a second instead of hanging it
-- -- and is 1% of the 5 s idle poll, so an idle worker cannot notice it. 200 ms
-- would quadruple the worst-case stall to save two round trips in two thousand.
CREATE FUNCTION claim_queue_lock(p_queue TEXT) RETURNS BOOLEAN
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

CREATE FUNCTION claim_jorb(
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
    -- Whether the limits above are counted per lane rather than per queue.
    partitioned BOOLEAN;
    -- The lanes that may not be admitted from on this attempt, split because
    -- SQL cannot put a NULL in an "= ANY(...)" set: NULL is a LANE here, not
    -- a missing value, so it gets its own flag rather than an array entry
    -- that would silently never match.
    full_keys   TEXT[]  := '{}';
    full_null   BOOLEAN := FALSE;
    rated_keys  TEXT[]  := '{}';
    rated_null  BOOLEAN := FALSE;
    -- Initialised, not merely declared: a NULL array would make
    -- `partition_key = ANY(blocked_keys)` NULL for every labelled row and
    -- quietly render the whole queue unclaimable, which is the failure mode
    -- this feature exists to prevent rather than to introduce.
    blocked_keys TEXT[]  := '{}';
    blocked_null BOOLEAN := FALSE;
BEGIN
    SELECT * INTO q FROM jorb_queue WHERE name = p_queue;

    IF COALESCE(q.paused, FALSE) THEN
        RETURN;
    END IF;

    -- PARTITIONED IS NOT A THIRD TIER, and the AND is what says so. A queue
    -- with partition_limits and no limit set has nothing to re-scope, so it
    -- stays on the lock-free fast path exactly as it would with the flag off
    -- -- the flag re-scopes limits, it never adds one.
    partitioned := COALESCE(q.partition_limits, FALSE)
                   AND (q.max_concurrency IS NOT NULL OR q.rate_limit IS NOT NULL);

    IF q.max_concurrency IS NOT NULL OR q.rate_limit IS NOT NULL THEN
        -- Bounded on purpose (see claim_queue_lock): wait a little to be
        -- served in order, but never longer than the timeout, so a claim held
        -- open by a slow or stuck transaction can never freeze the queue.
        --
        -- THE CONDITION IS THE SAME ONE IT ALWAYS WAS, per-lane or not: the
        -- lock is what makes a count taken before an uncommitted claim wrong,
        -- and a per-lane count is no less blind to one than a per-queue count
        -- is. So partitioning changes WHAT is counted and nothing about WHO
        -- serialises -- an unlimited queue still never arrives here.
        IF NOT claim_queue_lock(p_queue) THEN
            RETURN;
        END IF;

        IF NOT partitioned THEN
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
        ELSE
            -- Per lane, the same two counts GROUPED -- and the difference in
            -- shape is the whole design. A queue-wide limit answers "is the
            -- queue full?" and returns; a per-lane limit cannot, because the
            -- answer is different for every lane and a lane with headroom
            -- must still be served. So the counts do not decide whether to
            -- claim, they produce the set of lanes this attempt may not
            -- claim from, and the claim below skips exactly those.
            --
            -- BOUNDED BY THE SAME THING THE OLD COUNTS WERE. The saturated
            -- set cannot be larger than what produced it: at most
            -- (in-flight work / max_concurrency) lanes can be at the
            -- concurrency cap, and at most (admissions in the window /
            -- rate_limit) can be at the rate limit. Both are bounded by the
            -- fleet and the window, never by the backlog or by the number of
            -- lanes that EXIST -- there is deliberately no scan over the
            -- distinct values of partition_key anywhere in this function.
            IF q.max_concurrency IS NOT NULL THEN
                SELECT COALESCE(array_agg(lane.partition_key)
                                    FILTER (WHERE lane.partition_key IS NOT NULL),
                                '{}'::text[]),
                       COALESCE(bool_or(lane.partition_key IS NULL), FALSE)
                  INTO full_keys, full_null
                  FROM (SELECT partition_key
                          FROM jorb
                         WHERE queue = p_queue
                           AND state IN ('claimed', 'running')
                         GROUP BY partition_key
                        HAVING count(*) >= q.max_concurrency) lane;
            END IF;

            IF q.rate_limit IS NOT NULL THEN
                SELECT COALESCE(array_agg(lane.partition_key)
                                    FILTER (WHERE lane.partition_key IS NOT NULL),
                                '{}'::text[]),
                       COALESCE(bool_or(lane.partition_key IS NULL), FALSE)
                  INTO rated_keys, rated_null
                  FROM (SELECT partition_key
                          FROM jorb
                         WHERE queue = p_queue
                           AND claimed_at > now()
                               - make_interval(secs => q.rate_period_seconds)
                         GROUP BY partition_key
                        HAVING count(*) >= q.rate_limit) lane;
            END IF;

            blocked_keys := full_keys || rated_keys;
            blocked_null := full_null OR rated_null;
        END IF;
    END IF;

    IF partitioned THEN
        -- The claim, restricted to lanes with headroom.
        --
        -- WHY THIS IS A SECOND STATEMENT and not a predicate bolted onto the
        -- one below: an unpartitioned queue -- every queue, by default --
        -- must reach a claim that is byte-identical to the one it always ran,
        -- with the same plan and the same cost. A single statement carrying a
        -- lane test that is trivially true for them would still be a
        -- different statement, and this is the hottest query in the platform.
        --
        -- ORDER IS UNCHANGED: prio then run_after, the queue's own claim
        -- order, served by jorb_claim_idx exactly as below. Partitioning
        -- decides WHICH rows are eligible, never which eligible row wins.
        --
        -- WHAT IT COSTS, honestly: the scan walks past queued rows whose lane
        -- is saturated to reach the first one whose lane is not. When nothing
        -- is saturated -- the caught-up case, and the common one -- the
        -- blocked set is empty, `= ANY('{}')` is false for every row, and the
        -- scan stops on the first index entry exactly as the unpartitioned
        -- claim does. The cost is therefore paid only where the feature is
        -- doing work: it is proportional to the backlog of the lanes that are
        -- currently AT their limit and sorting ahead of the winner, which is
        -- the queued depth of the tenants being held back. `pj-bench plans`
        -- gates that shape (the `partitioned_claim` case) with seeded lanes.
        --
        -- THE NULL LANE IS A LANE. Its rows are refused only when the NULL
        -- lane itself is saturated, and admitted like anyone else's the rest
        -- of the time; `partition_key = ANY(blocked_keys)` would be NULL for
        -- them, so the CASE spells the two apart rather than letting three-
        -- valued logic quietly make every unlabelled job unclaimable forever.
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
                  AND CASE WHEN j.partition_key IS NULL
                           THEN NOT blocked_null
                           ELSE NOT (j.partition_key = ANY(blocked_keys))
                      END
                ORDER BY j.prio, j.run_after
                  FOR UPDATE OF j SKIP LOCKED
                LIMIT 1)
        RETURNING *;
        RETURN;
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

COMMENT ON FUNCTION claim_jorb IS 'Atomically admit at most one queued job for a worker, enforcing the queue pause/concurrency/rate controls. With jorb_queue.partition_limits those two limits are counted PER jorb.partition_key instead of per queue, and the claim skips the lanes that are at theirs -- so a saturated lane never blocks another, and the NULL lane is a lane like any other. Returns zero rows when nothing is claimable.';

