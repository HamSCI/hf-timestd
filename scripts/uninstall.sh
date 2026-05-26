#!/bin/bash
# =============================================================================
# TimeStd Recorder Uninstallation Script
# =============================================================================
# Usage: sudo ./uninstall.sh [--keep-data]
#
# This script reverses all changes made by install.sh:
#   1. Stops and disables systemd services
#   2. Removes service files and configurations
#   3. Removes Python virtual environment
#   4. Optionally removes data directories
#   5. Removes system user (production only)
#   6. Cleans up system configurations
# =============================================================================

set -euo pipefail

# Default values
KEEP_DATA=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $*"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode)
            # Legacy flag — accepted for backward compat, ignored
            shift 2
            ;;
        --keep-data)
            KEEP_DATA=true
            shift
            ;;
        --help|-h)
            echo "TimeStd Recorder Uninstallation Script"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --mode MODE             (ignored, accepted for backward compatibility)"
            echo "  --keep-data             Keep data directories (default: remove)"
            echo "  --help, -h              Show this help"
            echo ""
            echo "WARNING: This will remove all hf-timestd components."
            echo "         Use --keep-data to preserve recorded data."
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "=============================================="
echo "  TimeStd Recorder Uninstallation"
echo "=============================================="
echo "  Keep data: $KEEP_DATA"
echo "=============================================="
echo ""

# Confirm uninstall
log_warn "This will remove ALL hf-timestd components from the system."
if [[ "$KEEP_DATA" == "false" ]]; then
    log_warn "Data in /var/lib/timestd will be PERMANENTLY DELETED."
fi
read -p "Are you sure you want to continue? (yes/no) " -r
echo
if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    log_info "Uninstall cancelled."
    exit 0
fi

# Production paths
DATA_ROOT="/var/lib/timestd"
CONFIG_DIR="/etc/hf-timestd"
VENV_DIR="/opt/git/sigmond/hf-timestd/venv"
WEBUI_DIR="/opt/git/sigmond/hf-timestd/web-api"
LOG_DIR="/var/log/hf-timestd"
INSTALL_USER="timestd"

# =============================================================================
# Step 1: Stop and Disable Services
# =============================================================================
log_step "Stopping and disabling systemd services..."

SERVICES=(
    "timestd-core-recorder.service"
    "timestd-metrology.service"
    "timestd-metrology.target"
    "timestd-l2-calibration.service"
    "timestd-fusion.service"
    "timestd-physics.service"
    "timestd-web-api.service"
    "timestd-vtec.service"
    "timestd-radiod-monitor.service"
    "timestd-chrony-monitor.service"
    "timestd-ionex-download.service"
    "timestd-iono-reanalysis.service"
    "grape-daily.service"
    # Legacy services
    "timestd-analytics.service"
    "timestd-web-ui.service"
)

# Stop all metrology template instances first (glob doesn't work with systemctl stop)
for inst in $(systemctl list-units 'timestd-metrology@*.service' --no-legend --all 2>/dev/null | awk '{print $1}'); do
    log_info "  Stopping $inst..."
    sudo systemctl stop "$inst" 2>/dev/null || true
    sudo systemctl disable "$inst" 2>/dev/null || true
    log_info "  ✅ Stopped and disabled $inst"
done

for service in "${SERVICES[@]}"; do
    if systemctl list-unit-files | grep -q "$service"; then
        log_info "  Stopping $service..."
        sudo systemctl stop "$service" 2>/dev/null || true
        sudo systemctl disable "$service" 2>/dev/null || true
        log_info "  ✅ Stopped and disabled $service"
    fi
done

# Stop timers
TIMERS=(
    "timestd-upload-daily.timer"
    "timestd-ionex-download.timer"
    "timestd-chrony-monitor.timer"
    "timestd-iono-reanalysis.timer"
    "grape-daily.timer"
)

for timer in "${TIMERS[@]}"; do
    if systemctl list-unit-files | grep -q "$timer"; then
        log_info "  Stopping $timer..."
        sudo systemctl stop "$timer" 2>/dev/null || true
        sudo systemctl disable "$timer" 2>/dev/null || true
        log_info "  ✅ Stopped and disabled $timer"
    fi
done

# =============================================================================
# Step 2: Remove Service Files
# =============================================================================
log_step "Removing systemd service files..."

