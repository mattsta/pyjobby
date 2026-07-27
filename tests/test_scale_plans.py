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
import re

import pytest

from pyjobby.monitor import (
    DELETE_RETIRED_WORKERS_SQL,
    SWEEP_CHECKPOINT_JOBS_SQL,
    SWEEP_ORPHANED_DAGS_SQL,
    SWEEP_RETIRED_WORKERS_SQL,
    SWEEP_SCHEDULE_LOG_SQL,
    TERMINAL_STATES,
)
from tests.utils.plans import (
    assert_no_seq_scan,
    assert_reads_far_less_than_a_scan,
    plan_for,
    reset_job_tables,
    rows_removed_by_filter,
    settle,
)

pytestmark = pytest.mark.asyncio

# Enough rows that the planner has a real choice. A seq scan of a tiny table
# is genuinely cheaper than an index, so under this the test proves nothing.
ROWS = 20_000

#: The sweeps' default batch, so a plan assertion can say "a batch's worth"
#: rather than a bare number.
BATCH = 1000


def rows_scanned_by(plan: str, index: str) -> int:
    """Rows the node using `index` actually returned.

    "Which index did it use" is only half the question -- an index scan that
    reads a table's worth of rows costs a table's worth. This reads the
    `actual rows=` of the node itself, so a test can say what the scan is
    bounded BY rather than merely which access method it chose.
    """
    name = re.escape(index)
    match = re.search(
        rf"(?:Index (?:Only )?Scan using {name} on \S+"
        rf"|Bitmap Index Scan on {name})[^\n]*actual rows=([\d.]+)",
        plan,
    )
    assert match, f"no node using {index} in\n{plan}"
    return int(float(match.group(1)))


async def explain_rolled_back(pool, sql: str, *args) -> str:
    """EXPLAIN (ANALYZE) a statement that WRITES, then undo it.

    The sweeps under test are DELETEs, and a plan is only a fact if the
    statement really ran — so it runs, and the transaction is rolled back.
    """
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            rows = await conn.fetch(
                "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) " + sql, *args
            )
            return "\n".join(r["QUERY PLAN"] for r in rows)
        finally:
            await tx.rollback()


async def seed_terminal_jobs(pool, queue: str, rows: int = ROWS) -> None:
    """Insert `rows` jobs spread over 60 days, mixed terminal and live.

    `created` is spread too, so a reporting window covers a realistic slice
    rather than the whole table -- a scan IS the right plan when the window
    matches everything, and a test that seeds it that way proves nothing.
    """
    await reset_job_tables(pool)
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
    await settle(pool)


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


