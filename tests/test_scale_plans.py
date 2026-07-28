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

from pyjobby import dxe
from pyjobby.admin_api import ADMIN_OLDEST_QUEUED_AGE_SQL
from pyjobby.db import QUEUE_STATS_SQL
from pyjobby.lifecycle import TERMINAL_STATES
from pyjobby.monitor import (
    DELETE_MAILBOX_SQL,
    DELETE_RETIRED_WORKERS_SQL,
    SWEEP_CHECKPOINT_JOBS_SQL,
    SWEEP_MAILBOX_SQL,
    SWEEP_ORPHANED_DAGS_SQL,
    SWEEP_RETIRED_WORKERS_SQL,
    SWEEP_SCHEDULE_LOG_SQL,
)
from pyjobby.pj import STMTS
from pyjobby.scheduler import BACKPRESSURE_COUNT_SQL, CONCURRENCY_COUNT_SQL
from tests.utils.plans import (
    assert_no_seq_scan,
    assert_reads_far_less_than_a_scan,
    buffers_in,
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

        assert "jorb_finished_retention_idx" in plan, plan
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

        # rides the FINISHED-only partial index, not the all-terminal one:
        # the sweep reaps only finished checkpoints (crashed/cancelled are
        # retryable), so its index must not hand it crashed/cancelled rows
        # to walk past and discard.
        assert "jorb_finished_retention_idx" in plan, plan
        assert "Seq Scan on jorb " not in plan, plan
        # It still walks past step-less FINISHED jobs to fill a batch, so it
        # discards some — but the count scales with the BATCH and the
        # checkpointed fraction, never with the table or the crash rate.
        assert rows_removed_by_filter(plan) < self.STEP_EVERY * BATCH, plan

    async def test_the_sweep_deletes_only_finished_jobs_checkpoints(
        self, db_pool, unique_queue
    ):
        """The plan is only worth asserting if the statement is still right:
        finished checkpoints go, crashed/cancelled (retryable) stay, live
        jobs are never touched."""
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
                "WHERE j.queue = $1 AND j.state = 'finished'",
                unique_queue,
            )
            == 0
        ), "finished jobs kept checkpoints"
        # crashed/cancelled are retryable: their checkpoints MUST survive so a
        # DLQ retry resumes instead of re-running completed steps
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step s JOIN jorb j ON j.id = s.job_id "
                "WHERE j.queue = $1 AND j.state IN ('crashed','cancelled')",
                unique_queue,
            )
            > 0
        ), "retryable jobs' checkpoints were wrongly reaped"
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

    It was the LAST sweep still shaped as ``DELETE ... USING (CTE)``, and the
    form cost exactly what it cost everywhere else: the probe picked its batch
    by index and the delete then hash-joined a sequential scan of the whole
    mailbox against keys it was already holding (measured here at a 20,000-row
    backlog: 4,750 buffers, 331 of them the scan, to delete 1,000 rows). The
    steady-state case planned fine, which is why it survived -- so the backlog
    case below is the one that actually gates the shape.
    """

    async def seed(self, pool, queue: str) -> None:
        job_id = await pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('scale.Job', '{}', $1, 'running') RETURNING id""",
            queue,
        )
        await pool.execute(
            """
            INSERT INTO jorb_mailbox (dest_job_id, topic, message, consumed_at)
            SELECT $1, 't', '{}',
                   CASE WHEN i % 10 = 0 THEN NULL
                        ELSE now() - (i % 30) * interval '1 day' END
            FROM generate_series(1, $2) i
            """,
            job_id,
            ROWS,
        )
        await pool.execute("VACUUM (ANALYZE) jorb_mailbox")

    async def explain_sweep(self, pool, retention_days: float) -> str:
        return await explain_rolled_back(
            pool,
            SWEEP_MAILBOX_SQL,
            datetime.timedelta(days=retention_days),
            BATCH,
        )

    async def test_nothing_expired_reads_almost_nothing(self, db_pool, unique_queue):
        """The steady state, run every cycle forever."""
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=3650)

        assert "jorb_mailbox_consumed_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb_mailbox")
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan, "jorb_mailbox")

    async def test_a_backlog_is_still_index_driven(self, db_pool, unique_queue):
        """A full backlog: the case the CTE form got wrong.

        The probe is bounded by the BATCH -- it reads a batch's worth of index
        entries and their heap rows and stops -- so what it touches must not
        scale with the mailbox. Unconsumed messages are not in the partial
        index at all, so nothing is read and discarded either.
        """
        await self.seed(db_pool, unique_queue)

        plan = await self.explain_sweep(db_pool, retention_days=0)

        assert "jorb_mailbox_consumed_idx" in plan, plan
        # index order means the batch needs no sort, however big the backlog
        assert "Sort" not in plan, plan
        assert_no_seq_scan(plan, "jorb_mailbox")
        assert rows_removed_by_filter(plan) == 0, plan

    async def test_the_delete_finds_its_batch_by_primary_key(
        self, db_pool, unique_queue
    ):
        """...and the second statement must not re-read the table either.

        This is the assertion the CTE form could not have passed: its delete
        stage sequentially scanned jorb_mailbox to join a batch it had just
        been handed.
        """
        await self.seed(db_pool, unique_queue)
        doomed = [
            r["id"]
            for r in await db_pool.fetch(
                "SELECT id FROM jorb_mailbox WHERE consumed_at IS NOT NULL "
                "ORDER BY consumed_at LIMIT $1",
                BATCH,
            )
        ]

        plan = await explain_rolled_back(db_pool, DELETE_MAILBOX_SQL, doomed)

        assert "jorb_mailbox_pkey" in plan, plan
        assert_no_seq_scan(plan, "jorb_mailbox")
        assert rows_removed_by_filter(plan) == 0, plan

    async def test_the_sweep_reaps_only_consumed_messages_past_the_window(
        self, db_pool, unique_queue
    ):
        """The plan is only worth asserting if the statement is still right.

        Splitting one statement into two is exactly the change that can quietly
        stop honouring the batch bound, so the batch size is checked as well as
        which rows survive.
        """
        from pyjobby.monitor import sweep_consumed_mailbox

        await self.seed(db_pool, unique_queue)
        unconsumed = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_mailbox WHERE consumed_at IS NULL"
        )

        first = await sweep_consumed_mailbox(db_pool, 0, batch_size=BATCH)
        assert first == BATCH, "the batch bound stopped bounding the delete"

        deleted = first
        for _ in range(50):
            batch = await sweep_consumed_mailbox(db_pool, 0, batch_size=BATCH)
            deleted += batch
            if not batch:
                break

        assert deleted == ROWS - unconsumed
        assert (
            await db_pool.fetchval("SELECT count(*) FROM jorb_mailbox") == unconsumed
        ), "an unread message was reaped"


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


