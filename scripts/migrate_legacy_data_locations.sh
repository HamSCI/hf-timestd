#!/bin/bash
#
# Migrate Legacy Data Files to Standardized Subdirectories
#
# This script moves HDF5 files from channel root directories to their
# correct subdirectories as defined by the DataProductRegistry.
#
# Safe to run multiple times - only moves files that are in wrong location.
#

set -e

DATA_ROOT="${1:-/var/lib/timestd/phase2}"

echo "=========================================="
echo "Data Location Migration Script"
echo "=========================================="
echo "Data root: $DATA_ROOT"
echo ""

if [ ! -d "$DATA_ROOT" ]; then
    echo "ERROR: Data root directory does not exist: $DATA_ROOT"
    exit 1
fi

# Track statistics
TOTAL_MOVED=0
TOTAL_CHANNELS=0

# Process each channel directory
for channel_dir in "$DATA_ROOT"/*/; do
    channel=$(basename "$channel_dir")
    
    # Skip non-channel directories
    if [[ "$channel" == "fusion" || "$channel" == "science" || "$channel" == "phase2" ]]; then
        continue
    fi
    
    TOTAL_CHANNELS=$((TOTAL_CHANNELS + 1))
    CHANNEL_MOVED=0
    
    echo "Processing channel: $channel"
    
    # Move L2 timing measurements to clock_offset/
    if ls "${channel_dir}"*_timing_measurements_*.h5 2>/dev/null | grep -q .; then
        echo "  → Moving timing measurements to clock_offset/"
        mkdir -p "${channel_dir}clock_offset/"
        
        for file in "${channel_dir}"*_timing_measurements_*.h5; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                dest="${channel_dir}clock_offset/$filename"
                
                if [ ! -f "$dest" ]; then
                    mv "$file" "$dest"
                    echo "    Moved: $filename"
                    CHANNEL_MOVED=$((CHANNEL_MOVED + 1))
                else
                    echo "    Skipped (exists): $filename"
                fi
            fi
        done
    fi
    
    # Move L1 channel observables to carrier_power/
    if ls "${channel_dir}"*_channel_observables_*.h5 2>/dev/null | grep -q .; then
        echo "  → Moving channel observables to carrier_power/"
        mkdir -p "${channel_dir}carrier_power/"
        
        for file in "${channel_dir}"*_channel_observables_*.h5; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                dest="${channel_dir}carrier_power/$filename"
                
                if [ ! -f "$dest" ]; then
                    mv "$file" "$dest"
                    echo "    Moved: $filename"
                    CHANNEL_MOVED=$((CHANNEL_MOVED + 1))
                else
                    echo "    Skipped (exists): $filename"
                fi
            fi
        done
    fi
    
    # Move L1 tone detections to tone_detections/
    if ls "${channel_dir}"*_tone_detections_*.h5 2>/dev/null | grep -q .; then
        echo "  → Moving tone detections to tone_detections/"
        mkdir -p "${channel_dir}tone_detections/"
        
        for file in "${channel_dir}"*_tone_detections_*.h5; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                dest="${channel_dir}tone_detections/$filename"
                
                if [ ! -f "$dest" ]; then
                    mv "$file" "$dest"
                    echo "    Moved: $filename"
                    CHANNEL_MOVED=$((CHANNEL_MOVED + 1))
                else
                    echo "    Skipped (exists): $filename"
                fi
            fi
        done
    fi
    
    # Move L1 BCD timecode to bcd_discrimination/
    if ls "${channel_dir}"*_bcd_timecode_*.h5 2>/dev/null | grep -q .; then
        echo "  → Moving BCD timecode to bcd_discrimination/"
        mkdir -p "${channel_dir}bcd_discrimination/"
        
        for file in "${channel_dir}"*_bcd_timecode_*.h5; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                dest="${channel_dir}bcd_discrimination/$filename"
                
                if [ ! -f "$dest" ]; then
                    mv "$file" "$dest"
                    echo "    Moved: $filename"
                    CHANNEL_MOVED=$((CHANNEL_MOVED + 1))
                else
                    echo "    Skipped (exists): $filename"
                fi
            fi
        done
    fi
    
    # Move L2 test signals to test_signal/
    if ls "${channel_dir}"*_test_signal_*.h5 2>/dev/null | grep -q .; then
        echo "  → Moving test signals to test_signal/"
        mkdir -p "${channel_dir}test_signal/"
        
        for file in "${channel_dir}"*_test_signal_*.h5; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                dest="${channel_dir}test_signal/$filename"
                
                if [ ! -f "$dest" ]; then
                    mv "$file" "$dest"
                    echo "    Moved: $filename"
                    CHANNEL_MOVED=$((CHANNEL_MOVED + 1))
                else
                    echo "    Skipped (exists): $filename"
                fi
            fi
        done
    fi
    
    if [ $CHANNEL_MOVED -gt 0 ]; then
        echo "  ✓ Moved $CHANNEL_MOVED files"
        TOTAL_MOVED=$((TOTAL_MOVED + CHANNEL_MOVED))
    else
        echo "  ✓ No files to move"
    fi
    echo ""
done

echo "=========================================="
echo "Migration Complete"
echo "=========================================="
echo "Channels processed: $TOTAL_CHANNELS"
echo "Total files moved: $TOTAL_MOVED"
echo ""

if [ $TOTAL_MOVED -gt 0 ]; then
    echo "✓ Migration successful"
    echo ""
    echo "Next steps:"
    echo "  1. Verify data is accessible via web-api"
    echo "  2. Test all API endpoints"
    echo "  3. Monitor logs for any issues"
else
    echo "✓ No migration needed - all files already in correct locations"
fi
