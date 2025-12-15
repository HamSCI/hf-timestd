#!/bin/bash
#
# Overnight Discrimination Analysis - Multi-Frequency
# Reprocess multiple days and frequencies with new discrimination improvements
#
# Usage: ./scripts/overnight_discrimination_analysis.sh

set -e

OUTPUT_BASE="/tmp/timestd-test/discrimination_analysis"

# Channels to process (prioritize propagation-diverse frequencies)
CHANNELS=(
    "WWV 5 MHz"    # Best nighttime propagation (1-2 hop skywave)
    "WWV 10 MHz"   # All-around good propagation
    "WWV 15 MHz"   # Best daytime propagation (F2 layer)
)

mkdir -p "$OUTPUT_BASE"/{logs,reports,csvs}

echo "======================================================================"
echo "OVERNIGHT MULTI-FREQUENCY DISCRIMINATION ANALYSIS"
echo "Start time: $(date)"
echo "======================================================================"
echo ""
echo "Processing frequencies:"
for ch in "${CHANNELS[@]}"; do
    echo "  - $ch"
done
echo ""
echo "This will:"
echo "  1. Reprocess 7 days × 3 frequencies with new discrimination"
echo "  2. Validate BCD joint least squares across propagation conditions"
echo "  3. Compare discrimination performance by frequency"
echo "  4. Generate cross-frequency correlation analysis"
echo ""
echo "Estimated time: 4-8 hours for ~25,000 minutes"
echo "======================================================================"

# Dates to process (last 7 complete days)
DATES=(
    "20251113"
    "20251114"
    "20251115"
    "20251116"
    "20251117"
    "20251118"
    "20251119"
)

echo ""
echo "Phase 1: Reprocessing discrimination data (multi-frequency)"
echo "----------------------------------------------------------------------"

total_processed=0

for channel in "${CHANNELS[@]}"; do
    echo ""
    echo "Processing channel: $channel"
    echo "----------------------------------------"
    
    for date in "${DATES[@]}"; do
        echo "  $date..."
        
        channel_safe=$(echo "$channel" | tr ' ' '_' | tr '.' '_')
        
        python3 scripts/reprocess_discrimination_timerange.py \
            --date "$date" \
            --channel "$channel" \
            --start-hour 0 \
            --end-hour 24 \
            > "$OUTPUT_BASE/logs/reprocess_${channel_safe}_${date}.log" 2>&1
        
        total_processed=$((total_processed + 1))
        echo "    ✓ complete ($total_processed/21 total)"
    done
done

echo ""
echo "Phase 2: Consolidating CSV files (per frequency)"
echo "----------------------------------------------------------------------"

for channel in "${CHANNELS[@]}"; do
    channel_safe=$(echo "$channel" | tr ' ' '_' | tr '.' '_')
    ANALYTICS_DIR="/tmp/timestd-test/analytics/${channel_safe}"
    MASTER_CSV="$OUTPUT_BASE/csvs/discrimination_${channel_safe}.csv"
    
    echo ""
    echo "Consolidating $channel..."
    
    # Get header from first file
    find "$ANALYTICS_DIR/discrimination" -name "*.csv" -type f 2>/dev/null | head -1 | xargs head -1 > "$MASTER_CSV" 2>/dev/null || true
    
    # Append all data (skip headers)
    for date in "${DATES[@]}"; do
        CSV_FILES=$(find "$ANALYTICS_DIR/discrimination" -name "*${date}*.csv" -type f 2>/dev/null || true)
        for csv in $CSV_FILES; do
            tail -n +2 "$csv" >> "$MASTER_CSV" 2>/dev/null || true
        done
    done
    
    LINE_COUNT=$(wc -l < "$MASTER_CSV" 2>/dev/null || echo "0")
    echo "  ✓ $channel: $((LINE_COUNT - 1)) records"
done

echo ""
echo "Phase 3: BCD Joint Least Squares Validation (per frequency)"
echo "----------------------------------------------------------------------"

for channel in "${CHANNELS[@]}"; do
    channel_safe=$(echo "$channel" | tr ' ' '_' | tr '.' '_')
    MASTER_CSV="$OUTPUT_BASE/csvs/discrimination_${channel_safe}.csv"
    
    echo ""
    echo "Analyzing $channel BCD amplitudes..."
    
    python3 << EOF
import csv
import json
import numpy as np

channel = "$channel"
input_csv = "$MASTER_CSV"
output_json = "$OUTPUT_BASE/reports/bcd_analysis_${channel_safe}.json"

all_ratios = []
minute_ratios = {0: [], 8: [], 9: [], 10: [], 29: [], 30: []}

