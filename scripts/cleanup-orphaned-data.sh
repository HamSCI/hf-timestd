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

# Safety check: ensure no processes have files open.
#
# This guard is the only thing between this script and deleting data a service
# is still writing.  `lsof ... | wc -l` yields 0 both when nothing is open and
# when lsof is not installed -- and `set -e` does not catch it, because wc
# succeeds either way.  On B4 2026-08-07 (no lsof) it reported 0 open files
# while a recorder was actively writing to the directory under test.  A
# destructive operation must not proceed on a precondition it could not check.
if ! command -v lsof >/dev/null 2>&1; then
    echo "❌ ERROR: lsof is not installed, so the open-file safety check cannot run."
    echo "Refusing to delete $ORPHANED_DIR on an unverified precondition."
    echo "Install it (apt install lsof) and re-run."
    exit 1
fi
# Installing lsof is not enough: unprivileged, it cannot see file handles held
# by OTHER users, and every service here runs as its own user (timestd,
# magrec, ...).  Run as a normal operator the check returns 0 open files while
# a service is actively writing -- measured on B4 2026-08-07: 0 as the login
# user, 2 as root, for the same directory.  That is the same fail-open the
# lsof check above guards against, so require the privilege that makes the
# answer meaningful.
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ ERROR: must run as root."
    echo "Unprivileged, lsof cannot see other users' open files, so the safety"
    echo "check would report 0 even while a service is writing to $ORPHANED_DIR."
    echo "Re-run with sudo."
    exit 1
fi
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
