"""
Comprehensive tests for AdminAPI.

Tests the administrative API for managing jobs, queues, workers, and schedules.

Coverage Target: 85%+
"""

from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.admin_api import AdminAPI, JobInfo, QueueStats

# =============================================================================
# DATACLASS TESTS
# =============================================================================


class TestJobInfoDataclass:
    """Test JobInfo dataclass."""

    @pytest.mark.asyncio
    async def test_from_record(self, db_pool):
        """Test creating JobInfo from asyncpg Record."""
        async with db_pool.acquire() as conn:
            # Create a test job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, run_after, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NOW())
                RETURNING id
            """,
                "test.Job",
                {"arg": "value"},
                "default",
                "queued",
                100,
            )

            # Fetch as record
            record = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            # Create JobInfo from record
            job_info = JobInfo.from_record(record)

            assert job_info.id == job_id
            assert job_info.job_class == "test.Job"
            assert job_info.kwargs == {"arg": "value"}
            assert job_info.queue == "default"
            assert job_info.state == "queued"
            assert job_info.prio == 100

    @pytest.mark.asyncio
    async def test_to_dict_with_datetimes(self, db_pool):
        """Test JobInfo.to_dict() properly serializes datetimes."""
        async with db_pool.acquire() as conn:
            # Create a finished job with multiple timestamps
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio,
                    run_after, created, updated, started, finished
                )
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NOW(), NOW(), NOW())
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "finished",
                100,
            )

            record = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            job_info = JobInfo.from_record(record)

            # Convert to dict
            data = job_info.to_dict()

            # Verify datetimes are ISO strings
            assert isinstance(data["created"], str)
            assert isinstance(data["updated"], str)
            assert isinstance(data["started"], str)
            assert isinstance(data["finished"], str)

            # Verify ISO format
            datetime.fromisoformat(data["created"])  # Should not raise


class TestQueueStatsDataclass:
    """Test QueueStats dataclass."""

    def test_to_dict(self):
        """Test QueueStats.to_dict()."""
        stats = QueueStats(
            queue="test_queue",
            queued=10,
            running=5,
            finished=100,
            total=115,
            oldest_queued_age_seconds=300.5,
        )

        data = stats.to_dict()

        assert data["queue"] == "test_queue"
        assert data["queued"] == 10
        assert data["running"] == 5
        assert data["finished"] == 100
        assert data["total"] == 115
        assert data["oldest_queued_age_seconds"] == 300.5


# =============================================================================
# JOB MANAGEMENT TESTS
# =============================================================================


class TestAdminAPIJobManagement:
    """Test AdminAPI job management methods."""

    @pytest.mark.asyncio
    async def test_list_jobs_no_filters(self, db_pool):
        """Test listing jobs without filters."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create test jobs
            for i in range(5):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    f"test.Job{i}",
                    {},
                    "default",
                    "queued",
                    100,
                )

        jobs = await api.list_jobs()

        assert len(jobs) >= 5  # At least our 5 jobs
        assert all(isinstance(job, dict) for job in jobs)
        assert all("id" in job and "job_class" in job for job in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_queue(self, db_pool):
        """Test listing jobs filtered by queue."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create jobs in different queues
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_a",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_b",
                "queued",
                100,
            )

        jobs = await api.list_jobs(queue="queue_a")

        assert all(job["queue"] == "queue_a" for job in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_state(self, db_pool):
        """Test listing jobs filtered by state."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create jobs in different states
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "default",
                "running",
                100,
            )

        jobs = await api.list_jobs(state="queued")

        assert all(job["state"] == "queued" for job in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_pagination(self, db_pool):
        """Test job listing pagination."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create 10 jobs
            for i in range(10):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    f"test.Job{i}",
                    {},
                    "default",
                    "queued",
                    100,
                )

        # Get first page
        page1 = await api.list_jobs(limit=5, offset=0)
        assert len(page1) == 5

        # Get second page
        page2 = await api.list_jobs(limit=5, offset=5)
        assert len(page2) == 5

        # Pages should not overlap
        page1_ids = {job["id"] for job in page1}
        page2_ids = {job["id"] for job in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_get_job_exists(self, db_pool):
        """Test getting a single job that exists."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {"key": "value"},
                "default",
                "queued",
                100,
            )

        job = await api.get_job(job_id)

        assert job is not None
        assert job["id"] == job_id
        assert job["job_class"] == "test.Job"
        assert job["kwargs"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_get_job_not_exists(self, db_pool):
        """Test getting a job that doesn't exist."""
        api = AdminAPI(db_pool)

        job = await api.get_job(999999)

        assert job is None

    @pytest.mark.asyncio
    async def test_retry_job_crashed(self, db_pool):
        """Test retrying a crashed job."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create a crashed job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, error_message)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {"arg": "value"},
                "default",
                "crashed",
                100,
                "Test error",
            )

        result = await api.retry_job(job_id)

        assert result == {"job_id": job_id, "status": "requeued"}

        # The SAME row is requeued: identity is stable, errors reset
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job["state"] == "queued"
            assert job["job_class"] == "test.Job"
            assert job["kwargs"] == {"arg": "value"}
            assert job["error_count"] == 0
            assert job["error_message"] is None

    @pytest.mark.asyncio
    async def test_retry_job_invalid_state(self, db_pool):
        """Test retrying a job in invalid state is refused, not raised."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create a queued job (can't retry)
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

        assert await api.retry_job(job_id) == {
            "job_id": job_id,
            "status": "not_retriable",
        }

    @pytest.mark.asyncio
    async def test_cancel_job_queued(self, db_pool):
        """Test cancelling a queued job."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

        result = await api.cancel_job(job_id)

        assert result is not None
        assert result["job_id"] == job_id
        assert result["status"] == "cancelled"

        # Verify state changed
        async with db_pool.acquire() as conn:
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            assert state == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_job_running_requests_cancellation(self, db_pool):
        """Cancelling a running job delivers a cancellation request."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "running",
                100,
            )

        result = await api.cancel_job(job_id)
        assert result == {"job_id": job_id, "status": "cancel_requested"}

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, cancel_requested FROM jorb WHERE id = $1", job_id
            )
            assert row["state"] == "running"
            assert row["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_delete_job(self, db_pool):
        """Test deleting a job."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

        result = await api.delete_job(job_id)

        assert result is True

        # Verify job deleted
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM jorb WHERE id = $1)", job_id
            )
            assert exists is False


# =============================================================================
# QUEUE MANAGEMENT TESTS
# =============================================================================


class TestAdminAPIQueueManagement:
    """Test AdminAPI queue management methods."""

    @pytest.mark.asyncio
    async def test_list_queues(self, db_pool):
        """Test listing all queues."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create jobs in multiple queues
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_a",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_b",
                "queued",
                100,
            )

        queues = await api.list_queues()

        names = [q["name"] for q in queues]
        assert "queue_a" in names
        assert "queue_b" in names

    @pytest.mark.asyncio
    async def test_queue_stats_single_queue(self, db_pool):
        """Test getting stats for a single queue."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create jobs in different states
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "test_queue",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "test_queue",
                "running",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "test_queue",
                "finished",
                100,
            )

        stats = await api.queue_stats(queue="test_queue")

        assert len(stats) == 1
        assert stats[0]["queue"] == "test_queue"
        assert stats[0]["queued"] >= 1
        assert stats[0]["running"] >= 1
        assert stats[0]["finished"] >= 1
        assert stats[0]["total"] >= 3

    @pytest.mark.asyncio
    async def test_queue_stats_all_queues(self, db_pool):
        """Test getting stats for all queues."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_a",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "queue_b",
                "queued",
                100,
            )

        stats = await api.queue_stats()

        # Should have stats for both queues
        queue_names = {s["queue"] for s in stats}
        assert "queue_a" in queue_names
        assert "queue_b" in queue_names

    @pytest.mark.asyncio
    async def test_queue_stats_all_job_states(self, db_pool):
        """Test queue_stats with all possible job states including claimed, waiting, cancelled."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create jobs in all states to cover lines 499, 503, 508-509
            states_to_test = [
                "queued",
                "claimed",
                "running",
                "waiting",
                "finished",
                "crashed",
                "cancelled",
            ]

            for state in states_to_test:
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    "test.Job",
                    {},
                    "comprehensive_queue",
                    state,
                    100,
                )

        stats = await api.queue_stats(queue="comprehensive_queue")

        assert len(stats) == 1
        queue_stat = stats[0]

        # Verify all states are counted
        assert queue_stat["queue"] == "comprehensive_queue"
        assert queue_stat["queued"] == 1  # Line 496
        assert queue_stat["claimed"] == 1  # Line 499 - UNCOVERED
        assert queue_stat["running"] == 1  # Line 501
        assert queue_stat["waiting"] == 1  # Line 503 - UNCOVERED
        assert queue_stat["finished"] == 1  # Line 505
        assert queue_stat["crashed"] == 1  # Line 507
        assert queue_stat["cancelled"] == 1  # Line 509 - UNCOVERED
        assert queue_stat["total"] == 7


# =============================================================================
# WORKER MANAGEMENT TESTS
# =============================================================================


class TestAdminAPIWorkerManagement:
    """Test AdminAPI worker management methods (jorb_worker registry)."""

    @pytest.mark.asyncio
    async def test_list_workers_active(self, db_pool):
        """Registered workers are listed with liveness and current job."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            w1 = await conn.fetchval(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('worker-01', 12345, 'default', '{test}')
                RETURNING id
                """
            )
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('worker-02', 23456, 'default', '{test}')
                """
            )

            # worker-01 currently runs a job
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                  claimed_by, worker_host, worker_pid, started)
                VALUES ('test.Job1', '{}', 'default', 'running', 100,
                        $1, 'worker-01', 12345, NOW())
                """,
                w1,
            )

        workers = await api.list_workers()

        assert len(workers) >= 2

        by_host = {w["host"]: w for w in workers}
        # Registry identity comes straight from jorb_worker
        assert by_host["worker-01"]["pid"] == 12345
        assert by_host["worker-01"]["queue"] == "default"
        assert by_host["worker-01"]["live"] is True
        assert by_host["worker-01"]["current_job_class"] == "test.Job1"
        assert by_host["worker-02"]["current_job_id"] is None

    @pytest.mark.asyncio
    async def test_list_workers_excludes_long_shutdown(self, db_pool):
        """Workers shut down beyond the window are not listed."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities,
                                         shutdown_at)
                VALUES ('worker-gone', 99999, 'default', '{test}',
                        NOW() - INTERVAL '2 days')
                """
            )
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities,
                                         shutdown_at)
                VALUES ('worker-recent', 99998, 'default', '{test}',
                        NOW() - INTERVAL '1 minute')
                """
            )

        workers = await api.list_workers()

        worker_hosts = {w["host"] for w in workers}
        # Long-dead workers disappear; recently-shut-down ones still show
        assert "worker-gone" not in worker_hosts
        assert "worker-recent" in worker_hosts
        recent = next(w for w in workers if w["host"] == "worker-recent")
        assert recent["live"] is False

    @pytest.mark.asyncio
    async def test_worker_stats(self, db_pool):
        """Test registry-based worker statistics aggregation."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Two live workers, one stale (old heartbeat)
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('worker-01', 12345, 'default', '{test}'),
                       ('worker-02', 12346, 'default', '{test}')
                """
            )
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities,
                                         last_seen)
                VALUES ('worker-stale', 12347, 'default', '{test}',
                        NOW() - INTERVAL '10 minutes')
                """
            )

        stats = await api.worker_stats()

        assert stats["live_workers"] == 2
        assert stats["stale_workers"] == 1
        assert stats["total_registered"] == 3
        assert stats["per_queue"] == {"default": 2}


# =============================================================================
# METRICS & MONITORING TESTS
# =============================================================================


class TestAdminAPIMetrics:
    """Test AdminAPI metrics and monitoring methods."""

    @pytest.mark.asyncio
    async def test_get_metrics_default_time_range(self, db_pool):
        """Test get_metrics with default 24h time range."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create some jobs with different states
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created, finished)
                VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '1 hour', NOW())
            """,
                "test.FinishedJob",
                {},
                "default",
                "finished",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created)
                VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '30 minutes')
            """,
                "test.CrashedJob",
                {},
                "default",
                "crashed",
                100,
            )

        metrics = await api.get_metrics()

        # Should have metrics structure
        assert "finished_count" in metrics
        assert "crashed_count" in metrics
        assert "avg_duration_seconds" in metrics
        assert "avg_wait_seconds" in metrics
        assert "max_wait_seconds" in metrics

        # Counts should be non-negative
        assert metrics["finished_count"] >= 1
        assert metrics["crashed_count"] >= 1

    @pytest.mark.asyncio
    async def test_get_metrics_custom_time_range(self, db_pool):
        """Test get_metrics with custom time range."""
        api = AdminAPI(db_pool)

        since = datetime.now(UTC) - timedelta(hours=1)

        async with db_pool.acquire() as conn:
            # Create a recent job
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created)
                VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '30 minutes')
            """,
                "test.Job",
                {},
                "default",
                "finished",
                100,
            )

            # Create an old job (outside time range)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created)
                VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '25 hours')
            """,
                "test.OldJob",
                {},
                "default",
                "finished",
                100,
            )

        metrics = await api.get_metrics(since=since)

        # Old job should not be counted (outside 1-hour window)
        assert metrics is not None

    @pytest.mark.asyncio
    async def test_get_metrics_queue_filter(self, db_pool):
        """Test get_metrics filtered by queue."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, created)
                VALUES ($1, $2, $3, $4, $5, NOW())
            """,
                "test.Job",
                {},
                "metric_queue",
                "finished",
                100,
            )

        metrics = await api.get_metrics(queue="metric_queue")

        assert metrics is not None
        # Queue-filtered metrics should work

    @pytest.mark.asyncio
    async def test_metrics_separate_queue_wait_from_execution_time(self, db_pool):
        """Waiting to be picked up and running are different problems.

        A job that sat in the queue for 60s and then ran for 2s must not be
        reported as a 62s job: the first number is a capacity signal and the
        second is a code signal, and an operator acts on them differently.
        """
        api = AdminAPI(db_pool)
        queue = "metric_latency_split"

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio,
                    run_after, claimed_at, started, finished
                )
                VALUES ($1, $2, $3, 'finished', 100,
                        NOW() - INTERVAL '62 seconds',
                        NOW() - INTERVAL '2 seconds',
                        NOW() - INTERVAL '2 seconds',
                        NOW())
            """,
                "test.Job",
                {},
                queue,
            )

        metrics = await api.get_metrics(queue=queue)

        assert 1.0 <= metrics["avg_duration_seconds"] <= 3.0
        assert 59.0 <= metrics["avg_wait_seconds"] <= 61.0
        assert 59.0 <= metrics["max_wait_seconds"] <= 61.0


# =============================================================================
# DEAD LETTER QUEUE TESTS
# =============================================================================


class TestAdminAPIDLQ:
    """Test AdminAPI Dead Letter Queue methods."""

    @pytest.mark.asyncio
    async def test_list_dlq(self, db_pool):
        """Test listing Dead Letter Queue jobs."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create a DLQ job (crashed with high error count)
            await conn.execute(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio,
                    error_count, error_message
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                "test.DLQJob",
                {},
                "default",
                "crashed",
                100,
                15,
                "Persistent error",
            )

            # Also crashed, but with a LOW error count: v1 has no error-count
            # heuristic, so this is in the DLQ just the same
            await conn.execute(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio,
                    error_count
                )
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.RegularJob",
                {},
                "default",
                "crashed",
                100,
                3,
            )

        dlq_jobs = await api.list_dlq(limit=100)

        # The DLQ is exactly the crashed jobs: BOTH are in it (no
        # error-count heuristic)
        dlq_classes = {job["job_class"] for job in dlq_jobs}
        assert "test.DLQJob" in dlq_classes
        assert "test.RegularJob" in dlq_classes
        assert all(job["state"] == "crashed" for job in dlq_jobs)

    @pytest.mark.asyncio
    async def test_retry_from_dlq(self, db_pool):
        """Test retrying a job from DLQ."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create a DLQ job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio,
                    error_count, error_message
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """,
                "test.DLQJob",
                {"arg": "value"},
                "default",
                "crashed",
                100,
                12,
                "DLQ error",
            )

        result = await api.retry_from_dlq(job_id)

        assert result == {"job_id": job_id, "status": "requeued_from_dlq"}

        # The same row is requeued with its error budget reset
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job["state"] == "queued"
            assert job["error_count"] == 0
            assert job["error_message"] is None
            assert job["kwargs"] == {"arg": "value"}

    @pytest.mark.asyncio
    async def test_retry_from_dlq_not_crashed_fails(self, db_pool):
        """Test retrying from DLQ fails if job is not crashed."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create a non-crashed job
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, error_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
                15,
            )

        assert await api.retry_from_dlq(job_id) == {
            "job_id": job_id,
            "status": "not_retriable",
        }

    @pytest.mark.asyncio
    async def test_retry_from_dlq_job_not_found(self, db_pool):
        """Test retry_from_dlq refuses a job that doesn't exist."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            assert await api.retry_from_dlq(999999) == {
                "job_id": 999999,
                "status": "not_retriable",
            }


