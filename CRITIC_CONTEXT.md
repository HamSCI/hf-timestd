# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## NEXT SESSION: CODEBASE QUALITY, ROBUSTNESS & CONTRACT-COMPLIANCE AUDIT

**Goal:** Systematically review the hf-timestd codebase for adherence to the CODING_CONTRACT (`.windsurf/contracts/CODING_CONTRACT.md`), robustness under edge cases, error handling quality, and general production readiness. Identify code that violates contract rules, swallows errors silently, leaks resources, contains dead/zombie code, or lacks test coverage for critical paths.

**Method:** The user will direct the review module-by-module or subsystem-by-subsystem. For each module reviewed, produce:
1. A list of **contract violations** (cite the specific CODING_CONTRACT rule number)
2. **Edge-case and error-handling defects** (silent exception swallowing, missing bounds checks, unhandled None/NaN, race conditions)
3. **Resource management issues** (unclosed h5py.File, leaked mmap/FDs, unbounded memory growth, missing context managers)
4. **Dead code and zombie imports** (unused functions, deprecated code paths still imported, stale fallback branches)
5. **Test coverage gaps** (critical paths with no corresponding test, tests that test the wrong thing, tests that can't fail)
6. **Concrete recommendations** ranked by severity (CRITICAL / HIGH / MEDIUM / LOW)

Do NOT suggest cosmetic changes (reformatting, renaming variables, reordering imports) unless they mask a real bug. Focus on correctness, resilience, and maintainability.

---

## 1. The CODING_CONTRACT (Summary)

The full contract is in `.windsurf/contracts/CODING_CONTRACT.md`. The 10 rules are:

| # | Rule | Key Violations to Look For |
|---|------|---------------------------|
| 1 | No `exec`, `eval`, `globals()` manipulation | Dynamic code execution, monkey-patching |
| 2 | Max 3 levels of nesting (cyclomatic complexity) | Deeply nested if/for/try blocks |
| 3 | No `*args`/`**kwargs` in core APIs | Implicit interfaces that bypass type checking |
| 4 | Strict type hinting (`NDArray` exceptions only in DSP) | Missing type hints on public APIs, `Any` proliferation |
| 5 | No bare `except:`; broad catches must log at ERROR/WARNING | `except Exception: pass`, `except: ...` with DEBUG logging |
| 6 | Pre-allocate numpy buffers in hot path (zero-allocation DSP) | `np.zeros()` or list appends inside per-tick/per-second loops |
| 7 | Immutable dataclass/NamedTuple for metadata and measurements | Mutable dicts passed between modules for measurement records |
| 8 | Context managers for all I/O; explicit release of mmap/C handles | `h5py.File()` without `with`, `np.memmap` without explicit close |
| 9 | Compute threads must never block on disk/network I/O | HDF5 writes, HTTP requests, or file I/O in the audio/DSP thread |
| 10 | Strict venv isolation, exact version pinning, no side-effect imports | Import-time network calls, global state mutation on import |

**Additional contract detail:**
- Rule 5 expanded: Thread-level broad catches must log `ERROR` tracebacks, never `DEBUG` or `INFO`
- Rule 8 expanded: Specifically mandate explicit closure of `h5py.File`, `np.memmap`, file-backed numpy arrays
- Rule 9 expanded: FFT/audio threads on strict real-time deadlines; 200ms disk wait drops RTP packets

---

## 2. Companion Contracts (Cross-Check These)

The CODING_CONTRACT is the primary lens, but violations of these companion contracts should also be flagged:

| Contract | File | Key Cross-Check Items |
|----------|------|-----------------------|
| **DATA_CONTRACT** | `.windsurf/contracts/DATA_CONTRACT.md` | All `h5py.File()` must use `locking=False`; `HDF5_USE_FILE_LOCKING=FALSE` set before `import h5py`; CR-1 through CR-7 consistency rules enforced at write time; no per-record writes for high-frequency products (>10/min); schema version bumped on field changes |
| **METROLOGY_CONTRACT** | `.windsurf/contracts/METROLOGY_CONTRACT.md` | `TickEdgeDetector` is sole source for `tick_timing` HDF5; physics validation gate ±15 ms must not be widened; dual Kalman must have independent state; CHU 74ms H3E correction must be applied |
| **PHYSICS_CONTRACT** | `.windsurf/contracts/PHYSICS_CONTRACT.md` | `HFPropagationModel` is sole propagation model (deprecated `PhysicsPropagationModel` must not be used); group-delay TEC is below noise floor (don't claim it works); `_processed_minutes` set prevents duplicate records |
| **WEB_API_CONTRACT** | `.windsurf/contracts/WEB_API_CONTRACT.md` | Routers must not read HDF5 directly (services layer); numpy types cast before JSON serialization; no `isoformat() + 'Z'`; loading/error states on all charts |
| **INSTALLATION_CONTRACT** | `.windsurf/contracts/INSTALLATION_CONTRACT.md` | Services run as `timestd` user; CPU affinity separation (Python on 0–7, radiod on 8–15); schema files in both venv and `/opt/` src tree |

---

## 3. Architecture Overview (For Orientation)

```
Phase 1: Core Recorder (RTP → raw_buffer)
  core_recorder_v2.py → channel_recorder.py → binary_archive_writer.py
  ↓
Phase 2: Metrology (raw → L1/L2 measurements)
  metrology_service.py → metrology_engine.py → tick_edge_detector.py
  bootstrap_state.py, buffer_timing.py, broadcast_specs.py
  l2_calibration_service.py → broadcast_kalman_filter.py
  ↓
Phase 3: Fusion & Physics (L2 → L3 science products)
  multi_broadcast_fusion.py → chrony_shm.py (Chrony SHM output)
  physics_fusion_service.py → carrier_tec.py, propagation_model.py
  ↓
GRAPE Module (parallel pipeline)
  decimation_pipeline.py → decimation.py → spectrogram.py → packager.py → uploader.py
  ↓
Web API (visualization)
  web-api/main.py → routers/*.py → services/*.py → HDF5 reads
```

### Service Boundaries (systemd units)

| Service | Entry Point | Thread Model |
|---------|------------|--------------|
| `timestd-core-recorder` | `core_recorder_v2.py` | 1 thread per channel (9–17 channels), RTP receive + write |
| `timestd-metrology` | `metrology_service.py` | 1 thread per channel, processes raw buffer minutes |
| `timestd-l2-calibration` | `l2_calibration_service.py` | Single-threaded, reads L1, writes L2 |
| `timestd-fusion` | `multi_broadcast_fusion.py` | Single-threaded, 8-second cycle, writes Chrony SHM |
| `timestd-physics` | `physics_service.py` | Single-threaded + reanalysis timer |
| `timestd-web-api` | `web-api/main.py` | FastAPI/uvicorn async |
| `grape-daily.timer` | `cli.py grape daily` | One-shot daily batch |

---

## 4. Known Problem Areas (Prioritize These)

These are known issues or areas of fragility discovered during development. Verify whether they have been properly fixed and whether the fixes are robust:

### 4a. Error Handling & Silent Failures
- **`DataProductReader.read_time_range()`** in `io/hdf5_reader.py` loads entire HDF5 dataset into memory (`f[field_name][:]`) — O(N) full table scan for every time-range query. Any caller reading from large daily files (e.g., VTEC at 67K rows) will time out or OOM.
- **Silent exception swallowing**: Search for `except Exception` and `except:` blocks that log at `DEBUG` or don't log at all. Per CODING_CONTRACT Rule 5, broad catches at thread boundaries must log at `ERROR`/`WARNING`.
- **HDF5 read failures logged at DEBUG**: The DATA_CONTRACT specifically warns about "silent exception swallowing in HDF5 reads" causing invisible data starvation.

### 4b. Resource Management
- **`h5py.File()` without context managers**: Any `h5py.File()` opened without `with` and not explicitly closed leaks file descriptors. Particularly dangerous in long-running services.
- **`np.memmap` and `np.frombuffer` leaks**: Per CODING_CONTRACT Rule 8, file-backed numpy arrays hold C-level FDs that Python's GC doesn't deterministically clean up. Look for missing `del mm` or `.close()`.
- **HDF5 locking**: Every `h5py.File()` call must use `locking=False`. Check for any calls missing this parameter.
- **`HDF5_USE_FILE_LOCKING` env var timing**: Must be set **before** `import h5py`. Check import order in all modules.

### 4c. Thread Safety & Concurrency
- **Shared mutable state between metrology threads**: Each channel runs in its own thread. Any shared singleton (e.g., `ArrivalPatternMatrix`, `IonoDataService`, `BroadcastSpecs`) must be thread-safe.
- **HDF5 concurrent access**: Multiple services may read the same HDF5 file simultaneously. With `locking=False`, concurrent reads are safe but concurrent read+write is not. Check for any write-while-read scenarios.
- **Chrony SHM writes**: Must not block the fusion cycle. Check that SHM write is non-blocking.

### 4d. DSP Hot Path Performance (CODING_CONTRACT Rule 6)
- **`metrology_engine.py` `process_minute()`**: This is the innermost hot path — runs per-channel per-minute. Check for: numpy array allocations inside loops, list appends that should be pre-allocated arrays, Python-level iteration over samples.
- **`tick_edge_detector.py`**: Runs 57 matched-filter correlations per minute per channel. Check for allocation inside the per-tick loop.
- **`correlator_bank.py`**: Bank of correlators — check buffer reuse.

### 4e. Deprecated / Zombie Code
- **`physics_propagation.py`** (`PhysicsPropagationModel`): Explicitly deprecated by PHYSICS_CONTRACT. Must not be imported or called by any active code. Check for lingering imports.
- **`tof_kalman_ms`**: Deprecated schema field (all NaN). Check if any code reads or writes this field.
- **`core/legacy/wwvh_discrimination_archive.py`**: In a `legacy/` directory — verify nothing imports it.
- **`tick_matched_filter.py`** vs **`tick_edge_detector.py`**: The edge detector is the canonical timing source per METROLOGY_CONTRACT. `tick_matched_filter` may still exist for carrier phase extraction — verify it is not writing to `tick_timing/` HDF5 (which would be a contract violation).
- **`audio_stream.py` vs `audio_streamer.py`**: Two similarly-named modules — check if one is dead.
- **`ionospheric_model.py`**: May overlap with `propagation_model.py` — check if deprecated.

### 4f. Test Coverage Gaps
- **28 test files exist** for **~70 source modules** — coverage is sparse.
- **No tests for**: `core_recorder_v2.py`, `channel_recorder.py`, `binary_archive_writer.py`, `multi_broadcast_fusion.py`, `chrony_shm.py`, `l2_calibration_service.py`, `physics_fusion_service.py`, `quota_manager.py`, the entire GRAPE module, or any web-api code.
- **Critical untested paths**: HDF5 writer crash safety (simulate SIGKILL during write), day-boundary rollover, service restart with stale state files, RTP gap handling in core recorder.

### 4g. Data Integrity
- **Day boundary rollover** (midnight UTC): Files rotate daily. Check for off-by-one in date calculations, race conditions between the last write of day N and the first write of day N+1, and correct file selection when processing "yesterday."
- **HDF5 string encoding**: Recently fixed (variable-length → fixed-length byte strings). Verify the writer and reader are consistent. The writer uses `S` dtype; the reader must `.decode()` byte strings. Check for any code path that assumes strings are `str` type when reading from HDF5.
- **Schema consistency**: New fields added to code but not to JSON schemas, or vice versa.

---

## 5. Source Module Inventory

### Core Library (`src/hf_timestd/core/`) — 50+ modules

**Phase 1 — Recording:**
`core_recorder_v2.py`, `channel_recorder.py`, `binary_archive_writer.py`, `packet_resequencer.py`, `audio_buffer.py`

**Phase 2 — Metrology:**
`metrology_service.py`, `metrology_engine.py`, `tick_edge_detector.py`, `tick_matched_filter.py`, `bootstrap_state.py`, `bootstrap_validator.py`, `buffer_timing.py`, `broadcast_specs.py`, `decoder_config.py`, `bpm_discriminator.py`, `probabilistic_discriminator.py`, `multi_station_detector.py`, `audio_tone_monitor.py`, `chu_fsk_decoder.py`, `correlator_bank.py`

**Phase 2 — Calibration:**
`l2_calibration_service.py`, `broadcast_kalman_filter.py`, `clock_convergence.py`

**Phase 3 — Fusion:**
`multi_broadcast_fusion.py`, `fusion_timing_state.py`, `chrony_shm.py`, `chrony_stats.py`, `consensus_combiner.py`, `global_timing_coordinator.py`, `differential_time_solver.py`

**Phase 3 — Physics/Science:**
`physics_service.py`, `physics_fusion_service.py`, `carrier_tec.py`, `propagation_model.py`, `propagation_engine.py`, `propagation_mode_solver.py`, `propagation_stats.py`, `arrival_pattern_matrix.py`, `iono_data_service.py`, `ionospheric_model.py`, `ionospheric_reanalysis.py`, `iono_tomography.py`, `gnss_tec.py`, `advanced_signal_analysis.py`

**Infrastructure:**
`quality_metrics.py`, `operational_phase_manager.py`, `primary_time_standard.py`, `gpsdo_monitor.py`, `ground_truth_validator.py`, `leap_second.py`

**Deprecated/Legacy (verify no active imports):**
`physics_propagation.py`, `legacy/wwvh_discrimination_archive.py`

### I/O Layer (`src/hf_timestd/io/`)
`hdf5_writer.py`, `hdf5_reader.py`

### GRAPE Module (`src/hf_timestd/grape/`)
`decimation_pipeline.py`, `decimation.py`, `decimated_buffer.py`, `raw_reader.py`, `spectrogram.py`, `packager.py`, `uploader.py`

### Top-Level
`cli.py`, `config_utils.py`, `channel_manager.py`, `quota_manager.py`, `audio_stream.py`, `audio_streamer.py`, `cddis.py`, `cddis_auth.py`

### Web API (`web-api/`)
`main.py`, `routers/*.py` (14 routers), `services/*.py`

### Tests (`tests/`) — 28 files
See Section 4f for coverage gap analysis.

---

## 6. Recommended Review Order

Start with the highest-impact, highest-risk modules and work outward:

1. **Hot path**: `metrology_engine.py` → `tick_edge_detector.py` → `buffer_timing.py` (Rules 6, 9)
2. **Data integrity**: `io/hdf5_writer.py` → `io/hdf5_reader.py` (Rules 5, 8; DATA_CONTRACT)
3. **Long-running services**: `multi_broadcast_fusion.py` → `chrony_shm.py` → `fusion_timing_state.py` (Rules 5, 8, 9)
4. **Core recorder**: `core_recorder_v2.py` → `channel_recorder.py` → `packet_resequencer.py` (Rules 6, 8, 9)
5. **Calibration chain**: `l2_calibration_service.py` → `broadcast_kalman_filter.py` (Rule 7)
6. **Physics pipeline**: `physics_fusion_service.py` → `carrier_tec.py` → `propagation_model.py` (PHYSICS_CONTRACT)
7. **GRAPE module**: `decimation_pipeline.py` → `packager.py` → `uploader.py` (Rules 5, 8)
8. **Web API**: `services/*.py` → `routers/*.py` (WEB_API_CONTRACT)
9. **Zombie hunt**: Cross-reference all imports to find dead modules and unreachable code paths
10. **Test audit**: Map test files to source modules, identify untested critical paths

---

## 7. Diagnostic Commands

```bash
# Run existing test suite
cd /home/mjh/git/hf-timestd && python -m pytest tests/ -v --tb=short 2>&1 | tail -40

# Find bare except clauses (CODING_CONTRACT Rule 5)
grep -rn 'except:' src/hf_timestd/ --include='*.py'
grep -rn 'except Exception' src/hf_timestd/ --include='*.py' | grep -v 'logging\|logger\|log\.'

# Find h5py.File without locking=False (DATA_CONTRACT)
grep -rn 'h5py.File(' src/hf_timestd/ web-api/ --include='*.py' | grep -v 'locking'

# Find h5py.File without context manager
grep -rn 'h5py.File(' src/hf_timestd/ web-api/ --include='*.py' | grep -v 'with '

# Find HDF5_USE_FILE_LOCKING timing issues
grep -rn 'HDF5_USE_FILE_LOCKING' src/hf_timestd/ web-api/ --include='*.py'

# Find *args/**kwargs in core APIs (CODING_CONTRACT Rule 3)
grep -rn '\*args\|\*\*kwargs' src/hf_timestd/core/ --include='*.py'

# Find deprecated PhysicsPropagationModel usage (PHYSICS_CONTRACT)
grep -rn 'PhysicsPropagationModel\|physics_propagation' src/hf_timestd/ --include='*.py' | grep -v 'physics_propagation.py'

# Find deprecated tof_kalman_ms usage
grep -rn 'tof_kalman' src/hf_timestd/ web-api/ --include='*.py'

# Find imports of legacy module
grep -rn 'wwvh_discrimination_archive' src/hf_timestd/ --include='*.py' | grep -v 'legacy/'

# Check for numpy allocations in hot-path loops (CODING_CONTRACT Rule 6)
grep -rn 'np\.zeros\|np\.empty\|np\.array' src/hf_timestd/core/metrology_engine.py src/hf_timestd/core/tick_edge_detector.py

# Module-level complexity scan (deeply nested blocks)
grep -rn 'if\|for\|while\|try' src/hf_timestd/core/metrology_engine.py | wc -l

# Check services are healthy after review changes
systemctl status timestd-metrology timestd-fusion timestd-physics timestd-web-api
```

---

## 8. Output Format

For each module or subsystem reviewed, produce findings in this structure:

```markdown
### Module: `<module_name.py>`

**Contract Violations:**
- [RULE #] Description of violation (line N)

**Edge Cases & Error Handling:**
- [CRITICAL/HIGH/MEDIUM/LOW] Description

**Resource Management:**
- Description of leak or missing cleanup

**Dead Code:**
- Description of unused function/import/branch

**Test Coverage:**
- What is tested / what is not

**Recommendations:**
1. [CRITICAL] ...
2. [HIGH] ...
3. [MEDIUM] ...
```