class TestClientQueueStatsPlan:
    """db.QUEUE_STATS_SQL backs client queue_stats()/list_queues() AND
    AdminAPI.queue_stats — the first thing an operator calls, usually on a
    dashboard timer. Its predecessor's single GROUP BY read every row the
    install ever ran; the arm-per-partial-index form (the snapshot's
    construction) must read live work plus one recency window, never the
    history."""

    async def test_the_stats_read_no_history(self, db_pool, unique_queue):
        await seed_terminal_jobs(db_pool, unique_queue)

        plan = await plan_for(
            db_pool, QUEUE_STATS_SQL, datetime.timedelta(hours=1), None
        )

        assert_no_seq_scan(plan)
        # The honest bound is the WINDOW's own population, not a fraction of
        # the table: the seed compresses 60 days into a few hundred pages,
        # so its one-hour slice is a large share of a small table — while on
        # a real install the same window is a vanishing one. What the plan
        # must prove is that cost scales with live work + the window, so:
        # nothing read-and-discarded, and buffers bounded by a small
        # multiple of the rows the window actually holds (plus the live
        # arms' index pages).
        assert rows_removed_by_filter(plan) <= 5, plan
        recent = await db_pool.fetchval(
            """SELECT count(*) FROM jorb
               WHERE state IN ('finished', 'crashed', 'cancelled')
                 AND COALESCE(finished, updated) >= now() - interval '1 hour'"""
        )
        assert buffers_in(plan) <= recent * 2 + 100, (
            f"{buffers_in(plan)} buffers for {recent} in-window rows\n{plan}"
        )


