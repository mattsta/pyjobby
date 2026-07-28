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
import contextlib
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from hypothesis import settings as hypothesis_settings

# Hypothesis's per-example deadline (200ms by default) measures WALL CLOCK,
# and under pytest-xdist the wall clock measures the machine's load rather
# than the code: a pure-arithmetic example that takes microseconds alone
# blows 200ms while four workers saturate the box. That was this suite's
# recurring "one failure per full -n 4 run, always passes in isolation"
# family — every recent instance was a no-database hypothesis test.
# Per-example slowness is still bounded by the per-test timeout, so the
# deadline bought nothing here but noise. The "deadline-proof" profile
# exists to DEMONSTRATE the mechanism on demand:
# HYPOTHESIS_PROFILE=deadline-proof makes the same tests fail
# deterministically with DeadlineExceeded.
hypothesis_settings.register_profile("pyjobby", deadline=None)
hypothesis_settings.register_profile(
    "deadline-proof", deadline=0.001, max_examples=20
)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "pyjobby"))

# The base-schema directory: ordered purpose files whose lexical-order
# concatenation is the whole current schema.
SCHEMA_DIR = Path(__file__).parent.parent / "pyjobby" / "sql" / "schema"

# Get database connection from environment or use default
DEFAULT_TEST_DSN = (
    "postgresql://pyjobby_test:pyjobby_test_password@localhost:5432/pyjobby_test"
)
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


