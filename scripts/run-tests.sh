#!/usr/bin/env bash
#
# Run pyjobby test suite against PostgreSQL (portable: macOS + Linux, no sudo).
#
# Usage:
#   ./scripts/run-tests.sh                # Run all tests
#   ./scripts/run-tests.sh -k test_claim  # Run specific test
#   ./scripts/run-tests.sh --fast         # Skip slow/concurrency tests
#   ./scripts/run-tests.sh --parallel     # Run tests in parallel
#
# Set PYJOBBY_TEST_DSN to override the default test database.

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

export PYJOBBY_TEST_DSN="${PYJOBBY_TEST_DSN:-postgresql://pyjobby_test:pyjobby_test_password@localhost:5432/pyjobby_test}"

# Portable PostgreSQL liveness check (no sudo, no systemctl)
if command -v pg_isready >/dev/null 2>&1; then
    if ! pg_isready -h localhost -p 5432 -q; then
        log_error "PostgreSQL is not accepting connections on localhost:5432"
        log_info "Create/start the test database with: ./scripts/setup-test-db.sh"
        exit 1
    fi
fi

PYTEST_ARGS=()

for arg in "$@"; do
    case $arg in
        --fast)
            # pytest's -m is single-valued, so this REPLACES the "not performance"
            # filter in pyproject's addopts rather than narrowing it. Repeat it
            # here or --fast silently re-enables throughput assertions, which
            # measure the machine rather than the code.
            PYTEST_ARGS+=(-m "not slow and not concurrency and not performance")
            ;;
        --parallel)
            PYTEST_ARGS+=(-n auto)
            ;;
        *)
            PYTEST_ARGS+=("$arg")
            ;;
    esac
done

log_info "Running tests..."
poetry run pytest ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
