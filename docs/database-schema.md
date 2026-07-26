# Database Schema Documentation

## Overview

Pyjobby uses a single PostgreSQL table (`jorb`) to store all job state. The schema is designed for:

- **Performance**: Partial indexes for fast job selection
- **Atomicity**: Row-level locking prevents conflicts
- **Observability**: Complete audit trail of all state transitions
- **Dependencies**: Support for complex job workflows

Schema location: `priv/schema.py` (SQLAlchemy) and `priv/schema.sql` (raw SQL)

## The `jorb` Table

### Complete Column Reference

| Column            | Type      | Nullable | Default   | Purpose                                     |
| ----------------- | --------- | -------- | --------- | ------------------------------------------- |
| `id`              | BIGINT    | NO       | AUTO      | Primary key, auto-increment                 |
| `queue`           | TEXT      | NO       | 'default' | Queue identifier for routing jobs           |
| `capability`      | TEXT      | YES      | NULL      | Required worker capability (or ANY if NULL) |
| `prio`            | INTEGER   | NO       | 100       | Priority (lower = higher priority)          |
| `run_after`       | TIMESTAMP | NO       | NOW()     | Minimum start time                          |
| `deadline_key`    | TEXT      | YES      | NULL      | Unique key for singleton future jobs        |
| `state`           | ENUM      | NO       | 'queued'  | Current job state (see state machine below) |
| `run_count`       | INTEGER   | NO       | 0         | Number of times job has been claimed        |
| `job_class`       | TEXT      | NO       | -         | Full Python path to job class               |
| `kwargs`          | JSONB     | NO       | -         | Arguments for task(\*\*kwargs)              |
| `uid`             | INTEGER   | YES      | NULL      | User ID (for multi-tenancy)                 |
| `run_group`       | BIGINT    | YES      | NULL      | Group ID for parallel jobs                  |
| `waitfor_group`   | BIGINT    | YES      | NULL      | Wait for this group to finish               |
| `waitfor_job`     | BIGINT    | YES      | NULL      | Wait for this job ID to finish              |
| `admin_data`      | JSONB     | YES      | NULL      | Additional metadata/tags                    |
| `result`          | JSONB     | YES      | NULL      | Result from successful execution            |
| `error_message`   | TEXT      | YES      | NULL      | Most recent error message                   |
| `error_backtrace` | TEXT      | YES      | NULL      | Full stack trace from last error            |
| `error_count`     | INTEGER   | NO       | 0         | Number of failures                          |
| `worker_pid`      | INTEGER   | YES      | NULL      | Process ID of last worker                   |
| `worker_host`     | TEXT      | YES      | NULL      | Hostname of last worker                     |
| `created`         | TIMESTAMP | NO       | NOW()     | When job was created                        |
| `updated`         | TIMESTAMP | NO       | NOW()     | When job was last modified                  |

### State Machine

```sql
CREATE TYPE jorb_state AS ENUM (
    'waiting',      -- Waiting for dependency
    'queued',       -- Ready to run
    'claimed',      -- Claimed by worker
    'running',      -- Currently executing
    'heartbeat',    -- (Reserved for future use)
    'crashed',      -- Failed with error
    'finished'      -- Completed successfully
);
```

**State Transitions**:

```
waiting ───────────────────┐
  │                        │
  │ (dependency satisfied) │
  ▼                        │
queued ◄───────────────────┘
  │
  │ (worker claims)
  ▼
claimed
  │
  │ (execution starts)
  ▼
running
  │
  ├──── (success) ────▶ finished
  │
  └──── (error) ──────▶ crashed
                          │
                          │ (reschedule)
                          ▼
                        queued (with future run_after)
```

## Indexes

### 1. `jorb_poll_idx` (Job Selection)

**Purpose**: Optimize worker job claiming

```sql
CREATE INDEX jorb_poll_idx ON jorb (queue, capability, prio, run_after)
WHERE state = 'queued' OR state = 'crashed';
```

**Columns**:

- `queue`: Exact match on queue name
- `capability`: Match worker capabilities
- `prio`: Order by priority (ASC)
- `run_after`: Filter jobs eligible to run

