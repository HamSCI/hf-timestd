# TEC Estimation Integration - Progress Report

## Completed Work

### Backend Infrastructure

Successfully integrated TEC (Total Electron Content) estimation capability into the Phase 2 analytics pipeline:

#### 1. **TEC Directory Structure**

Added to `phase2_analytics_service.py` **init** (lines 267-271):

```python
# TEC Estimation - Ionospheric Total Electron Content
# Calculated from multi-frequency measurements when available
self.tec_dir = self.output_dir / 'tec'
self.tec_dir.mkdir(parents=True, exist_ok=True)
self._init_tec_csv()
```

Creates: `/var/lib/timestd/phase2/{STATION}/tec/` directory

#### 2. **TECEstimator Initialization**

Added to `phase2_analytics_service.py` **init** (lines 273-276):

```python
# Initialize TEC Estimator for ionospheric analysis
from .tec_estimator import TECEstimator
self.tec_estimator = TECEstimator(high_precision_mode=True)
logger.info("Initialized TEC estimator for ionospheric analysis")
```

#### 3. **TEC CSV Methods**

Added two methods to `phase2_analytics_service.py`:

- **`_init_tec_csv()`** (line 1014): Initializes daily CSV file with headers:
  - `timestamp_utc`, `minute_boundary`, `station`
  - `tec_tecu`, `t_vacuum_error_ms`, `confidence`, `residuals_ms`
  - `n_frequencies`, `frequencies_mhz`
  - Per-frequency group delays: `group_delay_2_5_mhz` through `group_delay_25_mhz`

- **`_write_tec()`** (line 1033): Writes TEC estimation results
  - Accepts `minute_boundary`, `station`, and `measurements` list
  - Calls `TECEstimator.estimate_tec()` for multi-frequency analysis
  - Writes results to daily-rotated CSV files

#### 4. **File Naming Convention**

TEC CSVs use station-agnostic naming: `tec_YYYYMMDD.csv`

- Rationale: TEC is calculated per-station across multiple frequencies
- All stations' TEC estimates for a given day go into the same file

## Architecture Challenge Identified

### Current Design: Per-Channel Processing

Each `Phase2AnalyticsService` instance processes **one channel** (one frequency):

- `WWV_10000` service processes 10 MHz data
- `WWV_15000` service processes 15 MHz data
- etc.

### TEC Requirement: Multi-Frequency Coordination

TEC estimation requires **simultaneous measurements** from the same station at multiple frequencies:

- WWV: 2.5, 5, 10, 15, 20, 25 MHz (6 frequencies)
- WWVH: 2.5, 5, 10, 15 MHz (4 frequencies)
- CHU: 3.33, 7.85, 14.67 MHz (3 frequencies)
- BPM: 2.5, 5, 10, 15 MHz (4 frequencies)

## Implementation Approaches

### Option 1: Cross-Channel Data Sharing (Recommended)

Each service writes its timing measurements to a shared location, and one designated service (e.g., lowest frequency for each station) performs TEC calculation:

**Pros:**

- Minimal code changes
- Leverages existing per-channel architecture
- Natural fit for systemd multi-instance services

**Cons:**

- Requires coordination mechanism
- Slight delay (up to 1 minute) for all frequencies to report

**Implementation:**

1. Each service writes timing data to: `phase2/shared/timing_measurements_{station}_{date}.json`
2. Designated service (e.g., `SHARED_2500`) reads all measurements and calculates TEC
3. TEC results written to `phase2/shared/tec/tec_{date}.csv`

### Option 2: Post-Processing Script

Separate script runs periodically to collect timing data across channels and calculate TEC:

**Pros:**

- Clean separation of concerns
- Easy to test and debug
- Can backfill historical data

**Cons:**

- Not real-time
- Additional process to manage

**Implementation:**

1. Create `scripts/calculate_tec.py`
2. Reads clock_offset CSVs from all channels
3. Groups by station and timestamp
4. Calculates TEC and writes to `tec/` directory
5. Run via cron every 5 minutes

### Option 3: Fusion Service Integration

Integrate TEC calculation into the multi-broadcast fusion service (if it exists):

**Pros:**

- Fusion already aggregates multi-frequency data
- Natural fit for cross-frequency analysis

**Cons:**

- Requires understanding fusion service architecture
- May not exist yet

## Next Steps

### Immediate (To Complete Backend)

1. **Choose implementation approach** (recommend Option 1 or 2)
2. **Implement multi-frequency data collection**
3. **Test with real data** from production system

### API Layer

4. **Add `/api/v1/propagation/tec` endpoint** to `monitoring-server-v3.js`
2. **Add `/api/v1/propagation/group-delay` endpoint**

### Frontend

6. **Populate TEC chart** in `ionosphere.html`
2. **Add multi-frequency scatter plot** showing dispersion
3. **Test visualization** with calculated TEC data

## Files Modified

- ✅ `/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py`
  - Added TEC directory initialization
  - Added TECEstimator import and initialization
  - Added `_init_tec_csv()` method
  - Added `_write_tec()` method

## Files Created

- ✅ `/home/mjh/git/hf-timestd/scripts/insert_tec_methods.py` (helper script, can be deleted)

## Testing Plan

Once multi-frequency collection is implemented:

1. **Verify CSV Creation**:

   ```bash
   ls -la /var/lib/timestd/phase2/*/tec/
   ```

2. **Check TEC Values**:

   ```bash
   tail -f /var/lib/timestd/phase2/shared/tec/tec_$(date +%Y%m%d).csv
   ```

3. **Validate Physics**:
   - TEC should be 5-50 TECU (typical daytime values)
   - Confidence should be >0.8 for good multi-frequency fits
   - Diurnal pattern: peak during day, minimum at night

4. **Compare with GPS TEC**:
   - Cross-reference with NOAA SWPC TEC maps
   - Expect ±5-10 TECU agreement

## Performance Considerations

- **TEC Calculation Cost**: ~1ms per minute (negligible)
- **CSV Write Cost**: ~0.1ms per row (negligible)
- **Storage**: ~50 KB/day for TEC data (minimal)

## Summary

The TEC estimation infrastructure is now in place in the backend. The remaining work is:

1. Implementing multi-frequency data collection (architecture decision needed)
2. Adding API endpoints
3. Populating frontend visualizations

**Recommendation**: Implement Option 2 (post-processing script) first as a proof-of-concept, then migrate to Option 1 (cross-channel sharing) if real-time TEC is desired.
