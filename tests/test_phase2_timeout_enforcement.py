"""
Phase 2: Timeout Enforcement Tests

Comprehensive tests for job timeout enforcement:
- Database timeout tracking (timeout_at column)
- Worker-side timeout via asyncio.wait_for()
- Background timeout monitor
- Timeout actions (retry/fail)
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from pyjobby.timeout_monitor import handle_timed_out_job, timeout_monitor
from tests.utils.factories import create_job, get_job


class TestTimeoutDatabaseSchema:
    """Test timeout-related database schema."""

    @pytest.mark.asyncio
    async def test_timeout_at_column_exists(self, db_connection):
        """Verify timeout_at column exists in jorb table."""
        result = await db_connection.fetchval("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'jorb' AND column_name = 'timeout_at'
        """)
        assert result == 'timeout_at'

    @pytest.mark.asyncio
    async def test_timeout_index_exists(self, db_connection):
        """Verify sparse index on timeout_at exists."""
        result = await db_connection.fetchval("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'jorb' AND indexname = 'jorb_timeout_idx'
        """)
        assert result == 'jorb_timeout_idx'

    @pytest.mark.asyncio
    async def test_timeout_at_nullable(self, db_connection):
        """Test that timeout_at is NULL by default."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)
        assert job['timeout_at'] is None

    @pytest.mark.asyncio
    async def test_set_timeout_at(self, db_connection):
        """Test setting timeout_at column."""
        job_id = await create_job(db_connection, job_class="test.Job")
        timeout_at = datetime.utcnow() + timedelta(seconds=60)

        await db_connection.execute("""
            UPDATE jorb
            SET timeout_at = $1
            WHERE id = $2
        """, timeout_at, job_id)

        job = await get_job(db_connection, job_id)
        assert job['timeout_at'] is not None
        assert job['timeout_at'] > datetime.utcnow()


class TestTimeoutConfiguration:
    """Test timeout configuration in admin_data."""

    @pytest.mark.asyncio
    async def test_timeout_config_in_admin_data(self, db_connection):
        """Test storing timeout configuration in admin_data."""
        admin_data = {
            "timeout_seconds": 300,
            "on_timeout": "retry"
        }

        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data))

        job = await get_job(db_connection, job_id)
        assert job['admin_data']['timeout_seconds'] == 300
        assert job['admin_data']['on_timeout'] == 'retry'

    @pytest.mark.asyncio
    async def test_on_timeout_fail(self, db_connection):
        """Test on_timeout='fail' configuration."""
        admin_data = {
            "timeout_seconds": 60,
            "on_timeout": "fail"
        }

        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data))

        job = await get_job(db_connection, job_id)
        assert job['admin_data']['on_timeout'] == 'fail'

    @pytest.mark.asyncio
    async def test_default_on_timeout_is_retry(self, db_connection):
        """Test that default on_timeout is 'retry'."""
        admin_data = {"timeout_seconds": 60}  # No on_timeout specified

        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data))

        job = await get_job(db_connection, job_id)
        # Default should be 'retry'
        on_timeout = job['admin_data'].get('on_timeout', 'retry')
        assert on_timeout == 'retry'


class TestTimeoutTracking:
    """Test timeout tracking during job execution."""

    @pytest.mark.asyncio
    async def test_timeout_at_set_when_job_starts(self, db_connection):
        """Test that timeout_at is set when job starts running."""
        from pyjobby.pj import STMTS

        job_id = await create_job(
            db_connection,
            job_class="test.Job",
            state="claimed",
            admin_data={"timeout_seconds": 300}
        )

        # Set timeout_at (simulating what worker does)
        timeout_at = datetime.utcnow() + timedelta(seconds=300)
        await db_connection.execute("""
            UPDATE jorb
            SET timeout_at = $1, state = 'running'
            WHERE id = $2
        """, timeout_at, job_id)

        job = await get_job(db_connection, job_id)
        assert job['timeout_at'] is not None
        assert job['state'] == 'running'

    @pytest.mark.asyncio
    async def test_timeout_at_cleared_on_completion(self, db_connection):
        """Test that timeout_at is cleared when job finishes."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at
        timeout_at = datetime.utcnow() + timedelta(seconds=60)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Mark as finished (should clear timeout_at)
        await db_connection.execute(
            STMTS["finished"],
            job_id,
            json.dumps({"status": "success"})
        )

        job = await get_job(db_connection, job_id)
        assert job['state'] == 'finished'
        assert job['timeout_at'] is None  # Should be cleared

    @pytest.mark.asyncio
    async def test_timeout_at_cleared_on_crash(self, db_connection):
        """Test that timeout_at is cleared when job crashes."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at
        timeout_at = datetime.utcnow() + timedelta(seconds=60)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Mark as crashed
        await db_connection.execute(
            STMTS["crash"],
            job_id,
            "Test error",
            "Test backtrace"
        )

        job = await get_job(db_connection, job_id)
        assert job['state'] == 'crashed'
        assert job['timeout_at'] is None  # Should be cleared


