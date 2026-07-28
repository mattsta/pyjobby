# Real-Time WebSocket Dashboard

**Pure PostgreSQL Implementation - NO Redis, NO Docker!**

Live, interactive dashboard for monitoring and managing pyjobby jobs in real-time using WebSocket and PostgreSQL LISTEN/NOTIFY.

---

## 🎯 Features

- **Live Aggregates**: A whole-system snapshot (depths, backlog age, in-flight,
  recent outcomes, worker count) pushed once per second — **one database query
  per interval, shared by every connected dashboard**, and none at all while
  nobody is subscribed
- **Per-Job Watches**: Follow one specific job to completion, on a channel the
  database only emits *because* you asked for that job
- **Interactive Management**: Cancel, retry, and adjust job priorities from the UI
- **Pure PostgreSQL**: Uses LISTEN/NOTIFY - no Redis or external message broker needed
- **Clean Frontend**: Pure HTML/CSS/JavaScript - no framework bloat
- **Self-Contained**: Simple, production-ready, no complex dependencies
- **Multi-Server**: Multiple WebSocket servers can run independently

---

## 🏗️ Architecture

```
PostgreSQL Database
    |
    |-- polled once per --snapshot-interval, ONLY while somebody is subscribed
    |   (one index-backed aggregate query, shared by all clients)
    |
    '-- NOTIFY jorb_done, gated on jorb.awaited
        (fires only for jobs a client explicitly watched)
    |
    v
WebSocket Server (asyncpg pool + one LISTEN connection)
    |
    v
WebSocket Clients (browsers)
```

**Key Point**: the dashboard feed is a **poll of aggregates**, not a push of
transitions.

There is deliberately no per-transition NOTIFY channel. Two reasons:

- **It would be the platform's biggest write-path cost.** Committing a
  transaction that issued a NOTIFY takes a global exclusive lock held
  through fsync, so notifying commits serialise. The lock is taken per
  COMMIT, not per notification, so one ungated channel costs as much as all
  of them — measured at **2.6-2.9x** of completion-path throughput (12,019
  vs 31,591 jobs/s on one run, 11,917 vs 35,191 on another; 16 concurrent
  connections, one transaction per job, median of 5; see
  `tests/test_notify_gating.py`).
- **Nobody could consume it.** At a million jobs an hour that is ~830
  messages per second of individual transitions. A dashboard wants
  aggregates.

Nor can such a channel be demand-gated like the others, because a browser
has no polling fallback: a gate would silently *drop* dashboard events
instead of delaying them. So the dashboard gets a different mechanism, the
one documented below.

---

## 📦 Components

### 1. Database Triggers (`pyjobby/sql/schema.sql`)

Every NOTIFY in the platform goes through one trigger function, `jorb_notify()`,
which declares each channel's topic, demand gate and payload in one place. The
two this server listens on:

- **jorb_done**: a job reached a terminal state. **Demand-gated** on
  `jorb.awaited`, so it is emitted only for jobs somebody is waiting on — which
  is exactly what `watch_job` registers.
- **schedule_executed**: a scheduled job ran. Ungated, and affordable because
  it fires at cron rate on `jorb_schedule_log`, not on any hot path.

There is deliberately **no per-transition channel**. Do not add one back; see
the note in `schema.sql` above the `jorb_notify()` function for the cost.

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

### Step 1: Create the Schema

```bash
pj-admin --config ./pyjobby.conf.py db migrate
```

The notification triggers this server relies on ship in
`pyjobby/sql/schema.sql`; there is nothing extra to install.

### Step 2: Start WebSocket Server

