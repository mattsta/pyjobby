"""What one Prometheus scrape costs, and what its numbers are allowed to mean.

/metrics is the only endpoint in the system that runs on a timer nobody
tunes. At a 15-second scrape interval it executes ~5,700 times a day against
the same tables the workers are trying to use, so a query here that is merely
*correct* is a latent outage: it stays correct as the table grows and simply
gets slower, until the monitoring is the thing that takes the platform down.

Three separate properties are asserted here and they fail in different ways:

1. COST (``TestScrapeQueryPlans``) -- every statement the scrape issues must
   be answered from an index or the catalog. These assert the PLAN, not a
   duration: a timing flakes on a loaded CI box and passes on a fast one with
   the index dropped. A plan is a fact.

2. COUNTER SEMANTICS (``TestCountersSurviveRetention``) -- a Prometheus
   counter is a promise that the number only ever goes up, and every
   ``rate()`` in every dashboard is built on that promise. A counter computed
   by RECOUNTING rows drops to zero the moment retention deletes them, and
   ``rate()`` reads the drop as a counter reset: the traffic in the window
   vanishes from the graph. So the counters here must come from a source
   retention cannot touch.

3. EXPOSITION CONTRACT (``TestExpositionContract``) -- ``# TYPE`` must match
   how the series is actually computed, and a name that survives must still
   mean what it meant, because alerts are written against names.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import pytest_asyncio

from pyjobby.web_admin import (
    PROM_RATE_WINDOW_SECONDS,
    PROM_SQL_DURATION_QUANTILES,
    PROM_SQL_ENQUEUED_TOTAL,
    PROM_SQL_LIVE_STATES,
    PROM_SQL_STARTED_RECENT,
    PROM_SQL_TERMINAL_RECENT,
    WebAdminServer,
)
from tests.utils.plans import reset_job_tables

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def web_admin_client(db_params, aiohttp_client):
    """A test client for the web admin server on the session's database."""
    server = WebAdminServer(db_params)
    return await aiohttp_client(server.app)


# =============================================================================
# Helpers
# =============================================================================

# Enough rows that the planner has a real choice: a sequential scan of a tiny
# table genuinely IS cheaper than an index, so below this the test proves
# nothing.
PLAN_ROWS = 20_000


async def seed_for_plans(pool, rows: int = PLAN_ROWS) -> None:
    """A steady state at scale: a large terminal history, a small live set.

    The mix is the whole point. The live states are bounded by how much work
    is in flight no matter how big the table gets, and the terminal states
    are not -- so seeding a quarter of the table as queued would make a
    sequential scan the *correct* plan and every assertion below meaningless.

    Timestamps are spread over 60 days so a scrape window covers a real slice
    rather than the entire table, and jorb_history is populated by the
    ``jorb_history_record`` trigger on the way in: ~1 history row per job
    here, against ~4 in production, which only makes these tests kinder to
    the plans they are trying to catch.
    """
    await reset_job_tables(pool)
    await pool.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, run_count,
                          created, updated, run_after, claimed_at,
                          started, finished)
        -- (i / 40) rather than (i % 5) so the live rows (every 40th) land
        -- across all five queues instead of piling into one.
        SELECT 'plan.Job', '{}', 'cost_q' || ((i / 40) % 5),
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
    )
    # ANALYZE for statistics, VACUUM for the visibility map that index-only
    # scans need. Autovacuum does both continuously in production; a test
    # that skips them measures a table no running system ever has.
    await pool.execute("VACUUM (ANALYZE) jorb")
    await pool.execute("VACUUM (ANALYZE) jorb_history")


async def plan_for(pool, sql: str, *args) -> str:
    rows = await pool.fetch(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) {sql}", *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


_BUFFERS_RE = re.compile(r"shared hit=(\d+)(?: read=(\d+))?")


def buffers_in(plan: str) -> int:
    """Buffers the whole statement touched, from the root node's line.

    EXPLAIN reports buffers cumulatively up the tree, so the first `Buffers:`
    line in the output is the total for execution. (The planner's own buffers
    are reported separately, under `Planning:`, further down.)
    """
    match = _BUFFERS_RE.search(plan)
    assert match, f"no buffer accounting in plan:\n{plan}"
    return int(match.group(1)) + int(match.group(2) or 0)


