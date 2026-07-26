"""Tests for pyjobby.monitor — the platform's single background reaper.

Covers every sweep the monitor daemon performs:

- timeout enforcement: running jobs past ``timeout_at`` are requeued with
  backoff (same row) or dead-lettered to state='crashed' per ``on_timeout``
  and ``max_retries``
- dead-worker reclaim: in-flight jobs of workers whose ``jorb_worker``
  heartbeat went stale are requeued and the workers retired
- unregistered-claim reclaim: 'claimed' jobs with no registry reference are
  requeued after a grace period
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

import pytest
from loguru import logger

from pyjobby import monitor as monitor_module
from pyjobby.monitor import (
    _drain,
    handle_timed_out_job,
    monitor,
    sweep_completed_checkpoints,
    sweep_consumed_mailbox,
    sweep_dead_workers,
    sweep_expired_jobs,
    sweep_timed_out_jobs,
    sweep_unregistered_claims,
)
from pyjobby.pj import STMTS
from tests.conftest import wait_for_job_state
from tests.utils.processes import dsn_from

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
) -> int:
    """Insert one jorb row in the state a test needs (JSONB never NULL)."""
    return await pool.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, admin_data, state,
                          error_count, run_epoch, claimed_by, worker_host,
                          worker_pid, timeout_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'test-host', 4242,
                CASE WHEN $9::float8 IS NULL THEN NULL
                     ELSE now() + make_interval(secs => $9) END)
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
        retired workers and the unregistered-claim sweep only covers claims
        with no worker reference at all."""
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
# unregistered-claim reclaim
# ============================================================================


class TestSweepUnregisteredClaims:
    async def test_requeues_stale_unregistered_claim(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 1

        job = await get_job(db_pool, job_id)
        assert job["state"] == "queued"
        assert job["timeout_at"] is None

    async def test_leaves_fresh_claims_alone(self, db_pool, unique_queue):
        job_id = await insert_job(db_pool, unique_queue, state="claimed")

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "claimed"

    async def test_leaves_registered_claims_to_the_dead_worker_sweep(
        self, db_pool, unique_queue
    ):
        """Claims with a registry reference are the dead-worker sweep's
        business, however old they are."""
        worker_id = await insert_worker(db_pool, unique_queue)
        job_id = await insert_job(
            db_pool, unique_queue, state="claimed", claimed_by=worker_id
        )
        await db_pool.execute(
            "UPDATE jorb SET updated = now() - interval '10 minutes' WHERE id = $1",
            job_id,
        )

        requeued = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)
        assert requeued == 0
        assert (await get_job(db_pool, job_id))["state"] == "claimed"


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
    """Checkpoints live on their own, much shorter window.

    A checkpoint exists to make a job resumable. A terminal job is never
    resumed, so from the moment it finishes its checkpoints are pure audit —
    the bulkiest thing on the row with the shortest useful life."""

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

    async def test_deletes_for_every_terminal_state(self, db_pool, unique_queue):
        jobs = [
            await insert_terminal_job(db_pool, unique_queue, state=state, days_ago=3)
            for state in ("finished", "crashed", "cancelled")
        ]
        for job in jobs:
            await add_checkpoints(db_pool, job, 2)

        deleted = await sweep_completed_checkpoints(
            db_pool, checkpoint_retention_days=1
        )
        assert deleted == 6

        assert await job_ids(db_pool) == sorted(jobs)
        assert await db_pool.fetchval("SELECT count(*) FROM jorb_step") == 0

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
