#!/bin/bash
# Cleanup old decimation products for re-processing with new algorithm
# Removes decimated 10 Hz NPZ files from November 12-13 forward
# These will be regenerated with the optimized 3-stage decimation pipeline

set -e

# Determine data root from config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_ROOT}/config/timestd-config.toml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Config file not found: $CONFIG_FILE"
    exit 1
fi

# Extract data root based on mode
MODE=$(grep '^mode = ' "$CONFIG_FILE" | sed 's/.*"\(.*\)".*/\1/')
if [ "$MODE" = "production" ]; then
    DATA_ROOT=$(grep '^production_data_root = ' "$CONFIG_FILE" | sed 's/.*"\(.*\)".*/\1/')
else
    DATA_ROOT=$(grep '^test_data_root = ' "$CONFIG_FILE" | sed 's/.*"\(.*\)".*/\1/')
fi

echo "🔧 GRAPE Decimation Cleanup Utility"
echo "=================================="
echo "Mode: $MODE"
echo "Data root: $DATA_ROOT"
echo ""

# Target date: November 12, 2024 (20241112) and forward
CUTOFF_DATE="20241112"

# Directories to clean
ANALYTICS_DIR="$DATA_ROOT/analytics"
SPECTROGRAMS_DIR="$DATA_ROOT/spectrograms"

if [ ! -d "$ANALYTICS_DIR" ]; then
    echo "⚠️  Analytics directory not found: $ANALYTICS_DIR"
    echo "Nothing to clean."
    exit 0
fi

echo "📋 Scanning for decimation products to remove..."
echo "Cutoff date: $CUTOFF_DATE (files >= this date will be removed)"
echo ""

# Count files before cleanup
total_decimated=0
total_spectrograms=0
total_carrier_archives=0

