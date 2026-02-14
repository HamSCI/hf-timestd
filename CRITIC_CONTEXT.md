# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: DIAGNOSE FLAT/ANOMALOUS D_CLOCK ON 24-HOUR DASHBOARD

**Objective:** The 24-hour broadcast dashboard (`http://bee1:8000/static/dashboard-24h.html`) shows that most channels have **flat or bizarre D_clock/timing-error patterns** that do not track the expected diurnal ionospheric variation. This is a critical data quality problem — if the underlying measurements are wrong, all downstream calculations (TEC, tomography, VTEC maps) are unreliable. Diagnose the root cause and fix it.

### 🔍 THE PROBLEM (Observed 2026-02-14)

The dashboard "All Broadcasts" grid tab shows per-channel SNR scatter plots with a solar elevation overlay (yellow curve). The expected behavior is that timing error (and SNR) should show clear diurnal variation correlated with the solar curve — ionospheric delay increases during the day and decreases at night. Instead:

- **WWV 5, 15, 20, 25 MHz**: SNR dots are essentially **flat lines** clustered around a constant value. No diurnal variation visible.
- **WWV 2.5 MHz**: Shows some scatter/variation (expected — lowest freq has largest ionospheric delay and is most affected by D-layer absorption).
- **WWV 10 MHz**: Shows variation but with a suspicious **step-function** pattern — abrupt transitions rather than smooth diurnal curves.
- **CHU 14.67 MHz**: Shows step-like transitions — not the smooth diurnal curve expected from ionospheric variation.
- **CHU 3.33, 7.85 MHz**: Show orange blocks with abrupt transitions.
- **WWVH channels**: Show scattered blue dots with some variation but noisy/incoherent patterns.

### 🎯 INVESTIGATION PLAN

The problem could be at multiple levels. Investigate from bottom up:

#### Level 1: Raw L2 Data Quality

**Question:** Are the L2 HDF5 timing measurements themselves flat, or is the dashboard misreading them?

**Actions:**
- Read the actual L2 HDF5 files directly with h5py and plot D_clock, raw_arrival_time_ms, and tof_kalman_ms for several channels over 24h
- Check if the fields the dashboard reads (`raw_toa_ms`) actually exist — the dashboard code at `web-api/routers/dashboard.py:197` does `m.get('raw_toa_ms')` but the L2 schema may use a different field name (e.g., `raw_arrival_time_ms`)
- Check if timing_error is always None because the field name is wrong, causing the dashboard to show only SNR (which may appear flat if signal is constant)

**Key files:**
- `/var/lib/timestd/phase2/SHARED_10000/clock_offset/` — L2 timing measurements
- `/var/lib/timestd/phase2/WWV_20000/clock_offset/` — unique WWV channel
- `/var/lib/timestd/phase2/CHU_14670/clock_offset/` — unique CHU channel

#### Level 2: Dashboard Data Pipeline

**Question:** Is the dashboard correctly reading and displaying the data?

**Actions:**
- Check `web-api/routers/dashboard.py` field name mapping — `raw_toa_ms` vs `raw_arrival_time_ms` mismatch?
- Check if `timing_error_ms` is being computed correctly: `raw_toa - expected_delay`
- Check if the grid panels are plotting SNR (which may look flat) vs timing error (which should show diurnal variation)
- Verify the `min_propagation_ms` baseline used for timing error computation
- Check the tick_timing second pass — `mean_timing_offset_ms` field availability

**Key files:**
- `web-api/routers/dashboard.py` — lines 158–258 (data fetching), lines 382–481 (timing-error endpoint)
- `web-api/static/dashboard-24h.html` — lines 684–822 (grid rendering, `renderMiniChart()`)

#### Level 3: Tone Detection / Matched Filter

**Question:** Is the tone detection algorithm correctly identifying tick onsets across all frequencies?

**Actions:**
- Check if `TickMatchedFilter` is producing valid detections on all channels
- Check detection rates per channel — are some channels producing far fewer detections?
- Examine the matched filter template for each broadcast — are the tone frequencies and durations correct in `broadcast_specs.py`?
- Check if the signal presence gate (`_check_signal_presence()`) is incorrectly rejecting valid signals
- Verify that shared-frequency discrimination (WWV vs WWVH on 2.5/5/10/15 MHz) is working — could misattribution cause flat patterns?

**Key files:**
- `src/hf_timestd/core/tick_matched_filter.py` — 1015 lines, IQ matched filter
- `src/hf_timestd/core/tick_edge_detector.py` — 572 lines, per-second onset detection
- `src/hf_timestd/core/broadcast_specs.py` — 622 lines, tone schedules and templates
- `src/hf_timestd/core/wwvh_discrimination.py` — 3917 lines, WWV/WWVH separation on shared freqs
- `src/hf_timestd/core/bpm_discriminator.py` — 952 lines, BPM detection

#### Level 4: Metrology Engine / Calibration

**Question:** Is the metrology engine correctly computing D_clock from the detected ticks?

