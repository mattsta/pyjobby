# JobSystem Class Documentation

## Overview

The `JobSystem` class (`pyjobby/pj.py:228-500`) is the core orchestrator of pyjobby. Each worker process runs one instance of JobSystem, which handles:

- Database connection and prepared statement management
- Job polling and atomic job claiming
- Job class loading and execution
- Optional web server for direct job invocation
- Signal handling for graceful shutdown
- Worker-local caching

## Class Definition

```python
@dataclass
class JobSystem:
    """A PostgreSQL Job system.

    Reads tasks with class and kwargs designated by a jorb table, runs
    tasks based on queue, priority, and next run time.

    If a task throws an exception, the exception is saved to the job row
    and job is marked as 'crashed' for future inspection."""

    dsn: dict[str, str]  # Database connection parameters
    qname: str  # Queue name this worker processes
    capabilities: tuple[str]  # Capabilities this worker advertises
    workerId: int  # Unique worker ID (0, 1, 2, ...)
    checkInterval: int = 5  # Seconds between job polls
    webPort: Optional[dict] = None  # Web server configuration
    prio: int = 1000  # Maximum job priority to claim
    stop: bool = False  # Shutdown flag
    pid: int  # Process ID
    node: str  # Hostname
    cache: dict[str, Any]  # Worker-local cache

    # Phase 1 Improvements: Self-Healing and Fault Tolerance
    max_retries: int = 10  # Maximum retry attempts before permanent failure
    default_timeout: int = 3600  # Default job timeout in seconds (1 hour)
    enable_recovery: bool = True  # Enable abandoned job recovery on startup
```

## Initialization

JobSystem instances are created by the `workit()` CLI function and run in separate processes:

```python
# From pyjobby/pj.py:586-603
runner = JobSystem(
    dsn=db_params,  # asyncpg connection parameters
    qname="default",  # Queue name
    capabilities=("gpu", "host:web-1"),  # Worker capabilities
    workerId=0,  # Worker index
    checkInterval=5,  # Poll every 5 seconds
    webPort=web_config,  # Optional web server config
    max_retries=10,  # Maximum retry attempts (default: 10)
    default_timeout=3600,  # Default job timeout in seconds (default: 1 hour)
    enable_recovery=True,  # Enable crash recovery (default: True)
)

signal.signal(signal.SIGTERM, runner.shutdown)
asyncio.run(runner.run())
```

## Key Methods

### `async run() -> None`

Location: `pyjobby/pj.py:305-499`

The main event loop for the worker. This method:

1. **Sets up web server** (if configured)
2. **Connects to PostgreSQL** via asyncpg
3. **Prepares all SQL statements**
4. **Enters polling loop**:
   - Claims next eligible job
   - Executes job
   - Marks job complete or crashed
   - Triggers dependent jobs
   - Repeats immediately if jobs are available
   - Sleeps 5-6 seconds if no jobs found
5. **Handles shutdown** gracefully

**Example Flow**:

```python
async def run(self) -> None:
    # 1. Start web server (optional)
    if self.webPort:
        server = web.Server(self.webHandler)
        runner = web.ServerRunner(server)
        await runner.setup()
        for site in self.webPort["sites"]:
            if "path" in site:  # Unix socket
                site["path"] = site["path"] + f"-{self.workerId}"
                await web.UnixSite(runner, **site).start()
            else:  # TCP socket
                site.update(dict(reuse_port=True))
                await web.TCPSite(runner, **site).start()

    # 2. Connect to database
    self.cxn = await asyncpg.connect(**self.dsn)
    await self.cxn.set_type_codec("json", encoder=orjson.dumps, decoder=orjson.loads)

    # 3. Prepare statements
    self.stmts = {}
    for name, stmt in STMTS.items():
        self.stmts[name] = await self.cxn.prepare(stmt)

    # 4. Poll for jobs
    while not self.stop:
        jobs = await self.ex(
            "claim", self.pid, self.node, self.qname, self.capabilities, self.prio
        )

        if jobs:
            job = jobs[0]
            klass = self.classForKlassFromName(job["job_class"], job=job)
            result = await klass.run()  # Execute job
            await self.ex("finished", job["id"], result)

            # Trigger dependent jobs
            await self.ex("enqueue-next-self-finished", job["id"])
            if job["run_group"]:
                await self.ex(
                    "enqueue-next-if-peer-group-is-finished", job["run_group"]
                )
        else:
            # Sleep 5-6 seconds with jitter
            await asyncio.sleep(5 + random.uniform(0, 0.001))
```

