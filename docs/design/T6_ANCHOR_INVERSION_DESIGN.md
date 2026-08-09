# T6 Anchor Inversion — Design

**Date:** 2026-08-09
**Status:** Approved design, pre-implementation
**Approvers:** Michael (mjh); builds on rob's 2026-08-08 architectural insight
**Prerequisites:** hf-timestd `cca50ff` (SHM pairing fix), `d463605`/`ae82ddf`
(disambiguation + batch-labelling instrumentation), ka9q-python `c5bf01e`
(anchor-pair audit). Signal model confirmed against WB6CXC TimeSync-1 guide
(MOD 0 = BPSK via XOR with GPS PPS; documented ~10 µs modulation-output delay)
and raw-IQ measurement (transitions at exactly 1.000000 s, zero intra-second,
52 µs 10–90 % edge through the ±25 kHz channel).

## 1. Problem

The TS-1 HF-PPS edge is a nanosecond-accurate statement of where the top of a
UTC second sits in the sample stream (0.12 µs measured repeatability). The
system currently uses it backwards: the matched-filter edge is *timed by* the
radiod GPS-pair / T5 anchor (`rtp_to_wallclock`), and the residual against the
NMEA second is stored as `effective_chain_delay` — a fitted ms-scale value
that absorbs every upstream timestamp error. Every chain_delay ever computed
(32.49, 37.71, 104.58, 105.74 ms) violates the physical definition in
`ARCHITECTURE-FIRST-PRINCIPLES.md` (analog path only ⇒ microseconds) by 3–4
orders of magnitude. Separately, the ±0.5 s antisymmetric matched filter has a
flat apex (0.2 %/ms), so argmax localisation converts small instrumental
amplitude tilt into ms-scale, perfectly-repeatable bias — chrony's "constant,
precise, wrong" signature.

## 2. Target architecture (the inversion)

When T6 is locked and sane, the edge **defines** the RTP→UTC anchor:

```
anchor_rtp    = fine_stage_edge_rtp
anchor_utc_ns = named_integer_second * 1e9 + delay_budget_ns
```

- **`named_integer_second`** comes from the coarse cascade (T5 NMEA → T4 →
  host clock) by rounding. It needs only ±0.5 s accuracy; its noise cannot
  enter the sub-second value by construction.
- **`delay_budget_ns`** is a bounded configured constant (§5), not a fitted
  quantity, not persisted, not per-site.
- `rtp_to_wallclock` is consulted only inside coarse naming and diagnostics.
- `chain_delay` survives only as a reported diagnostic (measured edge phase
  within the second), never as a correction.
- `NativeAnchor.captured_via_tier = "T6"` when the inversion is active. The
  Pattern-B offset (`_compute_rtp_to_utc_offset_ns`), chrony HPPS push (with
  the `cca50ff` pairing fix), and the offset judge's `NativeAnchorBench`
  consume the anchor unchanged. No judge or client-contract changes.
- Holds for local **and remote** radiod: the HF-PPS travels the same chain as
  the data, so radiod-side latency is common-mode and cancels.

**Scope:** hf-timestd internal (all Grape/PSWS data-product timestamps + the
existing chrony SHM export). Judge tier arbitration, fleet export to other
recorders, and TS-1 auto-enable are follow-on specs (§9).

## 3. Fine-stage estimator — `core/bpsk_edge_fine_stage.py`

New class `BpskEdgeFineStage` (one class per file), fed the same complex
baseband batches as the MF. The existing `BpskPpsCalibratorMF` is demoted to
coarse stage — window steering, lock/step detection — unchanged internally.

- **Fold buffer:** K seconds (config `fine_fold_seconds`, default 30) of
  complex64 folded modulo `sample_rate`, second *n* multiplied by (−1)ⁿ to
  undo BPSK alternation. Parity choice flips the averaged edge's sign, not
  its position — no parity bookkeeping needed.
- **Continuity indexing:** fold index derives from a stream-continuity
  counter, not per-batch declared RTP. Continuity→RTP registration is the
  **median** of all batch declarations in the window, so the measured
  ±60-sample batch mislabelling (91 %/9 % bimodal) averages out instead of
  smearing the 52 µs edge.
- **Derotation:** carrier phase estimated from folded samples away from the
  transition; rotate so power sits in I. (Measured carrier offset 0.00000 Hz —
  the fold is genuinely coherent.)
- **Zero-crossing localisation:** within a small window around the coarse
  position, linear-fit the central ramp of averaged I between the ∓40 %
  amplitude points (~5 samples at 96 kHz) and interpolate the crossing to
  sub-sample precision. A symmetric crossing makes amplitude tilt a
  second-order effect — the property that transfers fleet-wide with no
  per-site tuning (HF-PPS is locally injected: SNR high and narrow-range at
  every site).
- The fine stage never searches globally.

## 4. Authority state machine — `core/t6_anchor_authority.py`

Single owner of "is T6 the anchor authority right now". Dependency-injected
(probes, clock source) in the `AuthorityManager` style; no service imports.

```
ACQUIRING → AUTHORITATIVE
AUTHORITATIVE → DEGRADED   (any §6 invariant violated: hold last good anchor,
                            alarm; GPSDO permits long safe coasting)
DEGRADED → AUTHORITATIVE   (invariants green again)
DEGRADED / AUTHORITATIVE → UNLOCKED  (DEGRADED for longer than
                            `degraded_unlock_after_sec` (default 600 s), or MF
                            unlock: anchor invalidated, fall back to current
                            cascade)
UNLOCKED → ACQUIRING
```

