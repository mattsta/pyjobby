-- ============================================================================
-- Queue control plane (live-tunable; absent row = defaults)
-- ============================================================================
CREATE TABLE jorb_queue (
    name                TEXT PRIMARY KEY,
    paused              BOOLEAN     NOT NULL DEFAULT FALSE,
    max_concurrency     INTEGER,              -- NULL = unlimited (claimed+running cap)
    rate_limit          INTEGER,              -- max starts per rate_period; NULL = unlimited
    rate_period_seconds DOUBLE PRECISION NOT NULL DEFAULT 60,
    -- re-scope the two limits above to PER jorb.partition_key (see the COMMENT)
    partition_limits    BOOLEAN     NOT NULL DEFAULT FALSE,
    created             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated             TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE jorb_queue IS 'Per-queue controls enforced by claim_jorb(); rows are optional (missing queue = unpaused, unlimited).';
COMMENT ON COLUMN jorb_queue.partition_limits IS 'Re-scope max_concurrency and rate_limit to PER jorb.partition_key instead of per queue. It ADDS NO LIMIT OF ITS OWN: a queue with neither limit set gains nothing from turning it on, because there is nothing to re-scope. With it on, max_concurrency N means each key may have N jobs in flight and rate_limit R means each key admits R per window -- so one tenant saturating its own lane cannot starve the others, which is the whole point. THE NULL LANE IS A LANE. Jobs with no partition_key form ONE lane of their own, capped and counted exactly like every named one; they are never invisible to the claim and never refused for being unlabelled, because a fair-share scheme that silently blackholes the unlabelled work is worse than no scheme at all. The exactness and the cost model are unchanged: a queue with a limit set serialises its claims on claim_queue_lock whether the limit is per queue or per lane, and a queue with no limit still never takes the lock. What DOES change is the claim''s search -- it walks past queued rows whose lane is saturated to reach one whose lane is not (see claim_jorb).';

