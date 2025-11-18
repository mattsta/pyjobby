"""
Pytest configuration and fixtures for pyjobby tests.

Provides isolated PostgreSQL databases for each test using pytest-postgresql.
"""

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from pytest_postgresql import factories

# Path to schema file
SCHEMA_PATH = Path(__file__).parent.parent / "priv" / "schema.sql"

# PostgreSQL test database factory
# This creates a unique PostgreSQL instance for the test session
postgresql_proc = factories.postgresql_proc(
    port=None,  # Use random available port
    dbname="pyjobby_test",
)

# PostgreSQL client factory
postgresql = factories.postgresql("postgresql_proc", dbname="pyjobby_test")


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def db_params(postgresql) -> dict[str, str]:
    """
    Get database connection parameters for the test database.

    Returns:
        dict: Connection parameters for asyncpg
    """
    return {
        "host": postgresql.info.host,
        "port": postgresql.info.port,
        "user": postgresql.info.user,
        "password": postgresql.info.password or "",
        "database": postgresql.info.dbname,
    }


@pytest_asyncio.fixture
async def db_connection(db_params: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    """
    Create an asyncpg connection to the test database.

    The connection is automatically closed after the test.

    Yields:
        asyncpg.Connection: Database connection
    """
    conn = await asyncpg.connect(**db_params)

    # Load schema
    if SCHEMA_PATH.exists():
        schema_sql = SCHEMA_PATH.read_text()
        await conn.execute(schema_sql)

    # Configure JSON codec to use orjson (same as production)
    try:
        import orjson
        await conn.set_type_codec(
            "json",
            encoder=orjson.dumps,
            decoder=orjson.loads,
            schema="pg_catalog",
        )
    except ImportError:
        pass  # orjson not available, use default JSON codec

    yield conn

    # Cleanup: truncate all tables for next test
    await conn.execute("TRUNCATE TABLE jorb RESTART IDENTITY CASCADE")

    await conn.close()


@pytest_asyncio.fixture
async def clean_db(db_connection: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    """
    Provide a clean database with all tables truncated.

    This is useful for tests that need a completely fresh database state.

    Yields:
        asyncpg.Connection: Database connection with empty tables
    """
    await db_connection.execute("TRUNCATE TABLE jorb RESTART IDENTITY CASCADE")
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
        "recovery_timeout": 300,
    }


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
