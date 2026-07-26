"""PROBE 2 (temporary) — deterministic admission-control failures."""

from __future__ import annotations

import asyncpg
import pytest

from pyjobby.pj import STMTS

pytestmark = pytest.mark.asyncio

CLAIM_ARGS = (4242, "probe-host", None, ["test"], 1000, None)


async def _insert(pool, queue):
    return await pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.OkJob",
        {},
        queue,
    )


async def _claim(conn, queue):
    return await conn.fetchrow(
        STMTS["claim"], 4242, "probe-host", queue, ["test"], 1000, None
    )


async def test_probe_max_concurrency_uncommitted(db_pool, db_params, unique_queue):
    """cap=1: claim #2 issued while claim #1 is uncommitted."""
    await db_pool.execute(
        "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 1)", unique_queue
    )
    a = await _insert(db_pool, unique_queue)
    b = await _insert(db_pool, unique_queue)

    c1 = await asyncpg.connect(**db_params)
    c2 = await asyncpg.connect(**db_params)
    try:
        tx = c1.transaction()
        await tx.start()
        first = await _claim(c1, unique_queue)
        second = await _claim(c2, unique_queue)
        await tx.commit()
    finally:
        await c1.close()
        await c2.close()

    print(
        f"\nPROBE cap=1 uncommitted: first={first and first['id']} "
        f"second={second and second['id']} (jobs {a},{b})"
    )
    assert second is None


async def test_probe_rate_limit_committed_claim_not_counted(
    db_pool, db_params, unique_queue
):
    """rate_limit=1: claim #2 after claim #1 COMMITTED but before 'run'."""
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 1, 60)""",
        unique_queue,
    )
    await _insert(db_pool, unique_queue)
    await _insert(db_pool, unique_queue)

    c1 = await asyncpg.connect(**db_params)
    try:
        first = await _claim(c1, unique_queue)
        second = await _claim(c1, unique_queue)
    finally:
        await c1.close()

    inflight = await db_pool.fetchval(
        "SELECT count(*) FROM jorb WHERE queue=$1 AND state='claimed'", unique_queue
    )
    print(
        f"\nPROBE rate=1 pre-run: first={first and first['id']} "
        f"second={second and second['id']} claimed={inflight}"
    )
    assert second is None


async def test_probe_rate_limit_after_run(db_pool, db_params, unique_queue):
    """rate_limit=1, with 'run' actually executed: the next claim must fail."""
    await db_pool.execute(
        """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
           VALUES ($1, 1, 60)""",
        unique_queue,
    )
    await _insert(db_pool, unique_queue)
    await _insert(db_pool, unique_queue)

    c1 = await asyncpg.connect(**db_params)
    try:
        first = await _claim(c1, unique_queue)
        await c1.execute(STMTS["run"], first["id"], first["run_epoch"])
        second = await _claim(c1, unique_queue)
    finally:
        await c1.close()

    print(f"\nPROBE rate=1 post-run: second={second and second['id']}")
    assert second is None, "post-run claim was admitted"
