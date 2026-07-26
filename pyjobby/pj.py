#!/usr/bin/env python3

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
from typing import (
    Any,
)

import asyncpg  # type: ignore
import click
from aiohttp import web
from loguru import logger

from . import db
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


# We are using an async job update pattern where we CLAIM a job with
# a DB update, work the job, then mark the job as either error or finished.
# Other postgres job systems keep the job row as SELECT FOR UPDATE SKIP LOCKED
# for the entire time a job is running, then the lock gets removed when
# either the row is updated/deleted (or when the worker crashes, then the job
# row SELECT FOR UPDATE lock is abandoned and it reverts back to regular
# selectable job again).
# The only problem with holding open SELECT FOR UPDATE SKIPPED LOCKED rows is
# each held-open row needs to be scanned over to be SKIPPED for future
# selections. The more jobs you have open in SELECT FOR UPDATE state slows
# down future selects because each new selects has to scan over the current
# open selects to skip them all.
# So the goal is to have as few SELECT FOR UPDATE rows open concurrently as
# possible.
# Another benefit of SELECT FOR UPDATE being held open is it doesn't need
# to write back to the disk to mark a job as obtained. It just holds open
# the row as non-selectable by other transactions.
# The most advanced and highest performing method is to just take internal
# in-memory postgres advisory locks on rows since those don't need to write
# back to the table to mark a row as claimed and they also don't need to
# be scanned over sequentially if many are held simultaenously.
# This was also the basic pattern Que used before it moved to more complicated
# (but faster since there's no writes involved) advisory locking methods.
# Also note: this uses the 'jorb_poll_index' for state='queued' lookups.
# Also also note: the jobs will be <= the priority of the worker, so if you
# enqueue a job with a giant priority (3 million), you better also have workers
# with priority levels that high to read them. Lower priorities denote higher
# importance since we sort jobs with low priority numbers first.
STMTS: dict[str, str] = {}


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists, owned by someone else
        return True
    return True


# For alternative "in-memory lock without writing the update back to claim"
# appoach, see que project lib/que/poller.rb for their CTE and server-side
# SQL function for taking advisory locks without needing to update rows while
# consuming a job.
STMTS["claim"] = """UPDATE jorb
              SET state = 'claimed',
                  worker_pid = $1,
                  worker_host = $2,
                  updated = TIMEZONE('utc', clock_timestamp()),
                  run_count = run_count + 1
              WHERE id = (
                 SELECT id FROM jorb
                 WHERE queue = $3
                     AND (capability = ANY($4::text[]) OR capability is NULL)
                     AND prio <= $5
                     AND run_after <= TIMEZONE('utc', clock_timestamp())
                     AND state = 'queued'
                 ORDER BY prio, run_after
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
              )
              RETURNING *"""

# It's implicit in this 'claimed' lookup *we* are the node with the claim.
# TODO: need crash recovery to detect 'claimed' but not error or finished jobs.
STMTS["get"] = """SELECT * FROM jorb
                     WHERE id = $1
                        AND state = 'claimed'"""

# Fetch an upstream job's stored result for run-time result passing
# (admin_data.use_result_from).
STMTS["get-result"] = """SELECT state, result FROM jorb WHERE id = $1"""

# NOTE on time conventions: the original jorb columns (created, updated,
# run_after) are `timestamp without time zone` storing naive UTC, so they use
# TIMEZONE('utc', clock_timestamp()). The columns added later by migrations
# (started, finished, timeout_at) are TIMESTAMPTZ, so they use plain
# clock_timestamp() — applying TIMEZONE('utc', ...) to those would store the
# wrong instant on any server not running in UTC.
STMTS["finished"] = """UPDATE jorb
              SET state = 'finished',
                  result = $2,
                  finished = clock_timestamp(),
                  timeout_at = NULL,
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1
                AND state IN ('claimed', 'running')
          RETURNING *"""

STMTS["run"] = """UPDATE jorb
              SET state = 'running',
                  started = clock_timestamp(),
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1
          RETURNING *"""

STMTS["set-timeout"] = """UPDATE jorb
              SET timeout_at = clock_timestamp() + $2::interval
              WHERE id = $1"""

