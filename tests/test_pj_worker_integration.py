"""
Comprehensive integration tests for pj.py JobSystem worker (schema v1).

Tests the core worker machinery with LIVE database operations.
NO MOCKS - real job execution, real database, the real claim/finish/crash
statements from pyjobby.pj.STMTS.
"""

import asyncio

import pytest

from pyjobby import db
from pyjobby.pj import STMTS, Job, JobSystem

pytestmark = pytest.mark.asyncio


# ============================================================================
# Test Job Classes for Worker Integration Testing
# ============================================================================


class SimpleTestJob(Job):
    """Simple synchronous test job."""

    def task(self, value: str):
        """Simple task that returns the value."""
        return f"processed: {value}"


class AsyncTestJob(Job):
    """Async test job."""

    async def task(self, delay: float = 0.1):
        """Async task with optional delay."""
        await asyncio.sleep(delay)
        return "async_complete"


class FailingJob(Job):
    """Job that always fails."""

    def task(self):
        raise ValueError("Intentional test failure")


class TimeoutJob(Job):
    """Job that times out."""

    timeout = 1  # 1 second timeout

    async def task(self):
        await asyncio.sleep(10)  # Sleep longer than timeout
        return "should_not_reach"


async def claim(
    conn,
    queue,
    *,
    pid=12345,
    host="test-node",
    caps=(),
    prio=1000,
    app_version: str | None = None,
):
    """Claim the next job on `queue` via the REAL claim statement."""
    return await conn.fetchrow(
        STMTS["claim"], pid, host, queue, list(caps), prio, None, app_version
    )


# ============================================================================
# Test JobSystem Connection and Initialization
# ============================================================================


class TestJobSystemInitialization:
    """Test JobSystem initialization with live database."""

    async def test_jobsystem_database_connection(self, db_params, worker_params):
        """Test JobSystem can connect to database (with pyjobby codecs)."""
        system = JobSystem(dsn=db_params, **worker_params)

        system.cxn = await db.connect(**db_params)
        try:
            assert system.cxn is not None
            assert not system.cxn.is_closed()

            # codecs registered: jsonb round-trips as dict
            row = await system.cxn.fetchval("SELECT '{\"a\": 1}'::jsonb")
            assert row == {"a": 1}
        finally:
            await system.cxn.close()

    async def test_jobsystem_statement_preparation(self, db_params, worker_params):
        """Test JobSystem prepares all SQL statements."""
        system = JobSystem(dsn=db_params, **worker_params)

        system.cxn = await db.connect(**db_params)
        try:
            system.stmts = {
                name: await system.cxn.prepare(stmt) for name, stmt in STMTS.items()
            }

            assert len(system.stmts) == len(STMTS)
            assert all(stmt is not None for stmt in system.stmts.values())
        finally:
            await system.cxn.close()


# ============================================================================
# Test Job Polling and Claiming
# ============================================================================


class TestJobPollingAndClaiming:
    """Test worker job claiming with the real claim statement."""

    async def test_worker_claims_job(self, db_pool, unique_queue):
        """Test worker can claim a job from the queue."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {"value": "test"},
                unique_queue,
            )

            claimed = await claim(conn, unique_queue, pid=4242)

            assert claimed is not None
            assert claimed["id"] == job_id
            assert claimed["state"] == "claimed"
            assert claimed["worker_pid"] == 4242
            assert claimed["worker_host"] == "test-node"
            assert claimed["run_epoch"] == 1  # bumped on claim

    async def test_worker_skips_jobs_not_in_queue(self, db_pool, unique_queue):
        """Test worker doesn't claim jobs from other queues."""
        other_queue = f"{unique_queue}_high"
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                other_queue,
            )

            # Claim from unique_queue only — must not see the other queue
            claimed = await claim(conn, unique_queue)
            assert claimed is None

            # Verify job still queued
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == "queued"


# ============================================================================
# Test Job Execution
# ============================================================================