class TestAdminQueueStatsPlan:
    """The one thing AdminAPI.queue_stats asks beyond the shared counts:
    how long the oldest CLAIMABLE job has been waiting. It runs on the same
    `pj-admin queues stats` / /api/queues/stats timer, so it too must be
    answered from the runnable slice of jorb_claim_idx rather than by
    reading the table."""

    async def test_the_oldest_age_reads_only_runnable(self, db_pool, unique_queue):
        await seed_terminal_jobs(db_pool, unique_queue)

        plan = await plan_for(db_pool, ADMIN_OLDEST_QUEUED_AGE_SQL, None)

        assert_no_seq_scan(plan)
        assert rows_removed_by_filter(plan) <= 5, plan
        runnable = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE state = 'queued' AND run_after <= now()"
        )
        assert buffers_in(plan) <= runnable * 2 + 100, (
            f"{buffers_in(plan)} buffers for {runnable} runnable rows\n{plan}"
        )


class TestBackpressureCountPlan:
    """The scheduler's backpressure depth count, once per firing whenever a
    threshold is configured.

    One predicate spanning queued AND claimed/running matches neither
    ``jorb_claim_idx`` (partial on queued) nor ``jorb_inflight_idx``
    (partial on claimed/running) and collapses into a sequential scan of
    jorb — the identical defect the tree has diagnosed and fixed three
    times already. The two-arm split reads each partial index for exactly
    the rows it counts, so nothing is read-and-discarded and nothing is
    scanned. The queued arm's cost is the backlog being measured, which is
    the number the caller asked for.

    EXPLAINs the scheduler's own ``BACKPRESSURE_COUNT_SQL``, never a copy.
    """

    async def test_the_count_is_two_index_arms_not_a_scan(
        self, db_pool, unique_queue
    ):
        await seed_terminal_jobs(db_pool, unique_queue)
        # a slice of in-flight work on ANOTHER queue: the in-flight arm walks
        # the fleet-wide index and must filter these few out, and that
        # bounded discard must not grow into a table-shaped one
        await db_pool.execute(
            """
            UPDATE jorb SET state = 'running', claimed_at = now(), started = now()
            WHERE id IN (SELECT id FROM jorb
                          WHERE queue = $1 AND state = 'queued'
                          ORDER BY id LIMIT 20)
            """,
            unique_queue,
        )
        await settle(db_pool)

        plan = await plan_for(
            db_pool, BACKPRESSURE_COUNT_SQL, unique_queue + "_other"
        )

        assert_no_seq_scan(plan)
        # nothing read-and-discarded beyond the bounded in-flight slice
        assert rows_removed_by_filter(plan) <= 20, plan


