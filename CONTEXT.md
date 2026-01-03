# HF Time Standard Analysis - Project Context

**Last Updated:** January 3, 2026  
**Version:** 3.9.1 (Station Discrimination Fix Deployed)  
**Status:** Production (9 channels running at AC0G, 24kHz Sample Rate)

## Quick Reference

**What:** Precision HF timing system extracting D_clock measurements from WWV/WWVH/CHU/BPM broadcasts  
**Where:** `/opt/hf-timestd` (production) or `/home/mjh/git/hf-timestd` (development)  
**Services:** `timestd-core-recorder`, `timestd-analytics`, `timestd-fusion`, `timestd-vtec`, `timestd-web-ui`, `timestd-science-aggregator`, `timestd-radiod-monitor`  
**Web UI (FastAPI):** <http://localhost:8000> (replaces legacy port 3000)  
**Data Root:** `/var/lib/timestd/phase2/`

---

## Current State (Jan 3, 2026)

### ✅ Recently Completed

#### v3.9.1 - Station Discrimination Fix (Jan 3, 2026 19:41 UTC)

**Critical Fix:** Corrected analytics service to eliminate invalid station/frequency combinations

**Changes:**
1. **Broadcast Schedule Validation** - Added constants defining valid station/frequency combinations
2. **Frequency-Aware Discrimination** - Skip BCD discrimination on station-specific frequencies (20, 25, 3.33, 7.85, 14.67 MHz)
3. **Final Station Validation** - Reject physically impossible combinations (e.g., WWVH at 20/25 MHz)
4. **Validation Script** - Created `scripts/validate_station_discrimination.py` for automated testing

**Results:**
- **Before:** 808 invalid WWVH detections at 20/25 MHz in 24 hours (7.4% of measurements)
- **After:** Zero invalid combinations, 100% correct station labeling on station-specific frequencies
- **Performance:** ~40% CPU savings on BCD discrimination (5 of 9 channels skip it)

**Documentation:** `docs/changes/SESSION_2026_01_03_STATION_DISCRIMINATION_FIX.md`

**Files Modified:**
- `src/hf_timestd/core/wwv_constants.py` - Added broadcast schedule constants
- `src/hf_timestd/core/phase2_analytics_service.py` - Frequency-aware discrimination logic
- `src/hf_timestd/core/phase2_temporal_engine.py` - Skip BCD on station-specific frequencies, final validation
- `scripts/validate_station_discrimination.py` - Validation tool (NEW)

#### v3.9.0 - Phase 1 & Phase 2 Web UI (Jan 3, 2026)

1. **Phase 1 & Phase 2 FastAPI Web UI Complete**
    * **Framework:** FastAPI with Pydantic models, modular routers
    * **Location:** `/home/mjh/git/hf-timestd/web-api/`
    * **Server:** `http://localhost:8000` (auto-reload enabled)
    * **Status:** ✅ Production-ready, pulling real HDF5 data

2. **Phase 1 Pages (Basic Monitoring)**
    * **Station Overview** (`/`): Metadata, recent activity, quick links
    * **System Health** (`/health`): Process status, channel status, disk usage
    * **Metrology Dashboard** (`/metrology`): 
        * Latest fusion timing with ISO GUM uncertainty breakdown
        * Allan Deviation analysis (τ=1s to 10,000s)
        * Noise identification (white, flicker, random walk)
        * Fusion history and uncertainty plots

3. **Phase 2 Pages (Advanced Analysis)**
    * **Propagation Analysis** (`/propagation`):
        * **Per-broadcast propagation modes** (not misleading global aggregation)
        * **Multi-frequency comparison by station** (WWV, WWVH, CHU, BPM)
        * **Per-path TEC visualization** with error bars and quality indicators
        * **Propagation mode timeline** (color-coded by mode)
        * **Validated broadcast schedules** (filters impossible combinations)
    * **Features:**
        * Auto-refresh (60s)
        * Time range selection (6h, 24h)
        * Quality metrics and uncertainty quantification
        * Responsive Plotly.js visualizations

