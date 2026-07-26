"""
E2E Producer/Consumer Tests

Complete workflows with LIVE workers processing jobs from the database:
the producer side uses the real JobClient, the consumer side is the real
JobSystem worker loop (started via the conftest `live_worker` fixture)
executing the shared job classes in tests/dxe_jobs.py.

Scenarios tested:
1. Basic producer/consumer workflow (single worker, batches)
2. Job state transitions (queued -> claimed -> running -> finished),
   verified via the trigger-recorded jorb_history trail
3. Error handling: same-row retry with backoff, then terminal 'crashed'
   (the DLQ) once retries are exhausted
4. DAG structures with dependencies (jorb_dependencies / waitfor_job)
5. Result passing between pipeline stages
"""

import asyncio
import time
from typing import Any

import pytest

from pyjobby.client import JobClient
from pyjobby.pj import Job
from tests.conftest import wait_for_job_state
from tests.utils.factories import get_job


class EchoKwargsJob(Job):
    """Returns every kwarg it ran with (verifies run-time result passing)."""

    async def task(self, **kwargs: Any) -> dict[str, Any]:
        return {"kwargs": kwargs}


@pytest.mark.slow
@pytest.mark.e2e
class TestBasicProducerConsumer:
    """Basic producer/consumer workflow with a live worker."""

    @pytest.mark.asyncio
    async def test_single_job_execution(self, db_pool, live_worker, unique_queue):
        """Producer enqueues a job and the live worker executes it."""
        await live_worker()
        client = JobClient(pool=db_pool)

        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=21)
        assert job_id is not None

        row = await wait_for_job_state(db_pool, job_id, ("finished",))

        assert row["state"] == "finished"
        assert row["result"] == {"doubled": 42}
        assert row["run_count"] == 1
        assert row["worker_host"] is not None
        assert row["claimed_by"] is not None  # jorb_worker registry id
        assert row["started"] is not None
        assert row["finished"] is not None

    @pytest.mark.asyncio
    async def test_batch_job_execution(self, db_pool, live_worker, unique_queue):
        """Producer enqueues a batch; the worker drains all of it."""
        await live_worker()
        client = JobClient(pool=db_pool)

        job_ids = []
        for i in range(10):
            job_id = await client.enqueue(
                "tests.dxe_jobs.OkJob", queue=unique_queue, x=i
            )
            job_ids.append(job_id)

        for i, job_id in enumerate(job_ids):
            row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
            assert row["result"] == {"doubled": i * 2}

        # Nothing left claimable in this queue
        remaining = await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE queue = $1 AND state != 'finished'",
            unique_queue,
        )
        assert remaining == 0


@pytest.mark.slow
@pytest.mark.e2e
class TestJobStateTransitions:
    """Job state transitions during real execution."""

    @pytest.mark.asyncio
    async def test_job_lifecycle_states(self, db_pool, live_worker, unique_queue):
        """queued -> claimed -> running -> finished, with history recorded."""
        await live_worker()
        client = JobClient(pool=db_pool)

        job_id = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=1.5
        )

        # Initial state (may already be claimed by the time we look, but
        # never terminal yet)
        job = await get_job(db_pool, job_id)
        assert job["state"] in ("queued", "claimed", "running")

        # Observe it actually running
        row = await wait_for_job_state(db_pool, job_id, ("running",))
        assert row["started"] is not None
        assert row["run_count"] == 1
        assert row["run_epoch"] == 1  # bumped once, at claim time
        assert row["worker_pid"] is not None

        # And finishing
        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["result"] == "done"
        assert row["finished"] is not None

        # The trigger-recorded history holds the full per-attempt trail
        events = [
            r["event"]
            for r in await db_pool.fetch(
                "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id",
                job_id,
            )
        ]
        assert events == ["enqueued", "claimed", "running", "finished"]


@pytest.mark.slow
@pytest.mark.e2e
class TestErrorHandlingAndRetry:
    """Error handling and retry behavior with a live worker."""

    @pytest.mark.asyncio
    async def test_job_retry_on_failure(self, db_pool, live_worker, unique_queue):
        """A failing job is requeued (SAME row) with backoff after failure."""
        await live_worker()
        client = JobClient(pool=db_pool)

        # Long retry delay: after the first failure the job sits 'queued'
        # with run_after in the future, where we can observe it.
        job_id = await client.enqueue(
            "tests.dxe_jobs.FailJob",
            queue=unique_queue,
            max_retries=5,
            retry_strategy="fixed",
            initial_retry_delay=60,
        )

        deadline = time.monotonic() + 15
        job = None
        while time.monotonic() < deadline:
            job = await get_job(db_pool, job_id)
            if job["error_count"] >= 1 and job["state"] == "queued":
                break
            await asyncio.sleep(0.1)

        assert job is not None
        assert job["state"] == "queued"  # retrying: same row, same id
        assert job["error_count"] == 1
        assert "intentional failure" in job["error_message"]
        # Backoff: scheduled in the future relative to its update time
        assert job["run_after"] > job["updated"]

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, db_pool, live_worker, unique_queue):
        """Retries exhausted -> terminal 'crashed' (the DLQ)."""
        await live_worker()
        client = JobClient(pool=db_pool)

        job_id = await client.enqueue(
            "tests.dxe_jobs.FailJob",
            queue=unique_queue,
            max_retries=1,  # first failure is terminal
        )

        row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=15)

        assert row["state"] == "crashed"
        assert row["error_count"] == 1
        assert "intentional failure" in row["error_message"]

        # 'crashed' IS the dead letter queue
        dlq_ids = [
            r["id"]
            for r in await db_pool.fetch("SELECT id FROM jorb WHERE state = 'crashed'")
        ]
        assert job_id in dlq_ids