class TestCheckpointSweepPlan:
    """The checkpoint sweep runs every cycle forever, like retention does.

    It is also the one that hid the longest, because an INDEX SCAN THAT
    DISCARDS EVERY ROW IT READS is not a sequential scan and passes a
    seq-scan check while costing exactly the same. The measured original --
    ``jorb_step JOIN jorb`` filtered on the job's retention expression --
    planned as a merge join driven by ``jorb_pkey``: 20,000 rows removed by
    filter and 534-1,194 buffers to delete nothing, growing with the table
    forever. So these assert the access method AND the rows it threw away.

    They run the monitor's own ``SWEEP_CHECKPOINT_JOBS_SQL``, not a copy of it:
    a plan gate that reads a duplicate of the statement certifies a query
    nobody executes as soon as the two drift.
    """

    #: One job in `STEP_EVERY` has checkpoints at all, which is the real
    #: shape -- only DXE jobs check point, and the sweep has to walk past the
    #: ones that do not. `STEPS_PER_JOB` then puts ~20k rows in jorb_step, so
    #: that table is big enough for a scan of it to be the wrong plan too.
    #:
    #: 3, not 4: seed_terminal_jobs assigns state by `i % 4` and ids are
    #: sequential, so every 4th job is the SAME state -- checkpointing every
    #: 4th would give them all to the one non-terminal state and seed a
    #: backlog with nothing in it.
    STEP_EVERY = 3
    STEPS_PER_JOB = 3

    async def seed(self, pool, queue: str) -> None:
        await seed_terminal_jobs(pool, queue)
        await pool.execute(
            """
            INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
            SELECT j.id, s, 'step', '{}', 1
              FROM jorb j, generate_series(1, $3) s
             WHERE j.queue = $1 AND j.id % $2 = 0
            """,
            queue,
            self.STEP_EVERY,
            self.STEPS_PER_JOB,
        )
        await settle(pool)
        await pool.execute("ANALYZE jorb_step")

    async def explain_sweep(self, pool, retention_days: float) -> str:
        """EXPLAIN the real sweep statement, rolled back so it deletes nothing."""
        return await explain_rolled_back(
            pool,
            SWEEP_CHECKPOINT_JOBS_SQL,
            datetime.timedelta(days=retention_days),
            BATCH,
        )

    async def test_nothing_expired_reads_almost_nothing(self, db_pool, unique_queue):
        """The steady state: caught up, asked every cycle, answer empty.

        Nothing for a LIMIT to stop early on, so a badly planned version has
        to examine every terminal row in the table to return none."""
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=3650)

        assert "jorb_retention_idx" in plan, plan
        assert "Seq Scan on jorb " not in plan, plan
        # the whole point: nothing read, so nothing thrown away. The original
        # form reported 20,000 here while deleting nothing.
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_a_backlog_is_still_driven_by_the_retention_index(
        self, db_pool, unique_queue
    ):
        """...and when there IS work, it must not degrade into a scan.

        The batch is bounded by jobs that actually HAVE checkpoints, which
        is also what stops the drain loop concluding it is caught up after a
        batch of step-less jobs."""
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=0)

        assert "jorb_retention_idx" in plan, plan
        assert "Seq Scan on jorb " not in plan, plan
        # It walks past step-less jobs to fill a batch, so it does discard
        # some — but the count scales with the BATCH and the fraction of jobs
        # that check point (here 1 in 3), never with the table. The form this
        # replaced discarded every terminal row in existence, every cycle.
        assert rows_removed_by_filter(plan) < self.STEP_EVERY * BATCH, plan

    async def test_the_sweep_deletes_only_terminal_jobs_checkpoints(
        self, db_pool, unique_queue
    ):
        """The plan is only worth asserting if the statement is still right."""
        from pyjobby.monitor import sweep_completed_checkpoints

        await self.seed(db_pool, unique_queue)
        # a live job that the seed did NOT already give checkpoints to, so
        # this one row is unambiguously the thing the sweep must not touch
        live = await db_pool.fetchval(
            """SELECT j.id FROM jorb j
                WHERE j.queue = $1 AND j.state = 'queued'
                  AND NOT EXISTS (SELECT 1 FROM jorb_step s WHERE s.job_id = j.id)
                LIMIT 1""",
            unique_queue,
        )
        await db_pool.execute(
            """INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
               VALUES ($1, 1, 'live', '{}', 1)""",
            live,
        )

        deleted = 0
        for _ in range(50):
            batch = await sweep_completed_checkpoints(db_pool, 0, batch_size=BATCH)
            deleted += batch
            if not batch:
                break

        assert deleted > 0, "the sweep found nothing to reap"
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step s JOIN jorb j ON j.id = s.job_id "
                "WHERE j.queue = $1 AND j.state IN ('finished','crashed','cancelled')",
                unique_queue,
            )
            == 0
        ), "terminal jobs kept checkpoints"
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id = $1", live
            )
            == 1
        ), "a live job's checkpoints were reaped"


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


