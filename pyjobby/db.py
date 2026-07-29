"""Shared database helpers for pyjobby.

Every pyjobby component talks to PostgreSQL through the helpers here so that
connection behavior is uniform everywhere:

- ``json``/``jsonb`` columns always encode/decode via orjson, so Python dicts
  go in and come out of every connection identically (workers, client library,
  CLI, web admin, websocket server, timeout monitor, scheduler).
- Job states are the ``JobState`` enum instead of scattered string literals.
"""

from __future__ import annotations

import datetime
import enum
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import orjson

from . import lifecycle


class JobState(enum.StrEnum):
    """All states a job row can be in (mirrors the ``jorbstate`` enum)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"
    CRASHED = "crashed"  # terminal: the dead letter queue
    CANCELLED = "cancelled"


# =========================================================================
# NOTIFY channels
# =========================================================================
# Every channel the schema can emit on, spelled once. The names are declared
# by ``pyjobby/sql/schema/90_notify.sql`` -- as the trigger's TG_ARGV[0] and
# as the branch of ``jorb_notify()`` that builds that channel's payload --
# and a LISTENer that types one of them slightly differently gets no error
# from PostgreSQL at all: LISTEN accepts any identifier, so a typo is a
# subscription to a channel nothing will ever send on, reporting a confident
# zero forever. Named constants make that a NameError at import instead.
#
# ``pj-bench notify`` asserts these against the running database's triggers.

#: Seconds between worker registry heartbeats (``pj --heartbeat-interval``).
#: It lives HERE because two different processes have to agree about it: the
#: worker writes ``jorb_worker.last_seen`` on this cadence, and the monitor's
#: ``--liveness-grace`` judges staleness against it. A grace below the
#: heartbeat interval makes every LIVE worker look dead between beats -- the
#: monitor then requeues in-flight jobs from workers that are fine, over and
#: over, and no job longer than the grace can ever finish. The monitor warns
#: at startup when the two are configured into that state.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 10.0

#: A claimable job appeared on a queue some worker has published demand for
#: (payload: the queue name). The wakeup an idle worker sleeps on.
CHANNEL_ENQUEUED: Final[str] = "jorb_enqueued"

#: A job somebody is waiting on reached a terminal state (payload:
#: {"id", "state"}). Gated on ``jorb.awaited``, which is what makes
#: wait_for_result() cost nothing when nobody is waiting.
CHANNEL_DONE: Final[str] = "jorb_done"

#: A job published an event key (payload: {"job_id", "key"}), gated on the
#: job being awaited.
CHANNEL_EVENT: Final[str] = "jorb_event"

#: A RUNNING job was asked to stop (payload: the job id). The executing
#: worker cancels the task at its next await point.
CHANNEL_CANCEL: Final[str] = "jorb_cancel"

#: A recurring schedule fired (payload: {"schedule_id", "schedule_name",
#: "result", "job_id"}). Ungated: its consumer has no polling fallback.
CHANNEL_SCHEDULE_EXECUTED: Final[str] = "schedule_executed"


def _orjson_encode(obj: Any) -> str:
    # orjson.dumps returns bytes, but asyncpg expects str for text-format types
    return orjson.dumps(obj).decode("utf-8")


async def register_json_codecs(conn: asyncpg.Connection) -> None:
    """Use orjson for ``json``/``jsonb`` values on this connection.

    Safe to use directly as an asyncpg pool ``init`` hook.
    """
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=_orjson_encode,
            decoder=orjson.loads,
            schema="pg_catalog",
        )


def utcnow() -> datetime.datetime:
    """The current instant as an aware UTC datetime (the platform's only
    Python-side time representation; every schema column is timestamptz)."""
    return datetime.datetime.now(datetime.UTC)


def build_requeue_sql(
    allowed_states: tuple[str, ...] = ("crashed",),
    *,
    many: bool = False,
    wipe_checkpoints: bool = False,
) -> str:
    """SQL that puts a terminal/in-flight job back in the queue.

    Jobs keep ONE row for life: a retry (automatic or operator-driven)
    requeues the same row, the per-attempt audit trail lives in
    jorb_history, and run_epoch fences any stale execution out of writing
    results or checkpoints.

    The requeue bumps run_epoch itself rather than leaving that to the next
    claim. Otherwise the abandoned execution keeps the current epoch for the
    whole window between requeue and re-claim, and statements guarded ONLY by
    the epoch -- recording a DXE checkpoint, setting a timeout -- would still
    apply, letting a job the platform has given up on write checkpoints for
    the attempt that replaces it. Terminal writes were never exposed: they
    also guard on state IN ('claimed','running'). Checkpoints are loaded
    without an epoch filter, so bumping costs no resume capability.

    ``wipe_checkpoints`` deletes the job's jorb_step rows in the same
    statement: a resume replays checkpoints regardless of epoch, so a re-RUN
    ("do it again anyway", repeating side effects) must discard them or the
    durable job would fast-forward over the very work it was asked to redo.
    Retry leaves them (that IS resume). One statement, so the wipe and the
    requeue commit together and no re-claim can land between them.

    Parameters: $1 job_id, $2 delay (interval), $3 reset_errors (bool).
    """
    states = ", ".join(f"'{s}'" for s in allowed_states)
    target = "id = ANY($1::bigint[])" if many else "id = $1::bigint"
    requeue = f"""UPDATE jorb
            SET state = 'queued',
                run_epoch = run_epoch + 1,
                run_after = now() + $2::interval,
                error_count = CASE WHEN $3 THEN 0 ELSE error_count END,
                error_message = CASE WHEN $3 THEN NULL ELSE error_message END,
                error_backtrace = CASE WHEN $3 THEN NULL ELSE error_backtrace END,
                result = NULL,
                finished = NULL,
                timeout_at = NULL,
                cancel_requested = FALSE,
                updated = now()
            WHERE {target}
              AND state IN ({states})
            RETURNING id"""
    if not wipe_checkpoints:
        return requeue
    return f"""WITH bumped AS (
            {requeue}
        ), wiped AS (
            DELETE FROM jorb_step WHERE job_id IN (SELECT id FROM bumped)
        )
        SELECT id FROM bumped"""


#: States a RETRY may start from. Retry means "this job did not succeed;
#: run it again", so a job that already finished is deliberately excluded —
#: re-running successful work risks duplicate side effects and must be an
#: explicit decision (see ``rerun_job``).
RETRYABLE_STATES: tuple[str, ...] = ("crashed", "cancelled")

#: States a RE-RUN may start from: any terminal state, including success.
#: This is the operator's "do it again anyway" verb.
RERUNNABLE_STATES: tuple[str, ...] = ("crashed", "cancelled", "finished")


async def retry_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
) -> int | None:
    """Retry a job that did not succeed (crashed or cancelled).

    THE retry verb for every surface — client, admin API, CLI, websocket —
    so no surface can be more permissive than another. Returns the job id,
    or None if the job was not in a retryable state.
    """
    return await requeue_job(
        conn,
        job_id,
        delay=delay,
        reset_errors=reset_errors,
        allowed_states=RETRYABLE_STATES,
    )


async def retry_jobs(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_ids: list[int],
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
) -> list[int]:
    """retry_job() over a list, as ONE statement.

    Same guard, same semantics, same bumped fence — the only difference is
    `id = ANY($1)` instead of a round trip per id, which is what makes a
    thousand-job DLQ retry one statement instead of a thousand. Returns the
    ids actually requeued (jobs keep their id across retries), omitting any
    that were not in a retryable state.
    """
    if not job_ids:
        return []
    if delay is None:
        delay = datetime.timedelta(0)
    rows = await conn.fetch(
        build_requeue_sql(RETRYABLE_STATES, many=True), job_ids, delay, reset_errors
    )
    return [r["id"] for r in rows]


async def rerun_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
    fresh: bool = True,
) -> int | None:
    """Run a terminal job again, INCLUDING one that already finished.

    Separate from :func:`retry_job` on purpose: re-running successful work
    repeats its side effects, so callers must ask for it by name.

    ``fresh`` (the default) discards the job's DXE checkpoint log so the run
    actually re-executes -- a durable job's checkpoints are replayed with no
    epoch filter, so without the wipe a rerun would fast-forward over the
    very steps it was asked to redo and repeat nothing. Pass ``fresh=False``
    to keep the checkpoints, i.e. RESUME an interrupted durable job from
    where it stopped rather than restart it.
    """
    return await requeue_job(
        conn,
        job_id,
        delay=delay,
        reset_errors=reset_errors,
        allowed_states=RERUNNABLE_STATES,
        wipe_checkpoints=fresh,
    )


async def requeue_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
    allowed_states: tuple[str, ...] = RETRYABLE_STATES,
    wipe_checkpoints: bool = False,
) -> int | None:
    """Low-level requeue used by :func:`retry_job` and :func:`rerun_job`,
    and by the monitor (which requeues in-flight states). Prefer the named
    verbs; pass ``allowed_states`` only for a genuinely different guard.

    ``wipe_checkpoints`` discards the job's DXE checkpoint log so the next
    attempt re-executes from the start; retry leaves it to resume.

    Returns the job id, or None if it wasn't in an allowed state."""
    if delay is None:
        delay = datetime.timedelta(0)
    requeued: int | None = await conn.fetchval(
        build_requeue_sql(allowed_states, wipe_checkpoints=wipe_checkpoints),
        job_id,
        delay,
        reset_errors,
    )
    return requeued


