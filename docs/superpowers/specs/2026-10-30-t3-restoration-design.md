# Restoring T3: UTC reconstructed from the broadcasts

> **Status:** design, for Michael's approval.  Nothing here has been built.
> **Date:** 2026-10-30.
> **Evidence base:** `~/hamsci/tasks/2026-10-30-t3-recon.md` (a read-only survey of the code,
> the records and AC0G-B4, the same day) and `docs/design/review-fusion-kalman-20261022.md` §15.

## 1 · Why

Some sigmond stations will run with no T6, no T5 and no T4: no TS-1, no GPS receiver on USB, no
GPS time server on the LAN.  A GPSDO may still discipline the RX888's sample clock, so the
station keeps a good ruler, a rate, but nothing on site tells it which instant the ruler started
from.  Today such a station takes its epoch from internet NTP (T2), good to a few milliseconds
at best and tens of milliseconds at worst.

T3 exists to do better.  Michael put its purpose this way on 2026-10-23: "The entire aim of T3
from the beginning lay in reconstructing UTC from the received time standard broadcasts to the
best possible precision and accuracy."  The GPSDO keeps the rate; the broadcasts, less their
modelled propagation delay, supply the epoch.

T3 aims to beat T2 in both accuracy and precision.  It does not aim to rival a LAN time server
fed by GPS and PPS.

**Scope (Michael, 2026-10-30):** stations whose RX888 runs from a GPSDO.  A station without one
must also estimate the sample rate from the broadcasts; it stays on T2 until a later design and a
test bench other than B4.

## 2 · Where things stand

T3 went dark on 2026-10-22, by ruling.  The fused D_clock that fed it carried a calibration that
pulled each broadcast's mean toward the host clock, so it read near zero whatever the clock did.
The survey of 2026-10-30 found more:

1. **No record shows T3 beating T2 against an independent reference**, with or without CHU.
   Every good figure since December 2025, the QEX draft's 0.3 ms among them, measures the
   calibrated value.  The only independent comparison found holds one snapshot (+1.17 ms against
   a LAN GPS server, January 2026) with no T2 beside it.  So this design treats T3's promise as
   unproven, not as lost.
2. **One hint points the right way.**  On B4 at 15:04Z on 10-30, fusion's uncalibrated value sat
   3.4 ms from T4 while T2 sat 6.4 ms away.  One snapshot proves nothing, but it does not
   discourage.
3. **The tick registration, the intended replacement, fails.**  In its B4 shadow run of 10-23 to
   10-26 it named the wrong whole second about 82 % of the time.  Where it named the right one,
   it sat about 20 ms off, with its sigma pinned at a 1.0 ms floor.
4. **A known defect most likely explains most of that.**  Since ka9q-radio e5f790dd each radiod
   channel starts its own RTP counter.  The registration store, sibling fusion and the judge's T3
   bench all still assume one counter per radiod.  B4's channels show counter epochs spread over
   6 s, and 77 resets in 6 hours at recurring offsets of −30, −54, −77 and −113 ms.
5. **The receiver's own delay on the HF path stays unmeasured.**  The registration assumes none.
   T6 on B4 sits about 16 ms from T4, a delay T6 measures per station.  Whether the HF ticks pass
   through the same 16 ms remains open, and the answer changes what T3 must subtract.
6. **The propagation model runs, but nobody has scored it against a reference.**  B4 uses live
   GIRO ionosonde data first and IRI-2020 second, inside a parametric hop model in hamsci-dsp.
   PHaRLAP ray tracing exists only as a CLI command.  The model logs thousands of three-hop
   failures, fixes foE at 3 MHz, and chooses between E and F modes with no daily cycle.
7. **The station timing estimator has no consumer.**  The 10-22 review recommends routing T3
   through it.  It has tests and an acceptance corpus but no adapter, unit or status output.

## 3 · Goals

1. A T3 that reconstructs UTC from the time signals a station receives, standing alone: it takes
   nothing from T6, T5, T4 or the host clock at run time.
2. Proof on B4, against T6 and T4, that it beats T2 by median and by 95th percentile.  T3 claims
   nothing until B4 shows it.
3. Sources described by data, so a new transmitter joins by definition and validation, not by
   code written for it alone.
4. Timing from other clients' receptions (hfdl-recorder, codar-sounder) through a declared local
   interface, with no uploads needed for that role.
