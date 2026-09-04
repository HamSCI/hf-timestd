# The measurement model: one ruler, one registration

**Status:** Approved design, 2026-09-04. Foundational — every other timing
document in this repository derives from it, and §10 records what it supersedes.
**Author:** mjh (the model), written up by Claude from mjh's argument.
**Vocabulary:** JCGM 200 (VIM) and JCGM 100 (GUM). Where this document says
measurand, correction, coverage factor, or Type A / Type B, it means what those
standards mean.

---

## 0 · Why this document exists

The same defect has surfaced under six different names over four months. Each
time we described a contour of it, fixed that contour, and moved on. It returned
because nothing named the thing itself.

### 0.1 The instrument has carried two measurands and never chose between them

`METROLOGY.md` §2.1 states what the system measures:

> `D_clock = T_system − T_UTC(NIST)` — the offset between the local system clock
> and UTC(NIST).

`METROLOGY.md` §3.1, ninety lines later, states the job:

> The system's job is not to straighten the ruler (the GPSDO does that), but to
> use ionospheric physics to pin the ruler's zero-point to UTC.

Those name two different quantities. The first measures a host oscillator
against UTC. The second registers a sample counter against UTC. A station holds
both timebases, they drift apart freely, and no document chooses which one the
instrument exists to measure. Everything below follows from that omission.

### 0.2 One published field carries six quantities

`authority_manager._build_state` publishes `rtp_to_utc_offset_ns`. What that
number means depends on which rung answered:

| rung | the number | measurand |
|---|---|---|
| T6, anchor captured | anchor-derived correction to radiod's map | sample epoch |
| T6, cold start | `local_minus_source_ns`, a matched-filter residual | sample epoch |
| T3 | `d_clock_fused_ms`, a correction to the host clock | D_clock |
| T5 / T4 / T2, anchor-grounded | the probe's measured anchor disagreement | sample epoch |
| T5 / T4 / T2, ordinary case | the integer `0` | an assertion, not a measurement |
| T0 | `None` | absence |

`METROLOGY.md` §4.5 then directs every consumer to apply it without inspection:
*"Because the offset is applied uniformly, no client branches on T-level."* So a
WSPR recorder at an ordinary T4 station adds zero to radiod's mapping and writes
a file whose timestamp asserts that the `(GPS_TIME, RTP_TIMESNAP)` pair reads
correctly — a pair the same document measures, four hundred lines earlier,
disagreeing with itself by up to 816.4 ms across 20,813,617 observations, with
only about 37 % landing at exactly zero.

### 0.3 One document contradicts itself about the host clock

`METROLOGY.md` §4.3 establishes that radiod's `GPS_TIME` evaluates
`clock_gettime(CLOCK_TAI)` offset to the GPS epoch — the host system clock, not
a sample index. §4.5 then builds the labelling invariant as
`label_utc = rtp_time + rtp_to_utc_offset_ns` and claims it keeps clients off
their own system clock. The intent holds and stays worth keeping. The form
defeats it: `rtp_time` carries the host clock in through radiod, so the
invariant delivers its promise only while T6 holds authority with a live anchor,
and delivers the opposite everywhere else.

### 0.4 Six patches, one unnamed cause

Each of these describes the same distinction from a different angle, each
correct where it stands, none of them foundational:

| construction | where | what it says |
|---|---|---|
| Class A / Class B | `TIMING_AUTHORITY_TWO_AXIS.md` §1 | system-clock timing differs in kind from payload timing |
| the `rtp` / `sysclock` reference frame | `METROLOGY.md` §4.5 | never compare across frames |
| the ruler × origin axes | `TIMING_AUTHORITY_TWO_AXIS.md` §3 | rate and epoch rank separately |
| `plane="label"` | `offset_judge.py` | a bench reading belongs to one plane |
| content / legacy convention | `CONTENT_TIME_LABELING_CONVENTION.md` | the label names the antenna instant, or the emission instant |
| chain, not tier | `TIMING_PROVENANCE_MODEL.md` §2 | the chain travels; the tier stays local |

Six statements of one fact. This document states it once, and the six become
consequences.

---

## 1 · The measurand

**For every data product this station writes, the measurand reads: the UTC
instant at which sample `n` was taken at the station's reference plane.**

One quantity. Not a clock offset, not a tier, not a correction — an instant,
attached to a sample, at a declared plane.

```
t(n) = t₀ + (n − n₀) / f_s + Σ δᵢ
```