### `async ex(op: str, *args) -> list[asyncpg.Record]`

Location: `pyjobby/pj.py:249-263`

Execute a prepared statement by name.

**Parameters**:

- `op`: Statement name (e.g., "claim", "finished", "crash")
- `*args`: Arguments to pass to the prepared statement

**Returns**: List of asyncpg.Record objects

**Features**:

- Automatic retry on connection errors
- Uses prepared statements for performance and safety

**Example**:

```python
# Claim a job
jobs = await self.ex(
    "claim",
    os.getpid(),  # worker_pid
    platform.node(),  # worker_host
    "default",  # queue
    ["gpu", "host:ml-1"],  # capabilities
    1000,
)  # max priority

# Mark job finished
await self.ex("finished", job_id, {"status": "ok"})

# Record crash
await self.ex("crash", job_id, "Connection timeout", full_traceback)

# Reschedule job for future
import datetime

await self.ex("reschedule", job_id, datetime.timedelta(minutes=5))
```

### `classForKlassFromName(klassName: str, job: dict) -> Job`

Location: `pyjobby/pj.py:284-303`

Dynamically load and instantiate a job class from its string path.

**Parameters**:

- `klassName`: Full Python path (e.g., "job.email.SendEmail")
- `job`: Job row data from database (optional)

**Returns**: Instance of the Job subclass

**Features**:

- **Hot reloading**: Reloads the module on every invocation to pick up code changes
- **Error handling**: Raises `FileNotFoundError` if class not found
- **Dependency injection**: Injects `JobSystem` reference and job data

**Example**:

```python
# Load job class and create instance
klass = self.classForKlassFromName("job.email.SendEmail", job_row)

# Equivalent to:
# from job.email import SendEmail
# klass = SendEmail(s=self, job=job_row)

# Now execute the job
result = await klass.run()
```

**Hot Reloading Demonstration**:

```python
# Initial job implementation
# job/test.py
class TestJob(Job):
    def task(self):
        return "v1"


# Worker claims and runs job -> returns "v1"


# Edit job file while worker is running
# job/test.py
class TestJob(Job):
    def task(self):
        return "v2"  # Changed!


# Worker claims another job -> returns "v2" (automatically picked up changes!)
```

### `async webHandler(request: web.Request) -> web.Response`

Location: `pyjobby/pj.py:269-282`

Handle HTTP requests to invoke jobs directly without queueing.

**URL Format**: `http://host:port/{job.class.path}`

**Example**:

```python
# Configuration
web_listen = {
    "sites": [{"host": "localhost", "port": 8080}],
    "paths": {"job.email.SendEmail"},  # Whitelist
}


# Job implementation
class SendEmail(Job):
    async def web(self, request: web.Request) -> web.Response:
        data = await request.json()
        result = await self.task(**data)
        return web.Response(text=orjson.dumps(result), content_type="application/json")

    async def task(self, to: str, subject: str, body: str):
        # Send email...
        return {"status": "sent", "message_id": "abc123"}


# HTTP request
# POST http://localhost:8080/job.email.SendEmail
# {"to": "user@example.com", "subject": "Hello", "body": "..."}
```

### `shutdown(signum: int, frame: Any) -> None`

Location: `pyjobby/pj.py:265-267`

Signal handler for graceful shutdown.

**Signals Handled**: SIGTERM, SIGINT (via parent process)

**Behavior**:

- Sets `self.stop = True`
- Allows current job to complete
- Exits polling loop
- Closes database connection

**Example**:

```python
# Setup in runAndDone()
signal.signal(signal.SIGTERM, runner.shutdown)

# User sends SIGTERM
# $ kill -TERM <worker_pid>

# Worker finishes current job, then exits cleanly
```

