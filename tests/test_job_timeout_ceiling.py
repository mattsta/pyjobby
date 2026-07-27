"""The job-level timeout: one deadline, and what it can actually stop.

``timeout_seconds`` (or the class's ``timeout``, or the worker's
``--default-timeout``) is an operator-facing promise: *this job gets N
seconds*. This file pins that the in-process ceiling really is N — once —
whatever shape the job's ``run()`` takes.

The shapes matter because the worker resolves a job in stages: ``run()`` is
called in a thread (it may be synchronous), what it hands back may be a
coroutine, and what *that* returns may be an async generator that still has
to be drained. Bounding each stage separately gave each stage its own full
timeout, so a job that spent real time staging could run for up to twice its
configured ceiling, and an async generator was drained with no ceiling at
all. The measurements below are taken where the deadline is enforced — the
live worker runs in this process, so the job reports its own elapsed time as
it is cancelled — rather than inferred from polling the row.

The honest limit is also pinned here: a deadline is delivered as a
cancellation, so it stops a coroutine and merely *stops waiting* for a
blocking synchronous job. See ``tests/test_dxe_step_timeouts.py`` for the
same story at step granularity, and ``tests/test_monitor.py`` for the
out-of-process backstop that covers what neither can interrupt.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyjobby.pj import Job

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio

THIS = "tests.test_job_timeout_ceiling"

#: ``(job_id, seconds)`` — how long a job actually ran in-process before its
#: deadline cancelled it, reported by the job code at the cancellation point.
CEILINGS: list[tuple[int, float]] = []

#: ``job_id -> monotonic`` at task entry, for the synchronous job that cannot
#: report its own cancellation because it never gets one.
ENTERED: dict[int, float] = {}


# ============================================================================
# job classes (resolved by live workers via their dotted path)
# ============================================================================


class StagesThenHangsJob(Job):
    """Blocking work in ``run()``, then a coroutine that hangs.

    The shape that exposed the double bound: ``task()`` is synchronous and
    spends real time before handing back a coroutine, so staging and awaiting
    are two distinct spans of the same job's life.
    """

    def task(self, prep: float, hang: float) -> Any:
        ENTERED[self.job["id"]] = time.monotonic()
        time.sleep(prep)  # blocking, but in the worker's thread
        return self._hang(hang)

    async def _hang(self, hang: float) -> str:
        try:
            await asyncio.sleep(hang)
        except asyncio.CancelledError:
            CEILINGS.append(
                (self.job["id"], time.monotonic() - ENTERED[self.job["id"]])
            )
            raise
        return "hang finished"


class YieldsThenHangsJob(Job):
    """An async task returning an async generator that hangs mid-stream.

    Draining the generator is a third stage, after ``run()`` and after the
    coroutine it returned; it is job code, and it is on the job's clock.
    """

    async def task(self, hang: float) -> AsyncIterator[str]:
        entered = time.monotonic()
        ENTERED[self.job["id"]] = entered

        async def stream() -> AsyncIterator[str]:
            yield "first"
            try:
                await asyncio.sleep(hang)
            except asyncio.CancelledError:
                CEILINGS.append((self.job["id"], time.monotonic() - entered))
                raise
            yield "second"

        return stream()


class BlocksPastItsDeadlineJob(Job):
    """A wholly synchronous job that blocks far past its deadline."""

    def task(self, block: float) -> str:
        ENTERED[self.job["id"]] = time.monotonic()
        time.sleep(block)
        return "blocked"


class PromptSyncJob(Job):
    """Synchronous, and done long before any sane deadline."""

    def task(self, x: int) -> dict[str, int]:
        time.sleep(0.05)
        return {"doubled": x * 2}


class PromptAsyncJob(Job):
    """Asynchronous, and done long before any sane deadline."""

    async def task(self, x: int) -> dict[str, int]:
        await asyncio.sleep(0.05)
        return {"doubled": x * 2}


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


# ============================================================================
# the ceiling itself
# ============================================================================


async def test_a_staged_job_is_bounded_by_its_configured_timeout_once(
    live_worker, unique_queue, db_pool
):
    """The headline: N seconds configured means N seconds of running.

    This job spends 1.0s in a blocking ``task()`` and only then produces the
    coroutine that hangs. Bounding the two stages separately handed each one
    a fresh 2s budget, so the job ran ~3.0s — 1.5x what the operator asked
    for, and up to 2x for a job that stages more slowly. One deadline for the
    whole execution is the only way "timeout_seconds" means anything.
    """
    CEILINGS.clear()
    ENTERED.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.StagesThenHangsJob",
        {"prep": 1.0, "hang": 60},
        {"timeout_seconds": 2, "on_timeout": "fail", "max_retries": 5},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == "Job timed out after 2s"
    assert row["error_count"] == 1
    assert row["result"] is None
    assert row["timeout_at"] is None  # cleared by the terminal transition

    assert [jid for jid, _ in CEILINGS] == [job_id]
    ran = CEILINGS[0][1]
    assert 1.8 <= ran <= 2.5, f"ran {ran:.2f}s under a 2s timeout"


async def test_an_async_generator_is_drained_under_the_same_deadline(
    live_worker, unique_queue, db_pool
):
    """Draining the generator is job code, so it is on the job's clock.

    An async task returning an async generator used to be consumed outside
    every bound: the coroutine that *produced* the generator was timed, and
    then the generator was drained with no ceiling at all. A generator that
    hangs on its second item therefore ignored the job timeout entirely and
    was left to the monitor's out-of-process sweep.
    """
    CEILINGS.clear()
    ENTERED.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.YieldsThenHangsJob",
        {"hang": 60},
        {"timeout_seconds": 2, "on_timeout": "fail", "max_retries": 5},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == "Job timed out after 2s"
    assert row["error_count"] == 1
    assert row["result"] is None

    assert [jid for jid, _ in CEILINGS] == [job_id]
    ran = CEILINGS[0][1]
    assert 1.8 <= ran <= 2.5, f"ran {ran:.2f}s under a 2s timeout"


async def test_a_blocking_synchronous_job_is_abandoned_at_its_deadline(
    live_worker, unique_queue, db_pool
):
    """What the deadline can and cannot do to synchronous work.

    ``run()`` is called in a thread, so the event loop — and the timer — stay
    alive: the worker stops waiting exactly at the deadline, records the
    timeout, and claims the next job. What it cannot do is *stop the thread*.
    That thread runs its 8s to completion in the background and its return
    value is discarded, so the in-process deadline is a bound on the worker's
    attention, not on the work. Only the process exiting stops it.
    """
    ENTERED.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.BlocksPastItsDeadlineJob",
        {"block": 8},
        {"timeout_seconds": 2, "on_timeout": "fail", "max_retries": 5},
    )

    row = await wait_for_job_state(
        db_pool, job_id, ("crashed",), timeout=20, interval=0.05
    )
    observed = time.monotonic() - ENTERED[job_id]

    assert row["error_message"] == "Job timed out after 2s"
    assert row["error_count"] == 1
    assert row["result"] is None  # the thread's "blocked" never lands
    assert 1.8 <= observed <= 2.7, f"waited {observed:.2f}s under a 2s timeout"


# ============================================================================
# the on_timeout policy, unchanged
# ============================================================================


async def test_a_synchronous_timeout_retries_then_dead_letters(
    live_worker, unique_queue, db_pool
):
    """``on_timeout='retry'`` spends the retry budget, then the DLQ.

    The async equivalents live in ``tests/test_pj_worker_run_loop.py``; this
    is the synchronous half, where the timeout is produced by abandoning a
    thread rather than by cancelling a coroutine. Same policy, same row.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.BlocksPastItsDeadlineJob",
        {"block": 5},
        {
            "timeout_seconds": 1,
            "on_timeout": "retry",
            "max_retries": 2,
            "initial_retry_delay": 0,
        },
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=25)
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_count"] == 2  # attempt 1 requeued, attempt 2 exhausted it
    assert row["run_epoch"] >= 2

    requeued = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event = 'queued'",
        job_id,
    )
    assert requeued == 1


