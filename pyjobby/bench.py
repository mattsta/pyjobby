"""``pj-bench`` — pyjobby's permanent benchmark and diagnosis harness.

Every performance number in ``docs/SCALE.md`` was measured once, written
down, and the script that produced it thrown away. This module is the
replacement: each subcommand *reproduces* one of those measurements, so a
change can be shown to have made things better or worse instead of argued
about, and the next bottleneck hunt starts from a running tool rather than
from zero.

    pj-bench enqueue --concurrency 16 enqueue throughput + NOTIFY commit lock
    pj-bench claim   --workers 8      claim throughput and lock contention
    pj-bench e2e     --jobs 200       real worker processes, real latency
    pj-bench notify                   notifications per job lifecycle
    pj-bench plans   --seed 20000     EXPLAIN every hot query (CI gate)
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
import json
import math
import os
import re
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

from . import db
from .cli import (
    ConfigProblem,
    DatabaseProblem,
    fail,
    print_table,
    print_warning,
)
from .configloader import load_config_from_file
from .monitor import TERMINAL_STATES
from .pj import Job

DEFAULT_CONFIG = "./pyjobby.conf.py"

#: Reference workload from docs/SCALE.md: 1,000,000 jobs/hour.
SCALE_TARGET_RATE = 278.0

#: docs/SCALE.md's claims, carried here so a run can say "agrees" or
#: "DISAGREES" instead of leaving the reader to diff two documents.
SCALE_CLAIM_NOTIFICATIONS_PER_LIFECYCLE = 5
SCALE_CLAIM_HISTORY_ROWS_PER_LIFECYCLE = 4
SCALE_CLAIM_ROW_WRITES_PER_LIFECYCLE = 4

#: How many jobs may already exist before a run is refused without --force.
DEFAULT_MAX_EXISTING_JOBS = 1000

#: The job class ``pj-bench e2e`` enqueues. It lives here, in the installed
#: package, so real worker processes can import it by dotted path with no
#: --path argument and no dependency on the test tree.
BENCH_JOB_CLASS = "pyjobby.bench.BenchJob"

#: Triggers on ``jorb``, by the role docs/SCALE.md discusses them in.
TRIGGER_FIREHOSE = "job_state_change_notify"
TRIGGER_HISTORY = "jorb_history_record"
TRIGGER_ENQUEUED = "jorb_enqueued_notify"
TRIGGER_DONE = "jorb_done_notify"
TRIGGER_CANCEL = "jorb_cancel_notify"

#: Every trigger on ``jorb`` that issues a NOTIFY. Disabling all of them is
#: the upper bound for enqueue: the cost of the commit lock is paid once per
#: transaction that notifies at all, so the interesting comparison is
#: "notifies" versus "does not", not "notifies three times" versus "once".
NOTIFY_TRIGGERS = (TRIGGER_ENQUEUED, TRIGGER_DONE, TRIGGER_CANCEL, TRIGGER_FIREHOSE)

#: Every channel the schema can emit on, so ``pj-bench notify`` counts what
#: is actually sent rather than what it expected to be sent.
NOTIFY_CHANNELS = (
    "jorb_enqueued",
    "jorb_done",
    "jorb_cancel",
    "jorb_event",
    "jorb_mailbox",
    "job_state_change",
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

    The three global tables this can reach — ``jorb_queue``, and the worker
    registry rows the real processes ``pj-bench e2e`` starts register — are
    both keyed by queue name, so they are removed by the same predicate and
    no row belonging to anything else is ever in range.
    """
    jobs = int(await conn.fetchval("SELECT count(*) FROM jorb WHERE queue = $1", queue))
    workers = int(
        await conn.fetchval("SELECT count(*) FROM jorb_worker WHERE queue = $1", queue)
    )
    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute("DELETE FROM jorb_queue WHERE name = $1", queue)
    await conn.execute("DELETE FROM jorb_worker WHERE queue = $1", queue)
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

