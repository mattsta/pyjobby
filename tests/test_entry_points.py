"""Every console script must start as a real process and do its job.

These tests exist because coverage cannot see wiring: this platform once
shipped `scheduler.py` at 97% coverage with no entry point at all (cron
never fired) and `timeout_monitor.py` at 99% while being a complete no-op.
Each test here launches the installed console script in its own process
group and asserts an OBSERVABLE EFFECT in the database or over the network
— never just that `--help` parses.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest

from pyjobby.procs import (
    daemon,
    dsn_from,
    free_port,
    port_is_open,
    terminate,
    wait_until,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def dsn(db_params: dict) -> str:
    return dsn_from(db_params)


# ============================================================================
# pj — the worker fleet launcher
# ============================================================================


class TestWorkerEntryPoint:
    async def test_pj_executes_a_job_end_to_end(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """`pj` launches workers that claim and finish real jobs."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue)
               VALUES ('tests.dxe_jobs.OkJob', $1, $2) RETURNING id""",
            {"x": 4},
            unique_queue,
        )

        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--workers",
            "1",
            "--check-interval",
            "1",
        ):
            row = await wait_until(
                lambda: db_pool.fetchrow(
                    "SELECT state, result FROM jorb WHERE id = $1 AND state = 'finished'",
                    job_id,
                ),
                what="job finished by a pj-launched worker",
            )
        assert row["result"] == {"doubled": 8}

    async def test_pj_registers_workers_in_the_registry(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """Launched workers appear in jorb_worker and deregister on shutdown."""
        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--workers",
            "2",
            "--check-interval",
            "1",
        ):
            workers = await wait_until(
                lambda: db_pool.fetch(
                    """SELECT * FROM jorb_worker
                       WHERE queue = $1 AND shutdown_at IS NULL""",
                    unique_queue,
                ),
                what="two workers registered",
            )
            assert len(workers) >= 1
            assert all(w["pid"] for w in workers)

        # after the group is reaped, the monitor would retire them; the
        # graceful path marks shutdown_at itself
        await wait_until(
            lambda: db_pool.fetchval(
                """SELECT count(*) = 0 FROM jorb_worker
                   WHERE queue = $1 AND shutdown_at IS NULL""",
                unique_queue,
            ),
            timeout=20,
            what="workers deregistered after shutdown",
        )


# ============================================================================
# pj — which queues the fleet actually lands on
#
# `--workers N` used to be a TOTAL, with the queue list padded out to it
# using the literal "default": `pj --queue emails --workers 4` started one
# worker on `emails` and three on a queue the operator never named. These
# tests drive the real launcher and read jorb_worker, because the registry is
# the only place that answers "where did those processes actually go".
# ============================================================================


async def registered_fleet(db_pool, marker: str, at_least: int = 1):
    """Live registry rows for the workers tagged with this test's marker.

    A `--cap` unique to one launch is what makes the assertion exact under
    xdist: every other test's workers are invisible to it, including any on
    the shared `default` queue.
    """
    rows = await db_pool.fetch(
        """SELECT queue, pid FROM jorb_worker
           WHERE $1 = ANY(capabilities) AND shutdown_at IS NULL""",
        marker,
    )
    return rows if len(rows) >= at_least else None


class TestWorkerFleetPlacement:
    async def test_workers_flag_puts_every_worker_on_the_named_queue(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """One `--queue`, `--workers 4`: four workers, all on that queue."""
        marker = f"fleet:{uuid.uuid4().hex}"
        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--cap",
            marker,
            "--workers",
            "4",
            "--check-interval",
            "1",
        ):
            await wait_until(
                lambda: registered_fleet(db_pool, marker, 4),
                what="four workers registered",
            )
            # settle, so a fifth process landing anywhere would be counted
            await asyncio.sleep(1.5)
            rows = await registered_fleet(db_pool, marker)

        assert Counter(r["queue"] for r in rows) == {unique_queue: 4}
        assert len({r["pid"] for r in rows}) == 4  # four real processes

    async def test_workers_flag_is_per_queue_for_several_queues(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """Two `--queue`s and `--workers 2` is two workers on EACH: the flag
        is per queue, so naming another queue never changes the capacity of
        the queues already named."""
        marker = f"fleet:{uuid.uuid4().hex}"
        other_queue = f"{unique_queue}_b"
        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--queue",
            other_queue,
            "--cap",
            marker,
            "--workers",
            "2",
            "--check-interval",
            "1",
        ):
            await wait_until(
                lambda: registered_fleet(db_pool, marker, 4),
                what="two workers on each of two queues",
            )
            await asyncio.sleep(1.5)
            rows = await registered_fleet(db_pool, marker)

        assert Counter(r["queue"] for r in rows) == {unique_queue: 2, other_queue: 2}

    async def test_repeating_a_queue_name_asks_for_nothing_extra(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """`--queue Q --queue Q --workers 2` is two workers, not four.

        This is exactly the invocation `pj-bench e2e` builds (it repeats
        `--queue` once per worker to work around the old padding), so the
        de-duplication is what keeps the benchmark measuring the fleet size
        it asked for."""
        marker = f"fleet:{uuid.uuid4().hex}"
        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--queue",
            unique_queue,
            "--cap",
            marker,
            "--workers",
            "2",
            "--check-interval",
            "1",
        ):
            await wait_until(
                lambda: registered_fleet(db_pool, marker, 2),
                what="two workers registered",
            )
            await asyncio.sleep(1.5)
            rows = await registered_fleet(db_pool, marker)

        assert Counter(r["queue"] for r in rows) == {unique_queue: 2}


class TestWorkerPriorityCeiling:
    async def test_max_prio_claims_what_a_default_worker_will_not(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """`--max-prio` reaches the worker; without it, nothing does.

        `runAndDone` dropped the ceiling entirely, so every `pj`-launched
        worker ran at the dataclass default and a job above it was
        unclaimable by any process an operator could start.
        """
        config = str(write_config(tmp_path, dsn))
        low_urgency = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, prio)
               VALUES ('tests.dxe_jobs.OkJob', $1, $2, 5000) RETURNING id""",
            {"x": 1},
            unique_queue,
        )
        normal = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, prio)
               VALUES ('tests.dxe_jobs.OkJob', $1, $2, 100) RETURNING id""",
            {"x": 2},
            unique_queue,
        )

        async with daemon(
            "pj",
            "--config",
            config,
            "--queue",
            unique_queue,
            "--workers",
            "1",
            "--check-interval",
            "1",
        ):
            await wait_until(
                lambda: db_pool.fetchrow(
                    "SELECT id FROM jorb WHERE id = $1 AND state = 'finished'",
                    normal,
                ),
                what="the default-ceiling worker ran the prio-100 job",
            )
            assert (
                await db_pool.fetchval(
                    "SELECT state FROM jorb WHERE id = $1", low_urgency
                )
                == "queued"
            )

        async with daemon(
            "pj",
            "--config",
            config,
            "--queue",
            unique_queue,
            "--workers",
            "1",
            "--max-prio",
            "5000",
            "--check-interval",
            "1",
        ):
            row = await wait_until(
                lambda: db_pool.fetchrow(
                    "SELECT state, result FROM jorb WHERE id = $1 "
                    "AND state = 'finished'",
                    low_urgency,
                ),
                what="the raised-ceiling worker ran the prio-5000 job",
            )
        assert row["result"] == {"doubled": 2}

    async def test_an_idle_worker_reports_work_above_its_ceiling(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """The other half of the black hole: jobs that got in anyway.

        The client refuses to enqueue above its declared ceiling, but raw
        SQL, another tool or a schedule can still create one. An idle worker
        says so rather than sitting quietly next to work it will never take.
        """
        await db_pool.execute(
            """INSERT INTO jorb (job_class, kwargs, queue, prio)
               VALUES ('tests.dxe_jobs.OkJob', $1, $2, 4200)""",
            {"x": 3},
            unique_queue,
        )

        async with daemon(
            "pj",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--queue",
            unique_queue,
            "--workers",
            "1",
            "--check-interval",
            "1",
            capture=True,
        ) as proc:
            await asyncio.sleep(3)
            # reap the whole group first: the workers share the launcher's
            # stderr pipe, so communicate() only returns once they are gone
            terminate(proc)
            _, err = proc.communicate(timeout=15)

        log = err.decode(errors="replace")
        assert "ABOVE this worker's priority ceiling of 1000" in log
        assert "the lowest blocked one is 4200" in log


# ============================================================================
# pj-scheduler — the cron executor (this is the subsystem that had no runtime)
# ============================================================================


class TestSchedulerEntryPoint:
    async def test_pj_scheduler_fires_a_due_schedule(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """A due schedule produces a job — the whole point of the daemon."""
        schedule_id = await db_pool.fetchval(
            """INSERT INTO jorb_schedule
                   (name, job_class, kwargs, queue, cron_expr, next_run)
               VALUES ($1, 'tests.dxe_jobs.OkJob', $2, $3, '* * * * *', $4)
               RETURNING id""",
            f"sched_{unique_queue}",
            {"x": 1},
            unique_queue,
            datetime.now(UTC) - timedelta(minutes=1),
        )

        async with daemon(
            "pj-scheduler",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--poll-interval",
            "1",
        ):
            job = await wait_until(
                lambda: db_pool.fetchrow(
                    "SELECT * FROM jorb WHERE queue = $1", unique_queue
                ),
                what="scheduler enqueued a job for the due schedule",
            )

        assert job["job_class"] == "tests.dxe_jobs.OkJob"
        assert job["admin_data"]["schedule_id"] == str(schedule_id)

        # the schedule's bookkeeping advanced
        sched = await db_pool.fetchrow(
            "SELECT run_count, last_run, next_run FROM jorb_schedule WHERE id = $1",
            schedule_id,
        )
        assert sched["run_count"] >= 1
        assert sched["last_run"] is not None
        assert sched["next_run"] > datetime.now(UTC) - timedelta(minutes=1)

    async def test_pj_scheduler_leaves_disabled_schedules_alone(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        await db_pool.execute(
            """INSERT INTO jorb_schedule
                   (name, job_class, queue, cron_expr, next_run, enabled)
               VALUES ($1, 'tests.dxe_jobs.OkJob', $2, '* * * * *', $3, FALSE)""",
            f"disabled_{unique_queue}",
            unique_queue,
            datetime.now(UTC) - timedelta(minutes=5),
        )

        async with daemon(
            "pj-scheduler",
            "--config",
            str(write_config(tmp_path, dsn)),
            "--poll-interval",
            "1",
            startup=2.0,
        ):
            count = await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
        assert count == 0


# ============================================================================
# pj-monitor — the reaper (this is the subsystem that was a no-op)
# ============================================================================


class TestMonitorEntryPoint:
    async def test_pj_monitor_reclaims_a_dead_workers_job(
        self, db_pool, unique_queue, dsn
    ):
        """A stale-heartbeat worker's in-flight job is requeued by the daemon."""
        worker_id = await db_pool.fetchval(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ('gone-host', 999999, $1, $2) RETURNING id""",
            unique_queue,
            datetime.now(UTC) - timedelta(minutes=10),
        )
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, claimed_by, worker_host)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running', $2, 'gone-host')
               RETURNING id""",
            unique_queue,
            worker_id,
        )

        async with daemon(
            "pj-monitor",
            "--dsn",
            dsn,
            "--check-interval",
            "1",
            "--liveness-grace",
            "5",
        ):
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT state = 'queued' FROM jorb WHERE id = $1", job_id
                ),
                what="monitor requeued the dead worker's job",
            )
            # and retired the worker so it stops being rescanned
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT shutdown_at IS NOT NULL FROM jorb_worker WHERE id = $1",
                    worker_id,
                ),
                what="monitor retired the stale worker",
            )

    async def test_pj_monitor_enforces_timeouts(self, db_pool, unique_queue, dsn):
        """A running job past its deadline is retried by the daemon."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, admin_data, timeout_at)
               VALUES ('tests.dxe_jobs.SlowJob', $1, 'running', $2, $3)
               RETURNING id""",
            unique_queue,
            {"timeout_seconds": 1, "on_timeout": "retry", "max_retries": 5},
            datetime.now(UTC) - timedelta(seconds=30),
        )

        async with daemon("pj-monitor", "--dsn", dsn, "--check-interval", "1"):
            row = await wait_until(
                lambda: db_pool.fetchrow(
                    """SELECT state, error_count, error_message FROM jorb
                       WHERE id = $1 AND state = 'queued'""",
                    job_id,
                ),
                what="monitor requeued the timed-out job",
            )
        assert row["error_count"] == 1
        assert "Timeout" in row["error_message"]