**Actions:**
- Check `metrology_engine.py` `process_minute()` — how does it convert tick detections to L2 measurements?
- Check if the L2 calibration service is over-correcting, flattening the ionospheric signal
- Check if the hardware calibration (`timing_calibrator.py`) has converged to wrong offsets
- Examine calibration state files in `/var/lib/timestd/state/`

**Key files:**
- `src/hf_timestd/core/metrology_engine.py` — 1724 lines, core measurement pipeline
- `src/hf_timestd/core/l2_calibration_service.py` — 591 lines, adaptive calibration
- `src/hf_timestd/core/timing_calibrator.py` — 2037 lines, hardware offset learning
- `src/hf_timestd/core/multi_broadcast_fusion.py` — 5169 lines, fusion + Kalman

#### Level 5: Upstream Signal Chain

**Question:** Is radiod delivering valid IQ data on all channels?

**Actions:**
- Check radiod status for all channels — are all decoders active?
- Check RTP packet flow — are packets arriving for all channels?
- Check if some channels have very low SNR (below detection threshold)
- Check system logs for errors

**Key commands:**
```bash
journalctl -u timestd-metrology --since "1 hour ago" | grep -i error
journalctl -u timestd-fusion --since "1 hour ago" | grep -i error
cat /var/lib/timestd/phase2/*/status.json | python3 -m json.tool
```

### 🔑 LIKELY ROOT CAUSES (Hypotheses)

1. **Dashboard field name mismatch** (most likely quick fix): `dashboard.py:197` reads `raw_toa_ms` but L2 schema uses `raw_arrival_time_ms`. If this field is always None, timing_error is always None, and the grid shows only SNR — which may appear flat on strong channels.

2. **Calibration over-correction**: The hardware calibration or L2 calibration service may have learned offsets that absorb the ionospheric variation, flattening D_clock to near-zero.

3. **Tone detection failure on some channels**: The matched filter may not be detecting ticks reliably on certain frequencies, producing sparse or constant-offset measurements.

4. **Shared-frequency discrimination errors**: On 2.5/5/10/15 MHz, WWV and WWVH overlap. If the discriminator is misattributing signals, the timing measurements could be incoherent.

5. **Kalman filter over-smoothing**: The broadcast Kalman filter may be smoothing out the ionospheric variation, and the dashboard may be reading the smoothed state rather than raw measurements.

### ⚠️ IMPACT ON TEC PIPELINE

The TEC enhancements implemented in the previous session (2026-02-14) depend on valid multi-frequency timing measurements:

- **TECEstimator** needs frequency-dependent D_clock variation (1/f² dispersion) — if D_clock is flat across frequencies, TEC estimation produces zero or noise
- **CarrierTECEstimator** needs valid carrier phase — this may be unaffected if the phase extraction is working even when timing is flat
- **IonoTomography** needs valid sTEC from multiple paths — garbage in, garbage out
- **VTECMapper** needs valid sTEC — same concern

**Do NOT revert the TEC enhancements.** The algorithms are correct; the problem is upstream in the measurement pipeline. Fix the measurements and the TEC pipeline will produce valid results.

### Service Inventory

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony (WatchdogSec=120) | `/var/log/hf-timestd/fusion.log` |
| `timestd-physics` | TEC estimation, L3 physics products (+ tomography, VTEC) | `/var/log/hf-timestd/physics.log` |
| `timestd-l2-calibration` | L2 adaptive calibration | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| `timestd-vtec` | GNSS VTEC estimation (optional) | journalctl |
| `grape-daily.timer` | Daily GRAPE processing trigger (01:00 UTC) | journalctl |
| `grape-daily.service` | `grape daily` — orchestrated pipeline with validation gates | journalctl |
| **radiod** | Real-time USB/FFT (CPU 8-15, uncontested L3 cache) | journalctl |

### Deployment

- **Git repo**: `/home/mjh/git/hf-timestd/`
- **Production install**: `/opt/hf-timestd/` (venv with non-editable `pip install`)
- **Deploy script**: `sudo scripts/update-production.sh [--pull]` — removes editable installs, copies package, syncs web-api/scripts/docs, updates systemd, restarts services, verifies
- **NEVER use `pip install -e`** in production — editable installs make the git repo live production code
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data root**: `/var/lib/timestd/`

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### TEC Pipeline Audit & Enhancement (2026-02-14, evening session)

Complete audit of 17 concerns across 5 files. Four new modules implemented and wired into `physics_fusion_service.py`:

1. **Bayesian TEC Estimator** (`tec_estimator.py` rewrite) — MAD-based 3σ outlier rejection, SNR weighting, N=2 confidence cap at 0.3, negative slope → None rejection, `propagation_mode` on TECResult, dead `high_precision_mode` removed
2. **Carrier-Phase dTEC** (`carrier_tec.py` new) — phase rate → Doppler → dTEC/dt → integrated TEC(t), anchored to group-delay absolute TEC
3. **Multi-Layer Tomography** (`iono_tomography.py` new) — E/F layer separation via constrained least squares, solar-dependent priors, condition monitoring
4. **VTEC Map Generator** (`vtec_mapper.py` new) — sTEC→vTEC mapping, IPP computation, 2D polynomial surface, IONEX output

