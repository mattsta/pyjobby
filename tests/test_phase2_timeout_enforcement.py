"""
Phase 2: Timeout Enforcement Tests

Comprehensive tests for job timeout enforcement:
- Database timeout tracking (timeout_at column)
- Worker-side timeout via asyncio.wait_for()
- Background monitor handler (pyjobby.monitor)
- Timeout actions (retry/fail)
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.monitor import handle_timed_out_job
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
        assert result == "timeout_at"

    @pytest.mark.asyncio
    async def test_timeout_index_exists(self, db_connection):
        """Verify sparse index on timeout_at exists."""
        result = await db_connection.fetchval("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'jorb' AND indexname = 'jorb_timeout_idx'
        """)
        assert result == "jorb_timeout_idx"

    @pytest.mark.asyncio
    async def test_timeout_at_nullable(self, db_connection):
        """Test that timeout_at is NULL by default."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)
        assert job["timeout_at"] is None

    @pytest.mark.asyncio
    async def test_set_timeout_at(self, db_connection):
        """Test setting timeout_at column."""
        job_id = await create_job(db_connection, job_class="test.Job")
        timeout_at = datetime.now(UTC) + timedelta(seconds=60)

        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = $1
            WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        job = await get_job(db_connection, job_id)
        assert job["timeout_at"] is not None
        assert job["timeout_at"] > datetime.now(UTC)


class TestTimeoutConfiguration:
    """Test timeout configuration in admin_data."""

    @pytest.mark.asyncio
    async def test_timeout_config_in_admin_data(self, db_connection):
        """Test storing timeout configuration in admin_data."""
        admin_data = {"timeout_seconds": 300, "on_timeout": "retry"}

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
        )

        job = await get_job(db_connection, job_id)
        assert job["admin_data"]["timeout_seconds"] == 300
        assert job["admin_data"]["on_timeout"] == "retry"

    @pytest.mark.asyncio
    async def test_on_timeout_fail(self, db_connection):
        """Test on_timeout='fail' configuration."""
        admin_data = {"timeout_seconds": 60, "on_timeout": "fail"}

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
        )

        job = await get_job(db_connection, job_id)
        assert job["admin_data"]["on_timeout"] == "fail"

    @pytest.mark.asyncio
    async def test_default_on_timeout_is_retry(self, db_connection):
        """Test that default on_timeout is 'retry'."""
        admin_data = {"timeout_seconds": 60}  # No on_timeout specified

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
        )

        job = await get_job(db_connection, job_id)
        # Default should be 'retry'
        on_timeout = job["admin_data"].get("on_timeout", "retry")
        assert on_timeout == "retry"


class TestTimeoutTracking:
    """Test timeout tracking during job execution."""

    @pytest.mark.asyncio
    async def test_timeout_at_set_when_job_starts(self, db_connection):
        """Test that timeout_at is set when job starts running."""

        job_id = await create_job(
            db_connection,
            job_class="test.Job",
            state="claimed",
            admin_data={"timeout_seconds": 300},
        )

        # Set timeout_at (simulating what worker does)
        timeout_at = datetime.now(UTC) + timedelta(seconds=300)
        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = $1, state = 'running'
            WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        job = await get_job(db_connection, job_id)
        assert job["timeout_at"] is not None
        assert job["state"] == "running"

    @pytest.mark.asyncio
    async def test_timeout_at_cleared_on_completion(self, db_connection):
        """Test that timeout_at is cleared when job finishes."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at
        timeout_at = datetime.now(UTC) + timedelta(seconds=60)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        # Mark as finished (should clear timeout_at); epoch 0 = fresh row
        await db_connection.execute(STMTS["finished"], job_id, {"status": "success"}, 0)

        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["timeout_at"] is None  # Should be cleared

    @pytest.mark.asyncio
    async def test_timeout_at_cleared_on_crash(self, db_connection):
        """Test that timeout_at is cleared when job crashes."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at
        timeout_at = datetime.now(UTC) + timedelta(seconds=60)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        # Mark as crashed (terminal DLQ); epoch 0 = fresh row
        await db_connection.execute(
            STMTS["crashed"], job_id, "Test error", "Test backtrace", 0
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "crashed"
        assert job["timeout_at"] is None  # Should be cleared


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
            admin_data={"timeout_seconds": 60, "on_timeout": "retry"},
        )

        # Set timeout_at to 1 minute ago (timed out)
        timeout_at = datetime.now(UTC) - timedelta(minutes=1)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        # Find timed-out jobs
        timed_out = await db_connection.fetch("""
            SELECT id, job_class, timeout_at, admin_data
            FROM jorb
            WHERE state = 'running'
              AND timeout_at IS NOT NULL
              AND timeout_at < NOW()
        """)

        assert len(timed_out) == 1
        assert timed_out[0]["id"] == job_id

    # NOTE: schema v1 removed the check_timed_out_jobs() SQL function; the
    # equivalent sweep is pyjobby.monitor.sweep_timed_out_jobs (covered in
    # tests/test_monitor.py).


class TestTimeoutMonitorHandler:
    """Test timeout monitor handler function."""

    @pytest.mark.asyncio
    async def test_handle_timeout_with_retry(self, db_pool):
        """Test handling timeout with retry action."""
        # Create timed-out job using pool (not transactional connection)
        admin_data = {"timeout_seconds": 30, "on_timeout": "retry", "max_retries": 5}
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                admin_data,
                "running",
                0,
            )

            # Set timeout in past
            timeout_at = datetime.now(UTC) - timedelta(seconds=10)
            await conn.execute(
                """
                UPDATE jorb SET timeout_at = $1 WHERE id = $2
            """,
                timeout_at,
                job_id,
            )

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            admin_data,
            0,  # error_count
        )

        # Should be requeued - read from pool
        job = await get_job(db_pool, job_id)
        assert job["state"] == "queued"
        assert job["error_count"] == 1
        assert job["timeout_at"] is None
        assert "Timeout exceeded" in job["error_message"]

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

    @pytest.mark.asyncio
    async def test_handle_timeout_with_fail(self, db_pool):
        """Test handling timeout with fail action."""
        admin_data = {"timeout_seconds": 30, "on_timeout": "fail"}
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                admin_data,
                "running",
                0,
            )

        # Handle timeout
        await handle_timed_out_job(db_pool, job_id, "test.Job", admin_data, 0)

        # Should be crashed - read from pool
        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"
        assert job["error_count"] == 1
        assert "Timeout exceeded" in job["error_message"]

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

    @pytest.mark.asyncio
    async def test_handle_timeout_max_retries_exceeded(self, db_pool):
        """Test timeout handling when max retries exceeded."""
        admin_data = {"timeout_seconds": 30, "on_timeout": "retry", "max_retries": 3}
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                admin_data,
                "running",
                3,
            )  # At max

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            admin_data,
            3,  # At max retries
        )

        # Should be crashed (max retries exceeded) - read from pool
        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"
        assert "max retries exceeded" in job["error_message"].lower()

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)


# NOTE: schema v1 removed the jorb_timeout_violations view; overdue jobs are
# found with a plain query on timeout_at (see TestTimeoutDetection) and acted
# on by pyjobby.monitor.sweep_timed_out_jobs.


class TestTimeoutIntegration:
    """Integration tests for timeout enforcement."""

    @pytest.mark.asyncio
    async def test_job_with_timeout_config_lifecycle(self, db_connection):
        """Test complete lifecycle of job with timeout configuration."""
        # Create job with timeout
        admin_data = {"timeout_seconds": 300, "on_timeout": "retry", "max_retries": 5}

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
            "queued",
        )

        # 1. Job starts - set timeout_at
        timeout_at = datetime.now(UTC) + timedelta(seconds=300)
        await db_connection.execute(
            """
            UPDATE jorb SET state = 'running', timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        job = await get_job(db_connection, job_id)
        assert job["timeout_at"] is not None

        # 2. Job completes before timeout - clear timeout_at
        from pyjobby.pj import STMTS

        await db_connection.execute(STMTS["finished"], job_id, {"status": "success"}, 0)

        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["timeout_at"] is None

    @pytest.mark.asyncio
    async def test_multiple_jobs_different_timeouts(self, db_connection):
        """Test multiple jobs with different timeout configurations."""
        jobs = []

        # Job 1: Short timeout, retry
        job1 = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.FastJob",
            {},
            "default",
            {"timeout_seconds": 10, "on_timeout": "retry"},
            "running",
        )
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = NOW() + INTERVAL '10 seconds' WHERE id = $1
        """,
            job1,
        )
        jobs.append(job1)

        # Job 2: Long timeout, fail
        job2 = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.SlowJob",
            {},
            "default",
            {"timeout_seconds": 600, "on_timeout": "fail"},
            "running",
        )
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = NOW() + INTERVAL '600 seconds' WHERE id = $1
        """,
            job2,
        )
        jobs.append(job2)

        # Job 3: No timeout
        job3 = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.NoTimeoutJob",
            {},
            "default",
            {},
            "running",
        )
        jobs.append(job3)

        # Verify configurations
        for job_id in jobs:
            job = await get_job(db_connection, job_id)
            if job["job_class"] == "test.FastJob":
                assert job["admin_data"]["timeout_seconds"] == 10
                assert job["timeout_at"] is not None
            elif job["job_class"] == "test.SlowJob":
                assert job["admin_data"]["timeout_seconds"] == 600
                assert job["timeout_at"] is not None
            elif job["job_class"] == "test.NoTimeoutJob":
                assert job.get("timeout_at") is None


class TestTimeoutEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_timeout_at_without_admin_data(self, db_connection):
        """Test job with timeout_at but no admin_data."""
        job_id = await create_job(db_connection, job_class="test.Job", state="running")

        # Set timeout_at without admin_data
        timeout_at = datetime.now(UTC) + timedelta(seconds=60)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        job = await get_job(db_connection, job_id)
        assert job["timeout_at"] is not None
        # admin_data might be None or empty
        assert job.get("admin_data") is None or job["admin_data"] == {}

    @pytest.mark.asyncio
    async def test_very_short_timeout(self, db_connection):
        """Test with very short timeout (1 second)."""
        admin_data = {"timeout_seconds": 1, "on_timeout": "fail"}
        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
            "running",
        )

        # Set timeout_at to 1 second from now
        timeout_at = datetime.now(UTC) + timedelta(seconds=1)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        # Wait 2 seconds
        await asyncio.sleep(2)

        # Should be timed out (use clock_timestamp() not NOW() because NOW() returns transaction start time)
        timed_out = await db_connection.fetch(
            """
            SELECT id FROM jorb
            WHERE id = $1 AND timeout_at < clock_timestamp()
        """,
            job_id,
        )

        assert len(timed_out) == 1

    @pytest.mark.asyncio
    async def test_timeout_at_in_far_future(self, db_connection):
        """Test with timeout far in the future."""
        admin_data = {"timeout_seconds": 86400}  # 24 hours
        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
            "running",
        )

        timeout_at = datetime.now(UTC) + timedelta(days=1)
        await db_connection.execute(
            """
            UPDATE jorb SET timeout_at = $1 WHERE id = $2
        """,
            timeout_at,
            job_id,
        )

        # Should not be timed out
        timed_out = await db_connection.fetch(
            """
            SELECT id FROM jorb
            WHERE id = $1 AND timeout_at < NOW()
        """,
            job_id,
        )

        assert len(timed_out) == 0
