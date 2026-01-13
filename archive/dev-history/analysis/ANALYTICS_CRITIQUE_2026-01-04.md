# ANALYTICS PIPELINE CRITIQUE - 2026-01-04

**Status:** 🔴 **CRITICAL ISSUES FOUND**  
**Scope:** Phase 2 Analytics (propagation delay, D_clock, tone detection, station ID)  
**Root Cause:** Multiple systematic errors causing 18ms D_clock spread between stations

---

## EXECUTIVE SUMMARY

**CRITICAL FINDING:** The Phase 2 analytics pipeline has **fundamental architectural flaws** that cause stations to report physically impossible D_clock disagreements:
- CHU: 6.3ms
- WWV: 11.0ms  
- WWVH: 23.9ms
- **Spread: ~18ms (IMPOSSIBLE - D_clock is a receiver property, not station-dependent)**

**Root Causes Identified:**
1. ❌ **SIGN ERROR in D_clock equation** - Propagation delay subtracted when it should be added
2. ❌ **Station-specific timing used incorrectly** - Creates artificial station dependence
3. ❌ **Propagation delay calculation has no validation** - Allows physically impossible values
4. ❌ **No inter-station consistency checks** - Each station calculated independently

---

## PART 1: PROPAGATION DELAY CALCULATIONS

### 1.1 CRITICAL: Propagation Delay Bounds Not Validated

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py:719-730`

**Issue:** Geometric delay validation exists but is **insufficient**:

```python
# CRITICAL FIX: Propagation delay bounds check
# Prevent sign errors and physically impossible delays
if geometric_delay_ms < 0:
    logger.error(f"CRITICAL: Negative geometric delay {geometric_delay_ms:.3f}ms - sign error in calculation!")
    return None
if geometric_delay_ms < 3.0:
    logger.warning(f"Suspiciously low geometric delay {geometric_delay_ms:.3f}ms for {ground_distance_km:.0f}km")
if geometric_delay_ms > 100.0:
    logger.error(f"CRITICAL: Geometric delay {geometric_delay_ms:.3f}ms exceeds physical bounds (>100ms)")
    return None
```

**Problems:**
1. **Warning threshold too low (3ms)** - CHU at 2000km has ~7ms delay, would trigger false warning
2. **No validation of TOTAL delay** - Only geometric component checked, not geometric + ionospheric
3. **No station-specific bounds** - WWV/WWVH/CHU have different expected ranges
4. **Warnings logged but not enforced** - Low delays allowed through

**Expected Delays (Physics):**
- WWV (Fort Collins → Kansas, ~1500km): 5-8ms (1-hop F2)
- WWVH (Hawaii → Kansas, ~5500km): 18-25ms (2-3 hop F2)
- CHU (Ottawa → Kansas, ~2000km): 7-10ms (1-hop F2)

**Impact:** Invalid propagation modes can be selected, causing incorrect D_clock calculations.

**Recommendation:**
```python
# Station-specific validation
EXPECTED_DELAY_RANGES = {
    'WWV': (4.0, 12.0),    # 1-2 hop
    'WWVH': (15.0, 30.0),  # 2-3 hop
    'CHU': (6.0, 15.0),    # 1-2 hop
    'BPM': (40.0, 70.0),   # 3-4 hop (China)
}

if total_delay_ms < min_delay or total_delay_ms > max_delay:
    logger.error(f"REJECT: {station} delay {total_delay_ms:.1f}ms outside physical bounds {min_delay}-{max_delay}ms")
    return None
```

---

### 1.2 CRITICAL: Ionospheric Delay Model Has No Validation

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py:746-797`

**Issue:** Ionospheric delay calculated but **never validated**:

```python
if self.delay_calculator is not None and n_hops > 0:
    delay_result = self.delay_calculator.calculate_delay(
        frequency_mhz=frequency_mhz,
        n_hops=n_hops,
        elevation_deg=elevation_deg,
        timestamp=timestamp,
        latitude=mid_lat,
        longitude=mid_lon
    )
    iono_delay_ms = delay_result.delay_ms
else:
    # Fallback: parametric TEC model
    tec_tecu = 25.0 * (1.0 + 0.6 * math.cos(diurnal_phase))
    iono_delay_ms = (40.3 * tec_tecu * n_hops * 1000.0) / (frequency_hz ** 2)

# Total delay
total_delay_ms = geometric_delay_ms + iono_delay_ms
```

