"""Schema installation and migration runner.

The base schema lives in ``pyjobby/sql/schema/`` as ordered, purpose-named
files (``00_core.sql``, ``10_jobs.sql``, ... ``92_history_trigger.sql``); a
FRESH install executes their concatenation, in lexical order, in one
transaction. An EXISTING database runs any numbered files from
``pyjobby/sql/migrations/`` it has not recorded in ``schema_migrations`` yet
-- and a fresh install records every shipped migration WITHOUT running it,
because the base schema already contains their effects by definition.

Both paths are the same command::

    pj-admin db migrate

or programmatically::

    from pyjobby import db, migrations
    conn = await db.connect(**db_params)
    await migrations.migrate(conn)

PRE-LIVE POLICY (in force until the platform has live deployments): every
schema change edits the base schema files and the required-shape manifest
below, and NO migration files ship -- ``pyjobby/sql/migrations/`` does not
exist, ``available_migrations()`` is empty, and a fresh install is always the
complete current schema. The runner itself is kept exercised against
synthetic migrations by ``tests/test_migrations.py``, so the day the platform
goes live, the FIRST real schema change mints ``migrations/001_*.sql``
against the then-frozen baseline and everything below already works.

WHAT A SCHEMA CHANGE COSTS, POST-LIVE: edit the base schema files *and* add
``pyjobby/sql/migrations/NNN_*.sql`` carrying the same change in idempotent,
already-applied-tolerant form, then add the objects it introduces to the
required-shape manifest below. The invariant the two paths depend on -- base
schema == previous base schema + migration N -- is enforced by the test
suite, not by this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import lifecycle

_SQL_ROOT = files("pyjobby") / "sql"

_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

#: Advisory-lock key serialising every ``migrate()`` in the fleet.
#:
#: A first install is not idempotent at the statement level -- ``CREATE TYPE
#: jorbstate`` has no IF NOT EXISTS -- so two hosts running their deploy step
#: at the same instant used to race, and one of them failed the deploy on a
#: duplicate-object error. The lock is session level (not ``_xact_``) because
#: the install and each migration file run in their OWN transactions: an
#: xact-scoped lock would be released between them and let a second migrator
#: in halfway through.
#:
#: A literal rather than ``hashtext('pyjobby.migrate')``: this is the one lock
#: taken before any pyjobby object exists, so it must not depend on a hash
#: function whose value PostgreSQL is free to change between major versions.
MIGRATE_LOCK_KEY = 0x706A62_6D6967  # "pjb" "mig"


# ============================================================================
# The required-shape manifest
# ============================================================================
# What `pj-admin doctor` means by "the schema is installed". Before this
# existed, doctor checked that the `jorb` table was present and that no
# numbered migration was pending -- and a database installed from an older
# base schema satisfies BOTH: it has jorb, and it has no schema_migrations row
# saying otherwise. Doctor reported PASS schema and the very next check died
# on `column "job_threads" does not exist`. The health probe certified a
# database it could not use.
#
# So the check is now the SHAPE: every object the running code needs, by name.
# It is written out rather than derived, so an operator reading a FAIL sees
# what is missing -- and it cannot rot, because tests/test_migrations.py
# asserts this manifest equals the catalog of a fresh install in BOTH
# directions. Adding an object to the base schema without adding it here
# fails that test; so does leaving a dropped object behind.
#
# Indexes are in scope. A missing index does not raise, so this is the one
# entry that is about drift rather than about a query that cannot run -- and
# drift is the point: a database missing jorb_tags_idx was installed from a
# different revision of the base schema, which is the exact condition doctor
# kept certifying. It also matters on its own, since `pj-admin jobs list --tag`
# sequentially scans the hottest table in the system without it.
#
# Triggers are declared below but deliberately left OUT of missing_objects():
# doctor gives them their own check line so that a dropped trigger is reported
# as the specific thing it is -- nothing raises when one goes missing, the
# platform just quietly stops recording history or waking waiters. That check
# reads REQUIRED_TRIGGERS from here, so there is still exactly one list.


def _names(listing: str) -> frozenset[str]:
    """Whitespace-separated object names as a set.

    Written this way, and not as a set literal per name, so the manifest reads
    like the CREATE TABLE it mirrors instead of as three hundred lines of one
    quoted identifier apiece.
    """
    return frozenset(listing.split())


REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "jorb": _names("""
        id queue capability prio state job_class kwargs admin_data result uid
        tags run_group waitfor_group waitfor_job dag_id deadline_key
        schedule_id run_count
        error_count error_message error_backtrace run_epoch cancel_requested
        awaited claimed_by worker_pid worker_host created updated run_after
        claimed_at started finished timeout_at
    """),
    "jorb_queue": _names("""
        name paused max_concurrency rate_limit rate_period_seconds created
        updated
    """),
    "jorb_worker": _names("""
        id host pid queue capabilities max_prio version started last_seen
        shutdown_at idle job_threads job_threads_abandoned
    """),
    "jorb_step": _names("""
        job_id step_seq name output error run_epoch started finished
    """),
    "jorb_event": _names("job_id key value updated"),
    "jorb_mailbox": _names("id dest_job_id topic message created consumed_at"),
    "jorb_history": _names("id job_id at event detail"),
    "jorb_schedule": _names("""
        id name description job_class kwargs queue prio capability cron_expr
        timezone enabled max_concurrent_jobs jitter_seconds
        backpressure_threshold circuit_breaker_threshold consecutive_failures
        next_run last_run last_success last_failure run_count success_count
        failure_count skip_count created updated created_by
    """),
    "jorb_schedule_log": _names("""
        id schedule_id schedule_name scheduled_time actual_time result
        skip_reason job_id error_message duration_ms queue_depth_at_run
        concurrent_jobs_at_run jitter_applied_seconds
    """),
    "jorb_dag": _names("id name created completed metadata"),
    "jorb_dependencies": _names("job_id depends_on"),
}

REQUIRED_VIEWS: frozenset[str] = _names("jorb_dag_status jorb_dag_timeline")

REQUIRED_FUNCTIONS: frozenset[str] = _names("""
    claim_jorb claim_queue_lock complete_jorb_dag jorb_notify
    record_jorb_history
