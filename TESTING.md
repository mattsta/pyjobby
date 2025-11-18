# Pyjobby Testing Guide

Complete guide to running tests for pyjobby against a live PostgreSQL database.

## Quick Start

```bash
# 1. Install dependencies
make install

# 2. Start test database
make setup-db

# 3. Run tests
make test
```

## Test Infrastructure

Pyjobby uses a **live PostgreSQL database** running in Docker for all tests. This ensures tests run against real database behavior, not mocks.

### Architecture

- **PostgreSQL 15** running in Docker container
- **Transaction-based test isolation** - Each test runs in a transaction that is rolled back
- **Schema loaded once** per test session for speed
- **Parallel execution** supported via pytest-xdist

### Benefits of Live Database Testing

✅ **Real behavior**: Tests verify actual PostgreSQL semantics
✅ **Race conditions**: Can test concurrency with real locking
✅ **Performance**: Can benchmark actual query performance
✅ **SQL correctness**: Verifies SQL syntax and semantics
✅ **No mocking overhead**: Tests are simpler and more maintainable

## Setup

### Prerequisites

- Docker and docker-compose installed
- Poetry installed (for dependency management)
- Make (optional, for convenience commands)

### Installation

```bash
# Install Python dependencies
poetry install --with test,dev

# Or using make
make install
```

### Starting Test Database

```bash
# Using scripts
./scripts/setup-test-db.sh

# Or using make
make setup-db
```

The database will be available at:
- **Host**: localhost
- **Port**: 5433 (to avoid conflict with system PostgreSQL)
- **Database**: pyjobby_test
- **User**: pyjobby_test
- **Password**: pyjobby_test_password

### Stopping Test Database

```bash
# Stop (preserves data)
./scripts/stop-test-db.sh
# or
make stop-db

# Reset (wipes all data)
./scripts/reset-test-db.sh
# or
make reset-db
```

## Running Tests

### All Tests

```bash
# Using make
make test

# Or directly
./scripts/run-tests.sh

# Or with poetry
poetry run pytest
```

### Fast Tests Only

Skip slow and concurrency tests:

```bash
make test-fast

# Or
./scripts/run-tests.sh --fast
```

### Parallel Execution

Run tests in parallel using all CPU cores:

```bash
make test-parallel

# Or
./scripts/run-tests.sh --parallel
```

### Specific Tests

```bash
# Run specific test file
./scripts/run-tests.sh tests/test_sql/test_claim.py

# Run specific test function
./scripts/run-tests.sh -k test_claim_single_job

# Run tests matching pattern
./scripts/run-tests.sh -k "claim or retry"
```

### With Coverage

```bash
# Generate coverage report
make coverage

# Opens htmlcov/index.html with detailed coverage
```

## Test Categories

Tests are organized using pytest markers:

### Markers

- `@pytest.mark.slow` - Long-running tests (skip with `-m "not slow"`)
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.concurrency` - Concurrency/race condition tests
- `@pytest.mark.performance` - Performance benchmarks

### Example Usage

```bash
# Run only fast tests
pytest -m "not slow and not concurrency"

# Run only concurrency tests
pytest -m concurrency

# Run everything except performance benchmarks
pytest -m "not performance"
```

## Test Structure

```
tests/
├── conftest.py              # Fixtures and configuration
├── test_infrastructure.py   # Test the test infrastructure
├── test_sql/                # SQL statement tests
│   ├── test_claim.py
│   ├── test_state_transitions.py
│   ├── test_dependencies.py
│   └── test_recovery.py
├── test_job_lifecycle/      # Integration tests
│   ├── test_simple_job.py
│   ├── test_retry.py
│   ├── test_timeout.py
│   └── test_crash_recovery.py
├── test_concurrency/        # Race condition tests
│   ├── test_multiple_workers.py
│   ├── test_dependency_race.py
│   └── test_claim_race.py
├── test_performance/        # Performance benchmarks
│   └── test_throughput.py
└── utils/                   # Test utilities
    ├── factories.py         # Job factories
    └── helpers.py           # Test helpers
```

## Writing Tests

### Basic Test

```python
async def test_my_feature(db_connection):
    """Test description."""
    from tests.utils.factories import create_job

    # Create test job
    job_id = await create_job(
        db_connection,
        job_class="test.MyJob",
        kwargs={"arg": "value"}
    )

    # Test something
    job = await db_connection.fetchrow(
        "SELECT * FROM jorb WHERE id = $1",
        job_id
    )

    assert job["job_class"] == "test.MyJob"
```

### Using Factories

```python
from tests.utils.factories import (
    create_job,
    create_job_batch,
    create_dependency_chain,
    create_job_group,
    get_job,
    count_jobs_by_state,
)

