"""Saturation metrics: is the PLATFORM keeping up?

The metrics that existed before this file all describe JOBS -- how many
finished, how long they ran, which classes crashed. None of them answer the
question an operator actually has at a million jobs an hour: are completions
keeping pace with arrivals, how deep and how old is the backlog, is the
fleet busy or wedged, is autovacuum losing, and is the NOTIFY queue about to
take the whole system down.

Every rate here is asserted as an EXACT value over a known window, because
"is not None" would pass just as happily on a metric that reports raw counts
and therefore cannot be compared between a 60-second and a 24-hour scrape.

The last class asserts PLANS rather than values. /metrics is scraped on a
timer against a table with hundreds of millions of rows, so a query that is
merely correct is not good enough -- a sequential scan there turns the
monitoring into the outage.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import pytest_asyncio
from click.testing import CliRunner

from pyjobby import db
from pyjobby.admin_api import AdminAPI
from pyjobby.cli import (
    DOCTOR_NOTIFY_FAIL,
    DOCTOR_NOTIFY_WARN,
    cli,
    notify_queue_verdict,
)
from pyjobby.web_admin import PROM_RATE_WINDOW_SECONDS, WebAdminServer
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


async def seed_completed(
    conn,
    queue: str,
    count: int,
    *,
    state: str = "finished",
    finished_ago_seconds: float = 30.0,
    run_count: int = 1,
    job_class: str = "tests.metrics.OkJob",
    error_message: str | None = None,
) -> None:
    """Insert `count` jobs that reached `state` at a known instant.

    `finished` is what the completion window filters on, so it is set
    explicitly rather than left to the row's `updated` default.
    """
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, run_count,
                          created, updated, run_after, claimed_at,
                          started, finished, error_message)
        SELECT $1, '{}', $2, $3::jorbstate, $4,
               now() - make_interval(secs => $5 + 10),
               now() - make_interval(secs => $5),
               now() - make_interval(secs => $5 + 10),
               now() - make_interval(secs => $5 + 5),
               now() - make_interval(secs => $5 + 5),
               now() - make_interval(secs => $5),
               $7
        FROM generate_series(1, $6) i
        """,
        job_class,
        queue,
        state,
        run_count,
        finished_ago_seconds,
        count,
        error_message,
    )


async def seed_queued(
    conn,
    queue: str,
    count: int,
    *,
    run_after_offset_seconds: float,
    created_ago_seconds: float | None = None,
) -> None:
    """Insert `count` queued jobs whose run_after is offset from now.

    A NEGATIVE offset is in the past (claimable); a positive one is in the
    future (scheduled, and therefore not backlog).
    """
    if created_ago_seconds is None:
        created_ago_seconds = max(-run_after_offset_seconds, 0.0)
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, created,
                          updated, run_after)
        SELECT 'tests.metrics.QueuedJob', '{}', $1, 'queued',
               now() - make_interval(secs => $3),
               now() - make_interval(secs => $3),
               now() + make_interval(secs => $4)
        FROM generate_series(1, $2) i
        """,
        queue,
        count,
        created_ago_seconds,
        run_after_offset_seconds,
    )


async def seed_inflight(
    conn, queue: str, count: int, *, state: str, updated_ago_seconds: float
) -> None:
    """Insert `count` jobs a worker is holding, last touched N seconds ago."""
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, created,
                          updated, run_after)
        SELECT 'tests.metrics.InflightJob', '{}', $1, $3::jorbstate,
               now() - make_interval(secs => $4 + 5),
               now() - make_interval(secs => $4),
               now() - make_interval(secs => $4 + 5)
        FROM generate_series(1, $2) i
        """,
        queue,
        count,
        state,
        updated_ago_seconds,
    )


def window(seconds: float):
    """`since` for a window of exactly `seconds` ending now."""
    return db.utcnow() - timedelta(seconds=seconds)


# =============================================================================
# 1. Throughput and arrival rate
# =============================================================================


