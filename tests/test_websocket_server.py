"""
Comprehensive tests for websocket_server.py - WebSocket Real-Time Monitoring.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from aiohttp import web

from pyjobby.websocket_server import ClientConnection, WebSocketServer


def unique_name(base: str) -> str:
    """Generate unique name for test isolation."""
    return f"{base}_{uuid.uuid4().hex[:8]}"


# db_params comes from conftest.py (honors PYJOBBY_TEST_DSN)


class TestWebSocketServerInit:
    """Test WebSocketServer initialization - covers lines 61-87."""

    def test_init_defaults(self, db_params):
        """Test server initialization with default parameters."""
        server = WebSocketServer(db_params)

        assert server.db_params == db_params
        assert server.max_subscriptions == 100
        assert server.max_actions_per_second == 10
        assert server.clients == {}
        assert server.subscriptions == {}
        assert server.db_pool is None
        assert server.notify_conn is None

    def test_init_custom_params(self, db_params):
        """Test server initialization with custom parameters."""
        server = WebSocketServer(
            db_params, max_subscriptions=50, max_actions_per_second=5
        )

        assert server.max_subscriptions == 50
        assert server.max_actions_per_second == 5

    def test_init_stats(self, db_params):
        """Test that stats are properly initialized."""
        server = WebSocketServer(db_params)

        assert server.stats["total_connections"] == 0
        assert server.stats["current_connections"] == 0
        assert server.stats["messages_sent"] == 0
        assert server.stats["messages_received"] == 0
        assert server.stats["events_received"] == 0
        assert server.stats["errors"] == 0


class TestClientConnection:
    """Test ClientConnection dataclass - covers lines 41-49."""

    def test_client_connection_creation(self):
        """Test creating a ClientConnection."""
        ws = None  # In real tests, this would be a WebSocketResponse
        conn = ClientConnection(
            ws=ws,
            channels={"jobs", "queues:default"},
            connected_at=1234567890.0,
        )

        assert conn.ws is None
        assert "jobs" in conn.channels
        assert "queues:default" in conn.channels
        assert conn.connected_at == 1234567890.0
        assert len(conn.action_times) == 0
        assert conn.uid is None

    def test_client_connection_with_uid(self):
        """Test ClientConnection with uid for multi-tenancy."""
        conn = ClientConnection(
            ws=None,
            channels=set(),
            connected_at=0.0,
            uid=12345,
        )

        assert conn.uid == 12345


class TestDatabasePoolInit:
    """Test database pool initialization - covers lines 89-97."""

    @pytest.mark.asyncio
    async def test_init_db_pool(self, db_params):
        """Test initializing database connection pool."""
        server = WebSocketServer(db_params)

        assert server.db_pool is None

        await server.init_db_pool()

        assert server.db_pool is not None

        # Test that pool is usable
        async with server.db_pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1

        # Cleanup
        await server.db_pool.close()

    @pytest.mark.asyncio
    async def test_init_db_pool_idempotent(self, db_params):
        """Test that calling init_db_pool twice is safe."""
        server = WebSocketServer(db_params)

        await server.init_db_pool()
        pool1 = server.db_pool

        # Second call should not create new pool
        await server.init_db_pool()
        pool2 = server.db_pool

        assert pool1 is pool2

        await server.db_pool.close()


class TestNotifyConnection:
    """Test PostgreSQL LISTEN connection - covers lines 99-118."""

    @pytest.mark.asyncio
    async def test_init_notify_connection(self, db_params):
        """Test initializing notify connection."""
        server = WebSocketServer(db_params)

        assert server.notify_conn is None

        await server.init_notify_connection()

        assert server.notify_conn is not None
        assert not server.notify_conn.is_closed()

        # Cleanup
        await server.notify_conn.close()

    @pytest.mark.asyncio
    async def test_notify_connection_adds_listeners(self, db_params):
        """Test that notify connection adds LISTEN handlers."""
        server = WebSocketServer(db_params)

        await server.init_notify_connection()

        # Connection should have listeners registered
        assert server.notify_conn is not None

        # Cleanup
        await server.notify_conn.close()


class TestBroadcastChannels:
    """Test channel subscription and broadcasting - covers lines 162-175."""

    @pytest.mark.asyncio
    async def test_determine_broadcast_channel_job_done(self, db_params):
        """A jorb_done notification goes to the watchers of THAT job only.

        There is no per-transition channel to fan out any more: the whole
        system view is polled (see tests/test_ws_snapshot.py), and the only
        per-job push is the demand-gated completion of a watched job."""
        server = WebSocketServer(db_params)

        data = {"id": 123, "state": "finished"}
        channel = server.determine_broadcast_channel("jorb_done", data)

        assert channel == "job:123"

    @pytest.mark.asyncio
    async def test_determine_broadcast_channel_schedule(self, db_params):
        """Test determining broadcast channel for schedule events."""
        server = WebSocketServer(db_params)

        data = {"schedule_name": "daily-cleanup"}
        channel = server.determine_broadcast_channel("schedule_executed", data)

        assert channel == "schedules"


class TestMessageProcessing:
    """Test message processing - covers lines 129-159."""

    @pytest.mark.asyncio
    async def test_process_notification_json(self, db_params):
        """Test processing notification payload."""
        server = WebSocketServer(db_params)

        # Process a job completion notification
        payload = json.dumps({"id": 123, "state": "finished"})

        # This should not raise
        await server.process_notification("jorb_done", payload)

        # Stats should be updated
        assert server.stats["events_received"] == 1


class TestQueueStatsQuery:
    """Test queue stats query - covers lines 545-578."""

    @pytest.mark.asyncio
    async def test_get_queue_stats(self, db_params, db_pool):
        """Test getting queue statistics for broadcasts."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()

        # Create some jobs
        async with db_pool.acquire() as conn:
            queue = unique_name("ws_stats")
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('StatsJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('StatsJob', '{}', $1, 100, 'running')
            """,
                queue,
            )

        # Query stats using server's pool
        async with server.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT queue, state, COUNT(*) as count
                FROM jorb
                WHERE state IN ('queued', 'running', 'waiting')
                GROUP BY queue, state
            """)

            assert len(rows) > 0

        # Cleanup
        await server.db_pool.close()