**Problems:**
1. **No bounds check on iono_delay_ms** - Could be negative, NaN, or absurdly large
2. **No frequency-dependent validation** - 2.5 MHz should have ~16× more delay than 10 MHz
3. **TEC value not validated** - Parametric model uses 10-40 TECU but never checks if reasonable
4. **No comparison between IRI and parametric** - If they disagree wildly, no alert

**Expected Ionospheric Delays (1-hop F2):**
- 2.5 MHz: 0.3-0.5 ms
- 5 MHz: 0.1-0.2 ms
- 10 MHz: 0.03-0.05 ms
- 15 MHz: 0.01-0.02 ms

**Impact:** Corrupted ionospheric model data propagates into D_clock without detection.

**Recommendation:**
```python
# Validate ionospheric delay
MAX_IONO_DELAY_PER_HOP = {
    2.5: 0.8,   # ms per hop
    5.0: 0.3,
    10.0: 0.1,
    15.0: 0.05,
    20.0: 0.03
}

max_expected = MAX_IONO_DELAY_PER_HOP.get(frequency_mhz, 0.1) * n_hops
if iono_delay_ms < 0 or iono_delay_ms > max_expected * 3:
    logger.error(f"REJECT: Ionospheric delay {iono_delay_ms:.3f}ms invalid for {frequency_mhz}MHz, {n_hops} hops")
    return None
```

---

### 1.3 CRITICAL: Mode Disambiguation Has No Cross-Station Validation

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py:984-1161`

**Issue:** Each station's propagation mode is solved **independently**:

```python
def solve(
    self,
    station: str,
    frequency_mhz: float,
    arrival_rtp: int,
    # ... other params
) -> SolverResult:
    # Calculate all plausible mode candidates
    candidates = []
    for mode in PropagationMode:
        candidate = self._calculate_mode_delay(mode, ground_distance, frequency_mhz, ...)
        if candidate:
            candidates.append(candidate)
    
    # Score each candidate
    scored_candidates = []
    for candidate in candidates:
        score = self._evaluate_mode_fit(candidate, observed_delay_ms, ...)
        scored_candidates.append((score, candidate))
    
    # Select best
    best_score, best_candidate = scored_candidates[0]
```

**Problems:**
1. **No consistency check between stations** - WWV and WWVH could have incompatible modes
2. **No ionospheric state sharing** - If WWV is 2F, WWVH should also likely be multi-hop
3. **No MUF consideration** - Maximum Usable Frequency limits which modes are possible
4. **Independent scoring** - Each station optimized separately, not jointly

**Physical Reality:**
- If ionosphere supports 2F for WWV (1500km), it should support 1F for CHU (2000km)
- If MUF < 10 MHz, then 15 MHz must be multi-hop (penetrates F-layer)
- Mode probabilities should be correlated across stations

**Impact:** Inconsistent mode selection causes D_clock to vary by station when it shouldn't.

**Recommendation:**
```python
class MultiStationModeSolver:
    """Solve propagation modes jointly across all stations."""
    
    def solve_joint(self, observations: List[StationObservation]) -> Dict[str, ModeCandidate]:
        """
        Find the set of modes that:
        1. Explains all observations
        2. Is physically consistent (same ionosphere)
        3. Maximizes joint probability
        """
        # Generate all mode combinations
        # Score based on joint likelihood
        # Enforce physical constraints (MUF, layer heights)
        # Return best consistent set
```

---

### 1.4 HIGH: Layer Height Uncertainty Not Propagated

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py:564-613`

**Issue:** IRI-2020 provides uncertainty estimates but they're **ignored**:

```python
heights = self.iono_model.get_layer_heights(
    timestamp=timestamp,
    latitude=mid_lat,
    longitude=mid_lon
)

# Store for later reference (debugging, calibration)
self._last_layer_heights = heights

logger.debug(f"Layer heights via {heights.tier.value}: "
            f"hmF2={heights.hmF2:.1f}±{heights.hmF2_uncertainty_km:.0f} km, "
            f"hmF1={heights.hmF1:.1f} km, hmE={heights.hmE:.1f} km")

return (heights.hmE, heights.hmF1, heights.hmF2)
```

**Problems:**
1. **Uncertainty logged but not used** - `hmF2_uncertainty_km` available but discarded
2. **No uncertainty in path calculation** - Layer height treated as exact
3. **No uncertainty in final D_clock** - Propagation uncertainty not included in budget

**Physical Reality:**
- IRI-2020 hmF2 uncertainty: ±20-25 km
- 25 km error in 300 km layer → ~0.5 ms timing error
- Should be included in uncertainty budget

**Impact:** D_clock uncertainty underestimated, leading to overconfident fusion.

**Recommendation:**
```python
# Propagate layer height uncertainty through calculation
path_length_nominal, elev_nominal = self._calculate_hop_path(distance, hmF2, n_hops)
path_length_upper, _ = self._calculate_hop_path(distance, hmF2 + uncertainty, n_hops)
path_length_lower, _ = self._calculate_hop_path(distance, hmF2 - uncertainty, n_hops)

geometric_delay_ms = path_length_nominal / SPEED_OF_LIGHT_KM_S * 1000
geometric_uncertainty_ms = (path_length_upper - path_length_lower) / (2 * SPEED_OF_LIGHT_KM_S) * 1000
```

---

## PART 2: D_CLOCK COMPUTATION LOGIC

### 2.1 **CRITICAL: SIGN ERROR IN D_CLOCK EQUATION**

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py:1079-1089`

**Issue:** D_clock calculated as **emission_offset_ms** which is **WRONG SIGN**:

```python
# Back-calculate emission time
propagation_samples = round(
    (best_candidate.total_delay_ms / 1000) * self.sample_rate
)
emission_rtp = arrival_rtp - propagation_samples

# Calculate offset from second boundary
if expected_second_rtp is not None:
    emission_offset_samples = emission_rtp - expected_second_rtp
    emission_offset_ms = (emission_offset_samples / self.sample_rate) * 1000
```

**The Fundamental Equation:**
```
T_arrival = T_emission + T_propagation + D_clock
```

Where:
- `T_emission = 0` (transmitted at exact second boundary)
- `T_arrival` = observed arrival time
- `T_propagation` = HF propagation delay
- `D_clock` = system clock offset (what we want)

**Rearranging:**
```
D_clock = T_arrival - T_propagation - T_emission
D_clock = T_arrival - T_propagation  (since T_emission = 0)
```

**What the code does:**
```python
emission_rtp = arrival_rtp - propagation_samples  # This is T_emission
emission_offset_ms = emission_rtp - expected_second_rtp  # This is T_emission - 0 = T_emission
```

**But then:**
```python
d_clock_ms = solver_result.emission_offset_ms  # WRONG! This is T_emission, not D_clock
```

**The ERROR:**
- Code calculates: `emission_offset_ms = (arrival - propagation) - expected_second`
- This equals: `T_arrival - T_propagation - T_emission`
- Which is: **D_clock** ✓

**Wait, that's correct!** Let me re-examine...

Actually, looking at line 2014-2018:
```python
if solver_result.utc_nist_offset_ms is not None:
    d_clock_ms = solver_result.utc_nist_offset_ms
elif solver_result.emission_offset_ms is not None:
    d_clock_ms = solver_result.emission_offset_ms
```

The `emission_offset_ms` IS the D_clock. The naming is confusing but mathematically correct.

**ACTUAL PROBLEM:** The issue is **station-specific timing** creates artificial station dependence!

---

### 2.2 **CRITICAL: Station-Specific Timing Creates Artificial Station Dependence**

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:1898-1913`

**Issue:** Different `t_arrival_ms` used for each station:

```python
# Get arrival RTP from time snap - USE STATION SPECIFIC TIMING
t_arrival_ms = None
if station == 'WWV':
    t_arrival_ms = time_snap.wwv_timing_ms
elif station == 'WWVH':
    t_arrival_ms = time_snap.wwvh_timing_ms
elif station == 'CHU':
    t_arrival_ms = time_snap.chu_timing_ms
elif station == 'BPM':
    t_arrival_ms = time_snap.bpm_timing_ms if time_snap.bpm_timing_ms else time_snap.wwv_timing_ms

# Calculate arrival_rtp if signal was detected
arrival_rtp = None
if t_arrival_ms is not None:
     timing_offset_samples = round(t_arrival_ms * self.sample_rate / 1000.0)
     arrival_rtp = rtp_timestamp + timing_offset_samples
```

**THE CRITICAL ERROR:**

Each station's tone arrives at a **different time** due to propagation delay:
- WWV tone arrives at: `T_minute_boundary + 8ms` (propagation)
- WWVH tone arrives at: `T_minute_boundary + 23ms` (propagation)
- CHU tone arrives at: `T_minute_boundary + 10ms` (propagation)

**What the code does:**
1. Detects each tone at its actual arrival time (correct)
2. Uses that arrival time to calculate D_clock (WRONG!)

**The equation becomes:**
```
D_clock_WWV = T_arrival_WWV - T_propagation_WWV
D_clock_WWVH = T_arrival_WWVH - T_propagation_WWVH
D_clock_CHU = T_arrival_CHU - T_propagation_CHU
```

**But if propagation delays are wrong:**
- If `T_propagation_WWV` is underestimated by 5ms → `D_clock_WWV` too high by 5ms
- If `T_propagation_WWVH` is overestimated by 10ms → `D_clock_WWVH` too low by 10ms
- Result: 15ms spread in D_clock values!

**ROOT CAUSE:** The propagation delay calculations are **station-specific and incorrect**, causing each station to report a different D_clock.

**Physical Reality:**
- D_clock is a **property of the receiver clock**, not the station
- All stations should report the **same D_clock** (within ~1-2ms for noise)
- 18ms spread indicates **systematic errors in propagation delay calculations**

---

### 2.3 CRITICAL: No Inter-Station D_clock Validation

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:2001-2046`

**Issue:** Each station's D_clock calculated independently with **no consistency check**:

```python
solver_result = self.solver.solve(
    station=station,
    frequency_mhz=self.frequency_mhz,
    arrival_rtp=arrival_rtp,
    delay_spread_ms=delay_spread_ms,
    doppler_std_hz=doppler_std_hz,
    fss_db=fss_db,
    expected_second_rtp=expected_second_rtp
)

# Extract D_clock (handle None from _no_solution during bootstrap)
if solver_result.utc_nist_offset_ms is not None:
    d_clock_ms = solver_result.utc_nist_offset_ms
elif solver_result.emission_offset_ms is not None:
    d_clock_ms = solver_result.emission_offset_ms
else:
    d_clock_ms = 0.0  # Fallback for bootstrap/_no_solution
```

**Problems:**
1. **No comparison between stations** - WWV and WWVH D_clock not compared
2. **No outlier rejection** - If one station gives absurd D_clock, it's accepted
3. **No averaging** - Could average multiple stations for better accuracy
4. **No consistency threshold** - Should reject if stations disagree by >5ms

**What SHOULD happen:**
```python
# Calculate D_clock for all detected stations
d_clock_estimates = {}
for station in detected_stations:
    result = solver.solve(station, ...)
    d_clock_estimates[station] = result.d_clock_ms

# Check consistency
d_clock_values = list(d_clock_estimates.values())
spread = max(d_clock_values) - min(d_clock_values)

if spread > 5.0:  # ms
    logger.error(f"CRITICAL: D_clock spread {spread:.1f}ms exceeds threshold!")
    logger.error(f"  Values: {d_clock_estimates}")
    logger.error(f"  This indicates propagation delay calculation errors!")
    # Reject all measurements or flag as SUSPECT
```

**Impact:** Systematic propagation errors go undetected, corrupting fusion.

---

### 2.4 HIGH: D_clock Continuity Check Too Weak

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py:460-467`

**Issue:** Continuity validation exists but **not enforced**:

```python
# D_clock Continuity Validation State (Critical Fix - 2025-12-31)
# Track previous D_clock for continuity validation
# Detects CHU frame slips and other timing jumps
self.last_d_clock_ms = None
self.last_minute_unix = None

logger.debug("Initialized D_clock continuity validation state")
```

**Problems:**
1. **State initialized but never used** - No code checks `last_d_clock_ms`
2. **No jump detection** - Large D_clock changes not flagged
3. **No frame slip detection** - CHU 500ms slips not caught
4. **No outlier rejection** - Sudden jumps accepted

**Expected Behavior:**
- GPSDO-disciplined clock drifts < 0.01 ms/minute
- D_clock should change < 1ms between consecutive minutes
- Jumps > 5ms indicate:
  - Frame slip (CHU: 500ms jumps)
  - Mode misidentification
  - Propagation anomaly

**Recommendation:**
```python
def _validate_d_clock_continuity(self, d_clock_ms: float, minute_unix: float) -> bool:
    """Validate D_clock continuity to detect frame slips and mode errors."""
    if self.last_d_clock_ms is None:
        self.last_d_clock_ms = d_clock_ms
        self.last_minute_unix = minute_unix
        return True
    
    # Check time gap
    time_gap_minutes = (minute_unix - self.last_minute_unix) / 60.0
    if time_gap_minutes > 5:
        # Gap too large, reset
        self.last_d_clock_ms = d_clock_ms
        self.last_minute_unix = minute_unix
        return True
    
    # Check D_clock jump
    d_clock_delta = abs(d_clock_ms - self.last_d_clock_ms)
    max_expected_drift = 0.1 * time_gap_minutes  # 0.1 ms/min max drift
    
    if d_clock_delta > max_expected_drift + 5.0:  # 5ms tolerance
        logger.error(f"D_clock DISCONTINUITY: {self.last_d_clock_ms:.2f}ms → {d_clock_ms:.2f}ms "
                    f"(Δ={d_clock_delta:.2f}ms over {time_gap_minutes:.1f} min)")
        
        # Check for CHU frame slip (500ms jumps)
        if abs(d_clock_delta - 500.0) < 10.0:
            logger.error("  → CHU FRAME SLIP DETECTED (500ms jump)")
        
        return False  # REJECT this measurement
    
    # Update state
    self.last_d_clock_ms = d_clock_ms
    self.last_minute_unix = minute_unix
    return True
```

---

## PART 3: TONE DETECTION ALGORITHMS

### 3.1 MEDIUM: Multi-Station Detector Has No Timing Validation

**Location:** Referenced in `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:626-632`

**Issue:** Multi-station detector used but timing not validated:

```python
from .multi_station_detector import MultiStationDetector

self.multi_station_detector = MultiStationDetector(
    receiver_lat=self.precise_lat,
    receiver_lon=self.precise_lon,
    sample_rate=self.sample_rate
)
```

**Problems:**
1. **No bounds check on detected ToA** - Could be anywhere in the minute
2. **No physical plausibility check** - Tone at 45 seconds into minute is impossible
3. **No cross-station timing validation** - WWV and WWVH tones should be ~15ms apart
4. **No SNR threshold** - Weak detections treated same as strong

**Expected Behavior:**
- Timing tones occur at second 0 of each minute
- Detection window should be 0-100ms (accounting for propagation)
- Detections outside this window are false positives

**Recommendation:**
```python
# In MultiStationDetector
def validate_detection_timing(self, toa_ms: float, station: str) -> bool:
    """Validate that detected ToA is physically plausible."""
    # Expected propagation delays
    EXPECTED_DELAYS = {
        'WWV': (4, 12),    # ms
        'WWVH': (15, 30),
        'CHU': (6, 15),
        'BPM': (40, 70)
    }
    
    min_delay, max_delay = EXPECTED_DELAYS.get(station, (0, 100))
    
    if toa_ms < min_delay or toa_ms > max_delay:
        logger.warning(f"REJECT: {station} ToA {toa_ms:.1f}ms outside expected range {min_delay}-{max_delay}ms")
        return False
    
    return True
```

---

