"""
Comprehensive tests for pj.py JobSystem run() loop - THE CORE WORKER!

Tests the ACTUAL worker execution loop with LIVE database operations.
NO MOCKS - real job processing, real database, real worker loop.

This tests the HEART of the platform - lines 403-699 in pj.py!

Coverage Target: Drive pj.py from 44% to 70%+
"""

import pytest
import asyncio
import asyncpg
from datetime import datetime, timedelta
import time
import os
import signal
from typing import Any

from pyjobby.pj import JobSystem, Job, STMTS


# ============================================================================
# Test Job Classes
# ============================================================================

class QuickJob(Job):
    """Job that completes quickly."""
    def task(self, value: str = "default"):
        return f"quick: {value}"


class AsyncQuickJob(Job):
    """Async job that completes quickly."""
    async def task(self, value: str = "async"):
        await asyncio.sleep(0.01)
        return f"async: {value}"


class TimeoutTestJob(Job):
    """Job that will timeout."""
    timeout = 2

    async def task(self):
        await asyncio.sleep(10)  # Will timeout
        return "should not reach"


class FailingTestJob(Job):
    """Job that always fails."""
    def task(self):
        raise ValueError("Test error for retry")


class CounterJob(Job):
    """Job that returns the attempt counter."""
    def task(self, attempt: int = 1):
        return f"attempt_{attempt}"


class AsyncGenJob(Job):
    """Job that returns async generator."""
    async def task(self):
        async def gen():
            for i in range(3):
                yield i
        return gen()


# ============================================================================
# Test Main run() Loop
# ============================================================================

class TestWorkerRunLoop:
    """Test the actual worker run() loop execution."""

    @pytest.mark.asyncio
    async def test_worker_processes_single_job(self, db_pool, db_params):
        """Test worker run loop processes a job and stops."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create a job - let asyncpg handle JSON encoding
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.QuickJob',
                {'value': 'test1'},
                'default', 'queued', 100)

        # Create worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=1,
            checkInterval=0.1,
            webPort=None
        )

        # Start worker in background with timeout
        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=2.0)

        worker_task = asyncio.create_task(run_worker())

        # Wait a bit for processing
        await asyncio.sleep(0.5)

        # Stop the worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass  # Expected when worker stops

        # Verify job was processed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job['state'] == 'finished'
            assert job['result'] == 'quick: test1'

    @pytest.mark.asyncio
    async def test_worker_processes_multiple_jobs(self, db_pool, db_params):
        """Test worker processes multiple jobs in sequence."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create multiple jobs
            job_ids = []
            for i in range(3):
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                    RETURNING id
                """, 'tests.test_pj_worker_run_loop.QuickJob',
                    {'value': f'job{i}'},
                    'default', 'queued', 100)
                job_ids.append(job_id)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=2,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())

        # Wait for all jobs to process
        await asyncio.sleep(1.0)

        # Stop worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify all jobs processed
        async with db_pool.acquire() as conn:
            for i, job_id in enumerate(job_ids):
                job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
                assert job['state'] == 'finished'
                assert job['result'] == f'quick: job{i}'

    @pytest.mark.asyncio
    async def test_worker_processes_async_jobs(self, db_pool, db_params):
        """Test worker can process async jobs."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create async job
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.AsyncQuickJob',
                {'value': 'async_test'},
                'default', 'queued', 100)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=3,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=2.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.5)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify async job processed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job['state'] == 'finished'
            assert job['result'] == 'async: async_test'


# ============================================================================
# Test Timeout Handling in run() Loop
# ============================================================================

class TestWorkerTimeoutHandling:
    """Test timeout handling within the run() loop."""

    @pytest.mark.asyncio
    async def test_worker_handles_job_timeout_with_retry(self, db_pool, db_params):
        """Test worker handles timeout and creates retry job."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create job that will timeout with retry enabled
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.TimeoutTestJob',
                {},
                'default', 'queued', 100,
                {'timeout_seconds': 1, 'on_timeout': 'retry', 'max_retries': 3})

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=4,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=5.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(2.5)  # Wait for timeout to occur
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify original job crashed and retry was created
        async with db_pool.acquire() as conn:
            original_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert original_job['state'] == 'crashed'
            assert 'timed out' in original_job['error_message'].lower()

            # Check retry job was created
            retry_jobs = await conn.fetch("""
                SELECT * FROM jorb
                WHERE job_class = 'tests.test_pj_worker_run_loop.TimeoutTestJob'
                AND state = 'queued'
                AND error_count = 1
            """)
            assert len(retry_jobs) >= 1

    @pytest.mark.asyncio
    async def test_worker_handles_timeout_with_fail(self, db_pool, db_params):
        """Test worker handles timeout when on_timeout=fail."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb WHERE job_class LIKE 'tests.test_pj_worker_run_loop.%'")

            # Create job that will timeout with fail mode
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.TimeoutTestJob',
                {},
                'default', 'queued', 100,
                {'timeout_seconds': 1, 'on_timeout': 'fail', 'max_retries': 3})

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=5,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=5.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(2.5)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify job crashed and NO retry was created
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job['state'] == 'crashed'

            # Should be no retry jobs created
            retry_count = await conn.fetchval("""
                SELECT COUNT(*) FROM jorb
                WHERE job_class = 'tests.test_pj_worker_run_loop.TimeoutTestJob'
                AND id != $1
                AND error_count > 0
            """, job_id)
            assert retry_count == 0


