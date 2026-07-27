"""The pyjobby monitor: the platform's single background reaper.

Every safety-net sweep lives here — one daemon, one place to reason about
recovery:

1. **Timeout enforcement**: running jobs past ``timeout_at`` are retried or
   dead-lettered according to their ``on_timeout`` policy. Workers enforce
   timeouts in-process too; this catches workers that died mid-job or sync
   tasks that could not be interrupted.
2. **Dead-worker reclaim**: jobs claimed by workers whose registry heartbeat
   (``jorb_worker.last_seen``) went stale are requeued, on any host. Workers
   heartbeat on a dedicated connection, so a stale heartbeat means the
   process is gone (or partitioned — in which case run_epoch fencing keeps
   the zombie from writing results after the job is requeued).
3. **Unregistered-claim reclaim**: jobs stuck in 'claimed' with no registry
   reference past a grace period (a worker died between claim and register,
   or the registry was unavailable).
4. **Job retention**: terminal jobs older than ``--retention-days`` (default
   30) are deleted, taking their checkpoints, history, events and mailbox
   with them via ON DELETE CASCADE. On by default: a platform that runs
   indefinitely accumulates every completed job forever otherwise, and a
   retention policy nobody remembers to switch on is not a policy. Pass
   ``--retention-days 0`` to keep everything forever.
5. **Checkpoint retention**: ``jorb_step`` rows of terminal jobs older than
   ``--checkpoint-retention-days`` (default 1) are deleted while the job row
   itself stays. Checkpoints exist to make a job RESUMABLE; the moment it
   reaches a terminal state resume is impossible, so they are the bulkiest
   thing on the row with the shortest useful life. They outlive the job's
   terminal transition only far enough to debug it. ``0`` keeps them for as
   long as the job.
6. **The four tables the job cascade cannot reach**, all on the same
   ``--retention-days`` window, because none of them has a lifetime argument
   of its own — they are all "as long as the jobs they describe":

   * ``jorb_mailbox`` — ``recv`` only stamps ``consumed_at``, and the
     job-scoped cascade cannot reach a job that is still ALIVE, so a durable
     workflow running for months keeps every message it has ever read.
   * ``jorb_dag`` — jobs point AT a DAG (``ON DELETE SET NULL``), so
     deleting jobs never touches it. Left alone it is not just leaked
     storage: ``jorb_dag_status`` LEFT JOINs jorb, so a DAG whose jobs aged
     out reports ``total_jobs = 0`` forever, and ``pj-admin dag list``
     fills up with DAGs that appear to have run nothing.
   * ``jorb_schedule_log`` — cascades only from ``jorb_schedule``, which
     operators disable rather than delete, so it had no bound at all: one
     row per execution at cron rate, forever.
   * ``jorb_worker`` — one row per worker PROCESS START, never deleted, only
     stamped ``shutdown_at``. A fleet that redeploys daily accumulates rows
     indefinitely.

Every retention sweep DRAINS: a cycle keeps taking batches until it is caught
up or spends ``--retention-max-seconds``, then yields. One batch per cycle
would be a fixed deletion rate that a busy install simply outruns, leaving
retention switched on and the table still growing; the budget keeps a backlog
from delaying the latency-critical sweeps above.

Every retention sweep also REFUSES to delete a row something live still
needs, and the refusal is the interesting half of each one: a terminal job a
'waiting' job depends on, an unread message, a DAG that still has jobs, a
schedule's most recent execution, a worker row whose jobs are still in
flight.

Every sweep's SQL is a module constant, and ``pj-bench plans`` EXPLAINs those
exact constants as a CI gate — deriving its case list from the ``SWEEP_*_SQL``
names defined here, so a sweep added without a gate entry fails the gate
instead of quietly going unmeasured. All of them probe by index and then
delete by primary key, in two statements: the one-statement
``DELETE ... USING (CTE)`` form plans its second stage against the target's
whole-table statistics and hash-joins a SEQUENTIAL SCAN of it against a batch
whose keys it is already holding. Measured on four of these sweeps, at
20,000-row seeds, that is hundreds to thousands of buffers per batch to
delete rows it had already identified — and it grows with the table forever,
while a batch does not.

Every requeue and every dead-letter here bumps ``run_epoch`` itself (see
db.build_requeue_sql for the argument): the abandoned execution may still be
running, and epoch-only-guarded writes — checkpoints, events, mailbox sends —
must stop applying the moment the row leaves its attempt, not at the next
claim.

Run it: ``pj-monitor --config ./pyjobby.conf.py`` (one instance is enough;
several are safe — every sweep is a single atomic statement or a
transaction holding its row locks).
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import db
from .lifecycle import TERMINAL_STATES

# =========================================================================
# The sweep statements
# =========================================================================
# Every sweep's SQL is a module constant, in the style of pj.py's STMTS,
# for one reason: `pj-bench plans` is the CI gate that asserts these queries
# still use their indexes, and to measure a statement it has to have the
# statement. When they lived inline in the function bodies the benchmark
# kept its own COPY of each one -- and a copy drifts, silently, until the
# gate is certifying a query nobody executes. That is the exact failure the
# gate exists to prevent, so the monitor and the benchmark now read the same
# string.
#
# The `_TERMINAL_STATES_SQL` interpolation below is a module constant, never
# user input; see its own comment for why the states are inlined rather than
# bound.

#: The same list inlined into SQL, because ``jorb_retention_idx`` is PARTIAL
#: on exactly this predicate. A bound ``state = ANY($1)`` reads fine under the
#: custom plan of the first few executions and then silently falls off the
#: index: asyncpg prepares its statements, and once PostgreSQL switches to a
#: GENERIC plan it can no longer prove an unknown parameter implies the index
#: predicate. Measured on 20k terminal rows with nothing expired -- generic
#: plan, bound states: Seq Scan + Sort, 617 buffers; literal states: Index
#: Scan, 2 buffers. Interpolating a module constant is not user input.
_TERMINAL_STATES_SQL = ", ".join(f"'{state}'" for state in TERMINAL_STATES)

#: Running jobs past their deadline. ($1 batch size)
SWEEP_TIMED_OUT_SQL = """
    SELECT id, job_class, timeout_at, admin_data, error_count
    FROM jorb
    WHERE state = 'running'
      AND timeout_at IS NOT NULL
      AND timeout_at < now()
    FOR UPDATE SKIP LOCKED
    LIMIT $1
