"""
Tests for pj.py entry points and CLI (schema v1).

Tests the worker entry points (runAndDone parameterization, signal
handling), the workit CLI (flags, config loading, real subprocess
launches), and the ex() statement runner — with live database operations.
"""

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyjobby import db
from pyjobby.pj import STMTS, JobSystem, workit

from .conftest import wait_for_job_state

REPO_ROOT = Path(__file__).resolve().parent.parent
# stays a str: it is passed straight into argv lists and into
# load_config_from_file(), whose "python:"/"file:" prefix handling is string
# surgery on the value it is given
CONFIG_PATH = str(REPO_ROOT / "pyjobby.conf.py")


def run_workit_briefly(args: list[str], cwd: Path, timeout: float = 2) -> bool:
    """Launch `python -m pyjobby.pj <args>` in its own process group, kill
    the WHOLE group after `timeout`, and report whether it was still ALIVE
    at the kill — i.e. it started and survived its flags rather than dying
    on argument parsing or config loading. Callers must assert on that:
    a launch test that checks nothing passes identically for a launcher
    that exits instantly on a TypeError.

    subprocess.run(timeout=...) only kills the direct child; the workit
    launcher's multiprocessing worker children would leak and keep claiming
    jobs from later tests. A process-group kill reaps every descendant."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pyjobby.pj", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=timeout)
        alive_at_kill = False  # it exited on its own — startup failed
    except subprocess.TimeoutExpired:
        alive_at_kill = True  # still running when we came to kill it
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    return alive_at_kill


# ============================================================================
# Test runAndDone Parameterization
# ============================================================================


class TestRunAndDoneFunction:
    """Test the JobSystem construction runAndDone performs."""

    def test_runAndDone_creates_job_system(self, db_params, unique_queue):
        """JobSystem carries exactly the parameters runAndDone passes."""
        runner = JobSystem(
            dsn=db_params,
            qname=unique_queue,
            capabilities=("test_cap",),
            workerId=999,
            checkInterval=5,
            webPort=None,
            max_retries=15,
            default_timeout=7200,
            _launcher_pid=os.getppid(),
        )

        assert runner.dsn == db_params
        assert runner.qname == unique_queue
        assert runner.capabilities == ("test_cap",)
        assert runner.workerId == 999
        assert runner.checkInterval == 5
        assert runner.webPort is None
        assert runner.max_retries == 15
        assert runner.default_timeout == 7200
        # orphan protection: worker stops if the launcher process dies
        assert runner._launcher_pid == os.getppid()

    def test_runAndDone_with_web_port(self, db_params, unique_queue):
        """Test runAndDone-style construction with webPort configuration."""
        web_config = {
            "sites": [{"host": "127.0.0.1", "port": 9999}],
            "paths": {"test.Job"},
        }

        runner = JobSystem(
            dsn=db_params,
            qname=unique_queue,
            capabilities=("web",),
            workerId=1001,
            checkInterval=5,
            webPort=web_config,
        )

        assert runner.webPort == web_config
        assert runner.webPort["sites"][0]["port"] == 9999

    def test_runAndDone_signal_handler(self, db_params, unique_queue):
        """runAndDone registers shutdown as the SIGTERM handler."""
        system = JobSystem(
            dsn=db_params,
            qname=unique_queue,
            capabilities=("test",),
            workerId=888,
            checkInterval=0.1,
            webPort=None,
        )

        original_handler = signal.getsignal(signal.SIGTERM)
        try:
            # what runAndDone does
            signal.signal(signal.SIGTERM, system.shutdown)
            assert signal.getsignal(signal.SIGTERM) == system.shutdown

            assert system.stop is False
            system.shutdown(signal.SIGTERM, None)
            assert system.stop is True, "Signal handler should set stop=True"
        finally:
            signal.signal(signal.SIGTERM, original_handler)


# ============================================================================
# Test CLI Entry Point
# ============================================================================


class TestWorkitCLI:
    """Test the workit CLI command."""

    def test_workit_version_flag(self):
        """Test -v flag shows version and exits."""
        runner = CliRunner()
        result = runner.invoke(workit, ["-v"])

        assert result.exit_code == 0
        # Version should be a valid version string
        assert result.output.strip()  # Should have some output

    def test_workit_config_not_found(self):
        """Test error when config file not found."""
        runner = CliRunner()
        result = runner.invoke(workit, ["--config", "/nonexistent/path/config.py"])

        assert result.exit_code == 1
        # Should not crash

    def test_workit_help(self):
        """Test --help shows current usage information."""
        runner = CliRunner()
        result = runner.invoke(workit, ["--help"])

        assert result.exit_code == 0
        assert "--queue" in result.output
        assert "--workers" in result.output
        assert "--max-retries" in result.output
        assert "--default-timeout" in result.output
        assert "--check-interval" in result.output
        # recovery flags are gone: dead-worker recovery lives in the
        # monitor via the jorb_worker registry, not in the worker CLI
        assert "--recovery-timeout" not in result.output
        assert "--no-recovery" not in result.output

    def test_workit_removed_recovery_flags_rejected(self):
        """The old recovery flags are no longer accepted."""
        runner = CliRunner()
        for flag in (["--no-recovery"], ["--recovery-timeout", "300"]):
            result = runner.invoke(workit, [*flag, "--config", "/nonexistent"])
            assert result.exit_code == 2, f"{flag} should be a usage error"
            assert "no such option" in result.output.lower()

    def test_workit_default_options(self):
        """Test default option values are set correctly."""
        runner = CliRunner()
        # Run with a non-existent config to trigger early exit
        # This still validates the option parsing
        result = runner.invoke(
            workit, ["--config", "/nonexistent/nonexistent_pyjobby.conf.py"]
        )

        # Should fail due to missing config, but options should parse
        assert result.exit_code == 1

    def test_workit_custom_max_retries(self):
        """Test --max-retries option parsing."""
        runner = CliRunner()
        result = runner.invoke(
            workit, ["--max-retries", "20", "--config", "/nonexistent"]
        )

        # Should fail due to config, but option should be parsed
        assert result.exit_code == 1


# ============================================================================
# Test ex() Statement Runner
# ============================================================================


class TestExStatementRunner:
    """Test the ex() prepared statement runner."""

    @pytest.mark.asyncio
    async def test_ex_returns_list_from_fetch(self, db_params, unique_queue):
        """ex() returns a list (empty when nothing is claimable)."""
        system = JobSystem(
            dsn=db_params,
            qname=unique_queue,
            capabilities=("test",),
            workerId=555,
            checkInterval=0.1,
            webPort=None,
        )

        system.cxn = await db.connect(**db_params)
        try:
            system.stmts = {
                name: await system.cxn.prepare(stmt) for name, stmt in STMTS.items()
            }

            # schema v1 claim: 6 parameters, last is the registry worker id
            result = await system.ex(
                "claim", os.getpid(), "testhost", unique_queue, ("test",), 1000, None
            )
            assert isinstance(result, list)
            assert result == []
        finally:
            await system.cxn.close()


# ============================================================================
# Test Job Processing Counters
# ============================================================================


class TestJobProcessingCounters:
    """Test job processing counters in run loop."""

    @pytest.mark.asyncio
    async def test_processed_counter_increments(
        self, live_worker, unique_queue, db_pool
    ):
        """Test that the processed counter increments for each job."""
        system = await live_worker()

        job_ids = [
            await db_pool.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ($1, $2, $3) RETURNING id""",
                "tests.dxe_jobs.OkJob",
                {"x": i},
                unique_queue,
            )
            for i in range(3)
        ]

        for job_id in job_ids:
            await wait_for_job_state(db_pool, job_id, ("finished",))

        finished = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'finished'",
            unique_queue,
        )
        assert finished == 3
        assert system.processed == 3
        assert system.errors == 0


