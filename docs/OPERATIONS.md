# Operating pyjobby

The runbook: what runs, how to check it, and what to do when something is
wrong. The executable version of the health section is `pj-admin doctor`.

## The processes

| Process | Command | Count | Purpose |
|---|---|---|---|
| Workers | `pj --config ./pyjobby.conf.py --queue Q --workers N` | per queue/host as needed | claim + execute jobs |
| Monitor | `pj-monitor --config ./pyjobby.conf.py` | 1 (more are safe) | timeout enforcement, dead-worker reclaim |
| Scheduler | `pj-scheduler --config ./pyjobby.conf.py` | 1 (more are safe) | fires cron schedules |
| Web admin | `pj-web ./pyjobby.conf.py --host 127.0.0.1 --port 8081` | optional | HTML admin + `/metrics` |
| Websocket | `pj-ws ./pyjobby.conf.py --port 8082` | optional | realtime dashboard feed |

Start order does not matter — every process connects independently and
tolerates the database being briefly unavailable. One command installs or
upgrades the schema idempotently:

```bash
pj-admin db migrate        # base schema + any pending migrations
pj-admin db status
```

Neither web surface has authentication: keep them on localhost or behind an
authenticating proxy.

## Health: `pj-admin doctor`

```bash
pj-admin --dsn "$PYJOBBY_DSN" doctor [--max-depth 10000] [--max-age-minutes 60]
```

Checks (FAIL exits nonzero; WARN does not): database reachable, schema
installed and migrations current, NOTIFY triggers present, live workers
seen in the last 60s, per-queue depth and oldest-job age, DLQ size, overdue
schedules. Run it from cron/CI as a platform health probe; scrape
`GET /metrics` on the web admin for Prometheus.

## How execution works (what the states mean)

```
queued -> claimed -> running -> finished          (success)
                          \-> queued (retry, same row, backoff)
                          \-> crashed              (terminal: THE DLQ)
                          \-> cancelled            (terminal)
waiting -> queued                                  (dependency satisfied)
```

* A job keeps **one row for life**. Retries requeue the same row;
  `run_epoch` increments on every claim and fences superseded executions
  out of writing anything. Per-attempt details are in `jorb_history`
  (`pj-admin jobs history ID`).
* **`crashed` is terminal**: the dead letter queue is exactly
  `state = 'crashed'`. `pj-admin dlq list` / `pj-admin dlq retry ID`
  (errors reset) or `pj-admin jobs requeue ID`.
* **DXE jobs** (using `self.step(...)`) resume from their last completed
  checkpoint on any retry — `pj-admin jobs steps ID` shows what completed.
  `pj-admin jobs requeue ID --fresh` wipes checkpoints for a from-scratch
  rerun.
* **Durable sleeps** hold no worker: a sleeping job is simply `queued` with
  a future `run_after`.

## Timeouts

A job's timeout is `admin_data.timeout_seconds`, else the job class's
`timeout`, else the worker's `--default-timeout` (3600s). `0` disables it.
It is **one** number, enforced in two places:

* **In-process, by the worker.** A single deadline wraps the whole
  execution, so a job configured for N seconds stops being run at N seconds
  — not at 2N because it spent time producing its coroutine, and not
  indefinitely because it streamed its results from an async generator.
  Reaching it applies `on_timeout` (`retry`, the default, or `fail`).
* **Out-of-process, by the monitor.** `jorb.timeout_at` is written when the
  job starts running and cleared by every terminal transition; the monitor
  sweeps rows past it and applies the same `on_timeout` policy. This is the
  backstop for everything the worker cannot enforce itself — a killed
  worker, a lost database connection, or a job that blocks so hard it
  cannot be interrupted.

**What the in-process deadline can actually interrupt.** It is delivered as
a cancellation at an await point, so async code is genuinely stopped where
it is suspended, `finally` blocks and all. A *synchronous* `task()` runs in
a worker thread: the deadline still fires on time and the job is recorded as
timed out, but nothing stops the thread — it runs to completion in the
background and its result is discarded. Synchronous code called **inline**
from an async `task()` is worse: it blocks the event loop and starves the
timer, so its deadline is advisory until it returns. For both, the monitor
(or ending the process) is what bounds the rest. Note that `self.cancelled`
is the **operator** cancel signal (`pj-admin jobs cancel`) and is *not* set
by a timeout: a long synchronous loop that wants to stop itself early has to
watch its own clock.

## Queue controls (live; no restarts)

