# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: DOCUMENTATION CLEANUP

**Task:** Review, consolidate, and bring the project documentation up to date. The docs were written incrementally across many development sessions and now contain staleness, overlap, inconsistencies, and gaps. The goal is a coherent, accurate, and non-redundant documentation set that serves all four audiences: users, metrologists, ionospheric scientists, and software engineers.

---

## System Context

- **System:** hf-timestd v6.8.0 (February 27, 2026) — multi-broadcast HF time transfer and ionospheric measurement
- **Receiver:** GPSDO-locked RX888 SDR via KA9Q-radio, RTP-timestamped IQ at 24 kHz/channel
- **Stations:** WWV, WWVH, CHU, BPM — 9 frequencies, 17 logical broadcasts
- **Location:** EM38ww40pk (~38.9°N, ~92.1°W, central Missouri)
- **Git:** `/home/mjh/git/hf-timestd/` | **Production:** `/opt/hf-timestd/` | **Data:** `/var/lib/timestd/`
- **Deploy:** `sudo scripts/update-production.sh [--pull]`

---

## 1. Complete Feature/Capability Inventory

This is the authoritative list of what hf-timestd actually implements as of v6.8.0. The documentation should cover all of these. Features marked with their primary implementation file.

### 1.1 Signal Reception & Recording

| Capability | Implementation | Notes |
|---|---|---|
| **ka9q-python library integration** | `__init__.py`, `core/core_recorder_v2.py` | Python interface to Phil Karn's radiod — channel creation, RTP reception, resequencing, gap detection. This is the foundation: all RF data enters via ka9q-python `RadiodStream`. |
| **Multi-channel IQ recording** | `core/binary_archive_writer.py` | 9 channels × 24 kHz IQ, binary format with JSON metadata sidecars, optional zstd compression |
| **RTP timestamp preservation** | `core/buffer_timing.py` | GPS+PPS authoritative timestamps (~50 μs) from radiod's `GPS_TIME`/`RTP_TIMESNAP` |
| **Three-phase architecture** | Phase 1 (recorder), Phase 2 (analytics), Phase 3 (fusion) | Immutable raw data → derived products → system discipline |

### 1.2 Time Signal Detection & Analysis

| Capability | Implementation | Notes |
|---|---|---|
| **Tick edge detection** | `core/tick_edge_detector.py` | Quadrature matched filter for tick shapes (WWV 1000Hz, WWVH 1200Hz, CHU 300-cycle, BPM 10-cycle). Sub-sample parabolic interpolation. SNR-weighted robust median ensemble of up to 57 ticks/minute. Front-edge back-calculation inspired by ntpd refclock_wwv.c. |
| **Minute marker correlation** | `core/tone_detector.py` | Cross-correlation against known tone templates for second-0 marker |
| **Carrier phase Doppler** | `core/tick_edge_detector.py` | Phase slope across ticks → Doppler shift (Hz) per minute |
| **Multipath detection** | `core/tone_detector.py` | Peak broadening, secondary peaks, phase stability analysis |
| **D_clock extraction** | `core/metrology_engine.py` | `D_clock = T_system - T_UTC(NIST)` from tone arrival times minus modeled propagation delay |

### 1.3 Station Identification (Shared-Channel Discrimination)

| Capability | Implementation | Notes |
|---|---|---|
| **WWV/WWVH discrimination** | `core/wwvh_discrimination.py` | Weighted voting: BCD correlation, 1000/1200 Hz tone ratio, station ID tones (440/500/600 Hz), test signal detection |
| **BPM discrimination** | `core/bpm_discriminator.py` | Tick duration (10ms UTC vs 100ms UT1), minute gating (0-24/30-54 = UTC, 25-29/55-59 = UT1) |
| **Probabilistic discriminator** | `core/probabilistic_discriminator.py` | Logistic regression model for station ID confidence scoring |
| **Audio tone schedule awareness** | `core/tick_edge_detector.py` | WWV silent minutes {29,43-51,59}, WWVH silent {0,8-10,14-19,30} — avoids intermod contamination |

### 1.4 CHU FSK Time Code Decoding

