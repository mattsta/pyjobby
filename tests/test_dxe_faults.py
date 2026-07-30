"""Failure injection: what survives when the process or the database dies.

A durable execution engine is only worth the failures it has actually been
subjected to, so nothing here is simulated with mocks:

* the worker's PostgreSQL backends are terminated server-side mid-job;
* a real worker process group is SIGKILLed while a step is in flight (no
  signal handler runs, nothing deregisters, no terminal state is written);
* a job is abandoned in 'claimed' with no registry reference at all;
* the same worker is SIGKILLed in the window between a job's database write
  and its checkpoint, once with ``step()`` and once with ``transaction()``,
  which is where at-least-once and exactly-once actually come apart;
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

from pyjobby import db, dxe
from pyjobby.monitor import (
    handle_timed_out_job,
    sweep_dead_workers,
    sweep_stuck_claims,
)
from pyjobby.pj import STMTS, Job
from pyjobby.procs import spawn, terminate, wait_until

from .conftest import wait_for_job_state
from .utils.dxe import connection_bound_job
from .utils.faults import (
    age_claim,
    age_worker_heartbeats,
    backend_pids,
    effect_counts,
    ensure_effects_table,
    kill_backends,
    new_backends,
    record_effect,
    record_effect_out_of_band,
    sigkill_group,
    wait_until_blocked_on_a_transaction,
    write_worker_config,
)

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


class UncommittedTransactionJob(Job):
    """A ``transaction()`` step killed between its write and its commit.

    The write happens on the connection the primitive hands in, so it is
    still uncommitted when the process dies: the recovery attempt must find
    no trace of it and produce the effect EXACTLY once overall.

    ``attempt`` is announced on a separate connection (committed
    immediately) so the test can see that the write is staged and kill at
    precisely that moment — and so a re-execution is countable even though
    the transactional write it accompanies was rolled back.
    """

    async def task(self, tag: str) -> dict[str, str]:
        result: dict[str, str] = await self.transaction("write", self._write, tag)
        return result

    async def _write(self, conn, tag: str) -> dict[str, str]:
        await record_effect(conn, tag, self.job["id"], "write")
        await record_effect_out_of_band(self.s.dsn, tag, self.job["id"], "attempt")
        if self.job["run_epoch"] == 1:
            await asyncio.sleep(600)  # killed here: the transaction never commits
        return {"stamp": "written"}


class AtLeastOnceWriteJob(Job):
    """The SAME shape written with ``step()`` — the control for the contrast.

    Here the write commits on its own (the worker connection is in
    autocommit) and the checkpoint would commit afterwards, so a kill in
    between leaves the effect behind and the recovery attempt performs it a
    second time. That duplicate is not a bug in ``step()``; it is what
    at-least-once means, and it is why ``transaction()`` exists.
    """

    async def task(self, tag: str) -> dict[str, str]:
        result: dict[str, str] = await self.step("write", self._write, tag)
        return result

    async def _write(self, tag: str) -> dict[str, str]:
        await record_effect(self.s.cxn, tag, self.job["id"], "write")
        await record_effect_out_of_band(self.s.dsn, tag, self.job["id"], "attempt")
        if self.job["run_epoch"] == 1:
            await asyncio.sleep(600)  # killed here: the checkpoint never lands
        return {"stamp": "written"}


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


async def claim_once(
    conn, queue: str, worker_id: int | None = None, app_version: str | None = None
):
    """Claim through the REAL claim statement (bumping run_epoch)."""
    return await conn.fetchrow(
        STMTS["claim"], 4242, CLAIM_HOST, queue, ["test"], 1000, worker_id, app_version
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
    # (the result proves it ran at epoch 1; the terminal write then advanced
    # the row's fence one past the attempt)
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
    assert row["result"] == {"marker": "survivor", "epoch": 1}
    assert row["run_epoch"] == 2
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
    # the terminal write advances the fence one past the attempt that ran,
    # so the recovery attempt's own epoch is the row's final epoch minus one
    recovery_epoch = row["run_epoch"] - 1
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
# 10b. the window between the effect and the checkpoint: step vs transaction
# ============================================================================

#: (job class, effects visible right after the kill, effects when it is done).
#: The two rows are the same fault injected into the same code shape, so the
#: only thing that differs is the primitive — and with it, how many times the
#: application write really happened. Pinning BOTH is what stops the two
#: primitives being "simplified" back into one.
KILL_WINDOW_CASES = (
    pytest.param(
        "tests.test_dxe_faults.UncommittedTransactionJob",
        {"attempt": 1},  # the write is staged, invisible, and never commits
        {"attempt": 2, "write": 1},  # exactly once across both attempts
        id="transaction-is-exactly-once",
    ),
    pytest.param(
        "tests.test_dxe_faults.AtLeastOnceWriteJob",
        {"attempt": 1, "write": 1},  # the write already committed on its own
        {"attempt": 2, "write": 2},  # ...so recovery performs it a second time
        id="step-is-at-least-once",
    ),
)


@pytest.mark.slow
@pytest.mark.parametrize("job_class,after_kill,at_the_end", KILL_WINDOW_CASES)
async def test_kill_between_the_write_and_the_checkpoint(
    live_worker,
    unique_queue,
    db_pool,
    db_params,
    tmp_path,
    job_class,
    after_kill,
    at_the_end,
):
    """SIGKILL a real worker in the window this feature exists to close.

    A ``pj`` process is hard-killed while the job's database write has
    happened but its checkpoint has not. With ``step()`` the write is
    already committed, so the recovery attempt redoes it — at-least-once.
    With ``transaction()`` the write and the checkpoint are one commit that
    never happened, so the recovery attempt is the only execution that
    counts — exactly-once. The ledger counts what really executed; the
    checkpoint table is not consulted for the claim.
    """
    await ensure_effects_table(db_pool)
    config = write_worker_config(tmp_path, db_params)

    job_id = await enqueue(db_pool, unique_queue, job_class, {"tag": unique_queue})

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
        # the out-of-band marker says the write is done and the checkpoint
        # is not: this is the instant the whole feature is about
        await wait_until(
            lambda: db_pool.fetchval(
                """SELECT 1 FROM jorb_test_effect
                   WHERE tag = $1 AND label = 'attempt'""",
                unique_queue,
            ),
            timeout=60,
            what="the worker staged its write inside the target window",
        )
        assert sigkill_group(proc) == -9
    finally:
        terminate(proc)

    # no checkpoint was recorded on either side of the contrast — the kill
    # really landed in the window, rather than before or after it
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )
    assert await effect_counts(db_pool, unique_queue) == after_kill

    orphan = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert orphan["state"] == "running"
    assert orphan["run_epoch"] == 1

    # the monitor reclaims the orphan and a fresh worker runs it to the end
    assert await age_worker_heartbeats(db_pool, unique_queue, 300) == "UPDATE 1"
    assert await sweep_dead_workers(db_pool, liveness_grace_seconds=60) == 1

    await live_worker()
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=40)
    assert row["result"] == {"stamp": "written"}
    assert row["error_count"] == 0
    assert row["run_epoch"] > orphan["run_epoch"]

    assert await effect_counts(db_pool, unique_queue) == at_the_end
    steps = await db_pool.fetch(
        """SELECT step_seq, name, output, error, run_epoch FROM jorb_step
           WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    assert [(s["step_seq"], s["name"], s["error"]) for s in steps] == [
        (1, "write", None)
    ]
    # the checkpoint carries the epoch of the attempt that recorded it; the
    # terminal write then advanced the row's fence one past it
    assert steps[0]["run_epoch"] == row["run_epoch"] - 1


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
    assert await sweep_stuck_claims(db_pool, claimed_grace_seconds=300) == 0
    # still inside the grace period: untouched
    assert await age_claim(db_pool, job_id, 60) == "UPDATE 1"
    assert await sweep_stuck_claims(db_pool, claimed_grace_seconds=300) == 0
    still = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert still["state"] == "claimed"

    # past the grace period: reclaimed
    assert await age_claim(db_pool, job_id, 600) == "UPDATE 1"
    assert await sweep_stuck_claims(db_pool, claimed_grace_seconds=300) == 1
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
EPOCH_FENCED = ("run", "finished", "retry", "crashed", "cancelled")

