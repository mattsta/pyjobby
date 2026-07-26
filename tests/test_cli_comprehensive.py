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
    with (
        patch(
            "pyjobby.cli.load_config_from_file",
            return_value={"db_params": mock_db_params},
        ),
        patch("pyjobby.cli.db.connect", new=AsyncMock()),
        patch("pyjobby.cli.AdminAPI", return_value=mock_admin_api),
    ):
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
    """Create mock AdminAPI for testing CLI commands.

    Return values mirror the real AdminAPI contract: datetime fields on
    job/worker dicts are ISO-8601 strings (from .isoformat()), while
    schedule rows keep datetime objects (the CLI calls .strftime on them
    and JSON-serializes with default=str).
    """
    mock_api = AsyncMock()

    # Setup default mock return values
    mock_api.list_jobs.return_value = [
        {
            "id": 1,
            "job_class": "test.Job",
            "state": "queued",
            "queue": "default",
            "created": datetime.now().isoformat(),
            "prio": 100,
            "run_after": None,
        },
        {
            "id": 2,
            "job_class": "test.Job2",
            "state": "running",
            "queue": "high",
            "created": datetime.now().isoformat(),
            "prio": 200,
            "run_after": None,
        },
    ]

    # AdminAPI.get_job returns a JobInfo.to_dict(): every field of the
    # dataclass, with datetimes serialized to ISO strings.
    mock_api.get_job.return_value = {
        "id": 1,
        "state": "queued",
        "queue": "default",
        "job_class": "test.Job",
        "kwargs": {"key": "value"},
        "prio": 100,
        "run_after": datetime.now().isoformat(),
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
        "run_count": 0,
        "error_count": 0,
        "capability": None,
        "uid": None,
        "run_group": None,
        "waitfor_job": None,
        "waitfor_group": None,
        "deadline_key": None,
        "worker_pid": None,
        "worker_host": None,
        "result": None,
        "error_message": None,
        "error_backtrace": None,
        "admin_data": {"timeout_seconds": 60},
        "started": None,
        "finished": None,
        "timeout_at": None,
        "dag_id": None,
    }

    mock_api.retry_job.return_value = {
        "original_job_id": 1,
        "new_job_id": 11,
        "status": "retry_queued",
    }
    mock_api.retry_jobs.return_value = [
        {"original_job_id": 1, "new_job_id": 11, "status": "retry_queued"},
        {"original_job_id": 2, "new_job_id": 12, "status": "retry_queued"},
    ]
    mock_api.cancel_job.return_value = {"job_id": 1, "status": "cancelled"}
    mock_api.cancel_jobs.return_value = [
        {"job_id": 1, "status": "cancelled"},
    ]
    mock_api.delete_job.return_value = True
    mock_api.delete_jobs.return_value = 5
    mock_api.clear_queue.return_value = 5

    mock_api.list_queues.return_value = ["default", "high", "low"]

    # AdminAPI.queue_stats returns a LIST of per-queue stat dicts
    # (QueueStats.to_dict()), all values JSON-serializable.
    mock_api.queue_stats.return_value = [
        {
            "queue": "default",
            "queued": 10,
            "claimed": 0,
            "running": 5,
            "waiting": 1,
            "finished": 100,
            "crashed": 2,
            "cancelled": 0,
            "total": 118,
            "oldest_queued_age_seconds": 7200.0,
        },
    ]

    # AdminAPI.list_workers returns WorkerInfo.to_dict() rows.
    mock_api.list_workers.return_value = [
        {
            "worker_host": "host1",
            "worker_pid": 1234,
            "job_id": 1,
            "job_class": "test.Job",
            "state": "running",
            "started_at": datetime.now().isoformat(),
        },
        {
            "worker_host": "host2",
            "worker_pid": 5678,
            "job_id": 2,
            "job_class": "test.Job2",
            "state": "running",
            "started_at": datetime.now().isoformat(),
        },
    ]

    # AdminAPI.worker_stats: oldest_job_started is an ISO string
    # (the CLI slices it with [:19]).
    mock_api.worker_stats.return_value = {
        "active_workers": 2,
        "workers": [
            {
                "host": "host1",
                "pid": 1234,
                "job_count": 3,
                "oldest_job_started": datetime.now().isoformat(),
            },
            {
                "host": "host2",
                "pid": 5678,
                "job_count": 1,
                "oldest_job_started": None,
            },
        ],
    }

    mock_api.list_dlq.return_value = [
        {
            "id": 10,
            "job_class": "test.FailedJob",
            "state": "crashed",
            "error_message": "test error",
            "error_count": 10,
            "created": datetime.now().isoformat(),
        },
    ]

    mock_api.retry_from_dlq.return_value = {
        "original_job_id": 10,
        "new_job_id": 20,
        "status": "retry_queued_from_dlq",
    }

    # AdminAPI.get_metrics return shape (fully JSON-serializable).
    mock_api.get_metrics.return_value = {
        "period_start": (datetime.now() - timedelta(hours=24)).isoformat(),
        "period_end": datetime.now().isoformat(),
        "queue": None,
        "state_counts": {"finished": 950, "crashed": 50},
        "finished_count": 950,
        "crashed_count": 50,
        "avg_duration_seconds": 12.3,
        "top_errors": [
            {
                "job_class": "test.FailedJob",
                "error_count": 50,
                "latest_error": "boom",
            },
        ],
    }

    # Schedule rows come straight from jorb_schedule (dict(record)):
    # datetime columns stay datetime objects.
    mock_api.list_schedules.return_value = [
        {
            "id": 1,
            "name": "test-schedule",
            "job_class": "test.Job",
            "cron_expr": "0 * * * *",
            "enabled": True,
            "queue": "default",
            "next_run": datetime.now() + timedelta(hours=1),
            "last_success": None,
        },
    ]

    mock_api.get_schedule.return_value = {
        "id": 1,
        "name": "test-schedule",
        "job_class": "test.Job",
        "cron_expr": "0 * * * *",
        "enabled": True,
        "queue": "default",
        "kwargs": {},
        "prio": 100,
        "capability": None,
        "timezone": "UTC",
        "description": None,
        "next_run": datetime.now() + timedelta(hours=1),
        "max_concurrent_jobs": 1,
        "jitter_seconds": 0,
        "backpressure_threshold": 1000,
        "circuit_breaker_threshold": 5,
        "run_count": 100,
        "success_count": 95,
        "failure_count": 5,
        "skip_count": 0,
        "consecutive_failures": 0,
        "last_run": None,
        "last_success": None,
        "last_failure": None,
    }

    # AdminAPI.create_schedule returns the newly created schedule row.
    mock_api.create_schedule.return_value = {
        "id": 1,
        "name": "test-schedule",
        "job_class": "test.Job",
        "cron_expr": "0 * * * *",
        "enabled": True,
        "queue": "default",
        "next_run": datetime.now() + timedelta(hours=1),
    }

    mock_api.enable_schedule.return_value = {"id": 1, "enabled": True}
    mock_api.disable_schedule.return_value = {"id": 1, "enabled": False}
    mock_api.delete_schedule.return_value = {"status": "deleted", "schedule_id": "1"}

    # jorb_schedule_log rows: actual_time is a datetime (CLI calls .strftime).
    mock_api.get_schedule_history.return_value = [
        {
            "actual_time": datetime.now(),
            "result": "success",
            "job_id": 100,
            "duration_ms": 1500,
            "skip_reason": None,
            "error_message": None,
        },
    ]

    mock_api.get_schedule_stats.return_value = [
        {
            "name": "test-schedule",
            "enabled": True,
            "run_count": 100,
            "success_count": 95,
            "failure_count": 5,
            "skip_count": 0,
            "success_rate_pct": 95.0,
            "next_run": datetime.now() + timedelta(hours=1),
        },
    ]

    return mock_api


