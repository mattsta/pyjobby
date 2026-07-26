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
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import orjson


class JobState(enum.StrEnum):
    """All states a job row can be in (mirrors the ``jorbstate`` enum)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"
    CRASHED = "crashed"  # terminal: the dead letter queue
    CANCELLED = "cancelled"


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


def build_requeue_sql(allowed_states: tuple[str, ...] = ("crashed",)) -> str:
    """SQL that puts a terminal/in-flight job back in the queue.

    Jobs keep ONE row for life: a retry (automatic or operator-driven)
    requeues the same row, the per-attempt audit trail lives in
    jorb_history, and run_epoch (bumped at claim) fences any stale
    execution out of writing results or checkpoints.

    Parameters: $1 job_id, $2 delay (interval), $3 reset_errors (bool).
    """
    states = ", ".join(f"'{s}'" for s in allowed_states)
    return f"""UPDATE jorb
            SET state = 'queued',
                run_after = now() + $2::interval,
                error_count = CASE WHEN $3 THEN 0 ELSE error_count END,
                error_message = CASE WHEN $3 THEN NULL ELSE error_message END,
                error_backtrace = CASE WHEN $3 THEN NULL ELSE error_backtrace END,
                result = NULL,
                finished = NULL,
                timeout_at = NULL,
                cancel_requested = FALSE,
                updated = now()
            WHERE id = $1::bigint
              AND state IN ({states})
            RETURNING id"""


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


async def rerun_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
) -> int | None:
    """Run a terminal job again, INCLUDING one that already finished.

    Separate from :func:`retry_job` on purpose: re-running successful work
    repeats its side effects, so callers must ask for it by name.
    """
    return await requeue_job(
        conn,
        job_id,
        delay=delay,
        reset_errors=reset_errors,
        allowed_states=RERUNNABLE_STATES,
    )


async def requeue_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
    allowed_states: tuple[str, ...] = RETRYABLE_STATES,
) -> int | None:
    """Low-level requeue used by :func:`retry_job` and :func:`rerun_job`,
    and by the monitor (which requeues in-flight states). Prefer the named
    verbs; pass ``allowed_states`` only for a genuinely different guard.

    Returns the job id, or None if it wasn't in an allowed state."""
    if delay is None:
        delay = datetime.timedelta(0)
    requeued: int | None = await conn.fetchval(
        build_requeue_sql(allowed_states), job_id, delay, reset_errors
    )
    return requeued


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
