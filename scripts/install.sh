#!/bin/bash
# =============================================================================
# TimeStd Recorder Installation Script
# =============================================================================
# Usage: ./install.sh [--mode test|production] [--user <username>]
#
# This script:
#   1. Creates required directories
#   2. Sets up Python virtual environment
#   3. Installs systemd services (production mode)
#   4. Creates configuration from template
#   5. Validates prerequisites
# =============================================================================

set -euo pipefail

# Default values
MODE=""
INSTALL_USER="${USER}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VERBOSE=false

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
        --user)
            INSTALL_USER="$2"
            shift 2
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "TimeStd Recorder Installation Script"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --mode test|production  Installation mode (prompts if omitted)"
            echo "  --user <username>       User to run services as (default: current user)"
            echo "  --verbose, -v           Verbose output"
            echo "  --help, -h              Show this help"
            echo ""
            echo "Test Mode:"
            echo "  - Data stored in /tmp/timestd-test"
            echo "  - Manual startup via Python module commands"
            echo "  - Ideal for development and testing"
            echo ""
            echo "Production Mode:"
            echo "  - Data stored in /var/lib/timestd"
            echo "  - Configuration in /etc/hf-timestd"
            echo "  - Systemd services for auto-start and recovery"
            echo "  - Web API (FastAPI) on port 8000"
            echo "  - Periodic timers and cron jobs enabled"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Interactive mode selection if --mode not specified
if [[ -z "$MODE" ]]; then
    echo ""
    echo "=============================================="
    echo "  TimeStd Recorder Installation"
    echo "=============================================="
    echo ""
    echo "  Select installation mode:"
    echo ""
    echo "    1) test"
    echo "       - Data in /tmp/timestd-test"
    echo "       - Manual startup, no systemd services"
    echo "       - For development and testing"
    echo ""
    echo "    2) production"
    echo "       - Data in /var/lib/timestd"
    echo "       - Systemd services with auto-start"
    echo "       - Web API on port 8000"
    echo "       - Requires sudo"
    echo ""
    while true; do
        read -rp "  Enter choice [1/2]: " choice
        case "$choice" in
            1|test)       MODE="test"; break ;;
            2|production) MODE="production"; break ;;
            *)            echo "  Invalid choice. Enter 1 or 2." ;;
        esac
    done
    echo ""
fi

# Validate mode
if [[ "$MODE" != "test" && "$MODE" != "production" ]]; then
    log_error "Invalid mode: $MODE (must be 'test' or 'production')"
    exit 1
fi

echo "=============================================="
echo "  TimeStd Recorder Installation"
echo "=============================================="
echo "  Mode:    $MODE"
echo "  User:    $INSTALL_USER"
echo "  Project: $PROJECT_DIR"
echo "=============================================="
echo ""

# =============================================================================
# Step 1: Install apt Dependencies
# =============================================================================
log_step "Checking and installing apt dependencies..."

# Packages required to build/run hf-timestd:
#   python3-dev      - headers for sysv_ipc, digital_rf C extensions
#   python3-venv     - venv module (not always included with python3)
#   python3-pip      - pip bootstrap
#   git              - required to install iri2020 from GitHub
#   libhdf5-dev      - h5py and digital_rf build dependency
#   libsndfile1-dev  - soundfile (libsndfile) build dependency
#   libsystemd-dev   - systemd-python build dependency
#   pkg-config       - needed by systemd-python and others to find libs
#   rsync            - used by this script and update-production.sh
#   avahi-utils      - avahi-browse for mDNS/zeroconf (ka9q-python discovery)
#   hdf5-tools       - h5clear for HDF5 crash recovery
APT_PACKAGES=(
    python3
    python3-dev
    python3-venv
    python3-pip
    git
    libhdf5-dev
    libsndfile1-dev
    libsystemd-dev
    pkg-config
    rsync
    avahi-utils
    hdf5-tools
)

# Check which packages are missing
MISSING_APT=()
for pkg in "${APT_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        MISSING_APT+=("$pkg")
    fi
done

