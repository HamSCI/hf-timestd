# WWV/WWVH Discrimination User Guide

**For Station Operators and Data Users**

---

## Overview

This guide explains how to use and interpret the 5-method WWV/WWVH discrimination system implemented in the GRAPE Signal Recorder. The system automatically separates WWV (Fort Collins, CO) and WWVH (Kauai, HI) signals on shared frequencies (2.5, 5, 10, 15 MHz) using complementary measurement techniques.

**Why 5 Methods?**
- **Redundancy:** Multiple independent measurements validate each other
- **Adaptability:** Different methods work better under different propagation conditions
- **Temporal Coverage:** From hourly calibration (440 Hz) to sub-second dynamics (ticks)
- **Cross-Validation:** Agreement between methods confirms discrimination accuracy

---

## The Five Methods

### Method 1: 440 Hz Hourly Tone 🎺

**What it measures:** Station ID tones broadcast exclusively by each station  
**When:** Minute 1 (WWVH) and Minute 2 (WWV) each hour  
**Temporal resolution:** 2 measurements per hour  
**Typical SNR requirement:** >10 dB

**How it works:**
- WWVH transmits 440 Hz tone during minute 1 (:15-:59 seconds)
- WWV transmits 440 Hz tone during minute 2 (:15-:59 seconds)
- Never simultaneous - provides unambiguous station identification

**Strengths:**
✅ Highest confidence - no possibility of confusion  
✅ Clean frequency (no harmonic contamination)  
✅ Long integration time (44 seconds)  
✅ Perfect for hourly calibration reference

**Limitations:**
⚠️ Only 2 data points per hour  
⚠️ Requires moderate to strong signals  
⚠️ No sub-hour dynamics

**When to trust:** Always - this is the "ground truth" when both stations are strong

---

### Method 2: BCD Correlation (100 Hz) 🚀

**What it measures:** Time-of-arrival difference via cross-correlation of BCD time code  
**When:** Continuous throughout each minute  
**Temporal resolution:** 15 measurements per minute (3-second steps)  
**Typical processing gain:** 10 dB (10-second integration)

**How it works:**
- Both WWV and WWVH transmit identical 100 Hz BCD time code
- Signals arrive at different times due to propagation path differences
- Cross-correlation produces two peaks separated by differential delay (typically 5-30 ms)
- Peak heights = individual station amplitudes

**Strengths:**
✅ PRIMARY METHOD - highest temporal resolution  
✅ Continuous coverage (no gaps in discrimination)  
✅ Measures both amplitude AND delay simultaneously  
✅ No harmonic contamination (100 Hz isolated from tones)  
✅ Captures ionospheric coherence time variations

**Limitations:**
⚠️ Requires both stations present  
⚠️ Sensitive to multipath (can split/smear peaks)  
⚠️ Template must match transmitted BCD exactly

**When to trust:** When correlation quality >3.0 and delay is 5-30 ms

**Optimization (Nov 2025):** Reduced from 1-second to 3-second steps for 3× performance improvement while maintaining excellent resolution.

---

### Method 3: Timing Tones (1000/1200 Hz) 📡

**What it measures:** Power ratio of WWV (1000 Hz) vs WWVH (1200 Hz) marker tones  
**When:** Top of every minute (:00.0 seconds)  
**Temporal resolution:** 1 measurement per minute  
**Typical tone duration:** 0.8 seconds

**How it works:**
- WWV transmits 1000 Hz, WWVH transmits 1200 Hz  
- Phase-invariant quadrature matched filtering detects both  
- Power ratio (dB) indicates which station is stronger  
- Differential delay from timing error difference

**Strengths:**
✅ Baseline method - always attempted  
✅ Works even with weak signals (SNR >15 dB)  
✅ Well-tested and reliable  
✅ Integrated with time_snap timing corrections

**Limitations:**
⚠️ Subject to harmonic contamination (500/600 Hz create 1000/1200 Hz)  
⚠️ Only 1 data point per minute  
⚠️ No sub-minute dynamics

**When to trust:** When both tones detected with SNR >20 dB and differential delay <100 ms

---

### Method 4: Tick Windows (5 ms per second) ⚡

**What it measures:** SNR of WWV (1000 Hz) vs WWVH (1200 Hz) per-second ticks  
**When:** Every second except :00 (which has the 800 ms marker)  
**Temporal resolution:** 6 measurements per minute (10-second windows)  
**Integration:** Adaptive coherent/incoherent based on phase stability

