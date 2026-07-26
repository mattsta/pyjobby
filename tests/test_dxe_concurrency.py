"""Multi-worker races: the invariants a single-worker test cannot see.

Every test here runs SEVERAL real ``JobSystem`` workers against one queue at
the same time, because that is the only configuration where "the test passed"
and "the property held" come apart. The properties under test:

* a completed DXE step is executed exactly once, even when the job is retried
  while other workers hammer the same queue (side effects are counted in the
  ``jorb_test_effect`` ledger, not inferred from the checkpoint table);
* two workers never execute the same job (claim exclusivity under load);
* ``run_epoch`` fencing: a superseded execution cannot overwrite the winner's
  result, and a job reaches exactly one terminal state;
* cancellation lands cleanly mid-step and during a durable sleep, leaving the
  completed checkpoints intact and recording nothing for work that never ran;
* the queue control plane (``max_concurrency``, ``rate_limit``) under real
  concurrent claims — where two admission-control bugs live: each limit gets a
  strict test for the part that holds, a load test marked xfail for the part
  that does not, and a deterministic ``xfail(strict=True)`` test pinning the
  exact mechanism so a fix flips it;
* concurrent monitor sweeps handle an overdue job exactly once.

Job classes are defined at module level so the workers can resolve them by
dotted path (``tests.test_dxe_concurrency.<Name>``) exactly like production
job classes.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pyjobby import db
from pyjobby.monitor import sweep_dead_workers, sweep_timed_out_jobs
from pyjobby.pj import STMTS, Job

from .conftest import wait_for_job_state
from .utils.faults import (
    count_effects,
    effect_counts,
    effect_counts_per_job,
    ensure_effects_table,
    record_effect,
)
from .utils.processes import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.concurrency]


# ============================================================================
# job classes (resolved by the workers via their dotted path)
# ============================================================================


class CountedPipelineJob(Job):
    """Three checkpointed steps, each with an OBSERVABLE side effect.

    Step 2 raises *before* its side effect on the first attempt, so the ledger
    must end up holding exactly one row per successfully recorded step: the
    only thing that can make it hold more is a completed step re-executing on
    the retry.
    """

    async def task(self, tag: str) -> dict[str, bool]:
        await self.step("s1", self._bump, tag, "s1")
        await self.step("s2", self._explode_once, tag)
        await self.step("s3", self._bump, tag, "s3")
        return {"pipeline": True}

    async def _bump(self, tag: str, label: str) -> dict[str, int]:
        return {"effect": await record_effect(self.s.cxn, tag, self.job["id"], label)}

    async def _explode_once(self, tag: str) -> dict[str, int]:
        if self.job["error_count"] == 0:
            raise RuntimeError("failing before the side effect")
        return await self._bump(tag, "s2")


class EpochWitnessJob(Job):
    """Runs slowly, then reports WHICH attempt produced the result.

    Distinguishable results are what make a lost update visible: a stale
    execution and its replacement would otherwise return the same value.
    """

    async def task(self, seconds: float = 3.0) -> dict[str, int]:
        await asyncio.sleep(seconds)
        return {"epoch": self.job["run_epoch"]}


class CancelMidStepJob(Job):
    """Completes one step, then holds inside the next one until cancelled."""

    async def task(self, tag: str, hold: float = 60.0) -> str:
        await self.step("before", self._bump, tag, "before")
        await self.step("during", self._hold, tag, hold)
        await self.step("after", self._bump, tag, "after")
        return "all-steps-done"

    async def _bump(self, tag: str, label: str) -> dict[str, int]:
        return {"effect": await record_effect(self.s.cxn, tag, self.job["id"], label)}

    async def _hold(self, tag: str, hold: float) -> dict[str, int]:
        await asyncio.sleep(hold)
        return await self._bump(tag, "during")


# ============================================================================
# helpers
# ============================================================================

CLAIM_HOST = "concurrency-test-host"


async def enqueue(
    pool,
    queue: str,
    job_class: str,
    kwargs: dict | None = None,
    admin_data: dict | None = None,
) -> int:
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        job_class,
        kwargs or {},
        queue,
        admin_data or {},
    )
    return job_id


async def enqueue_batch(
    pool, queue: str, job_class: str, kwargs: dict, count: int
) -> list[int]:
    """Enqueue ``count`` identical jobs in ONE transaction.

    All the enqueue NOTIFYs are delivered together at commit, so every idle
    worker wakes in the same event-loop pass and issues its claim at the same
    instant — the arrival pattern that stresses admission control hardest."""
    rows = await pool.fetch(
        """INSERT INTO jorb (job_class, kwargs, queue)
           SELECT $1, $2, $3 FROM generate_series(1, $4) RETURNING id""",
        job_class,
        kwargs,
        queue,
        count,
    )
    return [r["id"] for r in rows]


async def claim_once(conn, queue: str, worker_id: int | None = None):
    """Claim through the REAL claim statement (bumping run_epoch)."""
    return await conn.fetchrow(
        STMTS["claim"], 4242, CLAIM_HOST, queue, ["test"], 1000, worker_id
    )


async def history_events(pool, job_id: int) -> list[str]:
    return [
        r["event"]
        for r in await pool.fetch(
            "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
        )
    ]


async def event_count(pool, job_id: int, *events: str) -> int:
    total: int = await pool.fetchval(
        "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event = ANY($2)",
        job_id,
        list(events),
    )
    return total


async def worker_is_idle(system) -> bool:
    return system._current_job_id is None


async def sample_inflight_until_drained(
    pool, queue: str, total: int, timeout: float = 90.0, interval: float = 0.025
) -> list[int]:
    """Poll (claimed+running) for a queue until every job is finished.

    Returns every sample taken, so a test can assert on the observed peak."""
    samples: list[int] = []
    done = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await pool.fetchrow(
            """SELECT count(*) FILTER (WHERE state IN ('claimed', 'running'))
                          AS inflight,
                      count(*) FILTER (WHERE state = 'finished') AS done
               FROM jorb WHERE queue = $1""",
            queue,
        )
        samples.append(row["inflight"])
        done = row["done"]
        if done == total:
            return samples
        await asyncio.sleep(interval)
    raise AssertionError(
        f"only {done}/{total} jobs finished within {timeout}s "
        f"(peak in-flight {max(samples)})"
    )


# ============================================================================
# 1. exactly-once step effects under contention
# ============================================================================


@pytest.mark.slow
async def test_completed_steps_never_re_execute_under_worker_contention(
    live_worker, unique_queue, db_pool
):
    """The ledger must match the checkpoints: one execution per recorded step.

    Four retrying pipelines are raced by three workers. Each job fails its
    second step on attempt 1 (before that step's side effect), so 12 steps
    succeed in total across 8 attempts. If fast-forwarding regressed — steps
    re-running on the retry — the ledger would hold 16 rows instead of 12
    while every state assertion still passed.
    """
    await ensure_effects_table(db_pool)
    for _ in range(3):
        await live_worker()

    ids = [
        await enqueue(
            db_pool,
            unique_queue,
            "tests.test_dxe_concurrency.CountedPipelineJob",
            {"tag": unique_queue},
            {"max_retries": 3, "initial_retry_delay": 0},
        )
        for _ in range(4)
    ]

    rows = [
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=40)
        for job_id in ids
    ]
    assert [r["result"] for r in rows] == [{"pipeline": True}] * 4
    assert [r["error_count"] for r in rows] == [1] * 4
    assert [r["run_epoch"] for r in rows] == [2] * 4  # exactly two attempts each

    # the checkpoint table: 12 successful steps, no failures left behind
    # (step 2's recorded error is overwritten when it succeeds on the retry)
    recorded = await db_pool.fetchrow(
        """SELECT count(*) FILTER (WHERE error IS NULL)     AS succeeded,
                  count(*) FILTER (WHERE error IS NOT NULL) AS failed
           FROM jorb_step WHERE job_id = ANY($1)""",
        ids,
    )
    assert (recorded["succeeded"], recorded["failed"]) == (12, 0)

    # ...and the ledger of what ACTUALLY executed matches it exactly
    assert await count_effects(db_pool, unique_queue) == 12
    assert await effect_counts(db_pool, unique_queue) == {"s1": 4, "s2": 4, "s3": 4}
    per_job = await effect_counts_per_job(db_pool, unique_queue)
    assert sorted(per_job) == sorted(
        (job_id, label) for job_id in ids for label in ("s1", "s2", "s3")
    )
    assert list(per_job.values()) == [1] * 12

    # the fast-forwarded step keeps its ORIGINAL attempt's epoch
    first_steps = await db_pool.fetch(
        """SELECT run_epoch FROM jorb_step
           WHERE job_id = ANY($1) AND name = 's1'""",
        ids,
    )
    assert [s["run_epoch"] for s in first_steps] == [1] * 4
    # while the re-executed ones belong to the retry
    later_steps = await db_pool.fetch(
        """SELECT run_epoch FROM jorb_step
           WHERE job_id = ANY($1) AND name IN ('s2', 's3')""",
        ids,
    )
    assert [s["run_epoch"] for s in later_steps] == [2] * 8


# ============================================================================
# 2. claim exclusivity under real concurrent load
# ============================================================================


@pytest.mark.slow
async def test_every_job_runs_exactly_once_across_four_workers(
    live_worker, unique_queue, db_pool
):
    """Twelve jobs, four workers: each job is executed by exactly one worker.

    ``jorb_history`` is the witness — one 'claimed' and one 'running' event
    per job. Without ``FOR UPDATE ... SKIP LOCKED`` in the claim, two workers
    would claim the same row and the counts would double while every job still
    ended up 'finished' with a correct-looking result.
    """
    workers = [await live_worker() for _ in range(4)]

    ids = [
        await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": n})
        for n in range(12)
    ]

    rows = [
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=40)
        for job_id in ids
    ]
    assert [r["result"] for r in rows] == [{"doubled": n * 2} for n in range(12)]
    assert [r["run_count"] for r in rows] == [1] * 12
    assert [r["run_epoch"] for r in rows] == [1] * 12
    assert [r["error_count"] for r in rows] == [0] * 12

    counts = await db_pool.fetch(
        """SELECT job_id,
                  count(*) FILTER (WHERE event = 'claimed') AS claimed,
                  count(*) FILTER (WHERE event = 'running') AS running
           FROM jorb_history WHERE job_id = ANY($1) GROUP BY job_id""",
        ids,
    )
    assert len(counts) == 12
    assert [(c["claimed"], c["running"]) for c in counts] == [(1, 1)] * 12

    # the workers collectively processed the 12 jobs and nothing more
    assert sum(w.processed for w in workers) == 12
    assert sum(w.errors for w in workers) == 0

    # every job was owned by one of THIS test's registered workers
    registered = {
        r["id"]
        for r in await db_pool.fetch(
            "SELECT id FROM jorb_worker WHERE queue = $1", unique_queue
        )
    }
    assert len(registered) == 4
    assert len({r["claimed_by"] for r in rows} - registered) == 0


# ============================================================================
# 3. run_epoch fencing under real concurrency
# ============================================================================


@pytest.mark.slow
async def test_stale_execution_cannot_overwrite_the_winning_result(
    live_worker, unique_queue, db_pool
):
    """Worker A is superseded mid-run; only worker B's result may survive.

    A claims (epoch 1) and starts a slow job; a monitor-style requeue makes
    the row claimable again; B claims it (epoch 2) and completes it. A's own
    completion is epoch-fenced, so the job must end up with B's result and
    exactly ONE terminal transition.
    """
    a = await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_concurrency.EpochWitnessJob",
        {"seconds": 3},
    )
    running = await wait_for_job_state(db_pool, job_id, ("running",))
    assert running["run_epoch"] == 1

    requeued = await db.requeue_job(
        db_pool, job_id, allowed_states=("claimed", "running"), reset_errors=False
    )
    assert requeued == job_id

    b = await live_worker()
    claimed = await wait_for_job_state(db_pool, job_id, ("running",))
    assert claimed["run_epoch"] == 2

    # let A come back from its (now stale) execution and try to complete
    await wait_until(
        lambda: worker_is_idle(a),
        timeout=20,
        what="worker A finished its stale attempt",
    )
    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "running", "the stale attempt must not finish the job"
    assert row["result"] is None

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"epoch": 2}  # B's result, never A's
    assert row["run_epoch"] == 2
    assert row["error_count"] == 0

    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "queued",
        "claimed",
        "running",
        "finished",
    ]
    assert await event_count(db_pool, job_id, "finished", "crashed", "cancelled") == 1
    assert (a.processed, b.processed) == (1, 1)


# ============================================================================
# 4. cancellation arriving mid-step
# ============================================================================


async def test_cancel_mid_step_keeps_finished_checkpoints_and_records_no_more(
    live_worker, unique_queue, db_pool
):
    """Cancel while step 2 is in flight: step 1 survives, 2 and 3 never exist.

    The completed checkpoint must stay (a resume would reuse it), the
    in-flight step must record nothing, and the step that never started must
    leave no row at any epoch.
    """
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_concurrency.CancelMidStepJob",
        {"tag": unique_queue, "hold": 60},
    )
    await wait_until(
        lambda: db_pool.fetchval(
            "SELECT 1 FROM jorb_step WHERE job_id = $1 AND name = 'before'", job_id
        ),
        what="step 1 checkpointed (so the job is inside step 2)",
    )

    assert await db.cancel_job(db_pool, job_id) == "cancel_requested"

    row = await wait_for_job_state(db_pool, job_id, ("cancelled",), timeout=15)
    assert row["run_epoch"] == 1
    assert row["error_count"] == 0
    assert row["cancel_requested"] is True
    assert row["finished"] > row["started"]
    assert row["result"] is None

    steps = await db_pool.fetch(
        """SELECT step_seq, name, error, run_epoch
           FROM jorb_step WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    assert [(s["step_seq"], s["name"], s["error"], s["run_epoch"]) for s in steps] == [
        (1, "before", None, 1)
    ]

    # only the step that really ran left a mark
    assert await effect_counts(db_pool, unique_queue) == {"before": 1}
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "cancelled",
    ]