def _dsn_params(dsn: str) -> dict[str, str | int]:
    """asyncpg connection kwargs from a DSN string."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": unquote(parsed.username or "pyjobby_test"),
        "password": unquote(parsed.password or "pyjobby_test_password"),
        "database": (parsed.path or "/pyjobby_test").lstrip("/"),
    }


def _schema_fingerprint() -> str:
    """Content hash of the canonical schema: every base-schema file, in the
    order the installer concatenates them, names included so a rename or
    reorder re-fingerprints too."""
    digest = hashlib.sha256()
    for entry in sorted(SCHEMA_DIR.glob("*.sql")):
        digest.update(entry.name.encode())
        digest.update(entry.read_bytes())
    return digest.hexdigest()


async def _install_schema(params: dict[str, str | int]) -> None:
    """Make this database match the CURRENT schema.sql, rebuilding if it does not.

    pyjobby is forward-only with one canonical schema file, so a test database
    installed from an older revision of that file is simply wrong -- and it
    fails in a way that looks like a product bug ("function does not exist",
    "column does not exist") rather than a stale fixture. Fingerprinting the
    file and reinstalling on any change means editing schema.sql is all a
    schema change ever requires.
    """
    from pyjobby import db as pjdb
    from pyjobby import migrations

    conn = await pjdb.connect(**params)
    try:
        installed = (
            await conn.fetchval(
                "SELECT fingerprint FROM test_schema_fingerprint LIMIT 1"
            )
            if await conn.fetchval("SELECT to_regclass('test_schema_fingerprint')")
            else None
        )

        want = _schema_fingerprint()
        if installed != want:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            await migrations.migrate(conn)
            await conn.execute(
                "CREATE TABLE test_schema_fingerprint (fingerprint TEXT NOT NULL)"
            )
            await conn.execute("INSERT INTO test_schema_fingerprint VALUES ($1)", want)
    finally:
        await conn.close()


def _create_worker_database(
    base: dict[str, str | int], name: str | None = None
) -> None:
    """Create `name` (if given and absent) and install the current schema into it.

    Synchronous on purpose: this runs once per xdist worker during session
    setup, before any event loop exists.
    """
    import asyncio

    async def _setup() -> None:
        if name is not None:
            admin = await asyncpg.connect(**base)
            try:
                exists = await admin.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", name
                )
                if not exists:
                    await admin.execute(f'CREATE DATABASE "{name}"')
            finally:
                await admin.close()

        target = base if name is None else {**base, "database": name}
        await _install_schema(target)

    asyncio.run(_setup())


@pytest.fixture(scope="session")
def db_params(worker_id: str) -> dict[str, str | int]:
    """Connection parameters for THIS test session's database.

    Sequential runs use PYJOBBY_TEST_DSN as-is. Under pytest-xdist each
    worker gets its own database (`<base>_gw0`, `_gw1`, ...), created and
    migrated on demand, because jorb_worker/jorb_queue and the aggregate
    views are global tables: workers sharing one database would see each
    other's rows and truncate each other's data mid-test.
    """
    base = _dsn_params(TEST_DSN)
    if worker_id == "master":
        _create_worker_database(base)
        return base

    per_worker = f"{base['database']}_{worker_id}"
    _create_worker_database(base, per_worker)
    return {**base, "database": per_worker}


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
        # Delete in correct order to respect foreign keys.
        # (jorb_step / jorb_event / jorb_mailbox cascade from jorb.)
        await conn.execute("DELETE FROM jorb_schedule_log")
        await conn.execute("DELETE FROM jorb_dependencies")
        await conn.execute("DELETE FROM jorb_dag")
        await conn.execute("DELETE FROM jorb_schedule")
        await conn.execute("DELETE FROM jorb")
        await conn.execute("DELETE FROM jorb_history")
        await conn.execute("DELETE FROM jorb_worker")
        await conn.execute("DELETE FROM jorb_queue")
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True, scope="function")
async def ensure_clean_database(db_params: dict[str, str]):
    """Clean the session's database before every test.

    Unconditional: each xdist worker owns its own database (see db_params),
    so cleanup is always safe and tests may assert exact global counts
    (worker registry, queue controls, aggregate views) without racing a
    sibling worker.
    """
    await _cleanup_database(db_params)

    yield
    # No post-test cleanup: pre-test cleanup is sufficient and halves the
    # connection churn.


# ============================================================================
# JSON Codec Configuration
# ============================================================================


async def _configure_json_codec(conn: asyncpg.Connection) -> None:
    """Configure orjson codec for JSON/JSONB types (same codecs production
    connections get from pyjobby.db)."""
    from pyjobby.db import register_json_codecs

    await register_json_codecs(conn)


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

    # orjson not available -> default JSON codec
    with contextlib.suppress(ImportError):
        await _configure_json_codec(conn)

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
async def clean_db(
    db_connection: asyncpg.Connection,
) -> AsyncIterator[asyncpg.Connection]:
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
        **db_params, min_size=2, max_size=10, init=init_connection
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
    }


# ============================================================================
# Client Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def job_client(db_pool: asyncpg.Pool):
    """A pool-backed JobClient, closed at teardown.

    In conftest because four files carried byte-identical private copies of
    it. (The pool is the fixture's, not the client's, so close() leaves it
    open by the ownership contract — teardown here is about the listener.)
    """
    from pyjobby.client import JobClient

    client = JobClient(pool=db_pool)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def web_admin_client(db_params: dict, aiohttp_client):
    """A test client for the web admin server on the session's database.

    In conftest because three files carried identical private copies."""
    from pyjobby.web_admin import WebAdminServer

    server = WebAdminServer(db_params)
    return await aiohttp_client(server.app)


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
    from pyjobby.pj import STMTS, JobSystem

    created_systems = []

    async def _factory(
        queue_suffix: str = None,
        capabilities: tuple = ("test",),
        worker_id: int = None,
        **kwargs,
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
            max_retries=kwargs.get("max_retries", 10),
            default_timeout=kwargs.get("default_timeout", 3600),
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
# Live Worker Fixture (DXE)
# ============================================================================


@pytest_asyncio.fixture
async def live_worker(db_params: dict[str, str], unique_queue: str):
    """A fully running JobSystem worker on this test's unique queue.

    Runs the REAL worker loop (registry, heartbeat, LISTEN wakeups, DXE
    checkpoint binding) as an asyncio task inside the test process. Yields
    a factory so tests needing several workers can start more on the same
    queue; all workers stop and deregister at teardown.

    Usage:
        async def test_x(live_worker, unique_queue, db_connection):
            worker = await live_worker()          # first worker
            other = await live_worker()           # optional second worker
    """
    from pyjobby.pj import JobSystem

    started: list[tuple[JobSystem, asyncio.Task]] = []

    async def _start(**overrides) -> JobSystem:
        params: dict = {
            "qname": unique_queue,
            "capabilities": ("test",),
            "workerId": len(started),
            "checkInterval": 0.2,
        }
        params.update(overrides)
        system = JobSystem(dsn=db_params, **params)
        task = asyncio.create_task(system.run())
        started.append((system, task))
        # give the worker a beat to connect, register, and LISTEN
        await asyncio.sleep(0.4)
        return system

    yield _start

    for system, task in started:
        system.stop = True
    for system, task in started:
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError, asyncio.CancelledError:
            task.cancel()


async def wait_for_job_state(
    conn: asyncpg.Connection,
    job_id: int,
    states: tuple[str, ...],
    timeout: float = 10.0,
    interval: float = 0.1,
):
    """Poll until the job reaches one of `states`; returns the full row.

    Reusable helper for any test that drives real workers."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        if row and row["state"] in states:
            return row
        await asyncio.sleep(interval)
    row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
    raise AssertionError(
        f"job {job_id} never reached {states} within {timeout}s "
        f"(state: {row['state'] if row else 'MISSING'})"
    )


# ============================================================================
# Test Markers
# ============================================================================


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "slow: slow running tests")
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "concurrency: concurrency tests")
    config.addinivalue_line("markers", "e2e: end-to-end producer/consumer tests")
    config.addinivalue_line(
        "markers", "hypothesis: property-based tests using Hypothesis"
    )
    config.addinivalue_line("markers", "isolated: tests that require full isolation")
