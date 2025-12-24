# HF Time Standard - System Context

**Last Updated:** 2025-12-24  
**Current Version:** v3.1.0

---

## CRITICAL: Data Model Management Crisis

### The Problem

**Perennial Issue:** Choosing where to write measurements, where to find written data, and how to coordinate producers/consumers has gotten out of hand.

**Symptoms:**

- Data scattered across 11+ CSV types per channel
- No single source of truth for schemas
- Producers and consumers don't share contracts
- Silent failures (NaN → 0 conversion)
- Schema fragmentation causes brittleness
- `DATA_API_DESIGN.md` exists but not enforced

**Recent Example (2025-12-24):**

- carrier_power CSV contained "nan" strings
- ionosphere.html showed zeros/flatlines
- Root cause: No validation, no schema enforcement
- Quick fix applied, but systemic problem remains

### What We Need

**A comprehensive data model and management plan that provides:**

1. **Single Source of Truth**
   - Canonical schema definitions (machine-readable)
   - Clear producer/consumer contracts
   - Versioning strategy

2. **Scalable Organization**
   - Consolidated CSV schemas (reduce 11 types → 3-4)
   - Clear data lifecycle (write → validate → read)
   - Automated enforcement

3. **Validation at Every Layer**
   - Producers validate before writing
   - Consumers validate after reading
   - Monitoring alerts for violations

4. **Clear Documentation**
   - Where each measurement lives
   - How to access each data type
   - Migration guides for schema changes

---

## Focus for Next Session: Data Model & Management Plan

### Goals

1. **Design unified data model**
   - Consolidate fragmented CSVs
   - Define canonical schemas
   - Create schema registry

2. **Implement validation infrastructure**
   - Schema enforcement in producers
   - Validation in consumers
   - Monitoring and alerts

3. **Create migration strategy**
   - Parallel writes during transition
   - Backward compatibility
   - Deprecation timeline

4. **Update documentation**
   - Comprehensive schema docs
   - Producer/consumer guides
   - API contracts

### Current State

**CSV Types Per Channel (11 total):**

```
audio_tones/          - Audio tone analysis
bcd_discrimination/   - BCD time code discrimination  
carrier_power/        - Carrier power (BROKEN - had NaN)
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

**Problems:**

- Monitoring server must join 3 CSVs (carrier_power, doppler, clock_offset) to build complete picture
- No validation before writing
- No schema versioning
- Silent failures hide problems

**What Works:**

- `TimeStdPaths` API for path management
- TEC data generation (v3.1.0)
- Clock offset measurements
- Basic CSV structure

**Recent Fixes (2025-12-24):**

- ✅ Fixed NaN in carrier_power calculation
- ✅ Created `scripts/validate_csv_schemas.py`
- ✅ Documented schema gaps in `DATA_API_DESIGN.md`

### Key Files to Review

**Schema Documentation:**

- `docs/DATA_API_DESIGN.md` - Ideal API design (created long ago, not enforced)
- `scripts/validate_csv_schemas.py` - NEW validator (detects NaN, schema violations)

**Producers:**

- `src/hf_timestd/core/phase2_analytics_service.py` - Writes 11 CSV types
- `src/hf_timestd/core/science_aggregator.py` - Writes TEC CSVs

**Consumers:**

- `web-ui/monitoring-server-v3.js` - Reads CSVs, serves API
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Reads clock_offset

**Path Management:**

- `src/hf_timestd/paths.py` - TimeStdPaths API (good pattern to follow)

### Investigation Steps for Next Session

1. **Audit Current Data Model**
   - Map all CSV types to their purpose
   - Identify redundancy and overlap
   - Document dependencies

2. **Design Unified Schema**
   - Consolidate carrier_power + doppler + clock_offset
   - Define core vs optional fields
   - Create schema versioning plan

3. **Create Schema Registry**
   - Machine-readable schemas (JSON Schema)
   - Validation functions
   - Migration tools

4. **Implement Enforcement**
   - Add validation to producers
   - Add validation to consumers
   - Add monitoring alerts

5. **Document Everything**
   - Update DATA_API_DESIGN.md
   - Create producer guide
   - Create consumer guide

---

## System Architecture

### Core Services

- **`timestd-core-recorder`**: Receives RTP streams from `radiod`, writes Digital RF archives (Phase 1)
- **`timestd-analytics`**: 9 Phase 2 processes + fusion engine for timing analysis
- **`timestd-science-aggregator`**: Multi-frequency TEC calculation (NEW in v3.1.0)
- **`timestd-web-ui`**: Node.js monitoring server serving real-time dashboards

### Data Flow

```
SDR → radiod → RTP/F32 → core-recorder → Hot Buffer (/dev/shm/timestd/raw_buffer)
                                               ↓ (background archiver)
                                           Cold Buffer (/var/lib/timestd/raw_buffer)
                                               ↑ (reads hot first, falls back to cold)
                                           analytics → 11 CSV types per channel
                                                    ↓
                                           clock_offset CSVs → science-aggregator → TEC CSVs
                                                                                   → web-ui
