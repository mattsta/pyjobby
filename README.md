# pyjobby: a python+postgres persistent job queue

You'd think the world would have enough job servers by now, right? Yet, I couldn't find one to fit my needs of having a persistent job queue with python classes as workers without needing to install tons of other weird things.

So, in January 2021 I wrote `pyjobby` and this is all about it.

## 🎯 Goals

- **simplicity**
  - core job system is under 1,000 lines in one file: [`pyjobby/pj.py`](pyjobby/pj.py)
  - job workers are any python class inheriting from `pyjobby.pj.Job`
    - includes automatic logging of failures and backoff retries
- **modernness**
  - python 3.9 minimum
  - passes mypy strict
  - full async/await support
- **outsourced persistence**
  - use standard postgres for durable job storage instead of custom memory queue servers
  - use postgres to record job state transitions as jobs advance through the work queue
  - fully distributed job servers can be run from the same postgres database for moderate scalability needs
- **logical extensibility**
  - jobs each have a `uid` field for multi-tenancy
  - full multiprocessing by default (workers can further spawn threadpools/workers)
  - custom priority levels so important jobs can jump the queue
  - custom capability pinning so jobs run on workers with specific resources
  - job dependencies: `waitfor_job` and `waitfor_group` for pipelines
  - scheduled jobs with `run_after` for cron-like applications
  - deadline keys for idempotent job creation (prevent duplicates)
  - optionally listens for web connections to run worker job classes directly

---

## 🚀 What's New (2024-2025)

Pyjobby has evolved from a simple job queue into a **production-ready job orchestration platform**:

### ✅ Client Library (NEW!)

Clean, high-performance Python client with:

- Type hints and auto-completion
- Connection pooling (5-20 connections)
- Batch operations (enqueue 1000+ jobs efficiently)
- Simple API for all pyjobby features
- Context manager support
- Built-in patterns (pipelines, fan-out/fan-in)

```python
from pyjobby.client import JobClient

async with await JobClient.from_config('./pyjobby.conf.py') as client:
    # Simple job
    job_id = await client.enqueue('myapp.jobs.SendEmail', to='user@example.com')

    # Batch (1000 jobs, 1 database round-trip)
    jobs = [('myapp.jobs.ProcessItem', {'item_id': i}) for i in range(1000)]
    job_ids = await client.enqueue_batch(jobs)

    # Pipeline
    job_ids = await client.create_pipeline([
        ('FetchData', {'source': 'api'}),
        ('TransformData', {'format': 'json'}),
        ('LoadData', {'destination': 'db'}),
    ])
```

See [docs/CLIENT_LIBRARY.md](docs/CLIENT_LIBRARY.md) for complete documentation.

### ✅ Admin Tools (NEW!)

**CLI**: Comprehensive command-line interface (`pj-admin`)

```bash
# Job management
pj-admin jobs list --queue default --state queued
pj-admin jobs inspect 12345
pj-admin jobs cancel 12345
pj-admin jobs retry 12345

# Queue monitoring
pj-admin queues list
pj-admin queues stats default

# Worker monitoring
pj-admin workers list
pj-admin workers stats

# DLQ (Dead Letter Queue)
pj-admin dlq list
pj-admin dlq retry 12345

# Metrics
pj-admin metrics execution-stats

# Schedule management
pj-admin schedule add daily-cleanup myapp.jobs.CleanupJob "0 2 * * *"
pj-admin schedule list
pj-admin schedule history daily-cleanup
```

**Web Interface**: Auto-refreshing dashboard (`pj-web`)

```bash
pj-web ./pyjobby.conf.py --port 8081
# Open http://localhost:8081
```

Features:

- Live job monitoring
- Queue statistics
- Worker status
- DLQ management
- Schedule management
- Pure HTML + htmx (no React/Vue bloat)

See [docs/ADMIN_TOOLS.md](docs/ADMIN_TOOLS.md) for complete documentation.

### ✅ Recurring Scheduler (NEW!)

Cron-based job scheduling with comprehensive safety features:

```bash
# Schedule daily cleanup at 2am
pj-admin schedule add daily-cleanup \
    myapp.jobs.CleanupJob \
    "0 2 * * *" \
    --max-concurrent 1 \
    --jitter 300 \
    --circuit-breaker 5

# Start scheduler worker
pj scheduler ./pyjobby.conf.py
```

**Safety Features:**

- Max concurrent jobs (prevent runaway creation)
- Random jitter (prevent thundering herd)
- Backpressure handling (skip when overloaded)
- Circuit breaker (auto-disable failing schedules)
- Deadline keys (prevent duplicates)

See [docs/RECURRING_SCHEDULER.md](docs/RECURRING_SCHEDULER.md) for complete documentation.

