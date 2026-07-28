"""
Comprehensive tests for admin_api.py - Administrative API.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.admin_api import (
    AdminAPI,
    JobInfo,
    QueueStats,
)
from tests.conftest import unique_name


class TestJobInfoDataclass:
    """Test JobInfo dataclass - covers lines 18-62."""

    @pytest.mark.asyncio
    async def test_job_info_from_record(self, db_pool):
        """Test creating JobInfo from database record."""
        async with db_pool.acquire() as conn:
            # Create a test job
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('TestJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

            record = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            job_info = JobInfo.from_record(record)

            assert job_info.id == job_id
            assert job_info.job_class == "TestJob"
            assert job_info.state == "queued"
            assert job_info.queue == "test"
            assert job_info.prio == 100

    @pytest.mark.asyncio
    async def test_job_info_to_dict(self, db_pool):
        """Test converting JobInfo to dictionary with datetime serialization."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('TestJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

            record = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            job_info = JobInfo.from_record(record)
            job_dict = job_info.to_dict()

            # Check datetime fields are serialized to ISO strings
            assert isinstance(job_dict["created"], str)
            assert isinstance(job_dict["updated"], str)
            assert "T" in job_dict["created"]  # ISO format has T separator


class TestQueueStatsDataclass:
    """Test QueueStats dataclass - covers lines 64-80."""

    def test_queue_stats_defaults(self):
        """Test QueueStats has correct defaults."""
        stats = QueueStats(queue="test_queue")
        assert stats.queue == "test_queue"
        assert stats.queued == 0
        assert stats.claimed == 0
        assert stats.running == 0
        assert stats.waiting == 0
        assert stats.finished == 0
        assert stats.crashed == 0
        assert stats.cancelled == 0
        assert stats.total == 0
        assert stats.oldest_queued_age_seconds is None

    def test_queue_stats_to_dict(self):
        """Test QueueStats to_dict."""
        stats = QueueStats(queue="test", queued=5, running=2, total=7)
        stats_dict = stats.to_dict()

        assert stats_dict["queue"] == "test"
        assert stats_dict["queued"] == 5
        assert stats_dict["running"] == 2
        assert stats_dict["total"] == 7


