# Newell's detector and the fold — what each buys, and what neither does

**Date:** 2026-09-19
**Status:** Measured; synthetic only (see §6)
**Companion to:** `T6_EDGE_METHODS_COMPARED.md` §8b and §8c, which measured
these on captured IQ from one station at one signal condition. This note adds
the thing a capture cannot supply: **known truth**.

---

## 1. Why this comparison exists

Scott Newell's `wd-record` finds the TS-1's polarity flip with a per-sample
phase-step state machine — no carrier recovery, one sample of resolution, a few
operations per sample in C. On DASI-009.AI6VN it placed **every** edge, 128 of
128, at a single bit-identical position, while our matched-filter chain accepted
one good edge every 27 seconds (§8b).

That result reads as a rout, and it invites an obvious conclusion: drop the
expensive chain and take the cheap detector. This note tests that conclusion and
finds it wrong — for reasons that have nothing to do with which detector finds
more edges.

## 2. The two approaches

**Newell alone.** Differentiate the phase sample to sample; accept a jump near
π. The edge lands on an integer sample index. No interpolation, no averaging,
no model of the transition's shape.

**The fold.** Average many seconds of the channel modulo one second, then fit
the transition inside the averaged result and report a *sub-sample* position.
Our shipped path (`BpskEdgeFineStage`) folds complex baseband with per-second
sign alternation, derotates, and fits the zero crossing. The carrier-free
variant of §8c folds `|diff(x)|` and interpolates the peak parabolically.

The difference that matters sits in what each can express. Newell answers in
whole samples. The fold answers in fractions of one.

## 3. Method — and why synthetic

⚠ **Accuracy needs truth, and a capture has none.** On recorded IQ you can
measure how well estimates agree with each other, and how tightly they repeat.
Both are *precision*. Neither tells you whether the answer sits where the edge
actually was.

So the measurement below drives synthetic signal whose edge position is known
exactly, and sweeps that position across **twelve sub-sample phases**. That
sweep carries the whole argument: a quantised estimator is perfectly repeatable
at any one phase and wrong by a different amount at each.

`tools/t6_fold_discriminant_bench.py` supplied the Newell and magnitude-fold
implementations; `BpskEdgeFineStage` is the shipped production stage,
unmodified.

## 4. What the measurement says

Error against known truth, in microseconds. One sample spans 10.4 µs.

| C/N0 | K | Newell sd | Newell RMS | magdiff+fold sd | RMS | **fine stage sd** | **RMS** |
|---|---|---|---|---|---|---|---|
| 77.0 | 30 | 3.150 | 5.367 | 0.606 | 5.262 | **0.125** | **0.130** |
| 60.0 | 30 | 3.400 | 6.968 | 3.271 | 6.235 | **0.808** | **0.848** |
| 48.4 | 30 | *no detection* | — | *no detection* | — | **2.423** | **2.591** |
| 48.4 | 60 | *no detection* | — | *no detection* | — | **1.478** | **1.689** |

Three readings, in order of importance.

**The fold is the only approach that survives B4's worst hour.** At
48.4 dB-Hz — measured on B4, 2026-08-28 — both per-sample detectors fail to
find the edge at all, while the shipped stage holds 2.4 µs and improves to
1.5 µs on a 60 s fold. The ×1.64 improvement across that doubling tracks the
√2 = 1.41 the fold's processing gain predicts.

**The fold delivers sub-sample precision, as expected.** 0.125 µs at 77 dB-Hz
is **0.012 of a sample**. Newell's 3.150 µs does not improve with K at all —
same number at K = 1 and K = 30.

**Precision and accuracy are earned separately.** Compare sd against RMS.
For Newell and for magdiff+fold, RMS far exceeds sd: a **+5.2 µs bias**
dominates, which is half a sample, and which both inherit from attributing a
difference between samples *i−1* and *i* to index *i*. The fine stage's RMS
(0.130) barely exceeds its sd (0.125) — its zero-crossing fit places the
transition rather than the operator that found it.

⚡ So the fold improves precision. Only the **fit** removes the bias.

## 5. Why Newell's 128/128 and its 3.15 µs are the same fact

§8b's headline — one bit-identical position across 128 consecutive edges, zero
scatter — and this note's 3.150 µs sit in apparent contradiction. They agree.

A quantised estimator repeats **perfectly** at a fixed edge. Every one of those
128 edges sat at the same sub-sample phase, so every answer rounded the same
way. What §8b measured was repeatability, and repeatability was perfect.

Move the edge through a sample and the answer steps. Across a uniform sweep the
error has RMS 1/√12 = 0.289 samples = **3.01 µs**; measured, 3.15.