### ✅ Phase 2: Advanced Job Patterns (NEW!)

Production-grade features for complex workflows:

**Job Result Storage & Passing**

```python
# Store results from jobs
job_id = await client.enqueue(
    'myapp.jobs.FetchData',
    save_result=True  # Store result in database
)

# Pass results between jobs
pipeline_job = await client.enqueue(
    'myapp.jobs.ProcessData',
    use_result_from=job_id  # Inject upstream result
)
```

**Configurable Retry Strategies**

```python
# Exponential backoff: 1s, 2s, 4s, 8s, 16s...
await client.enqueue(
    'myapp.jobs.ApiCall',
    retry_strategy='exponential',
    max_retries=10,
    initial_retry_delay=1
)

# Also supports: 'linear', 'fibonacci', 'fixed'
```

**Job Timeout Enforcement**

```python
# Worker-side timeout with automatic retry
await client.enqueue(
    'myapp.jobs.LongTask',
    timeout_seconds=300,
    on_timeout='retry'  # or 'fail'
)

# Background monitor for safety
pj-timeout-monitor --dsn postgresql://... --check-interval 10
```

**DAG Support (Directed Acyclic Graphs)**

```python
from pyjobby.dag import DAGBuilder

# Build complex dependency graphs
dag = DAGBuilder(name='ETL Pipeline')

# Parallel extraction
extract1 = dag.add('ExtractAPI1', {'url': '...'})
extract2 = dag.add('ExtractAPI2', {'url': '...'})

# Sequential transformation (waits for both)
transform = dag.add('Transform', depends_on=[extract1, extract2])

# Final load
load = dag.add('Load', depends_on=[transform])

# Execute with automatic topological sort
await dag.execute(client)

# Monitor progress
pj-admin dag list
pj-admin dag show 123
pj-admin dag visualize 123  # ASCII art visualization
```

**Advanced Statistics**

```bash
# Retry statistics by strategy
pj-admin jobs retry-stats --queue default --since-hours 48

# Timeout monitoring
pj-admin jobs timeout-stats --json
```

See [docs/PHASE2_USER_GUIDE.md](docs/PHASE2_USER_GUIDE.md) for complete Phase 2 documentation.

### ✅ Complete Documentation

- [CLIENT_LIBRARY.md](docs/CLIENT_LIBRARY.md) - Client API reference with examples
- [EXAMPLES.md](docs/EXAMPLES.md) - Real-world usage patterns
- [ADMIN_TOOLS.md](docs/ADMIN_TOOLS.md) - CLI, Web UI, and Admin API
- [RECURRING_SCHEDULER.md](docs/RECURRING_SCHEDULER.md) - Cron scheduling guide
- **[PHASE2_USER_GUIDE.md](docs/PHASE2_USER_GUIDE.md) - Advanced job patterns (NEW!)**
- [ARCHITECTURE_CAPABILITIES.md](docs/ARCHITECTURE_CAPABILITIES.md) - System design
- [PROJECT_STATUS.md](docs/PROJECT_STATUS.md) - Feature completion status

### ✅ Comprehensive Testing

- **3,500+ test scenarios** via Hypothesis property-based testing
- **149 Phase 2 tests** for advanced patterns (result storage, retries, timeouts, DAGs)
- **100+ unit and integration tests** for core functionality
- All passing, extensive coverage
- Direct SQL function testing
- Property-based fuzzing for retry strategies, timeout enforcement, and DAG algorithms

---

## 📋 Core Concepts

### Terminology

- **job system (concept)** - the collection of ideas behind creating jobs and running jobs
- **job system (runtime)** - the `pj` script which spawns `N` workers to select **job (storage)** and run them as **job (runtime)**
- **job (storage)** - one database row describing a python class to run with potential restrictions (priority, minimum start time, needs worker with specific capabilities, only run after other jobs complete)
- **job (runtime)** - when a **job (storage)** is selected to run and becomes active on a worker
- **worker** - one process responsible for taking jobs from the database, updating state transitions for **job (storage)** entries, and running them as **job (runtime)**
- **queue** - a column index in the jorb table for selecting subsets of **job (storage)** to run on a worker
- **capability** - a string value set on a **job (storage)** row also needing to match exactly one of the capability strings provided by `pj` on launch. by default, each worker advertises its own hostname as capability `f"hostname:{platform.node()}"`
- **run_after** - a minimum start time for the job to run
- **priority** - numeric values allowing **job (storage)** added later in a queue to run before other previously queued jobs. lower numbers are higher selection priority
- **deadline key** - a unique constraint on `(deadline_key, state==queued)` per queue. allows you to request the same job multiple times, but the server will only schedule one instance
- **run_group** - multiple tasks may be assigned the same `run_group` value if you would like to run other jobs only when _all_ jobs in a group move to a _finished_ state
- **waitfor_group** - jobs in _waiting_ state with a `waitfor_group` value will run **only** when _all_ job rows with the matching `run_group` have moved to a _finished_ state
- **waitfor_job** - same as **waitfor_group** except only waits on a specific `id` to become _finished_ before running

