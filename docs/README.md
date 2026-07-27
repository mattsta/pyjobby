# Pyjobby Documentation

Comprehensive documentation for the pyjobby PostgreSQL-backed job queue system.

## What's New

🎉 **Phase 1 Improvements** - Self-Healing and Fault Tolerance

Pyjobby now includes critical production-ready improvements:

- ✅ **Fixed Retry Mechanism** - Jobs now retry correctly (critical bug fix)
- ✅ **Worker Crash Recovery** - Automatic recovery of abandoned jobs on startup
- ✅ **Job Timeout Protection** - Configurable per-job and system-wide timeouts
- ✅ **Max Retry Limits** - Prevent infinite retry loops with permanent failure detection
- ✅ **Enhanced Error Handling** - Detailed logging and monitoring support
- ✅ **Recurring Scheduler** - Cron-based schedules executed by `pj-scheduler` (see [RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md))
- ✅ **Realtime Websocket Dashboard** - Live event stream via `pj-ws` (see [WEBSOCKET_DASHBOARD.md](WEBSOCKET_DASHBOARD.md))
- ✅ **One-Step Schema Install** - `pj-admin db migrate` installs the base schema and all migrations idempotently

## Table of Contents

### Getting Started

1. **[Architecture](ARCHITECTURE.md)** - Components, the life of a job, and why the system is shaped this way
   - System architecture and design philosophy
   - Component breakdown and relationships
   - Data flow examples
   - Scaling and distribution strategies
   - Advanced features overview

2. **[Operations Runbook](OPERATIONS.md)** - What runs, how to check it, what to do when it breaks
   - Process inventory and start commands
   - Health checking (`pj-admin doctor`, `/metrics`)
   - The job state machine (one row for life, epoch fencing, crashed = DLQ)
   - Live queue controls
   - Failure playbooks (dead host, hung job, flooding queue, DLQ triage)

