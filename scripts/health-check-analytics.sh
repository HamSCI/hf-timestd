#!/bin/bash
# Health check for analytics: Verify HDF5 files are being updated

set -e

DATA_ROOT="/var/lib/timestd"
MAX_AGE_SECONDS=300  # Alert if no updates in 5 minutes

# Find most recent HDF5 file in phase2
LATEST_FILE=$(find "$DATA_ROOT/phase2" -name "*.h5" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$LATEST_FILE" ]; then
    echo "WARNING: No HDF5 files found in $DATA_ROOT/phase2 (may be starting up)"
    exit 0  # Don't fail on startup
fi

# Check file age
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest HDF5 file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    exit 1
fi

echo "OK: Analytics processing data (latest HDF5: $FILE_AGE seconds old)"
exit 0
