#!/bin/bash
# Move HDF5 timing measurement files from legacy clock_offset dir to channel root

PHASE2_DIR="/var/lib/timestd/phase2"

echo "Stopping Analytics Service..."
sudo systemctl stop timestd-analytics

echo "Scanning for misplaced HDF5 files..."
for channel_dir in "$PHASE2_DIR"/*; do
    if [ -d "$channel_dir" ]; then
        misplaced_file=$(find "$channel_dir/clock_offset" -name "*_timing_measurements_*.h5" 2>/dev/null)
        if [ -n "$misplaced_file" ]; then
            echo "Found misplaced files in $channel_dir/clock_offset/"
            sudo mv "$channel_dir/clock_offset"/*_timing_measurements_*.h5 "$channel_dir/"
            echo "  -> Moved to $channel_dir/"
        fi
    fi
done

echo "Restarting Analytics Service..."
sudo systemctl start timestd-analytics

echo "Restarting Fusion Service..."
sudo systemctl restart timestd-fusion

echo "Done."
