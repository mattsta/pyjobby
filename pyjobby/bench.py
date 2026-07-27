"""``pj-bench`` — pyjobby's permanent benchmark and diagnosis harness.

Every performance number in ``docs/SCALE.md`` was measured once, written
down, and the script that produced it thrown away. This module is the
replacement: each subcommand *reproduces* one of those measurements, so a
change can be shown to have made things better or worse instead of argued
about, and the next bottleneck hunt starts from a running tool rather than
from zero.

    pj-bench enqueue --concurrency 16 enqueue throughput + NOTIFY commit lock
    pj-bench claim   --workers 8      claim throughput, lock contention, and
                                      what a capped queue SUSTAINS with short
                                      jobs at a high and at a low cap
    pj-bench e2e     --jobs 200       real worker processes, real latency
    pj-bench notify                   notifications per job lifecycle
    pj-bench plans   --seed 20000     EXPLAIN every hot query (CI gate)
    pj-bench resolve                  what resolving a job class costs per
                                      job: the cache, and what --reload adds
    pj-bench all                      everything, with a summary table

``--json`` on every subcommand emits stable key names for CI and for
diffing two runs. ``pj-bench plans`` exits non-zero if any hot query does a
``Seq Scan on jorb``, which makes it runnable as a regression gate.

WHAT THIS TOUCHES (the cleanup guarantee)
-----------------------------------------
Every subcommand creates its rows in a queue named ``pjbench_<cmd>_<hex>``
that cannot collide with anything else, and deletes exactly that queue's
rows in a ``finally`` — including on Ctrl-C. Child rows (``jorb_history``,
``jorb_step``, ``jorb_event``, ``jorb_mailbox``) follow by ON DELETE
CASCADE. Nothing here ever runs TRUNCATE, ever issues an unqualified
DELETE, and never touches a row outside its own queue. The only global
state it writes is a ``jorb_queue`` control row for its own queue name,
deleted in the same ``finally``.

Two subcommands also write outside the database, both into a temporary
directory removed in a ``finally``: ``pj-bench e2e`` writes the config file
its real worker processes read, and ``pj-bench resolve`` writes the
throwaway jobs module it re-imports (and takes it back off ``sys.path`` and
out of ``sys.modules``, which its ``cleanup`` block reports).

Two things are deliberately global while a run is in flight, both restored:

* ``ANALYZE jorb`` (``pj-bench plans``) updates planner statistics for the
  whole table, which is the point — a plan measured against stale stats is
  a plan for a table that no longer exists.
* ``ALTER TABLE jorb DISABLE TRIGGER`` (``pj-bench enqueue``, and ONLY
  behind ``--allow-trigger-toggle``). Leaving a production trigger disabled
  would silently stop history recording or worker wakeups, so restoration
  runs from a ``finally``, from a SIGTERM handler, and from an ``atexit``
  hook that opens a brand-new connection if the original one is gone.

SAFETY
------
A benchmark that competes with real work produces garbage numbers *and*
adds load to production, so every subcommand refuses to run against a
database that already holds a lot of jobs unless ``--force`` is given, and
says how many it found.

MEASUREMENT DISCIPLINE
----------------------
First touch of a page is not the steady state, and one sample is not a
measurement. Every timed subcommand runs a discarded-but-reported warm-up
(``--no-warmup`` to skip) and then ``--repeat N`` measured runs, reporting
the MEDIAN and the spread rather than a single number.

Shape matters as much as repetition. ``pj-bench enqueue`` measures
CONCURRENT, one-transaction-per-job inserts because that is the only shape
in which pyjobby's real write ceiling appears: committing a transaction
that issued a NOTIFY takes a global exclusive lock held to the end of the
commit, since notifications must be delivered in commit order and that
order is not settled until commits finish. Concurrent enqueues therefore
serialize instead of grouping. A single-client benchmark has nothing to
serialize against and a bulk insert amortizes one lock over the whole
batch, so both report a number several times too high — which is exactly
how docs/SCALE.md's 67k rows/s came to describe an enqueue path that
actually tops out an order of magnitude lower. Those two shapes are still
measured, and labelled ``*_contrast`` so they cannot be quoted as the
answer.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import datetime
import importlib
import json
import math
import os
import re
import shutil
import signal
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import asyncpg  # type: ignore[import-untyped]
import click
from loguru import logger

from . import db, monitor
from .cli import (
    ConfigProblem,
    DatabaseProblem,
    fail,
    print_table,
    print_warning,
)
from .configloader import load_config_from_file
from .monitor import TERMINAL_STATES
from .pj import STMTS, Job, JobSystem
from .scheduler import CONCURRENCY_COUNT_SQL

DEFAULT_CONFIG = "./pyjobby.conf.py"

#: Reference workload from docs/SCALE.md: 1,000,000 jobs/hour.
SCALE_TARGET_RATE = 278.0

#: Jobs left in flight (claimed/running) by ``pj-bench plans``. Deliberately a
#: constant and not a fraction of the seed: in-flight work is bounded by the
#: workers and by ``max_concurrency``, so it does not grow with the table.
PLAN_IN_FLIGHT = 200

#: One job in this many gets DXE checkpoints, and one DAG in this many keeps a
#: job. Both sweeps have to walk past the rest to fill a batch, which is the
#: shape that catches an index scan doing a table's worth of work — seeding
#: them all-eligible would make every sweep stop at its first row and prove
#: nothing. 3 rather than 4 because the job seed assigns state by ``i % 4``,
#: so every 4th job is the same state.
PLAN_STEP_EVERY = 3
PLAN_STEPS_PER_JOB = 3
PLAN_DAG_EVERY = 3

#: Schedules the plan seed creates, sharing the seeded log between them. The
#: schedule-log sweep refuses to delete each schedule's newest execution, so
#: this is also the bound on what that sweep may read and discard.
PLAN_SCHEDULES = 50

#: Worker registry rows the plan seed leaves RETIRED, for the sweep that
#: reaps them. A fleet accumulates these per deploy, so unlike in-flight work
#: the count genuinely does grow — seeding it at the table scale is what
#: gives the planner a real choice.
PLAN_LIVE_WORKERS = 10

#: docs/SCALE.md's claims, carried here so a run can say "agrees" or
#: "DISAGREES" instead of leaving the reader to diff two documents.
#: DISAGREES is not automatically a bug in the platform — a schema that
#: gates notifications on demand legitimately emits fewer for a job nobody
#: is waiting for, and then it is docs/SCALE.md that is stale. The harness's
#: job is to say which number is true today, not which one ought to be.
#:
#: "Notifications per lifecycle" HAS TWO ANSWERS NOW, and the gap between
#: them is the feature rather than an inconsistency. Every remaining channel
#: is gated on demand, so:
#:
#:   unobserved  0 — nothing parked on the queue, nothing awaiting a result.
#:                   The insert's wakeup is gated on an idle worker
#:                   (jorb_enqueued/idle_worker), the terminal transition's
#:                   signal on jorb.awaited (jorb_done/row_local), and the
#:                   claimed/running transitions have no channel at all since
#:                   job_state_change was deleted. Nobody is asking, so
#:                   nothing is sent — and nothing pays the commit lock.
#:   observed    2 — one jorb_enqueued when a worker is parked on the queue,
#:                   one jorb_done when a wait_for_result()-style caller has
#:                   set jorb.awaited. That is the ceiling for a plain job:
#:                   the two load-bearing wakeups, and nothing else.
#:
#: The old claim of 5 was wrong twice over: it counted three job_state_change
#: transitions on a channel that no longer exists, and it counted the other
#: two unconditionally on channels that now only fire on demand.
SCALE_CLAIM_NOTIFICATIONS_UNOBSERVED = 0
SCALE_CLAIM_NOTIFICATIONS_OBSERVED = 2
SCALE_CLAIM_HISTORY_ROWS_PER_LIFECYCLE = 4
SCALE_CLAIM_ROW_WRITES_PER_LIFECYCLE = 4

#: How many jobs may already exist before a run is refused without --force.
DEFAULT_MAX_EXISTING_JOBS = 1000

#: What a plan may read and discard when the rows it walks past are bounded by
#: WORK IN FLIGHT rather than by table size — the seeded in-flight set plus
#: whatever the busy-database guard tolerates already being here, since those
#: jobs are in the same index. Two orders of magnitude under the seed, so a
#: regression to table-scale still fails the gate.
PLAN_IN_FLIGHT_BUDGET = PLAN_IN_FLIGHT + DEFAULT_MAX_EXISTING_JOBS

#: The job class ``pj-bench e2e`` enqueues. It lives here, in the installed
#: package, so real worker processes can import it by dotted path with no
#: --path argument and no dependency on the test tree.
BENCH_JOB_CLASS = "pyjobby.bench.BenchJob"

#: Triggers on ``jorb``, by the role docs/SCALE.md discusses them in.
#: ``job_state_change_notify`` used to be here. It is not "disabled" or
#: "deprecated": the trigger and its channel were DELETED from the schema,
#: so naming it would make every variant below fail on a missing trigger.
TRIGGER_HISTORY = "jorb_history_record"
TRIGGER_ENQUEUED = "jorb_enqueued_notify"
TRIGGER_DONE = "jorb_done_notify"
TRIGGER_CANCEL = "jorb_cancel_notify"

#: Every trigger on ``jorb`` that issues a NOTIFY. Disabling all of them is
#: the upper bound for enqueue: the cost of the commit lock is paid once per
#: transaction that notifies at all, so the interesting comparison is
#: "notifies" versus "does not", not "notifies three times" versus "once".
NOTIFY_TRIGGERS = (TRIGGER_ENQUEUED, TRIGGER_DONE, TRIGGER_CANCEL)

#: Every channel the schema can emit on, so ``pj-bench notify`` counts what
#: is actually sent rather than what it expected to be sent. Two names that
#: used to be here are gone from the schema entirely, and LISTENing on a
#: dead channel is not harmless — PostgreSQL accepts any name, so the tool
#: would sit there reporting a confident 0 for a channel that cannot exist:
#:
#:   job_state_change  deleted; the dashboard polls aggregates instead.
#:   jorb_mailbox      deleted; Job.recv() polls jorb_mailbox directly.
NOTIFY_CHANNELS = (
    "jorb_enqueued",
    "jorb_done",
    "jorb_cancel",
    "jorb_event",
    "schedule_executed",
)

#: Channels a worker or client depends on; the rest are observability.
LOAD_BEARING_CHANNELS = ("jorb_enqueued", "jorb_done", "jorb_cancel")

#: Terminal states inlined as SQL literals, exactly as monitor.py does it:
#: jorb_retention_idx is PARTIAL on this predicate, and a bound parameter
#: falls off the index once PostgreSQL switches to a generic plan.
TERMINAL_STATES_SQL = ", ".join(f"'{state}'" for state in TERMINAL_STATES)


class BenchJob(Job):
    """The trivial job ``pj-bench e2e`` runs on real workers.

    It does nothing on purpose: the measurement is the platform's
    claim/execute/complete overhead, and any work in here would be measured
    instead.
    """

    async def task(self, n: int = 0) -> dict[str, int]:
        return {"n": n}


# =========================================================================
# Target resolution (same inputs as the rest of the platform)
# =========================================================================


@dataclass(frozen=True)
class Target:
    """A resolved database, as both asyncpg kwargs and a DSN string.

    Both forms are needed: connections here use the kwargs, and the real
    worker processes ``pj-bench e2e`` launches read a config file that
    holds a ``db_params`` dict.
    """

    params: dict[str, Any]
    dsn: str

    @property
    def label(self) -> str:
        """Printable identity with no password in it."""
        return f"{self.params['host']}:{self.params['port']}/{self.params['database']}"

    @classmethod
    def from_dsn(cls, dsn: str) -> Target:
        parts = urlsplit(dsn)
        params = {
            "database": unquote(parts.path.lstrip("/")),
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "host": parts.hostname or "localhost",
            "port": parts.port or 5432,
        }
        return cls(params=params, dsn=dsn)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> Target:
        user = quote(str(params.get("user", "")), safe="")
        password = quote(str(params.get("password", "")), safe="")
        host = params.get("host", "localhost")
        port = params.get("port", 5432)
        database = params.get("database", "")
        return cls(
            params={
                "database": database,
                "user": params.get("user", ""),
                "password": params.get("password", ""),
                "host": host,
                "port": port,
            },
            dsn=f"postgresql://{user}:{password}@{host}:{port}/{database}",
        )


def resolve_target(config: str | None, dsn: str | None) -> Target:
    """Resolve --dsn / --config the way every other pyjobby entry point does.

    A DSN wins outright (including ``PYJOBBY_DSN``); otherwise the config
    file's ``db_params`` is used. Config problems and database problems stay
    distinct, because telling an operator "database failure" when their
    config file is missing sends them debugging the wrong system.
    """
    if dsn:
        return Target.from_dsn(dsn)

    path = config or DEFAULT_CONFIG
    try:
        cfg = load_config_from_file(path, keys=["db_params"])
    except RuntimeError as e:
        fail(
            f"Could not load config file: {path}",
            str(e),
            "Use --config to point at a pyjobby conf file, or --dsn to "
            "connect directly (also read from PYJOBBY_DSN).",
            problem=ConfigProblem,
        )
    params = cfg.get("db_params")
    if not params:
        fail(
            f"No db_params found in config file: {path}",
            "Config file must define a db_params dict",
            problem=ConfigProblem,
        )
    return Target.from_params(params)


async def open_connection(target: Target) -> asyncpg.Connection:
    """Connect with pyjobby's JSON codecs, failing as a database problem."""
    try:
        return await db.connect(**target.params)
    except Exception as e:
        fail(f"Failed to connect to database: {e}", problem=DatabaseProblem)


# =========================================================================
# Guards, statistics, and cleanup
# =========================================================================


def bench_queue(kind: str) -> str:
    """A queue name that cannot collide with real work or another run."""
    return f"pjbench_{kind}_{uuid.uuid4().hex[:8]}"


#: Above this planner estimate, the guard reports the estimate instead of
#: counting. An exact count(*) on a table with the volume this platform
#: targets is itself a full scan, and a run that is about to be refused
#: does not need the number to the row.
EXACT_COUNT_CEILING = 1_000_000


async def existing_job_count(conn: asyncpg.Connection) -> tuple[int, bool]:
    """How many jobs are already here, and whether that is exact.

    The planner's estimate decides which question to ask. Small table: an
    exact ``count(*)``, because "already holds 5 jobs" is an actionable
    message and "about 500" (a stale ``reltuples``) is a confusing one.
    Large table: the estimate, flagged approximate, because counting it
    would cost more than the benchmark.
    """
    estimate = int(
        await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = 'jorb'::regclass"
        )
        or 0
    )
    if estimate > EXACT_COUNT_CEILING:
        return estimate, True
    return int(await conn.fetchval("SELECT count(*) FROM jorb")), False


async def guard_busy_database(
    conn: asyncpg.Connection, *, limit: int, force: bool
) -> dict[str, Any]:
    """Refuse to benchmark a database that is already doing real work.

    Rows this run did not create compete for the same buffers, locks and
    autovacuum budget, so the numbers stop meaning anything — and the run
    adds load to whatever is using the database. ``--force`` overrides,
    deliberately loudly.
    """
    count, approximate = await existing_job_count(conn)
    report = {"existing_jobs": count, "approximate": approximate, "limit": limit}
    if count <= limit:
        return report
    about = "about " if approximate else ""
    if not force:
        fail(
            f"Database already holds {about}{count} jobs (limit {limit}).",
            "Benchmarking alongside real work measures the contention, not "
            "the platform, and adds load to a database in use.",
            "Pass --force to run anyway, or --max-existing-jobs to raise the limit.",
        )
    print_warning(
        f"--force: running against a database that already holds "
        f"{about}{count} jobs; numbers include their contention."
    )
    return report


