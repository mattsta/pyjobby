#!/usr/bin/env bash
#
# Reset test database (drop and recreate)
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DB_NAME="pyjobby_test"
DB_USER="pyjobby_test"
SCHEMA_FILE="priv/schema.sql"

echo -e "${YELLOW}[WARN]${NC} This will DROP the test database and all data!"
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo -e "${GREEN}[INFO]${NC} Dropping test database..."
sudo -u postgres psql -c "DROP DATABASE IF EXISTS $DB_NAME;"

echo -e "${GREEN}[INFO]${NC} Recreating test database..."
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

if [ -f "$SCHEMA_FILE" ]; then
    echo -e "${GREEN}[INFO]${NC} Loading schema..."
    sudo -u postgres psql -d "$DB_NAME" -f "$SCHEMA_FILE" > /dev/null
    echo -e "${GREEN}[INFO]${NC} Schema loaded"
fi

echo -e "${GREEN}[INFO]${NC} Test database has been reset!"