3. **[Testing Guide](TESTING.md)** - Running the suite, shared fixtures, and why coverage is a diagnostic
   - How to run tests and point them at any database
   - Reusable fixtures (`live_worker`, `wait_for_job_state`, shared job classes)
   - Coverage baseline and the anti-goal (evidence from this repo's own history)

4. **[Deployment Guide](deployment-guide.md)** - Production deployment and operations
   - Quick start for development
   - Production deployment checklist
   - Docker and Kubernetes configurations
   - Systemd service setup
   - Monitoring and logging
   - Security best practices
   - Disaster recovery

### Core Components

5. **[Operations](OPERATIONS.md)** - Running the fleet: queue controls, retention, timeouts, playbooks
   - Class definition and initialization
   - Key methods and their usage
   - Database operations and prepared statements
   - Worker-local caching
   - Web server integration
   - Real-world usage examples
   - Performance tuning

6. **[Writing Jobs](writing-jobs.md)** - Which durable primitive to reach for, and what each one guarantees
   - Class interface and attributes
   - Core methods (task, run, reschedule)
   - Job lifecycle and state transitions
   - Execution modes (sync, async, generator)
   - Real-world job examples
   - Advanced patterns
   - Best practices

7. **[The schema itself](../pyjobby/sql/schema.sql)** - The canonical source, commented end to end; see also [DXE.md](DXE.md) and [SCALE.md](SCALE.md)
   - Complete column reference
   - State machine and transitions
   - Indexes and their purposes
   - Job submission patterns
   - Monitoring and maintenance queries
   - Schema migration examples

### Features

8. **Configuration System** _(See sample.conf.py)_ - How to configure pyjobby
   - Database connection parameters
   - Web server configuration
   - Custom application settings
   - Environment-specific configs

9. **Job Dependencies** _(Covered in [ARCHITECTURE.md](ARCHITECTURE.md))_ - waitfor_job and waitfor_group
   - Single job dependencies
   - Group dependencies (fan-out/fan-in)
   - Complex workflow examples
   - Best practices

10. **Web Server Integration** _(`web_listen` in the worker config)_ - Direct HTTP job invocation
   - Configuration and setup
   - Job web() method
   - Load balancing strategies
   - Security considerations

11. **Retry and Backoff** _(Covered in [writing-jobs.md](writing-jobs.md))_ - Automatic error handling
   - Exponential backoff algorithm
   - Custom retry logic
   - Manual rescheduling

### Operations

12. **Best Practices** - Production-ready patterns
    - Idempotent jobs
    - Resource caching
    - Error handling
    - Security
    - Performance optimization

13. **Troubleshooting** - Common issues and solutions
    - Worker not claiming jobs
    - Jobs stuck in claimed/running state
    - High error rates
    - Database performance issues
    - Memory leaks

## Quick Reference

### Essential Files

- `pyjobby/pj.py` - Core job system
- `pyjobby/sql/schema.sql` - the canonical schema v1 (shipped in the wheel)
- `pyjobby/sql/migrations/` - future incremental migrations (v1 is the baseline; `pj-admin db migrate` installs and tracks both)
- `pyjobby/dxe.py` - Durable Execution Engine semantics and SQL
- `pyjobby/monitor.py` - the reaper (timeouts, dead-worker reclaim)
- `sample.conf.py` - Example configuration

### Common Commands

```bash
# Install/upgrade the database schema (base schema + all migrations)
pj-admin db migrate --config ./pyjobby.conf.py
pj-admin db status --config ./pyjobby.conf.py

# Start workers
pj --queue default --workers 4 --config ./pyjobby.conf.py

# Start the recurring (cron) schedule executor
pj-scheduler --config ./pyjobby.conf.py

# Start the web admin UI (localhost:8081, no auth)
pj-web ./pyjobby.conf.py

# Start the realtime websocket dashboard server (localhost:8082)
pj-ws ./pyjobby.conf.py

# View help
pj --help

# Check version
pj -v
```

### Job Submission Template

```python
import asyncpg
import orjson


async def submit_job(job_class: str, kwargs: dict):
    conn = await asyncpg.connect(**db_params)
    job_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue)
        VALUES ($1, $2, $3)
        RETURNING id
    """,
        job_class,
        orjson.dumps(kwargs),
        "default",
    )
    return job_id
```

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
CLI (pj) → spawns workers (multiprocessing), each registers in jorb_worker
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
| `prio`            | Priority (lower = higher priority)                             |
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


# 2. Start workers
# $ pj --queue email --workers 2

# 3. Submit job
await db.execute("""
    INSERT INTO jorb (job_class, kwargs, queue)
    VALUES ('job.email.SendEmail',
            '{"to": "user@example.com", "subject": "Hello"}',
            'email')
""")
```

### Job Pipeline with Dependencies

```python
# 1. Parallel jobs with group dependency
group_id = secrets.randbits(63)

# Create 3 parallel jobs (all in same group)
for job in ["Hash", "Thumbnail", "EXIF"]:
    await db.execute(
        """
        INSERT INTO jorb (job_class, kwargs, run_group)
        VALUES ($1, $2, $3)
    """,
        f"job.{job}",
        '{"file": "/tmp/upload.jpg"}',
        group_id,
    )

# Create aggregator (waits for all 3 to finish)
await db.execute(
    """
    INSERT INTO jorb (job_class, kwargs, state, waitfor_group)
    VALUES ($1, $2, $3, $4)
""",
    "job.Aggregate",
    "{}",
    "waiting",
    group_id,
)

# Execution: Hash, Thumbnail, EXIF run in parallel
#            → When ALL finish, Aggregate runs
```

## Performance Characteristics

| Metric                  | Typical Value               |
| ----------------------- | --------------------------- |
| Job claiming latency    | <1ms                        |
| Polling interval        | 5-6 seconds                 |
| Throughput (small jobs) | 100-500 jobs/sec per worker |
| Throughput (large jobs) | Limited by job duration     |
| Database bottleneck     | ~1000 jobs/sec aggregate    |

## Design Trade-offs

### Chosen: Simplicity over Raw Performance

**What we sacrificed**:

- 5-6 second polling (not instant job start)
- Every state change writes to WAL
- FOR UPDATE SKIP LOCKED (not advisory locks)

**What we gained**:

- <1000 lines of code
- No complex pub/sub coordination
- Easy to understand and debug
- Predictable behavior

### Chosen: PostgreSQL over Message Broker

**What we sacrificed**:

- Peak throughput vs Redis/RabbitMQ
- Real-time job execution

**What we gained**:

- One dependency instead of two
- Durable by default
- Observable with SQL
- ACID guarantees

## When to Use Pyjobby

**Good fit**:

- Applications already using PostgreSQL
- Job volumes <1000/second
- Need for durable job state
- Teams valuing simplicity
- Mixed sync/async workloads

**Not ideal for**:

- Ultra-high throughput (millions of tiny jobs/second)
- Real-time requirements (<1 second latency)
- Complex workflow orchestration (use Airflow/Prefect)

## Contributing

Pyjobby aims to stay under 1,000 lines in `pyjobby/pj.py`. When adding features, consider:

1. **Is this feature essential?** (Avoid feature creep)
2. **Can it be implemented in user code?** (Prefer extensibility)
3. **Does it maintain simplicity?** (Code golf is not the goal, clarity is)

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

## Version History

- **v1.1.0** (2025-01-XX): Phase 1 - Self-Healing and Fault Tolerance
  - **CRITICAL FIX**: Retry mechanism now works correctly (jobs were stuck in 'crashed')
  - Worker crash recovery - automatic requeue of abandoned jobs on startup
  - Job timeout protection with configurable per-job and system-wide timeouts
  - Max retry limits to prevent infinite retry loops
  - Enhanced error handling with detailed logging
  - Complete audit trail via separate retry jobs
  - New configuration options: `max_retries`, `default_timeout`, `enable_recovery`
  - 100% backward compatible - existing jobs work without modification

- **v1.0.0** (2025-01-XX): Initial release
  - Core job system (<1000 lines)
  - PostgreSQL-backed persistence
  - Multiprocessing workers
  - Dependencies (waitfor_job, waitfor_group)
  - Priority queues
  - Automatic retry with backoff
  - Web server integration
  - Type-safe (mypy strict)

## Next Steps

1. Read [Architecture](ARCHITECTURE.md) for system design
2. Follow [Deployment Guide](deployment-guide.md) to get started
3. Study [Writing Jobs](writing-jobs.md) to write your first job
4. Review [Client Library](CLIENT_LIBRARY.md) for job submission
5. Check [DXE.md](DXE.md) for durable execution: checkpoints, fencing, exactly-once

Happy job processing! 🚀