"""

#: In-flight jobs of workers whose heartbeat went stale. ($1 grace, $2 batch)
SWEEP_DEAD_WORKER_JOBS_SQL = """
    WITH doomed AS MATERIALIZED (
        SELECT j.id FROM jorb j
        JOIN jorb_worker w ON w.id = j.claimed_by
        WHERE j.state IN ('claimed', 'running')
          AND w.last_seen < now() - $1::interval
        FOR UPDATE OF j SKIP LOCKED
        LIMIT $2
    )
    UPDATE jorb
    SET state = 'queued',
        run_epoch = run_epoch + 1,
        run_after = now(),
        timeout_at = NULL,
        updated = now()
    FROM doomed
    WHERE jorb.id = doomed.id
      AND jorb.state IN ('claimed', 'running')
    RETURNING jorb.id, jorb.job_class, jorb.worker_host, jorb.claimed_by
"""

#: Retire the workers themselves. ($1 grace)
#:
#: `idle = FALSE` is not cosmetic: an idle worker is a live subscription to
#: its queue's jorb_enqueued notifications (sql/schema.sql), so a worker that
#: died while parked would keep every enqueue on that queue paying the NOTIFY
#: commit lock forever. This sweep is what bounds that to the liveness grace.
#: Costing an occasional needless notification is fine; leaking one
#: permanently is not.
RETIRE_DEAD_WORKERS_SQL = """
    UPDATE jorb_worker
    SET shutdown_at = now(),
        idle = FALSE
    WHERE shutdown_at IS NULL
      AND last_seen < now() - $1::interval
"""

#: Jobs stuck in 'claimed' with no registry reference. ($1 grace, $2 batch)
SWEEP_UNREGISTERED_CLAIMS_SQL = """
    WITH doomed AS MATERIALIZED (
        SELECT id FROM jorb
        WHERE state = 'claimed'
          AND claimed_by IS NULL
          AND updated < now() - $1::interval
        FOR UPDATE SKIP LOCKED
        LIMIT $2
    )
    UPDATE jorb
    SET state = 'queued',
        run_epoch = run_epoch + 1,
        run_after = now(),
        timeout_at = NULL,
        updated = now()
    FROM doomed
    WHERE jorb.id = doomed.id
      AND jorb.state = 'claimed'
    RETURNING jorb.id, jorb.job_class, jorb.worker_host
"""

#: Terminal jobs whose retention window elapsed. ($1 retention, $2 batch)
SWEEP_EXPIRED_JOBS_SQL = f"""
    SELECT j.id
    FROM jorb j
    WHERE j.state IN ({_TERMINAL_STATES_SQL})
      AND COALESCE(j.finished, j.updated) < now() - $1::interval
      AND NOT EXISTS (
          SELECT 1 FROM jorb w
          WHERE w.state = 'waiting' AND w.waitfor_job = j.id
      )
      AND NOT EXISTS (
          SELECT 1 FROM jorb w
          WHERE w.state = 'waiting' AND w.waitfor_group = j.run_group
      )
    ORDER BY COALESCE(j.finished, j.updated)
    FOR UPDATE OF j SKIP LOCKED
    LIMIT $2
"""  # noqa: S608 - interpolates a module constant, never input

DELETE_EXPIRED_JOBS_SQL = "DELETE FROM jorb WHERE id = ANY($1::bigint[])"

#: Jobs whose checkpoints have aged out. ($1 retention, $2 batch)
#:
#: Deliberately the same TWO-STATEMENT shape as sweep_expired_jobs: probe for
#: victims by index, then delete them by key. The obvious single statement --
#: jorb_step JOIN jorb, filtered on the job's retention expression -- planned
#: as a merge join that walks jorb_pkey and discards every row it reads:
#: measured at a 20,000-row seed, 20,000 rows removed by filter and 534-1,194
#: buffers touched to delete nothing, growing with the table forever. That is
#: NOT a sequential scan, so a seq-scan check passes it happily; the number
#: that catches it is rows-removed-by-filter.
#:
#: This probe walks jorb_retention_idx oldest-first under a range predicate,
#: so when nothing has expired it reads the two buffers it takes to say so.
#:
#: The "has any checkpoint" test is what keeps the sweep making PROGRESS:
#: most terminal jobs have no checkpoints at all (only DXE jobs write them),
#: and a batch taken in retention order without it would usually be
#: all-stepless, delete nothing, and convince the drain loop it was caught up
#: while the real backlog sat behind it.
#:
#: That test is a SCALAR SUBQUERY and not the EXISTS it reads like, for a
#: measured reason: PostgreSQL flattens an EXISTS sublink into a semi-join,
#: the planner then costs a hash semi-join against the whole of jorb_step,
#: and paying for that hash makes a sequential scan of jorb look free --
#: straight back to 20,001 rows discarded. A scalar subquery is not
#: flattenable, so it stays a per-row primary-key probe on top of the
#: index-driven scan, which is the plan this sweep needs.
SWEEP_CHECKPOINT_JOBS_SQL = f"""
    SELECT j.id
    FROM jorb j
    WHERE j.state IN ({_TERMINAL_STATES_SQL})
      AND COALESCE(j.finished, j.updated) < now() - $1::interval
      AND (SELECT s.job_id FROM jorb_step s
            WHERE s.job_id = j.id LIMIT 1) IS NOT NULL
    ORDER BY COALESCE(j.finished, j.updated)
    LIMIT $2