class TestJobExecution:
    """Test actual job execution with live database and worker."""

    async def test_execute_simple_sync_job(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test executing a simple synchronous job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {"value": "test123"},
                unique_queue,
            )
            claimed = await claim(conn, unique_queue)
            assert claimed["id"] == job_id
            epoch = claimed["run_epoch"]

            system = JobSystem(dsn=db_params, **worker_params)

            # Instantiate job class with the claimed row
            job_instance = SimpleTestJob(s=system, job=dict(claimed))
            result = job_instance.run()

            assert result == "processed: test123"

            # Mark as finished via the real (epoch-fenced) statement
            await conn.execute(STMTS["finished"], job_id, result, epoch)

            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "finished"
            assert final_job["result"] == "processed: test123"

    async def test_execute_async_job(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test executing an async job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.test_pj_worker_integration.AsyncTestJob",
                {"delay": 0.1},
                unique_queue,
            )
            claimed = await claim(conn, unique_queue)
            epoch = claimed["run_epoch"]

            system = JobSystem(dsn=db_params, **worker_params)

            # Execute async job
            job_instance = AsyncTestJob(s=system, job=dict(claimed))
            resultStageA = job_instance.run()

            assert asyncio.iscoroutine(resultStageA)
            result = await resultStageA

            assert result == "async_complete"

            await conn.execute(STMTS["finished"], job_id, result, epoch)

            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "finished"


# ============================================================================
# Test Error Handling and Retries
# ============================================================================


class TestJobErrorHandling:
    """Test job error handling and retry logic."""

    async def test_job_failure_marks_crashed(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test that failing jobs can be dead-lettered (state 'crashed')."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                "tests.test_pj_worker_integration.FailingJob",
                {},
                unique_queue,
                {"max_retries": 3},
            )
            claimed = await claim(conn, unique_queue)
            epoch = claimed["run_epoch"]

            system = JobSystem(dsn=db_params, **worker_params)

            # Try to execute failing job
            job_instance = FailingJob(s=system, job=dict(claimed))
            error_message = None
            try:
                job_instance.run()
            except ValueError as e:
                error_message = str(e)

            assert error_message == "Intentional test failure"

            # Dead-letter via the real statement (increments error_count)
            await conn.execute(
                STMTS["crashed"], job_id, error_message, "Traceback...", epoch
            )

            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "crashed"
            assert final_job["error_count"] == 1
            assert "Intentional test failure" in final_job["error_message"]

    async def test_job_retry_on_failure(self, db_pool, unique_queue):
        """A failed attempt requeues the SAME row with backoff."""
        from datetime import timedelta

        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                "tests.test_pj_worker_integration.FailingJob",
                {},
                unique_queue,
                {"max_retries": 5},
            )
            claimed = await claim(conn, unique_queue)

            # Same-row retry with a 1-second backoff
            await conn.execute(
                STMTS["retry"],
                job_id,
                timedelta(seconds=1),
                "Intentional test failure",
                "Traceback...",
                claimed["run_epoch"],
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "queued"
            assert job["error_count"] == 1
            assert job["run_after"] is not None

            # one row only — retries never create copies
            total = await conn.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            assert total == 1


# ============================================================================
# Test Timeout Handling
# ============================================================================


class TestJobTimeoutHandling:
    """Test job timeout enforcement."""

    async def test_timeout_enforcement(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test that jobs respect timeout settings."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                "tests.test_pj_worker_integration.TimeoutJob",
                {},
                unique_queue,
                {"timeout_seconds": 1},
            )
            claimed = await claim(conn, unique_queue)

            # Arm the (epoch-fenced) timeout deadline like the worker does:
            # it rides in the claimed -> running write itself
            from datetime import timedelta

            await conn.execute(
                STMTS["run"],
                job_id,
                claimed["run_epoch"],
                timedelta(seconds=1),
            )
            timeout_at = await conn.fetchval(
                "SELECT timeout_at FROM jorb WHERE id = $1", job_id
            )
            assert timeout_at is not None

            system = JobSystem(dsn=db_params, **worker_params)

            # Execute with timeout
            job_instance = TimeoutJob(s=system, job=dict(claimed))
            resultStageA = job_instance.run()

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(resultStageA, timeout=1.0)


# ============================================================================
# Test Crash Recovery (monitor-style requeue of a dead worker's job)
# ============================================================================


class TestCrashRecovery:
    """Test requeueing in-flight jobs owned by a dead worker."""

    async def test_requeue_abandoned_running_job(self, db_pool, unique_queue):
        """The monitor's requeue primitive puts an abandoned job back in
        the queue: SAME row, epoch trail preserved in history."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
            )
            claimed = await claim(conn, unique_queue, pid=99999, host="dead-node")
            await conn.execute(STMTS["run"], job_id, claimed["run_epoch"], None)

            # dead-worker recovery is the shared requeue primitive in
            # pyjobby.db (driven by pyjobby.monitor via the worker registry)
            requeued = await db.requeue_job(
                conn,
                job_id,
                allowed_states=("claimed", "running"),
                reset_errors=False,
            )
            assert requeued == job_id

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "queued"

            # a fresh claim gets a HIGHER epoch, fencing out the dead attempt
            reclaimed = await claim(conn, unique_queue, pid=11111, host="live-node")
            assert reclaimed["id"] == job_id
            # the requeue fenced the abandoned attempt, then the claim
            # advanced the token again -- only the ordering is guaranteed
            assert reclaimed["run_epoch"] > claimed["run_epoch"]

            stale = await conn.fetch(
                STMTS["finished"], job_id, {"stale": True}, claimed["run_epoch"]
            )
            assert stale == []


# ============================================================================
# Test Job Class Loading
# ============================================================================


class TestJobClassLoading:
    """Test dynamic job class loading."""

    async def test_class_for_klass_from_name(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test loading job class from string name."""
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING *""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {"value": "test"},
                unique_queue,
            )

        system = JobSystem(dsn=db_params, **worker_params)

        # the real loader: resolves the dotted path, reloads the module,
        # and instantiates with (s=..., job=...)
        instance = system.classForKlassFromName(job["job_class"], job=dict(job))
        assert type(instance).__name__ == "SimpleTestJob"

        result = instance.run()
        assert result == "processed: test"


