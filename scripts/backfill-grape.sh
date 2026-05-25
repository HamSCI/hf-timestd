#!/bin/bash
# Backfill GRAPE pipeline: decimate (if needed) + package + upload
# Run as: sudo -u timestd bash scripts/backfill-grape.sh [--config /path/to/config.toml]
set -e

CONFIG="/etc/hf-timestd/timestd-config.toml"
if [[ "$1" == "--config" && -n "$2" ]]; then
    CONFIG="$2"; shift 2
fi

PYTHON="/opt/hf-timestd/venv/bin/python3"
CLI="hf_timestd.cli"
DATA_ROOT="/var/lib/timestd"

# Read station info from config
eval "$($PYTHON -c "
import tomllib, sys
try:
    with open('$CONFIG', 'rb') as f:
        cfg = tomllib.load(f)
    s = cfg.get('station', {})
    r = cfg.get('recorder', {}).get('channel_group', {}).get('timestd', {})
    n = len(r.get('channels', []))
    print(f'CALLSIGN={s.get(\"callsign\", \"\")}')
    print(f'GRID={s.get(\"grid_square\", \"\")[:6]}')
    print(f'STATION_ID={s.get(\"id\", \"\")}')
    print(f'NUM_CHANNELS={n}')
except Exception as e:
    print(f'echo \"ERROR: cannot read config: {e}\"; exit 1', file=sys.stderr)
    sys.exit(1)
" 2>&1)"

if [[ -z "$CALLSIGN" || -z "$GRID" ]]; then
    echo "ERROR: callsign or grid_square not set in $CONFIG"
    exit 1
fi

# Dates to force-decimate (leave empty for normal operation)
DECIMATE_DATES="${DECIMATE_DATES:-}"

echo "=========================================="
echo "GRAPE Backfill — $(date -u)"
echo "=========================================="

# Phase 1: Decimate incomplete dates
echo ""
echo "=== Phase 1: Decimation ==="
for date in $DECIMATE_DATES; do
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    if [ "$dec_count" -eq "$NUM_CHANNELS" ]; then
        echo "[$date] Already has $NUM_CHANNELS/$NUM_CHANNELS channels decimated, skipping"
        continue
    fi
    echo "[$date] Decimating all channels ($dec_count/$NUM_CHANNELS currently)..."
    $PYTHON -m $CLI grape decimate --all-channels --date "$date" 2>&1 | grep -E '(Completed|ERROR|minutes)' | tail -"$NUM_CHANNELS"
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    echo "[$date] Done: $dec_count/$NUM_CHANNELS channels"
done

# Phase 2: Package + Upload all dates with 9/9 decimated channels
echo ""
echo "=== Phase 2: Package + Upload ==="
# Discover all dates with 9/9 decimated channels
ALL_DATES=$(ls "$DATA_ROOT"/products/CHU_3330/decimated/*.bin 2>/dev/null | sed 's|.*/||;s|\.bin||' | sort)

for date in $ALL_DATES; do
    # Check if already uploaded
    if find "$DATA_ROOT/upload/${date}" -name ".upload_complete" 2>/dev/null | grep -q .; then
        echo "[$date] Already uploaded, skipping"
        continue
    fi

    # Verify all channels decimated
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    if [ "$dec_count" -lt "$NUM_CHANNELS" ]; then
        echo "[$date] SKIP — only $dec_count/$NUM_CHANNELS channels decimated"
        continue
    fi

    # Package
    echo -n "[$date] Package..."
    $PYTHON -m $CLI grape package --date "$date" --callsign "$CALLSIGN" --grid "$GRID" 2>&1 | grep -oE '(Package complete|ERROR|[0-9.]+% complete)' | tail -1
    
    # Upload
    echo -n "[$date] Upload..."
    $PYTHON -m $CLI grape upload --date "$date" 2>&1 | grep -oE '(Upload successful|Queue status.*|ERROR|failed)' | tail -1

    echo "[$date] Done"
done

echo ""
echo "=========================================="
echo "Backfill complete — $(date -u)"
echo "=========================================="

# Summary
uploaded=$(find "$DATA_ROOT/upload" -name ".upload_complete" 2>/dev/null | wc -l)
echo "Total datasets uploaded: $uploaded"
