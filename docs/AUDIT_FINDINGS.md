# Codebase Quality, Robustness & Contract-Compliance Audit

**Date**: 2026-03-15  
**Scope**: Groups 1–10 (Full codebase audit)  
**Method**: Per-module review against CODING_CONTRACT rules, companion contracts, edge-case analysis

---

## Group 1: Hot Path — metrology_engine.py, tick_edge_detector.py, buffer_timing.py

### 1.1 Contract Violations

#### CRITICAL: `edge_results` and `rtp_all_attempts` undefined in Fusion mode (NameError crash)
- **File**: `metrology_engine.py:1620, 1857`
- **Rule**: N/A (correctness bug)
- **Detail**: `edge_results` is defined only inside the `if self.is_rtp_authority:` block (line 1353). After the RTP/Fusion if/else, code at line 1620 references `edge_results` unconditionally:
  ```python
  signal_present = (
      bool(edge_results)        # ← NameError in Fusion mode
      or self._check_signal_presence(iq_samples)
  )
  ```
  Similarly, `rtp_all_attempts` is defined only in the RTP block (line 1202) but referenced at line 1857:
  ```python
  self._last_rtp_attempts = rtp_all_attempts if self.is_rtp_authority else None
  ```
  The `rtp_all_attempts` reference is guarded by `if self.is_rtp_authority` so it won't crash, but `edge_results` at line 1620 **will crash with `NameError` in Fusion mode**.  
  **Impact**: Any channel running in Fusion mode (NTP-only, no GPS+PPS) will crash on every `process_minute()` call after the RTP/Fusion branch, preventing tick phase extraction and all downstream processing.  
  **Severity**: **CRITICAL**  
  **Fix**: Initialize `edge_results = {}` before the if/else branch (e.g., at ~line 1180).

#### HIGH: `except Exception` at DEBUG level in hot path (Rule 5)
- **File**: `metrology_engine.py:1369-1371, 1705-1706, 1707-1708, 577-578, 2081-2082`
- **Rule**: CODING_CONTRACT Rule 5 — "broad catches MUST log at ERROR or WARNING"
- **Detail**: Multiple `except Exception as e:` blocks in the core `process_minute()` path log at `logger.debug()`, which is invisible in production (INFO level). These include:
  - Edge detection failures (line 1370): `logger.debug(f"Edge detection failed...")`
  - PLL comparison failures (line 1706): `logger.debug(f"PLL comparison failed...")`
  - Tick extraction failures (line 1708): `logger.debug(f"tick extraction failed...")`
  - Signal presence check (line 578): `logger.debug(f"Signal presence check failed...")`
  - FSK result JSON write (line 2082): `logger.debug(f"Failed to write FSK result JSON...")`
- **Impact**: Persistent failures are silently swallowed. This was the root cause of prior HDF5 stall incidents (fusion starved because read errors logged at DEBUG).
- **Severity**: **HIGH**
- **Fix**: Raise to `logger.warning()` for all `except Exception` blocks in the processing pipeline. Only truly expected/harmless failures (e.g., optional enrichment) should stay at DEBUG.

#### MEDIUM: Scipy imports inside hot-path functions (Rule 6 / Rule 9)
- **File**: `metrology_engine.py:529, 700-701`
- **Rule**: CODING_CONTRACT Rule 6 (hot-path efficiency) / Rule 9 (no blocking in compute threads)
- **Detail**: `from scipy.signal import butter, sosfiltfilt` and `from scipy import signal` are imported inside `_check_signal_presence()` and `_measure_tone_at_known_time()` respectively. These are called 15+ times per minute per channel. While Python caches module imports after the first call, the import machinery still acquires the GIL and performs a dict lookup on every call.
- **Severity**: **MEDIUM**
- **Fix**: Move scipy imports to module level.

#### MEDIUM: `from collections import defaultdict` inside hot path (Rule 6)
- **File**: `metrology_engine.py:1465-1466`
- **Detail**: `from collections import defaultdict` is imported inside the per-minute processing function.
- **Severity**: **LOW** (stdlib, negligible cost)
- **Fix**: Move to module level for style consistency.