**Why Partial**: Only indexes jobs that are eligible to be claimed. Completed jobs (finished/crashed) are excluded, dramatically reducing index size and scan time.

**Query Pattern**:

```sql
SELECT * FROM jorb
WHERE queue = 'default'
  AND (capability = ANY(ARRAY['gpu', 'host:web-1']) OR capability IS NULL)
  AND prio <= 1000
  AND run_after <= NOW()
  AND state = 'queued'
ORDER BY prio, run_after
LIMIT 1;
```

**Performance**: Sub-millisecond even with millions of completed jobs.

### 2. `jorb_deadline_noconflict_idx` (Unique Deadline Keys)

**Purpose**: Prevent duplicate scheduled jobs

```sql
CREATE UNIQUE INDEX jorb_deadline_noconflict_idx ON jorb (deadline_key, queue)
WHERE state = 'queued' AND deadline_key IS NOT NULL;
```

**Uniqueness Constraint**: `(deadline_key, queue)` must be unique for all queued jobs.

**Lifecycle**:

- Job A inserted with `deadline_key='billing:123:2025-11-18'`, `state='queued'` → SUCCESS
- Job B inserted with same deadline_key → **FAILS** (unique violation)
- Job A transitions to `state='finished'` → Index entry removed
- Job C inserted with same deadline_key → SUCCESS (previous instance finished)

**Use Case**:

```python
# User uploads multiple files, but only schedule one billing update
for file in uploaded_files:
    try:
        await db.execute(
            """
            INSERT INTO jorb (job_class, kwargs, deadline_key, run_after, queue)
            VALUES ($1, $2, $3, $4, $5)
        """,
            "job.billing.Update",
            '{"user_id": 123}',
            "billing:123:2025-11-18",  # Same key
            "2025-11-18 23:59:00",
            "default",
        )
    except UniqueViolationError:
        pass  # Already scheduled, ignore
```

### 3. `jorb_run_group_idx` (Group Tracking)

**Purpose**: Efficiently find all jobs in a group

```sql
CREATE INDEX jorb_run_group_idx ON jorb (run_group)
WHERE run_group IS NOT NULL;
```

**Query Pattern**:

```sql
-- Check if all jobs in group are finished
SELECT COUNT(*) FROM jorb
WHERE run_group = 123456789
  AND state != 'finished';

-- Get all jobs in group
SELECT * FROM jorb
WHERE run_group = 123456789;
```

### 4. `jorb_waitfor_group_idx` (Group Dependencies)

**Purpose**: Find jobs waiting for a group

```sql
CREATE INDEX jorb_waitfor_group_idx ON jorb (waitfor_group)
WHERE waitfor_group IS NOT NULL AND state = 'waiting';
```

**Query Pattern**:

```sql
-- Activate jobs waiting for group 123456789
UPDATE jorb
SET state = 'queued'
WHERE waitfor_group = 123456789
  AND state = 'waiting'
  AND 0 = (
      SELECT COUNT(*) FROM jorb
      WHERE run_group = 123456789 AND state != 'finished'
  );
```

### 5. `jorb_waitfor_job_idx` (Job Dependencies)

**Purpose**: Find jobs waiting for a specific job

```sql
CREATE INDEX jorb_waitfor_job_idx ON jorb (waitfor_job)
WHERE waitfor_job IS NOT NULL AND state = 'waiting';
```

**Query Pattern**:

```sql
-- Activate jobs waiting for job 1001
UPDATE jorb
SET state = 'queued'
WHERE waitfor_job = 1001
  AND state = 'waiting'
  AND 0 = (
      SELECT COUNT(*) FROM jorb
      WHERE id = 1001 AND state != 'finished'
  );
```

### 6. `jorb_uid_idx` (User Tracking)

**Purpose**: Query jobs by user ID

```sql
CREATE INDEX jorb_uid_idx ON jorb (uid);
```

**Query Pattern**:

```sql
-- Get all jobs for user 12345
SELECT * FROM jorb WHERE uid = 12345;

-- User's recent jobs
SELECT * FROM jorb
WHERE uid = 12345
ORDER BY created DESC
LIMIT 100;
```

