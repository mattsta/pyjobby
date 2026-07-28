"""Tests for pyjobby.monitor — the platform's single background reaper.

Covers every sweep the monitor daemon performs:

- timeout enforcement: running jobs past ``timeout_at`` are requeued with
  backoff (same row) or dead-lettered to state='crashed' per ``on_timeout``
  and ``max_retries``
- dead-worker reclaim: in-flight jobs of workers whose ``jorb_worker``
  heartbeat went stale are requeued and the workers retired
- stuck-claim reclaim: jobs 'claimed' past the grace are requeued whoever
  claims them (covers unregistered claimers AND lost claim acks)
- job retention: terminal jobs past the window are deleted with every child
  row; live work is never deleted at any age; on by default, with 0 as the
  keep-forever escape hatch
- checkpoint retention: the jorb_step rows of terminal jobs go on their own,
  much shorter window while the job row stays — two lifetimes, not one
- draining: one retention cycle clears a multi-batch backlog, stops on its
  time budget, and says which of the two it did
- run_epoch fencing: a monitor requeue makes the superseded execution's
  completion a no-op
- the ``monitor()`` loop wires the sweeps together and keeps each one's
  failure to itself
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import uuid

import pytest
from loguru import logger

from pyjobby import monitor as monitor_module
from pyjobby.monitor import (
    CANCEL_UNSATISFIABLE_WAITERS_SQL,
    WAKE_WAITERS_SQL,
    _drain,
    handle_timed_out_job,
    monitor,
    sweep_completed_checkpoints,
    sweep_consumed_mailbox,
    sweep_dead_workers,
    sweep_expired_jobs,
    sweep_job_history,
    sweep_orphaned_dags,
    sweep_retired_workers,
    sweep_schedule_log,
    sweep_stranded_waiters,
    sweep_stuck_claims,
    sweep_timed_out_jobs,
)
from pyjobby.pj import STMTS
from pyjobby.procs import dsn_from
from tests.conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


# ============================================================================
# helpers
# ============================================================================


async def insert_job(
    pool,
    queue: str,
    *,
    state: str = "queued",
    job_class: str = "tests.dxe_jobs.OkJob",
    kwargs: dict | None = None,
    admin_data: dict | None = None,
    error_count: int = 0,
    run_epoch: int = 0,
    claimed_by: int | None = None,
    timeout_at_offset_seconds: float | None = None,
    waitfor_job: int | None = None,
    waitfor_group: int | None = None,
    run_group: int | None = None,
) -> int:
    """Insert one jorb row in the state a test needs (JSONB never NULL)."""
    return await pool.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, admin_data, state,
                          error_count, run_epoch, claimed_by, worker_host,
                          worker_pid, timeout_at, waitfor_job, waitfor_group,
                          run_group)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'test-host', 4242,
                CASE WHEN $9::float8 IS NULL THEN NULL
                     ELSE now() + make_interval(secs => $9) END,
                $10, $11, $12)
        RETURNING id
        """,
        job_class,
        kwargs or {},
        queue,
        admin_data or {},
        state,
        error_count,
        run_epoch,
        claimed_by,
        timeout_at_offset_seconds,
        waitfor_job,
        waitfor_group,
        run_group,
    )


async def insert_worker(pool, queue: str, *, last_seen_age_seconds: float = 0) -> int:
    """Insert a jorb_worker registry row with a heartbeat this old."""
    return await pool.fetchval(
        """
        INSERT INTO jorb_worker (host, pid, queue, capabilities, version,
                                 last_seen)
        VALUES ('test-host', 4242, $1, '{test}', 'test',
                now() - make_interval(secs => $2))
        RETURNING id
        """,
        queue,
        last_seen_age_seconds,
    )


async def insert_dag(pool, name: str, *, days_ago: float = 0) -> int:
    """A jorb_dag row created ``days_ago`` days ago."""
    return await pool.fetchval(
        """
        INSERT INTO jorb_dag (name, created)
        VALUES ($1, now() - make_interval(secs => $2))
        RETURNING id
        """,
        name,
        days_ago * 86400,
    )


async def dag_ids(pool) -> list[int]:
    return [r["id"] for r in await pool.fetch("SELECT id FROM jorb_dag ORDER BY id")]


async def dag_status(pool, dag_id: int):
    """What ``pj-admin dag list`` shows for one DAG."""
    return await pool.fetchrow(
        "SELECT * FROM jorb_dag_status WHERE dag_id = $1", dag_id
    )


async def insert_schedule(pool, name: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run)
        VALUES ($1, 'tests.dxe_jobs.OkJob', '* * * * *', now())
        RETURNING id
        """,
        name,
    )


async def log_execution(
    pool, schedule_id: int, name: str, *, days_ago: float, result: str = "success"
) -> int:
    """One jorb_schedule_log row, as the scheduler writes it."""
    return await pool.fetchval(
        """
        INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time,
                                       actual_time, result)
        VALUES ($1, $2, now() - make_interval(secs => $3),
                now() - make_interval(secs => $3), $4)
        RETURNING id
        """,
        schedule_id,
        name,
        days_ago * 86400,
        result,
    )


async def schedule_log_ids(pool) -> list[int]:
    return [
        r["id"]
        for r in await pool.fetch("SELECT id FROM jorb_schedule_log ORDER BY id")
    ]


async def retire_worker(pool, worker_id: int, *, days_ago: float) -> None:
    """Stamp shutdown_at (and last_seen) that long ago: a worker gone since."""
    await pool.execute(
        """
        UPDATE jorb_worker
        SET shutdown_at = now() - make_interval(secs => $2),
            last_seen   = now() - make_interval(secs => $2)
        WHERE id = $1
        """,
        worker_id,
        days_ago * 86400,
    )


async def worker_ids(pool) -> list[int]:
    return [r["id"] for r in await pool.fetch("SELECT id FROM jorb_worker ORDER BY id")]


async def get_job(pool, job_id: int):
    return await pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)


async def job_ids(pool) -> list[int]:
    """Every surviving job id, so tests can assert exact survivors."""
    return [r["id"] for r in await pool.fetch("SELECT id FROM jorb ORDER BY id")]


async def age_job(pool, job_id: int, *, days: float) -> None:
    """Backdate a job's clocks so a retention window can see past it.

    ``finished`` is the terminal timestamp retention reads; ``updated`` moves
    with it so a live job under test looks just as old (proving state, not
    age, is what protects it)."""
    await pool.execute(
        """
        UPDATE jorb
        SET finished = CASE WHEN finished IS NULL THEN NULL
                            ELSE now() - make_interval(secs => $2) END,
            updated = now() - make_interval(secs => $2),
            created = now() - make_interval(secs => $2)
        WHERE id = $1
        """,
        job_id,
        days * 86400,
    )


async def insert_terminal_job(
    pool, queue: str, *, state: str = "finished", days_ago: float = 30
) -> int:
    """Enqueue a job, transition it to a terminal state, and backdate it.

    Enqueue-then-transition rather than inserting the terminal row directly:
    that is what produces a real jorb_history trail (the trigger records the
    INSERT and the state change), which retention has to clean up too."""
    job_id = await insert_job(pool, queue)
    await pool.execute(
        "UPDATE jorb SET state = $2, finished = now() WHERE id = $1", job_id, state
    )
    await age_job(pool, job_id, days=days_ago)
    return job_id


async def add_child_rows(pool, job_id: int) -> None:
    """Give a job one row in each table that hangs off it."""
    await pool.execute(
        """
        INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
        VALUES ($1, 1, 'step-one', $2, 0)
        """,
        job_id,
        {"ok": True},
    )
    await pool.execute(
        "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, 'progress', $2)",
        job_id,
        {"pct": 50},
    )
    await pool.execute(
        "INSERT INTO jorb_mailbox (dest_job_id, topic, message) VALUES ($1, 'ping', $2)",
        job_id,
        {"hello": "world"},
    )


async def add_checkpoints(
    pool, job_id: int, count: int, *, name: str = "step", output: dict | None = None
) -> list[int]:
    """Record ``count`` DXE checkpoints on a job; returns their step_seqs."""
    seqs = list(range(1, count + 1))
    for seq in seqs:
        await pool.execute(
            """
            INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
            VALUES ($1, $2, $3, $4, 0)
            """,
            job_id,
            seq,
            f"{name}-{seq}",
            output if output is not None else {"seq": seq},
        )
    return seqs


async def step_seqs(pool, job_id: int) -> list[int]:
    """Every surviving checkpoint sequence for a job, in order."""
    return [
        r["step_seq"]
        for r in await pool.fetch(
            "SELECT step_seq FROM jorb_step WHERE job_id = $1 ORDER BY step_seq", job_id
        )
    ]


async def child_counts(pool, job_id: int) -> dict[str, int]:
    """Row counts in every table keyed by this job id."""
    return {
        "jorb_step": await pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
        ),
        "jorb_event": await pool.fetchval(
            "SELECT count(*) FROM jorb_event WHERE job_id = $1", job_id
        ),
        "jorb_mailbox": await pool.fetchval(
            "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", job_id
        ),
        "jorb_history": await pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = $1", job_id
        ),
    }


@contextlib.contextmanager
def captured_logs(level: str):
    """Collect the monitor's loguru messages at or above ``level``.

    What retention reports is part of its contract: an operator has to be able
    to tell "nothing to delete" from "gave up with a backlog left"."""
    messages: list[str] = []
    handler_id = logger.add(messages.append, level=level, format="{message}")
    try:
        yield messages
    finally:
        logger.remove(handler_id)


async def wait_until(predicate, *, timeout: float = 10) -> None:
    """Poll ``predicate`` (sync or async) until true, or fail at the deadline.

    The retention sweeps delete rows, so the monitor loop cannot be observed
    with wait_for_job_state — there is no row left to read a state from."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not reached within {timeout}s")


