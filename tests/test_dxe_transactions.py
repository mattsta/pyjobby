"""``transaction()``: the step whose effect and checkpoint are one commit.

``step()`` is at-least-once — the effect commits, then the checkpoint does,
and a crash between them re-executes the function. ``transaction()`` closes
that window for work against THIS database by handing the function the
worker's connection and writing the checkpoint inside the same transaction.

What is pinned here:

* the write and the checkpoint commit together, and roll back together;
* a **superseded** attempt's write is rolled back by the epoch fence itself
  — exactly-once and fencing are one mechanism, not two;
* replay is byte-for-byte the same decision as ``step()`` (shared
  ``_dxe_resume``): a completed transaction fast-forwards without executing,
  a failed one re-executes, a name mismatch raises NondeterminismError;
* a failed transaction still records its error checkpoint, in a separate
  transaction, because the one that would have carried it was rolled back;
* nesting inside an enclosing transaction degrades to a savepoint, and the
  prepared-statement checkpoint really does participate in it.

The crash-in-the-window proof lives in ``tests/test_dxe_faults.py``
(``test_kill_between_the_write_and_the_checkpoint``), where a real worker
process is SIGKILLed at that exact instant.

Every "did this really execute?" claim is counted in the ``jorb_test_effect``
ledger rather than inferred from the checkpoint table.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from pyjobby import dxe
from pyjobby.db import requeue_job
from pyjobby.pj import Job, JobSystem

from .conftest import wait_for_job_state
from .utils.faults import (
    count_effects,
    effect_counts,
    ensure_effects_table,
    record_effect,
    record_effect_out_of_band,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# job classes (resolved by live workers via their dotted path)
# ============================================================================


class SupersededTransactionJob(Job):
    """Runs a ``transaction()`` step only after this attempt has been fenced.

    Attempt 1 parks until the test requeues the job (which advances
    ``run_epoch``), so the fencing is deterministic rather than timed. The
    transactional write then happens, the checkpoint insert matches zero
    rows, and the StaleExecutionError must take the write down with it.
    Attempt 2 owns the epoch and simply succeeds.
    """

    async def task(self, tag: str) -> dict[str, Any]:
        attempt = self.job["run_count"]
        if attempt == 1:
            await self._wait_until_superseded()
        result: dict[str, Any] = await self.transaction(
            "write", self._write, tag, attempt
        )
        return result

    async def _wait_until_superseded(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = await self.s.cxn.fetchval(
                "SELECT run_epoch FROM jorb WHERE id = $1", self.job["id"]
            )
            if current != self._dxe_epoch:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the test never superseded this attempt")

    async def _write(self, conn, tag: str, attempt: int) -> dict[str, Any]:
        # out-of-band (committed, survives the rollback): proof this attempt
        # really executed the function...
        await record_effect_out_of_band(
            self.s.dsn, tag, self.job["id"], f"attempt-{attempt}"
        )
        # ...and the transactional write, which must NOT survive it
        await record_effect(conn, tag, self.job["id"], f"write-{attempt}")
        return {"attempt": attempt}


class FailingTransactionJob(Job):
    """Writes inside the transaction, then raises — the write must vanish."""

    async def task(self, tag: str, always: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = await self.transaction(
            "write", self._write, tag, always
        )
        return result

    async def _write(self, conn, tag: str, always: bool) -> dict[str, Any]:
        await record_effect_out_of_band(self.s.dsn, tag, self.job["id"], "attempt")
        await record_effect(conn, tag, self.job["id"], "write")
        if always or self.job["error_count"] == 0:
            raise RuntimeError("boom after the write")
        return {"stamp": "written"}


class ReplayTransactionJob(Job):
    """A committed ``transaction()`` followed by a step that fails once.

    The retry must fast-forward the transaction without re-executing it —
    the ledger, not the return value, is what proves that.
    """

    async def task(self, tag: str) -> dict[str, Any]:
        wrote = await self.transaction("write", self._write, tag)
        await self.step("gate", self._explode_once)
        return {"wrote": wrote}

    async def _write(self, conn, tag: str) -> dict[str, Any]:
        await record_effect(conn, tag, self.job["id"], "write")
        return {"stamp": "written"}

    def _explode_once(self) -> dict[str, bool]:
        if self.job["error_count"] == 0:
            raise RuntimeError("failing after the transaction committed")
        return {"ok": True}


class NondeterministicTransactionJob(Job):
    """Names its transaction differently on the second attempt."""

    async def task(self, tag: str) -> str:
        first = self.job["error_count"] == 0
        await self.transaction("alpha" if first else "beta", self._write, tag)
        if first:
            raise RuntimeError("failing after the checkpoint committed")
        return "ok"

    async def _write(self, conn, tag: str) -> dict[str, bool]:
        await record_effect(conn, tag, self.job["id"], "write")
        return {"written": True}


# ============================================================================
# helpers
# ============================================================================


async def enqueue(pool, queue: str, job_class: str, kwargs=None, admin=None) -> int:
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        job_class,
        kwargs or {},
        queue,
        admin or {},
    )
    return job_id


@pytest_asyncio.fixture
async def prepared_worker(db_params, unique_queue) -> Any:
    """A JobSystem with a live connection and prepared statements, no loop.

    The transaction/savepoint semantics are properties of the worker's
    connection, so testing them needs the real connection and the real
    prepared statements — but not the claim loop."""
    system = JobSystem(
        dsn=db_params, qname=unique_queue, capabilities=("test",), workerId=0
    )
    await system._connect_and_prepare()
    try:
        yield system
    finally:
        await system.cxn.close()


# ============================================================================
# the exactly-once/fencing unification
# ============================================================================


async def test_stale_epoch_rolls_back_the_transactions_write(
    live_worker, unique_queue, db_pool
):
    """A superseded attempt's application write is undone by the fence.

    The checkpoint insert is conditional on this execution still owning the
    job. When it matches zero rows the raise happens INSIDE the transaction,
    so the application write goes back with it: a zombie worker cannot
    commit application data for a job another worker now owns. The
    out-of-band marker proves the function really ran at the stale epoch —
    the missing write is a rollback, not a skipped execution.
    """
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_transactions.SupersededTransactionJob",
        {"tag": unique_queue},
    )
    row = await wait_for_job_state(db_pool, job_id, ("running",))
    stale_epoch = row["run_epoch"]

    # supersede it exactly as the monitor would, while it is parked
    await requeue_job(
        db_pool, job_id, allowed_states=("claimed", "running"), reset_errors=False
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"attempt": 2}
    assert row["run_epoch"] > stale_epoch
    assert row["error_count"] == 0  # a superseded attempt is abandoned, not failed

    assert await effect_counts(db_pool, unique_queue) == {
        "attempt-1": 1,  # the fenced attempt really executed the function
        "attempt-2": 1,
        "write-2": 1,  # ...but only the winner's write is in the database
    }

    steps = await db_pool.fetch(
        """SELECT step_seq, name, output, error, run_epoch FROM jorb_step
           WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    assert [(s["step_seq"], s["name"], s["output"], s["error"]) for s in steps] == [
        (1, "write", {"attempt": 2}, None)
    ]
    assert steps[0]["run_epoch"] == row["run_epoch"]


