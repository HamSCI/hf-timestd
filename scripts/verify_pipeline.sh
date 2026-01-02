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

set -uo pipefail

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
        "timestd-science-aggregator.service"
        "timestd-vtec.service"
        "timestd-radiod-monitor.service"
        "timestd-web-ui.service"
    )
    
    # Threshold for "recently restarted" warning (5 minutes)
    UPTIME_WARN_SEC=300
    NOW=$(date +%s)
    
    for service in "${SERVICES[@]}"; do
        # Check if service is active or activating
        if systemctl is-active --quiet "$service" || systemctl show "$service" -p ActiveState | grep -q "activating"; then
            STATE=$(systemctl show "$service" -p ActiveState -p SubState --value | head -1)
            SUBSTATE=$(systemctl show "$service" -p SubState --value)
            
            if [[ "$STATE" == "activating" ]]; then
                check_warn "$service is starting ($SUBSTATE)"
            else
                # Get start time and calculate uptime
                START_TIMESTAMP=$(systemctl show "$service" -p ActiveEnterTimestamp --value)
                
                # Handle potential empty timestamp
                if [[ -n "$START_TIMESTAMP" ]]; then
                    START_EPOCH=$(date -d "$START_TIMESTAMP" +%s 2>/dev/null || echo "0")
                    UPTIME=$((NOW - START_EPOCH))
                    
                    # Format uptime for display
                    if [[ $UPTIME -lt 60 ]]; then
                        UPTIME_STR="${UPTIME}s"
                    elif [[ $UPTIME -lt 3600 ]]; then
                        UPTIME_STR="$((UPTIME/60))m"
                    elif [[ $UPTIME -lt 86400 ]]; then
                        UPTIME_STR="$((UPTIME/3600))h $(( (UPTIME%3600)/60 ))m"
                    else
                        DAYS=$((UPTIME/86400))
                        UPTIME_STR="${DAYS}d $(( (UPTIME%86400)/3600 ))h"
                    fi
                    
                    if [[ $UPTIME -lt $UPTIME_WARN_SEC ]]; then
                        check_warn "$service is running (UPTIME: $UPTIME_STR < 5m) - Recent Restart!"
                    else
                        check_pass "$service is running (uptime: $UPTIME_STR)"
                    fi
                else
                    check_pass "$service is running (unknown uptime)"
                fi
            fi
        else
            # Special case for VTEC which might be optional/disabled
            if [[ "$service" == "timestd-vtec.service" ]]; then
                ENABLE_STATE=$(systemctl show "$service" -p UnitFileState --value)
                if [[ "$ENABLE_STATE" == "disabled" || "$ENABLE_STATE" == "masked" ]]; then
                     check_warn "$service is NOT running (disabled/optional)"
                else
                     check_fail "$service is NOT running"
                fi
            else
                check_fail "$service is NOT running"
            fi
        fi
    done
    
    echo ""
    echo "Note: Continuing to check data outputs even if services are starting..."
fi

# =============================================================================
# Phase 0.5: Radio Hardware (Radiod)
# =============================================================================
section "Phase 0.5: Radio Hardware (Radiod)"

RADIOD_STATUS_FILE="${DATA_ROOT}/state/radiod-status.json"

if [[ -f "$RADIOD_STATUS_FILE" ]]; then
    # Parse status using jq if available, otherwise grep
    if command -v jq &>/dev/null; then
        RADIOD_HEALTH=$(jq -r '.health' "$RADIOD_STATUS_FILE" 2>/dev/null)
        RADIOD_UPTIME=$(jq -r '.uptime_seconds' "$RADIOD_STATUS_FILE" 2>/dev/null)
        RX_COUNT=$(jq -r '.process.count' "$RADIOD_STATUS_FILE" 2>/dev/null)
        
        if [[ "$RADIOD_HEALTH" == "healthy" ]]; then
            check_pass "Radiod is HEALTHY (pid $RX_COUNT, uptime ${RADIOD_UPTIME}s)"
        elif [[ "$RADIOD_HEALTH" == "degraded" ]]; then
            check_warn "Radiod is DEGRADED (running but issues detected)"
        else
            check_fail "Radiod is UNHEALTHY/CRITICAL"
        fi
    else
        # Fallback if jq missing
        check_pass "Radiod status file exists (install jq for details)"
    fi
