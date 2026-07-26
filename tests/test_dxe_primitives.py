"""End-to-end tests of the DXE durable execution primitives.

Step checkpointing/replay, durable sleep, events, and durable messaging —
all against live workers and real job classes from ``tests.dxe_jobs``.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


async def test_step_checkpoint_resume_skips_completed_work(
    live_worker, unique_queue, db_pool
):
    """Crash at step 2 on attempt 1; attempt 2 fast-forwards step 1."""
    await live_worker()

    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, queue, admin_data)
           VALUES ($1,$2,$3) RETURNING id""",
        "tests.dxe_jobs.StepPipelineJob",
        unique_queue,
        {"max_retries": 3, "initial_retry_delay": 0},
    )

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"] == {"final": 14}
    assert row["error_count"] == 1  # exactly one failed attempt

    steps = await db_pool.fetch(
        """SELECT step_seq, name, output, error, run_epoch
           FROM jorb_step WHERE job_id = $1 ORDER BY step_seq""",
        job_id,
    )
    by_seq = {s["step_seq"]: s for s in steps}

    # step 1 was recorded by the FIRST attempt and never re-executed: its
    # checkpoint still carries that attempt's epoch after the retry
    first_attempt = by_seq[1]["run_epoch"]
    assert first_attempt == 1
    assert by_seq[1]["name"] == "fetch"
    assert by_seq[1]["output"] == {"n": 7}
    # step 2 failed on the first attempt, then re-executed and succeeded on a
    # later one (epochs only increase; they are not consecutive, because the
    # retry that abandons an attempt advances the fence too)
    retry_attempt = by_seq[2]["run_epoch"]
    assert retry_attempt > first_attempt
    assert by_seq[2]["name"] == "maybe-explode"
    assert by_seq[2]["error"] is None
    assert by_seq[2]["output"] == {"ok": True}
    # step 3 only ever ran on that same later attempt
    assert by_seq[3]["run_epoch"] == retry_attempt


async def test_durable_sleep_unwinds_and_resumes(live_worker, unique_queue, db_pool):
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.SleeperJob",
        {"seconds": 2},
        unique_queue,
    )

    # phase 1: the job runs, checkpoints the sleep, and goes back to queued
    # with a future run_after — it holds no worker while sleeping
    for _ in range(50):
        row = await db_pool.fetchrow(
            "SELECT state, run_after > now() AS future FROM jorb WHERE id=$1", job_id
        )
        if row["state"] == "queued" and row["future"]:
            break
        await asyncio.sleep(0.1)
    assert row["state"] == "queued" and row["future"], dict(row)

    ev = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='phase'", job_id
    )
    assert ev == {"at": "before-sleep"}

    # phase 2: after the wake time it resumes PAST the sleep and finishes
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=15)
    assert row["result"] == "woke"
    ev = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='phase'", job_id
    )
    assert ev == {"at": "after-sleep"}

    # the sleep is a recorded checkpoint
    sleep_step = await db_pool.fetchrow(
        "SELECT name, output FROM jorb_step WHERE job_id=$1 AND name='dxe.sleep'",
        job_id,
    )
    assert sleep_step is not None
    assert "wake_at" in sleep_step["output"]


async def test_send_recv_between_jobs(live_worker, unique_queue, db_pool):
    """PongJob blocks in recv on one worker; PingJob delivers from another."""
    await live_worker()
    await live_worker()  # second worker so ping isn't stuck behind pong

    pong_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.PongJob",
        {"timeout": 10},
        unique_queue,
    )
    await wait_for_job_state(db_pool, pong_id, ("running",))

    ping_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.PingJob",
        {"dest": pong_id},
        unique_queue,
    )

    pong = await wait_for_job_state(db_pool, pong_id, ("finished",), timeout=15)
    assert pong["result"] == {"got": {"ping": True}}

    ping = await wait_for_job_state(db_pool, ping_id, ("finished",), timeout=5)
    assert ping["result"] == "pinged"

    # the message was consumed exactly once
    unconsumed = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id=$1 AND consumed_at IS NULL",
        pong_id,
    )
    assert unconsumed == 0


async def test_recv_timeout_returns_none(live_worker, unique_queue, db_pool):
    await live_worker()

    pong_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.PongJob",
        {"timeout": 0.5},
        unique_queue,
    )
    row = await wait_for_job_state(db_pool, pong_id, ("finished",))
    assert row["result"] == {"got": None}