class TestAdminAPIJobManagement:
    """Test AdminAPI job management methods - covers lines 129-431."""

    @pytest.mark.asyncio
    async def test_list_jobs_no_filters(self, db_pool):
        """Test listing jobs without filters."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Create test jobs
            queue = unique_name("list_test")
            for i in range(3):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', $2, 100, 'queued')
                """,
                    f"TestJob{i}",
                    queue,
                )

            jobs = await api.list_jobs(queue=queue)
            assert len(jobs) == 3

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_state(self, db_pool):
        """Test listing jobs filtered by state."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("state_filter")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job1', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job2', '{}', $1, 100, 'crashed')
            """,
                queue,
            )

            queued_jobs = await api.list_jobs(queue=queue, state="queued")
            assert len(queued_jobs) == 1
            assert queued_jobs[0]["job_class"] == "Job1"

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_job_class(self, db_pool):
        """Test listing jobs filtered by job_class pattern."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("class_filter")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('EmailJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('SmsJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

            email_jobs = await api.list_jobs(queue=queue, job_class="Email")
            assert len(email_jobs) == 1
            assert "Email" in email_jobs[0]["job_class"]

    @pytest.mark.asyncio
    async def test_list_jobs_filter_by_uid(self, db_pool):
        """Test listing jobs filtered by uid."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("uid_filter")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, uid)
                VALUES ('Job1', '{}', $1, 100, 'queued', 123)
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, uid)
                VALUES ('Job2', '{}', $1, 100, 'queued', 456)
            """,
                queue,
            )

            uid_jobs = await api.list_jobs(queue=queue, uid=123)
            assert len(uid_jobs) == 1

    @pytest.mark.asyncio
    async def test_list_jobs_pagination(self, db_pool):
        """Test listing jobs with pagination."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("pagination")

            for i in range(5):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', $2, 100, 'queued')
                """,
                    f"Job{i}",
                    queue,
                )

            page1 = await api.list_jobs(queue=queue, limit=2, offset=0)
            page2 = await api.list_jobs(queue=queue, limit=2, offset=2)

            assert len(page1) == 2
            assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_list_jobs_order_by(self, db_pool):
        """Test listing jobs with custom ordering."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("ordering")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('LowPrio', '{}', $1, 10, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HighPrio', '{}', $1, 1000, 'queued')
            """,
                queue,
            )

            jobs_asc = await api.list_jobs(
                queue=queue, order_by="prio", order_dir="ASC"
            )
            assert jobs_asc[0]["job_class"] == "LowPrio"

            jobs_desc = await api.list_jobs(
                queue=queue, order_by="prio", order_dir="DESC"
            )
            assert jobs_desc[0]["job_class"] == "HighPrio"

    @pytest.mark.asyncio
    async def test_list_jobs_invalid_order_by(self, db_pool):
        """Test listing jobs with invalid order_by defaults to 'created'."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("invalid_order")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('TestJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

            # Should not raise - defaults to 'created'
            jobs = await api.list_jobs(queue=queue, order_by="invalid_column")
            assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_get_job(self, db_pool):
        """Test getting single job by ID."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('GetTestJob', '{"key": "value"}', 'test', 100, 'queued')
                RETURNING id
            """)

            job = await api.get_job(job_id)
            assert job is not None
            assert job["id"] == job_id
            assert job["job_class"] == "GetTestJob"

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, db_pool):
        """Test getting non-existent job returns None."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            job = await api.get_job(99999999)
            assert job is None

    @pytest.mark.asyncio
    async def test_retry_job(self, db_pool):
        """Test retrying a crashed job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CrashedJob', '{"x": 1}', 'test', 100, 'crashed')
                RETURNING id
            """)

            result = await api.retry_job(job_id)

            assert result == {"job_id": job_id, "status": "requeued"}

            # The SAME row is requeued (job ids are stable across retries)
            job = await api.get_job(job_id)
            assert job["state"] == "queued"
            assert job["job_class"] == "CrashedJob"
            assert job["error_count"] == 0

    @pytest.mark.asyncio
    async def test_retry_job_not_found(self, db_pool):
        """Test retrying non-existent job raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.retry_job(99999999)
            assert "not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_retry_job_not_retriable(self, db_pool):
        """Test retrying job in non-retriable state raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('RunningJob', '{}', 'test', 100, 'running')
                RETURNING id
            """)

            with pytest.raises(ValueError) as excinfo:
                await api.retry_job(job_id)
            assert "running" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_retry_jobs_bulk(self, db_pool):
        """Test bulk retry of multiple jobs."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_ids = []
            for i in range(3):
                job_id = await conn.fetchval(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', 'test', 100, 'crashed')
                    RETURNING id
                """,
                    f"BulkRetryJob{i}",
                )
                job_ids.append(job_id)

            results = await api.retry_jobs(job_ids)

            assert len(results) == 3
            for result, job_id in zip(results, job_ids):
                assert result == {"job_id": job_id, "status": "requeued"}

    @pytest.mark.asyncio
    async def test_retry_jobs_partial_failure(self, db_pool):
        """Test bulk retry with some failures."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            crashed_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CrashedJob', '{}', 'test', 100, 'crashed')
                RETURNING id
            """)
            running_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('RunningJob', '{}', 'test', 100, 'running')
                RETURNING id
            """)

            results = await api.retry_jobs([crashed_id, running_id])

            assert len(results) == 2
            # First should succeed
            assert results[0] == {"job_id": crashed_id, "status": "requeued"}
            # Second should fail
            assert results[1]["status"] == "error"

    @pytest.mark.asyncio
    async def test_cancel_job(self, db_pool):
        """Test cancelling a queued job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CancelableJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

            result = await api.cancel_job(job_id)

            assert result["job_id"] == job_id
            assert result["status"] == "cancelled"

            # Verify job state
            job = await api.get_job(job_id)
            assert job["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_waiting_job(self, db_pool):
        """Test cancelling a waiting job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('WaitingJob', '{}', 'test', 100, 'waiting')
                RETURNING id
            """)

            result = await api.cancel_job(job_id)
            assert result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_job_not_found(self, db_pool):
        """Test cancelling non-existent job raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.cancel_job(99999999)
            assert "not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_cancel_running_job_requests_cancellation(self, db_pool):
        """Running jobs are cancellable now: cancel_requested is delivered."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('RunningJob', '{}', 'test', 100, 'running')
                RETURNING id
            """)

            result = await api.cancel_job(job_id)
            assert result == {"job_id": job_id, "status": "cancel_requested"}

            job = await api.get_job(job_id)
            assert job["state"] == "running"
            assert job["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_cancel_job_terminal_raises(self, db_pool):
        """Terminal jobs cannot be cancelled."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('DoneJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

            with pytest.raises(ValueError) as excinfo:
                await api.cancel_job(job_id)
            assert "cannot be cancelled" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_cancel_jobs_bulk(self, db_pool):
        """Test bulk cancel of multiple jobs."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_ids = []
            for i in range(3):
                job_id = await conn.fetchval(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', 'test', 100, 'queued')
                    RETURNING id
                """,
                    f"BulkCancelJob{i}",
                )
                job_ids.append(job_id)

            results = await api.cancel_jobs(job_ids)

            assert len(results) == 3
            for result in results:
                assert result["status"] == "cancelled", result

    @pytest.mark.asyncio
    async def test_delete_job(self, db_pool):
        """Test deleting a single job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('DeleteJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

            result = await api.delete_job(job_id)
            assert result is True

            # Verify job is deleted
            job = await api.get_job(job_id)
            assert job is None

    @pytest.mark.asyncio
    async def test_delete_job_not_found(self, db_pool):
        """Test deleting non-existent job returns False."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            result = await api.delete_job(99999999)
            assert result is False

    @pytest.mark.asyncio
    async def test_delete_jobs_by_queue_and_state(self, db_pool):
        """Test bulk delete jobs by queue and state."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("bulk_delete")

            for i in range(5):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', $2, 100, 'finished')
                """,
                    f"DeleteJob{i}",
                    queue,
                )

            deleted = await api.delete_jobs(queue=queue, state="finished")
            assert deleted == 5

    @pytest.mark.asyncio
    async def test_delete_jobs_no_filter_raises(self, db_pool):
        """Test bulk delete without filters raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.delete_jobs()
            assert "Must specify at least one filter" in str(excinfo.value)


class TestAdminAPIQueueManagement:
    """Test AdminAPI queue management methods - covers lines 437-536."""

    @pytest.mark.asyncio
    async def test_list_queues(self, db_pool):
        """Test listing all queue names."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Create jobs in different queues
            queue1 = unique_name("queue_a")
            queue2 = unique_name("queue_b")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job1', '{}', $1, 100, 'queued')
            """,
                queue1,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job2', '{}', $1, 100, 'queued')
            """,
                queue2,
            )

            queues = await api.list_queues()
            names = [q["name"] for q in queues]
            assert queue1 in names
            assert queue2 in names

    @pytest.mark.asyncio
    async def test_queue_stats(self, db_pool):
        """Test getting queue statistics."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("stats_queue")

            # Create jobs in various states
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job1', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job2', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job3', '{}', $1, 100, 'running')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job4', '{}', $1, 100, 'finished')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job5', '{}', $1, 100, 'crashed')
            """,
                queue,
            )

            stats = await api.queue_stats(queue=queue)
            assert len(stats) == 1

            stat = stats[0]
            assert stat["queue"] == queue
            assert stat["queued"] == 2
            assert stat["running"] == 1
            assert stat["finished"] == 1
            assert stat["crashed"] == 1
            assert stat["total"] == 5

    @pytest.mark.asyncio
    async def test_queue_stats_all_queues(self, db_pool):
        """Test getting stats for all queues."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue1 = unique_name("all_stats_a")
            queue2 = unique_name("all_stats_b")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job1', '{}', $1, 100, 'queued')
            """,
                queue1,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job2', '{}', $1, 100, 'running')
            """,
                queue2,
            )

            stats = await api.queue_stats()
            queue_names = [s["queue"] for s in stats]
            assert queue1 in queue_names
            assert queue2 in queue_names

    @pytest.mark.asyncio
    async def test_queue_stats_waiting_cancelled(self, db_pool):
        """Test queue stats includes waiting and cancelled states."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("all_states")

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job1', '{}', $1, 100, 'waiting')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job2', '{}', $1, 100, 'cancelled')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('Job3', '{}', $1, 100, 'claimed')
            """,
                queue,
            )

            stats = await api.queue_stats(queue=queue)
            stat = stats[0]

            assert stat["waiting"] == 1
            assert stat["cancelled"] == 1
            assert stat["claimed"] == 1

    @pytest.mark.asyncio
    async def test_clear_queue(self, db_pool):
        """Test clearing a queue."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("clear_queue")

            for i in range(5):
                await conn.execute(
                    """
                    INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                    VALUES ($1, '{}', $2, 100, 'finished')
                """,
                    f"ClearJob{i}",
                    queue,
                )

            deleted = await api.clear_queue(queue=queue, state="finished")
            assert deleted == 5


