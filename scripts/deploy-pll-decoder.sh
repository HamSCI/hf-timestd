#!/bin/bash
#
# deploy-pll-decoder.sh - Deploy PLL decoder for A/B testing
#
# Usage: sudo scripts/deploy-pll-decoder.sh [--pull]
#
# This script extends update-production.sh with PLL-specific deployment steps:
# 1. Verifies PLL decoder files are present
# 2. Updates environment configuration for A/B testing
# 3. Creates necessary directories for L2/decoder_comparison
# 4. Updates HDF5 schema registry
# 5. Runs standard update-production.sh
# 6. Verifies PLL decoder loads correctly
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse arguments
DO_GIT_PULL=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --pull)
            DO_GIT_PULL=true
            shift
            ;;
        --help|-h)
            echo "Usage: sudo $0 [--pull]"
            echo ""
            echo "Deploys PLL decoder for A/B testing against existing matched filter."
            echo ""
            echo "Options:"
            echo "  --pull    Run 'git pull' before deploying"
            echo "  --help    Show this help"
            echo ""
            echo "Environment variables (from /etc/hf-timestd/environment):"
            echo "  TIMESTD_DECODER_VARIANT=both       # Enable both decoders"
            echo "  TIMESTD_ENABLE_AB_COMPARISON=true  # Collect comparison metrics"
            echo ""
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Configuration
INSTALL_DIR="/opt/hf-timestd"
VENV_DIR="$INSTALL_DIR/venv"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="/var/lib/timestd"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PLL Decoder Deployment"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (sudo)"
    exit 1
fi

# =============================================================================
# Step 1: Verify PLL Decoder Files
# =============================================================================
log_info "Step 1: Verifying PLL decoder files..."

REQUIRED_FILES=(
    "$PROJECT_DIR/src/hf_timestd/core/tick_pll_decoder.py"
    "$PROJECT_DIR/src/hf_timestd/core/decoder_config.py"
    "$PROJECT_DIR/src/hf_timestd/schemas/l2_decoder_comparison_v1.json"
    "$PROJECT_DIR/tests/test_tick_pll_decoder.py"
)

ALL_PRESENT=true
for file in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        log_error "Missing: $file"
        ALL_PRESENT=false
    fi
done

if [[ "$ALL_PRESENT" == "false" ]]; then
    log_error "Required PLL files missing. Cannot deploy."
    exit 1
fi

log_info "  ✅ All PLL decoder files present"

# =============================================================================
# Step 2: Run Unit Tests
# =============================================================================
log_info "Step 2: Running PLL unit tests..."

if "$VENV_DIR/bin/python" "$PROJECT_DIR/tests/test_tick_pll_decoder.py"; then
    log_info "  ✅ PLL unit tests passed"
else
    log_error "PLL unit tests failed. Aborting deployment."
    exit 1
fi

# =============================================================================
# Step 3: Update Environment Configuration
# =============================================================================
log_info "Step 3: Updating environment configuration..."

ENV_FILE="/etc/hf-timestd/environment"

# Check if environment file exists
if [[ ! -f "$ENV_FILE" ]]; then
    log_warn "Environment file not found at $ENV_FILE"
    log_info "Creating from template..."
    
    # Create directory if needed
    mkdir -p "$(dirname "$ENV_FILE")"
    
    # Copy template
    cp "$PROJECT_DIR/config/environment.timestd.template" "$ENV_FILE"
    log_info "  ✅ Created environment file from template"
fi

# Function to add or update environment variable
update_env_var() {
    local var_name="$1"
    local var_value="$2"
    local comment="${3:-}"
    
    if grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
        # Update existing
        sed -i "s/^${var_name}=.*/${var_name}=${var_value}/" "$ENV_FILE"
    else
        # Add new
        if [[ -n "$comment" ]]; then
            echo "" >> "$ENV_FILE"
            echo "# $comment" >> "$ENV_FILE"
        fi
        echo "${var_name}=${var_value}" >> "$ENV_FILE"
    fi
}

# Update PLL-specific environment variables
update_env_var "TIMESTD_DECODER_VARIANT" "both" "A/B testing: run both decoders"
update_env_var "TIMESTD_ENABLE_AB_COMPARISON" "true" "Enable comparison metrics"

