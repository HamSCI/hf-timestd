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

```text
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

### 🎯 PRIMARY: Monitoring Server & Web UI HDF5 Integration

**Objective:** Enable monitoring server and web UI to consume HDF5 data with quality metadata for enhanced visualization and user experience.

#### Phase 1: Monitoring Server (`monitoring-server-v3.js`)

**Current State:**

- Serves CSV data to web UI via REST API
- 12+ endpoints reading various CSV files
- No quality metadata in responses

**Goals:**

1. **Add HDF5 Reader for Node.js**
   - Evaluate libraries: `h5wasm` (WebAssembly, no native deps) vs `hdf5.node` (native bindings)
   - Recommended: `h5wasm` for easier deployment
   - Create utility module: `web-ui/utils/hdf5-reader.js`

2. **Update API Endpoints (Priority Order)**
   - **HIGH:** `/api/channel/:channel/clock-offset` (L2 timing measurements)
     - Add quality_grade, quality_flag, uncertainty_ms to response
     - Try HDF5 first, fall back to CSV
   - **MEDIUM:** `/api/channel/:channel/carrier-power` (L1A channel observables)
     - Add SNR, Doppler, coherence time, phase variance
     - Include quality metadata
   - **LOW:** Other endpoints as needed

3. **Response Format Enhancement**

   ```json
   {
     "timestamp": "2025-12-25T12:00:00Z",
     "value": -2.14,
     "uncertainty": 1.2,
     "quality_grade": "A",
     "quality_flag": "GOOD",
     "confidence": 0.95,
     "station": "WWV",
     "metadata": {
       "processing_version": "3.2.0",
       "traceability": "UTC(NIST) via WWVB"
     }
   }
   ```

#### Phase 2: Web UI (`summary.html`, `ionosphere.html`)

**Current State:**

- Displays timing data from monitoring server
- No quality visualization
- No uncertainty bounds

**Goals:**

1. **Quality Visualization**
   - Color-code data points by quality grade:
     - Grade A: Green
     - Grade B: Yellow/Amber
     - Grade C: Orange
     - Grade D: Red
   - Show quality flags in tooltips
   - Add legend for quality grades

2. **Uncertainty Bounds**
   - Display uncertainty as error bars on charts
   - Use Chart.js error bar plugin
   - Show ±1σ, ±2σ, ±3σ options

3. **Quality Filters**
   - Checkbox controls to show/hide quality grades
   - Slider for minimum quality threshold
   - Toggle to show/hide uncertainty bounds
   - Filter by quality flags (GOOD/MARGINAL/BAD)

4. **Metadata Display**
   - Processing version in footer
   - Data completeness indicator
   - Traceability information in info panel

#### Implementation Strategy

1. **Start with monitoring server** - Foundation for web UI
2. **Implement one endpoint at a time** - Iterative approach
3. **Test with existing web UI** - Verify backward compatibility
4. **Enhance web UI incrementally** - Add features progressively
5. **Maintain CSV fallback** - Ensure resilience

#### Success Criteria

- ✅ Monitoring server reads HDF5 successfully
- ✅ Quality metadata included in API responses
- ✅ Web UI displays quality-coded data points
- ✅ Uncertainty bounds visible on charts
- ✅ Quality filters functional
- ✅ No degradation in performance
- ✅ CSV fallback working

#### Optional Enhancements

- Real-time quality alerts (e.g., when quality drops below threshold)
- Historical quality trends chart
- Export data with quality metadata (CSV/JSON download)
- Quality statistics dashboard

**Note:** This work enhances user experience and visualization but is not required for metrological compliance (already achieved with HDF5 reader implementation).

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