class TestOrphanedDagSweepPlan:
    """The DAG sweep runs every cycle forever like the rest of retention.

    It has the same trap as the checkpoint sweep and one extra: as well as
    walking jorb_dag by an index, it asks "has this DAG any jobs left?" per
    candidate, and that question is a second chance to accidentally read the
    whole job table.
    """

    #: 2 in 3 DAGs still hold a job, so the sweep genuinely has to walk past
    #: them to fill a batch -- the shape that catches an index scan doing a
    #: table's worth of work.
    JOB_EVERY = 3

    async def seed(self, pool, queue: str) -> None:
        # jorb_dag is the PARENT, so truncating it needs CASCADE (which
        # empties jorb too) and therefore has to happen before the job seed.
        await pool.execute("TRUNCATE jorb_dag RESTART IDENTITY CASCADE")
        await pool.execute(
            """
            INSERT INTO jorb_dag (name, created)
            SELECT 'plan-dag', now() - (i % 60) * interval '1 day'
              FROM generate_series(1, $1) i
            """,
            ROWS,
        )
        await seed_terminal_jobs(pool, queue)
        # ids restart at 1 in both tables, so job i belongs to dag i
        await pool.execute(
            "UPDATE jorb SET dag_id = id WHERE id % $1 <> 0", self.JOB_EVERY
        )
        await settle(pool)
        await pool.execute("VACUUM (ANALYZE) jorb_dag")

    async def explain_sweep(self, pool, retention_days: float) -> str:
        return await explain_rolled_back(
            pool,
            SWEEP_ORPHANED_DAGS_SQL,
            datetime.timedelta(days=retention_days),
            BATCH,
        )

    async def test_nothing_expired_reads_almost_nothing(self, db_pool, unique_queue):
        """The steady state, and the one that runs every cycle forever."""
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=3650)

        assert "jorb_dag_retention_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_dag")
        assert_no_seq_scan(plan, "jorb")
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan, "jorb_dag")

    async def test_a_backlog_is_still_index_driven(self, db_pool, unique_queue):
        """...and with work to do it must not degrade into a scan of either
        table: jorb_dag by the retention index, jorb by jorb_dag_idx.

        The rows it discards are DAGs that still have jobs, so the count
        scales with the batch and the populated fraction -- never with how
        many DAGs the install has ever run.
        """
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=0)

        assert "jorb_dag_retention_idx" in plan, plan
        assert "jorb_dag_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_dag")
        assert_no_seq_scan(plan, "jorb")
        assert rows_removed_by_filter(plan) < self.JOB_EVERY * BATCH, plan


class TestScheduleLogSweepPlan:
    """The schedule log was the one table with no bound at all, so its sweep
    is the one most likely to meet a very large backlog on first run."""

    SCHEDULES = 50

    async def seed(self, pool) -> None:
        await pool.execute("TRUNCATE jorb_schedule RESTART IDENTITY CASCADE")
        await pool.execute(
            """
            INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run)
            SELECT 'plan-schedule-' || i, 'plan.Job', '* * * * *', now()
              FROM generate_series(1, $1) i
            """,
            self.SCHEDULES,
        )
        await pool.execute(
            """
            INSERT INTO jorb_schedule_log (schedule_id, schedule_name,
                                           scheduled_time, actual_time, result)
            SELECT 1 + (i % $2), 'plan-schedule',
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day',
                   'success'
              FROM generate_series(1, $1) i
            """,
            ROWS,
            self.SCHEDULES,
        )
        await pool.execute("VACUUM (ANALYZE) jorb_schedule_log")

    async def explain_sweep(self, pool, retention_days: float) -> str:
        return await explain_rolled_back(
            pool,
            SWEEP_SCHEDULE_LOG_SQL,
            datetime.timedelta(days=retention_days),
            BATCH,
        )

    async def test_nothing_expired_reads_almost_nothing(self, db_pool):
        """Ordering by actual_time is what makes this a range probe. Ordering
        by id would look equivalent -- id ascends with actual_time -- and
        would make the planner walk the primary key and filter every row in
        the table to discover that nothing has expired."""
        await self.seed(db_pool)

        plan = await self.explain_sweep(db_pool, retention_days=3650)

        assert "jorb_schedule_log_retention_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_schedule_log")
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan, "jorb_schedule_log")

    async def test_a_backlog_is_still_index_driven(self, db_pool):
        """The whole table expired at once -- the first-run shape.

        The only rows discarded are each schedule's newest, which the sweep
        refuses to delete, so the discard count is bounded by the number of
        SCHEDULES and not by the size of the log.
        """
        await self.seed(db_pool)

        plan = await self.explain_sweep(db_pool, retention_days=0)

        assert "jorb_schedule_log_retention_idx" in plan, plan
        # the "keep the newest" refusal is a per-row backwards index-only
        # probe of (schedule_id, id) -- max(id) for one schedule read off the
        # end of its own index range -- and not a scan or a hash of the log
        assert "Index Only Scan Backward using jorb_schedule_log_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_schedule_log")
        assert "Sort" not in plan, plan
        assert rows_removed_by_filter(plan) <= self.SCHEDULES, plan


