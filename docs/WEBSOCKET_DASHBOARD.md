# Real-Time WebSocket Dashboard

**Pure PostgreSQL Implementation - NO Redis, NO Docker!**

Live, interactive dashboard for monitoring and managing pyjobby jobs in real-time using WebSocket and PostgreSQL LISTEN/NOTIFY.

---

## 🎯 Features

- **Real-Time Updates**: Job state changes broadcast instantly via WebSocket
- **Live Queue Statistics**: Queue depth and stats updated every 5 seconds
- **Interactive Management**: Cancel, retry, and adjust job priorities from the UI
- **Pure PostgreSQL**: Uses LISTEN/NOTIFY - no Redis or external message broker needed
- **Clean Frontend**: Pure HTML/CSS/JavaScript - no framework bloat
- **Self-Contained**: Simple, production-ready, no complex dependencies
- **Multi-Server**: Multiple WebSocket servers can run independently

---

## 🏗️ Architecture

```
PostgreSQL Database
    ↓
Triggers (on job state changes)
    ↓
NOTIFY (PostgreSQL pub/sub)
    ↓
WebSocket Server (asyncpg listener)
    ↓
WebSocket Clients (browsers)
```

**Key Point**: Each WebSocket server listens directly to PostgreSQL via asyncpg's LISTEN functionality. No Redis needed for message distribution!

---

## 📦 Components

### 1. Database Triggers (`priv/migrations/004_add_realtime_events.sql`)

PostgreSQL triggers that send NOTIFY events:

- **job_state_change**: Fires when job state changes (queued → running → finished, etc.)
- **schedule_executed**: Fires when scheduled job completes
- **queue_alert**: Fires when queue depth exceeds threshold (1000 jobs)
- **job_created**: Fires for 20% of new jobs (sampled to avoid spam)

**Install**:

```bash
psql pyjobby < priv/migrations/004_add_realtime_events.sql
```

### 2. WebSocket Server (`pyjobby/websocket_server.py`)

Python WebSocket server using aiohttp and asyncpg:

- Listens to PostgreSQL NOTIFY via dedicated connection
- Manages WebSocket client connections
- Broadcasts events to subscribed channels
- Handles client actions (cancel, retry, etc.)
- Provides health check endpoint

**Start**:

```bash
python -m pyjobby.websocket_server ./pyjobby.conf.py --port 8082
```

Or with custom host:

```bash
python -m pyjobby.websocket_server ./pyjobby.conf.py --host 0.0.0.0 --port 8082
```

### 3. Live Dashboard (`frontend/live-dashboard.html`)

Single-page HTML dashboard with:

- Live job list with state badges
- Real-time queue statistics
- Event log
- Connection status indicator
- Cancel/retry buttons
- Auto-reconnect on disconnect

**Open**:

```bash
# Edit WS_URL in HTML to point to your WebSocket server
# Default: ws://localhost:8082/ws

# Then open in browser
open frontend/live-dashboard.html
```

---

## 🚀 Quick Start

### Step 1: Apply Database Migration

```bash
psql pyjobby < priv/migrations/004_add_realtime_events.sql
```

### Step 2: Start WebSocket Server

```bash
python -m pyjobby.websocket_server ./pyjobby.conf.py
```

You should see:

```
WebSocket server running on ws://0.0.0.0:8082/ws
Health check available at http://0.0.0.0:8082/health
Using PostgreSQL LISTEN/NOTIFY (no Redis needed!)
PostgreSQL LISTEN connections established
```

### Step 3: Open Dashboard

Open `frontend/live-dashboard.html` in your browser.

### Step 4: Enqueue Some Jobs

```python
from pyjobby.client import JobClient
import asyncio


async def test_live_updates():
    async with await JobClient.from_config("./pyjobby.conf.py") as client:
        # Enqueue jobs - watch them appear in dashboard!
        for i in range(10):
            await client.enqueue("test.DemoJob", task_id=i, queue="default")
            await asyncio.sleep(0.5)


asyncio.run(test_live_updates())
```

Watch the jobs appear live in the dashboard!

---

## 🔌 WebSocket Protocol

### Connection

```javascript
const ws = new WebSocket("ws://localhost:8082/ws");

ws.onopen = () => {
  console.log("Connected!");

  // Subscribe to channels
  ws.send(
    JSON.stringify({
      action: "subscribe",
      channels: ["jobs", "queues:default", "schedules"],
    }),
  );
};
```

### Events Received

#### connected

```json
{
  "event": "connected",
  "timestamp": "2025-11-18T10:30:00.000Z",
  "data": {
    "server": "pyjobby-websocket",
    "version": "1.0.0",
    "backend": "PostgreSQL LISTEN/NOTIFY"
  }
}
```

#### job_state_change