class TestAdminAPIWorkerManagement:
    """Test AdminAPI worker management methods (jorb_worker registry)."""

    @pytest.mark.asyncio
    async def test_list_workers(self, db_pool):
        """Workers come from the registry, joined with their claimed job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            worker_id = await conn.fetchval("""
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('host1', 12345, 'test', '{test}')
                RETURNING id
            """)

            # Give the worker a running job (claimed_by links them)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                  claimed_by, worker_host, worker_pid)
                VALUES ('WorkerJob', '{}', 'test', 100, 'running',
                        $1, 'host1', 12345)
            """,
                worker_id,
            )

            workers = await api.list_workers()

            worker = next(
                (w for w in workers if w["host"] == "host1" and w["pid"] == 12345),
                None,
            )
            assert worker is not None
            assert worker["live"] is True
            assert worker["current_job_class"] == "WorkerJob"
            assert worker["current_job_state"] == "running"

    @pytest.mark.asyncio
    async def test_list_workers_includes_claimed(self, db_pool):
        """The joined current job may be in 'claimed' state."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            worker_id = await conn.fetchval("""
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('host2', 54321, 'test', '{test}')
                RETURNING id
            """)
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                  claimed_by, worker_host, worker_pid)
                VALUES ('ClaimedJob', '{}', 'test', 100, 'claimed',
                        $1, 'host2', 54321)
            """,
                worker_id,
            )

            workers = await api.list_workers()

            worker = next((w for w in workers if w["pid"] == 54321), None)
            assert worker is not None
            assert worker["current_job_state"] == "claimed"

    @pytest.mark.asyncio
    async def test_worker_stats(self, db_pool):
        """Test getting registry-based worker statistics."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            for pid in (99991, 99992, 99993):
                await conn.execute(
                    """
                    INSERT INTO jorb_worker (host, pid, queue, capabilities)
                    VALUES ('stats_host', $1, 'stats_queue', '{test}')
                """,
                    pid,
                )

            stats = await api.worker_stats()

            assert stats["live_workers"] >= 3
            assert stats["total_registered"] >= 3
            assert stats["per_queue"]["stats_queue"] == 3


class TestAdminAPIMetrics:
    """Test AdminAPI metrics methods - covers lines 609-686."""

    @pytest.mark.asyncio
    async def test_get_metrics(self, db_pool):
        """Test getting system metrics."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            queue = unique_name("metrics")

            # Create jobs for metrics
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('MetricsJob1', '{}', $1, 100, 'finished')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_message)
                VALUES ('MetricsJob2', '{}', $1, 100, 'crashed', 'Test error')
            """,
                queue,
            )

            metrics = await api.get_metrics(queue=queue)

            assert "period_start" in metrics
            assert "period_end" in metrics
            assert "state_counts" in metrics
            assert "finished_count" in metrics
            assert "crashed_count" in metrics

    @pytest.mark.asyncio
    async def test_get_metrics_default_since(self, db_pool):
        """Test get_metrics uses default 24h window."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            metrics = await api.get_metrics()

            # Should have period_start ~24 hours ago (aware UTC ISO string)
            period_start = datetime.fromisoformat(metrics["period_start"])
            assert datetime.now(UTC) - period_start < timedelta(hours=25)


