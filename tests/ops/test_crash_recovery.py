"""Crash and recovery: the claims a durable job platform lives or dies on.

OPERATIONS.md promises, verbatim: a SIGKILLed worker corrupts nothing and
its in-flight jobs are reclaimed once its registry row goes stale; run-epoch
fencing keeps the killed attempt from writing anything after reclaim; a DXE
job resumes from its last completed step; SIGTERM finishes the current job,
stops claiming, and exits cleanly; a stranded claim with no live worker
behind it is requeued by age. Each test here inflicts the real failure on a
real process fleet and holds the platform to the promise.
"""

from __future__ import annotations

import asyncio
import signal

import asyncpg
import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import registered_workers, wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


async def job_state(pool: asyncpg.Pool, job_id: int) -> str:
    return await pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)


class TestSigkill:
    async def test_killed_worker_is_reclaimed_and_job_resumes_from_last_step(
        self, fleet, db_pool, unique_queue
    ):
        fleet.monitor(liveness_grace=3.0, check_interval=0.5)
        worker_a = fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.FirstAttemptBlocksStepJob", queue=unique_queue
        )

        # Wait until step "first" is checkpointed and the job is inside
        # "blocker" -- the mid-job moment the docs are about.
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb_step WHERE job_id = $1 AND name = 'first' "
                "AND finished IS NOT NULL",
                job_id,
            ),
            describe="first step checkpointed",
            timeout=30,
        )
        assert await job_state(db_pool, job_id) == "running"

        worker_a.signal_group(signal.SIGKILL)

        # "its in-flight jobs are reclaimed by the monitor's dead-worker
        # sweep once the worker's registry row goes stale" -- and the row
        # itself is retired.
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'queued'", job_id
            ),
            describe="job reclaimed to queued",
            timeout=20,
        )
        retired = await registered_workers(db_pool, unique_queue, live=False)
        assert retired, "the killed worker's registry row must be retired"

        # A fresh worker picks the job up and it RESUMES: the recorded
        # "first" checkpoint is replayed (original epoch inside), only
        # "blocker" re-executes, on the new epoch. Epoch arithmetic: 0 at
        # enqueue, +1 per claim, +1 per requeue, +1 on the terminal
        # transition (every exit from an attempt fences it) -- so the first
        # execution runs at 1, the reclaim leaves 2, the second claim runs
        # at 3, and the finished row rests at 4.
        fleet.worker(unique_queue)
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert row["result"] == {"first": {"epoch": 1}, "blocker": {"epoch": 3}}
        assert row["run_count"] == 2
        assert row["run_epoch"] == 4

        steps = {
            r["name"]: r["run_epoch"]
            for r in await db_pool.fetch(
                "SELECT name, run_epoch FROM jorb_step WHERE job_id = $1", job_id
            )
        }
        assert steps == {"first": 1, "blocker": 3}


class TestSigterm:
    async def test_sigterm_finishes_current_job_claims_nothing_more_and_exits_zero(
        self, fleet, db_pool, unique_queue
    ):
        worker = fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        running_id = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=3
        )
        await wait_for_job_state(db_pool, running_id, ("running",), timeout=30)

        # A second job arrives while the first is running; SIGTERM must not
        # let the worker start it.
        parked_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, x=1
        )
        worker.signal_launcher(signal.SIGTERM)

        assert worker.wait(timeout=30) == 0
        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", running_id)
        assert row["state"] == "finished"
        assert row["result"] == "done"
        assert await job_state(db_pool, parked_id) == "queued"

        live = await registered_workers(db_pool, unique_queue, live=True)
        assert live == [], "graceful exit must retire the registry row"


class TestRunEpochFencing:
    async def test_stalled_execution_cannot_write_after_reclaim(
        self, fleet, db_pool, unique_queue
    ):
        """SIGSTOP makes an honest zombie: the worker stops heartbeating,
        the monitor reclaims its job, a second worker finishes it -- and
        then the first worker wakes up and tries to store its own result.
        Run-epoch fencing must refuse that write."""
        fleet.monitor(liveness_grace=2.0, check_interval=0.5)
        worker_a = fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.EpochSleeperJob", queue=unique_queue, seconds=4
        )
        await wait_for_job_state(db_pool, job_id, ("running",), timeout=30)

        worker_a.signal_group(signal.SIGSTOP)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'queued' "
                "AND run_epoch = 2",
                job_id,
            ),
            describe="job reclaimed from stalled worker",
            timeout=20,
        )

        # The second claim runs at epoch 3 (0 at enqueue, +1 per claim,
        # +1 per requeue); the winning result must be the second worker's.
        fleet.worker(unique_queue)
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert row["result"] == {"epoch": 3}
        settled = (row["state"], row["result"], row["run_epoch"], row["finished"])

        # Wake the zombie and give it time to finish its sleep and attempt
        # the write. Nothing about the finished row may change.
        worker_a.signal_group(signal.SIGCONT)
        with pytest.raises(TimeoutError):
            await wait_until(
                lambda: db_pool.fetchrow(
                    "SELECT 1 FROM jorb WHERE id = $1 AND "
                    "(state, result, run_epoch, finished) IS DISTINCT FROM "
                    "($2::jorbstate, $3::jsonb, $4, $5)",
                    job_id,
                    *settled,
                ),
                describe="zombie overwrote the finished row (must never happen)",
                timeout=8,
            )


class TestLivenessMisconfiguration:
    async def test_monitor_warns_when_grace_cannot_be_beaten_by_the_heartbeat(
        self, fleet
    ):
        """A --liveness-grace below the worker heartbeat cadence requeues
        jobs from LIVE workers mid-run, forever. The monitor cannot refuse
        (the fleet may run a faster --heartbeat-interval) but it must say
        so at startup -- this warning is how the operational-validation
        harness itself discovered the failure mode."""
        monitor = fleet.monitor(liveness_grace=1.0)

        def warned() -> bool:
            return "under twice the default worker heartbeat interval" in (
                monitor.log_text()
            )

        deadline = asyncio.get_running_loop().time() + 20
        while not warned():
            assert asyncio.get_running_loop().time() < deadline, monitor.log_text()
            await asyncio.sleep(0.2)


class TestStrandedClaim:
    async def test_stale_claim_with_no_registered_worker_is_requeued_by_age(
        self, fleet, db_pool, unique_queue
    ):
        """A worker that claimed and died before registering leaves a
        'claimed' row nothing heartbeats for; the monitor requeues it by
        claim AGE after --claimed-grace, bumping run_epoch to fence the
        claimer out if it somehow returns."""
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=2)
        await db_pool.execute(
            "UPDATE jorb SET state = 'claimed', updated = now() - interval '60s' "
            "WHERE id = $1",
            job_id,
        )

        fleet.monitor(claimed_grace=2.0, check_interval=0.5)
        # The faked claim never went through claim_jorb, so the epoch is
        # still 0; the requeue's bump to 1 is what fences the claimer out.
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'queued' "
                "AND run_epoch = 1",
                job_id,
            ),
            describe="stranded claim requeued with epoch bumped",
            timeout=20,
        )