| symbol | name | axis |
|---|---|---|
| `f_s` | the sample rate the ADC actually runs at — the ruler | A |
| `(t₀, n₀)` | the registration: the UTC instant `t₀` known to coincide with sample index `n₀` | T |
| `δᵢ` | corrections between the reference plane and the point the timestamp attaches to | budget |

**The reference plane.** For a payload-anchored chain the plane sits at the
TS-1 injection point, immediately ahead of the RX888. Downstream of it the
measured signal and the injected reference share one coax, so that delay cancels
and never enters the measurand. Upstream the two paths differ — the reference
originates inside the TS-1 while the signal crosses the antenna feed and
whatever preamplifier and filter precede the injector — and that segment
survives in the measurand. Each station declares its own plane.

**Where the corrections get applied.** The station folds `Σ δᵢ` into `t₀` when
it forms the registration, so the published registration already carries them
and a consumer evaluating §1 adds nothing further. The sum stays written out
above because the budget of §6 must enumerate every term that went in, and
because a term folded silently into a constant becomes the fitted 16.618 ms all
over again.

**What the measurand excludes.** `D_clock` does not appear above. The host
system clock plays no part in `t(n)`. §7.1 recovers `D_clock` as a derived
quantity for the stations and services that want it.

---

## 2 · Axis A — the ruler

`f_s` names the rate at which the ADC actually samples. A GPSDO governs it, so
the ruler inherits the GPSDO's stability and nothing else. The ruler carries no
information about time of day. It says how far apart two samples sit; it cannot
say which second either one belongs to.

**Concretely:** the RX888 MkII takes a **27 MHz** external reference — not
10 MHz — and derives 64.8 or 129.6 Msps from it. Two failures therefore both
present as a governed ruler that does not govern. A GPSDO emitting only 10 MHz
never clocks the ADC at all. And a synthesised unit set correctly to 27 MHz but
driven at too low a level leaves the RX888 free-running: AC0G-ND ran roughly
350 ppm with an LBE-Mini at its 8 mA floor, and 32 mA locked it. Presence of a
cable establishes nothing. §2's provenance rule exists for exactly this — the
probe observes lock, or the level counts as assumed.

**Uncertainty.** `u(f_s)/f_s`, in parts per million.

| ruler state | `u(f_s)/f_s` | provenance |
|---|---|---|
| disciplined, measured | 0.0004 ppm | AC0G-B4, `t6_residual_rate`, n = 900 over 900 s, 2026-08-16 |
| disciplined, stand-in | 0.01 ppm | `t6_holdover.UNMEASURED_RATE_SIGMA_PPM`, 25× the measured value |
| undisciplined, stand-in | 2.0 ppm | `UNMEASURED_RATE_SIGMA_PPM_A0`; a free-running TCXO runs 0.5–2 ppm |

**How the station knows which state holds.** Observed, attested, or assumed —
never silently assumed. A probe reads the GPSDO and reports, freshness-gated. An
operator may attest with a stated reason where no probe can reach, the
legitimate case being a remote RX888 whose `/run/gpsdo` lives on another
machine. An unstated ruler counts as undisciplined. `hf-timestd validate` warns
on the assumed path, because a station that assumed discipline it did not have
shipped on AC0G-B4 and every sigma inherited the assertion.

**A1 and A0 remain useful shorthand for these two states and carry no other
meaning.**

---

## 3 · Axis T — the registration

`(t₀, n₀)` fixes where the ruler's zero lies against UTC. Registering a ruler
constitutes a measurement in its own right, with its own chain, its own method,
and its own uncertainty `u(t₀)`.

**The tiers rank registrations, not clocks.** This sentence carries most of the
document's weight. T6 through T0 do not name different kinds of thing. They name
competing estimators of one quantity, `t₀`, differing in method and in
`u(t₀)`. A tier is shorthand for the uncertainty of a registration.

| estimator | method | `u(t₀)` today | notes |
|---|---|---|---|
| T6 | BPSK PPS injected ahead of the ADC, recovered from the sample stream by matched filter and coherent fold | 0.012 µs spread offline; not yet computed at runtime (§6.3) | no propagation term in the chain |
| T5 | GPS + PPS delivered over USB | ~1 ms | bus scheduling floors it |
| T4 | host clock disciplined to a LAN GPS + PPS peer | ~2 ms | |
| T3 | WWV / WWVH / CHU tick fusion recovered from the payload | ~0.5–2 ms under A1 | propagation-limited; see `METROLOGY.md` §13.2 |
| T2 | host clock disciplined to WAN NTP | ~1–50 ms | |
| T1 | no registration newer than the last one; coasting | §4 governs | not a steady operating point |
| T0 | no registration available | absent | absence, recorded as absence |

