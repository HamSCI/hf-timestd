# HF Time Standard - System Context

**Last Updated:** 2025-12-25  
**Current Version:** v3.2.0-dev (HDF5 Consumer Migration)

---

## Recent Accomplishments (2025-12-25)

### ✅ HDF5 Data Consumer Migration - PHASE 1 COMPLETE

**Science Aggregator HDF5 Integration:**

- ✅ Implemented HDF5 reader for L2 timing measurements in `multi_broadcast_fusion.py`
- ✅ Quality filtering: Grades A/B/C, flags GOOD/MARGINAL, min confidence 0.01
- ✅ Per-channel CSV fallback for resilience
- ✅ **HDF5 SWMR mode enabled** - resolves file locking for concurrent read/write
- ✅ Deployed to production and verified working

**HDF5 SWMR (Single Writer Multiple Reader) Mode:**

- Writer: Opens with `libver='latest'`, enables `swmr_mode=True`
- Reader: Opens with `swmr=True, libver='latest'`
- **Result:** All 9 channels reading successfully without file locking errors

**Production Status:**

- ✅ Analytics service writing HDF5 with SWMR mode
- ✅ Fusion service reading HDF5 with SWMR mode
- ✅ Concurrent access working perfectly
- ✅ Fusion producing UTC(NIST) timing data from HDF5

**Files Modified:**

- `src/hf_timestd/core/multi_broadcast_fusion.py` - HDF5 readers with CSV fallback
- `src/hf_timestd/io/hdf5_writer.py` - SWMR mode enabled
- `src/hf_timestd/io/hdf5_reader.py` - SWMR mode enabled
- `tests/test_fusion_hdf5_reader.py` - Test script

## Goals for Next Session

### 🎯 PRIMARY: Complete HDF5 Consumer Migration

**Remaining Work:**

1. **Science Aggregator** - Finish L1A tone detections reader
   - Implement HDF5 reader for `_read_latest_tone_observations()`
   - Similar approach to L2 timing measurements
   - Test equivalence with CSV data

2. **Monitoring Server** (`monitoring-server-v3.js`) - **MEDIUM PRIORITY**
   - Add HDF5 reader utility for Node.js (h5wasm or similar)
   - Update 12+ API endpoints to try HDF5 first
   - Include quality metadata in responses

3. **Web UI** (`summary.html`, `ionosphere.html`) - **LOW PRIORITY**
   - Update charts to consume HDF5 data
   - Display quality grades and uncertainty bounds
   - Add quality filter controls

**Migration Strategy:**

- Keep CSV reads as fallback during transition
- Test equivalence between CSV and HDF5 data
- Gradually switch to HDF5-only after validation
- Deprecate CSV writes once all consumers migrated

### Key Files to Implement

**I/O Module**:

- `src/hf_timestd/io/__init__.py` - DataProductWriter, DataProductReader
- `src/hf_timestd/io/hdf5_writer.py` - HDF5 writing with schema validation
- `src/hf_timestd/io/hdf5_reader.py` - HDF5 reading with quality filtering
- `src/hf_timestd/io/uncertainty.py` - ISO GUM uncertainty calculator

**Tests**:

- `tests/unit/test_hdf5_io.py` - Unit tests for HDF5 I/O
- `tests/integration/test_data_flow.py` - End-to-end data flow tests

---

## Current State: Data Model

### Schema Registry (NEW - v3.2.0-dev)

**Location**: `src/hf_timestd/schemas/`

**Schemas Available**:

- L1A: `channel_observables_v1.json` - Consolidates carrier_power, doppler, tones, test_signal
- L1B: `bcd_timecode_v1.json` - BCD time code discrimination
- L2: `timing_measurements_v1.json` - Station-assigned timing with ISO GUM uncertainty
- L3A: `tec_v1.json` - Total Electron Content estimates
- L3B: `fusion_timing_v1.json` - Multi-station UTC(NIST) fusion

**Usage**:

```python
from hf_timestd.schemas import get_schema

# Load L2 timing measurements schema
schema = get_schema('L2', 'timing_measurements')
print(schema['schema_version'])  # '1.0.0'
```

### Data Product Levels (NASA EOSDIS)

- **L0**: Raw RTP/Digital RF (unchanged)
- **L1**: Calibrated measurements (channel observables + BCD)
- **L2**: Derived geophysical variables (timing measurements with uncertainty)
- **L3**: Science products (TEC + fusion timing)

### Current CSV Types (Legacy - to be replaced)

