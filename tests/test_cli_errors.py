"""Failure-path tests for the `pj-admin` command groups.

Everything here is a mistake an operator makes under pressure: a config
file that isn't there, a DSN with the wrong database, a job id that no
longer exists, retrying a job that isn't retriable, a typo'd limit value.
Each test pins the EXIT CODE and the MESSAGE, because "it printed
something red" is not a contract -- scripts and runbooks branch on the
exit status.

Operator-facing failures print their message AND exit non-zero -- every
one of them, so `pj-admin ... && next-step` is safe to write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import migrations
from pyjobby.cli import cli
from pyjobby.client import DEFAULT_PRIO_CEILING
from tests.conftest import reserved_unused_port
from tests.schema_fixtures import drop_database

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


@pytest_asyncio.fixture
async def scratch_db(db_params: dict):
    """Factory for throwaway databases, dropped at teardown."""
    import uuid

    admin = await asyncpg.connect(**db_params)
    created: list[str] = []

    async def _make(*, schema: bool = False, stale: bool = False) -> str:
        """`stale=True` installs the current schema and then drops the
        objects the stale-message tests probe: a database at a different
        shape than the one this release addresses."""
        name = f"pj_err_{uuid.uuid4().hex[:12]}"
        await admin.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        if schema or stale:
            conn = await asyncpg.connect(**{**db_params, "database": name})
            try:
                await migrations.migrate(conn)
                if stale:
                    await conn.execute("ALTER TABLE jorb DROP COLUMN tags")
                    await conn.execute("ALTER TABLE jorb DROP COLUMN claimed_at")
                    await conn.execute(
                        "ALTER TABLE jorb_worker DROP COLUMN job_threads"
                    )
                    await conn.execute("DROP FUNCTION claim_jorb")
            finally:
                await conn.close()
        return name

    try:
        yield _make
    finally:
        for name in created:
            # best effort: a leaked scratch database must not fail the test
            with contextlib.suppress(asyncpg.PostgresError):
                await drop_database(admin, name)
        await admin.close()


# ============================================================================
# --config failures
# ============================================================================


class TestConfigFile:
    async def test_valid_config_file_is_used(self, tmp_path, db_params):
        """Control case: the --config path really works, so the failures below
        are about the failure, not about a broken config loader."""
        from pyjobby.procs import write_config_toml

        conf = write_config_toml(tmp_path / "pyjobby.toml", db_params)

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output

    async def test_python_config_is_refused_by_name(self, tmp_path, db_params):
        """A .py config is an executable-config format; the loader refuses
        it with the migration hint rather than executing it."""
        conf = tmp_path / "pyjobby.conf.py"
        conf.write_text(f"db_params = {db_params!r}\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 1
        assert "is a Python file; pyjobby config is TOML" in result.stderr
        assert "never executed" in result.stderr

    async def test_missing_config_file_exits_one(self, tmp_path):
        missing = tmp_path / "absent.toml"

        result = await run_cli("--config", str(missing), "jobs", "list")

        assert result.exit_code == 1
        # A config problem is reported as a config problem: the operator is
        # told which file could not be loaded, why, and how to point the CLI
        # somewhere else. It is NOT misreported as a database failure.
        assert result.stderr.startswith(f"Error: Could not load config file: {missing}")
        assert f"Error: '{missing}' doesn't exist" in result.stderr
        assert (
            "Error: Use --config to point at a pyjobby conf file, or --dsn to "
            "connect directly." in result.stderr
        )
        assert "Failed to connect to database" not in result.stderr

    async def test_config_without_db_params_exits_one(self, tmp_path):
        conf = tmp_path / "nodb.toml"
        # a KNOWN key, so this is the no-db_params failure and not the
        # unknown-key one (which the loader refuses first, by name)
        conf.write_text("prio_ceiling = 900\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 1
        assert f"Error: No db_params found in config file: {conf}" in result.stderr
        assert "Error: Config file must define a db_params dict" in result.stderr
        assert "Failed to connect to database" not in result.stderr

    async def test_config_with_empty_db_params_exits_one(self, tmp_path):
        conf = tmp_path / "empty.toml"
        conf.write_text("[db_params]\n")

        result = await run_cli("--config", str(conf), "queues", "list")

        assert result.exit_code == 1
        assert f"Error: No db_params found in config file: {conf}" in result.stderr

    async def test_config_with_invalid_toml_exits_one(self, tmp_path):
        """Config is DATA now: the failure mode is a parse error at a line,
        never arbitrary code raising during import."""
        conf = tmp_path / "broken.toml"
        conf.write_text("= this is not toml\n")

        result = await run_cli("--config", str(conf), "jobs", "list")

        assert result.exit_code == 1
        assert f"Error: Could not load config file: {conf}" in result.stderr
        assert f"Error: Failed to parse config file {conf}:" in result.stderr
        assert (
            "Error: Use --config to point at a pyjobby conf file, or --dsn to "
            "connect directly." in result.stderr
        )
        assert "Failed to connect to database" not in result.stderr

    async def test_unreadable_config_file_exits_one(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions, so 000 is still readable")
        conf = tmp_path / "locked.toml"
        conf.write_text("[db_params]\n")
        conf.chmod(0o000)
        try:
            result = await run_cli("--config", str(conf), "jobs", "list")
        finally:
            conf.chmod(0o600)

        assert result.exit_code == 1
        assert f"Error: Could not load config file: {conf}" in result.stderr
        assert f"Error: Failed to read config file: {conf}:" in result.stderr
        assert "Permission denied" in result.stderr
        assert (
            "Error: Use --config to point at a pyjobby conf file, or --dsn to "
            "connect directly." in result.stderr
        )
        assert "Failed to connect to database" not in result.stderr

    async def test_dsn_overrides_a_broken_config(self, tmp_path, dsn):
        """--dsn wins, so a stale config file cannot block an emergency."""
        result = await run_cli(
            "--config", str(tmp_path / "absent.toml"), "--dsn", dsn, "jobs", "list"
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
        # the port stays reserved until the assertion: sampling one and
        # letting it go hands the next xdist worker a real listener to hit
        with reserved_unused_port() as port:
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
        """cancel/retry/rerun answer one refusal for "missing" and "wrong
        state" alike, so the message names both -- an operator must not have
        to guess which one they hit."""
        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(MISSING_ID))

        assert result.exit_code == 1
        assert (
            f"Error: Job {MISSING_ID} cannot be cancelled "
            "(not found, or already terminal)" in result.stderr
        )

    async def test_retry_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "retry", str(MISSING_ID))

        assert result.exit_code == 1
        assert (
            f"Error: Job {MISSING_ID} cannot be retried "
            "(not found, or not crashed/cancelled)" in result.stderr
        )

    async def test_rerun_exits_one(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "rerun", str(MISSING_ID))

        assert result.exit_code == 1
        assert (
            f"Error: Job {MISSING_ID} cannot be rerun "
            "(not found, or not crashed, cancelled, or finished)" in result.stderr
        )

    async def test_rerun_resume_exits_one(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "jobs", "rerun", str(MISSING_ID), "--resume"
        )

        assert result.exit_code == 1
        assert (
            f"Error: Job {MISSING_ID} cannot be rerun "
            "(not found, or not crashed, cancelled, or finished)" in result.stderr
        )

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


class TestJobsDeleteMany:
    """`jobs delete` takes as many ids as `jobs retry` and `jobs cancel`."""

    async def test_several_ids_are_deleted_with_a_per_id_line(
        self, dsn, db_pool, unique_queue
    ):
        ids = [await make_job(db_pool, unique_queue, "finished") for _ in range(3)]

        result = await run_cli(
            "--dsn", dsn, "jobs", "delete", *[str(i) for i in ids], "--force"
        )

        assert result.exit_code == 0, result.output
        for job_id in ids:
            assert f"Job {job_id} deleted" in result.output
        assert "Deleted: 3" in result.output
        assert (
            await db_pool.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE id = ANY($1::bigint[])", ids
            )
            == 0
        )

    async def test_a_missing_id_is_reported_and_exits_one(
        self, dsn, db_pool, unique_queue
    ):
        """The rest still go: a bulk verb reports per id and then fails."""
        job_id = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli(
            "--dsn", dsn, "jobs", "delete", str(job_id), str(MISSING_ID), "--force"
        )

        assert result.exit_code == 1
        assert f"Job {job_id} deleted" in result.output
        assert f"Error: Job {MISSING_ID} not found" in result.stderr
        assert "Deleted: 1" in result.output
        assert not await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE id = $1", job_id
        )

    async def test_one_prompt_covers_the_whole_list(self, dsn, db_pool, unique_queue):
        """Declining once leaves every one of them alone."""
        ids = [await make_job(db_pool, unique_queue, "finished") for _ in range(2)]

        result = await run_cli(
            "--dsn", dsn, "jobs", "delete", *[str(i) for i in ids], input="n\n"
        )

        assert result.exit_code == 0, result.output
        assert "Delete 2 job(s)?" in result.output
        assert "Cancelled" in result.output
        assert (
            await db_pool.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE id = ANY($1::bigint[])", ids
            )
            == 2
        )

    async def test_no_ids_at_all_is_a_usage_error(self, dsn):
        result = await run_cli("--dsn", dsn, "jobs", "delete", "--force")

        assert result.exit_code == 2


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
            f"Error: Job {job_id} cannot be cancelled "
            "(not found, or already terminal)" in result.stderr
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
            f"Error: Job {job_id} cannot be retried "
            "(not found, or not crashed/cancelled)" in result.stderr
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
    async def test_rerun_non_terminal_job_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "jobs", "rerun", str(job_id))

        assert result.exit_code == 1
        assert (
            f"Error: Job {job_id} cannot be rerun "
            "(not found, or not crashed, cancelled, or finished)" in result.stderr
        )

    async def test_bulk_retry_exits_one_when_every_job_failed(self, dsn):
        """The bulk path exits non-zero exactly like the single-job form, so
        `pj-admin jobs retry a b && next-step` cannot run after a failure."""
        a, b = MISSING_ID, MISSING_ID - 1

        result = await run_cli("--dsn", dsn, "jobs", "retry", str(a), str(b))

        assert result.exit_code == 1
        assert f"Error: Job {a} cannot be retried" in result.stderr
        assert f"Error: Job {b} cannot be retried" in result.stderr
        assert "Retried: 0" in result.output
        assert "Error:   Failed: 2" in result.stderr

    async def test_bulk_retry_exits_zero_when_every_job_succeeded(
        self, dsn, db_pool, unique_queue
    ):
        """Control case: the exit code tracks the failures, not the batch."""
        a = await make_job(db_pool, unique_queue, "crashed", error_count=2)
        b = await make_job(db_pool, unique_queue, "crashed", error_count=1)

        result = await run_cli("--dsn", dsn, "jobs", "retry", str(a), str(b))

        assert result.exit_code == 0, result.output
        assert f"Job {a} requeued" in result.output
        assert f"Job {b} requeued" in result.output
        assert "Retried: 2" in result.output
        assert "Failed" not in result.output + result.stderr

    async def test_bulk_cancel_mixes_success_and_failure(
        self, dsn, db_pool, unique_queue
    ):
        """One bad id in the batch is still a failure: the per-job lines are
        unchanged, but the command exits 1."""
        good = await make_job(db_pool, unique_queue, "queued")
        done = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(good), str(done))

        assert result.exit_code == 1
        assert f"Job {good} cancelled" in result.output
        assert (
            f"Error: Job {done} cannot be cancelled "
            "(not found, or already terminal)" in result.stderr
        )
        assert "Cancelled: 1" in result.output
        assert "Error:   Failed: 1" in result.stderr


# ============================================================================
# jobs: invalid filters
# ============================================================================


class TestJobsInvalidFilters:
    VALID_STATES = "queued, claimed, running, waiting, finished, crashed, cancelled"

    async def test_unknown_state_filter_is_rejected_with_the_valid_states(self, dsn):
        """A typo'd state never reaches the jorbstate enum: the operator gets
        the name they typed back plus the list of states that do exist."""
        result = await run_cli("--dsn", dsn, "jobs", "list", "--state", "bogus")

        # 2, not 1: the arguments were wrong, which is what click itself
        # exits 2 for -- nothing was attempted against the database
        assert result.exit_code == 2
        assert "Error: Unknown job state: 'bogus'" in result.stderr
        assert f"Error: Valid states: {self.VALID_STATES}" in result.stderr
        assert not isinstance(result.exception, asyncpg.InvalidTextRepresentationError)
        assert "jorbstate" not in result.stderr

    async def test_unknown_state_in_queues_clear_is_rejected_too(
        self, dsn, unique_queue, db_pool
    ):
        """The same guard runs before `queues clear` deletes anything."""
        job_id = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli(
            "--dsn", dsn, "queues", "clear", unique_queue, "--state", "bogus", "--force"
        )

        assert result.exit_code == 2
        assert "Error: Unknown job state: 'bogus'" in result.stderr
        assert f"Error: Valid states: {self.VALID_STATES}" in result.stderr
        assert not isinstance(result.exception, asyncpg.InvalidTextRepresentationError)
        # rejected before any database work: the queue is untouched
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb WHERE id = $1", job_id)

    @pytest.mark.parametrize(
        "state",
        ["queued", "claimed", "running", "waiting", "finished", "crashed", "cancelled"],
    )
    async def test_every_advertised_state_is_accepted(self, dsn, unique_queue, state):
        """Control case: every state named in the rejection message works."""
        result = await run_cli(
            "--dsn", dsn, "jobs", "list", "--queue", unique_queue, "--state", state
        )

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output

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
        assert (
            f"Error: Job {MISSING_ID} is not in the DLQ (not found, or not crashed)"
            in result.stderr
        )

    @pytest.mark.parametrize("state", ["queued", "running", "finished", "cancelled"])
    async def test_retry_job_that_is_not_crashed_exits_one(
        self, dsn, db_pool, unique_queue, state
    ):
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "dlq", "retry", str(job_id))

        assert result.exit_code == 1
        assert (
            f"Error: Job {job_id} is not in the DLQ (not found, or not crashed)"
            in result.stderr
        )
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
    async def test_stats_is_fleet_wide_only(self, dsn, unique_queue):
        """`queues stats` has no per-queue option: `queues show NAME` is the
        single-queue view, with the pause/limit controls alongside. Two
        spellings of one question is how they drift."""
        result = await run_cli("--dsn", dsn, "queues", "stats", "-q", unique_queue)

        assert result.exit_code == 2
        assert "no such option" in result.output.lower() + result.stderr.lower()

    async def test_stats_json_is_an_array_of_queues(self, dsn, unique_queue, db_pool):
        await make_job(db_pool, unique_queue, "queued")

        result = await run_cli("--dsn", dsn, "queues", "stats", "--json")

        assert result.exit_code == 0, result.output
        stats = json.loads(result.stdout)
        assert isinstance(stats, list)
        assert unique_queue in {row["queue"] for row in stats}

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

    @pytest.mark.parametrize("name", ["", "   "])
    async def test_clear_with_empty_queue_name_is_refused(
        self, dsn, db_pool, unique_queue, name
    ):
        """An empty name filters nothing, so clearing it would delete every
        job: refused up front with an explanation, not a bare ValueError."""
        job_id = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "queues", "clear", name, "--force")

        assert result.exit_code == 1
        assert "Error: Queue name must not be empty" in result.stderr
        assert (
            "Error: Refusing to run: an empty name filters nothing and would "
            "target every job." in result.stderr
        )
        assert not isinstance(result.exception, ValueError)
        # nothing was deleted
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb WHERE id = $1", job_id)

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

    async def test_clear_leaves_running_work_alone_by_default(
        self, dsn, db_pool, unique_queue
    ):
        """The default is unstarted work: deleting a claimed or running row
        does not stop its worker, it strands the run."""
        queued = await make_job(db_pool, unique_queue, "queued")
        running = await make_job(db_pool, unique_queue, "running")
        finished = await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "queues", "clear", unique_queue, "--force")

        assert result.exit_code == 0, result.output
        assert "Deleted 1 job(s)" in result.output
        survivors = await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE id = ANY($1::bigint[])",
            [running, finished],
        )
        assert survivors == 2
        assert not await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE id = $1", queued
        )

    async def test_the_prompt_names_the_states_it_will_delete(
        self, dsn, db_pool, unique_queue
    ):
        await make_job(db_pool, unique_queue, "queued")

        result = await run_cli(
            "--dsn", dsn, "queues", "clear", unique_queue, input="n\n"
        )

        assert "in state queued/waiting" in result.output

    async def test_an_explicit_state_reaches_running_work(
        self, dsn, db_pool, unique_queue
    ):
        running = await make_job(db_pool, unique_queue, "running")

        result = await run_cli(
            "--dsn",
            dsn,
            "queues",
            "clear",
            unique_queue,
            "--state",
            "running",
            "--force",
        )

        assert result.exit_code == 0, result.output
        assert "Deleted 1 job(s)" in result.output
        assert "in state running" in result.output
        assert not await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE id = $1", running
        )

    async def test_not_updated_for_days_spares_fresh_rows(
        self, dsn, db_pool, unique_queue
    ):
        fresh = await make_job(db_pool, unique_queue, "queued")
        stale = await make_job(db_pool, unique_queue, "queued")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '30 days' WHERE id = $1",
            stale,
        )

        result = await run_cli(
            "--dsn",
            dsn,
            "queues",
            "clear",
            unique_queue,
            "--not-updated-for-days",
            "7",
            "--force",
        )

        assert result.exit_code == 0, result.output
        assert "Deleted 1 job(s)" in result.output
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb WHERE id = $1", fresh)
        assert not await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE id = $1", stale
        )


# ============================================================================
# schedule
# ============================================================================


class TestScheduleUnknownName:
    """Unknown schedules print an error AND exit 1.

    Every one of these commands reports "Schedule not found" on stderr and
    exits non-zero, so `pj-admin schedule show x && deploy` stops before the
    deploy instead of running it against a schedule that isn't there.
    """

    async def test_show_by_name(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "show", "no_such_sched")

        assert result.exit_code == 1
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_show_by_id(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "show", str(MISSING_ID))

        assert result.exit_code == 1
        assert f"Error: Schedule not found: {MISSING_ID}" in result.stderr

    async def test_history(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "history", "no_such_sched")

        assert result.exit_code == 1
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_enable(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "enable", "no_such_sched")

        assert result.exit_code == 1
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_disable(self, dsn):
        result = await run_cli("--dsn", dsn, "schedule", "disable", "no_such_sched")

        assert result.exit_code == 1
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_delete_confirmed(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "schedule", "delete", "no_such_sched", "--force"
        )

        assert result.exit_code == 1
        assert "Error: Schedule not found: no_such_sched" in result.stderr

    async def test_delete_declined_at_the_prompt_does_nothing(self, dsn, db_pool):
        """Declining is not a failure -- the same -f/prompt shape (and the
        same 'Cancelled' + exit 0) as `jobs delete` and `queues clear`."""
        result = await run_cli(
            "--dsn", dsn, "schedule", "delete", "no_such_sched", input="n\n"
        )

        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output


class TestScheduleAddValidation:
    """`schedule add` rejections.

    Every rejection -- malformed --kwargs, invalid cron, unknown timezone,
    duplicate name -- reports on stderr and exits 1, and leaves no schedule
    behind. A provisioning script can rely on the exit status.
    """

    async def _count(self, db_pool, name: str) -> int:
        return await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", name
        )

    async def test_invalid_cron_expression(self, dsn, db_pool, test_id):
        result = await run_cli(
            "--dsn", dsn, "schedule", "add", test_id, "tests.dxe_jobs.OkJob", "nope"
        )

        assert result.exit_code == 1, result.output
        assert "Error: malformed cron expression 'nope'" in result.stderr
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

        assert result.exit_code == 1, result.output
        assert "Error: malformed cron expression '0 99 * * *'" in result.stderr
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

        assert result.exit_code == 1, result.output
        assert "Error: unknown timezone 'Mars/Phobos'" in result.stderr
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

        # this branch DOES go through fail(): malformed JSON exits 1
        assert result.exit_code == 1
        assert "Error: Invalid JSON for kwargs:" in result.stderr
        assert "Expecting property name enclosed in double quotes" in result.stderr
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

        assert second.exit_code == 1, second.output
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
            "--priority",
            "high",
        )

        assert result.exit_code == 2
        assert (
            "Error: Invalid value for '--priority' / '-p': 'high' is not a valid integer."
            in result.stderr
        )

    async def test_priority_above_the_ceiling_writes_no_schedule(
        self, dsn, db_pool, test_id
    ):
        """A schedule mints a job on EVERY firing, so one unclaimable
        priority is an unbounded stream of jobs nobody will ever run: the
        row must not exist at all, not merely be reported."""
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--priority",
            str(DEFAULT_PRIO_CEILING + 1),
        )

        # 2: a refused ARGUMENT, reported before the database is touched
        assert result.exit_code == 2, result.output
        assert (
            f"Error: priority {DEFAULT_PRIO_CEILING + 1} is above the worker "
            f"priority ceiling ({DEFAULT_PRIO_CEILING})" in result.stderr
        )
        # names the inverted ordering, which is what produces the bad value
        assert "LOWER numbers are MORE urgent" in result.stderr
        # and the escape hatch is the one an operator can actually type here
        assert (
            f"--priority {DEFAULT_PRIO_CEILING + 1} --max-prio "
            f"{DEFAULT_PRIO_CEILING + 1}" in result.stderr
        )
        assert await self._count(db_pool, test_id) == 0

    async def test_priority_at_the_ceiling_is_accepted(self, dsn, db_pool, test_id):
        """The mirror: the ceiling itself is claimable, so it is allowed."""
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--priority",
            str(DEFAULT_PRIO_CEILING),
        )

        assert result.exit_code == 0, result.output
        assert f"Schedule created: {test_id}" in result.output
        assert await self._count(db_pool, test_id) == 1
        assert (
            await db_pool.fetchval(
                "SELECT prio FROM jorb_schedule WHERE name = $1", test_id
            )
            == DEFAULT_PRIO_CEILING
        )

    async def test_a_fleet_that_declares_a_higher_ceiling_may_use_it(
        self, dsn, db_pool, test_id
    ):
        """`--max-prio` is the CLI's version of `JobClient(prio_ceiling=N)`:
        the deployment saying what its workers actually run with."""
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--priority",
            "5000",
            "--max-prio",
            "5000",
        )

        assert result.exit_code == 0, result.output
        assert (
            await db_pool.fetchval(
                "SELECT prio FROM jorb_schedule WHERE name = $1", test_id
            )
            == 5000
        )

    async def test_a_declared_ceiling_still_has_a_top(self, dsn, db_pool, test_id):
        """Raising the declaration does not disable the check -- it moves
        it, so `--prio 5001 --max-prio 5000` is still refused."""
        result = await run_cli(
            "--dsn",
            dsn,
            "schedule",
            "add",
            test_id,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--priority",
            "5001",
            "--max-prio",
            "5000",
        )

        assert result.exit_code == 2, result.output
        assert (
            "Error: priority 5001 is above the worker priority ceiling (5000)"
            in result.stderr
        )
        assert await self._count(db_pool, test_id) == 0


class TestPriorityCeilingFromConfig:
    """The fleet's ceiling is a deployment fact, declared once.

    `pj-admin` reads `prio_ceiling` from the config file, exactly as the
    four daemons and JobClient.from_config do -- before this, a fleet
    running `pj --max-prio 5000` could not set a priority its own workers
    claim happily from any CLI verb but `schedule add`.
    """

    @staticmethod
    def _config(tmp_path, db_params: dict, ceiling: int | None) -> str:
        lines = [
            "[db_params]",
            f'host = "{db_params["host"]}"',
            f"port = {db_params['port']}",
            f'database = "{db_params["database"]}"',
            f'user = "{db_params["user"]}"',
            f'password = "{db_params["password"]}"',
        ]
        if ceiling is not None:
            lines.insert(0, f"prio_ceiling = {ceiling}\n")
        conf = tmp_path / "pyjobby.toml"
        conf.write_text("\n".join(lines) + "\n")
        return str(conf)

    async def test_set_priority_above_the_default_is_refused(
        self, dsn, db_pool, unique_queue
    ):
        job_id = await make_job(db_pool, unique_queue, "queued")

        result = await run_cli(
            "--dsn", dsn, "jobs", "set-priority", str(job_id), "5000"
        )

        assert result.exit_code == 2
        assert "above the worker priority ceiling" in result.stderr
        assert (
            await db_pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
            != 5000
        )

    async def test_set_priority_accepts_it_with_max_prio(
        self, dsn, db_pool, unique_queue
    ):
        job_id = await make_job(db_pool, unique_queue, "queued")

        result = await run_cli(
            "--dsn",
            dsn,
            "jobs",
            "set-priority",
            str(job_id),
            "5000",
            "--max-prio",
            "5000",
        )

        assert result.exit_code == 0, result.output
        assert (
            await db_pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
            == 5000
        )

    async def test_set_priority_accepts_it_from_the_config_ceiling(
        self, tmp_path, db_params, db_pool, unique_queue
    ):
        """No flag at all: the config file said what the fleet runs."""
        job_id = await make_job(db_pool, unique_queue, "queued")
        config = self._config(tmp_path, db_params, 5000)

        result = await run_cli(
            "--config", config, "jobs", "set-priority", str(job_id), "5000"
        )

        assert result.exit_code == 0, result.output
        assert (
            await db_pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
            == 5000
        )

    async def test_schedule_add_reads_the_config_ceiling_too(
        self, tmp_path, db_params, db_pool
    ):
        config = self._config(tmp_path, db_params, 5000)
        name = "config_ceiling_schedule"
        await db_pool.execute("DELETE FROM jorb_schedule WHERE name = $1", name)

        result = await run_cli(
            "--config",
            config,
            "schedule",
            "add",
            name,
            "tests.dxe_jobs.OkJob",
            "0 2 * * *",
            "--priority",
            "5000",
        )

        try:
            assert result.exit_code == 0, result.output
            assert (
                await db_pool.fetchval(
                    "SELECT prio FROM jorb_schedule WHERE name = $1", name
                )
                == 5000
            )
        finally:
            await db_pool.execute("DELETE FROM jorb_schedule WHERE name = $1", name)

    async def test_a_config_without_a_ceiling_keeps_the_platform_default(
        self, tmp_path, db_params, db_pool, unique_queue
    ):
        job_id = await make_job(db_pool, unique_queue, "queued")
        config = self._config(tmp_path, db_params, None)

        result = await run_cli(
            "--config",
            config,
            "jobs",
            "set-priority",
            str(job_id),
            str(DEFAULT_PRIO_CEILING + 1),
        )

        assert result.exit_code == 2
        assert (
            f"above the worker priority ceiling ({DEFAULT_PRIO_CEILING})"
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
        # Nothing is pending and nothing is missing on a database with no
        # schema: `db migrate` installs schema.sql -- which already contains
        # every migration's effect -- and records them without running one.
        assert "Pending migrations:    none" in result.output
        assert "Missing objects:       none" in result.output

    async def test_migrate_on_a_drifted_database_fails_and_names_the_objects(
        self, db_params, scratch_db
    ):
        """`db migrate` must never say "up to date" about a database doctor
        FAILs. With jorb present and nothing pending there is no DDL that
        repairs drift, so the closed loop doctor -> migrate -> doctor needs
        migrate to be the one that breaks it: exit nonzero and name what is
        missing and what to do."""
        name = await scratch_db(stale=True)

        result = await run_cli("--dsn", dsn_for(db_params, name), "db", "migrate")

        assert result.exit_code == 1, result.output
        assert "Database schema is up to date" not in result.output
        assert "column jorb.tags" in result.stderr
        assert "Recreate" in result.stderr

    async def test_status_on_a_drifted_database_lists_what_is_missing(
        self, db_params, scratch_db
    ):
        """The line that answers the question the version lines cannot.

        A drifted database records exactly what a current one records, so
        "Applied: none / Pending: none" is literally true of both. Only the
        object list tells them apart."""
        name = await scratch_db(stale=True)

        result = await run_cli("--dsn", dsn_for(db_params, name), "db", "status")

        assert result.exit_code == 0, result.output
        assert "Base schema installed: yes" in result.output
        assert "Pending migrations:    none" in result.output
        assert "column jorb_worker.job_threads" in result.output
        assert "function claim_jorb" in result.output

    async def test_status_json_is_the_status_dict(self, dsn):
        """A deploy gate reads this: the same four facts, as data."""
        result = await run_cli("--dsn", dsn, "db", "status", "--json")

        assert result.exit_code == 0, result.output
        info = json.loads(result.stdout)
        assert info["base_schema_installed"] is True
        assert info["missing"] == []
        assert set(info) >= {
            "base_schema_installed",
            "applied",
            "pending",
            "missing",
        }

    async def test_status_json_names_missing_objects_on_a_drifted_database(
        self, db_params, scratch_db
    ):
        name = await scratch_db(stale=True)

        result = await run_cli(
            "--dsn", dsn_for(db_params, name), "db", "status", "--json"
        )

        assert result.exit_code == 0, result.output
        info = json.loads(result.stdout)
        assert "column jorb_worker.job_threads" in info["missing"]

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

        # the privilege error is translated into an operator-facing message
        # naming the missing grant -- no traceback escapes asyncio.run()
        assert result.exit_code == 1
        assert not isinstance(result.exception, asyncpg.InsufficientPrivilegeError)
        assert "Error: Not permitted to install the schema:" in result.stderr
        assert "permission denied for schema public" in result.stderr
        assert (
            "Error: The connecting role needs CREATE on the target schema."
            in result.stderr
        )
        assert "Traceback" not in result.output

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
# A schema that is missing or out of date
# ============================================================================


class TestStaleSchemaMessages:
    """What an operator sees when the database is not the shape this release
    needs, from commands that are not `doctor`.

    Every one of these used to print ~40 lines of asyncpg stack ending in
    `column "job_threads" does not exist` -- true, and useless: it names a
    column nobody asked for, does not say the schema is out of date, and does
    not name the command that fixes it. The exit code was already 1, so
    scripts were safe; only the human was not.

    Three different command groups are covered because the handler is on the
    root group, not on the commands: if it regresses, it regresses for all of
    them at once, and a single-command test would be as likely to be checking
    a coincidence.
    """

    STALE_MESSAGE = "Error: The database schema is missing or out of date:"
    STALE_REMEDY = (
        "Error: Install or upgrade it with `pj-admin db migrate`, then confirm "
        "with `pj-admin doctor`."
    )

    def assert_clean_failure(self, result) -> None:
        assert result.exit_code == 1, result.output
        assert self.STALE_MESSAGE in result.stderr
        assert self.STALE_REMEDY in result.stderr
        assert "Traceback" not in result.output
        assert "Traceback" not in result.stderr
        # The asyncpg exception must not escape as the command's result
        # either: `pj-admin` is a program, and an unhandled exception is a
        # crash however it is rendered.
        assert not isinstance(result.exception, asyncpg.PostgresError)

    async def test_workers_list_on_a_stale_database(self, db_params, scratch_db):
        name = await scratch_db(stale=True)

        result = await run_cli("--dsn", dsn_for(db_params, name), "workers", "list")

        self.assert_clean_failure(result)
        assert "job_threads" in result.stderr  # the underlying cause is kept

    async def test_jobs_tag_filter_on_a_stale_database(self, db_params, scratch_db):
        name = await scratch_db(stale=True)

        result = await run_cli(
            "--dsn", dsn_for(db_params, name), "jobs", "list", "--tag", "customer=acme"
        )

        self.assert_clean_failure(result)
        assert "tags" in result.stderr

    async def test_metrics_on_a_stale_database(self, db_params, scratch_db):
        name = await scratch_db(stale=True)

        result = await run_cli("--dsn", dsn_for(db_params, name), "metrics")

        self.assert_clean_failure(result)

    async def test_a_database_with_no_schema_at_all(self, db_params, scratch_db):
        """The same message, and deliberately so: "there is no schema" and
        "the schema is too old" have one remedy, and inventing a second
        vocabulary for them would only make the runbook longer."""
        name = await scratch_db()

        result = await run_cli("--dsn", dsn_for(db_params, name), "jobs", "list")

        self.assert_clean_failure(result)

    async def test_the_same_commands_work_once_migrated(self, db_params, scratch_db):
        """Control: the failures above are about the schema, and `db migrate`
        is genuinely the remedy the message names -- on an empty database it
        installs the whole base schema and every command starts working."""
        name = await scratch_db()
        dsn = dsn_for(db_params, name)

        migrated = await run_cli("--dsn", dsn, "db", "migrate")
        assert migrated.exit_code == 0, migrated.output
        assert "Installed base schema" in migrated.output

        for args in (["workers", "list"], ["metrics"], ["jobs", "list"]):
            result = await run_cli("--dsn", dsn, *args)
            assert result.exit_code == 0, f"{args}: {result.output}"


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
        # click's own wording, quoted since 8.2
        assert "Error: No such option '--nope'." in result.stderr

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
            "Error: Invalid value for '--since-hours': 'lots' is not a valid "
            "integer range." in result.stderr
        )

    @pytest.mark.parametrize(
        "command", [("metrics",), ("jobs", "retry-stats"), ("jobs", "timeout-stats")]
    )
    @pytest.mark.parametrize("value", ["-24", "0"])
    async def test_non_positive_since_hours_is_rejected(
        self, dsn, db_pool, unique_queue, command, value
    ):
        """A sign typo is a usage error, not a window that silently looks into
        the future and matches nothing."""
        await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, *command, "--since-hours", value, "--json")

        assert result.exit_code == 2
        assert (
            f"Error: Invalid value for '--since-hours': {value} is not in the "
            "range x>=1." in result.stderr
        )
        assert result.stdout == ""

    async def test_positive_since_hours_still_works(self, dsn, db_pool, unique_queue):
        """Control case: the range guard rejects the typo, not the flag."""
        await make_job(db_pool, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "metrics", "--since-hours", "24", "--json")

        assert result.exit_code == 0, result.output
        import json

        assert json.loads(result.stdout)["finished_count"] == 1


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

        # the request was accepted, so the exit code is still 0 ...
        assert result.exit_code == 0, result.output
        # ... but the wording never claims the job stopped: the API returned
        # 'cancel_requested' and the operator is told exactly that.
        assert f"Job {job_id}: cancellation requested" in result.output
        assert "the worker stops it at its next await point" in result.output
        assert f"Job {job_id} cancelled" not in result.output
        row = await db_pool.fetchrow(
            "SELECT state::text AS state, cancel_requested FROM jorb WHERE id = $1",
            job_id,
        )
        # still running, only flagged: the dead worker will never act on it
        assert row["state"] == "running"
        assert row["cancel_requested"] is True

    @pytest.mark.parametrize("state", ["queued", "waiting"])
    async def test_cancel_of_an_unclaimed_job_reports_a_real_cancellation(
        self, dsn, db_pool, unique_queue, state
    ):
        """Contrast case: nothing is holding a queued/waiting job, so it really
        is cancelled and the CLI says so without the warning."""
        job_id = await make_job(db_pool, unique_queue, state)

        result = await run_cli("--dsn", dsn, "jobs", "cancel", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"Job {job_id} cancelled" in result.output
        assert "cancellation requested" not in result.output
        assert (
            await db_pool.fetchval("SELECT state::text FROM jorb WHERE id = $1", job_id)
            == "cancelled"
        )
