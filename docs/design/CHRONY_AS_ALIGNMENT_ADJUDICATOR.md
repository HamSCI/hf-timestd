# Chrony as the Alignment Adjudicator — Design Note

**Date:** 2026-08-16
**Status:** PROPOSAL — for discussion, not approved. Narrows work rob designed
(`JUDGE-CROSS-BENCH-GATE-2026-08-05.md`, spec §14) and needs his agreement before
anything is built on it.
**Reframe author:** Michael (mjh)
**Evidence base:** AC0G-B4, 2026-08-16 (see §10)

## 1. The decomposition

RTP primacy exists because the GPSDO provides a *steel ruler* — a rate so stable that
sample counts are a rigid measure of elapsed time. That is correct and unchanged by
anything here.

But **the ruler exists regardless of what any clock does.** The problem was never the
ruler; it is the ruler's **alignment to UTC**. And "align a clock to UTC given several
sources of varying and asserted quality" is exactly, and only, what chrony does.

The invariant was right one level down — protect the ruler — and over-reached one level
up, forbidding the system clock as a *source*. That over-reach is what forced hf-timestd
to grow a parallel adjudicator: tiers, per-source sigmas, cross-bench agreement gates,
precision non-regression clauses, hysteresis, shadow residuals. That is chrony's job
description, re-implemented.

Note also that the invariant is already aspirational rather than actual: radiod's
`GPS_TIME` is `clock_gettime(CLOCK_TAI/CLOCK_REALTIME)` offset to the GPS epoch
(`ka9q-radio/src/misc.c`, `gps_time_ns()`). Despite the name there is no GPS on radiod's
side — the GPSDO supplies rate, never time of day. Every RTP→UTC conversion that goes
through radiod's advertised pair already routes through the system clock.

## 2. Why the parallel adjudicator is the weaker one

On 2026-08-16 T6 was ~15 ms wrong on AC0G-B4:

* **chrony** marked HPPS `#x` falseticker and refused to steer to it.
* **hf-timestd's Offset Judge** adopted T6 as `AUTHORITATIVE` and published
  `sigma_ns: 2264` — 2.26 µs.

The bespoke selector claimed microsecond precision on a falseticker. The mature one
caught it. That is the empirical case for this note.

Every failure in that incident was in the duplicated selection layer, not in the physics:

| failure | cause |
|---|---|
| tier taxonomy carried no independence | `METROLOGY.md:303` — "T5 / T4 / T2 are system-clock disciplines". T5's bench literally computes `truth_utc = self._time() - age`. |
| cross-bench gate blind to a 16 ms error | T5's σ is the 25 ms placeholder ⇒ bound `5·√(2.19² + 25²) ≈ 125 ms`. Formally "agreement". |
| shadow residuals unusable | they read the host clock, which chrony was steering *toward T6* — so they **shrink toward zero as a T6 error propagates**. |

The fine stage, meanwhile, held ~2 µs residual and a chain delay stable to 4 µs
throughout. The measurement was never the problem.

## 3. Proposed split

| owner | job |
|---|---|
| **GPSDO** | the ruler — rate, unconditionally (the A-levels) |
| **chrony** | align the system clock to UTC, adjudicating T6/T5/T4/T3/T2 as available (the T-tiers) |
| **hf-timestd** | supply T3 and T6 *as sources*; supply the RTP↔UTC anchor from T6 directly; measure radiod's epoch error |

hf-timestd stops being a timing authority with its own selection engine and becomes a
good **sensor**, plus the one thing chrony cannot do: putting UTC on a *sample*.

## 4. Alignment rides in the payload

This is the property that makes the design uniform across deployments.

* **WWV/WWVH are in every HF stream.** The tick gives sub-second alignment at a known
  *sample number* — a sample-space measurement, no clock involved.
* **The absolute second comes from the payload too.** `core/wwv_bcd_decoder.py` decodes
  the IRIG-H BCD time code on the 100 Hz subcarrier — full date and time, not merely a
  tick. Integer-second naming therefore needs no external reference.
* **TS-1 BPSK PPS** where installed, at µs class instead of ms.

⇒ **Any machine running radiod can derive its own alignment from what it is already
receiving.** No time is shipped across the network. The multi-host case stops being a
special problem and becomes the same problem, solved locally on each radiod host.

### Configuration matrix

| configuration | rate from | alignment from | class |
|---|---|---|---|
| GPSDO + TS-1 | GPSDO | T6 PPS edge (sample space) | µs |
| GPSDO, no TS-1, bad host clock | GPSDO | WWV tick + BCD code | ms — **host clock never enters** |
| no GPSDO | WWV tick spacing (`regress_rate_ppm`) | WWV tick + BCD code | ms, and the ruler is re-surveyed rather than rigid |

The third row is a genuine capability change, not just a precision loss: without A1 the
oscillator wanders, uncertainty **grows between fixes**, and long-capture interpolation
degrades. It also requires revisiting spec §11 / audit G7, which currently *records* the
measured rate and deliberately never applies it — a prohibition that is right while A1
holds and wrong when it does not.

## 5. What hf-timestd keeps

1. **The anchor — sample ↔ UTC, clock-free.** chrony has no concept of a sample. This is
   irreducible and it is the part that worked all along.
