#!/usr/bin/env python3
"""The pyjobby worker: claims jobs from PostgreSQL and executes them.

Design notes:

* Claiming is a durable write (UPDATE ... WHERE id = (SELECT ... FOR UPDATE
  SKIP LOCKED)) so no locks are held while a job runs; queue controls
  (pause / max concurrency / rate limit) are enforced inside the claim
  statement itself from the jorb_queue table.
* A job keeps ONE row for life. Retries requeue the same row; run_epoch is
  bumped on every claim and every state-changing statement is fenced on it,
  so a stale execution (superseded by the reaper or an operator requeue)
  cannot write results or checkpoints.
* Workers register in jorb_worker and heartbeat last_seen on a dedicated
  connection; the monitor requeues jobs owned by workers that stop beating.
* Idle workers sleep on LISTEN jorb_enqueued and wake the moment work
  arrives; polling remains the fallback for run_after-delayed jobs. The
  wakeup is DEMAND-GATED: enqueueing only notifies when some worker on
  that queue has published jorb_worker.idle, so a busy fleet -- which is
  never asleep -- costs the enqueue path nothing. A worker publishes idle
  BEFORE its last claim attempt, which is what makes it impossible for a
  job to be both unseen and unannounced (see _set_idle and sql/schema.sql).
* Cancellation of running jobs: operators set cancel_requested, the
  jorb_cancel NOTIFY reaches the executing worker, and the job's task is
  cancelled at the next await point.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime
import importlib
import inspect
import os
import platform
import pydoc  # for instantiating classes from string names
import random
import signal
import sys
import time
import traceback
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import Process
from pathlib import Path
from typing import Any, ClassVar, Final

import asyncpg  # type: ignore[import-untyped]
import click
from aiohttp import web
from loguru import logger

from . import db, dxe
from .client import DEFAULT_PRIO_CEILING
from .configloader import load_config_from_file

fmt = (
    "<yellow>{process.id:>}:{process.name:<}</yellow> "
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<1}</level> | "
    "<cyan>{name}</cyan>:"
    "<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


def configure_worker_logging() -> None:
    """Install the worker's terminal log format.

    Called from the worker entry points only — importing this module must
    never reconfigure the process-global loguru sinks."""
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=fmt)


def cleanupLogLengths(record: dict[str, Any]) -> None:
    """Make visual logging cleaner by removing redundant components for
    shorter in-terminal line lengths."""
    record["process"].name = record["process"].name.replace("rocess-", "")
    record["level"].name = record["level"].name[0]

    # only use deepest module name for the log
    record["name"] = (record["name"] or "").split(".")[-1]


# ignore type here because the loguru signature is too specific
logger = logger.patch(cleanupLogLengths)  # type: ignore[arg-type]


STMTS: dict[str, str] = {}

# Claim the single most-urgent runnable job in our queue, honoring the
# Claiming lives in claim_jorb() (see sql/schema.sql), so the queue control
# plane -- paused / max_concurrency / rate_limit -- is enforced for every
# claimer rather than re-implemented by each one. Enforcing it there is also
# the only way it can be CORRECT: the caps need claims for a controlled queue
# serialized against each other, which a single statement cannot do.
# run_epoch increments on every claim: it is the fencing token that keeps a
# superseded execution from writing results/checkpoints later.
# Argument order is the worker's, not the function's.
STMTS["claim"] = """SELECT * FROM claim_jorb($3, $4::text[], $5, $1, $2, $6)"""

# Runnable work in this queue that this worker's ceiling hides from it.
# Only ever run by an IDLE worker, at most once a minute (see
# _report_unclaimable_priorities): a job above every live worker's ceiling
# is otherwise completely silent -- queued forever, never failing, absent
# from the DLQ -- so this is the one place the platform can notice it.
# Served by jorb_claim_idx (queue, prio, run_after) WHERE state = 'queued'.
STMTS["above-ceiling"] = """SELECT count(*) AS above, min(prio) AS lowest
                              FROM jorb
                             WHERE queue = $1
                               AND state = 'queued'
                               AND prio > $2
                               AND run_after <= now()"""

# Fetch an upstream job's stored result for run-time result passing
# (admin_data.use_result_from).
STMTS["get-result"] = """SELECT state, result FROM jorb WHERE id = $1"""

# The DATABASE's clock. Durable sleep computes its wake time against this
# rather than the worker's wall clock, because the reschedule that enforces
# the wake compares run_after to the database's now(): a worker clock ahead
# of the server would otherwise wake "early" by the skew, compute a positive
# remainder, and re-sleep an extra round — forever, for a skewed-enough
# worker. One clock domain per decision.
STMTS["now"] = """SELECT now() AS now"""

# claimed -> running (records `started`; timeout enforcement, duration
# metrics, and rate limiting all key off this transition). The deadline is
# stamped HERE rather than by a separate statement: the two writes were
# microseconds apart on the hottest table in the system, and a job's
# deadline measured from `started` is the deadline the operator configured.
# $3 is NULL for a job with no timeout.
#
# RETURNING id, and the caller MUST check it: zero rows means the row was
# requeued or cancelled between claim and here, and executing the job
# anyway runs its side effects concurrently with the attempt that replaced
# it — a non-DXE job would never find out.
STMTS["run"] = """UPDATE jorb
              SET state = 'running',
                  started = now(),
                  timeout_at = CASE WHEN $3::interval IS NULL THEN NULL
                                    ELSE now() + $3::interval END,
                  updated = now()
              WHERE id = $1
                AND state = 'claimed'
                AND run_epoch = $2
          RETURNING id"""

# Terminal success. Epoch-fenced: if the reaper or an operator requeued this
# job while we ran, our (stale) completion is a no-op.
#
# Every transition OUT of an attempt (finished/crashed/cancelled/retry/
# reschedule) advances run_epoch, for the reason db.build_requeue_sql
# documents: statements guarded ONLY by the epoch — checkpoints, events,
# mailbox sends, set-timeout — must stop applying the moment the row leaves
# the attempt, and a state guard alone does not stop them. The concrete
# leak this closes: a synchronous job thread the worker had to abandon is
# unstoppable and still holds a then-valid epoch.
# RETURNING cancel_requested as well as id: a completion that lands with a
# cancel still pending means the task never yielded at an await point for
# the cancellation to be delivered — the operator's cancel silently did
# nothing, and the caller logs that instead of recording plain success.
STMTS["finished"] = """UPDATE jorb
              SET state = 'finished',
                  result = $2,
                  run_epoch = run_epoch + 1,
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $3
          RETURNING id, cancel_requested"""

# Same-row retry: back into the queue with backoff; jorb_history holds the
# per-attempt audit trail (recorded by trigger on the state change).
# Bumps run_epoch so the attempt being abandoned is fenced out immediately --
# a timed-out task may still be executing, and checkpoint writes are guarded
# by the epoch alone.
STMTS["retry"] = """UPDATE jorb
              SET state = 'queued',
                  run_epoch = run_epoch + 1,
                  run_after = now() + $2::interval,
                  error_message = $3,
                  error_backtrace = $4,
                  error_count = error_count + 1,
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $5
          RETURNING id"""

# Terminal failure: retries exhausted (or on_timeout='fail'). state='crashed'
# IS the dead letter queue. Bumps run_epoch (see STMTS["finished"]): the
# execution being dead-lettered may still be alive in a thread.
STMTS["crashed"] = """UPDATE jorb
              SET state = 'crashed',
                  error_message = $2,
                  error_backtrace = $3,
                  error_count = error_count + 1,
                  run_epoch = run_epoch + 1,
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $4
          RETURNING id"""

# A running job whose cancellation was requested and honored. Bumps
# run_epoch (see STMTS["finished"]): "honored" is the worker's view — a
# synchronous task that ignored the cancellation is still running in its
# thread, and this is what fences its writes out.
STMTS["cancelled"] = """UPDATE jorb
              SET state = 'cancelled',
                  run_epoch = run_epoch + 1,
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $2
          RETURNING id"""

# Job.reschedule() and durable sleep: the task asked to run again later, so
# the requeue wins over normal completion. Fenced like every other
# state-changing write: a superseded attempt must not be able to requeue a
# job the live attempt is still running (the winner's fenced completion
# would then no-op and its result would be lost).
# Bumps run_epoch (see STMTS["finished"]): the row is back in the queue the
# moment this commits, so the execution that asked to be rescheduled is no
# longer entitled to write — "run me again later, from the top" abandons
# the current attempt by definition.
STMTS["reschedule"] = """UPDATE jorb
              SET state = 'queued',
                  run_epoch = run_epoch + 1,
                  run_after = now() + $2::interval,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $3
          RETURNING id"""

# Wake jobs waiting on a group: when ZERO jobs in run_group $1 are
# unfinished, everything waiting on that group becomes claimable.
#
# NOT EXISTS, never `0 = count(*)`: a count must visit EVERY member of the
# group before it can say zero, and this statement runs on the completion
# path of every grouped job — count made a fan-out of N cost O(N²) index
# reads across its lifetime. NOT EXISTS stops at the first unfinished
# member, which is O(1) for all N-1 completions that don't wake anyone.
STMTS["enqueue-next-if-peer-group-is-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = now()
            WHERE id IN (
                SELECT id FROM jorb
                WHERE waitfor_group = $1
                   AND state = 'waiting'
                   AND NOT EXISTS (
                       SELECT 1 FROM jorb g
                       WHERE g.run_group = $1
                          AND g.state != 'finished'
                   )
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id"""

