# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: PHYSICS SERVICE MEMORY LEAK + OPERATIONAL RESILIENCE

**Objective:** Investigate and fix the `timestd-physics` memory leak that causes OOM kills, and address operational resilience issues (logrotate, service recovery) that allow silent failures to persist for hours.

**Context:** The physics service (`physics_fusion_service.py`) was OOM-killed on 2026-02-13 at 11:01 UTC after consuming 16h 25min of CPU time. After restart, it grew to 2.9 GB RSS (10% of 30 GB RAM) within 13 hours. The fusion service also stalled silently — it ran for 10+ hours producing `0 entries from 11 channels` every 8-second cycle due to missing schema files (stale process from before a production update). Both failures were invisible because logrotate truncated the log files without signaling the services, so `.log` was 0 bytes while output went to `.log.1`.

---

### Known State (as of 2026-02-14)

**What works (after `update-production.sh` run at 12:07 UTC):**
- All services running, `verify_pipeline.sh` shows 32 PASS, 0 FAIL
- Fusion reading 547 tick timing observations from 9 channels per cycle
- Physics producing TEC estimates (fresh within seconds)
- GRAPE daily pipeline fully operational with validation gates (see Resolved section)
- `update-production.sh` correctly removes editable installs and does non-editable `pip install`

**What needs investigation:**

1. **Physics service memory leak** — grows unbounded, OOM-killed after ~19 hours
   - PID 92947: 2.9 GB RSS after 13h, was OOM-killed previous day at 16h 25min CPU
   - Likely cause: accumulating data structures in `PhysicsFusionService` main loop
   - Key file: `src/hf_timestd/core/physics_fusion_service.py`
   - Related: `src/hf_timestd/core/tec_estimator.py`, `src/hf_timestd/io/hdf5_reader.py`
   - The service re-initializes `DataProductReader` objects every cycle (line-level investigation needed)

2. **Logrotate misconfiguration** — services write to stale file descriptors after rotation
   - Config: `/etc/logrotate.d/hf-timestd` uses `create` mode (rename old → create new)
   - Problem: long-running services keep the old FD open, write to `.log.1` while `.log` stays 0 bytes
   - Fix options: (a) `copytruncate` instead of `create`, or (b) add `postrotate` block to send SIGHUP/restart
   - Affects: `fusion.log`, `physics.log`, `phase2-*.log`

3. **Silent failure pattern** — services can run for hours producing nothing with no alert
   - Fusion ran 10+ hours reading 0 measurements every 8s — no alarm triggered
   - The watchdog (`WatchdogSec=120`) only checks if the process is alive, not if it's productive
   - Consider: a "productivity watchdog" that checks output freshness (e.g., HDF5 mtime)

### Verification Steps for This Session

1. **Profile physics memory** — add `tracemalloc` or monitor RSS growth over time, identify the leaking data structure
2. **Fix the leak** — likely need to bound or clear accumulated state in the main loop
3. **Fix logrotate** — switch to `copytruncate` or add `postrotate` signal handling
4. **Consider MemoryMax** — add `MemoryMax=4G` to `timestd-physics.service` as a safety net
5. **Consider output freshness monitoring** — extend `verify_pipeline.sh` or add a cron-based freshness check that alerts on stale outputs

---

## 🏗️ ARCHITECTURE REFERENCE