2. **T3 and T6 published as chrony sources.**
3. **radiod-epoch offset** — how wrong radiod's advertised pair is. Always useful,
   essential when radiod is on a machine chrony cannot discipline.

## 6. What this removes

Tier ranking and adoption logic; cross-bench agreement gates; σ floors as an adoption
control; shadow residuals *as a selection mechanism* (they remain fine as a diagnostic).

The T2/T4/T5 benches can be replaced by chrony's **per-source** measurements.
`ChronyTrackingProbe` already polls `chronyc -n -c sources`, but only to decide tier
availability; the T4 bench then reads `tracking` — the *disciplined clock*. Reading
per-source data instead makes those tiers genuinely independent, because chrony measures
each source by round-trip rather than through the clock it is steering, **including
sources it has not selected**. That is the independent witness the current design cannot
produce, available today, with no new hardware.

## 7. The residual that does not dissolve

chrony aligns the **clock**. The science needs UTC for **sample N**. The bridge is
radiod's `(GPS_TIME, RTP_TIMESNAP)` pair, and that pair is **not sampled atomically**:
`GPS_TIME` is read live at packet build while `RTP_TIMESNAP` is a cached block-grid
value (`encode_radio_status()`), so emission lateness becomes pair skew.

Measured on B4 (81,600 packets, 900 s): a one-shot anchor inherits **median 2.3 ms,
p99 8.0 ms, max 47.7 ms**; `rtp_timesnap` advances in rigid 500 ms quanta while
`gps_time` tracks the real emission gap.

So a perfect chrony clock still costs milliseconds crossing into sample space. That
transfer, not the clock, is then the dominant error — tracked as `HamSCI/ka9q-python#4`,
which this design makes **more** important, not less.

**T6's anchor is the answer to exactly that**, and it is why T6 must not be spent *solely*
as a chrony refclock. In sample space it is ~2 µs; pushed through the SHM interface it is
forced back into wall-clock space (hence the 526 ms `push_lag` and the `_sys_at_edge`
back-calculation) and degraded. Use it both ways, for different jobs.

## 8. On "circularity"

T3 measures the offset of a clock that T3 is helping steer. That is **the normal control
loop every clock discipline has** — every NTP source has this property, and chrony's loop
filter exists for it.

The 2026-08-16 confusion did not come from that loop. It came from building a *second,
unfiltered* comparison on top of a clock chrony was already steering, and then reading it
as an independent measurement. The circularity was in the judge, not in the concept.

## 9. Consequences for open issues

* **#8** (arm the cross-bench gate) — scaffolding for a selection layer this design
  removes. Mark superseded-pending-decision rather than actionable.
* **#15** (T3-vs-T6 host-clock-error guard) — same; it exists to compensate for the
  missing independence that chrony's per-source data supplies directly.
* **#9** (kernel PPS for T5) — still wanted, and unblocked in value: under this design T5
  becomes a chrony source, which is its natural form. Note the virtualisation constraint
  recorded there.
* **#13** (sub-second residuals from `rtp_to_utc`) — unchanged and still a real defect.
* **ka9q-python#4** (non-atomic anchor pair) — promoted; it becomes the dominant term.

## 10. Evidence (AC0G-B4, 2026-08-16)

```
chrony        #x HPPS ... marked falseticker;  sourcestats StdDev 177-203 ms
judge         t_level_active=T6, AUTHORITATIVE, sigma_ns=2264
ScreenPI4     LAN stratum-1, ±145 µs, StdDev 6.6 µs over 24 min -> host clock 15 ms out
T3 fusion     d_clock_fused_ms -0.05..-0.21 ms ± 3.9 ms
T6            offset_to_chrony -12.7..-16.4 ms
T6 fine stage residual ~2 µs; chain_delay stable to 4 µs
anchor pair   median 2.3 ms / p99 8.0 ms / max 47.7 ms one-shot inheritance
```

## 11. Open questions

1. **rob's agreement.** This narrows the cross-bench gate work deliberately, not by
   oversight.
2. ~~**Sign audit before wiring anything.**~~ **RESOLVED 2026-08-16 — no sign bug.**
   Traced end to end: `refclock_shm.c` calls
   `RCL_AddSample(instance, &receive_ts, &clock_ts, …)`; `refclock.c` computes
   `raw_offset = UTI_DiffTimespecsToDouble(ref_time, sample_time)`; `util.c` defines
   `UTI_DiffTimespecsToDouble(a, b) → a - b`. So chrony computes
   **clockTimeStamp − receiveTimeStamp**, the field assignment in `chrony_shm.py` is
   correct, and a fast host clock yields a negative offset that slews it backwards.

   The apparent contradiction was **`chronyc sources` displaying the negation** of
   chrony's internal offset. Confirmed same-source, same-instant by reading the SHM
   segment directly (SysV key `0x4e545032`): segment held
   `clock − receive = −15.484 ms` while `chronyc sources` showed HPPS at `+14 ms`.
   `chrony_shm.py`'s comment had the subtraction backwards and has been corrected.
3. **Does T6 feed chrony at all?** Arguments both ways: valuable for GPS-denied holdover,
   lossy and the entry point for the 2026-08-16 error.
4. **The no-GPSDO rate policy** (§4, row 3) — spec §11 / audit G7 would need amending for
   that configuration only.
