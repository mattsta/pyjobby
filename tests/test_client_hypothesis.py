#!/usr/bin/env python3
"""
Hypothesis Property-Based Tests for JobClient.

Tests JobClient operations with property-based testing using Hypothesis
to verify correctness across a wide range of inputs against a live database.

All tests use:
- Actual JobClient API (no mocks)
- Live PostgreSQL database
- Real worker STMTS for job processing
- Hypothesis for generating test cases
"""

import pytest
import asyncio
import json
from datetime import datetime, timedelta, timezone
from hypothesis import given, strategies as st, settings, assume, HealthCheck
from typing import Dict, Any

from pyjobby.client import JobClient
from pyjobby.pj import STMTS


# =============================================================================
# Hypothesis Strategies
# =============================================================================

@st.composite
def job_kwargs_strategy(draw):
    """Generate valid job kwargs dictionaries."""
    return {
        "test_id": draw(st.integers(min_value=0, max_value=10000)),
        "message": draw(st.text(min_size=0, max_size=100)),
        "value": draw(st.floats(allow_nan=False, allow_infinity=False) | st.integers()),
        "flag": draw(st.booleans())
    }


@st.composite
def priority_strategy(draw):
    """Generate valid priority values."""
    return draw(st.integers(min_value=1, max_value=1000))


@st.composite
def queue_name_strategy(draw):
    """Generate valid queue names."""
    return draw(st.sampled_from(["default", "critical", "background", "test_queue"]))


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
async def job_client(db_pool):
    """Create a JobClient using the test database pool."""
    client = JobClient(pool=db_pool)
    yield client
    await client.close()


# =============================================================================
# Helper Functions
# =============================================================================

async def claim_and_finish_job(conn, queue="default"):
    """Claim a job and mark it as finished."""
    claimed = await conn.fetchrow(
        STMTS["claim"],
        12345,  # worker_pid
        "test-worker",
        queue,
        [],  # capabilities
        1000  # max_priority
    )
    if claimed:
        await conn.execute(STMTS["run"], claimed["id"])
        await conn.execute(
            STMTS["finished"],
            claimed["id"],
            json.dumps({"result": "success"})
        )
    return claimed


# =============================================================================
# Property-Based Tests: JobClient.enqueue()
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestEnqueueProperties:
    """Property-based tests for JobClient.enqueue()."""

    @settings(max_examples=50, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        kwargs=job_kwargs_strategy(),
        priority=priority_strategy(),
        queue=queue_name_strategy()
    )
    async def test_enqueue_creates_claimable_job(self, db_pool, job_client, kwargs, priority, queue):
        """Property: Enqueued job can be claimed by worker."""
        # PRODUCER: Enqueue job using JobClient
        job_id = await job_client.enqueue(
            "test.Job",
            queue=queue,
            priority=priority,
            **kwargs
        )

        assert job_id is not None
        assert isinstance(job_id, int)
        assert job_id > 0

        # CONSUMER: Worker can claim the job
        async with db_pool.acquire() as conn:
            claimed = await conn.fetchrow(
                STMTS["claim"],
                12345,
                "test-worker",
                queue,
                [],
                1000
            )

            assert claimed is not None
            assert claimed["id"] == job_id
            assert claimed["state"] == "claimed"
            assert claimed["queue"] == queue
            assert claimed["prio"] == priority

            # Cleanup: Delete job created in this example
            await conn.execute("DELETE FROM jorb WHERE id = $1", job_id)

    @settings(max_examples=30, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        job_count=st.integers(min_value=1, max_value=20),
        priority=priority_strategy()
    )
    async def test_enqueue_preserves_priority_order(self, db_pool, job_client, job_count, priority):
        """Property: Jobs are claimed in priority order (lower prio number = higher priority)."""
        # PRODUCER: Enqueue jobs with different priorities
        job_priorities = []
        for i in range(job_count):
            prio = priority + (i * 10)  # Ascending priorities
            job_id = await job_client.enqueue(
                "test.Job",
                priority=prio,
                test_index=i
            )
            job_priorities.append((job_id, prio))

        # CONSUMER: Claim jobs and verify priority order
        async with db_pool.acquire() as conn:
            claimed_priorities = []
            for _ in range(job_count):
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345,
                    "test-worker",
                    "default",
                    [],
                    10000  # High enough to claim all
                )
                if claimed:
                    claimed_priorities.append(claimed["prio"])

            # Verify priorities are in ascending order (lower = higher priority)
            assert claimed_priorities == sorted(claimed_priorities)

            # Cleanup: Delete jobs created in this example
            job_ids = [job_id for job_id, _ in job_priorities]
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

    @settings(max_examples=30, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        delay_seconds=st.integers(min_value=1, max_value=10)
    )
    async def test_enqueue_respects_run_after(self, db_pool, job_client, delay_seconds):
        """Property: Jobs with run_after are not claimable until specified time."""
        future_time = datetime.now() + timedelta(seconds=delay_seconds)

        # PRODUCER: Enqueue job for future
        job_id = await job_client.enqueue(
            "test.Job",
            run_after=future_time,
            test="delayed"
        )

        # CONSUMER: Cannot claim immediately
        async with db_pool.acquire() as conn:
            claimed_early = await conn.fetchrow(
                STMTS["claim"],
                12345,
                "test-worker",
                "default",
                [],
                1000
            )
            assert claimed_early is None  # Too early

            # Verify job exists but is not ready
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "queued"
            assert job["run_after"] > datetime.now()

            # Cleanup: Delete job created in this example
            await conn.execute("DELETE FROM jorb WHERE id = $1", job_id)


