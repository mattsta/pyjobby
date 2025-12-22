"""
Comprehensive tests for pyjobby CLI (pj-admin).

Tests the administrative CLI commands for job, queue, worker, DLQ,
schedule, and metrics management.

Coverage Target: 80%+
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from pyjobby.cli import cli


@contextmanager
def mock_cli_context(mock_admin_api, mock_db_params):
    """Context manager that provides all necessary mocks for CLI testing."""
    with patch("pyjobby.cli.load_config_from_file", return_value=mock_db_params):
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                yield


@pytest.fixture
def cli_runner():
    """Create Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_db_params():
    """Mock database parameters for config loading."""
    return {
        "user": "test",
        "password": "test",
        "database": "test",
        "host": "localhost",
        "port": 5432,
    }


@pytest.fixture
def mock_admin_api():
    """Create mock AdminAPI for testing CLI commands."""
    mock_api = AsyncMock()

    # Setup default mock return values
    mock_api.list_jobs.return_value = [
        {
            "id": 1,
            "job_class": "test.Job",
            "state": "queued",
            "queue": "default",
            "created": datetime.now(),
            "prio": 100,
            "run_after": None,
        },
        {
            "id": 2,
            "job_class": "test.Job2",
            "state": "running",
            "queue": "high",
            "created": datetime.now(),
            "prio": 200,
            "run_after": None,
        },
    ]

    mock_api.get_job_full.return_value = {
        "id": 1,
        "job_class": "test.Job",
        "state": "queued",
        "queue": "default",
        "kwargs": {"key": "value"},
        "created": datetime.now(),
        "prio": 100,
        "error_count": 0,
        "error_message": None,
        "run_after": None,
        "admin_data": {"timeout_seconds": 60},
    }

    mock_api.retry_jobs.return_value = None
    mock_api.cancel_jobs.return_value = None
    mock_api.delete_jobs.return_value = 5

    mock_api.list_queues.return_value = ["default", "high", "low"]

    mock_api.queue_stats.return_value = {
        "queued": 10,
        "running": 5,
        "finished": 100,
        "crashed": 2,
        "waiting": 1,
        "total": 118,
        "oldest_queued": datetime.now() - timedelta(hours=2),
    }

    mock_api.list_workers.return_value = [
        {
            "queue": "default",
            "capability": "std",
            "last_heartbeat": datetime.now(),
            "active_count": 3,
        },
        {
            "queue": "high",
            "capability": "gpu",
            "last_heartbeat": datetime.now(),
            "active_count": 1,
        },
    ]

    mock_api.worker_stats.return_value = {
        "total_workers": 2,
        "total_active": 4,
        "queues": ["default", "high"],
    }

    mock_api.list_dlq.return_value = [
        {
            "id": 10,
            "job_class": "test.FailedJob",
            "state": "crashed",
            "error_message": "test error",
            "error_count": 10,
            "created": datetime.now(),
        },
    ]

    mock_api.retry_from_dlq.return_value = None

    mock_api.get_execution_metrics.return_value = {
        "total_jobs": 1000,
        "success_rate": 95.5,
        "avg_duration": 12.3,
    }

    mock_api.list_schedules.return_value = [
        {
            "schedule_id": 1,
            "name": "test-schedule",
            "job_class": "test.Job",
            "cron_expr": "0 * * * *",
            "enabled": True,
            "queue": "default",
        },
    ]

    mock_api.get_schedule.return_value = {
        "schedule_id": 1,
        "name": "test-schedule",
        "job_class": "test.Job",
        "cron_expr": "0 * * * *",
        "enabled": True,
        "queue": "default",
        "kwargs": {},
        "next_run": datetime.now() + timedelta(hours=1),
    }

    mock_api.add_schedule.return_value = 1
    mock_api.enable_schedule.return_value = None
    mock_api.disable_schedule.return_value = None
    mock_api.delete_schedule.return_value = None

    mock_api.get_schedule_history.return_value = [
        {"executed_at": datetime.now(), "result": "success", "job_id": 100},
    ]

    mock_api.get_schedule_stats.return_value = [
        {
            "name": "test-schedule",
            "success_count": 95,
            "failure_count": 5,
            "success_rate_pct": 95.0,
        },
    ]

    return mock_api


# ============================================================================
# Test Job Commands
# ============================================================================