## Database Operations

### Prepared Statements

Location: `pyjobby/pj.py:99-224`

JobSystem uses prepared statements for all database operations:

| Statement                                | Purpose                   | Parameters                                                                          | Returns            |
| ---------------------------------------- | ------------------------- | ----------------------------------------------------------------------------------- | ------------------ |
| `claim`                                  | Atomically claim next job | pid, host, queue, capabilities[], max_prio                                          | Job row or empty   |
| `get`                                    | Retrieve claimed job      | job_id                                                                              | Job row            |
| `run`                                    | Mark job as running       | job_id                                                                              | Updated row        |
| `finished`                               | Mark job complete         | job_id, result (JSONB)                                                              | Updated row        |
| `crash`                                  | Record error              | job_id, error_msg, backtrace                                                        | Updated row        |
| `reschedule`                             | Re-queue job              | job_id, interval                                                                    | Updated row        |
| `schedule-deadline`                      | Insert with deadline key  | deadline_key, queue, prio, run_after, uid, run_group, job_class, kwargs, admin_data | New row ID         |
| `enqueue-next-self-finished`             | Trigger dependent jobs    | job_id                                                                              | Triggered job rows |
| `enqueue-next-if-peer-group-is-finished` | Trigger group deps        | run_group                                                                           | Triggered job rows |

### Job Claiming Algorithm

The `claim` statement is the heart of job selection:

```sql
UPDATE jorb
SET state = 'claimed',
    worker_pid = $1,
    worker_host = $2,
    updated = TIMEZONE('utc', clock_timestamp()),
    run_count = run_count + 1
WHERE id = (
    SELECT id FROM jorb
    WHERE queue = $3                                      -- Match queue
        AND (capability = ANY($4::text[]) OR capability IS NULL)  -- Match capability
        AND prio <= $5                                    -- Within priority limit
        AND run_after <= TIMEZONE('utc', clock_timestamp())      -- Eligible to run
        AND state = 'queued'                             -- Available
    ORDER BY prio, run_after                             -- Highest priority first
    FOR UPDATE SKIP LOCKED                                -- Atomic claiming
    LIMIT 1
)
RETURNING *
```

**Selection Criteria** (in order):

1. **Queue match**: Job in worker's queue
2. **Capability match**: Job requires capability worker has (or no capability)
3. **Priority filter**: Job priority ≤ worker's max priority
4. **Time eligibility**: `run_after` timestamp has passed
5. **State filter**: Only `queued` jobs
6. **Priority order**: Lower `prio` selected first
7. **Atomic lock**: `FOR UPDATE SKIP LOCKED` prevents conflicts

**Performance Characteristics**:

- **Index used**: `jorb_poll_idx` (partial index on `state='queued'`)
- **Typical latency**: <1ms even with millions of completed jobs
- **Contention**: Zero (workers never wait on each other)

## Worker-Local Cache

The `cache` dictionary is available to all jobs running on a worker:

```python
class JobSystem:
    cache: dict[str, Any] = field(default_factory=dict)
```

**Use Cases**:

- **Credentials**: Load once, reuse across jobs
- **Connections**: Database/API connection pools
- **Statistics**: Track worker performance metrics
- **Rate limiting**: Count operations per time window

**Example**:

```python
class EmailJob(Job):
    def task(self, to: str, **kwargs):
        # Get cached SMTP connection (create if doesn't exist)
        if "smtp" not in self.s.cache:
            import smtplib

            self.s.cache["smtp"] = smtplib.SMTP("localhost")
            logger.info("Created new SMTP connection")

        smtp = self.s.cache["smtp"]
        smtp.sendmail("noreply@example.com", to, "Hello!")

        # Track statistics
        stats = self.s.cache.setdefault("email_stats", {"sent": 0})
        stats["sent"] += 1

        return {"status": "sent", "total_sent": stats["sent"]}
```

**Isolation**: Each worker process has its own cache (not shared between workers).

## Web Server Integration

### Configuration