async def wait_for_job_gone(pool, job_id: int, *, timeout: float = 10) -> None:
    await wait_until(
        lambda: pool.fetchval(
            "SELECT NOT EXISTS (SELECT 1 FROM jorb WHERE id = $1)", job_id
        ),
        timeout=timeout,
    )


async def wait_for_mailbox_empty(pool, job_id: int, *, timeout: float = 10) -> None:
    await wait_until(
        lambda: pool.fetchval(
            "SELECT NOT EXISTS (SELECT 1 FROM jorb_mailbox WHERE dest_job_id = $1)",
            job_id,
        ),
        timeout=timeout,
    )


async def hand_claim(pool, job_id: int) -> int:
    """Simulate a worker claim: bump run_epoch and move to running.

    Returns the new epoch (the fencing token for this simulated attempt)."""
    return await pool.fetchval(
        """
        UPDATE jorb
        SET state = 'running',
            run_count = run_count + 1,
            run_epoch = run_epoch + 1,
            started = now(),
            updated = now()
        WHERE id = $1
        RETURNING run_epoch
        """,
        job_id,
    )


# ============================================================================
# timeout enforcement
# ============================================================================


class TestHandleTimedOutJob:
    """The per-job retry/dead-letter decision."""

    async def test_retry_requeues_same_row_with_backoff(self, db_pool, unique_queue):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "retry", "max_retries": 5},
            timeout_at_offset_seconds=-10,
        )

        before = await db_pool.fetchval("SELECT now()")
        await handle_timed_out_job(db_pool, job_id, "test.Job", {"max_retries": 5}, 0)

        job = await get_job(db_pool, job_id)
        assert job["id"] == job_id  # SAME row — no retry-copy rows in v1
        assert job["state"] == "queued"
        assert job["error_count"] == 1
        assert job["timeout_at"] is None
        assert "Timeout exceeded" in job["error_message"]
        # backoff pushed run_after into the future
        assert job["run_after"] > before

    async def test_on_timeout_fail_dead_letters(self, db_pool, unique_queue):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "fail"},
            timeout_at_offset_seconds=-10,
        )

        await handle_timed_out_job(
            db_pool, job_id, "test.Job", {"on_timeout": "fail"}, 0
        )

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"  # terminal: the DLQ
        assert job["error_count"] == 1
        assert job["timeout_at"] is None
        assert job["finished"] is not None
        assert "on_timeout=fail" in job["error_message"]

    async def test_max_retries_exhausted_dead_letters(self, db_pool, unique_queue):
        admin = {"on_timeout": "retry", "max_retries": 3}
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data=admin,
            error_count=2,  # attempt = 3 == max_retries
            timeout_at_offset_seconds=-10,
        )

        await handle_timed_out_job(db_pool, job_id, "test.Job", admin, 2)

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"
        assert job["error_count"] == 3
        assert "max retries exceeded" in job["error_message"]

    async def test_ignores_job_no_longer_running(self, db_pool, unique_queue):
        """The UPDATE is guarded on state='running': a job that finished
        between detection and handling is left alone."""
        job_id = await insert_job(db_pool, unique_queue, state="finished")

        await handle_timed_out_job(db_pool, job_id, "test.Job", {}, 0)

        job = await get_job(db_pool, job_id)
        assert job["state"] == "finished"
        assert job["error_count"] == 0


class TestSweepTimedOutJobs:
    """The batch sweep over running jobs past their deadline."""

    async def test_sweep_requeues_overdue_and_leaves_the_rest(
        self, db_pool, unique_queue
    ):
        overdue = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )
        not_due = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=3600
        )
        no_timeout = await insert_job(db_pool, unique_queue, state="running")

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1

        assert (await get_job(db_pool, overdue))["state"] == "queued"
        assert (await get_job(db_pool, not_due))["state"] == "running"
        assert (await get_job(db_pool, no_timeout))["state"] == "running"

    async def test_sweep_dead_letters_when_policy_says_fail(
        self, db_pool, unique_queue
    ):
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"on_timeout": "fail"},
            timeout_at_offset_seconds=-5,
        )

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "crashed"

    async def test_sweep_respects_batch_size(self, db_pool, unique_queue):
        ids = [
            await insert_job(
                db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
            )
            for _ in range(3)
        ]

        handled = await sweep_timed_out_jobs(db_pool, batch_size=2)
        assert handled == 2

        states = [(await get_job(db_pool, job_id))["state"] for job_id in ids]
        assert states.count("queued") == 2
        assert states.count("running") == 1

        # a second sweep finishes the backlog
        handled = await sweep_timed_out_jobs(db_pool, batch_size=2)
        assert handled == 1

    async def test_timeout_retry_recorded_in_history(self, db_pool, unique_queue):
        """The requeue is a state change, so the jorb_history trigger records
        the attempt trail on the ONE job id."""
        job_id = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )

        await sweep_timed_out_jobs(db_pool)

        events = await db_pool.fetch(
            "SELECT event, detail FROM jorb_history WHERE job_id = $1 ORDER BY id",
            job_id,
        )
        assert [e["event"] for e in events] == ["enqueued", "queued"]
        assert events[-1]["detail"]["from"] == "running"
        assert events[-1]["detail"]["error_count"] == 1


# ============================================================================
# dead-worker reclaim
# ============================================================================


class TestSweepJobHistory:
    """History retention by AGE, for the jobs whose cascade never fires.

    A terminal job's history dies with the job's own retention; a durable
    machine that never terminates writes ~3 history rows per wake forever,
    and this sweep is the only thing that bounds them.
    """

    async def _age_history(self, pool, job_id: int, days: float) -> None:
        await pool.execute(
            "UPDATE jorb_history SET at = now() - make_interval(days => $2)"
            " WHERE job_id = $1",
            job_id,
            days,
        )

    async def test_old_rows_of_a_live_job_are_deleted(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="running")
        await self._age_history(db_pool, job_id, days=60)

        deleted = await sweep_job_history(db_pool, retention_days=30)

        assert deleted >= 1
        remaining = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = $1", job_id
        )
        assert remaining == 0
        # the job row itself is untouched — this sweep is about the trail
        assert (await get_job(db_pool, job_id))["state"] == "running"

    async def test_recent_rows_are_kept(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="running")

        await sweep_job_history(db_pool, retention_days=30)

        remaining = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = $1", job_id
        )
        assert remaining >= 1  # the trigger-recorded 'enqueued' row survives

    async def test_batch_size_bounds_one_bite(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="running")
        await db_pool.execute(
            """INSERT INTO jorb_history (job_id, at, event)
               SELECT $1, now() - interval '60 days', 'queued'
               FROM generate_series(1, 5)""",
            job_id,
        )
        await self._age_history(db_pool, job_id, days=60)

        assert await sweep_job_history(db_pool, retention_days=30, batch_size=2) == 2