# ============================================================================
# Test Exception Handling in run() Loop
# ============================================================================

class TestWorkerExceptionHandling:
    """Test exception handling and retry logic in run() loop."""

    @pytest.mark.asyncio
    async def test_worker_handles_exception_with_retry(self, db_pool, db_params):
        """Test worker handles exception and creates retry job."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb WHERE job_class LIKE 'tests.test_pj_worker_run_loop.%'")

            # Create failing job with retry
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.FailingTestJob',
                {},
                'default', 'queued', 100,
                {'max_retries': 3, 'retry_strategy': 'exponential'})

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=6,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.8)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify job crashed and retry created
        async with db_pool.acquire() as conn:
            original_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert original_job['state'] == 'crashed'
            assert 'Test error for retry' in original_job['error_message']
            assert 'Traceback' in (original_job['error_backtrace'] or '')

            # Check retry job exists
            retry_jobs = await conn.fetch("""
                SELECT * FROM jorb
                WHERE job_class = 'tests.test_pj_worker_run_loop.FailingTestJob'
                AND state = 'queued'
                AND error_count = 1
            """)
            assert len(retry_jobs) >= 1

    @pytest.mark.asyncio
    async def test_worker_stops_retry_after_max_attempts(self, db_pool, db_params):
        """Test worker stops retrying after max_retries exceeded."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb WHERE job_class LIKE 'tests.test_pj_worker_run_loop.%'")

            # Create failing job with error_count near max
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 error_count, admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6, $7)
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.FailingTestJob',
                {},
                'default', 'queued', 100, 2,  # Already failed twice
                {'max_retries': 3})

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=7,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.8)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify job crashed and NO retry created (max exceeded)
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job['state'] == 'crashed'

            # Should be no new retry jobs (error_count would be 3)
            retry_count = await conn.fetchval("""
                SELECT COUNT(*) FROM jorb
                WHERE job_class = 'tests.test_pj_worker_run_loop.FailingTestJob'
                AND error_count >= 3
                AND state = 'queued'
            """)
            assert retry_count == 0


# ============================================================================
# Test run() Loop Edge Cases
# ============================================================================

class TestWorkerRunLoopEdgeCases:
    """Test edge cases in the run() loop."""

    @pytest.mark.asyncio
    async def test_worker_handles_empty_queue(self, db_pool, db_params):
        """Test worker correctly sleeps when queue is empty."""
        async with db_pool.acquire() as conn:
            # Clean database - ensure no jobs
            await conn.execute("DELETE FROM jorb WHERE queue = 'empty_test'")

        # Create worker
        system = JobSystem(
            dsn=db_params,
            qname='empty_test',
            capabilities=('std',),
            workerId=8,
            checkInterval=0.2,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=1.0)

        worker_task = asyncio.create_task(run_worker())

        # Let worker run with empty queue
        await asyncio.sleep(0.6)

        # Stop worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Worker should have handled empty queue gracefully
        # (No assertion needed - just verify it doesn't crash)

    @pytest.mark.asyncio
    async def test_worker_respects_queue_filter(self, db_pool, db_params):
        """Test worker only processes jobs from specified queues."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create job in 'default' queue
            job_id_default = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.QuickJob',
                {'value': 'default'},
                'default', 'queued', 100)

            # Create job in 'high' queue
            job_id_high = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.QuickJob',
                {'value': 'high'},
                'high', 'queued', 100)

        # Create worker that only processes 'default' queue
        system = JobSystem(
            dsn=db_params,
            qname='default',  # Only 'default', not 'high'
            capabilities=('std',),
            workerId=9,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=2.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.8)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify only default queue job was processed
        async with db_pool.acquire() as conn:
            default_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id_default)
            high_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id_high)

            assert default_job['state'] == 'finished'
            assert high_job['state'] == 'queued'  # Not processed!

    @pytest.mark.asyncio
    async def test_worker_processes_async_generator_job(self, db_pool, db_params):
        """Test worker can handle async generator jobs."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb WHERE job_class LIKE 'tests.test_pj_worker_run_loop.%'")

            # Create async generator job
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_worker_run_loop.AsyncGenJob',
                {},
                'default', 'queued', 100)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=10,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=2.0)

        worker_task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.8)
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify async generator job was processed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job['state'] == 'finished'
            # Result should be list from async generator
            assert job['result'] == [0, 1, 2]
