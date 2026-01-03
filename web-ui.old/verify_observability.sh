#!/bin/bash
# Verify Observability Enhancements
set -e

# Configuration
SERVER_DIR="/home/mjh/git/hf-timestd/web-ui"
SERVER_SCRIPT="monitoring-server-v3.js"
PORT=3000
DATA_ROOT="/tmp/timestd-test"

echo "Starting verification..."

# 1. Start Server
cd "$SERVER_DIR"
echo "Stopping any existing server..."
pkill -f "$SERVER_SCRIPT" || true
sleep 2

echo "Starting server on port $PORT..."
nohup node "$SERVER_SCRIPT" > /tmp/server_output.log 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Wait for startup
sleep 5

# Check if running
if ! ps -p $SERVER_PID > /dev/null; then
    echo "Server failed to start. Check /tmp/server_output.log"
    cat /tmp/server_output.log
    exit 1
fi

echo "Server running."

# 2. Verify Broadcasts History (Exclusion Zones)
echo "Verifying /api/v1/broadcasts/history..."
RESPONSE=$(curl -s "http://localhost:$PORT/api/v1/broadcasts/history?date=20240101")
# We just check if it returns valid JSON with 'history' key
if echo "$RESPONSE" | grep -q "history"; then
    echo "✅ Broadcasts history endpoint works."
else
    echo "❌ Broadcasts history endpoint failed."
    echo "Response: $RESPONSE"
fi

# 3. Verify Fusion Transmission (Convergence)
echo "Verifying /api/v1/timing/transmission..."
RESPONSE=$(curl -s "http://localhost:$PORT/api/v1/timing/transmission?hours=24")
if echo "$RESPONSE" | grep -q "results"; then
    echo "✅ Fusion/Transmission endpoint works."
else
    echo "❌ Fusion/Transmission endpoint failed."
    echo "Response: $RESPONSE"
fi

# Cleanup
echo "Stopping server..."
kill $SERVER_PID
echo "Verification complete."
