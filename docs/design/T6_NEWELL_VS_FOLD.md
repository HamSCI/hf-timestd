# Newell's detector and the fold — what each buys, and what neither does

**Date:** 2026-09-19
**Status:** Measured on synthetic sweep (§4), on captured B4 signal (§5d),
and live on B4's injected pilot (§5f, §5g, 2026-09-22)
**Companion to:** `T6_EDGE_METHODS_COMPARED.md` §8b and §8c, which measured
these on captured IQ from one station at one signal condition. This note adds
the thing a capture cannot supply: **known truth**.

---

## 1. Why this comparison exists

Scott Newell's `wd-record` finds the TS-1's polarity flip with a per-sample
phase-step state machine — no carrier recovery, one sample of resolution, a few
operations per sample in C. On DASI-009.AI6VN it placed **every** edge, 128 of
128, at a single bit-identical position.

That result reads as a rout, and it invites an obvious conclusion: drop the
expensive machinery and take the cheap detector. This note tests that
conclusion and finds it wrong — for reasons that have nothing to do with which
detector finds more edges.

⚠ **Three chains, not two, and §8b compared a different pair than this note
does.** §8b measured Newell against the **MF calibrator** — the per-second
Costas-plus-matched-filter path that was then T6's only edge source. This note
measures Newell against the **fold**, which is a separate stage that does not
use the MF calibrator to compute anything. Keeping the two comparisons apart
matters, because the MF calibrator's poor showing in §8b says nothing about the
fold's.

## 2. The chains, drawn

### 2.1 Newell — per-sample phase step

```
  IQ ──► ∠x ──► |Δ∠| between ──► accept jump ──► edge at an
         phase   adjacent samples    near π       INTEGER sample
                                                  (10.4 µs at 96 kHz)

  no fold · no matched filter · no carrier recovery · no interpolation
```

One second of signal yields one answer per edge, quantised to the sample grid.

### 2.2 The MF calibrator — context only, NOT what this note measures

```
  IQ ──► Costas loop ──► matched filter ──► adaptive-threshold ──► per-second
         carrier recovery  0.5 s integration   peak pick            edge
```

Included so the reader can see what §8b actually beat. It still runs, and it
still seeds §2.3 when it locks, but it computes none of §2.3's answer.

### 2.3 The fold + fit — what ships, and what this note measures

```
  IQ ──► accumulate K seconds ──► derotate ──► locate the ──► zero-crossing
         modulo one second        by SQUARING  transition      LINEAR FIT
         (sign-alternated)        φ=½∠⟨avg²⟩       │                │
                                                   │                ▼
                          ┌────────────────────────┤          SUB-SAMPLE
                          │            │           │           position
                     "seeded"     "tracking"  "bootstrap"
                   MF supplies   its own last  boxcar MF ON
                   the window    position      THE FOLDED SECOND
                   (optional)    (optional)    (self-sufficient)

  no Costas of its own · MF optional · processing gain 10·log10(K)
```

⛔ **The fold path needs neither Newell nor the MF calibrator.** In bootstrap
mode it finds its own edge, with a boxcar matched filter run once over the
*folded* second — a different object from §2.2's matched filter, which
integrates 0.5 s of *unfolded* signal per edge.

### 2.4 Newell + the fold — the hybrid that does not exist, and why

```
  IQ ──► Newell ──► K integer ──► average ──► the SAME integer
                    detections                (see §5b)
```

The obvious hybrid: keep Newell's cheap detector, fold its answers for
precision. §5b measures it. It buys nothing, and the reason is instructive.

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

## 5b. Folding Newell's answers buys nothing — measured

The hybrid of §2.4, driven the same way: take every Newell detection in a run
and average them.

| C/N0 | K | one detection | averaged over the fold | distinct positions per run |
|---|---|---|---|---|
| 77.0 | 1 | 5.367 µs | 5.367 µs | **1.00** |
| 77.0 | 30 | 5.367 µs | 5.560 µs | 1.33 |
| 60.0 | 30 | 217 ms | 29 ms | 45.67 |

