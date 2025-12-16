# Phase 3 Pipeline Critical Review

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-08  
**Status:** ✅ High Priority Issues Resolved

---

## SUMMARY

Phase 3 implements derived product generation: decimation (20 kHz → 10 Hz), spectrograms, power charts with solar zenith overlays, DRF packaging, and upload preparation. The implementation is **substantially complete** with good architecture, but contains **significant path inconsistencies, duplicate/overlapping modules, missing upload implementation, and documentation drift**.

### Issue Count by Severity

| Severity | Count | Description |
|----------|-------|-------------|
| **HIGH** | 4 | ~~Data loss risk, broken paths, missing critical features~~ **RESOLVED** |
| **MEDIUM** | 6 | Inconsistencies, duplicate code, maintainability issues |
| **LOW** | 5 | Documentation drift, minor inefficiencies |

---

## HIGH SEVERITY ISSUES

### 1.1 ✅ RESOLVED: Path Inconsistency: DecimatedBuffer vs Phase3ProductEngine

**Files:**
- `src/grape_recorder/grape/decimated_buffer.py` line 137
- `src/grape_recorder/grape/phase3_product_engine.py` line 237

**Problem:** Two different modules write decimated 10 Hz data to **incompatible locations**.

**Fix Applied (2025-12-08):** Updated `DecimatedBuffer` to write to `products/{CHANNEL}/decimated/` (Phase 3 location), consistent with `Phase3ProductEngine`.

---

### 1.2 ✅ RESOLVED: Legacy Script Uses Obsolete Path Pattern

**File:** `scripts/generate_spectrograms_from_10hz.py` (now in `archive/deprecated-scripts/`)

**Problem:** Uses hardcoded legacy path pattern instead of GRAPEPaths.

**Fix Applied (2025-12-08):**
1. Moved `generate_spectrograms_from_10hz.py` to `archive/deprecated-scripts/`
2. Updated `auto-generate-spectrograms.sh` to use `CarrierSpectrogramGenerator`
3. Updated `grape-phase3.sh` to use canonical `carrier_spectrogram` module

---

### 1.3 ✅ RESOLVED: Upload Script Path Mismatch

**File:** `systemd/grape-daily-upload.service`

**Problem:** Service referenced wrong project path (`signal-recorder` vs `grape-recorder`).

**Fix Applied (2025-12-08):**
1. Updated systemd service to reference `/home/wsprdaemon/grape-recorder/scripts/daily-drf-upload.sh`
2. Updated `daily-drf-upload.sh` to use `grape_recorder.grape.daily_drf_packager` module
3. Changed environment variables to use `GRAPE_` prefix for consistency

---

### 1.4 ✅ RESOLVED: astropy Documentation Error

**File:** `CRITIC_CONTEXT.md`

**Problem:** CRITIC_CONTEXT.md incorrectly stated Phase 3 requires `astropy` for solar zenith.

**Fix Applied (2025-12-08):** Updated CRITIC_CONTEXT.md to document the actual implementation using `solar_zenith_calculator.py` with NOAA algorithms (pure Python, no dependencies).

---

## MEDIUM SEVERITY ISSUES

### 2.1 ✅ RESOLVED: Duplicate Spectrogram Generators

**Files:**
- `src/grape_recorder/grape/spectrogram_generator.py` (DEPRECATED)
- `src/grape_recorder/grape/carrier_spectrogram.py` (CANONICAL)
- `scripts/generate_spectrograms_from_10hz.py` (ARCHIVED)

**Problem:** Three separate spectrogram implementations with overlapping functionality.

**Fix Applied (2025-12-08):**
1. `CarrierSpectrogramGenerator` established as **canonical** implementation
2. `spectrogram_generator.py` marked deprecated with `warnings.warn()`
3. `generate_spectrograms_from_10hz.py` moved to `archive/deprecated-scripts/`
4. `__init__.py` updated to reflect deprecation
5. All scripts updated to use `carrier_spectrogram` module

---

### 2.2 ✅ RESOLVED: DecimatedBuffer Writes to Phase 2, Not Phase 3

**File:** `src/grape_recorder/grape/decimated_buffer.py`

**Problem:** The `DecimatedBuffer` class stored 10 Hz data under `phase2/` (analytical intermediate) instead of `products/` (derived product).

**Fix Applied (2025-12-08):** Changed `DecimatedBuffer` to write to `products/{CHANNEL}/decimated/` (correct Phase 3 location).

---

### 2.3 ✅ RESOLVED: Decimation Filter Transients at Minute Boundaries

**Files:**
- `src/grape_recorder/grape/decimation.py`
- `src/grape_recorder/grape/phase2_analytics_service.py`

**Problem:** Spectrograms showed periodic horizontal banding at ~1 minute intervals. Root cause: the 3-stage decimation filter (CIC + compensation FIR + final FIR) reset state at every minute boundary. The final FIR has **401 taps at 400 Hz = ~1 second transient**, causing visible artifacts.

**Evidence:** Measured variance at minute boundaries was **3.9× higher** than mid-minute:
```
Power variance at minute boundaries: 0.012132
Power variance at mid-minute:        0.003108
Ratio (boundary/mid): 3.90x
```

