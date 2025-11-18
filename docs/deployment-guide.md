# Pyjobby Deployment Guide

## Quick Start (Development)

### 1. Install Dependencies

```bash
# Using Poetry (recommended)
poetry add git+https://github.com/mattsta/pyjobby.git#main

# Or with pip
pip install git+https://github.com/mattsta/pyjobby.git#main
```

### 2. Set Up PostgreSQL Database

```bash
# Create database
createdb pyjobby_dev

# Load schema
psql pyjobby_dev -f priv/schema.sql

# Or manually
psql pyjobby_dev <<'EOF'
CREATE TYPE jorb_state AS ENUM (
    'waiting', 'queued', 'claimed', 'running',
    'heartbeat', 'crashed', 'finished'
);

-- See docs/database-schema.md for full schema
EOF
```

### 3. Create Configuration File

```python
# pyjobby.conf.py
db_params = {
    "database": "pyjobby_dev",
    "user": "youruser",
    "password": "",  # or your password
    "host": "/tmp",  # Unix socket, or "localhost" for TCP
    "port": 5432,
}

# Optional: Web server
web_listen = {
    "sites": [{"host": "127.0.0.1", "port": 8080}],
    "paths": set()  # Add job classes that can be called via web
}
```

### 4. Create a Job

```python
# job/hello.py
from pyjobby.pj import Job

class HelloWorld(Job):
    def task(self, name: str = "World"):
        return {"message": f"Hello, {name}!"}
```

### 5. Start Workers

```bash
# Start with default settings (queue=default, workers=CPU/2)
poetry run pj

# Or with custom settings
poetry run pj --queue default --workers 4 --config ./pyjobby.conf.py
```

### 6. Submit a Job

```python
import asyncio
import asyncpg
import orjson

async def submit_test_job():
    conn = await asyncpg.connect(
        database="pyjobby_dev",
        user="youruser",
        host="/tmp"
    )

    job_id = await conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs)
        VALUES ($1, $2)
        RETURNING id
    """, "job.hello.HelloWorld", orjson.dumps({"name": "Alice"}))

    print(f"Submitted job {job_id}")
    await conn.close()

asyncio.run(submit_test_job())
```

## Production Deployment

### Architecture Decisions

Before deploying, decide on:

1. **Number of Workers**: CPU-bound vs I/O-bound workloads
2. **Queue Strategy**: Single queue vs multiple specialized queues
3. **Horizontal Scaling**: Single server vs multiple servers
4. **Database**: Dedicated PostgreSQL instance or shared
5. **Monitoring**: Logging, metrics, alerting strategy

### Production Checklist

- [ ] PostgreSQL tuned for workload
- [ ] Database backups configured
- [ ] Workers run as systemd services (or Docker containers)
- [ ] Configuration file secured (encrypted secrets)
- [ ] Logging configured (file + external aggregation)
- [ ] Monitoring and alerting set up
- [ ] Job retention/archival policy defined
- [ ] Dead letter queue or manual intervention process
- [ ] Capacity planning completed
- [ ] Runbooks created for common issues

### PostgreSQL Tuning

#### Connection Pooling

```python
# pyjobby.conf.py
db_params = {
    "database": "pyjobby_prod",
    "user": "pyjobby",
    "password": "...",  # Use env var or secrets manager
    "host": "postgres.internal",
    "port": 5432,
    "min_size": 2,      # Minimum connections per worker
    "max_size": 10,     # Maximum connections per worker
    "command_timeout": 60,  # Query timeout (seconds)
}

# With 10 workers: 20-100 total connections
```

#### PostgreSQL Configuration

