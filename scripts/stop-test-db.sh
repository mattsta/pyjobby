#!/usr/bin/env bash
#
# Stop PostgreSQL service (optional - usually leave it running)
#

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}[WARN]${NC} This will stop the entire PostgreSQL service."
echo -e "${YELLOW}[WARN]${NC} This may affect other databases on your system."
read -p "Are you sure? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo -e "${GREEN}[INFO]${NC} Stopping PostgreSQL service..."
sudo systemctl stop postgresql

echo -e "${GREEN}[INFO]${NC} PostgreSQL stopped."
echo -e "${GREEN}[INFO]${NC} To start again: sudo systemctl start postgresql"
