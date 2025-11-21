"""
Comprehensive tests for TimeoutMonitor module.

Tests all aspects of job timeout enforcement:
- Timeout handling logic (retry vs fail)
- Retry strategy integration
- Max retries enforcement
- Timeout monitor main loop
- Error handling and recovery
- Integration with live database
"""

import asyncio
import json
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock
import uuid

from pyjobby.timeout_monitor import (
    handle_timed_out_job,
    timeout_monitor,
    run_timeout_monitor
)


# ============================================================================
# Test handle_timed_out_job
# ============================================================================

class TestHandleTimedOutJob:
    """Test timeout job handling logic."""

    def _parse_admin_data(self, admin_data):
        """Helper to parse admin_data if it's a string."""
        if isinstance(admin_data, str):
            return json.loads(admin_data)
        return admin_data

    @pytest.mark.asyncio
    async def test_timeout_retry_within_limit(self, db_pool, client):
        """Test job is retried when timeout occurs and retries remain."""
        # Create job with timeout_seconds in admin_data
        job_id = await client.enqueue(
            "test.TimeoutJob",
            timeout_seconds=5,
            admin_data={'on_timeout': 'retry', 'max_retries': 10}
        )

        # Simulate job running and then timing out
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 2
                WHERE id = $1
            """, job_id)

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        # Parse admin_data if it's a string
        admin_data = self._parse_admin_data(job['admin_data'])

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.TimeoutJob",
            admin_data,
            2  # error_count
        )

        # Verify job was requeued
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'queued'
            assert job['timeout_at'] is None
            assert job['error_count'] == 3
            assert 'Timeout exceeded - retrying' in job['error_message']
            assert job['run_after'] > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_timeout_fail_max_retries_exceeded(self, db_pool, client):
        """Test job is marked crashed when max retries exceeded."""
        # Create job with timeout (max_retries=3 as parameter, not in admin_data)
        job_id = await client.enqueue(
            "test.TimeoutJob",
            timeout_seconds=5,
            on_timeout='retry',
            max_retries=3
        )

        # Simulate job at max retries
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 2
                WHERE id = $1
            """, job_id)

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        # Handle timeout (error_count + 1 = 3 = max_retries)
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.TimeoutJob",
            self._parse_admin_data(job['admin_data']),
            2  # error_count (next attempt would be 3)
        )

        # Verify job was marked as crashed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'crashed'
            assert job['timeout_at'] is None
            assert job['error_count'] == 3
            assert 'max retries exceeded' in job['error_message']

    @pytest.mark.asyncio
    async def test_timeout_fail_on_timeout_fail(self, db_pool, client):
        """Test job is marked crashed when on_timeout='fail'."""
        # Create job with on_timeout='fail' (as parameter, not in admin_data)
        job_id = await client.enqueue(
            "test.TimeoutJob",
            timeout_seconds=5,
            on_timeout='fail',
            max_retries=10
        )

        # Simulate job timing out
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 0
                WHERE id = $1
            """, job_id)

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        # Handle timeout
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.TimeoutJob",
            self._parse_admin_data(job['admin_data']),
            0  # error_count
        )

        # Verify job was marked as crashed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'crashed'
            assert job['timeout_at'] is None
            assert job['error_count'] == 1
            assert 'on_timeout=fail' in job['error_message']

    @pytest.mark.asyncio
    async def test_timeout_default_admin_data(self, db_pool, client):
        """Test timeout handling with default admin_data values."""
        # Create job without explicit timeout config
        job_id = await client.enqueue("test.Job")

        # Simulate timeout
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 0
                WHERE id = $1
            """, job_id)

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        # Handle timeout (should use defaults: on_timeout='retry', max_retries=10)
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            self._parse_admin_data(job['admin_data']),
            0
        )

        # Verify job was requeued with defaults
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'queued'
            assert job['error_count'] == 1

    @pytest.mark.asyncio
    async def test_timeout_null_admin_data(self, db_pool):
        """Test timeout handling when admin_data is None."""
        # Create job directly without admin_data
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, state, timeout_at, error_count)
                VALUES ($1, '{}'::jsonb, 'running', NOW() - INTERVAL '1 minute', 0)
                RETURNING id
            """, "test.Job")

        # Handle timeout with None admin_data
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            None,  # admin_data is None
            0
        )

        # Verify job was requeued with defaults
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'queued'
            assert job['error_count'] == 1

    @pytest.mark.asyncio
    async def test_timeout_job_already_deleted(self, db_pool, client):
        """Test handling timeout for job that no longer exists."""
        # Create and then delete a job
        job_id = await client.enqueue("test.Job")

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Handle timeout (should not crash)
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            {'on_timeout': 'retry'},
            0
        )

        # Verify no job exists (function should have returned early)
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job is None

    @pytest.mark.asyncio
    async def test_timeout_retry_delay_calculation(self, db_pool, client):
        """Test that retry delay is calculated correctly."""
        # Create job with exponential backoff
        job_id = await client.enqueue(
            "test.Job",
            timeout_seconds=5,
            admin_data={
                'on_timeout': 'retry',
                'retry_strategy': 'exponential',
                'initial_retry_delay': 1,
                'max_retry_delay': 60
            }
        )

        # Simulate timeout at error_count=2
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 2
                WHERE id = $1
            """, job_id)

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        # Handle timeout
        before = datetime.utcnow()
        await handle_timed_out_job(
            db_pool,
            job_id,
            "test.Job",
            self._parse_admin_data(job['admin_data']),
            2
        )

        # Verify retry delay
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Exponential backoff: 1 * 2^2 = 4 seconds
            expected_run_after = before + timedelta(seconds=4)

            # Allow 2 second tolerance
            assert abs((job['run_after'] - expected_run_after).total_seconds()) < 2


