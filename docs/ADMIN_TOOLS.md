# Pyjobby Admin Tools Documentation

**Date**: 2025-11-18
**Version**: 1.3.0
**Status**: Production Ready

---

## 📊 Overview

Pyjobby now includes comprehensive administrative tools for managing jobs, queues, and workers:

1. **Backend Admin API** (`pyjobby/admin_api.py`) - Python API for all management operations
2. **CLI Tools** (`pj-admin`) - Command-line interface for terminal management
3. **Web Interface** (`pj-web`) - Modern web UI with htmx for browser-based management

All three interfaces share the same backend API for consistency.

---

## 🔧 Backend Admin API

### Installation

The Admin API is included in pyjobby. Just import it:

```python
from pyjobby.admin_api import AdminAPI
import asyncpg

# Connect to database
conn = await asyncpg.connect(
    database="pyjobby", user="pyjobby", password="password", host="localhost"
)

# Create API instance
api = AdminAPI(conn)
```

### Job Management

#### List Jobs

```python
# List all jobs
jobs = await api.list_jobs(limit=50)

# Filter by queue
jobs = await api.list_jobs(queue="default")

# Filter by state
jobs = await api.list_jobs(state="crashed")

# Filter by job class (supports LIKE patterns)
jobs = await api.list_jobs(job_class="job.email%")

# Filter by user ID
jobs = await api.list_jobs(uid=123)

# Pagination
jobs = await api.list_jobs(limit=25, offset=50)

# Custom ordering
jobs = await api.list_jobs(order_by="created", order_dir="DESC")
```

#### Get Job Details

```python
job = await api.get_job(job_id=12345)

if job:
    print(f"Job {job['id']}: {job['state']}")
    print(f"Class: {job['job_class']}")
    print(f"Args: {job['kwargs']}")
    if job["error_message"]:
        print(f"Error: {job['error_message']}")
```

#### Retry Jobs

```python
# Retry single crashed job
result = await api.retry_job(job_id=12345)
print(f"Retry queued as job {result['new_job_id']}")

# Retry multiple jobs
results = await api.retry_jobs([12345, 12346, 12347])
for r in results:
    if r["status"] == "error":
        print(f"Failed: {r['error']}")
    else:
        print(f"Job {r['original_job_id']} → {r['new_job_id']}")
```

#### Cancel Jobs

```python
# Cancel queued job
result = await api.cancel_job(job_id=12345)

# Cancel multiple jobs
results = await api.cancel_jobs([12345, 12346])
```

#### Delete Jobs

```python
# Delete single job
deleted = await api.delete_job(job_id=12345)

# Bulk delete
count = await api.delete_jobs(queue="test", state="finished", older_than_days=30)
print(f"Deleted {count} jobs")
```

### Queue Management

#### List Queues

```python
queues = await api.list_queues()
print(f"Queues: {', '.join(queues)}")
```

#### Queue Statistics

```python
# All queues
stats = await api.queue_stats()

for s in stats:
    print(f"Queue: {s['queue']}")
    print(f"  Queued: {s['queued']}")
    print(f"  Running: {s['running']}")
    print(f"  Crashed: {s['crashed']}")
    print(f"  Total: {s['total']}")

# Specific queue
stats = await api.queue_stats(queue="default")
```

#### Clear Queue

```python
# Clear all jobs in queue
count = await api.clear_queue(queue="test")

# Clear only finished jobs
count = await api.clear_queue(queue="default", state="finished")

# Clear old jobs
count = await api.clear_queue(queue="default", state="finished", older_than_days=7)
```

### Worker Management

#### List Active Workers

```python
workers = await api.list_workers()

for w in workers:
    print(f"Worker: {w['worker_host']}:{w['worker_pid']}")
    print(f"  Job: {w['job_id']} ({w['job_class']})")
    print(f"  State: {w['state']}")
```

#### Worker Statistics

