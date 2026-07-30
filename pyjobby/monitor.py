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
   the zombie from writing results after the job is requeued, and because
   every durable write LOCKS the job row to read its epoch, that holds for a
   write already in flight when the requeue commits, not only for one that
   starts afterwards).
3. **Unregistered-claim reclaim**: jobs stuck in 'claimed' with no registry
   reference past a grace period (a worker died between claim and register,
   or the registry was unavailable).
4. **Job retention**: terminal jobs older than ``--retention-days`` (default
   30) are deleted, taking their checkpoints, history, events and mailbox
   with them via ON DELETE CASCADE. On by default: a platform that runs
   indefinitely accumulates every completed job forever otherwise, and a
   retention policy nobody remembers to switch on is not a policy. Pass
   ``--retention-days 0`` to keep everything forever.
5. **Checkpoint retention**: ``jorb_step`` and ``jorb_stream`` rows of
   ``finished`` jobs older than ``--checkpoint-retention-days`` (default 1)
   are deleted while the job row itself stays. A stream shares the window
   because it shares the argument: it exists to be read while the job runs,
   and every reader stops at the terminal state. FINISHED only, deliberately:
   ``crashed`` and ``cancelled`` are retryable, and ``retry_job`` resumes from
   checkpoints, so reaping those early would make a DLQ retry re-execute every
   completed step — and leave its stream missing the rows the first attempt
   wrote. A finished job is only re-run by an explicit ``rerun_job``, which is
   meant to re-execute, so its checkpoints — the bulkiest thing on the row —
   are pure audit from the moment it finishes. Crashed/cancelled checkpoints
   and streams live until the whole job ages out under ``--retention-days``.
   ``0`` keeps both for as long as the job.
6. **The five tables the job cascade cannot reach**, all on the same
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
   * ``jorb_history`` — the cascade reaches it only when the job is deleted,
     which never happens for a job that never terminates: a parked durable
     machine writes ~3 history rows per wake forever, so age-based retention
     is the only thing that bounds it.

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

Run it: ``pj-monitor --config ./pyjobby.toml`` (one instance is enough;
several are safe — every sweep is a single atomic statement or a
transaction holding its row locks).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import signal
from collections.abc import Awaitable, Callable
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import db, migrations
from .configloader import describe_db_target
from .lifecycle import LIVE_STATES_SQL, TERMINAL_STATES_SQL

# The monitor sizes its own pool: its sweeps run one at a time, so two
# connections is the whole daemon's concurrency. These are NOT the operator's
# to set -- see _pool_kwargs for what happens when db_params tries.
MONITOR_POOL_SIZES: Final[dict[str, int]] = {"min_size": 1, "max_size": 2}

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
# The `TERMINAL_STATES_SQL` and `LIVE_STATES_SQL` interpolations below are
# module constants from lifecycle.py, never user input; the states are inlined
# rather than bound because the indexes they ride -- ``jorb_retention_idx`` for
# one, the three dependency indexes for the other -- are PARTIAL on exactly
# these predicates. A
# bound ``state = ANY($1)`` reads fine under the custom plan of the first few
# executions and then silently falls off the index: asyncpg prepares its
# statements, and once PostgreSQL switches to a GENERIC plan it can no longer
# prove an unknown parameter implies the index predicate. Measured on 20k
# terminal rows with nothing expired -- generic plan, bound states: Seq Scan
# + Sort, 617 buffers; literal states: Index Scan, 2 buffers.

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
#:
#: Clears the dedupe keys (db.REQUEUE_CLEARS_KEYS holds the rule): this puts
#: rows back into 'queued', and it does it for a WHOLE BATCH in one statement.
#: A single row whose deadline_key a duplicate has since re-armed would raise a
#: unique violation that aborts the entire UPDATE -- so every other doomed job
#: of every dead worker stays stranded, and the sweep fails the same way on
#: every cycle after this one. Recovery permanently disabled by one row.
SWEEP_DEAD_WORKER_JOBS_SQL = f"""
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
        {db.REQUEUE_CLEARS_KEYS}
        updated = now()
    FROM doomed
    WHERE jorb.id = doomed.id
      AND jorb.state IN ('claimed', 'running')
    RETURNING jorb.id, jorb.job_class, jorb.worker_host, jorb.claimed_by
"""

#: Retire the workers themselves. ($1 grace)
#:
#: `idle = FALSE` is not cosmetic: an idle worker is a live subscription to
#: its queue's jorb_enqueued notifications (sql/schema/90_notify.sql), so a worker that
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

