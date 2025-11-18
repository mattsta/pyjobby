"""
Pytest configuration and fixtures for pyjobby tests.

Connects to native PostgreSQL database on localhost.
Each test gets a clean database state via transaction rollback.
"""

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio

# Path to schema file
SCHEMA_PATH = Path(__file__).parent.parent / "priv" / "schema.sql"

# Get database connection from environment or use default
DEFAULT_TEST_DSN = "postgresql://pyjobby_test:pyjobby_test_password@localhost:5432/pyjobby_test"
TEST_DSN = os.getenv("PYJOBBY_TEST_DSN", DEFAULT_TEST_DSN)


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def db_params() -> dict[str, str]:
    """
    Get database connection parameters for the test database.

    Uses native PostgreSQL server on localhost:5432.
    Connection details can be overridden via PYJOBBY_TEST_DSN environment variable.

    Returns:
        dict: Connection parameters for asyncpg
    """
    return {
        "host": "localhost",
        "port": 5432,
        "user": "pyjobby_test",
        "password": "pyjobby_test_password",
        "database": "pyjobby_test",
    }


@pytest_asyncio.fixture
async def db_connection(db_params: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    """
    Create an asyncpg connection to the test database for each test.

    Each test runs in a transaction that is rolled back after the test,
    ensuring complete isolation between tests without needing to truncate tables.

    Yields:
        asyncpg.Connection: Database connection
    """
    conn = await asyncpg.connect(**db_params)

    # Configure JSON codec to use orjson (same as production)
    try:
        import orjson

        # orjson.dumps returns bytes, so decode to str for asyncpg
        def orjson_encoder(obj):
            return orjson.dumps(obj).decode('utf-8')

        await conn.set_type_codec(
            "json",
            encoder=orjson_encoder,
            decoder=orjson.loads,
            schema="pg_catalog",
        )
        # Also configure jsonb
        await conn.set_type_codec(
            "jsonb",
            encoder=orjson_encoder,
            decoder=orjson.loads,
            schema="pg_catalog",
        )
    except ImportError:
        pass  # orjson not available, use default JSON codec

    # Start transaction for test isolation
    transaction = conn.transaction()
    await transaction.start()

    try:
        yield conn
    finally:
        # Rollback transaction to undo all changes
        await transaction.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def clean_db(db_connection: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    """
    Provide a clean database with all tables truncated.

    Since we use transaction-based isolation, this is actually the same
    as db_connection (both start with empty tables).

    Yields:
        asyncpg.Connection: Database connection with empty tables
    """
    # No truncation needed - transaction rollback handles isolation
    yield db_connection


@pytest.fixture
def worker_params() -> dict:
    """
    Default worker parameters for testing.

    Returns:
        dict: Worker configuration
    """
    return {
        "qname": "test_queue",
        "capabilities": ("test",),
        "workerId": 0,
        "checkInterval": 1,  # Faster for tests
        "prio": 1000,
        "max_retries": 10,
        "default_timeout": 3600,
        "enable_recovery": True,
    }


@pytest_asyncio.fixture(autouse=True, scope="function")
async def cleanup_after_concurrency_tests(request, db_params: dict[str, str]):
    """Clean up database after concurrency tests that don't use transactions."""
    yield

    # Only clean up after concurrency tests (which create their own connections)
    if "test_concurrency" in str(request.fspath):
        conn = await asyncpg.connect(**db_params)
        try:
            # Delete all jobs created during concurrency tests
            await conn.execute("DELETE FROM jorb")
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def job_system(db_params: dict[str, str], db_connection, worker_params: dict):
    """
    Create a JobSystem instance for testing.

    The JobSystem is automatically started and stopped around the test.

    Yields:
        JobSystem: Running job system instance
    """
    from pyjobby.pj import JobSystem

    system = JobSystem(
        dsn=db_params,
        **worker_params,
    )

    # Initialize connection and prepared statements
    system.cxn = db_connection

    # Prepare all SQL statements
    from pyjobby.pj import STMTS
    system.stmts = {}
    for name, stmt in STMTS.items():
        system.stmts[name] = await db_connection.prepare(stmt)

    yield system

    # Cleanup
    system.stop = True


# Markers for test categorization
def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "slow: slow running tests")
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "concurrency: concurrency tests")
    config.addinivalue_line("markers", "performance: performance benchmarks")
