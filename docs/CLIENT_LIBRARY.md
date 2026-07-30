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

Pyjobby is installed from git (it is not published on PyPI):

```bash
# uv
uv add git+https://github.com/mattsta/pyjobby.git

# pip
pip install "git+https://github.com/mattsta/pyjobby.git@main"

# poetry
poetry add git+https://github.com/mattsta/pyjobby.git@main
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
async with await JobClient.from_config("./pyjobby.toml") as client:
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
client = await JobClient.from_config("./pyjobby.toml")

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

### Security: `job_class` is trusted input

`job_class` is a dotted import path, and the worker resolves it by importing
the named module before checking that the target is a `Job` subclass. That
import runs the module's top-level code. Being able to set `job_class` is
therefore equivalent to being able to name any importable module for the
worker to import — so `job_class` must come from your own code, never
straight from an untrusted source (a request body, a webhook payload). Put a
whitelist of allowed job names in front of `enqueue()` if end users can
influence what gets enqueued.

---

## API Reference

### One-call workflows

```python
# Enqueue and wait — request/response in one call. Raises what
# wait_for_result raises (JobFailedError, JobCancelledError, TimeoutError).
# `timeout` here is the WAIT budget, consumed by run() itself — it is never
# passed to task() (the payload/option collision rule below applies).
report = await client.run("myapp.jobs.Report", day="mon", timeout=60)

# Cancel and wait for the cancellation to LAND. 'cancel_requested' is a
# promise, not an outcome: this returns the terminal state — 'cancelled',
# or 'finished'/'crashed' when the job outran the cancel — or None when
# there was nothing to cancel. It never raises TimeoutError: if the wait
# elapses (the default is finite), it returns the still-live state instead.
final = await client.cancel_and_wait(job_id, timeout=30)

