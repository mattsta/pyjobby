"""Admin API + CLI tests for the schema-v1 operator control plane.

Exercises the AdminAPI queue controls (pause/resume/limits), the
jorb_worker registry views, the crashed-state DLQ, the jorb_history /
jorb_step introspection methods, checkpoint-aware requeue (resume vs
--fresh), and the `pj-admin doctor` health checks — all against live
JobSystem workers where claiming behavior matters.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby import db
from pyjobby.admin_api import AdminAPI
from pyjobby.cli import cli

from .conftest import wait_for_job_state


async def _insert_job(
    pool, queue: str, job_class: str, kwargs: dict, admin_data: dict | None = None
) -> int:
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        job_class,
        kwargs,
        queue,
        admin_data or {},
    )
    return job_id


# ============================================================================
# Queue control plane
# ============================================================================


async def test_admin_pause_blocks_claims_then_resume_completes(
    live_worker, unique_queue, db_pool
):
    await live_worker()

    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        control = await api.pause_queue(unique_queue)
        assert control["paused"] is True

        job_id = await _insert_job(
            db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 5}
        )

        await asyncio.sleep(1.0)
        state = await db_pool.fetchval("SELECT state FROM jorb WHERE id=$1", job_id)
        assert state == "queued", "paused queue must not be claimed from"

        control = await api.resume_queue(unique_queue)
        assert control["paused"] is False

        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["result"] == {"doubled": 10}


async def test_queue_control_upsert_updates_only_provided_fields(db_pool, unique_queue):
    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        # absent row = defaults
        assert await api.get_queue_control(unique_queue) is None

        control = await api.set_queue_control(
            unique_queue, max_concurrency=2, rate_limit=5, rate_period_seconds=30.0
        )
        assert control["name"] == unique_queue
        assert control["paused"] is False
        assert control["max_concurrency"] == 2
        assert control["rate_limit"] == 5
        assert control["rate_period_seconds"] == 30.0

        # pausing must not clobber the limits
        control = await api.set_queue_control(unique_queue, paused=True)
        assert control["paused"] is True
        assert control["max_concurrency"] == 2
        assert control["rate_limit"] == 5
        assert control["rate_period_seconds"] == 30.0

        # explicit None clears one limit, leaves the rest alone
        control = await api.set_queue_control(unique_queue, max_concurrency=None)
        assert control["max_concurrency"] is None
        assert control["rate_limit"] == 5
        assert control["paused"] is True

        controls = await api.list_queue_controls()
        assert unique_queue in [c["name"] for c in controls]

        # queue_stats joins the control plane (even with zero jobs)
        stats = await api.queue_stats(queue=unique_queue)
        assert len(stats) == 1
        assert stats[0]["queue"] == unique_queue
        assert stats[0]["paused"] is True
        assert stats[0]["rate_limit"] == 5
        assert stats[0]["total"] == 0

        # list_queues surfaces control-only queues too
        queues = await api.list_queues()
        mine = next(q for q in queues if q["name"] == unique_queue)
        assert mine["paused"] is True
        assert mine["rate_limit"] == 5


# ============================================================================
# Worker registry
# ============================================================================


async def test_workers_list_and_stats_from_registry(live_worker, unique_queue, db_pool):
    system = await live_worker()

    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        workers = await api.list_workers()
        mine = [w for w in workers if w["queue"] == unique_queue]
        assert len(mine) == 1
        worker = mine[0]
        assert worker["live"] is True
        assert worker["pid"] == system.pid
        assert worker["host"]
        assert worker["shutdown_at"] is None
        assert worker["last_seen_age_seconds"] < 60
        assert worker["capabilities"] == ["test"]
        assert worker["current_job_id"] is None

        # a claimed/running job shows up via the claimed_by join
        job_id = await _insert_job(
            db_pool, unique_queue, "tests.dxe_jobs.SlowJob", {"seconds": 30}
        )
        await wait_for_job_state(db_pool, job_id, ("running",))

        workers = await api.list_workers()
        worker = next(w for w in workers if w["queue"] == unique_queue)
        assert worker["current_job_id"] == job_id
        assert worker["current_job_class"] == "tests.dxe_jobs.SlowJob"
        assert worker["current_job_state"] == "running"

        stats = await api.worker_stats()
        assert stats["live_workers"] >= 1
        assert stats["total_registered"] >= 1
        assert stats["per_queue"][unique_queue] == 1

        # don't leave the worker stuck in a 30s sleep at teardown
        await db.cancel_job(db_pool, job_id)
        await wait_for_job_state(db_pool, job_id, ("cancelled",), timeout=5)


# ============================================================================
# DLQ (state = 'crashed', no error-count heuristic)
# ============================================================================


async def test_dlq_lists_crashed_job_and_retry_resets(
    live_worker, unique_queue, db_pool
):
    await live_worker()

    job_id = await _insert_job(
        db_pool,
        unique_queue,
        "tests.dxe_jobs.FailJob",
        {},
        {"max_retries": 1, "initial_retry_delay": 0},
    )
    row = await wait_for_job_state(db_pool, job_id, ("crashed",))
    assert row["error_count"] >= 1

    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        dlq = await api.list_dlq()
        entry = next(j for j in dlq if j["id"] == job_id)
        # far below the old >=10 heuristic: crashed alone is the DLQ now
        assert entry["error_count"] < 10
        assert "intentional failure" in entry["error_message"]

        # the audit trail recorded the terminal crash with its error
        history = await api.get_job_history(job_id)
        crashed = [h for h in history if h["event"] == "crashed"]
        assert crashed
        assert "intentional failure" in (crashed[-1]["detail"].get("error") or "")

        # pause first so the requeued state is observable (FailJob would
        # otherwise be instantly reclaimed by the live worker)
        await api.pause_queue(unique_queue)
        result = await api.retry_from_dlq(job_id)
        assert result == {"job_id": job_id, "status": "requeued_from_dlq"}

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id=$1", job_id)
        assert row["state"] == "queued"
        assert row["error_count"] == 0
        assert row["error_message"] is None


# ============================================================================
# History & steps introspection
# ============================================================================


async def test_job_history_returns_ordered_serialized_trail(
    live_worker, unique_queue, db_pool
):
    await live_worker()

    job_id = await _insert_job(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 3})
    await wait_for_job_state(db_pool, job_id, ("finished",))

    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)
        history = await api.get_job_history(job_id)

    events = [h["event"] for h in history]
    assert events == ["enqueued", "claimed", "running", "finished"]

    for h in history:
        assert h["job_id"] == job_id
        # datetimes serialized to ISO strings
        datetime.datetime.fromisoformat(h["at"])
        assert isinstance(h["detail"], dict)

    assert history[0]["detail"]["queue"] == unique_queue
    running = next(h for h in history if h["event"] == "running")
    assert running["detail"]["run_epoch"] == 1


async def test_requeue_resume_keeps_checkpoints_fresh_wipes_them(
    live_worker, unique_queue, db_pool
):
    await live_worker()

    job_id = await _insert_job(
        db_pool,
        unique_queue,
        "tests.dxe_jobs.StepPipelineJob",
        {},
        {"max_retries": 3, "initial_retry_delay": 0},
    )
    row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=15)
    assert row["result"] == {"final": 14}

    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        steps = await api.get_job_steps(job_id)
        assert [s["step_seq"] for s in steps] == [1, 2, 3]
        assert [s["name"] for s in steps] == ["fetch", "maybe-explode", "double"]
        for s in steps:
            assert s["error"] is None
            assert s["duration_seconds"] is not None
            datetime.datetime.fromisoformat(s["started"])

        # --- resume-style requeue: checkpoints survive ---
        await api.pause_queue(unique_queue)
        result = await api.requeue_job(job_id)
        assert result == {"job_id": job_id, "status": "requeued", "fresh": False}

        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id=$1", job_id)
            == "queued"
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id=$1", job_id
            )
            == 3
        )

        # a queued job cannot be requeued again
        with pytest.raises(ValueError, match="cannot"):
            await api.requeue_job(job_id)

        await api.resume_queue(unique_queue)
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=15)
        assert row["result"] == {"final": 14}

        # --- fresh requeue: checkpoints wiped, restart from step 1 ---
        await api.pause_queue(unique_queue)
        result = await api.requeue_job(job_id, fresh=True)
        assert result["fresh"] is True
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id=$1", job_id
            )
            == 0
        )

        await api.resume_queue(unique_queue)
        # error budget was reset, so step 2 explodes once again and the
        # retry completes the pipeline from scratch
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
        assert row["result"] == {"final": 14}

        steps = await api.get_job_steps(job_id)
        assert [s["step_seq"] for s in steps] == [1, 2, 3]


async def test_rerun_job_wipes_checkpoints_by_default_but_fresh_false_keeps(
    unique_queue, db_pool
):
    """db.rerun_job is the "do it again, repeat side effects" verb: a durable
    job replays its checkpoints with no epoch filter, so a rerun that kept
    them would fast-forward over the work and repeat nothing. Default wipes;
    fresh=False is the explicit RESUME opt-out that keeps them."""
    for keep in (False, True):
        job_id = await _insert_job(
            db_pool, unique_queue, "tests.dxe_jobs.OkJob", {}
        )
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
            job_id,
        )
        await db_pool.execute(
            """INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
               VALUES ($1, 1, 'did-it', '"done"'::jsonb, 0)""",
            job_id,
        )

        requeued = await db.rerun_job(db_pool, job_id, fresh=not keep)

        assert requeued == job_id
        steps = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        )
        assert steps == (1 if keep else 0)
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            == "queued"
        )


async def test_update_job_priority_only_touches_queued_and_waiting(
    unique_queue, db_pool
):
    """The operator re-prioritise verb: queued/waiting jobs move, a running
    one does not (its priority no longer decides anything), and a priority
    above the worker ceiling is refused before it becomes an unclaimable
    black hole."""
    async with db_pool.acquire() as conn:
        api = AdminAPI(conn)

        queued = await _insert_job(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {})
        assert await api.update_job_priority(queued, 5) is True
        assert (
            await db_pool.fetchval("SELECT prio FROM jorb WHERE id = $1", queued) == 5
        )

        running = await _insert_job(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {})
        await db_pool.execute(
            "UPDATE jorb SET state = 'running' WHERE id = $1", running
        )
        assert await api.update_job_priority(running, 5) is False

        with pytest.raises(ValueError):
            await api.update_job_priority(queued, api.prio_ceiling + 1)


def test_cli_jobs_set_priority(dsn):
    runner = CliRunner()
    queue = f"setprio_{uuid.uuid4().hex[:8]}"

    async def _seed() -> int:
        conn = await asyncpg.connect(dsn)
        try:
            jid: int = await conn.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, state)
                   VALUES ('tests.dxe_jobs.OkJob', '{}', $1, 'queued')
                   RETURNING id""",
                queue,
            )
            return jid
        finally:
            await conn.close()

    async def _read_prio(jid: int) -> int:
        conn = await asyncpg.connect(dsn)
        try:
            prio: int = await conn.fetchval("SELECT prio FROM jorb WHERE id = $1", jid)
            return prio
        finally:
            await conn.close()

    job_id = asyncio.run(_seed())

    result = runner.invoke(
        cli, ["--dsn", dsn, "jobs", "set-priority", str(job_id), "7"], obj={}
    )
    assert result.exit_code == 0, result.output
    assert "priority set to 7" in result.output
    assert asyncio.run(_read_prio(job_id)) == 7


