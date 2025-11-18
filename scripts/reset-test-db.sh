#!/usr/bin/env bash
#
# Reset test database (wipe all data and reload schema)
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}[WARN]${NC} This will wipe all data in the test database!"
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo -e "${GREEN}[INFO]${NC} Stopping test database..."
docker-compose down

echo -e "${GREEN}[INFO]${NC} Removing test database volume..."
docker volume rm pyjobby-test-data 2>/dev/null || true

echo -e "${GREEN}[INFO]${NC} Starting fresh test database..."
./scripts/setup-test-db.sh

echo -e "${GREEN}[INFO]${NC} Test database has been reset!"