**Fix Applied (2025-12-08):**
1. Created `StatefulDecimator` class in `decimation.py` (~140 lines)
   - Preserves filter state (`signal.lfilter_zi`) across calls
   - Maintains decimation phase alignment with sample counters
   - Method: `process(samples)` instead of callable function
2. Updated `phase2_analytics_service.py` to use `StatefulDecimator`
3. Exported `StatefulDecimator` from `grape_recorder.grape`

**Key Code:**
```python
# Old (stateless) - transients at every minute:
decimated = self.decimator(iq_samples)  # Filter resets!

# New (stateful) - continuous filter state:
self.decimator = StatefulDecimator(sample_rate, 10)
decimated = self.decimator.process(iq_samples)  # State preserved!
```

**Note:** Existing decimated data (before fix) contains the artifacts. Re-decimation from raw archive required to fix historical data.

---

### 2.4 DRF Output Format Inconsistency (Open)

**Files:**
- `phase3_product_engine.py` lines 568, 723-727
- `daily_drf_packager.py` lines 230-238, 249

**Problem:** Two different DRF output formats:

```python
# phase3_product_engine.py (per-minute real-time):
dtype=np.float32  # (N, 2) format
is_complex=True
num_subchannels=1

# daily_drf_packager.py (batch packaging):
dtype='f4'  # float32
is_complex=True
num_subchannels=num_channels  # 9 subchannels stacked!
```

**Impact:**
- Phase 3 engine writes single-channel DRF files
- Daily packager writes multi-channel stacked DRF (all 9 channels in one file)
- PSWS consumers may expect one format or the other

**Fix Required:** Clarify which format PSWS expects. Document the difference. Possibly unify to one approach.

---

### 2.5 Missing Data Contract: DecimatedBuffer Binary Format (Open)

**File:** `src/grape_recorder/grape/decimated_buffer.py`

**Problem:** Binary format is implicit, not explicitly documented:

```python
# Format is implied by code but not documented:
# - complex64 (8 bytes per sample: 4 bytes real + 4 bytes imag)
# - 600 samples per minute (10 Hz × 60 sec)
# - 1440 minutes per day (24 hours)
# - Pre-allocated file size: 6,912,000 bytes (~6.9 MB)
```

**Impact:** Any reader must reverse-engineer the format from code.

**Fix Required:** Add explicit format documentation in docstring and create format spec in `docs/`.

---

### 2.6 Web UI Hardcoded Script Reference (Open)

**File:** `docs/features/AUTOMATIC_SPECTROGRAM_GENERATION.md` lines 172-175

**Problem:** Documentation references hardcoded script path:

```javascript
const child = spawn('python3', [
  'scripts/generate_spectrograms_drf.py',  // This script doesn't exist!
  '--date', '20241113',
```

**Impact:** Documentation is out of sync with actual implementation.

**Fix Required:** Update documentation to reference current scripts.

---

### 2.7 PSWS Upload Endpoint Configuration Incomplete (Open)

**File:** `config/grape-config.toml` lines 125-147

**Problem:** Upload configuration exists but:
1. `enabled = false` by default
2. No verification of SSH key existence on startup
3. No `create_trigger_directory` implementation in code

```toml
[uploader]
enabled = false  # Set to true when PSWS account is verified
create_trigger_directory = true  # CRITICAL: Signals PSWS to process data
```

The code to implement `create_trigger_directory` doesn't exist.

**Impact:** Even when enabled, upload may not trigger PSWS processing.

**Fix Required:** Implement trigger directory creation in upload script.

---

## LOW SEVERITY ISSUES

### 3.1 Unused Phase3ProductEngine in Current Pipeline

**File:** `src/grape_recorder/grape/phase3_product_engine.py`

**Problem:** This 1003-line module is fully implemented but:
- Not integrated with `phase2_analytics_service.py`
- Not called from any systemd service
- Tests exist but no production integration

**Impact:** Good code sitting unused. Real-time Phase 3 processing not operational.

**Fix Required:** Either integrate with analytics service or document as batch-only tool.

---

### 3.2 Solar Zenith Calculation at Path Midpoint Not Documented

**File:** `src/grape_recorder/grape/solar_zenith_calculator.py`

**Problem:** The solar zenith calculation uses path midpoint between receiver and transmitter, which is **scientifically correct** but not explained to users:

```python
wwv_mid_lat, wwv_mid_lon = calculate_midpoint(rx_lat, rx_lon, *WWV_LOCATION)
```

**Impact:** Users may not understand why solar elevation differs from their local value.

**Fix Required:** Add user-facing documentation explaining path midpoint rationale.

---

### 3.3 Version String Hardcoded in Phase3ProductEngine

**File:** `src/grape_recorder/grape/phase3_product_engine.py` line 611

**Problem:** Pipeline version hardcoded:

```python
'pipeline_version': '3.0.0'
```

Should use centralized version from `src/grape_recorder/version.py`.

**Fix Required:** Import and use `VERSION` constant.

---

### 3.4 Missing WWVH 2.5 MHz in STANDARD_CHANNELS

