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
MODE="test"
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
            echo "  --mode test|production  Installation mode (default: test)"
            echo "  --user <username>       User to run services as (default: current user)"
            echo "  --verbose, -v           Verbose output"
            echo "  --help, -h              Show this help"
            echo ""
            echo "Test Mode:"
            echo "  - Data stored in /tmp/timestd-test"
            echo "  - Manual startup via scripts/timestd-all.sh"
            echo "  - Ideal for development and testing"
            echo ""
            echo "Production Mode:"
            echo "  - Data stored in /var/lib/timestd"
            echo "  - Configuration in /etc/hf-timestd"
            echo "  - Systemd services for auto-start and recovery"
            echo "  - Web UI (FastAPI) on port 8080"
            echo "  - Daily upload timer enabled"
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
echo "  TimeStd Recorder Installation"
echo "=============================================="
echo "  Mode:    $MODE"
echo "  User:    $INSTALL_USER"
echo "  Project: $PROJECT_DIR"
echo "=============================================="
echo ""

# =============================================================================
# Step 1: Check Prerequisites
# =============================================================================
log_info "Checking prerequisites..."

# Check for required commands
MISSING_PREREQS=false

if command -v python3 > /dev/null; then
    log_info "  ✅ python3 found"
else
    log_warn "  ❌ python3 not found"
    MISSING_PREREQS=true
fi

if command -v pip3 > /dev/null; then
    log_info "  ✅ pip3 found"
else
    log_warn "  ❌ pip3 not found"
    MISSING_PREREQS=true
fi

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -ge 3 ] && [ "$PYTHON_MINOR" -ge 10 ]; then
    log_info "  ✅ Python $PYTHON_VERSION (>= 3.10 required)"
else
    log_error "  ❌ Python $PYTHON_VERSION found, but 3.10+ required"
    MISSING_PREREQS=true
fi

if [ "$MISSING_PREREQS" = true ]; then
    log_error "Prerequisites not met. Please install missing packages."
    echo ""
    echo "On Debian/Ubuntu, run:"
    echo "  sudo apt-get update"
    echo "  sudo apt-get install python3 python3-pip python3-venv python3-dev"
    echo "  sudo apt-get install avahi-utils hdf5-tools libhdf5-dev libsystemd-dev"
    echo ""
    exit 1
fi

# =============================================================================
# Step 1.5: Check System Dependencies (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    log_step "Checking system dependencies..."
    
    # Check for hdf5-tools (h5clear needed for robust recovery)
    if ! command -v h5clear &> /dev/null; then
        log_warn "  ⚠️  h5clear not found (required for HDF5 crash recovery)"
        read -p "Install hdf5-tools? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            log_info "  Installing hdf5-tools..."
            sudo apt-get update && sudo apt-get install -y hdf5-tools
        else
            log_warn "  Skipping hdf5-tools. Automatic HDF5 lock clearing will not work."
        fi
    else
        log_info "  ✅ h5clear found"
    fi

    # Check for chrony - REQUIRED for production (system clock discipline is core functionality)
    if ! command -v chronyd &> /dev/null; then
        log_warn "  ⚠️  chronyd not found"
        log_info "  Chrony is REQUIRED for production mode (system clock discipline)"
        log_info "  Installing chrony..."
        sudo apt-get update && sudo apt-get install -y chrony
        
        if ! command -v chronyd &> /dev/null; then
            log_error "Failed to install chrony. This is required for production mode."
            exit 1
        fi
        log_info "  ✅ chronyd installed successfully"
    else
        log_info "  ✅ chronyd found"
    fi
    
    # Configure chrony for timestd SHM integration
    if command -v chronyd &> /dev/null; then
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

# L1 Feed: Raw metrology fusion (fast, robust baseline)
# - Uses L1 metrology measurements (raw TOA)
# - Uncertainty: ±0.85ms (multi-broadcast fusion with outlier rejection)
# - Latency: ~75-135 seconds
# - Fallback feed if L2 pipeline fails
refclock SHM 0 refid TSL1 poll 4 precision 1e-3 offset 0.0 delay 0.1

# L2 Feed: Calibrated timing fusion (accurate, primary)
# - Uses L2 calibrated measurements (geometric + TEC + system corrections)
# - Uncertainty: ±0.3-1.0ms (ISO GUM uncertainty budget)
# - Latency: ~105-195 seconds
# - Primary feed for clock discipline
refclock SHM 1 refid TSL2 poll 4 precision 1e-4 offset 0.0 delay 0.1

# Chrony will automatically prefer TSL2 (lower uncertainty, better precision)
# TSL1 serves as backup if L2 calibration pipeline fails
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

