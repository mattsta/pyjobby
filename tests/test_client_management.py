"""
Comprehensive tests for JobClient management features.

Tests cover:
- Job listing and filtering
- Job searching
- Job result retrieval
- Job deletion
- Job priority updates
- Queue management
- Failed/waiting job queries
- Bulk operations
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

# =============================================================================
# State helpers
# =============================================================================
#
# These management tests exercise listing/searching/purging, not the claim
# path, so they force jobs into target states directly (all timestamps are
# timestamptz; plain now() in SQL is correct).


async def force_finish(conn, job_id: int, result: dict) -> None:
    """Put a job into terminal 'finished' with a stored result."""
    await conn.execute(
        """
        UPDATE jorb
        SET state = 'finished', result = $2, run_count = run_count + 1,
            started = now(), finished = now(), updated = now()
        WHERE id = $1
        """,
        job_id,
        result,
    )


async def force_crash(conn, job_id: int, error: str, backtrace: str = "") -> None:
    """Put a job into terminal 'crashed' (the DLQ)."""
    await conn.execute(
        """
        UPDATE jorb
        SET state = 'crashed', error_message = $2, error_backtrace = $3,
            error_count = error_count + 1, run_count = run_count + 1,
            started = now(), finished = now(), updated = now()
        WHERE id = $1
        """,
        job_id,
        error,
        backtrace,
    )


# =============================================================================
# Test Fixtures
# =============================================================================




@pytest.fixture
async def setup_test_jobs(db_pool, job_client):
    """Create various test jobs for management tests."""
    job_ids = []

    # Create jobs in different queues with different states
    # Queue: default, State: queued
    for i in range(5):
        job_id = await job_client.enqueue(
            "test.DefaultJob", queue="default", priority=100 + i, data=f"default_{i}"
        )
        job_ids.append(("default", "queued", job_id))

    # Queue: emails, State: queued
    for i in range(3):
        job_id = await job_client.enqueue(
            "test.EmailJob", queue="emails", priority=200 + i, data=f"email_{i}"
        )
        job_ids.append(("emails", "queued", job_id))

    # Queue: processing, State: various
    for i in range(4):
        job_id = await job_client.enqueue(
            "test.ProcessJob", queue="processing", priority=300 + i, data=f"process_{i}"
        )
        job_ids.append(("processing", "queued", job_id))

    # Create some finished jobs with results
    for i in range(3):
        job_id = await job_client.enqueue(
            "test.FinishedJob", queue="default", priority=400 + i, data=f"finished_{i}"
        )
        async with db_pool.acquire() as conn:
            await force_finish(
                conn, job_id, {"result": f"completed_{i}", "value": i * 10}
            )
        job_ids.append(("default", "finished", job_id))

    # Create some crashed jobs
    for i in range(2):
        job_id = await job_client.enqueue(
            "test.CrashedJob", queue="processing", priority=500 + i, data=f"crashed_{i}"
        )
        async with db_pool.acquire() as conn:
            await force_crash(conn, job_id, f"Error {i}", f"Traceback {i}")
        job_ids.append(("processing", "crashed", job_id))

    # Create some waiting jobs
    first_job = await job_client.enqueue("test.FirstJob", queue="default")
    for i in range(2):
        job_id = await job_client.enqueue(
            "test.WaitingJob",
            queue="default",
            waitfor_job=first_job,
            data=f"waiting_{i}",
        )
        job_ids.append(("default", "waiting", job_id))

    return job_ids


# =============================================================================
# Job Listing and Filtering Tests
# =============================================================================


@pytest.mark.asyncio
class TestJobListing:
    """Tests for job listing and filtering."""

    async def test_get_jobs_all(self, db_pool, job_client, setup_test_jobs):
        """Test getting all jobs with default limit."""
        jobs = await job_client.get_jobs(limit=100)

        assert len(jobs) > 0
        assert all("id" in job for job in jobs)
        assert all("state" in job for job in jobs)
        assert all("queue" in job for job in jobs)

    async def test_get_jobs_by_queue(self, db_pool, job_client, setup_test_jobs):
        """Test filtering jobs by queue."""
        # Get jobs from 'emails' queue
        email_jobs = await job_client.get_jobs(queue="emails")

        assert len(email_jobs) >= 3  # We created 3 email jobs
        assert all(job["queue"] == "emails" for job in email_jobs)

    async def test_get_jobs_by_state(self, db_pool, job_client, setup_test_jobs):
        """Test filtering jobs by state."""
        # Get queued jobs
        queued_jobs = await job_client.get_jobs(state="queued")
        assert all(job["state"] == "queued" for job in queued_jobs)

        # Get finished jobs
        finished_jobs = await job_client.get_jobs(state="finished")
        assert len(finished_jobs) >= 3  # We created 3 finished jobs
        assert all(job["state"] == "finished" for job in finished_jobs)

    async def test_get_jobs_pagination(self, db_pool, job_client, setup_test_jobs):
        """Test pagination with limit and offset."""
        # Get first page
        page1 = await job_client.get_jobs(limit=5, offset=0)
        assert len(page1) <= 5

        # Get second page
        page2 = await job_client.get_jobs(limit=5, offset=5)

        # Pages should have different jobs
        page1_ids = {job["id"] for job in page1}
        page2_ids = {job["id"] for job in page2}
        assert len(page1_ids & page2_ids) == 0  # No overlap

    async def test_get_jobs_ordering(self, db_pool, job_client, setup_test_jobs):
        """Test job ordering by different fields."""
        # Order by created (default, descending)
        jobs_created_desc = await job_client.get_jobs(
            order_by="created", ascending=False
        )
        created_dates = [job["created"] for job in jobs_created_desc]
        assert created_dates == sorted(created_dates, reverse=True)

        # Order by priority (ascending)
        jobs_prio_asc = await job_client.get_jobs(
            order_by="prio", ascending=True, limit=50
        )
        priorities = [job["prio"] for job in jobs_prio_asc]
        assert priorities == sorted(priorities)

    async def test_get_jobs_combined_filters(
        self, db_pool, job_client, setup_test_jobs
    ):
        """Test combining multiple filters."""
        jobs = await job_client.get_jobs(queue="default", state="queued", limit=10)

        assert all(job["queue"] == "default" for job in jobs)
        assert all(job["state"] == "queued" for job in jobs)


# =============================================================================
# Job Search Tests
# =============================================================================


@pytest.mark.asyncio
class TestJobSearch:
    """Tests for advanced job searching."""

    async def test_search_by_job_class(self, db_pool, job_client, setup_test_jobs):
        """Test searching by job class name."""
        jobs = await job_client.search_jobs(job_class="test.EmailJob")

        assert len(jobs) >= 3
        assert all(job["job_class"] == "test.EmailJob" for job in jobs)

    async def test_search_by_job_class_wildcard(
        self, db_pool, job_client, setup_test_jobs
    ):
        """Test searching with wildcard patterns."""
        jobs = await job_client.search_jobs(job_class="%Email%")

        assert len(jobs) >= 3
        assert all("Email" in job["job_class"] for job in jobs)

    async def test_search_by_priority_range(self, db_pool, job_client, setup_test_jobs):
        """Test searching by priority range."""
        jobs = await job_client.search_jobs(min_priority=200, max_priority=300)

        assert all(200 <= job["prio"] <= 300 for job in jobs)

    async def test_search_by_min_priority(self, db_pool, job_client, setup_test_jobs):
        """Test searching with minimum priority."""
        jobs = await job_client.search_jobs(min_priority=400)

        assert all(job["prio"] >= 400 for job in jobs)

    async def test_search_by_max_priority(self, db_pool, job_client, setup_test_jobs):
        """Test searching with maximum priority."""
        jobs = await job_client.search_jobs(max_priority=200)

        assert all(job["prio"] <= 200 for job in jobs)

    async def test_search_by_created_after(self, db_pool, job_client, setup_test_jobs):
        """Test searching by creation time."""
        cutoff = datetime.now(UTC) - timedelta(minutes=1)
        jobs = await job_client.search_jobs(created_after=cutoff)

        assert all(job["created"] >= cutoff for job in jobs)

    async def test_search_by_created_before(self, db_pool, job_client, setup_test_jobs):
        """Test searching before a specific time."""
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        jobs = await job_client.search_jobs(created_before=cutoff)

        assert all(job["created"] <= cutoff for job in jobs)

    async def test_search_by_uid(self, db_pool, job_client):
        """Test searching by user/tenant ID."""
        # Create jobs with specific UID
        job1 = await job_client.enqueue("test.Job", uid=12345, data="user1")
        job2 = await job_client.enqueue("test.Job", uid=12345, data="user2")
        job3 = await job_client.enqueue("test.Job", uid=67890, data="user3")

        jobs = await job_client.search_jobs(uid=12345)

        assert len(jobs) >= 2
        assert all(job["uid"] == 12345 for job in jobs)

    async def test_search_by_run_group(self, db_pool, job_client):
        """Test searching by run group."""
        group_id = 999

        # Create jobs in a run group
        await job_client.enqueue("test.Job", run_group=group_id, data="g1")
        await job_client.enqueue("test.Job", run_group=group_id, data="g2")

        jobs = await job_client.search_jobs(run_group=group_id)

        assert len(jobs) >= 2
        assert all(job["run_group"] == group_id for job in jobs)

    async def test_search_by_capability(self, db_pool, job_client):
        """Test searching by capability requirement."""
        # Create jobs with capabilities
        await job_client.enqueue("test.Job", capability="gpu", data="gpu1")
        await job_client.enqueue("test.Job", capability="gpu", data="gpu2")
        await job_client.enqueue("test.Job", capability="cpu", data="cpu1")

        gpu_jobs = await job_client.search_jobs(capability="gpu")

        assert len(gpu_jobs) >= 2
        assert all(job["capability"] == "gpu" for job in gpu_jobs)

    async def test_search_combined_criteria(self, db_pool, job_client):
        """Test searching with multiple criteria."""
        # Create specific jobs
        job1 = await job_client.enqueue(
            "test.SpecialJob", priority=500, uid=111, capability="special"
        )

        jobs = await job_client.search_jobs(
            job_class="test.SpecialJob", min_priority=400, uid=111, capability="special"
        )

        assert any(job["id"] == job1 for job in jobs)


# =============================================================================
# Job Result Retrieval Tests
# =============================================================================


@pytest.mark.asyncio
class TestJobResults:
    """Tests for job result retrieval."""

    async def test_get_job_result_finished(self, db_pool, job_client, setup_test_jobs):
        """Test getting result from finished job."""
        finished_jobs = [j for j in setup_test_jobs if j[1] == "finished"]
        if finished_jobs:
            job_id = finished_jobs[0][2]
            result = await job_client.get_job_result(job_id)

            assert result is not None
            assert "result" in result

    async def test_get_job_result_not_finished(self, db_pool, job_client):
        """Test getting result from non-finished job returns None."""
        # Create queued job
        job_id = await job_client.enqueue("test.Job", data="test")
        result = await job_client.get_job_result(job_id)

        assert result is None

    async def test_get_job_full_details(self, db_pool, job_client, setup_test_jobs):
        """Test getting full job details."""
        job_id = setup_test_jobs[0][2]
        job = await job_client.get_job_full(job_id)

        assert job is not None
        assert "id" in job
        assert "job_class" in job
        assert "kwargs" in job
        assert "queue" in job
        assert "state" in job
        assert "created" in job

    async def test_get_job_full_includes_kwargs(self, db_pool, job_client):
        """Test that full job details include kwargs."""
        job_id = await job_client.enqueue("test.Job", data="test_data", value=12345)

        job = await job_client.get_job_full(job_id)

        assert job is not None
        kwargs = (
            json.loads(job["kwargs"])
            if isinstance(job["kwargs"], str)
            else job["kwargs"]
        )
        assert "data" in kwargs
        assert kwargs["data"] == "test_data"


# =============================================================================
# Job Deletion Tests
# =============================================================================


@pytest.mark.asyncio
class TestJobDeletion:
    """Tests for job deletion."""

    async def test_delete_job_success(self, db_pool, job_client):
        """Test successful job deletion."""
        job_id = await job_client.enqueue("test.Job", data="to_delete")

        # Delete the job
        deleted = await job_client.delete_job(job_id)
        assert deleted is True

        # Verify job is gone
        job = await job_client.get_job(job_id)
        assert job is None

    async def test_delete_job_not_found(self, db_pool, job_client):
        """Test deleting non-existent job."""
        deleted = await job_client.delete_job(999999999)
        assert deleted is False

    async def test_delete_multiple_jobs(self, db_pool, job_client):
        """Test deleting multiple jobs individually."""
        job_ids = []
        for i in range(5):
            job_id = await job_client.enqueue("test.Job", data=f"delete_{i}")
            job_ids.append(job_id)

        # Delete all jobs
        for job_id in job_ids:
            deleted = await job_client.delete_job(job_id)
            assert deleted is True

        # Verify all gone
        for job_id in job_ids:
            job = await job_client.get_job(job_id)
            assert job is None


# =============================================================================
# Job Priority Update Tests
# =============================================================================


@pytest.mark.asyncio
class TestJobPriorityUpdate:
    """Tests for job priority updates."""

    async def test_update_priority_queued_job(self, db_pool, job_client):
        """Test updating priority of queued job."""
        job_id = await job_client.enqueue("test.Job", priority=100)

        # Update priority
        updated = await job_client.update_job_priority(job_id, 500)
        assert updated is True

        # Verify priority changed
        job = await job_client.get_job(job_id)
        assert job.priority == 500

    async def test_update_priority_waiting_job(self, db_pool, job_client):
        """Test updating priority of waiting job."""
        first_job = await job_client.enqueue("test.First")
        waiting_job = await job_client.enqueue(
            "test.Second", waitfor_job=first_job, priority=100
        )

        # Update priority
        updated = await job_client.update_job_priority(waiting_job, 300)
        assert updated is True

        # Verify priority changed
        job_full = await job_client.get_job_full(waiting_job)
        assert job_full["prio"] == 300

    async def test_update_priority_running_job(self, db_pool, job_client):
        """Test that updating priority of running job fails."""
        job_id = await job_client.enqueue("test.Job", priority=100)

        # Force the job into 'running'
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jorb SET state = 'running', started = now() WHERE id = $1",
                job_id,
            )

        # Try to update priority (should fail)
        updated = await job_client.update_job_priority(job_id, 500)
        assert updated is False


# =============================================================================
# Queue Management Tests
# =============================================================================


@pytest.mark.asyncio
class TestQueueManagement:
    """Tests for queue management operations."""

    async def test_list_queues(self, db_pool, job_client, setup_test_jobs):
        """Test listing all queues with stats."""
        queues = await job_client.list_queues()

        assert len(queues) > 0
        assert all("queue" in q for q in queues)
        assert all("total" in q for q in queues)
        assert all("queued" in q for q in queues)

        # Should have our test queues
        queue_names = {q["queue"] for q in queues}
        assert "default" in queue_names
        assert "emails" in queue_names
        assert "processing" in queue_names

    async def test_purge_queue_default_states(self, db_pool, job_client):
        """Test purging queue with default states (queued, waiting)."""
        # Create test jobs
        await job_client.enqueue("test.Job", queue="purge_test1")
        await job_client.enqueue("test.Job", queue="purge_test1")
        await job_client.enqueue("test.Job", queue="purge_test1")

        # Purge the queue
        deleted = await job_client.purge_queue("purge_test1")
        assert deleted == 3

        # Verify queue is empty
        depth = await job_client.queue_depth("purge_test1")
        assert depth == 0

    async def test_purge_queue_specific_states(self, db_pool, job_client):
        """Test purging queue with specific states."""
        # Create and finish some jobs
        for i in range(3):
            job_id = await job_client.enqueue("test.Job", queue="purge_test2")
            async with db_pool.acquire() as conn:
                await force_finish(conn, job_id, {"done": True})

        # Purge only finished jobs
        deleted = await job_client.purge_queue("purge_test2", states=["finished"])
        assert deleted == 3

    async def test_purge_queue_empty(self, db_pool, job_client):
        """Test purging empty queue."""
        deleted = await job_client.purge_queue("nonexistent_queue")
        assert deleted == 0


# =============================================================================
# Failed/Waiting Jobs Tests
# =============================================================================


@pytest.mark.asyncio
class TestFailedWaitingJobs:
    """Tests for querying failed and waiting jobs."""

    async def test_get_failed_jobs(self, db_pool, job_client, setup_test_jobs):
        """Test getting crashed/failed jobs."""
        failed = await job_client.get_failed_jobs()

        assert len(failed) >= 2  # We created 2 crashed jobs
        assert all(job["state"] == "crashed" for job in failed)

    async def test_get_failed_jobs_by_queue(self, db_pool, job_client, setup_test_jobs):
        """Test getting failed jobs from specific queue."""
        failed = await job_client.get_failed_jobs(queue="processing")

        assert len(failed) >= 2  # We created 2 crashed jobs in processing queue
        assert all(job["queue"] == "processing" for job in failed)
        assert all(job["state"] == "crashed" for job in failed)

    async def test_get_waiting_jobs(self, db_pool, job_client, setup_test_jobs):
        """Test getting jobs waiting on dependencies."""
        waiting = await job_client.get_waiting_jobs()

        assert len(waiting) >= 2  # We created 2 waiting jobs
        assert all(job["state"] == "waiting" for job in waiting)
        assert all(
            job.get("waitfor_job") is not None or job.get("waitfor_group") is not None
            for job in waiting
        )


# =============================================================================
# Bulk Operations Tests
# =============================================================================


@pytest.mark.asyncio
class TestCancelAndWait:
    """cancel_and_wait resolves the cancel's outcome instead of leaving the
    caller with the 'cancel_requested' promise."""

    async def test_a_queued_job_lands_as_cancelled(self, db_pool, job_client):
        """A queued job has no worker to outrun the request, so the cancel is
        immediate and the resolved outcome is 'cancelled'."""
        job_id = await job_client.enqueue("test.Job", data="x")

        assert await job_client.cancel_and_wait(job_id) == "cancelled"

    async def test_nothing_to_cancel_is_none(self, db_pool, job_client):
        """A job that does not exist was never cancellable, so the outcome is
        None -- distinct from any terminal state string."""
        assert await job_client.cancel_and_wait(2**62) is None


@pytest.mark.asyncio
class TestRunTimeout:
    async def test_run_cancels_the_job_it_gave_up_waiting_on(
        self, db_pool, job_client
    ):
        """No worker runs the job, so run()'s wait times out. The abandoned
        job must be cancelled, not left queued and orphaned, and the caller
        still sees the TimeoutError."""
        queue = f"run_timeout_{uuid.uuid4().hex}"
        with pytest.raises(TimeoutError):
            await job_client.run("test.Job", timeout=0.5, queue=queue, data="x")

        jobs = await job_client.get_jobs(queue=queue)
        assert [j["state"] for j in jobs] == ["cancelled"]


@pytest.mark.asyncio
class TestBulkOperations:
    """Tests for bulk job operations."""

    async def test_bulk_cancel(self, db_pool, job_client):
        """Test cancelling multiple jobs."""
        # Create jobs to cancel
        job_ids = []
        for i in range(10):
            job_id = await job_client.enqueue("test.Job", data=f"cancel_{i}")
            job_ids.append(job_id)

        # Cancel all jobs
        cancelled = await job_client.bulk_cancel(job_ids)
        assert cancelled == 10

        # Verify all cancelled
        for job_id in job_ids:
            job = await job_client.get_job_full(job_id)
            assert job["state"] == "cancelled"

    async def test_bulk_cancel_mixed_states(self, db_pool, job_client):
        """Bulk cancel is the single-job verb applied to a list: queued jobs
        stop now, running ones get a cancellation request for their worker."""
        queued_ids = []
        running_ids = []

        for i in range(3):
            queued_ids.append(await job_client.enqueue("test.Job", data=f"queued_{i}"))

        for i in range(2):
            job_id = await job_client.enqueue("test.Job", data=f"running_{i}")
            running_ids.append(job_id)
            # Manually set to running state (bypassing claim)
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
                )

        cancelled = await job_client.bulk_cancel(queued_ids + running_ids)
        assert cancelled == 5

        for job_id in queued_ids:
            job = await job_client.get_job_full(job_id)
            assert job["state"] == "cancelled"

        for job_id in running_ids:
            job = await job_client.get_job_full(job_id)
            assert job["state"] == "running"
            assert job["cancel_requested"] is True

    async def test_bulk_cancel_empty_list(self, db_pool, job_client):
        """Test bulk cancel with empty list."""
        cancelled = await job_client.bulk_cancel([])
        assert cancelled == 0

    async def test_bulk_retry(self, db_pool, job_client):
        """Test retrying multiple failed jobs requeues the SAME rows."""
        # Create and crash some jobs
        original_job_ids = []
        for i in range(5):
            job_id = await job_client.enqueue("test.Job", data=f"retry_{i}")
            async with db_pool.acquire() as conn:
                await force_crash(conn, job_id, "Test error", "Test traceback")
            original_job_ids.append(job_id)

        # Retry all jobs: bulk_retry returns the requeued ids (identical to
        # the originals — retries keep one row per job for life)
        requeued_ids = await job_client.bulk_retry(original_job_ids)
        assert requeued_ids == original_job_ids

        # Verify the same rows are queued again with errors reset
        for job_id in requeued_ids:
            job = await job_client.get_job_full(job_id)
            assert job["state"] == "queued"
            assert job["error_count"] == 0
            assert job["error_message"] is None
            assert job["result"] is None

    async def test_bulk_retry_empty_list(self, db_pool, job_client):
        """Test bulk retry with empty list."""
        new_jobs = await job_client.bulk_retry([])
        assert new_jobs == []

    async def test_bulk_delete(self, db_pool, job_client):
        """Test deleting multiple jobs."""
        # Create jobs to delete
        job_ids = []
        for i in range(8):
            job_id = await job_client.enqueue("test.Job", data=f"delete_{i}")
            job_ids.append(job_id)

        # Delete all jobs
        deleted = await job_client.bulk_delete(job_ids)
        assert deleted == 8

        # Verify all deleted
        for job_id in job_ids:
            job = await job_client.get_job(job_id)
            assert job is None

    async def test_bulk_delete_empty_list(self, db_pool, job_client):
        """Test bulk delete with empty list."""
        deleted = await job_client.bulk_delete([])
        assert deleted == 0

    async def test_bulk_update_priority(self, db_pool, job_client):
        """Test updating priority for multiple jobs."""
        # Create jobs with low priority
        job_ids = []
        for i in range(6):
            job_id = await job_client.enqueue("test.Job", priority=50, data=f"prio_{i}")
            job_ids.append(job_id)

        # Update all to high priority
        updated = await job_client.bulk_update_priority(job_ids, 800)
        assert updated == 6

        # Verify all updated
        for job_id in job_ids:
            job = await job_client.get_job(job_id)
            assert job.priority == 800

    async def test_bulk_update_priority_mixed_states(self, db_pool, job_client):
        """Test bulk priority update with mixed job states."""
        job_ids = []

        # Create queued jobs
        for i in range(3):
            job_id = await job_client.enqueue(
                "test.Job", priority=100, data=f"queued_{i}"
            )
            job_ids.append(job_id)

        # Create and run some jobs (can't update priority)
        for i in range(2):
            job_id = await job_client.enqueue(
                "test.Job", priority=100, data=f"running_{i}"
            )
            job_ids.append(job_id)
            # Manually set to running state (bypassing claim)
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
                )

        # Try to update all (only queued should be updated)
        updated = await job_client.bulk_update_priority(job_ids, 500)
        assert updated == 3  # Only the queued jobs

    async def test_bulk_update_priority_empty_list(self, db_pool, job_client):
        """Test bulk priority update with empty list."""
        updated = await job_client.bulk_update_priority([], 500)
        assert updated == 0


# =============================================================================
# Integration Tests: Complex Workflows
# =============================================================================


@pytest.mark.asyncio
class TestManagementWorkflows:
    """Integration tests combining multiple management operations."""

    async def test_find_failed_jobs_and_retry(self, db_pool, job_client):
        """Test workflow: find failed jobs and retry them."""
        # Create and crash some jobs in a specific queue
        queue_name = "retry_workflow"
        original_ids = []

        for i in range(3):
            job_id = await job_client.enqueue(
                "test.FailedJob", queue=queue_name, data=f"fail_{i}"
            )
            async with db_pool.acquire() as conn:
                await force_crash(conn, job_id, "Simulated failure", "Stack trace")
            original_ids.append(job_id)

        # Find failed jobs
        failed = await job_client.get_failed_jobs(queue=queue_name)
        failed_ids = [job["id"] for job in failed if job["id"] in original_ids]
        assert len(failed_ids) == 3

        # Retry them: the same rows are requeued (stable job ids)
        requeued_ids = await job_client.bulk_retry(failed_ids)
        assert sorted(requeued_ids) == sorted(original_ids)

        # Verify the jobs are queued again in their original queue
        for job_id in requeued_ids:
            job = await job_client.get_job(job_id)
            assert job.state == "queued"
            assert job.queue == queue_name

    async def test_search_and_cancel_workflow(self, db_pool, job_client):
        """Test workflow: search for jobs and cancel them."""
        # Create high-priority jobs
        for i in range(5):
            await job_client.enqueue(
                "test.HighPriorityJob", priority=900, uid=777, data=f"high_{i}"
            )

        # Search for high-priority jobs
        high_prio_jobs = await job_client.search_jobs(min_priority=850, uid=777)
        job_ids = [job["id"] for job in high_prio_jobs]
        assert len(job_ids) >= 5

        # Cancel them all
        cancelled = await job_client.bulk_cancel(job_ids)
        assert cancelled >= 5

    async def test_queue_cleanup_workflow(self, db_pool, job_client):
        """Test workflow: cleanup finished jobs from queue."""
        queue_name = "cleanup_test"

        # Create and finish some jobs
        for i in range(10):
            job_id = await job_client.enqueue(
                "test.Job", queue=queue_name, data=f"cleanup_{i}"
            )
            async with db_pool.acquire() as conn:
                await force_finish(conn, job_id, {"done": True})

        # Get queue stats before cleanup
        stats_before = await job_client.queue_stats(queue_name)
        assert stats_before["finished"] >= 10

        # Cleanup finished jobs
        deleted = await job_client.purge_queue(queue_name, states=["finished"])
        assert deleted >= 10

        # Verify cleanup
        stats_after = await job_client.queue_stats(queue_name)
        assert stats_after["finished"] == 0

    async def test_priority_management_workflow(self, db_pool, job_client):
        """Test workflow: find and reprioritize jobs."""
        # Create jobs with different priorities
        job_ids_low = []
        for i in range(5):
            job_id = await job_client.enqueue("test.LowPriority", priority=50, uid=888)
            job_ids_low.append(job_id)

        # Search for low-priority jobs
        low_prio = await job_client.search_jobs(max_priority=100, uid=888)
        found_ids = [job["id"] for job in low_prio if job["id"] in job_ids_low]

        # Boost their priority
        updated = await job_client.bulk_update_priority(found_ids, 500)
        assert updated >= 5

        # Verify priority changed
        for job_id in found_ids:
            job = await job_client.get_job(job_id)
            assert job.priority == 500
