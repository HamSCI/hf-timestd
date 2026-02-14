# Quality Metrics for KA9Q Timing Architecture

## Overview

The GRAPE V2 recorder implements Phil Karn's KA9Q timing architecture where **RTP timestamp is the primary time reference**. Quality metrics are designed around this principle.

## Quality Philosophy

> **"RTP timestamp is truth, everything else is derived."**

Unlike traditional systems that compare data timing to system clock, KA9Q establishes its own authoritative timeline from RTP timestamps + WWV timing marks.

---

## Priority 1: Sample Count Integrity (40% of grade)

**Most Critical Metric**

```
Expected: 960,000 samples/minute (exactly)
Any deviation = data corruption
```

### Tracked Metrics:
- `actual_samples`: Samples written to file
- `expected_samples`: Should always be 960,000
- `completeness_percent`: Should be 100.00%
- `gap_samples_filled`: Zero-filled samples

### Quality Impact:
- Error >0.1%: Grade penalty up to 40 points
- Error >1.0%: Automatic F grade

### Why Critical:
Sample count integrity validates that:
- Resequencing is working
- Gap detection is accurate
- Zero-filling maintains timeline
- No duplicate or missing data

---

## Priority 2: RTP Continuity (30% of grade)

**Ground Truth for Gaps**

```
gap = next_rtp_timestamp - expected_rtp_timestamp
Expected increment: 320 per packet
```

### Tracked Metrics:
- `packets_received`: Total packets processed
- `packets_expected`: Expected based on sample rate
- `packets_dropped`: Missing packets
- `packet_loss_percent`: Loss rate
- `gaps_count`: Number of discontinuities
- `total_gap_duration_ms`: Total missing time
- `largest_gap_ms`: Worst single gap

### Quality Impact:
- Loss >0.1%: Up to 30 point penalty
- Multiple gaps (>5): Additional 15 point penalty

### Why Critical:
RTP timestamp IS your time reference. Gaps here are absolute truth - they represent actual missing data that was filled with zeros.

---

## Priority 3: Time_snap Quality (20% of grade)

**RTP → UTC Mapping Accuracy**

```
drift_ms = (predicted_utc - actual_wwv_utc) * 1000
```

### Tracked Metrics:
- `time_snap_established`: Is time_snap active?
- `time_snap_source`: "wwv_first", "wwv_verified", "wwv_corrected", "fallback"
- `time_snap_drift_ms`: Current drift from WWV
- `time_snap_age_minutes`: Time since establishment

### Quality Impact:
- Drift >50ms: 20 point penalty + time_snap re-establishment
- Drift >20ms: 10 point penalty
- Drift >10ms: 5 point penalty
- No time_snap + no WWV: 15 point penalty

### Why Critical:
This validates your RTP→UTC mapping is accurate. Large drift means your timestamps are wrong.

### Alert Thresholds:
- **Warning**: >20ms drift
- **Critical**: >50ms drift (triggers correction)

---

## Priority 4: Network Stability (10% of grade)

**Resequencing Activity**

```
reordering_depth = arrival_seq - expected_seq
```

### Tracked Metrics:
- `packets_resequenced`: Out-of-order packets fixed
- `max_resequencing_depth`: Worst reordering
- `resequencing_buffer_utilization`: Peak % of 64-entry buffer

### Quality Impact:
- Depth >10 packets: Up to 10 point penalty
- Depth >30 packets: Alert + max penalty

### Why This Matters:
High reordering indicates network instability. Your resequencing handles it (data is still good), but it indicates potential future problems.

---

## Quality Grades

| Grade | Score | Meaning | Data Usability |
|-------|-------|---------|----------------|
| **A** | 90-100 | Perfect | Excellent for all analysis |
| **B** | 80-89 | Good | Minor issues, fully usable |
| **C** | 70-79 | Fair | Some issues, use with caution |
| **D** | 60-69 | Poor | Significant problems |
| **F** | <60 | Failed | Data unusable |

---

## Per-Minute Quality Report

### Example: Perfect Minute (Grade A)

```
============================================================
Minute: 2025-11-04T06:25:00Z
Quality: ✅ A (100.0/100)

📊 Sample Integrity:
   Samples: 960,000/960,000 (100.00%)

📡 RTP Continuity:
   Packets: 3000/3000 (0.000% loss)

⏱️  Time_snap Quality:
   Status: wwv_verified
   Drift: +2.3 ms
   Age: 15 minutes

📻 WWV/CHU Detection:
   Detected: YES ✅
   Timing error: +44.7 ms
   Duration: 798.0 ms
============================================================
```

### Example: Good with Minor Gap (Grade B)

