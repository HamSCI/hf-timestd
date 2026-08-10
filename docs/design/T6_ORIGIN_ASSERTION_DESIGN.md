# T6 origin assertion — stop deriving the sub-second RTP→UTC term

**Date:** 2026-08-10 · **Status:** proposed, stage 1 is an experiment ·
**Amends:** `T6_ANCHOR_INVERSION_DESIGN.md` §5 · **Station:** AC0G-B4

## 1. The defect

T6's sub-second RTP→UTC term is *derived* from radiod's advertised wall
clock and then published as `chain_delay`:

```python
raw_wall_time_sec = rtp_to_wallclock(last_edge_rtp, channel_info)
integer_offset    = int(round(raw_wall_time_sec - reading.pps_utc_sec))
residual_sec      = raw_wall_time_sec - (int(reading.pps_utc_sec) + integer_offset)
# residual_sec becomes effective_chain_delay
```

NMEA names only the integer second (±0.5 s). Every sub-second millisecond
comes from `rtp_to_wallclock` — that is, from the mapping the correction
is supposed to correct. For the term we care about, the computation is
circular.

`METROLOGY.md` already states the intended architecture: *"T6 and T3 …
are independent of the system clock entirely and survive arbitrary
system-clock drift."* The implementation does not honour it.

## 2. Evidence (AC0G-B4, 2026-08-09/10)

**The instrument is excellent.** Method 5 (diff detector), 33 291 edges
over 7.4 h. Sampling `chain_delay_samples` every 3000 rows:

```
42808.000000  86496.924869  21276.922596   5856.920746  86496.919525
51936.918316  21276.919255  76896.920189  42396.919251  25056.920197
```

The **fractional** part is constant to ~0.005 samples ≈ **52 ns over 7.4
hours**. Consecutive edges are exactly 96 000 RTP samples apart. The PPS
and the ADC clock are phase-locked (both GPSDO-fed) and edge localisation
is nanosecond-class, as `T6_ANCHOR_INVERSION_DESIGN.md` predicted.

**The origin is not.** The integer part wanders by tens of thousands of
samples, and the published offset is not repeatable at fixed
configuration:

| config | effective_chain_delay |
|---|---|
| 96 kHz / ±25 kHz | 31.84 ms |
| 24 kHz / ±11 kHz | 45.75 ms |
| 12 kHz / ±5.5 kHz | 108.89 ms (M2 agreed to 84 µs) |
| 96 kHz / ±25 kHz, 15 min later | **47.30 ms** |

