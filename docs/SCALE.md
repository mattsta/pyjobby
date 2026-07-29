# Running pyjobby at scale

Reference workload throughout: **1,000,000 jobs/hour** — about 278/second
sustained. Every number below was measured on this schema, not estimated. Where
something is a projection from a smaller measurement, it says so.

The short version: write throughput has **~125× headroom** at this target
(34,671 jobs/s measured in production shape). The single biggest factor in
that number is `NOTIFY` demand-gating — see [Why NOTIFY set the
ceiling](#why-notify-set-the-ceiling), which is worth reading before tuning
anything, because the fix is the opposite of the obvious one.

What breaks first is now unambiguously everything that has to read or retain
the _accumulated_ table, not the writes.

Every number here is reproducible with `pj-bench` — see [Reproducing
these numbers](#reproducing-these-numbers). They are not hand-measurements.

---

## What one job costs

| Per job                                             | Count                          | At 278 jobs/s                      |
| --------------------------------------------------- | ------------------------------ | ---------------------------------- |
| `jorb` row writes (insert + claim + run + terminal) | 4                              | ~1,100 writes/s                    |
| `jorb_history` rows (trigger, one per transition)   | 4                              | ~1,100 inserts/s, **96M rows/day** |
| Notifications emitted                               | **0** unobserved, 1–2 observed | ~0/s (was ~1,390/s)                |
| `jorb_step` rows                                    | 1 per `step()` call            | workload-dependent                 |

### Measuring enqueue honestly

How you measure this changes the answer by **6×**, so it is worth being precise
about what production actually does: each job is enqueued in **its own
transaction**, and many clients do so **concurrently**.

```
one bulk transaction, 20k rows                     68,105 rows/s   ← misleading
serial, one transaction per job                     5,979 jobs/s
16 concurrent connections, one transaction each    34,671 jobs/s   ← production shape
```

The bulk number is the one a careless benchmark reports, and it is meaningless:
a single transaction pays the per-commit costs **once**, amortised across 20,000
rows. It barely moved when the real ceiling was lifted, which is the clearest
proof that it was measuring the wrong thing all along.

Against a 278/s requirement, 34,671/s is **~125× headroom**.

That figure was **11,326/s** before the notification work below — so the same
benchmark, on the same schema, reported a third of the truth while a single
`NOTIFY` remained on the commit path.

### Why NOTIFY set the ceiling

Committing a transaction that calls `NOTIFY` requires Postgres to take a
**global exclusive lock**, held until the commit completes and reaches disk.
Postgres does this because notifications must be delivered in commit order, and
commit order is not established until commits finish — so it serialises every
commit containing a `NOTIFY`, defeating group commit. DBOS documented the same
wall and recovered ~20× by moving notifications out of the commit path
([writeup](https://www.dbos.dev/blog/postgres-listen-notify-scalability)).

Measured here, 16 concurrent connections, one transaction per job:

|                                                  | jobs/s     |
| ------------------------------------------------ | ---------- |
| as shipped when this was measured (all channels) | 11,326     |
| `job_state_change` firehose disabled             | 11,668     |
| all `NOTIFY` triggers disabled                   | **28,790** |

**The cost is per-COMMIT, not per-notification.** Disabling the transition
firehose alone — four of the five notifications a job emitted, one per
transition through `queued -> claimed -> running -> finished` — recovered only
**3%**: the lock is per COMMIT, so a transaction that still issues one
`NOTIFY` pays exactly what a transaction that issued several paid, and every
commit on every path still issued one. Reducing notification _volume_ does not
raise the ceiling; only removing `NOTIFY` from the commit path does.

**What was done about it.** Every channel is now emitted only when a consumer
has registered demand for it, so a job nobody is watching notifies nobody — and
the last ungated channel, `job_state_change`, was **deleted** rather than
gated, because its consumer (the websocket dashboard) is push-only and a gate
would have dropped its events rather than delayed them. That consumer now polls
aggregates instead: one index-backed query per interval, shared by every
connected dashboard, independent of job throughput.

Measured on the completion path, 16 concurrent connections, one transaction per
job, median of 5 (`tests/test_notify_gating.py`, which rebuilds the deleted
trigger so the "before" stays measurable):

|                                       | jobs/s     |
| ------------------------------------- | ---------- |
| before: a client waiting, firehose on | 11,917     |
| before: fire and forget, firehose on  | 12,193     |
| after: `job_state_change` deleted     | **35,191** |

Gating the completion channel alone bought 1.02x; deleting the firehose as
well bought **2.9x**. Repeat runs land between 2.6x and 2.9x — the benchmark
is a median of 5 interleaved rounds, not a single ordered pass, precisely so
that drift shows up as noise rather than as a result. Same lesson, now paid for: it is the number of
_notifying commits_ that matters, not the number of notifications.

This is the single most important thing to understand before tuning: it is
invisible to a serial benchmark (3% there) and invisible to a bulk benchmark,
and it only appears under concurrent commits — which is exactly the production
shape.

### What the history trigger costs

Now measured under the production shape, 16 concurrent connections, one
transaction per job, median of 3 (`pj-bench enqueue --allow-trigger-toggle`):

|                                | jobs/s |
| ------------------------------ | ------ |
| as shipped                     | 29,768 |
| history trigger off            | 41,431 |
| all NOTIFY off                 | 36,238 |
| all NOTIFY **and** history off | 42,112 |

So history costs roughly **1.4×** — real, but nowhere near what isolating it in
a bulk benchmark implied. That earlier reading was the same methodology that
overstated enqueue headroom by 6×, and it is the reason no design decision was
allowed to rest on it until now.

**Decision: keep it, unchanged.** At ~100× headroom the throughput cost buys a
complete, reliable audit trail, and the alternatives are all worse trades —
sampling makes the trail untrustworthy exactly when you need it, and writing it
asynchronously loses rows on the crashes it exists to explain. `jorb_history`
is still the largest table in the system, but that is a **storage** problem and
retention already answers it.

Note the spread on these runs is 16–34%. At this rate the measurement is noisy
enough that the history and NOTIFY figures overlap at the edges — treat them as
"about 1.4×" and "about 1.2×", not as three significant figures, and re-measure
on your own hardware rather than trusting these.

---

## What breaks first

### 1. Anything that scans the accumulated table

This is the real scaling wall, and it is entirely about _plans_, not volume. A
query that reads the whole job table stays correct as the table grows and
simply gets slower, which is the failure you discover months in.

Measured on the retention probe — the query the monitor runs every cycle,
forever, whose honest answer once caught up is usually "nothing expired":

|                                             | Buffers | Rows examined       |
| ------------------------------------------- | ------- | ------------------- |
| unindexed, 300k rows                        | 5,741   | 300,000 → returns 0 |
| unindexed, 20k rows, `ORDER BY id`          | 465     | 20,000 → returns 0  |
| indexed + ordered by the indexed expression | **2**   | 0                   |

Two things were required, because the index alone was not enough:

- `jorb_retention_idx` on `COALESCE(finished, updated)`, partial over terminal
  states.
- **Ordering by that same expression.** `ORDER BY id` makes the planner prefer
  a primary-key scan to avoid a sort, then filter every row anyway. Ordering by
  the indexed expression is a 2-buffer index scan — and "oldest first" is what
  retention means regardless.

`tests/test_scale_plans.py` asserts the _plan_ for these paths, not a duration.
Timings flake on a loaded CI box and pass on a fast one with the index dropped;
a plan is a fact.

The same file checks that **every foreign key to `jorb` has a leading index**.
Postgres does not create one automatically, and a cascade delete without one is
a sequential scan of the child table _per deleted row_ — precisely what
retention does in bulk. That check found `jorb_dependencies.depends_on`, which
had no index because the primary key leads with the other column.

### 2. Retention falling behind ingest

A sweep that takes one batch per cycle deletes `batch_size / check_interval`
jobs per second. At the shipped defaults that is 360k/hour against 1M/hour of
arrivals: **retention is enabled, and the table still grows forever.**

Retention therefore drains — it keeps taking batches until it is caught up or
its per-cycle time budget expires — and it reports which of those two happened,
because a retention sweep that silently cannot keep up is worse than none: the
dashboard says retention is on.

The time budget exists so one retention cycle can never starve the
latency-critical sweeps (timeouts, dead workers). Retention is not urgent;
recovering a job from a dead worker is.

### 3. NOTIFY queue saturation — the cliff

This is the highest-severity failure mode in the system, because it is a cliff
rather than a gradient.

`pg_notification_queue_usage()` reports how full Postgres's shared async
notification queue is. It is server-wide and bounded. **At 1.0, every
transaction that issues a NOTIFY fails** — which, in pyjobby, means no job can
be enqueued or completed anywhere.

The queue drains only as fast as the **slowest connected listener**. So a single
wedged dashboard that stops reading fills it, and an observability client takes
down job processing. Everything is fine until it is a total outage.

This is far less pressing than it was: at ~1,390 notifications/second there was
no margin for a slow consumer at all, and an unobserved job now emits none. But
the cliff is a property of Postgres, not of pyjobby, and one wedged listener on
a busy install can still reach it — so it stays monitored (`notify_queue_usage`
metric, and a `doctor` check that WARNs well before the edge) rather than
assumed away.

There is deliberately NO per-transition notification channel — an
unfiltered firehose (no queue filter, broadcast to every listener) would be
~830 messages/second no consumer could use, and a dashboard wants
aggregates, so the dashboard polls instead (see [Why NOTIFY sets the
ceiling](#why-notify-set-the-ceiling)).

What remains is gated on demand, which means the notification rate now scales
with how many consumers are actually parked rather than with job throughput:
`jorb_enqueued` fires only when a worker is idle on that queue, `jorb_done`,
`jorb_event` and `jorb_stream` only when a client is waiting, `jorb_cancel`
only for a running job. Under load — the regime that fills the queue — almost nobody is parked, so
almost nothing is sent. There is no longer a channel to turn off for relief;
the levers are the consumer side (find the listener that stopped draining) and
the `notify_queue_usage` metric that warns before the cliff.

### 4. Vacuum pressure

Four row versions per job means **~4M dead tuples/hour** on the hottest table.
Default autovacuum thresholds are proportional to table size, so on a large
`jorb` they trigger too late and the claim index bloats.

**The schema already tunes this per table** — it is part of the install, not a
runbook step you have to remember:

```sql
ALTER TABLE jorb SET (autovacuum_vacuum_scale_factor  = 0.02,
                      autovacuum_vacuum_threshold     = 1000,
                      autovacuum_analyze_scale_factor = 0.02,
                      autovacuum_vacuum_cost_limit    = 2000,
                      fillfactor                      = 85);
```

`fillfactor` leaves room on each page for an updated row version to live beside
its original, which is what allows a **HOT update** — one that does not have to
touch every index. That is also why `jorb.updated` is deliberately _not_
indexed: it is rewritten by every state transition, so an index on it would add
a write to each of the ~4 updates per job and defeat HOT, paying permanent
write-path bloat for a read that happens once per scrape. Reporting windows use
`created`, which is written once.

The dead-tuple ratio for `jorb` is reported in metrics, because "is autovacuum
keeping up" is a survival question at this rate and nothing else answers it.

---

## What holds up

- **Claiming.** `claim_jorb()` uses `FOR UPDATE SKIP LOCKED` against a partial
  index over claimable rows only, so claim cost is independent of how many
  finished jobs are in the table. Workers never block each other.
- **Enqueue.** 34,671 jobs/s in the production shape — 16 concurrent
  connections, one transaction per job — against a 278/s requirement. (The
  68k figure is the single-bulk-transaction number, which
  [measures the wrong thing](#measuring-enqueue-honestly).)
- **Fencing.** `run_epoch` comparisons are per-row and add nothing measurable.
- **Cascade deletes** — now that every foreign key has a leading index.
- **Resolving the job class.** 0.49 µs/job from the class cache, and the
  `--reload` dev flag adds 5 µs — measured, not assumed; see
  [Caching the resolved job class](#caching-the-resolved-job-class-and-what-the-reload-flag-costs).

### The one caveat on capped queues

`max_concurrency` and `rate_limit` are exact rather than approximate, which
requires serialising claims for that queue through an advisory lock. Queues with
no limits never take the lock at all and are unaffected by any of this.

A capped queue runs at `1 / (critical section)`, and **no lock strategy changes
that** — it is set by the serialised section itself. Do not put a
million-jobs-per-hour queue under a cap and expect uncapped throughput.

Measured, on the shape where the lock is the only thing binding — a cap too
high to refuse, short jobs, claimers to spare — that ceiling is **3,211
claims/s, 11.6× the reference workload**, so it is a caveat and not a wall.
Raising it further would take claiming a _batch_ per lock acquisition; that was
measured against this number and [rejected](#claiming-a-batch-per-lock-acquisition-rejected-on-the-measurement),
along with what would change the answer.

A cap that is _low_ is a different thing entirely and no claim strategy touches
it: `max_concurrency` bounds in-flight work, so the queue permits
`cap / job duration` and that is the whole story.

What the lock choice _does_ decide is what happens to a claimer that loses it.
The lock waits up to 50ms rather than failing instantly, and the reason is
worth knowing because it is not the obvious one:

A worker does not retry in a tight loop. It reads an empty claim as _"the queue
is empty"_ — so it publishes idle demand, which re-arms that queue's enqueue
notifications for **every producer** (see [Why NOTIFY set the
ceiling](#why-notify-set-the-ceiling)), and then parks for `checkInterval`, 5
seconds by default, waiting for a wakeup nobody is going to send. Measured with
4 real workers against a cap that could never bind: failing instantly left **1
of 4 workers ever claiming anything**; waiting briefly left **4 of 4**.

So losing the lock never cost a round trip. It cost a worker — and quietly
undid the notification gating at the same time. Wasted claim round trips went
from 87% to 2.3%.

The wait stays bounded so a claim held open by a stuck transaction can still
never freeze the queue, which is what the non-blocking version was protecting.

### Partitioned claims: what fairness costs

`partition_limits` re-scopes a queue's limits to each `jorb.partition_key`
([OPERATIONS.md](OPERATIONS.md#partition_limits-the-same-limits-per-tenant)).
It changes what the claim counts, not who serialises: the same queues take
the same lock, and a queue with no limit still never takes it.

Two things were measured, both gated in `pj-bench plans` against a seed of
20,000 jobs spread over 8 lanes:

- **The per-lane count** (`partition_lane_count`) — the `GROUP BY
  partition_key` that runs inside the advisory lock. **2 buffers, 0 rows
  discarded**: an index-only scan of `jorb_partition_inflight_idx (queue,
  partition_key) WHERE state IN ('claimed','running')`, so it reads one
  queue's in-flight rows and nothing else. That index is the cheapest one in
  the schema to keep — enqueue never touches it, because a `queued` row is
  not in it.
- **The claim probe** (`partitioned_claim`) — **5 buffers, 0 rows
  discarded** when nothing is saturated, which is the same first-index-entry
  stop the unpartitioned probe makes. The caught-up case is free.

The cost appears only where the feature is doing work, and it is the walk:
with a lane at its cap the probe reads past that lane's queued rows to reach
one it may take. Measured in the worst arrangement — a saturated tenant with
**500 queued jobs sorting ahead of everybody**, tens of thousands of other
queued rows behind them — that is **17 buffers and exactly 500 rows
discarded** (`partitioned_claim_blocked`). The bound is the **held-back
tenant's own backlog**, not the table's, and the gate fails if it ever
becomes the latter.

So the shape to avoid is one lane parked at its cap with an enormous backlog
in front of the queue's ordering: every claim on that queue re-walks it. If
that is your workload, give the hog its own queue, or raise its lane's share
so the backlog drains.

---

## Sizing

Per million jobs, with an average of 3 steps each:

| Table          | Rows | Notes                                              |
| -------------- | ---- | -------------------------------------------------- |
| `jorb`         | 1M   | plus 3 dead versions each until vacuumed           |
| `jorb_history` | 4M   | the largest table; one row per transition          |
| `jorb_step`    | 3M   | prunable on a much shorter window than the job row |

`jorb_step` exists to make a job **resumable**. Once the job reaches a terminal
state, resume is impossible and every checkpoint it holds is dead weight kept
only for audit — which is why checkpoints get their own, much shorter retention
window than the job row. See [DXE.md](DXE.md#retention-checkpoints-outlive-the-run-but-not-the-job).

### The tables that do not scale with jobs

These grow on their own clocks, which is exactly why they were missed: none of
them is reachable from a job's `ON DELETE CASCADE`, so job retention could run
perfectly and leave all three growing forever.

| Table               | Grows with                                 | Bounded by                                         |
| ------------------- | ------------------------------------------ | -------------------------------------------------- |
| `jorb_dag`          | DAG executions                             | the DAG sweep, once its jobs are gone              |
| `jorb_schedule_log` | schedule _fires_ (cron rate, not job rate) | the schedule-log sweep, minus one row per schedule |
| `jorb_worker`       | worker process **starts** — i.e. deploys   | the retired-worker sweep                           |

`jorb_worker` is the one that surprises people: it is a _deployment_ clock,
completely unrelated to throughput. A 100-worker fleet redeployed daily writes
36,500 rows a year at zero jobs/second.

`jorb_dag` is the one where size was never the point. Its rows are tiny; the
cost of keeping them was that `jorb_dag_status` LEFT JOINs `jorb`, so a DAG
whose jobs had aged out reported `total_jobs = 0` **permanently**. That is a
wrong answer served to an operator, and unlike a slow query it does not
announce itself.

### Durable machines have a different cost model

A state machine (`start_machine`) is a job that parks on `recv()` waiting for
the next event, so its cost scales with **how many machines are alive at
once**, not with job throughput:

- **A machine holds a worker only while it is awaiting.** `recv()` parks a
  worker for at most `wait_seconds`; when that times out with no event the
  machine checkpoints a wake time, requeues itself and **unwinds** — the
  worker is released and the machine waits `idle_seconds` in the database
  holding no worker, no connection and no thread (`StateMachineJob.task()` in
  `pyjobby/statemachine.py`). So the sizing input is not the number of live
  machines but the number **concurrently in a `recv()` window**: a machine
  occupies a worker about `wait / (wait + idle)` of the time, which at the
  class defaults (30s / 300s, see
  [STATECHARTS.md § Running machines](STATECHARTS.md#running-machines)) is
  ~9% — roughly one worker per eleven idle machines. Machines still default
  to their own `machines` queue so a burst of simultaneous wakes cannot
  starve the workers serving latency-sensitive job queues. Size that queue's
  `--workers` to concurrent _awaiting_ machines; raising `idle_seconds`
  lowers occupancy and adds up to that much delay to an event arriving just
  after a park ends.
- **A long-lived machine accumulates `jorb_history` and consumed mail.** The
  job-scoped `ON DELETE CASCADE` never fires for a machine that never
  terminates, so its wake/sleep history and read messages are bounded only by
  the history and mailbox sweeps (`--retention-days`), not by job completion.
  Those two sweeps, not job retention, are what keep a fleet of durable
  machines from growing without limit.

The per-machine `recv` and event-wait rates depend on how chatty your
machines are; measure yours with `pj-bench` rather than assuming, the same
way the job-cost numbers above were measured.

---

## Checklist before running at this rate

1. Set `--retention-days` deliberately. It is on by default; the default is
   unlikely to match your storage budget.
2. Autovacuum is already tuned per table in the schema — verify it survived if
   you have customised `jorb`.
3. Alert on `notify_queue_usage`, backlog age (**not** depth), and
   completions/sec versus arrivals/sec. Those three catch almost everything.
4. Watch that retention reports "caught up" rather than "out of budget".
5. Run `pj-bench plans` in CI. It is the only item here that catches a problem
   _before_ it reaches production.

Nothing on this list asks you to disable a notification channel. That used to
be step 3, and it was wrong: the channels that remain are gated on demand, so
they cost nothing when nobody is waiting, and the one that could not be gated
is gone. If you find yourself reaching for `ALTER TABLE ... DISABLE TRIGGER`,
measure with `pj-bench notify` first — the answer for an unobserved job should
be zero.

---

## Reproducing these numbers

Every measurement above comes from `pj-bench`, which ships with the platform so
the numbers can be re-taken on your hardware and re-checked after a change:

```
pj-bench enqueue    # write throughput; bulk vs serial vs concurrent, and the
                    # per-NOTIFY-channel breakdown that shows the commit lock
pj-bench claim      # claim throughput, advisory-lock contention, and what a
                    # capped queue sustains with short jobs at a high and a
                    # low cap (--hold-ms / --high-cap / --low-cap)
pj-bench e2e        # completed jobs/sec and enqueue->finished p50/p95/p99
pj-bench notify     # notifications per lifecycle, per channel, + queue usage
pj-bench plans      # EXPLAINs every hot query; exits non-zero on a seq scan
pj-bench resolve    # per-job class resolution, four interleaved arms: cached,
                    # the --reload mtime check, no cache at all, and a real
                    # re-import (--resolutions / --reloads)
pj-bench all --json # everything, machine-readable, for diffing runs
```

`pj-bench plans` is the one to wire into CI: it is the gate that catches a lost
index before it reaches production, and it needs no baseline to compare against
— a sequential scan of the job table is wrong regardless of hardware.

The rest are hardware-dependent, so compare runs against **your own** previous
`--json` output rather than against the figures here. Ratios travel between
machines; absolute numbers do not.

Two measurement traps these tools exist to avoid, both of which produced wrong
answers here before the harness existed:

- **Bulk inserts amortise per-commit costs.** Measure one transaction per job.
- **The NOTIFY commit lock only appears under concurrency.** A serial benchmark
  reports 3% for something that costs 62% in production.

---

## Design decisions on the write path — and on retention

These are recorded because each one is a place where the obvious improvement
makes the platform slower (or, for the retention entries, quieter about being
wrong), and someone will propose it again.

### Cumulative per-queue counters: rejected

Prometheus counters want `pyjobby_jobs_finished_total{queue}` — monotonic,
per-queue, cumulative. The only O(1) source is an incrementally-maintained
rollup row updated by the same trigger that writes history.

That is exactly the wrong trade. At the reference workload it funnels ~1,100
updates/second onto a handful of rows: every transition in a queue serialises
on one tuple, on the write path, forever — to make a read cheaper that happens
once per scrape. A sharded counter (`PRIMARY KEY (queue, event, shard)`) fixes
the contention and adds a write per transition anyway.

The endpoint instead exposes **windowed gauges** for per-queue traffic and one
true counter, `pyjobby_jobs_enqueued_total`, sourced from the id sequence —
O(1), catalog-only, and structurally immune to retention, because deleting rows
does not un-issue their ids.

Revisit only if someone genuinely needs `rate()` over per-queue crash events
specifically. The write path is the scarcest resource in the system; a
monitoring convenience does not get to spend it.

### A GIN index on tags: accepted, because it is partial

`jorb.tags` carries a GIN index, which is the most expensive index type to
maintain — so it was measured before it was kept, with the arms interleaved
because the box was under load 45 and straight before/after runs were swinging
2.5k–20k jobs/s for reasons unrelated to the change:

| untagged enqueue        | jobs/s          |
| ----------------------- | --------------- |
| without `jorb_tags_idx` | 28,700          |
| with `jorb_tags_idx`    | 28,854 (1.005×) |

Identical, and that is the whole argument. The index is partial
(`WHERE tags <> '{}'`), so an enqueue that sets no tags never matches the
predicate and never touches it. Tagged enqueue costs 0.93–1.04× — noise —
including GIN's worst case of a distinct value per job, because `fastupdate`
parks entries in the pending list and the merge becomes autovacuum's cost
rather than the enqueuing transaction's.

Contrast this with the rejected rollup below. Both are "an index/table to make
a read cheaper". The difference is that this one charges nothing to jobs that
do not use it, while the rollup charged every transition in a queue whether
anyone read the counter or not. **That is the test to apply to the next
proposal: not "is it cheap", but "who pays when it is unused".**

### A column and a doubly-partial index for the scheduler: accepted

The scheduler's `max_concurrent_jobs` check filtered `admin_data->>'schedule_id'`
with no index serving it — a sequential scan of `jorb` on every firing, growing
with the table rather than with the schedule's own load.

It became a real `jorb.schedule_id` column rather than an expression index,
measured the same way tags was — nine interleaved A/B pairs on two identically
installed databases:

| enqueue                      | jobs/s (median of 9) |
| ---------------------------- | -------------------- |
| without the column and index | 27,941               |
| with them                    | 28,010               |

1.002×, against a within-run spread of 10–33%. No difference, which is the
argument.

**The predicate is doubly partial, and the second clause is the load-bearing
one:**

```sql
WHERE schedule_id IS NOT NULL
  AND state IN ('queued', 'claimed', 'running', 'waiting')
```

`IS NOT NULL` alone would have been used, would have reported no sequential
scan, and would have handed the check **every job the schedule had ever
created** — about 525,000 a year for a minutely schedule — to count and
discard. An index scan that discards every row costs what a scan costs. The
live-state clause bounds the index by in-flight work, which
`max_concurrent_jobs` itself bounds.

Gated as `schedule_concurrency` in `pj-bench plans` with a discard budget of
**zero**, because that is the property worth defending rather than the access
method.

### Recounting for counters: rejected, and it is a correctness bug

A counter derived by recounting rows (`COUNT(*) FROM jorb_history WHERE
event='finished'`) **decreases when retention prunes**. Prometheus reads a
falling counter as a process restart and attributes the entire window's traffic
to a reset — silently. The old `_total` metrics did exactly this.

Counters were therefore **renamed** rather than re-typed in place, so a
dashboard using `rate(pyjobby_jobs_crashed_total[5m])` breaks loudly with a
missing series instead of quietly reporting garbage. A metric that lies is
worse than one that is absent.

### An index on `jorb.claimed_by`: rejected, on who pays

Retention cannot delete a worker registry row whose jobs are still `claimed`
or `running` — `claimed_by` has no foreign key, so removing the row would
strand that work where neither recovery sweep can find it (the dead-worker
sweep JOINs `jorb_worker`; the stuck-claims sweep covers only `claimed`
rows). The obvious way to ask "does this worker still hold
anything?" is an index on `jorb.claimed_by`.

Same test as the rollup and the GIN index: **who pays when it is unused.**
`claimed_by` is written on the claim path, on the hottest table in the
system, for every job — and the only question anyone asks of it is one
retention asks a few times a day. A plain index also stores an entry for
every `queued` job's NULL, and a partial one moves the row in and out of the
index as its state changes, defeating HOT on exactly the update it is added
to.

The refusal instead rides `jorb_inflight_idx`, which already exists for the
reaper and whose partial predicate is exactly `state IN ('claimed',
'running')`. In-flight work is bounded by the fleet, never by the table:
measured at 20,000 jobs with 25 in flight, the anti-join's inner side reads
**25 rows and 3 buffers** — and `tests/test_scale_plans.py` asserts that it
reads the in-flight set rather than merely "used an index".

### Fixing the empty-DAG report in the view: rejected, in favour of deleting the row

`jorb_dag_status` LEFT JOINs `jorb`, so a DAG whose jobs retention removed
reports `total_jobs = 0` forever. The cheap fix is an inner join — hide DAGs
with no jobs — and it is wrong, because **a view cannot tell "never had jobs"
from "had jobs, they aged out"**. A DAG with no jobs _yet_ is a real state:
`DAGBuilder` writes that row before the jobs it will own, so an inner join
would hide a DAG from `dag list` exactly while it is being built.

Recording a job count on `jorb_dag` was rejected on the rollup argument
above: it is a counter maintained by the write path so a read can be cheap.

So the row is **deleted** rather than reinterpreted, and the sweep runs
immediately after job retention in the same cycle. Because a DAG row is
created before its own jobs, `created` is always earlier than any of their
terminal timestamps — so a DAG becomes eligible on the very cycle that
removes its last job, and the empty-DAG window is one `--check-interval`
wide instead of unbounded.

### A retention knob per table: rejected

Five job-scoped tables outlive the job cascade (`jorb_mailbox` and
`jorb_history` for live jobs, `jorb_dag`, `jorb_schedule_log`,
`jorb_worker`) and they all share `--retention-days`. Per-table windows were
considered and rejected: none of them has a lifetime of its own to argue for
— they all mean "as long as the work they describe" — and five more knobs is
five more ways for an install to be quietly wrong in a direction nobody
checks. `--checkpoint-retention-days` stays separate because checkpoints
genuinely do have their own lifetime: bulkiest rows in the system, useless
the instant their job goes terminal. `jorb_stream` joins that window rather
than earning a third knob — a stream is read while the job runs and every
reader stops at the terminal state, which is the same lifetime written out.

### `DELETE ... USING (a CTE of victims)`: rejected, measured, three times

Every sweep here **probes for victims by index, then deletes them by primary
key**, in two statements. The one-statement form reads better and plans
worse: its second stage is costed against the target table's whole-table
statistics and hash-joins a **sequential scan** of it against the batch it
was just handed. Measured at a 20,000-row seed: 1,006 buffers on the
checkpoint sweep to delete nothing, and 3,300 buffers on the DAG sweep on top
of the probe to delete 1,000 rows it already had the keys for. A batch is
bounded; a scan per batch grows with the table forever.

Two statements also mean the delete is **not executed at all** when the probe
comes back empty — which is the steady state of every retention sweep in the
system.

### Claiming a batch per lock acquisition: rejected, on the measurement

`claim_jorb()` admits **one** job per advisory-lock acquisition, so a capped
queue's claims serialise and its ceiling is `1 / (critical section)`. Claiming
N jobs under one acquisition would divide the serialised part by N. The
proposal is sound; it is the requirement that is missing.

Measured with `pj-bench claim --workers 8 --jobs 2000 --repeat 7`, all arms
**interleaved** (median of 7, PostgreSQL 18.3, 10-core box under load ~4):

| arm                     | claimers | cap   | job  | claims/s  | vs 278/s  |
| ----------------------- | -------- | ----- | ---- | --------- | --------- |
| uncapped, no completion | 8        | none  | —    | 19,037    | 68×       |
| capped, no completion   | 8        | 3,000 | —    | 2,953     | 10.6×     |
| uncapped, short jobs    | 32       | none  | 5 ms | 5,042     | 18×       |
| **capped, short jobs**  | 32       | 1,000 | 5 ms | **3,211** | **11.6×** |
| capped, short jobs      | 2        | 2     | 5 ms | 236       | 0.85×     |

The fourth row is the whole question: a cap too high to ever refuse, jobs short
enough that admission is all the work there is, and enough claimers that the
serialised section is the only thing left binding. It sustains **11.6× the
reference workload**. One capped queue would have to run **11.5M jobs/hour on
its own** before the lock is what stops it, against a platform target of
1M/hour across all queues. Exactness costs 0.64–0.68× against the same shape
with no cap (row 4 vs row 3) — that is the price of the lock, paid, and it is
already priced in above.

**Read these to one significant figure.** Repeat runs of the capped short-job
arm land at 2,969–3,260 claims/s (10.7–11.7×), and its _within-run_ spread was
16% on one run and 89% on another on the same unchanged schema — it is the
noisiest arm here, because it is the one whose rate is set by a serialised
section competing with everything else on the box. That is survivable for this
decision and would not be for a 1.2× one: nothing at 10× turns into a problem
at 20% noise. The low-cap and uncapped-churn arms are quiet by comparison
(6–14%) because their rate is set by the cap and the round trips rather than by
contention.

**A low cap does not need batching; it forbids it.** `max_concurrency` bounds
in-flight work, so the rate it permits is `cap / duration` and nothing else —
row 5 is cap 2 on 5 ms jobs, which permits 400/s in theory and measures
210–236/s because the claim and completion round trips land _on top of_ the
job, not inside it. A batch cannot admit more than the cap has slots for. When a capped
queue is too slow, the cap is the thing that is too small; every other answer
is arithmetic denial.

**Who pays when it is unused.** Uncapped queues — the common case, which never
takes the lock at all — would still get the batch worker model, and everything
the one-at-a-time model gets for free becomes something that has to be
maintained: a worker holding N claimed jobs strands N when it dies rather than
one, `run_epoch` fencing, cancellation and per-job timeouts have to stay
per-job while admission became per-batch, and "how big a batch, and does one
greedy worker starve the others" turns into a policy with a tuning knob and a
fairness bug waiting in it. That is a permanent tax on the entire worker model
to speed up the one shape that already has 11.6× headroom. Same test as the
rollup above: not "is it cheap", but _who pays when it is unused_.

**What would change this answer**, in the order it is likely to arrive:

1. A single capped queue that genuinely needs more than ~3,000 claims/s
   sustained — 11M jobs/hour through one cap. Two capped queues at half the
   rate are not this: the lock is per queue.
2. **The cap count losing its index.** The `count(*)` over
   `state IN ('claimed','running')` runs _inside_ the lock, so it is subtracted
   from the queue's whole throughput rather than from one timer. It is gated as
   `concurrency_cap` by `pj-bench plans`, which measures it at **33–34 buffers**
   through `jorb_inflight_idx` with 200 jobs in flight in a 200k-row table.
   That plan holds while in-flight is a small fraction of the table, which is
   what a cap _makes_ it. Drive in-flight up to the whole table and the planner
   correctly switches to a scan: 20,000 jobs claimed and none completing
   measured **581 claims/s** — still 2.1× the requirement, but that is the
   number that falls with _table_ size instead of with the queue's own load.
   A capped queue holding tens of thousands of jobs in flight means jobs are
   not finishing; fix the count, or the workers, before touching the claim.

Until one of those is a measurement rather than a worry, the ceiling is not
where this platform runs out.

### Caching the resolved job class, and what the reload flag costs

A worker turns `jorb.job_class` — a dotted path carried on every job row — into
a class object once per job, in `JobSystem.resolve_job_class`. That result is
cached. With the `--reload` dev flag the resolver first stats the module's
source file and re-imports only when the mtime has moved.

The cache was added for **correctness**: an unconditional `importlib.reload` was
re-executing job modules between jobs, re-evaluating decorators and breaking
Hypothesis tests. Its effect on throughput was never measured, and "turn
`--reload` off in production" has been folklore ever since. Measured with
`pj-bench resolve --repeat 7 --resolutions 50000 --reloads 2000`, four arms
**interleaved** (median of 7, Python 3.14, PostgreSQL 18.3, 10-core box under
load ~3):

| arm                                                  | µs/job   | vs cached | ceiling, if resolution were the only work |
| ---------------------------------------------------- | -------- | --------- | ----------------------------------------- |
| cached — `--reload` off                              | **0.49** | —         | 2,020,000 jobs/s                          |
| `--reload` on, module unchanged: the mtime **check** | **5.5**  | 11×       | 182,000 jobs/s                            |
| no class cache: `pydoc.locate` per job               | **7.0**  | 14×       | 144,000 jobs/s                            |
| `--reload` on, module edited: the **reload**         | **104**  | 211×      | 9,600 jobs/s                              |

Rows two and four are different questions and the gap between them is the flag's
whole design. **The check is what a production worker with `--reload` left on
pays on every job. The reload is what a developer pays on the first job after
each edit** — and, before the cache existed, what every job paid.

**`--reload` is safe to leave on, on throughput grounds.** Five microseconds
against a measured `claim->finished` p50 of ~1 ms is 0.5% of a job, under a
benchmark whose own round-to-round spread is 4–15%. As a ceiling it permits
182,000 jobs/s in one worker process — about 10× the platform's entire uncapped
claim ceiling of 19,037 claims/s, so the flag cannot become the binding
constraint before claiming does.

It stays off by default anyway, and the reason is not throughput: a flag that
re-executes module-level code whenever a file's mtime moves will do that under
running jobs, on a deploy that rsyncs into a live tree. That argument stands on
its own. The throughput argument never existed.

**What the cache is worth is mostly correctness too — but not entirely.**
Dropping it costs 6.5 µs/job (14×), which is still a 144,000 jobs/s ceiling and
would not be visible end to end. Making resolution _unconditional_ in the old
sense — `importlib.reload` on every job — costs 104 µs and implies **9,600
jobs/s, below the 19,037 claims/s the claim path sustains**. That is the shape
that turns a per-job overhead into the system's bottleneck, and it is the number
the correctness fix also bought.

**Why this is not measured through `pj-bench e2e`,** which is the window the
cost actually lands in: e2e is the honest window and a dishonest instrument. Its
spread is percent-scale on a p50 of milliseconds and the effect is microseconds,
three orders of magnitude under its own noise floor. An e2e comparison could
only ever return "no difference" — and there, "no difference" is
indistinguishable from "the flag never reached the workers" and from "the
harness is broken". A measurement with no failure mode is not a measurement.
Two runs at 200 jobs on 4 workers landed at 1,454 jobs/s with the flag off and
1,497 with it on; the flag-on run was _faster_, which is the noise floor
demonstrating itself. `pj-bench e2e --reload` exists for exactly that smell test
and is documented as not being an arm.

**What would change these answers**, in the order it is likely to arrive:

1. **A different operating system.** The check is two `stat()` calls, so its
   cost belongs to the kernel and the filesystem rather than to this platform —
   5 µs is APFS on macOS. A Linux box with a warm dentry cache stats faster, and
   unusually for this document the _ratio_ will not travel either, because its
   numerator is a syscall and its denominator is a dict lookup. Re-take it with
   `pj-bench resolve`; do not quote this row.
2. **A jobs module that does real work at import time.** `importlib.reload`
   re-executes only the target module — everything it imports is already in
   `sys.modules` and costs a dict lookup — so 104 µs is a **floor**, measured on
   a four-class file. A module that builds a client or reads config at import
   time pays that on every edit, and would have paid it on every job.
   `pj-bench resolve`'s own reload is warm, too: page cache hot, bytecode
   already compiled. A developer's first edit after a cold boot costs more, and
   that number belongs to their filesystem.
3. **Jobs short enough for 5 µs to matter.** At the 278 jobs/s reference
   workload the per-job budget is 3.6 ms and the flag is 0.14% of it. A claim
   round trip alone is longer than 5 µs, so a job this cheap does not exist
   here.
