"""Restart PostgreSQL under load: the reconnect-with-backoff claim, held to.

OPERATIONS.md: "Database was down. Workers/monitor/scheduler reconnect with
backoff automatically and re-prepare their statements; nothing needs a
restart." TROUBLESHOOTING.md adds that a database that goes away after
startup is not an incident. This is the only claim in the runbook that
cannot be tested against a private scratch database -- restarting the
server restarts it for every database on the instance -- so the whole
module is opt-in, and runs sequentially:

    poetry run pytest -m disruptive
"""

from __future__ import annotations

import getpass
import shutil
import subprocess

import asyncpg
import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until

pytestmark = [
    pytest.mark.ops,
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.disruptive,
]


async def restart_postgres(db_params: dict[str, str | int]) -> None:
    """Take the instance serving this database down and bring it back.

    A launchd-managed (brew services) server must be restarted through brew:
    stopping it behind launchd's back just races the KeepAlive respawn, and
    the pg_ctl start loses to it. Standalone servers get pg_ctl restart
    against their data directory. Skips (rather than fails) on any box the
    suite cannot do either: a remote database, or no tooling with the
    authority to bounce the server.
    """
    if db_params["host"] not in ("localhost", "127.0.0.1", "::1"):
        pytest.skip("database is not local; cannot restart it")

    service = _brew_postgres_service()
    if service is not None:
        result = subprocess.run(
            ["brew", "services", "restart", service],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.skip(f"brew services restart failed: {result.stderr.strip()}")
        return

    pg_ctl = shutil.which("pg_ctl")
    if pg_ctl is None:
        pytest.skip("no brew-managed service and no pg_ctl on PATH")
    data_dir = await _data_directory(db_params)
    if data_dir is None:
        pytest.skip("no connection with permission to read data_directory")
    result = subprocess.run(
        [pg_ctl, "-D", data_dir, "restart", "-m", "fast", "-w", "-t", "60"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(f"pg_ctl restart failed on this box: {result.stderr.strip()}")


def _brew_postgres_service() -> str | None:
    """The name of the STARTED brew-managed postgresql service, if any."""
    if shutil.which("brew") is None:
        return None
    listing = subprocess.run(
        ["brew", "services", "list"], capture_output=True, text=True, timeout=60
    )
    if listing.returncode != 0:
        return None
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and "postgres" in fields[0] and fields[1] == "started":
            return fields[0]
    return None


async def _data_directory(db_params: dict[str, str | int]) -> str | None:
    # The test role usually lacks pg_read_all_settings; a Homebrew-style
    # install makes the OS user a superuser, so fall back to that.
    for params in (
        dict(db_params),
        {
            "host": db_params["host"],
            "port": db_params["port"],
            "user": getpass.getuser(),
            "database": "postgres",
        },
    ):
        try:
            conn = await asyncpg.connect(**params)
        except OSError, asyncpg.PostgresError:
            continue
        try:
            return await conn.fetchval("SHOW data_directory")
        except asyncpg.InsufficientPrivilegeError:
            continue
        finally:
            await conn.close()
    return None


async def fetchval_fresh(db_params: dict[str, str | int], sql: str, *args: object):
    """One query on its own connection, None while the server is unreachable.

    The test's own pool dies in the restart too; probing through fresh
    connections is what lets the assertions span the outage.
    """
    try:
        conn = await asyncpg.connect(**db_params, timeout=5)
    except OSError, asyncpg.PostgresError:
        return None
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


class TestPostgresRestartUnderLoad:
    async def test_fleet_survives_a_database_restart_without_process_restarts(
        self, fleet, db_pool, db_params, unique_queue
    ):
        monitor = fleet.monitor()
        worker = fleet.worker(unique_queue)
        scheduler = fleet.scheduler()

        client = JobClient(pool=db_pool)
        # One job caught mid-run by the outage, several queued behind it.
        in_flight = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=10
        )
        queued = [
            await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=n)
            for n in range(5)
        ]
        await wait_for_job_state(db_pool, in_flight, ("running",), timeout=30)

        await restart_postgres(db_params)

        # Every process survives the outage in place...
        assert worker.alive()
        assert monitor.alive()
        assert scheduler.alive()

        # ...and the whole workload drains with no intervention: the
        # mid-run job's completion write lands on a reconnected connection,
        # and the queued jobs are claimed after it.
        all_ids = [in_flight, *queued]
        await wait_until(
            lambda: fetchval_fresh(
                db_params,
                "SELECT count(*) = 6 FROM jorb WHERE id = ANY($1) "
                "AND state = 'finished'",
                all_ids,
            ),
            describe="entire workload finished after the restart",
            timeout=90,
            interval=0.5,
        )
        assert worker.alive()
        assert monitor.alive()
        assert scheduler.alive()