## Database Schema Setup

### Method 1: Raw SQL (Recommended for Production)

```bash
# Load schema from dump
psql -U pyjobby -d myapp -f priv/schema.sql

# Or create manually
psql -U pyjobby -d myapp <<'EOF'
CREATE TYPE jorb_state AS ENUM (
    'waiting', 'queued', 'claimed', 'running',
    'heartbeat', 'crashed', 'finished'
);

CREATE TABLE jorb (
    id BIGSERIAL PRIMARY KEY,
    queue TEXT NOT NULL DEFAULT 'default',
    capability TEXT,
    prio INTEGER NOT NULL DEFAULT 100,
    run_after TIMESTAMP NOT NULL DEFAULT TIMEZONE('utc', CURRENT_TIMESTAMP),
    deadline_key TEXT,
    state jorb_state NOT NULL DEFAULT 'queued',
    run_count INTEGER NOT NULL DEFAULT 0,
    job_class TEXT NOT NULL,
    kwargs JSONB NOT NULL,
    uid INTEGER,
    run_group BIGINT,
    waitfor_group BIGINT,
    waitfor_job BIGINT REFERENCES jorb(id),
    admin_data JSONB,
    result JSONB,
    error_message TEXT,
    error_backtrace TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    worker_host TEXT,
    created TIMESTAMP NOT NULL DEFAULT TIMEZONE('utc', CURRENT_TIMESTAMP),
    updated TIMESTAMP NOT NULL DEFAULT TIMEZONE('utc', CURRENT_TIMESTAMP)
);

-- Create indexes
CREATE INDEX jorb_poll_idx ON jorb (queue, capability, prio, run_after)
WHERE state = 'queued' OR state = 'crashed';

CREATE UNIQUE INDEX jorb_deadline_noconflict_idx ON jorb (deadline_key, queue)
WHERE state = 'queued' AND deadline_key IS NOT NULL;

CREATE INDEX jorb_run_group_idx ON jorb (run_group)
WHERE run_group IS NOT NULL;

CREATE INDEX jorb_waitfor_group_idx ON jorb (waitfor_group)
WHERE waitfor_group IS NOT NULL AND state = 'waiting';

CREATE INDEX jorb_waitfor_job_idx ON jorb (waitfor_job)
WHERE waitfor_job IS NOT NULL AND state = 'waiting';

CREATE INDEX jorb_uid_idx ON jorb (uid);
EOF
```

### Method 2: SQLAlchemy (For Integrated Applications)

```python
# Use the schema from priv/schema.py
from priv.schema import Jorb, JorbState, Base
from sqlalchemy import create_engine

engine = create_engine("postgresql://user:pass@localhost/myapp")
Base.metadata.create_all(engine)
```

## Job Submission Patterns

### Basic Job Submission

```python
import asyncpg
import orjson


async def submit_job(job_class: str, kwargs: dict, **options):
    conn = await asyncpg.connect(**db_params)

    job_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, queue, prio, uid, capability)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
    """,
        job_class,
        orjson.dumps(kwargs),
        options.get("queue", "default"),
        options.get("prio", 0),
        options.get("uid"),
        options.get("capability"),
    )

    return job_id


# Usage
job_id = await submit_job(
    "job.email.SendEmail",
    {"to": "user@example.com", "subject": "Hello"},
    queue="email",
    prio=0,
    uid=12345,
)
```

### Scheduled Job

```python
import datetime


async def submit_scheduled_job(job_class: str, kwargs: dict, run_at: datetime.datetime):
    conn = await asyncpg.connect(**db_params)

    return await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, run_after)
        VALUES ($1, $2, $3)
        RETURNING id
    """,
        job_class,
        orjson.dumps(kwargs),
        run_at,
    )


# Schedule for tomorrow 9 AM
tomorrow_9am = datetime.datetime.now() + datetime.timedelta(days=1)
tomorrow_9am = tomorrow_9am.replace(hour=9, minute=0, second=0, microsecond=0)

await submit_scheduled_job(
    "job.reports.DailySummary", {"report_type": "sales"}, tomorrow_9am
)
```

