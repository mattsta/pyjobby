"""Live-database tests for every branch of `pj-admin doctor`.

`doctor` is the 3am command: an operator runs it when something is wrong,
so every FAIL/WARN branch it can emit has to be exercised against a real
database, and its EXIT CODE has to be right (0 = healthy or warnings only,
1 = at least one FAIL). The happy path was the only covered case before
this file.

Branches that need a damaged schema (no schema at all, missing NOTIFY
triggers, un-writable database) run against throwaway databases created
with CREATE DATABASE and dropped in the fixture teardown; the rest run
against the session's test database, which the autouse
``ensure_clean_database`` fixture empties before every test so doctor's
global (un-filtered) queries are deterministic.

There is no --json mode on doctor, so assertions are made on the parsed
"STATUS name: message" check lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import migrations
from pyjobby.cli import DOCTOR_REQUIRED_TRIGGERS, cli
from tests.schema_fixtures import install_legacy_schema

pytestmark = pytest.mark.asyncio

STATUSES = ("PASS", "WARN", "FAIL")


def dsn_for(db_params: dict, database: str | None = None) -> str:
    """Build a DSN for `database` (default: the session's test database)."""
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}"
        f"/{database or db_params['database']}"
    )


@pytest.fixture
def dsn(db_params: dict) -> str:
    return dsn_for(db_params)


def parse_checks(output: str) -> dict[str, tuple[str, str]]:
    """Parse doctor's output into {check name: (status, message)}.

    click.echo strips the ANSI colors when stdout is not a terminal, so the
    lines arrive as plain "PASS database: connected".
    """
    checks: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        status, _, rest = line.partition(" ")
        if status not in STATUSES or ": " not in rest:
            continue
        name, _, message = rest.partition(": ")
        checks[name] = (status, message)
    return checks


async def run_doctor(dsn: str, *args: str):
    """Invoke `pj-admin doctor` against a real DSN in a worker thread.

    The CLI drives its own asyncio.run(), so it must not run on the test's
    event loop."""

    def _invoke():
        return CliRunner().invoke(cli, ["--dsn", dsn, "doctor", *args])

    return await asyncio.to_thread(_invoke)


@pytest_asyncio.fixture
async def scratch_db(db_params: dict):
    """Factory for throwaway databases; every one is dropped at teardown.

    Damaging a schema (dropping triggers, skipping the install) cannot be
    done to the shared test database, so these branches get their own.
    """
    admin = await asyncpg.connect(**db_params)
    created: list[str] = []

    async def _make(*, schema: bool = True, legacy: bool = False) -> str:
        """`legacy=True` installs the frozen pre-migration schema instead of
        the current one -- a database an older pyjobby release created, which
        is the shape doctor used to certify and then die on."""
        name = f"pj_doctor_{uuid.uuid4().hex[:12]}"
        await admin.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        if schema:
            conn = await asyncpg.connect(**{**db_params, "database": name})
            try:
                if legacy:
                    await install_legacy_schema(conn)
                else:
                    await migrations.migrate(conn)
            finally:
                await conn.close()
        return dsn_for(db_params, name)

    try:
        yield _make
    finally:
        for name in created:
            # best effort: a leaked scratch database must not fail the test
            with contextlib.suppress(asyncpg.PostgresError):
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


ScratchFactory = Callable[..., Awaitable[str]]


