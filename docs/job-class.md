# Job Base Class Documentation

## Overview

The `Job` class (`pyjobby/pj.py:502-576`) is the abstract base class for all user-defined job workers in pyjobby. It provides a simple interface for implementing work tasks while the `JobSystem` handles all orchestration, state management, and error handling.

## Class Definition

```python
@dataclass
class Job:
    """Parent class of all jobs run by JobSystem.

    User jobs subclass Job and override the task() method to
    run operations as needed."""

    s: JobSystem                # Reference to the JobSystem instance
    job: dict[str, Any]         # Job row data from database
```

**Key Attributes**:
- `s`: Access to the JobSystem that's running this job
  - Database connection: `self.s.cxn`
  - Worker cache: `self.s.cache`
  - Configuration: `self.s.config` (if loaded)
- `job`: Dictionary containing all columns from the job row
  - `job["id"]`: Job ID
  - `job["job_class"]`: Full class path
  - `job["kwargs"]`: Arguments to pass to task()
  - `job["state"]`: Current state
  - `job["queue"]`: Queue name
  - `job["prio"]`: Priority level
  - `job["uid"]`: User ID
  - `job["error_count"]`: Number of previous failures
  - See full schema in `priv/schema.py`

**Configurable Class Attributes** (Phase 1 Improvements):
- `timeout`: Maximum execution time in seconds (default: uses `JobSystem.default_timeout`)

```python
class LongRunningJob(Job):
    timeout = 7200  # 2 hours for this specific job type

    async def task(self, data_size: int):
        # This job can run for up to 2 hours
        # If it exceeds 7200 seconds, TimeoutError is raised
        # and the job is marked as crashed and retried
        pass
```

## Core Methods

### `task(**kwargs) -> Any`

Location: `pyjobby/pj.py:513-519`

**The method you must implement** in your job subclass. This contains your actual job logic.

**Parameters**: Extracted from `job["kwargs"]` JSONB column in database

**Returns**: Any serializable value (will be stored as JSONB in `result` column)

**Execution Modes**:

1. **Synchronous** (regular Python function):
```python
class SyncJob(Job):
    def task(self, url: str) -> dict:
        import requests
        response = requests.get(url)
        return response.json()
```

2. **Asynchronous** (coroutine):
```python
class AsyncJob(Job):
    async def task(self, url: str) -> dict:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.json()
```

3. **Async Generator** (streaming results):
```python
class StreamingJob(Job):
    async def task(self, urls: list[str]):
        for url in urls:
            data = await fetch_url(url)
            yield data  # Partial progress
            await asyncio.sleep(0.1)

# JobSystem collects all yielded values into a list
```

**Important Notes**:
- If task raises an exception, job is marked as `crashed` and rescheduled
- Return value is serialized to JSON and stored in `result` column
- Don't return large objects (>1MB) - use external storage and return a reference

### `run() -> Any`

Location: `pyjobby/pj.py:521-525`

Wrapper method that calls `task()` with kwargs from the database.

**Default Implementation**:
```python
def run(self) -> Any:
    """Call subclass .task() with arguments from DB"""
    return self.task(**self.job["kwargs"])
```

**You rarely need to override this**, but you can if you need custom argument handling:

```python
class CustomJob(Job):
    def run(self) -> Any:
        # Add extra context
        kwargs = self.job["kwargs"].copy()
        kwargs["job_id"] = self.job["id"]
        kwargs["user_id"] = self.job["uid"]
        return self.task(**kwargs)

    def task(self, data: str, job_id: int, user_id: int):
        # Now has access to job_id and user_id
        pass
```

### `async reschedule(relative: int, unit: str = "seconds") -> timedelta`

Location: `pyjobby/pj.py:547-576`

Reschedule this job to run at a future time.

**Parameters**:
- `relative`: Number of time units in the future
- `unit`: Time unit (default: "seconds")
  - Valid units: `"microseconds"`, `"milliseconds"`, `"seconds"`, `"minutes"`, `"hours"`, `"days"`, `"weeks"`
- `deltas`: Alternative dict format for multiple units

