"""Hostile-input, rate-limit and authorization-boundary tests for the
websocket dashboard server.

``tests/test_websocket_server.py`` drives most handlers through a ``FakeWS``
stand-in. Everything here goes over a **real** websocket connection to a real
``WebSocketServer`` with a real PostgreSQL pool, because the properties under
test (frames the server must survive, the sliding-window rate limiter, per
connection bookkeeping, LISTEN/NOTIFY fan-out) only exist end to end.

Contents:

1. Rate limiter under adversarial traffic shapes.
2. Malformed / hostile frames: the server must answer and stay alive.
3. Subscription limits, idempotency, and post-disconnect bookkeeping.
4. ``TestUnauthenticatedMutationBoundary`` — the executable specification of
   what an anonymous client can do to the database today.
5. Notification task bounding.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import asyncpg
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient

from pyjobby.websocket_server import WebSocketServer
from tests.utils.processes import wait_until

# =============================================================================
# Harness
# =============================================================================


@dataclass
class WsHarness:
    """A live WebSocketServer, an HTTP client for it, and its open sockets."""

    server: WebSocketServer
    client: TestClient
    sockets: list[aiohttp.ClientWebSocketResponse] = field(default_factory=list)

    async def connect(self) -> aiohttp.ClientWebSocketResponse:
        """Open /ws and consume the welcome frame.

        No credentials of any kind are supplied: the server has no
        authentication, so a bare connect is a fully privileged session.
        """
        ws = await self.client.ws_connect("/ws")
        self.sockets.append(ws)
        hello = await recv(ws)
        assert hello["event"] == "connected"
        assert hello["data"]["server"] == "pyjobby-websocket"
        return ws

    def server_side(self, index: int = 0) -> web.WebSocketResponse:
        """The server's own WebSocketResponse for the nth accepted client."""
        return list(self.server.clients)[index]


async def recv(ws: aiohttp.ClientWebSocketResponse, timeout: float = 5.0) -> dict:
    """Receive one JSON text frame (fails on close/binary/timeout)."""
    msg = await asyncio.wait_for(ws.receive(), timeout)
    assert msg.type is aiohttp.WSMsgType.TEXT, f"unexpected frame: {msg}"
    return json.loads(msg.data)


async def ask(ws: aiohttp.ClientWebSocketResponse, payload: Any) -> dict:
    """Send a frame (dict -> JSON, str -> verbatim) and read the reply."""
    if isinstance(payload, str):
        await ws.send_str(payload)
    else:
        await ws.send_json(payload)
    return await recv(ws)


@pytest_asyncio.fixture
async def ws_factory(db_params, aiohttp_client):
    """Factory for in-process websocket servers.

    Each call builds a fresh WebSocketServer (so ``stats`` counters start at
    zero), initializes its real db pool, mounts /ws, and returns a harness.
    """
    harnesses: list[WsHarness] = []

    async def _make(
        *, notify: bool = False, pool: bool = True, **kwargs: Any
    ) -> WsHarness:
        server = WebSocketServer(db_params, **kwargs)
        if pool:
            await server.init_db_pool()
        if notify:
            await server.init_notify_connection()
        app = web.Application()
        app.router.add_get("/ws", server.handle_websocket)
        client = await aiohttp_client(app)
        harness = WsHarness(server=server, client=client)
        harnesses.append(harness)
        return harness

    yield _make

    for harness in harnesses:
        for ws in harness.sockets:
            if not ws.closed:
                await ws.close()
        if harness.server.notify_conn is not None:
            await harness.server.notify_conn.close()
        if harness.server.db_pool is not None:
            await harness.server.db_pool.close()