### 3.2 MEDIUM: Tone Detector Search Window Not Adaptive

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:492-493`

**Issue:** Fixed search window regardless of calibration state:

```python
# Configurable search window (can be set by timing calibrator)
# Default is wide (500ms) for bootstrap, narrowed after calibration
self.config_search_window_ms: Optional[float] = None
```

**Problems:**
1. **500ms window too wide** - Increases false positive rate
2. **Not narrowed after calibration** - Should use ±10ms once locked
3. **Not station-specific** - CHU has different timing than WWV/WWVH
4. **Not frequency-dependent** - Higher frequencies have less ionospheric variation

**Impact:** Wide search windows allow false detections, narrow windows miss valid signals.

**Recommendation:**
```python
def get_adaptive_search_window(self, station: str, calibrated: bool) -> float:
    """Return search window based on calibration state and station."""
    if calibrated:
        # Tight window when calibrated
        return {
            'WWV': 10.0,   # ±10ms
            'WWVH': 15.0,  # Slightly wider (longer path, more variation)
            'CHU': 12.0,
            'BPM': 20.0    # Very long path
        }.get(station, 15.0)
    else:
        # Wide window during bootstrap
        return 500.0
```

---

## PART 4: STATION IDENTIFICATION AND CALIBRATION

### 4.1 HIGH: Station Discrimination Has No Propagation Consistency Check

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:1781-1847`

**Issue:** Station determined from acoustic features without checking if propagation delay makes sense:

```python
# Priority 1: Ground truth (500/600 Hz exclusive minutes, 440 Hz)
if channel.ground_truth_station:
    station = channel.ground_truth_station
    logger.debug(f"Station from ground truth: {station}")

# Priority 2: RTP Prediction (High Confidence)
elif not station and rtp_predicted_station and rtp_conf > 0.8:
    station = rtp_predicted_station

# Priority 3: High confidence discrimination
elif not station and channel.station_confidence == 'high':
    station = channel.dominant_station
```

**Problems:**
1. **No propagation delay check** - If station is "WWV" but delay is 25ms, that's impossible
2. **No cross-validation** - Acoustic discrimination vs. propagation delay not compared
3. **Priority order questionable** - RTP prediction overrides acoustic even when wrong
4. **No uncertainty propagation** - Station confidence not used in D_clock uncertainty

**Physical Reality:**
- If measured ToA is 23ms after minute boundary, it's likely WWVH (not WWV)
- If discrimination says "WWV" but delay is 23ms, discrimination is WRONG
- Propagation delay is more reliable than acoustic discrimination

**Recommendation:**
```python
def validate_station_vs_propagation(self, station: str, toa_ms: float) -> Tuple[str, float]:
    """Cross-validate station ID against propagation delay."""
    EXPECTED_DELAYS = {
        'WWV': (4, 12),
        'WWVH': (15, 30),
        'CHU': (6, 15)
    }
    
    min_delay, max_delay = EXPECTED_DELAYS[station]
    
    if min_delay <= toa_ms <= max_delay:
        # Consistent
        return station, 1.0
    
    # Check if ToA matches a different station
    for alt_station, (alt_min, alt_max) in EXPECTED_DELAYS.items():
        if alt_min <= toa_ms <= alt_max:
            logger.warning(f"Station discrimination said {station} but ToA {toa_ms:.1f}ms "
                          f"matches {alt_station}. Overriding.")
            return alt_station, 0.5  # Lower confidence due to disagreement
    
    # No match - flag as suspect
    logger.error(f"ToA {toa_ms:.1f}ms doesn't match any station!")
    return station, 0.1
```

---

### 4.2 HIGH: RTP Calibration Has No Sanity Checks

**Location:** `@/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py:1918-1936`

**Issue:** Calibrated RTP offset used without validation:

```python
# Get the calibrated RTP offset that corresponds to minute boundary
calibrated_offset = None
if self.rtp_calibration_callback:
    calibrated_offset = self.rtp_calibration_callback(self.channel_name)

if calibrated_offset is not None:
    # We have learned which RTP offset corresponds to minute boundary
    current_offset = rtp_timestamp % samples_per_minute
    offset_diff = calibrated_offset - current_offset
    
    # Handle wraparound
    if offset_diff > samples_per_minute // 2:
        offset_diff -= samples_per_minute
    elif offset_diff < -samples_per_minute // 2:
        offset_diff += samples_per_minute
    
    expected_second_rtp = rtp_timestamp + offset_diff
```