class TestSweepStrandedWaiters:
    """The level trigger behind the edge-triggered dependency wake.

    The wake normally fires in the statement after the upstream's terminal
    write; these are the cases where that edge was missed (worker crash in
    the window, waiter enqueued after the upstream finished) or where no
    edge will ever fire (the target does not exist). Assertions are on the
    specific rows, never on global sweep counts: the sweep is global, and a
    parallel test's rows may legitimately ride along.
    """

    async def test_wakes_a_waiter_whose_upstream_already_finished(
        self, db_pool, unique_queue
    ):
        """The enqueue-after-terminal race, and the crash-window strand:
        both look identical from the database — waiting on a finished
        upstream — and both must self-heal."""
        upstream = await insert_job(db_pool, unique_queue, state="finished")
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_job=upstream
        )

        await sweep_stranded_waiters(db_pool)

        assert (await get_job(db_pool, waiter))["state"] == "queued"

    async def test_leaves_a_waiter_whose_upstream_is_still_running(
        self, db_pool, unique_queue
    ):
        upstream = await insert_job(db_pool, unique_queue, state="running")
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_job=upstream
        )

        await sweep_stranded_waiters(db_pool)

        assert (await get_job(db_pool, waiter))["state"] == "waiting"

    async def test_leaves_a_waiter_of_a_crashed_upstream_for_the_operator(
        self, db_pool, unique_queue
    ):
        """Crashed IS the DLQ: the upstream may be retried back to life, so
        the platform must not decide for the operator. The condition is
        surfaced by `pj-admin doctor` instead."""
        upstream = await insert_job(db_pool, unique_queue, state="crashed")
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_job=upstream
        )

        await sweep_stranded_waiters(db_pool)

        row = await get_job(db_pool, waiter)
        assert row["state"] == "waiting"

    async def test_wakes_a_group_waiter_once_every_member_is_finished(
        self, db_pool, unique_queue
    ):
        leader = await insert_job(db_pool, unique_queue, state="finished")
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", leader
        )
        await insert_job(
            db_pool, unique_queue, state="finished", run_group=leader
        )
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_group=leader
        )

        await sweep_stranded_waiters(db_pool)

        assert (await get_job(db_pool, waiter))["state"] == "queued"

    async def test_leaves_a_group_waiter_while_any_member_is_unfinished(
        self, db_pool, unique_queue
    ):
        leader = await insert_job(db_pool, unique_queue, state="finished")
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", leader
        )
        await insert_job(
            db_pool, unique_queue, state="running", run_group=leader
        )
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_group=leader
        )

        await sweep_stranded_waiters(db_pool)

        assert (await get_job(db_pool, waiter))["state"] == "waiting"

    async def test_cancels_a_waiter_whose_upstream_does_not_exist(
        self, db_pool, unique_queue
    ):
        """Nothing will ever wake it, so 'waiting' would be forever and
        invisible; cancelled-with-reason is the defined outcome. (Crashed
        would be wrong: a DLQ retry would then RUN the job with its
        dependency unsatisfied.)"""
        ghost = await insert_job(db_pool, unique_queue, state="finished")
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", ghost)
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_job=ghost
        )

        await sweep_stranded_waiters(db_pool)

        row = await get_job(db_pool, waiter)
        assert row["state"] == "cancelled"
        assert f"waitfor_job {ghost} does not exist" in row["error_message"]
        assert row["finished"] is not None

    async def test_cancels_a_waiter_on_a_group_with_no_members(
        self, db_pool, unique_queue
    ):
        ghost = await insert_job(db_pool, unique_queue, state="finished")
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", ghost)
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_group=ghost
        )

        await sweep_stranded_waiters(db_pool)

        row = await get_job(db_pool, waiter)
        assert row["state"] == "cancelled"
        assert f"waitfor_group {ghost} has no jobs" in row["error_message"]

    async def test_cancel_re_checks_the_target_still_absent_under_lock(
        self, db_pool, unique_queue
    ):
        """The probe found the group empty, but a member is inserted before
        the cancel fires. The cancel statement re-verifies target-absence, so
        it must NOT cancel a waiter whose group now exists — closing the
        window where incrementally-built groups get their waiter cancelled."""
        group = await insert_job(db_pool, unique_queue, state="running")
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", group
        )
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_group=group
        )

        # A member of the group now exists (the probe's snapshot did not see it).
        cancelled = await db_pool.fetch(CANCEL_UNSATISFIABLE_WAITERS_SQL, [waiter])

        assert cancelled == []
        assert (await get_job(db_pool, waiter))["state"] == "waiting"

    async def test_wake_re_checks_the_upstream_still_finished_under_lock(
        self, db_pool, unique_queue
    ):
        """The probe found the upstream finished, but it is requeued (a rerun)
        before the wake fires. The wake re-verifies the upstream is still
        finished, so it must NOT wake a waiter whose dependency is running
        again."""
        upstream = await insert_job(db_pool, unique_queue, state="queued")
        waiter = await insert_job(
            db_pool, unique_queue, state="waiting", waitfor_job=upstream
        )

        await db_pool.execute(WAKE_WAITERS_SQL, [waiter])

        assert (await get_job(db_pool, waiter))["state"] == "waiting"

    async def test_woken_waiter_is_claimable_and_actually_runs(
        self, db_pool, unique_queue, live_worker
    ):
        """End to end: a stranded waiter, once woken, is ordinary queued
        work — claimed, executed, finished."""
        upstream = await insert_job(db_pool, unique_queue, state="finished")
        waiter = await insert_job(
            db_pool,
            unique_queue,
            state="waiting",
            waitfor_job=upstream,
            kwargs={"x": 4},
        )

        await sweep_stranded_waiters(db_pool)
        await live_worker()

        row = await wait_for_job_state(db_pool, waiter, ("finished",), timeout=20)
        assert row["result"] == {"doubled": 8}


class TestSweepDeadWorkers:
    async def test_requeues_jobs_of_stale_worker_and_retires_it(
        self, db_pool, unique_queue
    ):
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        claimed = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=dead_worker
        )
        running = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            claimed_by=dead_worker,
            timeout_at_offset_seconds=3600,
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 2

        for job_id in (claimed, running):
            job = await get_job(db_pool, job_id)
            assert job["state"] == "queued"
            assert job["timeout_at"] is None
            # immediately claimable again
            assert job["run_after"] <= await db_pool.fetchval("SELECT now()")

        # the stale worker is retired from the registry
        worker = await db_pool.fetchrow(
            "SELECT * FROM jorb_worker WHERE id = $1", dead_worker
        )
        assert worker["shutdown_at"] is not None

    async def test_leaves_live_workers_and_their_jobs_alone(
        self, db_pool, unique_queue
    ):
        live = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=1)
        job_id = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=live
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 0

        assert (await get_job(db_pool, job_id))["state"] == "running"
        worker = await db_pool.fetchrow("SELECT * FROM jorb_worker WHERE id = $1", live)
        assert worker["shutdown_at"] is None

    async def test_leaves_terminal_jobs_of_a_retired_worker_alone(
        self, db_pool, unique_queue
    ):
        """A gracefully-exited worker (shutdown_at set) leaves its terminal
        jobs exactly where they are — the sweep only touches in-flight ones."""
        retired = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=300)
        await db_pool.execute(
            "UPDATE jorb_worker SET shutdown_at = now() WHERE id = $1", retired
        )
        # a finished job from that worker must not be requeued
        job_id = await insert_job(
            db_pool, unique_queue, state="finished", claimed_by=retired
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "finished"

    async def test_recovers_in_flight_jobs_of_a_retired_worker(
        self, db_pool, unique_queue
    ):
        """Liveness is the heartbeat, not the shutdown flag.

        A worker gets ``shutdown_at`` stamped either by deregistering or by an
        earlier sweep that found its heartbeat stale. Either way, a job still
        'running' behind a long-dead heartbeat has no one executing it — and
        skipping those rows stranded them permanently: this sweep ignored
        retired workers and the stuck-claims sweep only covers 'claimed'
        rows, not 'running' ones."""
        retired = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=300)
        await db_pool.execute(
            "UPDATE jorb_worker SET shutdown_at = now() WHERE id = $1", retired
        )
        running = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=retired
        )
        claimed = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=retired
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 2

        for job_id in (running, claimed):
            assert (await get_job(db_pool, job_id))["state"] == "queued"

    async def test_retired_worker_still_within_grace_is_left_alone(
        self, db_pool, unique_queue
    ):
        """The grace period applies to retired workers too: a worker that just
        deregistered may still be finishing its last job."""
        retired = await insert_worker(db_pool, unique_queue, last_seen_age_seconds=1)
        await db_pool.execute(
            "UPDATE jorb_worker SET shutdown_at = now() WHERE id = $1", retired
        )
        job_id = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=retired
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "running"

    async def test_recovered_job_is_executed_by_a_real_worker(
        self, live_worker, db_pool, unique_queue
    ):
        """End to end: a job orphaned by a dead worker is requeued by the
        sweep and then actually claimed and finished by a live worker."""
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        job_id = await insert_job(
            db_pool,
            unique_queue,
            state="running",
            kwargs={"x": 5},
            claimed_by=dead_worker,
        )

        requeued = await sweep_dead_workers(db_pool, liveness_grace_seconds=60)
        assert requeued == 1

        await live_worker()

        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["result"] == {"doubled": 10}
        # the requeue fenced the dead worker's attempt and the live
        # worker's claim advanced the token again
        assert row["run_epoch"] > 1


# ============================================================================
# stuck-claim reclaim
# ============================================================================


class TestSweepStuckClaims:
    async def test_requeues_a_stale_unregistered_claim(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )

        requeued = await sweep_stuck_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "queued"
        assert job["timeout_at"] is None

    async def test_leaves_fresh_claims_alone(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")

        requeued = await sweep_stuck_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "claimed"

    async def test_requeues_a_lost_ack_claim_under_a_live_worker(
        self, db_pool, unique_queue
    ):
        """THE lost-ack case: the claim committed, the connection dropped
        before the rows reached the worker, and the reconnecting worker
        claimed a different job. The claimer is alive and heartbeating, so
        the dead-worker sweep never fires and the timeout sweep (running
        only) never sees it — a stale 'claimed' is stranded no matter what
        the registry says, and the epoch bump fences the claimer out if it
        ever does come back."""
        worker_id = await insert_worker(db_pool, unique_queue)  # live heartbeat
        job_id = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=worker_id
        )
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )
        epoch_before = (await get_job(db_pool, job_id))["run_epoch"]

        requeued = await sweep_stuck_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "queued"
        assert job["run_epoch"] > epoch_before


# ============================================================================
# retention
# ============================================================================


class TestRetentionCascade:
    async def test_every_child_table_cascades(self, db_pool):
        """What actually reaps the child rows is the schema, not the sweep.

        Pinned per table rather than assumed. jorb_history was the one child
        with no foreign key at all, which meant retention would free the small
        tables and leave the largest one growing; it cascades now like the
        rest."""
        constraints = await db_pool.fetch(
            """
            SELECT conrelid::regclass::text AS child,
                   confdeltype::text        AS on_delete
            FROM pg_constraint
            WHERE contype = 'f' AND confrelid = 'jorb'::regclass
            """
        )

        # 'c' is pg_constraint's code for ON DELETE CASCADE
        cascading = {c["child"] for c in constraints if c["on_delete"] == "c"}
        assert cascading == {
            "jorb_step",
            "jorb_event",
            "jorb_mailbox",
            "jorb_dependencies",
            "jorb_history",
        }
        # every foreign key to jorb cascades; none of them is history's
        assert {c["child"] for c in constraints} == cascading


