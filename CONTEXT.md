# HF Time Standard - System Context

**Last Updated:** 2025-12-25  
**Current Version:** v3.2.0 (HDF5 Complete)

---

## Recent Accomplishments (2025-12-25)

### ✅ HDF5 Data Consumer Migration - COMPLETE

**Science Aggregator HDF5 Integration:**

- ✅ L2 timing measurements reader with quality filtering
- ✅ L1A tone detections reader with quality filtering
- ✅ HDF5 SWMR mode for concurrent read/write
- ✅ Per-channel CSV fallback for resilience
- ✅ Deployed to production and verified working

**Metrological Provenance Chain Established:**

```
L0 (RTP timestamps) → L1A (tone timing) → L2 (calibrated) → L3B (fused UTC)
         ↓ HDF5           ↓ HDF5            ↓ HDF5           ↓ Fusion
```

**Production Status:**

- ✅ Analytics service: Writing L1A, L1B, L2 to HDF5 with SWMR
- ✅ Fusion service: Reading L1A and L2 from HDF5 with quality filtering
- ✅ All 9 channels operational without file locking errors
- ✅ UTC(NIST) timing data flowing from HDF5 sources

**Quality Filtering:**

- L2: Grades A/B/C, flags GOOD/MARGINAL, min confidence 0.01
- L1A: Flags GOOD/MARGINAL (excludes BAD/MISSING)
- BPM UT1 minute filtering maintained

**Files Modified:**

- `src/hf_timestd/core/multi_broadcast_fusion.py` - HDF5 readers for L1A and L2
- `src/hf_timestd/core/phase2_analytics_service.py` - HDF5 writer for L1A tones
- `src/hf_timestd/io/hdf5_writer.py` - SWMR mode enabled
- `src/hf_timestd/io/hdf5_reader.py` - SWMR mode enabled
- `src/hf_timestd/schemas/l1_tone_detections_v1.json` - New schema with provenance

## Goals for Next Session

### 🎯 OPTIONAL: Monitoring Server & Web UI Enhancements

**Monitoring Server** (`monitoring-server-v3.js`) - **OPTIONAL**

- Add HDF5 reader for Node.js (h5wasm or similar)
- Update API endpoints to include quality metadata
- Enable uncertainty bounds in responses

**Web UI** (`summary.html`, `ionosphere.html`) - **OPTIONAL**

- Color-code data by quality grade
- Show uncertainty bounds as error bars
- Add quality filter controls

**Note:** Core HDF5 migration is complete. These enhancements would improve visualization but are not required for metrological compliance.

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