async def unused_port() -> int:
    """A localhost port with nothing listening on it."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


# ============================================================================
# Database reachability
# ============================================================================


class TestDoctorDatabaseReachability:
    async def test_closed_port_fails_and_stops_after_first_check(self, db_params):
        port = await unused_port()
        bad = (
            f"postgresql://{db_params['user']}:{db_params['password']}"
            f"@127.0.0.1:{port}/{db_params['database']}"
        )

        result = await run_doctor(bad)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        assert checks["database"] == ("FAIL", "unreachable")
        # nothing else is checkable without a connection
        assert list(checks) == ["database"]
        assert "Failed to connect to database" in result.stderr

    async def test_nonexistent_database_fails(self, db_params):
        missing = f"pj_absent_{uuid.uuid4().hex[:8]}"

        result = await run_doctor(dsn_for(db_params, missing))

        assert result.exit_code == 1, result.output
        assert parse_checks(result.output)["database"] == ("FAIL", "unreachable")
        assert f'database "{missing}" does not exist' in result.stderr

    async def test_reachable_database_passes(self, dsn):
        result = await run_doctor(dsn)

        assert parse_checks(result.output)["database"] == ("PASS", "connected")

    async def test_missing_config_file_fails_with_the_config_reason(self, tmp_path):
        """A config problem is reported AS a config problem.

        Blaming the database for an unreadable config file sends the operator
        to debug the wrong system, so the check line names config and the
        reason names the file and points at --config/--dsn."""
        missing = tmp_path / "absent.toml"

        def _invoke():
            return CliRunner().invoke(cli, ["--config", str(missing), "doctor"])

        result = await asyncio.to_thread(_invoke)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        assert checks["config"] == ("FAIL", "unusable")
        assert "database" not in checks
        assert list(checks) == ["config"]
        assert f"Error: Could not load config file: {missing}" in result.stderr
        assert f"Error: '{missing}' doesn't exist" in result.stderr
        assert (
            "Error: Use --config to point at a pyjobby conf file, or --dsn to "
            "connect directly." in result.stderr
        )
        assert "Failed to connect to database" not in result.stderr


# ============================================================================
# Schema / migrations
# ============================================================================


class TestDoctorSchema:
    async def test_empty_database_fails_and_skips_remaining_checks(
        self, scratch_db: ScratchFactory
    ):
        empty = await scratch_db(schema=False)

        result = await run_doctor(empty)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        assert checks["database"] == ("PASS", "connected")
        assert checks["schema"] == (
            "FAIL",
            "base schema not installed (run: pj-admin db migrate)",
        )
        # the command returns immediately: nothing else can be checked
        assert set(checks) == {"database", "schema"}

    async def test_fresh_install_reports_the_migrations_it_recorded(
        self, scratch_db: ScratchFactory
    ):
        """A fresh install carries every shipped migration by construction --
        schema.sql already contains their effects -- so it reports them as
        applied rather than as 'baseline'."""
        fresh = await scratch_db()
        versions = [m.version for m in migrations.available_migrations()]

        result = await run_doctor(fresh)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        assert checks["schema"] == (
            "PASS",
            f"installed, migrations current ({versions})",
        )
        assert checks["triggers"] == (
            "PASS",
            f"all schema triggers present ({len(DOCTOR_REQUIRED_TRIGGERS)})",
        )

    async def test_stale_database_fails_and_names_the_remedy(
        self, scratch_db: ScratchFactory
    ):
        """THE regression this whole check exists for.

        A database installed by an older release has `jorb`, so the old check
        ("is jorb there, is anything pending") reported PASS schema -- and the
        very next check died on `column "job_threads" does not exist`. The
        health probe certified a database it could not use. Now the check is
        the SHAPE, it FAILs, it names objects the operator can look up, and it
        names the command that fixes it.
        """
        stale = await scratch_db(legacy=True)

        result = await run_doctor(stale)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        status, message = checks["schema"]
        assert status == "FAIL"
        assert "object(s) this release needs are missing" in message
        # The message names the first few by name and counts the rest, so the
        # one asserted here has to be one that sorts near the front and will
        # stay there: jorb's own columns precede every other table's.
        assert "column jorb.tags" in message
        assert "run: pj-admin db migrate" in message
        # It stops there on purpose: every check below queries something this
        # one just reported missing, and doctor must not end in a traceback.
        assert set(checks) == {"database", "schema"}
        assert "Traceback" not in result.output + result.stderr

    async def test_a_stale_database_passes_once_it_is_migrated(
        self, scratch_db: ScratchFactory
    ):
        """The other half of the contract: FAIL has to be actionable, and the
        action is one documented command."""
        stale = await scratch_db(legacy=True)
        conn = await asyncpg.connect(stale)
        try:
            await migrations.migrate(conn)
        finally:
            await conn.close()

        result = await run_doctor(stale)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["schema"][0] == "PASS"

    async def test_a_single_dropped_object_is_enough_to_fail(
        self, scratch_db: ScratchFactory
    ):
        """The shape check is not a proxy for "was migrate ever run" -- it is
        a statement about the objects the running code addresses, so one of
        them going missing is a FAIL even on a perfectly tracked database."""
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            await conn.execute("ALTER TABLE jorb DROP COLUMN tags")
        finally:
            await conn.close()

        result = await run_doctor(damaged)

        assert result.exit_code == 1, result.output
        status, message = parse_checks(result.output)["schema"]
        assert status == "FAIL"
        assert "column jorb.tags" in message

    async def test_unrecorded_but_complete_schema_passes_and_says_so(
        self, scratch_db: ScratchFactory
    ):
        """A database installed from the CURRENT schema.sql by a release that
        did not record migrations: every object is present, so it runs the
        code correctly and doctor must not page anyone -- but the record is
        what the next upgrade reads, so it does not go silently either.
        """
        installed = await scratch_db()
        conn = await asyncpg.connect(installed)
        try:
            await conn.execute("DELETE FROM schema_migrations")
        finally:
            await conn.close()

        result = await run_doctor(installed)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        status, message = checks["schema"]
        assert status == "PASS"
        assert "are not recorded yet" in message
        assert "run: pj-admin db migrate" in message
        # and unlike a FAIL it does not stop the report
        assert "dlq" in checks

    async def test_applied_versions_are_listed(self, scratch_db: ScratchFactory):
        installed = await scratch_db()
        conn = await asyncpg.connect(installed)
        try:
            # a version this release does not ship, recorded by a newer one
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) "
                "VALUES (99, '099_from_the_future.sql')"
            )
        finally:
            await conn.close()

        result = await run_doctor(installed)

        assert result.exit_code == 0, result.output
        versions = [m.version for m in migrations.available_migrations()]
        assert parse_checks(result.output)["schema"] == (
            "PASS",
            f"installed, migrations current ({sorted([*versions, 99])})",
        )


# ============================================================================
# Triggers
# ============================================================================

#: Which table each required trigger lives on, so a test can drop them all.
#: Kept here rather than in the manifest: doctor looks triggers up by name
#: across the whole schema, and only a test needs to name their tables.
TRIGGER_TABLES = {
    "jorb_enqueued_notify": "jorb",
    "jorb_done_notify": "jorb",
    "jorb_cancel_notify": "jorb",
    "jorb_history_record": "jorb",
    "jorb_dag_complete": "jorb",
    "jorb_event_notify": "jorb_event",
    "schedule_executed_notify": "jorb_schedule_log",
}


class TestDoctorTriggers:
    async def test_every_required_trigger_is_covered_by_this_file(self):
        """The drop-them-all test below is only exhaustive if this mapping is:
        a trigger added to the schema without a table here would silently stop
        being exercised."""
        assert set(TRIGGER_TABLES) == set(DOCTOR_REQUIRED_TRIGGERS)

    async def test_one_missing_trigger_fails_by_name(self, scratch_db: ScratchFactory):
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            await conn.execute("DROP TRIGGER jorb_enqueued_notify ON jorb")
        finally:
            await conn.close()

        result = await run_doctor(damaged)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        assert checks["triggers"] == (
            "FAIL",
            "missing triggers: jorb_enqueued_notify (run: pj-admin db migrate)",
        )
        # a trigger FAIL must not stop the remaining checks
        assert checks["workers"][0] == "WARN"
        assert checks["dlq"] == ("PASS", "empty")

    async def test_all_missing_triggers_are_named(self, scratch_db: ScratchFactory):
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            for trigger, table in TRIGGER_TABLES.items():
                await conn.execute(f"DROP TRIGGER {trigger} ON {table}")
        finally:
            await conn.close()

        result = await run_doctor(damaged)

        assert result.exit_code == 1, result.output
        assert parse_checks(result.output)["triggers"] == (
            "FAIL",
            "missing triggers: "
            # reported in required order
            + ", ".join(DOCTOR_REQUIRED_TRIGGERS)
            + " (run: pj-admin db migrate)",
        )


# ============================================================================
# Live workers
# ============================================================================


async def insert_worker(
    pool,
    queue: str,
    *,
    last_seen_age: timedelta = timedelta(0),
    shutdown: bool = False,
) -> int:
    return await pool.fetchval(
        """INSERT INTO jorb_worker (host, pid, queue, last_seen, shutdown_at)
           VALUES ('doctor-test', 4242, $1, now() - $2::interval,
                   CASE WHEN $3 THEN now() ELSE NULL END)
           RETURNING id""",
        queue,
        last_seen_age,
        shutdown,
    )


class TestDoctorWorkers:
    async def test_no_workers_warns_and_exit_stays_zero(self, dsn, db_pool):
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb_worker") == 0

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["workers"] == (
            "WARN",
            "no live workers seen in last 60s",
        )
        assert "FAIL" not in result.output

    async def test_fresh_heartbeat_passes(self, dsn, db_pool, unique_queue):
        await insert_worker(db_pool, unique_queue)

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["workers"] == (
            "PASS",
            "1 live worker(s) seen in last 60s",
        )

    async def test_heartbeat_older_than_60s_still_warns(
        self, dsn, db_pool, unique_queue
    ):
        await insert_worker(db_pool, unique_queue, last_seen_age=timedelta(minutes=5))

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["workers"] == (
            "WARN",
            "no live workers seen in last 60s",
        )
        assert result.exit_code == 0, result.output

    async def test_shut_down_worker_does_not_count_as_live(
        self, dsn, db_pool, unique_queue
    ):
        await insert_worker(db_pool, unique_queue, shutdown=True)

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["workers"] == (
            "WARN",
            "no live workers seen in last 60s",
        )

    async def test_real_live_worker_passes(self, dsn, live_worker, db_pool):
        """The registry row a REAL worker writes satisfies the check."""
        await live_worker()
        assert await db_pool.fetchval(
            "SELECT COUNT(*) FROM jorb_worker WHERE shutdown_at IS NULL"
        )

        result = await asyncio.wait_for(run_doctor(dsn), timeout=30)

        assert result.exit_code == 0, result.output
        status, message = parse_checks(result.output)["workers"]
        assert status == "PASS"
        assert message.endswith("live worker(s) seen in last 60s")


# ============================================================================
# Queue backlogs (depth / age thresholds)
# ============================================================================


async def insert_queued(
    pool, queue: str, count: int = 1, *, run_after_age: timedelta = timedelta(0)
) -> None:
    for _ in range(count):
        await pool.execute(
            """INSERT INTO jorb (job_class, queue, state, run_after)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'queued', now() - $2::interval)""",
            queue,
            run_after_age,
        )


class TestDoctorQueueBacklog:
    async def test_no_queued_jobs_passes(self, dsn, db_pool, unique_queue):
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'finished')""",
            unique_queue,
        )

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        assert checks["queues"] == ("PASS", "no queued jobs")
        assert f"queue {unique_queue}" not in checks

    async def test_depth_threshold_is_exclusive_on_both_sides(
        self, dsn, db_pool, unique_queue
    ):
        await insert_queued(db_pool, unique_queue, count=3)
        key = f"queue {unique_queue}"

        at_threshold = await run_doctor(dsn, "--max-depth", "3")
        assert at_threshold.exit_code == 0, at_threshold.output
        assert parse_checks(at_threshold.output)[key] == (
            "PASS",
            "depth 3, oldest runnable 0m",
        )

        over_threshold = await run_doctor(dsn, "--max-depth", "2")
        assert over_threshold.exit_code == 0, over_threshold.output
        assert parse_checks(over_threshold.output)[key] == (
            "WARN",
            "depth 3, oldest runnable 0m (thresholds: depth 2, age 60m)",
        )
        # a backlog is a warning, never a failure
        assert "FAIL" not in over_threshold.output

    async def test_age_threshold_is_respected_on_both_sides(
        self, dsn, db_pool, unique_queue
    ):
        await insert_queued(db_pool, unique_queue, run_after_age=timedelta(minutes=90))
        key = f"queue {unique_queue}"

        under = await run_doctor(dsn, "--max-age-minutes", "91")
        assert under.exit_code == 0, under.output
        assert parse_checks(under.output)[key] == (
            "PASS",
            "depth 1, oldest runnable 90m",
        )

        over = await run_doctor(dsn, "--max-age-minutes", "89")
        assert over.exit_code == 0, over.output
        assert parse_checks(over.output)[key] == (
            "WARN",
            "depth 1, oldest runnable 90m (thresholds: depth 10000, age 89m)",
        )

    async def test_deferred_work_is_information_not_backlog(
        self, dsn, db_pool, unique_queue
    ):
        """A job whose run_after is in the future is deliberately deferred
        (retry backoff, a scheduled batch) — counting it as backlog tripped
        the WARN operators alert on while nothing was wrong, and its future
        run_after dragged the age negative. It is reported, separately, and
        alarms nothing."""
        await insert_queued(db_pool, unique_queue, run_after_age=timedelta(minutes=-10))

        result = await run_doctor(dsn, "--max-depth", "0", "--max-age-minutes", "1")

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)[f"queue {unique_queue}"] == (
            "PASS",
            "depth 0, oldest runnable 0m, 1 deferred to the future",
        )

    async def test_a_retry_backoff_storm_does_not_trip_the_backlog_warn(
        self, dsn, db_pool, unique_queue
    ):
        """Many jobs parked on retry backoff must read as deferred, not as a
        depth alarm."""
        await insert_queued(
            db_pool, unique_queue, count=50, run_after_age=timedelta(minutes=-5)
        )

        result = await run_doctor(dsn, "--max-depth", "10")

        assert result.exit_code == 0, result.output
        status, summary = parse_checks(result.output)[f"queue {unique_queue}"]
        assert status == "PASS"
        assert "50 deferred to the future" in summary

    async def test_each_backlogged_queue_reports_separately(
        self, dsn, db_pool, unique_queue
    ):
        other = f"{unique_queue}_b"
        await insert_queued(db_pool, unique_queue, count=2)
        await insert_queued(db_pool, other, count=1)

        result = await run_doctor(dsn, "--max-depth", "1")

        checks = parse_checks(result.output)
        assert checks[f"queue {unique_queue}"][0] == "WARN"
        assert checks[f"queue {other}"] == ("PASS", "depth 1, oldest runnable 0m")
        assert "queues" not in checks  # the "no queued jobs" line is suppressed


