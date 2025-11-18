# Recurring Scheduler User Guide

The Pyjobby Recurring Scheduler allows you to automatically create jobs on a schedule using cron expressions. It includes comprehensive safety features to prevent runaway job creation and system overload.

## Quick Start

### Creating a Schedule via CLI

```bash
# Create a daily cleanup job at 2am
pj-admin schedule add daily-cleanup \
    myapp.jobs.CleanupJob \
    "0 2 * * *" \
    --description "Daily cleanup at 2am" \
    --queue cleanup

# Create an hourly report with safety features
pj-admin schedule add hourly-report \
    myapp.jobs.ReportJob \
    "0 * * * *" \
    --queue reports \
    --max-concurrent 3 \
    --jitter 300 \
    --description "Hourly reports with 5min jitter"

# List all schedules
pj-admin schedule list

# Show detailed schedule information
pj-admin schedule show daily-cleanup

# Disable a schedule (without deleting it)
pj-admin schedule disable daily-cleanup

# Re-enable a schedule
pj-admin schedule enable daily-cleanup

# View execution history
pj-admin schedule history daily-cleanup

# View statistics for all schedules
pj-admin schedule stats
```

### Creating a Schedule via Python API

```python
import asyncio
import asyncpg
from pyjobby.admin_api import AdminAPI

async def create_schedule():
    conn = await asyncpg.connect(
        host='localhost',
        database='pyjobby',
        user='postgres'
    )

    api = AdminAPI(conn)

    # Create schedule
    schedule = await api.create_schedule(
        name='daily-cleanup',
        job_class='myapp.jobs.CleanupJob',
        cron_expr='0 2 * * *',  # 2am daily
        queue='cleanup',
        kwargs={'days': 30},  # Job arguments
        description='Daily cleanup of old data',
        max_concurrent_jobs=1,  # Only 1 at a time
        jitter_seconds=300,  # Random 0-5min delay
        backpressure_threshold=1000,  # Skip if queue > 1000
        circuit_breaker_threshold=5,  # Disable after 5 failures
    )

    print(f"Created schedule: {schedule['name']}")
    print(f"Next run: {schedule['next_run']}")

    await conn.close()

asyncio.run(create_schedule())
```

### Creating a Schedule via Web Interface

1. Navigate to `http://localhost:8081/schedules`
2. Click "+ Add Schedule"
3. Fill in the form:
   - **Schedule Name**: `daily-cleanup`
   - **Job Class**: `myapp.jobs.CleanupJob`
   - **Cron Expression**: `0 2 * * *`
   - **Queue**: `default`
   - **Description**: `Daily cleanup at 2am`
4. Configure safety features (optional):
   - **Max Concurrent Jobs**: 1
   - **Jitter (seconds)**: 300
   - **Backpressure Threshold**: 1000
   - **Circuit Breaker Threshold**: 5
5. Click "Create Schedule"

## Cron Expression Guide

Cron expressions define when jobs should run:

```
*    *    *    *    *
│    │    │    │    │
│    │    │    │    └─── Day of week (0-7, 0 and 7 are Sunday)
│    │    │    └──────── Month (1-12)
│    │    └───────────── Day of month (1-31)
│    └────────────────── Hour (0-23)
└─────────────────────── Minute (0-59)
```

### Common Examples

```bash
"0 2 * * *"        # Every day at 2:00 AM
"0 */6 * * *"      # Every 6 hours
"0 0 * * 0"        # Every Sunday at midnight
"*/15 * * * *"     # Every 15 minutes
"0 9-17 * * 1-5"   # Every hour 9am-5pm, Mon-Fri
"0 0 1 * *"        # First day of every month
"0 0 1 1 *"        # January 1st every year
```

## Safety Features

The scheduler includes multiple safety mechanisms to prevent system overload:

### 1. Max Concurrent Jobs

**Purpose**: Prevent unlimited job creation if jobs are slow or buggy

**How it works**: Before creating a new job, the scheduler counts how many jobs from this schedule are currently running. If the count reaches `max_concurrent_jobs`, the execution is skipped.

**Example**:
```bash
# Only allow 1 daily report to run at a time
pj-admin schedule add daily-report \
    ReportJob "0 2 * * *" \
    --max-concurrent 1
```

