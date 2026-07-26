"""Tests for pyjobby.monitor — the platform's single background reaper.

Covers every sweep the monitor daemon performs:

- timeout enforcement: running jobs past ``timeout_at`` are requeued with
  backoff (same row) or dead-lettered to state='crashed' per ``on_timeout``
  and ``max_retries``
- dead-worker reclaim: in-flight jobs of workers whose ``jorb_worker``
  heartbeat went stale are requeued and the workers retired
- unregistered-claim reclaim: 'claimed' jobs with no registry reference are
  requeued after a grace period
- run_epoch fencing: a monitor requeue makes the superseded execution's
  completion a no-op
- the ``monitor()`` loop wires the sweeps together
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from pyjobby.monitor import (
    handle_timed_out_job,
    monitor,
    sweep_dead_workers,
    sweep_timed_out_jobs,
    sweep_unregistered_claims,
)
from pyjobby.pj import STMTS
from tests.conftest import wait_for_job_state
from tests.utils.processes import dsn_from

pytestmark = pytest.mark.asyncio


# ============================================================================
# helpers
# ============================================================================


async def insert_job(
    pool,
    queue: str,
    *,
    state: str = "queued",
    job_class: str = "tests.dxe_jobs.OkJob",
    kwargs: dict | None = None,
    admin_data: dict | None = None,
    error_count: int = 0,
    run_epoch: int = 0,
    claimed_by: int | None = None,
    timeout_at_offset_seconds: float | None = None,
) -> int:
    """Insert one jorb row in the state a test needs (JSONB never NULL)."""
    return await pool.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, admin_data, state,
                          error_count, run_epoch, claimed_by, worker_host,
                          worker_pid, timeout_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'test-host', 4242,
                CASE WHEN $9::float8 IS NULL THEN NULL
                     ELSE now() + make_interval(secs => $9) END)
        RETURNING id
        """,
        job_class,
        kwargs or {},
        queue,
        admin_data or {},
        state,
        error_count,
        run_epoch,
        claimed_by,
        timeout_at_offset_seconds,
    )


async def insert_worker(pool, queue: str, *, last_seen_age_seconds: float = 0) -> int:
    """Insert a jorb_worker registry row with a heartbeat this old."""
    return await pool.fetchval(
        """
        INSERT INTO jorb_worker (host, pid, queue, capabilities, version,
                                 last_seen)
        VALUES ('test-host', 4242, $1, '{test}', 'test',
                now() - make_interval(secs => $2))
        RETURNING id
        """,
        queue,
        last_seen_age_seconds,
    )


async def get_job(pool, job_id: int):
    return await pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)


async def hand_claim(pool, job_id: int) -> int:
    """Simulate a worker claim: bump run_epoch and move to running.

    Returns the new epoch (the fencing token for this simulated attempt)."""
    return await pool.fetchval(
        """
        UPDATE jorb
        SET state = 'running',
            run_count = run_count + 1,
            run_epoch = run_epoch + 1,
            started = now(),
            updated = now()
        WHERE id = $1
        RETURNING run_epoch
        """,
        job_id,
    )


# ============================================================================
# timeout enforcement
# ============================================================================


class TestHandleTimedOutJob:
    """The per-job retry/dead-letter decision."""

    async def test_retry_requeues_same_row_with_backoff(self, db_pool, unique_queue):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "retry", "max_retries": 5},
            timeout_at_offset_seconds=-10,
        )

        before = await db_pool.fetchval("SELECT now()")
        await handle_timed_out_job(db_pool, job_id, "test.Job", {"max_retries": 5}, 0)

        job = await get_job(db_pool, job_id)
        assert job["id"] == job_id  # SAME row — no retry-copy rows in v1
        assert job["state"] == "queued"
        assert job["error_count"] == 1
        assert job["timeout_at"] is None
        assert "Timeout exceeded" in job["error_message"]
        # backoff pushed run_after into the future
        assert job["run_after"] > before

    async def test_on_timeout_fail_dead_letters(self, db_pool, unique_queue):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "fail"},
            timeout_at_offset_seconds=-10,
        )

        await handle_timed_out_job(
            db_pool, job_id, "test.Job", {"on_timeout": "fail"}, 0
        )

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"  # terminal: the DLQ
        assert job["error_count"] == 1
        assert job["timeout_at"] is None
        assert job["finished"] is not None
        assert "on_timeout=fail" in job["error_message"]

    async def test_max_retries_exhausted_dead_letters(self, db_pool, unique_queue):
        admin = {"on_timeout": "retry", "max_retries": 3}
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data=admin,
            error_count=2,  # attempt = 3 == max_retries
            timeout_at_offset_seconds=-10,
        )

        await handle_timed_out_job(db_pool, job_id, "test.Job", admin, 2)

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"
        assert job["error_count"] == 3
        assert "max retries exceeded" in job["error_message"]

    async def test_ignores_job_no_longer_running(self, db_pool, unique_queue):
        """The UPDATE is guarded on state='running': a job that finished
        between detection and handling is left alone."""
        job_id = await insert_job(db_pool, unique_queue, state="finished")

        await handle_timed_out_job(db_pool, job_id, "test.Job", {}, 0)

        job = await get_job(db_pool, job_id)
        assert job["state"] == "finished"
        assert job["error_count"] == 0


