#!/bin/bash
#
# Apply Phase 2 migrations to pyjobby database
#

set -e  # Exit on error

# Database connection details
DB_HOST="${PYJOBBY_DB_HOST:-localhost}"
DB_PORT="${PYJOBBY_DB_PORT:-5432}"
DB_USER="${PYJOBBY_DB_USER:-pyjobby_test}"
DB_PASSWORD="${PYJOBBY_DB_PASSWORD:-pyjobby_test_password}"
DB_NAME="${PYJOBBY_DB_NAME:-pyjobby_test}"

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MIGRATIONS_DIR="$PROJECT_DIR/priv/migrations"

echo "Applying Phase 2 migrations to $DB_NAME@$DB_HOST:$DB_PORT"
echo "=============================================="

# Set PGPASSWORD environment variable for psql
export PGPASSWORD="$DB_PASSWORD"

# Apply each Phase 2 migration
for migration in 005_add_result_storage.sql \
                 006_add_retry_strategy.sql \
                 007_add_timeout_enforcement.sql \
                 008_add_dag_support.sql; do

    echo ""
    echo "Applying $migration..."

    if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -f "$MIGRATIONS_DIR/$migration" > /dev/null 2>&1; then
        echo "✓ $migration applied successfully"
    else
        echo "✗ $migration failed (may already be applied)"
    fi
done

echo ""
echo "=============================================="
echo "Phase 2 migrations complete!"
echo ""
echo "Verifying new columns..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c \
    "SELECT column_name, data_type
     FROM information_schema.columns
     WHERE table_name = 'jorb'
     AND column_name IN ('result', 'timeout_at', 'dag_id')
     ORDER BY column_name;"

echo ""
echo "Verifying new tables..."
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c \
    "SELECT tablename
     FROM pg_tables
     WHERE schemaname = 'public'
     AND tablename IN ('jorb_dag', 'jorb_dependencies')
     ORDER BY tablename;"

echo ""
echo "All done!"