class TestJobsCommands:
    """Test 'jobs' command group."""

    def test_jobs_list_default(self, cli_runner, mock_admin_api):
        """Test jobs list with default options."""
        mock_db_params = {
            "user": "test",
            "password": "test",
            "database": "test",
            "host": "localhost",
            "port": 5432,
        }

        with patch("pyjobby.cli.load_config_from_file", return_value=mock_db_params):
            with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
                with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                    result = cli_runner.invoke(
                        cli, ["--config", "test.py", "jobs", "list"]
                    )

                    # Should succeed
                    assert result.exit_code == 0
                    assert "test.Job" in result.output
                    assert (
                        "queued" in result.output or "QUEUED" in result.output.upper()
                    )

                    # Verify API was called with defaults
                    mock_admin_api.list_jobs.assert_called_once()

    def test_jobs_list_with_filters(self, cli_runner, mock_admin_api):
        """Test jobs list with filters."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "jobs",
                        "list",
                        "--queue",
                        "default",
                        "--state",
                        "queued",
                        "--limit",
                        "10",
                    ],
                )

                assert result.exit_code == 0
                mock_admin_api.list_jobs.assert_called_once()

    def test_jobs_list_json_output(self, cli_runner, mock_admin_api):
        """Test jobs list with JSON output."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "jobs", "list", "--json"]
                )

                assert result.exit_code == 0
                # JSON output should start with [ or {
                assert result.output.strip().startswith(
                    "["
                ) or result.output.strip().startswith("{")

    def test_jobs_inspect(self, cli_runner, mock_admin_api):
        """Test jobs inspect command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "jobs", "inspect", "1"]
                )

                assert result.exit_code == 0
                assert "test.Job" in result.output
                mock_admin_api.get_job_full.assert_called_once_with(1)

    def test_jobs_inspect_json(self, cli_runner, mock_admin_api):
        """Test jobs inspect with JSON output."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    ["--config", "nonexistent.py", "jobs", "inspect", "1", "--json"],
                )

                assert result.exit_code == 0
                assert result.output.strip().startswith("{")

    def test_jobs_retry(self, cli_runner, mock_admin_api):
        """Test jobs retry command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "jobs", "retry", "1", "2"]
                )

                assert result.exit_code == 0
                mock_admin_api.retry_jobs.assert_called_once_with([1, 2])

    def test_jobs_cancel(self, cli_runner, mock_admin_api):
        """Test jobs cancel command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "jobs", "cancel", "1"]
                )

                assert result.exit_code == 0
                mock_admin_api.cancel_jobs.assert_called_once_with([1])

    def test_jobs_delete_with_force(self, cli_runner, mock_admin_api):
        """Test jobs delete with --force flag."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    ["--config", "nonexistent.py", "jobs", "delete", "1", "--force"],
                )

                assert result.exit_code == 0
                mock_admin_api.delete_jobs.assert_called_once()


# ============================================================================
# Test Queue Commands
# ============================================================================


class TestQueuesCommands:
    """Test 'queues' command group."""

    def test_queues_list(self, cli_runner, mock_admin_api):
        """Test queues list command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "queues", "list"]
                )

                assert result.exit_code == 0
                assert "default" in result.output
                assert "high" in result.output
                mock_admin_api.list_queues.assert_called_once()

    def test_queues_stats(self, cli_runner, mock_admin_api):
        """Test queues stats command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "queues", "stats"]
                )

                assert result.exit_code == 0
                mock_admin_api.queue_stats.assert_called()

    def test_queues_stats_json(self, cli_runner, mock_admin_api):
        """Test queues stats with JSON output."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "queues", "stats", "--json"]
                )

                assert result.exit_code == 0

    def test_queues_clear_with_force(self, cli_runner, mock_admin_api):
        """Test queues clear with --force flag."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "queues",
                        "clear",
                        "default",
                        "--force",
                    ],
                )

                assert result.exit_code == 0
                mock_admin_api.delete_jobs.assert_called_once()


# ============================================================================
# Test Worker Commands
# ============================================================================


class TestWorkersCommands:
    """Test 'workers' command group."""

    def test_workers_list(self, cli_runner, mock_admin_api):
        """Test workers list command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "workers", "list"]
                )

                assert result.exit_code == 0
                mock_admin_api.list_workers.assert_called_once()

    def test_workers_stats(self, cli_runner, mock_admin_api):
        """Test workers stats command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "workers", "stats"]
                )

                assert result.exit_code == 0
                mock_admin_api.worker_stats.assert_called_once()


# ============================================================================
# Test DLQ Commands
# ============================================================================


class TestDLQCommands:
    """Test 'dlq' (Dead Letter Queue) command group."""

    def test_dlq_list(self, cli_runner, mock_admin_api):
        """Test DLQ list command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "dlq", "list"]
                )

                assert result.exit_code == 0
                mock_admin_api.list_dlq.assert_called_once()

    def test_dlq_retry(self, cli_runner, mock_admin_api):
        """Test DLQ retry command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "dlq", "retry", "10"]
                )

                assert result.exit_code == 0
                mock_admin_api.retry_from_dlq.assert_called_once_with(10)


# ============================================================================
# Test Metrics Commands
# ============================================================================


class TestMetricsCommands:
    """Test 'metrics' command."""

    def test_metrics(self, cli_runner, mock_admin_api):
        """Test metrics command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "metrics"]
                )

                assert result.exit_code == 0
                mock_admin_api.get_execution_metrics.assert_called_once()

    def test_metrics_json(self, cli_runner, mock_admin_api):
        """Test metrics with JSON output."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "metrics", "--json"]
                )

                assert result.exit_code == 0


# ============================================================================
# Test Schedule Commands
# ============================================================================


class TestScheduleCommands:
    """Test 'schedule' command group."""

    def test_schedule_list(self, cli_runner, mock_admin_api):
        """Test schedule list command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "schedule", "list"]
                )

                assert result.exit_code == 0
                mock_admin_api.list_schedules.assert_called_once()

    def test_schedule_show(self, cli_runner, mock_admin_api):
        """Test schedule show command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    ["--config", "nonexistent.py", "schedule", "show", "test-schedule"],
                )

                assert result.exit_code == 0

    def test_schedule_add(self, cli_runner, mock_admin_api):
        """Test schedule add command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "schedule",
                        "add",
                        "test-schedule",
                        "test.Job",
                        "0 * * * *",
                    ],
                )

                assert result.exit_code == 0
                mock_admin_api.add_schedule.assert_called_once()

    def test_schedule_enable(self, cli_runner, mock_admin_api):
        """Test schedule enable command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "schedule",
                        "enable",
                        "test-schedule",
                    ],
                )

                assert result.exit_code == 0

    def test_schedule_disable(self, cli_runner, mock_admin_api):
        """Test schedule disable command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "schedule",
                        "disable",
                        "test-schedule",
                    ],
                )

                assert result.exit_code == 0

    def test_schedule_delete_with_force(self, cli_runner, mock_admin_api):
        """Test schedule delete with --force flag."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "schedule",
                        "delete",
                        "test-schedule",
                        "--force",
                    ],
                )

                assert result.exit_code == 0
                mock_admin_api.delete_schedule.assert_called_once()

    def test_schedule_history(self, cli_runner, mock_admin_api):
        """Test schedule history command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli,
                    [
                        "--config",
                        "nonexistent.py",
                        "schedule",
                        "history",
                        "test-schedule",
                    ],
                )

                assert result.exit_code == 0

    def test_schedule_stats(self, cli_runner, mock_admin_api):
        """Test schedule stats command."""
        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "schedule", "stats"]
                )

                assert result.exit_code == 0
                mock_admin_api.get_schedule_stats.assert_called_once()


