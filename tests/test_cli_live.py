"""
Comprehensive LIVE CLI tests for pyjobby (pj-admin).

Tests the administrative CLI commands with REAL database operations.
NO MOCKS - all tests use live database connections and real data.

Coverage Target: 80%+
"""

import pytest
import asyncio
from click.testing import CliRunner
from datetime import datetime, timedelta
import tempfile
import os

from pyjobby.cli import cli
from pyjobby.admin_api import AdminAPI


@pytest.fixture
def cli_runner():
    """Create Click CLI test runner."""
    return CliRunner()


@pytest.fixture
async def admin_api(db_pool):
    """Create live AdminAPI instance with real database connection."""
    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)
        yield api


@pytest.fixture
def temp_config_file(db_params):
    """Create temporary config file with real database parameters."""
    config_content = f"""
DB_PARAMS = {{
    'user': '{db_params['user']}',
    'password': '{db_params['password']}',
    'host': '{db_params['host']}',
    'port': {db_params['port']},
    'database': '{db_params['database']}'
}}
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(config_content)
        temp_path = f.name

    yield temp_path

    # Cleanup
    try:
        os.unlink(temp_path)
    except:
        pass


# ============================================================================
# Test Job Commands with LIVE Database
# ============================================================================

class TestJobsCommandsLive:
    """Test 'jobs' command group with live database operations."""

    def test_jobs_list_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs list with real database data."""
        # Create real test jobs in database (using sync wrapper)
        async def setup_data():
            async with db_pool.acquire() as conn:
                job_id1 = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                    RETURNING id
                """, 'test.LiveJob', {'key': 'value1'}, 'default', 'queued', 100)

                job_id2 = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                    RETURNING id
                """, 'test.LiveJob2', {'key': 'value2'}, 'high', 'running', 200)
                return job_id1, job_id2

        job_id1, job_id2 = asyncio.run(setup_data())

        # Run CLI command (it will create its own event loop)
        result = cli_runner.invoke(cli, ['--config', temp_config_file, 'jobs', 'list'])

        # Verify output
        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert 'LiveJob' in result.output
        assert str(job_id1) in result.output or str(job_id2) in result.output

    
    def test_jobs_list_with_queue_filter_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs list with queue filter using real data."""
        async with db_pool.acquire() as conn:
            # Create jobs in different queues
            await conn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            """, 'test.QueueJob', {}, 'specific_queue', 'queued', 100)

            await conn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            """, 'test.OtherJob', {}, 'other_queue', 'queued', 100)

        # Filter by specific queue
        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'jobs', 'list',
            '--queue', 'specific_queue'
        ])

        assert result.exit_code == 0
        assert 'QueueJob' in result.output
        # Should not show jobs from other queue
        assert 'OtherJob' not in result.output

    
    def test_jobs_inspect_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs inspect with real job data."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, admin_data)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
                RETURNING id
            """, 'test.InspectJob', {'arg1': 'val1', 'arg2': 42}, 'default', 'queued', 150,
                {'timeout_seconds': 60})

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'jobs', 'inspect', str(job_id)
        ])

        assert result.exit_code == 0
        assert 'InspectJob' in result.output
        assert str(job_id) in result.output
        assert 'queued' in result.output.lower()

    
    def test_jobs_retry_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs retry with real database update."""
        async with db_pool.acquire() as conn:
            # Create crashed job
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, error_count)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 2)
                RETURNING id
            """, 'test.RetryJob', {}, 'default', 'crashed', 100)

        # Retry the job
        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'jobs', 'retry', str(job_id)
        ])

        assert result.exit_code == 0

        # Verify job was actually retried in database
        async with db_pool.acquire() as conn:
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == 'queued'

    
    def test_jobs_cancel_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs cancel with real database update."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'test.CancelJob', {}, 'default', 'queued', 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'jobs', 'cancel', str(job_id)
        ])

        assert result.exit_code == 0

        # Verify job was cancelled
        async with db_pool.acquire() as conn:
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == 'cancelled'

    
    def test_jobs_delete_live(self, cli_runner, temp_config_file, db_pool):
        """Test jobs delete with real database deletion."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                RETURNING id
            """, 'test.DeleteJob', {}, 'default', 'finished', 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'jobs', 'delete', str(job_id), '--force'
        ])

        assert result.exit_code == 0

        # Verify job was deleted
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT COUNT(*) FROM jorb WHERE id = $1", job_id)
            assert exists == 0