STALE_WRITE_CASES = (
    *EPOCH_FENCED,
    "record-step",
    "set-event",
    "send",
    "recv",
    "stream-append",
)


async def apply_fenced_statement(pool, name: str, job_id: int, epoch: int) -> int:
    """Run one epoch-fenced statement; return how many rows it wrote."""
    if name == "run":
        # the deadline rides in the same statement (there is no separate
        # set-timeout write any more), so the stale case also proves a
        # zombie cannot move a deadline
        rows = await pool.fetch(
            STMTS[name], job_id, epoch, datetime.timedelta(seconds=60)
        )
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
    if name == "record-step":
        rows = await pool.fetch(
            STMTS[name], job_id, 1, "stale-step", {"v": 1}, None, epoch, db.utcnow()
        )
        return len(rows)
    if name == "set-event":
        rows = await pool.fetch(STMTS[name], job_id, "stale-key", {"v": 1}, epoch)
        return len(rows)
    if name == "send":
        # fenced on the SENDER, so the job sends to itself: one row is enough
        # to prove the write happened, and self-delivery keeps the case from
        # needing a second job whose own epoch would confuse the assertion.
        rows = await pool.fetch(
            STMTS[name], job_id, "stale-topic", {"v": 1}, job_id, epoch
        )
        return len(rows)
    if name == "recv":
        # a pending message must exist so the live control has something to
        # consume; the stale attempt must leave it untouched
        await pool.execute(
            "INSERT INTO jorb_mailbox (dest_job_id, topic, message)"
            " VALUES ($1, $2, $3)",
            job_id,
            "fence",
            {"v": 1},
        )
        row = (
            await pool.fetch(
                STMTS[name], job_id, 1, "fence", "dxe.recv:fence", epoch, db.utcnow()
            )
        )[0]
        return int(row["consumed"])
    if name == "stream-append":
        # Always returns exactly one row (fenced / already_closed / seq), so
        # "did it write?" is the fence column and not the row count -- the
        # statement has to be able to say "superseded" and "you closed this
        # stream" separately, and neither can be an empty result.
        row = await pool.fetchrow(STMTS[name], job_id, "fence", {"v": 1}, False, epoch)
        assert not row["already_closed"]
        return int(row["fenced"])
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


