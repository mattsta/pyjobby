# pyjobby: a python+postgres persistent job queue

You'd think the world would have enough job servers by now, right? Yet, I couldn't find one to fit my needs of having a persistent job queue with python classes as workers without needing to install tons of other weird things.

So, in January 2021 I wrote `pyjobby` and this is all about it.

## 🎯 Goals

- **simplicity**
  - a small, focused core worker loop: [`pyjobby/pj.py`](pyjobby/pj.py)
  - job workers are any python class inheriting from `pyjobby.Job`
    - includes automatic logging of failures and backoff retries
- **modernness**
  - python 3.14 minimum
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
  - optionally listens for web connections to run worker job classes directly (experimental/unsupported)

---

## 🚀 What's New (2024-2025)

Pyjobby has evolved from a simple job queue into a **production-ready job orchestration platform**:

### ⚡ DXE: the Durable Execution Engine

Jobs are durable workflows, not opaque function calls. Inside any job:

```python
from pyjobby import Job


class ProcessOrder(Job):
    async def task(self, order_id: int):
        # Checkpointed steps: a completed step NEVER re-executes, even
        # across retries, worker crashes, or host loss. A job that dies at
        # step 3 resumes at step 3.
        order = await self.step("load", self._load, order_id)
        charge = await self.step("charge", self._charge, order)  # exactly once
        await self.sleep(24 * 3600)  # durable: lives in the DB,
        # holds no worker, survives restarts
        await self.step("follow-up", self._follow_up, order)

        await self.set_event("progress", {"stage": "done"})  # observable state
        reply = await self.recv(topic="approvals", timeout=300)  # durable messaging
        return {"charged": charge["id"]}
```

- **Steps** checkpoint into the database; retries fast-forward past
  completed work and re-execute only what failed (your retry budget applies
  to the *failing* step — an improvement over replay-the-recorded-error
  designs).
- **Durable sleeps** are rows, not processes — a million sleeping jobs cost
  zero workers.
- **Events** (`set_event` / `client.get_event`) publish live job state;
  **mailboxes** (`send` / `recv`) give exactly-once job-to-job messaging.
- **Cancellation reaches running jobs** (~1s via NOTIFY), cancelling the
  task at its next await point; `self.cancelled` for sync loops.
- Every attempt of every job is recorded in `jorb_history`; every
  checkpoint in `jorb_step` (`pj-admin jobs history/steps ID`).
- `run_epoch` fencing guarantees a superseded execution can never write
  results or checkpoints.

### ✅ Live queue controls & worker registry

```bash
pj-admin queues pause imports          # takes effect on the next claim
pj-admin queues limits imports --max-concurrency 8 --rate-limit 100 --rate-period 60
pj-admin workers list                  # real registry: idle workers visible,
                                       # dead workers detected by heartbeat
pj-admin doctor                        # executable health runbook
```

Pause, concurrency caps, and rate limits are enforced *inside* the claim
statement — no worker restarts, no config deploys. Dead workers are
detected by registry heartbeat and their jobs requeued globally by
`pj-monitor` (jobs resume from their last completed step).

### ✅ Client Library

Clean, high-performance Python client with:

- Type hints and auto-completion
- Connection pooling (5-20 connections)
- Batch operations (enqueue 1000+ jobs efficiently)
- Simple API for all pyjobby features
- Context manager support
- Built-in patterns (pipelines, fan-out/fan-in)

```python
from pyjobby import JobClient

async with await JobClient.from_config("./pyjobby.conf.py") as client:
    # Simple job
    job_id = await client.enqueue("myapp.jobs.SendEmail", to="user@example.com")

    # Batch (1000 jobs, 1 database round-trip)
    jobs = [("myapp.jobs.ProcessItem", {"item_id": i}) for i in range(1000)]
    job_ids = await client.enqueue_batch(jobs)

    # Pipeline
    job_ids = await client.create_pipeline(
        [
            ("FetchData", {"source": "api"}),
            ("TransformData", {"format": "json"}),
            ("LoadData", {"destination": "db"}),
        ]
    )
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
pj-admin queues stats -q default

# Worker monitoring
pj-admin workers list
pj-admin workers stats

# DLQ (Dead Letter Queue)
pj-admin dlq list
pj-admin dlq retry 12345

# Metrics (single command, not a group)
pj-admin metrics
pj-admin metrics -q default --since-hours 48 --json

# Schedule management
pj-admin schedule add daily-cleanup myapp.jobs.CleanupJob "0 2 * * *"
pj-admin schedule list
pj-admin schedule history daily-cleanup
pj-admin schedule delete daily-cleanup --yes

# Database schema management
pj-admin db migrate   # install base schema + apply all pending migrations
pj-admin db status    # show applied vs pending migrations
```

