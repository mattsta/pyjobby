# Writing jobs

How to write a pyjobby job: what a job class is, which durable primitive to
reach for, and what the platform promises about each one.

This is the task-oriented guide. Its companions:

- **[DXE.md](DXE.md)** — the durable execution engine itself: the checkpoint
  table, the fencing model, the invariants, and why they hold. Read it when
  you want to know _why_; read this when you want to know _what to write_.
- **[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md)** — enqueueing, waiting for
  results, pipelines, tags, DAGs.
- **[OPERATIONS.md](OPERATIONS.md)** — running workers, retention, what a
  stuck queue looks like from outside.

Every complete job class below is executed against a real worker by
`tests/test_writing_jobs.py`. Fragments — snippets that need a service this
repository does not have — are marked as such and are the only code here
that is not run on every test run.

---

## A job is a class with a `task()`

```python
from pyjobby import Job


class Greet(Job):
    """A synchronous job: arguments in, JSON-serializable result out."""

    def task(self, name: str, greeting: str = "hello") -> dict[str, str]:
        return {"message": f"{greeting}, {name}!"}
```

The worker finds the class by its **dotted path** (`myapp.jobs.Greet`), which
is what the enqueue call stores on the row, so the module must be importable
by the worker process (`pj --path ...`).

**Arguments** are the enqueue call's keyword arguments. They are stored in
`jorb.kwargs` as `JSONB` and handed back as `task(**kwargs)`, so:

- they must be JSON-serializable — pass an id, not an ORM object, and not a
  `datetime`;
- they are keyword arguments only. A task with a required _positional-only_
  parameter can never be given one, and `@job` rejects it at import time;
- they survive the process. A retry six hours later gets the same arguments.

**The return value** is stored in `jorb.result` as `JSONB` and must be
JSON-serializable too. Return a reference (an S3 key, a row id) rather than a
payload; enqueue with `save_result=False` when the result is large and nobody
reads it.

### Sync, async, and async generators

`task()` may be a plain function, a coroutine function, or an async
generator. The worker calls `run()` — which by default is
`self.task(**self.job["kwargs"])` — in a thread from its own pool, then
awaits whatever came back and drains a generator:

```python
class StreamBatches(Job):
    """An async generator: the worker stores the list of everything yielded."""

    async def task(self, count: int = 3) -> Any:
        for i in range(count):
            yield {"batch": i}


# result == [{"batch": 0}, {"batch": 1}, {"batch": 2}]
```

Which to write:

| Shape                           | Use it when                                               | Cost                                                                       |
| ------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------- |
| `def task(...)`                 | the work is blocking (a C library, `requests`, heavy CPU) | runs in a worker thread; **cannot be interrupted** by any timeout          |
| `async def task(...)`           | the work waits on I/O, or uses any DXE primitive          | interruptible at every `await`; required for `step()`, `sleep()`, `recv()` |
| `async def task(...)` + `yield` | you want partial progress recorded in the result          | the stored result is the list of every value yielded                       |

DXE primitives are `await`ed, so a job that checkpoints anything is an
`async def task`.

Override `run()` only when you need to change how arguments reach `task()`;
overriding it is also how a job gets at the row before dispatch.

### What you have on `self`

- `self.job` — the job's **whole row** as a dict: every `jorb` column is
  present. The commonly useful keys: `id`, `kwargs`, `queue`, `prio`,
  `uid`, `tags`, `error_count`, `run_count`, `run_epoch`, `admin_data`,
  and `schedule_id` (the schedule that minted this job, else None — see
  RECURRING_SCHEDULER.md § Deadline keys). The examples below use
  `self.job["error_count"]` to mean "which attempt is this".
- `self.s` — the `JobSystem` running the job. `self.s.cxn` is the worker's
  own PostgreSQL connection (also what `transaction()` hands you) and
  `self.s.cache` is a plain per-worker dict for expensive objects you want to
  build once per process, not once per job.
