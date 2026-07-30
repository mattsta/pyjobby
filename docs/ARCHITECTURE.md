# pyjobby architecture

What the moving parts are, how they fit together, and why the system is
shaped the way it is.

This document is the map. Three others are the territory, and nothing here
repeats them:

| For                                                         | Read                                            |
| ----------------------------------------------------------- | ----------------------------------------------- |
| Durable execution — steps, checkpoints, replay, invariants  | [DXE.md](DXE.md)                                |
| Measured throughput, and the write-path decisions behind it | [SCALE.md](SCALE.md)                            |
| Running it — health, playbooks, queue controls, timeouts    | [OPERATIONS.md](OPERATIONS.md)                  |
| Every column, index and trigger, with the reasoning inline  | [`pyjobby/sql/schema/`](../pyjobby/sql/schema/) |

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
  ║     ├── jorb_stream    ordered durable output    (CASCADE)           ║
  ║     ├── jorb_mailbox   durable job-to-job mail   (CASCADE)           ║
  ║     └── jorb_dependencies  DAG edges             (CASCADE)           ║
  ║                                                                      ║
  ║   jorb_queue     control plane: paused / concurrency / rate,         ║
  ║                  counted per queue or per lane                       ║
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

| Process        | Script         | What it owns                                                                                                                                                                                       | What it does **not** do                                                                                                                                                                                                                                                                                               |
| -------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Worker**     | `pj`           | Claiming, executing job code, this attempt's state transitions, its own registry row and heartbeat, its own job-thread pool                                                                        | Decide whether a queue may run at all — `claim_jorb()` does. Recover its own crash — the monitor does. Enforce any other worker's deadline.                                                                                                                                                                           |
| **Monitor**    | `pj-monitor`   | Every safety-net sweep in the platform: timeouts, dead-worker reclaim, stuck-claim reclaim, stranded-waiter recovery, and seven retention sweeps                                                   | Execute jobs, enqueue anything, elect a leader. Several instances are safe; every sweep is one atomic statement or a transaction holding its own row locks.                                                                                                                                                           |
| **Scheduler**  | `pj-scheduler` | Firing due `jorb_schedule` rows into `jorb`, the safety checks around that (concurrency, backpressure, jitter, circuit breaker), the **bounded** catch-up after an outage, and `jorb_schedule_log` | Run the jobs it creates — it only inserts them. Catch up without a ceiling: `backfill_limit` is both the opt-in and the bound, so an outage cannot become a flood. Several instances are safe: each schedule is row-locked `FOR UPDATE SKIP LOCKED` while it fires, and `deadline_key` makes a duplicate insert fail. |
| **Admin CLI**  | `pj-admin`     | Nothing at runtime. It is a client: schema install/migrate, queue controls, DLQ, requeue, `doctor`                                                                                                 | Participate in execution.                                                                                                                                                                                                                                                                                             |
| **Web admin**  | `pj-web`       | HTML operator UI, a JSON API, and `GET /metrics` for Prometheus                                                                                                                                    | Authenticate anybody. Keep it on localhost or behind a proxy.                                                                                                                                                                                                                                                         |
| **Websocket**  | `pj-ws`        | The aggregate dashboard feed (one polled query per interval, shared by every client) and per-job watches                                                                                           | Tail individual transitions — see [the notification model](#the-notification-model). Also unauthenticated.                                                                                                                                                                                                            |
| **Benchmarks** | `pj-bench`     | Reproducing every number in SCALE.md, and the `pj-bench plans` CI gate that fails when a hot query stops using its index                                                                           | Anything outside its own uniquely-named queue, which it deletes in a `finally`.                                                                                                                                                                                                                                       |

A `pj` invocation is a launcher: it forks `--workers N` processes **on each
`--queue` named** (so two queues at `--workers 4` is eight processes), each
of which is **one queue, one job at a time**. Repeating a queue name asks for
nothing extra, and no worker is ever started on a queue that was not named.
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
   inserted `waiting` instead of `queued`. Any of three
   [dedupe keys](#three-dedupe-keys-three-partial-unique-indexes) may make
   the insert conditional, each enforced by a partial unique index rather
   than by the producer.

2. **Claim.** The worker calls `claim_jorb()`, which returns at most one
   row and does everything in one statement: state → `claimed`, `run_count
   - 1`, **`run*epoch + 1`**, `claimed_at`, `claimed_by`, `worker_pid`,
`worker_host`. Zero rows back means "nothing claimable", which covers
     an empty queue, a paused queue, a queue at its cap, a lane at \_its*
     cap, and a backlog pinned to a code version this worker does not
     advertise — identically.

3. **Prepare.** The worker resolves the job class (cached — importing per
   job re-executes module code), loads any `jorb_step` checkpoints and
   binds them to the instance, resolves the timeout
   (`admin_data.timeout_seconds` → class `timeout` → `--default-timeout`),
   and stamps `timeout_at`.

4. **Run.** `claimed → running` stamps `started`. Then the job's `run()`
   goes to _this worker's own_ thread pool — not the event loop's default
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

Three shapes are worth noticing because they look like exceptions and are
not. A **durable sleep** is not a parked worker: it checkpoints a wake time
and requeues the row with a future `run_after`, so a job can sleep for
months while occupying nothing. A **retry keeps the row**, so the job id a
caller holds stays valid for the job's whole life — the per-attempt detail
lives in `jorb_history`, not in new rows. And a **fork does not keep the
row**, which is the whole point of it: it inserts a second job that
re-executes the first one's work from step N, with steps 1..N−1 copied in
as its own checkpoints so they fast-forward, and leaves the source
completely untouched — any state, running included. That is why a fork can
change what a requeue cannot (queue, priority, arguments, code version)
and why it cannot inherit an identity.

---

## Claiming lives in the database

The claim is a PL/pgSQL function, `claim_jorb()`, and not a query the
worker composes. That is the load-bearing choice in the whole design.

`jorb_queue` is a live control plane: a row per queue carrying `paused`,
`max_concurrency`, `rate_limit`, `rate_period_seconds` and
`partition_limits` (an absent row means unpaused and unlimited). Those
controls are checked **inside the claim**, which means they bind _every_
claimer — the worker, a test harness, a benchmark, anything anybody writes
next — rather than only the clients that remember to enforce them. An
operator pausing a queue does not have to trust that the code claiming from
it is well-behaved.

Enforcing them there is also the only way they can be _correct_. Under READ
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
the _wait_ rather than an immediate try-lock is what puts claimers in the
lock manager's FIFO queue, so they take turns instead of one winner
starving the rest. `sql/schema/30_claim.sql` carries the measurements for both.

The row itself is picked with `FOR UPDATE SKIP LOCKED` over
`jorb_claim_idx (queue, prio, run_after) WHERE state = 'queued'`, ordered
by priority then run time — and because the order is `ASC`, **the smallest
`prio` is claimed first**: it is a finishing position, not a rating. A
worker claims only `prio <= its ceiling` (`pj --max-prio`, default 1000),
so past the ceiling a bigger number stops meaning "later" and starts
meaning "never"; the client refuses those enqueues for that reason.

### What else the claim filters on

Three more things decide whether a candidate row may be claimed. Two of
them are per-row filters over the rows `jorb_claim_idx` already returned —
one comparison against a scalar the worker passed in, costing what
`capability` costs — and the third is not, which is why it is a separate
statement rather than another clause.

- **`capability`.** A job that names one is claimed only by a worker
  advertising it; a job that names none is claimed by anyone. This
  direction is _worker declares, job requires_.
- **`app_version`.** The opposite direction, and the reason it is worth
  spelling out: an **unversioned** job — the default, and every job unless
  a version was declared — is claimable by every worker, versioned workers
  included. Only a job that pins itself asks the fleet to match it. The
  gate is therefore opt-in per job, and there is deliberately no
  worker-side "matching work only" flag: the cell that flag would add — a
  versioned worker refusing unversioned work — is a fleet that stops
  draining its own backlog halfway through a deploy.
- **The lane, on a partitioned queue.** Its test reads a set computed under
  the queue lock, not a scalar, so a queue with no lanes would otherwise be
  carrying a plan built for one. Forking the claim on the version as well
  would give four statements to keep in step and buy nothing. See below.

Nothing can refuse a pin at enqueue time, because the fleet it must match
is whatever is running when the job is finally claimed. A job pinned to a
build nobody runs is therefore unclaimable in exactly the way a job above
every ceiling is, and it is caught the same three ways: `pj-admin doctor`'s
unclaimable sweep counts it per queue, `pj-admin jobs why` names it, and
an idle worker logs it once a minute. `jorb_app_version_idx` exists for
those three readers and not for the claim — they ask the _inverse_
question ("is any queued job here pinned to a version nobody advertises?"),
which the claim's own index cannot answer at all. It is partial on
`app_version IS NOT NULL`, so a deployment that never stamps a job has an
index that is physically empty and an enqueue path that never touches it.

### Fair-share lanes: the same limits, counted per key

A queue-wide `max_concurrency` is a fair-share scheme with one
participant. Put two tenants on the queue and the busier one takes the
whole cap while the other's work sits queued, runnable, and never claimed.
`jorb_queue.partition_limits` re-scopes the limits that queue **already
has** to each distinct value of `jorb.partition_key`, so `max_concurrency
4` means four in flight _per lane_ and `rate_limit R` means R admissions
per lane per window.

It **adds no limit of its own**: on a queue with neither limit set, turning
it on changes nothing, and that queue stays on the lock-free fast path
exactly as before. Partitioning is not a third cost tier — it changes what
the counts count, never who serialises.

The shape of the per-lane check is what makes it a separate statement. A
queue-wide limit answers "is the queue full?" and returns; a per-lane limit
cannot, because the answer differs per lane and a lane with headroom must
still be served. So the counts do not decide whether to claim — they
produce the set of lanes this attempt may not claim _from_, and the claim
skips exactly those. That set is bounded by the same things the old counts
were (at most `in-flight / max_concurrency` lanes can be at the concurrency
cap; at most `admissions / rate_limit` at the rate limit), never by the
backlog or by how many lanes exist: there is no scan over the distinct
values of `partition_key` anywhere in the function.

Two properties fall out of that and are load-bearing:

- **Claim order is unchanged** — `prio, run_after`, served by
  `jorb_claim_idx`. Partitioning decides which rows are _eligible_, never
  which eligible row wins.
- **The NULL lane is a lane.** Jobs carrying no `partition_key` form one
  lane among the others, counted and capped and admitted like a named one.
  They are never hidden from the claim and never refused for being
  unlabelled, which is what lets a live queue adopt the key one producer at
  a time. A scheme that silently blackholes the work nobody remembered to
  label is a worse failure than the starvation it was meant to fix.

The cost is paid only where the feature is working: when nothing is
saturated the blocked set is empty and the scan stops on the first index
entry, exactly as the unpartitioned claim does. When lanes _are_ saturated,
the scan walks past their queued rows to reach one with headroom — a cost
proportional to the backlog of the tenants being held back, which is
measured in [SCALE.md](SCALE.md#partitioned-claims-what-fairness-costs).
`jorb_partition_inflight_idx` keeps the per-lane count itself an index-only
scan over one queue's in-flight rows, because it runs inside the queue lock
— the most expensive place in the system for a query to be slow.

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
> its topic, and demand is registered _before_ that consumer's last look at
> the underlying state.**

The cost then scales _inversely_ with load, which is exactly right: when
the system is busy nobody is parked and a notification would be pure
overhead paid at the global commit lock; when the system is idle, latency
matters but volume is low and the lock is free. Measured end to end: **zero
notifications per job lifecycle** when nobody is watching, two when
somebody is.

One function, `jorb_notify()`, implements every channel. Its trigger
arguments are `(channel, demand kind)`, and the topic, the gate and the
payload for all six channels are declared in that one body — so changing
the convention is one edit, not six.

| Channel             | Fires on                       | Demand        | "Somebody is waiting" means                            |
| ------------------- | ------------------------------ | ------------- | ------------------------------------------------------ |
| `jorb_enqueued`     | `jorb` insert/state → `queued` | `idle_worker` | some worker on that queue published `jorb_worker.idle` |
| `jorb_done`         | `jorb` state → terminal        | `row_local`   | `jorb.awaited` on the very row changing                |
| `jorb_event`        | `jorb_event` insert/update     | `job_awaited` | `jorb.awaited` on the publishing job                   |
| `jorb_stream`       | `jorb_stream` insert           | `job_awaited` | `jorb.awaited` on the appending job                    |
| `jorb_cancel`       | `jorb.cancel_requested` set    | `row_local`   | the job is actually `running`                          |
| `schedule_executed` | `jorb_schedule_log` insert     | **ungated**   | — see below                                            |

The gate runs before the payload is built, so a write path that turns out
not to need a notification does not pay to construct one.

### Two shapes of correctness argument

The demand _storage_ is deliberately not uniform — each channel uses the
cheapest correct signal for its own shape — and that produces two different
proofs that no wakeup is lost.

**Row-local, ordered by the row lock** (`jorb_done`). The demand flag
`jorb.awaited` sits on the same row as the state change, so the waiter and
the worker take the same row lock and PostgreSQL orders them:

- The waiter's `awaited = TRUE` commits first → the worker's terminal
  `UPDATE` either already saw it, or blocked on the row lock and
  re-evaluated against the newest version, where it is true. The trigger
  fires; the waiter is woken.
- The terminal `UPDATE` commits first → the waiter's registration
  necessarily commits after it, so the waiter's _first_ state read — which
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
found _by_ that claim. There is no order in which a job is both unseen and
unannounced, apart from the sub-millisecond window between an enqueue's WAL
flush and its visibility — covered by the worker's unconditional poll every
`--check-interval`. Reversing the worker's order would widen that window to
a whole claim round trip on every park.

`jorb_event` and `jorb_stream` are the honest third case: the demand is on
another table _and_ the trigger is not deferred, because `get_event()`
routinely waits for a key the job has not published yet and
`read_stream()` routinely starts before the first row exists — so there is
no row to hang a row-local flag on, and the latch has to live on the job.
A client that registers while a `set_event()` or a `stream_write()` is
mid-commit can miss that one notification and learns the value from its
2 s fallback poll instead. Bounded latency on a race, never a lost value —
both are durable rows, and a stream reader re-reads from its own offset.

That the stream latch is per _job_ rather than per key is the price of a
demand signal cheap enough to evaluate on the write path: a job that
streams while anything at all awaits it pays a notification per append,
bounded by the appends the job chooses to make.

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

The six channels above are all of them. Nothing notifies on
`queued → claimed → running → finished`, and adding such a trigger would
undo the whole model in one edit — the lock is per commit, so a single
ungated channel firing four times per job costs exactly what all six
would. Reinstating it measures 2.6–2.9× _slower_ on the completion path
(`tests/test_notify_gating.py` builds that trigger on purpose so the number
stays measurable).

Nor could it be gated, because the consumer it would serve is a browser
with no polling fallback. So the dashboard is served the other way instead.
At the reference workload a per-transition feed is ~830 events per second,
which no dashboard renders and no human reads, so `pj-ws` **polls
aggregates**: one index-backed `UNION ALL` per second, shared by every
connected dashboard, and none at all while nobody is subscribed. That is
O(1) in both dashboards and job throughput. A client that genuinely needs
one job watches _that_ job, which rides `jorb_done` and is gated on
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
size and how many of its threads are _abandoned_ (left behind by timed-out
synchronous jobs, which nothing can interrupt), because a worker whose pool
is full of those refuses to claim while looking healthy by every other
signal. Publishing it in the registry is what makes that visible to the
whole platform for no extra statement.

**The sweeps.** `pj-monitor` runs them all, every `--check-interval`
(10 s), each isolated so one failure cannot cancel the rest of the cycle:

- _Timeout enforcement_ — `running` jobs past `timeout_at` are retried or
  dead-lettered per their `on_timeout` policy. Workers enforce timeouts
  in-process too; this catches the ones that died mid-job.
- _Dead-worker reclaim_ — jobs `claimed`/`running` behind a stale
  `last_seen` are requeued, on any host, and their workers retired (which
  also clears `idle`, bounding the leaked subscription to the liveness
  grace).
- _Unregistered-claim reclaim_ — jobs stuck `claimed` with no
  `claimed_by`, past a longer grace. A worker died between claiming and
  registering, or the registry was unavailable; age is the only signal
  available.

**The fence.** `run_epoch` is the token that makes all of this safe, and it
is deliberately **not** an attempt counter (`run_count` is). It advances
whenever a job _enters_ an attempt (a claim) or is _abandoned_ by one — a
retry, a monitor requeue, an operator requeue. Every state-changing
statement a worker issues carries `AND run_epoch = $n`.

So a "dead" worker that was merely partitioned, and is still executing, can
do no harm when it comes back: its completion matches zero rows, its
checkpoint writes match zero rows and raise `StaleExecutionError`, and it
abandons the attempt quietly. Requeueing bumps the epoch _itself_ rather
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

Seven sweeps on two windows. `--checkpoint-retention-days` governs the first;
`--retention-days` governs the other six, because none of those tables has
a lifetime of its own to argue for — they all mean "as long as the work they
describe".

| Sweep           | Default | What goes                                                                                                         | Why this window                                                                                                                                                                                                                                            |
| --------------- | ------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Checkpoints     | 1 day   | `jorb_step` rows of **`finished`** jobs only; the job row stays                                                   | Checkpoints exist to make a job _resumable_. Once it has succeeded there is nothing left to resume — so they are the bulkiest thing hanging off a job with the shortest useful life. `finished` and not all three terminal states: `crashed` and `cancelled` are retryable and a retry resumes from exactly these rows, so those wait for the job window. |
| Streams         | 1 day   | `jorb_stream` rows of **`finished`** jobs only; the job row stays                                                 | Same window, same argument, and the same `finished`-only restriction for the same reason: a retry's completed `stream_write` checkpoints fast-forward without appending, so reaping early would leave the resumed job's stream permanently missing its own prefix. A second sweep rather than a second table in the checkpoint one, so each stays a two-buffer answer when there is nothing to do. |
| Jobs            | 30 days | The whole `jorb` row, and its history, events, streams, mailbox, checkpoints and DAG edges by `ON DELETE CASCADE` | The job's own audit lifetime — and, because an `identity_key` lives exactly as long as its row, the horizon on at-most-once.                                                                                                                               |
| Consumed mail   | 30 days | `jorb_mailbox` rows with `consumed_at` set                                                                        | The job-scoped cascade cannot reach these: a long-lived workflow reads mail for months and never terminates, so nothing else would ever free them.                                                                                                         |
| History         | 30 days | `jorb_history` rows past the window                                                                               | The audit trail lives as long as the work it describes. A durable machine that never terminates is never reached by the job cascade, so nothing else bounds its wake/sleep history.                                                                        |
| Orphaned DAGs   | 30 days | `jorb_dag` rows past the window with no jobs left                                                                 | Jobs point **at** a DAG (`ON DELETE SET NULL`), so job retention never touches it. Left alone it does not merely linger, it keeps _answering_: `jorb_dag_status` LEFT JOINs `jorb`, so an emptied DAG reports `total_jobs = 0` forever.                    |
| Schedule log    | 30 days | `jorb_schedule_log` rows, except each schedule's newest                                                           | It cascades only from `jorb_schedule`, which operators disable rather than delete — so it had no upper bound of any kind, at cron rate, for the life of the install.                                                                                       |
| Retired workers | 30 days | `jorb_worker` rows both retired and silent for the window                                                         | One row per worker _process start_, and nothing ever deleted one: a fleet that redeploys accumulates registry rows for as long as it exists.                                                                                                               |

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

A checkpoint of a _non-terminal_ job is never touched at any age — a
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
Everything per-_attempt_ lives elsewhere.

| Table               | Grain                                                       | Lifetime                                                                                  |
| ------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `jorb`              | one row per job, forever                                    | retention window                                                                          |
| `jorb_history`      | one row per transition (~4 per job) — the per-attempt trail | cascade from `jorb`                                                                       |
| `jorb_step`         | one row per DXE checkpoint                                  | its own, much shorter window                                                              |
| `jorb_event`        | one row per `(job, key)` published                          | cascade from `jorb`                                                                       |
| `jorb_stream`       | one row per streamed value, `(job, key, seq)`               | the checkpoint window, or cascade from `jorb`                                             |
| `jorb_mailbox`      | one row per durable message                                 | cascade, **or** the consumed-mail sweep                                                   |
| `jorb_dependencies` | DAG edges, two FKs to `jorb`, both cascading                | cascade from either end                                                                   |
| `jorb_queue`        | one row per _controlled_ queue; absent = defaults           | operator-managed                                                                          |
| `jorb_worker`       | one row per worker _process_                                | retired by the monitor, then the retired-worker sweep; never while it owns in-flight jobs |
| `jorb_schedule`     | one cron definition                                         | operator-managed                                                                          |
| `jorb_schedule_log` | one row per schedule _execution_                            | the schedule-log sweep, which always keeps each schedule's newest                         |
| `jorb_dag`          | one DAG header; `jorb.dag_id` is `ON DELETE SET NULL`       | the orphaned-DAG sweep, once it is past the window **and** has no jobs left               |

`jorb_history` is recorded **by trigger**, not by the writers. The worker,
monitor, scheduler, admin API and websocket server all mutate `jorb`;
putting the audit in a trigger means none of them can forget to, and none
of them has to know it exists. `jorb_dag.completed` is stamped the same way
for the same reason.

`jorb.tags` and `jorb.admin_data` are deliberately different columns.
`tags` is the _caller's_ labels (customer, region, batch) — flat, and the
only JSONB with an index, because filtering on them is a thing applications
do. `admin_data` is the _platform's_ own execution config (`max_retries`,
`timeout_seconds`, `on_timeout`, `save_result`, schedule metadata), which
nobody filters on, so indexing it would tax every enqueue to make no query
faster.

`jorb_worker` is a plain registry, not a foreign key: `jorb.claimed_by`
holds a worker id with no constraint behind it, so retiring a worker can
never cascade into jobs.

### The families of columns on `jorb`

The row is wide, but it is not a grab bag. Every column belongs to one of
six families, and which family a column is in decides how the rest of the
platform treats it:

| Family                | Columns                                                                             | Answers                        |
| --------------------- | ----------------------------------------------------------------------------------- | ------------------------------ |
| **the work**          | `job_class`, `kwargs`, `result`, `admin_data`                                       | what to run, and what came out |
| **routing**           | `queue`, `prio`, `capability`, `app_version`, `run_after`                           | who may claim it, and when     |
| **whose work it is**  | `uid`, `tags`, `partition_key`                                                      | for the application's benefit  |
| **which work it is**  | `deadline_key`, `identity_key`, `debounce_key` (+ `debounce_deadline`)              | is this a duplicate?           |
| **structure/lineage** | `waitfor_job`, `waitfor_group`, `run_group`, `dag_id`, `schedule_id`, `forked_from` | how it relates to other rows   |
| **execution**         | `state`, `run_count`, `error_count`, `run_epoch`, `claimed_by`, the timestamps      | where this attempt is          |

The split between the third family and the fourth is the one that earns
its keep, because it decides what a **fork** carries. A fork is a new row
that re-executes an existing job's work from step N; it inherits everything
in "whose work it is" — a tenant's fork is still that tenant's, still in
that tenant's lane, still tagged for their dashboard — and none of "which
work it is", because two live rows sharing an idempotency key would make
that key mean nothing. `forked_from` records the source with `ON DELETE SET
NULL`, since lineage is best-effort audit and not a dependency: retention
reaps the older source on its own schedule and the fork must outlive it.

### Three dedupe keys, three partial unique indexes

All three stop a duplicate **row** at the `INSERT`, none of them says
anything about a job whose `task()` runs twice after a retry — that is what
DXE checkpoints are for, and the two layers compose. Which one a caller
_wants_ is a decision
[writing-jobs.md](writing-jobs.md#choosing-your-dedupe-primitive) makes;
what is architectural is that the difference between them is entirely the
_shape of the index_, and the shape is the promise:

| Key            | Index predicate                                  | Held until                 | A duplicate enqueue  |
| -------------- | ------------------------------------------------ | -------------------------- | -------------------- |
| `deadline_key` | `state = 'queued'`, and scoped **per queue**     | the claim, then it re-arms | raises               |
| `identity_key` | none — every state, table-wide                   | the row is deleted         | returns the same job |
| `debounce_key` | `state = 'queued' AND run_count = 0`, table-wide | the first claim, for good  | **moves** the job    |

`run_count = 0` on the third is not decoration: `claim_jorb` increments it,
so the key is released at the first claim and can never be taken back — and
without that, a row that was claimed, failed and was **retried** would come
back to `queued` still holding a key a new burst may already have taken,
and the retry `UPDATE` would violate the index inside a worker's failure
handler.

The other half of that rule is in the statements rather than in the indexes:
**every statement that puts a row back into `queued` clears `deadline_key`,
`debounce_key` and `debounce_deadline`** (`db.REQUEUE_CLEARS_KEYS` — retry,
rerun, DLQ retry, a job's own **reschedule** (`Job.reschedule()` and durable
`sleep()`, which park the row back in `queued` for the rest of the nap), and
the monitor's timeout retry and its dead-worker and stuck-claim sweeps; a
waiter's wake clears `deadline_key`, which is the only
one a `waiting` row can hold). A key's collapse duty ends the first time its
row leaves `queued`, so a requeue must not carry it back into an index the
row was already released from. `run_count` alone does not cover the row that
was **cancelled while still parked** — it was never claimed, so `run_count`
is 0 — and it never covered `deadline_key` at all. Both indexes are unique,
and the sweeps are **batch** statements, so one such row aborted the whole
`UPDATE`: every other doomed job in that batch stayed stranded, every cycle.
The consequence to carry is that a requeued job is dedupe-anonymous — it
runs, and a duplicate submitted while it was gone is its own job.

The consequence worth carrying is that **retention is the horizon on "at
most once"**. `identity_key`'s index has no state predicate, so the key
lives exactly as long as the row; when the retention sweep reaps the
terminal job the key is released and the same identity enqueued afterwards
is a new job. This is the honest bound rather than a leak: nothing in the
platform remembers anything longer than it remembers a job, so a caller
needing uniqueness past the horizon scopes the key to a time it can name.

All three indexes are partial, for the reason every partial index here is:
the key is NULL on the overwhelming majority of jobs, and an unconditional
unique index would write an entry for each of them — write amplification on
the hottest table in the system, on the enqueue path, for rows it could
never answer a question about. Partial _unique_ indexes are also what `ON
CONFLICT` infers against, and inference requires the predicate restated in
the statement, which is why exactly one SQL constant in `client.py` does so
per key.

---

## The principle that ties it together

Most of the recent decisions look arbitrary in isolation and stop looking
arbitrary once you have this:

> **The write path does the minimum necessary work inside the transaction.
> Everything optional is either made conditional, or moved out.**

_Made conditional:_

- Notifications fire only against registered demand — the gate is
  evaluated before the payload is built, and for `jorb_done` it is the
  trigger's `WHEN` clause, so the function is not even entered.
- The claim's serialising lock is taken only for queues that actually have
  a control set; everything else keeps the lock-free path — per-lane
  limits re-scope what is counted inside it, and add no new tier.
- `idle` and `awaited` are written only on transition, so the flags cost
  one write per park and per await, not one per poll.
- The GIN index on `tags` is partial, so an untagged enqueue — the hot
  shape — is not in the index at all. Same reasoning for `run_group`,
  `uid`, `dag_id`, `forked_from`, the three dedupe keys and
  `app_version`: a deployment that does not use one of those features has
  an index for it that is physically empty, and an enqueue path that never
  writes to it.
- The per-lane count has an index of its own
  (`jorb_partition_inflight_idx`) whose predicate is `state IN ('claimed',
'running')`, so it is bounded by work in flight rather than by the table,
  and a queued row — the hot write — is not in it at all.
- `updated` is deliberately _not_ indexed: it is rewritten by every state
  change, so an index on it would add a write to each of the ~4 updates per
  job and block HOT updates on the hottest table in the system. Reporting
  windows key off `created`, which is written once.

_Moved out:_

- The dashboard firehose became a polled aggregate — cost O(1) in both
  dashboards and throughput, instead of O(transitions).
- Retention, timeout enforcement and crash recovery are all the monitor's
  work on its own schedule, never a producer's or a worker's.
- Durable sleep leaves the process entirely: the sleep is a future
  `run_after` in the database, not a parked worker.
- Job code runs on the worker's own thread pool, off the event loop that
  is holding the deadline.

The counterweight, so this does not become an excuse to do too little: the
minimum genuinely necessary work stays _inside_ the transaction. The
history trigger, the epoch fence and the claim's control checks are all on
the write path precisely because moving them out would make them
optional — and a correctness property that a caller can forget to enforce
is not a property.
