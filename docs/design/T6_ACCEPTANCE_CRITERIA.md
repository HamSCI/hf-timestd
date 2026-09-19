# T6 Acceptance Criteria — self-consistency in place of a lesser witness

**Date:** 2026-09-19
**Status:** Approved design, pre-implementation
**Approvers:** Michael (mjh)
**Extends:** `T6_FOLDED_SELF_ACQUISITION.md` (which stage may gate) and
`T6_ANCHOR_INVERSION_DESIGN.md` §6 (chain-delay persistence retired).
Neither gets superseded. This document changes what T6 must prove before it
asserts, and who may overrule it afterwards.
**Constrains:** `T6_EDGE_METHODS_COMPARED.md` §8 stays in force — see §1.2.

---

## 1. The governing principle

Michael's statement, which the rest of this document turns into mechanism:

> T6 is not just an afterthought. When present and functional, no other timing
> authority can achieve its accuracy or should hobble it. We should prepare
> fallback when hardware fails, of course. We should have guardrails to alert
> us when something goes awry. But otherwise, T6 should purr along.

### 1.1 What primacy grants

T6 owns the **origin**: which UTC instant a sample carries, what the station
asserts to its consumers, and the anchor chrony gets disciplined from. While
T6 passes the battery of §4, no lesser tier gates it, slews it, or adjudicates
it.

### 1.2 What primacy does not grant

T6 votes on the **ruler** never. The edge arrives through the sample stream,
so the measurement lives in sample time and inherits the converter's rate, and
a source that inherits a quantity cannot check it. `T6_EDGE_METHODS_COMPARED.md`
§8 and `MEASUREMENT_MODEL.md` §7.1.1 state that prohibition, and this document
leaves it standing. A station whose GPSDO drive had fallen to 8 mA sampled
350 ppm fast while every self-check read healthy — the case that shows why a
rate claim needs a witness outside the sample stream.

The two-axis model separates these cleanly: T6 holds the origin, other sources
hold the ruler, and nothing here merges them.

---

## 2. The fault

Three layers decide whether T6 may assert. They currently disagree.

| Layer | Where | Behaviour today |
|---|---|---|
| Acquisition | `core_recorder_v2._get_disambiguation_reference` (`:2355`) | Demands a non-T6 tier reporting σ < `T6_DISAMBIGUATION_MAX_SIGMA_MS` = 0.010 ms (`:2238`). |
| Arbitration | `authority_manager._witness_drives_consequences` (`:665`) | Shields an rtp-frame GPS-disciplined tier from sysclock-frame witnesses: they flag, they never demote. |
| Judge | `offset_judge` cross-bench gate (`cross_bench_k` = 5.0, `:1038`) | Adopts a candidate bench only when it agrees with a trusted lower tier inside `k·√(σ_c² + σ_l²)`. |

The arbitration layer already implements the governing principle. This design
finishes the job at the two layers that never received it.

### 2.1 The acquisition gate asks for something that does not exist

At 96 kHz one sample spans 10.4 µs. The gate therefore demands a reference
whose uncertainty falls **below one sample period**. No tier we run delivers
that, at any station:

| Tier | Best σ observed | Against a 10.4 µs sample |
|---|---|---|
| T5 — GPS+PPS over USB | ~1 ms (bus-jitter floored) | 100× too wide |
| T4 — LAN GPS+PPS via chrony | 0.29 ms at AI6VN | 28× too wide |
| T3 — HF Fusion | sub-ms at best | ~100× too wide |
| T2 — WAN NTP | ~20 ms | ~2000× too wide |

The docstring in `_get_disambiguation_reference` says as much about T3 already:
the sigma gate "will reject it in practice". The gate names a bar only the
measurement itself can clear, then declines to let the measurement clear it.

DASI-009.AI6VN shows the consequence without ambiguity. It has no T5 (the
lbe-mini exposes no PPS), no T3 (no antenna), and T4 at 0.29 ms. So
`_t6_disambiguate_via_external_reference` logs *"no usable non-T6 timing
authority for disambiguation; accepting calibrator value as-is"*, never
completes acquisition, and the station sits at T2 — while its edge lands on
one sample position, every second, 128 seconds running.

⚡ **The gate exists to catch integer-sample wraps (10.4 µs) and half-second
lattice errors. The measurement it refuses scatters ±100 ns** — two orders of
magnitude better than the ambiguity that gate guards against.

### 2.2 The judge's tolerance scales the wrong way

