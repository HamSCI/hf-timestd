#!/bin/bash
# One-time cleanup of orphaned data from path configuration bug
# This script deletes the nested /var/lib/timestd/raw_buffer/raw_buffer/ directory

set -e

DATA_ROOT="${1:-/var/lib/timestd}"
ORPHANED_DIR="$DATA_ROOT/raw_buffer/raw_buffer"

echo "=========================================="
echo "HF-TimeStd Orphaned Data Cleanup"
echo "=========================================="
echo "Data root: $DATA_ROOT"
echo "Orphaned directory: $ORPHANED_DIR"
echo ""

# Check if directory exists
if [ ! -d "$ORPHANED_DIR" ]; then
    echo "✓ Orphaned directory does not exist - nothing to clean"
    exit 0
fi

# Calculate size
SIZE=$(du -sh "$ORPHANED_DIR" | cut -f1)
echo "Orphaned data size: $SIZE"
echo ""

# Safety check: ensure no processes have files open
OPEN_FILES=$(lsof +D "$ORPHANED_DIR" 2>/dev/null | wc -l)
if [ "$OPEN_FILES" -gt 0 ]; then
    echo "❌ ERROR: $OPEN_FILES files are currently open in $ORPHANED_DIR"
    echo "Cannot safely delete. Stop services first."
    exit 1
fi

# Confirm deletion
echo "This will permanently delete all data in:"
echo "  $ORPHANED_DIR"
echo ""
read -p "Continue? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

# Delete
echo ""
echo "Deleting orphaned data..."
rm -rf "$ORPHANED_DIR"

echo "✓ Deleted $SIZE of orphaned data"
echo ""
echo "Verifying..."
if [ -d "$ORPHANED_DIR" ]; then
    echo "❌ ERROR: Directory still exists"
    exit 1
else
    echo "✓ Cleanup successful"
fi
