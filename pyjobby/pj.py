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
  arrives; polling remains the fallback for run_after-delayed jobs.
* Cancellation of running jobs: operators set cancel_requested, the
  jorb_cancel NOTIFY reaches the executing worker, and the job's task is
  cancelled at the next await point.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from dataclasses import dataclass, field
from multiprocessing import Process
from typing import Any, ClassVar

import asyncpg  # type: ignore[import-untyped]
import click
from aiohttp import web
from loguru import logger

from . import db, dxe
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
logger = logger.patch(cleanupLogLengths)  # type: ignore


STMTS: dict[str, str] = {}

# Claim the single most-urgent runnable job in our queue, honoring the
# jorb_queue control plane (absent row = unpaused / unlimited):
#   * paused queues yield nothing
#   * max_concurrency caps claimed+running rows for the queue
#   * rate_limit caps executions STARTED within the trailing rate period
# run_epoch increments on every claim: it is the fencing token that keeps a
# superseded execution from writing results/checkpoints later.
STMTS["claim"] = """UPDATE jorb
              SET state = 'claimed',
                  worker_pid = $1,
                  worker_host = $2,
                  claimed_by = $6,
                  run_count = run_count + 1,
                  run_epoch = run_epoch + 1,
                  updated = now()
              WHERE id = (
                 SELECT j.id FROM jorb j
                 LEFT JOIN jorb_queue q ON q.name = j.queue
                 WHERE j.queue = $3
                     AND NOT COALESCE(q.paused, FALSE)
                     AND (q.max_concurrency IS NULL OR q.max_concurrency > (
                          SELECT count(*) FROM jorb c
                          WHERE c.queue = $3 AND c.state IN ('claimed', 'running')))
                     AND (q.rate_limit IS NULL OR q.rate_limit > (
                          SELECT count(*) FROM jorb s
                          WHERE s.queue = $3
                            AND s.started > now() - make_interval(secs => q.rate_period_seconds)))
                     AND (j.capability = ANY($4::text[]) OR j.capability IS NULL)
                     AND j.prio <= $5
                     AND j.run_after <= now()
                     AND j.state = 'queued'
                 ORDER BY j.prio, j.run_after
                 FOR UPDATE OF j SKIP LOCKED
                 LIMIT 1
              )
              RETURNING *"""

STMTS["get"] = """SELECT * FROM jorb
                     WHERE id = $1
                        AND state = 'claimed'"""

# Fetch an upstream job's stored result for run-time result passing
# (admin_data.use_result_from).
STMTS["get-result"] = """SELECT state, result FROM jorb WHERE id = $1"""

# claimed -> running (records `started`; timeout enforcement, duration
# metrics, and rate limiting all key off this transition)
STMTS["run"] = """UPDATE jorb
              SET state = 'running',
                  started = now(),
                  updated = now()
              WHERE id = $1
                AND state = 'claimed'
                AND run_epoch = $2
          RETURNING *"""

STMTS["set-timeout"] = """UPDATE jorb
              SET timeout_at = now() + $2::interval
              WHERE id = $1
                AND run_epoch = $3"""

# Terminal success. Epoch-fenced: if the reaper or an operator requeued this
# job while we ran, our (stale) completion is a no-op.
STMTS["finished"] = """UPDATE jorb
              SET state = 'finished',
                  result = $2,
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $3
          RETURNING *"""

# Same-row retry: back into the queue with backoff; jorb_history holds the
# per-attempt audit trail (recorded by trigger on the state change).
STMTS["retry"] = """UPDATE jorb
              SET state = 'queued',
                  run_after = now() + $2::interval,
                  error_message = $3,
                  error_backtrace = $4,
                  error_count = error_count + 1,
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $5
          RETURNING *"""

# Terminal failure: retries exhausted (or on_timeout='fail'). state='crashed'
# IS the dead letter queue.
STMTS["crashed"] = """UPDATE jorb
              SET state = 'crashed',
                  error_message = $2,
                  error_backtrace = $3,
                  error_count = error_count + 1,
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $4
          RETURNING *"""

