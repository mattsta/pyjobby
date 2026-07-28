"""The aggregate dashboard feed that replaced the ``job_state_change`` firehose.

The deleted channel pushed one NOTIFY per job state transition, ungated, to
every listener. It was the last ungated channel in the schema, and because the
commit lock NOTIFY takes is per COMMIT rather than per notification, that one
channel cost as much as all seven together — measured at 2.6-2.9x on the
completion path in ``tests/test_notify_gating.py``. It could not be demand-gated like the
others because its consumer (a browser) has no polling fallback, so a gate
would have DROPPED dashboard events rather than delaying them.

So the push became a poll, and this file is the specification of that poll.
Four properties, and the feed is only affordable if all four hold:

1. **It says something true.** The snapshot a subscriber receives matches the
   database, asserted field by field against a known seed.
2. **It costs nothing when nobody is watching.** Zero subscribers, zero
   queries — the same demand principle the schema's notification gates use.
3. **It costs the same when everybody is watching.** N subscribers share ONE
   query per interval. This is what makes it O(1) in dashboards where the
   firehose was O(transitions).
4. **It cannot silently become expensive.** Every arm of the statement is
   index-backed at scale, and the channel it replaced is gone from the
   catalog and from the source tree.

Plus the per-job primitive: a client that wants a specific job says so
(``watch_job``), which rides the demand-gated ``jorb_done`` channel instead of
tailing every transition in the system.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient

from pyjobby import db, websocket_server
from pyjobby.websocket_server import (
    JOB_CHANNEL_PREFIX,
    QUEUE_CHANNEL_PREFIX,
    SNAPSHOT_CHANNEL,
    SNAPSHOT_SQL,
    WebSocketServer,
)
from tests.test_metrics_scrape_cost import plan_for, seed_for_plans

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Fast enough that a test does not spend its life sleeping, slow enough that
#: a tick is still a scheduled event rather than a busy loop.
TICK = 0.05

#: The window the snapshot asks for, as the server binds it.
WINDOW = timedelta(seconds=60)

#: "Seq Scan on jorb", and not on jorb_queue/jorb_worker/jorb_history.
_SEQ_SCAN_ON_JORB = re.compile(r"Seq Scan on jorb\b(?!_)")


# =============================================================================
# Harness
# =============================================================================


@dataclass
class SnapshotHarness:
    """A live WebSocketServer with its snapshot loop under test control."""

    server: WebSocketServer
    client: TestClient
    sockets: list[aiohttp.ClientWebSocketResponse] = field(default_factory=list)
    feed: asyncio.Task | None = None

    async def connect(self) -> aiohttp.ClientWebSocketResponse:
        ws = await self.client.ws_connect("/ws")
        self.sockets.append(ws)
        hello = json.loads((await ws.receive()).data)
        assert hello["event"] == "connected"
        return ws

    async def subscribe(
        self, ws: aiohttp.ClientWebSocketResponse, *channels: str
    ) -> None:
        await ws.send_json({"action": "subscribe", "channels": list(channels)})
        reply = json.loads((await ws.receive()).data)
        assert reply["event"] == "subscribed", reply

    def start_feed(self) -> None:
        """Run the real loop — the same coroutine ``start()`` launches."""
        assert self.feed is None
        self.feed = asyncio.create_task(self.server.snapshot_broadcast())

    async def stop_feed(self) -> None:
        if self.feed is not None:
            self.feed.cancel()
            await asyncio.gather(self.feed, return_exceptions=True)
            self.feed = None

    async def drain(
        self, ws: aiohttp.ClientWebSocketResponse, event: str, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Read frames until one of type `event` arrives."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"no {event!r} frame within {timeout}s"
            msg = await asyncio.wait_for(ws.receive(), remaining)
            assert msg.type is aiohttp.WSMsgType.TEXT, f"unexpected frame: {msg}"
            frame = json.loads(msg.data)
            if frame["event"] == event:
                return frame


@pytest_asyncio.fixture
async def snapshot_server(db_params, aiohttp_client):
    """Factory for in-process servers whose snapshot loop the test drives."""
    harnesses: list[SnapshotHarness] = []

    async def _make(*, notify: bool = False, **kwargs: Any) -> SnapshotHarness:
        kwargs.setdefault("snapshot_interval", TICK)
        server = WebSocketServer(db_params, **kwargs)
        await server.init_db_pool()
        if notify:
            await server.init_notify_connection()
        app = web.Application()
        app.router.add_get("/ws", server.handle_websocket)
        harness = SnapshotHarness(server=server, client=await aiohttp_client(app))
        harnesses.append(harness)
        return harness

    yield _make

    for harness in harnesses:
        await harness.stop_feed()
        for ws in harness.sockets:
            if not ws.closed:
                await ws.close()
        if harness.server.notify_conn is not None:
            await harness.server.notify_conn.close()
        if harness.server.db_pool is not None:
            await harness.server.db_pool.close()


async def seed_known_queue(pool: asyncpg.Pool, queue: str) -> None:
    """A queue in a known, fully enumerated state.

    Every counter the snapshot reports gets a distinct value, so a field that
    is silently reading the wrong column cannot pass. In particular the queue
    holds one job scheduled for the future: it is `queued` but it is NOT
    backlog, which is the distinction `backlog_stats` exists to make (a job
    deliberately scheduled for next week is not the fleet falling behind).
    """
    await pool.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after,
                          started, finished)
        VALUES
            -- claimable backlog, the older one setting the head-of-queue age
            ('SnapJob', '{}', $1, 100, 'queued',  now() - interval '30 seconds',
             NULL, NULL),
            ('SnapJob', '{}', $1, 100, 'queued',  now(), NULL, NULL),
            ('SnapJob', '{}', $1, 100, 'queued',  now(), NULL, NULL),
            -- queued but not yet due: counted as queued, excluded from backlog
            ('SnapJob', '{}', $1, 100, 'queued',  now() + interval '1 hour',
             NULL, NULL),
            ('SnapJob', '{}', $1, 100, 'claimed', now(), NULL, NULL),
            ('SnapJob', '{}', $1, 100, 'running', now(), now(), NULL),
            ('SnapJob', '{}', $1, 100, 'running', now(), now(), NULL),
            ('SnapJob', '{}', $1, 100, 'waiting', now(), NULL, NULL),
            ('SnapJob', '{}', $1, 100, 'finished', now(), now(), now()),
            ('SnapJob', '{}', $1, 100, 'finished', now(), now(), now()),
            ('SnapJob', '{}', $1, 100, 'finished', now(), now(), now()),
            ('SnapJob', '{}', $1, 100, 'crashed', now(), now(), now()),
            ('SnapJob', '{}', $1, 100, 'cancelled', now(), NULL, now())
        """,
        queue,
    )