Also fixed: inverted uncertainty weighting in fusion (concern #9), too-narrow TEC validation window 5-100→1-200 TECU (concern #10). 19/19 tests passing. Full details: `docs/changes/SESSION_2026_02_14_TEC_PIPELINE_AUDIT.md`.

**⚠️ NOTE:** These TEC modules are algorithmically correct but depend on valid upstream measurements. The dashboard anomaly discovered at the end of this session suggests the input data may be flat/invalid on most channels. The TEC pipeline will produce meaningful results only after the measurement pipeline is fixed.

### HamSCI 2026 Workshop Abstract (2026-02-14)

Presentation abstract written and committed: `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md`. Updated project description reflecting current system capabilities. Six forward-looking recommendations for TEC optimization. Recommendations 1, 2, 3, 6 implemented in the TEC session above.

### Documentation Conformance Audit (2026-02-14)

Broken links fixed in `docs/PHYSICS.md` and `docs/STATION_SETUP_GUIDE.md`. Audit execution status updated in `docs/DOCUMENTATION_AUDIT_2026_02_14.md`. Bulk archival of superseded docs completed.

### GRAPE Pipeline + PSWS Uploads (2026-02-14)

Full GRAPE pipeline operational. `grape daily` orchestrates: decimate → validate → spectrogram → validate → package → upload. Fixed `gap_samples` unit mismatch. Verified 9/9 channels processed and uploaded.

### Physics Memory + Operational Resilience (2026-02-14)

Physics service caches readers, bounds retry state. Memory safety limits in systemd. Logrotate `copytruncate`. Freshness monitoring. Deployment correspondence checklist.

### Signal Presence Gate Fix (2026-02-12)

`_check_signal_presence()` failed on shared channels (0.5% duty cycle). Fixed: use `edge_results` as primary signal presence indicator. tick_timing now writing 56 windows/min.

### Edge Ensemble (2026-02-12)

`TickEdgeDetector`: 50–57 ticks/min, ±2ms uncertainty. Recovery when minute marker fails.

### Phase Continuity + Doppler UI (2026-02-11)

Buffer-relative time fix for IQ mixer. Three-tier phase extraction. Cross-frequency discrimination gate. Phase/Doppler web dashboard.

### Earlier Fixes

- **CHU FSK Decoder** (2026-02-10): USB sidecar, quadrature demod, ring buffer timing
- **Pipeline Offset Calibration** (2026-02-09): Removed — radiod RTP timestamps authoritative
- **Timing Accuracy** (2026-02-06): Circular calibration, GPS ground truth, Kalman filter
- **HDF5 Crash Safety** (2026-02-06): SWMR eliminated — open-write-close
- **CHU Memory Leak** (2026-01-02): Extract 1.1s slice before demodulation
- **HDF5 File Lock Contention** (recurring): `locking=False` on all h5py.File() calls

---

## 🔑 KEY PRINCIPLES

1. **The GPSDO is a steel ruler.** Every sample has a known UTC timestamp via the RTP chain. The buffer exists only to find the tone. Once found, read the timestamp. That's the ToA.
2. **The ionosphere is the unknown.** Multi-frequency, multi-station geometry solves it — with or without GPS. GPS just removes one unknown (clock error).
3. **D_clock is the observable for TEC.** D_clock = observed_toa − predicted_geometric_delay. Any residual 1/f² pattern in D_clock across frequencies IS the ionospheric dispersion signal. Use D_clock, not raw ToA.
4. **Carrier phase gives dTEC; group delay gives absolute TEC.** Phase is 1000× more precise but ambiguous. Anchor phase-derived dTEC to group-delay absolute TEC at minute boundaries.
5. **Mode priors prevent contamination.** A 2F measurement mixed with 1F measurements corrupts the 1/f² fit. Use the propagation model's mode predictions to gate which measurements enter the estimator.
6. **17 paths = geometric diversity.** Different elevation angles and azimuths separate E-layer from F-layer contributions. Low-angle paths (BPM) are E-layer sensitive; high-angle paths (CHU) are F-layer dominated.
7. **Bandpass before correlate.** On shared channels, competing stations corrupt long-template correlations.
8. **Edge results are the signal presence indicator.** If the edge detector found ticks, signal is present.

---

## ✅ Success Criteria — This Session

1. **Root cause identified** — determine exactly why most channels show flat/constant D_clock on the 24h dashboard.
2. **Dashboard data pipeline verified** — confirm whether the problem is in the display layer (field name mismatch, wrong data plotted) or in the actual measurements.
3. **L2 data quality assessed** — directly inspect HDF5 files to determine if the raw measurements contain ionospheric variation.
4. **Fix implemented and verified** — whether it's a dashboard bug, tone detection issue, calibration problem, or signal chain issue.
5. **Diurnal variation visible** — after fix, the 24h dashboard should show clear diurnal timing-error variation on at least WWV 2.5/5/10 MHz and CHU channels.
6. **TEC pipeline validated** — with corrected measurements, verify that the TEC estimator produces physically reasonable TEC values that vary diurnally.
7. **No regressions** — existing tests still pass, fusion service still feeds chrony.
