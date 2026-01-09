#!/bin/bash
# =============================================================================
# Chrony TMGR Reach Monitor
# =============================================================================
# Monitors the Chrony TMGR (Time Manager) source reach value and alerts if low.
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
#   0 = OK (reach >= threshold)
#   1 = WARNING (reach < threshold)
#   2 = CRITICAL (TMGR source not found or chronyd not running)
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
            echo "Chrony TMGR Reach Monitor"
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

# Check if chronyd is running
if ! systemctl is-active --quiet chronyd 2>/dev/null; then
    echo "CRITICAL: chronyd service is not running"
    exit 2
fi

# Get TMGR source info
TMGR_LINE=$(chronyc sources 2>/dev/null | grep "TMGR" || true)

if [[ -z "$TMGR_LINE" ]]; then
    echo "CRITICAL: TMGR source not found in chronyc sources"
    echo "  Fusion service may not be writing to Chrony SHM"
    exit 2
fi

# Extract reach value (5th column in chronyc sources output)
REACH_OCT=$(echo "$TMGR_LINE" | awk '{print $5}')

# Convert octal to decimal
REACH_DEC=$((8#$REACH_OCT))

# Calculate success percentage
SUCCESS_PCT=$((REACH_DEC * 100 / 255))

# Determine status
if [[ $REACH_DEC -ge $THRESHOLD_DEC ]]; then
    STATUS="OK"
    EXIT_CODE=0
else
    STATUS="WARNING"
    EXIT_CODE=1
fi

# Output
if [[ "$VERBOSE" == "true" ]] || [[ $EXIT_CODE -ne 0 ]]; then
    echo "$STATUS: Chrony TMGR reach = $REACH_OCT (octal) = $REACH_DEC (decimal) = $SUCCESS_PCT%"
    echo "  Threshold: $THRESHOLD_DEC (decimal)"
    echo "  Full line: $TMGR_LINE"
fi

# Run alert command if provided and status is not OK
if [[ -n "$ALERT_COMMAND" ]] && [[ $EXIT_CODE -ne 0 ]]; then
    eval "$ALERT_COMMAND"
fi

# Restart logic
if [[ "$RESTART_ON_FAILURE" == "true" ]] && [[ "$REACH_DEC" -eq 0 ]]; then
    echo "CRITICAL: Chrony reach is 0. Attempting to restart chronyd..."
    # Should ideally also clear SHM or restart fusion, but start with chronyd
    systemctl restart chronyd
    echo "Restarted chronyd. Fusion service should auto-reconnect."
fi

exit $EXIT_CODE