async def cleanup_queue(conn: asyncpg.Connection, queue: str) -> dict[str, int]:
    """Delete everything this run created, and nothing else.

    Scoped to one queue name by design: ``jorb_history``, ``jorb_step``,
    ``jorb_event`` and ``jorb_mailbox`` rows follow via ON DELETE CASCADE,
    so deleting the job rows is the whole cleanup.

    The global tables this can reach — ``jorb_queue``, the worker registry
    rows the real processes ``pj-bench e2e`` starts register, and the
    schedules and DAGs ``pj-bench plans`` seeds so the sweeps that read them
    have something to plan against — all carry this run's queue name, in
    ``jorb_dag``'s case as the DAG name because it has no queue column. So
    they are removed by the same predicate and no row belonging to anything
    else is ever in range. ``jorb_schedule_log`` follows its schedule by
    ON DELETE CASCADE.

    Cleanup is page-accurate, not just row-accurate: see the VACUUM below.
    """
    jobs = int(await conn.fetchval("SELECT count(*) FROM jorb WHERE queue = $1", queue))
    workers = int(
        await conn.fetchval("SELECT count(*) FROM jorb_worker WHERE queue = $1", queue)
    )
    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute("DELETE FROM jorb_queue WHERE name = $1", queue)
    await conn.execute("DELETE FROM jorb_worker WHERE queue = $1", queue)
    await conn.execute("DELETE FROM jorb_schedule WHERE queue = $1", queue)
    await conn.execute("DELETE FROM jorb_dag WHERE name = $1", queue)

    # Deleting the rows is not the whole cleanup, because a deleted row is not
    # a gone row: it leaves a dead tuple and an unset visibility-map bit. A
    # benchmark churns hundreds of thousands of them, which measurably changes
    # how the planner costs OTHER queries on the same database -- enough to
    # turn index-only scans into heap access and fail plan assertions in tests
    # that never ran a benchmark. A tool that silently degrades everything
    # sharing its database is not usable infrastructure.
    #
    # Plain VACUUM, never FULL: this resets the visibility map and statistics
    # without taking an exclusive lock, so it is safe even on a live table.
    # It does not return pages to the operating system; it makes them reusable,
    # which is what the planner cares about.
    await conn.execute("VACUUM (ANALYZE) jorb")
    await conn.execute("VACUUM (ANALYZE) jorb_history")
    return {"jobs_deleted": jobs, "workers_deleted": workers}


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (0.0 on an empty sample)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(samples: Sequence[float]) -> dict[str, Any]:
    """Median plus spread for a set of timings.

    The median is the headline because one sample is noise; the spread is
    reported alongside it because a median with a 3x spread is also noise,
    and a reader who cannot see that will believe the median.
    """
    if not samples:
        return {
            "median": 0.0,
            "min": 0.0,
            "max": 0.0,
            "spread": 0.0,
            "spread_pct": 0.0,
            "runs": 0,
            "samples": [],
        }
    median = statistics.median(samples)
    low, high = min(samples), max(samples)
    return {
        "median": median,
        "min": low,
        "max": high,
        "spread": high - low,
        "spread_pct": ((high - low) / median * 100.0) if median else 0.0,
        "runs": len(samples),
        "samples": list(samples),
    }