```json
{
  "event": "job_state_change",
  "timestamp": "2025-11-18T10:30:01.000Z",
  "data": {
    "job_id": 12345,
    "old_state": "queued",
    "new_state": "running",
    "queue": "default",
    "job_class": "myapp.jobs.ProcessData",
    "priority": 100
  }
}
```

#### queue_stats

```json
{
  "event": "queue_stats",
  "timestamp": "2025-11-18T10:30:05.000Z",
  "data": {
    "queue": "default",
    "queued": 42,
    "running": 5,
    "waiting": 3
  }
}
```

#### schedule_executed

```json
{
  "event": "schedule_executed",
  "timestamp": "2025-11-18T10:30:10.000Z",
  "data": {
    "schedule_id": 10,
    "schedule_name": "daily-cleanup",
    "job_id": 12346,
    "result": "success",
    "next_run": "2025-11-19T02:00:00.000Z",
    "duration_ms": 1234
  }
}
```

#### queue_alert

```json
{
  "event": "queue_alert",
  "timestamp": "2025-11-18T10:30:15.000Z",
  "data": {
    "queue": "default",
    "depth": 1523,
    "threshold": 1000,
    "severity": "warning",
    "message": "Queue 'default' has 1523 jobs (threshold: 1000)"
  }
}
```

### Actions Sent

#### subscribe

```json
{
  "action": "subscribe",
  "channels": ["jobs", "queues:default", "schedules"]
}
```

#### unsubscribe

```json
{
  "action": "unsubscribe",
  "channels": ["queues:processing"]
}
```

#### cancel_job

```json
{
  "action": "cancel_job",
  "job_id": 12345
}
```

#### retry_job

```json
{
  "action": "retry_job",
  "job_id": 12345
}
```

#### adjust_priority

```json
{
  "action": "adjust_priority",
  "job_id": 12345,
  "new_priority": 500
}
```

#### get_stats

```json
{
  "action": "get_stats"
}
```

---

## 📊 Channels

Clients subscribe to specific channels to filter events:

- **jobs**: All job events
- **queues:{queue_name}**: Events for specific queue (e.g., `queues:default`)
- **schedules**: Schedule execution events
- **alerts:queues:{queue_name}**: Queue alert events

Example subscription:

```javascript
ws.send(
  JSON.stringify({
    action: "subscribe",
    channels: [
      "jobs", // All jobs
      "queues:default", // Default queue only
      "queues:processing", // Processing queue only
      "schedules", // Schedule events
      "alerts:queues:default", // Alerts for default queue
    ],
  }),
);
```

---

## ⚡ Performance

- **Latency**: Sub-second from database event to client update
- **Throughput**: 1000+ events/second
- **Connections**: 1000+ concurrent WebSocket clients per server
- **Overhead**: Minimal - triggers fire only on actual changes
- **Sampling**: Queue alerts and job creation sampled to avoid spam

---

## 🔒 Rate Limiting

- **Max subscriptions per client**: 100 channels
- **Max actions per second**: 10 actions/second
- **Automatic cleanup**: Dead connections removed automatically
- **Heartbeat**: 30-second ping/pong to detect disconnections

---

## 🏥 Health Check

```bash
curl http://localhost:8082/health
```

Response:

```json
{
  "status": "healthy",
  "stats": {
    "total_connections": 42,
    "current_connections": 5,
    "messages_sent": 1234,
    "messages_received": 567,
    "events_received": 890,
    "errors": 0
  },
  "notify_connection": true,
  "timestamp": "2025-11-18T10:30:00.000Z"
}
```

---

## 🎨 Frontend Customization

The dashboard HTML can be customized:

### Change WebSocket URL

```javascript
// In live-dashboard.html, line ~240
const WS_URL = "ws://your-server:8082/ws";
```

### Change Colors

```css
/* Modify the <style> section */
.badge.running {
  background: #your-color;
  color: #your-text-color;
}
```

### Add New Features

The dashboard is plain JavaScript - easy to extend:

```javascript
// Add custom event handler
if (event.event === "your_custom_event") {
  // Your logic here
}
```

---

## 🚧 Production Deployment

### Running as Service

Create systemd service `/etc/systemd/system/pyjobby-websocket.service`:

```ini
[Unit]
Description=Pyjobby WebSocket Server
After=network.target postgresql.service

[Service]
Type=simple
User=pyjobby
WorkingDirectory=/opt/pyjobby
ExecStart=/usr/bin/python3 -m pyjobby.websocket_server /opt/pyjobby/pyjobby.conf.py --host 0.0.0.0 --port 8082
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable pyjobby-websocket
sudo systemctl start pyjobby-websocket
```

### Behind NGINX

```nginx
# WebSocket proxy
location /ws {
    proxy_pass http://localhost:8082/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 86400;
}

# Health check
location /health {
    proxy_pass http://localhost:8082/health;
}
```