# Scan decimated NPZ files
for channel_dir in "$ANALYTICS_DIR"/*; do
    if [ -d "$channel_dir" ]; then
        decimated_dir="$channel_dir/decimated"
        if [ -d "$decimated_dir" ]; then
            count=$(find "$decimated_dir" -name "*_iq_10hz.npz" -type f 2>/dev/null | wc -l)
            total_decimated=$((total_decimated + count))
        fi
    fi
done

# Scan spectrograms
if [ -d "$SPECTROGRAMS_DIR" ]; then
    for date_dir in "$SPECTROGRAMS_DIR"/*; do
        if [ -d "$date_dir" ]; then
            date=$(basename "$date_dir")
            # Check if date >= cutoff
            if [ "$date" -ge "$CUTOFF_DATE" ] 2>/dev/null; then
                count=$(find "$date_dir" -name "*_spectrogram.png" -type f 2>/dev/null | wc -l)
                total_spectrograms=$((total_spectrograms + count))
            fi
        fi
    done
fi

# Scan for carrier channel archives (200 Hz channels to be removed)
ARCHIVES_DIR="$DATA_ROOT/archives"
if [ -d "$ARCHIVES_DIR" ]; then
    for channel_dir in "$ARCHIVES_DIR"/*_carrier; do
        if [ -d "$channel_dir" ]; then
            count=$(find "$channel_dir" -name "*_iq.npz" -type f 2>/dev/null | wc -l)
            total_carrier_archives=$((total_carrier_archives + count))
        fi
    done
fi

echo "📊 Summary of files to be removed:"
echo "  - Decimated NPZ (10 Hz): $total_decimated files"
echo "  - Spectrograms (>=$CUTOFF_DATE): $total_spectrograms files"
echo "  - Carrier channel archives: $total_carrier_archives files"
echo ""

# Calculate approximate space
space_estimate=$((total_decimated * 10 + total_spectrograms * 500 + total_carrier_archives * 100))  # KB estimate
space_mb=$((space_estimate / 1024))

echo "💾 Estimated disk space to free: ~${space_mb} MB"
echo ""

# Confirmation prompt
read -p "⚠️  Proceed with cleanup? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Cleanup cancelled."
    exit 0
fi

echo ""
echo "🗑️  Starting cleanup..."
echo ""

# Remove decimated NPZ files
removed_decimated=0
for channel_dir in "$ANALYTICS_DIR"/*; do
    if [ -d "$channel_dir" ]; then
        channel_name=$(basename "$channel_dir")
        decimated_dir="$channel_dir/decimated"
        
        if [ -d "$decimated_dir" ]; then
            echo "  Cleaning $channel_name/decimated/"
            count=$(find "$decimated_dir" -name "*_iq_10hz.npz" -type f -delete -print 2>/dev/null | wc -l)
            removed_decimated=$((removed_decimated + count))
        fi
    fi
done

echo "  ✅ Removed $removed_decimated decimated NPZ files"

# Remove spectrograms from cutoff date forward
removed_spectrograms=0
if [ -d "$SPECTROGRAMS_DIR" ]; then
    for date_dir in "$SPECTROGRAMS_DIR"/*; do
        if [ -d "$date_dir" ]; then
            date=$(basename "$date_dir")
            
            # Check if date >= cutoff
            if [ "$date" -ge "$CUTOFF_DATE" ] 2>/dev/null; then
                echo "  Cleaning spectrograms/$date/"
                count=$(find "$date_dir" -name "*_spectrogram.png" -type f -delete -print 2>/dev/null | wc -l)
                removed_spectrograms=$((removed_spectrograms + count))
                
                # Remove empty date directory
                rmdir "$date_dir" 2>/dev/null || true
            fi
        fi
    done
fi

echo "  ✅ Removed $removed_spectrograms spectrograms"

# Remove carrier channel archives
removed_carrier_archives=0
if [ -d "$ARCHIVES_DIR" ]; then
    for channel_dir in "$ARCHIVES_DIR"/*_carrier; do
        if [ -d "$channel_dir" ]; then
            channel_name=$(basename "$channel_dir")
            echo "  Removing carrier channel: $channel_name"
            count=$(find "$channel_dir" -name "*_iq.npz" -type f 2>/dev/null | wc -l)
            rm -rf "$channel_dir"
            removed_carrier_archives=$((removed_carrier_archives + count))
        fi
    done
fi

if [ $removed_carrier_archives -gt 0 ]; then
    echo "  ✅ Removed $removed_carrier_archives carrier channel archive files"
fi

# Remove carrier channel analytics directories
if [ -d "$ANALYTICS_DIR" ]; then
    for channel_dir in "$ANALYTICS_DIR"/*_carrier; do
        if [ -d "$channel_dir" ]; then
            channel_name=$(basename "$channel_dir")
            echo "  Removing carrier analytics: $channel_name"
            rm -rf "$channel_dir"
        fi
    done
fi

# Remove carrier channel state files
STATE_DIR="$DATA_ROOT/state"
if [ -d "$STATE_DIR" ]; then
    removed_state=$(find "$STATE_DIR" -name "analytics-*carrier*.json" -type f -delete -print 2>/dev/null | wc -l)
    if [ $removed_state -gt 0 ]; then
        echo "  ✅ Removed $removed_state carrier state files"
    fi
fi

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "📋 Next steps:"
echo "  1. Restart analytics service to regenerate decimated NPZ with new algorithm:"
echo "     sudo systemctl restart grape-analytics-service"
echo ""
echo "  2. Monitor regeneration progress:"
echo "     journalctl -u grape-analytics-service -f"
echo ""
echo "  3. Generate new spectrograms after decimation completes:"
echo "     ./scripts/generate_spectrograms_drf.py --all"
echo ""
echo "  4. Verify new decimation quality:"
echo "     - Check spectrograms for smooth frequency variations (no artifacts)"
echo "     - Confirm timing_quality = 'TONE_LOCKED' in metadata"
echo "     - Verify passband flatness with test tones"
echo ""