```python
# pyjobby.conf.py
web_listen = {
    "sites": [
        # TCP socket (Linux: load-balanced across workers)
        {"host": "0.0.0.0", "port": 8080},
        # Unix socket (one per worker: path-{workerId})
        {"path": "/var/run/pyjobby.sock"},
    ],
    "paths": {
        "job.api.WebhookHandler",
        "job.image.Thumbnail",
    },
}
```

### Load Balancing

**Linux (TCP sockets)**:

- Each worker binds to same `host:port` with `SO_REUSEPORT`
- Kernel distributes incoming connections across workers
- Automatic load balancing and failover

**Other Platforms (Unix sockets)**:

- Each worker creates separate socket: `/var/run/pyjobby.sock-0`, `.sock-1`, etc.
- Use nginx/haproxy to balance:

```nginx
upstream pyjobby {
    server unix:/var/run/pyjobby.sock-0;
    server unix:/var/run/pyjobby.sock-1;
    server unix:/var/run/pyjobby.sock-2;
    server unix:/var/run/pyjobby.sock-3;
}

server {
    location /jobs/ {
        proxy_pass http://pyjobby/;
    }
}
```

### Security

**Whitelist Only**: Only job classes in `web_listen["paths"]` are accessible via HTTP.

```python
# This job can be called via web:
web_listen = {"paths": {"job.api.PublicWebhook"}}

# This job CANNOT be called via web (returns 403):
# job.internal.DeleteAllUsers
```

## Real-World Usage Examples

### Example 1: Email Processing System

**Setup**: Process email queue with 4 workers

```python
# pyjobby.conf.py
db_params = {
    "database": "emailapp",
    "user": "pyjobby",
    "password": "secret",
    "host": "/var/run/postgresql",
}

# job/email.py
import smtplib
from pyjobby.pj import Job


class SendEmail(Job):
    def task(self, to: str, subject: str, body: str, uid: int):
        # Reuse SMTP connection from cache
        if "smtp" not in self.s.cache:
            self.s.cache["smtp"] = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            self.s.cache["smtp"].login("noreply@example.com", "password")

        smtp = self.s.cache["smtp"]
        msg = f"Subject: {subject}\n\n{body}"
        smtp.sendmail("noreply@example.com", to, msg)

        return {"status": "sent", "timestamp": datetime.utcnow()}


# Start workers
# $ pj --queue email --workers 4
```

**Submit Jobs**:

```python
import asyncpg


async def send_welcome_email(user_id: int, email: str):
    conn = await asyncpg.connect(**db_params)
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, uid)
        VALUES ($1, $2, $3, $4)
    """,
        "job.email.SendEmail",
        orjson.dumps(
            {
                "to": email,
                "subject": "Welcome!",
                "body": "Thanks for signing up.",
                "uid": user_id,
            }
        ),
        "email",
        user_id,
    )
```

### Example 2: Image Processing Pipeline

**Setup**: Parallel processing with dependencies

```python
# job/image.py
from PIL import Image
from pyjobby.pj import Job
import hashlib


class GenerateThumbnail(Job):
    async def task(self, filepath: str, sizes: list[int]):
        img = Image.open(filepath)
        thumbnails = []

        for size in sizes:
            thumb = img.copy()
            thumb.thumbnail((size, size))
            thumb_path = f"{filepath}.thumb.{size}.jpg"
            thumb.save(thumb_path)
            thumbnails.append(thumb_path)

        return {"thumbnails": thumbnails}


class HashFile(Job):
    def task(self, filepath: str):
        with open(filepath, "rb") as f:
            return {"sha256": hashlib.sha256(f.read()).hexdigest()}


class ExtractEXIF(Job):
    def task(self, filepath: str):
        from PIL.ExifTags import TAGS

        img = Image.open(filepath)
        exif = {TAGS.get(k, k): v for k, v in (img._getexif() or {}).items()}
        return {"exif": exif}


class UploadToS3(Job):
    async def task(self, filepath: str, thumbnails: list[str]):
        import boto3

        s3 = self.s.cache.setdefault("s3", boto3.client("s3"))

        # Upload original
        s3.upload_file(filepath, "my-bucket", f"originals/{filepath}")

        # Upload thumbnails
        for thumb in thumbnails:
            s3.upload_file(thumb, "my-bucket", f"thumbs/{thumb}")

        return {"s3_key": f"originals/{filepath}"}


class NotifyComplete(Job):
    async def task(self, user_id: int, filepath: str):
        # Send notification that processing is complete
        await notify_user(user_id, f"Processing complete for {filepath}")
        return {"notified": True}
```

