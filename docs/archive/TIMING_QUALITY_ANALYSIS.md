# Timing Quality Analysis - Per-Channel Hourly Breakdown

**Purpose**: Correlate data quality with ionospheric propagation conditions across the day.

---

## Quick Start

### Analyze Today's Data

```bash
# All channels:
./scripts/today-quality.sh

# Specific channel:
./scripts/today-quality.sh "WWV 10 MHz"
```

### Analyze Specific Date

```bash
# All channels:
python3 scripts/analyze_timing.py --date 20251116 --data-root /tmp/grape-test

# Specific channel:
python3 scripts/analyze_timing.py --date 20251116 --channel "WWV 10 MHz" --data-root /tmp/grape-test

# Export to JSON for further analysis:
python3 scripts/analyze_timing.py --date 20251116 --channel "WWV 10 MHz" --data-root /tmp/grape-test --export wwv10_quality.json
```

---

## What It Shows

### 1. Time_snap History

```
📍 CURRENT:
   RTP:    1,236,241,978
   UTC:    2025-11-16 14:53:00
   Source: wwv_verified
   Rate:   16,000 Hz

📜 HISTORY (7 entries):
   1. 2025-11-16 14:53:00 (RTP: 1,236,241,978, wwv_verified)
   2. 2025-11-16 14:49:00 (RTP: 1,232,403,578, wwv_verified)
   ...
```

**Use**: Verify timing references are being established regularly (every 4-5 minutes expected for strong signals)

### 2. Gap Analysis with Hourly Breakdown

```
📊 OVERALL STATISTICS:
   Total files:      927
   Files with gaps:  856 (92.3%)
   Total gaps:       1,041
   Samples filled:   20,526,080
   Completeness:     89.34%
   Quality Grade:    F

⏰ HOURLY BREAKDOWN:
   Hour │ Gaps │ Samples │ Files
   ─────┼──────┼─────────┼──────
   00:xx│   79 │ 1700160 │   60 ███████████████
   01:xx│   71 │ 1589440 │   60 ██████████████
   02:xx│   51 │ 1281600 │   60 ██████████      ← WORST
   03:xx│   45 │ 1103040 │   60 █████████       ← WORST
   04:xx│   74 │ 1272960 │   60 ██████████████
   ...
   12:xx│   77 │ 1336320 │   60 ███████████████
   15:xx│   31 │  609920 │   26 ██████          ← BEST
```

**Use**: Identify problem hours - often correlates with:
- Ionospheric propagation fades (sunrise/sunset transitions)
- Local interference patterns (industrial activity)
- Network congestion (if consistent across channels)

### 3. Completeness Timeline Chart

```
   Hour │ Quality
   ─────┼────────────────────────────────────────────
   00:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 95.4%
   01:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 93.9%
   02:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 79.4%     ← Propagation fade
   03:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 73.1%       ← Weakest
   04:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 97.8% ← Recovery
   ...
   12:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 97.7% ← Strong daytime
   15:xx│ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 97.5%

   ▓ >99.9%   ▒ 99.0-99.9%   ░ <99.0%
```

**Use**: Visual pattern recognition - see when signal quality degrades/improves

---

## Propagation Correlation Examples

### Example 1: Early Morning Fade (WWV 10 MHz)

**Observed Pattern** (from 2025-11-16 data):
```
02:00 - 03:00 UTC: 73-79% completeness
04:00+:           97%+ completeness recovers
```

**Likely Cause**: 
- WWV Fort Collins → Colorado receiver path
- Nighttime D-layer absorption minimum
- Ionospheric transition period (pre-sunrise)
- Expected for 10 MHz HF propagation

**Scientific Value**: 
- Documents ionospheric D-layer recovery timing
- Can correlate with solar activity indices
- Validates propagation models

### Example 2: Consistent Daytime Quality

**Observed Pattern**:
```
12:00 - 15:00 UTC: 97%+ completeness
Strong signal, minimal gaps
```

**Likely Cause**:
- Strong F-layer propagation
- Optimal skip distance for WWV-Colorado path
- Low local noise conditions