#: Triggers disabled right now that have not been re-enabled yet. The
#: atexit hook below is the last line of defense: a benchmark that leaves
#: jorb_history_record or jorb_enqueued_notify disabled silently breaks the
#: audit trail or every worker wakeup in the install.
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
    """Last-resort re-enable, running at interpreter exit.

    Reached when the normal ``finally`` could not run its statements — a
    cancelled event loop after Ctrl-C, or a connection that died mid-run —
    so it opens a fresh connection on a fresh loop.
    """
    while _PENDING_RESTORE:
        dsn, triggers = _PENDING_RESTORE.pop()
        try:
            asyncio.run(_restore_via_new_connection(dsn, triggers))
            print(
                f"pj-bench: re-enabled triggers {', '.join(triggers)} on jorb",
                file=sys.stderr,
            )
        except Exception as e:  # pragma: no cover - needs a dead database
            print(
                f"pj-bench: CRITICAL - could not re-enable triggers "
                f"{', '.join(triggers)} on jorb ({e}). Run: "
                + "; ".join(f"ALTER TABLE jorb ENABLE TRIGGER {t}" for t in triggers),
                file=sys.stderr,
            )


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
    ("firehose_off", (TRIGGER_FIREHOSE,)),
    ("wakeup_notify_off", (TRIGGER_ENQUEUED,)),
    ("history_off", (TRIGGER_HISTORY,)),
    ("firehose_and_history_off", (TRIGGER_FIREHOSE, TRIGGER_HISTORY)),
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
    * The cost is per COMMIT, not per notification. Turning off the
      ``job_state_change`` firehose removes three of the five notifications
      in a job's lifecycle and recovers almost nothing, because one NOTIFY
      in a transaction takes the same lock as three. Trimming channels does
      not raise the ceiling; only taking NOTIFY out of the commit path (or
      batching it outside) does.
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

    ``firehose_only_recovery_pct`` is reported next to the full cost on
    purpose: it is the number that stops someone "optimizing" by trimming
    notification volume. Three fewer notifications per lifecycle, and the
    ceiling does not move, because the lock is taken once per commit.
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
        "firehose_only_recovery_pct": recovery("production", "firehose_off"),
        "wakeup_only_recovery_pct": recovery("production", "wakeup_notify_off"),
        "serial_contrast_recovery_pct": recovery("serial_contrast", "all_notify_off"),
        "bulk_contrast_recovery_pct": recovery("bulk_contrast", "all_notify_off"),
        "explanation": (
            "The lock is taken once per COMMIT, not once per notification: "
            "removing the 3-of-5 job_state_change firehose recovers "
            "essentially nothing, while removing NOTIFY from the commit "
            "path entirely recovers the whole gap. Reduce commits that "
            "notify, or move the notification outside the commit — trimming "
            "channels does not raise the ceiling."
        ),
    }


# =========================================================================
# 2. claim — throughput through the real claim_jorb(), and contention
# =========================================================================

CLAIM_SQL = "SELECT id FROM claim_jorb($1, $2::text[], $3, $4, $5, $6)"