`pj-admin` reads its connection from `--config/-c` (a pyjobby conf file) or
`--dsn` (also read from the `PYJOBBY_DSN` environment variable).

**Web Interface**: Auto-refreshing dashboard (`pj-web`)

```bash
pj-web ./pyjobby.conf.py --host 127.0.0.1 --port 8081
# Open http://127.0.0.1:8081
```

Binds to `127.0.0.1` by default and has **no authentication** — if you pass
`--host 0.0.0.0` to expose it, put an authenticating proxy in front of it.

Features:

- Live job monitoring
- Queue statistics
- Worker status
- DLQ management
- Schedule management
- Pure HTML + htmx (no React/Vue bloat)

See [docs/ADMIN_TOOLS.md](docs/ADMIN_TOOLS.md) for complete documentation.

### ✅ Realtime Websocket Dashboard (NEW!)

A live event-stream dashboard server (`pj-ws`) pushes job/queue/worker events
over websockets as they happen:

```bash
pj-ws ./pyjobby.conf.py --host 127.0.0.1 --port 8082
```

Defaults to `127.0.0.1:8082`; the websocket API is unauthenticated, so front
it with a proxy before exposing it. The standalone client page lives at
[`frontend/live-dashboard.html`](frontend/live-dashboard.html).

See [docs/WEBSOCKET_DASHBOARD.md](docs/WEBSOCKET_DASHBOARD.md) for details.

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

# Start the schedule executor (polls every 60s by default)
pj-scheduler --config ./pyjobby.conf.py --poll-interval 60
```

The scheduler is a separate process from `pj` workers: it only enqueues jobs
when schedules come due; regular `pj` workers execute them. It is safe to run
multiple `pj-scheduler` instances (schedules are row-locked while firing and
deadline keys prevent duplicate jobs).

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
# Results are stored by default (save_result=True); opt out per job:
job_id = await client.enqueue(
    "myapp.jobs.FetchData",
    save_result=False,  # Don't persist this job's return value
)

# Pass results between jobs: combine use_result_from with waitfor_job
job_id = await client.enqueue("myapp.jobs.FetchData")
pipeline_job = await client.enqueue(
    "myapp.jobs.ProcessData",
    waitfor_job=job_id,  # Run only after the upstream job finishes
    use_result_from=job_id,  # Worker injects kwargs['upstream_result']
)
```

The upstream result is resolved **at run time** by the worker (not at enqueue
time): when the downstream job executes, its `task()` receives the upstream
job's stored result as `kwargs['upstream_result']`.

**Configurable Retry Strategies**

```python
# Exponential backoff: 1s, 2s, 4s, 8s, 16s...
await client.enqueue(
    "myapp.jobs.ApiCall",
    retry_strategy="exponential",
    max_retries=10,
    initial_retry_delay=1,
)

# Also supports: 'linear', 'fibonacci', 'quadratic', 'fixed' (constant)
```

When a job crashes, the retry **requeues the same row** — the job keeps
its id for life, `error_count` and `run_epoch` advance, and every attempt
(with its error) is recorded in `jorb_history`
(`pj-admin jobs history ID`). After `max_retries` attempts the job lands
in the terminal `crashed` state, which **is** the dead letter queue. DXE
jobs resume from their last completed step on every retry.

**Job Timeout Enforcement**

```python
# Worker-side timeout with automatic retry
await client.enqueue(
    'myapp.jobs.LongTask',
    timeout_seconds=300,
    on_timeout='retry'  # or 'fail'
)

# Background monitor for safety
pj-monitor --dsn postgresql://... --check-interval 10
```