**Returns**: `datetime.timedelta` of the scheduled delay

**Examples**:

```python
class MyJob(Job):
    async def task(self, url: str):
        try:
            result = await fetch(url)
            return result
        except TemporaryError:
            # Retry in 5 minutes
            await self.reschedule(5, "minutes")
            raise  # Still mark as crashed

        except RateLimitError as e:
            # Retry after rate limit resets
            await self.reschedule(e.retry_after_seconds, "seconds")
            raise

# Advanced: Multiple units
await self.reschedule(deltas={
    "days": 1,
    "hours": 6,
    "minutes": 30
})  # 1 day, 6 hours, 30 minutes from now
```

**How it Works**:
1. Updates current job's `run_after` to future timestamp
2. Sets state to `queued`
3. Job becomes eligible when `run_after` passes
4. Workers will claim it again

**Use Cases**:
- Rate limiting (retry after limit resets)
- Business logic scheduling (retry during business hours)
- Dependency waiting (check again later if prerequisite not ready)

### `async rescheduleBackoff(attempt: int = None) -> timedelta`

Location: `pyjobby/pj.py:527-545`

Reschedule with exponential backoff (automatically called on job failure).

**Parameters**:
- `attempt`: Retry attempt number (default: uses `job["error_count"]`)

**Returns**: `datetime.timedelta` of the backoff delay

**Backoff Schedule**:
```python
attempt = 0: 16s  (2^4)
attempt = 1: 32s  (2^5)
attempt = 2: 64s  (2^6) = ~1 minute
attempt = 3: 128s (2^7) = ~2 minutes
attempt = 4: 256s (2^8) = ~4 minutes
attempt = 5: 512s (2^9) = ~8 minutes
attempt = 6+: 1024s (2^10) = ~17 minutes (capped)
```

**Implementation**:
```python
def rescheduleBackoff(self, attempt: Optional[int] = None) -> Awaitable[datetime.timedelta]:
    if attempt is None:
        attempt = self.job["error_count"]

    # Min 2^4 (16s), Max 2^10 (1024s = 17min)
    # Plus random jitter: 0-10 seconds
    delayFor = 2 ** min(max(4, attempt), 10) + (random.randint(0, 1000) / 100)
    return self.reschedule(delayFor, "seconds")
```

**Automatic Behavior**:
When a job raises an exception, JobSystem automatically calls `rescheduleBackoff()`:

```python
# From JobSystem.run() error handler
except Exception as e:
    if klass:
        rescheduleFor = await klass.rescheduleBackoff()
        logger.info(f"[job {job['id']}] Rescheduling to run in {rescheduleFor.total_seconds() / 60:.3f} minutes")
```

**Custom Backoff**:
```python
class MyJob(Job):
    async def rescheduleBackoff(self, attempt: int = None) -> timedelta:
        if attempt is None:
            attempt = self.job["error_count"]

        # Custom schedule: 1min, 5min, 15min, 1hour, 24hours
        delays = [60, 300, 900, 3600, 86400]
        seconds = delays[min(attempt, len(delays) - 1)]

        return await self.reschedule(seconds, "seconds")
```

## Job Lifecycle

### State Transitions

```
┌─────────┐
│ waiting │  (initial state for dependent jobs)
└────┬────┘
     │ Dependency satisfied
     ▼
┌─────────┐
│ queued  │  (initial state for independent jobs)
└────┬────┘
     │ Worker claims job (UPDATE ... FOR UPDATE SKIP LOCKED)
     ▼
┌─────────┐
│ claimed │
└────┬────┘
     │ Worker begins execution
     ▼
┌─────────┐
│ running │
└────┬────┘
     │
     ├──── Success ────────▶ ┌──────────┐
     │                       │ finished │
     │                       └──────────┘
     │
     └──── Exception ──────▶ ┌─────────┐
                             │ crashed │
                             └────┬────┘
                                  │ Auto-reschedule
                                  ▼
                             ┌─────────┐
                             │ queued  │ (with future run_after)
                             └─────────┘
```

### Complete Lifecycle Example

Let's trace a job from creation to completion:

```python
# 1. Job Submission
await db.execute("""
    INSERT INTO jorb (job_class, kwargs, queue, uid)
    VALUES ($1, $2, $3, $4)
""", "job.email.SendEmail",
    '{"to": "user@example.com", "subject": "Hello"}',
    "default", 12345)

# Database row created:
# {
#   "id": 1001,
#   "state": "queued",
#   "job_class": "job.email.SendEmail",
#   "kwargs": {"to": "user@example.com", "subject": "Hello"},
#   "queue": "default",
#   "prio": 0,
#   "uid": 12345,
#   "run_after": "2025-11-18 10:00:00",
#   "error_count": 0,
# }

# 2. Worker Claims Job (5-6 seconds later)
# JobSystem executes: await self.ex("claim", ...)
# UPDATE jorb SET state = 'claimed', worker_pid = 12345, worker_host = 'web-1' ...

# 3. Job Class Instantiation
klass = self.classForKlassFromName("job.email.SendEmail", job=job_row)
# Equivalent to:
# from job.email import SendEmail
# klass = SendEmail(s=self, job=job_row)

# 4. Execution Begins
await self.ex("run", job_id)  # UPDATE jorb SET state = 'running'

# 5. User Code Runs
result = await klass.run()
# -> calls klass.task(**job_row["kwargs"])
# -> returns {"status": "sent", "message_id": "abc123"}

# 6. Success
await self.ex("finished", job_id, result)
# UPDATE jorb SET state = 'finished', result = '{"status": "sent", ...}'

# 7. Trigger Dependent Jobs
await self.ex("enqueue-next-self-finished", job_id)
# UPDATE jorb SET state = 'queued' WHERE waitfor_job = 1001
```

### Error Flow

```python
# 1. Job Fails
class EmailJob(Job):
    async def task(self, to: str, subject: str):
        smtp = await connect_smtp()
        raise smtplib.SMTPServerDisconnected("Connection lost")  # Error!

# 2. Exception Caught by JobSystem
try:
    result = await klass.run()
except Exception as e:
    # 3. Mark as Crashed
    await self.ex("crash", job_id, str(e), traceback.format_exc())
    # UPDATE jorb SET state = 'crashed', error_message = '...',
    #                  error_backtrace = '...', error_count = error_count + 1

    # 4. Schedule Retry with Backoff
    delay = await klass.rescheduleBackoff()  # Returns timedelta(seconds=16)
    await self.ex("reschedule", job_id, delay)
    # UPDATE jorb SET state = 'queued', run_after = NOW() + '16 seconds'

# 5. Job Eligible Again in 16 Seconds
# Worker claims it again, retry #1 begins...
```

## Accessing JobSystem Features

### Database Queries

```python
class MyJob(Job):
    async def task(self, user_id: int):
        # Execute raw SQL
        user = await self.s.cxn.fetchrow(
            "SELECT * FROM users WHERE id = $1", user_id)

        # Use prepared statements
        result = await self.s.ex("custom-query", user_id)

        return {"user": dict(user)}
```

### Worker Cache

```python
class MyJob(Job):
    async def task(self, api_key: str):
        # Get or create cached client
        if "api_client" not in self.s.cache:
            self.s.cache["api_client"] = APIClient(api_key)
            logger.info("Created new API client")

        client = self.s.cache["api_client"]
        data = await client.fetch_data()

        return data
```

### Configuration

```python
class MyJob(Job):
    def task(self, **kwargs):
        # Access loaded configuration
        storage_path = self.s.config.storage_path
        api_key = self.s.config.api_key

        # Use configuration values
        with open(f"{storage_path}/output.txt", "w") as f:
            f.write("result")
```

### Job Metadata

```python
class MyJob(Job):
    def task(self, data: str):
        # Access job metadata
        job_id = self.job["id"]
        user_id = self.job["uid"]
        priority = self.job["prio"]
        attempt = self.job["error_count"]
        queue = self.job["queue"]

        logger.info(f"Job {job_id} for user {user_id} (attempt {attempt})")

        # Conditional logic based on metadata
        if attempt > 3:
            logger.warning("Many retries, switching to fallback method")
            return self.fallback_method(data)

        return self.primary_method(data)
```

