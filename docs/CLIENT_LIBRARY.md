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

### Job Submission

#### `enqueue(job_class, **kwargs)`

Enqueue a single job.

**Parameters:**

- `job_class` (str): Full Python class path (e.g., `'myapp.jobs.SendEmail'`)
- `queue` (str): Queue name (default: `'default'`)
- `priority` (int): Priority (higher = more urgent, default: 100)
- `run_after` (datetime): When to run (default: now)
- `capability` (str): Required worker capability (default: None)
- `uid` (int): User/tenant ID for multi-tenancy (default: None)
- `run_group` (int): Group ID for tracking related jobs (default: None)
- `waitfor_job` (int): Wait for this job ID to complete (default: None)
- `waitfor_group` (int): Wait for all jobs in this group (default: None)
- `deadline_key` (str): Idempotency key to prevent duplicates (default: None)
- `admin_data` (dict): Metadata for tracking (default: None)
- `**kwargs`: Job arguments passed to job class

**Returns:** Job ID (int)

**Examples:**

```python
# Simple job
job_id = await client.enqueue("myapp.jobs.SendEmail", to="user@example.com")

# Scheduled job (run in 1 hour)
from datetime import datetime, timedelta

job_id = await client.enqueue(
    "myapp.jobs.DailyReport",
    run_after=datetime.now() + timedelta(hours=1),
    report_type="sales",
)

# High priority job
job_id = await client.enqueue(
    "myapp.jobs.UrgentTask",
    priority=500,  # Higher than default 100
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
```

#### `enqueue_batch(jobs, queue='default', priority=100, run_after=None, run_group=None)`

Enqueue multiple jobs efficiently in a single transaction.

**Parameters:**

- `jobs` (List[Tuple[str, Dict]]): List of (job_class, kwargs) tuples
- `queue` (str): Queue name for all jobs
- `priority` (int): Priority for all jobs
- `run_after` (datetime): When to run all jobs
- `run_group` (int): Group ID for all jobs

**Returns:** List of job IDs

**Examples:**

```python
# Enqueue 1000 jobs efficiently
jobs = [
    ("myapp.jobs.ProcessItem", {"item_id": i, "action": "process"}) for i in range(1000)
]
job_ids = await client.enqueue_batch(jobs, queue="processing")

# Batch with scheduling
from datetime import datetime, timedelta

jobs = [("myapp.jobs.SendReminder", {"user_id": user_id}) for user_id in user_ids]
job_ids = await client.enqueue_batch(
    jobs, run_after=datetime.now() + timedelta(hours=24), queue="notifications"
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
from datetime import datetime, timedelta

# Run in 1 hour
await client.enqueue(
    "myapp.jobs.SendReminder",
    run_after=datetime.now() + timedelta(hours=1),
    user_id=123,
)

# Run tomorrow at 9am
tomorrow_9am = datetime.now().replace(
    hour=9, minute=0, second=0, microsecond=0
) + timedelta(days=1)

await client.enqueue(
    "myapp.jobs.DailyReport",
    run_after=tomorrow_9am,
    report_date=(datetime.now() - timedelta(days=1)).date(),
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
date = datetime.now().date()
try:
    job_id = await client.enqueue(
        "myapp.jobs.DailyReport", deadline_key=f"daily_report:{date}", report_date=date
    )
except asyncpg.UniqueViolationError:
    print(f"Report for {date} already scheduled")
```

### 5. High-Priority Jobs

Queue urgent jobs ahead of others.

```python
# Normal priority (100 is default)
await client.enqueue("myapp.jobs.ProcessData", priority=100)

# High priority - processes first
await client.enqueue("myapp.jobs.UrgentTask", priority=500)

# Low priority - processes last
await client.enqueue("myapp.jobs.BackgroundCleanup", priority=10)
```

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
        send_time = datetime.now() + timedelta(hours=24)

        for cart_id in cart_ids:
            # Use deadline key to prevent duplicate reminders
            jobs.append(
                (
                    "myapp.jobs.SendAbandonedCartEmail",
                    {"cart_id": cart_id, "deadline_key": f"cart_reminder:{cart_id}"},
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
- See [ARCHITECTURE_CAPABILITIES.md](ARCHITECTURE_CAPABILITIES.md) for system design
- See [examples/](../examples/) for more real-world usage patterns
