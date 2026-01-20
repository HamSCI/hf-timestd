#!/bin/bash
# =============================================================================
# TimeStd Recorder Uninstallation Script
# =============================================================================
# Usage: ./uninstall.sh [--mode test|production] [--keep-data]
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
MODE="production"
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
            MODE="$2"
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
            echo "  --mode test|production  Uninstall mode (default: production)"
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

# Validate mode
if [[ "$MODE" != "test" && "$MODE" != "production" ]]; then
    log_error "Invalid mode: $MODE (must be 'test' or 'production')"
    exit 1
fi

echo "=============================================="
echo "  TimeStd Recorder Uninstallation"
echo "=============================================="
echo "  Mode:      $MODE"
echo "  Keep data: $KEEP_DATA"
echo "=============================================="
echo ""

# Confirm uninstall
if [[ "$MODE" == "production" ]]; then
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
fi

# Set paths based on mode
if [[ "$MODE" == "production" ]]; then
    DATA_ROOT="/var/lib/timestd"
    CONFIG_DIR="/etc/hf-timestd"
    VENV_DIR="/opt/hf-timestd/venv"
    WEBUI_DIR="/opt/hf-timestd/web-ui"
    LOG_DIR="/var/log/hf-timestd"
    INSTALL_USER="timestd"
else
    DATA_ROOT="/tmp/timestd-test"
    CONFIG_DIR="$PROJECT_DIR/config"
    VENV_DIR="$PROJECT_DIR/venv"
    WEBUI_DIR="$PROJECT_DIR/web-ui"
    LOG_DIR="$DATA_ROOT/logs"
    INSTALL_USER="${USER}"
fi

# =============================================================================
# Step 1: Stop and Disable Services (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Stopping and disabling systemd services..."
    
    SERVICES=(
        "timestd-core-recorder.service"
        "timestd-metrology.service"
        "timestd-l2-calibration.service"
        "timestd-fusion.service"
        "timestd-physics.service"
        "timestd-web-api.service"
        "timestd-vtec.service"
        "timestd-radiod-monitor.service"
        "timestd-chrony-monitor.service"
        "timestd-ionex-download.service"
        "grape-daily.service"
        # Legacy services
        "timestd-analytics.service"
        "timestd-web-ui.service"
    )
    
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
fi

# =============================================================================
# Step 2: Remove Service Files (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Removing systemd service files..."
    
    SERVICE_FILES=(
        "/etc/systemd/system/timestd-core-recorder.service"
        "/etc/systemd/system/timestd-metrology.service"
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
fi

# =============================================================================
# Step 3: Remove Chrony Configuration (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Removing chrony configuration..."
    
    # Detect chrony config file
    CHRONY_CONF=""
    if [[ -f "/etc/chrony/chrony.conf" ]]; then
        CHRONY_CONF="/etc/chrony/chrony.conf"
    elif [[ -f "/etc/chrony.conf" ]]; then
        CHRONY_CONF="/etc/chrony.conf"
    fi
    
    if [[ -n "$CHRONY_CONF" ]]; then
        if grep -q "refclock SHM 0 refid TSL1" "$CHRONY_CONF" 2>/dev/null; then
            log_info "  Removing timestd SHM refclock from $CHRONY_CONF..."
            # Remove the dual refclock block added by install.sh
            sudo sed -i '/# HF Time Standard Dual Chrony Refclock Configuration/,/# TSL1 serves as backup/d' "$CHRONY_CONF"
            log_info "  ✅ Removed SHM refclock configuration"
            
            # Restart chronyd if running
            if systemctl is-active --quiet chronyd; then
                log_info "  Restarting chronyd..."
                sudo systemctl restart chronyd
            fi
        else
            log_info "  ℹ️  No timestd SHM configuration found in chrony.conf"
        fi
    fi
fi

# =============================================================================
# Step 4: Remove System Configurations (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
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
    
    # Remove shared memory directory
    if [[ -d "/dev/shm/timestd" ]]; then
        sudo rm -rf "/dev/shm/timestd"
        log_info "  Removed: /dev/shm/timestd"
    fi
fi

# =============================================================================
# Step 5: Remove Python Virtual Environment
# =============================================================================
log_step "Removing Python virtual environment..."

if [[ -d "$VENV_DIR" ]]; then
    if [[ "$MODE" == "production" ]]; then
        sudo rm -rf "$VENV_DIR"
    else
        rm -rf "$VENV_DIR"
    fi
    log_info "  Removed: $VENV_DIR"
else
    log_info "  ℹ️  Virtual environment not found: $VENV_DIR"
fi

# =============================================================================
# Step 6: Remove Installation Directories (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Removing installation directories..."
    
    # Remove /opt/hf-timestd (except venv which was already removed)
    if [[ -d "/opt/hf-timestd" ]]; then
        sudo rm -rf "/opt/hf-timestd"
        log_info "  Removed: /opt/hf-timestd"
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
fi

# =============================================================================
# Step 7: Remove Data Directories (Optional)
# =============================================================================
if [[ "$KEEP_DATA" == "false" ]]; then
    log_step "Removing data directories..."
    
    if [[ -d "$DATA_ROOT" ]]; then
        if [[ "$MODE" == "production" ]]; then
            log_warn "  Removing $DATA_ROOT (this may take a while)..."
            sudo rm -rf "$DATA_ROOT"
        else
            rm -rf "$DATA_ROOT"
        fi
        log_info "  ✅ Removed: $DATA_ROOT"
    else
        log_info "  ℹ️  Data directory not found: $DATA_ROOT"
    fi
else
    log_info "Keeping data directories (--keep-data specified)"
    log_info "  Data preserved in: $DATA_ROOT"
fi

# =============================================================================
# Step 8: Remove System User (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
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
        
        # Remove user
        sudo userdel timestd 2>/dev/null || true
        log_info "  ✅ Removed system user: timestd"
    else
        log_info "  ℹ️  System user 'timestd' not found"
    fi
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
    if [[ "$MODE" == "production" ]]; then
        log_warn "  sudo rm -rf $DATA_ROOT"
    else
        log_warn "  rm -rf $DATA_ROOT"
    fi
fi

if [[ "$MODE" == "production" ]]; then
    echo ""
    log_info "To reinstall, run:"
    log_info "  cd $PROJECT_DIR"
    log_info "  sudo ./scripts/install.sh --mode production"
fi

echo ""