## Real-World Job Examples

### Example 1: Simple Synchronous Job

```python
# job/hello.py
from pyjobby.pj import Job

class HelloWorld(Job):
    def task(self, name: str = "World"):
        """Synchronous job (no async needed)"""
        message = f"Hello, {name}!"
        print(message)
        return {"message": message}

# Submit job
await db.execute("""
    INSERT INTO jorb (job_class, kwargs)
    VALUES ('job.hello.HelloWorld', '{"name": "Alice"}')
""")
```

### Example 2: Async Job with External API

```python
# job/weather.py
from pyjobby.pj import Job
import aiohttp

class FetchWeather(Job):
    async def task(self, city: str, country_code: str = "US"):
        """Fetch weather data from external API"""

        # Reuse HTTP session from cache
        if "http_session" not in self.s.cache:
            self.s.cache["http_session"] = aiohttp.ClientSession()

        session = self.s.cache["http_session"]

        api_key = self.s.config.openweather_api_key
        url = f"https://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": f"{city},{country_code}",
            "appid": api_key,
            "units": "metric"
        }

        try:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()

                return {
                    "city": city,
                    "temperature": data["main"]["temp"],
                    "conditions": data["weather"][0]["description"],
                    "humidity": data["main"]["humidity"],
                }

        except aiohttp.ClientError as e:
            # Network error - will retry with backoff
            logger.error(f"Failed to fetch weather for {city}: {e}")
            raise

# Submit job
await db.execute("""
    INSERT INTO jorb (job_class, kwargs, queue)
    VALUES ('job.weather.FetchWeather',
            '{"city": "San Francisco", "country_code": "US"}',
            'default')
""")
```

### Example 3: Streaming Job (Async Generator)

```python
# job/batch.py
from pyjobby.pj import Job
import asyncio

class ProcessBatch(Job):
    async def task(self, item_ids: list[int]):
        """Process items in batches, yielding progress"""

        total = len(item_ids)
        for i, item_id in enumerate(item_ids):
            # Process item
            result = await self.process_item(item_id)

            # Yield progress update
            yield {
                "item_id": item_id,
                "result": result,
                "progress": f"{i+1}/{total}",
                "percent": (i+1) / total * 100
            }

            # Small delay to avoid overwhelming downstream services
            await asyncio.sleep(0.1)

        # Final result
        yield {"status": "complete", "total_processed": total}

    async def process_item(self, item_id: int):
        # Fetch item from database
        item = await self.s.cxn.fetchrow(
            "SELECT * FROM items WHERE id = $1", item_id)

        # Do some processing
        await asyncio.sleep(0.5)  # Simulate work

        # Update item status
        await self.s.cxn.execute(
            "UPDATE items SET processed = true WHERE id = $1", item_id)

        return {"id": item_id, "status": "processed"}

# Submit job
await db.execute("""
    INSERT INTO jorb (job_class, kwargs, queue)
    VALUES ('job.batch.ProcessBatch',
            '{"item_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}',
            'default')
""")

# Result stored in database (list of all yielded values):
# [
#   {"item_id": 1, "result": {...}, "progress": "1/10", "percent": 10.0},
#   {"item_id": 2, "result": {...}, "progress": "2/10", "percent": 20.0},
#   ...
#   {"status": "complete", "total_processed": 10}
# ]
```

### Example 4: Job with Custom Retry Logic