**Submit Pipeline**:

```python
import secrets
import asyncpg
import orjson


async def process_upload(user_id: int, filepath: str):
    conn = await asyncpg.connect(**db_params)
    group_id = secrets.randbits(63)

    # Step 1: Create dependent jobs (all run in parallel)
    jobs_config = [
        (
            "job.image.GenerateThumbnail",
            {"filepath": filepath, "sizes": [150, 300, 800]},
        ),
        ("job.image.HashFile", {"filepath": filepath}),
        ("job.image.ExtractEXIF", {"filepath": filepath}),
    ]

    for job_class, kwargs in jobs_config:
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, uid, run_group, prio,
                            capability)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
            job_class,
            orjson.dumps(kwargs),
            "default",
            user_id,
            group_id,
            0,
            f"host:{platform.node()}",
        )  # Pin to current server (local files)

    # Step 2: Upload to S3 (waits for thumbnail job)
    thumb_job_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, uid, state, waitfor_group)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
    """,
        "job.image.UploadToS3",
        orjson.dumps({"filepath": filepath, "thumbnails": []}),
        "default",
        user_id,
        "waiting",
        group_id,
    )

    # Step 3: Notify user (waits for S3 upload)
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, uid, state, waitfor_job)
        VALUES ($1, $2, $3, $4, $5, $6)
    """,
        "job.image.NotifyComplete",
        orjson.dumps({"user_id": user_id, "filepath": filepath}),
        "default",
        user_id,
        "waiting",
        thumb_job_id,
    )


# Execution flow:
# 1. GenerateThumbnail, HashFile, ExtractEXIF run in parallel
# 2. When ALL complete, UploadToS3 runs
# 3. When UploadToS3 completes, NotifyComplete runs
```

### Example 3: Scheduled Reports with Deadline Keys

**Setup**: Prevent duplicate scheduled reports

```python
# job/reports.py
from pyjobby.pj import Job
import datetime


class DailyUserReport(Job):
    async def task(self, user_id: int, report_date: str):
        # Generate report for ALL activity on report_date
        # (even if user triggered this multiple times)

        activities = await self.s.cache["db"].fetch(
            """
            SELECT * FROM activities
            WHERE user_id = $1
              AND date = $2
        """,
            user_id,
            report_date,
        )

        report = {
            "user_id": user_id,
            "date": report_date,
            "total_activities": len(activities),
            "summary": "...",
        }

        # Email report
        await send_email(user_id, report)

        return report
```

**Submit Jobs** (with deduplication):

```python
async def schedule_report(user_id: int):
    """Called every time user uploads a file.

    Only schedules ONE report per user per day, even if called 100 times.
    """
    conn = await asyncpg.connect(**db_params)

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_midnight = datetime.datetime.combine(tomorrow, datetime.time.min)

    deadline_key = f"daily-report:{user_id}:{tomorrow.isoformat()}"

    try:
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, uid, run_after, deadline_key)
            VALUES ($1, $2, $3, $4, $5, $6)
        """,
            "job.reports.DailyUserReport",
            orjson.dumps({"user_id": user_id, "report_date": tomorrow.isoformat()}),
            "default",
            user_id,
            tomorrow_midnight,
            deadline_key,
        )

        logger.info(f"Scheduled report for user {user_id} at {tomorrow_midnight}")
    except asyncpg.UniqueViolationError:
        # Report already scheduled for this user/date - ignore
        logger.info(f"Report already scheduled for user {user_id}")


# User uploads 10 files throughout the day
for _ in range(10):
    await schedule_report(user_id=123)

# Result: Only ONE job created (subsequent inserts fail unique constraint)
```

### Example 4: GPU-Accelerated ML Inference

