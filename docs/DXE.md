# DXE — the Durable Execution Engine

DXE is what makes a pyjobby job _resumable_. A plain job runs from the top
every time it is attempted. A DXE job records what it has already done, so a
retry — after a crash, a timeout, a killed worker, a machine reboot — picks up
where it left off instead of repeating work that already happened.

Everything DXE knows lives in PostgreSQL. There is no in-memory cache, no
sidecar, and no coordination between workers beyond the database. A worker can
die at any instant and another worker resumes the job correctly.

---

## The primitives

```python
class ChargeAndShip(Job):
    async def task(self, order_id: int) -> dict:
        charge = await self.step("charge", self.charge_card, order_id, timeout=30)
        label = await self.step("label", self.buy_label, order_id)
        await self.sleep(3600)  # durable: holds no worker
        await self.set_event("shipped", label)  # readable by others
        note = await self.recv("support", timeout=60)
        return {"charge": charge, "label": label, "note": note}
```

| Primitive                                                              | Backing table           | What it guarantees                                                                                                                                                                                                                                           |
| ---------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `await self.step(name, fn, *a, **kw)`                                  | `jorb_step`             | `fn` runs **at least once**; once its checkpoint commits it never runs again                                                                                                                                                                                 |
| `await self.transaction(name, fn, *a, **kw)`                           | `jorb_step`             | **exactly once** for work `fn` does on the connection it is handed — that write and the checkpoint are one commit                                                                                                                                            |
| `timeout=` on either of those (or `step_timeout` on the class)         | `jorb_step`             | one step is bounded on its own; blowing the budget records a **timeout against that step** and retries the job                                                                                                                                               |
| `await self.sleep(seconds)`                                            | `jorb_step`             | the job resumes after the delay **without occupying a worker**                                                                                                                                                                                               |
| `await self.set_event(key, value)`                                     | `jorb_event`            | a durable key/value another job or an operator can read                                                                                                                                                                                                      |
| `await self.get_event(key)`                                            | `jorb_event`            | reads one back — this job's, or another's by id. **Not a step**: an event is durable state, so reading it is a query, and recording the answer would freeze the first value read into every later replay                                                     |
| `await self.send(job_id, msg)` / `await self.recv(topic)`              | `jorb_mailbox`          | a durable mailbox, **exactly-once on both ends**: a send commits with its checkpoint (it runs through `transaction()`), and a recv consumes and checkpoints in one statement — no crash timing can deliver twice, consume twice, or eat a message unrecorded |
| `await self.stream_write(key, value)` / `await self.stream_close(key)` | `jorb_stream`           | an ordered, durable output channel a client reads incrementally, **exactly-once per call site** — the row and its checkpoint are one commit, so a retry continues the stream instead of repeating it                                                         |
| `await self.compact()`                                                 | `jorb_step`             | discards this job's checkpoint log and restarts its step sequence, bounding replay for a job that lives indefinitely                                                                                                                                         |
| `self.cancelled`                                                       | `jorb.cancel_requested` | cooperative cancellation for long synchronous loops                                                                                                                                                                                                          |

---

## The core invariant: a completed step never runs twice

### How a step is identified

A checkpoint is keyed `(job_id, step_seq)`.

- **`job_id` is stable for the job's entire life.** A retry re-queues the
  _same row_ — it never creates a new job — so checkpoints written on attempt 1
  are still addressed by attempt 5. This is why retries reuse the row rather
  than inserting a new one.
- **`step_seq` is assigned by call order**, not by name: the first `step()` in
  an attempt is 1, the second is 2, and so on (`_dxe_next_seq`).

Sequencing by call order is what makes the fast-forward cheap and exact, and it
is also the source of DXE's single obligation on your code:

> **Job code must be deterministic outside steps.** The sequence of `step()`
> calls must be the same on every attempt.

That obligation is _enforced_, not merely documented. The step's `name` is
recorded alongside its sequence number, and on replay a mismatch raises
`NondeterminismError` rather than silently returning another step's result.
(The primitives checkpoint under implicit names an operator will meet in
`pj-admin jobs steps`: `dxe.sleep` for a durable sleep, `dxe.recv:<topic>`
for a receive, `dxe.send:<dest>:<topic>` for a send, `dxe.stream:<key>`
for a stream append and `dxe.stream-close:<key>` for its closing marker.)

```
step 2 was 'charge' on a previous attempt but is 'refund' now
— job code must be deterministic outside steps
```

So this is a bug DXE catches for you:

```python
if random.random() < 0.5:  # ← changes the step sequence
    await self.step("maybe", ...)
await self.step("always", ...)  # ← becomes step 1 or 2 depending
```

