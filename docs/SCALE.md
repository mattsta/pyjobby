# Running pyjobby at scale

Reference workload throughout: **1,000,000 jobs/hour** — about 278/second
sustained. Every number below was measured on this schema, not estimated. Where
something is a projection from a smaller measurement, it says so.

The short version: **raw write throughput is not the problem** — there is
roughly 240× headroom on enqueue. What breaks first is everything that has to
read or retain the *accumulated* table, and the notification fan-out.

---

## What one job costs

| Per job | Count | At 278 jobs/s |
|---|---|---|
| `jorb` row writes (insert + claim + run + terminal) | 4 | ~1,100 writes/s |
| `jorb_history` rows (trigger, one per transition) | 4 | ~1,100 inserts/s, **96M rows/day** |
| Notifications emitted | 5 | **~1,390/s** |
| `jorb_step` rows | 1 per `step()` call | workload-dependent |

Measured enqueue throughput, 20k rows, single connection:

```
all triggers on (as shipped)      298 ms    67,085 rows/s
state-change firehose off         279 ms    71,767 rows/s
firehose + history off            102 ms   196,160 rows/s
```

The history trigger is **59%** of enqueue cost and the transition firehose is
**7%**. Both are affordable — 67k rows/s against a 278/s requirement is 240×
headroom — but the history trigger is why `jorb_history` becomes the largest
table in the system, and that is a storage and retention problem rather than a
throughput one.

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
