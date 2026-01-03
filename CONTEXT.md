# HF Time Standard Analysis - Project Context

**Last Updated:** January 3, 2026  
**Version:** 3.9.0 (Phase 1 & Phase 2 Web UI Complete)  
**Status:** Production (9 channels running at AC0G, 24kHz Sample Rate)

## Quick Reference

**What:** Precision HF timing system extracting D_clock measurements from WWV/WWVH/CHU/BPM broadcasts  
**Where:** `/opt/hf-timestd` (production) or `/home/mjh/git/hf-timestd` (development)  
**Services:** `timestd-core-recorder`, `timestd-analytics`, `timestd-fusion`, `timestd-vtec`, `timestd-web-ui`, `timestd-science-aggregator`, `timestd-radiod-monitor`  
**Web UI (FastAPI):** <http://localhost:8000> (replaces legacy port 3000)  
**Data Root:** `/var/lib/timestd/phase2/`

---

## Current State (Jan 3, 2026)

### ✅ Recently Completed (v3.9.0 - Jan 3, 2026)

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

## 🎯 Next Session Priority: Analytics Service Improvements

**Goal:** Fix station discrimination logic and improve TEC calculations based on issues identified during web UI development.

**Context:**  
During Phase 2 web UI development, three critical issues were identified in the analytics pipeline:

1. **Incorrect station discrimination on station-specific frequencies**
2. **Unnecessary BCD discrimination on all channels**
3. **TEC calculation aggregates frequencies incorrectly**

### Critical Issues to Fix

#### 1. Station Discrimination on Wrong Frequencies

**Problem:** Analytics performs BCD discrimination on **all channels**, including station-specific frequencies where the station is known a priori.

