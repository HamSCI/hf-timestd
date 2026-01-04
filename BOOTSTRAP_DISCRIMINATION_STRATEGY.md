# Bootstrap and Station Discrimination Strategy

**Date:** 2026-01-04  
**Status:** Documented from existing implementation

---

## A Priori Knowledge (Before Turning On Radio)

### 1. Geography
- **Receiver location:** Missouri (38.918°N, 92.128°W)
- **Station locations:**
  - WWV: Colorado (~1120 km, bearing ~280°)
  - CHU: Ottawa (~1522 km, bearing ~40°)
  - WWVH: Hawaii (~6600 km, bearing ~260°)
  - BPM: China (~11500 km, bearing ~320°)

### 2. Physics
- **Speed of light:** 299,792 km/s
- **Light-speed delays:**
  - WWV: 3.7 ms
  - CHU: 5.1 ms
  - WWVH: 22.0 ms
  - BPM: 38.3 ms
- **Ionospheric propagation:** 1-4 hops, adds delay
- **Frequency dependence:** 1/f² for ionospheric delay

### 3. Propagation Models
- **IRI-2020:** Dynamic ionospheric layer heights
- **VTEC readings:** Real-time ionospheric conditions
- **Ray tracing:** Geometric path calculation

### 4. Key Correlation Principle
**WWVH ToA correlation across frequencies >> WWV vs WWVH correlation on same frequency**

Example:
- WWVH at 2.5, 5, 10, 15 MHz: ToA = 23±2ms (tight correlation)
- WWV vs WWVH at 10 MHz: ToA differs by ~15ms (large separation)

---

## Bootstrap Strategy (Implemented in `timing_calibrator.py`)

### Phase 1: Anchor Channel Detection

**Priority channels (unambiguous station identification):**
```python
ANCHOR_CHANNELS = {
    'CHU 3.33 MHz',    # CHU-only frequency
    'CHU 7.85 MHz',    # CHU-only frequency  
    'CHU 14.67 MHz',   # CHU-only frequency
    'WWV 20 MHz',      # WWV-only frequency
    'WWV 25 MHz',      # WWV-only frequency
}
```

**Why these are optimal for bootstrap:**
1. **No ambiguity** - Only one station broadcasts on these frequencies
2. **Station ID is certain** - Detection = station identification
3. **Provides clean RTP offset** - No multi-station interference

**Bootstrap process:**
1. **Wide search** (±500ms) on anchor channels
2. **Detect strongest signal** (SNR > 20dB preferred)
3. **Measure ToA** - e.g., CHU at 7.85 MHz shows 10.2ms
4. **Calculate D_clock** - Using propagation model for CHU
5. **Establish RTP offset** - Now know receiver clock offset

**Result:** Preliminary D_clock estimate with high confidence

### Phase 2: Calibration (Narrow Search Windows)

Once anchor channel provides D_clock:
1. **Adjust RTP expectations** for all channels
2. **Narrow search windows** from ±500ms to ±5ms
3. **Update continuously** as more detections arrive

**Calibration criteria (from `timing_calibrator.py`):**
```python
# PROVISIONAL Mode (Fast Path - GPSDO-Validated)
PROVISIONAL_MIN_DETECTIONS = 10        # Quick operational use
PROVISIONAL_MIN_CONFIDENCE = 0.7       # Moderate confidence threshold
PROVISIONAL_MAX_RTP_VARIANCE = 50**2   # GPSDO stability check

# CALIBRATED Mode (Rigorous Path - Scientific)
BOOTSTRAP_MIN_DETECTIONS = 30          # Per station
BOOTSTRAP_MIN_STATIONS = 2             # Cross-validation
```

### Phase 3: Shared Channel Discrimination

Now tackle shared channels (2.5, 5, 10, 15 MHz) with multiple tools:

#### Primary: ToA Separation
- **WWV:** 8±2ms (short path)
- **WWVH:** 23±3ms (medium path)
- **BPM:** 45±5ms (long path)
- **Separation:** 15ms between WWV/WWVH, 22ms between WWVH/BPM

