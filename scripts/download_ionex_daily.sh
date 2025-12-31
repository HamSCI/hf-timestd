#!/bin/bash
#
# Daily IONEX Download Script
# Run via cron at 02:00 UTC
#

# Configuration
IONEX_DIR="/var/lib/timestd/ionex"
LOG_FILE="/var/log/timestd/ionex_download.log"
VENV_PYTHON="/opt/hf-timestd/venv/bin/python3"
SCRIPT_DIR="/opt/hf-timestd/scripts"
IONEX_SCRIPT="$SCRIPT_DIR/ionex_integration.py"

# Ensure directories exist
mkdir -p "$IONEX_DIR"
dirname "$LOG_FILE" | xargs mkdir -p

# Logging function
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

log "Starting daily IONEX download..."

# Check script exists
if [ ! -f "$IONEX_SCRIPT" ]; then
    log "ERROR: Script not found at $IONEX_SCRIPT"
    exit 1
fi

# Run python script
# The script now accepts 'yesterday' as a date argument
"$VENV_PYTHON" "$IONEX_SCRIPT" yesterday --output-dir "$IONEX_DIR" >> "$LOG_FILE" 2>&1

if [ $? -eq 0 ]; then
    log "✓ IONEX download successful"
    
    # Clean up old files (>30 days) to save space
    # Modern files are .gz, legacy are .Z
    find "$IONEX_DIR" -type f -name "*.gz" -mtime +30 -delete
    find "$IONEX_DIR" -type f -name "*.Z" -mtime +30 -delete
    log "Cleaned up old files"
else
    log "✗ IONEX download failed"
    exit 1
fi

log "Done."
