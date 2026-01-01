#!/bin/bash
# Health check for core-recorder: Verify raw data is being written

set -e

DATA_ROOT="/var/lib/timestd"
MAX_AGE_SECONDS=120  # Alert if no new files in 2 minutes

# Find most recent .bin file across all channels IN TODAY'S DIRECTORY ONLY
# Retry loop for cold start (service takes up to 120s to write first chunk if starting just after minute boundary)
MAX_RETRIES=40  # 40 * 5s = 200s wait
TODAY=$(date +%Y%m%d)

for i in $(seq 1 $MAX_RETRIES); do
    # Search only in today's directories to avoid finding old files from previous days
    LATEST_FILE=$(find "$DATA_ROOT/raw_buffer" -path "*/$TODAY/*.bin" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    
    
    if [ -n "$LATEST_FILE" ]; then
        # Check file age immediately
        FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))
        
        if [ "$FILE_AGE" -le "$MAX_AGE_SECONDS" ]; then
            echo "Found recent file: $LATEST_FILE ($FILE_AGE sec old)"
            break
        else
            echo "Found only old file: $LATEST_FILE ($FILE_AGE sec old). Waiting for new data... ($i/$MAX_RETRIES)"
        fi
    else
        # If we are here, no files found. Wait and retry.
        echo "Waiting for first file in $TODAY directory... ($i/$MAX_RETRIES)"
    fi
    
    sleep 5
done

if [ -z "$LATEST_FILE" ]; then
    echo "ERROR: No raw buffer files found in $DATA_ROOT/raw_buffer after $((MAX_RETRIES * 5)) seconds"
    exit 1
fi

# Re-verify age (redundant but safe)
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest raw buffer file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    # Even if stale, we found ONE. If we timed out waiting for *better*, maybe just warn?
    # But for a health check, stale data = broken recorder.
    exit 1
fi

echo "OK: Core recorder writing data (latest file: $FILE_AGE seconds old)"
exit 0
