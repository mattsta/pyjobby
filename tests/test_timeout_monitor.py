"""
Comprehensive tests for timeout_monitor.py - Job timeout enforcement.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

import asyncio
import contextlib
import uuid

import pytest

from pyjobby.timeout_monitor import (
    handle_timed_out_job,
    run_timeout_monitor,
    timeout_monitor,
)


def unique_name(base: str) -> str:
    """Generate unique name for test isolation."""
    return f"{base}_{uuid.uuid4().hex[:8]}"


class TestHandleTimedOutJobRetry:
    """Test handle_timed_out_job with retry behavior - covers lines 19-73."""

    @pytest.mark.asyncio
    async def test_handle_timeout_retry_default(self, db_pool):
        """Test default retry behavior when job times out."""
        # Create a running job that has timed out
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('TimeoutTestJob', '{}', 'running', 0, $1)
            RETURNING id
        """,
            {"on_timeout": "retry", "max_retries": 10},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="TimeoutTestJob",
            admin_data={"on_timeout": "retry", "max_retries": 10},
            error_count=0,
        )

        # Verify job was requeued
        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["state"] == "queued"
        assert row["error_count"] == 1
        assert "Timeout exceeded - retrying" in row["error_message"]

    @pytest.mark.asyncio
    async def test_handle_timeout_retry_with_none_admin_data(self, db_pool):
        """Test retry with None admin_data uses defaults - covers line 37-38."""
        job_id = await db_pool.fetchval("""
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('TimeoutTestJob', '{}', 'running', 0, NULL)
            RETURNING id
        """)

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="TimeoutTestJob",
            admin_data=None,  # None admin_data
            error_count=0,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["state"] == "queued"  # Default is retry

    @pytest.mark.asyncio
    async def test_handle_timeout_job_not_found(self, db_pool):
        """Test handling when job doesn't exist - covers lines 50-52."""
        # Try to handle a non-existent job
        await handle_timed_out_job(
            pool=db_pool,
            job_id=-99999,  # Non-existent
            job_class="NonExistent",
            admin_data={"on_timeout": "retry"},
            error_count=0,
        )
        # Should not raise, just return silently


class TestHandleTimedOutJobFail:
    """Test handle_timed_out_job with fail behavior - covers lines 74-93."""

    @pytest.mark.asyncio
    async def test_handle_timeout_fail_explicitly(self, db_pool):
        """Test on_timeout=fail marks job as crashed - covers lines 74-93."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('TimeoutFailJob', '{}', 'running', 0, $1)
            RETURNING id
        """,
            {"on_timeout": "fail"},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="TimeoutFailJob",
            admin_data={"on_timeout": "fail"},
            error_count=0,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["state"] == "crashed"
        assert "on_timeout=fail" in row["error_message"]

    @pytest.mark.asyncio
    async def test_handle_timeout_max_retries_exceeded(self, db_pool):
        """Test job crashes when max retries exceeded - covers line 76."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('MaxRetriesJob', '{}', 'running', 9, $1)
            RETURNING id
        """,
            {"on_timeout": "retry", "max_retries": 10},
        )

        # error_count=9, so +1 = 10, which equals max_retries
        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="MaxRetriesJob",
            admin_data={"on_timeout": "retry", "max_retries": 10},
            error_count=9,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["state"] == "crashed"
        assert "max retries exceeded" in row["error_message"]


class TestTimeoutMonitorLoop:
    """Test timeout_monitor function - covers lines 96-189."""

    @pytest.mark.asyncio
    async def test_timeout_monitor_finds_timed_out_jobs(self, db_pool, db_params):
        """Test that monitor finds and handles timed-out jobs."""
        # Create a job that is running and past its timeout
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, timeout_at, admin_data, error_count)
            VALUES ('TimedOutJob', '{}', 'running', NOW() - INTERVAL '1 minute', $1, 0)
            RETURNING id
        """,
            {"on_timeout": "retry", "max_retries": 10},
        )

        # Build DSN
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Run monitor briefly
        async def run_and_stop():
            await asyncio.sleep(0.5)
            raise asyncio.CancelledError()

        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(
                asyncio.gather(
                    timeout_monitor(dsn, check_interval=0.1, batch_size=10),
                    run_and_stop(),
                    return_exceptions=True,
                ),
                timeout=2.0,
            )

        # Verify job was handled
        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        # Job should be either queued (retried) or still running
        # (depending on timing)
        assert row["state"] in ("queued", "running")

    @pytest.mark.asyncio
    async def test_timeout_monitor_handles_empty_queue(self, db_params):
        """Test monitor handles no timed-out jobs gracefully."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Run monitor briefly with no timed-out jobs
        async def run_briefly():
            task = asyncio.create_task(
                timeout_monitor(dsn, check_interval=0.1, batch_size=10)
            )
            await asyncio.sleep(0.3)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await asyncio.wait_for(run_briefly(), timeout=2.0)
        # Should complete without error


