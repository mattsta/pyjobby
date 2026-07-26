#!/usr/bin/env python3
"""
Comprehensive Client + Worker Integration Tests.

Tests complete producer/consumer workflows using the actual JobClient API
and worker claim/run/finish cycle.

These tests demonstrate:
- Full end-to-end job lifecycle using real client code
- Producer: JobClient.enqueue() and batch operations
- Consumer: Worker claim -> run -> finish cycle using STMTS
- Real job class instantiation and execution
- Result passing through pipelines
- DAG execution with actual job completion
- Error handling and retry logic with real client code
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.client import JobClient
from pyjobby.pj import STMTS

# =============================================================================
# Test Job Classes
# =============================================================================


class SimpleTestJob:
    """Simple job that returns success."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def run(self) -> dict:
        await asyncio.sleep(0.01)  # Simulate work
        return {"status": "success", "input": self.kwargs}


class DataProcessingJob:
    """Job that processes data from upstream result."""

    def __init__(
        self, upstream_result: dict | None = None, operation: str = "identity", **kwargs
    ):
        self.upstream_result = upstream_result
        self.operation = operation
        self.kwargs = kwargs

    async def run(self) -> dict:
        if self.upstream_result and "data" in self.upstream_result:
            data = self.upstream_result["data"]

            if self.operation == "double":
                result = [x * 2 for x in data]
            elif self.operation == "sum":
                result = sum(data)
            elif self.operation == "filter_positive":
                result = [x for x in data if x > 0]
            else:
                result = data

            return {"data": result, "operation": self.operation}
        else:
            # Initial data generation
            return {"data": [1, 2, 3, 4, 5], "operation": "init"}


class FailingJob:
    """Job that fails with an error."""

    def __init__(self, error_message: str = "Test error", **kwargs):
        self.error_message = error_message

    async def run(self) -> dict:
        await asyncio.sleep(0.01)
        raise ValueError(self.error_message)


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


async def claim_job(conn, queue="default", capabilities=None, max_priority=1000):
    """Claim a job using the actual worker STMTS."""
    if capabilities is None:
        capabilities = []

    claimed = await conn.fetchrow(
        STMTS["claim"],
        12345,  # worker_pid
        "test-worker",  # worker_host
        queue,
        capabilities,
        max_priority,
    )
    return claimed


async def mark_running(conn, job_id, timeout_seconds=None):
    """Mark job as running using actual worker STMTS."""
    await conn.execute(STMTS["run"], job_id)

    # Set timeout if specified
    if timeout_seconds:
        await conn.execute(STMTS["set-timeout"], job_id, f"{timeout_seconds} seconds")


async def mark_finished(conn, job_id, result: dict):
    """Mark job as finished using actual worker STMTS."""
    await conn.execute(STMTS["finished"], job_id, json.dumps(result))


async def mark_error(conn, job_id, error_message: str, error_backtrace: str = ""):
    """Mark job as crashed using actual worker STMTS."""
    await conn.execute(STMTS["crash"], job_id, error_message, error_backtrace)


async def execute_job(conn, job_row):
    """Execute a job by instantiating its class and running it."""
    job_class_path = job_row["job_class"]
    kwargs = job_row["kwargs"]

    # Parse kwargs if it's a string (JSON from database)
    if isinstance(kwargs, str):
        kwargs = json.loads(kwargs)

    # Ensure kwargs is a dict
    if not isinstance(kwargs, dict):
        raise TypeError(f"kwargs must be a dict, got {type(kwargs)}: {kwargs}")

    # Dynamically import and instantiate job class
    module_path, class_name = job_class_path.rsplit(".", 1)

    # For test jobs, we can directly instantiate from globals
    if "SimpleTestJob" in job_class_path:
        job_instance = SimpleTestJob(**kwargs)
    elif "DataProcessingJob" in job_class_path:
        job_instance = DataProcessingJob(**kwargs)
    elif "FailingJob" in job_class_path:
        job_instance = FailingJob(**kwargs)
    else:
        raise ValueError(f"Unknown job class: {job_class_path}")

    # Execute the job
    result = await job_instance.run()
    return result


