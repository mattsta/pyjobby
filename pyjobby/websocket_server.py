#!/usr/bin/env python3
"""
WebSocket Server for Real-Time Job Monitoring

Pure PostgreSQL LISTEN/NOTIFY implementation - NO Redis, NO external dependencies!

Provides live updates for:
- Job state changes
- Queue statistics
- Schedule executions
- Alerts and notifications

Features:
- Direct PostgreSQL LISTEN/NOTIFY (no Redis needed!)
- Channel-based subscriptions
- Interactive job management (cancel, retry, priority adjust)
- Connection pooling and rate limiting
- Automatic reconnection handling
- Multiple WebSocket servers can run independently
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
import asyncpg
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class ClientConnection:
    """Represents a connected WebSocket client"""

    ws: web.WebSocketResponse
    channels: set[str]
    connected_at: float
    last_action: float
    action_count: int
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
    ):
        self.db_params = db_params
        self.max_subscriptions = max_subscriptions
        self.max_actions_per_second = max_actions_per_second

        # Client management
        self.clients: dict[web.WebSocketResponse, ClientConnection] = {}
        self.subscriptions: dict[str, set[web.WebSocketResponse]] = {}

        # Database connections
        self.db_pool = None  # For queries
        self.notify_conn = None  # Dedicated connection for LISTEN

        # Statistics
        self.stats = {
            "total_connections": 0,
            "current_connections": 0,
            "messages_sent": 0,
            "messages_received": 0,
            "events_received": 0,
            "errors": 0,
        }

    async def init_db_pool(self):
        """Initialize database connection pool for queries"""
        if not self.db_pool:
            self.db_pool = await asyncpg.create_pool(
                **self.db_params, min_size=2, max_size=10
            )
            logger.info("Database connection pool initialized")

    async def init_notify_connection(self):
        """Initialize dedicated connection for PostgreSQL LISTEN"""
        if not self.notify_conn or self.notify_conn.is_closed():
            self.notify_conn = await asyncpg.connect(**self.db_params)

            # Set up listeners for all event channels
            await self.notify_conn.add_listener(
                "job_state_change", self.handle_notification
            )
            await self.notify_conn.add_listener(
                "schedule_executed", self.handle_notification
            )
            await self.notify_conn.add_listener("queue_alert", self.handle_notification)

            logger.info("PostgreSQL LISTEN connections established")

    def handle_notification(self, connection, pid, channel, payload):
        """
        Handle PostgreSQL NOTIFY event.

        This is called by asyncpg when a NOTIFY is received.
        We schedule it as a task to avoid blocking.
        """
        asyncio.create_task(self.process_notification(channel, payload))

    async def process_notification(self, channel: str, payload: str):
        """Process and broadcast notification to WebSocket clients"""
        try:
            # Parse payload
            data = json.loads(payload)

            # Create event structure
            event = {
                "event": channel,
                "timestamp": datetime.utcnow().isoformat(),
                "data": data,
            }

            # Determine broadcast channel
            broadcast_channel = self.determine_broadcast_channel(channel, data)

            # Broadcast to subscribers
            await self.broadcast_event(broadcast_channel, event)

            # Also broadcast to 'jobs' channel for all job events
            if channel == "job_state_change":
                await self.broadcast_event("jobs", event)

            self.stats["events_received"] += 1

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in NOTIFY payload: {payload} - {e}")
            self.stats["errors"] += 1
        except Exception as e:
            logger.error(f"Error processing notification: {e}")
            self.stats["errors"] += 1

    def determine_broadcast_channel(self, event_type: str, data: dict[str, Any]) -> str:
        """Determine which WebSocket channel to broadcast on"""
        if event_type == "job_state_change":
            queue = data.get("queue", "default")
            return f"queues:{queue}"

        elif event_type == "schedule_executed":
            return "schedules"

        elif event_type == "queue_alert":
            queue = data.get("queue", "default")
            return f"alerts:queues:{queue}"

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
            last_action=time.time(),
            action_count=0,
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
                    "timestamp": datetime.utcnow().isoformat(),
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
                    try:
                        data = json.loads(msg.data)
                        await self.handle_message(ws, client, data)
                    except json.JSONDecodeError:
                        await self.send_error(ws, "Invalid JSON")
                    except Exception as e:
                        logger.error(f"Error handling message: {e}")
                        await self.send_error(ws, f"Error: {str(e)}")
                        self.stats["errors"] += 1

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    self.stats["errors"] += 1

        except asyncio.CancelledError:
            logger.info("WebSocket connection cancelled")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.stats["errors"] += 1
        finally:
            # Cleanup
            self.clients.pop(ws, None)
            for channel_subs in self.subscriptions.values():
                channel_subs.discard(ws)
            self.stats["current_connections"] -= 1

            logger.info(
                f"WebSocket disconnected (remaining: {self.stats['current_connections']})"
            )

        return ws

    async def handle_message(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle incoming client message"""
        action = data.get("action")

        if not action:
            await self.send_error(ws, "Missing 'action' field")
            return

        # Rate limiting
        now = time.time()
        if now - client.last_action < 1.0:
            client.action_count += 1
            if client.action_count > self.max_actions_per_second:
                await self.send_error(ws, "Rate limit exceeded")
                return
        else:
            client.action_count = 0

        client.last_action = now

        # Handle different actions
        if action == "subscribe":
            await self.handle_subscribe(ws, client, data)

        elif action == "unsubscribe":
            await self.handle_unsubscribe(ws, client, data)

        elif action == "cancel_job":
            await self.handle_cancel_job(ws, client, data)

        elif action == "retry_job":
            await self.handle_retry_job(ws, client, data)

        elif action == "adjust_priority":
            await self.handle_adjust_priority(ws, client, data)

        elif action == "get_stats":
            await self.handle_get_stats(ws, client, data)

        else:
            await self.send_error(ws, f"Unknown action: {action}")

    async def handle_subscribe(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle channel subscription"""
        channels = data.get("channels", [])

        if not isinstance(channels, list):
            await self.send_error(ws, "channels must be an array")
            return

        # Check subscription limit
        if len(client.channels) + len(channels) > self.max_subscriptions:
            await self.send_error(
                ws, f"Max subscriptions exceeded ({self.max_subscriptions})"
            )
            return

        # Subscribe to channels
        for channel in channels:
            if channel not in client.channels:
                client.channels.add(channel)
                self.subscriptions.setdefault(channel, set()).add(ws)

        await self.send_to_client(
            ws,
            {
                "event": "subscribed",
                "timestamp": datetime.utcnow().isoformat(),
                "data": {"channels": list(client.channels)},
            },
        )

        logger.debug(f"Client subscribed to: {channels}")

    async def handle_unsubscribe(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle channel unsubscription"""
        channels = data.get("channels", [])

        for channel in channels:
            if channel in client.channels:
                client.channels.discard(channel)
                self.subscriptions.get(channel, set()).discard(ws)

        await self.send_to_client(
            ws,
            {
                "event": "unsubscribed",
                "timestamp": datetime.utcnow().isoformat(),
                "data": {"channels": channels},
            },
        )

    async def handle_cancel_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle job cancellation request"""
        job_id = data.get("job_id")

        if not job_id:
            await self.send_error(ws, "Missing job_id")
            return

        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE jorb
                    SET state = 'cancelled'
                    WHERE id = $1
                      AND state IN ('queued', 'waiting')
                """,
                    job_id,
                )

                if result == "UPDATE 0":
                    await self.send_error(
                        ws, f"Job {job_id} not found or cannot be cancelled"
                    )
                else:
                    await self.send_to_client(
                        ws,
                        {
                            "event": "job_cancelled",
                            "timestamp": datetime.utcnow().isoformat(),
                            "data": {"job_id": job_id, "success": True},
                        },
                    )
                    logger.info(f"Job {job_id} cancelled via WebSocket")

        except Exception as e:
            logger.error(f"Error cancelling job: {e}")
            await self.send_error(ws, f"Failed to cancel job: {str(e)}")
            self.stats["errors"] += 1

    async def handle_retry_job(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle job retry request"""
        job_id = data.get("job_id")

        if not job_id:
            await self.send_error(ws, "Missing job_id")
            return

        try:
            async with self.db_pool.acquire() as conn:
                new_job_id = await conn.fetchval(
                    """
                    INSERT INTO jorb (
                        job_class, kwargs, queue, prio, uid, capability,
                        run_after, run_group, admin_data, state
                    )
                    SELECT
                        job_class, kwargs, queue, prio, uid, capability,
                        TIMEZONE('utc', clock_timestamp()) as run_after,
                        run_group,
                        jsonb_set(
                            COALESCE(admin_data::jsonb, '{}'::jsonb),
                            '{retry_of}',
                            to_jsonb($1::bigint)
                        )::json,
                        'queued' as state
                    FROM jorb
                    WHERE id = $1
                      AND state IN ('crashed', 'finished')
                    RETURNING id
                """,
                    job_id,
                )

                if new_job_id:
                    await self.send_to_client(
                        ws,
                        {
                            "event": "job_retried",
                            "timestamp": datetime.utcnow().isoformat(),
                            "data": {
                                "original_job_id": job_id,
                                "new_job_id": new_job_id,
                                "success": True,
                            },
                        },
                    )
                    logger.info(f"Job {job_id} retried as {new_job_id} via WebSocket")
                else:
                    await self.send_error(
                        ws, f"Job {job_id} not found or cannot be retried"
                    )

        except Exception as e:
            logger.error(f"Error retrying job: {e}")
            await self.send_error(ws, f"Failed to retry job: {str(e)}")
            self.stats["errors"] += 1

    async def handle_adjust_priority(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle priority adjustment request"""
        job_id = data.get("job_id")
        new_priority = data.get("new_priority")

        if not job_id or new_priority is None:
            await self.send_error(ws, "Missing job_id or new_priority")
            return

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
                            "timestamp": datetime.utcnow().isoformat(),
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

    async def handle_get_stats(
        self, ws: web.WebSocketResponse, client: ClientConnection, data: dict[str, Any]
    ):
        """Handle stats request"""
        await self.send_to_client(
            ws,
            {
                "event": "stats",
                "timestamp": datetime.utcnow().isoformat(),
                "data": {
                    "server": self.stats,
                    "client": {
                        "connected_at": client.connected_at,
                        "channels": list(client.channels),
                        "action_count": client.action_count,
                    },
                },
            },
        )

    async def broadcast_event(self, channel: str, event: dict[str, Any]):
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

    async def send_to_client(self, ws: web.WebSocketResponse, event: dict[str, Any]):
        """Send event to specific client"""
        await ws.send_json(event)
        self.stats["messages_sent"] += 1

    async def send_error(self, ws: web.WebSocketResponse, message: str):
        """Send error message to client"""
        await self.send_to_client(
            ws,
            {
                "event": "error",
                "timestamp": datetime.utcnow().isoformat(),
                "data": {"message": message},
            },
        )

    async def periodic_stats_broadcast(self):
        """Periodically broadcast queue statistics"""
        while True:
            try:
                await asyncio.sleep(5)  # Every 5 seconds

                if not self.db_pool:
                    continue

                # Get queue statistics
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch("""
                        SELECT
                            queue,
                            state,
                            COUNT(*) as count
                        FROM jorb
                        WHERE state IN ('queued', 'running', 'waiting')
                        GROUP BY queue, state
                    """)

                # Organize by queue
                queue_stats = {}
                for row in rows:
                    queue = row["queue"]
                    if queue not in queue_stats:
                        queue_stats[queue] = {"queued": 0, "running": 0, "waiting": 0}
                    queue_stats[queue][row["state"]] = row["count"]

                # Broadcast to subscribers
                for queue, stats in queue_stats.items():
                    await self.broadcast_event(
                        f"queues:{queue}",
                        {
                            "event": "queue_stats",
                            "timestamp": datetime.utcnow().isoformat(),
                            "data": {"queue": queue, **stats},
                        },
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic stats broadcast: {e}")
                self.stats["errors"] += 1

    async def start(self, host: str = "0.0.0.0", port: int = 8082):
        """Start WebSocket server"""
        # Initialize connections
        await self.init_db_pool()
        await self.init_notify_connection()

        # Create web application
        app = web.Application()
        app.router.add_get("/ws", self.handle_websocket)

        # Health check endpoint
        async def health_check(request):
            return web.json_response(
                {
                    "status": "healthy",
                    "stats": self.stats,
                    "notify_connection": not self.notify_conn.is_closed()
                    if self.notify_conn
                    else False,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )

        app.router.add_get("/health", health_check)

        # Start periodic stats broadcasting
        stats_task = asyncio.create_task(self.periodic_stats_broadcast())

        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"WebSocket server running on ws://{host}:{port}/ws")
        logger.info(f"Health check available at http://{host}:{port}/health")
        logger.info("Using PostgreSQL LISTEN/NOTIFY (no Redis needed!)")

        try:
            # Keep running
            await asyncio.Event().wait()
        finally:
            # Cleanup
            stats_task.cancel()
            if self.notify_conn and not self.notify_conn.is_closed():
                await self.notify_conn.close()
            if self.db_pool:
                await self.db_pool.close()


async def main():
    """Main entry point for WebSocket server"""
    import sys

    from .configloader import load_config_from_file

    if len(sys.argv) < 2:
        print(
            "Usage: python -m pyjobby.websocket_server <config_file> [--host HOST] [--port PORT]"
        )
        sys.exit(1)

    config_file = sys.argv[1]
    host = "0.0.0.0"
    port = 8082

    # Parse command line arguments
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
        elif arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    # Load configuration
    config = load_config_from_file(config_file, keys=["db_params"])
    db_params = config.get("db_params", {})

    # Create and start server
    server = WebSocketServer(db_params=db_params)

    try:
        await server.start(host=host, port=port)
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