`pj-monitor` is the platform's reaper: it enforces `timeout_at` on running
jobs, requeues in-flight jobs owned by workers whose registry heartbeat
went stale (`--liveness-grace`, default 60s — this is how jobs on a dead
host recover, on any host), and recovers unregistered stale claims
(`--claimed-grace`, default 300s). It connects via `--dsn` (or the
`PYJOBBY_DSN` environment variable) or `--config`.

**DAG Support (Directed Acyclic Graphs)**

```python
from pyjobby import DAGBuilder

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

### Documentation

Start here:

- **[DXE.md](docs/DXE.md)** — the durable execution engine: what a step
  guarantees, how "never run completed work twice" is enforced, fencing, and
  how checkpoints are retained
- **[SCALE.md](docs/SCALE.md)** — measured behaviour at a million jobs/hour:
  what breaks first, what holds, and how to reproduce every number
- [OPERATIONS.md](docs/OPERATIONS.md) — running it: queue controls, retry vs
  re-run, reading the latency numbers

Reference:

- [writing-jobs.md](docs/writing-jobs.md) - What goes inside one job, and which durable primitive to reach for
- [CLIENT_LIBRARY.md](docs/CLIENT_LIBRARY.md) - Client API reference with examples
- [EXAMPLES.md](docs/EXAMPLES.md) - Complete applications, executed by the test suite
- [ADMIN_TOOLS.md](docs/ADMIN_TOOLS.md) - CLI, Web UI, and Admin API
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) - Symptom index: what `doctor` is telling you and what to do
- [deployment-guide.md](docs/deployment-guide.md) - Install, configure, systemd, containers, Kubernetes
- [RECURRING_SCHEDULER.md](docs/RECURRING_SCHEDULER.md) - Cron scheduling guide
- [WEBSOCKET_DASHBOARD.md](docs/WEBSOCKET_DASHBOARD.md) - Realtime websocket dashboard
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - What the moving parts are and why
- [TESTING.md](docs/TESTING.md) - How the suite is structured, and why coverage
  is a diagnostic rather than a target

The full index, with what each document answers, is
[docs/README.md](docs/README.md).

### ✅ Testing

- The test suite runs against a **real PostgreSQL** instance — no mocked database anywhere in the core paths (count and structure: [TESTING.md](docs/TESTING.md))
- Covers core job lifecycle, result storage, retries, timeouts, DAGs, the scheduler, and the admin tools
- Includes direct SQL function testing and Hypothesis property-based tests for retry strategies, timeout enforcement, and DAG algorithms

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
- **priority** - a finishing position, so the **smallest** number is selected first and a job added later can run before jobs already queued. each worker also has a **ceiling** (`pj --max-prio`, default 1000) and selects only `prio <=` it, so past the ceiling a bigger number means "never selected", not "selected later" — the client refuses those enqueues
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

Note on `Job.reschedule()`: if a job calls `reschedule()` while it is
executing, the reschedule **wins** over normal completion — instead of moving
to `finished`, the job returns to `queued` for the requested future run.

### Job Selection

- Using the `pj` script, on startup `--workers` completely independent workers are forked using `multiprocessing.Process` **for each `--queue` named** (default: half the number of CPU cores, per queue). Two queues at `--workers 4` is eight processes; repeating a queue name asks for nothing extra; and no worker is ever started on a queue you did not name
- If web endpoints are enabled, each worker also opens a web server for requests
  - Under linux, each web server on each worker process can receive queries due to in-kernel TCP port load balancing
  - On other platforms, only one of the workers will receive all web requests
- Workers `LISTEN` on postgres `NOTIFY` channels (the triggers are in `pyjobby/sql/schema.sql`), so a newly enqueued job wakes an idle worker **immediately**; the periodic poll (`--check-interval`, default 5 seconds) is only a fallback
- If a worker finds a job, it claims it (state `claimed`), marks it `running` while executing, completes it, then immediately checks the job database for more jobs without entering the delay loop again
  - see query `claim` for logic behind next job selection based on: matching server capability, most urgent job priority first (priority is a finishing position — the **smallest** `prio` is claimed first — and each worker claims only `prio <=` its own ceiling, `pj --max-prio`, default 1000), scheduled run time, and current job state
- If a worker doesn't find an eligible job, it waits for a `NOTIFY` or the next `--check-interval` poll
- On startup, workers recover abandoned same-host jobs by checking pid liveness: jobs claimed by a process on this host that is no longer alive get requeued

---

## 📦 Installation

Requires Python 3.14+. The project uses a standard PEP 621 `pyproject.toml`
and works with both **uv** and **poetry** (both lockfiles are committed).

### As a dependency

```bash
# uv
uv add git+https://github.com/mattsta/pyjobby.git