if [[ "$MODE" == "production" ]]; then
    sudo mkdir -p "$(dirname "$VENV_DIR")"
    sudo python3 -m venv "$VENV_DIR"
    sudo chown -R "$INSTALL_USER:$INSTALL_USER" "$VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
fi

# Activate and install
source "$VENV_DIR/bin/activate"
pip install --upgrade pip

log_info "Installing hf-timestd package (and dependencies from pyproject.toml)..."

# CRITICAL: In production mode, use regular install (NOT editable)
# Editable installs create .pth files pointing to the source directory,
# which breaks when systemd runs as a different user or the source is removed
if [[ "$MODE" == "production" ]]; then
    # Copy source to temp location to avoid any path dependencies
    TEMP_INSTALL_DIR=$(mktemp -d)
    
    # Copy all files except legacy setup.py and requirements.txt (project uses pyproject.toml)
    rsync -a --exclude='setup.py' --exclude='requirements.txt' --exclude='requirements-dev.txt' \
          "$PROJECT_DIR/" "$TEMP_INSTALL_DIR/"
    
    # Install from temp location (ensures no references to $PROJECT_DIR)
    pip install "$TEMP_INSTALL_DIR"
    
    # Clean up
    rm -rf "$TEMP_INSTALL_DIR"
    
    log_info "  Installed hf-timestd in production mode (no source directory references)"
else
    # Test mode: use editable install for development convenience
    pip install -e .
    log_info "  Installed hf-timestd in editable mode (for development)"
fi

# Verify installation
python -c "import hf_timestd; print(f'  ✅ hf_timestd installed from: {hf_timestd.__file__}')"
python -c "import sysv_ipc; print(f'  ✅ sysv_ipc installed')"
python -c "import iri2020; print(f'  ✅ iri2020 installed')"

# Verify no repo path references in production
if [[ "$MODE" == "production" ]]; then
    if python -c "import sys; exit(1 if '$PROJECT_DIR' in str(sys.path) else 0)"; then
        log_info "  ✅ No source directory in Python path (production clean)"
    else
        log_warn "  ⚠️  Source directory still in Python path - may cause issues"
    fi
fi

deactivate


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
    
    # Create service files with correct paths
    SYSTEMD_DIR="/etc/systemd/system"
    
    # Core Recorder Service (Phase 1: RTP → Digital RF)
    sudo tee "$SYSTEMD_DIR/timestd-core-recorder.service" > /dev/null << EOF
[Unit]
Description=HF Time Standard Core Recorder - Phase 1 RTP to Raw Buffer Archive
Documentation=https://github.com/mijahauan/grape-recorder
After=network-online.target
Wants=network-online.target
# Wait for radiod if running on same machine
After=ka9q-radio.service
Wants=ka9q-radio.service

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
EnvironmentFile=$CONFIG_DIR/environment
WorkingDirectory=$DATA_ROOT

ExecStart=$VENV_DIR/bin/python -m hf_timestd.core.core_recorder_v2 --config $CONFIG_DIR/timestd-config.toml
    
# Wait up to 5 minutes for startup (health check waits for 1st minute of data)
TimeoutStartSec=300

# Health check: Verify data is being written
ExecStartPost=/opt/hf-timestd/scripts/health-check-recorder.sh

Restart=always
RestartSec=10
StartLimitInterval=300
StartLimitBurst=5

# Resource limits - prioritize for real-time recording
Nice=-5
MemoryMax=2G

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=timestd-core-recorder

[Install]
WantedBy=multi-user.target
EOF
    
    # Analytics Service (Phase 2: All 9 channels + fusion)
    # Uses timestd-analytics.sh which starts all channel analyzers
    sudo tee "$SYSTEMD_DIR/timestd-analytics.service" > /dev/null << EOF
[Unit]
Description=HF Time Standard Analytics Service - Phase 2 Timing Analysis
Documentation=https://github.com/mijahauan/grape-recorder
After=timestd-core-recorder.service
Wants=timestd-core-recorder.service

[Service]
Type=forking
User=$INSTALL_USER
Group=$INSTALL_USER
EnvironmentFile=$CONFIG_DIR/environment
WorkingDirectory=$DATA_ROOT

# Use the shell script that starts all 9 channel analyzers + fusion
ExecStart=/opt/hf-timestd/scripts/timestd-analytics.sh -start $CONFIG_DIR/timestd-config.toml
ExecStop=/opt/hf-timestd/scripts/timestd-analytics.sh -stop

# Type=forking since script backgrounds processes
RemainAfterExit=yes

