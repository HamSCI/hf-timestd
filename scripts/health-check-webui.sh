#!/bin/bash
# Health check for web-ui: Verify HTTP endpoint is responding

set -e

WEB_UI_PORT=8080
MAX_RETRIES=5
RETRY_DELAY=2

for i in $(seq 1 $MAX_RETRIES); do
    if curl -f -s -o /dev/null "http://localhost:$WEB_UI_PORT/api/v2/system/health-summary"; then
        echo "OK: Web UI responding on port $WEB_UI_PORT"
        exit 0
    fi
    
    if [ $i -lt $MAX_RETRIES ]; then
        echo "Retry $i/$MAX_RETRIES: Web UI not responding, waiting ${RETRY_DELAY}s..."
        sleep $RETRY_DELAY
    fi
done

echo "ERROR: Web UI failed to respond after $MAX_RETRIES attempts"
exit 1
