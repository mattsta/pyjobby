# Pyjobby Documentation

Every document in `docs/`, what it answers, and when to reach for it.
Nothing here is a summary of a document that does not exist: each entry
below is a file you can open.

## Where to start

| If you want to… | Read |
|---|---|
| understand how the platform is shaped | [ARCHITECTURE.md](ARCHITECTURE.md) |
| get it running in production | [deployment-guide.md](deployment-guide.md) |
| write your first job | [writing-jobs.md](writing-jobs.md) |
| submit jobs from your application | [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) |
| see whole applications, not snippets | [EXAMPLES.md](EXAMPLES.md) |
| model a workflow as a state machine | [STATECHARTS.md](STATECHARTS.md) |
| run and watch the fleet | [OPERATIONS.md](OPERATIONS.md) |
| find out why something is broken | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

## The documents

### Understanding it

1. **[ARCHITECTURE.md](ARCHITECTURE.md)** — the components, the life of a
   job, why claiming lives in the database, the notification model, and
   liveness/fencing/recovery.
2. **[DXE.md](DXE.md)** — the Durable Execution Engine: checkpointed
   `step()`, `transaction()`, durable `sleep()`, events and mailboxes, and
   the invariant that a completed step never runs twice.
3. **[SCALE.md](SCALE.md)** — every measured number, what breaks first at
   1M jobs/hour, and the design decisions on the write path that were
   rejected and why. All of it reproducible with `pj-bench`.

### Building on it

4. **[writing-jobs.md](writing-jobs.md)** — what goes *inside* one job:
   `task()`, sync vs async vs generator, which durable primitive to reach
   for, the determinism obligation, timeouts, retries, tags, and a
   checklist for a new job class.
5. **[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md)** — the enqueue API in full:
   `JobClient`, every `enqueue()` option, batches, pipelines,
   fan-out/fan-in, deadline keys, priority and the worker ceiling.
6. **[EXAMPLES.md](EXAMPLES.md)** — complete applications: accept-now/
   work-later, an ETL pipeline, the transactional outbox, a rate-limited
   third-party API, human-in-the-loop, batch import. Every complete example
   is executed against a real worker by `tests/test_examples_doc.py`.
7. **[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md)** — cron schedules:
   `pj-scheduler`, timezones and DST, and the five safety features
   (circuit breaker, max concurrent, backpressure, jitter, deadline keys).
8. **[STATECHARTS.md](STATECHARTS.md)** — durable state machines: declaring
   one with `StateMachineJob`, driving it with the `MachineHandle` client
   API, and running the queue they live on. A machine survives a crash,
   fences a stale worker, runs each action once, and can wait months — all
   on the existing schema.

### Running it

9. **[deployment-guide.md](deployment-guide.md)** — install, the database,
   configuration, the processes to run, systemd, containers, Kubernetes,
   exposure, backup/restore and how to verify a deployment.
10. **[OPERATIONS.md](OPERATIONS.md)** — the runbook: the process
    inventory, `pj-admin doctor`, the state machine, timeouts, abandoned job
    threads, live queue controls, priority and the worker ceiling,
    retention, and the failure playbooks.
11. **[ADMIN_TOOLS.md](ADMIN_TOOLS.md)** — the reference for *what exists*:
    every `pj-admin` command with real output, `pj-web`, and the
    `AdminAPI` Python interface.
12. **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — the symptom index. Start
    at `pj-admin doctor`, then jump to the entry for what you are seeing.
13. **[WEBSOCKET_DASHBOARD.md](WEBSOCKET_DASHBOARD.md)** — `pj-ws` and
    `frontend/live-dashboard.html`: live aggregates, per-job watches, and
    the demand-gated channels behind them.

### Changing it

14. **[TESTING.md](TESTING.md)** — running the suite against any database,
    the shared fixtures (`live_worker`, `wait_for_job_state`), and why
    coverage is a diagnostic rather than a target.
15. **[The schema itself](../pyjobby/sql/schema.sql)** — the canonical
    source, commented end to end, including the measurements behind the
    indexes and autovacuum settings.