# ============================================================================
# 5. cancellation during a durable sleep
# ============================================================================


async def test_cancel_during_durable_sleep_is_immediate_and_keeps_checkpoint(
    live_worker, unique_queue, db_pool
):
    """A sleeping job is 'queued', so cancelling it is immediate.

    Its sleep checkpoint must survive (it is the record of work already done)
    and no worker may resurrect the cancelled row.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool, unique_queue, "tests.dxe_jobs.SleeperJob", {"seconds": 30}
    )
    await wait_until(
        lambda: db_pool.fetchval(
            """SELECT 1 FROM jorb
               WHERE id = $1 AND state = 'queued' AND run_after > now()""",
            job_id,
        ),
        what="job checkpointed its durable sleep and released the worker",
    )

    assert await db.cancel_job(db_pool, job_id) == "cancelled"

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "cancelled"
    assert row["run_count"] == 1
    assert row["run_epoch"] == 1
    assert row["cancel_requested"] is False  # the queued path never asks nicely

    sleep_steps = await db_pool.fetch(
        """SELECT step_seq, name, error, run_epoch,
                  (output ->> 'wake_at')::timestamptz > now() AS still_future
           FROM jorb_step WHERE job_id = $1""",
        job_id,
    )
    assert [
        (s["step_seq"], s["name"], s["error"], s["run_epoch"], s["still_future"])
        for s in sleep_steps
    ] == [(1, "dxe.sleep", None, 1, True)]

    # the job never got past its sleep
    events = await db_pool.fetch(
        "SELECT key, value FROM jorb_event WHERE job_id = $1", job_id
    )
    assert [(e["key"], e["value"]) for e in events] == [
        ("phase", {"at": "before-sleep"})
    ]

    # and the still-running worker must not claim a cancelled row afterwards
    await asyncio.sleep(1.0)
    after = await db_pool.fetchrow(
        "SELECT state, run_count, run_epoch FROM jorb WHERE id = $1", job_id
    )
    assert (after["state"], after["run_count"], after["run_epoch"]) == (
        "cancelled",
        1,
        1,
    )
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "queued",
        "cancelled",
    ]


# ============================================================================
# 6. max_concurrency under load
# ============================================================================


async def test_max_concurrency_refuses_a_claim_while_the_cap_is_full(
    db_pool, db_params, unique_queue
):
    """The enforceable half of the cap: a COMMITTED in-flight job blocks the
    next claim outright (cap 1, two runnable jobs, one claim granted)."""
    await db_pool.execute(
        "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 1)", unique_queue
    )
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")

    conn = await db.connect(**db_params)
    try:
        first = await claim_once(conn, unique_queue)
        second = await claim_once(conn, unique_queue)
    finally:
        await conn.close()

    assert first["state"] == "claimed"
    assert second is None
    inflight = await db_pool.fetchval(
        """SELECT count(*) FROM jorb
           WHERE queue = $1 AND state IN ('claimed', 'running')""",
        unique_queue,
    )
    assert inflight == 1


@pytest.mark.slow
async def test_max_concurrency_queue_drains_and_runs_at_the_cap(
    live_worker, unique_queue, db_pool
):
    """A capped queue still drains completely, running each job exactly once.

    This is the part of the ``max_concurrency`` contract that survives
    concurrent claims: no work is lost or duplicated, and the cap is the level
    the queue actually runs at. Never EXCEEDING the cap is the xfail below.
    """
    await db_pool.execute(
        "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 2)", unique_queue
    )
    for _ in range(4):
        await live_worker()

    ids = [
        await enqueue(db_pool, unique_queue, "tests.dxe_jobs.SlowJob", {"seconds": 1})
        for _ in range(8)
    ]
    samples = await sample_inflight_until_drained(db_pool, unique_queue, len(ids))

    rows = [
        await db_pool.fetchrow(
            "SELECT state, run_count, result FROM jorb WHERE id = $1", job_id
        )
        for job_id in ids
    ]
    assert [r["state"] for r in rows] == ["finished"] * 8
    assert [r["run_count"] for r in rows] == [1] * 8
    assert [r["result"] for r in rows] == ["done"] * 8
    assert 2 in samples, f"the cap was never the running level: {sorted(set(samples))}"


@pytest.mark.slow
@pytest.mark.xfail(
    reason="BUG: max_concurrency admission counts only COMMITTED claimed/running "
    "rows, so simultaneous claims from several workers all pass the check and "
    "over-admit (see the strict mechanism test below)",
    strict=False,  # how far over the cap it goes depends on claim interleaving
)
async def test_max_concurrency_is_never_exceeded_under_load(
    live_worker, unique_queue, db_pool
):
    """In-flight jobs must NEVER exceed the queue's max_concurrency."""
    await db_pool.execute(
        "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 2)", unique_queue
    )
    for _ in range(4):
        await live_worker()

    ids = await enqueue_batch(
        db_pool, unique_queue, "tests.dxe_jobs.SlowJob", {"seconds": 1}, 10
    )
    samples = await sample_inflight_until_drained(db_pool, unique_queue, len(ids))
    assert max(samples) == 2


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a concurrent claim is admitted past max_concurrency because the "
    "cap subquery cannot see another transaction's uncommitted claim",
)
async def test_max_concurrency_cap_holds_against_a_concurrent_claim(
    db_pool, db_params, unique_queue
):
    """The mechanism behind the over-admission, with zero timing dependence.

    Claim #1 runs inside an open transaction; claim #2 (another connection)
    cannot see it under READ COMMITTED, so its ``max_concurrency`` subquery
    counts zero in-flight jobs and admits a second job past a cap of 1.

    xfail is STRICT: the visibility rule is deterministic, so an unexpected
    pass means the claim grew real mutual exclusion (an advisory lock, or a
    lock on the jorb_queue row) and this test should become a plain assertion.
    """
    await db_pool.execute(
        "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 1)", unique_queue
    )
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")

    first_conn = await db.connect(**db_params)
    second_conn = await db.connect(**db_params)
    try:
        tx = first_conn.transaction()
        await tx.start()
        await claim_once(first_conn, unique_queue)
        second = await claim_once(second_conn, unique_queue)
        await tx.commit()
    finally:
        await first_conn.close()
        await second_conn.close()

    assert second is None