class TestThroughputAndArrivals:
    """The pair that says whether the fleet is keeping up.

    Rates, not counts: the same 60 completions mean "1/sec" over a minute
    and "0.0007/sec" over a day, and only the rate is comparable between
    two scrapes with different windows.
    """

    async def test_sixty_completions_in_a_sixty_second_window_is_one_per_second(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 60, finished_ago_seconds=30)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["terminal_count"] == 60
        assert metrics["throughput_per_second"] == pytest.approx(1.0, rel=0.05)

    async def test_the_same_completions_over_a_ten_times_wider_window_are_a_tenth_the_rate(
        self, db_connection, unique_queue
    ):
        """The point of publishing a rate: the number scales with the window
        so two scrapes on different intervals can be compared directly."""
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 60, finished_ago_seconds=30)

        narrow = await api.get_metrics(since=window(60), queue=unique_queue)
        wide = await api.get_metrics(since=window(600), queue=unique_queue)

        assert narrow["terminal_count"] == wide["terminal_count"] == 60
        assert wide["throughput_per_second"] == pytest.approx(0.1, rel=0.05)

    async def test_throughput_counts_every_way_a_job_leaves_not_just_success(
        self, db_connection, unique_queue
    ):
        """A crash loop keeps the fleet fully busy while `finished` collapses.

        Counting only successes would report that as throughput collapse and
        send the operator looking for missing workers.
        """
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 10, state="finished")
        await seed_completed(db_connection, unique_queue, 20, state="crashed")
        await seed_completed(db_connection, unique_queue, 30, state="cancelled")

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["finished_count"] == 10
        assert metrics["crashed_count"] == 20
        assert metrics["cancelled_count"] == 30
        assert metrics["terminal_count"] == 60
        assert metrics["throughput_per_second"] == pytest.approx(1.0, rel=0.05)

    async def test_arrivals_above_completions_is_visible_as_two_comparable_rates(
        self, db_connection, unique_queue
    ):
        """Falling behind is a comparison, so both sides must be published."""
        api = AdminAPI(db_connection)
        # 30 completed in the window...
        await seed_completed(db_connection, unique_queue, 30, finished_ago_seconds=30)
        # ...while 90 more arrived and are still queued.
        await seed_queued(db_connection, unique_queue, 90, run_after_offset_seconds=-10)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        # 30 completions + 90 arrivals: the completed jobs were created
        # inside the window too, so arrivals is 120.
        assert metrics["arrival_count"] == 120
        assert metrics["arrival_rate_per_second"] == pytest.approx(2.0, rel=0.05)
        assert metrics["throughput_per_second"] == pytest.approx(0.5, rel=0.05)
        assert metrics["arrival_rate_per_second"] > metrics["throughput_per_second"], (
            "arrivals outrunning completions must be readable off the two rates"
        )

    async def test_a_zero_length_window_reports_zero_rates_rather_than_dividing_by_nothing(
        self, db_connection, unique_queue
    ):
        """`since_hours=0` is a legal query on the web API."""
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 5)

        metrics = await api.get_metrics(since=db.utcnow(), queue=unique_queue)

        assert metrics["throughput_per_second"] == 0.0
        assert metrics["arrival_rate_per_second"] == 0.0
        assert metrics["retry_rate_per_second"] == 0.0
        assert metrics["dlq_growth_per_second"] == 0.0

    async def test_state_counts_are_the_arrival_cohort_not_all_history(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 4, state="finished")
        await seed_queued(db_connection, unique_queue, 3, run_after_offset_seconds=-5)
        # Created long before the window: not an arrival.
        await seed_queued(
            db_connection,
            unique_queue,
            7,
            run_after_offset_seconds=-5,
            created_ago_seconds=86_400,
        )

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["state_counts"] == {"finished": 4, "queued": 3}
        assert metrics["arrival_count"] == 7


# =============================================================================
# 2. Backlog depth and age
# =============================================================================