```ini
# postgresql.conf

# Connections
max_connections = 200  # Enough for workers + app servers

# Memory
shared_buffers = 4GB
effective_cache_size = 12GB
work_mem = 16MB
maintenance_work_mem = 512MB

# WAL (Write-Ahead Log)
wal_buffers = 16MB
checkpoint_completion_target = 0.9
max_wal_size = 4GB
min_wal_size = 1GB

# Vacuum (important for high job churn)
autovacuum = on
autovacuum_max_workers = 3
autovacuum_vacuum_cost_limit = 1000

# Query Planner
random_page_cost = 1.1  # For SSD
effective_io_concurrency = 200

# Logging
log_min_duration_statement = 1000  # Log slow queries (>1s)
log_line_prefix = '%t [%p]: [%l-1] user=%u,db=%d,app=%a,client=%h '
log_checkpoints = on
log_connections = on
log_disconnections = on
log_lock_waits = on
```

#### Database Maintenance

```bash
# Daily vacuum (cron)
0 2 * * * psql -U pyjobby -d pyjobby_prod -c "VACUUM ANALYZE jorb;"

# Weekly cleanup (archive + delete old jobs)
0 3 * * 0 /usr/local/bin/pyjobby-cleanup.sh

# Monitor table bloat
psql -c "SELECT * FROM pgstattuple('jorb');"
```

### Systemd Service Configuration

#### `/etc/systemd/system/pyjobby@.service`

```ini
[Unit]
Description=Pyjobby Worker (%i queue)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=pyjobby
Group=pyjobby
WorkingDirectory=/opt/pyjobby

# Environment
Environment="PATH=/opt/pyjobby/.venv/bin:/usr/local/bin:/usr/bin"
Environment="PYTHONPATH=/opt/pyjobby"

# Security
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/log/pyjobby /var/run/pyjobby

# Resource Limits
LimitNOFILE=65536
LimitNPROC=512

# Start command
ExecStart=/opt/pyjobby/.venv/bin/pj \
    --queue %i \
    --workers 4 \
    --config /etc/pyjobby/pyjobby.conf.py

# Restart policy
Restart=always
RestartSec=10s
StartLimitInterval=5min
StartLimitBurst=3

# Logging
StandardOutput=append:/var/log/pyjobby/worker-%i.log
StandardError=append:/var/log/pyjobby/worker-%i.error.log

[Install]
WantedBy=multi-user.target
```

#### Usage

```bash
# Install service
sudo cp pyjobby@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start multiple queues
sudo systemctl enable --now pyjobby@default.service
sudo systemctl enable --now pyjobby@email.service
sudo systemctl enable --now pyjobby@ml.service

# Check status
sudo systemctl status 'pyjobby@*'

# View logs
sudo journalctl -u 'pyjobby@*' -f
```

### Docker Deployment

#### `Dockerfile`

```dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd -m -u 1000 pyjobby

# Install application
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN pip install poetry && \
    poetry config virtualenvs.create false && \
    poetry install --no-dev --no-interaction

# Copy application code
COPY pyjobby/ ./pyjobby/
COPY priv/ ./priv/
COPY job/ ./job/  # Your job classes

# Switch to non-root user
USER pyjobby

# Run workers
CMD ["pj", "--config", "/etc/pyjobby/pyjobby.conf.py"]
```

#### `docker-compose.yml`

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: pyjobby
      POSTGRES_USER: pyjobby
      POSTGRES_PASSWORD: secret
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./priv/schema.sql:/docker-entrypoint-initdb.d/schema.sql
    ports:
      - "5432:5432"

  worker-default:
    build: .
    depends_on:
      - postgres
    environment:
      - QUEUE=default
      - WORKERS=4
    volumes:
      - ./pyjobby.conf.py:/etc/pyjobby/pyjobby.conf.py:ro
      - ./job:/app/job:ro
    command: ["pj", "--queue", "default", "--workers", "4"]
    restart: always

  worker-email:
    build: .
    depends_on:
      - postgres
    volumes:
      - ./pyjobby.conf.py:/etc/pyjobby/pyjobby.conf.py:ro
      - ./job:/app/job:ro
    command: ["pj", "--queue", "email", "--workers", "2"]
    restart: always

  worker-ml:
    build: .
    depends_on:
      - postgres
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - ./pyjobby.conf.py:/etc/pyjobby/pyjobby.conf.py:ro
      - ./job:/app/job:ro
    command: ["pj", "--queue", "ml", "--workers", "1", "--cap", "gpu"]
    restart: always

