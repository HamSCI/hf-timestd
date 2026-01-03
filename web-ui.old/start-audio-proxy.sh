#!/bin/bash

# Start ka9q-radio Audio Proxy
# This provides smooth audio streaming by interfacing directly with radiod

cd "$(dirname "$0")"

echo "🎵 Starting ka9q-radio Audio Proxy..."
echo "📡 This replaces the Python audio_streamer for smooth playback"
echo ""

# Check if radiod is running
if ! pgrep -f "radiod" > /dev/null; then
    echo "❌ Error: radiod is not running"
    echo "   Please start ka9q-radio first"
    exit 1
fi

echo "✅ radiod is running"

# Start the audio proxy
node ka9q-radio-proxy.js