async def make_job(
    pool: asyncpg.Pool,
    queue: str,
    state: str = "queued",
    *,
    job_class: str = "HardeningJob",
    uid: int | None = None,
    error_count: int = 0,
) -> int:
    """Insert one job row directly and return its id."""
    async with pool.acquire() as conn:
        job_id: int = await conn.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, prio, state, uid,
                              error_count)
            VALUES ($1, '{}', $2, 100, $3::jorbstate, $4, $5)
            RETURNING id
            """,
            job_class,
            queue,
            state,
            uid,
            error_count,
        )
    return job_id


async def job_row(pool: asyncpg.Pool, job_id: int) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    assert row is not None
    return row


# =============================================================================
# 1. Rate limiting under adversarial traffic shapes
# =============================================================================

# 'subscribe' with an empty channel list is the cheapest rate-limited action:
# it touches no database, so burst timing measures the limiter, not PostgreSQL.
PING = {"action": "subscribe", "channels": []}


class TestRateLimiterUnderAdversarialTraffic:
    """The limiter is a per-connection sliding 1-second window of action
    timestamps. These tests drive real frames, not the handler directly."""

    @pytest.mark.asyncio
    async def test_straight_burst_over_limit_is_rejected(self, ws_factory):
        """The first N actions in a second are served; the N+1th is refused
        and the connection survives."""
        h = await ws_factory(max_actions_per_second=5, pool=False)
        ws = await h.connect()

        for i in range(5):
            reply = await ask(ws, PING)
            assert reply["event"] == "subscribed", f"action {i} was refused"

        refused = await ask(ws, PING)
        assert refused["event"] == "error"
        assert refused["data"]["message"] == "Rate limit exceeded"

        # Still refused while the window is full, and still connected.
        again = await ask(ws, PING)
        assert again["data"]["message"] == "Rate limit exceeded"
        assert not ws.closed
        assert h.server.stats["current_connections"] == 1

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_burst_pause_burst_refills_but_stays_bounded(self, ws_factory):
        """burst -> pause 1.1s -> burst.

        This is the shape a fixed-counter limiter gets wrong in one direction
        or the other: either the second burst is wrongly refused (counter never
        decays) or the window never refills. A sliding window must serve the
        second burst in full and then refuse the overflow again.
        """
        h = await ws_factory(max_actions_per_second=5, pool=False)
        ws = await h.connect()

        for _ in range(5):
            assert (await ask(ws, PING))["event"] == "subscribed"
        assert (await ask(ws, PING))["data"]["message"] == "Rate limit exceeded"

        await asyncio.sleep(1.1)

        for i in range(5):
            reply = await ask(ws, PING)
            assert reply["event"] == "subscribed", (
                f"action {i} of the post-pause burst was wrongly refused: {reply}"
            )
        assert (await ask(ws, PING))["data"]["message"] == "Rate limit exceeded"

        # The window holds exactly the limit, never more.
        client = h.server.clients[h.server_side()]
        assert len(client.action_times) == 5

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_sustained_just_under_limit_is_never_rejected(self, ws_factory):
        """4 actions/second against a limit of 5, sustained for 3 seconds:
        not one refusal (a leaky/fixed-window bucket would eventually trip)."""
        h = await ws_factory(max_actions_per_second=5, pool=False)
        ws = await h.connect()

        started = time.monotonic()
        for i in range(12):
            reply = await ask(ws, PING)
            assert reply["event"] == "subscribed", f"message {i} refused: {reply}"
            await asyncio.sleep(0.25)

        elapsed = time.monotonic() - started
        assert elapsed >= 2.9, f"traffic was not actually sustained ({elapsed:.2f}s)"
        assert h.server.stats["messages_received"] == 12

    @pytest.mark.asyncio
    async def test_rate_limit_is_per_connection_not_global(self, ws_factory):
        """One abusive client cannot exhaust another client's budget."""
        h = await ws_factory(max_actions_per_second=3, pool=False)
        abusive = await h.connect()
        victim = await h.connect()

        for _ in range(3):
            assert (await ask(abusive, PING))["event"] == "subscribed"
        assert (await ask(abusive, PING))["data"]["message"] == "Rate limit exceeded"

        for _ in range(3):
            assert (await ask(victim, PING))["event"] == "subscribed"

    @pytest.mark.asyncio
    async def test_unparseable_frames_bypass_the_limiter(self, ws_factory):
        """Documented behavior: the limiter runs *after* JSON parsing and the
        'action' lookup, so frames that fail those checks are unmetered. A
        flood of invalid JSON is bounded only by the socket, not by
        max_actions_per_second."""
        h = await ws_factory(max_actions_per_second=1, pool=False)
        ws = await h.connect()

        for _ in range(20):
            reply = await ask(ws, "{not json")
            assert reply["data"]["message"] == "Invalid JSON"

        client = h.server.clients[h.server_side()]
        assert len(client.action_times) == 0
        # A metered action is still available afterwards.
        assert (await ask(ws, PING))["event"] == "subscribed"


# =============================================================================
# 2. Malformed and hostile frames
# =============================================================================