- `self.cancelled` — see [cooperative cancellation](#selfcancelled--stop-a-long-synchronous-loop).

---

## Registering and enqueueing

The dotted path always works:

```python
job_id = await client.enqueue("myapp.jobs.Greet", queue="default", name="ada")
```

The `@job` decorator adds a typed enqueue that validates keyword arguments
against the task signature **before** anything reaches the database:

```python
from pyjobby import Job, job


@job
class ResizeImage(Job):
    """Registered, so it enqueues itself with checked keyword arguments."""

    async def task(self, url: str, width: int = 128) -> dict[str, Any]:
        return {"url": url, "width": width}


job_id = await ResizeImage.enqueue(client, queue=..., url="s3://bucket/a.png", width=64)
await ResizeImage.enqueue(client, widht=64)  # TypeError, before any INSERT
```

`enqueue_handle(...)` returns a `JobHandle` you can wait on, cancel, or read
events from. Decorate **every** subclass you enqueue: an inherited typed
enqueue is refused rather than silently enqueueing the parent's class.

A plain function can be decorated too, and becomes a Job subclass:

```python
@job  # fragment: registration shape only
async def resize_image(url: str, width: int = 128) -> dict: ...
```

Function jobs get no `self`, so they cannot use any durable primitive. Use a
`Job` subclass for anything that checkpoints.

Enqueue-time options (queue, priority, `run_after`, `deadline_key`,
`waitfor_job`, `capability`, retries, timeouts, tags) are documented in
[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#enqueuejob_class-kwargs).

One of them reads backwards: **`priority` is inverted — LOWER is MORE
urgent** (100 is the default, 10 goes first, 900 is background work), and a
worker claims only jobs at or below its own ceiling (`pj --max-prio`,
default 1000). `priority=5000` is therefore not "whenever you get around to
it" but a job no ordinary worker will ever claim, so the client refuses that
enqueue instead of writing a row nothing would run. If you really do run
workers for less-urgent work, say so on both sides —
`pj --max-prio 5000` and `JobClient(pool, prio_ceiling=5000)`; see
[OPERATIONS.md](OPERATIONS.md#priority-and-the-ceiling-a-worker-claims-under).

### Choosing your dedupe primitive

Three of those options stop the same job being enqueued twice, and they mean
different things. Pick by what you want to be true, not by which name reads
better — and the question that separates them is **what happens to the
duplicate**.

| Option         | Unique among                            | A duplicate enqueue…                                                                                        |
| -------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `deadline_key` | `queued` rows, per queue                | is **ignored** — it raises, and the pending job is untouched; the key re-arms once claimed                  |
| `identity_key` | all rows, table-wide                    | **returns the existing job** untouched: this exact work happens at most once, until retention reaps the row |
| `debounce()`   | `queued` rows never claimed, table-wide | **moves the job** — later, and with the new arguments: the burst collapses into one run once it goes quiet  |

**`deadline_key` collapses, then re-arms.** The unique index covers queued
rows only, so while a job sits in the queue a duplicate submission raises
`asyncpg.UniqueViolationError` and you treat that as "already scheduled" —
but the instant a worker claims it, the key is free again. That is exactly
right for work that recurs: a nightly digest, a debounced re-index, a "the
cart changed, schedule a reminder" job. Tomorrow's is a legitimately new
job, and the key must not stop it.

Once released, the key is not taken back: a **retry, re-run or DLQ retry
clears `deadline_key` from the row it requeues** (as does the monitor when it
reclaims a job from a dead worker, and a waiter's wake). So a job that failed
and was retried no longer collapses anything — it just runs, and a duplicate
submitted while it was out of the queue is a job of its own. That is the only
answer that works: the duplicate may already hold the key, and a requeue that
tried to take it back would raise instead of requeueing.

```python
# one pending reminder per cart; tomorrow's is a different job
try:
    await client.enqueue(
        "myapp.jobs.CartReminder",
        deadline_key=f"cart_reminder:{cart_id}",
        cart_id=cart_id,
    )
except asyncpg.UniqueViolationError:
    pass  # a reminder is already queued for this cart
```

**`identity_key` holds, and does not re-arm.** The unique index has no state
predicate, so the row holds the key for its whole life — queued, running,
finished, crashed alike — and a second enqueue does not raise: it returns
the existing job's id, which you observe or wait on exactly as if you had
just created it. That is right for work that must happen once and only
once: ship this order, charge this invoice, provision this account.

```python
# whoever calls this, however often, one shipment happens
job_id = await client.enqueue(
    "myapp.jobs.ShipOrder",
    identity_key=f"order:{order_id}:ship",
    order_id=order_id,
)
await client.wait_for_result(job_id)
```

**The horizon.** "At most once" lasts as long as the row does. When
retention reaps the terminal job (`--retention-days`, 30 by default) the key
is released, and the same identity enqueued afterwards is a new job. So
scope the key to a time the platform cannot outlive: `order:4711:ship` is
safe because order ids are never reused, `nightly-rebuild` is not, and
`nightly-rebuild:2026-07-29` is. If your key would be re-used inside the
retention window on purpose — because the work genuinely recurs — you wanted
`deadline_key`.

**`debounce()` collapses a burst, and runs the freshest arguments.** The
other two leave the pending job alone; this one changes it. Each call parks
one job `period` seconds out and every duplicate while it is still queued
pushes `run_after` further out _and replaces the row's kwargs_, so what
finally runs is a single job carrying the arguments of the last call. That
is right for work whose input is a moving target: re-index this document,
recompute this cart, reload the config that just changed nine times.

```python
# one re-index per document, 5s after the edits stop -- and never more
# than 30s after the first one, however long the editing goes on
job_id, created = await client.debounce(
    "myapp.jobs.ReindexDocument",
    key=f"reindex:{doc_id}",
    period=5.0,
    cap=30.0,
    doc_id=doc_id,
    revision=revision,  # the LAST revision is the one indexed
)
```

Because the kwargs are replaced, `debounce()` is **only** for work whose
latest arguments are the right ones. Work that must run with the arguments
it was first submitted with wants `deadline_key`.

Two things bound it. `period` restates the wait rather than extending it, so
a caller asking for a shorter quiet window pulls the job in; and without
`cap`, a key bounced faster than its own period is deferred forever. Pass a
`cap` unless indefinite deferral is genuinely what you want. The key is
released when a **worker claims** the job, so the next burst opens a new
window while the collapsed one runs.

None of the three is an execution-side guarantee. All of them stop a
duplicate **row**; they do nothing about a job whose own `task()` runs twice
after a retry. That is what [`step()`](#step--do-not-do-that-twice) and
[`transaction()`](#transaction--exactly-once-for-writes-to-this-database)
are for, and the two layers compose: an `identity_key` makes the work exist
once, `step()` makes each part of it execute once.

---

## Which primitive, and when

This is the decision the rest of the document exists for. Plain code is the
default; everything else buys a specific guarantee at the price of a database
write.

| Reach for                                             | When                                                      | What you get                                                          |
| ----------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------- |
| plain code                                            | cheap, deterministic, side-effect-free work               | nothing durable — it simply re-runs on the next attempt               |
| `await self.step(name, fn, ...)`                      | expensive or side-effecting work you do not want repeated | **at-least-once**: once its checkpoint commits, `fn` never runs again |
| `await self.transaction(name, fn, ...)`               | the work is a write to **this** database                  | **exactly-once**: the write and the checkpoint are one commit         |
| `await self.sleep(seconds)`                           | you need to wait                                          | the job leaves the worker entirely and resumes later                  |
| `await self.set_event(key, value)`                    | someone outside wants progress                            | a durable key/value readable by clients and operators                 |
| `await self.send(id, msg)` / `await self.recv(topic)` | jobs must coordinate                                      | a durable mailbox, each message consumed once                         |
| `await self.stream_write(key, value)`                 | output arrives in pieces and someone wants it as it does  | an ordered, durable stream a client reads live from any position      |
| `if self.cancelled:`                                  | a long **synchronous** loop                               | an operator's cancel can actually stop it                             |

Two rules apply to all of them, and they are the whole contract:

1. **Every checkpointed call consumes a sequence number, in call order.** The
   sequence must be identical on every attempt — see
   [determinism](#the-determinism-obligation).
2. **Everything checkpointed must be JSON-serializable** — a step's return
   value, an event's value, a message's payload.

### Plain code — the default

Recomputing a hash, formatting a string, filtering a list: cheaper to redo
than to checkpoint. A step costs a round trip and a row; do not spend one to
avoid an addition.

### `step()` — do not do that twice

```python
class ImportOrder(Job):
    """Two steps; the second fails once, and the first never runs twice."""

    async def task(self, order_id: int) -> dict[str, Any]:
        order = await self.step("fetch", self.fetch_order, order_id)
        await self.step("charge", self.charge, order)
        return {"imported": order["id"]}

    def fetch_order(self, order_id: int) -> dict[str, Any]:
        FETCHES.append(order_id)  # the expensive call you do not want repeated
        return {"id": order_id, "cents": 1999}

    def charge(self, order: dict[str, Any]) -> dict[str, Any]:
        if self.job["error_count"] == 0:  # fail once, to show the retry
            raise RuntimeError("payment gateway timed out")
        return {"charged": order["cents"]}
```

`FETCHES` is a module-level list standing in for the remote system, so the
test can count real executions. It enqueues this job, watches it fail and
retry, and asserts that `fetch` ran **once** across both attempts while
`charge` ran twice. That is the whole
value: the second attempt fast-forwards the completed prefix and resumes at
the failure.

`fn` may be sync or async; its result is recorded as `JSONB`. A **failed**
step is not a cached result — it re-executes on the next attempt — but the
error is recorded, so `pj-admin jobs steps <id>` names the step that broke.

**`step()` is at-least-once.** The effect commits, then the checkpoint does,
and a crash in between re-runs `fn`. That window is inherent for work outside
this database, so **make the effect idempotent yourself**:

```python
async def charge(self, order_id: int, cents: int) -> dict:  # fragment
    return await payments.charge(
        idempotency_key=f"order-{order_id}",  # the provider dedupes the retry
        cents=cents,
    )
```

### `transaction()` — exactly-once for writes to this database

When the effect is a write to the same PostgreSQL pyjobby lives in, the
window closes completely: `fn` is handed the worker's connection, and the
checkpoint is written on that connection inside the same transaction.

```python
class RecordPayment(Job):
    """The write and its checkpoint are one commit, so neither can be lost."""

    async def task(self, order_id: int, cents: int) -> dict[str, Any]:
        return await self.transaction("record", self.record, order_id, cents)

    async def record(self, conn: Any, order_id: int, cents: int) -> dict[str, Any]:
        await conn.execute(
            "INSERT INTO guide_payment (order_id, cents) VALUES ($1, $2)",
            order_id,
            cents,
        )
        if self.job["error_count"] == 0:
            # a crash here rolls the INSERT back with the checkpoint
            raise RuntimeError("crashed after the write, before the commit")
        return {"order": order_id, "cents": cents}
```

The first attempt inserts and then raises; the insert rolls back with the
checkpoint. The second attempt inserts again and commits. The table ends with
**exactly one** row — which is what the test asserts.

The constraint is exact: **`fn` must use the connection it is handed.**
Anything it does on another connection, over HTTP, or on the filesystem is
outside the transaction, is not rolled back with it, and is at-least-once
exactly like `step()`. Do not commit, roll back, or close that connection.

`transaction()` is otherwise identical to `step()` — same sequence number,
same replay, same `timeout=`, same recorded error. Details and the fencing
argument are in [DXE.md](DXE.md#transactional-steps).

### `sleep()` — wait without holding a worker

```python
class PollShipment(Job):
    """Publishes where it is, sleeps durably, then finishes."""

    async def task(self, order_id: int) -> dict[str, Any]:
        await self.set_event("stage", {"at": "awaiting-carrier"})
        await self.sleep(1)
        return {"order": order_id, "stage": "delivered"}
```

`await self.sleep(n)` checkpoints a wake time, requeues the job for the
future, and unwinds. While it sleeps the job is `queued` with a future
`run_after`, holding **no worker and no connection** — the test asserts
exactly that before waiting for the finish. Sleeping an hour costs nothing;
`await asyncio.sleep(3600)` costs a worker for an hour.

On resume, execution continues _past_ the sleep. A job requeued early sleeps
out the remainder rather than starting the wait again.

For "run me again later, from the top", use `await self.reschedule(30,
"minutes")` instead: it requeues the job without checkpointing a resume
point.

### `set_event()` / `send()` / `recv()` — coordination

`set_event()` publishes a durable key/value on the job. `send()`/`recv()` are
a durable mailbox: each message is consumed exactly once, and a retry of the
receiver replays the message it already consumed rather than eating another.

```python
class WaitForApproval(Job):
    """Announces itself, then blocks on its mailbox until someone decides."""

    async def task(self, order_id: int, timeout: float = 20) -> dict[str, Any]:
        await self.set_event("awaiting", {"order": order_id})
        decision = await self.recv(topic="approval", timeout=timeout)
        return {"approved": bool(decision and decision.get("ok"))}


class Approve(Job):
    """Delivers the decision to another job's mailbox."""

    async def task(self, dest: int) -> str:
        await self.send(dest, {"ok": True}, topic="approval")
        return "sent"
```

Both directions are executed: another job delivers the decision with
`send()`, and a client delivers the same decision with
`await client.send_message(job_id, {"ok": True}, topic="approval")`. Readers
outside the job use `await client.get_event(job_id, "awaiting", timeout=...)`.

`recv()` **occupies a worker while it waits** (it polls), so keep its
`timeout` short-ish and prefer `waitfor_job` dependencies for plain
"run after that one finished" ordering.

### `stream_write()` — output somebody reads while you produce it

Four ways a job says something to the outside, and each answers a different
question. Pick by the **conversation**, not by which call is nearest:

| Use              | To answer               | Reader sees                                         | Readable    |
| ---------------- | ----------------------- | --------------------------------------------------- | ----------- |
| `return`         | "how did it come out?"  | ONE value, once, at the end                         | after       |
| `set_event()`    | "where is it up to?"    | the LATEST value; earlier ones are overwritten      | during      |
| `stream_write()` | "what has it produced?" | EVERY value, in order, from any position            | during      |
| `send()`         | "who should act next?"  | one consumer takes each message and nobody else can | on delivery |

So: the answer the caller was waiting for is the **result** — one value, at
the end, and `enqueue_handle().wait()` or `use_result_from=` is how it
travels. A percentage, a phase name, a machine's current state is an
**event** — a reader wants the current answer and does not care how many
times it changed. A log line, a report row, a partial result is a **stream**
— dropping the middle would lose the output itself, and a reader can resume
at an offset. Work handed to exactly one other job is **mail**.

The two failure modes this table exists to prevent: returning a growing list
so a caller can watch it (nothing can read a result until the job ends —
stream it), and streaming a value that is only ever read last (two rows per
write to deliver what `return` delivers in none).

```python
class ReportJob(Job):
    """Streams each row as it is produced, then closes the stream."""

    async def task(self, account: int) -> dict[str, Any]:
        rows = await self.step("query", self.fetch_rows, account)
        for row in rows:
            await self.stream_write("rows", row)
        await self.stream_close("rows")
        return {"rows": len(rows)}
```

```python
async for row in client.read_stream(job_id, "rows"):
    render(row)
```

Each call site appends **exactly once** across every attempt — the row and
its checkpoint are one commit — so a job that streams half its rows and then
crashes streams the _rest_ on the retry rather than repeating what it already
sent. That is what makes the loop above safe to retry, and it is also why the
loop's LENGTH must be deterministic: `rows` comes from a checkpointed
`step()`, so the retry sees the same list and the same call sequence.

`stream_close()` is optional. Readers also stop when the job reaches a
terminal state, so close only when the stream ends before the job does.

The costs are worth knowing: two rows per value (the stream row and its
checkpoint), and a job whose reader is live pays a notification per append.
Stream what a human or a client is watching; write bulk output to storage and
stream the progress.

### `self.cancelled` — stop a long synchronous loop

An operator cancel (`pj-admin jobs cancel`, `client.cancel_job`) is delivered
to the running worker, which cancels the task **at its next `await`**. An
async job therefore stops on its own. A synchronous loop has no await points,
so nothing can interrupt it — unless it looks:

```python
class ReindexEverything(Job):
    """A long synchronous loop that asks whether an operator cancelled it."""

    def task(self, batches: int = 600) -> dict[str, int]:
        done = 0
        for _ in range(batches):
            if self.cancelled:  # operator asked us to stop
                break
            time.sleep(0.05)  # one batch of real work
            done += 1
        return {"batches": done}
```

The job is recorded `cancelled` either way — the worker does not wait for the
loop. What polling buys is that the **thread ends**: a loop that never looks
keeps a slot in the worker's job-thread pool until it finishes on its own,
and enough of those make the worker stop claiming (see
[OPERATIONS.md](OPERATIONS.md)). The test asserts the pool is empty seconds
after the cancel, against a loop that would otherwise have run for 30.

Two things `self.cancelled` is **not**:

- **It is not a timeout signal.** It reports only that an operator requested
  cancellation. Neither the job's deadline nor a step budget sets it. A
  synchronous loop that wants to bound itself must watch its own clock.
- **It is not observable from a blocking call inside an async job.** The flag
  is set by the worker's event loop, so a synchronous `fn` that blocks the
  loop inside `await self.step(...)` never sees it change. It works in a
  wholly synchronous `task()`, which the worker runs in a thread while the
  loop stays free.

---

## The determinism obligation

Checkpoints are keyed by **position**: the first checkpointed call in an
attempt is step 1, the second is step 2. So the _sequence_ of those calls
must be the same on every attempt — `step()`, `transaction()`, `sleep()`,
`send()`, `recv()`, `stream_write()` and `stream_close()` all consume a
number. (`set_event()` does not: it overwrites one row per key and has
nothing to replay.)

This is enforced. The recorded name is compared on replay:

```python
class BranchOutsideStep(Job):
    """WRONG: the branch changes which step is number 1."""

    async def task(self) -> dict[str, Any]:
        if self.job["error_count"] == 0:
            await self.step("probe", lambda: {"seen": True})
            raise RuntimeError("something transient")
        return await self.step("finish", lambda: {"ok": True})
```

Attempt 2 records nothing and fails with `NondeterminismError`, retries until
`max_retries` is spent, and dead-letters carrying this message — which the
test asserts verbatim:

```
step 1 was 'probe' on a previous attempt but is 'finish' now
— job code must be deterministic outside steps
```

The fix is never "make the code deterministic" — it is to move the
nondeterminism **inside** a step, where its answer is checkpointed and every
later attempt replays the same one:

```python
class BranchInsideStep(Job):
    """RIGHT: the choice is checkpointed, so every attempt makes it again."""

    async def task(self, order_id: int) -> dict[str, Any]:
        charged = await self.step("was-charged", self.was_charged, order_id)
        if charged:
            await self.step("refund", self.refund, order_id)
        return {"refunded": charged}

    def was_charged(self, order_id: int) -> bool:
        CHARGE_LOOKUPS.append(order_id)  # a fact that could change under us
        return True

    def refund(self, order_id: int) -> dict[str, Any]:
        if self.job["error_count"] == 0:
            raise RuntimeError("refund API unavailable")
        return {"refunded": order_id}
```

The lookup happens once, on the first attempt. The retry takes the same
branch even if the answer would be different now — which is the point:
`self.job`, `now()`, `random`, and a remote read are all things that change
between attempts, and a step is how you freeze one.

Loops are fine as long as their length is fixed by something checkpointed
(the job's kwargs, or a step's result). A loop over "whatever the API returns
today" is not.

---

## Timeouts

Two ceilings, and the tighter one wins.

**The job's deadline** comes from `timeout_seconds` at enqueue, else the
class attribute, else the worker's `--default-timeout` (an hour):

```python
class Impatient(Job):
    """A job-level ceiling: this job may never run longer than a second."""

    timeout = 1

    async def task(self) -> str:
        await asyncio.sleep(30)
        return "never"
```

`timeout = 0` disables the ceiling; `None` (the default) defers to the
worker. Blowing it records `Job timed out after 1s` and takes the retry path,
so a job that always overruns eventually dead-letters. Enqueue with
`on_timeout="fail"` to make the first overrun terminal instead. That policy
is about the job's deadline whichever of the three sources supplied it — pass
it alone and it still applies to the class attribute and to the worker
default, neither of which the caller can see from the enqueue site.
`on_timeout` is `"retry"` or `"fail"`; anything else raises at enqueue,
because the worker reads any non-`"retry"` value as terminal.

**A per-step budget** bounds one step, so one slow call cannot spend the
job's whole budget anonymously:

```python
class RenderReport(Job):
    """A per-step budget: the step that hangs is the one that is blamed."""

    step_timeout = 5  # default budget for every step in this job

    async def task(self) -> dict[str, Any]:
        rows = await self.step("gather", self.gather)
        return await self.step("render", self.render, rows, timeout=0.2)
```

`timeout=` on the call wins over `step_timeout` on the class; `timeout=0`
disables the budget for that call; `None` on both means unbounded. `timeout`
is consumed by the primitive, not forwarded — a function with its own
`timeout=` keyword must be bound first
(`self.step("x", partial(fn, timeout=5), timeout=30)`).

Blowing a step budget is a **step failure**, not a job verdict: it records
`StepTimeoutError` against that step (`pj-admin jobs steps <id>` names it) and
takes the ordinary retry path, so the next attempt fast-forwards the
completed prefix and re-runs only the step that hung. The test asserts both
halves: `gather`'s output survives, `render` carries the error.

**How they compose.** The job deadline is a ceiling: a step budget is armed
only while it is _strictly tighter_ than the time the job has left. Once the
job's own deadline binds, the step budget is not armed at all and the job
timeout fires alone. A step can never extend the job, and one overrun is
never reported as two failures.

**What a timeout cannot do.** Cancellation is delivered at an `await`. An
async `fn` is genuinely stopped; a **blocking synchronous** `fn` starves the
timer that would fire and runs to completion, and if it succeeded its result
is recorded as a success. The overrun is logged and nothing else can be done
about it — bound blocking work by making it interruptible, or give it its own
worker. See [DXE.md](DXE.md#what-a-timeout-can-and-cannot-interrupt).

Catching your own cancellation to release something and re-raising is fine.
Catching it and returning normally is refused: the timeout is recorded
instead of the invented result.

---

## Retries and failure

Any exception out of the job is a failure. The **same row** is requeued with
a backoff delay until the retry budget is spent:

- `max_retries` (default 10) is the number of _attempts_, not extra ones. It
  is a per-enqueue option; the worker's `--max-retries` is the fallback.
- `retry_strategy` is `exponential` (default), `linear`, `fibonacci`,
  `quadratic` or `fixed`, shaped by `initial_retry_delay` (1s) and
  `max_retry_delay` (3600s), with jitter (up to 10% of the delay, capped
  at 5s) so a fleet does not retry in lockstep.
- Override `rescheduleBackoff()` on the class for a policy of your own.

```python
class AlwaysFails(Job):
    """Exhausts its retries and ends `crashed` — the dead-letter state."""

    def task(self) -> None:
        raise ValueError("nothing here works")
```

Enqueued with `max_retries=2`, this attempts twice and ends `crashed` with
`error_count == 2`.

**`crashed` is terminal, and it _is_ the dead-letter queue** — there is no
separate table. The row keeps its arguments, its `error_message`, its
`error_backtrace`, and every checkpoint it wrote, which is what makes a
dead-lettered job debuggable and resumable:

```
pj-admin jobs steps <id>            # which step failed, and why
pj-admin jobs rerun <id> --resume   # resume: completed steps fast-forward
pj-admin jobs rerun <id>            # restart: discard checkpoints first
```

Fresh is the default — a plain `rerun` discards the checkpoints; there is
no `--fresh` flag (`fresh=` is the `AdminAPI.rerun_job` keyword). Rerun
fresh when the recorded results are _wrong_ (you fixed a bug in a step);
`--resume` when they are merely incomplete.

Failures that are not exceptions from your code:

| Situation                        | Recorded as                       | Retried?                                              |
| -------------------------------- | --------------------------------- | ----------------------------------------------------- |
| the job's deadline expires       | `Job timed out after Ns`          | yes, unless `on_timeout='fail'`                       |
| a step blows its budget          | `StepTimeoutError` on that step   | yes, as an ordinary step failure                      |
| the step sequence changed        | `NondeterminismError`             | yes, and it will keep failing until the code is fixed |
| an operator cancels              | state `cancelled`                 | no — terminal                                         |
| the worker is superseded mid-run | nothing; the attempt is abandoned | the live attempt owns the row                         |

---

## Job tags

`tags` are your own labels — customer, region, batch — indexed for filtering:

```python
job_id = await client.enqueue(
    "myapp.jobs.Greet", tags={"customer": "acme", "batch": 42}, name="acme"
)
jobs = await client.search_jobs(tags={"customer": "acme"})
```

...and `pj-admin jobs list --tag customer=acme`. They are a flat dict of
scalars, matched by containment (extra tags never disqualify a job), and they
are deliberately _not_ `admin_data`, which is the platform's own execution
config. Full rules in
[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#8-job-tags).

---

## A checklist for a new job

1. Are the arguments JSON-serializable ids rather than objects?
2. Is the result small — a reference, not a payload?
3. Is every expensive or side-effecting call inside a `step()`?
4. Is every external effect idempotent, or is it a `transaction()` on this
   database?
5. Is the sequence of checkpointed calls the same on every attempt?
6. Does anything nondeterministic that a branch depends on live _inside_ a
   step?
7. Does every wait use `sleep()` rather than `asyncio.sleep()`?
8. Does the slowest step have a `timeout=`, and does the job have a `timeout`
   it could actually meet?
9. If it is a long synchronous loop, does it check `self.cancelled`?

---

## Where the examples live

`tests/test_writing_jobs.py` holds every class above and runs each one
against a live worker: the retry that skips a completed step, the rolled-back
transaction, the durable sleep that releases its worker, the mailbox
round-trip, the cancelled loop that frees its thread, the blown step budget,
the dead-lettered job, and the `NondeterminismError` message quoted here.
Change the API and the guide fails a test.
