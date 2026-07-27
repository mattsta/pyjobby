# Running pyjobby at scale

Reference workload throughout: **1,000,000 jobs/hour** — about 278/second
sustained. Every number below was measured on this schema, not estimated. Where
something is a projection from a smaller measurement, it says so.

The short version: write throughput has **~125× headroom** at this target
(34,671 jobs/s measured in production shape). It used to be ~43×, and the
difference was `NOTIFY` — see [Why NOTIFY set the
ceiling](#why-notify-set-the-ceiling), which is worth reading before tuning
anything, because the fix is the opposite of the obvious one.

What breaks first is now unambiguously everything that has to read or retain
the *accumulated* table, not the writes.

Every number here is reproducible with `pj-bench` — see [Reproducing
these numbers](#reproducing-these-numbers). They are not hand-measurements.

---

## What one job costs

| Per job | Count | At 278 jobs/s |
|---|---|---|
| `jorb` row writes (insert + claim + run + terminal) | 4 | ~1,100 writes/s |
| `jorb_history` rows (trigger, one per transition) | 4 | ~1,100 inserts/s, **96M rows/day** |
| Notifications emitted | **0** unobserved, 1–2 observed | ~0/s (was ~1,390/s) |
| `jorb_step` rows | 1 per `step()` call | workload-dependent |

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

| | jobs/s |
|---|---|
| as shipped when this was measured (all channels) | 11,326 |
| `job_state_change` firehose disabled | 11,668 |
| all `NOTIFY` triggers disabled | **28,790** |

**The cost is per-COMMIT, not per-notification.** Disabling the transition
firehose alone — 3 of the 5 notifications per job — recovered only **3%**,
because one `NOTIFY` in a transaction takes the same global lock as three.
Reducing notification *volume* does not raise the ceiling; only removing
`NOTIFY` from the commit path does.

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

| | jobs/s |
|---|---|
| before: a client waiting, firehose on | 11,917 |
| before: fire and forget, firehose on | 12,193 |
| after: `job_state_change` deleted | **35,191** |

Gating the completion channel alone bought 1.02x; deleting the firehose as
well bought **2.9x**. Repeat runs land between 2.6x and 2.9x — the benchmark
is a median of 5 interleaved rounds, not a single ordered pass, precisely so
that drift shows up as noise rather than as a result. Same lesson, now paid for: it is the number of
*notifying commits* that matters, not the number of notifications.

This is the single most important thing to understand before tuning: it is
invisible to a serial benchmark (3% there) and invisible to a bulk benchmark,
and it only appears under concurrent commits — which is exactly the production
shape.

### What the history trigger costs

Now measured under the production shape, 16 concurrent connections, one
transaction per job, median of 3 (`pj-bench enqueue --allow-trigger-toggle`):

| | jobs/s |
|---|---|
| as shipped | 29,768 |
| history trigger off | 41,431 |
| all NOTIFY off | 36,238 |
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

This is the real scaling wall, and it is entirely about *plans*, not volume. A
query that reads the whole job table stays correct as the table grows and
simply gets slower, which is the failure you discover months in.

Measured on the retention probe — the query the monitor runs every cycle,
forever, whose honest answer once caught up is usually "nothing expired":

| | Buffers | Rows examined |
|---|---|---|
| unindexed, 300k rows | 5,741 | 300,000 → returns 0 |
| unindexed, 20k rows, `ORDER BY id` | 465 | 20,000 → returns 0 |
| indexed + ordered by the indexed expression | **2** | 0 |

Two things were required, because the index alone was not enough:

* `jorb_retention_idx` on `COALESCE(finished, updated)`, partial over terminal
  states.
* **Ordering by that same expression.** `ORDER BY id` makes the planner prefer
  a primary-key scan to avoid a sort, then filter every row anyway. Ordering by
  the indexed expression is a 2-buffer index scan — and "oldest first" is what
  retention means regardless.

`tests/test_scale_plans.py` asserts the *plan* for these paths, not a duration.
Timings flake on a loaded CI box and pass on a fast one with the index dropped;
a plan is a fact.

The same file checks that **every foreign key to `jorb` has a leading index**.
Postgres does not create one automatically, and a cascade delete without one is
a sequential scan of the child table *per deleted row* — precisely what
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

The three notifications per job that used to be `job_state_change` — an
unfiltered per-transition firehose, no queue filter, broadcast to every
listener — are gone. No consumer could use ~830 messages/second of individual
state transitions, and a dashboard wants aggregates, so the channel was deleted
and the dashboard now polls (see [Why NOTIFY sets the
ceiling](#why-notify-set-the-ceiling)).

What remains is gated on demand, which means the notification rate now scales
with how many consumers are actually parked rather than with job throughput:
`jorb_enqueued` fires only when a worker is idle on that queue, `jorb_done` and
`jorb_event` only when a client is waiting, `jorb_cancel` only for a running
job. Under load — the regime that fills the queue — almost nobody is parked, so
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
touch every index. That is also why `jorb.updated` is deliberately *not*
indexed: it is rewritten by every state transition, so an index on it would add
a write to each of the ~4 updates per job and defeat HOT, paying permanent
write-path bloat for a read that happens once per scrape. Reporting windows use
`created`, which is written once.

The dead-tuple ratio for `jorb` is reported in metrics, because "is autovacuum
keeping up" is a survival question at this rate and nothing else answers it.

---

## What holds up

* **Claiming.** `claim_jorb()` uses `FOR UPDATE SKIP LOCKED` against a partial
  index over claimable rows only, so claim cost is independent of how many
  finished jobs are in the table. Workers never block each other.
* **Enqueue.** 67k rows/s measured against a 278/s requirement.
* **Fencing.** `run_epoch` comparisons are per-row and add nothing measurable.
* **Cascade deletes** — now that every foreign key has a leading index.

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
Raising it further would take claiming a *batch* per lock acquisition; that was
measured against this number and [rejected](#claiming-a-batch-per-lock-acquisition-rejected-on-the-measurement),
along with what would change the answer.

A cap that is *low* is a different thing entirely and no claim strategy touches
it: `max_concurrency` bounds in-flight work, so the queue permits
`cap / job duration` and that is the whole story.

What the lock choice *does* decide is what happens to a claimer that loses it.
The lock waits up to 50ms rather than failing instantly, and the reason is
worth knowing because it is not the obvious one:

A worker does not retry in a tight loop. It reads an empty claim as *"the queue
is empty"* — so it publishes idle demand, which re-arms that queue's enqueue
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

---

## Sizing

Per million jobs, with an average of 3 steps each:

| Table | Rows | Notes |
|---|---|---|
| `jorb` | 1M | plus 3 dead versions each until vacuumed |
| `jorb_history` | 4M | the largest table; one row per transition |
| `jorb_step` | 3M | prunable on a much shorter window than the job row |

`jorb_step` exists to make a job **resumable**. Once the job reaches a terminal
state, resume is impossible and every checkpoint it holds is dead weight kept
only for audit — which is why checkpoints get their own, much shorter retention
window than the job row. See [DXE.md](DXE.md#retention).

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
   *before* it reaches production.

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

* **Bulk inserts amortise per-commit costs.** Measure one transaction per job.
* **The NOTIFY commit lock only appears under concurrency.** A serial benchmark
  reports 3% for something that costs 62% in production.

---

## Design decisions on the write path

These are recorded because each one is a place where the obvious improvement
makes the platform slower, and someone will propose it again.

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

| untagged enqueue | jobs/s |
|---|---|
| without `jorb_tags_idx` | 28,700 |
| with `jorb_tags_idx` | 28,854 (1.005×) |

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

### Recounting for counters: rejected, and it is a correctness bug

A counter derived by recounting rows (`COUNT(*) FROM jorb_history WHERE
event='finished'`) **decreases when retention prunes**. Prometheus reads a
falling counter as a process restart and attributes the entire window's traffic
to a reset — silently. The old `_total` metrics did exactly this.

Counters were therefore **renamed** rather than re-typed in place, so a
dashboard using `rate(pyjobby_jobs_crashed_total[5m])` breaks loudly with a
missing series instead of quietly reporting garbage. A metric that lies is
worse than one that is absent.

### Claiming a batch per lock acquisition: rejected, on the measurement

`claim_jorb()` admits **one** job per advisory-lock acquisition, so a capped
queue's claims serialise and its ceiling is `1 / (critical section)`. Claiming
N jobs under one acquisition would divide the serialised part by N. The
proposal is sound; it is the requirement that is missing.

Measured with `pj-bench claim --workers 8 --jobs 2000 --repeat 7`, all arms
**interleaved** (median of 7, PostgreSQL 18.3, 10-core box under load ~4):

| arm | claimers | cap | job | claims/s | vs 278/s |
|---|---|---|---|---|---|
| uncapped, no completion | 8 | none | — | 19,037 | 68× |
| capped, no completion | 8 | 3,000 | — | 2,953 | 10.6× |
| uncapped, short jobs | 32 | none | 5 ms | 5,042 | 18× |
| **capped, short jobs** | 32 | 1,000 | 5 ms | **3,211** | **11.6×** |
| capped, short jobs | 2 | 2 | 5 ms | 236 | 0.85× |

The fourth row is the whole question: a cap too high to ever refuse, jobs short
enough that admission is all the work there is, and enough claimers that the
serialised section is the only thing left binding. It sustains **11.6× the
reference workload**. One capped queue would have to run **11.5M jobs/hour on
its own** before the lock is what stops it, against a platform target of
1M/hour across all queues. Exactness costs 0.64–0.68× against the same shape
with no cap (row 4 vs row 3) — that is the price of the lock, paid, and it is
already priced in above.

**Read these to one significant figure.** Repeat runs of the capped short-job
arm land at 2,969–3,260 claims/s (10.7–11.7×), and its *within-run* spread was
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
210–236/s because the claim and completion round trips land *on top of* the
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
rollup above: not "is it cheap", but *who pays when it is unused*.

**What would change this answer**, in the order it is likely to arrive:

1. A single capped queue that genuinely needs more than ~3,000 claims/s
   sustained — 11M jobs/hour through one cap. Two capped queues at half the
   rate are not this: the lock is per queue.
2. **The cap count losing its index.** The `count(*)` over
   `state IN ('claimed','running')` runs *inside* the lock, so it is subtracted
   from the queue's whole throughput rather than from one timer. It is gated as
   `concurrency_cap` by `pj-bench plans`, which measured it at **23 buffers**
   through `jorb_inflight_idx` with 200 jobs in flight in a 200k-row table.
   That plan holds while in-flight is a small fraction of the table, which is
   what a cap *makes* it. Drive in-flight up to the whole table and the planner
   correctly switches to a scan: 20,000 jobs claimed and none completing
   measured **581 claims/s** — still 2.1× the requirement, but that is the
   number that falls with *table* size instead of with the queue's own load.
   A capped queue holding tens of thousands of jobs in flight means jobs are
   not finishing; fix the count, or the workers, before touching the claim.

Until one of those is a measurement rather than a worry, the ceiling is not
where this platform runs out.
