#!/usr/bin/env bash
#
# Install PostgreSQL natively (Debian/Ubuntu)
#
# Supports: Ubuntu, Debian, Fedora, Arch
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

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    log_error "Cannot detect OS"
    exit 1
fi

log_info "Detected OS: $OS"

# Check if PostgreSQL is already installed
if command -v psql &> /dev/null; then
    PG_VERSION=$(psql --version | grep -oP '\d+' | head -1)
    log_info "PostgreSQL $PG_VERSION is already installed"
    exit 0
fi

log_info "Installing PostgreSQL..."

case "$OS" in
    ubuntu|debian)
        log_info "Installing via apt..."
        sudo apt-get update
        sudo apt-get install -y postgresql postgresql-contrib
        ;;
    fedora|rhel|centos)
        log_info "Installing via dnf/yum..."
        if command -v dnf &> /dev/null; then
            sudo dnf install -y postgresql-server postgresql-contrib
        else
            sudo yum install -y postgresql-server postgresql-contrib
        fi
        # Initialize database
        sudo postgresql-setup --initdb
        ;;
    arch|manjaro)
        log_info "Installing via pacman..."
        sudo pacman -S --noconfirm postgresql
        # Initialize database
        sudo -u postgres initdb -D /var/lib/postgres/data
        ;;
    *)
        log_error "Unsupported OS: $OS"
        log_info "Please install PostgreSQL manually"
        exit 1
        ;;
esac

# Start and enable PostgreSQL service
log_info "Starting PostgreSQL service..."
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Wait for PostgreSQL to be ready
log_info "Waiting for PostgreSQL to start..."
sleep 2

log_info "PostgreSQL installed and running!"
log_info "Run ./scripts/setup-test-db.sh to create test database"