if [[ ${#MISSING_APT[@]} -gt 0 ]]; then
    log_info "  Missing apt packages: ${MISSING_APT[*]}"
    if [[ "$EUID" -eq 0 ]] || sudo -n true 2>/dev/null; then
        log_info "  Running: sudo apt-get install -y ${MISSING_APT[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y "${MISSING_APT[@]}"
        log_info "  ✅ apt packages installed"
    else
        log_error "Cannot install missing packages without sudo."
        log_error "Please run: sudo apt-get install -y ${MISSING_APT[*]}"
        exit 1
    fi
else
    log_info "  ✅ All apt dependencies already installed"
fi

# Verify Python version (must be 3.10+ — apt may provide an older version)
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -ge 3 && "$PYTHON_MINOR" -ge 10 ]]; then
    log_info "  ✅ Python $PYTHON_VERSION (>= 3.10 required)"
else
    log_error "  ❌ Python $PYTHON_VERSION found, but 3.10+ is required."
    log_error "     On older Debian/Ubuntu, install python3.11 or python3.12:"
    log_error "       sudo apt-get install python3.11 python3.11-venv python3.11-dev"
    log_error "     Then re-run this script."
    exit 1
fi

# =============================================================================
# Step 1.5: Check System Dependencies (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Checking system dependencies..."

    # chrony is REQUIRED for production mode (system clock discipline)
    # Install it now if missing (not in the base APT_PACKAGES list above since
    # it's production-only and may conflict with existing NTP setups in test mode)
    if ! command -v chronyd &> /dev/null && [[ ! -x /usr/sbin/chronyd ]]; then
        log_info "  Installing chrony (required for production mode)..."
        sudo apt-get install -y chrony
        if ! command -v chronyd &> /dev/null && [[ ! -x /usr/sbin/chronyd ]]; then
            log_error "Failed to install chrony. This is required for production mode."
            exit 1
        fi
        log_info "  ✅ chronyd installed"
    else
        log_info "  ✅ chronyd found"
    fi

    # Configure chrony for timestd SHM integration
    if command -v chronyd &> /dev/null || [[ -x /usr/sbin/chronyd ]]; then
        # Detect chrony config file location
        if [[ -f "/etc/chrony/chrony.conf" ]]; then
            CHRONY_CONF="/etc/chrony/chrony.conf"
        elif [[ -f "/etc/chrony.conf" ]]; then
            CHRONY_CONF="/etc/chrony.conf"
        else
            CHRONY_CONF=""
        fi

        if [[ -n "$CHRONY_CONF" ]]; then
            log_info "  Found chrony config: $CHRONY_CONF"
            if ! grep -q "refclock SHM 0 refid TSL1" "$CHRONY_CONF" 2>/dev/null; then
            log_info "  Adding timestd dual SHM refclocks to chrony.conf..."
            sudo tee -a "$CHRONY_CONF" > /dev/null <<'EOF'

# HF Time Standard Dual Chrony Refclock Configuration
# Add this to /etc/chrony/chrony.conf or include it via:
#   include /etc/hf-timestd/chrony-timestd-refclocks.conf

# L1 Feed: Raw metrology fusion (backup)
# - Uses L1 metrology measurements (raw TOA)
# - Measured uncertainty: ±0.5-1.0ms
# - Backup feed if L2 pipeline fails
refclock SHM 0 refid TSL1 poll 4 precision 1e-3 offset 0.0 delay 0.002

# L2 Feed: Calibrated timing fusion (primary HF source)
# - Uses L2 calibrated measurements (geometric + TEC + system corrections)
# - Measured uncertainty: ±0.1-0.2ms (verified from fusion logs)
# - 'trust' ensures it's always combined with other sources
# - If no GNSS timeserver, add 'prefer' to make TSL2 primary
refclock SHM 1 refid TSL2 poll 4 precision 1e-4 offset 0.0 delay 0.001 trust
EOF
            log_info "  ✅ Chrony configured for timestd dual SHM integration (TSL1=L1, TSL2=L2)"
            log_info "  📝 Note: timestd-fusion must start BEFORE chronyd to create SHM with correct permissions"
        else
            log_info "  ℹ️  Chrony already configured for timestd SHM"
        fi
        else
            log_warn "  ⚠️  Could not find chrony.conf (checked /etc/chrony/chrony.conf and /etc/chrony.conf)"
            log_warn "      Please manually add 'refclock SHM 0 refid TMGR ...' to your chrony configuration."
        fi
        
        # Install chronyd service override to ensure correct startup order
        log_info "  Installing chronyd service override for SHM ordering..."
        sudo mkdir -p /etc/systemd/system/chronyd.service.d
        sudo cp "$PROJECT_DIR/systemd/chronyd-timestd-shm.conf" /etc/systemd/system/chronyd.service.d/timestd-shm.conf
        sudo systemctl daemon-reload
        log_info "  ✅ Chronyd will start after timestd-fusion (ensures correct SHM permissions)"
        
        # Restart chronyd if it's running to apply configuration changes
        if systemctl is-active --quiet chronyd; then
            log_info "  Restarting chronyd to apply configuration changes..."
            sudo systemctl restart chronyd
            log_info "  ✅ Chronyd restarted"
        fi
    fi
    
    # Configure UDP receive buffers (CRITICAL for preventing packet loss)
    log_step "Configuring UDP receive buffers..."
    if [[ ! -f "/etc/sysctl.d/99-timestd.conf" ]]; then
        log_info "  Creating /etc/sysctl.d/99-timestd.conf..."
        sudo tee /etc/sysctl.d/99-timestd.conf > /dev/null <<'EOF'
# HF-TimeStd: Increase UDP receive buffers to prevent packet loss
# Radiod sends large RTP packets (up to 3.8KB at 24kHz sample rate)
# which can be fragmented across multiple IP packets
net.core.rmem_max = 16777216      # 16MB max
net.core.rmem_default = 8388608   # 8MB default
EOF
        sudo sysctl -p /etc/sysctl.d/99-timestd.conf > /dev/null
        log_info "  ✅ UDP buffers configured (16MB max, 8MB default)"
    else
        log_info "  ℹ️  UDP buffer config already exists"
    fi
fi

# =============================================================================
# Step 2: Set Paths Based on Mode
# =============================================================================
log_step "Setting up paths for $MODE mode..."

if [[ "$MODE" == "production" ]]; then
    DATA_ROOT="/var/lib/timestd"
    CONFIG_DIR="/etc/hf-timestd"
    VENV_DIR="/opt/hf-timestd/venv"
    WEBUI_DIR="/opt/hf-timestd/web-api"
    LOG_DIR="/var/log/hf-timestd"  # FHS standard: logs in /var/log/
else
    DATA_ROOT="/tmp/timestd-test"
    CONFIG_DIR="$PROJECT_DIR/config"
    VENV_DIR="$PROJECT_DIR/venv"
    WEBUI_DIR="$PROJECT_DIR/web-api"
    LOG_DIR="$DATA_ROOT/logs"  # Test mode: keep logs with data for simplicity
fi

log_info "  Data root: $DATA_ROOT"
log_info "  Config:    $CONFIG_DIR"
log_info "  Venv:      $VENV_DIR"
log_info "  Web UI:    $WEBUI_DIR"
log_info "  Logs:      $LOG_DIR"

# =============================================================================
# Step 2.5: Create Service User (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Creating timestd service user..."
    
    # Create timestd system user and group
    if ! id -u timestd &>/dev/null; then
        sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "HF Time Standard Service" timestd
        log_info "  ✅ Created system user: timestd"
    else
        log_info "  ℹ️  User timestd already exists"
    fi
    
    # Detect chrony group (distribution-specific)
    CHRONY_GROUP=""
    if getent group _chrony &>/dev/null; then
        CHRONY_GROUP="_chrony"  # Debian/Ubuntu
    elif getent group chrony &>/dev/null; then
        CHRONY_GROUP="chrony"   # RHEL/Fedora/Arch
    fi
    
    if [[ -n "$CHRONY_GROUP" ]]; then
        sudo usermod -a -G "$CHRONY_GROUP" timestd
        log_info "  ✅ Added timestd to $CHRONY_GROUP group (for chrony SHM access)"
    else
        log_warn "  ⚠️  Chrony group not found - chrony SHM integration may not work"
        log_warn "     Install chrony and run: sudo usermod -a -G <chrony-group> timestd"
    fi
    
    # Override INSTALL_USER for production mode
    INSTALL_USER="timestd"
    log_info "  📝 Services will run as: $INSTALL_USER"
fi

# =============================================================================
# Step 3: Create Directories
# =============================================================================
log_step "Creating directories..."

create_dir() {
    local dir="$1"
    local owner="${2:-$INSTALL_USER}"
    
    if [[ "$MODE" == "production" ]]; then
        sudo mkdir -p "$dir"
        sudo chown "$owner:$owner" "$dir"
    else
        mkdir -p "$dir"
    fi
    log_info "  Created: $dir"
}

# Data directories - THREE-PHASE ARCHITECTURE
create_dir "$DATA_ROOT"
create_dir "$DATA_ROOT/raw_buffer"    # Phase 1: Immutable binary IQ archive
create_dir "$DATA_ROOT/phase2"        # Phase 2: Analytical engine outputs
create_dir "$DATA_ROOT/products"      # Phase 3: Derived products (decimated, spectrograms)
create_dir "$DATA_ROOT/state"         # Global state files
create_dir "$DATA_ROOT/status"        # System status files
create_dir "$DATA_ROOT/drf"           # Digital RF (L0) output
create_dir "$DATA_ROOT/grape"         # GRAPE format exports
create_dir "$DATA_ROOT/upload"        # Upload queue for GRAPE/external
create_dir "$DATA_ROOT/audio_buffers" # Audio buffer scratch space
create_dir "$DATA_ROOT/raw_archive"   # Long-term raw archive
create_dir "$DATA_ROOT/processed"     # Processed data products
create_dir "$DATA_ROOT/data"          # General data directory
create_dir "$DATA_ROOT/space_weather_cache" # Cached space weather indices
create_dir "$LOG_DIR"

# Shared memory directory for hot buffer (tiered storage)
if [[ "$MODE" == "production" ]]; then
    sudo mkdir -p /dev/shm/timestd
    sudo chown "$INSTALL_USER:$INSTALL_USER" /dev/shm/timestd
    log_info "  Created: /dev/shm/timestd (hot buffer)"
    
    # Install tmpfiles.d configuration to recreate on boot
    sudo cp "$PROJECT_DIR/systemd/timestd-tmpfiles.conf" /etc/tmpfiles.d/timestd.conf
    log_info "  Installed: /etc/tmpfiles.d/timestd.conf (ensures /dev/shm/timestd persists across reboots)"
fi

# Config directory
create_dir "$CONFIG_DIR"

if [[ "$MODE" == "production" ]]; then
    create_dir "/opt/hf-timestd"
fi

# =============================================================================
# Step 4: Create Python Virtual Environment
# =============================================================================
log_step "Setting up Python virtual environment..."

if [[ ! -x "$PROJECT_DIR/scripts/ensure-venv.sh" ]]; then
    log_error "Missing venv bootstrap script: $PROJECT_DIR/scripts/ensure-venv.sh"
    exit 1
fi

if [[ "$MODE" == "production" ]]; then
    sudo bash "$PROJECT_DIR/scripts/ensure-venv.sh" --mode production --venv "$VENV_DIR" --python python3
else
    bash "$PROJECT_DIR/scripts/ensure-venv.sh" --mode test --venv "$VENV_DIR" --python python3
fi

# Verify installation (using venv python)
"$VENV_DIR/bin/python" -c "import hf_timestd; print(f'  ✅ hf_timestd installed from: {hf_timestd.__file__}')"
"$VENV_DIR/bin/python" -c "import sysv_ipc; print(f'  ✅ sysv_ipc installed')"
"$VENV_DIR/bin/python" -c "import iri2020; print(f'  ✅ iri2020 installed')"

# Verify no repo path references in production
if [[ "$MODE" == "production" ]]; then
    if "$VENV_DIR/bin/python" -c "import sys; exit(1 if '$PROJECT_DIR' in str(sys.path) else 0)"; then
        log_info "  ✅ No source directory in Python path (production clean)"
    else
        log_warn "  ⚠️  Source directory still in Python path - may cause issues"
    fi
fi


# =============================================================================
# Step 5: Set up Web API and Scripts (Python FastAPI)
# =============================================================================
# Web API is now Python-based (FastAPI) - all dependencies installed via pip above
# Copy web-api and scripts directories to production location
if [[ "$MODE" == "production" ]]; then
    sudo mkdir -p "$WEBUI_DIR"
    sudo cp -r "$PROJECT_DIR/web-api/"* "$WEBUI_DIR/"
    sudo chown -R "$INSTALL_USER:$INSTALL_USER" "$WEBUI_DIR"
    log_info "Web API installed at $WEBUI_DIR (Python FastAPI)"
    
    # Copy scripts directory for service startup scripts
    sudo mkdir -p /opt/hf-timestd/scripts
    sudo cp -r "$PROJECT_DIR/scripts/"* /opt/hf-timestd/scripts/
    sudo chown -R "$INSTALL_USER:$INSTALL_USER" /opt/hf-timestd/scripts
    log_info "Scripts installed at /opt/hf-timestd/scripts"
    
    # Create config symlink for web-api (expects /opt/hf-timestd/config/)
    sudo mkdir -p /opt/hf-timestd/config
    sudo ln -sf /etc/hf-timestd/timestd-config.toml /opt/hf-timestd/config/timestd-config.toml
    log_info "Config symlink created: /opt/hf-timestd/config -> /etc/hf-timestd"
else
    log_info "Web API will run from $PROJECT_DIR/web-api (Python FastAPI)"
    log_info "Scripts will run from $PROJECT_DIR/scripts"
fi

# =============================================================================
# Step 6: Create Configuration Files
# =============================================================================
log_step "Creating configuration files..."

# Environment file
ENV_FILE="$CONFIG_DIR/environment"

generate_env_block() {
    cat << EOF
# HF Time Standard Environment
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)

TIMESTD_MODE=$1
TIMESTD_DATA_ROOT=$DATA_ROOT
TIMESTD_LOG_DIR=$LOG_DIR
TIMESTD_CONFIG=$CONFIG_DIR/timestd-config.toml
TIMESTD_PROJECT=$PROJECT_DIR
TIMESTD_INSTALL_DIR=$PROJECT_DIR
TIMESTD_WEBUI=$WEBUI_DIR
TIMESTD_VENV=$VENV_DIR
TIMESTD_LOG_LEVEL=$2
EOF
}