SERVICE_FILES=(
    "/etc/systemd/system/timestd-core-recorder.service"
    "/etc/systemd/system/timestd-metrology.service"
    "/etc/systemd/system/timestd-metrology.service.disabled"
    "/etc/systemd/system/timestd-metrology.target"
    "/etc/systemd/system/timestd-metrology@.service"
    "/etc/systemd/system/timestd-l2-calibration.service"
    "/etc/systemd/system/timestd-fusion.service"
    "/etc/systemd/system/timestd-physics.service"
    "/etc/systemd/system/timestd-web-api.service"
    "/etc/systemd/system/timestd-vtec.service"
    "/etc/systemd/system/timestd-radiod-monitor.service"
    "/etc/systemd/system/timestd-chrony-monitor.service"
    "/etc/systemd/system/timestd-chrony-monitor.timer"
    "/etc/systemd/system/timestd-ionex-download.service"
    "/etc/systemd/system/timestd-ionex-download.timer"
    "/etc/systemd/system/timestd-upload-daily.service"
    "/etc/systemd/system/timestd-upload-daily.timer"
    "/etc/systemd/system/timestd-alert@.service"
    "/etc/systemd/system/timestd-iono-reanalysis.service"
    "/etc/systemd/system/timestd-iono-reanalysis.timer"
    "/etc/systemd/system/grape-daily.service"
    "/etc/systemd/system/grape-daily.timer"
    # Legacy services (no longer used)
    "/etc/systemd/system/timestd-analytics.service"
    "/etc/systemd/system/timestd-web-ui.service"
)

for file in "${SERVICE_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        sudo rm -f "$file"
        log_info "  Removed: $file"
    fi
done

# Remove chronyd override
if [[ -f "/etc/systemd/system/chronyd.service.d/timestd-shm.conf" ]]; then
    sudo rm -f "/etc/systemd/system/chronyd.service.d/timestd-shm.conf"
    log_info "  Removed: /etc/systemd/system/chronyd.service.d/timestd-shm.conf"
    
    # Remove directory if empty
    if [[ -d "/etc/systemd/system/chronyd.service.d" ]]; then
        sudo rmdir "/etc/systemd/system/chronyd.service.d" 2>/dev/null || true
    fi
fi

# Reload systemd
sudo systemctl daemon-reload
log_info "  ✅ Systemd daemon reloaded"

# =============================================================================
# Step 3: Remove Chrony Configuration
# =============================================================================
log_step "Removing chrony configuration..."

# Detect chrony config file
CHRONY_CONF=""
if [[ -f "/etc/chrony/chrony.conf" ]]; then
    CHRONY_CONF="/etc/chrony/chrony.conf"
elif [[ -f "/etc/chrony.conf" ]]; then
    CHRONY_CONF="/etc/chrony.conf"
fi

if [[ -n "$CHRONY_CONF" ]]; then
    CHRONY_CHANGED=false
    
    if grep -q "refclock SHM 0 refid TSL1" "$CHRONY_CONF" 2>/dev/null; then
        log_info "  Removing timestd SHM refclock from $CHRONY_CONF..."
        # Remove the dual refclock block added by install.sh
        sudo sed -i '/# HF Time Standard Dual Chrony Refclock Configuration/,/# TSL1 serves as backup/d' "$CHRONY_CONF"
        log_info "  ✅ Removed SHM refclock configuration"
        CHRONY_CHANGED=true
    else
        log_info "  ℹ️  No timestd SHM configuration found in chrony.conf"
    fi
    
    # Remove GNSS timeserver if added by install.sh
    if grep -q "# GNSS Timeserver (ZED-F9P host providing NTP)" "$CHRONY_CONF" 2>/dev/null; then
        log_info "  Removing GNSS timeserver from $CHRONY_CONF..."
        sudo sed -i '/# GNSS Timeserver (ZED-F9P host providing NTP)/,/^server .* iburst prefer$/d' "$CHRONY_CONF"
        log_info "  ✅ Removed GNSS timeserver configuration"
        CHRONY_CHANGED=true
    fi
    
    # Restart chronyd if we made changes and it's running
    if [[ "$CHRONY_CHANGED" == "true" ]] && systemctl is-active --quiet chronyd; then
        log_info "  Restarting chronyd..."
        sudo systemctl restart chronyd
    fi
fi

# =============================================================================
# Step 4: Remove System Configurations
# =============================================================================
log_step "Removing system configurations..."

# Remove UDP buffer configuration
if [[ -f "/etc/sysctl.d/99-timestd.conf" ]]; then
    sudo rm -f "/etc/sysctl.d/99-timestd.conf"
    log_info "  Removed: /etc/sysctl.d/99-timestd.conf"
fi