"""  # noqa: S608 - interpolates a module constant, never input

#: ...and the delete, by the primary key's leading column. Bounded by the
#: probe's batch, and never executed at all when the probe came back empty --
#: which is the steady state, and the reason this is two statements and not a
#: CTE: a CTE's second stage is PLANNED against jorb_step's whole-table
#: statistics and hash-joins it even when the first stage returns nothing
#: (measured: 1,006 buffers to delete zero rows, growing with jorb_step).
DELETE_CHECKPOINTS_SQL = """
    DELETE FROM jorb_step
    WHERE job_id = ANY($1::bigint[])
    RETURNING job_id, step_seq
"""

#: DAGs whose jobs are all gone and which outlived the window. ($1, $2)
#:
#: This one is not primarily about storage — a jorb_dag row is a name, two
#: timestamps and a small JSONB — it is about a WRONG ANSWER. jorb.dag_id is
#: the CHILD side of the foreign key (ON DELETE SET NULL), so deleting jobs
#: never touches this table; and jorb_dag_status is a LEFT JOIN, so the
#: instant retention removes a DAG's last job the view starts reporting that
#: DAG as ``total_jobs = 0, pending_jobs = 0`` — a DAG that ran nothing —
#: and goes on reporting it forever. An operator running `pj-admin dag list`
#: on a year-old install would see a fleet of empty ghosts, each one a lie
#: about work that in fact completed.
#:
#: The DAG is therefore reaped, not hidden. A view cannot distinguish "never
#: had jobs" from "had jobs, they aged out", so making jorb_dag_status an
#: inner join would trade this wrong answer for a different one: it would
#: hide a DAG during construction, which is a legitimate empty state.
#:
#: Two conditions, and the second is the one that keeps it safe:
#:
#: * ``created < now() - retention`` — the DAG itself outlived the window.
#:   It is ordered by ``created`` too, which ``jorb_dag_retention_idx``
#:   serves directly, so a caught-up sweep is a two-buffer range probe
#:   rather than a scan of every DAG ever run.
#: * no job points at it — ANY job, at any age, in any state. The DAG row is
#:   the only thing that gives its surviving jobs a group, and dropping it
#:   would silently NULL their dag_id via the foreign key; a DAG with even
#:   one job left is a DAG somebody can still ask about.
#:
#: The "no jobs" test is a scalar subquery and not the EXISTS it reads like,
#: for the same measured reason as SWEEP_CHECKPOINT_JOBS_SQL: PostgreSQL
#: flattens an EXISTS sublink into a semi-join and can then cost a hash
#: against the whole of jorb, which makes scanning jorb_dag look free. A
#: scalar subquery stays a per-row index probe through jorb_dag_idx.
#:
#: Probe then delete by key, the shape sweep_expired_jobs and
#: sweep_completed_checkpoints use, and for the third measured time it is not
#: cosmetic: the one-statement ``DELETE ... USING (CTE)`` form plans its
#: second stage against jorb_dag's whole-table statistics and hash-joins a
#: SEQUENTIAL SCAN of jorb_dag against the batch (measured at 20,000 DAGs:
#: 3,300 buffers on top of the probe, to delete 1,000 rows by primary key).
#: A batch is bounded; a scan per batch grows with every DAG the install has
#: ever run.
SWEEP_ORPHANED_DAGS_SQL = """
    SELECT d.id
    FROM jorb_dag d
    WHERE d.created < now() - $1::interval
      AND (SELECT j.id FROM jorb j
            WHERE j.dag_id = d.id LIMIT 1) IS NULL
    ORDER BY d.created
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""

DELETE_ORPHANED_DAGS_SQL = "DELETE FROM jorb_dag WHERE id = ANY($1::bigint[])"

#: Schedule executions past the window, except each schedule's newest. ($1, $2)
#:
#: jorb_schedule_log cascades from jorb_schedule and from nothing else, and
#: operators do not delete schedules — they disable them. So one row per
#: execution accumulated with no upper bound whatsoever: a minutely schedule
#: writes ~43,000 rows a month and keeps every one of them for the life of
#: the install. It is also the only unbounded table sitting on a notification
#: path (``schedule_executed_notify`` is deliberately ungated), which makes
#: its size everybody's problem and not just the DBA's.
#:
#: THE REFUSAL: a schedule's most recent execution is never deleted, however
#: old it is. `pj-admin schedule history NAME` and the dashboard read this
#: table to answer "when did this last run, and did it work?", and a schedule
#: that fires quarterly or yearly would otherwise have its entire history
#: erased and read as "never ran" — while jorb_schedule.last_run says
#: otherwise. That is the same class of wrong answer as the empty DAG above,
#: so it gets the same treatment: keep the row the live object still needs.
#: ``id < (SELECT max(id) ...)`` is served by jorb_schedule_log_idx
#: (schedule_id, id) as a backwards index-only probe, one per candidate row.
SWEEP_SCHEDULE_LOG_SQL = """
    SELECT l.id
    FROM jorb_schedule_log l
    WHERE l.actual_time < now() - $1::interval
      AND l.id < (SELECT max(l2.id) FROM jorb_schedule_log l2
                   WHERE l2.schedule_id = l.schedule_id)
    ORDER BY l.actual_time
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""

#: ...and the delete by primary key, for the same reason as the DAG sweep
#: above: the CTE form's second stage sequentially scans the log to join a
#: batch it was handed.
DELETE_SCHEDULE_LOG_SQL = "DELETE FROM jorb_schedule_log WHERE id = ANY($1::bigint[])"

#: Retired workers whose registry row aged out. ($1 retention, $2 batch)
#:
#: jorb_worker holds one row per worker PROCESS START, and nothing has ever
#: deleted one: graceful exit and the monitor's own dead-worker retirement
#: both only stamp ``shutdown_at``. A fleet of 100 workers redeployed daily
#: leaves 36,500 rows a year behind, all of them invisible to every operator
#: surface (``pj-admin workers list`` shows dead workers for an hour) and all
#: of them read by nothing.
#:
#: ``shutdown_at IS NOT NULL`` is the safety gate and ``last_seen`` is the
#: second one. A live worker never has shutdown_at set, so it can never be a
#: candidate — and a worker the monitor retired during a network blip that
#: then came BACK keeps beating last_seen, so requiring both to be stale
#: means resurrection cannot be mistaken for death. Deleting a live worker's
#: row would be silent and total: its heartbeat UPDATE would match no row,
#: and every liveness surface in the platform would say the process is gone
#: while it goes on claiming jobs.
SWEEP_RETIRED_WORKERS_SQL = """
    SELECT w.id
    FROM jorb_worker w
    WHERE w.shutdown_at IS NOT NULL
      AND w.shutdown_at < now() - $1::interval
      AND w.last_seen   < now() - $1::interval
    ORDER BY w.shutdown_at
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""