""")

#: Every trigger the base schema installs. `pj-admin doctor` reads this for its
#: "triggers" check, so the platform has one list of them and not two.
REQUIRED_TRIGGERS: tuple[str, ...] = (
    "jorb_enqueued_notify",
    "jorb_done_notify",
    "jorb_cancel_notify",
    "jorb_event_notify",
    "schedule_executed_notify",
    "jorb_history_record",
    "jorb_dag_complete",
)

REQUIRED_INDEXES: frozenset[str] = _names("""
    jorb_pkey jorb_claim_idx jorb_claimed_at_idx jorb_started_idx
    jorb_inflight_idx jorb_retention_idx jorb_created_idx jorb_timeout_idx
    jorb_waitfor_job_idx jorb_waitfor_group_idx jorb_run_group_idx
    jorb_group_unfinished_idx jorb_uid_idx
    jorb_dag_idx jorb_tags_idx jorb_deadline_idx jorb_schedule_id_idx
    jorb_finished_retention_idx
    jorb_queue_pkey
    jorb_worker_pkey jorb_worker_live_idx jorb_worker_retention_idx
    jorb_worker_idle_idx
    jorb_step_pkey
    jorb_event_pkey
    jorb_mailbox_pkey jorb_mailbox_pending_idx jorb_mailbox_consumed_idx
    jorb_history_pkey jorb_history_job_idx jorb_history_at_idx
    jorb_schedule_pkey jorb_schedule_name_key jorb_schedule_due_idx
    jorb_schedule_log_pkey jorb_schedule_log_idx
    jorb_schedule_log_retention_idx
    jorb_dag_pkey jorb_dag_retention_idx
    jorb_dependencies_pkey jorb_dependencies_depends_on_idx
