#!/bin/bash
#
# update-production.sh - Update production installation after git pull
#
# Usage:
#   cd /home/mjh/git/hf-timestd
#   git pull
#   sudo scripts/update-production.sh
#
# This script:
# 1. Reinstalls the Python package (editable install)
# 2. Copies updated scripts to /opt/hf-timestd/scripts
# 3. Restarts affected services
# 4. Verifies the update was successful
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Configuration
INSTALL_DIR="/opt/hf-timestd"
VENV_DIR="$INSTALL_DIR/venv"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HF-TimeStd Production Update"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check we're running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (sudo)"
    exit 1
fi

# Check project directory
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    log_error "Cannot find pyproject.toml in $PROJECT_DIR"
    log_error "Run this script from the git repository root"
    exit 1
fi

# Check venv exists
if [[ ! -d "$VENV_DIR" ]]; then
    log_error "Virtual environment not found at $VENV_DIR"
    log_error "Run scripts/install.sh first"
    exit 1
fi

# =============================================================================
# Step 1: Update Python Package
# =============================================================================
log_info "Step 1: Updating Python package..."

# Use regular install (not editable) so timestd user can access the installed code
# Editable installs require the source directory to be readable by the service user
"$VENV_DIR/bin/pip" install "$PROJECT_DIR" --quiet --no-deps
log_info "  ✅ Python package updated"

# =============================================================================
# Step 2: Copy Updated Scripts
# =============================================================================
log_info "Step 2: Copying updated scripts..."

# Copy all scripts
cp "$PROJECT_DIR/scripts/"*.sh "$INSTALL_DIR/scripts/" 2>/dev/null || true
cp "$PROJECT_DIR/scripts/"*.py "$INSTALL_DIR/scripts/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true

log_info "  ✅ Scripts copied to $INSTALL_DIR/scripts/"

# =============================================================================
# Step 2b: Sync Web API Directory
# =============================================================================
log_info "Step 2b: Syncing web-api directory..."

# Sync web-api (excluding __pycache__ and .pyc files)
rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    "$PROJECT_DIR/web-api/" "$INSTALL_DIR/web-api/"

# Ensure correct ownership
chown -R timestd:timestd "$INSTALL_DIR/web-api/"

log_info "  ✅ Web API synced to $INSTALL_DIR/web-api/"

# =============================================================================
# Step 3: Update Systemd Service Files (if changed)
# =============================================================================
log_info "Step 3: Checking systemd service files..."

SYSTEMD_DIR="/etc/systemd/system"
SERVICES_UPDATED=false

for service_file in "$PROJECT_DIR/systemd/"*.service "$PROJECT_DIR/systemd/"*.timer; do
    if [[ -f "$service_file" ]]; then
        filename=$(basename "$service_file")
        if [[ -f "$SYSTEMD_DIR/$filename" ]]; then
            # Check if file has changed
            if ! diff -q "$service_file" "$SYSTEMD_DIR/$filename" > /dev/null 2>&1; then
                cp "$service_file" "$SYSTEMD_DIR/$filename"
                log_info "  Updated: $filename"
                SERVICES_UPDATED=true
            fi
        fi
    fi
done

if [[ "$SERVICES_UPDATED" == "true" ]]; then
    systemctl daemon-reload
    log_info "  ✅ Systemd daemon reloaded"
else
    log_info "  ✅ No service file changes detected"
fi

# =============================================================================
# Step 4: Restart Services
# =============================================================================
log_info "Step 4: Restarting services..."

# List of services to restart (in order)
SERVICES=(
    "timestd-fusion"
    "timestd-metrology"
    "timestd-l2-calibration"
    "timestd-physics"
    "timestd-web-api"
)

for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$service"; then
        systemctl restart "$service"
        log_info "  Restarted: $service"
    fi
done

# Note: We don't restart core-recorder to avoid data gaps
if systemctl is-active --quiet "timestd-core-recorder"; then
    log_warn "  ⚠️  timestd-core-recorder NOT restarted (to avoid data gaps)"
    log_info "     Restart manually if needed: sudo systemctl restart timestd-core-recorder"
fi

# =============================================================================
# Step 5: Verify Update
# =============================================================================
log_info "Step 5: Verifying update..."

# Check services are running
FAILED_SERVICES=()
for service in "${SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$service" 2>/dev/null; then
        if ! systemctl is-active --quiet "$service"; then
            FAILED_SERVICES+=("$service")
        fi
    fi
done

if [[ ${#FAILED_SERVICES[@]} -gt 0 ]]; then
    log_warn "  ⚠️  Some services failed to restart:"
    for service in "${FAILED_SERVICES[@]}"; do
        log_warn "     - $service"
    done
    log_info "     Check logs: journalctl -u <service> -n 50"
else
    log_info "  ✅ All restarted services are running"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Update Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
log_info "Python package and scripts updated"
log_info "Services restarted (except core-recorder)"
echo ""
log_info "Monitor fusion: journalctl -u timestd-fusion -f"
log_info "Check status:   systemctl status timestd-fusion timestd-metrology"
echo ""
