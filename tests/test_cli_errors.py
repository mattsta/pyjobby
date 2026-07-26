"""Failure-path tests for the `pj-admin` command groups.

Everything here is a mistake an operator makes under pressure: a config
file that isn't there, a DSN with the wrong database, a job id that no
longer exists, retrying a job that isn't retriable, a typo'd limit value.
Each test pins the EXIT CODE and the MESSAGE, because "it printed
something red" is not a contract -- scripts and runbooks branch on the
exit status.

Where the CLI's real behavior is questionable (an error message with exit
0, or a bare traceback) the test asserts what the code actually does and
says so in a comment, so the behavior cannot change silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import migrations
from pyjobby.cli import cli

pytestmark = pytest.mark.asyncio

# No job/DAG row ever reaches this id: the tables are emptied before each
# test and their identity sequences start at 1.
MISSING_ID = 999_999_999


def dsn_for(db_params: dict, database: str | None = None) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}"
        f"/{database or db_params['database']}"
    )


@pytest.fixture
def dsn(db_params: dict) -> str:
    return dsn_for(db_params)


async def run_cli(*args: str, input: str | None = None):
    """Invoke pj-admin in a worker thread (the CLI owns its own event loop)."""

    def _invoke():
        return CliRunner().invoke(cli, list(args), input=input)

    return await asyncio.to_thread(_invoke)


async def make_job(pool, queue: str, state: str, **cols) -> int:
    """Insert one job row directly in the given state."""
    return await pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, error_message, error_count)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        cols.get("job_class", "tests.dxe_jobs.OkJob"),
        queue,
        state,
        cols.get("error_message"),
        cols.get("error_count", 0),
    )


async def unused_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


@pytest_asyncio.fixture
async def scratch_db(db_params: dict):
    """Factory for throwaway databases, dropped at teardown."""
    import uuid

    admin = await asyncpg.connect(**db_params)
    created: list[str] = []

    async def _make(*, schema: bool = False) -> str:
        name = f"pj_err_{uuid.uuid4().hex[:12]}"
        await admin.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        if schema:
            conn = await asyncpg.connect(**{**db_params, "database": name})
            try:
                await migrations.migrate(conn)
            finally:
                await conn.close()
        return name

    try:
        yield _make
    finally:
        for name in created:
            # best effort: a leaked scratch database must not fail the test
            with contextlib.suppress(asyncpg.PostgresError):
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ============================================================================
# --config failures
# ============================================================================


class TestConfigFile:
    async def test_valid_config_file_is_used(self, tmp_path, db_params):
        """Control case: the --config path really works, so the failures below
        are about the failure, not about a broken config loader."""
        conf = tmp_path / "pyjobby.conf.py"
        conf.write_text(f"db_params = {db_params!r}\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output

    async def test_missing_config_file_exits_one(self, tmp_path):
        missing = tmp_path / "absent.conf.py"

        result = await run_cli("--config", str(missing), "jobs", "list")

        assert result.exit_code == 1
        # NOTE: the configloader raises RuntimeError("... doesn't exist"), not
        # FileNotFoundError, so the CLI's friendlier "Config file not found /
        # Use --config to specify config file path" branch is unreachable and
        # a missing config is reported as a *database* failure instead.
        assert result.stderr.startswith("Error: Failed to connect to database:")
        assert f"'{missing}' doesn't exist" in result.stderr
        assert "Config file not found" not in result.stderr

    async def test_config_without_db_params_exits_one(self, tmp_path):
        conf = tmp_path / "nodb.conf.py"
        conf.write_text("workers = 4\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 1
        assert f"Error: No db_params found in config file: {conf}" in result.stderr
        assert "Config file must define db_params dict" in result.stderr

    async def test_config_with_empty_db_params_exits_one(self, tmp_path):
        conf = tmp_path / "empty.conf.py"
        conf.write_text("db_params = {}\n")

        result = await run_cli("--config", str(conf), "queues", "list")

        assert result.exit_code == 1
        assert f"Error: No db_params found in config file: {conf}" in result.stderr

    async def test_config_that_raises_on_import_exits_one(self, tmp_path):
        conf = tmp_path / "broken.conf.py"
        conf.write_text("raise RuntimeError('bad config')\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 1
        assert "Error: Failed to connect to database:" in result.stderr
        assert f"Failed to read config file: {conf}" in result.stderr
        assert "bad config" in result.stderr

    async def test_unreadable_config_file_exits_one(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions, so 000 is still readable")
        conf = tmp_path / "locked.conf.py"
        conf.write_text("db_params = {}\n")
        conf.chmod(0o000)
        try:
            result = await run_cli("--config", str(conf), "jobs", "list")
        finally:
            conf.chmod(0o600)

        assert result.exit_code == 1
        assert "Error: Failed to connect to database:" in result.stderr
        assert f"Failed to read config file: {conf}" in result.stderr
        assert "Permission denied" in result.stderr

    async def test_dsn_overrides_a_broken_config(self, tmp_path, dsn):
        """--dsn wins, so a stale config file cannot block an emergency."""
        result = await run_cli(
            "--config", str(tmp_path / "absent.conf.py"), "--dsn", dsn, "jobs", "list"
        )

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output


# ============================================================================
# --dsn failures
# ============================================================================


class TestBadDsn:
    async def test_malformed_dsn_exits_one(self):
        result = await run_cli("--dsn", "not-a-dsn", "jobs", "list")

        assert result.exit_code == 1
        assert "Error: Failed to connect to database: invalid DSN:" in result.stderr
        assert 'scheme is expected to be either "postgresql" or "postgres"' in (
            result.stderr
        )

    async def test_unreachable_port_exits_one(self, db_params):
        port = await unused_port()
        bad = (
            f"postgresql://{db_params['user']}:{db_params['password']}"
            f"@127.0.0.1:{port}/{db_params['database']}"
        )

        result = await run_cli("--dsn", bad, "queues", "list")

        assert result.exit_code == 1
        assert "Error: Failed to connect to database:" in result.stderr
        assert "Connect call failed" in result.stderr

    async def test_unknown_database_exits_one(self, db_params):
        result = await run_cli("--dsn", dsn_for(db_params, "pj_no_such_db"), "metrics")

        assert result.exit_code == 1
        assert (
            'Error: Failed to connect to database: database "pj_no_such_db" '
            "does not exist" in result.stderr
        )

    async def test_unknown_role_exits_one(self, db_params):
        # NOTE: an authentication *failure* (wrong password) is not testable on
        # a host whose pg_hba.conf trusts local connections; an unknown role
        # exercises the same connection-error handler.
        bad = (
            f"postgresql://pj_no_such_role:whatever"
            f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
        )

        result = await run_cli("--dsn", bad, "workers", "list")

        assert result.exit_code == 1
        assert (
            'Error: Failed to connect to database: role "pj_no_such_role" '
            "does not exist" in result.stderr
        )

    async def test_every_group_fails_the_same_way(self, db_params):
        """One broken DSN, one shared handler: no group swallows the error."""
        bad = dsn_for(db_params, "pj_no_such_db")
        commands = (
            ("jobs", "list"),
            ("jobs", "inspect", "1"),
            ("queues", "list"),
            ("queues", "stats"),
            ("workers", "stats"),
            ("dlq", "list"),
            ("schedule", "list"),
            ("dag", "list"),
            ("db", "status"),
            ("metrics",),
        )
        for command in commands:
            result = await run_cli("--dsn", bad, *command)
            assert result.exit_code == 1, f"{command}: {result.output}"
            assert 'database "pj_no_such_db" does not exist' in result.stderr, command


# ============================================================================
# jobs: unknown ids
# ============================================================================


class TestJobsUnknownId:
    async def test_inspect_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_inspect_json_also_exits_one(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "jobs", "inspect", str(MISSING_ID), "--json"
        )

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr
        assert result.stdout == ""

    async def test_cancel_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_retry_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "retry", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_requeue_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "requeue", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_requeue_fresh_exits_one(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "jobs", "requeue", str(MISSING_ID), "--fresh"
        )

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_delete_exits_one(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "jobs", "delete", str(MISSING_ID), "--force"
        )

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    async def test_history_is_not_an_error(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "history", str(MISSING_ID))

        # NOTE: read-only introspection of a missing job exits 0 (an absent job
        # and a job with no transitions are indistinguishable here).
        assert result.exit_code == 0, result.output
        assert f"No history for job {MISSING_ID}" in result.output
        assert "Error" not in result.output

    async def test_history_json_is_an_empty_array(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "jobs", "history", str(MISSING_ID), "--json"
        )

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "[]"

    async def test_steps_is_not_an_error(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "steps", str(MISSING_ID))

        assert result.exit_code == 0, result.output
        assert f"No step checkpoints for job {MISSING_ID}" in result.output

    async def test_steps_json_is_an_empty_array(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "steps", str(MISSING_ID), "--json")

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "[]"

    async def test_delete_declined_at_the_prompt_changes_nothing(
        self, dsn, db_pool, unique_queue
    ):
        job_id = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "jobs", "delete", str(job_id), input="n\n")

        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb WHERE id = $1", job_id)


# ============================================================================
# jobs: wrong state
# ============================================================================


class TestJobsWrongState:
    @pytest.mark.parametrize("state", ["finished", "crashed", "cancelled"])
    async def test_cancel_terminal_job_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(job_id))

        assert result.exit_code == 1
        assert (
            f"Error: Job {job_id} is in state '{state}' and cannot be cancelled"
            in result.stderr
        )
        assert (
            await db_pool.fetchval("SELECT state::text FROM jorb WHERE id = $1", job_id)
            == state
        )

    @pytest.mark.parametrize("state", ["queued", "running", "finished"])
    async def test_retry_non_retriable_state_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "jobs", "retry", str(job_id))

        assert result.exit_code == 1
        assert (
            f"Error: Job {job_id} is in state '{state}', "
            "can only retry crashed or cancelled jobs" in result.stderr
        )

    async def test_retry_crashed_job_succeeds(self, dsn, db_pool, unique_queue):
        """Control case: the rejection above is about the state, not the id."""
        job_id = await make_job(
            db_pool, unique_queue, "crashed", error_message="boom", error_count=3
        )

        result = await run_cli("--dsn", dsn, "jobs", "retry", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"Job {job_id} requeued for retry" in result.output
        assert (
            await db_pool.fetchval("SELECT state::text FROM jorb WHERE id = $1", job_id)
            == "queued"
        )

    @pytest.mark.parametrize("state", ["queued", "running", "claimed"])
    async def test_requeue_non_terminal_job_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "jobs", "requeue", str(job_id))

        assert result.exit_code == 1
        assert (
            f"Error: Job {job_id} is in state '{state}' and cannot be requeued "
            "(must be crashed, cancelled, or finished)" in result.stderr
        )

    async def test_bulk_retry_reports_each_failure_but_exits_zero(self, dsn):
        a, b = MISSING_ID, MISSING_ID - 1

        result = await run_cli("--dsn", dsn, "jobs", "retry", str(a), str(b))

        # NOTE: a single bad id exits 1, but the bulk path only prints a
        # summary -- two failures out of two still exits 0.
        assert result.exit_code == 0, result.output
        assert f"Error: Job {a}: Job {a} not found" in result.stderr
        assert f"Error: Job {b}: Job {b} not found" in result.stderr
        assert "Retried: 0" in result.output
        assert "Failed: 2" in result.output

    async def test_bulk_cancel_mixes_success_and_failure(
        self, dsn, db_pool, unique_queue
    ):
        good = await make_job(db_pool, unique_queue, "queued")
        done = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(good), str(done))

        assert result.exit_code == 0, result.output
        assert f"Job {good} cancelled" in result.output
        assert (
            f"Error: Job {done}: Job {done} is in state 'finished' and cannot "
            "be cancelled" in result.stderr
        )
        assert "Cancelled: 1" in result.output
        assert "Failed: 1" in result.output


# ============================================================================
# jobs: invalid filters
# ============================================================================


class TestJobsInvalidFilters:
    async def test_unknown_state_filter_raises_a_database_error(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "list", "--state", "bogus")

        # NOTE: the state is passed straight to the jorbstate enum, so a typo
        # surfaces as an unhandled asyncpg error (exit 1, bare traceback)
        # instead of "unknown state: bogus".
        assert result.exit_code == 1
        assert isinstance(result.exception, asyncpg.InvalidTextRepresentationError)
        assert 'invalid input value for enum jorbstate: "bogus"' in str(
            result.exception
        )

    async def test_unknown_state_in_queues_clear_raises_too(self, dsn, unique_queue):
        result = await run_cli(
            "--dsn", dsn, "queues", "clear", unique_queue, "--state", "bogus", "--force"
        )

        assert result.exit_code == 1
        assert isinstance(result.exception, asyncpg.InvalidTextRepresentationError)

    async def test_unmatched_filters_are_not_an_error(self, dsn, unique_queue):
        result = await run_cli("--dsn", dsn, "jobs", "list", "--queue", unique_queue)

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output


# ============================================================================
# dlq
# ============================================================================


class TestDlq:
    async def test_retry_unknown_job_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "dlq", "retry", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Job {MISSING_ID} not found" in result.stderr

    @pytest.mark.parametrize("state", ["queued", "running", "finished", "cancelled"])
    async def test_retry_job_that_is_not_crashed_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "dlq", "retry", str(job_id))

        assert result.exit_code == 1
        assert f"Error: Job {job_id} is not in DLQ (state: {state})" in result.stderr
        assert (
            await db_pool.fetchval("SELECT state::text FROM jorb WHERE id = $1", job_id)
            == state
        )

    async def test_retry_crashed_job_resets_the_error_budget(
        self, dsn, db_pool, unique_queue
    ):
        job_id = await make_job(
            db_pool, unique_queue, "crashed", error_message="boom", error_count=5
        )

        result = await run_cli("--dsn", dsn, "dlq", "retry", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"DLQ job {job_id} requeued (error count reset to 0)" in result.output
        row = await db_pool.fetchrow(
            "SELECT state::text AS state, error_count FROM jorb WHERE id = $1", job_id
        )
        assert (row["state"], row["error_count"]) == ("queued", 0)


# ============================================================================
# queues
# ============================================================================


class TestQueues:
    async def test_stats_for_unknown_queue_reports_nothing(self, dsn, unique_queue):
        result = await run_cli("--dsn", dsn, "queues", "stats", "-q", unique_queue)

        assert result.exit_code == 0, result.output
        assert "No stats available" in result.output

    async def test_stats_json_for_unknown_queue_is_an_empty_array(
        self, dsn, unique_queue
    ):
        result = await run_cli(
            "--dsn", dsn, "queues", "stats", "-q", unique_queue, "--json"
        )

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "[]"

    async def test_show_for_unknown_queue_says_so(self, dsn, unique_queue):
        result = await run_cli("--dsn", dsn, "queues", "show", unique_queue)

        assert result.exit_code == 0, result.output
        assert f"Queue '{unique_queue}' has no jobs and no control row" in result.output

    async def test_show_json_for_unknown_queue_is_all_empty(self, dsn, unique_queue):
        import json

        result = await run_cli("--dsn", dsn, "queues", "show", unique_queue, "--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"control": None, "stats": []}

    async def test_limits_for_unknown_queue_says_no_control_row(
        self, dsn, unique_queue
    ):
        result = await run_cli("--dsn", dsn, "queues", "limits", unique_queue)

        assert result.exit_code == 0, result.output
        assert (
            f"Queue '{unique_queue}' has no control row "
            "(unpaused, unlimited defaults)" in result.output
        )

    async def test_limits_rejects_non_integer_max_concurrency(self, dsn, unique_queue):
        result = await run_cli(
            "--dsn", dsn, "queues", "limits", unique_queue, "--max-concurrency", "abc"
        )

        assert result.exit_code == 2
        assert (
            "Error: --max-concurrency must be an integer or 'none' (got 'abc')"
            in result.stderr
        )

    async def test_limits_rejects_non_integer_rate_limit(
        self, dsn, unique_queue, db_pool
    ):
        result = await run_cli(
            "--dsn", dsn, "queues", "limits", unique_queue, "--rate-limit", "5x"
        )

        assert result.exit_code == 2
        assert (
            "Error: --rate-limit must be an integer or 'none' (got '5x')"
            in result.stderr
        )
        # rejected before any database work: no control row was created
        assert not await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb_queue WHERE name = $1", unique_queue
        )

    async def test_limits_rejects_non_float_rate_period(self, dsn, unique_queue):
        result = await run_cli(
            "--dsn", dsn, "queues", "limits", unique_queue, "--rate-period", "abc"
        )

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--rate-period': 'abc' is not a valid float."
            in result.stderr
        )

    async def test_limits_accepts_none_to_clear(self, dsn, unique_queue, db_pool):
        """Control case: 'none' is the documented spelling for unlimited."""
        result = await run_cli(
            "--dsn", dsn, "queues", "limits", unique_queue, "--max-concurrency", "none"
        )

        assert result.exit_code == 0, result.output
        assert "Max concurrency:     -" in result.output
        assert (
            await db_pool.fetchval(
                "SELECT max_concurrency FROM jorb_queue WHERE name = $1", unique_queue
            )
            is None
        )

    async def test_pause_unknown_queue_creates_a_control_row(
        self, dsn, unique_queue, db_pool
    ):
        # A queue is just a name on a job row, so there is nothing to "not
        # find": pausing pre-creates the control row and later jobs obey it.
        paused = await run_cli("--dsn", dsn, "queues", "pause", unique_queue)

        assert paused.exit_code == 0, paused.output
        assert f"Queue '{unique_queue}' paused" in paused.output
        assert (
            await db_pool.fetchval(
                "SELECT paused FROM jorb_queue WHERE name = $1", unique_queue
            )
            is True
        )

        resumed = await run_cli("--dsn", dsn, "queues", "resume", unique_queue)

        assert resumed.exit_code == 0, resumed.output
        assert f"Queue '{unique_queue}' resumed" in resumed.output
        assert (
            await db_pool.fetchval(
                "SELECT paused FROM jorb_queue WHERE name = $1", unique_queue
            )
            is False
        )

    async def test_clear_with_empty_queue_name_raises(self, dsn):
        result = await run_cli("--dsn", dsn, "queues", "clear", "", "--force")

        # NOTE: an empty queue name falsifies every filter, and clear_queue's
        # guard escapes as an unhandled ValueError (exit 1, bare traceback).
        assert result.exit_code == 1
        assert isinstance(result.exception, ValueError)
        assert "Must specify at least one filter" in str(result.exception)

    async def test_clear_declined_at_the_prompt_deletes_nothing(
        self, dsn, db_pool, unique_queue
    ):
        await make_job(db_pool, unique_queue, "finished")

        result = await run_cli(
            "--dsn", dsn, "queues", "clear", unique_queue, input="n\n"
        )

        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE queue = $1", unique_queue
        )


# ============================================================================
# schedule
# ============================================================================


class TestScheduleUnknownName:
    """Unknown schedules print an error and exit 0.

    NOTE: every one of these commands reports "Schedule not found" on stderr
    and then returns normally, so `pj-admin schedule show x && deploy` still
    runs the deploy. The exit code is asserted as 0 because that is what the
    code does today, not because it is right.
    """

    async def test_show_by_name(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "show", "no_such_sched")

        assert result.exit_code == 0, result.output
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_show_by_id(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "show", str(MISSING_ID))

        assert result.exit_code == 0, result.output
        assert f"Error: Schedule not found: {MISSING_ID}" in result.stderr

    async def test_history(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "history", "no_such_sched")

        assert result.exit_code == 0, result.output
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_enable(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "enable", "no_such_sched")

        assert result.exit_code == 0, result.output
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_disable(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "disable", "no_such_sched")

        assert result.exit_code == 0, result.output
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_delete_confirmed(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "schedule", "delete", "no_such_sched", "--yes"
        )

        assert result.exit_code == 0, result.output
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_delete_without_confirmation_aborts(self, dsn, db_pool):
        result = await run_cli(
            "--dsn", dsn, "schedule", "delete", "no_such_sched", input="n\n"
        )

        assert result.exit_code == 1
        assert "Aborted!" in result.output


class TestScheduleAddValidation:
    async def _count(self, db_pool, name: str) -> int:
        return await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", name
        )

    async def test_invalid_cron_expression(self, dsn, db_pool, test_id):
        result = await run_cli(
            "--dsn", dsn, "schedule", "add", test_id, "tests.dxe_jobs.OkJob", "nope"
        )

        # NOTE: validation failures print an error but exit 0 (see above).
        assert result.exit_code == 0, result.output
        assert "Error: Invalid cron expression or timezone:" in result.stderr
        assert "Exactly 5, 6 or 7 columns" in result.stderr
        assert await self._count(db_pool, test_id) == 0

    async def test_cron_with_out_of_range_field(self, dsn, db_pool, test_id):
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 99 * * *",
        )

        assert result.exit_code == 0, result.output
        assert "Error: Invalid cron expression or timezone:" in result.stderr
        assert await self._count(db_pool, test_id) == 0

    async def test_invalid_timezone(self, dsn, db_pool, test_id):
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--timezone",
            "Mars/Phobos",
        )

        assert result.exit_code == 0, result.output
        assert (
            "Error: Invalid cron expression or timezone: 'Mars/Phobos'" in result.stderr
        )
        assert await self._count(db_pool, test_id) == 0

    async def test_invalid_kwargs_json(self, dsn, db_pool, test_id):
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--kwargs",
            "{bad json",
        )

        assert result.exit_code == 0, result.output
        assert "Error: Invalid JSON for kwargs:" in result.stderr
        assert await self._count(db_pool, test_id) == 0

    async def test_duplicate_name(self, dsn, db_pool, test_id):
        first = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
        )
        assert first.exit_code == 0, first.output
        assert f"Schedule created: {test_id}" in first.output

        second = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 3 * * *",
        )

        assert second.exit_code == 0, second.output
        assert "Error: Failed to create schedule:" in second.stderr
        assert "duplicate key value violates unique constraint" in second.stderr
        assert await self._count(db_pool, test_id) == 1

    async def test_non_integer_priority_is_a_click_error(self, dsn, test_id):
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--prio",
            "high",
        )

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--prio' / '-p': 'high' is not a valid integer."
            in result.stderr
        )

    async def test_list_enabled_requires_a_boolean(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "list", "--enabled", "maybe")

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--enabled': 'maybe' is not a valid boolean."
            in (result.stderr)
        )


# ============================================================================
# dag
# ============================================================================


class TestDag:
    async def test_show_unknown_dag_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "dag", "show", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: DAG {MISSING_ID} not found" in result.stderr

    async def test_visualize_unknown_dag_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "dag", "visualize", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: DAG {MISSING_ID} not found or has no jobs" in result.stderr

    async def test_visualize_dag_without_jobs_exits_one(self, dsn, db_pool, test_id):
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", test_id
        )

        result = await run_cli("--dsn", dsn, "dag", "visualize", str(dag_id))

        # an existing DAG with no jobs is reported with the same message
        assert result.exit_code == 1
        assert f"Error: DAG {dag_id} not found or has no jobs" in result.stderr


# ============================================================================
# db migrate / status
# ============================================================================


class TestDbCommands:
    async def test_status_on_an_empty_database(self, dsn, db_params, scratch_db):
        name = await scratch_db()

        result = await run_cli("--dsn", dsn_for(db_params, name), "db", "status")

        assert result.exit_code == 0, result.output
        assert "Base schema installed: no" in result.output
        assert "Applied migrations:    none" in result.output
        assert "Pending migrations:    none" in result.output

    async def test_migrate_without_create_privilege_fails(
        self, db_params, scratch_db, db_pool
    ):
        name = await scratch_db()
        conn = await asyncpg.connect(**{**db_params, "database": name})
        try:
            # strip CREATE from the only schema the role can write
            await conn.execute("REVOKE CREATE ON SCHEMA public FROM pg_database_owner")
            await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        finally:
            await conn.close()

        result = await run_cli("--dsn", dsn_for(db_params, name), "db", "migrate")

        # NOTE: no operator-friendly message -- the asyncpg error escapes
        # asyncio.run() as a traceback (exit 1).
        assert result.exit_code == 1
        assert isinstance(result.exception, asyncpg.InsufficientPrivilegeError)
        assert "permission denied for schema public" in str(result.exception)

        # and the database is still empty afterwards
        check = await asyncpg.connect(**{**db_params, "database": name})
        try:
            assert await check.fetchval("SELECT to_regclass('public.jorb')") is None
        finally:
            await check.close()

    async def test_migrate_with_bad_dsn_exits_one(self, db_params):
        result = await run_cli(
            "--dsn", dsn_for(db_params, "pj_no_such_db"), "db", "migrate"
        )

        assert result.exit_code == 1
        assert 'database "pj_no_such_db" does not exist' in result.stderr

    async def test_migrate_is_idempotent_on_an_installed_database(
        self, db_params, scratch_db
    ):
        """Control case: the privilege failure above is about the privilege."""
        name = await scratch_db(schema=True)

        result = await run_cli("--dsn", dsn_for(db_params, name), "db", "migrate")

        assert result.exit_code == 0, result.output
        assert "Database schema is up to date" in result.output


# ============================================================================
# Argument / flag validation (click-level, no database work)
# ============================================================================


class TestFlagValidation:
    async def test_unknown_command(self, dsn):
        result = await run_cli("--dsn", dsn, "bogus-cmd")

        assert result.exit_code == 2
        assert "Error: No such command 'bogus-cmd'." in result.stderr

    async def test_unknown_subcommand(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "bogus-sub")

        assert result.exit_code == 2
        assert "Error: No such command 'bogus-sub'." in result.stderr

    async def test_non_integer_job_id(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "inspect", "abc")

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for 'JOB_ID': 'abc' is not a valid integer."
            in result.stderr
        )

    async def test_retry_requires_at_least_one_id(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "retry")

        assert result.exit_code == 2
        assert "Error: Missing argument 'JOB_IDS...'." in result.stderr

    async def test_cancel_requires_at_least_one_id(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "cancel")

        assert result.exit_code == 2
        assert "Error: Missing argument 'JOB_IDS...'." in result.stderr

    async def test_non_integer_limit(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "list", "--limit", "abc")

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--limit' / '-l': 'abc' is not a valid integer."
            in result.stderr
        )

    async def test_doctor_thresholds_must_be_integers(self, dsn):
        result = await run_cli("--dsn", dsn, "doctor", "--max-depth", "abc")

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--max-depth': 'abc' is not a valid integer."
            in result.stderr
        )

    async def test_unknown_option(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "list", "--nope")

        assert result.exit_code == 2
        assert "Error: No such option: --nope" in result.stderr

    async def test_queues_clear_requires_a_queue_argument(self, dsn):
        result = await run_cli("--dsn", dsn, "queues", "clear")

        assert result.exit_code == 2
        assert "Error: Missing argument 'QUEUE'." in result.stderr

    async def test_schedule_add_requires_three_arguments(self, dsn, test_id):
        result = await run_cli("--dsn", dsn, "schedule", "add", test_id)

        assert result.exit_code == 2
        assert "Error: Missing argument 'JOB_CLASS'." in result.stderr

    async def test_metrics_since_hours_must_be_an_integer(self, dsn):
        result = await run_cli("--dsn", dsn, "metrics", "--since-hours", "lots")

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--since-hours': 'lots' is not a valid integer."
            in result.stderr
        )

    async def test_negative_since_hours_is_accepted_and_matches_nothing(
        self, dsn, db_pool, unique_queue
    ):
        """A sign typo is not rejected: the window simply looks into the future."""
        await make_job(db_pool, unique_queue, "finished")

        result = await run_cli(
            "--dsn", dsn, "metrics", "--since-hours", "-24", "--json"
        )

        assert result.exit_code == 0, result.output
        import json

        assert json.loads(result.stdout)["finished_count"] == 0


# ============================================================================
# Environment-driven DSN
# ============================================================================


class TestEnvironmentDsn:
    async def test_pyjobby_dsn_env_var_is_used(self, monkeypatch, db_params):
        """PYJOBBY_DSN is the documented fallback, so its failures matter too."""
        monkeypatch.setenv("PYJOBBY_DSN", dsn_for(db_params, "pj_no_such_db"))

        result = await run_cli("jobs", "list")

        assert result.exit_code == 1
        assert 'database "pj_no_such_db" does not exist' in result.stderr

    async def test_explicit_dsn_beats_the_environment(self, monkeypatch, db_params):
        monkeypatch.setenv("PYJOBBY_DSN", dsn_for(db_params, "pj_no_such_db"))

        result = await run_cli("--dsn", dsn_for(db_params), "jobs", "list")

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output


# ============================================================================
# Stale rows: a job whose worker vanished
# ============================================================================


class TestStaleRegistryRows:
    async def test_cancel_running_job_with_a_dead_worker_is_a_request(
        self, dsn, db_pool, unique_queue
    ):
        """Cancelling a 'running' job only records the request; if the worker
        is gone (stale registry row) nothing else happens, and the operator
        must be able to see that instead of assuming the job stopped."""
        worker_id = await db_pool.fetchval(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ('gone', 1, $1, now() - $2::interval) RETURNING id""",
            unique_queue,
            timedelta(hours=1),
        )
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, claimed_by, worker_host,
                                 worker_pid, started)
               VALUES ('tests.dxe_jobs.SlowJob', $1, 'running', $2, 'gone', 1, now())
               RETURNING id""",
            unique_queue,
            worker_id,
        )

        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(job_id))

        assert result.exit_code == 0, result.output
        # NOTE: the API returns status 'cancel_requested' here, but the CLI
        # hardcodes "cancelled" -- the operator is told the job stopped when
        # only a request was recorded.
        assert f"Job {job_id} cancelled" in result.output
        row = await db_pool.fetchrow(
            "SELECT state::text AS state, cancel_requested FROM jorb WHERE id = $1",
            job_id,
        )
        # still running, only flagged: the dead worker will never act on it
        assert row["state"] == "running"
        assert row["cancel_requested"] is True
