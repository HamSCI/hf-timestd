# HDF5 Migration Guide

**Status**: Phase 1 - Parallel Writes Active  
**Updated**: 2025-12-25

## Overview

This document describes the safe, phased migration from CSV to HDF5 data products for hf-timestd.

## Migration Strategy: Zero-Risk Rollover

### Current Implementation: Parallel Writes

**Both CSV and HDF5 are being written simultaneously** by Phase 2 analytics service.

**Code Pattern:**

```python
# 1. CSV write (existing, unchanged)
with open(self.clock_offset_csv, 'a', newline='') as f:
    writer.writerow([...])

# 2. HDF5 write (new, error-tolerant)
if self.enable_hdf5_writes and self.hdf5_l2_writer:
    try:
        self.hdf5_l2_writer.write_measurement(measurement)
    except Exception as e:
        logger.error(f"HDF5 write failed: {e}")
        # CSV continues regardless
```

**Safety Features:**

- ✅ CSV writes happen **first** (proven, stable code)
- ✅ HDF5 failures **don't break CSV** pipeline
- ✅ Flag `enable_hdf5_writes` allows instant disable
- ✅ If HDF5 init fails, service continues CSV-only

## Migration Phases

### Phase 1: Parallel Writes ✅ (Current - Dec 25, 2025)

**Status**: ACTIVE

**What's Happening:**

- Phase 2 analytics writes both CSV and HDF5
- All 9 channels producing HDF5 files
- Consumers still reading CSV only

**Data Products:**

- L1A: `{CHANNEL}_channel_observables_{DATE}.h5` (~33 KB/day)
- L1B: `{CHANNEL}_bcd_timecode_{DATE}.h5` (~20 KB/day)
- L2: `{CHANNEL}_timing_measurements_{DATE}.h5` (~88 KB/day)

**Monitoring:**

- Watch for HDF5 write errors in logs
- Verify daily file creation
- Spot-check data integrity

**Duration**: 24-48 hours minimum

---

### Phase 2: Consumer Migration (Next Session)

**Status**: PLANNED

**Tasks:**

1. **Science Aggregator** (`multi_broadcast_fusion.py`)
   - Add HDF5 reader for L2 timing measurements
   - Keep CSV fallback
   - Test fusion output equivalence

2. **Monitoring Server** (`monitoring-server-v3.js`)
   - Add HDF5 endpoints
   - Serve both CSV and HDF5 to UI
   - Test API responses

3. **Web UI** (`summary.html`, etc.)
   - Update to request HDF5 data
   - Display quality metadata
   - Fall back to CSV if HDF5 unavailable

**Validation:**

- Compare CSV vs HDF5 data values
- Verify no functionality loss
- Check performance (HDF5 should be faster)

**Duration**: 1-2 sessions

---

### Phase 3: HDF5 Primary, CSV Safety Net

**Status**: FUTURE

**What Changes:**

- Consumers prefer HDF5, use CSV as fallback
- Both formats still being written
- Monitor consumer behavior

**Success Criteria:**

- All consumers successfully reading HDF5
- No fallback to CSV in normal operation
- Quality metadata visible in UI
- Performance acceptable

**Duration**: 7-14 days

---

### Phase 4: CSV Deprecation

**Status**: FUTURE (Requires Approval)

**Prerequisites:**

- ✅ 30+ days of stable HDF5 operation
- ✅ All consumers migrated and tested
- ✅ No data quality issues
- ✅ User approval

**Changes:**

- Remove CSV write code from Phase 2 analytics
- Keep `enable_hdf5_writes = True`
- Archive old CSV files

**Rollback Plan:**

- Restore from git: `git revert <commit>`
- Restart services
- CSV writes resume immediately

## Emergency Procedures

### Disable HDF5 Writes

If HDF5 causes issues, disable immediately:

**Option 1: Code Change**

```python
# In phase2_analytics_service.py, line ~304
self.enable_hdf5_writes = False  # Change True to False
```

**Option 2: Restart Service**

```bash
sudo systemctl restart timestd-analytics
```

CSV writes continue unaffected.

### Rollback to CSV-Only

If needed, revert the HDF5 integration:

```bash
cd /opt/hf-timestd
sudo git log --oneline | grep HDF5  # Find commit
sudo git revert <commit-hash>
sudo systemctl restart timestd-analytics
```

## Data Equivalence Verification

### Manual Spot Check

```python
# Compare CSV vs HDF5 for same time period
from hf_timestd.io import DataProductReader
import pandas as pd

# Read HDF5
reader = DataProductReader(
    data_dir='/var/lib/timestd/phase2/SHARED_10000/clock_offset',
    product_level='L2',
    product_name='timing_measurements',
    channel='SHARED_10000'
)
hdf5_data = reader.read_time_range(
    start='2025-12-25T00:00:00Z',
    end='2025-12-25T01:00:00Z'
)

# Read CSV
csv_data = pd.read_csv(
    '/var/lib/timestd/phase2/SHARED_10000/clock_offset/SHARED_10000_clock_offset_20251225.csv'
)

# Compare clock_offset_ms values
# Should match within ±0.001 ms
```

### Automated Validation

Create test script to compare formats daily:

- Read same time range from CSV and HDF5
- Compare key fields (clock_offset, uncertainty, quality)
- Alert if differences exceed tolerance
- Log results for review

## File Locations

### HDF5 Files

```
/var/lib/timestd/phase2/{CHANNEL}/
├── carrier_power/
│   └── {CHANNEL}_channel_observables_{YYYYMMDD}.h5
├── bcd_discrimination/
│   └── {CHANNEL}_bcd_timecode_{YYYYMMDD}.h5
└── clock_offset/
    └── {CHANNEL}_timing_measurements_{YYYYMMDD}.h5
```

### CSV Files (Legacy)

```
/var/lib/timestd/phase2/{CHANNEL}/
├── carrier_power/
│   └── carrier_power_{YYYYMMDD}.csv
├── bcd_discrimination/
│   └── {CHANNEL}_bcd_{YYYYMMDD}.csv
└── clock_offset/
    └── {CHANNEL}_clock_offset_{YYYYMMDD}.csv
```

## Monitoring Checklist

**Daily (During Phase 1-2):**

- [ ] Check HDF5 files created for all 9 channels
- [ ] Verify file sizes reasonable (~33-88 KB)
- [ ] Check logs for HDF5 write errors
- [ ] Spot-check data integrity

**Weekly (During Phase 3):**

- [ ] Compare CSV vs HDF5 data equivalence
- [ ] Monitor consumer performance
- [ ] Check disk space usage
- [ ] Review quality statistics

**Before Phase 4:**

- [ ] 30+ days stable operation
- [ ] All consumers migrated
- [ ] Data validation complete
- [ ] User approval obtained

## Benefits of HDF5

Once migration is complete:

1. **Data Integrity**: Schema validation prevents NaN silent failures
2. **Self-Describing**: Embedded metadata (units, provenance, quality)
3. **Quality Filtering**: Read only high-quality data
4. **Uncertainty**: ISO GUM uncertainty budgets included
5. **Performance**: Faster reads with quality filtering
6. **Extensibility**: Add new fields without breaking readers
7. **Standards**: Compatible with CEDAR Madrigal, NASA conventions

## Questions?

Contact: See project documentation or git history for implementation details.