""")

#: The jorbstate labels, in order — read from lifecycle, the ONE Python home
#: for the state list (tests bind that home to pg_enum and to db.JobState).
REQUIRED_ENUM_LABELS: dict[str, tuple[str, ...]] = {
    "jorbstate": lifecycle.JOB_STATES,
}

#: What doctor tells the operator to do about any of it. One string, because
#: every stale-schema message in the platform should name the same command.
MIGRATE_REMEDY = "run: pj-admin db migrate"

#: The errors PostgreSQL raises when the code addresses an object the database
#: does not have. Every one of them means the same thing anywhere in the
#: platform -- this database was installed from a different revision of the
#: base schema, or from none at all -- and none of them means the operator
#: typed something wrong.
#:
#: It lives HERE, next to the remedy and the required-shape manifest, because
#: the daemons need it too: pj's startup preflight and JobClient.enqueue both
#: have to recognise a missing schema, and a copy of this tuple inside the
#: admin CLI is a copy the daemons cannot import.
SCHEMA_ERRORS = (
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedColumnError,
    asyncpg.UndefinedFunctionError,
    asyncpg.UndefinedObjectError,
    asyncpg.InvalidSchemaNameError,
)

#: The whole sentence a daemon or client prints when it meets one of them.
#: Built from MIGRATE_REMEDY so the command is still written once.
SCHEMA_REMEDY = (
    f"The database schema is missing or out of date: {MIGRATE_REMEDY}, "
    f"then confirm with: pj-admin doctor"
)


def schema_error_hint(e: BaseException) -> str | None:
    """``SCHEMA_REMEDY`` when ``e`` is a missing-object error, else None.

    So a caller can append the remedy to its own message without importing
    the error tuple and reimplementing the isinstance check -- the retry
    loops in particular log the same line for a network blip and for a
    database that has no schema, and only the second one has an answer.
    """
    return SCHEMA_REMEDY if isinstance(e, SCHEMA_ERRORS) else None


@dataclass
class Migration:
    version: int
    name: str
    sql: str


@dataclass
class MigrationResult:
    """What ``migrate()`` actually did.

    ``recorded`` exists so "a fresh install is not double-migrated" is
    observable rather than assumed: on a fresh database every shipped
    migration lands in ``recorded`` (stamped, never executed) and ``applied``
    is empty, because the base schema already contains their effects.
    """

    installed_base: bool = False
    applied: list[int] = field(default_factory=list)
    recorded: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.installed_base or bool(self.applied)


def available_migrations() -> list[Migration]:
    """All migration files shipped with the package, ordered by version."""
    migrations_dir = _SQL_ROOT / "migrations"
    try:
        entries = list(migrations_dir.iterdir())
    except FileNotFoundError, NotADirectoryError:
        return []

    migrations = []
    for entry in entries:
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            migrations.append(
                Migration(
                    version=int(m.group(1)),
                    name=entry.name,
                    sql=entry.read_text(),
                )
            )
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        # Two files sharing a number would both be applied while only one is
        # recorded, and the other then re-applies on every migrate() forever.
        dupes = sorted({v for v in versions if versions.count(v) > 1})
        raise RuntimeError(
            f"duplicate migration version number(s) {dupes} in "
            f"{migrations_dir}: every NNN_*.sql must have a unique prefix"
        )
    return migrations


def base_schema_sql() -> str:
    """The whole current schema: the ordered base-schema files, concatenated.

    Lexical order IS the dependency order -- the numeric prefixes exist so
    that tables come before the functions that read them and the trigger
    files come last. One string, so the install runs in one transaction and
    a failure leaves nothing behind.
    """
    schema_dir = _SQL_ROOT / "schema"
    try:
        entries = sorted(schema_dir.iterdir(), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise RuntimeError(
            f"base schema directory {schema_dir} is missing from the "
            f"installed package -- the wheel was built without pyjobby/sql/"
            f"schema/, so there is nothing to install"
        ) from e
    sql = "".join(e.read_text() for e in entries if e.name.endswith(".sql"))
    if not sql.strip():
        raise RuntimeError(
            f"base schema directory {schema_dir} contains no SQL -- the "
            f"installed package is broken, so there is nothing to install"
        )
    return sql


async def applied_versions(conn: asyncpg.Connection) -> set[int]:
    """Versions already recorded in schema_migrations (empty if untracked)."""
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
    if not exists:
        return set()
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def missing_objects(conn: asyncpg.Connection) -> list[str]:
    """Names from the required-shape manifest that this database does not have.

    The question `pj-admin doctor` has to answer is not "which version does
    this database claim to be?" but "can the code that is running right now
    address it?" -- so this reads the catalog and compares it against the
    manifest, and a database that was never tracked by the migration runner is
    judged by what it actually contains.

    Returns sorted "kind name" strings, empty when the shape is complete.
    """
    missing: list[str] = []

    present_columns: dict[str, set[str]] = {}
    for row in await conn.fetch(
        """
        SELECT c.table_name, c.column_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        """
    ):
        present_columns.setdefault(row["table_name"], set()).add(row["column_name"])

    for table, columns in REQUIRED_COLUMNS.items():
        if table not in present_columns:
            missing.append(f"table {table}")
            continue
        missing += [
            f"column {table}.{c}" for c in sorted(columns - present_columns[table])
        ]

    present_views = {
        r["viewname"]
        for r in await conn.fetch(
            "SELECT viewname FROM pg_views WHERE schemaname = 'public'"
        )
    }
    missing += [f"view {v}" for v in sorted(REQUIRED_VIEWS - present_views)]

    present_functions = {
        r["proname"]
        for r in await conn.fetch(
            """SELECT p.proname FROM pg_proc p
                 JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'"""
        )
    }
    missing += [f"function {f}" for f in sorted(REQUIRED_FUNCTIONS - present_functions)]

    present_indexes = {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    }
    missing += [f"index {i}" for i in sorted(REQUIRED_INDEXES - present_indexes)]

    for enum_name, labels in REQUIRED_ENUM_LABELS.items():
        present_labels = {
            r["enumlabel"]
            for r in await conn.fetch(
                """SELECT e.enumlabel FROM pg_enum e
                     JOIN pg_type t ON t.oid = e.enumtypid
                     JOIN pg_namespace n ON n.oid = t.typnamespace
                    WHERE n.nspname = 'public' AND t.typname = $1""",
                enum_name,
            )
        }
        missing += [
            f"enum {enum_name}.{label}"
            for label in labels
            if label not in present_labels
        ]

    return sorted(missing)