#: ...and the delete, which is where the refusal lives. ($1 ids)
#:
#: A worker row whose jobs are still 'claimed' or 'running' is NOT deletable
#: at any age, because ``claimed_by`` carries no foreign key: deleting the
#: registry row of a worker that still owns in-flight work would strand those
#: jobs permanently. SWEEP_DEAD_WORKER_JOBS_SQL finds them by JOINing jorb to
#: jorb_worker, so with the worker row gone there is nothing left to join to,
#: and SWEEP_UNREGISTERED_CLAIMS_SQL cannot pick them up either — it looks
#: for ``claimed_by IS NULL``, and these rows point at an id that no longer
#: exists. The job would sit in 'running' until somebody noticed by hand.
#:
#: The check runs on the DELETE rather than in the probe on purpose: it is
#: driven by ``jorb_inflight_idx``, whose partial predicate is exactly these
#: two states written as literals, so it costs the in-flight set (bounded by
#: work in flight, never by table size) and not a scan of jorb. Putting it in
#: the probe would make the planner cost that join against jorb_worker's
#: whole-table statistics on every cycle, including the overwhelmingly common
#: one where the probe returns nothing at all.
DELETE_RETIRED_WORKERS_SQL = """
    DELETE FROM jorb_worker w
    WHERE w.id = ANY($1::bigint[])
      AND NOT EXISTS (
          SELECT 1 FROM jorb j
          WHERE j.claimed_by = w.id
            AND j.state IN ('claimed', 'running')
      )
    RETURNING w.id
"""

#: Consumed mailbox messages past the window. ($1 retention, $2 batch)
#:
#: ``consumed_at IS NOT NULL`` is written out as well as implied by the range
#: comparison because ``jorb_mailbox_consumed_idx`` is PARTIAL on exactly that
#: predicate: without it the planner cannot prove the index covers every row
#: the query wants and will not use it at all.
#:
#: Ordered by ``consumed_at``, which that index provides directly, so the
#: probe walks it in order and stops at the batch — no sort however large the
#: backlog, and a two-buffer answer once caught up. Ordering by id would sort
#: every matching row first, and that cost grows with the backlog.
#:
#: Probe then delete by key, the shape every other retention sweep here uses,
#: and measured to matter for the fourth time. The one-statement
#: ``DELETE ... USING (CTE)`` form this replaced planned its second stage
#: against jorb_mailbox's whole-table statistics and hash-joined a SEQUENTIAL
#: SCAN of the mailbox against a batch whose keys it was already holding:
#: measured at 20,000 messages with a full backlog, 4,750 buffers total, 331
#: of them the scan, to delete 1,000 rows by primary key. The steady state
#: (nothing expired) planned fine, which is what let it survive review — the
#: cost only appears once the sweep has work, i.e. exactly when it matters.
SWEEP_MAILBOX_SQL = """
    SELECT m.id
    FROM jorb_mailbox m
    WHERE m.consumed_at IS NOT NULL
      AND m.consumed_at < now() - $1::interval
    ORDER BY m.consumed_at
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""

#: ...and the delete, by the primary key's leading column.
DELETE_MAILBOX_SQL = "DELETE FROM jorb_mailbox WHERE id = ANY($1::bigint[])"

#: Requeue one timed-out job for another attempt. Bumps run_epoch: the
#: execution that blew the deadline may still be running, and its
#: epoch-only-guarded writes must stop applying now, not at the next claim.
#: ($1 job_id, $2 error message, $3 retry delay interval)
RETRY_TIMED_OUT_SQL = """
    UPDATE jorb
    SET state = 'queued',
        run_epoch = run_epoch + 1,
        timeout_at = NULL,
        error_count = error_count + 1,
        error_message = $2,
        run_after = now() + $3::interval,
        updated = now()
    WHERE id = $1
      AND state = 'running'
"""

#: Dead-letter one timed-out job (retries exhausted or on_timeout='fail').
#: Bumps run_epoch for the same reason RETRY_TIMED_OUT_SQL does — this is
#: the abandonment with the LONGEST-lived zombie, since nothing will ever
#: reclaim the row and re-fence it. ($1 job_id, $2 error message)
DEADLETTER_TIMED_OUT_SQL = """
    UPDATE jorb
    SET state = 'crashed',
        run_epoch = run_epoch + 1,
        timeout_at = NULL,
        error_count = error_count + 1,
        error_message = $2,
        finished = now(),
        updated = now()
    WHERE id = $1
      AND state = 'running'