async def test_a_synchronous_timeout_can_dead_letter_on_the_first_overrun(
    live_worker, unique_queue, db_pool
):
    """``on_timeout='fail'`` is terminal on attempt 1, retries or not."""
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.BlocksPastItsDeadlineJob",
        {"block": 5},
        {"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 10},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_count"] == 1
    # dead-lettering fences out the abandoned execution (epoch 1 -> 2)
    assert row["run_epoch"] == 2

    requeued = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event = 'queued'",
        job_id,
    )
    assert requeued == 0


# ============================================================================
# jobs the deadline never touches
# ============================================================================


async def test_a_job_that_finishes_inside_its_timeout_is_unaffected(
    live_worker, unique_queue, db_pool
):
    """The control case, in every shape: a deadline nobody reaches.

    Synchronous, asynchronous, and unbounded (``timeout_seconds: 0`` disables
    the deadline, exercising the path where no timer is armed at all) — all
    three finish with their real result and a clean error count.
    """
    await live_worker()

    sync_id = await enqueue(
        db_pool, unique_queue, f"{THIS}.PromptSyncJob", {"x": 3}, {"timeout_seconds": 5}
    )
    async_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.PromptAsyncJob",
        {"x": 4},
        {"timeout_seconds": 5},
    )
    unbounded_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.PromptAsyncJob",
        {"x": 5},
        {"timeout_seconds": 0},
    )

    for job_id, doubled in ((sync_id, 6), (async_id, 8), (unbounded_id, 10)):
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
        assert row["result"] == {"doubled": doubled}
        assert row["error_count"] == 0
        assert row["error_message"] is None
        assert row["timeout_at"] is None
