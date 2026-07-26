"""Per-step timeouts: bounding one step instead of the whole job.

A job-level timeout tells you the job overran. It cannot tell you *which*
step hung, and one slow step spends the entire job budget on the way there.
``step(..., timeout=)`` / ``transaction(..., timeout=)`` — and the
``step_timeout`` class default behind them — bound a single step.

What is pinned here:

* a blown budget is recorded as **that step's** error, tagged
  ``StepTimeoutError`` so a reader (and ``pj-admin jobs steps``) can tell a
  timeout from an ordinary failure;
* it then takes the **ordinary retry path**: the next attempt fast-forwards
  the completed prefix and re-executes only the step that hung;
* per-call budget beats the class default, and ``timeout=0`` disables it;
* composition with the job's own deadline — only the tighter of the two
  bounds is ever armed, so they cannot race to report one overrun twice;
* the honest limit: a **synchronous** step that blocks the event loop cannot
  be interrupted by anything, and is not retroactively failed for it.

``transaction()``'s side — the write rolls back, the connection is not
poisoned, and both primitives produce the identical error — is pinned in
``tests/test_dxe_transactions.py`` next to the rest of the connection
mechanics.

Every "did this really execute?" claim is counted in the ``jorb_test_effect``
ledger rather than inferred from the checkpoint table.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, ClassVar

import pytest

from pyjobby.admin_api import AdminAPI
from pyjobby.pj import Job, JobSystem

from .conftest import wait_for_job_state
from .utils.faults import effect_counts, ensure_effects_table, record_effect

pytestmark = pytest.mark.asyncio


#: Cancellations observed *inside* step functions, as ``(job_id, step)``.
#: The live worker runs in this process, so job code can report directly
#: that the timeout really cancelled its coroutine rather than merely
#: abandoning it.
CANCELLED_IN_STEP: list[tuple[int, str]] = []


# ============================================================================
# job classes (resolved by live workers via their dotted path)
# ============================================================================


class TimingOutStepJob(Job):
    """A cheap step, then a step that hangs far past its budget.

    ``always`` keeps it hanging on every attempt (for the terminal case);
    otherwise only the first attempt hangs, so the retry can be observed
    fast-forwarding the prefix and re-running only the step that timed out.
    """

    async def task(
        self, tag: str, budget: float = 0.3, always: bool = False
    ) -> dict[str, Any]:
        await self.step("prep", self._prep, tag)
        hung = await self.step("hang", self._hang, tag, always, timeout=budget)
        return {"hang": hung}

    async def _prep(self, tag: str) -> dict[str, bool]:
        await record_effect(self.s.cxn, tag, self.job["id"], "prep")
        return {"prepped": True}

    async def _hang(self, tag: str, always: bool) -> dict[str, bool]:
        await record_effect(self.s.cxn, tag, self.job["id"], "hang")
        if always or self.job["error_count"] == 0:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                CANCELLED_IN_STEP.append((self.job["id"], "hang"))
                raise
        return {"ok": True}


class BudgetedStepJob(Job):
    """One step that naps ``nap`` seconds under a ``budget``-second budget."""

    async def task(self, nap: float, budget: float) -> dict[str, Any]:
        return {"nap": await self.step("nap", self._nap, nap, timeout=budget)}

    async def _nap(self, nap: float) -> dict[str, float]:
        await asyncio.sleep(nap)
        return {"napped": nap}


class ClassBudgetJob(Job):
    """Every step inherits ``step_timeout``; one call overrides it upward."""

    step_timeout: ClassVar[float] = 0.3

    async def task(self) -> str:
        await self.step("inside-the-class-budget", self._nap, 0.05)
        await self.step("overrides-the-class-budget", self._nap, 0.6, timeout=5)
        await self.step("hits-the-class-budget", self._nap, 30)
        return "unreachable"

    async def _nap(self, nap: float) -> dict[str, float]:
        await asyncio.sleep(nap)
        return {"napped": nap}


class BlockingStepJob(Job):
    """A *synchronous* step that blocks the loop past its budget.

    Nothing can interrupt it — the timer that would fire cannot run while the
    event loop is blocked. The documented behavior is that it runs to
    completion and its result stands.
    """

    async def task(self, block: float = 0.6) -> dict[str, Any]:
        return {"blocked": await self.step("block", self._block, block, timeout=0.1)}

    def _block(self, block: float) -> dict[str, float]:
        time.sleep(block)
        return {"slept": block}


class LooseBudgetJob(Job):
    """Declares a step budget far looser than the job's own deadline."""

    async def task(self) -> str:
        await self.step("hang", self._hang, timeout=60)
        return "unreachable"

    async def _hang(self) -> None:
        await asyncio.sleep(30)