Every transition logs at WARNING with the violated invariant named, and is
reflected in `authority.json` and the status JSON. Fallback is the existing
radiod-pair/T5 path. Never silent (expose-don't-correct rule,
`docs/METROLOGY.md` §4.5–4.6).

## 5. Delay budget

`delay_budget_ns` = TS-1 modulation-output delay (~10 µs, documented) +
analog path (coax ~5 ns/m + front end, sub-µs) + radiod channel-filter group
delay (deterministic for a given sample-rate/bandwidth — a **fleet constant
keyed to channel config**, not a per-site fit).

- Config key under `[timing.t6_pps]` with a documented default for the
  standard 96 kHz / ±25 kHz configuration.
- **Hard validation bound ±1 ms** at config load and at every anchor
  computation; out-of-bound refuses with an alarm naming the physical
  argument. (Would have rejected every historical chain_delay on day one.)
- **Approved:** until the one-time B4 characterisation against GPS truth
  (§8, live phase), the default carries a stated uncertainty of a few hundred
  µs — 100× better than the current ms-scale error, and honest. The
  characterised value becomes the fleet default for that channel config.

## 6. Standing invariants (continuous, loud)

1. **Periodicity:** successive fine-stage edges advance by exactly
   `sample_rate` samples; |deviation| > `edge_period_tolerance_ns`
   (default 5 µs) → DEGRADED. With a GPSDO-locked ADC, deviation is a fault.
2. **Fine–coarse consistency:** |fine − MF apex| ≤ ~5 ms (same edge; apex
   bias is ms-class, so a larger gap means one estimator is broken).
3. **Naming residual:** coarse-source residual after rounding < 0.4 s
   (margin inside the ±0.5 s cell).
4. **Delay-budget bound:** ±1 ms hard (see §5).
5. **Cross-tier diagnostic:** T6 vs T4/T5 disagreement is *reported* (status
   JSON + judge benches), never corrects the anchor.

The old implicit check ("pps_firing_utc lands on an integer second") is
forced true by construction under the inversion; these five are its
non-circular replacements.

**Approved deletion:** the persisted chain-delay store
(`bpsk_chain_delay_store.py`) is retired on the T6 path, along with the
250 ms `T6_PHYSICAL_CHAIN_DELAY_MAX_NS` plausibility guard it fed. No
ms-scale fitted state exists any more to be worth persisting; re-lock costs
~`fine_fold_seconds` + a few seconds. Persistence code is removed from the
anchor path; the store file, if present, is ignored (log at INFO once).

## 7. Configuration

New keys under `[timing.t6_pps]` (defaults in the template, all validated):

| key | default | meaning |
|---|---|---|
| `fine_fold_seconds` | 30 | coherent fold length K |
| `delay_budget_ns` | 10_000 | §5: TS-1 modulation delay; group-delay term 0 pending §8 characterisation (stated uncertainty: few hundred µs) |
| `edge_period_tolerance_ns` | 5000 | invariant 1 |
| `fine_coarse_max_ms` | 5 | invariant 2 |
| `degraded_unlock_after_sec` | 600 | continuous DEGRADED longer than this → UNLOCKED |

Deprecated `[timing.l6_pps]` emission from the template and
`setup-station.sh` is a separate cleanup (§9).

## 8. Testing and validation

TDD throughout (test-first for each unit).

- **Unit:** synthetic BPSK with the measured 52 µs edge shape — prove the
  zero-crossing is invariant under amplitude tilt that provably shifts argmax
  by ms; s16 quantisation; ±60-sample batch-jitter folding; parity flip;
  derotation; state-machine transitions for each invariant violation;
  delay-budget bound rejection.
- **Offline replay (acceptance gate):** `t6-rawiq.bin` (24.72 s @ 96 kHz) and
  both MF NPZ dumps through an extended `tools/t6_estimator_sweep.py`.
  Require: ≤1 µs repeatability across split windows, and agreement with an
  independent early-minus-late discriminant (kept in the harness as a
  cross-check only, not shipped in the service path).
- **Live (deferred until B4 is back on v3.25):** chrony HPPS offset collapses
  from ~+30 ms to µs-class; 24 h soak with zero DEGRADED transitions; judge
  shadow comparison vs the T4 bench; one-time delay-budget characterisation
  (§5). B4 and b4-prox are unreachable at design time — nothing in the
  implementation or offline acceptance depends on them.

## 9. Out of scope (follow-on specs)

- Offset-judge tier-ladder arbitration (T6 outranking T4/T5 station-wide).
- Fleet export of the anchor to wspr/psk/meteor recorders (crosses the
  ka9q-python contract boundary).
- TS-1 auto-enable on USB detection (`239a:801e`) + alias frequency keyed
  off the radiod sample rate (84.225 MHz carrier: 129.6 MHz → 45.375,
  64.8 MHz → 19.425).
- The batch-mislabelling defect itself (fine stage tolerates it; radiod-side
  fix is its own ticket) and the radiod anchor-pair inconsistency (bypassed;
  ka9q-python `c5bf01e` diagnostics remain).
- hpps-watchdog restart behaviour while T6 is acquiring; `[timing.l6_pps]`
  template cleanup.