#: Jobs stuck in 'claimed' too long, WHOEVER claims them. ($1 grace, $2 batch)
#:
#: 'claimed' is a transit state: the worker moves it to 'running' within
#: milliseconds, so a claim older than the grace is stranded no matter what
#: the registry says. The two ways it happens: a claim with no registry
#: reference (worker died before registering), and a LOST CLAIM ACK -- the
#: server commits the claim, the connection drops before the rows reach the
#: worker, and ex() reconnects and claims a DIFFERENT job. In the second
#: case the worker is alive and heartbeating, so the dead-worker sweep never
#: fires and the timeout sweep (state='running') never sees it; this sweep
#: is the only thing that can. Requeue bumps run_epoch, so if the claimer
#: somehow does come back for it, its writes are fenced out. Clears the dedupe
#: keys, and it is a batch statement, so for the reason the dead-worker sweep
#: above spells out.
SWEEP_STUCK_CLAIMS_SQL = f"""
    WITH doomed AS MATERIALIZED (
        SELECT id FROM jorb
        WHERE state = 'claimed'
          AND updated < now() - $1::interval
        FOR UPDATE SKIP LOCKED
        LIMIT $2
    )
    UPDATE jorb
    SET state = 'queued',
        run_epoch = run_epoch + 1,
        run_after = now(),
        timeout_at = NULL,
        {db.REQUEUE_CLEARS_KEYS}
        updated = now()
    FROM doomed
    WHERE jorb.id = doomed.id
      AND jorb.state = 'claimed'
    RETURNING jorb.id, jorb.job_class, jorb.worker_host
"""

#: Terminal jobs whose retention window elapsed. ($1 retention, $2 batch)
#:
#: THE REFUSAL COVERS EVERY UNFINISHED DEPENDENT, not just parked ones. None of
#: the three ways one job depends on another carries a foreign key, so nothing
#: in the schema stops this DELETE from stranding a reader:
#:
#:   * ``waitfor_job`` / ``waitfor_group`` -- a waiter of a deleted upstream is
#:     parked forever; nothing but the upstream's own terminal transition wakes
#:     it, and the monitor's unsatisfiable sweep then cancels it.
#:   * ``admin_data.use_result_from`` -- the reader is handed the upstream's
#:     stored ``result`` at execution time, and a reader whose upstream is gone
#:     raises ``LookupError`` on every attempt until its retries run out
#:     (pj._process says why running without the input would be worse).
#:
#: The refusal used to test ``state = 'waiting'`` alone, which protected a
#: dependent only while it was PARKED. A woken dependent -- queued, claimed or
#: running -- is just as unfinished and just as dependent, and a
#: ``use_result_from`` reader is never parked at all: it is enqueued
#: claimable and reads its input on claim. Both were deletable the moment the
#: upstream aged out.
#:
#: Every probe rides a partial index that holds only LIVE dependents
#: (``jorb_waitfor_job_idx``, ``jorb_waitfor_group_idx``,
#: ``jorb_use_result_from_idx``), so the widened refusal costs three index
#: lookups per candidate rather than three scans -- and the indexes shrink as
#: the dependents finish.
SWEEP_EXPIRED_JOBS_SQL = f"""
    SELECT j.id
    FROM jorb j
    WHERE j.state IN ({TERMINAL_STATES_SQL})
      AND COALESCE(j.finished, j.updated) < now() - $1::interval
      AND NOT EXISTS (
          SELECT 1 FROM jorb w
          WHERE w.state IN ({LIVE_STATES_SQL}) AND w.waitfor_job = j.id
      )
      AND NOT EXISTS (
          SELECT 1 FROM jorb w
          WHERE w.state IN ({LIVE_STATES_SQL}) AND w.waitfor_group = j.run_group
      )
      AND NOT EXISTS (
          SELECT 1 FROM jorb r
          WHERE r.state IN ({LIVE_STATES_SQL})
            AND r.admin_data ? 'use_result_from'
            AND r.admin_data ->> 'use_result_from' = j.id::text
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
#: state = 'finished' ONLY, deliberately NOT all terminal states. `crashed`
#: and `cancelled` are RETRYABLE (db.RETRYABLE_STATES), and retry_job resumes
#: from checkpoints -- so reaping a crashed DXE job's checkpoints after the
#: short checkpoint window (1 day) would make a DLQ retry re-execute every
#: already-completed step's side effects. Their checkpoints instead live
#: until the whole job ages out under --retention-days (30 days) and the
#: cascade takes them. A `finished` job is only ever re-run by an explicit
#: `rerun_job` ("do it again anyway"), which is SUPPOSED to re-execute, so
#: dropping its checkpoints early is correct rather than harmful. The
#: `state = 'finished'` filter still rides jorb_retention_idx (finished is
#: one of its partial-predicate states).
SWEEP_CHECKPOINT_JOBS_SQL = """
    SELECT j.id
    FROM jorb j
    WHERE j.state = 'finished'
      AND COALESCE(j.finished, j.updated) < now() - $1::interval
      AND (SELECT s.job_id FROM jorb_step s
            WHERE s.job_id = j.id LIMIT 1) IS NOT NULL
    ORDER BY COALESCE(j.finished, j.updated)
    LIMIT $2