volumes:
  postgres_data:
```

### Kubernetes Deployment

#### `deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pyjobby-worker-default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: pyjobby-worker
      queue: default
  template:
    metadata:
      labels:
        app: pyjobby-worker
        queue: default
    spec:
      containers:
      - name: worker
        image: myregistry/pyjobby:latest
        command: ["pj", "--queue", "default", "--workers", "4"]
        env:
        - name: DB_HOST
          valueFrom:
            secretKeyRef:
              name: pyjobby-db
              key: host
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: pyjobby-db
              key: password
        volumeMounts:
        - name: config
          mountPath: /etc/pyjobby
          readOnly: true
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "2000m"
        livenessProbe:
          exec:
            command:
            - /bin/sh
            - -c
            - "pgrep -f 'pj --queue default' || exit 1"
          initialDelaySeconds: 30
          periodSeconds: 60
      volumes:
      - name: config
        configMap:
          name: pyjobby-config
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: pyjobby-config
data:
  pyjobby.conf.py: |
    import os
    db_params = {
        "database": "pyjobby",
        "user": "pyjobby",
        "password": os.environ["DB_PASSWORD"],
        "host": os.environ["DB_HOST"],
        "port": 5432,
    }
```

### Configuration Management

#### Production Config Template

```python
# /etc/pyjobby/pyjobby.conf.py
import os
import json

# Load secrets from file (Kubernetes secret, AWS Secrets Manager, etc.)
def load_secret(path):
    with open(path) as f:
        return json.load(f)

secrets = load_secret(os.environ.get("SECRETS_FILE", "/run/secrets/pyjobby.json"))

db_params = {
    "database": os.environ.get("DB_NAME", "pyjobby_prod"),
    "user": os.environ.get("DB_USER", "pyjobby"),
    "password": secrets["db_password"],
    "host": os.environ.get("DB_HOST", "postgres.internal"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "min_size": 2,
    "max_size": 10,
}

# Optional web server
web_listen = {
    "sites": [
        {"host": "0.0.0.0", "port": 8080},
        {"path": "/var/run/pyjobby.sock"}
    ],
    "paths": {
        "job.webhooks.StripeWebhook",
        "job.api.PublicAPI",
    }
}

# Application secrets (accessible via self.s.config in jobs)
stripe_api_key = secrets["stripe_api_key"]
aws_access_key = secrets["aws_access_key"]
aws_secret_key = secrets["aws_secret_key"]
```

### Logging

#### Structured Logging with Loguru

```python
# At the top of your job files
from loguru import logger
import sys

# Configure logging
logger.remove()  # Remove default handler

# Console logging (for Docker/Kubernetes)
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO"
)

# File logging (for bare metal/VMs)
logger.add(
    "/var/log/pyjobby/worker-{time}.log",
    rotation="500 MB",
    retention="30 days",
    compression="gz",
    level="INFO"
)

# Error-only file
logger.add(
    "/var/log/pyjobby/error-{time}.log",
    rotation="100 MB",
    retention="90 days",
    level="ERROR"
)

# JSON logging for external aggregation
logger.add(
    "/var/log/pyjobby/json-{time}.log",
    serialize=True,  # JSON format
    rotation="500 MB",
    level="INFO"
)
```

#### Integration with External Logging

```python
# Send to Datadog
import datadog

logger.add(
    lambda msg: datadog.api.Event.create(
        title="Pyjobby Log",
        text=msg,
        tags=["env:prod", "service:pyjobby"]
    ),
    level="WARNING"
)

# Send to Sentry
import sentry_sdk

sentry_sdk.init(dsn="https://...")