try:
    with open(input_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            minute = int(row['minute_number'])
            bcd_windows = row['bcd_windows']
            
            if bcd_windows and bcd_windows.strip():
                try:
                    windows = json.loads(bcd_windows)
                    for w in windows:
                        wwv_amp = w.get('wwv_amplitude', 0)
                        wwvh_amp = w.get('wwvh_amplitude', 0)
                        
                        if wwv_amp > 0 and wwvh_amp > 0:
                            ratio_db = 20 * np.log10(wwv_amp / wwvh_amp)
                            all_ratios.append(ratio_db)
                            if minute in minute_ratios:
                                minute_ratios[minute].append(ratio_db)
                except:
                    pass

    results = {
        'channel': channel,
        'total_windows': len(all_ratios),
        'ratio_stats': {
            'mean_db': float(np.mean(all_ratios)) if all_ratios else 0,
            'std_db': float(np.std(all_ratios)) if all_ratios else 0,
            'min_db': float(np.min(all_ratios)) if all_ratios else 0,
            'max_db': float(np.max(all_ratios)) if all_ratios else 0,
            'median_db': float(np.median(all_ratios)) if all_ratios else 0
        },
        'separation_quality': {
            'near_zero_pct': 100 * sum(1 for r in all_ratios if abs(r) < 0.5) / len(all_ratios) if all_ratios else 0,
            'significant_pct': 100 * sum(1 for r in all_ratios if abs(r) >= 3) / len(all_ratios) if all_ratios else 0
        },
        'by_bcd_minute': {
            str(m): {
                'count': len(ratios),
                'mean_db': float(np.mean(ratios)) if ratios else 0,
                'std_db': float(np.std(ratios)) if ratios else 0
            } for m, ratios in minute_ratios.items() if ratios
        }
    }
    
    with open(output_json, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"  Total windows: {results['total_windows']}")
    print(f"  Ratio spread: {results['ratio_stats']['std_db']:.1f} dB (good if >3 dB)")
    print(f"  Significant separation: {results['separation_quality']['significant_pct']:.1f}%")
    
except Exception as e:
    print(f"  Error: {e}")
EOF
done

echo ""
echo "Phase 4: Cross-Frequency Correlation Analysis"
echo "----------------------------------------------------------------------"

echo ""
echo "Comparing discrimination agreement across frequencies..."

python3 << 'EOF'
import csv
from collections import defaultdict

channels = ["WWV_5_MHz", "WWV_10_MHz", "WWV_15_MHz"]
base = "/tmp/timestd-test/discrimination_analysis"

# Load discrimination results for each frequency
freq_data = {}
for ch in channels:
    freq_data[ch] = {}
    try:
        with open(f"{base}/csvs/discrimination_{ch}.csv", 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row['minute_timestamp']
                freq_data[ch][ts] = row['dominant_station']
    except:
        pass

# Find common timestamps
common_ts = set(freq_data[channels[0]].keys())
for ch in channels[1:]:
    common_ts &= set(freq_data[ch].keys())

print(f"  Common timestamps: {len(common_ts)}")

if common_ts:
    # Calculate agreement
    agreements = 0
    for ts in common_ts:
        stations = [freq_data[ch][ts] for ch in channels]
        if stations[0] == stations[1] == stations[2]:
            agreements += 1
    
    pct = 100 * agreements / len(common_ts)
    print(f"  All frequencies agree: {agreements}/{len(common_ts)} ({pct:.1f}%)")
    
    # Pairwise agreement
    for i in range(len(channels)):
        for j in range(i+1, len(channels)):
            ch1, ch2 = channels[i], channels[j]
            pair_agree = sum(1 for ts in common_ts if freq_data[ch1][ts] == freq_data[ch2][ts])
            pct = 100 * pair_agree / len(common_ts)
            print(f"  {ch1} vs {ch2}: {pct:.1f}% agreement")

EOF

echo ""
echo "Phase 5: Generate Summary Report"
echo "----------------------------------------------------------------------"

cat > "$OUTPUT_BASE/reports/SUMMARY.txt" << 'SUMMARY_EOF'
================================================================================
MULTI-FREQUENCY DISCRIMINATION ANALYSIS SUMMARY
================================================================================

SUMMARY_EOF

for channel in "${CHANNELS[@]}"; do
    channel_safe=$(echo "$channel" | tr ' ' '_' | tr '.' '_')
    
    cat >> "$OUTPUT_BASE/reports/SUMMARY.txt" << CHAN_EOF

Channel: $channel
----------------------------------------
CHAN_EOF
    
    if [ -f "$OUTPUT_BASE/reports/bcd_analysis_${channel_safe}.json" ]; then
        python3 << EOF
import json
with open("$OUTPUT_BASE/reports/bcd_analysis_${channel_safe}.json") as f:
    data = json.load(f)
    print(f"  Total BCD Windows: {data['total_windows']}")
    print(f"  Ratio Spread: {data['ratio_stats']['std_db']:.2f} dB")
    print(f"  Significant Separation: {data['separation_quality']['significant_pct']:.1f}%")
    print(f"  Mean WWV/WWVH Ratio: {data['ratio_stats']['mean_db']:+.2f} dB")
EOF
    fi >> "$OUTPUT_BASE/reports/SUMMARY.txt"
done

cat >> "$OUTPUT_BASE/reports/SUMMARY.txt" << 'FOOTER_EOF'

================================================================================
KEY FINDINGS:

1. BCD Joint Least Squares Performance:
   - Check that ratio spread > 3 dB (shows real amplitude separation)
   - Significant separation % should be >40% (clear discrimination)
   - Near-zero ratios should be <15% (not mirroring bug)

2. Cross-Frequency Correlation:
   - High agreement (>80%) indicates reliable discrimination
   - Disagreements show propagation-dependent reception patterns

3. Next Steps:
   - Review detailed logs in logs/
   - Analyze per-frequency CSVs in csvs/
   - Compare with old discrimination results (if available)

================================================================================
FOOTER_EOF

echo ""
echo "======================================================================"
echo "ANALYSIS COMPLETE"
echo "End time: $(date)"
echo "======================================================================"
echo ""
echo "Results location: $OUTPUT_BASE"
echo ""
echo "Key outputs:"
echo "  - Summary Report: $OUTPUT_BASE/reports/SUMMARY.txt"
echo "  - Per-frequency CSVs: $OUTPUT_BASE/csvs/discrimination_*.csv"
echo "  - BCD Analysis: $OUTPUT_BASE/reports/bcd_analysis_*.json"
echo ""
echo "View summary now:"
echo "  cat $OUTPUT_BASE/reports/SUMMARY.txt"
echo ""
echo "Total minutes processed: ~$((${#CHANNELS[@]} * ${#DATES[@]} * 1440))"
echo ""
