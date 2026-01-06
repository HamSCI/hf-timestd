#!/bin/bash
# Move HDF5 timing measurement files BACK to clock_offset dir

PHASE2_DIR="/var/lib/timestd/phase2"

echo "Stopping Analytics Service..."
sudo systemctl stop timestd-analytics

echo "Scanning for misplaced HDF5 files (root -> clock_offset)..."
for channel_dir in "$PHASE2_DIR"/*; do
    if [ -d "$channel_dir" ]; then
        # Ensure clock_offset exists
        if [ ! -d "$channel_dir/clock_offset" ]; then
             echo "Creating $channel_dir/clock_offset"
             sudo -u timestd mkdir -p "$channel_dir/clock_offset"
        fi

        misplaced_file=$(find "$channel_dir" -maxdepth 1 -name "*_timing_measurements_*.h5" 2>/dev/null)
        if [ -n "$misplaced_file" ]; then
            echo "Found files in root of $channel_dir"
            sudo mv "$channel_dir"/*_timing_measurements_*.h5 "$channel_dir/clock_offset/"
            echo "  -> Moved back to $channel_dir/clock_offset/"
        fi
    fi
done

echo "Restarting Analytics Service..."
sudo systemctl start timestd-analytics

# Do not restart Fusion yet - detailed debug pending