# ============================================================================
# failure: the write rolls back, the error checkpoint survives it
# ============================================================================


async def test_failed_transaction_rolls_back_its_write_and_records_the_error(
    live_worker, unique_queue, db_pool
):
    """The rollback erases the work; the error checkpoint outlives it.

    The error is recorded in a SEPARATE transaction precisely because the
    one that carried the work is gone by then — observability that rolled
    back with the failure would be no observability at all.
    """
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_transactions.FailingTransactionJob",
        {"tag": unique_queue, "always": True},
        {"max_retries": 1, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert "boom after the write" in row["error_message"]

    # the function ran once; nothing it wrote through the handed connection
    # is in the database
    assert await effect_counts(db_pool, unique_queue) == {"attempt": 1}

    step = await db_pool.fetchrow("SELECT * FROM jorb_step WHERE job_id = $1", job_id)
    assert step["step_seq"] == 1
    assert step["name"] == "write"
    assert step["output"] is None
    assert step["error"] == "RuntimeError: boom after the write"
    assert step["run_epoch"] == row["run_epoch"]


async def test_failed_transaction_re_executes_on_the_next_attempt(
    live_worker, unique_queue, db_pool
):
    """A recorded failure is not a result: the next attempt runs it again."""
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_transactions.FailingTransactionJob",
        {"tag": unique_queue},
        {"max_retries": 3, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"stamp": "written"}
    assert row["error_count"] == 1

    # executed twice, but only the committed attempt left a write behind
    assert await effect_counts(db_pool, unique_queue) == {"attempt": 2, "write": 1}

    step = await db_pool.fetchrow("SELECT * FROM jorb_step WHERE job_id = $1", job_id)
    assert step["error"] is None
    assert step["output"] == {"stamp": "written"}


# ============================================================================
# replay parity with step()
# ============================================================================


async def test_completed_transaction_fast_forwards_on_retry(
    live_worker, unique_queue, db_pool
):
    """A committed transaction never executes again, ledger-proved."""
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_transactions.ReplayTransactionJob",
        {"tag": unique_queue},
        {"max_retries": 3, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"wrote": {"stamp": "written"}}
    assert row["error_count"] == 1  # the step AFTER the transaction failed once

    # the transaction executed exactly once, across both attempts
    assert await count_effects(db_pool, unique_queue, "write") == 1

    steps = await db_pool.fetch(
        """SELECT step_seq, name, output, run_epoch FROM jorb_step
           WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    # step 1 still carries the FIRST attempt's epoch: it was replayed from
    # the checkpoint, not re-executed
    assert steps[0]["step_seq"] == 1
    assert steps[0]["name"] == "write"
    assert steps[0]["output"] == {"stamp": "written"}
    assert steps[0]["run_epoch"] == 1
    assert steps[1]["name"] == "gate"
    assert steps[1]["run_epoch"] > steps[0]["run_epoch"]


async def test_transaction_name_mismatch_is_caught_at_replay(
    live_worker, unique_queue, db_pool
):
    """Renaming a recorded transaction is nondeterminism, and it is fatal."""
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_transactions.NondeterministicTransactionJob",
        {"tag": unique_queue},
        {"max_retries": 2, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert (
        "step 1 was 'alpha' on a previous attempt but is 'beta' now"
        in (row["error_message"])
    )
    # nothing executed on the mismatching attempt
    assert await count_effects(db_pool, unique_queue, "write") == 1


async def test_step_and_transaction_share_one_replay_decision(db_params, unique_queue):
    """The two primitives must not grow separate replay logic.

    Both go through ``_dxe_resume`` — asserted structurally AND behaviorally,
    because two copies of this decision would drift on the first change to
    either one.
    """
    assert "_dxe_resume(" in inspect.getsource(Job.step)
    assert "_dxe_resume(" in inspect.getsource(Job.transaction)

    system = JobSystem(
        dsn=db_params, qname=unique_queue, capabilities=("test",), workerId=0
    )
    recorded = [{"step_seq": 1, "name": "alpha", "output": {"n": 1}, "error": None}]

    # a completed checkpoint fast-forwards identically (no connection is
    # touched: neither primitive executes anything here)
    stepper = Job(s=system, job={"id": 1, "run_epoch": 1})
    stepper._dxe_bind(recorded, 1)
    assert await stepper.step("alpha", lambda: pytest.fail("must not execute")) == {
        "n": 1
    }

    txn = Job(s=system, job={"id": 1, "run_epoch": 1})
    txn._dxe_bind(recorded, 1)
    assert await txn.transaction(
        "alpha", lambda conn: pytest.fail("must not execute")
    ) == {"n": 1}

    # ...and a name mismatch raises the SAME error, word for word
    stepper._dxe_bind(recorded, 1)
    with pytest.raises(dxe.NondeterminismError) as from_step:
        await stepper.step("beta", lambda: None)
    txn._dxe_bind(recorded, 1)
    with pytest.raises(dxe.NondeterminismError) as from_txn:
        await txn.transaction("beta", lambda conn: None)
    assert str(from_txn.value) == str(from_step.value)
    assert "must be deterministic" in str(from_txn.value)


# ============================================================================
# transaction mechanics on the worker's own connection
# ============================================================================


async def test_checkpoint_and_write_are_visible_together_and_roll_back_together(
    prepared_worker, unique_queue, db_pool
):
    """The prepared-statement checkpoint really joins the open transaction.

    Driving the primitive under an ENCLOSING transaction proves two things
    at once: asyncpg degrades the nested ``transaction()`` to a savepoint
    (so a worker that already holds a transaction is handled), and the
    checkpoint — written through a prepared statement — is inside it, since
    rolling the outer transaction back takes the checkpoint with the write.
    """
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
        unique_queue,
    )

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    klass._dxe_bind([], 1)

    async def write(conn) -> dict[str, bool]:
        await record_effect(conn, unique_queue, job_id, "nested")
        return {"nested": True}

    outer = prepared_worker.cxn.transaction()
    await outer.start()
    assert await klass.transaction("nested", write) == {"nested": True}

    # inside the transaction both rows exist...
    assert (
        await prepared_worker.cxn.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 1
    )
    # ...and nothing is visible outside it yet
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )
    assert await count_effects(db_pool, unique_queue, "nested") == 0

    await outer.rollback()

    # the write and the checkpoint went back together
    assert await count_effects(db_pool, unique_queue, "nested") == 0
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )


async def test_transaction_commits_the_write_with_its_checkpoint(
    prepared_worker, unique_queue, db_pool
):
    """The plain path: one commit, both rows, no enclosing transaction."""
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
        unique_queue,
    )

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    klass._dxe_bind([], 1)

    async def write(conn) -> dict[str, bool]:
        await record_effect(conn, unique_queue, job_id, "committed")
        return {"committed": True}

    assert await klass.transaction("committed", write) == {"committed": True}

    assert await count_effects(db_pool, unique_queue, "committed") == 1
    step = await db_pool.fetchrow("SELECT * FROM jorb_step WHERE job_id = $1", job_id)
    assert (step["name"], step["output"], step["error"]) == (
        "committed",
        {"committed": True},
        None,
    )


async def test_a_foreign_connections_work_is_not_rolled_back(
    prepared_worker, unique_queue, db_pool
):
    """The documented limit, pinned so the docstring cannot quietly lie.

    ``transaction()`` guarantees exactly as much as the connection it hands
    in covers. Work a function does on a DIFFERENT connection commits on its
    own and survives the rollback — it is at-least-once, like ``step()``.
    This cannot be prevented (the function is free to ignore its argument),
    so it is stated in the docstring and proved here.
    """
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
        unique_queue,
    )

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    klass._dxe_bind([], 1)

    async def write(conn) -> dict[str, bool]:
        await record_effect(conn, unique_queue, job_id, "handed")
        await record_effect_out_of_band(
            prepared_worker.dsn, unique_queue, job_id, "foreign"
        )
        raise RuntimeError("rolls back only what the handed connection did")

    with pytest.raises(RuntimeError):
        await klass.transaction("mixed", write)

    assert await count_effects(db_pool, unique_queue, "handed") == 0  # rolled back
    assert await count_effects(db_pool, unique_queue, "foreign") == 1  # survived

    # ...and the failure checkpoint survived the rollback too, in its own
    # transaction
    step = await db_pool.fetchrow("SELECT * FROM jorb_step WHERE job_id = $1", job_id)
    assert (
        step["error"] == "RuntimeError: rolls back only what the handed connection did"
    )
    assert step["output"] is None


async def test_transaction_at_a_stale_epoch_writes_nothing(
    prepared_worker, unique_queue, db_pool
):
    """The fence, exercised directly: StaleExecutionError and no write.

    The live-worker test above proves this end to end; this one isolates the
    mechanism, at an epoch that never owned the job at all.
    """
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 7) RETURNING id""",
        unique_queue,
    )

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 3})
    klass._dxe_bind([], 3)

    async def write(conn) -> dict[str, bool]:
        await record_effect(conn, unique_queue, job_id, "zombie")
        return {"zombie": True}

    with pytest.raises(dxe.StaleExecutionError):
        await klass.transaction("zombie", write)

    assert await count_effects(db_pool, unique_queue, "zombie") == 0
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        == 0
    )
    # the connection is usable afterwards: the rollback was clean
    assert await prepared_worker.cxn.fetchval("SELECT 1") == 1


