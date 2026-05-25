#!/bin/bash
# Quick backfill script - reprocess gaps in discrimination data
# Usage: ./scripts/backfill-now.sh [channel-name] [max-files]

CHANNEL="${1:-WWV 10 MHz}"
MAX_FILES="${2:-60}"

echo "🔄 Backfilling gaps for: $CHANNEL"
echo "   Maximum files: $MAX_FILES"
echo ""

source venv/bin/activate

python3 -m grape_recorder.gap_backfill \
  --archive-dir "/tmp/timestd-test/archives/${CHANNEL// /_}" \
  --discrimination-csv "/tmp/timestd-test/analytics/${CHANNEL// /_}/discrimination/${CHANNEL// /_}_discrimination_$(date +%Y%m%d).csv"

echo ""
echo "Now reprocessing gaps..."
echo ""

# Get list of gap files
python3 << 'EOF'
import sys
sys.path.insert(0, 'src')
from grape_recorder.gap_backfill import find_gaps
from grape_recorder.interfaces.data_models import NPZArchive
from pathlib import Path
import os

channel = os.environ.get('CHANNEL', 'WWV 10 MHz')
max_files = int(os.environ.get('MAX_FILES', '60'))

archive_dir = Path(f"/tmp/timestd-test/archives/{channel.replace(' ', '_')}")
csv_file = Path(f"/tmp/timestd-test/analytics/{channel.replace(' ', '_')}/discrimination/{channel.replace(' ', '_')}_discrimination_{Path.cwd().name.split('-')[0]}.csv")

# For today
import datetime
today = datetime.datetime.now().strftime('%Y%m%d')
csv_file = Path(f"/tmp/timestd-test/analytics/{channel.replace(' ', '_')}/discrimination/{channel.replace(' ', '_')}_discrimination_{today}.csv")

gaps = find_gaps(archive_dir, csv_file)
print(f"Found {len(gaps)} gaps")

# Limit to max_files
gaps_to_fill = gaps[:max_files]
print(f"Processing first {len(gaps_to_fill)} gaps...")
print()

for i, (gap_time, npz_file) in enumerate(gaps_to_fill):
    print(f"[{i+1}/{len(gaps_to_fill)}] {gap_time.strftime('%Y-%m-%d %H:%M')} UTC → {npz_file.name}")
    # Process this file through analytics
    # This would need the full analytics service context...

print()
print("⚠️  Note: Full backfill requires analytics service restart with --backfill-gaps")
print("   Or manually reprocess with analytics service methods")
EOF