#### LOW: `np.zeros()` allocation per tick in tick_edge_detector (Rule 6)
- **File**: `tick_edge_detector.py:403` — `residual = corr_env.copy()`
- **Rule**: CODING_CONTRACT Rule 6 — "pre-allocate numpy buffers, mutate in-place"
- **Detail**: `_clean_deconvolve()` copies the correlation envelope on every call. This runs only for CLEAN deconvolution on dedicated channels with high-SNR ticks, so practical impact is minimal.
- **Severity**: **LOW**

### 1.2 Edge-Case and Error-Handling Defects

#### HIGH: No bounds check on `noise_floor` variable in _measure_tone_at_known_time
- **File**: `metrology_engine.py:1060`
- **Detail**: At line 1060, `_find_all_correlation_peaks()` is called with `noise_floor=noise_floor`, but `noise_floor` is only defined inside the `if len(noise_region) > 10:` branch (line 836) or the else branch (line 840). If `noise_region` has exactly 6-10 elements, it takes the median path correctly. However, the variable `noise_median` (line 832) is used at line 857 in the rejection log message even if the `else` branch at 840 was taken (where `noise_median` is not defined).
- **Impact**: `NameError` on `noise_median` when correlation is weak AND `noise_region` has ≤10 elements.
- **Severity**: **HIGH**
- **Fix**: Use `noise_floor` (always defined) instead of `noise_median` in the log message at line 857.

#### MEDIUM: `_predict_geometric_delay` called twice per detection
- **File**: `metrology_engine.py:1173, 1732`
- **Detail**: The same function is called at line 1173 (to build `expected_delays_by_station`) and again at line 1732 (per detection, inside the Step 3 loop). The result is deterministic for the same `(station, system_time)`. This duplicates work and makes one network/model call per detection.
- **Severity**: **MEDIUM** (correctness OK, but wasteful)
- **Fix**: Cache the results from the first call and reuse in Step 3.

### 1.3 Resource Management Issues

- **buffer_timing.py**: Clean. No file handles, no allocations. Pure computation from metadata. ✅
- **tick_edge_detector.py**: Templates and PSF are pre-computed in `__init__`. Per-call allocations are correlation arrays (unavoidable for scipy.signal.correlate). ✅
- **metrology_engine.py**: `_envelope_buffer` is pre-allocated and reused (line 94, 1136). Good. ✅

### 1.4 Dead Code

#### MEDIUM: `_check_signal_presence()` is effectively dead for its original purpose
- **File**: `metrology_engine.py:517-579`
- **Detail**: The comment at line 1614 states "The old `_check_signal_presence()` band-energy test always fails for WWV/WWVH 5ms ticks." It is now only used as a fallback after `edge_results` (which, per the CRITICAL bug above, crashes in Fusion mode). In RTP mode, `edge_results` is always populated first, so `_check_signal_presence` is the fallback-of-a-fallback. Consider removing or marking deprecated.

#### LOW: `_load_calibration` / `_save_calibration` (lines 2128-2149)
- **Detail**: These write/read `timing_calibration.json` for BPM calibration. No callers were found in the codebase outside this file. May be unused after the calibration architecture moved to `multi_broadcast_fusion.py`.

### 1.5 Test Coverage Gaps

| Module | Test File | Coverage |
|--------|-----------|----------|
| `metrology_engine.py` (2149 lines) | `test_metrology_engine.py` (94 lines) | **Very thin** — likely only basic smoke tests |
| `tick_edge_detector.py` (816 lines) | **No dedicated test file** | **None** |
| `buffer_timing.py` (235 lines) | **No dedicated test file** | **None** |

- **CRITICAL GAP**: `tick_edge_detector.py` has no tests. The CLEAN deconvolution, ensemble combination, weighted median, and Doppler extraction are all untested.
- **CRITICAL GAP**: `buffer_timing.py` has no tests. The RTP-to-UTC mapping and 32-bit wraparound logic (`_rtp_delta_signed`) are fundamental to all timing and are untested.

---

## Group 2: Data Integrity — io/hdf5_writer.py, io/hdf5_reader.py

### 2.1 Contract Violations

#### HIGH: `chrony_stats.py` missing `locking=False` (DATA_CONTRACT)
- **File**: `chrony_stats.py:423`
- **Detail**: `h5py.File(h5_path, 'a')` without `locking=False`. All other h5py.File calls in the codebase were fixed in a prior session, but this one was missed.
- **Impact**: Can cause `OSError: [Errno 11]` lock contention with concurrent readers.
- **Severity**: **HIGH**
- **Fix**: Add `locking=False` to the h5py.File call.