# Await a whole fan-out. Returns the member count when every job in the
# group finished; raises if a member crashed/was cancelled (the group can
# then never finish) or the group has no members.
job_ids, group_id = await client.create_fan_out("myapp.jobs.Resize", items)
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
- `priority` (int): Priority as a **finishing position** — a _smaller_
  number runs _sooner_, the way `priority=1` means "first" in a race
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
- `deadline_key` (str): Idempotency key that collapses duplicate submissions
  of work that has **not started** — one _queued_ row per
  `(deadline_key, queue)`, so a second enqueue while the first is still
  queued raises `asyncpg.UniqueViolationError`, and the key re-arms once a
  worker claims the job (default: None). Refused in combination with
  `identity_key` and with `waitfor_job`/`waitfor_group` (`ValueError`): a
  dependent job is inserted `waiting`, which is outside the unique index, and
  the wake clears `deadline_key` on the way into `queued` — so the key would
  never collapse anything at any point in the row's life. Put it on the job
  that does the work and let an unidentified waiter depend on that job. See
  [Idempotent jobs](#4-idempotent-jobs-deadline-keys).
- `identity_key` (str): This exact work happens **at most once**. Unique
  across _every_ state, so if a job with this key already exists — queued,
  running, finished, crashed — the enqueue returns **that job's id** instead
  of writing a second row (default: None). It does not raise on the DUPLICATE,
  which is the point; it does raise `ValueError` when the existing job is a
  different `job_class` than the one you asked for, and
  `SpeculativeEnqueueExhausted` (a `RuntimeError`, exported from `pyjobby`,
  carrying `.kind` and `.key`) in the pathological case where a stream of
  other writers claims the key past the speculative retry budget — the other
  cause of which is calling from a REPEATABLE READ transaction, where a retry
  reuses the snapshot and can never see the row. `debounce()` raises the same
  type for the same reason. Refused in combination with `deadline_key`,
  `debounce_key`, `waitfor_job`/`waitfor_group` and DAG nodes — see
  [At-most-once work](#4b-at-most-once-work-identity-keys) for why each.
  Bounded by retention.
- `partition_key` (str): The fair-share **lane** this job belongs to — a
  tenant, an account, an api key (default: None). Inert labelling unless the
  job's queue has `partition_limits` set
  (`pj-admin queues limits QUEUE --partition-limits`), and on such a queue
  that queue's `max_concurrency` and `rate_limit` are counted **per key**, so
  one tenant filling its own share cannot starve the rest. Jobs with no key
  form **one lane of their own** — never hidden, never refused for being
  unlabelled. Inherited by a fork, like `uid` and `tags`. See
  [Queue controls](OPERATIONS.md#queue-controls-what-the-limits-actually-promise).
- `app_version` (str): **Pin** this job to a code version — only a worker
  advertising the same `pj --app-version` will claim it (default: None, which
  means this client's declared version, and unpinned when it declared none).
  An unpinned job is claimed by every worker, versioned ones included, so the
  job opts in and the worker never does. Kept across retry and rerun (same
  row, same code); **not** inherited by a fork. Empty strings and versions
  longer than 128 characters raise `ValueError`. See
  [Pinning work to a code version](#7b-pinning-work-to-a-code-version).

  > **Every caller-chosen key is validated the same way.** `deadline_key`,
  > `identity_key`, `debounce_key` and `partition_key` must be **non-empty**
  > and at most **256 characters**; anything else raises `ValueError` before
  > any row is written. Empty is refused rather than stored because `''` is a
  > real value, not the absence of one: it takes a slot in that column's index,
  > so every other caller who passed an empty key collides with it — unrelated
  > jobs deduplicating against each other, or sharing one fair-share lane. It
  > is almost always an f-string over a value that was missing. `None` (the
  > default) is how a job says it is not using the feature.

- `admin_data` (dict): Metadata for tracking (default: None)
- `tags` (dict): Your own labels — customer, region, batch — that you can
  filter jobs by later (default: None). See [Job Tags](#8-job-tags).
- `save_result` (bool): Store the job's return value (default: True)
- `use_result_from` (int): Inject that job's result as `upstream_result`
  (default: None). Pair with `waitfor_job` so it has finished first.
- `retry_strategy` (str): `'exponential'`, `'linear'`, `'fibonacci'`,
  `'quadratic'` or `'fixed'` (default: `'exponential'`)
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

**Returns:** Job ID (int) — of the job this call created, or, when
`identity_key` names a job that already exists, of that one.

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

# At-most-once job — every call returns the SAME id while the row lives
job_id = await client.enqueue(
    "myapp.jobs.ShipOrder",
    identity_key=f"order:{order_id}:ship",
    order_id=order_id,
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

A batch refuses two `enqueue()` options, shared or per-job, for two different
reasons. `identity_key`: a batch is one INSERT returning one id per row **in
order**, and an identity that already exists has no row in it to return —
enqueue identified jobs one at a time with `enqueue_identified()`.
`debounce_key`: a batch has no bounce statement in front of its INSERT, so a
key already held would violate `jorb_debounce_idx` and take the whole batch
down instead of collapsing — call `debounce()` per key.

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

Cancel a job wherever it is in its lifecycle. Returns `{"job_id", "status"}`
with status `'cancelled'` (queued/waiting jobs stop immediately),
`'cancel_requested'` (a running worker stops at its next await point), or
`'not_cancellable'` — which is also the answer for a job that does not
exist, since it is not running either way.

```python
result = await client.cancel_job(12345)
if result["status"] == "not_cancellable":
    print("Job not found or already terminal")
else:
    print(f"Cancel: {result['status']}")
```

#### `retry_job(job_id)`

Retry a crashed or cancelled job. The job keeps its id — the same row is
requeued and the per-attempt history lives in `jorb_history`. Returns
`{"job_id", "status"}` with status `'requeued'`, or `'not_retriable'` when
the job is missing or in a state retry refuses (a job that already finished
is not retriable; see `rerun_job`).

```python
if (await client.retry_job(12345))["status"] == "requeued":
    print("Job requeued")
```

#### `rerun_job(job_id, *, fresh=True)`

Run a terminal job again, including one that already finished (repeating its
side effects). Returns `{"job_id", "status", "fresh"}` with status
`'requeued'` or `'not_rerunnable'`; `fresh` echoes the mode asked for —
`True` wipes DXE checkpoints **and the job's durable streams** and restarts
from step 1 (so the new run's stream starts at seq 0 instead of being appended
to the last run's), `False` resumes from both.

#### `fork_job(job_id, *, from_step=1, queue=None, priority=None, kwargs_override=None, app_version=None)`

Create a **NEW** job that re-executes this one's work from `from_step`, with
steps `1..from_step-1` copied in as checkpoints so they fast-forward. The
third verb, and the only one that does not reuse the row: `retry_job` and
`rerun_job` requeue the same id, a fork leaves the source completely alone —
any state, including `running`.

`from_step` is 1-based and names the step the fork **executes first**, so
`from_step=4` copies three checkpoints and `from_step=1` (the default)
copies none. Returns `{"job_id", "source_job_id", "from_step",
"steps_copied", "queue", "priority"}`, where `job_id` is the new job.

```python
fork = await client.fork_job(12345, from_step=4, priority=10)
print(f"job {fork['job_id']} skips {fork['steps_copied']} step(s)")
result = await client.wait_for_result(fork["job_id"])
```

The fork inherits the job class, arguments, queue, priority, capability,
retry/timeout policy, and everything that says **whose** work it is —
`uid`, `tags` and `partition_key`, so a tenant's fork stays theirs and
stays in the same fair-share lane. It does **not** inherit identity or
structure: `deadline_key`, `identity_key`, `debounce_key`, `schedule_id`,
DAG membership and dependency edges are left unset, because two live rows
sharing an idempotency key would make that key mean nothing — and an
`identity_key` most of all, since its whole promise is that the row holding
it is the only one. It does not inherit the source's `app_version` either, nor
this client's: a fork is usually how work is re-run under **new** code, so
inheriting a pin would strand the fork on the build you just replaced — pass
`app_version=` to pin it deliberately. Streams, events and
mailbox messages are the source's output and are not copied either — see
[DXE.md](DXE.md#forking-a-job-a-new-row-from-a-checkpoint-prefix).

Raises `ForkRefused` (`from pyjobby import ForkRefused`, a `ValueError`)
when there is no such job, when `from_step`
is below 1, or when it is past the source's recorded step count + 1 — and
`ValueError` for a `priority` above this client's worker ceiling, the same
refusal `enqueue` makes.

#### `fork_job_from_failure(job_id, *, queue=None, priority=None, kwargs_override=None, app_version=None)`

`fork_job` from the first step whose checkpoint recorded an error — the
incident shape: deploy the fix, fork the crashed job from the step that
broke, and the completed prefix is not paid for twice. Raises `ForkRefused`
when no step recorded a failure (a job that crashed outside its steps has no
failing step to start from).

```python
fork = await client.fork_job_from_failure(12345)
```

### Queue Operations

#### `queue_depth(queue=None)`

How many jobs are claimable right now — every queue by default, or one
named queue. Jobs parked in the future (retry backoff, `run_after`) are not
counted: they are not waiting for a worker.

```python
depth = await client.queue_depth("emails")
print(f"Queue has {depth} jobs waiting")
```

#### `queue_stats(queue=None, window=timedelta(hours=1))`

Per-state counts — every queue aggregated by default, or one named queue.
Live states (`queued`, `scheduled`, `claimed`, `running`, `waiting`) are
counted exactly; terminal states (`finished`, `crashed`, `cancelled`) are
counted only within `window` (default: the last hour), a recent-activity
number rather than an all-time total.

`queued` means claimable **now**; a job deferred to the future is reported
as `scheduled` and is deliberately not counted as backlog.

```python
from datetime import timedelta

stats = await client.queue_stats("emails", window=timedelta(hours=24))
print(f"Queued: {stats['queued']}")
print(f"Running: {stats['running']}")
print(f"Finished (last 24h): {stats['finished']}")
print(f"Crashed (last 24h): {stats['crashed']}")
```

### Reading a job's stream

#### `read_stream(job_id, key, *, offset=0)`

Consume a job's durable stream as it is written. Jobs append with
`await self.stream_write(key, value)`; this is the reader, an async generator
that yields values in order and returns when the stream ends.

```python
async for row in client.read_stream(job_id, "progress"):
    print(row)
```

It stops on the closing marker the job wrote (`stream_close`), or on the job
reaching a terminal state — a crashed or cancelled job ends its readers even
though no marker exists, and one final read happens after that state is
observed so nothing committed in the window is lost. A job that does not
exist raises `JobError` immediately rather than waiting for a stream nothing
will write.

Positions are dense and 0-based, so resuming is arithmetic: count what you
consumed and pass it back.

```python
seen = 0
async for row in client.read_stream(job_id, "progress"):
    seen += 1
    render(row)

# ...and after a disconnect, pick up exactly where that left off
async for row in client.read_stream(job_id, "progress", offset=seen):
    seen += 1
    render(row)
```

There is no `timeout=`: the read lasts as long as the job does, and a caller
that needs a bound puts one around the loop.

```python
async with asyncio.timeout(60):
    async for row in client.read_stream(job_id, "progress"):
        render(row)
```

The sync twin is a plain generator: `for row in sync_client.read_stream(...)`.

#### `get_stream(job_id, key)`

The snapshot form — everything written so far, and whether the stream is
closed — for a caller that wants a value rather than a feed.

```python
snapshot = await client.get_stream(job_id, "progress")
snapshot["values"]  # [{'pct': 10}, {'pct': 20}, ...]
snapshot["closed"]  # True once the job called stream_close()
```

An unwritten key is `{"values": [], "closed": False}`, not an error: a
snapshot is a query, so there is nothing to wait in vain for.

### Health Check

#### `health_check()`

Check database connection health.

```python
if await client.health_check():
    print("Database healthy")
else:
    print("Database connection failed")
```

### More methods

The rest of the public surface, one line each. Every async method has a
synchronous twin on `SyncJobClient` with the same signature, except the two
marked "async only" below.

**Inspection**

- `get_job_full(job_id)` — complete row: kwargs, result, error, timestamps.
- `get_job_by_identity(identity_key)` — the job holding that at-most-once key, or None (never enqueued, or already reaped by retention — the two are deliberately the same answer).
- `get_job_result(job_id)` — a finished job's stored result, without waiting.
- `wait_for_result(job_id, timeout=None)` — block until the job is terminal and return its result; raises `JobFailedError` / `JobCancelledError` / `TimeoutError`. The by-id form of `run()`, for a job enqueued earlier.
- `get_steps(job_id)` — a job's recorded DXE checkpoints, oldest first.
- `get_jobs(queue=None, state=None, limit=100, offset=0, order_by='created', ascending=False)` — list jobs, filtered and paginated.
- `get_failed_jobs(queue=None, limit=100)` / `get_waiting_jobs(limit=100)` — filtered views of `get_jobs`.
- `list_queues(window=timedelta(hours=1))` — every queue with per-state counts (same contract as `queue_stats`).

**Events & mail** (see also State Machines below)

- `send_message(dest_job_id, message, topic=None)` — put a durable message in a job's mailbox.
- `get_event(job_id, key, timeout=None)` — wait for a job's published event value.
- `wait_for_event(job_id, key, accept=None, timeout=None)` — wait until the event exists _and_ satisfies `accept`.
- `read_stream(job_id, key, offset=0)` — yield a job's stream values in order, as they are written (an async generator; the sync twin is a plain generator).
- `get_stream(job_id, key)` — everything written to that stream so far, and whether it is closed.

**Bulk operations** (the single-job verbs over a list of ids)

- `bulk_retry(job_ids)`, `bulk_cancel(job_ids)`, `bulk_delete(job_ids)`, `bulk_update_priority(job_ids, new_priority)`.
- `delete_job(job_id)`, `purge_queue(queue, states=None)` — delete one job, or a queue's jobs by state.

**Changing a queued job**

- `update_job_priority(job_id, new_priority)` — re-prioritise a **queued or waiting** job; validated against this client's `prio_ceiling` for the same reason `enqueue` is.
- `update_job_app_version(job_id, app_version)` — re-pin (or, with `None`, unpin) a **queued or waiting** job. The remedy for work stranded by a deploy that moved past its pin; see [Pinning work to a code version](#7b-pinning-work-to-a-code-version).

**Advanced enqueue**

- `enqueue_identified(job_class, *, identity_key, ...)` — the at-most-once enqueue, returning `(job_id, created)` so a caller can tell "I started this" from "this was already under way". Plain `enqueue(..., identity_key=...)` does the same write and returns the bare id.
- `debounce(job_class, *, key, period, cap=None, ...)` — collapse a burst of equivalent enqueues onto one job that fires `period` seconds after the last of them, carrying that last call's arguments; returns `(job_id, created)`. See [Debouncing a burst](#4c-debouncing-a-burst-debounce).
- `enqueue_handle(...)` — enqueue and get a `JobHandle` (`.wait()` — alias `.result()` —, `.status()`, `.cancel()`, `.event()`) (async only; a handle's own methods are coroutines bound to the async client, so `run()` / `wait_for_result()` are the sync shapes of this workflow).
- `enqueue_in_transaction(conn, ...)` — enqueue on the CALLER's asyncpg connection, inside their transaction (async only; no sync twin). Accepts `identity_key` (the identified statement runs inside your transaction and returns the existing job's id when the key is held); **refuses** `debounce_key`, which needs a bounce-or-insert pair this path does not run.
- `create_pipeline_with_results(stages, ...)` — a pipeline where each stage receives the previous stage's result.

**Property**

- `listening` — True when this client can ride LISTEN/NOTIFY for its waits (constructed with `db_params`) rather than polling only.

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

### 2b. DAG Workflows (Arbitrary Dependencies)

When the dependency graph is neither a straight pipeline nor a single
fan-out, build it explicitly. `client.dag()` returns a builder; `dag.add()`
returns a node you pass to a later job's `depends_on`; `execute_dag()` writes
every job at once and returns a node → job_id map.

```python
dag = client.dag("nightly-report", queue="reports")

extract = dag.add("etl.Extract", {"source": "sales"})
transform = dag.add("etl.Transform", depends_on=[extract])
load = dag.add("etl.Load", depends_on=[transform])
notify = dag.add("etl.Notify", depends_on=[load])

nodes = await client.execute_dag(dag)  # {node: job_id}
dag_id = nodes[extract]  # any node's job id identifies the DAG

# Wait for the whole graph, or inspect it without waiting.
ok = await client.wait_for_dag(dag_id, timeout=3600)
status = await client.get_dag_status(dag_id)
```

A job runs only once every job in its `depends_on` has finished; a member
that crashes or is cancelled means the jobs downstream of it never become
runnable (surfaced by `pj-admin doctor`).

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

A `deadline_key` is about work that has **not started**: the unique index
covers only `queued` rows, so the key is released the moment a worker claims
the job and the next enqueue is a legitimately new one. That is what you
want for "one pending digest at a time"; it is not what you want for "this
shipment happens once".

That predicate is also why a `deadline_key` may not be combined with
`waitfor_job` / `waitfor_group`, and it is refused with `ValueError` at the
door. A dependent job is inserted `waiting` — outside the index, so it
refuses no duplicate there — and the wake **clears** `deadline_key` on the way
into `queued`, because several waiters of one upstream may legally hold the
same key and the wake is one statement over all of them. The window would
therefore never open at any point in the row's life: the caller asks for
at-most-one-pending and silently gets every duplicate. `debounce_key` is
refused with the dependency edge for the same reason one index over.

### 4b. At-most-once work (Identity Keys)

An `identity_key` is your own name for a piece of work, and only one job can
carry it — **in any state**. A second enqueue does not raise: it returns the
id of the job that already exists.

```python
# Every call returns the same id: the queued one, then the running one,
# then the finished one. The work is done once.
job_id = await client.enqueue(
    "myapp.jobs.ShipOrder",
    identity_key=f"order:{order_id}:ship",
    order_id=order_id,
)
result = await client.wait_for_result(job_id)
```

Use `enqueue_identified()` when you need to know which call created it:

```python
job_id, created = await client.enqueue_identified(
    "myapp.jobs.ShipOrder",
    identity_key=f"order:{order_id}:ship",
    order_id=order_id,
)
if not created:
    log.info("order %s was already being shipped as job %s", order_id, job_id)
```

And `get_job_by_identity()` answers "did this ever run, and what became of
it" without an id you would have had to store:

```python
job = await client.get_job_by_identity(f"order:{order_id}:ship")
print("not shipped" if job is None else f"shipment {job.state}")
```

Two rules come with it:

- **The job class must match.** If the identity already names a job of a
  _different_ class, the enqueue raises `ValueError` naming both classes and
  the key. One identity means one piece of work; the platform will not hand
  back a job of the other class as if it were what you asked for.
- **Retention is the horizon.** The key is held by the _row_, so when the
  retention sweep reaps the terminal job (`--retention-days`, 30 by default)
  the key is free and the same identity enqueued afterwards creates a **new**
  job. This is the honest version of at-most-once: bounded by exactly the
  same window as everything else the platform remembers. If you need
  uniqueness beyond that window, put the time in the key —
  `order:4711:ship` is safe because order ids are not reused;
  `nightly-rebuild` is not, and `nightly-rebuild:2026-07-29` is.

**What an `identity_key` refuses, and why each one is not a limitation.** All
four are `ValueError` at the door, before anything is written:

- **`deadline_key`** — the two answer "what happens to a duplicate?" with
  opposite answers (hand back the existing job for the life of the row, vs.
  raise and then re-arm at the claim). Pick one; see
  [writing-jobs.md § Choosing your dedupe primitive](writing-jobs.md#choosing-your-dedupe-primitive).
- **`debounce_key`** — the third answer to the same question (move the job and
  rewrite its arguments).
- **`waitfor_job` / `waitfor_group`** — an identified enqueue may return a job
  it did **not** create, and that job carries whatever dependency the enqueue
  that really made it asked for. Your edge would silently not be applied and
  the work would run unordered. Give the identity to the job that does the
  work and let an unidentified waiter depend on it.
- **A DAG node** (`DAGBuilder.add(..., identity_key=...)`) — a DAG stamps
  `dag_id` and `run_group` onto the ids its enqueues return, so a pre-existing
  identity would have the DAG rewire a live job out of the DAG it belongs to.
  Enqueue the identified job separately and depend on it.

Identity is also **not** a batch option: `enqueue_batch()` returns one id per
row in order, and an identity that already exists has no row in that INSERT to
return, so it is refused rather than silently dropped.
`enqueue_in_transaction()` **does** accept it, and it behaves exactly as it
does elsewhere: the identified statement runs inside your transaction, returns
the existing job's id when the key is already held (discarding the row it would
have created), and its retry loop — which exists because a conflicting writer
committing after your snapshot leaves the statement with nothing to return —
re-runs inside that transaction, so it converges at `READ COMMITTED` and cannot
above it.

### 4c. Debouncing a burst (`debounce()`)

```python
job_id, created = await client.debounce(
    job_class, *, key, period, cap=None, **enqueue_options_and_kwargs
)
```

Collapse a burst of equivalent enqueues onto **one** job that runs once the
burst stops. The first call with a quiet `key` enqueues an ordinary job
parked `period` seconds in the future; every call after it, while that row is
still queued, **bounces** the row instead of writing another — `run_after`
moves to now + `period`, and the row's kwargs are replaced with this call's.

```python
# nine edits in three seconds, one re-index -- of the last revision
job_id, created = await client.debounce(
    "myapp.jobs.ReindexDocument",
    key=f"reindex:{doc_id}",
    period=5.0,
    cap=30.0,
    doc_id=doc_id,
    revision=revision,
)
```

**Returns** `(job_id, created)`, the same shape and the same meaning as
`enqueue_identified()`: `created` is True only for the call that opened the
window.

**Parameters**

- `key` (str): your name for the burst. The row holds it while it is
  `queued`; a worker claiming the job releases it.
- `period` (float): the quiet window in seconds. It **restates** the wait
  rather than extending it, so a bounce asking for a shorter period pulls the
  job in — the last caller to bounce decides when it fires.
- `cap` (float | None): the longest the job may be deferred, measured from
  the **first** call. Written to the row, so bounces from other processes
  respect it even though they never saw it; a later call passing a different
  `cap` does not change the window it joined. `None` means an endlessly
  bounced key is deferred indefinitely — a legitimate choice for work that is
  worthless until the burst really stops, and a starvation bug otherwise.
- everything else `enqueue()` takes, plus the job's kwargs.

**Last writer wins on the arguments**, and that is the feature: the collapsed
job runs the freshest input. It also means `debounce()` is only for work
whose latest arguments are the right ones. Work that must run with the
arguments it was first submitted with wants `deadline_key`.

**What it refuses**, all with `ValueError`:

- `identity_key` or `deadline_key` in the same call. The three keys answer
  "what happens to a duplicate?" differently — return the existing job,
  refuse the duplicate, move the existing job — and a row carrying two of
  them would have to do two of those at once.
- `waitfor_job` / `waitfor_group`. A dependent job is inserted `waiting`, and
  the collapse window is held by a `queued` row, so nothing would ever
  collapse.
- a `key` already naming a job of a **different** class, exactly as
  `identity_key` does, and the parked row is left untouched when it happens.
- a non-positive `period` or `cap`, and a `priority` above the worker ceiling
  (see [Priority, and the ceiling](#5-priority-and-the-ceiling)).

It is also **not** a batch option: collapsing is a bounce-or-insert pair of
statements and `enqueue_batch()` is one INSERT with no bounce in front of it,
so a key already held would fail the batch rather than collapse into it.

**The window closes at the claim, not at `run_after`.** A duplicate arriving
after the wait has elapsed but before any worker has taken the row still
collapses onto it — the work has not started. Once a worker does claim it,
the key is free and the next burst opens a new window while the collapsed job
runs. That release is permanent: a job that failed and is retrying is queued
again, but it is not bounceable, so a burst collapses onto the open window
rather than onto a job that is already on its second attempt. The row keeps
the key as **provenance** — `pj-admin jobs inspect` still names the window a
finished job came out of, and says the window is closed.

`pj-admin jobs why` reports a parked debounced job as `deferred` and names
the key, when it fires, and what caps it — and says nothing about debouncing
for a retried one, whose deferral is ordinary backoff.

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
it as a _declaration_ rather than trying to observe it:

```bash
pj --queue backfill --max-prio 5000            # the workers that will claim it
```

```python
client = JobClient(pool, prio_ceiling=5000)
# or: await JobClient.create(..., prio_ceiling=5000)
# or: await JobClient.from_config("./pyjobby.toml", prio_ceiling=5000)
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

### 7b. Pinning work to a code version

A rolling deploy replaces the code under jobs that are already in flight. For
most work that is fine, and for a durable (DXE) job whose checkpoints were
written by the old build it is usually fine too — `NondeterminismError` catches
a resumed job whose step sequence really did change. When a deployment cannot
accept the risk, it says so on the job:

```python
# this job's remaining work belongs to THIS build
await client.enqueue(
    "myapp.jobs.MigrateTenant", app_version="2026.07.28+a1b2c3d", tenant="acme"
)
```

and starts its workers advertising the same version:

```console
$ pj --queue default --app-version 2026.07.28+a1b2c3d
```

**One rule: the job opts in.** A job with an `app_version` is claimed only by
a worker advertising that exact version. A job **without** one — the default —
is claimed by every worker, versioned ones included, so turning this on never
stops a fleet draining its ordinary backlog mid-deploy. There is no worker-side
"only take matching work" flag, because that would make claimability a matrix
of two settings instead of one rule.

Declare it once for a deployment that pins everything, on the client or in the
config file both halves already read:

```python
client = JobClient(pool, app_version="2026.07.28+a1b2c3d")  # every enqueue
client = await JobClient.from_config("./pyjobby.toml")  # app_version = "..."
```

A per-call `app_version=` overrides the client's, so a deployment that wants
_most_ work unpinned leaves the client unset and pins the individual jobs.

**What keeps the pin:** `retry`, `rerun` and DLQ retry — they requeue the same
row to re-execute the same code. **What does not:** a fork, unless you ask
(`fork_job(..., app_version=...)`), because a fork is usually how work is
re-run under _new_ code and inheriting the old pin would strand it. Jobs minted
by a recurring schedule are never pinned: a schedule describes recurring work,
not a deployment.

**Stranding is loud, in three places.** Nothing can refuse a pin at enqueue
time — the fleet it has to match is whatever is running when the job is finally
claimed — so a job pinned to a build nobody runs is reported by `pj-admin
doctor`'s `unclaimable` check, explained by `pj-admin jobs why ID` as
`app_version_unmet` with the versions the fleet _does_ run, and logged once a
minute by every idle worker on that queue. Two remedies, either of which frees
it:

```console
$ pj --queue default --app-version 2026.07.01          # run what it asked for
$ pj-admin jobs set-app-version 48821 2026.07.28       # or repin it
$ pj-admin jobs set-app-version 48821 --clear          # or unpin it
```

From code, `client.update_job_app_version(job_id, version_or_None)` does the
same for a **queued or waiting** job.

### 8. Job Tags

Find jobs by something _your application_ means — which customer, which
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
enqueue to make no query faster. `tags` is yours, and it _is_ indexed.

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
  tagged with customer _and_ region _and_ batch. Extra tags never disqualify
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
result = await order.result()  # whatever the machine's job returned
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
_consumes_ the message and checkpoints having consumed it whether or not any
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

`history()` is the _current turn_: the machine compacts its checkpoint log at
each turn boundary so that replay stays bounded no matter how long it lives
(see [DXE.md](DXE.md#bounding-replay-compact)). For a permanent audit trail,
publish one — as machine events, or into your own table from inside a
`transaction()`.

### Synchronously

`SyncJobClient` mirrors JobClient's async surface, blocking, for scripts and
cron jobs — every method above except the two marked "async only"
(`enqueue_handle`, `enqueue_in_transaction`) exists on it under the same
name, with the same parameter names and defaults (held complete and
signature-identical by mirror tests), plus `SyncJobClient.from_config()` for
the config file a script already has. Machines come back as `SyncMachine`:

```python
with SyncJobClient.from_config("./pyjobby.toml") as client:
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

        # Alert if many crashes. crashed/finished are counted within
        # queue_stats' window (default: the last hour), so this is a RECENT
        # crash rate, not an all-time one. Divide by all terminal jobs in the
        # window (crashed + finished), not by finished alone.
        terminal = stats["crashed"] + stats["finished"]
        crash_rate = stats["crashed"] / max(terminal, 1)
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
    async with await JobClient.from_config("./pyjobby.toml") as client:
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
