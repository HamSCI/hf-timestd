#!/bin/bash
#
# Daily IONEX Download Script
# Downloads latest IONEX files from IGS for global VTEC maps
#
# Schedule with cron:
#   0 2 * * * /opt/hf-timestd/scripts/download_ionex_daily.sh
#
# Author: HF Time Standard Team
# Date: 2025-12-31

set -euo pipefail

# Configuration
IONEX_DIR="/var/lib/timestd/ionex"
LOG_FILE="/var/log/timestd/ionex_download.log"
PYTHON_VENV="/opt/hf-timestd/venv/bin/python"
IONEX_SCRIPT="/opt/hf-timestd/scripts/ionex_integration.py"

# Ensure directories exist
mkdir -p "$IONEX_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "Starting IONEX download"

# Download IONEX for yesterday (most recent complete day)
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)
log "Downloading IONEX for $YESTERDAY"

# Run Python download script
if "$PYTHON_VENV" "$IONEX_SCRIPT" "$YESTERDAY" --output-dir "$IONEX_DIR" >> "$LOG_FILE" 2>&1; then
    log "✓ IONEX download successful for $YESTERDAY"
else
    log "✗ IONEX download failed for $YESTERDAY"
    exit 1
fi

# Clean up old IONEX files (keep last 7 days)
log "Cleaning up old IONEX files (keeping last 7 days)"
find "$IONEX_DIR" -name "*.Z" -mtime +7 -delete
find "$IONEX_DIR" -name "*.i" -mtime +7 -delete

log "IONEX download complete"
