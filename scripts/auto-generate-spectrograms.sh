#!/bin/bash
# Auto-generate spectrograms every 15 minutes
# Uses CarrierSpectrogramGenerator (canonical implementation)
# 
# Input:  products/{CHANNEL}/decimated/ (Phase 3 decimated buffer)
# Output: products/{CHANNEL}/spectrograms/ (PNG images with solar zenith overlay)

DATA_ROOT="${1:-/tmp/timestd-test}"
INTERVAL="${2:-900}"  # 15 minutes default
CONFIG_FILE="${3:-config/timestd-config.toml}"

# Get grid square from config
GRID="EM38ww"  # Default
if [ -f "$CONFIG_FILE" ]; then
    GRID=$(grep '^grid_square' "$CONFIG_FILE" | head -1 | cut -d'"' -f2)
fi

echo "=========================================="
echo "GRAPE Auto-Spectrogram Generator"
echo "=========================================="
echo "Data root: $DATA_ROOT"
echo "Grid square: $GRID"
echo "Interval: $INTERVAL seconds ($(($INTERVAL/60)) minutes)"
echo "Started at: $(date)"
echo ""

cd "$(dirname "$0")/.." || exit 1

while true; do
    DATE=$(date -u +%Y%m%d)
    echo "[$(date -u +%H:%M:%S)] Generating spectrograms for $DATE..."
    
    # Generate ALL channel spectrograms using CarrierSpectrogramGenerator
    python3 -m grape_recorder.grape.carrier_spectrogram \
        --data-root "$DATA_ROOT" \
        --all-channels \
        --date "$DATE" \
        --grid "$GRID" \
        2>&1 | grep -E "(✅|❌|Generated|Error)" || true
    
    echo "[$(date -u +%H:%M:%S)] Done. Next run in $(($INTERVAL/60)) minutes."
    echo ""
    
    sleep "$INTERVAL"
done
