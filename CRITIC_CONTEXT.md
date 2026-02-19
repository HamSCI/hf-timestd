# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: PHYSICS PIPELINE CRITICAL REVIEW

**Task:** Scrutinize the physics pipeline for errors, inconsistencies, circular reasoning, clarity of data model, and missed opportunities. Trace data from raw observable through each processing stage. Produce a ranked list of findings with severity and recommended action. Do not fix cosmetically — find structural problems.

**Focus:** Ionospheric physics in RTP mode (GPS+PPS, Lock Tier L6). UTC reconstruction is out of scope.

---

## System Context

- **Receiver:** GPSDO-locked RX888 SDR via KA9Q-radio, RTP-timestamped IQ at 24 kHz/channel
- **Stations:** WWV (2.5–25 MHz), WWVH (2.5–15 MHz), CHU (3.33, 7.85, 14.67 MHz), BPM (2.5–15 MHz)
- **Location:** EM38 (~38.9°N, ~92.1°W, central Missouri)
- **Git:** `/home/mjh/git/hf-timestd/` | **Production:** `/opt/hf-timestd/` | **Data:** `/var/lib/timestd/`
- **Deploy:** `sudo scripts/update-production.sh [--pull]`

| Service | Purpose | Log |
|---|---|---|
| `timestd-metrology` | IQ → L1 measurements | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-l2-calibration` | L1 → L2 calibrated timing | journalctl |
| `timestd-physics` | L2 → L3 TEC/dTEC/VTEC | `/var/log/hf-timestd/physics.log` |
| `timestd-web-api` | REST API + dashboard (port 8000) | journalctl |

---

## Data Pipeline and Field Semantics (Read First)

```
L1  timing_error_ms      = observed_ToA − model_expected_delay
                           HDF5: phase2/{CHANNEL}/metrology/

L2  clock_offset_ms      = same as L1 timing_error_ms (mislabeled — it is a residual, not a clock offset)
    propagation_delay_ms = model path delay (~10ms CHU, ~4ms WWV, ~24ms WWVH, ~39ms BPM)
    raw_arrival_time_ms  = clock_offset_ms + propagation_delay_ms  ← NOT an absolute ToA
    tof_kalman_ms        = ALL NaN in production
                           HDF5: phase2/{CHANNEL}/clock_offset/

L3  tec_tecu             = group-delay TEC fit (see F1 — mostly noise)
    t_vacuum_error_ms    = TEC-fit intercept = ionosphere-free D_clock (metrologically useful)
    vtec_tecu            = ALL NaN in production (mapper runs but field not written to records)
    dtec_rate_tecu_per_s = carrier-phase dTEC — 250K records/day — the viable physics product
                           HDF5: phase2/science/tec/, phase2/science/dtec/
```

`physics_fusion_service._read_l2_slice()` prefers `tof_kalman_ms`, falls back to `raw_arrival_time_ms`. Since `tof_kalman_ms` is all NaN, the fallback is always used. `raw_arrival_time_ms` is a residual + model delay, not a raw ToA — feeding it to the TEC estimator as `toa_ms` is conceptually wrong.

---

## Pre-Verified Findings (Confirmed 2026-02-19 by HDF5 Inspection)

### F1 — Group-delay TEC is below the noise floor *(CRITICAL)*

| Station | Freq range | Signal @ 40 TECU | Noise 1σ | SNR |
|---|---|---|---|---|
| CHU | 3.33–14.67 MHz | 0.46 ms | 37 ms | **0.01** |
| WWV | 2.5–25 MHz | 0.85 ms | 6.5 ms | **0.13** |
| WWVH/BPM | 2.5–15 MHz | ~0.7 ms | ~5 ms | **~0.14** |

The noise is propagation model error (inter-minute mode/condition variability), not instrument noise. The TEC estimator aggregates over a 5-minute lookback window mixing different propagation conditions. It cannot recover a sub-ms dispersion signal. The 11.7K TEC records today are noise fits — 71% have confidence < 0.5. The viable path is carrier-phase dTEC (already 250K records/day).

### F2 — CHU has a ~76 ms systematic offset in `clock_offset_ms` *(STRUCTURAL)*

All three CHU channels: `clock_offset_ms ≈ −76 ms`, `raw_arrival_time_ms ≈ −66 ms`. Model predicts ~10 ms for Ottawa→Missouri. This −76 ms systematic is not ionospheric. WWV ≈ +3 ms, WWVH ≈ +22 ms, BPM ≈ +38 ms — all different, suggesting per-station propagation model errors.

### F3 — `vtec_tecu` all NaN; VTEC RMS=0.00 is suspicious *(BUG)*

Logs show "VTEC map: N IPPs, RMS=0.00 TECU, conf=1.00" every minute but `vtec_tecu` is all NaN in HDF5. RMS=0.00 with conf=1.00 suggests trivial/degenerate solution. Field not written to per-station TEC records.

### F4 — Propagation mode labels are unreliable *(KNOWN)*

BPM 2.5 MHz labeled "4F2" at 07:00 UTC (nighttime Missouri). Mode solver appears purely geometric — no physical constraints (MUF, absorption, layer height).

### F5 — `docs/PHYSICS.md` and HamSCI abstract overstate capabilities *(DOCUMENTATION)*

PHYSICS.md claims ✅ for TEC, scintillation, TIDs. Live system contradicts several claims. `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md` makes public claims needing honest validation.

---

## What Actually Works (Verified 2026-02-19)

| Product | Status |
|---|---|
| L1 timing measurements | ✅ ~15K/day |
| L2 clock_offset_ms | ✅ Real residuals (systematic offsets per station — see F2) |
| SNR per broadcast | ✅ Real, frequency- and time-varying |
| Carrier-phase dTEC | ✅ 250K records/day, anchored to group-delay TEC |
| IONEX output | ✅ Written per minute |
| All-arrivals (multi-path) | ✅ NEW — `all_arrivals/` HDF5; CHU_7850: 374 rows/min, 258 secondary |
| GRAPE spectrograms | ✅ 9/9 channels uploading to PSWS |
| Group-delay TEC | ❌ Below noise floor |
| vtec_tecu | ❌ All NaN |
| tof_kalman_ms | ❌ All NaN |
| Scintillation indices | ❓ Not verified |

---

## Key Files for Review

| File | What to scrutinize |
|---|---|
| `src/hf_timestd/core/physics_fusion_service.py` | `_read_l2_slice()` input semantics; TEC estimator inputs; vtec_tecu write path |
| `src/hf_timestd/core/tec_estimator.py` | Whether inputs are raw ToA or residuals; confidence calibration |
| `src/hf_timestd/core/l2_calibration_service.py` | `raw_arrival_time_ms` construction; field naming; systematic offsets |
| `src/hf_timestd/core/propagation_mode_solver.py` | Physical constraints vs pure geometry |
| `src/hf_timestd/core/vtec_mapper.py` | Why RMS=0.00; why vtec_tecu not written to per-station records |
| `src/hf_timestd/core/carrier_tec.py` | dTEC anchor quality; how group-delay TEC is used as anchor |
| `web-api/routers/propagation.py` | Does timeline expose D_clock? TEC endpoint freshness |
| `web-api/static/physics.html` | SNR plot coloring (by station, not frequency — misses D-layer story) |
| `docs/PHYSICS.md` | Accuracy audit — downgrade ✅ claims that contradict live system |
| `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md` | Public claims — honest validation |