"""


async def handle_timed_out_job(
    conn: asyncpg.Pool | asyncpg.Connection,
    job_id: int,
    job_class: str,
    admin_data: dict | None,
    error_count: int,
) -> None:
    """Retry or dead-letter one job that exceeded its timeout."""
    admin = admin_data or {}
    on_timeout = admin.get("on_timeout", "retry")
    max_retries = admin.get("max_retries", 10)
    attempt = error_count + 1

    logger.warning(
        f"Job {job_id} ({job_class}) exceeded timeout, "
        f"action: {on_timeout}, attempt {attempt}/{max_retries}"
    )

    if on_timeout == "retry" and attempt < max_retries:
        from .retry_strategies import calculate_retry_from_job

        job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        if not job:
            return

        retry_delay = calculate_retry_from_job(dict(job), attempt)

        await conn.execute(
            RETRY_TIMED_OUT_SQL,
            job_id,
            "Timeout exceeded - retrying",
            retry_delay,
        )

        logger.info(
            f"Job {job_id} requeued for retry in {retry_delay.total_seconds()}s"
        )
    else:
        reason = "max retries exceeded" if attempt >= max_retries else "on_timeout=fail"

        await conn.execute(
            DEADLETTER_TIMED_OUT_SQL,
            job_id,
            f"Timeout exceeded - dead-lettered ({reason})",
        )

        logger.error(f"Job {job_id} dead-lettered: {reason}")


async def sweep_timed_out_jobs(pool: asyncpg.Pool, batch_size: int = 100) -> int:
    """Find running jobs past their deadline and retry/dead-letter them.

    The SELECT ... FOR UPDATE SKIP LOCKED and the per-job updates share one
    transaction so the row locks are held while handling the jobs: multiple
    monitor instances cannot double-process, and a worker finishing a job
    concurrently is serialized against us (its epoch-fenced update then
    no-ops)."""
    async with pool.acquire() as conn, conn.transaction():
        timed_out = await conn.fetch(SWEEP_TIMED_OUT_SQL, batch_size)

        for job in timed_out:
            await handle_timed_out_job(
                conn,
                job["id"],
                job["job_class"],
                job["admin_data"],
                job["error_count"],
            )

    return len(timed_out)


async def sweep_dead_workers(
    pool: asyncpg.Pool,
    liveness_grace_seconds: float = 60,
    batch_size: int = 500,
) -> int:
    """Requeue in-flight jobs owned by workers whose heartbeat went stale,
    and retire those workers from the registry.

    Liveness is the heartbeat and nothing else: a job still 'claimed' or
    'running' behind a stale ``last_seen`` is orphaned whether or not the
    worker managed to stamp ``shutdown_at`` on the way out. Requiring the
    worker to still look live would strand exactly the jobs that need
    recovery — those of a worker that deregistered (or was retired by an
    earlier sweep) while a job was still in flight.

    Single atomic statements; safe with concurrent monitor instances."""
    grace = datetime.timedelta(seconds=liveness_grace_seconds)

    requeued = await pool.fetch(SWEEP_DEAD_WORKER_JOBS_SQL, grace, batch_size)

    for row in requeued:
        logger.warning(
            f"Requeued job {row['id']} ({row['job_class']}) from dead worker "
            f"{row['claimed_by']} on {row['worker_host']}"
        )

    # retire workers that stopped heartbeating so they aren't rescanned (and
    # so the operator surface shows them as gone, not alive); see
    # RETIRE_DEAD_WORKERS_SQL for why it also clears idle
    retired = await pool.execute(RETIRE_DEAD_WORKERS_SQL, grace)
    if retired != "UPDATE 0":
        logger.warning(f"Retired stale workers from registry: {retired}")

    return len(requeued)


async def sweep_unregistered_claims(
    pool: asyncpg.Pool,
    claimed_grace_seconds: float = 300,
    batch_size: int = 100,
) -> int:
    """Requeue jobs stuck in 'claimed' with no registry reference.

    Covers the rare gap where a worker claimed a job while the registry was
    unavailable and then died: nothing heartbeats for it, so age is the only
    signal. A healthy worker moves claimed -> running almost immediately."""
    grace = datetime.timedelta(seconds=claimed_grace_seconds)
    requeued = await pool.fetch(SWEEP_UNREGISTERED_CLAIMS_SQL, grace, batch_size)

    for row in requeued:
        logger.warning(
            f"Requeued unregistered stale claim {row['id']} ({row['job_class']}) "
            f"from {row['worker_host']}"
        )

    return len(requeued)


async def sweep_expired_jobs(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete terminal jobs whose retention window has elapsed.

    Eligibility is state first, age second: only ``TERMINAL_STATES`` rows are
    considered, and their age is ``finished`` — the terminal timestamp every
    completion, crash and cancellation path stamps. ``updated`` stands in when
    ``finished`` is somehow NULL so a terminal row can never become immortal
    and leak.

    Jobs an unfinished job is still parked on are kept regardless of age:
    ``waitfor_job``/``waitfor_group`` carry no foreign key, so deleting the
    upstream would strand the waiter in 'waiting' forever — nothing but the
    upstream's own terminal transition ever wakes it.

    Every child table — jorb_step, jorb_event, jorb_mailbox, jorb_history —
    follows via ON DELETE CASCADE, so deleting the job row is the whole job.

    Bounded and batched: one bite of ``batch_size`` per call, holding only
    those rows' locks. FOR UPDATE SKIP LOCKED makes concurrent monitors
    partition the backlog instead of colliding on it. ``_drain`` calls this
    repeatedly so a cycle can catch up rather than nibble.

    Ordered by the retention expression, never by id. Oldest-first is the
    semantically right order to reap in, and it is also the only order that
    ``jorb_retention_idx`` can serve: ORDER BY id makes the planner prefer a
    pkey scan to avoid a sort and then filter the entire terminal backlog
    (measured on 20k rows with nothing expired: 366 buffers and 20k rows
    discarded, against 2 buffers and no sort). That gap grows with the table,
    so the sweep that exists to stop unbounded growth would itself get slower
    forever. Returns the number of jobs deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        expired = await conn.fetch(SWEEP_EXPIRED_JOBS_SQL, retention, batch_size)
        if not expired:
            return 0

        job_ids = [row["id"] for row in expired]
        await conn.execute(DELETE_EXPIRED_JOBS_SQL, job_ids)

    return len(job_ids)


async def sweep_completed_checkpoints(
    pool: asyncpg.Pool,
    checkpoint_retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete the DXE checkpoints of jobs that terminated long enough ago.

    Checkpoints exist for one reason: so a resumed attempt fast-forwards past
    steps it already ran. A terminal job is never resumed, so from the instant
    it finishes its ``jorb_step`` rows are pure audit — readable through
    ``pj-admin jobs steps <id>`` and nothing else. They are also the bulkiest
    thing hanging off a job (one row per step, each with the step's output),
    which is why they get their own, much shorter window instead of living as
    long as the job row.

    Only the checkpoints go. The job row and its history, events and mailbox
    are ``sweep_expired_jobs``' business on the longer window — the two are
    deliberately independent lifetimes.

    A checkpoint of a NON-terminal job is never touched at any age. A durable
    sleep parks a job in 'queued' for months holding the very checkpoint that
    records when to wake; deleting it would silently re-run completed steps.
    So state is the gate and age only decides among terminal jobs.

    Probe, then delete by key — the same two-statement shape as
    ``sweep_expired_jobs``, and for the same reason: it is the only shape
    whose plan is actually DRIVEN by ``jorb_retention_idx``, oldest
    terminated first. The single-statement join form was not. It planned as
    a merge join walking ``jorb_pkey`` and discarding every row it read —
    measured at a 20,000-row seed as 20,000 rows removed by filter and
    534–1,194 buffers to delete nothing, growing with the table forever,
    which is exactly the pathology the retention index exists to prevent. An
    index scan that throws away everything it reads is not a sequential scan
    and costs the same, so the number to watch is rows-removed-by-filter,
    not the access method. ``tests/test_scale_plans.py`` asserts both, on
    these very constants rather than on a copy of them.

    ``batch_size`` bounds the JOBS taken per call, not their rows: the probe
    rides an index on the JOB's terminal time, so a job is the unit it can
    stop on, and all of a doomed job's checkpoints go together. Splitting a
    batch mid-job would need a second index-driven lookup into jorb_step per
    batch, which is the cost this was rewritten to stop paying. ``_drain``
    still terminates on it — a short batch means fewer than ``batch_size``
    jobs were available, and every job in a batch contributes at least one
    row.

    No job row is locked: a worker writing to a live job must never queue
    behind retention, and a terminal job has no writer to protect from
    anyway. Concurrent monitors converge on the same batch and the second
    one finds the rows already gone. Returns the number of rows deleted."""
    retention = datetime.timedelta(days=checkpoint_retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_CHECKPOINT_JOBS_SQL, retention, batch_size)
        if not doomed:
            return 0

        deleted = await conn.fetch(
            DELETE_CHECKPOINTS_SQL, [row["id"] for row in doomed]
        )

    return len(deleted)


