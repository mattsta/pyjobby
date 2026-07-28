# The admin surface

Three ways to drive a running platform, over one backend:

| | What it is | Best for |
|---|---|---|
| `pj-admin` | the operator CLI | everything below; scripting |
| `pj-web` | HTML admin + `/metrics`, no auth | dashboards, browsing |
| `pyjobby.admin_api.AdminAPI` | the Python API both of the above call | custom automation |

This is the reference for *what exists*. What to do with it when something
is wrong is [OPERATIONS.md](OPERATIONS.md) (playbooks) and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) (symptoms). Every command here was
run against a live database; the outputs are real.

## Connecting

`pj-admin` takes a DSN or a config file, and a DSN wins:

```bash
pj-admin --dsn postgresql://user:pass@host:5432/pyjobby doctor
export PYJOBBY_DSN=postgresql://user:pass@host:5432/pyjobby   # same thing
pj-admin -c /etc/pyjobby/pyjobby.toml doctor               # config file
```

```console
$ pj-admin --help
Usage: pj-admin [OPTIONS] COMMAND [ARGS]...

  Pyjobby job queue management CLI

Options:
  -c, --config TEXT  Config file path
  --dsn TEXT         PostgreSQL DSN (overrides --config; also read from
                     PYJOBBY_DSN)
  --help             Show this message and exit.

Commands:
  dag       Manage DAGs (Directed Acyclic Graphs)
  db        Manage the database schema (install / migrate / status)
  dlq       Manage Dead Letter Queue
  doctor    Run health checks against the job platform (exit 1 on any FAIL)
  jobs      Manage jobs
  metrics   Show system metrics
  queues    Manage queues
  schedule  Manage recurring schedules
  workers   Manage workers
```

### Exit codes: scripts can rely on them

Operator-facing failures exit non-zero. A job that does not exist, a job in
the wrong state for the verb, an unusable config, an unreachable database,
or any `doctor` FAIL all exit 1:

```console
$ pj-admin jobs inspect 999999999
Error: Job 999999999 not found
$ echo $?
1
```

An empty *answer* is not a failure — `dlq list` on an empty DLQ, or
`queues show` for a queue with no jobs and no control row, exit 0.

Machine-readable output is `--json`, available on `jobs list`, `jobs
inspect`, `jobs history`, `jobs steps`, `jobs retry-stats`, `jobs
timeout-stats`, `queues list`, `queues show`, `queues stats`, `workers
list`, `dlq list`, `metrics`, `schedule list`, `schedule show`, `schedule
history`, `dag list` and `dag show`.

```bash
pj-admin jobs retry $(pj-admin jobs list --state crashed --json | jq -r '.[].id')
```

## `db` — schema install and upgrade

```console
$ pj-admin db --help
Commands:
  migrate  Install the base schema if missing, then apply pending migrations
  status   Show applied vs pending schema migrations
```

`db migrate` handles **both** histories, and upgrading an existing database
is one of them:

* **A fresh database** gets `schema.sql` — the whole current schema, in one
  file — and every shipped migration is then *recorded* without being run,
  because `schema.sql` already contains their effects.
* **An existing database** gets the numbered files in
  `pyjobby/sql/migrations/` it has not recorded, oldest first, one
  transaction per file. A database installed before the runner existed has
  no `schema_migrations` table, which reads as "recorded nothing", so it
  gets every migration — which is right, because it was installed from an
  older revision of `schema.sql`.

Same command either way, and it is idempotent. It is **safe to run from
every host's deploy step simultaneously**: `migrate()` takes a session-level
advisory lock, so one process installs or upgrades and the others wait and
then find nothing to do. (A first install is not idempotent statement by
statement — `CREATE TYPE jorbstate` has no `IF NOT EXISTS` — which is what
the lock is there for.)

`db status` on a database that is behind:

```console
$ pj-admin db status
Base schema installed: yes
Applied migrations:    none
Pending migrations:    [1, 2, 3, 4]
Missing objects:       3
  index jorb_dag_retention_idx
  index jorb_schedule_log_retention_idx
  index jorb_worker_retention_idx
```

`Missing objects` is the load-bearing line. `Pending migrations` can only
say "this database has not *recorded* migration N", and a database that
predates the runner records nothing at all while still being stale; the
missing list is read out of the catalog and compared against the
required-shape manifest in `pyjobby/migrations.py`. A healthy database
prints `Missing objects:       none`.