# =============================================================================
# Property-Based Tests: JobClient.enqueue_batch()
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestBatchEnqueueProperties:
    """Property-based tests for JobClient.enqueue_batch()."""

    @settings(max_examples=30, deadline=15000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        batch_size=st.integers(min_value=1, max_value=50)
    )
    async def test_batch_enqueue_creates_all_jobs(self, db_pool, job_client, batch_size):
        """Property: Batch enqueue creates exactly N claimable jobs."""
        # PRODUCER: Batch enqueue
        jobs = [("test.Job", {"index": i}) for i in range(batch_size)]
        job_ids = await job_client.enqueue_batch(jobs)

        assert len(job_ids) == batch_size
        assert len(set(job_ids)) == batch_size  # All unique

        # CONSUMER: All jobs are claimable
        async with db_pool.acquire() as conn:
            claimed_count = 0
            while True:
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345 + claimed_count,  # Different worker IDs
                    f"worker-{claimed_count}",
                    "default",
                    [],
                    1000
                )
                if not claimed:
                    break
                claimed_count += 1

            assert claimed_count == batch_size

            # Cleanup: Delete jobs created in this example
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

    @settings(max_examples=20, deadline=15000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        batch_size=st.integers(min_value=2, max_value=20),
        priority=priority_strategy()
    )
    async def test_batch_enqueue_same_priority(self, db_pool, job_client, batch_size, priority):
        """Property: Batch enqueued jobs with same priority are all claimable."""
        # PRODUCER: Batch enqueue with same priority
        jobs = [("test.Job", {"index": i}) for i in range(batch_size)]
        job_ids = await job_client.enqueue_batch(jobs, priority=priority)

        # CONSUMER: Claim all and verify priority
        async with db_pool.acquire() as conn:
            claimed_priorities = []
            for _ in range(batch_size):
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345,
                    "test-worker",
                    "default",
                    [],
                    10000
                )
                if claimed:
                    claimed_priorities.append(claimed["prio"])

            # All should have the same priority
            assert len(claimed_priorities) == batch_size
            assert all(p == priority for p in claimed_priorities)

            # Cleanup: Delete jobs created in this example
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)