# (frame, exact reply message) — frames whose reply is fully deterministic.
EXACT_FRAME_CASES: tuple[tuple[Any, str], ...] = (
    ("not json at all", "Invalid JSON"),
    ("{not json", "Invalid JSON"),
    ("", "Invalid JSON"),
    ('"a bare string"', "Error: 'str' object has no attribute 'get'"),
    ("123", "Error: 'int' object has no attribute 'get'"),
    ("null", "Error: 'NoneType' object has no attribute 'get'"),
    ("[]", "Error: 'list' object has no attribute 'get'"),
    ("[1, 2, 3]", "Error: 'list' object has no attribute 'get'"),
    ("true", "Error: 'bool' object has no attribute 'get'"),
    ({"no_action_here": 1}, "Missing 'action' field"),
    ({"action": None}, "Missing 'action' field"),
    ({"action": ""}, "Missing 'action' field"),
    ({"action": 0}, "Missing 'action' field"),
    ({"action": "nope"}, "Unknown action: nope"),
    ({"action": "ENQUEUE"}, "Unknown action: ENQUEUE"),
    ({"action": ["subscribe"]}, "Unknown action: ['subscribe']"),
    ({"action": "cancel_job"}, "Missing job_id"),
    ({"action": "cancel_job", "job_id": 0}, "Missing job_id"),
    ({"action": "cancel_job", "job_id": None}, "Missing job_id"),
    ({"action": "retry_job"}, "Missing job_id"),
    ({"action": "retry_job", "job_id": 0}, "Missing job_id"),
    ({"action": "adjust_priority"}, "Missing job_id or new_priority"),
    ({"action": "adjust_priority", "job_id": 1}, "Missing job_id or new_priority"),
    (
        {"action": "adjust_priority", "new_priority": 1},
        "Missing job_id or new_priority",
    ),
    ({"action": "subscribe", "channels": "notalist"}, "channels must be an array"),
    ({"action": "subscribe", "channels": 7}, "channels must be an array"),
    ({"action": "unsubscribe", "channels": 7}, "Error: 'int' object is not iterable"),
)

# (frame, reply message prefix) — the tail is a PostgreSQL/driver message.
PREFIX_FRAME_CASES: tuple[tuple[dict[str, Any], str], ...] = (
    ({"action": "cancel_job", "job_id": "abc"}, "Failed to cancel job: "),
    ({"action": "cancel_job", "job_id": [1, 2]}, "Failed to cancel job: "),
    ({"action": "cancel_job", "job_id": {"a": 1}}, "Failed to cancel job: "),
    ({"action": "cancel_job", "job_id": 2**63}, "Failed to cancel job: "),
    ({"action": "retry_job", "job_id": "abc"}, "Failed to retry job: "),
    ({"action": "retry_job", "job_id": 2**63}, "Failed to retry job: "),
    (
        {"action": "adjust_priority", "job_id": 1, "new_priority": "high"},
        "Failed to adjust priority: ",
    ),
    (
        {"action": "adjust_priority", "job_id": 1, "new_priority": 2**40},
        "Failed to adjust priority: ",
    ),
)