The cross-bench gate arrived after the 2026-08-05 displaced-peak incident, in
which — the code comment records it — "a biased-but-stable T6 was adopted over
a healthy T5 because its honest wide sigma kept k·σ quiet."

The gate reproduces that same polarity one level up. Its bound,
`k·√(σ_c² + σ_l²)`, grows with the candidate's own reported uncertainty. At
AI6VN on 2026-09-18 a σ of 477 ms widened the bound to roughly 2.4 s, and a
284 ms disagreement passed unremarked.

⚠ **A bench that reports worse uncertainty earns a wider licence.** For a gate
meant to catch a bench that has gone wrong, that runs backwards — going wrong
usually inflates σ.

`authority_manager` met the mirror-image problem and floored its pair
thresholds so a noisy witness could not mask a real disagreement. The judge
needs the opposite bound: a **ceiling**, so a noisy candidate cannot excuse its
own error. §5.4 places it.

### 2.3 Why a reference cannot be repaired into sufficiency

Sharpening the reference does not help, because acquisition conflates two
questions whose tolerances differ by four orders of magnitude:

| Question | Tolerance it needs | Best tier available |
|---|---|---|
| Which GPS second does this edge belong to? | ≪ 500 ms | T2, ~20 ms — ample |
| Which lattice peak, among phantoms 20 ms apart? | ≪ 10 ms | T4, 0.29 ms — ample |
| Which integer sample within the second? | ≪ 10.4 µs | **none, anywhere** |

One threshold cannot serve all three. §3 separates them.

---

## 3. Two resolutions, two tolerances

Acquisition stops asking one question at one threshold.

### 3.1 Ordinal — which GPS second

The physical-plausibility bound already in
`_t6_disambiguate_via_external_reference` (`T6_PHYSICAL_CHAIN_DELAY_MAX_NS` =
250 ms) sets this tolerance. Any clock inside ±250 ms names the right second.

T2 sits at ~20 ms (`authority_manager.TRUST_SIGMA_MS`), clearing the bound
twelvefold, and T2 never goes away. So the ordinal resolver becomes
structural rather than conditional: every station has one, AI6VN included.
Where a real HF antenna sharpens T3, it makes an easy question easier and
changes nothing about the design.

T6 reads an integer from the resolver and grants it nothing further. The
resolver names a second; it does not measure a phase, does not gate, and does
not slew.

⚡ The tier least able to do T6's job performs the only job T6 needs from it.

**Selection.** Walk the tier ranking downward and take the first source
reporting an offset with σ ≤ `T6_ORDINAL_MAX_SIGMA_MS` (50 ms — a fifth of the
plausibility bound). Record which source supplied it in the anchor's
`captured_via_tier`, as the code does today, so provenance survives.

**When no source clears even 50 ms**, T6 does not acquire, names the condition
once per throttle period, and the station runs its fallback. That looks like
today's refusal but differs in kind: the threshold sits 5000× looser, so a
station holding no clock within 50 ms of UTC carries a far larger problem than
a T6 acquisition. The old gate refused stations that were healthy; this one
refuses only stations that are not.

### 3.2 Phase — where in the second

The measurement answers this, alone. No tier gets consulted, because §2.1
shows none can.

The fine stage's folded estimate, having passed §4's battery, supplies the
sub-second term outright. Concretely, where the present code derives the
integer-sample shift from a reference offset —

```
disagreement_sec = offset_sec - (ref_offset_ms / 1000.0)
shift_samples    = round(disagreement_sec * sample_rate)
```

— the new path takes the battery-passed fold position as the edge's phase
within the second, and uses the §3.1 ordinal only to name which second that
phase belongs to. `_t6_disambiguate_via_external_reference` keeps its ±250 ms
plausibility guard, keeps its anchor capture, and keeps recording
`captured_via_tier`; it loses its dependence on a reference **offset**, and
retains a dependence on a reference **second**.

### 3.3 The functional test replaces the reference test

T6 acquires when its own edge passes the battery, not when a worse clock nods.
A tier failing the battery does not assert. A tier passing it asserts, and the
judge stops second-guessing it.

---

## 4. What "functional" means

Seven criteria. Each carries a number already measured on station, and each
catches a named failure.