async def sweep_consumed_mailbox(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete consumed mailbox messages older than the retention window.

    The job-scoped cascade cannot reach these: ``recv`` only stamps
    ``consumed_at``, so a long-lived job — a durable workflow that runs for
    months, or a never-terminating one — keeps every message it ever read.
    A consumed message is unreadable (``recv`` filters ``consumed_at IS
    NULL``) and referenced by nothing, so age is the only thing to decide on.

    Probe then delete by key, the same two-statement shape as every other
    retention sweep here, and for the same measured reason. The single
    statement it replaced — ``DELETE ... USING (materialized CTE)`` — picked
    its batch by index perfectly well and then hash-joined a SEQUENTIAL SCAN
    of the whole mailbox against keys it was already holding: 4,750 buffers at
    a 20,000-message backlog to delete 1,000 rows, 331 of them pure scan, and
    both numbers grow with the table forever. It planned correctly in the
    steady state, so a caught-up system never showed the cost; it appeared
    only once the sweep had work to do.

    Ordered by ``consumed_at``, which ``jorb_mailbox_consumed_idx`` provides
    directly: the probe walks the index in order and stops at the batch size,
    with no sort even when a large backlog matches. Ordering by id instead
    would have to sort every matching row first, which is the cost that grows.

    The two statements share one transaction, so the rows the probe locked
    with FOR UPDATE SKIP LOCKED are still held when the delete runs and
    concurrent monitors partition the backlog rather than colliding on it.
    Returns the number of messages deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_MAILBOX_SQL, retention, batch_size)
        if not doomed:
            return 0

        await conn.execute(DELETE_MAILBOX_SQL, [row["id"] for row in doomed])

    return len(doomed)


async def sweep_orphaned_dags(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete DAG rows that outlived the window and have no jobs left.

    This is a CORRECTNESS sweep wearing retention's clothes. ``jorb.dag_id``
    is the child side of the foreign key, so job retention never touches
    ``jorb_dag``; and ``jorb_dag_status`` LEFT JOINs jorb, so a DAG whose
    jobs have aged out reports ``total_jobs = 0`` — permanently, and to
    anyone who runs ``pj-admin dag list``. The row is tiny. The answer it
    produces is wrong, and wrong answers do not get cheaper at scale.

    A DAG with even ONE job left is kept regardless of age: the row is what
    gives those jobs a group, and the foreign key would silently NULL their
    ``dag_id`` on the way out. Age is only the tiebreaker among DAGs that
    already have nothing.

    Because a DAG is created before its own jobs (inside the same
    transaction), ``created`` is always earlier than any of their terminal
    timestamps — so a DAG becomes eligible on the very cycle that removes
    its last job, and the empty-DAG window is one monitor cycle wide rather
    than forever. Ordered by ``created``, which ``jorb_dag_retention_idx``
    serves directly. Returns the number of DAGs deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_ORPHANED_DAGS_SQL, retention, batch_size)
        if not doomed:
            return 0

        await conn.execute(DELETE_ORPHANED_DAGS_SQL, [row["id"] for row in doomed])

    return len(doomed)