#: The only ``UPDATE jorb`` statements that MAY omit the run_epoch fence,
#: each with the reason it is safe. They wake DOWNSTREAM waiters, keyed on
#: another job's terminal state (finished / all-group-members-finished), not
#: on the running attempt's identity -- level-triggered exactly like the
#: monitor's stranded-waiter sweep, and correct whichever worker fires them.
#: A NEW entry here is a deliberate assertion that a jorb write needs no
#: fence; anything else that writes jorb must carry one.
FENCE_EXEMPT_JORB_WRITES = {
    "enqueue-next-self-finished": "wakes waiters on a finished upstream job",
    "enqueue-next-if-peer-group-is-finished": "wakes waiters on a finished group",
}


async def test_every_state_changing_statement_carries_the_fence():
    """A new worker statement must not be able to forget the fencing token.

    Reflective over STMTS rather than a hand-kept list: every statement that
    UPDATEs the jorb table (the \\b excludes jorb_worker/jorb_step/... which
    have no run_epoch) must carry ``run_epoch = $n`` unless it is one of the
    explicitly justified waiter-wake writes. A statement added without a
    fence and without an exemption fails here, at the point it is written,
    instead of surfacing as a zombie overwriting a live attempt in
    production.
    """
    import re

    updates_jorb = re.compile(r"\bUPDATE\s+jorb\b", re.IGNORECASE)
    unfenced = {
        name
        for name, sql in STMTS.items()
        if updates_jorb.search(sql) and "run_epoch = $" not in sql
    }
    assert unfenced <= set(FENCE_EXEMPT_JORB_WRITES), (
        f"jorb-writing statements with no fence and no exemption: "
        f"{sorted(unfenced - set(FENCE_EXEMPT_JORB_WRITES))}"
    )
    # the historical hand-kept set stays covered by the reflective sweep
    assert set(EPOCH_FENCED) <= {
        name for name, sql in STMTS.items() if "run_epoch = $" in sql
    }

    # The DXE writes that touch child tables, not jorb, so the sweep above
    # cannot see them -- each was at some point the LAST unfenced durable
    # write, and each fence closed a specific zombie hazard.
    assert "AND run_epoch = $6" in STMTS["record-step"]
    # set-event / send: a superseded worker could overwrite a live attempt's
    # published events, or deliver a mailbox message and only THEN raise on
    # its own checkpoint -- the effect escaping while its record was refused.
    assert "run_epoch = $4" in STMTS["set-event"]
    assert "run_epoch = $5" in STMTS["send"]
    # recv: a superseded execution could consume (and fail to checkpoint) a
    # message the live attempt was entitled to -- eaten by a zombie.
    assert "run_epoch = $5" in STMTS["recv"]
    # stream-append: a superseded execution's rows would interleave with the
    # live attempt's in a stream whose reader cannot tell the two apart.
    assert "run_epoch = $5" in STMTS["stream-append"]

    # ...and each of those fences LOCKS the job row rather than reading it.
    # A snapshot EXISTS is passed by a zombie whose statement began before the
    # requeue committed, so the write lands after the supersession -- see
    # SECTION 14, which drives that interleave for real, as two real
    # transactions, for the three statements a caller can fire directly
    # (record-step, set-event, stream-append). `send`, `recv` and the
    # compaction fence are covered HERE, reflectively: their locking clause is
    # asserted rather than raced, because driving each of them to the same
    # deterministic block needs a second job, a pending message, or a turn
    # boundary, and the clause is the whole of what the interleave proves.
    # The lock is what makes "the fence matched nothing" a fact about commit
    # time.
    for name in ("record-step", "set-event", "send", "recv"):
        assert "FOR SHARE" in STMTS[name] or "FOR UPDATE" in STMTS[name], (
            f"STMTS[{name!r}] fences on run_epoch without locking the job row: "
            f"a write already in flight when the requeue commits passes the "
            f"snapshot test and commits anyway"
        )
    assert "FOR SHARE" in STMTS["stream-append"]
    # Compaction fences with an UPDATE, which takes the EXCLUSIVE lock -- the
    # deliberate exception (see dxe.COMPACT_FENCE_SQL): it has to write the
    # `awaited` latch anyway, and two concurrent compactions each holding a
    # share lock and then reaching for the exclusive one is a lock-upgrade
    # deadlock. The DELETE it fences is a SEPARATE statement in the same
    # transaction, and carries no epoch of its own on purpose: a fresh
    # snapshot is the point, and it is unreachable except behind the fence.
    assert "UPDATE jorb" in STMTS["compact-fence"]
    assert "run_epoch = $2" in STMTS["compact-fence"]
    assert "run_epoch" not in STMTS["compact-steps"]
    assert "jorb_step" in STMTS["compact-steps"]


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