# ============================================================================
# 7. rate_limit under load
# ============================================================================


@pytest.mark.slow
async def test_rate_limit_admits_exactly_its_budget_then_stops_the_queue(
    live_worker, unique_queue, db_pool
):
    """One worker, ten jobs, a budget of three per minute: three run.

    Sequential claims make the rate window exact, so this pins the intended
    semantics: after three starts the queue is closed for the rest of the
    period and the remaining seven jobs stay 'queued'.
    """
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 3, 60)""",
        unique_queue,
    )
    await live_worker()

    for _ in range(10):
        await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})

    await wait_until(
        lambda: db_pool.fetchval(
            """SELECT (count(*) = 3) OR NULL FROM jorb
               WHERE queue = $1 AND started IS NOT NULL""",
            unique_queue,
        ),
        what="the rate budget was spent",
    )
    await asyncio.sleep(1.0)  # prove the queue STAYS closed

    counts = await db_pool.fetchrow(
        """SELECT count(*)                                     AS total,
                  count(*) FILTER (WHERE started IS NOT NULL)  AS started,
                  count(*) FILTER (WHERE state = 'finished')   AS finished,
                  count(*) FILTER (WHERE state = 'queued')     AS queued
           FROM jorb WHERE queue = $1""",
        unique_queue,
    )
    assert (
        counts["total"],
        counts["started"],
        counts["finished"],
        counts["queued"],
    ) == (10, 3, 3, 7)


@pytest.mark.slow
@pytest.mark.xfail(
    reason="BUG: the rate window counts jorb.started, which the worker only "
    "writes AFTER its claim commits, so every claim overlapping that gap is "
    "admitted (see the strict mechanism test below)",
    strict=False,  # how far over budget it goes depends on claim interleaving
)
async def test_rate_limit_is_never_exceeded_under_load(
    live_worker, unique_queue, db_pool
):
    """No more than the budget may START within the rate period."""
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 3, 60)""",
        unique_queue,
    )
    for _ in range(4):
        await live_worker()

    await enqueue_batch(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1}, 10)

    peak = 0
    for _ in range(80):
        peak = max(
            peak,
            await db_pool.fetchval(
                """SELECT count(*) FROM jorb
                   WHERE queue = $1 AND started > now() - interval '60 seconds'""",
                unique_queue,
            ),
        )
        await asyncio.sleep(0.025)
    assert peak == 3