# poetry
poetry add git+https://github.com/mattsta/pyjobby.git#main

# pip
pip install git+https://github.com/mattsta/pyjobby.git#main
```

### Working on a checkout

```bash
# uv
uv sync

# poetry
poetry install
```

### Database Setup

One step, and the same step whether the database is new or old. `pj-admin db
migrate` installs `pyjobby/sql/schema.sql` on a fresh database (recording
every shipped migration as already contained in it, so none of them runs),
and on an **existing** database applies the numbered files in
`pyjobby/sql/migrations/` it has not recorded in `schema_migrations` — one
transaction per file. All the SQL ships inside the package.

It is idempotent, and safe to run from every host's deploy step at once: it
takes an advisory lock, so one process does the work and the others wait and
find nothing left to do.

```bash
createdb pyjobby
pj-admin --config ./pyjobby.conf.py db migrate

# What is applied, what is pending, and — the line that matters —
# what objects this release needs that the database does not have
pj-admin --config ./pyjobby.conf.py db status

# Confirm the platform agrees (exits 1 on any FAIL)
pj-admin --config ./pyjobby.conf.py doctor
```

For local development, `make setup-db` (which runs
`scripts/setup-test-db.sh`) creates the role and database and then runs the
same migrate command.

---

## 🚦 Quick Start

The public API is exported from the package top level:

```python
from pyjobby import Job, JobClient, JobState, JobSystem, DAGBuilder, RetryStrategy
```

### 1. Define a Job

```python
# jobs/email.py
from pyjobby import Job
from dataclasses import dataclass
import smtplib


@dataclass
class SendEmailJob(Job):
    async def task(self, to: str, subject: str, body: str):
        # Send email
        server = smtplib.SMTP("localhost")
        server.sendmail("noreply@example.com", to, f"Subject: {subject}\n\n{body}")
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
```

(See [`sample.conf.py`](sample.conf.py) for all options, including the
experimental `web_listen` per-worker web endpoints.)

### 3. Start Worker

```bash
# Start job workers (pj is a flat command: no subcommands, no positional args)
# --workers is PER --queue: this is 4 processes, all on `default`.
pj --config ./pyjobby.conf.py --queue default --workers 4

# Two queues, 4 workers on each = 8 processes. No worker ever lands on a
# queue you did not name.
pj --config ./pyjobby.conf.py --queue emails --queue billing --workers 4
```

### 4. Enqueue Jobs

**Using Client Library (Recommended):**

```python
from pyjobby import JobClient
import asyncio


async def main():
    async with await JobClient.from_config("./pyjobby.conf.py") as client:
        job_id = await client.enqueue(
            "jobs.email.SendEmailJob",
            to="user@example.com",
            subject="Welcome!",
            body="Thanks for signing up!",
        )
        print(f"Job enqueued: {job_id}")


asyncio.run(main())
```

**Using Direct SQL:**

```python
import asyncpg
import json