async def repeat_timed(
    run: Callable[[], Any],
    *,
    repeat: int,
    warmup: bool,
    setup: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run a coroutine factory ``repeat`` times, timing each.

    ``setup`` runs before every measured run and is NOT timed — it is where
    a benchmark puts the work of getting back to a comparable starting
    state, which is usually deleting what the last run inserted and has
    nothing to do with the thing being measured.

    The warm-up run is REPORTED but never folded into the statistics:
    first-touch page cache and first-plan effects otherwise dominate a small
    run and make an improvement look like a regression.
    """
    warmup_seconds: float | None = None
    if warmup:
        if setup is not None:
            await setup()
        started = time.perf_counter()
        await run()
        warmup_seconds = time.perf_counter() - started

    samples: list[float] = []
    for _ in range(repeat):
        if setup is not None:
            await setup()
        started = time.perf_counter()
        await run()
        samples.append(time.perf_counter() - started)

    result = summarize(samples)
    result["warmup_seconds"] = warmup_seconds
    return result


# =========================================================================
# Trigger toggling — the one genuinely dangerous thing in here
# =========================================================================

#: Triggers disabled right now that have not been re-enabled yet. A
#: benchmark that leaves jorb_history_record or jorb_enqueued_notify
#: disabled silently breaks the audit trail or every worker wakeup in the
#: install, so this list is drained from three places (see run_command and
#: TriggerToggle) rather than one.
_PENDING_RESTORE: list[tuple[str, tuple[str, ...]]] = []
_ATEXIT_REGISTERED = False


async def _set_triggers(
    conn: asyncpg.Connection, triggers: Sequence[str], enable: bool
) -> None:
    verb = "ENABLE" if enable else "DISABLE"
    for name in triggers:
        # Names are module constants, never operator input.
        await conn.execute(f"ALTER TABLE jorb {verb} TRIGGER {name}")


async def _restore_via_new_connection(dsn: str, triggers: tuple[str, ...]) -> None:
    conn = await db.connect(dsn)
    try:
        await _set_triggers(conn, triggers, enable=True)
    finally:
        await conn.close()


def _restore_pending_triggers() -> None:
    """Re-enable anything ``TriggerToggle``'s own ``finally`` could not.

    Reached when the in-band restore failed — a connection that died
    mid-run, or an event loop torn down under the unwinding — so it opens a
    fresh connection on a fresh loop. If even that fails it prints the exact
    ALTER TABLE statements to run by hand, because an operator who is told
    only that something went wrong cannot fix a disabled trigger.
    """
    while _PENDING_RESTORE:
        dsn, triggers = _PENDING_RESTORE.pop()
        try:
            asyncio.run(_restore_via_new_connection(dsn, triggers))
            print(
                f"pj-bench: re-enabled triggers {', '.join(triggers)} on jorb",
                file=sys.stderr,
            )
        except BaseException as e:  # pragma: no cover - needs a dead database
            print(
                f"pj-bench: CRITICAL - could not re-enable triggers "
                f"{', '.join(triggers)} on jorb ({e}). Run: "
                + "; ".join(f"ALTER TABLE jorb ENABLE TRIGGER {t}" for t in triggers),
                file=sys.stderr,
            )


def run_command(coro: Any) -> Any:
    """``asyncio.run`` for a benchmark, with the trigger rescue attached.

    Every subcommand goes through here so the rescue runs while the
    interpreter is still fully alive. An ``atexit`` hook is NOT sufficient
    on its own and this was measured, not assumed: atexit callbacks run
    after ``threading._shutdown()``, so ``concurrent.futures``' executor is
    already closed and the fresh ``asyncio.run`` the rescue needs dies with
    "cannot schedule new futures after interpreter shutdown" — leaving the
    trigger disabled and only a printed remedy behind. Running the rescue
    from this ``finally`` covers a normal exit, an exception, Ctrl-C and
    SIGTERM alike; the atexit hook stays registered for the case where this
    frame itself never runs.
    """
    try:
        return asyncio.run(coro)
    finally:
        _restore_pending_triggers()


class TriggerToggle:
    """Disable triggers on ``jorb`` for a block, and always put them back.

    Restoration is attempted three ways because leaving one of these off is
    a silent, install-wide correctness failure rather than a slow query:
    the ``finally`` below, a SIGTERM handler that converts the signal into a
    normal unwind, and an ``atexit`` hook holding a fresh connection.
    """

    def __init__(
        self, conn: asyncpg.Connection, dsn: str, triggers: Sequence[str]
    ) -> None:
        self.conn = conn
        self.dsn = dsn
        self.triggers = tuple(triggers)
        self._previous_sigterm: Any = None

    async def __aenter__(self) -> TriggerToggle:
        global _ATEXIT_REGISTERED
        if not self.triggers:
            return self
        await _set_triggers(self.conn, self.triggers, enable=False)
        _PENDING_RESTORE.append((self.dsn, self.triggers))
        if not _ATEXIT_REGISTERED:
            atexit.register(_restore_pending_triggers)
            _ATEXIT_REGISTERED = True
        # SIGTERM otherwise kills the process without running atexit.
        with contextlib.suppress(ValueError, OSError):
            self._previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_exits)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if not self.triggers:
            return
        try:
            await _set_triggers(self.conn, self.triggers, enable=True)
        except BaseException:
            # Leave the entry queued so the atexit hook retries it on a new
            # connection; re-raising here would lose the restore entirely.
            raise
        else:
            with contextlib.suppress(ValueError):
                _PENDING_RESTORE.remove((self.dsn, self.triggers))
        finally:
            if self._previous_sigterm is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signal.SIGTERM, self._previous_sigterm)


def _sigterm_exits(signum: int, frame: Any) -> None:
    """Turn SIGTERM into SystemExit so ``atexit`` (and the restore) runs."""
    raise SystemExit(128 + signum)


# =========================================================================
# 1. enqueue — insert throughput and what the triggers cost
# =========================================================================

ENQUEUE_SQL = """
    INSERT INTO jorb (job_class, kwargs, queue, prio)
    SELECT $1, jsonb_build_object('n', i), $2, 100
    FROM generate_series(1, $3::int) i
"""

ENQUEUE_ONE_SQL = """
    INSERT INTO jorb (job_class, kwargs, queue, prio)
    VALUES ($1, '{}'::jsonb, $2, 100)
"""

#: (key, triggers to disable). ``all_triggers_on`` needs no toggling and is
#: the number an operator actually gets; the rest attribute its cost.
#: ``all_notify_off`` is the upper bound: what enqueue costs with NOTIFY
#: out of the commit path entirely.
ENQUEUE_VARIANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("all_triggers_on", ()),
    ("wakeup_notify_off", (TRIGGER_ENQUEUED,)),
    ("history_off", (TRIGGER_HISTORY,)),
    ("all_notify_off", NOTIFY_TRIGGERS),
    ("all_notify_and_history_off", (*NOTIFY_TRIGGERS, TRIGGER_HISTORY)),
)

#: The three ways to insert the same rows, and what each one is FOR.
#: Only the first is production enqueue; the other two exist because
#: quoting them without this label is how a 6x-optimistic number gets into
#: a document (see docs/SCALE.md's 67k rows/s).
ENQUEUE_MODE_MEANING = {
    "production": (
        "concurrent connections, ONE TRANSACTION PER JOB — what a real "
        "enqueue path does, and the only number that exposes the NOTIFY "
        "commit lock"
    ),
    "serial_contrast": (
        "CONTRAST ONLY: one connection, one transaction per job. No commit "
        "concurrency, so the NOTIFY commit lock has nothing to serialize "
        "against and looks nearly free"
    ),
    "bulk_contrast": (
        "CONTRAST ONLY: one transaction inserting every row. One lock "
        "acquisition amortized over the whole batch — this is what "
        "docs/SCALE.md quotes, and it is NOT production enqueue"
    ),
}


async def _insert_serial(conn: asyncpg.Connection, queue: str, jobs: int) -> None:
    for _ in range(jobs):
        await conn.execute(ENQUEUE_ONE_SQL, BENCH_JOB_CLASS, queue)


async def _insert_concurrent(
    pool: asyncpg.Pool, queue: str, concurrency: int, per_connection: int
) -> None:
    """One transaction per job, on ``concurrency`` connections at once.

    The concurrency is the entire point. Committing a transaction that
    issued a NOTIFY takes a GLOBAL exclusive lock held until that commit
    finishes, because notifications have to be delivered in commit order
    and commit order is not established until commits complete. That
    serializes every NOTIFY-bearing commit against every other one and
    defeats group commit — which is invisible to a single client (nothing
    to serialize against) and invisible to a bulk insert (one lock for the
    whole batch).
    """

    async def worker() -> None:
        async with pool.acquire() as conn:
            await _insert_serial(conn, queue, per_connection)

    await asyncio.gather(*(worker() for _ in range(concurrency)))


async def run_enqueue(
    target: Target,
    *,
    rows: int,
    concurrency: int,
    jobs_per_connection: int,
    repeat: int,
    warmup: bool,
    allow_trigger_toggle: bool,
    max_existing_jobs: int,
    force: bool,
) -> dict[str, Any]:
    """Measure enqueue throughput the way production actually enqueues.

    The headline is the CONCURRENT, one-transaction-per-job mode, because
    that is the only one that pays what pyjobby's NOTIFY triggers really
    cost: a transaction that issues a NOTIFY takes a global exclusive lock
    at commit, so concurrent enqueues serialize against each other instead
    of grouping. Measured here at ~2.5x on this schema.

    Two consequences that the output is arranged to make unmissable:

    * The bulk number docs/SCALE.md quotes (~67k rows/s) is one transaction
      inserting 20k rows. It amortizes a single lock acquisition over the
      whole batch and overstates production enqueue by roughly 6x. It is
      reported here as ``bulk_contrast`` and nothing else.
    * The cost is per COMMIT, not per notification — and that is a much
      sharper statement than "trimming channels never helps", which is what
      this docstring used to say and what the numbers used to look like.

      What was actually measured, in order. While several channels still
      notified unconditionally, silencing any ONE of them recovered nothing:
      switching off the three-per-lifecycle ``job_state_change`` firehose
      left the ceiling where it was, because the surviving ungated channels
      took the same global lock in the same commit. Gating ``jorb_done`` on
      ``jorb.awaited`` then bought 1.01x, for the same reason. Deleting
      ``job_state_change`` — the LAST ungated channel — measured 2.63-2.95x
      on the completion path.

      Same mechanism throughout: what the lock responds to is the number of
      COMMITS that notify at all, not the number of notifications inside
      them. Removing one of several ungated channels buys nothing; removing
      the last one buys everything, because that is the commit which stops
      taking the lock. The lesson is not "do not trim" — it is that partial
      trimming is worth exactly zero until a commit path reaches zero, so
      the unit of optimization is the transaction, never the channel.
    """
    queue = bench_queue("enqueue")
    conn = await open_connection(target)
    pool = await db.create_pool(
        **target.params, min_size=concurrency, max_size=concurrency
    )
    result: dict[str, Any] = {}
    production_jobs = concurrency * jobs_per_connection
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)

        async def reset() -> None:
            await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)

        workloads: dict[str, tuple[int, Callable[[], Any]]] = {
            "production": (
                production_jobs,
                lambda: _insert_concurrent(
                    pool, queue, concurrency, jobs_per_connection
                ),
            ),
            "serial_contrast": (
                production_jobs,
                lambda: _insert_serial(conn, queue, production_jobs),
            ),
            "bulk_contrast": (
                rows,
                lambda: conn.execute(ENQUEUE_SQL, BENCH_JOB_CLASS, queue, rows),
            ),
        }

        modes: dict[str, Any] = {}
        for mode, (job_count, work) in workloads.items():
            variants: dict[str, Any] = {}
            for key, triggers in ENQUEUE_VARIANTS:
                if triggers and not allow_trigger_toggle:
                    variants[key] = {"skipped": "requires --allow-trigger-toggle"}
                    continue
                async with TriggerToggle(conn, target.dsn, triggers):
                    timing = await repeat_timed(
                        work, repeat=repeat, warmup=warmup, setup=reset
                    )
                timing["disabled_triggers"] = list(triggers)
                timing["jobs"] = job_count
                timing["jobs_per_second"] = (
                    job_count / timing["median"] if timing["median"] else 0.0
                )
                timing["milliseconds"] = timing["median"] * 1000.0
                variants[key] = timing
            modes[mode] = {
                "meaning": ENQUEUE_MODE_MEANING[mode],
                "jobs": job_count,
                "variants": variants,
                "jobs_per_second": float(
                    variants["all_triggers_on"].get("jobs_per_second") or 0.0
                ),
            }

        result.update(
            {
                "benchmark": "enqueue",
                "database": target.label,
                "queue": queue,
                "concurrency": concurrency,
                "jobs_per_connection": jobs_per_connection,
                "jobs": production_jobs,
                "rows": rows,
                "repeat": repeat,
                "guard": guard,
                "trigger_toggle_allowed": allow_trigger_toggle,
                "modes": modes,
                "jobs_per_second": modes["production"]["jobs_per_second"],
                "notify_commit_lock": _notify_commit_lock_cost(modes),
                "headroom_vs_target_rate": (
                    modes["production"]["jobs_per_second"] / SCALE_TARGET_RATE
                ),
                "target_rate": SCALE_TARGET_RATE,
            }
        )
        return result
    finally:
        await pool.close()
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


def _notify_commit_lock_cost(modes: dict[str, Any]) -> dict[str, Any]:
    """What NOTIFY-in-the-commit-path costs, and what it does NOT cost.

    ``wakeup_only_recovery_pct`` is reported next to the full cost on
    purpose. ``jorb_enqueued`` is the only channel on the INSERT path, so
    turning it off and turning ALL notification off are the same experiment
    for enqueue — and they must therefore report the same recovery. When
    they do, that is the per-COMMIT mechanism visible in one line: the last
    channel to leave a commit path is the one that pays for everything.
    When they DISAGREE, the run is noisy rather than informative: they are
    the same experiment, so the spread between them is a lower bound on
    this machine's measurement error, and neither figure means more than
    that gap. Read ``spread_pct`` on the variants before quoting either.

    WHAT THIS GAP IS MADE OF, now that the channel is gated. Nothing is
    parked on the bench's queue, so the ``idle_worker`` gate says no and no
    notification is emitted at all — ``pj-bench notify``'s unobserved phase
    measures exactly zero on this channel. The throughput this variant
    recovers is therefore NOT the commit lock: it is the price of the gate
    itself, a deferred constraint trigger dispatched per row at commit plus
    one indexed EXISTS against jorb_worker. The commit lock is on top of
    that, and is paid only when a worker really is parked. Reading this gap
    as "what NOTIFY costs" would overstate the lock and understate how much
    the gate already won.
    """

    def rate(mode: str, variant: str) -> float:
        data = modes.get(mode, {}).get("variants", {}).get(variant, {})
        return float(data.get("jobs_per_second") or 0.0)

    def recovery(mode: str, variant: str) -> float | None:
        shipped = rate(mode, "all_triggers_on")
        other = rate(mode, variant)
        if not shipped or not other:
            return None
        return (other - shipped) / shipped * 100.0

    shipped = rate("production", "all_triggers_on")
    no_notify = rate("production", "all_notify_off")
    return {
        "as_shipped_jobs_per_second": shipped,
        "no_notify_jobs_per_second": no_notify,
        "ratio": (no_notify / shipped) if shipped and no_notify else None,
        "cost_pct": (
            (no_notify - shipped) / no_notify * 100.0 if shipped and no_notify else None
        ),
        "wakeup_only_recovery_pct": recovery("production", "wakeup_notify_off"),
        "serial_contrast_recovery_pct": recovery("serial_contrast", "all_notify_off"),
        "bulk_contrast_recovery_pct": recovery("bulk_contrast", "all_notify_off"),
        "explanation": (
            "The lock is taken once per COMMIT, not once per notification. "
            "That is why removing ONE of several ungated channels recovered "
            "nothing (the 3-of-5 job_state_change firehose: ceiling "
            "unmoved; gating jorb_done while the firehose survived: 1.01x) "
            "and why removing the LAST one recovered everything (deleting "
            "job_state_change: 2.63-2.95x on the completion path). Partial "
            "trimming is worth zero until a commit path notifies zero "
            "times, so the unit of optimization is the transaction, not "
            "the channel: reduce commits that notify at all, gate the rest "
            "on demand, or move the notification outside the commit. "
            "CAVEAT on the numbers above: nothing is parked on the bench's "
            "queue, so jorb_enqueued's idle_worker gate emits nothing and "
            "as_shipped_jobs_per_second already excludes the commit lock. "
            "The gap to no_notify_jobs_per_second is the GATE's cost -- a "
            "deferred constraint trigger plus one indexed EXISTS -- with "
            "the lock on top of it only when a worker really is parked."
        ),
    }


# =========================================================================
# 2. claim — throughput through the real claim_jorb(), and contention
# =========================================================================

CLAIM_SQL = "SELECT id, run_epoch FROM claim_jorb($1, $2::text[], $3, $4, $5, $6)"

#: What a real worker writes the instant its job returns -- the worker's own
#: epoch-fenced terminal statement, not an approximation of it, so a churn arm
#: pays the same triggers, history row and page writes production pays.
FINISH_SQL = STMTS["finished"]


@dataclass(frozen=True)
class ClaimArm:
    """One shape the claim path gets measured in.

    ``max_concurrency`` of None means the queue has no control row at all, so
    ``claim_jorb`` never takes the advisory lock: the lock-free fast path.

    ``finish`` separates a DRAIN arm from a CHURN arm, and it is the whole
    reason this dataclass exists. A drain arm never completes anything, so
    its in-flight count only grows -- under a cap that can bind it would
    admit exactly ``cap`` jobs and then spin until the deadline, and under a
    cap that cannot bind it makes the cap's own ``count(*)`` more expensive
    with every claim it makes. A churn arm hands the slot back immediately,
    which is the only shape in which a capped queue is a *queue* rather than
    a one-shot admission of ``cap`` jobs.

    ``hold_seconds`` stands in for the job itself, and a cap is meaningless
    without it: ``max_concurrency`` bounds in-flight work, so the throughput
    a cap permits is ``cap / duration``. With an instantaneous job even a cap
    of 1 permits thousands per second, so a cap benchmarked against zero-cost
    jobs measures the lock and reports it as the cap.

    ``claimers`` is per-arm because a cap is a SIZING decision: nobody runs
    32 workers against a cap of 2. A low-cap arm runs one claimer per slot,
    so what it reports is the ceiling the cap imposes rather than the cost of
    thirty workers being told no.
    """

    key: str
    what: str
    max_concurrency: int | None
    claimers: int
    hold_seconds: float
    finish: bool

    @property
    def cap_can_bind(self) -> bool:
        """Can this arm's cap ever refuse a claim?

        In-flight work can never exceed the claimer count, so a cap at or
        above it is decorative: it costs the advisory lock and the count, and
        refuses nothing. That distinction decides what an empty-handed return
        MEANS here, and conflating a cap doing its job with a lost lock would
        credit the lock for work the cap correctly refused.
        """
        return self.max_concurrency is not None and self.max_concurrency < self.claimers

    @property
    def cap_ceiling_per_second(self) -> float | None:
        """The rate the cap alone permits, ``cap / duration``.

        None when there is no cap, or when the arm holds nothing and the
        "duration" is a round trip rather than a job.
        """
        if self.max_concurrency is None or not self.hold_seconds:
            return None
        return self.max_concurrency / self.hold_seconds


def claim_arms(
    *, workers: int, jobs: int, low_cap: int, high_cap: int, hold_ms: float
) -> tuple[ClaimArm, ...]:
    """The five shapes, and why each one is here.

    The first two are the drain arms this benchmark has always run. The three
    churn arms exist to answer one question the drain arms cannot: a capped
    queue's *sustained* admission rate, which is what a 278/s requirement is
    stated in. They are sized so that each one has exactly one candidate
    bottleneck -- the low-cap arm is provisioned to its cap so the cap binds,
    the high-cap arm is provisioned far past the measured lock ceiling so the
    LOCK binds, and the uncapped churn arm is the same claimers with no lock
    at all, which is what the other two are read against.
    """
    hold = hold_ms / 1000.0
    # 4x, because a churn claimer spends most of its cycle holding a job
    # rather than claiming one: it takes that many to keep the serialised
    # section busy, and if it does not the arm reports the claimer count.
    churn_claimers = workers * 4
    return (
        ClaimArm(
            "uncapped",
            "no control row: the lock-free fast path",
            None,
            workers,
            0.0,
            False,
        ),
        ClaimArm(
            "capped",
            "cap far above the job count and nothing completes, so every "
            "empty-handed return is a lost lock and never a refusal",
            jobs + 1000,
            workers,
            0.0,
            False,
        ),
        ClaimArm(
            "churn_uncapped",
            "short jobs, completed and the slot returned, no lock: the "
            "control the two capped churn arms are read against",
            None,
            churn_claimers,
            hold,
            True,
        ),
        ClaimArm(
            "churn_cap_high",
            "short jobs under a cap too high to ever refuse: the serialised "
            "section is the only constraint left, which is the shape a batch "
            "claim would exist to speed up",
            high_cap,
            churn_claimers,
            hold,
            True,
        ),
        ClaimArm(
            "churn_cap_low",
            "short jobs under a cap sized to bind, one claimer per slot: the "
            "cap is the constraint and no claim strategy can lift it",
            low_cap,
            low_cap,
            hold,
            True,
        ),
    )


async def _claim_loop(
    pool: asyncpg.Pool,
    queue: str,
    worker_index: int,
    deadline: float,
    remaining: dict[str, int],
    arm: ClaimArm,
) -> dict[str, int]:
    """One claimer, hammering ``claim_jorb`` until the queue is empty.

    Counts the empty-handed returns separately from the claims: for a queue
    whose cap cannot bind (see ``ClaimArm.cap_can_bind``), an empty return
    means the claimer lost the per-queue advisory lock, and that ratio is the
    number that says a capped queue is thrashing rather than working. For an
    arm whose cap CAN bind the same return also happens when the cap refuses,
    and the two are not separable from out here -- which is why only the
    non-binding arms report it as a lock miss.
    """
    claims = 0
    empty_with_work = 0
    async with pool.acquire() as conn:
        while time.monotonic() < deadline and remaining["left"] > 0:
            row = await conn.fetchrow(
                CLAIM_SQL, queue, ["bench"], 1000, worker_index, "pj-bench", None
            )
            if row is None:
                queued = await conn.fetchval(
                    "SELECT count(*) FROM jorb "
                    "WHERE queue = $1 AND state = 'queued' AND run_after <= now()",
                    queue,
                )
                if queued:
                    empty_with_work += 1
                else:
                    remaining["left"] = 0
                    break
            else:
                claims += 1
                remaining["left"] -= 1
                if arm.finish:
                    if arm.hold_seconds:
                        await asyncio.sleep(arm.hold_seconds)
                    await conn.execute(FINISH_SQL, row["id"], "{}", row["run_epoch"])
    return {"claims": claims, "empty_with_work": empty_with_work}


async def _seed_claimable(conn: asyncpg.Connection, queue: str, jobs: int) -> None:
    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute(ENQUEUE_SQL, BENCH_JOB_CLASS, queue, jobs)


async def _claim_round(
    pool: asyncpg.Pool, conn: asyncpg.Connection, queue: str, jobs: int, arm: ClaimArm
) -> dict[str, Any]:
    await _seed_claimable(conn, queue, jobs)
    remaining = {"left": jobs}
    deadline = time.monotonic() + 120.0
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            _claim_loop(pool, queue, index, deadline, remaining, arm)
            for index in range(arm.claimers)
        )
    )
    elapsed = time.perf_counter() - started
    claims = sum(r["claims"] for r in results)
    empty = sum(r["empty_with_work"] for r in results)
    attempts = claims + empty
    return {
        "seconds": elapsed,
        "claims": claims,
        "empty_claims_with_work_available": empty,
        "attempts": attempts,
        "claims_per_second": claims / elapsed if elapsed else 0.0,
        "lock_miss_rate": (empty / attempts) if attempts else 0.0,
    }


async def run_claim(
    target: Target,
    *,
    workers: int,
    jobs: int,
    repeat: int,
    warmup: bool,
    max_existing_jobs: int,
    force: bool,
    low_cap: int = 2,
    high_cap: int = 1000,
    hold_ms: float = 5.0,
) -> dict[str, Any]:
    """Claim throughput on an uncapped queue and on capped ones.

    Five arms (see ``claim_arms``), all interleaved. The two drain arms
    answer "what does one claim cost", the three churn arms answer "what rate
    can a capped queue SUSTAIN", which is the only form in which the 278/s
    reference workload can be compared against a cap at all.
    """
    arms = claim_arms(
        workers=workers, jobs=jobs, low_cap=low_cap, high_cap=high_cap, hold_ms=hold_ms
    )
    connections = max(max(a.claimers for a in arms), 2)
    queue = bench_queue("claim")
    conn = await open_connection(target)
    pool = await db.create_pool(
        **target.params, min_size=min(workers, connections), max_size=connections
    )
    modes: dict[str, Any] = {}
    result: dict[str, Any] = {}
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)

        async def install(arm: ClaimArm) -> None:
            await conn.execute("DELETE FROM jorb_queue WHERE name = $1", queue)
            if arm.max_concurrency is not None:
                await conn.execute(
                    "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, $2)",
                    queue,
                    arm.max_concurrency,
                )

        # INTERLEAVED, not one mode then the other. Running every uncapped
        # repeat before every capped one lands all of the machine's drift on
        # whichever mode went last -- and the reported number is their RATIO,
        # so the drift is indistinguishable from the effect being measured.
        # This is not hypothetical: a run of this benchmark once moved the
        # uncapped rate 15x on an unchanged schema, purely because the box
        # picked up load between the two halves. Alternating rounds makes
        # drift show up as spread in both modes instead of as a result in one.
        by_mode: dict[str, list[dict[str, Any]]] = {arm.key: [] for arm in arms}
        if warmup:
            for arm in arms:
                await install(arm)
                await _claim_round(pool, conn, queue, min(jobs, 100), arm)
        for _ in range(repeat):
            for arm in arms:
                await install(arm)
                by_mode[arm.key].append(
                    await _claim_round(pool, conn, queue, jobs, arm)
                )

        for arm in arms:
            rounds = by_mode[arm.key]
            rates = [r["claims_per_second"] for r in rounds]
            summary = summarize(rates)
            modes[arm.key] = {
                "what": arm.what,
                "claims_per_second": summary,
                "seconds": summarize([r["seconds"] for r in rounds]),
                "claims": rounds[-1]["claims"] if rounds else 0,
                "empty_claims_with_work_available": sum(
                    r["empty_claims_with_work_available"] for r in rounds
                ),
                "lock_miss_rate": statistics.median(
                    [r["lock_miss_rate"] for r in rounds]
                )
                if rounds
                else 0.0,
                "empty_returns_mean": (
                    "cap refusals AND lock misses, not separable"
                    if arm.cap_can_bind
                    else "lost advisory locks only: this cap cannot refuse"
                ),
                "max_concurrency": arm.max_concurrency,
                "claimers": arm.claimers,
                "hold_ms": arm.hold_seconds * 1000.0,
                "completes_jobs": arm.finish,
                "cap_ceiling_per_second": arm.cap_ceiling_per_second,
                "headroom_vs_target_rate": (
                    summary["median"] / SCALE_TARGET_RATE if summary["median"] else 0.0
                ),
            }

        uncapped = modes["uncapped"]["claims_per_second"]["median"]
        capped = modes["capped"]["claims_per_second"]["median"]
        churn_uncapped = modes["churn_uncapped"]["claims_per_second"]["median"]
        churn_high = modes["churn_cap_high"]["claims_per_second"]["median"]
        result.update(
            {
                "benchmark": "claim",
                "database": target.label,
                "queue": queue,
                "workers": workers,
                "jobs": jobs,
                "repeat": repeat,
                "hold_ms": hold_ms,
                "low_cap": low_cap,
                "high_cap": high_cap,
                "guard": guard,
                "modes": modes,
                "capped_throughput_ratio": (capped / uncapped) if uncapped else 0.0,
                # What serialising costs a queue whose cap never refuses: the
                # price of exactness, isolated from the cap itself.
                "churn_capped_throughput_ratio": (
                    (churn_high / churn_uncapped) if churn_uncapped else 0.0
                ),
                "claims_per_second": uncapped,
                "target_rate": SCALE_TARGET_RATE,
                "headroom_vs_target_rate": (
                    uncapped / SCALE_TARGET_RATE if uncapped else 0.0
                ),
                # The one number the batch-claim question turns on: what a
                # capped queue sustains when the LOCK, not the cap, is what
                # is left binding.
                "capped_headroom_vs_target_rate": (
                    churn_high / SCALE_TARGET_RATE if churn_high else 0.0
                ),
            }
        )
        return result
    finally:
        await pool.close()
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


# =========================================================================
# 3. e2e — real worker processes, real end-to-end latency
# =========================================================================


@contextlib.contextmanager
def worker_config(target: Target) -> Iterator[str]:
    """A throwaway pyjobby config file for the worker processes to read.

    ``pj`` takes ``db_params`` from a config file, not a DSN, so a run
    driven by ``--dsn`` needs one written for it. Removed in the finally.
    """
    handle, path = tempfile.mkstemp(prefix="pjbench_conf_", suffix=".py")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(
                "# generated by pj-bench; safe to delete\n"
                f"db_params = {target.params!r}\n"
                "web_listen = None\n"
            )
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


async def _e2e_round(
    conn: asyncpg.Connection,
    config_path: str,
    queue: str,
    jobs: int,
    workers: int,
    timeout: float,
    reload_jobs: bool = False,
) -> dict[str, Any]:
    """Enqueue, run real ``pj`` workers, and measure what came out.

    Deliberately uses the console script and a process group kill (the
    helpers in pyjobby/procs.py), not an in-process JobSystem: a
    worker that only ever runs inside the benchmark's own event loop is not
    the thing an operator deploys.
    """
    from pyjobby.procs import spawn, terminate, wait_until

    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute(ENQUEUE_SQL, BENCH_JOB_CLASS, queue, jobs)

    args = ["pj", "--config", config_path, "--workers", str(workers)]
    if reload_jobs:
        args.append("--reload")
    for _ in range(workers):
        args.extend(["--queue", queue])

    wall_started = time.perf_counter()
    proc = spawn(*args)
    completed = 0
    timed_out = False
    try:

        async def all_done() -> Any:
            nonlocal completed
            completed = int(
                await conn.fetchval(
                    "SELECT count(*) FROM jorb WHERE queue = $1 "
                    f"AND state IN ({TERMINAL_STATES_SQL})",
                    queue,
                )
            )
            return completed >= jobs

        try:
            await wait_until(all_done, timeout=timeout, interval=0.1, what="drain")
        except AssertionError:
            timed_out = True
    finally:
        terminate(proc)
    wall_seconds = time.perf_counter() - wall_started

    # COALESCE down the admission timestamps rather than requiring
    # claimed_at: a job whose admission stamp is missing is still a job the
    # fleet completed, and dropping it would report a throughput of zero for
    # a run that plainly did work — the one output a benchmark must never
    # produce, because it reads as a catastrophic regression.
    rows = await conn.fetch(
        """
        SELECT EXTRACT(EPOCH FROM (finished - created)) AS enqueue_to_finished,
               EXTRACT(EPOCH FROM (
                   finished - COALESCE(claimed_at, started, created)
               ))                                       AS claim_to_finished
        FROM jorb
        WHERE queue = $1 AND finished IS NOT NULL
        """,
        queue,
    )
    window = await conn.fetchrow(
        """
        SELECT EXTRACT(EPOCH FROM (
                   max(finished) - min(COALESCE(claimed_at, started, created))
               ))       AS drain_seconds,
               count(*) AS finished
        FROM jorb
        WHERE queue = $1 AND finished IS NOT NULL
        """,
        queue,
    )
    drain_seconds = max(float(window["drain_seconds"] or 0.0), 0.0)
    finished = int(window["finished"] or 0)
    # A drain window of zero means the whole batch landed inside one clock
    # tick, not that it was infinitely fast. Fall back to wall time and SAY
    # which basis was used, so two runs are only ever compared like for like.
    basis = "first_claim_to_last_finish" if drain_seconds > 0 else "wall_clock"
    measured_seconds = drain_seconds if drain_seconds > 0 else wall_seconds

    return {
        "enqueued": jobs,
        "completed": completed,
        "finished": finished,
        "timed_out": timed_out,
        "wall_seconds": wall_seconds,
        "drain_seconds": drain_seconds,
        "drain_basis": basis,
        "jobs_per_second": (finished / measured_seconds) if measured_seconds else 0.0,
        "jobs_per_second_including_startup": (
            finished / wall_seconds if wall_seconds else 0.0
        ),
        "enqueue_to_finished": _latency_block(
            [float(r["enqueue_to_finished"] or 0.0) for r in rows]
        ),
        "claim_to_finished": _latency_block(
            [float(r["claim_to_finished"] or 0.0) for r in rows]
        ),
    }


def _latency_block(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else 0.0,
        "min": min(values) if values else 0.0,
        "count": float(len(values)),
    }


async def run_e2e(
    target: Target,
    *,
    jobs: int,
    workers: int,
    repeat: int,
    warmup: bool,
    timeout: float,
    max_existing_jobs: int,
    force: bool,
    reload_jobs: bool = False,
) -> dict[str, Any]:
    """The headline number: completed jobs/sec through real processes.

    Latency is reported twice on purpose. ``enqueue_to_finished`` is what a
    caller experiences and includes waiting behind the backlog this
    benchmark deliberately creates; ``claim_to_finished`` is what the worker
    itself costs. Reporting only the first makes a fast platform look slow
    at any queue depth; reporting only the second hides the queue.

    ``reload_jobs`` starts the fleet with ``pj --reload``. It is a smell
    test, not the measurement: what that flag costs is measured per call by
    ``pj-bench resolve``, and comparing two separate ``e2e`` invocations is
    precisely the un-interleaved before/after shape docs/TESTING.md rule 3
    exists to forbid. The flag is recorded in the JSON so a run cannot be
    mistaken for the other one.
    """
    queue = bench_queue("e2e")
    conn = await open_connection(target)
    rounds: list[dict[str, Any]] = []
    warmup_round: dict[str, Any] | None = None
    result: dict[str, Any] = {}
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)
        with worker_config(target) as config_path:
            if warmup:
                warmup_round = await _e2e_round(
                    conn,
                    config_path,
                    queue,
                    min(jobs, 20),
                    workers,
                    timeout,
                    reload_jobs,
                )
            for _ in range(repeat):
                rounds.append(
                    await _e2e_round(
                        conn, config_path, queue, jobs, workers, timeout, reload_jobs
                    )
                )

        rates = [r["jobs_per_second"] for r in rounds]
        median_index = rates.index(statistics.median_low(rates)) if rates else 0
        representative = rounds[median_index] if rounds else {}
        result.update(
            {
                "benchmark": "e2e",
                "database": target.label,
                "queue": queue,
                "jobs": jobs,
                "workers": workers,
                "repeat": repeat,
                "reload_jobs": reload_jobs,
                "guard": guard,
                "warmup": warmup_round,
                "rounds": rounds,
                "jobs_per_second": summarize(rates),
                "completed": representative.get("completed", 0),
                "timed_out": any(r["timed_out"] for r in rounds),
                "enqueue_to_finished": representative.get("enqueue_to_finished", {}),
                "claim_to_finished": representative.get("claim_to_finished", {}),
                "target_rate": SCALE_TARGET_RATE,
                "headroom_vs_target_rate": (
                    statistics.median(rates) / SCALE_TARGET_RATE if rates else 0.0
                ),
            }
        )
        return result
    finally:
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


# =========================================================================
# 4. notify — the fan-out that docs/SCALE.md calls the cliff
# =========================================================================


async def run_notify(
    target: Target,
    *,
    lifecycles: int,
    target_rate: float,
    max_existing_jobs: int,
    force: bool,
) -> dict[str, Any]:
    """Count the notifications one job lifecycle actually emits — TWICE.

    "How many notifications does a job cost?" has two correct answers on
    this schema, and the distance between them IS the demand gate. So the
    run drives the same four row writes (insert, claim, run, terminal)
    under two conditions and reports both:

      unobserved  nothing parked on the queue, nobody awaiting a result.
                  Every remaining channel is gated on demand, so the expected
                  answer is ZERO — no wakeup, no completion signal, and no
                  commit anywhere in the lifecycle that takes the NOTIFY
                  commit lock. This is what the overwhelming majority of jobs
                  cost in an installation that fires and forgets.
      observed    one worker row parked idle on the queue (the jorb_enqueued
                  gate), and jorb.awaited set on each job the way
                  wait_for_result() sets it (the jorb_done gate). Expected
                  answer: TWO, the two load-bearing wakeups and nothing else.

    Reporting only the first would understate what a watched job costs;
    reporting only the second would restate the pre-gate world. Reporting one
    number labelled "notifications per lifecycle" would be worst of all,
    because its meaning would depend on what happened to be running.

    docs/SCALE.md's old claim of five per job (~1,390/s at the reference
    rate) is refuted on both counts: three of those five were
    ``job_state_change``, a channel that no longer exists, and the remaining
    two only fire when somebody is actually waiting.

    Each of the four writes runs in its OWN transaction, because that is
    what a real install does and because PostgreSQL collapses identical
    (channel, payload) notifications within one transaction. Driving the
    lifecycles set-based instead measures the deduplication rather than the
    fan-out: ``jorb_enqueued`` carries only the queue name, so a thousand
    inserts batched into one transaction emit exactly ONE wakeup — a real
    and useful property of batch enqueue, and a completely wrong answer to
    "what does one job cost".

    Notifications from other work in the database are counted separately and
    excluded from the per-lifecycle figures.

    ``pg_notification_queue_usage()`` is sampled before and after because
    that queue is server-wide, bounded, and drains only as fast as the
    slowest listener: at 1.0 every NOTIFY-issuing transaction fails, which
    in this platform means nothing can be enqueued or completed anywhere.
    """
    queue = bench_queue("notify")
    conn = await open_connection(target)
    listener = await open_connection(target)
    job_ids: set[int] = set()
    counts: dict[str, int] = dict.fromkeys(NOTIFY_CHANNELS, 0)
    foreign: dict[str, int] = dict.fromkeys(NOTIFY_CHANNELS, 0)
    result: dict[str, Any] = {}

    def make_handler(channel: str) -> Callable[..., None]:
        def handler(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
            if _payload_is_ours(channel, payload, queue, job_ids):
                counts[channel] += 1
            else:
                foreign[channel] += 1

        return handler

    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)
        for channel in NOTIFY_CHANNELS:
            await listener.add_listener(channel, make_handler(channel))

        usage_before = float(
            await conn.fetchval("SELECT pg_notification_queue_usage()")
        )
        usage_peak = usage_before
        phases: dict[str, Any] = {}

        phase_names = ("unobserved", "observed")
        for phase in phase_names:
            # Fresh counters per phase: the two phases are two separate
            # measurements, and carrying counts across them would report the
            # observed run's notifications as if the unobserved run had made
            # some of them.
            counts.update(dict.fromkeys(NOTIFY_CHANNELS, 0))
            foreign.update(dict.fromkeys(NOTIFY_CHANNELS, 0))
            job_ids.clear()
            observed = phase == "observed"
            if observed:
                await _park_idle_worker(conn, queue)
            await _drive_lifecycles(conn, queue, lifecycles, job_ids, observed=observed)
            usage_peak = max(
                usage_peak,
                float(await conn.fetchval("SELECT pg_notification_queue_usage()")),
            )
            total = await _drain_notifications(listener, counts)
            phases[phase] = _notify_phase(
                dict(counts),
                {k: v for k, v in foreign.items() if v},
                total=total,
                lifecycles=lifecycles,
                target_rate=target_rate,
            )
            if phase != phase_names[-1]:
                # Each phase drives its own lifecycles. The last phase's rows
                # stay: they are what the history count below is taken from,
                # and cleanup_queue removes them at the end.
                await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)

        usage_after = float(await conn.fetchval("SELECT pg_notification_queue_usage()"))

        # History is a property of the row writes, not of who was watching:
        # record_jorb_history() fires on INSERT and on UPDATE OF state, and
        # the observed phase's extra `awaited` write touches neither.
        history_rows = int(
            await conn.fetchval(
                "SELECT count(*) FROM jorb_history h JOIN jorb j ON j.id = h.job_id "
                "WHERE j.queue = $1",
                queue,
            )
        )

        unobserved = phases["unobserved"]["per_lifecycle"]
        observed_rate = phases["observed"]["per_lifecycle"]
        result.update(
            {
                "benchmark": "notify",
                "database": target.label,
                "queue": queue,
                "lifecycles": lifecycles,
                "guard": guard,
                # No unqualified "per_lifecycle" key, deliberately: a single
                # name here would silently mean whichever phase the reader
                # assumed, which is exactly the ambiguity this command exists
                # to remove.
                "phases": phases,
                "per_lifecycle_unobserved": unobserved,
                "per_lifecycle_observed": observed_rate,
                "demand_gate_saves_per_lifecycle": observed_rate - unobserved,
                "history_rows_per_lifecycle": (
                    history_rows / lifecycles if lifecycles else 0.0
                ),
                "row_writes_per_lifecycle": float(SCALE_CLAIM_ROW_WRITES_PER_LIFECYCLE),
                "target_rate": target_rate,
                "notify_queue_usage": {
                    "before": usage_before,
                    "peak": max(usage_before, usage_peak),
                    "after": usage_after,
                },
                "scale_md": {
                    "claimed_unobserved": SCALE_CLAIM_NOTIFICATIONS_UNOBSERVED,
                    "claimed_observed": SCALE_CLAIM_NOTIFICATIONS_OBSERVED,
                    "unobserved_agrees": (
                        unobserved == SCALE_CLAIM_NOTIFICATIONS_UNOBSERVED
                    ),
                    "observed_agrees": (
                        observed_rate == SCALE_CLAIM_NOTIFICATIONS_OBSERVED
                    ),
                    "agrees": (
                        unobserved == SCALE_CLAIM_NOTIFICATIONS_UNOBSERVED
                        and observed_rate == SCALE_CLAIM_NOTIFICATIONS_OBSERVED
                    ),
                    "claimed_history_rows_per_lifecycle": (
                        SCALE_CLAIM_HISTORY_ROWS_PER_LIFECYCLE
                    ),
                    "history_agrees": (
                        history_rows
                        == lifecycles * SCALE_CLAIM_HISTORY_ROWS_PER_LIFECYCLE
                    ),
                },
            }
        )
        return result
    finally:
        await listener.close()
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


def _notify_phase(
    counts: dict[str, int],
    foreign: dict[str, int],
    *,
    total: int,
    lifecycles: int,
    target_rate: float,
) -> dict[str, Any]:
    """One phase's counts, in the shape both phases report."""
    per_lifecycle = total / lifecycles if lifecycles else 0.0
    load_bearing = sum(counts[c] for c in LOAD_BEARING_CHANNELS)
    return {
        "per_channel": counts,
        "per_channel_per_lifecycle": {
            channel: (count / lifecycles if lifecycles else 0.0)
            for channel, count in counts.items()
        },
        "foreign_notifications": foreign,
        "total": total,
        "per_lifecycle": per_lifecycle,
        "load_bearing_per_lifecycle": (
            load_bearing / lifecycles if lifecycles else 0.0
        ),
        "projected_notifications_per_second": per_lifecycle * target_rate,
    }