async def test_rate_limit_refuses_a_claim_once_a_start_is_recorded(
    db_pool, db_params, unique_queue
):
    """The enforceable half of the rate limit: once ``started`` is written, the
    budget is respected exactly (budget 1, one start, next claim refused)."""
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 1, 60)""",
        unique_queue,
    )
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")

    conn = await db.connect(**db_params)
    try:
        first = await claim_once(conn, unique_queue)
        started = await conn.fetch(STMTS["run"], first["id"], first["run_epoch"])
        second = await claim_once(conn, unique_queue)
    finally:
        await conn.close()

    assert [r["state"] for r in started] == ["running"]
    assert second is None


@pytest.mark.xfail(
    strict=True,
    reason="BUG: rate_limit counts jorb.started, so a claimed-but-not-yet-started "
    "job does not consume the budget and the next claim is admitted",
)
async def test_rate_limit_budget_counts_claims_not_only_starts(
    db_pool, db_params, unique_queue
):
    """With ``rate_limit = 1``, the second claim must be refused.

    No concurrency at all here: two SEQUENTIAL claims on ONE connection are
    both admitted today, because nothing has reached 'running' yet.

    xfail is STRICT: the gap between the claim and the 'run' statement is
    unconditional, so an unexpected pass means admission started counting
    claims (or the claim started recording ``started`` itself).
    """
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 1, 60)""",
        unique_queue,
    )
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")
    await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob")

    conn = await db.connect(**db_params)
    try:
        await claim_once(conn, unique_queue)
        second = await claim_once(conn, unique_queue)
    finally:
        await conn.close()

    assert second is None