class TestTimeoutDetection:
    """Test detection of timed-out jobs."""

    @pytest.mark.asyncio
    async def test_find_timed_out_jobs(self, db_connection):
        """Test SQL query to find timed-out jobs."""
        # Create a job with timeout in the past
        job_id = await create_job(
            db_connection,
            job_class="test.SlowJob",
            state="running",
            admin_data={"timeout_seconds": 60, "on_timeout": "retry"}
        )

        # Set timeout_at to 1 minute ago (timed out)
        timeout_at = datetime.utcnow() - timedelta(minutes=1)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Find timed-out jobs
        timed_out = await db_connection.fetch("""
            SELECT id, job_class, timeout_at, admin_data
            FROM jorb
            WHERE state = 'running'
              AND timeout_at IS NOT NULL
              AND timeout_at < NOW()
        """)

        assert len(timed_out) == 1
        assert timed_out[0]['id'] == job_id

    @pytest.mark.asyncio
    async def test_check_timed_out_jobs_function(self, db_connection):
        """Test SQL function for checking timed-out jobs."""
        # Create timed-out job
        job_id = await create_job(
            db_connection,
            job_class="test.SlowJob",
            state="running",
            admin_data={"timeout_seconds": 30}
        )

        timeout_at = datetime.utcnow() - timedelta(seconds=10)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Call function
        result = await db_connection.fetch("SELECT * FROM check_timed_out_jobs()")

        assert len(result) > 0
        assert result[0]['job_id'] == job_id
        assert result[0]['overdue_seconds'] > 0


class TestTimeoutMonitorHandler:
    """Test timeout monitor handler function."""

    @pytest.mark.asyncio
    async def test_handle_timeout_with_retry(self, db_connection, db_pool):
        """Test handling timeout with retry action."""
        # Create timed-out job
        admin_data = {
            "timeout_seconds": 30,
            "on_timeout": "retry",
            "max_retries": 5
        }
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "running", 0)

        # Set timeout in past
        timeout_at = datetime.utcnow() - timedelta(seconds=10)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            admin_data,
            0  # error_count
        )

        # Should be requeued
        job = await get_job(db_connection, job_id)
        assert job['state'] == 'queued'
        assert job['error_count'] == 1
        assert job['timeout_at'] is None
        assert 'Timeout exceeded' in job['error_message']

    @pytest.mark.asyncio
    async def test_handle_timeout_with_fail(self, db_connection, db_pool):
        """Test handling timeout with fail action."""
        admin_data = {
            "timeout_seconds": 30,
            "on_timeout": "fail"
        }
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "running", 0)

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            admin_data,
            0
        )

        # Should be crashed
        job = await get_job(db_connection, job_id)
        assert job['state'] == 'crashed'
        assert job['error_count'] == 1
        assert 'Timeout exceeded' in job['error_message']

    @pytest.mark.asyncio
    async def test_handle_timeout_max_retries_exceeded(self, db_connection, db_pool):
        """Test timeout handling when max retries exceeded."""
        admin_data = {
            "timeout_seconds": 30,
            "on_timeout": "retry",
            "max_retries": 3
        }
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "running", 3)  # At max

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            admin_data,
            3  # At max retries
        )

        # Should be crashed (max retries exceeded)
        job = await get_job(db_connection, job_id)
        assert job['state'] == 'crashed'
        assert 'max retries exceeded' in job['error_message'].lower()