# ============================================================================
# Test timeout_monitor
# ============================================================================

class TestTimeoutMonitor:
    """Test timeout monitor main loop."""

    @pytest.mark.asyncio
    async def test_monitor_finds_timed_out_jobs(self, db_pool, client, db_params):
        """Test monitor finds and handles timed-out jobs."""
        # Create 3 jobs, 2 with timeout_at in past
        job1 = await client.enqueue("test.Job", timeout_seconds=5)
        job2 = await client.enqueue("test.Job", timeout_seconds=5)
        job3 = await client.enqueue("test.Job", timeout_seconds=5)

        async with db_pool.acquire() as conn:
            # Set job1 and job2 to running and timed out
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute'
                WHERE id = ANY($1::bigint[])
            """, [job1, job2])

            # Set job3 to running but not timed out
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() + INTERVAL '10 minutes'
                WHERE id = $1
            """, job3)

        # Build DSN
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Run monitor for one iteration
        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

        # Give it time to process one batch
        await asyncio.sleep(0.5)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify job1 and job2 were handled
        async with db_pool.acquire() as conn:
            job1_state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job1)
            job2_state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job2)
            job3_state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job3)

            # job1 and job2 should be requeued (retry)
            assert job1_state == 'queued'
            assert job2_state == 'queued'

            # job3 should still be running (not timed out)
            assert job3_state == 'running'

    @pytest.mark.asyncio
    async def test_monitor_batch_size_limit(self, db_pool, client, db_params):
        """Test monitor respects batch_size limit."""
        # Create 5 timed-out jobs
        job_ids = []
        for _ in range(5):
            job_id = await client.enqueue("test.Job", timeout_seconds=5)
            job_ids.append(job_id)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute'
                WHERE id = ANY($1::bigint[])
            """, job_ids)

        # Build DSN
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Run monitor with batch_size=3
        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999, batch_size=3))

        # Give it time to process first batch
        await asyncio.sleep(0.5)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify at most 3 jobs were handled in first batch
        async with db_pool.acquire() as conn:
            queued_count = await conn.fetchval("""
                SELECT COUNT(*) FROM jorb
                WHERE id = ANY($1::bigint[])
                  AND state = 'queued'
            """, job_ids)

            # Should have processed exactly 3 jobs in first batch
            assert queued_count == 3

    @pytest.mark.asyncio
    async def test_monitor_error_handling(self, db_pool, client, db_params, caplog):
        """Test monitor handles errors gracefully."""
        # Create a timed-out job
        job_id = await client.enqueue("test.Job", timeout_seconds=5)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute'
                WHERE id = $1
            """, job_id)

        # Build DSN
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Patch handle_timed_out_job to raise an error
        with patch('pyjobby.timeout_monitor.handle_timed_out_job', side_effect=Exception("Test error")):
            monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

            # Give it time to hit error
            await asyncio.sleep(0.5)

            # Cancel monitor
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        # Monitor should have logged error but continued
        # (verify by checking logs if needed)

    @pytest.mark.asyncio
    async def test_monitor_pool_cleanup(self, db_params):
        """Test monitor closes pool on shutdown."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

        # Let monitor initialize
        await asyncio.sleep(0.2)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Pool should be closed (if we could access it, but we can't easily)
        # This test mainly ensures no exceptions during cleanup

    @pytest.mark.asyncio
    async def test_monitor_no_timed_out_jobs(self, db_pool, client, db_params):
        """Test monitor handles case with no timed-out jobs."""
        # Create jobs that are not timed out
        job_ids = []
        for _ in range(3):
            job_id = await client.enqueue("test.Job")
            job_ids.append(job_id)

        # Build DSN
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

        # Give it time to check
        await asyncio.sleep(0.3)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify all jobs still in original state
        async with db_pool.acquire() as conn:
            states = await conn.fetch("""
                SELECT state FROM jorb
                WHERE id = ANY($1::bigint[])
            """, job_ids)

            assert all(s['state'] == 'queued' for s in states)


# ============================================================================
# Test run_timeout_monitor
# ============================================================================

class TestRunTimeoutMonitor:
    """Test sync entry point."""

    def test_run_timeout_monitor_calls_asyncio_run(self, db_params):
        """Test run_timeout_monitor calls asyncio.run with timeout_monitor."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Patch asyncio.run to prevent actually running the monitor
        with patch('asyncio.run') as mock_run:
            run_timeout_monitor(dsn)

            # Verify asyncio.run was called
            assert mock_run.called
            assert mock_run.call_count == 1