Across the evening the same channel gave 41.60 / 37.45 / 34.13 / 34.26 /
31.84 / 47.30 ms. The design's own physical bound for this quantity is
**±1 ms** (`t6_anchor_authority.py:44`, "analog TS-1→ADC path plus
channel-filter group delay is microseconds to sub-millisecond").

## 3. Mechanism

`_t6_apply_authority_decision`, on every UNLOCK:

```python
elif decision.state is UNLOCKED and prev in (AUTHORITATIVE, DEGRADED):
    self._t6_native_anchor = None
    # Also reopen the legacy cascade's own gate...
```

The anchor is discarded and disambiguation re-runs. **58 authority
transitions in one night** ⇒ 58 fresh derivations ⇒ 58 origins. The
variance is not noise in a measurement; it is repeated re-derivation of a
quantity that should be constant.

## 4. Eliminated hypotheses

Recorded so they are not re-derived. Each was killed by measurement.

| Hypothesis | Killed by |
|---|---|
| RF level / TS-1 too hot | Level identical (-57.07 / -57.12 / -57.08 dBm) across AGC change and power cycle; jumpers already in RX |
| Displaced MF peak (`hf-timestd#7`) | Method 5 (no integration window, no peak to displace) agrees with Method 2 to 84 µs at 12 kHz |
| Batch/RTP mislabelling | 11 recorder batches = exactly 10 radiod blocks = 19 200 samples; 18.18% mismatch is exactly 2/11 and cancels; drift bounded at −240 samples, not accumulating |
| radiod's advertised anchor | Anchor freshness 0.35–0.82 ms on 12 k/24 k/96 k channels |
| USB queue latency (16 × 2.02272 ms = 32.36 ms) | Matched 31.84 ms — then the same config gave 47.30 ms |
| Cumulative UDP sample loss | core-recorder RTP socket: 128 MB buffer, Recv-Q 41 KB, **zero drops** |

## 5. The change

Assert the origin instead of deriving it:

```python
# was:  chain_delay = rtp_to_wallclock(edge_rtp) − integer_second
# now:  chain_delay = chain_delay_calib_s
```

Touch points, all `core/core_recorder_v2.py`:

* `_t6_disambiguate_via_t5_lb1421` — MF / HPPS path
* `_t6_diff_disambiguate_via_t5_lb1421` — diff / HFPS path
* `_compute_lb1421_residual_ns` — demoted to diagnostic
* `_t6_name_integer_second`, `_t6_name_second_via_nmea` — **unchanged**;
  already NMEA-derived and host-clock-free

The derived residual is **retained and reported**, mirroring the pattern
already used for integer-second naming in
`_t6_report_naming_vs_radiod_pair` (spec §6 invariant 5: *"reported only,
no correction applied"*). It is the diagnostic that produced this
document; it simply stops steering.

`chain_delay_calib_s` remains **0** for stage 1. The test is
repeatability, not magnitude, and any constant satisfies it.

## 6. Acceptance criterion

> **Across a night's re-locks, the origin is identical to within one
> sample (10.4 µs at 96 kHz).**
>
> One sample is the natural floor: the asserted origin is an integer
> RTP index plus a constant, so any spread beyond sub-sample rounding
> means something is still being re-derived.

Measured by chrony's HPPS/HFPS offsets holding a single value, and by
`chain_delay_samples` in `bpsk_diff_edges.csv`. The present behaviour —
31.84 vs 47.30 ms at identical configuration — fails this.

Magnitude is explicitly **not** the criterion: the absolute value depends
on `chain_delay_calib_s`, which has never been established on any station.

**This is the experiment that decides whether the wider architectural
change is warranted.** If the origin still scatters after the fix, the
diagnosis is wrong and the wider change would not have helped either.

## 7. Non-goals (stage 1)

* Calibrating `chain_delay_calib_s` to a physical value.
* The wider reframing (hf-timestd selects the highest authority, then
  disciplines the host clock when radiod is local or publishes an RTP
  offset when remote). Deferred pending the criterion above.
* The offset judge's 25 ms bench sigma
  (`offset_judge.py:545 LATENCY_SIGMA_FLOOR_NS`), which makes T6
  unpromotable and simultaneously renders the cross-bench gate unable to
  fire below ~177 ms. Separate defect, separate change.
* Method 2 vs Method 5 selection. Method 2 remains anchor authority; the
  fine stage rides the MF path only.

## 8. Blast radius and rollback

T6's published offset becomes a constant. That affects the HPPS/HFPS
chrony feeds and the offset judge. **Data labels are unaffected** —
`authority.json` is on T3 (WWV/WWVH) with `rtp_to_utc_offset_ns` = 92 µs.
HFPS is `noselect`; HPPS is a rejected falseticker. Nothing currently
steering the clock changes.

Stage 1 is patched on B4's checkout with a backup, not committed, until
the criterion passes. Rollback is file restore plus a service restart.

## 9. Open questions

1. What establishes the physical sub-ms chain delay? No independent
   measurement exists on this station today.
2. Why 58 authority transitions per night? The re-locks are the trigger
   for re-derivation; asserting the origin removes the *consequence* but
   not the flapping. Tracked separately (`edge_period`, `mf_unlock`).
3. Does the same circularity affect T3/FUSION's offset determination?
   `METROLOGY.md` groups T3 with T6 as payload-signal products.
