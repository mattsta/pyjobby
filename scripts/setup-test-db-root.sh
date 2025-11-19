#!/usr/bin/env bash
#
# Setup test database for pyjobby (root-compatible version)
#
# This script works when running as root, using 'su' instead of 'sudo'
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

# Check if PostgreSQL is installed
if ! command -v psql &> /dev/null; then
    log_error "PostgreSQL is not installed!"
    exit 1
fi

log_info "PostgreSQL is installed"

# Fix permissions for PostgreSQL
log_info "Fixing PostgreSQL permissions..."
chown -R postgres:postgres /var/lib/postgresql/
chown -R postgres:postgres /etc/postgresql/
mkdir -p /var/log/postgresql /var/run/postgresql
chown -R postgres:postgres /var/log/postgresql /var/run/postgresql
chmod 2775 /var/run/postgresql

# Disable SSL to avoid certificate permission issues
log_info "Disabling SSL in PostgreSQL config..."
sed -i "s/^ssl = on/ssl = off/" /etc/postgresql/16/main/postgresql.conf 2>/dev/null || true

# Start PostgreSQL using pg_ctlcluster
log_info "Starting PostgreSQL..."
if pg_ctlcluster 16 main start 2>&1; then
    log_info "PostgreSQL started successfully"
else
    log_warn "PostgreSQL may already be running"
fi

sleep 2

# Test database configuration
DB_NAME="pyjobby_test"
DB_USER="pyjobby_test"
DB_PASS="pyjobby_test_password"
SCHEMA_FILE="priv/schema.sql"

# Check if database already exists
if su - postgres -c "psql -lqt" | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    log_warn "Database '$DB_NAME' already exists - dropping and recreating"
    su - postgres -c "psql -c \"DROP DATABASE IF EXISTS $DB_NAME;\""
    su - postgres -c "psql -c \"DROP USER IF EXISTS $DB_USER;\""
fi

# Create test user
log_info "Creating test user '$DB_USER'..."
su - postgres -c "psql -c \"CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';\"" 2>/dev/null || {
    log_warn "User already exists, resetting password..."
    su - postgres -c "psql -c \"ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';\""
}

# Create test database
log_info "Creating test database '$DB_NAME'..."
su - postgres -c "psql -c \"CREATE DATABASE $DB_NAME OWNER $DB_USER;\"" 2>/dev/null || {
    log_warn "Database already exists"
}

# Grant privileges
log_info "Granting privileges..."
su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;\""

# Change to project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Load schema
if [ -f "$SCHEMA_FILE" ]; then
    log_info "Loading schema from $SCHEMA_FILE..."
    su - postgres -c "cd '$PROJECT_ROOT' && psql -d \"$DB_NAME\" -f \"$SCHEMA_FILE\"" > /dev/null
    log_info "Schema loaded successfully"
else
    log_error "Schema file not found: $SCHEMA_FILE (pwd: $(pwd))"
    exit 1
fi

# Apply scheduler migration (needed for test_client.py)
log_info "Applying scheduler migration..."
if [ -f "priv/migrations/003_add_recurring_scheduler.sql" ]; then
    log_info "Applying 003_add_recurring_scheduler.sql..."
    su - postgres -c "cd '$PROJECT_ROOT' && psql -d \"$DB_NAME\" -f 'priv/migrations/003_add_recurring_scheduler.sql'" > /dev/null
fi

# Apply Phase 2 migrations
log_info "Applying Phase 2 migrations..."
for migration in priv/migrations/005_*.sql priv/migrations/006_*.sql priv/migrations/007_*.sql priv/migrations/008_*.sql; do
    if [ -f "$migration" ]; then
        log_info "Applying $(basename "$migration")..."
        su - postgres -c "cd '$PROJECT_ROOT' && psql -d \"$DB_NAME\" -f \"$migration\"" > /dev/null
    fi
done

# Grant all table and sequence permissions to test user
log_info "Granting table and sequence permissions..."
su - postgres -c "psql -d \"$DB_NAME\" -c 'GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;'" > /dev/null
su - postgres -c "psql -d \"$DB_NAME\" -c 'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;'" > /dev/null

# Test connection
log_info "Testing connection..."
PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null

log_info ""
log_info "✓ Test database is ready!"
log_info "Connection details:"
log_info "  Host: localhost"
log_info "  Port: 5432"
log_info "  Database: $DB_NAME"
log_info "  User: $DB_USER"
log_info "  Password: $DB_PASS"
log_info ""
log_info "Connection string:"
log_info "  postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