class TestRetiredWorkerSweepPlan:
    """One row per worker process start, so this table grows with DEPLOYS --
    slowly, forever, and entirely unrelated to job throughput."""

    async def seed(self, pool, queue: str) -> None:
        await seed_terminal_jobs(pool, queue)
        await pool.execute("TRUNCATE jorb_worker RESTART IDENTITY")
        await pool.execute(
            """
            INSERT INTO jorb_worker (host, pid, queue, started, last_seen,
                                     shutdown_at)
            SELECT 'plan-host', i, $2,
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day'
              FROM generate_series(1, $1) i
            """,
            ROWS,
            queue,
        )
        await pool.execute("VACUUM (ANALYZE) jorb_worker")

    async def explain_probe(self, pool, retention_days: float) -> str:
        return await explain_rolled_back(
            pool,
            SWEEP_RETIRED_WORKERS_SQL,
            datetime.timedelta(days=retention_days),
            BATCH,
        )

    async def test_nothing_expired_reads_almost_nothing(self, db_pool, unique_queue):
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_probe(db_pool, retention_days=3650)

        assert "jorb_worker_retention_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_worker")
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan, "jorb_worker")

    async def test_a_backlog_is_still_index_driven(self, db_pool, unique_queue):
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_probe(db_pool, retention_days=0)

        assert "jorb_worker_retention_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_worker")
        assert rows_removed_by_filter(plan) == 0, plan

    async def test_the_in_flight_refusal_costs_the_in_flight_set(
        self, db_pool, unique_queue
    ):
        """The refusal is on the DELETE, and it must be paid in IN-FLIGHT
        jobs rather than in table size.

        jorb.claimed_by has no index and must not get one -- it is written on
        the claim path, on the hottest table in the system, to answer a
        question only retention asks. jorb_inflight_idx already covers exactly
        the two states this refusal cares about, and in-flight work is bounded
        by the fleet however big the job table grows: the assertion is that
        the join's inner side reads the in-flight rows and NOT the table.

        Buffers are deliberately not asserted here. This statement is a bulk
        DELETE, so most of what it touches is the heap and index writes for
        the rows it is removing -- that cost scales with the batch, which is
        the point of having a batch.
        """
        await self.seed(db_pool, unique_queue)
        in_flight = await db_pool.fetchval(
            """
            WITH picked AS (
                SELECT id FROM jorb WHERE queue = $1 ORDER BY id LIMIT 25
            )
            UPDATE jorb j SET state = 'running', claimed_by = j.id
              FROM picked p WHERE j.id = p.id
            RETURNING (SELECT count(*) FROM picked)
            """,
            unique_queue,
        )
        await settle(db_pool)
        doomed = [
            r["id"]
            for r in await db_pool.fetch(
                "SELECT id FROM jorb_worker ORDER BY shutdown_at LIMIT $1", BATCH
            )
        ]

        plan = await explain_rolled_back(db_pool, DELETE_RETIRED_WORKERS_SQL, doomed)

        assert "jorb_inflight_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb")
        assert rows_scanned_by(plan, "jorb_inflight_idx") == in_flight, plan


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
