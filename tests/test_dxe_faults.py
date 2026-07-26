"""Failure injection: what survives when the process or the database dies.

A durable execution engine is only worth the failures it has actually been
subjected to, so nothing here is simulated with mocks:

* the worker's PostgreSQL backends are terminated server-side mid-job;
* a real worker process group is SIGKILLed while a step is in flight (no
  signal handler runs, nothing deregisters, no terminal state is written);
* a job is abandoned in 'claimed' with no registry reference at all;
* every epoch-fenced statement is fired at a superseded epoch, table-driven,
  with a positive control at the current epoch so a statement that simply
  stopped working could never pass.

The invariant behind all of it: a job's completed work is recorded in the
database, and recovery resumes from that record instead of repeating it —
proved with the ``jorb_test_effect`` ledger (see ``tests/utils/faults.py``),
which counts what the code REALLY executed rather than what the checkpoint
table claims.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from pyjobby import db
from pyjobby.monitor import sweep_dead_workers, sweep_unregistered_claims
from pyjobby.pj import STMTS, Job

from .conftest import wait_for_job_state
from .utils.faults import (
    age_claim,
    age_worker_heartbeats,
    backend_pids,
    effect_counts,
    ensure_effects_table,
    kill_backends,
    new_backends,
    record_effect,
    sigkill_group,
    write_worker_config,
)
from .utils.processes import spawn, terminate, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ============================================================================
# job classes (resolved by in-process AND subprocess workers by dotted path)
# ============================================================================


class SlowResultJob(Job):
    """Runs long enough for its worker's database connection to be killed."""

    async def task(self, seconds: float = 2.0, marker: str = "ok") -> dict[str, object]:
        await asyncio.sleep(seconds)
        return {"marker": marker, "epoch": self.job["run_epoch"]}


class ResumableEffectJob(Job):
    """Two checkpointed steps; the second one never returns on attempt 1.

    Killing the worker inside step 2 leaves step 1 checkpointed. The recovery
    attempt (a later ``run_epoch``) must fast-forward step 1 — proved by the
    ledger, which must never gain a second 'first' row — and execute only the
    step that did not finish.
    """

    async def task(self, tag: str) -> dict[str, object]:
        first = await self.step("first", self._first, tag)
        second = await self.step("second", self._second, tag)
        return {"first": first, "second": second}

    async def _first(self, tag: str) -> dict[str, str]:
        await record_effect(self.s.cxn, tag, self.job["id"], "first")
        return {"stamp": "first-done"}

    async def _second(self, tag: str) -> dict[str, str]:
        if self.job["run_epoch"] == 1:
            await asyncio.sleep(600)  # the killed attempt never gets past here
        await record_effect(self.s.cxn, tag, self.job["id"], "second")
        return {"stamp": "second-done"}


# ============================================================================
# helpers
# ============================================================================

CLAIM_HOST = "faults-test-host"


