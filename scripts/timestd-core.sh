#!/bin/bash
# HF Time Standard Phase 1: Core Recorder Control
#
# Phase 1 captures raw IQ data from radiod RTP stream:
#   - 20 kHz sample rate, complex float32
#   - Per-minute binary complex64 + JSON sidecar
#   - System time tagging
#
# Output: raw_buffer/{CHANNEL}/ (immutable source of truth)
#
# Usage: timestd-core.sh -start|-stop|-status [config-file]

# Source common settings (sets PYTHON, PROJECT_DIR, etc.)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ACTION=""
CONFIG=""

for arg in "$@"; do
    case $arg in
        -start) ACTION="start" ;;
        -stop) ACTION="stop" ;;
        -status) ACTION="status" ;;
        *) CONFIG="$arg" ;;
    esac
done

CONFIG="${CONFIG:-$DEFAULT_CONFIG}"

if [ -z "$ACTION" ]; then
    echo "Usage: $0 -start|-stop|-status [config-file]"
    exit 1
fi

DATA_ROOT=$(get_data_root "$CONFIG")

case $ACTION in
start)
    echo "▶️  Starting Phase 1 Core Recorder..."
    
    if pgrep -f "hf_timestd.core.core_recorder_v2" > /dev/null; then
        echo "   ℹ️  Already running (PID: $(pgrep -f 'hf_timestd.core.core_recorder_v2'))"
        exit 0
    fi
    
    if [ ! -f "$CONFIG" ]; then
        echo "   ❌ Config not found: $CONFIG"
        exit 1
    fi
    
    # Create required directory structure
    mkdir -p "$DATA_ROOT/logs" "$DATA_ROOT/raw_buffer" "$DATA_ROOT/status"
    cd "$PROJECT_DIR"
    
    # Use V2 recorder (ka9q-python RadiodStream)
    nohup $PYTHON -m hf_timestd.core.core_recorder_v2 --config "$CONFIG" \
        > "$DATA_ROOT/logs/phase1-core.log" 2>&1 &
    
    PID=$!
    sleep 3
    
    if ps -p $PID > /dev/null 2>&1; then
        echo "   ✅ Started (PID: $PID)"
        echo "   📄 Log: $DATA_ROOT/logs/phase1-core.log"
        echo "   📦 Output: $DATA_ROOT/raw_buffer/{CHANNEL}/"
    else
        echo "   ❌ Failed to start"
        tail -5 "$DATA_ROOT/logs/phase1-core.log" 2>/dev/null
        exit 1
    fi
    ;;

stop)
    echo "🛑 Stopping Phase 1 Core Recorder..."
    
    if ! pgrep -f "hf_timestd.core.core_recorder_v2" > /dev/null; then
        echo "   ℹ️  Not running"
        exit 0
    fi
    
    pkill -f "hf_timestd.core.core_recorder_v2" 2>/dev/null
    sleep 2
    
    if pgrep -f "hf_timestd.core.core_recorder_v2" > /dev/null; then
        pkill -9 -f "hf_timestd.core.core_recorder_v2" 2>/dev/null
    fi
    
    echo "   ✅ Stopped"
    ;;

status)
    if pgrep -f "hf_timestd.core.core_recorder_v2" > /dev/null; then
        echo "✅ Phase 1 Core Recorder: RUNNING (PID: $(pgrep -f 'hf_timestd.core.core_recorder_v2'))"
        echo "   Output: $DATA_ROOT/raw_buffer/{CHANNEL}/"
        
        # Show channel count if raw_buffer exists
        if [ -d "$DATA_ROOT/raw_buffer" ]; then
            CHANNELS=$(ls -d "$DATA_ROOT/raw_buffer"/*/  2>/dev/null | wc -l)
            echo "   Active channels: $CHANNELS"
        fi
    else
        echo "⭕ Phase 1 Core Recorder: STOPPED"
    fi
    ;;
esac
