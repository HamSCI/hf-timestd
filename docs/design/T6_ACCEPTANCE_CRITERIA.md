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

⚡ **This codebase already resolves the ordinal, and the acquisition path
simply does not use it.** `core_recorder_v2._t6_name_integer_second` names the
integer UTC second of a fine-stage edge, and its docstring states this
document's §3.1 almost word for word: *"The coarse cascade only NAMES the
second — it needs ±0.5 s accuracy and its noise cannot enter the sub-second
value."* It prefers the T5 NMEA reading, falls back to the radiod-pair wall
estimate, refuses a residual beyond ±0.4 s, reconciles whole-second slips, and
*reports* its disagreement with the radiod pair rather than correcting by it.
The fine-estimate path has called it since the anchor-inversion work.

So §2's finding widens. The arbitration layer got primacy right; the
fine-estimate layer got the ordinal right. Only
`_t6_disambiguate_via_external_reference` remains in the old regime, demanding
a sub-10 µs reference for a question answered correctly a thousand lines away
in the same file.

⛔ Acquisition therefore **calls the existing namer**. It does not grow a
second one. A parallel resolver would be a weaker duplicate of a tested
function and a second thing to keep in agreement.

The paragraphs below state the requirement the namer already satisfies, and
remain the specification of what any replacement would owe.


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

**Selection.** `_t6_name_integer_second` walks T5 NMEA first, then the
radiod-pair wall estimate, and refuses anything landing more than ±0.4 s from
an integer second. Provenance survives in the anchor's `captured_via_tier`.

**When the namer returns None**, T6 does not acquire, names the condition once
per throttle period, and the station runs its fallback. That looks like today's
refusal but differs in kind: the namer asks for ±0.4 s where the old gate asked
for 10 µs, so a station it refuses carries a far larger problem than a T6
acquisition. The old gate refused stations that were healthy; this one refuses
only stations that are not.

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

### 3.2.1 Where the battery runs, and why not in acquisition

⛔ The battery evaluates **once per fold block**, at the site where the fine
stage delivers an estimate. Acquisition *consults* the standing verdict; it
never evaluates inline.

The reason is arithmetic, and a first implementation got it wrong. Acquisition
is gated on `_t6_last_chain_delay_ns is None`. But criteria 2 and 3 need a *run*
of blocks. A battery evaluated in acquisition therefore sees one block, fails
ruler and unimodality forever, and T6 never asserts: the old blocker returns
wearing a new reason.

So T6 waits about three folds — 90 s at K = 30 — after first lock before it may
assert. That delay is honest. A battery cannot claim self-consistency from a
single block.

⛔ **And the gate must stay open for the whole of that wait.** It did not. The
recorder latched `_t6_last_chain_delay_ns` at the end of the initial-accept
branch *unconditionally* — whether or not the walk had captured anything. The
matched filter locks after about ten edges (~10 s), the first fold block lands
at K = 30 s, and the battery needs three of them. So acquisition ran exactly
once, at t ≈ 10 s, with `_t6_last_fine_est is None`; it logged
`no_fine_estimate` and returned; and the latch closed the gate behind it. Only
an abnormal event — stuck-recovery, step-recovery, a stale-lock abandonment, an
authority UNLOCK — reopened it. A healthy station could not acquire at all.

