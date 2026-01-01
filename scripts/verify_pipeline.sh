#!/bin/bash
# =============================================================================
# Pipeline Verification Script
# =============================================================================
# Provides comprehensive overview of all pipeline stages and outputs.
# Complements individual health-check-*.sh scripts (used by systemd).
#
# Checks:
#   - Phase 0: Service status (production only)
#   - Phase 1: Digital RF (L0 raw IQ data)
#   - Phase 2: Analytics (L2 timing measurements - HDF5 + CSV)
#   - Phase 3: Fusion (L3 fused timing estimates)
#   - Chrony integration (production only)
#
# Usage:
#   ./verify_pipeline.sh              # Full verification
#   ./verify_pipeline.sh --quick      # Skip detailed file checks
# =============================================================================

set -euo pipefail

# Parse arguments
QUICK_MODE=false
if [[ "${1:-}" == "--quick" ]]; then
    QUICK_MODE=true
fi

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
WARN=0

check_pass() { echo -e "${GREEN}✅ PASS${NC} $*"; ((PASS++)); }
check_fail() { echo -e "${RED}❌ FAIL${NC} $*"; ((FAIL++)); }
check_warn() { echo -e "${YELLOW}⚠️  WARN${NC} $*"; ((WARN++)); }
section() { echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n${BLUE}$*${NC}\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# Detect mode
if [[ -d "/var/lib/timestd" ]]; then
    DATA_ROOT="/var/lib/timestd"
    MODE="production"
else
    DATA_ROOT="/tmp/timestd-test"
    MODE="test"
fi

echo "=============================================="
echo "  hf-timestd Pipeline Verification"
echo "=============================================="
echo "  Mode: $MODE"
echo "  Data: $DATA_ROOT"
echo "=============================================="

# =============================================================================
# Phase 0: Service Status (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    section "Phase 0: Service Status"
    
    SERVICES=(
        "timestd-core-recorder.service"
        "timestd-analytics.service"
        "timestd-fusion.service"
        "timestd-web-ui.service"
    )
    
    for service in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$service"; then
            check_pass "$service is running"
        else
            check_fail "$service is NOT running"
        fi
    done
    
    # Check VTEC service (optional)
    if systemctl list-unit-files | grep -q "timestd-vtec.service"; then
        if systemctl is-active --quiet "timestd-vtec.service"; then
            check_pass "timestd-vtec.service is running"
        else
            check_warn "timestd-vtec.service is NOT running (optional)"
        fi
    fi
fi

# =============================================================================
# Phase 1: Digital RF (L0 Raw IQ Data)
# =============================================================================
section "Phase 1: Digital RF (L0 Raw IQ)"

DRF_DIR="$DATA_ROOT/drf"
if [[ -d "$DRF_DIR" ]]; then
    check_pass "Digital RF directory exists: $DRF_DIR"
    
    # Check for recent HDF5 files (within last 5 minutes)
    RECENT_DRF=$(find "$DRF_DIR" -name "*.h5" -mmin -5 2>/dev/null | wc -l)
    if [[ $RECENT_DRF -gt 0 ]]; then
        check_pass "Found $RECENT_DRF recent DRF HDF5 files (last 5 min)"
        
        # Show sample
        echo "  Sample files:"
        find "$DRF_DIR" -name "*.h5" -mmin -5 2>/dev/null | head -3 | while read f; do
            SIZE=$(du -h "$f" | cut -f1)
            echo "    - $(basename $f) ($SIZE)"
        done
    else
        check_warn "No recent DRF HDF5 files (last 5 min) - recorder may not be running"
    fi
else
    check_fail "Digital RF directory not found: $DRF_DIR"
fi

# =============================================================================
# Phase 2: Analytics (L2 Timing Measurements)
# =============================================================================
section "Phase 2: Analytics (L2 Timing)"

PHASE2_DIR="$DATA_ROOT/phase2"
if [[ -d "$PHASE2_DIR" ]]; then
    check_pass "Phase 2 directory exists: $PHASE2_DIR"
    
    # Count channel directories
    CHANNELS=$(find "$PHASE2_DIR" -maxdepth 1 -type d -name "*_*" 2>/dev/null | wc -l)
    if [[ $CHANNELS -gt 0 ]]; then
        check_pass "Found $CHANNELS channel directories"
    else
        check_fail "No channel directories found in $PHASE2_DIR"
    fi
    
    # Check for HDF5 timing measurements
    echo ""
    echo "  Checking HDF5 outputs (primary)..."
    HDF5_COUNT=0
    for channel_dir in "$PHASE2_DIR"/*_*/; do
        if [[ -d "$channel_dir" ]]; then
            CHANNEL=$(basename "$channel_dir")
            HDF5_FILES=$(find "$channel_dir" -name "${CHANNEL}_timing_measurements_*.h5" -mmin -10 2>/dev/null)
            
            if [[ -n "$HDF5_FILES" ]]; then
                SIZE=$(du -h $HDF5_FILES 2>/dev/null | head -1 | cut -f1)
                check_pass "$CHANNEL: HDF5 timing measurements found ($SIZE)"
                ((HDF5_COUNT++))
            else
                check_warn "$CHANNEL: No recent HDF5 timing measurements"
            fi
        fi
    done
    
    if [[ $HDF5_COUNT -eq 0 ]]; then
        check_fail "No HDF5 timing measurements found across any channels"
    fi
    
    # Check for CSV files (backup)
    echo ""
    echo "  Checking CSV outputs (backup)..."
    CSV_COUNT=0
    for channel_dir in "$PHASE2_DIR"/*_*/; do
        if [[ -d "$channel_dir" ]]; then
            CHANNEL=$(basename "$channel_dir")
            CSV_FILE="$channel_dir/clock_offset/clock_offset_series.csv"
            
            if [[ -f "$CSV_FILE" ]]; then
                # Check if updated recently (last 5 min)
                if [[ $(find "$CSV_FILE" -mmin -5 2>/dev/null) ]]; then
                    LINES=$(wc -l < "$CSV_FILE" 2>/dev/null || echo 0)
                    check_pass "$CHANNEL: CSV updated recently ($LINES lines)"
                    ((CSV_COUNT++))
                else
                    check_warn "$CHANNEL: CSV exists but not recently updated"
                fi
            fi
        fi
    done
    
    # Check for other Phase 2 products
    echo ""
    echo "  Checking other Phase 2 products..."
    
    # BCD discrimination
    BCD_FILES=$(find "$PHASE2_DIR" -path "*/bcd_discrimination/*.h5" -mmin -10 2>/dev/null | wc -l)
    if [[ $BCD_FILES -gt 0 ]]; then
        check_pass "BCD discrimination: $BCD_FILES recent HDF5 files"
    else
        check_warn "BCD discrimination: No recent HDF5 files"
    fi
    
    # Tone detections
    TONE_FILES=$(find "$PHASE2_DIR" -path "*/tone_detections/*.h5" -mmin -10 2>/dev/null | wc -l)
    if [[ $TONE_FILES -gt 0 ]]; then
        check_pass "Tone detections: $TONE_FILES recent HDF5 files"
    else
        check_warn "Tone detections: No recent HDF5 files"
    fi
    
else
    check_fail "Phase 2 directory not found: $PHASE2_DIR"
fi

# =============================================================================
# Phase 3: Fusion (L3 Fused Timing)
# =============================================================================
section "Phase 3: Fusion (L3 Fused Timing)"

FUSION_DIR="$PHASE2_DIR/fusion"
if [[ -d "$FUSION_DIR" ]]; then
    check_pass "Fusion directory exists: $FUSION_DIR"
    
    # Check for fusion HDF5 output
    FUSION_HDF5=$(find "$FUSION_DIR" -name "fusion_timing_*.h5" -mmin -10 2>/dev/null)
    if [[ -n "$FUSION_HDF5" ]]; then
        SIZE=$(du -h $FUSION_HDF5 2>/dev/null | head -1 | cut -f1)
        RECORDS=$(h5dump -d timestamp_utc -y -w0 $FUSION_HDF5 2>/dev/null | grep -c ":" || echo "unknown")
        check_pass "Fusion HDF5 found: $(basename $FUSION_HDF5) ($SIZE, ~$RECORDS records)"
    else
        check_warn "No recent fusion HDF5 files (last 10 min)"
    fi
    
    # Check for fusion CSV (fallback)
    FUSION_CSV="$FUSION_DIR/fusion_timing.csv"
    if [[ -f "$FUSION_CSV" ]]; then
        if [[ $(find "$FUSION_CSV" -mmin -5 2>/dev/null) ]]; then
            LINES=$(wc -l < "$FUSION_CSV" 2>/dev/null || echo 0)
            check_pass "Fusion CSV updated recently ($LINES lines)"
        else
            check_warn "Fusion CSV exists but not recently updated"
        fi
    else
        check_warn "Fusion CSV not found: $FUSION_CSV"
    fi
    
else
    check_warn "Fusion directory not found: $FUSION_DIR"
fi

# =============================================================================
# Chrony Integration (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    section "Chrony Integration"
    
    if command -v chronyc &>/dev/null; then
        # Check if TMGR source exists
        if chronyc sources 2>/dev/null | grep -q "TMGR"; then
            check_pass "Chrony TMGR source configured"
            
            # Check reachability
            REACH=$(chronyc sources 2>/dev/null | grep "TMGR" | awk '{print $4}')
            if [[ "$REACH" != "0" ]]; then
                check_pass "TMGR source reachable (reach: $REACH)"
            else
                check_warn "TMGR source configured but not reachable (reach: 0)"
            fi
        else
            check_warn "Chrony TMGR source not configured"
        fi
    else
        check_warn "chronyd not installed"
    fi
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=============================================="
echo "  Verification Summary"
echo "=============================================="
echo -e "  ${GREEN}PASS: $PASS${NC}"
echo -e "  ${YELLOW}WARN: $WARN${NC}"
echo -e "  ${RED}FAIL: $FAIL${NC}"
echo "=============================================="

if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}✅ Pipeline verification completed successfully${NC}"
    exit 0
else
    echo -e "${RED}❌ Pipeline verification found $FAIL failures${NC}"
    exit 1
fi