# ============================================================================
# 8. concurrent monitor sweeps
# ============================================================================


async def test_four_concurrent_timeout_sweeps_handle_a_job_exactly_once(
    db_pool, unique_queue
):
    """Several monitors may run: an overdue job must be retried ONCE.

    The sweep holds its row locks in a transaction and its updates are guarded
    on state='running', so ``error_count`` moves by exactly 1 no matter how
    many sweeps race. Without that, each sweep would count another failure and
    walk the job toward the dead-letter queue for a single timeout.
    """
    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.dxe_jobs.SlowJob",
        {"seconds": 30},
        {"on_timeout": "retry", "max_retries": 5, "initial_retry_delay": 0},
    )
    claimed = await claim_once(db_pool, unique_queue)
    assert claimed["id"] == job_id
    await db_pool.execute(STMTS["run"], job_id, claimed["run_epoch"])
    await db_pool.execute(
        "UPDATE jorb SET timeout_at = now() - interval '5 seconds' WHERE id = $1",
        job_id,
    )

    handled = await asyncio.gather(*[sweep_timed_out_jobs(db_pool) for _ in range(4)])
    assert sum(handled) == 1

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "queued"
    assert row["error_count"] == 1
    assert row["timeout_at"] is None
    assert row["run_epoch"] == 1  # a requeue never bumps the fence; the claim does
    assert await event_count(db_pool, job_id, "queued") == 1


async def test_four_concurrent_dead_worker_sweeps_requeue_a_job_exactly_once(
    db_pool, unique_queue
):
    """Concurrent dead-worker sweeps requeue an orphaned job exactly once."""
    worker_id = await db_pool.fetchval(
        """INSERT INTO jorb_worker (host, pid, queue, capabilities, version, last_seen)
           VALUES ('gone-host', 999, $1, '{test}', 'test',
                   now() - interval '5 minutes') RETURNING id""",
        unique_queue,
    )
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 2})
    claimed = await claim_once(db_pool, unique_queue, worker_id=worker_id)
    assert claimed["id"] == job_id
    await db_pool.execute(STMTS["run"], job_id, claimed["run_epoch"])

    requeued = await asyncio.gather(
        *[sweep_dead_workers(db_pool, liveness_grace_seconds=60) for _ in range(4)]
    )
    assert sum(requeued) == 1

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "queued"
    assert row["error_count"] == 0  # a reclaim is not a failure
    assert await event_count(db_pool, job_id, "queued") == 1
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "queued",
    ]