Put the nondeterminism _inside_ a step, where its result is checkpointed:

```python
choice = await self.step("choose", lambda: random.random() < 0.5)
if choice:
    await self.step("maybe", ...)
```

### What happens on the next attempt

Before the task runs, the worker loads every checkpoint for the job in one
query and binds it to the job instance (`_dxe_bind`):

```sql
SELECT step_seq, name, output, error FROM jorb_step
 WHERE job_id = $1 ORDER BY step_seq
```

Then, for each `step()` call:

| Recorded state               | What happens                                                 |
| ---------------------------- | ------------------------------------------------------------ |
| no row for this `step_seq`   | the function **executes**, and the result is recorded        |
| row exists, `error IS NULL`  | the recorded `output` is **returned without executing**      |
| row exists, `error` is set   | the function **re-executes** (a failed step is not a result) |
| row exists, different `name` | `NondeterminismError` — nothing executes                     |

A retry therefore fast-forwards through the completed prefix at the cost of one
query and a dict lookup per step, then does real work only from the point of
failure onward.

**A recorded `NULL` output is a real answer, not a missing one.** The second row
of that table means what it says: `error IS NULL` returns the recorded `output`
even when that output is `NULL`. This matters most for a `recv()` that timed
out — it checkpoints its empty-handed result unconditionally, so on every later
replay that call site returns `None` forever, even if the message arrived a
millisecond after the timeout expired. That is required for determinism, not an
oversight, and the message is not lost: a _later_ `recv()`, at a new sequence
number, still picks it up. Write the retry loop as a new call, never as a
re-entry into the same one.

**A failed step is deliberately not a cached result.** Recording the failure
buys observability (`pj-admin jobs steps <id>` shows exactly which step failed
and why) without making a transient error permanent.

---

## Transactional steps