async def test_a_superseded_attempt_cannot_compact_a_live_ones_checkpoints(
    db_pool, unique_queue
):
    """Compaction DELETES checkpoints, so it is the fence's sharpest test.

    It cannot join STALE_WRITE_CASES because the fence is a statement of its
    own: compaction is a PAIR (`compact-fence`, then `compact-steps` under a
    fresh snapshot in the same transaction -- see dxe.COMPACT_FENCE_SQL), and
    the whole refusal has to happen in the first of them. A stale epoch must
    match nothing there, so the delete is never reached and the live attempt's
    log is untouched.
    """
    job_id, stale, current = await superseded_job(db_pool, unique_queue)

    for seq in (1, 2, 3):
        await db_pool.fetch(
            STMTS["record-step"],
            job_id,
            seq,
            f"work-{seq}",
            {"by": "current"},
            None,
            current,
            db.utcnow(),
        )

    assert await db_pool.fetch(STMTS["compact-fence"], job_id, stale) == [], (
        "a stale epoch must not own the job"
    )
    surviving = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
    )
    assert surviving == 3, "the zombie deleted the live attempt's checkpoints"

    # positive control: the live attempt's own compaction does the work
    assert len(await db_pool.fetch(STMTS["compact-fence"], job_id, current)) == 1
    assert await db_pool.fetchval(STMTS["compact-steps"], job_id) == 3
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )


async def test_compact_refuses_while_a_previous_attempts_log_is_unreplayed(
    db_pool, db_params, unique_queue
):
    """Compacting mid-replay would silently re-execute completed work.

    The guard is on the Job object rather than in SQL because it is a
    statement about THIS attempt's progress, which the database cannot see:
    the rows exist either way, and what matters is whether this execution has
    caught up to them yet.

    On a real CONNECTION, not the pool: compaction is a fenced UPDATE and a
    DELETE in ONE transaction (see dxe.COMPACT_FENCE_SQL), and a transaction
    is scoped to a connection.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    for seq in (1, 2, 3):
        await db_pool.fetch(
            STMTS["record-step"],
            job_id,
            seq,
            f"work-{seq}",
            {"n": seq},
            None,
            epoch,
            db.utcnow(),
        )

    caller = await db.connect(**db_params)
    try:
        job = await connection_bound_job(caller, claimed, epoch)

        # Sequence is at 0; three steps are recorded. Nothing has been replayed.
        assert await job.compact() is False
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
            )
            == 3
        )

        # Catch up to the last recorded step, and it becomes safe.
        job._dxe_seq = 3
        assert await job.compact() is True
    finally:
        await caller.close()

    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )
    assert job._dxe_seq == 0, "the sequence restarts, or the next step collides"


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


async def test_an_error_record_never_clobbers_a_committed_success(
    db_pool, unique_queue
):
    """The in-doubt-commit hole: transaction() commits its application write
    AND the success checkpoint together, but if the COMMIT ack is lost the
    client cannot tell it committed, runs its error path, and reconnects to
    write an error at the same seq. That error must NOT overwrite the
    durable success — otherwise the retry re-runs fn and re-delivers an
    exactly-once send(). RECORD_STEP_SQL's DO UPDATE guard enforces it.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]

    # the committed success (as transaction() writes it)
    ok = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "send",
        {"delivered": 1},
        None,
        epoch,
        db.utcnow(),
    )
    assert [r["step_seq"] for r in ok] == [1]

    # the stale error write from the lost-commit fallback, SAME seq + epoch.
    # It writes a row (so _dxe_record does NOT misread it as a stale epoch),
    # but the CASE preserves the committed success unchanged.
    clobber = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "send",
        None,
        "ConnectionError: gone",
        epoch,
        db.utcnow(),
    )
    assert [r["step_seq"] for r in clobber] == [1]

    row = await db_pool.fetchrow(
        "SELECT output, error FROM jorb_step WHERE job_id=$1 AND step_seq=1",
        job_id,
    )
    assert row["output"] == {"delivered": 1}
    assert row["error"] is None  # the success stands; replay fast-forwards

    # positive controls: an error CAN be re-recorded, and a success replaces
    # an existing error (the retry that finally succeeded)
    await db_pool.execute("DELETE FROM jorb_step WHERE job_id=$1", job_id)
    await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "s",
        None,
        "first failure",
        epoch,
        db.utcnow(),
    )
    reerr = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "s",
        None,
        "second failure",
        epoch,
        db.utcnow(),
    )
    assert [r["step_seq"] for r in reerr] == [1]  # error -> error updates
    win = await db_pool.fetch(
        STMTS["record-step"],
        job_id,
        1,
        "s",
        {"ok": 1},
        None,
        epoch,
        db.utcnow(),
    )
    assert [r["step_seq"] for r in win] == [1]  # error -> success updates
    final = await db_pool.fetchrow(
        "SELECT output, error FROM jorb_step WHERE job_id=$1 AND step_seq=1",
        job_id,
    )
    assert final["output"] == {"ok": 1} and final["error"] is None


