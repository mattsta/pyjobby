"""
Tests for pj.py DAG continuation logic (schema v1).

Job dependencies (waitfor_job) and group coordination (run_group /
waitfor_group) driven through the REAL worker run() loop via the
``live_worker`` fixture — no mocks.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio

OK_JOB = "tests.dxe_jobs.OkJob"


async def enqueue(
    conn,
    queue: str,
    *,
    x: int = 1,
    state: str = "queued",
    waitfor_job: int | None = None,
    run_group: int | None = None,
    waitfor_group: int | None = None,
) -> int:
    """Insert an OkJob row on `queue` with optional dependency edges."""
    return await conn.fetchval(
        """INSERT INTO jorb
               (job_class, kwargs, queue, state, waitfor_job, run_group,
                waitfor_group)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           RETURNING id""",
        OK_JOB,
        {"x": x},
        queue,
        state,
        waitfor_job,
        run_group,
        waitfor_group,
    )


class TestDAGWaitForJob:
    """Test DAG continuation with waitfor_job dependencies."""

    async def test_waitfor_job_triggers_dependent_jobs(
        self, live_worker, unique_queue, db_pool
    ):
        """Completing a job triggers jobs waiting for it (waitfor_job)."""
        await live_worker()

        parent_id = await enqueue(db_pool, unique_queue, x=1)
        child_id = await enqueue(
            db_pool, unique_queue, x=2, state="waiting", waitfor_job=parent_id
        )

        parent_job = await wait_for_job_state(db_pool, parent_id, ("finished",))
        assert parent_job["result"] == {"doubled": 2}

        # child was woken by the parent's completion and then ran
        child_job = await wait_for_job_state(db_pool, child_id, ("finished",))
        assert child_job["result"] == {"doubled": 4}

        # the wakeup is visible in history: waiting -> queued -> ... -> finished
        events = [
            r["event"]
            for r in await db_pool.fetch(
                "SELECT event FROM jorb_history WHERE job_id=$1 ORDER BY id", child_id
            )
        ]
        assert events == ["enqueued", "queued", "claimed", "running", "finished"]

    async def test_waitfor_job_with_multiple_dependent_jobs(
        self, live_worker, unique_queue, db_pool
    ):
        """Test that one job can trigger multiple waiting jobs."""
        await live_worker()

        parent_id = await enqueue(db_pool, unique_queue, x=1)
        child_ids = [
            await enqueue(
                db_pool,
                unique_queue,
                x=10 + i,
                state="waiting",
                waitfor_job=parent_id,
            )
            for i in range(3)
        ]

        parent_job = await wait_for_job_state(db_pool, parent_id, ("finished",))
        assert parent_job["state"] == "finished"

        # All 3 children should get woken and finished
        for i, child_id in enumerate(child_ids):
            child_job = await wait_for_job_state(db_pool, child_id, ("finished",))
            assert child_job["result"] == {"doubled": (10 + i) * 2}


class TestDAGWaitForGroup:
    """Test DAG continuation with run_group/waitfor_group dependencies."""

    async def test_waitfor_group_triggers_after_all_group_jobs_finish(
        self, live_worker, unique_queue, db_pool
    ):
        """Jobs waiting for a group run only when ALL group jobs finish."""
        await live_worker()

        group_id = 12345
        group_job_ids = [
            await enqueue(db_pool, unique_queue, x=i, run_group=group_id)
            for i in range(3)
        ]
        waiting_job_id = await enqueue(
            db_pool, unique_queue, x=50, state="waiting", waitfor_group=group_id
        )

        for job_id in group_job_ids:
            await wait_for_job_state(db_pool, job_id, ("finished",))

        waiting_job = await wait_for_job_state(db_pool, waiting_job_id, ("finished",))
        assert waiting_job["result"] == {"doubled": 100}

    async def test_waitfor_group_not_triggered_until_all_finish(
        self, live_worker, unique_queue, db_pool
    ):
        """The waiter is NOT woken while any group job is unfinished."""
        await live_worker()

        group_id = 54321
        job1_id = await enqueue(db_pool, unique_queue, x=1, run_group=group_id)
        # second group member is dead-lettered (terminal, but NOT 'finished')
        job2_id = await enqueue(
            db_pool, unique_queue, x=2, state="crashed", run_group=group_id
        )
        waiting_job_id = await enqueue(
            db_pool, unique_queue, x=3, state="waiting", waitfor_group=group_id
        )

        await wait_for_job_state(db_pool, job1_id, ("finished",))
        # give the worker time to (incorrectly) wake the waiter if it would
        await asyncio.sleep(1.0)

        job2 = await db_pool.fetchrow("SELECT * FROM jorb WHERE id=$1", job2_id)
        waiting_job = await db_pool.fetchrow(
            "SELECT * FROM jorb WHERE id=$1", waiting_job_id
        )

        assert job2["state"] == "crashed"
        # not triggered: job2 never reached 'finished'
        assert waiting_job["state"] == "waiting", (
            f"Waiting job should stay waiting, got: {waiting_job['state']}"
        )