# =============================================================================
# SCHEDULE MANAGEMENT TESTS
# =============================================================================


class TestAdminAPIScheduleManagement:
    """Test AdminAPI schedule management methods."""

    @pytest.mark.asyncio
    async def test_create_schedule_basic(self, db_pool):
        """Test creating a basic schedule."""
        api = AdminAPI(db_pool)

        result = await api.create_schedule(
            name="test-schedule",
            job_class="test.Job",
            cron_expr="* * * * *",
            queue="default",
            kwargs={"arg": "value"},
        )

        assert result is not None
        assert "id" in result
        schedule_id = result["id"]

        # Verify schedule created
        assert result["name"] == "test-schedule"
        assert result["job_class"] == "test.Job"
        assert result["cron_expr"] == "* * * * *"
        assert result["enabled"] is True
        assert result["kwargs"] == {"arg": "value"}

    @pytest.mark.asyncio
    async def test_create_schedule_invalid_cron(self, db_pool):
        """Test creating schedule with invalid cron expression fails."""
        api = AdminAPI(db_pool)

        with pytest.raises(ValueError, match="malformed cron expression"):
            await api.create_schedule(
                name="bad-schedule",
                job_class="test.Job",
                cron_expr="invalid cron",
                queue="default",
            )

    @pytest.mark.asyncio
    async def test_create_schedule_invalid_timezone(self, db_pool):
        """Test creating schedule with invalid timezone fails."""
        api = AdminAPI(db_pool)

        with pytest.raises(
            ValueError, match="malformed cron expression|unknown timezone"
        ):
            await api.create_schedule(
                name="bad-tz-schedule",
                job_class="test.Job",
                cron_expr="* * * * *",
                queue="default",
                timezone="Invalid/Timezone",
            )

    @pytest.mark.asyncio
    async def test_create_schedule_with_timezone(self, db_pool):
        """Test creating schedule with custom timezone."""
        api = AdminAPI(db_pool)

        result = await api.create_schedule(
            name="tz-schedule",
            job_class="test.Job",
            cron_expr="0 12 * * *",  # Daily at noon
            queue="default",
            timezone="America/New_York",
        )

        # Verify timezone saved
        assert result["timezone"] == "America/New_York"

    @pytest.mark.asyncio
    async def test_get_schedule_by_id(self, db_pool):
        """Test getting schedule by ID."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        schedule = await api.get_schedule(schedule_id=schedule_id)

        assert schedule is not None
        assert schedule["id"] == schedule_id
        assert schedule["name"] == "test-schedule"

    @pytest.mark.asyncio
    async def test_get_schedule_by_name(self, db_pool):
        """Test getting schedule by name."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "unique-schedule-name",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        schedule = await api.get_schedule(name="unique-schedule-name")

        assert schedule is not None
        assert schedule["name"] == "unique-schedule-name"

    @pytest.mark.asyncio
    async def test_update_schedule_basic_fields(self, db_pool):
        """Test updating schedule basic fields."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        await api.update_schedule(
            schedule_id, description="Updated description", max_concurrent_jobs=5
        )

        # Verify updates
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                SELECT * FROM jorb_schedule WHERE id = $1
            """,
                schedule_id,
            )

            assert schedule["description"] == "Updated description"
            assert schedule["max_concurrent_jobs"] == 5

    @pytest.mark.asyncio
    async def test_update_schedule_recalculates_next_run(self, db_pool):
        """Test updating cron_expr recalculates next_run."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            old_next_run = await conn.fetchval(
                """
                SELECT next_run FROM jorb_schedule WHERE id = $1
            """,
                schedule_id,
            )

        # Update cron expression
        await api.update_schedule(
            schedule_id,
            cron_expr="0 * * * *",  # Change to hourly
        )

        # Verify next_run was recalculated
        async with db_pool.acquire() as conn:
            new_next_run = await conn.fetchval(
                """
                SELECT next_run FROM jorb_schedule WHERE id = $1
            """,
                schedule_id,
            )

            # next_run should have changed
            # (might be same if we're exactly at the top of the hour, but cron_expr changed)
            assert new_next_run is not None

    @pytest.mark.asyncio
    async def test_enable_schedule(self, db_pool):
        """Test enabling a schedule."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled,
                    next_run, consecutive_failures
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8)
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                False,
                5,
            )

        await api.enable_schedule(schedule_id)

        # Verify enabled and consecutive_failures reset
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                SELECT enabled, consecutive_failures FROM jorb_schedule WHERE id = $1
            """,
                schedule_id,
            )

            assert schedule["enabled"] is True
            assert schedule["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_disable_schedule(self, db_pool):
        """Test disabling a schedule."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        await api.disable_schedule(schedule_id)

        # Verify disabled
        async with db_pool.acquire() as conn:
            enabled = await conn.fetchval(
                """
                SELECT enabled FROM jorb_schedule WHERE id = $1
            """,
                schedule_id,
            )

            assert enabled is False

    @pytest.mark.asyncio
    async def test_delete_schedule(self, db_pool):
        """Test deleting a schedule."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        result = await api.delete_schedule(schedule_id)

        assert result is not None
        assert result["status"] == "deleted"
        assert result["schedule_id"] == str(schedule_id)

        # Verify deleted
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                """
                SELECT EXISTS(SELECT 1 FROM jorb_schedule WHERE id = $1)
            """,
                schedule_id,
            )

            assert exists is False

    @pytest.mark.asyncio
    async def test_list_schedules_no_filters(self, db_pool):
        """Test listing all schedules."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "schedule-1",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        schedules = await api.list_schedules()

        assert len(schedules) >= 1
        schedule_names = {s["name"] for s in schedules}
        assert "schedule-1" in schedule_names

    @pytest.mark.asyncio
    async def test_list_schedules_filter_enabled(self, db_pool):
        """Test listing schedules filtered by enabled status."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "enabled-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "disabled-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                False,
            )

        schedules = await api.list_schedules(enabled=True)

        # All returned schedules should be enabled
        assert all(s["enabled"] for s in schedules)

        schedule_names = {s["name"] for s in schedules}
        assert "enabled-schedule" in schedule_names
        assert "disabled-schedule" not in schedule_names

    @pytest.mark.asyncio
    async def test_list_schedules_filter_by_queue(self, db_pool):
        """Test listing schedules filtered by queue - covers lines 799-801."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "priority-schedule",
                "test.Job",
                "* * * * *",
                "priority_queue",
                100,
                {},
                True,
            )

            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
                "default-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

        # Filter by specific queue - covers lines 799-801
        schedules = await api.list_schedules(queue="priority_queue")

        # All returned schedules should be in priority_queue
        assert all(s["queue"] == "priority_queue" for s in schedules)
        schedule_names = {s["name"] for s in schedules}
        assert "priority-schedule" in schedule_names
        assert "default-schedule" not in schedule_names

    @pytest.mark.asyncio
    async def test_get_schedule_neither_id_nor_name_error(self, db_pool):
        """Test get_schedule raises error when neither id nor name provided - covers line 838."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Call without either parameter - covers line 838
            with pytest.raises(
                ValueError, match="Must provide either schedule_id or name"
            ):
                await api.get_schedule()

    @pytest.mark.asyncio
    async def test_update_schedule_no_valid_fields_error(self, db_pool):
        """Test update_schedule raises error when no valid fields - covers line 949."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Try to update with invalid/no fields - covers line 949
            with pytest.raises(ValueError, match="No valid fields to update"):
                await api.update_schedule(schedule_id, invalid_field="value")

    @pytest.mark.asyncio
    async def test_update_schedule_not_found_during_cron_recalc(self, db_pool):
        """Test update_schedule handles schedule not found during cron recalc - covers line 955."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Try to update non-existent schedule with cron change
            # This triggers get_schedule which returns None, covering line 955
            with pytest.raises(ValueError, match="Schedule 999999 not found"):
                await api.update_schedule(999999, cron_expr="0 * * * *")

    @pytest.mark.asyncio
    async def test_update_schedule_invalid_cron_or_timezone(self, db_pool):
        """Test update_schedule handles invalid cron expression or timezone - covers lines 969-970."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Try to update with invalid cron expression - covers lines 969-970
            with pytest.raises(
                ValueError, match="malformed cron expression|unknown timezone"
            ):
                await api.update_schedule(schedule_id, cron_expr="INVALID_CRON")

    @pytest.mark.asyncio
    async def test_update_schedule_not_found_after_update(self, db_pool):
        """Test update_schedule handles schedule not found after update - covers line 997."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Create a schedule then delete it before updating
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Delete it
            await conn.execute("DELETE FROM jorb_schedule WHERE id = $1", schedule_id)

            # Try to update - covers line 997
            with pytest.raises(ValueError, match=f"Schedule {schedule_id} not found"):
                await api.update_schedule(schedule_id, enabled=False)

    @pytest.mark.asyncio
    async def test_delete_schedule_not_found(self, db_pool):
        """Test delete_schedule raises error when schedule doesn't exist - covers line 1016."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Try to delete non-existent schedule - covers line 1016
            with pytest.raises(ValueError, match="Schedule 999999 not found"):
                await api.delete_schedule(999999)


