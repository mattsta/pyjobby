# Pyjobby Troubleshooting Guide

## Common Issues and Solutions

### Workers Not Claiming Jobs

**Symptoms**:

- Jobs stuck in `queued` state
- Workers running but idle
- No logs showing job execution

**Possible Causes**:

#### 1. Queue Mismatch

**Diagnosis**:

```sql
-- Check which queues have jobs
SELECT queue, COUNT(*) FROM jorb WHERE state = 'queued' GROUP BY queue;

-- Check worker logs for queue name
grep "Connected and waiting for jobs" /var/log/pyjobby/worker.log
```

**Solution**:

```bash
# Workers must match job queue
pj --queue email  # If jobs are in 'email' queue

# Or use multiple queues
pj --queue default --queue email
```

---

#### 2. Capability Mismatch

**Diagnosis**:

```sql
-- Check required capabilities
SELECT DISTINCT capability FROM jorb WHERE state = 'queued';

-- Check if any jobs require capabilities worker doesn't have
SELECT id, job_class, capability
FROM jorb
WHERE state = 'queued'
  AND capability IS NOT NULL
  AND capability NOT IN ('host:your-hostname');
```

**Solution**:

```bash
# Add required capability to worker
pj --cap "gpu" --cap "ml-node"

# Or remove capability requirement from jobs
UPDATE jorb SET capability = NULL WHERE id = 12345;
```

---

#### 3. Future `run_after` Timestamp

**Diagnosis**:

```sql
-- Check if jobs are scheduled for the future
SELECT id, job_class, run_after, run_after - NOW() as time_until_run
FROM jorb
WHERE state = 'queued'
  AND run_after > NOW();
```

**Solution**:
Wait for timestamp to pass, or update to run immediately:

```sql
UPDATE jorb
SET run_after = NOW()
WHERE id = 12345;
```

---

#### 4. Priority Too Low

**Diagnosis**:

```sql
-- Check job priorities
SELECT id, job_class, prio FROM jorb WHERE state = 'queued' ORDER BY prio;

-- Check worker priority limit (from logs)
grep "prio" /var/log/pyjobby/worker.log
```

**Solution**:

```bash
# Start worker with higher priority limit
pj --prio 10000  # Process jobs with prio <= 10000

# Or lower job priority
UPDATE jorb SET prio = 0 WHERE id = 12345;
```

---

#### 5. Database Connection Issues

**Diagnosis**:

```bash
# Check worker logs for connection errors
grep -i "error" /var/log/pyjobby/worker.log
grep -i "connection" /var/log/pyjobby/worker.log

# Test database connection
psql -h localhost -U pyjobby -d pyjobby_prod -c "SELECT 1;"
```

**Solution**:

```python
# Check config file
cat pyjobby.conf.py

# Verify credentials
psql -h <host> -U <user> -d <database>

# Check PostgreSQL logs
tail -f /var/log/postgresql/postgresql-15-main.log
```

---

### Jobs Stuck in "claimed" or "running" State

**Symptoms**:

- Jobs never complete
- `state` is "claimed" or "running" for hours/days
- Worker that claimed job is no longer running