class TestHostileFrames:
    """Every malformed frame gets an error reply; the connection survives all
    of them and other clients are untouched."""

    @pytest.mark.asyncio
    async def test_every_malformed_frame_gets_the_documented_error(self, ws_factory):
        h = await ws_factory(max_actions_per_second=10_000)
        ws = await h.connect()

        for frame, expected in EXACT_FRAME_CASES:
            reply = await ask(ws, frame)
            assert reply["event"] == "error", (frame, reply)
            assert reply["data"]["message"] == expected, frame

        for frame, prefix in PREFIX_FRAME_CASES:
            reply = await ask(ws, frame)
            assert reply["event"] == "error", (frame, reply)
            assert reply["data"]["message"].startswith(prefix), (frame, reply)

        # After every hostile frame the socket is still fully usable.
        assert not ws.closed
        assert (await ask(ws, {"action": "get_stats"}))["event"] == "stats"

    @pytest.mark.asyncio
    async def test_hostile_frames_do_not_disturb_other_clients(self, ws_factory):
        """A second connection keeps working, keeps its subscriptions, and
        still receives broadcasts while the first is abused."""
        h = await ws_factory(max_actions_per_second=10_000)
        attacker = await h.connect()
        bystander = await h.connect()

        channel = f"jobs:{uuid.uuid4().hex[:8]}"
        assert (await ask(bystander, {"action": "subscribe", "channels": [channel]}))[
            "data"
        ]["channels"] == [channel]

        for frame, _ in EXACT_FRAME_CASES:
            reply = await ask(attacker, frame)
            assert reply["event"] == "error"

        await h.server.broadcast_event(channel, {"event": "probe", "data": {"n": 1}})
        event = await recv(bystander)
        assert event == {"event": "probe", "data": {"n": 1}}
        assert h.server.stats["current_connections"] == 2

    @pytest.mark.asyncio
    async def test_binary_frame_is_ignored_and_connection_stays_usable(
        self, ws_factory
    ):
        """Only TEXT frames are handled: a binary frame draws no reply at all
        (not even an error) and does not close the socket."""
        h = await ws_factory(pool=False)
        ws = await h.connect()

        await ws.send_bytes(b"\x00\x01\x02binary garbage")
        with pytest.raises(TimeoutError):
            await recv(ws, timeout=0.5)

        assert not ws.closed
        assert (await ask(ws, PING))["event"] == "subscribed"
        # The frame was never counted as a message.
        assert h.server.stats["messages_received"] == 1

    @pytest.mark.asyncio
    async def test_one_megabyte_frame_is_handled_and_echoed_back(self, ws_factory):
        """A 1 MiB frame is accepted. Note the amplification: the unknown
        action is echoed verbatim, so a 1 MiB request produces a >1 MiB
        response. Nothing truncates client-supplied text in error messages."""
        h = await ws_factory(max_actions_per_second=10_000, pool=False)
        ws = await h.connect()

        blob = "A" * (1024 * 1024)
        reply = await ask(ws, {"action": blob})
        assert reply["event"] == "error"
        assert reply["data"]["message"] == f"Unknown action: {blob}"
        assert len(reply["data"]["message"]) > 1024 * 1024

        assert not ws.closed
        assert (await ask(ws, PING))["event"] == "subscribed"

    @pytest.mark.asyncio
    async def test_one_megabyte_channel_name_is_stored_verbatim(self, ws_factory):
        """A 1 MiB channel name is accepted and retained in server memory:
        subscription channel names are unvalidated and unbounded in length."""
        h = await ws_factory(pool=False)
        ws = await h.connect()

        blob = "C" * (1024 * 1024)
        reply = await ask(ws, {"action": "subscribe", "channels": [blob]})
        assert reply["event"] == "subscribed"
        assert reply["data"]["channels"] == [blob]
        assert blob in h.server.subscriptions
        assert len(h.server.subscriptions[blob]) == 1

    @pytest.mark.asyncio
    async def test_float_job_id_is_silently_truncated_to_another_job(
        self, ws_factory, db_pool, unique_queue
    ):
        """BUG (reported): job_id is never validated as an integer, and asyncpg
        truncates a float bound to a bigint parameter. ``job_id: N + 0.9``
        therefore acts on job N while the reply echoes back ``N + 0.9`` — a UI
        would report success against a job id that does not exist."""
        h = await ws_factory()
        ws = await h.connect()
        target = await make_job(db_pool, unique_queue, "queued")
        other = await make_job(db_pool, unique_queue, "queued")

        reply = await ask(ws, {"action": "cancel_job", "job_id": target + 0.9})
        assert reply["event"] == "job_cancelled"
        assert reply["data"] == {"job_id": target + 0.9, "status": "cancelled"}

        assert (await job_row(db_pool, target))["state"] == "cancelled"
        assert (await job_row(db_pool, other))["state"] == "queued"

    @pytest.mark.asyncio
    async def test_error_stats_count_only_unexpected_failures(self, ws_factory):
        """Exact stats accounting: validation replies are not 'errors',
        unexpected exceptions are."""
        h = await ws_factory(max_actions_per_second=10_000, pool=False)
        ws = await h.connect()
        assert h.server.stats["messages_sent"] == 1  # welcome frame
        assert h.server.stats["errors"] == 0

        assert (await ask(ws, "{bad json"))["data"]["message"] == "Invalid JSON"
        assert h.server.stats["messages_received"] == 1
        assert h.server.stats["messages_sent"] == 2
        assert h.server.stats["errors"] == 0

        assert (await ask(ws, {"action": "nope"}))["data"]["message"] == (
            "Unknown action: nope"
        )
        assert h.server.stats["errors"] == 0

        # A frame that raises inside handle_message does count.
        await ask(ws, "123")
        assert h.server.stats["errors"] == 1
        assert h.server.stats["messages_received"] == 3


# =============================================================================
# 3. Subscription limits, idempotency, and bookkeeping
# =============================================================================


