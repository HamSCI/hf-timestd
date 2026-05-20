# METROLOGY CONTRACT — hf-timestd

**Version:** 1.2.0
**Last Updated:** 2026-05-20
**Status:** Active — evolves with implementation
**Last refresh:** 2026-05-20 — re-reconciled after the metrology/physics review remediation pass (S2, S3, S4 + all P-H + all P-M + all M-M). The §5 Known Deviations from the 2026-05-17 snapshot have been worked through: D3 (M-C1), D4 (M-C2), D6 (M-M18) are resolved; D1 (M-H2) and D5 are still open with status notes below. See `docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md` for the full audit.

---

## 1. Goal

Ensure the metrology pipeline (Phase 2) extracts **accurate, traceable, uncertainty-quantified timing measurements** from raw IQ data, suitable for both ionospheric science (RTP mode) and UTC recovery (Fusion mode).

### Performance Objectives

- **Tick detection rate**: ≥50 ticks/minute/station at ≥10 dB SNR (57 max for WWV/WWVH)
- **Ensemble timing uncertainty**: ±0.008 ms (CHU at 20+ dB), ±0.5–2 ms (WWV/WWVH typical)
- **Doppler extraction**: meaningful fit requires ≥5 ticks spanning ≥5 seconds
- **False positive rate**: <1% after physics validation gate (arrival_matrix; nominally ±15 ms — note per-station uncertainty floors widen this in practice, see §5 D2)
- **Bootstrap convergence**: ACQUIRING → LOCKED within ~2 minutes (with NTP)
- **Processing latency**: raw buffer → L1/L2 HDF5 within 90 seconds
- **Station discrimination accuracy**: >70% correct on shared frequencies (2.5, 5, 10, 15 MHz)

### Deliverable Products

| Product | Source Module | Key Fields |
|---------|-------------|------------|
| D_clock (ms) | `tick_edge_detector.py` | AM-domain front-edge ensemble, UTC-referenced via `buffer_timing` |
| Doppler (Hz) | `tick_edge_detector.py` | Carrier phase slope across minute (IQ mixed at tick freq) |
| SNR (dB) | `tick_edge_detector.py` | Per-tick matched filter SNR |
| Carrier phase (rad) | `tick_edge_detector.py` | Per-tick IQ phase at tone frequency |
| Station ID | `metrology_engine.py` | Cross-freq gate + BCD + tone power voting |
| CHU FSK decode | `chu_fsk_decoder.py` | UTC time, DUT1, TAI-UTC, confidence |

### Verification Steps

1. `TickEdgeDetector` is the **single source** for all `tick_timing` HDF5 fields — no other module writes to this product
2. Production logs show `tick analysis` lines with `n_edges ≥ 50` for CHU channels at good SNR
3. Cross-check Δ between minute marker correlation and tick ensemble < 2 ms when both available
4. `chronyc sources` shows TSL1/TSL2 with reachability > 0 and offset within ±5 ms of GPS reference
5. `pytest tests/` — all metrology tests pass, including `test_carrier_phase_continuity` (σ < 0.3 rad)

---

## 2. Constraints

### RTP Timestamp Authority

- RTP timestamps from radiod are **authoritative** (~50 μs accuracy via GPS+PPS)
- UTC reconstruction: `utc = gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate`
- `GPS_TIME` and `RTP_TIMESNAP` are in the **same counter space** (input_sample_index / decimation) — no pipeline offset correction needed
- Wall clock time is **derived** from RTP timestamps, never vice versa
- Sample count invariant: 1,440,000 samples per minute at 24 kHz, exactly

### TickEdgeDetector (Primary Timing Source)

- Inspired by ntpd `refclock_wwv.c` Type 36 driver
- **Tick templates**: WWV 5ms/1000Hz, WWVH 5ms/1200Hz, CHU 300ms/1000Hz, BPM 10ms/1000Hz
- 800–1400 Hz bandpass rejects 100 Hz BCD, 440/500/600 Hz audio tones
- Front-edge back-calculation: subtract half tick duration from correlation peak center
- Sub-sample parabolic interpolation (~5 μs precision)
- SNR-weighted robust median ensemble with MAD outlier rejection
- Carrier phase: mix IQ at tone frequency over tick duration, angle of mean phasor
- Phase must use **buffer-relative time** (not window-relative) to avoid phase jumps

### Search Window Constraints

- Search window: primary path is **adaptive**, derived from the `ArrivalPatternMatrix` per-broadcast 3σ uncertainty; the formula `max(50, min(100, tone_duration_sec * 625))` ms is the fallback when no adaptive estimate is available
- Noise exclusion zone = full template length (not half)
- Physics validation gate: arrival_matrix ±15 ms nominal — **do not widen or remove** (currently widened per-station via uncertainty floors; see §5 D2)
- Cross-frequency gate: MIN_FREQ_ADVANTAGE_DB = 3.0 dB (rejects 1000↔1200 Hz cross-response)