**Registration transfer.** Each radiod channel numbers its own samples, so each
carries its own `n₀` on the shared ruler. Moving a registration from one channel
to another constitutes another registration step, with its own method and its
own `u`. It composes exactly as §4 says, under its own name, and it claims no
tier of its own.

Measured on AC0G-B4 2026-08-25: the six 24 kHz metrology channels share one
counter space, their implied epochs agreeing to 1.937 ms, which equals the pair
non-atomicity rather than an origin difference. The 96 kHz T6 channel does not
scale into that space — 362,095,021 samples of residual against WWV_20000 after
removing one 2³² wrap, about 3772 s. So the transfer must be measured, and
§6.2 gives it a budget term.

**Counter re-basing.** A radiod restart renumbers the samples underneath a
registration. A consumer holding a registration across that event labels
confidently from a numbering that no longer exists, and errs by seconds rather
than milliseconds. The station therefore publishes a `counter_epoch_id` that
changes whenever it observes a re-base, and no consumer may extrapolate across a
change in that identifier. `coast_ruler_intact()` already detects the event by
watching the arrival-floor offset jump.

---

## 4 · The composition law

A registration decays at the ruler's rate. That single law binds the two axes
and needs no special cases:

```
u(t₀, t) = sqrt( u(t₀, t_reg)² + ( (t − t_reg) · u(f_s)/f_s )² )
```

`t6_holdover.holdover_sigma_ns()` already implements this. We have been treating
it as a T6 holdover feature. It governs every rung, always. A registration made
by any method ages the same way, and only the two inputs differ.

The law explains, with nothing left over, the numbers we already measured. Under
discipline a frozen registration costs 1.44 µs per hour and needs roughly 29
days to reach a millisecond, which is why coasting through weather costs
essentially nothing. Undisciplined, the stand-in 2.0 ppm costs 7.2 ms per hour,
which is why the same coast on a station without a GPSDO amounts to a fiction.
The premise "coasting is essentially free" holds for the ruler we measured and
fails for a ruler we did not, and the law says which is which.

**One bound the law does not supply.** Nobody has measured what the LBE-1421's
oscillator does when GPS goes away and the GPSDO enters holdover. Until someone
does, a station losing GPS falls to the undisciplined stand-in. That makes GPS
loss visibly expensive, which errs in the safe direction, and §11 records the
debt.

**Representability, not quality, ends a coast.** `T6_HOLDOVER_MAX_SIGMA_NS`
equals 62.5 ms because beyond that point the chrony SHM precision field
saturates and a pushed claim would understate its own uncertainty. A coast ends
when the claim can no longer be stated honestly, never on a timer.

---

## 5 · The two uncertainties, and who reads which

`TIMING_PROVENANCE_MODEL.md` §3.3 publishes two uncertainties on the grounds
that two audiences need different ones. That reasoning holds, and the model
above explains why: **the two uncertainties are the two axes seen from the data
side.**

| published quantity | what it measures | axis |
|---|---|---|
| `u_epoch_ns`, with `k` and `p` | `u(t₀, t)` — how wrong the epoch may sit right now | T, decayed by A |
| `stability_ns` over `tau_s` | `u(f_s)` integrated over `tau_s` — how much the ruler wanders inside a file | A |

On this instrument the two differ by roughly five orders of magnitude, so a
single combined figure would bury whichever one the reader needed.

**Which audience reads which follows from the physics.** Single-station Doppler
differentiates phase against the ruler, so it reads axis A. A constant epoch
error cannot manufacture a travelling ionospheric disturbance; a ruler error at
10 MHz maps straight into apparent Doppler, and the difference between 0.0004
ppm and 1 ppm spans 0.004 Hz against 10 Hz on a measurand of order 0.1 Hz.
Cross-instrument and cross-station work compares epochs, so it reads axis T —
the multi-station geometry needs a quantity the single-station product does not.

This also states, in one line, the strongest claim the sidecar can make on
physics' behalf: **a chain containing no ionospheric term cannot manufacture an
ionospheric feature.**

---

## 6 · The budget