async def test_transaction_does_not_leave_an_open_transaction_behind(
    prepared_worker, unique_queue, db_pool
):
    """Neither path may strand the worker connection mid-transaction.

    A leaked transaction would silently swallow every subsequent write of
    the attempt, so both the success and the failure path are checked
    against ``pg_stat_activity``.
    """
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
        unique_queue,
    )
    backend = await prepared_worker.cxn.fetchval("SELECT pg_backend_pid()")

    async def state() -> str:
        value: str = await db_pool.fetchval(
            "SELECT state FROM pg_stat_activity WHERE pid = $1", backend
        )
        return value

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    klass._dxe_bind([], 1)
    await klass.transaction("ok", lambda conn: {"ok": True})
    assert await state() == "idle"

    klass._dxe_bind([], 1)

    async def boom(conn) -> None:
        await record_effect(conn, unique_queue, job_id, "boom")
        raise ValueError("failure inside the transaction")

    with pytest.raises(ValueError):
        await klass.transaction("boom", boom)
    assert await state() == "idle"
    assert await count_effects(db_pool, unique_queue, "boom") == 0


async def test_a_timed_out_transaction_rolls_back_and_leaves_the_connection_clean(
    prepared_worker, unique_queue, db_pool
):
    """A blown step budget must not strand the worker's connection.

    The budget expires *inside* the transaction and while a query is really
    in flight — the hardest case, because the cancellation has to abort a
    running statement rather than an idle ``asyncio.sleep``. Everything after
    that is on trial: the application write must roll back, no transaction
    may be left open, and the connection must still work, since a stranded
    transaction would silently swallow every subsequent statement of the
    attempt. The error checkpoint is written afterwards, in its own
    transaction, exactly like any other failed ``transaction()``.
    """
    await ensure_effects_table(db_pool)
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
        unique_queue,
    )
    backend = await prepared_worker.cxn.fetchval("SELECT pg_backend_pid()")

    klass = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    klass._dxe_bind([], 1)

    async def slow(conn) -> dict[str, bool]:
        await record_effect(conn, unique_queue, job_id, "timed-out")
        await conn.execute("SELECT pg_sleep(30)")  # cancelled mid-statement
        return {"never": True}

    with pytest.raises(dxe.StepTimeoutError) as blown:
        await klass.transaction("slow", slow, timeout=0.3)
    assert str(blown.value) == "step 'slow' exceeded its 0.3s timeout"
    assert (blown.value.name, blown.value.timeout) == ("slow", 0.3)

    # the application write went back with the rollback
    assert await count_effects(db_pool, unique_queue, "timed-out") == 0
    # nothing was left open, and the connection is still usable
    assert prepared_worker.cxn.is_in_transaction() is False
    assert (
        await db_pool.fetchval(
            "SELECT state FROM pg_stat_activity WHERE pid = $1", backend
        )
        == "idle"
    )
    assert await prepared_worker.cxn.fetchval("SELECT 42") == 42

    # ...and the checkpoint that survived the rollback says it was a timeout
    step = await db_pool.fetchrow("SELECT * FROM jorb_step WHERE job_id = $1", job_id)
    assert (step["step_seq"], step["name"], step["output"], step["error"]) == (
        1,
        "slow",
        None,
        "StepTimeoutError: step 'slow' exceeded its 0.3s timeout",
    )


