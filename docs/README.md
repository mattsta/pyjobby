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

See [PHASE1_IMPROVEMENTS.md](../PHASE1_IMPROVEMENTS.md) for complete details.

## Table of Contents

### Getting Started

1. **[Architecture Overview](architecture.md)** - Complete system design, components, and data flow
   - System architecture and design philosophy
   - Component breakdown and relationships
   - Data flow examples
   - Scaling and distribution strategies
   - Advanced features overview

2. **[Deployment Guide](deployment-guide.md)** - Production deployment and operations
   - Quick start for development
   - Production deployment checklist
   - Docker and Kubernetes configurations
   - Systemd service setup
   - Monitoring and logging
   - Security best practices
   - Disaster recovery

### Core Components

3. **[JobSystem Class](jobsystem.md)** - The orchestrator that runs on each worker
   - Class definition and initialization
   - Key methods and their usage
   - Database operations and prepared statements
   - Worker-local caching
   - Web server integration
   - Real-world usage examples
   - Performance tuning

4. **[Job Base Class](job-class.md)** - How to write job workers
   - Class interface and attributes
   - Core methods (task, run, reschedule)
   - Job lifecycle and state transitions
   - Execution modes (sync, async, generator)
   - Real-world job examples
   - Advanced patterns
   - Best practices

5. **[Database Schema](database-schema.md)** - PostgreSQL table structure
   - Complete column reference
   - State machine and transitions
   - Indexes and their purposes
   - Job submission patterns
   - Monitoring and maintenance queries
   - Schema migration examples

### Features

6. **Configuration System** *(See sample.conf.py)* - How to configure pyjobby
   - Database connection parameters
   - Web server configuration
   - Custom application settings
   - Environment-specific configs

7. **Job Dependencies** *(Covered in architecture.md and job-class.md)* - waitfor_job and waitfor_group
   - Single job dependencies
   - Group dependencies (fan-out/fan-in)
   - Complex workflow examples
   - Best practices

8. **Web Server Integration** *(Covered in jobsystem.md)* - Direct HTTP job invocation
   - Configuration and setup
   - Job web() method
   - Load balancing strategies
   - Security considerations

9. **Retry and Backoff** *(Covered in job-class.md)* - Automatic error handling
   - Exponential backoff algorithm
   - Custom retry logic
   - Manual rescheduling

### Operations

10. **Best Practices** - Production-ready patterns
    - Idempotent jobs
    - Resource caching
    - Error handling
    - Security
    - Performance optimization

11. **Troubleshooting** - Common issues and solutions
    - Worker not claiming jobs
    - Jobs stuck in claimed/running state
    - High error rates
    - Database performance issues
    - Memory leaks

## Quick Reference

### Essential Files

- `pyjobby/pj.py` - Core job system (783 lines)
- `priv/schema.py` - SQLAlchemy schema definition
- `priv/schema.sql` - PostgreSQL schema dump
- `sample.conf.py` - Example configuration

### Common Commands

```bash
# Start workers
pj --queue default --workers 4 --config ./pyjobby.conf.py

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
    job_id = await conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue)
        VALUES ($1, $2, $3)
        RETURNING id
    """, job_class, orjson.dumps(kwargs), "default")
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
CLI (pj) → Spawns Workers (multiprocessing)
    ↓
Workers Poll Database (every 5-6s)
    ↓
Claim Job (FOR UPDATE SKIP LOCKED)
    ↓
Load Job Class (pydoc.locate + importlib.reload)
    ↓
Execute task(**kwargs)
    ↓
Mark Finished or Crashed
    ↓
Trigger Dependent Jobs
```

## Key Features

- ✅ **Simple**: <1000 lines in one file
- ✅ **Reliable**: PostgreSQL-backed persistence
- ✅ **Type-safe**: Full mypy strict compliance
- ✅ **Powerful**: Dependencies, priorities, scheduling, retries
- ✅ **Flexible**: Sync/async jobs, web integration
- ✅ **Observable**: Complete audit trail in database
- ✅ **Scalable**: Horizontal scaling via database
- ✅ **Self-Healing**: Automatic crash recovery and retry management
- ✅ **Fault-Tolerant**: Timeout protection and max retry limits
- ✅ **Production-Ready**: Enhanced error handling and monitoring

## Database Schema Summary

| Column | Purpose |
|--------|---------|
| `id` | Primary key |
| `queue` | Route jobs to specific workers |
| `state` | Current status (queued → claimed → running → finished/crashed) |
| `prio` | Priority (lower = higher priority) |
| `run_after` | Minimum start time |
| `job_class` | Python class path |
| `kwargs` | Arguments (JSONB) |
| `result` | Return value (JSONB) |
| `error_backtrace` | Stack trace on failure |
| `waitfor_job` | Dependency on specific job |
| `waitfor_group` | Dependency on job group |
| `run_group` | Group identifier for this job |
| `deadline_key` | Unique key for singleton scheduling |

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
for job in ['Hash', 'Thumbnail', 'EXIF']:
    await db.execute("""
        INSERT INTO jorb (job_class, kwargs, run_group)
        VALUES ($1, $2, $3)
    """, f'job.{job}', '{"file": "/tmp/upload.jpg"}', group_id)

# Create aggregator (waits for all 3 to finish)
await db.execute("""
    INSERT INTO jorb (job_class, kwargs, state, waitfor_group)
    VALUES ($1, $2, $3, $4)
""", 'job.Aggregate', '{}', 'waiting', group_id)

# Execution: Hash, Thumbnail, EXIF run in parallel
#            → When ALL finish, Aggregate runs
```

## Performance Characteristics

| Metric | Typical Value |
|--------|---------------|
| Job claiming latency | <1ms |
| Polling interval | 5-6 seconds |
| Throughput (small jobs) | 100-500 jobs/sec per worker |
| Throughput (large jobs) | Limited by job duration |
| Database bottleneck | ~1000 jobs/sec aggregate |

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

1. Read [Architecture Overview](architecture.md) for system design
2. Follow [Deployment Guide](deployment-guide.md) to get started
3. Study [Job Class](job-class.md) to write your first job
4. Review [Database Schema](database-schema.md) for job submission
5. Check [JobSystem](jobsystem.md) for advanced features

Happy job processing! 🚀
