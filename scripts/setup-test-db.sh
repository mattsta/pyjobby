#!/usr/bin/env bash
#
# Setup test database for pyjobby (portable: macOS + Linux).
#
# 1. Checks PostgreSQL is installed and accepting connections
# 2. Creates test user and database (direct superuser psql, or sudo fallback)
# 3. Installs schema + migrations via `pj-admin db migrate`
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if PostgreSQL is installed
if ! command -v psql &> /dev/null; then
    log_error "PostgreSQL is not installed!"
    log_info "Run: ./scripts/install-postgres.sh"
    exit 1
fi

# Check if PostgreSQL is accepting connections (portable; no systemctl)
if ! pg_isready -h localhost -p 5432 -q; then
    log_error "PostgreSQL is not accepting connections on localhost:5432"
    log_info "Start it first (e.g. 'brew services start postgresql' or 'sudo systemctl start postgresql')"
    exit 1
fi

log_info "PostgreSQL is running"

# Test database configuration
DB_NAME="pyjobby_test"
DB_USER="pyjobby_test"
DB_PASS="pyjobby_test_password"

# Pick a way to run superuser statements: direct (Homebrew-style, current
# user is superuser), psql -U postgres, or sudo -u postgres.
run_su_psql() {
    if psql -h localhost postgres -c "SELECT 1" > /dev/null 2>&1; then
        psql -h localhost postgres -c "$1"
    elif psql -U postgres -c "SELECT 1" > /dev/null 2>&1; then
        psql -U postgres -c "$1"
    else
        sudo -u postgres psql -c "$1"
    fi
}

# Check if database already exists
if run_su_psql "SELECT 1" > /dev/null 2>&1 && \
   run_su_psql "\l" 2>/dev/null | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    log_warn "Database '$DB_NAME' already exists"
    read -p "Do you want to drop and recreate it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Dropping existing database..."
        run_su_psql "DROP DATABASE IF EXISTS $DB_NAME;"
        run_su_psql "DROP USER IF EXISTS $DB_USER;"
    else
        log_info "Using existing database (running migrations only)"
        PYJOBBY_DSN="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME" \
            poetry run pj-admin db migrate
        exit 0
    fi
fi

# Create test user
log_info "Creating test user '$DB_USER'..."
run_su_psql "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || {
    log_warn "User already exists, resetting password..."
    run_su_psql "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';"
}

# Create test database
log_info "Creating test database '$DB_NAME'..."
run_su_psql "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || {
    log_warn "Database already exists"
}

# Grant privileges
log_info "Granting privileges..."
run_su_psql "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

# Install schema + all migrations through the unified runner
log_info "Installing schema + migrations..."
PYJOBBY_DSN="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME" \
    poetry run pj-admin db migrate

# Test connection
log_info "Testing connection..."
PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null

log_info ""
log_info "Test database is ready!"
log_info "Connection string:"
log_info "  postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
log_info ""
log_info "To reset the database: ./scripts/reset-test-db.sh"
log_info "To run tests: make test"