@pytest.fixture
def dsn(db_params: dict) -> str:
    """The DSN of THIS session's database.

    Not the module-level TEST_DSN: under xdist every worker gets its own
    database, so a CLI invoked against the base DSN inspects a database the
    test never wrote to -- and one that conftest never re-installs the schema
    into, so it silently lags behind schema.sql. These tests were passing only
    because the two happened to agree.
    """
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


# ============================================================================
# CLI (sync tests: click commands drive their own event loop)
# ============================================================================


def test_doctor_healthy_database_exits_zero(dsn):
    runner = CliRunner()
    result = runner.invoke(cli, ["--dsn", dsn, "doctor"], obj={})
    assert result.exit_code == 0, result.output
    assert "PASS database: connected" in result.output
    assert "PASS schema" in result.output
    assert "PASS triggers" in result.output
    assert "FAIL" not in result.output


def test_doctor_thresholds_accept_options(dsn):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--dsn", dsn, "doctor", "--max-depth", "1", "--max-age-minutes", "1"],
        obj={},
    )
    # thresholds only produce WARNs, never FAILs
    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output


def test_cli_queue_controls_roundtrip(dsn):
    runner = CliRunner()
    qname = f"cliq_{uuid.uuid4().hex[:8]}"

    result = runner.invoke(cli, ["--dsn", dsn, "queues", "pause", qname], obj={})
    assert result.exit_code == 0, result.output
    assert "paused" in result.output

    result = runner.invoke(
        cli,
        [
            "--dsn",
            dsn,
            "queues",
            "limits",
            qname,
            "--max-concurrency",
            "4",
            "--rate-limit",
            "none",
            "--rate-period",
            "30",
        ],
        obj={},
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["--dsn", dsn, "queues", "show", qname], obj={})
    assert result.exit_code == 0, result.output
    assert "yes" in result.output  # paused
    assert "4" in result.output  # max concurrency

    result = runner.invoke(cli, ["--dsn", dsn, "queues", "resume", qname], obj={})
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["--dsn", dsn, "queues", "list"], obj={})
    assert result.exit_code == 0, result.output
    assert qname in result.output


def test_cli_workers_and_jobs_commands_run(dsn):
    runner = CliRunner()

    result = runner.invoke(cli, ["--dsn", dsn, "workers", "list"], obj={})
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["--dsn", dsn, "workers", "stats"], obj={})
    assert result.exit_code == 0, result.output
    assert "Live workers" in result.output

    # requeueing a nonexistent job fails cleanly
    result = runner.invoke(cli, ["--dsn", dsn, "jobs", "requeue", "999999999"], obj={})
    assert result.exit_code == 1
    assert "not found" in result.output