⚡ **At 77 dB-Hz there is nothing to average.** One distinct position per run —
thirty detections, all the same integer. The mean of thirty identical numbers
is that number, so the fold has no purchase and the error is unchanged.

**Averaging needs dither.** A fold reduces scatter by averaging *variation*
around a truth. A clean per-sample detector produces no variation: it rounds
the same way every time, because the edge sits at the same sub-sample phase
every time. Its perfect repeatability — the very thing that made §8b's 128/128
so striking — is exactly what forecloses improvement.

The 60 dB-Hz row shows the other regime and is not a counter-example. Forty-six
distinct positions appear because noise is producing false detections; averaging
pulls the mean toward the middle of that spread, from 217 ms to 29 ms, and the
answer stays useless. Averaging noise does not recover an edge.

⛔ So a quantised detector cannot be folded into sub-sample accuracy. The
sub-sample answer has to come from a stage that *models the transition* —
§2.3's linear fit through the zero crossing — rather than from repeating a
stage that only ever reports which sample the edge fell in.

## 5c. The per-sample floor belongs to the channel, not to the method

⚠ **96 kHz came from a configuration choice.** We ask radiod for the T6
channel at that rate — wide enough for the pulse and no wider — and every
per-sample figure in §4 inherits it. A quantised estimator errs by at most half
a sample, so its floor tracks the sample period exactly:

| Channel rate | One sample | Per-sample floor, T/√12 |
|---|---|---|
| 48 kHz | 20.833 µs | 6.014 µs |
| **96 kHz** | **10.417 µs** | **3.007 µs** ← as configured |
| 192 kHz | 5.208 µs | 1.504 µs |
| 384 kHz | 2.604 µs | 0.752 µs |

The folded chain does not sit on this ladder. Its precision comes from
processing gain and the transition's shape, not from the sample grid.

⚡ So why not sample faster and keep the cheap detector? Matching the fold's
0.130 µs at 77 dB-Hz by rate alone wants a 0.45 µs sample period — **2.2 MHz**,
about 23× the channel we run, for a pulse occupying ±25 kHz. Twenty-three times
the samples through every stage downstream, to arrive where 30 s of folding
already stands.

⛔ And the *character* of the error survives the climb. A higher rate shrinks
the quantisation step; at any rate the residual stays a fixed offset set by
where the edge falls between two samples, invisible to every self-check, and
untouched by repetition. Sampling faster buys a smaller irreducible error, never
a reducible one.

## 5d. On real signal — B4, two band conditions, 2026-09-20

§6 below opened by conceding that everything above ran on synthetic signal.
That concession no longer holds. Two captures of B4's own TS-1 channel now
carry the comparison onto real samples.

Both methods read the **same bytes**. A capture supplies no truth, so what
follows measures precision and mutual agreement, never accuracy.

| | dawn, live | night, stored |
|---|---|---|
| captured | 2026-09-20 11:34:28Z, 120 s | 2026-09-11 04:00Z, 60 s |
| band | grey line, HF opening | quiet |
| source | passive subscriber, SSRC 2072147062 | `t6-anomaly` ring, zero-fill trigger |
| gaps | 0 packets lost | — |

### What the two methods reported

| | dawn | night |
|---|---|---|
| Newell raw candidates | 8809 (**73.4/s**) | 892 (**14.9/s**) |
| Newell, unaided, per-pulse sd | 6178 samples | 19176 samples |
| Newell, ±30-sample tracking gate, sd | 2.304 samples = **24.0 µs** | 0.132 samples = **1.38 µs** |
| seconds yielding an edge | 109/120 | 56/60 |
| fold, block-to-block sd (K=30) | 0.0657 samples = **0.684 µs** | 0.0251 samples = **0.261 µs** |
| fold retention | 0.96–0.98 | 0.967 |
| precision ratio, Newell ÷ fold | **35.1×** | **5.3×** |