| | Criterion | Statistic | Measured 2026-09-18 | Catches |
|---|---|---|---|---|
| 1 | Fold retention | folded amplitude ÷ mean per-second amplitude | 0.9998 AI6VN, 0.98 B4, **0.006 when broken** | a missing reference cable; any incoherent chain |
| 2 | Ruler | inter-edge Δ against the sample rate | 127/127 = 96000 exactly | the pulse source walking against the ADC |
| 3 | Unimodality | per-second estimates about their median | 128/128 and 89/89 at one position | multipath, split peaks, a competing signal |
| 4 | Shape — triangle fidelity, apex agreement, width | RMS residual of \|T(e)\| against the ideal triangle ÷ peak; apex-to-reported-edge distance; pulse width against 1/(2B) | residual 0.0008–0.0017 real against 0.25–0.42 noise; −1919 samples on a 20 ms displaced lock | lattice phantoms |
| 5 | Scatter, and σ within the tier's physical budget | per-second sd; σ/√K over the fold | 95 ns, n = 89 | a broken tier masquerading as a wide one |
| 6 | Split-half agreement | two disjoint folds, against k·σ predicted; **NaN when either sub-fold is empty** | 41 ns against 17 ns predicted, n = 2 | a wandering apex |
| 7 | Plausibility | implied chain delay within ±250 ms | shipped and in force | gross wrap and sidelobe capture |

### 4.1 Criterion 1 diagnoses the fault that cost two days

`BpskEdgeFineStage` accumulates complex baseband with per-second sign
alternation (`:136`, `:282`). That fold survives only while the carrier stays
coherent with the ADC clock across seconds. Feed the TS-1's REF IN from the
same GPSDO that governs the RX888 and retention reaches 0.9998; leave REF IN on
the internal 10 MHz and retention collapses to 0.006 — the fold cancels the
very signal it accumulates.

So a single published number separates a working reference chain from a
missing coax. AI6VN spent two days looking like a detector problem and turned
out to be a cable.

### 4.2 Criterion 4 carries the weight the reference used to carry

Every criterion that measures *repeatability* passes a phantom, because a
phantom repeats perfectly. B4 locked onto a 20.000 ms lattice on 2026-09-04 and
held it.

Criterion 4 discriminates on *shape* instead, and on two statistics rather than
one.

**Triangle fidelity.** The closed-form matched filter of
`T6_FOLDED_SELF_ACQUISITION.md` §3.1,

    T(e) = sum_{j>=e} x[j] - sum_{j<e} x[j] = C[p-1] - 2*C[e-1]

traces a **triangle** over the folded second when that second holds one clean
polarity flip: |T| climbs linearly to the apex at the edge and falls linearly
away. Fitting the ideal triangle and reporting the RMS residual over the peak
therefore measures whether a flip is present at all, independently of how
strong it is.

**Apex agreement.** The distance between T(e)'s apex and the edge position the
stage reports. The 2026-09-04 failure was a lock at a lattice position *away
from* the true apex, so this is the statistic that addresses it directly.

⚠ **An earlier draft of this document specified peak ÷ median and cited 105×
from §8c. Measurement refuted it.** Driven against synthetic signal on
2026-09-19, a peak-over-background ratio read 4.9–5.5 at B4's governing
48.4 dB-Hz against 4.2–5.3 on pure noise — no threshold separates those. The
fault was operator choice: that statistic *differentiates*, which doubles noise
power and confines the signal to the ~2 samples of the transition, where T(e)
*integrates* the whole second and survives. Two orders of magnitude separate
the two approaches at the condition that governs.

    fidelity residual   pure noise            0.2547 – 0.4199
                        real, 70 dB-Hz        0.00137
                        real, 48.4 dB-Hz      0.00079 – 0.00171
    apex distance       real                  0.35 – 0.87 samples
                        20 ms displaced lock  −1919.19 samples

⚠ These come from **synthetic signal**, not a station capture — unlike §8c's
105×, which was measured on 89 s of real IQ. They establish that the statistic
separates; they do not stand in for an on-station measurement, which §8 owes.

⚠ Apex distance does **not** separate a real edge from noise, and is not meant
to. It is a displacement check. It also runs weak in the fine stage's
*bootstrap* mode, where the search centre and the apex derive from the same
fold; it earns its keep in *seeded* and *tracking* mode, where an external
coarse offset can place the search away from the apex — which is the 09-04
shape exactly. The battery weights it by search mode rather than applying one
threshold to all three.

⚠ The `FineEdgeEstimate` field keeps the name `peak_prominence` for contract
stability while the criterion is named `shape`. **Lower now means better** —
the field carries a residual, not a ratio.

