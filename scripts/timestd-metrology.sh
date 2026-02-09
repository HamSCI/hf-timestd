#!/bin/bash
# HF Time Standard Metrology Services Control (all 9 channels)
#
# Phase 1 processes raw_buffer data to produce:
#   - L1 Metrology Measurements (HDF5)
#   - Station discrimination
#   - Quality metrics and tone detections
#
# Input:  raw_buffer/{CHANNEL}/ (20 kHz binary IQ from Phase 1)
# Output: phase2/{CHANNEL}/      (timing analysis, clock offset CSV)
#
# Usage: timestd-analytics.sh {start|stop|restart|status} [config-file]

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
    echo "▶️  Starting Metrology Services..."
    
    # Stop existing first
    pkill -f "hf_timestd.core.metrology_service" 2>/dev/null
    sleep 1
    
    if [ ! -f "$CONFIG" ]; then
        echo "   ❌ Config not found: $CONFIG"
        exit 1
    fi
    
    CALLSIGN=$(grep '^callsign' "$CONFIG" | head -1 | cut -d'"' -f2)
    GRID=$(grep '^grid_square' "$CONFIG" | head -1 | cut -d'"' -f2)
    STATION_ID=$(grep '^id' "$CONFIG" | head -1 | cut -d'"' -f2)
    INSTRUMENT_ID=$(grep '^instrument_id' "$CONFIG" | head -1 | cut -d'"' -f2)
    
    # Precise coordinates for improved timing accuracy (~16μs improvement)
    LATITUDE=$(grep '^latitude' "$CONFIG" | head -1 | awk '{print $3}')
    LONGITUDE=$(grep '^longitude' "$CONFIG" | head -1 | awk '{print $3}')
    
    # Build coordinate args if available
    COORD_ARGS=""
    if [ -n "$LATITUDE" ] && [ -n "$LONGITUDE" ]; then
        COORD_ARGS="--latitude $LATITUDE --longitude $LONGITUDE"
        echo "   📍 Using precise coordinates: ${LATITUDE}°N, ${LONGITUDE}°W"
    fi
    
    # Check if tiered storage is enabled in config
    TIERED_STORAGE=$(grep '^tiered_storage' "$CONFIG" | head -1 | awk '{print $3}')
    TIERED_ARGS=""
    ARCHIVE_ROOT="$DATA_ROOT/raw_buffer"  # Default to cold buffer
    if [ "$TIERED_STORAGE" = "true" ]; then
        TIERED_ARGS="--use-tiered-storage"
        ARCHIVE_ROOT="/dev/shm/timestd/raw_buffer"  # Use hot buffer for tiered storage
        echo "   💾 Tiered storage enabled: reading from /dev/shm hot buffer"
    fi
    
    # Create directories
    mkdir -p "$LOG_DIR" "$DATA_ROOT/state" "$DATA_ROOT/status"
    mkdir -p "$DATA_ROOT/phase2"
    cd "$PROJECT_DIR"
    
    # Check for stale HDF5 locks (Phase 3 Robustness)
    # If the service crashed, HDF5 files might be marked as "open", preventing new writes.
    # We use h5clear to reset these flags, BUT ONLY if the file is not actually open by a process.
    if command -v h5clear &> /dev/null; then
        echo "   🔍 Scanning for stale HDF5 file locks..."
        find "$DATA_ROOT/phase2" -name "*.h5" -type f -mmin -60 2>/dev/null | while read -r h5file; do
            # Check if file is open by any process (should return non-zero if NOT open)
            if ! lsof "$h5file" >/dev/null 2>&1; then
                # File is not open, safe to clear consistency flags
                # This fixes "Unable to synchronously open file (file is already open for write)"
                h5clear -s "$h5file" >/dev/null 2>&1 || true
            fi
        done
        echo "   ✅ HDF5 consistency checks complete"
    else
        echo "   ⚠️  h5clear not found: skipping HDF5 lock recovery (install hdf5-tools)"
    fi
    
    # SHARED Channels (2.5, 5, 10, 15 MHz - WWV/WWVH/BPM all broadcast here)
    # Input: raw_buffer/SHARED_X_MHz/ (Phase 1 binary IQ)
    # Output: phase2/SHARED_X_MHz/    (D_clock, timing metrics)
    for freq_mhz in 2.5 5 10 15; do
        freq_hz=$(echo "$freq_mhz * 1000000" | bc | cut -d. -f1)
        freq_khz=$(echo "$freq_hz / 1000" | bc)
        channel_dir="SHARED_${freq_khz}"
        
        nohup taskset 0x00ff $PYTHON -m hf_timestd.core.metrology_service \
          --archive-dir "$ARCHIVE_ROOT/$channel_dir" \
          --output-dir "$DATA_ROOT/phase2/$channel_dir" \
          --channel-name "$channel_dir" \
          --frequency-hz "$freq_hz" \
          --state-file "$DATA_ROOT/state/phase2-shared${freq_mhz}.json" \
          --poll-interval 10.0 \
          --log-level INFO \
          --callsign "$CALLSIGN" --grid-square "$GRID" \
          --receiver-name "HF-TimeStd" \
          --station-id "$STATION_ID" --instrument-id "$INSTRUMENT_ID" \
          $COORD_ARGS $TIERED_ARGS \
          > "$LOG_DIR/phase2-shared${freq_mhz}.log" 2>&1 &
        
        sleep 0.2
    done
    
    # WWV-only Channels (20, 25 MHz - only WWV broadcasts here)
    # Input: raw_buffer/WWV_X_MHz/ (Phase 1 binary IQ)
    # Output: phase2/WWV_X_MHz/    (D_clock, timing metrics)
    for freq_mhz in 20 25; do
        freq_hz=$(echo "$freq_mhz * 1000000" | bc | cut -d. -f1)
        freq_khz=$(echo "$freq_hz / 1000" | bc)
        channel_dir="WWV_${freq_khz}"
        
        nohup taskset 0x00ff $PYTHON -m hf_timestd.core.metrology_service \
          --archive-dir "$ARCHIVE_ROOT/$channel_dir" \
          --output-dir "$DATA_ROOT/phase2/$channel_dir" \
          --channel-name "$channel_dir" \
          --frequency-hz "$freq_hz" \
          --state-file "$DATA_ROOT/state/phase2-wwv${freq_mhz}.json" \
          --poll-interval 10.0 \
          --log-level INFO \
          --callsign "$CALLSIGN" --grid-square "$GRID" \
          --receiver-name "HF-TimeStd" \
          --station-id "$STATION_ID" --instrument-id "$INSTRUMENT_ID" \
          $COORD_ARGS $TIERED_ARGS \
          > "$LOG_DIR/phase2-wwv${freq_mhz}.log" 2>&1 &
        
        sleep 0.2
    done
    
    # CHU Channels
    declare -A CHU_FREQS=( ["3.33"]="3330000" ["7.85"]="7850000" ["14.67"]="14670000" )
    
    for freq_mhz in 3.33 7.85 14.67; do
        freq_hz=${CHU_FREQS[$freq_mhz]}
        freq_khz=$(echo "$freq_hz / 1000" | bc)
        channel_dir="CHU_${freq_khz}"
        
        nohup taskset 0x00ff $PYTHON -m hf_timestd.core.metrology_service \
          --archive-dir "$ARCHIVE_ROOT/$channel_dir" \
          --output-dir "$DATA_ROOT/phase2/$channel_dir" \
          --channel-name "$channel_dir" \
          --frequency-hz "$freq_hz" \
          --state-file "$DATA_ROOT/state/phase2-chu${freq_mhz}.json" \
          --poll-interval 10.0 \
          --log-level INFO \
          --callsign "$CALLSIGN" --grid-square "$GRID" \
          --receiver-name "HF-TimeStd" \
          --station-id "$STATION_ID" --instrument-id "$INSTRUMENT_ID" \
          $COORD_ARGS $TIERED_ARGS \
          > "$LOG_DIR/phase2-chu${freq_mhz}.log" 2>&1 &
        
        sleep 0.2
    done
    
    sleep 2
    COUNT=$(pgrep -f "hf_timestd.core.metrology_service" 2>/dev/null | wc -l)
    echo "   ✅ Started $COUNT/9 Metrology analytics channels"
    
    echo "   📄 Logs: $LOG_DIR/phase2-*.log"
    echo "   📊 Output: $DATA_ROOT/phase2/{CHANNEL}/clock_offset/"
    ;;

stop)
    echo "🛑 Stopping Phase 2 Analytics Services..."
    
    REMAINING=$(pgrep -f "hf_timestd.core.metrology_service" 2>/dev/null | wc -l)
    if [ "$REMAINING" -gt 0 ]; then
        pkill -9 -f "hf_timestd.core.metrology_service" 2>/dev/null
    fi
    
    echo "   ✅ Stopped $COUNT Metrology services"
    ;;

restart)
    echo "🔄 Restarting Metrology Services..."
    "$0" stop "$CONFIG"
    sleep 2
    "$0" start "$CONFIG"
    ;;

status)
    COUNT=$(pgrep -f "hf_timestd.core.metrology_service" 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        echo "✅ Metrology Services: RUNNING ($COUNT/9 channels)"
        echo "   Input:  $DATA_ROOT/raw_buffer/{CHANNEL}/"
        echo "   Output: $DATA_ROOT/phase2/{CHANNEL}/clock_offset/"
    else
        echo "⭕ Metrology Services: STOPPED"
    fi
    ;;
esac