@pytest.mark.slow
@pytest.mark.e2e
class TestDAGExecution:
    """DAG execution with dependencies driven by a live worker."""

    @pytest.mark.asyncio
    async def test_linear_dag_execution(self, db_pool, live_worker, unique_queue):
        """Linear DAG Job1 -> Job2 -> Job3 runs to completion in order."""
        await live_worker()

        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id",
            "E2E Linear Pipeline",
        )

        # Job1 (no dependencies)
        job1_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id)
            VALUES ($1, $2, $3, 'queued', $4)
            RETURNING id
            """,
            "tests.dxe_jobs.OkJob",
            {"x": 1},
            unique_queue,
            dag_id,
        )

        # Job2 waits on Job1; Job3 waits on Job2
        job2_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id, waitfor_job)
            VALUES ($1, $2, $3, 'waiting', $4, $5)
            RETURNING id
            """,
            "tests.dxe_jobs.OkJob",
            {"x": 2},
            unique_queue,
            dag_id,
            job1_id,
        )
        job3_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id, waitfor_job)
            VALUES ($1, $2, $3, 'waiting', $4, $5)
            RETURNING id
            """,
            "tests.dxe_jobs.OkJob",
            {"x": 3},
            unique_queue,
            dag_id,
            job2_id,
        )

        # The worker finishes Job1, which wakes Job2, then Job3
        row1 = await wait_for_job_state(db_pool, job1_id, ("finished",), timeout=20)
        row2 = await wait_for_job_state(db_pool, job2_id, ("finished",), timeout=20)
        row3 = await wait_for_job_state(db_pool, job3_id, ("finished",), timeout=20)

        assert row1["result"] == {"doubled": 2}
        assert row2["result"] == {"doubled": 4}
        assert row3["result"] == {"doubled": 6}
        # Dependency order was respected
        assert row1["finished"] <= row2["started"]
        assert row2["finished"] <= row3["started"]

        # DAG status view reflects completion
        status = await db_pool.fetchrow(
            "SELECT * FROM jorb_dag_status WHERE dag_id = $1", dag_id
        )
        assert status["total_jobs"] == 3
        assert status["finished_jobs"] == 3
        assert status["pending_jobs"] == 0

    @pytest.mark.asyncio
    async def test_parallel_dag_execution(self, db_pool, live_worker, unique_queue):
        """Parallel branches run; the merge job records its dependencies."""
        await live_worker()

        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id",
            "E2E Parallel Pipeline",
        )

        job1_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id)
            VALUES ('tests.dxe_jobs.OkJob', $1, $2, 'queued', $3)
            RETURNING id
            """,
            {"x": 10},
            unique_queue,
            dag_id,
        )
        job2_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id)
            VALUES ('tests.dxe_jobs.OkJob', $1, $2, 'queued', $3)
            RETURNING id
            """,
            {"x": 20},
            unique_queue,
            dag_id,
        )

        # Merge job waits (its release is DAG-orchestrator work; here we
        # verify the dependency edges are stored correctly)
        job3_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, dag_id)
            VALUES ('tests.dxe_jobs.OkJob', $1, $2, 'waiting', $3)
            RETURNING id
            """,
            {"x": 30},
            unique_queue,
            dag_id,
        )
        await db_pool.execute(
            """
            INSERT INTO jorb_dependencies (job_id, depends_on)
            VALUES ($1, $2), ($1, $3)
            """,
            job3_id,
            job1_id,
            job2_id,
        )

        # Both parallel branches complete
        await wait_for_job_state(db_pool, job1_id, ("finished",), timeout=20)
        await wait_for_job_state(db_pool, job2_id, ("finished",), timeout=20)

        # Merge job is still waiting on its recorded dependencies
        job3 = await get_job(db_pool, job3_id)
        assert job3["state"] == "waiting"

        deps = await db_pool.fetch(
            """
            SELECT depends_on FROM jorb_dependencies
            WHERE job_id = $1
            ORDER BY depends_on
            """,
            job3_id,
        )
        assert {d["depends_on"] for d in deps} == {job1_id, job2_id}


@pytest.mark.slow
@pytest.mark.e2e
class TestResultPassing:
    """Result passing through a job pipeline (use_result_from)."""

    @pytest.mark.asyncio
    async def test_result_passed_to_downstream_job(
        self, db_pool, live_worker, unique_queue
    ):
        """The worker injects the upstream result as 'upstream_result'."""
        await live_worker()
        client = JobClient(pool=db_pool)

        job1_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=5)
        # Job2 waits for Job1 and receives its stored result at run time
        # (the worker injects it into kwargs as 'upstream_result').
        job2_id = await client.enqueue(
            "tests.test_e2e_producer_consumer.EchoKwargsJob",
            queue=unique_queue,
            waitfor_job=job1_id,
            use_result_from=job1_id,
            tag="downstream",
        )

        row1 = await wait_for_job_state(db_pool, job1_id, ("finished",), timeout=20)
        assert row1["result"] == {"doubled": 10}

        row2 = await wait_for_job_state(db_pool, job2_id, ("finished",), timeout=20)
        echoed = row2["result"]["kwargs"]
        assert echoed["tag"] == "downstream"
        assert echoed["upstream_result"] == {"doubled": 10}
