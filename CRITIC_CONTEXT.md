# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🔴 CURRENT FOCUS (NEXT SESSION): RECEIVER / SSRC PROLIFERATION

**Purpose:** Diagnose why the application keeps creating duplicate radiod channels/streams (“receiver proliferation”). This typically manifests as:

- Multiple radiod streams for the same frequency/preset/sample_rate
- Increasing SSRC count after service restarts
- Resource exhaustion (CPU/network/multicast subscriptions)

**Key hypothesis:** `RadiodControl.ensure_channel(...)` is not idempotent under current inputs. The most common causes are mismatched or unstable parameters:

- **Destination mismatch** (`destination=None` vs explicit multicast IP, or mDNS resolution changes)
- **Encoding mismatch** (F32 vs S16) causing radiod to treat the request as a new unique channel
- **Preset/sample_rate mismatch** across services/processes

**Critical invariant:** For a given logical channel, every caller must use the *same* `(frequency_hz, preset, sample_rate, encoding, destination)` tuple (or intentionally reuse radiod defaults consistently).

**Author:** Michael James Hauan (AC0G)
**Date:** 2025-12-23
**Status:** 🟡 In Progress

---

## Next Session Goal

Identify the exact condition(s) that trigger new channel creation and make channel acquisition idempotent across:

- service restarts
- multiple processes
- day boundaries

Success criteria:

- Restarting core recorder does **not** increase radiod channel count
- Re-running analytics/web-ui does **not** create new IQ channels
- The system converges on a stable set of SSRCs

### SESSION GUIDE: RECEIVER PROLIFERATION DEBUGGING

This session focuses on the complete lifecycle of a timing sample.

#### 1. Data Pipeline Overview (CURRENT IMPLEMENTATION)

```
ka9q-radio (radiod)
  ↓ (RTP multicast)
ka9q-python RadiodControl.ensure_channel(...)
  ↓ (creates or reuses channel)
ka9q-python RadiodStream
  ↓ (decoded IQ callbacks)
CoreRecorderV2 / StreamRecorderV2
  ↓
Phase 1 raw_buffer/*.bin + metadata.json
  ↓
Phase2AnalyticsService (per-channel)
  ↓
phase2/{CHANNEL}/clock_offset/*.csv + tone_detections/*.csv
  ↓
MultiBroadcastFusion (cross-channel)
  ↓
phase2/fusion/fused_d_clock.csv
```

#### 2. Key Files for Review (Receiver Proliferation)

| Stage | File | Focus Area |
|-------|------|------------|
| **Channel acquisition** | `core/core_recorder_v2.py` | `_initialize_channels()` destination/encoding stability |
| **Radiod ensure_channel wrapper** | `core/stream_recorder_v2.py` | `RobustManagedStream._ensure_stream()` args passed to `ensure_channel()` |
| **Stream lifecycle** | `stream/stream_manager.py` | Discovery vs creation vs reuse semantics |
| **Legacy control path** | `channel_manager.py` | `create_channel()` and destination resolution behavior |
| **Symptoms** | `radiod_health.py` | Any reporting of channel counts / SSRC drift |

#### 3. Investigation Checklist (What to Prove)

- [ ] **Are we calling `ensure_channel()` with stable parameters?**
  - Compare the exact args across processes: `frequency_hz`, `preset`, `sample_rate`, `encoding`, `destination`.
  - Pay special attention to `destination=None` vs an explicit multicast IP.

- [ ] **Does destination selection vary across restarts?**
  - `core_recorder_v2.py` currently sets `use_destination = self.data_destination` and attempts to “latch” onto existing destinations, but discovery/dedup is intentionally disabled.
  - If discovery is disabled, verify whether `use_destination` is always stable.

- [ ] **Is encoding the duplication trigger?**
  - `stream_recorder_v2.py` explicitly passes `encoding=F32` to `ensure_channel()`.
  - If *any* other client creates S16 channels at the same frequency, ensure_channel may create a parallel F32 channel.

- [ ] **Is there more than one component managing channels?**
  - Confirm whether any service besides the core recorder calls `RadiodControl` to create/ensure IQ channels.
  - Identify legacy paths that might still be active (`channel_manager.py`, older recorder variants).

- [ ] **Is there an infinite duplicate loop?**
  - A classic failure mode is: request A uses destination X, request B uses destination None → radiod assigns Y, then A sees mismatch and creates again.
  - The fix is to make destination and encoding deterministic and identical across all requesters.

---

## RECEIVER PROLIFERATION PLAYBOOK (NEXT SESSION)

### What to capture first

- The radiod channel list *before* starting services
- The radiod channel list immediately after starting core recorder
- The radiod channel list after restarting core recorder (should be identical)

### What to compare

For any duplicated frequency, compare:

- encoding (S16 vs F32)
- destination (multicast IP)
- preset + sample_rate

### High-signal code hotspots

- `src/hf_timestd/core/core_recorder_v2.py` `_initialize_channels()` (destination selection and stability)
- `src/hf_timestd/core/stream_recorder_v2.py` `RobustManagedStream._ensure_stream()` (exact `ensure_channel()` args)
- `src/hf_timestd/stream/stream_manager.py` (reuse vs create policy)
- `src/hf_timestd/channel_manager.py` (legacy channel creation; destination resolution)

### Commands (manual)

```bash
# Show current channels (SSRC, freq, preset, sample_rate, destination)
python -c "from ka9q import discover_channels; import json; ch=discover_channels('radiod.local'); print(json.dumps({k: {'freq': v.frequency, 'preset': v.preset, 'rate': v.sample_rate, 'dest': getattr(v,'multicast_address',None), 'encoding': getattr(v,'encoding',None)} for k,v in ch.items()}, indent=2))"

# Find all call sites that can create/ensure channels
grep -R "ensure_channel\|create_channel\|RadiodControl" -n src/hf_timestd/
```

#### 4. Verified Improvements (2025-12-20)

- **Calibration Loading**: Logs confirm `CorrelatorBank: WWV calibration updated`.
- **Fusion Logic**: Fusion service successfully generating `Fused D_clock` from 17 broadcasts.
- **Geometric Fallback**: `PropagationEngine` correctly falls back to Geometric model when IRI is missing.
- **Detector Stability**: BPM detection running with specific calibration, reducing false positives on WWV.

---

### CRITIQUE CHECKLIST: TIMING ANALYSIS

#### 1. StationModel Correctness (`station_model.py`)

**Question:** Are the physics-based station models accurate?

- [ ] **Propagation delay calculation**: Is `distance_km / 299.792458` correct for HF?
  - This assumes speed of light in vacuum
  - HF propagation is via ionospheric reflection, not direct path
  - **POTENTIAL ERROR**: Should use great-circle distance × 1.1-1.5 factor for ionospheric path

- [ ] **BPM timing offset**: Is `-20 ms` correct?
  - BPM transmits 20 ms BEFORE UTC second boundary
  - This means BPM pulses arrive at `expected_delay - 20 ms`
  - **VERIFY**: Check against actual BPM reception data

- [ ] **Confidence windows**: Are calibration/ground-truth minutes correct?

  ```python
  BPM_UT1_MINUTES = {25, 26, 27, 28, 29, 55, 56, 57, 58, 59}
  BPM_PURE_CARRIER_MINUTES = {10, 11, 12, 13, 14, 15, 40, 41, 42, 43, 44, 45}
  ```

  - **VERIFY**: Cross-reference with BPM broadcast schedule

- [ ] **Search window sizing**: Is `±10 ms` (calibrated) / `±50 ms` (bootstrap) appropriate?
  - Ionospheric delays vary by 2-60 ms typically
  - Mode changes can cause 5-10 ms jumps
  - **POTENTIAL ISSUE**: ±10 ms may be too tight for disturbed conditions

#### 2. CorrelatorBank Implementation (`correlator_bank.py`)

**Question:** Is the correlator bank correctly implementing MLE?

- [ ] **Template generation**: Are quadrature templates correct?

  ```python
  template_sin = np.sin(2 * np.pi * model.tone_frequency_hz * t) * window
  template_cos = np.cos(2 * np.pi * model.tone_frequency_hz * t) * window
  ```

  - **VERIFY**: Template duration matches station tick duration
  - **VERIFY**: Tukey window α=0.1 is appropriate

- [ ] **Search window centering**: Is the search centered correctly?

  ```python
  center_ms = model.expected_delay_ms + model.timing_offset_ms
  ```

  - For BPM: `44.1 ms + (-20 ms) = 24.1 ms`
  - For WWVH: `25.3 ms + 0 ms = 25.3 ms`
  - **POTENTIAL ISSUE**: BPM and WWVH windows overlap (~1.2 ms apart)

- [ ] **Sub-sample refinement**: Is parabolic interpolation correct?

  ```python
  refined_offset = 0.5 * (y_m1 - y_p1) / (y_m1 - 2*y_0 + y_p1)
  ```

  - Standard parabolic interpolation formula
  - **VERIFY**: Clamping to ±0.5 samples is appropriate

- [ ] **Cross-validation logic**: Is emission time back-calculation correct?

  ```python
  t_emission = result.toa_refined_ms - expected_delay
  ```

  - All stations transmit at same UTC instant
  - After correcting for propagation, emission times should agree
  - **THRESHOLD**: 5 ms cross-validation threshold - is this appropriate?