class TestSweepTimedOutJobs:
    """The batch sweep over running jobs past their deadline."""

    async def test_sweep_requeues_overdue_and_leaves_the_rest(
        self, db_pool, unique_queue
    ):
        overdue = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )
        not_due = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=3600
        )
        no_timeout = await insert_job(db_pool, unique_queue, state="running")

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1

        assert (await get_job(db_pool, overdue))["state"] == "queued"
        assert (await get_job(db_pool, not_due))["state"] == "running"
        assert (await get_job(db_pool, no_timeout))["state"] == "running"

    async def test_sweep_dead_letters_when_policy_says_fail(
        self, db_pool, unique_queue
    ):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "fail"},
            timeout_at_offset_seconds=-5,
        )

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"

    async def test_sweep_respects_batch_size(self, db_pool, unique_queue):
        ids = [
            await insert_job(
                db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
            )
            for _ in range(3)
        ]

        handled = await sweep_timed_out_jobs(db_pool, batch_size=2)
        assert handled == 2

        states = [(await get_job(db_pool, job_id))["state"] for job_id in ids]
        assert states.count("queued") == 2
        assert states.count("running") == 1

        # a second sweep finishes the backlog
        handled = await sweep_timed_out_jobs(db_pool, batch_size=2)
        assert handled == 1

    async def test_timeout_retry_recorded_in_history(self, db_pool, unique_queue):
        """The requeue is a state change, so the jorb_history trigger records
        the attempt trail on the ONE job id."""
        job_id = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )

        await sweep_timed_out_jobs(db_pool)

        events = await db_pool.fetch(
            "SELECT event, detail FROM jorb_history WHERE job_id = $1 ORDER BY id",
            job_id,
        )
        assert [e["event"] for e in events] == ["enqueued", "queued"]
        assert events[-1]["detail"]["from"] == "running"
        assert events[-1]["detail"]["error_count"] == 1


# ============================================================================
# dead-worker reclaim
# ============================================================================