⛔ **The consequence is the point of this note.** That error is a *fixed
offset*, not noise, so averaging cannot reduce it. A thousand more edges buy
nothing. A perfectly repeatable wrong answer stays wrong, and its repeatability
is exactly what hides it.

## 6. What this does not establish

⚠ **All synthetic.** Band-limited BPSK plus additive Gaussian noise — no
multipath, no AGC excursion, no registration jitter, no real receiver. The
*separations* are real; their margins on a real antenna are not yet known.

⚠ **One edge position family.** The sweep moves the edge within a sample but
holds it near mid-fold. Criterion 4's fold-position dependence (see
`T6_ACCEPTANCE_CRITERIA.md` §4.2) showed how badly a statistic can behave away
from where it was calibrated.

⚠ **`magdiff+fold` is not the shipped path** and should not be read as a
verdict on it. It appears here because §8c measured it on real IQ, which makes
it the bridge between that capture and this sweep.

⚠ **Newell's detector was not built for this.** `wd-record` aligns recordings
to the PPS, and for that purpose one sample of resolution is ample and its
cheapness is a virtue. Nothing here criticises it at the job it does.

## 7. Bearing on metrology

The measurand is a *registration*: which UTC instant a sample carries.

A quantisation bias of ±5.2 µs sits **50× above** T6's per-tick floor. It does
not average down, it does not announce itself, and every consistency check
passes it — the estimator agrees with itself perfectly. This is the case
`MEASUREMENT_MODEL.md` exists to keep separate: precision is not accuracy, and
a self-consistent instrument can be self-consistently wrong.

⚡ The fold earns the precision; the sub-sample fit earns the accuracy. A
station running Newell alone would report a σ near zero and carry a systematic
error two orders of magnitude larger than that σ — an uncertainty statement
that is not merely optimistic but structurally misleading.

For metrology, then: fold, and fit. The fit is not a refinement of the fold.
It is the half that removes the bias.

## 8. Bearing on physics timing

The physics products live in `PHYSICS.md`, and none of them observes the T6
path — no ionosphere sits in front of an injected pilot. T6 supplies the
*registration against which* propagation measurements are read. So the question
becomes: where does a 5.2 µs registration bias survive, and where does it
cancel?

**It cancels in same-station differentials.** dTEC compares carrier phase
across frequencies at one receiver; Doppler takes a phase slope within one
minute at one receiver. A constant offset in the station's clock enters both
terms and subtracts out. A station running Newell alone would still produce
sound dTEC.

**It does not cancel across stations.** Multi-station fusion, any time-of-flight
work, and cross-site comparison all difference *between* receivers, where each
station's own registration bias enters with its own sign and magnitude. Two
stations quantising differently — because their edges sit at different
sub-sample phases — differ by up to one full sample, 10.4 µs, with nothing in
either station's self-checks to reveal it.

Against the recorded tolerances — same-site WWV bands agreeing to ~1 ms,
cross-site to ~4 ms — 10.4 µs sits comfortably inside the noise *today*. That
is a statement about where HF propagation measurement currently stands, not a
licence: the whole purpose of a ns-class tier is to stop being the limiting
term, and a bias that no self-check can see is the wrong thing to carry into a
measurement that is getting better.

⛔ And one place it matters now: **absolute** arrival timing. Mode
identification fits an observed delay against geometric path lengths. That fit
reads absolute delay, not a differential, so a per-station registration bias
lands directly in the residual the mode selection minimises.

## 9. What to measure next

1. **Repeat on captured IQ, both stations.** §8c's 89 s capture gives precision
   against real signal; the truth for accuracy must come from an independent
   reference on the same edge.
2. **Sweep fold position as well as sub-sample phase.** §4.2's lesson, applied
   here.
3. **Measure the cross-station term directly.** Two stations, one TS-1 epoch,
   the difference of their registrations. That is the number §8 of this note
   argues about and nobody has yet put a value on.

---

## Summary

| | Newell alone | Fold + fit (shipped) |
|---|---|---|
| Resolution | one sample, 10.4 µs | sub-sample |
| Repeatability at a fixed edge | perfect | 0.125 µs at 77 dB-Hz |
| Error across sub-sample phase | **3.01 µs, irreducible** | falls as √K |
| Bias | +5.2 µs, half a sample | ~0 |
| Works at B4's 48.4 dB-Hz worst hour | no | yes, 2.4 µs at K=30 |
| Cost | a few ops per sample, in C | a Python service, tens of ms per batch |

Newell's detector finds the edge better than our chain did. It cannot say
*where* the edge is to better than a sample, and no amount of repetition
improves that. The fold buys precision; the fit buys accuracy; and at the signal
condition that actually governs a real station, only the folded path finds the
edge at all.