class TestSweepExpiredJobs:
    """The batch sweep that deletes terminal jobs past the retention window."""

    async def test_expired_job_takes_every_child_row_with_it(
        self, db_pool, unique_queue
    ):
        expired = await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        recent = await insert_terminal_job(db_pool, unique_queue, days_ago=1)
        await add_child_rows(db_pool, expired)
        await add_child_rows(db_pool, recent)

        # enqueued + finished transitions, one row per child table
        assert await child_counts(db_pool, expired) == {
            "jorb_step": 1,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

        deleted = await sweep_expired_jobs(db_pool, retention_days=7)
        assert deleted == 1

        assert await job_ids(db_pool) == [recent]
        assert await child_counts(db_pool, expired) == {
            "jorb_step": 0,
            "jorb_event": 0,
            "jorb_mailbox": 0,
            "jorb_history": 0,
        }
        # the in-window job kept everything
        assert await child_counts(db_pool, recent) == {
            "jorb_step": 1,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

    async def test_deletes_every_terminal_state(self, db_pool, unique_queue):
        for state in ("finished", "crashed", "cancelled"):
            await insert_terminal_job(db_pool, unique_queue, state=state, days_ago=30)

        deleted = await sweep_expired_jobs(db_pool, retention_days=7)
        assert deleted == 3
        assert await job_ids(db_pool) == []

    async def test_terminal_job_inside_the_window_is_untouched(
        self, db_pool, unique_queue
    ):
        recent = await insert_terminal_job(db_pool, unique_queue, days_ago=6)

        deleted = await sweep_expired_jobs(db_pool, retention_days=7)
        assert deleted == 0
        assert await job_ids(db_pool) == [recent]
        assert (await get_job(db_pool, recent))["state"] == "finished"

    @pytest.mark.parametrize("state", ["queued", "claimed", "running", "waiting"])
    async def test_live_job_is_never_deleted_however_old(
        self, db_pool, unique_queue, state
    ):
        """The dangerous failure mode: age must never outrank state.

        A job parked on a dependency, or sleeping durably, is legitimately
        older than any retention window and is still live work."""
        live = await insert_job(db_pool, unique_queue, state=state)
        await age_job(db_pool, live, days=365)
        await add_child_rows(db_pool, live)

        deleted = await sweep_expired_jobs(db_pool, retention_days=1)
        assert deleted == 0
        assert await job_ids(db_pool) == [live]
        assert (await get_job(db_pool, live))["state"] == state
        assert await child_counts(db_pool, live) == {
            "jorb_step": 1,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 1,
        }

    async def test_expired_upstream_of_a_waiting_job_is_kept(
        self, db_pool, unique_queue
    ):
        """Deleting the upstream would strand the waiter forever: nothing but
        the upstream's own terminal transition ever wakes a waitfor_job."""
        upstream = await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        waiter = await insert_job(db_pool, unique_queue, state="waiting")
        await db_pool.execute(
            "UPDATE jorb SET waitfor_job = $2 WHERE id = $1", waiter, upstream
        )

        assert await sweep_expired_jobs(db_pool, retention_days=7) == 0
        assert await job_ids(db_pool) == sorted([upstream, waiter])

        # once nothing waits on it, the upstream expires normally
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", waiter)
        assert await sweep_expired_jobs(db_pool, retention_days=7) == 1
        assert await job_ids(db_pool) == []

    async def test_expired_member_of_a_group_a_waiting_job_needs_is_kept(
        self, db_pool, unique_queue
    ):
        """Same hazard through waitfor_group: the group wakeup counts members
        that are not finished, so deleting one silently changes the verdict."""
        member = await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        await db_pool.execute("UPDATE jorb SET run_group = 4242 WHERE id = $1", member)
        waiter = await insert_job(db_pool, unique_queue, state="waiting")
        await db_pool.execute(
            "UPDATE jorb SET waitfor_group = 4242 WHERE id = $1", waiter
        )

        assert await sweep_expired_jobs(db_pool, retention_days=7) == 0
        assert await job_ids(db_pool) == sorted([member, waiter])

    async def test_batch_size_bounds_one_sweep_taking_the_oldest_first(
        self, db_pool, unique_queue
    ):
        """Batches are bounded, and each one takes the jobs that terminated
        longest ago — the order ``jorb_retention_idx`` provides, and the order
        an operator would expect a retention policy to reap in."""
        # oldest last: ids[4] terminated 34 days ago, ids[0] 30 days ago
        ids = [
            await insert_terminal_job(db_pool, unique_queue, days_ago=30 + n)
            for n in range(5)
        ]

        assert await sweep_expired_jobs(db_pool, retention_days=7, batch_size=2) == 2
        assert await job_ids(db_pool) == sorted(ids[:3])

        assert await sweep_expired_jobs(db_pool, retention_days=7, batch_size=2) == 2
        assert await job_ids(db_pool) == [ids[0]]

        assert await sweep_expired_jobs(db_pool, retention_days=7, batch_size=2) == 1
        assert await job_ids(db_pool) == []

    async def test_sweep_is_idempotent(self, db_pool, unique_queue):
        await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        keep = await insert_terminal_job(db_pool, unique_queue, days_ago=1)

        assert await sweep_expired_jobs(db_pool, retention_days=7) == 1
        assert await sweep_expired_jobs(db_pool, retention_days=7) == 0
        assert await sweep_expired_jobs(db_pool, retention_days=7) == 0
        assert await job_ids(db_pool) == [keep]

    async def test_concurrent_sweeps_partition_the_backlog(self, db_pool, unique_queue):
        """FOR UPDATE SKIP LOCKED means two monitors split the work instead of
        colliding: no error, no double count, no orphaned history."""
        for _ in range(6):
            await insert_terminal_job(db_pool, unique_queue, days_ago=30)

        counts = await asyncio.gather(
            sweep_expired_jobs(db_pool, retention_days=7, batch_size=4),
            sweep_expired_jobs(db_pool, retention_days=7, batch_size=4),
        )

        assert sum(counts) == 6
        assert await job_ids(db_pool) == []
        assert await db_pool.fetchval("SELECT count(*) FROM jorb_history") == 0


class TestSweepCompletedCheckpoints:
    """Checkpoints live on their own, much shorter window — but ONLY for
    `finished` jobs.

    A checkpoint exists to make a job resumable. A `finished` job is only
    ever re-run by an explicit `rerun_job` ("do it again anyway"), which is
    supposed to re-execute, so its checkpoints are pure audit from the moment
    it finishes. But `crashed`/`cancelled` jobs are RETRYABLE, and `retry_job`
    resumes from their checkpoints — so those must survive the short window
    and are reaped only when the whole job ages out under --retention-days."""

    async def test_terminal_job_loses_checkpoints_and_keeps_everything_else(
        self, db_pool, unique_queue
    ):
        """The whole point: two lifetimes, not one."""
        job = await insert_terminal_job(db_pool, unique_queue, days_ago=3)
        await add_child_rows(db_pool, job)
        assert await child_counts(db_pool, job) == {
            "jorb_step": 1,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1
        )
        assert deleted == 1

        assert await job_ids(db_pool) == [job]
        assert await child_counts(db_pool, job) == {
            "jorb_step": 0,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

    async def test_reaps_finished_but_preserves_retryable_checkpoints(
        self, db_pool, unique_queue
    ):
        """A crashed or cancelled job's checkpoints are what a DLQ retry
        resumes from; reaping them early would re-run every completed step.
        Only `finished` checkpoints (never resumed except by explicit rerun)
        are dropped on the short window."""
        by_state = {
            state: await insert_terminal_job(
                db_pool, unique_queue, state=state, days_ago=3
            )
            for state in ("finished", "crashed", "cancelled")
        }
        for job in by_state.values():
            await add_checkpoints(db_pool, job, 2)

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1
        )
        assert deleted == 2  # only the finished job's two checkpoints

        assert await job_ids(db_pool) == sorted(by_state.values())
        remaining = {
            state: await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id = $1", job
            )
            for state, job in by_state.items()
        }
        assert remaining == {"finished": 0, "crashed": 2, "cancelled": 2}

    async def test_job_terminated_inside_the_window_keeps_its_checkpoints(
        self, db_pool, unique_queue
    ):
        job = await insert_terminal_job(db_pool, unique_queue, days_ago=0.5)
        seqs = await add_checkpoints(db_pool, job, 3)

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1
        )
        assert deleted == 0
        assert await step_seqs(db_pool, job) == seqs

    @pytest.mark.parametrize("state", ["queued", "claimed", "running", "waiting"])
    async def test_live_job_keeps_its_checkpoints_however_old(
        self, db_pool, unique_queue, state
    ):
        """The dangerous failure mode.

        A durable sleep parks a job in 'queued' for months holding the very
        checkpoint that records when to wake; deleting it would make the job
        silently re-run steps it already completed."""
        live = await insert_job(db_pool, unique_queue, state=state)
        await age_job(db_pool, live, days=365)
        await add_checkpoints(
            db_pool, live, 2, name="sleep", output={"wake_at": "2027-01-01T00:00:00Z"}
        )

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1
        )
        assert deleted == 0

        assert await step_seqs(db_pool, live) == [1, 2]
        # the checkpoint is intact, not merely present
        surviving = await db_pool.fetch(
            "SELECT name, output FROM jorb_step WHERE job_id = $1 ORDER BY step_seq",
            live,
        )
        assert [r["name"] for r in surviving] == ["sleep-1", "sleep-2"]
        assert surviving[0]["output"] == {"wake_at": "2027-01-01T00:00:00Z"}
        assert (await get_job(db_pool, live))["state"] == state

    async def test_batch_size_bounds_one_sweep(self, db_pool, unique_queue):
        """``batch_size`` bounds the JOBS a sweep takes, not their rows.

        A job's checkpoints always go together: the sweep finds its victims
        by walking jorb_retention_idx (which is on the JOB's terminal time)
        and then deletes by job id, so there is no cheap way to stop
        half-way through one job and no reason to want one. Splitting a
        batch mid-job would also mean a second index-driven lookup on
        jorb_step for every batch, which is the cost this sweep was
        rewritten to stop paying."""
        jobs = [
            await insert_terminal_job(db_pool, unique_queue, days_ago=3 + n)
            for n in range(3)
        ]
        for job in jobs:
            await add_checkpoints(db_pool, job, 2)

        # two jobs per batch, two checkpoints each
        for expected, remaining in ((4, 2), (2, 0), (0, 0)):
            deleted = await sweep_completed_checkpoints(
                db_pool, checkpoint_retention_days=1, batch_size=2
            )
            left = 0
            for job in jobs:
                left += len(await step_seqs(db_pool, job))
            assert (deleted, left) == (expected, remaining)

        # the job rows themselves were never in scope
        assert await job_ids(db_pool) == sorted(jobs)

    async def test_checkpoints_of_the_longest_dead_job_go_first(
        self, db_pool, unique_queue
    ):
        """Across jobs the order IS exact: oldest terminal job first."""
        older = await insert_terminal_job(db_pool, unique_queue, days_ago=10)
        newer = await insert_terminal_job(db_pool, unique_queue, days_ago=3)
        await add_checkpoints(db_pool, older, 1)
        await add_checkpoints(db_pool, newer, 1)

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1, batch_size=1
        )
        assert deleted == 1
        assert await step_seqs(db_pool, older) == []
        assert await step_seqs(db_pool, newer) == [1]

    async def test_the_two_windows_are_independent(self, db_pool, unique_queue):
        """Checkpoints go at 1 day while the job row sits well inside its own
        30-day window — that separation is the reason this sweep exists."""
        job = await insert_terminal_job(db_pool, unique_queue, days_ago=5)
        await add_child_rows(db_pool, job)

        assert (
            await sweep_completed_checkpoints(db_pool, checkpoint_retention_days=1) == 1
        )
        assert await sweep_expired_jobs(db_pool, retention_days=30) == 0

        assert await job_ids(db_pool) == [job]
        assert await child_counts(db_pool, job) == {
            "jorb_step": 0,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

        # ...and the job goes on its own schedule once past the longer window
        await age_job(db_pool, job, days=40)
        assert await sweep_expired_jobs(db_pool, retention_days=30) == 1
        assert await job_ids(db_pool) == []

    async def test_concurrent_sweeps_partition_the_backlog(self, db_pool, unique_queue):
        job = await insert_terminal_job(db_pool, unique_queue, days_ago=3)
        await add_checkpoints(db_pool, job, 6)

        counts = await asyncio.gather(
            sweep_completed_checkpoints(
                db_pool, checkpoint_retention_days=1, batch_size=4
            ),
            sweep_completed_checkpoints(
                db_pool, checkpoint_retention_days=1, batch_size=4
            ),
        )

        assert sum(counts) == 6
        assert await step_seqs(db_pool, job) == []
        assert await job_ids(db_pool) == [job]


class TestRetentionDrain:
    """One retention cycle drains its backlog instead of taking one bite.

    batch_size per check_interval is a fixed deletion rate. Any platform
    ingesting faster than it outruns retention permanently — the operator
    surface says retention is on while the table grows forever."""

    async def test_one_cycle_clears_a_multi_batch_backlog(self, db_pool, unique_queue):
        ids = [
            await insert_terminal_job(db_pool, unique_queue, days_ago=30 + n)
            for n in range(5)
        ]

        deleted = await _drain(
            "expired jobs",
            lambda: sweep_expired_jobs(db_pool, retention_days=7, batch_size=2),
            batch_size=2,
            max_seconds=5.0,
        )

        # all five in ONE cycle, not two per cycle
        assert deleted == len(ids)
        assert await job_ids(db_pool) == []

    async def test_a_caught_up_cycle_reports_the_count(self, db_pool, unique_queue):
        await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        await insert_terminal_job(db_pool, unique_queue, days_ago=31)

        with captured_logs("INFO") as messages:
            deleted = await _drain(
                "expired jobs",
                lambda: sweep_expired_jobs(db_pool, retention_days=7, batch_size=10),
                batch_size=10,
                max_seconds=5.0,
            )

        assert deleted == 2
        assert [m for m in messages if "caught up" in m] == [
            "Retention expired jobs: deleted 2, caught up\n"
        ]

    async def test_an_empty_cycle_says_nothing(self, db_pool, unique_queue):
        await insert_terminal_job(db_pool, unique_queue, days_ago=1)

        with captured_logs("INFO") as messages:
            deleted = await _drain(
                "expired jobs",
                lambda: sweep_expired_jobs(db_pool, retention_days=7, batch_size=10),
                batch_size=10,
                max_seconds=5.0,
            )

        assert deleted == 0
        assert messages == []

    async def test_the_time_budget_stops_the_cycle_and_warns(self):
        """Falling behind must be loud: silence reads exactly like caught up.

        The fake sweep always returns a full batch, so there is always more to
        do; a spent budget is the only thing that can end the cycle."""
        calls = []

        async def endless_sweep() -> int:
            calls.append(1)
            return 2

        with captured_logs("WARNING") as messages:
            deleted = await _drain(
                "expired jobs", endless_sweep, batch_size=2, max_seconds=0.0
            )

        # exactly one batch, then it yields the cycle
        assert (deleted, len(calls)) == (2, 1)
        assert len(messages) == 1
        assert "deleted 2" in messages[0]
        assert "falling behind" in messages[0]

    async def test_the_budget_still_allows_several_batches(self):
        calls = []

        async def endless_sweep() -> int:
            calls.append(1)
            await asyncio.sleep(0.01)
            return 2

        with captured_logs("WARNING") as messages:
            deleted = await _drain(
                "expired jobs", endless_sweep, batch_size=2, max_seconds=0.1
            )

        assert len(calls) >= 3  # many batches within one cycle's budget
        assert deleted == 2 * len(calls)
        assert len(messages) == 1


class TestSweepConsumedMailbox:
    """Consumed messages of jobs that are still alive: the cascade cannot
    reach them because the job never goes away."""

    async def test_prunes_old_consumed_messages_of_a_live_job(
        self, db_pool, unique_queue
    ):
        live = await insert_job(db_pool, unique_queue, state="running")
        old_consumed = await db_pool.fetchval(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
            VALUES ($1, 'ping', $2, now() - interval '30 days') RETURNING id
            """,
            live,
            {"n": 1},
        )
        recent_consumed = await db_pool.fetchval(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
            VALUES ($1, 'ping', $2, now() - interval '1 day') RETURNING id
            """,
            live,
            {"n": 2},
        )
        pending = await db_pool.fetchval(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, created)
            VALUES ($1, 'ping', $2, now() - interval '90 days') RETURNING id
            """,
            live,
            {"n": 3},
        )

        deleted = await sweep_consumed_mailbox(db_pool, retention_days=7)
        assert deleted == 1

        surviving = [
            r["id"]
            for r in await db_pool.fetch("SELECT id FROM jorb_mailbox ORDER BY id")
        ]
        # the unread message survives at any age — it is still deliverable
        assert surviving == sorted([recent_consumed, pending])
        assert old_consumed not in surviving
        # the job itself is untouched
        assert await job_ids(db_pool) == [live]

    async def test_batch_size_bounds_one_sweep(self, db_pool, unique_queue):
        live = await insert_job(db_pool, unique_queue, state="running")
        ids = [
            await db_pool.fetchval(
                """
                INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
                VALUES ($1, 'ping', $2, now() - interval '30 days') RETURNING id
                """,
                live,
                {},
            )
            for _ in range(5)
        ]

        async def surviving() -> list[int]:
            return [
                r["id"]
                for r in await db_pool.fetch("SELECT id FROM jorb_mailbox ORDER BY id")
            ]

        for expected, remaining in ((2, ids[2:]), (2, ids[4:]), (1, []), (0, [])):
            deleted = await sweep_consumed_mailbox(
                db_pool, retention_days=7, batch_size=2
            )
            left = await surviving()
            assert (deleted, left) == (expected, remaining)


# ============================================================================
# the other three tables the job cascade cannot reach
# (the fourth, jorb_mailbox, is above: a live job's read mail)
# ============================================================================


class TestSweepOrphanedDags:
    """jorb_dag is the PARENT of the relationship, so nothing cascades to it.

    That makes it the only retention target where the row's size is beside
    the point: an emptied DAG does not merely linger, it goes on ANSWERING —
    jorb_dag_status LEFT JOINs jorb, so the moment job retention removes its
    last job the view reports total_jobs = 0 for it, forever.
    """

    async def add_dag_job(
        self, pool, queue: str, dag_id: int, *, state: str = "finished", days_ago: float
    ) -> int:
        job_id = await insert_terminal_job(pool, queue, state=state, days_ago=days_ago)
        await pool.execute("UPDATE jorb SET dag_id = $2 WHERE id = $1", job_id, dag_id)
        return job_id

    async def test_an_emptied_dag_reports_a_lie_until_the_sweep_removes_it(
        self, db_pool, unique_queue
    ):
        """The wrong answer, produced end to end and then fixed.

        This is the whole case for the sweep: it is not "a few small rows
        leak", it is "`pj-admin dag list` tells an operator that a DAG which
        ran two jobs to completion ran nothing at all, and keeps saying so".
        """
        dag = await insert_dag(db_pool, "nightly", days_ago=40)
        first = await self.add_dag_job(db_pool, unique_queue, dag, days_ago=40)
        second = await self.add_dag_job(db_pool, unique_queue, dag, days_ago=40)

        before = await dag_status(db_pool, dag)
        assert (before["total_jobs"], before["finished_jobs"]) == (2, 2)

        # ...retention comes past and takes the jobs, exactly as designed
        assert await sweep_expired_jobs(db_pool, retention_days=30) == 2
        assert await job_ids(db_pool) == []

        # THE WRONG ANSWER: a DAG that ran two jobs now reads as one that
        # ran none, and no later event ever corrects it.
        ghost = await dag_status(db_pool, dag)
        assert (
            ghost["total_jobs"],
            ghost["finished_jobs"],
            ghost["crashed_jobs"],
            ghost["pending_jobs"],
        ) == (0, 0, 0, 0)
        assert ghost["name"] == "nightly"

        assert await sweep_orphaned_dags(db_pool, retention_days=30) == 1

        assert await dag_ids(db_pool) == []
        assert await dag_status(db_pool, dag) is None
        # and the jobs it once had are still gone, not resurrected by a
        # foreign key doing something clever
        assert await job_ids(db_pool) == []
        assert first != second

    async def test_a_dag_with_one_surviving_job_is_kept_however_old(
        self, db_pool, unique_queue
    ):
        """The refusal. The DAG row is what gives a surviving job its group,
        and the foreign key would silently NULL it on the way out."""
        emptied = await insert_dag(db_pool, "emptied", days_ago=400)
        populated = await insert_dag(db_pool, "populated", days_ago=400)
        survivor = await self.add_dag_job(db_pool, unique_queue, populated, days_ago=1)

        assert await sweep_orphaned_dags(db_pool, retention_days=30) == 1

        assert await dag_ids(db_pool) == [populated]
        assert emptied not in await dag_ids(db_pool)
        # the surviving job kept its group
        assert (
            await db_pool.fetchval("SELECT dag_id FROM jorb WHERE id = $1", survivor)
            == populated
        )

    async def test_a_live_job_keeps_its_dag_at_any_age(self, db_pool, unique_queue):
        """A 'waiting' job is exactly the shape retention already protects on
        the job side: it is live work, however old the row looks."""
        dag = await insert_dag(db_pool, "long-runner", days_ago=400)
        job_id = await insert_job(db_pool, unique_queue, state="waiting")
        await db_pool.execute("UPDATE jorb SET dag_id = $2 WHERE id = $1", job_id, dag)
        await age_job(db_pool, job_id, days=400)

        assert await sweep_orphaned_dags(db_pool, retention_days=30) == 0
        assert await dag_ids(db_pool) == [dag]

    async def test_a_dag_inside_the_window_is_kept_even_with_no_jobs(self, db_pool):
        """A DAG with no jobs YET is a real state — DAGBuilder writes this row
        before the jobs it will own. Age is what tells that apart from a DAG
        whose jobs are gone, and it is the only thing that can."""
        fresh = await insert_dag(db_pool, "being-built", days_ago=0)

        assert await sweep_orphaned_dags(db_pool, retention_days=30) == 0
        assert await dag_ids(db_pool) == [fresh]

    async def test_batch_size_bounds_one_sweep_oldest_first(self, db_pool):
        dags = [
            await insert_dag(db_pool, f"dag-{n}", days_ago=100 - n) for n in range(5)
        ]

        for expected, remaining in ((2, dags[2:]), (2, dags[4:]), (1, []), (0, [])):
            deleted = await sweep_orphaned_dags(
                db_pool, retention_days=30, batch_size=2
            )
            assert (deleted, await dag_ids(db_pool)) == (expected, remaining)


class TestSweepScheduleLog:
    """One row per schedule execution, cascading only from a table operators
    disable rather than delete. Before this sweep it had no bound at all."""

    async def test_old_executions_go_and_the_newest_survives(self, db_pool, test_id):
        """The refusal: `pj-admin schedule history` must never come back
        empty for a schedule that has in fact run."""
        schedule = await insert_schedule(db_pool, f"nightly_{test_id}")
        ancient = await log_execution(db_pool, schedule, "nightly", days_ago=400)
        old = await log_execution(db_pool, schedule, "nightly", days_ago=90)
        newest = await log_execution(db_pool, schedule, "nightly", days_ago=45)

        assert await sweep_schedule_log(db_pool, retention_days=30) == 2

        assert await schedule_log_ids(db_pool) == [newest]
        assert ancient not in await schedule_log_ids(db_pool)
        assert old not in await schedule_log_ids(db_pool)
        # the schedule itself is untouched
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_schedule WHERE id = $1", schedule
            )
            == 1
        )

    async def test_a_rarely_firing_schedules_only_execution_is_never_deleted(
        self, db_pool, test_id
    ):
        """A yearly schedule's single execution is older than any sane window,
        and deleting it would make the schedule read as "never ran" while
        jorb_schedule.last_run says otherwise."""
        schedule = await insert_schedule(db_pool, f"yearly_{test_id}")
        only = await log_execution(db_pool, schedule, "yearly", days_ago=400)

        assert await sweep_schedule_log(db_pool, retention_days=30) == 0
        assert await schedule_log_ids(db_pool) == [only]

    async def test_executions_inside_the_window_are_kept(self, db_pool, test_id):
        schedule = await insert_schedule(db_pool, f"hourly_{test_id}")
        recent = [
            await log_execution(db_pool, schedule, "hourly", days_ago=days)
            for days in (5, 3, 1)
        ]

        assert await sweep_schedule_log(db_pool, retention_days=30) == 0
        assert await schedule_log_ids(db_pool) == recent

    async def test_each_schedule_keeps_its_own_newest(self, db_pool, test_id):
        """The rule is per schedule, not per table: reaping everything but
        one global row would erase whole schedules' histories."""
        first = await insert_schedule(db_pool, f"a_{test_id}")
        second = await insert_schedule(db_pool, f"b_{test_id}")
        await log_execution(db_pool, first, "a", days_ago=400)
        first_newest = await log_execution(db_pool, first, "a", days_ago=200)
        await log_execution(db_pool, second, "b", days_ago=300)
        second_newest = await log_execution(db_pool, second, "b", days_ago=100)

        assert await sweep_schedule_log(db_pool, retention_days=30) == 2
        assert await schedule_log_ids(db_pool) == sorted([first_newest, second_newest])

    async def test_batch_size_bounds_one_sweep_oldest_first(self, db_pool, test_id):
        schedule = await insert_schedule(db_pool, f"minutely_{test_id}")
        rows = [
            await log_execution(db_pool, schedule, "minutely", days_ago=100 - n)
            for n in range(5)
        ]

        # rows[4] is the newest and is never eligible, so the backlog is four
        # rows and the third batch comes back empty — which is what tells the
        # drain loop it is caught up rather than out of work to look at
        for expected, remaining in ((2, rows[2:]), (2, rows[4:]), (0, rows[4:])):
            deleted = await sweep_schedule_log(db_pool, retention_days=30, batch_size=2)
            assert (deleted, await schedule_log_ids(db_pool)) == (expected, remaining)


class TestSweepRetiredWorkers:
    """One row per worker PROCESS START, and until now nothing ever deleted
    one — shutdown only stamps the row."""

    async def test_a_long_retired_worker_is_deleted(self, db_pool, unique_queue):
        gone = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, gone, days_ago=90)

        assert await sweep_retired_workers(db_pool, retention_days=30) == 1
        assert await worker_ids(db_pool) == []

    async def test_a_live_worker_is_never_deleted(self, db_pool, unique_queue):
        """Deleting a live worker's row would be silent and total: its
        heartbeat UPDATE would match nothing and every liveness surface would
        say the process is gone while it kept claiming."""
        live = await insert_worker(db_pool, unique_queue)
        await db_pool.execute(
            "UPDATE jorb_worker SET started = now() - interval '400 days' WHERE id = $1",
            live,
        )

        assert await sweep_retired_workers(db_pool, retention_days=30) == 0
        assert await worker_ids(db_pool) == [live]

    async def test_a_worker_retired_during_a_blip_that_came_back_is_kept(
        self, db_pool, unique_queue
    ):
        """shutdown_at is set (the monitor retired it) but it is beating
        again, so it is alive: both clocks have to be stale, not one."""
        resurrected = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, resurrected, days_ago=90)
        await db_pool.execute(
            "UPDATE jorb_worker SET last_seen = now() WHERE id = $1", resurrected
        )

        assert await sweep_retired_workers(db_pool, retention_days=30) == 0
        assert await worker_ids(db_pool) == [resurrected]

    async def test_a_worker_still_owning_in_flight_work_is_refused(
        self, db_pool, unique_queue
    ):
        """The dangerous one. jorb.claimed_by carries no foreign key, so
        deleting this row would strand a RUNNING job permanently: the
        dead-worker sweep finds orphans by JOINing to jorb_worker, and the
        stuck-claims sweep only covers 'claimed' rows."""
        holder = await insert_worker(db_pool, unique_queue)
        stranded = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=holder
        )
        await retire_worker(db_pool, holder, days_ago=90)

        assert await sweep_retired_workers(db_pool, retention_days=30) == 0
        assert await worker_ids(db_pool) == [holder]

        # ...and because the row survived, recovery still works
        assert await sweep_dead_workers(db_pool, liveness_grace_seconds=60) == 1
        assert (await get_job(db_pool, stranded))["state"] == "queued"

        # now the job is no longer in flight, so the next cycle takes the row
        assert await sweep_retired_workers(db_pool, retention_days=30) == 1
        assert await worker_ids(db_pool) == []

    async def test_a_worker_whose_jobs_all_finished_is_deleted(
        self, db_pool, unique_queue
    ):
        """The common case: the jobs are terminal, and worker_host/worker_pid
        on the job row carry everything the registry row was telling anyone."""
        gone = await insert_worker(db_pool, unique_queue)
        job_id = await insert_job(db_pool, unique_queue, claimed_by=gone)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1", job_id
        )
        await retire_worker(db_pool, gone, days_ago=90)

        assert await sweep_retired_workers(db_pool, retention_days=30) == 1
        assert await worker_ids(db_pool) == []
        assert await job_ids(db_pool) == [job_id]

    async def test_a_recently_retired_worker_is_kept(self, db_pool, unique_queue):
        recent = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, recent, days_ago=1)

        assert await sweep_retired_workers(db_pool, retention_days=30) == 0
        assert await worker_ids(db_pool) == [recent]

    async def test_batch_size_bounds_one_sweep_oldest_first(
        self, db_pool, unique_queue
    ):
        workers = []
        for n in range(5):
            worker_id = await insert_worker(db_pool, unique_queue)
            await retire_worker(db_pool, worker_id, days_ago=100 - n)
            workers.append(worker_id)

        for expected, remaining in (
            (2, workers[2:]),
            (2, workers[4:]),
            (1, []),
            (0, []),
        ):
            deleted = await sweep_retired_workers(
                db_pool, retention_days=30, batch_size=2
            )
            assert (deleted, await worker_ids(db_pool)) == (expected, remaining)