class TestSubscriptionLimits:
    """max_subscriptions is a per-connection cap on channel count."""

    @pytest.mark.asyncio
    async def test_limit_enforced_and_reported(self, ws_factory):
        h = await ws_factory(max_subscriptions=3, pool=False)
        ws = await h.connect()

        reply = await ask(ws, {"action": "subscribe", "channels": ["a", "b", "c"]})
        assert reply["event"] == "subscribed"
        assert sorted(reply["data"]["channels"]) == ["a", "b", "c"]

        refused = await ask(ws, {"action": "subscribe", "channels": ["d"]})
        assert refused["event"] == "error"
        assert refused["data"]["message"] == "Max subscriptions exceeded (3)"

        # The refused channel was not partially applied.
        client = h.server.clients[h.server_side()]
        assert client.channels == {"a", "b", "c"}
        assert "d" not in h.server.subscriptions

    @pytest.mark.asyncio
    async def test_batch_larger_than_limit_is_rejected_whole(self, ws_factory):
        h = await ws_factory(max_subscriptions=3, pool=False)
        ws = await h.connect()

        refused = await ask(
            ws, {"action": "subscribe", "channels": ["a", "b", "c", "d"]}
        )
        assert refused["data"]["message"] == "Max subscriptions exceeded (3)"
        assert h.server.clients[h.server_side()].channels == set()
        assert h.server.subscriptions == {}

    @pytest.mark.asyncio
    async def test_duplicates_count_against_the_limit(self, ws_factory):
        """Documented quirk: the check adds len(channels) without
        de-duplicating, so four copies of one channel trips a limit of three
        even though only one channel would be added."""
        h = await ws_factory(max_subscriptions=3, pool=False)
        ws = await h.connect()

        refused = await ask(
            ws, {"action": "subscribe", "channels": ["same", "same", "same", "same"]}
        )
        assert refused["data"]["message"] == "Max subscriptions exceeded (3)"
        assert h.server.clients[h.server_side()].channels == set()

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self, ws_factory):
        h = await ws_factory(max_subscriptions=100, pool=False)
        ws = await h.connect()

        for _ in range(3):
            reply = await ask(ws, {"action": "subscribe", "channels": ["jobs"]})
            assert reply["data"]["channels"] == ["jobs"]

        client = h.server.clients[h.server_side()]
        assert client.channels == {"jobs"}
        assert len(h.server.subscriptions["jobs"]) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_is_idempotent_and_tolerates_unknown_channels(
        self, ws_factory
    ):
        h = await ws_factory(max_subscriptions=100, pool=False)
        ws = await h.connect()
        server_ws = h.server_side()

        await ask(ws, {"action": "subscribe", "channels": ["jobs"]})

        reply = await ask(ws, {"action": "unsubscribe", "channels": ["jobs"]})
        assert reply["event"] == "unsubscribed"
        assert reply["data"]["channels"] == ["jobs"]
        assert h.server.clients[server_ws].channels == set()
        assert h.server.subscriptions["jobs"] == set()

        # Unsubscribing twice, and from a channel never subscribed, are both
        # no-ops that still acknowledge (echoing back what was requested).
        for channels in (["jobs"], ["never-subscribed"], []):
            reply = await ask(ws, {"action": "unsubscribe", "channels": channels})
            assert reply["event"] == "unsubscribed"
            assert reply["data"]["channels"] == channels
        assert h.server.clients[server_ws].channels == set()
        assert "never-subscribed" not in h.server.subscriptions

    @pytest.mark.asyncio
    async def test_disconnect_drops_every_socket_reference(self, ws_factory):
        """No websocket object survives its connection anywhere in the server
        (a leak here would keep buffers alive and break broadcasts)."""
        h = await ws_factory(max_subscriptions=100, pool=False)
        ws = await h.connect()
        server_ws = h.server_side()
        channels = [f"ghost:{i}" for i in range(5)]
        await ask(ws, {"action": "subscribe", "channels": channels})
        assert h.server.stats["current_connections"] == 1

        await ws.close()
        await wait_until(
            lambda: asyncio.sleep(0, result=server_ws not in h.server.clients),
            timeout=5.0,
            what="server-side cleanup",
        )

        assert h.server.clients == {}
        assert h.server.stats["current_connections"] == 0
        assert not any(
            server_ws in subscribers for subscribers in h.server.subscriptions.values()
        )

        # KNOWN LEAK (reported, not fixed here): the channel *keys* survive as
        # empty sets. Channel names are attacker-chosen and unvalidated, so a
        # client can add max_subscriptions keys, disconnect, and repeat — the
        # dict only ever grows.
        assert sorted(h.server.subscriptions) == sorted(channels)
        assert all(v == set() for v in h.server.subscriptions.values())

    @pytest.mark.asyncio
    async def test_broadcast_reaches_only_subscribers(self, ws_factory):
        h = await ws_factory(max_subscriptions=100, pool=False)
        subscriber = await h.connect()
        outsider = await h.connect()

        await ask(subscriber, {"action": "subscribe", "channels": ["queues:x"]})
        await h.server.broadcast_event(
            "queues:x", {"event": "queue_stats", "data": {"queue": "x"}}
        )

        assert (await recv(subscriber))["event"] == "queue_stats"
        with pytest.raises(TimeoutError):
            await recv(outsider, timeout=0.5)


# =============================================================================
# 4. THE UNAUTHENTICATED-MUTATION BOUNDARY
# =============================================================================