### 2.2 Edge-Case and Error-Handling Defects

#### HIGH: Full table scan in `read_time_range()` — O(N) for every query
- **File**: `hdf5_reader.py:229` — `data[field_name] = f[field_name][:]`
- **Detail**: Every `read_time_range()` call loads the **entire dataset** into memory with `f[field_name][:]`. For large daily files (~560MB, 67K+ rows), this causes multi-second stalls and memory pressure. The time filtering at line 278-315 happens AFTER the full load.
- **Impact**: Known to cause intermittent timeouts and VTEC data starvation in production.
- **Severity**: **HIGH** (known production issue, documented in memory)
- **Fix**: Read `timestamp_utc` first, binary-search for the matching slice, then read only matching rows for other fields.

#### MEDIUM: Corrupt chunk recovery assumes all fields have same chunk size
- **File**: `hdf5_reader.py:238`
- **Detail**: `chunk_size = (ds.chunks or (1024,))[0]` — if different datasets have different chunk sizes (e.g., due to schema evolution), `truncate_to` may be computed from one field's chunk boundary but applied to another field with a different boundary.
- **Severity**: **MEDIUM** (unlikely in practice since all datasets are created with `chunks=True` which auto-selects)

### 2.3 Resource Management

- **hdf5_writer.py**: Crash-safe open/write/close pattern. All `h5py.File()` calls use context managers with `locking=False`. ✅
- **hdf5_reader.py**: All `h5py.File()` calls use context managers with `locking=False`. ✅
- **No leaked file descriptors** found in either module. ✅

### 2.4 Test Coverage

| Module | Test File | Coverage |
|--------|-----------|----------|
| `hdf5_writer.py` | `test_hdf5_standalone.py`, `unit/test_hdf5_io.py` | Basic write/read cycle covered |
| `hdf5_reader.py` | `test_fusion_hdf5_reader.py` | Fusion-specific reads covered |

- **GAP**: No test for corrupt chunk recovery path (`read_time_range` OSError handling).
- **GAP**: No test for `write_measurements_batch()` (the fix for HDF5 heap corruption).

---

## Group 3: Long-Running Services — multi_broadcast_fusion.py, chrony_shm.py, fusion_timing_state.py

### 3.1 Contract Violations

#### MEDIUM: chrony_shm.py — mmap not released in disconnect() error path
- **File**: `chrony_shm.py:359-371`
- **Rule**: CODING_CONTRACT Rule 8 — "strict context managers for I/O, explicit mmap release"
- **Detail**: `disconnect()` calls `self.shm_map.close()` but if it throws, the mmap remains open. Also, `_connect_file()` (line 190-192) opens an fd, creates an mmap, and closes the fd — but if `mmap.mmap()` throws, the fd leaks (no try/finally).
- **Severity**: **MEDIUM**
- **Fix**: Use try/finally for fd cleanup in `_connect_file()`.

#### MEDIUM: chrony_shm.py mode=0 vs comment says mode=1
- **File**: `chrony_shm.py:278, 347`
- **Detail**: The struct pack at line 278 writes `mode=0` ("no count locking"), but the log message at line 347 says `mode=1`. This is a documentation inconsistency. Mode 0 means chrony ignores the count field, which is actually correct for this use case (single writer).
- **Severity**: **LOW** (log message wrong, behavior correct)

### 3.2 Edge-Case Defects

#### MEDIUM: fusion_timing_state.py — measurements list trimming loses station tracking
- **File**: `fusion_timing_state.py:189-191`
- **Detail**: When measurements exceed `_max_measurements` (500), the oldest 20% are trimmed. But `_stations_seen` and `_minutes_with_detections` are never pruned. After a long run, `_stations_seen` accumulates stations that may no longer be contributing, and `_minutes_with_detections` grows without bound.
- **Impact**: `_minutes_with_detections` is a `set` of minute boundaries (ints). At 1 per minute, this is ~525K entries/year (~4MB). Not a crash risk, but violates the spirit of bounded memory.
- **Severity**: **MEDIUM**
- **Fix**: Prune `_minutes_with_detections` to keep only recent entries when trimming measurements.