# ============================================================================
# pj-web — admin UI + Prometheus metrics
# ============================================================================


class TestWebEntryPoint:
    async def test_pj_web_serves_pages_and_metrics(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        port = await free_port()
        await db_pool.execute(
            "INSERT INTO jorb (job_class, queue) VALUES ('tests.dxe_jobs.OkJob', $1)",
            unique_queue,
        )

        async with daemon(
            "pj-web",
            str(write_config(tmp_path, dsn)),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ):
            await wait_until(
                lambda: port_is_open("127.0.0.1", port), what="pj-web listening"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as resp:
                    assert resp.status == 200
                    assert "Pyjobby" in await resp.text()

                async with session.get(f"http://127.0.0.1:{port}/metrics") as resp:
                    assert resp.status == 200
                    assert resp.headers["Content-Type"].startswith("text/plain")
                    body = await resp.text()

        # metrics reflect the real database, not a stub
        assert "pyjobby_jobs_by_state" in body
        assert unique_queue in body

    async def test_pj_web_api_returns_live_data(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        port = await free_port()
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue) VALUES ('tests.dxe_jobs.OkJob', $1)
               RETURNING id""",
            unique_queue,
        )

        async with daemon(
            "pj-web",
            str(write_config(tmp_path, dsn)),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ):
            await wait_until(
                lambda: port_is_open("127.0.0.1", port), what="pj-web listening"
            )
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{port}/api/jobs/{job_id}/history"
                async with session.get(url) as resp:
                    assert resp.status == 200
                    history = json.loads(await resp.text())

        assert [h["event"] for h in history] == ["enqueued"]


# ============================================================================
# pj-ws — realtime websocket dashboard
# ============================================================================


class TestWebsocketEntryPoint:
    async def test_pj_ws_delivers_a_live_job_event(
        self, db_pool, unique_queue, dsn, tmp_path
    ):
        """Database state reaches a subscribed websocket, through the daemon.

        It arrives as an aggregate snapshot on an interval, not as a message
        per transition: the per-transition channel was deleted (see
        tests/test_ws_snapshot.py for why and for what replaced it). The
        interval is turned down here so the test does not wait a full second.
        """
        port = await free_port()

        async with daemon(
            "pj-ws",
            str(write_config(tmp_path, dsn)),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--snapshot-interval",
            "0.1",
        ):
            await wait_until(
                lambda: port_is_open("127.0.0.1", port), what="pj-ws listening"
            )

            async with (
                aiohttp.ClientSession() as session,
                session.ws_connect(f"http://127.0.0.1:{port}/ws") as ws,
            ):
                welcome = await ws.receive_json(timeout=5)
                assert welcome["event"] == "connected"

                await ws.send_json({"action": "subscribe", "channels": ["jobs"]})
                ack = await ws.receive_json(timeout=5)
                assert ack["event"] == "subscribed"

                # real rows in a real queue; the snapshot must report them
                await db_pool.execute(
                    """INSERT INTO jorb (job_class, queue, state)
                       VALUES ('tests.dxe_jobs.OkJob', $1, 'queued'),
                              ('tests.dxe_jobs.OkJob', $1, 'running')""",
                    unique_queue,
                )

                for _ in range(20):
                    msg = await ws.receive_json(timeout=5)
                    if msg.get("event") != "dashboard":
                        continue
                    stats = msg["data"]["queues"].get(unique_queue)
                    if stats and stats["running"] == 1:
                        assert stats["queued"] == 1
                        assert stats["backlog"] == 1
                        break
                else:
                    pytest.fail("no dashboard snapshot delivered")

    async def test_pj_ws_health_endpoint(self, dsn, tmp_path):
        port = await free_port()
        async with daemon(
            "pj-ws",
            str(write_config(tmp_path, dsn)),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ):
            await wait_until(
                lambda: port_is_open("127.0.0.1", port), what="pj-ws listening"
            )
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{port}/health") as resp,
            ):
                assert resp.status == 200
                health = await resp.json()
        assert health["status"] == "healthy"


# ============================================================================
# pj-admin db migrate — a fresh database becomes usable
# ============================================================================


class TestMigrateEntryPoint:
    async def test_migrate_makes_a_fresh_database_usable(self, db_params):
        """Install into a brand-new database and enqueue against it."""
        import asyncpg

        admin_dsn = (
            f"postgresql://{db_params['user']}:{db_params['password']}"
            f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
        )
        fresh_db = f"pyjobby_fresh_{datetime.now(UTC).strftime('%H%M%S%f')}"

        # create the database from the current one (no superuser needed:
        # the test role owns its own databases)
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'CREATE DATABASE "{fresh_db}"')
        finally:
            await admin.close()

        fresh_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{fresh_db}"
        try:
            proc = await run_to_completion(
                "pj-admin", "--dsn", fresh_dsn, "db", "migrate"
            )
            assert proc.returncode == 0, proc.stderr

            conn = await asyncpg.connect(fresh_dsn)
            try:
                # the schema is complete enough to run the platform
                job_id = await conn.fetchval(
                    """INSERT INTO jorb (job_class, queue)
                       VALUES ('tests.dxe_jobs.OkJob', 'default') RETURNING id"""
                )
                assert job_id is not None
                # and the history trigger is installed
                events = await conn.fetch(
                    "SELECT event FROM jorb_history WHERE job_id = $1", job_id
                )
                assert [e["event"] for e in events] == ["enqueued"]

                status = await run_to_completion(
                    "pj-admin", "--dsn", fresh_dsn, "db", "status"
                )
                assert "Base schema installed: yes" in status.stdout
            finally:
                await conn.close()
        finally:
            admin = await asyncpg.connect(admin_dsn)
            try:
                await admin.execute(
                    f'DROP DATABASE IF EXISTS "{fresh_db}" WITH (FORCE)'
                )
            finally:
                await admin.close()


# ============================================================================
# helpers
# ============================================================================


def write_config(tmp_path, dsn: str) -> object:
    """Write a pyjobby.conf.py pointing at `dsn` and return its path."""
    from urllib.parse import unquote, urlparse

    p = urlparse(dsn)
    config = tmp_path / "pyjobby.conf.py"
    config.write_text(
        "db_params = {\n"
        f"    'host': {p.hostname!r},\n"
        f"    'port': {p.port or 5432!r},\n"
        f"    'user': {unquote(p.username or '')!r},\n"
        f"    'password': {unquote(p.password or '')!r},\n"
        f"    'database': {(p.path or '').lstrip('/')!r},\n"
        "}\n"
        "web_listen = None\n"
    )
    return config


async def run_to_completion(*args: str, timeout: float = 30):
    """Run a console script to completion and return the finished process."""
    import asyncio
    import os
    import subprocess
    import sys

    from pyjobby.procs import REPO_ROOT

    bin_dir = os.path.join(REPO_ROOT, ".venv", "bin")
    executable = os.path.join(bin_dir, args[0])
    if not os.path.exists(executable):
        executable = os.path.join(os.path.dirname(sys.executable), args[0])

    def _run():
        return subprocess.run(
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=timeout,
        )

    return await asyncio.to_thread(_run)