**How it works:**
- Each second has a 5 ms tick at 1000 Hz (WWV) or 1200 Hz (WWVH)  
- 10 ticks integrated coherently when phase is stable (10 dB gain)  
- Falls back to incoherent integration when phase unstable (5 dB gain)  
- 6 windows per minute: seconds 1-10, 11-20, 21-30, 31-40, 41-50, 51-59

**Strengths:**
✅ Sub-minute temporal resolution  
✅ Adaptive to channel conditions  
✅ Captures rapid propagation changes  
✅ Harmonic contamination removed via notch filters

**Limitations:**
⚠️ Short integration (5 ms per tick) requires higher SNR  
⚠️ Phase instability reduces gain  
⚠️ Contamination possible if notch filters insufficient

**When to trust:** When coherence quality >0.5 and coherent integration used

---

### Method 5: Weighted Voting 📊

**What it measures:** Final determination combining all methods with minute-specific weighting  
**When:** Every minute  
**Temporal resolution:** 1 determination per minute  
**Output:** Dominant station + confidence level

**How it works:**
- Applies different weights based on minute number:
  - **Minutes 1-2:** 440 Hz highest weight (10×) → Tick (5×) → BCD (2×) → Carrier (1×)
  - **Minutes 0, 8-10, 29-30:** BCD highest weight (10×) → Tick (5×) → Carrier (2×)
  - **All other minutes:** Carrier highest weight (10×) → Tick (5×) → BCD (2×)
- Only counts methods with significant differences (>3 dB)
- Normalizes scores to determine dominant station
- Calculates confidence based on agreement margin

**Confidence Levels:**
- **High:** Margin >70%, strong agreement between methods
- **Medium:** Margin 40-70%, moderate agreement
- **Low:** Margin <40%, weak or conflicting signals

**Strengths:**
✅ Leverages strengths of all methods  
✅ Adaptive to signal conditions  
✅ Provides confidence metric  
✅ Robust to single-method failures

**Limitations:**
⚠️ Complex - harder to debug  
⚠️ Depends on all upstream methods  
⚠️ May mask method-specific issues

**When to trust:** When confidence is 'high' and at least 3 methods agree

---

## Interpreting the Web UI

### Discrimination Analysis Page

**7-Panel Display (top to bottom):**

1. **Method 3: Timing Tones SNR Ratio**
   - Scatter: Raw per-minute measurements
   - Line: 10-minute smoothed trend
   - Threshold lines at ±3 dB (balanced region)
   - **Interpretation:** Positive = WWV stronger, Negative = WWVH stronger

2. **Method 1: 440 Hz ID Tones Power**
   - WWV (minute 2) in purple
   - WWVH (minute 1) in cyan
   - **Interpretation:** 2 points per hour, use as calibration reference

3. **Method 3: Power Ratio**
   - Similar to SNR ratio but based on absolute power
   - Color gradient shows dominance
   - **Interpretation:** Confirms timing tone results

4. **Method 5: Weighted Voting Dominance**
   - Color-coded squares showing final determination
   - **Interpretation:** This is the "official" answer each minute

5. **Method 4: Tick Windows SNR**
   - WWV above zero line (green)
   - WWVH below zero line (red)
   - **Interpretation:** 6 measurements per minute show sub-minute variations

6. **Method 2: BCD Amplitude (HIGH RESOLUTION 🚀)**
   - WWV above zero (positive)
   - WWVH below zero (negative, mirrored)
   - **Interpretation:** 15 points/minute - most detailed view

7. **Method 2: BCD Differential Delay**
   - Time-of-arrival difference (milliseconds)
   - Color-coded by correlation quality
   - **Interpretation:** Ionospheric path length difference

### Statistics Cards

**Four cards show daily performance:**

1. **440 Hz Tones** - Detections out of 48 possible (24 hours × 2/hour)
2. **BCD Correlation** - Total windows analyzed (~21,600 for full day)
3. **Tick Coherence** - Percentage using coherent integration
4. **High Confidence** - Minutes with strong discrimination

**Good day indicators:**
- 440 Hz: >80% detection rate
- BCD: >90% valid windows
- Tick: >70% coherent
- High conf: >60% of minutes

---

## Common Scenarios

### Scenario 1: Strong WWV, Weak WWVH

**Symptoms:**
- SNR ratio consistently positive (+10 to +20 dB)
- 440 Hz: WWV detections frequent, WWVH rare
- BCD: Only one peak detected (WWV only)
- Voting: WWV dominant with high confidence

