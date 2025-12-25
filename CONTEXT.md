# HF Time Standard - System Context

**Last Updated:** 2025-12-24  
**Current Version:** v3.2.0-dev (Schema Registry)

---

## RECENT: Schema Registry Implementation (2025-12-24)

### What Was Accomplished

✅ **Schema Registry Infrastructure Complete**

# hf-timestd Project Context

**Last Updated**: 2025-12-25 00:36 UTC

## Recent Accomplishments (This Session)

### ✅ HDF5 I/O Module - DEPLOYED TO PRODUCTION

**Implemented:**

- ISO GUM uncertainty calculator with Type A/B propagation
- Schema-validated HDF5 writer with NaN/inf rejection
- Quality-filtered HDF5 reader with time range queries
- Comprehensive unit tests (all passing)

**Integrated into Phase 2 Analytics:**

- L1A: Channel observables (carrier power, SNR, tones)
- L1B: BCD timecode discrimination
- L2: Timing measurements with ISO GUM uncertainty budgets

**Production Status:**

- ✅ Deployed to `/opt/hf-timestd` on 2025-12-25
- ✅ HDF5 files being created for all 9 channels
- ✅ Parallel CSV+HDF5 writes active (backward compatible)
- ✅ File locations: `/var/lib/timestd/phase2/{CHANNEL}/{clock_offset|carrier_power|bcd_discrimination}/*.h5`
- ✅ Dependencies added to `setup.py` and `requirements.txt`

**Example Files:**

- `SHARED_10000_timing_measurements_20251225.h5` (88 KB)
- `SHARED_10000_channel_observables_20251225.h5` (33 KB)
- `SHARED_10000_bcd_timecode_20251225.h5` (20 KB)

## Goals for Next Session

### 🎯 PRIMARY: Migrate Data Consumers to HDF5

**Consumer Migration Priority:**

1. **Science Aggregator** (`multi_broadcast_fusion.py`)
   - Currently reads CSV files for fusion
   - Migrate to read L2 HDF5 timing measurements
   - Benefit: Quality filtering, uncertainty propagation

2. **Monitoring Server** (`monitoring-server-v3.js`)
   - Currently serves CSV data to Web UI
   - Add HDF5 endpoints for real-time data
   - Benefit: Structured data with metadata

3. **Web UI** (`summary.html`, `ionosphere.html`)
   - Update charts to consume HDF5 data
   - Display quality grades and uncertainty bounds
   - Benefit: Better visualization of data quality

**Migration Strategy:**

- Keep CSV reads as fallback during transition
- Add HDF5 readers alongside CSV
- Test equivalence between CSV and HDF5 data
- Gradually switch to HDF5-only after validation
- Deprecate CSV writes once all consumers migrated

**Success Criteria:**

- All consumers reading HDF5 successfully
- No degradation in functionality
- Quality metadata visible in UI
- CSV writes can be safely disabled

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
