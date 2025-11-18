#!/usr/bin/env bash
#
# Stop test database
#

set -euo pipefail

GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}[INFO]${NC} Stopping PostgreSQL test database..."
docker-compose down

echo -e "${GREEN}[INFO]${NC} Test database stopped."
echo -e "${GREEN}[INFO]${NC} Note: Data is preserved. Use ./scripts/reset-test-db.sh to wipe data."
