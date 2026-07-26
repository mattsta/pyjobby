"""Schema installation and migration runner.

The base schema (``pyjobby/sql/schema.sql``) plus the ordered migration files
(``pyjobby/sql/migrations/NNN_*.sql``) fully define the database. This module
applies them idempotently and records progress in a ``schema_migrations``
table, so a fresh install and an upgrade are the same operation:

    pj-admin db migrate

or programmatically::

    from pyjobby import db, migrations
    conn = await db.connect(**db_params)
    await migrations.migrate(conn)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

_SQL_ROOT = files("pyjobby") / "sql"

_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


@dataclass
class Migration:
    version: int
    name: str
    sql: str


def available_migrations() -> list[Migration]:
    """All migration files shipped with the package, ordered by version.

    Schema v1 is the current baseline, so this is empty today; future
    changes land as pyjobby/sql/migrations/NNN_*.sql files."""
    migrations_dir = _SQL_ROOT / "migrations"
    try:
        entries = list(migrations_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
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
    return migrations


def base_schema_sql() -> str:
    return (_SQL_ROOT / "schema.sql").read_text()


async def applied_versions(conn: asyncpg.Connection) -> set[int]:
    """Versions already recorded in schema_migrations (empty if untracked)."""
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
    if not exists:
        return set()
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def migrate(conn: asyncpg.Connection) -> list[int]:
    """Install the base schema if needed, then apply pending migrations.

    Safe to run repeatedly and safe on databases created before this runner
    existed: every migration file is itself idempotent (IF NOT EXISTS /
    CREATE OR REPLACE), so the first tracked run over an already-migrated
    database records versions without changing anything.

    Returns the list of migration versions applied (and newly recorded).
    """
    await conn.execute(_TRACKING_TABLE)

    # Fresh database? Install the base schema first.
    if not await conn.fetchval("SELECT to_regclass('public.jorb')"):
        logger.info("Installing base schema (jorb table not found)")
        await conn.execute(base_schema_sql())

    done = await applied_versions(conn)
    applied = []

    for migration in available_migrations():
        if migration.version in done:
            continue
        logger.info(f"Applying migration {migration.name}")
        # Files manage their own transactional/idempotency needs; record the
        # version only after the file executes without error.
        await conn.execute(migration.sql)
        await conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES ($1, $2) "
            "ON CONFLICT (version) DO NOTHING",
            migration.version,
            migration.name,
        )
        applied.append(migration.version)

    if applied:
        logger.info(f"Applied migrations: {applied}")
    else:
        logger.info("Database schema is up to date")

    return applied


async def status(conn: asyncpg.Connection) -> dict:
    """Report installed vs available migration versions."""
    available = available_migrations()
    done = await applied_versions(conn)
    has_jorb = bool(await conn.fetchval("SELECT to_regclass('jorb')"))
    return {
        "base_schema_installed": has_jorb,
        "applied": sorted(done),
        "pending": [m.version for m in available if m.version not in done],
        "available": [m.name for m in available],
    }
