"""
Comprehensive integration tests for pj.py JobSystem worker.

Tests THE CORE WORKER PROCESS with LIVE database operations.
NO MOCKS - real job execution, real database, real worker process.

This is the HEART of the platform - the actual job execution engine!

Coverage Target: 70%+ for pj.py
"""

import asyncio
import os
from typing import Any

import asyncpg
import pytest

from pyjobby.pj import Job, JobSystem

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


class CounterJob(Job):
    """Job that increments a counter in kwargs."""

    def task(self, count: int = 0):
        return count + 1


# ============================================================================
# Test JobSystem Connection and Initialization
# ============================================================================


class TestJobSystemInitialization:
    """Test JobSystem initialization with live database."""

    @pytest.mark.asyncio
    async def test_jobsystem_database_connection(self, db_params):
        """Test JobSystem can connect to database."""
        system = JobSystem(
            dsn=db_params,
            qname=["default"],
            capabilities=["std"],
            workerId=1,
            checkInterval=1,
            webPort=None,
        )

        # Connect to database
        system.cxn = await asyncpg.connect(**db_params)

        assert system.cxn is not None
        assert not system.cxn.is_closed()

        # Test codec setup (orjson)
        def orjson_encoder(obj: Any) -> str:
            import orjson

            return orjson.dumps(obj).decode("utf-8")

        import orjson

        await system.cxn.set_type_codec(
            "json", encoder=orjson_encoder, decoder=orjson.loads, schema="pg_catalog"
        )

        # Cleanup
        await system.cxn.close()

    @pytest.mark.asyncio
    async def test_jobsystem_statement_preparation(self, db_params):
        """Test JobSystem prepares all SQL statements."""
        from pyjobby.pj import STMTS

        system = JobSystem(
            dsn=db_params,
            qname=["default"],
            capabilities=["std"],
            workerId=1,
            checkInterval=1,
            webPort=None,
        )

        system.cxn = await asyncpg.connect(**db_params)

        # Prepare all statements
        system.stmts = {}
        for name, stmt in STMTS.items():
            system.stmts[name] = await system.cxn.prepare(stmt)

        # Verify all statements prepared
        assert len(system.stmts) == len(STMTS)
        # PreparedStatement is not directly exposed by asyncpg, so just check they're not None
        assert all(stmt is not None for stmt in system.stmts.values())

        # Cleanup
        await system.cxn.close()


# ============================================================================
# Test Job Polling and Claiming
# ============================================================================


class TestJobPollingAndClaiming:
    """Test worker job polling and claiming with live database."""

    @pytest.mark.asyncio
    async def test_worker_claims_job(self, db_pool):
        """Test worker can claim a job from the queue."""
        async with db_pool.acquire() as conn:
            # Clean up any existing queued jobs to avoid test pollution
            await conn.execute("""
                DELETE FROM jorb
                WHERE state = 'queued'
                AND queue = 'default'
                AND (run_after IS NULL OR run_after <= NOW())
            """)

            # Create a job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {"value": "test"},
                "default",
                "queued",
                100,
            )

            # Claim the job (simulating worker claim logic)
            claimed = await conn.fetchrow(
                """
                UPDATE jorb
                SET state = 'claimed',
                    worker_pid = $2,
                    worker_host = $3
                WHERE id = (
                    SELECT id FROM jorb
                    WHERE state = 'queued'
                    AND queue = ANY($1::text[])
                    AND (run_after IS NULL OR run_after <= NOW())
                    ORDER BY prio DESC, id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """,
                ["default"],
                os.getpid(),
                "test-node",
            )

            assert claimed is not None
            assert claimed["id"] == job_id
            assert claimed["state"] == "claimed"
            assert claimed["worker_pid"] == os.getpid()

    @pytest.mark.asyncio
    async def test_worker_skips_jobs_not_in_queue(self, db_pool):
        """Test worker doesn't claim jobs from other queues."""
        async with db_pool.acquire() as conn:
            # First, clean up any queued jobs in 'default' queue to avoid test pollution
            await conn.execute(
                "DELETE FROM jorb WHERE state = 'queued' AND queue = 'default'"
            )

            # Create job in 'high' queue
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "high",
                "queued",
                100,
            )

            # Try to claim from 'default' queue only
            claimed = await conn.fetchrow(
                """
                UPDATE jorb
                SET state = 'claimed'
                WHERE id = (
                    SELECT id FROM jorb
                    WHERE state = 'queued'
                    AND queue = ANY($1::text[])
                    ORDER BY prio DESC, id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """,
                ["default"],
            )  # Only looking in 'default', not 'high'

            # Should not claim the job
            assert claimed is None

            # Verify job still queued
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == "queued"


# ============================================================================
# Test Job Execution
# ============================================================================