### Station-Specific Handling

| Station | Modulation | Tick Duration | Special Handling |
|---------|-----------|---------------|-----------------|
| WWV | AM DSB | 5 ms | Silent minutes: {29, 43–51, 59} |
| WWVH | AM DSB | 5 ms | Silent minutes: {0, 8–10, 14–19, 30} |
| CHU | USB + preserved carrier | 300 ms | H3E sideband filter: 74 ms group delay correction |
| BPM | AM DSB | 10 ms (UTC), 100 ms (UT1) | UT1 minutes (25–29, 55–59) filtered out |

### Kalman Filter ("Steel Ruler")

Values below are the L3 fusion Kalman (`multi_broadcast_fusion._kalman_update`). The per-broadcast `BroadcastKalmanFilter` carries its own separate `q_tof`/`q_doppler` per station.

| Parameter | Value (current code) | Rationale |
|-----------|----------------------|-----------|
| Q (Offset) | 0.01 ms²/min | Allows tracking real measurements |
| Q (Drift) | 1e-8 (ms/min)²/min | GPSDO drift is negligible — **value lacks a documented derivation; see §5** |
| R (Measurement) | adaptive: `measurement_uncertainty²` | Rejects ionospheric turbulence (was a flat 30.0 ms in the prior contract) |
| Drift clamping | 0.0 after convergence | GPSDO prevents drift accumulation |

### Dual Kalman Architecture

- **TSL1** (SHM 0): L1 Kalman — geometric fallback, no ionospheric model
- **TSL2** (SHM 1): L2 Kalman — full ionospheric correction via propagation model
- Each feed has **independent** Kalman state (separate `kalman_state_l2` arrays)
- Discontinuity filter threshold: `max(10, 3 * uncertainty_ms)` — must not permanently latch

### Dependencies

- `ka9q-python` for RTP reception and channel management
- `numpy`, `scipy` for DSP (matched filtering, correlation, bandpass)
- `h5py` for HDF5 output — SWMR model (writers keep the daily file open with `swmr_mode=True` and flush; readers open `swmr=True`, `libver='latest'`)
- `ArrivalPatternMatrix` for physics-based expected arrival predictions
- `HFPropagationModel` for frequency-dependent delay predictions
- `IonoDataService` for real-time ionospheric parameters

---

## 3. Format

### Tick Timing HDF5 Record

Schema: `l2_tick_timing_v1.json` (`schema_version` 2.0.0; `processing_version` is a free-form software-version string, not pinned by the schema)

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `d_clock_ms` | float64 | ms | Ensemble timing residual from expected arrival |
| `d_clock_uncertainty_ms` | float64 | ms | MAD of per-tick timing errors |
| `d_clock_source` | string | — | Always `edge_ensemble` |
| `doppler_hz` | float64 | Hz | Carrier phase slope across minute |
| `doppler_uncertainty_hz` | float64 | Hz | From linear fit covariance |
| `mean_snr_db` | float64 | dB | Mean per-tick matched filter SNR |
| `valid_windows` | int | — | Ticks with valid detections |
| `total_windows` | int | — | Total seconds attempted |
| `ensemble_n_edges` | int | — | Tick edges used in ensemble |
| `n_clean` | int | — | Ticks from intermod-free minutes |

### Log Format

- Per-minute tick analysis: `INFO: tick analysis: {channel} {station} n_edges={N} snr={X}dB d_clock={Y}ms`
- Edge cross-check: `INFO: edge cross-check: Δ={X}ms (minute_marker vs ensemble)`
- Bootstrap state transitions: `INFO: Bootstrap: {channel} {old_state} → {new_state}`
- Detection failures: `WARNING: No ticks detected for {channel} (SNR below threshold)`

### Uncertainty Budget (ISO GUM)

Every fused D_clock measurement carries a combined uncertainty:

```
u_combined = √(u_cramer_rao² + u_multipath² + u_propagation² + u_systematic²)
```

Individual components are recorded in the L3 fusion HDF5.

---

## 4. Failure Conditions