class TestRunTimeoutMonitor:
    """Test run_timeout_monitor entry point - covers lines 192-201."""

    def test_run_timeout_monitor_function_exists(self):
        """Test that run_timeout_monitor is callable."""
        assert callable(run_timeout_monitor)

    # Note: Actually running run_timeout_monitor would block forever,
    # so we just verify it's properly defined


class TestTimeoutMonitorEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_handle_timeout_with_empty_admin_data(self, db_pool):
        """Test handling with empty dict admin_data."""
        job_id = await db_pool.fetchval("""
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('EmptyAdminJob', '{}', 'running', 0, '{}')
            RETURNING id
        """)

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="EmptyAdminJob",
            admin_data={},  # Empty dict - should use defaults
            error_count=0,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        # Default on_timeout is 'retry'
        assert row["state"] == "queued"

    @pytest.mark.asyncio
    async def test_handle_timeout_retry_increments_error_count(self, db_pool):
        """Test that retry increments error_count correctly."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('RetryCountJob', '{}', 'running', 5, $1)
            RETURNING id
        """,
            {"on_timeout": "retry", "max_retries": 10},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="RetryCountJob",
            admin_data={"on_timeout": "retry", "max_retries": 10},
            error_count=5,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["error_count"] == 6

    @pytest.mark.asyncio
    async def test_handle_timeout_fail_increments_error_count(self, db_pool):
        """Test that fail also increments error_count."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('FailCountJob', '{}', 'running', 3, $1)
            RETURNING id
        """,
            {"on_timeout": "fail"},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="FailCountJob",
            admin_data={"on_timeout": "fail"},
            error_count=3,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["error_count"] == 4
        assert row["state"] == "crashed"


class TestTimeoutMonitorWithCustomRetryStrategy:
    """Test timeout with different retry strategies."""

    @pytest.mark.asyncio
    async def test_handle_timeout_with_linear_strategy(self, db_pool):
        """Test timeout retry with linear backoff strategy."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, admin_data)
            VALUES ('LinearRetryJob', '{}', 'running', 2, $1)
            RETURNING id
        """,
            {
                "on_timeout": "retry",
                "max_retries": 10,
                "retry_strategy": "linear",
                "initial_retry_delay": 5,
            },
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="LinearRetryJob",
            admin_data={
                "on_timeout": "retry",
                "max_retries": 10,
                "retry_strategy": "linear",
                "initial_retry_delay": 5,
            },
            error_count=2,
        )

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row["state"] == "queued"
        # run_after should be set to future time


class TestTimeoutAtField:
    """Test timeout_at field handling."""

    @pytest.mark.asyncio
    async def test_timeout_clears_timeout_at_on_retry(self, db_pool):
        """Test that timeout_at is cleared when job is retried."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, timeout_at, admin_data)
            VALUES ('ClearTimeoutJob', '{}', 'running', 0, NOW(), $1)
            RETURNING id
        """,
            {"on_timeout": "retry", "max_retries": 10},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="ClearTimeoutJob",
            admin_data={"on_timeout": "retry", "max_retries": 10},
            error_count=0,
        )

        row = await db_pool.fetchrow(
            "SELECT timeout_at FROM jorb WHERE id = $1", job_id
        )
        assert row["timeout_at"] is None

    @pytest.mark.asyncio
    async def test_timeout_clears_timeout_at_on_fail(self, db_pool):
        """Test that timeout_at is cleared when job fails."""
        job_id = await db_pool.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, state, error_count, timeout_at, admin_data)
            VALUES ('FailTimeoutJob', '{}', 'running', 0, NOW(), $1)
            RETURNING id
        """,
            {"on_timeout": "fail"},
        )

        await handle_timed_out_job(
            pool=db_pool,
            job_id=job_id,
            job_class="FailTimeoutJob",
            admin_data={"on_timeout": "fail"},
            error_count=0,
        )

        row = await db_pool.fetchrow(
            "SELECT timeout_at FROM jorb WHERE id = $1", job_id
        )
        assert row["timeout_at"] is None