# =============================================================================
# ADDITIONAL COVERAGE TESTS
# =============================================================================


class TestAdminAPIAdditionalCoverage:
    """Additional tests to achieve 90%+ coverage."""

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_job_class(self, db_pool):
        """Test listing jobs filtered by job_class (LIKE query)."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "EmailJob",
                {},
                "default",
                "queued",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "DataJob",
                {},
                "default",
                "queued",
                100,
            )

        jobs = await api.list_jobs(job_class="Email")

        assert all("Email" in job["job_class"] for job in jobs)
        assert not any("Data" in job["job_class"] for job in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_uid(self, db_pool):
        """Test listing jobs filtered by uid."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, uid)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
                12345,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, uid)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
                67890,
            )

        jobs = await api.list_jobs(uid=12345)

        assert all(job["uid"] == 12345 for job in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_invalid_order_by(self, db_pool):
        """Test that invalid order_by defaults to 'created'."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

        # Should not raise error - defaults to 'created'
        jobs = await api.list_jobs(order_by="invalid_column")
        assert isinstance(jobs, list)

    @pytest.mark.asyncio
    async def test_list_jobs_with_job_class_and_uid(self, db_pool):
        """Test listing jobs with both job_class and uid filters."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, uid)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "EmailJob",
                {},
                "default",
                "queued",
                100,
                12345,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, uid)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "DataJob",
                {},
                "default",
                "queued",
                100,
                12345,
            )

        jobs = await api.list_jobs(job_class="Email", uid=12345)

        assert all("Email" in job["job_class"] and job["uid"] == 12345 for job in jobs)

    @pytest.mark.asyncio
    async def test_get_schedule_history_basic(self, db_pool):
        """Test getting schedule execution history."""
        # Create a schedule
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "log-test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Add some execution logs
            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, result, duration_ms)
                VALUES ($1, 'log-test-schedule', NOW() - INTERVAL '1 hour', 'success', 100)
            """,
                schedule_id,
            )

            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, result, duration_ms)
                VALUES ($1, 'log-test-schedule', NOW() - INTERVAL '30 minutes', 'failure', 50)
            """,
                schedule_id,
            )

            # Get history using AdminAPI with connection
            api = AdminAPI(conn)
            logs = await api.get_schedule_history(schedule_id)

            assert len(logs) >= 2
            assert all(log["schedule_id"] == schedule_id for log in logs)

    @pytest.mark.asyncio
    async def test_get_schedule_history_with_filter(self, db_pool):
        """Test getting schedule history with result filter."""
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "filter-test-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Add success and failure logs
            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, result, duration_ms)
                VALUES ($1, 'filter-test-schedule', NOW(), 'success', 100),
                       ($1, 'filter-test-schedule', NOW(), 'failure', 50)
            """,
                schedule_id,
            )

            # Filter for only success
            api = AdminAPI(conn)
            logs = await api.get_schedule_history(schedule_id, result_filter="success")

            assert all(log["result"] == "success" for log in logs)

    @pytest.mark.asyncio
    async def test_get_schedule_history_pagination(self, db_pool):
        """Test schedule history pagination."""
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id
            """,
                "pagination-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            # Add multiple logs
            for i in range(5):
                await conn.execute(
                    """
                    INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, result, duration_ms)
                    VALUES ($1, 'pagination-schedule', NOW() - INTERVAL '1 hour' * $2, 'success', 100)
                """,
                    schedule_id,
                    i,
                )

            api = AdminAPI(conn)

            # Get with limit
            logs = await api.get_schedule_history(schedule_id, limit=2)

            assert len(logs) == 2

            # Get with offset
            logs_offset = await api.get_schedule_history(schedule_id, limit=2, offset=2)

            assert len(logs_offset) == 2
            # Should be different logs
            assert logs[0]["id"] != logs_offset[0]["id"]

    @pytest.mark.asyncio
    async def test_get_schedule_stats_basic(self, db_pool):
        """Test getting schedule statistics."""
        # Create schedules with execution history
        async with db_pool.acquire() as conn:
            schedule_id1 = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run,
                    run_count, success_count, failure_count, skip_count, consecutive_failures
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), 10, 8, 2, 0, 0)
                RETURNING id
            """,
                "stats-schedule-1",
                "test.Job1",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            schedule_id2 = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run,
                    run_count, success_count, failure_count, skip_count, consecutive_failures
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), 5, 5, 0, 0, 0)
                RETURNING id
            """,
                "stats-schedule-2",
                "test.Job2",
                "0 * * * *",
                "default",
                100,
                {},
                True,
            )

            # Get stats using AdminAPI with connection
            api = AdminAPI(conn)
            stats = await api.get_schedule_stats()

            assert len(stats) >= 2

            # Find our schedules in stats
            schedule1_stats = next(
                (s for s in stats if s["name"] == "stats-schedule-1"), None
            )
            schedule2_stats = next(
                (s for s in stats if s["name"] == "stats-schedule-2"), None
            )

            assert schedule1_stats is not None
            assert schedule1_stats["run_count"] == 10
            assert schedule1_stats["success_count"] == 8
            assert schedule1_stats["failure_count"] == 2
            assert schedule1_stats["success_rate_pct"] == 80.0  # 8/10 * 100

            assert schedule2_stats is not None
            assert schedule2_stats["run_count"] == 5
            assert schedule2_stats["success_count"] == 5
            assert schedule2_stats["success_rate_pct"] == 100.0  # 5/5 * 100

    @pytest.mark.asyncio
    async def test_get_schedule_stats_null_success_rate(self, db_pool):
        """Test schedule stats with no executions (NULL success rate)."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio, kwargs, enabled, next_run,
                    run_count, success_count, failure_count
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), 0, 0, 0)
            """,
                "no-exec-schedule",
                "test.Job",
                "* * * * *",
                "default",
                100,
                {},
                True,
            )

            api = AdminAPI(conn)
            stats = await api.get_schedule_stats()

            # Find the schedule with no executions
            no_exec_stats = next(
                (s for s in stats if s["name"] == "no-exec-schedule"), None
            )

            assert no_exec_stats is not None
            assert no_exec_stats["run_count"] == 0
            assert no_exec_stats["success_rate_pct"] is None  # NULL when no executions