"""

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

#: Jobs whose DXE STREAM rows have outlived the checkpoint window. ($1, $2)
#:
#: The same shape and the same window as the checkpoint probe above, because
#: it is the same argument: a stream exists to be read WHILE the job runs,
#: every reader of a terminal job's stream has already stopped (they stop at
#: the terminal state whether or not the job closed the stream), and what is
#: left is audit material of exactly the kind checkpoints are -- readable
#: through the job surfaces and nothing else, and the bulkiest thing hanging
#: off a job that produced output row by row.
#:
#: state = 'finished' ONLY, for the reason SWEEP_CHECKPOINT_JOBS_SQL spells
#: out and one of its own: `crashed`/`cancelled` are retryable, a retry
#: RESUMES from checkpoints, and a stream_write whose checkpoint fast-forwards
#: appends nothing. Reaping a crashed job's stream early would therefore leave
#: a retry that "succeeds" having produced a stream with a hole in it -- the
#: rows the first attempt wrote gone, and no attempt left that will write them
#: again. Their stream lives until the whole job ages out under
#: --retention-days and the cascade takes it.
#:
#: The existence test is a scalar subquery and not the EXISTS it reads like,
#: for the measured reason SWEEP_CHECKPOINT_JOBS_SQL documents: an EXISTS
#: sublink flattens into a semi-join, whose hash against the whole of
#: jorb_stream makes a sequential scan of jorb look free. A scalar subquery
#: stays a per-row probe of jorb_stream's primary key, whose leading column
#: is job_id -- which is why that table needs no index of its own for this.
SWEEP_STREAM_JOBS_SQL = """
    SELECT j.id
    FROM jorb j
    WHERE j.state = 'finished'
      AND COALESCE(j.finished, j.updated) < now() - $1::interval
      AND (SELECT s.job_id FROM jorb_stream s
            WHERE s.job_id = j.id LIMIT 1) IS NOT NULL
    ORDER BY COALESCE(j.finished, j.updated)
    LIMIT $2
"""

#: ...and the delete, by the primary key's leading column, for the same
#: reason the checkpoint delete is a second statement rather than a CTE.
DELETE_STREAM_ROWS_SQL = """
    DELETE FROM jorb_stream
    WHERE job_id = ANY($1::bigint[])
    RETURNING job_id, key, seq
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
#: and SWEEP_STUCK_CLAIMS_SQL only covers 'claimed' — a 'running' job
#: pointing at a deleted worker id would sit there until somebody noticed
#: by hand.
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

#: Seconds without a heartbeat before a worker counts as dead — THE liveness
#: threshold, defined once. The monitor sweeps by it, and every operator
#: surface (doctor, /metrics, the workers page) must judge liveness by the
#: SAME number: when this was written out six separate times, raising
#: `--liveness-grace` on the monitor left every UI still calling those
#: workers dead.
DEFAULT_LIVENESS_GRACE_SECONDS = 60.0

#: History rows past the retention window, oldest first. ($1 window, $2 batch)
#:
#: History of a TERMINAL job dies with the job's cascade at this same
#: window, so this sweep exists for the jobs that never terminate: a parked
#: durable machine transitions queued -> claimed -> running -> queued on
#: every wake — roughly three rows per turn, forever — and the cascade that
#: bounds every other child table never fires for it. jorb_history_at_idx
#: serves the range in order, so the probe stops at its batch instead of
#: scanning the largest table in the system.
SWEEP_HISTORY_SQL = """
    SELECT h.id
    FROM jorb_history h
    WHERE h.at < now() - $1::interval
    ORDER BY h.at
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""

#: ...and the delete, by primary key.
DELETE_HISTORY_SQL = "DELETE FROM jorb_history WHERE id = ANY($1::bigint[])"

#: Waiting jobs whose single-job dependency is already satisfied. ($1 batch)
#:
#: The wake is normally edge-triggered: the upstream's terminal transition
#: moves its waiters to 'queued' in the statement right after `finished`
#: commits. An edge can be missed two ways — the worker died in the window
#: between its terminal write and the wake, or the waiter was ENQUEUED after
#: the upstream had already finished, so the edge had already fired with
#: nobody listening. This probe is the level trigger that makes both
#: self-heal within a monitor cycle.
#:
#: One flavor per statement (job here, group below) because each maps to its
#: own partial index (``jorb_waitfor_job_idx`` / ``jorb_waitfor_group_idx``,
#: both ``WHERE state = 'waiting'``); an OR spanning the two flavors matches
#: neither index cleanly. Both scan only 'waiting' rows — live work, bounded
#: by what is actually parked, never by the size of jorb.
SWEEP_SATISFIED_JOB_WAITERS_SQL = """
    SELECT w.id
    FROM jorb w
    WHERE w.state = 'waiting'
      AND w.waitfor_job IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM jorb u
          WHERE u.id = w.waitfor_job AND u.state = 'finished'
      )
    FOR UPDATE SKIP LOCKED
    LIMIT $1