async def enqueue_job():
    conn = await asyncpg.connect(database="pyjobby", user="postgres", host="localhost")

    job_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, prio, state)
        VALUES ($1, $2::jsonb, $3, $4, $5)
        RETURNING id
    """,
        "jobs.email.SendEmailJob",
        json.dumps({"to": "user@example.com", "subject": "Hi", "body": "Hello!"}),
        "default",
        100,
        "queued",
    )

    await conn.close()
    return job_id
```

---

## 🎨 Common Patterns

### Simple Job

```python
await client.enqueue("MyJob", arg="value")
```

### Scheduled Job

```python
from datetime import datetime, timedelta

await client.enqueue(
    "MyJob", run_after=datetime.now() + timedelta(hours=1), arg="value"
)
```

### Job Pipeline (Sequential)

```python
# A → B → C
job_ids = await client.create_pipeline(
    [
        ("FetchData", {"source": "api"}),
        ("TransformData", {"format": "json"}),
        ("LoadData", {"destination": "db"}),
    ]
)
```

### Fan-Out / Fan-In (Parallel + Aggregate)

```python
# Process 1000 items in parallel
items = [{"item_id": i} for i in range(1000)]
job_ids, group_id = await client.create_fan_out("ProcessItem", items)

# Aggregate results after all complete
summary_job = await client.enqueue("SummarizeResults", waitfor_group=group_id)
```

### Batch Enqueueing

```python
# Efficiently enqueue 10,000 jobs
jobs = [("ProcessItem", {"id": i}) for i in range(10000)]
job_ids = await client.enqueue_batch(jobs)
```

### Idempotent Jobs (Prevent Duplicates)

```python
await client.enqueue(
    "ProcessPayment",
    deadline_key=f"payment:{payment_id}",
    payment_id=payment_id,
    amount=99.99,
)
```

### Priority

```python
# Priority is a FINISHING POSITION, not a rating: the smaller number runs
# sooner, the way `priority=1` means "first" in a race.
await client.enqueue("UrgentTask", priority=10)  # runs first
await client.enqueue("NormalTask", priority=100)  # the default
await client.enqueue("BackgroundTask", priority=500)  # runs last

# Each worker claims only prio <= its ceiling (`pj --max-prio`, default
# 1000), so past the ceiling a bigger number means "never", not "later".
# The client refuses those enqueues instead of writing an unclaimable row:
await client.enqueue("Whenever", priority=5000)  # ValueError, nothing written

# Really running less-urgent workers? Declare the ceiling on both sides:
#   pj --queue backfill --max-prio 5000
#   JobClient(pool, prio_ceiling=5000)
```

### Capability-Based Routing

```python
# Route to GPU workers
await client.enqueue("TrainModel", capability="gpu", model="resnet50")
```

---

## 📚 Documentation

- **[Client Library Guide](docs/CLIENT_LIBRARY.md)** - Complete API reference, examples, performance tips
- **[Real-World Examples](docs/EXAMPLES.md)** - Web apps, ETL pipelines, image processing, email campaigns, etc.
- **[Admin Tools](docs/ADMIN_TOOLS.md)** - CLI, Web UI, and Admin API documentation
- **[Recurring Scheduler](docs/RECURRING_SCHEDULER.md)** - Cron-based scheduling with safety features
- **[Architecture](docs/ARCHITECTURE.md)** - Components, the life of a job, and the design principle behind the write path
- **[Websocket Dashboard](docs/WEBSOCKET_DASHBOARD.md)** - Realtime event-stream dashboard (`pj-ws`)

---

## ⚡ Performance Notes

- Batch enqueueing uses a single `UNNEST` insert, so thousands of jobs are one database round-trip
- Enqueue triggers postgres `NOTIFY`, so idle workers pick up new jobs immediately instead of waiting for the next poll
- The client pools 5-20 connections by default
- Throughput is bounded by your PostgreSQL instance and job duration; every job state change is a committed update

---

## 📝 Schema Notes

- **Every** timestamp column in the schema is `TIMESTAMPTZ`, in every table — `created`, `updated`, `run_after`, `claimed_at`, `started`, `finished`, `timeout_at`, and all the schedule, step, history, mailbox, DAG and worker timestamps. There is no longer a naive/aware split to reason about when writing raw SQL.
- Timestamp comparisons therefore happen in UTC regardless of the connecting session's `TimeZone`; a cron schedule's own timezone lives in `jorb_schedule.timezone` and is applied when computing `next_run`, not by the column type.

---

## 🛠️ Current Limitations

- Python / PostgreSQL only (no other languages/databases)
- Every job state change hits the DB as a committed update (WAL pollution for high volume servers)
- Using `FOR UPDATE SKIP LOCKED` atomic update primitive (reliable but not highest-performing)
  - We use postgres `LISTEN`/`NOTIFY` only as a wakeup signal for idle workers; we've avoided the in-memory-table pub/sub designs that [some projects use](https://github.com/que-rb/que/blob/master/lib/que/migrations/4/up.sql) for higher performance, preferring simplicity

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