5. An honest uncertainty, built from the measurement and the propagation model, never a floor.

## 4 · Non-goals

- Rivalling T4 or T5.  A station with GPS on site uses GPS.
- Feeding chrony.  MEASUREMENT_MODEL.md §7.1.1 stands: a source recovered from the sample
  stream never votes on the host clock.  T3 serves the data labels and the authority snapshot.
- Uploading HFDL or CODAR products for this purpose.
- CHU.  The Canadian authorities discontinued it; it does not return.
- Any change to T6's chain-delay learning or the judge's T6 gate.

## 5 · Anti-goals

These would count as failure even if the code ran:

- **A T3 that reflects the host clock or T2 back.**  Any path by which T3's answer depends on the
  host clock repeats the defect of 10-22.
- **A T3 calibrated at run time against a higher tier.**  Ruling 3 of 10-22 rejected Option A:
  no calibration may give T6 or T5 authority over T3 locally.  B4's references score T3 and
  guide changes to the model; they never become a correction a station applies to itself.
- **A sigma that sits at a floor**, or one that the data contradict.
- **A fault that disappears.**  A step, a walk or a wrong second must show, loudly, once.
  Timing faults get exposed, never corrected away.
- **A claim before the measurement.**  No document, panel or status field calls T3 better than
  T2 until the B4 shadow run passes.

## 6 · Constraints

- **Layering.**  Signal processing shared across clients lives in hamsci-dsp.  The propagation
  model and its science live in hamsci-dsp and hamsci-physics.  The reconstruction, the authority
  and the judge live in hf-timestd.  Sigmond carries only the result, and none of the detail.
- **Clients stay independent.**  hfdl-recorder and codar-sounder must still run without
  hf-timestd and without sigmond.  They publish timing observations; they import nothing from
  hf-timestd.
- **Counter domains.**  Every observation names the RTP counter it was measured in.  Nothing
  combines two channels' RTP values directly.
- **B4 first.**  Every step deploys to AC0G-B4 first, after an announcement on the claude-bus,
  and follows the hf-timestd deploy rule (git fast-forward, restart the two units; no install.sh).
- **Light on the station.**  Shadow logging writes small rows; no analysis runs on a station VM.
- **The sign convention** of MEASUREMENT_MODEL.md: `label_utc = rtp_time + rtp_to_utc_offset_ns`.

## 7 · The design

### 7.1 Sources described by data

A source definition, one TOML file per transmitter in hf-timestd, states what a detector needs
and what the reconstruction needs:

