# METROLOGY CONTRACT — hf-timestd

**Version:** 1.0.0
**Last Updated:** 2026-02-23
**Status:** Active — evolves with implementation

---

## 1. Goal

Ensure the metrology pipeline (Phase 2) extracts **accurate, traceable, uncertainty-quantified timing measurements** from raw IQ data, suitable for both ionospheric science (RTP mode) and UTC recovery (Fusion mode).

### Performance Objectives

- **Tick detection rate**: ≥50 ticks/minute/station at ≥10 dB SNR (57 max for WWV/WWVH)
- **Ensemble timing uncertainty**: ±0.008 ms (CHU at 20+ dB), ±0.5–2 ms (WWV/WWVH typical)
- **Doppler extraction**: meaningful fit requires ≥5 ticks spanning ≥5 seconds
- **False positive rate**: <1% after physics validation gate (arrival_matrix ±15 ms)
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

- Search window formula: `max(50, min(100, tone_duration_sec * 625))` ms
- Noise exclusion zone = full template length (not half)
- Physics validation gate: arrival_matrix ±15 ms — **do not widen or remove**
- Cross-frequency gate: MIN_FREQ_ADVANTAGE_DB = 3.0 dB (rejects 1000↔1200 Hz cross-response)

### Station-Specific Handling

| Station | Modulation | Tick Duration | Special Handling |
|---------|-----------|---------------|-----------------|
| WWV | AM DSB | 5 ms | Silent minutes: {29, 43–51, 59} |
| WWVH | AM DSB | 5 ms | Silent minutes: {0, 8–10, 14–19, 30} |
| CHU | USB + preserved carrier | 300 ms | H3E sideband filter: 74 ms group delay correction |
| BPM | AM DSB | 10 ms (UTC), 100 ms (UT1) | UT1 minutes (25–29, 55–59) filtered out |

### Kalman Filter ("Steel Ruler")

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Q (Offset) | 0.01 ms | Allows tracking real measurements |
| Q (Drift) | 1e-12 ms/min | GPSDO does not wander |
| R (Measurement) | 30.0 ms | Rejects ionospheric turbulence |
| Drift clamping | 0.0 after convergence | GPSDO prevents drift accumulation |

### Dual Kalman Architecture

- **TSL1** (SHM 0): L1 Kalman — geometric fallback, no ionospheric model
- **TSL2** (SHM 1): L2 Kalman — full ionospheric correction via propagation model
- Each feed has **independent** Kalman state (separate `kalman_state_l2` arrays)
- Discontinuity filter threshold: `max(10, 3 * uncertainty_ms)` — must not permanently latch

### Dependencies

- `ka9q-python` for RTP reception and channel management
- `numpy`, `scipy` for DSP (matched filtering, correlation, bandpass)
- `h5py` for HDF5 output (with `locking=False`)
- `ArrivalPatternMatrix` for physics-based expected arrival predictions
- `HFPropagationModel` for frequency-dependent delay predictions
- `IonoDataService` for real-time ionospheric parameters

---

## 3. Format

### Tick Timing HDF5 Record

Schema: `l2_tick_timing_v1.json` (schema_version 2.0.0, processing_version 5.0.0)

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

- **Using any module other than `TickEdgeDetector` to write `tick_timing` HDF5** — `TickMatchedFilter` is legacy; the A/B decoder comparison feature was removed
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
