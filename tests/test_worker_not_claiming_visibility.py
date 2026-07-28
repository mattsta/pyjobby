"""A worker that stops claiming must stop looking healthy.

``tests/test_job_timeout_escapes.py`` pins the *behaviour*: a worker whose
job-thread pool is full of threads abandoned by timed-out synchronous jobs
refuses to claim, and says so at ERROR. That log line was the only place the
condition existed. The worker kept heartbeating, so ``jorb_worker`` showed it
alive, ``pj-admin doctor`` reported a healthy fleet, ``/metrics`` counted it
as capacity, and the dashboard agreed — an operator saw a healthy worker
silently doing no work, and the only way to find out otherwise was to read
that one worker's log.

This file pins the *visibility*. Every test drives the real condition with a
real worker and real jobs that block past their deadline, because a test that
writes ``jorb_worker`` by hand proves the reporting and nothing about the
detection — and detection is the half that was missing.

The two counts (``job_threads``, ``job_threads_abandoned``) are asserted as
exact values throughout: a worker at 7 of 8 and a worker at 0 of 8 are
different situations, and "not None" cannot tell them apart.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from pyjobby.admin_api import AdminAPI
from pyjobby.cli import DOCTOR_THREADS_REMEDY
from pyjobby.pj import WORKER_HEARTBEAT_SQL, JobSystem

from .conftest import wait_for_job_state
from .test_cli_doctor import dsn_for, parse_checks, run_doctor
from .test_job_timeout_escapes import enqueue
from .test_metrics_scrape_cost import parse_samples

pytestmark = pytest.mark.asyncio

#: The job that blocks far past its deadline in a thread nothing can
#: interrupt. Imported rather than re-declared: it is the same escape, and a
#: second copy of it would be a second thing to keep true.
BLOCKER = "tests.test_job_timeout_escapes.BlocksLongPastItsDeadlineJob"
PROMPT = "tests.test_job_timeout_escapes.PromptJob"

#: How long a blocker's thread keeps running after the job it belonged to has
#: already been recorded as timed out. Long enough that every assertion in a
#: test lands while the worker is still refusing, short enough that the
#: worker recovers inside the test's own teardown.
BLOCK_SECONDS = 12.0

#: Fast enough that the registry reflects a change within a poll or two.
FAST_HEARTBEAT = 0.25


async def fill_the_pool(
    live_worker: Any, db_pool: Any, queue: str, *, job_threads: int
) -> JobSystem:
    """Drive a real worker into the refusing state and return it.

    ``job_threads`` synchronous jobs each block for ``BLOCK_SECONDS`` under a
    1s deadline, so each is recorded as timed out while its thread keeps its
    pool slot. A worker runs one job at a time, so once the last one has
    crashed the worker is holding exactly ``job_threads`` threads it is not
    waiting for: the pool is full of abandoned work and it will claim nothing.
    """
    worker = await live_worker(
        job_threads=job_threads, heartbeat_interval=FAST_HEARTBEAT
    )

    blockers = [
        await enqueue(
            db_pool,
            queue,
            BLOCKER,
            {"block": BLOCK_SECONDS},
            {"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 5},
        )
        for _ in range(job_threads)
    ]
    for job_id in blockers:
        row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=30)
        assert row["error_message"] == "Job timed out after 1s"

    assert worker._abandoned_job_threads() == job_threads
    return worker


async def registry_row(db_pool: Any, worker: JobSystem) -> Any:
    """The worker's own ``jorb_worker`` row."""
    return await db_pool.fetchrow(
        "SELECT * FROM jorb_worker WHERE id = $1", worker.worker_id
    )