class TestBacklog:
    """Depth alone cannot tell a healthy deep queue from a stalled shallow
    one; the age of the head of the queue can."""

    async def test_oldest_claimable_age_is_how_long_the_head_has_been_ready(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_queued(db_connection, unique_queue, 1, run_after_offset_seconds=-90)
        await seed_queued(db_connection, unique_queue, 1, run_after_offset_seconds=-5)

        backlog = await api.backlog_stats(queue=unique_queue)

        assert backlog["depth"] == 2
        assert backlog["oldest_age_seconds"] == pytest.approx(90.0, abs=5.0)

    async def test_a_job_scheduled_for_the_future_is_not_backlog(
        self, db_connection, unique_queue
    ):
        """The subtle one. `run_after` gates claimability, so a job queued
        for next week is not work the fleet is behind on -- counting it
        would make every cron-heavy install look permanently saturated, and
        no worker can claim it however many workers you add.
        """
        api = AdminAPI(db_connection)
        await seed_queued(
            db_connection,
            unique_queue,
            1,
            run_after_offset_seconds=-30,
        )
        await seed_queued(
            db_connection,
            unique_queue,
            5,
            run_after_offset_seconds=7 * 86_400,
            created_ago_seconds=600,
        )

        backlog = await api.backlog_stats(queue=unique_queue)

        assert backlog["depth"] == 1, "future-dated jobs are not claimable"
        # ...and their age must not leak into the head-of-queue age either:
        # they were created 10 minutes ago but have waited zero seconds.
        assert backlog["oldest_age_seconds"] == pytest.approx(30.0, abs=5.0)

    async def test_a_queue_with_only_future_work_disappears_from_the_backlog(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_queued(db_connection, unique_queue, 4, run_after_offset_seconds=3600)

        backlog = await api.backlog_stats(queue=unique_queue)

        assert backlog["per_queue"] == {}
        assert backlog["depth"] == 0
        assert backlog["oldest_age_seconds"] == 0.0

    async def test_per_queue_breakdown_isolates_queues(self, db_connection, test_id):
        api = AdminAPI(db_connection)
        busy = f"busy_{test_id}"
        idle = f"idle_{test_id}"
        await seed_queued(db_connection, busy, 7, run_after_offset_seconds=-120)
        await seed_queued(db_connection, idle, 2, run_after_offset_seconds=-3)

        backlog = await api.backlog_stats()

        assert backlog["per_queue"][busy]["depth"] == 7
        assert backlog["per_queue"][idle]["depth"] == 2
        assert backlog["per_queue"][busy]["oldest_age_seconds"] == pytest.approx(
            120.0, abs=5.0
        )
        assert backlog["per_queue"][idle]["oldest_age_seconds"] == pytest.approx(
            3.0, abs=5.0
        )
        # The fleet-wide numbers are the sum and the worst case, not an
        # average that would hide the stalled queue behind the healthy one.
        assert backlog["depth"] == 9
        assert backlog["oldest_age_seconds"] == pytest.approx(120.0, abs=5.0)

    async def test_filtering_by_queue_excludes_every_other_queue(
        self, db_connection, test_id
    ):
        api = AdminAPI(db_connection)
        mine = f"mine_{test_id}"
        theirs = f"theirs_{test_id}"
        await seed_queued(db_connection, mine, 3, run_after_offset_seconds=-10)
        await seed_queued(db_connection, theirs, 99, run_after_offset_seconds=-600)

        backlog = await api.backlog_stats(queue=mine)

        assert set(backlog["per_queue"]) == {mine}
        assert backlog["depth"] == 3
        assert backlog["oldest_age_seconds"] == pytest.approx(10.0, abs=5.0)


# =============================================================================
# 3. In-flight and stuck work
# =============================================================================


class TestInflight:
    """Busy and wedged look identical if you only count in-flight jobs."""

    async def test_stuck_is_the_subset_that_has_not_moved_past_the_threshold(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_inflight(
            db_connection, unique_queue, 6, state="running", updated_ago_seconds=2
        )
        await seed_inflight(
            db_connection, unique_queue, 2, state="running", updated_ago_seconds=900
        )
        await seed_inflight(
            db_connection, unique_queue, 1, state="claimed", updated_ago_seconds=600
        )

        stats = await api.inflight_stats(queue=unique_queue, stuck_after_seconds=300)

        assert stats["inflight"] == 9, "claimed and running are both in flight"
        assert stats["stuck"] == 3
        assert stats["stuck_after_seconds"] == 300.0
        assert stats["oldest_age_seconds"] == pytest.approx(900.0, abs=10.0)

    async def test_the_threshold_is_a_knob_not_a_constant(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_inflight(
            db_connection, unique_queue, 4, state="running", updated_ago_seconds=120
        )

        assert (await api.inflight_stats(queue=unique_queue, stuck_after_seconds=60))[
            "stuck"
        ] == 4
        assert (await api.inflight_stats(queue=unique_queue, stuck_after_seconds=300))[
            "stuck"
        ] == 0

    async def test_terminal_and_queued_jobs_are_not_in_flight(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 5)
        await seed_queued(db_connection, unique_queue, 5, run_after_offset_seconds=-5)
        await seed_inflight(
            db_connection, unique_queue, 1, state="claimed", updated_ago_seconds=1
        )

        stats = await api.inflight_stats(queue=unique_queue)

        assert stats["inflight"] == 1


# =============================================================================
# 4. Retry and failure pressure
# =============================================================================


class TestRetryAndDlqPressure:
    """A rising DLQ rate is the earliest signal of a bad deploy."""

    async def test_retry_rate_counts_attempts_beyond_the_first(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        # 10 jobs that took 3 attempts each = 20 wasted attempts, plus 10
        # that succeeded first time and cost nothing.
        await seed_completed(db_connection, unique_queue, 10, run_count=3)
        await seed_completed(db_connection, unique_queue, 10, run_count=1)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["retry_count"] == 20
        assert metrics["retry_rate_per_second"] == pytest.approx(20 / 60, rel=0.05)

    async def test_a_clean_fleet_reports_no_retry_pressure(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 25, run_count=1)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["retry_count"] == 0
        assert metrics["retry_rate_per_second"] == 0.0

    async def test_dlq_growth_is_the_crash_rate_because_crashed_is_the_dlq(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 12, state="crashed")
        await seed_completed(db_connection, unique_queue, 100, state="finished")

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["crashed_count"] == 12
        assert metrics["dlq_growth_per_second"] == pytest.approx(0.2, rel=0.05)

    async def test_completions_outside_the_window_do_not_count(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        await seed_completed(db_connection, unique_queue, 5, finished_ago_seconds=10)
        await seed_completed(
            db_connection, unique_queue, 500, finished_ago_seconds=7200
        )

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert metrics["terminal_count"] == 5
        assert metrics["throughput_per_second"] == pytest.approx(5 / 60, rel=0.05)


# =============================================================================
# 5. Footprint and autovacuum
# =============================================================================


class TestStorageFootprint:
    """At ~4M dead tuples an hour, whether autovacuum keeps up is a survival
    question -- and nothing reported it before."""

    async def test_every_watched_table_reports_a_sane_footprint(self, db_connection):
        api = AdminAPI(db_connection)

        storage = await api.storage_stats()

        assert set(storage["tables"]) == {"jorb", "jorb_history", "jorb_step"}
        for name, stats in storage["tables"].items():
            assert stats["total_bytes"] > 0, name
            assert stats["table_bytes"] >= 0, name
            assert stats["index_bytes"] >= 0, name
            # total covers the table, its indexes, and TOAST
            assert stats["total_bytes"] >= stats["table_bytes"], name
            assert stats["total_bytes"] >= stats["index_bytes"], name
            assert stats["live_tuples"] >= 0, name
            assert stats["dead_tuples"] >= 0, name
            assert 0.0 <= stats["dead_tuple_ratio"] <= 1.0, name

        assert storage["total_bytes"] == sum(
            t["total_bytes"] for t in storage["tables"].values()
        )
        assert 0.0 <= storage["dead_tuple_ratio"] <= 1.0

    async def test_dead_tuple_ratio_tracks_jorb_specifically(self, db_connection):
        """The hot table is the one whose bloat stops the claim path, so the
        headline ratio is jorb's and not an average across tables."""
        api = AdminAPI(db_connection)

        storage = await api.storage_stats()

        assert (
            storage["dead_tuple_ratio"] == storage["tables"]["jorb"]["dead_tuple_ratio"]
        )

    async def test_footprint_is_carried_into_get_metrics(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert set(metrics["storage"]["tables"]) == {
            "jorb",
            "jorb_history",
            "jorb_step",
        }
        assert 0.0 <= metrics["storage"]["dead_tuple_ratio"] <= 1.0


# =============================================================================
# 6. NOTIFY queue saturation
# =============================================================================


class TestNotifyQueueSaturation:
    """The sharpest cliff in the platform.

    pyjobby fires a notification on enqueue, on every state transition, and
    on completion -- roughly 1,400/second at a million jobs an hour. The
    shared queue drains only as fast as the slowest connected listener, so
    one wedged dashboard fills it, and at 1.0 EVERY transaction that issues
    a NOTIFY fails: no job can be enqueued or completed anywhere. An
    observability client takes down job processing.
    """

    async def test_usage_is_a_fraction_between_zero_and_one(self, db_connection):
        api = AdminAPI(db_connection)

        usage = await api.notify_queue_usage()

        assert isinstance(usage, float)
        assert 0.0 <= usage <= 1.0

    async def test_usage_is_carried_into_get_metrics(self, db_connection, unique_queue):
        api = AdminAPI(db_connection)

        metrics = await api.get_metrics(since=window(60), queue=unique_queue)

        assert 0.0 <= metrics["notify_queue_usage"] <= 1.0

    @pytest.mark.parametrize(
        "usage,expected",
        [
            (0.0, "PASS"),
            (0.2499, "PASS"),
            (DOCTOR_NOTIFY_WARN, "WARN"),
            (0.4, "WARN"),
            (DOCTOR_NOTIFY_FAIL, "WARN"),
            (0.5001, "FAIL"),
            (1.0, "FAIL"),
        ],
    )
    async def test_doctor_thresholds(self, usage, expected):
        """Every branch, including the two no test can provoke for real: the
        queue is server-wide and 8GB by default, so filling a quarter of it
        is not something a test can honestly do."""
        status, _ = notify_queue_verdict(usage)
        assert status == expected

    async def test_the_warning_names_the_cause_and_the_levers(self):
        """A percentage tells an operator nothing at 3am. The message has to
        say what went wrong and what to do about it."""
        for usage in (0.3, 0.9):
            _, message = notify_queue_verdict(usage)
            assert "listening session has stopped draining" in message
            assert "pg_stat_activity" in message
            # No "disable this trigger" lever is offered any more, and that is
            # the point: the per-transition feed was deleted and every
            # remaining channel is demand-gated, so there is no notification
            # volume left that nobody asked for.
            assert "DISABLE TRIGGER" not in message
            assert "every remaining channel is demand-gated" in message
            assert "load-bearing and must stay enabled" in message


# =============================================================================
# 7. CLI surface
# =============================================================================


def dsn_for(db_params: dict) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


async def run_cli(dsn: str, *args: str):
    """Invoke pj-admin against a real database off the test's event loop."""

    def _invoke():
        return CliRunner().invoke(cli, ["--dsn", dsn, *args])

    return await asyncio.to_thread(_invoke)


class TestCliMetrics:
    """`pj-admin metrics` is where an operator looks first."""

    async def test_human_output_prints_every_new_signal(
        self, db_params, db_pool, unique_queue
    ):
        async with db_pool.acquire() as conn:
            await seed_completed(conn, unique_queue, 6, run_count=2)
            await seed_completed(conn, unique_queue, 2, state="crashed")
            await seed_queued(conn, unique_queue, 3, run_after_offset_seconds=-45)
            await seed_inflight(
                conn, unique_queue, 2, state="running", updated_ago_seconds=900
            )

        result = await run_cli(dsn_for(db_params), "metrics", "--since-hours", "1")

        assert result.exit_code == 0, result.output
        out = result.output
        for label in (
            "Throughput:",
            "Arrivals:",
            "Balance:",
            "Retry Pressure:",
            "DLQ Growth:",
            "Cancelled:",
            "Backlog:",
            "In Flight:",
            "NOTIFY Queue:",
            "Dead Tuples:",
            "Backlog by Queue:",
            "Storage:",
        ):
            assert label in out, f"{label!r} missing from:\n{out}"

        assert unique_queue in out
        assert "jorb_history" in out
        assert "1 stuck" not in out and "2 stuck" in out

    async def test_json_output_carries_the_whole_payload(
        self, db_params, db_pool, unique_queue
    ):
        async with db_pool.acquire() as conn:
            await seed_completed(conn, unique_queue, 4)
            await seed_queued(conn, unique_queue, 1, run_after_offset_seconds=-20)

        result = await run_cli(
            dsn_for(db_params),
            "metrics",
            "--queue",
            unique_queue,
            "--since-hours",
            "1",
            "--json",
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        for key in (
            "window_seconds",
            "throughput_per_second",
            "arrival_rate_per_second",
            "retry_rate_per_second",
            "dlq_growth_per_second",
            "terminal_count",
            "backlog",
            "inflight",
            "storage",
            "notify_queue_usage",
        ):
            assert key in payload, key

        assert payload["backlog"]["per_queue"][unique_queue]["depth"] == 1
        assert payload["terminal_count"] == 4
        # 3600-second window, 4 completions
        assert payload["throughput_per_second"] == pytest.approx(4 / 3600, rel=0.05)

    async def test_doctor_reports_the_notify_queue(self, db_params):
        result = await run_cli(dsn_for(db_params), "doctor")

        assert "notify-queue" in result.output
        line = next(ln for ln in result.output.splitlines() if "notify-queue:" in ln)
        # An idle test database has an empty queue, so this is the PASS path
        # end to end against a real server.
        assert line.startswith("PASS"), line
        assert "full" in line


# =============================================================================
# 8. Prometheus surface
# =============================================================================


class TestPrometheusSaturationMetrics:
    """Each signal is exposed as a properly named and typed series."""

    async def test_every_new_metric_is_named_typed_and_present(
        self, web_admin_client, db_pool, unique_queue
    ):
        async with db_pool.acquire() as conn:
            await seed_completed(conn, unique_queue, 5, run_count=2)
            await seed_completed(conn, unique_queue, 1, state="crashed")
            await seed_queued(conn, unique_queue, 4, run_after_offset_seconds=-60)
            await seed_inflight(
                conn, unique_queue, 3, state="claimed", updated_ago_seconds=800
            )

        resp = await web_admin_client.get("/metrics")
        assert resp.status == 200
        text = await resp.text()

        for metric in (
            "pyjobby_throughput_jobs_per_second",
            "pyjobby_arrival_jobs_per_second",
            "pyjobby_retry_attempts_per_second",
            "pyjobby_dlq_jobs_per_second",
            "pyjobby_jobs_inflight",
            "pyjobby_jobs_stuck",
            "pyjobby_inflight_oldest_age_seconds",
            "pyjobby_notify_queue_usage_ratio",
            "pyjobby_backlog_depth",
            "pyjobby_table_total_bytes",
            "pyjobby_table_bytes",
            "pyjobby_table_index_bytes",
            "pyjobby_table_live_tuples",
            "pyjobby_table_dead_tuples",
            "pyjobby_table_dead_tuple_ratio",
        ):
            assert f"# HELP {metric} " in text, metric
            assert f"# TYPE {metric} gauge" in text, metric

        lines = {
            ln.split(" ", 1)[0]: ln.split(" ", 1)[1]
            for ln in text.splitlines()
            if ln and not ln.startswith("#")
        }
        assert lines["pyjobby_jobs_inflight"] == "3"
        assert lines["pyjobby_jobs_stuck"] == "3"
        assert lines[f'pyjobby_backlog_depth{{queue="{unique_queue}"}}'] == "4"
        assert float(lines["pyjobby_inflight_oldest_age_seconds"]) == pytest.approx(
            800.0, abs=15.0
        )
        assert 0.0 <= float(lines["pyjobby_notify_queue_usage_ratio"]) <= 1.0

    async def test_rates_use_the_documented_scrape_window(
        self, web_admin_client, db_pool, unique_queue
    ):
        """The scrape window is 5 minutes, so 60 completions inside it read
        as 0.2/sec. A raw count here would be meaningless to a Prometheus
        rate() that assumes a counter."""
        async with db_pool.acquire() as conn:
            await seed_completed(
                conn, unique_queue, 60, finished_ago_seconds=30, run_count=2
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()
        lines = {
            ln.split(" ", 1)[0]: ln.split(" ", 1)[1]
            for ln in text.splitlines()
            if ln and not ln.startswith("#")
        }

        expected = 60 / PROM_RATE_WINDOW_SECONDS
        assert float(lines["pyjobby_throughput_jobs_per_second"]) == pytest.approx(
            expected, rel=0.05
        )
        assert float(lines["pyjobby_arrival_jobs_per_second"]) == pytest.approx(
            expected, rel=0.05
        )
        # one wasted attempt per job
        assert float(lines["pyjobby_retry_attempts_per_second"]) == pytest.approx(
            expected, rel=0.05
        )

    async def test_footprint_series_are_labelled_by_table(self, web_admin_client):
        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        for table in ("jorb", "jorb_history", "jorb_step"):
            assert f'pyjobby_table_total_bytes{{table="{table}"}} ' in text
            ratio_line = next(
                ln
                for ln in text.splitlines()
                if ln.startswith(f'pyjobby_table_dead_tuple_ratio{{table="{table}"}}')
            )
            assert 0.0 <= float(ratio_line.rsplit(" ", 1)[1]) <= 1.0


class TestDashboardMetricsFragment:
    """The HTML dashboard shows the same signals as the CLI."""

    async def test_html_fragment_shows_the_saturation_signals(
        self, web_admin_client, db_pool, unique_queue
    ):
        async with db_pool.acquire() as conn:
            await seed_queued(conn, unique_queue, 2, run_after_offset_seconds=-30)
            await seed_inflight(
                conn, unique_queue, 1, state="running", updated_ago_seconds=1000
            )

        resp = await web_admin_client.get("/api/metrics?format=html&since_hours=1")
        text = await resp.text()

        for label in (
            "Throughput",
            "Arrivals",
            "Retry Pressure",
            "DLQ Growth",
            "Backlog Depth",
            "Oldest Ready",
            "In Flight",
            "Stuck",
            "NOTIFY Queue",
            "Dead Tuples",
            "Storage",
        ):
            assert f"<span>{label}</span>" in text, label

    async def test_json_endpoint_exposes_the_new_keys(
        self, web_admin_client, unique_queue
    ):
        resp = await web_admin_client.get(f"/api/metrics?queue={unique_queue}")
        payload = await resp.json()

        assert payload["queue"] == unique_queue
        for key in (
            "throughput_per_second",
            "arrival_rate_per_second",
            "retry_rate_per_second",
            "dlq_growth_per_second",
            "backlog",
            "inflight",
            "storage",
            "notify_queue_usage",
            "window_seconds",
        ):
            assert key in payload, key


# =============================================================================
# 9. Query plans
# =============================================================================

# Enough rows that the planner has a real choice: a sequential scan of a tiny
# table genuinely IS cheaper than an index, so below this the test proves
# nothing.
PLAN_ROWS = 20_000


async def seed_for_plans(pool, rows: int = PLAN_ROWS) -> None:
    """A steady state at scale: mostly terminal history, a live backlog, and
    an in-flight set bounded by the worker fleet.

    The mix matters. Seeding a quarter of the table as in-flight would make a
    sequential scan the correct plan and the assertion meaningless -- what
    the indexes have to survive is a large table in which the interesting
    rows are a small slice.

    Timestamps are spread over 60 days so a reporting window covers a real
    slice rather than everything.
    """
    await reset_job_tables(pool)
    await pool.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, state, run_count,
                          created, updated, run_after, claimed_at,
                          started, finished)
        -- (i / 40) rather than (i % 5) so the queued rows (every 40th)
        -- land across all five queues instead of piling into one.
        SELECT 'plan.Job', '{}', 'plan_q' || ((i / 40) % 5),
               CASE WHEN i % 40 = 0  THEN 'queued'
                    WHEN i % 400 = 1 THEN 'claimed'
                    WHEN i % 400 = 2 THEN 'running'
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


async def plan_for(pool, sql: str, *args) -> str:
    rows = await pool.fetch(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) {sql}", *args)
    return "\n".join(r["QUERY PLAN"] for r in rows)


class TestSaturationQueryPlans:
    """/metrics is scraped on a timer against hundreds of millions of rows.

    These assert the PLAN, not a duration: a timing would flake on a loaded
    CI box and pass on a fast one with the index dropped. A plan is a fact.

    Note which indexes these rely on, because the schema deliberately does
    NOT index `updated` -- every state transition rewrites it, so an index
    there taxes the write path forever to speed up one read per scrape.
    Completions are found through `jorb_retention_idx`, arrivals through
    `jorb_created_idx`, backlog through `jorb_claim_idx`, and in-flight
    through `jorb_inflight_idx`.
    """

    async def test_backlog_is_an_index_only_scan_of_the_claim_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT queue, COUNT(*) AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(run_after)))::float8
            FROM jorb
            WHERE state = 'queued' AND run_after <= now()
            GROUP BY queue ORDER BY queue
            """,
        )

        assert "Seq Scan on jorb" not in plan, plan
        assert "jorb_claim_idx" in plan, plan
        # Both columns live in the index, so the heap is never touched --
        # measured at 20k rows as 7 buffers against 572 for the sequential
        # scan that MIN(created) would force instead.
        assert "Index Only Scan" in plan, plan

    async def test_inflight_is_an_index_only_scan_of_the_inflight_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (
                       WHERE updated <= now() - make_interval(secs => $1)
                   ),
                   EXTRACT(EPOCH FROM (now() - MIN(updated)))::float8
            FROM jorb
            WHERE state IN ('claimed', 'running')
            """,
            300.0,
        )

        assert "Seq Scan on jorb" not in plan, plan
        assert "jorb_inflight_idx" in plan, plan
        assert "Index Only Scan" in plan, plan

    async def test_completions_window_uses_the_retention_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT COUNT(*) AS terminal_count,
                   COUNT(*) FILTER (WHERE state = 'finished'),
                   COUNT(*) FILTER (WHERE state = 'crashed'),
                   COUNT(*) FILTER (WHERE state = 'cancelled'),
                   COALESCE(SUM(GREATEST(run_count - 1, 0)), 0),
                   AVG(EXTRACT(EPOCH FROM (finished - started)))
                       FILTER (WHERE state = 'finished'
                               AND started IS NOT NULL),
                   AVG(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                       FILTER (WHERE claimed_at IS NOT NULL),
                   MAX(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                       FILTER (WHERE claimed_at IS NOT NULL)
            FROM jorb
            WHERE state IN ('finished', 'crashed', 'cancelled')
              AND COALESCE(finished, updated) >= now() - $1::interval
            """,
            timedelta(hours=1),
        )

        assert "Seq Scan on jorb" not in plan, plan
        assert "jorb_retention_idx" in plan, plan

    async def test_arrivals_window_uses_the_created_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT state, COUNT(*) FROM jorb
            WHERE created >= now() - $1::interval
            GROUP BY state
            """,
            timedelta(hours=1),
        )

        assert "Seq Scan on jorb" not in plan, plan
        assert "jorb_created_idx" in plan, plan

    async def test_top_errors_rides_the_same_completion_index(self, db_pool):
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT job_class, COUNT(*) AS error_count,
                   MAX(error_message) AS latest_error
            FROM jorb
            WHERE state = 'crashed'
              AND COALESCE(finished, updated) >= now() - $1::interval
            GROUP BY job_class ORDER BY error_count DESC LIMIT 10
            """,
            timedelta(hours=1),
        )

        assert "Seq Scan on jorb" not in plan, plan
        assert "jorb_retention_idx" in plan, plan

    async def test_the_footprint_query_never_reads_a_job_row(self, db_pool):
        """Sizes and dead tuples come from the catalog, so this one costs the
        same whether the table holds twenty thousand rows or a billion."""
        await seed_for_plans(db_pool)

        plan = await plan_for(
            db_pool,
            """
            SELECT relname::text, pg_total_relation_size(relid),
                   pg_table_size(relid), pg_indexes_size(relid),
                   n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
            FROM pg_stat_user_tables
            WHERE relname = ANY($1::text[])
            """,
            ["jorb", "jorb_history", "jorb_step"],
        )

        # No node of any kind reads the job table: the whole plan is over
        # the statistics view.
        assert "Scan on jorb" not in plan, plan

    async def test_get_metrics_still_answers_correctly_at_twenty_thousand_rows(
        self, db_pool
    ):
        """The plans above are asserted against hand-written SQL, so this
        runs the REAL code path over the same seeded table: a query that is
        fast but wrong is not an improvement."""
        await seed_for_plans(db_pool)

        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            metrics = await api.get_metrics(since=window(3600))

        assert metrics["terminal_count"] > 0
        assert metrics["throughput_per_second"] == pytest.approx(
            metrics["terminal_count"] / metrics["window_seconds"], rel=0.01
        )
        # 1 in 40 rows is queued and due, spread over 5 queues.
        assert metrics["backlog"]["depth"] == PLAN_ROWS // 40
        assert len(metrics["backlog"]["per_queue"]) == 5
        # 2 in 400 rows are held by a worker.
        assert metrics["inflight"]["inflight"] == PLAN_ROWS // 200
        assert metrics["storage"]["tables"]["jorb"]["total_bytes"] > 0
        assert 0.0 <= metrics["notify_queue_usage"] <= 1.0