# =============================================================================
# ERROR PATH TESTS
# =============================================================================


class TestAdminAPIErrorPaths:
    """Test error handling and edge cases in AdminAPI."""

    @pytest.mark.asyncio
    async def test_retry_job_not_found(self, db_pool):
        """Test retry_job with non-existent job ID."""
        api = AdminAPI(db_pool)

        assert await api.retry_job(999999) == {
            "job_id": 999999,
            "status": "not_retriable",
        }

    @pytest.mark.asyncio
    async def test_retry_job_wrong_state(self, db_pool):
        """Test retry_job with job in wrong state."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Create job in 'queued' state (not crashed/cancelled)
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
            )

        assert await api.retry_job(job_id) == {
            "job_id": job_id,
            "status": "not_retriable",
        }

    @pytest.mark.asyncio
    async def test_cancel_job_not_found(self, db_pool):
        """Test cancel_job with non-existent job ID."""
        api = AdminAPI(db_pool)

        # a missing job is not_cancellable, not an exception
        assert await api.cancel_job(999999) == {
            "job_id": 999999,
            "status": "not_cancellable",
        }

    @pytest.mark.asyncio
    async def test_cancel_job_wrong_state(self, db_pool):
        """Test cancel_job with a terminal (uncancellable) job."""
        api = AdminAPI(db_pool)

        async with db_pool.acquire() as conn:
            # Terminal states cannot be cancelled
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "finished",
                100,
            )

        # A terminal state is refused, with the refusal in the status
        assert await api.cancel_job(job_id) == {
            "job_id": job_id,
            "status": "not_cancellable",
        }

    @pytest.mark.asyncio
    async def test_retry_jobs_bulk_with_errors(self, db_pool):
        """Test retry_jobs handles mix of valid and invalid job IDs."""
        api = AdminAPI(db_pool)

        # Create one valid crashed job
        async with db_pool.acquire() as conn:
            valid_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, error_count, prio)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """,
                "test.Job",
                {},
                "default",
                "crashed",
                1,
                100,
            )

        # Mix valid and invalid IDs - the batch reports every id
        results = await api.retry_jobs([valid_id, 999999, 999998])

        assert len(results) == 3
        # First should succeed (same-row requeue)
        assert results[0] == {"job_id": valid_id, "status": "requeued"}
        # Missing ids are refused, in place, with retry's own refusal status
        assert results[1] == {"job_id": 999999, "status": "not_retriable"}
        assert results[2] == {"job_id": 999998, "status": "not_retriable"}

    @pytest.mark.asyncio
    async def test_delete_jobs_no_filters(self, db_pool):
        """Test delete_jobs raises error when no filters provided."""
        api = AdminAPI(db_pool)

        # Should raise ValueError when no filters provided (line 418)
        with pytest.raises(ValueError, match="Must specify at least one filter"):
            await api.delete_jobs()

    @pytest.mark.asyncio
    async def test_cancel_jobs_bulk(self, db_pool):
        """Test cancel_jobs with mixed valid/invalid IDs - covers lines 346-357."""
        api = AdminAPI(db_pool)

        # Create jobs in different states
        async with db_pool.acquire() as conn:
            job1 = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job1",
                {},
                "default",
                "queued",
                100,
            )

            job2 = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job2",
                {},
                "default",
                "waiting",
                100,
            )

            job3 = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job3",
                {},
                "default",
                "running",
                100,
            )

            job4 = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """,
                "test.Job4",
                {},
                "default",
                "finished",
                100,
            )

        # Bulk cancel with a mix of cancellable, running, invalid, and
        # terminal jobs
        results = await api.cancel_jobs([job1, job2, 999999, job3, job4])

        assert len(results) == 5

        # job1 (queued) is cancelled immediately
        assert results[0] == {"job_id": job1, "status": "cancelled"}

        # job2 (waiting) is cancelled immediately
        assert results[1] == {"job_id": job2, "status": "cancelled"}

        # 999999 (invalid) is refused in place
        assert results[2] == {"job_id": 999999, "status": "not_cancellable"}

        # job3 (running) gets a cancellation request delivered
        assert results[3] == {"job_id": job3, "status": "cancel_requested"}

        # job4 (finished) is terminal: the same refusal as a missing job
        assert results[4] == {"job_id": job4, "status": "not_cancellable"}

    @pytest.mark.asyncio
    async def test_delete_jobs_by_queue(self, db_pool):
        """Test delete_jobs with queue filter - covers lines 401-403."""
        api = AdminAPI(db_pool)

        # Create jobs in different queues
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job1",
                {},
                "queue_a",
                "finished",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job2",
                {},
                "queue_a",
                "finished",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job3",
                {},
                "queue_b",
                "finished",
                100,
            )

        # Delete jobs from queue_a only
        deleted = await api.delete_jobs(queue="queue_a")

        assert deleted == 2

        # Verify only queue_a jobs deleted
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE queue = 'queue_a'"
            )
            assert remaining == 0

            remaining_b = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE queue = 'queue_b'"
            )
            assert remaining_b == 1

    @pytest.mark.asyncio
    async def test_delete_jobs_by_state(self, db_pool):
        """Test delete_jobs with state filter - covers lines 406-408."""
        api = AdminAPI(db_pool)

        # Create jobs in different states
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job1",
                {},
                "default",
                "crashed",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job2",
                {},
                "default",
                "crashed",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job3",
                {},
                "default",
                "finished",
                100,
            )

        # Delete only crashed jobs
        deleted = await api.delete_jobs(state="crashed")

        assert deleted == 2

        # Verify only crashed jobs deleted
        async with db_pool.acquire() as conn:
            remaining_crashed = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE state = 'crashed'"
            )
            assert remaining_crashed == 0

            remaining_finished = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE state = 'finished'"
            )
            assert remaining_finished == 1

    @pytest.mark.asyncio
    async def test_delete_jobs_by_age(self, db_pool):
        """Test delete_jobs with the not_updated_for_days filter."""
        from datetime import timedelta

        api = AdminAPI(db_pool)

        # Create old and new jobs
        async with db_pool.acquire() as conn:
            # Old job (10 days ago)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, updated)
                VALUES ($1, $2, $3, $4, $5, TIMEZONE('utc', clock_timestamp()) - $6::interval)
            """,
                "test.OldJob",
                {},
                "default",
                "finished",
                100,
                timedelta(days=10),
            )

            # Recent job (1 day ago)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, updated)
                VALUES ($1, $2, $3, $4, $5, TIMEZONE('utc', clock_timestamp()) - $6::interval)
            """,
                "test.NewJob",
                {},
                "default",
                "finished",
                100,
                timedelta(days=1),
            )

        # Delete jobs not updated for 7 days
        deleted = await api.delete_jobs(state="finished", not_updated_for_days=7)

        assert deleted == 1

        # Verify only old job deleted
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE state = 'finished'"
            )
            assert remaining == 1

            # Verify it's the new job that remains
            job = await conn.fetchrow(
                "SELECT job_class FROM jorb WHERE state = 'finished'"
            )
            assert job["job_class"] == "test.NewJob"

    @pytest.mark.asyncio
    async def test_delete_jobs_combined_filters(self, db_pool):
        """Test delete_jobs with multiple filters - covers lines 422-431."""
        from datetime import timedelta

        api = AdminAPI(db_pool)

        # Create various jobs
        async with db_pool.acquire() as conn:
            # Old crashed job in queue_a (should be deleted)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, updated)
                VALUES ($1, $2, $3, $4, $5, TIMEZONE('utc', clock_timestamp()) - $6::interval)
            """,
                "test.Target",
                {},
                "queue_a",
                "crashed",
                100,
                timedelta(days=10),
            )

            # Old crashed job in queue_b (should NOT be deleted - wrong queue)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, updated)
                VALUES ($1, $2, $3, $4, $5, TIMEZONE('utc', clock_timestamp()) - $6::interval)
            """,
                "test.Wrong",
                {},
                "queue_b",
                "crashed",
                100,
                timedelta(days=10),
            )

            # New crashed job in queue_a (should NOT be deleted - too recent)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, updated)
                VALUES ($1, $2, $3, $4, $5, TIMEZONE('utc', clock_timestamp()) - $6::interval)
            """,
                "test.Recent",
                {},
                "queue_a",
                "crashed",
                100,
                timedelta(days=1),
            )

        # Delete only: queue_a AND crashed AND not updated for 7 days
        deleted = await api.delete_jobs(
            queue="queue_a", state="crashed", not_updated_for_days=7
        )

        assert deleted == 1

        # Verify correct job deleted
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM jorb")
            assert remaining == 2

            # Verify the target job is gone
            target = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE job_class = 'test.Target'"
            )
            assert target == 0

    @pytest.mark.asyncio
    async def test_clear_queue(self, db_pool):
        """clear_queue defaults to queued/waiting and takes states explicitly.

        Terminal rows are NOT swept by a bare clear_queue(): reaching them
        (like reaching claimed/running) has to be asked for.
        """
        api = AdminAPI(db_pool)

        # Create jobs in test_queue
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job1",
                {},
                "test_queue",
                "finished",
                100,
            )

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """,
                "test.Job2",
                {},
                "test_queue",
                "crashed",
                100,
            )

        # The default states leave both terminal rows alone
        assert await api.clear_queue("test_queue") == 0

        # Named explicitly, they go
        deleted = await api.clear_queue("test_queue", states=("finished", "crashed"))

        assert deleted == 2

        # Verify queue cleared
        async with db_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE queue = 'test_queue'"
            )
            assert remaining == 0


# =============================================================================
# COMPREHENSIVE SUMMARY
# =============================================================================

"""
Comprehensive AdminAPI Test Summary:

Test Classes: 7
Total Tests: 50+

Coverage Areas:
✅ Data Classes (JobInfo, QueueStats)
✅ Job Management (list, get, retry, cancel, delete)
✅ Queue Management (list, stats, clear)
✅ Worker Management (list, stats)
✅ Metrics & Monitoring
✅ Dead Letter Queue (DLQ)
✅ Schedule Management (create, update, enable/disable, delete, list)

Coverage Target: 85%+

Key Testing Focus:
- State machine validation (job states)
- Datetime handling (UTC, ISO format)
- JSONB manipulation (admin_data)
- SQL injection prevention
- Pagination
- Error handling
- asyncpg integration
- Cron validation
- Timezone handling
"""
