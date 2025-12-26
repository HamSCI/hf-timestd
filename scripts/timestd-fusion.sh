#!/bin/bash
# HF Time Standard Phase 3 Fusion Service Control
#
# Phase 3 processes Phase 2 CSV data to produce:
#   - Fused UTC(NIST) alignment
#   - Chrony SHM updates
#   - Feedback calibration state
#
# Input:  phase2/{CHANNEL}/      (clock offset CSVs)
# Output: phase2/fusion/         (fused clock offset)
#         Chrony SHM segment 0
#
# Usage: timestd-fusion.sh {start|stop|restart|status} [config-file]

# Source common settings (sets PYTHON, PROJECT_DIR, etc.)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ACTION=""
CONFIG=""

# First positional arg is the action
ACTION="$1"
shift 2>/dev/null || true

# Remaining args could be config file
for arg in "$@"; do
    case $arg in
        start|stop|restart|status) ;; # ignore if repeated
        *) CONFIG="$arg" ;;
    esac
done

CONFIG="${CONFIG:-$DEFAULT_CONFIG}"

if [ -z "$ACTION" ]; then
    echo "Usage: $0 {start|stop|restart|status} [config-file]"
    exit 1
fi

DATA_ROOT=$(get_data_root "$CONFIG")
LOG_DIR=$(get_log_dir "$CONFIG")

case $ACTION in
start)
    echo "▶️  Starting Phase 3 Fusion Service..."
    
    # Stop existing first
    pkill -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null
    sleep 1
    
    if [ ! -f "$CONFIG" ]; then
        echo "   ❌ Config not found: $CONFIG"
        exit 1
    fi
    
    # Create directories
    mkdir -p "$LOG_DIR" "$DATA_ROOT/phase2/fusion"
    cd "$PROJECT_DIR"
    
    # Default interval is 60s, but can be configured
    # In test mode we might want faster updates, but 60s is standard for fusion
    
    nohup $PYTHON -m hf_timestd.core.multi_broadcast_fusion \
      --data-root "$DATA_ROOT" \
      --interval 60.0 \
      --log-level INFO \
      --enable-chrony \
      > "$LOG_DIR/phase2-fusion.log" 2>&1 &
    
    echo "   🔀 Started Multi-Broadcast Fusion (17 broadcasts → UTC(NIST) → Chrony SHM)"
    echo "   📄 Log: $LOG_DIR/phase2-fusion.log"
    echo "   🎯 Output: $DATA_ROOT/phase2/fusion/fused_d_clock.csv"
    ;;

stop)
    echo "🛑 Stopping Phase 3 Fusion Service..."
    
    COUNT=$(pgrep -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null | wc -l)
    if [ "$COUNT" -eq 0 ]; then
        echo "   ℹ️  Not running"
        exit 0
    fi
    
    pkill -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null
    sleep 1
    
    REMAINING=$(pgrep -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null | wc -l)
    if [ "$REMAINING" -gt 0 ]; then
        pkill -9 -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null
    fi
    
    echo "   ✅ Stopped Fusion Service"
    ;;

restart)
    echo "🔄 Restarting Phase 3 Fusion Service..."
    "$0" stop "$CONFIG"
    sleep 2
    "$0" start "$CONFIG"
    ;;

status)
    COUNT=$(pgrep -f "hf_timestd.core.multi_broadcast_fusion" 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        echo "✅ Phase 3 Fusion:    RUNNING"
        echo "   Log:    $LOG_DIR/phase2-fusion.log"
        echo "   Output: $DATA_ROOT/phase2/fusion/fused_d_clock.csv"
    else
        echo "⭕ Phase 3 Fusion:    STOPPED"
    fi
    ;;
esac