class OwnTimeoutJob(Job):
    """A step that raises a ``TimeoutError`` of its own, well inside budget."""

    async def task(self) -> str:
        await self.step("call-out", self._call_out, timeout=10)
        return "unreachable"

    async def _call_out(self) -> None:
        async with asyncio.timeout(0.05):  # the step's own inner deadline
            await asyncio.sleep(30)


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


async def steps_of(pool, job_id: int) -> list[tuple[int, str, Any, Any, int]]:
    rows = await pool.fetch(
        """SELECT step_seq, name, output, error, run_epoch FROM jorb_step
           WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    return [
        (r["step_seq"], r["name"], r["output"], r["error"], r["run_epoch"])
        for r in rows
    ]


# ============================================================================
# the recorded checkpoint: which step, and that it was a timeout
# ============================================================================


async def test_a_blown_budget_is_recorded_as_a_timeout_against_that_step(
    live_worker, unique_queue, db_pool
):
    """The diagnosis is the point: the checkpoint names the step and the kind.

    A job-level timeout says only "this job overran". Here the step that hung
    is the one carrying the error, and the error is tagged with the exception
    type, so a timeout is distinguishable from an ordinary failure by reading
    the row — no schema change, and the tag is the first thing in the string
    so it survives the 40-column truncation ``pj-admin jobs steps`` applies.
    """
    await ensure_effects_table(db_pool)
    CANCELLED_IN_STEP.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.TimingOutStepJob",
        {"tag": unique_queue, "budget": 0.3, "always": True},
        {"max_retries": 1, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == "step 'hang' exceeded its 0.3s timeout"
    assert row["error_count"] == 1

    timed_out = "StepTimeoutError: step 'hang' exceeded its 0.3s timeout"
    assert await steps_of(db_pool, job_id) == [
        (1, "prep", {"prepped": True}, None, 1),
        (2, "hang", None, timed_out, 1),
    ]
    assert await effect_counts(db_pool, unique_queue) == {"prep": 1, "hang": 1}

    # the async step was really cancelled, not merely abandoned
    assert [(job_id, "hang")] == CANCELLED_IN_STEP

    # ...and the row `pj-admin jobs steps` reads carries the same tag,
    # still legible after the CLI truncates the column to 40 characters
    async with db_pool.acquire() as conn:
        listed = await AdminAPI(conn).get_job_steps(job_id)
    assert [(s["step_seq"], s["name"], s["error"]) for s in listed] == [
        (1, "prep", None),
        (2, "hang", timed_out),
    ]
    assert timed_out[:37].startswith("StepTimeoutError: step 'hang'")


async def test_an_ordinary_failure_and_a_timeout_are_told_apart_by_the_row(
    live_worker, unique_queue, db_pool
):
    """Same table, same column, two distinguishable kinds of failure."""
    await ensure_effects_table(db_pool)
    await live_worker()

    timed_out_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.TimingOutStepJob",
        {"tag": unique_queue, "budget": 0.3, "always": True},
        {"max_retries": 1, "initial_retry_delay": 0},
    )
    failed_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.dxe_jobs.StepPipelineJob",
        {},
        {"max_retries": 1, "initial_retry_delay": 0},
    )

    await wait_for_job_state(db_pool, timed_out_id, ("crashed",), timeout=20)
    await wait_for_job_state(db_pool, failed_id, ("crashed",), timeout=20)

    errors = await db_pool.fetch(
        """SELECT job_id, error FROM jorb_step
           WHERE job_id = ANY($1) AND error IS NOT NULL ORDER BY job_id""",
        [timed_out_id, failed_id],
    )
    assert [(r["job_id"], r["error"]) for r in errors] == [
        (timed_out_id, "StepTimeoutError: step 'hang' exceeded its 0.3s timeout"),
        (failed_id, "RuntimeError: mid-pipeline crash"),
    ]


async def test_a_steps_own_timeouterror_is_not_relabelled_as_a_blown_budget(
    live_worker, unique_queue, db_pool
):
    """Only the step's *budget* may be recorded as a step timeout.

    A step that manages its own deadline — an HTTP client, an inner
    ``asyncio.timeout`` — raises ``TimeoutError`` on its own account. Blindly
    converting that would make the checkpoint claim a budget expired when it
    had 9.95 of its 10 seconds left, which is worse than no diagnosis at all.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.OwnTimeoutJob",
        {},
        {"max_retries": 1, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == ""  # a bare TimeoutError carries no message
    assert await steps_of(db_pool, job_id) == [
        (1, "call-out", None, "TimeoutError: ", 1)
    ]


# ============================================================================
# what a timed-out step DOES: the ordinary retry path
# ============================================================================


async def test_the_retry_fast_forwards_the_prefix_and_re_runs_the_timed_out_step(
    live_worker, unique_queue, db_pool
):
    """A blown budget is a step failure, so retrying it is already cheap.

    The completed prefix fast-forwards (``prep`` executes once across both
    attempts, ledger-proved) and only the step that hung runs again. That is
    why a timeout retries rather than dead-letters: the job's existing retry
    budget and DLQ handle a step that keeps hanging, and nothing that already
    succeeded is repeated on the way there.
    """
    await ensure_effects_table(db_pool)
    CANCELLED_IN_STEP.clear()
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.TimingOutStepJob",
        {"tag": unique_queue, "budget": 0.3},
        {"max_retries": 3, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"hang": {"ok": True}}
    assert row["error_count"] == 1  # exactly one timed-out attempt

    # prep ran once and was replayed; hang ran on both attempts
    assert await effect_counts(db_pool, unique_queue) == {"prep": 1, "hang": 2}
    assert [(job_id, "hang")] == CANCELLED_IN_STEP

    recorded = await steps_of(db_pool, job_id)
    prefix_epoch = recorded[0][4]
    assert recorded[0] == (1, "prep", {"prepped": True}, None, prefix_epoch)
    assert prefix_epoch == 1  # still the first attempt's: never re-executed
    assert recorded[1][:4] == (2, "hang", {"ok": True}, None)
    assert recorded[1][4] > prefix_epoch  # re-executed on the retry
    assert len(recorded) == 2


async def test_a_step_inside_its_budget_is_unaffected(
    live_worker, unique_queue, db_pool
):
    """The control case: a budget nobody exceeds changes nothing."""
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.BudgetedStepJob",
        {"nap": 0.05, "budget": 5},
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"nap": {"napped": 0.05}}
    assert row["error_count"] == 0
    assert await steps_of(db_pool, job_id) == [(1, "nap", {"napped": 0.05}, None, 1)]


# ============================================================================
# where the budget is declared
# ============================================================================


async def test_the_class_default_applies_and_a_call_can_override_it(
    live_worker, unique_queue, db_pool
):
    """``step_timeout`` is the job's default; ``timeout=`` is the exception.

    A step's sensible budget is a property of the work, so it belongs on the
    call that names that work; a class-wide default exists because most jobs
    want one number for all of their steps. Both are proved in one run: step
    1 passes under the inherited 0.3s, step 2 needs 0.6s and says so, step 3
    inherits 0.3s and hangs.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.ClassBudgetJob",
        {},
        {"max_retries": 1, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == (
        "step 'hits-the-class-budget' exceeded its 0.3s timeout"
    )
    assert await steps_of(db_pool, job_id) == [
        (1, "inside-the-class-budget", {"napped": 0.05}, None, 1),
        (2, "overrides-the-class-budget", {"napped": 0.6}, None, 1),
        (
            3,
            "hits-the-class-budget",
            None,
            "StepTimeoutError: step 'hits-the-class-budget' exceeded its 0.3s timeout",
            1,
        ),
    ]


# ============================================================================
# composition with the job-level timeout
# ============================================================================


async def test_only_the_tighter_of_the_two_deadlines_is_ever_armed(db_params):
    """The composition rule, isolated: ``_dxe_budget`` arms one bound.

    A per-step budget is installed only while it is strictly tighter than the
    time the job has left. Once the job's own deadline is the binding
    constraint, the step budget is not armed at all and the job timeout is
    left to fire alone — so a step timeout can never outlive the job's
    deadline, and the two can never race to report a single overrun as two
    different failures.
    """
    system = JobSystem(
        dsn=db_params, qname="unused", capabilities=("test",), workerId=0
    )
    job = Job(s=system, job={"id": 1, "run_epoch": 1})

    # no job deadline (a Job built outside a worker): declared budgets stand
    assert job._dxe_deadline is None
    assert job._dxe_budget(5) == 5
    assert job._dxe_budget(None) is None  # no class default either
    assert job._dxe_budget(0) is None  # explicitly disabled for this call

    class Defaulted(Job):
        step_timeout: ClassVar[float] = 2.5

    defaulted = Defaulted(s=system, job={"id": 1, "run_epoch": 1})
    assert defaulted._dxe_budget(None) == 2.5  # class default
    assert defaulted._dxe_budget(5) == 5  # per-call wins, even upward
    assert defaulted._dxe_budget(0) is None  # per-call disables it

    # the job has plenty of time left: the step budget is the tighter bound
    job._dxe_deadline = time.monotonic() + 100
    assert job._dxe_budget(5) == 5

    # the job's deadline is now the tighter bound: nothing is armed for the
    # step, and the job timeout owns the outcome
    job._dxe_deadline = time.monotonic() + 0.5
    assert job._dxe_budget(5) is None
    assert job._dxe_budget(None) is None

    # ...and that holds once the deadline is already behind us
    job._dxe_deadline = time.monotonic() - 1
    assert job._dxe_budget(5) is None


async def test_a_step_budget_subdivides_a_generous_job_budget(
    live_worker, unique_queue, db_pool
):
    """A 0.3s step inside a 30s job fails as a *step* timeout, at 0.3s."""
    await ensure_effects_table(db_pool)
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.TimingOutStepJob",
        {"tag": unique_queue, "budget": 0.3, "always": True},
        {"max_retries": 1, "initial_retry_delay": 0, "timeout_seconds": 30},
    )

    # the 30s job budget never gets to expire — the wait itself proves it
    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=15)
    assert row["error_message"] == "step 'hang' exceeded its 0.3s timeout"
    assert row["timeout_at"] is None  # cleared by the terminal transition

    step = await db_pool.fetchrow(
        "SELECT name, error FROM jorb_step WHERE job_id = $1 AND step_seq = 2", job_id
    )
    assert (step["name"], step["error"]) == (
        "hang",
        "StepTimeoutError: step 'hang' exceeded its 0.3s timeout",
    )


async def test_the_job_deadline_still_fires_under_a_looser_step_budget(
    live_worker, unique_queue, db_pool
):
    """A 60s step budget inside a 1s job: the job timeout wins, as itself.

    The step budget is never armed (it is looser than the job's remaining
    time), so the failure is reported as the job-level timeout with the
    job's ``on_timeout`` policy applied to it — not mislabeled as a step
    timeout, and not retried past the policy.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.LooseBudgetJob",
        {},
        {
            "max_retries": 5,
            "initial_retry_delay": 0,
            "timeout_seconds": 1,
            "on_timeout": "fail",
        },
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
    assert row["error_message"] == "Job timed out after 1s"
    assert row["error_count"] == 1  # on_timeout=fail: terminal on attempt 1

    # the whole task was cancelled, so no step got to record anything
    assert await steps_of(db_pool, job_id) == []


# ============================================================================
# the honest limit: a blocking synchronous step
# ============================================================================


async def test_a_blocking_synchronous_step_is_not_interrupted(
    live_worker, unique_queue, db_pool
):
    """Documented, and pinned so the documentation cannot quietly inflate.

    A timeout is delivered as a cancellation at an await point. A
    synchronous function that blocks the event loop has no await point and
    starves the very timer that would fire, so *nothing* can interrupt it —
    not the step budget, not the job's in-process deadline. It runs to
    completion and its result stands: failing a step whose work already
    succeeded would discard a real result to enforce a bound that was never
    enforceable. Long synchronous loops poll ``self.cancelled`` instead.
    """
    await live_worker()

    job_id = await enqueue(
        db_pool,
        unique_queue,
        "tests.test_dxe_step_timeouts.BlockingStepJob",
        {"block": 0.6},  # six times its 0.1s budget
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    assert row["result"] == {"blocked": {"slept": 0.6}}
    assert row["error_count"] == 0

    step = await db_pool.fetchrow(
        """SELECT name, output, error,
                  EXTRACT(EPOCH FROM (finished - started))::float AS seconds
           FROM jorb_step WHERE job_id = $1""",
        job_id,
    )
    assert (step["name"], step["output"], step["error"]) == (
        "block",
        {"slept": 0.6},
        None,
    )
    assert step["seconds"] >= 0.6  # it really did overrun, uninterrupted


# ============================================================================
# one implementation for both primitives
# ============================================================================


async def test_step_and_transaction_share_one_budget_implementation():
    """Both primitives must execute user code through the same helper.

    Structural, because a second copy of the budget logic would drift on the
    first change to either one. The behavioral half of this — both producing
    the identical ``StepTimeoutError`` and checkpoint — is in
    ``tests/test_dxe_transactions.py``.
    """
    assert "_dxe_invoke(" in inspect.getsource(Job.step)
    assert "_dxe_invoke(" in inspect.getsource(Job.transaction)
    assert "timeout" in inspect.signature(Job.step).parameters
    assert "timeout" in inspect.signature(Job.transaction).parameters