```python
# job/api_call.py
from pyjobby.pj import Job
import aiohttp

class CallExternalAPI(Job):
    async def task(self, endpoint: str, data: dict):
        """Call external API with intelligent retry logic"""

        try:
            response = await self.make_api_call(endpoint, data)
            return response

        except aiohttp.ClientResponseError as e:
            if e.status == 429:  # Rate limited
                # Parse retry-after header
                retry_after = int(e.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited, retrying in {retry_after}s")
                await self.reschedule(retry_after, "seconds")
                raise

            elif e.status >= 500:  # Server error
                # Temporary error, use exponential backoff
                logger.error(f"Server error {e.status}, will retry with backoff")
                raise

            elif e.status >= 400:  # Client error
                # Permanent error, don't retry
                logger.error(f"Client error {e.status}, not retrying")
                await self.mark_as_permanent_failure(endpoint, e.status)
                return {"error": "permanent_failure", "status": e.status}

    async def make_api_call(self, endpoint: str, data: dict):
        api_base = self.s.config.api_base_url
        api_key = self.s.config.api_key

        if "http_session" not in self.s.cache:
            self.s.cache["http_session"] = aiohttp.ClientSession()

        session = self.s.cache["http_session"]

        async with session.post(
            f"{api_base}/{endpoint}",
            json=data,
            headers={"Authorization": f"Bearer {api_key}"}
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def mark_as_permanent_failure(self, endpoint: str, status: int):
        # Record permanent failure in database
        await self.s.cxn.execute("""
            INSERT INTO api_failures (job_id, endpoint, status_code, timestamp)
            VALUES ($1, $2, $3, NOW())
        """, self.job["id"], endpoint, status)

    async def rescheduleBackoff(self, attempt: int = None) -> datetime.timedelta:
        """Custom backoff: give up after 5 attempts"""
        if attempt is None:
            attempt = self.job["error_count"]

        if attempt >= 5:
            logger.error("Too many retries, giving up")
            await self.mark_as_permanent_failure("unknown", 0)
            # Don't reschedule (job stays crashed)
            return datetime.timedelta(seconds=0)

        # Standard exponential backoff for attempts < 5
        return await super().rescheduleBackoff(attempt)
```

### Example 5: Job with Database Transaction

```python
# job/transfer.py
from pyjobby.pj import Job

class TransferFunds(Job):
    async def task(self, from_account: int, to_account: int, amount: float):
        """Transfer funds between accounts (atomic transaction)"""

        # Use database transaction for atomicity
        async with self.s.cxn.transaction():
            # Check source balance
            balance = await self.s.cxn.fetchval(
                "SELECT balance FROM accounts WHERE id = $1 FOR UPDATE",
                from_account)

            if balance < amount:
                raise ValueError(f"Insufficient funds: {balance} < {amount}")

            # Debit source
            await self.s.cxn.execute(
                "UPDATE accounts SET balance = balance - $1 WHERE id = $2",
                amount, from_account)

            # Credit destination
            await self.s.cxn.execute(
                "UPDATE accounts SET balance = balance + $1 WHERE id = $2",
                amount, to_account)

            # Record transaction
            tx_id = await self.s.cxn.fetchval("""
                INSERT INTO transactions (from_account, to_account, amount, timestamp)
                VALUES ($1, $2, $3, NOW())
                RETURNING id
            """, from_account, to_account, amount)

        return {
            "transaction_id": tx_id,
            "from_account": from_account,
            "to_account": to_account,
            "amount": amount,
            "status": "completed"
        }
```

### Example 6: Job with Web Handler

```python
# job/image.py
from pyjobby.pj import Job
from aiohttp import web
from PIL import Image
import io

class GenerateThumbnail(Job):
    async def web(self, request: web.Request) -> web.Response:
        """Handle direct HTTP request (synchronous processing)"""

        # Read uploaded file
        data = await request.post()
        image_field = data["image"]
        image_data = image_field.file.read()

        # Get size from query params
        size = int(request.query.get("size", 300))

        # Generate thumbnail immediately (no queue)
        thumbnail = await self.task(image_data=image_data, size=size)

        # Return thumbnail
        return web.Response(
            body=thumbnail,
            content_type="image/jpeg",
            headers={"Content-Disposition": f"attachment; filename=thumbnail.jpg"}
        )

    async def task(self, image_data: bytes = None, filepath: str = None, size: int = 300):
        """Can also be called via queue with filepath"""

        # Load image from data or file
        if image_data:
            img = Image.open(io.BytesIO(image_data))
        elif filepath:
            img = Image.open(filepath)
        else:
            raise ValueError("Must provide image_data or filepath")

        # Generate thumbnail
        img.thumbnail((size, size))

        # Save to bytes
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85)
        output.seek(0)

        return output.read()

# Configuration (in pyjobby.conf.py)
# web_listen = {
#     "sites": [{"host": "0.0.0.0", "port": 8080}],
#     "paths": {"job.image.GenerateThumbnail"}
# }

# Usage:
# POST http://localhost:8080/job.image.GenerateThumbnail?size=150
# Form-Data: image=@photo.jpg
```

