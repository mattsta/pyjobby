# DXE — the Durable Execution Engine

DXE is what makes a pyjobby job *resumable*. A plain job runs from the top
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
        charge   = await self.step("charge", self.charge_card, order_id)
        label    = await self.step("label",  self.buy_label,   order_id)
        await self.sleep(3600)                      # durable: holds no worker
        await self.set_event("shipped", label)      # readable by others
        note = await self.recv("support", timeout=60)
        return {"charge": charge, "label": label, "note": note}
```

| Primitive | Backing table | What it guarantees |
|---|---|---|
| `await self.step(name, fn, *a, **kw)` | `jorb_step` | `fn` runs **at most once** across every attempt of this job |
| `await self.sleep(seconds)` | `jorb_step` | the job resumes after the delay **without occupying a worker** |
| `await self.set_event(key, value)` | `jorb_event` | a durable key/value another job or an operator can read |
| `await self.send(job_id, msg)` / `await self.recv(topic)` | `jorb_mailbox` | a durable mailbox; each message is consumed exactly once |
| `self.cancelled` | `jorb.cancel_requested` | cooperative cancellation for long synchronous loops |

---

## The core invariant: a completed step never runs twice

### How a step is identified

A checkpoint is keyed `(job_id, step_seq)`.

* **`job_id` is stable for the job's entire life.** A retry re-queues the
  *same row* — it never creates a new job — so checkpoints written on attempt 1
  are still addressed by attempt 5. This is why retries reuse the row rather
  than inserting a new one.
* **`step_seq` is assigned by call order**, not by name: the first `step()` in
  an attempt is 1, the second is 2, and so on (`_dxe_next_seq`).

Sequencing by call order is what makes the fast-forward cheap and exact, and it
is also the source of DXE's single obligation on your code:

> **Job code must be deterministic outside steps.** The sequence of `step()`
> calls must be the same on every attempt.

That obligation is *enforced*, not merely documented. The step's `name` is
recorded alongside its sequence number, and on replay a mismatch raises
`NondeterminismError` rather than silently returning another step's result:

```
step 2 was 'charge' on a previous attempt but is 'refund' now
— job code must be deterministic outside steps
```

So this is a bug DXE catches for you:

```python
if random.random() < 0.5:               # ← changes the step sequence
    await self.step("maybe", ...)
await self.step("always", ...)          # ← becomes step 1 or 2 depending
```

Put the nondeterminism *inside* a step, where its result is checkpointed:

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

| Recorded state | What happens |
|---|---|
| no row for this `step_seq` | the function **executes**, and the result is recorded |
| row exists, `error IS NULL` | the recorded `output` is **returned without executing** |
| row exists, `error` is set | the function **re-executes** (a failed step is not a result) |
| row exists, different `name` | `NondeterminismError` — nothing executes |

A retry therefore fast-forwards through the completed prefix at the cost of one
query and a dict lookup per step, then does real work only from the point of
failure onward.

**A failed step is deliberately not a cached result.** Recording the failure
buys observability (`pj-admin jobs steps <id>` shows exactly which step failed
and why) without making a transient error permanent.

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

## Fencing: why a zombie cannot corrupt a checkpoint

The dangerous case is not a crash — it is a worker that is *presumed* dead but
is still running. Its network partition heals, or its timed-out task finally
returns, and it tries to write results for a job another worker has taken over.

`jorb.run_epoch` is the fencing token that makes those writes impossible.

* It **advances whenever the job enters an attempt** (claim) **or is abandoned
  by one** (retry, monitor requeue, operator requeue).
* It is **not an attempt counter** — `run_count` is. It is monotonic and
  carries no other meaning.
* Every state-changing statement carries `AND run_epoch = $n`, so a statement
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

1. **A completed step executes at most once per job**, across every attempt,
   forever.
2. **A job keeps one row for its entire life.** Retries requeue it;
   `jorb_history` is the per-attempt audit trail.
3. **`run_epoch` only increases**, and a write at a stale epoch is a no-op.
4. **A superseded execution cannot write** a result, a checkpoint, a timeout,
   or a reschedule.
5. **The step sequence is deterministic**, or `NondeterminismError` is raised
   before anything runs.
6. **A step's result is JSON-serializable**, or it cannot be checkpointed.
7. **A failed step is re-executed**, not replayed.
8. **`crashed` is terminal** — it *is* the dead letter queue.
9. **A durable sleep holds no worker.**

These are enforced by tests, not just asserted here: see
`tests/test_dxe_primitives.py`, `tests/test_dxe_faults.py`,
`tests/test_dxe_concurrency.py`, and `tests/test_invariants.py`.

---

## Resuming an interrupted job

```
pj-admin jobs steps <id>       # what completed, what failed, timings
pj-admin jobs requeue <id>     # resume: completed steps fast-forward
pj-admin jobs requeue <id> --fresh
                               # restart: deletes checkpoints, runs from step 1
```

Use `--fresh` when the recorded results are *wrong* rather than merely
incomplete — after fixing a bug in a step, for instance. It is the only
operation that discards checkpoints for a job that is going to run again.

---

## Retention: checkpoints do not expire on their own

`jorb_step`, `jorb_history`, `jorb_event` and `jorb_mailbox` are all
`ON DELETE CASCADE` from `jorb`. They therefore live **exactly as long as the
job row**, and nothing shortens that automatically:

* A finished job keeps its full checkpoint set indefinitely.
* `jorb_history` gains a row per state transition per attempt.
* Deleting the job deletes all of it, atomically, via the cascade.

Checkpoints are kept after success on purpose — they are the audit trail of
what a job actually did, and `pj-admin jobs steps` reads them — but that means
**an installation that never deletes jobs grows without bound.**

Retention is the operator's call:

```
pj-monitor --retention-days 30              # automatic sweep (opt-in)
pj-monitor --retention-days 30 --retention-batch-size 500
pj-admin queues clear <queue> --state finished --older-than-days 30
pj-admin jobs delete <id>
```

The automatic sweep is **off by default** — a fresh install must not silently
delete an operator's history — and it is deliberately conservative:

* it removes only jobs in a **terminal** state (`finished`, `crashed`,
  `cancelled`) past the window. A `queued`, `claimed`, `running` or `waiting`
  job is never deleted at any age: a job waiting on a dependency can
  legitimately be very old.
* it will not delete a terminal job that a `waiting` job still depends on
  (via `waitfor_job` or `waitfor_group`), which would strand the waiter.
* it deletes in **bounded batches** so the reaper never takes a long lock.
* consumed mailbox messages are pruned on the same window even when their job
  is still alive — a long-running workflow reads messages for months, and the
  job-scoped cascade would never reach them.

The child rows go with the job through `ON DELETE CASCADE`.

Sizing rule of thumb: budget one `jorb_step` row per `step()` call per job, and
roughly four `jorb_history` rows per attempt.
