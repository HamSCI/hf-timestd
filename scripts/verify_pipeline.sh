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
#   - Phase 2: Metrology (L1 raw measurements - HDF5)
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

# Detect engine type from config (radiod: 9 frequency channels, phase-engine: 17 broadcast channels)
# Both modes are broadcast-oriented — metrology and physics resolve to the same 17 broadcasts.
# The difference is channel naming: radiod uses SHARED_* for multi-station frequencies,
# phase-engine uses per-station names (WWV_5000, WWVH_5000, BPM_5000) via beamforming.
ENGINE_TYPE="radiod"
CONFIG_FILE="/etc/hf-timestd/timestd-config.toml"
if [[ -f "$CONFIG_FILE" ]]; then
    # Check ka9q.source first (takes priority), then recorder.engine
    KA9Q_SOURCE=$(grep -E '^source\s*=' "$CONFIG_FILE" 2>/dev/null | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/')
    RECORDER_ENGINE=$(grep -E '^engine\s*=' "$CONFIG_FILE" 2>/dev/null | grep -v '^#' | head -1 | sed 's/.*=\s*"\([^"]*\)".*/\1/')
    if [[ -n "$KA9Q_SOURCE" && "$KA9Q_SOURCE" != "radiod" ]]; then
        ENGINE_TYPE="$KA9Q_SOURCE"
    elif [[ -n "$RECORDER_ENGINE" && "$RECORDER_ENGINE" != "radiod" ]]; then
        ENGINE_TYPE="$RECORDER_ENGINE"
    fi
fi

if [[ "$ENGINE_TYPE" == "radiod" ]]; then
    EXPECTED_CHANNELS=9
    ENGINE_DESC="radiod (9 frequency channels, SHARED require discrimination)"
else
    EXPECTED_CHANNELS=17
    ENGINE_DESC="phase-engine (17 broadcast channels, per-station beamforming)"
fi

echo "=============================================="
echo "  hf-timestd Pipeline Verification"
echo "=============================================="
echo "  Mode:   $MODE"
echo "  Engine: $ENGINE_DESC"
echo "  Data:   $DATA_ROOT"
echo "=============================================="

# =============================================================================
# Phase 0: Service Status (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    section "Phase 0: Service Status"
    
    # Core pipeline services (failures expected if not running)
    CORE_SERVICES=(
        "timestd-core-recorder.service"
        "timestd-metrology.service"
        "timestd-l2-calibration.service"
        "timestd-fusion.service"
        "timestd-physics.service"
        "timestd-web-api.service"
    )
    
    # Optional monitoring/supplementary services (warnings only)
    OPTIONAL_SERVICES=(
        "timestd-vtec.service"
        "timestd-radiod-monitor.service"
    )
    
    # Threshold for "recently restarted" warning (5 minutes)
    UPTIME_WARN_SEC=300
    NOW=$(date +%s)
    
    # Check core services (failures if not running)
    for service in "${CORE_SERVICES[@]}"; do
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
            check_fail "$service is NOT running"
        fi
    done
    
    # Check optional services (warnings only, not failures)
    for service in "${OPTIONAL_SERVICES[@]}"; do
        if systemctl is-active --quiet "$service"; then
            START_TIMESTAMP=$(systemctl show "$service" -p ActiveEnterTimestamp --value)
            if [[ -n "$START_TIMESTAMP" ]]; then
                START_EPOCH=$(date -d "$START_TIMESTAMP" +%s 2>/dev/null || echo "0")
                UPTIME=$((NOW - START_EPOCH))
                
                if [[ $UPTIME -lt 60 ]]; then
                    UPTIME_STR="${UPTIME}s"
                elif [[ $UPTIME -lt 3600 ]]; then
                    UPTIME_STR="$((UPTIME/60))m"
                else
                    UPTIME_STR="$((UPTIME/3600))h"
                fi
                check_pass "$service is running (uptime: $UPTIME_STR) [optional]"
            else
                check_pass "$service is running [optional]"
            fi
        else
            ENABLE_STATE=$(systemctl show "$service" -p UnitFileState --value)
            if [[ "$ENABLE_STATE" == "disabled" || "$ENABLE_STATE" == "masked" ]]; then
                echo -e "${BLUE}ℹ️  INFO${NC} $service is disabled (optional monitoring)"
            else
                check_warn "$service is NOT running (optional monitoring)"
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
    
    SEARCH_PATHS="$RAW_BUFFER_DIR"
    if [[ -d "/dev/shm/timestd/raw_buffer" ]]; then
        SEARCH_PATHS="$SEARCH_PATHS /dev/shm/timestd/raw_buffer"
        check_pass "Hot buffer (tiered storage) exists: /dev/shm/timestd/raw_buffer"
    fi
    
    # Check for recent .bin.zst files (within last 5 minutes)
    RECENT_BIN=$(find $SEARCH_PATHS -name "*.bin.zst" -mmin -5 2>/dev/null | wc -l)
    
    if [[ $RECENT_BIN -gt 0 ]]; then
        check_pass "Found $RECENT_BIN recent binary archive files (last 5 min)"
        
        # Check for matching .json metadata sidecars
        RECENT_JSON=$(find $SEARCH_PATHS -name "*.json" -mmin -5 2>/dev/null | wc -l)
        if [[ $RECENT_JSON -ge $RECENT_BIN ]]; then
            check_pass "All binary files have matching JSON metadata sidecars"
        else
            check_warn "Only found $RECENT_JSON metadata files for $RECENT_BIN binary files"
            echo "  → Critical for timing alignment (RTP-to-Unix sync)"
        fi
        
        # CRITICAL: Buffer alignment check (v5.3.12 fix)
        # start_system_time MUST equal minute_boundary exactly for correct timing
        if command -v jq &>/dev/null && [[ -d "/dev/shm/timestd/raw_buffer" ]]; then
            # Find a recent JSON file to check alignment
            SAMPLE_JSON=$(find /dev/shm/timestd/raw_buffer -name "*.json" -mmin -2 2>/dev/null | head -1)
            if [[ -n "$SAMPLE_JSON" ]]; then
                MINUTE_BOUNDARY=$(jq -r '.minute_boundary' "$SAMPLE_JSON" 2>/dev/null)
                START_SYSTEM_TIME=$(jq -r '.start_system_time' "$SAMPLE_JSON" 2>/dev/null)
                
                if [[ -n "$MINUTE_BOUNDARY" ]] && [[ -n "$START_SYSTEM_TIME" ]]; then
                    # Check if start_system_time equals minute_boundary (allow for float representation)
                    # Convert to integers for comparison (truncate decimals)
                    MB_INT=${MINUTE_BOUNDARY%.*}
                    SST_INT=${START_SYSTEM_TIME%.*}
                    
                    if [[ "$MB_INT" == "$SST_INT" ]]; then
                        # Check if there's a fractional offset
                        if [[ "$START_SYSTEM_TIME" == *"."* ]] && [[ "${START_SYSTEM_TIME#*.}" != "0" ]]; then
                            FRAC="${START_SYSTEM_TIME#*.}"
                            # If fractional part is significant (>1ms = 0.001)
                            if [[ "${FRAC:0:3}" != "000" ]] && [[ "${FRAC:0:3}" != "0" ]]; then
                                check_warn "Buffer alignment: start_system_time has offset (${START_SYSTEM_TIME} vs ${MINUTE_BOUNDARY})"
                                echo "  → May cause timing errors - consider updating to v5.3.12+"
                            else
                                check_pass "Buffer alignment: start_system_time = minute_boundary (exact)"
                            fi
                        else
                            check_pass "Buffer alignment: start_system_time = minute_boundary (exact)"
                        fi
                    else
                        check_fail "Buffer alignment BROKEN: start_system_time ($START_SYSTEM_TIME) != minute_boundary ($MINUTE_BOUNDARY)"
                        echo "  → CRITICAL: This causes timing errors of 100s of milliseconds"
                        echo "  → Fix: Update to v5.3.12+ and restart core-recorder"
                    fi
                fi
            fi
        fi
    else
        check_warn "No recent binary archive files (last 5 min) - recorder may not be running"
    fi
else
    check_fail "Binary archive directory not found: $RAW_BUFFER_DIR"
fi

# =============================================================================
# Phase 2: Metrology (L1 Raw Measurements)
# =============================================================================
section "Phase 2: Metrology (L1 Measurements)"

PHASE2_DIR="$DATA_ROOT/phase2"
if [[ -d "$PHASE2_DIR" ]]; then
    check_pass "Phase 2 directory exists: $PHASE2_DIR"
    
    # Count channel directories — expected set depends on engine mode
    # radiod: 9 (SHARED_2500..SHARED_15000, WWV_20000, WWV_25000, CHU_3330, CHU_7850, CHU_14670)
    # phase-engine: up to 17 (WWV_2500..WWV_25000, WWVH_2500..WWVH_15000, CHU_*, BPM_*)
    # Both modes: directories are named {STATION}_{FREQ_KHZ} or SHARED_{FREQ_KHZ}
    CHANNELS=$(find "$PHASE2_DIR" -maxdepth 1 -type d -name "*_*" 2>/dev/null | wc -l)
    if [[ $CHANNELS -gt 0 ]]; then
        if [[ "$ENGINE_TYPE" == "phase-engine" ]]; then
            # In phase-engine mode, SHARED_* directories are legacy — only broadcast-specific dirs expected
            SHARED_COUNT=$(find "$PHASE2_DIR" -maxdepth 1 -type d -name "SHARED_*" 2>/dev/null | wc -l)
            BROADCAST_COUNT=$((CHANNELS - SHARED_COUNT))
            if [[ $SHARED_COUNT -gt 0 ]]; then
                check_warn "Found $SHARED_COUNT legacy SHARED_* directories (phase-engine uses per-broadcast channels)"
                echo "  → SHARED dirs are from previous radiod operation, not actively written in phase-engine mode"
            fi
            check_pass "Found $BROADCAST_COUNT broadcast channel directories (phase-engine mode, expect up to 17)"
        else
            check_pass "Found $CHANNELS channel directories (radiod mode, expect 9)"
        fi
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
            
            # Skip legacy SHARED_* directories in phase-engine mode
            if [[ "$ENGINE_TYPE" == "phase-engine" && "$CHANNEL" == SHARED_* ]]; then
                continue
            fi
            # Metrology (L1/L2 primary metric)
            # Find most recent file (no time filter - we check latency below)
            LATEST_HDF5=$(find "$channel_dir/metrology" -name "${CHANNEL}_metrology_measurements_*.h5" -type f 2>/dev/null | sort | tail -1)
            
            if [[ -n "$LATEST_HDF5" ]]; then
                # Get size and age
                SIZE=$(du -h "$LATEST_HDF5" | cut -f1)
                HDF5_MTIME=$(stat -c %Y "$LATEST_HDF5")
                LATENCY=$((NOW - HDF5_MTIME))
                
                # Metrology updates vary by channel (10-30 min typical)
                # Use 30-minute threshold to avoid false positives
                if [[ $LATENCY -lt 1800 ]]; then
                    check_pass "$CHANNEL: Metrology measurements found (latency: ${LATENCY}s, $SIZE)"
                else
                    check_warn "$CHANNEL: Metrology measurements found but STALE (latency: ${LATENCY}s)"
                fi
                ((HDF5_COUNT++))
            else
                check_warn "$CHANNEL: No HDF5 metrology measurements found"
            fi
        fi
    done
    
    if [[ $HDF5_COUNT -eq 0 ]]; then
        check_fail "No HDF5 timing measurements found across any channels"
    fi
    
    # Note: CSV files no longer updated (HDF5-only as of 2026-01-02)
    # Note: BCD discrimination and tone_detections HDF5 are legacy products from phase2_analytics_service
    #       The current metrology_service writes L1 metrology measurements directly.
    
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
    
    # Check for fusion HDF5 output and verify it's being actively written
    FUSION_HDF5=$(find "$FUSION_DIR" -name "*fusion_timing_*.h5" -type f 2>/dev/null | sort | tail -1)
    if [[ -n "$FUSION_HDF5" ]]; then
        SIZE=$(du -h "$FUSION_HDF5" 2>/dev/null | head -1 | cut -f1)
        
        # Check HDF5 file freshness (fusion writes every ~60s)
        HDF5_MTIME=$(stat -c %Y "$FUSION_HDF5" 2>/dev/null || echo "0")
        HDF5_AGE=$((NOW - HDF5_MTIME))
        
        if [[ $HDF5_AGE -gt 300 ]]; then
            # >5 min is a critical failure
            check_fail "Fusion HDF5 not updated in ${HDF5_AGE}s (expected ~60s)"
        elif [[ $HDF5_AGE -gt 120 ]]; then
            # >2 min is suspicious
            check_warn "Fusion HDF5 last updated ${HDF5_AGE}s ago (expected ~60s)"
        else
            check_pass "Fusion HDF5 actively being written (${HDF5_AGE}s ago, $SIZE)"
            
            # Steel Ruler Health Check (Issue 3.5)
            if command -v jq &>/dev/null; then
                CAL_FILE="${DATA_ROOT}/state/broadcast_calibration.json"
                if [[ -f "$CAL_FILE" ]]; then
                    OFFSET=$(jq -r '._kalman_state.offset_ms' "$CAL_FILE" 2>/dev/null)
                    DRIFT=$(jq -r '._kalman_state.drift_ms_per_min' "$CAL_FILE" 2>/dev/null)
                    
                    if [[ "$DRIFT" == "0.0" || "$DRIFT" == "0" ]]; then
                        check_pass "Steel Ruler: Baseline is STABLE (drift = 0.0 ms/min)"
                    else
                        check_warn "Steel Ruler: Baseline is WALKING (drift = ${DRIFT} ms/min)"
                        echo "  → System may be in legacy mode or not yet converged"
                    fi
                    echo "  → Current Kalman Offset: ${OFFSET} ms"
                fi
            fi
        fi
        
        # Check fusion service log file for activity (production only)
        # Note: Logs rotate at midnight, check both .log and .log.1
        if [[ "$MODE" == "production" ]]; then
            FUSION_LOG="/var/log/hf-timestd/fusion.log"
            FUSION_LOG_1="/var/log/hf-timestd/fusion.log.1"
            
            # Use whichever log is more recent
            if [[ -f "$FUSION_LOG" ]] && [[ -s "$FUSION_LOG" ]]; then
                ACTIVE_LOG="$FUSION_LOG"
            elif [[ -f "$FUSION_LOG_1" ]]; then
                ACTIVE_LOG="$FUSION_LOG_1"
            else
                ACTIVE_LOG=""
            fi
            
            if [[ -n "$ACTIVE_LOG" ]]; then
                LOG_MTIME=$(stat -c %Y "$ACTIVE_LOG" 2>/dev/null || echo "0")
                LOG_AGE=$((NOW - LOG_MTIME))
                
                if [[ $LOG_AGE -gt 120 ]]; then
                    check_fail "Fusion service SILENT (log not updated in ${LOG_AGE}s)"
                    echo "  → Cause: Python crash during initialization, import error"
                    echo "  → Diagnose: sudo journalctl -u timestd-fusion -n 100"
                else
                    # Check for recent errors in log
                    ERROR_COUNT=$(tail -50 "$ACTIVE_LOG" 2>/dev/null | grep -c -E "(ERROR|CRITICAL|Traceback|Exception|CRASHED)" 2>/dev/null || echo "0")
                    ERROR_COUNT=$(echo "$ERROR_COUNT" | tr -d '\n' | tr -d ' ')
                    if [[ "$ERROR_COUNT" -gt 0 ]] 2>/dev/null; then
                        check_warn "Fusion service has $ERROR_COUNT recent errors in logs"
                        echo "  → Check: tail -50 $ACTIVE_LOG | grep ERROR"
                    fi
                    
                    # D_clock sanity check - extract recent D_clock values
                    DCLOCK_LINE=$(grep "Fused D_clock" "$ACTIVE_LOG" 2>/dev/null | tail -1)
                    if [[ -n "$DCLOCK_LINE" ]]; then
                        # Extract D_clock value (e.g., "+31.952 ms" or "-1.772 ms")
                        DCLOCK_MS=$(echo "$DCLOCK_LINE" | grep -oP 'D_clock: [+-]?\d+\.?\d*' | grep -oP '[+-]?\d+\.?\d*')
                        if [[ -n "$DCLOCK_MS" ]]; then
                            # Check if absolute value is reasonable (<100ms)
                            DCLOCK_ABS=${DCLOCK_MS#-}  # Remove leading minus
                            DCLOCK_INT=${DCLOCK_ABS%.*}  # Get integer part
                            
                            if [[ -n "$DCLOCK_INT" ]] && [[ "$DCLOCK_INT" -lt 100 ]]; then
                                check_pass "D_clock sanity: ${DCLOCK_MS}ms (within ±100ms)"
                            elif [[ -n "$DCLOCK_INT" ]] && [[ "$DCLOCK_INT" -lt 500 ]]; then
                                check_warn "D_clock elevated: ${DCLOCK_MS}ms (expected <100ms)"
                                echo "  → May indicate timing alignment issues"
                            else
                                check_fail "D_clock UNSTABLE: ${DCLOCK_MS}ms (expected <100ms)"
                                echo "  → CRITICAL: Check buffer alignment (v5.3.12 fix)"
                                echo "  → Diagnose: grep 'expected_marker_at_sample' /var/log/hf-timestd/phase2-*.log*"
                            fi
                        fi
                    fi
                fi
            fi
        fi
    else
        check_fail "No fusion HDF5 files found in $FUSION_DIR"
        echo "  → Cause: Fusion service never ran or failed to initialize"
        echo "  → Diagnose: sudo systemctl status timestd-fusion"
        echo "  → Check: sudo journalctl -u timestd-fusion -n 100"
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
             
            # TEC freshness check (expected update every ~5 minutes)
            if [[ $AGE -lt 900 ]]; then
                # <15 min is good
                check_pass "TEC HDF5 fresh (updated ${AGE_STR} ago)"
            elif [[ $AGE -lt 1800 ]]; then
                # 15-30 min is suspicious
                check_warn "TEC HDF5 stale (${AGE_STR}, expected ~5min updates)"
                echo "  → Possible cause: No multi-frequency detections available"
                echo "  → Check: Metrology producing measurements on multiple bands"
                echo "  → Diagnose: sudo journalctl -u timestd-physics -n 50"
            else
                # >30 min is a failure
                check_fail "TEC HDF5 very stale (${AGE_STR})"
                echo "  → Cause: Physics service stuck or no multi-frequency data"
                echo "  → Diagnose: sudo systemctl status timestd-physics"
                echo "  → Check logs: sudo journalctl -u timestd-physics -n 100"
                echo "  → Fix: sudo systemctl restart timestd-physics"
            fi
        else
            check_warn "No TEC HDF5 files found - Check timestd-physics"
        fi
        
        # Note: TEC CSV files no longer primary (HDF5-only as of 2026-01-02)
    else
        check_warn "TEC directory not found: $TEC_DIR"
    fi
else
    check_warn "Science directory not found: $SCIENCE_DIR (physics service may not have run yet)"
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
# Phase 5: Adaptive Calibration (System State)
# =============================================================================
section "Phase 5: Adaptive Calibration (System State)"

# 5a. Timing Authority Check
# As of v5.4.0, bootstrap is deprecated. Check timing authority mode instead.
# CONFIG_FILE already set at top of script during engine detection
TIMING_AUTHORITY="unknown"

if [[ -f "$CONFIG_FILE" ]]; then
    TIMING_AUTHORITY=$(grep -E '^authority\s*=' "$CONFIG_FILE" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' | head -1)
    if [[ -z "$TIMING_AUTHORITY" ]]; then
        TIMING_AUTHORITY="rtp"  # Default
    fi
fi

if [[ "$TIMING_AUTHORITY" == "rtp" ]]; then
    check_pass "Timing authority: RTP mode (GPS+PPS via radiod - authoritative)"
    echo "  → Clock discipline via GPS+PPS, not HF fusion"
elif [[ "$TIMING_AUTHORITY" == "fusion" ]]; then
    # In Fusion mode, check if MetrologyEngine has achieved lock
    # This is now internal to MetrologyEngine (FusionTimingState)
    # We can check the metrology logs for lock status
    FUSION_LOCKED=false
    for logfile in /var/log/hf-timestd/phase2-*.log; do
        if [[ -f "$logfile" ]] && grep -q "PROVISIONAL LOCK\|REFINED LOCK" "$logfile" 2>/dev/null; then
            FUSION_LOCKED=true
            break
        fi
    done
    
    if [[ "$FUSION_LOCKED" == "true" ]]; then
        check_pass "Timing authority: Fusion mode (timing lock achieved)"
    else
        check_warn "Timing authority: Fusion mode (timing lock pending)"
        echo "  → MetrologyEngine searching for timing lock"
    fi
else
    check_warn "Timing authority: $TIMING_AUTHORITY (unknown mode)"
fi

# 5b. Broadcast Calibration State
CAL_STATE_FILE="${DATA_ROOT}/state/broadcast_calibration.json"

if [[ -f "$CAL_STATE_FILE" ]]; then
    check_pass "Modern calibration state exists (broadcast_calibration.json)"
    
    if command -v jq &>/dev/null; then
        # Count calibrated broadcasts (excluding the _kalman_state key)
        STATIONS=$(jq -r 'keys | map(select(. != "_kalman_state")) | length' "$CAL_STATE_FILE" 2>/dev/null)
        check_pass "Found $STATIONS calibrated broadcast channels"
        
        # Check last update
        LATEST_STATION=$(jq -r 'to_entries | map(select(.key != "_kalman_state")) | sort_by(.value.last_updated) | last | .key' "$CAL_STATE_FILE" 2>/dev/null)
        LATEST_TS=$(jq -r ".\"$LATEST_STATION\".last_updated" "$CAL_STATE_FILE" 2>/dev/null)
        
        if [[ -n "$LATEST_TS" ]]; then
            TS=$(date -d "$LATEST_TS" +%s 2>/dev/null || echo "0")
            AGE=$((NOW - TS))
            if [[ $AGE -lt 600 ]]; then
                check_pass "Calibration is FRESH (${AGE}s ago, via $LATEST_STATION)"
            else
                check_warn "Calibration is STALE (${AGE}s ago)"
            fi
        fi
    fi
else
    check_warn "Modern calibration state file NOT FOUND"
fi


# =============================================================================
# Chrony Integration (Production Only)
# =============================================================================
if [[ "$MODE" == "production" ]]; then
    section "Chrony Integration"
    
    if command -v chronyc &>/dev/null; then
        # In RTP mode, GPS+PPS disciplines the clock, not HF-timestd
        # HF-timestd TSL sources may still be configured but aren't primary
        if [[ "$TIMING_AUTHORITY" == "rtp" ]]; then
            # Check for selected time source (marked with * = selected)
            # Could be: refclock (#*), NTP server (^*), or pool member (^*)
            # In RTP mode, we trust radiod's GPS+PPS - chrony source is informational
            SELECTED_REF=$(chronyc sources 2>/dev/null | grep -E "^#\*" | awk '{print $2}')
            SELECTED_NTP=$(chronyc sources 2>/dev/null | grep -E "^\^\*" | awk '{print $2}')
            
            if [[ -n "$SELECTED_REF" ]]; then
                check_pass "Chrony using refclock: $SELECTED_REF"
            elif [[ -n "$SELECTED_NTP" ]]; then
                # Check if it's a stratum 1 source (likely GPS-disciplined)
                STRATUM=$(chronyc sources 2>/dev/null | grep -E "^\^\*" | awk '{print $3}')
                if [[ "$STRATUM" == "1" ]]; then
                    check_pass "Chrony using stratum-1 NTP: $SELECTED_NTP (GPS-disciplined)"
                else
                    check_pass "Chrony using NTP source: $SELECTED_NTP (stratum $STRATUM)"
                fi
            else
                check_warn "Chrony has no selected time source"
                echo "  → Check: chronyc sources"
            fi
            
            echo -e "${BLUE}ℹ️  INFO${NC} RTP mode: radiod provides authoritative timing via GPS+PPS"
            
            # TSL sources are informational in RTP mode
            if chronyc sources 2>/dev/null | grep -q "TSL"; then
                echo -e "${BLUE}ℹ️  INFO${NC} HF-timestd TSL sources configured (secondary in RTP mode)"
            fi
        else
            # Fusion mode - HF-timestd should discipline the clock
            if chronyc sources 2>/dev/null | grep -q "TSL"; then
                TSL_COUNT=$(chronyc sources 2>/dev/null | grep "TSL" | wc -l)
                check_pass "Chrony HF-timestd feed configured ($TSL_COUNT sources: TSL1=L1, TSL2=L2)"
                
                # Check reachability
                TSL1_REACH=$(chronyc sources 2>/dev/null | grep "TSL1" | awk '{print $5}')
                TSL2_REACH=$(chronyc sources 2>/dev/null | grep "TSL2" | awk '{print $5}')
                
                if [[ "$TSL1_REACH" == "0" ]] && [[ "$TSL2_REACH" == "0" ]]; then
                    check_fail "TSL sources not reachable (reach: TSL1=$TSL1_REACH, TSL2=$TSL2_REACH)"
                    echo "  → Check fusion service: systemctl status timestd-fusion"
                    echo "  → Check SHM permissions: ipcs -m | grep 0x4e54503"
                elif [[ -n "$TSL1_REACH" ]] && [[ "$TSL1_REACH" != "0" ]] || [[ -n "$TSL2_REACH" ]] && [[ "$TSL2_REACH" != "0" ]]; then
                    TSL1_DEC=$((8#$TSL1_REACH))
                    TSL2_DEC=$((8#$TSL2_REACH))
                    check_pass "TSL sources reachable (TSL1: $TSL1_REACH/$TSL1_DEC polls, TSL2: $TSL2_REACH/$TSL2_DEC polls)"
                    
                    # Check if chrony is using HF-timestd
                    SELECTED=$(chronyc sources 2>/dev/null | grep "TSL" | grep -E "^#\*" | awk '{print $2}')
                    if [[ -n "$SELECTED" ]]; then
                        check_pass "Chrony using HF-timestd source: $SELECTED"
                    else
                        COMBINED=$(chronyc sources 2>/dev/null | grep "TSL" | grep -E "^#\+" | awk '{print $2}')
                        if [[ -n "$COMBINED" ]]; then
                            check_pass "Chrony combining HF-timestd source: $COMBINED"
                        else
                            check_warn "Chrony not yet using HF-timestd (sources still being evaluated)"
                        fi
                    fi
                fi
            else
                check_fail "Chrony HF-timestd feed not configured (Fusion mode requires TSL sources)"
                echo "  → Check: /etc/hf-timestd/chrony-timestd-refclocks.conf"
            fi
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