4. **Data Quality Enhancements**
    * **Broadcast validation:** Rejects WWVH @ 20/25 MHz (doesn't broadcast there)
    * **Station-specific frequencies:** 20/25 MHz = WWV only, CHU frequencies = CHU only
    * **Shared frequencies:** 2.5, 5, 10, 15 MHz = WWV, WWVH, BPM (discrimination required)
    * **TEC uncertainty:** Error bars, quality flags, confidence levels, multi-frequency validation

### 📊 Deployment Status

* **Services:** All 7 services active
* **Pipeline:** HDF5-native flow functional from L0 to L3
* **Web UI:** FastAPI server operational, serving real-time data
* **Data Quality:** Broadcast validation active, per-path analysis implemented

---

## 🎯 Next Session Priority: WWV/WWVH Test Signal Analysis

**Goal:** Extract maximum scientific value from WWV/WWVH test signals (minutes 8 and 44) for ionospheric research and propagation analysis.

**Context:**  
WWV and WWVH broadcast special test signals during minutes 8 and 44 of each hour. These signals provide unique opportunities for:
1. **Ionospheric absorption measurement** (D-layer characterization)
2. **Propagation mode identification** (E-layer vs F-layer discrimination)
3. **Path loss analysis** (frequency-dependent attenuation)
4. **Multipath detection** (delay spread and coherence analysis)

Currently, the system detects test signals and uses them for mode disambiguation in the transmission time solver, but we're not extracting their full scientific potential.

### Test Signal Characteristics

**WWV Test Signal (Minute 8 and 44):**
- **Duration:** 45 seconds (second 10 through second 54)
- **Frequencies:** 2.5, 5, 10, 15, 20, 25 MHz
- **Format:** Continuous 500 Hz tone (no modulation)
- **Purpose:** Propagation testing, receiver calibration

**WWVH Test Signal (Minute 8 and 44):**
- **Duration:** 45 seconds (second 10 through second 54)
- **Frequencies:** 2.5, 5, 10, 15 MHz
- **Format:** Continuous 600 Hz tone (no modulation)
- **Purpose:** Propagation testing, receiver calibration

**Current Implementation:**
- Test signals detected in `wwvh_discrimination.py` (Vote 5)
- Field Strength Stability (FSS) calculated from tone amplitude
- FSS used for mode disambiguation (D-layer absorption indicator)
- **Limited to:** Single FSS value per minute, used only for mode solving

### Opportunities for Enhancement

#### 1. ✅ Station Discrimination Fix (COMPLETED)

**Status:** Deployed Jan 3, 2026 19:41 UTC  
**Impact:** Eliminated 808 invalid WWVH detections at 20/25 MHz per 24 hours

#### 2. Test Signal Scientific Data Products (NEW PRIORITY)

**Goal:** Extract comprehensive ionospheric and propagation data from WWV/WWVH test signals.

**Current State:**
- Test signal detection: ✅ Working (Vote 5 in discrimination)
- FSS calculation: ✅ Working (single value per minute)
- Mode disambiguation: ✅ Working (uses FSS for D-layer absorption)
- **Scientific data products:** ❌ Not implemented

**Proposed Enhancements:**

**A. Frequency-Dependent Field Strength Analysis**
- **Current:** Single FSS value aggregated across all frequencies
- **Enhanced:** Per-frequency field strength measurements
- **Scientific Value:**
  - Frequency-dependent absorption (D-layer characterization)
  - Critical frequency identification (MUF estimation)
  - Propagation mode transitions (E→F layer)
  - Solar flare detection (sudden ionospheric disturbances)

**B. Time-Series Coherence Analysis**
- **Current:** 45-second test signal treated as single measurement
- **Enhanced:** Per-second coherence and stability tracking
- **Scientific Value:**
  - Ionospheric scintillation detection
  - Multipath fading characterization
  - Propagation mode stability
  - Coherence time estimation (critical for communication systems)

**C. Comparative Station Analysis**
- **Current:** WWV and WWVH processed independently
- **Enhanced:** Differential analysis of WWV vs WWVH test signals
- **Scientific Value:**
  - Path-dependent propagation differences
  - Azimuthal ionospheric variations
  - Station-specific absorption patterns
  - Validation of propagation models

**D. Test Signal Event Detection**
- **Current:** No event detection on test signals
- **Enhanced:** Automated detection of anomalous conditions
- **Scientific Value:**
  - Solar flare detection (sudden amplitude drops)
  - Sporadic E detection (sudden amplitude increases)
  - Geomagnetic storm effects
  - Propagation mode changes

**Implementation Approach:**

1. **Enhance Test Signal Detection** (`wwvh_discrimination.py`)
   - Extract per-frequency field strength (not just aggregated FSS)
   - Calculate per-second coherence during 45-second test window
   - Store time-series data (not just summary statistics)

2. **Create Test Signal Data Product** (New L2 product)
   - Schema: `l2_test_signal_v1.json`
   - Fields:
     - `timestamp_utc`: Test signal start time
     - `station`: WWV or WWVH
     - `frequency_mhz`: Broadcast frequency
     - `field_strength_db`: Per-frequency field strength
     - `coherence_time_sec`: Measured coherence time
     - `stability_metric`: Amplitude stability over 45 seconds
     - `multipath_detected`: Boolean flag
     - `delay_spread_ms`: Multipath delay spread
     - `snr_db`: Signal-to-noise ratio
     - `quality_flag`: GOOD/MARGINAL/BAD

3. **Aggregate to L3 Science Products**
   - **Ionospheric Absorption** (L3B product)
     - Frequency-dependent absorption coefficients
     - D-layer characterization
     - Solar flare detection
   - **Propagation Stability** (L3B product)
     - Coherence time statistics
     - Scintillation indices
     - Multipath metrics

4. **Web UI Visualization**
   - Test signal timeline (color-coded by quality)
   - Frequency-dependent field strength plots
   - Coherence time vs. frequency
   - Comparative WWV/WWVH analysis

**Scientific Impact:**
- **Ionospheric Research:** Continuous D-layer monitoring, solar flare detection
- **Propagation Studies:** Mode identification, path loss analysis
- **Communication Planning:** Frequency selection, link budget estimation
- **Space Weather:** Real-time ionospheric disturbance detection

#### 3. TEC Calculation Aggregates Frequencies Incorrectly (MEDIUM PRIORITY)

**Problem:** Current TEC calculation aggregates all frequencies from a station into a single TEC value, but **different frequencies take different propagation paths** through the ionosphere.

**Physical Reality:**
* **2.5 MHz:** E-layer refraction (~150 km altitude), short path → TEC_low
* **10 MHz:** F-layer refraction (~300 km altitude), medium path → TEC_mid
* **20 MHz:** High F-layer or multi-hop (~400+ km), long path → TEC_high
* Where TEC_low < TEC_mid < TEC_high

**Current Implementation:**
* CHU: TEC from (3.33, 7.85, 14.67 MHz) → single aggregated TEC value
* WWV: TEC from (2.5, 5, 10, 15, 20, 25 MHz) → single aggregated TEC value
* Result: "Average path" TEC that doesn't represent any actual propagation path

**Correct Implementation:**
* Calculate TEC for **each frequency pair** separately:
    * CHU: (3.33-7.85 MHz), (7.85-14.67 MHz), (3.33-14.67 MHz)
    * WWV: (2.5-5 MHz), (5-10 MHz), (10-15 MHz), (15-20 MHz), (20-25 MHz)
* Store per-frequency-pair TEC values with specific frequencies labeled
* Enable validation: if all pairs give similar TEC → good; if divergent → multipath/mode mixing

**Benefits:**
* Physically meaningful TEC values representing actual propagation paths
* Frequency-dependent TEC reveals ionospheric structure
* Better validation of propagation models
* Enables detection of multipath and mode mixing

### Key Files to Modify

#### Test Signal Scientific Analysis (HIGH PRIORITY)

* **Primary File:** `src/hf_timestd/core/wwvh_discrimination.py`
  * Lines ~600-650: `detect_test_signal()` method (Vote 5)
  * **Current Implementation:**
    - Detects 500 Hz (WWV) or 600 Hz (WWVH) tone during minutes 8 and 44
    - Calculates single FSS value from tone amplitude
    - Returns FSS for mode disambiguation
  * **Enhancements Needed:**
    1. Extract per-frequency field strength (not aggregated)
    2. Calculate per-second coherence during 45-second window
    3. Measure delay spread and multipath indicators
    4. Detect anomalous conditions (solar flares, sporadic E)
    5. Return comprehensive test signal metrics

* **New File:** `src/hf_timestd/core/test_signal_analyzer.py` (CREATE)
  * **Purpose:** Dedicated test signal analysis module
  * **Methods:**
    - `analyze_test_signal(iq_samples, sample_rate, frequency_mhz, station)` → TestSignalMetrics
    - `calculate_field_strength(tone_amplitude, frequency_mhz)` → float (dB)
    - `measure_coherence_time(iq_samples, tone_freq)` → float (seconds)
    - `detect_multipath(iq_samples, tone_freq)` → (bool, delay_spread_ms)
    - `detect_anomalies(field_strength_timeseries)` → AnomalyFlags
  * **Returns:** Comprehensive TestSignalMetrics dataclass

* **Schema File:** `src/hf_timestd/schemas/l2_test_signal_v1.json` (CREATE)
  * **Purpose:** Define L2 test signal data product schema
  * **Fields:** timestamp_utc, station, frequency_mhz, field_strength_db, coherence_time_sec, stability_metric, multipath_detected, delay_spread_ms, snr_db, quality_flag

* **Integration:** `src/hf_timestd/core/phase2_analytics_service.py`
  * Add test signal analysis call during minutes 8 and 44
  * Write results to L2 test signal HDF5 files
  * Pass comprehensive metrics to temporal engine

#### TEC Calculation Improvement (MEDIUM PRIORITY)

* **Primary File:** `src/hf_timestd/core/tec_estimator.py`
  * Current: `estimate_tec()` aggregates all frequencies
  * **Changes Needed:**
    1. Add `estimate_tec_pairwise()` method for frequency-pair TEC
    2. Calculate TEC for each frequency pair separately
    3. Return list of (freq1, freq2, tec, uncertainty) tuples
    4. Validate consistency across pairs

* **Science Aggregator:** `src/hf_timestd/core/science_aggregator.py`
  * Update to call pairwise TEC estimation
  * Store per-frequency-pair results in HDF5
  * Add frequency pair labels to output

* **HDF5 Schema:** `src/hf_timestd/io/schemas/L3_tec.yaml`
  * **Current:** `frequencies_mhz: "3.33,7.85,14.67"` (aggregated)
  * **New:** Add fields:
    * `frequency_pair_mhz: "3.33-7.85"` (specific pair used)
    * `frequency_low_mhz: 3.33`
    * `frequency_high_mhz: 7.85`
  * Enables per-pair TEC tracking and frequency-dependent analysis

### Implementation Plan

#### Phase 1: Test Signal Scientific Analysis (HIGH PRIORITY - NEXT SESSION)

**Goal:** Extract comprehensive ionospheric and propagation data from WWV/WWVH test signals.

**Step 1: Create Test Signal Analyzer Module**
1. **Create `src/hf_timestd/core/test_signal_analyzer.py`:**
   - `TestSignalMetrics` dataclass for comprehensive results
   - `analyze_test_signal()` main analysis function
   - Per-frequency field strength calculation
   - Per-second coherence measurement
   - Multipath detection and delay spread
   - Anomaly detection (solar flares, sporadic E)

**Step 2: Define L2 Test Signal Schema**
1. **Create `src/hf_timestd/schemas/l2_test_signal_v1.json`:**
   - Define fields for comprehensive test signal data
   - Include per-frequency metrics
   - Add quality flags and anomaly indicators

**Step 3: Integrate into Analytics Pipeline**
1. **Modify `phase2_analytics_service.py`:**
   - Detect minutes 8 and 44 (test signal minutes)
   - Call test signal analyzer during these minutes
   - Write results to L2 test signal HDF5 files
   - Pass metrics to temporal engine for mode disambiguation

**Step 4: Create L3 Science Products**
1. **Ionospheric Absorption Product** (L3B):
   - Aggregate test signal data for absorption analysis
   - Calculate frequency-dependent absorption coefficients
   - Detect solar flares and sudden ionospheric disturbances
2. **Propagation Stability Product** (L3B):
   - Coherence time statistics
   - Scintillation indices
   - Multipath metrics

**Step 5: Web UI Visualization**
1. **Test Signal Dashboard** (new page):
   - Timeline of test signal quality
   - Frequency-dependent field strength plots
   - Coherence time vs. frequency
   - WWV vs WWVH comparative analysis
   - Anomaly detection alerts

**Testing:**
- Wait for minutes 8 and 44 to test with live data
- Verify per-frequency field strength extraction
- Validate coherence time measurements
- Check multipath detection accuracy
- Confirm anomaly detection (use historical solar flare data)

#### Phase 2: TEC Calculation Improvement (MEDIUM PRIORITY)

1. **Enhance `tec_estimator.py`:**
   * Add `estimate_tec_pairwise()` method
   * Calculate TEC for each frequency pair
   * Return per-pair results with uncertainty
2. **Update `science_aggregator.py`:**
   * Call pairwise TEC estimation
   * Write per-pair results to HDF5
3. **Update HDF5 schema:**
   * Add frequency pair fields
   * Maintain backward compatibility
4. **Test with multi-frequency data:**
   * Verify per-pair TEC values are reasonable
   * Check consistency across pairs
   * Validate frequency-dependent TEC trends

### Testing and Validation

#### Station Discrimination Validation (COMPLETED)
```bash
# Validate no invalid station/frequency combinations
python3 scripts/validate_station_discrimination.py --hours 24

# Expected output after fix:
# ✅ VALIDATION PASSED: All station/frequency combinations are valid!
# ✅ 20.0 MHz: All measurements labeled as WWV
# ✅ 25.0 MHz: All measurements labeled as WWV
# ✅ 3.33 MHz: All measurements labeled as CHU
```

#### Test Signal Analysis Validation (NEXT SESSION)
```bash
# Monitor for test signal minutes (8 and 44 of each hour)
sudo journalctl -u timestd-analytics -f | grep -i "test signal"

# Check L2 test signal data
python3 -c "
from hf_timestd.io.hdf5_reader import DataProductReader
from pathlib import Path
from datetime import datetime, timedelta

# Read test signal data for WWV 20 MHz
reader = DataProductReader(
    data_dir=Path('/var/lib/timestd/phase2/WWV_20000'),
    product_level='L2',
    product_name='test_signal',
    channel='WWV_20000'
)
end = datetime.utcnow()
start = end - timedelta(hours=6)
measurements = reader.read_time_range(start.isoformat()+'Z', end.isoformat()+'Z')

print(f'Test signal measurements: {len(measurements)}')
if measurements:
    print('Sample measurement:')
    for k, v in measurements[0].items():
        print(f'  {k}: {v}')
"

# Verify per-frequency field strength extraction
# Validate coherence time measurements
# Check multipath detection
```

---

## System Architecture

### The Seven Services

1. **Core Recorder:** Digital RF capture (`timestd-core-recorder`)
2. **Analytics:** Signal processing (`timestd-analytics`)
3. **Fusion:** Multi-broadcast timing solve (`timestd-fusion`)
4. **VTEC:** GNSS/IONEX data manager (`timestd-vtec`)
5. **Science Aggregator:** TEC estimation (`timestd-science-aggregator`) ← **NEXT FOCUS**
6. **Web UI:** Visualization dashboard (`timestd-web-ui`)
7. **Radiod Monitor:** Hardware watchdog (`timestd-radiod-monitor`)

### Data Flow (HDF5-Native)

```
RTP (UDP) → Core (Digital RF .h5) → Analytics (L2 .h5) → Fusion (L3 .h5) → Chrony (SHM)
                                           ↓
                                    Science Aggregator (L3 TEC .h5)
                                           ↑
                                      VTEC (L3A .h5)
```

## AI Agent Guidance for Next Session

**Session Focus:** WWV/WWVH Test Signal Scientific Analysis

**Preparation:**

* You are implementing comprehensive test signal analysis to extract ionospheric and propagation data
* Test signals occur during **minutes 8 and 44** of each hour (45-second duration)
* Current implementation detects test signals but only extracts single FSS value
* Goal is to extract per-frequency, time-resolved scientific data products

**Implementation Priority:**

1. **Create Test Signal Analyzer Module (HIGH PRIORITY)**
   * New file: `src/hf_timestd/core/test_signal_analyzer.py`
   * Implement `TestSignalMetrics` dataclass
   * Extract per-frequency field strength (not aggregated)
   * Measure per-second coherence during 45-second window
   * Detect multipath and calculate delay spread
   * Identify anomalies (solar flares, sporadic E)

2. **Define L2 Test Signal Schema (HIGH PRIORITY)**
   * New file: `src/hf_timestd/schemas/l2_test_signal_v1.json`
   * Fields: timestamp, station, frequency, field_strength_db, coherence_time_sec, multipath_detected, delay_spread_ms, snr_db, quality_flag

3. **Integrate into Analytics Pipeline (HIGH PRIORITY)**
   * Modify `phase2_analytics_service.py` to call test signal analyzer during minutes 8 and 44
   * Write results to L2 test signal HDF5 files
   * Pass comprehensive metrics to temporal engine

4. **Create L3 Science Products (MEDIUM PRIORITY)**
   * Ionospheric absorption product (frequency-dependent)
   * Propagation stability product (coherence, scintillation)

5. **Web UI Visualization (MEDIUM PRIORITY)**
   * Test signal dashboard with timeline and frequency plots
   * WWV vs WWVH comparative analysis

**Key Principles:**

* **Per-frequency analysis:** Extract field strength for each broadcast frequency separately
* **Time-resolved data:** Track coherence and stability over 45-second test window
* **Comparative analysis:** Differential WWV vs WWVH measurements
* **Anomaly detection:** Automated identification of ionospheric disturbances
* **Scientific validity:** All metrics must be physically meaningful and traceable

**Success Criteria:**

* Per-frequency field strength extracted from test signals
* Coherence time measured for each test signal
* Multipath detection working correctly
* Anomaly detection identifies known solar flare events
* L2 test signal data written to HDF5
* Web UI displays test signal analysis results
* All changes tested during minutes 8 and 44 with live data

**Testing Strategy:**

* **Wait for test signal minutes:** Minutes 8 and 44 of each hour
* **Use multiple frequencies:** Test on WWV (2.5-25 MHz) and WWVH (2.5-15 MHz)
* **Validate against known events:** Use historical data with solar flares
* **Compare WWV vs WWVH:** Verify differential analysis works correctly

**Scientific Impact:**

* Continuous D-layer monitoring (ionospheric absorption)
* Solar flare detection (sudden ionospheric disturbances)
* Propagation mode identification (E-layer vs F-layer)
* Communication link planning (frequency selection, path loss)
* Space weather monitoring (real-time ionospheric conditions)