else
    check_warn "Radiod status file not found: $RADIOD_STATUS_FILE"
fi

# =============================================================================
# Phase 1: Binary Archive (L0 Raw IQ Data)
# =============================================================================
section "Phase 1: Binary Archive (L0 Raw IQ)"

RAW_BUFFER_DIR="$DATA_ROOT/raw_buffer"
if [[ -d "$RAW_BUFFER_DIR" ]]; then
    check_pass "Binary archive directory exists: $RAW_BUFFER_DIR"
    
    # Check for recent .bin.zst files (within last 5 minutes)
    # Check both cold storage and hot buffer (tiered storage)
    SEARCH_PATHS="$RAW_BUFFER_DIR"
    if [[ -d "/dev/shm/timestd/raw_buffer" ]]; then
        SEARCH_PATHS="$SEARCH_PATHS /dev/shm/timestd/raw_buffer"
        check_pass "Hot buffer (tiered storage) exists: /dev/shm/timestd/raw_buffer"
    fi
    
    RECENT_BIN=$(find $SEARCH_PATHS -name "*.bin.zst" -mmin -5 2>/dev/null | wc -l)
    
    if [[ $RECENT_BIN -gt 0 ]]; then
        check_pass "Found $RECENT_BIN recent binary archive files (last 5 min)"
    else
        check_warn "No recent binary archive files (last 5 min) - recorder may not be running"
    fi
else
    check_fail "Binary archive directory not found: $RAW_BUFFER_DIR"
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
    
    # Note: CSV files no longer updated (HDF5-only as of 2026-01-02)
    
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
    FUSION_HDF5=$(find "$FUSION_DIR" -name "*fusion_timing_*.h5" -mmin -10 2>/dev/null)
    if [[ -n "$FUSION_HDF5" ]]; then
        SIZE=$(du -h $FUSION_HDF5 2>/dev/null | head -1 | cut -f1)
        RECORDS=$(h5dump -d timestamp_utc -y -w0 $FUSION_HDF5 2>/dev/null | grep -c ":" || echo "unknown")
        check_pass "Fusion HDF5 found: $(basename $FUSION_HDF5) ($SIZE, ~$RECORDS records)"
    else
        check_warn "No recent fusion HDF5 files (last 10 min)"
    fi
    
    # Note: Fusion CSV no longer updated (HDF5-only as of 2026-01-02)
    
else
    check_warn "Fusion directory not found: $FUSION_DIR"
fi

# =============================================================================
# Phase 4: Science Products (Ionosphere)
# =============================================================================
section "Phase 4: Science Products (Ionosphere)"

SCIENCE_DIR="$PHASE2_DIR/science"
if [[ -d "$SCIENCE_DIR" ]]; then
    check_pass "Science directory exists: $SCIENCE_DIR"
    
    # Check for TEC Output
    TEC_DIR="$SCIENCE_DIR/tec"
    if [[ -d "$TEC_DIR" ]]; then
        # HDF5
        # HDF5
        LAST_TEC=$(find "$TEC_DIR" -name "*tec_*.h5" -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -f2- -d" ")
        
        if [[ -n "$LAST_TEC" ]]; then
            LAST_MOD=$(stat -c %Y "$LAST_TEC")
            NOW=$(date +%s)
            AGE=$((NOW - LAST_MOD))
            
            # Format age
             if [[ $AGE -lt 60 ]]; then
                 AGE_STR="${AGE}s"
             elif [[ $AGE -lt 3600 ]]; then
                 AGE_STR="$((AGE/60))m"
             elif [[ $AGE -lt 86400 ]]; then
                 AGE_STR="$((AGE/3600))h"
             else
                 AGE_STR="$((AGE/86400))d $(( (AGE%86400)/3600 ))h"
             fi
             
            if [[ $AGE -lt 900 ]]; then
                check_pass "Found recent TEC HDF5 file (updated $AGE_STR ago)"
            else
                check_warn "TEC HDF5 stale (last updated $AGE_STR ago) - Check timestd-science-aggregator"
            fi
        else
            check_warn "No TEC HDF5 files found - Check timestd-science-aggregator"
        fi
        
        # Note: TEC CSV files no longer primary (HDF5-only as of 2026-01-02)
    else
        check_warn "TEC directory not found: $TEC_DIR"
    fi