Fixed 2026-09-19: the latch fires only when *that attempt* captured an anchor
(`_t6_native_anchor` compared by identity against its value before the walk, so
an anchor the authority already held is not mistaken for this attempt's). The
gate now stays open, one walk per `T6_DISAMBIG_RETRY_INTERVAL_SEC`, until the
battery produces a passing verdict and an anchor is taken on it — which is what
this section claimed all along. `_t6_last_chain_delay_ns` thereby stops meaning
"the MF has locked" and starts meaning "an anchor has been captured";
stuck-recovery, which needed the earlier fact, reads its own
`_t6_mf_ever_locked` flag instead.

⚠ The evaluation sits inside the fine-stage block, **upstream of the authority
call**. An exception there would skip the authority update and stop T6 asserting
at all, so the evaluation fails closed behind its own guard: it drops the
verdict, says so once per throttle period, and lets the authority carry on.
Anything added to that block inherits this hazard.

⚠ A refused verdict must not move the timing path. The integer-sample shift is
computed into a local and committed only once the verdict passes and the ±250 ms
guard clears. An earlier implementation committed it first, so a fold refused for
a missing reference cable still shifted the clock by the mapping error it
carried.

### 3.2.2 What the acquisition anchor rests on

⚠ The anchor captured at acquisition reduces algebraically to
`rtp_to_utc(edge_rtp)` — radiod's own mapping, evaluated at a precisely located
edge. Its accuracy is therefore bounded by radiod's registration error, and
radiod's GPS_TIME pairing is host-clock-derived and non-atomic.

⚡ **This bounds the bootstrap, not what T6 asserts.**
`t6_anchor_authority._build_anchor` constructs the AUTHORITATIVE anchor from the
fine estimate plus the named second with an **asserted** chain delay
(`delay_budget_ns + filter_group_delay_ns`), reading neither the acquisition
anchor nor its disambiguation term. That anchor is `named_second + asserted
delay` — independent of radiod entirely — and it overwrites the capture seconds
later. `_t6_anchor_is_authoritative` requires the authority state, so the
acquisition anchor alone never makes T6 authoritative for the T3 plane.

⛔ Both routes call `_t6_name_integer_second`, so they fail together on a station
that cannot name a second. That is the single shared dependency, and §8 should
measure how often it refuses.

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
| 5 | Scatter, and σ within the tier's physical budget | reported σ against `max(predicted × margin, 1.0 ms)` | 95 ns on real IQ (n = 89); 490 ns anchors the shipped curve | a broken tier masquerading as a wide one |
| 6 | Split-half agreement | two disjoint folds, against a fixed `max_split_half_delta_samples = 10.0` (104 µs at 96 kHz — *not* a k·σ prediction; see §4.5 for the derivation); **NaN when either sub-fold is empty** | 41 ns against 17 ns predicted, n = 2 | a wandering apex |
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

traces a **tent** over the folded second when that second holds one clean
polarity flip: T climbs linearly to the apex at the edge and falls linearly
away. Fitting the ideal tent and reporting the RMS residual over the apex
magnitude therefore measures whether a flip is present at all, independently of
how strong it is.

⛔ **Fit the signed T, never |T|.** For an edge at *e* the tent's endpoints are
`+A(p−2e)` and `−A(p−2e)`, so they carry **opposite signs** unless the edge
splits the fold exactly in half. Take the absolute value and |T| acquires a
V-notch wherever T crosses zero. No endpoint → apex → endpoint triangle can
follow that notch, so the residual grows with how far off-centre the edge sits
— and where the edge sits inside the fold follows only from where the stream
started, which changes on every boot. The criterion then reports an arbitrary
per-boot offset rather than anything about the station.

The shipped code did fit |T| until 2026-09-19. Measured at 77 dB-Hz against the
0.015 bound then in force:

    edge 47916 → 0.0014    edge 40000 → 0.1260    edge 30000 → 0.2611
    edge 24000 → 0.3333    edge  9600 → 0.4869    edge  1000 → 0.5683

Every position but mid-fold was refused. Fitting the signed T removes the
dependence outright: a clean flip fits the tent exactly, whatever the apex
position, so the residual falls to the noise the fold leaves behind. Locating
the apex by `argmax |T|` stays correct — it finds the extremum whichever sign
the flip carries; only the *fit* must run in signed space.

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

Re-measured 2026-09-19 with the signed fit, over **nine fold positions**
(37 … 95 960 samples) × six C/N0 × three seeds, and 32 noise seeds:

    fidelity residual   pure noise            0.1115 – 0.6760   (90 blocks)
                        real, 77 dB-Hz        0.0000081 – 0.000031
                        real, 58 dB-Hz        0.000060  – 0.00014
                        real, 48.4 dB-Hz      0.00017   – 0.00039
                        real, 44 dB-Hz        0.00029   – 0.00064
    apex distance       real                  0.35 – 0.87 samples
                        20 ms displaced lock  −1919.19 samples

Across the nine positions the worst healthy reading at 48.4 dB-Hz moves only
0.00031 → 0.00039, a 1.3× spread, against the 410× spread the |T| fit produced.
Separation from the null holds at **every** position at both 77 and
48.4 dB-Hz. `max_fidelity_residual` is **0.008**: the geometric middle of the
worst healthy reading at 44 dB-Hz and the nearest noise block, 20.5× above the
worst reading at the governing 48.4 dB-Hz and 13.9× below the lowest of 90
noise blocks.

⚠ The earlier table — noise 0.2547–0.4199, real 0.00079–0.00171 — was taken at
one fold position, 47916, the one place the defect does not show.

⚠ **The fit is on signed `T(e)`, never on `|T(e)|`.** `T` is a clean tent —
linear on both arms, apex at the edge, endpoints `±A(p−2e)`. Off-centre those
endpoints carry opposite signs, so `T` crosses zero on the descending arm and
`|T|` acquires a V-notch a fitted triangle cannot follow. Fitting `|T|` made the
residual a function of where the edge happened to land in the fold — an
arbitrary per-boot offset. Measured at 48.4 dB-Hz: a spread of **410×** across
fold position under `|T|`, against **1.3–2.2×** under signed `T`.

⚠ **Headroom shrinks with a shorter fold.** Against the 0.008 bound, the worst
healthy reading at 48.4 dB-Hz sits 13.7× below it at the shipped K = 30, but
only **6.5× at K = 10**. Still separating, and the null stays 15× above the
bound — but a station running a shorter fold has materially less room, and
should not do so without re-measuring.

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
shipped curve anchors at **490 ns** per second at 77 dB-Hz.

⚠ **Not the 95 ns of §8c, and the difference matters.** That figure came from
real IQ; the sweep that calibrates this code measures 4.0–5.2× wider, uniformly
across 33 dB. The constant is anchored to what the code actually produces
rather than to the better number, because a ceiling anchored optimistically
refuses healthy stations.

⛔ **The ceiling carries an absolute floor of 1.0 ms, and that floor governs
everywhere real stations live** — from 77 dB-Hz down to about 36. The C/N0
curve is therefore inert across the whole operating range, by design.

The reason: `reported_sigma_ms` is the tier's *own published uncertainty*,
computed by the authority; the prediction models the *fold's* block-to-block
scatter. Those are different quantities, and no choice of anchor makes
comparing them sound. Pinned to the curve alone, the gate misfires on a real
station — at a plausible 57 dB-Hz it computed 0.0173 ms against B4's recorded
0.003–0.07 ms, refusing a healthy B4 by 4×. That is the exact failure this
whole document exists to end.

⛔ **And the σ it reads is the fold's own, not the tier's.** The recorder used
to hand criterion 5 the standard deviation of `_t6_chain_delay_history`, a deque
appended to only inside a branch that requires `_t6_native_anchor is not None`.
Before acquisition it is empty, so σ was None, became NaN, and failed criterion
5 through `_fails`: **the battery needed an anchor and the anchor needed the
battery**. Verified 2026-09-19 — three healthy blocks against an empty deque
gave `failures=('sigma',), sigma=nan` and no anchor, forever; on a station with
no chrony SHM configured that route never supplied a value at all. The recorder
now prefers `T6Battery.block_position_sigma_ms()`, the standard deviation of the
block positions the battery already retains for criterion 3, and falls back to
the chain-delay history only when the battery holds fewer than two. That is the
**fold's own repeatability**, not the tier's published uncertainty — the
authority's `t6_sigma_ms` is unchanged and is still a different number. The
comparison becomes self-consistent (a model of block scatter against a
measurement of block scatter) instead of circular; it does not become a
precision check.

⚠ **Criterion 5 is now nearly inert, and the note records that rather than
implying otherwise.** Its input became the fold's own block-to-block position
scatter — about 5e-5 ms — because the previous source was circular: it read a
history appended only once an anchor existed, so the battery needed an anchor
and the anchor needed the battery. Against the 1.0 ms floor that leaves four
orders of magnitude of slack, and the tier's *published* sigma is no longer
checked by any criterion. Better than a criterion that always fails, but it
wants re-deriving against its new population.

⚡ So criterion 5 guards **gross breakage only**. Orders of magnitude, never a
factor of two. Against the 1.0 ms floor, AI6VN's 477 ms fails by 477× while
B4's worst recorded hour passes with 14× of room. Tightening it needs a station
measurement of what healthy tiers actually report — see §8. §2.2's defect then cannot arise from the
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

### 4.7 Non-finite evidence fails, always

⛔ A criterion whose evidence arrives as NaN or infinity **fails**. It never
passes, and it never raises.

Python makes this a trap rather than a nicety: `nan < x` and `nan > x` both
evaluate False, so a naive threshold test admits a NaN silently. Measured on
2026-09-19 against the first implementation of §4's battery — a six-block run
with `fold_retention` NaN and every other field healthy returned a PASS. Four
of the seven criteria leaked that way; a fifth raised `ValueError` instead.

One helper enforces the rule for every criterion, so a criterion added later
inherits it rather than re-learning it. The principle matches §4.6 for
split-half and §5.2 for the tier, and it is the same sentence each time:
evidence the fold could not compute is not evidence that the fold is healthy.

⚠ The masking is worse than the leak. `max()` over a list holding NaN returned
a *finite* value at four of six block positions, so the unimodality leak
produced a PASSING test when the NaN sat in the wrong place. A test written
against one position would have certified the hole. Sweep the whole run.

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

⛔ **Sweep fold position as well as C/N0.** C/N0 is not the only axis a
criterion can depend on. Where the edge lands inside the folded second is set
by where the stream started, so it changes on every boot and says nothing about
the station — and the original sweep held it fixed at 47916 throughout, for
every criterion. That single choice hid the |T| defect of §4.2 completely: the
statistic was position-dependent by a factor of 410, and the one position the
sweep drove was the one where the dependence vanishes. Any re-derivation must
drive a spread of positions — near the fold origin, near its end, and several
between — for both the healthy and the null arm, and confirm the separation at
each. A criterion that survives a C/N0 sweep has been checked on one axis only.

### 4.6 The battery reads the fold, not individual seconds

⚠ The numbers in §4's table come from per-second estimates on captured IQ. The
battery evaluates at **fold-block** granularity, because that is what the
production path produces: `BpskEdgeFineStage` emits one `FineEdgeEstimate` per
`fold_seconds` block, not one per second.

The mapping, criterion by criterion:

| | Per-second form (measured) | Fold-block form (implemented) |
|---|---|---|
| 2 | inter-edge Δ = the sample rate | consecutive block edges differ by an **integer multiple** of `fold_seconds × sample_rate` — see below |
| 3 | per-second estimates at one position | consecutive block estimates agree — `T6Battery.evaluate` computes its **own** max-deviation-about-the-median over the positions it retains, against `max_unimodality_spread_ms = 0.25`. It reuses only the *block count* from `BOOTSTRAP_CONFIRM_BLOCKS` (as `HISTORY_REQUIRED_BLOCKS`); the stage's bootstrap-confirmation statistic itself is neither surfaced nor called, and its tolerance is 4× looser |
| 5 | per-second sd (95 ns real IQ, 490 ns swept) | block-to-block sd, predicted as the per-second sd ÷ √K — then floored at 1.0 ms, see §4.3 |

Criteria 1, 4, 6 and 7 already evaluate on the folded block and need no
mapping.

⛔ **Criterion 2 measures each gap against its own nearest multiple of the fold
period, not against one fold.** `BpskEdgeFineStage._finish_block` returns None
for a block whose registration spread exceeds its limit, so the battery is never
called for that block and the next `evaluate` sees a gap of two folds. Measured
against one fold that reads as an error of a whole fold period — 2,880,000
samples at K = 30, 96 kHz — and because the criterion takes the worst gap over
the whole retained history (`keep = 8`), **one** discarded block marked a
running T6 suspect for about seven further evaluations, roughly 3.5 minutes.

A dropped block is not a ruler fault. What this criterion exists to catch is the
pulse source walking against the ADC, and a walk shows up as a gap that is *not*
a whole number of folds. So the residual is taken to the nearest multiple and a
gap shorter than half a fold is still held to one fold, so a too-short gap still
fails. This does not blunt the criterion: with a 2,880,000-sample period and a
2-sample tolerance, an arbitrary gap lands within tolerance of some multiple
with probability about 1.7 × 10⁻⁶. `criteria["ruler_folds_skipped"]` carries how
many folds the worst gap spanned.

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

⛔ **"Still accumulating" is not "suspect".** The authority captures its anchor
on block 1, while criteria 2 and 3 refuse to report before
`HISTORY_REQUIRED_BLOCKS` and criterion 5 cannot state the fold's scatter below
two retained positions. So every cold start emitted `T6 SUSPECT: ruler,
unimodality` and stamped `t6_suspect_criteria` into the authority snapshot for
two blocks — a fault report for a battery doing exactly what it is supposed to
do. An alarm that fires on every healthy start teaches the operator to ignore
alarms. Fixed 2026-09-19 (`t6_battery.is_still_accumulating`): a verdict failing
*only* on criteria that cannot yet report, while the battery is below
`HISTORY_REQUIRED_BLOCKS`, is **accumulating** — no suspect mark, no alarm, a
DEBUG line. Criterion 5 counts as unreportable only while its evidence is
non-finite; a finite σ over its ceiling, or any other criterion, marks suspect
at once.

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

**Measure what a healthy tier actually reports.** ⚠ The 1.0 ms sigma floor sits
pinned to B4's recorded hourly `t6_sigma_ms` of 0.003–0.07 ms — a figure taken
from the station record, not measured during this work. Read it off the station
and confirm it. Should a healthy B4 hour ever exceed 1.0 ms, the floor must
rise and criterion 5 loses what discrimination it retains.

⚠ **Confirm the thresholds against real captures.** Every number in §4 comes
from band-limited BPSK plus additive Gaussian noise. That model carries no
multipath, no AGC excursion, and no registration jitter. The separations it
establishes are real; their margins on a real antenna are not yet known.

⚠ **`min_fold_retention` = 0.50 puts T6's floor near 43.7 dB-Hz, and B4 holds
4.7 dB above it.** Real room, not generous. Do not raise that threshold without
re-measuring B4 first.

⚠ **Clear the 13 failing tests in `test_core_recorder_t6_step_recovery.py` and
`test_core_recorder_t6_fine_integration.py` first.** They predate this work —
one fixture hands a `MagicMock` calibrator into an `int >= mock` comparison —
but they now mask the regression signal in exactly the file this design touches
most. A change that broke step recovery would read as "12 failures instead of
11", which nobody will notice.

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
