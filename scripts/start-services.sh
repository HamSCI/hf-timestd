#!/bin/bash
# start-services.sh - Start all timestd services in the correct order
#
# Usage: sudo ./scripts/start-services.sh [--status]
#
# This script starts services in dependency order and waits for each
# to be ready before starting the next.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo)"
    exit 1
fi

# Parse arguments
STATUS_ONLY=false
if [[ "$1" == "--status" ]]; then
    STATUS_ONLY=true
fi

# Configuration
MAIN_CONFIG="/etc/hf-timestd/timestd-config.toml"
VENV_DIR="/opt/hf-timestd/venv"

# Check if VTEC is enabled
VTEC_ENABLED=$($VENV_DIR/bin/python3 -c "
import tomllib
try:
    with open('$MAIN_CONFIG', 'rb') as f:
        config = tomllib.load(f)
    print('true' if config.get('gnss_vtec', {}).get('enabled', False) else 'false')
except:
    print('false')
" 2>/dev/null)

# Service startup order (respects dependencies)
CORE_SERVICES=(
    "timestd-core-recorder"    # Phase 1: RTP → Raw Buffer
    "timestd-metrology"        # Phase 2: L1 Raw Measurements  
    "timestd-l2-calibration"   # Phase 2: L2 Calibrated Timing
    "timestd-fusion"           # Phase 3: Fusion → Chrony SHM
    "timestd-physics"          # Phase 3: TEC Estimation
    "timestd-web-api"          # Web API & Dashboard
    "timestd-radiod-monitor"   # Hardware Health Monitor
)

# Optional services (conditional)
OPTIONAL_SERVICES=()
if [[ "$VTEC_ENABLED" == "true" ]]; then
    OPTIONAL_SERVICES+=("timestd-vtec")
fi

# Timers
TIMERS=(
    "timestd-ionex-download.timer"
    "timestd-chrony-monitor.timer"
    "timestd-upload-daily.timer"
    "grape-daily.timer"
)

# Function to wait for service to be active
wait_for_service() {
    local service=$1
    local max_wait=30
    local waited=0
    
    while [[ $waited -lt $max_wait ]]; do
        if systemctl is-active --quiet "$service"; then
            return 0
        fi
        sleep 1
        ((waited++))
    done
    return 1
}

# Function to start a service with status check
start_service() {
    local service=$1
    local description=$2
    
    if systemctl is-active --quiet "$service"; then
        log_info "  ✓ $service (already running)"
        return 0
    fi
    
    printf "  Starting %-35s" "$service..."
    if systemctl start "$service" 2>/dev/null; then
        # Wait briefly for service to stabilize
        sleep 1
        if systemctl is-active --quiet "$service"; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        else
            echo -e " ${RED}✗${NC} (started but failed)"
            return 1
        fi
    else
        echo -e " ${RED}✗${NC} (failed to start)"
        return 1
    fi
}

# Function to show status
show_status() {
    echo ""
    log_step "Service Status"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    for service in "${CORE_SERVICES[@]}" "${OPTIONAL_SERVICES[@]}"; do
        if systemctl is-active --quiet "$service"; then
            status="${GREEN}●${NC} active"
        elif systemctl is-enabled --quiet "$service" 2>/dev/null; then
            status="${RED}○${NC} inactive"
        else
            status="${YELLOW}○${NC} disabled"
        fi
        printf "  %-35s %b\n" "$service" "$status"
    done
    
    echo ""
    log_step "Timer Status"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    for timer in "${TIMERS[@]}"; do
        if systemctl is-active --quiet "$timer"; then
            status="${GREEN}●${NC} active"
        else
            status="${RED}○${NC} inactive"
        fi
        printf "  %-35s %b\n" "$timer" "$status"
    done
    
    echo ""
}

# Status only mode
if [[ "$STATUS_ONLY" == "true" ]]; then
    show_status
    exit 0
fi

# Main startup sequence
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HF-TimeStd Service Startup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Phase 1: Core recorder (must start first - creates raw buffer)
log_step "Phase 1: Starting core recorder..."
start_service "timestd-core-recorder" "RTP → Raw Buffer"

# Wait for raw buffer to be created
sleep 2

# Phase 2: Metrology services
log_step "Phase 2: Starting metrology services..."
start_service "timestd-metrology" "L1 Raw Measurements"
start_service "timestd-l2-calibration" "L2 Calibrated Timing"

# Phase 3: Fusion and physics
log_step "Phase 3: Starting fusion and physics..."

# CRITICAL: Clear any stale SHM segments before starting fusion
# If chrony created them first, they'll have wrong permissions (root:600)
# Fusion needs to create them with timestd:666 for chrony to read
log_info "  Clearing stale Chrony SHM segments..."
for key in 0x4e545030 0x4e545031; do
    shmid=$(ipcs -m 2>/dev/null | grep "$key" | awk '{print $2}')
    if [[ -n "$shmid" ]]; then
        ipcrm -m "$shmid" 2>/dev/null || true
        log_info "    Removed SHM $key (id=$shmid)"
    fi
done

start_service "timestd-fusion" "Fusion → Chrony SHM"
start_service "timestd-physics" "TEC Estimation"

# Restart chronyd to pick up SHM (fusion creates SHM segments with correct permissions)
if systemctl is-active --quiet chronyd; then
    log_info "  Restarting chronyd to pick up SHM segments..."
    systemctl restart chronyd
    sleep 1
    log_info "  ✓ chronyd restarted"
fi

# Web API and monitoring
log_step "Starting web API and monitoring..."
start_service "timestd-web-api" "Web API & Dashboard"
start_service "timestd-radiod-monitor" "Hardware Health Monitor"

# Optional services
if [[ ${#OPTIONAL_SERVICES[@]} -gt 0 ]]; then
    log_step "Starting optional services..."
    for service in "${OPTIONAL_SERVICES[@]}"; do
        start_service "$service" ""
    done
fi

# Start timers
log_step "Starting periodic timers..."
for timer in "${TIMERS[@]}"; do
    if systemctl start "$timer" 2>/dev/null; then
        log_info "  ✓ $timer"
    else
        log_warn "  ✗ $timer (failed)"
    fi
done

# Show final status
show_status

# Quick health check
echo ""
log_step "Quick Health Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check chrony sources
if command -v chronyc &> /dev/null; then
    echo ""
    echo "  Chrony sources:"
    chronyc sources 2>/dev/null | grep -E "TSL|192.168" | head -5 | sed 's/^/    /'
fi

# Check web API
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    log_info "  Web API: http://localhost:8000 ✓"
else
    log_warn "  Web API: not responding yet (may need a moment)"
fi

echo ""
log_info "Startup complete. Monitor with: journalctl -u timestd-fusion -f"
echo ""