# ============================================================================
# run_epoch fencing
# ============================================================================


class TestEpochFencing:
    async def test_monitor_requeue_fences_out_stale_finish(self, db_pool, unique_queue):
        """After the monitor requeues a timed-out job and a new attempt
        claims it, the ORIGINAL execution's finish must be a no-op."""
        job_id = await insert_job(db_pool, unique_queue)

        # attempt 1 claims (epoch 1), starts running, and stalls past its
        # deadline
        stale_epoch = await hand_claim(db_pool, job_id)
        assert stale_epoch == 1
        await db_pool.execute(
            "UPDATE jorb SET timeout_at = now() - interval '5 seconds' WHERE id = $1",
            job_id,
        )

        handled = await sweep_timed_out_jobs(db_pool)
        assert handled == 1
        assert (await get_job(db_pool, job_id))["state"] == "queued"

        # attempt 2 claims the requeued row
        new_epoch = await hand_claim(db_pool, job_id)
        assert new_epoch > stale_epoch

        # the zombie from attempt 1 comes back and tries to complete
        fenced = await db_pool.fetch(
            STMTS["finished"], job_id, {"from": "stale-attempt"}, stale_epoch
        )
        assert fenced == []

        job = await get_job(db_pool, job_id)
        assert job["state"] == "running"  # attempt 2 still owns the row
        assert job["result"] is None
        assert job["run_epoch"] == new_epoch

        # ...while the current attempt's completion succeeds
        done = await db_pool.fetch(
            STMTS["finished"], job_id, {"from": "current-attempt"}, new_epoch
        )
        assert len(done) == 1
        job = await get_job(db_pool, job_id)
        assert job["state"] == "finished"
        assert job["result"] == {"from": "current-attempt"}

    async def test_monitor_requeue_fences_out_stale_retry(self, db_pool, unique_queue):
        """A superseded attempt cannot push the job back to queued either
        (its retry statement is epoch-fenced exactly like its finish)."""
        job_id = await insert_job(db_pool, unique_queue)
        stale_epoch = await hand_claim(db_pool, job_id)
        await db_pool.execute(
            "UPDATE jorb SET timeout_at = now() - interval '5 seconds' WHERE id = $1",
            job_id,
        )
        await sweep_timed_out_jobs(db_pool)
        live_epoch = await hand_claim(db_pool, job_id)  # a new attempt owns it
        assert live_epoch > stale_epoch

        fenced = await db_pool.fetch(
            STMTS["retry"],
            job_id,
            datetime.timedelta(seconds=1),
            "stale error",
            "stale backtrace",
            stale_epoch,
        )
        assert fenced == []
        job = await get_job(db_pool, job_id)
        assert job["state"] == "running"
        assert job["run_epoch"] == live_epoch


