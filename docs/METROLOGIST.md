# Metrologist's Guide to "Steel Ruler" Verification

**Date:** January 12, 2026
**Version:** 1.0 (Steel Ruler)

## Philosophy

The **"Steel Ruler"** philosophy asserts that in a GPSDO-disciplined system, the local clock is significantly more stable (sub-ppb stability) than the ionosphere (10-100 ppb equivalent jitter). Therefore, we must:

1. Trust the local clock (zero process noise).
2. Attribute all residuals to ionospheric path variation.
3. Clamp long-term drift to 0.0, as the GPSDO prevents accumulation.

## Validation Procedure

### 1. Verify Baseline Stability

The most critical check is ensuring the `D_clock` baseline is **horizontal**, not "walking".

1. **Open Web UI:** Go to Metrology Dashboard.
2. **Check Slope:** The fused `D_clock` line should be flat over 24 hours.
3. **Verify Fusion Log:**

   ```bash
   journalctl -u timestd-fusion -n 50 | grep "Steel Ruler"
   # Expected Output:
   # INFO:__main__:Steel Ruler: Baseline is STABLE (drift = 0.0 ms/min)
   ```

### 2. Verify Latency

Ensure latency is low to allow real-time Chrony discipline.

1. **Run Verification:** `scripts/verify_pipeline.sh`
2. **Check Phase 2 Latency:** Should be < 90 seconds for active channels.
3. **Check Phase 3 Latency:** Should be < 120 seconds.

### 3. Chrony Discipline

Verify the system is actively steering the kernel clock.

```bash
chronyc tracking
# Look for:
# Ref ID        : 544D4752 (TMGR)
# Stratum       : 1 (if treating HF as primary) or >1
# Last offset   : +0.000xxxx seconds (sub-millisecond)
# RMS offset    : 0.000xxxx seconds
# Frequency     : x.xxx ppm (should be stable)
```

### 4. Ionospheric "Weather"

If `D_clock` values are jumping significantly (> 2-3 ms), this is likely ionospheric weather (storms, TIDs), not clock failure.

- **Check Propagation Analysis:** Look for similar jumps across *multiple frequencies*.
- **Global Differential:** If 10 MHz and 15 MHz both jump +2ms, it's a layer height change.

## Troubleshooting

### "Walking" Baseline (Non-Zero Slope)

If the baseline starts tilting:

1. **Check GPSDO Lock:** Ensure the physical clock is actually locked.
2. **Force Reset:**

   ```bash
   sudo systemctl stop timestd-fusion
   # Edit /var/lib/timestd/state/broadcast_calibration.json
   # Set "drift_ms_per_min": 0.0 for all stations.
   sudo systemctl start timestd-fusion
   ```

### "Stale TEC" Warning

If `verify_pipeline.sh` reports stale TEC:

1. **Check Night/Day:** TEC requires multi-frequency data. At night, MUF drops, and we often lose higher bands (15/20/25 MHz), making TEC calculation impossible.
2. **Action:** Wait for sunrise or check `timestd-physics` logs.
