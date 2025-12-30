#!/bin/bash
# Health check for fusion: Verify Chrony SHM is being updated

set -e

# Check if Chrony is receiving updates from TMGR source
CHRONY_OUTPUT=$(chronyc sources 2>/dev/null | grep TMGR || true)

if [ -z "$CHRONY_OUTPUT" ]; then
    echo "WARNING: TMGR source not found in Chrony sources"
    exit 0  # Don't fail - may be starting up
fi

# Extract reachability (should be non-zero if receiving updates)
REACH=$(echo "$CHRONY_OUTPUT" | awk '{print $4}')

if [ "$REACH" = "0" ]; then
    echo "WARNING: TMGR source has zero reachability"
    exit 1
fi

echo "OK: Fusion feeding Chrony (reachability: $REACH)"
exit 0
