"""
Pytest configuration and fixtures for pyjobby tests.

Connects to native PostgreSQL database on localhost.
Provides robust test isolation through:
1. Unique queue/job names per test (via test_id fixture)
2. Pre-test database cleanup (clean slate before each test)
3. Transaction-based isolation where possible
4. Proper async resource management

This architecture supports concurrent test execution.
"""

import asyncio
import os
import uuid
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


# ============================================================================
# Test Isolation Helpers
# ============================================================================

def unique_name(base: str) -> str:
    """
    Generate a unique name for test isolation.

    Args:
        base: Base name to make unique

    Returns:
        Unique name with UUID suffix
    """
    return f"{base}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def test_id(request) -> str:
    """
    Generate a unique test identifier based on test function name.

    This provides a stable, readable identifier for each test that can be
    used for queue names, job class prefixes, etc. to ensure test isolation.

    Returns:
        str: Unique test identifier (e.g., "test_worker_handles_exception_a1b2c3d4")
    """
    # Get test function name and add short UUID for uniqueness across runs
    test_name = request.node.name
    # Truncate long names and add UUID
    short_name = test_name[:30] if len(test_name) > 30 else test_name
    return f"{short_name}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def unique_queue(test_id: str) -> str:
    """
    Get a unique queue name for this test.

    Returns:
        str: Unique queue name that won't conflict with other tests
    """
    return f"q_{test_id}"


# ============================================================================
# Event Loop Configuration
# ============================================================================

@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Database Connection Parameters
# ============================================================================

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


# ============================================================================
# Database Cleanup - BEFORE each test for clean slate
# ============================================================================

async def _cleanup_database(db_params: dict[str, str]) -> None:
    """
    Clean all test data from database.

    This is called BEFORE each test to ensure a clean slate.
    Creates a fresh connection to avoid event loop issues.
    """
    conn = await asyncpg.connect(**db_params)
    try:
        # Delete in correct order to respect foreign keys
        await conn.execute("DELETE FROM jorb_schedule_log")
        await conn.execute("DELETE FROM jorb_dependencies")
        await conn.execute("DELETE FROM jorb_dag")
        await conn.execute("DELETE FROM jorb_schedule")
        await conn.execute("DELETE FROM jorb")
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True, scope="function")
async def ensure_clean_database(request, db_params: dict[str, str]):
    """
    Ensure database is clean BEFORE each test runs.

    For sequential execution: Clean database before each test
    For parallel execution: Skip cleanup, rely on unique names per test

    This fixture detects if running under pytest-xdist and adjusts behavior.
    """
    # Check if running with pytest-xdist (parallel)
    # If PYTEST_XDIST_WORKER is set, we're in a worker process
    is_parallel = os.environ.get('PYTEST_XDIST_WORKER') is not None

    if is_parallel:
        # In parallel mode, DON'T clean the whole database
        # Tests should use unique names (test_id, unique_queue fixtures)
        yield
        return

    # Sequential mode: clean database for isolation
    test_file = str(request.fspath)

    # List of test files that use db_pool (need cleanup)
    pool_test_files = [
        "test_concurrency",
        "test_e2e_producer_consumer",
        "test_performance_benchmarks",
        "test_client_worker_integration",
        "test_client_hypothesis",
        "test_client_management",
        "test_dag_comprehensive",
        "test_scheduler_comprehensive",
        "test_admin_api_comprehensive",
        "test_admin_api_new",
        "test_pj_worker_run_loop",
        "test_pj_worker_integration",
        "test_pj_dag_continuation",
        "test_pj_entry_points",
        "test_websocket_server",
        "test_web_admin",
        "test_timeout_monitor",
        "test_scheduler",
        "test_client",
        "test_dag",
        "test_retry_strategies",
    ]

    needs_cleanup = any(pf in test_file for pf in pool_test_files)

    if needs_cleanup:
        # Clean BEFORE the test runs
        await _cleanup_database(db_params)

    yield
    # NOTE: No post-test cleanup - reduces connections by 50%
    # Pre-test cleanup is sufficient for isolation


# ============================================================================
# JSON Codec Configuration
# ============================================================================

async def _configure_json_codec(conn: asyncpg.Connection) -> None:
    """Configure orjson codec for JSON/JSONB types."""
    import orjson

    def orjson_encoder(obj):
        return orjson.dumps(obj).decode('utf-8')

    def orjson_decoder(s):
        return orjson.loads(s)

    await conn.set_type_codec(
        "json",
        encoder=orjson_encoder,
        decoder=orjson_decoder,
        schema="pg_catalog",
        format="text"
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=orjson_encoder,
        decoder=orjson_decoder,
        schema="pg_catalog",
        format="text"
    )