# =============================================================================
# Integration Tests: Producer + Consumer Workflows
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestProducerConsumerIntegration:
    """Test complete producer/consumer workflows using actual client code."""

    async def test_simple_job_full_lifecycle(self, db_pool, job_client):
        """Test: Producer enqueues -> Consumer claims/runs/finishes."""
        # PRODUCER: Enqueue using JobClient
        job_id = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            message="Hello from client!",
        )

        assert job_id is not None

        # CONSUMER: Claim, run, and finish using worker STMTS
        async with db_pool.acquire() as conn:
            # Claim job
            claimed = await claim_job(conn, queue="default")
            assert claimed is not None
            assert claimed["id"] == job_id
            assert claimed["state"] == "claimed"

            # Mark as running
            await mark_running(conn, job_id)

            # Execute the actual job
            job_row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            result = await execute_job(conn, job_row)

            # Mark as finished with result
            await mark_finished(conn, job_id, result)

            # Verify final state
            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "finished"

            # Parse result from JSON
            result_data = (
                json.loads(final_job["result"])
                if isinstance(final_job["result"], str)
                else final_job["result"]
            )
            assert result_data["status"] == "success"
            assert result_data["input"]["message"] == "Hello from client!"

    async def test_batch_enqueue_and_process(self, db_pool, job_client):
        """Test: Producer batch enqueues -> Consumer processes all."""
        # PRODUCER: Batch enqueue using JobClient
        job_ids = await job_client.enqueue_batch(
            [
                ("tests.test_client_worker_integration.SimpleTestJob", {"task": 1}),
                ("tests.test_client_worker_integration.SimpleTestJob", {"task": 2}),
                ("tests.test_client_worker_integration.SimpleTestJob", {"task": 3}),
            ]
        )

        assert len(job_ids) == 3

        # CONSUMER: Process all jobs
        async with db_pool.acquire() as conn:
            processed_count = 0

            while True:
                # Claim next job
                claimed = await claim_job(conn, queue="default")
                if not claimed:
                    break

                # Mark as running
                await mark_running(conn, claimed["id"])

                # Execute job
                result = await execute_job(conn, claimed)

                # Mark as finished
                await mark_finished(conn, claimed["id"], result)

                processed_count += 1

            assert processed_count == 3

            # Verify all finished
            finished_count = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE state = 'finished'"
            )
            assert finished_count == 3

    async def test_priority_based_consumption(self, db_pool, job_client):
        """Test: Consumer claims jobs by priority."""
        # PRODUCER: Enqueue jobs with different priorities
        low_priority = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            priority=1000,
            task="low",
        )
        high_priority = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            priority=10,
            task="high",
        )
        medium_priority = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            priority=100,
            task="medium",
        )

        # CONSUMER: Claim jobs - should get high priority first
        async with db_pool.acquire() as conn:
            # First claim should be high priority
            claimed1 = await claim_job(conn, queue="default")
            assert claimed1["id"] == high_priority
            await mark_running(conn, claimed1["id"])
            result1 = await execute_job(conn, claimed1)
            await mark_finished(conn, claimed1["id"], result1)

            # Second claim should be medium priority
            claimed2 = await claim_job(conn, queue="default")
            assert claimed2["id"] == medium_priority
            await mark_running(conn, claimed2["id"])
            result2 = await execute_job(conn, claimed2)
            await mark_finished(conn, claimed2["id"], result2)

            # Third claim should be low priority
            claimed3 = await claim_job(conn, queue="default")
            assert claimed3["id"] == low_priority
            await mark_running(conn, claimed3["id"])
            result3 = await execute_job(conn, claimed3)
            await mark_finished(conn, claimed3["id"], result3)

    async def test_pipeline_with_result_passing(self, db_pool, job_client):
        """Test: Pipeline jobs pass results through dependency chain."""
        # PRODUCER: Create pipeline (Job1 -> Job2 -> Job3)
        job1_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob", operation="init"
        )

        job2_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob",
            operation="double",
            waitfor_job=job1_id,
        )

        job3_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob",
            operation="sum",
            waitfor_job=job2_id,
        )

        # CONSUMER: Process pipeline
        async with db_pool.acquire() as conn:
            # Process Job1
            claimed1 = await claim_job(conn, queue="default")
            assert claimed1["id"] == job1_id
            await mark_running(conn, claimed1["id"])
            result1 = await execute_job(conn, claimed1)
            await mark_finished(conn, claimed1["id"], result1)

            # Job2 should now be available (waitfor_job satisfied)
            await conn.execute(
                "UPDATE jorb SET state = 'queued' WHERE id = $1 AND state = 'waiting'",
                job2_id,
            )

            # Process Job2 with Job1's result
            claimed2 = await claim_job(conn, queue="default")
            assert claimed2["id"] == job2_id

            # Inject upstream result (simulating dependency resolution)
            job2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2_id)
            job2_kwargs = (
                json.loads(job2["kwargs"])
                if isinstance(job2["kwargs"], str)
                else job2["kwargs"]
            )
            job2_kwargs["upstream_result"] = result1
            await conn.execute(
                "UPDATE jorb SET kwargs = $2::json WHERE id = $1",
                job2_id,
                json.dumps(job2_kwargs),
            )

            claimed2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2_id)
            await mark_running(conn, claimed2["id"])
            result2 = await execute_job(conn, claimed2)
            await mark_finished(conn, claimed2["id"], result2)

            # Verify Job2 doubled the data
            assert result2["data"] == [2, 4, 6, 8, 10]

            # Job3 should now be available
            await conn.execute(
                "UPDATE jorb SET state = 'queued' WHERE id = $1 AND state = 'waiting'",
                job3_id,
            )

            # Process Job3 with Job2's result
            job3 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job3_id)
            job3_kwargs = (
                json.loads(job3["kwargs"])
                if isinstance(job3["kwargs"], str)
                else job3["kwargs"]
            )
            job3_kwargs["upstream_result"] = result2
            await conn.execute(
                "UPDATE jorb SET kwargs = $2::json WHERE id = $1",
                job3_id,
                json.dumps(job3_kwargs),
            )

            claimed3 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job3_id)
            await mark_running(conn, claimed3["id"])
            result3 = await execute_job(conn, claimed3)
            await mark_finished(conn, claimed3["id"], result3)

            # Verify Job3 summed the data
            assert result3["data"] == 30  # sum([2, 4, 6, 8, 10])

    async def test_error_handling_and_retry(self, db_pool, job_client):
        """Test: Job fails -> Consumer marks as crashed."""
        # PRODUCER: Enqueue failing job
        job_id = await job_client.enqueue(
            "tests.test_client_worker_integration.FailingJob",
            error_message="Intentional test failure",
        )

        # CONSUMER: First attempt fails
        async with db_pool.acquire() as conn:
            # Claim and attempt to run
            claimed = await claim_job(conn, queue="default")
            assert claimed["id"] == job_id
            await mark_running(conn, claimed["id"])

            # Execute job (will fail)
            try:
                result = await execute_job(conn, claimed)
                assert False, "Job should have failed"
            except ValueError as e:
                # Mark as crashed (real worker behavior)
                await mark_error(conn, claimed["id"], str(e))

            # Verify job is marked as crashed
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "crashed"
            assert job["error_count"] == 1
            assert job["run_count"] == 1
            assert "Intentional test failure" in job["error_message"]

    async def test_concurrent_workers_no_duplicate_claims(self, db_pool, job_client):
        """Test: Multiple workers don't claim the same job."""
        # PRODUCER: Enqueue several jobs
        job_ids = await job_client.enqueue_batch(
            [
                (
                    "tests.test_client_worker_integration.SimpleTestJob",
                    {"worker_test": i},
                )
                for i in range(10)
            ]
        )

        # CONSUMER: Simulate 5 concurrent workers
        claimed_jobs = []

        async def worker(worker_id):
            async with db_pool.acquire() as conn:
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    worker_id,  # Each worker has unique PID
                    f"worker-{worker_id}",
                    "default",
                    [],
                    1000,
                )
                if claimed:
                    claimed_jobs.append(claimed["id"])

        # Run workers concurrently
        await asyncio.gather(*[worker(i) for i in range(5)])

        # Verify no duplicates
        assert len(claimed_jobs) == len(set(claimed_jobs))
        assert len(claimed_jobs) == 5  # 5 workers claimed 5 jobs