if [[ "$MODE" == "production" ]]; then
    generate_env_block "production" "INFO" | sudo tee "$ENV_FILE" > /dev/null
    sudo chown "$INSTALL_USER:$INSTALL_USER" "$ENV_FILE"
else
    generate_env_block "test" "DEBUG" > "$ENV_FILE"
fi

log_info "  Created: $ENV_FILE"

# Copy/update main config if not exists
MAIN_CONFIG="$CONFIG_DIR/timestd-config.toml"
if [[ ! -f "$MAIN_CONFIG" ]]; then
    if [[ "$MODE" == "production" ]]; then
        sudo cp "$PROJECT_DIR/config/timestd-config.toml" "$MAIN_CONFIG"
        # Update mode in config
        sudo sed -i 's/mode = "test"/mode = "production"/' "$MAIN_CONFIG"
        sudo sed -i "s|test_data_root = .*|test_data_root = \"/tmp/timestd-test\"|" "$MAIN_CONFIG"
        sudo sed -i "s|production_data_root = .*|production_data_root = \"$DATA_ROOT\"|" "$MAIN_CONFIG"
        sudo chown "$INSTALL_USER:$INSTALL_USER" "$MAIN_CONFIG"
    else
        cp "$PROJECT_DIR/config/timestd-config.toml" "$MAIN_CONFIG" 2>/dev/null || true
    fi
    log_info "  Created: $MAIN_CONFIG"