## Advanced Patterns

### Pattern 1: Fan-Out/Fan-In (Parallel Processing)

```python
# job/coordinator.py
from pyjobby.pj import Job
import secrets

class DataPipeline(Job):
    async def task(self, dataset_id: int, operations: list[str]):
        """Coordinate parallel data processing pipeline"""

        # Create unique group ID
        group_id = secrets.randbits(63)

        # Fan-out: Create parallel jobs for each operation
        for operation in operations:
            await self.s.cxn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, run_group, uid)
                VALUES ($1, $2, $3, $4, $5)
            """, f"job.data.{operation}",
                orjson.dumps({"dataset_id": dataset_id}),
                "default", group_id, self.job["uid"])

        # Fan-in: Create aggregation job that waits for all operations
        await self.s.cxn.execute("""
            INSERT INTO jorb (job_class, kwargs, queue, state, waitfor_group, uid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, "job.data.AggregateResults",
            orjson.dumps({"dataset_id": dataset_id, "group_id": group_id}),
            "default", "waiting", group_id, self.job["uid"])

        return {"group_id": group_id, "operations": operations}

class AggregateResults(Job):
    async def task(self, dataset_id: int, group_id: int):
        """Aggregate results from all parallel operations"""

        # Fetch results from all jobs in group
        results = await self.s.cxn.fetch("""
            SELECT job_class, result
            FROM jorb
            WHERE run_group = $1 AND state = 'finished'
        """, group_id)

        # Combine results
        aggregated = {
            row["job_class"]: row["result"]
            for row in results
        }

        # Store final result
        await self.s.cxn.execute("""
            UPDATE datasets
            SET processed_data = $1, status = 'complete'
            WHERE id = $2
        """, orjson.dumps(aggregated), dataset_id)

        return aggregated
```

### Pattern 2: Cron-Like Scheduling

```python
# job/cron.py
from pyjobby.pj import Job
import datetime

class DailyReport(Job):
    async def task(self, report_type: str):
        """Generate daily report and schedule next run"""

        # Generate report for yesterday
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        report = await self.generate_report(report_type, yesterday)

        # Store report
        await self.store_report(report)

        # Schedule next run for tomorrow
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        tomorrow_midnight = datetime.datetime.combine(tomorrow, datetime.time.min)

        deadline_key = f"{report_type}:{tomorrow.isoformat()}"

        try:
            await self.s.cxn.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, run_after, deadline_key)
                VALUES ($1, $2, $3, $4, $5)
            """, "job.cron.DailyReport",
                orjson.dumps({"report_type": report_type}),
                "default", tomorrow_midnight, deadline_key)
        except:
            # Already scheduled
            pass

        return {"report_date": yesterday.isoformat(), "next_run": tomorrow_midnight}

    async def generate_report(self, report_type: str, date: datetime.date):
        # Generate report...
        pass

    async def store_report(self, report: dict):
        # Store report...
        pass
```

### Pattern 3: Circuit Breaker