class TestDoctorBlockedWaiters:
    async def test_a_waiter_on_a_crashed_upstream_warns(
        self, dsn, db_pool, unique_queue
    ):
        """The monitor deliberately leaves these parked (the upstream is
        retryable); doctor is the ONLY place the condition is visible."""
        upstream = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'crashed') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, waitfor_job)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'waiting', $2)""",
            unique_queue,
            upstream,
        )

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        status, summary = parse_checks(result.output)["blocked-waiters"]
        assert status == "WARN"
        assert "blocked on a crashed/cancelled upstream" in summary

    async def test_a_group_waiter_with_a_crashed_member_warns(
        self, dsn, db_pool, unique_queue
    ):
        """A group whose members all exist never trips the unsatisfiable
        sweep, and never gets woken while one member is crashed -- so a group
        waiter is stranded just like a job waiter, and doctor must surface it
        the same way."""
        leader = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'crashed') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", leader
        )
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, waitfor_group)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'waiting', $2)""",
            unique_queue,
            leader,
        )

        result = await run_doctor(dsn)

        status, summary = parse_checks(result.output)["blocked-waiters"]
        assert status == "WARN"
        assert "blocked on a crashed/cancelled upstream" in summary

    async def test_ordinary_waiters_do_not_warn(self, dsn, db_pool, unique_queue):
        upstream = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, waitfor_job)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'waiting', $2)""",
            unique_queue,
            upstream,
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["blocked-waiters"][0] == "PASS"


class TestDoctorMailbox:
    async def test_stale_unread_mail_warns(self, dsn, db_pool, unique_queue):
        """The platform never deletes unconsumed mail of a live job (durable
        delivery is the contract), so aging unread mail is the one signal
        that a sender is using a topic nothing recv()s."""
        dest = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            """INSERT INTO jorb_mailbox (dest_job_id, topic, message, created)
               VALUES ($1, 'nobody-reads-this', '{}', now() - interval '2 days')""",
            dest,
        )

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        status, summary = parse_checks(result.output)["mailbox"]
        assert status == "WARN"
        assert "unread mailbox message" in summary

    async def test_fresh_unread_mail_passes(self, dsn, db_pool, unique_queue):
        dest = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            """INSERT INTO jorb_mailbox (dest_job_id, topic, message)
               VALUES ($1, 'about-to-be-read', '{}')""",
            dest,
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["mailbox"][0] == "PASS"


# ============================================================================
# Dead letter queue
# ============================================================================


class TestDoctorDLQ:
    async def test_crashed_job_warns(self, dsn, db_pool, unique_queue):
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, error_message, error_count)
               VALUES ('tests.dxe_jobs.FailJob', $1, 'crashed', 'boom', 3)""",
            unique_queue,
        )

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["dlq"] == (
            "WARN",
            "1 dead-lettered job(s) (inspect: pj-admin dlq list)",
        )
        assert "FAIL" not in result.output

    async def test_empty_dlq_passes(self, dsn, db_pool, unique_queue):
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'finished')""",
            unique_queue,
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["dlq"] == ("PASS", "empty")

    async def test_dlq_count_from_a_real_crashed_job(
        self, dsn, db_pool, live_worker, unique_queue
    ):
        """A job the REAL worker dead-letters is what doctor counts."""
        from .conftest import wait_for_job_state

        await live_worker(max_retries=0)
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, kwargs, admin_data)
               VALUES ('tests.dxe_jobs.FailJob', $1, '{}', '{"max_retries": 0}')
               RETURNING id""",
            unique_queue,
        )
        await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)

        result = await asyncio.wait_for(run_doctor(dsn), timeout=30)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["dlq"] == (
            "WARN",
            "1 dead-lettered job(s) (inspect: pj-admin dlq list)",
        )