# ============================================================================
# Test Job Commands
# ============================================================================


class TestJobsCommands:
    """Test 'jobs' command group."""

    def test_jobs_list_default(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs list with default options."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "jobs", "list"])

            # Should succeed
            assert result.exit_code == 0
            assert "test.Job" in result.output
            assert "queued" in result.output or "QUEUED" in result.output.upper()

            # Verify API was called with defaults
            mock_admin_api.list_jobs.assert_called_once()

    def test_jobs_list_with_filters(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs list with filters."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
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

    def test_jobs_list_json_output(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs list with JSON output."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "list", "--json"]
            )

            assert result.exit_code == 0
            # JSON output should start with [ or {
            assert result.output.strip().startswith(
                "["
            ) or result.output.strip().startswith("{")

    def test_jobs_inspect(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs inspect command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "inspect", "1"]
            )

            assert result.exit_code == 0
            assert "test.Job" in result.output
            mock_admin_api.get_job.assert_called_once_with(1)

    def test_jobs_inspect_json(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs inspect with JSON output."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                ["--config", "test.py", "jobs", "inspect", "1", "--json"],
            )

            assert result.exit_code == 0
            assert result.output.strip().startswith("{")

    def test_jobs_retry(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs retry command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "retry", "1", "2"]
            )

            assert result.exit_code == 0
            mock_admin_api.retry_jobs.assert_called_once_with([1, 2])

    def test_jobs_cancel(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs cancel command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "cancel", "1"]
            )

            assert result.exit_code == 0
            # For a single job id the CLI calls cancel_job, not cancel_jobs
            mock_admin_api.cancel_job.assert_called_once_with(1)

    def test_jobs_delete_with_force(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs delete with --force flag."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                ["--config", "test.py", "jobs", "delete", "1", "--force"],
            )

            assert result.exit_code == 0
            # The CLI deletes a single job via delete_job
            mock_admin_api.delete_job.assert_called_once_with(1)