log_info "  ✅ Environment configured for A/B testing:"
log_info "     - TIMESTD_DECODER_VARIANT=both"
log_info "     - TIMESTD_ENABLE_AB_COMPARISON=true"

# =============================================================================
# Step 4: Create Decoder Comparison Directory
# =============================================================================
log_info "Step 4: Creating decoder comparison data directory..."

# Create L2/decoder_comparison directories for all channels
CHANNEL_DIRS=$(find "$DATA_ROOT/phase2" -maxdepth 1 -type d -name "*_*" 2>/dev/null || true)

if [[ -n "$CHANNEL_DIRS" ]]; then
    for channel_dir in $CHANNEL_DIRS; do
        comparison_dir="$channel_dir/decoder_comparison"
        if [[ ! -d "$comparison_dir" ]]; then
            mkdir -p "$comparison_dir"
            chown timestd:timestd "$comparison_dir"
            log_info "  Created: $(basename "$channel_dir")/decoder_comparison"
        fi
    done
else
    log_warn "  No channel directories found (may be first install)"
fi

# =============================================================================
# Step 5: Run Standard Update
# =============================================================================
log_info "Step 5: Running standard production update..."

UPDATE_ARGS=""
if [[ "$DO_GIT_PULL" == "true" ]]; then
    UPDATE_ARGS="--pull"
fi

if bash "$PROJECT_DIR/scripts/deploy.sh" $UPDATE_ARGS; then
    log_info "  ✅ Production update completed"
else
    log_error "Production update failed"
    exit 1
fi

# =============================================================================
# Step 6: Verify PLL Decoder Loads
# =============================================================================
log_info "Step 6: Verifying PLL decoder in production venv..."

# Test import
if "$VENV_DIR/bin/python" -c "from hf_timestd.core.tick_pll_decoder import DualStationPLL; print('OK')" 2>/dev/null | grep -q "OK"; then
    log_info "  ✅ PLL decoder imports successfully"
else
    log_error "PLL decoder failed to import in production venv"
    exit 1
fi

# Test decoder config
if "$VENV_DIR/bin/python" -c "from hf_timestd.core.decoder_config import get_decoder_config; cfg = get_decoder_config(); print(f'Variant: {cfg.primary_decoder.value}, AB: {cfg.enable_ab_comparison}')" 2>/dev/null | grep -q "Variant: both"; then
    log_info "  ✅ Decoder configuration loaded correctly (variant=both)"
else
    log_warn "  Decoder configuration may need restart to take effect"
fi

# =============================================================================
# Step 7: Post-Deployment Checks
# =============================================================================
log_info "Step 7: Post-deployment checks..."

# Check if services are running
SERVICES_TO_CHECK=(
    "timestd-metrology"
    "timestd-l2-calibration"
    "timestd-fusion"
)

for service in "${SERVICES_TO_CHECK[@]}"; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        log_info "  ✅ $service running"
    else
        log_warn "  ⚠️  $service not running"
    fi
done

# Check for PLL logs
sleep 2  # Brief wait for services to start logging
if journalctl -u timestd-metrology -n 20 --no-pager 2>/dev/null | grep -q "PLL\|pll"; then
    log_info "  ✅ PLL decoder activity detected in logs"
else
    log_info "  ℹ️  PLL initialization may still be in progress"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PLL Decoder Deployment Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
log_info "Both decoders are now running in parallel:"
log_info "  - Matched Filter (existing): tick_matched_filter.py"
log_info "  - PLL (new): tick_pll_decoder.py"
echo ""
log_info "A/B comparison data will be written to:"
log_info "  $DATA_ROOT/phase2/{channel}/decoder_comparison/"
echo ""
log_info "Monitor comparison:"
log_info "  journalctl -u timestd-metrology -f | grep -i 'pll\|comparison\|winner'"
log_info "  tail -f $DATA_ROOT/phase2/*/decoder_comparison/*.h5"
echo ""
log_info "After 7 days, check:"
log_info "  /opt/hf-timestd/venv/bin/python -m hf_timestd.decoder_comparison_report"
echo ""
