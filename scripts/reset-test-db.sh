#!/usr/bin/env bash
#
# Reset test database (drop and recreate, then install the base schema)
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DB_NAME="pyjobby_test"
DB_USER="pyjobby_test"
DB_PASS="pyjobby_test_password"

echo -e "${YELLOW}[WARN]${NC} This will DROP the test database and all data!"
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo -e "${GREEN}[INFO]${NC} Dropping test database..."
psql -U postgres -c "DROP DATABASE IF EXISTS $DB_NAME;" 2>/dev/null \
    || sudo -u postgres psql -c "DROP DATABASE IF EXISTS $DB_NAME;"

echo -e "${GREEN}[INFO]${NC} Recreating test database..."
psql -U postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null \
    || sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

echo -e "${GREEN}[INFO]${NC} Installing base schema (pj-admin db migrate)..."
PYJOBBY_DSN="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME" \
    poetry run pj-admin db migrate

echo -e "${GREEN}[INFO]${NC} Test database has been reset!"