else
    check_warn "Science directory not found: $SCIENCE_DIR (aggregator may not have run yet)"
fi

# Check for GNSS VTEC (L3A)
VTEC_DIR="$DATA_ROOT/data/gnss_vtec"
if [[ -d "$VTEC_DIR" ]]; then
    # HDF5
    VTEC_HDF5=$(find "$VTEC_DIR" -name "*gnss_vtec_*.h5" -mmin -15 2>/dev/null | wc -l)
    if [[ $VTEC_HDF5 -gt 0 ]]; then
        check_pass "GNSS VTEC: Found $VTEC_HDF5 recent HDF5 files (last 15 min)"
    else
        check_warn "GNSS VTEC output directory exists but no recent HDF5 files"
    fi
    
    # Note: GNSS VTEC CSV no longer primary (HDF5-only)
else
    # Optional service
    check_warn "GNSS VTEC directory not found (service specific)"
fi

# =============================================================================
# Phase 5: Adaptive Calibration (Phase 5)
# =============================================================================
section "Phase 5: Adaptive Calibration (System State)"

CAL_STATE_FILE="${DATA_ROOT}/state/timing_calibration.json"

if [[ -f "$CAL_STATE_FILE" ]]; then
    check_pass "Calibration state file exists"
    
     if command -v jq &>/dev/null; then
        CAL_PHASE=$(jq -r '.phase' "$CAL_STATE_FILE" 2>/dev/null)
        CAL_UPDATED=$(jq -r '.saved_at' "$CAL_STATE_FILE" 2>/dev/null)
        
        # Check freshness
        CAL_TS=$(date -d "$CAL_UPDATED" +%s 2>/dev/null || echo "0")
        CAL_AGE=$((NOW - CAL_TS))
        
        if [[ "$CAL_PHASE" == "calibrated" ]]; then
            check_pass "System is fully CALIBRATED (Adaptive Windows Active)"
        elif [[ "$CAL_PHASE" == "provisional" ]]; then
            check_pass "System is PROVISIONAL (GPSDO-locked)"
        else
            # Bootstrap is normal on startup, warn if system up for > 1 hour
            SYS_UPTIME=$(cut -d. -f1 /proc/uptime)
            if [[ $SYS_UPTIME -gt 3600 ]]; then
                check_warn "System stuck in BOOTSTRAP phase (uptime > 1h)"
            else
                check_pass "System in BOOTSTRAP phase (normal for startup)"
            fi
        fi
        
        if [[ $CAL_AGE -lt 600 ]]; then
             check_pass "Calibration state updated recently (${CAL_AGE}s ago)"
        else
             # Format age
             if [[ $CAL_AGE -lt 3600 ]]; then
                 AGE_STR="$((CAL_AGE/60))m"
             elif [[ $CAL_AGE -lt 86400 ]]; then
                 AGE_STR="$((CAL_AGE/3600))h"
             else
                 AGE_STR="$((CAL_AGE/86400))d $(( (CAL_AGE%86400)/3600 ))h"
             fi
             check_warn "Calibration state stale (last updated $AGE_STR ago)"
        fi
        
     else
        check_pass "Calibration state valid (install jq for details)"
     fi
else
    check_warn "Calibration state file NOT FOUND (Adaptive logic inactive?)"
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