class TestScheduleConcurrencyPlan:
    """The scheduler's max_concurrent_jobs check, once per firing, forever.

    It is on the same timer footing as the sweeps above: every enabled
    schedule asks it every time it comes due, and the answer it usually gives
    is a small number. It used to ask by ``admin_data->>'schedule_id'``, which
    no index on jorb could serve, so the cost of firing a schedule grew with
    the JOB TABLE instead of with the schedule's own load -- fine at one
    schedule a minute against a young install, and a full scan per firing
    against an old one.

    Two things had to be true for the fix to be a fix, and this class asserts
    both separately because either one alone looks like success:

    * the check is served by ``jorb_schedule_id_idx`` rather than by a scan;
    * it does not READ the schedule's whole history and discard the terminal
      part of it. That is why the index predicate carries the live states as
      well as ``schedule_id IS NOT NULL``, and an index scan that throws away
      everything it reads costs exactly what a scan costs while passing a
      no-seq-scan check.

    They EXPLAIN the scheduler's own ``CONCURRENCY_COUNT_SQL``, not a copy:
    the state list in the query has to stay syntactically identical to the
    index predicate for PostgreSQL to prove the partial index usable, so a
    gate reading a duplicate of the statement would certify a query nobody
    runs the moment somebody edits one of the two.
    """

    #: Firings of one schedule that have already finished. Deliberately most
    #: of the table: this is what an install accumulates, and it is the part
    #: the check must not read.
    SCHEDULE_ROWS = ROWS // 2

    #: Jobs of that schedule still in flight. Bounded by max_concurrent_jobs
    #: in reality, so a handful is the honest shape.
    LIVE_PER_SCHEDULE = 3

    async def seed(self, pool, queue: str) -> tuple[int, int]:
        """One busy schedule with a long history, one with none.

        Returns (busy schedule id, quiet schedule id). The quiet one exists
        because "nothing to count" is the case that runs on most firings and
        the case an unindexed predicate is most expensive for: there is no
        LIMIT to stop early on, so it examines everything to return zero.
        """
        await pool.execute("TRUNCATE jorb_schedule RESTART IDENTITY CASCADE")
        make = """INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run)
                  VALUES ($1, 'plan.Job', '* * * * *', now()) RETURNING id"""
        busy = await pool.fetchval(make, "plan-busy")
        quiet = await pool.fetchval(make, "plan-quiet")
        await reset_job_tables(pool)
        # Half the table belongs to the busy schedule and is finished; the
        # rest is ordinary client-enqueued work in a spread of states, so the
        # planner is choosing against a realistic mix rather than against a
        # table that is all one thing.
        await pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, schedule_id,
                              created, updated, finished)
            SELECT 'plan.Job', '{}', $1,
                   CASE WHEN i > $3            THEN 'finished'
                        WHEN i % 40 = 0        THEN 'queued'
                        WHEN i % 400 = 1       THEN 'claimed'
                        WHEN i % 400 = 2       THEN 'running'
                        WHEN i % 400 = 3       THEN 'waiting'
                        WHEN i % 40 = 3        THEN 'crashed'
                        ELSE 'finished' END::jorbstate,
                   CASE WHEN i > $3 THEN $4::BIGINT END,
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day'
            FROM generate_series(1, $2) i
            """,
            queue,
            ROWS,
            ROWS - self.SCHEDULE_ROWS,
            busy,
        )
        await pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, schedule_id)
            SELECT 'plan.Job', '{}', $1, 'running', $2
              FROM generate_series(1, $3) i
            """,
            queue,
            busy,
            self.LIVE_PER_SCHEDULE,
        )
        await settle(pool)
        return busy, quiet

    async def test_a_schedule_with_nothing_running_reads_almost_nothing(
        self, db_pool, unique_queue
    ):
        """The common firing: the previous job finished, so the answer is 0.

        The expensive shape, because an empty answer has nothing to stop
        early on.
        """
        _, quiet = await self.seed(db_pool, unique_queue)

        plan = await plan_for(db_pool, CONCURRENCY_COUNT_SQL, quiet)

        assert "jorb_schedule_id_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb")
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_the_count_is_bounded_by_the_schedules_live_jobs(
        self, db_pool, unique_queue
    ):
        """The assertion that actually gates the index's SHAPE.

        This schedule has fired SCHEDULE_ROWS times and has
        LIVE_PER_SCHEDULE jobs left in flight. An index on schedule_id alone
        would be used, would report no sequential scan, and would hand the
        check every one of those historical rows to discard -- a scan wearing
        an index, growing with the age of the install forever. The live
        states are in the index predicate precisely so the node reads the
        in-flight set and nothing else.
        """
        busy, _ = await self.seed(db_pool, unique_queue)

        plan = await plan_for(db_pool, CONCURRENCY_COUNT_SQL, busy)

        assert "jorb_schedule_id_idx" in plan, plan
        assert_no_seq_scan(plan, "jorb")
        assert rows_scanned_by(plan, "jorb_schedule_id_idx") == self.LIVE_PER_SCHEDULE
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_the_index_holds_only_schedule_created_jobs(self, db_pool):
        """Who pays when it is unused: nobody.

        Every job a client enqueues has schedule_id NULL and never matches
        the predicate, so the hot path writes no index entry at all. Asserted
        as "the index does not grow", the same way tests/test_job_tags.py
        gates jorb_tags_idx -- the enqueue-throughput half of that argument is
        a pj-bench measurement (docs/SCALE.md), not something a plan can see.
        """
        await reset_job_tables(db_pool)
        await settle(db_pool)
        before = await db_pool.fetchval(
            "SELECT pg_relation_size('jorb_schedule_id_idx')"
        )

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state)
            SELECT 'plan.Job', '{}', 'unscheduled', 'queued'
              FROM generate_series(1, $1) i
            """,
            ROWS,
        )
        await settle(db_pool)

        assert (
            await db_pool.fetchval("SELECT pg_relation_size('jorb_schedule_id_idx')")
            == before
        ), f"{ROWS} client-enqueued jobs grew a schedule-only index"


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


class TestGroupWakePlan:
    """The group wake runs on the completion path of EVERY grouped job.

    Its gate ("is any member still unfinished?") is NOT EXISTS, which stops
    at the first witness. The `0 = count(*)` form it replaced had to visit
    every member before it could say no, so an N-job fan-out paid O(N²)
    index reads across its lifetime — half a million for the documented
    1,000-item `create_fan_out` example, on the hot path of live workers.
    """

    GROUP_SIZE = 3_000

    async def seed(self, pool, queue: str) -> int:
        """A large group with EVERY member unfinished, plus one waiter.

        All-running is the common case for the statement (N-1 of N
        completions have unfinished peers), and it is the case where the
        early exit matters: any member is a witness, so the probe should
        touch a handful of rows however large the group is.

        Seeded on top of the standard 20k-row table: against the group
        alone the planner rightly seq-scans a tiny table and the test
        proves nothing (see the module comment on ROWS)."""
        await seed_terminal_jobs(pool, queue)
        leader = await pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('scale.Job', '{}', $1, 'running') RETURNING id""",
            queue,
        )
        await pool.execute("UPDATE jorb SET run_group = $1 WHERE id = $1", leader)
        await pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, run_group,
                              claimed_at, started)
            SELECT 'scale.Job', '{}', $1, 'running', $2, now(), now()
            FROM generate_series(1, $3) i
            """,
            queue,
            leader,
            self.GROUP_SIZE - 1,
        )
        await pool.execute(
            """INSERT INTO jorb (job_class, kwargs, queue, state, waitfor_group)
               VALUES ('scale.Job', '{}', $1, 'waiting', $2)""",
            queue,
            leader,
        )
        await settle(pool)
        return int(leader)

    async def test_the_gate_stops_at_the_first_unfinished_member(
        self, db_pool, unique_queue
    ):
        group = await self.seed(db_pool, unique_queue)

        plan = await explain_rolled_back(
            db_pool, STMTS["enqueue-next-if-peer-group-is-finished"], group
        )

        assert_no_seq_scan(plan)
        # The probe found a witness without walking the group: the whole
        # statement — gate probe included — reads and discards a small
        # multiple of nothing, not a multiple of GROUP_SIZE. This is the
        # assertion the count(*) form fails: a count cannot answer without
        # visiting every member.
        removed = rows_removed_by_filter(plan)
        assert removed * 100 < self.GROUP_SIZE, (
            f"the wake gate read and discarded {removed} rows against a "
            f"{self.GROUP_SIZE}-member group: that is a count wearing an "
            f"EXISTS\n{plan}"
        )
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_a_mostly_finished_group_still_probes_in_o1(
        self, db_pool, unique_queue
    ):
        """The witness-LAST case, which the all-running seed above cannot see.

        Near completion, almost every member is finished — and over the plain
        run_group index the probe walks past all of them to find the one
        unfinished witness, an O(members) tail paid by each of the last
        completions. jorb_group_unfinished_idx holds only unfinished members,
        so the probe must ride it and touch a handful of entries however many
        members have already finished."""
        await seed_terminal_jobs(db_pool, unique_queue)
        leader = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, finished)
               VALUES ('scale.Job', '{}', $1, 'finished', now()) RETURNING id""",
            unique_queue,
        )
        await db_pool.execute("UPDATE jorb SET run_group = $1 WHERE id = $1", leader)
        # N-2 more finished members, and ONE still running: the witness is the
        # needle, the finished members are the haystack.
        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, run_group, finished)
            SELECT 'scale.Job', '{}', $1, 'finished', $2, now()
            FROM generate_series(1, $3)
            """,
            unique_queue,
            leader,
            self.GROUP_SIZE - 2,
        )
        await db_pool.execute(
            """INSERT INTO jorb (job_class, kwargs, queue, state, run_group,
                                 claimed_at, started)
               VALUES ('scale.Job', '{}', $1, 'running', $2, now(), now())""",
            unique_queue,
            leader,
        )
        await settle(db_pool)

        plan = await explain_rolled_back(
            db_pool, STMTS["enqueue-next-if-peer-group-is-finished"], leader
        )

        assert_no_seq_scan(plan)
        assert "jorb_group_unfinished_idx" in plan, plan
        removed = rows_removed_by_filter(plan)
        assert removed * 100 < self.GROUP_SIZE, (
            f"the wake gate read and discarded {removed} rows against a "
            f"{self.GROUP_SIZE}-member mostly-finished group: the probe is "
            f"walking finished members instead of riding "
            f"jorb_group_unfinished_idx\n{plan}"
        )
        await assert_reads_far_less_than_a_scan(db_pool, plan)


class TestCompactionPlan:
    """`compact()` is issued by every long-lived job, on every turn.

    It deletes one job's checkpoints out of a `jorb_step` that holds every
    other job's too. The whole point of the primitive is to keep a job that
    lives indefinitely from getting slower, so a compaction that scans
    `jorb_step` would defeat itself: the table it scans is the one that grows
    with the fleet, and the cost would return by a different route than the
    one that was closed.

    Runs `dxe.COMPACT_STEPS_SQL` itself, rolled back, rather than a copy —
    a gate reading a duplicate certifies a statement nobody executes as soon
    as the two drift.
    """

    STEP_EVERY = 3
    STEPS_PER_JOB = 3

    async def seed(self, pool, queue: str) -> tuple[int, int]:
        """Fill jorb_step for many jobs; return (job to compact, its epoch)."""
        await seed_terminal_jobs(pool, queue)
        await pool.execute(
            """
            INSERT INTO jorb_step (job_id, step_seq, name, output, run_epoch)
            SELECT j.id, s, 'machine.transition', '{}', j.run_epoch
              FROM jorb j, generate_series(1, $3) s
             WHERE j.queue = $1 AND j.id % $2 = 0
            """,
            queue,
            self.STEP_EVERY,
            self.STEPS_PER_JOB,
        )
        await settle(pool)
        await pool.execute("ANALYZE jorb_step")
        row = await pool.fetchrow(
            """SELECT j.id, j.run_epoch FROM jorb j
                WHERE EXISTS (SELECT 1 FROM jorb_step s WHERE s.job_id = j.id)
                ORDER BY j.id LIMIT 1"""
        )
        return int(row["id"]), int(row["run_epoch"])

    async def test_compaction_finds_one_jobs_checkpoints_by_index(
        self, db_pool, unique_queue
    ):
        """It must reach this job's rows through `jorb_step_pkey`.

        The epoch passed is the job's real one, or the fence matches nothing,
        the DELETE is never executed, and the plan being asserted on is one
        that did no work — which would pass every check while proving nothing.
        """
        job_id, epoch = await self.seed(db_pool, unique_queue)
        total = await db_pool.fetchval("SELECT count(*) FROM jorb_step")
        assert total > 1000, f"only {total} steps seeded; a scan would be cheap"

        plan = await explain_rolled_back(db_pool, dxe.COMPACT_STEPS_SQL, job_id, epoch)

        assert "Seq Scan on jorb_step" not in plan, plan
        assert "jorb_step_pkey" in plan, plan
        # It really ran: the statement reports the rows it removed, so a
        # fenced-out no-op cannot masquerade as a well-planned delete.
        assert f"actual rows={self.STEPS_PER_JOB}" in plan, plan

    async def test_compaction_of_a_job_with_no_checkpoints_is_just_as_cheap(
        self, db_pool, unique_queue
    ):
        """The common case for a long-lived job: an already-empty log.

        The loop calls `compact()` every turn, so most calls have nothing to
        remove. That call must not be the expensive one — and "nothing to
        remove" is exactly the shape that tempts a planner into a scan,
        because there is no row for a LIMIT to stop early on.
        """
        await self.seed(db_pool, unique_queue)
        row = await db_pool.fetchrow(
            """SELECT j.id, j.run_epoch FROM jorb j
                WHERE j.queue = $1
                  AND NOT EXISTS (SELECT 1 FROM jorb_step s WHERE s.job_id = j.id)
                LIMIT 1""",
            unique_queue,
        )
        assert row is not None

        plan = await explain_rolled_back(
            db_pool, dxe.COMPACT_STEPS_SQL, row["id"], row["run_epoch"]
        )

        assert "Seq Scan on jorb_step" not in plan, plan
        assert "jorb_step_pkey" in plan, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan, "jorb_step")
