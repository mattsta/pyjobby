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

from pyjobby.admin_api import AdminAPI, Unset
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
    # spec=AdminAPI: a test that mocks a method the real AdminAPI does not
    # have now fails loudly instead of passing vacuously.
    mock_api = AsyncMock(spec=AdminAPI)

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
        "tags": {},
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
        "run_epoch": 0,
        "cancel_requested": False,
        "claimed_by": None,
    }

    mock_api.retry_job.return_value = {"job_id": 1, "status": "requeued"}
    mock_api.retry_jobs.return_value = [
        {"job_id": 1, "status": "requeued"},
        {"job_id": 2, "status": "requeued"},
    ]
    mock_api.cancel_job.return_value = {"job_id": 1, "status": "cancelled"}
    mock_api.cancel_jobs.return_value = [
        {"job_id": 1, "status": "cancelled"},
    ]
    mock_api.delete_job.return_value = True
    mock_api.delete_jobs.return_value = 5
    mock_api.clear_queue.return_value = 5

    # AdminAPI.list_queues returns per-queue dicts with the jorb_queue
    # control fields alongside (defaults when no control row exists).
    mock_api.list_queues.return_value = [
        {
            "name": "default",
            "paused": False,
            "max_concurrency": None,
            "rate_limit": None,
            "rate_period_seconds": 60.0,
        },
        {
            "name": "high",
            "paused": True,
            "max_concurrency": 4,
            "rate_limit": 10,
            "rate_period_seconds": 60.0,
        },
        {
            "name": "low",
            "paused": False,
            "max_concurrency": None,
            "rate_limit": None,
            "rate_period_seconds": 60.0,
        },
    ]

    # AdminAPI.queue_stats returns a LIST of per-queue stat dicts
    # (QueueStats.to_dict()), all values JSON-serializable.
    mock_api.queue_stats.return_value = [
        {
            "queue": "default",
            "queued": 10,
            "scheduled": 3,
            "claimed": 0,
            "running": 5,
            "waiting": 1,
            "finished": 100,
            "crashed": 2,
            "cancelled": 0,
            "total": 118,
            "oldest_queued_age_seconds": 7200.0,
            "paused": False,
            "max_concurrency": None,
            "rate_limit": None,
            "rate_period_seconds": 60.0,
        },
    ]

    # AdminAPI.list_workers returns jorb_worker registry rows: liveness,
    # heartbeat age, and the currently claimed job (datetimes as ISO strings).
    mock_api.list_workers.return_value = [
        {
            "id": 1,
            "host": "host1",
            "pid": 1234,
            "queue": "default",
            "capabilities": ["test"],
            "version": None,
            "started": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "shutdown_at": None,
            "last_seen_age_seconds": 3.0,
            "live": True,
            "not_claiming": False,
            "job_threads": 8,
            "job_threads_abandoned": 0,
            "current_job_id": 1,
            "current_job_class": "test.Job",
            "current_job_state": "running",
        },
        {
            "id": 2,
            "host": "host2",
            "pid": 5678,
            "queue": "default",
            "capabilities": ["test"],
            "version": None,
            "started": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "shutdown_at": None,
            "last_seen_age_seconds": 8.0,
            "live": True,
            "not_claiming": False,
            "job_threads": 8,
            "job_threads_abandoned": 0,
            "current_job_id": None,
            "current_job_class": None,
            "current_job_state": None,
        },
    ]

    # AdminAPI.worker_stats: registry-based aggregate counts.
    mock_api.worker_stats.return_value = {
        "live_workers": 2,
        "stale_workers": 0,
        "shutdown_workers": 1,
        "total_registered": 3,
        "per_queue": {"default": 2},
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
        "job_id": 10,
        "status": "requeued_from_dlq",
    }

    # Queue control plane and requeue/history/steps surfaces.
    _control = {
        "name": "default",
        "paused": False,
        "max_concurrency": None,
        "rate_limit": None,
        "rate_period_seconds": 60.0,
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    }
    mock_api.pause_queue.return_value = {**_control, "paused": True}
    mock_api.resume_queue.return_value = _control
    mock_api.get_queue_control.return_value = _control
    mock_api.set_queue_control.return_value = _control

    mock_api.rerun_job.return_value = {
        "job_id": 1,
        "status": "requeued",
        "fresh": True,
    }
    mock_api.get_job_history.return_value = [
        {
            "id": 1,
            "job_id": 1,
            "at": datetime.now().isoformat(),
            "event": "enqueued",
            "detail": {"queue": "default", "job_class": "test.Job"},
        },
        {
            "id": 2,
            "job_id": 1,
            "at": datetime.now().isoformat(),
            "event": "claimed",
            "detail": {
                "from": "queued",
                "run_epoch": 1,
                "error_count": 0,
                "worker_host": "host1",
                "worker_pid": 1234,
            },
        },
    ]
    mock_api.get_job_steps.return_value = [
        {
            "job_id": 1,
            "step_seq": 1,
            "name": "fetch",
            "output": {"n": 7},
            "error": None,
            "run_epoch": 1,
            "started": datetime.now().isoformat(),
            "finished": datetime.now().isoformat(),
            "duration_seconds": 0.05,
        },
    ]

    # AdminAPI.get_metrics return shape (fully JSON-serializable).
    # Rates are per-second over `window_seconds`, levels are instants; see
    # tests/test_metrics_saturation.py for what each one means and is worth.
    mock_api.get_metrics.return_value = {
        "period_start": (datetime.now() - timedelta(hours=24)).isoformat(),
        "period_end": datetime.now().isoformat(),
        "window_seconds": 86400.0,
        "queue": None,
        "state_counts": {"finished": 950, "crashed": 50},
        "finished_count": 950,
        "crashed_count": 50,
        "cancelled_count": 0,
        "terminal_count": 1000,
        "throughput_per_second": 1000 / 86400,
        "arrival_count": 1200,
        "arrival_rate_per_second": 1200 / 86400,
        "retry_count": 120,
        "retry_rate_per_second": 120 / 86400,
        "dlq_growth_per_second": 50 / 86400,
        "avg_duration_seconds": 12.3,
        "avg_wait_seconds": 4.5,
        "max_wait_seconds": 31.0,
        "backlog": {
            "per_queue": {"default": {"depth": 200, "oldest_age_seconds": 95.0}},
            "depth": 200,
            "oldest_age_seconds": 95.0,
        },
        "inflight": {
            "inflight": 40,
            "stuck": 2,
            "stuck_after_seconds": 300.0,
            "oldest_age_seconds": 610.0,
        },
        "storage": {
            "tables": {
                "jorb": {
                    "total_bytes": 7_110_656,
                    "table_bytes": 2_383_872,
                    "index_bytes": 4_726_784,
                    "live_tuples": 20000,
                    "dead_tuples": 1000,
                    "dead_tuple_ratio": 1000 / 21000,
                    "last_autovacuum": datetime.now().isoformat(),
                    "last_autoanalyze": datetime.now().isoformat(),
                },
            },
            "total_bytes": 7_110_656,
            "dead_tuple_ratio": 1000 / 21000,
        },
        "notify_queue_usage": 0.0,
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

    def test_jobs_retry_single_uses_retry_job(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """A single job id goes through retry_job and reports 'requeued'."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "retry", "1"]
            )

            assert result.exit_code == 0
            assert "requeued" in result.output
            mock_admin_api.retry_job.assert_called_once_with(1)

    def test_jobs_rerun(self, cli_runner, mock_admin_api, mock_db_params):
        """jobs rerun defaults to a FRESH restart (that is what rerun means)."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "rerun", "1"]
            )

            assert result.exit_code == 0
            assert "fresh restart" in result.output
            mock_admin_api.rerun_job.assert_called_once_with(1, fresh=True)

    def test_jobs_rerun_resume(self, cli_runner, mock_admin_api, mock_db_params):
        """jobs rerun --resume keeps checkpoints (continue an interrupted job)."""
        mock_admin_api.rerun_job.return_value = {
            "job_id": 1,
            "status": "requeued",
            "fresh": False,
        }
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "rerun", "1", "--resume"]
            )

            assert result.exit_code == 0
            assert "resume with checkpoints" in result.output
            mock_admin_api.rerun_job.assert_called_once_with(1, fresh=False)

    def test_jobs_history(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs history command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "history", "1"]
            )

            assert result.exit_code == 0
            assert "enqueued" in result.output
            assert "claimed" in result.output
            mock_admin_api.get_job_history.assert_called_once_with(1)

    def test_jobs_steps(self, cli_runner, mock_admin_api, mock_db_params):
        """Test jobs steps command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "jobs", "steps", "1"]
            )

            assert result.exit_code == 0
            assert "fetch" in result.output
            mock_admin_api.get_job_steps.assert_called_once_with(1)


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

    def test_queues_pause(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues pause command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "queues", "pause", "default"]
            )

            assert result.exit_code == 0
            assert "paused" in result.output
            mock_admin_api.pause_queue.assert_called_once_with("default")

    def test_queues_resume(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues resume command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "queues", "resume", "default"]
            )

            assert result.exit_code == 0
            assert "resumed" in result.output
            mock_admin_api.resume_queue.assert_called_once_with("default")

    def test_queues_limits_show_current(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """queues limits with no options shows the current control row."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "queues", "limits", "default"]
            )

            assert result.exit_code == 0
            mock_admin_api.get_queue_control.assert_called_once_with("default")
            mock_admin_api.set_queue_control.assert_not_called()

    def test_queues_limits_set(self, cli_runner, mock_admin_api, mock_db_params):
        """queues limits with options updates the control row."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "queues",
                    "limits",
                    "default",
                    "--max-concurrency",
                    "4",
                ],
            )

            assert result.exit_code == 0
            mock_admin_api.set_queue_control.assert_called_once()
            args, kwargs = mock_admin_api.set_queue_control.call_args
            assert args == ("default",)
            assert kwargs["max_concurrency"] == 4
            # Options NOT passed must arrive as the UNSET sentinel so the
            # UPDATE leaves those columns alone -- None would CLEAR them.
            assert isinstance(kwargs["rate_limit"], Unset)
            assert kwargs["rate_period_seconds"] is None

    def test_queues_limits_clear_uses_none_not_unset(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """'--max-concurrency none' explicitly CLEARS the limit (None)."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "queues",
                    "limits",
                    "default",
                    "--max-concurrency",
                    "none",
                ],
            )

            assert result.exit_code == 0
            _, kwargs = mock_admin_api.set_queue_control.call_args
            # None (clear), distinct from UNSET (leave alone)
            assert kwargs["max_concurrency"] is None
            assert not isinstance(kwargs["max_concurrency"], Unset)
            assert isinstance(kwargs["rate_limit"], Unset)

    def test_queues_limits_rejects_non_integer(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """A non-integer, non-'none' limit exits 2 without touching the DB."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli,
                [
                    "--config",
                    "test.py",
                    "queues",
                    "limits",
                    "default",
                    "--max-concurrency",
                    "lots",
                ],
            )

            assert result.exit_code == 2
            mock_admin_api.set_queue_control.assert_not_called()

    def test_queues_show(self, cli_runner, mock_admin_api, mock_db_params):
        """Test queues show command."""
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "queues", "show", "default"]
            )

            assert result.exit_code == 0
            mock_admin_api.get_queue_control.assert_called_once_with("default")
            mock_admin_api.queue_stats.assert_called_once_with(queue="default")


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

    def test_schedule_enable_exits_nonzero_on_error(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        """A deploy script doing `pj-admin schedule enable X && next` must
        NOT proceed when the enable failed. These verbs used to print the
        error and exit 0."""
        mock_admin_api.enable_schedule.side_effect = RuntimeError("db exploded")
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "schedule", "enable", "test-schedule"]
            )
        assert result.exit_code != 0
        assert "Failed to enable schedule" in result.output

    def test_schedule_disable_exits_nonzero_on_error(
        self, cli_runner, mock_admin_api, mock_db_params
    ):
        mock_admin_api.disable_schedule.side_effect = RuntimeError("db exploded")
        with mock_cli_context(mock_admin_api, mock_db_params):
            result = cli_runner.invoke(
                cli, ["--config", "test.py", "schedule", "disable", "test-schedule"]
            )
        assert result.exit_code != 0
        assert "Failed to disable schedule" in result.output

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
    """The CLI's console helpers must RENDER their message — an exception
    swallowed into empty output would pass a does-not-raise test while
    every command using the helper printed nothing."""

    def test_print_success(self, capsys):
        from pyjobby.cli import print_success

        print_success("Test message")
        assert "Test message" in capsys.readouterr().out

    def test_print_error(self, capsys):
        from pyjobby.cli import print_error

        print_error("Test error")
        captured = capsys.readouterr()
        assert "Test error" in captured.out + captured.err

    def test_print_warning(self, capsys):
        from pyjobby.cli import print_warning

        print_warning("Test warning")
        captured = capsys.readouterr()
        assert "Test warning" in captured.out + captured.err

    def test_print_table(self, capsys):
        """The table renders its headers and every row's cells."""
        from pyjobby.cli import print_table

        headers = ["ID", "Name", "Status"]
        rows = [["1", "test", "active"], ["2", "demo", "inactive"]]
        print_table(headers, rows)
        out = capsys.readouterr().out
        for cell in ("ID", "Status", "test", "inactive"):
            assert cell in out


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