# ============================================================================
# Database Connection Fixtures
# ============================================================================

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

    try:
        await _configure_json_codec(conn)
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
    yield db_connection


# ============================================================================
# Connection Pool Fixtures
# ============================================================================

@pytest_asyncio.fixture
async def db_pool(db_params: dict[str, str]) -> AsyncIterator[asyncpg.Pool]:
    """
    Create an asyncpg connection pool for tests that need pooling.

    Each test gets a fresh pool that is automatically closed.
    Configures JSON codec to use orjson (same as production).

    Yields:
        asyncpg.Pool: Connection pool
    """
    async def init_connection(conn):
        await _configure_json_codec(conn)

    pool = await asyncpg.create_pool(
        **db_params,
        min_size=2,
        max_size=10,
        init=init_connection
    )

    try:
        yield pool
    finally:
        await pool.close()


# ============================================================================
# Worker Configuration Fixtures
# ============================================================================

@pytest.fixture
def worker_params(unique_queue: str) -> dict:
    """
    Default worker parameters for testing.

    Uses unique queue name to ensure test isolation.

    Returns:
        dict: Worker configuration
    """
    return {
        "qname": unique_queue,
        "capabilities": ("test",),
        "workerId": 0,
        "checkInterval": 0.1,  # Fast for tests
        "prio": 1000,
        "max_retries": 10,
        "default_timeout": 3600,
        "enable_recovery": True,
        "recovery_timeout": 300,
    }


# ============================================================================
# Client Fixtures
# ============================================================================

@pytest_asyncio.fixture
async def client(db_pool: asyncpg.Pool):
    """
    Create a JobClient instance for testing.

    Uses the connection pool for database operations.

    Yields:
        JobClient: Client instance
    """
    from pyjobby.client import JobClient

    client = JobClient(pool=db_pool)
    yield client


# ============================================================================
# JobSystem Fixtures
# ============================================================================

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


# ============================================================================
# Isolated JobSystem Factory (for tests that need custom configuration)
# ============================================================================

@pytest_asyncio.fixture
async def create_isolated_job_system(db_params: dict[str, str], db_pool: asyncpg.Pool):
    """
    Factory fixture for creating isolated JobSystem instances.

    Each call creates a JobSystem with a unique queue name, ensuring
    complete isolation even when multiple systems run in the same test.

    Usage:
        async def test_something(create_isolated_job_system):
            system1 = await create_isolated_job_system()
            system2 = await create_isolated_job_system(capabilities=('gpu',))

    Returns:
        Callable that creates isolated JobSystem instances
    """
    from pyjobby.pj import JobSystem, STMTS

    created_systems = []

    async def _factory(
        queue_suffix: str = None,
        capabilities: tuple = ('test',),
        worker_id: int = None,
        **kwargs
    ) -> JobSystem:
        # Generate unique queue name
        unique_suffix = queue_suffix or uuid.uuid4().hex[:8]
        queue_name = f"isolated_{unique_suffix}"

        # Generate unique worker ID if not specified
        wid = worker_id if worker_id is not None else len(created_systems)

        system = JobSystem(
            dsn=db_params,
            qname=queue_name,
            capabilities=capabilities,
            workerId=wid,
            checkInterval=0.1,
            webPort=None,
            max_retries=kwargs.get('max_retries', 10),
            default_timeout=kwargs.get('default_timeout', 3600),
            recovery_timeout=kwargs.get('recovery_timeout', 300),
            enable_recovery=kwargs.get('enable_recovery', True),
        )

        # Connect and prepare statements
        system.cxn = await asyncpg.connect(**db_params)
        await _configure_json_codec(system.cxn)

        system.stmts = {}
        for name, stmt in STMTS.items():
            system.stmts[name] = await system.cxn.prepare(stmt)

        created_systems.append(system)
        return system

    yield _factory

    # Cleanup all created systems
    for system in created_systems:
        system.stop = True
        if system.cxn and not system.cxn.is_closed():
            await system.cxn.close()


# ============================================================================
# Test Markers
# ============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "slow: slow running tests")
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "concurrency: concurrency tests")
    config.addinivalue_line("markers", "performance: performance benchmarks")
    config.addinivalue_line("markers", "e2e: end-to-end producer/consumer tests")
    config.addinivalue_line("markers", "hypothesis: property-based tests using Hypothesis")
    config.addinivalue_line("markers", "isolated: tests that require full isolation")