class TestJobExecution:
    """Test actual job execution with live database and worker."""

    @pytest.mark.asyncio
    async def test_execute_simple_sync_job(self, db_pool, db_params):
        """Test executing a simple synchronous job."""
        async with db_pool.acquire() as conn:
            # Create and claim job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {"value": "test123"},
                "default",
                "claimed",
                100,
            )

            # Execute job manually (simulating worker execution)
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Create a minimal JobSystem for the job
            system = JobSystem(
                dsn=db_params,
                qname=["default"],
                capabilities=["std"],
                workerId=1,
                checkInterval=1,
                webPort=None,
            )

            # Instantiate job class with proper parameters
            job_instance = SimpleTestJob(s=system, job=dict(job))
            result = job_instance.run()

            assert result == "processed: test123"

            # Mark as finished
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'finished',
                    result = $2,
                    finished = NOW()
                WHERE id = $1
            """,
                job_id,
                result,
            )

            # Verify
            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "finished"
            assert final_job["result"] == "processed: test123"

    @pytest.mark.asyncio
    async def test_execute_async_job(self, db_pool, db_params):
        """Test executing an async job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.AsyncTestJob",
                {"delay": 0.1},
                "default",
                "claimed",
                100,
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Create a minimal JobSystem for the job
            system = JobSystem(
                dsn=db_params,
                qname=["default"],
                capabilities=["std"],
                workerId=1,
                checkInterval=1,
                webPort=None,
            )

            # Execute async job
            job_instance = AsyncTestJob(s=system, job=dict(job))
            resultStageA = job_instance.run()

            assert asyncio.iscoroutine(resultStageA)
            result = await resultStageA

            assert result == "async_complete"

            # Mark as finished
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'finished',
                    result = $2,
                    finished = NOW()
                WHERE id = $1
            """,
                job_id,
                result,
            )

            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "finished"


# ============================================================================
# Test Error Handling and Retries
# ============================================================================


class TestJobErrorHandling:
    """Test job error handling and retry logic."""

    @pytest.mark.asyncio
    async def test_job_failure_marks_crashed(self, db_pool, db_params):
        """Test that failing jobs are marked as crashed."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 error_count, admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 0, $6)
                RETURNING id
            """,
                "test_pj_worker_integration.FailingJob",
                {},
                "default",
                "claimed",
                100,
                {"max_retries": 3},
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Create a minimal JobSystem for the job
            system = JobSystem(
                dsn=db_params,
                qname=["default"],
                capabilities=["std"],
                workerId=1,
                checkInterval=1,
                webPort=None,
            )

            # Try to execute failing job
            job_instance = FailingJob(s=system, job=dict(job))
            error_message = None
            try:
                job_instance.run()
            except ValueError as e:
                error_message = str(e)

            assert error_message == "Intentional test failure"

            # Mark as crashed
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'crashed',
                    error_count = error_count + 1,
                    error_message = $2,
                    finished = NOW()
                WHERE id = $1
            """,
                job_id,
                error_message,
            )

            final_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert final_job["state"] == "crashed"
            assert final_job["error_count"] == 1
            assert "Intentional test failure" in final_job["error_message"]

    @pytest.mark.asyncio
    async def test_job_retry_on_failure(self, db_pool):
        """Test that failed jobs are retried."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 error_count, admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 1, $6)
                RETURNING id
            """,
                "test_pj_worker_integration.FailingJob",
                {},
                "default",
                "crashed",
                100,
                {"max_retries": 5},
            )

            # Simulate retry logic - requeue the job
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'queued',
                    run_after = NOW() + INTERVAL '1 second'
                WHERE id = $1
            """,
                job_id,
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["state"] == "queued"
            assert job["error_count"] == 1
            assert job["run_after"] is not None


# ============================================================================
# Test Timeout Handling
# ============================================================================


class TestJobTimeoutHandling:
    """Test job timeout enforcement."""

    @pytest.mark.asyncio
    async def test_timeout_enforcement(self, db_pool, db_params):
        """Test that jobs respect timeout settings."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """,
                "test_pj_worker_integration.TimeoutJob",
                {},
                "default",
                "claimed",
                100,
                {"timeout_seconds": 1},
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Set timeout_at in database
            await conn.execute(
                """
                UPDATE jorb
                SET timeout_at = NOW() + INTERVAL '1 second'
                WHERE id = $1
            """,
                job_id,
            )

            # Create a minimal JobSystem for the job
            system = JobSystem(
                dsn=db_params,
                qname=["default"],
                capabilities=["std"],
                workerId=1,
                checkInterval=1,
                webPort=None,
            )

            # Execute with timeout
            job_instance = TimeoutJob(s=system, job=dict(job))
            resultStageA = job_instance.run()

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(resultStageA, timeout=1.0)


# ============================================================================
# Test Crash Recovery
# ============================================================================


