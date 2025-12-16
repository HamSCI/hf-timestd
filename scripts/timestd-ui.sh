#!/bin/bash
# HF Time Standard Web-UI Control (Monitoring Dashboard)
#
# Provides real-time visualization of Phase 1 + Phase 2:
#   - Phase 1: raw_buffer recording status
#   - Phase 2: Timing analysis, D_clock, discrimination
#
# Usage: timestd-ui.sh -start|-stop|-status [config-file]

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
    echo "▶️  Starting Web-UI..."
    
    # Stop existing
    pkill -f "monitoring-server" 2>/dev/null
    sleep 1
    
    mkdir -p "$DATA_ROOT/logs"
    cd "$PROJECT_DIR/web-ui"
    
    nohup env TIMESTD_CONFIG="$CONFIG" node monitoring-server-v3.js \
        > "$DATA_ROOT/logs/webui.log" 2>&1 &
    
    PID=$!
    sleep 2
    
    if ps -p $PID > /dev/null 2>&1; then
        echo "   ✅ Started (PID: $PID)"
        echo "   🌐 http://localhost:3000/"
        echo "   📄 Log: $DATA_ROOT/logs/webui.log"
    else
        echo "   ❌ Failed to start"
        tail -5 "$DATA_ROOT/logs/webui.log" 2>/dev/null
        exit 1
    fi
    ;;

stop)
    echo "🛑 Stopping Web-UI..."
    
    if ! pgrep -f "monitoring-server" > /dev/null; then
        echo "   ℹ️  Not running"
        exit 0
    fi
    
    pkill -f "monitoring-server" 2>/dev/null
    sleep 1
    echo "   ✅ Stopped"
    ;;

status)
    if pgrep -f "monitoring-server" > /dev/null; then
        echo "✅ Web-UI: RUNNING → http://localhost:3000/"
        echo "   Dashboard pages:"
        echo "   - /            Overview and channel status"
        echo "   - /timing      Timing analysis and D_clock"
        echo "   - /carriers    Carrier tracking and Doppler"
    else
        echo "⭕ Web-UI: STOPPED"
    fi
    ;;
esac