#### LOW: fusion_timing_state.py `__post_init__` redundant checks
- **File**: `fusion_timing_state.py:121-124`
- **Detail**: `__post_init__` checks `hasattr(self, '_stations_seen')` — this is always True because `_stations_seen` has a `field(default_factory=set)`. The check is redundant.
- **Severity**: **LOW**

### 3.3 multi_broadcast_fusion.py (initial scan)

- **HDF5_USE_FILE_LOCKING** set before h5py import ✅ (fixed in prior session, line 196)
- Module is 5488 lines — full review deferred to continued session
- Dual Kalman state (L1/L2) architecture confirmed present

---

## Cross-Cutting Findings (from diagnostic scans)

### Deprecated Code Still Imported

| Item | Location | Status |
|------|----------|--------|
| `PhysicsPropagationModel` | `core/__init__.py:63, 262` | Marked `# deprecated` but still exported in `__all__` |
| `tof_kalman_ms` field | `models/broadcast_measurement.py:314`, `models/measurement.py:86`, `physics_fusion_service.py:364` | Deprecated schema field still defined and read |

### `except Exception` at DEBUG Level (Codebase-Wide)

Found 13 instances across 6 files where `except Exception` logs at DEBUG level:
- `chu_fsk_decoder.py:755`
- `iono_data_service.py:492, 681, 720, 764, 818`
- `stream_recorder_v2.py:146, 633, 790`
- `tone_detector.py:1279, 2145, 2596`
- `timing_calibrator.py:1911, 1985`

All of these should be reviewed and most elevated to WARNING per Rule 5.

---

## Recommendations (Ranked)

### CRITICAL
1. **Fix `edge_results` NameError in Fusion mode** (`metrology_engine.py:1620`). Initialize `edge_results = {}` before the RTP/Fusion branch. This is a crash bug.
2. **Fix `noise_median` NameError** (`metrology_engine.py:857`). Use `noise_floor` instead.

### HIGH  
3. **Add `locking=False`** to `chrony_stats.py:423`.
4. **Elevate `except Exception` logging** from DEBUG to WARNING in all hot-path catch blocks (13 instances across 6 files).
5. **Add tests for `tick_edge_detector.py`** and **`buffer_timing.py`** — these are untested critical-path modules.
6. **Optimize `read_time_range()`** to avoid full table scan of large HDF5 files.

### MEDIUM
7. **Move scipy imports** to module level in `metrology_engine.py`.
8. **Cache `_predict_geometric_delay` results** to avoid duplicate calls per minute.
9. **Remove deprecated `PhysicsPropagationModel`** from `core/__init__.py` exports.
10. **Bound `_minutes_with_detections`** set in `fusion_timing_state.py`.
11. **Fix fd leak** in `chrony_shm.py:_connect_file()` — wrap in try/finally.

### LOW
12. **Fix log message** `mode=1` → `mode=0` in `chrony_shm.py:347`.
13. **Review `_load_calibration`/`_save_calibration`** for dead code removal.
14. **Remove deprecated `tof_kalman_ms`** field from models.

---

## Group 4: Core Recorder — stream_recorder_v2.py

### 4.1 Contract Violations

#### HIGH: Silent `except Exception: pass` swallows capability check errors
- **File**: `stream_recorder_v2.py:440-441`
- **Rule**: CODING_CONTRACT Rule 5 — "broad catches MUST log at ERROR or WARNING"
- **Detail**: `except Exception as e: pass` — no logging at all. If `get_capabilities()` throws, the error is silently swallowed. This could mask backend capability detection failures that affect encoding selection.
- **Severity**: **HIGH**
- **Fix**: Add `logger.debug(f"get_capabilities failed: {e}")` at minimum.

#### HIGH: Silent `except Exception: pass` on stream stop
- **File**: `stream_recorder_v2.py:470-471`
- **Detail**: `except Exception: pass` — bare except with no variable capture or logging when stopping a previous stream before creating a new one. If the old stream is stuck, this hides it.
- **Severity**: **MEDIUM**