```python
stats = await api.worker_stats()

print(f"Active Workers: {stats['active_workers']}")
for w in stats["workers"]:
    print(f"  {w['host']}:{w['pid']} - {w['job_count']} jobs")
```

### Metrics & Monitoring

#### System Metrics

```python
from datetime import datetime, timedelta

# Last 24 hours
since = datetime.utcnow() - timedelta(hours=24)
metrics = await api.get_metrics(since=since)

print(f"Finished: {metrics['finished_count']}")
print(f"Crashed: {metrics['crashed_count']}")
print(f"Avg Duration: {metrics['avg_duration_seconds']:.2f}s")

# State counts
for state, count in metrics["state_counts"].items():
    print(f"  {state}: {count}")

# Top errors
for error in metrics["top_errors"][:5]:
    print(f"  {error['job_class']}: {error['error_count']} errors")

# Specific queue
metrics = await api.get_metrics(since=since, queue="priority")
```

### Dead Letter Queue

#### List DLQ Jobs

```python
# List permanently failed jobs (error_count >= 10)
dlq_jobs = await api.list_dlq(limit=100)

for job in dlq_jobs:
    print(f"Job {job['id']}: {job['job_class']}")
    print(f"  Errors: {job['error_count']}")
    print(f"  Last Error: {job['error_message']}")
```

#### Retry from DLQ

```python
# Retry DLQ job (resets error_count to 0)
result = await api.retry_from_dlq(job_id=12345)
print(f"DLQ job {result['original_job_id']} → {result['new_job_id']}")
```

### Complete Example

```python
import asyncio
import asyncpg
from pyjobby.admin_api import AdminAPI


async def manage_jobs():
    # Connect
    conn = await asyncpg.connect(
        database="pyjobby", user="pyjobby", password="password"
    )

    try:
        api = AdminAPI(conn)

        # Check crashed jobs
        crashed = await api.list_jobs(state="crashed", limit=10)
        print(f"Found {len(crashed)} crashed jobs")

        # Retry them
        if crashed:
            job_ids = [j["id"] for j in crashed]
            results = await api.retry_jobs(job_ids)
            success = sum(1 for r in results if r["status"] != "error")
            print(f"Retried {success}/{len(job_ids)} jobs")

        # Clean up old finished jobs
        count = await api.delete_jobs(state="finished", older_than_days=30)
        print(f"Cleaned up {count} old jobs")

    finally:
        await conn.close()


asyncio.run(manage_jobs())
```

---

## 💻 CLI Tools (`pj-admin`)

### Installation

The `pj-admin` command is automatically installed with pyjobby:

```bash
poetry add git+https://github.com/mattsta/pyjobby.git#main
```

Or if developing locally:

```bash
poetry install
```

### Configuration

Create a config file (`pyjobby.conf.py`):

```python
db_params = {
    "database": "pyjobby",
    "user": "pyjobby",
    "password": "password",
    "host": "localhost",
    "port": 5432,
}
```

### Job Commands

#### List Jobs

```bash
# List all jobs
pj-admin jobs list

# Filter by queue
pj-admin jobs list --queue default

# Filter by state
pj-admin jobs list --state crashed

# Filter by job class
pj-admin jobs list --job-class "job.email%"

# Filter by user ID
pj-admin jobs list --uid 123

# Pagination
pj-admin jobs list --limit 25 --offset 50

# JSON output
pj-admin jobs list --json
```

#### Inspect Job

```bash
# View detailed job information
pj-admin jobs inspect 12345

# JSON output
pj-admin jobs inspect 12345 --json
```

#### Retry Jobs

```bash
# Retry single job
pj-admin jobs retry 12345

# Retry multiple jobs
pj-admin jobs retry 12345 12346 12347
```

#### Cancel Jobs

```bash
# Cancel single job
pj-admin jobs cancel 12345

# Cancel multiple jobs
pj-admin jobs cancel 12345 12346 12347
```