#: What ``seed_known_queue`` must produce, exactly. Volatile fields (the two
#: ages, and the fleet-wide worker count) are asserted separately.
EXPECTED_SEEDED_QUEUE = {
    # `queued` is CLAIMABLE NOW on every surface (db.QUEUE_STATS_SQL), so it
    # equals `backlog` here and excludes the one `scheduled` job. Summing the
    # two into `queued` made this feed disagree with pj-admin queues stats
    # under the same word.
    "queued": 3,
    "claimed": 1,
    "running": 2,
    "waiting": 1,
    "backlog": 3,
    "scheduled": 1,
    "finished": 3,
    "crashed": 1,
    "cancelled": 1,
    "paused": False,
}


def strip_volatile(stats: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a queue's stats into the exact part and the timing-dependent part."""
    stats = dict(stats)
    volatile = {
        key: stats.pop(key)
        for key in (
            "oldest_backlog_age_seconds",
            "oldest_inflight_age_seconds",
            "workers_live",
        )
    }
    return stats, volatile


# =============================================================================
# 1. The snapshot says something true
# =============================================================================


class TestSnapshotContent:
    async def test_snapshot_matches_a_known_seed(
        self, snapshot_server, db_pool, unique_queue
    ):
        """Every counter, against a queue whose contents are enumerated."""
        h = await snapshot_server()
        await seed_known_queue(db_pool, unique_queue)

        snapshot = await h.server.collect_snapshot()
        stats, volatile = strip_volatile(snapshot["queues"][unique_queue])

        assert stats == EXPECTED_SEEDED_QUEUE
        # The head of the backlog has been ready for 30s. The job scheduled
        # an hour out must not be what sets this, or a queue with future work
        # would report a negative (or absurd) age.
        assert 30.0 <= volatile["oldest_backlog_age_seconds"] < 120.0
        assert volatile["oldest_inflight_age_seconds"] >= 0.0
        assert volatile["workers_live"] == snapshot["workers_live"]

    async def test_snapshot_reaches_a_subscribed_client(
        self, snapshot_server, db_pool, unique_queue
    ):
        """The whole path: poll loop -> broadcast -> a real websocket frame."""
        h = await snapshot_server()
        ws = await h.connect()
        await h.subscribe(ws, SNAPSHOT_CHANNEL)
        await seed_known_queue(db_pool, unique_queue)

        h.start_feed()
        frame = await h.drain(ws, "dashboard")
        await h.stop_feed()

        data = frame["data"]
        assert data["interval_seconds"] == TICK
        assert data["window_seconds"] == 60.0
        stats, _ = strip_volatile(data["queues"][unique_queue])
        assert stats == EXPECTED_SEEDED_QUEUE

    async def test_per_queue_subscribers_get_their_slice_of_the_same_poll(
        self, snapshot_server, db_pool, unique_queue
    ):
        """`queues:<name>` still works, and costs no extra query.

        It is fed from the same snapshot as the `jobs` channel, which is why
        a per-queue dashboard is not a per-queue database cost.
        """
        h = await snapshot_server()
        ws = await h.connect()
        await h.subscribe(ws, f"{QUEUE_CHANNEL_PREFIX}{unique_queue}")
        await seed_known_queue(db_pool, unique_queue)

        h.start_feed()
        frame = await h.drain(ws, "queue_stats")
        await h.stop_feed()

        assert frame["data"]["queue"] == unique_queue
        stats, _ = strip_volatile(frame["data"])
        del stats["queue"]
        assert stats == EXPECTED_SEEDED_QUEUE
        assert h.server.stats["snapshot_queries"] >= 1

    async def test_a_paused_queue_with_nothing_pending_still_appears(
        self, snapshot_server, db_pool, unique_queue
    ):
        """Control rows are part of the picture: an operator who paused a
        queue must see it, not watch it vanish as it drains."""
        h = await snapshot_server()
        await db_pool.execute(
            "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", unique_queue
        )

        stats = (await h.server.collect_snapshot())["queues"][unique_queue]

        assert stats["paused"] is True
        assert stats["queued"] == 0
        assert stats["backlog"] == 0

    async def test_totals_are_the_sum_and_the_worst_of_the_queues(
        self, snapshot_server, db_pool, unique_queue
    ):
        """Counts add up; ages take the maximum. Averaging an age would hide
        the one queue that is actually stuck."""
        h = await snapshot_server()
        await seed_known_queue(db_pool, unique_queue)

        snapshot = await h.server.collect_snapshot()
        queues, totals = snapshot["queues"], snapshot["totals"]

        assert "paused" not in totals
        for key in ("queued", "claimed", "running", "waiting", "backlog", "scheduled"):
            assert totals[key] == sum(q[key] for q in queues.values()), key
        assert totals["oldest_backlog_age_seconds"] == max(
            q["oldest_backlog_age_seconds"] for q in queues.values()
        )


# =============================================================================
# 2 & 3. What the feed costs: nothing idle, one query busy
# =============================================================================


class TestSnapshotCost:
    async def test_no_subscribers_means_no_query_at_all(self, snapshot_server):
        """The demand gate on the loop.

        A connected client that has subscribed to nothing is not demand
        either: this is the regime a dashboard server spends most of its life
        in, and the firehose's replacement must not reintroduce a per-second
        cost that nobody asked for.
        """
        h = await snapshot_server()
        await h.connect()

        h.start_feed()
        await asyncio.sleep(TICK * 10)
        await h.stop_feed()

        assert h.server.stats["snapshot_queries"] == 0

    async def test_subscribing_opens_the_gate_and_unsubscribing_shuts_it(
        self, snapshot_server
    ):
        """Demand is withdrawable: the cost stops when the last client leaves."""
        h = await snapshot_server()
        ws = await h.connect()

        h.start_feed()
        await asyncio.sleep(TICK * 4)
        assert h.server.stats["snapshot_queries"] == 0, "polled with no demand"

        await h.subscribe(ws, SNAPSHOT_CHANNEL)
        await h.drain(ws, "dashboard")
        assert h.server.stats["snapshot_queries"] >= 1

        await ws.send_json({"action": "unsubscribe", "channels": [SNAPSHOT_CHANNEL]})
        await h.drain(ws, "unsubscribed")
        await asyncio.sleep(TICK * 2)
        settled = h.server.stats["snapshot_queries"]

        await asyncio.sleep(TICK * 8)
        await h.stop_feed()
        assert h.server.stats["snapshot_queries"] == settled

    async def test_a_disconnect_shuts_the_gate_too(self, snapshot_server):
        """The bookkeeping path that matters most, because nobody asked for
        it: a browser tab closing must stop the polling it caused."""
        h = await snapshot_server()
        ws = await h.connect()
        await h.subscribe(ws, SNAPSHOT_CHANNEL)

        h.start_feed()
        await h.drain(ws, "dashboard")
        await ws.close()

        # the server needs a moment to notice the close and run its cleanup
        for _ in range(200):
            if not h.server.clients:
                break
            await asyncio.sleep(0.01)
        assert h.server.subscriptions == {}, "a closed socket left demand behind"

        settled = h.server.stats["snapshot_queries"]
        await asyncio.sleep(TICK * 8)
        await h.stop_feed()
        assert h.server.stats["snapshot_queries"] == settled

    async def test_one_query_serves_every_subscriber(
        self, snapshot_server, db_pool, unique_queue
    ):
        """THE property. Five dashboards, one query — not five.

        Asserted on the loop body directly so the count is exact rather than
        a function of how long the test happened to run: one pass of the
        thing the loop calls, five identical frames delivered, one query.
        """
        h = await snapshot_server()
        await seed_known_queue(db_pool, unique_queue)

        sockets = []
        for _ in range(5):
            ws = await h.connect()
            await h.subscribe(ws, SNAPSHOT_CHANNEL)
            sockets.append(ws)

        await h.server.broadcast_snapshot(await h.server.collect_snapshot())

        assert h.server.stats["snapshot_queries"] == 1
        frames = [await h.drain(ws, "dashboard", timeout=5.0) for ws in sockets]
        assert len(frames) == 5
        assert all(f["data"] == frames[0]["data"] for f in frames), (
            "subscribers must share one snapshot, not each get their own"
        )

    async def test_the_loop_issues_one_query_per_interval_not_per_client(
        self, snapshot_server
    ):
        """The same property through the real timing loop.

        The exact assertion is per-client: every subscriber receives exactly
        as many snapshots as there were queries. If the query were per-client
        the counter would run at N times the delivery rate.
        """
        h = await snapshot_server(max_actions_per_second=100)
        sockets = []
        for _ in range(4):
            ws = await h.connect()
            await h.subscribe(ws, SNAPSHOT_CHANNEL)
            sockets.append(ws)

        h.start_feed()
        for _ in range(500):
            if h.server.stats["snapshot_queries"] >= 3:
                break
            await asyncio.sleep(0.01)
        await h.stop_feed()

        queries = h.server.stats["snapshot_queries"]
        assert queries >= 3, "the loop never ran"
        for ws in sockets:
            received = 0
            while received < queries:
                await h.drain(ws, "dashboard", timeout=5.0)
                received += 1
            assert received == queries


# =============================================================================
# 4. It cannot silently become expensive
# =============================================================================


class TestSnapshotPlan:
    async def test_the_snapshot_never_scans_a_job_table(self, db_pool):
        """One query per second is only affordable if it is index-backed.

        Planned at 20k rows with a production-shaped mix (the same seed the
        /metrics scrape-cost tests use), because a sequential scan of a tiny
        table genuinely is the cheaper plan and proves nothing. The failure
        this guards against is the one that does not look like a failure: the
        feed stays correct while getting slower with every job ever run.
        """
        await seed_for_plans(db_pool)

        plan = await plan_for(db_pool, SNAPSHOT_SQL, WINDOW)

        # `jorb_queue` and `jorb_worker` are control tables — one row per
        # queue, one per worker process — so scanning them is correct and is
        # what the /metrics scrape does too. `jorb` is the one that grows.
        assert not _SEQ_SCAN_ON_JORB.search(plan), plan
        assert "jorb_history" not in plan, plan

    async def test_the_snapshot_reads_less_than_the_table(self, db_pool):
        """The same claim in pages, calibrated to this machine's heap."""
        await seed_for_plans(db_pool)
        heap_pages = await db_pool.fetchval(
            "SELECT pg_relation_size('jorb') / current_setting('block_size')::int"
        )
        assert heap_pages > 100, "seed too small for this to prove anything"

        plan = await plan_for(db_pool, SNAPSHOT_SQL, WINDOW)
        buffers = [int(m) for m in re.findall(r"shared hit=(\d+)", plan)]
        assert buffers, f"no buffer accounting in plan:\n{plan}"
        assert max(buffers) < heap_pages, (
            f"the snapshot read {max(buffers)} buffers against a "
            f"{heap_pages}-page heap — that is reading the table:\n{plan}"
        )


class TestTheChannelIsGone:
    async def test_no_trigger_emits_the_channel(self, db_pool):
        """The catalog, not the schema text: a trigger created by anything
        else — a migration, a fixture, a benchmark that forgot to clean up —
        would cost exactly what the deleted one cost."""
        triggers = [
            r["tgname"]
            for r in await db_pool.fetch(
                """SELECT tgname FROM pg_trigger
                    WHERE NOT tgisinternal AND tgrelid = 'jorb'::regclass"""
            )
        ]
        assert "job_state_change_notify" not in triggers
        assert not [t for t in triggers if "state_change" in t], triggers

    async def test_the_notify_function_rejects_the_channel(self, db_pool):
        """jorb_notify() enumerates every channel it knows; this one is not
        among them, so re-adding a trigger without re-adding the channel
        fails loudly instead of silently notifying nobody."""
        with pytest.raises(asyncpg.PostgresError, match="unknown channel"):
            await db_pool.execute(
                """
                CREATE CONSTRAINT TRIGGER tmp_state_change_probe
                    AFTER UPDATE OF state ON jorb
                    FOR EACH ROW
                    EXECUTE FUNCTION jorb_notify('job_state_change', 'ungated');
                """
            )
            try:
                await db_pool.execute(
                    "INSERT INTO jorb (job_class, queue, state) "
                    "VALUES ('probe.Job', 'probe_q', 'queued')"
                )
                await db_pool.execute(
                    "UPDATE jorb SET state = 'claimed' WHERE queue = 'probe_q'"
                )
            finally:
                await db_pool.execute(
                    "DROP TRIGGER IF EXISTS tmp_state_change_probe ON jorb"
                )
                await db_pool.execute("DELETE FROM jorb WHERE queue = 'probe_q'")

    async def test_nothing_in_the_repo_listens_on_it(self):
        """No LISTEN, anywhere, in any language.

        A listener left behind on a dead channel is not harmless: it is a
        consumer that will never fire, and the next person to read it will
        conclude the feed still exists.
        """
        found = subprocess.run(
            [
                "git",
                "grep",
                "-nE",
                r"""(add_listener\(\s*['"]job_state_change|LISTEN\s+job_state_change)""",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert found.stdout == "", (
            f"something still LISTENs on the deleted channel:\n{found.stdout}"
        )


async def emitted_notify_channels(pool: asyncpg.Pool) -> set[str]:
    """Every channel the schema can actually NOTIFY on, from the catalog.

    Read out of ``pg_trigger`` rather than the schema text so a channel that
    is declared but never wired to a trigger does not count, and so a trigger
    added by a migration does. Same query ``tests/test_bench.py`` uses to keep
    ``pj-bench notify`` honest.
    """
    return {
        r["channel"]
        for r in await pool.fetch(
            r"""
            SELECT DISTINCT
                   (regexp_match(pg_get_triggerdef(t.oid),
                                 $re$jorb_notify\('([a-z_]+)'$re$))[1]
                       AS channel
              FROM pg_trigger t
              JOIN pg_proc p ON p.oid = t.tgfoid
             WHERE p.proname = 'jorb_notify' AND NOT t.tgisinternal
            """
        )
    }


class TestNoListenerOnAChannelNothingEmits:
    """The general form of the defect above, not one dead channel.

    PostgreSQL accepts ``LISTEN`` on any identifier, so a listener for a
    channel no trigger emits does not raise, does not warn, and does not
    fire. It reads like a working feed to the next person, and worse, the
    protocol documentation grows an event that clients can subscribe to and
    then wait for forever -- which is precisely what ``queue_alert`` was:
    registered here, routed to ``alerts:queues:{queue}``, documented in
    docs/WEBSOCKET_DASHBOARD.md, and emitted by nothing in the platform.

    So: every channel anything in this repo LISTENs on must be one the
    schema NOTIFYs on. Adding a listener is now a claim the catalog checks.
    """

    async def test_the_dashboard_server_listens_only_on_live_channels(
        self, db_params, db_pool, monkeypatch
    ):
        """Behavioral: what ``init_notify_connection`` really registers.

        Recorded off the connection, not read off ``LISTEN_CHANNELS``, so a
        stray ``add_listener`` next to the loop is caught too.
        """
        registered: list[str] = []

        class RecordingConnection:
            def is_closed(self) -> bool:
                return False

            async def add_listener(self, channel: str, callback: Any) -> None:
                registered.append(channel)

        async def fake_connect(**_kwargs: Any) -> RecordingConnection:
            return RecordingConnection()

        monkeypatch.setattr(websocket_server.db, "connect", fake_connect)

        server = WebSocketServer(db_params)
        await server.init_notify_connection()

        assert registered, "the server registered no listeners at all"
        assert set(registered) == set(websocket_server.LISTEN_CHANNELS)

        dead = set(registered) - await emitted_notify_channels(db_pool)
        assert not dead, (
            f"pj-ws LISTENs on {sorted(dead)}, which no trigger emits: "
            f"a client subscribed to those events waits forever"
        )

    async def test_every_declared_channel_is_one_the_schema_emits(self, db_pool):
        """The same rule for the worker, the client library, anything else.

        Every LISTENer in the platform names its channel through a
        ``db.CHANNEL_*`` constant, so checking the constants covers all of
        them at once -- including the channels whose only call site builds
        its list at runtime (pj-bench, pj-ws), which a grep for literals
        never reached.
        """
        declared = {
            name: value
            for name, value in vars(db).items()
            if name.startswith("CHANNEL_")
        }
        assert declared, "pyjobby.db declares no NOTIFY channel constants"

        dead = set(declared.values()) - await emitted_notify_channels(db_pool)
        assert not dead, (
            f"these channels are declared and LISTENed on but emitted by "
            f"nothing: {sorted(dead)}"
        )

    async def test_no_listener_spells_its_channel_as_a_bare_string(self):
        """A literal is how a channel name drifts from the schema silently:
        PostgreSQL accepts LISTEN on any identifier, so a typo is a feed that
        never fires. The constants are the only spelling."""
        found = subprocess.run(
            ["git", "grep", "-nE", r"""add_listener\(\s*["'][a-z_]+["']"""],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        literals = re.findall(r"""add_listener\(\s*["']([a-z_]+)["']""", found.stdout)
        assert not literals, (
            f"add_listener() called with a literal channel name; use the "
            f"pyjobby.db.CHANNEL_* constant instead:\n{found.stdout}"
        )


# =============================================================================
# 5. The per-job primitive: watch ONE job, not every transition
# =============================================================================


class TestListenWatchdog:
    async def test_a_dead_listen_connection_is_reestablished(self, snapshot_server):
        """The LISTEN connection was opened once at startup and never again:
        after any drop, every watch_job subscription waited forever for a
        jorb_done that could not arrive, while /health reported a live port.
        The snapshot loop now re-opens it on its own beat."""
        harness = await snapshot_server(notify=True)
        server = harness.server
        assert server.notify_conn is not None
        assert not server.notify_conn.is_closed()

        await server.notify_conn.close()
        assert server.notify_conn.is_closed()

        harness.start_feed()
        deadline = asyncio.get_running_loop().time() + 5.0
        while server.notify_conn is None or server.notify_conn.is_closed():
            assert asyncio.get_running_loop().time() < deadline, (
                "snapshot loop never re-established the LISTEN connection"
            )
            await asyncio.sleep(TICK)

        assert not server.notify_conn.is_closed()


class TestWatchJob:
    """What a client uses when it genuinely needs per-job updates.

    The old answer was "subscribe to the firehose and filter", which made one
    client's interest in one job cost a NOTIFY on every transition of every
    job in the system. The new answer is bounded by what the client asked
    for: watching job N sets `jorb.awaited` on row N, which is exactly the
    gate `jorb_done_notify` tests, so the platform emits one notification for
    one job because one client asked for it.
    """

    async def test_watching_registers_demand_and_reports_current_state(
        self, snapshot_server, db_pool, unique_queue
    ):
        h = await snapshot_server()
        ws = await h.connect()
        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1, 'running')"
            " RETURNING id",
            unique_queue,
        )

        await ws.send_json({"action": "watch_job", "job_id": job_id})
        reply = await h.drain(ws, "watching")

        assert reply["data"] == {
            "job_id": job_id,
            "channel": f"{JOB_CHANNEL_PREFIX}{job_id}",
            "state": "running",
        }
        assert await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id)

    async def test_a_watched_job_pushes_its_completion(
        self, snapshot_server, db_pool, unique_queue
    ):
        """End to end over real LISTEN/NOTIFY, on the gated channel."""
        h = await snapshot_server(notify=True)
        ws = await h.connect()
        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1, 'running')"
            " RETURNING id",
            unique_queue,
        )

        await ws.send_json({"action": "watch_job", "job_id": job_id})
        await h.drain(ws, "watching")

        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        event = await h.drain(ws, "jorb_done")
        assert event["data"] == {"id": job_id, "state": "finished"}

    async def test_a_watch_survives_the_awaited_latch_being_cleared(
        self, snapshot_server, db_pool, unique_queue
    ):
        """compact() clears jorb.awaited to shed notification cost on a
        long-lived job; a push-only watch has no fallback poll, so the
        snapshot loop must re-arm the latch (and catch a completion whose
        NOTIFY was missed while it was clear). Here the latch is cleared and
        the job finished in the SAME window the NOTIFY cannot fire, and the
        re-arm on the next beat must still deliver the completion."""
        h = await snapshot_server(notify=True)
        ws = await h.connect()
        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1, 'running')"
            " RETURNING id",
            unique_queue,
        )

        await ws.send_json({"action": "watch_job", "job_id": job_id})
        await h.drain(ws, "watching")

        # the machine's compact() clears the latch, THEN the job finishes —
        # so the terminal UPDATE reads awaited=FALSE and emits no NOTIFY
        await db_pool.execute("UPDATE jorb SET awaited = FALSE WHERE id = $1", job_id)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        h.start_feed()  # the snapshot beat re-arms + checks watched jobs
        event = await h.drain(ws, "jorb_done", timeout=5)
        assert event["data"] == {"id": job_id, "state": "finished"}

    async def test_an_unwatched_job_pushes_nothing(
        self, snapshot_server, db_pool, unique_queue
    ):
        """Two jobs, one watched: the other one's completion is not delivered,
        because nothing registered demand for it and the schema never emitted
        it in the first place."""
        h = await snapshot_server(notify=True)
        ws = await h.connect()
        watched, ignored = [
            await db_pool.fetchval(
                "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1,"
                " 'running') RETURNING id",
                unique_queue,
            )
            for _ in range(2)
        ]

        await ws.send_json({"action": "watch_job", "job_id": watched})
        await h.drain(ws, "watching")

        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = ANY($1::bigint[])",
            [ignored, watched],
        )

        event = await h.drain(ws, "jorb_done")
        assert event["data"]["id"] == watched
        assert h.server.stats["events_received"] == 1, (
            "the unwatched job's completion was notified anyway"
        )

    async def test_watching_a_job_that_already_finished_is_not_a_hang(
        self, snapshot_server, db_pool, unique_queue
    ):
        """The race the reply closes.

        Demand is registered on the same row whose state change would notify,
        so PostgreSQL orders the two writers: either the completion sees the
        demand and notifies, or it committed first and the state this reply
        carries is already terminal. A watch can never wait for an event that
        has already happened.
        """
        h = await snapshot_server(notify=True)
        ws = await h.connect()
        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1, 'finished')"
            " RETURNING id",
            unique_queue,
        )

        await ws.send_json({"action": "watch_job", "job_id": job_id})
        reply = await h.drain(ws, "watching")

        assert reply["data"]["state"] == "finished"

    async def test_unwatching_stops_delivery(
        self, snapshot_server, db_pool, unique_queue
    ):
        h = await snapshot_server(notify=True)
        ws = await h.connect()
        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, queue, state) VALUES ('W', $1, 'running')"
            " RETURNING id",
            unique_queue,
        )

        await ws.send_json({"action": "watch_job", "job_id": job_id})
        await h.drain(ws, "watching")
        await ws.send_json({"action": "unwatch_job", "job_id": job_id})
        await h.drain(ws, "unwatched")

        assert h.server.subscriptions == {}, "an unwatch left the channel key behind"

    async def test_watching_a_job_that_does_not_exist_is_an_error(
        self, snapshot_server
    ):
        h = await snapshot_server()
        ws = await h.connect()

        await ws.send_json({"action": "watch_job", "job_id": 2**62})
        reply = await h.drain(ws, "error")

        assert "not found" in reply["data"]["message"]
        assert h.server.subscriptions == {}

    async def test_a_malformed_job_id_never_reaches_the_database(self, snapshot_server):
        """watch_job inherits the shared job_id bounds, like every other
        job-addressed action."""
        h = await snapshot_server()
        ws = await h.connect()

        for bad in (None, "7", 1.5, True, -1, 2**63):
            await ws.send_json({"action": "watch_job", "job_id": bad})
            reply = await h.drain(ws, "error")
            assert "job_id" in reply["data"]["message"], bad