#### MEDIUM: `except Exception` at DEBUG in stream ensure, discovery, and tap callbacks
- **File**: `stream_recorder_v2.py:145-146, 632-633, 789-790`
- **Rule**: CODING_CONTRACT Rule 5
- **Detail**: `_ensure_stream()` failure, channel discovery failure, and tap callback errors all log at DEBUG. Stream ensure failures are particularly concerning — if the stream can't be created, the channel silently does nothing.
- **Severity**: **MEDIUM** (ensure_stream should be WARNING; discovery can stay DEBUG; tap errors should be WARNING)

#### LOW: Commented-out monitor thread
- **File**: `stream_recorder_v2.py:97-99`
- **Detail**: `_monitor_thread` start is commented out with "disabled by manual override". The `RobustManagedStream` class has a `_monitor_loop` method that is never called. Either remove the dead code or re-enable with a flag.
- **Severity**: **LOW**

### 4.2 Resource Management

- **Lock discipline**: Uses `threading.RLock` for state access. `_handle_samples` acquires lock only for state update, not for archive write — correct (non-blocking DSP). ✅
- **Tap callbacks**: Called outside lock, with individual try/except per tap. ✅
- **Stream lifecycle**: `stop()` joins health monitor and timing poll threads with 2s timeout. ✅
- **Archive writer**: Closed in `stop()`. ✅

### 4.3 Duplicate Code

#### LOW: `_set_filter_edges()` defined twice
- **File**: `stream_recorder_v2.py:150-177` (in `RobustManagedStream`) and `stream_recorder_v2.py:508-535` (in `StreamRecorderV2`)
- **Detail**: Nearly identical implementations. The `RobustManagedStream` version uses `self.control` and `self.config.get()`, while `StreamRecorderV2` uses `self._control` and `self.config.low_edge`. Should be deduplicated.

---

## Group 5: Calibration Chain — l2_calibration_service.py, broadcast_kalman_filter.py

### 5.1 Contract Violations

#### MEDIUM: `import time` inside function body in broadcast_kalman_filter.py
- **File**: `broadcast_kalman_filter.py:569`
- **Rule**: CODING_CONTRACT Rule 6 — imports at module level
- **Detail**: `import time` inside `is_converged()`. The module already uses `time` indirectly via `datetime`, but doesn't import `time` at module level.
- **Severity**: **LOW**
- **Fix**: Move `import time` to module-level imports.

#### MEDIUM: `except Exception` at DEBUG in l2_calibration_service.py stop()
- **File**: `l2_calibration_service.py:174-176`
- **Detail**: `logger.debug(f"Ignored exception: {e}")` followed by bare `pass` when stopping IonoDataService. If stop fails, the service may leak resources.
- **Severity**: **LOW**

### 5.2 Architectural Observations

- **broadcast_kalman_filter.py**: Clean 2-state Kalman filter (ToF, Doppler) with adaptive process noise and mode transition detection. State persistence via JSON. No HDF5 access. ✅
- **l2_calibration_service.py**: Well-structured L1→L2 pipeline with ISO GUM uncertainty budget. Signal handlers for graceful shutdown. Systemd watchdog integration. ✅
- **l2_calibration_service.py**: Correctly uses `DataProductReader` (which already has `locking=False`). ✅
- **Propagation mode identification**: Tries all candidate modes and picks highest confidence — avoids circular assumption of lowest-hop mode. Good. ✅

### 5.3 Potential Issue

#### MEDIUM: `_create_missing_l2` hardcodes `StationID.WWV` as fallback
- **File**: `l2_calibration_service.py:456`
- **Detail**: `station=StationID[station_id] if station_id in StationID.__members__ else StationID.WWV` — if station_id is unknown, it silently maps to WWV. This corrupts the L2 record's station field.
- **Severity**: **MEDIUM**
- **Fix**: Return `None` instead of writing a record with wrong station.

---

## Group 6: Physics Pipeline — physics_fusion_service.py, carrier_tec.py, propagation_model.py

### 6.1 Contract Violations

#### HIGH: `except Exception` at DEBUG in physics_fusion_service.py (3 instances)
- **File**: `physics_fusion_service.py:733-734, 796-797, 841-842`
- **Rule**: CODING_CONTRACT Rule 5
- **Detail**: tick_phase reader creation, tick_phase reads, and GNSS VTEC reads all log at DEBUG. These are the primary data feeds for the physics pipeline. If they fail silently (as happened with HDF5 locking), the pipeline starves.
- **Severity**: **HIGH** (same pattern as the fusion HDF5 stall root cause)
- **Fix**: Elevate to WARNING.

