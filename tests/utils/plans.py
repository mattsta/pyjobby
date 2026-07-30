"""Shared machinery for tests that assert a query PLAN rather than a result.

These tests ask "how was the answer reached?", because the paths they cover
stay correct as the table grows and simply get slower — the failure you find
in production, months in, when the table is finally big enough to matter.

Everything here exists because plan measurement has two traps that produce
confidently wrong answers, and both were hit for real before this module
existed:

**Seeding.** A sequential scan of a small table genuinely IS the cheaper plan,
so a plan assertion against a lightly-populated table proves nothing — and the
mix matters as much as the row count. Seeding a quarter of the table as
`queued` makes a scan the *correct* plan for the live-state queries and every
assertion meaningless.

**State.** A deleted row is not a gone row: it leaves a dead tuple and an unset
visibility-map bit, and the planner costs the same query differently as a
result. Tests that each seeded their own way drifted apart on whether they
vacuumed, which showed up as order-dependent failures — a file passing alone
and failing after its neighbours. Seeding therefore ALWAYS leaves a vacuumed,
analyzed table, so a plan test measures the same database no matter what ran
before it.

Assertions are about plan SHAPE — access method, rows discarded, buffers
relative to the table — never about duration. Timings flake on a loaded CI box
and pass on a fast one with the index dropped. A plan is a fact.
"""

from __future__ import annotations

import re

import asyncpg  # type: ignore[import-untyped]

# Enough rows that the planner has a real choice.
PLAN_ROWS = 20_000

_REMOVED_RE = re.compile(r"Rows Removed by (?:Filter|Index Recheck): (\d+)")
_BUFFERS_RE = re.compile(r"shared hit=(\d+)(?: read=(\d+))?")


