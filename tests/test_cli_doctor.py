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

Assertions are made on the parsed "STATUS name: message" check lines; the
--json mode emits the SAME records (see TestDoctorJson), so there is one
enumeration of the checks, not two.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import migrations
from pyjobby.cli import DOCTOR_REQUIRED_TRIGGERS, cli
from pyjobby.client import DEFAULT_PRIO_CEILING
from tests.conftest import reserved_unused_port
from tests.schema_fixtures import drop_database

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
            # best effort: a leaked scratch database must not fail the test
            with contextlib.suppress(asyncpg.PostgresError):
                await drop_database(admin, name)
        await admin.close()


ScratchFactory = Callable[..., Awaitable[str]]


# ============================================================================
# Database reachability
# ============================================================================


class TestDoctorDatabaseReachability:
    async def test_closed_port_fails_and_stops_after_first_check(self, db_params):
        # held for the whole check: a port merely sampled and released can be
        # taken by another xdist worker before doctor dials it
        with reserved_unused_port() as port:
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

    async def test_fresh_install_passes_at_baseline(self, scratch_db: ScratchFactory):
        """Pre-live no migration files ship, so a fresh install has recorded
        nothing and reports the baseline."""
        fresh = await scratch_db()

        result = await run_doctor(fresh)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        assert checks["schema"] == (
            "PASS",
            "installed, migrations current (baseline)",
        )
        assert checks["triggers"] == (
            "PASS",
            f"all schema triggers present ({len(DOCTOR_REQUIRED_TRIGGERS)})",
        )

    async def test_drifted_database_fails_and_names_the_missing_objects(
        self, scratch_db: ScratchFactory
    ):
        """THE regression this whole check exists for.

        A database at a different shape still has `jorb`, so a presence-only
        check ("is jorb there") reported PASS schema -- and the very next
        check died on the missing column. The health probe certified a
        database it could not use. Now the check is the SHAPE, it FAILs, it
        names objects the operator can look up, and it prescribes the remedy
        that is actually true for the drift it found: columns and functions
        are not something `db migrate` can recreate, so the line must say
        recreate-or-reconcile rather than send the operator to a command
        that will refuse.
        """
        drifted = await scratch_db()
        conn = await asyncpg.connect(drifted)
        try:
            await conn.execute("ALTER TABLE jorb DROP COLUMN tags")
            await conn.execute("ALTER TABLE jorb_worker DROP COLUMN job_threads")
            await conn.execute("DROP FUNCTION claim_jorb")
        finally:
            await conn.close()

        result = await run_doctor(drifted)

        assert result.exit_code == 1, result.output
        checks = parse_checks(result.output)
        status, message = checks["schema"]
        assert status == "FAIL"
        assert "object(s) this release needs are missing" in message
        # The message names the first few by name and counts the rest, so the
        # one asserted here has to be one that sorts near the front and will
        # stay there: jorb's own columns precede every other table's.
        assert "column jorb.tags" in message
        assert "run: pj-admin db migrate" not in message
        assert "recreate the database or reconcile by hand" in message
        # It stops there on purpose: every check below queries something this
        # one just reported missing, and doctor must not end in a traceback.
        assert set(checks) == {"database", "schema"}
        assert "Traceback" not in result.output + result.stderr

    async def test_an_empty_database_passes_once_installed(
        self, scratch_db: ScratchFactory
    ):
        """The other half of the contract: FAIL has to be actionable, and the
        action is one documented command."""
        empty = await scratch_db(schema=False)
        conn = await asyncpg.connect(empty)
        try:
            await migrations.migrate(conn)
        finally:
            await conn.close()

        result = await run_doctor(empty)

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
        self, monkeypatch, scratch_db: ScratchFactory
    ):
        """The post-live branch, exercised with a SYNTHETIC migration: every
        object a pending file installs is already present (the base schema
        contains it), so the database runs the code correctly and doctor must
        not page anyone -- but the record is what the next upgrade reads, so
        it does not go silently either."""
        installed = await scratch_db()
        monkeypatch.setattr(
            migrations,
            "available_migrations",
            lambda: [
                migrations.Migration(
                    version=1, name="001_synthetic.sql", sql="SELECT 1"
                )
            ],
        )

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
        assert parse_checks(result.output)["schema"] == (
            "PASS",
            "installed, migrations current ([99])",
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
    "jorb_stream_notify": "jorb_stream",
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
    max_prio: int = DEFAULT_PRIO_CEILING,
    capabilities: tuple[str, ...] = (),
    app_version: str | None = None,
) -> int:
    """Register a worker row directly, as pj.py's WORKER_REGISTER_SQL does.

    `max_prio`, `capabilities` and `app_version` are the whole of what a worker
    will accept, so they are what the `unclaimable` check reads; starting a real
    worker to publish three columns it writes once at registration buys nothing
    and costs a process per case.
    """
    return await pool.fetchval(
        """INSERT INTO jorb_worker
               (host, pid, queue, last_seen, shutdown_at, max_prio, capabilities,
                app_version)
           VALUES ('doctor-test', 4242, $1, now() - $2::interval,
                   CASE WHEN $3 THEN now() ELSE NULL END, $4, $5, $6)
           RETURNING id""",
        queue,
        last_seen_age,
        shutdown,
        max_prio,
        list(capabilities),
        app_version,
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
    pool,
    queue: str,
    count: int = 1,
    *,
    run_after_age: timedelta = timedelta(0),
    prio: int = 100,
    capability: str | None = None,
    app_version: str | None = None,
) -> list[int]:
    """Insert `count` queued jobs; returns their ids in insertion order."""
    return [
        await pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, run_after, prio,
                                 capability, app_version)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'queued', now() - $2::interval,
                       $3, $4, $5)
               RETURNING id""",
            queue,
            run_after_age,
            prio,
            capability,
            app_version,
        )
        for _ in range(count)
    ]


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


# ============================================================================
# Queue controls that promise something they cannot deliver
# ============================================================================


class TestDoctorPartitionLimits:
    """`partition_limits` is the one queue-control setting that does nothing
    on its own.

    It does not CAP anything -- it re-scopes the caps, counting
    `max_concurrency` and `rate_limit` per distinct `partition_key` instead of
    per queue. With neither set there is nothing to re-scope, so the flag is
    on, `queues show` prints `Partition Limits: yes` (truthfully, about the
    column), the operator believes their tenants are isolated, and every lane
    is unlimited. Nothing else in the platform says so.
    """

    async def _queue(self, pool, name: str, **control) -> None:
        await pool.execute(
            """INSERT INTO jorb_queue
                   (name, partition_limits, max_concurrency, rate_limit)
               VALUES ($1, TRUE, $2, $3)""",
            name,
            control.get("max_concurrency"),
            control.get("rate_limit"),
        )

    async def test_a_queue_with_no_limit_to_scope_warns(
        self, dsn, db_pool, unique_queue
    ):
        await self._queue(db_pool, unique_queue)

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output  # a config problem, not a FAIL
        status, message = parse_checks(result.output)["partition-limits"]
        assert status == "WARN"
        assert unique_queue in message
        assert "nothing to re-scope" in message
        assert "--max-concurrency" in message

    @pytest.mark.parametrize(
        "control",
        [{"max_concurrency": 4}, {"rate_limit": 10}],
        ids=["max-concurrency", "rate-limit"],
    )
    async def test_either_limit_is_enough_to_scope(
        self, dsn, db_pool, unique_queue, control
    ):
        """Both limits are re-scoped by the flag, so either one on its own
        gives it something to do."""
        await self._queue(db_pool, unique_queue, **control)

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["partition-limits"] == (
            "PASS",
            "every queue with partition_limits has a limit to scope",
        )

    async def test_a_limited_queue_without_the_flag_is_not_this_checks_business(
        self, dsn, db_pool, unique_queue
    ):
        """The inverse is a perfectly ordinary queue-wide cap, which is the
        default and the common case."""
        await db_pool.execute(
            """INSERT INTO jorb_queue (name, partition_limits, max_concurrency)
               VALUES ($1, FALSE, 4)""",
            unique_queue,
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["partition-limits"][0] == "PASS"


class TestDoctorBackfill:
    """A schedule's own concurrency cap silently swallowing its backfill.

    `backfill_limit` and `max_concurrent_jobs` interact invisibly: a
    backfilled tick fires through the SAME path an on-time one does, so the
    cap refuses it identically, and the currently-due tick has already taken
    a slot -- landing N backfilled ticks needs `max_concurrent_jobs` of N + 1.
    At the default cap of 1, ANY `backfill_limit` catches up on nothing and
    the feature is a no-op nobody is told about.

    `pj-admin schedule add` warns at creation, but it is the only door that
    does: the web admin form, `AdminAPI.create_schedule` and
    `Scheduler.create_schedule` all reach the same table without passing that
    code, so for those schedules this check is the ONLY warning there is.
    """

    async def _schedule(
        self,
        pool,
        name: str,
        *,
        backfill_limit: int,
        max_concurrent_jobs: int,
        enabled: bool = True,
    ) -> None:
        await pool.execute(
            """INSERT INTO jorb_schedule
                   (name, job_class, cron_expr, next_run, enabled,
                    backfill_limit, max_concurrent_jobs)
               VALUES ($1, 'tests.dxe_jobs.OkJob', '*/5 * * * *',
                       now() + interval '1 hour', $2, $3, $4)""",
            name,
            enabled,
            backfill_limit,
            max_concurrent_jobs,
        )

    async def test_the_default_cap_makes_any_backfill_inert(
        self, dsn, db_pool, test_id
    ):
        """The demonstrated no-op: max_concurrent_jobs 1 is the default, so a
        schedule asking for backfill out of the box catches up on nothing."""
        await self._schedule(db_pool, test_id, backfill_limit=3, max_concurrent_jobs=1)

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output  # a config problem, not a FAIL
        status, message = parse_checks(result.output)["backfill"]
        assert status == "WARN"
        assert (
            f"1 enabled schedule(s) whose backfill cannot land in full: "
            f"{test_id} (backfill_limit 3 needs max_concurrent_jobs 4, has 1: "
            f"0 tick(s) can fire)." in message
        )
        # the arithmetic, and both ways out
        assert "the currently-due tick already occupies one slot" in message
        assert "Raise max_concurrent_jobs to backfill_limit + 1" in message

    async def test_a_cap_that_lands_only_part_of_the_backfill_still_warns(
        self, dsn, db_pool, test_id
    ):
        """Partly inert is still inert: the operator asked for 3 and gets 1,
        and the message says which number is real."""
        await self._schedule(db_pool, test_id, backfill_limit=3, max_concurrent_jobs=2)

        result = await run_doctor(dsn)

        status, message = parse_checks(result.output)["backfill"]
        assert status == "WARN"
        assert "backfill_limit 3 needs max_concurrent_jobs 4, has 2: 1 tick(s)" in (
            message
        )
        # what survives the cap is the freshest, so the operator knows what was lost
        assert "Fires go newest-first, so the ticks lost are the oldest" in message

    async def test_a_cap_with_room_for_the_whole_backfill_passes(
        self, dsn, db_pool, test_id
    ):
        """backfill_limit + 1 is the exact requirement, not a margin: 3 + the
        currently-due tick is 4, and 4 is enough."""
        await self._schedule(db_pool, test_id, backfill_limit=3, max_concurrent_jobs=4)

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["backfill"] == (
            "PASS",
            "every enabled schedule's backfill_limit fits its max_concurrent_jobs",
        )

    async def test_a_schedule_that_never_backfills_is_not_this_checks_business(
        self, dsn, db_pool, test_id
    ):
        """backfill_limit 0 is the default and means "skip what you missed".
        Nothing is inert, because nothing was asked for."""
        await self._schedule(db_pool, test_id, backfill_limit=0, max_concurrent_jobs=1)

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["backfill"][0] == "PASS"

    async def test_a_disabled_schedule_is_ignored(self, dsn, db_pool, test_id):
        """A schedule that is not firing is not misconfigured yet -- the same
        rule the overdue-schedules check applies."""
        await self._schedule(
            db_pool,
            test_id,
            backfill_limit=5,
            max_concurrent_jobs=1,
            enabled=False,
        )

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["backfill"][0] == "PASS"

    async def test_the_check_reaches_json_like_every_other(self, dsn, db_pool, test_id):
        """A schedule created through the web or the API is exactly the case
        this check exists for, and a CI scrape must see it."""
        await self._schedule(db_pool, test_id, backfill_limit=2, max_concurrent_jobs=1)

        result = await run_doctor(dsn, "--json")

        record = next(r for r in json.loads(result.stdout) if r["check"] == "backfill")
        assert record["status"] == "WARN"
        assert test_id in record["message"]


# ============================================================================
# Work no live worker can claim
# ============================================================================
# The proactive half of `pj-admin jobs why ID`. Both ways into the condition
# need a worker whose registry row says what it will accept (max_prio,
# capabilities), which is why they are built with insert_worker rather than a
# real worker: those two columns are written once at registration and never
# again, so a live process publishes nothing a row cannot.


class TestDoctorUnclaimable:
    async def test_a_job_above_every_live_ceiling_warns(
        self, dsn, db_pool, unique_queue
    ):
        """The silent failure: healthy row, healthy fleet, nobody can see it.
        It never fails and never reaches the DLQ, so this line is the only
        warning there is."""
        await insert_worker(db_pool, unique_queue, max_prio=10)
        (job,) = await insert_queued(db_pool, unique_queue, prio=900)

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output  # a workload problem, not a FAIL
        status, message = parse_checks(result.output)["unclaimable"]
        assert status == "WARN"
        assert message.startswith(
            "1 claimable job(s) that no live worker can claim: 1 on "
            f"{unique_queue!r} above every live worker's ceiling (prio 900; "
            "the highest --max-prio among 1 live worker(s) is 10; "
            f"e.g. jobs {job})."
        )
        # the remedy, and where the per-job detail lives
        assert "pj --max-prio N" in message
        assert "pj-admin jobs set-priority ID N" in message
        assert "`pj-admin jobs why ID`" in message

    async def test_a_capability_no_live_worker_advertises_warns(
        self, dsn, db_pool, unique_queue
    ):
        await insert_worker(db_pool, unique_queue, capabilities=("cpu",))
        (job,) = await insert_queued(db_pool, unique_queue, capability="gpu")

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        status, message = parse_checks(result.output)["unclaimable"]
        assert status == "WARN"
        assert message.startswith(
            "1 claimable job(s) that no live worker can claim: 1 on "
            f"{unique_queue!r} needing capability 'gpu', which none of the 1 "
            "live worker(s) advertises (they advertise: cpu; "
            f"e.g. jobs {job})."
        )
        assert "pj --queue Q --cap C" in message

    async def test_an_app_version_no_live_worker_runs_warns(
        self, dsn, db_pool, unique_queue
    ):
        """The deploy-shaped silence, and the one the reference design leaves
        unreported: the pin was correct when the job was enqueued and the fleet
        rolled past it."""
        await insert_worker(db_pool, unique_queue, app_version="v3")
        (job,) = await insert_queued(db_pool, unique_queue, app_version="v2")

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        status, message = parse_checks(result.output)["unclaimable"]
        assert status == "WARN"
        assert message.startswith(
            "1 claimable job(s) that no live worker can claim: 1 on "
            f"{unique_queue!r} needing app version 'v2', which none of the 1 "
            "live worker(s) advertises (they advertise: v3; "
            f"e.g. jobs {job})."
        )
        # both remedies, named
        assert "pj --queue Q --app-version V" in message
        assert "pj-admin jobs set-app-version ID [V|--clear]" in message

    async def test_unpinned_work_beside_a_versioned_fleet_does_not_warn(
        self, dsn, db_pool, unique_queue
    ):
        """A versioned worker is not a narrower worker: it claims unpinned work
        like any other, so a deploy must not make this check fire across the
        whole install."""
        await insert_worker(db_pool, unique_queue, app_version="v3")
        await insert_queued(db_pool, unique_queue)

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["unclaimable"] == (
            "PASS",
            "no queued job is invisible to its queue's live workers",
        )

    async def test_claimable_work_does_not_warn(self, dsn, db_pool, unique_queue):
        await insert_worker(db_pool, unique_queue, max_prio=500, capabilities=("gpu",))
        await insert_queued(db_pool, unique_queue, prio=500, capability="gpu")

        result = await run_doctor(dsn)

        assert parse_checks(result.output)["unclaimable"] == (
            "PASS",
            "no queued job is invisible to its queue's live workers",
        )

    async def test_an_idle_database_passes(self, dsn, db_pool):
        assert await db_pool.fetchval("SELECT COUNT(*) FROM jorb") == 0

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        assert parse_checks(result.output)["unclaimable"] == (
            "PASS",
            "no queued job is invisible to its queue's live workers",
        )

    async def test_a_queue_with_no_live_workers_is_left_to_the_workers_check(
        self, dsn, db_pool, unique_queue
    ):
        """The deliberate exclusion. "Nothing is running" and "the workers
        running are blind to this work" are different remedies, and the first
        is already reported by the `workers` check (and by `jobs why` as
        no_live_workers). Reporting it here too would fire on every idle queue
        in the install and bury the condition this check exists to find."""
        await insert_queued(db_pool, unique_queue, prio=900, capability="gpu")

        result = await run_doctor(dsn)

        assert result.exit_code == 0, result.output
        checks = parse_checks(result.output)
        assert checks["unclaimable"][0] == "PASS"
        assert checks["workers"] == ("WARN", "no live workers seen in last 60s")

    async def test_every_cause_and_both_queues_are_named(
        self, dsn, db_pool, unique_queue
    ):
        other = f"{unique_queue}_b"
        await insert_worker(
            db_pool,
            unique_queue,
            max_prio=10,
            capabilities=("cpu",),
            app_version="v3",
        )
        await insert_worker(db_pool, other, max_prio=10)
        await insert_queued(db_pool, unique_queue, count=2, prio=900)
        # under the ceiling, so this one is blocked by its capability alone:
        # the causes are disjoint, and a job that is both is counted as
        # above_worker_ceiling (the cause `jobs why` headlines)
        await insert_queued(db_pool, unique_queue, prio=5, capability="gpu")
        # under the ceiling and wanting nothing special, so this one is blocked
        # by its pin alone -- the third cause, disjoint from the first two
        await insert_queued(db_pool, unique_queue, prio=5, app_version="v2")
        await insert_queued(db_pool, other, prio=900)

        result = await run_doctor(dsn)

        status, message = parse_checks(result.output)["unclaimable"]
        assert status == "WARN"
        assert message.startswith("5 claimable job(s) that no live worker can claim:")
        assert f"2 on {unique_queue!r} above every live worker's ceiling" in message
        assert f"1 on {unique_queue!r} needing capability 'gpu'" in message
        assert f"1 on {unique_queue!r} needing app version 'v2'" in message
        # DOCTOR_UNCLAIMABLE_NAMED is 3, so the fourth group is summarised
        # rather than spelled out -- the line has to stay one line
        assert "and 1 more" in message

    async def test_the_warning_is_identical_in_json(self, dsn, db_pool, unique_queue):
        await insert_worker(db_pool, unique_queue, capabilities=("cpu",))
        await insert_queued(db_pool, unique_queue, capability="gpu")

        text = await run_doctor(dsn)
        payload = await run_doctor(dsn, "--json")

        assert payload.exit_code == text.exit_code
        record = next(
            r for r in json.loads(payload.stdout) if r["check"] == "unclaimable"
        )
        assert (record["status"], record["message"]) == parse_checks(text.output)[
            "unclaimable"
        ]
        assert record["status"] == "WARN"


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
        await db_pool.execute("UPDATE jorb SET run_group = $1 WHERE id = $1", leader)
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


# ============================================================================
# --json
# ============================================================================


class TestDoctorJson:
    """--json is the SAME checks, serialised.

    A CI job scrapes this, so the records must carry every check the text
    mode prints, in the same order, with the same message -- and the exit
    code must not change with the output format.
    """

    async def test_json_matches_the_text_report_check_for_check(self, dsn, db_pool):
        await db_pool.execute(
            """INSERT INTO jorb (job_class, queue, state, error_message)
               VALUES ('tests.dxe_jobs.FailJob', 'default', 'crashed', 'boom')"""
        )

        text = await run_doctor(dsn)
        payload = await run_doctor(dsn, "--json")

        assert payload.exit_code == text.exit_code
        records = json.loads(payload.stdout)
        assert {r["check"] for r in records} == set(parse_checks(text.output))
        assert [(r["check"], r["status"]) for r in records] == [
            (name, status) for name, (status, _) in parse_checks(text.output).items()
        ]
        dlq = next(r for r in records if r["check"] == "dlq")
        assert dlq["status"] == "WARN"
        assert "dead-lettered job(s)" in dlq["message"]

    async def test_json_prints_no_check_lines(self, dsn):
        result = await run_doctor(dsn, "--json")

        assert result.exit_code == 0, result.output
        assert parse_checks(result.stdout) == {}
        assert isinstance(json.loads(result.stdout), list)

    async def test_a_failing_report_is_still_json_and_still_exits_one(
        self, scratch_db: ScratchFactory
    ):
        """The early returns emit too: a FAIL that stops the report must not
        leave a scraper with empty stdout and a bare exit code."""
        damaged = await scratch_db()
        conn = await asyncpg.connect(damaged)
        try:
            await conn.execute("DROP TRIGGER jorb_done_notify ON jorb")
        finally:
            await conn.close()

        result = await run_doctor(damaged, "--json")

        assert result.exit_code == 1
        records = json.loads(result.stdout)
        triggers = next(r for r in records if r["check"] == "triggers")
        assert triggers["status"] == "FAIL"
        assert "jorb_done_notify" in triggers["message"]

    async def test_an_unreachable_database_still_reports_json(self, db_params):
        with reserved_unused_port() as port:
            result = await run_doctor(dsn_for({**db_params, "port": port}), "--json")

        assert result.exit_code == 1
        assert json.loads(result.stdout) == [
            {"check": "database", "status": "FAIL", "message": "unreachable"}
        ]