async def _park_idle_worker(conn: asyncpg.Connection, queue: str) -> None:
    """Register one worker parked on ``queue``, the jorb_enqueued gate.

    The gate is ``EXISTS (SELECT 1 FROM jorb_worker WHERE queue = ... AND
    idle AND shutdown_at IS NULL)``, so this row IS the demand signal a real
    idle worker publishes before its last claim attempt. It carries this
    run's queue name, which is what lets ``cleanup_queue`` reach it.
    """
    await conn.execute(
        "INSERT INTO jorb_worker (host, pid, queue, idle) "
        "VALUES ('pj-bench', $1, $2, TRUE)",
        os.getpid(),
        queue,
    )


async def _drive_lifecycles(
    conn: asyncpg.Connection,
    queue: str,
    lifecycles: int,
    job_ids: set[int],
    *,
    observed: bool,
) -> None:
    """Drive the four row writes of a real lifecycle, one txn per write.

    ``observed`` sets ``jorb.awaited`` immediately after the insert, which is
    what ``wait_for_result()`` does and what the jorb_done trigger's WHEN
    clause reads. It is its own statement rather than a column on the INSERT
    because that is the real ordering: the client learns the id, then
    registers. It touches no state column, so it fires nothing itself.
    """
    for _ in range(lifecycles):
        job_id = int(
            await conn.fetchval(
                "INSERT INTO jorb (job_class, kwargs, queue) "
                "VALUES ($1, '{}'::jsonb, $2) RETURNING id",
                BENCH_JOB_CLASS,
                queue,
            )
        )
        job_ids.add(job_id)
        if observed:
            await conn.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)
        await conn.execute(
            "UPDATE jorb SET state = 'claimed', claimed_at = now(), "
            "run_count = run_count + 1, run_epoch = run_epoch + 1, "
            "updated = now() WHERE id = $1",
            job_id,
        )
        await conn.execute(
            "UPDATE jorb SET state = 'running', started = now(), "
            "updated = now() WHERE id = $1",
            job_id,
        )
        await conn.execute(
            "UPDATE jorb SET state = 'finished', finished = now(), "
            "result = '{}'::jsonb, updated = now() WHERE id = $1",
            job_id,
        )


def _payload_is_ours(channel: str, payload: str, queue: str, job_ids: set[int]) -> bool:
    """Attribute a notification to this run, or to other work.

    Every channel carries enough to tell: the wakeup channel carries the
    queue name, and the rest carry a job id this run knows it created.
    """
    if channel == "jorb_enqueued":
        return payload == queue
    if channel == "jorb_cancel":
        return payload.isdigit() and int(payload) in job_ids
    if channel == "schedule_executed":
        return False
    try:
        data = json.loads(payload)
    except ValueError, TypeError:
        return False
    if channel == "jorb_done":
        return int(data.get("id", -1)) in job_ids
    if channel == "jorb_event":
        return int(data.get("job_id", -1)) in job_ids
    return False


async def _drain_notifications(
    listener: asyncpg.Connection,
    counts: dict[str, int],
    timeout: float = 5.0,
    quiet: float = 0.3,
    minimum: float = 1.0,
) -> int:
    """Wait until the notification count stops moving.

    NOTIFY is delivered after commit and asynchronously, so counting
    immediately after the last UPDATE undercounts. Settling on "no new
    message for ``quiet`` seconds" is what makes the per-lifecycle figure a
    fact rather than a race.

    ZERO IS AN ANSWER, and that is why "settled" cannot mean "stopped
    growing after it grew". A demand-gated schema emits nothing at all for
    an unobserved lifecycle, so a loop that refuses to settle until it has
    seen at least one message would burn the whole timeout on every correct
    run and then report the same zero. Instead the loop settles on "nothing
    new for ``quiet``, having watched for at least ``minimum``" — a claim
    about silence that is as checkable as a claim about traffic.
    """
    started = time.monotonic()
    deadline = started + timeout
    last_total = sum(counts.values())
    stable_since = started
    while time.monotonic() < deadline:
        await listener.execute("SELECT 1")
        await asyncio.sleep(0.05)
        total = sum(counts.values())
        now = time.monotonic()
        if total != last_total:
            last_total = total
            stable_since = now
        elif now - stable_since > quiet and now - started > minimum:
            break
    return sum(counts.values())


# =========================================================================
# 5. plans — EXPLAIN every hot query; the CI regression gate
# =========================================================================

CLAIM_PROBE_SQL = """
    SELECT j.id FROM jorb j
     WHERE j.queue = $1
       AND (j.capability = ANY($2::text[]) OR j.capability IS NULL)
       AND j.prio <= $3
       AND j.run_after <= now()
       AND j.state = 'queued'
     ORDER BY j.prio, j.run_after
       FOR UPDATE OF j SKIP LOCKED
     LIMIT 1
"""

#: claim_jorb's max_concurrency check, copied from sql/schema.sql. This one
#: runs INSIDE the per-queue advisory lock, which makes it the most expensive
#: place in the system for a query to be slow: every millisecond it takes is a
#: millisecond no other claimer on that queue can be admitted, so the capped
#: queue's whole ceiling is 1/(this + the claiming UPDATE). It is gated here
#: because count(*) is O(rows matched) however good the index is -- the plan
#: decides only whether that is "rows in flight on this queue" or "every row
#: this queue ever claimed".
CAP_COUNT_SQL = """
    SELECT count(*) FROM jorb
     WHERE queue = $1 AND state IN ('claimed', 'running')
"""