# =============================================================================
# Property-Based Tests: Job Dependencies (waitfor_job)
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestDependencyProperties:
    """Property-based tests for job dependencies."""

    @settings(max_examples=20, deadline=15000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        chain_length=st.integers(min_value=2, max_value=10)
    )
    async def test_dependency_chain_execution_order(self, db_pool, job_client, chain_length):
        """Property: Jobs in dependency chain execute in correct order."""
        # PRODUCER: Create dependency chain
        job_ids = []
        prev_job = None

        for i in range(chain_length):
            job_id = await job_client.enqueue(
                "test.Job",
                step=i,
                waitfor_job=prev_job
            )
            job_ids.append(job_id)
            prev_job = job_id

        # CONSUMER: Process chain and verify order
        async with db_pool.acquire() as conn:
            executed_order = []

            for expected_index in range(chain_length):
                # Claim next available job
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345,
                    "test-worker",
                    "default",
                    [],
                    1000
                )

                assert claimed is not None
                executed_order.append(claimed["id"])

                # Finish job to release next in chain
                await conn.execute(STMTS["run"], claimed["id"])
                await conn.execute(
                    STMTS["finished"],
                    claimed["id"],
                    json.dumps({"step": expected_index})
                )

                # Release waiting jobs
                await conn.execute(
                    STMTS["enqueue-next-self-finished"],
                    claimed["id"]
                )

            # Verify jobs executed in dependency order
            assert executed_order == job_ids

            # Cleanup: Delete jobs created in this example
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)


# =============================================================================
# Property-Based Tests: Queue Isolation
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestQueueIsolationProperties:
    """Property-based tests for queue isolation."""

    @settings(max_examples=30, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        queue1_jobs=st.integers(min_value=1, max_value=20),
        queue2_jobs=st.integers(min_value=1, max_value=20)
    )
    async def test_queues_are_isolated(self, db_pool, job_client, queue1_jobs, queue2_jobs):
        """Property: Jobs in different queues don't interfere."""
        # PRODUCER: Enqueue to two different queues
        queue1_ids = []
        for i in range(queue1_jobs):
            job_id = await job_client.enqueue("test.Job", queue="queue1", index=i)
            queue1_ids.append(job_id)

        queue2_ids = []
        for i in range(queue2_jobs):
            job_id = await job_client.enqueue("test.Job", queue="queue2", index=i)
            queue2_ids.append(job_id)

        # CONSUMER: Worker on queue1 only sees queue1 jobs
        async with db_pool.acquire() as conn:
            queue1_claimed = []
            for _ in range(queue1_jobs):
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345,
                    "worker-queue1",
                    "queue1",  # Only claim from queue1
                    [],
                    1000
                )
                if claimed:
                    queue1_claimed.append(claimed["id"])

            # All claimed jobs should be from queue1
            assert len(queue1_claimed) == queue1_jobs
            assert set(queue1_claimed) == set(queue1_ids)

            # Queue2 jobs still available (filter by our specific job IDs)
            queue2_count = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE id = ANY($1::bigint[]) AND state = 'queued'",
                queue2_ids
            )
            assert queue2_count == queue2_jobs

            # Cleanup: Delete jobs created in this example
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", queue1_ids + queue2_ids)


# =============================================================================
# Property-Based Tests: Concurrent Operations
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestConcurrentOperationProperties:
    """Property-based tests for concurrent producer/consumer operations."""

    @settings(max_examples=20, deadline=20000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        producer_count=st.integers(min_value=2, max_value=5),
        jobs_per_producer=st.integers(min_value=1, max_value=10)
    )
    async def test_concurrent_producers_no_job_loss(self, db_pool, job_client, producer_count, jobs_per_producer):
        """Property: Concurrent producers don't lose jobs."""
        total_jobs = producer_count * jobs_per_producer

        # PRODUCERS: Multiple concurrent producers
        async def producer(producer_id):
            job_ids = []
            for i in range(jobs_per_producer):
                job_id = await job_client.enqueue(
                    "test.Job",
                    producer_id=producer_id,
                    job_index=i
                )
                job_ids.append(job_id)
            return job_ids

        # Run producers concurrently
        all_job_ids = await asyncio.gather(*[producer(i) for i in range(producer_count)])
        flat_job_ids = [job_id for producer_jobs in all_job_ids for job_id in producer_jobs]

        # Verify all jobs created
        assert len(flat_job_ids) == total_jobs
        assert len(set(flat_job_ids)) == total_jobs  # All unique

        # CONSUMERS: All jobs are claimable
        async with db_pool.acquire() as conn:
            claimed_count = 0
            for _ in range(total_jobs):
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    12345,
                    "test-worker",
                    "default",
                    [],
                    1000
                )
                if claimed:
                    claimed_count += 1

            assert claimed_count == total_jobs

            # Cleanup: Delete jobs created in this example
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", flat_job_ids)

    @settings(max_examples=15, deadline=20000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        worker_count=st.integers(min_value=2, max_value=5),
        total_jobs=st.integers(min_value=5, max_value=20)
    )
    async def test_concurrent_workers_no_duplicate_claims(self, db_pool, job_client, worker_count, total_jobs):
        """Property: Concurrent workers don't claim the same job."""
        # Ensure we have more jobs than workers
        assume(total_jobs >= worker_count)

        # PRODUCER: Enqueue jobs
        job_ids = []
        for i in range(total_jobs):
            job_id = await job_client.enqueue("test.Job", index=i)
            job_ids.append(job_id)

        # CONSUMERS: Multiple workers claim concurrently
        claimed_jobs = []
        claim_lock = asyncio.Lock()

        async def worker(worker_id):
            async with db_pool.acquire() as conn:
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    worker_id,
                    f"worker-{worker_id}",
                    "default",
                    [],
                    1000
                )
                if claimed:
                    async with claim_lock:
                        claimed_jobs.append(claimed["id"])

        # Run workers concurrently
        await asyncio.gather(*[worker(i) for i in range(worker_count)])

        # Verify no duplicates
        assert len(claimed_jobs) == len(set(claimed_jobs))
        assert len(claimed_jobs) <= min(worker_count, total_jobs)

        # Cleanup: Delete jobs created in this example
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)


