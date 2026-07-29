"""More than one of everything: the coordination claims, held to.

OPERATIONS.md's process table says one monitor and one scheduler, "(more
are safe)"; the scheduler's own help promises row-locked schedules and
deadline-key dedupe across instances. And a rolling worker restart --
SIGTERM old, start new, while jobs flow -- must lose nothing and run
nothing twice. Real processes, racing for real.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


class TestTwoMonitors:
    async def test_dead_worker_reclaim_happens_exactly_once(
        self, fleet, db_pool, unique_queue
    ):
        """Both monitors sweep the same corpse; FOR UPDATE SKIP LOCKED must
        make exactly one of them requeue the job. A double requeue is
        visible as an extra run_epoch bump, so pin the epoch and hold it
        pinned across several further sweep cycles."""
        fleet.monitor(liveness_grace=3.0, check_interval=0.5)
        fleet.monitor(liveness_grace=3.0, check_interval=0.5)
        worker_a = fleet.worker(unique_queue)

        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.EpochSleeperJob", queue=unique_queue, seconds=600
        )
        await wait_for_job_state(db_pool, job_id, ("running",), timeout=30)
        worker_a.signal_group(signal.SIGKILL)

        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'queued'", job_id
            ),
            describe="job reclaimed",
            timeout=20,
        )
        # One reclaim = epoch exactly 2 (1 for the claim, 1 for the requeue).
        assert (
            await db_pool.fetchval("SELECT run_epoch FROM jorb WHERE id = $1", job_id)
            == 2
        )
        # Both monitors keep sweeping a queue with a queued, runnable job on
        # it; the epoch must not move again.
        await asyncio.sleep(3)
        row = await db_pool.fetchrow(
            "SELECT state, run_epoch, run_count FROM jorb WHERE id = $1", job_id
        )
        assert (row["state"], row["run_epoch"], row["run_count"]) == ("queued", 2, 1)


class TestTwoSchedulers:
    async def test_a_due_schedule_fires_exactly_once(
        self, fleet, admin, db_pool, unique_queue, test_id
    ):
        name = f"sched_{test_id}"
        added = admin(
            "schedule",
            "add",
            name,
            "tests.dxe_jobs.OkJob",
            "* * * * *",
            "--queue",
            unique_queue,
        )
        assert added.returncode == 0, added.stdout + added.stderr

        fleet.scheduler(poll_interval=1)
        fleet.scheduler(poll_interval=1)
        # Make it due NOW, with both schedulers already polling -- the race
        # the row lock and deadline key exist for.
        await db_pool.execute(
            "UPDATE jorb_schedule SET next_run = now() - interval '5 seconds' "
            "WHERE name = $1",
            name,
        )

        schedule_id = await db_pool.fetchval(
            "SELECT id FROM jorb_schedule WHERE name = $1", name
        )
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE schedule_id = $1", schedule_id
            ),
            describe="schedule fired",
            timeout=20,
        )
        # Give the other scheduler every chance to double-fire, then count.
        await asyncio.sleep(3)
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
            )
            == 1
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_schedule_log WHERE schedule_id = $1 "
                "AND result = 'success'",
                schedule_id,
            )
            == 1
        )
        # And the schedule marches on rather than staying due forever.
        assert await db_pool.fetchval(
            "SELECT next_run > now() FROM jorb_schedule WHERE id = $1", schedule_id
        )


class TestRollingRestart:
    async def test_rolling_the_fleet_loses_nothing_and_runs_nothing_twice(
        self, fleet, db_pool, unique_queue
    ):
        """Enqueue first (start order does not matter), drain through worker
        A, roll to worker B mid-drain with SIGTERM. Every job must finish
        exactly once: SIGTERM abandons nothing, so no run_count may exceed 1
        and nothing may crash or linger."""
        client = JobClient(pool=db_pool)
        job_ids = [
            await client.enqueue(
                "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=0.4
            )
            for _ in range(12)
        ]

        worker_a = fleet.worker(unique_queue, workers=2)
        # Wait for the drain to be genuinely mid-flight before rolling.
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) >= 3 FROM jorb WHERE id = ANY($1) "
                "AND state = 'finished'",
                job_ids,
            ),
            describe="drain under way",
            timeout=30,
        )
        fleet.worker(unique_queue, workers=2)
        worker_a.signal_launcher(signal.SIGTERM)
        assert worker_a.wait(timeout=30) == 0

        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) = 12 FROM jorb WHERE id = ANY($1) "
                "AND state = 'finished'",
                job_ids,
            ),
            describe="every job finished",
            timeout=60,
        )
        counts = await db_pool.fetch(
            "SELECT id, run_count, state FROM jorb WHERE id = ANY($1)", job_ids
        )
        assert all(r["run_count"] == 1 for r in counts), [dict(r) for r in counts]