class TestUnauthenticatedMutationBoundary:
    """Executable specification of the websocket authorization model.

    **There is no authentication and no authorization.** Anyone who can open a
    TCP connection to the ``/ws`` endpoint gets a fully privileged session: no
    token, no cookie, no header, no origin check, and ``ClientConnection.uid``
    (the multi-tenancy field) is never populated by any code path. This is
    documented, intended behavior for the current release — ``pj-ws --host``
    warns that the API is unauthenticated and must be fronted by a proxy — so
    these tests assert what an anonymous client CAN do rather than treating it
    as a defect.

    Blast radius, proven job-by-job below. An anonymous client can:

    * ``cancel_job`` — cancel any queued/waiting job outright, and set
      ``cancel_requested`` on any claimed/running job, for any queue and any
      ``uid``. Denial of service against every tenant's work.
    * ``retry_job`` — requeue any job in state crashed, cancelled **or
      finished**. Re-running a *successfully finished* job is a duplicate side
      effect (re-charge, re-send, re-deliver); note the HTTP admin API refuses
      this (crashed/cancelled only), so the websocket surface is strictly more
      permissive than the admin surface.
    * ``adjust_priority`` — set any queued/waiting job's priority to any int32,
      so any job can jump ahead of or behind every other job.
    * ``get_stats`` — read every queue name, its paused flag and its
      concurrency/rate limits, the live worker count, and the server's own
      counters. Information disclosure of the whole topology.
    * ``subscribe`` — receive the live ``job_state_change`` feed for any queue,
      including job ids, job classes and state transitions belonging to other
      tenants.

    It cannot: create jobs, delete jobs or rows, pause/resume queues, or touch
    schedules — those actions simply do not exist in the dispatcher (asserted
    in ``test_no_other_actions_exist``). Adding authentication later must keep
    exactly the reachable set below as the thing being gated.
    """

    @pytest.mark.asyncio
    async def test_connection_requires_no_credentials(self, ws_factory):
        """A bare connect with no headers yields a privileged session whose
        tenancy field is empty."""
        h = await ws_factory(pool=False)
        ws = await h.connect()

        client = h.server.clients[h.server_side()]
        assert client.uid is None, "no code path ever authenticates a client"
        assert client.channels == set()
        assert h.server.stats["total_connections"] == 1

    @pytest.mark.asyncio
    async def test_cancel_job_kills_another_tenants_queued_job(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        job_id = await make_job(db_pool, unique_queue, "queued", uid=4242)

        reply = await ask(ws, {"action": "cancel_job", "job_id": job_id})
        assert reply["event"] == "job_cancelled"
        assert reply["data"] == {"job_id": job_id, "status": "cancelled"}

        row = await job_row(db_pool, job_id)
        assert row["state"] == "cancelled"
        assert row["uid"] == 4242  # ownership was never consulted
        assert row["finished"] is not None

    @pytest.mark.asyncio
    async def test_cancel_job_interrupts_another_tenants_running_job(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        job_id = await make_job(db_pool, unique_queue, "running", uid=99)

        reply = await ask(ws, {"action": "cancel_job", "job_id": job_id})
        assert reply["data"] == {"job_id": job_id, "status": "cancel_requested"}

        row = await job_row(db_pool, job_id)
        assert row["state"] == "running"
        assert row["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_cancel_job_refuses_terminal_states(
        self, ws_factory, db_pool, unique_queue
    ):
        """Bounding the radius: terminal jobs are not cancellable."""
        h = await ws_factory()
        ws = await h.connect()
        for state in ("finished", "crashed", "cancelled"):
            job_id = await make_job(db_pool, unique_queue, state)
            reply = await ask(ws, {"action": "cancel_job", "job_id": job_id})
            assert reply["event"] == "error"
            assert reply["data"]["message"] == (
                f"Job {job_id} not found or cannot be cancelled"
            )
            assert (await job_row(db_pool, job_id))["state"] == state

    @pytest.mark.asyncio
    async def test_retry_job_reruns_a_successfully_finished_job(
        self, ws_factory, db_pool, unique_queue
    ):
        """The widest mutation on this surface: an anonymous client can make a
        completed job run again (the HTTP admin API forbids this)."""
        h = await ws_factory()
        ws = await h.connect()
        job_id = await make_job(db_pool, unique_queue, "finished", uid=7)

        reply = await ask(ws, {"action": "retry_job", "job_id": job_id})
        assert reply["event"] == "job_retried"
        assert reply["data"] == {"job_id": job_id, "status": "requeued"}

        row = await job_row(db_pool, job_id)
        assert row["state"] == "queued"

    @pytest.mark.asyncio
    async def test_retry_job_requeues_crashed_and_resets_the_error_budget(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        job_id = await make_job(db_pool, unique_queue, "crashed", error_count=9)

        reply = await ask(ws, {"action": "retry_job", "job_id": job_id})
        assert reply["data"] == {"job_id": job_id, "status": "requeued"}

        row = await job_row(db_pool, job_id)
        assert row["state"] == "queued"
        assert row["error_count"] == 0

    @pytest.mark.asyncio
    async def test_retry_job_refuses_in_flight_states(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        for state in ("queued", "claimed", "running"):
            job_id = await make_job(db_pool, unique_queue, state)
            reply = await ask(ws, {"action": "retry_job", "job_id": job_id})
            assert reply["event"] == "error"
            assert reply["data"]["message"] == (
                f"Job {job_id} not found or cannot be retried"
            )
            assert (await job_row(db_pool, job_id))["state"] == state

    @pytest.mark.asyncio
    async def test_adjust_priority_reorders_another_tenants_queue(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        job_id = await make_job(db_pool, unique_queue, "queued", uid=1234)

        # Lower prio = more urgent: int32 minimum jumps the whole queue.
        reply = await ask(
            ws,
            {"action": "adjust_priority", "job_id": job_id, "new_priority": -(2**31)},
        )
        assert reply["event"] == "priority_adjusted"
        assert reply["data"] == {
            "job_id": job_id,
            "new_priority": -(2**31),
            "success": True,
        }
        assert (await job_row(db_pool, job_id))["prio"] == -(2**31)

        # And back to the least urgent value.
        await ask(
            ws,
            {"action": "adjust_priority", "job_id": job_id, "new_priority": 2**31 - 1},
        )
        assert (await job_row(db_pool, job_id))["prio"] == 2**31 - 1

    @pytest.mark.asyncio
    async def test_adjust_priority_only_touches_pending_jobs(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        for state in ("running", "finished", "crashed"):
            job_id = await make_job(db_pool, unique_queue, state)
            reply = await ask(
                ws,
                {"action": "adjust_priority", "job_id": job_id, "new_priority": 1},
            )
            assert reply["event"] == "error"
            assert reply["data"]["message"] == (
                f"Job {job_id} not found or cannot adjust priority"
            )
            assert (await job_row(db_pool, job_id))["prio"] == 100

    @pytest.mark.asyncio
    async def test_get_stats_discloses_the_whole_topology(
        self, ws_factory, db_pool, unique_queue
    ):
        h = await ws_factory()
        ws = await h.connect()
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_queue (name, paused, max_concurrency, rate_limit)
                VALUES ($1, TRUE, 5, 100)
                """,
                unique_queue,
            )
            await conn.execute("""
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ('secret-host-1', 4321, 'default')
            """)

        reply = await ask(ws, {"action": "get_stats"})
        assert reply["event"] == "stats"
        data = reply["data"]
        assert data["queues"][unique_queue] == {
            "paused": True,
            "max_concurrency": 5,
            "rate_limit": 100,
        }
        assert data["workers_live"] == 1
        assert data["server"]["total_connections"] == 1
        assert set(data["client"]) == {"connected_at", "channels", "action_count"}

    @pytest.mark.asyncio
    async def test_subscribe_streams_another_tenants_job_transitions(
        self, ws_factory, db_pool, unique_queue
    ):
        """End-to-end through PostgreSQL LISTEN/NOTIFY: an anonymous
        subscriber to `queues:<victim>` sees every state change on that queue,
        with job ids and job classes."""
        h = await ws_factory(notify=True)
        ws = await h.connect()

        # Job state changes fan out to queues:<queue> (and to 'jobs').
        reply = await ask(
            ws, {"action": "subscribe", "channels": [f"queues:{unique_queue}"]}
        )
        assert reply["event"] == "subscribed"
        assert reply["data"]["channels"] == [f"queues:{unique_queue}"]

        job_id = await make_job(
            db_pool, unique_queue, "queued", job_class="VictimJob", uid=555
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
            )

        event = await recv(ws, timeout=10.0)
        assert event["event"] == "job_state_change"
        assert event["data"]["id"] == job_id
        assert event["data"]["queue"] == unique_queue
        assert event["data"]["job_class"] == "VictimJob"
        assert event["data"]["old_state"] == "queued"
        assert event["data"]["new_state"] == "running"

    @pytest.mark.asyncio
    async def test_no_other_actions_exist(self, ws_factory, db_pool):
        """The reachable action set is exactly five verbs. Anything else — job
        creation, deletion, queue control, schedule management, SQL — is not
        dispatched, and nothing is written to the database while trying."""
        h = await ws_factory(max_actions_per_second=10_000)
        ws = await h.connect()
        async with db_pool.acquire() as conn:
            before = await conn.fetchval("SELECT COUNT(*) FROM jorb")

        for action in (
            "enqueue",
            "enqueue_job",
            "create_job",
            "delete_job",
            "pause_queue",
            "resume_queue",
            "create_schedule",
            "delete_schedule",
            "query",
            "sql",
            "eval",
            "shutdown",
            "SUBSCRIBE",
        ):
            reply = await ask(ws, {"action": action, "job_id": 1, "queue": "x"})
            assert reply["event"] == "error"
            assert reply["data"]["message"] == f"Unknown action: {action}"

        async with db_pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM jorb") == before

    def test_dispatcher_action_set_is_exactly_documented(self):
        """Guard the spec above against silent growth: the source of
        handle_message must dispatch exactly these actions."""
        import inspect

        source = inspect.getsource(WebSocketServer.handle_message)
        dispatched = {
            line.split('== "', 1)[1].split('"', 1)[0]
            for line in source.splitlines()
            if 'action == "' in line
        }
        assert dispatched == {
            "subscribe",
            "unsubscribe",
            "cancel_job",
            "retry_job",
            "adjust_priority",
            "get_stats",
        }


# =============================================================================
# 5. Notification task bounding
# =============================================================================


class TestNotificationTaskBounding:
    """`handle_notification` fires tasks from a sync asyncpg callback; the set
    of in-flight tasks is what keeps them alive and what bounds them."""

    @pytest.mark.asyncio
    async def test_saturated_task_set_drops_notifications(self, db_params):
        """With max_pending_notifications real tasks parked, further
        notifications are dropped rather than queued — and the set drains back
        to empty once they finish (no task-handle leak)."""
        server = WebSocketServer(db_params)
        server.max_pending_notifications = 50

        gate = asyncio.Event()
        processed: list[str] = []

        async def parked(channel: str, payload: str) -> None:
            await gate.wait()
            processed.append(payload)

        server.process_notification = parked  # type: ignore[method-assign]

        payloads = [json.dumps({"id": i, "queue": "q"}) for i in range(70)]
        for payload in payloads:
            server.handle_notification(None, 1, "job_state_change", payload)
            # Tasks cannot start until we await, so the set fills up.
        assert len(server._notification_tasks) == 50

        # Let the parked tasks finish.
        gate.set()
        await wait_until(
            lambda: asyncio.sleep(0, result=not server._notification_tasks),
            timeout=5.0,
            what="notification task set drains",
        )

        assert server._notification_tasks == set()
        assert processed == payloads[:50]
        assert len(processed) == 50  # the last 20 were dropped, not buffered

    @pytest.mark.asyncio
    async def test_completed_tasks_are_discarded_from_the_set(self, db_params):
        """Every completed task removes its own handle, so a long-lived server
        does not accumulate them."""
        server = WebSocketServer(db_params)

        for i in range(200):
            server.handle_notification(
                None, 1, "job_state_change", json.dumps({"id": i, "queue": "q"})
            )
        assert len(server._notification_tasks) == 200

        await wait_until(
            lambda: asyncio.sleep(0, result=not server._notification_tasks),
            timeout=5.0,
            what="notification task set drains",
        )
        assert server._notification_tasks == set()
        assert server.stats["events_received"] == 200
        assert server.stats["errors"] == 0

    @pytest.mark.asyncio
    async def test_dropped_notifications_do_not_break_later_ones(self, db_params):
        """After a flood is dropped, the server still processes new events."""
        server = WebSocketServer(db_params)
        server.max_pending_notifications = 5

        gate = asyncio.Event()

        async def parked(channel: str, payload: str) -> None:
            await gate.wait()

        original = server.process_notification
        server.process_notification = parked  # type: ignore[method-assign]
        for i in range(20):
            server.handle_notification(
                None, 1, "job_state_change", json.dumps({"id": i, "queue": "q"})
            )
        assert len(server._notification_tasks) == 5
        gate.set()
        await wait_until(
            lambda: asyncio.sleep(0, result=not server._notification_tasks),
            timeout=5.0,
            what="notification task set drains",
        )

        server.process_notification = original  # type: ignore[method-assign]
        server.handle_notification(
            None, 1, "job_state_change", json.dumps({"id": 999, "queue": "q"})
        )
        await wait_until(
            lambda: asyncio.sleep(0, result=not server._notification_tasks),
            timeout=5.0,
            what="notification task set drains",
        )
        assert server.stats["events_received"] == 1