def parse_samples(body: str) -> dict[str, float]:
    """Map ``name{labels}`` -> value for every sample line in an exposition."""
    return {
        line.split(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if line and not line.startswith("#")
    }


# =============================================================================
# 1. Cost: what one scrape reads
# =============================================================================


# The window every scrape query is asked for, as the handler passes it.
WINDOW = timedelta(seconds=PROM_RATE_WINDOW_SECONDS)

# Every statement /metrics runs against the job tables, paired with the args
# the handler binds. These are the SAME string objects the handler executes --
# imported, not copied -- so a plan certified here is a plan the scrape gets.
SCRAPE_STATEMENTS = (
    ("live states", PROM_SQL_LIVE_STATES, ()),
    ("terminal recent", PROM_SQL_TERMINAL_RECENT, (WINDOW,)),
    ("started recent", PROM_SQL_STARTED_RECENT, (WINDOW,)),
    ("duration quantiles", PROM_SQL_DURATION_QUANTILES, (WINDOW,)),
    ("enqueued total", PROM_SQL_ENQUEUED_TOTAL, ()),
)


class TestScrapeQueryPlans:
    """Every statement in the scrape, planned at 20k rows.

    The two that used to be here and are gone are the reason this file
    exists: a ``COUNT(*) ... GROUP BY queue, state`` over all of jorb with no
    window at all, and a ``jorb_history JOIN jorb`` with no window either --
    the largest table in the system, joined to the second largest, on a timer.

    Measured on the 20k-row seed below (jorb heap = 871 pages, one history
    row per job), buffers touched per statement::

        state census (removed)      871   whole table, grows with the table
        history join (removed)      770   whole history, grows with history
        live states                 154   bounded by live work
        terminal recent             170   bounded by completions in 300s
        started recent               21   bounded by starts in 300s
        duration quantiles          178   bounded by completions in 300s
        enqueued total               12   constant

    Only the first two lines grow when the installation gets older. That is
    the whole point, and it is what the assertions below pin down -- as
    access methods and page counts relative to the table, never as durations.
    """

    @pytest.mark.parametrize(
        "label,sql,args",
        SCRAPE_STATEMENTS,
        ids=[s[0] for s in SCRAPE_STATEMENTS],
    )
    async def test_no_statement_in_the_scrape_scans_a_job_table(
        self, db_pool, label, sql, args
    ):
        """The blanket rule, applied to the real SQL one statement at a time.

        A sequential scan of jorb is the failure this whole file is about, and
        ANY node reading jorb_history is worse: history holds ~4 rows per job
        and has no index on time, so there is no cheap way to read it at all.
        """
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, sql, *args)

        assert "Seq Scan on jorb" not in plan, f"{label}:\n{plan}"
        assert "jorb_history" not in plan, f"{label}:\n{plan}"

    @pytest.mark.parametrize(
        "label,sql,args",
        SCRAPE_STATEMENTS,
        ids=[s[0] for s in SCRAPE_STATEMENTS],
    )
    async def test_no_statement_reads_as_much_as_the_table_itself(
        self, db_pool, label, sql, args
    ):
        """The same claim in the unit that actually costs money: pages.

        The ceiling is the size of the jorb heap *measured on this machine*
        rather than a constant, so the test calibrates itself to whatever the
        seed produced and cannot flake on page-size or fillfactor differences.
        Reading as many pages as the table has IS reading the table.
        """
        await seed_for_plans(db_pool)
        heap_pages = await db_pool.fetchval(
            "SELECT pg_relation_size('jorb') / current_setting('block_size')::int"
        )
        assert heap_pages > 100, "seed too small for this to prove anything"

        plan = await plan_for(db_pool, sql, *args)

        assert buffers_in(plan) < heap_pages, (
            f"{label} touched {buffers_in(plan)} buffers against a "
            f"{heap_pages}-page table:\n{plan}"
        )

    async def test_live_state_gauge_uses_one_partial_index_per_state(self, db_pool):
        """pyjobby_jobs_by_state is bounded by LIVE work, not by history.

        Each arm of the union is a different partial index, which is why the
        query is written as a union rather than ``state IN (...)``: one
        predicate spanning four states matches no partial index and collapses
        straight back into the sequential scan this replaced.
        """
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, PROM_SQL_LIVE_STATES)

        # queue is jorb_claim_idx's leading column, so the backlog arm never
        # touches the heap at all.
        assert "Index Only Scan using jorb_claim_idx" in plan, plan
        assert "jorb_inflight_idx" in plan, plan
        assert "jorb_waitfor" in plan, plan

    async def test_terminal_gauge_rides_the_retention_index(self, db_pool):
        """The terminal states are the unbounded ones, so they are only ever
        reported over a window -- and that window is written as exactly the
        expression `jorb_retention_idx` is built on."""
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, PROM_SQL_TERMINAL_RECENT, WINDOW)

        assert "jorb_retention_idx" in plan, plan

    async def test_started_gauge_rides_the_started_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, PROM_SQL_STARTED_RECENT, WINDOW)

        assert "jorb_started_idx" in plan, plan

    async def test_duration_quantiles_ride_the_retention_index(self, db_pool):
        """Quantiles used to filter on bare `finished`, which no index covers
        -- `jorb_retention_idx` is on COALESCE(finished, updated), and for a
        finished job the two are the same instant."""
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, PROM_SQL_DURATION_QUANTILES, WINDOW)

        assert "jorb_retention_idx" in plan, plan

    async def test_the_enqueued_counter_reads_no_table_at_all(self, db_pool):
        """The cumulative counter comes from the sequence, so it costs the
        same at twenty thousand rows and at a billion -- and, crucially, it
        cannot be moved by a retention delete."""
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, PROM_SQL_ENQUEUED_TOTAL)

        assert "Scan on jorb" not in plan, plan
        assert plan.startswith("Result"), plan

    async def test_the_old_state_census_read_the_whole_job_table(self, db_pool):
        """The removed pyjobby_jobs_by_state query, planned.

        No window, no predicate, nothing to index: grouping every row by
        (queue, state) can only be answered by reading every row. It cost
        more buffers than the table has pages, on every scrape, forever.
        """
        await seed_for_plans(db_pool)
        heap_pages = await db_pool.fetchval(
            "SELECT pg_relation_size('jorb') / current_setting('block_size')::int"
        )

        plan = await plan_for(
            db_pool,
            """
            SELECT queue, state::text AS state, COUNT(*) AS n
            FROM jorb GROUP BY queue, state
            """,
        )

        assert "Seq Scan on jorb" in plan, plan
        assert buffers_in(plan) >= heap_pages, plan

    async def test_the_old_history_join_really_did_read_every_history_row(
        self, db_pool
    ):
        """The removed query, planned, so the regression it represents is
        documented rather than remembered.

        This is what ran on every scrape. There is no window on either side
        and no index on jorb_history.at, so the only way to answer it is to
        read jorb_history whole -- every row examined, none skipped. At the
        reference workload (a million jobs an hour, 30-day retention) that is
        ~2.9 billion history rows, every 15 seconds.
        """
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT j.queue, h.event, COUNT(*) AS n
            FROM jorb_history h
            JOIN jorb j ON j.id = h.job_id
            WHERE h.event IN ('running', 'finished', 'crashed')
            GROUP BY j.queue, h.event
            """,
        )

        assert "Seq Scan on jorb_history" in plan, plan
        # The seed drives PLAN_ROWS jobs through the history trigger, and the
        # scan had to look at every one of their rows to answer.
        assert f"Rows Removed by Filter: {PLAN_ROWS}" in plan, plan


# =============================================================================
# 2. Counter semantics across retention
# =============================================================================


class TestCountersSurviveRetention:
    """A counter that is recomputed by counting rows is not a counter."""

    async def test_enqueued_counter_does_not_go_down_when_history_is_deleted(
        self, web_admin_client, db_pool, unique_queue
    ):
        """Record it, delete every terminal job, scrape again.

        The assertions are exact on both sides: five enqueues must move the
        counter by exactly five, and a retention sweep must move it by
        exactly zero.
        """
        async with db_pool.acquire() as conn:
            before = parse_samples(
                await (await web_admin_client.get("/metrics")).text()
            )["pyjobby_jobs_enqueued_total"]

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state,
                                  started, finished)
                SELECT 'retention.Job', '{}', $1, 'finished',
                       now() - interval '10 seconds', now()
                FROM generate_series(1, 5)
                """,
                unique_queue,
            )

            after_enqueue = parse_samples(
                await (await web_admin_client.get("/metrics")).text()
            )["pyjobby_jobs_enqueued_total"]
            assert after_enqueue == before + 5

            # What retention does: delete aged-out terminal jobs. The history
            # rows go with them through ON DELETE CASCADE, which is exactly
            # what made the old recount-based counters collapse.
            history_before = await conn.fetchval("SELECT COUNT(*) FROM jorb_history")
            deleted = await conn.fetchval(
                """
                WITH gone AS (
                    DELETE FROM jorb
                     WHERE state IN ('finished', 'crashed', 'cancelled')
                     RETURNING 1
                )
                SELECT COUNT(*) FROM gone
                """
            )
            assert deleted >= 5
            history_after = await conn.fetchval("SELECT COUNT(*) FROM jorb_history")
            # The premise of the test: rows really did disappear underneath.
            assert history_after < history_before

            after_delete = parse_samples(
                await (await web_admin_client.get("/metrics")).text()
            )["pyjobby_jobs_enqueued_total"]

        assert after_delete == after_enqueue, (
            "pyjobby_jobs_enqueued_total is declared a counter; retention "
            "must not be able to move it"
        )

    async def test_a_recount_of_history_would_have_gone_down(
        self, db_pool, unique_queue
    ):
        """Why the old ``*_total`` series could not stay counters.

        This is the deleted implementation, run directly: count the 'running'
        events in jorb_history, delete the jobs, count again. It drops. Every
        ``rate()`` over that series reads the drop as a counter reset and
        silently loses the traffic in the window.
        """
        recount = """
            SELECT COUNT(*) FROM jorb_history h
            JOIN jorb j ON j.id = h.job_id
            WHERE h.event = 'running' AND j.queue = $1
        """
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state)
                SELECT 'recount.Job', '{}', $1, 'queued'
                FROM generate_series(1, 4)
                """,
                unique_queue,
            )
            await conn.execute(
                "UPDATE jorb SET state = 'running' WHERE queue = $1", unique_queue
            )
            await conn.execute(
                """UPDATE jorb SET state = 'finished', finished = now()
                    WHERE queue = $1""",
                unique_queue,
            )

            assert await conn.fetchval(recount, unique_queue) == 4

            await conn.execute(
                "DELETE FROM jorb WHERE queue = $1 AND state = 'finished'",
                unique_queue,
            )

            assert await conn.fetchval(recount, unique_queue) == 0


# =============================================================================
# 3. Exposition contract
# =============================================================================

# Every series the endpoint is allowed to emit, with the type it must be
# declared as. Adding a line here is a deliberate act: a name in this table is
# a name someone can write an alert against.
EXPECTED_TYPES = {
    # cumulative, monotonic, O(1) source
    "pyjobby_jobs_enqueued_total": "counter",
    # levels, bounded by live work
    "pyjobby_jobs_by_state": "gauge",
    "pyjobby_backlog_depth": "gauge",
    "pyjobby_queue_oldest_queued_seconds": "gauge",
    "pyjobby_queue_paused": "gauge",
    "pyjobby_workers_live": "gauge",
    # the live workers that are nonetheless claiming nothing, and the
    # approach to that state -- bounded by the fleet, not the job table
    "pyjobby_workers_not_claiming": "gauge",
    "pyjobby_worker_job_threads_abandoned_max": "gauge",
    "pyjobby_jobs_inflight": "gauge",
    "pyjobby_jobs_stuck": "gauge",
    "pyjobby_inflight_oldest_age_seconds": "gauge",
    "pyjobby_notify_queue_usage_ratio": "gauge",
    # window aggregates -- gauges, and named so they cannot be mistaken for
    # cumulative totals
    "pyjobby_jobs_started_recent": "gauge",
    "pyjobby_jobs_terminal_recent": "gauge",
    "pyjobby_job_duration_seconds": "gauge",
    "pyjobby_throughput_jobs_per_second": "gauge",
    "pyjobby_arrival_jobs_per_second": "gauge",
    "pyjobby_retry_attempts_per_second": "gauge",
    "pyjobby_dlq_jobs_per_second": "gauge",
    # footprint, from the catalog
    "pyjobby_table_total_bytes": "gauge",
    "pyjobby_table_bytes": "gauge",
    "pyjobby_table_index_bytes": "gauge",
    "pyjobby_table_live_tuples": "gauge",
    "pyjobby_table_dead_tuples": "gauge",
    "pyjobby_table_dead_tuple_ratio": "gauge",
}

# The three that were removed. They joined all of jorb_history to all of jorb
# on every scrape, and no window could be added without turning a counter
# into a gauge under a name ending in _total. Re-adding any of them under the
# old name is a regression, not a feature.
RETIRED_NAMES = (
    "pyjobby_jobs_started_total",
    "pyjobby_jobs_finished_total",
    "pyjobby_jobs_crashed_total",
)


async def scrape_body(client, db_pool, queue: str) -> str:
    """A scrape of a database with at least one job in every family."""
    async with db_pool.acquire() as conn:
        job_id = await conn.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state)
            VALUES ('contract.Job', '{}', $1, 'queued') RETURNING id
            """,
            queue,
        )
        await conn.execute(
            """UPDATE jorb SET state = 'running', started = now() - interval '5 s'
                WHERE id = $1""",
            job_id,
        )
        await conn.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
            job_id,
        )
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, updated)
            VALUES ('contract.Job', '{}', $1, 'crashed', now()),
                   ('contract.Job', '{}', $1, 'queued', now()),
                   ('contract.Job', '{}', $1, 'waiting', now())
            """,
            queue,
        )
        await conn.execute(
            "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", queue
        )
    resp = await client.get("/metrics")
    assert resp.status == 200
    return await resp.text()


class TestExpositionContract:
    async def test_every_metric_declares_help_and_type_exactly_once(
        self, web_admin_client, db_pool, unique_queue
    ):
        body = await scrape_body(web_admin_client, db_pool, unique_queue)

        emitted = {name.split("{", 1)[0] for name in parse_samples(body)}
        assert emitted == set(EXPECTED_TYPES), (
            f"metric names drifted: unexpected="
            f"{sorted(emitted - set(EXPECTED_TYPES))} "
            f"missing={sorted(set(EXPECTED_TYPES) - emitted)}"
        )
        for name, kind in EXPECTED_TYPES.items():
            assert body.count(f"# HELP {name} ") == 1, name
            assert body.count(f"# TYPE {name} ") == 1, name
            assert f"# TYPE {name} {kind}\n" in body, name

    async def test_only_total_suffixed_series_are_counters(
        self, web_admin_client, db_pool, unique_queue
    ):
        """The naming convention IS the contract: `_total` means cumulative
        and monotonic, and nothing else may claim it."""
        body = await scrape_body(web_admin_client, db_pool, unique_queue)

        declared = dict(
            line.removeprefix("# TYPE ").split(" ", 1)
            for line in body.splitlines()
            if line.startswith("# TYPE ")
        )
        for name, kind in declared.items():
            assert (kind == "counter") == name.endswith("_total"), (
                f"{name} is declared {kind}: only _total series may be counters"
            )

    async def test_the_unbounded_counters_are_gone(
        self, web_admin_client, db_pool, unique_queue
    ):
        body = await scrape_body(web_admin_client, db_pool, unique_queue)

        for name in RETIRED_NAMES:
            assert name not in body, (
                f"{name} is back; it cannot be computed without scanning jorb_history"
            )

    async def test_jobs_by_state_reports_live_states_only(
        self, web_admin_client, db_pool, unique_queue
    ):
        """The meaning change, pinned.

        Terminal states left this gauge because they are unbounded: their
        count is 'every job that ever ran and has not aged out yet', which is
        a number no scrape can produce cheaply and no operator can act on.
        The live states stayed because they are bounded by work in progress.
        """
        body = await scrape_body(web_admin_client, db_pool, unique_queue)
        samples = parse_samples(body)
        q = unique_queue

        assert samples[f'pyjobby_jobs_by_state{{queue="{q}",state="queued"}}'] == 1
        assert samples[f'pyjobby_jobs_by_state{{queue="{q}",state="waiting"}}'] == 1
        for terminal in ("finished", "crashed", "cancelled"):
            assert (
                f'pyjobby_jobs_by_state{{queue="{q}",state="{terminal}"}}'
                not in samples
            ), terminal

    async def test_terminal_states_are_reported_over_the_scrape_window(
        self, web_admin_client, db_pool, unique_queue
    ):
        body = await scrape_body(web_admin_client, db_pool, unique_queue)
        samples = parse_samples(body)
        q = unique_queue

        assert (
            samples[f'pyjobby_jobs_terminal_recent{{queue="{q}",state="finished"}}']
            == 1
        )
        assert (
            samples[f'pyjobby_jobs_terminal_recent{{queue="{q}",state="crashed"}}'] == 1
        )
        assert samples[f'pyjobby_jobs_started_recent{{queue="{q}"}}'] == 1

    async def test_the_window_is_the_documented_one(
        self, web_admin_client, db_pool, unique_queue
    ):
        """A job that finished before the window opened is outside it, and the
        window in the HELP text is the window the query uses."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state,
                                  started, finished, updated)
                VALUES ('stale.Job', '{}', $1, 'finished',
                        now() - make_interval(secs => $2 + 20),
                        now() - make_interval(secs => $2 + 10),
                        now() - make_interval(secs => $2 + 10))
                """,
                unique_queue,
                float(PROM_RATE_WINDOW_SECONDS),
            )

        resp = await web_admin_client.get("/metrics")
        body = await resp.text()
        samples = parse_samples(body)

        assert (
            f'pyjobby_jobs_terminal_recent{{queue="{unique_queue}",'
            f'state="finished"}}' not in samples
        )
        assert f"last {PROM_RATE_WINDOW_SECONDS}s" in body
