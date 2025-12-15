#!/bin/bash
# Automated Quality Analysis Runner
# Run every 15 minutes to keep dashboard updated

DATA_ROOT="${1:-/tmp/timestd-test}"
cd "$(dirname "$0")/.." || exit 1

# Activate venv if available
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run analysis for today
DATE=$(date -u +%Y%m%d)
python3 scripts/automated_quality_analysis.py \
    --data-root "$DATA_ROOT" \
    --date "$DATE" \
    >> "$DATA_ROOT/logs/quality-analysis.log" 2>&1

echo "[$(date -u +%H:%M:%S)] Quality analysis completed for $DATE"
