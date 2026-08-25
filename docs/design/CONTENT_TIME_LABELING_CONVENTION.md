# Content-time labels: the anchor asserts physics, the benches own transport

**Status:** APPROVED (rob, after the 2026-08-24 A/B confirmed all six
predictions).  Code SHIPPED — `cd57586` (fine stage) + `0274439` (coarse
`chain_delay_calib_s`).  **ADOPTION BLOCKED** — see the box below.  Changes
the meaning of the T6 anchor's chain-delay terms, resolves #12 and #38, and
amends `T6_ANCHOR_INVERSION_DESIGN.md` §5 and `METROLOGY.md` §4.5.  Generalised
by `TIMING_AUTHORITY_TWO_AXIS.md` (2026-08-25), whose §1 is this document's
argument stated for every timing source rather than just T6.

> ⛔ **ADOPTION BLOCKED 2026-08-25 — but NOT for the reason first recorded.**
> The flip was attempted on AC0G-B4 at 15:00:31Z and rolled back at 15:07Z
> because `shadow_residuals` read **−1007.7 ms** and it appeared the labels
> had gone a full second out.
>
> ⚡ **They had not.**  The anchor ledger rows from that window
> (`state/t6-anchor-ledger/`, `chain_delay_ns=10000`) show the anchors were
> exactly right:
>
> ```
> anchor_utc_ns       1787670061000011634
> named_second_utc_ns 1787670061000000000    -> named_second + 11.634 us
> chain_delay_ns      10000                  -> sub_ns = -1634 ns
> ```
>
> Two consecutive content anchors 30 s apart: ΔRTP = 2,880,000 = exactly
> 30 s × 96 kHz, Δutc = 30 s − 272 ns.  Self-consistent to **272 ns**, the
> same shape as the legacy anchors, differing by exactly the intended
> 16.618 ms.  `test_convention_step_is_exactly_the_retired_constant` and
> `test_content_anchor_matches_the_b4_ledger_shape` now pin this.
>
> **So the labelling half of the convention works.**  What is broken is the
> REPORTING path: `NativeAnchorBench.poll()` publishes
> `utc = floor.offset_s + arrival_mono`, i.e. the arrival-floor map, not
> `utc_ns_at_rtp` from the anchor.  Under content labels `arrival − label`
> becomes the whole pipeline latency (§5.2 predicts exactly this), and the
> label-plane tracker went `source='measured'` with `offset_ns
> −25,846,958` and `sigma_ns 197,134,152` — 197 ms of scatter.  The
> −1007.7 ms is an artifact of that path, not of the anchor.
>
> ⚠ **chrony could not see any of it** — HPPS feeds `reference_time` = the
> integer second and builds `system_time` from the floor, so it read a
> healthy-looking `+16 ms` throughout.  Judge a convention change by
> `shadow_residuals` AND the anchor ledger; chrony cannot adjudicate it.
>
> **Remaining work before adoption:** fix the bench/label-plane reporting
> so `shadow_residuals` means something under content labels.  Tracked as
> hf-timestd#44.

---

## 1 · The question

What UTC should a sample's label mean?  Two candidate conventions have been
living in the codebase unnamed:

* **Content time** — the instant the field crossed the antenna (to within the
  µs-class analog chain).  This is the only convention the science can use:
  time-of-flight, phase, and every absolute ionospheric observable are defined
  at the antenna.
* **Transport-consistent time** — labels offset so that they agree with
  arrival-based witnesses (chrony benches, T4/T5 comparisons), which measure
  when samples *reached the software*, not when the field existed.

Today's labels are transport-consistent **by accident of calibration**: the
`filter_group_delay_ns = 16 618 000` constant was fitted on 2026-08-15 to zero
the T6-vs-T4 shadow residual, and T4 is measured through the transport.

## 2 · The physics already settles it

The TS-1 flips its carrier on the GPS second: ±~100 ns of GPS plus a
documented ~10 µs modulator delay (Paul Elliott, TimeSync-1).  That flip
travels the same coax → RX888 → ADC path as the science signal, and the fine
stage locates it in the sample stream to nanoseconds (5 ns repeatability;
1.9 µs origin spread over 4.5 h, 2026-08-24).  **This is an a-priori absolute
time transfer.**  There is no physical mechanism by which the wavefront
reached the antenna 16.6 ms after the second — so the content-true assertion
for the edge-sample is

    anchor_utc(edge) = named_second + ε,     ε ≈ 10 µs (analog + modulator)

i.e. `delay_budget_ns ≈ 10 000` and `filter_group_delay_ns = 0` — **which is
the shipped default**.  The ±1 ms `DELAY_BUDGET_BOUND_NS` guard and the
original §5 "analog path is µs to sub-ms" statement were right all along;
issue #12 resolves in their favor, not the 250 ms guard's.

## 3 · Where the 16.618 ms actually comes from

Derived from radiod source and measured on B4 (2026-08-24):