Every term appearing as a `δᵢ` in §1, or as a contribution to `u(t₀)`, enters a
budget with the same five fields: the correction applied, its uncertainty, its
GUM type, the method that evaluated it, and its disposition. A term that nobody
has evaluated appears with disposition `not_declared` and no number. It never
appears as a plausible value.

### 6.1 The payload-anchored chain, as it stands

| term | correction | type | method | disposition |
|---|---|---|---|---|
| UTC(USNO) via GPS to TS-1 modulator | ≤ 200 ns | B | designer statement, P. Elliott WB6CXC, 2026-08-30, standard injector mode | declared (corrected 2026-09-04: an earlier row read "~10 µs, WB6CXC documentation" — that figure came from our own template comment, and the software still applies it as `delay_budget_ns`; §11.8) |
| antenna terminals to injection point | — | — | — | `not_declared` (§11) |
| radiod channel filter group delay | folded into 16.618 ms | B | fitted against T4, n = 90 over 15 min, 2026-08-15 | **asserted, not measured** |
| edge estimation | 0.012 µs offline | A | fold-block scatter, offline harness | not computed at runtime (§6.3) |
| registration transfer between counter spaces | — | A | running minimum over the pair error | planned (§6.2) |

The third row deserves its emphasis. `chain_delay_calib_s = 0.016618` came from
fitting T6's answer to T4's over a quarter of an hour. Owning the map moves the
registration into a coordinate system worth its precision; it does not improve
that constant. Two independent routes can convert the assertion into a
measurement — computing `(N−1)/2` from radiod's published filter `L` and `M`
for a linear-phase FIR, and measuring against T5's GPS PPS over long baselines,
newly possible because T6 now holds lock for hours. Agreement converts it.
Disagreement constitutes the finding.

### 6.2 The transfer term

The only cross-channel evidence radiod offers reads the `(GPS_TIME,
RTP_TIMESNAP)` pair, whose error runs one-sided late — `GPS_TIME` live,
`RTP_TIMESNAP` cached against the 20 ms block grid. Averaging a one-sided error
converges on the wrong number, so the estimator takes a running minimum. The
transfer constant holds for a radiod session, because both counters descend from
one ADC clock, so observations only sharpen it. The term publishes the
uncertainty of that minimum — how far it has fallen, and how long since it last
fell — and that uncertainty combines into `u_epoch_ns` under its own name.

`TIMING_AUTHORITY_TWO_AXIS.md` §5 forbids a Class A witness from adjudicating
sub-second placement. That prohibition stands and needs no amendment. It guards
against a coarse registration silently replacing a fine one while wearing the
fine one's uncertainty, which happened on 2026-08-25. A budget term carrying its
own measured uncertainty, under its own name, into a combined figure does the
opposite of what the rule forbids.

### 6.3 What the estimator must start publishing

`FineEdgeEstimate` today carries `fit_rms` and `plateau_amplitude` and no
uncertainty in seconds. The 0.012 µs figure came from an offline harness. Until
the estimator publishes the scatter of successive edges about the position the
ruler predicts, `u_epoch_ns` for a T6 registration remains asserted, and the
overclaim gate of §9 has nothing to check.

`t6_arrival_floor.py` currently supplies the number the offset judge treats as
T6's precision. It measures transport arrival scatter — when an anchor reached
the bench — and never touches the edge estimate. It returns to diagnostics.

### 6.4 A registration may not claim a tier its lock does not support

A microsecond registration on a wrongly-identified edge does more harm than an
honest millisecond one, because a consumer can defend itself against a wide
uncertainty and cannot defend itself against a confident error. AC0G-B4 on
2026-09-04 locked 118.482 ms against a calibrated 16.618 ms, an error of
101.87 ms, on `ok=22 noise=94` — 19 % good detections, measured and accepted in
the same log line.

So lock credibility gates entry to the T6 estimator, ahead of any question of
scatter. A wrong edge announces itself as a discrete step rather than a change
of slope, which history can catch and a single pulse cannot;
`t6_stale_lock.py` already implements the guard.

---

## 7 · Derived products

### 7.1 `D_clock` and chrony

`D_clock` becomes a derived quantity, computed from the model rather than
measured alongside it:

```
D_clock = T_system − t(n) , both evaluated at one instant
```

The station computes it, hands it to chrony, and chrony disciplines the host
clock. Nothing in any data product depends on the result.

Two consequences follow. First, the fusion engine's product stops serving as the
system's measurand and becomes one estimator of `t₀`, which is what it always
was. Second, the timing-independence loop opens without a separate fix: once
data labels stop descending from the host clock, chrony's choice of reference
stops feeding back into them, and the shadow benches recover their independence.
Whether T6 should feed chrony at all becomes a question about the host clock's
convenience rather than about the science.