- [ ] **Multi-second averaging**: Is averaging across seconds 1-10 valid?
  - Assumes propagation is stable across 10 seconds
  - **POTENTIAL ISSUE**: Fading can cause significant variation within 10 seconds

#### 3. BPM UT1 Pulse Detection (`bpm_discriminator.py`)

**Question:** Is the 100 ms UT1 pulse detection robust?

- [ ] **Pulse duration filter**: Is 70-150 ms range correct?

  ```python
  if 70.0 <= duration_ms <= 150.0:
  ```

  - BPM UT1 pulses are nominally 100 ms
  - **VERIFY**: Is ±30 ms tolerance appropriate for multipath spreading?

- [ ] **Threshold calculation**: Is adaptive threshold robust?

  ```python
  threshold = median_env + 3 * mad * 1.4826
  ```

  - Uses median + 3×MAD (robust to outliers)
  - **POTENTIAL ISSUE**: May fail in high-noise conditions

- [ ] **Minimum pulse count**: Is 5 pulses sufficient?

  ```python
  if len(pulses) < 5:
      return None
  ```

  - Expect ~59 pulses per minute
  - **QUESTION**: Why not require more pulses for calibration?

- [ ] **ToA residual calculation**: Is expected arrival correct?

  ```python
  expected_arrival_offset_ms = self.expected_delay_ms + timing_offset_ms
  expected_toa = p['second'] * 1000.0 + expected_arrival_offset_ms
  ```

  - **VERIFY**: Does this correctly account for BPM's -20 ms offset?

#### 4. Doppler-Compensated BCD (`wwvh_discrimination.py`)

**Question:** Is Doppler de-rotation improving coherent integration?

- [ ] **Doppler estimation source**: Where does Doppler come from?

  ```python
  doppler_info = self.estimate_doppler_shift_from_ticks(iq_samples, sample_rate)
  ```

  - Uses per-tick phase progression
  - **VERIFY**: Is this estimate accurate enough for de-rotation?

- [ ] **De-rotation implementation**: Is the phasor correct?

  ```python
  derotation = np.exp(-2j * np.pi * avg_doppler_hz * t)
  derotated = analytic_bcd * derotation
  ```

  - Negative sign removes positive Doppler
  - **VERIFY**: Sign convention matches Doppler estimation

- [ ] **Window overlap**: Is 50% overlap appropriate?

  ```python
  overlap_fraction: float = 0.5
  ```

  - 50% overlap is standard for Welch-style averaging
  - **QUESTION**: Does this provide sufficient time resolution?

- [ ] **Averaging WWV/WWVH Doppler**: Is this valid?

  ```python
  avg_doppler_hz = (wwv_doppler_hz + wwvh_doppler_hz) / 2.0
  ```

  - **POTENTIAL ISSUE**: WWV and WWVH may have different Doppler if paths differ

#### 5. Phase2TemporalEngine Integration

**Question:** Is the integration correct and complete?

- [ ] **BPM UT1 calibration timing**: When does calibration run?

  ```python
  if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0) and minute_number in {25, 26, 27, 28, 29, 55, 56, 57, 58, 59}:
  ```

  - Only runs on shared frequencies during UT1 minutes
  - **VERIFY**: Does calibration persist across minutes?

- [ ] **Correlator bank fallback**: What if correlator bank fails?

  ```python
  if result.bcd_wwv_amplitude is None or result.bcd_wwvh_amplitude is None:
      # Fall back to BCD correlation
  ```

  - Falls back to existing BCD correlation
  - **VERIFY**: Is this fallback tested?

- [ ] **ChannelAssignment usage**: Are all fields being used?
  - `wwv_component_power_db`, `wwvh_component_power_db`, `bpm_component_power_db`
  - `wwv_toa_ms`, `wwvh_toa_ms`, `bpm_toa_ms`
  - `cross_validation_error_ms`, `cross_validation_passed`
  - **QUESTION**: Is `bpm_usable_for_timing` being respected downstream?

---

### POTENTIAL WEAKNESSES AND ERRORS

#### 1. **Propagation Delay Model**

**Issue:** `StationModelFactory` uses straight-line distance for delay calculation.

```python
delay_ms = distance_km / 299.792458  # Speed of light
```

**Problem:** HF propagation is via ionospheric reflection, not direct path. The actual path length is:

- 1F hop: ~1.1-1.2× great-circle distance
- 2F hop: ~1.3-1.5× great-circle distance

**Impact:** Expected delays may be 10-50% too short, causing search windows to be misaligned.

**Recommendation:** Add ionospheric path factor based on frequency and time of day.

#### 2. **BPM/WWVH Overlap**

**Issue:** At receiver location EM38, BPM and WWVH arrivals are only ~1.2 ms apart.

| Station | Expected Delay | Net Arrival |
|---------|----------------|-------------|
| WWV     | 4.3 ms         | 4.3 ms      |
| WWVH    | 25.3 ms        | 25.3 ms     |
| BPM     | 44.1 ms        | 24.1 ms (−20 ms offset) |

**Problem:** With ±10 ms search windows, BPM and WWVH windows overlap significantly.

