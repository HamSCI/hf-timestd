#!/bin/bash
# Email notification script for service failures
# Usage: service-alert.sh <service-name>

SERVICE="$1"
HOSTNAME=$(hostname)
EMAIL_TO="${TIMESTD_ALERT_EMAIL:-root@localhost}"
EMAIL_FROM="timestd-monitor@${HOSTNAME}"

# Check if mail command is available
if ! command -v mail &> /dev/null; then
    echo "WARNING: 'mail' command not found. Install mailutils or configure email."
    logger -t timestd-alert "Service $SERVICE failed but email notification unavailable"
    exit 0
fi

# Get recent logs
LOGS=$(journalctl -u "$SERVICE" -n 50 --no-pager 2>&1)

# Get service status
STATUS=$(systemctl status "$SERVICE" --no-pager 2>&1)

# Compose email
SUBJECT="[ALERT] HF-TimeStd Service Failure: $SERVICE on $HOSTNAME"

BODY="Service $SERVICE has failed on $HOSTNAME at $(date)

SERVICE STATUS:
$STATUS

RECENT LOGS (last 50 lines):
$LOGS

---
This is an automated alert from hf-timestd service monitoring.
To configure email recipient, set TIMESTD_ALERT_EMAIL environment variable.
"

# Send email
echo "$BODY" | mail -s "$SUBJECT" "$EMAIL_TO"

# Also log to syslog
logger -t timestd-alert "Service $SERVICE failed - email sent to $EMAIL_TO"

echo "Alert email sent to $EMAIL_TO for service $SERVICE"