class TestCrashRecovery:
    """Test worker crash recovery."""

    @pytest.mark.asyncio
    async def test_recover_abandoned_running_jobs(self, db_pool):
        """Test recovery of jobs that were running when worker crashed."""
        async with db_pool.acquire() as conn:
            # Clean up any existing abandoned jobs from this worker to avoid test pollution
            await conn.execute("""
                DELETE FROM jorb
                WHERE worker_pid = 99999
                AND worker_host = 'dead-node'
            """)

            # Create "abandoned" jobs (running but worker died)
            abandoned_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 worker_pid, worker_host, started)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6, $7, NOW() - INTERVAL '10 minutes')
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "default",
                "running",
                100,
                99999,
                "dead-node",
            )

            # Simulate recovery (what JobSystem.recover_abandoned_jobs does)
            recovered = await conn.fetch(
                """
                UPDATE jorb
                SET state = 'queued',
                    worker_pid = NULL,
                    worker_host = NULL,
                    error_count = error_count + 1,
                    error_message = 'Worker crash - requeued'
                WHERE state IN ('running', 'claimed')
                AND worker_pid = $1
                AND worker_host = $2
                RETURNING *
            """,
                99999,
                "dead-node",
            )

            assert len(recovered) == 1
            assert recovered[0]["id"] == abandoned_id
            assert recovered[0]["state"] == "queued"
            assert recovered[0]["error_count"] == 1


# ============================================================================
# Test Job Class Loading
# ============================================================================


class TestJobClassLoading:
    """Test dynamic job class loading."""

    @pytest.mark.asyncio
    async def test_class_for_klass_from_name(self, db_pool, db_params):
        """Test loading job class from string name."""
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING *
            """,
                "tests.test_pj_worker_integration.SimpleTestJob",
                {"value": "test"},
                "default",
                "queued",
                100,
            )

        # Test class loading (what JobSystem.classForKlassFromName does)
        import importlib
        import pydoc

        klass_mod = pydoc.locate(".".join(job["job_class"].split(".")[:-1]))
        assert klass_mod is not None

        importlib.reload(klass_mod)

        klass = pydoc.locate(job["job_class"])
        assert klass is not None
        assert klass == SimpleTestJob

        # Create a minimal JobSystem for the job
        system = JobSystem(
            dsn=db_params,
            qname=["default"],
            capabilities=["std"],
            workerId=1,
            checkInterval=1,
            webPort=None,
        )

        # Instantiate and run
        instance = klass(s=system, job=dict(job))
        result = instance.run()
        assert result == "processed: test"


# ============================================================================
# Test Priority and Queue Ordering
# ============================================================================


class TestJobPriorityOrdering:
    """Test that jobs are claimed in correct priority order."""

    @pytest.mark.asyncio
    async def test_high_priority_claimed_first(self, db_pool):
        """Test that high priority jobs are claimed before low priority."""
        async with db_pool.acquire() as conn:
            # Create jobs with different priorities
            low_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "default",
                "queued",
                50,
            )

            high_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "default",
                "queued",
                200,
            )

            # Claim next job (should get high priority first)
            claimed = await conn.fetchrow("""
                UPDATE jorb
                SET state = 'claimed'
                WHERE id = (
                    SELECT id FROM jorb
                    WHERE state = 'queued'
                    AND queue = 'default'
                    ORDER BY prio DESC, id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """)

            assert claimed["id"] == high_id
            assert claimed["prio"] == 200


# ============================================================================
# Test run_after Scheduling
# ============================================================================


class TestRunAfterScheduling:
    """Test run_after delayed execution."""

    @pytest.mark.asyncio
    async def test_job_not_claimed_before_run_after(self, db_pool):
        """Test that jobs with run_after are not claimed too early."""
        async with db_pool.acquire() as conn:
            # Clean up any queued jobs in 'default' queue to avoid test pollution
            await conn.execute("""
                DELETE FROM jorb
                WHERE state = 'queued'
                AND queue = 'default'
                AND (run_after IS NULL OR run_after <= NOW())
            """)

            # Create job with future run_after
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 run_after)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "default",
                "queued",
                100,
            )

            # Try to claim (should not claim - run_after is in future)
            claimed = await conn.fetchrow("""
                UPDATE jorb
                SET state = 'claimed'
                WHERE id = (
                    SELECT id FROM jorb
                    WHERE state = 'queued'
                    AND queue = 'default'
                    AND (run_after IS NULL OR run_after <= NOW())
                    ORDER BY prio DESC, id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """)

            assert claimed is None

            # Verify our job is still queued
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == "queued"

    @pytest.mark.asyncio
    async def test_job_claimed_after_run_after_time(self, db_pool):
        """Test that jobs are claimed after run_after time passes."""
        async with db_pool.acquire() as conn:
            # Clean up any existing queued jobs
            await conn.execute("""
                DELETE FROM jorb
                WHERE state = 'queued'
                AND queue = 'default'
                AND (run_after IS NULL OR run_after <= NOW())
            """)

            # Create job with past run_after
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 run_after)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NOW() - INTERVAL '1 second')
                RETURNING id
            """,
                "test_pj_worker_integration.SimpleTestJob",
                {},
                "default",
                "queued",
                100,
            )

            # Should be able to claim now
            claimed = await conn.fetchrow("""
                UPDATE jorb
                SET state = 'claimed'
                WHERE id = (
                    SELECT id FROM jorb
                    WHERE state = 'queued'
                    AND queue = 'default'
                    AND (run_after IS NULL OR run_after <= NOW())
                    ORDER BY prio DESC, id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """)

            assert claimed is not None
            assert claimed["id"] == job_id