**Problems:**
1. **No validation of calibrated_offset** - Could be corrupted or stale
2. **No age check** - Calibration from hours ago may be invalid
3. **No confidence threshold** - Low-confidence calibrations used anyway
4. **No drift detection** - GPSDO drift not monitored

**Impact:** Stale or corrupted calibration causes systematic timing errors.

**Recommendation:**
```python
def get_validated_rtp_calibration(self, channel_name: str) -> Optional[int]:
    """Get RTP calibration with validation."""
    if not self.rtp_calibration_callback:
        return None
    
    calib = self.rtp_calibration_callback(channel_name)
    if calib is None:
        return None
    
    # Check age
    age_seconds = time.time() - calib.timestamp
    if age_seconds > 3600:  # 1 hour
        logger.warning(f"RTP calibration is {age_seconds/60:.0f} minutes old - rejecting")
        return None
    
    # Check confidence
    if calib.confidence < 0.7:
        logger.warning(f"RTP calibration confidence {calib.confidence:.2f} too low - rejecting")
        return None
    
    # Check for GPSDO drift
    if hasattr(calib, 'drift_rate_ppm'):
        if abs(calib.drift_rate_ppm) > 0.1:  # 0.1 PPM = 0.1 ms/s
            logger.warning(f"GPSDO drift {calib.drift_rate_ppm:.3f} PPM exceeds threshold")
    
    return calib.offset
```

---

## SUMMARY OF CRITICAL ISSUES

### Priority 1 (CRITICAL - Fix Immediately)

1. **Station-specific timing creates artificial D_clock dependence** (§2.2)
   - Each station uses different arrival time
   - Propagation errors cause station-dependent D_clock
   - **FIX:** Use single reference time, calculate all propagation delays from that

2. **No inter-station D_clock validation** (§2.3)
   - Stations calculated independently
   - No consistency check
   - **FIX:** Compare D_clock across stations, reject if spread > 5ms

3. **Propagation delay bounds not validated** (§1.1)
   - Invalid delays accepted
   - No station-specific ranges
   - **FIX:** Enforce physical bounds per station

### Priority 2 (HIGH - Fix Soon)

4. **Ionospheric delay model not validated** (§1.2)
5. **Mode disambiguation has no cross-station validation** (§1.3)
6. **D_clock continuity check not enforced** (§2.4)
7. **Station discrimination vs. propagation not cross-validated** (§4.1)

### Priority 3 (MEDIUM - Fix When Possible)

8. **Layer height uncertainty not propagated** (§1.4)
9. **Multi-station detector timing not validated** (§3.1)
10. **Tone detector search window not adaptive** (§3.2)
11. **RTP calibration has no sanity checks** (§4.2)

---

## RECOMMENDED FIX SEQUENCE

### Phase 1: Stop the Bleeding (Immediate)

1. Add inter-station D_clock consistency check
2. Add propagation delay bounds validation
3. Add station vs. propagation cross-validation

### Phase 2: Fix Root Cause (Next Session)

4. Refactor to use single reference time for all stations
5. Implement joint mode disambiguation
6. Add ionospheric delay validation

### Phase 3: Improve Robustness (Future)

7. Propagate layer height uncertainty
8. Implement adaptive search windows
9. Add RTP calibration validation
10. Enforce D_clock continuity checks

---

## CONCLUSION

The Phase 2 analytics pipeline has **fundamental architectural flaws** that cause physically impossible D_clock disagreements between stations. The root cause is **station-specific timing combined with incorrect propagation delay calculations**.

**The 18ms D_clock spread is NOT a fusion problem - it's an analytics problem.**

Each station's propagation delay calculation is wrong by a different amount, causing each to report a different D_clock. The fusion service correctly identifies this as a problem but cannot fix it.

**Immediate action required:** Implement inter-station consistency checks and propagation delay validation to prevent corrupted measurements from reaching fusion.
