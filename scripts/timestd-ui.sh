#!/bin/bash
# HF Time Standard Web-UI Control (Monitoring Dashboard)
#
# Provides real-time visualization of Phase 1 + Phase 2:
#   - Phase 1: raw_buffer recording status
#   - Phase 2: Timing analysis, D_clock, discrimination
#
# Usage: timestd-ui.sh {start|stop|restart|status} [config-file]

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

case $ACTION in
start)
    echo "▶️  Starting Web-UI..."
    
    # Stop existing
    pkill -f "monitoring_server" 2>/dev/null
    sleep 1
    
    mkdir -p "$DATA_ROOT/logs"
    
    SERVER_SCRIPT="$PROJECT_DIR/web-ui/start_server.sh"
    
    echo "   Starting: $SERVER_SCRIPT"
    nohup env TIMESTD_CONFIG="$CONFIG" "$SERVER_SCRIPT" \
        > "$DATA_ROOT/logs/webui.log" 2>&1 &
    
    PID=$!
    sleep 2
    
    if ps -p $PID > /dev/null 2>&1; then
        echo "   ✅ Started (PID: $PID)"
        echo "   🌐 http://localhost:8080/"
        echo "   📄 Log: $DATA_ROOT/logs/webui.log"
    else
        echo "   ❌ Failed to start"
        tail -5 "$DATA_ROOT/logs/webui.log" 2>/dev/null
        exit 1
    fi
    ;;

stop)
    echo "🛑 Stopping Web-UI..."
    
    if ! pgrep -f "monitoring_server" > /dev/null; then
        echo "   ℹ️  Not running"
        exit 0
    fi
    
    pkill -f "monitoring_server" 2>/dev/null
    sleep 1
    echo "   ✅ Stopped"
    ;;

restart)
    echo "🔄 Restarting Web-UI..."
    "$0" stop "$CONFIG"
    sleep 1
    "$0" start "$CONFIG"
    ;;

status)
    if pgrep -f "monitoring_server" > /dev/null; then
        echo "✅ Web-UI: RUNNING → http://localhost:8080/"
        echo "   Dashboard pages:"
        echo "   - /            Overview and channel status"
        echo "   - /timing      Timing analysis and D_clock"
        echo "   - /carriers    Carrier tracking and Doppler"
    else
        echo "⭕ Web-UI: STOPPED"
    fi
    ;;
esac