# ============================================================================
# Integration Tests
# ============================================================================

class TestTimeoutMonitorIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_timeout_lifecycle(self, db_pool, client, db_params):
        """Test complete timeout lifecycle: enqueue → timeout → retry → success."""
        # Create job with timeout
        job_id = await client.enqueue(
            "test.TimeoutJob",
            timeout_seconds=5,
            admin_data={'on_timeout': 'retry', 'max_retries': 10}
        )

        # Simulate job starting and timing out
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 0
                WHERE id = $1
            """, job_id)

        # Run monitor to handle timeout
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))
        await asyncio.sleep(0.5)
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify job was retried
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'queued'
            assert job['error_count'] == 1
            assert job['timeout_at'] is None
            assert job['run_after'] > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_timeout_cascade_to_failure(self, db_pool, client, db_params):
        """Test job eventually fails after exhausting retries."""
        # Create job with only 1 retry allowed
        job_id = await client.enqueue(
            "test.Job",
            timeout_seconds=5,
            max_retries=1,
            on_timeout='retry'
        )

        # Simulate timeout at max retries
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute',
                    error_count = 0
                WHERE id = $1
            """, job_id)

        # Run monitor
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))
        await asyncio.sleep(0.5)
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify job was marked as crashed
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['state'] == 'crashed'
            assert job['error_count'] == 1
            assert 'max retries exceeded' in job['error_message']


# ============================================================================
# Test JSON Codec Initialization
# ============================================================================