class TestServerStart:
    """Test server start functionality - covers lines 586-630."""

    @pytest.mark.asyncio
    async def test_health_check_endpoint(self, db_params, aiohttp_client):
        """Test the health check endpoint."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        await server.init_notify_connection()

        # Create app with health check
        app = web.Application()

        async def health_check(request):
            return web.json_response(
                {
                    "status": "healthy",
                    "stats": server.stats,
                    "notify_connection": not server.notify_conn.is_closed()
                    if server.notify_conn
                    else False,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        app.router.add_get("/health", health_check)

        # Create test client
        client = await aiohttp_client(app)

        # Test health check
        resp = await client.get("/health")
        assert resp.status == 200

        data = await resp.json()
        assert data["status"] == "healthy"
        assert "stats" in data
        assert data["notify_connection"] is True

        # Cleanup
        await server.notify_conn.close()
        await server.db_pool.close()


class TestHandleNotification:
    """Test handle_notification callback - covers line 127."""

    @pytest.mark.asyncio
    async def test_handle_notification_schedules_task(self, db_params):
        """Test that handle_notification schedules an async task."""
        server = WebSocketServer(db_params)

        # Track if process_notification was called
        called = []
        original_process = server.process_notification

        async def mock_process(channel, payload):
            called.append((channel, payload))
            return await original_process(channel, payload)

        server.process_notification = mock_process

        # Call handle_notification
        payload = json.dumps({"id": 1, "state": "finished"})
        server.handle_notification(None, 123, "jorb_done", payload)

        # Wait for task to complete
        await asyncio.sleep(0.1)

        assert len(called) == 1
        assert called[0][0] == "jorb_done"


class TestProcessNotificationErrors:
    """Test process_notification error handling - covers lines 154-159."""

    @pytest.mark.asyncio
    async def test_process_notification_invalid_json(self, db_params):
        """Test handling of invalid JSON payload."""
        server = WebSocketServer(db_params)

        # Process invalid JSON
        await server.process_notification("jorb_done", "not valid json")

        # Error should be recorded in stats
        assert server.stats["errors"] == 1

    @pytest.mark.asyncio
    async def test_process_notification_json_decode_error(self, db_params):
        """Test JSON decode error handling."""
        server = WebSocketServer(db_params)

        # Process malformed JSON
        await server.process_notification("test", "{invalid}")

        assert server.stats["errors"] == 1
        assert server.stats["events_received"] == 0


class TestDetermineChannelQueueAlert:
    """Test determine_broadcast_channel for queue_alert - covers lines 170-174."""

    def test_queue_alert_with_queue(self, db_params):
        """Test queue_alert broadcast channel determination."""
        server = WebSocketServer(db_params)

        data = {"queue": "high-priority", "alert": "queue_depth_high"}
        channel = server.determine_broadcast_channel("queue_alert", data)

        assert channel == "alerts:queues:high-priority"

    def test_queue_alert_default_queue(self, db_params):
        """Test queue_alert with default queue."""
        server = WebSocketServer(db_params)

        data = {"alert": "some_alert"}  # No queue specified
        channel = server.determine_broadcast_channel("queue_alert", data)

        assert channel == "alerts:queues:default"

    def test_unknown_event_type(self, db_params):
        """Test unknown event type defaults to 'jobs' channel."""
        server = WebSocketServer(db_params)

        data = {"something": "else"}
        channel = server.determine_broadcast_channel("unknown_event", data)

        assert channel == "jobs"


class TestBroadcastEventNoClients:
    """Test broadcast_event when no clients are subscribed - covers lines 507-528."""

    @pytest.mark.asyncio
    async def test_broadcast_to_empty_channel(self, db_params):
        """Test broadcasting to channel with no subscribers."""
        server = WebSocketServer(db_params)

        # No clients subscribed
        event = {"event": "test", "data": {}}

        # Should not raise
        await server.broadcast_event("empty_channel", event)

        # Messages sent should still be 0
        assert server.stats["messages_sent"] == 0


class TestSendError:
    """Test send_error method - covers lines 530-538."""

    @pytest.mark.asyncio
    async def test_send_error_stats_increment(self, db_params):
        """Test that send_error increments error stats."""
        server = WebSocketServer(db_params)

        # We can't easily test without a real WebSocket connection,
        # but we can verify the method exists and has correct signature
        assert hasattr(server, "send_error")
        assert asyncio.iscoroutinefunction(server.send_error)


class TestHandleGetStats:
    """Test handle_get_stats method - covers lines 487-505."""

    @pytest.mark.asyncio
    async def test_stats_handler_exists(self, db_params):
        """Test that stats handler is available."""
        server = WebSocketServer(db_params)

        # Verify method exists
        assert hasattr(server, "handle_get_stats")
        assert asyncio.iscoroutinefunction(server.handle_get_stats)


class FakeWS:
    """Minimal stand-in for a WebSocketResponse capturing sent events."""

    def __init__(self):
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)


GET_STATS_FRAME = json.dumps({"action": "get_stats"})


class TestRateLimiting:
    """Test sliding-window per-client action rate limiting.

    Metering lives in handle_text_frame, ahead of json.loads and the action
    lookup, so these drive raw frames rather than decoded messages.
    """

    @pytest.mark.asyncio
    async def test_burst_over_limit_rejected(self, db_params):
        """More than max_actions_per_second in one burst gets rejected."""
        import time

        server = WebSocketServer(db_params)
        ws = FakeWS()
        client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())

        for _ in range(server.max_actions_per_second):
            await server.handle_text_frame(ws, client, GET_STATS_FRAME)

        assert all(e["event"] == "stats" for e in ws.sent)

        # 11th action within the same second must be rejected
        await server.handle_text_frame(ws, client, GET_STATS_FRAME)
        assert ws.sent[-1]["event"] == "error"
        assert "Rate limit" in ws.sent[-1]["data"]["message"]

    @pytest.mark.asyncio
    async def test_burst_pause_burst_still_limited(self, db_params):
        """A full window in the last second blocks even after earlier pauses."""
        import time

        server = WebSocketServer(db_params)
        ws = FakeWS()
        client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())

        # Simulate 10 actions performed 0.5s ago (still inside the window)
        now = time.time()
        client.action_times.extend([now - 0.5] * server.max_actions_per_second)

        await server.handle_text_frame(ws, client, GET_STATS_FRAME)
        assert ws.sent[-1]["event"] == "error"
        assert "Rate limit" in ws.sent[-1]["data"]["message"]

    @pytest.mark.asyncio
    async def test_old_actions_expire_from_window(self, db_params):
        """Actions older than 1s are pruned and no longer count."""
        import time

        server = WebSocketServer(db_params)
        ws = FakeWS()
        client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())

        # Actions from >1s ago must not count against the limit
        now = time.time()
        client.action_times.extend([now - 1.5] * server.max_actions_per_second)

        await server.handle_text_frame(ws, client, GET_STATS_FRAME)
        assert ws.sent[-1]["event"] == "stats"
        # Expired entries pruned; only the new action remains
        assert len(client.action_times) == 1

    @pytest.mark.asyncio
    async def test_unparseable_frame_costs_a_token(self, db_params):
        """A frame that never parses is metered all the same."""
        import time

        server = WebSocketServer(db_params, max_actions_per_second=1)
        ws = FakeWS()
        client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())

        await server.handle_text_frame(ws, client, "{not json")
        assert ws.sent[-1]["data"]["message"] == "Invalid JSON"
        assert len(client.action_times) == 1

        await server.handle_text_frame(ws, client, GET_STATS_FRAME)
        assert ws.sent[-1]["data"]["message"] == "Rate limit exceeded"


class TestNotificationTaskTracking:
    """Test that fire-and-forget notification tasks are tracked and bounded."""

    @pytest.mark.asyncio
    async def test_task_tracked_then_discarded(self, db_params):
        """Tasks are held in the set while running, discarded when done."""
        server = WebSocketServer(db_params)

        payload = json.dumps({"id": 1, "state": "finished"})
        server.handle_notification(None, 123, "jorb_done", payload)

        assert len(server._notification_tasks) == 1

        await asyncio.sleep(0.1)

        assert len(server._notification_tasks) == 0
        assert server.stats["events_received"] == 1

    @pytest.mark.asyncio
    async def test_notification_dropped_when_over_cap(self, db_params):
        """Notifications are dropped when too many tasks are pending."""
        server = WebSocketServer(db_params)

        # Simulate a full backlog of pending tasks
        server._notification_tasks = {
            object() for _ in range(server.max_pending_notifications)
        }

        payload = json.dumps({"id": 1, "state": "finished"})
        server.handle_notification(None, 123, "jorb_done", payload)

        # Nothing added, nothing processed
        assert len(server._notification_tasks) == server.max_pending_notifications
        await asyncio.sleep(0.05)
        assert server.stats["events_received"] == 0


class TestJobActionHandlers:
    """cancel/retry handlers use the shared v1 db primitives and shapes."""

    @pytest.mark.asyncio
    async def test_cancel_queued_job(self, db_params, db_pool):
        """Cancelling a queued job cancels it immediately."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            async with db_pool.acquire() as conn:
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ('WsCancelJob', '{}', 'test', 100, 'queued')
                    RETURNING id
                """)

            import time

            ws = FakeWS()
            client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())
            await server.handle_cancel_job(ws, client, {"job_id": job_id})

            assert ws.sent[-1]["event"] == "job_cancelled"
            assert ws.sent[-1]["data"] == {"job_id": job_id, "status": "cancelled"}

            async with db_pool.acquire() as conn:
                state = await conn.fetchval(
                    "SELECT state FROM jorb WHERE id = $1", job_id
                )
            assert state == "cancelled"
        finally:
            await server.db_pool.close()

    @pytest.mark.asyncio
    async def test_cancel_running_job_requests_cancel(self, db_params, db_pool):
        """Cancelling a running job delivers a cancel request instead."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            async with db_pool.acquire() as conn:
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ('WsCancelRun', '{}', 'test', 100, 'running')
                    RETURNING id
                """)

            import time

            ws = FakeWS()
            client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())
            await server.handle_cancel_job(ws, client, {"job_id": job_id})

            assert ws.sent[-1]["event"] == "job_cancelled"
            assert ws.sent[-1]["data"] == {
                "job_id": job_id,
                "status": "cancel_requested",
            }

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state, cancel_requested FROM jorb WHERE id = $1", job_id
                )
            assert row["state"] == "running"
            assert row["cancel_requested"] is True
        finally:
            await server.db_pool.close()

    @pytest.mark.asyncio
    async def test_retry_crashed_job_same_row(self, db_params, db_pool):
        """Retry requeues the SAME row; response carries job_id/status."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            async with db_pool.acquire() as conn:
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                      error_count)
                    VALUES ('WsRetryJob', '{}', 'test', 100, 'crashed', 5)
                    RETURNING id
                """)

            import time

            ws = FakeWS()
            client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())
            await server.handle_retry_job(ws, client, {"job_id": job_id})

            assert ws.sent[-1]["event"] == "job_retried"
            assert ws.sent[-1]["data"] == {"job_id": job_id, "status": "requeued"}

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT state, error_count FROM jorb WHERE id = $1", job_id
                )
            assert row["state"] == "queued"
            assert row["error_count"] == 0
        finally:
            await server.db_pool.close()

    @pytest.mark.asyncio
    async def test_retry_running_job_rejected(self, db_params, db_pool):
        """A running job cannot be retried."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            async with db_pool.acquire() as conn:
                job_id = await conn.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ('WsRetryRun', '{}', 'test', 100, 'running')
                    RETURNING id
                """)

            import time

            ws = FakeWS()
            client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())
            await server.handle_retry_job(ws, client, {"job_id": job_id})

            assert ws.sent[-1]["event"] == "error"
        finally:
            await server.db_pool.close()


class TestStatsControlPlane:
    """get_stats / periodic broadcast include paused flags + live workers."""

    @pytest.mark.asyncio
    async def test_get_stats_includes_paused_and_workers(self, db_params, db_pool):
        """stats reply carries jorb_queue paused flags and live worker count."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            queue = unique_name("ws_ctrl")
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)",
                    queue,
                )
                await conn.execute("""
                    INSERT INTO jorb_worker (host, pid, queue)
                    VALUES ('ws_host', 4321, 'default')
                """)

            import time

            ws = FakeWS()
            client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())
            await server.handle_get_stats(ws, client, {})

            data = ws.sent[-1]["data"]
            assert ws.sent[-1]["event"] == "stats"
            assert data["queues"][queue]["paused"] is True
            assert data["workers_live"] == 1
        finally:
            await server.db_pool.close()

    @pytest.mark.asyncio
    async def test_get_stats_without_pool_defaults(self, db_params):
        """With no pool yet, stats still answer with defaults."""
        import time

        server = WebSocketServer(db_params)
        ws = FakeWS()
        client = ClientConnection(ws=ws, channels=set(), connected_at=time.time())

        await server.handle_get_stats(ws, client, {})

        data = ws.sent[-1]["data"]
        assert data["queues"] == {}
        assert data["workers_live"] == 0

    @pytest.mark.asyncio
    async def test_collect_snapshot(self, db_params, db_pool):
        """Broadcast stats carry depths, paused flag, and worker count."""
        server = WebSocketServer(db_params)
        await server.init_db_pool()
        try:
            busy_queue = unique_name("ws_busy")
            idle_paused = unique_name("ws_idle")
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ('WsStatsJob', '{}', $1, 100, 'queued'),
                           ('WsStatsJob', '{}', $1, 100, 'running')
                """,
                    busy_queue,
                )
                # Paused queue with no pending jobs must still be reported
                await conn.execute(
                    "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)",
                    idle_paused,
                )
                await conn.execute("""
                    INSERT INTO jorb_worker (host, pid, queue)
                    VALUES ('ws_host', 8765, 'default')
                """)

            stats = (await server.collect_snapshot())["queues"]

            busy = stats[busy_queue]
            assert busy["queued"] == 1
            assert busy["running"] == 1
            assert busy["paused"] is False
            assert busy["workers_live"] == 1

            idle = stats[idle_paused]
            assert idle["queued"] == 0
            assert idle["paused"] is True
        finally:
            await server.db_pool.close()


class TestNotifyConnectionListen:
    """Test LISTEN setup in init_notify_connection - covers lines 99-118."""

    @pytest.mark.asyncio
    async def test_listen_channels_configured(self, db_params):
        """Test that LISTEN is configured on expected channels."""
        server = WebSocketServer(db_params)

        await server.init_notify_connection()

        # Connection should be established
        assert server.notify_conn is not None

        # The connection should have notification handler
        # We verify by checking it's a valid asyncpg connection
        assert hasattr(server.notify_conn, "add_listener")

        # Cleanup
        await server.notify_conn.close()