METRICS_COMPLETIONS_SQL = f"""
    SELECT count(*) AS terminal_count,
           count(*) FILTER (WHERE state = 'finished') AS finished_count
    FROM jorb
    WHERE state IN ({TERMINAL_STATES_SQL})
      AND COALESCE(finished, updated) >= $1
"""

METRICS_ARRIVALS_SQL = """
    SELECT state, count(*) AS count
    FROM jorb
    WHERE created >= $1
    GROUP BY state
"""


@dataclass(frozen=True)
class HotQuery:
    """One query that runs on a timer against the accumulated table."""

    key: str
    what: str
    sql: str
    args: Callable[[str], list[Any]]
    #: Relations a sequential scan of is a FAILURE. Per-query rather than a
    #: global "jorb", because the sweeps added since this gate was written
    #: read jorb_dag, jorb_worker, jorb_mailbox and jorb_schedule_log -- a
    #: gate hardcoded to one table certifies those as healthy while they scan.
    tables: tuple[str, ...] = ("jorb",)
    #: Rows this plan may read and throw away. NOT optional, and rarely large:
    #: an index scan that discards everything it reads is not a sequential
    #: scan, costs the same, and passes a seq-scan check. Where a sweep
    #: legitimately walks past rows it cannot take, the budget says how many
    #: and the comment says why -- always a multiple of the BATCH, never of
    #: the table.
    max_rows_removed: int = 0
    #: Statements that write are explained inside a transaction that is
    #: rolled back, so EXPLAIN ANALYZE measures the real plan without
    #: changing any rows.
    mutating: bool = False
    #: The ``monitor.SWEEP_*_SQL`` constant this case covers, if any. What
    #: makes the gate's coverage checkable rather than a matter of trust.
    sweep: str | None = None


#: The batch every gated sweep is explained with: the monitor's own default,
#: so a discard budget can be stated as "a batch's worth" and stay true.
PLAN_BATCH = 1000

#: "Nothing has expired" — the steady state, run every cycle forever, and the
#: most expensive way for a sweep to say nothing.
CAUGHT_UP = datetime.timedelta(days=3650)

#: "Everything has expired" — first run against an install that has never
#: swept, and the state the mailbox sweep's sequential scan hid in: its
#: caught-up plan was a clean two-buffer probe, and the scan only appeared
#: once the sweep had rows to delete. Gating one state and not the other is
#: how that survived review.
BACKLOG = datetime.timedelta(0)


@dataclass(frozen=True)
class SweepGate:
    """How the plan gate exercises one of ``monitor.py``'s sweeps.

    Keyed by CONSTANT NAME in `SWEEP_GATES`, and checked against the
    constants ``monitor.py`` actually defines — see `sweep_queries`.
    """

    what: str
    #: Sequentially scanning any of these fails the gate. The sweep's own
    #: target table, plus whatever it joins or probes.
    tables: tuple[str, ...]
    #: Discard budgets for the caught-up and backlog states.
    caught_up_discards: int = 0
    backlog_discards: int = 0
    #: False for the timeout sweep, whose only parameter is the batch size.
    takes_window: bool = True
    #: Only the two requeue sweeps write; the rest probe and let the monitor
    #: delete by key in a second statement.
    mutating: bool = False


#: Every sweep, and how to run it. Hand-written — the parameters and the
#: discard budgets cannot be derived — but NOT a hand-maintained list of what
#: to gate: `sweep_queries` compares these keys against the ``SWEEP_*_SQL``
#: constants ``monitor.py`` defines and refuses to run if either side has an
#: entry the other lacks. Adding a sweep without gating it is therefore a
#: hard error at gate time rather than a silent gap, which is the failure
#: this arrangement exists to prevent: three sweeps were added and none was
#: gated, and the gate went on reporting success.
SWEEP_GATES: dict[str, SweepGate] = {
    "SWEEP_TIMED_OUT_SQL": SweepGate(
        "monitor timeout sweep (running jobs past their deadline)",
        ("jorb",),
        takes_window=False,
    ),
    "SWEEP_DEAD_WORKER_JOBS_SQL": SweepGate(
        "monitor dead-worker requeue (in-flight jobs of stale workers)",
        ("jorb", "jorb_worker"),
        mutating=True,
        # It walks the in-flight set and probes each job's worker, discarding
        # the ones still beating -- bounded by work in flight, never by the
        # table. That is true in BOTH states: caught up, every probe is a
        # discard, which is the honest cost of asking.
        caught_up_discards=PLAN_IN_FLIGHT_BUDGET,
        backlog_discards=PLAN_IN_FLIGHT_BUDGET,
    ),
    "SWEEP_UNREGISTERED_CLAIMS_SQL": SweepGate(
        "monitor unregistered-claim requeue (claimed with no registry row)",
        ("jorb",),
        mutating=True,
        # Same index, and here the discards are claimed rows that DO have a
        # registry reference -- the overwhelmingly normal case.
        backlog_discards=PLAN_IN_FLIGHT_BUDGET,
    ),
    "SWEEP_EXPIRED_JOBS_SQL": SweepGate(
        "monitor job retention sweep (every cycle, forever)",
        ("jorb",),
    ),
    "SWEEP_CHECKPOINT_JOBS_SQL": SweepGate(
        "monitor checkpoint retention sweep",
        ("jorb", "jorb_step"),
        # Most terminal jobs have no checkpoints, so filling a batch means
        # walking past the ones that do not: a multiple of the batch, set by
        # the seeded checkpointed fraction, and independent of table size.
        # One job in PLAN_STEP_EVERY checkpoints and three in four are
        # terminal, so a batch costs about 4x itself.
        backlog_discards=(PLAN_STEP_EVERY + 1) * PLAN_BATCH,
    ),
    "SWEEP_MAILBOX_SQL": SweepGate(
        "monitor consumed-mailbox sweep",
        ("jorb_mailbox",),
    ),
    "SWEEP_ORPHANED_DAGS_SQL": SweepGate(
        "monitor orphaned-DAG sweep (a wrong answer, not just storage)",
        ("jorb_dag", "jorb"),
        # DAGs that still hold a job are refused, and the sweep walks past
        # them to fill a batch.
        backlog_discards=PLAN_DAG_EVERY * PLAN_BATCH,
    ),
    "SWEEP_SCHEDULE_LOG_SQL": SweepGate(
        "monitor schedule-log sweep (the one table with no other bound)",
        ("jorb_schedule_log",),
        # Each schedule's newest execution is kept at any age, so the
        # discards are bounded by the number of SCHEDULES, not the log.
        backlog_discards=PLAN_SCHEDULES,
    ),
    "SWEEP_RETIRED_WORKERS_SQL": SweepGate(
        "monitor retired-worker sweep (grows with deploys, forever)",
        ("jorb_worker",),
    ),
}


def monitor_sweeps() -> dict[str, str]:
    """Every ``SWEEP_*_SQL`` statement ``monitor.py`` defines, by name.

    DISCOVERED from the module, never listed here. The gate's whole value is
    that it certifies the statements the monitor actually runs, and a
    hand-copied roster of them is the one part that can go stale without
    anybody noticing — which is exactly what happened: retention grew three
    sweeps, the roster kept four, and the gate reported success.
    """
    return {
        name: sql
        for name, sql in vars(monitor).items()
        if name.startswith("SWEEP_") and name.endswith("_SQL") and isinstance(sql, str)
    }


def _batch_only_args(_queue: str) -> list[Any]:
    """Parameters for the one sweep that takes no time window."""
    return [PLAN_BATCH]


def _window_args(window: datetime.timedelta) -> Callable[[str], list[Any]]:
    """Bind `window` NOW. A closure over the loop variable would hand every
    case the last window built, so the whole gate would measure one state."""

    def args(_queue: str) -> list[Any]:
        return [window, PLAN_BATCH]

    return args


def sweep_queries() -> tuple[HotQuery, ...]:
    """A gate case per sweep per state, derived from the sweeps themselves.

    Raises rather than skipping when `SWEEP_GATES` and ``monitor.py`` disagree
    in either direction. A sweep with no gate entry is the gap this replaced;
    a gate entry for a sweep that no longer exists is a case certifying
    nothing, which reads exactly like coverage.
    """
    sweeps = monitor_sweeps()
    ungated = sorted(set(sweeps) - set(SWEEP_GATES))
    if ungated:
        raise RuntimeError(
            f"monitor.py defines sweeps the plan gate does not run: "
            f"{', '.join(ungated)}. Add an entry to SWEEP_GATES — a gate that "
            f"silently covers a subset is worse than no gate, because it is "
            f"trusted."
        )
    stale = sorted(set(SWEEP_GATES) - set(sweeps))
    if stale:
        raise RuntimeError(
            f"SWEEP_GATES names sweeps monitor.py no longer defines: "
            f"{', '.join(stale)}. Remove them; a case that gates nothing "
            f"still counts as coverage to whoever reads the output."
        )

    queries: list[HotQuery] = []
    for name, sql in sorted(sweeps.items()):
        gate = SWEEP_GATES[name]
        key = name.removeprefix("SWEEP_").removesuffix("_SQL").lower()
        if not gate.takes_window:
            queries.append(
                HotQuery(
                    key,
                    gate.what,
                    sql,
                    _batch_only_args,
                    tables=gate.tables,
                    max_rows_removed=gate.caught_up_discards,
                    mutating=gate.mutating,
                    sweep=name,
                )
            )
            continue
        for suffix, window, budget in (
            ("", CAUGHT_UP, gate.caught_up_discards),
            ("_backlog", BACKLOG, gate.backlog_discards),
        ):
            queries.append(
                HotQuery(
                    key + suffix,
                    gate.what + (" — full backlog" if suffix else " — caught up"),
                    sql,
                    _window_args(window),
                    tables=gate.tables,
                    max_rows_removed=budget,
                    mutating=gate.mutating,
                    sweep=name,
                )
            )
    return tuple(queries)


def hot_queries() -> tuple[HotQuery, ...]:
    """The queries whose plan is load-bearing, and why each one is here.

    The monitor's sweeps come from `sweep_queries`, which reads them off
    ``monitor.py`` itself: a benchmark that gates CI on a query's plan has to
    run the query the monitor runs, and a copy passes review, drifts on the
    next edit, and then certifies a statement nobody executes.
    """

    def claim_args(queue: str) -> list[Any]:
        return [queue, ["bench"], 1000]

    def window_args(_queue: str) -> list[Any]:
        return [db.utcnow() - datetime.timedelta(hours=1)]

    return (
        HotQuery(
            "claim",
            "claim_jorb's claimable-row probe (every worker poll)",
            CLAIM_PROBE_SQL,
            claim_args,
        ),
        HotQuery(
            "concurrency_cap",
            "claim_jorb's max_concurrency count, inside the serialised section",
            CAP_COUNT_SQL,
            lambda queue: [queue],
            # It reads the in-flight index and discards other queues' rows.
            # That is the cost of the count being per-queue, and it is bounded
            # by work in flight everywhere, never by the size of jorb.
            max_rows_removed=PLAN_IN_FLIGHT_BUDGET,
        ),
        HotQuery(
            "schedule_concurrency",
            "the scheduler's max_concurrent_jobs count, once per firing",
            CONCURRENCY_COUNT_SQL,
            # Zero discards is the whole point of this one. Its index is
            # partial on the LIVE states as well as on schedule_id -- without
            # that second clause it still reports no seq scan while counting
            # and discarding every job the schedule has ever created.
            lambda queue: [1],
            max_rows_removed=0,
        ),
        HotQuery(
            "metrics_completions",
            "/metrics completion window (terminal jobs in the window)",
            METRICS_COMPLETIONS_SQL,
            window_args,
        ),
        HotQuery(
            "metrics_arrivals",
            "/metrics arrival window (jobs created in the window)",
            METRICS_ARRIVALS_SQL,
            window_args,
        ),
        *sweep_queries(),
    )