| Capability | Implementation | Notes |
|---|---|---|
| **Bell 103 FSK demodulation** | `core/chu_fsk_decoder.py` | Mark=2225Hz, Space=2025Hz, 300 baud. Quadrature discriminator on audio, direct IQ demod on complex samples. |
| **Frame A decode** | `core/chu_fsk_decoder.py` | UTC day/hour/minute/second, BCD nibble-swapped, 5-byte redundancy check |
| **Frame B decode** | `core/chu_fsk_decoder.py` | DUT1 (UT1-UTC), year, TAI-UTC, DST status, bitwise NOT redundancy |
| **Multi-second consensus** | `core/chu_fsk_decoder.py` | Majority vote across 8 Frame A seconds for validated time |
| **FSK cross-validation** | `core/metrology_engine.py` | Frame A UTC sanity check vs RTP, TAI-UTC leap second watch, DUT1 tracking, BER-based confidence weighting |
| **CHU FSK listener** | `core/chu_fsk_listener.py` | Standalone service: USB-preset channels, ring buffers, health monitoring |

### 1.5 WWV/WWVH BCD Subcarrier Decoding

| Capability | Implementation | Notes |
|---|---|---|
| **100 Hz BCD subcarrier** | `core/correlator_bank.py` | BCD time code extraction from WWV/WWVH |
| **Station identification** | `core/wwvh_discrimination.py` | BCD correlation peaks distinguish WWV from WWVH |

### 1.6 WWV/WWVH Scientific Test Signal Analysis

| Capability | Implementation | Notes |
|---|---|---|
| **Test signal detection** | `core/wwv_test_signal.py` | Minutes :08 (WWV) and :44 (WWVH) |
| **Multi-tone power measurement** | `core/wwv_test_signal.py` | 2, 3, 4, 5 kHz tone powers |
| **Frequency Selectivity Score (FSS)** | `core/wwv_test_signal.py` | `10×log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))` — path signature |
| **Chirp delay spread** | `core/wwv_test_signal.py` | Matched filter pulse compression for multipath characterization |
| **Transient detection** | `core/wwv_test_signal.py` | Noise segment comparison for solar flare/SID events |

### 1.7 Ionospheric Science Products

| Capability | Implementation | Notes |
|---|---|---|
| **Differential TEC (dTEC)** | `core/carrier_tec.py` | Carrier-phase integrated dTEC — see Section 2 below for detailed discussion |
| **Group-delay TEC** | `core/tec_estimator.py` | 1/f² dispersion fit across frequencies — noisy but absolute-capable in principle |
| **Local GNSS VTEC** | `core/gnss_tec.py`, `scripts/live_vtec.py` | u-blox ZED-F9P dual-frequency GPS, ~1 Hz cadence, DCB-corrected, ±1 TECU |
| **GNSS VTEC anchoring** | `core/physics_fusion_service.py` | Anchors carrier-phase dTEC to absolute scale using local GNSS overhead TEC |
| **Propagation mode identification** | `core/propagation_mode_solver.py` | GW, 1E, 1F2, 2F2, 3F2 mode assignment from delay matching |
| **Sporadic-E detection** | `core/propagation_mode_solver.py` | SNR anomaly + mode change detection + foEs estimation |
| **TID detection** | `core/tid_detector.py` | Cross-path timing residual correlation → TID velocity, direction, period |
| **Scintillation indices** | `core/advanced_signal_analysis.py` | S4 (amplitude) and σ_φ (phase) per ITU-R P.531 |
| **Ionospheric reanalysis** | `core/ionospheric_reanalysis.py` | Hourly offline physics-based mode re-validation using solar zenith / Chapman MUF |
| **D-layer absorption** | Multi-frequency SNR analysis | Frequency-dependent absorption pattern, SID detection |

### 1.8 Propagation Modeling

| Capability | Implementation | Notes |
|---|---|---|
| **HF propagation model** | `core/propagation_model.py` | Multi-mode delay prediction with Ne(h) integration |
| **IonoDataService** | `core/iono_data_service.py` | WAM-IPE (NOAA S3) + GIRO ionosonde fetch, cache, interpolation |
| **IRI-2020 integration** | `core/ionospheric_model.py` | International Reference Ionosphere as Tier 2 fallback |
| **Parametric/climatological model** | `core/propagation_model.py` | Diurnal/seasonal fallback with DUT1-corrected solar time |
| **IONEX VTEC maps** | `core/iono_data_service.py` | NASA CDDIS Global Ionosphere Maps |
| **Arrival pattern matrix** | `core/arrival_pattern_matrix.py` | Physics-validated arrival windows per broadcast |

### 1.9 Multi-Broadcast Fusion & Time Transfer