**Scientific Value**:
- Confirms reliable propagation window
- Best data for timing reference studies
- High-confidence discrimination measurements

### Example 3: All Channels Degraded Simultaneously

**If you see this**:
```
ALL channels show poor quality same hour
Not just one frequency/station
```

**Likely Causes** (in order of probability):
1. **Local interference** - Check for nearby equipment
2. **Network issues** - Verify radiod/recorder connectivity  
3. **System overload** - Check CPU/memory usage
4. **Geomagnetic storm** - Check space weather indices

**NOT propagation** - Different frequencies/paths should vary independently

---

## Integration with Web UI

The **Timing Dashboard** (`http://localhost:3000/timing-dashboard.html`) shows:
- Real-time system status
- Aggregate quality metrics across all channels
- Time_snap status
- Active alerts

**CLI tool complements this** with:
- Per-channel hourly detail
- Historical pattern analysis
- Exportable data for correlation studies
- Quick diagnosis of quality issues

**Workflow**:
1. Check web dashboard for current system status
2. If quality issues seen, run CLI tool for hourly breakdown
3. Correlate patterns with propagation conditions
4. Document findings for ionospheric studies

---

## Quality Grading Reference

| Grade | Completeness | Interpretation |
|-------|--------------|----------------|
| A+    | ≥99.9%       | Excellent - GPS-quality timing achievable |
| A     | 99.5-99.9%   | Very Good - Suitable for all analyses |
| B     | 99.0-99.5%   | Good - Minor packet loss, timing valid |
| C     | 95.0-99.0%   | Acceptable - Usable with caution |
| F     | <95.0%       | Failed - Investigate cause before use |

**Note**: Hourly grades can vary widely due to propagation. Overall grade reflects full-day average.

---

## Advanced Usage

### Compare Multiple Channels

```bash
# Analyze all WWV channels for pattern comparison:
for freq in 5 10 15 20; do
    echo "=== WWV $freq MHz ==="
    python3 scripts/analyze_timing.py --date 20251116 --channel "WWV ${freq} MHz" --data-root /tmp/grape-test | grep -A 10 "HOURLY BREAKDOWN"
done
```

**Use**: See if propagation issues are frequency-dependent (higher frequencies fade first at sunset)

### Export for Correlation Analysis

```bash
# Export all channels to JSON:
python3 scripts/analyze_timing.py --date 20251116 --data-root /tmp/grape-test --export quality_20251116.json

# Then process with custom scripts:
python3 my_correlation_analysis.py quality_20251116.json solar_data.json
```

### Multi-Day Trend Analysis

```bash
# Analyze last 7 days:
for days_ago in {0..6}; do
    date=$(date -d "$days_ago days ago" +%Y%m%d)
    python3 scripts/analyze_timing.py --date "$date" --channel "WWV 10 MHz" --data-root /tmp/grape-test | grep "Quality Grade"
done
```

---

## Troubleshooting

### No Data for Channel

```
❌ Channel directory not found: /tmp/grape-test/archives/WWV_10_MHz
```

**Fix**: Verify channel name exactly matches directory name (underscores, spaces, case)

### Empty NPZ Directory

```
❌ No NPZ files found for 20251116
```

**Fixes**:
1. Check date format: YYYYMMDD (no dashes/slashes)
2. Verify recorder was running on that date
3. Check data-root path is correct

### Time_snap Not Available

```
❌ No time_snap data available
```

**Causes**:
- Analytics service not running
- No WWV/CHU tones detected yet
- State file not created

**Check**: `ls -l /tmp/grape-test/state/analytics-*.json`

---

## See Also

- **Web Dashboard**: http://localhost:3000/timing-dashboard.html
- **Script Source**: `scripts/analyze_timing.py`
- **Convenience Wrapper**: `scripts/today-quality.sh`
- **Timing Architecture**: `docs/TIMING_ARCHITECTURE_V2.md`
- **WWV Detection Guide**: `docs/WWV_DETECTION.md`