### Three things the captures show that the sweep could not

**Newell's 128/128 does not transfer between stations.** On AI6VN the detector
placed every edge at one bit-identical sample. On B4 it fires **73 times a
second** at dawn against one real edge, and an unaided search over the whole
second lands nowhere useful — sd of 6178 samples, which names no edge at all.
Only a tracking gate that already knows where to look recovers a usable answer,
and a gate needs something else to acquire it first. AI6VN's result measured
that station's signal-to-background, not the algorithm's portability.

**Band condition dominates the per-sample detector and barely touches the
fold.** Between quiet night and grey-line dawn Newell's gated scatter degrades
**17×** (1.38 → 24.0 µs). The fold degrades **2.6×** (0.261 → 0.684 µs). The
detector that integrates nothing inherits the whole diurnal swing; the one that
folds 30 seconds absorbs most of it.

**The half-sample bias §4 predicted appears on real signal.** §4 derived it from
first principles: Newell attributes a difference between samples *i−1* and *i*
to index *i*, so its answer lands half a sample late. The captures measure the
fold minus Newell at **−0.4334** and **−0.5419** samples, bracketing the
predicted −0.5. A synthetic prediction reproduced on captured HF. Nothing in
either station's self-checks would have surfaced it.

### What the captures say about the acceptance battery

Running the shipped `BpskEdgeFineStage` over real samples exercised the
evidence fields that `T6_ACCEPTANCE_CRITERIA.md` §4 reads. Three of them carry
less information than their own comments claim.

⛔ **`split_half_delta_samples` resolved only whole samples — since fixed.**
`_split_half_delta` located each sub-fold's apex with `np.argmax(np.abs(t))`
and interpolated nothing, so it could report only integer differences. Across
six blocks it read `0.0000` five times and `−1.0000` once. Criterion 6 exists to
catch the wandering apex behind B4's ~270 tier transitions a day, and it policed
that wander at **10.4 µs resolution while the fold's own block-to-block scatter
runs 0.68 µs** — roughly fifteen times finer than the check watching it. The
criterion could fire only once the wander exceeded half a sample.

The stage now refines each sub-fold's apex with `_fit_edge`, the same
zero-crossing localiser that produces the reported estimate, so the difference
lands in the same units as the scatter it polices. The same six blocks now read
0.028–0.308 samples, against the √2 × block-scatter the geometry predicts
(0.13 dawn, 0.05 night).

⚠ **One wrong way to write that fix, worth recording.** Seeding *both*
sub-folds from a single shared search centre is the obvious simplification, and
it silently destroys the criterion. Pinned to the same narrow window, two folds
of **pure noise** agree to 0.1–0.9 samples — inside the healthy range at
48.4 dB-Hz, so noise stops being distinguishable from signal. Each sub-fold
must find its own apex by a global search first; two noise folds then land
765–39190 samples apart, and that separation is what refuses the null. The
criterion buys its resolution from the local fit and its discrimination from
the global search, and it needs both.

The threshold stayed at 10 samples. It clears the worst healthy reading
(7.37 at 44 dB-Hz) and sits 76× below the nearest noise block. Note what
that bound now rests on: the healthy spread at low C/N0, not the sample
grid. A wander smaller than that spread still passes — but the reported
*value* now carries it, where before it was pinned at exactly `0.0000`.

⚠ **`transition_width_samples` is quantised to whole sample intervals.** The
code sets `width = hi − lo`, the span of the fit bracket, which widens only
while neighbouring samples stay inside 0.4 of the plateau amplitude. It read
**1.00 on every block of both captures**.

That is the right quantity, coarsely measured — not the wrong one. The folded
samples across B4's edge run

```
dawn    −0.929  −1.125  −0.960  [ −0.142  +0.797 ]  +1.176  +1.043
night   −0.931  −1.182  −1.160  [ −0.427  +0.602 ]  +1.130  +1.043
```

