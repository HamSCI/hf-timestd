#!/bin/bash
# =============================================================================
# Deploy Service Stability Improvements
# =============================================================================
# Deploys systemd watchdog and monitoring improvements to production
#
# Changes:
#   1. Updated timestd-fusion.service with watchdog support
#   2. Added check-chrony-reach.sh monitoring script
#   3. Added timestd-chrony-monitor service and timer
#
# Usage:
#   sudo ./deploy-service-improvements.sh
# =============================================================================

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   log_error "This script must be run as root (use sudo)"
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=============================================="
echo "  Service Stability Improvements Deployment"
echo "=============================================="
echo ""

# Step 1: Copy monitoring script
log_info "Installing Chrony reach monitoring script..."
cp "$PROJECT_DIR/scripts/check-chrony-reach.sh" /opt/hf-timestd/scripts/
chmod +x /opt/hf-timestd/scripts/check-chrony-reach.sh
chown timestd:timestd /opt/hf-timestd/scripts/check-chrony-reach.sh
log_info "  ✅ Installed: /opt/hf-timestd/scripts/check-chrony-reach.sh"

# Step 2: Test monitoring script
log_info "Testing monitoring script..."
if /opt/hf-timestd/scripts/check-chrony-reach.sh --verbose; then
    log_info "  ✅ Monitoring script works correctly"
else
    log_warn "  ⚠️  Monitoring script returned non-zero (reach may be low)"
fi

# Step 3: Update fusion service
log_info "Updating timestd-fusion.service with watchdog support..."
cp "$PROJECT_DIR/systemd/timestd-fusion.service" /etc/systemd/system/
log_info "  ✅ Updated: /etc/systemd/system/timestd-fusion.service"
log_info "     - Type changed from 'simple' to 'notify'"
log_info "     - WatchdogSec=30 enabled"

# Step 4: Install monitoring service and timer
log_info "Installing Chrony monitoring service and timer..."
cp "$PROJECT_DIR/systemd/timestd-chrony-monitor.service" /etc/systemd/system/
cp "$PROJECT_DIR/systemd/timestd-chrony-monitor.timer" /etc/systemd/system/
log_info "  ✅ Installed: /etc/systemd/system/timestd-chrony-monitor.service"
log_info "  ✅ Installed: /etc/systemd/system/timestd-chrony-monitor.timer"

# Step 5: Reload systemd
log_info "Reloading systemd daemon..."
systemctl daemon-reload
log_info "  ✅ Systemd daemon reloaded"

# Step 6: Enable monitoring timer
log_info "Enabling Chrony monitoring timer..."
systemctl enable timestd-chrony-monitor.timer
log_info "  ✅ Timer enabled (will run every 5 minutes)"

# Step 7: Restart fusion service
log_info "Restarting timestd-fusion service..."
log_warn "  This will briefly interrupt Chrony SHM updates"
read -p "  Continue? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    systemctl restart timestd-fusion
    sleep 3
    
    if systemctl is-active --quiet timestd-fusion; then
        log_info "  ✅ Fusion service restarted successfully"
    else
        log_error "  ❌ Fusion service failed to start!"
        log_error "     Check logs: journalctl -u timestd-fusion -n 50"
        exit 1
    fi
else
    log_warn "  Skipped fusion service restart"
    log_warn "  Changes will take effect on next service restart"
fi

# Step 8: Start monitoring timer
log_info "Starting Chrony monitoring timer..."
systemctl start timestd-chrony-monitor.timer
log_info "  ✅ Timer started"

# Step 9: Verify deployment
echo ""
log_info "Verifying deployment..."

# Check fusion service
if systemctl is-active --quiet timestd-fusion; then
    log_info "  ✅ Fusion service: active"
else
    log_warn "  ⚠️  Fusion service: inactive"
fi

# Check monitoring timer
if systemctl is-active --quiet timestd-chrony-monitor.timer; then
    log_info "  ✅ Monitoring timer: active"
else
    log_warn "  ⚠️  Monitoring timer: inactive"
fi

# Check Chrony reach
REACH_LINE=$(chronyc sources 2>/dev/null | grep "TMGR" || echo "")
if [[ -n "$REACH_LINE" ]]; then
    REACH=$(echo "$REACH_LINE" | awk '{print $5}')
    log_info "  ✅ Chrony TMGR reach: $REACH (octal)"
else
    log_warn "  ⚠️  TMGR source not found in Chrony"
fi

echo ""
echo "=============================================="
echo "  Deployment Complete"
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Monitor fusion service for 24 hours: journalctl -u timestd-fusion -f"
echo "  2. Check Chrony reach: watch -n 10 'chronyc sources -v | grep TMGR'"
echo "  3. View monitoring timer status: systemctl status timestd-chrony-monitor.timer"
echo "  4. View monitoring logs: journalctl -u timestd-chrony-monitor"
echo ""