async def enqueue(pool, queue: str, job_class: str, kwargs: dict | None = None) -> int:
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue)
           VALUES ($1, $2, $3) RETURNING id""",
        job_class,
        kwargs or {},
        queue,
    )
    return job_id


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


# ============================================================================
# 9. the worker's database connections are killed mid-job
# ============================================================================


@pytest.mark.slow
async def test_worker_reconnects_after_its_backends_are_killed_mid_job(
    live_worker, unique_queue, db_pool
):
    """Terminating a worker's backends must cost nothing but a reconnect.

    The job is mid-flight when both of the worker's connections are killed
    server-side (a failover, in effect). The worker has to notice on its next
    statement, rebuild the connection and every prepared statement, and then
    still record the SAME attempt's result — no retry, no duplicate execution
    — and go on claiming afterwards.
    """
    async with new_backends(db_pool) as worker_pids:
        system = await live_worker(heartbeat_interval=1.0)
    assert len(worker_pids) == 2  # the worker connection and its heartbeat

    worker_row = await db_pool.fetchrow(
        "SELECT id, last_seen FROM jorb_worker WHERE queue = $1", unique_queue
    )

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_faults.SlowResultJob",
        {"seconds": 3, "marker": "survivor"},
    )
    await wait_for_job_state(db_pool, job_id, ("running",))

    assert await kill_backends(db_pool, worker_pids) == 2

    # the SAME attempt still records its result once the worker reconnects
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
    assert row["result"] == {"marker": "survivor", "epoch": 1}
    assert row["run_epoch"] == 1
    assert row["run_count"] == 1
    assert row["error_count"] == 0
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "finished",
    ]

    # neither killed backend came back from the dead
    assert len(await backend_pids(db_pool) & set(worker_pids)) == 0
    assert system.cxn.is_closed() is False
    assert len(set(STMTS) - set(system.stmts)) == 0  # statements re-prepared

    # the heartbeat connection recovered too (liveness keeps being reported)
    await wait_until(
        lambda: db_pool.fetchval(
            "SELECT last_seen > $2 OR NULL FROM jorb_worker WHERE id = $1",
            worker_row["id"],
            worker_row["last_seen"],
        ),
        timeout=30,
        what="the worker heartbeat resumed on a new connection",
    )

    # ...and the worker keeps claiming on the new connection
    next_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 21})
    after = await wait_for_job_state(db_pool, next_id, ("finished",), timeout=30)
    assert after["result"] == {"doubled": 42}
    assert system.processed == 2


# ============================================================================
# 10. a real worker process is SIGKILLed mid-step
# ============================================================================


@pytest.mark.slow
async def test_sigkilled_worker_is_reclaimed_and_resumes_from_its_checkpoint(
    live_worker, unique_queue, db_pool, db_params, tmp_path
):
    """Hard-kill a worker inside step 2; recovery must not redo step 1.

    A real ``pj`` process group is SIGKILLed (no graceful shutdown, so the
    registry row stays 'alive' and the job stays 'running'), the monitor's
    dead-worker sweep requeues the orphan, and a fresh worker finishes it.
    The ledger proves the completed step was reused rather than re-executed:
    'first' must have run exactly once across BOTH attempts.
    """
    await ensure_effects_table(db_pool)
    config = write_worker_config(tmp_path, db_params)

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_faults.ResumableEffectJob",
        {"tag": unique_queue},
    )

    proc = spawn(
        "pj",
        "--config",
        str(config),
        "--queue",
        unique_queue,
        "--workers",
        "1",
        "--check-interval",
        "1",
    )
    try:
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb_step WHERE job_id = $1 AND name = 'first'", job_id
            ),
            timeout=60,
            what="the subprocess worker checkpointed step 1",
        )
        killed_worker = await db_pool.fetchrow(
            "SELECT id, pid FROM jorb_worker WHERE queue = $1", unique_queue
        )
        assert sigkill_group(proc) == -9
    finally:
        terminate(proc)

    # the hard kill left the job in-flight and the registry row 'alive'
    orphan = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert orphan["state"] == "running"
    assert orphan["run_epoch"] == 1
    assert orphan["claimed_by"] == killed_worker["id"]
    assert await effect_counts(db_pool, unique_queue) == {"first": 1}

    # the monitor reclaims it once the heartbeat goes stale, and not before
    assert await sweep_dead_workers(db_pool, liveness_grace_seconds=60) == 0
    assert await age_worker_heartbeats(db_pool, unique_queue, 300) == "UPDATE 1"
    assert await sweep_dead_workers(db_pool, liveness_grace_seconds=60) == 1

    requeued = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert requeued["state"] == "queued"
    # the reclaim fences the SIGKILLed attempt: were it somehow still alive,
    # it could no longer write a checkpoint over the recovery attempt
    assert requeued["run_epoch"] > orphan["run_epoch"]

    # a fresh worker resumes: step 1 fast-forwards, only step 2 executes
    await live_worker()
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=40)
    assert row["result"] == {
        "first": {"stamp": "first-done"},
        "second": {"stamp": "second-done"},
    }
    recovery_epoch = row["run_epoch"]
    assert recovery_epoch > requeued["run_epoch"]
    assert row["error_count"] == 0

    assert await effect_counts(db_pool, unique_queue) == {"first": 1, "second": 1}
    steps = await db_pool.fetch(
        """SELECT step_seq, name, error, run_epoch FROM jorb_step
           WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    assert [(s["step_seq"], s["name"], s["error"], s["run_epoch"]) for s in steps] == [
        # step 1 still carries the KILLED attempt's epoch: it was never
        # re-executed, it was replayed from the checkpoint
        (1, "first", None, orphan["run_epoch"]),
        (2, "second", None, recovery_epoch),  # only this one actually ran
    ]
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "running",
        "queued",
        "claimed",
        "running",
        "finished",
    ]