**Impact:** Correlator may confuse BPM and WWVH, especially during non-UT1 minutes when BPM has 10 ms ticks (vs WWVH's 5 ms).

**Recommendation:** Use tick duration as discriminator - BPM's 10 ms ticks are 2× longer than WWVH's 5 ms.

#### 3. **Calibration Persistence**

**Issue:** BPM calibration updates `self.bpm_calibration` dict, but this is instance state.

**Problem:** If the Phase2TemporalEngine is restarted, calibration is lost.

**Impact:** System must re-calibrate from UT1 minutes after every restart.

**Recommendation:** Persist calibration to state file (like `timing_calibration.json`).

#### 4. **Cross-Validation Threshold**

**Issue:** Cross-validation uses 5 ms threshold.

```python
assignment.cross_validation_passed = max_error < 5.0
```

**Problem:** Ionospheric conditions can cause >5 ms variation between stations.

**Impact:** Valid measurements may be flagged as failing cross-validation.

**Recommendation:** Make threshold adaptive based on ionospheric conditions (Kp index, time of day).

#### 5. **Doppler Averaging**

**Issue:** Doppler de-rotation uses average of WWV and WWVH Doppler.

```python
avg_doppler_hz = (wwv_doppler_hz + wwvh_doppler_hz) / 2.0
```

**Problem:** WWV and WWVH propagate via different ionospheric paths with potentially different Doppler.

**Impact:** De-rotation may be suboptimal for both stations.

**Recommendation:** Apply station-specific de-rotation, or use dominant station's Doppler.

---

### MISSED OPPORTUNITIES

#### 1. **BPM Pure Carrier Minutes**

BPM transmits pure carrier (no time code) during minutes 10-15 and 40-45. This could be used for:

- High-precision carrier phase measurement
- Path gain calibration without BCD interference
- Doppler estimation from carrier frequency offset

**Current Status:** `BPM_PURE_CARRIER_MINUTES` constant defined but not used.

#### 2. **CHU 1000 Hz Tone Correlation**

CHU also transmits 1000 Hz tones (same as WWV/BPM). On CHU-only frequencies (3.33, 7.85, 14.67 MHz), this provides unambiguous timing.

**Current Status:** CHU is handled separately, not integrated into correlator bank.

#### 3. **Multi-Frequency Consistency**

WWV transmits on 2.5, 5, 10, 15, 20, 25 MHz simultaneously. All should show the same D_clock (after propagation correction).

**Current Status:** Each frequency processed independently; no cross-frequency validation.

#### 4. **Ionospheric Model Integration**

The system has `ionospheric_model.py` with IRI-2016 layer heights, but this isn't used for:

- Predicted propagation delays
- Search window sizing
- Mode disambiguation

**Current Status:** Ionospheric model exists but is underutilized.

---

### VALIDATION COMMANDS

```bash
# Test StationModel propagation delays
python3 -c "
from src.hf_timestd.core.station_model import StationModelFactory
f = StationModelFactory(38.918, -92.128)
for sid, model in f.create_all_models().items():
    print(f'{sid.value}: dist={model.distance_km:.0f}km, delay={model.expected_delay_ms:.1f}ms, offset={model.timing_offset_ms:.1f}ms')
"

# Test CorrelatorBank search windows
python3 -c "
from src.hf_timestd.core.correlator_bank import create_correlator_bank
bank = create_correlator_bank(38.918, -92.128)
for model in bank.factory.get_models_for_frequency(10.0):
    center, width = model.get_search_window(25, calibrated=False)
    print(f'{model.station.value}: center={center:.1f}ms, width=±{width:.1f}ms')
"

# Check BPM UT1 detection
python3 -c "
from src.hf_timestd.core.bpm_discriminator import create_bpm_discriminator
bpm = create_bpm_discriminator(38.918, -92.128)
print(f'UT1 minutes: {sorted(bpm.UT1_MINUTES)}')
print(f'Expected delay: {bpm.expected_delay_ms:.1f}ms')
"

# Verify Phase2TemporalEngine integration
python3 -c "
from src.hf_timestd.core.phase2_temporal_engine import Phase2TemporalEngine
import inspect
src = inspect.getsource(Phase2TemporalEngine._step2_channel_characterization)
print('BPM UT1 calibration:', 'detect_ut1_pulses' in src)
print('CorrelatorBank:', 'correlator_bank.process_minute' in src)
"
```

---

### SUCCESS CRITERIA FOR TIMING ANALYSIS REVIEW

| Metric | Target | How to Verify |
|--------|--------|---------------|
| Propagation delay accuracy | ±5 ms | Compare predicted vs measured ToA |
| BPM/WWVH discrimination | >95% correct | Check UT1 vs non-UT1 minute detection |
| Cross-validation pass rate | >80% | Monitor `cross_validation_passed` |
| D_clock stability | σ < 3 ms | Check intra-station spread |
| Calibration persistence | Survives restart | Kill/restart service, check state |

---

## 🟡 PREVIOUS FOCUS: WEB UI INTEGRATION REVIEW

---

### RECENT BACKEND CHANGES (2025-12-16 Session)

The following significant changes were made to the backend that the Web UI must integrate with:

#### 1. MultiStationDetector (Replaces Voting)

**Old Approach (DEPRECATED):**

- `GlobalStationVoter` picked the "loudest" station
- Single station used for timing
- Other detected stations discarded

**New Approach:**

- `MultiStationDetector` detects ALL receivable stations
- GPSDO is the timing reference, not the loudest station
- Each station's ToA reveals propagation conditions
- ALL measurements passed to fusion with uncertainty weighting

**Files Changed:**

- `phase2_temporal_engine.py` - Now uses `MultiStationDetector`
- `multi_station_detector.py` - **NEW** Physics-based detection
- `global_station_voter.py` - **DEPRECATED**
- `station_lock_coordinator.py` - **DEPRECATED**

**Web UI Impact:**

- [ ] Does the UI still reference "voting" or "anchor" concepts?
- [ ] Are there displays that only show one station per frequency?
- [ ] Does the UI show multi-station detection results?

#### 2. BPM (China) Station Integration

**New Capability:**

- BPM broadcasts on 2.5, 5, 10, 15 MHz (shared with WWV/WWVH)
- 10ms tick duration (vs 5ms WWV)
- UT1 minutes (25-29, 55-59) not usable for UTC timing
- Now detected and passed to fusion

**Files Changed:**

- `phase2_temporal_engine.py` - BPM detection in Step 1
- `bpm_discriminator.py` - BPM-specific analysis
- `phase2_analytics_service.py` - BPM fields in CSV outputs

**Web UI Impact:**

- [ ] Does the UI display BPM detections?
- [ ] Are BPM timing modes (UTC/UT1) shown?
- [ ] Is BPM included in station lists and charts?

#### 3. Tiered Storage (RAM Hot Buffer)

**New Capability:**

- `/dev/shm/timestd` for hot buffer (RAM)
- Disk for cold buffer with background archival
- Auto-configured based on available RAM

**Files Changed:**

- `tiered_storage.py` - **NEW** Storage manager
- `binary_archive_writer.py` - Tiered storage integration

**Web UI Impact:**

- [ ] Does the UI show storage tier status?
- [ ] Are file paths updated for tiered storage?

#### 4. BCD Downsampling (CPU Optimization)

**Change:**

- BCD correlation now uses 4x downsampling (20 kHz → 5 kHz)
- Reduces CPU by ~75% with negligible accuracy loss

**Files Changed:**

- `wwvh_discrimination.py` - `downsample_factor` parameter

**Web UI Impact:**

- [ ] None expected (internal optimization)

#### 5. Chrony Update Rate Limiting

**Change:**

- Chrony SHM updates now rate-limited to 8 seconds
- Matches chrony poll interval

**Files Changed:**

- `multi_broadcast_fusion.py` - Rate limiting logic

**Web UI Impact:**

- [ ] Does the UI show chrony update frequency?

#### 6. CSV Schema Changes

**Tone Detections CSV:**

- Added: `bpm_detected`, `bpm_snr_db`, `bpm_timing_ms`, `bpm_timing_mode`, `bpm_usable_for_utc`

**BCD Discrimination CSV:**

- Added: `bpm_amplitude`, `bpm_toa_ms`

**Web UI Impact:**

- [ ] Does the UI parse the new CSV columns?
- [ ] Are there hardcoded column indices that will break?

---

### WEB UI FILE INVENTORY

| File | Purpose | Review Priority |
|------|---------|-----------------|
| `monitoring-server-v3.js` | Express server, API endpoints | HIGH |
| `timestd-paths.js` | Path construction (must match Python) | HIGH |
| `timing-dashboard-enhanced.html` | Main dashboard | HIGH |
| `discrimination-charts.js` | Station discrimination visualization | MEDIUM |
| `components/navigation.js` | Navigation component | LOW |
| `components/theme-toggle.js` | Theme switching | LOW |

---

### CRITIQUE CHECKLIST: WEB UI INTEGRATION

#### 1. Data Contract Consistency

- [ ] **CSV Column Names**: Do JS parsers match Python CSV writers?
  - Check: `monitoring-server-v3.js` CSV parsing
  - Check: New BPM columns in tone_detections and bcd_discrimination

- [ ] **JSON Schema**: Do status file readers match writers?
  - Check: `analytics-service-status.json` schema
  - Check: `convergence_state.json` schema

- [ ] **Path Construction**: Does `timestd-paths.js` match `paths.py`?
  - Check: `raw_buffer` vs `raw_archive` naming
  - Check: Channel name sanitization (`WWV 10 MHz` → `WWV_10_MHz`)

#### 2. Deprecated Concept References

- [ ] **Voting/Anchor References**: Remove or update
  - Search for: "voter", "voting", "anchor", "best station"
  - Replace with: Multi-station detection concepts

- [ ] **Legacy broadcast counts**: Remove hardcoded broadcast counts
  - Search for: "13 broadcasts", "13-broadcast" (legacy text)
  - Update to: "all available broadcasts" and ensure BPM is included

#### 3. Missing Features

- [ ] **BPM Display**: Add BPM to station lists
  - Station selector dropdowns
  - Detection charts
  - Timing displays

- [ ] **Multi-Station View**: Show all detected stations per frequency
  - Currently may only show "dominant" station
  - Should show all with SNR/ToA

- [ ] **Propagation Analysis**: Show measured vs expected delay
  - New `delay_residual_ms` field available
  - Indicates ionospheric conditions

#### 4. Security Vulnerabilities

- [ ] **Path Traversal**: Are file paths validated?
  - Check: API endpoints that read files
  - Check: User-supplied channel names

- [ ] **Injection**: Are inputs sanitized?
  - Check: Query parameters
  - Check: File path construction

#### 5. Error Handling

- [ ] **Missing Files**: Graceful handling?
  - Check: What happens when CSV doesn't exist?
  - Check: What happens when status file is stale?

- [ ] **Malformed Data**: Validation before use?
  - Check: JSON parse errors
  - Check: CSV with wrong column count

#### 6. Performance

- [ ] **File Watching**: Efficient?
  - Check: Are files re-read unnecessarily?
  - Check: Is there caching?

- [ ] **Large CSV Handling**: Memory-safe?
  - Check: Are entire files loaded into memory?
  - Check: Is there pagination?

---

### SPECIFIC AREAS TO AUDIT

#### API Endpoints (monitoring-server-v3.js)

| Endpoint | Purpose | Check For |
|----------|---------|-----------|
| `/api/channels` | List channels | BPM channels included? |
| `/api/status/:channel` | Channel status | New fields present? |
| `/api/detections/:channel` | Tone detections | BPM columns parsed? |
| `/api/discrimination/:channel` | Discrimination data | BPM fields included? |
| `/api/fusion` | Fused D_clock | 17 broadcasts shown? |

#### Dashboard Components

| Component | Purpose | Check For |
|-----------|---------|-----------|
| Station selector | Choose station | BPM option? |
| Detection chart | Show detections | Multi-station view? |
| Timing display | Show D_clock | All stations shown? |
| Propagation panel | Show delays | Residual display? |

---

### VALIDATION COMMANDS

```bash
# Check for deprecated references
grep -r "voter\|voting\|anchor" web-ui/

# Check for hardcoded broadcast counts
grep -r "13 broadcast\|13-broadcast" web-ui/

# Check path consistency
diff <(grep -o "raw_buffer\|raw_archive" web-ui/*.js | sort -u) \
     <(grep -o "raw_buffer\|raw_archive" src/hf_timestd/*.py | sort -u)

# Check CSV column references
grep -r "bpm_detected\|bpm_snr\|bpm_timing" web-ui/

# List all API endpoints
grep -E "app\.(get|post|put|delete)" web-ui/monitoring-server-v3.js
```

---

### SUCCESS CRITERIA

| Metric | Target | How to Verify |
|--------|--------|---------------|
| BPM display | Shown in UI | Visual inspection |
| Multi-station view | All stations shown | Check detection charts |
| No deprecated refs | Zero "voter" mentions | grep search |
| CSV parsing | No errors | Check browser console |
| Path consistency | 100% match | Diff Python vs JS |

---

### DATA PIPELINE ARCHITECTURE OVERVIEW

The `hf_timestd.core` module implements a two-phase data pipeline for HF time standard analysis:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 1: RAW DATA CAPTURE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  RTP Stream (ka9q-radio / radiod)                                           │
│       ↓                                                                     │
│  ka9q-python RadiodControl.ensure_channel(...)                               │
│       ↓                                                                     │
│  ka9q-python RadiodStream                                                    │
│       ↓                                                                     │
│  CoreRecorderV2 / StreamRecorderV2 → BinaryArchiveWriter                     │
│       ↓                                                                     │
│  raw_buffer/*.bin + metadata.json                                            │
└─────────────────────────────────────────────────────────────────────────────┘
       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 2: ANALYTICAL ENGINE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│  Phase2AnalyticsService                                                     │
│       ↓                                                                     │
│  Phase2TemporalEngine (3-step analysis)                                     │
│       ├─ Step 1: Time Snap (tone detection, minute boundary)                │
│       ├─ Step 2: Channel Characterization (discrimination, SNR)             │
│       └─ Step 3: Transmission Time Solution (D_clock calculation)           │
│       ↓                                                                     │
│  ClockOffsetSeries → MultiBroadcastFusion → ClockConvergenceModel           │
│       ↓                    ↓                       ↓                        │
│  clock_offset/*.csv   fusion/*.csv          convergence_state.json          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### CORE MODULE INVENTORY (54 files, ~1.2M bytes)

#### Phase 1 - Data Capture Pipeline

| File | Size | Purpose | Robustness Concerns |
|------|------|---------|---------------------|
| `core_recorder_v2.py` | 28 KB | RadiodStream-based core recorder | ensure_channel idempotency, destination/encoding stability |
| `stream_recorder_v2.py` | 16 KB | RadiodStream wrapper + resilience | ensure_channel args, recovery behavior |
| `binary_archive_writer.py` | 18 KB | Binary IQ + JSON metadata | Disk full, partial writes |
| `recording_session.py` | 22 KB | Minute segmentation | Boundary alignment, crash recovery |
| `audio_buffer.py` | 5 KB | Ring buffer for samples | Overflow, underflow |

#### Phase 2 - Analytical Engine

| File | Size | Purpose | Robustness Concerns |
|------|------|---------|---------------------|
| `phase2_analytics_service.py` | 66 KB | Service orchestration | File watching, state reload |
| `phase2_temporal_engine.py` | 79 KB | 3-step temporal analysis | Exception handling, timeouts |
| `tone_detector.py` | 68 KB | Matched filter detection | FFT errors, edge cases |
| `wwvh_discrimination.py` | 184 KB | 8-vote discrimination | Vote correlation, confidence |
| `clock_offset_series.py` | 30 KB | D_clock calculation | RTP calibration, outliers |
| `multi_broadcast_fusion.py` | 50 KB | Multi-broadcast fusion + global differential constraint | State poisoning, divergence |
| `clock_convergence.py` | 42 KB | Convergence tracking | State persistence, reset |

#### Supporting Modules

| File | Size | Purpose | Robustness Concerns |
|------|------|---------|---------------------|
| `timing_calibrator.py` | 39 KB | Bootstrap → Calibrated phases | Multi-process coordination |
| `global_station_voter.py` | 26 KB | Cross-channel station lock | IPC race conditions |
| `station_lock_coordinator.py` | 18 KB | Guided detection | Lock file contention |
| `propagation_mode_solver.py` | 28 KB | Ionospheric mode ID | Model assumptions |
| `ionospheric_model.py` | 43 KB | IRI-2016 layer heights | External data dependencies |

#### New Matched Filter Templates (2025-12-16)

| File | Size | Purpose | Robustness Concerns |
|------|------|---------|---------------------|
| `tick_matched_filter.py` | 23 KB | Per-second tick detection | Window edge effects |
| `signal_templates.py` | 35 KB | BCD/AFSK/BPM templates | Template accuracy |

---

### CRITIQUE CHECKLIST: PIPELINE ROBUSTNESS

#### 1. Error Handling and Recovery

- [ ] **Stream dropouts**: How does the system handle sustained packet loss (>5%) or radiod restarts?
  - Check: `stream_recorder_v2.py` `RobustManagedStream` / `RadiodStream` handling
  - Check: Does the writer mark minutes as incomplete or just low-completeness?

- [ ] **Disk full conditions**: What happens when disk is full during write?
  - Check: `binary_archive_writer.py` exception handling
  - Check: Is partial data cleaned up or left corrupted?

- [ ] **Service crash recovery**: Can Phase 2 resume after crash?
  - Check: `phase2_analytics_service.py` state persistence
  - Check: Are incomplete analyses marked and retried?

- [ ] **Network interruption**: How do RadiodStream callbacks behave on network drops?
  - Check: whether `ensure_channel()` is re-invoked and whether it creates duplicates

#### 2. State Management and Persistence

- [ ] **State file atomicity**: Are state files written atomically?
  - Pattern: Write to temp file, then rename
  - Check: `timing_calibrator.py`, `clock_convergence.py`, `multi_broadcast_fusion.py`

- [ ] **State file corruption**: What if JSON is malformed?
  - Check: All `json.load()` calls for exception handling
  - Check: Fallback to default state

- [ ] **State file versioning**: Can old state files cause issues?
  - Check: Version fields in state files
  - Check: Migration logic for schema changes

- [ ] **Multi-process state coordination**: 9 channels share state files
  - Check: File locking in `timing_calibrator.py`
  - Check: Race conditions in `global_station_voter.py`

#### 3. Data Integrity

- [ ] **Sample count validation**: Is 1,200,000 samples/minute enforced?
  - Check: `recording_session.py` minute boundary logic
  - Check: What happens with short/long minutes?

- [ ] **RTP timestamp continuity**: Are timestamp jumps detected?
  - Check: `RadiodStream`/`StreamQuality` + downstream calibration in `clock_offset_series.py`

- [ ] **Binary file integrity**: Are binary files validated on read?
  - Check: `binary_archive_writer.py` reader validation
  - Check: Checksum or size validation

- [ ] **Metadata consistency**: Does JSON metadata match binary data?
  - Check: `sample_count` in JSON vs actual file size
  - Check: `start_rtp_timestamp` accuracy

#### 4. Resource Management

- [ ] **Memory growth**: Are there unbounded data structures?
  - Check: `phase2_temporal_engine.py` result accumulation
  - Check: `wwvh_discrimination.py` history buffers

- [ ] **File handle leaks**: Are files properly closed?
  - Check: Context managers (`with open(...)`)
  - Check: Exception paths that might skip cleanup

- [ ] **Thread safety**: Are shared resources protected?
  - Check: `core_recorder_v2.py` threading model
  - Check: `phase2_analytics_service.py` file watcher

- [ ] **Graceful shutdown**: Does the system shut down cleanly?
  - Check: Signal handlers in services
  - Check: Pending writes flushed

#### 5. Edge Cases and Boundary Conditions

- [ ] **Minute boundary alignment**: What if first packet isn't at :00?
  - Check: `recording_session.py` initial alignment

- [ ] **Leap second handling**: How are leap seconds handled?
  - Check: 61-second minutes
  - Check: RTP timestamp discontinuity

- [ ] **Day/hour boundaries**: Any issues at 00:00 UTC?
  - Check: File naming, directory creation
  - Check: State file rotation

- [ ] **Low SNR conditions**: Does the system degrade gracefully?
  - Check: `tone_detector.py` minimum SNR thresholds
  - Check: `wwvh_discrimination.py` low-confidence handling

#### 6. Logging and Observability

- [ ] **Error logging**: Are all exceptions logged with context?
  - Check: `logger.exception()` vs `logger.error()`
  - Check: Sufficient context for debugging

- [ ] **Performance metrics**: Can we identify bottlenecks?
  - Check: Processing time logging
  - Check: Queue depth monitoring

- [ ] **Health checks**: Can we verify system is working?
  - Check: Status files, heartbeats
  - Check: Stale data detection

---

### SPECIFIC FILES TO AUDIT (Priority Order)

#### Critical Path (Phase 1)

1. **`core_recorder_v2.py`** - Channel acquisition + stream start
   - Destination stability and encoding consistency
   - `RadiodControl.ensure_channel()` call patterns
   - Behavior across restarts (idempotency)

2. **`stream_recorder_v2.py`** - `RobustManagedStream` wrapper
   - Exact `ensure_channel()` arguments
   - Whether destination/encoding differ across channels or processes

3. **`recording_session.py`** - Minute boundary logic
   - State machine correctness
   - Crash recovery
   - Partial minute handling

4. **`binary_archive_writer.py`** - Data persistence
   - Atomic writes
   - Compression errors
   - Disk space handling

#### Critical Path (Phase 2)

1. **`phase2_analytics_service.py`** - Service orchestration
   - File watcher reliability
   - State reload safety
   - Exception isolation

2. **`clock_offset_series.py`** - D_clock calculation
   - RTP calibration correctness
   - Outlier handling
   - Numerical stability

3. **`multi_broadcast_fusion.py`** - Kalman fusion
   - State persistence
   - Divergence detection
   - Measurement rejection

4. **`timing_calibrator.py`** - Multi-process coordination
   - File locking
   - State consistency
   - Bootstrap exit conditions

---

### KNOWN ISSUES FROM PREVIOUS SESSIONS

#### 1. Kalman State Poisoning (Fixed 2025-12-08)

- **Issue**: Corrupted Kalman state persisted across restarts
- **Fix**: Added state versioning and sanity checks
- **Verify**: Check `clock_convergence.py` for validation

#### 2. Multi-Process State Race (Partially Fixed)

- **Issue**: 9 channel processes share state files
- **Current**: Reload-before-update pattern
- **Remaining**: No file locking - race conditions possible

#### 3. Discrimination Flip-Flopping (Ongoing)

- **Issue**: Station ID changes between minutes
- **Impact**: ~50ms D_clock errors when wrong station
- **Mitigation**: RTP-based station prediction

---

### VALIDATION COMMANDS

```bash
# Check for Python exceptions in logs
grep -r "Traceback\|Exception\|Error" /tmp/timestd-test/logs/

# Monitor file descriptor usage
ls -la /proc/$(pgrep -f phase2_analytics)/fd/ | wc -l

# Check state file sizes (should be stable)
watch -n 60 'ls -la /tmp/timestd-test/state/*.json'

# Verify minute file integrity
for f in /tmp/timestd-test/raw_buffer/*/*.bin; do
  size=$(stat -c%s "$f")
  expected=$((1200000 * 4))  # 1.2M samples * 4 bytes (complex int16)
  if [ "$size" -ne "$expected" ]; then
    echo "INCOMPLETE: $f ($size bytes)"
  fi
done

# Check for stale processing
find /tmp/timestd-test/phase2 -name "*.json" -mmin +5 -ls
```

---

### SUCCESS CRITERIA FOR ROBUSTNESS

| Metric | Target | How to Verify |
|--------|--------|---------------|
| Packet loss tolerance | <5% loss → no data corruption | Inject packet loss, verify output |
| Crash recovery | Resume within 1 minute | Kill process, verify restart |
| State file integrity | No corruption after 24h | Monitor state files continuously |
| Memory stability | <500 MB after 24h | Monitor RSS over time |
| File handle stability | <100 handles | Monitor /proc/*/fd |
| Error rate | <1 exception/hour | Count log exceptions |

---

### PREVIOUS FOCUS: GPSDO-FIRST TIMING CALIBRATION METHODOLOGY

**Status:** ✅ Implemented (2025-12-14)

The system leverages GPSDO-disciplined RTP timestamps as the **primary timing foundation**, then progressively refines with tone detections and multi-broadcast fusion:

```
LAYER 1: GPSDO Foundation
├─ RTP timestamps from GPS-disciplined ka9q-radio (±0.1 PPM)
├─ All 9 channels share the same master clock
└─ Sample count integrity: 1,200,000 samples = exactly 60 seconds

LAYER 2: Tone Detection
├─ WWV/WWVH 1000/1200 Hz tones at second 0 (800ms duration)
├─ CHU 1000 Hz tone at second 0 (500ms duration)
├─ Per-second tick confirmations (59 per minute, 5ms each)
└─ CHU FSK timing (seconds 31-39) for independent verification

LAYER 3: Station-Level Calibration
├─ Each station (WWV, WWVH, CHU) has ONE atomic clock
├─ Station mean is ground truth; frequency variance = propagation
└─ Calibration offset brings station mean to UTC(NIST) = 0

LAYER 4: Multi-Broadcast Fusion
├─ Weighted average across all available broadcasts (WWV/WWVH/CHU/BPM)
├─ Kalman filter for convergence and anomaly detection
└─ Intra-station consistency checks for discrimination validation
```

#### Key Implementation Files

| File | Purpose | Critical Functions |
|------|---------|-------------------|
| `timing_calibrator.py` | Bootstrap → Calibrated → Verified phases | `predict_station()`, `update_from_detection()` |
| `phase2_temporal_engine.py` | Three-step temporal analysis | `_step1_time_snap()`, `_step2_channel_characterization()`, `_step3_transmission_time_solution()` |
| `clock_offset_series.py` | ClockOffsetEngine with RTP calibration | `_get_calibrated_rtp_offset()`, `process_minute()` |
| `multi_broadcast_fusion.py` | Station-level calibration + Kalman | `_update_calibration()`, `_kalman_update()`, `fuse()` |
| `wwvh_discrimination.py` | 8-vote weighted discrimination | `finalize_discrimination()`, `detect_tick_windows()` |
| `propagation_mode_solver.py` | Ionospheric mode identification | `solve()` |
| `pipeline_orchestrator.py` | Wires timing_calibrator to ClockOffsetEngine | `_get_calibrated_rtp_offset()` |

---

### CRITIQUE CHECKLIST: METHODOLOGY VALIDATION

#### 1. GPSDO Foundation Assumptions

**Question:** Are we correctly leveraging the GPSDO stability?

- [x] **RTP offset predictability**: Does `rtp_timestamp % 1,200,000` actually remain constant across minutes?
  - ✅ CONFIRMED: RTP offset is deterministic with GPSDO (~100ns stability)
  - ✅ IMPLEMENTED (2025-12-14): RTP-first timing uses calibrated offset as gold standard ruler
  - Validation: Check `timing_calibration.json` for RTP offset drift warnings

- [ ] **Cross-channel coherence**: Are all channels truly sharing the same clock?
  - Potential issue: Different RTP origins per channel could mask clock issues
  - Validation: Compare tone arrival times across channels for same minute

- [x] **Sample count integrity**: Is 1,200,000 samples always exactly 60 seconds?
  - ✅ With GPSDO: Yes, to ±0.1 PPM (±7.2ms/day max drift)
  - Validation: Check PPM estimates in time_snap data

#### 2. Tone Detection Accuracy

**Question:** Are we detecting tones at the correct positions?

- [ ] **Matched filter template**: Is the 800ms template correct for WWV/WWVH?
  - NIST confirms 800ms duration for timing tones
  - CHU uses 500ms (1000ms at top of hour)

- [ ] **Search window**: Is ±500ms (bootstrap) → ±50ms (calibrated) appropriate?
  - Ionospheric delays range 2-60ms typically
  - Propagation mode changes can cause 5-10ms jumps

- [ ] **Per-second tick detection**: Are we using 59 ticks correctly?
  - Ticks are 5ms pulses at 1000/1200 Hz
  - Coherent integration provides √59 ≈ 7.7x SNR improvement

- [ ] **CHU FSK timing**: Is the 500ms boundary detection accurate?
  - FSK frames at seconds 31-39
  - Should provide independent timing confirmation

#### Next Session Context: Critical Data Pipeline Review

**Objective**: Critically examine the end-to-end data pipeline to identify and fix latent issues, ensuring the system is robust for 24/7 production.

## Key Areas to Investigate

1. **HDF5 vs CSV Integrity**:
    - Verify that `phase2_analytics_service.py` is writing *identical* data to both formats.
    - Check specifically for the "file lock" issue recurrence (search logs for "SWMR write").
    - Confirm `scripts/timestd-analytics.sh`'s new `h5clear` logic is working (or at least not causing harm).

2. **Calibration Stability**:
    - We recently introduced "Two-Tier Calibration" (Bootstrap vs Provisional).
    - **Critique**: Is the system successfully transitioning to `PROVISIONAL`? Or does it get stuck in `BOOTSTRAP`?
    - **Check**: `/var/lib/timestd/state/timing_calibration.json` for `phase` and stability of `propagation_delay_ms`.

3. **Fusion Service Reliability**:
    - This is a new service (`timestd-fusion`).
    - **Check**: Uptime, memory usage, and log errors (`/var/log/hf-timestd/fusion.log`).
    - **Verify**: Is it actually feeding Chrony? (`chronyc sources`, `chronyc tracking`).

4. **Data Gaps**:
    - Look for gaps in the `phase2` data series (`/var/lib/timestd/phase2/...`).
    - Determine if gaps align with service restarts or logic errors.

## Recent Changes (v3.1.0)

- **New Service**: `timestd-fusion.service`
- **New Dependency**: `hdf5-tools` (for `h5clear`)
- **Dual Write**: Writing both `.csv` and `.h5` files.
- **Robustness**: Automated HDF5 lock clearing on startup.

#### 3. Station-Level Calibration Logic

**Question:** Is station-level calibration the right abstraction?

- [ ] **Single clock assumption**: Is it true that all frequencies from one station share the same clock?
  - YES: WWV/WWVH/CHU each have one cesium/rubidium reference
  - Frequency-to-frequency variance is ionospheric, not clock

- [ ] **Station mean calculation**: Are we correctly computing the station mean?
  - Current: `station_mean = np.mean([d_clock for all frequencies])`
  - Potential issue: Should we weight by SNR or quality?

- [ ] **Calibration offset stability**: Does the offset converge or oscillate?
  - EMA smoothing: `new_offset = α × ideal + (1-α) × old_offset`
  - α = max(0.1, 10.0 / n_samples) - faster initially, slower as samples accumulate

#### 4. Multi-Broadcast Fusion

**Question:** Is the fusion algorithm optimal?

- [ ] **Weighting scheme**: Are weights appropriate?
  - Current: SNR-based + quality grade + propagation mode
  - Potential issue: Should discrimination confidence affect weight?

- [ ] **Intra-station consistency**: Are we correctly detecting discrimination errors?
  - Current: Flag DISCRIMINATION_SUSPECT if intra-station σ > 5ms
  - Potential issue: 5ms threshold may be too tight for multi-hop propagation

- [ ] **Kalman filter model**: Is the state model appropriate?
  - Current: [d_clock_offset, drift_rate]
  - Potential issue: Drift rate may not be meaningful with GPSDO

- [ ] **Suspect measurement exclusion**: Are we excluding the right measurements?
  - Current: Exclude measurements that increase intra-station variance
  - Potential issue: May exclude valid measurements during propagation mode changes

#### 5. Discrimination System

**Question:** Is the 8-vote weighted discrimination robust?

| Vote | Method | Weight | Potential Issues |
|------|--------|--------|------------------|
| 0 | Test Signal | 15 | Only minutes 8/44; may miss if signal weak |
| 1 | 440 Hz Station ID | 10 | Only minutes 1/2; harmonic contamination possible |
| 2 | BCD Amplitude | 2-10 | Requires good SNR; dual-peak detection fragile |
| 3 | 1000/1200 Hz Power | 1-10 | Affected by propagation fading |
| 4 | Tick SNR Average | 5 | **NOW CONNECTED** - 59 ticks provide robustness |
| 5 | 500/600 Hz Ground Truth | 10-15 | 14 minutes/hour; most reliable |
| 6 | Doppler Stability | 2 | Requires stable channel; may fail in disturbed conditions |
| 7 | Timing Coherence | 3 | Requires test signal + BCD agreement |

- [ ] **Vote 4 integration**: Is tick SNR now being used correctly?
  - Added: `detect_tick_windows()` call in Step 2B
  - Passed to `finalize_discrimination()` as `tick_results`

- [ ] **RTP-based station prediction**: Is it improving discrimination?
  - `predict_station()` uses RTP offset to predict expected station
  - Should reduce flip-flopping on shared frequencies

- [ ] **Low-confidence rejection**: Are we correctly rejecting low-confidence results?
  - On shared frequencies, require MEDIUM confidence minimum
  - LOW confidence falls through to RTP prediction or channel name

#### 6. Ionospheric Propagation Limits

**Question:** What is the theoretical limit of timing accuracy?

- [ ] **Propagation mode ambiguity**: Can we distinguish 1F vs 2F vs 3F hops?
  - Mode delays differ by ~1-2ms per hop
  - Current uncertainty: ±2-5ms after mode identification

- [ ] **Ionospheric jitter**: What is the irreducible variance?
  - Typical: ±0.5-2ms from ionospheric turbulence
  - Severe conditions: ±5-10ms

- [ ] **Intra-station spread**: What is the expected variance across frequencies?
  - Current observation: WWV σ=4-5ms, CHU σ=4-5ms
  - This may be the ionospheric limit, not a system error

---

### KNOWN ISSUES AND LIMITATIONS

#### 1. Bootstrap Phase Sensitivity

**Issue:** Bootstrap requires high-SNR detections (>15 dB) with high confidence (>0.7).

**Impact:** May take longer to exit bootstrap in weak signal conditions.

**Mitigation:** Consider lowering thresholds or using cross-channel voting.

#### 2. Multi-Process State Coordination

**Issue:** 9 channel recorder processes share one state file.

**Current Fix:** Reload state before update, save after every detection during bootstrap.

**Potential Issue:** Race conditions possible if two processes update simultaneously.

**Mitigation:** Consider file locking or centralized state manager.

#### 3. CHU FSK Detection Sensitivity

**Issue:** CHU FSK decoder may not detect signal in weak conditions.

**Impact:** CHU timing confirmation unavailable when FSK not detected.

**Mitigation:** FSK is optional confirmation; system works without it.

#### 4. Discrimination Flip-Flopping

**Issue:** On shared frequencies, discrimination can flip between WWV and WWVH.

**Current Fix:**

- Reject low-confidence discrimination
- Use RTP-based station prediction
- Store detected_station in RTP calibration

**Remaining Issue:** First detection on a new channel has no RTP history.

---

### VALIDATION COMMANDS

```bash
# Check timing calibration state
cat /tmp/timestd-test/state/timing_calibration.json | python3 -m json.tool

# Check broadcast calibration state
cat /tmp/timestd-test/state/broadcast_calibration.json | python3 -m json.tool

# Monitor fusion convergence
tail -f /tmp/timestd-test/logs/phase2-fusion.log

# Check intra-station spread
grep "intra-station" /tmp/timestd-test/logs/phase2-fusion.log | tail -10

# Verify discrimination corrections
grep "RTP prediction overrides" /tmp/timestd-test/logs/phase1-*.log
```

---

### SUCCESS CRITERIA

| Metric | Target | Current Status |
|--------|--------|----------------|
| D_clock accuracy | ±1 ms | ~±5 ms (discrimination errors dominate) |
| Intra-station spread | <5 ms | ~7-10 ms (discrimination + ionospheric) |
| Discrimination stability | No flip-flopping | 🔴 MAIN ISSUE - misidentification causes ~50ms errors |
| Bootstrap exit | <10 minutes | ✅ ~3-5 minutes |
| Kalman convergence | Grade A/B | Grade C/D (blocked by discrimination) |

### 2025-12-14 UPDATE: RTP-First Timing Implemented

**Root Cause Analysis Complete:**

The D_clock instability is **NOT** caused by RTP timing jitter. The RTP-first timing implementation confirmed that:

1. **RTP offset is stable**: Calibrated offset (e.g., `411038` for WWV 10 MHz) is deterministic
2. **D_clock clusters correctly by station**:
   - WWV: clusters around `-4ms` to `-6.5ms`
   - WWVH: clusters around `-27.9ms`
3. **Outliers are misidentified stations**: When WWVH is misidentified as WWV, wrong propagation delay is applied, causing ~50ms errors

**Remaining Issue: Station Discrimination**

The discrimination system is the bottleneck. When WWVH signals are misidentified as WWV (or vice versa), the wrong propagation delay is used, causing large D_clock errors. This is particularly problematic on shared frequencies (2.5, 5, 10, 15 MHz).

---

## ✅ COMPLETED: DATA FLOW CONTRACT ENFORCEMENT

**Purpose:** Critically examine how data is written and read across the GRAPE Recorder system, identifying mismatches between producers and consumers.

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-08  
**Status:** ✅ Complete - 10 Issues Identified and Fixed

### Summary (Session 2025-12-08)

| ID | Severity | Status | Description |
|----|----------|--------|-------------|
| 1.1 | HIGH | ✅ FIXED | Calibration per-station vs per-broadcast key mismatch |
| 1.2 | HIGH | ✅ FIXED | State file version not validated on load |
| 1.3 | HIGH | ✅ FIXED | Kalman state loaded without sanity checks |
| 2.1 | MEDIUM | ✅ FIXED | CSV column vs API field name documented |
| 2.2 | MEDIUM | ✅ FIXED | Python discover_channels() now checks all phases |
| 2.3 | MEDIUM | ✅ FIXED | PathResolver deprecated with warning |
| 2.4 | MEDIUM | ✅ FIXED | Mode coordination documented, reset script added |
| 2.5 | MEDIUM | ✅ FIXED | Storage quota implications documented |
| 3.1 | LOW | ✅ FIXED | Centralized version module created |
| 3.2 | LOW | ✅ FIXED | Standardized UTC timestamps |

### New Files Created

- `src/grape_recorder/version.py` - Centralized version and timestamp utilities
- `docs/STATE_FILES.md` - State file documentation and reset procedures
- `scripts/reset-state.sh` - Safe state reset script

---

## THE CORE PROBLEM

**Too often, the parts that WRITE important info to files (analytics services) and the parts that READ them (other analytics, web-ui) change the destination or expected source of information WITHOUT notifying the rest of the system.**

This leads to:

1. **Silent failures** - Readers find empty/missing data, return nulls, UI shows blanks
2. **Stale data** - State files persist incorrect values across restarts
3. **Hidden coupling** - No explicit contracts between producers and consumers
4. **Debugging hell** - Tracing where data comes from requires reading multiple files

---

## EXAMPLES FROM DEC 8 SESSION

### Example 1: Kalman State Persistence Poisoning

**Bug**: D_clock showed linear drift of ~6.5 ms/minute despite GPSDO discipline.

**Root Cause Chain**:

1. `phase2_analytics_service.py` was synthesizing RTP timestamps from Unix time instead of reading `start_rtp_timestamp` from metadata JSON
2. This produced incorrect D_clock values that were fed to the Kalman filter
3. The Kalman filter in `clock_convergence.py` persisted state to `convergence_state.json`
4. When the RTP bug was fixed, the Kalman filter CONTINUED using the corrupted state from the JSON file
5. New correct measurements were rejected as "5-sigma outliers" because the filter's prediction was 900+ ms off

**Hidden Data Contract**:

```
PRODUCER: phase2_analytics_service.py writes convergence_state.json
CONSUMER: phase2_analytics_service.py reads convergence_state.json on restart
CONTRACT: State must be valid when loaded - but NO VALIDATION exists
```

**Fix Required**: State files need versioning and sanity checks before loading.

### Example 2: Channel Discovery Across Data Directories

**Bug**: Channels appeared in Phase 2 output but `discoverChannels()` couldn't find them.

**Root Cause**: The discovery function only checked `raw_archive/` but channels may only exist in `phase2/` or `products/`.

**Hidden Data Contract**:

```
PRODUCER: Phase 1 creates raw_archive/{CHANNEL}/
PRODUCER: Phase 2 creates phase2/{CHANNEL}/
CONSUMER: grape-paths.js discoverChannels() assumes raw_archive is canonical
CONTRACT: UNDEFINED - no single source of truth for "what channels exist"
```

### Example 3: RTP Timestamp Metadata

**Bug**: Timing calculations drifted because RTP timestamps weren't being used.

**Root Cause**: `_read_binary_minute()` synthesized timestamps instead of reading them from the JSON metadata that sits alongside the binary data.

**Hidden Data Contract**:

```
PRODUCER: Binary writer creates {minute}.bin + {minute}.json
CONSUMER: _read_binary_minute() should read BOTH files
CONTRACT: IMPLICIT - metadata fields like start_rtp_timestamp are optional
```

---

## DATA FLOW INVENTORY TO AUDIT

### Phase 1 → Phase 2 Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `raw_archive_writer.py` | `raw_archive/{CH}/` | `phase2_analytics_service.py` | DRF format |
| `binary_minute_writer.py` | `raw_buffer/{CH}/{minute}.bin` | `phase2_analytics_service.py` | Binary IQ + JSON metadata |
| `binary_minute_writer.py` | `raw_buffer/{CH}/{minute}.json` | `phase2_analytics_service.py` | **start_rtp_timestamp required** |

### Phase 2 Internal Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `phase2_analytics_service.py` | `phase2/{CH}/status/analytics-service-status.json` | `monitoring-server-v3.js` | Status JSON schema |
| `phase2_analytics_service.py` | `phase2/{CH}/status/convergence_state.json` | `phase2_analytics_service.py` | Kalman state |
| `phase2_analytics_service.py` | `phase2/{CH}/clock_offset/*.csv` | `multi_broadcast_fusion.py` | CSV with d_clock_ms column |
| `multi_broadcast_fusion.py` | `state/broadcast_calibration.json` | `monitoring-server-v3.js` | Calibration per station |
| `multi_broadcast_fusion.py` | `phase2/fusion/fused_d_clock.csv` | `monitoring-server-v3.js` | Fused output CSV |

### Phase 2 → Web UI Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `phase2_analytics_service.py` | `phase2/{CH}/status/*.json` | `transmission-time-helpers.js` | Status JSON fields |
| `multi_broadcast_fusion.py` | `state/broadcast_calibration.json` | `timing-dashboard-enhanced.html` | Calibration keys |
| Multiple | Various CSVs | `monitoring-server-v3.js` | Column names must match |

---

## CRITIQUE CHECKLIST

For each data producer/consumer pair, verify:

### 1. Schema Documentation

- [ ] Is the output format documented?
- [ ] Are required vs optional fields explicit?
- [ ] Is there a version number for the schema?

### 2. Validation on Read

- [ ] Does the consumer validate data before using it?
- [ ] Are there sanity checks for numeric ranges?
- [ ] Does it fail gracefully if data is missing/corrupt?

### 3. State File Hygiene

- [ ] Is persisted state versioned?
- [ ] Can stale state poison fresh calculations?
- [ ] Is there a mechanism to reset corrupted state?

### 4. Path Consistency

- [ ] Are paths constructed the same way in producer and consumer?
- [ ] Are there hardcoded paths that diverge from config?
- [ ] Does channel name sanitization match (`WWV 10 MHz` vs `WWV_10_MHz`)?

### 5. Timestamp Consistency

- [ ] Are timestamps Unix epoch, ISO string, or other?
- [ ] Are timezones explicit?
- [ ] Do column names match (`system_time` vs `timestamp` vs `utc_time`)?

---

## SPECIFIC FILES TO AUDIT

### High Priority (State Persistence)

1. `clock_convergence.py` - Kalman state save/load
2. `multi_broadcast_fusion.py` - Calibration state save/load
3. `phase2_analytics_service.py` - All file writes

### Medium Priority (CSV Contracts)

4. `clock_offset_series.py` - CSV column definitions
2. `carrier_power_writer.py` - CSV format
3. `monitoring-server-v3.js` - CSV parsing logic

### Lower Priority (Path Management)

7. `grape-paths.js` - Path construction functions
2. `phase2_analytics_service.py` - Output directory creation
3. Various HTML files - Hardcoded API endpoints

---

## PREVIOUS SESSION: Phase 2 Analytics Critique (Dec 7-8)

**Status:** ✅ Critique Complete - 16 Issues Addressed

The critical review identified **17 issues**, of which **16 were fixed** and **1 was invalidated**:

### Summary Table

| ID | Category | Severity | Status | Description |
|----|----------|----------|--------|-------------|
| 1.1 | Methodology | High | ✅ FIXED | Matched filter template length mismatch |
| 1.2 | Methodology | High | ✅ FIXED | Fixed ionospheric layer heights |
| 1.3 | Methodology | Medium | ✅ FIXED | Ionospheric delay model oversimplified |
| 2.1 | Discrimination | Medium | ✅ FIXED | Unvalidated voting weights |
| 2.2 | Discrimination | Medium | ✅ FIXED | Correlation between methods not modeled |
| 2.3 | Discrimination | Low | ✅ FIXED | Binary classification loses information |
| 3.1 | Statistics | High | ✅ FIXED | Wrong model for non-stationary data |
| 3.2 | Statistics | Medium | ⚠️ PARTIAL | Multi-broadcast fusion (per-station, not per-broadcast) |
| 4.1 | Bug | Medium | ✅ FIXED | Inconsistent station coordinates |
| 4.2 | Bug | Low | ❌ INVALID | Tone duration - NIST confirms 800ms correct |
| 4.3 | Bug | Low | ✅ FIXED | Hardcoded default calibration offsets |
| 5.1 | Enhancement | Medium | ✅ FIXED | No use of phase information |
| 5.2 | Enhancement | Medium | ✅ FIXED | No multipath detection |
| 5.3 | Enhancement | Low | ✅ FIXED | No cross-correlation WWV/WWVH |
| 5.4 | Enhancement | Low | ✅ FIXED | No CHU FSK time code exploitation |
| 6.1 | Validation | High | ✅ FIXED | No ground truth validation mechanism |
| 6.2 | Validation | Low | ✅ FIXED | Quality grades are arbitrary |

### New Modules Created

| Module | Lines | Purpose |
|--------|-------|---------|
| `ionospheric_model.py` | ~600 | Dynamic layer heights (IRI-2016/parametric) |
| `ground_truth_validator.py` | ~700 | GPS PPS, silent minute, mode validation |
| `probabilistic_discriminator.py` | ~750 | Logistic regression with L2 regularization |
| `advanced_signal_analysis.py` | ~900 | Phase correlation, multipath, CHU FSK |

### Key Changes

1. **Issue 6.2**: `quality_grade` (A/B/C/D) replaced with `uncertainty_ms` + `confidence`
2. **Backwards Compatibility**: Grade computed from uncertainty for web UI:
   - A: < 1 ms
   - B: < 3 ms
   - C: < 10 ms
   - D: ≥ 10 ms

**Full details**: `docs/PHASE2_CRITIQUE.md`

---

## 🚨 NEXT PRIORITY: PHASE 3 PIPELINE IMPLEMENTATION

**Purpose:** Implement the Phase 3 derived products pipeline - decimation, spectrograms, power graphs, and GRAPE/PSWS upload.

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-08 (Next Session)  
**Status:** 🔴 Not Started

---

### PHASE 3 OVERVIEW

Phase 3 produces derived products from Phase 2 analytical data:

```
Phase 2 Output (20 kHz IQ)
    ↓
┌─────────────────────────────────────────────────────────────┐
│                    PHASE 3 PIPELINE                         │
├─────────────────────────────────────────────────────────────┤
│  1. Decimation: 20 kHz → 10 Hz (carrier amplitude/phase)    │
│  2. Spectrogram: Daily carrier frequency/amplitude plot      │
│  3. Power Graphs: Carrier power with solar zenith overlay   │
│  4. Digital RF Product: 24-hour UTC day archive (HamSCI)    │
│  5. Upload: GRAPE/PSWS data repository submission           │
└─────────────────────────────────────────────────────────────┘
    ↓
products/{CHANNEL}/
├── decimated/YYYYMMDD.bin         # 10 Hz carrier data
├── spectrograms/YYYYMMDD.png      # Daily spectrogram
├── power/YYYYMMDD_power.png       # Power graph + solar zenith
└── drf/YYYYMMDD/                  # Digital RF for upload
```

---

### COMPONENT 1: DECIMATION (20 kHz → 10 Hz)

**Goal:** Extract carrier amplitude and phase at 10 Hz for efficient storage and analysis.

**Input:**

- `raw_archive/{CHANNEL}/` - 20 kHz complex IQ from Phase 1

**Output:**

- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data

**Implementation Notes:**

- Use scipy.signal.decimate or polyphase filter
- Extract carrier: mix to baseband, lowpass filter, decimate
- Output format: binary float32 (amplitude, phase) pairs
- File size: ~7 MB/day/channel (10 Hz × 86400 sec × 8 bytes)

**Existing Code to Review:**

| File | Status | Notes |
|------|--------|-------|
| `archive/legacy-grape-modules/decimator.py` | ⚠️ Legacy | May have useful algorithms |
| `scripts/analyze_decimation_quality.py` | ✅ Active | Quality analysis script |

---

### COMPONENT 2: CARRIER SPECTROGRAM

**Goal:** Generate daily spectrogram showing carrier frequency/amplitude variations.

**Input:**

- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data

**Output:**

- `products/{CHANNEL}/spectrograms/YYYYMMDD_spectrogram.png`

**Implementation Notes:**

- X-axis: UTC time (00:00 - 24:00)
- Y-axis: Frequency offset from carrier (±0.5 Hz typical)
- Color: Signal amplitude (dB)
- Show ionospheric Doppler shifts, propagation mode changes

**Existing Code to Review:**

| File | Status | Notes |
|------|--------|-------|
| `scripts/generate_spectrograms.py` | ⚠️ Archive | Check for reuse |
| `scripts/auto-generate-spectrograms.sh` | ✅ Active | Automation script |
| `docs/features/AUTOMATIC_SPECTROGRAM_GENERATION.md` | ✅ Reference | Design doc |

---

### COMPONENT 3: POWER GRAPHS WITH SOLAR ZENITH OVERLAY

**Goal:** Visualize carrier power alongside solar zenith angle for ionospheric correlation.

**Input:**

- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data
- Station coordinates from `timestd-config.toml`
- Transmitter coordinates (WWV: 40.68°N, 105.04°W)

**Output:**

- `products/{CHANNEL}/power/YYYYMMDD_power.png`

**Implementation Notes:**

- Primary Y-axis: Carrier power (dB)
- Secondary Y-axis: Solar zenith angle (degrees)
- Solar zenith calculation: Uses NOAA algorithms via `solar_zenith_calculator.py`
- Calculates at **path midpoint** (halfway between receiver and transmitter)
- Show sunrise/sunset transitions, D-layer absorption effects

**Solar Zenith Calculation (Already Implemented):**

```python
from grape_recorder.grape.solar_zenith_calculator import (
    calculate_solar_zenith_for_day,
    calculate_midpoint,
    solar_position
)

# Get solar elevation at path midpoints for all stations
solar_data = calculate_solar_zenith_for_day(date_str, receiver_grid)
# Returns: wwv_solar_elevation, wwvh_solar_elevation, chu_solar_elevation arrays
```

**Dependencies:** None required (uses pure Python NOAA algorithms)

---

### COMPONENT 4: DIGITAL RF PRODUCT (24-HOUR UTC DAY)

**Goal:** Package 24-hour UTC day of data in Digital RF format for HamSCI PSWS compatibility.

**Input:**

- `raw_archive/{CHANNEL}/` - 20 kHz complex IQ

**Output:**

- `products/{CHANNEL}/drf/YYYYMMDD/` - Digital RF directory structure

**Implementation Notes:**

- Digital RF format: HDF5 files with specific structure
- Time boundary: 00:00:00 UTC to 23:59:59 UTC
- Metadata: Station info, receiver config, GPSDO status
- Use existing `digital_rf` library

**Existing Code to Review:**

| File | Status | Notes |
|------|--------|-------|
| `src/grape_recorder/core/drf_writer.py` | ✅ Active | Real-time DRF writer |
| `archive/legacy-grape-modules/core_npz_writer.py` | ⚠️ Legacy | NPZ alternative |

---

### COMPONENT 5: GRAPE/PSWS UPLOAD

**Goal:** Upload completed daily products to the GRAPE data repository.

**Input:**

- `products/{CHANNEL}/drf/YYYYMMDD/` - Digital RF package
- `products/{CHANNEL}/spectrograms/YYYYMMDD.png`

**Destination:**

- GRAPE/PSWS data repository (TBD - endpoint configuration)

**Implementation Notes:**

- Upload after 00:00 UTC (previous day complete)
- Verify file integrity before upload (checksums)
- Track upload state to prevent duplicates
- Retry logic for network failures

**Existing Code to Review:**

| File | Status | Notes |
|------|--------|-------|
| `wsprdaemon/upload-client-utils.sh` | ✅ Active | Upload utilities |
| `systemd/grape-daily-upload.service` | ✅ Active | Systemd timer |
| `systemd/grape-daily-upload.timer` | ✅ Active | Daily trigger |

**Configuration to Check:**

| File | Setting | Purpose |
|------|---------|---------|
| `timestd-config.toml` | `[uploader]` section | Upload credentials/endpoint |
| `config/environment` | `GRAPE_UPLOAD_*` | Environment variables |

---

### DATA CONTRACTS FOR PHASE 3

| Producer | Output Path | Consumer | Contract |
|----------|-------------|----------|----------|
| Decimator | `products/{CH}/decimated/YYYYMMDD.bin` | Spectrogram Generator | Binary float32 pairs |
| Decimator | `products/{CH}/decimated/YYYYMMDD.json` | Uploader | Metadata (sample rate, start time) |
| Spectrogram Gen | `products/{CH}/spectrograms/YYYYMMDD.png` | Web UI, Uploader | PNG image |
| Power Graph Gen | `products/{CH}/power/YYYYMMDD_power.png` | Web UI | PNG with solar overlay |
| DRF Packager | `products/{CH}/drf/YYYYMMDD/` | Uploader | Digital RF structure |
| Uploader | `products/{CH}/upload_state.json` | Uploader | Prevents re-upload |

---

### CRITIQUE CHECKLIST FOR PHASE 3

#### 1. Decimation Quality

- [ ] Does the filter preserve carrier phase information?
- [ ] Is anti-aliasing sufficient (stopband attenuation > 60 dB)?
- [ ] Are edge effects handled at day boundaries?

#### 2. Spectrogram Accuracy

- [ ] Is time axis aligned to UTC?
- [ ] Does frequency axis match actual carrier offset range?
- [ ] Are colormaps appropriate for the data range?

#### 3. Solar Zenith Calculation

- [ ] Are coordinates correct for both receiver AND transmitter?
- [ ] Is the calculation for the MIDPOINT of the propagation path?
- [ ] Are time zones handled correctly (UTC throughout)?

#### 4. Digital RF Compliance

- [ ] Does output match HamSCI PSWS format specification?
- [ ] Are all required metadata fields present?
- [ ] Is the file structure compatible with existing tools?

#### 5. Upload Robustness

- [ ] Is there retry logic for transient failures?
- [ ] Are credentials stored securely (not in code)?
- [ ] Is upload state persisted across service restarts?

---

### FILES TO CREATE/MODIFY

**New Files:**

| File | Purpose |
|------|---------|
| `src/grape_recorder/grape/decimator.py` | 20 kHz → 10 Hz decimation |
| `src/grape_recorder/grape/spectrogram_generator.py` | Daily spectrogram creation |
| `src/grape_recorder/grape/power_graph_generator.py` | Power + solar zenith plots |
| `src/grape_recorder/grape/drf_packager.py` | 24-hour DRF packaging |
| `src/grape_recorder/grape/uploader.py` | GRAPE repository upload |
| `src/grape_recorder/grape/phase3_pipeline.py` | Pipeline orchestration |

**Files to Update:**

| File | Change |
|------|--------|
| `requirements.txt` | Add `astropy` for solar calculations |
| `systemd/grape-daily-upload.service` | Update for new pipeline |
| `timestd-config.toml` | Add Phase 3 configuration section |

---

## ORIGINAL CRITIQUE CONTEXT (Reference)

The sections below document the original problem being solved and the theoretical framework used for the critique. Preserved for future reference.

### 1. THE PROBLEM BEING SOLVED

**Objective**: Extract precise UTC(NIST) time from HF radio signals transmitted by WWV, WWVH, and CHU time signal stations, achieving sub-millisecond accuracy despite ionospheric propagation delays of 2-60 ms.

**The Fundamental Equation**:

```
T_arrival = T_emission + T_propagation + D_clock

Where:
  T_arrival = Detected tone time (from matched filter)
  T_emission = 0 (by definition - tones at second boundary)
  T_propagation = Ionospheric path delay (ESTIMATED)
  D_clock = System clock offset from UTC(NIST) (DESIRED OUTPUT)

Therefore:
  D_clock = T_arrival - T_propagation
```

### 2. THREE-STEP TEMPORAL ANALYSIS

| Step | Method | Window | Output |
|------|--------|--------|--------|
| 1 | Tone Detection | ±500 ms | Time snap anchor |
| 2 | Channel Characterization | ±50 ms | Station ID, mode hints |
| 3 | Transmission Time Solution | — | D_clock value |

### 3. THEORETICAL REFERENCES

- **NIST Special Publication 432** - WWV/WWVH specifications
- **ITU-R P.531** - Ionospheric propagation prediction
- **ITU-R P.533** - HF propagation method
- **IRI-2016** - International Reference Ionosphere model

### 4. KEY FILES FOR PHASE 2 ANALYTICS

| File | Purpose |
|------|---------|
| `tone_detector.py` | Matched filter, correlation |
| `transmission_time_solver.py` | Mode scoring, D_clock |
| `phase2_temporal_engine.py` | Pipeline orchestration |
| `wwvh_discrimination.py` | Station discrimination |
| `clock_convergence.py` | Kalman filter tracking |
| `multi_broadcast_fusion.py` | Multi-broadcast fusion |

### 5. SUCCESS CRITERIA

| Metric | Target | Status |
|--------|--------|--------|
| D_clock accuracy | ±1 ms | ✅ Framework in place |
| Station discrimination | 95% | ✅ Probabilistic model added |
| Propagation mode ID | 80% | ✅ Dynamic iono model added |
| Time to lock | < 30 min | ✅ Kalman filter added |
| Validation | Ground truth | ✅ GPS PPS + silent minutes |

---

*This document has been updated to reflect the completed critique. The next priority is ensuring web UI correctly interfaces with the updated analytics modules.*