⚡ Without criterion 4 this design would trade a gate that blocks good edges for
one that admits bad ones. It is the load-bearing member.

### 4.3 Criterion 5 disposes of the σ-inflation case

A T6 reporting σ = 477 ms has not produced a wide T6; it has produced a broken
one. Bound the accepted σ by what the tier's own physics permits: the
per-second edge scatter that the channel's measured C/N0 predicts, improved by
the fold's processing gain of 10·log10(K), times a stated margin. The
09-18 numbers anchor that curve at both ends — 95 ns per second at 77 dB-Hz,
and the §8b budget's 1.8σ per-sample margin at 48.4 dB-Hz. Any σ orders of
magnitude above the prediction reports a broken measurement, not a wide one,
and fails the battery before any gate sees it. §2.2's defect then cannot arise from the
T6 side at all, and §5.4 closes it from the judge's side as well.

### 4.4 What the battery cannot see

⛔ All seven criteria ride one antenna, one converter, one path.
`T6_EDGE_METHODS_COMPARED.md` §8 already says a T6 anchor and a T6 fold are not
two witnesses, and that holds here: the battery proves **self-consistency**, and
claims nothing more.

⚠ Criteria 1 and 2 test **coherence**, not accuracy. Both read healthy when one
oscillator drives everything and drifts. Citing a zero beat or an exact
inter-edge delta as proof that a GPSDO holds UTC repeats an error made on
2026-09-18 and corrected on the bus the same day. See
`reference_ts1_reference_chain`.

The residual the battery cannot reach — a GPSDO disciplined to the wrong
second, a TS-1 whose pulse marks something other than UTC — falls to the
ordinal resolver of §3.1 and to calibration against UTC. State that in the
published uncertainty rather than leave it implied.

### 4.5 Thresholds

Derive every threshold from the C/N0 sweep during implementation, pin each with
a test, and write it as a module constant beside its derivation — the pattern
`T6_FOLDED_SELF_ACQUISITION.md` §4 already set for the matched-filter
threshold. None of them becomes an operator knob.

⚠ **They must hold at B4 after dark.** §8b's phase-noise budget leaves a
per-sample discriminant 1.8σ of margin at B4's measured 48.4 dB-Hz worst hour,
against 52σ on AI6VN's injected pilot at 77 dB-Hz. Folding buys that margin
back, which is why the battery reads the fold rather than individual seconds.
A threshold set on an injected pilot and never checked against 48.4 dB-Hz would
ship a gate that fails every night at exactly the hours §2 of the folded
acquisition design says we are judged on.

### 4.6 The battery reads the fold, not individual seconds

⚠ The numbers in §4's table come from per-second estimates on captured IQ. The
battery evaluates at **fold-block** granularity, because that is what the
production path produces: `BpskEdgeFineStage` emits one `FineEdgeEstimate` per
`fold_seconds` block, not one per second.

The mapping, criterion by criterion:

| | Per-second form (measured) | Fold-block form (implemented) |
|---|---|---|
| 2 | inter-edge Δ = the sample rate | consecutive block edges differ by `fold_seconds × sample_rate` |
| 3 | per-second estimates at one position | consecutive block estimates agree — the existing `BOOTSTRAP_CONFIRM_BLOCKS` mechanism, surfaced rather than rebuilt |
| 5 | per-second sd = 95 ns | block-to-block sd, predicted as the per-second sd ÷ √K |

Criteria 1, 4, 6 and 7 already evaluate on the folded block and need no
mapping.

⛔ Criterion 6 reports **NaN**, never 0.0, when either sub-fold holds no
samples. 0.0 is the most favourable value the criterion can return, so
returning it for "no evidence" would report a parity fault — every sample
routed into one sub-fold — as perfect agreement. §5.2's rule that absence stays
visible as absence binds inside a criterion, not only at the tier boundary.
The battery fails the criterion on NaN. §4.5's thresholds therefore get derived in per-second terms from the
C/N0 sweep, then converted by the fold's √K improvement before they become
module constants.

⚡ This keeps a per-second detector out of the production path. §8b's
measurement argues one belongs there eventually — Newell's detector placed
every edge where our chain could not — but adopting it would change the
detector and the acceptance rule in one step, and a regression in either would
then hide the other. That sequencing lesson belongs to
`T6_FOLDED_SELF_ACQUISITION.md` §4, and it applies here unchanged.

---

## 5. Failure, alarm, and the judge