#### Delete Jobs

```bash
# Delete single job (with confirmation)
pj-admin jobs delete 12345

# Skip confirmation
pj-admin jobs delete 12345 --force
```

### Queue Commands

#### List Queues

```bash
pj-admin queues list
```

#### Queue Statistics

```bash
# All queues
pj-admin queues stats

# Specific queue
pj-admin queues stats --queue default

# JSON output
pj-admin queues stats --json
```

#### Clear Queue

```bash
# Clear all jobs in queue (with confirmation)
pj-admin queues clear test

# Clear only finished jobs
pj-admin queues clear default --state finished

# Clear old jobs
pj-admin queues clear default --older-than-days 30 --force
```

### Worker Commands

#### List Workers

```bash
# List active workers
pj-admin workers list

# JSON output
pj-admin workers list --json
```

#### Worker Statistics

```bash
pj-admin workers stats
```

### Dead Letter Queue Commands

#### List DLQ

```bash
# List permanently failed jobs
pj-admin dlq list

# Limit results
pj-admin dlq list --limit 50

# JSON output
pj-admin dlq list --json
```

#### Retry from DLQ

```bash
# Retry DLQ job (resets error_count)
pj-admin dlq retry 12345
```

### Metrics Commands

#### System Metrics

```bash
# Last 24 hours (default)
pj-admin metrics

# Custom time range
pj-admin metrics --since-hours 168  # Last week

# Specific queue
pj-admin metrics --queue priority

# JSON output
pj-admin metrics --json
```

### Configuration Options

All commands support:

```bash
# Custom config file
pj-admin --config /path/to/config.py jobs list

# Short form
pj-admin -c /path/to/config.py jobs list
```

### Complete Examples

```bash
# Monitor crashed jobs
pj-admin jobs list --state crashed | grep -c ^
pj-admin jobs list --state crashed --limit 5
pj-admin jobs retry $(pj-admin jobs list --state crashed --json | jq -r '.[].id')

# Clean up old jobs
pj-admin queues clear default --state finished --older-than-days 7 --force

# Check system health
pj-admin queues stats
pj-admin workers stats
pj-admin metrics --since-hours 24

# DLQ management
pj-admin dlq list | head -10
pj-admin dlq retry 12345
```

---

## 🌐 Web Interface (`pj-web`)

### Starting the Web Server

```bash
# Default config (./pyjobby.conf.py)
pj-web

# Custom config
pj-web /path/to/config.py
```

The server runs at **http://localhost:8081/** by default.

### Features

#### Dashboard

- **Real-time statistics** - Auto-refreshing queue stats, worker count, and metrics
- **Queue overview** - See job counts by state for all queues
- **Recent activity** - View last 24 hours of job activity
- **Recent jobs** - Latest jobs with state badges

#### Jobs Page

- **Job list** - View all jobs with filtering
- **State badges** - Color-coded job states (queued, running, finished, crashed)
- **Auto-refresh** - Updates every 5 seconds via htmx

#### API Endpoints

The web interface provides both JSON and HTML responses:

##### Jobs

```bash
# JSON response
curl http://localhost:8081/api/jobs

# HTML fragment (for htmx)
curl http://localhost:8081/api/jobs?format=html

# Filters
curl http://localhost:8081/api/jobs?queue=default&state=crashed&limit=25
```

##### Queues

```bash
# List queues
curl http://localhost:8081/api/queues

# Queue stats
curl http://localhost:8081/api/queues/default/stats
```

##### Workers

```bash
# List active workers
curl http://localhost:8081/api/workers

# Worker stats
curl http://localhost:8081/api/workers/stats
```

##### Metrics

```bash
# System metrics
curl http://localhost:8081/api/metrics

# Custom time range
curl "http://localhost:8081/api/metrics?since_hours=168"

# Specific queue
curl "http://localhost:8081/api/metrics?queue=priority"
```

##### DLQ