**Setup**: Route ML jobs to GPU workers

```python
# job/ml.py
from pyjobby.pj import Job
import torch


class ImageClassification(Job):
    def task(self, image_url: str):
        # Load model from cache (expensive operation, do once)
        if "model" not in self.s.cache:
            self.s.cache["model"] = torch.load("/models/resnet50.pth")
            self.s.cache["model"].cuda()
            self.s.cache["model"].eval()

        model = self.s.cache["model"]

        # Download and preprocess image
        img = download_and_preprocess(image_url)

        # Run inference
        with torch.no_grad():
            predictions = model(img.cuda())

        # Return top 5 predictions
        top5 = predictions.topk(5)
        return {
            "classes": top5.indices.cpu().tolist(),
            "probabilities": top5.values.cpu().tolist(),
        }
```

**Infrastructure**:

```bash
# GPU server (has CUDA, powerful GPU)
$ pj --queue ml --cap "gpu" --cap "cuda-11.8" --workers 2

# CPU servers (no GPU)
$ pj --queue default --workers 8
```

**Submit Jobs**:

```python
async def classify_image(image_url: str):
    conn = await asyncpg.connect(**db_params)
    await conn.execute(
        """
        INSERT INTO jorb (job_class, kwargs, queue, capability)
        VALUES ($1, $2, $3, $4)
    """,
        "job.ml.ImageClassification",
        orjson.dumps({"image_url": image_url}),
        "ml",  # Route to ML queue
        "gpu",
    )  # Requires GPU capability


# This job will ONLY run on the GPU server!
```

### Example 5: Web Request Handler with Direct Invocation

**Setup**: Handle webhook callbacks without queueing

```python
# pyjobby.conf.py
web_listen = {
    "sites": [{"host": "0.0.0.0", "port": 8080}],
    "paths": {"job.webhooks.StripeWebhook"},
}

# job/webhooks.py
from pyjobby.pj import Job
from aiohttp import web
import hmac
import hashlib


class StripeWebhook(Job):
    async def web(self, request: web.Request) -> web.Response:
        """Handle Stripe webhook (direct, no queue)"""

        # Verify signature
        payload = await request.read()
        sig = request.headers.get("Stripe-Signature")

        if not self.verify_stripe_signature(payload, sig):
            return web.Response(status=401, text="Invalid signature")

        # Parse event
        event = orjson.loads(payload)

        # Process immediately
        result = await self.task(**event)

        return web.Response(text="ok")

    async def task(self, type: str, data: dict, **kwargs):
        """Can also be called via queue for retries"""

        if type == "charge.succeeded":
            await self.handle_charge_succeeded(data)
        elif type == "charge.failed":
            await self.handle_charge_failed(data)

        return {"processed": type}

    def verify_stripe_signature(self, payload: bytes, sig: str) -> bool:
        secret = self.s.cache.get("stripe_secret", "whsec_...")
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    async def handle_charge_succeeded(self, data: dict):
        # Update database, send confirmation email, etc.
        pass

    async def handle_charge_failed(self, data: dict):
        # Retry payment, notify user, etc.
        pass
```

**Usage**:

```bash
# Direct webhook (immediate processing, no queue)
$ curl -X POST http://localhost:8080/job.webhooks.StripeWebhook \
       -H "Stripe-Signature: t=123,v1=abc..." \
       -d '{"type": "charge.succeeded", "data": {...}}'

# Or submit to queue for retry/scheduling
$ psql -c "INSERT INTO jorb (job_class, kwargs, queue)
           VALUES ('job.webhooks.StripeWebhook',
                   '{\"type\": \"charge.failed\", \"data\": {...}}',
                   'default')"
```

## Performance Tuning

### Polling Interval

```python
# Faster polling (more DB load, lower latency)
runner = JobSystem(..., checkInterval=1)  # 1 second

# Slower polling (less DB load, higher latency)
runner = JobSystem(..., checkInterval=10)  # 10 seconds

# Default
runner = JobSystem(..., checkInterval=5)  # 5 seconds
```

**Trade-offs**:

- Lower interval: Jobs start faster, but more DB queries when idle
- Higher interval: Fewer DB queries, but jobs wait longer to start

### Worker Count

```bash
# CPU-bound jobs: Use all cores
$ pj --workers $(nproc)

# I/O-bound jobs: Oversubscribe
$ pj --workers $(($(nproc) * 2))

# Memory-intensive jobs: Limit workers
$ pj --workers 2

# Default: Half of CPU cores
$ pj --workers $(( $(nproc) / 2 ))
```

### Priority Limits

```python
# Worker only processes high-priority jobs (prio <= 10)
runner = JobSystem(..., prio=10)

# Worker processes all jobs (prio <= 1000)
runner = JobSystem(..., prio=1000)  # Default

# Worker processes only low-priority background jobs (prio <= 10000)
runner = JobSystem(..., prio=10000)
```

**Use Case**: Dedicated workers for urgent jobs

```bash
# High-priority worker (2 workers, only prio <= 10)
$ pj --workers 2 --prio 10

# Normal workers (8 workers, all priorities)
$ pj --workers 8
```

### Connection Pooling

```python
# pyjobby.conf.py
db_params = {
    "database": "myapp",
    "user": "pyjobby",
    "min_size": 1,  # Minimum pool size per worker
    "max_size": 5,  # Maximum pool size per worker
}

# With 10 workers: total 10-50 connections to PostgreSQL
```

## Monitoring and Debugging

### Logging

JobSystem uses loguru with process-aware formatting:

```
12345:Process-0 2025-11-18 10:00:00.123 | I | pj:run:363 - [default:1000] Connected and waiting for jobs!
12345:Process-0 2025-11-18 10:00:05.456 | I | pj:run:416 - [job 1001] Running SendEmail (job.email.SendEmail, default, 0, None)
12345:Process-0 2025-11-18 10:00:05.789 | I | pj:run:442 - [job 1001] Completed SendEmail in 333.12 ms
```

**Customize Logging**:

```python
from loguru import logger

# Add file logging
logger.add(
    "/var/log/pyjobby/worker.log", rotation="500 MB", retention="10 days", level="INFO"
)

# Add structured logging
logger.add(lambda msg: send_to_datadog(msg), format="{message}", level="WARNING")
```

### Metrics Collection

```python
class MetricsJob(Job):
    def task(self, **kwargs):
        # Initialize metrics
        metrics = self.s.cache.setdefault(
            "metrics",
            {
                "jobs_processed": 0,
                "total_duration": 0.0,
                "errors": 0,
            },
        )

        start = time.time()
        try:
            result = self.do_work(**kwargs)
            metrics["jobs_processed"] += 1
        except Exception as e:
            metrics["errors"] += 1
            raise
        finally:
            metrics["total_duration"] += time.time() - start

        # Periodically flush to external system
        if metrics["jobs_processed"] % 100 == 0:
            send_to_prometheus(metrics)

        return result
```

### Database Queries for Monitoring

```sql
-- Worker activity
SELECT worker_host, worker_pid, state, COUNT(*)
FROM jorb
WHERE updated > NOW() - INTERVAL '5 minutes'
GROUP BY worker_host, worker_pid, state;

-- Slow jobs
SELECT id, job_class,
       EXTRACT(EPOCH FROM (updated - run_at)) as duration_seconds
FROM jorb
WHERE state = 'running'
  AND run_at < NOW() - INTERVAL '5 minutes'
ORDER BY duration_seconds DESC;

-- Error rate by job class
SELECT job_class,
       COUNT(*) FILTER (WHERE state = 'finished') as succeeded,
       COUNT(*) FILTER (WHERE state = 'crashed') as failed
FROM jorb
WHERE updated > NOW() - INTERVAL '1 hour'
GROUP BY job_class;
```

## Error Handling

### Automatic Retry

When a job raises an exception:

1. **Crash recorded**: Error message and stack trace saved to database
2. **Exponential backoff**: Retry delay increases with each attempt
3. **Job rescheduled**: New job created with future `run_after`

**Backoff Schedule**:

