"""Shared machinery for the tests that exercise schema INSTALLATION itself.

Not a test module (pytest collects `test_*.py` only). It lives apart from
tests/conftest.py because everything here is about databases the session
fixture cannot provide: the session database is dropped and reinstalled from
the current base schema whenever it changes, which is precisely the
operation these tests need to observe from the outside -- installing into a
database that is empty, damaging one on purpose, or racing two installers.

Two things are shared, by the files that need them
(tests/test_migrations.py, tests/test_cli_doctor.py, tests/test_cli_errors.py):

* the catalog snapshot query, so "the same schema" means the same thing
  everywhere it is asserted;
* a throwaway-database factory, because damaging a schema cannot be done to
  a database other tests are using.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

import asyncpg

_SQL = Path(__file__).parent / "sql"

CATALOG_SQL = (_SQL / "catalog_snapshot.sql").read_text()


async def catalog(conn: asyncpg.Connection) -> list[str]:
    """Every catalog object in `public`, as sorted comparable text."""
    return [r["line"] for r in await conn.fetch(CATALOG_SQL)]


async def drop_database(admin: asyncpg.Connection, name: str) -> None:
    """DROP a scratch database, waiting out whatever is still attached.

    Plain DROP with retries, not ``WITH (FORCE)``: FORCE terminates every
    backend on the target, and when one of them is an autovacuum worker that
    takes superuser -- an intermittent InsufficientPrivilegeError that
    either failed the test (where the drop was unguarded) or silently LEAKED
    the database (where it was wrapped in suppress; dozens accumulated). A
    just-disconnected client's backend or an autovacuum pass clears in
    milliseconds, so a short retry outlasts both. FORCE remains only as the
    last resort, for a backend that genuinely will not go away on its own.
    """
    for _ in range(40):
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
            return
        except asyncpg.ObjectInUseError:
            await asyncio.sleep(0.05)
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class ScratchDatabases:
    """Factory for throwaway databases, all dropped together at teardown.

    `install` selects the starting shape:
      * "current" -- run the migration runner, i.e. what a new deployment gets
      * None      -- an empty database
    """

    def __init__(self, db_params: dict) -> None:
        self._params = db_params
        self._admin: asyncpg.Connection | None = None
        self._created: list[str] = []

    async def _connect_admin(self) -> asyncpg.Connection:
        if self._admin is None:
            self._admin = await asyncpg.connect(**self._params)
        return self._admin

    async def create(
        self, *, install: str | None = "current", prefix: str = "pj_scratch"
    ) -> dict:
        """Create a database and return connection params for it."""
        admin = await self._connect_admin()
        name = f"{prefix}_{uuid.uuid4().hex[:12]}"
        await admin.execute(f'CREATE DATABASE "{name}"')
        self._created.append(name)
        params = {**self._params, "database": name}

        if install is not None:
            conn = await asyncpg.connect(**params)
            try:
                if install == "current":
                    from pyjobby import migrations

                    await migrations.migrate(conn)
                else:  # pragma: no cover - programming error in a test
                    raise ValueError(f"unknown install shape: {install!r}")
            finally:
                await conn.close()
        return params

    async def close(self) -> None:
        if self._admin is None:
            return
        for name in self._created:
            # Best effort: a leaked scratch database must not fail a test that
            # otherwise passed.
            with contextlib.suppress(asyncpg.PostgresError):
                await drop_database(self._admin, name)
        await self._admin.close()
        self._admin = None


def dsn_from(params: dict) -> str:
    return (
        f"postgresql://{params['user']}:{params['password']}"
        f"@{params['host']}:{params['port']}/{params['database']}"
    )
