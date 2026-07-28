"""
Hypothesis Property Tests for Live Producer-Consumer Workflows

These tests run ACTUAL pyjobby workers against live PostgreSQL to verify
real-world producer-consumer scenarios under property-based testing.

This is advanced integration testing that:
1. Spawns real worker processes
2. Generates random job creation patterns
3. Verifies all jobs are processed correctly
4. Tests concurrent producers and consumers
5. Validates system stability under various loads

WARNING: These tests are SLOW (spawn real processes, wait for processing)
"""

import asyncio
import multiprocessing
import signal
import time

import asyncpg
import pytest
from hypothesis import HealthCheck, Phase, assume, given, settings
from hypothesis import strategies as st

from pyjobby.pj import JobSystem
from tests.utils.factories import create_job

pytestmark = [pytest.mark.asyncio, pytest.mark.hypothesis, pytest.mark.slow]


# ============================================================================
# Test Job Implementations
# ============================================================================


class SuccessJob:
    """Simple job that always succeeds for testing."""

    def __init__(self, s, job):
        self.s = s
        self.job = job

    def task(self, **kwargs):
        """Record that this job ran."""
        return {"status": "success", "job_id": self.job["id"], "timestamp": time.time()}


class CountingJob:
    """Job that increments a counter in worker cache."""

    def __init__(self, s, job):
        self.s = s
        self.job = job

    def task(self, **kwargs):
        """Increment counter."""
        count = self.s.cache.get("job_count", 0)
        self.s.cache["job_count"] = count + 1
        return {"count": count + 1, "job_id": self.job["id"]}


class SlowJob:
    """Job that takes time to complete."""

    def __init__(self, s, job):
        self.s = s
        self.job = job

    async def task(self, sleep_time: float = 0.1, **kwargs):
        """Sleep for specified time."""
        await asyncio.sleep(sleep_time)
        return {"slept": sleep_time, "job_id": self.job["id"]}


# ============================================================================
# Helper Functions
# ============================================================================


def run_worker_process(
    worker_id: int,
    queue: str,
    db_params: dict,
    duration_seconds: int = 10,
    job_sleep_seconds: float = 0,
):
    """Run a single worker process for specified duration.

    A simplified worker loop matching the schema v1 statements: it registers
    in jorb_worker (so the monitor's dead-worker sweep can reclaim its jobs
    if it dies), claims with its registry id, and drives the epoch-fenced
    run/finished/crashed statements.

    ``job_sleep_seconds`` holds each job in 'running' for that long, so a
    test can kill the process while it demonstrably owns work."""

    def signal_handler(signum, frame):
        """Handle shutdown signal."""
        pass

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    async def run_worker():
        """Run worker async."""
        worker = JobSystem(
            dsn=db_params,
            qname=queue,
            capabilities=("test",),
            workerId=worker_id,
            checkInterval=1,  # Check frequently for tests
        )

        # Connect to DB (shared factory registers both json AND jsonb codecs)
        from pyjobby import db as pjdb

        worker.cxn = await pjdb.connect(**db_params)

        # Register in the worker registry (as the real worker loop does)
        from pyjobby import __version__
        from pyjobby.pj import STMTS, WORKER_REGISTER_SQL

        registry_id = await worker.cxn.fetchval(
            WORKER_REGISTER_SQL,
            worker.node,
            worker.pid,
            queue,
            list(worker.capabilities),
            worker.prio,
            __version__,
            worker.job_threads,
        )

        worker.stmts = {}
        for name, stmt in STMTS.items():
            worker.stmts[name] = await worker.cxn.prepare(stmt)

        # Run for specified duration
        start_time = time.time()
        while time.time() - start_time < duration_seconds and not worker.stop:
            # Poll for job
            jobs = await worker.ex(
                "claim",
                worker.pid,
                worker.node,
                worker.qname,
                worker.capabilities,
                worker.prio,
                registry_id,
            )

            if jobs:
                job = jobs[0]
                epoch = job["run_epoch"]
                try:
                    # Mark as running (no deadline for these driver loops)
                    await worker.ex("run", job["id"], epoch, None)

                    # Execute job (simplified - no class loading for speed)
                    if job_sleep_seconds:
                        await asyncio.sleep(job_sleep_seconds)
                    result = {"status": "success", "job_id": job["id"]}

                    # Mark as finished
                    await worker.ex("finished", job["id"], result, epoch)

                except Exception as e:
                    # Mark as crashed (terminal DLQ)
                    await worker.ex("crashed", job["id"], str(e), "", epoch)
            else:
                # Sleep if no jobs
                await asyncio.sleep(0.1)

        await worker.cxn.close()

    # Run the worker
    asyncio.run(run_worker())