async def test_step_and_transaction_time_out_identically(
    prepared_worker, unique_queue, db_pool
):
    """The budget must not drift between the two primitives.

    Same budget, same shape of slow work, same recorded checkpoint and same
    error — the structural half (one shared ``_dxe_invoke``) is asserted in
    ``tests/test_dxe_step_timeouts.py``.
    """
    ids = [
        await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, run_epoch)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running', 1) RETURNING id""",
            unique_queue,
        )
        for _ in range(2)
    ]

    async def slow_step() -> None:
        await asyncio.sleep(30)

    async def slow_txn(conn) -> None:
        await asyncio.sleep(30)

    stepper = Job(s=prepared_worker, job={"id": ids[0], "run_epoch": 1})
    stepper._dxe_bind([], 1)
    with pytest.raises(dxe.StepTimeoutError) as from_step:
        await stepper.step("slow", slow_step, timeout=0.3)

    txn = Job(s=prepared_worker, job={"id": ids[1], "run_epoch": 1})
    txn._dxe_bind([], 1)
    with pytest.raises(dxe.StepTimeoutError) as from_txn:
        await txn.transaction("slow", slow_txn, timeout=0.3)

    assert str(from_txn.value) == str(from_step.value)
    recorded = [
        await db_pool.fetchval("SELECT error FROM jorb_step WHERE job_id = $1", job_id)
        for job_id in ids
    ]
    assert recorded == [
        "StepTimeoutError: step 'slow' exceeded its 0.3s timeout",
        "StepTimeoutError: step 'slow' exceeded its 0.3s timeout",
    ]


async def test_asyncpg_nests_a_transaction_as_a_savepoint(prepared_worker):
    """The nesting assumption the docstring makes, verified against asyncpg.

    ``transaction()`` documents that an enclosing transaction turns the
    inner one into a savepoint rather than an error; that is a property of
    asyncpg, so it is asserted rather than assumed.
    """
    conn: asyncpg.Connection = prepared_worker.cxn
    async with conn.transaction():
        assert conn.is_in_transaction()
        async with conn.transaction():  # savepoint, not a second BEGIN
            assert conn.is_in_transaction()
    assert conn.is_in_transaction() is False