`db migrate` reports "applied" and "recorded" distinctly, because they are
different events: a fresh install prints `Installed base schema` followed by
`Recorded migrations [...] (already contained in the base schema)`, an
upgrade prints `Applied migrations: [...]`, and a database already current
prints `Database schema is up to date`.

See
[deployment-guide.md § The database](deployment-guide.md#the-database) for
what it does and does not bring forward.

## `doctor` — the health entry point

```console
$ pj-admin doctor --help
Usage: pj-admin doctor [OPTIONS]

  Run health checks against the job platform (exit 1 on any FAIL)

  Checks: database reachability, schema/migrations, NOTIFY triggers, NOTIFY
  queue saturation, live workers, workers that are alive but claiming nothing,
  queue backlogs, the DLQ, and overdue schedules.

Options:
  --max-depth INTEGER        WARN when a queue's backlog exceeds this many
                             queued jobs
  --max-age-minutes INTEGER  WARN when a queue's oldest queued job is older
                             than this
```

A live platform, idle:

```console
$ pj-admin --dsn "$PYJOBBY_DSN" doctor
PASS database: connected
PASS schema: installed and complete; migrations [1, 2, 3, 4] are not recorded yet, which the next upgrade reads (run: pj-admin db migrate)
PASS triggers: all schema triggers present (7)
PASS notify-queue: 0.0% full
WARN workers: no live workers seen in last 60s
PASS job-threads: 0 live worker(s) claiming
PASS queues: no queued jobs
PASS dlq: empty
PASS schedules: no overdue schedules
$ echo $?
0
```

### What the `schema` check actually checks

Presence-and-pending is not enough to certify a schema: a database
installed from an *older* `schema.sql` has `jorb` and records nothing
pending, so both answers look healthy while the very next query dies on a
missing column.

So the check is the **shape**: every table, column, view, function, index
and enum label this release addresses, by name, read out of the catalog and
compared against the manifest in `pyjobby/migrations.py` (which the test
suite asserts equals a fresh install's catalog in both directions, so it
cannot rot). Three verdicts:

| Verdict | Line |
|---|---|
| FAIL | `base schema not installed (run: pj-admin db migrate)` |
| FAIL | `installed, but N object(s) this release needs are missing: …` — up to five named, then a count |
| PASS | `installed, migrations current (…)`, or `installed and complete; migrations […] are not recorded yet, which the next upgrade reads` |

A stale schema, named:

```console
$ pj-admin doctor
PASS database: connected
FAIL schema: installed, but 3 object(s) this release needs are missing: index jorb_dag_retention_idx, index jorb_schedule_log_retention_idx, index jorb_worker_retention_idx (run: pj-admin db migrate)
$ echo $?
1
```

Either FAIL ends the report there — every check below `schema` queries
something it just reported missing, and a health report that crashes halfway
through is worse than one that stops. `pj-admin db status` prints the full
missing list.

A schema that is complete but has *unrecorded* migrations — the third line
in that table — is a **PASS**, not a FAIL: the shape check just proved every
object those files install is already present, so the running code can
address this database and only the bookkeeping is behind. Waking someone at
3am over a missing row in `schema_migrations` is how a health probe teaches
people to ignore it. It is still said out loud, because that record is what
the *next* upgrade reads: until `db migrate` runs, a later release cannot
tell this database from one that never applied the migration at all.

Triggers get their own check for the same reason the shape does: nothing
raises when one is missing, the platform just quietly stops waking waiters
or recording history. All seven the schema installs are checked by name.

FAIL is otherwise reserved for "the platform cannot function" — a missing
trigger, a NOTIFY queue past half full — and is the only thing that changes
the exit code. Lost capacity is a WARN. Which check means what, and what to
do about each, is in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#start-with-doctor).

Defaults are `--max-depth 10000` and `--max-age-minutes 60`. Age is the
more honest alarm: a deep queue that is draining is fine, an old queue is
not.

## `jobs`

```console
$ pj-admin jobs --help
Commands:
  cancel         Cancel one or more jobs.
  delete         Delete a job (permanent!)
  history        Show a job's full transition trail (including per-attempt errors)
  inspect        Show detailed information about a job
  list           List jobs with optional filtering
  requeue        Requeue a terminal job for another run (also: RESUME an interrupted job)
  retry          Retry one or more crashed jobs
  retry-stats    Show retry statistics from the jorb_history audit trail
  set-priority   Change a queued or waiting job's priority.
  steps          Show a job's DXE step checkpoints
  timeout-stats  Show timeout statistics (from jorb.timeout_at/state)
```

### Finding jobs

```console
$ pj-admin jobs list --limit 5
ID     State     Queue          Job Class      Priority  Created
----------------------------------------------------------------------
19615  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19616  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19612  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19609  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19617  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01

Showing 5 job(s). Use --limit and --offset for pagination.
```

Filters: `-q/--queue`, `-s/--state`, `--job-class` (patterns), `--uid`,
`--tag KEY=VALUE` (repeat for AND), `-l/--limit`, `-o/--offset`, `--json`.
The table truncates long values for width — use `--json` when you need the
full queue name or class path.

`--tag` matches jobs *containing* the pair, so extra tags on the job are
fine, and values are read as JSON when they look like it: `batch=7` matches
the number 7, `batch='"7"'` the string.

### One job, end to end

```console
$ pj-admin jobs inspect 48148

Job 48148 Details
--------------------------------------------------
State:           finished
Queue:           pjbench_e2e_3057f4d4
Job Class:       pyjobby.bench.BenchJob
Priority:        100
Created:         2026-07-27T01:34:31.958272+00:00
Updated:         2026-07-27T01:34:40.001308+00:00
Run After:       2026-07-27T01:34:31.958272+00:00
Run Count:       1
Error Count:     0
Worker:          optionality.local:79115

Arguments:
{
  "n": 17748
}

Result:
{
  "n": 17748
}
```

```console
$ pj-admin jobs history 48148

Job 48148 History
At                   Event     From     Epoch  Errors  Worker                Error
----------------------------------------------------------------------------------
2026-07-27T01:34:31  enqueued  -        -      -       -
2026-07-27T01:34:39  claimed   queued   1      0       optionality.local:79
2026-07-27T01:34:40  running   claimed  1      0       optionality.local:79
2026-07-27T01:34:40  finished  running  1      0       optionality.local:79

Total: 4 transition(s)
```

A job keeps **one row for life**: retries requeue the same id, and `history`
is where the per-attempt trail (and each attempt's error) lives. `Epoch` is
the fencing token, not an attempt counter.

```console
$ pj-admin jobs steps 48148
No step checkpoints for job 48148
```

`steps` shows the DXE checkpoints of a durable job — what completed, so
what a resume will skip. See [DXE.md](DXE.md).

### Acting on jobs

```console
$ pj-admin jobs requeue --help
Usage: pj-admin jobs requeue [OPTIONS] JOB_ID

  Requeue a terminal job for another run (also: RESUME an interrupted job)

  By default the job keeps its DXE step checkpoints, so completed steps fast-
  forward and execution resumes where it left off — use this to resume
  interrupted durable jobs. Pass --fresh to wipe the checkpoints and restart
  from step 1.

Options:
  --fresh  Wipe DXE checkpoints first: restart from step 1 instead of resuming
```

* `jobs retry ID...` — for jobs that did **not** succeed (`crashed` or
  `cancelled`). Refuses anything else.
* `jobs requeue ID` — also accepts a **finished** job. Running successful
  work again repeats its side effects, which is why it is a separate verb.
* `jobs cancel ID...` — queued and waiting jobs are cancelled immediately;
  a claimed or running job gets a cancellation *request* delivered to its
  worker, reported distinctly, because a job whose worker has died stays
  running with only the request recorded.
* `jobs set-priority ID PRIORITY` — re-prioritise a **queued or waiting**
  job (lower numbers are claimed first). Once a job is claimed its priority
  no longer decides anything, so those are refused; a priority above the
  deployment's worker ceiling is refused too, since no worker would claim it.
* `jobs delete ID` — permanent, prompts unless `-f/--force`.

The retry-versus-rerun distinction is expanded in
[OPERATIONS.md § Retry vs. re-run](OPERATIONS.md#retry-vs-re-run).

### Aggregates

```console
$ pj-admin jobs retry-stats

Retry Statistics (last 24h)
------------------------------------------------------------
No retried jobs found

$ pj-admin jobs timeout-stats

Timeout Statistics (last 24h)
------------------------------------------------------------
No timeout data found
```

Both take `-q/--queue`, `--since-hours` and `--json`. `retry-stats` reads
`jorb_history` — an attempt is a `running` event, so a job with more than
one was retried.

## `queues`

```console
$ pj-admin queues --help
Commands:
  clear   Clear (delete) jobs from a queue
  limits  Set (or show, with no options) a queue's concurrency/rate limits
  list    List all queues with their pause/limit controls
  pause   Pause a queue (workers stop claiming from it immediately)
  resume  Resume a paused queue
  show    Show one queue's controls and statistics
  stats   Show queue statistics
```

Controls are live — they are rows in `jorb_queue`, read by the claim
statement, so a change takes effect on the next claim attempt:

```console
$ pj-admin queues pause maintenance
Queue 'maintenance' paused

$ pj-admin queues limits maintenance --max-concurrency 8 --rate-limit 100 --rate-period 60
Queue 'maintenance' limits updated
Paused:              yes
Max concurrency:     8 (claimed+running cap; '-' = unlimited)
Rate limit:          100 start(s) per 60s ('-' = unlimited)

$ pj-admin queues show maintenance

Queue 'maintenance'
--------------------------------------------------
Paused:              yes
Max concurrency:     8 (claimed+running cap; '-' = unlimited)
Rate limit:          100 start(s) per 60s ('-' = unlimited)

Depths:
  queued       0
  claimed      0
  running      0
  waiting      0
  finished     0
  crashed      0
  cancelled    0
  total        0

$ pj-admin queues resume maintenance
Queue 'maintenance' resumed
```

`queues limits NAME --max-concurrency none` clears a limit. `queues list`
and `queues stats` are the fleet-wide views:

```console
$ pj-admin queues stats
Queue        Paused  Queued  Running  Waiting  Finished  Crashed  Total  Limits
--------------------------------------------------------------------------------------
maintenance  no      0       0        0        0         0        0      conc=8, rate=
```

`queues clear QUEUE` deletes jobs, optionally narrowed by `-s/--state` and
`--older-than-days`, and prompts unless `-f/--force`. It is a blunt
instrument for a test queue — routine cleanup is retention's job
(see [deployment-guide.md § Retention](deployment-guide.md#retention--on-by-default)).

## `workers`

```console
$ pj-admin workers list
ID  Host             PID    Queue            Status  Threads  Last Seen  Current Job
------------------------------------------------------------------------------------
1   optionality.loc  75674  pjbench_e2e_9c5  live    0/8      5s ago     -
2   optionality.loc  75675  pjbench_e2e_9c5  live    0/8      5s ago     -
3   optionality.loc  75673  pjbench_e2e_9c5  live    0/8      5s ago     -
4   optionality.loc  75676  pjbench_e2e_9c5  live    0/8      5s ago     -
```

This reads the `jorb_worker` registry: live workers plus recently
shut-down ones, each with the job it currently holds. Two columns carry the
important signal:

* **Status** — `live`, or **`not claiming`** for a worker that heartbeats
  perfectly and does no work, because abandoned job threads fill its pool:

  ```
  2   host-b    9910   heavy  not claiming  8/8      3s ago     -
  ```

* **Threads** — `abandoned/pool`. `8/8` *is* the refusing state; `7/8` is
  one timed-out job away from it and reads nothing like `0/8`.

Both are explained in
[OPERATIONS.md § Abandoned job threads](OPERATIONS.md#abandoned-job-threads-when-a-worker-stops-claiming-on-purpose).

```console
$ pj-admin workers stats

Worker Statistics
--------------------------------------------------
Live workers:      4
Stale workers:     0
Shut down:         0
Total registered:  4

Live Workers by Queue:
Queue                 Live Workers
----------------------------------
pjbench_e2e_67f5e7c6  4
```

## `dlq` — the dead letter queue

The DLQ is not a separate table or an error-count heuristic: it is exactly
`state = 'crashed'`, the terminal state a job reaches when its retries are
exhausted.

```console
$ pj-admin dlq list
Dead Letter Queue is empty!
```

`dlq list` takes `--limit` and `--json`. `dlq retry ID` requeues the same
row with a fresh error budget — the operator-driven re-run, as against
`jobs retry`, which is the ordinary one. Triage a dead-lettered job with
`jobs history ID` (the per-attempt errors) and `jobs steps ID` (where a
durable pipeline stopped).

<a id="schedule-management"></a>

## `schedule` — schedule management

```console
$ pj-admin schedule --help
Commands:
  add      Create new recurring schedule
  delete   Delete a recurring schedule
  disable  Disable an enabled schedule
  enable   Enable a disabled schedule
  history  Show schedule execution history
  list     List recurring schedules
  show     Show schedule details
  stats    Show execution statistics for all schedules
```

```console
$ pj-admin schedule add nightly-cleanup examples.jobs.example_jobs.BasicJob "0 2 * * *" \
    --queue maintenance --kwargs '{"message":"cleanup"}'
✓ Schedule created: nightly-cleanup (ID: 1)
  Next run: 2026-07-27 02:00:00+00:00
  Cron:     0 2 * * *
  Queue:    maintenance
```

`add` also takes `-p/--prio`, `--capability`, `--timezone`, `--jitter`,
`--max-concurrent`, `--backpressure`, `--circuit-breaker`, `--description`
and `--disabled`. The cron expression and timezone are validated at this
point, not at fire time.

```console
$ pj-admin schedule show nightly-cleanup

Schedule: nightly-cleanup
------------------------------------------------------------
ID:                    1
Enabled:               ✓ Yes
Description:           -

Schedule:
Cron Expression:       0 2 * * *
Timezone:              UTC
Next Run:              2026-07-27 02:00:00+00:00

Job Configuration:
Job Class:             examples.jobs.example_jobs.BasicJob
Queue:                 maintenance
Priority:              100
Capability:            -
Arguments:             {"message": "cleanup"}

Safety Features:
Max Concurrent Jobs:   1
Jitter (seconds):      0
Backpressure Threshold:1000
Circuit Breaker:       5 failures

Statistics:
Total Runs:            0
Successes:             0
Failures:              0
Skips:                 0
Consecutive Failures:  0
Last Run:              Never
Last Success:          Never
Last Failure:          Never
```

`schedule history NAME_OR_ID` takes `--result success|failure|skipped`,
`-l/--limit` and `--json`; `schedule stats` is the fleet view. Prefer
`disable` to `delete`: it keeps the execution log.

Everything a schedule *means* — cron syntax, jitter, backpressure, the
circuit breaker, timezone handling — is in
[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md). Nothing fires unless
`pj-scheduler` is running; `doctor` warns about schedules overdue by more
than five minutes for exactly that reason.

## `dag`

```console
$ pj-admin dag --help
Commands:
  list       List DAGs
  show       Show DAG details and job status
  visualize  Visualize DAG structure (ASCII art)
```

`list` takes `-l/--limit` and `--json`.

## `metrics`

```console
$ pj-admin metrics

System Metrics (last 24h)
--------------------------------------------------
Throughput:        0.35 jobs/s (completed)
Arrivals:          0.35 jobs/s (created)
Balance:           +0.00 jobs/s (keeping up)
Retry Pressure:    0.00 attempts/s
DLQ Growth:        0.0000 jobs/s
Finished:          30000
Crashed:           0
Cancelled:         0
Avg Duration:      0.00s
Avg Queue Wait:    7.00s
Max Queue Wait:    13.09s
Backlog:           0 claimable, oldest ready 0s
In Flight:         0 (0 stuck > 5.0m, oldest 0s)
NOTIFY Queue:      0.0% used
Dead Tuples:       79.8% of jorb

Storage:
  jorb              17.2MB total (10.6MB table + 6.5MB index), dead 79.8%
  jorb_history      35.3MB total (26.6MB table + 8.8MB index), dead 1.3%
  jorb_step         16.0KB total (8.0KB table + 8.0KB index), dead 0.0%

Jobs Created in Window, by State:
  finished     30000
```

Takes `-q/--queue`, `--since-hours` (default 24) and `--json`. Two kinds of
number live here and must not be confused: **rates** (`jobs/s`) are
measured over the window and comparable across window sizes; **levels**
(backlog, in flight, storage, NOTIFY usage) are instants.

* **Throughput vs Arrivals** is the only pair that can say "falling
  behind". Sustained arrivals above completions is the definition, and no
  single number expresses it.
* **Queue Wait vs Duration** are reported separately on purpose. Rising
  wait with flat duration is a capacity problem; rising duration is a code
  or dependency problem. A blended "how long did the job take" cannot tell
  those apart, so it is not reported. See
  [OPERATIONS.md § Reading the latency numbers](OPERATIONS.md#reading-the-latency-numbers).
* **Dead Tuples** is a survival question at rate — it answers "is
  autovacuum keeping up" and nothing else does. See
  [SCALE.md § Vacuum pressure](SCALE.md#4-vacuum-pressure).

## `pj-web` — the HTML admin

```bash
pj-web --config /etc/pyjobby/pyjobby.toml --host 127.0.0.1 --port 8081
```

It takes a config file with `-c`/`--config` (the same flag every pyjobby
daemon takes); it does not read `PYJOBBY_DSN`.
Defaults are `127.0.0.1:8081`. **There is no authentication**, and the API
can cancel, retry and delete jobs — bind it to localhost, or put an
authenticating proxy in front. `--host 0.0.0.0` exposes an unauthenticated
control plane.

Pages: `/`, `/jobs`, `/queues`, `/workers`, `/dlq`, `/schedules`.

JSON API:

| Method | Path |
|---|---|
| GET | `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/history`, `/api/jobs/{id}/steps` |
| POST | `/api/jobs/{id}/retry`, `/api/jobs/{id}/cancel` |
| DELETE | `/api/jobs/{id}` |
| GET | `/api/queues`, `/api/queues/{queue}/stats` |
| POST | `/api/queues/{queue}/pause`, `/api/queues/{queue}/resume` |
| GET | `/api/workers`, `/api/workers/stats` |
| GET | `/api/dlq` |
| POST | `/api/dlq/{id}/retry` |
| GET | `/api/metrics` |
| GET/POST | `/api/schedules`, `/api/schedules/{id}` |

`GET /metrics` (not `/api/metrics`) is the Prometheus scrape endpoint. The
gauges and counters it exposes:

```
pyjobby_jobs_by_state              pyjobby_jobs_enqueued_total
pyjobby_backlog_depth              pyjobby_queue_oldest_queued_seconds
pyjobby_queue_paused               pyjobby_jobs_inflight
pyjobby_jobs_stuck                 pyjobby_inflight_oldest_age_seconds
pyjobby_jobs_started_recent        pyjobby_jobs_terminal_recent
pyjobby_job_duration_seconds       pyjobby_throughput_jobs_per_second
pyjobby_arrival_jobs_per_second    pyjobby_retry_attempts_per_second
pyjobby_dlq_jobs_per_second        pyjobby_notify_queue_usage_ratio
pyjobby_workers_live               pyjobby_workers_not_claiming
pyjobby_worker_job_threads_abandoned_max
pyjobby_table_bytes                pyjobby_table_index_bytes
pyjobby_table_total_bytes          pyjobby_table_live_tuples
pyjobby_table_dead_tuples          pyjobby_table_dead_tuple_ratio
```

The three worth alerting on first are `pyjobby_notify_queue_usage_ratio`,
`pyjobby_queue_oldest_queued_seconds` (age, not depth) and
`pyjobby_workers_not_claiming`.

For the realtime feed — `pj-ws`, its protocol and its dashboard — see
[WEBSOCKET_DASHBOARD.md](WEBSOCKET_DASHBOARD.md). It is unauthenticated
too, and its actions include cancelling and re-prioritising jobs.

## `AdminAPI` — the Python API

Everything above is a thin shell over `pyjobby.admin_api.AdminAPI`, which
takes an asyncpg connection:

```python
import asyncpg
from pyjobby.admin_api import AdminAPI

conn = await asyncpg.connect(dsn)
api = AdminAPI(conn)

crashed = await api.list_jobs(state="crashed", limit=10)
for job in crashed:
    await api.retry_job(job["id"])  # {"job_id": ..., "status": "requeued"}
```

Method groups, all `async`:

* **Jobs** — `list_jobs`, `get_job`, `get_job_history`, `get_job_steps`,
  `retry_job`, `retry_jobs`, `requeue_job`, `cancel_job`, `cancel_jobs`,
  `update_job_priority`, `delete_job`, `delete_jobs`.
* **Queues** — `list_queues`, `queue_stats`, `clear_queue`,
  `list_queue_controls`, `get_queue_control`, `set_queue_control`,
  `pause_queue`, `resume_queue`.
* **Workers** — `list_workers`, `worker_stats`, `job_thread_stats`.
* **Health and metrics** — `get_metrics`, `backlog_stats`,
  `inflight_stats`, `storage_stats`, `notify_queue_usage`.
* **DLQ** — `list_dlq`, `retry_from_dlq`.
* **Schedules** — `list_schedules`, `get_schedule`, `create_schedule`,
  `update_schedule`, `delete_schedule`, `enable_schedule`,
  `disable_schedule`, `get_schedule_history`, `get_schedule_stats`.

Two shapes are worth stating because they are easy to assume wrong:

* `retry_job` and `retry_from_dlq` return the **same** `job_id` they were
  given. A retry requeues the row a job has had since it was enqueued;
  there is no new id to follow.
* `list_dlq()` is `state = 'crashed'`, ordered by `updated`. It is not
  filtered on an error count.

The docstrings in `pyjobby/admin_api.py` are the full reference, including
the exact keys each method returns.