async def test_stale_reschedule_cannot_requeue_the_live_attempt(db_pool, unique_queue):
    """A stale attempt's reschedule must not disturb the running attempt.

    reschedule carries the same epoch fence and state guard as every other
    state-changing write, so the superseded attempt's requeue simply does
    not apply.
    """
    job_id, stale, current = await superseded_job(db_pool, unique_queue)
    started = await db_pool.fetch(STMTS["run"], job_id, current, None)
    assert [r["id"] for r in started] == [job_id]

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
    await db_pool.fetch(STMTS["run"], job_id, current, None)
    await db_pool.fetch(
        STMTS["reschedule"], job_id, datetime.timedelta(seconds=60), stale
    )

    completion = await db_pool.fetch(STMTS["finished"], job_id, {"real": True}, current)
    assert [r["id"] for r in completion] == [job_id]

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "finished"
    assert row["result"] == {"real": True}


async def test_live_attempt_can_still_reschedule_itself(db_pool, unique_queue):
    """The fence blocks stale attempts only -- the current one still works."""
    job_id, _stale, current = await superseded_job(db_pool, unique_queue)
    await db_pool.fetch(STMTS["run"], job_id, current, None)

    applied = await db_pool.fetch(
        STMTS["reschedule"], job_id, datetime.timedelta(seconds=60), current
    )
    assert [r["id"] for r in applied] == [job_id]

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "queued"
    assert row["run_after"] > row["started"]


async def test_compaction_drops_the_notification_latch(
    db_pool, db_params, unique_queue
):
    """compact() clears jorb.awaited alongside the checkpoint log.

    The latch's design ("set once, dies with the row") assumes rows die; a
    compacting job is exactly the one that never does, so one wait ever
    would make every future publish a NOTIFY-bearing commit forever. An
    ACTIVE waiter re-arms from its fallback poll (client._poll_until), so
    clearing costs at most one fallback interval of latency, once per turn.

    The clearing IS the fence now: `compact-fence` is the UPDATE that writes
    it, so a compaction that reports success has necessarily cleared the latch
    and one that reports supersession has necessarily left it alone.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    await db_pool.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)

    caller = await db.connect(**db_params)
    try:
        job = await connection_bound_job(caller, claimed)
        assert await job.compact() is True

        assert (
            await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id)
            is False
        )

        # a zombie's compact must not drop a live attempt's latch
        await db_pool.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)
        zombie = await connection_bound_job(
            caller, claimed, epoch=claimed["run_epoch"] - 1
        )
        with pytest.raises(dxe.StaleExecutionError):
            await zombie.compact()
    finally:
        await caller.close()

    assert (
        await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id) is True
    )


# ============================================================================
# 13. the mailbox has no crash window and no zombie window
# ============================================================================


async def test_recv_consume_and_checkpoint_are_one_statement(db_pool, unique_queue):
    """Replaying recv's statement at the same seq fast-forwards; it never
    consumes a second message.

    Consume and checkpoint commit together in one statement, so re-executing
    it — a reconnect replaying a statement whose reply was lost, or a retry
    reaching the same call site — finds the recorded answer instead of
    eating the next message. This is the property that makes a worker crash
    unable to lose mail: there is no state in which a message is consumed
    but unrecorded.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    for n in (1, 2):
        await db_pool.execute(
            "INSERT INTO jorb_mailbox (dest_job_id, message) VALUES ($1, $2)",
            job_id,
            {"n": n},
        )

    first = (
        await db_pool.fetch(
            STMTS["recv"], job_id, 1, None, "dxe.recv:", epoch, db.utcnow()
        )
    )[0]
    assert first["fenced"] == 1
    assert first["consumed"] == 1
    assert first["message"] == {"n": 1}

    replay = (
        await db_pool.fetch(
            STMTS["recv"], job_id, 1, None, "dxe.recv:", epoch, db.utcnow()
        )
    )[0]
    assert replay["replayed"] == 1
    assert replay["prior_output"] == {"n": 1}
    assert replay["consumed"] == 0

    # the second message is still pending — the replay ate nothing
    pending = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_mailbox"
        " WHERE dest_job_id = $1 AND consumed_at IS NULL",
        job_id,
    )
    assert pending == 1

    step = await db_pool.fetchrow(
        "SELECT name, output FROM jorb_step WHERE job_id = $1 AND step_seq = 1",
        job_id,
    )
    assert step["name"] == "dxe.recv:"
    assert step["output"] == {"n": 1}