so the transition really does cross in about two sample periods, against the
1.92 that 1/(2B) predicts for a ±25 kHz channel. Typically one sample lands
inside the band, so the bracket spans one interval and the field reports 1
where the physics says 1.92. Widening the band to 0.98 moved the dawn reading
to 2 and left the night reading at 1 — the grid, not the statistic, sets the
resolution.

⛔ The consequence is narrow but real: the field cannot separate a correct edge
from one twice as wide, because both round to the same small integer. Its
threshold comment already says what it is — "a PHYSICS BOUND, not a
discriminator", with 30 samples aimed at a fit containing no transition at all
— so the battery does not lean on it for discrimination. The field's own
comment in `FineEdgeEstimate`, predicting about 2 samples, reads as a promise
the statistic cannot keep at this rate, and the two comments should be
reconciled.

⚠ **`fit_rms` approaches zero by construction, and the battery never reads
it.** With the bracket at two points, `np.polyfit(…, 1)` fits a line through
two points exactly; every block reported ~1e-13. It was introduced with the
zero-crossing localiser as a fit-quality diagnostic — does a straight line
actually describe this transition region, or is the stage fitting noise? — and
it is surfaced in the status JSON, never consumed as a criterion. The intent is
sound; it goes unserved because the bracket almost never holds more than two
points. Both fields therefore trace back to one cause: at 96 kHz the edge is
about two samples wide, so there is barely any transition region to characterise.

The other evidence fields did vary and did carry signal: the triangle residual
moved across 0.000065–0.000779, apex distance across 0.535–0.965 samples, and
retention across 0.960–0.982.

⚡ So B4 passing all seven criteria overnight read as weaker evidence than the
count suggested — though less weaker than a first pass suggested. Criterion 6
was genuinely blind and now resolves what it polices. The width field is
honest but coarse, and the battery already treats it as a bound rather than a
discriminator. `fit_rms` is telemetry, not a criterion. The shared root is that
a ±25 kHz channel at 96 kHz gives an edge about two samples wide, which leaves
almost no transition region to characterise — a sampling-rate consequence
(§5c), not a defect in either statistic.

## 5e. Widening the channel — measured, B4, 2026-09-20

§5c argued that a faster sample rate buys a smaller irreducible error and never
a reducible one, and concluded that matching the fold by rate alone was absurd.
That argument stands, and it turns out to have been answering the wrong
question. The lever is not the sample rate. It is the **channel bandwidth**,
and it was already paid for.

B4 runs the T6 channel at ±25 kHz inside a 96 kHz complex channel that admits
±48. A spectrum of the capture shows why that matters: the filter is a brick
wall, in-band at −42.5 dB and −166 dB by 30 kHz, which is below what complex64
can represent. Everything the estimator could use, it already has — **and half
the available band is switched off.**

### Method

`ka9q-python` created a second channel at 45.375 MHz, identical to the
production one in rate, preset, gain and encoding, differing only in filter
edges (±45 kHz). Both were subscribed simultaneously, folded on-station by the
shipped `BpskEdgeFineStage`, for thirty minutes.

⛔ **Let the library own the SSRC and the destination.** A first attempt passed
a hand-picked multicast address. `allocate_ssrc` hashes the destination but
*not* the filter edges, so an explicit address is both unnecessary and load-
bearing in ways that are easy to get wrong: the run returned full-scale samples
with no 1 Hz structure and a fold retention of 0.174 — which is 1/√30, the
signature of folding thirty uncorrelated seconds. Constructing
`RadiodControl(client_id=…)` derives both (CONTRACT v0.3 §7), and the pair then
came back matched at 1.04e−5 against 1.06e−5 mean amplitude. A bandwidth
experiment that also moves the gain measures neither.

### Result — 60 fold blocks a side, zero gaps, zero packets lost

