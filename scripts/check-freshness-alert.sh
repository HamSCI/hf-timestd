#!/bin/bash
# External data freshness monitor with alerting
# Run via cron every 5 minutes: */5 * * * * root /opt/hf-timestd/scripts/check-freshness-alert.sh
#
# This provides an independent check outside of the service processes themselves,
# catching failures that internal watchdogs might miss.

set -e

# Configuration
DATA_ROOT="/var/lib/timestd"
HOT_BUFFER="/dev/shm/timestd/raw_buffer"
MAX_STALE_SECONDS=600  # 10 minutes - alert if data older than this
ALERT_FILE="/var/lib/timestd/state/freshness_alert_sent"
ALERT_COOLDOWN=3600  # Only send one alert per hour to avoid spam

# Email configuration (optional - set ALERT_EMAIL to enable)
ALERT_EMAIL="${TIMESTD_ALERT_EMAIL:-}"  # Set via environment or /etc/default/timestd

# Log file for this monitor
LOG_FILE="/var/log/hf-timestd/freshness-monitor.log"

log() {
    echo "$(date -Iseconds) $1" >> "$LOG_FILE" 2>/dev/null || echo "$(date -Iseconds) $1"
}

send_alert() {
    local subject="$1"
    local body="$2"
    
    # Check cooldown
    if [ -f "$ALERT_FILE" ]; then
        last_alert=$(stat -c %Y "$ALERT_FILE" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ $((now - last_alert)) -lt $ALERT_COOLDOWN ]; then
            log "Alert suppressed (cooldown active, last alert $((now - last_alert))s ago)"
            return
        fi
    fi
    
    # Update alert timestamp
    mkdir -p "$(dirname "$ALERT_FILE")"
    touch "$ALERT_FILE"
    
    # Log the alert
    log "ALERT: $subject"
    log "  $body"
    
    # Send email if configured
    if [ -n "$ALERT_EMAIL" ]; then
        echo "$body" | mail -s "[hf-timestd] $subject" "$ALERT_EMAIL" 2>/dev/null || \
            log "Failed to send email alert to $ALERT_EMAIL"
    fi
    
    # Write to system journal for visibility
    logger -t hf-timestd-alert -p user.crit "$subject: $body"
}

clear_alert() {
    if [ -f "$ALERT_FILE" ]; then
        rm -f "$ALERT_FILE"
        log "Alert cleared - data freshness restored"
    fi
}

# Determine which buffer to check
if [ -d "$HOT_BUFFER" ]; then
    SEARCH_PATH="$HOT_BUFFER"
else
    SEARCH_PATH="$DATA_ROOT/raw_buffer"
fi

if [ ! -d "$SEARCH_PATH" ]; then
    log "ERROR: Buffer directory not found: $SEARCH_PATH"
    exit 1
fi

# Find most recent .bin file across all channels
TODAY=$(date -u +%Y%m%d)
YESTERDAY=$(date -u -d "yesterday" +%Y%m%d 2>/dev/null || date -u -v-1d +%Y%m%d)

LATEST_FILE=$(find "$SEARCH_PATH" \( -path "*/$TODAY/*.bin*" -o -path "*/$YESTERDAY/*.bin*" \) \
    -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$LATEST_FILE" ]; then
    send_alert "No raw buffer files found" \
        "No .bin files found in $SEARCH_PATH for $TODAY or $YESTERDAY. Pipeline may be completely down."
    exit 1
fi

# Check file age
FILE_MTIME=$(stat -c %Y "$LATEST_FILE")
NOW=$(date +%s)
FILE_AGE=$((NOW - FILE_MTIME))

if [ "$FILE_AGE" -gt "$MAX_STALE_SECONDS" ]; then
    # Get service status for context
    CORE_STATUS=$(systemctl is-active timestd-core-recorder 2>/dev/null || echo "unknown")
    FUSION_STATUS=$(systemctl is-active timestd-fusion 2>/dev/null || echo "unknown")
    
    send_alert "Data freshness critical - ${FILE_AGE}s stale" \
        "Latest raw buffer file is ${FILE_AGE}s old ($(echo "scale=1; $FILE_AGE/60" | bc) min).
File: $LATEST_FILE
Core recorder: $CORE_STATUS
Fusion: $FUSION_STATUS
Threshold: ${MAX_STALE_SECONDS}s

Possible causes:
- RTP clock drift from radiod
- Disk full or permissions issue
- Network connectivity to radiod
- Service crash without restart

Check: sudo journalctl -u timestd-core-recorder -n 50"
    exit 1
else
    # Data is fresh - clear any previous alert
    clear_alert
    log "OK: Data fresh (${FILE_AGE}s old, threshold ${MAX_STALE_SECONDS}s)"
fi

exit 0