logger.add(
    lambda msg: sentry_sdk.capture_message(msg),
    level="ERROR"
)
```

### Monitoring

#### Metrics Collection

```python
# job/metrics.py
from pyjobby.pj import Job
import time

class BaseMetricJob(Job):
    """Base class with metrics collection"""

    def run(self):
        metrics = self.s.cache.setdefault("metrics", {
            "jobs_processed": 0,
            "total_duration": 0.0,
            "errors": 0,
        })

        start = time.time()
        try:
            result = super().run()
            metrics["jobs_processed"] += 1
            return result
        except Exception as e:
            metrics["errors"] += 1
            raise
        finally:
            duration = time.time() - start
            metrics["total_duration"] += duration

            # Flush to Prometheus every 100 jobs
            if metrics["jobs_processed"] % 100 == 0:
                self.flush_metrics(metrics)

    def flush_metrics(self, metrics):
        # Push to Prometheus Pushgateway
        import requests
        requests.post(
            "http://pushgateway:9091/metrics/job/pyjobby",
            data=f"""
# TYPE pyjobby_jobs_processed counter
pyjobby_jobs_processed {metrics["jobs_processed"]}

# TYPE pyjobby_total_duration_seconds counter
pyjobby_total_duration_seconds {metrics["total_duration"]}

# TYPE pyjobby_errors_total counter
pyjobby_errors_total {metrics["errors"]}
            """
        )
```

#### PostgreSQL Monitoring

```sql
-- Monitor queue depth
SELECT state, COUNT(*) FROM jorb GROUP BY state;

-- Find stuck jobs
SELECT id, job_class, state, updated, NOW() - updated as stuck_duration
FROM jorb
WHERE state IN ('claimed', 'running')
  AND updated < NOW() - INTERVAL '1 hour';

-- Worker activity
SELECT worker_host, COUNT(*) as active_jobs
FROM jorb
WHERE state IN ('claimed', 'running')
GROUP BY worker_host;
```

Create monitoring script:

```python
#!/usr/bin/env python3
# /usr/local/bin/pyjobby-monitor.py
import asyncpg
import asyncio

async def check_health():
    conn = await asyncpg.connect(...)

    # Check queue depth
    queued = await conn.fetchval("SELECT COUNT(*) FROM jorb WHERE state = 'queued'")
    if queued > 10000:
        alert("High queue depth", f"{queued} jobs queued")

    # Check stuck jobs
    stuck = await conn.fetch("""
        SELECT id, job_class FROM jorb
        WHERE state IN ('claimed', 'running')
          AND updated < NOW() - INTERVAL '1 hour'
    """)
    if stuck:
        alert("Stuck jobs detected", f"{len(stuck)} jobs stuck")

    # Check error rate
    recent_errors = await conn.fetchval("""
        SELECT COUNT(*) FROM jorb
        WHERE state = 'crashed'
          AND updated > NOW() - INTERVAL '5 minutes'
    """)
    if recent_errors > 10:
        alert("High error rate", f"{recent_errors} errors in last 5 min")

asyncio.run(check_health())
```

### Security

#### Database Security

```sql
-- Create restricted user
CREATE USER pyjobby_worker WITH PASSWORD '...';

-- Grant only necessary permissions
GRANT SELECT, INSERT, UPDATE ON jorb TO pyjobby_worker;
GRANT USAGE, SELECT ON SEQUENCE jorb_id_seq TO pyjobby_worker;

-- Revoke dangerous permissions
REVOKE DELETE, TRUNCATE ON jorb FROM pyjobby_worker;
```

#### Application Security

```python
# Validate job inputs
class SecureJob(Job):
    def task(self, user_input: str):
        # Sanitize inputs
        if not self.validate_input(user_input):
            raise ValueError("Invalid input")

        # Use parameterized queries
        await self.s.cxn.execute(
            "INSERT INTO logs (data) VALUES ($1)",
            user_input  # Safe from SQL injection
        )

    def validate_input(self, data: str) -> bool:
        # Input validation logic
        return len(data) < 1000 and data.isprintable()
