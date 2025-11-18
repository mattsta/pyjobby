"""
Tests for SQL improvements based on audit recommendations.

These tests validate the SQL improvements recommended in the comprehensive audit,
particularly the time-based recovery mechanism that prevents race conditions.
"""

from datetime import datetime, timedelta

import pytest

from tests.utils.factories import create_job, get_job


pytestmark = pytest.mark.asyncio


class TestTimeBasedRecovery:
    """Test the time-based recovery improvement."""

    async def test_recovery_respects_timeout(self, db_connection):
        """Test that recovery only recovers jobs older than timeout."""
        from pyjobby.pj import STMTS

        # Create three jobs all claimed by same worker
        very_old_job = await create_job(db_connection, state="queued")
        old_job = await create_job(db_connection, state="queued")
        recent_job = await create_job(db_connection, state="queued")

        # Very old: 15 minutes ago
        very_old_time = datetime.utcnow() - timedelta(minutes=15)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            very_old_time,
            very_old_job
        )

        # Old: 6 minutes ago (should be recovered with 5 min timeout)
        old_time = datetime.utcnow() - timedelta(minutes=6)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            old_job
        )

        # Recent: 2 minutes ago (should NOT be recovered)
        recent_time = datetime.utcnow() - timedelta(minutes=2)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            recent_time,
            recent_job
        )

        # Recover with 5 minute timeout
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            recovery_timeout
        )

        recovered_ids = [r["id"] for r in results]

        # Should recover both old jobs, but not recent
        assert very_old_job in recovered_ids
        assert old_job in recovered_ids
        assert recent_job not in recovered_ids
        assert len(recovered_ids) == 2

        # Verify states
        very_old = await get_job(db_connection, very_old_job)
        old = await get_job(db_connection, old_job)
        recent = await get_job(db_connection, recent_job)

        assert very_old["state"] == "queued"
        assert old["state"] == "queued"
        assert recent["state"] == "claimed"  # Still claimed

    async def test_prevents_same_host_restart_race(self, db_connection):
        """Test that time-based recovery prevents same-host restart races."""
        from pyjobby.pj import STMTS

        # Scenario: Worker on host-1 crashes and restarts while old process
        # is still running (but slow). New process should not steal jobs
        # from old process that's still working.

        # Old process has been working on this job for 2 minutes
        job_id = await create_job(db_connection, state="queued")
        recent_time = datetime.utcnow() - timedelta(minutes=2)
        await db_connection.execute(
            """UPDATE jorb SET state = 'running',
               worker_host = 'host-1',
               worker_pid = 12345,
               updated = $1 WHERE id = $2""",
            recent_time,
            job_id
        )

        # New process on same host tries to recover (with 5 min timeout)
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "host-1",
            recovery_timeout
        )

        # Should NOT recover this job (old process is still alive)
        assert len(results) == 0

        # Job should still be running
        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"

    async def test_configurable_timeout(self, db_connection):
        """Test that recovery timeout is configurable."""
        from pyjobby.pj import STMTS

        # Create jobs with different ages
        job_2min = await create_job(db_connection, state="queued")
        job_10min = await create_job(db_connection, state="queued")

        # 2 minutes old
        time_2min = datetime.utcnow() - timedelta(minutes=2)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            time_2min,
            job_2min
        )

        # 10 minutes old
        time_10min = datetime.utcnow() - timedelta(minutes=10)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            time_10min,
            job_10min
        )

        # Try with 1 minute timeout - should recover both
        short_timeout = timedelta(minutes=1)
        results1 = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            short_timeout
        )
        assert len(results1) == 2

        # Reset jobs back to claimed
        await db_connection.execute(
            "UPDATE jorb SET state = 'claimed' WHERE id = ANY($1::bigint[])",
            [job_2min, job_10min]
        )

        # Try with 15 minute timeout - should recover none
        long_timeout = timedelta(minutes=15)
        results2 = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            long_timeout
        )
        assert len(results2) == 0

    async def test_recovery_updates_timestamp(self, db_connection):
        """Test that recovery updates the job's updated timestamp."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, state="queued")

        # Set old timestamp
        old_time = datetime.utcnow() - timedelta(minutes=10)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            job_id
        )

        # Recover
        before_recovery = datetime.utcnow()
        recovery_timeout = timedelta(minutes=5)
        await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            recovery_timeout
        )

        # Check timestamp was updated
        job = await get_job(db_connection, job_id)
        assert job["updated"] > before_recovery
        assert job["updated"] > old_time

    async def test_recovery_resets_run_after(self, db_connection):
        """Test that recovery resets run_after to current time."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, state="queued")

        # Set job to claimed with future run_after
        old_time = datetime.utcnow() - timedelta(minutes=10)
        future_time = datetime.utcnow() + timedelta(hours=1)
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1,
               run_after = $2
               WHERE id = $3""",
            old_time,
            future_time,
            job_id
        )

        # Recover
        recovery_timeout = timedelta(minutes=5)
        await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            recovery_timeout
        )

        # run_after should be reset to now (not future)
        job = await get_job(db_connection, job_id)
        assert job["run_after"] < datetime.utcnow() + timedelta(seconds=5)


class TestRecoveryEdgeCases:
    """Test edge cases in recovery logic."""

    async def test_recover_multiple_states(self, db_connection):
        """Test that recovery works for both claimed and running states."""
        from pyjobby.pj import STMTS

        # Create jobs in different states
        claimed_job = await create_job(db_connection, state="queued")
        running_job = await create_job(db_connection, state="queued")
        finished_job = await create_job(db_connection, state="queued")

        old_time = datetime.utcnow() - timedelta(minutes=10)

        # Set to different states
        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            claimed_job
        )

        await db_connection.execute(
            """UPDATE jorb SET state = 'running',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            running_job
        )

        await db_connection.execute(
            """UPDATE jorb SET state = 'finished',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            finished_job
        )

        # Recover
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            recovery_timeout
        )

        recovered_ids = [r["id"] for r in results]

        # Should recover claimed and running, but not finished
        assert claimed_job in recovered_ids
        assert running_job in recovered_ids
        assert finished_job not in recovered_ids

    async def test_recover_returns_old_state(self, db_connection):
        """Test that recovery returns the old state for logging."""
        from pyjobby.pj import STMTS

        claimed_job = await create_job(db_connection, state="queued")
        running_job = await create_job(db_connection, state="queued")

        old_time = datetime.utcnow() - timedelta(minutes=10)

        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            claimed_job
        )

        await db_connection.execute(
            """UPDATE jorb SET state = 'running',
               worker_host = 'test-worker',
               updated = $1 WHERE id = $2""",
            old_time,
            running_job
        )

        # Recover
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "test-worker",
            recovery_timeout
        )

        # Check old_state is returned (confusingly named - it's the NEW state)
        for result in results:
            assert result["old_state"] == "queued"  # New state after recovery
            assert "job_class" in result  # Includes job info for logging

    async def test_recover_different_workers(self, db_connection):
        """Test that recovery is specific to worker host."""
        from pyjobby.pj import STMTS

        worker1_job = await create_job(db_connection, state="queued")
        worker2_job = await create_job(db_connection, state="queued")

        old_time = datetime.utcnow() - timedelta(minutes=10)

        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'worker-1',
               updated = $1 WHERE id = $2""",
            old_time,
            worker1_job
        )

        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed',
               worker_host = 'worker-2',
               updated = $1 WHERE id = $2""",
            old_time,
            worker2_job
        )

        # Recover only worker-1
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "worker-1",
            recovery_timeout
        )

        assert len(results) == 1
        assert results[0]["id"] == worker1_job

        # worker-2 job should still be claimed
        worker2 = await get_job(db_connection, worker2_job)
        assert worker2["state"] == "claimed"