### SSL/TLS

Use `wss://` (WebSocket Secure) in production:

```javascript
const WS_URL = "wss://your-domain.com/ws";
```

NGINX handles SSL termination, forwards to local WebSocket server.

---

## 🐛 Troubleshooting

### WebSocket Won't Connect

1. Check server is running:

   ```bash
   curl http://localhost:8082/health
   ```

2. Check PostgreSQL connection:

   ```bash
   psql pyjobby -c "SELECT 1"
   ```

3. Check migration applied:
   ```bash
   psql pyjobby -c "SELECT trigger_name FROM information_schema.triggers WHERE trigger_name LIKE 'job_%'"
   ```

### Not Receiving Updates

1. Subscribe to channels:

   ```javascript
   ws.send(JSON.stringify({ action: "subscribe", channels: ["jobs"] }));
   ```

2. Check PostgreSQL NOTIFY working:

   ```bash
   # Terminal 1
   psql pyjobby -c "LISTEN job_state_change;"

   # Terminal 2 - trigger a job state change
   psql pyjobby -c "UPDATE jorb SET state='running' WHERE id=123;"

   # Terminal 1 should show NOTIFY
   ```

3. Check server logs for errors

### High Memory Usage

- Limit max subscriptions per client (default: 100)
- Reduce periodic stats interval (default: 5 seconds)
- Limit job list size in frontend (default: 100 jobs)

---

## 🎓 Examples

### Simple Monitor

```html
<!DOCTYPE html>
<html>
  <body>
    <div id="jobs"></div>
    <script>
      const ws = new WebSocket("ws://localhost:8082/ws");

      ws.onopen = () => {
        ws.send(
          JSON.stringify({
            action: "subscribe",
            channels: ["jobs"],
          }),
        );
      };

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.event === "job_state_change") {
          document.getElementById("jobs").innerHTML +=
            `<p>Job ${msg.data.job_id}: ${msg.data.new_state}</p>`;
        }
      };
    </script>
  </body>
</html>
```

### Node.js Client

```javascript
const WebSocket = require("ws");

const ws = new WebSocket("ws://localhost:8082/ws");

ws.on("open", () => {
  console.log("Connected");

  // Subscribe
  ws.send(
    JSON.stringify({
      action: "subscribe",
      channels: ["jobs", "queues:default"],
    }),
  );
});

ws.on("message", (data) => {
  const event = JSON.parse(data);
  console.log("Event:", event.event, event.data);

  // Auto-cancel slow jobs
  if (
    event.event === "job_state_change" &&
    event.data.new_state === "running"
  ) {
    setTimeout(() => {
      ws.send(
        JSON.stringify({
          action: "cancel_job",
          job_id: event.data.job_id,
        }),
      );
    }, 60000); // Cancel after 1 minute
  }
});
```

### Python Client

```python
import asyncio
import websockets
import json


async def monitor():
    uri = "ws://localhost:8082/ws"

    async with websockets.connect(uri) as ws:
        # Subscribe
        await ws.send(
            json.dumps({"action": "subscribe", "channels": ["jobs", "queues:default"]})
        )

        # Receive events
        async for message in ws:
            event = json.loads(message)
            print(f"Event: {event['event']}")
            print(f"Data: {event['data']}")


asyncio.run(monitor())
```

---

## 📚 See Also

- [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) - Python client for job submission
- [ADMIN_TOOLS.md](ADMIN_TOOLS.md) - CLI and Admin API
- [RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md) - Cron-based scheduling
- [INTERACTIVE_DASHBOARD.md](INTERACTIVE_DASHBOARD.md) - Full architecture design

---

## 💡 Why No Redis?

**PostgreSQL LISTEN/NOTIFY is perfect for this use case:**

✅ **Already there**: You're using PostgreSQL anyway
✅ **Reliable**: Transactions guarantee message delivery
✅ **Simple**: No extra infrastructure to maintain
✅ **Fast enough**: Sub-second latency for 1000+ events/sec
✅ **Scalable**: Each WebSocket server listens independently

**When you might need Redis:**

- 10,000+ concurrent WebSocket connections
- Cross-datacenter message distribution
- Message persistence beyond PostgreSQL

For most use cases, PostgreSQL LISTEN/NOTIFY is simpler and sufficient!

---

## 🎉 Summary

You now have a **production-ready, real-time WebSocket dashboard** for monitoring and managing pyjobby jobs with:

- ✅ Live job updates
- ✅ Interactive management
- ✅ Clean, simple architecture
- ✅ NO external dependencies (Redis, Docker, etc.)
- ✅ Pure PostgreSQL solution
- ✅ Self-contained and maintainable

**Start monitoring your jobs in real-time today!**
