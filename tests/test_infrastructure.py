"""
Test the testing infrastructure itself.

These tests verify that:
- PostgreSQL test database is created
- Schema is loaded correctly
- Fixtures work as expected
- Test isolation works
"""

from tests.utils.factories import count_jobs_by_state, create_job, get_job


async def test_database_connection(db_connection):
    """Test that we can connect to the test database."""
    result = await db_connection.fetchval("SELECT 1")
    assert result == 1


async def test_schema_loaded(db_connection):
    """Test that the jorb table exists."""
    # Check table exists
    table_exists = await db_connection.fetchval(
        """
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = 'jorb'
        )
        """
    )
    assert table_exists is True


async def test_can_insert_job(db_connection):
    """Test that we can insert a job into the database."""
    job_id = await create_job(
        db_connection,
        job_class="test.SimpleJob",
        kwargs={"test": "value"},
        queue="test",
    )

    assert job_id is not None
    assert isinstance(job_id, int)


async def test_can_query_job(db_connection):
    """Test that we can query jobs."""
    job_id = await create_job(
        db_connection,
        job_class="test.SimpleJob",
        kwargs={"test": "value"},
        queue="test",
    )

    job = await get_job(db_connection, job_id)
    assert job is not None
    assert job["id"] == job_id
    assert job["job_class"] == "test.SimpleJob"
    assert job["queue"] == "test"
    assert job["state"] == "queued"  # Default state


async def test_test_isolation(db_connection):
    """Test that each test gets a clean database."""
    # This test should start with zero jobs
    count = await count_jobs_by_state(db_connection, "queued")
    assert count == 0


async def test_test_isolation_second(db_connection):
    """
    Test isolation again.

    If isolation works, this test should also start with zero jobs,
    even if previous test created jobs.
    """
    count = await count_jobs_by_state(db_connection, "queued")
    assert count == 0


async def test_job_system_fixture(job_system):
    """Test that the job_system fixture works."""
    assert job_system is not None
    # fixture assigns a unique per-test queue name for isolation
    assert job_system.qname.startswith("q_")
    assert job_system.cxn is not None
    assert job_system.stmts is not None


async def test_orjson_codec(db_connection):
    """Test that orjson codec is configured for JSON columns."""
    job_id = await create_job(
        db_connection,
        job_class="test.JsonTest",
        kwargs={"nested": {"data": [1, 2, 3]}},
    )

    job = await get_job(db_connection, job_id)

    # Should be Python dict, not string
    assert isinstance(job["kwargs"], dict)
    assert job["kwargs"]["nested"]["data"] == [1, 2, 3]
