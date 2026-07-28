#!/usr/bin/env python3
"""
WebSocket Server for Real-Time Job Monitoring

Pure PostgreSQL implementation - NO Redis, NO external dependencies!

WHAT THIS SERVER PUSHES, AND WHY IT IS SHAPED THIS WAY

There used to be a ``job_state_change`` NOTIFY: one message per job state
transition, ungated, broadcast to every listener. It is gone, and this module
is where its replacement lives. Two independent reasons killed it:

* **Cost.** Committing a transaction that issued a NOTIFY takes a GLOBAL
  exclusive lock held through fsync, so notifying commits serialise against
  each other. The lock is per COMMIT, so ONE ungated channel costs as much as
  seven -- every other channel in the schema is demand-gated, which made this
  one the entire remaining bill. Deleting it is worth 2.6-2.9x on the
  completion path (measured across runs; tests/test_notify_gating.py rebuilds
  the deleted trigger so the "before" number stays measurable).
* **Usefulness.** At the reference workload (1M jobs/hour) it is ~830
  individual transitions per second. No dashboard renders that and no human
  reads it. A dashboard wants aggregates.

It could not simply be *gated* like the others: a gate trades a notification
for the consumer's polling fallback, and this consumer -- a browser -- has no
fallback, so gating would have silently DROPPED dashboard events rather than
delaying them.

So the push became a **poll of aggregates**, driven here:

* one query per :data:`DEFAULT_SNAPSHOT_INTERVAL`, shared by every subscribed
  client. Database cost is O(1) in the number of dashboards AND O(1) in job
  throughput, instead of O(transitions) as before.
* nothing runs at all while nobody is subscribed. The demand principle that
  governs the schema's notification gates governs this loop too.
* the statement is index-backed and bounded by work in flight, never by table
  size -- see :data:`SNAPSHOT_SQL`.

A client that genuinely needs per-job updates asks for THAT JOB (``watch_job``)
rather than tailing every transition; that rides ``jorb_done``, which is gated
on ``jorb.awaited`` and therefore costs a notification only for jobs somebody
actually asked about.

Features:
- Aggregate dashboard snapshots on an interval, one query for all clients
- Per-job watches over the demand-gated ``jorb_done`` channel
- Channel-based subscriptions
- Interactive job management (cancel, retry, priority adjust)
- Connection pooling and rate limiting
- Multiple WebSocket servers can run independently
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import aiohttp
import asyncpg  # type: ignore[import-untyped]
from aiohttp import web

from . import db
from .client import DEFAULT_PRIO_CEILING, validate_priority
from .lifecycle import TERMINAL_STATES, TERMINAL_STATES_SQL
from .monitor import DEFAULT_LIVENESS_GRACE_SECONDS

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# Bounds on client-supplied input
#
# The /ws endpoint is unauthenticated: every frame is hostile input. Each of
# these bounds exists because an unbounded value on this surface is an
# amplification or memory-growth primitive, not because of a protocol limit.
# =============================================================================

#: Largest inbound TEXT frame accepted, in characters. Bigger frames are
#: refused before they are parsed.
MAX_MESSAGE_LENGTH = 64 * 1024
#: Longest channel name accepted. Channel names become dict keys in
#: ``WebSocketServer.subscriptions``, so their length is server memory.
MAX_CHANNEL_NAME_LENGTH = 255
#: Error replies quote client-supplied text (an unknown action, a channel
#: name); truncating bounds the reply so a big frame cannot amplify.
MAX_ERROR_MESSAGE_LENGTH = 200

MAX_BIGINT = 2**63 - 1
MIN_INT32 = -(2**31)
MAX_INT32 = 2**31 - 1

# =============================================================================
# The aggregate snapshot: what replaced the per-transition firehose
# =============================================================================

#: Channel a client subscribes to for the whole-system aggregate snapshot.
#: It keeps the name the per-transition feed used, because it answers the same
#: question ("what is happening across all jobs") with a shape a dashboard can
#: actually render.
SNAPSHOT_CHANNEL = "jobs"
#: Prefix of the per-queue channels that receive the per-queue slice of the
#: same snapshot.
QUEUE_CHANNEL_PREFIX = "queues:"
#: Prefix of the per-job channels created by ``watch_job``.
JOB_CHANNEL_PREFIX = "job:"

#: The PostgreSQL NOTIFY channels this server LISTENs on. The names come from
#: :mod:`pyjobby.db`, which declares the channels sql/schema/90_notify.sql
#: emits, because PostgreSQL accepts LISTEN on any string: a name nothing
#: NOTIFYs does not fail, it just never fires -- and a client that subscribed
#: to the events it was supposed to carry waits forever. ``queue_alert`` was
#: exactly that, and is gone.
LISTEN_CHANNELS: Final = (db.CHANNEL_DONE, db.CHANNEL_SCHEDULE_EXECUTED)

#: Seconds between snapshots. A dashboard does not need faster, and this is
#: the whole database cost of the feed: one query per interval however many
#: dashboards are open and however many jobs are running. The old firehose
#: had no such bound -- it cost one NOTIFY per transition, forever upward.
DEFAULT_SNAPSHOT_INTERVAL = 1.0
#: How far back the snapshot's terminal-outcome counts look. Recent outcomes,
#: not all of history: "count everything" is hundreds of millions of rows at
#: the reference workload, and this runs every second.
DEFAULT_SNAPSHOT_WINDOW_SECONDS = 60.0

#: The ONE statement a snapshot issues.
#:
#: Every arm is an index-backed shape lifted from code that was already
#: measured for exactly this purpose, so the plan is known rather than hoped
#: for (tests/test_ws_snapshot.py EXPLAINs this exact string and fails on a
#: sequential scan):
#:
#:   backlog   admin_api.backlog_stats  -- jorb_claim_idx, index-only. Depth
#:                                         AND head-of-queue age; claimable
#:                                         only, so work scheduled for next
#:                                         week is not reported as backlog.
#:   scheduled the complement of that predicate on the same index: queued but
#:                                         not yet due. backlog + scheduled is
#:                                         every queued job, which is what
#:                                         PROM_SQL_LIVE_STATES arm 1 counts --
#:                                         split in two here because BOTH
#:                                         halves then carry an index
#:                                         condition, and a bare index-only
#:                                         scan with no condition is exactly
#:                                         the shape a planner abandons for a
#:                                         sequential scan.
#:   inflight  admin_api.inflight_stats -- jorb_inflight_idx, index-only, and
#:                                         bounded by the worker fleet.
#:   waiting   PROM_SQL_LIVE_STATES arm 3 -- jorb_waitfor_*_idx.
#:   recent    web_admin.PROM_SQL_TERMINAL_RECENT -- the COALESCE(finished,
#:                                         updated) predicate is what
#:                                         jorb_retention_idx is BUILT on;
#:                                         filtering bare `finished` matches
#:                                         no index and reads the heap of
#:                                         every job ever run.
#:   queue     web_admin.PROM_SQL_QUEUE_PAUSED -- control rows, so a paused
#:                                         queue with nothing pending still
#:                                         appears.
#:   workers   web_admin.PROM_SQL_WORKERS_LIVE -- jorb_worker_live_idx.
#:
#: Written as a UNION ALL rather than one grouped scan for the reason spelled
#: out over PROM_SQL_LIVE_STATES: a single predicate spanning several states
#: matches none of the partial indexes and collapses into a sequential scan.
#: Each arm gets its own index; the union is what keeps them.
#:
#: :data:`pyjobby.db.QUEUE_STATS_SQL` is the semantic contract these counts
#: answer to (queued = claimable now, scheduled = deferred and NOT backlog,
#: terminal states windowed); this string stays separate only because its
#: plan is pinned and it carries the kind/age columns the snapshot needs.
# f-string ONLY for module constants (the liveness grace from monitor.py and
# the terminal states from lifecycle.py, never user input): the dashboard's
# live-worker count must use the same grace as the monitor and pj-web, and
# the same three terminal states as everything else, or the surfaces
# disagree the day either declaration changes.
SNAPSHOT_SQL = f"""
    SELECT 'backlog'::text AS kind, queue, 'queued'::text AS state,
           COUNT(*)::bigint AS n,
           EXTRACT(EPOCH FROM (now() - MIN(run_after)))::float8 AS age
      FROM jorb WHERE state = 'queued' AND run_after <= now()
     GROUP BY queue
    UNION ALL
    SELECT 'scheduled', queue, 'scheduled', COUNT(*)::bigint, NULL::float8
      FROM jorb WHERE state = 'queued' AND run_after > now()
     GROUP BY queue
    UNION ALL
    SELECT 'inflight', queue, state::text, COUNT(*)::bigint,
           EXTRACT(EPOCH FROM (now() - MIN(updated)))::float8
      FROM jorb WHERE state IN ('claimed', 'running')
     GROUP BY queue, state
    UNION ALL
    SELECT 'live', queue, 'waiting', COUNT(*)::bigint, NULL::float8
      FROM jorb WHERE state = 'waiting'
     GROUP BY queue
    UNION ALL
    SELECT 'recent', queue, state::text, COUNT(*)::bigint, NULL::float8
      FROM jorb
     WHERE state IN ({TERMINAL_STATES_SQL})
       AND COALESCE(finished, updated) >= now() - $1::interval
     GROUP BY queue, state
    UNION ALL
    SELECT 'queue', name, NULL::text,
           (CASE WHEN paused THEN 1 ELSE 0 END)::bigint, NULL::float8
      FROM jorb_queue
    UNION ALL
    SELECT 'workers', NULL::text, NULL::text, COUNT(*)::bigint, NULL::float8
      FROM jorb_worker
     WHERE shutdown_at IS NULL
       AND last_seen > now() - interval '{DEFAULT_LIVENESS_GRACE_SECONDS} seconds'