### Dependent Job (waitfor_job)

```python
async def submit_dependent_jobs():
    conn = await asyncpg.connect(**db_params)

    # Parent job
    parent_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs)
        VALUES ($1, $2)
        RETURNING id
    """,
        "job.file.Upload",
        '{"filepath": "/tmp/file.jpg"}',
    )

    # Child job (waits for parent)
    child_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, state, waitfor_job)
        VALUES ($1, $2, $3, $4)
        RETURNING id
    """,
        "job.image.Thumbnail",
        '{"filepath": "/tmp/file.jpg"}',
        "waiting",  # Must start in waiting state!
        parent_id,
    )

    return parent_id, child_id
```

### Group Jobs (waitfor_group)

```python
import secrets


async def submit_parallel_pipeline():
    conn = await asyncpg.connect(**db_params)
    group_id = secrets.randbits(63)

    # Create 3 parallel jobs
    for job_class in ["job.Hash", "job.EXIF", "job.Thumbnail"]:
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, run_group)
            VALUES ($1, $2, $3)
        """,
            job_class,
            '{"file": "/tmp/upload.jpg"}',
            group_id,
        )

    # Create aggregator job (waits for all 3)
    aggregator_id = await conn.fetchval(
        """
        INSERT INTO jorb (job_class, kwargs, state, waitfor_group)
        VALUES ($1, $2, $3, $4)
        RETURNING id
    """,
        "job.Aggregate",
        f'{{"group_id": {group_id}}}',
        "waiting",
        group_id,
    )

    return group_id, aggregator_id
```

### Deadline Key (Singleton Scheduling)

```python
async def submit_with_deadline_key(user_id: int):
    conn = await asyncpg.connect(**db_params)

    deadline_key = f"billing-update:{user_id}:{date.today()}"
    midnight = datetime.datetime.combine(
        datetime.date.today() + datetime.timedelta(days=1), datetime.time.min
    )

    try:
        job_id = await conn.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, deadline_key, run_after)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "job.billing.UpdateUsage",
            f'{{"user_id": {user_id}}}',
            deadline_key,
            midnight,
        )
        print(f"Scheduled job {job_id}")
    except asyncpg.UniqueViolationError:
        print("Job already scheduled for this user/date")
```

## Monitoring and Maintenance

### Health Queries

```sql
-- Queue depth by state
SELECT state, COUNT(*), AVG(EXTRACT(EPOCH FROM (NOW() - created))) as avg_age_seconds
FROM jorb
GROUP BY state
ORDER BY COUNT(*) DESC;

-- Oldest pending jobs
SELECT id, job_class, queue, created, NOW() - created as age
FROM jorb
WHERE state = 'queued'
ORDER BY created ASC
LIMIT 10;

-- Recent crashes
SELECT id, job_class, error_message, error_count, updated
FROM jorb
WHERE state = 'crashed'
ORDER BY updated DESC
LIMIT 20;

-- Jobs by queue
SELECT queue, state, COUNT(*)
FROM jorb
GROUP BY queue, state
ORDER BY queue, state;

-- Active workers
SELECT worker_host, worker_pid, COUNT(*) as active_jobs
FROM jorb
WHERE state IN ('claimed', 'running')
GROUP BY worker_host, worker_pid;
```

### Performance Queries

```sql
-- Average job duration by class
SELECT job_class,
       COUNT(*) as total,
       AVG(EXTRACT(EPOCH FROM (updated - created))) as avg_duration_seconds,
       MAX(EXTRACT(EPOCH FROM (updated - created))) as max_duration_seconds
FROM jorb
WHERE state = 'finished'
  AND created > NOW() - INTERVAL '24 hours'
GROUP BY job_class
ORDER BY avg_duration_seconds DESC;

-- Failure rate by job class
SELECT job_class,
       COUNT(*) FILTER (WHERE state = 'finished') as succeeded,
       COUNT(*) FILTER (WHERE state = 'crashed') as failed,
       ROUND(100.0 * COUNT(*) FILTER (WHERE state = 'crashed') / COUNT(*), 2) as failure_rate_pct
FROM jorb
WHERE created > NOW() - INTERVAL '24 hours'
GROUP BY job_class
ORDER BY failure_rate_pct DESC;

-- Job throughput (jobs/minute over last hour)
SELECT DATE_TRUNC('minute', created) as minute,
       COUNT(*) as jobs_created,
       COUNT(*) FILTER (WHERE state = 'finished') as jobs_finished
FROM jorb
WHERE created > NOW() - INTERVAL '1 hour'
GROUP BY minute
ORDER BY minute DESC;
```

