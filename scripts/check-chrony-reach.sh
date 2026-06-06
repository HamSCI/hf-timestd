#!/bin/bash
# =============================================================================
# Chrony FUSE/HPPS Reach Monitor
# =============================================================================
# Monitors the Chrony FUSE and HPPS refclock SHM source reach values and
# alerts if low.  FUSE and HPPS are the two timing inputs hf-timestd
# publishes via chrony's SHM driver (see /etc/chrony/chrony.conf):
#
#   FUSE = fused authority output from timestd-fusion (SHM 1)
#   HPPS = high-precision T6 BPSK direct measurement (SHM 2)
#
# (HFPS exists on SHM 3 but is a slower-cadence backup not gated here.)
#
# Reach is an octal value (0-377) representing the last 8 poll attempts:
#   377 (octal) = 11111111 (binary) = 8/8 successful polls (optimal)
#   210 (octal) = 10001000 (binary) = 5/8 successful polls (acceptable)
#     0 (octal) = 00000000 (binary) = 0/8 successful polls (critical)
#
# Usage:
#   ./check-chrony-reach.sh [--threshold DECIMAL] [--alert-command "COMMAND"]
#
# Exit codes:
#   0 = OK (at least one source reach >= threshold)
#   1 = WARNING (all sources reach < threshold)
#   2 = CRITICAL (neither FUSE nor HPPS source found, or chronyd not running)
# =============================================================================

set -euo pipefail

# Default threshold (64 decimal = 100 octal = 50% success rate)
THRESHOLD_DEC=64
ALERT_COMMAND=""
VERBOSE=false
RESTART_ON_FAILURE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --threshold)
            THRESHOLD_DEC="$2"
            shift 2
            ;;
        --alert-command)
            ALERT_COMMAND="$2"
            shift 2
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "Chrony FUSE/HPPS Reach Monitor"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --threshold N          Alert if reach < N (decimal, default: 64)"
            echo "  --alert-command CMD    Command to run on alert"
            echo "  --verbose, -v          Verbose output"
            echo "  --help, -h             Show this help"
            echo ""
            echo "Examples:"
            echo "  $0                                    # Check with default threshold"
            echo "  $0 --threshold 128                    # Alert if reach < 128"
            echo "  $0 --alert-command 'mail -s Alert'    # Send email on alert"
            exit 0
            ;;
        --restart-on-failure)
            RESTART_ON_FAILURE=true
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# Check if chrony is running (Debian/Ubuntu = 'chrony', RHEL/Fedora = 'chronyd')
if ! systemctl is-active --quiet chrony 2>/dev/null && ! systemctl is-active --quiet chronyd 2>/dev/null; then
    echo "CRITICAL: chrony service is not running"
    exit 2
fi

# =============================================================================
# NEW: Check for Chrony SHM segments (indicates fusion is writing)
# Chrony's SHM driver uses System V shared memory with keys 0x4e545030
# ("NTP0"), 0x4e545031 ("NTP1"), etc. — NOT files in /dev/shm — so we
# check via `ipcs -m` for any segment whose key starts with 0x4e54503.
# =============================================================================
SHM_MISSING=false
if ! ipcs -m 2>/dev/null | grep -qE '^0x4e54503[0-9]\b'; then
    # Check if fusion service is even supposed to be writing
    if systemctl is-active --quiet timestd-fusion 2>/dev/null; then
        echo "WARNING: Chrony SHM segments not found but fusion service is running"
        echo "  This may indicate fusion is in single-station mode (Chrony feed disabled)"
        SHM_MISSING=true
    fi
fi

# =============================================================================
# NEW: Check calibration state freshness
# =============================================================================
CALIBRATION_FILE="/var/lib/timestd/state/broadcast_calibration.json"
CALIBRATION_MAX_AGE_HOURS=48

if [[ -f "$CALIBRATION_FILE" ]]; then
    CALIBRATION_AGE_SEC=$(( $(date +%s) - $(stat -c %Y "$CALIBRATION_FILE") ))
    CALIBRATION_AGE_HOURS=$(( CALIBRATION_AGE_SEC / 3600 ))
    
    if [[ $CALIBRATION_AGE_HOURS -gt $CALIBRATION_MAX_AGE_HOURS ]]; then
        echo "WARNING: Calibration state is ${CALIBRATION_AGE_HOURS}h old (max: ${CALIBRATION_MAX_AGE_HOURS}h)"
        echo "  File: $CALIBRATION_FILE"
        echo "  This may indicate Kalman filters are not converging or saving"
    fi
fi

# =============================================================================
# NEW: Check for single-station mode in fusion logs
# =============================================================================
if systemctl is-active --quiet timestd-fusion 2>/dev/null; then
    # Check recent logs for single-station warnings
    SINGLE_STATION_COUNT=$(journalctl -u timestd-fusion --since "10 minutes ago" --no-pager 2>/dev/null | grep -c "single-station" 2>/dev/null || true)
    SINGLE_STATION_COUNT=${SINGLE_STATION_COUNT:-0}
    if [[ "$SINGLE_STATION_COUNT" -gt 10 ]]; then
        echo "WARNING: Fusion service in single-station mode (${SINGLE_STATION_COUNT} warnings in last 10 min)"
        echo "  Chrony feed is likely DISABLED for safety"
        echo "  Check calibration state and upstream data quality"
    fi