| | ±25 kHz | ±45 kHz |
|---|---|---|
| 10–90% edge rise | 18.23 µs | **10.42 µs** |
| 1/(2B) predicts | 20.0 | 11.1 |
| block-to-block sd | 0.510 µs | **0.390 µs** |
| MAD | 0.256 µs | 0.203 µs |
| fold retention (median) | 0.973 | 0.952 |

**Timing scatter improves 1.309×, against the 1.342 that √(45/25) predicts.**
F = 1.713 on 59 and 59 degrees of freedom, p = 0.041, with the sd ratio's 95%
interval running 1.012–1.693. The prediction sits comfortably inside it.

Three consequences.

**The σ_t ∝ 1/√B model holds for this signal.** Bandwidth buys precision, and
it buys it at the rate theory says. Nothing about the sample rate changed.

**The TS-1 is still not the limiter.** At ±45 kHz the rise tracks 1/(2B) to
within 6%, so the injector's own transition is faster than 10.4 µs. Headroom
remains above this.

**25–45 kHz holds no interference**, at least at this hour: −42.6 dB against
the in-band −42.5. Widening admits noise, not signals.

### What this does not establish

⚠ **One band condition.** Thirty minutes, mid-morning. The dawn capture in §5d
showed Newell's scatter degrading 17× between quiet night and grey line while
the fold moved 2.6×; admitting 20 kHz more spectrum plausibly interacts with
that. A clean result here argues for repeating at local midnight, not for
stopping.

⚠ **p = 0.041 is one run.** The point estimate matches theory well, but the
interval's lower bound sits at 1.012 — the evidence excludes "no effect" only
narrowly.

⚠ **Nothing here measures the calibration cost.** radiod's channel-filter group
delay *is* the T6 chain-delay constant — 16.618 ms on B4, IQR 1.41 ms — and it
follows the filter width. The experimental channel never fed the authority, so
this run says nothing about the new value. Any real change to a station's T6
filter has to re-measure it from `shadow_residuals.T6.shadow_residual_ns` over
~15 minutes immediately afterwards. That, not CPU, is what the change costs.

### An independent confirmation of the criterion-6 defect

The station folded with `6c2bbd0`, before the split-half fix. Across all 120
blocks, both filters, `split_half_delta_samples` read a median of **exactly
0.00000** — the integer-argmax behaviour of §5d reproduced on twenty times the
sample. `transition_width_samples` likewise read 1.00 throughout, including on
the wide channel whose edge spans a single sample period.

## 5f. Choosing by the job — a cut, or a measurement

The two detectors answer to two different jobs, and which one wins turns on a
single question: **does the act of using the answer quantise it?**

### Starting a recording on a UTC minute — Newell

A file begins at a sample. Nothing lets it begin between two. So the act
quantises the answer, and any fraction we computed goes in the bin the moment
we use it. Newell supplies exactly what the job wants: one integer position,
no interpolation, and no fold to fill first. It decides on a single pulse,
where the folded chain waits K blocks before it says anything at all. On B4
over 2026-09-22 it returned one distinct position, cycle after cycle.

⚡ **Quantise the act, never the record of it.** Starting on a sample boundary
does not oblige us to *describe* the start that way. Begin at the integer
sample, then write the residual offset into the sidecar at full sub-sample
precision — `start_rtp_timestamp`, `starting_offset`, `pipeline_offset_samples`
already carry it. A consumer can then place the first sample against UTC far
more closely than the cut itself allowed. Losing the fraction is a property of
where we cut, not of what we know.

### Measuring time of arrival or time of flight — the fold

Nothing here forces a grid. The deliverable is a number, so interpolation stops
being a luxury and becomes the point. Measured on B4, 2026-09-22, on the
injected pilot:

| | scatter |
|---|---|
| nearest-sample pick, 96 kHz (Newell's granularity) | 0.289 samples = 3.007 µs |
| magdiff fold + fit, measured over 14 cycles | **0.034 samples = 0.36 µs** |

About 8× better. And on TS-1 that scatter accounts for the whole error, because
the pilot enters the chain immediately ahead of the RX888 — the edge does not
fade, hop or walk. Truth stands flat, so repeatability *is* accuracy, with no
real variation hiding inside the spread.

⚠ The two methods disagreed by a constant **−1.883 µs** across those cycles.
On a constant input one of them owns that offset outright. Newell cannot
adjudicate it: an integer detector holds no opinion about a fraction of a
sample.

### The floor, and where it actually binds

§5c sets out the per-sample ladder and why climbing it costs 23× the samples to
reach where folding already stands. The distinction this section adds:

**The sample rate floors the cut. The bandwidth floors the knowledge.**

A quantised decision inherits T/√12 and cannot do better — 3.007 µs on the
96 kHz pilot channel, and **12.028 µs on the 24 kHz WWV metrology channels**,
which carry the propagation we actually want to measure. An estimate inherits
no such floor: it answers to bandwidth and SNR, falling as 1/√B and 1/√SNR,
with K looks adding 1/√K. §5e measured the bandwidth term directly — widening
±25 kHz to ±45 kHz, a factor 1.8, improved scatter 1.31×, against √1.8 = 1.34.
The rate did not move.

Sample rate still earns its place, as conditioning rather than as information.
It must clear Nyquist for the bandwidth, and it must put enough points on a
transition that rises in about 1/(2B) for the fit to determine itself. At
96 kHz on a ±25 kHz channel we oversample 1.92×, which is why a three-point
parabola behaves.

## 5g. Scott's two-second period on a carrier-free discriminant — the plan

`wd-record` carries `--leaky-folding` (off by default), which folds over
**two seconds** rather than one and averages with an exponential, not a boxcar:

```c
acc[i] = acc[i]*filter + sample;          // filter defaults to 0.99
out    = acc[i]*(1 - filter);
i      = (i + 1) % (samprate * 2);        // TWO seconds
```

### Measured, B4, 2026-09-22 — the period alone does not rescue it

Reproduced faithfully in Python and run beside the other discriminants on
identical samples:

| discriminant | peak / median |
|---|---|
| magnitude difference, boxcar fold | **3.50** |
| leaky 2 s fold (Scott's) | 1.20 |
| complex fold (our current path) | 1.21 |

The leaky fold forms no peak, and lands statistically on top of the complex
fold it was meant to improve.

### Why — two cancellations, not one

The chain suffers **two** distinct cancellations, and the two-second period
addresses only the first.

1. **Alternation.** A BPSK pulse that inverts on alternate seconds averages
   itself away in a one-second fold. Folding over two seconds keeps it. Scott's
   period is correct, and this mechanism is real.
2. **Carrier.** Residual carrier rotates the phase between blocks, so a
   *complex* accumulation cancels regardless of period. Scott's accumulator
   holds complex samples, so it inherits this exactly as our complex fold does.

The second dominates, which is why fixing the first changed nothing measurable.

### The plan

Put Scott's period under a discriminant that carries no carrier: fold the
**magnitude difference** over two seconds instead of one. The magnitude
difference already survives without carrier recovery (§5d, and 3.50 above), and
the two-second period preserves the alternation it currently averages across.

Falsifiable, and cheaply: if the two mechanisms act independently, a 2 s magdiff
fold should hold its peak ratio near the 1 s figure while gaining the
alternation now discarded. If the ratio instead collapses toward 1.2, the
carrier explanation was incomplete and something else cancels it.

⚠ Keep the exponential out of it unless we want its memory. At filter 0.99 the
time constant runs 100 periods — 200 s — so a transient contaminates for
minutes and a gain step is carried forward with decaying weight. A boxcar over
K blocks at least states its window.

### ⛔ Two bugs in the C, patched but not shipped

Both follow from one line pair:

```c
static float complex acc[32000];             // fixed
acc_i = (acc_i + 1) % (sp->samprate * 2);    // rate-dependent
```

1. The array fits exactly at 16 kHz and overruns above it. At our 96 kHz the
   index reaches 191,999 — 160,000 entries past a static array.
2. `acc` and `acc_i` sit `static` inside `bpsk_state_machine(struct session *sp,
   …)`, a **per-session** function, so every SSRC shares one accumulator and one
   index.

Patch (2026-09-22) moves both into `struct session`, sizes from
`sp->samprate * 2`, frees in `close_session`. It applies cleanly to upstream
`401992cd`. Not built and not installed anywhere — worth sending to Phil and
Scott rather than carrying.

## 5h. The magnitude difference runs exactly half a sample late

⚠ A bench result, not a product one — but it explains a number that looked
like an open question for a day, so it belongs in the record.

The 2026-09-22 overnight comparison reported Newell and the magnitude
difference sitting a steady **1.74 µs** apart on B4, stable all night across
14 dB of gain movement. On a pilot whose position cannot move, that reads as
an unresolved disagreement. It is not one.

Swept against synthetic truth:

| true fraction | magdiff − truth | magdiff − Newell |
|---|---|---|
| 0.00 | +0.5000 | +5.208 µs |
| 0.20 | +0.5273 | −2.848 µs |
| 0.40 | +0.5156 | −0.895 µs |
| 0.50 | +0.5000 | 0.000 µs |
| 0.60 | +0.4844 | +0.895 µs |
| 0.80 | +0.4727 | +2.848 µs |

⛔ **A constant +0.5000 sample.** `np.abs(np.diff(x, prepend=x[0]))` stores the
difference between samples n−1 and n at index n, so the discriminant is a
filter centred at n − ½ and its feature lands exactly half a sample late. Not
an error in the signal; an error in the discriminant's definition.

⚠ **Plus a ±0.027 sample ripple**, antisymmetric about a half sample —
parabolic interpolation on an asymmetric peak. Ten times the ±0.0078 sample
S-curve the shipped fit shows (§5i), because the magdiff peak is far less
symmetric than what the shipped chain fits.

Those two account for the 1.74 µs completely. Newell quantises to an integer;
magdiff sits half a sample late plus ripple; the residue between them depends
on where the true edge falls and runs −2.85 to +5.21 µs across one sample.

⚡ **A free by-product.** Inverting it, the measured +1.74 µs implies B4's
edge sits about **0.72** of the way between two samples.

⛔ None of this touches the shipped estimator, which was checked in the same
harness against the same synthetic truth and tracks it to ±81 ns. Any bench
that compares the magnitude difference against another method must subtract
the half sample first.

## 5i. Does the shipped estimator care where the edge falls?

Prompted by Scott Newell's fractional-delay plots and Phil Karn's reading of
them — one continuous sinc, sampled at different points. We apply no
fractional delay anywhere, so that asymmetry cannot arise here, but the
question transfers to any sub-sample estimator.

Swept noise-free across one sample interval, shipped fold-and-fit:

```
frac  0.000  0.100  0.200  0.300  0.400  0.500
err   0.000 +0.061 +0.081 +0.071 +0.041  0.000   µs
frac  0.600  0.700  0.800  0.900  1.000
err  -0.041 -0.071 -0.081 -0.061  0.000   µs
```

A clean S-curve, zero at 0, ½ and 1, peaking at **±81 ns**.

⚠ It does NOT average away. The pilot is injected coherently and the ADC is
GPSDO-disciplined, so the edge holds the same fractional position for hours —
the bias is a constant offset, not a zero-mean wobble.

⛔ We do not correct it, and the reason is not that it is small. It sits below
the **200 ns** type-B uncertainty `timing_chain.json` already declares for
`ts1_modulator_delay` (designer statement, P. Elliott WB6CXC). Correcting it
would assert a precision the injector's own stated uncertainty cannot support.
Harness: `tools/t6_fractional_response.py`.

## 6. What this does not establish

⚠ **§4's sweep ran on synthetic signal** — band-limited BPSK plus additive
Gaussian noise, with no multipath, no AGC excursion, no registration jitter and
no real receiver. §5d since carried the comparison onto two captures of B4's
own TS-1 channel, which confirmed the half-sample bias and the fold's
advantage, and which revealed a station-to-station spread in Newell's
behaviour that the sweep could not have shown. Only B4 has supplied captures;
AI6VN and any third station may differ again.

⚠ **One edge position family.** The sweep moves the edge within a sample but
holds it near mid-fold. Criterion 4's fold-position dependence (see
`T6_ACCEPTANCE_CRITERIA.md` §4.2) showed how badly a statistic can behave away
from where it was calibrated.

⚠ **`magdiff+fold` is not the shipped path** and should not be read as a
verdict on it. It appears here because §8c measured it on real IQ, which makes
it the bridge between that capture and this sweep.

⚠ **The MF calibrator was not benchmarked here.** §2.2 draws it for context
only. §8b measured it against Newell on captured IQ and it lost; this note
neither repeats nor disputes that.

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

⛔ **Do not reach for `SAME_SITE_AGREE_MS` / `CROSS_SITE_AGREE_MS` here.**
Those constants (`registration_acquirer.py`, 1.5 ms and 4.0 ms) separate
corrections derived from different **transmitters** — Fort Collins, Kauai,
Lintong — at ONE receiver, and the 4 ms is independent *path-model* error
between different great-circle paths. The quantity in this section is the
difference between two **receivers'** registrations: an instrument property
that shares none of that budget. An earlier draft quoted the transmitter-side
numbers as though they bounded the receiver-side one. They do not, and the two
axes must not be conflated — transmitter disagreement is propagation, receiver
disagreement is instrument.

What holds without a number: the purpose of a ns-class tier is to stop being
the limiting term, and a bias that no self-check can see is the wrong thing to
carry into a measurement that is getting better.

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
3. **Measure the RECEIVER-to-receiver term directly.** Two stations, one TS-1
   epoch, the difference of their registrations — the number §8 argues about.

   ⚠ Cross-station work on the **transmitter** axis is long-established here
   and already carries measured tolerances: the same/cross-site split in
   `registration_acquirer.py`, multi-broadcast fusion's agreement check across
   all station pairs, and `MULTI_STATION_MLE_DESIGN.md`'s three-station
   superposition. This item is the OTHER axis, and only that one is open.

4. **Fold the magnitude difference over two seconds** — §5g. The cheapest open
   question, and the one with a stated prediction to fail.

5. **Catch the AGC actually stepping.** Running overnight on B4 from
   2026-09-22 03:00Z: four discriminants on identical samples, with radiod's
   own `rf_gain`, `rf_atten` and `input_power_dbm` read passively off the
   status group beside every cycle.

   ⚠ The loop sat still for the first hour — one distinct `rf_gain` value while
   input power wandered 2.15 dB, so the AGC never left its dead zone. Every
   precision figure above therefore describes the QUIET regime. Scott Newell's
   concern is about the other one, and only the dawn enhancement will supply it.

   ⛔ Read `rf_gain`, never the sample RMS. The AGC steers its OUTPUT to the
   midpoint of its thresholds, so output level is the quantity the loop labours
   to hold constant — measured span 0.075 dB while the loop was entirely free
   to move. Measuring the regulated variable of a control loop shows the
   regulation, not the disturbance.

   The mechanism to look for: a gain step INSIDE a fold window weights its
   blocks unequally and biases the centroid, rather than merely adding scatter.
   A diurnal gain pattern would make that bias diurnal. On the injected pilot
   no propagation exists to confuse it with, so anything diurnal there belongs
   to us.

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
| Improves when folded | **no — nothing to average (§5b)** | yes, as √K |

Newell's detector finds the edge better than our chain did. It cannot say
*where* the edge is to better than a sample, and no amount of repetition
improves that. The fold buys precision; the fit buys accuracy; and at the signal
condition that actually governs a real station, only the folded path finds the
edge at all.