# =============================================================================
# Property-Based Tests: Job State Transitions
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestStateTransitionProperties:
    """Property-based tests for job state transitions."""

    @settings(max_examples=30, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        kwargs=job_kwargs_strategy()
    )
    async def test_job_lifecycle_state_invariants(self, db_pool, job_client, kwargs):
        """Property: Job follows valid state transitions (queued -> claimed -> running -> finished)."""
        # PRODUCER: Enqueue job
        job_id = await job_client.enqueue("test.Job", **kwargs)

        async with db_pool.acquire() as conn:
            # Initial state: queued
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "queued"
            assert job["run_count"] == 0

            # Claim job: queued -> claimed
            await conn.execute(
                STMTS["claim"],
                12345,
                "test-worker",
                "default",
                [],
                1000
            )
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "claimed"
            assert job["run_count"] == 1

            # Mark running: claimed -> running
            await conn.execute(STMTS["run"], job_id)
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "running"
            assert job["started"] is not None

            # Mark finished: running -> finished
            await conn.execute(
                STMTS["finished"],
                job_id,
                json.dumps({"result": "success"})
            )
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "finished"
            assert job["finished"] is not None
            assert job["result"] is not None

            # Cleanup: Delete job created in this example
            await conn.execute("DELETE FROM jorb WHERE id = $1", job_id)


# =============================================================================
# Property-Based Tests: Result Storage
# =============================================================================

@pytest.mark.hypothesis
@pytest.mark.asyncio
class TestResultStorageProperties:
    """Property-based tests for result storage."""

    @settings(max_examples=30, deadline=10000, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        result_data=st.dictionaries(
            keys=st.text(min_size=1, max_size=20),
            values=st.one_of(
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=100),
                st.booleans()
            ),
            min_size=1,
            max_size=10
        )
    )
    async def test_result_data_roundtrip(self, db_pool, job_client, result_data):
        """Property: Result data survives roundtrip through database."""
        # PRODUCER: Enqueue job
        job_id = await job_client.enqueue("test.Job", test="result_storage")

        # CONSUMER: Process and store result
        async with db_pool.acquire() as conn:
            claimed = await claim_and_finish_job(conn, "default")
            assert claimed is not None

            # Store custom result
            await conn.execute(
                "UPDATE jorb SET result = $2::json WHERE id = $1",
                claimed["id"],
                json.dumps(result_data)
            )

            # Retrieve and verify
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", claimed["id"])
            stored_result = json.loads(job["result"]) if isinstance(job["result"], str) else job["result"]

            assert stored_result == result_data

            # Cleanup: Delete job created in this example
            await conn.execute("DELETE FROM jorb WHERE id = $1", claimed["id"])