Restart=on-failure
RestartSec=30
StartLimitInterval=300
StartLimitBurst=3

StandardOutput=journal
StandardError=journal
SyslogIdentifier=timestd-analytics

[Install]
WantedBy=multi-user.target
EOF

    # Fusion Service (Phase 3: Multi-Broadcast Fusion)
    sudo tee "$SYSTEMD_DIR/timestd-fusion.service" > /dev/null << EOF
[Unit]
Description=HF-Timestd Multi-Broadcast Fusion (Chrony Feed)
After=timestd-analytics.service
Requires=timestd-analytics.service

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
# Run as the installed module from the venv
ExecStart=$VENV_DIR/bin/python -m hf_timestd.core.multi_broadcast_fusion --data-root $DATA_ROOT --interval 15.0 --enable-chrony --log-level INFO
Restart=always
RestartSec=10
# Standard output logging
StandardOutput=append:$LOG_DIR/fusion.log
StandardError=append:$LOG_DIR/fusion.log

[Install]
WantedBy=multi-user.target
EOF

    # Web API Service (FastAPI)
    sudo tee "$SYSTEMD_DIR/timestd-web-api.service" > /dev/null << EOF
[Unit]
Description=HF-TimeStd Web API (FastAPI Monitoring Server)
Documentation=https://github.com/mijahauan/hf-timestd
After=network-online.target timestd-analytics.service
Wants=network-online.target

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
EnvironmentFile=$CONFIG_DIR/environment

# Environment
Environment="PYTHONUNBUFFERED=1"

WorkingDirectory=$WEBUI_DIR

# Run the startup script which sets up environment and launches uvicorn
ExecStart=$WEBUI_DIR/start.sh

Restart=always
RestartSec=10
StartLimitInterval=300
StartLimitBurst=5

# Resource limits
MemoryMax=512M

StandardOutput=journal
StandardError=journal
SyslogIdentifier=timestd-web-api

[Install]
WantedBy=multi-user.target
EOF

    # Physics Fusion Service (Phase 3: Science First)
    sudo tee "$SYSTEMD_DIR/timestd-physics.service" > /dev/null << EOF
[Unit]
Description=HF-TimeStd Physics-Based Fusion (Science First)
Documentation=https://github.com/mijahauan/hf-timestd
After=timestd-analytics.service
Requires=timestd-analytics.service
PartOf=timestd-analytics.service

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER

# Run as the installed module from the venv
ExecStart=$VENV_DIR/bin/python -m hf_timestd.core.physics_fusion_service \\
    --data-root $DATA_ROOT \\
    --output $DATA_ROOT/phase2/fusion

# Ensure correct permissions
ExecStartPre=+/usr/bin/chown -R $INSTALL_USER:$INSTALL_USER $DATA_ROOT/phase2/fusion

# Restart on failure
Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

# Standard output logging
StandardOutput=append:$LOG_DIR/physics.log
StandardError=append:$LOG_DIR/physics.log

[Install]
WantedBy=multi-user.target
EOF

    # GNSS VTEC Service (Optional - only if enabled in config)
    # Check if GNSS VTEC is enabled in the config
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
        log_info "  GNSS VTEC enabled in config - installing timestd-vtec.service..."
        
        sudo tee "$SYSTEMD_DIR/timestd-vtec.service" > /dev/null <<'VTEC_EOF'
[Unit]
Description=HF-TimeStd GNSS VTEC Monitor
Documentation=https://github.com/mijahauan/hf-timestd
After=network.target
Wants=network.target

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
EnvironmentFile=$CONFIG_DIR/environment
WorkingDirectory=$DATA_ROOT

ExecStart=$VENV_DIR/bin/python -u /opt/hf-timestd/scripts/live_vtec.py --config $CONFIG_DIR/timestd-config.toml

Restart=always
RestartSec=10
StartLimitInterval=300
StartLimitBurst=5

# Health check (verify data production)
ExecStartPost=/opt/hf-timestd/scripts/health-check-vtec.sh

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=timestd-vtec