# ============================================================================
# Test Priority and Queue Ordering
# ============================================================================


class TestJobPriorityOrdering:
    """Test that jobs are claimed in correct priority order."""

    async def test_most_urgent_priority_claimed_first(self, db_pool, unique_queue):
        """Lower prio number = more urgent: claimed first."""
        async with db_pool.acquire() as conn:
            urgent_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, prio)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
                50,
            )
            await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, prio)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
                200,
            )

            claimed = await claim(conn, unique_queue)
            assert claimed["id"] == urgent_id
            assert claimed["prio"] == 50

    async def test_priority_ceiling_excludes_urgent_enough_jobs(
        self, db_pool, unique_queue
    ):
        """Jobs above the worker's prio ceiling are not claimed."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO jorb (job_class, kwargs, queue, prio)
                   VALUES ($1, $2, $3, $4)""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
                500,
            )

            assert await claim(conn, unique_queue, prio=100) is None
            assert await claim(conn, unique_queue, prio=1000) is not None


# ============================================================================
# Test run_after Scheduling
# ============================================================================


class TestRunAfterScheduling:
    """Test run_after delayed execution."""

    async def test_job_not_claimed_before_run_after(self, db_pool, unique_queue):
        """Test that jobs with run_after are not claimed too early."""
        async with db_pool.acquire() as conn:
            # Create job with future run_after (plain now() arithmetic —
            # every timestamp column is timestamptz)
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, run_after)
                   VALUES ($1, $2, $3, now() + INTERVAL '1 hour')
                   RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
            )

            claimed = await claim(conn, unique_queue)
            assert claimed is None

            # Verify our job is still queued
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == "queued"

    async def test_job_claimed_after_run_after_time(self, db_pool, unique_queue):
        """Test that jobs are claimed after run_after time passes."""
        async with db_pool.acquire() as conn:
            # Create job with past run_after
            job_id = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, run_after)
                   VALUES ($1, $2, $3, now() - INTERVAL '1 second')
                   RETURNING id""",
                "tests.test_pj_worker_integration.SimpleTestJob",
                {},
                unique_queue,
            )

            claimed = await claim(conn, unique_queue)
            assert claimed is not None
            assert claimed["id"] == job_id