async def test_with_factories(db_connection):
    # Create 10 jobs
    job_ids = await create_job_batch(db_connection, count=10)

    # Create dependency chain
    chain = await create_dependency_chain(db_connection, depth=3)

    # Create job group
    group_id, job_ids = await create_job_group(db_connection, group_size=5)

    # Query helpers
    job = await get_job(db_connection, job_ids[0])
    count = await count_jobs_by_state(db_connection, "queued")
```

### Concurrency Tests

```python
import asyncio

@pytest.mark.concurrency
async def test_concurrent_claims(db_connection, job_system):
    # Create job
    job_id = await create_job(db_connection)

    # Try to claim from two workers simultaneously
    async def claim_job():
        return await job_system.ex("claim", ...)

    results = await asyncio.gather(
        claim_job(),
        claim_job(),
        return_exceptions=True
    )

    # Verify only one succeeded
    assert sum(1 for r in results if r) == 1
```

## Debugging Tests

### Running Single Test with Output

```bash
# Show all output
pytest tests/test_sql/test_claim.py::test_claim_single_job -v -s

# Drop into debugger on failure
pytest tests/test_sql/test_claim.py -v --pdb
```

### Inspecting Database During Tests

```python
async def test_debug(db_connection):
    # Create data
    job_id = await create_job(db_connection)

    # Pause test and inspect database
    import pdb; pdb.set_trace()

    # Connect to database and query:
    # psql -h localhost -p 5433 -U pyjobby_test -d pyjobby_test
```

### Using pgAdmin

Start development environment with pgAdmin:

```bash
make dev
```

Then open http://localhost:5050 and connect to database:
- **Host**: postgres-test
- **Port**: 5432 (internal Docker network)
- **Database**: pyjobby_test
- **Username**: pyjobby_test
- **Password**: pyjobby_test_password

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: |
          pip install poetry
          poetry install --with test,dev

      - name: Start test database
        run: make setup-db

      - name: Run tests
        run: make test

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml
```

### GitLab CI Example

```yaml
test:
  image: python:3.9
  services:
    - postgres:15-alpine
  variables:
    POSTGRES_DB: pyjobby_test
    POSTGRES_USER: pyjobby_test
    POSTGRES_PASSWORD: pyjobby_test_password
    PYJOBBY_TEST_DSN: postgresql://pyjobby_test:pyjobby_test_password@postgres:5432/pyjobby_test
  before_script:
    - pip install poetry
    - poetry install --with test,dev
  script:
    - poetry run pytest --cov
  coverage: '/TOTAL.*\s+(\d+%)$/'
```

## Continuous Integration Checks

Run all CI checks locally:

```bash
make ci
```

This runs:
1. `make format` - Code formatting with black
2. `make lint` - Linting with ruff
3. `make type-check` - Type checking with mypy
4. `make test` - Full test suite

## Performance Benchmarking

```bash
# Run performance tests
pytest tests/test_performance/ -v

# With detailed output
pytest tests/test_performance/ -v -s --benchmark-only
```

## Troubleshooting

### Database Connection Refused

```bash
# Check if database is running
docker-compose ps

# Check logs
docker-compose logs postgres-test

# Restart database
make reset-db
```

### Tests Hanging

- Check for deadlocks in PostgreSQL logs
- Ensure transaction isolation is working
- Use pytest-timeout (already configured)

### Slow Tests

```bash
# Profile test execution
pytest --durations=10

# Run only fast tests
make test-fast
```

### Port Already in Use

If port 5433 is already in use, edit `docker-compose.yml` to use a different port.

## Best Practices

### Test Isolation

✅ **DO**: Use transaction-based isolation (automatic)
✅ **DO**: Use factories for test data
✅ **DO**: Test one thing per test
❌ **DON'T**: Share state between tests
❌ **DON'T**: Rely on test execution order

### Test Data

✅ **DO**: Use factories from `tests/utils/factories.py`
✅ **DO**: Create minimal data needed for test
✅ **DO**: Use descriptive job_class names for debugging
❌ **DON'T**: Hard-code job IDs
❌ **DON'T**: Create unnecessary test data

### Async Tests

✅ **DO**: Use `async def` for all tests with DB
✅ **DO**: Use `pytest_asyncio.fixture` for async fixtures
✅ **DO**: Use `await` for all async operations
❌ **DON'T**: Mix sync and async code
❌ **DON'T**: Use `asyncio.run()` in tests (pytest handles it)

## Coverage Goals

- **Overall**: 90%+ code coverage
- **Critical paths**: 100% coverage
  - SQL statements
  - State transitions
  - Retry logic
  - Recovery logic
- **Concurrency**: All race conditions tested

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [asyncpg documentation](https://magicstack.github.io/asyncpg/)
- [PostgreSQL testing best practices](https://www.postgresql.org/docs/current/regress.html)

## Support

For issues or questions:
1. Check this guide
2. Check test output and logs
3. Open an issue on GitHub
4. Ask in discussions

---

**Happy Testing!** 🧪✨