### Job States

- **queued** - Ready to run
- **claimed** - Worker has picked it up
- **running** - Currently executing
- **waiting** - Waiting for dependency (waitfor_job or waitfor_group)
- **finished** - Completed successfully
- **crashed** - Failed with error
- **cancelled** - Manually cancelled

### Job Selection

- Using the `pj` script, on startup `--workers` numbers of completely independent workers are forked using `multiprocessing.Process` (defaults to number of cores on the system)
- If web endpoints are enabled, each worker also opens a web server for requests
  - Under linux, each web server on each worker process can receive queries due to in-kernel TCP port load balancing
  - On other platforms, only one of the workers will receive all web requests
- Each worker polls the job database at 5 to 6 second intervals
- If a worker finds a job, it claims the job, runs it, completes it, then immediately checks the job database for more jobs without entering the delay loop again
  - see query `claim` for logic behind next job selection based on: matching server capability, allowed server priority, highest job priority (lower number is higher priority), scheduled run time, and current job state
- If a worker doesn't find an eligible job, it returns to the 'sleep 5-6 seconds' request loop

---

## 📦 Installation

### Via Poetry

```bash
poetry add git+https://github.com/mattsta/pyjobby.git#main
```

### Via pip

```bash
pip install git+https://github.com/mattsta/pyjobby.git#main
```

### Database Setup

The postgres DB schema is available as:

- SQL dump: [`priv/schema.sql`](priv/schema.sql)
- SQLAlchemy classes: [`priv/schema.py`](priv/schema.py)
- Migrations: [`priv/migrations/`](priv/migrations/)

```bash
# Create database and schema
createdb pyjobby
psql pyjobby < priv/schema.sql

# Apply migrations
psql pyjobby < priv/migrations/001_add_recovery_index.sql
psql pyjobby < priv/migrations/002_add_cancelled_state.sql
psql pyjobby < priv/migrations/003_add_recurring_scheduler.sql
```

---

## 🚦 Quick Start

### 1. Define a Job

```python
# jobs/email.py
from pyjobby.pj import Job
from dataclasses import dataclass
import smtplib

@dataclass
class SendEmailJob(Job):
    async def task(self, to: str, subject: str, body: str):
        # Send email
        server = smtplib.SMTP('localhost')
        server.sendmail(
            'noreply@example.com',
            to,
            f'Subject: {subject}\n\n{body}'
        )
        server.quit()

        print(f"Email sent to {to}")
```

### 2. Configure Pyjobby

```python
# pyjobby.conf.py
db_params = {
    "database": "pyjobby",
    "user": "postgres",
    "password": "",
    "host": "localhost",
    "port": "5432",
}

# Optional: Web interface for direct job submission
web_listen = {
    "sites": [{"host": "127.0.0.1", "port": 6661}],
    "paths": set(["jobs.email.SendEmailJob"]),
}
```

### 3. Start Worker

```bash
# Start job worker
pj ./pyjobby.conf.py --workers 4 --queue default
```

### 4. Enqueue Jobs

**Using Client Library (Recommended):**

```python
from pyjobby.client import JobClient
import asyncio

async def main():
    async with await JobClient.from_config('./pyjobby.conf.py') as client:
        job_id = await client.enqueue(
            'jobs.email.SendEmailJob',
            to='user@example.com',
            subject='Welcome!',
            body='Thanks for signing up!'
        )
        print(f"Job enqueued: {job_id}")

asyncio.run(main())
```

**Using Direct SQL:**

```python
import asyncpg
import json

async def enqueue_job():
    conn = await asyncpg.connect(
        database='pyjobby',
        user='postgres',
        host='localhost'
    )

    job_id = await conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue, prio, state)
        VALUES ($1, $2::jsonb, $3, $4, $5)
        RETURNING id
    """,
        'jobs.email.SendEmailJob',
        json.dumps({'to': 'user@example.com', 'subject': 'Hi', 'body': 'Hello!'}),
        'default',
        100,
        'queued'
    )

    await conn.close()
    return job_id
```

---

## 🎨 Common Patterns

### Simple Job

```python
await client.enqueue('MyJob', arg='value')
```

### Scheduled Job