def walk_plan(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def summarize_plan(document: dict[str, Any], query: HotQuery) -> dict[str, Any]:
    """Access method, index, and buffers — the three facts that matter.

    Buffers rather than milliseconds: a duration says how fast the machine
    is, a buffer count says how much of the table the query had to read,
    and only the second one stays true on someone else's hardware.

    The buffer count comes from the ROOT node, not from summing the tree:
    EXPLAIN's per-node buffer counts are cumulative, so a sum counts every
    child once per ancestor and reports a query as several times more
    expensive than it is.

    Sequential scans are looked for on the relations THIS query is gated on,
    not on a hardcoded ``jorb``. The sweeps read jorb_dag, jorb_worker,
    jorb_mailbox and jorb_schedule_log, and a scan of any of them grows with
    DAGs, deploys, mail and cron rate exactly as a scan of jorb grows with
    jobs.
    """
    plan = document["Plan"]
    nodes = list(walk_plan(plan))
    scans: list[dict[str, Any]] = []
    seq_scans: list[str] = []
    buffers = int(plan.get("Shared Hit Blocks", 0)) + int(
        plan.get("Shared Read Blocks", 0)
    )
    for node in nodes:
        node_type = str(node.get("Node Type", ""))
        relation = node.get("Relation Name")
        if "Scan" not in node_type:
            continue
        # Multiplied by the loop count, because EXPLAIN reports both of these
        # PER LOOP. The inner side of a nested loop that discards one row on
        # each of two hundred passes reports "1", and a budget compared
        # against that number is comparing against a two-hundredth of the
        # work actually done.
        loops = max(int(node.get("Actual Loops", 1)), 1)
        scans.append(
            {
                "node": node_type,
                "relation": relation,
                "index": node.get("Index Name"),
                "loops": loops,
                "rows_removed_by_filter": (
                    int(node.get("Rows Removed by Filter", 0)) * loops
                ),
                "actual_rows": int(node.get("Actual Rows", 0)) * loops,
            }
        )
        if node_type == "Seq Scan" and relation in query.tables:
            seq_scans.append(f"{node_type} on {relation}")
    indexes = sorted({str(s["index"]) for s in scans if s["index"]})
    # Per-node and therefore genuinely additive, unlike buffers. This is the
    # "read the whole table to return nothing" tell: a plan can use an index,
    # stay off the seq-scan gate, and still discard every row in the table
    # because the index it chose was the wrong one.
    discarded = sum(int(s["rows_removed_by_filter"]) for s in scans)
    return {
        "access_methods": sorted({str(s["node"]) for s in scans}),
        "indexes": indexes,
        "buffers": buffers,
        "scans": scans,
        "gated_tables": list(query.tables),
        "seq_scans": seq_scans,
        "rows_removed_by_filter": discarded,
        "max_rows_removed": query.max_rows_removed,
        "over_discard_budget": discarded > query.max_rows_removed,
        "sweep": query.sweep,
        "planning_ms": float(document.get("Planning Time", 0.0)),
        "execution_ms": float(document.get("Execution Time", 0.0)),
    }


_SETTING_RE = re.compile(r"^[a-z_]+$")
_SETTING_VALUE_RE = re.compile(r"^[A-Za-z0-9._]+$")


async def _apply_planner_settings(
    conn: asyncpg.Connection, settings: Sequence[str]
) -> dict[str, str]:
    """Apply ``name=value`` planner settings to this connection only.

    An operator's "what would this cost without the index?" question, and
    the only honest way to prove the seq-scan gate actually fires. Session
    scoped: the connection is closed when the command ends, so nothing
    persists.
    """
    applied: dict[str, str] = {}
    for setting in settings:
        name, _, value = setting.partition("=")
        name, value = name.strip(), value.strip()
        if not _SETTING_RE.match(name) or not _SETTING_VALUE_RE.match(value):
            fail(
                f"Invalid --planner-setting {setting!r}",
                "Expected name=value, e.g. enable_indexscan=off",
            )
        await conn.execute(f"SET {name} = {value}")
        applied[name] = value
    return applied


async def seed_plan_data(
    conn: asyncpg.Connection, queue: str, rows: int
) -> dict[str, int]:
    """Populate enough rows that the planner has a real choice.

    A plan measured against an empty table proves nothing: a sequential
    scan of a tiny table is genuinely the cheapest plan, so the gate would
    pass for the wrong reason. Jobs are spread over 60 days and across
    terminal and live states so a reporting window covers a slice rather
    than the whole table.

    A small FIXED number of them are then put in flight, because one of the
    gated queries is the concurrency-cap count and it reads exactly those
    rows. The number is fixed rather than proportional on purpose: in-flight
    work is bounded by the workers and the cap, not by the table, so a queue
    holds tens or hundreds of ``claimed``/``running`` rows whether the table
    has twenty thousand or twenty million. Seeding a *fraction* instead would
    make the count match a third of the table, the planner would rightly
    choose a sequential scan, and the gate would report a design flaw that
    only its own seeding created. ``claimed_at`` is set with them: a real
    claimed row always has one, and without it the row is missing from an
    index the planner may want.

    Every table a gated sweep reads is seeded at the same scale, not just
    ``jorb``: the DAG, schedule-log and worker-registry sweeps grow with
    deploys, cron rate and DAG count rather than with job throughput, and a
    plan measured against three rows in those tables certifies a sequential
    scan as healthy — which is what a seq-scan gate is for.
    """
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, created, finished, updated)
        SELECT $1, '{}'::jsonb, $2,
               (ARRAY['finished','crashed','cancelled','queued'])[1 + (i % 4)]::jorbstate,
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day'
        FROM generate_series(1, $3::int) i
        """,
        BENCH_JOB_CLASS,
        queue,
        rows,
    )
    in_flight = min(PLAN_IN_FLIGHT, max(rows // 10, 1))
    # Split claimed/running by ROW NUMBER, never by `id % 2`: the seed above
    # makes every 4th row queued, so the rows this picks have ids four apart
    # and all share a parity -- `id % 2` silently puts every one of them in
    # the same state, and the sweeps that read the other one then plan
    # against an empty set and certify nothing.
    #
    # `timeout_at` goes on the running half, past its deadline, because that
    # is what the timeout sweep reads: without it that sweep stops at its
    # first index entry and its plan proves nothing either.
    await conn.execute(
        """
        UPDATE jorb SET state = CASE WHEN picked.n % 2 = 0 THEN 'claimed'
                                     ELSE 'running' END::jorbstate,
                        claimed_at = now(),
                        started = CASE WHEN picked.n % 2 = 1 THEN now() END,
                        timeout_at = CASE WHEN picked.n % 2 = 1
                                          THEN now() - interval '1 minute' END
          FROM (SELECT id, row_number() OVER (ORDER BY id) AS n
                  FROM jorb WHERE queue = $1 AND state = 'queued'
                 ORDER BY id LIMIT $2) picked
         WHERE jorb.id = picked.id
        """,
        queue,
        in_flight,
    )
    job_ids = await conn.fetch(
        "SELECT id FROM jorb WHERE queue = $1 ORDER BY id LIMIT 200", queue
    )
    anchor = int(job_ids[0]["id"])
    # One job in PLAN_STEP_EVERY gets checkpoints, and the checkpointed
    # FRACTION is what the sweep's cost turns on: it walks past step-less
    # jobs to fill a batch, so capping the insert at a flat few thousand
    # rows (as this once did) leaves one job in twelve checkpointed and
    # reports the sweep discarding ten thousand rows — a number produced
    # entirely by the seeding, not by the query.
    steps = int(
        await conn.fetchval(
            """
            WITH inserted AS (
                INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
                SELECT j.id, i, 'bench', '{}'::jsonb, 0
                  FROM (SELECT id, row_number() OVER (ORDER BY id) AS n
                          FROM jorb WHERE queue = $1) j,
                       generate_series(1, $2) i
                 WHERE j.n % $3 = 0
                RETURNING 1
            )
            SELECT count(*) FROM inserted
            """,
            queue,
            PLAN_STEPS_PER_JOB,
            PLAN_STEP_EVERY,
        )
    )
    mailbox = min(rows, 20000)
    await conn.execute(
        """
        INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
        SELECT $1, 'bench', '{}'::jsonb,
               CASE WHEN i % 10 = 0 THEN NULL
                    ELSE now() - (i % 30) * interval '1 day' END
        FROM generate_series(1, $2::int) i
        """,
        anchor,
        mailbox,
    )
    # DAGs are named for this run's queue, which is the only handle cleanup
    # has on them: jorb_dag carries no queue of its own.
    await conn.execute(
        """
        INSERT INTO jorb_dag (name, created)
        SELECT $1, now() - (i % 60) * interval '1 day'
        FROM generate_series(1, $2::int) i
        """,
        queue,
        rows,
    )
    dags_with_jobs = await conn.fetchval(
        """
        WITH paired AS (
            SELECT j.id AS job_id, d.id AS dag_id
              FROM (SELECT id, row_number() OVER (ORDER BY id) AS n
                      FROM jorb WHERE queue = $1) j
              JOIN (SELECT id, row_number() OVER (ORDER BY id) AS n
                      FROM jorb_dag WHERE name = $1) d ON d.n = j.n
             WHERE j.n % $2 <> 0
        )
        UPDATE jorb SET dag_id = paired.dag_id
          FROM paired WHERE jorb.id = paired.job_id
        RETURNING (SELECT count(*) FROM paired)
        """,
        queue,
        PLAN_DAG_EVERY,
    )
    await conn.execute(
        """
        INSERT INTO jorb_schedule (name, job_class, queue, cron_expr, next_run)
        SELECT $1 || '_' || i, $2, $1, '* * * * *', now()
        FROM generate_series(1, $3::int) i
        """,
        queue,
        BENCH_JOB_CLASS,
        PLAN_SCHEDULES,
    )
    await conn.execute(
        """
        INSERT INTO jorb_schedule_log (schedule_id, schedule_name,
                                       scheduled_time, actual_time, result)
        SELECT s.id, s.name,
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               'success'
          FROM generate_series(1, $2::int) i
          JOIN (SELECT id, name, row_number() OVER (ORDER BY id) AS n
                  FROM jorb_schedule WHERE queue = $1) s
            ON s.n = 1 + (i % $3)
        """,
        queue,
        rows,
        PLAN_SCHEDULES,
    )
    # Retired workers for the registry sweep, plus a handful of live ones so
    # the dead-worker requeue has rows to JOIN to rather than an empty table.
    await conn.execute(
        """
        INSERT INTO jorb_worker (host, pid, queue, started, last_seen,
                                 shutdown_at)
        SELECT 'bench-plans', i, $1,
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               CASE WHEN i > $3 THEN now() - (i % 60) * interval '1 day' END
        FROM generate_series(1, $2::int) i
        """,
        queue,
        rows,
        PLAN_LIVE_WORKERS,
    )
    # The in-flight jobs point at the live workers, so the dead-worker sweep
    # plans a real join instead of one whose inner side is always empty.
    await conn.execute(
        """
        UPDATE jorb SET claimed_by = w.id
          FROM (SELECT id, row_number() OVER (ORDER BY id) AS n
                  FROM jorb_worker WHERE queue = $1 AND shutdown_at IS NULL) w
         WHERE jorb.queue = $1
           AND jorb.state IN ('claimed', 'running')
           AND w.n = 1 + (jorb.id % $2)
        """,
        queue,
        PLAN_LIVE_WORKERS,
    )
    # VACUUM as well as ANALYZE, and not as a nicety: ANALYZE gives the
    # planner its statistics, but only VACUUM sets the VISIBILITY MAP, and
    # an index-only or bitmap plan is costed as though every tuple needed a
    # heap fetch until it is set. On a freshly seeded (or repeatedly
    # re-seeded, and therefore bloated) table that inflates the index plans
    # until a sequential scan wins -- and this gate then reports a sequential
    # scan for a query whose index is perfectly healthy. Autovacuum
    # does both continuously in production; a gate that skips them measures
    # a table no running system ever has.
    for table in (
        "jorb",
        "jorb_step",
        "jorb_mailbox",
        "jorb_dag",
        "jorb_schedule",
        "jorb_schedule_log",
        "jorb_worker",
    ):
        await conn.execute(f"VACUUM (ANALYZE) {table}")  # noqa: S608 - literal
    return {
        "jobs": rows,
        "steps": steps,
        "mailbox": mailbox,
        "in_flight": in_flight,
        "dags": rows,
        "dags_with_jobs": int(dags_with_jobs or 0),
        "schedules": PLAN_SCHEDULES,
        "schedule_log": rows,
        "workers": rows,
    }


async def run_plans(
    target: Target,
    *,
    seed: int,
    planner_settings: Sequence[str],
    max_existing_jobs: int,
    force: bool,
) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS) every hot query and gate on its plan.

    This is the CI-runnable half of the harness. Timings flake on a loaded
    box and pass on a fast one with the index dropped; a plan is a fact, and
    "did this query stop using its index" is the regression that stays
    correct while getting slower forever.

    TWO verdicts, because one of them is not enough. A sequential scan is the
    obvious failure; the quiet one is an INDEX scan that reads a table's worth
    of rows and discards them, which is not a Seq Scan node, costs the same,
    and passes any seq-scan check. Both fail the run.
    """
    queue = bench_queue("plans")
    conn = await open_connection(target)
    queries: dict[str, Any] = {}
    result: dict[str, Any] = {}
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)
        applied = await _apply_planner_settings(conn, planner_settings)
        seeded = await seed_plan_data(conn, queue, seed)

        for query in hot_queries():
            args = query.args(queue)
            explain = f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) {query.sql}"
            if query.mutating:
                tx = conn.transaction()
                await tx.start()
                try:
                    raw = await conn.fetchval(explain, *args)
                finally:
                    await tx.rollback()
            else:
                raw = await conn.fetchval(explain, *args)
            document = json.loads(raw) if isinstance(raw, str) else raw
            summary = summarize_plan(document[0], query)
            summary["what"] = query.what
            queries[query.key] = summary

        offenders = sorted(k for k, v in queries.items() if v["seq_scans"])
        discarders = sorted(k for k, v in queries.items() if v["over_discard_budget"])
        result.update(
            {
                "benchmark": "plans",
                "database": target.label,
                "queue": queue,
                "seed_rows": seed,
                "seeded": seeded,
                "guard": guard,
                "planner_settings": applied,
                "queries": queries,
                # Which sweeps this run certified, stated rather than implied:
                # a reader can check the coverage against monitor.py without
                # trusting the harness to have kept up.
                "sweeps_gated": sorted(
                    {v["sweep"] for v in queries.values() if v["sweep"]}
                ),
                "seq_scan_offenders": offenders,
                "discard_offenders": discarders,
                "healthy": not offenders and not discarders,
            }
        )
        return result
    finally:
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


# =========================================================================
# 6. resolve — what turning a dotted path into a class costs, per job
# =========================================================================

#: A worker resolves ``jorb.job_class`` — a dotted path on every job row — to
#: a class object once per job, in ``JobSystem.resolve_job_class``. That
#: result is cached, and with the ``--reload`` dev flag the resolver also
#: stats the module's source file so an edit takes effect on the next job.
#:
#: Neither number had ever been measured. The cache was added for
#: CORRECTNESS — an unconditional ``importlib.reload`` was re-executing
#: decorators between jobs and breaking Hypothesis tests — and "so turn
#: --reload off in production" has been advice rather than a finding ever
#: since. These arms make both of them numbers.
#:
#: WHY NOT A ``--reload`` VARIANT OF ``pj-bench e2e``, which is where this
#: cost actually lands: resolution happens once per job inside
#: claim→finished, so e2e is the honest WINDOW and a dishonest INSTRUMENT.
#: e2e's own round-to-round spread is percent-scale on a p50 of
#: milliseconds; the effect here is microseconds, three to four orders of
#: magnitude under that noise floor. An e2e comparison could only ever
#: return "no difference", and "no difference" there is indistinguishable
#: from "the flag was never passed to the workers" and from "the harness is
#: broken" — a measurement with no failure mode is not a measurement.
#: Timing the call directly returns a number with a spread, and the e2e p50
#: is then what turns it into a share of a job. ``pj-bench e2e --reload``
#: exists as a smell test (a real fleet, flag on), explicitly not as an arm.


#: The throwaway jobs module ``pj-bench resolve`` re-imports. It is written
#: to a temporary directory, put on ``sys.path``, and both are removed in a
#: ``finally`` — the same shape as ``worker_config()`` above, and for the
#: same reason: the subject is a DEVELOPER'S jobs file and the installed
#: package does not contain one to borrow. Reloading ``pyjobby.bench``
#: instead would re-execute the module this benchmark is running inside,
#: rebinding its own globals mid-run.
#:
#: ``importlib.reload`` re-executes ONLY this module — everything it imports
#: is already in ``sys.modules`` and costs a dict lookup — so the reload arm
#: is set by the top-level statements here and by nothing else. Four small
#: classes is a modest jobs file. A module that defines forty job classes,
#: or builds a client at import time, pays more. **Read the reload arm as a
#: floor, not as a typical cost.**
RESOLVE_JOB_SOURCE = '''\
"""Generated by `pj-bench resolve`; safe to delete."""

from pyjobby.pj import Job

DEFAULT_N = 0


class ResolveBenchJob(Job):
    """The class `pj-bench resolve` resolves over and over."""

    async def task(self, n: int = DEFAULT_N) -> dict[str, int]:
        return {"n": n}


class ResolveBenchJobTwo(Job):
    async def task(self, n: int = DEFAULT_N) -> dict[str, int]:
        return {"n": n}


class ResolveBenchJobThree(Job):
    async def task(self, n: int = DEFAULT_N) -> dict[str, int]:
        return {"n": n}


class ResolveBenchJobFour(Job):
    async def task(self, n: int = DEFAULT_N) -> dict[str, int]:
        return {"n": n}
'''

#: A reload costs orders of magnitude more than a cached lookup, so running
#: it as many times as the cheap arms would spend the whole run inside one
#: arm and force everyone's ``--repeat`` down to fit. Per-arm iteration
#: counts are safe here because every arm is reduced to a PER-RESOLUTION
#: cost before anything is compared.
DEFAULT_RESOLVE_RELOADS = 500


@dataclass(frozen=True)
class ResolveModule:
    """The generated jobs module, and proof it was cleaned up afterwards."""

    module_name: str
    klass: str
    directory: str

    def cleanup_report(self) -> dict[str, Any]:
        """What ``resolve`` removed — asserted by tests, not just claimed.

        Three separate places have to be put back, and leaving any one of
        them behind leaks into the calling process rather than into the
        database: the directory, the ``sys.path`` entry, and the imported
        module object. ``jobs_deleted``/``workers_deleted`` are here and
        always zero because this subcommand writes no rows at all, and a
        consumer reading every benchmark's ``cleanup`` block should get the
        same keys back rather than a KeyError.
        """
        return {
            "jobs_deleted": 0,
            "workers_deleted": 0,
            "job_module_dir": self.directory,
            "job_module_removed": not os.path.exists(self.directory),
            "off_sys_path": self.directory not in sys.path,
            "out_of_sys_modules": self.module_name not in sys.modules,
        }


@contextlib.contextmanager
def resolve_job_module() -> Iterator[ResolveModule]:
    """Write, import-enable, and afterwards fully remove a jobs module.

    The module name carries a random suffix so a second run in the same
    interpreter (the test suite does exactly this) cannot resolve the first
    run's cached module object and measure nothing.
    """
    directory = tempfile.mkdtemp(prefix="pjbench_jobs_")
    module_name = f"pjbench_jobs_{uuid.uuid4().hex[:8]}"
    module = ResolveModule(
        module_name=module_name,
        klass=f"{module_name}.ResolveBenchJob",
        directory=directory,
    )
    try:
        with open(os.path.join(directory, f"{module_name}.py"), "w") as handle:
            handle.write(RESOLVE_JOB_SOURCE)
        sys.path.insert(0, directory)
        # The import system caches a directory's listing by mtime with
        # one-second granularity, so a file written into a directory it has
        # already scanned this second is invisible without this.
        importlib.invalidate_caches()
        try:
            yield module
        finally:
            with contextlib.suppress(ValueError):
                sys.path.remove(directory)
            sys.modules.pop(module_name, None)
            importlib.invalidate_caches()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@dataclass(frozen=True)
class ResolveArm:
    """One branch of ``resolve_job_class``, and what it means in production.

    ``drop_cache`` and ``stale_mtime`` are how an arm forces the resolver
    down its branch on every call: dropping the cache entry is a worker with
    no class cache at all, and zeroing the recorded mtime is exactly what
    the resolver reads as "the source file moved" — without a ``utime()``
    syscall inside the timed loop, which would be measured as though it were
    part of the reload.
    """

    key: str
    what: str
    means: str
    reload_jobs: bool
    drop_cache: bool
    stale_mtime: bool
    reloads: bool


RESOLVE_ARMS = (
    ResolveArm(
        key="cached",
        what="--reload off, class already resolved: a dict hit",
        means="what a production worker pays per job today",
        reload_jobs=False,
        drop_cache=False,
        stale_mtime=False,
        reloads=False,
    ),
    ResolveArm(
        key="reload_check",
        what="--reload on, module untouched: the mtime CHECK and no import",
        means="what a production worker with --reload left on pays per job",
        reload_jobs=True,
        drop_cache=False,
        stale_mtime=False,
        reloads=False,
    ),
    ResolveArm(
        key="uncached",
        what="no class cache: pydoc.locate() per job, module already imported",
        means="what a refactor that dropped _class_cache would cost per job",
        reload_jobs=False,
        drop_cache=True,
        stale_mtime=False,
        reloads=False,
    ),
    ResolveArm(
        key="reload_fire",
        what="--reload on and the source looks edited: importlib.reload plus "
        "pydoc.locate, every call",
        means="what a developer pays on the first job after each edit — and "
        "what the pre-cache resolver paid on EVERY job",
        reload_jobs=True,
        drop_cache=False,
        stale_mtime=True,
        reloads=True,
    ),
)


