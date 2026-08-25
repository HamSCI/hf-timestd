# Two axes of timing authority: the ruler and the origin

**Status:** ADOPTED 2026-08-25.  **Replaces** the L1–L6 single ladder and
the "L6 is a calibration layer" definition, both **excised** from
`TIMING_AUTHORITY_ARCHITECTURE.md` on 2026-08-25 (recoverable via `git log`).  Consistent
with `T6_ANCHOR_INVERSION_DESIGN.md` and
`CONTENT_TIME_LABELING_CONVENTION.md`, which it generalises.

**Author:** mjh, 2026-08-25 (written up by Claude from mjh's argument).

---

## 1 · The distinction that was missing

Two fundamentally different classes of timing live in this system, and until
now they shared one ranked ladder as if they were commensurable.

**Class A — system-clock timing.**  A host clock, set by a wristwatch, WAN
NTP, a LAN stratum-1 server, or GPS+PPS wired into the machine.  radiod
publishes an RTP→UTC mapping from it as the `(GPS_TIME, RTP_TIMESNAP)` status
pair.  `TIMING_AUTHORITY_ARCHITECTURE.md` is candid that *"the quality of this
mapping depends entirely on radiod's system clock (CLOCK_REALTIME)"*.

The pair is read at **status-emission** time, not at acquisition, so it
carries every millisecond of pipeline and scheduling latency.  Measured on
AC0G-B4 2026-08-25: wspr-recorder saw 20,813,617 updates, **max disagreement
+816.4 ms**, with only ~37 % of pairs landing at exactly 0.  Any Class A
quantity must account for processing delay.

**Class B — payload timing.**  The time is *in the received samples*.

* **T6** — a GPS receiver wired to a TimeSync-1 injector flips carrier phase
  on the UTC second, into the RF feed ahead of the receiver.  It traverses
  the same coax → RX-888 → ADC path as the science signal.
* **T3** — WWV/WWVH tick and tone structure recovered from the HF stream and
  fused across broadcasts.

Class B does not care how long processing took, because the timing is evident
at the point of *reception into the sample stream*, not at the point of
*emission from radiod*.  A recording analysed two weeks later yields the same
answer as one analysed live.

**This is the governing distinction.**  Every timing decision in the system
should state which class it belongs to.

## 2 · The RTP counter is Class B; radiod's mapping is Class A

The seam is finer than "radiod is untrusted":

* The **RTP counter itself** is a sample index — intrinsic to the stream,
  GPSDO-disciplined, and carries no host clock.  Trustworthy.
* The **RTP→UTC mapping** radiod publishes is host-clock-derived and
  emission-contaminated.  Not trustworthy at sub-10-ms scale.

So hf-timestd must never consume radiod's published mapping for anything
needing precision.  It establishes its own from T6.  This is exactly
`T6_ANCHOR_INVERSION_DESIGN.md`: *"T6 should SET the RTP timestamps, not be
corrected by them."*

## 3 · Two axes, not one ladder

Authority is a **2-D capability matrix**, not a rank:

**A-axis — the ruler.**  Does a GPSDO discipline the ADC clock?  This governs
*rate*: how long any origin, however obtained, stays good.
`A1` = disciplined, `A0` = free-running.

**T-axis — the origin.**  What names the second and places it within the
stream?  `T6` (payload, ns-class) / `T5` (local GPS+PPS via USB) / `T4` (LAN
stratum-1) / `T3` (HF fusion, payload).

### 3.1 · How we know the A-level: observed / attested / assumed

The A-axis can be established three ways, and the difference is
load-bearing — an uncertainty quoted off an *observed* A1 is a
measurement; the same number off an *assumed* A1 is a guess.

| provenance | how | when it is the right answer |
|---|---|---|
| `observed` | `gpsdo_probe` reads `/run/gpsdo/<serial>.json`, freshness-gated, degrades to A0 on its own | the receiver's GPSDO is visible to *this* host |
| `attested` | operator declares it, **with a reason**, via `a_level_attested_by` | a **remote RX-888**: it may well be disciplined, but its `/run/gpsdo` is on another machine and cannot be probed from here (mjh, 2026-08-25) |
| `assumed` | nothing said — `authority_runner` falls back to configured `a_level`, which **defaults to `"A1"`** | never |

A remote receiver is the case that makes attestation necessary rather
than sloppy: there is no mechanism by which this host could observe a
GPSDO on another machine, so the operator's word is the only evidence
obtainable.  It is recorded as `attested` in the sidecar and **never
laundered into `observed`** — a downstream reader can then weigh it as a
human claim.

`assumed` is the hazard: a station with no GPSDO at all silently claims a
disciplined ruler, and every sigma downstream inherits the claim.
`hf-timestd validate` warns on it (`timing_axis_issues`).

```toml
[timing.authority_manager]
a_level = "A1"
# Required when the A-level is asserted rather than probed — e.g. a remote
# RX-888 whose GPSDO this host cannot see.  Free text: who attests, and on
# what basis.  Carried into the data sidecar as provenance.
a_level_attested_by = "AC0G 2026-08-25: remote RX-888 clocked by LBE-1421, verified on site"
```

⚡ **The code already has this.**  `a_level` (`gpsdo_probe.py`) is the A-axis
and is *observed*, not configured: freshness-gated, with `a_level_reason`, and
`"unknown"` for a malformed file.  `authority.json` publishes `a_level`
alongside `t_level_active`.  What is missing is that the ladder in the
architecture doc collapses both axes onto one rank, and that neither axis
reaches the recorded data.

### Deployment matrix (mjh, 2026-08-25)

| deployment | A | T available |
|---|---|---|
| DASI2: TS-1 + LBE-1421 | A1 | T6, T5, T3 |
| PSWS kit: LBE-1421 / 1420 / mini GPS, no injector | A1 | T5, T3 |
| older GPSDO, disciplines the RX-888 but no NMEA/PPS out | A1 | T3 only |
| separate GPS receiver, undisciplined ADC | **A0** | **T5**, T3 |
| radio only | A0 | T3 only |

The fourth row — good origin, bad ruler — **cannot be expressed on a single
ladder at all**.  That alone retires the ladder.

In every row the station still hears WWV, so T3 always yields something:
±0.5 ms against WAN NTP's ±1–10 ms by the old doc's own table.  A kit with
nothing but a radio still produces defensible timing — but only a reader who
is *told the configuration* can use it.

## 4 · Two goals, so two ladders

Ranking Class A against Class B on one scale forces a comparison with no
answer, because they serve different purposes:

* **Labelling samples** — pure counter arithmetic from an anchor.  No
  transport floor.  T6 wins outright; the host clock is irrelevant.
* **Disciplining the host clock** — to say "UTC now is X" you must cross from
  payload space into wall-clock space, and that crossing costs the transport.
  Floor ~1 ms; FUSE already does it at 20 µs.  **T6 structurally cannot win
  here**, no matter how good it gets.

⇒ Publish **label authority** and **clock authority** separately.  Nearly
every guard in `core_recorder_v2.py` is implicitly asking "which ladder am I
on?" and getting no answer.

## 5 · Class A must not govern Class B

All five defects found on 2026-08-25 are the same error in this frame — a
Class A quantity governing a Class B source:

| defect | the confusion |
|---|---|
| T5 sanity check at ±5 ms | Class A grading Class B's sub-second placement — and `_t5_implied_effective_chain_delay()` sources its "GPS truth" from `rtp_to_utc()`, i.e. radiod's own mapping |
| coarse T5 fallback anchor | Class A anchor silently replacing a Class B one; re-based the ruler +234 ms |
| bench sigma from the arrival floor | Class A anchor published with Class B's error bar |
| `coast_ruler_intact` on arrival latency | Class A proxy for a Class B quantity the calibrator already measures exactly |
| metrology origin | arrival measured from the *labelled* minute boundary rather than the PPS edge in the same recording |

**Rule.** A Class A witness may name the integer second (an integer choice
cannot inject sub-second error) and may flag gross mislock (~0.5 s).  It may
never adjudicate a Class B source's sub-second placement, supply its sigma,
or silently replace its anchor.

### 5.1 · Metrology should be PPS-referenced

`metrology_engine.py:1164` computes tick arrival as an offset from the
labelled minute boundary:

```python
arrival_sample  = start_sample + precise_peak_idx
raw_arrival_ms  = arrival_sample * 1000 / self.sample_rate
# Timing is measured from RTP timestamp (sample 0 = minute boundary)
```

so every millisecond of anchor error enters `timing_error_ms` and `d_clock`.
Referencing ticks to the **T6 PPS edge in the same recording**, by RTP
difference, makes the science immune to the anchor's absolute error and to
every labelling convention — the same immunity offline analysis has.
Precondition: confirm cross-channel RTP commensurability (T6 at 96 kHz vs
metrology channels at 24 kHz, and a common per-channel RTP origin).

## 6 · The sidecar: recorded data must state its own provenance

On 2026-08-25 AC0G-B4 recorded under **five distinct timestamp regimes in one
day** — legacy/T6 (~+2 ms), legacy/coarse-T5 (**−26 ms**), legacy/T6
(−0.2 ms), content (**−1007 ms**), legacy/T6 (+2 ms) — and nothing in the
data distinguishes them.

What rides with extracted IQ today (`ring_buffer_reader.py:393`) is
`start_rtp_timestamp`, `gps_time_ns`, `rtp_timesnap`, `sample_rate`,
`channel`, `n_samples`, `start_system_time`, `source`.  No tier, no anchor
provenance, no convention, no constants, no sigma — and `start_system_time`
is derived from radiod's Class A pair, the noisiest mapping on the box.

**Requirement.** Every recorded product carries a provenance block naming the
hardware capability actually delivering, the derived axes, and the governing
origin:

```
ruler:    gpsdo_present, gpsdo_disciplined (A1/A0),
          a_level_provenance (observed | attested | assumed),
          a_level_attested_by, serial, pll_locked, gps_fix,
          age_sec, a_level_reason
host:     local_gps_pps_present, nmea_fresh, pps_edges_per_min
payload:  tsi_present, costas_locked, authority_state
derived:  a_level, t_level_active, t_level_available
origin:   anchor_rtp, anchor_utc_ns, captured_via_tier,
          chain_delay_ns, delay_budget_ns, filter_group_delay_ns,
          labeling_convention, sigma_ns
```

**Observed where it can be; attested where it cannot; never silently
assumed.**  A remote RX-888 legitimately cannot be probed — record the
operator's attestation and say that is what it is.  DASI002 is the
cautionary case for the local path: LBE-1421
physically present, `pps_enabled=false`, no GPS fix, 0 PPS edges in 60 s.
Present ≠ delivering.

Everything above `origin:` already exists at runtime.  `origin:` is the
anchor ledger (`state/t6-anchor-ledger/`), which needs `labeling_convention`
added and equivalent rows for intervals not governed by T6.

## 7 · Defect this frame exposes: the holdover constant is A1-only

`t6_holdover.UNMEASURED_RATE_SIGMA_PPM = 0.01` is documented as *"25× the
value measured on B4"* — i.e. calibrated against a **GPSDO** (B4 measured
1.44 µs/hour).  A free-running TCXO is 0.5–2 ppm = **1.8–7.2 ms/hour**:
50–200× larger than that "pessimistic" stand-in, and ~1250–5000× worse than a
disciplined ruler.

On an A0 station the coast module's premise — *"coasting through weather is
essentially free"* — is false, and the anchor would coast on a badly
optimistic sigma.  The constant is A1-specific and does not know it.  This is
the honest-sigma failure one level down: a claim calibrated on one hardware
class, silently applied to another.

**Fix:** make the stand-in a function of `a_level`.  Filed separately.

## 8 · What this supersedes

| document | status |
|---|---|
| `TIMING_AUTHORITY_ARCHITECTURE.md` §"Timing Accuracy Hierarchy" (L1–L6) | **EXCISED** 2026-08-25 (recover via `git log`) — a single ladder cannot express A0+T5, and it conflated the two classes on one rank |
| same, §"L6: BPSK PPS Chain-Delay Calibration" | **EXCISED** 2026-08-25 — T6 is a first-class Class B authority, not a calibration layer for Class A; and its chain delay must NOT include "DSP pipeline, and RTP packetization", which is where the fitted 16.618 ms came from |
| `T6_ANCHOR_INVERSION_DESIGN.md` | consistent; generalised by §2 |
| `CONTENT_TIME_LABELING_CONVENTION.md` | consistent; §1 is its general case |