### 7.2 The label

A data product's timestamp evaluates §1 directly:

```
t(n) = t₀ + (n − n₀) / f_s + Σ δᵢ
```

radiod's `(GPS_TIME, RTP_TIMESNAP)` pair appears nowhere in that arithmetic. It
survives in the record as engineering provenance, which is where
`TIMING_PROVENANCE_MODEL.md` §3.4 already places it.

This preserves everything `METROLOGY.md` §4.5's invariant meant to protect —
clients never consult their own system clock, and a single radiod's clients
agree on labels by construction — while removing the term that defeated it. The
2026-04-20 incident, where system-clock drift of ~107 s mislabelled WSPR files,
stays structurally impossible, and now genuinely rather than nominally.

### 7.3 The record

`TIMING_PROVENANCE_MODEL.md` describes what the recorded data must state, and
this document supplies the model it states. The mapping runs one to one: the
`state` block publishes the registration in force with `u_epoch_ns`,
`stability_ns` and `tau_s`; the `chain` block publishes §1's plane and §6's
budget; the normative and engineering namespaces divide by that document's
membership rule. The tier appears in the engineering namespace as local
shorthand, per §3 above.

---

## 8 · The runtime carrier

One value object carries the model at runtime, and every consumer's arithmetic
and every recorded `state` block descend from the same instance, so the two
cannot disagree. It lives in `hamsci_dsp.timing`, which already hosts
`AuthoritySnapshot` and `AuthorityReader`.

```
TimeMap
  counter_space      which channel's numbering this registration applies to
  counter_epoch_id   changes on a radiod re-base; never extrapolate across it
  n0, t0_utc_ns      the registration
  f_s_hz             the ruler
  chain              the chain identifier of §1 and §6
  origin             native_anchor | sysclock | null
  u_epoch_ns, k, p   the T axis, decayed by §4
  stability_ns, tau_s the A axis
  a_level, a_level_provenance, a_level_attested_by
  measurand_plane, calibration_plane
  measured_at
  engineering: { judge_tier, radiod_gps_time_ns, radiod_rtp_timesnap,
                 rf_gain_db, cn0_db_hz, fine_search_mode, ... }
```

Evaluation reads `t(n) = t0_utc_ns + (n − n0) / f_s_hz`, with Karn's signed-32
technique on the difference and the corrections of §6 already folded into `t0`.

**Every rung fills this shape.** A station with no TS-1, no GPS and no local
peer still publishes a map — the one it has, built from radiod's pair and naming
`origin: sysclock` — wearing the uncertainty that pair was measured to carry. A
registration taken from a single pair inherits its skew permanently, and AC0G-B4
measured that inheritance directly on 2026-08-16 over 900 s: median 2.31 ms,
p99 8.03 ms, maximum 47.70 ms on T6's own channel, with two 12 kHz channels
reaching 57–63 ms, and the anchor-pair audit logging 401 ms excursions under
load. Nothing about that station's labels changes. What changes:
the station now states what it did, so a consumer needing microseconds refuses
the data and a WSPR decode needing 20 ms proceeds without complaint. A station
that can register nothing publishes `origin: null` with a reason, and absence
stays visible as absence.

**Consumers read `u_epoch_ns` and `stability_ns`, never a tier.** The uniform
application rule of `METROLOGY.md` §4.5 was right that consumers should not
branch on a tier. It was wrong that they should therefore branch on nothing.

---

## 9 · Invariants, and how they get enforced

1. **One measurand.** Every timing claim in this system either states `t(n)` or
   derives from it. A quantity that measures something else carries a different
   name.
2. **Provenance travels with every number.** No uncertainty without its method
   and type. No level without its provenance — observed, attested, or assumed,
   and never silently assumed.
3. **Absence stays visible as absence.** `origin: null`, `u_epoch_ns: null`, and
   a stated reason. Never an omitted record, never an uncertainty narrower than
   the evidence supports.
4. **No improvement may weaken the description.** A term may not leave the budget
   because it grew small; a correction may not be applied because it makes a
   symptom vanish; a chain may not be relabelled to look better. This rule
   forbade pasting 16.618 ms into the config to drive HPPS toward zero.
