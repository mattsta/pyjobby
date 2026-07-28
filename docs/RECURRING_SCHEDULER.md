# The recurring scheduler

Cron schedules that live in the database instead of a crontab: a row per
schedule, with execution history, statistics, an enable/disable switch, and
the safety limits that stop a slow job turning a schedule into a runaway.

A schedule does not *run* anything. It creates an ordinary job at the right
instant; a worker runs it like any other. Everything in
[writing-jobs.md](writing-jobs.md) applies unchanged to a scheduled job.

**Two things here are subtle enough to be worth reading before you write a
schedule**: the [cron column layout](#cron-expressions) (the sixth field is
seconds *at the end*, not the Quartz seconds-first layout) and
[what happens at a DST transition](#daylight-saving-time). Both are pinned by
`tests/test_cron_semantics.py`; the scheduler's failure handling is pinned by
`tests/test_scheduler_correctness.py`.

## The moving parts

| Piece | Where | What it does |
|---|---|---|
| `jorb_schedule` | `pyjobby/sql/schema/70_schedules.sql` | one row per schedule: the job to create, the cron expression, the safety limits, the next fire time, the counters |
| `jorb_schedule_log` | same | one row per firing: result, skip reason, created job, duration, queue depth, jitter applied |
| `pyjobby/cron.py` | — | all cron and timezone evaluation. Owns the column layout and the DST rules so no caller can get them half right |
| `SchedulerWorker` | `pyjobby/scheduler.py` | the poll loop: find due schedules, apply the safety checks, create the job, advance `next_run` |
| `ScheduleSafetyManager` | same | concurrency, backpressure, circuit breaker, jitter |
| `ScheduleManager` | same | create, next-run arithmetic, the bookkeeping updates |
| `AdminAPI.*_schedule` | `pyjobby/admin_api.py` | the management API the CLI and the web interface both use |

## Running it

```bash
pj-scheduler --config ./pyjobby.toml          # polls every 60s
pj-scheduler --config ./pyjobby.toml --poll-interval 15
```

The config file need only define `db_params`. It is a separate process from
the workers — the scheduler creates jobs, it does not execute them, so you
still need `pj` running somewhere on the schedule's queue.

**Run more than one for redundancy.** Each firing is done inside a
transaction that re-locks the schedule row `FOR UPDATE SKIP LOCKED`, and the
created job carries a deadline key of `schedule:<id>:<scheduled_time>`, so a
second instance is a no-op rather than a duplicate.

`SIGTERM` and `SIGINT` cut the poll sleep short rather than waiting out the
interval, so a 60-second poll does not mean a 60-second shutdown.

## Creating a schedule

### CLI

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

pj-admin schedule list
pj-admin schedule show daily-revenue
pj-admin schedule disable daily-revenue
pj-admin schedule enable daily-revenue           # also resets the failure streak
pj-admin schedule history daily-revenue --result failure
pj-admin schedule stats
pj-admin schedule delete daily-revenue
```

Every command that names a schedule takes a name **or** an id. `--prio`,
`--capability`, `--jitter`, `--backpressure` and `--disabled` (create it
switched off) are the remaining `add` options, and `list`, `show`, `history`
and `stats` all take `--json`; `pj-admin schedule --help` is authoritative.

### Python

```python
from pyjobby import db
from pyjobby.admin_api import AdminAPI

conn = await db.connect(**db_params)
schedule = await AdminAPI(conn).create_schedule(
    name="daily-revenue",
    job_class="myapp.reports.DailyRevenueReport",
    cron_expr="0 2 * * *",
    timezone="America/New_York",
    queue="reports",
    kwargs={"region": "emea"},
    max_concurrent_jobs=1,
    circuit_breaker_threshold=3,
    description="Daily revenue report at 2am Eastern",
)
```

Use `pyjobby.db.connect()` (or a `JobClient` pool), **not** bare
`asyncpg.connect()`: `kwargs` is a JSONB column and needs pyjobby's codecs. A
bare connection fails at the INSERT with a `DataError` —
`tests/test_examples_doc.py` pins both halves.

`create_schedule()` and `update_schedule()` evaluate the expression before
they write, so a malformed cron or an unknown timezone is rejected at the
point it was entered rather than silently never firing. Changing `cron_expr`
or `timezone` through `update_schedule()` recomputes `next_run` with it.

The other methods: `list_schedules(enabled=..., queue=...)`,
`get_schedule(schedule_id=...)` / `get_schedule(name=...)`,
`enable_schedule()`, `disable_schedule()`, `delete_schedule()`,
`get_schedule_history(schedule_id, limit=, result_filter=)`,
`get_schedule_stats()`.

### Web

`http://localhost:8081/schedules` (`pj-web`) lists every schedule with its
statistics, creates new ones from a form, and enables, disables or deletes
with one click. It refreshes every 10 seconds.

## Cron expressions

pyjobby delegates parsing and next-fire arithmetic to
[croniter](https://github.com/kiorky/croniter), through `pyjobby/cron.py`.
That makes croniter's exact behavior part of the platform's contract, so it
is pinned against real datetimes in `tests/test_cron_semantics.py` rather than
assumed from documentation.

**The column layout is croniter's default, and it is not Quartz:**

```
 ┌───────────── minute        (0-59)
 │ ┌─────────── hour          (0-23)
 │ │ ┌───────── day of month  (1-31)
 │ │ │ ┌─────── month         (1-12)
 │ │ │ │ ┌───── day of week   (0-7; 0 and 7 are Sunday)
 │ │ │ │ │ ┌─── seconds       (0-59)   OPTIONAL, and at the END
 │ │ │ │ │ │ ┌─ year                   OPTIONAL
 │ │ │ │ │ │ │
 * * * * * * *
```

> **The sixth field is seconds at the END.** Quartz and Spring put seconds
> *first*, so the same six numbers mean different times under the two
> conventions. `0 2 * * * 30` here is **02:00:30 daily**, not "every 2am
> minute, 30 seconds in". If you are porting expressions from a Quartz-based
> scheduler, re-read every six-column one.

Five columns is the ordinary form and the one to prefer.

```bash
"0 2 * * *"        # 02:00 every day
"0 */6 * * *"      # every 6 hours
"0 0 * * 0"        # Sunday at midnight
"*/15 * * * *"     # every 15 minutes
"0 9-17 * * 1-5"   # hourly, 09:00-17:00, Mon-Fri
"0 0 1 * *"        # first of the month
"0 2 * * * 30"     # 02:00:30 every day  (6 columns: seconds LAST)
"0 2 * * * 0 2028" # 02:00:00, only in 2028 (7 columns: + year)
```

The next fire time is always **strictly after** the instant it is computed
from, so a schedule that has just fired cannot select itself again.

## Timezones

`timezone` is an IANA name resolved with the standard library's `zoneinfo`;
the default is `UTC`. The computed `next_run` is timezone-aware and carries
the schedule's own zone, so the `timestamptz` column records the intended
instant.

```
UTC · America/New_York · America/Los_Angeles · Europe/London · Asia/Tokyo
```

**UTC has no transitions**, which makes it the right choice for anything that
means "every 24 hours" rather than "at this time on the clock". Everything in
the next section applies only to zones that observe daylight saving.

## Daylight saving time

Two days a year, "02:00 daily" is ambiguous or impossible, and the
right answer depends on what the schedule *means*. pyjobby distinguishes the
two cases by the **hour field**: an expression that enumerates hours is
anchored to the wall clock, and one with a wildcard or a step is an interval.
That is the rule vixie cron settled on, for the same reason.

```python
is_wall_clock_anchored("30 1 * * *")  # True  — a named hour
is_wall_clock_anchored("0 2,14 * * *")  # True  — several named hours
is_wall_clock_anchored("0 * * * *")  # False — every hour
is_wall_clock_anchored("*/15 * * * *")  # False — every 15 minutes
is_wall_clock_anchored("0 */2 * * *")  # False — every 2 hours
```

### Falling back: the hour that happens twice

On 2027-11-07 `America/New_York` repeats 01:00–02:00. 01:30 occurs twice —
once at UTC-4, once at UTC-5 — and croniter yields both, because as a
wall-clock expression both really are matches.

* **A wall-clock-anchored schedule fires ONCE.** `30 1 * * *` means "once a
  day, at half past one". Firing on both passes would run a daily job twice
  and duplicate every side effect it has: a second invoice, a second email, a
  second charge. `next_cron_run()` skips the pass marked `fold=1` — the
  replay of a wall-clock time the schedule has already fired at — and the
  schedule resumes normally the next day.
* **An interval schedule fires on BOTH passes.** `0 * * * *` means every hour
  of *real* time. Skipping one would leave a two-hour gap, which is the
  opposite mistake. Measured in UTC, the cadence through the transition is
  exactly 3600 seconds per step.

A trap worth knowing while debugging one of these: subtracting two datetimes
that share a `tzinfo` is naive arithmetic — Python ignores `fold` — so the
two 01:30s appear to be zero seconds apart. Convert to UTC before measuring
elapsed time across a transition.

### Springing forward: the hour that does not happen

On 2027-03-14 `America/New_York` jumps 02:00 → 03:00, so 02:30 does not
exist. A schedule of `30 2 * * *` is **not skipped**: it fires at 03:00, the
instant the clock lands on, and returns to 02:30 the following day. A daily
job silently not running is worse than one running half an hour late.

## Safety features

All four are per-schedule columns with defaults, and all four are checked in
this order before a job is created.

### 1. Circuit breaker — `circuit_breaker_threshold` (default 5)

Consecutive failures are counted. On reaching the threshold the schedule is
**disabled** and stays that way until someone re-enables it, which resets the
counter to zero. A schedule that always fails should stop trying, loudly.

```bash
pj-admin schedule show flaky-job      # Consecutive Failures: 5, Enabled: No
pj-admin schedule enable flaky-job    # after fixing the cause
```

Suggested values: 3–5 for critical schedules that should fail fast; 10–20 for
ordinary ones; higher when occasional failures are expected.

### 2. Max concurrent jobs — `max_concurrent_jobs` (default 1)

Counts this schedule's jobs still in `queued`, `claimed`, `running` or
`waiting`. At the limit, the firing is **skipped** and recorded as such.

This is the guard against a schedule outrunning its job: an hourly job that
takes 90 minutes would otherwise accumulate one more copy every hour, forever.

Use `1` for anything that must not overlap (backups, migrations, anything
writing the same rows), 3–5 for jobs that may overlap but not unboundedly,
higher for fast stateless ones.

### 3. Backpressure — `backpressure_threshold` (default 1000, `None` disables)

Counts `queued`, `claimed` and `running` rows in the schedule's **queue** —
not just this schedule's jobs, and *not* `waiting` ones, which are blocked on
a dependency rather than competing for a worker. Over the threshold, the
firing is skipped: adding work to an overloaded queue makes the overload
worse, and a skipped run of a non-critical schedule is cheap.

100–500 catches overload early on a queue that should stay shallow;
1000–5000 suits an ordinary queue; `None` for a schedule that must always
fire.

### 4. Jitter — `jitter_seconds` (default 0)

A random 0..N second offset that is **added to the created job's
`run_after`** — the scheduler does not sleep. That distinction matters: a
scheduler that slept would stall every schedule queued behind the jittery one,
and one slow schedule would delay the whole install.

Use 0 for time-critical schedules, 60–300 to spread a thundering herd of jobs
that all want the same external API at the top of the hour, 600–1800 for
background work with flexible timing.

### 5. Deadline keys — automatic

Every created job carries `deadline_key = schedule:<id>:<scheduled_time>`,
which is unique across queued rows. Two scheduler instances firing the same
schedule at the same instant produce one job; the loser records a `duplicate`
skip. Nothing to configure.

The created job also carries `schedule_id` — a **column** on `jorb`, not an
`admin_data` key — plus `admin_data.schedule_name` and
`admin_data.scheduled_time`. `schedule_id` is how the concurrency check finds
its own jobs and how a job identifies the schedule that made it
(`self.job["schedule_id"]`, a `bigint`, `None` for a job nobody scheduled).

It is a column because it is the one thing about a scheduled job that anything
*queries by*, and while it lived in the `admin_data` blob no index could serve
that query — see [Performance](#performance). The `admin_data` copy is **gone**
rather than kept alongside: two copies of one fact disagree eventually.
`pj-admin db migrate` moves it on existing databases, including jobs that are
still in flight.

## When a schedule cannot be evaluated

A cron expression or timezone the platform cannot evaluate — a hand-edited
row, or an expression a newer croniter no longer accepts — is a special case,
because such a schedule can never compute a new `next_run` and would
therefore be due forever.

The scheduler resolves the *following* fire time **before** it fires. If that
fails, the schedule is **disabled**, the failure is counted
(`run_count`, `failure_count`, `consecutive_failures`, `last_run`,
`last_failure`), and a `failure` row naming the error is written to
`jorb_schedule_log`. `next_run` is deliberately left alone.

Disabling is the only outcome that both stops the spin and is visible to an
operator: leaving it enabled means re-selecting and re-failing it on every
poll, with the failing transaction rolling its own bookkeeping back, so
nothing is recorded anywhere anyone would look. `tests/test_scheduler_correctness.py`
pins both directions — the broken schedule disabled and counted, and a working
schedule alongside it still advancing.

## Monitoring

```bash
pj-admin schedule stats                          # success rate and next run
pj-admin schedule history daily-revenue          # last 50 firings
pj-admin schedule history daily-revenue --result failure
pj-admin schedule history daily-revenue --json   # for scripting
```

`jorb_schedule_log` records, per firing: `scheduled_time` (when it was due)
and `actual_time` (when it really ran), `result`
(`success` / `failure` / `skipped`), `skip_reason`, the `job_id` created,
`error_message`, `duration_ms`, `queue_depth_at_run`,
`concurrent_jobs_at_run` and `jitter_applied_seconds` — enough to answer "why
did this not run" without guessing. Rows cascade away with their schedule.

Log levels the scheduler uses: `INFO` for a firing, `WARNING` for a skip or a
prevented duplicate, `ERROR` for a failure or a tripped circuit breaker.

### A schedule is not running

1. `pj-admin schedule show <name>` — is it enabled? Did the circuit breaker
   trip (`Consecutive Failures` at the threshold)?
2. Is `next_run` in the future? Read it in the schedule's own timezone before
   concluding it is wrong; a DST transition moves it by an hour on purpose.
3. Is `pj-scheduler` running at all?
4. `pj-admin schedule history <name>` — a run of `skipped` rows names the
   safety check that skipped them.
5. If firings succeed but nothing happens, the *jobs* are the problem, not
   the schedule: is a worker running on that queue, and does it advertise the
   schedule's `capability`?

## Performance

Finding the due schedules is a single indexed query
(`jorb_schedule_due_idx ON (next_run) WHERE enabled`), so the cost of a poll
is proportional to the schedules actually due, not to the number configured.
Each firing is one short transaction, and the created job is one INSERT.

The safety checks run once per firing, and the concurrency one is why
`jorb.schedule_id` is a COLUMN with a partial index rather than an
`admin_data` key: no index can serve an expression inside a jsonb blob, so
counting that way would sequentially scan the whole job table on **every
firing** — a cost set by how many jobs the install has ever run rather than
by anything about the schedule, invisible on a young database and never
announcing itself.

`schedule_id` is now a column with a partial index over it:

```sql
CREATE INDEX jorb_schedule_id_idx ON jorb (schedule_id)
    WHERE schedule_id IS NOT NULL
      AND state IN ('queued', 'claimed', 'running', 'waiting');
```

Both halves of that predicate are load-bearing. `schedule_id IS NOT NULL` keeps
every client-enqueued job out of the index, so the hot enqueue path writes
nothing to it — measured at 27,941 vs 28,010 jobs/s across nine interleaved
`pj-bench enqueue --concurrency 16 --repeat 3` pairs, which is no difference.
The live-state list is what stops the fix becoming the original problem: a
schedule accumulates jobs forever (a minutely one, ~525k a year), so an index
on `schedule_id` alone would hand the check every job the schedule had *ever*
created and make it discard the finished ones — an index scan that reads a
table's worth of rows costs a table's worth. Restricted to the live states, the
check reads only in-flight work, which `max_concurrent_jobs` itself bounds.

The catch of a partial index is that a query may use it only when its own
clauses **imply** the predicate, and PostgreSQL proves that syntactically — so
the check spells out the same state list rather than binding it as a parameter.
`tests/test_scale_plans.py` EXPLAINs the scheduler's own statement and fails on
a sequential scan, on a wrong access method, and on rows read and thrown away.

The backpressure check counts unfinished rows in the schedule's *queue*, which
is a different question and unaffected by any of this.

## Migrating to it

**From crontab**: you gain centralized management, the safety limits above,
execution history, and enable/disable without editing a file on a box. You
lose nothing — the cron expression is the same five columns.

**From Celery Beat**: no Redis, no separate result backend; history and
statistics are rows in the same database as the jobs. Note the [column
layout](#cron-expressions) if you are carrying over six-column expressions.

## See also

* [writing-jobs.md](writing-jobs.md) — what goes inside the scheduled job
* [EXAMPLES.md](EXAMPLES.md#9-a-recurring-report) — the whole path, end to end
* [ADMIN_TOOLS.md](ADMIN_TOOLS.md) — the rest of `pj-admin`
* [OPERATIONS.md](OPERATIONS.md) — running the workers that execute the jobs
