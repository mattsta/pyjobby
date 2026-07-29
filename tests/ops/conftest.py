"""Fixtures for process-level operational validation.

Everything in tests/ops runs the platform the way an operator does: real
``pj`` / ``pj-monitor`` / ``pj-scheduler`` / ``pj-admin`` processes spawned
through ``poetry run``, against this test session's database, configured
through a real pyjobby.toml. The suite exists to hold OPERATIONS.md and
TROUBLESHOOTING.md to their word — every scenario here induces a documented
failure (or documented recovery) and asserts the documented outcome, so a
test failing means either a real bug or a doc that lies.

The processes live in their own process groups so a test can deliver the
exact signal an operator's init system would (SIGTERM to the launcher, or
SIGKILL to one worker child found via the ``jorb_worker`` registry) without
pytest or the harness being collateral.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.schema_fixtures import ScratchDatabases, dsn_from

REPO_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def ops_config(tmp_path: Path, db_params: dict[str, str | int]) -> Path:
    """A real pyjobby.toml pointing at this session's database.

    Workers, the scheduler and the web surfaces accept ONLY a config file
    (no DSN env var), so process-level tests need one on disk.
    """
    config = tmp_path / "pyjobby.toml"
    config.write_text(
        "[db_params]\n"
        f'database = "{db_params["database"]}"\n'
        f'user = "{db_params["user"]}"\n'
        f'password = "{db_params["password"]}"\n'
        f'host = "{db_params["host"]}"\n'
        f"port = {db_params['port']}\n"
    )
    return config


class OpsProc:
    """One spawned platform process (launcher) and its log file."""

    def __init__(self, name: str, argv: list[str], log_path: Path):
        self.name = name
        self.log_path = log_path
        self._log_handle = log_path.open("ab")
        self.popen = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None

    def log_text(self) -> str:
        return self.log_path.read_text(errors="replace")

    def signal_launcher(self, signum: int) -> None:
        """Deliver to the launcher only — exercises its broadcast-to-children."""
        self.popen.send_signal(signum)

    def signal_group(self, signum: int) -> None:
        """Deliver to the whole process group, as an init system's kill does."""
        os.killpg(os.getpgid(self.popen.pid), signum)

    def wait(self, timeout: float = 30) -> int:
        code = self.popen.wait(timeout=timeout)
        self._log_handle.close()
        return code

    def destroy(self) -> None:
        """Teardown backstop: SIGKILL the whole group, reap, close the log."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.popen.wait(timeout=10)
        self._log_handle.close()


class Fleet:
    """Spawns platform processes and guarantees none outlive the test."""

    def __init__(self, config: Path, tmp_path: Path):
        self.config = config
        self.tmp_path = tmp_path
        self.procs: list[OpsProc] = []
        self._counter = 0

    def spawn(self, entry_point: str, *args: str, name: str | None = None) -> OpsProc:
        self._counter += 1
        name = name or f"{entry_point}-{self._counter}"
        proc = OpsProc(
            name,
            ["poetry", "run", entry_point, *args],
            self.tmp_path / f"{name}.log",
        )
        self.procs.append(proc)
        return proc

    def worker(
        self,
        queue: str,
        *args: str,
        workers: int = 1,
        check_interval: float = 0.2,
        name: str | None = None,
    ) -> OpsProc:
        return self.spawn(
            "pj",
            "-c",
            str(self.config),
            "--queue",
            queue,
            "--workers",
            str(workers),
            "--check-interval",
            str(check_interval),
            *args,
            name=name,
        )

    def monitor(
        self,
        *args: str,
        check_interval: float = 0.5,
        liveness_grace: float = 3.0,
        claimed_grace: float = 5.0,
        name: str | None = None,
    ) -> OpsProc:
        return self.spawn(
            "pj-monitor",
            "--config",
            str(self.config),
            "--check-interval",
            str(check_interval),
            "--liveness-grace",
            str(liveness_grace),
            "--claimed-grace",
            str(claimed_grace),
            *args,
            name=name,
        )

    def scheduler(
        self, *args: str, poll_interval: int = 1, name: str | None = None
    ) -> OpsProc:
        return self.spawn(
            "pj-scheduler",
            "-c",
            str(self.config),
            "--poll-interval",
            str(poll_interval),
            *args,
            name=name,
        )

    def destroy_all(self) -> None:
        for proc in self.procs:
            proc.destroy()


@pytest.fixture
def fleet(ops_config: Path, tmp_path: Path) -> Iterator[Fleet]:
    the_fleet = Fleet(ops_config, tmp_path)
    yield the_fleet
    the_fleet.destroy_all()


@pytest.fixture
def admin(
    db_params: dict[str, str | int],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run one ``pj-admin`` command, capturing output.

    Defaults to the session database; pass ``dsn=`` for another database, or
    ``dsn=None`` to run with no --dsn at all (config-error scenarios).
    """

    def run(
        *args: str, dsn: str | None = "session"
    ) -> subprocess.CompletedProcess[str]:
        argv = ["poetry", "run", "pj-admin"]
        if dsn == "session":
            argv += ["--dsn", dsn_from(db_params)]
        elif dsn is not None:
            argv += ["--dsn", dsn]
        return subprocess.run(
            argv + list(args),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run


@pytest.fixture
async def scratch(
    db_params: dict[str, str | int],
) -> AsyncIterator[ScratchDatabases]:
    """Throwaway databases for schema-drift scenarios (see schema_fixtures).

    The session database is migrated and shared with every other test, so
    scenarios that drop schema objects or start with no schema at all get
    their own databases, dropped together at teardown.
    """
    factory = ScratchDatabases(db_params)
    try:
        yield factory
    finally:
        await factory.close()


async def run_sql(params: dict[str, str | int], sql: str) -> object:
    """Execute one statement against an arbitrary database, returning its status."""
    conn = await asyncpg.connect(**params)
    try:
        return await conn.execute(sql)
    finally:
        await conn.close()


async def wait_until(
    predicate: Callable[[], Awaitable[object]],
    *,
    timeout: float = 20,
    interval: float = 0.1,
    describe: str = "condition",
) -> object:
    """Poll an async predicate until it returns something truthy."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval)
    raise TimeoutError(f"{describe} not reached within {timeout}s (last: {last!r})")


async def registered_workers(
    pool: asyncpg.Pool, queue: str, live: bool | None = True
) -> list[asyncpg.Record]:
    """Worker registry rows for a queue (live means not shut down)."""
    rows = await pool.fetch(
        "SELECT * FROM jorb_worker WHERE queue = $1 ORDER BY id", queue
    )
    if live is None:
        return list(rows)
    return [row for row in rows if (row["shutdown_at"] is None) == live]


@pytest.fixture
async def job_row(db_pool: asyncpg.Pool) -> Callable[[int], Awaitable[asyncpg.Record]]:
    async def fetch(job_id: int) -> asyncpg.Record:
        return await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

    return fetch