# Phase 2 improvement: Crashed jobs create retry jobs (see create-retry below)
# instead of being requeued directly, preserving crash audit trail
STMTS["crash"] = """UPDATE jorb
              SET state = 'crashed',
                error_message = $2,
                error_backtrace = $3,
                error_count = error_count + 1,
                timeout_at = NULL,
                finished = clock_timestamp(),
                updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1
                AND state IN ('claimed', 'running')
          RETURNING *"""

STMTS["reschedule"] = """UPDATE jorb
              SET state = 'queued',
                  run_after = TIMEZONE('utc', clock_timestamp()) + $2::interval,
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1"""


# Deadline scheduler stops multiple tasks from being queued for
# the same deadline key.
STMTS["schedule-deadline"] = """ INSERT INTO jorb
            (deadline_key, queue, prio, run_after, uid, run_group,
             job_class, kwargs, admin_data) 
            VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8, $9)"""

# enqueue next waitfor_group if all jobs in current group are finished
# (meaning, ZERO jobs are NOT FINISHED for this group)
# Then update waiting waitfor_group jobs to be queued.
# Also need nested selects with FOR UPDATE SKIP LOCKED so the inner
# sub-select isn't run twice if jobs are completing at the same time.
STMTS["enqueue-next-if-peer-group-is-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = TIMEZONE('utc', clock_timestamp())
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

# Wake up any waiting jobs for a job ID we just finished.
STMTS["enqueue-next-self-finished"] = """ UPDATE jorb
            SET state = 'queued',
                updated = TIMEZONE('utc', clock_timestamp())
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

# Recover jobs that were stuck when this worker crashed previously
# Updated to include time-based check to prevent recovering jobs from slow-but-alive workers
STMTS["recover-abandoned"] = """UPDATE jorb
              SET state = 'queued',
                  run_after = TIMEZONE('utc', clock_timestamp()),
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE worker_host = $1
                AND state IN ('claimed', 'running')
                AND updated < TIMEZONE('utc', clock_timestamp()) - $2::interval
              RETURNING id, job_class, state as old_state"""

# In-flight jobs claimed by this host, used for pid-liveness recovery on startup.
STMTS["list-in-flight-for-host"] = """SELECT id, worker_pid, job_class, state
              FROM jorb
              WHERE worker_host = $1
                AND state IN ('claimed', 'running')"""

# Requeue a specific set of jobs whose worker process is known to be dead.
STMTS["requeue-ids"] = """UPDATE jorb
              SET state = 'queued',
                  run_after = TIMEZONE('utc', clock_timestamp()),
                  timeout_at = NULL,
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = ANY($1::bigint[])
                AND state IN ('claimed', 'running')
              RETURNING id, job_class, state as old_state"""

# Create a retry job (new row) instead of modifying the crashed job.
# This preserves the crashed job as audit trail and creates a clean retry.
# The SQL is shared with the client/admin/websocket retry paths (db module)
# so every retry row looks identical no matter which component created it.
STMTS["create-retry"] = db.build_retry_sql(allowed_states=("crashed",))

# Cancel a queued or waiting job
# Only jobs not yet claimed can be cancelled
STMTS["cancel"] = """UPDATE jorb
              SET state = 'cancelled',
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1
                AND state IN ('queued', 'waiting')
              RETURNING *"""


@dataclass
class JobSystem:
    """A PostgreSQL Job system.

    Reads tasks with class and kwargs designated by a jorb table, runs
    tasks based on queue, priority, and next run time.

    If a task throws an exception, the exception is saved to the job row
    and job is marked as 'crashed' for future inspection."""

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
    # Improvement: Configurable retry and timeout settings
    max_retries: int = 10  # Maximum retry attempts before dead letter
    default_timeout: int = 3600  # Default job timeout in seconds (1 hour)
    enable_recovery: bool = True  # Enable abandoned job recovery on startup
    recovery_timeout: int = (
        300  # Time in seconds before job is considered abandoned (5 minutes)
    )
    # pid of the launcher process that forked us; when set, the worker stops
    # if that process dies (prevents orphaned workers polling forever after
    # their launcher is killed). 0 disables the check (direct/embedded use).
    _launcher_pid: int = 0

    async def ex(self, op: str, *args: Any) -> list[asyncpg.Record]:
        """Execute 'op' from prepared statement dict with *args.

        Returns the coroutine to await for returning array of asyncpg Result instances."""
        # fetch -> return all rows
        # fetchrow -> return first row
        # fetchval -> return val in column=x of results
        # docs: https://magicstack.github.io/asyncpg/current/api/index.html
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

    async def _listen_for_enqueues(self) -> None:
        """LISTEN for jobs entering 'queued' in our queue (wakeup channel)."""

        def _on_enqueue(
            conn: Any, pid: int, channel: str, payload: str
        ) -> None:  # pragma: no cover - trivial
            if payload == self.qname:
                self._wake.set()

        try:
            await self.cxn.add_listener("jorb_enqueued", _on_enqueue)
        except asyncpg.PostgresError as e:
            logger.warning(f"Could not LISTEN for enqueue wakeups ({e}); polling only")

    async def _reconnect(self) -> None:
        """Re-establish the worker connection and re-prepare all statements.

        Retries until the database is reachable again (workers are long-lived
        daemons: losing the database is expected to be a transient condition)."""
        with contextlib.suppress(Exception):
            await self.cxn.close()

        while not self.stop:
            try:
                self.cxn: asyncpg.Connection = await db.connect(**self.dsn)
                self.stmts: dict[str, asyncpg.PreparedStatement] = {
                    name: await self.cxn.prepare(stmt) for name, stmt in STMTS.items()
                }
                await self._listen_for_enqueues()
                logger.info("Database connection re-established")
                return
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logger.warning(f"Reconnect attempt failed ({e}); retrying...")
                await asyncio.sleep(1.0)

    def shutdown(self, signum: int, frame: Any) -> None:
        """Request graceful shutdown - stop processing new jobs but finish current job."""
        logger.info(f"Shutdown request received by signal {signum}")
        self.stop = True
        # Note: The main loop will finish the current job before exiting

    async def recover_abandoned_jobs(self) -> list[asyncpg.Record]:
        """Recover jobs that were left in claimed/running state when this worker crashed.

        This is called on startup to reclaim jobs that this worker was processing
        when it previously crashed or was killed. Jobs are moved back to 'queued'
        state so they can be claimed and processed again.

        Returns list of recovered job records."""
        if not self.enable_recovery:
            return []

        try:
            # Precise path: any in-flight job on this host whose worker process
            # no longer exists is definitely abandoned — requeue immediately.
            in_flight = await self.ex("list-in-flight-for-host", self.node)
            dead_ids = [
                row["id"]
                for row in in_flight
                if row["worker_pid"] and not _pid_alive(row["worker_pid"])
            ]
            recovered = []
            if dead_ids:
                recovered = list(await self.ex("requeue-ids", dead_ids))

            # Fallback path: rows without a recorded pid can only be recovered
            # by age, using the configured recovery timeout.
            from datetime import timedelta

            recovery_interval = timedelta(seconds=self.recovery_timeout)
            if any(not row["worker_pid"] for row in in_flight):
                recovered.extend(
                    await self.ex("recover-abandoned", self.node, recovery_interval)
                )

            if recovered:
                logger.warning(
                    f"Recovered {len(recovered)} abandoned jobs from previous crash "
                    f"(older than {self.recovery_timeout}s): "
                    f"{[r['id'] for r in recovered]}"
                )

                for job in recovered:
                    logger.info(
                        f"  Job {job['id']} ({job['job_class']}) "
                        f"recovered from state '{job['old_state']}'"
                    )

            return recovered
        except Exception as e:
            logger.error(f"Failed to recover abandoned jobs: {e}")
            return []

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

    def classForKlassFromName(
        self, klassName: str, job: dict[str, Any] | None = None
    ) -> Any:
        # reload worker module on each run to catch any worker code changes
        # reload details:
        # https://docs.python.org/3/library/importlib.html#importlib.reload
        klass_mod = pydoc.locate(".".join(klassName.split(".")[:-1]))
        importlib.reload(klass_mod)  # type: ignore

        # now lookup the class itself...
        klassi = pydoc.locate(klassName)

        if not klassi:
            raise FileNotFoundError(
                f"Job class not found: {klassName}; search path: {sys.path}"
            )

        # disable check because pydoc.locate() has no typed return value
        klass = klassi(s=self, job=job)  # type: ignore
        return klass

    async def run(self) -> None:
        # start jobby webserver for out-of-queue processing requests!
        if self.webPort and "sites" in self.webPort:
            # https://docs.aiohttp.org/en/stable/web_lowlevel.html
            # Ignore typing on Server() because it is too specific to the
            # internals of aiohttp and mypy isn't matching the class hierarchy.
            server = web.Server(self.webHandler)  # type: ignore
            runner = web.ServerRunner(server)
            await runner.setup()

            for site in self.webPort["sites"]:
                assert isinstance(site, dict)
                # note: .start() returns but continues running  server in background!
                if "path" in site:
                    assert isinstance(site["path"], str)
                    site["path"] = site["path"] + f"-{self.workerId}"
                    await web.UnixSite(runner, **site).start()
                else:
                    # allow multiple binding under Linux...
                    assert isinstance(site, dict)
                    site.update({"reuse_port": True})
                    await web.TCPSite(runner, **site).start()

                logger.info(
                    "Starting server at {}",
                    ";".join(
                        [f"{k}:{v}" for k, v in site.items() if not isinstance(v, bool)]
                    ),
                )

        # TODO: could also have the MainProcess run the queue reader then
        #       dispatch results via multiprocessing.Queue to workers, but
        #       then we'd have a larger failure window where maybe the main
        #       thread dispatches, inserts into Queue, then there's a restart
        #       or workers crash and we lose the 'claimed' but unprocessed entries.
        #       Though, those claimed-unprocessed jobs would be picked up by the
        #       job failure detector, eventually.
        #       Also though, moving to the advisory lock mechanism would fix the
        #       claimed-but-crashed problem because a crash would just release
        #       the row again.
        self.cxn = await db.connect(**self.dsn)

        # Wake immediately when work arrives in our queue instead of waiting
        # for the next poll tick (migration 009's jorb_enqueued trigger).
        # Polling remains the fallback for run_after-delayed jobs and for
        # databases without the trigger installed.
        self._wake: asyncio.Event = asyncio.Event()
        await self._listen_for_enqueues()

        # even though the asyncpg adapter will cache statements as they are run,
        # manually preparing all statements before use also validates all SQL is
        # well-formed on startup before any execution attempts.
        self.stmts = {}
        for name, stmt in STMTS.items():
            self.stmts[name] = await self.cxn.prepare(stmt)

        # IMPROVEMENT: Recover jobs that were running when this worker crashed
        # This resolves the TODO above - we now recover abandoned jobs on startup
        await self.recover_abandoned_jobs()

        logger.info(f"[{self.qname}:{self.prio}] Connected and waiting for jobs!")
        prev: float = 0.0
        processed: int = 0
        error: int = 0
        start_counter: float = time.perf_counter()
        prev_status: float = time.perf_counter()
        prev_processed: int = 0  # Initialize BEFORE loop for correct rate calculation
        skipSleep: bool = True  # process without sleeping until job retrieval is empty
        jobs: list[asyncpg.Record] = []
        klass: Job | None = None
        sleepytime: bool = False  # skip initial sleep check
        while not self.stop:
            # only check for next job after checkInterval seconds
            now: float = time.perf_counter()
            # logger.info(f"Checking interval: {now} - {prev} < {self.checkInterval}")

            diff = now - prev
            if sleepytime and diff < self.checkInterval:
                # Sleep until either the poll interval elapses (with jitter so
                # workers never check for work in lockstep) or a NOTIFY says a
                # job just entered our queue — whichever comes first.
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.checkInterval
                        - diff
                        + random.randint(0, 1000) / 1000,
                    )
                # a shutdown may have been requested mid-sleep: never claim
                # new work after stop is set (graceful shutdown means finish
                # the current job only, not "take one more")
                if self.stop:
                    continue

                # orphan check: if our launcher process died (we were
                # reparented), stop instead of polling headless forever
                if self._launcher_pid and os.getppid() != self._launcher_pid:
                    logger.warning("Launcher process died; worker stopping")
                    self.stop = True
                    continue

            # record time of the current job check
            prev = time.perf_counter()

            # Log status every 5 minutes with correct rate calculation
            if now - prev_status >= 300:
                pdiff_total = (processed - prev_processed) / (now - prev_status)
                logger.info(
                    f"[processed {processed} ({pdiff_total:0.2f}/s)] [errors {error}]"
                )
                prev_status = now
                prev_processed = processed

            jobs = await self.ex(
                "claim", self.pid, self.node, self.qname, self.capabilities, self.prio
            )

            if not jobs:
                sleepytime = True
                continue

            # here we ONLY have one job of 'jobs' because of LIMIT 1 in
            # "claim" (dict copy so kwargs can be augmented before running)
            job = dict(jobs[0])

            # don't sleep for next check, we may have a run of jobs
            sleepytime = False

            processed += 1
            # reset so exception handlers never see the previous job's class
            klass = None
            try:
                jid = job["id"]
                jname = job["job_class"].split(".")[-1]
                logger.info(
                    "[job {}] Running {} ({}, {}, {})",
                    jid,
                    jname,
                    job["job_class"],
                    job["queue"],
                    job["prio"],
                    job["capability"],
                )

                # Phase 2: run-time result passing — inject the upstream
                # job's stored result into kwargs before the task runs
                admin_data = job.get("admin_data") or {}
                if isinstance(admin_data, dict) and admin_data.get("use_result_from"):
                    upstream = await self.ex(
                        "get-result", admin_data["use_result_from"]
                    )
                    if upstream and upstream[0]["state"] == "finished":
                        job["kwargs"] = {
                            **(job.get("kwargs") or {}),
                            "upstream_result": upstream[0]["result"],
                        }

                klass = self.classForKlassFromName(job["job_class"], job=job)

                # Phase 2: Extract timeout from admin_data or use class attribute or default
                job_timeout = (
                    admin_data.get("timeout_seconds")
                    if isinstance(admin_data, dict)
                    else None
                )
                if job_timeout is None:
                    job_timeout = getattr(klass, "timeout", self.default_timeout)

                # Phase 2: Set timeout_at in database if timeout is configured
                if job_timeout:
                    await self.ex(
                        "set-timeout",
                        job["id"],
                        datetime.timedelta(seconds=job_timeout),
                    )

                # transition claimed -> running and record `started`; the
                # timeout monitor, duration metrics, and DAG timeline all key
                # off this state.
                await self.ex("run", job["id"])

                # .run() executes synchronous tasks to completion, so call it
                # in a thread: the event loop stays responsive and sync tasks
                # honor job_timeout too. Async tasks just create their
                # coroutine/generator in the thread, which is cheap and safe.
                # (A timed-out sync task's thread keeps running to completion
                # in the background; only its result is abandoned.)
                startJobTime = time.perf_counter()
                resultStageA = await asyncio.wait_for(
                    asyncio.to_thread(klass.run), timeout=job_timeout or None
                )

                if asyncio.iscoroutine(resultStageA):
                    # Apply timeout to async jobs
                    if job_timeout:
                        result = await asyncio.wait_for(
                            resultStageA, timeout=job_timeout
                        )
                    else:
                        result = await resultStageA

                    # Check if the awaited result is an async generator
                    if inspect.isasyncgen(result):
                        collected_inner = result

                        async def collect_gen() -> list[Any]:
                            return [x async for x in collected_inner]

                        result = await collect_gen()
                elif inspect.isasyncgen(resultStageA):
                    # Apply timeout to async generator jobs (direct return, not from async function)
                    async def collect_with_timeout() -> list[Any]:
                        return [x async for x in resultStageA]

                    if job_timeout:
                        result = await asyncio.wait_for(
                            collect_with_timeout(), timeout=job_timeout
                        )
                    else:
                        result = await collect_with_timeout()
                else:
                    result = resultStageA

                totalJobTime = time.perf_counter() - startJobTime
                logger.info(
                    f"[job {jid}] Completed {jname} in {totalJobTime * 1000:.2f} ms"
                )

                # record job completion back to database (honoring an
                # explicit save_result=False opt-out)
                if (
                    isinstance(admin_data, dict)
                    and admin_data.get("save_result") is False
                ):
                    result = None
                await self.ex("finished", job["id"], result)

                # check for any new jobs next down the run tree...
                nextFromSelf = await self.ex("enqueue-next-self-finished", jid)
                if nextFromSelf:
                    nextJobIds = [x["id"] for x in nextFromSelf]
                    logger.info(
                        f"[job {jid}:{jname}] Triggered scheduling of {nextJobIds}"
                    )

                gid = job["run_group"]
                if gid:
                    nextFromGroup = await self.ex(
                        "enqueue-next-if-peer-group-is-finished", gid
                    )
                    if nextFromGroup:
                        nextJobIds = [x["id"] for x in nextFromGroup]
                        logger.info(
                            f"[job {jid}:{jname}; group {gid:x}] Triggered scheduling of {nextJobIds}"
                        )
            except TimeoutError as e:
                # Phase 2: Handle timeouts based on on_timeout configuration
                admin_data = job.get("admin_data") or {}
                on_timeout = admin_data.get("on_timeout", "retry")
                max_retries = admin_data.get("max_retries", self.max_retries)
                error_msg = f"Job timed out after {job_timeout}s"

                logger.error(
                    "[job {}:{}] TIMEOUT in {} after {}s (on_timeout={})",
                    job["id"],
                    jname,
                    job["job_class"],
                    job_timeout,
                    on_timeout,
                )

                # Mark original job as crashed (audit trail)
                await self.ex(
                    "crash",
                    job["id"],
                    error_msg,
                    "Timeout error - job exceeded maximum execution time",
                )

                # Retry or fail based on on_timeout configuration
                current_error_count = job.get("error_count", 0) + 1
                if on_timeout == "retry" and current_error_count < max_retries:
                    # fall back to base Job backoff if the class never loaded
                    rescheduleFor = await (klass or Job).rescheduleBackoff(
                        job,  # type: ignore[arg-type]
                        current_error_count,
                    )
                    retry_job_id = await self.stmts["create-retry"].fetchval(
                        job["id"], rescheduleFor, current_error_count
                    )
                    logger.info(
                        "[job {}] Created retry job {} (attempt {}/{}) "
                        "scheduled for {:.1f} minutes",
                        job["id"],
                        retry_job_id,
                        current_error_count + 1,
                        max_retries,
                        rescheduleFor.total_seconds() / 60,
                    )
                else:
                    reason = (
                        "max retries exceeded"
                        if current_error_count >= max_retries
                        else "on_timeout=fail"
                    )
                    logger.error(
                        "[job {}] PERMANENTLY FAILED after {} attempts - {}",
                        job["id"],
                        current_error_count,
                        reason,
                    )

                error += 1

            except Exception as e:
                # Phase 2: Use configurable max_retries from admin_data
                exc_type, exc_value, exc_traceback = sys.exc_info()
                admin_data = job.get("admin_data") or {}
                max_retries = admin_data.get("max_retries", self.max_retries)

                logger.exception(
                    "[job {}:{}] Error in {}: {}", job["id"], jname, job["job_class"], e
                )

                # Mark original job as crashed (for audit trail)
                # Note: we aren't recording the stack because with
                # our multiprocessing forks, each stack is just the
                # multiprocessing pre-fork setup frames.
                await self.ex(
                    "crash",
                    job["id"],
                    str(e),
                    "Traceback:\n" + "".join(traceback.format_tb(exc_traceback)),
                )

                # Phase 2: Create NEW retry job using configurable retry strategy
                # This preserves the crashed job as audit trail
                current_error_count = job.get("error_count", 0) + 1
                if current_error_count < max_retries:
                    # fall back to base Job backoff if the class never loaded
                    rescheduleFor = await (klass or Job).rescheduleBackoff(
                        job,  # type: ignore[arg-type]
                        current_error_count,
                    )

                    # Create a NEW job for retry (separate row)
                    retry_job_id = await self.stmts["create-retry"].fetchval(
                        job["id"], rescheduleFor, current_error_count
                    )

                    logger.info(
                        "[job {}] Created retry job {} (attempt {}/{}) "
                        "scheduled for {:.1f} minutes using {} strategy",
                        job["id"],
                        retry_job_id,
                        current_error_count + 1,
                        max_retries,
                        rescheduleFor.total_seconds() / 60,
                        admin_data.get("retry_strategy", "exponential"),
                    )
                else:
                    logger.error(
                        "[job {}] PERMANENTLY FAILED after {} attempts - max retries ({}) exceeded",
                        job["id"],
                        current_error_count,
                        max_retries,
                    )

                error += 1

        # if we ever exit the loop...
        await self.cxn.close()


@dataclass
class Job:
    """Parent class of all jobs run by JobSystem.

    User jobs subclass Job and override the task() method to
    run operations as needed."""

    s: JobSystem
    job: dict[str, Any]

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

    async def rescheduleBackoff(
        self,
        job: dict | int | None = None,
        attempt: int | None = None,
    ) -> datetime.timedelta:
        """Calculate retry delay using configurable retry strategy from admin_data.

        Returns a timedelta for when the job should be retried.

        If no 'attempt' count is given, use the current job's error_count.

        Accepted call shapes (all seen in the wild; all supported):
            instance.rescheduleBackoff()               # uses self.job
            instance.rescheduleBackoff(attempt=3)
            instance.rescheduleBackoff(3)              # attempt positionally
            instance.rescheduleBackoff(job_dict, 3)
            Job.rescheduleBackoff(job_dict, attempt=1) # legacy classmethod style

        Supports Phase 2 retry strategies:
        - exponential (default): 1s, 2s, 4s, 8s, 16s...
        - linear: 1s, 2s, 3s, 4s, 5s...
        - fibonacci: 1s, 1s, 2s, 3s, 5s, 8s...
        - fixed (legacy): quadratic backoff

        NOTE: This method only CALCULATES the delay. It does NOT update the database.
        The caller is responsible for using the returned timedelta in database updates.
        """
        from .retry_strategies import calculate_retry_from_job

        # normalize the accepted call shapes into (job, attempt)
        if isinstance(job, int) and attempt is None:
            job, attempt = None, job
        if job is None:
            if isinstance(self, Job):
                job = self.job
            elif hasattr(self, "get"):
                # legacy Job.rescheduleBackoff(job_dict, ...) class-style call
                job = self
            else:
                job = {}

        assert not isinstance(job, int)
        if attempt is None:
            attempt = job.get("error_count", 0)

        # Use Phase 2 retry strategies if configured
        retry_delay = calculate_retry_from_job(job, attempt)

        # Return the interval as timedelta (do NOT call reschedule() which would UPDATE the database!)
        return retry_delay

    async def reschedule(
        self,
        relative: int,
        unit: str = "seconds",
        deltas: dict[str, int] | None = None,  # or provide units as key=interval
    ) -> datetime.timedelta:
        """Schedule event at [relative] [unit] duration into the future from now.

        Default argument just takes number of seconds in the futrue to reschedule.

        Units are from timedelta:
            "microseconds milliseconds seconds minutes hours days weeks"

        You can also provide a custom unit for the boost or even provide a dict of multiple
        interval types for aggregate multi-level boosting (5 days, 3 hours, 6 minutes, etc).

        Note: the re-schedule is from NOW and *not* from the original job requested run time.
        """

        if not deltas:
            deltas = {unit: relative}

        ds = {str(u): r for u, r in deltas.items()}

        # asyncpg requires a python timedelta for doing '::interval' math
        interval = datetime.timedelta(**ds)
        await self.s.ex("reschedule", self.job["id"], interval)

        # return the interval used for future math calculation
        return interval


def runAndDone(
    qname: str,
    caps: tuple[str],
    n: int,
    db_params: dict[str, str],
    web_listen: dict[str, Any] | None,
    max_retries: int = 10,
    default_timeout: int = 3600,
    recovery_timeout: int = 300,
    enable_recovery: bool = True,
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
        recovery_timeout=recovery_timeout,
        enable_recovery=enable_recovery,
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
    help="Maximum retry attempts before job is marked as permanently failed",
    show_default=True,
)
@click.option(
    "--default-timeout",
    default=3600,
    help="Default job timeout in seconds (1 hour)",
    show_default=True,
)
@click.option(
    "--recovery-timeout",
    default=300,
    help="Time in seconds before abandoned jobs are recovered (5 minutes)",
    show_default=True,
)
@click.option(
    "--no-recovery",
    is_flag=True,
    help="Disable abandoned job recovery on startup",
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
    recovery_timeout: int,
    no_recovery: bool,
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

    # If queue requests are less than total worker count,
    # pad out the queue workers with default listeners up to
    # the requested worker count.
    lqueue = list(queue)
    lcap = list(cap)
    if len(queue) < workers:
        lqueue.extend(["default"] * (workers - len(queue)))

    # capability includes this hostname specification by default
    # TODO: allow dynamic capabilites based on system performance?
    #       e.g. "disk-10G" if disk has > 10 GB free, etc.
    #            and/or allow jobs to have a "pre-check" routine
    #            where they can decline to run and be re-scheduled
    #            on another node without signaling error. (negative
    #            capability? run on anything EXCEPT the failed test node?)
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
                recovery_timeout,
                not no_recovery,  # enable_recovery is opposite of no_recovery flag
                check_interval,
            ),
        )
        p.start()
        launched.add(p)
        logger.info(f"[{p.pid}] Launched...")

        # random delay before launching next worker so
        # job checks are staggered over launch times instead
        # of all bunching up at the same start microsecond.
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