async def sweep_schedule_log(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete schedule executions older than the window, keeping the newest.

    ``jorb_schedule_log`` cascades only from ``jorb_schedule``, and nobody
    deletes a schedule — they disable it. So this table had no upper bound of
    any kind: one row per execution, at cron rate, kept for the life of the
    install.

    Each schedule's most recent execution survives at any age. It is what
    ``pj-admin schedule history`` and the dashboard read to answer "when did
    this last run", so reaping it would make a quarterly schedule read as
    "never ran" while ``jorb_schedule.last_run`` says otherwise — a wrong
    answer, not merely a shorter history.

    Ordered by ``actual_time``, which ``jorb_schedule_log_retention_idx``
    provides directly: no sort however large the backlog, and a two-buffer
    answer once caught up. Returns the number of rows deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_SCHEDULE_LOG_SQL, retention, batch_size)
        if not doomed:
            return 0

        await conn.execute(DELETE_SCHEDULE_LOG_SQL, [row["id"] for row in doomed])

    return len(doomed)


async def sweep_retired_workers(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete registry rows of workers that shut down long enough ago.

    One row per worker PROCESS START and nothing ever deleted one, so a fleet
    that redeploys accumulates registry rows forever. Only rows that are both
    retired (``shutdown_at`` set) and silent (``last_seen`` stale) for the
    whole window are candidates, which is what makes a live worker — or one
    that was retired during a blip and came back — structurally ineligible.

    A worker that still owns 'claimed' or 'running' jobs is refused whatever
    its age. ``jorb.claimed_by`` has no foreign key, so deleting the row
    would strand that work: the dead-worker sweep finds orphaned jobs by
    joining to this table, and the unregistered-claim sweep only looks at
    ``claimed_by IS NULL``. Neither can see a job pointing at an id that no
    longer exists.

    Probe then delete, the same two-statement shape as the other sweeps: the
    probe rides ``jorb_worker_retention_idx`` oldest-shutdown-first, and the
    in-flight refusal rides ``jorb_inflight_idx`` on the delete, where it is
    only paid when there were candidates at all. A refused worker makes the
    batch come back short, which ``_drain`` reads as "caught up" — correct
    here, because the refusal is transient: the dead-worker sweep requeues
    those jobs within the liveness grace and the next cycle takes the row.
    Returns the number of registry rows deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_RETIRED_WORKERS_SQL, retention, batch_size)
        if not doomed:
            return 0

        deleted = await conn.fetch(
            DELETE_RETIRED_WORKERS_SQL, [row["id"] for row in doomed]
        )

    return len(deleted)


async def _run_sweep(name: str, sweep: Callable[[], Awaitable[int]]) -> int:
    """Run one sweep, containing its failure to itself.

    The sweeps are independent safety nets: a database error while handling a
    timed-out job must not also cancel dead-worker recovery for that cycle
    (nor kill the daemon), so each one reports and returns 0 on failure."""
    try:
        return await sweep()
    except Exception:
        logger.exception(f"Monitor sweep {name} failed")
        return 0