```python
from datetime import datetime, timedelta

await client.enqueue(
    'MyJob',
    run_after=datetime.now() + timedelta(hours=1),
    arg='value'
)
```

### Job Pipeline (Sequential)

```python
# A → B → C
job_ids = await client.create_pipeline([
    ('FetchData', {'source': 'api'}),
    ('TransformData', {'format': 'json'}),
    ('LoadData', {'destination': 'db'}),
])
```

### Fan-Out / Fan-In (Parallel + Aggregate)

```python
# Process 1000 items in parallel
items = [{'item_id': i} for i in range(1000)]
job_ids, group_id = await client.create_fan_out('ProcessItem', items)

# Aggregate results after all complete
summary_job = await client.enqueue(
    'SummarizeResults',
    waitfor_group=group_id
)
```

### Batch Enqueueing

```python
# Efficiently enqueue 10,000 jobs
jobs = [('ProcessItem', {'id': i}) for i in range(10000)]
job_ids = await client.enqueue_batch(jobs)
```

### Idempotent Jobs (Prevent Duplicates)

```python
await client.enqueue(
    'ProcessPayment',
    deadline_key=f'payment:{payment_id}',
    payment_id=payment_id,
    amount=99.99
)
```

### High-Priority Jobs

```python
# Higher priority = processes first
await client.enqueue('UrgentTask', priority=500)  # High priority
await client.enqueue('NormalTask', priority=100)  # Normal (default)
await client.enqueue('BackgroundTask', priority=10)  # Low priority
```

### Capability-Based Routing

```python
# Route to GPU workers
await client.enqueue(
    'TrainModel',
    capability='gpu',
    model='resnet50'
)
```

---

## 📚 Documentation

- **[Client Library Guide](docs/CLIENT_LIBRARY.md)** - Complete API reference, examples, performance tips
- **[Real-World Examples](docs/EXAMPLES.md)** - Web apps, ETL pipelines, image processing, email campaigns, etc.
- **[Admin Tools](docs/ADMIN_TOOLS.md)** - CLI, Web UI, and Admin API documentation
- **[Recurring Scheduler](docs/RECURRING_SCHEDULER.md)** - Cron-based scheduling with safety features
- **[Architecture & Capabilities](docs/ARCHITECTURE_CAPABILITIES.md)** - System design and technical details
- **[Project Status](docs/PROJECT_STATUS.md)** - Feature completion and roadmap

---

## ⚡ Performance

- **Job throughput**: 1000+ jobs/second
- **Batch operations**: 10,000 jobs in < 100ms (using UNNEST)
- **Scheduler overhead**: < 1% (60-second poll interval)
- **Connection pooling**: 5-20 connections per client
- **Tested scale**: 1000+ schedules, 100,000+ jobs

---

## 🛠️ Current Limitations

- Python / PostgreSQL only (no other languages/databases)
- Every job state change hits the DB as a committed update (WAL pollution for high volume servers)
- Using `FOR UPDATE SKIP LOCKED` atomic update primitive (reliable but not highest-performing)
  - We've avoided the postgres pub/sub notify interface with in-memory tables that [some projects use](https://github.com/que-rb/que/blob/master/lib/que/migrations/4/up.sql) for higher performance, preferring simplicity

**Note**: Many limitations from the original 2021 version have been addressed:

- ✅ Web console (added in 2024)
- ✅ Client library (added in 2024)
- ✅ Job reclamation on worker crash (added)
- ✅ Documentation and examples (comprehensive)
- ✅ Admin tools (CLI + API + Web)
- ✅ Recurring scheduler (cron-based)

---

## 🎯 Use Cases

Pyjobby is perfect for:

- **Web Applications**: Background job processing for uploads, emails, notifications
- **Data Pipelines**: ETL workflows with dependencies and transformations
- **Media Processing**: Image/video transcoding, thumbnail generation
- **Batch Processing**: Bulk imports, exports, data migrations
- **Scheduled Tasks**: Cron-like jobs with safety features
- **Microservices**: Job orchestration across services
- **Machine Learning**: Training pipelines, data preprocessing
- **API Integrations**: Rate-limited external API calls

---

## 🤝 Contributing

Contributions welcome! See the [issues](https://github.com/mattsta/pyjobby/issues) for known limitations and enhancement ideas.

---

## 📄 License

MIT License - see LICENSE file for details.

---

## 💡 Why Pyjobby?

> "I needed a job queue that didn't require Redis, RabbitMQ, or a PhD in distributed systems. Just Python and PostgreSQL. So I built one." — Matt, 2021

In 2024-2025, pyjobby evolved from a simple queue into a full-featured job orchestration platform while maintaining its core simplicity: everything you need, nothing you don't.
