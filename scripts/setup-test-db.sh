#!/usr/bin/env bash
#
# Setup test database for pyjobby
#
# This script:
# 1. Starts PostgreSQL in Docker
# 2. Waits for it to be ready
# 3. Creates test database
# 4. Loads schema
#

set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if docker is installed
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if docker-compose is installed
if ! command -v docker-compose &> /dev/null; then
    log_error "docker-compose is not installed. Please install docker-compose first."
    exit 1
fi

log_info "Starting PostgreSQL test database..."
docker-compose up -d postgres-test

log_info "Waiting for PostgreSQL to be ready..."
timeout=60
elapsed=0
while ! docker-compose exec -T postgres-test pg_isready -U pyjobby_test &> /dev/null; do
    if [ $elapsed -ge $timeout ]; then
        log_error "PostgreSQL did not become ready within ${timeout} seconds"
        exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
    echo -n "."
done
echo ""

log_info "PostgreSQL is ready!"

# Test connection
log_info "Testing database connection..."
docker-compose exec -T postgres-test psql -U pyjobby_test -d pyjobby_test -c "SELECT version();" > /dev/null

log_info "Database connection successful!"

# Show connection info
log_info ""
log_info "Test database is ready!"
log_info "Connection details:"
log_info "  Host: localhost"
log_info "  Port: 5433"
log_info "  Database: pyjobby_test"
log_info "  User: pyjobby_test"
log_info "  Password: pyjobby_test_password"
log_info ""
log_info "Connection string:"
log_info "  postgresql://pyjobby_test:pyjobby_test_password@localhost:5433/pyjobby_test"
log_info ""
log_info "To stop the database: ./scripts/stop-test-db.sh"
log_info "To reset the database: ./scripts/reset-test-db.sh"