**When to use**:
- Jobs that take a long time (> 1 hour)
- Jobs that shouldn't overlap (database migrations, backups)
- Resource-intensive jobs

### 2. Random Jitter

**Purpose**: Prevent all scheduled jobs from starting at the exact same time (thundering herd)

**How it works**: Adds a random delay between 0 and `jitter_seconds` before creating the job. This spreads the load over time.

**Example**:
```bash
# Hourly job with 5-minute jitter window
pj-admin schedule add hourly-sync \
    SyncJob "0 * * * *" \
    --jitter 300  # 0-300 seconds (0-5 minutes)
```

**When to use**:
- Many schedules running at the same time
- Jobs that access external APIs (spread load)
- Jobs that compete for resources

### 3. Backpressure Handling

**Purpose**: Skip job creation when the queue is overloaded

**How it works**: Before creating a job, checks the queue depth. If there are more than `backpressure_threshold` jobs waiting, the execution is skipped.

**Example**:
```bash
# Skip if queue has > 500 jobs waiting
pj-admin schedule add data-export \
    ExportJob "0 * * * *" \
    --backpressure 500
```

**When to use**:
- Non-critical jobs that can be skipped
- Jobs during peak load times
- Systems with variable load

### 4. Circuit Breaker

**Purpose**: Automatically disable schedules that repeatedly fail

**How it works**: Tracks consecutive failures. When failures reach `circuit_breaker_threshold`, the schedule is automatically disabled. Requires manual re-enabling after fixing the issue.

**Example**:
```bash
# Disable after 5 consecutive failures
pj-admin schedule add critical-job \
    CriticalJob "0 * * * *" \
    --circuit-breaker 5
```

**When to re-enable**:
1. Fix the underlying issue (bug, database problem, etc.)
2. Re-enable the schedule: `pj-admin schedule enable critical-job`
3. The failure counter is automatically reset when re-enabled

### 5. Deadline Keys (Automatic)

**Purpose**: Prevent duplicate job creation

**How it works**: Each schedule execution gets a unique deadline key based on `schedule:id:scheduled_time`. If the same job is created twice (e.g., by multiple scheduler instances), the duplicate is automatically prevented.

**No configuration needed**: This works automatically.

## Monitoring & Troubleshooting

### View Schedule Statistics

```bash
# View success rates and execution counts
pj-admin schedule stats
```

Output:
```
Schedule Statistics
Name         Enabled  Runs  Success  Fails  Skips  Rate    Next
----------------------------------------------------------------
daily-clea   ✓        100   98       2      0      98.0%   11-19 02:00
hourly-rep   ✓        2400  2350     50     10     97.9%   11-18 20:00
```

### View Execution History

```bash
# View last 50 executions
pj-admin schedule history daily-cleanup

# Filter by result
pj-admin schedule history daily-cleanup --result failure
```

### Check Schedule Status

```bash
# View detailed schedule information
pj-admin schedule show daily-cleanup
```

Output shows:
- Current enabled/disabled status
- Next run time
- Safety feature configuration
- Execution statistics (runs, successes, failures, skips)
- Consecutive failure count
- Last run/success/failure timestamps

### Common Issues

**Schedule not running:**
1. Check if enabled: `pj-admin schedule show <name>`
2. Check next_run time - might be in the future
3. Check scheduler worker is running
4. Check circuit breaker hasn't triggered (consecutive_failures)

**Schedule skipping executions:**
1. Check logs for skip reason (concurrency, backpressure, etc.)
2. View history: `pj-admin schedule history <name>`
3. Adjust safety thresholds if needed

**Circuit breaker triggered:**
1. Check error logs for the underlying issue
2. Fix the problem (bug, database, API, etc.)
3. Re-enable: `pj-admin schedule enable <name>`

## Running the Scheduler Worker

The scheduler worker polls for due schedules every 60 seconds and creates jobs with all safety checks applied.

```bash
# Start scheduler worker (runs alongside job workers)
pj scheduler ./pyjobby.conf.py
```

### Multiple Scheduler Instances

You can run multiple scheduler worker instances for redundancy. Deadline keys prevent duplicate job creation even with multiple instances.