`step()` is at-least-once, because the step's effect and its checkpoint commit
separately (see [Invariants](#invariants)). When the step's work is a write to
**this** database, that window can be closed completely: put the effect and the
checkpoint in the _same_ transaction, so they commit or roll back together.

```python
async def task(self, order_id: int) -> dict:
    # exactly-once: the row insert and the checkpoint are one commit
    await self.transaction("charge", self.charge, order_id)


async def charge(self, conn, order_id: int) -> dict:
    await conn.execute("INSERT INTO charges (order_id) VALUES ($1)", order_id)
    return {"charged": order_id}
```

There is no window: a crash before the commit leaves neither the charge nor the
checkpoint, so the step runs again cleanly. A crash after it leaves both, so the
step is skipped. Nothing in between exists.

Everything else about the primitive is `step()` — the same `step_seq`, the same
fast-forward of a completed checkpoint, the same re-execution of a recorded
failure, the same `NondeterminismError` on a renamed step. Both call the same
`_dxe_resume`, so the replay decision is one implementation rather than two
that can drift.

This also unifies exactly-once with fencing, rather than bolting them together.
The checkpoint write is already epoch-fenced, so a **superseded** execution's
checkpoint insert matches zero rows — which raises `StaleExecutionError` inside
the transaction and rolls the application write back with it. A zombie worker
cannot commit application data for a job another worker has taken over.

**When `fn` raises**, the transaction is rolled back — the work _and_ its
checkpoint — and the error checkpoint is then written in a **separate**
transaction. Observability that rolled back with the failure would be no
observability at all, so `pj-admin jobs steps <id>` still shows which step
failed and why, for work that left nothing else behind.

The trade is real, and it is exactly as wide as the connection:

- **`fn` must use the connection it is handed.** Anything it does on another
  connection — a second pool, an HTTP call, a file — is outside the transaction
  and is **not** rolled back with it. That work is at-least-once, exactly like
  `step()`. This cannot be enforced (a function is free to ignore its argument);
  it is documented, and pinned by a test.
- `fn` must not commit, roll back, or close the connection.
- If the worker's connection already holds a transaction, the inner one becomes
  a savepoint: the write and the checkpoint still stand or fall together, they
  just commit with the enclosing transaction.
- Because the guarantee lives on one connection, the checkpoint inside a
  transaction is written _without_ the worker's transparent reconnect. A
  reconnect there would commit the checkpoint on a new connection while the
  server rolled the write back — a checkpoint for work that no longer exists.
  Inside a transaction, a lost connection is an error, which fails the step and
  leaves it to re-execute.

Use `step()` for external effects and make them idempotent; use `transaction()`
for database work and get exactly-once for free.

---

## Per-step timeouts

`jorb.timeout_at` bounds the **job**. That is not enough for a job made of
several steps: one slow step spends the whole budget, and when the job is
finally timed out the row does not say which step hung. Both step primitives
therefore take a budget of their own:

```python
class ChargeAndShip(Job):
    step_timeout = 30  # default for every step here

    async def task(self, order_id: int) -> dict:
        charge = await self.step("charge", self.charge_card, order_id, timeout=5)
        label = await self.step("label", self.buy_label, order_id)  # 30s
        await self.transaction("record", self.write_row, order_id, timeout=2)
```

**Where it is declared.** A step's sensible budget is a property of the work,
and the call is where that work is named — so `timeout=` on the call is the
primary form. `step_timeout` on the class is the default behind it, for the
common job where every step wants one number. Per-call wins, `timeout=0`
disables the budget for that one call, and `None` (both defaults) means no
per-step bound at all. `timeout` is consumed by the primitive rather than
forwarded to `fn`, so a function that wants its own `timeout=` keyword must be
bound to it first: `self.step("x", partial(fn, timeout=5), timeout=30)`.

**What a blown budget does.** It raises `StepTimeoutError`, which is recorded
as _that step's_ error and then takes the **ordinary retry path** — the same
one an exception from the step takes. The next attempt fast-forwards the
completed prefix and re-executes only the step that hung, so retrying a
timeout is as cheap as retrying any other step failure, and a step that keeps
hanging exhausts `max_retries` and dead-letters exactly like one that keeps
raising. There is no separate escalation policy to configure, and no way for
one slow call to make a job permanently unrunnable on the first occurrence.

**Reading it back.** The recorded error carries the exception type, as every
recorded step error does, so a timeout is distinguishable from an ordinary
failure by reading the row — and the tag comes first, so it survives the
truncation `pj-admin jobs steps` applies:

```
Seq  Name    Epoch  Status  Duration  Error
2    hang     1     error    0.301s   StepTimeoutError: step 'hang' exceeded...
```

Only the _budget_ is reported that way. A `TimeoutError` a step raises on its
own account — an inner `asyncio.timeout`, an HTTP client's deadline — is an
ordinary failure: it is recorded as `TimeoutError`, not relabelled as a blown
budget, and not reported as the job's timeout either (so the job's `on_timeout`
policy is not applied to a deadline the operator never set).

### How it composes with the job timeout

> **The job's deadline is a ceiling, and only the tighter of the two bounds is
> ever armed.** A per-step budget is installed only while it is strictly
> tighter than the time the job has left. Once the job's own deadline is the
> binding constraint, the step budget is not armed at all and the job timeout
> fires alone.

So per-step budgets _subdivide_ the job's budget and never extend it; a step
timeout cannot outlive the job's deadline; the job timeout still fires however
the work is split into steps, reported as a job timeout with the job's
`on_timeout` policy applied; and the two can never race to report one overrun
as two different failures. A step that declares more time than the job has
left logs a warning saying so.

The job's deadline is one `asyncio.timeout` around the **whole** execution —
calling `run()`, awaiting whatever coroutine it returned, and draining an
async generator are all inside it, so "N seconds" means N seconds of the job,
once, whatever shape the job takes. It therefore fires wherever the job
happens to be, including inside a `step()` the composition rule left unarmed.
When it does, the completed prefix stays checkpointed (the retry still
fast-forwards it) and the interrupted step records **nothing**: the job ran
out of time around that step, which is not the same claim as that step having
been tried and failed.

### What a timeout can and cannot interrupt

A timeout is delivered as a **cancellation at an await point**.

- An **async** `fn` is genuinely stopped: it is cancelled where it is
  suspended, its `finally` blocks run, and the step then raises
  `StepTimeoutError`. Inside `transaction()` the cancellation aborts whatever
  statement is in flight, and the raise rolls the application write back on
  the way out — the connection is left clean and idle, never mid-transaction.
- A **synchronous** `fn` that blocks the event loop **cannot be interrupted by
  anything**. It starves the very timer that would fire, so neither the step
  budget nor the job's in-process deadline can touch it. It runs to
  completion, and if it succeeded its result is recorded as a success — a step
  whose work actually finished is not retroactively failed to enforce a bound
  that was never enforceable. The overrun is logged. Nothing but killing the
  process stops a blocking call — and note that `self.cancelled` is the
  **operator** cancel signal, not a timeout signal, so a long synchronous
  loop that wants to bound itself has to watch its own clock.

A wholly synchronous `task()` — no steps, no `await` — is a different case:
the worker calls `run()` in a thread, so the event loop and the deadline's
timer stay alive. The job is timed out on time and the worker moves on; what
the deadline cannot do is stop the thread, which runs to completion in the
background with its result discarded. That thread is not free, and what a
worker does when abandoned threads pile up is in `docs/OPERATIONS.md`.

### Catching the cancellation does not turn it into a success

Catching `CancelledError` to release something and **re-raising** is correct,
supported, and unchanged: the cancellation propagates and the timeout is
reported exactly as it always was.

Catching it and **returning normally** is refused. `asyncio.timeout` raises
nothing when its body swallows the cancellation, so a job could report a
result for an attempt the worker had already given up on — terminal under its
own power, so the monitor's out-of-process sweep could not correct it either.
Both scopes now refuse that:

- the job's deadline reports `JobTimeout` and applies the job's `on_timeout`
  policy, exactly as if the cancellation had propagated;
- a step's budget records `StepTimeoutError` against that step, and the step
  re-executes on the retry instead of fast-forwarding a value it invented.

**This cannot produce a spurious timeout.** The question asked is _did this
scope's timer fire while the job was still inside it_ (`Timeout.expired()`),
never _what time is it now compared to the deadline_. Leaving the scope
cancels the timer, so a job that returns even a microsecond before its
deadline is a success however long the worker then takes to store it, and a
blocking synchronous call never trips it at all — it starves the timer that
would have had to fire. Only work that was genuinely cancelled and chose to
continue anyway can reach the refusal.

An **exception** raised after the deadline is left as that exception, with its
own message and traceback: the job reported a failure, nothing false was
claimed, and relabelling would break the control-flow signals (`DurableSleep`,
`StaleExecutionError`) that legitimately unwind through the same scope. The
consequence worth knowing: a job that swallows its cancellation and then
raises something else is recorded as that error and follows `max_retries`, not
`on_timeout`.

---

## How results are stored and reused

`jorb_step.output` is `JSONB`, so **a step's return value must be
JSON-serializable**. That is the price of durability: the value has to survive
the process that produced it.

```
jorb_step
  job_id     BIGINT   ─ the job, stable across every attempt
  step_seq   INTEGER  ─ position in the call sequence
  name       TEXT     ─ determinism check
  output     JSONB    ─ the cached result (NULL if the step failed)
  error      TEXT     ─ recorded exception; presence means "re-execute"
  run_epoch  INTEGER  ─ which attempt wrote this checkpoint
  started, finished    ─ per-step timing
  PRIMARY KEY (job_id, step_seq)
```

`await self.sleep(n)` uses the same table: it checkpoints a wake time, requeues
the job for the future, and unwinds the worker with a `DurableSleep`. The job
holds **no worker and no connection** while it sleeps. On resume the checkpoint
says how much time is left; if the sleep is already satisfied, execution simply
continues past it.

---

## Streams: output a client reads while the job runs

A step's result is readable when the step is done. An event holds one current
value. A stream is the third shape: **an ordered sequence of values a job
appends to and anyone reads from, incrementally, from any position.**

```python
class ReportJob(Job):
    async def task(self, account: int) -> dict:
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

`jorb_stream` holds one row per value, keyed `(job_id, key, seq)`:

```
jorb_stream
  job_id     BIGINT      ─ the job, stable across every attempt
  key        TEXT        ─ which stream (a job may write several)
  seq        INTEGER     ─ position: DENSE, 0-based, per (job_id, key)
  value      JSONB       ─ the value (NULL is a streamed null)
  closed     BOOLEAN     ─ end-of-stream marker; carries no value
  run_epoch  INTEGER     ─ which attempt appended this row
  created    TIMESTAMPTZ
  PRIMARY KEY (job_id, key, seq)
```

### Exactly-once, stated precisely

`stream_write` runs through `transaction()`: the row and the checkpoint that
records it are one commit on one connection. So **each call site appends at
most one row, ever**, across every attempt — the guarantee `send()` has, for
the same reason and by the same code path. A job that writes five rows and
crashes writes rows six onward on the retry, because the first five
fast-forward from their checkpoints and return the positions they already
took.

That is a claim about **call sites**, not about values. A loop whose length
changes between attempts is a change to the step sequence, which is
nondeterminism DXE catches (`NondeterminismError`); a loop whose length is
derived from a checkpointed step is deterministic and streams exactly once.

The position comes from the same statement that writes the row — one past
the highest that key holds — so a resumed job continues its own sequence and
nothing has to loop looking for a free slot.

Appends are fenced on `run_epoch` like every other durable write: a
superseded execution appends nothing, because a reader has no way to tell two
writers apart.

### The closing marker is a column, never a value

End of stream is `closed = TRUE` on a row of its own. It is deliberately not
a sentinel inside `value`: any magic value is a collision with the job's own
data domain by construction, and the collision shows up as a reader that
stops early on real data. Because the marker is out of band, **every JSON
value a job can produce is streamable, including `None`** — `stream_write(k,
None)` is a streamed null and the reader yields it.

Closing is optional. `stream_close` exists for the job whose stream ends
before the job does; a job that simply returns, crashes or is cancelled ends
its readers anyway (below).

### What an operator sees

One checkpoint per call, in call order, under implicit names:

```
Seq  Name                    Epoch  Status   Output
1    dxe.stream:rows           1    ok       0
2    dxe.stream:rows           1    ok       1
3    dxe.stream-close:rows     1    ok       2
```

The output is the position the row took, which is what makes the replay
decision visible: a fast-forwarded write shows the epoch of the attempt that
really appended it. `jorb_stream` itself is the source of truth for the
values; `pj-admin jobs steps <id>` is the source of truth for what was
attempted.

### When a reader stops

`client.read_stream(job_id, key, offset=0)` is an async generator. It stops
on the first of:

- **the closing marker** — the job said the stream was over;
- **the job reaching a terminal state** — `finished`, `crashed` or
  `cancelled`. A cancel or a timeout ends a job out of band while a
  fenced-out execution may still believe it is writing, so the reader trusts
  the job's state and not the writer's intent. It re-reads **once** after
  observing the terminal state, so a row committed just before the end is
  still delivered.

A job that does not exist raises `JobError` immediately: nothing will ever
append, so waiting only delays the same answer. Everything else is a wait,
and it is the caller's to bound — `asyncio.timeout()` around the loop —
because the job's own deadline is the real one.

Between rows the reader parks on the `jorb_stream` notification with a
2-second fallback poll, and registers demand **once for the whole read**
rather than once per row. Positions are dense precisely so that resuming is
`offset=`: count what you consumed, pass it back, hold no cursor anywhere.

`client.get_stream(job_id, key)` is the snapshot form —
`{"values": [...], "closed": bool}` — for a caller that wants what exists
now rather than a live feed.

### Retention

Stream rows share the **checkpoint** window (`--checkpoint-retention-days`,
default 1 day) rather than the job window, because they share the argument: a
stream exists to be read while the job runs, every reader stops at the
terminal state, and after that the rows are audit material exactly as
checkpoints are. `finished` jobs only — a `crashed` or `cancelled` job is
retryable, its retry fast-forwards completed `stream_write` checkpoints
without appending, and reaping early would leave the resumed job's stream
permanently missing what its first attempt wrote. Those live until the whole
job ages out under `--retention-days`, when the cascade takes them.

---

## Fencing: why a zombie cannot corrupt a checkpoint

The dangerous case is not a crash — it is a worker that is _presumed_ dead but
is still running. Its network partition heals, or its timed-out task finally
returns, and it tries to write results for a job another worker has taken over.

`jorb.run_epoch` is the fencing token that makes those writes impossible.

- It **advances whenever the job enters an attempt** (claim) **or leaves
  one** — finish, crash, cancel, retry, reschedule, monitor requeue, operator
  requeue. Leaving covers the terminal writes too: the execution a terminal
  state ends may still be alive (a synchronous task in a thread is
  unstoppable), and it must not keep writing checkpoints, events, or mail
  for a job the platform has moved past.
- It is **not an attempt counter** — `run_count` is. It is monotonic and
  carries no other meaning.
- Every state-changing statement carries `AND run_epoch = $n`, so a statement
  issued by a superseded execution matches zero rows and does nothing.

The checkpoint write is fenced the same way — the insert is conditional on the
job still being at the writer's epoch:

```sql
INSERT INTO jorb_step (...)
SELECT $1, $2, $3, $4, $5, $6, $7, now()
 WHERE EXISTS (SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $6)
    ON CONFLICT (job_id, step_seq) DO UPDATE SET ...
```

When that write applies nothing, the worker raises `StaleExecutionError` and
abandons the attempt — a superseded execution stops as soon as it notices,
rather than running to completion and discarding its own result.

Advancing the epoch **at abandonment** rather than only at the next claim is
what closes the window between "the monitor gave up on this job" and "another
worker claimed it". During that window the old execution still held the current
epoch, so a checkpoint write from it would have applied.

Because checkpoints are loaded **without an epoch filter**, advancing the epoch
costs no resume capability: the new attempt still fast-forwards through every
step the old attempts completed.

---

## Invariants

1. **A step whose checkpoint committed never executes again**, across every
   attempt, forever.

   Read that precisely. The guarantee is anchored on the _checkpoint_, not on
   the step's side effect, and the two are written in separate transactions:

   ```
   fn() runs and its effects commit
        ← a crash HERE re-executes fn on the next attempt
   the checkpoint commits
   ```

   That window is narrow but real, and it is inherent to any step whose work
   happens outside the checkpoint's transaction — calling an external API,
   writing to another database. For those, `step()` is **at-least-once**, and
   the step should be made idempotent by the caller. (The built-in mailbox
   primitives are NOT in this class: `send()` runs through `transaction()`,
   and `recv()` consumes and checkpoints in one statement, so both are
   exactly-once.)

   For work against _this_ database the window is closed, not merely narrow:
   `transaction()` writes the effect and the checkpoint in one transaction on
   one connection, so the pair commits or rolls back together and the step is
   **exactly-once**. `send()` and `stream_write()` are that primitive wearing
   different names, which is why both are exactly-once per call site. See [Transactional steps](#transactional-steps) — and note
   that the guarantee covers only what `fn` does on the connection it is
   handed.

2. **A job keeps one row for its entire life.** Retries requeue it;
   `jorb_history` is the per-attempt audit trail. A
   [fork](#forking-a-job-a-new-row-from-a-checkpoint-prefix) does not break
   this — it is a second **job**, with its own row, its own history and its
   own copy of the prefix, not a second row for this one.
3. **`run_epoch` only increases**, and a write at a stale epoch is a no-op.
4. **A superseded execution cannot write** — a result, a checkpoint, a timeout,
   a reschedule, a published event, a mailbox message, or a stream row. The list is every
   durable write there is; a new one that skips the fence is caught by
   `test_every_state_changing_statement_carries_the_fence`. `send` is fenced on
   the **sender's** epoch, not the destination's: the question is whether this
   execution is still entitled to act.
5. **The step sequence is deterministic**, or `NondeterminismError` is raised
   before anything runs.
6. **A step's result is JSON-serializable**, or it cannot be checkpointed.
7. **A failed step is re-executed**, not replayed.
8. **`crashed` is terminal** — it _is_ the dead letter queue.
9. **A durable sleep holds no worker.**
10. **A per-step budget never outlives the job's deadline**, and only the
    tighter of the two is ever armed — a blown step budget is a step failure
    on the ordinary retry path, not a job verdict.
11. **A job's in-process ceiling is its configured timeout, once**, whatever
    shape its `run()` takes.

These are enforced by tests, not just asserted here: see
`tests/test_dxe_primitives.py`, `tests/test_dxe_transactions.py`,
`tests/test_dxe_streams.py`,
`tests/test_dxe_step_timeouts.py`, `tests/test_job_timeout_ceiling.py`,
`tests/test_dxe_faults.py`, `tests/test_dxe_concurrency.py`,
`tests/test_job_fork.py`, and `tests/test_invariants.py`.

The at-least-once/exactly-once distinction in particular is proved by fault
injection rather than argued: `test_kill_between_the_write_and_the_checkpoint`
SIGKILLs a real worker in that exact window, with `step()` and `transaction()`
running the same code shape, and asserts that the effect happened **twice** for
`step()` and **once** for `transaction()`.

---

## Resuming an interrupted job

```
pj-admin jobs steps <id>       # what completed, what failed, timings
pj-admin jobs rerun <id> --resume
                               # resume: completed steps fast-forward
pj-admin jobs rerun <id>       # restart: deletes checkpoints, runs from step 1
```

Fresh is the default — a plain `rerun` (no flag; there is no `--fresh`,
`fresh=` is the `AdminAPI.rerun_job` keyword) discards the checkpoints. Use
it when the recorded results are _wrong_ rather than merely incomplete —
after fixing a bug in a step, for instance. It is the operator's way to
discard checkpoints for a job that is going to run again.

---

## Forking a job: a NEW row from a checkpoint prefix

A resume and a re-run both reuse the job's row. A **fork** does not: it
creates a second job that re-executes the source's work from step N, with
steps 1..N-1 copied into the new job as its own checkpoints, so they
fast-forward exactly as a resume's would.

```
pj-admin jobs fork <id> --from-step 4    # copies steps 1-3, executes from 4
pj-admin jobs fork <id> --from-failure   # starts at the first recorded error
pj-admin jobs fork <id>                  # copies nothing: a fresh run, new id
```

```python
fork = await client.fork_job(job_id, from_step=4)
result = await client.wait_for_result(fork["job_id"])
```

`from_step` is **1-based and names the step the fork EXECUTES first**, so
`--from-step 4` copies three checkpoints. The refusal boundary is one past
the last recorded step (that is "run the next one"); anything beyond it is
refused with the number the source actually recorded.

**The source is not touched.** Not its state, not its `run_epoch`, not its
checkpoints, not its result — a fork writes one new job row and its copied
steps, in one statement, and nothing else. That is why the source may be in
_any_ state, including `running`: the prefix is copied as of the fork
statement's snapshot, and a source that goes on recording steps afterwards
simply has steps the fork never saw.

**Copied checkpoints carry `run_epoch` 0.** A `jorb_step` row's epoch says
which attempt of _its own job_ recorded it, and no attempt of the fork
recorded these; 0 is below every epoch the fork will run at (its first claim
makes it 1), so the copied prefix reads as "predates this job's first
attempt". Nothing depends on the value — replay loads checkpoints with no
epoch filter, the same property `rerun --resume` relies on — so
`pj-admin jobs steps` on a fork shows the fast-forwarded prefix at epoch 0
and the steps it really ran at 1 or more.

### Streams, events and mail are NOT copied

They are the **source's** output. A fork produces its own, from the steps it
actually runs — and readers of the fork see a fresh stream that starts at
position 0.

The consequence is worth stating rather than discovering: `stream_write` is
a checkpointed step, so a **copied** `dxe.stream:<key>` checkpoint
fast-forwards, which by definition appends nothing. A job that streams five
rows and then fails, forked from step 6, produces a stream containing only
what steps 6 onward write. The fast-forwarded calls still return their
recorded positions to the job code, and the fork's own first append is
position 0 of the fork's own dense sequence — positions are per `(job, key)`,
so a reader of the fork sees an ordinary stream and a reader of the source
sees the untouched original.

If a fork must reproduce the whole stream, fork from step 1: nothing is
copied and everything re-runs.

### What the new row inherits

| Copied                                 | Not copied                                         |
| -------------------------------------- | -------------------------------------------------- |
| `job_class`, `kwargs` (or an override) | `deadline_key`, `identity_key`, `debounce_key`     |
| `queue`, `prio` (or overrides)         | `schedule_id`                                      |
| `capability`, `tags`                   | `dag_id`, `run_group`, `waitfor_*`                 |
| `uid`, `partition_key`                 | `app_version` (pass one to pin the fork)           |
| `admin_data` (retry/timeout policy)    | `result`, error fields, `run_count`, `error_count` |

The split is identity: everything that describes the **work**, or says
**whose** it is, comes across — and nothing that names **this particular
job** or wires it into somebody else's structure does. So `uid` and
`partition_key` are copied, because a tenant's fork is still that tenant's
job and still counts against that tenant's fair-share lane, exactly as
`tags` does. Two live rows sharing a `deadline_key` would make idempotent
enqueue meaningless, an `identity_key` promises there is exactly one row
holding it (its unique index would refuse the second), and a fork is
nobody's DAG member.

`app_version` is the one routing column that is deliberately **not**
inherited, and the contrast with `partition_key` is the reason: a
`partition_key` says whose work this is, an `app_version` says which build
may run it — and the main reason to fork is to re-run the work under new
code. A fork that inherited the pin would be stranded by the deploy the
operator just made. Pass `app_version=` when the fork really does belong
to a version.

Lineage lives in `jorb.forked_from` (`ON DELETE SET NULL`, so a fork
outlives the retention sweep that reaps its source) and, permanently, in the
fork's own `enqueued` history row, which records the source id, the step and
how many checkpoints were copied. There is no `forked` history event and no
row on the source's trail: `jorb_history` records state transitions, and a
fork transitions nothing.

---

## Bounding replay: `compact()`

A resume loads **every step the job has ever recorded** and fast-forwards
through the completed prefix. `pj-bench replay` measures what that costs:
**0.9 µs and 260 bytes per checkpoint, linear from 1k to 100k**. So 10k
checkpoints resume in 8 ms and 100k in 75 ms and 26 MB resident — per job,
times however many such jobs a worker runs at once.

Almost no job needs to care. A job's step count tracks the work it does, and
work that takes 10,000 checkpointed steps takes long enough that 8 ms is
nothing. The exception is a job whose step count tracks **elapsed time** rather
than work — a state machine that wakes, finds no message, sleeps, and repeats,
recording a `recv` and a `sleep` on every wake. Woken every five minutes, that
is ~210,000 checkpoints a year, none of which record anything happening.

`compact()` bounds it:

```python
while True:
    await self.compact()  # at a loop boundary, not mid-turn
    message = await self.recv(topic="work", timeout=30)
    ...
```

It deletes the job's checkpoints and restarts the step sequence at 1.

**The contract you take on by calling it.** The checkpoint log is what stops
completed work re-running after a crash, so discarding it is only safe where
your code can re-derive its position from durable state it wrote itself. A
machine that reads back its own `set_event("machine.state")` at entry can. A
linear `task()` that relies on replay to skip the first nine of ten steps
cannot, and calling `compact()` there means step one runs twice.

It returns `False` and does nothing while a previous attempt's log is still
being replayed, because compacting mid-replay would delete checkpoints this
attempt has not yet caught up to. That is what makes a loop boundary the right
call site: call it every time round, and it takes effect on the first pass that
owes nothing to a previous attempt.

Fenced like every other durable write — a superseded execution cannot delete a
live one's checkpoints.

[`StateMachineJob`](../pyjobby/statemachine.py) does all of this for you; see
[STATECHARTS.md](STATECHARTS.md).

---

## Retention: checkpoints outlive the run, but not the job

A checkpoint exists to make a job **resumable**. The moment a job reaches a
terminal state, resume is impossible and every checkpoint it holds is audit
material only — which is why checkpoints have their own, much shorter life
than the job row.

Both windows apply in `pj-monitor` and both are **on by default**:

```
--retention-days 30              delete terminal jobs, with their history,
                                 events, streams, mailbox and checkpoints —
                                 and the five things no cascade reaches:
                                 consumed mail of LIVE jobs, history of LIVE
                                 jobs, emptied DAGs, aged schedule
                                 executions, retired worker rows
--checkpoint-retention-days 1    delete the CHECKPOINTS and STREAMS of
                                 terminal jobs, keeping the job row itself
```

Pass `0` to either to keep that data forever. So by default a finished job
keeps its step checkpoints and its streams for a day — long enough to answer
"which step failed, and why" after an incident — and the job row, its result
and its history for thirty.

`--retention-days` drives six separate sweeps, not one. Five of the tables
it covers are not reachable from a job at all — `jorb_dag` is the _parent_ of
its jobs, `jorb_schedule_log` cascades only from `jorb_schedule`,
`jorb_worker` is referenced by nothing, and a live job's consumed mail and
its history outlive any job deletion — so deleting jobs would never free them.
They share the one window because none of them has a lifetime of its own to
argue for. What each sweep refuses to delete, and why, is in
[OPERATIONS.md § Retention](OPERATIONS.md#retention-what-it-deletes-and-what-it-refuses-to);
the DXE-relevant half is below.

The job sweep is deliberately conservative:

- it removes only jobs in a **terminal** state (`finished`, `crashed`,
  `cancelled`) past the window. A `queued`, `claimed`, `running` or `waiting`
  job is never deleted at any age: a job waiting on a dependency can
  legitimately be very old.
- it will not delete a terminal job that a `waiting` job still depends on
  (via `waitfor_job` or `waitfor_group`), which would strand the waiter.
- it **drains** rather than taking one batch per cycle — a sweep that deletes
  slower than jobs arrive is retention in name only — under a per-cycle time
  budget so it can never starve the latency-critical sweeps.
- consumed mailbox messages are pruned by a sweep of their own, even when
  their job is still alive: a long-running workflow reads messages for months
  and the job-scoped cascade would never reach them. Only messages `recv` has
  already consumed are candidates — unread mail is kept at any age, because it
  is still deliverable.
- `jorb_history` is pruned by a sweep of its own for the same reason: a
  durable machine that never terminates is never reached by the job cascade,
  so nothing else would ever bound its wake/sleep audit trail.

`jorb_stream` is reaped on the checkpoint window by a sweep of its own, with
the same `finished`-only rule and for the same reason (see
[Streams § Retention](#retention)). It is a second statement rather than a
second table in the checkpoint sweep because each probes for the table it
deletes from, and a sweep that deletes nothing must stay a two-buffer answer.

The child rows go with the job through `ON DELETE CASCADE`.

Operators can also delete explicitly:

```
pj-admin queues clear <queue> --state finished --not-updated-for-days 30
pj-admin jobs delete <id>
```

Sizing rule of thumb: budget one `jorb_step` row per `step()` call per job, and
roughly four `jorb_history` rows per attempt. A streaming job costs two rows
per value — the `jorb_stream` row and its checkpoint — which is what makes
streams the wrong shape for output measured in millions of rows and the right
one for output a human or a client is watching.
