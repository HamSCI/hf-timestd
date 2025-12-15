#!/bin/bash
#
# Quick wrapper for analyze_timing.py to show today's quality metrics
#
# Usage:
#   ./scripts/today-quality.sh                    # All channels
#   ./scripts/today-quality.sh "WWV 10 MHz"       # Specific channel
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Get data root from config or use default
DATA_ROOT="${GRAPE_DATA_ROOT:-/tmp/timestd-test}"

# Get today's date in YYYYMMDD format
TODAY=$(date +%Y%m%d)

echo "📊 GRAPE Quality Analysis for $TODAY"
echo "📁 Data root: $DATA_ROOT"
echo ""

if [ -n "$1" ]; then
    # Single channel mode
    echo "🔍 Analyzing channel: $1"
    python3 "$PROJECT_ROOT/scripts/analyze_timing.py" \
        --date "$TODAY" \
        --channel "$1" \
        --data-root "$DATA_ROOT"
else
    # All channels mode
    echo "🔍 Analyzing all channels..."
    python3 "$PROJECT_ROOT/scripts/analyze_timing.py" \
        --date "$TODAY" \
        --data-root "$DATA_ROOT"
fi