```python
# job/resilient.py
from pyjobby.pj import Job
import time

class ResilientAPICall(Job):
    async def task(self, endpoint: str, data: dict):
        """API call with circuit breaker pattern"""

        circuit_key = f"circuit:{endpoint}"

        # Check circuit breaker state
        circuit = self.s.cache.get(circuit_key, {
            "state": "closed",  # closed = normal, open = failing
            "failures": 0,
            "last_failure": 0,
            "opened_at": 0
        })

        # If circuit is open, check if we should try again
        if circuit["state"] == "open":
            time_since_open = time.time() - circuit["opened_at"]
            if time_since_open < 60:  # Wait 60s before retry
                raise Exception(f"Circuit breaker open for {endpoint}")

            # Try to close circuit (half-open state)
            circuit["state"] = "half-open"

        try:
            # Make API call
            result = await self.call_api(endpoint, data)

            # Success - close circuit
            circuit["state"] = "closed"
            circuit["failures"] = 0
            self.s.cache[circuit_key] = circuit

            return result

        except Exception as e:
            # Failure - increment counter
            circuit["failures"] += 1
            circuit["last_failure"] = time.time()

            # Open circuit if too many failures
            if circuit["failures"] >= 5:
                circuit["state"] = "open"
                circuit["opened_at"] = time.time()
                logger.error(f"Circuit breaker opened for {endpoint}")

            self.s.cache[circuit_key] = circuit
            raise
```

## Best Practices

### 1. Keep Jobs Idempotent

```python
# Good: Can be run multiple times safely
class UpdateUserEmail(Job):
    def task(self, user_id: int, new_email: str):
        await self.s.cxn.execute(
            "UPDATE users SET email = $1 WHERE id = $2",
            new_email, user_id)

# Bad: Creates duplicate records on retry
class CreateUser(Job):
    def task(self, username: str, email: str):
        await self.s.cxn.execute(
            "INSERT INTO users (username, email) VALUES ($1, $2)",
            username, email)  # Fails on retry (unique constraint)

# Better: Check if exists first
class CreateUser(Job):
    def task(self, username: str, email: str):
        existing = await self.s.cxn.fetchval(
            "SELECT id FROM users WHERE username = $1", username)

        if existing:
            return {"id": existing, "created": False}

        user_id = await self.s.cxn.fetchval(
            "INSERT INTO users (username, email) VALUES ($1, $2) RETURNING id",
            username, email)

        return {"id": user_id, "created": True}
```

### 2. Use Cache for Expensive Resources

```python
# Good: Reuse connections
class MyJob(Job):
    def task(self):
        if "db_pool" not in self.s.cache:
            self.s.cache["db_pool"] = create_pool()
        return self.s.cache["db_pool"].query(...)

# Bad: Create new connection every time
class MyJob(Job):
    def task(self):
        pool = create_pool()  # Expensive!
        return pool.query(...)
```

### 3. Return Meaningful Results

```python
# Good: Useful result for debugging/auditing
class ProcessOrder(Job):
    def task(self, order_id: int):
        return {
            "order_id": order_id,
            "status": "shipped",
            "tracking_number": "1Z999AA10123456784",
            "timestamp": datetime.utcnow().isoformat(),
            "items_count": 3
        }

# Bad: No useful information
class ProcessOrder(Job):
    def task(self, order_id: int):
        # Do work...
        return True  # Unhelpful!
```

### 4. Handle Cleanup Properly

```python
class FileProcessing(Job):
    async def task(self, filepath: str):
        temp_file = None
        try:
            # Create temp file
            temp_file = await self.create_temp_copy(filepath)

            # Process file
            result = await self.process_file(temp_file)

            return result

        finally:
            # Always cleanup (even if job fails)
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)
```

### 5. Log Appropriately

```python
class MyJob(Job):
    def task(self, user_id: int, action: str):
        logger.info(f"Processing {action} for user {user_id} (job {self.job['id']})")

        try:
            result = self.do_action(action, user_id)
            logger.info(f"Successfully completed {action} for user {user_id}")
            return result

        except Exception as e:
            # Don't log full traceback (JobSystem already does this)
            logger.error(f"Failed {action} for user {user_id}: {e}")
            raise
```

## Summary

The `Job` base class provides:

- ✅ **Simple interface**: Override one method (`task`)
- ✅ **Flexible execution**: Sync, async, or async generator
- ✅ **Automatic retry**: Exponential backoff on failure
- ✅ **Access to system**: Database, cache, configuration
- ✅ **Custom scheduling**: Reschedule jobs programmatically
- ✅ **Job metadata**: Access to all job properties
- ✅ **Hot reloading**: Code changes picked up automatically

Your job classes should focus on business logic while pyjobby handles orchestration, state management, and reliability.
