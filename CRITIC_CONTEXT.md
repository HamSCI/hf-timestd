# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: GRAPE MODULE AND PSWS UPLOADS

**Objective:** Confirm end-to-end functionality of the GRAPE (GRAPE Recorder and Processor Engine) module — the Phase 3 pipeline that decimates 24 kHz IQ data to 10 Hz, packages it as Digital RF, and uploads to the HamSCI PSWS repository. The system should produce daily uploads automatically via the `grape-daily.timer`.

**Context:** The grape module exists and has run successfully at least once (2026-01-20 upload completed to PSWS). However, the `grape-daily.service` produces no journalctl entries despite the timer being active and firing daily. Decimation IS running (latest decimated files are from 2026-02-10/11), but no uploads have occurred since Jan 20. The pipeline needs end-to-end verification and likely has configuration or wiring issues preventing daily uploads.

---

### Known State (as of 2026-02-12)

**What works:**
- `grape-daily.timer` is active, fires daily at ~01:00 UTC
- Decimation produces daily `.bin` + `_meta.json` files in `products/<CHANNEL>/decimated/` (latest: 20260210/20260211)
- DRF packaging worked once (2026-01-20, output in `upload/20260120/`)
- SFTP upload to PSWS completed once (queue.json shows `status: completed` for 2026-01-20)
- Raw archive has continuous data (`raw_archive/<CHANNEL>/<YYYYMMDD>/` with ~2866 minute files/day)
- CLI commands exist: `grape decimate`, `grape spectrogram`, `grape package`, `grape upload`, `grape status`

**What's broken or missing:**
- `grape-daily.service` produces **no journal entries** — the service may be failing silently or the Python path may be wrong (`/usr/bin/python3` vs `/opt/hf-timestd/venv/bin/python3`)
- No `[uploader.sftp]` section in config — no host, user, or SSH key configured. CLI falls back to defaults (`pswsnetwork.eng.ua.edu`, `~/.ssh/psws_key`)
- No SSH key found at `~/.ssh/psws_key` or `/home/timestd/.ssh/`
- Only 1 upload in history (Jan 20) despite 3+ weeks of data available
- `grape package` in the systemd service is missing `--date` argument (required per CLI definition)
- The service uses `/usr/bin/python3` but the project is installed in `/opt/hf-timestd/venv/`

### Verification Steps for This Session

1. **Run `grape decimate` manually** for yesterday's data and confirm output
2. **Run `grape spectrogram` manually** for one channel and confirm PNG output
3. **Run `grape package` manually** for a recent date and confirm DRF output
4. **Run `grape upload --dry-run`** to verify config and path resolution
5. **Fix `grape-daily.service`** — correct Python path, add missing `--date` arg, verify journal output
6. **Configure SFTP** — add `[uploader.sftp]` section with host, user, SSH key path
7. **Test actual upload** to PSWS for one day's data
8. **Verify on PSWS** that the uploaded data appears correctly

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

### Key Files (GRAPE Focus)

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/grape/decimation.py` | CIC + compensation FIR decimation (24 kHz → 10 Hz) | **Critical** |
| `src/hf_timestd/grape/decimation_pipeline.py` | Orchestrates read → decimate → write per channel/day | **Critical** |
| `src/hf_timestd/grape/decimated_buffer.py` | Binary 10 Hz IQ storage with gap/timing metadata | **Critical** |
| `src/hf_timestd/grape/raw_reader.py` | Reads raw_archive `.bin.zst` minute files | **Critical** |
| `src/hf_timestd/grape/packager.py` | `DailyDRFPackager` — multi-subchannel Digital RF output | **Critical** |
| `src/hf_timestd/grape/uploader.py` | `UploadManager`, `SFTPUpload`, `SSHRsyncUpload` — PSWS upload | **Critical** |
| `src/hf_timestd/grape/spectrogram.py` | `CarrierSpectrogramGenerator` — daily/rolling PNG spectrograms | High |
| `src/hf_timestd/cli.py` | CLI entry point — `grape` subcommand group (lines 307–511) | High |
| `systemd/grape-daily.service` | Systemd oneshot — decimate + spectrogram + package + upload | **Critical** |
| `systemd/grape-daily.timer` | Daily timer at 01:00 UTC | High |
| `config/timestd-config.toml` | `[uploader]` and `[station]` sections | High |

### Key Files (Detection & Fusion — for reference)

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | `process_minute()`, edge ensemble, physics validation |
| `src/hf_timestd/core/tick_edge_detector.py` | `TickEdgeDetector` — per-second onset edge detection |
| `src/hf_timestd/core/tick_matched_filter.py` | Per-second tick phase extraction |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Dual Kalman fusion → Chrony SHM |
| `src/hf_timestd/core/propagation_model.py` | HFPropagationModel — ionospheric delay prediction |

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
| `timestd-fusion` | Multi-broadcast fusion → Chrony | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| `grape-daily.timer` | Daily GRAPE processing trigger (01:00 UTC) | journalctl |
| `grape-daily.service` | Decimate + spectrogram + package + upload | journalctl (currently empty!) |
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
- **Production install**: `/opt/hf-timestd/` (venv with `pip install -e`)
- **Production copy shortcut**: `sudo cp <src> /opt/hf-timestd/...` + `sudo systemctl restart <service>`
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data root**: `/var/lib/timestd/`
- **3 machines**: bee1 (primary), B3-1, B4-1

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

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

1. **`grape decimate` produces output** — run manually for yesterday, confirm `.bin` + `_meta.json` in `products/<CH>/decimated/`
2. **`grape spectrogram` produces PNG** — run for one channel, confirm output in `products/<CH>/spectrograms/`
3. **`grape package` produces Digital RF** — run for a recent date, confirm DRF structure in `upload/<YYYYMMDD>/`
4. **`grape upload` succeeds** — configure SFTP (SSH key, `[uploader.sftp]` in config), upload one day to PSWS
5. **`grape-daily.service` runs end-to-end** — fix Python path, fix missing `--date` arg, confirm journal output
6. **Verify on PSWS** — uploaded data visible at pswsnetwork.eng.ua.edu for station S000171