**File:** `src/grape_recorder/grape/daily_drf_packager.py` lines 53-63

**Problem:** Channel list names only "WWV" but 2.5/5/10/15 MHz are shared with WWVH:

```python
STANDARD_CHANNELS = [
    ('WWV 2.5 MHz', 2.5e6),  # Actually receives WWV + WWVH
    ...
]
```

**Impact:** Metadata may incorrectly attribute all power to WWV.

**Fix Required:** Document that channel name is nominal and discrimination results determine actual station.

---

### 3.5 auto-generate-spectrograms.sh Script Hardcoded Path

**File:** `scripts/auto-generate-spectrograms.sh` line 23

**Problem:** Calls script with hardcoded name:

```bash
python3 scripts/generate_spectrograms_from_10hz.py \
```

This script uses legacy paths (Issue 1.2).

**Impact:** Automated spectrogram generation writes to wrong location.

**Fix Required:** Update to use `carrier_spectrogram.py` or fix the script paths.

---

## DATA CONTRACT INVENTORY

### Phase 2 → Phase 3

| Producer | Output Path | Consumer | Status |
|----------|-------------|----------|--------|
| `decimated_buffer.py` | `phase2/{CH}/decimated/{YYYYMMDD}.bin` | `carrier_spectrogram.py` | ⚠️ Wrong phase |
| `decimated_buffer.py` | `phase2/{CH}/decimated/{YYYYMMDD}_meta.json` | `daily_drf_packager.py` | ⚠️ Wrong phase |
| `phase3_product_engine.py` | `products/{CH}/decimated/...` | (unused) | ✅ Correct path |

### Phase 3 Internal

| Producer | Output Path | Consumer | Status |
|----------|-------------|----------|--------|
| `carrier_spectrogram.py` | `products/{CH}/spectrograms/{YYYYMMDD}_spectrogram.png` | Web UI | ✅ Correct |
| `generate_spectrograms_from_10hz.py` | `spectrograms/{YYYYMMDD}/` | Web UI | ❌ Legacy path |
| `daily_drf_packager.py` | `upload/{YYYYMMDD}/...` | SFTP upload | ✅ Correct |

### Phase 3 → External

| Producer | Output Path | Consumer | Status |
|----------|-------------|----------|--------|
| `daily_drf_packager.py` | `upload/` | PSWS SFTP | ⚠️ Script missing |

---

## RECOMMENDED ACTIONS

### Immediate (Fix Before Production)

1. **Create `scripts/daily-drf-upload.sh`** - Required for systemd timer
2. **Fix `decimated_buffer.py` output path** - Move to `products/{CH}/decimated/`
3. **Update `generate_spectrograms_from_10hz.py`** - Use GRAPEPaths correctly

### Short-Term (Next Sprint)

4. **Consolidate spectrogram generators** - Pick one authoritative implementation
5. **Integrate `Phase3ProductEngine`** - Either real-time or document as batch-only
6. **Clarify DRF output format** - Single-channel vs multi-subchannel for PSWS

### Documentation

7. **Update CRITIC_CONTEXT.md** - Remove astropy reference
8. **Create `docs/PHASE3_FORMATS.md`** - Document binary and DRF formats
9. **Update AUTOMATIC_SPECTROGRAM_GENERATION.md** - Fix script references

---

## FILES REQUIRING CHANGES

| File | Changes Needed |
|------|----------------|
| `src/grape_recorder/grape/decimated_buffer.py` | Change output path to `products/` |
| `scripts/generate_spectrograms_from_10hz.py` | Use GRAPEPaths, fix output location |
| `scripts/auto-generate-spectrograms.sh` | Reference correct generator |
| `scripts/daily-drf-upload.sh` | **CREATE** - Upload implementation |
| `src/grape_recorder/grape/phase3_product_engine.py` | Use centralized version |
| `CRITIC_CONTEXT.md` | Remove astropy reference |
| `docs/features/AUTOMATIC_SPECTROGRAM_GENERATION.md` | Update script references |

---

## APPENDIX: Existing Files by Purpose

### Core Phase 3 Modules

| File | Purpose | Lines | Status |
|------|---------|-------|--------|
| `phase3_product_engine.py` | Real-time DRF product generation | 1003 | Unused |
| `decimation.py` | 20 kHz → 10 Hz multi-stage decimator | 700 | ✅ Active |
| `decimated_buffer.py` | Binary 10 Hz storage | 400 | ⚠️ Wrong path |
| `daily_drf_packager.py` | Multi-channel DRF for upload | 443 | ✅ Active |
| `solar_zenith_calculator.py` | NOAA solar position algorithm | 286 | ✅ Active |

### Spectrogram Generators

| File | Input Source | Output Path | Status |
|------|--------------|-------------|--------|
| `carrier_spectrogram.py` | DecimatedBuffer | `products/{CH}/spectrograms/` | ✅ Recommended |
| `spectrogram_generator.py` | Phase 3 DRF | `products/{CH}/spectrograms/{date}/` | Redundant |
| `generate_spectrograms_from_10hz.py` | NPZ files | `spectrograms/{date}/` | ⚠️ Legacy path |

---

*End of Phase 3 Critical Review*
