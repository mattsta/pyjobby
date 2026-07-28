# Pyjobby Client Library

Clean, well-encapsulated Python client for job submission and management with full type hint support, connection pooling, and high-performance batch operations.

## Features

- 🚀 **High Performance**: Connection pooling (5-20 connections), batch operations via PostgreSQL UNNEST
- 🎯 **Type Hints**: Full type annotations for IDE auto-completion and type checking
- 🔧 **Easy to Use**: Context manager support, simple API, minimal configuration
- 📦 **Complete**: Supports all pyjobby features (scheduling, pipelines, priorities, capabilities, deadlines)
- ⚡ **Batch Operations**: Efficiently enqueue 1000+ jobs with single database roundtrip
- 🎨 **Patterns**: Built-in support for pipelines, fan-out/fan-in, and job dependencies

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
- [API Reference](#api-reference)
- [Common Patterns](#common-patterns)
- [State Machines](#state-machines)
- [Advanced Usage](#advanced-usage)
- [Performance Tips](#performance-tips)

---

## Installation

```bash
pip install pyjobby
```

Or with Poetry:

```bash
poetry add pyjobby
```

---

## Quick Start

### Simple Job Submission

```python
import asyncio
from pyjobby.client import JobClient


async def main():
    # Connect using context manager (recommended)
    async with await JobClient.create(
        host="localhost", database="pyjobby", user="postgres"
    ) as client:
        # Enqueue a simple job
        job_id = await client.enqueue(
            "myapp.jobs.SendEmail",
            to="user@example.com",
            subject="Welcome!",
            body="Thanks for signing up!",
        )

        print(f"Job enqueued: {job_id}")


asyncio.run(main())
```

### Using Configuration File

```python
async with await JobClient.from_config("./pyjobby.conf.py") as client:
    job_id = await client.enqueue("myapp.jobs.ProcessData", data_id=123)
```

---

## Core Concepts

### JobClient

The main interface for interacting with the job queue. Manages connection pooling and provides methods for job submission and management.

**Creation Methods:**

```python
# Method 1: Direct connection parameters
client = await JobClient.create(
    host="localhost",
    port=5432,
    database="pyjobby",
    user="postgres",
    password="secret",
    min_size=5,  # Minimum pool size
    max_size=20,  # Maximum pool size
)

# Method 2: From configuration file
client = await JobClient.from_config("./pyjobby.conf.py")

# Method 3: From existing pool
import asyncpg

pool = await asyncpg.create_pool(...)
client = JobClient(pool)

# All three take prio_ceiling=, the priority ceiling THIS deployment's
# workers run with (`pj --max-prio`, default 1000). It is a declaration,
# not something the client can observe; see "Priority, and the ceiling".
client = JobClient(pool, prio_ceiling=5000)

# Always close when done
await client.close()

# Or use context manager (auto-closes)
async with await JobClient.create(...) as client:
    # Use client
    pass  # Automatically closed here
```

### Job States

Jobs flow through the following states:

- **queued** - Ready to run
- **claimed** - Worker has picked it up
- **running** - Currently executing
- **waiting** - Waiting for dependency (waitfor_job or waitfor_group)
- **finished** - Completed successfully
- **crashed** - Failed with error
- **cancelled** - Manually cancelled

---

## API Reference

### One-call workflows

```python
# Enqueue and wait — request/response in one call. Raises what
# wait_for_result raises (JobFailedError, JobCancelledError, TimeoutError).
report = await client.run("myapp.jobs.Report", day="mon", timeout=60)

# Cancel and wait for the cancellation to LAND. 'cancel_requested' is a
# promise, not an outcome: this returns the terminal state — 'cancelled',
# or 'finished'/'crashed' when the job outran the cancel.
final = await client.cancel_and_wait(job_id, timeout=30)

# Await a whole fan-out. Returns the member count when every job in the
# group finished; raises if a member crashed/was cancelled (the group can
# then never finish) or the group has no members.
group_id, job_ids = await client.create_fan_out("myapp.jobs.Resize", items)
await client.wait_for_group(group_id, timeout=600)
```

A payload key named like an option (`queue`, `priority`, ...) can always be
delivered by passing the payload as a dict instead of splatting it:
`enqueue("j.Job", job_kwargs={"queue": "the-jobs-own-argument"})` keeps
payload and options in separate namespaces.

Errors follow one contract: `get_*` methods are snapshots and return `None`
for a missing row; `wait_*`/`run` methods raise — `JobError` (with a
`.job_id` attribute) and its subclasses `JobFailedError`/`JobCancelledError`
for job outcomes, `TimeoutError` when your `timeout=` elapsed (never an
outcome), and plain `ValueError` for invalid arguments caught before any
database work.

### Job Submission

#### `enqueue(job_class, **kwargs)`

Enqueue a single job.

**Parameters:**

- `job_class` (str): Full Python class path (e.g., `'myapp.jobs.SendEmail'`)
- `queue` (str): Queue name (default: `'default'`)
- `priority` (int): Priority as a **finishing position** — a *smaller*
  number runs *sooner*, the way `priority=1` means "first" in a race
  (default: 100). Workers claim in ascending `prio` order and only take
  jobs at or below their ceiling (`pj --max-prio`, default 1000), so a
  number above the ceiling is not "run last", it is "never run". This
  client **refuses** such an enqueue with `ValueError` rather than writing
  a row nothing would claim — see
  [Priority, and the ceiling](#5-priority-and-the-ceiling).
- `run_after` (datetime): When to run (default: now)
- `capability` (str): Required worker capability (default: None)
- `uid` (int): User/tenant ID for multi-tenancy (default: None)
- `run_group` (int): Group ID for tracking related jobs (default: None)
- `waitfor_job` (int): Wait for this job ID to complete (default: None)
- `waitfor_group` (int): Wait for all jobs in this group (default: None)
- `deadline_key` (str): Idempotency key to prevent duplicates (default: None)
- `admin_data` (dict): Metadata for tracking (default: None)
- `tags` (dict): Your own labels — customer, region, batch — that you can
  filter jobs by later (default: None). See [Job Tags](#8-job-tags).
- `save_result` (bool): Store the job's return value (default: True)
- `use_result_from` (int): Inject that job's result as `upstream_result`
  (default: None). Pair with `waitfor_job` so it has finished first.
- `retry_strategy` (str): `'exponential'`, `'linear'`, `'fibonacci'` or
  `'fixed'` (default: `'exponential'`)
- `max_retries` (int): Attempts before the dead-letter state (default: 10)
- `initial_retry_delay` / `max_retry_delay` (int): Backoff floor and ceiling
  in seconds (defaults: 1 and 3600)
- `timeout_seconds` (int): This job's deadline, overriding the job class's
  `timeout` attribute and the worker's `--default-timeout`. `0` means no
  deadline at all; `None` (the default) defers to the class, then the worker.
- `on_timeout` (str): What a blown deadline means — `'retry'` (default) or
  `'fail'`, terminal on the first overrun. It governs **whichever** of those
  three deadlines binds, so passing it without `timeout_seconds` is
  meaningful: the class attribute and the worker default are deadlines too,
  and neither is visible from the enqueue site. Any other value raises
  `ValueError`.
- `**kwargs`: Job arguments passed to job class

**Returns:** Job ID (int)

**Examples:**

```python
# Simple job
job_id = await client.enqueue("myapp.jobs.SendEmail", to="user@example.com")

# Scheduled job (run in 1 hour)
from datetime import UTC, datetime, timedelta

job_id = await client.enqueue(
    "myapp.jobs.DailyReport",
    run_after=datetime.now(UTC) + timedelta(hours=1),
    report_type="sales",
)

# High priority job — LOWER is more urgent
job_id = await client.enqueue(
    "myapp.jobs.UrgentTask",
    priority=10,  # ahead of the default 100
    task_id=12345,
)

# Job requiring GPU capability
job_id = await client.enqueue(
    "myapp.jobs.TrainModel", capability="gpu", model_type="resnet50", dataset="imagenet"
)

# Idempotent job (safe to retry)
job_id = await client.enqueue(
    "myapp.jobs.ProcessPayment",
    deadline_key=f"payment:{payment_id}",
    payment_id=payment_id,
    amount=99.99,
)

# Multi-tenant job
job_id = await client.enqueue(
    "myapp.jobs.GenerateReport",
    uid=user.id,  # Tenant ID
    report_type="monthly",
)

# Job with metadata tracking
job_id = await client.enqueue(
    "myapp.jobs.ProcessData",
    admin_data={
        "request_id": request_id,
        "user_agent": request.headers.get("User-Agent"),
        "ip_address": request.remote_addr,
    },
    data_id=123,
)

# Job labelled for later filtering
job_id = await client.enqueue(
    "myapp.jobs.ProcessData",
    tags={"customer": "acme", "region": "eu-west-1", "batch": 42},
    data_id=123,
)
```

#### `enqueue_batch(jobs, **options)`

Enqueue multiple jobs in one INSERT, with the **same option set as
`enqueue()`** — retry strategy, timeout policy, tags, `deadline_key`,
`capability`, dependencies. A batched job means exactly what the same
single enqueue means.

**Parameters:**

- `jobs`: a list of `(job_class, kwargs)` tuples, or
  `(job_class, kwargs, per_job_options)` — the third element is a dict of
  `enqueue()` options applying to that job only, layered over the shared
  ones. Per-job options are how a batch carries a per-item `deadline_key`,
  `tags`, or `waitfor_job`.
- `**options`: any `enqueue()` option (`queue`, `priority`, `run_after`,
  `run_group`, `tags`, `max_retries`, `timeout_seconds`, ...), applied to
  every job in the batch.

Payload and options never collide: the `kwargs` dict is delivered to the
job verbatim, even if it contains keys named like options.

**Returns:** List of job IDs, in the order given

**Examples:**

```python
# Enqueue 1000 jobs efficiently, each with its own idempotency key
jobs = [
    ("myapp.jobs.ProcessItem", {"item_id": i}, {"deadline_key": f"item:{i}"})
    for i in range(1000)
]
job_ids = await client.enqueue_batch(jobs, queue="processing", max_retries=5)

# Batch with scheduling (always timezone-aware datetimes)
from datetime import UTC, datetime, timedelta

jobs = [("myapp.jobs.SendReminder", {"user_id": user_id}) for user_id in user_ids]
job_ids = await client.enqueue_batch(
    jobs, run_after=datetime.now(UTC) + timedelta(hours=24), queue="notifications"
)

# Batch with group tracking
job_ids = await client.enqueue_batch(
    jobs,
    run_group=123,  # All jobs in same group
    priority=200,
)
```

### Job Management

#### `get_job(job_id)`

Get job information.

```python
job = await client.get_job(12345)
if job:
    print(f"Job {job.id}: {job.state}")
    print(f"  Class: {job.job_class}")
    print(f"  Queue: {job.queue}")
    print(f"  Priority: {job.priority}")
    print(f"  Created: {job.created}")
else:
    print("Job not found")
```

#### `cancel_job(job_id)`

Cancel a queued or waiting job.

```python
if await client.cancel_job(12345):
    print("Job cancelled")
else:
    print("Job not found or already running")
```

#### `retry_job(job_id)`

Retry a failed or crashed job (creates new job).

```python
new_job_id = await client.retry_job(12345)
if new_job_id:
    print(f"Retry job created: {new_job_id}")
```

### Queue Operations

#### `queue_depth(queue='default')`

Get number of queued jobs.

```python
depth = await client.queue_depth("emails")
print(f"Queue has {depth} jobs waiting")
```

#### `queue_stats(queue='default')`

Get statistics for a queue.

```python
stats = await client.queue_stats("emails")
print(f"Queued: {stats['queued']}")
print(f"Running: {stats['running']}")
print(f"Finished: {stats['finished']}")
print(f"Crashed: {stats['crashed']}")
```

### Health Check

#### `health_check()`

Check database connection health.

```python
if await client.health_check():
    print("Database healthy")
else:
    print("Database connection failed")
```

---

## Common Patterns

### 1. Job Pipeline (Sequential Processing)

Process data through multiple stages where each step waits for the previous.

```python
# Manual pipeline
job1 = await client.enqueue("myapp.jobs.FetchData", source="api")
job2 = await client.enqueue("myapp.jobs.TransformData", waitfor_job=job1, format="json")
job3 = await client.enqueue(
    "myapp.jobs.LoadData", waitfor_job=job2, destination="database"
)

# Or use helper method
job_ids = await client.create_pipeline(
    [
        ("myapp.jobs.FetchData", {"source": "api"}),
        ("myapp.jobs.TransformData", {"format": "json"}),
        ("myapp.jobs.LoadData", {"destination": "database"}),
    ]
)
print(f"Pipeline created: {job_ids}")
```

**Real-world ETL example:**

```python
async def process_daily_data(client, date):
    """ETL pipeline for daily data processing"""

    # Create pipeline
    job_ids = await client.create_pipeline(
        [
            # Step 1: Extract from multiple sources
            (
                "myapp.jobs.ExtractFromAPI",
                {"date": date.isoformat(), "endpoint": "sales"},
            ),
            # Step 2: Transform and validate
            (
                "myapp.jobs.TransformSalesData",
                {"validation_rules": ["check_totals", "verify_dates"]},
            ),
            # Step 3: Load to warehouse
            ("myapp.jobs.LoadToWarehouse", {"table": "sales_daily", "truncate": False}),
            # Step 4: Update analytics
            ("myapp.jobs.RefreshAnalytics", {"dashboards": ["sales", "revenue"]}),
        ],
        queue="etl",
        priority=200,
    )

    return job_ids
```

### 2. Fan-Out / Fan-In (Parallel Processing)

Process many items in parallel, then aggregate results.

```python
# Process 1000 orders in parallel
orders = [{"order_id": i, "total": i * 10.0} for i in range(1000)]

# Fan-out: Create parallel jobs
job_ids, group_id = await client.create_fan_out(
    "myapp.jobs.ProcessOrder", orders, queue="processing", priority=150
)

# Fan-in: Wait for all to complete
summary_job = await client.enqueue(
    "myapp.jobs.GenerateSummary", waitfor_group=group_id, report_type="daily_orders"
)

print(f"Created {len(job_ids)} processing jobs")
print(f"Summary job {summary_job} will run after all complete")
```

**Real-world image processing example:**

```python
async def process_user_uploads(client, user_id, image_urls):
    """Process multiple uploaded images in parallel"""

    # Create jobs for each image
    items = [
        {"user_id": user_id, "image_url": url, "size": "thumbnail"}
        for url in image_urls
    ]

    # Fan-out: Process images in parallel
    job_ids, group_id = await client.create_fan_out(
        "myapp.jobs.ResizeImage", items, queue="images", priority=100
    )

    # Fan-in: Generate gallery after all processed
    gallery_job = await client.enqueue(
        "myapp.jobs.CreateGallery",
        waitfor_group=group_id,
        user_id=user_id,
        image_count=len(image_urls),
    )

    return {
        "processing_jobs": job_ids,
        "gallery_job": gallery_job,
        "group_id": group_id,
    }
```

### 3. Scheduled Jobs

Schedule jobs to run at specific times.

```python
from datetime import UTC, datetime, timedelta

# Run in 1 hour. Always pass an AWARE datetime: a naive one is encoded as
# UTC by the driver, so "an hour from now" written on a UTC+2 machine
# actually runs three hours late.
await client.enqueue(
    "myapp.jobs.SendReminder",
    run_after=datetime.now(UTC) + timedelta(hours=1),
    user_id=123,
)

# Run tomorrow at 9am UTC
tomorrow_9am = datetime.now(UTC).replace(
    hour=9, minute=0, second=0, microsecond=0
) + timedelta(days=1)

await client.enqueue(
    "myapp.jobs.DailyReport",
    run_after=tomorrow_9am,
    report_date=(datetime.now(UTC) - timedelta(days=1)).date(),
)
```

### 4. Idempotent Jobs (Deadline Keys)

Prevent duplicate job creation using deadline keys.

```python
# Payment processing - prevent duplicates
payment_id = "pay_abc123"

try:
    job_id = await client.enqueue(
        "myapp.jobs.ProcessPayment",
        deadline_key=f"payment:{payment_id}",
        payment_id=payment_id,
        amount=99.99,
    )
    print(f"Payment job created: {job_id}")
except asyncpg.UniqueViolationError:
    print("Payment already processing")

# Daily reports - one per day
date = datetime.now(UTC).date()
try:
    job_id = await client.enqueue(
        "myapp.jobs.DailyReport", deadline_key=f"daily_report:{date}", report_date=date
    )
except asyncpg.UniqueViolationError:
    print(f"Report for {date} already scheduled")
```

### 5. Priority, and the ceiling

Priority is a **finishing position**, not a rating: `priority=1` means
"first" the way it does in a race, and the big numbers are the ones that
wait. Sorting the calls by the number sorts them by when they run.

```python
await client.enqueue("myapp.jobs.UrgentTask", priority=10)  # runs first
await client.enqueue("myapp.jobs.ProcessData", priority=100)  # the default
await client.enqueue("myapp.jobs.BackgroundCleanup", priority=900)  # runs last
```

Each worker also has a **ceiling** — `pj --max-prio`, default 1000 — and
claims only `prio <= ceiling`. Past it, a bigger number stops meaning
"later" and starts meaning "never": the job is never claimed, never fails,
never retries and never reaches the DLQ, and no age check looks at `queued`.

The client closes that at the call site rather than letting you find out
months later:

```python
await client.enqueue("myapp.jobs.Whenever", priority=5000)
# ValueError: priority 5000 is above the worker priority ceiling (1000):
# workers claim only jobs with prio <= their ceiling, so this job would sit
# 'queued' forever -- no error, no retry, no DLQ. LOWER numbers are MORE
# urgent, so least-urgent work wants a priority just UNDER the ceiling
# (e.g. 900), not a large one. If this deployment really runs its workers
# with `pj --max-prio 5000` (or higher), declare it once:
# JobClient(pool, prio_ceiling=5000).

await client.enqueue("myapp.jobs.Whenever", priority=1000)  # fine: the least urgent
```

A refused enqueue writes nothing.

**Raising the ceiling takes both halves.** The ceiling belongs to the worker
fleet and nothing about it is visible over a connection, so the client takes
it as a *declaration* rather than trying to observe it:

```bash
pj --queue backfill --max-prio 5000            # the workers that will claim it
```
```python
client = JobClient(pool, prio_ceiling=5000)
# or: await JobClient.create(..., prio_ceiling=5000)
# or: await JobClient.from_config("./pyjobby.conf.py", prio_ceiling=5000)
# or, for one call only:
await client.enqueue("myapp.jobs.Whenever", priority=5000, prio_ceiling=5000)
```

Declaring it on the client alone writes a job no default-ceiling worker will
claim; raising it only on the workers leaves the client refusing to feed
them. `update_job_priority()` is validated against the same ceiling, for the
same reason.

### 6. Multi-Tenant Jobs

Isolate jobs by tenant/user.

```python
# Enqueue jobs for specific tenant
async def enqueue_user_job(client, user_id, job_class, **kwargs):
    return await client.enqueue(
        job_class,
        uid=user_id,  # Tenant isolation
        queue=f"user_{user_id}",  # Dedicated queue
        **kwargs,
    )


# Usage
job_id = await enqueue_user_job(
    client, user_id=123, job_class="myapp.jobs.GenerateReport", report_type="monthly"
)
```

### 7. Capability-Based Routing

Route jobs to workers with specific capabilities.

```python
# GPU-required jobs
await client.enqueue(
    "myapp.jobs.TrainModel", capability="gpu", model="resnet50", dataset="imagenet"
)

# High-memory jobs
await client.enqueue(
    "myapp.jobs.ProcessLargeDataset", capability="high-memory", dataset_size="100GB"
)

# Geolocation-specific
await client.enqueue("myapp.jobs.SyncData", capability="us-west", region="us-west-1")
```

### 8. Job Tags

Find jobs by something *your application* means — which customer, which
region, which nightly batch — rather than by queue and job class.

```python
job_id = await client.enqueue(
    "myapp.jobs.GenerateReport",
    tags={"customer": "acme", "region": "eu-west-1", "batch": 42},
    report_type="monthly",
)

# Every job for one customer, whatever else it is tagged with
jobs = await client.search_jobs(tags={"customer": "acme"})

# Narrow it: several pairs mean AND
jobs = await client.search_jobs(tags={"customer": "acme", "region": "eu-west-1"})
```

From the command line:

```bash
pj-admin jobs list --tag customer=acme
pj-admin jobs list --tag customer=acme --tag region=eu-west-1
pj-admin jobs list --tag batch=42          # matches the NUMBER 42
pj-admin jobs list --tag 'batch="42"'      # matches the STRING "42"
```

**Tags are not `admin_data`.** `admin_data` is pyjobby's own execution
config — retry strategy, timeout, schedule bookkeeping — and it is not
indexed, because nobody filters on it and indexing it would tax every
enqueue to make no query faster. `tags` is yours, and it *is* indexed.

**Tags are not `uid`.** `uid` is a single BIGINT, so it answers "which
tenant" and nothing else. Reach for it when integer tenancy is the whole
question: it is narrower, cheaper, and already there. Reach for `tags` when
you need more than one dimension, or a value that is not an integer.

Rules, all enforced at enqueue time with a `ValueError`:

- keys are non-empty strings;
- values are strings, numbers, booleans or `None` — no nested objects or
  arrays, because they cannot be expressed as `--tag key=value` and a tag
  you cannot filter by is not a tag;
- matching is **containment**: asking for `{"customer": "acme"}` finds a job
  tagged with customer *and* region *and* batch. Extra tags never disqualify
  a job.

Tagging costs the write path nothing measurable — the index is partial
(`WHERE tags <> '{}'`), so an untagged job never touches it, and a tagged
enqueue measures within noise of an untagged one. `tests/test_job_tags.py`
holds the measurement.

---

## State Machines

A long-running workflow with named states — an order, an onboarding, an
approval, anything that waits for the outside world between steps — is a
[durable state machine](STATECHARTS.md). It is an ordinary job, so everything
above still applies; what follows is the vocabulary that makes driving one
readable.

### Starting one

```python
from myapp.orders import Order  # a StateMachineJob subclass

order = await client.start_machine(Order, kwargs={"customer": 42})
print(order.id)  # an ordinary job id
```

Pass the class when you can import it: the handle then holds the transition
table and can check events locally. `client.start_machine("myapp.orders.Order")`
works when you only have the name, at the cost of those checks.

Machines default to a **`machines` queue**. They park waiting for events, and
a worker parked on a machine is a worker not running ordinary jobs — so keep
them off the queues that serve your latency-sensitive work. Pass `queue=` to
choose another.

### Driving one

```python
await order.send("paid", amount=100)  # payload is yours
state = await order.wait_for_state("shipped", "refunded", timeout=600)
result = await order.result()  # {"final_state": "shipped"}
```

`wait_for_state()` waits for a **state**, not a transition, so it returns
immediately if the machine is already there — and raises rather than hanging
if the machine crashes or is cancelled on the way.

### Why `send()` refuses events

```python
await order.send("packed")
# UnhandledEventError: machine 41 is in 'awaiting_payment', which has no
# transition for 'packed'; it accepts ['cancel', 'paid']
```

This is not a nicety borrowed from in-process FSM libraries. There, an
unhandled event raises on the machine's thread and your event is still in your
hand. Here the event travels through a durable mailbox, and the machine
*consumes* the message and checkpoints having consumed it whether or not any
transition fires. An event sent to the wrong state is not deferred, not
re-queued and not returned — it is gone, and the only symptom is that nothing
happened.

The check costs one read of the machine's state. Ask directly with
`await order.may("paid")`, or skip it with `send(..., check=False)` when you
are deliberately racing the machine or do not have the class.

### Reconnecting to one

A machine outlives the process that started it — usually by a lot. The normal
case is a handle rebuilt from an id you stored:

```python
order = client.machine(order_id, Order)  # cheap, no I/O
await order.send("cancel")
```

### Inspecting one

```python
await order.state()  # "packing"
await order.history()  # this turn's transitions, from jorb_step
order.diagram()  # Mermaid, rendered from the declaration
```

`history()` is the *current turn*: the machine compacts its checkpoint log at
each turn boundary so that replay stays bounded no matter how long it lives
(see [DXE.md](DXE.md#bounding-replay-compact)). For a permanent audit trail,
publish one — as machine events, or into your own table from inside a
`transaction()`.

### Synchronously

`SyncJobClient` mirrors JobClient's **whole** async surface, blocking, for
scripts and cron jobs — every method above exists on it under the same name
(held complete by a mirror test), plus `SyncJobClient.from_config()` for
the config file a script already has. Machines come back as `SyncMachine`:

```python
with SyncJobClient.from_config("./pyjobby.conf.py") as client:
    order = client.start_machine(Order)
    order.send("paid", amount=100)
    order.wait_for_state("shipped", timeout=600)
```

---

## Advanced Usage

### Custom Connection Pooling

```python
import asyncpg

# Create custom pool
pool = await asyncpg.create_pool(
    host="db.example.com",
    port=5432,
    database="pyjobby_prod",
    user="app_user",
    password="secret",
    min_size=10,  # Minimum connections
    max_size=50,  # Maximum connections
    max_queries=50000,  # Recycle after 50k queries
    max_inactive_connection_lifetime=300,  # 5 minutes
    command_timeout=60,  # Command timeout
)

# Use custom pool
client = JobClient(pool)

try:
    await client.enqueue("myapp.jobs.ProcessData")
finally:
    await client.close()
```

### Error Handling

```python
import asyncpg
from pyjobby.client import JobClient


async def enqueue_with_retry(client, job_class, max_retries=3, **kwargs):
    """Enqueue job with retry logic"""

    for attempt in range(max_retries):
        try:
            return await client.enqueue(job_class, **kwargs)

        except asyncpg.UniqueViolationError:
            # Deadline key collision - job already exists
            print(f"Job already exists (deadline_key: {kwargs.get('deadline_key')})")
            return None

        except asyncpg.PostgresConnectionError as e:
            # Database connection error
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)  # Exponential backoff
                continue
            raise

        except Exception as e:
            # Other errors
            print(f"Failed to enqueue job: {e}")
            raise
```

### Monitoring Queue Health

```python
async def monitor_queues(client, queues=["default", "emails", "processing"]):
    """Monitor queue health and alert on issues"""

    for queue in queues:
        stats = await client.queue_stats(queue)
        depth = await client.queue_depth(queue)

        # Alert if queue is backing up
        if depth > 1000:
            print(f"ALERT: Queue '{queue}' has {depth} jobs waiting!")

        # Alert if many crashes
        crash_rate = stats["crashed"] / max(stats["finished"], 1)
        if crash_rate > 0.1:  # > 10% crash rate
            print(f"ALERT: Queue '{queue}' has {crash_rate:.1%} crash rate!")

        # Print stats
        print(f"Queue: {queue}")
        print(f"  Queued: {stats['queued']}")
        print(f"  Running: {stats['running']}")
        print(f"  Finished: {stats['finished']}")
        print(f"  Crashed: {stats['crashed']}")
        print()
```

### Bulk Operations

```python
async def bulk_enqueue_from_csv(client, csv_file, job_class):
    """Read CSV and enqueue jobs in batches"""

    import csv

    batch_size = 1000
    batch = []

    with open(csv_file) as f:
        reader = csv.DictReader(f)

        for row in reader:
            batch.append((job_class, dict(row)))

            # Enqueue in batches of 1000
            if len(batch) >= batch_size:
                job_ids = await client.enqueue_batch(batch)
                print(f"Enqueued {len(job_ids)} jobs")
                batch = []

        # Enqueue remaining
        if batch:
            job_ids = await client.enqueue_batch(batch)
            print(f"Enqueued {len(job_ids)} jobs")


# Usage
await bulk_enqueue_from_csv(client, "data/orders.csv", "myapp.jobs.ProcessOrder")
```

---

## Performance Tips

### 1. Use Batch Operations

**❌ Slow (1000 round-trips):**

```python
for i in range(1000):
    await client.enqueue("myapp.jobs.ProcessItem", item_id=i)
```

**✅ Fast (1 round-trip):**

```python
jobs = [("myapp.jobs.ProcessItem", {"item_id": i}) for i in range(1000)]
await client.enqueue_batch(jobs)
```

### 2. Connection Pooling

Use connection pooling to avoid connection overhead:

```python
# ❌ Creates new connection for each job
for i in range(100):
    async with await JobClient.create(...) as client:
        await client.enqueue(...)

# ✅ Reuses pooled connections
async with await JobClient.create(...) as client:
    for i in range(100):
        await client.enqueue(...)
```

### 3. Appropriate Pool Size

Match pool size to concurrency:

```python
# For high-concurrency web servers
client = await JobClient.create(
    ...,
    min_size=20,  # Keep 20 connections ready
    max_size=100,  # Allow bursts up to 100
)

# For low-concurrency batch scripts
client = await JobClient.create(..., min_size=2, max_size=5)
```

### 4. Queue Organization

Separate queues for different priorities:

```python
# High-priority queue
await client.enqueue("myapp.jobs.UrgentTask", queue="high-priority")

# Normal queue
await client.enqueue("myapp.jobs.NormalTask", queue="default")

# Background queue
await client.enqueue("myapp.jobs.Cleanup", queue="low-priority")
```

### 5. Minimize Metadata

Keep `kwargs` and `admin_data` lean:

```python
# ❌ Stores large data in database
await client.enqueue(
    'myapp.jobs.ProcessData',
    data={'huge': [... 1MB of data ...]}  # Don't do this
)

# ✅ Store reference instead
await client.enqueue(
    'myapp.jobs.ProcessData',
    data_key='s3://bucket/data/file123.json'  # Reference to data
)
```

---

## Complete Example: E-commerce Order Processing

```python
import asyncio
from datetime import datetime, timedelta
from pyjobby.client import JobClient


class OrderProcessor:
    def __init__(self, client: JobClient):
        self.client = client

    async def process_new_order(self, order_id: int, items: list):
        """
        Process new order through multi-stage pipeline:
        1. Validate inventory
        2. Charge payment
        3. Ship items
        4. Send confirmation email
        """

        # Create pipeline for order processing
        pipeline_jobs = await self.client.create_pipeline(
            [
                (
                    "myapp.jobs.ValidateInventory",
                    {"order_id": order_id, "items": items},
                ),
                ("myapp.jobs.ChargePayment", {"order_id": order_id}),
                ("myapp.jobs.CreateShipment", {"order_id": order_id}),
                ("myapp.jobs.SendConfirmationEmail", {"order_id": order_id}),
            ],
            queue="orders",
            priority=200,
        )

        return pipeline_jobs

    async def process_bulk_shipments(self, order_ids: list):
        """
        Process bulk shipments in parallel, then generate manifest
        """

        items = [{"order_id": oid} for oid in order_ids]

        # Fan-out: Process shipments in parallel
        job_ids, group_id = await self.client.create_fan_out(
            "myapp.jobs.GenerateShippingLabel", items, queue="shipping", priority=150
        )

        # Fan-in: Generate manifest after all labels created
        manifest_job = await self.client.enqueue(
            "myapp.jobs.GenerateShippingManifest",
            waitfor_group=group_id,
            group_id=group_id,
            order_count=len(order_ids),
        )

        return {"label_jobs": job_ids, "manifest_job": manifest_job}

    async def schedule_abandoned_cart_reminders(self, cart_ids: list):
        """
        Schedule reminder emails for abandoned carts (24 hours later)
        """

        jobs = []
        send_time = datetime.now(UTC) + timedelta(hours=24)

        for cart_id in cart_ids:
            # The per-job options dict carries each job's own deadline_key,
            # so a cart already scheduled is not scheduled twice
            jobs.append(
                (
                    "myapp.jobs.SendAbandonedCartEmail",
                    {"cart_id": cart_id},
                    {"deadline_key": f"cart_reminder:{cart_id}"},
                )
            )

        job_ids = await self.client.enqueue_batch(
            jobs,
            queue="emails",
            run_after=send_time,
            priority=50,  # Low priority
        )

        return job_ids

    async def monitor_order_queues(self):
        """Monitor order processing queues"""

        for queue in ["orders", "shipping", "emails"]:
            stats = await self.client.queue_stats(queue)
            depth = await self.client.queue_depth(queue)

            print(f"\nQueue: {queue}")
            print(f"  Depth: {depth}")
            print(f"  Running: {stats['running']}")
            print(f"  Crashed: {stats['crashed']}")


# Usage
async def main():
    async with await JobClient.from_config("./pyjobby.conf.py") as client:
        processor = OrderProcessor(client)

        # Process new order
        order_jobs = await processor.process_new_order(
            order_id=12345, items=[{"sku": "ABC", "qty": 2}, {"sku": "XYZ", "qty": 1}]
        )
        print(f"Order pipeline: {order_jobs}")

        # Process bulk shipments
        shipment_result = await processor.process_bulk_shipments(
            order_ids=list(range(1000, 2000))
        )
        print(f"Shipment jobs: {len(shipment_result['label_jobs'])}")

        # Schedule cart reminders
        reminder_jobs = await processor.schedule_abandoned_cart_reminders(
            cart_ids=[111, 222, 333]
        )
        print(f"Scheduled {len(reminder_jobs)} reminders")

        # Monitor queues
        await processor.monitor_order_queues()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Next Steps

- See [ADMIN_TOOLS.md](ADMIN_TOOLS.md) for CLI and web interface documentation
- See [RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md) for cron-based scheduling
- See [ARCHITECTURE.md](ARCHITECTURE.md) for system design
- See [examples/](../examples/) for more real-world usage patterns