Configuration is not a separate document: `sample.conf.py` in the
repository root is the annotated example, and
[deployment-guide.md § Configuration](deployment-guide.md#configuration)
covers which process reads what.

## Quick Reference

### Essential Files

- `pyjobby/pj.py` - the worker: claiming, execution, state transitions
- `pyjobby/client.py` - `JobClient`, the enqueue side
- `pyjobby/sql/schema.sql` - the canonical schema, shipped in the wheel
- `pyjobby/sql/migrations/` - the numbered migration files. `pj-admin db migrate` installs `schema.sql` on a fresh database (recording every migration as already contained in it) and applies unrecorded migrations to an existing one
- `pyjobby/migrations.py` - the runner, plus the required-shape manifest `pj-admin doctor` checks a database against
- `pyjobby/dxe.py` - Durable Execution Engine semantics and SQL
- `pyjobby/monitor.py` - the reaper (timeouts, dead-worker reclaim, retention)
- `sample.conf.py` - Example configuration

### Common Commands

```bash
# Install or upgrade the database schema (fresh install, or pending migrations)
pj-admin db migrate --config ./pyjobby.conf.py
pj-admin db status --config ./pyjobby.conf.py

# Is the platform healthy? (exits 1 on any FAIL, so it works as a CI gate)
pj-admin --dsn "$PYJOBBY_DSN" doctor

# Start workers: --workers is PER --queue, so this is 4 processes on `default`
pj --queue default --workers 4 --config ./pyjobby.conf.py

# Start the reaper: timeouts, dead-worker reclaim, retention. NOT optional.
pj-monitor --config ./pyjobby.conf.py

# Start the recurring (cron) schedule executor
pj-scheduler --config ./pyjobby.conf.py

# Start the web admin UI (localhost:8081, no auth)
pj-web --config ./pyjobby.conf.py

# Start the realtime websocket dashboard server (localhost:8082)
pj-ws --config ./pyjobby.conf.py

# View help
pj --help

# Check version
pj -v
```

### Job Submission Template

```python
from pyjobby import JobClient


async def submit_job(job_class: str, **kwargs):
    async with await JobClient.from_config("./pyjobby.conf.py") as client:
        return await client.enqueue(job_class, queue="default", **kwargs)
```

Enqueue through the client rather than with a hand-written `INSERT`: the
client validates what the database cannot reject on its own — a `priority`
above the worker fleet's ceiling, an unusable `on_timeout`, tag values that
are not filterable — and a raw insert is the usual way an unclaimable row
gets into the table. Full API:
[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md).

### Job Class Template

```python
from pyjobby.pj import Job


class MyJob(Job):
    def task(self, arg1: str, arg2: int):
        # Your job logic here
        result = do_something(arg1, arg2)
        return {"status": "success", "result": result}
```

## Architecture at a Glance

```
CLI (pj) → spawns --workers processes on EACH --queue named
           (multiprocessing), each registers in jorb_worker
    ↓
Worker sleeps on LISTEN jorb_enqueued (poll is the fallback)
    ↓
Claim: claim_jorb() — FOR UPDATE SKIP LOCKED, enforcing jorb_queue
       (paused / max_concurrency / rate_limit), stamping claimed_at
       and bumping run_epoch
    ↓
claimed → running (records `started`; timeouts key off this)
    ↓
Load job class (pydoc.locate + importlib.reload) and bind DXE checkpoints
    ↓
Execute task(**kwargs) — steps/sleeps/events/messages are durable
    ↓
finished │ queued (same-row retry with backoff) │ crashed (terminal DLQ)
         │ cancelled (operator request, delivered by NOTIFY)
    ↓
Wake dependents (waitfor_job / waitfor_group); every transition lands in
jorb_history; pj-monitor reaps timeouts and jobs of dead workers
```

## Key Features

- ✅ **Focused**: a small worker loop; the platform is explicit and readable
- ✅ **Reliable**: PostgreSQL-backed persistence
- ✅ **Type-safe**: Full mypy strict compliance
- ✅ **Powerful**: durable execution (checkpointed steps, durable sleep, events, messaging), dependencies, priorities, cron
- ✅ **Flexible**: Sync/async jobs, web integration
- ✅ **Observable**: full transition history, DXE step checkpoints, Prometheus `/metrics`
- ✅ **Scalable**: Horizontal scaling via database
- ✅ **Self-Healing**: registry-heartbeat dead-worker reclaim, same-row retries, epoch fencing
- ✅ **Fault-Tolerant**: Timeout protection and max retry limits
- ✅ **Production-Ready**: Enhanced error handling and monitoring

## Database Schema Summary

| Column            | Purpose                                                        |
| ----------------- | -------------------------------------------------------------- |
| `id`              | Primary key                                                    |
| `queue`           | Route jobs to specific workers                                 |
| `state`           | Current status (queued → claimed → running → finished/crashed) |
| `prio`            | Priority as a finishing position: the **smallest** number is claimed first, and each worker claims only `prio <=` its own ceiling (`pj --max-prio`, default 1000) |
| `run_after`       | Minimum start time                                             |
| `job_class`       | Python class path                                              |
| `kwargs`          | Arguments (JSONB)                                              |
| `result`          | Return value (JSONB)                                           |
| `error_backtrace` | Stack trace on failure                                         |
| `waitfor_job`     | Dependency on specific job                                     |
| `waitfor_group`   | Dependency on job group                                        |
| `run_group`       | Group identifier for this job                                  |
| `deadline_key`    | Unique key for singleton scheduling                            |

## Example Workflows

### Simple Job

```python
# 1. Create job class
class SendEmail(Job):
    def task(self, to: str, subject: str):
        send_email(to, subject)
        return {"sent": True}


# 2. Start workers (2 processes, both on `email`)
# $ pj --queue email --workers 2

# 3. Submit job
await client.enqueue(
    "job.email.SendEmail",
    queue="email",
    to="user@example.com",
    subject="Hello",
)
```

### Job Pipeline with Dependencies

```python
# Many units in parallel, then one job that runs when ALL of them finish.
items = [{"file": f} for f in ("a.jpg", "b.jpg", "c.jpg")]
job_ids, group_id = await client.create_fan_out("job.Thumbnail", items)

await client.enqueue("job.Aggregate", waitfor_group=group_id, expected=len(items))

# Execution: every Thumbnail runs in parallel
#            → when ALL of them finish, Aggregate runs
```

More of these, executed against a real worker on every test run:
[EXAMPLES.md](EXAMPLES.md).

## Performance Characteristics

Measured, not estimated, and all of it in one place:
**[SCALE.md](SCALE.md)** — what one job costs, what breaks first, sizing
per million jobs, and the reproduction commands (`pj-bench`) for every
figure. It is the only set of numbers in this documentation, deliberately:
a second copy here would drift away from the benchmarks that produce them.

The two facts worth carrying out of it: enqueue is **not** the bottleneck
(measured throughput has roughly two orders of magnitude of headroom over
1M jobs/hour), and what breaks first is anything that has to read or retain
the *accumulated* table — which is what retention exists for.

## Design Trade-offs

### Chosen: Simplicity over Raw Performance

**What we sacrificed**:

- Every state change writes to WAL
- FOR UPDATE SKIP LOCKED (not advisory locks)

Note that polling is *not* on that list: enqueue fires a `NOTIFY` and an
idle worker wakes immediately. `--check-interval` (5 s, jittered) is the
fallback for a missed wakeup, not the normal path.

**What we gained**:

- No pub/sub coordination outside the database
- One place to look when something is wrong: the tables
- Predictable behavior

### Chosen: PostgreSQL over Message Broker

**What we sacrificed**:

- Peak throughput vs Redis/RabbitMQ

**What we gained**:

- One dependency instead of two
- Durable by default
- Observable with SQL
- ACID guarantees, including enqueue-inside-your-own-transaction

## When to Use Pyjobby

**Good fit**:

- Applications already using PostgreSQL
- Need for durable job state, resumable work, or an audit trail
- Work that must not be lost, and must not run twice
- Mixed sync/async workloads

**Not ideal for**:

- Ultra-high throughput (millions of tiny jobs/second) — see
  [SCALE.md](SCALE.md) for where the real ceilings are
- Sub-millisecond dispatch latency
- Complex workflow orchestration (use Airflow/Prefect)

## Contributing

When adding features, consider:

1. **Is this feature essential?** (Avoid feature creep)
2. **Can it be implemented in user code?** (Prefer extensibility)
3. **Does it maintain simplicity?** (Code golf is not the goal, clarity is)
4. **Is it proved by a test against a real database?** See
   [TESTING.md](TESTING.md).

## Support

- **GitHub Issues**: https://github.com/mattsta/pyjobby/issues
- **Discussions**: For questions and ideas
- **Source Code**: https://github.com/mattsta/pyjobby

## License

See LICENSE file in repository root.

## Credits

Created by Matt Stancliff (@mattsta) in January 2021.

Inspired by:

- Que (Ruby) - PostgreSQL-backed job queue
- RQ (Python) - Simple Redis queue
- Celery (Python) - Distributed task queue
- Good Queue (Go) - PostgreSQL queue implementation

## Version

The version is declared once, in `pyproject.toml`, and the installed one is
what `pj -v` prints:

```bash
pj -v
```

There is no hand-maintained changelog here — `git log` is the record, and a
second copy of it in this file would only tell you what was true when
someone last remembered to update it.

## Next Steps

1. Read [ARCHITECTURE.md](ARCHITECTURE.md) for system design
2. Follow [deployment-guide.md](deployment-guide.md) to get it running
3. Study [writing-jobs.md](writing-jobs.md) to write your first job
4. Review [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) for job submission
5. Work through [EXAMPLES.md](EXAMPLES.md) for whole applications
6. Check [DXE.md](DXE.md) for durable execution: checkpoints, fencing, exactly-once
7. Keep [ADMIN_TOOLS.md](ADMIN_TOOLS.md) and
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md) open once it is running

Happy job processing! 🚀