async def wait_for_jobs_completion(
    conn: asyncpg.Connection,
    job_ids: list[int],
    timeout_seconds: int = 30,
    check_interval: float = 0.5,
) -> bool:
    """
    Wait for all jobs to reach terminal state (finished/crashed/cancelled).

    Returns True if all completed, False if timeout.
    """
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        # Check if all jobs are in terminal states
        result = await conn.fetchval(
            """SELECT COUNT(*) FROM jorb
               WHERE id = ANY($1::bigint[])
                 AND state IN ('finished', 'crashed', 'cancelled')""",
            job_ids,
        )

        if result == len(job_ids):
            return True

        await asyncio.sleep(check_interval)

    return False


async def get_job_states(
    conn: asyncpg.Connection, job_ids: list[int]
) -> dict[str, int]:
    """Get count of jobs in each state."""
    results = await conn.fetch(
        """SELECT state, COUNT(*) as count FROM jorb
           WHERE id = ANY($1::bigint[])
           GROUP BY state""",
        job_ids,
    )

    return {row["state"]: row["count"] for row in results}


# ============================================================================
# Property Tests: Live Worker Integration
# ============================================================================


@pytest.mark.hypothesis
class TestLiveProducerConsumerWorkflows:
    """Property tests with real workers processing real jobs."""

    @settings(
        max_examples=10,  # Fewer examples because these are slow
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
        phases=[Phase.explicit, Phase.reuse, Phase.generate],
    )
    @given(
        job_count=st.integers(min_value=1, max_value=20),
        worker_count=st.integers(min_value=1, max_value=3),
    )
    async def test_all_jobs_eventually_processed(
        self, db_params, job_count: int, worker_count: int
    ):
        """
        Property: Given N jobs and M workers, all N jobs should eventually be processed.

        This is the CORE producer-consumer invariant.
        """
        assume(job_count >= worker_count)  # Ensure enough work

        from pyjobby import db as pjdb

        conn = await pjdb.connect(**db_params)

        try:
            # Create jobs
            job_ids = []
            for i in range(job_count):
                job_id = await create_job(
                    conn,
                    state="queued",
                    queue="test",
                    admin_data={"test_job_number": i},
                )
                job_ids.append(job_id)

            # Start workers
            workers = []
            for i in range(worker_count):
                p = multiprocessing.Process(
                    daemon=True,
                    target=run_worker_process,
                    args=(i, "test", db_params, 15),  # Run for 15 seconds
                )
                p.start()
                workers.append(p)

            # Wait for jobs to complete
            completed = await wait_for_jobs_completion(
                conn, job_ids, timeout_seconds=20
            )

            # Stop workers
            for p in workers:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)
                    if p.is_alive():
                        p.kill()
                        p.join()

            # Verify completion
            assert completed, f"Not all jobs completed within timeout"

            # Get final states
            states = await get_job_states(conn, job_ids)

            # INVARIANT: All jobs should be finished (or crashed, but should be finished)
            finished_count = states.get("finished", 0)
            crashed_count = states.get("crashed", 0)

            # At least 90% should have finished successfully
            success_rate = finished_count / job_count
            assert success_rate >= 0.9, (
                f"Success rate too low: {success_rate:.1%} ({finished_count}/{job_count} finished, {crashed_count} crashed)"
            )

            # INVARIANT: No jobs should remain in claimed/running state
            assert states.get("claimed", 0) == 0, "Jobs stuck in claimed state"
            assert states.get("running", 0) == 0, "Jobs stuck in running state"

        finally:
            # Cleanup
            await conn.execute("DELETE FROM jorb")
            await conn.close()

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
        phases=[Phase.explicit, Phase.reuse, Phase.generate],
    )
    @given(
        job_count=st.integers(min_value=5, max_value=15),
    )
    async def test_no_duplicate_processing(self, db_params, job_count: int):
        """
        Property: Each job should be processed exactly once (no duplicates).

        This tests the SKIP LOCKED mechanism.
        """
        from pyjobby import db as pjdb

        conn = await pjdb.connect(**db_params)

        try:
            # Create jobs with unique markers
            job_ids = []
            for i in range(job_count):
                job_id = await create_job(
                    conn,
                    state="queued",
                    queue="test",
                    admin_data={"unique_id": f"job-{i}"},
                )
                job_ids.append(job_id)

            # Start multiple workers (high concurrency to stress test)
            worker_count = 3
            workers = []
            for i in range(worker_count):
                p = multiprocessing.Process(
                    daemon=True,
                    target=run_worker_process,
                    args=(i, "test", db_params, 10),
                )
                p.start()
                workers.append(p)

            # Wait for completion
            await wait_for_jobs_completion(conn, job_ids, timeout_seconds=15)

            # Stop workers
            for p in workers:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)
                    if p.is_alive():
                        p.kill()
                        p.join()

            # INVARIANT: Each job should have run_count = 1 (processed exactly once)
            results = await conn.fetch(
                """SELECT id, run_count FROM jorb WHERE id = ANY($1::bigint[])""",
                job_ids,
            )

            for row in results:
                assert row["run_count"] == 1, (
                    f"Job {row['id']} was processed {row['run_count']} times (expected 1)"
                )

        finally:
            await conn.execute("DELETE FROM jorb")
            await conn.close()

    @settings(
        max_examples=5,  # Very slow test
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
        phases=[Phase.explicit, Phase.reuse, Phase.generate],
    )
    @given(
        job_batches=st.lists(
            st.integers(min_value=1, max_value=5), min_size=2, max_size=4
        ),
    )
    async def test_continuous_producer_consumer(
        self, db_params, job_batches: list[int]
    ):
        """
        Property: Continuous producers adding jobs should all be consumed.

        Tests producer-consumer pattern with jobs arriving over time.
        """
        from pyjobby import db as pjdb

        conn = await pjdb.connect(**db_params)

        try:
            # Start workers first
            worker_count = 2
            workers = []
            for i in range(worker_count):
                p = multiprocessing.Process(
                    daemon=True,
                    target=run_worker_process,
                    args=(i, "test", db_params, 20),  # Run longer
                )
                p.start()
                workers.append(p)

            # Produce jobs in batches over time
            all_job_ids = []
            for batch_idx, batch_size in enumerate(job_batches):
                # Create batch of jobs
                batch_ids = []
                for i in range(batch_size):
                    job_id = await create_job(
                        conn,
                        state="queued",
                        queue="test",
                        admin_data={"batch": batch_idx, "job_in_batch": i},
                    )
                    batch_ids.append(job_id)
                    all_job_ids.append(job_id)

                # Wait a bit before next batch (simulate real-world job arrival)
                await asyncio.sleep(1)

            # Wait for all jobs to complete
            completed = await wait_for_jobs_completion(
                conn, all_job_ids, timeout_seconds=25
            )

            # Stop workers
            for p in workers:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)
                    if p.is_alive():
                        p.kill()
                        p.join()

            # INVARIANT: All jobs across all batches should be processed
            assert completed, "Not all jobs completed"

            states = await get_job_states(conn, all_job_ids)
            finished_count = states.get("finished", 0)

            success_rate = finished_count / len(all_job_ids)
            assert success_rate >= 0.9, (
                f"Success rate: {success_rate:.1%} ({finished_count}/{len(all_job_ids)})"
            )

        finally:
            await conn.execute("DELETE FROM jorb")
            await conn.close()

    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
        phases=[Phase.explicit, Phase.reuse, Phase.generate],
    )
    @given(
        job_count=st.integers(min_value=5, max_value=10),
    )
    async def test_worker_crash_and_recovery(self, db_params, job_count: int):
        """
        Property: If a worker crashes, its in-flight jobs are reclaimed and
        finished by another worker.

        Schema v1 moved recovery out of the worker: a killed worker never
        deregisters, so its ``jorb_worker.last_seen`` goes stale and the
        monitor's dead-worker sweep requeues everything it was holding.
        """
        from pyjobby import db as pjdb
        from pyjobby.monitor import sweep_dead_workers

        conn = await pjdb.connect(**db_params)
        pool = await pjdb.create_pool(**db_params, min_size=1, max_size=2)

        try:
            # Create jobs
            job_ids = []
            for i in range(job_count):
                job_id = await create_job(
                    conn, state="queued", queue="test", admin_data={"job_number": i}
                )
                job_ids.append(job_id)

            # Start a worker that holds each job long enough to still own one
            # when we kill it
            worker1 = multiprocessing.Process(
                daemon=True,
                target=run_worker_process,
                args=(1, "test", db_params, 30, 10),
            )
            worker1.start()

            # Let it register and claim some jobs
            await asyncio.sleep(2)

            # KILL the worker (simulate crash) — no graceful deregistration
            if worker1.is_alive():
                worker1.kill()
                worker1.join()

            # Let the dead worker's heartbeat age past the liveness grace
            await asyncio.sleep(2)

            in_flight = await conn.fetchval(
                """SELECT count(*) FROM jorb
                   WHERE id = ANY($1::bigint[])
                     AND state IN ('claimed', 'running')""",
                job_ids,
            )
            assert in_flight >= 1, "worker died without owning any job"

            # The monitor sweep reclaims exactly the orphaned in-flight jobs
            requeued = await sweep_dead_workers(pool, liveness_grace_seconds=1)
            assert requeued == in_flight, (
                f"sweep requeued {requeued} jobs, {in_flight} were orphaned"
            )

            still_stuck = await conn.fetchval(
                """SELECT count(*) FROM jorb
                   WHERE id = ANY($1::bigint[])
                     AND state IN ('claimed', 'running')""",
                job_ids,
            )
            assert still_stuck == 0, "orphaned jobs left in claimed/running"

            # The crashed worker is retired from the registry
            live_workers = await conn.fetchval(
                "SELECT count(*) FROM jorb_worker WHERE shutdown_at IS NULL"
            )
            assert live_workers == 0

            # A fresh worker picks the requeued jobs back up
            worker2 = multiprocessing.Process(
                daemon=True,
                target=run_worker_process,
                args=(2, "test", db_params, 15),
            )
            worker2.start()

            # Wait for all jobs to complete
            completed = await wait_for_jobs_completion(
                conn, job_ids, timeout_seconds=20
            )

            # Stop worker
            if worker2.is_alive():
                worker2.terminate()
                worker2.join(timeout=2)
                if worker2.is_alive():
                    worker2.kill()
                    worker2.join()

            # INVARIANT: All jobs should eventually be processed despite crash
            assert completed, "Not all jobs completed after crash recovery"

            states = await get_job_states(conn, job_ids)
            assert states.get("finished", 0) == job_count, (
                f"expected every job finished after recovery, got {states}"
            )

        finally:
            await pool.close()
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_worker")
            await conn.close()