```
============================================================
Minute: 2025-11-04T06:26:00Z
Quality: ✓ B (85.0/100)

📊 Sample Integrity:
   Samples: 960,000/960,000 (100.00%)
   Gaps filled: 320 samples (1 events)

📡 RTP Continuity:
   Packets: 2999/3000 (0.033% loss)
   Resequenced: 2 packets (max depth: 1)
   Queue usage: 3.1%

⏱️  Time_snap Quality:
   Status: wwv_verified
   Drift: +3.1 ms
   Age: 16 minutes
============================================================
```

### Example: Problem Minute (Grade D)

```
============================================================
Minute: 2025-11-04T06:27:00Z
Quality: ❌ D (65.0/100)

🚨 ALERTS:
   Multiple gaps: 8
   High reordering: 15 packets

📊 Sample Integrity:
   Samples: 960,000/960,000 (100.00%)
   Gaps filled: 12,800 samples (8 events)

📡 RTP Continuity:
   Packets: 2960/3000 (1.333% loss)
   Resequenced: 45 packets (max depth: 15)
   Queue usage: 23.4%

⏱️  Time_snap Quality:
   Status: wwv_verified
   Drift: +8.2 ms
   Age: 17 minutes

📻 WWV/CHU Detection:
   Detected: NO (propagation/signal)
============================================================
```

---

## Hourly Summary Example

```
HOURLY QUALITY SUMMARY (KA9Q Timing Architecture)
================================================================================

📡 WWV_5_MHz:
   Minutes: 60
   Quality: A:55 B:4 C:1 D:0 F:0
   WWV: 58 detections
   Avg drift: +2.8 ms

📡 WWV_2.5_MHz:
   Minutes: 60
   Quality: A:54 B:5 C:1 D:0 F:0
   Resequenced: 12 packets
   WWV: 59 detections
   Avg drift: +3.1 ms

📡 WWV_10_MHz:
   Minutes: 60
   Quality: A:48 B:10 C:2 D:0 F:0
   Gaps: 2 events
   WWV: 45 detections
   Avg drift: +4.2 ms

================================================================================
OVERALL:
   Quality distribution:
      A: 90.0%
      B: 8.5%
      C: 1.5%
   Total resequencing: 28 packets
   Total gaps: 3 events
   Time_snap drift: +1.2 to +5.8 ms (avg: +3.2)
================================================================================
```

---

## What System Time Comparison Shows

**Limited Value in KA9Q Architecture**

System time comparison is **not a primary metric** because:
1. System time is DERIVED from RTP timestamps (via time_snap)
2. Comparing RTP to system time is circular
3. You're establishing your own timeline, not matching system clock

**Still Useful For:**
- Sanity check (is system clock ~reasonable?)
- Initial time_snap establishment (before first WWV)
- Debugging major system issues

**Not Useful For:**
- Primary quality assessment
- Data usability determination
- Timing accuracy validation (use WWV drift instead)

---

## Using Quality Metrics

### Real-Time Monitoring

```bash
# Watch quality grades in real-time
tail -f recorder_combined.log | grep "Minute complete"

# Output:
WWV_5_MHz: Minute complete: 06:25 → ...iq.npz [✅ A]
WWV_2.5_MHz: Minute complete: 06:25 → ...iq.npz [✅ A]
WWV_10_MHz: Minute complete: 06:25 → ...iq.npz [✓ B]
```

### Post-Analysis

```bash
# Show quality summary
python scripts/show_quality_summary.py /path/to/quality/

# Show last 10 minutes
python scripts/show_quality_summary.py /path/to/quality/ --last 10

# Specific channel
python scripts/show_quality_summary.py /path/to/quality/ --channel WWV_5_MHz
```

### CSV Export

Quality metrics are exported to CSV for detailed analysis:
- `{channel}_minute_quality_{date}.csv`: Per-minute metrics
- `{channel}_discontinuities_{date}.csv`: Every gap/discontinuity

---

## Integration with Scientific Analysis

### Data Selection

**Recommended Quality Filters:**

- **Critical analysis** (timing-sensitive): Grade A only
- **Standard analysis** (propagation studies): Grade A or B
- **Exploratory** (looking for events): Grade C acceptable
- **Never use**: Grade D or F

### Quality Metadata

Every minute file should reference its quality metrics:
- Link to quality CSV
- Include quality grade in metadata
- Note any alerts or issues

### Gap Handling

Gaps filled with zeros are logged in discontinuities CSV:
- Exact sample index
- Duration in samples and ms
- RTP timestamp context
- Explanation

This allows:
- Filtering data around gaps
- Excluding problematic periods
- Understanding propagation conditions

---

## Summary

**KA9Q Quality Priorities:**

1. **Sample Count**: Is the data stream intact? (40 points)
2. **RTP Continuity**: Are timestamps accurate? (30 points)
3. **Time_snap**: Is RTP→UTC mapping good? (20 points)
4. **Network**: Is resequencing handling issues? (10 points)

**Key Insight**: Focus on data integrity and timing accuracy, not system time matching. Your quality metrics validate that your self-contained timeline (RTP + WWV) is reliable.