### Data Flow (v6.8 — with Edge Ensemble + Propagation Model)

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer (60s)
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology (9 channels)
                                                           ↓ (per channel, per station)
                                                    MetrologyEngine.process_minute()
                                                      ├─ AM demod → bandpass → matched filter correlation
                                                      ├─ Per-tone detection (minute markers + long tones)
                                                      ├─ TickEdgeDetector: 50-57 per-second tick edges/min
                                                      │   └─ EDGE ENSEMBLE recovery when minute marker fails
                                                      ├─ TickMatchedFilter: per-second tick phase extraction
                                                      │   └─ Gated by edge_results || _check_signal_presence()
                                                      ├─ BufferTiming: sample → UTC via RTP chain
                                                      ├─ ArrivalPatternMatrix physics validation
                                                      │   └─ HFPropagationModel.predict()
                                                      │       └─ IonoDataService (WAM-IPE/GIRO/fallback)
                                                      ├─ Multi-mode arrival windows (1F, 2F, 3F, 1E)
                                                      ├─ timing_error_ms = arrival_utc - expected_utc
                                                      └─ L1MetrologyMeasurement → fusion
                                                           ↓
                                                    HDF5: L2/timing_measurements, L2/tick_timing,
                                                          L2/detection_attempts
                                                           ↓
                                                    MultiBroadcastFusion → Chrony SHM
                                                           ↓
                                                    web-api dashboard-24h.html

                                              === GRAPE Pipeline (Phase 3) ===
Raw IQ Buffer (60s, 24 kHz) → DecimationPipeline → DecimatedBuffer (10 Hz daily .bin)
                                                         ↓
                                                    CarrierSpectrogramGenerator → PNG spectrograms
                                                         ↓
                                                    DailyDRFPackager → Digital RF (multi-subchannel)
                                                         ↓
                                                    UploadManager (SFTP) → PSWS repository
                                                         (pswsnetwork.eng.ua.edu)