def _resolve_round(arm: ResolveArm, klass: str, resolutions: int) -> dict[str, Any]:
    """One arm, one round: ``resolutions`` calls of the real resolver.

    The first resolution happens OUTSIDE the timer, deliberately. It is the
    cold one — source read off disk, compiled, executed — and no arm here is
    measuring that: ``cached`` and ``reload_check`` are steady-state
    questions, and ``uncached`` and ``reload_fire`` re-pay their own cost
    inside every iteration by construction.

    The two ``if``s in the loop are constant per arm and present in all four
    arms, so the loop's own overhead is identical everywhere and cancels out
    of every difference this subcommand reports. They are inside the timer
    because the alternative is timing each call individually, and a
    ``perf_counter`` pair costs more than the entire cached call.

    ``re_imported`` is how an arm PROVES it drove the branch it is named
    after: ``importlib.reload`` rebuilds the class object, so a round that
    really re-imported ends holding a different class than it started with,
    while re-locating a module that is already in ``sys.modules`` hands back
    the same object. An arm that quietly stopped reloading would otherwise
    measure the mtime check and publish it as the cost of an import — wrong
    by two orders of magnitude, with nothing failing.
    """
    system = JobSystem(
        dsn={},
        qname="pjbench_resolve",
        capabilities=("pjbench",),
        workerId=0,
        reload_jobs=arm.reload_jobs,
    )
    primed = system.resolve_job_class(klass)

    # Private on purpose: these two dicts ARE the mechanism under test, and
    # the arms exist to drive the resolver down each of its branches.
    cache = system._class_cache
    mtimes = system._class_mtimes
    drop = arm.drop_cache
    stale = arm.stale_mtime

    started = time.perf_counter()
    for _ in range(resolutions):
        if drop:
            cache.pop(klass, None)
        if stale:
            mtimes[klass] = 0.0
        system.resolve_job_class(klass)
    elapsed = time.perf_counter() - started
    return {
        "seconds": elapsed,
        "resolutions": resolutions,
        # Outside the timer, and a plain identity check: see the docstring.
        "re_imported": system.resolve_job_class(klass) is not primed,
    }


def _resolve_arm_report(
    arm: ResolveArm, rounds: Sequence[dict[str, Any]], resolutions: int
) -> dict[str, Any]:
    summary = summarize([r["seconds"] for r in rounds])
    per_resolution = summary["median"] / resolutions if resolutions else 0.0
    return {
        "what": arm.what,
        "means": arm.means,
        "resolutions": resolutions,
        # Measured, not declared: whether the class object actually changed
        # under this arm. It must equal ``arm.reloads``, and tests assert
        # that it does -- an arm whose name and behaviour drifted apart is
        # how a benchmark starts publishing the wrong number silently.
        "re_imported": bool(rounds) and all(r["re_imported"] for r in rounds),
        "declares_reimport": arm.reloads,
        "seconds": summary,
        "per_resolution_us": per_resolution * 1e6,
        # The spread belongs to the per-resolution figure too: it is the same
        # sample divided by a constant, and a median quoted without it is
        # the failure mode docs/TESTING.md rule 3 is about.
        "spread_pct": summary["spread_pct"],
        # If resolution were the only thing a worker did. Not a throughput
        # claim — a ceiling, and the only form in which a per-call cost can
        # be compared against a jobs/second requirement at all.
        "implied_ceiling_jobs_per_second": (
            1.0 / per_resolution if per_resolution else 0.0
        ),
        "pct_of_reference_job_budget": per_resolution * SCALE_TARGET_RATE * 100.0,
    }


async def run_resolve(
    target: Target,
    *,
    resolutions: int,
    reloads: int,
    repeat: int,
    warmup: bool,
    max_existing_jobs: int,
    force: bool,
) -> dict[str, Any]:
    """Per-job class resolution: cached, reload-checking, and reloading.

    This is the one subcommand that writes nothing to the database, and it
    still opens a connection and runs the busy-database guard. That is not
    ceremony: it is a CPU measurement, and a database busy running real work
    is a box whose CPU it would be competing for — which lands on the ratio
    just as row contention would. ``--force`` still overrides, loudly.

    WHAT THE WARM-UP ROUND DOES AND DOES NOT ABSORB, because here that
    matters more than usual: import cost is the subject, not the noise.

    * It absorbs the interpreter's warm-up — the generated module's first
      compile to bytecode, its first read off a cold page cache, and CPython
      specialising the code paths involved.
    * It does NOT absorb the import cost being measured. ``uncached`` and
      ``reload_fire`` re-pay theirs on every iteration by construction, so
      there is nothing for a warm-up to amortise away.
    * Neither does it stand between any arm and a cold first touch: each
      round primes the cache outside its own timer, so no measured window
      ever contains the very first import — including in the first measured
      round, and including with ``--no-warmup``.

    So what the reload arm reports is a WARM reload: the source is in the
    page cache and its bytecode is already compiled. A developer's first
    edit after a cold boot costs more, and that number belongs to their
    filesystem rather than to this platform.
    """
    conn = await open_connection(target)
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)
    finally:
        await conn.close()

    by_arm: dict[str, list[dict[str, Any]]] = {arm.key: [] for arm in RESOLVE_ARMS}
    warmup_rounds: dict[str, Any] = {}
    counts = {arm.key: reloads if arm.reloads else resolutions for arm in RESOLVE_ARMS}

    with resolve_job_module() as module:
        # A real reload writes one log line, and this loop drives hundreds of
        # them. Its cost is the SINK's — a terminal is milliseconds, a log
        # file is microseconds — and a real worker pays it once per edit
        # rather than once per job, so leaving it in would report the
        # operator's terminal as the cost of importlib.
        logger.disable("pyjobby")
        try:
            # INTERLEAVED. Running every cached round before every reload
            # round would land whatever else the box picked up in between on
            # the ratio, which is the whole reported result here.
            if warmup:
                for arm in RESOLVE_ARMS:
                    warmup_rounds[arm.key] = _resolve_round(
                        arm, module.klass, max(1, counts[arm.key] // 10)
                    )
            for _ in range(repeat):
                for arm in RESOLVE_ARMS:
                    by_arm[arm.key].append(
                        _resolve_round(arm, module.klass, counts[arm.key])
                    )
        finally:
            logger.enable("pyjobby")

    arms = {
        arm.key: _resolve_arm_report(arm, by_arm[arm.key], counts[arm.key])
        for arm in RESOLVE_ARMS
    }
    cached = arms["cached"]["per_resolution_us"]

    def against_cached(key: str) -> dict[str, Any]:
        """One arm read against the cached path, which is the baseline every
        question here is really asking about."""
        value = arms[key]["per_resolution_us"]
        return {
            "extra_us_per_job": value - cached,
            "ratio": (value / cached) if cached else 0.0,
            "pct_of_reference_job_budget": (
                (value - cached) / 1e6 * SCALE_TARGET_RATE * 100.0
            ),
        }

    return {
        "benchmark": "resolve",
        "database": target.label,
        "resolutions": resolutions,
        "reloads": reloads,
        "repeat": repeat,
        "guard": guard,
        "job_class": module.klass,
        "warmup": warmup_rounds or None,
        "arms": arms,
        # The three questions, in the order they get asked.
        "reload_flag_cost": against_cached("reload_check"),
        "cache_saving": against_cached("uncached"),
        "reload_cost": against_cached("reload_fire"),
        "target_rate": SCALE_TARGET_RATE,
        "cleanup": module.cleanup_report(),
    }


# =========================================================================
# Output
# =========================================================================


def emit(
    result: dict[str, Any], output_json: bool, rows: Sequence[Sequence[str]]
) -> None:
    """Print JSON for machines or a table for people, never both.

    ``--json`` output has to stay parseable, so nothing else may be written
    to stdout when it is on.
    """
    if output_json:
        click.echo(json.dumps(result, indent=2, default=str, sort_keys=True))
        return
    # max_width is per-table, and these values are sentences: the default
    # 80 truncates an index name to nothing, which is the whole answer.
    print_table(["metric", "value"], [[str(a), str(b)] for a, b in rows], max_width=220)


def fmt(value: Any, digits: int = 1) -> str:
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# =========================================================================
# CLI
# =========================================================================


def db_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """--dsn/--config/--json/--force, on every subcommand."""
    f = click.option(
        "--max-existing-jobs",
        default=DEFAULT_MAX_EXISTING_JOBS,
        show_default=True,
        help="Refuse to run if the database already holds more jobs than this",
    )(f)
    f = click.option(
        "--force",
        is_flag=True,
        help="Run even though the database already holds a lot of jobs "
        "(the numbers will include that work's contention)",
    )(f)
    f = click.option(
        "--json", "output_json", is_flag=True, help="Emit JSON with stable keys"
    )(f)
    f = click.option(
        "--config", "-c", default=None, help=f"Config file path [{DEFAULT_CONFIG}]"
    )(f)
    f = click.option(
        "--dsn",
        envvar="PYJOBBY_DSN",
        default=None,
        help="PostgreSQL DSN (overrides --config; also read from PYJOBBY_DSN)",
    )(f)
    return f


def timing_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """--repeat/--warmup, for the subcommands that report a duration."""
    f = click.option(
        "--warmup/--no-warmup",
        default=True,
        show_default=True,
        help="Run and report a discarded warm-up first (first-touch page "
        "cache effects otherwise dominate a small run)",
    )(f)
    f = click.option(
        "--repeat",
        default=3,
        show_default=True,
        type=click.IntRange(min=1),
        help="Measured runs; the report is their MEDIAN and spread, because "
        "a single sample is noise",
    )(f)
    return f


def pick(ctx: click.Context, local: str | None, key: str) -> str | None:
    """A per-command --dsn/--config wins over the group-level one."""
    if local:
        return local
    value = ctx.obj.get(key) if ctx.obj else None
    return str(value) if value else None


@click.group()
@click.option(
    "--config", "-c", default=None, help=f"Config file path [{DEFAULT_CONFIG}]"
)
@click.option(
    "--dsn",
    envvar="PYJOBBY_DSN",
    default=None,
    help="PostgreSQL DSN (overrides --config; also read from PYJOBBY_DSN)",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None, dsn: str | None) -> None:
    """Reproduce and extend every measurement in docs/SCALE.md.

    Each subcommand creates its data in a uniquely named queue and deletes
    exactly that queue in a finally; nothing here truncates a table or
    touches a row it did not create.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["dsn"] = dsn


@cli.command("enqueue")
@db_options
@timing_options
@click.option(
    "--concurrency",
    default=16,
    show_default=True,
    type=click.IntRange(min=1),
    help="Connections enqueueing at once, each job in its own transaction. "
    "This is the measurement that matters: a NOTIFY-bearing commit takes a "
    "global exclusive lock, so concurrent enqueues serialize against each "
    "other. At concurrency 1 that cost is invisible.",
)
@click.option(
    "--jobs-per-connection",
    default=100,
    show_default=True,
    type=click.IntRange(min=1),
    help="Jobs each connection enqueues per run (one transaction each)",
)
@click.option(
    "--rows",
    default=20000,
    show_default=True,
    help="Rows for the BULK CONTRAST run (one transaction, set-based). This "
    "is the figure docs/SCALE.md quotes; it is not production enqueue.",
)
@click.option(
    "--allow-trigger-toggle",
    is_flag=True,
    help="Permit the per-trigger breakdown, which must ALTER TABLE jorb "
    "DISABLE TRIGGER to measure what each trigger costs. Off by default "
    "because a disabled jorb_history_record silently stops the audit trail "
    "and a disabled jorb_enqueued_notify silently stops every worker "
    "wakeup; the triggers are restored in a finally, from a SIGTERM "
    "handler, and from an atexit hook on a fresh connection, but a "
    "production database should still opt in on purpose.",
)
@click.pass_context
def enqueue_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    repeat: int,
    warmup: bool,
    concurrency: int,
    jobs_per_connection: int,
    rows: int,
    allow_trigger_toggle: bool,
) -> None:
    """Enqueue throughput, and what NOTIFY costs at the commit lock."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_enqueue(
            target,
            rows=rows,
            concurrency=concurrency,
            jobs_per_connection=jobs_per_connection,
            repeat=repeat,
            warmup=warmup,
            allow_trigger_toggle=allow_trigger_toggle,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    emit(result, output_json, enqueue_table(result))


def enqueue_table(result: dict[str, Any]) -> list[list[str]]:
    """Lay the enqueue result out so the commit-lock story reads top-down."""
    lock = result["notify_commit_lock"]

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.1f}%"

    table: list[list[str]] = [
        [
            "PRODUCTION enqueue",
            f"{result['concurrency']} connections x "
            f"{result['jobs_per_connection']} jobs, one txn per job",
        ],
        ["  as shipped", f"{lock['as_shipped_jobs_per_second']:,.0f} jobs/s"],
        [
            "  all NOTIFY triggers off",
            f"{lock['no_notify_jobs_per_second']:,.0f} jobs/s (upper bound)",
        ],
        [
            # NOT "commit-lock cost": with jorb_enqueued gated and nothing
            # parked, no notification is emitted here at all, so this gap
            # cannot be the lock. See the row two below.
            "  cost of having them",
            (
                "n/a (needs --allow-trigger-toggle)"
                if lock["ratio"] is None
                else f"{lock['cost_pct']:.0f}% of throughput lost "
                f"({lock['ratio']:.2f}x ceiling)"
            ),
        ],
        [
            "  wakeup notify off ONLY",
            f"{pct(lock['wakeup_only_recovery_pct'])} — the only NOTIFY on the "
            "INSERT path, so it SHOULD equal the all-off number above; any "
            "gap between the two is this run's noise, not a finding",
        ],
        [
            "  what that gap really is",
            "nothing is parked on this queue, so the gate emits NOTHING: this "
            "is the gate's own cost, not the commit lock",
        ],
        [
            "  why those two match",
            "the lock is taken per COMMIT, not per NOTIFY",
        ],
        [
            "    one of several ungated",
            "job_state_change off while others still notified: ceiling unmoved",
        ],
        [
            "    the LAST one ungated",
            "job_state_change deleted: 2.63-2.95x on the completion path",
        ],
        ["headroom vs 278/s", f"{result['headroom_vs_target_rate']:,.0f}x"],
    ]
    for mode in ("serial_contrast", "bulk_contrast"):
        data = result["modes"][mode]
        table.append(
            [
                f"CONTRAST {mode.removesuffix('_contrast')}",
                f"{data['jobs_per_second']:,.0f} jobs/s over {data['jobs']:,} "
                f"jobs — NOT production enqueue",
            ]
        )
    for mode, data in result["modes"].items():
        for key, variant in data["variants"].items():
            if "skipped" in variant:
                table.append([f"  {mode}/{key}", variant["skipped"]])
                continue
            table.append(
                [
                    f"  {mode}/{key}",
                    f"{variant['milliseconds']:,.0f} ms  "
                    f"{variant['jobs_per_second']:,.0f} jobs/s  "
                    f"(spread {variant['spread_pct']:.0f}%)",
                ]
            )
    return table