"""

#: ...the group flavor: every member finished (and the group is not empty —
#: an empty group is unsatisfiable, which is the sweep below's business).
#: NOT EXISTS stops at the first unfinished member, exactly like the wake
#: statement it backs up.
SWEEP_SATISFIED_GROUP_WAITERS_SQL = """
    SELECT w.id
    FROM jorb w
    WHERE w.state = 'waiting'
      AND w.waitfor_group IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM jorb g WHERE g.run_group = w.waitfor_group
      )
      AND NOT EXISTS (
          SELECT 1 FROM jorb g
          WHERE g.run_group = w.waitfor_group AND g.state != 'finished'
      )
    FOR UPDATE SKIP LOCKED
    LIMIT $1
"""

#: ...and the wake both flavors share. Guarded on 'waiting' so a row that
#: moved (a concurrent cancel, the edge-triggered wake beating us) is left
#: alone; the loser of that race loses quietly, as everywhere else.
#:
#: Clears deadline_key (db.WAKE_CLEARS_KEYS): 'waiting' is outside
#: jorb_deadline_idx, so two waiters may legally hold the same key, and this
#: statement wakes a whole BATCH of them at once. Carrying the key into
#: 'queued' would make the pair violate the index, roll the statement back, and
#: leave every other waiter in the batch parked -- and since the sweep is
#: level-triggered it would do it again on every pass, forever.
WAKE_WAITERS_SQL = f"""
    UPDATE jorb w
    SET state = 'queued',
        {db.WAKE_CLEARS_KEYS}
        updated = now()
    WHERE w.id = ANY($1::bigint[])
      AND w.state = 'waiting'
      AND (
        (w.waitfor_job IS NOT NULL AND EXISTS (
            SELECT 1 FROM jorb u
            WHERE u.id = w.waitfor_job AND u.state = 'finished'))
        OR (w.waitfor_group IS NOT NULL
            AND EXISTS (SELECT 1 FROM jorb g WHERE g.run_group = w.waitfor_group)
            AND NOT EXISTS (
                SELECT 1 FROM jorb g
                WHERE g.run_group = w.waitfor_group AND g.state != 'finished')))
"""

#: Waiting jobs that can never be woken: their waitfor target does not
#: exist. ($1 batch)
#:
#: Nothing but a terminal transition of the upstream ever wakes a waiter,
#: and a job that does not exist will never have one — so without this,
#: a typo'd waitfor_job id (or a waiter enqueued after retention deleted
#: its upstream) parks a row in 'waiting' forever, invisibly. Waiters of a
#: CRASHED or CANCELLED upstream are deliberately NOT here: crashed is the
#: DLQ and the upstream may be retried back to life, so those stay parked
#: and are surfaced by ``pj-admin doctor`` for the operator to decide.
SWEEP_UNSATISFIABLE_WAITERS_SQL = """
    SELECT w.id
    FROM jorb w
    WHERE w.state = 'waiting'
      AND ((w.waitfor_job IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM jorb u WHERE u.id = w.waitfor_job))
        OR (w.waitfor_group IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM jorb g WHERE g.run_group = w.waitfor_group
            )))
    FOR UPDATE SKIP LOCKED
    LIMIT $1
"""

#: The cancellation those rows get, with the reason written where every
#: surface shows it. waiting -> cancelled is the lifecycle's declared edge
#: for "this parked work is not going to happen".
#:
#: `cancelled` rather than `crashed` for surface reasons only -- this is not a
#: failure of the job, it is the platform declaring the work unreachable -- and
#: NOT because one is safer to retry than the other. Both are retryable
#: (db.RETRYABLE_STATES), and the original argument here ("crashed would be
#: wrong because a DLQ retry re-queues the row and it would then RUN with its
#: dependency unsatisfied") was true of `cancelled` too: it named a real hazard
#: and then chose the state that had it. The hazard is closed where it belongs,
#: in the requeue itself -- db.build_requeue_sql returns any row still carrying
#: a waitfor column to 'waiting', not to 'queued' -- so a retry of one of these
#: rows parks again and this sweep cancels it again on the next pass, which is
#: the honest answer while the upstream is still missing.
CANCEL_UNSATISFIABLE_WAITERS_SQL = """
    UPDATE jorb AS j
    SET state = 'cancelled',
        error_message = CASE
            WHEN j.waitfor_job IS NOT NULL THEN
                'cancelled by the monitor: waitfor_job ' || j.waitfor_job
                || ' does not exist'
            ELSE
                'cancelled by the monitor: waitfor_group ' || j.waitfor_group
                || ' has no jobs'
        END,
        finished = now(),
        updated = now()
    WHERE j.id = ANY($1::bigint[])
      AND j.state = 'waiting'
      AND ((j.waitfor_job IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM jorb u WHERE u.id = j.waitfor_job))
        OR (j.waitfor_group IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM jorb g WHERE g.run_group = j.waitfor_group)))
    RETURNING id
