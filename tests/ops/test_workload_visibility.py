"""Work the fleet cannot see, and the controls that steer it -- live.

TROUBLESHOOTING.md's hardest promise is about jobs that fail silently by
never failing: queued, runnable, and invisible to every live worker. Its
claims -- doctor's unclaimable sweep names the cause per queue, `jobs why`
answers with a stable reason code and the numbers, the documented remedy
un-hides the work -- plus the live queue controls (pause stops claims
sub-second without touching the running job; max-concurrency is exact, not
approximate) all get induced here against real workers.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import registered_workers, wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


async def raw_enqueue(db_pool, queue: str, prio: int = 100, capability=None) -> int:
    """The back door the docs blame: a row that arrived without JobClient,
    so nothing could refuse it at the caller."""
    return await db_pool.fetchval(
        "INSERT INTO jorb (queue, job_class, kwargs, prio, capability, state) "
        "VALUES ($1, 'tests.dxe_jobs.OkJob', '{\"x\": 1}'::jsonb, $2, $3, "
        "'queued') RETURNING id",
        queue,
        prio,
        capability,
    )


class TestPauseWithWorkInFlight:
    async def test_pause_stops_claims_but_not_the_running_job(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        running_id = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=4
        )
        await wait_for_job_state(db_pool, running_id, ("running",), timeout=30)
        parked = [
            await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=n)
            for n in range(3)
        ]

        paused = admin("queues", "pause", unique_queue)
        assert paused.returncode == 0

        show = admin("queues", "show", unique_queue)
        assert "yes" in show.stdout.lower(), show.stdout

        # The running job is not touched by a pause...
        row = await wait_for_job_state(db_pool, running_id, ("finished",), timeout=30)
        assert row["result"] == "done"
        # ...and with the worker now idle and the queue paused, nothing else
        # is claimed.
        await asyncio.sleep(2)
        states = [
            r["state"]
            for r in await db_pool.fetch(
                "SELECT state FROM jorb WHERE id = ANY($1)", parked
            )
        ]
        assert states == ["queued", "queued", "queued"]

        assert admin("queues", "resume", unique_queue).returncode == 0
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) = 3 FROM jorb WHERE id = ANY($1) "
                "AND state = 'finished'",
                parked,
            ),
            describe="parked jobs drained after resume",
            timeout=30,
        )


class TestUnclaimablePriority:
    async def test_doctor_names_it_why_explains_it_and_set_priority_frees_it(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)  # ceiling 1000
        await wait_until(
            lambda: registered_workers(db_pool, unique_queue),
            describe="worker registered",
            timeout=30,
        )
        job_id = await raw_enqueue(db_pool, unique_queue, prio=5000)

        report = admin("doctor")
        assert report.returncode == 0, "unclaimable is a WARN, never a FAIL"
        assert "WARN unclaimable:" in report.stdout
        assert "above every live worker's ceiling" in report.stdout
        assert "jobs why" in report.stdout

        why = admin("jobs", "why", str(job_id), "--json")
        answer = json.loads(why.stdout)
        assert answer["reason"] == "above_worker_ceiling"

        # The worker with nothing to do says what is hiding above it.
        await wait_until(
            lambda: asyncio.sleep(
                0, "ABOVE this worker's priority ceiling" in fleet.procs[0].log_text()
            ),
            describe="idle worker logged the hidden work",
            timeout=90,
        )

        # The documented remedy, then the job actually runs.
        fixed = admin("jobs", "set-priority", str(job_id), "900")
        assert fixed.returncode == 0
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)


class TestUnclaimableCapability:
    async def test_doctor_names_the_missing_capability_and_a_capable_worker_frees_it(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue, "--cap", "cpu")
        await wait_until(
            lambda: registered_workers(db_pool, unique_queue),
            describe="worker registered",
            timeout=30,
        )
        job_id = await raw_enqueue(db_pool, unique_queue, capability="gpu")

        report = admin("doctor")
        assert report.returncode == 0
        assert "WARN unclaimable:" in report.stdout
        assert "needing capability 'gpu'" in report.stdout
        assert "they advertise: cpu" in report.stdout

        why = admin("jobs", "why", str(job_id), "--json")
        answer = json.loads(why.stdout)
        assert answer["reason"] == "capability_unmet"

        # The documented remedy: start a worker advertising it.
        fleet.worker(unique_queue, "--cap", "gpu")
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert admin("doctor").stdout.count("WARN unclaimable") == 0


class TestExactConcurrencyLimit:
    async def test_max_concurrency_one_is_never_exceeded_by_a_racing_fleet(
        self, fleet, admin, db_pool, unique_queue
    ):
        limits = admin("queues", "limits", unique_queue, "--max-concurrency", "1")
        assert limits.returncode == 0, limits.stdout + limits.stderr

        fleet.worker(unique_queue, workers=4)
        client = JobClient(pool=db_pool)
        job_ids = [
            await client.enqueue(
                "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=0.5
            )
            for _ in range(6)
        ]

        # Sample the in-flight count the whole way down the drain. "Exact,
        # not approximate" means the cap holds at every observation.
        max_seen = 0
        for _ in range(600):
            in_flight = await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE id = ANY($1) "
                "AND state IN ('claimed', 'running')",
                job_ids,
            )
            max_seen = max(max_seen, in_flight)
            assert max_seen <= 1, f"cap of 1 exceeded: {max_seen} in flight"
            done = await db_pool.fetchval(
                "SELECT count(*) = 6 FROM jorb WHERE id = ANY($1) "
                "AND state = 'finished'",
                job_ids,
            )
            if done:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("queue did not drain under the cap")