**Interpretation:** **WWV path is much better than WWVH path**
- Normal for continental US locations closer to Colorado than Hawaii
- Daytime ionospheric conditions favor shorter path
- East coast typically sees this pattern

**Action:** None - this is expected behavior for your location/time

---

### Scenario 2: Balanced Signals

**Symptoms:**
- SNR ratio oscillates around 0 dB (±3 dB)
- 440 Hz: Both stations detected hourly
- BCD: Two clear peaks, similar amplitudes
- Voting: Alternates between WWV/WWVH/BALANCED

**Interpretation:** **Both paths are equally viable**
- Ideal condition for discrimination analysis
- Provides best data for ionospheric studies
- Fading and propagation variations visible

**Action:** Monitor BCD and tick data for rapid variations

---

### Scenario 3: Neither Station Detected

**Symptoms:**
- SNR ratio near 0 dB (noise floor)
- 440 Hz: No detections
- BCD: No valid correlations
- Voting: Low confidence, "NONE" determination

**Interpretation:** **Propagation blackout or local interference**
- Ionospheric absorption (D-layer)
- Geomagnetic storm effects
- Local RFI masking signals

**Action:** Check other frequencies, verify antenna/receiver

---

### Scenario 4: BCD Shows Fine Structure

**Symptoms:**
- BCD amplitude varies significantly within single minute
- Differential delay fluctuates ±5 ms
- Tick windows show different results per 10-second period

**Interpretation:** **Ionospheric dynamics faster than 1 minute**
- Traveling ionospheric disturbances (TIDs)
- Selective fading
- Multipath variations

**Action:** This is valuable scientific data - ensure high completeness

---

## Troubleshooting

### Problem: 440 Hz Never Detected

**Possible causes:**
1. Signals too weak (SNR <10 dB)
2. Local interference at 440 Hz
3. Receiver bandwidth too narrow

**Diagnosis:**
```bash
# Check if ANY tones detected
grep "440 Hz" /path/to/logs/analytics-*.log

# Verify signal strength
# Look at Method 3 SNR values - if those are >20 dB but 440 Hz fails,
# it's likely a detection threshold issue
```

**Solutions:**
- Wait for better propagation
- Check antenna system
- Reduce 440 Hz detection threshold (advanced)

---

### Problem: BCD Correlation Quality Low

**Possible causes:**
1. Weak signals (SNR <20 dB)
2. Only one station present
3. Multipath smearing correlation peaks
4. BCD template mismatch

**Diagnosis:**
Check correlation quality in CSV:
```bash
grep "bcd_correlation_quality" discrimination_logs/*.csv | \
  awk -F',' '{print $19}' | \
  awk '{sum+=$1; count++} END {print "Mean:", sum/count}'
```

**Solutions:**
- Quality <2.0: Very weak, discrimination unreliable
- Quality 2.0-5.0: Moderate, usable with caution
- Quality >5.0: Good, trust the results

---

### Problem: Methods Disagree

**Symptoms:**
- 440 Hz says WWV, but BCD says WWVH
- Timing tones say balanced, but ticks say WWV dominant

**Interpretation:** **Not necessarily a problem!**
- Different methods measure at different times (minute boundaries vs continuous)
- Rapid fading can cause disagreement
- Check confidence level - weighted voting accounts for this

**Action:**
1. Look at time-series - is one method showing an outlier?
2. Check if disagreement correlates with low SNR
3. Trust Method 5 (weighted voting) final determination
4. If persistent, may indicate RFI or calibration issue

---

## Data Quality Indicators

### Excellent Quality (Publication-Ready)

- ✅ 440 Hz detection rate >80%
- ✅ BCD correlation quality >5.0
- ✅ Tick coherence rate >70%
- ✅ High confidence >60% of minutes
- ✅ Differential delay 5-30 ms and stable

### Good Quality (Usable for Analysis)

- ✅ 440 Hz detection rate 50-80%
- ✅ BCD correlation quality 3.0-5.0
- ✅ Tick coherence rate 50-70%
- ✅ High confidence 40-60% of minutes
- ✅ Differential delay <50 ms

### Poor Quality (Use with Caution)

- ⚠️ 440 Hz detection rate <50%
- ⚠️ BCD correlation quality <3.0
- ⚠️ Tick coherence rate <50%
- ⚠️ High confidence <40% of minutes
- ⚠️ Differential delay >50 ms or unstable