- **Using any module other than `TickEdgeDetector` to write `tick_timing` HDF5** — `TickEdgeDetector` is the intended sole timing source. (Note: as of 2026-05-17 `TickMatchedFilter` is still instantiated and run, and the A/B decoder-comparison machinery has *not* been removed — see §5 D1.)
- **Window-relative time in IQ mixer** — causes ~1.7 rad phase jumps; must use buffer-relative time (`t_abs = start_second + adjusted_start/sample_rate + arange(n)/sample_rate`)
- **Widening or removing the physics validation gate** (±15 ms from arrival_matrix) — this catches remaining noise false positives after search window capping
- **Shared Kalman state between TSL1 and TSL2** — defeats the dual-feed architecture; L2 ionospheric correction is discarded
- **Fixed discontinuity filter threshold** (e.g., 10 ms) — causes permanent chrony SHM latch when uncertainty is high; must scale with `max(10, 3*uncertainty_ms)`
- **Not advancing `last_chrony_d_clock` on rejected updates** — causes permanent latch
- **Treating `clock_offset_ms` as a calibrated UTC offset** — it contains propagation model systematic errors (CHU: −76 ms)
- **Per-record HDF5 writes for tick_phase** (55 writes/min/channel) — causes heap corruption; must use `write_measurements_batch()`
- **Forgetting CHU 74 ms H3E sideband filter correction** — produces systematic offset on all CHU channels
- **Processing BPM UT1 minutes (25–29, 55–59) as UTC** — 100 ms ticks encode UT1, not UTC
- **Silent exception swallowing in HDF5 reads** — must log at WARNING, not DEBUG; causes invisible data starvation
- **Tests failing**: `test_carrier_phase_continuity` σ > 0.3 rad, or any regression in `tests/`

---

## 5. Known Deviations (current code vs. this contract)

Recorded 2026-05-17 from the code review (`docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md`). These are points where the **current code does not yet meet the contract above**. The contract states the intended design; this section is the honest gap list. Each item should be resolved by either fixing the code or — if the clause itself is wrong — amending the clause.

| # | Contract clause | 2026-05-17 reality | 2026-05-20 status | Review ref |
|---|-----------------|--------------------|-------------------|------------|
| D1 | §1/§4 — `TickEdgeDetector` is the sole timing module; A/B comparison removed | `metrology_engine.py` constructed four `TickMatchedFilter` instances; A/B comparison machinery + `l2_decoder_comparison_v1.json` still existed | **Open.**  A/B comparison still in place pending the S1 contract-refresh follow-up (which is itself listed in review §7 as the natural place to decide the matched-filter's fate). | M-H2 |
| D2 | §1/§2/§4 — physics validation gate ±15 ms, "do not widen" | Per-station 3σ floors (CHU 100 ms, BPM 50 ms) widened the effective gate | **Open.** Floors still in place; the "gate → likelihood weight" refactor (D5 below) supersedes this — once the gate becomes a weight, the floors stop controlling accept/reject. | review §2 S1, §3 |
| D3 | §2/§4 — TSL1 and TSL2 must have independent Kalman state | Per-broadcast Kalmans + convergence/drift state were shared and mutated twice per cycle | **Resolved 2026-05-17** (M-C1). The two feeds run independent banks; the M-M13 cycle-persistence convergence gate (commit `bdbfd2d`) further protects against premature lock during restart settling. | M-C1 |
| D4 | §4 — must advance `last_chrony_d_clock` on rejected updates | `last_chrony_d_clock` advanced but `last_chrony_update_time` only on success, desynchronising the two references | **Resolved 2026-05-17** (M-C2).  Both references advance every cycle; see the in-source comment at `multi_broadcast_fusion.py` line ~5285. | M-C2 |
| D5 | `METROLOGY_PHYSICS_SPLIT.md` action item 1 — gate → likelihood weight | `ArrivalPatternMatrix.validate_detection()` was a binary gate; no `[0,1]` weight method | **Open.** Listed in review §7 as item 11 ("S1 / D-H7"); pending the contract-refresh sequenced with M-H2's decision on the matched-filter. | review §6 item 7 |
| D6 | §4 — no per-record HDF5 writes for high-cadence products | `all_arrivals` and `detection_attempts` were per-record | **Resolved** (M-M18, commit `39658c1`).  Both products now use `write_measurements_batch`; tick + CLEAN multipath share one batch.  Companion M-M19 promoted the failure path to rate-limited WARNING. | M-M18 |
| D7 | §4 — Joseph-form covariance update for per-broadcast Kalmans | Short-form `P = (I − KH) P` accumulated asymmetry / drift toward non-PSD over ~10⁶ updates/week | **Resolved** (M-M14, commit `3b0245c`).  Joseph form + explicit symmetrisation; M-M15 added the NaN/Inf entrypoint guard. | M-M14/15 |
| D8 | §4 — leap-second hold must coast the per-broadcast Kalmans across the entire transition | `_fsk_leap_second_hold` was a per-cycle boolean that cleared the very next cycle | **Resolved** (M-M11, commit `bdbfd2d`).  Hold is now a timestamp window (`_fsk_leap_second_hold_until`) with a 10-minute default; single TAI-UTC change observation coasts through the transition. | M-M11 |

**Open question for the next contract revision:** the §2 Kalman `Q` values have no documented derivation, and `Q (Drift)` was `1e-12` in the prior contract but is `1e-8` in code (10⁴× larger). The table above now reflects the code; the *correct* value must be established (measurement-based) and the table re-pinned.