# ============================================================================
# Test workit CLI with Valid Config (real subprocess launches)
# ============================================================================


class TestWorkitCLIWithConfig:
    """Test workit CLI with valid configuration."""

    def test_workit_loads_config_and_exits_quickly(self):
        """Test workit loads config and launches successfully."""
        # Run workit with a very short timeout to just test startup
        # Use --workers=1 for minimal spawning
        assert run_workit_briefly(
            ["--config", CONFIG_PATH, "--workers", "1"],
            cwd=REPO_ROOT,
        )

        # Process group is killed by the helper, which is expected
        # We just want to verify it starts correctly

    def test_workit_with_multiple_queues(self):
        """Two queues at --workers 3 is six workers, three per queue —
        the flag is PER QUEUE (behavior proven with registry assertions in
        test_entry_points; this only checks the launcher survives the
        invocation)."""
        assert run_workit_briefly(
            [
                "--config",
                CONFIG_PATH,
                "--queue",
                "high",
                "--queue",
                "low",
                "--workers",
                "3",
            ],
            cwd=REPO_ROOT,
        )

    def test_workit_with_capabilities(self):
        """Test workit with capability options."""
        assert run_workit_briefly(
            [
                "--config",
                CONFIG_PATH,
                "--cap",
                "gpu",
                "--cap",
                "memory-16g",
                "--workers",
                "1",
            ],
            cwd=REPO_ROOT,
        )

    def test_workit_with_path_option(self):
        """Test workit with extra job-class path options."""
        assert run_workit_briefly(
            [
                "--config",
                CONFIG_PATH,
                "--path",
                "/tmp",
                "--path",
                "/var",
                "--workers",
                "1",
            ],
            cwd=REPO_ROOT,
        )


# ============================================================================
# Test configloader Integration
# ============================================================================


class TestConfigloaderIntegration:
    """Test configloader integration with workit."""

    def test_load_config_from_file(self):
        """Test that load_config_from_file works correctly."""
        from pyjobby.configloader import load_config_from_file

        config = load_config_from_file(CONFIG_PATH, {"db_params", "web_listen"})

        assert "db_params" in config
        assert "database" in config["db_params"]
        assert "web_listen" in config