"""

#: Registers a client's interest in one specific job and reports that job's
#: state in the same round trip.
#:
#: ORDER MATTERS, and it is the same argument the schema makes over
#: jorb_done_notify: demand (``awaited``) is set on the VERY ROW whose state
#: change would notify, so the two writers take the same row lock and
#: PostgreSQL orders them. Either the worker's terminal UPDATE sees
#: ``awaited`` and notifies us, or it committed first and the ``state`` this
#: statement returns is already terminal -- in which case the reply tells the
#: client so and no notification is owed. There is no third case, so a watch
#: can never hang waiting for an event that already happened.
#:
#: The UPDATE is conditional on ``NOT awaited`` so a re-watch is not a row
#: write; the CTE's outer read still answers from the pre-update snapshot,
#: where ``state`` is whatever it already was. NULL means no such job.
WATCH_JOB_SQL = """
    WITH registered AS (
        UPDATE jorb SET awaited = TRUE
         WHERE id = $1 AND NOT awaited
     RETURNING state::text AS state
    )
    SELECT COALESCE(
        (SELECT state FROM registered),
        (SELECT state::text FROM jorb WHERE id = $1)
    ) AS state
"""


class InvalidMessage(ValueError):
    """A client message rejected by validation.

    ``str(exc)`` is the message sent back to the client, so it must be safe to
    show and must not embed unbounded client text.
    """


@dataclass
class ClientConnection:
    """Represents a connected WebSocket client"""

    ws: web.WebSocketResponse
    channels: set[str]
    connected_at: float
    # Sliding window of recent action timestamps for rate limiting
    action_times: deque[float] = field(default_factory=deque)
    uid: int | None = None  # For multi-tenancy


class WebSocketServer:
    """
    WebSocket server for real-time job monitoring and management.

    Uses ONLY PostgreSQL LISTEN/NOTIFY - no Redis, no external dependencies!
    Each WebSocket server instance listens directly to PostgreSQL.
    Multiple servers can run independently.
    """

    def __init__(
        self,
        db_params: dict[str, Any],
        max_subscriptions: int = 100,
        max_actions_per_second: int = 10,
        snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL,
        snapshot_window_seconds: float = DEFAULT_SNAPSHOT_WINDOW_SECONDS,
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
    ):
        self.db_params = db_params
        self.max_subscriptions = max_subscriptions
        self.max_actions_per_second = max_actions_per_second
        # The priority ceiling this deployment's workers run with (`pj
        # --max-prio`). `adjust_priority` writes jorb.prio directly, so a
        # dashboard could otherwise push a job above every worker's ceiling
        # -- where it is never claimed, never fails and never reaches the
        # DLQ. Declared, not observed, for the reason client.py gives: the
        # ceiling belongs to the worker fleet and is invisible from here.
        self.prio_ceiling = prio_ceiling
        self.snapshot_interval = snapshot_interval
        self.snapshot_window = timedelta(seconds=snapshot_window_seconds)

        # Client management
        self.clients: dict[web.WebSocketResponse, ClientConnection] = {}
        self.subscriptions: dict[str, set[web.WebSocketResponse]] = {}

        # Database connections
        self.db_pool: asyncpg.Pool | None = None  # For queries
        self.notify_conn: asyncpg.Connection | None = None  # For LISTEN

        # In-flight notification broadcast tasks (kept to avoid GC of
        # fire-and-forget tasks; bounded so a flood cannot grow unbounded)
        self._notification_tasks: set[asyncio.Task] = set()
        self.max_pending_notifications = 1000

        # Statistics. `snapshot_queries` is the feed's entire database cost,
        # counted where it is incurred: it must stay flat while nobody is
        # subscribed and must not scale with the number of subscribers, and
        # tests/test_ws_snapshot.py asserts exactly that against this counter.
        self.stats = {
            "total_connections": 0,
            "current_connections": 0,
            "messages_sent": 0,
            "messages_received": 0,
            "events_received": 0,
            "snapshot_queries": 0,
            "errors": 0,
        }

    async def init_db_pool(self) -> None:
        """Initialize database connection pool for queries"""
        if not self.db_pool:
            self.db_pool = await db.create_pool(
                **self.db_params, min_size=2, max_size=10
            )
            logger.info("Database connection pool initialized")

    async def init_notify_connection(self) -> None:
        """Initialize dedicated connection for PostgreSQL LISTEN"""
        if not self.notify_conn or self.notify_conn.is_closed():
            self.notify_conn = await db.connect(**self.db_params)

            # Every channel this server LISTENs on, and every one of them is
            # emitted by a trigger in sql/schema/90_notify.sql -- LISTENing on a name
            # nothing NOTIFYs is accepted by PostgreSQL and then waits
            # forever, which is a promise to subscribers that cannot be kept
            # (tests/test_ws_snapshot.py checks this set against the catalog).
            # There is deliberately no per-transition channel here: the
            # whole-system view is polled (see SNAPSHOT_SQL), and the only
            # per-job push is jorb_done, which the schema gates on
            # jorb.awaited -- so it fires only for jobs a watch_job actually
            # asked about.
            for channel in LISTEN_CHANNELS:
                await self.notify_conn.add_listener(channel, self.handle_notification)

            logger.info("PostgreSQL LISTEN connections established")

    def handle_notification(
        self, connection: Any, pid: int, channel: str, payload: str
    ) -> None:
        """
        Handle PostgreSQL NOTIFY event.

        This is called by asyncpg when a NOTIFY is received.
        We schedule it as a task to avoid blocking. Task handles are kept in
        a bounded set so they cannot be garbage-collected mid-flight; if too
        many are pending, the notification is dropped (dashboard events are
        lossy by design).
        """
        if len(self._notification_tasks) >= self.max_pending_notifications:
            logger.warning(
                f"Too many pending notification tasks "
                f"({len(self._notification_tasks)}); dropping notification "
                f"on channel {channel}"
            )
            return

        task = asyncio.create_task(self.process_notification(channel, payload))
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    async def process_notification(self, channel: str, payload: str) -> None:
        """Process and broadcast notification to WebSocket clients"""
        try:
            # Parse payload
            data = json.loads(payload)

            # Create event structure
            event = {
                "event": channel,
                "timestamp": datetime.now(UTC).isoformat(),
                "data": data,
            }

            # Determine broadcast channel
            broadcast_channel = self.determine_broadcast_channel(channel, data)

            # Broadcast to subscribers
            await self.broadcast_event(broadcast_channel, event)

            self.stats["events_received"] += 1

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in NOTIFY payload: {payload} - {e}")
            self.stats["errors"] += 1
        except Exception as e:
            logger.error(f"Error processing notification: {e}")
            self.stats["errors"] += 1

    def determine_broadcast_channel(self, event_type: str, data: dict[str, Any]) -> str:
        """Determine which WebSocket channel to broadcast on"""
        if event_type == db.CHANNEL_DONE:
            # Delivered only to the clients that asked for THIS job. The
            # notification exists at all only because one of them registered
            # demand on the row (see WATCH_JOB_SQL).
            return f"{JOB_CHANNEL_PREFIX}{data.get('id')}"

        elif event_type == db.CHANNEL_SCHEDULE_EXECUTED:
            return "schedules"

        return "jobs"

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connection"""
        ws = web.WebSocketResponse(
            heartbeat=30,  # Send ping every 30 seconds
            timeout=60,  # Close if no pong in 60 seconds
        )
        await ws.prepare(request)

        # Create client connection
        client = ClientConnection(
            ws=ws,
            channels=set(),
            connected_at=time.time(),
        )

        self.clients[ws] = client
        self.stats["total_connections"] += 1
        self.stats["current_connections"] += 1

        logger.info(
            f"WebSocket connected: {request.remote} (total: {self.stats['current_connections']})"
        )

        try:
            # Send welcome message
            await self.send_to_client(
                ws,
                {
                    "event": "connected",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "server": "pyjobby-websocket",
                        "version": "1.0.0",
                        "backend": "PostgreSQL LISTEN/NOTIFY",
                    },
                },
            )

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self.stats["messages_received"] += 1
                    await self.handle_text_frame(ws, client, msg.data)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    self.stats["errors"] += 1

        except asyncio.CancelledError:
            logger.info("WebSocket connection cancelled")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.stats["errors"] += 1
        finally:
            # Cleanup. detach() is what keeps a disconnect from leaking an
            # empty, attacker-named channel key (see its docstring); the
            # sweep over self.subscriptions catches anything broadcast_event
            # already half-removed while reaping this client.
            self.clients.pop(ws, None)
            for channel in list(client.channels):
                self.detach(ws, client, channel)
            for channel, channel_subs in list(self.subscriptions.items()):
                channel_subs.discard(ws)
                if not channel_subs:
                    del self.subscriptions[channel]
            self.stats["current_connections"] -= 1

            logger.info(
                f"WebSocket disconnected (remaining: {self.stats['current_connections']})"
            )

        return ws

    def is_rate_limited(self, client: ClientConnection) -> bool:
        """Charge one token to the client's sliding 1-second window.

        Returns True (and charges nothing) when the window is already full.
        """
        now = time.time()
        window = client.action_times
        while window and now - window[0] > 1.0:
            window.popleft()
        if len(window) >= self.max_actions_per_second:
            return True
        window.append(now)
        return False

    async def handle_text_frame(
        self, ws: web.WebSocketResponse, client: ClientConnection, raw: str
    ) -> None:
        """Meter, size-check, parse and dispatch one inbound TEXT frame.

        Metering happens FIRST, before parsing and before the action lookup:
        every frame costs one token, so a flood of invalid JSON or unknown
        actions is limited exactly like a flood of valid requests. Metering
        after the parse would leave the cheapest-to-send frames unmetered.
        """
        if self.is_rate_limited(client):
            await self.send_error(ws, "Rate limit exceeded")
            return

        if len(raw) > MAX_MESSAGE_LENGTH:
            await self.send_error(
                ws, f"Message too large (max {MAX_MESSAGE_LENGTH} characters)"
            )
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_error(ws, "Invalid JSON")
            return

        try:
            await self.handle_message(ws, client, data)
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await self.send_error(ws, f"Error: {str(e)}")
            self.stats["errors"] += 1

    @staticmethod
    def validate_channels(channels: Any) -> list[str]:
        """Validate a subscribe/unsubscribe channel list."""
        if not isinstance(channels, list):
            raise InvalidMessage("channels must be an array")
        for channel in channels:
            if not isinstance(channel, str):
                raise InvalidMessage("channel names must be strings")
            if len(channel) > MAX_CHANNEL_NAME_LENGTH:
                raise InvalidMessage(
                    f"Channel name too long (max {MAX_CHANNEL_NAME_LENGTH} characters)"
                )
        return channels

    @staticmethod
    def validate_job_id(job_id: Any) -> int:
        """Validate a job id: a non-negative integer inside the bigint range.

        Nothing else may reach the database. asyncpg silently *truncates* a
        float bound to a bigint parameter, so an unvalidated ``job_id`` of
        ``N + 0.9`` would act on job N while the reply echoed ``N + 0.9``.
        Booleans are rejected explicitly (``bool`` is an ``int`` subclass), and
        absence is an explicit ``None`` check so that ``job_id: 0`` is reported
        as a missing job rather than as a missing field.
        """
        if job_id is None:
            raise InvalidMessage("Missing job_id")
        if isinstance(job_id, bool) or not isinstance(job_id, int):
            raise InvalidMessage("job_id must be an integer")
        value: int = job_id
        if not 0 <= value <= MAX_BIGINT:
            raise InvalidMessage(f"job_id must be between 0 and {MAX_BIGINT}")
        return value

    def validate_priority(self, new_priority: Any) -> int:
        """Validate a priority: an int32 (the width of the jorb.prio column)
        at or below this deployment's worker priority ceiling.

        The column's width is not the real bound. `claim_jorb()` takes only
        jobs whose `prio <= the claiming worker's ceiling`, so an operator
        who raises a job above it has not deprioritized it -- they have made
        it unclaimable, permanently, with no error, no retry and no DLQ
        entry. `JobClient` refuses that at enqueue; this handler writes
        `jorb.prio` with its own SQL, so it has to refuse it here.

        The predicate is the client's, imported, so the two doors onto the
        same column cannot drift apart. Only the wording is local: replies
        are truncated at MAX_ERROR_MESSAGE_LENGTH, and the client's longer
        message (which ends in `JobClient(...)` advice nobody clicking a
        dashboard button can act on) would arrive cut in half.
        """
        if new_priority is None:
            raise InvalidMessage("Missing new_priority")
        if isinstance(new_priority, bool) or not isinstance(new_priority, int):
            raise InvalidMessage("new_priority must be an integer")
        value: int = new_priority
        if not MIN_INT32 <= value <= MAX_INT32:
            raise InvalidMessage(
                f"new_priority must be between {MIN_INT32} and {MAX_INT32}"
            )
        try:
            validate_priority(value, self.prio_ceiling)
        except ValueError:
            raise InvalidMessage(
                f"new_priority {value} is above the worker priority ceiling "
                f"({self.prio_ceiling}): the job would sit 'queued' forever, "
                f"unclaimable. LOWER is MORE urgent, so least-urgent work "
                f"wants a value just under {self.prio_ceiling}."
            ) from None
        return value

    def validate_message(self, action: Any, data: dict[str, Any]) -> None:
        """Validate and normalize one message in place, before dispatch.

        All per-action input validation lives here rather than in the
        handlers, so an action added to the dispatcher inherits the bounds by
        naming its parameters here — and no handler can forget a check.
        """
        if action in ("subscribe", "unsubscribe"):
            data["channels"] = self.validate_channels(data.get("channels", []))
        if action in (
            "cancel_job",
            "rerun_job",
            "adjust_priority",
            "watch_job",
            "unwatch_job",
        ):
            data["job_id"] = self.validate_job_id(data.get("job_id"))
        if action == "adjust_priority":
            data["new_priority"] = self.validate_priority(data.get("new_priority"))

    async def handle_message(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Validate and dispatch one decoded client message.

        Rate limiting happens upstream in :meth:`handle_text_frame`: by the
        time a message gets here it has already been metered.
        """
        action = data.get("action")

        if not action:
            await self.send_error(ws, "Missing 'action' field")
            return

        try:
            self.validate_message(action, data)
        except InvalidMessage as e:
            await self.send_error(ws, str(e))
            return

        # Handle different actions
        if action == "subscribe":
            await self.handle_subscribe(ws, client, data)

        elif action == "unsubscribe":
            await self.handle_unsubscribe(ws, client, data)

        elif action == "watch_job":
            await self.handle_watch_job(ws, client, data)

        elif action == "unwatch_job":
            await self.handle_unwatch_job(ws, client, data)

        elif action == "cancel_job":
            await self.handle_cancel_job(ws, client, data)

        elif action == "rerun_job":
            await self.handle_rerun_job(ws, client, data)

        elif action == "adjust_priority":
            await self.handle_adjust_priority(ws, client, data)

        elif action == "get_stats":
            await self.handle_get_stats(ws, client, data)

        else:
            await self.send_error(ws, f"Unknown action: {action}")

    async def handle_subscribe(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle channel subscription (channels validated by
        validate_message: a list of strings, each length-bounded)"""
        channels = data.get("channels", [])

        # Check subscription limit
        if len(client.channels) + len(channels) > self.max_subscriptions:
            await self.send_error(
                ws, f"Max subscriptions exceeded ({self.max_subscriptions})"
            )
            return

        # Subscribe to channels
        for channel in channels:
            self.attach(ws, client, channel)

        await self.send_to_client(
            ws,
            {
                "event": "subscribed",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"channels": list(client.channels)},
            },
        )

        logger.debug(f"Client subscribed to: {channels}")

    async def handle_unsubscribe(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle channel unsubscription (channels validated by
        validate_message)"""
        channels = data.get("channels", [])

        for channel in channels:
            self.detach(ws, client, channel)

        await self.send_to_client(
            ws,
            {
                "event": "unsubscribed",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"channels": channels},
            },
        )

    def attach(
        self, ws: web.WebSocketResponse, client: ClientConnection, channel: str
    ) -> None:
        """Record that `client` wants `channel`. The one place demand is
        registered, so every feed reads it the same way."""
        if channel not in client.channels:
            client.channels.add(channel)
            self.subscriptions.setdefault(channel, set()).add(ws)

    def detach(
        self, ws: web.WebSocketResponse, client: ClientConnection, channel: str
    ) -> None:
        """Withdraw `client`'s interest in `channel`.

        Channel names are attacker-chosen, so the last subscriber must take
        the dict KEY with it: an empty set left behind is unbounded,
        attacker-controlled memory. Every removal path goes through here.
        """
        client.channels.discard(channel)
        subscribers = self.subscriptions.get(channel)
        if subscribers is not None:
            subscribers.discard(ws)
            if not subscribers:
                del self.subscriptions[channel]

    async def handle_watch_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Follow ONE job to its terminal state (job_id validated upstream).

        This is the per-job primitive that replaced tailing every transition.
        It is bounded by what the client asked for -- one job, one row of
        demand, one notification -- where the old feed was bounded by nothing
        and cost a NOTIFY per transition of every job in the system.

        Registering the watch sets `jorb.awaited`, which is precisely the gate
        `jorb_done_notify` tests, so the notification exists only because
        somebody is watching. The reply carries the job's CURRENT state, which
        is what makes the watch race-free: see WATCH_JOB_SQL.
        """
        job_id = data["job_id"]
        channel = f"{JOB_CHANNEL_PREFIX}{job_id}"

        if channel not in client.channels and (
            len(client.channels) >= self.max_subscriptions
        ):
            await self.send_error(
                ws, f"Max subscriptions exceeded ({self.max_subscriptions})"
            )
            return

        assert self.db_pool is not None
        try:
            state = await self.db_pool.fetchval(WATCH_JOB_SQL, job_id)
        except Exception as e:
            logger.error(f"Error watching job: {e}")
            await self.send_error(ws, f"Failed to watch job: {str(e)}")
            self.stats["errors"] += 1
            return

        if state is None:
            await self.send_error(ws, f"Job {job_id} not found")
            return

        self.attach(ws, client, channel)
        await self.send_to_client(
            ws,
            {
                "event": "watching",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"job_id": job_id, "channel": channel, "state": state},
            },
        )

    async def handle_unwatch_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Stop following one job (job_id validated upstream).

        `jorb.awaited` is deliberately NOT cleared. It is a latch, exactly as
        it is for JobClient.wait_for_result: clearing it would race a terminal
        UPDATE that has already read the row and decided to notify, and the
        cost of leaving it set is one extra notification for one job that is
        about to finish anyway.
        """
        job_id = data["job_id"]
        channel = f"{JOB_CHANNEL_PREFIX}{job_id}"
        self.detach(ws, client, channel)
        await self.send_to_client(
            ws,
            {
                "event": "unwatched",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"job_id": job_id, "channel": channel},
            },
        )

    async def handle_cancel_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle job cancellation request (job_id validated by
        validate_message)"""
        job_id = data["job_id"]

        assert self.db_pool is not None
        try:
            async with self.db_pool.acquire() as conn:
                # shared cancel path: immediate for queued/waiting, delivered
                # to the executing worker for claimed/running
                outcome = await db.cancel_job(conn, job_id)
                # the shared {job_id, status} shape, sent through as the
                # event payload so this surface says what the client and
                # admin API say
                result = {"job_id": job_id, "status": outcome or "not_cancellable"}

                if result["status"] == "not_cancellable":
                    await self.send_error(
                        ws, f"Job {job_id} not found or cannot be cancelled"
                    )
                else:
                    await self.send_to_client(
                        ws,
                        {
                            "event": "job_cancelled",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "data": result,
                        },
                    )
                    logger.info(f"Job {job_id} cancel outcome: {result['status']}")

        except Exception as e:
            logger.error(f"Error cancelling job: {e}")
            await self.send_error(ws, f"Failed to cancel job: {str(e)}")
            self.stats["errors"] += 1

    async def handle_rerun_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle a job RE-RUN request (job_id validated by validate_message).

        Named for what it does: this surface deliberately allows re-running
        FINISHED jobs (db.rerun_job), which repeats their side effects. The
        admin API and CLI `retry` verbs refuse finished jobs, so the two
        words must stay distinct — a surface labeled "retry" that silently
        repeats completed work is the drift this name exists to prevent.
        """
        job_id = data["job_id"]

        assert self.db_pool is not None
        try:
            async with self.db_pool.acquire() as conn:
                # shared re-run verb — jobs keep their ids across reruns.
                # fresh is stated, not defaulted, and echoed in the event:
                # "restart from step 1" vs "resume from checkpoints" are
                # opposite answers to the same button, so the payload must
                # say which one happened.
                requeued = await db.rerun_job(conn, job_id, fresh=True)
                result = {
                    "job_id": job_id,
                    "status": "requeued" if requeued else "not_rerunnable",
                    "fresh": True,
                }

                if result["status"] == "requeued":
                    await self.send_to_client(
                        ws,
                        {
                            "event": "job_rerun",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "data": result,
                        },
                    )
                    logger.info(f"Job {job_id} requeued via WebSocket")
                else:
                    await self.send_error(
                        ws, f"Job {job_id} not found or cannot be rerun"
                    )

        except Exception as e:
            logger.error(f"Error rerunning job: {e}")
            await self.send_error(ws, f"Failed to rerun job: {str(e)}")
            self.stats["errors"] += 1

    async def handle_adjust_priority(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle priority adjustment request (job_id and new_priority
        validated by validate_message)"""
        job_id = data["job_id"]
        new_priority = data["new_priority"]

        assert self.db_pool is not None
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE jorb
                    SET prio = $2
                    WHERE id = $1
                      AND state IN ('queued', 'waiting')
                """,
                    job_id,
                    new_priority,
                )

                if result == "UPDATE 0":
                    await self.send_error(
                        ws, f"Job {job_id} not found or cannot adjust priority"
                    )
                else:
                    await self.send_to_client(
                        ws,
                        {
                            "event": "priority_adjusted",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "data": {
                                "job_id": job_id,
                                "new_priority": new_priority,
                                "success": True,
                            },
                        },
                    )
                    logger.info(
                        f"Job {job_id} priority adjusted to {new_priority} via WebSocket"
                    )

        except Exception as e:
            logger.error(f"Error adjusting priority: {e}")
            await self.send_error(ws, f"Failed to adjust priority: {str(e)}")
            self.stats["errors"] += 1

    async def get_queue_controls(self) -> dict[str, dict[str, Any]]:
        """Fetch jorb_queue control rows keyed by queue name."""
        assert self.db_pool is not None
        rows = await self.db_pool.fetch(
            """
            SELECT name, paused, max_concurrency, rate_limit
            FROM jorb_queue ORDER BY name
            """
        )
        return {
            r["name"]: {
                "paused": r["paused"],
                "max_concurrency": r["max_concurrency"],
                "rate_limit": r["rate_limit"],
            }
            for r in rows
        }

    async def get_live_worker_count(
        self, stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS
    ) -> int:
        """Count live workers in the registry (no shutdown, fresh heartbeat)."""
        assert self.db_pool is not None
        count: int = await self.db_pool.fetchval(
            """
            SELECT COUNT(*) FROM jorb_worker
            WHERE shutdown_at IS NULL
              AND last_seen > now() - make_interval(secs => $1)
            """,
            stale_after_seconds,
        )
        return count

    async def handle_get_stats(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ) -> None:
        """Handle stats request"""
        queues: dict[str, dict[str, Any]] = {}
        workers_live = 0
        if self.db_pool is not None:
            queues = await self.get_queue_controls()
            workers_live = await self.get_live_worker_count()

        await self.send_to_client(
            ws,
            {
                "event": "stats",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {
                    "server": self.stats,
                    "queues": queues,
                    "workers_live": workers_live,
                    "client": {
                        "connected_at": client.connected_at,
                        "channels": list(client.channels),
                        "action_count": len(client.action_times),
                    },
                },
            },
        )

    async def broadcast_event(self, channel: str, event: dict[str, Any]) -> None:
        """Broadcast event to all subscribers of channel"""
        subscribers = self.subscriptions.get(channel, set())
        dead_clients = set()

        for ws in subscribers:
            try:
                await self.send_to_client(ws, event)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                dead_clients.add(ws)
                self.stats["errors"] += 1

        # Clean up dead clients
        for ws in dead_clients:
            subscribers.discard(ws)
            self.clients.pop(ws, None)
        if not subscribers:
            self.subscriptions.pop(channel, None)

    async def send_to_client(
        self, ws: web.WebSocketResponse, event: dict[str, Any]
    ) -> None:
        """Send event to specific client"""
        await ws.send_json(event)
        self.stats["messages_sent"] += 1

    async def send_error(self, ws: web.WebSocketResponse, message: str) -> None:
        """Send an error to the client, bounding what is echoed back.

        Error text quotes client-supplied values (an unknown action name, a
        driver message), so every error is truncated to
        MAX_ERROR_MESSAGE_LENGTH characters: a large frame must never buy a
        larger reply.
        """
        if len(message) > MAX_ERROR_MESSAGE_LENGTH:
            message = message[: MAX_ERROR_MESSAGE_LENGTH - 1] + "…"
        await self.send_to_client(
            ws,
            {
                "event": "error",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"message": message},
            },
        )

    @staticmethod
    def empty_queue_stats() -> dict[str, Any]:
        """The zero row of the snapshot: every counter a queue can report.

        Materialised for every queue that appears at all, so a consumer never
        has to distinguish "absent" from "zero" -- a queue that just drained
        must read as 0, not vanish.
        """
        return {
            "queued": 0,
            "claimed": 0,
            "running": 0,
            "waiting": 0,
            "backlog": 0,
            "scheduled": 0,
            "oldest_backlog_age_seconds": 0.0,
            "oldest_inflight_age_seconds": 0.0,
            "finished": 0,
            "crashed": 0,
            "cancelled": 0,
            "paused": False,
        }

    def snapshot_demand(self) -> bool:
        """Is anybody subscribed to something a snapshot would feed?

        The gate on the poll loop, and the same principle the schema's NOTIFY
        gates use: no registered demand, no work. `subscriptions` drops a
        channel's key with its last subscriber (see detach), so a present key
        IS a live subscriber.
        """
        return SNAPSHOT_CHANNEL in self.subscriptions or any(
            channel.startswith(QUEUE_CHANNEL_PREFIX) for channel in self.subscriptions
        )

    async def collect_snapshot(self) -> dict[str, Any]:
        """The aggregate a dashboard actually renders, in ONE query.

        One query for ALL subscribers, not one per subscriber: the database
        cost of the dashboard feed is a constant per interval, independent of
        how many browsers are open and independent of how many jobs per
        second the platform is running.
        """
        assert self.db_pool is not None
        rows = await self.db_pool.fetch(SNAPSHOT_SQL, self.snapshot_window)
        self.stats["snapshot_queries"] += 1

        queues: dict[str, dict[str, Any]] = {}
        workers_live = 0

        def bucket(name: str) -> dict[str, Any]:
            return queues.setdefault(name, self.empty_queue_stats())

        for row in rows:
            kind, queue, state = row["kind"], row["queue"], row["state"]
            count, age = row["n"], row["age"]
            if kind == "workers":
                workers_live = count
                continue
            stats = bucket(queue)
            if kind == "queue":
                stats["paused"] = bool(count)
            elif kind == "backlog":
                # `queued` means CLAIMABLE NOW on every surface -- see
                # db.QUEUE_STATS_SQL, which this snapshot answers to. It is
                # the same number `backlog` carries; both names are emitted
                # because the dashboard renders backlog-with-age while the
                # rest of the platform speaks in states.
                stats["backlog"] = count
                stats["queued"] = count
                stats["oldest_backlog_age_seconds"] = max(float(age or 0.0), 0.0)
            elif kind == "scheduled":
                # Queued in the database, but deliberately not due yet. NOT
                # added to `queued`: a job scheduled for next week is not the
                # fleet falling behind, and summing it here made the dashboard
                # report a different `queued` than pj-admin queues stats.
                stats["scheduled"] = count
            elif kind == "inflight":
                stats[state] = count
                stats["oldest_inflight_age_seconds"] = max(
                    stats["oldest_inflight_age_seconds"], float(age or 0.0)
                )
            else:  # 'live' and 'recent' are plain per-state counts
                stats[state] = count

        for stats in queues.values():
            stats["workers_live"] = workers_live

        totals = self.empty_queue_stats()
        del totals["paused"]
        for stats in queues.values():
            for key, value in stats.items():
                if key.startswith("oldest_"):
                    totals[key] = max(totals[key], value)
                elif key in totals:
                    totals[key] += value

        return {
            "interval_seconds": self.snapshot_interval,
            "window_seconds": self.snapshot_window.total_seconds(),
            "workers_live": workers_live,
            "totals": totals,
            "queues": queues,
        }

    async def broadcast_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Fan one snapshot out to everything that asked for part of it."""
        timestamp = datetime.now(UTC).isoformat()
        await self.broadcast_event(
            SNAPSHOT_CHANNEL,
            {"event": "dashboard", "timestamp": timestamp, "data": snapshot},
        )
        for queue, stats in snapshot["queues"].items():
            await self.broadcast_event(
                f"{QUEUE_CHANNEL_PREFIX}{queue}",
                {
                    "event": "queue_stats",
                    "timestamp": timestamp,
                    "data": {"queue": queue, **stats},
                },
            )

    async def _rearm_job_watches(self) -> None:
        """Re-arm jorb.awaited for every actively-watched job, and push a
        completion for any already terminal.

        Bounded by the number of active watches, not by the job table. Runs
        WATCH_JOB_SQL per watched job — the same statement handle_watch_job
        uses — which sets awaited (idempotent: `AND NOT awaited`) and returns
        the current state in one round trip. A terminal state means either
        the job finished this interval or its NOTIFY was missed while the
        latch was clear; either way we deliver the jorb_done event the watch
        promised and drop the watch. A vanished row (state None) is also a
        terminal outcome for the watch.
        """
        if self.db_pool is None:
            return
        # Parse defensively: `watch_job` only ever mints `job:<int>`, but a
        # client can `subscribe` to any string, and this loop runs for EVERY
        # connected client. An unparseable name used to raise out of the
        # comprehension, past the per-channel try below, into the broadcast
        # loop's catch-all -- so one client subscribing to "job:x" silently
        # stopped snapshots AND watch re-arming for everybody, for as long
        # as it stayed connected.
        watched: list[tuple[str, int]] = []
        for channel in list(self.subscriptions):
            if not channel.startswith(JOB_CHANNEL_PREFIX):
                continue
            suffix = channel[len(JOB_CHANNEL_PREFIX) :]
            if not suffix.isdigit():
                continue
            watched.append((channel, int(suffix)))
        for channel, job_id in watched:
            try:
                state = await self.db_pool.fetchval(WATCH_JOB_SQL, job_id)
            except Exception as e:
                logger.error(f"Error re-arming watch on job {job_id}: {e}")
                continue
            if state is not None and state not in TERMINAL_STATES:
                continue
            # terminal (or gone): deliver the completion the watch promised,
            # exactly as a live jorb_done NOTIFY would, then close the watch
            await self.broadcast_event(
                channel,
                {
                    "event": "jorb_done",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {"id": job_id, "state": state},
                },
            )
            for ws in list(self.subscriptions.get(channel, ())):
                client = self.clients.get(ws)
                if client is not None:
                    self.detach(ws, client, channel)

    async def snapshot_broadcast(self) -> None:
        """Poll aggregates on an interval and push them to subscribers.

        This loop IS the replacement for the deleted job_state_change
        firehose. Two properties make it affordable where the firehose was
        not, and both are asserted in tests/test_ws_snapshot.py:

        * it does nothing at all when nobody is subscribed, and
        * it issues one query per interval however many clients are attached.
        """
        while True:
            try:
                await asyncio.sleep(self.snapshot_interval)

                # LISTEN watchdog, on the same beat: the notify connection is
                # opened once at startup, and a connection nothing re-opens
                # is a server that reports healthy on the port while every
                # watch_job subscription waits forever for a jorb_done that
                # cannot arrive. init_notify_connection() is a no-op while
                # the connection is alive.
                if self.notify_conn is None or self.notify_conn.is_closed():
                    try:
                        await self.init_notify_connection()
                        logger.warning("PostgreSQL LISTEN connection re-established")
                    except Exception as e:
                        logger.error(
                            f"PostgreSQL LISTEN connection is down and could "
                            f"not be re-established ({e}); job watches are "
                            f"stalled until it returns"
                        )

                # Re-arm job watches on the same beat, BEFORE the aggregate-
                # demand gate (a job watch is its own demand, unrelated to
                # snapshot subscribers). jorb.awaited is a latch that a
                # long-lived job's compact() clears to shed notification cost;
                # a push-only watch has no fallback poll of its own, so
                # without this a dashboard watching a machine that compacts
                # would never learn it ended. Re-running WATCH_JOB_SQL sets
                # the latch back AND returns the current state, so it also
                # catches a completion whose NOTIFY was missed in the window
                # the latch was clear.
                if self.db_pool:
                    await self._rearm_job_watches()

                if not self.db_pool or not self.snapshot_demand():
                    continue

                await self.broadcast_snapshot(await self.collect_snapshot())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in snapshot broadcast: {e}")
                self.stats["errors"] += 1

    async def start(self, host: str = "127.0.0.1", port: int = 8082) -> None:
        """Start WebSocket server"""
        # Initialize connections
        await self.init_db_pool()
        await self.init_notify_connection()

        # Create web application
        app = web.Application()
        app.router.add_get("/ws", self.handle_websocket)

        # Health check endpoint
        async def health_check(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "status": "healthy",
                    "stats": self.stats,
                    "notify_connection": not self.notify_conn.is_closed()
                    if self.notify_conn
                    else False,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        app.router.add_get("/health", health_check)

        # Start the aggregate snapshot feed (idle until somebody subscribes)
        stats_task = asyncio.create_task(self.snapshot_broadcast())

        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"WebSocket server running on ws://{host}:{port}/ws")
        logger.info(f"Health check available at http://{host}:{port}/health")
        logger.info("Using PostgreSQL LISTEN/NOTIFY (no Redis needed!)")

        # SIGTERM/SIGINT set the stop event so the finally below actually
        # runs under systemd/Docker stop — the default SIGTERM disposition
        # kills the process with the pool still holding connections and
        # every client socket dropped mid-frame.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)

        try:
            await stop.wait()
            logger.info("Shutdown signal received")
        finally:
            # HTTP/WS acceptor first: runner.cleanup() stops accepting and
            # drains in-flight handle_websocket handlers. It MUST precede the
            # pool close -- a handler still running a query against a closing
            # pool gets InterfaceError, and asyncpg's Pool.close() blocks on
            # held connections, so closing the pool first can hang SIGTERM on
            # one client mid-query.
            await runner.cleanup()
            # then the snapshot feed (cancelled AND awaited, so its loop's
            # cleanup runs before the pool it reads from goes away)...
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task
            # ...then in-flight notification fan-outs, for the same reason
            if self._notification_tasks:
                for task in self._notification_tasks:
                    task.cancel()
                await asyncio.gather(*self._notification_tasks, return_exceptions=True)
            if self.notify_conn and not self.notify_conn.is_closed():
                await self.notify_conn.close()
            if self.db_pool:
                await self.db_pool.close()