fi

# Get FUSE and HPPS source info
FUSE_LINE=$(chronyc sources 2>/dev/null | grep "FUSE" || true)
HPPS_LINE=$(chronyc sources 2>/dev/null | grep "HPPS" || true)

if [[ -z "$FUSE_LINE" ]] && [[ -z "$HPPS_LINE" ]]; then
    echo "CRITICAL: Neither FUSE nor HPPS source found in chronyc sources"
    echo "  Fusion service may not be writing to Chrony SHM"
    exit 2
fi

# Function to extract and check reach for a source
check_source_reach() {
    local SOURCE_NAME="$1"
    local SOURCE_LINE="$2"
    
    if [[ -z "$SOURCE_LINE" ]]; then
        echo "  $SOURCE_NAME: not present"
        return 1
    fi
    
    # Extract reach value (5th column in chronyc sources output)
    local REACH_OCT=$(echo "$SOURCE_LINE" | awk '{print $5}')
    
    # Convert octal to decimal
    local REACH_DEC=$((8#$REACH_OCT))
    
    # Calculate success percentage
    local SUCCESS_PCT=$((REACH_DEC * 100 / 255))
    
    if [[ "$VERBOSE" == "true" ]]; then
        echo "  $SOURCE_NAME: reach = $REACH_OCT (octal) = $REACH_DEC (decimal) = $SUCCESS_PCT%"
    fi
    
    # Return success if reach >= threshold
    if [[ $REACH_DEC -ge $THRESHOLD_DEC ]]; then
        return 0
    else
        return 1
    fi
}

# Check both sources - OK if at least one meets threshold
FUSE_OK=false
HPPS_OK=false
BEST_REACH=0

if [[ -n "$FUSE_LINE" ]]; then
    FUSE_REACH_OCT=$(echo "$FUSE_LINE" | awk '{print $5}')
    FUSE_REACH_DEC=$((8#$FUSE_REACH_OCT))
    if [[ $FUSE_REACH_DEC -ge $THRESHOLD_DEC ]]; then
        FUSE_OK=true
    fi
    if [[ $FUSE_REACH_DEC -gt $BEST_REACH ]]; then
        BEST_REACH=$FUSE_REACH_DEC
    fi
fi

if [[ -n "$HPPS_LINE" ]]; then
    HPPS_REACH_OCT=$(echo "$HPPS_LINE" | awk '{print $5}')
    HPPS_REACH_DEC=$((8#$HPPS_REACH_OCT))
    if [[ $HPPS_REACH_DEC -ge $THRESHOLD_DEC ]]; then
        HPPS_OK=true
    fi
    if [[ $HPPS_REACH_DEC -gt $BEST_REACH ]]; then
        BEST_REACH=$HPPS_REACH_DEC
    fi
fi

# Calculate success percentage for best reach
SUCCESS_PCT=$((BEST_REACH * 100 / 255))

# Determine status - OK if at least one source is good
if [[ "$FUSE_OK" == "true" ]] || [[ "$HPPS_OK" == "true" ]]; then
    STATUS="OK"
    EXIT_CODE=0
else
    STATUS="WARNING"
    EXIT_CODE=1
fi

# Output
if [[ "$VERBOSE" == "true" ]] || [[ $EXIT_CODE -ne 0 ]]; then
    echo "$STATUS: Chrony FUSE/HPPS reach (best) = $BEST_REACH (decimal) = $SUCCESS_PCT%"
    echo "  Threshold: $THRESHOLD_DEC (decimal)"
    [[ -n "$FUSE_LINE" ]] && echo "  FUSE: $FUSE_LINE"
    [[ -n "$HPPS_LINE" ]] && echo "  HPPS: $HPPS_LINE"
fi

# Run alert command if provided and status is not OK
if [[ -n "$ALERT_COMMAND" ]] && [[ $EXIT_CODE -ne 0 ]]; then
    eval "$ALERT_COMMAND"
fi

# Own-only recovery (sigmond docs/timing-chain-architecture.md): this monitor
# MUST NOT restart chrony.  chrony is shared with gpsd; restarting it broke the
# GPS reference and caused a restart storm.  Report only — remediation of a stale
# FUSE/HPPS feed is restarting the *writers* (fusion/core-recorder) or the
# smd-timing reconciler, never chrony.  (RESTART_ON_FAILURE kept for compat.)
if [[ "$RESTART_ON_FAILURE" == "true" ]] && [[ "$BEST_REACH" -eq 0 ]]; then
    echo "CRITICAL: Both FUSE and HPPS chrony sources have reach 0."
    echo "  (not restarting chrony — shared with gpsd; restart the writers instead)"
fi

exit $EXIT_CODE
