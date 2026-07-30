"""The migration RUNNER, proven ready before it is needed.

PRE-LIVE, no migration files ship: the base schema (pyjobby/sql/schema/) is
always the whole current schema, a fresh install is the only supported
history, and ``available_migrations()`` is empty -- a state this file pins so
a stray NNN_*.sql cannot sneak into the package while the pre-live policy is
in force.

The runner itself must stay exercised anyway, because the day the platform
goes live the FIRST schema change mints migrations/001_*.sql and everything
here -- ordering, per-file transactions, recorded-vs-applied, the advisory
lock -- has to already work. So the apply-path tests run against SYNTHETIC
migrations injected by monkeypatching ``available_migrations()``: the runner
executes real DDL on a real scratch database, and no file ships.

Nothing here uses the session database. Every test creates its own, and the
assertions are about the CATALOG -- never about a recorded version number,
which is exactly what a stale database lies about.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from pyjobby import db as pjdb
from pyjobby import migrations
from pyjobby.migrations import Migration
from tests.schema_fixtures import ScratchDatabases, catalog

pytestmark = pytest.mark.asyncio

#: The migration runner's own bookkeeping table. It is created by
#: migrations.py, not by the base schema, so it is deliberately absent from
#: the required-shape manifest -- `doctor` must not demand it of a database
#: that has never been migrated, it must demand `db migrate`.
RUNNER_OWN_OBJECTS = {"schema_migrations", "schema_migrations_pkey"}


@pytest_asyncio.fixture
async def scratch(db_params: dict):
    """Throwaway databases; all of them dropped when the test ends."""
    factory = ScratchDatabases(db_params)
    try:
        yield factory
    finally:
        await factory.close()


async def connect(params: dict) -> asyncpg.Connection:
    return await pjdb.connect(**params)


def synthetic_migrations(monkeypatch, *files: tuple[int, str, str]) -> None:
    """Make the runner see `files` as the shipped migrations.

    Each entry is (version, name, sql). Patching ``available_migrations`` is
    the whole intervention: everything downstream -- ordering, transactions,
    recording, locking -- is the real runner against a real database.
    """
    fixed = [Migration(version=v, name=n, sql=s) for v, n, s in files]
    monkeypatch.setattr(
        migrations,
        "available_migrations",
        lambda: sorted(fixed, key=lambda m: m.version),
    )


# ============================================================================
# The pre-live policy, pinned
# ============================================================================


class TestPreLivePolicy:
    async def test_no_migration_files_ship_before_the_platform_is_live(self):
        """Pre-live, every schema change goes into the base schema files and
        a fresh install is the whole story. A migration file appearing in the
        package would mean an upgrade path is being built for deployments
        that do not exist -- this fails the moment one ships, so minting 001
        is an explicit decision (going live), not an accident."""
        assert migrations.available_migrations() == []

    async def test_the_base_schema_is_the_ordered_purpose_files(self):
        """base_schema_sql() concatenates pyjobby/sql/schema/*.sql in lexical
        order; the numeric prefixes ARE the dependency order. Pinning the
        first and last keeps a stray file from silently changing what a fresh
        install runs first or last."""
        names = sorted(
            entry.name
            for entry in (migrations._SQL_ROOT / "schema").iterdir()
            if entry.name.endswith(".sql")
        )
        assert names[0] == "00_core.sql", names
        assert names[-1] == "92_history_trigger.sql", names
        sql = migrations.base_schema_sql()
        assert sql.index("CREATE TYPE jorbstate") < sql.index("CREATE TABLE jorb ")


# ============================================================================
# Fresh install
# ============================================================================


class TestFreshInstall:
    async def test_fresh_install_has_nothing_pending_and_nothing_missing(
        self, scratch: ScratchDatabases
    ):
        params = await scratch.create()
        conn = await connect(params)
        try:
            info = await migrations.status(conn)

            assert info["base_schema_installed"] is True
            assert info["pending"] == []
            assert info["missing"] == []
        finally:
            await conn.close()

    async def test_migrate_twice_changes_nothing(self, scratch: ScratchDatabases):
        """`db migrate` is meant to run on every deploy, so the second run of
        the day has to be a no-op down to the catalog."""
        params = await scratch.create()
        conn = await connect(params)
        try:
            before = await catalog(conn)

            result = await migrations.migrate(conn)

            assert result.installed_base is False
            assert result.applied == []
            assert result.changed is False
            assert await catalog(conn) == before
        finally:
            await conn.close()


# ============================================================================
# The apply path, against synthetic migrations
# ============================================================================


class TestRunnerAppliesMigrations:
    """The post-live half of the runner, kept working while unused.

    These are the semantics the first real migration will land on: files are
    applied in version order, each in its own transaction, recorded only when
    they commit -- and a fresh install stamps them all without running one.
    """

    async def test_fresh_install_records_synthetic_migrations_without_running_them(
        self, monkeypatch, scratch: ScratchDatabases
    ):
        """The base schema is by definition the sum of every migration, so a
        fresh database already has everything they produce. They are stamped,
        not applied -- proven by the side effect NOT existing."""
        synthetic_migrations(
            monkeypatch,
            (1, "001_a.sql", "CREATE TABLE pjtest_mig_a (id int)"),
            (2, "002_b.sql", "CREATE TABLE pjtest_mig_b (id int)"),
        )
        params = await scratch.create(install=None)
        conn = await connect(params)
        try:
            result = await migrations.migrate(conn)

            assert result.installed_base is True
            assert result.applied == []
            assert result.recorded == [1, 2]
            assert await migrations.applied_versions(conn) == {1, 2}
            # stamped means the DDL never ran
            assert not await conn.fetchval("SELECT to_regclass('pjtest_mig_a')")
        finally:
            await conn.close()

    async def test_pending_migrations_apply_in_order_and_are_recorded(
        self, monkeypatch, scratch: ScratchDatabases
    ):
        """An existing database (installed before the files existed) runs
        them, oldest first -- 002 depends on 001 having run."""
        params = await scratch.create()  # installed, records nothing yet
        synthetic_migrations(
            monkeypatch,
            (2, "002_b.sql", "ALTER TABLE pjtest_mig_a ADD COLUMN b int"),
            (1, "001_a.sql", "CREATE TABLE pjtest_mig_a (id int)"),
        )
        conn = await connect(params)
        try:
            result = await migrations.migrate(conn)

            assert result.installed_base is False
            assert result.applied == [1, 2]
            assert await migrations.applied_versions(conn) == {1, 2}
            assert await conn.fetchval("SELECT to_regclass('pjtest_mig_a')")

            again = await migrations.migrate(conn)
            assert again.applied == [], "recorded migrations were re-run"
        finally:
            await conn.close()

    async def test_a_failing_migration_is_rolled_back_and_not_recorded(
        self, monkeypatch, scratch: ScratchDatabases
    ):
        """One transaction per file: a failure leaves the database exactly as
        it was before that file, records nothing for it, and does not stop
        earlier files from having landed -- so the operator fixes the file
        and re-runs, rather than untangling half-applied DDL."""
        params = await scratch.create()
        synthetic_migrations(
            monkeypatch,
            (1, "001_ok.sql", "CREATE TABLE pjtest_mig_a (id int)"),
            (
                2,
                "002_boom.sql",
                "CREATE TABLE pjtest_mig_b (id int); SELECT no_such_function()",
            ),
        )
        conn = await connect(params)
        try:
            with pytest.raises(asyncpg.PostgresError):
                await migrations.migrate(conn)

            # 001 landed and is recorded; 002 left nothing behind
            assert await migrations.applied_versions(conn) == {1}
            assert await conn.fetchval("SELECT to_regclass('pjtest_mig_a')")
            assert not await conn.fetchval("SELECT to_regclass('pjtest_mig_b')")
        finally:
            await conn.close()

    async def test_two_upgrades_at_once_apply_each_migration_once(
        self, monkeypatch, scratch: ScratchDatabases
    ):
        """Two hosts deploying at the same instant: the advisory lock makes
        one apply and the other find nothing left to do -- CREATE TABLE has
        no IF NOT EXISTS here precisely so a double-run would fail loudly."""
        params = await scratch.create()
        synthetic_migrations(
            monkeypatch,
            (1, "001_a.sql", "CREATE TABLE pjtest_mig_a (id int)"),
        )
        a, b = await connect(params), await connect(params)
        try:
            first, second = await asyncio.gather(
                migrations.migrate(a), migrations.migrate(b)
            )

            assert sorted(first.applied + second.applied) == [1]
            assert await migrations.applied_versions(a) == {1}
        finally:
            await a.close()
            await b.close()


# ============================================================================
# The required-shape manifest
# ============================================================================


class TestRequiredShapeManifest:
    """migrations.REQUIRED_* is what `doctor` means by "installed".

    A hand-written manifest that nothing checks is a manifest that rots into
    listing objects the schema dropped and missing the ones it added, so it is
    compared against a fresh install in BOTH directions: it may not name
    anything that is not there, and it may not omit anything that is.
    """

    @pytest_asyncio.fixture
    async def fresh(self, scratch: ScratchDatabases):
        conn = await connect(await scratch.create())
        try:
            yield conn
        finally:
            await conn.close()

    async def test_manifest_lists_exactly_the_installed_tables_and_columns(
        self, fresh: asyncpg.Connection
    ):
        installed: dict[str, set[str]] = {}
        for row in await fresh.fetch(
            """SELECT c.table_name, c.column_name
                 FROM information_schema.columns c
                 JOIN information_schema.tables t
                   ON t.table_schema = c.table_schema
                  AND t.table_name = c.table_name
                WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"""
        ):
            if row["table_name"] in RUNNER_OWN_OBJECTS:
                continue
            installed.setdefault(row["table_name"], set()).add(row["column_name"])

        assert {t: set(c) for t, c in migrations.REQUIRED_COLUMNS.items()} == installed

    async def test_manifest_lists_exactly_the_installed_functions(
        self, fresh: asyncpg.Connection
    ):
        installed = {
            r["proname"]
            for r in await fresh.fetch(
                """SELECT p.proname FROM pg_proc p
                     JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'"""
            )
        }
        assert set(migrations.REQUIRED_FUNCTIONS) == installed

    async def test_manifest_lists_exactly_the_installed_views(
        self, fresh: asyncpg.Connection
    ):
        installed = {
            r["viewname"]
            for r in await fresh.fetch(
                "SELECT viewname FROM pg_views WHERE schemaname = 'public'"
            )
        }
        assert set(migrations.REQUIRED_VIEWS) == installed

    async def test_manifest_lists_exactly_the_installed_triggers(
        self, fresh: asyncpg.Connection
    ):
        installed = {
            r["tgname"]
            for r in await fresh.fetch(
                """SELECT tg.tgname FROM pg_trigger tg
                     JOIN pg_class cl ON cl.oid = tg.tgrelid
                     JOIN pg_namespace n ON n.oid = cl.relnamespace
                    WHERE n.nspname = 'public' AND NOT tg.tgisinternal"""
            )
        }
        assert set(migrations.REQUIRED_TRIGGERS) == installed

    async def test_manifest_lists_exactly_the_installed_indexes(
        self, fresh: asyncpg.Connection
    ):
        installed = {
            r["indexname"]
            for r in await fresh.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        } - RUNNER_OWN_OBJECTS
        assert set(migrations.REQUIRED_INDEXES) == installed

    async def test_manifest_lists_the_enum_labels_in_order(
        self, fresh: asyncpg.Connection
    ):
        for name, labels in migrations.REQUIRED_ENUM_LABELS.items():
            installed = [
                r["enumlabel"]
                for r in await fresh.fetch(
                    """SELECT e.enumlabel FROM pg_enum e
                         JOIN pg_type t ON t.oid = e.enumtypid
                        WHERE t.typname = $1 ORDER BY e.enumsortorder""",
                    name,
                )
            ]
            assert list(labels) == installed

    async def test_a_dropped_object_is_reported_by_name(
        self, scratch: ScratchDatabases
    ):
        """The manifest is only useful if a real absence surfaces through it."""
        conn = await connect(await scratch.create())
        try:
            await conn.execute("DROP INDEX jorb_tags_idx")
            await conn.execute("ALTER TABLE jorb_worker DROP COLUMN job_threads")

            missing = await migrations.missing_objects(conn)

            assert missing == [
                "column jorb_worker.job_threads",
                "index jorb_tags_idx",
            ]
        finally:
            await conn.close()


# ============================================================================
# Drift repair: what migrate() can heal, and what it must refuse
# ============================================================================


class TestDriftRepair:
    """`doctor`'s trigger and shape FAILs both end in "run: pj-admin db
    migrate", so migrate has to actually fix what they report -- for the
    drift whose canonical DDL is a standalone CREATE statement (triggers and
    plain indexes), and refuse everything deeper rather than dress a
    different schema revision up as current."""

    async def test_every_required_trigger_has_canonical_ddl(self):
        """The repair path is only a remedy if it can never meet a trigger
        it has no statement for."""
        for name in migrations.REQUIRED_TRIGGERS:
            statement = migrations.canonical_create_statement("trigger", name)
            assert statement is not None, name
            assert name in statement

    async def test_index_ddl_exists_exactly_for_non_constraint_indexes(self):
        """Constraint-backed indexes (``*_pkey``, ``*_key``) exist only
        through their table's constraint -- no standalone CREATE to extract,
        so they are deep drift by definition. Everything else must have one."""
        for name in migrations.REQUIRED_INDEXES:
            statement = migrations.canonical_create_statement("index", name)
            if name.endswith(("_pkey", "_key")):
                assert statement is None, name
            else:
                assert statement is not None, name
                assert name in statement

    async def test_only_triggers_and_plain_indexes_are_repairable(self):
        assert migrations.repairable("trigger jorb_history_record")
        assert migrations.repairable("index jorb_tags_idx")
        assert not migrations.repairable("index jorb_pkey")
        assert not migrations.repairable("column jorb_worker.job_threads")
        assert not migrations.repairable("table jorb")
        assert not migrations.repairable("function claim_jorb")
        assert not migrations.repairable("view jorb_dag_status")

    async def test_migrate_recreates_dropped_trigger_and_index_verbatim(
        self, scratch: ScratchDatabases
    ):
        """After repair the catalog is byte-identical to a fresh install --
        the strongest version of "recreated from the base schema"."""
        conn = await connect(await scratch.create())
        try:
            fresh = await catalog(conn)
            await conn.execute("DROP TRIGGER jorb_history_record ON jorb")
            await conn.execute("DROP INDEX jorb_dag_retention_idx")

            result = await migrations.migrate(conn)

            assert result.repaired == [
                "index jorb_dag_retention_idx",
                "trigger jorb_history_record",
            ]
            assert result.changed
            assert await catalog(conn) == fresh
            assert await migrations.missing_objects(conn) == []
            assert await migrations.missing_triggers(conn) == []
        finally:
            await conn.close()

    async def test_migrate_repairs_nothing_on_top_of_deep_drift(
        self, scratch: ScratchDatabases
    ):
        """All-or-nothing: a missing column means a different schema
        revision, and recreating the index on top of it would make doctor's
        report SHRINK while the database stays wrong."""
        conn = await connect(await scratch.create())
        try:
            await conn.execute("DROP INDEX jorb_dag_retention_idx")
            await conn.execute("ALTER TABLE jorb_worker DROP COLUMN job_threads")

            result = await migrations.migrate(conn)

            assert result.repaired == []
            assert not result.changed
            assert await migrations.missing_objects(conn) == [
                "column jorb_worker.job_threads",
                "index jorb_dag_retention_idx",
            ]
        finally:
            await conn.close()

    async def test_migrate_on_a_healthy_database_repairs_nothing(
        self, scratch: ScratchDatabases
    ):
        conn = await connect(await scratch.create())
        try:
            result = await migrations.migrate(conn)
            assert result.repaired == []
            assert not result.changed
        finally:
            await conn.close()


# ============================================================================
# Concurrency
# ============================================================================


class TestConcurrentMigrate:
    """Two hosts running the deploy step at the same instant.

    Without a lock the first install is a race on `CREATE TYPE jorbstate`,
    which has no IF NOT EXISTS: one deploy wins and the other fails on a
    duplicate-object error. Both connections here are real and concurrent --
    the lock is held in PostgreSQL, so nothing about it can be observed from
    a single process pretending.
    """

    async def test_two_first_installs_at_once_both_succeed(
        self, scratch: ScratchDatabases
    ):
        params = await scratch.create(install=None)
        a, b = await connect(params), await connect(params)
        try:
            first, second = await asyncio.gather(
                migrations.migrate(a), migrations.migrate(b)
            )

            installed = [r for r in (first, second) if r.installed_base]
            assert len(installed) == 1, "both connections installed the base schema"
            (loser,) = [r for r in (first, second) if not r.installed_base]
            assert loser.applied == [], "the loser re-ran migrations over the winner"
            assert loser.recorded == []
            assert await migrations.missing_objects(a) == []
        finally:
            await a.close()
            await b.close()

    async def test_migrate_waits_for_a_lock_another_session_holds(
        self, scratch: ScratchDatabases
    ):
        """Proves the serialisation is real and not a coincidence of timing:
        while the lock is held elsewhere, migrate makes no progress at all --
        it has not installed the schema, it is waiting to be allowed to."""
        params = await scratch.create(install=None)
        blocker, worker, observer = (
            await connect(params),
            await connect(params),
            await connect(params),
        )
        try:
            await blocker.execute(
                "SELECT pg_advisory_lock($1)", migrations.MIGRATE_LOCK_KEY
            )

            task = asyncio.create_task(migrations.migrate(worker))
            await asyncio.sleep(1.0)

            assert not task.done(), "migrate ignored a held lock"
            assert not await observer.fetchval("SELECT to_regclass('public.jorb')")

            await blocker.execute(
                "SELECT pg_advisory_unlock($1)", migrations.MIGRATE_LOCK_KEY
            )
            result = await asyncio.wait_for(task, timeout=60)

            assert result.installed_base is True
            assert await migrations.missing_objects(observer) == []
        finally:
            await blocker.close()
            await worker.close()
            await observer.close()

    async def test_the_lock_is_released_when_migrate_returns(
        self, scratch: ScratchDatabases
    ):
        """A migrator that kept the lock would block every later deploy on
        the fleet until its connection happened to close."""
        params = await scratch.create(install=None)
        conn, other = await connect(params), await connect(params)
        try:
            await migrations.migrate(conn)

            got = await other.fetchval(
                "SELECT pg_try_advisory_lock($1)", migrations.MIGRATE_LOCK_KEY
            )
            assert got is True, "migrate left its advisory lock held"
            await other.execute(
                "SELECT pg_advisory_unlock($1)", migrations.MIGRATE_LOCK_KEY
            )
        finally:
            await conn.close()
            await other.close()


# ============================================================================
# status()
# ============================================================================


class TestStatus:
    async def test_status_on_a_drifted_database_names_what_is_missing(
        self, scratch: ScratchDatabases
    ):
        """A database whose shape diverges from the base schema is reported
        by OBJECT, not by version -- a version number is exactly what a
        drifted database lies about."""
        conn = await connect(await scratch.create())
        try:
            await conn.execute("ALTER TABLE jorb DROP COLUMN tags")

            info = await migrations.status(conn)

            assert info["base_schema_installed"] is True
            assert "column jorb.tags" in info["missing"]
        finally:
            await conn.close()

    async def test_status_on_an_empty_database_asks_for_nothing_but_the_install(
        self, scratch: ScratchDatabases
    ):
        """`missing` is silent when there is no schema at all: listing sixty
        absent objects would bury the one fact that matters."""
        conn = await connect(await scratch.create(install=None))
        try:
            info = await migrations.status(conn)

            assert info["base_schema_installed"] is False
            assert info["missing"] == []
        finally:
            await conn.close()


# ============================================================================
# The startup preflight every daemon shares
# ============================================================================


class TestPreflightProblem:
    """``migrations.preflight_problem`` -- the one question pj, pj-monitor and
    pj-scheduler each ask once before their loop.

    It lives here because the REMEDY does: this module owns SCHEMA_REMEDY,
    schema_error_hint and the required-shape manifest, and a preflight is that
    knowledge asked as a yes/no question. Two of the three daemons carried a
    private copy of it and the third had none at all -- which is the shape a
    duplicated startup check always ends up in, and why the daemons' own tests
    now cover only their WIRING (that they call this, before the loop, and
    turn the answer into an exit code).
    """

    async def test_a_usable_database_has_no_problem(self, db_params: dict):
        """Both target forms, because the daemons hold different ones: a
        db_params table (pj, pj-scheduler, pj-monitor --config) and a DSN
        string (pj-monitor --dsn)."""
        dsn = (
            f"postgresql://{db_params['user']}:{db_params['password']}"
            f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
        )
        assert await migrations.preflight_problem(db_params) is None
        assert await migrations.preflight_problem(dsn) is None

    async def test_a_database_without_the_schema_names_the_remedy(
        self, scratch: ScratchDatabases
    ):
        """The problem string has to carry BOTH halves an operator needs:
        which database, and what to do about it."""
        params = await scratch.create(install=None)

        problem = await migrations.preflight_problem(params)

        assert problem is not None
        assert params["database"] in problem
        assert migrations.MIGRATE_REMEDY in problem

    async def test_an_unreachable_database_names_the_target_not_the_password(self):
        """A failure message must say WHICH database -- an operator running
        four deployments cannot act on "connection refused" -- and must do it
        without printing the password every target form carries."""
        problem = await migrations.preflight_problem(
            {
                "host": "127.0.0.1",
                # privileged and unbound: connection refused, now
                "port": 1,
                "database": "pyjobby",
                "user": "nobody",
                "password": "hunter2-must-not-be-logged",
            }
        )

        assert problem is not None
        assert "127.0.0.1:1/pyjobby" in problem
        assert "hunter2" not in problem