# ============================================================================
# Overdue schedules
# ============================================================================


async def insert_schedule(
    pool, name: str, *, next_run_age: timedelta, enabled: bool = True
) -> int:
    return await pool.fetchval(
        """INSERT INTO jorb_schedule
               (name, job_class, cron_expr, enabled, next_run)
           VALUES ($1, 'tests.dxe_jobs.OkJob', '*/5 * * * *', $2,
                   now() - $3::interval)
           RETURNING id""",
        name,
        enabled,
        next_run_age,
    )


class TestDoctorSchedules:
    async def test_overdue_enabled_schedule_warns(self, dsn, db_pool, test_id):
        await insert_schedule(db_pool, test_id, next_run_age=timedelta(minutes=10))

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["schedules"] == (
            "WARN",
            "1 enabled schedule(s) overdue by >5m (is pj-scheduler running?)",
        )

    async def test_disabled_overdue_schedule_is_ignored(self, dsn, db_pool, test_id):
        await insert_schedule(
            db_pool, test_id, next_run_age=timedelta(days=1), enabled=False
        )

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["schedules"] == (
            "PASS",
            "no overdue schedules",
        )

    async def test_within_five_minute_grace_period_passes(self, dsn, db_pool, test_id):
        await insert_schedule(db_pool, test_id, next_run_age=timedelta(minutes=1))

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["schedules"] == (
            "PASS",
            "no overdue schedules",
        )

    async def test_overdue_schedules_are_counted(self, dsn, db_pool, test_id):
        await insert_schedule(db_pool, f"{test_id}_a", next_run_age=timedelta(hours=1))
        await insert_schedule(db_pool, f"{test_id}_b", next_run_age=timedelta(hours=2))
        await insert_schedule(
            db_pool, f"{test_id}_c", next_run_age=timedelta(hours=3), enabled=False
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["schedules"] == (
            "WARN",
            "2 enabled schedule(s) overdue by >5m (is pj-scheduler running?)",
        )


# ============================================================================
# Exit code composition
# ============================================================================


class TestDoctorExitCode:
    async def test_many_warnings_still_exit_zero(self, dsn, db_pool, unique_queue):
        await insert_queued(db_pool, unique_queue, count=2)
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, error_message)
               VALUES ('tests.dxe_jobs.FailJob', $1, 'crashed', 'boom')""",
            unique_queue,
        )
        await insert_schedule(db_pool, unique_queue, next_run_age=timedelta(hours=1))

        result = await run_doctor(dsn, "--max-depth", "1")

        statuses = {name: s for name, (s, _) in parse_checks(result.output).items()}
        assert statuses[f"queue {unique_queue}"] == "WARN"
        assert statuses["dlq"] == "WARN"
        assert statuses["schedules"] == "WARN"
        assert statuses["workers"] == "WARN"
        assert "FAIL" not in result.output
        assert result.exit_code == 0, result.output

    async def test_one_fail_beats_all_passes_and_warns(
        self, scratch_db: ScratchFactory
    ):
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            await conn.execute("DROP TRIGGER jorb_done_notify ON jorb")
            await conn.execute(
                """INSERT INTO jorb (job_class, queue, state, error_message)
                   VALUES ('tests.dxe_jobs.FailJob', 'default', 'crashed', 'boom')"""
            )
        finally:
            await conn.close()

        result = await run_doctor(damaged)

        checks = parse_checks(result.output)
        assert checks["database"] == ("PASS", "connected")
        assert checks["triggers"] == (
            "FAIL",
            "missing triggers: jorb_done_notify (run: pj-admin db migrate)",
        )
        assert checks["dlq"][0] == "WARN"
        assert result.exit_code == 1, result.output