#: THE queue-statistics query. Every surface that reports per-queue,
#: per-state counts reads it from here (client ``queue_stats``/``list_queues``,
#: ``AdminAPI.queue_stats``) so no two surfaces can disagree about what a
#: number means. Parameters: $1 = the recency window as an interval,
#: $2 = a queue name, or NULL for every queue. Every arm returns
#: ``(queue, state text, n bigint)``.
#:
#: TWO THINGS THE SHAPE ENCODES.
#:
#: 1. LIVE STATES EXACTLY, TERMINAL STATES WITHIN A WINDOW. Live work
#:    (queued/scheduled/claimed/running/waiting) is bounded by work in
#:    progress however big the table gets; the terminal states are bounded by
#:    nothing at all. "How many jobs finished, EVER" is an audit question for
#:    SQL, not a number a dashboard asks for on a timer, so the terminal arm
#:    counts only what landed inside $1. One arm per partial index, rather
#:    than a single GROUP BY, is also what keeps this off a scan of the whole
#:    table: a predicate spanning several states matches none of the partial
#:    indexes and collapses into a sequential scan.
#:
#: 2. 'queued' IS SPLIT FROM 'scheduled'. A job whose ``run_after`` is still
#:    in the future -- a retry backoff, an enqueue-at -- is not backlog. It is
#:    exactly where it was asked to be, and counting it as queued makes a
#:    healthy install look like a stuck one, which is the number an operator
#:    pages on. So:
#:
#:      state="queued"     claimable RIGHT NOW.
#:      state="scheduled"  deliberately parked in the future. Not a backlog.
#:
#:    The split is also what gives BOTH halves a real index condition against
#:    ``jorb_claim_idx`` (queue, prio, run_after) instead of a bare
#:    ``state = 'queued'`` filter whose index-only-ness depends on when
#:    autovacuum last ran. ``web_admin.PROM_SQL_LIVE_STATES`` and
#:    ``websocket_server.SNAPSHOT_SQL`` ask the same two questions with the
#:    same predicates and emit the same two state names (their strings stay
#:    separate because their plans are pinned and they carry extra columns);
#:    web_admin's comment has the full planner history.
#:
#: $2 is written as an OR-NULL predicate, which trades the index *condition*
#: for a filter over each partial index's bounded live set -- the price of one
#: query shape serving both "this queue" and "all queues".
QUEUE_STATS_SQL = """
    SELECT queue, 'queued' AS state, COUNT(*)::bigint AS n
      FROM jorb
     WHERE state = 'queued' AND run_after <= now()
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, 'scheduled', COUNT(*)::bigint
      FROM jorb
     WHERE state = 'queued' AND run_after > now()
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, state::text, COUNT(*)::bigint
      FROM jorb
     WHERE state IN ('claimed', 'running') AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue, state
    UNION ALL
    SELECT queue, 'waiting', COUNT(*)::bigint
      FROM jorb
     WHERE state = 'waiting' AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, state::text, COUNT(*)::bigint
      FROM jorb
     WHERE state IN ('finished', 'crashed', 'cancelled')
       AND COALESCE(finished, updated) >= now() - $1::interval
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue, state
"""