async def wait_for_abandoned(db_pool: Any, worker: JobSystem, want: int) -> Any:
    """Poll the registry until the heartbeat has published ``want``."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = await registry_row(db_pool, worker)
        if row["job_threads_abandoned"] == want:
            return row
        await asyncio.sleep(0.1)
    row = await registry_row(db_pool, worker)
    raise AssertionError(
        f"registry never published {want} abandoned job thread(s); "
        f"last row: {row['job_threads_abandoned']}/{row['job_threads']}"
    )


# ============================================================================
# 1. The registry carries the condition
# ============================================================================


class TestRegistry:
    async def test_a_refusing_worker_records_both_counts(
        self, live_worker, unique_queue, db_pool
    ):
        """Two abandoned threads of two, published while it refuses.

        This is the row every other consumer in this file reads, so it is
        asserted as the exact pair rather than as a saturation boolean: the
        pool size is a per-worker choice (``--job-threads``), and 2 abandoned
        threads means "dead worker" here and "plenty of headroom" on a
        default worker of 8.
        """
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)

        row = await wait_for_abandoned(db_pool, worker, 2)

        assert row["job_threads"] == 2
        assert row["job_threads_abandoned"] == 2
        # ...and it is still, by every older signal, a perfectly live worker
        assert row["shutdown_at"] is None
        assert row["idle"] is False

    async def test_a_healthy_worker_records_zero(
        self, live_worker, unique_queue, db_pool
    ):
        """A worker doing its job reports no abandoned threads at all.

        Including *while a job is running*: every job's ``run()`` goes to a
        thread, so a naive count of live threads would report the running
        job's own thread as abandoned and cry wolf on every sync job. The
        assertion covers both instants — one job in flight, and afterwards.
        """
        worker = await live_worker(job_threads=4, heartbeat_interval=FAST_HEARTBEAT)

        job_id = await enqueue(db_pool, unique_queue, BLOCKER, {"block": 1.5}, {})
        await wait_for_job_state(db_pool, job_id, ("running",), timeout=15)
        assert worker._abandoned_job_threads() == 0

        row = await registry_row(db_pool, worker)
        assert row["job_threads"] == 4
        assert row["job_threads_abandoned"] == 0

        finished = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=25)
        assert finished["result"] == "blocked"

        row = await wait_for_abandoned(db_pool, worker, 0)
        assert row["job_threads"] == 4
        assert row["job_threads_abandoned"] == 0

    async def test_registration_publishes_the_pool_size_before_any_heartbeat(
        self, live_worker, db_pool
    ):
        """The pool size is on the row from the moment the worker exists.

        With an hour between heartbeats there is no way this came from one:
        the INSERT that registers the worker carries it, so the counts are
        never briefly uninterpretable during startup.
        """
        worker = await live_worker(job_threads=3, heartbeat_interval=3600.0)

        # Poll for the row's EXISTENCE only: the fixture returns concurrently
        # with the registration INSERT's commit, and a single read raced it.
        # The hour-long heartbeat still guarantees the VALUES cannot have
        # come from a heartbeat -- which is what this test is about.
        deadline = time.monotonic() + 10
        row = await registry_row(db_pool, worker)
        while row is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            row = await registry_row(db_pool, worker)
        assert row is not None, "worker never registered"

        assert row["job_threads"] == 3
        assert row["job_threads_abandoned"] == 0

    async def test_the_heartbeat_writes_it_with_no_extra_round_trip(
        self, live_worker, db_pool
    ):
        """The counts ride the statement that already ran every cycle.

        asyncpg logs every query issued on a connection, so this watches the
        worker's dedicated heartbeat connection directly. Two claims, both
        exact: the only statement it ever issues is ``WORKER_HEARTBEAT_SQL``
        (identity, not a match — no second statement of any kind exists), and
        it issues no more of them than there were heartbeat intervals in the
        window, so making the condition visible fleet-wide cost zero extra
        round trips per beat.
        """
        worker = await live_worker(job_threads=2, heartbeat_interval=FAST_HEARTBEAT)
        assert worker._hb_cxn is not None

        logged: list[Any] = []
        worker._hb_cxn.add_query_logger(logged.append)
        try:
            window = 2.0
            await asyncio.sleep(window)
        finally:
            worker._hb_cxn.remove_query_logger(logged.append)

        assert {q.query for q in logged} == {WORKER_HEARTBEAT_SQL}
        beats = int(window / FAST_HEARTBEAT)
        assert len(logged) <= beats + 1, [q.query for q in logged]
        assert len(logged) >= beats - 2, [q.query for q in logged]

        # and the statement really is the one carrying the counts
        assert "job_threads_abandoned = $3" in WORKER_HEARTBEAT_SQL


# ============================================================================
# 2. doctor
# ============================================================================


class TestDoctor:
    async def test_it_warns_and_names_the_worker_and_the_remedy(
        self, live_worker, unique_queue, db_pool, db_params
    ):
        """The 3am command says which worker, how bad, and what to do.

        WARN rather than FAIL, and the exit code is 0: this doctor reserves
        FAIL for "the platform cannot function" (no schema, missing NOTIFY
        triggers) and grades lost capacity as a warning — "no live workers at
        all" is a WARN here, so one worker of one refusing cannot be graver.
        It also self-heals when the abandoned threads finish, and exiting 1 on
        a condition that may already be over is how a pager gets ignored.
        """
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)
        row = await wait_for_abandoned(db_pool, worker, 2)

        result = await run_doctor(dsn_for(db_params))

        checks = parse_checks(result.output)
        assert checks["job-threads"] == (
            "WARN",
            "1 of 1 live worker(s) not claiming -- "
            f"worker {row['id']} ({row['host']}:{row['pid']}, "
            f"queue {unique_queue}) 2/2 job threads abandoned. "
            f"{DOCTOR_THREADS_REMEDY}",
        )
        # the remedy points at the cause, not at the number
        assert "cannot be interrupted" in DOCTOR_THREADS_REMEDY
        assert "exceeded their deadline" in DOCTOR_THREADS_REMEDY

        # the check it is standing next to still calls this fleet healthy --
        # which is exactly why this check had to exist
        assert checks["workers"] == ("PASS", "1 live worker(s) seen in last 60s")
        assert result.exit_code == 0

    async def test_a_healthy_fleet_passes(
        self, live_worker, unique_queue, db_pool, db_params
    ):
        """A worker that is claiming produces a PASS, not silence."""
        worker = await live_worker(job_threads=4, heartbeat_interval=FAST_HEARTBEAT)
        job_id = await enqueue(db_pool, unique_queue, PROMPT, {"x": 21}, {})
        finished = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
        assert finished["result"] == {"doubled": 42}
        await wait_for_abandoned(db_pool, worker, 0)

        result = await run_doctor(dsn_for(db_params))

        checks = parse_checks(result.output)
        assert checks["job-threads"] == ("PASS", "1 live worker(s) claiming")
        assert result.exit_code == 0


# ============================================================================
# 3. Metrics
# ============================================================================


class TestMetrics:
    async def test_the_admin_api_reports_the_saturation_exactly(
        self, live_worker, unique_queue, db_pool
    ):
        """``AdminAPI.job_thread_stats`` beside the other saturation stats."""
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)
        await wait_for_abandoned(db_pool, worker, 2)

        async with db_pool.acquire() as conn:
            stats = await AdminAPI(conn).job_thread_stats()

        assert stats == {
            "workers": 1,
            "not_claiming": 1,
            "abandoned": 2,
            "max_abandoned": 2,
        }

    async def test_a_healthy_worker_saturates_nothing(
        self, live_worker, unique_queue, db_pool
    ):
        await live_worker(job_threads=8, heartbeat_interval=FAST_HEARTBEAT)

        async with db_pool.acquire() as conn:
            stats = await AdminAPI(conn).job_thread_stats()

        assert stats == {
            "workers": 1,
            "not_claiming": 0,
            "abandoned": 0,
            "max_abandoned": 0,
        }

    async def test_the_scrape_exposes_it_as_a_gauge_and_stays_valid(
        self, live_worker, unique_queue, db_pool, web_admin_client
    ):
        """The Prometheus exposition: right names, right type, right values.

        ``pyjobby_workers_live`` counts the refusing worker — it is alive,
        that gauge is not wrong — which is precisely why the scrape needs the
        second number next to it. The exposition is re-validated as a whole
        here (every sample declares its HELP and TYPE exactly once) because a
        new metric is the way a valid exposition stops being one.
        """
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)
        await wait_for_abandoned(db_pool, worker, 2)

        resp = await web_admin_client.get("/metrics")
        assert resp.status == 200
        body = await resp.text()

        samples = parse_samples(body)
        assert samples["pyjobby_workers_live"] == 1.0
        assert samples["pyjobby_workers_not_claiming"] == 1.0
        assert samples["pyjobby_worker_job_threads_abandoned_max"] == 2.0

        assert "# TYPE pyjobby_workers_not_claiming gauge\n" in body
        assert "# TYPE pyjobby_worker_job_threads_abandoned_max gauge\n" in body

        for name in {sample.split("{", 1)[0] for sample in samples}:
            assert body.count(f"# HELP {name} ") == 1, name
            assert body.count(f"# TYPE {name} ") == 1, name

    async def test_the_gauges_are_zero_for_a_healthy_fleet(
        self, live_worker, unique_queue, db_pool, web_admin_client
    ):
        await live_worker(job_threads=8, heartbeat_interval=FAST_HEARTBEAT)

        resp = await web_admin_client.get("/metrics")
        body = await resp.text()

        samples = parse_samples(body)
        assert samples["pyjobby_workers_live"] == 1.0
        assert samples["pyjobby_workers_not_claiming"] == 0.0
        assert samples["pyjobby_worker_job_threads_abandoned_max"] == 0.0


# ============================================================================
# 4. The worker listings
# ============================================================================


class TestWorkerListings:
    async def test_list_workers_marks_it_not_claiming(
        self, live_worker, unique_queue, db_pool
    ):
        """``pj-admin workers list`` and the dashboard both read this row.

        Without ``not_claiming`` the only honest thing either could print was
        "live", so the listing an operator reaches for after doctor would
        have contradicted doctor.
        """
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)
        await wait_for_abandoned(db_pool, worker, 2)

        async with db_pool.acquire() as conn:
            listed = await AdminAPI(conn).list_workers()

        rows = [w for w in listed if w["id"] == worker.worker_id]
        assert len(rows) == 1
        assert rows[0]["live"] is True
        assert rows[0]["not_claiming"] is True
        assert rows[0]["job_threads"] == 2
        assert rows[0]["job_threads_abandoned"] == 2

    async def test_the_html_worker_table_shows_the_condition(
        self, live_worker, unique_queue, db_pool, web_admin_client
    ):
        worker = await fill_the_pool(live_worker, db_pool, unique_queue, job_threads=2)
        await wait_for_abandoned(db_pool, worker, 2)

        resp = await web_admin_client.get("/api/workers?format=html")
        assert resp.status == 200
        text = await resp.text()

        assert '<span class="badge crashed">not claiming</span>' in text
        assert "2 abandoned / 2" in text
        assert '<span class="badge live">live</span>' not in text