```bash
pj-ws --config ./pyjobby.conf.py --snapshot-interval 1.0
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

#### dashboard

The aggregate snapshot, on the `jobs` channel, once per `--snapshot-interval`.
Counts are per queue and fleet-wide; ages are in seconds; `backlog` counts only
*claimable* work, so a job deliberately scheduled for next week is `queued` but
is not backlog.

```json
{
  "event": "dashboard",
  "timestamp": "2025-11-18T10:30:01.000Z",
  "data": {
    "interval_seconds": 1.0,
    "window_seconds": 60.0,
    "workers_live": 12,
    "totals": {
      "queued": 431, "claimed": 8, "running": 24, "waiting": 3,
      "backlog": 402, "scheduled": 29, "oldest_backlog_age_seconds": 37.2,
      "oldest_inflight_age_seconds": 4.1,
      "finished": 1893, "crashed": 4, "cancelled": 0
    },
    "queues": {
      "default": {
        "queued": 431, "claimed": 8, "running": 24, "waiting": 3,
        "backlog": 402, "scheduled": 29, "oldest_backlog_age_seconds": 37.2,
        "oldest_inflight_age_seconds": 4.1,
        "finished": 1893, "crashed": 4, "cancelled": 0,
        "paused": false, "workers_live": 12
      }
    }
  }
}
```

`finished` / `crashed` / `cancelled` are counts over the last
`window_seconds`, not over all of history.

#### watching / unwatched

The reply to `watch_job` / `unwatch_job`. The `state` in a `watching` reply is
the job's state at the moment the watch was registered — if it is already
terminal, no `jorb_done` is coming and none is owed.

```json
{
  "event": "watching",
  "timestamp": "2025-11-18T10:30:01.000Z",
  "data": { "job_id": 12345, "channel": "job:12345", "state": "running" }
}
```

#### jorb_done

A watched job reached a terminal state.

```json
{
  "event": "jorb_done",
  "timestamp": "2025-11-18T10:30:09.000Z",
  "data": { "id": 12345, "state": "finished" }
}
```

#### queue_stats

One queue's slice of the same snapshot, on `queues:{queue_name}`. Same fields
as a `dashboard` entry, plus the queue name — it is cut from the same query,
not fetched separately.

```json
{
  "event": "queue_stats",
  "timestamp": "2025-11-18T10:30:05.000Z",
  "data": {
    "queue": "default",
    "queued": 42, "claimed": 2, "running": 5, "waiting": 3,
    "backlog": 40, "scheduled": 2,
    "oldest_backlog_age_seconds": 12.4,
    "oldest_inflight_age_seconds": 3.0,
    "finished": 190, "crashed": 1, "cancelled": 0,
    "paused": false, "workers_live": 12
  }
}
```

`queued` = `backlog` + `scheduled`: backlog is claimable *now*, scheduled is
queued with a `run_after` in the future.

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

There is no alert event. Nothing in the platform decides that a queue is too
deep — thresholds belong to the operator, not to the feed — and the
`queue_stats` payload above already carries what an alert would have said
(`backlog`, `oldest_backlog_age_seconds`, `paused`, `workers_live`), once per
interval, for every queue. A client that wants an alert compares those to its
own threshold. There is deliberately no server-side alert channel: a
subscription to a channel nothing emits is a client waiting forever.

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

#### watch_job

Follow one specific job to its terminal state. This is the per-job primitive —
there is no feed of every transition to filter.

Registering a watch sets `jorb.awaited` on that row, which is the demand gate
`jorb_done` is built on: the database emits a completion notification *because*
you asked, and emits nothing for jobs nobody watched. The `watching` reply
carries the job's current state, so watching an already-finished job answers
immediately instead of hanging.

```json
{
  "action": "watch_job",
  "job_id": 12345
}
```

#### unwatch_job

```json
{
  "action": "unwatch_job",
  "job_id": 12345
}
```

#### cancel_job

```json
{
  "action": "cancel_job",
  "job_id": 12345
}
```

#### rerun_job

Named for what it does: re-runs a terminal job, **including one that
finished successfully** — which repeats its side effects. (The admin API
and CLI `retry` verbs refuse finished jobs; this dashboard action is the
explicit "do it again anyway".)

```json
{
  "action": "rerun_job",
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

- **jobs**: the whole-system aggregate snapshot (`dashboard` events)
- **queues:{queue_name}**: that queue's slice of the same snapshot
  (`queue_stats` events) — fed from the same single query, so a per-queue
  dashboard costs no extra database work
- **schedules**: schedule execution events
- **job:{job_id}**: created by `watch_job`; carries that job's completion.
  Subscribing to it by name does nothing useful — the notification only exists
  because `watch_job` registered demand for it.

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
    ],
  }),
);
```

---

## ⚡ Performance

- **Database cost of the feed**: one index-backed query per
  `--snapshot-interval`, **independent of the number of connected dashboards
  and independent of job throughput**, and zero while nobody is subscribed.
  That bound is the whole reason the per-transition feed was deleted; it is
  asserted, exactly, in `tests/test_ws_snapshot.py`.
- **Latency**: bounded by the snapshot interval (default 1s) for aggregates;
  sub-second push for a watched job's completion.
- **Connections**: 1000+ concurrent WebSocket clients per server — they share
  one snapshot, so the database does not notice them.
- **Overhead of a watch**: one row flag per watched job, one notification per
  watched job's completion.

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
   psql pyjobby -c "SELECT trigger_name FROM information_schema.triggers WHERE trigger_name LIKE 'jorb_%'"
   ```

### Not Receiving Updates

1. Subscribe to channels:

   ```javascript
   ws.send(JSON.stringify({ action: "subscribe", channels: ["jobs"] }));
   ```

2. Remember that snapshots are **only** produced while somebody is
   subscribed. A server with no subscribers issues no queries and sends
   nothing — that is the design, not a fault.

3. Check PostgreSQL NOTIFY is working, using the channel that still exists:

   ```bash
   # Terminal 1
   psql pyjobby -c "LISTEN jorb_done;"

   # Terminal 2 — register demand, then finish the job
   psql pyjobby -c "UPDATE jorb SET awaited=TRUE WHERE id=123;"
   psql pyjobby -c "UPDATE jorb SET state='finished' WHERE id=123;"

   # Terminal 1 should show NOTIFY
   ```

   Without the first UPDATE nothing is sent, and that is correct: the channel
   is demand-gated.

4. Check server logs for errors

### High Memory Usage

- Limit max subscriptions per client (default: 100)
- Raise `--snapshot-interval` (default: 1 second)
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
        if (msg.event === "dashboard") {
          const t = msg.data.totals;
          document.getElementById("jobs").textContent =
            `${t.running} running, ${t.backlog} waiting ` +
            `(head of queue ${t.oldest_backlog_age_seconds.toFixed(0)}s old)`;
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

  // Alert when the fleet falls behind. Aggregates, not individual jobs:
  // there is no per-transition feed to filter, by design.
  if (event.event === "dashboard") {
    for (const [queue, stats] of Object.entries(event.data.queues)) {
      if (stats.oldest_backlog_age_seconds > 300) {
        console.warn(`${queue} head of queue is ${stats.oldest_backlog_age_seconds}s old`);
      }
    }
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
