#!/bin/bash
#
# Daily IONEX Download Script
# Run via systemd timer (timestd-ionex-download.timer)
# Output goes to systemd journal via StandardOutput=journal
#

set -euo pipefail

# Configuration
IONEX_DIR="/var/lib/timestd/ionex"
VENV_PYTHON="/opt/hf-timestd/venv/bin/python3"
SCRIPT_DIR="/opt/hf-timestd/scripts"
IONEX_SCRIPT="$SCRIPT_DIR/ionex_integration.py"

# Ensure IONEX directory exists
mkdir -p "$IONEX_DIR"

echo "Starting daily IONEX download..."

# Check script exists
if [ ! -f "$IONEX_SCRIPT" ]; then
    echo "ERROR: Script not found at $IONEX_SCRIPT"
    exit 1
fi

# Calculate yesterday's date
# Strategy: Try Final first (most accurate), fall back to Rapid (~1 day latency)
# With Rapid fallback, we only need to search back ~7 days
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)
"$VENV_PYTHON" "$IONEX_SCRIPT" "$YESTERDAY" --output-dir "$IONEX_DIR" --max-days-back 7 && DOWNLOAD_STATUS=0 || DOWNLOAD_STATUS=$?

if [ $DOWNLOAD_STATUS -eq 0 ]; then
    echo "IONEX download successful"
    
    # Clean up old files (>30 days) to save space
    # Modern files are .gz, legacy are .Z
    find "$IONEX_DIR" -type f -name "*.gz" -mtime +30 -delete
    find "$IONEX_DIR" -type f -name "*.Z" -mtime +30 -delete
    echo "Cleaned up old files"
else
    echo "IONEX download failed"
    exit 1
fi

echo "Done."
