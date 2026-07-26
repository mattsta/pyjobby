"""Shared database helpers for pyjobby.

Every pyjobby component talks to PostgreSQL through the helpers here so that
connection behavior is uniform everywhere:

- ``json``/``jsonb`` columns always encode/decode via orjson, so Python dicts
  go in and come out of every connection identically (workers, client library,
  CLI, web admin, websocket server, timeout monitor, scheduler).
- Job states are the ``JobState`` enum instead of scattered string literals.
"""

from __future__ import annotations

import enum
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import orjson


class JobState(enum.StrEnum):
    """All states a job row can be in (mirrors the ``jorbstate`` enum)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    HEARTBEAT = "heartbeat"
    CRASHED = "crashed"
    FINISHED = "finished"
    WAITING = "waiting"
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


def build_retry_sql(allowed_states: tuple[str, ...] = ("crashed",)) -> str:
    """SQL that inserts a fresh retry copy of a job (audit-preserving retry).

    This is THE retry statement — the worker, client library, admin API, and
    websocket server all use it so retry rows always look the same.

    Parameters: $1 job_id, $2 retry delay (interval), $3 error_count for the
    new row. The original row is left untouched (its terminal state is the
    audit trail); the copy gets both ``admin_data.parent_job_id`` and
    ``admin_data.retry_of`` stamped with the original id.
    """
    states = ", ".join(f"'{s}'" for s in allowed_states)
    return f"""INSERT INTO jorb (
                job_class, kwargs, queue, prio, uid, capability,
                run_after, run_group, admin_data, state, error_count
            )
            SELECT
                job_class, kwargs, queue, prio, uid, capability,
                TIMEZONE('utc', clock_timestamp()) + $2::interval AS run_after,
                run_group,
                (COALESCE(admin_data::text::jsonb, '{{}}'::jsonb)
                    || jsonb_build_object(
                        'parent_job_id', $1::bigint,
                        'retry_of', $1::bigint))::json AS admin_data,
                'queued' AS state,
                $3 AS error_count
            FROM jorb
            WHERE id = $1::bigint
              AND state IN ({states})
            RETURNING id"""


async def create_retry_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: Any = None,
    error_count: int = 0,
    allowed_states: tuple[str, ...] = ("crashed",),
) -> int | None:
    """Insert a retry copy of ``job_id``; returns the new job id or None
    if the original is missing / not in an allowed state."""
    import datetime

    if delay is None:
        delay = datetime.timedelta(0)
    new_job_id: int | None = await conn.fetchval(
        build_retry_sql(allowed_states), job_id, delay, error_count
    )
    return new_job_id


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