| Capability | Implementation | Notes |
|---|---|---|
| **Per-broadcast Kalman filters** | `core/broadcast_kalman_filter.py` | 17 independent filters for temporal continuity |
| **Weighted least squares fusion** | `core/multi_broadcast_fusion.py` | BLUE estimator with inverse-variance weighting |
| **Dual Kalman architecture** | `core/multi_broadcast_fusion.py` | L1 (geometric) and L2 (physics-corrected) independent filters |
| **Global differential solve** | `core/multi_broadcast_fusion.py` | All-channel simultaneous solution |
| **Chrony SHM integration** | `core/chrony_shm.py` | TSL1 + TSL2 refclocks for system clock discipline |
| **Steel Ruler metrology** | `core/multi_broadcast_fusion.py` | GPSDO treated as fixed reference; drift frozen at 0 after convergence |
| **NTP-based bootstrap** | `core/bootstrap_state.py` | Fast RTP-to-UTC calibration using GPSDO wallclock (~2 min to LOCKED) |
| **CHU FSK cross-validation** | `core/metrology_engine.py` | Frame A UTC sanity check, TAI-UTC leap second hold, BER confidence weighting |
| **Leap second awareness** | `core/multi_broadcast_fusion.py` | Kalman hold during TAI-UTC transitions detected via CHU FSK |
| **Allan deviation tracking** | `core/multi_broadcast_fusion.py` | Real-time ADEV at τ=60s for fusion quality monitoring |

### 1.10 HDF5 Data Model

| Capability | Implementation | Notes |
|---|---|---|
| **Schema-versioned HDF5** | `interfaces/schemas/` | JSON Schema definitions for all data products |
| **Crash-safe writes** | `io/hdf5_writer.py` | Open-write-close per measurement; `locking=False` for concurrent access |
| **L1 metrology** | `io/hdf5_writer.py` | Tone detections, D_clock, SNR, uncertainty |
| **L2 calibration** | `core/l2_calibration_service.py` | Physics-corrected timing |
| **L3 fusion** | `core/multi_broadcast_fusion.py` | Fused timing, ADEV, grade |
| **L3 dTEC** | `core/physics_fusion_service.py` | Carrier-phase dTEC with GNSS anchor |
| **L3C propagation stats** | `core/ionospheric_reanalysis.py` | Physics-validated mode statistics |
| **CHU FSK data** | `io/hdf5_writer.py` | Decoded time, DUT1, TAI-UTC, year, timing offset |
| **Tick timing** | `io/hdf5_writer.py` | Per-tick D_clock, Doppler, SNR, ensemble statistics |
| **GNSS VTEC** | `scripts/live_vtec.py` | ~86K records/day, per-satellite and aggregate |

### 1.11 HamSCI GRAPE Data Product

| Capability | Implementation | Notes |
|---|---|---|
| **10 Hz IQ decimation** | `grape/decimation.py`, `grape/decimation_pipeline.py` | Decimate 24 kHz raw IQ to 10 Hz for all 9 channels |
| **Digital RF packaging** | `grape/packager.py` | PSWS/wsprdaemon-compatible DRF format for upload |
| **GRAPE upload** | `grape/uploader.py` | Automated daily upload to HamSCI PSWS network |
| **Spectrogram generation** | `grape/spectrogram.py` | Daily spectrograms from decimated data |

### 1.12 Web UI & API

| Capability | Implementation | Notes |
|---|---|---|
| **FastAPI dashboard** | `web-api/` | Real-time monitoring (port 8000) |
| **Metrology page** | `web-api/static/metrology.html` | D_clock, ADEV, per-channel status, CHU FSK decode |
| **dTEC page** | `web-api/static/dtec.html` | Carrier-phase dTEC with GNSS anchoring |
| **Ionogram page** | `web-api/static/ionogram.html` | Virtual ionogram from propagation mode data |
| **GRAPE page** | `web-api/static/grape.html` | Spectrogram and data quality |
| **Propagation conditions** | `web-api/routers/propagation.py` | MUF, mode stats, SNR by frequency |
| **Custom date range** | All time-selector pages | Browse any historical day |

### 1.13 Infrastructure & Operations

| Capability | Implementation | Notes |
|---|---|---|
| **systemd services** | `systemd/` | 10+ services with dependency management |
| **Production installer** | `scripts/install.sh` | Creates timestd user, venv, config, services |
| **CPU affinity** | `scripts/setup-cpu-affinity.sh` | Isolates radiod and recorder on dedicated cores |
| **Log rotation** | `config/logrotate-timestd` | Daily rotation with 14-day retention |
| **Freshness monitoring** | `config/cron.d/timestd-freshness-monitor` | Alerts on stale data |
| **Tiered storage** | `core/core_recorder_v2.py` | Hot (SSD) → cold (HDD) migration |