```

### GRAPE Pipeline Detail

| Stage | Input | Output | Code | Trigger |
|-------|-------|--------|------|---------|
| **Decimation** | `raw_archive/<CH>/<YYYYMMDD>/*.bin.zst` (24 kHz IQ) | `products/<CH>/decimated/<YYYYMMDD>.bin` (10 Hz IQ) | `grape/decimation_pipeline.py` → `grape/decimation.py` (CIC + compensation FIR) | `grape decimate` |
| **Spectrogram** | `products/<CH>/decimated/<YYYYMMDD>.bin` | `products/<CH>/spectrograms/<YYYYMMDD>_daily.png` | `grape/spectrogram.py` | `grape spectrogram` |
| **Packaging** | `products/<CH>/decimated/<YYYYMMDD>.bin` (all 9 channels) | `upload/<YYYYMMDD>/<CALL>_<GRID>/<RX>@<ID>/OBS.../ch0/` (Digital RF) | `grape/packager.py` | `grape package` |
| **Upload** | `upload/<YYYYMMDD>/.../OBS.../` | PSWS repository via SFTP + trigger dir | `grape/uploader.py` (`SFTPUpload` class) | `grape upload` |

### Decimation Design

- **Two-stage**: 24 kHz → 400 Hz (CIC R=60, N=4) → 10 Hz (compensation FIR + decimate R=40)
- **Phase-continuous**: `StatefulDecimator` maintains filter state across minute boundaries
- **Gap handling**: Missing minutes fed as zeros to preserve filter state and time alignment
- **Output**: `complex64` at 10 Hz = 600 samples/minute, 864,000 samples/day = 6.9 MB/day/channel

### PSWS Upload Protocol (wsprdaemon-compatible)

1. `scp -r` the OBS directory to `{station_id}@pswsnetwork.eng.ua.edu:`
2. `sftp mkdir` a trigger directory: `c{OBS_name}_#{instrument_id}_#{timestamp}`
3. PSWS server processes data upon seeing trigger directory
4. `.upload_complete` marker file created locally after success

### Tone Hierarchy (2026-02-12 — Edge Ensemble Active)

| Station | Tone | Duration | Role | Status |
|---------|------|----------|------|--------|
| **CHU** | Minute marker (sec 0) | 500ms | PRIMARY timing anchor | ✅ Detected, 10–47 dB SNR |
| **CHU** | Per-second tones (sec 1–28, 40–49) | 300ms | Excellent timing source | ✅ 14–15/min validated |
| **WWV/WWVH** | Minute marker (sec 0) | 800ms | PRIMARY timing anchor | ✅ Detected when propagation supports |
| **WWV/WWVH** | Per-second ticks (sec 1–58) | 5ms | Edge ensemble recovery | ✅ 50–57 edges/min, ±2ms uncertainty |
| **BPM** | Minute marker (sec 0) | 300ms | Timing anchor | ⚠️ Marginal SNR on shared channels |
| **BPM** | Per-second ticks | 10ms | Edge ensemble recovery | ✅ 35–58 edges/min |

**Edge Ensemble (new 2026-02-12):** `TickEdgeDetector` uses differential envelope detection on 5ms/10ms ticks. While individual ticks have ±50ms jitter, the SNR-weighted robust median of 50+ ticks per minute yields ±2ms timing. This provides **recovery when the minute marker correlation fails** (nighttime, fading). The edge ensemble does NOT use the matched filter correlation path — it detects onset edges directly.

**TickMatchedFilter** (separate from edge ensemble): Extracts carrier phase from per-second ticks for ionospheric analysis (Doppler, TEC, scintillation). Gated by `edge_results` presence (fixed 2026-02-12 — was broken by `_check_signal_presence()` failing on 0.5% duty cycle ticks). Now produces 56 windows/min with 15–26 dB SNR on shared channels.

### Key Files (This Session Focus)

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/physics_fusion_service.py` | Physics daemon — TEC estimation, main loop with suspected leak | **Critical** |
| `src/hf_timestd/core/tec_estimator.py` | TEC math — may accumulate state | **Critical** |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Fusion daemon — Dual Kalman → Chrony SHM | High |
| `src/hf_timestd/io/hdf5_reader.py` | `DataProductReader` — re-initialized every cycle in physics | High |
| `systemd/timestd-physics.service` | Physics systemd unit — needs `MemoryMax`? | High |
| `systemd/timestd-fusion.service` | Fusion systemd unit — `WatchdogSec=120`, `Type=notify` | High |
| `/etc/logrotate.d/hf-timestd` | Logrotate config — needs `copytruncate` or `postrotate` | High |
| `scripts/update-production.sh` | Canonical deploy script — non-editable install + restart | Reference |

### Key Files (Detection & GRAPE — for reference)

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | `process_minute()`, edge ensemble, physics validation |
| `src/hf_timestd/core/tick_edge_detector.py` | `TickEdgeDetector` — per-second onset edge detection |
| `src/hf_timestd/core/tick_matched_filter.py` | Per-second tick phase extraction |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Dual Kalman fusion → Chrony SHM |
| `src/hf_timestd/core/propagation_model.py` | HFPropagationModel — ionospheric delay prediction |
| `src/hf_timestd/grape/decimation_pipeline.py` | Orchestrates read → decimate → write per channel/day |
| `src/hf_timestd/grape/packager.py` | `DailyDRFPackager` — multi-subchannel Digital RF output |
| `src/hf_timestd/grape/uploader.py` | `UploadManager`, `SFTPUpload` — PSWS upload |
| `src/hf_timestd/cli.py` | CLI entry point — `grape daily` orchestrated pipeline |

### Key Data Products

| Product | Path | Contents |
|---------|------|----------|
| **Decimated IQ** | `products/<CH>/decimated/<YYYYMMDD>.bin` | 10 Hz complex64, 6.9 MB/day/channel |
| **Decimated metadata** | `products/<CH>/decimated/<YYYYMMDD>_meta.json` | Per-minute timing, gaps, quality |
| **Spectrograms** | `products/<CH>/spectrograms/<YYYYMMDD>_daily.png` | 24h carrier spectrogram + solar zenith |
| **DRF package** | `upload/<YYYYMMDD>/<CALL>_<GRID>/<RX>@<ID>/OBS.../ch0/` | Digital RF for PSWS |
| **Upload queue** | `upload/queue.json` | Persistent upload task queue |
| `L2/timing_measurements` | `phase2/<CH>/clock_offset/` | Accepted timing detections |
| `L2/tick_timing` | `phase2/<CH>/tick_timing/` | Per-minute tick phase/SNR aggregate |
| `L2/detection_attempts` | `phase2/<CH>/detection_attempts/` | All attempts with rejection reasons |

### Service Inventory

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony (WatchdogSec=120) | `/var/log/hf-timestd/fusion.log` |
| `timestd-physics` | TEC estimation, L3 physics products (**leaks memory**) | `/var/log/hf-timestd/physics.log` |
| `timestd-l2-calibration` | L2 adaptive calibration | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| `timestd-vtec` | GNSS VTEC estimation (optional) | journalctl |
| `grape-daily.timer` | Daily GRAPE processing trigger (01:00 UTC) | journalctl |
| `grape-daily.service` | `grape daily` — orchestrated pipeline with validation gates | journalctl |
| `timestd-radiod-monitor` | Radiod health monitoring (optional) | journalctl |
| **radiod** | Real-time USB/FFT (CPU 8-15, uncontested L3 cache) | journalctl |

### Station Configuration

| Key | Value |
|-----|-------|
| Callsign | AC0G |
| Grid | EM38ww40pk |
| PSWS Station ID | S000171 |
| PSWS Instrument ID | 172 |
| PSWS Server | pswsnetwork.eng.ua.edu |
| Upload Protocol | SFTP (wsprdaemon-compatible) |

### Deployment

- **Git repo**: `/home/mjh/git/hf-timestd/`
- **Production install**: `/opt/hf-timestd/` (venv with non-editable `pip install`)
- **Deploy script**: `sudo scripts/update-production.sh [--pull]` — removes editable installs, copies package, syncs web-api/scripts/docs, updates systemd, restarts services, verifies
- **NEVER use `pip install -e`** in production — editable installs make the git repo live production code
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data root**: `/var/lib/timestd/`
- **3 machines**: bee1 (primary), B3-1, B4-1

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### GRAPE Pipeline + PSWS Uploads (2026-02-14)

**Full GRAPE pipeline now operational.** New `grape daily` CLI command orchestrates: decimate all 9 channels → validate all decimated → generate 9 spectrograms → validate all spectrograms → package Digital RF → upload to PSWS. Three validation gates abort the pipeline if any stage is incomplete, preventing partial uploads.

- `grape-daily.service` simplified to single `ExecStart` calling `grape daily`
- Fixed `gap_samples` unit mismatch in `decimation_pipeline.py` — was storing raw 24kHz sample counts but `update_summary()` compared against 10Hz `SAMPLES_PER_MINUTE` (600), producing -2600% completeness. Now divides by decimation ratio before writing metadata.
- Fixed 97 existing metadata files in-place.
- Verified: 20260213 processed 9/9 channels, 9/9 spectrograms, uploaded to PSWS in 63 minutes.

### Fusion Schema Stall (2026-02-14)

**Root cause:** Running fusion/physics processes loaded module paths from a stale (pre-editable-install) Python environment. Schema files resolved to `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/schemas/` which no longer existed after editable install pointed imports to the git repo. Result: `Available schemas: {}`, 0 measurements read every cycle for 10+ hours.

**Fix:** Ran `update-production.sh` which removed the editable install, did a proper non-editable `pip install`, and restarted all services. Schemas now resolve correctly from the venv's copied package.

### Signal Presence Gate Fix (2026-02-12, session 3)

**Root cause of sparse dashboard panels on shared WWV/WWVH channels.**

`_check_signal_presence()` always returned `False` on shared channels because:
1. It bandpass-filtered raw IQ at 1000/1200 Hz — but those are AM modulation frequencies in the envelope, not baseband IQ
2. Even after fixing AM demod (`np.abs`), 5ms ticks have 0.5% duty cycle — band energy never exceeds 3× noise floor

This killed `TickMatchedFilter` on ALL shared channels → no `tick_timing` HDF5 data → dashboard's second-pass supplement had nothing to add → sparse dots.

**Fix:** Use `edge_results` (already computed earlier in `process_minute()`) as primary signal presence indicator. Falls back to `_check_signal_presence()` for CHU (300ms ticks, 30% duty cycle — works fine).

**Result:** tick_timing now writing 56 windows/min with 15–26 dB SNR. Dashboard Y-axis normalized with p95 percentile.

### Edge Ensemble Implementation (2026-02-12, session 3)

`TickEdgeDetector` class in `tick_edge_detector.py` — detects per-second tick onset edges via differential envelope, inspired by ntpd refclock_wwv.c Type 36 driver. Provides EDGE ENSEMBLE recovery (50–57 ticks/min, ±2ms) when minute marker correlation fails. Integrated into `process_minute()` after the RTP correlation loop.

### Matched Filter False Positive Fix (2026-02-12, session 2)

Search window capped at ±100ms (was ±500ms). Noise exclusion zone widened. FP rate: 21% → <1% with physics gate.

### Metrology Methodology Audit (2026-02-12, session 1)

RTP timestamps as sole authority. Physics validation simplified. Short ticks dropped from correlation (but recovered via edge ensemble). Bandpass before correlate.

### Propagation Delay Modeling (2026-02-12)

WAM-IPE + GIRO + climatological fallback. HFPropagationModel with multi-mode arrivals (1F/2F/3F/1E). 23 tests passing. See: `docs/changes/SESSION_2026_02_12_PROPAGATION_MODEL.md`

### Phase Continuity + Doppler UI (2026-02-11)

IQ mixer phase jump fixed. Three-tier phase extraction. Cross-frequency discrimination gate. Phase/Doppler web dashboard.

### Earlier Fixes

- **CHU FSK Decoder** (2026-02-10): USB sidecar channels, quadrature demodulation, ring buffer timing
- **Pipeline Offset Calibration** (2026-02-09): Removed — radiod RTP timestamps authoritative
- **Timing Accuracy** (2026-02-06): Circular calibration, GPS ground truth, Kalman filter, serialization
- **HDF5 Crash Safety** (2026-02-06): SWMR eliminated — open-write-close
- **CHU Memory Leak** (2026-01-02): Extract 1.1s slice before demodulation
- **HDF5 File Lock Contention** (recurring): `locking=False` on all h5py.File() calls

---

## 🔑 KEY PRINCIPLES

1. **The GPSDO is a steel ruler.** Every sample has a known UTC timestamp via the RTP chain. The buffer exists only to find the tone. Once found, read the timestamp. That's the ToA.
2. **The ionosphere is the unknown.** Multi-frequency, multi-station geometry solves it — with or without GPS. GPS just removes one unknown (clock error).
3. **Longer tones = better timing.** Precision scales as √(duration). 800ms marker >> 300ms CHU >> 100ms BPM. But 50+ short ticks averaged via edge ensemble can match a single long tone.
4. **The physics doesn't change — only the ruler does.** Without GPS, the system can bootstrap from tone inter-relationships alone.
5. **Bandpass before correlate.** On shared channels, competing stations corrupt long-template correlations. A narrow bandpass (±50 Hz) isolates the target tone.
6. **Edge results are the signal presence indicator.** The old band-energy test fails for short-duty-cycle ticks. If the edge detector found ticks, signal is present.

---

## ✅ Success Criteria — This Session

1. **Physics memory leak identified and fixed** — RSS stays bounded over 24+ hours, no OOM kill
2. **Logrotate fixed** — services write to the current `.log` file after rotation, not `.log.1`
3. **`MemoryMax` safety net** — physics service has a systemd memory limit to prevent OOM-killing other services
4. **Output freshness monitoring** — some mechanism alerts when fusion/physics stop producing useful output
5. **`verify_pipeline.sh` stays at 0 FAIL** after 24 hours of operation
