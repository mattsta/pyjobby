#!/usr/bin/env bash
#
# Run pyjobby test suite
#
# Usage:
#   ./scripts/run-tests.sh              # Run all tests
#   ./scripts/run-tests.sh -k test_claim  # Run specific test
#   ./scripts/run-tests.sh --cov        # Run with coverage
#   ./scripts/run-tests.sh --fast       # Run fast tests only (skip slow/concurrency)
#   ./scripts/run-tests.sh --parallel   # Run tests in parallel
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if database is running
if ! docker-compose ps postgres-test | grep -q "Up"; then
    log_error "Test database is not running!"
    log_info "Start it with: ./scripts/setup-test-db.sh"
    exit 1
fi

# Wait for database to be ready
log_info "Checking database connection..."
if ! docker-compose exec -T postgres-test pg_isready -U pyjobby_test &> /dev/null; then
    log_error "Database is not ready"
    exit 1
fi

log_info "Database is ready!"

# Parse arguments
PYTEST_ARGS=()
FAST_ONLY=false
PARALLEL=false

for arg in "$@"; do
    case $arg in
        --fast)
            FAST_ONLY=true
            ;;
        --parallel)
            PARALLEL=true
            ;;
        *)
            PYTEST_ARGS+=("$arg")
            ;;
    esac
done

# Build pytest command
PYTEST_CMD="poetry run pytest"

if [ "$FAST_ONLY" = true ]; then
    PYTEST_CMD="$PYTEST_CMD -m 'not slow and not concurrency'"
fi

if [ "$PARALLEL" = true ]; then
    # Use all available CPU cores
    PYTEST_CMD="$PYTEST_CMD -n auto"
fi

# Add any additional arguments
if [ ${#PYTEST_ARGS[@]} -gt 0 ]; then
    PYTEST_CMD="$PYTEST_CMD ${PYTEST_ARGS[*]}"
fi

# Run tests
log_info "Running tests..."
log_info "Command: $PYTEST_CMD"
echo ""

# Export database connection for tests
export PYJOBBY_TEST_DSN="postgresql://pyjobby_test:pyjobby_test_password@localhost:5433/pyjobby_test"

eval "$PYTEST_CMD"

exit_code=$?

if [ $exit_code -eq 0 ]; then
    echo ""
    log_info "All tests passed! ✓"
else
    echo ""
    log_error "Some tests failed. ✗"
fi

exit $exit_code
