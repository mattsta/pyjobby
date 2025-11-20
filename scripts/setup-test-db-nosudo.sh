#!/usr/bin/env bash
#
# Setup test database without sudo (assumes PostgreSQL is running and you have postgres user access)
#
# This script:
# 1. Creates test user and database as current user or postgres
# 2. Loads schema
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Test database configuration
DB_NAME="pyjobby_test"
DB_USER="pyjobby_test"
DB_PASS="pyjobby_test_password"
SCHEMA_FILE="priv/schema.sql"

# Check if we can connect as postgres user
if psql -U postgres -c "SELECT 1" > /dev/null 2>&1; then
    PG_USER="postgres"
    log_info "Connected as postgres user"
elif [ "${USER:-}" = "postgres" ]; then
    PG_USER="postgres"
    log_info "Running as postgres user"
else
    log_error "Cannot connect to PostgreSQL"
    log_info "Either:"
    log_info "  1. Run as postgres user: su - postgres -c $0"
    log_info "  2. Configure pg_hba.conf to allow local connections"
    log_info "  3. Set PGPASSWORD environment variable"
    exit 1
fi

# Check if database already exists
if psql -U "$PG_USER" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    log_warn "Database '$DB_NAME' already exists"
    read -p "Do you want to drop and recreate it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Dropping existing database..."
        psql -U "$PG_USER" -c "DROP DATABASE IF EXISTS $DB_NAME;"
        psql -U "$PG_USER" -c "DROP USER IF EXISTS $DB_USER;"
    else
        log_info "Using existing database"
        exit 0
    fi
fi

# Create test user
log_info "Creating test user '$DB_USER'..."
psql -U "$PG_USER" -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || {
    log_warn "User already exists, resetting password..."
    psql -U "$PG_USER" -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';"
}

# Create test database
log_info "Creating test database '$DB_NAME'..."
psql -U "$PG_USER" -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || {
    log_warn "Database already exists"
}

# Grant privileges
log_info "Granting privileges..."
psql -U "$PG_USER" -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

# Load schema
if [ -f "$SCHEMA_FILE" ]; then
    log_info "Loading schema from $SCHEMA_FILE..."
    psql -U "$PG_USER" -d "$DB_NAME" -f "$SCHEMA_FILE" > /dev/null
    log_info "Schema loaded successfully"
else
    log_warn "Schema file not found: $SCHEMA_FILE"
    log_warn "You'll need to load the schema manually"
fi

# Test connection
log_info "Testing connection..."
PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null

log_info ""
log_info "Test database is ready!"
log_info "Connection details:"
log_info "  Host: localhost"
log_info "  Port: 5432"
log_info "  Database: $DB_NAME"
log_info "  User: $DB_USER"
log_info "  Password: $DB_PASS"
log_info ""
log_info "Connection string:"
log_info "  postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
log_info ""
log_info "To run tests: make test"