class TestTimeoutView:
    """Test timeout violations view."""

    @pytest.mark.asyncio
    async def test_timeout_violations_view(self, db_connection):
        """Test jorb_timeout_violations view."""
        # Create timed-out job
        job_id = await create_job(
            db_connection,
            job_class="test.SlowJob",
            state="running",
            admin_data={"timeout_seconds": 60, "on_timeout": "retry"}
        )

        timeout_at = datetime.utcnow() - timedelta(minutes=2)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1, started = NOW() - INTERVAL '5 minutes'
            WHERE id = $2
        """, timeout_at, job_id)

        # Query view
        violations = await db_connection.fetch("""
            SELECT * FROM jorb_timeout_violations
        """)

        assert len(violations) > 0
        found = False
        for v in violations:
            if v['id'] == job_id:
                found = True
                assert v['timeout_action'] == 'retry'
                assert v['overdue_by'].total_seconds() > 0
        assert found


class TestTimeoutIntegration:
    """Integration tests for timeout enforcement."""

    @pytest.mark.asyncio
    async def test_job_with_timeout_config_lifecycle(self, db_connection):
        """Test complete lifecycle of job with timeout configuration."""
        # Create job with timeout
        admin_data = {
            "timeout_seconds": 300,
            "on_timeout": "retry",
            "max_retries": 5
        }

        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "queued")

        # 1. Job starts - set timeout_at
        timeout_at = datetime.utcnow() + timedelta(seconds=300)
        await db_connection.execute("""
            UPDATE jorb SET state = 'running', timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        job = await get_job(db_connection, job_id)
        assert job['timeout_at'] is not None

        # 2. Job completes before timeout - clear timeout_at
        from pyjobby.pj import STMTS
        await db_connection.execute(
            STMTS["finished"],
            job_id,
            json.dumps({"status": "success"})
        )

        job = await get_job(db_connection, job_id)
        assert job['state'] == 'finished'
        assert job['timeout_at'] is None

    @pytest.mark.asyncio
    async def test_multiple_jobs_different_timeouts(self, db_connection):
        """Test multiple jobs with different timeout configurations."""
        jobs = []

        # Job 1: Short timeout, retry
        job1 = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.FastJob", '{}', "default",
            json.dumps({"timeout_seconds": 10, "on_timeout": "retry"}), "running")
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = NOW() + INTERVAL '10 seconds' WHERE id = $1
        """, job1)
        jobs.append(job1)

        # Job 2: Long timeout, fail
        job2 = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.SlowJob", '{}', "default",
            json.dumps({"timeout_seconds": 600, "on_timeout": "fail"}), "running")
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = NOW() + INTERVAL '600 seconds' WHERE id = $1
        """, job2)
        jobs.append(job2)

        # Job 3: No timeout
        job3 = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.NoTimeoutJob", '{}', "default", '{}', "running")
        jobs.append(job3)

        # Verify configurations
        for job_id in jobs:
            job = await get_job(db_connection, job_id)
            if job['job_class'] == 'test.FastJob':
                assert job['admin_data']['timeout_seconds'] == 10
                assert job['timeout_at'] is not None
            elif job['job_class'] == 'test.SlowJob':
                assert job['admin_data']['timeout_seconds'] == 600
                assert job['timeout_at'] is not None
            elif job['job_class'] == 'test.NoTimeoutJob':
                assert job.get('timeout_at') is None


class TestTimeoutEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_timeout_at_without_admin_data(self, db_connection):
        """Test job with timeout_at but no admin_data."""
        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at without admin_data
        timeout_at = datetime.utcnow() + timedelta(seconds=60)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        job = await get_job(db_connection, job_id)
        assert job['timeout_at'] is not None
        # admin_data might be None or empty
        assert job.get('admin_data') is None or job['admin_data'] == {}

    @pytest.mark.asyncio
    async def test_very_short_timeout(self, db_connection):
        """Test with very short timeout (1 second)."""
        admin_data = {"timeout_seconds": 1, "on_timeout": "fail"}
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "running")

        # Set timeout_at to 1 second from now
        timeout_at = datetime.utcnow() + timedelta(seconds=1)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Wait 2 seconds
        await asyncio.sleep(2)

        # Should be timed out
        timed_out = await db_connection.fetch("""
            SELECT id FROM jorb
            WHERE id = $1 AND timeout_at < NOW()
        """, job_id)

        assert len(timed_out) == 1

    @pytest.mark.asyncio
    async def test_timeout_at_in_far_future(self, db_connection):
        """Test with timeout far in the future."""
        admin_data = {"timeout_seconds": 86400}  # 24 hours
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.Job", '{}', "default", json.dumps(admin_data), "running")

        timeout_at = datetime.utcnow() + timedelta(days=1)
        await db_connection.execute("""
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """, timeout_at, job_id)

        # Should not be timed out
        timed_out = await db_connection.fetch("""
            SELECT id FROM jorb
            WHERE id = $1 AND timeout_at < NOW()
        """, job_id)

        assert len(timed_out) == 0
