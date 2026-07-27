"""The upgrade path: a database installed by an older release must be able to
become a current one.

WHY THIS FILE EXISTS. `pj-admin db migrate` used to install schema.sql when
the `jorb` table was absent and do nothing at all when it was present, and
`pyjobby/sql/migrations/` did not exist -- so there was no way to bring an
existing database up to a newer schema, and no test could notice, because the
test harness reinstalls the schema by dropping the whole `public` namespace
whenever schema.sql changes. Every test in the suite therefore ran against a
FRESH install, which is the one shape no long-lived deployment ever has.

So nothing here uses the session database. Every test creates its own, either
empty, or installed from the frozen pre-migration schema
(tests/sql/schema_before_001.sql), and the assertion is always about the
CATALOG -- columns, indexes with their predicates, function bodies, trigger
definitions, constraints, views, enum labels, storage parameters -- and never
about a recorded version number. A version number is exactly what a stale
database lies about.

THE INVARIANT UNDER TEST, stated once: a database upgraded with the shipped
migrations is indistinguishable from one installed fresh from schema.sql.
That is what makes it safe for schema.sql to be the whole current schema (so
a fresh install is one file and one statement) while migrations carry only
deltas (so an existing database is not asked to re-run history).
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from pyjobby import db as pjdb
from pyjobby import migrations
from tests.schema_fixtures import ScratchDatabases, catalog

pytestmark = pytest.mark.asyncio

#: The migration runner's own bookkeeping table. It is created by
#: migrations.py, not by schema.sql, so it is deliberately absent from the
#: required-shape manifest -- `doctor` must not demand it of a database that
#: has never been migrated, it must demand `db migrate`.
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


# ============================================================================
# What the package ships
# ============================================================================


class TestPackagedMigrations:
    async def test_migration_files_are_shipped_and_ordered(self):
        """The failure that started this: available_migrations() returned []
        because pyjobby/sql/migrations/ did not exist, so every database in
        the world was permanently at whatever schema.sql it was born with."""
        available = migrations.available_migrations()

        assert available, "no migration files ship with the package"
        versions = [m.version for m in available]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions), f"duplicate versions: {versions}"
        assert versions[0] == 1, "numbering starts at 001"
        assert all(m.sql.strip() for m in available), "a migration file is empty"

    async def test_migration_files_are_readable_through_the_installed_package(self):
        """They are loaded with importlib.resources, so a file that is on
        disk but not packaged reads as "no migrations" at runtime -- the exact
        silent failure this file exists to prevent."""
        names = {m.name for m in migrations.available_migrations()}

        assert names == {
            entry.name
            for entry in (migrations._SQL_ROOT / "migrations").iterdir()
            if entry.name.endswith(".sql")
        }


# ============================================================================
# Fresh install
# ============================================================================


class TestFreshInstall:
    async def test_fresh_install_records_migrations_without_running_them(
        self, scratch: ScratchDatabases
    ):
        """schema.sql IS the current schema, so a fresh database already has
        everything the migrations produce. Running them anyway would at best
        be wasted DDL and at worst real damage -- 001 rebuilds two indexes and
        deletes rows -- so they are stamped, not applied."""
        params = await scratch.create(install=None)
        conn = await connect(params)
        try:
            result = await migrations.migrate(conn)

            assert result.installed_base is True
            assert result.applied == [], "a fresh install must run no migration"
            assert result.recorded == [
                m.version for m in migrations.available_migrations()
            ]
            assert await migrations.applied_versions(conn) == set(result.recorded)
        finally:
            await conn.close()

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

    async def test_every_migration_file_is_a_no_op_on_a_fresh_install(
        self, scratch: ScratchDatabases
    ):
        """The safety net under the "stamp, don't run" decision.

        Stamping means a fresh database never executes these files -- but a
        database at an INTERMEDIATE shape executes them while already holding
        some of what they install, and there is no fixture for every
        intermediate shape that ever shipped. A fresh install is the extreme
        case of "already has all of it", so running the files against one and
        requiring the catalog not to move proves every statement in them is
        conditional.
        """
        params = await scratch.create()
        conn = await connect(params)
        try:
            before = await catalog(conn)

            for migration in migrations.available_migrations():
                await conn.execute(migration.sql)

            assert await catalog(conn) == before
        finally:
            await conn.close()


# ============================================================================
# Upgrade
# ============================================================================


class TestUpgradeFromLegacySchema:
    async def test_the_legacy_fixture_really_is_stale(self, scratch: ScratchDatabases):
        """Guards every assertion below it.

        If the frozen schema were quietly refreshed to the current one, the
        upgrade tests would all pass while testing nothing, so this pins the
        specific drift: the columns whose absence made `pj-admin doctor` die
        on a database it had just certified.
        """
        params = await scratch.create(install="legacy")
        conn = await connect(params)
        try:
            missing = await migrations.missing_objects(conn)

            assert "column jorb_worker.job_threads" in missing
            assert "column jorb.tags" in missing
            assert "column jorb.claimed_at" in missing
            assert "column jorb.awaited" in missing
            assert "function claim_jorb" in missing
            assert "function jorb_notify" in missing
            assert "index jorb_tags_idx" in missing
            assert "column jorb.schedule_id" in missing
            assert "index jorb_schedule_id_idx" in missing
            # ... and it is a database no version number can tell apart from a
            # current one: it records nothing, so nothing is "pending".
            assert await migrations.applied_versions(conn) == set()
        finally:
            await conn.close()

    async def test_upgraded_database_is_identical_to_a_fresh_install(
        self, scratch: ScratchDatabases
    ):
        """The whole point. Not "the version matches" -- the CATALOG matches."""
        old = await connect(await scratch.create(install="legacy"))
        new = await connect(await scratch.create())
        try:
            result = await migrations.migrate(old)
            assert result.applied == [
                m.version for m in migrations.available_migrations()
            ]

            upgraded, fresh = await catalog(old), await catalog(new)
            assert upgraded == fresh, "\n".join(
                ["upgraded database differs from a fresh install:"]
                + [f"  only upgraded: {o}" for o in sorted(set(upgraded) - set(fresh))]
                + [f"  only fresh:    {f}" for f in sorted(set(fresh) - set(upgraded))]
            )
        finally:
            await old.close()
            await new.close()

    async def test_upgrading_twice_changes_nothing(self, scratch: ScratchDatabases):
        conn = await connect(await scratch.create(install="legacy"))
        try:
            await migrations.migrate(conn)
            after_first = await catalog(conn)

            result = await migrations.migrate(conn)

            assert result.changed is False
            assert await catalog(conn) == after_first
        finally:
            await conn.close()

    async def test_upgrade_keeps_the_jobs_that_were_already_there(
        self, scratch: ScratchDatabases
    ):
        """An upgrade is not a re-create: the rows are the reason the database
        could not simply be dropped and reinstalled in the first place."""
        conn = await connect(await scratch.create(install="legacy"))
        try:
            job_id = await conn.fetchval(
                "INSERT INTO jorb (queue, job_class, kwargs) "
                "VALUES ('legacy', 'tests.dxe_jobs.OkJob', $1) RETURNING id",
                {"n": 1},
            )

            await migrations.migrate(conn)

            row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert row["queue"] == "legacy"
            assert row["kwargs"] == {"n": 1}
            # The new columns arrive with exactly the values a fresh row gets.
            assert row["tags"] == {}
            assert row["awaited"] is False
            assert row["claimed_at"] is None
        finally:
            await conn.close()

    async def test_upgrade_moves_schedule_provenance_out_of_admin_data(
        self, scratch: ScratchDatabases
    ):
        """002 relocates `admin_data->>'schedule_id'` to `jorb.schedule_id`.

        The backfill is a CORRECTNESS step, not tidiness. The concurrency
        check reads the column the instant the new code is deployed, so a
        schedule whose jobs are still in flight -- old jsonb key, NULL column
        -- would count ZERO of them and fire again while already at its
        limit, which is the runaway max_concurrent_jobs exists to prevent.

        And the key is removed rather than left beside the column, because
        two copies of one fact are two things that can disagree.
        """
        conn = await connect(await scratch.create(install="legacy"))
        try:
            live = await conn.fetchval(
                "INSERT INTO jorb (queue, job_class, state, admin_data) "
                "VALUES ('sched', 'J', 'running', $1) RETURNING id",
                {"schedule_id": "7", "schedule_name": "nightly"},
            )
            plain = await conn.fetchval(
                "INSERT INTO jorb (queue, job_class, admin_data) "
                "VALUES ('sched', 'J', $1) RETURNING id",
                {"max_retries": 3},
            )

            await migrations.migrate(conn)

            moved = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", live)
            assert moved["schedule_id"] == 7
            assert moved["admin_data"] == {"schedule_name": "nightly"}
            # a job no schedule created is untouched and stays out of the index
            untouched = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", plain)
            assert untouched["schedule_id"] is None
            assert untouched["admin_data"] == {"max_retries": 3}
        finally:
            await conn.close()

    async def test_upgrade_leaves_an_unreadable_schedule_id_alone(
        self, scratch: ScratchDatabases
    ):
        """admin_data is free-form jsonb, so the key may hold anything.

        Casting it blindly aborts the whole upgrade on one hand-edited row.
        Deleting it blindly is worse -- the migration would destroy the only
        copy of something it could not understand -- so such a row keeps both
        its key and its NULL column, where a human can still see it.
        """
        conn = await connect(await scratch.create(install="legacy"))
        try:
            odd = await conn.fetchval(
                "INSERT INTO jorb (queue, job_class, admin_data) "
                "VALUES ('sched', 'J', $1) RETURNING id",
                {"schedule_id": "not-a-number"},
            )

            await migrations.migrate(conn)

            row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", odd)
            assert row["schedule_id"] is None
            assert row["admin_data"] == {"schedule_id": "not-a-number"}
        finally:
            await conn.close()

    async def test_upgrade_installs_the_history_cascade_over_orphaned_rows(
        self, scratch: ScratchDatabases
    ):
        """jorb_history.job_id gained a foreign key, and an old database can
        hold rows that violate it -- history whose job was deleted before the
        cascade existed. Under the current schema those rows CANNOT exist, so
        the migration removes them; leaving them would make ADD CONSTRAINT
        fail and strand the operator mid-upgrade.
        """
        conn = await connect(await scratch.create(install="legacy"))
        try:
            job_id = await conn.fetchval(
                "INSERT INTO jorb (queue, job_class) VALUES ('legacy', 'J') "
                "RETURNING id"
            )
            await conn.execute(
                "INSERT INTO jorb_history (job_id, event) VALUES ($1, 'orphan')",
                job_id + 10_000,
            )
            assert await conn.fetchval("SELECT count(*) FROM jorb_history") == 2

            await migrations.migrate(conn)

            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM jorb_history WHERE event = 'orphan'"
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM jorb_history WHERE job_id = $1", job_id
                )
                == 1
            )
            # And the key is real: deleting the job now takes its history.
            await conn.execute("DELETE FROM jorb WHERE id = $1", job_id)
            assert await conn.fetchval("SELECT count(*) FROM jorb_history") == 0
        finally:
            await conn.close()


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

    async def test_two_upgrades_at_once_apply_each_migration_once(
        self, scratch: ScratchDatabases
    ):
        params = await scratch.create(install="legacy")
        a, b = await connect(params), await connect(params)
        try:
            first, second = await asyncio.gather(
                migrations.migrate(a), migrations.migrate(b)
            )

            versions = [m.version for m in migrations.available_migrations()]
            assert sorted(first.applied + second.applied) == versions
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
    async def test_status_on_a_stale_database_names_what_is_missing(
        self, scratch: ScratchDatabases
    ):
        conn = await connect(await scratch.create(install="legacy"))
        try:
            info = await migrations.status(conn)

            assert info["base_schema_installed"] is True
            assert info["pending"] == [
                m.version for m in migrations.available_migrations()
            ]
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