# =============================================================================
# Integration Tests: DAG Execution
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestDAGIntegration:
    """Test DAG execution using actual client code."""

    async def test_dag_linear_execution(self, db_pool, job_client):
        """Test: Linear DAG execution using client API."""
        # PRODUCER: Create DAG
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name, created) VALUES ($1, $2) RETURNING id",
            "Client Test Linear DAG",
            datetime.now(UTC),
        )

        # Create linear chain using client
        job1_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob", operation="init"
        )

        job2_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob",
            operation="double",
            waitfor_job=job1_id,
        )

        job3_id = await job_client.enqueue(
            "tests.test_client_worker_integration.DataProcessingJob",
            operation="sum",
            waitfor_job=job2_id,
        )

        # Link jobs to DAG (manual since client doesn't support dag_id parameter)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jorb SET dag_id = $1 WHERE id = ANY($2::bigint[])",
                dag_id,
                [job1_id, job2_id, job3_id],
            )

        # CONSUMER: Execute DAG sequentially
        async with db_pool.acquire() as conn:
            # Execute Job1
            claimed1 = await claim_job(conn, queue="default")
            assert claimed1["id"] == job1_id
            await mark_running(conn, claimed1["id"])
            result1 = await execute_job(conn, claimed1)
            await mark_finished(conn, claimed1["id"], result1)

            # Release Job2
            await conn.execute(
                "UPDATE jorb SET state = 'queued' WHERE id = $1 AND state = 'waiting'",
                job2_id,
            )

            # Inject Job1 result into Job2
            job2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2_id)
            job2_kwargs = (
                json.loads(job2["kwargs"])
                if isinstance(job2["kwargs"], str)
                else job2["kwargs"]
            )
            job2_kwargs["upstream_result"] = result1
            await conn.execute(
                "UPDATE jorb SET kwargs = $2::json WHERE id = $1",
                job2_id,
                json.dumps(job2_kwargs),
            )

            # Execute Job2
            claimed2 = await claim_job(conn, queue="default")
            assert claimed2["id"] == job2_id
            claimed2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2_id)
            await mark_running(conn, claimed2["id"])
            result2 = await execute_job(conn, claimed2)
            await mark_finished(conn, claimed2["id"], result2)

            # Release Job3
            await conn.execute(
                "UPDATE jorb SET state = 'queued' WHERE id = $1 AND state = 'waiting'",
                job3_id,
            )

            # Inject Job2 result into Job3
            job3 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job3_id)
            job3_kwargs = (
                json.loads(job3["kwargs"])
                if isinstance(job3["kwargs"], str)
                else job3["kwargs"]
            )
            job3_kwargs["upstream_result"] = result2
            await conn.execute(
                "UPDATE jorb SET kwargs = $2::json WHERE id = $1",
                job3_id,
                json.dumps(job3_kwargs),
            )

            # Execute Job3
            claimed3 = await claim_job(conn, queue="default")
            assert claimed3["id"] == job3_id
            claimed3 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job3_id)
            await mark_running(conn, claimed3["id"])
            result3 = await execute_job(conn, claimed3)
            await mark_finished(conn, claimed3["id"], result3)

            # Verify DAG completion
            dag_status = await conn.fetchrow(
                "SELECT * FROM jorb_dag_status WHERE dag_id = $1", dag_id
            )
            assert dag_status["finished_jobs"] == 3
            assert dag_status["dag_state"] == "complete"

    async def test_dag_parallel_branches(self, db_pool, job_client):
        """Test: Parallel DAG branches can be processed concurrently."""
        # PRODUCER: Create parallel DAG (Job1, Job2 can run in parallel)
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name, created) VALUES ($1, $2) RETURNING id",
            "Client Test Parallel DAG",
            datetime.now(UTC),
        )

        job1_id = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob", branch="A"
        )

        job2_id = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob", branch="B"
        )

        # Link jobs to DAG (manual since client doesn't support dag_id parameter)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jorb SET dag_id = $1 WHERE id = ANY($2::bigint[])",
                dag_id,
                [job1_id, job2_id],
            )

        # CONSUMER: Process both in parallel
        async with db_pool.acquire() as conn1, db_pool.acquire() as conn2:
            # Both jobs should be claimable simultaneously
            claimed1 = await claim_job(conn1, queue="default")
            claimed2 = await claim_job(conn2, queue="default")

            assert claimed1 is not None
            assert claimed2 is not None
            assert claimed1["id"] != claimed2["id"]
            assert {claimed1["id"], claimed2["id"]} == {job1_id, job2_id}

            # Execute both concurrently
            async def execute_and_finish(conn, claimed):
                await mark_running(conn, claimed["id"])
                result = await execute_job(conn, claimed)
                await mark_finished(conn, claimed["id"], result)

            await asyncio.gather(
                execute_and_finish(conn1, claimed1), execute_and_finish(conn2, claimed2)
            )

            # Verify both finished
            finished_jobs = await conn1.fetch(
                "SELECT * FROM jorb WHERE dag_id = $1 AND state = 'finished'", dag_id
            )
            assert len(finished_jobs) == 2