"""

#: Requeue one timed-out job for another attempt. Bumps run_epoch: the
#: execution that blew the deadline may still be running, and its
#: epoch-only-guarded writes must stop applying now, not at the next claim.
#: Clears the dedupe keys, like every other statement that returns a row to
#: 'queued' (db.REQUEUE_CLEARS_KEYS).
#: ($1 job_id, $2 error message, $3 retry delay interval)
RETRY_TIMED_OUT_SQL = f"""
    UPDATE jorb
    SET state = 'queued',
        run_epoch = run_epoch + 1,
        timeout_at = NULL,
        error_count = error_count + 1,
        error_message = $2,
        run_after = now() + $3::interval,
        {db.REQUEUE_CLEARS_KEYS}
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
    from .retry_strategies import get_retry_config

    admin = admin_data or {}
    on_timeout = admin.get("on_timeout", "retry")
    # The retry budget comes from the ONE home (retry_strategies defaults),
    # so whether a job dead-letters cannot depend on whether the worker or
    # this monitor noticed the failure.
    max_retries = get_retry_config(admin)["max_retries"]
    attempt = error_count + 1

    logger.warning(
        f"Job {job_id} ({job_class}) exceeded timeout, "
        f"action: {on_timeout}, attempt {attempt}/{max_retries}"
    )

    if on_timeout == "retry" and attempt < max_retries:
        from .retry_strategies import calculate_retry_from_job

        # The sweep's own SELECT already carried admin_data — the one column
        # the backoff calculation reads — so re-fetching the full row here
        # (kwargs, result, backtrace) was a wholly redundant round trip made
        # N times inside the transaction holding the batch's row locks.
        retry_delay = calculate_retry_from_job({"admin_data": admin}, attempt)

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