```

### Capacity Planning

#### Estimating Worker Count

```python
# Formula: Workers = (Jobs/Day) / (86400 / Avg_Job_Duration)

# Example:
jobs_per_day = 1_000_000
avg_job_duration_seconds = 2

workers_needed = (jobs_per_day * avg_job_duration_seconds) / 86400
# = 1,000,000 * 2 / 86400 = ~23 workers

# Add 20% buffer for peak load
workers_total = workers_needed * 1.2  # = ~28 workers
```

#### Database Sizing

```sql
-- Estimate table size
SELECT pg_size_pretty(pg_total_relation_size('jorb'));

-- Estimate based on retention
-- Assumptions:
--   - 1M jobs/day
--   - Keep 30 days
--   - Avg row size: 500 bytes
-- Size = 1M * 30 * 500 bytes = 15 GB
-- Add indexes: ~2x = 30 GB total
```

### Disaster Recovery

#### Backup Strategy

```bash
#!/bin/bash
# /usr/local/bin/pyjobby-backup.sh

# Full database backup
pg_dump -U pyjobby -d pyjobby_prod -F c -f /backups/pyjobby-$(date +%Y%m%d-%H%M%S).dump

# Archive old jobs before backup
psql -U pyjobby -d pyjobby_prod <<EOF
BEGIN;
CREATE TABLE IF NOT EXISTS jorb_archive (LIKE jorb INCLUDING ALL);
INSERT INTO jorb_archive SELECT * FROM jorb
WHERE state IN ('finished', 'crashed') AND updated < NOW() - INTERVAL '30 days';
DELETE FROM jorb
WHERE state IN ('finished', 'crashed') AND updated < NOW() - INTERVAL '30 days';
COMMIT;
EOF

# Upload to S3
aws s3 cp /backups/pyjobby-*.dump s3://my-backups/pyjobby/

# Clean up old local backups
find /backups -name "pyjobby-*.dump" -mtime +7 -delete
```

#### Recovery Procedure

```bash
# 1. Stop all workers
sudo systemctl stop 'pyjobby@*'

# 2. Restore database
pg_restore -U pyjobby -d pyjobby_prod -c /backups/pyjobby-20251118.dump

# 3. Verify data
psql -U pyjobby -d pyjobby_prod -c "SELECT COUNT(*) FROM jorb;"

# 4. Restart workers
sudo systemctl start 'pyjobby@*'
```

### Troubleshooting

See `docs/troubleshooting.md` for common issues and solutions.

## Multi-Region Deployment

### Architecture

```
Region A:                   Region B:
┌─────────────┐            ┌─────────────┐
│  Workers    │            │  Workers    │
│  (4 procs)  │            │  (4 procs)  │
└──────┬──────┘            └──────┬──────┘
       │                          │
       │   ┌──────────────────┐   │
       └───┤  PostgreSQL      │───┘
           │  (Primary)       │
           │  Replication ──► │
           └──────────────────┘
```

Workers in both regions connect to same PostgreSQL primary. Use read replicas for read-heavy workloads.

### Region-Specific Capabilities

```bash
# Region A
pj --queue default --cap "region:us-east-1" --cap "host:$(hostname)"

# Region B
pj --queue default --cap "region:eu-west-1" --cap "host:$(hostname)"

# Route jobs to specific region
await conn.execute("""
    INSERT INTO jorb (job_class, kwargs, capability)
    VALUES ($1, $2, $3)
""", "job.process.Data", ..., "region:us-east-1")
```

## Summary

Key deployment practices:

- ✅ Use systemd/Docker/K8s for orchestration
- ✅ Tune PostgreSQL for workload
- ✅ Implement monitoring and alerting
- ✅ Configure structured logging
- ✅ Secure database access
- ✅ Plan for capacity and scaling
- ✅ Implement backup and recovery procedures
- ✅ Document runbooks for common scenarios

Pyjobby is designed to be simple to deploy while providing production-grade reliability.
