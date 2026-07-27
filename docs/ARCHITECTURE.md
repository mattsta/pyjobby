# pyjobby architecture

What the moving parts are, how they fit together, and why the system is
shaped the way it is.

This document is the map. Three others are the territory, and nothing here
repeats them:

| For | Read |
|---|---|
| Durable execution — steps, checkpoints, replay, invariants | [DXE.md](DXE.md) |
| Measured throughput, and the write-path decisions behind it | [SCALE.md](SCALE.md) |
| Running it — health, playbooks, queue controls, timeouts | [OPERATIONS.md](OPERATIONS.md) |
| Every column, index and trigger, with the reasoning inline | [`pyjobby/sql/schema.sql`](../pyjobby/sql/schema.sql) |

---

## One database, several small daemons

There is no broker, no scheduler service, no coordination protocol. There is
one PostgreSQL database holding all state, and a handful of independent
processes that connect to it. Nothing talks to anything else.

```
   producers                                          operators
  ┌──────────────────┐                        ┌──────────────────────────┐
  │ JobClient        │                        │ pj-admin  (CLI)          │
  │ @job registry    │                        │ pj-web    (HTML+/metrics)│
  │ DAGBuilder       │                        │ pj-ws     (dashboard)    │
  └────────┬─────────┘                        └────────────┬─────────────┘
           │ INSERT INTO jorb                              │ reads, controls
           ▼                                               ▼
  ╔══════════════════════════════════════════════════════════════════════╗
  ║                            PostgreSQL                                ║
  ║                                                                      ║
  ║   jorb ── the job, one row for life                                  ║
  ║     ├── jorb_history   every transition          (CASCADE)           ║
  ║     ├── jorb_step      DXE checkpoints           (CASCADE)           ║
  ║     ├── jorb_event     published key/values      (CASCADE)           ║
  ║     ├── jorb_mailbox   durable job-to-job mail   (CASCADE)           ║
  ║     └── jorb_dependencies  DAG edges             (CASCADE)           ║
  ║                                                                      ║
  ║   jorb_queue     control plane: paused / concurrency / rate          ║
  ║   jorb_worker    liveness registry + notification demand             ║
  ║   jorb_schedule  cron definitions (+ jorb_schedule_log)              ║
  ║   jorb_dag       DAG headers                                         ║
  ║                                                                      ║
  ║   claim_jorb()   the claim, with the controls, in one statement      ║
  ║   jorb_notify()  the one notification trigger, demand-gated          ║
  ╚═══════▲══════════════════════▲═══════════════════════▲═══════════════╝
          │                      │                       │
  ┌───────┴───────┐   ┌──────────┴─────────┐   ┌─────────┴──────────┐
  │ pj            │   │ pj-monitor         │   │ pj-scheduler       │
  │ workers       │   │ the single reaper  │   │ cron firing        │
  └───────────────┘   └────────────────────┘   └────────────────────┘
```

Every arrow is a database connection. A worker does not know the monitor
exists; the monitor does not know how many workers there are except by
reading `jorb_worker`. Start order does not matter and no process is a
leader.

---

## The components

