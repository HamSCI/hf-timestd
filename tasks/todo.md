# Metrology/physics remediation — M-M batch

Branch `metrology-physics-review-remediation`. P-M1–P-M26 all done. Now
on **M-M (metrology-Medium)** findings, 11 file-clusters.

## Done this session (M-M)
- [x] **S4 + M-M1 + M-M3** — canonical correlation-peak SNR: new
      `core/snr.py` with `peak_snr_db_envelope` (Rayleigh) and
      `peak_snr_db_signed` (Gaussian); `tick_edge_detector` and
      `tick_matched_filter._correlate_tick_iq` migrated to the envelope
      branch; `tick_matched_filter._correlate_tick_am` migrated to the
      signed branch (replaces the 40 dB sentinel with NaN). The
      `metrology_engine` correlation-SNR sites will migrate when its
      M-M cluster lands.

## Remaining M-M clusters (one commit each)
- [ ] **M-M2** `tick_edge_detector` — Doppler polyfit unweighted +
      cycle-slip-risky unwrap.
- [ ] **M-M4** `buffer_timing` — GPS_LEAP_SECONDS captured once at import.
- [ ] **M-M5/M-M6/M-M7/M-M8 + S4-finish** `metrology_engine` — vacuum
      fallback `×1.15`; minute_number from untrusted system_time;
      synthetic edge round-trip truncates mid_sec; ±800 ms multipath
      suppression; migrate correlation SNR to `peak_snr_db_envelope`.
- [ ] **M-M9/M-M10/M-M11/M-M12/M-M13** `multi_broadcast_fusion` — key
      formatter; dead `gpsdo_locked` guard; leap-second Kalman hold
      length; >5 ms D_clock jump not damped; dual convergence definitions.
- [ ] **M-M14/M-M15** `broadcast_kalman_filter` — Joseph-form covariance
      update; NaN/Inf guard on entrypoint.
- [ ] **M-M16/M-M17** `chrony_shm` — `_connect_sysv` recreates a segment
      chronyd may be attached to; failed `update()` does not clear
      `.connected`.
- [ ] **M-M18/M-M19/M-M20** `metrology_service` — per-record
      `all_arrivals`/`detection_attempts` heap risk; DEBUG-not-WARNING
      log; `_cleanup_processed_set` horizon uses `time.time()` while
      minutes are keyed by ring `head_utc`.
- [ ] **M-M21/M-M22/M-M23** `l2_calibration_service` — vacuum-only
      geometric-fallback `propagation_delay_ms`; `k=2.0 / dof=10`
      mis-labelled as 95 %; uncertainty components un-sourced.
- [ ] **M-M24/M-M25/M-M26/M-M27/M-M28** `arrival_pattern_matrix` —
      `int()` truncation in sample conversions; `contains_sample` /
      `deviation_sigma` disagreement; `max_search_sample` clamp;
      RTP-mode gate on TEC correction; virtual-vs-true hmF2 semantics.
- [ ] **M-M30/M-M31/M-M32/M-M33/M-M34/M-M35** `propagation_mode_solver` —
      Tier-2 MUF check; `back_calculate_emission_time` fallback;
      E-layer ×0.5 fudge; `identify_mode` metric mismatch; dead FSS
      branch; circular `second_aligned` boost.

(M-M29 done by S2 — `propagation_mode_solver._hop_geometry` now uses
spherical `hop_geometry`.)

## Then
Low (§3.4, §4.4); documentation (§5); P-H29 (TID L3 wire-in, deferred).

## Workflow
`uv run --frozen --extra dev pytest tests/` — `--frozen` keeps uv.lock
pinned. Known time-of-day flakes (deselect / not regressions):
`test_geometric_prediction`, `test_fusion_gnss_vtec_rtp_gate`.