| term | value | nature |
|---|---|---|
| label-grid displacement `T_M/2 = blocktime/(2·(Overlap−1))` | 2.500 ms | deterministic filter geometry — but it **cancels inside the anchor construction** (the edge's label is itself displaced by the same amount), so it never belongs in the anchor constant |
| front-end FFT compute | 10.2–11.9 ms | measured two ways: live `fft` thread duty 50.9–51.9 % × 20 ms; on-box fftwf bench of the exact 3.24 M-point transform with radiod's wisdom (11.93 ms min) |
| USB transfer granularity | 0–2.02 ms (mean ≈ 1) | 32 × 16 384 B ÷ 2 B ÷ 129.6 MHz |
| demod/scheduling remainder | ~1–2 ms | residual |
| **total Λ + grid term** | **≈ 16.6 ms** | matches the fitted constant to ~1 ms |

Λ is **compute latency**, absorbed into the fitted constant because radiod's
`(GPS_TIME, RTP_TIMESNAP)` status anchor snapshots a wall clock after the
pipeline has run, and every on-station witness (T4/T5 benches, arrival floor,
chrony) measures through the same pipeline — they are transport-degenerate
and cannot distinguish a late label from a slow transport.  Λ also **scales
with machine load and CPU**: it is not a constant of the design, and a
calibration that moves when Phil parallelizes the FFT is not a physical
calibration.

Confirmation that the old picture was an artifact: the 2026-08-10 bandwidth
sweep (31.8 / 45.8 / 108.9 ms at 96 k/24 k/12 k) reproduced on the honest
post-fix stream reads **flat**: +16.55 / +14.13 / +16.80 / +15.89 ms derived
residual at 96 k/±25 k → 24 k/±11 k → 12 k/±5.5 k → 96 k restore, with zero
refusals, zero Costas unlocks, zero step adoptions.  The scaling story is
dead; the derivation's bandwidth-independence prediction held.

## 4 · The corollary that makes this fleet-grade

`T_M/2` is `blocktime/(2·(Overlap−1))` — **identical for every channel on the
front end, at any bandwidth and any sample rate**.  So a single content-true
T6 anchor labels *all* channels' content correctly, not just its own.  And
because the content-true constant is the shipped default (`0` + a µs
`delay_budget`), **a fresh DASI install needs no per-site chain-delay
calibration at all** — the "loudly uncalibrated until measured" workflow
dissolves.  Per-site ε differences are µs-class (cable lengths) and can ride
one fleet constant plus an optional site trim.

## 5 · What adopting the convention changes

1. **Config:** `filter_group_delay_ns` retired (key, bound, validator warning
   and template text); `delay_budget_ns` stays as the µs-class ε term with its
   ±1 ms guard.  `chain_delay_calib_s` on the coarse path gets the same
   treatment.
2. **Benches own the transport — and the convention makes it self-measuring.**
   The T4/T3 benches ground in host-now, T5 in sample arrival, T6 in the
   label plane (`offset_judge.py:420-425, :509-511, :647-651, :604-608`), so
   cross-bench deltas and shadow residuals expose a label-plane shift at full
   amplitude.  The fix is a plane-correction term in the cross-bench
   comparison only (never in any bench's own sigma).  The decisive detail:
   under content-true labels, `arrival − label` **is** the total pipeline
   latency — so the ArrivalFloorTracker, unchanged, becomes a continuous
   live measurement of the transport term (≈15–16 ms on B4) that was
   *unmeasurable by construction* under the old convention
   (`t6_arrival_floor.py:57-63`).  The term is measured, never asserted.
3. **HPPS pair: no push-side change needed.**  `t6_shm_pair.py:125-131`
   already builds `system_time` from the floor so a constant label shift
   cancels out of it; `reference_time` is the integer second.  What chrony
   displays is therefore exactly the label-plane error — today's standing
   −1.5…−3 ms HPPS offset *is* `pipeline_min − 16.628 ms`, a live residual
   of the old constant (consistent with the 2026-08-14 transport capture's
   min lag of −3.78 ms).  Under the new convention chrony would read
   ≈ −(pipeline_min); restoring an honest near-zero feed is one line —
   subtract the floor-measured transport from `system_time` — using the
   self-measuring term from §5.2.
4. **Science products:** labels step −16.6 ms at adoption.  The anchor
   ledger (2026-08-24, `state/t6-anchor-ledger/`) makes every anchor since
   08-24 re-labelable retroactively by arithmetic; pre-ledger data gets the
   constant documented in metadata.  Absolute ToF (T6 − T3) becomes real at
   the µs-to-ms level; all differential science is unaffected.
5. **Docs:** `METROLOGY.md` §4.5 tier table, `ARCHITECTURE-FIRST-PRINCIPLES`
   chain-delay definition (restored to its original analog-only meaning),
   `T6_ANCHOR_INVERSION_DESIGN` §5; resolves #12 and #38.

## 6 · The A/B validation (AC0G-B4, 2026-08-24, reverted)

`filter_group_delay_ns` was flipped 16 618 000 → 0 for a bounded window with
the LBE-1421's DCD PPS as a chrony-independent host-second marker (host clock
FUSE-disciplined, independently agreeing with a LAN stratum-1 to ~10 µs).

| observable | prediction | measured (window 13:31–14:09Z) |
|---|---|---|
| ledger anchor sub-second | 16.628 ms → ~0.010 ms | **0.0113 ms** (10 µs budget − ~1.3 µs subsample), every fold |
| T6 vs the other benches | steps ≈ −16.6 ms; others unmoved | **T6 shadow −16.481 ms vs T4; T3 +0.001, T5 +0.003** — only the label plane moved |
| judge | cross-bench conflict; tier falls for the window | **conflict {upper T6, lower T4, Δ −16.48 ms} at 13:32:09; judge on T4, σ 0.65 ms** |
| chrony HPPS | pair shifts by −16.6 ms, rejected; FUSE unaffected | **pair (ref−sys) −16.3 ms; `chronyc` displays +16 ms (its sign negation); FUSE +2.4 µs throughout** |
| DCD host-second marker | ±2 ms bound, both phases | **A: n=146 p50 +0.749 σ 0.33 ms · B: n=143 p50 +0.731 σ 0.28 ms — marker unmoved while everything label-side stepped 16.6 ms** |
| stability | lock held; no refusals | **AUTHORITATIVE 20 s after the flip restart (fine_coarse 0.003 ms), held all window; revert clean, AUTHORITATIVE at 14:12** |

**Bonus, the §5.2 self-measurement observed live:** under convention B the
floor-referenced `offset_to_chrony` read a steady **−15.6…−18.3 ms** — the
total pipeline latency, measured continuously for the first time, agreeing
with the independent decomposition (FFT 10.2–11.9 + USB ~1 + grid 2.5 +
UDP/sched).  Under convention A the same channel read −0.5…+2.6 ms — the
degenerate residual.

**Verdict: all six predictions confirmed.  The 16.618 ms constant is
transport, not physics; content-true labels are consistent, stable, and make
the transport itself observable.**

## 6b · Adoption status (2026-08-24)

**Approved and implemented.** What shipped:

| §5 item | How it landed |
|---|---|
| 5.1 Retire the constant | `[timing.t6_pps].labeling_convention` — `"content"` (default) does not apply `filter_group_delay_ns`; `"legacy"` is the pre-convention arithmetic, kept so a site reverts in one key. The configured value is preserved as `filter_group_delay_ns_configured` so nothing is lost silently. |
| 5.2 Bench transport term | `core/label_plane.py` — `LabelPlaneTracker` measures the (label − host) plane offset from paired bench readings; the judge uses it in `_cross_bench_delta_ns` and publishes it as `label_plane` in `offset_judge.json`. |
| 5.3 HPPS feed | `t6_shm_system_time(..., transport_ns=, transport_sigma_ns=)` — subtracts the measured transport and widens sigma accordingly. Opt-in; absent term is byte-identical to the old arithmetic. |
| 5.5 Docs | This section, `ARCHITECTURE-FIRST-PRINCIPLES` (analog-only definition restored), `METROLOGY` §4.5 tier row, `STATION_SETUP_GUIDE` (resolves #38). |

Two properties are load-bearing and are pinned by tests:

* **The plane term is measured, never asserted.** Replacing one
  hand-calibrated constant with another would have missed the point.
* **The term is slow; the gate is instantaneous.** A term that tracked
  the live T6−T4 delta would cancel exactly the disagreement the
  cross-bench gate exists to detect, making the gate quietly vacuous. It
  is a long-window median, so a T6 epoch step still reaches the gate at
  full amplitude (`test_a_t6_step_is_not_absorbed_immediately`).

It is also only honest while the host clock is independently disciplined
— chrony on FUSE holds ~20 µs, three orders below the ~16 ms term — so
observations from a host looser than 1 ms are refused, and every estimate
carries the host's sigma.

> ⚠ **Upgrading an existing site.** The default is `"content"`, so a host
> that carries a measured `filter_group_delay_ns` will shift its labels
> EARLIER by that amount (−16.618 ms on AC0G-B4) at the first restart
> after this release. To decouple the convention change from the upgrade,
> set `labeling_convention = "legacy"` before deploying and flip it as its
> own step. The anchor ledger makes either order re-labelable by
> arithmetic.

## 7 · What this does NOT change

The RTP-primacy invariant (labels still never derive from the host clock);
chrony as the host-clock adjudicator (#21); honest per-feed sigma;
detect-and-alarm-never-correct.  The anchor inversion is unchanged — this
proposal only fixes what its asserted constant *means*.

## 8 · Open questions for rob

1. Verify ε at the µs level: scope TS-1 PPS OUT against the RF flip (bench
   task, with Paul).  Until then ε carries the documented ~10 µs with a
   conservative uncertainty.
2. Order of operations: implement the bench transport terms (§5.2–5.3)
   *before* flipping the default, so the judge doesn't sit in cross-bench
   conflict through the transition.
3. Multi-host DASI2 (remote radiod): the HF-PPS travels the same chain as the
   data, so radiod-side latency stays common-mode and cancels — rob's
   original corollary — but the bench transport terms are per-host and need
   the same treatment there.
4. Historical products: annotate or re-emit?  (Operator ruling to date:
   forward-only.)