else
    log_info "  Config exists: $MAIN_CONFIG (not overwriting)"
fi

# =============================================================================
# Step 7: Install Systemd Services (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Installing systemd services..."
    
    SYSTEMD_DIR="/etc/systemd/system"
    
    # Copy pre-tested service files from repository
    # These files have watchdogs, proper dependencies, and security hardening
    log_info "  Copying service files from $PROJECT_DIR/systemd/..."
    
    # Core services (always installed)
    CORE_SERVICES=(
        "timestd-core-recorder"
        "timestd-metrology"
        "timestd-l2-calibration"
        "timestd-fusion"
        "timestd-physics"
        "timestd-web-api"
        "timestd-radiod-monitor"
    )
    
    for svc in "${CORE_SERVICES[@]}"; do
        sudo cp "$PROJECT_DIR/systemd/${svc}.service" "$SYSTEMD_DIR/"
        log_info "    ✅ ${svc}.service"
    done
    
    # Copy timer files and optional services
    TIMER_FILES=(
        "timestd-ionex-download.service"
        "timestd-ionex-download.timer"
        "timestd-chrony-monitor.service"
        "timestd-chrony-monitor.timer"
        "timestd-iono-reanalysis.service"
        "timestd-iono-reanalysis.timer"
        "grape-daily.service"
        "grape-daily.timer"
    )
    
    for timer_file in "${TIMER_FILES[@]}"; do
        if [[ -f "$PROJECT_DIR/systemd/$timer_file" ]]; then
            sudo cp "$PROJECT_DIR/systemd/$timer_file" "$SYSTEMD_DIR/"
            log_info "    ✅ $timer_file"
        fi
    done
    
    # Copy alert template service
    if [[ -f "$PROJECT_DIR/systemd/timestd-alert@.service" ]]; then
        sudo cp "$PROJECT_DIR/systemd/timestd-alert@.service" "$SYSTEMD_DIR/"
        log_info "    ✅ timestd-alert@.service"
    fi
    
    # GNSS VTEC Service (Optional - only if enabled in config)
    VTEC_ENABLED=$($VENV_DIR/bin/python3 -c "
import tomllib
try:
    with open('$MAIN_CONFIG', 'rb') as f:
        config = tomllib.load(f)
    print('true' if config.get('gnss_vtec', {}).get('enabled', False) else 'false')
except:
    print('false')
" 2>/dev/null)

    if [[ "$VTEC_ENABLED" == "true" ]]; then
        sudo cp "$PROJECT_DIR/systemd/timestd-vtec.service" "$SYSTEMD_DIR/"
        log_info "    ✅ timestd-vtec.service (GNSS VTEC enabled in config)"
        
        # Add GNSS timeserver to chrony if gnss_vtec is enabled
        # The ZED-F9P host typically also provides NTP service
        GNSS_HOST=$($VENV_DIR/bin/python3 -c "
import tomllib
try:
    with open('$MAIN_CONFIG', 'rb') as f:
        config = tomllib.load(f)
    print(config.get('gnss_vtec', {}).get('host', ''))
except:
    print('')
" 2>/dev/null)
        
        if [[ -n "$GNSS_HOST" && -n "$CHRONY_CONF" ]]; then
            if ! grep -q "server $GNSS_HOST" "$CHRONY_CONF" 2>/dev/null; then
                log_info "  Adding GNSS timeserver ($GNSS_HOST) to chrony.conf..."
                sudo tee -a "$CHRONY_CONF" > /dev/null <<EOF

# GNSS Timeserver (ZED-F9P host providing NTP)
# Added by hf-timestd install when gnss_vtec is enabled
server $GNSS_HOST iburst prefer
EOF
                log_info "  ✅ Added GNSS timeserver $GNSS_HOST to chrony"
            else
                log_info "  ℹ️  GNSS timeserver $GNSS_HOST already in chrony.conf"
            fi
        fi
    else
        log_info "    ℹ️  timestd-vtec.service skipped (GNSS VTEC disabled in config)"
        
        # No GNSS timeserver - make TSL2 the preferred source
        if [[ -n "$CHRONY_CONF" ]] && grep -q "refclock SHM 1 refid TSL2.*trust$" "$CHRONY_CONF" 2>/dev/null; then
            log_info "  Adding 'prefer' to TSL2 (no GNSS timeserver available)..."
            sudo sed -i 's/refclock SHM 1 refid TSL2\(.*\) trust$/refclock SHM 1 refid TSL2\1 trust prefer/' "$CHRONY_CONF"
            log_info "  ✅ TSL2 is now the preferred time source"
        fi
    fi

    # Reload systemd
    sudo systemctl daemon-reload
    
    log_info "  Installed systemd services:"
    log_info "    - timestd-core-recorder.service  (Phase 1: RTP → Raw Buffer)"
    log_info "    - timestd-metrology.service      (Phase 2: L1 Raw Measurements)"
    log_info "    - timestd-l2-calibration.service (Phase 2: L2 Calibrated Timing)"
    log_info "    - timestd-fusion.service         (Phase 3: Fusion → Chrony SHM)"
    log_info "    - timestd-physics.service        (Phase 3: TEC Estimation)"
    log_info "    - timestd-web-api.service        (Web API & Dashboard)"
    log_info "    - timestd-radiod-monitor.service (Hardware Health Monitor)"
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        log_info "    - timestd-vtec.service           (GNSS VTEC Monitor)"
    fi
    
    # Enable core services
    log_step "Enabling services for auto-start..."
    sudo systemctl enable timestd-core-recorder.service
    sudo systemctl enable timestd-metrology.service
    sudo systemctl enable timestd-l2-calibration.service
    sudo systemctl enable timestd-fusion.service
    sudo systemctl enable timestd-physics.service
    sudo systemctl enable timestd-web-api.service
    sudo systemctl enable timestd-radiod-monitor.service
    
    # Enable optional services/timers
    sudo systemctl enable timestd-ionex-download.timer
    sudo systemctl enable timestd-chrony-monitor.timer
    
    # Enable iono-reanalysis timer if service file exists
    if [[ -f "$SYSTEMD_DIR/timestd-iono-reanalysis.timer" ]]; then
        sudo systemctl enable timestd-iono-reanalysis.timer
        log_info "  ✅ timestd-iono-reanalysis.timer enabled (hourly)"
    fi
    
    # Enable grape-daily timer if service file exists
    if [[ -f "$SYSTEMD_DIR/grape-daily.timer" ]]; then
        sudo systemctl enable grape-daily.timer
        log_info "  ✅ grape-daily.timer enabled"
    fi
    
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        sudo systemctl enable timestd-vtec.service
        log_info "  ✅ timestd-vtec.service enabled"
    fi
    
    log_info "  Services enabled (will start on boot)"

    # Create logrotate config
    sudo tee "/etc/logrotate.d/hf-timestd" > /dev/null << EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
    log_info "  Created logrotate configuration"
    
    # Install freshness monitor cron job
    if [[ -f "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" ]]; then
        sudo cp "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" /etc/cron.d/timestd-freshness-monitor
        sudo chmod 644 /etc/cron.d/timestd-freshness-monitor
        log_info "  ✅ Installed freshness monitor cron job (/etc/cron.d/timestd-freshness-monitor)"
    fi
    
    # =============================================================================
    # Initial IONEX Download
    # =============================================================================
    log_info "Downloading initial IONEX data..."
    
    # Create IONEX directory
    sudo mkdir -p /var/lib/timestd/ionex
    sudo chown timestd:timestd /var/lib/timestd/ionex
    
    # Run initial IONEX download (yesterday's data)
    if [[ -f "$PROJECT_DIR/scripts/download_ionex_daily.sh" ]]; then
        sudo -u timestd "$PROJECT_DIR/scripts/download_ionex_daily.sh" 2>&1 | head -20 || true
        if ls /var/lib/timestd/ionex/*.gz 2>/dev/null | head -1 > /dev/null; then
            log_info "  ✅ Initial IONEX data downloaded"
        else
            log_warn "  ⚠️  IONEX download may have failed (check ~/.netrc for NASA CDDIS credentials)"
            log_info "     See: https://cddis.nasa.gov/Data_and_Derived_Products/CreateNetrcFile.html"
        fi
    else
        log_warn "  ⚠️  IONEX download script not found at $PROJECT_DIR/scripts/download_ionex_daily.sh"
    fi
    
    # =============================================================================
    # SHM Permissions Setup
    # =============================================================================
    log_info "Setting up Chrony SHM permissions..."
    
    # Remove any stale SHM segments that might have wrong permissions
    for key in 0x4e545030 0x4e545031; do
        shmid=$(ipcs -m | grep "$key" | awk '{print $2}')
        if [[ -n "$shmid" ]]; then
            ipcrm -m "$shmid" 2>/dev/null || true
        fi
    done
    log_info "  ✅ Cleared stale SHM segments (fusion will recreate with correct permissions)"
fi

# =============================================================================
# Step 7.5: Interactive Configuration
# =============================================================================
HAS_CONFIGURED=false
if [[ "$MODE" == "production" ]]; then
    echo ""
    read -rp "Would you like to configure your station settings now? [Y/n] " config_choice
    config_choice=${config_choice:-Y}
    if [[ "$config_choice" =~ ^[Yy]$ ]]; then
        HAS_CONFIGURED=true
        log_step "Opening configuration file..."
        sudo ${EDITOR:-nano} "$CONFIG_DIR/timestd-config.toml"
        
        # Parse for STATION_ID
        STATION_ID=$(grep -E '^\s*id\s*=' "$CONFIG_DIR/timestd-config.toml" | head -1 | sed 's/.*=\s*"\(.*\)".*/\1/')
        if [[ -n "$STATION_ID" && "$STATION_ID" != "<YOUR_STATION_ID>" && "$STATION_ID" != "<PSWS_STATION_ID>" ]]; then
            echo ""
            read -rp "Station ID '${STATION_ID}' detected. Would you like to set up the PSWS secure upload keys now? [Y/n] " key_choice
            key_choice=${key_choice:-Y}
            if [[ "$key_choice" =~ ^[Yy]$ ]]; then
                log_step "Executing PSWS key setup..."
                sudo "$PROJECT_DIR/scripts/setup-psws-keys.sh"
            fi
        fi
    fi
fi

# =============================================================================
# Step 8: Summary
# =============================================================================
echo ""
echo "=============================================="
echo "  Installation Complete!"
echo "=============================================="
echo ""

if [[ "$MODE" == "production" ]]; then
    echo "Production mode installed. Next steps:"
    echo ""
    if [[ "$HAS_CONFIGURED" == "false" ]]; then
        echo "1. Edit configuration:"
        echo "   sudo nano $CONFIG_DIR/timestd-config.toml"
        echo ""
        echo "2. Set your station info (callsign, grid_square, lat/lon, etc.)"
        echo ""
    fi
    echo "3. Start continuous services (in order):"
    echo "   sudo systemctl start timestd-core-recorder   # Phase 1: RTP → Raw Buffer"
    echo "   sudo systemctl start timestd-metrology       # Phase 2: L1 Raw Measurements"
    echo "   sudo systemctl start timestd-l2-calibration  # Phase 2: L2 Calibrated Timing"
    echo "   sudo systemctl start timestd-fusion          # Phase 3: Fusion → Chrony SHM"
    echo "   sudo systemctl start timestd-physics         # Phase 3: TEC Estimation"
    echo "   sudo systemctl start timestd-web-api         # Web API & Dashboard"
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        echo "   sudo systemctl start timestd-vtec            # GNSS VTEC Monitor"
    fi
    echo ""
    echo "4. Start periodic timers:"
    echo "   sudo systemctl start timestd-ionex-download.timer  # Daily IONEX maps"
    echo "   sudo systemctl start timestd-chrony-monitor.timer  # Chrony health check"
    echo ""
    echo "5. Check status:"
    echo "   sudo systemctl status timestd-core-recorder timestd-metrology timestd-l2-calibration timestd-fusion timestd-physics timestd-web-api"
    echo "   sudo systemctl list-timers timestd-*"
    echo "   journalctl -u timestd-core-recorder -f"
    echo ""
    
    # Add chrony note if it wasn't installed during setup
    if ! command -v chronyd &> /dev/null; then
        echo "📝 Note: If you install chrony later for system clock discipline:"
        echo "   sudo mkdir -p /etc/systemd/system/chronyd.service.d"
        echo "   sudo cp $PROJECT_DIR/systemd/chronyd-timestd-shm.conf /etc/systemd/system/chronyd.service.d/timestd-shm.conf"
        echo "   sudo systemctl daemon-reload"
        echo ""
    fi
    
    echo "Web API: http://localhost:8000"
else
    echo "Test mode installed. Next steps:"
    echo ""
    echo "1. Edit configuration:"
    echo "   nano $CONFIG_DIR/timestd-config.toml"
    echo ""
    echo "2. Activate virtual environment:"
    echo "   source $VENV_DIR/bin/activate"
    echo ""
    echo "3. Start core recorder:"
    echo "   python -m hf_timestd --config $CONFIG_DIR/timestd-config.toml"
    echo ""
    echo "4. In another terminal, start web API:"
    echo "   cd $PROJECT_DIR/web-api && $VENV_DIR/bin/python main.py"
    echo ""
    echo "Web API: http://localhost:8000"
fi

echo ""
echo "Data location: $DATA_ROOT"
echo "=============================================="