[Install]
WantedBy=multi-user.target
VTEC_EOF
        
        # Substitute variables in the service file
        sudo sed -i "s|\$INSTALL_USER|$INSTALL_USER|g" "$SYSTEMD_DIR/timestd-vtec.service"
        sudo sed -i "s|\$CONFIG_DIR|$CONFIG_DIR|g" "$SYSTEMD_DIR/timestd-vtec.service"
        sudo sed -i "s|\$PROJECT_DIR|$PROJECT_DIR|g" "$SYSTEMD_DIR/timestd-vtec.service"
        sudo sed -i "s|\$VENV_DIR|$VENV_DIR|g" "$SYSTEMD_DIR/timestd-vtec.service"
        sudo sed -i "s|\$DATA_ROOT|$DATA_ROOT|g" "$SYSTEMD_DIR/timestd-vtec.service"

        
        log_info "    ✅ timestd-vtec.service installed"
    else
        log_info "  GNSS VTEC disabled in config - skipping timestd-vtec.service"
    fi



    # Radiod Monitor Service (Phase 0.5: Hardware Health)
    sudo tee "$SYSTEMD_DIR/timestd-radiod-monitor.service" > /dev/null << EOF
[Unit]
Description=HF Time Standard Radiod Health Monitor
Documentation=https://github.com/mijahauan/hf-timestd
After=network.target
Wants=network.target

[Service]
Type=simple
User=$INSTALL_USER
Group=$INSTALL_USER
EnvironmentFile=$CONFIG_DIR/environment
WorkingDirectory=$DATA_ROOT

# Run the radiod health monitor
ExecStart=$VENV_DIR/bin/python -u /opt/hf-timestd/scripts/monitor_radiod_health.py \\
    /var/lib/timestd/state/radiod-status.json \\
    10

Restart=always
RestartSec=10
    
# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=timestd-radiod-monitor

# Process limits
LimitNOFILE=65536
Nice=5

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd
    sudo systemctl daemon-reload
    
    log_info "  Installed systemd services:"
    log_info "    - timestd-core-recorder.service  (Phase 1: RTP → DRF, continuous)"
    log_info "    - timestd-analytics.service      (Phase 2: Timing analysis, continuous)"
    log_info "    - timestd-fusion.service         (Phase 3: Fusion & Chrony feed)"
    log_info "    - timestd-web-api.service        (Web monitoring API, continuous)"
    log_info "    - timestd-physics.service        (Phase 3: Physics fusion & Science)"
    log_info "    - timestd-radiod-monitor.service (Phase 0.5: Hardware monitor)"
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        log_info "    - timestd-vtec.service           (GNSS VTEC monitor, continuous)"
    fi
    
    # Enable services
    log_step "Enabling services for auto-start..."
    sudo systemctl enable timestd-core-recorder.service
    sudo systemctl enable timestd-analytics.service
    sudo systemctl enable timestd-fusion.service
    sudo systemctl enable timestd-web-api.service
    sudo systemctl enable timestd-physics.service
    sudo systemctl enable timestd-radiod-monitor.service
    
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        sudo systemctl enable timestd-vtec.service
        log_info "  ✅ timestd-vtec.service enabled"
    fi

    
    log_info "  Services enabled (will start on boot)"

    # Create logrotate config
    sudo tee "/etc/logrotate.d/grape-recorder" > /dev/null << EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 $INSTALL_USER $INSTALL_USER
}
EOF
    log_info "  Created logrotate configuration"
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
    echo "1. Edit configuration:"
    echo "   sudo nano $CONFIG_DIR/timestd-config.toml"
    echo ""
    echo "2. Set your station info (callsign, grid_square, lat/lon, etc.)"
    echo ""
    echo "3. Start continuous services:"
    echo "   sudo systemctl start timestd-core-recorder   # Phase 1: RTP → DRF"
    echo "   sudo systemctl start timestd-analytics       # Phase 2: Timing analysis"
    echo "   sudo systemctl start timestd-fusion          # Phase 3: Fusion service"
    echo "   sudo systemctl start timestd-web-api         # Web monitoring API"
    echo "   sudo systemctl start timestd-physics         # Physics fusion"
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        echo "   sudo systemctl start timestd-vtec            # GNSS VTEC monitor"
    fi
    echo ""
    echo "4. Start periodic timers:"

    echo ""
    echo "5. Check status:"
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        echo "   sudo systemctl status timestd-core-recorder timestd-analytics timestd-fusion timestd-web-api timestd-vtec"
    else
        echo "   sudo systemctl status timestd-core-recorder timestd-analytics timestd-fusion timestd-web-api"
    fi
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
    echo "2. Start all services:"
    echo "   $PROJECT_DIR/scripts/timestd-all.sh -start"
    echo ""
    echo "3. Check status:"
    echo "   $PROJECT_DIR/scripts/timestd-all.sh -status"
    echo ""
    echo "4. Stop services:"
    echo "   $PROJECT_DIR/scripts/timestd-all.sh -stop"
    echo ""
    echo "Web API: http://localhost:8000"
fi

echo ""
echo "Data location: $DATA_ROOT"
echo "=============================================="