async def migrate(conn: asyncpg.Connection) -> MigrationResult:
    """Install the base schema if needed, then apply pending migrations.

    Serialised fleet-wide by an advisory lock, so running this from every
    host's deploy step at the same instant is safe: one process installs or
    upgrades and the others wait, then find nothing left to do.

    A fresh database gets the base schema and has every shipped migration
    RECORDED without being run -- the base schema is the current schema, so
    re-applying the migrations that produced it would at best be wasted DDL
    and at worst (dropping and rebuilding an index, deleting orphan rows)
    real work against a database that never had the problem.
    """
    await conn.execute("SELECT pg_advisory_lock($1)", MIGRATE_LOCK_KEY)
    try:
        return await _migrate_locked(conn)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATE_LOCK_KEY)


async def _migrate_locked(conn: asyncpg.Connection) -> MigrationResult:
    result = MigrationResult()
    available = available_migrations()

    # Inside the lock: CREATE TABLE IF NOT EXISTS is not concurrency-safe
    # against another session creating the same table (it races on the
    # catalog), and neither is the base install below.
    await conn.execute(_TRACKING_TABLE)

    if not await conn.fetchval("SELECT to_regclass('public.jorb')"):
        logger.info("Installing base schema (jorb table not found)")
        async with conn.transaction():
            await conn.execute(base_schema_sql())
            for migration in available:
                await _record(conn, migration)
        result.installed_base = True
        result.recorded = [m.version for m in available]
        logger.info(
            f"Base schema installed; recorded migrations {result.recorded} "
            "as already contained in it"
        )
        return result

    done = await applied_versions(conn)
    for migration in available:
        if migration.version in done:
            continue
        logger.info(f"Applying migration {migration.name}")
        # One transaction per file: a migration that fails leaves the database
        # exactly as it was, and is not recorded.
        async with conn.transaction():
            await conn.execute(migration.sql)
            await _record(conn, migration)
        result.applied.append(migration.version)

    if result.applied:
        logger.info(f"Applied migrations: {result.applied}")
    else:
        logger.info("Database schema is up to date")

    return result


async def _record(conn: asyncpg.Connection, migration: Migration) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2) "
        "ON CONFLICT (version) DO NOTHING",
        migration.version,
        migration.name,
    )


async def status(conn: asyncpg.Connection) -> dict[str, Any]:
    """Report installed vs available migration versions, and the actual shape.

    ``missing`` is the load-bearing one: ``pending`` can only ever say "this
    database has not recorded migration N", and a database installed before
    the runner existed records nothing at all while still being stale.
    """
    available = available_migrations()
    done = await applied_versions(conn)
    has_jorb = bool(await conn.fetchval("SELECT to_regclass('public.jorb')"))
    return {
        "base_schema_installed": has_jorb,
        "applied": sorted(done),
        # Nothing is pending on a database with no base schema, and nothing is
        # missing from it either: migrate() will install the base schema --
        # which already contains every migration's effect -- and record them
        # without running one. Listing them as work-to-do would describe DDL that is
        # never going to execute, and listing sixty absent objects would bury
        # the single fact that matters, which is that there is no schema yet.
        "pending": (
            [m.version for m in available if m.version not in done] if has_jorb else []
        ),
        "available": [m.name for m in available],
        "missing": await missing_objects(conn) if has_jorb else [],
    }