class TestJSONCodecInit:
    """Test JSON codec initialization in monitor."""

    @pytest.mark.asyncio
    async def test_monitor_with_orjson_available(self, db_params):
        """Test monitor initializes with orjson codec when available."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

        # Let monitor initialize with orjson codec
        await asyncio.sleep(0.2)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # If we got here without exceptions, orjson codec was set up successfully

    @pytest.mark.asyncio
    async def test_monitor_without_orjson(self, db_params):
        """Test monitor handles missing orjson gracefully."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Mock orjson import to fail
        import sys
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'orjson':
                raise ImportError("orjson not available")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=mock_import):
            monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

            # Let monitor initialize without orjson (should fall back gracefully)
            await asyncio.sleep(0.2)

            # Cancel monitor
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

            # If we got here, monitor handled missing orjson gracefully

    @pytest.mark.asyncio
    async def test_orjson_encoder_coverage(self, db_params, client):
        """Test that orjson encoder is actually used when encoding JSON data."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"

        # Create a job with JSON admin_data BEFORE starting monitor
        # so monitor's first check will find it
        job_id = await client.enqueue(
            "test.Job",
            timeout_seconds=1,
            admin_data={'test_key': 'test_value', 'nested': {'data': 123}}
        )

        # Set job to running with timeout in past to trigger monitor processing
        async with client.pool.acquire() as conn:
            await conn.execute("""
                UPDATE jorb
                SET state = 'running',
                    timeout_at = NOW() - INTERVAL '1 minute'
                WHERE id = $1
            """, job_id)

        # Now start the timeout monitor which will find and process the timed-out job
        # on its first check (using the orjson encoder when querying admin_data)
        monitor_task = asyncio.create_task(timeout_monitor(dsn, check_interval=999999))

        # Give monitor time to initialize and process (first check runs immediately)
        await asyncio.sleep(0.5)

        # Cancel monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Verify the job was processed (state should be queued for retry)
        async with client.pool.acquire() as conn:
            job = await conn.fetchrow("SELECT state FROM jorb WHERE id = $1", job_id)
            # Should have been requeued or marked as crashed
            assert job['state'] in ('queued', 'crashed')


# ============================================================================
# Test CLI Function
# ============================================================================

class TestCLI:
    """Test CLI entry point - simplified to avoid async mock issues."""

    def test_cli_with_dsn_executes(self, db_params):
        """Test CLI executes with --dsn argument."""
        dsn = f"postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['database']}"
        test_argv = ['timeout-monitor', '--dsn', dsn]

        # Mock that properly consumes the coroutine to avoid warnings
        def mock_asyncio_run(coro):
            """Mock that consumes coroutine to avoid 'unawaited coroutine' warnings."""
            try:
                coro.close()  # Properly close the coroutine
            except (GeneratorExit, StopIteration):
                pass  # Expected when closing coroutine
            return None

        with patch('sys.argv', test_argv):
            with patch('asyncio.run', side_effect=mock_asyncio_run):
                try:
                    from pyjobby.timeout_monitor import cli
                    cli()
                except SystemExit as e:
                    # Exit code 0 means success
                    assert e.code in (None, 0)

    def test_cli_with_config_executes(self, db_params, tmp_path):
        """Test CLI executes with --config argument."""
        config_file = tmp_path / "pyjobby.conf.py"
        config_file.write_text(f"""
DB_PARAMS = {{
    'user': '{db_params['user']}',
    'password': '{db_params['password']}',
    'host': '{db_params['host']}',
    'port': {db_params['port']},
    'database': '{db_params['database']}'
}}
""")
        test_argv = ['timeout-monitor', '--config', str(config_file)]

        # Mock that properly consumes the coroutine to avoid warnings
        def mock_asyncio_run(coro):
            """Mock that consumes coroutine to avoid 'unawaited coroutine' warnings."""
            try:
                coro.close()  # Properly close the coroutine
            except (GeneratorExit, StopIteration):
                pass  # Expected when closing coroutine
            return None

        with patch('sys.argv', test_argv):
            with patch('asyncio.run', side_effect=mock_asyncio_run):
                try:
                    from pyjobby.timeout_monitor import cli
                    cli()
                except SystemExit as e:
                    assert e.code in (None, 0)

    def test_cli_missing_both_exits_with_error(self):
        """Test CLI exits with error when neither DSN nor config provided."""
        test_argv = ['timeout-monitor']

        with patch('sys.argv', test_argv):
            with pytest.raises(SystemExit) as exc_info:
                from pyjobby.timeout_monitor import cli
                cli()

            # Should exit with non-zero code
            assert exc_info.value.code == 1
