#!/usr/bin/env python3

import asyncio
import asyncpg  # type: ignore
from aiohttp import web
import importlib

from .configloader import load_config_from_file

import abc
import signal

from enum import Enum
from dataclasses import dataclass, field

from multiprocessing import Process, Queue
from typing import (
    Any,
    Type,
    Union,
    Awaitable,
    Coroutine,
    Optional,
    Callable,
)

import inspect
import random

import os
import sys
import platform
import traceback
import datetime

import pydoc  # for instantiating classes from string names
import time
import click
import orjson

import subprocess
from loguru import logger

fmt = (
    "<yellow>{process.id:>}:{process.name:<}</yellow> "
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<1}</level> | "
    "<cyan>{name}</cyan>:"
    "<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


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
STMTS: dict[str, str] = dict()


# For alternative "in-memory lock without writing the update back to claim"
# appoach, see que project lib/que/poller.rb for their CTE and server-side
# SQL function for taking advisory locks without needing to update rows while
# consuming a job.
STMTS[
    "claim"
] = """UPDATE jorb
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
STMTS[
    "get"
] = """SELECT * FROM jorb
                     WHERE id = $1
                        AND state = 'claimed'"""

STMTS[
    "finished"
] = """UPDATE jorb
              SET state = 'finished',
                  result = $2,
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1
          RETURNING *"""

STMTS[
    "run"
] = """UPDATE jorb
              SET state = 'running',
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1"""

# TODO: are we going to re-enqueue crashed jobs? By setting state to
# 'crashed' here, it'll never be picked up again (also problem if worker
# claims a job and then crashes without any other updates too, need to
# check last updated timestamps for heartbeat/visibility timeouts).
STMTS[
    "crash"
] = """UPDATE jorb
              SET state = 'crashed',
                error_message = $2,
                error_backtrace = $3,
                error_count = error_count + 1,
                updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1"""

STMTS[
    "reschedule"
] = """UPDATE jorb
              SET state = 'queued',
                  run_after = TIMEZONE('utc', clock_timestamp()) + $2::interval,
                  updated = TIMEZONE('utc', clock_timestamp())
              WHERE id = $1"""


# Deadline scheduler stops multiple tasks from being queued for
# the same deadline key.
STMTS[
    "schedule-deadline"
] = """ INSERT INTO jorb
            (deadline_key, queue, prio, run_after, uid, run_group,
             job_class, kwargs, admin_data) 
            VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8, $9)"""

# enqueue next waitfor_group if all jobs in current group are finished
# (meaning, ZERO jobs are NOT FINISHED for this group)
# Then update waiting waitfor_group jobs to be queued.
# Also need nested selects with FOR UPDATE SKIP LOCKED so the inner
# sub-select isn't run twice if jobs are completing at the same time.
STMTS[
    "enqueue-next-if-peer-group-is-finished"
] = """ UPDATE jorb
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
STMTS[
    "enqueue-next-self-finished"
] = """ UPDATE jorb
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
    checkInterval: int = 5  # seconds
    webPort: Optional[dict[str, Union[list[dict[str, Any]], set[str]]]] = None
    prio: int = 1000
    stop: bool = False
    pid: int = field(default_factory=lambda: os.getpid())
    node: str = field(default_factory=lambda: platform.node())
    cache: dict[str, Any] = field(default_factory=dict)

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
            except asyncpg.InterfaceError:
                # if disconnect detected, just attempt to reconnect forever
                await asyncio.sleep(0.5)
                continue

    def shutdown(self, signum: int, frame: Any) -> None:
        logger.info(f"Shutdown request received by signal {signum}")
        self.stop = True

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
        self, klassName: str, job: Optional[dict[str, Any]] = None
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
            server = web.Server(self.webHandler) # type: ignore
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
                    site.update(dict(reuse_port=True))
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
        self.cxn = await asyncpg.connect(**self.dsn)

        # tell asyncpg we want to use orjson for json types
        await self.cxn.set_type_codec(
            "json", encoder=orjson.dumps, decoder=orjson.loads, schema="pg_catalog"
        )

        # even though the asyncpg adapter will cache statements as they are run,
        # manually preparing all statements before use also validates all SQL is
        # well-formed on startup before any execution attempts.
        self.stmts: dict[str, asyncpg.PreparedStatement] = {}
        for name, stmt in STMTS.items():
            self.stmts[name] = await self.cxn.prepare(stmt)

        # TODO: we should query for 'running' tasks with this worker hostname on startup
        #       because if they are running with this hostname, and we are STARTING,
        #       then they were previously abandoned and need to be re-queued so they
        #       can be picked up again.
        logger.info(f"[{self.qname}:{self.prio}] Connected and waiting for jobs!")
        prev: float = 0.0
        processed: int = 0
        error: int = 0
        start_counter: float = time.perf_counter()
        prev_status: float = time.perf_counter()
        skipSleep: bool = True  # process without sleeping until job retrieval is empty
        jobs: list[asyncpg.Record] = []
        klass: Optional[Job] = None
        sleepytime: bool = False  # skip initial sleep check
        while not self.stop:
            # only check for next job after checkInterval seconds
            now: float = time.perf_counter()
            # logger.info(f"Checking interval: {now} - {prev} < {self.checkInterval}")

            diff = now - prev
            if sleepytime and diff < self.checkInterval:
                # also adds jitter to the sleep interval so workers never get
                # trapped into only checking for new work in lockstep.
                await asyncio.sleep(
                    self.checkInterval - diff + random.randint(0, 1000) / 1000
                )

            # record time of the current job check
            prev_processed = 0
            prev = time.perf_counter()
            if now - prev_status >= 300:
                pdiff_total = (processed - prev_processed) / (prev - prev_status)
                logger.info(
                    f"[processed {processed} ({pdiff_total:0.2f}/s)] [errors {error}]"
                )
                prev_status = prev
                prev_processed = processed

            jobs = await self.ex(
                "claim", self.pid, self.node, self.qname, self.capabilities, self.prio
            )

            if not jobs:
                sleepytime = True
                continue

            # here we ONLY have one job of 'jobs' because of LIMIT 1 in "claim"
            job = jobs[0]

            # don't sleep for next check, we may have a run of jobs
            sleepytime = False

            processed += 1
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

                klass = self.classForKlassFromName(job["job_class"], job=job)

                # if job is async, .run() returns a coroutine we need to await.
                # else, if task is not async, .run() runs the job itself.
                # ignore type check because the exception will catch if bad
                startJobTime = time.perf_counter()
                resultStageA = klass.run()  # type: ignore

                if asyncio.iscoroutine(resultStageA):
                    result = await resultStageA
                elif inspect.isasyncgen(resultStageA):
                    result = [x async for x in resultStageA]
                else:
                    result = resultStageA

                totalJobTime = time.perf_counter() - startJobTime
                logger.info(
                    f"[job {jid}] Completed {jname} in {totalJobTime * 1000:.2f} ms"
                )

                # record job completion back to database
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
                            f"[job {jid}:{jname}; group {hex(gid)[2:]}] Triggered scheduling of {nextJobIds}"
                        )
            except Exception as e:
                # oops you excepted something
                exc_type, exc_value, exc_traceback = sys.exc_info()

                logger.exception(
                    "[job {}:{}] Error in {}: {}", job["id"], jname, job["job_class"], e
                )

                # since we failed, re-schedule...
                # TODO: implement max-retries logic before a hard failure?
                if klass:
                    rescheduleFor = await klass.rescheduleBackoff()
                    logger.exception(
                        "[job {}] Rescheduling to run in {:.3f} minutes",
                        job["id"],
                        rescheduleFor.total_seconds() / 60,
                    )

                error += 1

                # Note: we aren't recording the stack because with
                # our multiprocessing forks, each stack is just the
                # multiprocessing pre-fork setup frames.
                #    "\nStack:\n"
                #    + "".join(traceback.format_stack())
                await self.ex(
                    "crash",
                    job["id"],
                    str(e),
                    "Traceback:\n" + "".join(traceback.format_tb(exc_traceback)),
                )

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

    def rescheduleBackoff(
        self, attempt: Optional[int] = None
    ) -> Awaitable[datetime.timedelta]:
        """Reschedule using min/max exponential backoff with jitter.

        Returns a coroutine which returns amount of time before job runs
        again as a timedelta.

        If no 'attempt' count is given, use the current job's error_count.

        Job will sleep between 16 seconds and 17.2 minutes."""

        if attempt is None:
            attempt = self.job["error_count"]

        # Use a min of 4 (16 seconds) and a max of 10 (~17 minutes)
        # (plus a random jitter buffer between 0 and 10 seconds)
        delayFor = 2 ** min(max(4, attempt), 10) + (random.randint(0, 1000) / 100)
        return self.reschedule(delayFor, "seconds")

    async def reschedule(
        self,
        relative: int,
        unit: str = "seconds",
        deltas: dict[str, int] = {},  # or provide units as key=interval
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
    web_listen: Optional[dict[str, Any]],
) -> None:
    """ Run the JobSystem for this worker process """
    runner = JobSystem(
        dsn=db_params,
        qname=qname,
        capabilities=caps,
        workerId=n,
        checkInterval=5,
        webPort=web_listen,
    )

    signal.signal(signal.SIGTERM, runner.shutdown)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        return
    except:
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
    v: bool,
    config: str,
) -> None:
    from pyjobby import __version__ as localver

    if v:
        print(localver)
        sys.exit(0)

    if not os.path.isfile(config):
        logger.error("Config file not found! Requested: {}", config)
        sys.exit(1)

    loadedConfig = load_config_from_file(config, {"db_params", "web_listen"})

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
        """ Forward main interrupt to child processes """
        try:
            for p in launched:
                os.kill(p.pid, signum)  # type: ignore
        except:
            pass

    signal.signal(signal.SIGTERM, signalBroadcast)
    for l in launched:
        try:
            l.join()
        except KeyboardInterrupt:
            pass

    # success!
    sys.exit(0)


if __name__ == "__main__":
    workit()

    # Test basic asyncpg usage and how it bridges SQL syntax interop to python
    async def zooper() -> None:
        loadedConfig = load_config_from_file("conf.py", {"db_params", "web_listen"})
        cxn = await asyncpg.connect(**loadedConfig["db_params"])
        import datetime

        # interval syntax check
        # (and also checking limit values can be supplied by param)
        i = datetime.timedelta(seconds=300)
        got = await cxn.fetch(
            "SELECT TIMEZONE('utc', CURRENT_TIMESTAMP) + $1::interval LIMIT $2", i, 5
        )
        logger.info(got)
        logger.info(got[0])
        logger.info(got[0][0])
        logger.info(got[0]["?column?"])  # anonymous column name in the output

        got = await cxn.fetch(
            "CREATE TEMP TABLE zooper(a bigserial primary key, b text, c text)"
        )
        logger.info(got)

        got = await cxn.fetch("CREATE UNIQUE INDEX zooper_idx ON zooper (b, c)")
        logger.info(got)

        got = await cxn.fetch("INSERT INTO zooper (b, c) VALUES ('hi', 'there')")
        logger.info(got)

        try:
            got = await cxn.fetch("INSERT INTO zooper (b, c) VALUES ('hi', 'there')")
            logger.info(got)
        except asyncpg.exceptions.UniqueViolationError:
            logger.info("Duplicate insert conflicted as expected!")

        got = await cxn.fetch("SELECT * FROM zooper WHERE b = any($1::text[])", [])
        logger.info("Any query as empty: {}", got)
        got = await cxn.fetch("SELECT * FROM zooper WHERE b = any($1::text[])", [None])
        logger.info("Any query as none: {}", got)
        got = await cxn.fetch(
            "SELECT * FROM zooper WHERE b = any($1::text[])",
            ["asdfg", "jkl", "there", "hi"],
        )
        logger.info("Any query as actual: {}", got)

        got = await cxn.fetch("INSERT INTO zooper (b, c) VALUES (NULL, 'there')")
        got = await cxn.fetch("SELECT * FROM zooper WHERE b = any($1::text[])", [None])
        logger.info("Any query as none with null: {}", got)

        got = await cxn.fetch(
            "SELECT * FROM zooper WHERE b = ANY($1::text[]) OR b is NULL", [None]
        )
        logger.info("Any query as none with null OR: {}", got)

    asyncio.run(zooper())