```

**Tiered Storage:** Core recorder writes to RAM (`/dev/shm`), background thread archives old minutes to disk. Analytics reads from hot buffer first (zero-latency), falls back to cold if needed.

---

## Recent Changes (v3.1.0 - 2025-12-24)

### TEC Implementation ✅

**Achievement:** Multi-frequency ionospheric TEC measurement from HF timing data

**Key Changes:**

- **`science_aggregator.py`**: Refactored to use `TimeStdPaths` API
  - Uses `discover_phase2_channels()` for automatic channel discovery
  - Uses `get_clock_offset_dir(channel_name)` for consistent path resolution
  - Fixed field name bug: `'minute_boundary'` → `'minute_boundary_utc'`
  - Generates TEC every 5 minutes from multi-frequency clock offset data

- **`tec_geometry.py`**: Obliquity factor and geometric corrections

**Production Status:**

- ✅ Service running: `timestd-science-aggregator`
- ✅ Data generating: `/var/lib/timestd/phase2/science/tec/tec_YYYYMMDD.csv`
- ✅ 121+ measurements per day (CHU, WWV, WWVH)
- ✅ 3-6 frequencies per station, 1-minute cadence

### Data Quality Fixes ✅

**Problem:** NaN values in carrier_power CSV causing zeros in ionosphere.html

**Root Cause:**

- No validation of IQ samples before power calculation
- NaN is truthy in Python → `round(NaN, 2)` → "nan" string in CSV
- Monitoring server converts "nan" → 0 → UI shows flatlines

**Fixes Applied:**

- Added NaN validation before calculation (`phase2_analytics_service.py` lines 1570-1585)
- Added NaN/inf checks before CSV writing (lines 503-510)
- Created CSV schema validator (`scripts/validate_csv_schemas.py`)

**Result:**

- ✅ No more "nan" in new CSV rows
- ✅ Empty fields instead when data invalid
- ✅ Validator detects schema violations

**Committed:** 2025-12-24 18:37 UTC (commit 7966fbf)

---

## Key Configuration

- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data Root**: `/var/lib/timestd/`
- **Hot Buffer**: `/dev/shm/timestd/raw_buffer/`
- **Phase 2 CSVs**: `/var/lib/timestd/phase2/{CHANNEL}/`
- **TEC Data**: `/var/lib/timestd/phase2/science/tec/`
- **Logs**: `journalctl -u timestd-core-recorder` / `timestd-analytics` / `timestd-science-aggregator` / `timestd-web-ui`

## Channel Specifications (config.toml)

9 channels: 2.5, 5, 10, 15, 20, 25 MHz (WWV/WWVH) + 3.33, 7.85, 14.67 MHz (CHU)  
All use: `preset=iq`, `sample_rate=20000`, `agc=0`, `gain=0`, `encoding=F32`

---

## Important Patterns

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

### Data Validation (NEW - v3.1.0+)

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

# Validate CSVs
python3 scripts/validate_csv_schemas.py --csv-file /path/to/file.csv
python3 scripts/validate_csv_schemas.py --directory /var/lib/timestd/phase2/SHARED_10000

# Check logs
journalctl -u timestd-analytics -n 50
journalctl -u timestd-science-aggregator -n 20
```

---

## Documentation

**Data Model:**

- `docs/DATA_API_DESIGN.md` - Ideal API design (needs enforcement)
- `scripts/validate_csv_schemas.py` - Schema validator

**TEC:**

- `docs/GPS_TEC_OPTIONAL.md` - Comprehensive guide
- `docs/TEC_VALIDATION_METHODOLOGY.md` - Scientific methodology
- `docs/ZED_F9P_TEC_CONFIGURATION.md` - GPS receiver setup

**System:**

- `docs/DEPLOYMENT_CHECKLIST.md` - Deployment guide
- `docs/QUALITY_METRICS_KA9Q.md` - Quality metrics