```
audio_tones/          - Audio tone analysis
bcd_discrimination/   - BCD time code discrimination  
carrier_power/        - Carrier power (FIXED - NaN issue resolved)
clock_offset/         - Clock offset (WORKING)
discrimination/       - WWV/WWVH discrimination
doppler/              - Doppler measurements
station_id_440hz/     - Station ID from 440Hz
status/               - Status files
tec/                  - TEC calculations (WORKING)
test_signal/          - Test signal detection
timing/               - UTC(NIST) timing
tone_detections/      - Tone detection results
```

**Migration Plan**: Parallel writes (CSV + HDF5) → Switch consumers → Deprecate CSVs

---

## Key Configuration

- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data Root**: `/var/lib/timestd/`
- **Hot Buffer**: `/dev/shm/timestd/raw_buffer/`
- **Phase 2 CSVs**: `/var/lib/timestd/phase2/{CHANNEL}/` (legacy)
- **Phase 2 HDF5**: `/var/lib/timestd/phase2/{CHANNEL}/` (new, to be implemented)
- **TEC Data**: `/var/lib/timestd/phase2/science/tec/`
- **Logs**: `journalctl -u timestd-core-recorder` / `timestd-analytics` / `timestd-science-aggregator` / `timestd-web-ui`

## Channel Specifications (config.toml)

9 channels: 2.5, 5, 10, 15, 20, 25 MHz (WWV/WWVH) + 3.33, 7.85, 14.67 MHz (CHU)  
All use: `preset=iq`, `sample_rate=20000`, `agc=0`, `gain=0`, `encoding=F32`

---

## Important Patterns

### Schema Usage (NEW - v3.2.0+)

- ✅ **Always use schema registry** - Import from `hf_timestd.schemas`
- ✅ **Validate before writing** - Use DataProductWriter (to be implemented)
- ✅ **Include uncertainty budgets** - L2 requires ISO GUM components
- ❌ **Don't write without validation** - Prevents NaN silent failures

### Path Management (v3.1.0+)

- ✅ **Always use TimeStdPaths** - Import from `hf_timestd.paths`
- ✅ **Use discovery methods** - `discover_phase2_channels()`, `get_clock_offset_dir()`
- ✅ **Never hard-code paths** - Let TimeStdPaths handle all path construction
- ❌ **Don't manually construct paths** - Leads to field name mismatches

### Channel Management (v3.0.1+)

- ✅ **Always discover before creating** - Check for existing channels
- ✅ **Match by freq/preset/rate** - Reuse when possible
- ✅ **Health monitoring enabled** - Auto-recovery from radiod restarts
- ❌ **Never** manually manage channels - let discovery handle it

### Data Validation (v3.1.0+)

- ✅ **Validate before calculation** - Check for NaN/inf in input data
- ✅ **Validate before writing** - Check for NaN/inf before CSV write
- ✅ **Use validator script** - `scripts/validate_csv_schemas.py`
- ❌ **Don't write invalid data** - Better to skip row than write NaN

---

## Relevant Commands

```bash
# Check services
sudo systemctl status timestd-core-recorder
sudo systemctl status timestd-analytics
sudo systemctl status timestd-science-aggregator
sudo systemctl status timestd-web-ui

# Check TEC data
ls -lh /var/lib/timestd/phase2/science/tec/
tail /var/lib/timestd/phase2/science/tec/tec_$(date +%Y%m%d).csv

# Validate CSVs (legacy)
python3 scripts/validate_csv_schemas.py --csv-file /path/to/file.csv
python3 scripts/validate_csv_schemas.py --directory /var/lib/timestd/phase2/SHARED_10000

# Check schemas (NEW)
python3 -c "from hf_timestd.schemas import list_schemas; print(list_schemas())"

# Check logs
journalctl -u timestd-analytics -n 50
journalctl -u timestd-science-aggregator -n 20
```

---

## Documentation

**Data Model**:

- `docs/DATA_API_DESIGN.md` - Ideal API design (to be updated with HDF5)
- `src/hf_timestd/schemas/` - Schema registry (NEW)
- `scripts/validate_csv_schemas.py` - CSV schema validator (legacy)

**TEC**:

- `docs/GPS_TEC_OPTIONAL.md` - Comprehensive guide
- `docs/TEC_VALIDATION_METHODOLOGY.md` - Scientific methodology
- `docs/ZED_F9P_TEC_CONFIGURATION.md` - GPS receiver setup

**System**:

- `docs/DEPLOYMENT_CHECKLIST.md` - Deployment guide
- `docs/QUALITY_METRICS_KA9Q.md` - Quality metrics