```bash
# List DLQ jobs
curl http://localhost:8081/api/dlq

# Retry from DLQ
curl -X POST http://localhost:8081/api/dlq/12345/retry
```

##### Job Operations

```bash
# Get job details
curl http://localhost:8081/api/jobs/12345

# Retry job
curl -X POST http://localhost:8081/api/jobs/12345/retry

# Cancel job
curl -X POST http://localhost:8081/api/jobs/12345/cancel

# Delete job
curl -X DELETE http://localhost:8081/api/jobs/12345
```

### Technology Stack

- **Backend**: aiohttp (async HTTP server)
- **Frontend**: Pure HTML/CSS/JavaScript + htmx
- **Auto-refresh**: htmx polling (5-10 second intervals)
- **Styling**: Modern CSS with system fonts
- **No dependencies**: No React, Vue, or build step required

### Customization

The web interface can be extended by modifying `pyjobby/web_admin.py`:

```python
from pyjobby.web_admin import WebAdminServer

# Custom configuration
server = WebAdminServer(db_params=db_params, host="0.0.0.0", port=8081)


# Add custom routes
@server.app.router.add_get("/custom")
async def custom_route(request):
    return web.Response(text="Custom page")


# Start server
await server.start()
```

---

## 📊 Comparison

| Feature            | Admin API           | CLI (`pj-admin`)   | Web (`pj-web`)           |
| ------------------ | ------------------- | ------------------ | ------------------------ |
| **Platform**       | Python library      | Terminal           | Browser                  |
| **Best for**       | Automation, scripts | DevOps, debugging  | Monitoring, dashboards   |
| **Real-time**      | Manual refresh      | Manual commands    | Auto-refresh (htmx)      |
| **Filtering**      | Full control        | Command-line args  | URL parameters           |
| **Output**         | Python dicts        | Tables, JSON       | HTML, JSON               |
| **Batch ops**      | ✅ Yes              | ✅ Yes (multi-arg) | ⚠️ Limited               |
| **Authentication** | App-level           | None               | None (add reverse proxy) |

---

## 🔐 Security Considerations

**Current Status**: No built-in authentication

**Production Recommendations**:

1. **Network isolation** - Run web interface on private network only
2. **Reverse proxy** - Use nginx with basic auth:

```nginx
location / {
    proxy_pass http://localhost:8081;
    auth_basic "Pyjobby Admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
}
```

3. **SSH tunneling** - Access via SSH port forwarding:

```bash
ssh -L 8081:localhost:8081 production-server
# Then access http://localhost:8081 locally
```

4. **VPN** - Put admin interface behind VPN

**Future**: Built-in authentication may be added in future versions.

---

## 🧪 Testing

All admin tools have comprehensive test coverage:

```bash
# Run admin API tests
poetry run pytest tests/test_admin_api.py -v

# Results: 32/32 tests passing, 94% coverage
```

Test coverage:

- Job management (18 tests)
- Queue management (6 tests)
- Worker management (3 tests)
- Metrics (3 tests)
- Dead Letter Queue (2 tests)

---

## 📝 Summary

Pyjobby now provides three complementary admin interfaces:

1. **Admin API** - For programmatic access and automation
2. **CLI Tools** - For terminal-based management and DevOps workflows
3. **Web Interface** - For browser-based monitoring and dashboards

All three share the same backend implementation, ensuring consistency and reliability.

**Next Steps**:

- Use `pj-admin` for daily operations and debugging
- Use `pj-web` for monitoring and team dashboards
- Use Admin API for custom automation and integration

**Resources**:

- API Reference: `pyjobby/admin_api.py` (docstrings)
- CLI Help: `pj-admin --help`, `pj-admin jobs --help`, etc.
- Web API: http://localhost:8081/api/\* (JSON endpoints)

---

**Version**: 1.3.0
**Last Updated**: 2025-11-18
**Maintainer**: Pyjobby Team