**Current Behavior:**
* WWV_20000 and WWV_25000 channels: Discriminating WWV vs WWVH (WWVH doesn't broadcast there!)
* CHU channels: Discriminating WWV/WWVH/BPM (CHU is the only station on 3.33, 7.85, 14.67 MHz)

**Observed Data Quality Issue:**
* Web UI detected WWVH at 20 MHz and 25 MHz (36 invalid observations)
* These are physically impossible - WWVH only broadcasts on 2.5, 5, 10, 15 MHz

**Correct Behavior:**
* **Shared frequencies (2.5, 5, 10, 15 MHz):** Perform BCD discrimination (WWV vs WWVH vs BPM)
* **Station-specific frequencies:**
    * **20 MHz, 25 MHz:** Label as WWV (no discrimination needed)
    * **3.33 MHz, 7.85 MHz, 14.67 MHz:** Label as CHU (no discrimination needed)

**Impact:**
* Eliminates false WWVH detections at 20/25 MHz
* Saves CPU cycles (no discrimination on 5 of 9 channels)
* Improves data quality and scientific validity

#### 2. TEC Calculation Aggregates Frequencies Incorrectly

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

#### Station Discrimination Fix

* **Primary File:** `src/hf_timestd/core/phase2_analytics_service.py`
  * Lines ~1034-1116: `_write_bcd_discrimination()` method
  * Lines ~2141-2147: Discrimination call in main processing loop
  * **Changes Needed:**
    1. Add frequency-aware discrimination logic
    2. Skip BCD discrimination on station-specific frequencies (20, 25, 3.33, 7.85, 14.67 MHz)
    3. Only perform discrimination on shared frequencies (2.5, 5, 10, 15 MHz)
    4. Add validation to reject impossible station/frequency combinations

* **Constants File:** `src/hf_timestd/core/wwv_constants.py`
  * Add broadcast schedule constants:
    ```python
    # Valid station/frequency combinations (MHz)
    WWV_FREQUENCIES = [2.5, 5.0, 10.0, 15.0, 20.0, 25.0]
    WWVH_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]  # NOT 20/25 MHz
    CHU_FREQUENCIES = [3.33, 7.85, 14.67]
    BPM_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]
    
    # Shared frequencies requiring discrimination
    SHARED_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]
    
    # Station-specific frequencies (no discrimination)
    STATION_SPECIFIC_FREQ = {
        20.0: 'WWV',
        25.0: 'WWV',
        3.33: 'CHU',
        7.85: 'CHU',
        14.67: 'CHU'
    }
    ```

#### TEC Calculation Improvement

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

#### Phase 1: Station Discrimination Fix (High Priority)

1. **Add broadcast schedule constants** to `wwv_constants.py`
2. **Modify `phase2_analytics_service.py`:**
   * Add `_should_discriminate()` method to check if frequency requires discrimination
   * Update `_write_bcd_discrimination()` to skip station-specific frequencies
   * Add `_get_station_from_frequency()` for direct labeling
   * Add validation to reject impossible combinations
3. **Test with live data:**
   * Verify no WWVH detections at 20/25 MHz
   * Verify CHU channels labeled correctly without discrimination
   * Verify shared frequencies still perform discrimination

#### Phase 2: TEC Calculation Improvement (Medium Priority)

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

```bash
# Check analytics service logs for discrimination behavior
sudo journalctl -u timestd-analytics -f

# Verify no invalid station/frequency combinations in L2 data
python3 -c "
from hf_timestd.io.hdf5_reader import DataProductReader
from pathlib import Path
from datetime import datetime, timedelta

# Check recent timing measurements
for channel_dir in Path('/var/lib/timestd/phase2').iterdir():
    if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
        continue
    reader = DataProductReader(
        data_dir=channel_dir,
        product_level='L2',
        product_name='timing_measurements',
        channel=channel_dir.name
    )
    end = datetime.utcnow()
    start = end - timedelta(hours=1)
    measurements = reader.read_time_range(start.isoformat()+'Z', end.isoformat()+'Z')
    
    # Check for invalid combinations
    for m in measurements:
        station = m.get('station')
        freq = m.get('frequency_mhz')
        if station == 'WWVH' and freq in [20.0, 25.0]:
            print(f'INVALID: {station} at {freq} MHz')
"

# Check TEC data for per-pair structure
ls -lh /var/lib/timestd/phase2/science/tec/*.h5
python3 -c "
from hf_timestd.io.hdf5_reader import DataProductReader
from pathlib import Path
from datetime import datetime

reader = DataProductReader(
    data_dir=Path('/var/lib/timestd/phase2/science/tec'),
    product_level='L3',
    product_name='tec',
    channel='AGGREGATED'
)
measurements = reader.read_time_range(
    datetime(2026,1,2,0,0,0).isoformat()+'Z',
    datetime(2026,1,2,23,59,59).isoformat()+'Z'
)
if measurements:
    print('TEC measurement structure:')
    for k, v in measurements[0].items():
        print(f'  {k}: {v}')
"
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

**Preparation:**

* You are fixing station discrimination and TEC calculation issues in the analytics pipeline
* **Do not restart services** until changes are tested and validated
* Make minimal, targeted changes to fix specific issues
* Test each change independently before moving to the next

**Implementation Priority:**

1. **Station Discrimination Fix (HIGH PRIORITY)**
   * Add broadcast schedule constants
   * Modify discrimination logic to skip station-specific frequencies
   * Add validation for impossible combinations
   * Test with live data to verify no WWVH @ 20/25 MHz

2. **TEC Pairwise Calculation (MEDIUM PRIORITY)**
   * Implement per-frequency-pair TEC estimation
   * Update science aggregator to use pairwise method
   * Modify HDF5 schema for per-pair storage
   * Validate frequency-dependent TEC trends

**Key Principles:**

* **Frequency-aware discrimination:** Only discriminate on shared frequencies (2.5, 5, 10, 15 MHz)
* **Station-specific frequencies:** Label directly from channel config (no discrimination)
* **Per-pair TEC:** Each frequency pair represents a distinct propagation path
* **Validation:** Reject physically impossible station/frequency combinations

**Success Criteria:**

* No WWVH detections at 20 MHz or 25 MHz
* CHU channels labeled correctly without discrimination
* Shared frequencies (2.5, 5, 10, 15 MHz) still perform discrimination
* TEC calculated per frequency pair (future: enables frequency-dependent analysis)
* All changes tested with live data before deployment

**Web UI Integration:**

* Web UI already validates broadcast schedules as a defensive measure
* Once analytics is fixed, web UI validation becomes redundant (but harmless)
* Per-pair TEC will enable future web UI enhancement: "TEC vs Frequency" plots