async def serve(
    db_params: dict[str, Any],
    host: str,
    port: int,
    snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL,
    prio_ceiling: int = DEFAULT_PRIO_CEILING,
) -> None:
    """Create and run a WebSocketServer until interrupted."""
    server = WebSocketServer(
        db_params=db_params,
        snapshot_interval=snapshot_interval,
        prio_ceiling=prio_ceiling,
    )
    try:
        await server.start(host=host, port=port)
    except KeyboardInterrupt:
        logger.info("Shutting down...")


def main() -> None:
    """CLI entry point: the ``pj-ws`` console script."""
    import click

    @click.command()
    @click.option(
        "--config",
        "-c",
        default="./pyjobby.toml",
        show_default=True,
        help="Config file path (must define db_params; may define "
        "prio_ceiling) — the same -c/--config every other pyjobby daemon takes",
    )
    @click.option(
        "--host",
        default="127.0.0.1",
        show_default=True,
        help="Bind address (use 0.0.0.0 to expose beyond localhost; the "
        "websocket API is unauthenticated, so front it with a proxy first)",
    )
    @click.option("--port", default=8082, show_default=True, help="Bind port")
    @click.option(
        "--snapshot-interval",
        default=DEFAULT_SNAPSHOT_INTERVAL,
        show_default=True,
        type=float,
        help="Seconds between aggregate dashboard snapshots. This is the "
        "feed's whole database cost: one query per interval, shared by every "
        "connected dashboard, and none at all while nobody is subscribed",
    )
    @click.option(
        "--max-prio",
        default=None,
        type=int,
        help="The priority ceiling this fleet's workers run with (`pj "
        "--max-prio`). adjust_priority refuses anything above it: LOWER is "
        "MORE urgent, and a job above the ceiling is never claimed at all. "
        "Defaults to the config file's prio_ceiling, else 1000",
    )
    def cli(
        config: str,
        host: str,
        port: int,
        snapshot_interval: float,
        max_prio: int | None,
    ) -> None:
        """Run the realtime websocket dashboard server."""
        from .configloader import load_config_from_file

        cfg = load_config_from_file(config, keys=["db_params", "prio_ceiling"])
        db_params = cfg.get("db_params")
        if not db_params:
            raise click.ClickException(f"No db_params found in config: {config}")
        if max_prio is None:
            # `is not None`, not `or`: a configured prio_ceiling = 0 is a
            # real value, not "unset".
            configured = cfg.get("prio_ceiling")
            max_prio = DEFAULT_PRIO_CEILING if configured is None else configured

        if snapshot_interval <= 0:
            raise click.ClickException("--snapshot-interval must be positive")

        asyncio.run(serve(db_params, host, port, snapshot_interval, max_prio))

    cli()


if __name__ == "__main__":
    main()