#: The reported state names :data:`QUEUE_STATS_SQL` can emit: every
#: ``jorbstate`` label plus the ``scheduled`` split of ``queued``. Callers
#: zero-fill their result dicts from this so a quiet queue reports 0 rather
#: than a missing key.
QUEUE_STATS_STATES: tuple[str, ...] = (*lifecycle.JOB_STATES, "scheduled")


CANCEL_SQL = """UPDATE jorb
        SET state = CASE WHEN state IN ('queued', 'waiting')
                         THEN 'cancelled'::jorbstate ELSE state END,
            cancel_requested = CASE WHEN state IN ('claimed', 'running')
                                    THEN TRUE ELSE cancel_requested END,
            finished = CASE WHEN state IN ('queued', 'waiting')
                            THEN now() ELSE finished END,
            updated = now()
        WHERE id = $1
          AND state IN ('queued', 'waiting', 'claimed', 'running')
        RETURNING state, cancel_requested"""

#: cancel over a list, one statement — identical CASE logic to CANCEL_SQL.
CANCEL_MANY_SQL = CANCEL_SQL.replace("WHERE id = $1", "WHERE id = ANY($1::bigint[])")


async def cancel_job(
    conn: asyncpg.Connection | asyncpg.Pool, job_id: int
) -> str | None:
    """Cancel a job wherever it is in its lifecycle (the one cancel path
    shared by the client, admin API, and websocket server).

    Queued/waiting jobs are cancelled immediately. Claimed/running jobs get
    cancel_requested set — the jorb_cancel NOTIFY reaches the executing
    worker, which cancels the task at its next await point.

    Returns 'cancelled', 'cancel_requested', or None (job not cancellable).
    """
    row = await conn.fetchrow(CANCEL_SQL, job_id)
    if row is None:
        return None
    if row["state"] == "cancelled":
        return "cancelled"
    return "cancel_requested"


async def cancel_jobs(
    conn: asyncpg.Connection | asyncpg.Pool, job_ids: list[int]
) -> int:
    """cancel_job() over a list, as ONE statement.

    Returns how many jobs the cancel reached (cancelled outright or
    cancel-requested); ids not in a cancellable state are simply not
    counted, matching the single verb returning None for them.
    """
    if not job_ids:
        return 0
    rows = await conn.fetch(CANCEL_MANY_SQL, job_ids)
    return len(rows)


async def connect(*args: Any, **kwargs: Any) -> asyncpg.Connection:
    """``asyncpg.connect`` with pyjobby's JSON codecs registered."""
    conn = await asyncpg.connect(*args, **kwargs)
    await register_json_codecs(conn)
    return conn


async def create_pool(*args: Any, **kwargs: Any) -> asyncpg.Pool:
    """``asyncpg.create_pool`` with pyjobby's JSON codecs registered.

    A caller-provided ``init`` hook is still honored; codecs register first.
    """
    caller_init = kwargs.pop("init", None)

    async def _init(conn: asyncpg.Connection) -> None:
        await register_json_codecs(conn)
        if caller_init is not None:
            await caller_init(conn)

    return await asyncpg.create_pool(*args, init=_init, **kwargs)
