"""
Example Jobs Demonstrating Pyjobby Improvements

These examples show how to use the new features:
- Custom timeouts
- Retry logic
- Error handling
- Job recovery
"""

import asyncio
import time
from pyjobby.pj import Job


class BasicJob(Job):
    """Simple job that always succeeds"""

    def task(self, message: str = "Hello World"):
        print(f"Processing: {message}")
        return {"status": "success", "message": message}


class FailingJob(Job):
    """Job that fails to demonstrate retry mechanism"""

    def task(self, fail_count: int = 1):
        current_attempt = self.job.get("error_count", 0) + 1

        if current_attempt <= fail_count:
            raise Exception(f"Intentional failure (attempt {current_attempt}/{fail_count})")

        return {"status": "success", "attempts": current_attempt}


class TimeoutJob(Job):
    """Job with custom timeout (5 seconds)"""

    timeout = 5  # Override default timeout

    async def task(self, sleep_duration: int = 10):
        print(f"Starting long operation ({sleep_duration}s)...")
        await asyncio.sleep(sleep_duration)
        return {"status": "completed"}


class LongRunningJob(Job):
    """Job with extended timeout for legitimate long operations"""

    timeout = 7200  # 2 hours

    async def task(self, data_size: int):
        print(f"Processing large dataset ({data_size} items)...")
        # Simulate long operation
        for i in range(data_size):
            await asyncio.sleep(0.1)
            if i % 100 == 0:
                print(f"Progress: {i}/{data_size}")

        return {"status": "completed", "items_processed": data_size}


class RetryableAPICall(Job):
    """Job that demonstrates smart retry logic"""

    timeout = 30

    async def task(self, url: str, retry_on_failure: bool = True):
        """Make API call with automatic retry on transient failures"""

        try:
            # Simulate API call
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
                    response.raise_for_status()
                    data = await response.json()
                    return {"status": "success", "data": data}

        except aiohttp.ClientResponseError as e:
            if e.status >= 500:
                # Server error - retryable
                raise Exception(f"Server error {e.status} - will retry")
            elif e.status == 429:
                # Rate limited - retryable
                raise Exception("Rate limited - will retry with backoff")
            else:
                # Client error - not retryable
                return {"status": "error", "code": e.status, "message": "Client error (not retried)"}

        except aiohttp.ClientError as e:
            # Network error - retryable
            raise Exception(f"Network error: {e}")


class ConditionalRetryJob(Job):
    """Job that decides whether to retry based on error type"""

    async def task(self, operation: str):
        if operation == "transient_failure":
            # This will retry
            raise Exception("Temporary issue - safe to retry")

        elif operation == "permanent_failure":
            # This should not retry - override rescheduleBackoff
            raise ValueError("Permanent issue - do not retry")

        elif operation == "success":
            return {"status": "success"}

    async def rescheduleBackoff(self, attempt=None):
        """Custom retry logic"""
        # Check if error is permanent
        if attempt is not None and attempt > 0:
            # For demo: don't retry ValueError
            # In real code, you'd inspect the exception type
            pass

        return await super().rescheduleBackoff(attempt)


class StreamingJob(Job):
    """Async generator job that streams results"""

    timeout = 300  # 5 minutes

    async def task(self, num_items: int = 10):
        """Process items and yield results as they complete"""
        for i in range(num_items):
            await asyncio.sleep(0.5)  # Simulate processing
            yield {
                "item": i,
                "status": "processed",
                "timestamp": time.time()
            }


class DatabaseTransactionJob(Job):
    """Job that uses database transactions"""

    async def task(self, user_id: int, amount: float):
        """Transfer funds with atomic transaction"""

        # Access the database connection from JobSystem
        async with self.s.cxn.transaction():
            # Debit source
            await self.s.cxn.execute(
                "UPDATE accounts SET balance = balance - $1 WHERE user_id = $2",
                amount, user_id
            )

            # Credit destination
            await self.s.cxn.execute(
                "UPDATE accounts SET balance = balance + $1 WHERE user_id = $2",
                amount, user_id + 1
            )

            # Log transaction
            tx_id = await self.s.cxn.fetchval(
                "INSERT INTO transactions (from_user, to_user, amount) VALUES ($1, $2, $3) RETURNING id",
                user_id, user_id + 1, amount
            )

        return {"status": "success", "transaction_id": tx_id}


class CachedResourceJob(Job):
    """Job that uses worker-local cache for expensive resources"""

    async def task(self, api_key: str, endpoint: str):
        """Make API call using cached HTTP session"""

        # Get or create cached session
        if "http_session" not in self.s.cache:
            import aiohttp
            self.s.cache["http_session"] = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {api_key}"}
            )
            print("Created new HTTP session (cached for worker)")

        session = self.s.cache["http_session"]

        # Use cached session
        async with session.get(endpoint) as response:
            data = await response.json()
            return {"status": "success", "data": data}


# Example job submission functions

async def submit_basic_job(db_conn):
    """Submit a simple job"""
    import orjson

    job_id = await db_conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue)
        VALUES ($1, $2, $3)
        RETURNING id
    """, "examples.jobs.example_jobs.BasicJob",
        orjson.dumps({"message": "Hello from pyjobby!"}),
        "default")

    print(f"Submitted job {job_id}")
    return job_id


async def submit_failing_job(db_conn, fail_count=3):
    """Submit a job that will fail and retry"""
    import orjson

    job_id = await db_conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue)
        VALUES ($1, $2, $3)
        RETURNING id
    """, "examples.jobs.example_jobs.FailingJob",
        orjson.dumps({"fail_count": fail_count}),
        "default")

    print(f"Submitted failing job {job_id} (will fail {fail_count} times then succeed)")
    return job_id


async def submit_timeout_job(db_conn):
    """Submit a job that will timeout"""
    import orjson

    job_id = await db_conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue)
        VALUES ($1, $2, $3)
        RETURNING id
    """, "examples.jobs.example_jobs.TimeoutJob",
        orjson.dumps({"sleep_duration": 10}),  # Will timeout after 5s
        "default")

    print(f"Submitted timeout job {job_id} (will timeout and retry)")
    return job_id


if __name__ == "__main__":
    print("""
Example Jobs for Pyjobby

To use these jobs:

1. Start workers:
   pj --workers 2 --path ./examples

2. Submit jobs using the submit_* functions above, or manually:
   psql -c "INSERT INTO jorb (job_class, kwargs) VALUES ('examples.jobs.example_jobs.BasicJob', '{}')"

3. Monitor job execution:
   psql -c "SELECT id, state, error_count, result FROM jorb ORDER BY id DESC LIMIT 10"

Features demonstrated:
- BasicJob: Simple synchronous job
- FailingJob: Demonstrates retry mechanism
- TimeoutJob: Custom timeout handling
- LongRunningJob: Extended timeout for legitimate long operations
- RetryableAPICall: Smart retry logic for API calls
- StreamingJob: Async generator with streaming results
- DatabaseTransactionJob: Atomic database operations
- CachedResourceJob: Worker-local resource caching
    """)