async def _claim_loop(
    pool: asyncpg.Pool,
    queue: str,
    worker_index: int,
    deadline: float,
    remaining: dict[str, int],
) -> dict[str, int]:
    """One claimer, hammering ``claim_jorb`` until the queue is empty.

    Counts the empty-handed returns separately from the claims: for a queue
    with a concurrency cap that has not been reached, an empty return means
    the claimer lost the per-queue advisory try-lock, and that ratio is the
    number that says a capped queue is thrashing rather than working.
    """
    claims = 0
    empty_with_work = 0
    async with pool.acquire() as conn:
        while time.monotonic() < deadline and remaining["left"] > 0:
            row = await conn.fetchval(
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
    return {"claims": claims, "empty_with_work": empty_with_work}


async def _seed_claimable(conn: asyncpg.Connection, queue: str, jobs: int) -> None:
    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute(ENQUEUE_SQL, BENCH_JOB_CLASS, queue, jobs)


async def _claim_round(
    pool: asyncpg.Pool, conn: asyncpg.Connection, queue: str, jobs: int, workers: int
) -> dict[str, Any]:
    await _seed_claimable(conn, queue, jobs)
    remaining = {"left": jobs}
    deadline = time.monotonic() + 120.0
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            _claim_loop(pool, queue, index, deadline, remaining)
            for index in range(workers)
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
) -> dict[str, Any]:
    """Claim throughput on an uncapped queue and on a capped one.

    The capped run sets ``max_concurrency`` far above the job count on
    purpose. The cap is then never the binding constraint, so every
    empty-handed claim it records is a MISSED ADVISORY TRY-LOCK and nothing
    else — which is exactly the "capped queue is thrashing" signal, and is
    impossible to separate from legitimate cap refusals if the cap can bind.
    """
    queue = bench_queue("claim")
    conn = await open_connection(target)
    pool = await db.create_pool(
        **target.params, min_size=workers, max_size=max(workers, 2)
    )
    modes: dict[str, Any] = {}
    result: dict[str, Any] = {}
    try:
        guard = await guard_busy_database(conn, limit=max_existing_jobs, force=force)

        for mode in ("uncapped", "capped"):
            await conn.execute("DELETE FROM jorb_queue WHERE name = $1", queue)
            if mode == "capped":
                await conn.execute(
                    "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, $2)",
                    queue,
                    jobs + 1000,
                )
            rounds: list[dict[str, Any]] = []
            if warmup:
                await _claim_round(pool, conn, queue, min(jobs, 100), workers)
            for _ in range(repeat):
                rounds.append(await _claim_round(pool, conn, queue, jobs, workers))

            rates = [r["claims_per_second"] for r in rounds]
            summary = summarize(rates)
            modes[mode] = {
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
                "max_concurrency": (jobs + 1000) if mode == "capped" else None,
            }

        uncapped = modes["uncapped"]["claims_per_second"]["median"]
        capped = modes["capped"]["claims_per_second"]["median"]
        result.update(
            {
                "benchmark": "claim",
                "database": target.label,
                "queue": queue,
                "workers": workers,
                "jobs": jobs,
                "repeat": repeat,
                "guard": guard,
                "modes": modes,
                "capped_throughput_ratio": (capped / uncapped) if uncapped else 0.0,
                "claims_per_second": uncapped,
                "target_rate": SCALE_TARGET_RATE,
                "headroom_vs_target_rate": (
                    uncapped / SCALE_TARGET_RATE if uncapped else 0.0
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
) -> dict[str, Any]:
    """Enqueue, run real ``pj`` workers, and measure what came out.

    Deliberately uses the console script and a process group kill (the
    helpers in tests/utils/processes.py), not an in-process JobSystem: a
    worker that only ever runs inside the benchmark's own event loop is not
    the thing an operator deploys.
    """
    from tests.utils.processes import spawn, terminate, wait_until

    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute(ENQUEUE_SQL, BENCH_JOB_CLASS, queue, jobs)

    args = ["pj", "--config", config_path, "--workers", str(workers)]
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

    rows = await conn.fetch(
        """
        SELECT EXTRACT(EPOCH FROM (finished - created))    AS enqueue_to_finished,
               EXTRACT(EPOCH FROM (finished - claimed_at)) AS claim_to_finished
        FROM jorb
        WHERE queue = $1 AND finished IS NOT NULL
        """,
        queue,
    )
    window = await conn.fetchrow(
        """
        SELECT EXTRACT(EPOCH FROM (max(finished) - min(claimed_at))) AS drain_seconds,
               count(*)                                              AS finished
        FROM jorb
        WHERE queue = $1 AND finished IS NOT NULL AND claimed_at IS NOT NULL
        """,
        queue,
    )
    drain_seconds = float(window["drain_seconds"] or 0.0)
    finished = int(window["finished"] or 0)

    return {
        "enqueued": jobs,
        "completed": completed,
        "finished": finished,
        "timed_out": timed_out,
        "wall_seconds": wall_seconds,
        "drain_seconds": drain_seconds,
        "jobs_per_second": (finished / drain_seconds) if drain_seconds else 0.0,
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
) -> dict[str, Any]:
    """The headline number: completed jobs/sec through real processes.

    Latency is reported twice on purpose. ``enqueue_to_finished`` is what a
    caller experiences and includes waiting behind the backlog this
    benchmark deliberately creates; ``claim_to_finished`` is what the worker
    itself costs. Reporting only the first makes a fast platform look slow
    at any queue depth; reporting only the second hides the queue.
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
                    conn, config_path, queue, min(jobs, 20), workers, timeout
                )
            for _ in range(repeat):
                rounds.append(
                    await _e2e_round(conn, config_path, queue, jobs, workers, timeout)
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
    """Count the notifications one job lifecycle actually emits.

    docs/SCALE.md claims five per job and projects ~1,390/s at the reference
    rate. This LISTENs on every channel the schema can emit on and drives
    the four row writes of a real lifecycle (insert, claim, run, terminal),
    counting what arrives per channel — so the claim is proven or refuted
    rather than restated. Notifications from other work in the database are
    counted separately and excluded from the per-lifecycle figure.

    Each of the four writes runs in its OWN transaction, because that is
    what a real install does and because PostgreSQL collapses identical
    (channel, payload) notifications within one transaction. Driving the
    lifecycles set-based instead measures the deduplication rather than the
    fan-out: ``jorb_enqueued`` carries only the queue name, so a thousand
    inserts batched into one transaction emit exactly ONE wakeup — a real
    and useful property of batch enqueue, and a completely wrong answer to
    "what does one job cost".

    ``pg_notification_queue_usage()`` is sampled before and after because
    that queue is server-wide, bounded, and drains only as fast as the
    slowest listener: at 1.0 every NOTIFY-issuing transaction fails, which
    in this platform means nothing can be enqueued or completed anywhere.
    """
    queue = bench_queue("notify")
    conn = await open_connection(target)
    listener = await open_connection(target)
    counts: dict[str, int] = dict.fromkeys(NOTIFY_CHANNELS, 0)
    foreign: dict[str, int] = dict.fromkeys(NOTIFY_CHANNELS, 0)
    job_ids: set[int] = set()
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
        usage_peak = max(
            usage_peak,
            float(await conn.fetchval("SELECT pg_notification_queue_usage()")),
        )

        total = await _drain_notifications(listener, counts)
        usage_after = float(await conn.fetchval("SELECT pg_notification_queue_usage()"))

        history_rows = int(
            await conn.fetchval(
                "SELECT count(*) FROM jorb_history h JOIN jorb j ON j.id = h.job_id "
                "WHERE j.queue = $1",
                queue,
            )
        )

        per_lifecycle = total / lifecycles if lifecycles else 0.0
        firehose = counts["job_state_change"]
        load_bearing = sum(counts[c] for c in LOAD_BEARING_CHANNELS)
        result.update(
            {
                "benchmark": "notify",
                "database": target.label,
                "queue": queue,
                "lifecycles": lifecycles,
                "guard": guard,
                "per_channel": counts,
                "per_channel_per_lifecycle": {
                    channel: (count / lifecycles if lifecycles else 0.0)
                    for channel, count in counts.items()
                },
                "foreign_notifications": {k: v for k, v in foreign.items() if v},
                "total": total,
                "per_lifecycle": per_lifecycle,
                "history_rows_per_lifecycle": (
                    history_rows / lifecycles if lifecycles else 0.0
                ),
                "row_writes_per_lifecycle": float(SCALE_CLAIM_ROW_WRITES_PER_LIFECYCLE),
                "firehose_share": (firehose / total) if total else 0.0,
                "load_bearing_per_lifecycle": (
                    load_bearing / lifecycles if lifecycles else 0.0
                ),
                "target_rate": target_rate,
                "projected_notifications_per_second": per_lifecycle * target_rate,
                "projected_without_firehose_per_second": (
                    (total - firehose) / lifecycles * target_rate if lifecycles else 0.0
                ),
                "notify_queue_usage": {
                    "before": usage_before,
                    "peak": max(usage_before, usage_peak),
                    "after": usage_after,
                },
                "scale_md": {
                    "claimed_per_lifecycle": SCALE_CLAIM_NOTIFICATIONS_PER_LIFECYCLE,
                    "agrees": per_lifecycle == SCALE_CLAIM_NOTIFICATIONS_PER_LIFECYCLE,
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


def _payload_is_ours(channel: str, payload: str, queue: str, job_ids: set[int]) -> bool:
    """Attribute a notification to this run, or to other work.

    Every channel carries enough to tell: the wakeup channel carries the
    queue name, the state feed carries it in JSON, and the rest carry a job
    id this run knows it created.
    """
    if channel == "jorb_enqueued":
        return payload == queue
    if channel == "jorb_cancel":
        return payload.isdigit() and int(payload) in job_ids
    if channel == "schedule_executed":
        return False
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if channel == "job_state_change":
        return bool(data.get("queue") == queue)
    if channel == "jorb_done":
        return int(data.get("id", -1)) in job_ids
    if channel == "jorb_event":
        return int(data.get("job_id", -1)) in job_ids
    if channel == "jorb_mailbox":
        return int(data.get("dest", -1)) in job_ids
    return False


async def _drain_notifications(
    listener: asyncpg.Connection, counts: dict[str, int], timeout: float = 5.0
) -> int:
    """Wait until the notification count stops moving.

    NOTIFY is delivered after commit and asynchronously, so counting
    immediately after the last UPDATE undercounts. Settling on "no new
    message for 300ms" is what makes the per-lifecycle figure a fact.
    """
    deadline = time.monotonic() + timeout
    last_total = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        await listener.execute("SELECT 1")
        await asyncio.sleep(0.05)
        total = sum(counts.values())
        if total != last_total:
            last_total = total
            stable_since = time.monotonic()
        elif total > 0 and time.monotonic() - stable_since > 0.3:
            break
    return sum(counts.values())


# =========================================================================
# 5. plans — EXPLAIN every hot query; the CI regression gate
# =========================================================================

RETENTION_PROBE_SQL = f"""
    SELECT j.id
    FROM jorb j
    WHERE j.state IN ({TERMINAL_STATES_SQL})
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
"""

CHECKPOINT_SWEEP_SQL = f"""
    WITH doomed AS MATERIALIZED (
        SELECT s.job_id, s.step_seq
        FROM jorb_step s
        JOIN jorb j ON j.id = s.job_id
        WHERE j.state IN ({TERMINAL_STATES_SQL})
          AND COALESCE(j.finished, j.updated) < now() - $1::interval
        ORDER BY COALESCE(j.finished, j.updated)
        FOR UPDATE OF s SKIP LOCKED
        LIMIT $2
    )
    DELETE FROM jorb_step s
    USING doomed d
    WHERE s.job_id = d.job_id AND s.step_seq = d.step_seq
"""

MAILBOX_SWEEP_SQL = """
    WITH doomed AS MATERIALIZED (
        SELECT id FROM jorb_mailbox
        WHERE consumed_at IS NOT NULL
          AND consumed_at < now() - $1::interval
        ORDER BY consumed_at
        FOR UPDATE SKIP LOCKED
        LIMIT $2
    )
    DELETE FROM jorb_mailbox m
    USING doomed d
    WHERE m.id = d.id
"""

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
    #: DELETEs are explained inside a transaction that is rolled back, so
    #: EXPLAIN ANALYZE measures the real plan without removing rows.
    mutating: bool = False


def hot_queries() -> tuple[HotQuery, ...]:
    """The queries whose plan is load-bearing, and why each one is here."""
    import datetime

    def retention_args(_queue: str) -> list[Any]:
        return [datetime.timedelta(days=3650), 1000]

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
            "retention_probe",
            "monitor retention sweep (every cycle, forever)",
            RETENTION_PROBE_SQL,
            retention_args,
        ),
        HotQuery(
            "checkpoint_sweep",
            "monitor checkpoint retention sweep",
            CHECKPOINT_SWEEP_SQL,
            retention_args,
            mutating=True,
        ),
        HotQuery(
            "mailbox_sweep",
            "monitor consumed-mailbox sweep",
            MAILBOX_SWEEP_SQL,
            retention_args,
            mutating=True,
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
    )


def walk_plan(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def summarize_plan(document: dict[str, Any]) -> dict[str, Any]:
    """Access method, index, and buffers — the three facts that matter.

    Buffers rather than milliseconds: a duration says how fast the machine
    is, a buffer count says how much of the table the query had to read,
    and only the second one stays true on someone else's hardware.

    The buffer count comes from the ROOT node, not from summing the tree:
    EXPLAIN's per-node buffer counts are cumulative, so a sum counts every
    child once per ancestor and reports a query as several times more
    expensive than it is.
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
        scans.append(
            {
                "node": node_type,
                "relation": relation,
                "index": node.get("Index Name"),
                "rows_removed_by_filter": int(node.get("Rows Removed by Filter", 0)),
                "actual_rows": int(node.get("Actual Rows", 0)),
            }
        )
        if node_type == "Seq Scan" and relation == "jorb":
            seq_scans.append(f"{node_type} on {relation}")
    indexes = sorted({str(s["index"]) for s in scans if s["index"]})
    return {
        "access_methods": sorted({str(s["node"]) for s in scans}),
        "indexes": indexes,
        "buffers": buffers,
        "scans": scans,
        "seq_scan_on_jorb": seq_scans,
        # Per-node and therefore genuinely additive, unlike buffers. This is
        # the "read the whole table to return nothing" tell: a plan can use
        # an index, stay off the seq-scan gate, and still discard every row
        # in the table because the index it chose was the wrong one.
        "rows_removed_by_filter": sum(s["rows_removed_by_filter"] for s in scans),
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
    job_ids = await conn.fetch(
        "SELECT id FROM jorb WHERE queue = $1 ORDER BY id LIMIT 200", queue
    )
    anchor = int(job_ids[0]["id"])
    steps = min(rows, 5000)
    await conn.execute(
        """
        INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
        SELECT j.id, i, 'bench', '{}'::jsonb, 0
        FROM jorb j, generate_series(1, 3) i
        WHERE j.queue = $1
        LIMIT $2
        """,
        queue,
        steps,
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
    await conn.execute("ANALYZE jorb")
    await conn.execute("ANALYZE jorb_step")
    await conn.execute("ANALYZE jorb_mailbox")
    return {"jobs": rows, "steps": steps, "mailbox": mailbox}


async def run_plans(
    target: Target,
    *,
    seed: int,
    planner_settings: Sequence[str],
    max_existing_jobs: int,
    force: bool,
) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS) every hot query and gate on seq scans.

    This is the CI-runnable half of the harness. Timings flake on a loaded
    box and pass on a fast one with the index dropped; a plan is a fact, and
    "did this query stop using its index" is the regression that stays
    correct while getting slower forever.
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
            summary = summarize_plan(document[0])
            summary["what"] = query.what
            queries[query.key] = summary

        offenders = sorted(k for k, v in queries.items() if v["seq_scan_on_jorb"])
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
                "seq_scan_offenders": offenders,
                "healthy": not offenders,
            }
        )
        return result
    finally:
        result["cleanup"] = await cleanup_queue(conn, queue)
        await conn.close()


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
    result = asyncio.run(
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
            "  NOTIFY commit-lock cost",
            (
                "n/a (needs --allow-trigger-toggle)"
                if lock["ratio"] is None
                else f"{lock['cost_pct']:.0f}% of throughput lost "
                f"({lock['ratio']:.2f}x ceiling)"
            ),
        ],
        [
            "  firehose off ONLY",
            f"{pct(lock['firehose_only_recovery_pct'])} — 3 of 5 notifications "
            "removed, ceiling unmoved: the lock is per COMMIT, not per NOTIFY",
        ],
        [
            "  wakeup notify off ONLY",
            f"{pct(lock['wakeup_only_recovery_pct'])} — the only NOTIFY on the "
            "INSERT path; removing it removes the commit lock",
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
) -> None:
    """Claim throughput and contention through the real claim_jorb()."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = asyncio.run(
        run_claim(
            target,
            workers=workers,
            jobs=jobs,
            repeat=repeat,
            warmup=warmup,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    uncapped = result["modes"]["uncapped"]
    capped = result["modes"]["capped"]
    emit(
        result,
        output_json,
        [
            ["claimers", fmt(result["workers"])],
            ["uncapped claims/s", fmt(uncapped["claims_per_second"]["median"])],
            ["uncapped spread", f"{uncapped['claims_per_second']['spread_pct']:.0f}%"],
            ["capped claims/s", fmt(capped["claims_per_second"]["median"])],
            ["capped / uncapped", f"{result['capped_throughput_ratio']:.2f}x"],
            [
                "capped advisory-lock misses",
                f"{capped['empty_claims_with_work_available']:,} "
                f"({capped['lock_miss_rate'] * 100:.1f}% of attempts)",
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
) -> None:
    """End-to-end throughput and latency with REAL worker processes."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = asyncio.run(
        run_e2e(
            target,
            jobs=jobs,
            workers=workers,
            repeat=repeat,
            warmup=warmup,
            timeout=timeout,
            max_existing_jobs=max_existing_jobs,
            force=force,
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
    """Notifications per job lifecycle, per channel, and the projection."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = asyncio.run(
        run_notify(
            target,
            lifecycles=lifecycles,
            target_rate=target_rate,
            max_existing_jobs=max_existing_jobs,
            force=force,
        )
    )
    table = [
        ["lifecycles driven", fmt(result["lifecycles"])],
        ["notifications per lifecycle", fmt(result["per_lifecycle"], 2)],
        [
            "docs/SCALE.md claims",
            f"{result['scale_md']['claimed_per_lifecycle']} "
            f"({'agrees' if result['scale_md']['agrees'] else 'DISAGREES'})",
        ],
        ["history rows per lifecycle", fmt(result["history_rows_per_lifecycle"], 2)],
        [
            f"projected at {result['target_rate']:.0f} jobs/s",
            f"{result['projected_notifications_per_second']:,.0f} notify/s",
        ],
        [
            "without the state firehose",
            f"{result['projected_without_firehose_per_second']:,.0f} notify/s",
        ],
        [
            "pg_notification_queue_usage",
            f"{result['notify_queue_usage']['before']:.6f} -> "
            f"{result['notify_queue_usage']['after']:.6f}",
        ],
    ]
    table.extend(
        [f"  channel {channel}", fmt(count)]
        for channel, count in result["per_channel"].items()
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
    """EXPLAIN (ANALYZE, BUFFERS) every hot query; non-zero on a seq scan."""
    target = resolve_target(pick(ctx, config, "config"), pick(ctx, dsn, "dsn"))
    result = asyncio.run(
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
            f"{data['rows_removed_by_filter']:,} rows discarded",
        ]
        for key, data in result["queries"].items()
    ]
    emit(result, output_json, table)
    if not result["healthy"]:
        offenders = ", ".join(result["seq_scan_offenders"])
        click.echo(
            f"FAIL: sequential scan of jorb in: {offenders}. These run on a "
            f"timer forever; a scan here stays correct and gets slower as the "
            f"table grows.",
            err=True,
        )
        raise SystemExit(1)


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
        }

    results = asyncio.run(everything())
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
        ["e2e jobs/s", fmt(results["e2e"]["jobs_per_second"]["median"], 2)],
        [
            "e2e latency p50/p99",
            f"{results['e2e']['enqueue_to_finished']['p50']:.3f} / "
            f"{results['e2e']['enqueue_to_finished']['p99']:.3f} s",
        ],
        ["notifications/lifecycle", fmt(results["notify"]["per_lifecycle"], 2)],
        [
            f"notify/s at {target_rate:.0f} jobs/s",
            fmt(results["notify"]["projected_notifications_per_second"]),
        ],
        [
            "hot query plans",
            "healthy"
            if results["healthy"]
            else "SEQ SCAN: " + ", ".join(results["plans"]["seq_scan_offenders"]),
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
