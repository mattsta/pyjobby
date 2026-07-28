-- ============================================================================
-- Queue control plane (live-tunable; absent row = defaults)
-- ============================================================================
CREATE TABLE jorb_queue (
    name                TEXT PRIMARY KEY,
    paused              BOOLEAN     NOT NULL DEFAULT FALSE,
    max_concurrency     INTEGER,              -- NULL = unlimited (claimed+running cap)
    rate_limit          INTEGER,              -- max starts per rate_period; NULL = unlimited
    rate_period_seconds DOUBLE PRECISION NOT NULL DEFAULT 60,
    created             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated             TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE jorb_queue IS 'Per-queue controls enforced by claim_jorb(); rows are optional (missing queue = unpaused, unlimited).';