#### Secondary: Acoustic Discrimination
- **WWVH unique:** 1200 Hz tone (strong indicator)
- **WWV/WWVH:** 500/600 Hz tones (both have these)
- **BPM vs WWV:** Both use 1000 Hz, discriminate by ToA and BCD pattern

#### Tertiary: Cross-Frequency Correlation
- **WWVH consistency:** ToA at 2.5, 5, 10, 15 MHz should agree within ±3ms
- **WWV consistency:** ToA at 2.5, 5, 10, 15 MHz should agree within ±3ms
- **Validation:** If ToA varies >5ms across frequencies, suspect misidentification

---

## Conflict Resolution

### BPM vs WWV (Both use 1000 Hz tone)
**Challenge:** Acoustic similarity  
**Solution:**
1. **ToA difference:** 37ms separation (45ms - 8ms)
2. **Search window positioning:** Use D_clock from anchor to position windows
3. **BCD pattern:** Different timing patterns
4. **Tick structure:** BPM has different tick characteristics

**With calibrated D_clock:**
- WWV expected at: RTP_offset + 8ms
- BPM expected at: RTP_offset + 45ms
- Windows don't overlap, discrimination is reliable

### WWVH Detection
**Indicators:**
1. **Strong:** 1200 Hz tone present
2. **Medium:** ToA ≈ 23ms (vs WWV's 8ms)
3. **Weak:** 500/600 Hz tones (WWV also has these)

**Strategy:**
- If 1200 Hz detected → High confidence WWVH
- If only 500/600 Hz → Use ToA to discriminate from WWV
- Cross-validate with other frequencies

---

## Why Cross-Frequency Alone Isn't Sufficient

### Propagation Variability
1. **Frequency-dependent fading**
   - 10 MHz strong (SNR=9.8dB)
   - 5 MHz weak (SNR=2.1dB)
   - Can't correlate if signal absent

2. **Mode changes**
   - 5 MHz: 2-hop F-layer
   - 10 MHz: 1-hop E-layer
   - Different modes → different delays

3. **Ionospheric disturbances**
   - Solar flares affect frequencies differently
   - Sporadic E can appear/disappear
   - TEC variations

4. **Time of day**
   - 15 MHz unusable at night (no F-layer)
   - 2.5 MHz unusable during day (D-layer absorption)

### The Correct Hierarchy

**For station identification:**
1. **Primary:** Anchor channels (unambiguous)
2. **Secondary:** ToA separation (with calibrated D_clock)
3. **Tertiary:** Acoustic discrimination (1200 Hz, BCD, etc.)
4. **Quaternary:** Cross-frequency correlation (validation)

**For validation:**
1. **Primary:** Inter-station D_clock consistency (<5ms spread)
2. **Secondary:** Cross-frequency ToA consistency (<3ms variation)
3. **Tertiary:** Acoustic feature consistency

---

## Current System Implementation

### Bootstrap Flow (from `timing_calibrator.py`)

```python
def update_from_detection(
    self,
    station: str,
    frequency_mhz: float,
    channel_name: str,
    toa_ms: float,
    rtp_timestamp: int,
    confidence: float,
    snr_db: float
):
    """Update calibration from a detection."""
    
    # Check if this is an anchor channel
    is_anchor = channel_name in ANCHOR_CHANNELS
    
    if is_anchor:
        logger.info(f"🎯 ANCHOR detection: {station} @ {frequency_mhz}MHz, "
                   f"ToA={toa_ms:.2f}ms, SNR={snr_db:.1f}dB")
    
    # Calculate RTP offset
    rtp_offset = self._calculate_rtp_offset(
        toa_ms, rtp_timestamp, station, frequency_mhz
    )
    
    # Update station statistics
    self._update_station_stats(station, frequency_mhz, toa_ms, rtp_offset)
    
    # Check if we can transition from BOOTSTRAP to CALIBRATED
    if self.phase == CalibrationPhase.BOOTSTRAP:
        self._check_bootstrap_completion()
```

### Search Window Adaptation (from `phase2_temporal_engine.py`)

```python
# PRIORITY 1: Physics-based prediction (IRI-2020)
if predicted_station_name:
    expected_offset_ms = predicted_delay_ms
    adaptive_window_ms = 15.0  # Narrow window with physics

# PRIORITY 2: Learned ToA (after calibration)
if self.timing_calibrator:
    expected_toa = self.timing_calibrator.get_expected_toa(
        predicted_station_name, self.frequency_mhz, self.channel_name
    )
    if expected_toa is not None:
        expected_offset_ms = expected_toa
        adaptive_window_ms = 5.0  # Very narrow after calibration

# PRIORITY 3: Cross-frequency guidance (just deployed)
if search_strategy == "BLIND" and predicted_station_name:
    cross_freq_guidance = self.multi_station_detector.get_cross_freq_guidance(
        station=predicted_station_name,
        target_frequency_mhz=self.frequency_mhz,
        minute_boundary=minute_boundary
    )
    if cross_freq_guidance and cross_freq_guidance['source_snr_db'] > 10.0:
        expected_offset_ms = cross_freq_guidance['expected_toa_ms']
        adaptive_window_ms = cross_freq_guidance['search_window_ms']

# PRIORITY 4: Blind search (bootstrap)
if search_strategy == "BLIND":
    expected_offset_ms = 0.0
    adaptive_window_ms = 500.0  # Wide search
```

---

## Validation Strategy (Deployed 2026-01-04)

### Inter-Station D_clock Consistency
Once multiple stations detected on same channel:
```python
def _validate_inter_station_dclock_consistency(
    self,
    time_snap: TimeSnapResult,
    solutions: Dict[str, float]  # station -> d_clock_ms
) -> Tuple[bool, Optional[str]]:
    """
    Validate that all stations agree on D_clock.
    
    Key insight: D_clock is a property of the RECEIVER, not the station.
    All stations should measure the same D_clock (within measurement noise).
    """
    if len(solutions) < 2:
        return True, None  # Can't validate with single station
    
    d_clocks = list(solutions.values())
    mean_d_clock = np.mean(d_clocks)
    spread = max(d_clocks) - min(d_clocks)
    
    if spread > 5.0:  # CRITICAL threshold
        logger.error(f"CRITICAL: D_clock spread {spread:.1f}ms exceeds 5ms!")
        logger.error(f"  This indicates PROPAGATION DELAY CALCULATION ERRORS")
        return False, f"D_clock spread {spread:.1f}ms exceeds threshold"
    
    return True, None
```

### Cross-Frequency ToA Consistency
Validates same station across frequencies:
```python
# Expected: WWVH ToA at 2.5, 5, 10, 15 MHz should agree within ±3ms
# If variation > 5ms, suspect misidentification or mode error
```

---

## Summary

The system correctly implements the bootstrap strategy:

1. ✅ **Anchor channels prioritized** (CHU-only, WWV-only frequencies)
2. ✅ **Unambiguous station ID first** (no multi-station interference)
3. ✅ **D_clock from anchor propagates** (narrows all search windows)
4. ✅ **Shared channels use ToA separation** (primary discriminator)
5. ✅ **Acoustic features as secondary** (1200 Hz, BCD patterns)
6. ✅ **Cross-frequency as validation** (not primary discriminator)

**Key architectural principle:** Use what we know (geography, physics, propagation models) to guide what we measure (ToA, acoustic features), then validate with cross-frequency correlation.

This approach is robust to:
- Frequency-dependent fading
- Ionospheric disturbances
- Mode changes
- Time-of-day propagation variations

And provides:
- Rapid bootstrap (first detection on anchor channel)
- High-confidence station ID (unambiguous channels)
- Tight search windows (±5ms after calibration)
- Multi-layer validation (inter-station, cross-frequency, acoustic)
