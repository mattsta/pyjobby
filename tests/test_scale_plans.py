"""Query plans for the paths that run against the whole job table.

Most tests here ask "is the answer right?". These ask "how was it reached?",
because the two paths below stay correct as the table grows and simply get
slower — which is the failure mode you discover in production, months in,
when the table is finally big enough to matter.

Both are on timers. The retention sweep runs every cycle forever, and once it
has caught up its honest answer is usually "nothing expired" — the most
expensive way to say nothing. /metrics is scraped by Prometheus on an
interval. A sequential scan in either turns the monitoring into the outage.

These assert the PLAN, not a duration: timings are machine-dependent and
would either flake on a loaded CI box or pass on a fast one with the index
dropped. A plan is a fact.
"""

from __future__ import annotations

import datetime

import pytest

from pyjobby.monitor import TERMINAL_STATES

pytestmark = pytest.mark.asyncio

# Enough rows that the planner has a real choice. A seq scan of a tiny table
# is genuinely cheaper than an index, so under this the test proves nothing.
ROWS = 20_000


async def seed_terminal_jobs(pool, queue: str, rows: int = ROWS) -> None:
    """Insert `rows` jobs spread over 60 days, mixed terminal and live.

    `created` is spread too, so a reporting window covers a realistic slice
    rather than the whole table -- a scan IS the right plan when the window
    matches everything, and a test that seeds it that way proves nothing.
    """
    await pool.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, created, finished, updated)
        SELECT 'scale.Job', '{}', $1,
               (ARRAY['finished','crashed','cancelled','queued'])[1 + (i % 4)]::jorbstate,
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day',
               now() - (i % 60) * interval '1 day'
        FROM generate_series(1, $2) i
        """,
        queue,
        rows,
    )
    await pool.execute("ANALYZE jorb")


async def plan_for(pool, sql: str, *args) -> str:
    rows = await pool.fetch(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) {sql}", *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


class TestRetentionScanPlan:
    """The sweep must find expired jobs by index, not by reading the table."""

    async def test_retention_probe_uses_the_index_when_nothing_is_expired(
        self, db_pool, unique_queue
    ):
        """The steady state: caught up, nothing aged out, asked every cycle.

        This is the expensive case precisely because the answer is empty --
        there is nothing for the LIMIT to stop early on, so an unindexed
        predicate examines every terminal row in the table to return none.

        Ordering matters as much as the index here: ORDER BY id makes the
        planner prefer a pkey scan to avoid a sort and then filter everything
        (measured at 20k rows: 465 buffers, 20000 rows discarded), while
        ordering by the indexed expression is a 2-buffer index scan. Oldest
        first is also what retention means.
        """
        await seed_terminal_jobs(db_pool, unique_queue)

        plan = await plan_for(
            db_pool,
            """
            SELECT j.id FROM jorb j
             WHERE j.state = ANY($1::jorbstate[])
               AND COALESCE(j.finished, j.updated) < now() - $2::interval
             ORDER BY COALESCE(j.finished, j.updated) LIMIT 1000
            """,
            list(TERMINAL_STATES),
            datetime.timedelta(days=3650),
        )

        assert "jorb_retention_idx" in plan, plan
        assert "Seq Scan on jorb" not in plan, plan

    async def test_retention_probe_uses_the_index_when_a_backlog_exists(
        self, db_pool, unique_queue
    ):
        """...and when there IS work to do, so the sweep does not degrade
        into a scan the moment it falls behind."""
        await seed_terminal_jobs(db_pool, unique_queue)

        plan = await plan_for(
            db_pool,
            """
            SELECT j.id FROM jorb j
             WHERE j.state = ANY($1::jorbstate[])
               AND COALESCE(j.finished, j.updated) < now() - $2::interval
             ORDER BY COALESCE(j.finished, j.updated) LIMIT 1000
            """,
            list(TERMINAL_STATES),
            datetime.timedelta(days=30),
        )

        assert "Seq Scan on jorb" not in plan, plan


class TestMetricsScanPlan:
    """/metrics is scraped on a timer; it must not read the whole table.

    The window is on `created`, not `updated`: `updated` changes on every
    state transition, so indexing it would cost an index write per transition
    and block HOT updates on the hottest table -- a permanent write-path tax
    for a read that happens once a scrape.
    """

    async def test_metrics_window_uses_an_index(self, db_pool, unique_queue):
        await seed_terminal_jobs(db_pool, unique_queue)

        plan = await plan_for(
            db_pool,
            """
            SELECT state, count(*) FROM jorb
             WHERE created >= now() - $1::interval
             GROUP BY state
            """,
            datetime.timedelta(hours=1),
        )

        assert "jorb_created_idx" in plan, plan


class TestMailboxSweepPlan:
    """The mailbox sweep is the only thing that reaps a live job's read mail.

    `recv` marks a message consumed but never deletes it, and the job-scoped
    cascade cannot reach a job that is still alive -- so a durable workflow
    running for months accumulates every message it has read. This sweep runs
    every cycle forever, which makes its plan matter as much as retention's.
    """

    async def test_consumed_probe_uses_the_index(self, db_pool, unique_queue):
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('scale.Job', '{}', $1, 'running') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
            SELECT $1, 't', '{}',
                   CASE WHEN i % 10 = 0 THEN NULL
                        ELSE now() - (i % 30) * interval '1 day' END
            FROM generate_series(1, 20000) i
            """,
            job_id,
        )
        await db_pool.execute("ANALYZE jorb_mailbox")

        plan = await plan_for(
            db_pool,
            """
            SELECT id FROM jorb_mailbox
             WHERE consumed_at IS NOT NULL
               AND consumed_at < now() - $1::interval
             ORDER BY consumed_at LIMIT 1000
            """,
            datetime.timedelta(days=3650),
        )

        assert "jorb_mailbox_consumed_idx" in plan, plan
        # index order means the batch needs no sort, however big the backlog
        assert "Sort" not in plan, plan


class TestCascadeIndexes:
    """Deleting a job must not scan its child tables to find their rows.

    Postgres does NOT create an index for a foreign key automatically. Without
    one, every cascade delete is a sequential scan of the child table — and
    jorb_history is the largest table in the system, so retention would grind
    to a halt exactly when it is needed most.
    """

    async def test_every_cascading_child_can_find_its_rows_by_index(self, db_pool):
        rows = await db_pool.fetch(
            """
            SELECT c.conrelid::regclass::text AS child,
                   a.attname                  AS fk_column,
                   EXISTS (
                       SELECT 1 FROM pg_index i
                       WHERE i.indrelid = c.conrelid
                         AND i.indkey[0] = c.conkey[1]
                   )                          AS has_leading_index
            FROM pg_constraint c
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f' AND c.confrelid = 'jorb'::regclass
            """
        )

        assert rows, "expected foreign keys to jorb"
        unindexed = [r["child"] for r in rows if not r["has_leading_index"]]
        assert unindexed == [], (
            f"cascade delete would sequentially scan: {unindexed} "
            "— add an index on the referencing column"
        )
