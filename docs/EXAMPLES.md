# Examples

Complete applications, not snippets: how jobs are composed into a pipeline, a
fan-out, an outbox, a schedule, a multi-tenant queue.

**Every complete example below is executed against a real worker by
`tests/test_examples_doc.py`.** The document is written from that file, so an
API change breaks the examples here instead of leaving your first copy-paste
quietly broken. The handful of snippets that need a service this repository
does not have — a web framework, a payment provider — are marked
**fragment** and are the only code here that is not run on every test run.

Its companions:

- **[writing-jobs.md](writing-jobs.md)** — what goes _inside_ one job, and
  which durable primitive to reach for. This document applies those choices
  rather than re-explaining them; when an example uses `step()` or
  `transaction()`, the reasoning is there.
- **[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md)** — the full enqueue API, every
  option, and the sync client.
- **[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md)** — cron schedules.
- **[OPERATIONS.md](OPERATIONS.md)** — running the workers these examples
  assume are already running.

## Contents

1. [Web application: accept now, work later](#1-web-application-accept-now-work-later)
2. [ETL: a sequential pipeline that hands results forward](#2-etl-a-sequential-pipeline-that-hands-results-forward)
3. [Fan-out / fan-in](#3-fan-out--fan-in)
4. [The transactional outbox](#4-the-transactional-outbox)
5. [A rate-limited third-party API](#5-a-rate-limited-third-party-api)
6. [Human in the loop](#6-human-in-the-loop)
7. [Batch import](#7-batch-import)
8. [Priority and capability routing](#8-priority-and-capability-routing)
9. [A recurring report](#9-a-recurring-report)
10. [Waiting for the answer](#10-waiting-for-the-answer)
11. [Streaming output a client renders live](#11-streaming-output-a-client-renders-live)
12. [A per-tenant report pipeline](#12-a-per-tenant-report-pipeline)

---

## 1. Web application: accept now, work later

A request handler's job is to accept the work and return. The expensive part
runs on a worker, and the request supplies an idempotency key so a retried
`POST` cannot start it twice.

```python
class ProcessUpload(Job):
    """Parse an uploaded batch once, then record it exactly once."""

    async def task(self, batch: str, raw: list[str]) -> dict[str, Any]:
        rows = await self.step("parse", self.parse, raw)
        await self.transaction("store", self.store, batch, rows)
        return {"batch": batch, "rows": len(rows)}

    def parse(self, raw: list[str]) -> list[dict[str, Any]]:
        return [
            {"sku": sku, "units": int(units)}
            for sku, units in (line.split(",") for line in raw)
        ]

    async def store(self, conn, batch: str, rows: list[dict[str, Any]]) -> list[int]:
        written = []
        for row in rows:
            written.append(
                await conn.fetchval(
                    "INSERT INTO example_row (batch, sku, units) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    batch,
                    row["sku"],
                    row["units"],
                )
            )
        return written
```

Two primitives, two different reasons. The parse is a `step()` so a retry
does not redo it; the write is a `transaction()` so a retry cannot double the
batch — the INSERTs and the checkpoint are one commit. The test crashes the
job between the write and the commit and asserts the table ends with exactly
the two rows.

The web layer only enqueues:

```python
# fragment: FastAPI wiring
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from pyjobby import JobClient
import asyncpg


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jobs = await JobClient.from_config("./pyjobby.toml")
    yield
    await app.state.jobs.close()


app = FastAPI(lifespan=lifespan)


@app.post("/uploads/{batch}")
async def accept_upload(batch: str, file: UploadFile):
    raw = (await file.read()).decode().splitlines()
    try:
        job_id = await app.state.jobs.enqueue(
            "myapp.jobs.ProcessUpload",
            queue="uploads",
            deadline_key=f"upload:{batch}",  # the retried POST is a no-op
            batch=batch,
            raw=raw,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "that upload is already being processed")
    return {"job_id": job_id, "state": "accepted"}


@app.get("/uploads/{job_id}")
async def upload_status(job_id: int):
    info = await app.state.jobs.get_job(job_id)
    if not info:
        raise HTTPException(404)
    return {"id": info.id, "state": info.state, "created": info.created.isoformat()}
```

`deadline_key` is a unique index on `(deadline_key, queue)` **over `queued`
rows only**, so the duplicate enqueue _raises_ rather than returning the first
id — catch `asyncpg.UniqueViolationError` and treat it as success. Two
consequences worth knowing: the same key in a different queue is a different
job, and once the first job leaves `queued` the key is free again, so it
deduplicates concurrent submissions rather than remembering forever.

`get_job()` returns a `JobInfo` with `id`, `job_class`, `queue`, `priority`,
`state` and `created`.

---

## 2. ETL: a sequential pipeline that hands results forward

Three stages, each starting only when the one before it finished, each
reading the previous stage's stored result.

```python
class ExtractSales(Job):
    async def task(self, day: str, count: int = 4) -> dict[str, Any]:
        rows = await self.step(
            "fetch", lambda: [{"sku": f"sku-{i}", "units": i + 1} for i in range(count)]
        )
        return {"day": day, "rows": rows}


class TransformSales(Job):
    """Stage two: reads stage one's result out of ``upstream_result``."""

    async def task(self, upstream_result: dict[str, Any]) -> dict[str, Any]:
        rows = upstream_result["rows"]
        return {
            "day": upstream_result["day"],
            "rows": [r for r in rows if r["units"] > 1],
        }


class LoadWarehouse(Job):
    """Stage three: the write, so it lands exactly once."""

    async def task(self, batch: str, upstream_result: dict[str, Any]) -> dict[str, Any]:
        return await self.transaction("load", self.load, batch, upstream_result["rows"])

    async def load(
        self, conn, batch: str, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        for row in rows:
            await conn.execute(
                "INSERT INTO example_row (batch, sku, units) VALUES ($1, $2, $3)",
                batch,
                row["sku"],
                row["units"],
            )
        return {"loaded": len(rows)}
```

The wiring is two options on the enqueue, and they are separate concerns:
`waitfor_job` is the _ordering_ edge, `use_result_from` is the _data_ edge.

```python
extract = await client.enqueue(
    "myapp.etl.ExtractSales", queue="etl", day="2026-07-01", count=4
)
transform = await client.enqueue(
    "myapp.etl.TransformSales",
    queue="etl",
    waitfor_job=extract,  # do not start until it finished
    use_result_from=extract,  # ...and inject its result as `upstream_result`
)
load = await client.enqueue(
    "myapp.etl.LoadWarehouse",
    queue="etl",
    waitfor_job=transform,
    use_result_from=transform,
    batch="2026-07-01",
)
```

Both downstream rows are created in state `waiting`, not `queued`: no worker
can see them until their dependency finishes. The result is injected at
_run_ time from the upstream row, so if retention has already deleted that
row the job fails with a `LookupError` rather than computing a wrong answer
from a missing input. Use `save_result=False` only on jobs nothing reads.

When nothing has to flow between the stages, `create_pipeline()` builds the
same chain in one call:

```python
ids = await client.create_pipeline(
    [
        ("myapp.etl.ExtractSales", {"day": "2026-07-01"}),
        ("myapp.etl.ExtractSales", {"day": "2026-07-02"}),
        ("myapp.etl.ExtractSales", {"day": "2026-07-03"}),
    ],
    queue="etl",
)
# ids[1] waits for ids[0]; ids[2] waits for ids[1]
```

To run this every night, give it a [schedule](#9-a-recurring-report) rather
than a crontab entry.

---

## 3. Fan-out / fan-in

Many independent units in parallel, then one job that runs when _all_ of them
have finished.

```python
class ResizeImage(Job):
    """One image, one size — the unit of the fan-out."""

    async def task(self, image: str, size: str) -> dict[str, Any]:
        return {"image": image, "size": size}


class BuildGallery(Job):
    """Fan-in: runs only once every resize in the group has finished."""

    async def task(self, batch: str, expected: int) -> dict[str, Any]:
        return {"batch": batch, "expected": expected}
```

```python
items = [
    {"image": image, "size": size}
    for image in ("a.png", "b.png")
    for size in ("thumb", "small", "large")
]
resize_ids, group_id = await client.create_fan_out(
    "myapp.images.ResizeImage", items, queue="images"
)

gallery = await client.enqueue(
    "myapp.images.BuildGallery",
    queue="images",
    waitfor_group=group_id,  # every job in the group, not just the last
    batch="2026-07-01",
    expected=len(items),
)
```

`create_fan_out()` returns `(job_ids, group_id)`; the group id is what
`waitfor_group` takes. The test asserts the gallery is `waiting` while the
resizes run and that it _started_ only after the last one's `finished`
timestamp — so this is a real barrier, not a race you usually win.

`waitfor_job` and `waitfor_group` are mutually exclusive: passing both raises
`ValueError`.

---

## 4. The transactional outbox

The classic failure: you commit an order, then the enqueue fails — or you
enqueue, then the order rolls back and a worker picks up a job for an order
that does not exist. `enqueue_in_transaction()` runs the same INSERT on a
connection you control, so the job becomes visible **if and only if** your
transaction commits.

```python
async with client.pool.acquire() as conn, conn.transaction():
    await conn.execute(
        "INSERT INTO example_order (ref, cents) VALUES ($1, $2)", ref, 1999
    )
    job_id = await JobClient.enqueue_in_transaction(
        conn, "myapp.orders.FulfillOrder", queue="orders", ref=ref
    )
```

It is a `@staticmethod` — it needs a connection, not a client — and it takes
the same keyword arguments as `enqueue()`. The connection must have pyjobby's
JSON codecs registered, which every connection from `pyjobby.db` (and from a
`JobClient` pool) has.

The test proves both halves: the committed transaction produces a job that
runs, and a transaction that raises after the enqueue leaves **no order row
and no job row**.

---

## 5. A rate-limited third-party API

The provider allows one call per second and dedupes on an idempotency key.
Sleeping in the job would hold a worker for the whole run; `self.sleep()`
checkpoints a wake time and hands the worker back.

```python
class ChargeCards(Job):
    """Paces its calls to a rate-limited provider, one per second."""

    async def task(self, refs: list[str], cents: int) -> dict[str, Any]:
        for i, ref in enumerate(refs):
            if i:
                await self.sleep(1)  # the provider's rate limit, durably
            await self.step(f"charge-{ref}", self.charge, ref, cents)
        return {"charged": len(refs), "cents": cents}

    def charge(self, ref: str, cents: int) -> dict[str, Any]:
        # fragment: the test counts calls here instead of holding a card
        return provider.charge(idempotency_key=ref, cents=cents)
```

Three things are load-bearing here:

- **The loop is deterministic.** Its length comes from `refs`, a checkpointed
  argument, so every attempt makes the same sequence of `sleep()` and
  `step()` calls. `sleep()` consumes a sequence number exactly like `step()`
  does, so a loop whose length depends on the clock, or a `sleep()` inside an
  `if self.job["error_count"] == 0:`, dead-letters with a
  `NondeterminismError`. See
  [the determinism obligation](writing-jobs.md#the-determinism-obligation).
- **Each charge is its own step**, so the retry after a provider error
  re-sends only the call that failed. The test fails the second card once and
  asserts the ledger reads `[a, b, b, c]` — `b` retried, `a` not resent.
- **The idempotency key is still required.** `step()` is at-least-once for
  anything outside this database: the charge lands, then the checkpoint does,
  and a crash in that window re-sends it. Only the provider can close that
  window, which is what the key is for.

While it paces, the job is `queued` with a future `run_after` — no worker, no
connection. The test asserts exactly that before waiting for the finish.

---

## 6. Human in the loop

A job that cannot finish without a decision publishes what it needs and
blocks on its mailbox. Anyone can answer: another job with `send()`, or an
operator through the client.

```python
class AwaitRefundApproval(Job):
    """Publishes what it needs, then blocks on its mailbox."""

    async def task(self, ref: str, timeout: float = 25) -> dict[str, Any]:
        await self.set_event("awaiting", {"ref": ref})
        decision = await self.recv(topic="refund", timeout=timeout)
        return {"ref": ref, "approved": bool(decision and decision.get("ok"))}
```

```python
job_id = await client.enqueue(
    "myapp.refunds.AwaitRefundApproval",
    queue="refunds",
    tags={"ref": ref, "needs": "approval"},  # so a dashboard can find it
    ref=ref,
)

# the dashboard, later:
pending = await client.search_jobs(tags={"needs": "approval"})
await client.get_event(job_id, "awaiting", timeout=10)
await client.send_message(job_id, {"ok": True}, topic="refund")
```

`tags` are your own labels and are indexed for `search_jobs()` and
`pj-admin jobs list --tag needs=approval`. They are matched by containment,
so extra tags never disqualify a job.

`recv()` **occupies a worker while it waits** — it polls — so keep its
timeout to minutes, not days, and give jobs that block a queue of their own
so they cannot starve the rest. For plain "run after that one" ordering use
`waitfor_job` instead, which costs nothing while it waits.

---

## 7. Batch import

`enqueue_batch()` inserts every row in one statement inside one transaction.
That amortises the commit — and the commit-lock cost of the enqueue
notification — over the whole batch, which is why it is the right tool for
tens of thousands of rows and the wrong one for three.

```python
rows = [
    ("myapp.imports.ImportRow", {"sku": f"sku-{i}", "units": i}) for i in range(200)
]
ids = await client.enqueue_batch(rows, queue="imports")

stats = await client.queue_stats("imports")  # {'queued': …, 'finished': …, …}
```

Shared options (`queue`, `priority`, `run_after`, `run_group`, ...) apply to
every row, and a row that needs its own carries them itself: a 3-tuple
`(job_class, kwargs, per_job_options)` layers that job's `deadline_key`,
`tags`, `uid` or timeouts over the shared set — so a batch loses nothing
against a loop of `enqueue()` calls. `create_fan_out()` is `enqueue_batch()`
plus a group id.

Register the job class with `@job` and the producer gets its arguments checked
before anything reaches the database:

```python
@job
class ImportRow(Job):
    async def task(self, sku: str, units: int) -> dict[str, Any]:
        return {"sku": sku, "units": units}


await ImportRow.enqueue(client, queue="imports", sku="x", unts=1)  # TypeError
```

---

## 8. Priority and capability routing

**A smaller number runs sooner.** Priority here is a _finishing position_,
not a rating: `priority=1` means "first", the way it does in a race, and the
big numbers are the ones that wait. The default is 100.

```python
await client.enqueue("myapp.Job", priority=10, ...)  # ahead of everything normal
await client.enqueue("myapp.Job", ...)  # 100, the default
await client.enqueue("myapp.Job", priority=900, ...)  # backfill
```

Claiming is `ORDER BY prio, run_after`, and the test starts a worker after
enqueueing all three and asserts they _started_ in exactly that order.

The direction has a consequence at the far end: a worker claims only jobs
**at or below its own ceiling** (`pj --max-prio`, default 1000), so a number
big enough is not "run this last" but "run this never". `priority=5000` past
a default fleet would sit `queued` forever, never failing and never reaching
the DLQ. So the client refuses it where the caller can still see it:

```python
await client.enqueue("myapp.Job", priority=5000, ...)
# ValueError: priority 5000 is above the worker priority ceiling (1000):
# workers claim only jobs with prio <= their ceiling, so this job would sit
# 'queued' forever -- no error, no retry, no DLQ. LOWER numbers are MORE
# urgent, so least-urgent work wants a priority just UNDER the ceiling
# (e.g. 900), not a large one. ...
```

Nothing is written when it refuses — there is no row to find later and
wonder about. `priority=1000` exactly is fine, and is what "least urgent"
means on a default fleet.

If you genuinely run workers for less-urgent-than-1000 work, the deployment
declares that ceiling **twice**, because the client cannot observe a worker
flag:

```bash
pj --queue backfill --max-prio 5000        # the workers that will claim it
```

```python
client = JobClient(pool, prio_ceiling=5000)  # the client allowed to enqueue it
```

The test pins both halves: with only the client raised, the job is written
and a default-ceiling worker stays blind to it; a worker started at
`--max-prio 5000` then runs it. Still prefer a separate queue over a large
number when the point is isolation — priority orders work within a queue, it
does not separate it.

`capability` routes by hardware rather than by urgency. A job that names one
is invisible to every worker that does not advertise it:

```python
await client.enqueue("myapp.ml.Embed", queue="ml", capability="gpu", model="e5")
```

```bash
pj --queue ml --cap gpu --cap cpu     # this worker can take it
pj --queue ml --cap cpu               # this one never will
```

The test enqueues a `gpu` job and a plain one, runs a worker without the
capability — the plain job finishes, the GPU job stays `queued` — then starts
a worker that advertises `gpu` and watches it drain. A capability nobody
advertises is a job that waits forever, which is the intended behavior and
also the most common way to lose one.

---

## 9. A recurring report

Use a schedule, not a crontab entry: the schedule is a row, so it has
history, statistics, an enable/disable switch, and the safety features in
[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md).

```python
class DailyRevenueReport(Job):
    """The job a cron schedule creates; it reads its own schedule metadata."""

    async def task(self, region: str) -> dict[str, Any]:
        return {
            "region": region,
            "schedule": self.job["admin_data"].get("schedule_name"),
        }
```

```bash
pj-admin schedule add daily-revenue \
    myapp.reports.DailyRevenueReport \
    "0 2 * * *" \
    --timezone America/New_York \
    --queue reports \
    --kwargs '{"region": "emea"}' \
    --max-concurrent 1 \
    --circuit-breaker 3 \
    --description "Daily revenue report at 2am Eastern"
```

or, from Python:

```python
from pyjobby.admin_api import AdminAPI

schedule = await AdminAPI(conn).create_schedule(
    name="daily-revenue",
    job_class="myapp.reports.DailyRevenueReport",
    cron_expr="0 2 * * *",  # 02:00 every day...
    timezone="America/New_York",  # ...in Eastern wall-clock time
    queue="reports",
    kwargs={"region": "emea"},
    max_concurrent_jobs=1,
    circuit_breaker_threshold=3,
    description="Daily revenue report at 2am Eastern",
)
```

`conn` must come from `pyjobby.db.connect()` (or a `JobClient` pool), not from
bare `asyncpg.connect()`: `kwargs` is a JSONB column and needs pyjobby's
codecs. A malformed cron expression or an unknown timezone is rejected here,
at creation, rather than silently never firing.

A separate process fires the due schedules:

```bash
pj-scheduler --config ./pyjobby.toml
```

Each firing creates an ordinary job carrying `schedule_id` — a column on
`jorb`, which is how the scheduler counts its own in-flight work by index —
plus `admin_data.schedule_name` / `scheduled_time`, and a deadline key of
`schedule:<id>:<scheduled_time>`, so a second scheduler instance is a no-op
rather than a duplicate. The test drives the whole path — create the
schedule, run one poll, watch a worker execute the created job — and asserts
`next_run` advanced to the following 02:00 Eastern.

Timezone-aware schedules are the subtle part: what `0 2 * * *` means on the
two days a year the clock moves is in
[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md#daylight-saving-time).

---

## 10. Waiting for the answer

Most enqueues are fire-and-forget. When the caller does want the value back,
`enqueue_handle()` returns a handle that can wait on it, read its progress
events, and cancel it.

```python
class Estimate(Job):
    async def task(self, cents: int) -> dict[str, Any]:
        await self.set_event("progress", {"pct": 50})
        return {"total": cents * 2}
```

```python
handle = await client.enqueue_handle("myapp.quotes.Estimate", queue="quotes", cents=250)

await handle.event("progress", timeout=15)  # {'pct': 50}
result = await handle.wait(timeout=20)  # {'total': 500}
await handle.status()  # 'finished'
```

Both waits are `LISTEN`-driven on a client built by `from_config()` or
`create()`, with a polling fallback, so they cost a connection rather than a
spin; a client constructed from a bare pool polls. `handle.wait()` raises
`JobFailedError` if the job dead-letters — the test asserts the provider's
message comes back with it — and `JobCancelledError` if it is cancelled.

This is a synchronous request wearing an asynchronous coat, so it is worth
being deliberate: a web request that waits for a job holds a worker
_and_ an HTTP connection for the whole run. Prefer accepting the job and
polling — example 1 — for anything that is not fast and bounded.

---

## 11. Streaming output a client renders live

A long job that produces its output in pieces — an export, a report, a
migration log — does not have to make the caller wait for all of it. Append
each piece to a durable stream and the caller renders them as they land.

```python
class ExportLedger(Job):
    """Streams each page of an export as it is produced, then closes."""

    async def task(self, account: int, pages: int = 3) -> dict[str, Any]:
        for page in range(pages):
            rows = await self.step(f"page-{page}", self.fetch_page, account, page)
            await self.stream_write("pages", {"page": page, "rows": rows})
        await self.stream_close("pages")
        return {"pages": pages}
```

```python
job_id = await client.enqueue("myapp.exports.ExportLedger", queue="exports", account=42)

async for page in client.read_stream(job_id, "pages"):
    render(page)  # arrives while the job is still running
```

The test asserts exactly that: the job is still `running` when the first page
reaches the reader, and the loop ends on the marker `stream_close()` wrote.

Each `stream_write` appends **exactly once for that call site**, across every
attempt — the row and its checkpoint are one commit — so a retry after a
crash continues the export from where it stopped instead of re-sending pages
the client already rendered. Fetching each page inside `step()` is what makes
the loop deterministic across that retry, which is the obligation every
checkpointed primitive shares.

A reader stops on the closing marker, or on the job reaching a terminal state
— a crashed or cancelled export ends its readers with no marker at all. For
a snapshot instead of a feed, `await client.get_stream(job_id, "pages")`
returns `{"values": [...], "closed": bool}`; to resume after a disconnect,
count what you rendered and pass it as `offset=`.

Use a stream when every value matters. Use `set_event()` when only the latest
does — a percentage, a phase — and the mailbox when the output is work for
exactly one other job.

---

## 12. A per-tenant report pipeline

One job class, several ways in, and a shared queue that several customers are
on at once. Nothing below is a new component — every piece is an option on an
enqueue. This is what the earlier examples turn into once the work belongs to
_somebody_.

```python
class TenantReport(Job):
    """One tenant's report: collect the rows, render them, stream the stages."""

    async def task(self, tenant: str, month: str, revision: int = 1) -> dict[str, Any]:
        rows = await self.step("collect", self.collect, tenant, month)
        await self.stream_write("progress", {"stage": "collected", "rows": rows})
        url = await self.step("render", self.render, tenant, month)
        await self.stream_write("progress", {"stage": "rendered", "url": url})
        await self.stream_close("progress")
        return {"tenant": tenant, "revision": revision, "url": url}

    def collect(self, tenant: str, month: str) -> int:
        return len(warehouse.rows_for(tenant, month))  # fragment: the slow query

    def render(self, tenant: str, month: str) -> str:
        return f"s3://reports/{tenant}/{month}.pdf"
```

Two steps, because `collect` is the expensive half and the incident at the end
of this example must not pay for it twice. Two stream rows, because the caller
watches a progress list rather than waiting for the result.

### One lane per tenant

Every enqueue below carries `partition_key=tenant`. On its own that is a
label; the queue is what gives it teeth:

```bash
pj-admin queues limits reports --max-concurrency 1 --partition-limits
```

`max_concurrency 1` now means **one report in flight per tenant** rather than
one on the whole queue, so the customer who asks for a year of monthly
reports at once cannot take the cap and leave everybody else's single report
queued behind it. The test enqueues the noisy tenant's four reports _first_ —
so they sort ahead of everything at every claim — then one report for a quiet
tenant, runs two workers, and asserts both halves: the quiet tenant's report
finishes before the noisy backlog does, and the noisy lane still ran strictly
one job at a time.

Jobs with no `partition_key` are not excluded from this; they form one lane
among the others, which is what lets you adopt the key on a live queue one
producer at a time. [OPERATIONS.md](OPERATIONS.md#partition_limits-the-same-limits-per-tenant)
has the rest of the rules.

### The dashboard button: a burst, collapsed

A "Regenerate" button gets pressed. Then pressed again. `debounce()` parks one
job and every further press moves it, replacing its arguments:

```python
job_id, created = await client.debounce(
    "myapp.reports.TenantReport",
    key=f"report:{tenant}",
    period=1.0,  # fire one second after the clicking stops...
    cap=2.0,  # ...and never later than two seconds after the first click
    queue="reports",
    partition_key=tenant,
    tenant=tenant,
    month="2026-07",
    revision=revision,  # the LAST revision is the one rendered
)
```

Three presses, one row, and the revision that runs is the third one — which is
the whole reason this is `debounce()` and not a `deadline_key`. `cap` is
written to the row by the first call, so a fourth press asking for a much
longer quiet window still cannot push the job past it; the test asserts
`run_after` has landed exactly on that ceiling.

### The client tails the run

```python
async for update in client.read_stream(job_id, "progress"):
    show(update["stage"])  # 'collected', then 'rendered'
```

The test asserts the job is still `running` when the first stage reaches the
reader, and that the loop ends on the marker `stream_close()` wrote.

### The billing webhook: at most once, ever

Month-end close comes from somewhere else entirely — a provider webhook that
retries on any non-2xx, including the ones your own timeout caused. This work
must happen once:

```python
job_id, created = await client.enqueue_identified(
    "myapp.reports.TenantReport",
    identity_key=f"report:{tenant}:2026-07:final",
    queue="reports",
    partition_key=tenant,
    tenant=tenant,
    month="2026-07",
)
if not created:
    log.info("close for %s was already job %s", tenant, job_id)
```

The retried webhook gets the same id back rather than an exception, and —
unlike `deadline_key` — the key does **not** re-arm when the job finishes: the
test finishes the job and calls again, and still gets the first id. That
guarantee lasts as long as the row does, so the key names a month rather than
just a tenant. `await client.get_job_by_identity(key)` looks it up later.

### The incident: fork from the failing step

The renderer breaks and the close dead-letters after the expensive collect had
already succeeded. Deploy the fix, then fork:

```python
fork = await client.fork_job_from_failure(crashed_id)
```

The fork is a **new** job that starts at the step that failed, with the
completed prefix copied in as checkpoints, so the collect is not paid for a
second time — the test counts the real query and sees one call across both
jobs. (The real fix is a deploy; the test's `render` takes a flag and the
fork passes `kwargs_override` to flip it, which is the same thing with no
deploy in the loop.) The fork inherits `partition_key`, because that says
whose work this is; it does not inherit `identity_key`, because that says
_which_ work it is and two live rows holding one identity would make it mean
nothing. The crashed original is untouched, and stays crashed as the record
of what happened.

One detail the test pins because it surprises people: the fork's `progress`
stream contains only the `rendered` row. Streams are the source job's output
and are never copied, so the fast-forwarded `stream_write` checkpoint appends
nothing to the fork's own stream.

Note also what `--from-failure` resolved to: **step 3**, not step 2. `collect`
is step 1, the `stream_write` after it is step 2, and `render` is step 3 —
every checkpointed call takes a sequence number, streams included. See
[the determinism obligation](writing-jobs.md#the-determinism-obligation).

---

## Patterns at a glance

| You want                           | Reach for                                                      |
| ---------------------------------- | -------------------------------------------------------------- |
| fire and forget                    | `enqueue(...)`                                                 |
| run later                          | `enqueue(..., run_after=when)`                                 |
| never twice for the same request   | `enqueue(..., deadline_key=key)`, catch `UniqueViolationError` |
| never twice, ever                  | `enqueue_identified(..., identity_key=key)` → `(id, created)`  |
| a burst of clicks, run once        | `debounce(..., key=k, period=s, cap=s)`                        |
| A then B then C                    | `waitfor_job=`, or `create_pipeline([...])`                    |
| B needs A's result                 | `waitfor_job=a, use_result_from=a` → `upstream_result`         |
| many in parallel, then one         | `create_fan_out(...)` → `enqueue(..., waitfor_group=g)`        |
| thousands of identical rows        | `enqueue_batch([...])`                                         |
| the job must not outlive its order | `JobClient.enqueue_in_transaction(conn, ...)`                  |
| this one first                     | `priority=` a **smaller** number than 100 (10 goes before 100) |
| only on that hardware              | `capability="gpu"`, and `pj --cap gpu`                         |
| only on that build                 | `app_version="…"`, and `pj --app-version …`                    |
| no tenant starves another          | `partition_key=t` + `queues limits Q --partition-limits`       |
| every night at 2am                 | `pj-admin schedule add` + `pj-scheduler`                       |
| the caller wants the value         | `enqueue_handle(...)` → `await handle.wait()`                  |
| the caller wants it piece by piece | `stream_write(key, v)` → `async for v in client.read_stream()` |
| run the tail again, under new code | `fork_job_from_failure(id)` → a **new** id                     |
| find it later                      | `tags={...}` → `search_jobs(tags=...)`                         |

## Practices these examples are built on

1. **Arguments are JSON-serializable ids**, not ORM objects and not
   `datetime`s. A retry six hours later gets the same arguments.
2. **Results are small.** Return an S3 key or a row id; enqueue with
   `save_result=False` when nothing reads the result at all.
3. **Every external effect is idempotent, or it is a `transaction()`** on
   this database. `step()` is at-least-once for everything else.
4. **Queues are separated by what they need**, not by importance: a queue for
   blocking jobs, a queue for GPU jobs, a queue for the slow nightly batch.
   Priority orders work _within_ a queue; it does not isolate it.
5. **Duplicates are prevented at enqueue**, so the producer's own retry is
   free — with `deadline_key` while the job is pending, `identity_key` for
   work that must happen at most once, or `debounce()` for a burst whose
   latest arguments are the right ones.
6. **Failures are visible.** `crashed` is the dead-letter state; the row keeps
   its arguments, its backtrace and its checkpoints, and
   `pj-admin jobs rerun <id> --resume` resumes from the last completed step —
   or `jobs fork <id> --from-failure` re-runs the tail as a new job, leaving
   the original as the record.

## See also

- [writing-jobs.md](writing-jobs.md) — what goes inside a job
- [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) — the complete client API
- [RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md) — cron scheduling
- [ADMIN_TOOLS.md](ADMIN_TOOLS.md) — `pj-admin` and the web interface
- [OPERATIONS.md](OPERATIONS.md) — running and watching the workers
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the platform is built