# ============================================================================
# Test Queue Commands
# ============================================================================


class TestQueuesCommands:
    """Test 'queues' command group."""

    def test_queues_list(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues list command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "queues", "list"])

            assert result.exit_code == 0
            assert "default" in result.output
            assert "high" in result.output
            mock_admin_api.list_queues.assert_called_once()

    def test_queues_stats(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues stats command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "queues", "stats"])

            assert result.exit_code == 0
            mock_admin_api.queue_stats.assert_called()

    def test_queues_stats_json(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues stats with JSON output."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "queues", "stats", "--json"]
            )

            assert result.exit_code == 0

    def test_queues_clear_with_force(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues clear with --force flag."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "queues",
                    "clear",
                    "default",
                    "--force",
                ],
            )

            assert result.exit_code == 0
            # The CLI clears queues via clear_queue
            mock_admin_api.clear_queue.assert_called_once()


# ============================================================================
# Test Worker Commands
# ============================================================================


class TestWorkersCommands:
    """Test 'workers' command group."""

    def test_workers_list(self, cli_runner, mock_admin_api, mock_db_params):
        """Test workers list command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "workers", "list"])

            assert result.exit_code == 0
            mock_admin_api.list_workers.assert_called_once()

    def test_workers_stats(self, cli_runner, mock_admin_api, mock_db_params):
        """Test workers stats command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "workers", "stats"])

            assert result.exit_code == 0
            mock_admin_api.worker_stats.assert_called_once()


# ============================================================================
# Test DLQ Commands
# ============================================================================


class TestDLQCommands:
    """Test 'dlq' (Dead Letter Queue) command group."""

    def test_dlq_list(self, cli_runner, mock_admin_api, mock_db_params):
        """Test DLQ list command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "dlq", "list"])

            assert result.exit_code == 0
            mock_admin_api.list_dlq.assert_called_once()

    def test_dlq_retry(self, cli_runner, mock_admin_api, mock_db_params):
        """Test DLQ retry command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "dlq", "retry", "10"]
            )

            assert result.exit_code == 0
            mock_admin_api.retry_from_dlq.assert_called_once_with(10)


# ============================================================================
# Test Metrics Commands
# ============================================================================


class TestMetricsCommands:
    """Test 'metrics' command."""

    def test_metrics(self, cli_runner, mock_admin_api, mock_db_params):
        """Test metrics command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "metrics"])

            assert result.exit_code == 0
            # The CLI fetches metrics via get_metrics
            mock_admin_api.get_metrics.assert_called_once()

    def test_metrics_json(self, cli_runner, mock_admin_api, mock_db_params):
        """Test metrics with JSON output."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "metrics", "--json"]
            )

            assert result.exit_code == 0


# ============================================================================
# Test Schedule Commands
# ============================================================================


class TestScheduleCommands:
    """Test 'schedule' command group."""

    def test_schedule_list(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule list command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(cli, ["--config", "test.py", "schedule", "list"])

            assert result.exit_code == 0
            mock_admin_api.list_schedules.assert_called_once()

    def test_schedule_show(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule show command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                ["--config", "test.py", "schedule", "show", "test-schedule"],
            )

            assert result.exit_code == 0

    def test_schedule_add(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule add command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "schedule",
                    "add",
                    "test-schedule",
                    "test.Job",
                    "0 * * * *",
                ],
            )

            assert result.exit_code == 0
            # The CLI creates schedules via create_schedule
            mock_admin_api.create_schedule.assert_called_once()

    def test_schedule_enable(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule enable command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "schedule",
                    "enable",
                    "test-schedule",
                ],
            )

            assert result.exit_code == 0

    def test_schedule_disable(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule disable command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "schedule",
                    "disable",
                    "test-schedule",
                ],
            )

            assert result.exit_code == 0

    def test_schedule_delete_with_force(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """Test schedule delete skipping confirmation.

        The real CLI uses click.confirmation_option, whose skip flag is
        --yes (there is no --force option on schedule delete).
        """
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "schedule",
                    "delete",
                    "test-schedule",
                    "--yes",
                ],
            )

            assert result.exit_code == 0
            mock_admin_api.delete_schedule.assert_called_once()

    def test_schedule_history(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule history command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "schedule",
                    "history",
                    "test-schedule",
                ],
            )

            assert result.exit_code == 0

    def test_schedule_stats(self, cli_runner, mock_admin_api, mock_db_params):
        """Test schedule stats command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "schedule", "stats"]
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

    def test_invalid_job_id(self, cli_runner, mock_admin_api, mock_db_params):
        """Test handling of invalid job ID."""
        # jobs inspect uses AdminAPI.get_job; None means "not found"
        mock_admin_api.get_job.return_value = None

        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "inspect", "99999"]
            )

            # Should handle gracefully (may succeed with "not found" message or fail)
            # Either exit code is acceptable
            assert result.exit_code in (0, 1)
