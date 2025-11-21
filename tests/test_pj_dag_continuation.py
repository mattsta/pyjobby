"""
Comprehensive tests for pj.py DAG continuation logic.

Tests job dependencies and group coordination in the worker run() loop.
Covers lines 591-592 (waitfor_job) and 598-603 (waitfor_group).

NO MOCKS - real job processing, real database, real worker loop.
"""

import pytest
import asyncio
import asyncpg
from typing import Any

from pyjobby.pj import JobSystem, Job


# ============================================================================
# Test Job Classes
# ============================================================================

class QuickJob(Job):
    """Job that completes quickly."""
    def task(self, value: str = "quick"):
        return f"result: {value}"


class AsyncQuickJob(Job):
    """Async job that completes quickly."""
    async def task(self, value: str = "async"):
        await asyncio.sleep(0.01)
        return f"async_result: {value}"


# ============================================================================
# Test DAG Continuation - waitfor_job
# ============================================================================

class TestDAGWaitForJob:
    """Test DAG continuation with waitfor_job dependencies."""

    @pytest.mark.asyncio
    async def test_waitfor_job_triggers_dependent_jobs(self, db_pool, db_params):
        """Test that completing a job triggers jobs waiting for it (waitfor_job)."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create parent job (will run first)
            parent_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'parent'},
                'default', 'queued', 100)

            # Create child job that waits for parent (starts in 'waiting' state)
            child_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, waitfor_job)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'child'},
                'default', 'waiting', 100, parent_id)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=100,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())

        # Let worker process both jobs
        await asyncio.sleep(0.8)

        # Stop worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify both jobs completed
        async with db_pool.acquire() as conn:
            parent_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", parent_id)
            child_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", child_id)

            # Parent should be finished
            assert parent_job['state'] == 'finished', f"Parent state: {parent_job['state']}"
            assert parent_job['result'] == 'result: parent'

            # Child should also be finished (triggered after parent completed)
            assert child_job['state'] == 'finished', f"Child state: {child_job['state']}"
            assert child_job['result'] == 'result: child'

    @pytest.mark.asyncio
    async def test_waitfor_job_with_multiple_dependent_jobs(self, db_pool, db_params):
        """Test that one job can trigger multiple waiting jobs."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create parent job
            parent_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'parent'},
                'default', 'queued', 100)

            # Create multiple child jobs waiting for parent
            child_ids = []
            for i in range(3):
                child_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, waitfor_job)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                    RETURNING id
                """, 'tests.test_pj_dag_continuation.QuickJob',
                    {'value': f'child_{i}'},
                    'default', 'waiting', 100, parent_id)
                child_ids.append(child_id)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=101,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())

        # Let worker process all jobs
        await asyncio.sleep(1.0)

        # Stop worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify all jobs completed
        async with db_pool.acquire() as conn:
            parent_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", parent_id)
            assert parent_job['state'] == 'finished'

            # All 3 children should be finished
            for i, child_id in enumerate(child_ids):
                child_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", child_id)
                assert child_job['state'] == 'finished', f"Child {i} state: {child_job['state']}"
                assert child_job['result'] == f'result: child_{i}'


# ============================================================================
# Test DAG Continuation - waitfor_group (run_group)
# ============================================================================

class TestDAGWaitForGroup:
    """Test DAG continuation with run_group/waitfor_group dependencies."""

    @pytest.mark.asyncio
    async def test_waitfor_group_triggers_after_all_group_jobs_finish(self, db_pool, db_params):
        """Test that jobs waiting for a group are triggered when ALL group jobs finish."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            # Create a run_group ID (just use a timestamp-based ID)
            group_id = 12345

            # Create 3 jobs in the same run_group
            group_job_ids = []
            for i in range(3):
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, run_group)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                    RETURNING id
                """, 'tests.test_pj_dag_continuation.QuickJob',
                    {'value': f'group_{i}'},
                    'default', 'queued', 100, group_id)
                group_job_ids.append(job_id)

            # Create job that waits for the entire group to finish
            waiting_job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, waitfor_group)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'waiting_for_group'},
                'default', 'waiting', 100, group_id)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=102,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=3.0)

        worker_task = asyncio.create_task(run_worker())

        # Let worker process all jobs
        await asyncio.sleep(1.0)

        # Stop worker
        system.stop = True

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify all jobs completed
        async with db_pool.acquire() as conn:
            # All 3 group jobs should be finished
            for i, job_id in enumerate(group_job_ids):
                job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
                assert job['state'] == 'finished', f"Group job {i} state: {job['state']}"

            # Waiting job should be triggered and finished
            waiting_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", waiting_job_id)
            assert waiting_job['state'] == 'finished', f"Waiting job state: {waiting_job['state']}"
            assert waiting_job['result'] == 'result: waiting_for_group'

    @pytest.mark.asyncio
    async def test_waitfor_group_not_triggered_until_all_finish(self, db_pool, db_params):
        """Test that waiting job is NOT triggered until ALL group jobs finish."""
        async with db_pool.acquire() as conn:
            # Clean database
            await conn.execute("DELETE FROM jorb")

            group_id = 54321

            # Create first job that will complete quickly
            job1_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, run_group)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'group_1'},
                'default', 'queued', 100, group_id)

            # Create second job that we'll manually set to crashed (so it doesn't finish)
            # This simulates one job in group not being complete
            job2_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, run_group)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'group_2'},
                'default', 'crashed', 100, group_id)  # Start as crashed

            # Create waiting job
            waiting_job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, waitfor_group)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'tests.test_pj_dag_continuation.QuickJob',
                {'value': 'should_stay_waiting'},
                'default', 'waiting', 100, group_id)

        # Create and run worker
        system = JobSystem(
            dsn=db_params,
            qname='default',
            capabilities=('std',),
            workerId=103,
            checkInterval=0.1,
            webPort=None
        )

        async def run_worker():
            await asyncio.wait_for(system.run(), timeout=0.5)

        worker_task = asyncio.create_task(run_worker())

        try:
            await worker_task
        except asyncio.TimeoutError:
            pass

        # Verify: first job finished, second is crashed, waiting job still waiting
        async with db_pool.acquire() as conn:
            job1 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job1_id)
            job2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2_id)
            waiting_job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", waiting_job_id)

            assert job1['state'] == 'finished', f"Job1 should finish, got: {job1['state']}"
            assert job2['state'] == 'crashed', f"Job2 should be crashed, got: {job2['state']}"

            # Waiting job should STILL be waiting (not triggered because job2 didn't finish)
            assert waiting_job['state'] == 'waiting', f"Waiting job should stay waiting, got: {waiting_job['state']}"