async def sweep_job_history(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete history rows past the retention window, whatever their job is
    doing.

    Same window as job retention: an audit trail means "as long as the work
    it describes", and for the one class of job whose history nothing else
    bounds — a durable machine that never terminates — thirty days of its
    wake/sleep loop describes it as well as a year would. Probe-then-delete
    like every sweep here; FOR UPDATE SKIP LOCKED partitions concurrent
    monitors."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        expired = await conn.fetch(SWEEP_HISTORY_SQL, retention, batch_size)
        if not expired:
            return 0
        ids = [r["id"] for r in expired]
        await conn.execute(DELETE_HISTORY_SQL, ids)

    return len(ids)


async def sweep_stranded_waiters(pool: asyncpg.Pool, batch_size: int = 500) -> int:
    """Wake waiting jobs whose dependency is satisfied, and cancel the ones
    whose dependency can never be.

    The level trigger behind the edge-triggered wake: a worker crash between
    its terminal write and the wake statement, or a waiter enqueued after
    its upstream already finished, both leave a row in 'waiting' that no
    edge will ever move again — this moves it. A waiter whose target simply
    does not exist is cancelled with the reason in ``error_message``.

    Waiters of a crashed or cancelled upstream are left alone on purpose:
    the upstream is retryable (crashed IS the DLQ), so the platform cannot
    know whether the operator intends to revive it. ``pj-admin doctor``
    surfaces those.

    Each probe holds its row locks for the paired update only, the same
    two-statement shape as every other sweep here; safe with concurrent
    monitors via FOR UPDATE SKIP LOCKED.
    """
    moved = 0
    for probe in (SWEEP_SATISFIED_JOB_WAITERS_SQL, SWEEP_SATISFIED_GROUP_WAITERS_SQL):
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(probe, batch_size)
            if rows:
                ids = [r["id"] for r in rows]
                # Count what the wake ACTUALLY changed, not what the probe
                # found: the wake re-verifies the dependency under the lock,
                # so a probe hit whose upstream was re-run in the gap is
                # correctly refused -- and must not be logged as woken.
                woken = await conn.fetch(WAKE_WAITERS_SQL + " RETURNING w.id", ids)
                if woken:
                    woken_ids = [r["id"] for r in woken]
                    logger.warning(
                        f"Woke {len(woken_ids)} waiting jobs whose dependency "
                        f"was already satisfied (missed wake): {woken_ids[:10]}"
                    )
                    moved += len(woken_ids)

    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(SWEEP_UNSATISFIABLE_WAITERS_SQL, batch_size)
        if rows:
            ids = [r["id"] for r in rows]
            cancelled = await conn.fetch(CANCEL_UNSATISFIABLE_WAITERS_SQL, ids)
            logger.error(
                f"Cancelled {len(cancelled)} waiting jobs whose waitfor "
                f"target does not exist: {[r['id'] for r in cancelled][:10]}"
            )
            moved += len(cancelled)

    return moved


async def sweep_dead_workers(
    pool: asyncpg.Pool,
    liveness_grace_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS,
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


async def sweep_stuck_claims(
    pool: asyncpg.Pool,
    claimed_grace_seconds: float = 300,
    batch_size: int = 100,
) -> int:
    """Requeue jobs stuck in 'claimed' past the grace, whoever claims them.

    A healthy worker moves claimed -> running almost immediately, so age is
    the whole signal. Covers the worker that claimed while the registry was
    unavailable and died (nothing heartbeats for it), AND the lost claim
    ack: the claim commits, the connection drops before the rows reach the
    worker, and the reconnecting worker claims a different job -- leaving
    this one owned by a live, heartbeating worker that has never heard of
    it, invisible to the dead-worker and timeout sweeps."""
    grace = datetime.timedelta(seconds=claimed_grace_seconds)
    requeued = await pool.fetch(SWEEP_STUCK_CLAIMS_SQL, grace, batch_size)

    for row in requeued:
        logger.warning(
            f"Requeued stuck claim {row['id']} ({row['job_class']}) "
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

    Jobs that any UNFINISHED job still depends on are kept regardless of age.
    None of the three dependency links carries a foreign key, so nothing in the
    schema stops the delete: ``waitfor_job``/``waitfor_group`` strand a waiter
    in 'waiting' forever (only the upstream's own terminal transition wakes
    it), and ``admin_data.use_result_from`` leaves a reader that fails on every
    claim because the result it was told to read is gone. "Unfinished" means
    every live state and not merely 'waiting': a woken dependent is still
    dependent, and a ``use_result_from`` reader is never parked at all.

    Every child table — jorb_step, jorb_event, jorb_stream, jorb_mailbox,
    jorb_history — follows via ON DELETE CASCADE, so deleting the job row is
    the whole job.

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


async def sweep_completed_streams(
    pool: asyncpg.Pool,
    checkpoint_retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete the DXE stream rows of jobs that terminated long enough ago.

    A stream is output a client reads WHILE the job runs. Once the job is
    terminal every reader has stopped — `read_stream` ends at a terminal
    state whether or not the job closed the stream — so from that instant the
    rows are audit material, readable through the job surfaces and nothing
    else. That is the same lifetime argument checkpoints have, so they share
    the same (short) window rather than getting a knob of their own.

    A stream row of a NON-terminal job is never touched at any age, and a
    'crashed' or 'cancelled' job's stream survives to the long window: those
    states are retryable, a retry fast-forwards completed `stream_write`
    checkpoints without appending, and reaping early would leave the resumed
    job's stream permanently missing the rows its first attempt wrote.

    Probe then delete by key, the shape every retention sweep here uses, and
    ``batch_size`` bounds the JOBS taken per call rather than their rows —
    all of a doomed job's stream goes together, for the reasons
    ``sweep_completed_checkpoints`` documents at length. Returns the number
    of rows deleted."""
    retention = datetime.timedelta(days=checkpoint_retention_days)

    async with pool.acquire() as conn, conn.transaction():
        doomed = await conn.fetch(SWEEP_STREAM_JOBS_SQL, retention, batch_size)
        if not doomed:
            return 0

        deleted = await conn.fetch(
            DELETE_STREAM_ROWS_SQL, [row["id"] for row in doomed]
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
    joining to this table, and the stuck-claims sweep only covers
    'claimed'. Neither can see a running job pointing at an id that no
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
    stop: asyncio.Event | None = None,
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

        if stop is not None and stop.is_set():
            # SIGTERM arrived mid-drain: yield now rather than keep taking
            # batches. Retention resumes on the next start; a shutdown that
            # waited out every sweep's budget would blow past the
            # orchestrator's stop grace and get SIGKILLed mid-transaction.
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
    stop: asyncio.Event | None = None,
) -> int:
    """Drain one retention sweep with the same failure isolation as the rest."""
    return await _run_sweep(
        name, lambda: _drain(name, sweep, batch_size, max_seconds, stop)
    )


def _pool_kwargs(target: dict[str, Any]) -> dict[str, Any]:
    """asyncpg.create_pool keyword arguments for a ``db_params`` table.

    db_params is passed to asyncpg WHOLE (that is the point of it), so a
    table carrying ``min_size``/``max_size`` used to reach create_pool as a
    duplicate keyword and take the daemon down on a bare TypeError traceback
    before it swept anything. Refused by name instead: the monitor sizes its
    own pool, and an operator who wrote those keys deserves to be told which
    one is the problem rather than shown an interpreter error.
    """
    import click

    clashes = sorted(MONITOR_POOL_SIZES.keys() & target.keys())
    if clashes:
        raise click.ClickException(
            f"db_params sets {', '.join(clashes)}, which pj-monitor does not "
            f"accept: it sizes its own pool "
            f"(min_size={MONITOR_POOL_SIZES['min_size']}, "
            f"max_size={MONITOR_POOL_SIZES['max_size']}) because its sweeps "
            f"run one at a time. Remove "
            f"{'those keys' if len(clashes) > 1 else 'that key'} from the "
            f"db_params table; every other key is passed to asyncpg unchanged."
        )
    return {**MONITOR_POOL_SIZES, **target}


async def _preflight_problem(target: str | dict[str, Any]) -> str | None:
    """Connect once and check the schema is there. Returns the operator-facing
    problem, or None when the database is usable.

    Run BEFORE the sweep loop, for the same reason pj.py's namesake runs
    before the fork: against a database with no schema the monitor loops
    forever logging a failed sweep per cycle while every health check sees a
    live process, and nothing in that picture says "migrate". The loop's own
    resilience is deliberately unchanged — a database that goes away
    mid-run is transient and must be retried, not exited on. This asks the
    question once, at startup, where the answer can be an exit code.
    """
    described = describe_db_target(target)
    try:
        conn = (
            await db.connect(target)
            if isinstance(target, str)
            else await db.connect(**target)
        )
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        hint = migrations.schema_error_hint(e)
        return f"Cannot connect to the database at {described}: {e}" + (
            f" {hint}" if hint else ""
        )
    try:
        if not await conn.fetchval("SELECT to_regclass('public.jorb')"):
            return (
                f"No pyjobby schema in the database at {described}: "
                f"{migrations.SCHEMA_REMEDY}"
            )
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        hint = migrations.schema_error_hint(e)
        return f"Cannot query the database at {described}: {e}" + (
            f" {hint}" if hint else ""
        )
    finally:
        with contextlib.suppress(Exception):
            await conn.close()
    return None


async def monitor(
    target: str | dict[str, Any],
    check_interval: float = 10,
    batch_size: int = 100,
    liveness_grace_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS,
    claimed_grace_seconds: float = 300,
    retention_days: float = 30.0,
    checkpoint_retention_days: float = 1.0,
    retention_batch_size: int = 1000,
    retention_max_seconds: float = 5.0,
) -> None:
    """Run all sweeps every ``check_interval`` seconds, forever.

    ``target`` is either a DSN string or a dict of asyncpg.connect keyword
    arguments — the ``db_params`` table of a pyjobby.toml, passed through
    whole. The config path used to be flattened into a DSN string built by
    interpolation, which dropped every key beyond host/port/database/user/
    password (ssl, server_settings, statement_cache_size, ...) and produced
    an unusable URL for a unix-socket host, so the one process an operator
    configures by file was the one that could not use the file's settings.

    Retention is on by default and the two windows are independent:
    ``retention_days`` deletes whole terminal jobs — and the emptied DAGs,
    aged schedule executions, consumed mail and retired worker rows that no
    cascade can reach — while ``checkpoint_retention_days`` deletes just the
    checkpoints and streams of terminal jobs much sooner. Either set to ``0``
    means keep forever: those sweeps do not run at all.

    One window covers all five of those tables rather than five knobs because
    none of them has its own lifetime to argue for: they all mean "as long as
    the work they describe". Checkpoints and streams get the second knob
    because they genuinely do — they are the bulkiest rows in the system and
    stop being useful the instant their job goes terminal.

    Each retention sweep drains its backlog within a ``retention_max_seconds``
    budget per cycle, so it can catch up on a busy install without ever
    delaying the latency-critical sweeps above it."""
    if isinstance(target, str):
        pool = await db.create_pool(target, **MONITOR_POOL_SIZES)
    else:
        pool = await db.create_pool(**_pool_kwargs(target))

    # SIGTERM/SIGINT set the stop event so shutdown is a clean end-of-cycle
    # rather than a kill mid-sweep: the default SIGTERM disposition never
    # reaches the finally below, dropping the pool with a sweep's
    # transaction possibly open.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    def window(days: float) -> str:
        return f"{days}d" if days else "forever"

    logger.info(
        f"Monitor started (interval {check_interval}s, "
        f"liveness grace {liveness_grace_seconds}s, "
        f"job retention {window(retention_days)}, "
        f"checkpoint retention {window(checkpoint_retention_days)})"
    )

    # A grace at or below the heartbeat cadence declares LIVE workers dead
    # between beats: their in-flight jobs are requeued out from under them,
    # over and over, and no job longer than the grace can ever finish. The
    # monitor cannot see what --heartbeat-interval the workers actually run
    # with, so this judges against the default and says so.
    if liveness_grace_seconds < 2 * db.DEFAULT_HEARTBEAT_INTERVAL_SECONDS:
        logger.warning(
            f"--liveness-grace {liveness_grace_seconds:g}s is under twice the "
            f"default worker heartbeat interval "
            f"({db.DEFAULT_HEARTBEAT_INTERVAL_SECONDS:g}s, pj "
            f"--heartbeat-interval). A grace the heartbeat cannot reliably "
            f"beat makes LIVE workers look dead mid-job and requeues their "
            f"jobs out from under them, repeatedly. Ignore this only if the "
            f"whole fleet runs a proportionally faster heartbeat."
        )

    try:
        while not stop.is_set():
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
                "stuck claims",
                lambda: sweep_stuck_claims(pool, claimed_grace_seconds),
            )
            await _run_sweep(
                "stranded waiters",
                lambda: sweep_stranded_waiters(pool, batch_size),
            )

            if checkpoint_retention_days:
                await _run_retention(
                    "completed checkpoints",
                    lambda: sweep_completed_checkpoints(
                        pool, checkpoint_retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
                )
                # streams share the checkpoint window and the checkpoint
                # argument: both stop being useful the moment the job does
                if not stop.is_set():
                    await _run_retention(
                        "completed streams",
                        lambda: sweep_completed_streams(
                            pool, checkpoint_retention_days, retention_batch_size
                        ),
                        retention_batch_size,
                        retention_max_seconds,
                        stop,
                    )

            if retention_days and not stop.is_set():
                await _run_retention(
                    "expired jobs",
                    lambda: sweep_expired_jobs(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
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
                    stop,
                )
                await _run_retention(
                    "schedule log",
                    lambda: sweep_schedule_log(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
                )
                await _run_retention(
                    "retired workers",
                    lambda: sweep_retired_workers(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
                )
                await _run_retention(
                    "consumed mailbox",
                    lambda: sweep_consumed_mailbox(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
                )
                await _run_retention(
                    "job history",
                    lambda: sweep_job_history(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                    stop,
                )

            # wait out the interval, or leave immediately on SIGTERM/SIGINT
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=check_interval)

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
    @click.option("--config", type=click.Path(exists=True), help="Path to pyjobby.toml")
    @click.option(
        "--check-interval", default=10.0, show_default=True, help="Sweep interval (s)"
    )
    @click.option(
        "--liveness-grace",
        default=DEFAULT_LIVENESS_GRACE_SECONDS,
        show_default=True,
        help="Seconds without a heartbeat before a worker counts as dead",
    )
    @click.option(
        "--claimed-grace",
        default=300.0,
        show_default=True,
        help="Age before a 'claimed' job counts as stuck and is requeued",
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
        help="Delete the DXE checkpoints and streams of terminal jobs this long "
        "after they terminated, keeping the job itself. 0 keeps them as long as "
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

        from .configloader import load_config_from_file

        target: str | dict[str, Any]
        if dsn:
            target = dsn
        elif config:
            # The config's db_params are handed to asyncpg WHOLE. Rebuilding
            # a URL from five of its keys silently dropped the rest and could
            # not express a unix-socket host at all.
            cfg = load_config_from_file(config, keys=["db_params"])
            db_params = cfg.get("db_params")
            if not db_params:
                raise click.ClickException(f"No db_params found in config: {config}")
            target = db_params
        else:
            click.echo("Error: Must provide --dsn or --config", err=True)
            sys.exit(1)

        # Refuse the db_params keys the monitor owns BEFORE anything tries to
        # connect with them: min_size/max_size are not asyncpg.connect
        # arguments either, so the preflight below would die on the same raw
        # TypeError the pool used to.
        if isinstance(target, dict):
            _pool_kwargs(target)

        # Ask the question every sweep is about to ask, once, before the loop:
        # exit code 2 (a startup precondition) rather than a daemon that runs
        # forever failing every sweep while looking perfectly alive.
        problem = asyncio.run(_preflight_problem(target))
        if problem is not None:
            click.echo(problem, err=True)
            sys.exit(2)

        click.echo(f"Starting monitor (check every {check_interval}s)...")
        click.echo(f"Database: {describe_db_target(target)}")
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
                target,
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