async def test_send_and_its_checkpoint_commit_together(db_pool, unique_queue):
    """send() is exactly-once: delivery and checkpoint are one commit, and a
    replay of the same call site fast-forwards instead of re-sending."""
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)

    async with db_pool.acquire() as conn:
        job = await connection_bound_job(conn, claimed)
        await job.send(job_id, {"hello": 1}, topic="t")

        delivered = await conn.fetch(
            "SELECT id, message FROM jorb_mailbox WHERE dest_job_id = $1", job_id
        )
        assert [r["message"] for r in delivered] == [{"hello": 1}]
        step = await conn.fetchrow(
            "SELECT name, output FROM jorb_step WHERE job_id = $1", job_id
        )
        assert step["name"] == f"dxe.send:{job_id}:t"
        assert step["output"] == delivered[0]["id"]

        # a retried attempt reaches the same call site: the recorded
        # checkpoint answers and no second message is delivered
        retried = await connection_bound_job(conn, claimed)
        await retried.send(job_id, {"hello": 1}, topic="t")
        count = await conn.fetchval(
            "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", job_id
        )
        assert count == 1


async def test_superseded_send_delivers_nothing_and_records_nothing(
    db_pool, unique_queue
):
    """A zombie's send is refused ATOMICALLY: no mailbox row escapes and no
    checkpoint claims one did — the rollback takes both."""
    job_id, stale, _current = await superseded_job(db_pool, unique_queue)
    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

    async with db_pool.acquire() as conn:
        zombie = await connection_bound_job(conn, row, epoch=stale)
        with pytest.raises(dxe.StaleExecutionError):
            await zombie.send(job_id, {"from": "zombie"}, topic="t")

    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", job_id
        )
        == 0
    )
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )


async def test_dead_lettering_fences_the_execution_it_abandons(db_pool, unique_queue):
    """The monitor's dead-letter write bumps run_epoch, so the timed-out
    execution — possibly still alive in an unstoppable thread — can no
    longer write checkpoints, events, or mail for a job the platform has
    given up on. (Its retry sibling always did this; the dead-letter path
    is the abandonment with the longest-lived zombie, since nothing will
    ever reclaim the row and re-fence it.)"""
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    started = await db_pool.fetch(STMTS["run"], job_id, epoch, None)
    assert [r["id"] for r in started] == [job_id]

    await handle_timed_out_job(
        db_pool, job_id, "tests.dxe_jobs.OkJob", {"on_timeout": "fail"}, 0
    )

    row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "crashed"
    assert row["run_epoch"] > epoch

    # the abandoned execution's epoch-only-guarded writes are all refused
    assert (
        await db_pool.fetch(STMTS["set-event"], job_id, "zombie", {"v": 1}, epoch) == []
    )
    assert (
        await db_pool.fetch(
            STMTS["record-step"],
            job_id,
            1,
            "zombie-step",
            {"v": 1},
            None,
            epoch,
            db.utcnow(),
        )
        == []
    )


# ============================================================================
# 14. every durable write's fence is a LOCK, not a snapshot read
# ============================================================================
#
# An unlocked `EXISTS (SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $n)` is
# evaluated against the writing statement's OWN snapshot, so a requeue already
# in flight but not yet committed is invisible to it: the zombie reads the old
# epoch, passes the test, and commits its write AFTER the supersession. It was
# reproduced for the stream append, for the step checkpoint and for the event.
#
# `FOR SHARE` closes it because the epoch bump is an UPDATE and holds the row's
# exclusive lock: the write BLOCKS, and when the requeue commits, READ COMMITTED
# re-evaluates the clause against the new row version (EvalPlanQual) and finds
# the bumped epoch. Each case below drives that as TWO REAL TRANSACTIONS, made
# deterministic by waiting until the writer is provably stuck on the holder's
# lock -- so the test pins the ordering it means rather than whichever one the
# box happened to produce.


def _fires_record_step(conn, job_id: int, epoch: int):
    return conn.fetch(
        dxe.RECORD_STEP_SQL,
        job_id,
        1,
        "zombie-step",
        {"i": 0},
        None,
        epoch,
        db.utcnow(),
    )


def _fires_set_event(conn, job_id: int, epoch: int):
    return conn.fetch(dxe.SET_EVENT_SQL, job_id, "zombie-key", {"i": 0}, epoch)


def _fires_stream_append(conn, job_id: int, epoch: int):
    return conn.fetch(dxe.STREAM_APPEND_SQL, job_id, "rows", {"i": 0}, False, epoch)