```bash
pj-admin queues pause NAME          # workers stop claiming immediately
pj-admin queues resume NAME
pj-admin queues limits NAME --max-concurrency 8 --rate-limit 100 --rate-period 60
pj-admin queues limits NAME --max-concurrency none      # clear a limit
pj-admin queues show NAME
```

Controls live in `jorb_queue` and are enforced inside the worker's claim
statement — changes take effect on the next claim attempt (sub-second).

## Failure playbooks

**A worker host died.** Nothing to do. Its registry heartbeat
(`jorb_worker.last_seen`) goes stale; within the monitor's
`--liveness-grace` (60s default) the monitor requeues its in-flight jobs
and retires the worker rows. Jobs resume from their last completed step.

**A job is stuck running / hung.** `pj-admin jobs inspect ID` — if past its
`timeout_at`, the monitor will retry/dead-letter it per its `on_timeout`
policy. To intervene now: `pj-admin jobs cancel ID` (running jobs receive
the cancellation within ~1s and stop at their next await point).

**A queue is flooding the system.** `pj-admin queues pause NAME`, then
inspect (`pj-admin jobs list -q NAME`), bulk-cancel or fix, then resume.
For chronic pressure set `--max-concurrency` / `--rate-limit` instead.

**Jobs are landing in the DLQ.** `pj-admin dlq list`, then
`pj-admin jobs history ID` for the per-attempt errors and
`pj-admin jobs steps ID` to see where a durable pipeline stopped. After a
code fix, `pj-admin dlq retry ID` (fresh attempt budget).

**Nothing is being claimed.** In order: `pj-admin doctor`;
`pj-admin queues show NAME` (paused? limits hit?); `pj-admin workers list`
(any live workers on that queue?); remember jobs with `prio` above the
workers' ceiling (default 1000) or `capability` no worker advertises are
invisible to those workers.

**The scheduler missed fires** (was down at fire time). Missed ticks are
skipped, not backfilled; `next_run` advances from now. Check
`pj-admin schedule history NAME`.

**Database was down.** Workers/monitor/scheduler reconnect with backoff
automatically and re-prepare their statements; nothing needs a restart.

## Observability quick reference

| Question | Answer |
|---|---|
| Fleet health | `pj-admin doctor`, `pj-admin workers list` |
| Queue depths/ages | `pj-admin queues list`, `/metrics` gauges |
| What happened to job N | `pj-admin jobs history N`, `jobs steps N` |
| Throughput/error rates | `/metrics` counters + duration quantiles |
| Live event stream | `pj-ws` + `frontend/live-dashboard.html` |
| Progress of a running job | `client.get_event(job_id, "progress")` (if the job publishes) |

## Queue controls: what the limits actually promise

`max_concurrency` and `rate_limit` are enforced by `claim_jorb()` in the
database, not by the workers, so they bind every claimer — a worker, a
script, anything that admits a job. Two consequences worth knowing:

* **The limits are exact, not approximate.** Claims for a queue that has
  either limit set are serialized against each other, so simultaneous
  claims cannot each read a stale count and admit past the cap.
* **They cost nothing when unset.** A queue with no limits never takes the
  lock and claims exactly as fast as it did before.

`rate_limit` counts **admissions** in the trailing `rate_period_seconds`
window — jobs picked up by a worker, not jobs that reached `running`. The two
differ by one statement, and counting the latter let a burst slip through.

## Retry vs. re-run

Two different verbs, because they carry different risk:

* **Retry** — `pj-admin jobs retry ID`, `pj-admin dlq retry ID`. For a job
  that did *not* succeed (`crashed` or `cancelled`).
* **Re-run** — `pj-admin jobs requeue ID`. Also accepts a **finished** job.
  Running successful work again repeats its side effects, so it is a
  separate verb rather than a permissive retry.

Add `--fresh` to a re-run to drop the DXE step checkpoints and start from
step 1; without it, completed steps fast-forward and the job resumes.

## Reading the latency numbers

`pj-admin metrics` reports queue wait and execution duration separately:

* **Avg/Max Queue Wait** — how long jobs sat before a worker picked them up
  (`claimed_at - run_after`). Rising wait with flat duration is a **capacity**
  problem: add workers, or raise `max_concurrency`.
* **Avg Duration** — how long jobs ran once picked up (`finished - started`).
  Rising duration is a **code or dependency** problem.

A single blended "how long did the job take" number cannot tell these apart,
which is why it is not reported.