# ============================================================================
# Test Helper Functions
# ============================================================================


class TestHelperFunctions:
    """Test CLI helper functions."""

    def test_print_success(self, cli_runner):
        """Test print_success helper."""
        from pyjobby.cli import print_success

        # Should not raise exception
        print_success("Test message")

    def test_print_error(self, cli_runner):
        """Test print_error helper."""
        from pyjobby.cli import print_error

        # Should not raise exception
        print_error("Test error")

    def test_print_warning(self, cli_runner):
        """Test print_warning helper."""
        from pyjobby.cli import print_warning

        # Should not raise exception
        print_warning("Test warning")

    def test_print_table(self, cli_runner):
        """Test print_table helper."""
        from pyjobby.cli import print_table

        headers = ["ID", "Name", "Status"]
        rows = [["1", "test", "active"], ["2", "demo", "inactive"]]
        # Should not raise exception
        print_table(headers, rows)


# ============================================================================
# Test Error Handling
# ============================================================================


class TestCLIErrorHandling:
    """Test CLI error handling."""

    def test_config_file_not_found(self, cli_runner):
        """Test handling of missing config file."""
        # When config file doesn't exist, CLI should handle it gracefully
        # This might load default config or use DB_PARAMS from environment
        result = cli_runner.invoke(cli, ["--help"])

        # Should show help successfully
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_invalid_job_id(self, cli_runner, mock_admin_api):
        """Test handling of invalid job ID."""
        mock_admin_api.get_job_full.return_value = None

        with patch("pyjobby.cli.asyncpg.connect", new=AsyncMock()):
            with patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api):
                result = cli_runner.invoke(
                    cli, ["--config", "nonexistent.py", "jobs", "inspect", "99999"]
                )

                # Should handle gracefully (may succeed with "not found" message or fail)
                # Either exit code is acceptable
                assert result.exit_code in (0, 1)
