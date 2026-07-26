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
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import migrations
from pyjobby.cli import DOCTOR_REQUIRED_TRIGGERS, cli

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

    async def _make(*, schema: bool = True) -> str:
        name = f"pj_doctor_{uuid.uuid4().hex[:12]}"
        await admin.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        if schema:
            conn = await asyncpg.connect(**{**db_params, "database": name})
            try:
                await migrations.migrate(conn)
            finally:
                await conn.close()
        return dsn_for(db_params, name)

    try:
        yield _make
    finally:
        for name in created:
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except asyncpg.PostgresError:  # pragma: no cover - best effort cleanup
                pass
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

    async def test_untracked_install_reports_baseline(
        self, scratch_db: ScratchFactory, db_params
    ):
        """Schema present with an empty schema_migrations table -> 'baseline'."""
        fresh = await scratch_db()

        result = await run_doctor(fresh)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        assert checks["schema"] == ("PASS", "installed, migrations current (baseline)")
        assert checks["triggers"] == (
            "PASS",
            f"all NOTIFY triggers present ({len(DOCTOR_REQUIRED_TRIGGERS)})",
        )

    async def test_applied_versions_are_listed(self, scratch_db: ScratchFactory):
        installed = await scratch_db()
        conn = await asyncpg.connect(installed)
        try:
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (1, '001_x.sql')"
            )
        finally:
            await conn.close()

        result = await run_doctor(installed)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["schema"] == (
            "PASS",
            "installed, migrations current ([1])",
        )

    async def test_pending_migrations_branch_is_unreachable_at_schema_v1(
        self, scratch_db: ScratchFactory
    ):
        """Documents why doctor's 'pending migrations' FAIL has no test.

        `pending` is (files shipped in pyjobby/sql/migrations) minus (rows in
        schema_migrations). Schema v1 ships zero migration files, so pending
        is always empty and the FAIL branch cannot be reached without adding
        a migration file to the package. When the first migration lands this
        test fails, which is the reminder to cover that branch.
        """
        assert migrations.available_migrations() == []

        installed = await scratch_db()
        conn = await asyncpg.connect(installed)
        try:
            info = await migrations.status(conn)
        finally:
            await conn.close()

        assert info["base_schema_installed"] is True
        assert info["pending"] == []


# ============================================================================
# NOTIFY triggers
# ============================================================================


class TestDoctorTriggers:
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
            "missing NOTIFY triggers: jorb_enqueued_notify",
        )
        # a trigger FAIL must not stop the remaining checks
        assert checks["workers"][0] == "WARN"
        assert checks["dlq"] == ("PASS", "empty")

    async def test_all_missing_triggers_are_named(self, scratch_db: ScratchFactory):
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            for trigger in DOCTOR_REQUIRED_TRIGGERS:
                await conn.execute(f"DROP TRIGGER {trigger} ON jorb")
        finally:
            await conn.close()

        result = await run_doctor(damaged)

        assert result.exit_code == 1, result.output
        assert parse_checks(result.output)["triggers"] == (
            "FAIL",
            "missing NOTIFY triggers: "
            + ", ".join(DOCTOR_REQUIRED_TRIGGERS),  # reported in required order
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
            "depth 3, oldest queued 0m",
        )

        over_threshold = await run_doctor(dsn, "--max-depth", "2")
        assert over_threshold.exit_code == 0, over_threshold.output
        assert parse_checks(over_threshold.output)[key] == (
            "WARN",
            "depth 3, oldest queued 0m (thresholds: depth 2, age 60m)",
        )
        # a backlog is a warning, never a failure
        assert "FAIL" not in over_threshold.output

    async def test_age_threshold_is_respected_on_both_sides(
        self, dsn, db_pool, unique_queue
    ):
        await insert_queued(
            db_pool, unique_queue, run_after_age=timedelta(minutes=90)
        )
        key = f"queue {unique_queue}"

        under = await run_doctor(dsn, "--max-age-minutes", "91")
        assert under.exit_code == 0, under.output
        assert parse_checks(under.output)[key] == (
            "PASS",
            "depth 1, oldest queued 90m",
        )

        over = await run_doctor(dsn, "--max-age-minutes", "89")
        assert over.exit_code == 0, over.output
        assert parse_checks(over.output)[key] == (
            "WARN",
            "depth 1, oldest queued 90m (thresholds: depth 10000, age 89m)",
        )

    async def test_future_run_after_is_clamped_to_zero_minutes(
        self, dsn, db_pool, unique_queue
    ):
        """A delayed/retry-waiting job must not report a negative age."""
        await insert_queued(
            db_pool, unique_queue, run_after_age=timedelta(minutes=-10)
        )

        result = await run_doctor(dsn, "--max-age-minutes", "1")

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)[f"queue {unique_queue}"] == (
            "PASS",
            "depth 1, oldest queued 0m",
        )

    async def test_each_backlogged_queue_reports_separately(
        self, dsn, db_pool, unique_queue
    ):
        other = f"{unique_queue}_b"
        await insert_queued(db_pool, unique_queue, count=2)
        await insert_queued(db_pool, other, count=1)

        result = await run_doctor(dsn, "--max-depth", "1")

        checks = parse_checks(result.output)
        assert checks[f"queue {unique_queue}"][0] == "WARN"
        assert checks[f"queue {other}"] == ("PASS", "depth 1, oldest queued 0m")
        assert "queues" not in checks  # the "no queued jobs" line is suppressed


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
            "missing NOTIFY triggers: jorb_done_notify",
        )
        assert checks["dlq"][0] == "WARN"
        assert result.exit_code == 1, result.output