# ============================================================================
# 11. a worker dies between claiming and running
# ============================================================================


async def test_unregistered_claim_is_reclaimed_only_after_the_grace_period(
    live_worker, unique_queue, db_pool
):
    """A job claimed by a worker that never registered (and then died).

    Nothing heartbeats for it, so age is the only signal the monitor has: the
    claim must survive until the grace period elapses and be requeued (and
    then really executed) after it.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 6})
    claimed = await claim_once(db_pool, unique_queue, worker_id=None)
    assert claimed["id"] == job_id
    assert claimed["state"] == "claimed"
    assert claimed["claimed_by"] is None
    assert claimed["run_epoch"] == 1

    # fresh claim: untouched
    assert await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300) == 0
    # still inside the grace period: untouched
    assert await age_claim(db_pool, job_id, 60) == "UPDATE 1"
    assert await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300) == 0
    still = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert still["state"] == "claimed"

    # past the grace period: reclaimed
    assert await age_claim(db_pool, job_id, 600) == "UPDATE 1"
    assert await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300) == 1
    requeued = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert requeued["state"] == "queued"
    assert requeued["timeout_at"] is None
    # the requeue fences the abandoned claim; run_count still counts attempts
    assert requeued["run_epoch"] > claimed["run_epoch"]
    assert requeued["run_count"] == 1

    # and the recovered job actually runs
    await live_worker()
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"doubled": 12}
    assert row["run_epoch"] > requeued["run_epoch"]
    assert row["run_count"] == 2
    assert await history_events(db_pool, job_id) == [
        "enqueued",
        "claimed",
        "queued",
        "claimed",
        "running",
        "finished",
    ]


# ============================================================================
# 12. stale-execution writes: every fenced statement, table-driven
# ============================================================================

#: Statements a running attempt uses to change its job's row. Every one of
#: them carries ``AND run_epoch = $n``; this list is what makes that claim
#: testable rather than aspirational.
EPOCH_FENCED = ("run", "set-timeout", "finished", "retry", "crashed", "cancelled")

STALE_WRITE_CASES = (*EPOCH_FENCED, "record-step")


async def apply_fenced_statement(pool, name: str, job_id: int, epoch: int) -> int:
    """Run one epoch-fenced statement; return how many rows it wrote."""
    if name == "run":
        rows = await pool.fetch(STMTS[name], job_id, epoch)
        return len(rows)
    if name == "finished":
        rows = await pool.fetch(STMTS[name], job_id, {"wrote": name}, epoch)
        return len(rows)
    if name == "retry":
        rows = await pool.fetch(
            STMTS[name],
            job_id,
            datetime.timedelta(seconds=1),
            "stale error",
            "stale backtrace",
            epoch,
        )
        return len(rows)
    if name == "crashed":
        rows = await pool.fetch(
            STMTS[name], job_id, "stale error", "stale backtrace", epoch
        )
        return len(rows)
    if name == "cancelled":
        rows = await pool.fetch(STMTS[name], job_id, epoch)
        return len(rows)
    if name == "set-timeout":
        status = await pool.execute(
            STMTS[name], job_id, datetime.timedelta(seconds=60), epoch
        )
        return int(status.split()[-1])
    if name == "record-step":
        rows = await pool.fetch(
            STMTS[name], job_id, 1, "stale-step", {"v": 1}, None, epoch, db.utcnow()
        )
        return len(rows)
    raise AssertionError(f"unhandled statement {name}")


async def superseded_job(pool, queue: str) -> tuple[int, int, int]:
    """A job claimed twice: returns (job_id, stale epoch, current epoch).

    The epochs are only guaranteed to increase, not to be consecutive: the
    requeue between the two claims advances the token itself so the first
    attempt is fenced before the second one starts.
    """
    job_id = await enqueue(pool, queue, "tests.dxe_jobs.OkJob", {"x": 1})
    first = await claim_once(pool, queue)
    await db.requeue_job(pool, job_id, allowed_states=("claimed",), reset_errors=False)
    second = await claim_once(pool, queue)
    assert (first["id"], second["id"]) == (job_id, job_id)
    return job_id, first["run_epoch"], second["run_epoch"]


async def test_every_state_changing_statement_carries_the_fence():
    """A new worker statement must not be able to forget the fencing token."""
    assert [name for name in EPOCH_FENCED if "run_epoch = $" in STMTS[name]] == list(
        EPOCH_FENCED
    )
    assert "AND run_epoch = $6" in STMTS["record-step"]


@pytest.mark.parametrize("statement", STALE_WRITE_CASES)
async def test_stale_epoch_write_is_a_noop(db_pool, unique_queue, statement):
    """Every fenced statement writes NOTHING at a superseded epoch.

    Each case also fires the same statement at the CURRENT epoch, so a
    statement that silently stopped working (a renamed column, a broken state
    guard) cannot masquerade as good fencing.
    """
    job_id, stale, current = await superseded_job(db_pool, unique_queue)
    assert stale < current

    assert await apply_fenced_statement(db_pool, statement, job_id, stale) == 0

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "claimed"  # the current attempt still owns the row
    assert row["run_epoch"] == current
    assert row["result"] is None
    assert row["timeout_at"] is None
    assert row["error_count"] == 0
    assert row["finished"] is None
    steps = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
    )
    assert steps == 0

    # positive control: the live attempt's write lands
    assert await apply_fenced_statement(db_pool, statement, job_id, current) == 1


async def test_stale_step_cannot_overwrite_the_live_attempts_checkpoint(
    db_pool, unique_queue
):
    """A superseded attempt may not clobber a checkpoint the winner recorded.

    ``record-step`` upserts on (job_id, step_seq), so the fence is the only
    thing standing between a zombie execution and a corrupted checkpoint."""
    job_id, stale, current = await superseded_job(db_pool, unique_queue)

    live = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "work",
        {"by": "current"},
        None,
        current,
        db.utcnow(),
    )
    assert [r["step_seq"] for r in live] == [1]

    zombie = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "work",
        {"by": "stale"},
        "stale failure",
        stale,
        db.utcnow(),
    )
    assert zombie == []

    steps = await db_pool.fetch(
        "SELECT step_seq, name, output, error, run_epoch FROM jorb_step WHERE job_id=$1",
        job_id,
    )
    assert [
        (s["step_seq"], s["name"], s["output"], s["error"], s["run_epoch"])
        for s in steps
    ] == [(1, "work", {"by": "current"}, None, current)]


async def test_stale_reschedule_cannot_requeue_the_live_attempt(db_pool, unique_queue):
    """A stale attempt's reschedule must not disturb the running attempt.

    reschedule carries the same epoch fence and state guard as every other
    state-changing write, so the superseded attempt's requeue simply does
    not apply.
    """
    job_id, stale, current = await superseded_job(db_pool, unique_queue)
    started = await db_pool.fetch(STMTS["run"], job_id, current)
    assert [r["state"] for r in started] == ["running"]

    # the zombie from the superseded attempt tries to reschedule itself
    applied = await db_pool.fetch(
        STMTS["reschedule"], job_id, datetime.timedelta(seconds=60), stale
    )
    assert applied == []

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "running"
    assert row["run_epoch"] == current


async def test_live_completion_survives_a_stale_reschedule(db_pool, unique_queue):
    """The blast radius the fence closes: the winner's result is kept.

    Before the fence, a stale reschedule left the row 'queued', which fenced
    the CURRENT attempt's completion out by its own state guard -- the result
    was dropped and the job ran a second time. Now the stale reschedule is a
    no-op, so the live attempt still finishes normally.
    """
    job_id, stale, current = await superseded_job(db_pool, unique_queue)
    await db_pool.fetch(STMTS["run"], job_id, current)
    await db_pool.fetch(
        STMTS["reschedule"], job_id, datetime.timedelta(seconds=60), stale
    )

    completion = await db_pool.fetch(STMTS["finished"], job_id, {"real": True}, current)
    assert [r["state"] for r in completion] == ["finished"]

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "finished"
    assert row["result"] == {"real": True}


async def test_live_attempt_can_still_reschedule_itself(db_pool, unique_queue):
    """The fence blocks stale attempts only -- the current one still works."""
    job_id, _stale, current = await superseded_job(db_pool, unique_queue)
    await db_pool.fetch(STMTS["run"], job_id, current)

    applied = await db_pool.fetch(
        STMTS["reschedule"], job_id, datetime.timedelta(seconds=60), current
    )
    assert [r["id"] for r in applied] == [job_id]

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "queued"
    assert row["run_after"] > row["started"]