### 5.1 A failed guardrail keeps asserting

When the battery fails on a T6 already running, T6 **keeps asserting**, marks
the assertion suspect, names the failing criterion, and alarms. The operator
decides. That follows the standing rule for this project: expose a timing
fault, never silently correct it.

Demoting instead would reproduce the present problem in new costume — a
transient trip handing the station back to a tier two orders of magnitude
worse.

### 5.2 A missing estimate stays missing

⛔ A failed guardrail and an absent estimate remain different things, and
nothing here may blur them. When estimates stop arriving, the authority's
existing liveness invariant degrades loudly, exactly as today. Absence stays
visible as absence.

### 5.3 The judge inverts

Once T6 passes the battery, T6 becomes the bench. It extrapolates its own
anchor forward — at the measured 1.44 µs/hr holdover decay — and checks the
ordinal source against itself. A disagreement past half a second raises an
alarm **naming the ordinal source**, and T6 keeps its own count.

This mirrors what `authority_manager._witness_drives_consequences` already does
for a GPS-disciplined rtp-frame tier against sysclock-frame witnesses. The
acquisition path gains the same shielding the arbitration path has had all
along.

### 5.4 Cap the cross-bench tolerance

Cap the candidate's σ contribution to `k·√(σ_c² + σ_l²)` at the tier's physical
budget (§4.3), so a candidate reporting a worse σ stops buying itself a wider
licence. The existing `sigma_regression_margin` clause stays as it is; it
addresses a different question — whether a tier upgrade may regress precision.

### 5.5 Log rate

Every refusal and every alarm reports through the existing once-per-period
throttle (`_t6_say_once`, `T6_REPEAT_PERIOD_SEC`). A persistent condition
reported every cycle blinds the log it writes to, and this project has
collected five such floods already.

---

## 6. Fallback when hardware fails

Nothing above weakens the fallback path. A station with no TS-1, a dead
injector, or a battery that will not pass runs the tier below, exactly as it
does today. Primacy applies to a T6 that **passes §4**, and to no other.

The fallback ordering, the tier ranking, and `T6AnchorAuthority`'s state
machine all stay unchanged.

---

## 7. Testing

Every criterion in §4 earns a test that **fails when that criterion is
removed**. A test nobody has watched fail has not yet been shown to test
anything.

Beyond those, five that pin the behaviour this design exists to produce:

* an AI6VN-shaped fixture — no T5, no T3, T4 at 0.29 ms — reaches T6
* a B4-shaped fixture at 48.4 dB-Hz reaches T6
* a 20 ms lattice phantom gets **refused**
* a candidate reporting σ = 477 ms gets **refused**
* an ordinal source 2 s wrong **alarms without moving T6**

And one regression, for §4.1: a fixture with retention 0.006 fails the battery
and names criterion 1, so a missing reference cable reports itself as a missing
reference cable rather than as a detector fault.

---

## 8. Verification on station

**AI6VN first, because it strips the problem to bone.** No antenna, no PPS, no
peer above T4 — so an acquisition succeeding there carries no hidden dependence
on a peer. Success: `t_level_active = T6`, a σ consistent with §4's criterion 5,
and a residual disagreement that does not sit at a stable hundreds-of-ms value.

**B4 second, for the hours that matter.** Compare 00–06Z before and after, with
`rf_gain`, `if_power`, `t6_baseband_power` and `t6_n0` confirming comparable
receiver conditions. Success: the ~270 tier transitions per day fall, and T6
holds through hours it currently spends ACQUIRING.

⛔ Do not pin B4's gain to reach this. The AGC swings −4.2 to +27.5 dB against
real diurnal noise and B4 never clips because of it.

Deploy by git fast-forward plus a restart of the `timestd-*` units, never
`install.sh`. The restart re-anchors the recorders, so schedule it with the
operator.

---

## 9. Out of scope

* Retiring tier rank from acceptance in favour of pure evidence-weighted
  arbitration. That shape follows naturally from this work — §3.1's provenance
  recording supplies exactly what it would need — but it reaches into the
  authority state machine, the estimator, the judge, and every consumer's
  understanding of the word "tier". It earns its own document.
* Any change to the ruler axis, to `MEASUREMENT_MODEL.md` §7.1.1, or to the
  §8 prohibition (§1.2).
* Any change to TS-1 injection level, or to the RX888 AGC.
* The nightly apex wander itself. Criterion 6 detects it; curing it belongs to
  the folded-acquisition thread.