# =============================================================================
# Integration Tests: Advanced Features
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestAdvancedClientFeatures:
    """Test advanced client features with worker integration."""

    async def test_scheduled_job_execution(self, db_pool, job_client):
        """Test: Scheduled job runs at specified time."""
        # PRODUCER: Schedule job for future
        future_time = datetime.now(UTC).replace(tzinfo=None) + timedelta(
            seconds=2
        )
        job_id = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            run_after=future_time,
            scheduled=True,
        )

        # CONSUMER: Cannot claim immediately
        async with db_pool.acquire() as conn:
            claimed_early = await claim_job(conn, queue="default")
            assert claimed_early is None  # Too early

            # Wait until scheduled time
            await asyncio.sleep(2.1)

            # Now can claim
            claimed = await claim_job(conn, queue="default")
            assert claimed is not None
            assert claimed["id"] == job_id

    async def test_capability_matching(self, db_pool, job_client):
        """Test: Worker with matching capability claims job."""
        # PRODUCER: Enqueue job requiring GPU capability
        job_id = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            capability="gpu",
            gpu_task=True,
        )

        # CONSUMER: Worker without GPU cannot claim
        async with db_pool.acquire() as conn:
            claimed_no_gpu = await conn.fetchrow(
                STMTS["claim"],
                12345,
                "cpu-worker",
                "default",
                ["cpu"],  # Only has CPU capability
                1000,
            )
            assert claimed_no_gpu is None

            # Worker with GPU can claim
            claimed_with_gpu = await conn.fetchrow(
                STMTS["claim"],
                12346,
                "gpu-worker",
                "default",
                ["cpu", "gpu"],  # Has GPU capability
                1000,
            )
            assert claimed_with_gpu is not None
            assert claimed_with_gpu["id"] == job_id

    async def test_queue_isolation(self, db_pool, job_client):
        """Test: Workers only claim jobs from their assigned queue."""
        # PRODUCER: Enqueue jobs to different queues
        critical_job = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            queue="critical",
            priority=10,
        )

        normal_job = await job_client.enqueue(
            "tests.test_client_worker_integration.SimpleTestJob",
            queue="normal",
            priority=100,
        )

        # CONSUMER: Normal queue worker doesn't get critical jobs
        async with db_pool.acquire() as conn:
            claimed_normal = await claim_job(conn, queue="normal")
            assert claimed_normal is not None
            assert claimed_normal["id"] == normal_job

            # Critical queue worker gets critical jobs
            claimed_critical = await claim_job(conn, queue="critical")
            assert claimed_critical is not None
            assert claimed_critical["id"] == critical_job