async def _drain(
    name: str,
    sweep: Callable[[], Awaitable[int]],
    batch_size: int,
    max_seconds: float,
) -> int:
    """Run one batched retention sweep until it is caught up or out of time.

    One batch per cycle is a rate limit, not a policy: ``batch_size`` per
    ``check_interval`` is a hard ceiling on deletions per second, and any
    platform ingesting faster than that outruns retention permanently — the
    dashboard says retention is enabled while the table grows forever, which
    is the exact defect this feature exists to prevent. So a cycle keeps
    taking batches until one comes back short (nothing left to delete).

    ``max_seconds`` is the other half. Retention is the only sweep here that
    is not latency-critical: timeout enforcement and dead-worker recovery
    decide how long a stuck job stays stuck, and they must not queue behind an
    unbounded backlog. So a cycle yields when its budget is spent and picks up
    where it left off on the next one.

    Stopping early is logged at WARNING with the count, because a retention
    sweep that cannot keep up has to be visible: silence would read exactly
    like being caught up. Returns the number of rows deleted this cycle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    total = 0

    while True:
        deleted = await sweep()
        total += deleted

        if deleted < batch_size:
            if total:
                logger.info(f"Retention {name}: deleted {total}, caught up")
            return total

        if loop.time() >= deadline:
            logger.warning(
                f"Retention {name}: deleted {total} and stopped on its "
                f"{max_seconds}s budget with a backlog still pending — "
                f"retention is falling behind"
            )
            return total


async def _run_retention(
    name: str,
    sweep: Callable[[], Awaitable[int]],
    batch_size: int,
    max_seconds: float,
) -> int:
    """Drain one retention sweep with the same failure isolation as the rest."""
    return await _run_sweep(name, lambda: _drain(name, sweep, batch_size, max_seconds))


async def monitor(
    dsn: str,
    check_interval: float = 10,
    batch_size: int = 100,
    liveness_grace_seconds: float = 60,
    claimed_grace_seconds: float = 300,
    retention_days: float = 30.0,
    checkpoint_retention_days: float = 1.0,
    retention_batch_size: int = 1000,
    retention_max_seconds: float = 5.0,
) -> None:
    """Run all sweeps every ``check_interval`` seconds, forever.

    Retention is on by default and the two windows are independent:
    ``retention_days`` deletes whole terminal jobs — and the emptied DAGs,
    aged schedule executions, consumed mail and retired worker rows that no
    cascade can reach — while ``checkpoint_retention_days`` deletes just the
    checkpoints of terminal jobs much sooner. Either set to ``0`` means keep
    forever: those sweeps do not run at all.

    One window covers all five of those tables rather than five knobs because
    none of them has its own lifetime to argue for: they all mean "as long as
    the work they describe". Checkpoints get the second knob because they
    genuinely do — they are the bulkiest rows in the system and stop being
    useful the instant their job goes terminal.

    Each retention sweep drains its backlog within a ``retention_max_seconds``
    budget per cycle, so it can catch up on a busy install without ever
    delaying the latency-critical sweeps above it."""
    pool = await db.create_pool(dsn, min_size=1, max_size=2)

    def window(days: float) -> str:
        return f"{days}d" if days else "forever"

    logger.info(
        f"Monitor started (interval {check_interval}s, "
        f"liveness grace {liveness_grace_seconds}s, "
        f"job retention {window(retention_days)}, "
        f"checkpoint retention {window(checkpoint_retention_days)})"
    )

    try:
        while True:
            timed_out = await _run_sweep(
                "timed-out jobs", lambda: sweep_timed_out_jobs(pool, batch_size)
            )
            if timed_out:
                logger.info(f"Handled {timed_out} timed-out jobs")

            await _run_sweep(
                "dead workers",
                lambda: sweep_dead_workers(pool, liveness_grace_seconds),
            )
            await _run_sweep(
                "unregistered claims",
                lambda: sweep_unregistered_claims(pool, claimed_grace_seconds),
            )

            if checkpoint_retention_days:
                await _run_retention(
                    "completed checkpoints",
                    lambda: sweep_completed_checkpoints(
                        pool, checkpoint_retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )

            if retention_days:
                await _run_retention(
                    "expired jobs",
                    lambda: sweep_expired_jobs(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )
                # immediately after the jobs, so a DAG emptied by the sweep
                # above is reaped on the same cycle rather than spending one
                # interval reporting itself as a DAG that ran no jobs
                await _run_retention(
                    "orphaned dags",
                    lambda: sweep_orphaned_dags(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )
                await _run_retention(
                    "schedule log",
                    lambda: sweep_schedule_log(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )
                await _run_retention(
                    "retired workers",
                    lambda: sweep_retired_workers(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )
                await _run_retention(
                    "consumed mailbox",
                    lambda: sweep_consumed_mailbox(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )

            await asyncio.sleep(check_interval)

    finally:
        await pool.close()
        logger.info("Monitor stopped")


def cli() -> None:
    """CLI entry point: the ``pj-monitor`` console script."""
    import sys

    import click

    @click.command()
    @click.option(
        "--dsn",
        envvar="PYJOBBY_DSN",
        required=False,
        help="PostgreSQL DSN (or use PYJOBBY_DSN env var)",
    )
    @click.option(
        "--config", type=click.Path(exists=True), help="Path to pyjobby.conf.py"
    )
    @click.option(
        "--check-interval", default=10.0, show_default=True, help="Sweep interval (s)"
    )
    @click.option(
        "--liveness-grace",
        default=60.0,
        show_default=True,
        help="Seconds without a heartbeat before a worker counts as dead",
    )
    @click.option(
        "--claimed-grace",
        default=300.0,
        show_default=True,
        help="Age before an unregistered 'claimed' job counts as abandoned",
    )
    @click.option(
        "--retention-days",
        type=float,
        default=30.0,
        show_default=True,
        help="Delete terminal jobs (with their history, events, mailbox and "
        "checkpoints) older than this, plus the tables the job cascade cannot "
        "reach: emptied DAGs, schedule executions (each schedule keeps its "
        "latest) and retired worker registry rows. 0 keeps everything forever.",
    )
    @click.option(
        "--checkpoint-retention-days",
        type=float,
        default=1.0,
        show_default=True,
        help="Delete the DXE checkpoints of terminal jobs this long after they "
        "terminated, keeping the job itself. 0 keeps checkpoints as long as "
        "the job.",
    )
    @click.option(
        "--retention-batch-size",
        default=1000,
        show_default=True,
        help="Rows deleted per retention batch (a cycle drains many batches)",
    )
    @click.option(
        "--retention-max-seconds",
        type=float,
        default=5.0,
        show_default=True,
        help="Time budget per retention sweep per cycle. It drains its backlog "
        "until caught up or this runs out, so it can never delay timeout and "
        "dead-worker recovery.",
    )
    def main(
        dsn: str | None,
        config: str | None,
        check_interval: float,
        liveness_grace: float,
        claimed_grace: float,
        retention_days: float,
        checkpoint_retention_days: float,
        retention_batch_size: int,
        retention_max_seconds: float,
    ) -> None:
        """Run the pyjobby monitor: timeouts, dead-worker recovery, and
        retention (on by default; --retention-days 0 keeps everything)."""
        import asyncio

        if not dsn:
            if config:
                from urllib.parse import quote

                from .configloader import load_config_from_file

                cfg = load_config_from_file(config, keys=["db_params"])
                db_params = cfg.get("db_params", {})
                user = quote(str(db_params.get("user", "")), safe="")
                password = quote(str(db_params.get("password", "")), safe="")
                dsn = (
                    f"postgresql://{user}:{password}"
                    f"@{db_params.get('host')}:{db_params.get('port', 5432)}"
                    f"/{db_params.get('database')}"
                )
            else:
                click.echo("Error: Must provide --dsn or --config", err=True)
                sys.exit(1)

        click.echo(f"Starting monitor (check every {check_interval}s)...")
        click.echo(f"DSN: {dsn.split('@')[1] if '@' in dsn else dsn}")
        click.echo(
            "Retention: jobs "
            + (f"older than {retention_days}d" if retention_days else "kept forever")
            + ", checkpoints "
            + (
                f"{checkpoint_retention_days}d after the job terminates"
                if checkpoint_retention_days
                else "kept as long as the job"
            )
        )

        asyncio.run(
            monitor(
                dsn,
                check_interval=check_interval,
                liveness_grace_seconds=liveness_grace,
                claimed_grace_seconds=claimed_grace,
                retention_days=retention_days,
                checkpoint_retention_days=checkpoint_retention_days,
                retention_batch_size=retention_batch_size,
                retention_max_seconds=retention_max_seconds,
            )
        )

    main()


if __name__ == "__main__":
    cli()