### 6.2 HDF5 Access

- All h5py.File calls in the core library (`hdf5_reader.py`, `hdf5_writer.py`) already have `locking=False`. ✅
- `physics_fusion_service.py` uses `DataProductReader` which inherits `locking=False`. ✅
- No direct `h5py.File` calls found in `carrier_tec.py` or `propagation_model.py`. ✅

---

## Group 7: GRAPE Module — decimation_pipeline.py, packager.py, uploader.py

### 7.1 Contract Violations

#### MEDIUM: `except Exception` at DEBUG in raw_reader.py
- **File**: `grape/raw_reader.py:99-100`
- **Detail**: `logger.debug(f"Caught exception: {e}")` — generic catch during file reading.
- **Severity**: **LOW** (GRAPE is a data export pipeline, not timing-critical)

#### LOW: Bare `except Exception:` with `continue` in raw_reader.py
- **File**: `grape/raw_reader.py:237-238`
- **Detail**: `except Exception: continue` — no logging, no variable capture.
- **Severity**: **LOW**

### 7.2 Observations

- **decimation_pipeline.py** (193 lines): Clean, uses `exc_info=True` on errors. ✅
- **packager.py** (446 lines): Packages decimated data for PSWS upload. No HDF5 direct access. ✅
- **uploader.py** (1033 lines): SFTP upload to PSWS. No HDF5 access. ✅

---

## Group 8: Web API — services/*.py, routers/*.py

### 8.1 Contract Violations

#### HIGH: Missing `locking=False` in tid_service.py
- **File**: `web-api/services/tid_service.py:215`
- **Detail**: `h5py.File(filepath, 'r')` without `locking=False`. This is the same bug class that caused the fusion HDF5 stall.
- **Severity**: **HIGH**
- **Fix**: Add `locking=False`.

#### HIGH: `except Exception` at DEBUG across many web-api services (15+ instances)
- **Files**: `chu_fsk_service.py:115,385`, `chrony_service.py:159`, `health_service.py:257`, `scintillation_service.py:150,299`, `propagation_service.py:311`, `test_signal_service.py:180,320`, `event_service.py:292`
- **Rule**: CODING_CONTRACT Rule 5
- **Detail**: All web-api service HDF5 read errors log at DEBUG. If underlying data files have issues, the web UI shows empty dashboards with no visible error.
- **Severity**: **MEDIUM** (web-api is read-only; impact is UI data gaps, not timing corruption)
- **Fix**: Elevate to WARNING for HDF5 read failures; keep DEBUG for optional enrichment.

#### MEDIUM: Silent `pass` in propagation_service.py (4 instances)
- **File**: `web-api/services/propagation_service.py:403, 433, 450, 507`
- **Detail**: `except Exception as e: pass` — completely silent failure in mode timeline construction.
- **Severity**: **MEDIUM**

### 8.2 Observations

- All other web-api h5py.File calls have `locking=False`. ✅
- Web API is FastAPI-based, read-only, no writes to HDF5. ✅
- Total: ~10,553 lines across services + routers.

---

## Group 9: Zombie Hunt — Dead Modules

### Confirmed Dead Modules (not imported by any file, not invoked by systemd)

| Module | Path | Lines | Notes |
|--------|------|-------|-------|
| `audio_tone_monitor` | `src/hf_timestd/core/audio_tone_monitor.py` | — | No references found anywhere |
| `ground_truth_validator` | `src/hf_timestd/core/ground_truth_validator.py` | — | No references found anywhere |
| `ionospheric_reanalysis` | `src/hf_timestd/core/ionospheric_reanalysis.py` | — | No references found anywhere |

### False Positives (invoked via systemd or __main__)

| Module | Invocation |
|--------|-----------|
| `l2_calibration_service` | `systemd/timestd-l2-calibration.service` |

### Deprecated but Still Exported

| Item | Location | Status |
|------|----------|--------|
| `PhysicsPropagationModel` | `core/__init__.py` | Marked deprecated but in `__all__` |
| `tof_kalman_ms` | `models/broadcast_measurement.py`, `models/measurement.py` | Deprecated schema field still defined |

**Recommendation**: Move confirmed dead modules to `archive/deprecated-core/`.

---

