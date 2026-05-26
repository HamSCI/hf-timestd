#!/bin/bash
set -e

# Configuration
# Detect if running on localhost (bee1)
HOSTNAME=$(hostname)
REMOTE_DIR="/opt/git/sigmond/hf-timestd"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "🚀 Migrating Web UI to Web API service on $HOSTNAME..."

if [[ "$HOSTNAME" == "bee1" ]]; then
    # Local deployment mode
    echo "🏠 Running in LOCAL mode on bee1"
    
    # 1. Update web-api files in production dir
    echo "📦 Updating web-api files in $REMOTE_DIR/web-api..."
    sudo mkdir -p "$REMOTE_DIR/web-api"
    sudo cp -r "$LOCAL_DIR/web-api/"* "$REMOTE_DIR/web-api/"
    
    # 2. Install new service file
    echo "📄 Installing service file..."
    sudo cp "$LOCAL_DIR/systemd/timestd-web-api.service" "/etc/systemd/system/"
    
    # 3. Perform migration
    echo "🔄 Switching services..."
    
    # Reload systemd
    sudo systemctl daemon-reload
    
    # Stop and disable old service (if running)
    if systemctl is-active --quiet timestd-web-ui; then
        echo "🛑 Stopping old timestd-web-ui service..."
        sudo systemctl stop timestd-web-ui
        sudo systemctl disable timestd-web-ui
    fi
    
    # Enable and start new service
    echo "✨ Starting new timestd-web-api service..."
    sudo systemctl enable timestd-web-api
    sudo systemctl restart timestd-web-api
    
    # Check status
    echo "✅ Verifying status..."
    sudo systemctl status timestd-web-api --no-pager
    
    # Check if port 8000 is listening
    if ss -tuln | grep -q ":8000"; then
        echo "✅ Port 8000 is listening"
    else
        echo "❌ Port 8000 is NOT listening. Check logs."
        exit 1
    fi
    
else
    # Remote deployment mode (legacy fallback)
    SERVER="bee1"
    echo "📡 Running in REMOTE mode targeting $SERVER"
    echo "ERROR: Please run this script directly on bee1."
    exit 1
fi

echo "🎉 Migration complete! Access the new UI at http://localhost:8000"
echo "   - API Docs: http://localhost:8000/api/docs"
echo "   - Logs Viewer: http://localhost:8000/static/logs.html"