### Invalid Data (Do Not Use)

- ❌ No detections in any method
- ❌ BCD showing impossible delays (>100 ms)
- ❌ All methods show noise floor
- ❌ System logs show gaps or errors

---

## Best Practices

### For Station Operators

1. **Monitor daily statistics** - Check the 4 stat cards each morning
2. **Watch for trends** - Gradual degradation may indicate antenna/receiver issues
3. **Note propagation patterns** - Your location has characteristic WWV/WWVH balance
4. **Archive discrimination logs** - CSV files are compact and valuable for long-term studies
5. **Report anomalies** - Sudden changes in discrimination patterns may indicate equipment problems

### For Data Users

1. **Use Method 5 (voting) for final determination** - It's the most robust
2. **Cross-validate with 440 Hz** - When available, it's ground truth
3. **Leverage BCD's temporal resolution** - Best for studying rapid ionospheric variations
4. **Check confidence levels** - Filter to high confidence for statistical studies
5. **Understand your location** - Local propagation affects which methods work best

### For Scientific Analysis

1. **Differential delay is ionospheric data** - Don't discard as "jitter"
2. **BCD amplitude variations are real** - Capture fading dynamics
3. **Coherence quality indicates ionospheric state** - Low coherence = disturbed conditions
4. **Compare methods** - Agreement validates, disagreement reveals complexity
5. **Long-term statistics** - Seasonal and solar cycle variations are scientifically valuable

---

## Frequently Asked Questions

**Q: Why do I sometimes see only one station?**

A: Ionospheric propagation is variable. One path may be open while the other is closed. This is normal and scientifically interesting!

**Q: Is BCD really better than timing tones?**

A: For temporal resolution, yes - 15× more data points. But timing tones work at lower SNR. Both are valuable.

**Q: What's a "good" differential delay?**

A: Typical values are 5-30 ms. This represents ionospheric path difference. Values outside this range may indicate detection errors.

**Q: Why does weighted voting sometimes disagree with 440 Hz?**

A: 440 Hz is only measured hourly. If propagation changes between those measurements, voting (which runs every minute) will track it better.

**Q: How do I export discrimination data?**

A: CSV files are in `/path/to/analytics/{channel}/discrimination/` with one file per day. Standard CSV format, easy to import.

**Q: Can I adjust method weights?**

A: Advanced users can modify `wwvh_discrimination.py` line ~418, but default weights are optimized for most scenarios.

**Q: What causes "jumps" in BCD differential delay?**

A: Usually multipath or phase wrapping. If correlation quality is still good, the amplitude data is trustworthy even if delay jumps.

---

## Getting Help

**Logs to check:**
```bash
# Analytics service logs (tone detection, discrimination)
tail -f /path/to/logs/analytics-*.log

# Web UI access logs
tail -f web-ui/monitoring-server.log

# Discrimination CSV files
ls -lh /path/to/analytics/*/discrimination/
```

**Useful commands:**
```bash
# Check detection rates
grep "440 Hz detected" logs/analytics-*.log | wc -l

# BCD correlation quality summary
awk -F',' 'NR>1 {sum+=$19; count++} END {print "Mean BCD quality:", sum/count}' \
  analytics/WWV_5_MHz/discrimination/WWV_5_MHz_discrimination_20251120.csv

# Method agreement check
grep "high" analytics/WWV_5_MHz/discrimination/*.csv | wc -l
```

**Support Resources:**
- HamSCI GRAPE mailing list: grape@hamsci.groups.io
- GitHub issues: [project repo]/issues
- Documentation: `docs/` directory in repository

---

## Summary

The 5-method discrimination system provides:

✅ **Reliability** - Multiple independent measurements  
✅ **Temporal coverage** - From hourly to sub-second  
✅ **Adaptability** - Works under various propagation conditions  
✅ **Validation** - Methods cross-check each other  
✅ **Science value** - Captures ionospheric dynamics  

**Key takeaway:** Trust Method 5 (weighted voting) for final determination, but examine individual methods to understand WHY the system made that decision. The richness of the data comes from having multiple perspectives on the same physical phenomenon.

---

**Document Version:** 1.0  
**Last Updated:** November 23, 2025  
**Authors:** Michael James Hauan/AC0G  
**Related Docs:** `WWV_WWVH_DISCRIMINATION_METHODS.md`, `BCD_DISCRIMINATION_IMPLEMENTATION.md`
