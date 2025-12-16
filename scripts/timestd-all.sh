#!/bin/bash
# HF Time Standard All Services Control: Core + Analytics + Web-UI
#
# Two-Phase Pipeline Architecture:
#   Phase 1: Core Recorder → raw_buffer/ (20 kHz binary IQ)
#   Phase 2: Analytics → phase2/ (timing analysis, D_clock)
#
# Usage: timestd-all.sh {start|stop|restart|status} [config-file]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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

# Support both new and legacy config names
if [ -f "$PROJECT_DIR/config/timestd-config.toml" ]; then
    CONFIG="${CONFIG:-$PROJECT_DIR/config/timestd-config.toml}"
else
    CONFIG="${CONFIG:-$PROJECT_DIR/config/timestd-config.toml}"
fi

if [ -z "$ACTION" ]; then
    echo "Usage: $0 {start|stop|restart|status} [config-file]"
    exit 1
fi

get_data_root() {
    if [ -f "$CONFIG" ]; then
        MODE=$(grep '^mode' "$CONFIG" | cut -d'"' -f2)
        if [ "$MODE" = "production" ]; then
            grep '^production_data_root' "$CONFIG" | cut -d'"' -f2
        else
            grep '^test_data_root' "$CONFIG" | cut -d'"' -f2
        fi
    else
        echo "/tmp/timestd-test"
    fi
}

DATA_ROOT=$(get_data_root)

case $ACTION in
start)
    echo "🚀 Starting All HF Time Standard Services"
    echo "================================================================"
    echo "📋 Config: $CONFIG"
    echo "📁 Data: $DATA_ROOT"
    echo ""
    echo "📦 Phase 1: Core Recorder (20 kHz raw_buffer)"
    "$SCRIPT_DIR/timestd-core.sh" start "$CONFIG"
    echo ""
    echo "📊 Phase 2: Analytics (timing analysis, D_clock)"
    "$SCRIPT_DIR/timestd-analytics.sh" start "$CONFIG"
    echo ""
    echo "🌐 Web-UI (monitoring dashboard)"
    "$SCRIPT_DIR/timestd-ui.sh" start "$CONFIG"
    
    echo ""
    echo "================================================================"
    echo "✅ All real-time services started"
    echo "📊 Dashboard: http://localhost:3000/"
    ;;

stop)
    echo "🛑 Stopping All HF Time Standard Services"
    echo "================================================================"
    
    "$SCRIPT_DIR/timestd-ui.sh" stop
    "$SCRIPT_DIR/timestd-analytics.sh" stop
    "$SCRIPT_DIR/timestd-core.sh" stop
    
    echo ""
    echo "✅ All services stopped"
    ;;

restart)
    echo "🔄 Restarting All HF Time Standard Services"
    echo "================================================================"
    "$0" stop "$CONFIG"
    sleep 2
    "$0" start "$CONFIG"
    ;;

status)
    echo "📊 HF Time Standard Service Status"
    echo "================================================================"
    
    # Phase 1: Core Recorder (per-channel processes)
    CORE_COUNT=$(pgrep -f "hf_timestd.core.channel_recorder" 2>/dev/null | wc -l)
    if [ "$CORE_COUNT" -gt 0 ]; then
        echo "✅ Phase 1 (Core):    RUNNING ($CORE_COUNT channel processes)"
    else
        echo "⭕ Phase 1 (Core):    STOPPED"
    fi
    
    # Phase 2: Analytics
    ANALYTICS_COUNT=$(pgrep -f "hf_timestd.core.phase2_analytics_service" 2>/dev/null | wc -l)
    if [ "$ANALYTICS_COUNT" -gt 0 ]; then
        echo "✅ Phase 2 (Analytics): RUNNING ($ANALYTICS_COUNT/9 channels)"
    else
        echo "⭕ Phase 2 (Analytics): STOPPED"
    fi
    
    # Web-UI
    WEBUI_COUNT=$(pgrep -f "monitoring-server" 2>/dev/null | wc -l)
    if [ "$WEBUI_COUNT" -gt 0 ]; then
        echo "✅ Web-UI:            RUNNING → http://localhost:3000/"
    else
        echo "⭕ Web-UI:            STOPPED"
    fi
    
    echo ""
    echo "📁 Data Structure:"
    echo "   $DATA_ROOT/"
    echo "   ├── raw_buffer/      Phase 1: 20 kHz binary IQ"
    echo "   ├── phase2/          Phase 2: Timing analysis, D_clock"
    echo "   └── logs/            Service logs"
    
    # Show disk usage if data exists
    if [ -d "$DATA_ROOT/raw_buffer" ]; then
        RAW_SIZE=$(du -sh "$DATA_ROOT/raw_buffer" 2>/dev/null | cut -f1)
        echo ""
        echo "💾 Storage: raw_buffer=$RAW_SIZE"
    fi
    ;;
esac
