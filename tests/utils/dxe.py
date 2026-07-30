"""A real `Job` object driven directly, without a live worker.

Most DXE behaviour is worth testing through a running worker, because that is
what exercises claiming, binding and the retry path together. A few
properties are not: they are decisions the `Job` object makes in process —
whether a sequence has caught up to the recorded log, what the replay
decision is for a given checkpoint — and driving a whole worker to reach them
buries the property under timing.

This gives those tests a `Job` whose `self.s.ex(...)` goes straight to a pool.
It is deliberately NOT a mock: the statements are the real ones from `STMTS`,
the rows come back from a real database, and the only thing missing is the
worker loop around it.
"""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]

from pyjobby.pj import STMTS, Job


class _ConnStatement:
    """A prepared-statement stand-in bound to ONE connection.

    ``transaction()`` records its checkpoint through
    ``self.s.stmts["record-step"].fetch(...)`` precisely so the write joins
    the transaction already open on the worker's connection; a stand-in that
    routed through a pool would commit the checkpoint outside that
    transaction and silently void the atomicity under test.
    """

    def __init__(self, conn: asyncpg.Connection, sql: str) -> None:
        self._conn = conn
        self._sql = sql

    async def fetch(self, *args: Any) -> list[asyncpg.Record]:
        return list(await self._conn.fetch(self._sql, *args))


class ConnectionBoundSystem:
    """``PoolBoundSystem``'s sibling for primitives that need a CONNECTION.

    ``transaction()`` — and ``send()``, which runs through it — uses
    ``s.cxn`` for its transaction scope and ``s.stmts`` for the atomic
    checkpoint write. Everything else still goes through ``ex``, on the
    same connection, so a test drives exactly the statements the worker
    would issue in exactly the transaction scopes it would issue them in.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.cxn = conn
        self._cancel_current = False
        self.stmts = {name: _ConnStatement(conn, sql) for name, sql in STMTS.items()}

    async def ex(self, name: str, *args: Any) -> list[asyncpg.Record]:
        return list(await self.cxn.fetch(STMTS[name], *args))


async def connection_bound_job(
    conn: asyncpg.Connection,
    job_row: Any,
    epoch: int | None = None,
    cls: type[Job[Any]] = Job,
) -> Job[Any]:
    """A `Job` for `job_row` on one real connection, transaction-capable.

    Same contract as ``bound_job`` — real statements, real rows, recorded
    checkpoints loaded and bound — but ``transaction()`` and ``send()``
    work, because the system exposes the connection they scope to.

    ``cls`` builds a SUBCLASS instead, which is what lets a test drive a real
    ``StateMachineJob.task()`` at a chosen resume point: the machine's turn is
    a decision the object makes in process, and reaching a specific point in
    it through a live worker would bury the property under timing.
    """
    system = ConnectionBoundSystem(conn)
    job = cls(s=system, job=dict(job_row))  # type: ignore[arg-type]
    resolved = job_row["run_epoch"] if epoch is None else epoch
    checkpoints = await conn.fetch(STMTS["load-steps"], job_row["id"])
    job._dxe_bind(list(checkpoints), resolved)
    return job


class PoolBoundSystem:
    """The slice of `JobSystem` the DXE primitives actually reach for.

    `Job` calls `self.s.ex(name, *args)` and nothing else on its system for
    every primitive here, so this is the whole surface rather than a
    convenient subset — a primitive that started needing more would fail
    loudly on the attribute instead of quietly taking a mocked default.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self._cancel_current = False

    async def ex(self, name: str, *args: Any) -> list[asyncpg.Record]:
        return list(await self.pool.fetch(STMTS[name], *args))


async def bound_job(
    pool: asyncpg.Pool, job_row: Any, epoch: int | None = None
) -> Job[Any]:
    """A `Job` for `job_row`, bound to its recorded checkpoints and epoch.

    Loads the checkpoint log through the same statement the worker uses, so
    the object starts in exactly the state a resume would put it in.
    """
    system = PoolBoundSystem(pool)
    job = Job(s=system, job=dict(job_row))  # type: ignore[arg-type]
    resolved = job_row["run_epoch"] if epoch is None else epoch
    checkpoints = await pool.fetch(STMTS["load-steps"], job_row["id"])
    job._dxe_bind(list(checkpoints), resolved)
    return job