#: (statement fired at the stale epoch, the rows it would have left behind,
#: the caller-level primitive that must report the refusal as
#: StaleExecutionError). The stream case's statement always returns one row
#: -- it has to say "superseded" and "already closed" separately -- so its
#: refusal is read off the `fenced` column instead of the row count, which is
#: why `wrote` is a callable rather than ``len``.
LOCKED_FENCE_CASES = {
    "record-step": (
        _fires_record_step,
        lambda rows: len(rows),
        "SELECT count(*) FROM jorb_step WHERE job_id = $1",
        lambda job: job.step("zombie-step", _returns_a_value),
    ),
    "set-event": (
        _fires_set_event,
        lambda rows: len(rows),
        "SELECT count(*) FROM jorb_event WHERE job_id = $1",
        lambda job: job.set_event("zombie-key", {"i": 0}),
    ),
    "stream-append": (
        _fires_stream_append,
        lambda rows: int(rows[0]["fenced"]),
        "SELECT count(*) FROM jorb_stream WHERE job_id = $1",
        lambda job: job.stream_write("rows", {"i": 0}),
    ),
}


async def _returns_a_value() -> dict[str, int]:
    return {"i": 0}


@pytest.mark.parametrize("statement", sorted(LOCKED_FENCE_CASES))
async def test_a_zombies_write_cannot_commit_after_the_requeue_that_fenced_it(
    db_pool, db_params, unique_queue, statement
):
    """The window an unlocked epoch test leaves open, driven as two real
    transactions, for every durable write that has one.

    The blast radius differs per statement and none of it stays inside the
    zombie: a stream row a reader cannot tell from the live attempt's output;
    a step checkpoint that makes the live attempt fast-forward over work it
    never did (or clobbers the checkpoint it did write); an event that other
    jobs and every client read.
    """
    fire, wrote, survivors, primitive = LOCKED_FENCE_CASES[statement]

    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    assert [
        r["id"] for r in await db_pool.fetch(STMTS["run"], job_id, epoch, None)
    ] == [job_id]

    holder = await db.connect(**db_params)
    zombie = await db.connect(**db_params)
    try:
        # the requeue starts and takes the row lock, but does NOT commit
        requeue = holder.transaction()
        await requeue.start()
        assert (
            await holder.fetchval(
                db.build_requeue_sql(("claimed", "running")),
                job_id,
                datetime.timedelta(0),
                False,
            )
            == job_id
        )

        # ...and the abandoned execution tries to write at its old epoch
        writing = asyncio.create_task(fire(zombie, job_id, epoch))
        await wait_until_blocked_on_a_transaction(db_pool)
        assert not writing.done(), "the write must WAIT for the requeue, not race it"

        await requeue.commit()
        rows = await asyncio.wait_for(writing, timeout=20)
    finally:
        await zombie.close()
        await holder.close()

    assert wrote(rows) == 0, "the zombie must observe the BUMPED epoch"
    assert await db_pool.fetchval(survivors, job_id) == 0, (
        "a superseded execution's write reached a table a live attempt owns"
    )

    # and the caller sees the refusal as StaleExecutionError, not as silence.
    # On a real CONNECTION, not the pool: these primitives run through
    # transaction(), whose scope is the worker's own connection -- handed a
    # pool they would raise AttributeError, and transaction()'s error path
    # would convert that into a StaleExecutionError this assertion could not
    # tell from the real one.
    live = await db_pool.fetchval("SELECT run_epoch FROM jorb WHERE id = $1", job_id)
    assert live > epoch
    caller = await db.connect(**db_params)
    try:
        job = await connection_bound_job(
            caller, {"id": job_id, "run_epoch": epoch}, epoch
        )
        with pytest.raises(dxe.StaleExecutionError):
            await primitive(job)
    finally:
        await caller.close()


# ============================================================================
# 15. a DELETE that WAITED must see what committed while it waited
# ============================================================================
#
# Two statements in the platform take the job row exclusively and then delete
# the job's durable state: the "fresh" rerun's wipe, and compaction. Both used
# to be ONE statement with the delete hanging off a CTE, and one statement
# reads ONE snapshot -- taken when the statement began, which for these two is
# BEFORE a wait that can last as long as the durable write in flight. Rows the
# blocked-on writer committed during the wait survived the delete.
#
# Both are now two statements in one transaction: the lock is held to COMMIT
# (so nothing lands between them) and the delete takes a FRESH READ COMMITTED
# snapshot (so it sees exactly what it waited out).