# ============================================================================
# the monitor loop
# ============================================================================


class TestMonitorLoop:
    async def test_loop_runs_all_sweeps(self, db_pool, unique_queue, db_params):
        """monitor() repeatedly sweeps timeouts, dead workers, and
        unregistered claims until cancelled."""
        timed_out = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=dead_worker
        )
        unregistered = await insert_job(db_pool, unique_queue, state="claimed")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            unregistered,
        )

        task = asyncio.create_task(
            monitor(
                # this session's database, not the base DSN: under xdist each
                # worker owns its own database and the monitor must sweep the
                # one holding this test's rows
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                claimed_grace_seconds=300,
            )
        )
        try:
            for job_id in (timed_out, orphaned, unregistered):
                await wait_for_job_state(db_pool, job_id, ("queued",), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        worker = await db_pool.fetchrow(
            "SELECT * FROM jorb_worker WHERE id = $1", dead_worker
        )
        assert worker["shutdown_at"] is not None

    async def test_db_params_reach_asyncpg_whole(
        self, db_pool, unique_queue, db_params
    ):
        """--config mode hands asyncpg the db_params table itself.

        It used to rebuild a five-key URL from it by string interpolation,
        which dropped every other key (ssl, server_settings,
        statement_cache_size, ...) and could not express a unix-socket host
        at all — so the one daemon an operator configures by file was the one
        that could not use the file's settings. The proof here is a key no
        such rebuild ever carried: server_settings.application_name, read
        back out of pg_stat_activity."""
        marker = f"pj-monitor-{uuid.uuid4().hex[:8]}"
        timed_out = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )

        task = asyncio.create_task(
            monitor(
                {**db_params, "server_settings": {"application_name": marker}},
                check_interval=0.1,
                liveness_grace_seconds=60,
            )
        )
        try:
            # it really is the monitor: the sweep it exists for happened
            await wait_for_job_state(db_pool, timed_out, ("queued",), timeout=10)
            connections = await db_pool.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1",
                marker,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert connections >= 1, "the monitor's pool dropped the extra db_params"

    async def test_one_failing_sweep_does_not_starve_the_others(
        self, db_pool, unique_queue, db_params, monkeypatch
    ):
        """The sweeps are independent safety nets.

        A failing timeout sweep used to abort the whole cycle, so dead-worker
        recovery never ran while the failure persisted — the reaper looked
        alive while reaping nothing."""
        sweep_attempted = asyncio.Event()

        async def exploding_sweep(*args, **kwargs):
            sweep_attempted.set()
            raise RuntimeError("timeout sweep is broken")

        monkeypatch.setattr(monitor_module, "sweep_timed_out_jobs", exploding_sweep)

        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=dead_worker
        )

        task = asyncio.create_task(
            monitor(dsn_from(db_params), check_interval=0.1, liveness_grace_seconds=60)
        )
        try:
            await asyncio.wait_for(sweep_attempted.wait(), timeout=10)
            await wait_for_job_state(db_pool, orphaned, ("queued",), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_retention_is_on_by_default(self, db_pool, unique_queue, db_params):
        """A retention policy nobody remembers to switch on is not a policy.

        A loop started with no retention arguments at all reaps a 10-year-old
        terminal job on the 30-day default and its checkpoints on the 1-day
        one, while a job that finished yesterday keeps its row."""
        params = inspect.signature(monitor).parameters
        assert params["retention_days"].default == 30.0
        assert params["checkpoint_retention_days"].default == 1.0

        ancient = await insert_terminal_job(db_pool, unique_queue, days_ago=3650)
        await add_child_rows(db_pool, ancient)
        recent = await insert_terminal_job(db_pool, unique_queue, days_ago=1)

        task = asyncio.create_task(
            monitor(dsn_from(db_params), check_interval=0.1, liveness_grace_seconds=60)
        )
        try:
            await wait_for_job_gone(db_pool, ancient, timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await job_ids(db_pool) == [recent]
        assert await child_counts(db_pool, ancient) == {
            "jorb_step": 0,
            "jorb_event": 0,
            "jorb_mailbox": 0,
            "jorb_history": 0,
        }

    async def test_retention_days_zero_keeps_everything_forever(
        self, db_pool, unique_queue, db_params
    ):
        """The escape hatch, for operators who keep their audit trail.

        The dead-worker requeue is the barrier: once it lands, the loop has
        completed a full cycle, so the ancient job's survival is a decision
        and not a race."""
        ancient = await insert_terminal_job(db_pool, unique_queue, days_ago=3650)
        await add_child_rows(db_pool, ancient)
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=dead_worker
        )
        # every table no cascade reaches shares the one window, so `0`
        # has to be the escape hatch for all of them too
        empty_dag = await insert_dag(db_pool, "ancient", days_ago=3650)
        schedule = await insert_schedule(db_pool, f"ancient_{unique_queue}")
        old_run = await log_execution(db_pool, schedule, "ancient", days_ago=3650)
        newer_run = await log_execution(db_pool, schedule, "ancient", days_ago=3600)
        retired = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, retired, days_ago=3650)

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_days=0,
                checkpoint_retention_days=0,
            )
        )
        try:
            await wait_for_job_state(db_pool, orphaned, ("queued",), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await job_ids(db_pool) == sorted([ancient, orphaned])
        assert await child_counts(db_pool, ancient) == {
            "jorb_step": 1,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }
        assert await dag_ids(db_pool) == [empty_dag]
        assert await schedule_log_ids(db_pool) == [old_run, newer_run]
        # the dead worker was RETIRED by its sweep (that is recovery, not
        # retention) but no registry row was deleted
        assert await worker_ids(db_pool) == sorted([dead_worker, retired])

    async def test_loop_reaps_checkpoints_of_a_job_it_still_keeps(
        self, db_pool, unique_queue, db_params
    ):
        """The two windows stay independent inside the running daemon: the
        checkpoints of a job that terminated 5 days ago go, the job does not."""
        job = await insert_terminal_job(db_pool, unique_queue, days_ago=5)
        await add_child_rows(db_pool, job)

        task = asyncio.create_task(
            monitor(dsn_from(db_params), check_interval=0.1, liveness_grace_seconds=60)
        )
        try:
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM jorb_step WHERE job_id = $1)", job
                ),
                timeout=10,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await job_ids(db_pool) == [job]
        assert await child_counts(db_pool, job) == {
            "jorb_step": 0,
            "jorb_event": 1,
            "jorb_mailbox": 1,
            "jorb_history": 2,
        }

    async def test_loop_runs_the_retention_sweeps(
        self, db_pool, unique_queue, db_params
    ):
        expired = await insert_terminal_job(db_pool, unique_queue, days_ago=30)
        await add_child_rows(db_pool, expired)
        recent = await insert_terminal_job(db_pool, unique_queue, days_ago=1)

        live = await insert_job(db_pool, unique_queue, state="running")
        await db_pool.execute(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
            VALUES ($1, 'ping', $2, now() - interval '30 days')
            """,
            live,
            {},
        )

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_days=7,
            )
        )
        try:
            await wait_for_job_gone(db_pool, expired, timeout=10)
            await wait_for_mailbox_empty(db_pool, live, timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await job_ids(db_pool) == sorted([recent, live])
        assert await child_counts(db_pool, expired) == {
            "jorb_step": 0,
            "jorb_event": 0,
            "jorb_mailbox": 0,
            "jorb_history": 0,
        }

    async def test_loop_reaps_the_tables_no_cascade_reaches(
        self, db_pool, unique_queue, db_params
    ):
        """The DAG, the schedule log and the worker registry are cleaned by
        the same cycle and the same window as the jobs.

        The DAG is the one that has to happen in ORDER: its jobs are deleted
        by the sweep ahead of it in the same cycle, and only then is it
        empty. If the sweeps were reordered it would take an extra interval —
        an extra interval of `dag list` reporting a DAG that ran nothing.
        """
        dag = await insert_dag(db_pool, "nightly", days_ago=40)
        dag_job = await insert_terminal_job(db_pool, unique_queue, days_ago=40)
        await db_pool.execute("UPDATE jorb SET dag_id = $2 WHERE id = $1", dag_job, dag)

        schedule = await insert_schedule(db_pool, f"nightly_{unique_queue}")
        stale_run = await log_execution(db_pool, schedule, "nightly", days_ago=40)
        last_run = await log_execution(db_pool, schedule, "nightly", days_ago=35)

        gone = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, gone, days_ago=40)
        live = await insert_worker(db_pool, unique_queue)

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_days=7,
            )
        )
        try:
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM jorb_dag WHERE id = $1)", dag
                ),
                timeout=10,
            )
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM jorb_worker WHERE id = $1)", gone
                ),
                timeout=10,
            )
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM jorb_schedule_log WHERE id = $1)",
                    stale_run,
                ),
                timeout=10,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await dag_ids(db_pool) == []
        assert await job_ids(db_pool) == []
        # the schedule keeps its newest execution, and the live worker stays
        assert await schedule_log_ids(db_pool) == [last_run]
        assert await worker_ids(db_pool) == [live]

    async def test_failing_retention_sweep_does_not_starve_the_others(
        self, db_pool, unique_queue, db_params, monkeypatch
    ):
        """Retention is the newest sweep and the one most likely to hit a lock
        timeout on a big table; it must not take recovery down with it.

        Attempts are counted rather than flagged: a second attempt proves the
        loop kept cycling instead of dying on the first exception."""
        attempts = []

        async def exploding_sweep(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("retention sweep is broken")

        monkeypatch.setattr(monitor_module, "sweep_expired_jobs", exploding_sweep)

        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=dead_worker
        )

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_days=7,
            )
        )
        try:
            await wait_until(lambda: len(attempts) >= 2, timeout=10)
            await wait_for_job_state(db_pool, orphaned, ("queued",), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_a_failing_dag_sweep_does_not_starve_the_sweeps_behind_it(
        self, db_pool, unique_queue, db_params, monkeypatch
    ):
        """The DAG sweep sits in the middle of the retention block, so its
        failure is the one that could swallow the three behind it — and it is
        the sweep most likely to meet a lock, since it deletes the parent of
        a foreign key."""
        attempts = []

        async def exploding_sweep(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("dag sweep is broken")

        monkeypatch.setattr(monitor_module, "sweep_orphaned_dags", exploding_sweep)

        expired = await insert_terminal_job(db_pool, unique_queue, days_ago=3650)
        gone = await insert_worker(db_pool, unique_queue)
        await retire_worker(db_pool, gone, days_ago=3650)

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_days=7,
            )
        )
        try:
            await wait_until(lambda: len(attempts) >= 2, timeout=10)
            # the sweep before it and the sweeps after it both still ran
            await wait_for_job_gone(db_pool, expired, timeout=10)
            await wait_until(
                lambda: db_pool.fetchval(
                    "SELECT NOT EXISTS (SELECT 1 FROM jorb_worker WHERE id = $1)", gone
                ),
                timeout=10,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_saturated_retention_does_not_starve_the_latency_sweeps(
        self, db_pool, unique_queue, db_params, monkeypatch
    ):
        """Retention that can NEVER catch up must still yield every cycle.

        The fake sweep always returns a full batch, so the drain loop only
        ever ends on its time budget. Timeout enforcement and dead-worker
        recovery decide how long a stuck job stays stuck; they must keep
        running behind an unbounded backlog, and the cycle must keep turning
        (retention is re-entered, not stuck in one drain forever)."""
        batches = []

        async def endless_sweep(pool, days, batch_size):
            batches.append(1)
            await asyncio.sleep(0.01)
            return batch_size

        monkeypatch.setattr(monitor_module, "sweep_expired_jobs", endless_sweep)

        timed_out = await insert_job(
            db_pool, unique_queue, state="running", timeout_at_offset_seconds=-5
        )
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=dead_worker
        )

        task = asyncio.create_task(
            monitor(
                dsn_from(db_params),
                check_interval=0.1,
                liveness_grace_seconds=60,
                retention_batch_size=5,
                retention_max_seconds=0.05,
            )
        )
        try:
            await wait_for_job_state(db_pool, timed_out, ("queued",), timeout=10)
            await wait_for_job_state(db_pool, orphaned, ("queued",), timeout=10)
            await wait_until(lambda: len(batches) >= 4, timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_failing_checkpoint_sweep_does_not_starve_the_others(
        self, db_pool, unique_queue, db_params, monkeypatch
    ):
        """The checkpoint sweep runs first in the cycle, so its failure is the
        one that could swallow every sweep behind it."""
        attempts = []

        async def exploding_sweep(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("checkpoint sweep is broken")

        monkeypatch.setattr(
            monitor_module, "sweep_completed_checkpoints", exploding_sweep
        )

        ancient = await insert_terminal_job(db_pool, unique_queue, days_ago=3650)
        dead_worker = await insert_worker(
            db_pool, unique_queue, last_seen_age_seconds=300
        )
        orphaned = await insert_job(
            db_pool, unique_queue, state="running", claimed_by=dead_worker
        )

        task = asyncio.create_task(
            monitor(dsn_from(db_params), check_interval=0.1, liveness_grace_seconds=60)
        )
        try:
            await wait_until(lambda: len(attempts) >= 2, timeout=10)
            # both the sweep before it and the sweep after it still ran
            await wait_for_job_state(db_pool, orphaned, ("queued",), timeout=10)
            await wait_for_job_gone(db_pool, ancient, timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