class TestAdminAPIDLQ:
    """Test AdminAPI dead letter queue methods - covers lines 692-764."""

    @pytest.mark.asyncio
    async def test_list_dlq(self, db_pool):
        """Test listing dead letter queue jobs."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            # Any crashed job is in the DLQ ('crashed' is terminal)
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('DLQJob', '{}', 'test', 100, 'crashed', 15)
                RETURNING id
            """)

            dlq_jobs = await api.list_dlq()

            dlq_ids = [j["id"] for j in dlq_jobs]
            assert job_id in dlq_ids

    @pytest.mark.asyncio
    async def test_list_dlq_is_exactly_crashed_state(self, db_pool):
        """The DLQ has no error-count heuristic: every crashed job is in it,
        and no non-crashed job is."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            low_error_crashed = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('LowErrorJob', '{}', 'test', 100, 'crashed', 5)
                RETURNING id
            """)
            queued_job = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('QueuedJob', '{}', 'test', 100, 'queued', 15)
                RETURNING id
            """)

            dlq_jobs = await api.list_dlq()
            dlq_ids = [j["id"] for j in dlq_jobs]
            assert low_error_crashed in dlq_ids
            assert queued_job not in dlq_ids

    @pytest.mark.asyncio
    async def test_retry_from_dlq(self, db_pool):
        """Test retrying a job from DLQ requeues the same row, errors reset."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('DLQRetryJob', '{"data": 1}', 'test', 100, 'crashed', 20)
                RETURNING id
            """)

            result = await api.retry_from_dlq(job_id)

            assert result == {"job_id": job_id, "status": "requeued_from_dlq"}

            # The same row is queued again with the error budget reset
            job = await api.get_job(job_id)
            assert job["state"] == "queued"
            assert job["error_count"] == 0

    @pytest.mark.asyncio
    async def test_retry_from_dlq_not_found(self, db_pool):
        """Test retry from DLQ with non-existent job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.retry_from_dlq(99999999)
            assert "not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_retry_from_dlq_not_crashed(self, db_pool):
        """Test retry from DLQ with non-crashed job."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('QueuedJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

            with pytest.raises(ValueError) as excinfo:
                await api.retry_from_dlq(job_id)
            assert "not in DLQ" in str(excinfo.value)


class TestAdminAPIScheduleManagement:
    """Test AdminAPI schedule management methods - covers lines 770-1123."""

    @pytest.mark.asyncio
    async def test_list_schedules(self, db_pool):
        """Test listing schedules."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("list_schedule")

            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'TestJob', '0 * * * *', 'test', true, NOW() + INTERVAL '1 hour')
            """,
                name,
            )

            schedules = await api.list_schedules()

            schedule_names = [s["name"] for s in schedules]
            assert name in schedule_names

    @pytest.mark.asyncio
    async def test_list_schedules_filter_enabled(self, db_pool):
        """Test listing schedules filtered by enabled status."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name1 = unique_name("enabled_schedule")
            name2 = unique_name("disabled_schedule")

            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'Job1', '0 * * * *', 'test', true, NOW() + INTERVAL '1 hour')
            """,
                name1,
            )
            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'Job2', '0 * * * *', 'test', false, NOW() + INTERVAL '1 hour')
            """,
                name2,
            )

            enabled = await api.list_schedules(enabled=True)
            disabled = await api.list_schedules(enabled=False)

            enabled_names = [s["name"] for s in enabled]
            disabled_names = [s["name"] for s in disabled]

            assert name1 in enabled_names
            assert name2 in disabled_names

    @pytest.mark.asyncio
    async def test_list_schedules_filter_queue(self, db_pool):
        """Test listing schedules filtered by queue."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("queue_schedule")
            queue = unique_name("schedule_queue")

            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'Job1', '0 * * * *', $2, true, NOW() + INTERVAL '1 hour')
            """,
                name,
                queue,
            )

            schedules = await api.list_schedules(queue=queue)
            assert len(schedules) == 1
            assert schedules[0]["name"] == name

    @pytest.mark.asyncio
    async def test_get_schedule_by_id(self, db_pool):
        """Test getting schedule by ID."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("get_id_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'GetByIdJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            schedule = await api.get_schedule(schedule_id=schedule_id)
            assert schedule is not None
            assert schedule["name"] == name

    @pytest.mark.asyncio
    async def test_get_schedule_by_name(self, db_pool):
        """Test getting schedule by name."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("get_name_schedule")

            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'GetByNameJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
            """,
                name,
            )

            schedule = await api.get_schedule(name=name)
            assert schedule is not None
            assert schedule["job_class"] == "GetByNameJob"

    @pytest.mark.asyncio
    async def test_get_schedule_no_params_raises(self, db_pool):
        """Test get_schedule without params raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.get_schedule()
            assert "Must provide either" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_get_schedule_not_found(self, db_pool):
        """Test get_schedule returns None for non-existent schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            schedule = await api.get_schedule(schedule_id=99999999)
            assert schedule is None

    @pytest.mark.asyncio
    async def test_create_schedule(self, db_pool):
        """Test creating a new schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("create_schedule")

            schedule = await api.create_schedule(
                name=name,
                job_class="CreateScheduleJob",
                cron_expr="0 2 * * *",
                queue="test",
                kwargs={"key": "value"},
                prio=50,
                timezone="UTC",
                description="Test schedule",
            )

            assert schedule["name"] == name
            assert schedule["job_class"] == "CreateScheduleJob"
            assert schedule["cron_expr"] == "0 2 * * *"
            assert schedule["next_run"] is not None

    @pytest.mark.asyncio
    async def test_create_schedule_invalid_cron(self, db_pool):
        """Test creating schedule with invalid cron raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("invalid_cron")

            with pytest.raises(ValueError) as excinfo:
                await api.create_schedule(
                    name=name, job_class="InvalidCronJob", cron_expr="invalid cron"
                )
            assert "malformed cron expression" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_update_schedule(self, db_pool):
        """Test updating a schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("update_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, description, next_run)
                VALUES ($1, 'UpdateJob', '0 * * * *', 'test', 'Original', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            updated = await api.update_schedule(
                schedule_id, description="Updated description", prio=200
            )

            assert updated["description"] == "Updated description"
            assert updated["prio"] == 200

    @pytest.mark.asyncio
    async def test_update_schedule_cron_updates_next_run(self, db_pool):
        """Test updating cron expression recalculates next_run."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("update_cron")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'CronUpdateJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            updated = await api.update_schedule(schedule_id, cron_expr="30 * * * *")

            assert updated["cron_expr"] == "30 * * * *"
            assert updated["next_run"] is not None

    @pytest.mark.asyncio
    async def test_update_schedule_no_valid_fields(self, db_pool):
        """Test update_schedule with no valid fields raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("no_fields")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'NoFieldsJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            with pytest.raises(ValueError) as excinfo:
                await api.update_schedule(schedule_id, invalid_field="value")
            assert "No valid fields" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_update_schedule_not_found(self, db_pool):
        """Test update_schedule with invalid ID raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.update_schedule(99999999, description="Test")
            assert "not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_delete_schedule(self, db_pool):
        """Test deleting a schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("delete_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'DeleteScheduleJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            result = await api.delete_schedule(schedule_id)

            assert result["status"] == "deleted"

            # Verify schedule is deleted
            schedule = await api.get_schedule(schedule_id=schedule_id)
            assert schedule is None

    @pytest.mark.asyncio
    async def test_delete_schedule_not_found(self, db_pool):
        """Test delete_schedule with invalid ID raises ValueError."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)

            with pytest.raises(ValueError) as excinfo:
                await api.delete_schedule(99999999)
            assert "not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_enable_schedule(self, db_pool):
        """Test enabling a disabled schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("enable_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, consecutive_failures, next_run)
                VALUES ($1, 'EnableJob', '0 * * * *', 'test', false, 5, NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            updated = await api.enable_schedule(schedule_id)

            assert updated["enabled"] is True
            assert updated["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_disable_schedule(self, db_pool):
        """Test disabling an enabled schedule."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("disable_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'DisableJob', '0 * * * *', 'test', true, NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            updated = await api.disable_schedule(schedule_id)

            assert updated["enabled"] is False

    @pytest.mark.asyncio
    async def test_get_schedule_history(self, db_pool):
        """Test getting schedule execution history."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("history_schedule")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'HistoryJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            # Create a job for logging
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HistoryJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

            # Create log entry
            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, job_id, result)
                VALUES ($1, $2, NOW(), $3, 'success')
            """,
                schedule_id,
                name,
                job_id,
            )

            history = await api.get_schedule_history(schedule_id)

            assert len(history) >= 1
            assert history[0]["result"] == "success"

    @pytest.mark.asyncio
    async def test_get_schedule_history_filter_result(self, db_pool):
        """Test getting schedule history filtered by result."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("filter_history")

            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'FilterHistoryJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            # Create jobs
            job_id1 = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('FilterHistoryJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)
            job_id2 = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('FilterHistoryJob', '{}', 'test', 100, 'crashed')
                RETURNING id
            """)

            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, job_id, result)
                VALUES ($1, $2, NOW(), $3, 'success')
            """,
                schedule_id,
                name,
                job_id1,
            )
            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, job_id, result)
                VALUES ($1, $2, NOW(), $3, 'failure')
            """,
                schedule_id,
                name,
                job_id2,
            )

            success_history = await api.get_schedule_history(
                schedule_id, result_filter="success"
            )

            assert len(success_history) == 1
            assert success_history[0]["result"] == "success"

    @pytest.mark.asyncio
    async def test_get_schedule_stats(self, db_pool):
        """Test getting schedule statistics."""
        async with db_pool.acquire() as conn:
            api = AdminAPI(conn)
            name = unique_name("stats_schedule")

            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, run_count, success_count, failure_count, next_run)
                VALUES ($1, 'StatsJob', '0 * * * *', 'test', 100, 95, 5, NOW() + INTERVAL '1 hour')
            """,
                name,
            )

            stats = await api.get_schedule_stats()

            # Find our schedule
            schedule_stats = next((s for s in stats if s["name"] == name), None)
            assert schedule_stats is not None
            assert schedule_stats["run_count"] == 100
            assert schedule_stats["success_count"] == 95
            assert schedule_stats["failure_count"] == 5
            assert schedule_stats["success_rate_pct"] == 95.00