# ============================================================================
# Test Queue Commands with LIVE Database
# ============================================================================

class TestQueuesCommandsLive:
    """Test 'queues' command group with live database operations."""

    
    def test_queues_list_live(self, cli_runner, temp_config_file, db_pool):
        """Test queues list with real queue data."""
        async with db_pool.acquire() as conn:
            # Create jobs in different queues
            await conn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            """, 'test.Job', {}, 'queue_alpha', 'queued', 100)

            await conn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            """, 'test.Job', {}, 'queue_beta', 'queued', 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'queues', 'list'
        ])

        assert result.exit_code == 0
        assert 'queue_alpha' in result.output
        assert 'queue_beta' in result.output

    
    def test_queues_stats_live(self, cli_runner, temp_config_file, db_pool):
        """Test queues stats with real statistics."""
        async with db_pool.acquire() as conn:
            # Create jobs in various states
            for state in ['queued', 'running', 'finished', 'crashed']:
                await conn.execute("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                """, 'test.StatsJob', {}, 'stats_queue', state, 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'queues', 'stats', '--queue', 'stats_queue'
        ])

        assert result.exit_code == 0
        # Should show counts for different states
        assert 'queued' in result.output.lower() or 'running' in result.output.lower()

    
    def test_queues_clear_live(self, cli_runner, temp_config_file, db_pool):
        """Test queues clear with real database deletion."""
        async with db_pool.acquire() as conn:
            # Create jobs in queue to be cleared
            for i in range(3):
                await conn.execute("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                """, f'test.ClearJob{i}', {}, 'clear_queue', 'finished', 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'queues', 'clear', 'clear_queue', '--force'
        ])

        assert result.exit_code == 0

        # Verify jobs were cleared
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE queue = $1", 'clear_queue'
            )
            assert count == 0


# ============================================================================
# Test DLQ Commands with LIVE Database
# ============================================================================

