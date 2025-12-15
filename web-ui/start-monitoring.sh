#!/bin/bash
# Start hf-timestd Monitoring Server V3

cd "$(dirname "$0")"

echo "🚀 Starting hf-timestd Monitoring Server V3..."
echo ""

# Stop any existing instance
pkill -f monitoring-server 2>/dev/null
sleep 1

# Start the server (V3)
nohup node monitoring-server-v3.js > monitoring-server.log 2>&1 &

sleep 2

# Check if it started
if pgrep -f monitoring-server-v3 > /dev/null; then
    echo "✅ Server started successfully!"
    echo ""
    echo "📊 Access the dashboards:"
    echo "   http://localhost:3000/              (redirects to summary)"
    echo "   http://localhost:3000/summary.html"
    echo "   http://localhost:3000/carrier.html"
    echo "   http://localhost:3000/discrimination.html"
    echo "   http://localhost:3000/timing-dashboard-enhanced.html"
    echo ""
    echo "📝 View logs:"
    echo "   tail -f monitoring-server.log"
    echo ""
else
    echo "❌ Failed to start server"
    echo "Check monitoring-server.log for errors"
    exit 1
fi
