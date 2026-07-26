"""End-to-end tests of the schema-v1 worker core against live workers.

Covers the DXE-1 lifecycle guarantees: history trail, same-row retries into
the terminal DLQ, queue pause/resume, running-job cancellation, and the
worker registry. All tests drive REAL JobSystem workers via the
``live_worker`` fixture and real jobs from ``tests.dxe_jobs``.
"""

from __future__ import annotations

import asyncio

import pytest

from pyjobby import db

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


async def test_success_path_records_full_history(live_worker, unique_queue, db_pool):
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.OkJob",
        {"x": 21},
        unique_queue,
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"] == {"doubled": 42}
    assert row["started"] is not None
    assert row["finished"] is not None
    assert row["run_epoch"] == 1

    events = [
        r["event"]
        for r in await db_pool.fetch(
            "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
        )
    ]
    assert events == ["enqueued", "claimed", "running", "finished"]


async def test_retries_reuse_row_and_dead_letter(live_worker, unique_queue, db_pool):
    await live_worker()

    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        "tests.dxe_jobs.FailJob",
        {},
        unique_queue,
        {"max_retries": 2, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("crashed",))
    # terminal 'crashed' IS the DLQ; the job kept one id across attempts
    assert row["error_count"] == 2
    # run_count is the attempt counter; run_epoch is only a monotonic fence
    # (it also advances on the retry that abandons an attempt)
    assert row["run_count"] == 2
    assert row["run_epoch"] >= 2
    assert "intentional failure" in row["error_message"]

    attempts = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_history WHERE job_id=$1 AND event='running'", job_id
    )
    assert attempts == 2


async def test_queue_pause_blocks_claims_until_resume(
    live_worker, unique_queue, db_pool
):
    await live_worker()

    await db_pool.execute(
        """INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)
           ON CONFLICT (name) DO UPDATE SET paused = TRUE""",
        unique_queue,
    )
    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.OkJob",
        {"x": 1},
        unique_queue,
    )

    await asyncio.sleep(1.0)
    state = await db_pool.fetchval("SELECT state FROM jorb WHERE id=$1", job_id)
    assert state == "queued", "paused queue must not be claimed from"

    await db_pool.execute(
        "UPDATE jorb_queue SET paused = FALSE WHERE name = $1", unique_queue
    )
    await wait_for_job_state(db_pool, job_id, ("finished",))


async def test_cancel_running_job(live_worker, unique_queue, db_pool):
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.SlowJob",
        {"seconds": 30},
        unique_queue,
    )

    await wait_for_job_state(db_pool, job_id, ("running",))

    outcome = await db.cancel_job(db_pool, job_id)
    assert outcome == "cancel_requested"

    row = await wait_for_job_state(db_pool, job_id, ("cancelled",), timeout=5)
    assert row["finished"] is not None


async def test_cancel_queued_job_is_immediate(live_worker, unique_queue, db_pool):
    # no worker needed: queued cancellation is direct
    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, run_after)
           VALUES ($1,$2,$3, now() + interval '1 hour') RETURNING id""",
        "tests.dxe_jobs.OkJob",
        {},
        unique_queue,
    )
    outcome = await db.cancel_job(db_pool, job_id)
    assert outcome == "cancelled"
    state = await db_pool.fetchval("SELECT state FROM jorb WHERE id=$1", job_id)
    assert state == "cancelled"


async def test_worker_registry_lifecycle(live_worker, unique_queue, db_pool):
    system = await live_worker()

    row = await db_pool.fetchrow(
        "SELECT * FROM jorb_worker WHERE queue = $1 ORDER BY id DESC LIMIT 1",
        unique_queue,
    )
    assert row is not None
    assert row["shutdown_at"] is None
    assert row["capabilities"] == ["test"]
    assert row["pid"] == system.pid

    system.stop = True
    system._wake.set()  # nudge the sleeping loop
    for _ in range(50):
        done = await db_pool.fetchval(
            "SELECT shutdown_at FROM jorb_worker WHERE id = $1", row["id"]
        )
        if done:
            break
        await asyncio.sleep(0.1)
    assert done is not None, "graceful stop must close the registry row"


async def test_epoch_fences_stale_completion(live_worker, unique_queue, db_pool):
    """A requeued job's old execution cannot overwrite the new attempt."""
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.SlowJob",
        {"seconds": 30},
        unique_queue,
    )
    row = await wait_for_job_state(db_pool, job_id, ("running",))
    stale_epoch = row["run_epoch"]

    # simulate the monitor requeueing (worker presumed dead)
    await db.requeue_job(
        db_pool, job_id, allowed_states=("claimed", "running"), reset_errors=False
    )

    # the stale execution's epoch-fenced 'finished' must be a no-op
    result = await db_pool.execute(
        """UPDATE jorb SET state='finished' WHERE id=$1
           AND state IN ('claimed','running') AND run_epoch=$2""",
        job_id,
        stale_epoch,
    )
    assert result == "UPDATE 0"