```bash
# Instance 1
pj scheduler ./pyjobby.conf.py

# Instance 2 (on different server)
pj scheduler ./pyjobby.conf.py
```

## Best Practices

### Choosing Safety Parameters

**max_concurrent_jobs**:
- `1` - Jobs that must run sequentially (migrations, backups)
- `3-5` - Normal jobs that can overlap but shouldn't run unlimited
- Higher values - Fast, stateless jobs

**jitter_seconds**:
- `0` - Time-critical jobs that must run at exact time
- `60-300` (1-5 min) - Normal jobs, spreads thundering herd
- `600-1800` (10-30 min) - Background jobs, very flexible timing

**backpressure_threshold**:
- `100-500` - High-priority queues, catch overload early
- `1000-5000` - Normal queues
- `null` - Disable backpressure (job always runs)

**circuit_breaker_threshold**:
- `3-5` - Critical jobs, fail fast
- `10-20` - Normal jobs
- Higher values - Jobs with expected occasional failures

### Schedule Naming

Use descriptive names with hyphens:
- ✓ `daily-user-cleanup`
- ✓ `hourly-report-generation`
- ✓ `weekly-database-backup`
- ✗ `job1`, `test`, `x`

### Job Arguments

Pass configuration via kwargs:
```python
await api.create_schedule(
    name='cleanup-old-data',
    job_class='CleanupJob',
    cron_expr='0 2 * * *',
    kwargs={'days': 30, 'batch_size': 1000}  # Job-specific config
)
```

### Testing Schedules

1. Create schedule with `--disabled` flag
2. Review configuration: `pj-admin schedule show <name>`
3. Enable: `pj-admin schedule enable <name>`
4. Monitor first few executions: `pj-admin schedule history <name>`

### Timezone Handling

Schedules use UTC by default. To use a different timezone:

```python
await api.create_schedule(
    name='daily-report',
    job_class='ReportJob',
    cron_expr='0 9 * * *',  # 9am
    timezone='America/New_York'  # Eastern time
)
```

Common timezones:
- `UTC` (default)
- `America/New_York` (Eastern)
- `America/Los_Angeles` (Pacific)
- `Europe/London`
- `Asia/Tokyo`

## Web Interface

Access the web interface at `http://localhost:8081/schedules` (default port).

Features:
- View all schedules with status and statistics
- Create new schedules with form validation
- Enable/disable schedules with one click
- Delete schedules with confirmation
- Auto-refresh every 10 seconds
- Success rate visualization with color coding

## API Reference

See [Admin API Documentation](ADMIN_TOOLS.md#schedule-management) for complete API reference.

Key methods:
- `await api.create_schedule(...)`
- `await api.list_schedules(enabled=True, queue='default')`
- `await api.get_schedule(schedule_id=1)` or `get_schedule(name='daily-cleanup')`
- `await api.update_schedule(schedule_id, cron_expr='0 3 * * *')`
- `await api.enable_schedule(schedule_id)`
- `await api.disable_schedule(schedule_id)`
- `await api.delete_schedule(schedule_id)`
- `await api.get_schedule_history(schedule_id, limit=100)`
- `await api.get_schedule_stats()`

## Performance

The scheduler is designed for high performance:

- Polls every 60 seconds (low overhead)
- Efficiently queries only due schedules
- All safety checks use indexed queries
- Deadline keys prevent duplicates at database level
- Can handle 1000+ schedules without performance degradation

## Migration from Other Schedulers

### From cron

Replace cron jobs with pyjobby schedules to get:
- Centralized management
- Safety features (concurrency limits, backpressure)
- Execution history and statistics
- Easy enable/disable without editing crontab
- Web interface for non-technical users

### From Celery Beat

Replace Celery Beat with pyjobby scheduler to get:
- Better safety features (circuit breaker, backpressure)
- Native PostgreSQL integration (no Redis required)
- Execution history in database
- More flexible management (CLI + Web + API)

## Support

For detailed design information, see [SCHEDULER_DESIGN.md](SCHEDULER_DESIGN.md).

For issues or questions:
- Check logs for detailed error messages
- Review schedule statistics and history
- Verify safety thresholds are appropriate for your use case
