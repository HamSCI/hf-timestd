# TSL3 — V1-fix anchor-churn investigation (2026-05-18)

## What the V1-fix subsystem is

`docs/TIMING-PIPELINE-WIRING.md §10.3` — TSL3's SHM push computes the edge
wall-time via `rtp_to_wallclock`, which projects the RTP counter forward from a
`(gps_time, rtp_timesnap)` anchor captured from radiod's status. "V1" = that
anchor going stale. The fix is a layered policy in `core_recorder_v2.py`:
- Layer 1 — settled-capture gate: capture the anchor only when chrony is
  settled (|offset| ≤ 100 µs). Correct, working.
- Layer 2 — drift monitor: Signal A (`_t6_check_anchor_consistency`,
  anchor discontinuity) + Signal B (`_t6_check_delta_breach`, sustained Δ).
- Layer 3 — `_t6_attempt_recapture`: re-capture the anchor on a flag.

## Root cause of the live TSL3 churn — Layer 2 Signal A misfires on noise

The doc §10.3 is explicit: the anchor must be **FROZEN** (a frozen anchor +
GPSDO sample clock projects UTC exactly; Δ then tracks chrony's current
discipline error — what we want). Periodic re-capture is the **wrong** thing —
it "injects chrony's drift into Δ". Re-capture is meant to fire only on a
genuine radiod **restart** or a **sustained** Δ breach.

Signal A as implemented also flags an "anchor discontinuity" whenever
`|actual_rtp_delta − expected_rtp_delta| > T6_ANCHOR_DISCONTINUITY_SAMPLES`
(1000 samples ≈ 10 ms @ 96 kHz), and Layer 3 acts on it **immediately,
bypassing all hysteresis**. The design comment assumes the residual noise is
"single-digit samples". It is not:

- Measured directly (8 `discover_channels` polls, this session): residual
  **±410 samples** noise floor — already ~40 % of the threshold.
- Journal since restart: outlier-triggered residuals **−4023, −1690, −11728,
  −83193, −17417 samples** (the −83193 ≈ 867 ms). 5 re-captures in ~10 min.
- The re-capture old→new `(gps,rtp)` pairs are mutually consistent (±350) —
  the underlying RTP↔gps relation is fine; Signal A is reacting to **reading
  noise**, not real drift.

So every noise outlier → false "discontinuity" → immediate re-capture → the
`rtp_to_wallclock` anchor is swapped for a fresh (often-noisy) reading → TSL3's
reference jumps. Re-capture every ~1 min IS the "periodic refresh" §10.3 says
is wrong → TSL3 churns, chrony flags it `x`.

This is the same failure shape as the Costas/phantom bugs: a disruptive action
taken on a single noisy sample instead of a persistent signal.

## Fix — IMPLEMENTED on branch `tsl3-anchor-drift-monitor-fix`

- **Persistence-gated Signal A.** `_t6_check_anchor_consistency` now counts
  *consecutive* polls on which the residual breaches the threshold
  (`_t6_drift_residual_breach_count`); the flag is raised only at
  `T6_ANCHOR_DISCONTINUITY_POLLS` (5 → 25 s). A clean reading resets the
  counter, so a lone noise outlier is ignored and the anchor stays frozen
  (the §10.3 design). A genuine radiod restart / clock step breaches every
  poll and still flags. Same principle as Part 1's `STEP_CONFIRM_EDGES`.
- **Counter-rollback check kept as-is** — a backwards jump is unambiguous and
  still fires immediately.
- Bypass-hysteresis on `anchor_discontinuity` kept: once persistence-gated the
  flag only fires on a genuine, confirmed discontinuity, where an immediate
  re-capture is the right response. Signal B keeps its cooldown/cap.
- Re-capture clears `_t6_drift_residual_breach_count`; `residual_breach_count`
  added to the `drift_monitor` status block; corrected the stale "single-digit
  samples" design comment.
- Tests: `test_core_recorder_t6_drift_monitor.py` reworked — single large
  residual must NOT flag; sustained residual flags only at the Nth poll;
  isolated outliers interleaved with clean reads never flag. 78/78 T6+BPSK
  tests pass.

## Deferred (noted, not done)
- Radiod-side: why does radiod occasionally emit ~800 ms-off status readings?
  Separate, lower-priority.
- Bad-seed robustness (the drift anchor seeded from one possibly-noisy
  reading) — persistence-gating makes it converge in one re-capture; a
  median-of-N seed would be cleaner. Revisit if observed live.

## Status
- Costas-gate (`d8ca67e`) + calibrator phantom fix (`6bfc6b9`) — deployed.
- Anchor-drift-monitor fix — implemented on branch, NOT merged/deployed.