### Maintenance

```sql
-- Clean up old finished jobs (older than 30 days)
DELETE FROM jorb
WHERE state = 'finished'
  AND updated < NOW() - INTERVAL '30 days';

-- Archive old jobs before deletion
INSERT INTO jorb_archive
SELECT * FROM jorb
WHERE state IN ('finished', 'crashed')
  AND updated < NOW() - INTERVAL '30 days';

-- Vacuum after large deletions
VACUUM ANALYZE jorb;

-- Reindex if needed
REINDEX TABLE jorb;
```

## Schema Migration Example

### Adding a New Column

```sql
-- Add new column
ALTER TABLE jorb ADD COLUMN tags TEXT[];

-- Create index
CREATE INDEX jorb_tags_idx ON jorb USING GIN(tags);

-- Update existing rows
UPDATE jorb SET tags = '{}' WHERE tags IS NULL;

-- Make it non-nullable
ALTER TABLE jorb ALTER COLUMN tags SET DEFAULT '{}';
ALTER TABLE jorb ALTER COLUMN tags SET NOT NULL;
```

### Changing State Enum

```sql
-- Add new state to enum
ALTER TYPE jorb_state ADD VALUE 'paused';

-- Update jobs
UPDATE jorb SET state = 'paused' WHERE id IN (SELECT ...);
```

## Best Practices

### 1. Always Use Transactions for Multi-Job Submissions

```python
# Good: Atomic
async with conn.transaction():
    parent_id = await conn.fetchval("INSERT INTO jorb ...")
    child_id = await conn.fetchval("INSERT INTO jorb ... waitfor_job = $1", parent_id)

# Bad: Race condition (child might run before parent)
parent_id = await conn.fetchval("INSERT INTO jorb ...")
child_id = await conn.fetchval("INSERT INTO jorb ... waitfor_job = $1", parent_id)
```

### 2. Use Appropriate Data Types

```python
# Good: JSONB for structured data
kwargs = {"user_id": 123, "email": "user@example.com"}
await conn.execute("INSERT INTO jorb (kwargs) VALUES ($1)", orjson.dumps(kwargs))

# Bad: String encoding
kwargs_str = "user_id=123&email=user@example.com"  # Hard to query!
```

### 3. Set Indexes on Frequently Queried Columns

```sql
-- If you frequently query by custom fields, add index
CREATE INDEX jorb_admin_data_source_idx ON jorb ((admin_data->>'source'));

-- Query efficiently
SELECT * FROM jorb WHERE admin_data->>'source' = 'api';
```

### 4. Monitor Index Usage

```sql
-- Find unused indexes
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
  AND tablename = 'jorb'
  AND idx_scan = 0
ORDER BY indexname;

-- Drop if truly unused
DROP INDEX IF EXISTS unused_index_name;
```

### 5. Keep Job Results Small

```python
# Good: Return reference
return {"s3_key": "uploads/file.jpg", "size": 1024}

# Bad: Return large data (bloats table)
return {"image_data": base64.encode(huge_image)}  # Don't do this!
```

## Summary

The pyjobby database schema is designed for:

- ✅ **Performance**: Partial indexes minimize scan time
- ✅ **Atomicity**: Row-level locking prevents double-processing
- ✅ **Flexibility**: Support for dependencies, priorities, scheduling
- ✅ **Observability**: Complete audit trail with error tracking
- ✅ **Scalability**: Efficient even with millions of rows

Key design decisions:

- Single table keeps schema simple
- Partial indexes reduce overhead
- JSONB columns provide flexibility
- Enum state machine ensures consistency
- Foreign keys enforce referential integrity (where possible)
