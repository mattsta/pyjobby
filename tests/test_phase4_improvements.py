"""
Tests for Phase 4 improvements from comprehensive platform audit.

These tests validate:
- Job cancellation API (pyjobby.db.cancel_job — the one shared cancel path)
- Reaper/monitor support indexes
- orjson encoder fix
- Worker configuration parameters
"""

from datetime import timedelta

import pytest

from pyjobby.db import cancel_job, utcnow
from tests.utils.factories import create_job, get_job

pytestmark = pytest.mark.asyncio


class TestJobCancellation:
    """Test the job cancellation API."""

    async def test_cancel_queued_job(self, db_connection):
        """Cancelling a queued job cancels it immediately."""
        job_id = await create_job(db_connection, state="queued")

        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"

        result = await cancel_job(db_connection, job_id)
        assert result == "cancelled"

        job = await get_job(db_connection, job_id)
        assert job["state"] == "cancelled"
        assert job["finished"] is not None

    async def test_cancel_waiting_job(self, db_connection):
        """Cancelling a waiting job cancels it immediately."""
        parent_job_id = await create_job(db_connection, state="queued")

        job_id = await create_job(
            db_connection, state="waiting", waitfor_job=parent_job_id
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "waiting"

        result = await cancel_job(db_connection, job_id)
        assert result == "cancelled"

        job = await get_job(db_connection, job_id)
        assert job["state"] == "cancelled"

    async def test_cancel_claimed_job_requests_cancellation(self, db_connection):
        """Claimed jobs are not cancelled in place: cancel_requested is set
        and the executing worker cancels the task cooperatively."""
        job_id = await create_job(db_connection, state="queued")

        await db_connection.execute(
            """UPDATE jorb SET state = 'claimed', worker_pid = 12345,
               worker_host = 'test-host' WHERE id = $1""",
            job_id,
        )

        result = await cancel_job(db_connection, job_id)
        assert result == "cancel_requested"

        job = await get_job(db_connection, job_id)
        assert job["state"] == "claimed"
        assert job["cancel_requested"] is True

    async def test_cancel_running_job_requests_cancellation(self, db_connection):
        """Running jobs get cancel_requested set (worker honors it via NOTIFY)."""
        job_id = await create_job(db_connection, state="queued")

        await db_connection.execute(
            """UPDATE jorb SET state = 'running', worker_pid = 12345,
               worker_host = 'test-host' WHERE id = $1""",
            job_id,
        )

        result = await cancel_job(db_connection, job_id)
        assert result == "cancel_requested"

        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"
        assert job["cancel_requested"] is True

    async def test_cannot_cancel_finished_job(self, db_connection):
        """Terminal jobs cannot be cancelled."""
        job_id = await create_job(db_connection, state="queued")

        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        result = await cancel_job(db_connection, job_id)
        assert result is None

        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"

    async def test_cancel_nonexistent_job(self, db_connection):
        """Cancelling a job that doesn't exist returns None."""
        result = await cancel_job(db_connection, 999999)
        assert result is None

    async def test_cancel_updates_timestamp(self, db_connection):
        """Cancellation updates the `updated` timestamp."""
        job_id = await create_job(db_connection, state="queued")

        old_time = utcnow() - timedelta(hours=1)
        await db_connection.execute(
            "UPDATE jorb SET updated = $1 WHERE id = $2", old_time, job_id
        )

        # In-transaction now() is the transaction start time, so compare
        # against the database clock, not the Python clock.
        before_cancel = await db_connection.fetchval("SELECT now()")
        result = await cancel_job(db_connection, job_id)
        assert result == "cancelled"

        job = await get_job(db_connection, job_id)
        assert job["updated"] >= before_cancel
        assert job["updated"] > old_time


class TestReaperIndexes:
    """The monitor's sweeps rely on partial indexes over in-flight jobs."""

    async def test_inflight_index_exists(self, db_connection):
        """The reaper-scan index over claimed/running jobs exists."""
        result = await db_connection.fetchrow("""
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'jorb'
              AND indexname = 'jorb_inflight_idx'
        """)

        assert result is not None, "Reaper index 'jorb_inflight_idx' does not exist"

    async def test_timeout_index_exists(self, db_connection):
        """The timeout-sweep index over running jobs with deadlines exists."""
        result = await db_connection.fetchrow("""
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'jorb'
              AND indexname = 'jorb_timeout_idx'
        """)

        assert result is not None, "Timeout index 'jorb_timeout_idx' does not exist"


class TestOrjsonEncoder:
    """Test that orjson encoder works correctly."""

    async def test_orjson_encoder_handles_json_fields(self, db_connection):
        """Test that JSON fields are properly encoded/decoded."""
        # Create a job with complex JSON data
        job_id = await create_job(
            db_connection,
            kwargs={"test": "value", "nested": {"key": "value"}, "list": [1, 2, 3]},
            admin_data={"tags": ["tag1", "tag2"], "priority": "high"},
        )

        # Retrieve the job
        job = await get_job(db_connection, job_id)

        # Verify JSON fields are properly decoded
        assert job["kwargs"]["test"] == "value"
        assert job["kwargs"]["nested"]["key"] == "value"
        assert job["kwargs"]["list"] == [1, 2, 3]
        assert job["admin_data"]["tags"] == ["tag1", "tag2"]
        assert job["admin_data"]["priority"] == "high"

    async def test_orjson_encoder_handles_unicode(self, db_connection):
        """Test that Unicode characters are handled correctly."""
        job_id = await create_job(
            db_connection,
            kwargs={"message": "Hello 世界 🌍"},
            admin_data={"emoji": "🚀💻"},
        )

        job = await get_job(db_connection, job_id)

        assert job["kwargs"]["message"] == "Hello 世界 🌍"
        assert job["admin_data"]["emoji"] == "🚀💻"

    async def test_orjson_encoder_handles_empty_objects(self, db_connection):
        """Test that empty JSON objects are handled."""
        job_id = await create_job(db_connection, kwargs={}, admin_data={})

        job = await get_job(db_connection, job_id)

        assert job["kwargs"] == {}
        assert job["admin_data"] == {}


class TestConfigurationParameters:
    """Test that worker configuration parameters work correctly."""

    async def test_max_retries_configurable(self, worker_params):
        """Test that max_retries can be configured."""
        from pyjobby.pj import JobSystem

        system = JobSystem(
            dsn=worker_params.get("dsn", {}),
            qname="test",
            capabilities=("test",),
            workerId=0,
            max_retries=5,
        )

        assert system.max_retries == 5

    async def test_default_timeout_configurable(self, worker_params):
        """Test that default_timeout can be configured."""
        from pyjobby.pj import JobSystem

        system = JobSystem(
            dsn=worker_params.get("dsn", {}),
            qname="test",
            capabilities=("test",),
            workerId=0,
            default_timeout=7200,  # 2 hours
        )

        assert system.default_timeout == 7200