class TestDLQCommandsLive:
    """Test 'dlq' (Dead Letter Queue) command group with live database."""

    
    def test_dlq_list_live(self, cli_runner, temp_config_file, db_pool):
        """Test DLQ list with real crashed jobs."""
        async with db_pool.acquire() as conn:
            # Create crashed jobs (max retries exceeded)
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 error_count, error_message)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 10, $6)
                RETURNING id
            """, 'test.DLQJob', {}, 'default', 'crashed', 100, 'Max retries exceeded')

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'dlq', 'list'
        ])

        assert result.exit_code == 0
        assert str(job_id) in result.output or 'DLQJob' in result.output

    
    def test_dlq_retry_live(self, cli_runner, temp_config_file, db_pool):
        """Test DLQ retry with real job resurrection."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated,
                                 error_count, error_message)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), 10, $6)
                RETURNING id
            """, 'test.DLQRetryJob', {}, 'default', 'crashed', 100, 'Failed')

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'dlq', 'retry', str(job_id)
        ])

        assert result.exit_code == 0

        # Verify new job was created (original remains crashed)
        async with db_pool.acquire() as conn:
            new_jobs = await conn.fetch("""
                SELECT id, state FROM jorb
                WHERE job_class = 'test.DLQRetryJob' AND state = 'queued'
            """)
            assert len(new_jobs) > 0


# ============================================================================
# Test Schedule Commands with LIVE Database
# ============================================================================

class TestScheduleCommandsLive:
    """Test 'schedule' command group with live database operations."""

    
    def test_schedule_list_live(self, cli_runner, temp_config_file, db_pool):
        """Test schedule list with real schedules."""
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, kwargs, created, updated)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
            """, 'test-schedule-live', 'test.ScheduledJob', '0 * * * *', 'default', True, {})

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'schedule', 'list'
        ])

        assert result.exit_code == 0
        assert 'test-schedule-live' in result.output

    
    def test_schedule_add_live(self, cli_runner, temp_config_file, db_pool):
        """Test schedule add with real database insert."""
        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'schedule', 'add',
            'new-test-schedule', 'test.NewJob', '*/5 * * * *',
            '--queue', 'default'
        ])

        assert result.exit_code == 0

        # Verify schedule was created
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1",
                'new-test-schedule'
            )
            assert exists == 1

    
    def test_schedule_enable_disable_live(self, cli_runner, temp_config_file, db_pool):
        """Test schedule enable/disable with real updates."""
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, kwargs, created, updated)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
            """, 'toggle-schedule', 'test.ToggleJob', '0 * * * *', 'default', True, {})

        # Disable
        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'schedule', 'disable', 'toggle-schedule'
        ])
        assert result.exit_code == 0

        # Verify disabled
        async with db_pool.acquire() as conn:
            enabled = await conn.fetchval(
                "SELECT enabled FROM jorb_schedule WHERE name = $1", 'toggle-schedule'
            )
            assert enabled is False

        # Enable
        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'schedule', 'enable', 'toggle-schedule'
        ])
        assert result.exit_code == 0

        # Verify enabled
        async with db_pool.acquire() as conn:
            enabled = await conn.fetchval(
                "SELECT enabled FROM jorb_schedule WHERE name = $1", 'toggle-schedule'
            )
            assert enabled is True

    
    def test_schedule_delete_live(self, cli_runner, temp_config_file, db_pool):
        """Test schedule delete with real database deletion."""
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, kwargs, created, updated)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
            """, 'delete-schedule', 'test.DeleteJob', '0 * * * *', 'default', True, {})

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'schedule', 'delete', 'delete-schedule', '--force'
        ])

        assert result.exit_code == 0

        # Verify deleted
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", 'delete-schedule'
            )
            assert exists == 0


# ============================================================================
# Test Metrics Commands with LIVE Database
# ============================================================================

class TestMetricsCommandsLive:
    """Test 'metrics' command with live database statistics."""

    
    def test_metrics_live(self, cli_runner, temp_config_file, db_pool):
        """Test metrics with real job execution data."""
        async with db_pool.acquire() as conn:
            # Create finished jobs with execution times
            for i in range(5):
                await conn.execute("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, updated, finished)
                    VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '1 hour', NOW(), NOW())
                """, 'test.MetricsJob', {}, 'default', 'finished', 100)

        result = cli_runner.invoke(cli, [
            '--config', temp_config_file, 'metrics'
        ])

        assert result.exit_code == 0
        # Should show some metrics output
        assert len(result.output) > 0


# ============================================================================
# Test Helper Functions (no database needed)
# ============================================================================

class TestHelperFunctionsLive:
    """Test CLI helper functions."""

    def test_print_success(self):
        """Test print_success helper."""
        from pyjobby.cli import print_success
        print_success("Test message")  # Should not raise

    def test_print_error(self):
        """Test print_error helper."""
        from pyjobby.cli import print_error
        print_error("Test error")  # Should not raise

    def test_print_warning(self):
        """Test print_warning helper."""
        from pyjobby.cli import print_warning
        print_warning("Test warning")  # Should not raise

    def test_print_table(self):
        """Test print_table helper."""
        from pyjobby.cli import print_table
        headers = ['ID', 'Name', 'Status']
        rows = [['1', 'test', 'active'], ['2', 'demo', 'inactive']]
        print_table(headers, rows)  # Should not raise