async def test_a_fresh_requeue_wipes_the_rows_committed_while_it_waited(
    db_pool, db_params, unique_queue
):
    """The other side of the lock the fences above take.

    A ``fresh`` requeue used to be ONE statement: the ``UPDATE`` in a CTE, the
    two ``DELETE``s hanging off its ``RETURNING``. One statement reads ONE
    snapshot, taken when the statement begins -- and this statement can WAIT
    for a long time before it does anything, because a durable write in flight
    holds the job row ``FOR SHARE`` and the requeue needs it exclusively. Rows
    that writer committed during the wait were therefore invisible to the
    DELETEs and SURVIVED the wipe. The fresh run then appended after them, so
    every reader was handed the two runs concatenated as one stream -- the
    exact failure the wipe exists to prevent, reintroduced by the wipe's own
    race. Reproduced.

    Two statements in one transaction fixes it. Statement 1 holds the row lock
    until COMMIT, so "no re-claim can land between them" survives intact -- it
    was always the lock's guarantee, never the statement count's. Statement 2
    takes a FRESH READ COMMITTED snapshot and therefore sees exactly the writes
    statement 1 waited out.

    Driven through ``requeue_job`` with in-flight ``allowed_states`` because
    that is the reachable shape: with the fences locking the job row, a
    TERMINAL job can have no same-epoch writer still open (the terminal write
    would have had to wait for it), so ``rerun_job``'s own guard cannot reach
    this window. ``requeue_job``'s can, which is why its docstring calls
    ``allowed_states`` a boundary.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    assert [
        r["id"] for r in await db_pool.fetch(STMTS["run"], job_id, epoch, None)
    ] == [job_id]

    writer = await db.connect(**db_params)
    rerunner = await db.connect(**db_params)
    try:
        # a legitimate durable write is in flight: it holds the job row FOR
        # SHARE, and its rows are not committed yet
        inflight = writer.transaction()
        await inflight.start()
        appended = await writer.fetchrow(
            dxe.STREAM_APPEND_SQL, job_id, "rows", {"i": 0}, False, epoch
        )
        assert (appended["fenced"], appended["seq"]) == (1, 0)
        assert await writer.fetch(
            dxe.RECORD_STEP_SQL,
            job_id,
            1,
            "mid-flight",
            {"i": 0},
            None,
            epoch,
            db.utcnow(),
        )

        # ...and the requeue-with-wipe arrives while it is still open
        wiping = asyncio.create_task(
            db.requeue_job(
                rerunner,
                job_id,
                allowed_states=("claimed", "running"),
                wipe_checkpoints=True,
            )
        )
        await wait_until_blocked_on_a_transaction(db_pool)
        assert not wiping.done(), "the requeue must WAIT for the writer's row lock"

        await inflight.commit()
        requeued = await asyncio.wait_for(wiping, timeout=20)
    finally:
        await rerunner.close()
        await writer.close()

    assert requeued == job_id
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_stream WHERE job_id = $1", job_id
        )
        == 0
    ), "a stream row survived the wipe; the fresh run would append after it"
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    ), "a checkpoint survived the wipe; the fresh run would fast-forward over it"


async def test_compaction_discards_the_checkpoint_committed_while_it_waited(
    db_pool, db_params, unique_queue
):
    """The same window, in compaction, where it is worse.

    ``compact()`` promises the log is empty and the next ``step()`` is
    sequence 1 again. A checkpoint that survived it is a checkpoint sitting at
    a sequence the restarted log is about to REUSE -- so the next ``step()``
    does not run at all: it replays the previous turn's recorded output and
    returns it as this turn's answer. Silent, wrong, and indistinguishable
    from the work having been done.

    The interleave is the one that produced it: a durable write in flight
    holds the job row ``FOR SHARE``, compaction needs it exclusively and
    blocks, and the writer commits its checkpoint during the wait. Written as
    one statement, compaction's DELETE ran against the snapshot it took before
    the wait and could not see that row. Driven here as two real transactions,
    made deterministic by waiting until the compactor is provably stuck.
    """
    job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 1})
    claimed = await claim_once(db_pool, unique_queue)
    epoch = claimed["run_epoch"]
    assert [
        r["id"] for r in await db_pool.fetch(STMTS["run"], job_id, epoch, None)
    ] == [job_id]

    writer = await db.connect(**db_params)
    compactor = await db.connect(**db_params)
    try:
        # a legitimate durable write is in flight: it holds the job row FOR
        # SHARE, and its checkpoint is not committed yet
        inflight = writer.transaction()
        await inflight.start()
        assert await writer.fetch(
            dxe.RECORD_STEP_SQL,
            job_id,
            1,
            "mid-flight",
            {"i": 0},
            None,
            epoch,
            db.utcnow(),
        )

        # ...and the turn boundary arrives while it is still open. The job is
        # bound BEFORE the write, so its own view of the log is empty and the
        # unreplayed-log guard does not fire -- which is exactly the state a
        # state machine is in at the top of a turn.
        job = await connection_bound_job(compactor, claimed, epoch)
        compacting = asyncio.create_task(job.compact())
        await wait_until_blocked_on_a_transaction(db_pool)
        assert not compacting.done(), "compaction must WAIT for the writer's row lock"

        await inflight.commit()
        assert await asyncio.wait_for(compacting, timeout=20) is True
    finally:
        await compactor.close()
        await writer.close()

    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    ), (
        "a checkpoint survived compaction; the next step() at that sequence "
        "would replay it instead of running"
    )