5. **The published uncertainty never falls below the observed scatter.** An
   automated gate compares each published `u_epoch_ns` against the residual
   scatter measured against an independent registration over the same interval.
   A claim better than reality fails a gate rather than surfacing in a paper two
   years later.

Rule 5 mechanically enforces the rest. `TIMING_PROVENANCE_MODEL.md` §0 states
the honesty invariant this document serves, and §8 of that document specifies
the gate.

---

## 10 · What this supersedes, and what derives from it

| document | relationship |
|---|---|
| `METROLOGY.md` §2.1 (`D_clock` as the measurand) | **superseded.** §1 above states the measurand; §7.1 recovers `D_clock` as derived |
| `METROLOGY.md` §3 ("Steel Ruler" philosophy) | **retained and generalised.** §3.1's formulation stands correct and §2–§4 above give it a measurement model. Of §3.2's three layers, Layer 1 characterises the ruler against a transmitter and belongs to axis A by its own account — *"how fast time is passing, but not what time it is"*; Layers 2 and 3 estimate `t₀` and belong to axis T |
| `METROLOGY.md` §4.3 (RTP as authoritative reference) | **retained in intent, corrected in form.** The pair carries the host clock; §7.2 supplies the arithmetic the section was reaching for |
| `METROLOGY.md` §4.5 (uniform offset application) | **superseded.** §8 above replaces the scalar with the map, and consumers read uncertainty rather than branching on a tier or on nothing |
| `METROLOGY.md` §4.5 (the A / T axis tables) | **retained, redefined.** §2 and §3 above keep the levels and restate what they rank |
| `METROLOGY.md` §6 (GUM budget) | **retained for the fusion chain.** §6 above supplies the payload-anchored chain's own budget; neither replaces the other |
| `TIMING_AUTHORITY_TWO_AXIS.md` | **derives.** §1's Class A / Class B distinction and §3's two axes both follow from §1–§4 above; §5's prohibition stands, and §6.2 above shows why a budget term does not violate it |
| `TIMING_PROVENANCE_MODEL.md` | **derives.** Its §0 invariant becomes §9 above; its §2 chain becomes §1's plane plus §6's budget; its §3.3 two uncertainties become §5's two axes |
| `T6_ANCHOR_INVERSION_DESIGN.md` | **derives.** Its inversion states §3: the edge registers the ruler. Its §9 out-of-scope items are answered by §3 (transfer), §8 (fleet export) |
| `CONTENT_TIME_LABELING_CONVENTION.md` | **derives.** The convention names the reference plane of §1 |
| `TIMING_AUTHORITY_ARCHITECTURE.md` | already superseded in place 2026-08-25; no further change |

---

## 11 · What this does not settle

Naming these keeps them from re-emerging as surprises.

1. **The antenna-to-injector segment stays undeclared.** It survives in the
   measurand, it varies per station, and nobody has measured it. The client
   contract asks hf-timestd to calibrate an analogue chain delay and hf-timestd
   does not. Until an estimator exists — most plausibly a two-point method
   comparing the injected edge against the same edge at a second known plane —
   the honest publication remains absent rather than plausible.
2. **The filter group delay stays asserted.** §6.1, third row. Two routes are
   specified; neither has run.
3. **GPSDO holdover on GPS loss stays unmeasured.** §4.
4. **The estimator's runtime uncertainty does not exist yet.** §6.3.
5. **The transfer estimator has not been built.** §6.2 specifies it; nothing
   implements it.
6. **The A / T levels came from the hardware in one shack.** They map awkwardly
   because we invented them bottom-up, which usually indicates honesty rather
   than the reverse — but they do not travel. DASI2 holds no Stratum-1 LAN
   peer, so tiers defined by that hardware stay structurally unavailable there.
   The chain travels; the uncertainty travels; the tier stays local shorthand,
   and a station publishing a shorter chain with a wider uncertainty keeps its
   data comparable with ours.
7. **Whether T6 should feed chrony at all.** §7.1 makes it a question about host
   clock convenience. Deliberately left open.
8. **The software applies a 10 µs modulator correction that no measurement
   supports.** `delay_budget_ns` defaults to 10,000 in `core_recorder_v2.py`
   and every T6 anchor carries it; the designer puts the modulator under
   200 ns (§6.1, corrected 2026-09-04). The station over-corrects by roughly
   9.8 µs, which exceeds every evaluated term in the payload-anchored budget
   combined. Changing a live instrument's published timestamps is an
   operator decision; `TIMING_PROVENANCE_MODEL.md` §1 records the defect and
   proposes the change.