```
Attempt 1: 16 seconds
Attempt 2: 32 seconds
Attempt 3: 64 seconds (1 minute)
Attempt 4: 128 seconds (2 minutes)
Attempt 5: 256 seconds (4 minutes)
Attempt 6: 512 seconds (8 minutes)
Attempt 7+: 1024 seconds (17 minutes)
```

### Connection Resilience

JobSystem automatically retries database operations on connection errors:

```python
async def ex(self, op: str, *args: Any) -> list[asyncpg.Record]:
    while True:
        try:
            return await self.stmts[op].fetch(*args)
        except asyncpg.InterfaceError:
            # Connection lost - retry after delay
            await asyncio.sleep(0.5)
            continue
```

## Best Practices

### 1. Use Worker Cache for Expensive Resources

```python
# Good: Reuse connection
class MyJob(Job):
    def task(self):
        if "redis" not in self.s.cache:
            self.s.cache["redis"] = redis.Redis()
        return self.s.cache["redis"].get("key")


# Bad: Create new connection every time
class MyJob(Job):
    def task(self):
        r = redis.Redis()  # Expensive!
        return r.get("key")
```

### 2. Pin Resource-Specific Jobs to Capabilities

```python
# Job requires local file access
await conn.execute(
    """
    INSERT INTO jorb (job_class, kwargs, capability)
    VALUES ($1, $2, $3)
""",
    "job.file.Process",
    '{"filepath": "/local/file.txt"}',
    f"host:{platform.node()}",
)  # Must run on this specific host
```

### 3. Use Priorities for Urgency

```python
# Paid user (high priority)
await add_job(..., prio=-100)

# Free user (normal priority)
await add_job(..., prio=0)

# Background cleanup (low priority)
await add_job(..., prio=1000)
```

### 4. Implement Idempotent Jobs

Jobs may be retried, so ensure they're safe to run multiple times:

```python
# Good: Idempotent
class UpdateUser(Job):
    def task(self, user_id: int, email: str):
        # Safe to run multiple times
        await db.execute("UPDATE users SET email = $1 WHERE id = $2", email, user_id)


# Bad: Not idempotent
class SendWelcomeEmail(Job):
    def task(self, user_id: int):
        # User might receive multiple emails if job retries!
        await send_email(user_id, "Welcome!")


# Better: Check if already sent
class SendWelcomeEmail(Job):
    def task(self, user_id: int):
        sent = await db.fetchval(
            "SELECT welcome_sent FROM users WHERE id = $1", user_id
        )
        if not sent:
            await send_email(user_id, "Welcome!")
            await db.execute(
                "UPDATE users SET welcome_sent = true WHERE id = $1", user_id
            )
```

### 5. Use Deadline Keys for Deduplication

```python
# Prevent duplicate scheduled jobs
deadline_key = f"billing-update:{user_id}:{date.today()}"
await conn.execute(
    """
    INSERT INTO jorb (job_class, kwargs, deadline_key, run_after)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (queue, deadline_key) WHERE state = 'queued' DO NOTHING
""",
    "job.billing.Update",
    ...,
    deadline_key,
    tomorrow_midnight,
)
```

### 6. Handle Web Requests Separately from Queue

```python
class MyJob(Job):
    async def web(self, request: web.Request) -> web.Response:
        """Direct HTTP handler (low latency)"""
        data = await request.json()

        # Quick validation
        if not valid(data):
            return web.Response(status=400)

        # Submit to queue for async processing
        await self.s.ex("schedule", ...)

        return web.Response(text="Accepted", status=202)

    async def task(self, **kwargs):
        """Actual work (from queue)"""
        # Heavy processing here
        pass
```

## Summary

The `JobSystem` class provides:

- ✅ **Robust job orchestration** with atomic claiming and state management
- ✅ **Hot reloading** of job classes for development
- ✅ **Worker-local caching** for performance
- ✅ **Optional web server** for direct job invocation
- ✅ **Automatic retry** with exponential backoff
- ✅ **Connection resilience** for database failures
- ✅ **Graceful shutdown** handling
- ✅ **Prepared statements** for safety and performance

It's the engine that powers pyjobby's simple yet powerful job queue system.