---

## 2. TEC: The Evolution from Absolute to Differential (Critical Topic for Documentation)

### 2.1 The Problem with "Absolute TEC" from HF Group Delay

Early documentation (and some current text in PHYSICS.md §3.1 and ARCHITECTURE.md) describes the system as measuring "TEC" as if it were a reliable absolute quantity. This is misleading. The group-delay TEC method — fitting D_clock vs 1/f² across frequencies — is **fundamentally limited** for this system:

**Why absolute TEC from HF group delay is unreliable here:**

1. **Model contamination:** D_clock already has the propagation model delay subtracted. Any error in the propagation model (layer height, mode assignment) directly contaminates the 1/f² fit. The TEC estimate is measuring *model error* as much as *actual TEC*.

2. **Mode mixing:** Different frequencies may propagate via different modes (e.g., 5 MHz via 1F, 10 MHz via 2F). The 1/f² assumption requires all frequencies to traverse similar ionospheric paths. Mode mixing violates this.

3. **Low frequency diversity:** With only 4-6 distinct frequencies (some shared, some unusable at night), the 1/f² fit has few degrees of freedom. The SNR of the TEC estimate is ~0.13 (essentially noise) as documented in PHYSICS.md §8.4.

4. **Single receiver:** No spatial resolution means the "TEC" is a path-integrated slant quantity that varies per station and per mode. It is not comparable to standard VTEC without model-dependent mapping.

### 2.2 The Move to Differential TEC (dTEC)

The system now focuses on **carrier-phase differential TEC (dTEC)**, which is the *rate of change* of TEC along each HF path. This is a fundamentally more robust measurement:

**Why dTEC works where absolute TEC fails:**

1. **No model dependence:** dTEC is derived from the carrier phase slope, not from absolute arrival times. The phase progression is a direct observable — it doesn't depend on knowing the absolute propagation delay.

2. **High precision:** Carrier-phase measurement has ~6 mTECU/min precision (vs ~5-10 TECU noise on group-delay absolute TEC). Three orders of magnitude better for rate measurement.

3. **Physical meaning:** dTEC tracks ionospheric *dynamics* — TIDs, sunrise/sunset transitions, storm onset, substorm injections. These are the scientifically valuable signals.

4. **GNSS anchoring:** Local GNSS VTEC from the ZED-F9P provides an absolute DC offset (±1 TECU), converting the relative dTEC into a quasi-absolute product with `ANCHORED_GNSS` status. This is the best of both worlds: absolute level from GNSS + high-precision dynamics from HF carrier phase.

### 2.3 What Would Be Needed for True Absolute HF TEC

For completeness, here is what the system *cannot currently provide* and what would be needed:

1. **Calibrated absolute group delay per path:** Requires knowing the true ionospheric layer heights and mode assignments with high accuracy. This is circular — you need TEC to know the layer heights.

2. **Ionosonde co-location:** A local ionosonde (e.g., Digisonde) providing real-time foF2 and hmF2 would break the circularity. The system could then calibrate its propagation model against actual measured layer parameters, making the group-delay TEC residual meaningful.

3. **Multi-receiver network:** Two or more receivers at different locations would enable differential group delay measurements that cancel common-mode model errors. This is analogous to differential GPS.

4. **Absolute phase calibration:** If the transmitter-to-receiver phase path could be absolutely calibrated (e.g., via a dedicated calibration signal), the carrier-phase TEC could become absolute without GNSS anchoring.

5. **Better frequency diversity:** More frequencies (ideally 8+) spanning a wider range would improve the 1/f² fit. The current 4-6 usable frequencies are marginal.

### 2.4 Documentation Fix Needed

The following documents need TEC-related revisions:

| Document | Section | Issue |
|---|---|---|
| **PHYSICS.md §3.1** | "Total Electron Content (TEC) ✅" | Implies reliable absolute TEC. Should clarify that group-delay TEC is noisy (SNR ~0.13) and the primary product is now carrier-phase dTEC anchored by GNSS VTEC. |
| **PHYSICS.md §5.4** | "TEC Re-Estimation from Cleaned Data" | Reanalysis TEC is better than real-time but still limited by the fundamental issues above. Should state limitations explicitly. |
| **PHYSICS.md §8** | "GNSS-VTEC Integration" | This section is good but should be elevated in prominence — it's now the *primary* TEC capability, not an enhancement. |
| **ARCHITECTURE.md** | "TEC estimation" in Key Capabilities | Should say "dTEC" not "TEC". |
| **ARCHITECTURE.md** | Phase 3 description | "Per-Station TEC Validation (1/f² physics check)" — this still works as a consistency check but should not imply absolute TEC. |
| **README.md** | Key Capabilities list | Should mention dTEC, GNSS anchoring. |
| **METROLOGY.md** | Various | Check for absolute TEC claims. |

---

## 3. Documentation Landscape: Staleness, Overlap, and Gaps

### 3.1 Document Inventory and Status

| Document | Purpose | Last Updated | Status |
|---|---|---|---|
| **README.md** | User-facing overview, quick start | Recent | ⚠️ Feature list incomplete (no mention of Grape, BCD decode, test signals, GNSS VTEC); version string in `__init__.py` says 3.2.0 while README says 6.8.0 |
| **ARCHITECTURE.md** | System design philosophy ("The Why") | Feb 26, 2026 | ⚠️ Mostly current but TEC claims need revision; ka9q-python section is good |
| **METROLOGY.md** | RTP-to-UTC calibration, uncertainty budgets | Feb 17, 2026 | ⚠️ May have absolute TEC language; needs review |
| **PHYSICS.md** | Ionospheric science capabilities | Feb 26, 2026 | ⚠️ Comprehensive but TEC §3.1 is misleading; §8 GNSS VTEC should be more prominent; some implementation file references may be stale |
| **TECHNICAL_REFERENCE.md** | Algorithms, data formats | Unknown | ❓ Needs review for staleness |
| **CONTEXT.md** | AI session bootstrapping | Stale | ❌ Points to v6.7 deployment tasks that were completed months ago. Needs rewrite or removal. |
| **INSTALLATION.md** | Setup guide | Unknown | ❓ Needs review |
| **DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md** | Production deployment gates | Unknown | ❓ Needs review |
| **docs/changes/** | 30+ session notes | Various | 📁 Archive; useful as history but not canonical |
| **docs/design/** | Design docs | Various | 📁 Some may be superseded |
| **docs/features/** | Feature-specific docs | Various | 📁 Some may be superseded by PHYSICS.md |
| **.windsurf/contracts/** | AI agent contracts | Recent | ✅ Current |

### 3.2 Known Overlaps

1. **ARCHITECTURE.md vs PHYSICS.md:** Both describe propagation modes, TEC, Doppler, multipath. ARCHITECTURE.md should focus on *design decisions* (why we model propagation this way) while PHYSICS.md should cover the *science* (what measurements mean physically). Currently there's significant duplication.

2. **ARCHITECTURE.md vs TECHNICAL_REFERENCE.md:** Both describe the three-phase architecture and data flow. Need to verify they're consistent and non-redundant.

3. **PHYSICS.md Appendix B vs actual codebase:** The file-to-capability mapping should be verified — some files may have been renamed or consolidated.

4. **README.md Key Capabilities vs ARCHITECTURE.md Key Capabilities:** These lists differ and neither is complete.

### 3.3 Known Gaps (Not Documented or Under-Documented)

1. **Feature inventory:** No single document lists all capabilities. The inventory in Section 1 above should become part of the documentation (README.md or a dedicated FEATURES.md).

2. **ka9q-python integration:** ARCHITECTURE.md §4 has a good overview, but there's no documentation of the actual API surface we use (RadiodStream, RadiodControl, discover_channels, rtp_to_wallclock, etc.) or how to troubleshoot ka9q-python issues.

3. **GRAPE product:** The HamSCI GRAPE data product (10 Hz decimation, DRF packaging, PSWS upload) is an entire subsystem (`src/hf_timestd/grape/`) with no documentation outside code comments.

4. **HDF5 data model:** The schema files exist in `interfaces/schemas/` but there's no human-readable guide to what data products exist, what fields they contain, and how to consume them.

5. **Web UI:** No documentation of the dashboard pages, what they show, or how to use them.

6. **CHU FSK cross-validation (new):** The four integrations added 2026-02-27 (Frame A UTC check, TAI-UTC leap second watch, DUT1→propagation model, BER confidence weighting) are documented in `docs/changes/SESSION_2026_02_27_FSK_CROSS_VALIDATION.md` but not yet in the canonical docs.

7. **Operational runbook:** No documented procedures for common operational tasks (restart after crash, data recovery, interpreting alerts, adding a new channel).

### 3.4 Recommended Documentation Structure

After cleanup, the canonical documentation should be:

| Document | Audience | Scope |
|---|---|---|
| **README.md** | Everyone | Overview, complete feature list, quick start, links |
| **INSTALLATION.md** | Operators | Detailed setup, prerequisites, configuration |
| **ARCHITECTURE.md** | Engineers | Design decisions, data flow, service architecture |
| **METROLOGY.md** | Metrologists, time nuts | Timing methodology, uncertainty budgets, traceability |
| **PHYSICS.md** | Scientists | Ionospheric measurements, validation, science products |
| **TECHNICAL_REFERENCE.md** | Engineers | Algorithms, data formats, API reference |
| **CONTEXT.md** | AI agents | Current system state for session bootstrapping (should be auto-maintained or removed) |

---

## 4. Specific Cleanup Tasks

### 4.1 High Priority

1. **Fix TEC language everywhere** (see §2.4 table above)
2. **Add complete feature inventory** to README.md (use §1 above as source)
3. **Fix `__init__.py` version** — says 3.2.0, should be 6.8.0
4. **Rewrite or remove CONTEXT.md** — currently points to v6.7 deployment that's months old
5. **Verify PHYSICS.md Appendix B** file references against actual codebase

### 4.2 Medium Priority

6. **De-duplicate ARCHITECTURE.md vs PHYSICS.md** — clarify which owns what
7. **Add GRAPE documentation** — even a brief section in README + ARCHITECTURE
8. **Add HDF5 data model guide** — list all data products and their fields
9. **Review docs/features/** — which are superseded by PHYSICS.md?
10. **Review docs/design/** — which are superseded by ARCHITECTURE.md?

### 4.3 Lower Priority

11. **Add operational runbook** (or at least a FAQ)
12. **Web UI documentation** (at least a screenshot tour)
13. **ka9q-python API surface documentation**
14. **Archive obsolete docs/changes/ session notes** older than 3 months

---

## 5. CHU FSK Signal Specification (Reference)

CHU broadcasts a **Bell 103** compatible FSK time code during **seconds 31–39** of each minute:

| Parameter | Value |
|---|---|
| Mark frequency | 2225 Hz (logic 1) |
| Space frequency | 2025 Hz (logic 0) |
| Baud rate | 300 bps |
| Frame format | 1 start + 8 data + 1 parity (even) + 1 stop = 11 bits/byte |
| Bytes per second | 10 (5 data + 5 redundancy) |

**Frame A** (seconds 32–39): `6d dd hh mm ss` (BCD, nibble-swapped, repeated as bytes 5–9)
**Frame B** (second 31): `xz yy yy tt aa` (DUT1, year, TAI-UTC, DST; bytes 5–9 = bitwise NOT of 0–4)

---

## 6. Diagnostic Commands

```bash
# System status
systemctl list-units 'timestd-*' --no-pager

# Check fusion convergence
tail -20 /var/log/hf-timestd/fusion.log | grep "fused\|grade\|ADEV"

# Check CHU FSK decoding
for f in /dev/shm/timestd/fsk_results/CHU_*.json; do echo "=== $(basename $f) ==="; python3 -m json.tool "$f" 2>/dev/null | head -15; done

# Check HDF5 data products exist
ls /var/lib/timestd/phase2/*/metrology/ | head -20
ls /var/lib/timestd/phase2/*/broadcast:fsk/ 2>/dev/null

# Check GNSS VTEC
ls -lt /var/lib/timestd/data/gnss_vtec/ | head -5

# Check GRAPE uploads
ls /var/lib/timestd/upload/ 2>/dev/null

# Deploy after changes
sudo scripts/update-production.sh
```

---

## 7. Success Criteria for Documentation Cleanup

1. **TEC language corrected:** All docs accurately describe dTEC as primary product; absolute TEC limitations stated; GNSS anchoring prominent
2. **Feature inventory present:** README.md (or linked document) has complete, categorized capability list
3. **No stale session-specific content** in CONTEXT.md
4. **No internal contradictions** between README, ARCHITECTURE, PHYSICS, METROLOGY
5. **PHYSICS.md file references verified** against actual codebase
6. **GRAPE product documented** (at least in README + ARCHITECTURE)
7. **Version string consistency** across `__init__.py`, README, pyproject.toml
8. **All changes committed and pushed**