# Remove tmpfiles.d configuration
if [[ -f "/etc/tmpfiles.d/timestd.conf" ]]; then
    sudo rm -f "/etc/tmpfiles.d/timestd.conf"
    log_info "  Removed: /etc/tmpfiles.d/timestd.conf"
fi

# Remove logrotate configuration
if [[ -f "/etc/logrotate.d/hf-timestd" ]]; then
    sudo rm -f "/etc/logrotate.d/hf-timestd"
    log_info "  Removed: /etc/logrotate.d/hf-timestd"
fi

# Remove cron jobs
if [[ -f "/etc/cron.d/timestd-freshness-monitor" ]]; then
    sudo rm -f "/etc/cron.d/timestd-freshness-monitor"
    log_info "  Removed: /etc/cron.d/timestd-freshness-monitor"
fi

# Remove shared memory directory
if [[ -d "/dev/shm/timestd" ]]; then
    sudo rm -rf "/dev/shm/timestd"
    log_info "  Removed: /dev/shm/timestd"
fi

# =============================================================================
# Step 5: Remove Python Virtual Environment
# =============================================================================
log_step "Removing Python virtual environment..."

if [[ -d "$VENV_DIR" ]]; then
    sudo rm -rf "$VENV_DIR"
    log_info "  Removed: $VENV_DIR"
else
    log_info "  ℹ️  Virtual environment not found: $VENV_DIR"
fi

# Remove system-wide pip package (installed by install.sh for services using system Python)
if pip3 show hf-timestd &>/dev/null; then
    log_info "  Removing system pip package..."
    sudo pip3 uninstall hf-timestd -y --quiet --break-system-packages 2>/dev/null || true
    log_info "  ✅ Removed system pip package"
fi

# =============================================================================
# Step 6: Remove Installation Directories
# =============================================================================
log_step "Removing installation directories..."

# Remove /opt/git/sigmond/hf-timestd (except venv which was already removed)
if [[ -d "/opt/git/sigmond/hf-timestd" ]]; then
    sudo rm -rf "/opt/git/sigmond/hf-timestd"
    log_info "  Removed: /opt/git/sigmond/hf-timestd"
fi

# Remove config directory
if [[ -d "$CONFIG_DIR" ]]; then
    sudo rm -rf "$CONFIG_DIR"
    log_info "  Removed: $CONFIG_DIR"
fi

# Remove log directory
if [[ -d "$LOG_DIR" ]]; then
    sudo rm -rf "$LOG_DIR"
    log_info "  Removed: $LOG_DIR"
fi

# =============================================================================
# Step 7: Remove Data Directories (Optional)
# =============================================================================
if [[ "$KEEP_DATA" == "false" ]]; then
    log_step "Removing data directories..."
    
    if [[ -d "$DATA_ROOT" ]]; then
        log_warn "  Removing $DATA_ROOT (this may take a while)..."
        sudo rm -rf "$DATA_ROOT"
        log_info "  ✅ Removed: $DATA_ROOT"
    else
        log_info "  ℹ️  Data directory not found: $DATA_ROOT"
    fi
else
    log_info "Keeping data directories (--keep-data specified)"
    log_info "  Data preserved in: $DATA_ROOT"
fi

# =============================================================================
# Step 8: Remove System User
# =============================================================================
log_step "Removing system user..."

if id -u timestd &>/dev/null; then
    # Remove user from chrony group first
    CHRONY_GROUP=""
    if getent group _chrony &>/dev/null; then
        CHRONY_GROUP="_chrony"
    elif getent group chrony &>/dev/null; then
        CHRONY_GROUP="chrony"
    fi
    
    if [[ -n "$CHRONY_GROUP" ]]; then
        sudo gpasswd -d timestd "$CHRONY_GROUP" 2>/dev/null || true
    fi
    
    # Remove user and group
    sudo userdel timestd 2>/dev/null || true
    sudo groupdel timestd 2>/dev/null || true
    log_info "  ✅ Removed system user and group: timestd"
else
    log_info "  ℹ️  System user 'timestd' not found"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=============================================="
echo "  Uninstallation Complete"
echo "=============================================="
log_info "hf-timestd has been removed from the system."

if [[ "$KEEP_DATA" == "true" ]]; then
    echo ""
    log_warn "Data directories were preserved:"
    log_warn "  $DATA_ROOT"
    log_warn "To remove data manually, run:"
    log_warn "  sudo rm -rf $DATA_ROOT"
fi

echo ""
log_info "To reinstall, run:"
log_info "  cd $PROJECT_DIR"
log_info "  sudo ./scripts/install.sh"

echo ""
