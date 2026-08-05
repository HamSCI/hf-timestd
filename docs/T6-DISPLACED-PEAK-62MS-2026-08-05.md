# T6 displaced-peak recurrence at +62 ms — first live Offset-Judge T6 bench run

Date: 2026-08-05 ~20:00Z · Station: AC0G-B4 (fresh v3.19 appliance install,
same RX888/GPSDO/TS-1 hardware as the prior install) · Follow-on evidence for
**hf-timestd#7** (MF peak acquisition: deterministic displaced-peak preference).

## Context

The v3.19 greenfield shipped with every tier above chrony disabled
(`[timing.l6_pps] enabled = false`, no `lb1421_enabled`) — the offset judge ran
at T4. We enabled T5 (LBE-1421) + T6 (TS-1 BPSK PPS, bee1-parity params, no
`chain_delay_calib_s` so the chain delay was measured fresh). This was the
**first time the P2 NativeAnchorBench ever judged live data** — the previous
install's judge had also been running at T4 (same template gap, unnoticed).

## What worked (60 s from restart)

- TS-1 stream locked at 45.375 MHz, SNR 45.4 dB, `ok=11, noise=1`.
- **T5→T6 chain-delay disambiguation worked exactly as designed** (the flow
  built after the July 2026 sidelobe incident, commits `bffb2cd`/`88f810e`):
  raw MF delay 407 062 317 ns, NMEA-PPS integer-second alignment (+1 s),
  residual +104.583 ms → effective chain_delay **104 582 787 ns**, vs the
  prior install's measured ~105.74 ms (Δ ≈ 1.2 ms — different radiod
  build/config, plausible).
- Judge advanced T4 → T6; HPPS SHM fed chrony (reach 377).

## The defect evidence

1. **chrony marks HPPS falseticker (`#x`) at a rock-stable +62 ms ± 55 µs.**
   Same signature as the July +557 ms incident: a *constant, precise*
   displacement — instrumental, not noise. New displacement magnitude on the
   new radiod instance (24 kHz IQ channel, granted encoding s16 vs requested
   f32 — noted, not yet excluded as a factor).
2. **The judge's per-source offsets shifted coherently by ≈ −12 ms** when the
   bench switched T4 → T6 (WWV siblings +2 ms → −10 ms; the 2.5 MHz outlier
   +16.8 → +5.1 ms, its +14.6 ms intrinsic displacement preserved). radiod's
   epoch derives from the same chrony the T4 bench uses and agreed with it to
   ~2 ms, so a 12 ms coherent shift measures **bias in the T6 UTC
   reconstruction**, not in radiod.
3. T6 bench σ published as 25 ms (initial conservative), so no k·σ violation
   fired — the biased bench was silently adopted as the top tier and its
   offset applied to labels. See the companion proposal
   (`JUDGE-CROSS-BENCH-GATE-2026-08-05.md`): tier advance must require
   cross-bench agreement.

Note the two consumers disagree on the displacement magnitude (+62 ms in the
chrony SHM path vs −12 ms in the judge bench path). Both paths consume the
same native anchor; the difference (sign conventions, chain-delay application
point, or two distinct defects) is itself diagnostic and unexplained.

## Action taken (rob-approved, 2026-08-05 ~20:10Z)

`[timing.l6_pps] enabled = false` restored on AC0G-B4 (backup
`timestd-config.toml.bak-t5t6-20260805-195637` holds the enabled variant for
reproduction). **T5 stays enabled** — the judge now benches on the LBE-1421
PPS directly: unbiased, GPS-grade, a large upgrade over T4. T6 stays off
fleet-wide until #7 is resolved; the appliance flow deliberately does not
auto-enable it.

## Reproduction

Re-enable `[timing.l6_pps]` with `frequency_hz = 45375000` + bee1-parity
params on AC0G-B4, restart `timestd-core-recorder`, wait ~90 s: T6 locks,
`chronyc sources` shows HPPS `#x` at ≈ +62 ms, judge sources shift ≈ −12 ms.
Deterministic across the ~20 min it ran tonight.