class TestSweepDeadWorkers:
    async def test_requeues_jobs_of_stale_worker_and_retires_it(
        self, db_pool, unique_queue
    ):
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        claimed = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=dead_worker
        )
        running = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            claimed_by=dead_worker,
            timeout_at_offset_seconds=3600,
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 2

        for job_id in (claimed, running):
            job = await get_job(db_pool, job_id)
            assert job["state"] == "queued"
            assert job["timeout_at"] is None
            # immediately claimable again
            assert job["run_after"] <= await db_pool.fetchval("SELECT now()")

        # the stale worker is retired from the registry
        worker = await db_pool.fetchrow(
            "SELECT * FROM jorb_worker WHERE id = $1", dead_worker
        )
        assert worker["shutdown_at"] is not None

    async def test_leaves_live_workers_and_their_jobs_alone(
        self, db_pool, unique_queue
    ):
        live = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=1)
        job_id = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=live
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 0

        assert (await get_job(db_pool, job_id))["state"] == "running"
        worker = await db_pool.fetchrow("SELECT * FROM jorb_worker WHERE id = $1", live)
        assert worker["shutdown_at"] is None

    async def test_ignores_workers_already_shut_down(self, db_pool, unique_queue):
        """A gracefully-exited worker (shutdown_at set) is not rescanned; its
        terminal jobs stay put."""
        retired = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=300)
        await db_pool.execute(
            "UPDATE jorb_worker SET shutdown_at = now() WHERE id = $1", retired
        )
        # a finished job from that worker must not be requeued
        job_id = await insert_job(
            db_pool, unique_queue, state="finished", claimed_by=retired
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "finished"

    async def test_recovered_job_is_executed_by_a_real_worker(
        self, live_worker, db_pool, unique_queue
    ):
        """End to end: a job orphaned by a dead worker is requeued by the
        sweep and then actually claimed and finished by a live worker."""
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            kwargs={"x": 5},
            claimed_by=dead_worker,
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 1

        await live_worker()

        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["result"] == {"doubled": 10}
        # the live worker's claim bumped the fencing token
        assert row["run_epoch"] == 1


# ============================================================================
# unregistered-claim reclaim
# ============================================================================


class TestSweepUnregisteredClaims:
    async def test_requeues_stale_unregistered_claim(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "queued"
        assert job["timeout_at"] is None

    async def test_leaves_fresh_claims_alone(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "claimed"

    async def test_leaves_registered_claims_to_the_dead_worker_sweep(
        self, db_pool, unique_queue
    ):
        """Claims with a registry reference are the dead-worker sweep's
        business, however old they are."""
        worker_id = await insert_worker(db_pool, unique_queue)
        job_id = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=worker_id
        )
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "claimed"


# ============================================================================
# run_epoch fencing
# ============================================================================


class TestEpochFencing:
    async def test_monitor_requeue_fences_out_stale_finish(self, db_pool, unique_queue):
        """After the monitor requeues a timed-out job and a new attempt
        claims it, the ORIGINAL execution's finish must be a no-op."""
        job_id = await insert_job(db_pool, unique_queue)

        # attempt 1 claims (epoch 1), starts running, and stalls past its
        # deadline
        stale_epoch = await hand_claim(db_pool, job_id)
        assert stale_epoch == 1
        await db_pool.execute(
            "UPDATE jorb SET timeout_at = now() - interval '5 seconds' WHERE id = $1",
            job_id,
        )

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1
        assert (await get_job(db_pool, job_id))["state"] == "queued"

        # attempt 2 claims the requeued row (epoch 2)
        new_epoch = await hand_claim(db_pool, job_id)
        assert new_epoch == 2

        # the zombie from attempt 1 comes back and tries to complete
        fenced = await db_pool.fetch(
            STMTS["finished"], job_id, {"from": "stale-attempt"}, stale_epoch
        )
        assert fenced == []

        job = await get_job(db_pool, job_id)
        assert job["state"] == "running"  # attempt 2 still owns the row
        assert job["result"] is None
        assert job["run_epoch"] == 2

        # ...while the current attempt's completion succeeds
        done = await db_pool.fetch(
            STMTS["finished"], job_id, {"from": "current-attempt"}, new_epoch
        )
        assert len(done) == 1
        job = await get_job(db_pool, job_id)
        assert job["state"] == "finished"
        assert job["result"] == {"from": "current-attempt"}

    async def test_monitor_requeue_fences_out_stale_retry(self, db_pool, unique_queue):
        """A superseded attempt cannot push the job back to queued either
        (its retry statement is epoch-fenced exactly like its finish)."""
        job_id = await insert_job(db_pool, unique_queue)
        stale_epoch = await hand_claim(db_pool, job_id)
        await db_pool.execute(
            "UPDATE jorb SET timeout_at = now() - interval '5 seconds' WHERE id = $1",
            job_id,
        )
        await sweep_timed_out_jobs(db_pool)
        await hand_claim(db_pool, job_id)  # epoch 2 owns the row

        fenced = await db_pool.fetch(
            STMTS["retry"],
            job_id,
            datetime.timedelta(seconds=1),
            "stale error",
            "stale backtrace",
            stale_epoch,
        )
        assert fenced == []
        job = await get_job(db_pool, job_id)
        assert job["state"] == "running"
        assert job["run_epoch"] == 2


# ============================================================================
# the monitor loop
# ============================================================================


class TestMonitorLoop:
    async def test_loop_runs_all_sweeps(self, db_pool, unique_queue, db_params):
        """monitor() repeatedly sweeps timeouts, dead workers, and
        unregistered claims until cancelled."""
        timed_out = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=dead_worker
        )
        unregistered = await insert_job(db_pool, unique_queue, state="claimed")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            unregistered,
        )

        task = asyncio.create_task(
            monitor(
                # this session's database, not the base DSN: under xdist each
                # worker owns its own database and the monitor must sweep the
                # one holding this test's rows
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                claimed_grace_seconds=300,
            )
        )
        try:
            for job_id in (timed_out, orphaned, unregistered):
                await wait_for_job_state(db_pool, job_id, ("queued",), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        worker = await db_pool.fetchrow(
            "SELECT * FROM jorb_worker WHERE id = $1", dead_worker
        )
        assert worker["shutdown_at"] is not None
