#!/bin/bash
# stop-services.sh - Stop all timestd services cleanly
#
# Usage: sudo ./scripts/stop-services.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo)"
    exit 1
fi

# All services (reverse order of startup)
SERVICES=(
    "timestd-vtec"
    "timestd-radiod-monitor"
    "timestd-web-api"
    "timestd-physics"
    "timestd-fusion"
    "timestd-l2-calibration"
    "timestd-metrology.target"
    "timestd-core-recorder"
)

TIMERS=(
    "timestd-ionex-download.timer"
    "timestd-chrony-monitor.timer"
    "timestd-iono-reanalysis.timer"
    "grape-daily.timer"
)

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HF-TimeStd Service Shutdown"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Stop timers first
log_step "Stopping timers..."
for timer in "${TIMERS[@]}"; do
    if systemctl is-active --quiet "$timer" 2>/dev/null; then
        systemctl stop "$timer" 2>/dev/null || true
        log_info "  ✓ $timer stopped"
    fi
done

# Stop services in reverse order
log_step "Stopping services..."
for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        printf "  Stopping %-35s" "$service..."
        if systemctl stop "$service" 2>/dev/null; then
            echo -e " ${GREEN}✓${NC}"
        else
            echo -e " ${YELLOW}(already stopped)${NC}"
        fi
    fi
done

echo ""
log_info "All timestd services stopped."
echo ""