| Process | Script | What it owns | What it does **not** do |
|---|---|---|---|
| **Worker** | `pj` | Claiming, executing job code, this attempt's state transitions, its own registry row and heartbeat, its own job-thread pool | Decide whether a queue may run at all — `claim_jorb()` does. Recover its own crash — the monitor does. Enforce any other worker's deadline. |
| **Monitor** | `pj-monitor` | Every safety-net sweep in the platform: timeouts, dead-worker reclaim, unregistered-claim reclaim, and six retention sweeps | Execute jobs, enqueue anything, elect a leader. Several instances are safe; every sweep is one atomic statement or a transaction holding its own row locks. |
| **Scheduler** | `pj-scheduler` | Firing due `jorb_schedule` rows into `jorb`, the safety checks around that (concurrency, backpressure, jitter, circuit breaker), and `jorb_schedule_log` | Run the jobs it creates — it only inserts them. Several instances are safe: each schedule is row-locked `FOR UPDATE SKIP LOCKED` while it fires, and `deadline_key` makes a duplicate insert fail. |
| **Admin CLI** | `pj-admin` | Nothing at runtime. It is a client: schema install/migrate, queue controls, DLQ, requeue, `doctor` | Participate in execution. |
| **Web admin** | `pj-web` | HTML operator UI, a JSON API, and `GET /metrics` for Prometheus | Authenticate anybody. Keep it on localhost or behind a proxy. |
| **Websocket** | `pj-ws` | The aggregate dashboard feed (one polled query per interval, shared by every client) and per-job watches | Tail individual transitions — see [the notification model](#the-notification-model). Also unauthenticated. |
| **Benchmarks** | `pj-bench` | Reproducing every number in SCALE.md, and the `pj-bench plans` CI gate that fails when a hot query stops using its index | Anything outside its own uniquely-named queue, which it deletes in a `finally`. |

A `pj` invocation is a launcher: it forks `--workers N` processes **on each
`--queue` named** (so two queues at `--workers 4` is eight processes), each
of which is **one queue, one job at a time**. Repeating a queue name asks for
nothing extra, and no worker is ever started on a queue that was not named —
`--workers` used to be a fleet total with the queue list padded out using the
literal `default`, which put workers on a queue the operator never asked for.
Concurrency is process count, not threads. A worker that finds nothing to
claim parks on a wakeup and re-polls every `--check-interval` (5 s, jittered)
regardless.

---

## The life of a job

```
  enqueue                                                        terminal
  ───────                                                        ────────
  INSERT ──► queued ──► claimed ──► running ──┬──► finished
               ▲                              ├──► crashed   (this IS the DLQ)
               │                              └──► cancelled
               │                                     │
               └── retry (same row, backoff) ────────┘
               └── reschedule / durable sleep (same row, future run_after)

  waiting ──► queued        when waitfor_job / waitfor_group is satisfied
```

The dependency wake is edge-triggered — the upstream's own terminal
transition moves its waiters to `queued` — with the monitor as the level
trigger behind it: every cycle it wakes waiters whose upstream is already
`finished` (covering a worker that crashed between its terminal write and
the wake, and a waiter enqueued after its upstream had already finished),
and cancels waiters whose `waitfor` target does not exist at all, with the
reason in `error_message`. A waiter whose upstream **crashed or was
cancelled** stays `waiting` on purpose: crashed is the DLQ and the upstream
may be retried back to life, so only the operator can decide — `pj-admin
doctor` reports these as `blocked-waiters`.

Concretely, one attempt:

1. **Enqueue.** A single `INSERT INTO jorb`, shared by every producer path
   (`enqueue`, `enqueue_batch`, `enqueue_in_transaction`, the `@job`
   registry, `DAGBuilder`). A row with `waitfor_job`/`waitfor_group` is
   inserted `waiting` instead of `queued`. A `deadline_key` makes the
   enqueue idempotent: a partial unique index over `(deadline_key, queue)
   WHERE state = 'queued'` means the second insert of the same future job
   raises a unique violation rather than duplicating the work.

2. **Claim.** The worker calls `claim_jorb()`, which returns at most one
   row and does everything in one statement: state → `claimed`, `run_count
   + 1`, **`run_epoch + 1`**, `claimed_at`, `claimed_by`, `worker_pid`,
   `worker_host`. Zero rows back means "nothing claimable", which covers
   an empty queue, a paused queue, and a queue at its cap identically.

3. **Prepare.** The worker resolves the job class (cached — importing per
   job re-executes module code), loads any `jorb_step` checkpoints and
   binds them to the instance, resolves the timeout
   (`admin_data.timeout_seconds` → class `timeout` → `--default-timeout`),
   and stamps `timeout_at`.

4. **Run.** `claimed → running` stamps `started`. Then the job's `run()`
   goes to *this worker's own* thread pool — not the event loop's default
   executor, so a runaway job cannot take the worker's own I/O down with
   it — under exactly one deadline. Whatever comes back is reduced: a
   coroutine is awaited, an async generator is drained.

5. **Finish.** `finished` with the result, `finished` timestamp, and
   `timeout_at` cleared. Then dependents are woken: rows `waiting` on this
   job, and rows waiting on its whole `run_group` once no member is
   unfinished.

6. **Or fail.** One path handles exceptions and timeouts alike: retry the
   **same row** with backoff (`state = 'queued'`, `run_epoch + 1`,
   `error_count + 1`, `run_after = now() + delay`), or, once the retry
   budget is spent, `crashed`. There is no separate dead-letter table —
   the DLQ is exactly `WHERE state = 'crashed'`.

Every step from 3 onward is **fenced**: each statement carries `AND
run_epoch = $n`, and the terminal ones also carry `AND state IN ('claimed',
'running')`. A worker that lost the row while it was running writes
nothing.

Two shapes are worth noticing because they look like exceptions and are
not. A **durable sleep** is not a parked worker: it checkpoints a wake time
and requeues the row with a future `run_after`, so a job can sleep for
months while occupying nothing. And a **retry keeps the row**, so the job
id a caller holds stays valid for the job's whole life — the per-attempt
detail lives in `jorb_history`, not in new rows.

---

## Claiming lives in the database

The claim is a PL/pgSQL function, `claim_jorb()`, and not a query the
worker composes. That is the load-bearing choice in the whole design.

`jorb_queue` is a live control plane: a row per queue carrying `paused`,
`max_concurrency`, `rate_limit` and `rate_period_seconds` (an absent row
means unpaused and unlimited). Those controls are checked **inside the
claim**, which means they bind *every* claimer — the worker, a test
harness, a benchmark, anything anybody writes next — rather than only the
clients that remember to enforce them. An operator pausing a queue does not
have to trust that the code claiming from it is well-behaved.

Enforcing them there is also the only way they can be *correct*. Under READ
COMMITTED a statement sees the snapshot taken when it began, so two
simultaneous claims cannot see each other's uncommitted rows, and a
concurrency cap of 1 would admit both. A cap needs the claims for a
controlled queue serialised against each other, and a single statement
cannot do that.

So a **controlled** queue takes a per-queue advisory lock first, and then
counts — after the lock, in a later statement, therefore against a snapshot
that includes every claim already committed. An **uncontrolled** queue — the
common case — never takes the lock at all and keeps the lock-free fast
path.

The lock wait is bounded (`lock_timeout = 50 ms`, in a wrapper function so
the timeout covers the acquisition and nothing else). A timeout is reported
as "nothing claimable", identical to an empty queue. The bound is what
stops one claim held open by a stuck transaction from freezing the queue;
the *wait* rather than an immediate try-lock is what puts claimers in the
lock manager's FIFO queue, so they take turns instead of one winner
starving the rest. `schema.sql` carries the measurements for both.

The row itself is picked with `FOR UPDATE SKIP LOCKED` over
`jorb_claim_idx (queue, prio, run_after) WHERE state = 'queued'`, ordered
by priority then run time — and because the order is `ASC`, **the smallest
`prio` is claimed first**: it is a finishing position, not a rating. A
worker claims only `prio <= its ceiling` (`pj --max-prio`, default 1000),
so past the ceiling a bigger number stops meaning "later" and starts
meaning "never"; the client refuses those enqueues for that reason. It also
claims only jobs whose `capability` it advertises (or which name none).

---

## The notification model

`NOTIFY` is not cheap in the way it looks. Committing a transaction that
issued one takes a **global** exclusive lock held until that commit
completes and fsyncs, because notifications must be delivered in commit
order and commit order is not known until commits finish. Every
notification-bearing commit therefore serialises against every other one,
defeating group commit. Measured on this schema in production shape, that
cost two thirds of concurrent write throughput.

Crucially the lock is taken **per commit, not per notification**. Trimming
channels buys nothing; a transaction that notifies three times pays what a
transaction that notifies once pays. The only thing that helps is not
notifying — and a notification exists to wake a consumer, while under load
the consumers are never asleep.

Hence the policy, applied uniformly:

> **A notification is emitted only when a consumer has registered demand for
> its topic, and demand is registered *before* that consumer's last look at
> the underlying state.**

The cost then scales *inversely* with load, which is exactly right: when
the system is busy nobody is parked and a notification would be pure
overhead paid at the global commit lock; when the system is idle, latency
matters but volume is low and the lock is free. Measured end to end: **zero
notifications per job lifecycle** when nobody is watching, two when
somebody is.

One function, `jorb_notify()`, implements every channel. Its trigger
arguments are `(channel, demand kind)`, and the topic, the gate and the
payload for all five channels are declared in that one body — so changing
the convention is one edit, not seven.

| Channel | Fires on | Demand | "Somebody is waiting" means |
|---|---|---|---|
| `jorb_enqueued` | `jorb` insert/state → `queued` | `idle_worker` | some worker on that queue published `jorb_worker.idle` |
| `jorb_done` | `jorb` state → terminal | `row_local` | `jorb.awaited` on the very row changing |
| `jorb_event` | `jorb_event` insert/update | `job_awaited` | `jorb.awaited` on the publishing job |
| `jorb_cancel` | `jorb.cancel_requested` set | `row_local` | the job is actually `running` |
| `schedule_executed` | `jorb_schedule_log` insert | **ungated** | — see below |

The gate runs before the payload is built, so a write path that turns out
not to need a notification does not pay to construct one.

### Two shapes of correctness argument

The demand *storage* is deliberately not uniform — each channel uses the
cheapest correct signal for its own shape — and that produces two different
proofs that no wakeup is lost.

**Row-local, ordered by the row lock** (`jorb_done`). The demand flag
`jorb.awaited` sits on the same row as the state change, so the waiter and
the worker take the same row lock and PostgreSQL orders them:

* The waiter's `awaited = TRUE` commits first → the worker's terminal
  `UPDATE` either already saw it, or blocked on the row lock and
  re-evaluated against the newest version, where it is true. The trigger
  fires; the waiter is woken.
* The terminal `UPDATE` commits first → the waiter's registration
  necessarily commits after it, so the waiter's *first* state read — which
  always runs before it waits — already sees the terminal state, and it
  never waits.

There is no third case: one of the two commits first. The client's 2 s
fallback poll is a safety net this argument does not need. Because the
flag is row-local, it is also the trigger's `WHEN` clause, so the executor
evaluates it for free and `jorb_notify()` is not even entered when nobody
is waiting.

**Deferred constraint trigger, when demand lives on another table**
(`jorb_enqueued`). Here the signal is `jorb_worker.idle` — a different
table, so no row lock orders the two writers. An ordinary `AFTER` trigger
would decide whether to notify from the snapshot taken when the `INSERT`
ran, possibly long before that insert became visible; every job enqueued
inside a transaction that stays open would lose its wakeup. So the trigger
is `DEFERRABLE INITIALLY DEFERRED`: the decision and the delivery happen at
the same instant, at commit.

The worker supplies the other half of the ordering by publishing
`idle = TRUE` **before** its last claim attempt, never after:

```
set idle = TRUE  →  claim  →  got a job?  clear idle, run it
                           →  got nothing? sleep on the wakeup
```

An enqueue whose gate runs after that commit sees the worker and notifies
it. An enqueue that committed before the following claim's snapshot is
found *by* that claim. There is no order in which a job is both unseen and
unannounced, apart from the sub-millisecond window between an enqueue's WAL
flush and its visibility — covered by the worker's unconditional poll every
`--check-interval`. Reversing the worker's order would widen that window to
a whole claim round trip on every park.

`jorb_event` is the honest third case: the demand is on another table
*and* the trigger is not deferred, because `get_event()` routinely waits
for a key the job has not published yet, so there is no row to hang a
row-local flag on. A client that registers while a `set_event()` is
mid-commit can miss that one notification and learns the value from its 2 s
fallback poll instead. Bounded latency on a race, never a lost value — the
event itself is durable in `jorb_event`.

The `idle` and `awaited` flags are written only on transition
(`WHERE idle IS DISTINCT FROM $2`, `WHERE NOT awaited`), so a busy worker
never writes one and a re-watch is not a row write. `awaited` is a latch
that dies with the row, never a refcount to leak; `idle` is cleared on the
next claim, on graceful shutdown, and by the monitor when it retires a dead
worker — so a crashed worker cannot leave a queue's notifications switched
on forever.

### When a channel must not be gated

A gate trades a notification for the consumer's polling fallback. A
consumer with **no** fallback cannot pay that: a skipped notification
becomes an event it never learns about, not one it learns about late. That
is why `schedule_executed` is ungated (its consumer is a browser), and it
costs nothing because it fires at cron rate on `jorb_schedule_log`, not on
any hot path. `jorb_cancel` is gated only by `state = 'running'` for the
same reason — a skipped cancellation never happens — and fires at operator
rate.

### There is no per-transition channel, and there must not be one

The five channels above are all of them. Nothing notifies on
`queued → claimed → running → finished`, and adding such a trigger would
undo the whole model in one edit — the lock is per commit, so a single
ungated channel firing four times per job costs exactly what all seven
would. Reinstating it measures 2.6–2.9× *slower* on the completion path
(`tests/test_notify_gating.py` builds that trigger on purpose so the number
stays measurable).

Nor could it be gated, because the consumer it would serve is a browser
with no polling fallback. So the dashboard is served the other way instead.
At the reference workload a per-transition feed is ~830 events per second,
which no dashboard renders and no human reads, so `pj-ws` **polls
aggregates**: one index-backed `UNION ALL` per second, shared by every
connected dashboard, and none at all while nobody is subscribed. That is
O(1) in both dashboards and job throughput. A client that genuinely needs
one job watches *that* job, which rides `jorb_done` and is gated on
`jorb.awaited` like any other waiter.

---

## Liveness, fencing and recovery

Three mechanisms, and only together do they make a presumed-dead worker
safe.

**The registry.** Every worker inserts a `jorb_worker` row at startup and
heartbeats `last_seen` on a **dedicated connection**, so a long-running job
on the main connection can never delay liveness reporting. Liveness is that
heartbeat and nothing else — not a pid, not a grace period, not a guess.
The same heartbeat statement also publishes the worker's job-thread pool
size and how many of its threads are *abandoned* (left behind by timed-out
synchronous jobs, which nothing can interrupt), because a worker whose pool
is full of those refuses to claim while looking healthy by every other
signal. Publishing it in the registry is what makes that visible to the
whole platform for no extra statement.

**The sweeps.** `pj-monitor` runs them all, every `--check-interval`
(10 s), each isolated so one failure cannot cancel the rest of the cycle:

* *Timeout enforcement* — `running` jobs past `timeout_at` are retried or
  dead-lettered per their `on_timeout` policy. Workers enforce timeouts
  in-process too; this catches the ones that died mid-job.
* *Dead-worker reclaim* — jobs `claimed`/`running` behind a stale
  `last_seen` are requeued, on any host, and their workers retired (which
  also clears `idle`, bounding the leaked subscription to the liveness
  grace).
* *Unregistered-claim reclaim* — jobs stuck `claimed` with no
  `claimed_by`, past a longer grace. A worker died between claiming and
  registering, or the registry was unavailable; age is the only signal
  available.

**The fence.** `run_epoch` is the token that makes all of this safe, and it
is deliberately **not** an attempt counter (`run_count` is). It advances
whenever a job *enters* an attempt (a claim) or is *abandoned* by one — a
retry, a monitor requeue, an operator requeue. Every state-changing
statement a worker issues carries `AND run_epoch = $n`.

So a "dead" worker that was merely partitioned, and is still executing, can
do no harm when it comes back: its completion matches zero rows, its
checkpoint writes match zero rows and raise `StaleExecutionError`, and it
abandons the attempt quietly. Requeueing bumps the epoch *itself* rather
than leaving that to the next claim, because otherwise the abandoned
execution would keep the current epoch for the whole window between requeue
and re-claim and could still write checkpoints for an attempt that has been
replaced. This is what lets recovery be a plain `UPDATE` with no
distributed agreement anywhere. The DXE half of the argument is in
[DXE.md](DXE.md#fencing-why-a-zombie-cannot-corrupt-a-checkpoint).

---

## Retention

Left alone, a platform that runs indefinitely accumulates every job it ever
ran. Retention is therefore **on by default** — a policy nobody remembers
to switch on is not a policy — and `0` on either window means "keep
forever".

Six sweeps on two windows. `--checkpoint-retention-days` governs the first;
`--retention-days` governs the other five, because none of those tables has
a lifetime of its own to argue for — they all mean "as long as the work they
describe".

| Sweep | Default | What goes | Why this window |
|---|---|---|---|
| Checkpoints | 1 day | `jorb_step` rows of terminal jobs; the job row stays | Checkpoints exist to make a job *resumable*. The instant it terminates, resume is impossible — so they are the bulkiest thing hanging off a job with the shortest useful life. They outlive the terminal transition only far enough to debug it. |
| Jobs | 30 days | The whole `jorb` row, and its history, events, mailbox, checkpoints and DAG edges by `ON DELETE CASCADE` | The job's own audit lifetime. |
| Consumed mail | 30 days | `jorb_mailbox` rows with `consumed_at` set | The job-scoped cascade cannot reach these: a long-lived workflow reads mail for months and never terminates, so nothing else would ever free them. |
| Orphaned DAGs | 30 days | `jorb_dag` rows past the window with no jobs left | Jobs point **at** a DAG (`ON DELETE SET NULL`), so job retention never touches it. Left alone it does not merely linger, it keeps *answering*: `jorb_dag_status` LEFT JOINs `jorb`, so an emptied DAG reports `total_jobs = 0` forever. |
| Schedule log | 30 days | `jorb_schedule_log` rows, except each schedule's newest | It cascades only from `jorb_schedule`, which operators disable rather than delete — so it had no upper bound of any kind, at cron rate, for the life of the install. |
| Retired workers | 30 days | `jorb_worker` rows both retired and silent for the window | One row per worker *process start*, and nothing ever deleted one: a fleet that redeploys accumulates registry rows for as long as it exists. |

The two lifetimes are deliberately independent — that is the whole point of
splitting them. Every one of these sweeps also **refuses** to delete a row
something live still needs, and the refusals are the half worth reading:
[OPERATIONS.md § Retention](OPERATIONS.md#retention-what-it-deletes-and-what-it-refuses-to)
has them, along with the operator-facing knobs.

Two properties keep the sweeps honest. They **drain**: a cycle keeps taking
batches until it is caught up or spends `--retention-max-seconds` (5 s),
then yields. One batch per cycle would be a fixed deletion rate that a busy
install simply outruns, leaving retention nominally enabled while the table
grows forever. And the budget is what keeps a backlog from delaying the
latency-critical sweeps above, which decide how long a stuck job stays
stuck. Falling behind is logged at WARNING, because silence would read
exactly like being caught up.

A checkpoint of a *non-terminal* job is never touched at any age — a
durable sleep parks a job in `queued` for months holding the very
checkpoint that records when to wake. And a terminal job that something is
still `waiting` on is kept regardless of age: `waitfor_job` and
`waitfor_group` carry no foreign key, so deleting the upstream would strand
the waiter forever.

Every sweep's SQL is a module constant rather than an inline literal,
because `pj-bench plans` EXPLAINs those exact strings as a CI gate. The
number that matters there is rows-removed-by-filter, not the access method:
an index scan that discards everything it reads is not a sequential scan
and costs the same, so the gate fails on either.

The gate's case list is **derived** from the `SWEEP_*_SQL` constants
`monitor.py` defines, not written out beside them: a hand-maintained roster
is what let three sweeps ship ungated while the gate went on reporting
success, and a green gate with a hole in it is worse than no gate. A sweep
with no gate entry now fails the run. Each sweep is measured in two states —
caught up and full backlog — because a plan can be perfect in one and read
the whole table in the other, which is exactly how the consumed-mail sweep
hid a sequential scan of `jorb_mailbox` behind a clean two-buffer
steady-state probe.

---

## Where state lives

**One row per job, for its whole life.** `jorb.id` is stable across every
retry, resume and operator requeue, so a caller's handle never goes stale.
Everything per-*attempt* lives elsewhere.

| Table | Grain | Lifetime |
|---|---|---|
| `jorb` | one row per job, forever | retention window |
| `jorb_history` | one row per transition (~4 per job) — the per-attempt trail | cascade from `jorb` |
| `jorb_step` | one row per DXE checkpoint | its own, much shorter window |
| `jorb_event` | one row per `(job, key)` published | cascade from `jorb` |
| `jorb_mailbox` | one row per durable message | cascade, **or** the consumed-mail sweep |
| `jorb_dependencies` | DAG edges, two FKs to `jorb`, both cascading | cascade from either end |
| `jorb_queue` | one row per *controlled* queue; absent = defaults | operator-managed |
| `jorb_worker` | one row per worker *process* | retired by the monitor, then the retired-worker sweep; never while it owns in-flight jobs |
| `jorb_schedule` | one cron definition | operator-managed |
| `jorb_schedule_log` | one row per schedule *execution* | the schedule-log sweep, which always keeps each schedule's newest |
| `jorb_dag` | one DAG header; `jorb.dag_id` is `ON DELETE SET NULL` | the orphaned-DAG sweep, once it is past the window **and** has no jobs left |

`jorb_history` is recorded **by trigger**, not by the writers. The worker,
monitor, scheduler, admin API and websocket server all mutate `jorb`;
putting the audit in a trigger means none of them can forget to, and none
of them has to know it exists. `jorb_dag.completed` is stamped the same way
for the same reason.

`jorb.tags` and `jorb.admin_data` are deliberately different columns.
`tags` is the *caller's* labels (customer, region, batch) — flat, and the
only JSONB with an index, because filtering on them is a thing applications
do. `admin_data` is the *platform's* own execution config (`max_retries`,
`timeout_seconds`, `on_timeout`, `save_result`, schedule metadata), which
nobody filters on, so indexing it would tax every enqueue to make no query
faster.

`jorb_worker` is a plain registry, not a foreign key: `jorb.claimed_by`
holds a worker id with no constraint behind it, so retiring a worker can
never cascade into jobs.

---

## The principle that ties it together

Most of the recent decisions look arbitrary in isolation and stop looking
arbitrary once you have this:

> **The write path does the minimum necessary work inside the transaction.
> Everything optional is either made conditional, or moved out.**

*Made conditional:*

* Notifications fire only against registered demand — the gate is
  evaluated before the payload is built, and for `jorb_done` it is the
  trigger's `WHEN` clause, so the function is not even entered.
* The claim's serialising lock is taken only for queues that actually have
  a control set; everything else keeps the lock-free path.
* `idle` and `awaited` are written only on transition, so the flags cost
  one write per park and per await, not one per poll.
* The GIN index on `tags` is partial, so an untagged enqueue — the hot
  shape — is not in the index at all. Same reasoning for `run_group`,
  `uid` and `dag_id`.
* `updated` is deliberately *not* indexed: it is rewritten by every state
  change, so an index on it would add a write to each of the ~4 updates per
  job and block HOT updates on the hottest table in the system. Reporting
  windows key off `created`, which is written once.

*Moved out:*

* The dashboard firehose became a polled aggregate — cost O(1) in both
  dashboards and throughput, instead of O(transitions).
* Retention, timeout enforcement and crash recovery are all the monitor's
  work on its own schedule, never a producer's or a worker's.
* Durable sleep leaves the process entirely: the sleep is a future
  `run_after` in the database, not a parked worker.
* Job code runs on the worker's own thread pool, off the event loop that
  is holding the deadline.

The counterweight, so this does not become an excuse to do too little: the
minimum genuinely necessary work stays *inside* the transaction. The
history trigger, the epoch fence and the claim's control checks are all on
the write path precisely because moving them out would make them
optional — and a correctness property that a caller can forget to enforce
is not a property.
