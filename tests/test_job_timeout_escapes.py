"""The two ways a job used to get out from under its own deadline.

``tests/test_job_timeout_ceiling.py`` pins that a job configured for N
seconds *is stopped* at N seconds. Two escapes survived that: a job could
catch the cancellation and report success anyway, and a timed-out
synchronous job could leave a thread behind that eventually wedged the whole
worker. Both are closed here, and — more importantly — the control that
keeps closing them from inventing spurious timeouts is pinned too.

1. **A swallowed deadline is not a success.** ``asyncio.timeout`` raises
   nothing when the body catches its ``CancelledError`` and returns, so the
   worker stored a result for an attempt it had already given up on. The row
   reached a terminal state on its own, so the monitor's out-of-process
   sweep could not see it either: nothing anywhere said the job had overrun.
   ``_execute`` now refuses that success. The refusal is keyed on
   ``Timeout.expired()`` — *this scope's timer fired while the job was still
   inside it* — and never on a clock read taken after the job returned, which
   is what makes "finished microseconds before the deadline, recorded just
   after it" a success, deterministically. That control is
   ``test_a_job_finishing_just_inside_its_deadline_is_still_a_success``, and
   it is the test that matters most in this file: a correctness fix that
   fails jobs which did nothing wrong is worse than the escape it closed.

2. **Abandoned synchronous threads cannot wedge the worker silently.** A
   timed-out ``time.sleep(600)`` keeps its thread until it finishes; nothing
   can interrupt it. Those threads used to accumulate in the event loop's
   *default* executor — shared with the worker's own ``getaddrinfo`` — until
   the next ``to_thread`` blocked forever and the worker stopped claiming
   while looking perfectly healthy. Job threads now come from the worker's
   own bounded pool, and a worker whose pool is full of abandoned threads
   stops claiming *loudly* instead of accepting jobs it cannot start.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from loguru import logger

from pyjobby import dxe
from pyjobby.pj import Job, JobSystem

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio

THIS = "tests.test_job_timeout_escapes"

#: ``job_id -> [seconds from task entry to catching the cancellation]``, one
#: entry per attempt, reported by the job at the point it is cancelled.
CAUGHT: dict[int, list[float]] = {}

#: ``job_id -> [cleanup markers]`` for the job that cleans up and re-raises.
CLEANED: dict[int, list[str]] = {}

#: ``job_id -> thread name`` for the synchronous jobs.
THREADS: dict[int, str] = {}

#: ``job_id -> monotonic`` at entry / at the point the abandoned thread
#: actually finished its work.
ENTERED: dict[int, float] = {}
LEFT: dict[int, float] = {}


def _record(store: dict[int, list[Any]], job_id: int, value: Any) -> None:
    store.setdefault(job_id, []).append(value)


# ============================================================================
# job classes (resolved by live workers via their dotted path)
# ============================================================================


class SwallowsItsCancellationJob(Job):
    """Catches the deadline's cancellation and returns a value anyway.

    The exact shape of the escape: legal Python, no exception left, and a
    perfectly plausible-looking result for work that never finished.
    """

    async def task(self) -> dict[str, Any]:
        entered = time.monotonic()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            _record(CAUGHT, self.job["id"], time.monotonic() - entered)
            return {"swallowed": True, "pretend": "result"}
        return {"swallowed": False}


class CleansUpThenReraisesJob(Job):
    """Catches the cancellation to clean up, then re-raises it.

    The legitimate pattern the refusal must not disturb: this job's
    cancellation still propagates, so it takes exactly the path it always
    did — and its cleanup still runs.
    """

    async def task(self) -> str:
        entered = time.monotonic()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            _record(CLEANED, self.job["id"], "closed")
            _record(CAUGHT, self.job["id"], time.monotonic() - entered)
            raise
        return "never"


class FinishesJustInsideItsDeadlineJob(Job):
    """Returns a real result with only ``margin`` seconds of its deadline left.

    It reads the very deadline the worker armed, so "just inside" is not an
    estimate: whatever else the worker then does — record the result, wake
    dependents — happens *after* the deadline has passed.
    """

    async def task(self, margin: float) -> dict[str, str]:
        assert self._dxe_deadline is not None, "the worker must have armed one"
        await asyncio.sleep(self._dxe_deadline - time.monotonic() - margin)
        return {"beat": "the deadline"}


class BlocksLongPastItsDeadlineJob(Job):
    """Synchronous, blocks far past its deadline, cannot be interrupted."""

    def task(self, block: float) -> str:
        jid = self.job["id"]
        THREADS[jid] = threading.current_thread().name
        ENTERED[jid] = time.monotonic()
        time.sleep(block)
        LEFT[jid] = time.monotonic()
        return "blocked"


class PromptJob(Job):
    """Async and immediate: the job used to ask "is this worker claiming?"."""

    async def task(self, x: int) -> dict[str, int]:
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


def bare_worker(**overrides: Any) -> JobSystem:
    """A JobSystem that never connects: ``_execute`` needs no database.

    The deadline is enforced entirely in-process, so the boundary tests can
    drive it directly and observe the classification with no polling, no
    claim latency, and no storage round-trip in the way."""
    params: dict[str, Any] = {
        "dsn": {},
        "qname": "escapes",
        "capabilities": ("test",),
        "workerId": 0,
    }
    params.update(overrides)
    return JobSystem(**params)


# ============================================================================
# escape 1: a swallowed deadline is not a success
# ============================================================================


async def test_a_job_finishing_just_inside_its_deadline_is_still_a_success():
    """The anti-spurious-timeout control, at the tightest boundary there is.

    Each round finishes with 50ms of its 1s deadline left and is then held
    until the deadline has demonstrably passed before its result is looked
    at — the worst case for any implementation that decided "was this a
    timeout?" by comparing a clock against the deadline at recording time.
    ``expired()`` asks a different question: did this scope's timer *fire*
    while the job was inside it. ``__aexit__`` cancels the timer the instant
    the body returns, so the answer is no, and the ordering is causal rather
    than a race — which is why ten rounds pass, not nine.
    """
    system = bare_worker()
    try:
        for _ in range(10):
            klass = FinishesJustInsideItsDeadlineJob(
                s=system, job={"id": 0, "kwargs": {"margin": 0.05}}
            )
            klass._dxe_deadline = time.monotonic() + 1.0

            result = await system._execute(klass, 1.0)

            # only now — safely past the deadline — is the success "recorded"
            await asyncio.sleep(0.1)
            assert time.monotonic() > klass._dxe_deadline
            assert result == {"beat": "the deadline"}
    finally:
        assert system._threads is not None
        system._threads.shutdown(wait=False)


async def test_swallowing_the_deadline_raises_the_job_timeout_instead():
    """The refusal itself, with no storage in the way.

    The job returns a value; ``_execute`` reports the timeout the operator
    configured, naming it exactly as a propagated cancellation would.
    """
    system = bare_worker()
    try:
        klass = SwallowsItsCancellationJob(s=system, job={"id": 0, "kwargs": {}})
        klass._dxe_deadline = time.monotonic() + 0.5

        with pytest.raises(dxe.JobTimeout) as raised:
            await system._execute(klass, 0.5)

        assert str(raised.value) == "Job timed out after 0.5s"
        assert raised.value.timeout == 0.5
        assert CAUGHT[0] and 0.4 <= CAUGHT[0][-1] <= 0.9
    finally:
        CAUGHT.pop(0, None)
        assert system._threads is not None
        system._threads.shutdown(wait=False)


async def test_a_swallowed_deadline_dead_letters_under_on_timeout_fail(
    live_worker, unique_queue, db_pool
):
    """End to end: no stored result, and ``on_timeout='fail'`` applied.

    Before, this row read ``finished`` with ``{"swallowed": true, ...}`` in
    ``result`` — a success the platform had no business claiming, and one
    the monitor could never correct, because the job reached a terminal
    state under its own power.
    """
    CAUGHT.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.SwallowsItsCancellationJob",
        {},
        {"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 5},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["state"] == "crashed"
    assert row["result"] is None
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_backtrace"] == (
        "Timeout error - job exceeded maximum execution time"
    )
    assert row["error_count"] == 1
    # dead-lettering fences out the abandoned execution (epoch 1 -> 2)
    assert row["run_epoch"] == 2
    assert row["timeout_at"] is None

    # the job really did catch its cancellation, once, at the deadline
    assert list(CAUGHT) == [job_id]
    assert len(CAUGHT[job_id]) == 1
    assert 0.8 <= CAUGHT[job_id][0] <= 1.6


async def test_a_swallowed_deadline_retries_then_dead_letters(
    live_worker, unique_queue, db_pool
):
    """``on_timeout='retry'`` spends the retry budget, exactly as if the
    cancellation had propagated: attempt 1 requeues, attempt 2 exhausts it."""
    CAUGHT.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.SwallowsItsCancellationJob",
        {},
        {
            "timeout_seconds": 1,
            "on_timeout": "retry",
            "max_retries": 2,
            "initial_retry_delay": 0,
        },
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=25)
    assert row["result"] is None
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_count"] == 2
    assert row["run_count"] == 2

    requeued = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event = 'queued'",
        job_id,
    )
    assert requeued == 1

    # both attempts ran to their own deadline and swallowed it
    assert len(CAUGHT[job_id]) == 2
    assert all(0.8 <= caught <= 1.6 for caught in CAUGHT[job_id])


async def test_cleaning_up_and_re_raising_behaves_exactly_as_before(
    live_worker, unique_queue, db_pool
):
    """The legitimate pattern is untouched.

    Catching ``CancelledError`` to release something and re-raising is how
    correct async code cleans up. The cancellation propagates,
    ``asyncio.timeout`` converts it, and the row is the same timeout it has
    always been — the refusal never sees this job, because there is no
    normal return for it to refuse.
    """
    CAUGHT.clear()
    CLEANED.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        f"{THIS}.CleansUpThenReraisesJob",
        {},
        {"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 5},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["result"] is None
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_count"] == 1
    # dead-lettering fences out the abandoned execution (epoch 1 -> 2)
    assert row["run_epoch"] == 2

    assert CLEANED[job_id] == ["closed"]  # cleanup ran, once
    assert len(CAUGHT[job_id]) == 1
    assert 0.8 <= CAUGHT[job_id][0] <= 1.6


async def test_a_job_that_finishes_inside_its_deadline_is_stored_as_a_success(
    live_worker, unique_queue, db_pool
):
    """The same boundary, through the whole worker: claim, run, store.

    Each job returns with 100ms of its 1s deadline left, so the ``finished``
    write lands at or past the deadline. All three are successes with their
    real results.
    """
    await live_worker()

    job_ids = [
        await enqueue(
            db_pool,
            unique_queue,
            f"{THIS}.FinishesJustInsideItsDeadlineJob",
            {"margin": 0.1},
            {"timeout_seconds": 1, "on_timeout": "fail"},
        )
        for _ in range(3)
    ]

    for job_id in job_ids:
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=25)
        assert row["result"] == {"beat": "the deadline"}
        assert row["error_count"] == 0
        assert row["error_message"] is None
        ran = (row["finished"] - row["started"]).total_seconds()
        assert 0.85 <= ran <= 1.4, f"ran {ran:.3f}s of a 1s deadline"


# ============================================================================
# escape 2: abandoned synchronous threads
# ============================================================================


async def test_abandoned_job_threads_stop_the_worker_claiming_and_it_says_so(
    live_worker, unique_queue, db_pool
):
    """Two timed-out synchronous jobs fill a two-thread worker; it refuses.

    Each job blocks for 5s under a 1s deadline, so each leaves a thread
    running for ~4s after its row is already terminal. With the pool full,
    the old code went on claiming: ``to_thread`` queued behind the abandoned
    threads, the claimed job sat in ``running`` without ever starting, and
    the worker looked healthy while doing nothing. Now it claims nothing,
    logs at ERROR that it is refusing and why, and resumes the moment a
    thread frees a slot — so the third job waits in ``queued``, where any
    other worker could take it, and then runs normally.

    The threads are also this worker's own: a runaway job class can no
    longer starve the event loop's default executor, which the worker needs
    for its own reconnects.
    """
    THREADS.clear()
    ENTERED.clear()
    LEFT.clear()
    said: list[str] = []
    sink = logger.add(said.append, level="WARNING", format="{message}")
    try:
        worker = await live_worker(job_threads=2)

        blockers = [
            await enqueue(
                db_pool,
                unique_queue,
                f"{THIS}.BlocksLongPastItsDeadlineJob",
                {"block": 5.0},
                {"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 5},
            )
            for _ in range(2)
        ]
        for job_id in blockers:
            row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=25)
            assert row["error_message"] == "Job timed out after 1s"
            assert row["result"] is None  # the thread's "blocked" never lands

        # both threads are still running, and they are OURS, not asyncio's
        assert sorted(THREADS) == sorted(blockers)
        assert all(
            name.startswith(f"pyjobby-job-{unique_queue}") for name in THREADS.values()
        ), THREADS
        assert worker._live_job_threads() == 2
        assert set(LEFT) == set()  # neither has finished its 5s of blocking

        # a job the worker is perfectly capable of running, but must not take
        held = await enqueue(db_pool, unique_queue, f"{THIS}.PromptJob", {"x": 21}, {})
        refused_at = time.monotonic()
        for _ in range(10):
            await asyncio.sleep(0.15)
            state = await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", held)
            assert state == "queued", f"claimed at {time.monotonic() - refused_at:.2f}s"

        # ...and it says so, loudly, rather than going quiet
        refusals = [line for line in said if "NOT CLAIMING" in line]
        assert len(refusals) == 1, said
        assert f"{unique_queue}" in refusals[0]
        assert "2 abandoned job thread(s) fill this worker's pool of 2" in refusals[0]

        # the first abandoned thread finishes, the worker claims again
        row = await wait_for_job_state(db_pool, held, ("finished",), timeout=25)
        assert row["result"] == {"doubled": 42}
        assert row["error_count"] == 0

        freed = min(LEFT.values())
        waited = row["started"].timestamp() - row["created"].timestamp()
        assert waited >= 1.5, f"claimed after only {waited:.2f}s"
        assert time.monotonic() >= freed, "a thread must have finished first"
        assert worker._live_job_threads() <= 1

        resumed = [line for line in said if "Claiming again" in line]
        assert len(resumed) == 1, said
    finally:
        logger.remove(sink)