# A running job whose cancellation was requested and honored.
STMTS["cancelled"] = """UPDATE jorb
              SET state = 'cancelled',
                  finished = now(),
                  timeout_at = NULL,
                  updated = now()
              WHERE id = $1
                AND state IN ('claimed', 'running')
                AND run_epoch = $2
          RETURNING *"""

# Job.reschedule(): the task asked to run again later; wins over completion.
STMTS["reschedule"] = """UPDATE jorb
              SET state = 'queued',
                  run_after = now() + $2::interval,
                  updated = now()
              WHERE id = $1"""

# Wake jobs waiting on a group: when ZERO jobs in run_group $1 are
# unfinished, everything waiting on that group becomes claimable.
STMTS["enqueue-next-if-peer-group-is-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = now()
            WHERE id IN (
                SELECT id FROM jorb
                WHERE waitfor_group = $1
                   AND state = 'waiting'
                   AND 0 = (
                       SELECT count(*) FROM jorb
                       WHERE run_group = $1
                          AND state != 'finished'
                   )
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *"""

# Wake jobs waiting on a single upstream job we just finished.
STMTS["enqueue-next-self-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = now()
            WHERE id IN (
                SELECT id FROM jorb
                WHERE waitfor_job = $1
                   AND state = 'waiting'
                   AND 0 = (
                       SELECT count(*) FROM jorb
                       WHERE id = $1
                          AND state != 'finished'
                   )
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *"""

# DXE primitives (see pyjobby/dxe.py for semantics)
STMTS["load-steps"] = dxe.LOAD_STEPS_SQL
STMTS["record-step"] = dxe.RECORD_STEP_SQL
STMTS["set-event"] = dxe.SET_EVENT_SQL
STMTS["send"] = dxe.SEND_SQL
STMTS["recv"] = dxe.RECV_SQL

# Worker registry (executed on the heartbeat connection, not prepared).
WORKER_REGISTER_SQL = """INSERT INTO jorb_worker
        (host, pid, queue, capabilities, version)
        VALUES ($1, $2, $3, $4, $5) RETURNING id"""
WORKER_HEARTBEAT_SQL = "UPDATE jorb_worker SET last_seen = now() WHERE id = $1"
WORKER_SHUTDOWN_SQL = "UPDATE jorb_worker SET shutdown_at = now() WHERE id = $1"


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
    prio: int = 1000
    stop: bool = False
    pid: int = field(default_factory=lambda: os.getpid())
    node: str = field(default_factory=lambda: platform.node())
    cache: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 10  # Maximum attempts before terminal 'crashed'
    default_timeout: int = 3600  # Default job timeout in seconds (1 hour)
    heartbeat_interval: float = 10.0  # seconds between registry heartbeats
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
    _current_job_id: int | None = None
    _exec_task: asyncio.Task[Any] | None = None
    _cancel_current: bool = False
    # registry heartbeat runs on its own connection so a long job never
    # delays liveness reporting
    _hb_cxn: asyncpg.Connection | None = None
    _hb_task: asyncio.Task[None] | None = None
    # optional per-worker HTTP listener
    _web_runner: web.ServerRunner | None = None

    async def ex(self, op: str, *args: Any) -> list[asyncpg.Record]:
        """Execute prepared statement ``op`` with *args, reconnecting (and
        re-preparing everything) if the connection was lost."""
        while True:
            try:
                return await self.stmts[op].fetch(*args)  # type: ignore
            except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError) as e:
                if self.stop:
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
        """LISTEN for enqueue wakeups and cancellation requests."""

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
        from pyjobby import __version__

        try:
            self._hb_cxn = await db.connect(**self.dsn)
            self.worker_id = await self._hb_cxn.fetchval(
                WORKER_REGISTER_SQL,
                self.node,
                self.pid,
                self.qname,
                list(self.capabilities),
                __version__,
            )
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
        except (OSError, asyncpg.PostgresError) as e:
            logger.warning(f"Worker registry unavailable ({e}); running unregistered")
            self.worker_id = None
            self._hb_task = None

    async def _heartbeat_loop(self) -> None:
        assert self._hb_cxn is not None
        while not self.stop:
            try:
                await self._hb_cxn.execute(WORKER_HEARTBEAT_SQL, self.worker_id)
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
                with contextlib.suppress(Exception):
                    self._hb_cxn = await db.connect(**self.dsn)
            await asyncio.sleep(self.heartbeat_interval)

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
        assert self.webPort
        launcher = request.path.split("/")[1]
        result: web.Response
        if launcher in self.webPort["paths"]:
            ran = self.classForKlassFromName(launcher).web(request)
            if asyncio.iscoroutine(ran):
                result = await ran
            else:
                result = ran

            return result

        return web.Response(text="not so fast!")

    async def _start_web_listener(self) -> None:
        if not (self.webPort and "sites" in self.webPort):
            return
        server = web.Server(self.webHandler)  # type: ignore
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

    def classForKlassFromName(
        self, klassName: str, job: dict[str, Any] | None = None
    ) -> Any:
        # reload worker module on each run to catch any worker code changes
        klass_mod = pydoc.locate(".".join(klassName.split(".")[:-1]))
        importlib.reload(klass_mod)  # type: ignore

        klassi = pydoc.locate(klassName)

        if not klassi:
            raise FileNotFoundError(
                f"Job class not found: {klassName}; search path: {sys.path}"
            )

        klass = klassi(s=self, job=job)  # type: ignore
        return klass

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

                jobs = await self.ex(
                    "claim",
                    self.pid,
                    self.node,
                    self.qname,
                    self.capabilities,
                    self.prio,
                    self.worker_id,
                )

                if not jobs:
                    sleepytime = True
                    continue

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
                upstream = await self.ex("get-result", admin_data["use_result_from"])
                if upstream and upstream[0]["state"] == "finished":
                    job["kwargs"] = {
                        **(job.get("kwargs") or {}),
                        "upstream_result": upstream[0]["result"],
                    }

            klass = self.classForKlassFromName(job["job_class"], job=job)

            # DXE: bind previously recorded checkpoints so completed steps
            # fast-forward instead of re-executing on this attempt
            checkpoints = await self.ex("load-steps", jid)
            klass._dxe_bind(checkpoints, epoch)

            # timeout: admin_data override > class attribute > worker default
            job_timeout = admin_data.get("timeout_seconds")
            if job_timeout is None:
                # class attribute wins when set (0 disables the timeout),
                # otherwise the worker default applies
                job_timeout = (
                    self.default_timeout if klass.timeout is None else klass.timeout
                )

            if job_timeout:
                await self.ex(
                    "set-timeout",
                    jid,
                    datetime.timedelta(seconds=job_timeout),
                    epoch,
                )

            # claimed -> running (records `started`)
            await self.ex("run", jid, epoch)

            start_counter = time.perf_counter()
            self._exec_task = asyncio.create_task(self._execute(klass, job_timeout))
            result = await self._exec_task

            elapsed_ms = (time.perf_counter() - start_counter) * 1000
            logger.info(f"[job {jid}] Completed {jname} in {elapsed_ms:.2f} ms")

            if admin_data.get("save_result") is False:
                result = None
            await self.ex("finished", jid, result, epoch)
            await self._wake_dependents(job)

        except dxe.DurableSleep as sleep:
            # the job checkpointed a sleep and rescheduled itself; nothing
            # terminal to record — it resumes past this point when claimed
            # again after wake_at
            logger.info(f"[job {jid}] Durable sleep until {sleep.wake_at}")

        except dxe.StaleExecutionError:
            # a newer attempt owns the row (monitor/operator requeue while
            # we ran); abandon quietly — our writes were fenced out anyway
            logger.warning(f"[job {jid}] Superseded mid-run; abandoning stale attempt")

        except asyncio.CancelledError:
            if not self._cancel_current:
                raise  # the WORKER is being cancelled, not the job
            logger.warning(f"[job {jid}] Cancelled while running (operator request)")
            await self.ex("cancelled", jid, epoch)
            self.errors += 1

        except TimeoutError:
            await self._handle_failure(
                job,
                klass,
                error=f"Job timed out after {job_timeout}s",
                backtrace="Timeout error - job exceeded maximum execution time",
                timed_out=True,
            )

        except Exception as e:
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
        """Run the job's task under its timeout, whatever shape it takes.

        .run() executes synchronous tasks to completion, so it goes to a
        thread: the event loop stays responsive and sync tasks honor
        job_timeout too. Async tasks just create their coroutine/generator
        in the thread, which is cheap and safe. (A timed-out sync task's
        thread keeps running in the background; only its result is
        abandoned.)"""
        staged = await asyncio.wait_for(
            asyncio.to_thread(klass.run), timeout=job_timeout or None
        )

        if asyncio.iscoroutine(staged):
            if job_timeout:
                result = await asyncio.wait_for(staged, timeout=job_timeout)
            else:
                result = await staged

            if inspect.isasyncgen(result):
                inner = result
                result = [x async for x in inner]
            return result

        if inspect.isasyncgen(staged):

            async def collect() -> list[Any]:
                return [x async for x in staged]

            if job_timeout:
                return await asyncio.wait_for(collect(), timeout=job_timeout)
            return await collect()

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


@dataclass
class Job:
    """Parent class of all jobs run by JobSystem.

    User jobs subclass Job and override the task() method. Inside task()
    the DXE (Durable Execution Engine) primitives are available:

        await self.step("name", fn, *args)   # checkpointed: never re-runs
                                             # once succeeded, even across
                                             # retries and worker crashes
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

    #: Set by the @job decorator when a class is registered.
    job_class_path: ClassVar[str] = ""

    # --- DXE state (bound by the worker before execution; declared so the
    # attributes always exist and are type-checked) ---
    _dxe_steps: dict[int, Any] = field(default_factory=dict)
    _dxe_seq: int = 0
    _dxe_epoch: int = 0

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

    async def step(self, name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute ``fn(*args, **kwargs)`` as a durable, checkpointed step.

        On success the return value (must be JSON-serializable) is recorded;
        any later attempt of this job returns the recorded value without
        re-executing. On failure the error is recorded for observability
        and the exception propagates into the job's normal retry path — the
        next attempt fast-forwards every completed step and re-executes
        only from the failure onward.
        """
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
                return prior["output"]
            # recorded failure: fall through and re-execute this step

        started = db.utcnow()
        try:
            result = fn(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
        except dxe.DXEError:
            raise
        except Exception as e:
            recorded = await self.s.ex(
                "record-step",
                self.job["id"],
                seq,
                name,
                None,
                f"{type(e).__name__}: {e}",
                self._dxe_epoch,
                started,
            )
            if not recorded:
                raise dxe.StaleExecutionError(
                    f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
                ) from e
            raise

        recorded = await self.s.ex(
            "record-step",
            self.job["id"],
            seq,
            name,
            result,
            None,
            self._dxe_epoch,
            started,
        )
        if not recorded:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
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
            remaining = (wake_at - db.utcnow()).total_seconds()
            if remaining <= 0:
                return  # slept enough on a previous attempt; continue
            # woken early (operator requeue): go back to sleep for the rest
            await self.s.ex(
                "reschedule", self.job["id"], datetime.timedelta(seconds=remaining)
            )
            raise dxe.DurableSleep(wake_at)

        wake_at = db.utcnow() + datetime.timedelta(seconds=seconds)
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
        await self.s.ex(
            "reschedule", self.job["id"], datetime.timedelta(seconds=seconds)
        )
        raise dxe.DurableSleep(wake_at)

    async def set_event(self, key: str, value: Any) -> None:
        """Publish a key/value event on this job (idempotent upsert).
        Clients and other jobs read it with get_event; waiters are woken
        via NOTIFY."""
        await self.s.ex("set-event", self.job["id"], key, value)

    async def send(
        self, dest_job_id: int, message: Any, topic: str | None = None
    ) -> None:
        """Send a durable message to another job's mailbox — exactly once
        across retries (the send is a checkpointed step)."""

        async def _do_send() -> int | None:
            rows = await self.s.ex("send", dest_job_id, topic, message)
            return rows[0]["id"] if rows else None

        await self.step(f"dxe.send:{dest_job_id}:{topic or ''}", _do_send)

    async def recv(
        self,
        topic: str | None = None,
        timeout: float = 60,
        poll_interval: float = 0.25,
    ) -> Any | None:
        """Await one durable message for this job (oldest first), or None on
        timeout. The consumed message is checkpointed, so a retry of this
        job sees the same message again instead of consuming a second one."""
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

        deadline = db.utcnow() + datetime.timedelta(seconds=timeout)
        message: Any | None = None
        while True:
            rows = await self.s.ex("recv", self.job["id"], topic)
            if rows:
                message = rows[0]["message"]
                break
            if db.utcnow() >= deadline:
                break
            await asyncio.sleep(poll_interval)

        recorded = await self.s.ex(
            "record-step",
            self.job["id"],
            seq,
            name,
            message,
            None,
            self._dxe_epoch,
            db.utcnow(),
        )
        if not recorded:
            raise dxe.StaleExecutionError(
                f"job {self.job['id']} epoch {self._dxe_epoch} superseded"
            )
        return message

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
        await self.s.ex("reschedule", self.job["id"], interval)

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
) -> None:
    """Run the JobSystem for this worker process"""
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
        max_retries=max_retries,
        default_timeout=default_timeout,
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
    help="Queue to process (can be multiple)",
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
    help="Worker count",
    show_default=True,
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
    "-v",
    is_flag=True,
    help="show version then exit",
)
@click.option(
    "--config",
    "-c",
    default="./pyjobby.conf.py",
    help="config file path",
    show_default=True,
)
def workit(
    queue: tuple[str],
    cap: tuple[str],
    workers: int,
    path: str,
    max_retries: int,
    default_timeout: int,
    check_interval: float,
    v: bool,
    config: str,
) -> None:
    from pyjobby import __version__ as localver

    if v:
        print(localver)
        sys.exit(0)

    configure_worker_logging()

    if not os.path.isfile(config):
        logger.error("Config file not found! Requested: {}", config)
        sys.exit(1)

    try:
        loadedConfig = load_config_from_file(config, {"db_params", "web_listen"})
    except RuntimeError as e:
        logger.error("Failed to load config {}: {}", config, e)
        sys.exit(1)

    # If queue requests are less than total worker count, pad out the queue
    # workers with default listeners up to the requested worker count.
    lqueue = list(queue)
    lcap = list(cap)
    if len(queue) < workers:
        lqueue.extend(["default"] * (workers - len(queue)))

    # capability includes this hostname specification by default
    lcap.append(f"host:{platform.node()}")

    for pth in path:
        # Also use requested directories as paths for job class lookups
        sys.path.append(pth)

    logger.info(f"[{localver}] Launching {len(lqueue)} workers...")
    launched = set()
    for idx, q in enumerate(lqueue):
        p = Process(
            target=runAndDone,
            args=(
                q,
                tuple(lcap),
                idx,
                loadedConfig["db_params"],
                loadedConfig["web_listen"],
                max_retries,
                default_timeout,
                check_interval,
            ),
        )
        p.start()
        launched.add(p)
        logger.info(f"[{p.pid}] Launched...")

        # random delay before launching next worker so job checks are
        # staggered instead of bunching at the same start microsecond.
        time.sleep(random.uniform(0.001, 0.010))

    def signalBroadcast(signum: int, frame: Any) -> None:
        """Forward main interrupt to child processes"""
        try:
            for p in launched:
                os.kill(p.pid, signum)  # type: ignore
        except OSError:
            pass

    signal.signal(signal.SIGTERM, signalBroadcast)
    for l in launched:
        with contextlib.suppress(KeyboardInterrupt):
            l.join()

    # success!
    sys.exit(0)


if __name__ == "__main__":
    workit()