@cli.command("claim")
@db_options
@timing_options
@click.option("--workers", default=8, show_default=True, help="Concurrent claimers")
@click.option("--jobs", default=2000, show_default=True, help="Jobs to drain per run")
@click.option(
    "--hold-ms",
    default=5.0,
    show_default=True,
    help="Simulated job duration for the churn arms; a cap permits cap/duration",
)
@click.option(
    "--low-cap",
    default=2,
    show_default=True,
    help="max_concurrency for the arm sized so the cap binds",
)
@click.option(
    "--high-cap",
    default=1000,
    show_default=True,
    help="max_concurrency for the arm sized so only the claim lock binds",
)
@click.pass_context
def claim_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    repeat: int,
    warmup: bool,
    workers: int,
    jobs: int,
    hold_ms: float,
    low_cap: int,
    high_cap: int,
) -> None:
    """Claim throughput and contention through the real claim_jorb()."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_claim(
            target,
            workers=workers,
            jobs=jobs,
            repeat=repeat,
            warmup=warmup,
            max_existing_jobs=max_existing_jobs,
            force=force,
            low_cap=low_cap,
            high_cap=high_cap,
            hold_ms=hold_ms,
        )
    )
    capped = result["modes"]["capped"]
    rows: list[list[str]] = []
    for key, data in result["modes"].items():
        rate = data["claims_per_second"]
        cap = data["max_concurrency"]
        rows.append(
            [
                f"{key} claims/s",
                f"{rate['median']:,.0f}  "
                f"({data['headroom_vs_target_rate']:.1f}x the {result['target_rate']:,.0f}/s "
                f"target, spread {rate['spread_pct']:.0f}%, "
                f"{data['claimers']} claimers, cap {cap if cap is not None else 'none'})",
            ]
        )
    emit(
        result,
        output_json,
        [
            ["claimers", fmt(result["workers"])],
            ["short-job hold", f"{result['hold_ms']:.0f} ms (churn arms)"],
            *rows,
            ["capped / uncapped (drain)", f"{result['capped_throughput_ratio']:.2f}x"],
            [
                "capped / uncapped (churn)",
                f"{result['churn_capped_throughput_ratio']:.2f}x — what exact "
                "caps cost when the cap itself refuses nothing",
            ],
            [
                "what the low cap permits",
                f"{result['modes']['churn_cap_low']['cap_ceiling_per_second'] or 0:,.0f}/s "
                f"= cap {result['low_cap']} / {result['hold_ms']:.0f} ms. A cap IS a "
                "throughput limit; no claim strategy raises it",
            ],
            [
                "capped advisory-lock misses",
                f"{capped['empty_claims_with_work_available']:,} "
                f"({capped['lock_miss_rate'] * 100:.1f}% of attempts)",
            ],
            [
                "what the ratio is NOT",
                "lock contention. A claimer that loses the lock holds nothing, "
                "so its retries are wasted BESIDE the critical section; capped "
                "throughput is 1/(critical section) under any lock strategy",
            ],
            [
                "what a miss really costs",
                "more than the round trip counted here: a real worker reads an "
                "empty claim as 'queue empty', re-arms this queue's enqueue "
                "notifications and parks for checkInterval (5s default). "
                "See tests/test_claim_contention.py",
            ],
            [
                "modes are interleaved",
                "alternating rounds, so machine drift shows up as spread in "
                "both rather than as a ratio",
            ],
        ],
    )


@cli.command("e2e")
@db_options
@timing_options
@click.option("--jobs", default=200, show_default=True, help="Jobs per run")
@click.option("--workers", "-w", default=4, show_default=True, help="Worker processes")
@click.option(
    "--timeout",
    default=120.0,
    show_default=True,
    help="Seconds to wait for the queue to drain before giving up",
)
@click.option(
    "--reload",
    "reload_jobs",
    is_flag=True,
    help="Start the fleet with `pj --reload`, so every job re-checks its "
    "module's mtime. A SMELL TEST, not a measurement: the effect is "
    "microseconds against a claim->finished p50 in milliseconds, so it is "
    "far under this benchmark's own spread, and comparing two separate runs "
    "is the un-interleaved shape docs/TESTING.md rule 3 forbids. What the "
    "flag costs is measured per call by `pj-bench resolve`.",
)
@click.pass_context
def e2e_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    repeat: int,
    warmup: bool,
    jobs: int,
    workers: int,
    timeout: float,
    reload_jobs: bool,
) -> None:
    """End-to-end throughput and latency with REAL worker processes."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_e2e(
            target,
            jobs=jobs,
            workers=workers,
            repeat=repeat,
            warmup=warmup,
            timeout=timeout,
            max_existing_jobs=max_existing_jobs,
            force=force,
            reload_jobs=reload_jobs,
        )
    )
    latency = result["enqueue_to_finished"]
    service = result["claim_to_finished"]
    emit(
        result,
        output_json,
        [
            ["worker processes", fmt(result["workers"])],
            ["jobs per run", fmt(result["jobs"])],
            [
                "pj --reload",
                "ON (smell test; see `pj-bench resolve` for the cost)"
                if result["reload_jobs"]
                else "off",
            ],
            ["completed jobs/s", fmt(result["jobs_per_second"]["median"], 2)],
            ["spread", f"{result['jobs_per_second']['spread_pct']:.0f}%"],
            ["headroom vs 278/s", f"{result['headroom_vs_target_rate']:.2f}x"],
            [
                "enqueue->finished p50/p95/p99/max",
                f"{latency['p50']:.3f} / {latency['p95']:.3f} / "
                f"{latency['p99']:.3f} / {latency['max']:.3f} s",
            ],
            [
                "claim->finished p50/p95/p99/max",
                f"{service['p50']:.3f} / {service['p95']:.3f} / "
                f"{service['p99']:.3f} / {service['max']:.3f} s",
            ],
            ["drained within timeout", "no" if result["timed_out"] else "yes"],
        ],
    )


@cli.command("notify")
@db_options
@click.option(
    "--lifecycles", default=200, show_default=True, help="Job lifecycles to drive"
)
@click.option(
    "--target-rate",
    default=SCALE_TARGET_RATE,
    show_default=True,
    help="Jobs/second to project the per-lifecycle count onto",
)
@click.pass_context
def notify_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    lifecycles: int,
    target_rate: float,
) -> None:
    """Notifications per job lifecycle, unobserved AND observed.

    Two phases, because a demand-gated schema has two honest answers: what a
    job nobody is watching costs (the floor, and what almost every job is),
    and what a job with a parked worker and a waiting client costs.
    """
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_notify(
            target,
            lifecycles=lifecycles,
            target_rate=target_rate,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    claims = result["scale_md"]
    unobserved = result["phases"]["unobserved"]
    observed = result["phases"]["observed"]

    def verdict(agrees: bool) -> str:
        return "agrees" if agrees else "DISAGREES"

    table = [
        ["lifecycles driven", f"{fmt(result['lifecycles'])} per phase"],
        [
            "UNOBSERVED notify/lifecycle",
            f"{unobserved['per_lifecycle']:,.2f}  (no worker parked, nothing "
            f"awaited) — claim {claims['claimed_unobserved']}, "
            f"{verdict(claims['unobserved_agrees'])}",
        ],
        [
            "OBSERVED notify/lifecycle",
            f"{observed['per_lifecycle']:,.2f}  (a worker parked idle on the "
            f"queue, jorb.awaited set) — claim {claims['claimed_observed']}, "
            f"{verdict(claims['observed_agrees'])}",
        ],
        [
            "what the demand gate saves",
            f"{result['demand_gate_saves_per_lifecycle']:,.2f}/lifecycle — and "
            "the lock is per COMMIT, so those commits stop taking it at all",
        ],
        ["history rows per lifecycle", fmt(result["history_rows_per_lifecycle"], 2)],
        [
            f"projected at {result['target_rate']:.0f} jobs/s",
            f"{unobserved['projected_notifications_per_second']:,.0f} notify/s "
            f"unobserved, "
            f"{observed['projected_notifications_per_second']:,.0f} notify/s "
            f"if every job is watched",
        ],
        [
            "pg_notification_queue_usage",
            f"{result['notify_queue_usage']['before']:.6f} -> "
            f"{result['notify_queue_usage']['after']:.6f}",
        ],
    ]
    for phase_name, phase in (("unobserved", unobserved), ("observed", observed)):
        table.extend(
            [f"  {phase_name} channel {channel}", fmt(count)]
            for channel, count in phase["per_channel"].items()
            if count
        )
    emit(result, output_json, table)


@cli.command("plans")
@db_options
@click.option(
    "--seed",
    default=20000,
    show_default=True,
    help="Rows to insert before measuring. A plan against an empty table "
    "proves nothing: a seq scan of a tiny table is the RIGHT plan, so the "
    "gate would pass for the wrong reason.",
)
@click.option(
    "--planner-setting",
    "planner_settings",
    multiple=True,
    help="name=value applied to this connection only, e.g. "
    "enable_indexscan=off — answers 'what would this cost without the "
    "index?' and proves the seq-scan gate actually fires",
)
@click.pass_context
def plans_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    seed: int,
    planner_settings: tuple[str, ...],
) -> None:
    """EXPLAIN (ANALYZE, BUFFERS) every hot query; non-zero on a bad plan."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_plans(
            target,
            seed=seed,
            planner_settings=planner_settings,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    table = [
        [
            key,
            f"{'+'.join(data['access_methods']) or 'none'} "
            f"{'via ' + ','.join(data['indexes']) if data['indexes'] else ''} "
            f"— {data['buffers']:,} buffers, "
            f"{data['rows_removed_by_filter']:,} rows discarded "
            f"(budget {data['max_rows_removed']:,})",
        ]
        for key, data in result["queries"].items()
    ]
    emit(result, output_json, table)
    if not result["healthy"]:
        if result["seq_scan_offenders"]:
            offenders = ", ".join(
                f"{key} ({'; '.join(result['queries'][key]['seq_scans'])})"
                for key in result["seq_scan_offenders"]
            )
            click.echo(
                f"FAIL: sequential scan in: {offenders}. These run on a timer "
                f"forever; a scan here stays correct and gets slower as the "
                f"table grows.",
                err=True,
            )
        if result["discard_offenders"]:
            offenders = ", ".join(
                f"{key} ({result['queries'][key]['rows_removed_by_filter']:,} rows "
                f"discarded, budget {result['queries'][key]['max_rows_removed']:,})"
                for key in result["discard_offenders"]
            )
            click.echo(
                f"FAIL: rows read and thrown away over budget in: {offenders}. "
                f"An index scan that discards everything it reads is not a "
                f"sequential scan and costs exactly the same.",
                err=True,
            )
        raise SystemExit(1)


@cli.command("resolve")
@db_options
@timing_options
@click.option(
    "--resolutions",
    default=20000,
    show_default=True,
    type=click.IntRange(min=1),
    help="Calls per round for the arms that do not re-import",
)
@click.option(
    "--reloads",
    default=DEFAULT_RESOLVE_RELOADS,
    show_default=True,
    type=click.IntRange(min=1),
    help="Calls per round for the arm that DOES re-import. Lower on purpose: "
    "a reload costs orders of magnitude more than a cached lookup, and every "
    "arm is reduced to a per-resolution cost before anything is compared.",
)
@click.pass_context
def resolve_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    repeat: int,
    warmup: bool,
    resolutions: int,
    reloads: int,
) -> None:
    """What resolving a job class costs per job, and what --reload adds."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = run_command(
        run_resolve(
            target,
            resolutions=resolutions,
            reloads=reloads,
            repeat=repeat,
            warmup=warmup,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    emit(result, output_json, resolve_table(result))


def resolve_table(result: dict[str, Any]) -> list[list[str]]:
    """Four arms, then the three questions they were run to answer."""
    arms = result["arms"]

    def arm_row(key: str) -> list[str]:
        arm = arms[key]
        return [
            f"  {key}",
            f"{arm['per_resolution_us']:.3f} us/job "
            f"(spread {arm['spread_pct']:.0f}%, "
            f"ceiling {arm['implied_ceiling_jobs_per_second']:,.0f} jobs/s) "
            f"— {arm['means']}",
        ]

    def question(label: str, key: str) -> list[str]:
        block = result[key]
        return [
            label,
            f"{block['extra_us_per_job']:+.3f} us/job ({block['ratio']:.2f}x, "
            f"{block['pct_of_reference_job_budget']:.4f}% of the per-job "
            f"budget at {result['target_rate']:.0f} jobs/s)",
        ]

    return [
        [
            "per-job class resolution",
            f"{result['repeat']} interleaved rounds; medians",
        ],
        *(arm_row(arm.key) for arm in RESOLVE_ARMS),
        question("--reload costs a production worker", "reload_flag_cost"),
        question("the class cache saves", "cache_saving"),
        question("one reload, after an edit", "reload_cost"),
        [
            "what the reload number is NOT",
            "a cold import: the module is in the page cache and its bytecode "
            "is compiled. It is also a FLOOR — reload re-executes the job "
            "module's top level, so a bigger jobs file costs more.",
        ],
    ]


@cli.command("all")
@db_options
@timing_options
@click.option(
    "--rows", default=20000, show_default=True, help="enqueue: bulk contrast rows"
)
@click.option(
    "--concurrency", default=16, show_default=True, help="enqueue: connections"
)
@click.option("--jobs-per-connection", default=100, show_default=True)
@click.option("--claim-workers", default=8, show_default=True)
@click.option("--claim-jobs", default=2000, show_default=True)
@click.option("--e2e-jobs", default=200, show_default=True)
@click.option("--e2e-workers", default=4, show_default=True)
@click.option("--lifecycles", default=200, show_default=True)
@click.option("--target-rate", default=SCALE_TARGET_RATE, show_default=True)
@click.option("--seed", default=20000, show_default=True, help="plans: rows to seed")
@click.option("--resolutions", default=20000, show_default=True)
@click.option("--reloads", default=DEFAULT_RESOLVE_RELOADS, show_default=True)
@click.option("--allow-trigger-toggle", is_flag=True, help="See `pj-bench enqueue`.")
@click.pass_context
def all_cmd(
    ctx: click.Context,
    dsn: str | None,
    config: str | None,
    output_json: bool,
    force: bool,
    max_existing_jobs: int,
    repeat: int,
    warmup: bool,
    rows: int,
    concurrency: int,
    jobs_per_connection: int,
    claim_workers: int,
    claim_jobs: int,
    e2e_jobs: int,
    e2e_workers: int,
    lifecycles: int,
    target_rate: float,
    seed: int,
    resolutions: int,
    reloads: int,
    allow_trigger_toggle: bool,
) -> None:
    """Run every benchmark and print one summary table."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))

    async def everything() -> dict[str, Any]:
        return {
            "enqueue": await run_enqueue(
                target,
                rows=rows,
                concurrency=concurrency,
                jobs_per_connection=jobs_per_connection,
                repeat=repeat,
                warmup=warmup,
                allow_trigger_toggle=allow_trigger_toggle,
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
            "claim": await run_claim(
                target,
                workers=claim_workers,
                jobs=claim_jobs,
                repeat=repeat,
                warmup=warmup,
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
            "e2e": await run_e2e(
                target,
                jobs=e2e_jobs,
                workers=e2e_workers,
                repeat=repeat,
                warmup=warmup,
                timeout=120.0,
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
            "notify": await run_notify(
                target,
                lifecycles=lifecycles,
                target_rate=target_rate,
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
            "plans": await run_plans(
                target,
                seed=seed,
                planner_settings=(),
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
            "resolve": await run_resolve(
                target,
                resolutions=resolutions,
                reloads=reloads,
                repeat=repeat,
                warmup=warmup,
                max_existing_jobs=max_existing_jobs,
                force=force,
            ),
        }

    results = run_command(everything())
    results["healthy"] = results["plans"]["healthy"]
    lock = results["enqueue"]["notify_commit_lock"]
    summary = [
        [
            f"enqueue jobs/s ({concurrency} conns, 1 txn/job)",
            fmt(results["enqueue"]["jobs_per_second"]),
        ],
        [
            "  NOTIFY commit-lock cost",
            "n/a (needs --allow-trigger-toggle)"
            if lock["ratio"] is None
            else f"{lock['cost_pct']:.0f}% lost ({lock['ratio']:.2f}x ceiling)",
        ],
        [
            "  CONTRAST bulk rows/s (one txn)",
            fmt(results["enqueue"]["modes"]["bulk_contrast"]["jobs_per_second"]),
        ],
        [
            "claim/s (uncapped)",
            fmt(results["claim"]["modes"]["uncapped"]["claims_per_second"]["median"]),
        ],
        [
            "claim/s (capped)",
            fmt(results["claim"]["modes"]["capped"]["claims_per_second"]["median"]),
        ],
        [
            "claim/s (capped, short jobs)",
            fmt(
                results["claim"]["modes"]["churn_cap_high"]["claims_per_second"][
                    "median"
                ]
            ),
        ],
        ["e2e jobs/s", fmt(results["e2e"]["jobs_per_second"]["median"], 2)],
        [
            "e2e latency p50/p99",
            f"{results['e2e']['enqueue_to_finished']['p50']:.3f} / "
            f"{results['e2e']['enqueue_to_finished']['p99']:.3f} s",
        ],
        [
            "notifications/lifecycle",
            f"{results['notify']['per_lifecycle_unobserved']:,.2f} unobserved / "
            f"{results['notify']['per_lifecycle_observed']:,.2f} observed",
        ],
        [
            f"notify/s at {target_rate:.0f} jobs/s",
            f"{results['notify']['phases']['unobserved']['projected_notifications_per_second']:,.0f}"
            f" unobserved / "
            f"{results['notify']['phases']['observed']['projected_notifications_per_second']:,.0f}"
            f" observed",
        ],
        [
            "class resolution/job (cached)",
            f"{results['resolve']['arms']['cached']['per_resolution_us']:.3f} us",
        ],
        [
            "  --reload adds",
            f"{results['resolve']['reload_flag_cost']['extra_us_per_job']:+.3f} us "
            f"({results['resolve']['reload_flag_cost']['ratio']:.2f}x)",
        ],
        [
            "  the cache saves",
            f"{results['resolve']['cache_saving']['extra_us_per_job']:+.3f} us "
            f"({results['resolve']['cache_saving']['ratio']:.2f}x)",
        ],
        [
            "hot query plans",
            f"healthy ({len(results['plans']['sweeps_gated'])} monitor sweeps gated)"
            if results["healthy"]
            else "; ".join(
                part
                for part in (
                    "SEQ SCAN: " + ", ".join(results["plans"]["seq_scan_offenders"])
                    if results["plans"]["seq_scan_offenders"]
                    else "",
                    "OVER DISCARD BUDGET: "
                    + ", ".join(results["plans"]["discard_offenders"])
                    if results["plans"]["discard_offenders"]
                    else "",
                )
                if part
            ),
        ],
    ]
    emit(results, output_json, summary)
    if not results["healthy"]:
        raise SystemExit(1)


def main() -> None:
    """The ``pj-bench`` console script."""
    cli(obj={})


if __name__ == "__main__":
    main()