## Group 10: Test Coverage Audit

### Coverage Summary

| Metric | Count |
|--------|-------|
| Source modules (`.py` under `src/hf_timestd/`) | 125 |
| Test files (`.py` under `tests/`) | 26 |
| **Untested modules** | **114** |
| Test coverage ratio | **~9%** by module count |

### Critical Untested Modules (high-risk, actively used in production)

| Module | Lines | Risk | Notes |
|--------|-------|------|-------|
| `tick_edge_detector.py` | 816 | **CRITICAL** | Core timing extraction, CLEAN deconvolution, ensemble combination |
| `buffer_timing.py` | 235 | **CRITICAL** | RTP-to-UTC mapping, 32-bit wraparound — foundation of all timing |
| `multi_broadcast_fusion.py` | 5488 | **CRITICAL** | L3 fusion engine, Kalman state, calibration — the system's primary output |
| `stream_recorder_v2.py` | 894 | **HIGH** | Data acquisition pipeline |
| `physics_fusion_service.py` | 1338 | **HIGH** | TEC estimation, VTEC mapping |
| `chrony_shm.py` | 438 | **HIGH** | System clock discipline interface |
| `fusion_timing_state.py` | 340 | **HIGH** | Lock tier management |
| `tone_detector.py` | ~2600 | **HIGH** | Fusion-mode signal search |
| `chu_fsk_decoder.py` | ~1500 | **HIGH** | CHU time code reference |
| `propagation_model.py` | 1112 | **MEDIUM** | Ionospheric delay modeling |

### Existing Tests (26 files, ~2000 lines total)

Most tests are thin smoke tests or focused on specific subsystems:
- `test_metrology_engine.py` (94 lines) — basic smoke test only
- `test_hdf5_io.py` — write/read cycle
- `test_schemas.py` — schema validation
- `test_uncertainty.py` — ISO GUM budget
- `test_broadcast_kalman_filter.py` — Kalman filter basics
- `test_bpm_discriminator.py` — BPM station identification

**No integration tests** exist for the full L1→L2→L3 pipeline.

---

## Updated Recommendations (Ranked, Full Codebase)

### CRITICAL (production crash / data corruption risk)
1. **Fix `edge_results` NameError in Fusion mode** (`metrology_engine.py:1620`). Initialize `edge_results = {}` before the RTP/Fusion branch.
2. **Fix `noise_median` NameError** (`metrology_engine.py:857`). Use `noise_floor` instead.

### HIGH (silent data loss / known production failure pattern)
3. **Add `locking=False`** to `chrony_stats.py:423` and `tid_service.py:215`.
4. **Elevate `except Exception` logging** from DEBUG to WARNING in all data-pipeline catch blocks (~28 instances across 12 files).
5. **Add tests for `tick_edge_detector.py`** and **`buffer_timing.py`** — untested critical-path modules.
6. **Optimize `read_time_range()`** to avoid full table scan of large HDF5 files.
7. **Fix silent `pass` swallowing** in `stream_recorder_v2.py:440-441`.

### MEDIUM (correctness, maintainability)
8. **Move scipy imports** to module level in `metrology_engine.py`.
9. **Cache `_predict_geometric_delay` results** to avoid duplicate calls per minute.
10. **Remove deprecated `PhysicsPropagationModel`** from `core/__init__.py` exports.
11. **Bound `_minutes_with_detections`** set in `fusion_timing_state.py`.
12. **Fix fd leak** in `chrony_shm.py:_connect_file()` — wrap in try/finally.
13. **Fix `StationID.WWV` fallback** in `l2_calibration_service.py:456` — return None instead.
14. **Move dead modules** (`audio_tone_monitor`, `ground_truth_validator`, `ionospheric_reanalysis`) to `archive/`.
15. **Deduplicate `_set_filter_edges()`** in `stream_recorder_v2.py`.

### LOW (documentation, style)
16. **Fix log message** `mode=1` → `mode=0` in `chrony_shm.py:347`.
17. **Review `_load_calibration`/`_save_calibration`** for dead code removal.
18. **Remove deprecated `tof_kalman_ms`** field from models.
19. **Move `import time`** to module level in `broadcast_kalman_filter.py:569`.
20. **Remove `__post_init__` redundant checks** in `fusion_timing_state.py:121-124`.
