# Running pyjobby at scale

Reference workload throughout: **1,000,000 jobs/hour** — about 278/second
sustained. Every number below was measured on this schema, not estimated. Where
something is a projection from a smaller measurement, it says so.

The short version: write throughput has **~43× headroom** at this target, but
less than a naive benchmark suggests, and the limit is `NOTIFY` rather than the
writes themselves. What breaks first is everything that has to read or retain
the *accumulated* table.

Every number here is reproducible with `pj-bench` — see [Reproducing
these numbers](#reproducing-these-numbers). They are not hand-measurements.

---

## What one job costs

| Per job | Count | At 278 jobs/s |
|---|---|---|
| `jorb` row writes (insert + claim + run + terminal) | 4 | ~1,100 writes/s |
| `jorb_history` rows (trigger, one per transition) | 4 | ~1,100 inserts/s, **96M rows/day** |
| Notifications emitted | 5 | **~1,390/s** |
| `jorb_step` rows | 1 per `step()` call | workload-dependent |

### Measuring enqueue honestly

How you measure this changes the answer by **6×**, so it is worth being precise
about what production actually does: each job is enqueued in **its own
transaction**, and many clients do so **concurrently**.

```
one bulk transaction, 20k rows                     68,105 rows/s   ← misleading
serial, one transaction per job                     5,979 jobs/s
16 concurrent connections, one transaction each    11,326 jobs/s   ← production shape
```

The bulk number is the one a careless benchmark reports, and it is meaningless:
a single transaction pays the per-commit costs **once**, amortised across 20,000
rows.

Against a 278/s requirement, 11,326/s is **~43× headroom** — comfortable, but
not the 240× the bulk figure implies.

### Why NOTIFY sets the ceiling

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
| as shipped (all channels) | 11,326 |
| `job_state_change` firehose disabled | 11,668 |
| all `NOTIFY` triggers disabled | **28,790** |

**The cost is per-COMMIT, not per-notification.** Disabling the transition
firehose — 3 of the 5 notifications per job — recovers only **3%**, because one
`NOTIFY` in a transaction takes the same global lock as three. Reducing
notification *volume* does not raise the ceiling; only removing `NOTIFY` from
the commit path does.

This is the single most important thing to understand before tuning: it is
invisible to a serial benchmark (3% there) and invisible to a bulk benchmark,
and it only appears under concurrent commits — which is exactly the production
shape.

### The other per-job costs

The history trigger writes a row per transition, which is why `jorb_history`
becomes the largest table in the system. That much is certain, and it is a
storage and retention problem rather than a throughput one.

What its *throughput* cost is remains **unmeasured under the production
shape**. Isolating it in a bulk benchmark suggests it dominates the non-NOTIFY
cost, but that is the same methodology that overstated enqueue headroom by 6×,
so the figure is not quoted here and no design decision rests on it. `pj-bench
enqueue` measures it concurrently, one transaction per job; that is the number
to act on.

This is deliberate: a plausible number measured the wrong way is more dangerous
than no number, because it gets optimised against.

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

At 1,390 notifications/second there is no margin for a slow consumer, so this is
monitored (`notify_queue_usage` metric, and a `doctor` check that WARNs well
before the cliff) rather than assumed away.

Of the five notifications per job, three are `job_state_change` — an unfiltered
per-transition firehose with no queue filter, broadcast to every listener. No
consumer can use ~830 messages/second of individual state transitions; a
dashboard wants aggregates. It can be turned off without touching the
load-bearing channels:

```sql
ALTER TABLE jorb DISABLE TRIGGER job_state_change_notify;
```

Do this to stop drowning your listeners and to slow the queue filling — **not
to gain write throughput.** It buys 3% (see [Why NOTIFY sets the
ceiling](#why-notify-sets-the-ceiling)); the ceiling is per-commit, so the only
way through it is taking `NOTIFY` out of the commit path entirely.

`jorb_enqueued` (worker wakeup) and `jorb_done` (result waiters) are
load-bearing and must stay on.

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
requires serializing claims for that queue through an advisory lock. The lock is
non-blocking: a claimer that cannot take it reports nothing claimable and polls
again. That is the right trade for a capped queue — a cap is a throughput limit
by definition — but do not put a million-jobs-per-hour queue under a cap and
expect uncapped throughput. Queues with no limits never take the lock at all.

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
2. Tune autovacuum for `jorb` and `jorb_history` per table.
3. Decide about the transition firehose. If nothing consumes individual
   transitions, disable that trigger.
4. Alert on `notify_queue_usage`, backlog age (not depth), and completions/sec
   versus arrivals/sec. Those three catch almost everything.
5. Watch that retention reports "caught up" rather than "out of budget".

---

## Reproducing these numbers

Every measurement above comes from `pj-bench`, which ships with the platform so
the numbers can be re-taken on your hardware and re-checked after a change:

```
pj-bench enqueue    # write throughput; bulk vs serial vs concurrent, and the
                    # per-NOTIFY-channel breakdown that shows the commit lock
pj-bench claim      # claim throughput and advisory-lock contention
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

### Recounting for counters: rejected, and it is a correctness bug

A counter derived by recounting rows (`COUNT(*) FROM jorb_history WHERE
event='finished'`) **decreases when retention prunes**. Prometheus reads a
falling counter as a process restart and attributes the entire window's traffic
to a reset — silently. The old `_total` metrics did exactly this.

Counters were therefore **renamed** rather than re-typed in place, so a
dashboard using `rate(pyjobby_jobs_crashed_total[5m])` breaks loudly with a
missing series instead of quietly reporting garbage. A metric that lies is
worse than one that is absent.