# Wake jobs waiting on a single upstream job we just finished.
STMTS["enqueue-next-self-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = now()
            WHERE id IN (
                SELECT id FROM jorb
                WHERE waitfor_job = $1
                   AND state = 'waiting'
                   AND EXISTS (
                       SELECT 1 FROM jorb u
                       WHERE u.id = $1
                          AND u.state = 'finished'
                   )
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id"""

# DXE primitives (see pyjobby/dxe.py for semantics)
STMTS["load-steps"] = dxe.LOAD_STEPS_SQL
STMTS["record-step"] = dxe.RECORD_STEP_SQL
STMTS["compact-steps"] = dxe.COMPACT_STEPS_SQL
STMTS["set-event"] = dxe.SET_EVENT_SQL
STMTS["get-event"] = dxe.GET_EVENT_SQL
STMTS["send"] = dxe.SEND_SQL
STMTS["recv"] = dxe.RECV_SQL

# Publish (or withdraw) this worker's demand for jorb_enqueued wakeups.
# `idle IS DISTINCT FROM $2` makes a redundant call a no-op at the server:
# the flag must be written on the busy<->parked TRANSITION only, or the fix
# would have traded one NOTIFY per enqueue for one UPDATE per poll.
STMTS["worker-idle"] = """UPDATE jorb_worker
              SET idle = $2
              WHERE id = $1
                AND idle IS DISTINCT FROM $2"""

# Worker registry (executed on the heartbeat connection, not prepared).
WORKER_REGISTER_SQL = """INSERT INTO jorb_worker
        (host, pid, queue, capabilities, version, job_threads)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id"""
# The heartbeat carries the job-thread saturation with it. A worker whose pool
# is full of abandoned threads refuses to claim (see _too_many_abandoned_
# threads) but goes on beating, so every liveness signal the platform has says
# it is fine while it does nothing -- and the only place that condition was
# visible was that worker's own log. Publishing it HERE is what makes it
# visible everywhere else, and it costs nothing: this UPDATE already runs
# every heartbeat_interval, the columns are on the row being written, and
# jorb_worker has one row per worker with no index over either column.
WORKER_HEARTBEAT_SQL = """UPDATE jorb_worker
           SET last_seen = now(),
               job_threads = $2,
               job_threads_abandoned = $3
         WHERE id = $1"""
# Retiring clears idle in the same statement: a worker that exits while
# marked idle would otherwise keep this queue's notifications switched on
# for every enqueue until the monitor swept it.
WORKER_SHUTDOWN_SQL = (
    "UPDATE jorb_worker SET shutdown_at = now(), idle = FALSE WHERE id = $1"
)


@dataclass
class JobSystem:
    """A PostgreSQL job executor: one process, one queue, one job at a time.

    Claims by queue/priority/run_after/capability, executes the job class
    named on the row, and drives the full job lifecycle (running, retries
    with backoff, terminal crash into the DLQ, cancellation, dependency
    wakeups)."""

    dsn: dict[str, str]
    qname: str
    capabilities: tuple[str]
    workerId: int
    checkInterval: float = 5  # seconds
    webPort: dict[str, list[dict[str, Any]] | set[str]] | None = None
    # This worker's priority CEILING: it claims jobs with prio <= this and
    # is blind to everything above it (lower prio is more urgent). Set from
    # `pj --max-prio`; the default is shared with the client, which refuses
    # to enqueue above it (see client.DEFAULT_PRIO_CEILING).
    prio: int = DEFAULT_PRIO_CEILING
    stop: bool = False
    pid: int = field(default_factory=lambda: os.getpid())
    node: str = field(default_factory=lambda: platform.node())
    cache: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 10  # Maximum attempts before terminal 'crashed'
    default_timeout: int = 3600  # Default job timeout in seconds (1 hour)
    heartbeat_interval: float = 10.0  # seconds between registry heartbeats
    # Size of this worker's own job-thread pool, and therefore the number of
    # ABANDONED job threads it tolerates before it stops claiming and says so.
    # A worker runs one job at a time, so anything above 1 is headroom for
    # timed-out synchronous jobs whose threads cannot be stopped. See
    # _too_many_abandoned_threads.
    job_threads: int = 8
    # re-import a job's module when its source changes (development loop);
    # off by default so production never re-executes module code per job
    reload_jobs: bool = False
    # pid of the launcher process that forked us; when set, the worker stops
    # if that process dies (prevents orphaned workers polling forever after
    # their launcher is killed). 0 disables the check (direct/embedded use).
    _launcher_pid: int = 0

    # --- runtime state (populated by run(); declared so every attribute
    # exists from construction and is type-checked, never probed for) ---
    cxn: asyncpg.Connection | None = None
    stmts: dict[str, asyncpg.PreparedStatement] = field(default_factory=dict)
    worker_id: int | None = None  # jorb_worker.id once registered
    processed: int = 0
    errors: int = 0
    # wakeup + cancellation coordination
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    # last value published to jorb_worker.idle, tracked in memory so the
    # flag is written only when it changes (see _set_idle)
    _idle: bool = False
    _current_job_id: int | None = None
    _exec_task: asyncio.Task[Any] | None = None
    _cancel_current: bool = False
    # registry heartbeat runs on its own connection so a long job never
    # delays liveness reporting
    _hb_cxn: asyncpg.Connection | None = None
    _hb_task: asyncio.Task[None] | None = None
    # optional per-worker HTTP listener
    _web_runner: web.ServerRunner | None = None
    # resolved job classes (importing per job is a real cost; see
    # resolve_job_class) and the source mtimes --reload watches
    _class_cache: dict[str, type[Job]] = field(default_factory=dict)
    _class_mtimes: dict[str, float] = field(default_factory=dict)
    # this worker's OWN thread pool for running job code, plus the futures of
    # every thread it has started (see _live_job_threads)
    _threads: ThreadPoolExecutor | None = None
    _job_threads: list[ThreadFuture[Any]] = field(default_factory=list)
    # the one of those futures the worker is still WAITING on, if any (see
    # _abandoned_job_threads); None whenever no job is running here
    _running_thread: ThreadFuture[Any] | None = None
    # monotonic instant this worker started refusing to claim, and when it
    # last said so (see _too_many_abandoned_threads)
    _refusing_since: float | None = None
    _refusal_logged: float = 0.0
    # when this worker last reported queued work above its priority ceiling
    # (see _report_unclaimable_priorities)
    _ceiling_reported: float = 0.0

    async def ex(self, op: str, *args: Any) -> list[asyncpg.Record]:
        """Execute prepared statement ``op`` with *args, reconnecting (and
        re-preparing everything) if the connection was lost."""
        while True:
            try:
                return await self.stmts[op].fetch(*args)  # type: ignore[no-any-return]
            except (
                asyncpg.InterfaceError,
                asyncpg.PostgresConnectionError,
                OSError,
            ) as e:
                # OSError covers the socket-level drops (ConnectionResetError,
                # BrokenPipeError) that a failover delivers below asyncpg's
                # own exception types; _reconnect, _heartbeat_loop and the
                # scheduler already catch it, and the worker's whole point is
                # to survive a lost connection rather than exit on the claim.
                if self.stop:
                    raise
                if (
                    isinstance(e, asyncpg.InterfaceError)
                    and self.cxn is not None
                    and not self.cxn.is_closed()
                ):
                    # InterfaceError over a LIVE connection is client-side
                    # misuse (wrong argument count, bad parameter type), not
                    # a lost connection: reconnecting cannot fix it, so
                    # retrying spins forever on a bug that should be loud.
                    raise
                logger.warning(
                    f"Database connection lost during '{op}' ({e}); reconnecting..."
                )
                await asyncio.sleep(0.5)
                await self._reconnect()

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------

    async def _connect_and_prepare(self) -> None:
        self.cxn = await db.connect(**self.dsn)
        self.stmts = {
            name: await self.cxn.prepare(stmt) for name, stmt in STMTS.items()
        }
        await self._listen()

    async def _listen(self) -> None:
        """LISTEN for enqueue wakeups and cancellation requests.

        ``jorb_enqueued`` only arrives while this worker has published
        ``jorb_worker.idle`` (see _set_idle); LISTENing costs nothing when
        nothing is sent, and the poll in run() covers a missed wakeup."""

        def _on_enqueue(conn: Any, pid: int, channel: str, payload: str) -> None:
            if payload == self.qname:
                self._wake.set()

        def _on_cancel(conn: Any, pid: int, channel: str, payload: str) -> None:
            if (
                self._current_job_id is not None
                and payload == str(self._current_job_id)
                and self._exec_task is not None
                and not self._exec_task.done()
            ):
                self._cancel_current = True
                self._exec_task.cancel()

        assert self.cxn is not None, "_listen requires an established connection"
        try:
            await self.cxn.add_listener("jorb_enqueued", _on_enqueue)
            await self.cxn.add_listener("jorb_cancel", _on_cancel)
        except asyncpg.PostgresError as e:
            logger.warning(f"Could not LISTEN for wakeups ({e}); polling only")

    async def _reconnect(self) -> None:
        """Re-establish the worker connection and re-prepare all statements.

        Retries until the database is reachable again (workers are long-lived
        daemons: losing the database is expected to be a transient
        condition)."""
        if self.cxn is not None:
            with contextlib.suppress(Exception):
                await self.cxn.close()

        while not self.stop:
            try:
                await self._connect_and_prepare()
                logger.info("Database connection re-established")
                return
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logger.warning(f"Reconnect attempt failed ({e}); retrying...")
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # worker registry + heartbeat (dedicated connection so a long-running
    # job on the main connection never blocks liveness reporting)
    # ------------------------------------------------------------------

    async def _register_worker(self) -> None:
        try:
            self._hb_cxn = await db.connect(**self.dsn)
            await self._try_register()
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            # Unregistered is a DEGRADED mode, not a cosmetic one: claims
            # carry no claimed_by (dead-worker recovery cannot see this
            # worker's jobs), and _set_idle never arms the enqueue wakeup,
            # so claim latency is the full poll interval. Say all of that,
            # and keep retrying from the heartbeat loop instead of giving
            # up at startup forever.
            logger.warning(
                f"Worker registry unavailable ({e}); running UNREGISTERED — "
                f"claims carry no owner (dead-worker recovery cannot requeue "
                f"this worker's jobs) and enqueue wakeups are off (claim "
                f"latency is the poll interval). Registration will be "
                f"retried every {self.heartbeat_interval:g}s."
            )
            self.worker_id = None
        self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def _try_register(self) -> None:
        from pyjobby import __version__

        self.worker_id = await self._hb_cxn.fetchval(  # type: ignore[union-attr]
            WORKER_REGISTER_SQL,
            self.node,
            self.pid,
            self.qname,
            list(self.capabilities),
            __version__,
            self.job_threads,
        )

    async def _heartbeat_loop(self) -> None:
        failures = 0
        while not self.stop:
            try:
                if self._hb_cxn is None or self._hb_cxn.is_closed():
                    self._hb_cxn = await db.connect(**self.dsn)
                if self.worker_id is None:
                    # registration failed earlier; this is the retry
                    await self._try_register()
                    logger.info(
                        f"Worker registered (id {self.worker_id}); leaving "
                        f"unregistered mode"
                    )
                else:
                    # One statement, three columns: liveness and the reason
                    # this worker might be alive without working. See
                    # WORKER_HEARTBEAT_SQL for why the second belongs here.
                    await self._hb_cxn.execute(
                        WORKER_HEARTBEAT_SQL,
                        self.worker_id,
                        self.job_threads,
                        self._abandoned_job_threads(),
                    )
                if failures:
                    logger.info(
                        f"Worker heartbeat restored after {failures} failure(s)"
                    )
                failures = 0
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
                # NEVER silent: a worker whose heartbeat flatlines looks dead
                # to the monitor, which requeues its in-flight jobs while it
                # is still running them. Rate-limited so an outage does not
                # write a line per interval forever.
                failures += 1
                if failures == 1 or failures % 6 == 0:
                    logger.warning(
                        f"Worker heartbeat failing ({failures} consecutive: "
                        f"{e}); the monitor will treat this worker as dead "
                        f"after --liveness-grace"
                    )
            await asyncio.sleep(self.heartbeat_interval)

    async def _set_idle(self, idle: bool) -> None:
        """Publish (or withdraw) this worker's demand for enqueue wakeups.

        ``jorb_enqueued`` is only notified when some worker on the queue has
        this flag set (see sql/schema.sql), so this IS the subscription. The
        ordering around it is the correctness argument, and it is the reason
        run() publishes idle BEFORE its last claim rather than after:

            set idle = TRUE  ->  claim  ->  got a job? clear idle and run it
                                        ->  got nothing? sleep on the wakeup

        An enqueue whose gate runs after this commit sees us and notifies us.
        An enqueue that committed before the following claim's snapshot is
        found by that claim. There is no order in which a job is both unseen
        and unannounced, apart from the sub-millisecond gap between an
        enqueue's WAL flush and its visibility -- which the unconditional
        poll every ``checkInterval`` covers. A missed wakeup costs latency,
        never a lost job.

        The write happens only on the busy<->parked transition (tracked here
        and again in the WHERE clause), so a busy worker never writes it and
        a parked worker writes it once.
        """
        if self._idle == idle:
            return
        if self.worker_id is None:
            # unregistered (registry was unavailable at startup): there is no
            # row to publish demand on, so this worker relies on its poll.
            self._idle = idle
            return
        await self.ex("worker-idle", self.worker_id, idle)
        self._idle = idle

    # ------------------------------------------------------------------
    # job threads: the pool, and what happens when abandoned ones pile up
    # ------------------------------------------------------------------

    def _thread_pool(self) -> ThreadPoolExecutor:
        """This worker's OWN pool for running job code.

        Job code used to go to the event loop's *default* executor, which is
        shared with everything else asyncio runs in a thread — most notably
        ``getaddrinfo`` for every reconnect. Abandoned job threads filling
        that pool therefore took the worker's own I/O down with them. A
        dedicated pool means a runaway job class can only exhaust the budget
        that exists for running jobs, and makes that budget a number the
        platform chose (``job_threads``) rather than an interpreter default.

        Built on first use so constructing a JobSystem starts no threads."""
        if self._threads is None:
            self._threads = ThreadPoolExecutor(
                max_workers=self.job_threads,
                thread_name_prefix=f"pyjobby-job-{self.qname}",
            )
        return self._threads

    def _live_job_threads(self) -> int:
        """How many job threads are still running.

        Called from the event loop only. ``Future.done()`` is the exact
        signal — a thread's future completes when the thread returns, whether
        or not anybody is still waiting on it — so this needs no locking and
        no callbacks, and pruning here keeps the list from growing for the
        life of the worker."""
        self._job_threads = [t for t in self._job_threads if not t.done()]
        return len(self._job_threads)

    def _abandoned_job_threads(self) -> int:
        """How many live job threads belong to no job this worker is running.

        ``_live_job_threads`` counts pool OCCUPANCY, which is the right budget
        for the claim decision and the wrong number to publish: a synchronous
        job running normally holds a slot for its whole duration, so a worker
        with a small pool would report itself saturated every time it did
        exactly the thing it exists to do. Excluding the thread the worker is
        still waiting on leaves only the threads nothing is waiting for --
        left behind by jobs that already ended, and therefore never going to
        free their slot on any schedule the worker controls.

        The two agree at the moment that matters. ``_too_many_abandoned_
        threads`` runs between jobs, where there is no running thread to
        exclude, so what the heartbeat publishes is what the refusal decided:
        ``abandoned >= job_threads`` is the refusing state, exactly."""
        live = self._live_job_threads()
        running = self._running_thread
        if running is not None and not running.done():
            live -= 1
        return live

    def _too_many_abandoned_threads(self) -> bool:
        """Should this worker refuse to claim, because timed-out synchronous
        jobs have filled its thread pool?

        A timed-out synchronous ``task()`` is recorded and abandoned, but
        **nothing can stop its thread** — it keeps a pool slot until the work
        it is doing finishes on its own. Called between jobs, every live job
        thread is therefore an abandoned one, and once they fill the pool the
        next ``submit()`` would simply queue: the worker would go on claiming
        jobs that never start, hold them in ``running`` until the monitor
        swept them, and look healthy the whole time. A worker that stops
        working without failing is the worst shape of outage.

        So it claims only while the pool still has a free slot — the job it
        would claim needs exactly one, so a claimed job never queues behind an
        abandoned thread — and when they have taken every slot it says so at
        ERROR, immediately and every 30s until it recovers. Nothing is claimed
        and abandoned to do it, so no job's retry budget is spent on this
        worker's condition: the queue simply backs up, visibly, for other
        workers to drain."""
        live = self._live_job_threads()
        if live < self.job_threads:
            if self._refusing_since is not None:
                logger.warning(
                    "[{}:{}] Claiming again after {:.1f}s; {} abandoned job "
                    "thread(s) left",
                    self.qname,
                    self.prio,
                    time.monotonic() - self._refusing_since,
                    live,
                )
                self._refusing_since = None
            return False

        now = time.monotonic()
        if self._refusing_since is None:
            self._refusing_since = now
            self._refusal_logged = now - 30  # log immediately below
        if now - self._refusal_logged >= 30:
            self._refusal_logged = now
            logger.error(
                "[{}:{}] NOT CLAIMING: {} abandoned job thread(s) fill this "
                "worker's pool of {}. Timed-out synchronous jobs cannot be "
                "stopped; this worker resumes when they finish. Refusing for "
                "{:.0f}s so far — if this persists, that job class blocks far "
                "past its timeout and needs a shorter one, an interruptible "
                "implementation, or its own worker.",
                self.qname,
                self.prio,
                live,
                self.job_threads,
                now - self._refusing_since,
            )
        return True

    # ------------------------------------------------------------------
    # the priority ceiling: saying so when work is hiding above it
    # ------------------------------------------------------------------

    async def _report_unclaimable_priorities(self) -> None:
        """Say when this queue holds runnable work above this worker's
        ceiling.

        A job with ``prio`` above every live worker's ceiling is the quietest
        failure this platform has: ``claim_jorb`` filters it out, so it never
        runs, never errors, never retries, never reaches the DLQ and never
        ages into any check that looks at *terminal* states. It is simply
        ``queued``, forever, and the ordering being inverted (LOWER is MORE
        urgent) is what walks people into it — ``priority=5000`` reads as
        "whenever you get to it" and means "never".

        The client refuses to enqueue above its declared ceiling
        (``client.validate_priority``), which is where the caller can still
        be told. This is the other half, for the jobs that got in anyway —
        raw SQL, another tool, a schedule, a client that declared a higher
        ceiling than the workers actually run with.

        Run only by an IDLE worker (nothing was claimable, so this costs no
        throughput) and at most once a minute. It reports what is true of
        THIS worker; a fleet may legitimately run a higher-ceiling worker
        elsewhere, which the message says rather than assumes."""
        now = time.monotonic()
        # 0.0 means "never reported": a monotonic clock has no fixed epoch,
        # so an elapsed-time test against it would be a guess about uptime
        if self._ceiling_reported and now - self._ceiling_reported < 60:
            return
        self._ceiling_reported = now

        rows = await self.ex("above-ceiling", self.qname, self.prio)
        above = rows[0]["above"] if rows else 0
        if not above:
            return
        logger.warning(
            "[{}:{}] {} runnable job(s) on this queue are ABOVE this worker's "
            "priority ceiling of {} (least-urgent claimable prio; the lowest "
            "blocked one is {}) and will never be claimed here. Lower prio is "
            "MORE urgent, so a big number is not 'later', it is 'never': "
            "unless another worker on this queue runs with a higher "
            "--max-prio, those jobs stay queued forever. Fix by lowering "
            "their prio (client.update_job_priority) or by running a worker "
            "with --max-prio at or above {}.",
            self.qname,
            self.prio,
            above,
            self.prio,
            rows[0]["lowest"],
            rows[0]["lowest"],
        )

    async def _deregister_worker(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hb_task
        if self._hb_cxn is not None:
            if self.worker_id is not None:
                with contextlib.suppress(Exception):
                    await self._hb_cxn.execute(WORKER_SHUTDOWN_SQL, self.worker_id)
            with contextlib.suppress(Exception):
                await self._hb_cxn.close()

    def shutdown(self, signum: int, frame: Any) -> None:
        """Request graceful shutdown - stop processing new jobs but finish current job."""
        logger.info(f"Shutdown request received by signal {signum}")
        self.stop = True
        # Note: The main loop will finish the current job before exiting

    # ------------------------------------------------------------------
    # optional per-worker HTTP listener (experimental)
    # ------------------------------------------------------------------

    async def webHandler(self, request: web.Request) -> web.Response:
        """Dispatch ``/<dotted.class.Name>`` to that job class's web().

        Only classes explicitly listed in ``web_listen["paths"]`` are
        reachable — the dotted name comes from the URL, so an unrestricted
        lookup would let a caller import and invoke arbitrary code.
        """
        assert self.webPort
        requested = request.path.lstrip("/")
        if requested not in self.webPort["paths"]:
            return web.Response(status=404, text="not so fast!")

        return await self.resolve_job_class(requested).web(request)

    async def _start_web_listener(self) -> None:
        if not (self.webPort and "sites" in self.webPort):
            return
        server = web.Server(self.webHandler)  # type: ignore[arg-type]
        runner = web.ServerRunner(server)
        await runner.setup()
        self._web_runner = runner

        for site in self.webPort["sites"]:
            assert isinstance(site, dict)
            # note: .start() returns but continues running the server!
            if "path" in site:
                assert isinstance(site["path"], str)
                site["path"] = site["path"] + f"-{self.workerId}"
                await web.UnixSite(runner, **site).start()
            else:
                site.update({"reuse_port": True})
                await web.TCPSite(runner, **site).start()

            logger.info(
                "Starting server at {}",
                ";".join(
                    [f"{k}:{v}" for k, v in site.items() if not isinstance(v, bool)]
                ),
            )

    def resolve_job_class(self, klassName: str) -> type[Job]:
        """Resolve a dotted job-class path to a class object.

        Classes are resolved once and cached: importing is a filesystem
        stat + compile + module execution, and doing that per job costs
        throughput and re-runs arbitrary module-level code every time.

        With ``reload_jobs`` (the ``--reload`` dev flag) the module is
        re-imported only when its source file changes on disk, so an edit
        takes effect on the next job without paying import cost — or
        re-executing module side effects — on every job.
        """
        cached = self._class_cache.get(klassName)
        module_name = ".".join(klassName.split(".")[:-1])

        def source_mtime() -> float:
            module = sys.modules.get(module_name)
            source = getattr(module, "__file__", None) if module else None
            if source:
                # ONE stat syscall, where exists()+getmtime() was two. That is
                # NOT a speedup, and the number is here so nobody re-derives it
                # from first principles: measured by `pj-bench resolve` (the
                # reload_check arm, 3 interleaved pairs, spread 1-6%), the
                # os.path pair ran 5.29 us/job and this runs 5.71 us/job. The
                # Path() allocation costs more than the stat() it saves.
                #
                # Kept anyway, deliberately: 0.43 us lands only on the --reload
                # dev flag (the cached arm production actually runs measured
                # 0.485 us/job before and after), it is 0.012% of the per-job
                # budget, and docs/SCALE.md's "the check costs ~5.5 us" holds
                # for both. Consistency with the rest of the tree beats it.
                try:
                    return Path(source).stat().st_mtime
                except OSError:
                    return 0.0
            return 0.0

        if (
            self.reload_jobs
            and cached is not None
            and source_mtime() > self._class_mtimes.get(klassName, 0.0)
        ):
            logger.info(f"Reloading changed job module {module_name}")
            importlib.reload(sys.modules[module_name])
            cached = None

        if cached is None:
            located = pydoc.locate(klassName)
            if not located:
                raise FileNotFoundError(
                    f"Job class not found: {klassName}; search path: {sys.path}"
                )
            if not (isinstance(located, type) and issubclass(located, Job)):
                raise TypeError(
                    f"{klassName} is not a pyjobby Job subclass (got {located!r})"
                )
            cached = located
            self._class_cache[klassName] = cached
            # baseline AFTER importing: before the import the module is not
            # in sys.modules, so its mtime would read as 0 and the very next
            # lookup would look "changed"
            if self.reload_jobs:
                self._class_mtimes[klassName] = source_mtime()

        return cached

    def classForKlassFromName(
        self, klassName: str, job: dict[str, Any] | None = None
    ) -> Job:
        """Instantiate the job class named by a dotted path."""
        return self.resolve_job_class(klassName)(s=self, job=job or {})

    # ------------------------------------------------------------------
    # the main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self._start_web_listener()
        await self._connect_and_prepare()

        await self._register_worker()

        logger.info(f"[{self.qname}:{self.prio}] Connected and waiting for jobs!")
        prev: float = 0.0
        prev_status: float = time.perf_counter()
        prev_processed: int = 0
        sleepytime: bool = False  # skip initial sleep check
        try:
            while not self.stop:
                now: float = time.perf_counter()

                diff = now - prev
                if sleepytime and diff < self.checkInterval:
                    # Sleep until the poll interval elapses (with jitter so
                    # workers never poll in lockstep) or a NOTIFY says a job
                    # just entered our queue — whichever comes first.
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=self.checkInterval
                            - diff
                            + random.randint(0, 1000) / 1000,
                        )
                    # a shutdown may have been requested mid-sleep: never
                    # claim new work after stop is set
                    if self.stop:
                        continue

                    # orphan check: if our launcher process died (we were
                    # reparented), stop instead of polling headless forever
                    if self._launcher_pid and os.getppid() != self._launcher_pid:
                        logger.warning("Launcher process died; worker stopping")
                        self.stop = True
                        continue

                prev = time.perf_counter()

                # Log status every 5 minutes
                if now - prev_status >= 300:
                    rate = (self.processed - prev_processed) / (now - prev_status)
                    logger.info(
                        f"[processed {self.processed} ({rate:0.2f}/s)] "
                        f"[errors {self.errors}]"
                    )
                    prev_status = now
                    prev_processed = self.processed

                if self._too_many_abandoned_threads():
                    # Abandoned threads from timed-out synchronous jobs fill
                    # the pool; claiming now would admit a job that cannot
                    # start. Withdraw the wakeup demand too — a parked worker
                    # that will not claim must not be the reason an enqueue
                    # pays for a notification.
                    await self._set_idle(False)
                    sleepytime = True
                    continue

                claim_args = (
                    self.pid,
                    self.node,
                    self.qname,
                    self.capabilities,
                    self.prio,
                    self.worker_id,
                )
                jobs = await self.ex("claim", *claim_args)

                if not jobs and not self._idle:
                    # About to park. Publish the demand that switches this
                    # queue's enqueue notifications back on, and only THEN
                    # look again -- an enqueue that raced us either sees the
                    # flag and wakes us, or is already visible to this second
                    # claim. Doing it in the other order loses the wakeup.
                    await self._set_idle(True)
                    jobs = await self.ex("claim", *claim_args)

                if not jobs:
                    # nothing claimable: the one moment worth asking whether
                    # something is sitting just above our ceiling, unseen
                    await self._report_unclaimable_priorities()
                    sleepytime = True
                    continue

                # working again: withdraw the demand so enqueues stop paying
                # the notification (a no-op when we never parked)
                await self._set_idle(False)
                sleepytime = False
                # dict copy so kwargs can be augmented before running
                await self._process(dict(jobs[0]))
        finally:
            if self._web_runner is not None:
                with contextlib.suppress(Exception):
                    await self._web_runner.cleanup()  # release listen sockets
            await self._deregister_worker()
            if self.cxn is not None:
                await self.cxn.close()
            if self._threads is not None:
                # drop anything queued but not started; a thread already
                # running is abandoned work that nothing can interrupt, so
                # we do not wait for it — the process exit does.
                self._threads.shutdown(wait=False, cancel_futures=True)
                self._threads = None

    # ------------------------------------------------------------------
    # per-job orchestration
    # ------------------------------------------------------------------

    async def _process(self, job: dict[str, Any]) -> None:
        """Drive one claimed job through its lifecycle."""
        self.processed += 1
        jid: int = job["id"]
        epoch: int = job["run_epoch"]
        jname = job["job_class"].split(".")[-1]
        admin_data = job.get("admin_data") or {}
        klass: Job | None = None

        logger.info(
            "[job {}] Running {} ({}, {}, {})",
            jid,
            jname,
            job["job_class"],
            job["queue"],
            job["prio"],
        )

        self._current_job_id = jid
        self._cancel_current = False
        try:
            # run-time result passing: inject the upstream job's stored
            # result into kwargs before the task runs
            if admin_data.get("use_result_from"):
                upstream_id = admin_data["use_result_from"]
                upstream = await self.ex("get-result", upstream_id)
                if not upstream:
                    # The job this one was told to read is gone -- retention
                    # deleted it, or an operator did. Running anyway would
                    # silently produce a result computed WITHOUT the upstream
                    # input, which is a wrong answer rather than a failure.
                    raise LookupError(
                        f"job {jid} reads its input from job {upstream_id}, "
                        f"which no longer exists"
                    )
                if upstream[0]["state"] != "finished":
                    # Running without the input would silently produce a
                    # result computed WITHOUT it — the same wrong answer the
                    # missing-upstream raise above exists to prevent, so the
                    # not-yet case must not be quieter than the never case.
                    # The raise takes the ordinary retry path, so the reader
                    # retries with backoff until the upstream finishes (or
                    # the reader's budget runs out).
                    raise LookupError(
                        f"job {jid} reads its input from job {upstream_id}, "
                        f"which is {upstream[0]['state']!r}, not finished — "
                        f"enqueue the reader with waitfor_job={upstream_id} "
                        f"so it cannot start early"
                    )
                job["kwargs"] = {
                    **(job.get("kwargs") or {}),
                    "upstream_result": upstream[0]["result"],
                }

            klass = self.classForKlassFromName(job["job_class"], job=job)

            # DXE: bind previously recorded checkpoints so completed steps
            # fast-forward instead of re-executing on this attempt. A first
            # attempt provably has none — claim_jorb increments run_count,
            # so run_count == 1 means no execution ever preceded this one —
            # and skipping the load saves a round trip on the overwhelmingly
            # common path.
            if job.get("run_count", 0) > 1:
                checkpoints = await self.ex("load-steps", jid)
            else:
                checkpoints = []
            klass._dxe_bind(checkpoints, epoch)

            # timeout: admin_data override > class attribute > worker default
            job_timeout = admin_data.get("timeout_seconds")
            if job_timeout is None:
                # class attribute wins when set (0 disables the timeout),
                # otherwise the worker default applies
                job_timeout = (
                    self.default_timeout if klass.timeout is None else klass.timeout
                )

            # claimed -> running: records `started` and stamps the deadline
            # in the same write. Zero rows back means the row was requeued or
            # cancelled between claim and here — executing anyway would run
            # the job's side effects concurrently with the attempt that
            # replaced it, and a non-DXE job would never find out.
            started_rows = await self.ex(
                "run",
                jid,
                epoch,
                datetime.timedelta(seconds=job_timeout) if job_timeout else None,
            )
            if not started_rows:
                logger.warning(
                    f"[job {jid}] Superseded between claim and run; "
                    f"abandoning without executing"
                )
                return

            start_counter = time.perf_counter()
            # DXE: ONE deadline for this job. It is what _execute enforces and
            # the ceiling every per-step budget is measured against (see
            # Job._dxe_budget) — monotonic, in-process, set once.
            klass._dxe_deadline = (
                time.monotonic() + job_timeout if job_timeout else None
            )
            self._exec_task = asyncio.create_task(self._execute(klass, job_timeout))
            result = await self._exec_task

            elapsed_ms = (time.perf_counter() - start_counter) * 1000
            logger.info(f"[job {jid}] Completed {jname} in {elapsed_ms:.2f} ms")

            if admin_data.get("save_result") is False:
                result = None
            completed = await self.ex("finished", jid, result, epoch)
            if completed:
                if completed[0]["cancel_requested"]:
                    # the operator asked for a cancel and the task finished
                    # anyway: a synchronous task() has no await points for
                    # the cancellation to be delivered at. The job succeeded
                    # — but saying nothing is how "I cancelled that" and
                    # "it ran to completion" coexist as a mystery.
                    logger.warning(
                        f"[job {jid}] Completed DESPITE a pending cancel "
                        f"request (the task never yielded, so cancellation "
                        f"could not be delivered)"
                    )
                await self._wake_dependents(job)
            elif klass._dxe_rescheduled:
                # the task called reschedule(): the requeue won over normal
                # completion, by design — the row is already back in 'queued'
                logger.info(f"[job {jid}] Rescheduled itself; result discarded")
            else:
                # requeued/cancelled while we ran: the result was fenced out.
                # Said out loud because the work WAS done and its answer
                # dropped — silence here is how a lost result stays a mystery.
                logger.warning(
                    f"[job {jid}] Superseded before completion could be "
                    f"recorded; result discarded"
                )

        except dxe.DurableSleep as sleep:
            # the job checkpointed a sleep and rescheduled itself; nothing
            # terminal to record — it resumes past this point when claimed
            # again after wake_at
            logger.info(f"[job {jid}] Durable sleep until {sleep.wake_at}")

        except dxe.JobTimeout as expired:
            # the worker's own deadline, observed rather than inferred: only
            # _execute's scope raises this, so the operator's on_timeout
            # policy is applied to exactly the deadline they configured
            await self._handle_failure(
                job,
                klass,
                error=str(expired),
                backtrace="Timeout error - job exceeded maximum execution time",
                timed_out=True,
            )

        except dxe.StaleExecutionError:
            # a newer attempt owns the row (monitor/operator requeue while
            # we ran); abandon quietly — our writes were fenced out anyway
            logger.warning(f"[job {jid}] Superseded mid-run; abandoning stale attempt")

        except asyncio.CancelledError:
            if not self._cancel_current:
                raise  # the WORKER is being cancelled, not the job
            logger.warning(f"[job {jid}] Cancelled while running (operator request)")
            recorded = await self.ex("cancelled", jid, epoch)
            if not recorded:
                # a monitor requeue or dead-letter beat us to the row; the
                # cancel outcome belongs to whoever owns it now
                logger.warning(
                    f"[job {jid}] Cancel not recorded — row already moved on"
                )
            self.errors += 1

        except Exception as e:
            # Includes a bare TimeoutError from job code (a step's inner
            # deadline, an HTTP client). That is an ordinary failure: calling
            # it the job timeout would misname it and apply the operator's
            # on_timeout policy to a deadline they never configured.
            _, _, exc_traceback = sys.exc_info()
            logger.exception(
                "[job {}:{}] Error in {}: {}", jid, jname, job["job_class"], e
            )
            await self._handle_failure(
                job,
                klass,
                error=str(e),
                backtrace="Traceback:\n" + "".join(traceback.format_tb(exc_traceback)),
                timed_out=False,
            )
        finally:
            self._current_job_id = None
            self._exec_task = None

    async def _execute(self, klass: Job, job_timeout: float | None) -> Any:
        """Run the job's task under its ONE deadline.

        ``Job._dxe_deadline`` is the whole answer to "when does this job run
        out of time" — the same instant every per-step budget is measured
        against (see ``Job._dxe_budget``). ``job_timeout`` is carried here
        only to name the failure; the deadline is what enforces it.

        Resolving a job takes several stages — ``run()`` may be synchronous,
        what it returns may be a coroutine, and what *that* returns may be an
        async generator still to be drained — and every stage is job code on
        the job's clock. Bounding them separately gave each stage its own
        full timeout, so a job that spent real time staging ran for up to
        twice its configured ceiling and an async generator was drained with
        no ceiling at all. One scope around all of it, instead.

        Reaching the deadline raises ``dxe.JobTimeout``, which is the worker
        saying it observed the overrun; a ``TimeoutError`` from job code
        inside the scope is left alone as the ordinary failure it is.

        **A job cannot swallow its own deadline into a success.** Catching the
        cancellation and returning normally is inherited ``wait_for``
        behaviour, and it produced a *false success*: a stored result for an
        attempt the worker had already given up on, terminal on its own so the
        monitor's sweep could never see it either. So a normal return from an
        expired scope is refused and reported as the timeout it was, with the
        operator's ``on_timeout`` applied exactly as if the cancellation had
        propagated.

        The test is ``bounded.expired()`` — *did this scope's timer fire while
        the job was still inside it* — and never a clock read taken after the
        job finished. That distinction is what makes spurious timeouts
        impossible: ``__aexit__`` cancels the timer handler, so a job that
        returns even a microsecond before its deadline leaves the scope
        ENTERED and is a success no matter how long the worker then takes to
        record it. Only a job that was actually cancelled, and chose to
        continue anyway, can reach the refusal.

        An exception raised out of an expired scope is left alone: the job
        reported a failure and that failure is what gets recorded, with its
        own message and traceback. Relabelling it would also break the
        control-flow signals (``DurableSleep``, ``StaleExecutionError``) that
        legitimately unwind through this scope.
        """
        deadline = klass._dxe_deadline
        if deadline is None or not job_timeout:  # set together, absent together
            return await self._resolve(klass)

        try:
            async with asyncio.timeout(deadline - time.monotonic()) as bounded:
                result = await self._resolve(klass)
        except TimeoutError as expired:
            if not bounded.expired():
                raise  # job code's own deadline, not the job's
            raise dxe.JobTimeout(job_timeout) from expired

        if bounded.expired():
            # cancelled at the deadline, caught it, and returned anyway
            logger.warning(
                "[job {}] caught its own timeout cancellation and returned; "
                "recording the timeout instead of that result",
                klass.job.get("id"),
            )
            raise dxe.JobTimeout(job_timeout)
        return result

    async def _resolve(self, klass: Job) -> Any:
        """Reduce whatever ``run()`` produces to the job's stored result.

        ``run()`` goes to a thread because it may be synchronous and run the
        task to completion there: the event loop (and the timer enforcing the
        deadline) stays responsive either way, and an async task pays only
        for creating its coroutine off-loop. A cancelled sync job's thread
        keeps running to completion in the background — nothing can interrupt
        it — but its result is abandoned.

        The thread comes from this worker's own pool rather than the loop's
        default executor, and its future is kept so those abandoned threads
        can be counted: see ``_thread_pool`` and
        ``_too_many_abandoned_threads`` for what the worker does when they
        pile up, and ``_abandoned_job_threads`` for how it says so in the
        registry. The context copy is what ``asyncio.to_thread`` did for us."""
        thread = self._thread_pool().submit(contextvars.copy_context().run, klass.run)
        self._job_threads.append(thread)
        self._running_thread = thread
        try:
            staged = await asyncio.wrap_future(thread)
        finally:
            # Past here the thread is nobody's: it either returned, or this
            # scope was cancelled (the deadline) and the thread was abandoned
            # on the spot. Either way it stops counting as ours, which is what
            # _abandoned_job_threads publishes.
            self._running_thread = None
        if asyncio.iscoroutine(staged):
            staged = await staged
        if inspect.isasyncgen(staged):
            return [x async for x in staged]
        return staged

    async def _wake_dependents(self, job: dict[str, Any]) -> None:
        """Move jobs waiting on us (or on our whole group) into the queue."""
        jid = job["id"]
        woken = await self.ex("enqueue-next-self-finished", jid)
        if woken:
            logger.info(
                f"[job {jid}] Triggered scheduling of {[x['id'] for x in woken]}"
            )

        gid = job.get("run_group")
        if gid:
            woken = await self.ex("enqueue-next-if-peer-group-is-finished", gid)
            if woken:
                logger.info(
                    f"[job {jid}; group {gid:x}] Triggered scheduling of "
                    f"{[x['id'] for x in woken]}"
                )

    async def _handle_failure(
        self,
        job: dict[str, Any],
        klass: Job | None,
        *,
        error: str,
        backtrace: str,
        timed_out: bool,
    ) -> None:
        """One failure path for exceptions and timeouts: retry the SAME row
        with backoff, or mark it terminally 'crashed' (the DLQ)."""
        jid: int = job["id"]
        epoch: int = job["run_epoch"]
        admin_data = job.get("admin_data") or {}
        max_retries = admin_data.get("max_retries", self.max_retries)
        attempt = job.get("error_count", 0) + 1
        self.errors += 1

        retryable = attempt < max_retries and (
            not timed_out or admin_data.get("on_timeout", "retry") == "retry"
        )

        if retryable:
            # the class may have failed to load; the base Job knows how to
            # compute backoff from the row alone
            backoff_from = klass if klass is not None else Job(s=self, job=job)
            delay = await backoff_from.rescheduleBackoff(attempt)
            retried = await self.ex("retry", jid, delay, error, backtrace, epoch)
            if retried:
                logger.info(
                    "[job {}] Retrying in {:.1f}s (attempt {}/{}{})",
                    jid,
                    delay.total_seconds(),
                    attempt + 1,
                    max_retries,
                    ", timeout" if timed_out else "",
                )
            else:
                logger.warning(
                    f"[job {jid}] Superseded (epoch moved on); not retrying here"
                )
        else:
            crashed = await self.ex("crashed", jid, error, backtrace, epoch)
            if crashed:
                reason = (
                    "max retries exceeded"
                    if attempt >= max_retries
                    else "on_timeout=fail"
                )
                logger.error(
                    "[job {}] DEAD-LETTERED after {} attempts - {}",
                    jid,
                    attempt,
                    reason,
                )


#: Returned by ``Job._dxe_resume`` when there is no usable checkpoint and
#: the primitive must really execute. A sentinel rather than ``None``
#: because ``None`` is a perfectly good recorded step output.
_DXE_RUN: Final = object()


@dataclass
class Job:
    """Parent class of all jobs run by JobSystem.

    User jobs subclass Job and override the task() method. Inside task()
    the DXE (Durable Execution Engine) primitives are available:

        await self.step("name", fn, *args)   # checkpointed: never re-runs
                                             # once succeeded, even across
                                             # retries and worker crashes
        await self.transaction("name", fn)   # exactly-once: fn(conn) and
                                             # its checkpoint are ONE commit
        await self.step("slow", fn, timeout=30)   # per-step budget; blowing
                                             # it records a timeout against
                                             # THIS step and retries the job
        await self.sleep(3600)               # durable sleep: survives
                                             # restarts, resumes past here
        await self.set_event("progress", {"pct": 50})   # publish to waiters
        await self.send(other_job_id, {"go": True})     # durable message
        msg = await self.recv(timeout=60)               # await a message
        if self.cancelled: ...               # cooperative cancel check
    """

    s: JobSystem
    job: dict[str, Any]

    #: Per-class execution timeout in seconds. Subclasses may override:
    #: a number caps this job's runtime, 0 disables the timeout, and None
    #: (the default) defers to the worker's --default-timeout.
    timeout: ClassVar[int | None] = None

    #: Default per-step budget in seconds for every ``step()`` and
    #: ``transaction()`` this job runs. ``None`` (the default) means no
    #: per-step bound — only the job's own deadline applies. A single call
    #: overrides it with ``timeout=``; ``timeout=0`` disables it for that
    #: call, matching the "0 disables" convention of ``timeout`` above.
    step_timeout: ClassVar[float | None] = None

    #: Set by the @job decorator when a class is registered.
    job_class_path: ClassVar[str] = ""

    # --- DXE state (bound by the worker before execution; declared so the
    # attributes always exist and are type-checked) ---
    _dxe_steps: dict[int, Any] = field(default_factory=dict)
    _dxe_seq: int = 0
    _dxe_epoch: int = 0
    #: monotonic instant this job's own timeout fires, or None when the job
    #: has no deadline. Set by the worker; a Job built outside one has no
    #: ceiling and its declared step budgets apply as declared.
    _dxe_deadline: float | None = None
    #: True once this execution successfully requeued its own row
    #: (reschedule() / durable sleep). The worker reads it to tell "my
    #: completion no-oped because the reschedule won, by design" apart from
    #: "my completion no-oped because I was superseded" — the first is
    #: routine, the second deserves a warning.
    _dxe_rescheduled: bool = False

    def __post_init__(self) -> None:
        # a Job constructed outside the worker (tests, direct use) still has
        # a coherent epoch to fence its checkpoint writes on
        if isinstance(self.job, dict):
            self._dxe_epoch = self.job.get("run_epoch", 0)

    # @abc.abstractmethod # can't use with @dataclass
    def task(self, *args: Any, **kwargs: Any) -> Any:
        """User-implemented task definition.

        To run async tasks, return a coroutine.
        To run an async generator, return an async generator.
        To run a regular method, just return the result directly."""
        raise NotImplementedError("Subclass must define a concrete task runner!")

    def run(self) -> Any:
        """Call subclass .task() with arguments from DB

        Subclasses can override 'run' if it needs to be async."""
        return self.task(**self.job["kwargs"])

    @classmethod
    async def web(cls, request: web.Request) -> web.Response:
        """Handle a direct HTTP invocation of this job class.

        Opt-in extension point for the per-worker HTTP listener
        (``web_listen`` in the config): a job class listed under
        ``web_listen["paths"]`` is reachable at ``/<dotted.class.Name>`` and
        must override this to serve the request. Jobs that do not override
        it return 501, which is why this is a real method rather than an
        assumption the handler makes about arbitrary classes.
        """
        return web.Response(
            status=501,
            text=f"{cls.__module__}.{cls.__qualname__} does not implement web()",
        )

    # ------------------------------------------------------------------
    # DXE: durable execution primitives
    # ------------------------------------------------------------------

    def _dxe_bind(self, checkpoints: list[Any], epoch: int) -> None:
        """Called by the worker before execution: attach recorded
        checkpoints and this attempt's fencing epoch."""
        self._dxe_steps = {row["step_seq"]: row for row in checkpoints}
        self._dxe_seq = 0
        self._dxe_epoch = epoch

    def _dxe_next_seq(self) -> int:
        self._dxe_seq += 1
        return self._dxe_seq

    @property
    def cancelled(self) -> bool:
        """True once cancellation of this job has been requested (poll this
        from long sync loops; async code is cancelled at await points)."""
        return self.s._cancel_current

    def _dxe_resume(self, name: str) -> tuple[int, Any]:
        """Consume the next sequence number and make the replay decision.

        Returns ``(seq, output)``: *output* is the recorded result to hand
        back **without executing**, or the ``_DXE_RUN`` sentinel when this
        call has to really execute (no checkpoint, or a recorded failure —
        a failed step is not a result). A name mismatch raises
        NondeterminismError before anything runs.

        Every checkpointed primitive that can execute user code goes
        through here, so ``step()`` and ``transaction()`` cannot drift
        apart in their replay behavior."""
        seq = self._dxe_next_seq()
        prior = self._dxe_steps.get(seq)
        if prior is not None:
            if prior["name"] != name:
                raise dxe.NondeterminismError(
                    f"step {seq} was '{prior['name']}' on a previous attempt "
                    f"but is '{name}' now — job code must be deterministic "
                    f"outside steps"
                )
            if prior["error"] is None:
                logger.debug(f"[job {self.job['id']}] step {seq} '{name}' replayed")
                return seq, prior["output"]
            # recorded failure: fall through and re-execute this step
        return seq, _DXE_RUN

    def _dxe_budget(self, timeout: float | None) -> float | None:
        """Resolve this call's per-step budget, or None for "unbounded".

        Precedence is per-call > class default > none, with ``0`` disabling
        the budget for a call (the same convention as the job-level
        ``timeout`` class attribute).

        **The job's deadline is a ceiling, and only the tighter of the two
        bounds is ever armed.** A per-step budget is installed only while it
        is strictly tighter than the time the job has left; once the job's
        own deadline is the binding constraint, that deadline is left to fire
        alone. So a step timeout can never outlive the job's deadline, the
        job timeout still fires however the work is split into steps, and the
        two can never race to report the same overrun as two different
        failures."""
        budget = self.step_timeout if timeout is None else timeout
        if not budget:  # None or 0: no per-step bound
            return None
        if self._dxe_deadline is not None:
            remaining = self._dxe_deadline - time.monotonic()
            if remaining <= budget:
                logger.warning(
                    "[job {}] step budget {:g}s exceeds the {:.1f}s this job "
                    "has left; the job timeout is the binding deadline",
                    self.job["id"],
                    budget,
                    remaining,
                )
                return None
        return budget

    async def _dxe_invoke(
        self,
        name: str,
        fn: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float | None,
    ) -> Any:
        """Call ``fn`` under its per-step budget — the one place both
        primitives execute user code, so their timeout behavior cannot drift.

        Raises ``StepTimeoutError`` when the budget expires; the caller
        records that as the step's error and lets it take the job's ordinary
        retry path.

        The budget scopes **only** ``fn``. The checkpoint write happens after
        this returns, outside the cancel scope, because a cancellation
        delivered into the checkpoint write would abandon the observability
        it exists to provide — and, in ``transaction()``, would fire inside
        an open transaction.

        Interruption is real for a coroutine and impossible for a blocking
        synchronous ``fn``: cancellation is delivered at an await point, and
        a function that never yields to the event loop has none. A sync
        ``fn`` that overruns therefore runs to completion and, if it
        succeeded, is recorded as a success — the overrun is logged, not
        invented into a failure. ``self.cancelled`` remains pollable from such
        a loop, but it reports an *operator* cancel request, not an expiring
        budget — a synchronous loop that wants to bound itself must watch its
        own clock.

        A coroutine that *catches* its budget's cancellation and returns
        anyway does not get that value checkpointed as a completed step: the
        same refusal the job's own deadline makes in ``_execute``, for the
        same reason. It is keyed on this scope's timer having fired, not on a
        clock read, so a step that finishes just inside its budget is
        untouched — and a blocking sync ``fn`` never trips it at all, because
        it starves the timer it would have to have fired."""
        budget = self._dxe_budget(timeout)
        began = time.monotonic()

        if budget is None:
            result = fn(*args, **kwargs)
            return await result if asyncio.iscoroutine(result) else result

        try:
            async with asyncio.timeout(budget) as bounded:
                result = fn(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
        except TimeoutError as expired:
            if not bounded.expired():
                raise  # fn's OWN timeout, not this step's budget: don't relabel
            raise dxe.StepTimeoutError(name, budget) from expired

        if bounded.expired():
            # fn caught this budget's cancellation and returned anyway
            logger.warning(
                "[job {}] step '{}' caught its own budget's cancellation and "
                "returned; recording the step timeout instead of that result",
                self.job["id"],
                name,
            )
            raise dxe.StepTimeoutError(name, budget)

        elapsed = time.monotonic() - began
        if elapsed > budget:
            # only reachable when fn blocked the event loop: the timer never
            # got to run, so nothing could interrupt it
            logger.warning(
                "[job {}] synchronous step '{}' ran {:.3f}s over its {:g}s "
                "budget; a blocking step cannot be interrupted",
                self.job["id"],
                name,
                elapsed - budget,
                budget,
            )
        return result

    async def _dxe_record(
        self,
        seq: int,
        name: str,
        output: Any,
        error: str | None,
        started: datetime.datetime,
        atomic: bool = False,
    ) -> None:
        """Write this step's checkpoint, fenced on our run_epoch.

        Runs on the worker's connection, so it joins whatever transaction
        that connection currently holds — which is exactly what makes
        ``transaction()`` atomic. Raises StaleExecutionError when the fence
        rejects the write (a newer attempt owns the job).

        ``atomic=True`` bypasses ``JobSystem.ex``: that wrapper transparently
        reconnects on a lost connection, and a reconnect inside a
        transaction would commit the checkpoint on a NEW connection while
        the server rolled the application write back — a checkpoint for work
        that no longer exists, which is the precise failure this primitive
        exists to prevent. Inside a transaction the connection error must
        propagate instead."""
        args = (
            self.job["id"],
            seq,
            name,
            output,
            error,
            self._dxe_epoch,
            started,
        )
        recorded = (
            await self.s.stmts["record-step"].fetch(*args)
            if atomic
            else await self.s.ex("record-step", *args)
        )
        if not recorded:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )

    async def step(
        self,
        name: str,
        fn: Any,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute ``fn(*args, **kwargs)`` as a durable, checkpointed step.

        On success the return value (must be JSON-serializable) is recorded;
        any later attempt of this job returns the recorded value without
        re-executing. On failure the error is recorded for observability
        and the exception propagates into the job's normal retry path — the
        next attempt fast-forwards every completed step and re-executes
        only from the failure onward.

        ``timeout`` bounds this one step in seconds (falling back to the
        class's ``step_timeout``; ``0`` disables it for this call). Blowing
        the budget raises ``StepTimeoutError``, which is recorded as this
        step's error — ``pj-admin jobs steps`` then names the step that hung
        and says it was a timeout — and then takes the same retry path as any
        other step failure. See ``_dxe_invoke`` for what "timeout" can and
        cannot mean for a *synchronous* ``fn``, and ``_dxe_budget`` for how a
        step budget composes with the job's own deadline.

        ``timeout`` is consumed here rather than forwarded, so a function
        that wants its own ``timeout=`` keyword must be bound to it first
        (``functools.partial(fn, timeout=5)``).

        **At-least-once**: the effect and the checkpoint commit separately,
        so a crash between them re-executes ``fn`` on the next attempt.
        Make external effects idempotent — or, when the effect is a write to
        *this* database, use ``transaction()`` and get exactly-once.
        """
        seq, replayed = self._dxe_resume(name)
        if replayed is not _DXE_RUN:
            return replayed

        started = db.utcnow()
        try:
            result = await self._dxe_invoke(name, fn, args, kwargs, timeout)
        except dxe.DXEError:
            raise
        except Exception as e:
            try:
                await self._dxe_record(
                    seq, name, None, f"{type(e).__name__}: {e}", started
                )
            except dxe.StaleExecutionError as stale:
                raise stale from e
            raise

        await self._dxe_record(seq, name, result, None, started)
        return result

    async def transaction(
        self,
        name: str,
        fn: Any,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute ``fn(conn, *args, **kwargs)`` **exactly once**: the work
        and its checkpoint commit or roll back together.

        Identical to ``step()`` in every replay respect — same sequence
        numbering, same fast-forward of a completed checkpoint, same
        re-execution of a recorded failure, same NondeterminismError on a
        name mismatch, same ``timeout`` budget with the same
        ``StepTimeoutError`` recorded against the step (they share
        ``_dxe_resume`` and ``_dxe_invoke``). The difference is
        atomicity: ``fn`` is handed the worker's own connection inside an
        explicit transaction, and the checkpoint is written on that same
        connection before the commit. There is no window between the effect
        and the checkpoint for a crash to fall into.

        The epoch fence and exactly-once are the same mechanism here: the
        checkpoint insert is conditional on this execution still owning the
        job, so a superseded attempt's checkpoint matches zero rows, raises
        StaleExecutionError *inside* the transaction, and takes the
        application write down with it. A zombie worker cannot commit
        application data for a job another worker has taken over.

        Failure path: when ``fn`` raises, the transaction (including the
        checkpoint) is rolled back, and the error checkpoint is then
        recorded in a **separate** transaction — observability must survive
        the rollback that erased the work. A blown ``timeout`` is that same
        path: the budget expires *inside* the transaction, so the
        cancellation aborts whatever query is in flight and the raise rolls
        the application write back on the way out. The connection is left
        clean and idle, never mid-transaction.

        Caveats, because this guarantee is exactly as wide as the
        connection it runs on:

        * **Use the connection you are handed.** Anything ``fn`` does on a
          different connection (another pool, an HTTP call, a file) is
          outside the transaction and is *not* rolled back with it — that
          work is at-least-once, exactly like ``step()``. This cannot be
          prevented, only documented: the connection is passed in, so a
          function that ignores it silently loses the guarantee.
        * Do not commit, roll back, or close the connection inside ``fn``.
        * If the worker's connection already holds a transaction, asyncpg
          opens a savepoint instead; the write and the checkpoint still
          stand or fall together, they just commit with the enclosing
          transaction.
        """
        seq, replayed = self._dxe_resume(name)
        if replayed is not _DXE_RUN:
            return replayed

        started = db.utcnow()
        try:
            async with self.s.cxn.transaction():  # type: ignore[union-attr]
                result = await self._dxe_invoke(
                    name, fn, (self.s.cxn, *args), kwargs, timeout
                )
                # inside the transaction: a fenced-out checkpoint raises,
                # and the rollback undoes the application write with it
                await self._dxe_record(seq, name, result, None, started, atomic=True)
        except dxe.DXEError:
            raise
        except Exception as e:
            # the application transaction has rolled back, so the error
            # checkpoint needs a transaction of its own to survive
            try:
                await self._dxe_record(
                    seq, name, None, f"{type(e).__name__}: {e}", started
                )
            except dxe.StaleExecutionError as stale:
                raise stale from e
            raise
        return result

    async def sleep(self, seconds: float) -> None:
        """Durable sleep: checkpoint a wake time, reschedule this job for
        it, and unwind. When the job is claimed again after the wake time,
        execution fast-forwards straight past this sleep.

        Survives worker restarts and host failures — the sleep lives in the
        database, not in a process."""
        seq = self._dxe_next_seq()
        prior = self._dxe_steps.get(seq)
        name = "dxe.sleep"

        if prior is not None:
            if prior["name"] != name:
                raise dxe.NondeterminismError(
                    f"step {seq} was '{prior['name']}' on a previous attempt "
                    f"but is a sleep now"
                )
            wake_at = datetime.datetime.fromisoformat(prior["output"]["wake_at"])
            # measured against the DATABASE clock — the same clock the
            # reschedule's run_after gate uses, so a skewed worker cannot
            # wake "early" and re-sleep an extra round forever
            db_now = (await self.s.ex("now"))[0]["now"]
            remaining = (wake_at - db_now).total_seconds()
            if remaining <= 0:
                return  # slept enough on a previous attempt; continue
            # woken early (operator requeue): go back to sleep for the rest
            await self._reschedule(datetime.timedelta(seconds=remaining))
            raise dxe.DurableSleep(wake_at)

        db_now = (await self.s.ex("now"))[0]["now"]
        wake_at = db_now + datetime.timedelta(seconds=seconds)
        recorded = await self.s.ex(
            "record-step",
            self.job["id"],
            seq,
            name,
            {"wake_at": wake_at.isoformat()},
            None,
            self._dxe_epoch,
            db.utcnow(),
        )
        if not recorded:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
        await self._reschedule(datetime.timedelta(seconds=seconds))
        raise dxe.DurableSleep(wake_at)

    async def compact(self) -> bool:
        """Discard this job's checkpoint log and restart its step sequence.

        Replay costs about 0.9 us and 260 bytes per recorded step, linearly
        (``pj-bench replay``). A job whose step count tracks *work done* never
        notices. A job whose step count tracks *elapsed time* — a state
        machine that wakes, finds no mail, sleeps, and repeats — pays a little
        more on every wake, forever. This is what bounds that: after
        compaction the log is empty and the next ``step()`` is sequence 1
        again, so a machine's replay cost is one turn's worth of checkpoints
        rather than its whole life's.

        **The contract you take on by calling this.** The checkpoint log is
        what stops completed work re-running after a crash. Throwing it away
        is only safe where your code can re-derive its position from durable
        state it wrote itself — a machine that reads its own
        ``set_event("machine.state")`` at entry can; a linear ``task()`` that
        relies on replay to skip the first nine of ten steps cannot, and
        calling this there means step one runs twice.

        Returns False and does nothing while a previous attempt's log is
        still being replayed, because compacting mid-replay would delete
        checkpoints this attempt has not yet caught up to and silently
        re-execute them. That makes the safe call site a loop boundary: call
        it each time round, and it takes effect on the first pass that owes
        nothing to a previous attempt.

        Fenced like every other durable write: a superseded execution cannot
        delete a live one's checkpoints.
        """
        if self._dxe_steps and self._dxe_seq < max(self._dxe_steps):
            return False
        rows = await self.s.ex("compact-steps", self.job["id"], self._dxe_epoch)
        if not rows or not rows[0]["fenced"]:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
        removed = int(rows[0]["removed"])
        self._dxe_steps.clear()
        self._dxe_seq = 0
        if removed:
            logger.debug(f"[job {self.job['id']}] compacted {removed} checkpoints")
        return True

    async def _reschedule(self, interval: datetime.timedelta) -> None:
        """Requeue this job for a future run, fenced to THIS attempt.

        Raises StaleExecutionError if this execution has been superseded,
        so a stale attempt can neither requeue a live one nor keep running.
        """
        applied = await self.s.ex(
            "reschedule", self.job["id"], interval, self._dxe_epoch
        )
        if not applied:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
        self._dxe_rescheduled = True

    async def set_event(self, key: str, value: Any) -> None:
        """Publish a key/value event on this job (idempotent upsert).
        Clients and other jobs read it with get_event; waiters are woken
        via NOTIFY."""
        applied = await self.s.ex(
            "set-event", self.job["id"], key, value, self._dxe_epoch
        )
        if not applied:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )

    async def get_event(self, key: str, job_id: int | None = None) -> Any | None:
        """Read a published event — this job's by default, another job's by
        id. Returns None when the key has never been set.

        Deliberately NOT a checkpointed step. An event is durable state, so
        reading it is a query rather than an effect, and recording the answer
        would freeze the first value read into every later replay — which is
        the opposite of what a caller re-deriving its position after a crash
        wants. The value returned is the one committed now.
        """
        rows = await self.s.ex("get-event", job_id or self.job["id"], key)
        return rows[0]["value"] if rows else None

    async def send(
        self, dest_job_id: int, message: Any, topic: str | None = None
    ) -> None:
        """Send a durable message to another job's mailbox — exactly once
        across retries.

        The mailbox insert is a write to this same database, which is
        precisely the case ``transaction()`` exists for: the insert and its
        checkpoint commit together, so there is no crash window in which the
        message was delivered but unrecorded (a retry would re-send) or
        recorded but undelivered. The insert is additionally fenced on the
        SENDER's epoch, so a superseded execution delivers nothing."""

        async def _do_send(conn: asyncpg.Connection) -> int:
            rows = await conn.fetch(
                dxe.SEND_SQL,
                dest_job_id,
                topic,
                message,
                self.job["id"],
                self._dxe_epoch,
            )
            if not rows:
                raise dxe.StaleExecutionError(
                    f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
                )
            return int(rows[0]["id"])

        await self.transaction(f"dxe.send:{dest_job_id}:{topic or ''}", _do_send)

    async def recv(
        self,
        topic: str | None = None,
        timeout: float = 60,
        poll_interval: float = 0.25,
    ) -> Any | None:
        """Await one durable message for this job (oldest first), or None on
        timeout. Consuming a message and checkpointing it are ONE statement,
        so there is no crash window in which a message was consumed but not
        recorded: a retry of this job either replays the recorded message or
        finds it still pending — never neither. The consume is fenced on this
        execution's epoch, so a superseded attempt cannot eat a message the
        live attempt is entitled to.

        A recv that times out records None as this call site's answer; on
        replay that recorded None comes back without waiting again."""
        seq = self._dxe_next_seq()
        prior = self._dxe_steps.get(seq)
        name = f"dxe.recv:{topic or ''}"

        if prior is not None:
            if prior["name"] != name:
                raise dxe.NondeterminismError(
                    f"step {seq} was '{prior['name']}' on a previous attempt "
                    f"but is a recv now"
                )
            if prior["error"] is None:
                return prior["output"]

        started = db.utcnow()
        # the wait budget is a local duration, so it runs on the monotonic
        # clock like every other budget in the engine — wall time can jump
        deadline = time.monotonic() + timeout
        while True:
            row = (
                await self.s.ex(
                    "recv",
                    self.job["id"],
                    seq,
                    topic,
                    name,
                    self._dxe_epoch,
                    started,
                )
            )[0]
            if not row["fenced"]:
                raise dxe.StaleExecutionError(
                    f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
                )
            if row["replayed"]:
                # this seq already holds a committed answer (a lost
                # connection raced an earlier commit of this very call);
                # nothing was consumed just now
                return row["prior_output"]
            if row["consumed"]:
                return row["message"]
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval)

        # Timed out with nothing pending: record None as this call site's
        # durable answer. There is no effect to pair with, so the plain
        # (fenced, idempotent) checkpoint write suffices.
        recorded = await self.s.ex(
            "record-step",
            self.job["id"],
            seq,
            name,
            None,
            None,
            self._dxe_epoch,
            started,
        )
        if not recorded:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
        return None

    async def rescheduleBackoff(self, attempt: int | None = None) -> datetime.timedelta:
        """Calculate this job's retry delay from its admin_data strategy.

        Args:
            attempt: which attempt to compute the delay for; defaults to the
                job's current error_count.

        Strategies: exponential (default), linear, fibonacci, fixed.
        Subclasses may override to implement custom backoff policy.

        NOTE: This only CALCULATES the delay; it does not touch the
        database."""
        from .retry_strategies import calculate_retry_from_job

        if attempt is None:
            attempt = self.job.get("error_count", 0)

        return calculate_retry_from_job(self.job, attempt)

    async def reschedule(
        self,
        relative: int,
        unit: str = "seconds",
        deltas: dict[str, int] | None = None,  # or provide units as key=interval
    ) -> datetime.timedelta:
        """Schedule this job to run again [relative] [unit] into the future.

        A job that reschedules itself stays 'queued' for the future run —
        the reschedule wins over normal completion.

        Units are from timedelta:
            "microseconds milliseconds seconds minutes hours days weeks"

        Provide `deltas` for aggregate multi-level offsets
        (e.g. {"days": 5, "hours": 3}).

        Note: the re-schedule is from NOW, not from the original run time.
        """
        if not deltas:
            deltas = {unit: relative}

        ds = {str(u): r for u, r in deltas.items()}

        interval = datetime.timedelta(**ds)
        await self._reschedule(interval)

        return interval


def runAndDone(
    qname: str,
    caps: tuple[str],
    n: int,
    db_params: dict[str, str],
    web_listen: dict[str, Any] | None,
    max_retries: int = 10,
    default_timeout: int = 3600,
    check_interval: float = 5,
    reload_jobs: bool = False,
    job_threads: int = 8,
    max_prio: int = DEFAULT_PRIO_CEILING,
) -> None:
    """Run the JobSystem for this worker process.

    ``max_prio`` is this worker's priority ceiling. It is passed explicitly
    because it used to be dropped here: whatever `pj` was told, every worker
    it launched ran at the dataclass default, so no operator could run a
    worker for low-urgency work at all."""
    configure_worker_logging()
    # our parent right now IS the launcher; if it dies we should too
    launcher_pid = os.getppid()
    runner = JobSystem(
        dsn=db_params,
        qname=qname,
        capabilities=caps,
        workerId=n,
        checkInterval=check_interval,
        webPort=web_listen,
        prio=max_prio,
        max_retries=max_retries,
        default_timeout=default_timeout,
        reload_jobs=reload_jobs,
        job_threads=job_threads,
        _launcher_pid=launcher_pid,
    )

    signal.signal(signal.SIGTERM, runner.shutdown)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        return
    except Exception:
        logger.exception("what went wrong now?")


@click.command()
@click.option(
    "--queue",
    default=["default"],
    multiple=True,
    help="Queue to process. Repeatable: EVERY named queue gets its own full "
    "set of --workers processes (2 queues x --workers 4 = 8 processes). "
    "Repeating the same name changes nothing. No worker is ever started on "
    "a queue you did not name.",
    show_default=True,
)
@click.option(
    "--cap",
    default=[],
    multiple=True,
    help="Capabilities for this server",
    show_default=True,
)
@click.option(
    "--workers",
    default=(os.cpu_count() or 2) // 2,
    help="Worker processes to start PER --queue (total = this x the number "
    "of distinct queues named)",
    show_default=True,
)
@click.option(
    "--max-prio",
    default=None,
    type=int,
    help="Priority CEILING for these workers: they claim jobs whose prio is "
    "<= this and are blind to everything above it. LOWER prio is MORE "
    "urgent, so raising this makes a worker take LESS urgent work as well; "
    "a job above every worker's ceiling is never claimed at all. Defaults "
    "to the config file's prio_ceiling, else 1000",
)
@click.option(
    "--path",
    default=["."],
    multiple=True,
    help="extra job class paths (can be multiple)",
    show_default=True,
)
@click.option(
    "--max-retries",
    default=10,
    help="Maximum attempts before a job is dead-lettered (state 'crashed')",
    show_default=True,
)
@click.option(
    "--default-timeout",
    default=3600,
    help="Default job timeout in seconds (1 hour)",
    show_default=True,
)
@click.option(
    "--check-interval",
    default=5.0,
    help="Seconds between job polls when idle (LISTEN/NOTIFY wakes workers sooner)",
    show_default=True,
)
@click.option(
    "--job-threads",
    default=8,
    help="Size of this worker's job-thread pool, and so how many ABANDONED "
    "threads (from timed-out synchronous jobs, which cannot be stopped) it "
    "tolerates before it stops claiming and logs that it is doing so",
    show_default=True,
)
@click.option(
    "--reload",
    "reload_jobs",
    is_flag=True,
    help="re-import a job's module when its source changes (development "
    "loop; off by default so production never re-executes module code "
    "on every job)",
)
@click.option(
    "-v",
    is_flag=True,
    help="show version then exit",
)
@click.option(
    "--config",
    "-c",
    default="./pyjobby.toml",
    help="config file path",
    show_default=True,
)
def workit(
    queue: tuple[str],
    cap: tuple[str],
    workers: int,
    max_prio: int | None,
    path: str,
    max_retries: int,
    default_timeout: int,
    check_interval: float,
    job_threads: int,
    reload_jobs: bool,
    v: bool,
    config: str,
) -> None:
    """Launch a fleet of workers: --workers processes on EACH --queue.

    `--workers` is PER QUEUE: naming another queue never changes the
    capacity of the queues already named, and a worker is never started on
    a queue the operator did not name. Repeating a queue name asks for
    nothing extra."""
    from pyjobby import __version__ as localver

    if v:
        print(localver)
        sys.exit(0)

    configure_worker_logging()

    if not Path(config).is_file():
        logger.error("Config file not found! Requested: {}", config)
        sys.exit(1)

    try:
        loadedConfig = load_config_from_file(
            config, {"db_params", "web_listen", "prio_ceiling"}
        )
    except RuntimeError as e:
        logger.error("Failed to load config {}: {}", config, e)
        sys.exit(1)

    # ceiling precedence: explicit flag > config file's prio_ceiling >
    # platform default — ONE config key, so a fleet does not repeat the same
    # number on four command lines (and forget one)
    if max_prio is None:
        max_prio = loadedConfig.get("prio_ceiling") or DEFAULT_PRIO_CEILING

    if not loadedConfig.get("db_params"):
        logger.error("No db_params found in config: {}", config)
        sys.exit(1)

    # One full set of workers per DISTINCT named queue. Duplicates collapse
    # (asking for the same queue twice asks for the same set twice), which
    # also means `--queue Q --queue Q --workers 2` is two workers on Q
    # rather than four.
    queues = list(dict.fromkeys(queue))
    lcap = list(cap)

    # capability includes this hostname specification by default
    lcap.append(f"host:{platform.node()}")

    for pth in path:
        # Also use requested directories as paths for job class lookups
        sys.path.append(pth)

    logger.info(
        "[{}] Launching {} worker(s) on each of {} queue(s) [{}] at priority "
        "ceiling {}: {} processes",
        localver,
        workers,
        len(queues),
        ", ".join(queues),
        max_prio,
        workers * len(queues),
    )
    launched = set()
    # worker ids are unique across the whole fleet, not per queue: they name
    # this process's per-worker web listen socket (see _start_web_listener)
    fleet = [q for q in queues for _ in range(workers)]
    for idx, q in enumerate(fleet):
        p = Process(
            target=runAndDone,
            args=(
                q,
                tuple(lcap),
                idx,
                loadedConfig["db_params"],
                # absent key = no web listener (TOML has no null; the loader
                # simply omits keys the file does not define)
                loadedConfig.get("web_listen"),
            ),
            kwargs={
                "max_retries": max_retries,
                "default_timeout": default_timeout,
                "check_interval": check_interval,
                "reload_jobs": reload_jobs,
                "job_threads": job_threads,
                "max_prio": max_prio,
            },
        )
        p.start()
        launched.add(p)
        logger.info(f"[{p.pid}] Launched on queue {q}...")

        # random delay before launching next worker so job checks are
        # staggered instead of bunching at the same start microsecond.
        time.sleep(random.uniform(0.001, 0.010))

    def signalBroadcast(signum: int, frame: Any) -> None:
        """Forward main interrupt to every child, including after one is gone.

        The suppression is PER CHILD, not around the loop. A worker that has
        already exited raises ProcessLookupError from os.kill, and catching
        that outside the loop abandoned the broadcast there -- so one dead
        child silenced the signal for every worker after it, and the survivors
        only noticed at their next orphan check, up to a poll interval later.
        """
        for p in launched:
            if p.pid is None:  # never started, or already reaped
                continue
            with contextlib.suppress(OSError):
                os.kill(p.pid, signum)

    signal.signal(signal.SIGTERM, signalBroadcast)
    for l in launched:
        with contextlib.suppress(KeyboardInterrupt):
            l.join()

    # success!
    sys.exit(0)


if __name__ == "__main__":
    workit()