**Cause**: Worker crashed before completing job (see CODE_AUDIT.md Issue #1)

**Solution**:

**Immediate Fix** (Manual Recovery):

```sql
-- Find stuck jobs
SELECT id, job_class, worker_host, worker_pid, updated,
       NOW() - updated as stuck_duration
FROM jorb
WHERE state IN ('claimed', 'running')
  AND updated < NOW() - INTERVAL '1 hour';

-- Re-queue them
UPDATE jorb
SET state = 'queued', run_after = NOW()
WHERE state IN ('claimed', 'running')
  AND updated < NOW() - INTERVAL '1 hour';
```

**Automated Solution** (Create Cron Job):

```bash
# /usr/local/bin/pyjobby-recover-stuck-jobs.sh
#!/bin/bash
psql -U pyjobby -d pyjobby_prod <<EOF
UPDATE jorb
SET state = 'queued',
    run_after = TIMEZONE('utc', CURRENT_TIMESTAMP)
WHERE state IN ('claimed', 'running')
  AND updated < TIMEZONE('utc', CURRENT_TIMESTAMP) - INTERVAL '1 hour';
EOF

# Add to crontab (run every 5 minutes)
*/5 * * * * /usr/local/bin/pyjobby-recover-stuck-jobs.sh
```

**Long-term Fix**: Implement job recovery on worker startup (see CODE_AUDIT.md)

---

### High Error Rate

**Symptoms**:

- Many jobs in `crashed` state
- Worker logs show frequent exceptions

**Diagnosis**:

#### 1. Identify Failing Jobs

```sql
-- Recent crashes
SELECT job_class, COUNT(*) as crash_count,
       AVG(error_count) as avg_retries
FROM jorb
WHERE state = 'crashed'
  AND updated > NOW() - INTERVAL '24 hours'
GROUP BY job_class
ORDER BY crash_count DESC;

-- View specific error messages
SELECT id, job_class, error_message, error_backtrace
FROM jorb
WHERE state = 'crashed'
ORDER BY updated DESC
LIMIT 10;
```

#### 2. Common Error Causes

**A. Bad Input Data**

```sql
-- Example: Email job failing due to invalid email
SELECT id, kwargs->>'email' as email
FROM jorb
WHERE job_class = 'job.email.SendEmail'
  AND state = 'crashed';
```

**Solution**: Validate inputs before submission

```python
def validate_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[1]


if not validate_email(email):
    raise ValueError(f"Invalid email: {email}")
```

**B. External Service Unavailable**

```python
# Error: Connection timeout to external API
# Solution: Add retry logic with backoff
class APIJob(Job):
    async def task(self, endpoint: str):
        for attempt in range(3):
            try:
                return await call_api(endpoint)
            except aiohttp.ClientError:
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
```

**C. Resource Limits**

```python
# Error: Out of memory
# Solution: Process data in chunks
class ProcessLargeFile(Job):
    def task(self, filepath: str):
        # Bad: Load entire file into memory
        # data = open(filepath).read()

        # Good: Process in chunks
        with open(filepath, "rb") as f:
            while chunk := f.read(4096):
                process_chunk(chunk)
```

---

### Database Performance Issues

**Symptoms**:

- Slow job claiming
- Workers timing out
- High database CPU usage

**Diagnosis**:

#### 1. Check Index Usage

```sql
-- Are indexes being used?
EXPLAIN ANALYZE
SELECT id FROM jorb
WHERE queue = 'default'
  AND state = 'queued'
  AND run_after <= NOW()
ORDER BY prio, run_after
LIMIT 1;

-- Should show "Index Scan using jorb_poll_idx"
-- If showing "Seq Scan", indexes are not being used!
```

**Solution** (If indexes missing):

```sql
-- Recreate indexes
CREATE INDEX jorb_poll_idx ON jorb (queue, capability, prio, run_after)
WHERE state = 'queued' OR state = 'crashed';
```

#### 2. Table Bloat

**Diagnosis**:

```sql
-- Check table size
SELECT pg_size_pretty(pg_total_relation_size('jorb'));

-- Check dead tuples
SELECT n_dead_tup, n_live_tup,
       round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2) as dead_pct
FROM pg_stat_user_tables
WHERE relname = 'jorb';
```

**Solution**:

```sql
-- Vacuum the table
VACUUM VERBOSE jorb;

-- Or full vacuum (requires table lock, do during maintenance window)
VACUUM FULL jorb;

-- Analyze to update statistics
ANALYZE jorb;
```

#### 3. Too Many Completed Jobs

**Diagnosis**:

```sql
-- Count jobs by state
SELECT state, COUNT(*) FROM jorb GROUP BY state;

-- If finished/crashed count is in millions, cleanup needed
```

**Solution**:

```bash
# Archive old jobs
psql -U pyjobby -d pyjobby_prod <<EOF
-- Archive to separate table
CREATE TABLE IF NOT EXISTS jorb_archive (LIKE jorb INCLUDING ALL);

INSERT INTO jorb_archive
SELECT * FROM jorb
WHERE state IN ('finished', 'crashed')
  AND updated < NOW() - INTERVAL '30 days';

-- Delete archived jobs
DELETE FROM jorb
WHERE state IN ('finished', 'crashed')
  AND updated < NOW() - INTERVAL '30 days';

-- Vacuum to reclaim space
VACUUM ANALYZE jorb;
EOF
```

**Automate** (Add to cron):

```bash
# /etc/cron.daily/pyjobby-cleanup
0 2 * * * /usr/local/bin/pyjobby-archive-old-jobs.sh
```

#### 4. Connection Pool Exhaustion

**Diagnosis**:

```sql
-- Check current connections
SELECT COUNT(*), state
FROM pg_stat_activity
WHERE datname = 'pyjobby_prod'
GROUP BY state;

-- Check max connections
SHOW max_connections;
```

**Solution**:

```python
# Reduce pool size in config
db_params = {
    "database": "pyjobby_prod",
    "min_size": 1,  # Reduce from 2
    "max_size": 5,  # Reduce from 10
}

# Or increase PostgreSQL max_connections
# In postgresql.conf:
max_connections = 200  # Increase from 100
```

---

### Worker Memory Leaks

**Symptoms**:

- Worker memory usage grows over time
- Eventually crashes with OOM error

**Diagnosis**:

```bash
# Monitor worker memory
ps aux | grep pj

# Watch memory over time
watch -n 5 'ps aux | grep pj | grep -v grep'
```

**Common Causes**:

#### 1. Cache Growing Unbounded

**Problem**:

```python
class LeakyJob(Job):
    def task(self, data: str):
        # Cache grows forever!
        cache_key = f"result:{data}"
        self.s.cache[cache_key] = large_computation(data)
```

**Solution**:

```python
class FixedJob(Job):
    def task(self, data: str):
        # Limit cache size
        MAX_CACHE_SIZE = 1000

        if len(self.s.cache) > MAX_CACHE_SIZE:
            # Clear oldest entries (simple approach)
            self.s.cache.clear()

        cache_key = f"result:{data}"
        if cache_key not in self.s.cache:
            self.s.cache[cache_key] = large_computation(data)
```

#### 2. Unclosed Resources

**Problem**:

```python
class LeakyJob(Job):
    async def task(self, url: str):
        # File handle never closed!
        f = open("/tmp/output.txt", "w")
        f.write(await fetch(url))
        # Missing f.close()
```

**Solution**:

```python
class FixedJob(Job):
    async def task(self, url: str):
        # Use context manager
        with open("/tmp/output.txt", "w") as f:
            f.write(await fetch(url))

        # Or for async resources
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                data = await response.read()
```

#### 3. Large Result Objects

**Problem**:

```python
class LeakyJob(Job):
    def task(self, filepath: str):
        # Returns entire file contents!
        with open(filepath, "rb") as f:
            return {"data": f.read()}  # Could be gigabytes!
```

**Solution**:

```python
class FixedJob(Job):
    def task(self, filepath: str):
        # Upload to S3, return reference only
        s3_key = self.upload_to_s3(filepath)
        return {"s3_key": s3_key, "size": os.path.getsize(filepath)}
```

**Monitoring**:

```python
# Add memory tracking
import psutil
import os


class MonitoredJob(Job):
    def run(self):
        process = psutil.Process(os.getpid())
        mem_before = process.memory_info().rss / 1024 / 1024  # MB

        result = super().run()

        mem_after = process.memory_info().rss / 1024 / 1024
        mem_delta = mem_after - mem_before

        if mem_delta > 100:  # More than 100MB increase
            logger.warning(f"Job {self.job['id']} used {mem_delta:.1f}MB memory")

        return result
```

---

### Web Endpoint Not Working

**Symptoms**:

- HTTP requests to job endpoints return 404 or timeout
- Workers running but web requests not handled

**Diagnosis**:

#### 1. Check Web Configuration

```python
# Verify config file
cat pyjobby.conf.py

# Should have:
web_listen = {
    "sites": [{"host": "0.0.0.0", "port": 8080}],
    "paths": {"job.webhook.Handler"}  # Job class must be in paths!
}
```

**Solution**:

```python
# Add job class to paths
web_listen = {
    "sites": [{"host": "0.0.0.0", "port": 8080}],
    "paths": {
        "job.webhook.Handler",  # Add your job class here
    },
}
```

#### 2. Check Port Binding

```bash
# Is worker listening?
netstat -tlnp | grep 8080

# Or with ss
ss -tlnp | grep 8080

# Should show: LISTEN 0.0.0.0:8080
```

**Solution**:

```bash
# Check firewall
sudo ufw status
sudo ufw allow 8080

# Check SELinux (if applicable)
sudo semanage port -a -t http_port_t -p tcp 8080
```

#### 3. Test Endpoint

```bash
# Test with curl
curl -v http://localhost:8080/job.webhook.Handler

# Should NOT return "not so fast!" (that means path not in whitelist)

# Check worker logs
tail -f /var/log/pyjobby/worker.log
```

---

### Job Not Found Error

**Symptoms**:

```
FileNotFoundError: Job class not found: job.email.SendEmail; search path: [...]
```

**Diagnosis**:

#### 1. Check Job Class Exists

```bash
# Verify file exists
ls -la job/email.py

# Verify class is defined
grep "class SendEmail" job/email.py
```

**Solution**:

```python
# Ensure proper structure
# job/email.py
from pyjobby.pj import Job


class SendEmail(Job):  # Class name must match
    def task(self, **kwargs):
        pass
```

#### 2. Check Python Path

```bash
# Verify worker can import
python3 -c "import job.email; print(job.email.SendEmail)"

# Should print: <class 'job.email.SendEmail'>
```

**Solution**:

```bash
# Add path when starting worker
pj --path /opt/myapp --path /opt/myapp/workers

# Or set PYTHONPATH
export PYTHONPATH=/opt/myapp:$PYTHONPATH
pj
```

#### 3. Check Job Class Name in Database

```sql
-- Verify spelling
SELECT DISTINCT job_class FROM jorb;

-- Common mistakes:
-- "job.email.sendemail"  ✗ (lowercase)
-- "job.email.SendEmail"  ✓ (correct)
-- "jobs.email.SendEmail" ✗ (wrong package)
```

---

### Jobs Running Multiple Times

**Symptoms**:

- Same job executes multiple times
- Duplicate side effects (emails sent twice, etc.)

**Causes**:

#### 1. Non-Idempotent Job Retrying

**Problem**:

```python
class SendEmail(Job):
    def task(self, to: str):
        send_email(to, "Welcome!")
        raise Exception("Oops!")  # Job retries, email sent again!
```

**Solution**: Make idempotent

```python
class SendEmail(Job):
    async def task(self, to: str, message_id: str):
        # Check if already sent
        sent = await self.s.cxn.fetchval(
            "SELECT 1 FROM sent_emails WHERE message_id = $1", message_id
        )

        if not sent:
            send_email(to, "Welcome!")

            # Record that we sent it
            await self.s.cxn.execute(
                "INSERT INTO sent_emails (message_id, sent_at) VALUES ($1, NOW())",
                message_id,
            )
```

#### 2. Duplicate Job Submission

**Problem**:

```python
# User clicks "submit" button multiple times
for _ in range(5):  # Oops!
    await submit_job("job.Process", {"data": "..."})
```

**Solution**: Use deadline_key

```python
import uuid

# Generate unique key for this operation
operation_id = str(uuid.uuid4())

await conn.execute(
    """
    INSERT INTO jorb (job_class, kwargs, deadline_key)
    VALUES ($1, $2, $3)
    ON CONFLICT (deadline_key, queue) WHERE state = 'queued' DO NOTHING
""",
    "job.Process",
    '{"data": "..."}',
    operation_id,
)
```

---

### Slow Job Processing

**Symptoms**:

- Jobs taking much longer than expected
- Queue depth growing

**Diagnosis**:

#### 1. Identify Slow Jobs

```sql
-- Find longest-running jobs
SELECT id, job_class,
       EXTRACT(EPOCH FROM (NOW() - updated)) as running_seconds
FROM jorb
WHERE state = 'running'
ORDER BY running_seconds DESC
LIMIT 10;

-- Average duration by job class
SELECT job_class,
       COUNT(*) as total,
       AVG(EXTRACT(EPOCH FROM (updated - created))) as avg_seconds
FROM jorb
WHERE state = 'finished'
  AND updated > NOW() - INTERVAL '24 hours'
GROUP BY job_class
ORDER BY avg_seconds DESC;
```

#### 2. Optimize Jobs

**Profile Job**:

```python
import time


class SlowJob(Job):
    def task(self, **kwargs):
        t1 = time.time()
        step1_result = self.step1()
        logger.info(f"Step 1: {time.time() - t1:.2f}s")

        t2 = time.time()
        step2_result = self.step2(step1_result)
        logger.info(f"Step 2: {time.time() - t2:.2f}s")

        # Identify bottleneck, optimize that step
```

**Common Optimizations**:

```python
# Bad: N+1 queries
for user_id in user_ids:
    user = await fetch_user(user_id)  # 1000 queries!
    process(user)

# Good: Batch query
users = await fetch_users(user_ids)  # 1 query
for user in users:
    process(user)

# Bad: Blocking I/O
result = requests.get(url)  # Blocks worker

# Good: Async I/O
async with aiohttp.ClientSession() as session:
    async with session.get(url) as response:
        result = await response.json()
```

#### 3. Increase Workers

```bash
# If CPU is underutilized, add more workers
pj --workers 8  # Increase from 4

# Or start multiple worker instances
systemctl start pyjobby@default.service
systemctl start pyjobby@queue2.service
```

---

### Debugging Tips

#### Enable Debug Logging

```python
# In your job file or pyjobby.conf.py
from loguru import logger
import sys

logger.remove()
logger.add(sys.stderr, level="DEBUG")  # Show all logs
```

#### Interactive Debugging

```python
# Add breakpoint in job
class DebugJob(Job):
    def task(self, **kwargs):
        import pdb

        pdb.set_trace()  # Debugger
        # Job will pause here, attach debugger
```

#### Query Job History

```sql
-- Trace job through states
SELECT id, state, updated, error_message
FROM jorb
WHERE id = 12345;

-- Find related jobs
SELECT * FROM jorb
WHERE run_group = (SELECT run_group FROM jorb WHERE id = 12345);

-- See what user's jobs are doing
SELECT id, job_class, state, created
FROM jorb
WHERE uid = 123
ORDER BY created DESC
LIMIT 50;
```

---

## Getting Help

If you're still stuck:

1. **Check worker logs**:

   ```bash
   journalctl -u pyjobby@default -f
   ```

2. **Check PostgreSQL logs**:

   ```bash
   tail -f /var/log/postgresql/postgresql-15-main.log
   ```

3. **Enable query logging** (temporarily):

   ```sql
   ALTER DATABASE pyjobby_prod SET log_statement = 'all';
   ```

4. **Open GitHub issue** with:
   - Pyjobby version (`pj -v`)
   - PostgreSQL version (`SELECT version();`)
   - Worker configuration
   - Relevant logs
   - Steps to reproduce

5. **Community support**:
   - GitHub Discussions
   - Stack Overflow tag: `pyjobby`

---

## Prevention Checklist

- [ ] Monitor queue depth daily
- [ ] Set up alerts for stuck jobs
- [ ] Schedule regular table cleanup
- [ ] Implement job timeout
- [ ] Make jobs idempotent
- [ ] Validate inputs before submission
- [ ] Test job recovery after worker crash
- [ ] Monitor database performance
- [ ] Log structured errors
- [ ] Track job metrics

---

## Quick Reference

```bash
# View queue status
psql -c "SELECT state, COUNT(*) FROM jorb GROUP BY state;"

# Find stuck jobs
psql -c "SELECT id, state, updated FROM jorb WHERE state IN ('claimed', 'running') AND updated < NOW() - INTERVAL '1 hour';"

# Restart workers
sudo systemctl restart 'pyjobby@*'

# Clean old jobs
psql -c "DELETE FROM jorb WHERE state = 'finished' AND updated < NOW() - INTERVAL '30 days';"

# Check worker health
ps aux | grep pj
sudo systemctl status 'pyjobby@*'
```

Happy troubleshooting! 🔧