- an identifier, the transmitter's location, and its frequencies;
- the signal type, naming the detector in hamsci-dsp that measures it (an AM tick and minute
  marker, the WWV BCD code, WWVB's phase code, an HFDL frame, a CODAR sweep);
- the emission schedule: when it transmits, silent minutes, voice periods, shared frequencies;
- the emission's offset from UTC, with its uncertainty and where that number came from;
- what it can name: the fraction of a second only, the second, or the minute.

WWV, WWVH and WWVB get definitions first.  The Maine transmitter on 3.265 MHz gets one when its
format arrives (Michael has asked John Ackerman).  If its ticks resemble WWV's, the definition
alone admits it; if not, it needs a detector in hamsci-dsp first.

### 7.2 Timing observations from other clients

A client that hears a time-bearing signal may publish timing observations to a local spool that
hf-timestd reads.  Each observation carries:

- the source identifier from §7.1;
- the arrival instant, as a sample index in a named counter domain (the channel's own RTP
  counter, with what it takes to map it to the station's absolute sample count);
- the emission instant the client inferred from the schedule;
- the client's own measurement uncertainty and a quality flag.

hfdl-recorder and codar-sounder implement this when their sources join (§8, stage 6).  Neither
uploads anything for this role.  The interface belongs in the client contract, so a new client
can join the same way.

### 7.3 The reconstruction

For each source and each minute, hf-timestd computes an epoch estimate: the arrival instant, less
the expected propagation delay, less the receiver's own delay, against the emission instant.  It
combines the estimates with weights from their honest uncertainties, treating as one witness the
frequencies that share a path from one site.  The station timing estimator carries the result
forward in sample time, with the GPSDO-disciplined rate, and publishes a T3 registration with
its u(t₀).

The integer second and the minute come from the broadcasts where they can: WWVB's time code,
WWV's BCD code and the minute marker.  **Michael, 2026-10-30:** when no broadcast can name them,
the host clock may.  On a T3-only station the host follows T2 and so cannot miss a whole second.
Any broadcast namer that disagrees overrules the host and raises a loud alarm, once.  The fraction
of a second, which carries T3's accuracy, comes from the broadcasts alone.

**Coasting.**  Once T3 has named the second, the GPSDO-disciplined ruler carries that name by
sample count; no broadcast needs to name it again.  The ruler's phase decays at about 1.44 µs per
hour in holdover (measured for T6), so hours without WWVB, or without any HF tick, cost
microseconds, not milliseconds.  Naming matters only at acquisition and after a counter break: a
radiod restart, or a channel created afresh.  So the host-clock fallback for naming should fire
rarely, and the record of §7.4 counts each time it does.  While the broadcasts fade, T3 holds its
last registration and its sigma grows at the ruler's rate; it does not drop to T2.

### 7.4 The refinement loop on B4

B4 holds two references, T6 and the LAN GPS server behind T4.  *(My call.)*  T6 serves as the
primary truth for T3, because both live in the sample plane.  T4 reaches the sample plane only
through the host-to-sample link, so comparisons with T4 carry that link's uncertainty, stated.
T2's error comes from the same host-clock comparison against T4.  For every broadcast and minute it
records: the measured arrival, the expected delay with its mode and its ionospheric data source,
T3's answer, and T6 (less the learned chain delay), T4 and T2 at the same instant.

Those records score the propagation model against truth: mode choice, E against F, the three-hop
failures, GIRO against IRI, and whether wiring PHaRLAP into the timing path pays.  Fixes land in
the model, in hamsci-dsp, for every station.  No station learns a correction from its own
references.

### 7.5 What carries over from the earlier method, and what changes

T3 does not start from nothing.  The earlier method already built most of the parts:

- the tick detectors, the 1 s fold and the template fit of the tick registration;
- the minute marker, the BCD decoder and the WWVB decoder;
- the propagation model in hamsci-dsp, with GIRO and IRI-2020 inputs;
- the WWV-against-WWVH differential solver and the multi-frequency consistency check in
  `differential_time_solver.py`, which identify propagation modes from differences in which the
  clock error cancels;
- the TEC estimator, which can use the spread of delay across frequencies (group delay scales as
  1/f²) to estimate the ionospheric term;
- the station timing estimator, which already keeps time by sample count from the ruler.

This design reuses them.  What it changes lies in how they connect and how the result gets judged:

1. **The frame.**  The earlier method measured each broadcast against a host-stamped plane and
   then calibrated each broadcast's mean toward zero, so its answer followed the host clock.
   T3 measures in the sample plane, against the ruler, and calibrates nothing against the host.
2. **The score.**  The earlier method never measured its answer against an independent reference.
   T3 gets scored against T6 and T4 on B4, every minute.
3. **The uncertainty.**  The earlier sigma came from an SNR formula and floors, and its weighted
   mean treated ten frequencies and thirty minutes of rows as independent.  T3 treats one
   transmitter site as one witness and carries the model's uncertainty as measured.
4. **The counters.**  T3 respects each channel's own RTP counter.
5. **The model.**  Measured errors, not assigned confidences, decide which model tiers and mode
   choices earn their place.
6. **Coasting** by sample count, where the old Kalman filter assumed one minute between updates.

So the work consists mostly of rewiring and correcting, not reinventing.  One expectation follows:
the honest T3 will at first look worse than the earlier figures, which read near zero by
construction.  The comparison that counts sets T3 against T2, both measured against T4 at the
same moments.

## 8 · The order of work

Each stage ends on B4 with a measured result, reported before the next begins.

0. **Measure the HF chain delay.**  The ~16 ms between T6 and T4 on B4 could sit in three
   places: radiod's processing, which every channel shares; the TS-1 injection path; or the link
   between host time and sample time (radiod's GPS_TIME pair, which memory records as non-atomic)
   that every comparison with T4 crosses.  On-air ticks cannot separate these, because they mix
   the receiver's delay with the unscored propagation model.  *(My call.)*  Stage 0 therefore
   measures with a signal of known timing and no propagation: the TS-1 pilot received through a
   channel set up as each HF channel is (same preset, filter bandwidth and output rate), so each
   channel gets its own value if they differ.  It needs an accuracy well below T3's target, about
   0.1 ms.  If radiod's processing carries the delay, T3 must subtract a receiver delay that a
   station without T6 can know, which means deriving it from radiod's configuration; stage 0 then
   also checks that derivation against the measurement.  Criteria 1 and 2 of §9 follow the answer.
1. **Fix the per-channel counter assumption** in the registration store, sibling fusion and the
   judge's T3 bench: map each channel's RTP to the absolute sample count, or combine only in the
   UTC plane.  Raise the ka9q-radio change with Rob and upstream.  This also closes the regression
   flagged on 10-06.
2. **Stand-alone shadow mode.**  T3 verifies against its own ticks and markers, never against T6,
   exactly as on a T3-only station.  Comparison with T6 and T4 moves out to the judge.  On B4
   every input already lies near truth (T6 sets the label plane; the host sits within 0.02 ms of
   T4), so a leak of host or T6 information would stay invisible there.  *(My call.)*  The shadow
   T3 therefore starts from radiod's raw pair, not from T6's plane, and the test also injects
   offsets of several milliseconds and of a whole second to show that T3 recovers truth on its
   own.
3. **Name the second** from WWVB, the BCD code and the minute marker.  Measure each namer's
   success rate per channel.
4. **Honest sigma**, from the measurement and the model's stated uncertainty, with the model's
   uncertainty scored against B4's truth.
5. **The durable shadow record** of §7.4, per minute, on B4.  State transitions log once each and
   count.
6. **Refine the model** from that record, in hamsci-dsp.
7. **Route T3 through the station timing estimator**: the adapter, its owner in the authority
   manager, a status block, and a T3 probe in place of the fusion probe.  *(My call.)*  The
   estimator's admission rules may change for a T3-only station, which may have only one to three
   independent witnesses (one per transmitter site), below its present minimums of two tiers and
   eight witnesses.  Its rule that a step never becomes a rate stays.  Every change gets a test
   seen to fail first.
8. **The shadow run** under §9.  When it passes, T3 takes its slot on B4 (step 7 of the 10-22
   order).
9. **Added sources, one at a time**: WWVB as an epoch source and not only a namer, then HFDL,
   CODAR and Maine.  Each joins only after B4 shows it improves the result.

The first version runs on WWV, WWVH and WWVB.  It defines the source description and the client
interface from the start, so later sources need no redesign.

## 9 · Pass criteria

The six criteria of 10-22 (review §15) stand, over 48 hours from T3's first acquired minute on
B4, with two changes.  Stage 0 decides how criteria 1 and 2 state the expected offsets against T4
and T6.  **Michael, 2026-10-30:** beating T2 suffices for admission; the 1 ms median of criterion 1
becomes a target for refining the model, reported but not gating.

1. Its residual against T4 stays within 2 sigma at least 95 % of the time.  Target, not gate: a
   median magnitude under 1 ms.
2. Its residual against T6 holds steady at the offset stage 0 predicts, with no step larger than
   2 sigma.
3. Its sigma stays honest: the rms of residual over sigma lies between 0.5 and 2, and the sigma
   never sits at a floor.
4. It never names the wrong second and never jumps by more than 0.5 s.
5. Its state transitions log loudly, once each, and count.
6. It beats T2: with T4 as truth, its error stays smaller than T2's at the same station, by median
   and by 95th percentile.

An added source (stage 9) passes when T3 with it beats T3 without it on criteria 1, 3 and 6.

## 10 · Risks

- **Host naming.**  Where the host names the second, a broadcast namer must eventually confirm it;
  the record of §7.4 counts how often the host named alone.

- **T3 may not beat T2 with WWV, WWVH and WWVB alone.**  The propagation model sets the floor, and
  nobody has measured it.  If so, the record of §7.4 shows where the error lives, and the added
  sources become the path forward rather than an extra.
- **The counter fix touches every registration path.**  It needs its own tests against recorded
  data before B4 sees it.
- **Mode ambiguity** (E against F, one hop against two) may defeat accuracy at some hours.  The
  sigma must say so when it does.
- **B4 cannot speak for every site.**  One station's ionosphere does not sample the others'.  A
  second reference station would strengthen any claim; ND has T4 and could serve.

## 11 · Left to the builder

- File formats for the source definitions and the observation spool.
- How the shadow record stores its rows (JSONL or SQLite) and its retention.
- The detector interfaces in hamsci-dsp.
- Test design, provided each test is seen to fail first.