# ============================================================================
# Property Tests: Priority and Capability Routing (Live)
# ============================================================================


@pytest.mark.hypothesis
class TestLivePriorityAndCapability:
    """Property tests for priority and capability with real workers."""

    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(
        high_prio_count=st.integers(min_value=1, max_value=5),
        low_prio_count=st.integers(min_value=1, max_value=5),
    )
    async def test_priority_ordering_in_practice(
        self, db_params, high_prio_count: int, low_prio_count: int
    ):
        """
        Property: High priority jobs should be processed before low priority jobs.
        """
        from pyjobby import db as pjdb

        conn = await pjdb.connect(**db_params)

        try:
            # Create high priority jobs
            high_prio_ids = []
            for i in range(high_prio_count):
                job_id = await create_job(
                    conn,
                    state="queued",
                    queue="test",
                    prio=10,
                    admin_data={"priority": "high", "order": i},
                )
                high_prio_ids.append(job_id)

            # Create low priority jobs
            low_prio_ids = []
            for i in range(low_prio_count):
                job_id = await create_job(
                    conn,
                    state="queued",
                    queue="test",
                    prio=1000,
                    admin_data={"priority": "low", "order": i},
                )
                low_prio_ids.append(job_id)

            # Start ONE worker (to enforce ordering)
            worker = multiprocessing.Process(
                daemon=True, target=run_worker_process, args=(1, "test", db_params, 15)
            )
            worker.start()

            # Wait for all to complete
            all_ids = high_prio_ids + low_prio_ids
            await wait_for_jobs_completion(conn, all_ids, timeout_seconds=20)

            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)
                if worker.is_alive():
                    worker.kill()
                    worker.join()

            # Check completion timestamps
            high_prio_times = await conn.fetch(
                """SELECT id, updated FROM jorb
                   WHERE id = ANY($1::bigint[]) AND state = 'finished'
                   ORDER BY updated""",
                high_prio_ids,
            )

            low_prio_times = await conn.fetch(
                """SELECT id, updated FROM jorb
                   WHERE id = ANY($1::bigint[]) AND state = 'finished'
                   ORDER BY updated""",
                low_prio_ids,
            )

            # INVARIANT: Last high priority job should finish before first low priority job
            if high_prio_times and low_prio_times:
                last_high_prio = high_prio_times[-1]["updated"]
                first_low_prio = low_prio_times[0]["updated"]

                assert last_high_prio <= first_low_prio, (
                    "Low priority job finished before all high priority jobs"
                )

        finally:
            await conn.execute("DELETE FROM jorb")
            await conn.close()
