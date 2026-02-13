#!/bin/bash
# Backfill GRAPE pipeline: decimate (if needed) + package + upload
# Run as: sudo -u timestd bash /home/mjh/git/hf-timestd/scripts/backfill-grape.sh
set -e

PYTHON="/opt/hf-timestd/venv/bin/python3"
CLI="hf_timestd.cli"
DATA_ROOT="/var/lib/timestd"

# Only 20260211 needs decimation (other incomplete dates lack raw binary data)
DECIMATE_DATES="20260211"

echo "=========================================="
echo "GRAPE Backfill — $(date -u)"
echo "=========================================="

# Phase 1: Decimate incomplete dates
echo ""
echo "=== Phase 1: Decimation ==="
for date in $DECIMATE_DATES; do
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    if [ "$dec_count" -eq 9 ]; then
        echo "[$date] Already has 9/9 channels decimated, skipping"
        continue
    fi
    echo "[$date] Decimating all channels ($dec_count/9 currently)..."
    $PYTHON -m $CLI grape decimate --all-channels --date "$date" 2>&1 | grep -E '(Completed|ERROR|minutes)' | tail -9
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    echo "[$date] Done: $dec_count/9 channels"
done

# Phase 2: Package + Upload all dates with 9/9 decimated channels
echo ""
echo "=== Phase 2: Package + Upload ==="
# Discover all dates with 9/9 decimated channels
ALL_DATES=$(ls "$DATA_ROOT"/products/CHU_3330/decimated/*.bin 2>/dev/null | sed 's|.*/||;s|\.bin||' | sort)

for date in $ALL_DATES; do
    # Check if already uploaded
    if [ -f "$DATA_ROOT/upload/${date}/AC0G_EM38ww/GRAPE@AC0G_1_1/.upload_complete" ]; then
        echo "[$date] Already uploaded, skipping"
        continue
    fi

    # Verify 9 channels decimated
    dec_count=$(ls "$DATA_ROOT"/products/*/decimated/${date}.bin 2>/dev/null | wc -l)
    if [ "$dec_count" -lt 9 ]; then
        echo "[$date] SKIP — only $dec_count/9 channels decimated"
        continue
    fi

    # Package
    echo -n "[$date] Package..."
    $PYTHON -m $CLI grape package --date "$date" --callsign AC0G --grid EM38ww 2>&1 | grep -oE '(Package complete|ERROR|[0-9.]+% complete)' | tail -1
    
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