async def seed_for_plans(
    pool: asyncpg.Pool, rows: int = PLAN_ROWS, queue_prefix: str = "plan_q"
) -> None:
    """A steady state at scale: a large terminal history, a small live set.

    The live states are bounded by work in flight however big the table gets;
    the terminal states are not. That asymmetry is what the plans under test
    have to cope with, so the seed reproduces it.

    Timestamps spread over 60 days so a reporting window covers a real slice
    rather than the whole table. `jorb_history` fills via its trigger on the
    way in — about one row per job here against ~4 in production, which only
    makes these tests kinder to the plans they are trying to catch.

    Starts from a truncated table and leaves it vacuumed and analyzed, so
    the measurement does not depend on what ran before it.
    """
    await reset_job_tables(pool)
    await pool.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, run_count,
                          created, updated, run_after, claimed_at,
                          started, finished)
        -- (i / 40) rather than (i % 5) so the live rows (every 40th) land
        -- across all five queues instead of piling into one.
        SELECT 'plan.Job', '{}', $2 || ((i / 40) % 5),
               CASE WHEN i % 40 = 0  THEN 'queued'
                    WHEN i % 400 = 1 THEN 'claimed'
                    WHEN i % 400 = 2 THEN 'running'
                    WHEN i % 400 = 3 THEN 'waiting'
                    WHEN i % 40 = 3  THEN 'crashed'
                    WHEN i % 40 = 7  THEN 'cancelled'
                    ELSE 'finished' END::jorbstate,
               1 + (i % 3),
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day'
        FROM generate_series(1, $1) i
        """,
        rows,
        queue_prefix,
    )
    await settle(pool)


async def seed_live_fleet(
    pool: asyncpg.Pool, queue_prefix: str = "plan_q", queues: int = 5
) -> None:
    """One live worker per queue `seed_for_plans` filled.

    Separate from the seed because only one plan under test needs it, and it
    is not free to add everywhere: several suites count ``jorb_worker`` rows.
    ``AdminAPI.unclaimable_jobs`` starts from the fleet -- a queue with no live
    worker produces no group and never reaches the job table at all -- so
    planning it against a workerless database measures nothing.

    The worker advertises no capability and no app_version, which is the
    ordinary fleet and the case the sweep must be cheap in: every arm has to
    report zero, and the question is how many rows it reads to do it.
    """
    await pool.execute(
        """
        INSERT INTO jorb_worker (host, pid, queue, max_prio, last_seen)
        SELECT 'plan-fleet', 6000 + i, $1 || i, 1000, now()
        FROM generate_series(0, $2 - 1) i
        """,
        queue_prefix,
        queues,
    )


async def reset_job_tables(pool: asyncpg.Pool) -> None:
    """Start from an empty, UNBLOATED job table.

    The per-test cleanup in conftest uses DELETE, which is correct for
    correctness tests and quietly fatal for plan tests: a deleted row leaves
    a dead tuple, so each 20k-row seed lays its rows across pages the last
    one left behind. After a handful of plan tests the heap holds several
    times the live rows, the same query touches proportionally more buffers,
    and assertions start failing purely because of what ran earlier -- a file
    that passes alone and fails in a suite.

    Plain VACUUM does not rescue it. It marks pages reusable rather than
    returning them, so the table stays large and the rows stay spread.
    TRUNCATE reclaims immediately, which is what makes a plan measurement
    reproducible.
    """
    await pool.execute(
        "TRUNCATE jorb, jorb_history, jorb_step, jorb_event, "
        "jorb_mailbox, jorb_dependencies RESTART IDENTITY CASCADE"
    )


async def settle(pool: asyncpg.Pool) -> None:
    """Leave the job tables in the state a running system actually has.

    ANALYZE for statistics, VACUUM for the visibility map that index-only
    scans need. Autovacuum does both continuously in production, so a test
    that skips them measures a table no running system ever has — and, worse,
    a different one depending on what ran before it.

    Never VACUUM FULL: it takes an exclusive lock and is not what production
    looks like either.
    """
    await pool.execute("VACUUM (ANALYZE) jorb")
    await pool.execute("VACUUM (ANALYZE) jorb_history")


async def plan_for(pool: asyncpg.Pool, sql: str, *args) -> str:
    """The EXPLAIN output for `sql`, actually executed."""
    rows = await pool.fetch(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) {sql}", *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


def rows_removed_by_filter(plan: str) -> int:
    """Every row the plan read and then threw away.

    The number that catches an index scan doing a table's worth of work: it
    is not a Seq Scan, so a seq-scan assertion passes it, and it costs the
    same. Summed across nodes because the discard can happen at any of them.
    """
    return sum(int(m.group(1)) for m in _REMOVED_RE.finditer(plan))


def buffers_in(plan: str) -> int:
    """Buffers the whole statement touched, from the root node's line.

    EXPLAIN reports buffers cumulatively up the tree, so summing every node
    would count each child once per ancestor.
    """
    match = _BUFFERS_RE.search(plan)
    if not match:
        return 0
    return int(match.group(1)) + int(match.group(2) or 0)


async def heap_pages(pool: asyncpg.Pool, table: str = "jorb") -> int:
    """How many pages a sequential scan of `table` would have to read."""
    pages: int = await pool.fetchval(
        "SELECT (pg_relation_size($1::regclass) / "
        "current_setting('block_size')::bigint)::bigint",
        table,
    )
    return max(pages, 1)


async def assert_reads_far_less_than_a_scan(
    pool: asyncpg.Pool, plan: str, table: str = "jorb"
) -> None:
    """The probe touched a small fraction of what reading the table costs.

    Calibrated against the table's CURRENT size rather than a fixed number.
    An absolute threshold looks precise and is not: dead tuples inflate both
    heap and index, so the same correct plan touches more buffers on a
    well-used database. A gate that passes only after VACUUM FULL is a gate
    nobody trusts, and the first flake teaches everyone to ignore it.
    """
    pages = await heap_pages(pool, table)
    touched = buffers_in(plan)
    assert touched * 10 < pages, (
        f"touched {touched} buffers against a {pages}-page {table}: "
        f"that is not a probe, that is a scan wearing an index\n{plan}"
    )


def assert_no_seq_scan(plan: str, table: str = "jorb") -> None:
    """No sequential scan of `table` anywhere in the plan.

    Necessary but NOT sufficient — always pair it with
    `rows_removed_by_filter`, because an index scan that discards every row
    it reads costs the same as a scan and passes this check.
    """
    assert f"Seq Scan on {table} " not in plan and not plan.endswith(
        f"Seq Scan on {table}"
    ), plan
