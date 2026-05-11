# Timing pipeline — wiring the wall-clock contract

**Status:** open design discussion, no policy decisions yet.
**Audience:** Rob, Michael — and future contributors arriving cold.
**Why this exists:** the timing pipeline has working components but
inconsistent wiring between them. The +237 ms TSL3 chronyc reading of
2026-05-11 is a symptom; the deeper finding is that two parts of the
codebase document mutually contradictory contracts for how UTC reaches
a data record. This document picks a contract and traces every wire
that needs to honour it.

This is the runtime sibling of `METROLOGY.md §4.5` (which defines the
T-level taxonomy and the `authority.json` schema) and presupposes
familiarity with that section.

---

## 1. The principle

> **hf-timestd, in conjunction with radiod, sets the wall clock.**

We do not inherit "current UTC" from Linux's `CLOCK_REALTIME` and trust
it. We compute it ourselves, from the highest-authority source
currently available, and *publish* the offset for every consumer that
needs to stamp data with UTC.

`system_time` (Linux's clock) is a substrate — convenient because it
ticks predictably, and because chrony can be steered toward our
published UTC over time. But at any given instant, system_time may be
wrong by milliseconds (worst case, seconds). The wall clock we
*stamp* with cannot be system_time directly.

What hf-timestd publishes IS the wall clock for the rest of the
station. Sidecars, archives, SHM feeds, mDNS advertisements,
downstream science clients — all consume the same number and apply it
consistently. If hf-timestd loses its authority sources entirely
(T0), the wall clock collapses to system_time and that fact is
explicit in the published state, not silent.

---

## 2. Six clocks — definitions locked

Every piece of code below is one of these. Naming carefully because
they're routinely confused.

| Clock | What it is | Set by | Authority |
|---|---|---|---|
| `system_time` | `time.time()` / `CLOCK_REALTIME`; the Linux kernel clock | chrony, ultimately disciplined by whatever SHM/NTP source it selects | inherits whatever chrony's source provides — may include hf-timestd's own SHM output (feedback) |
| `radiod_clock` | the clock radiod reads when emitting `gps_time` into its status protocol | `system_time` (same kernel clock) | = `system_time` |
| `rtp_grid` | the monotonic RTP timestamp counter per SSRC, ticking at `sample_rate` | radiod's DSP loop; origin anchored to `(gps_time, rtp_timesnap)` at SSRC creation | **rate-accurate from A1** (the GPSDO); **origin-accurate only to `radiod_clock` at the anchor moment** |
| `raw_wall_time` | output of `ka9q.rtp_recorder.rtp_to_wallclock(rtp_ts, channel)` with `chain_delay_correction_ns = None` | projects `rtp_grid` forward from the anchor using `sample_rate` | tracks `system_time` *as it was when the anchor was captured* — frozen at anchor time |
| `utc_time` | true UTC; the thing science requires | nobody currently — that is the architectural bug | should be set by the **authority cascade** (T6 → T5 → T4 → T3 → T2 → T1 → T0) |
| `wall_clock` | the time stamped on our data | **hf-timestd**, by publishing `rtp_to_utc_offset_ns` from the active T-level | T6 if available, else T5, …, else T0 |

In the steady-state success case, **`wall_clock = utc_time`** by
definition. We compute the offset between `system_time` (or
`raw_wall_time`, which is anchored to it) and `utc_time`, and that
offset *is* the published wall-clock correction.

A consumer that needs to stamp a record with UTC computes:

```
utc_time = raw_wall_time(rtp_ts, channel) + rtp_to_utc_offset_ns
```

or, for a consumer that doesn't have an RTP timestamp:

```
utc_time = system_time + rtp_to_utc_offset_ns
```

These are the same offset (within ka9q anchor noise). The first form
is preferred where an RTP timestamp is available because it pins the
stamp to the actual sample, not to the moment the writer happened to
call `time.time()`.

---

## 3. The cascade is already built — we wire to it

`METROLOGY.md §4.5` defines the authority schema. The runtime is
implemented in `hf_timestd/core/authority_manager.py`. The probes
exist:

| Probe | Tier | Status |
|---|---|---|
| `BpskPpsProbe` | T6 | implemented, currently publishes `offset_ms = 0` (see §6) |
| `ChronyTrackingProbe` | T2 / T4 / T5 | implemented, publishes `offset_ms` from `chronyc -n -c sources` |
| `FusionStatusProbe` | T3 | implemented, reads `/run/hf-timestd/fusion_status.json` |
| `GpsdoProbe` | A1 (separate axis) | implemented |

The manager polls them on a fixed cadence (default 30 s), applies
upgrade hysteresis (default 3 consecutive availabilities), selects
the highest-rank available tier, and writes `authority.json`. Its
`AuthorityState.rtp_to_utc_offset_ns` is the single load-bearing
number that every consumer should consume.

**The cascade is correct. The consumers are not subscribed.** Every
fragility documented in §7 traces to a consumer reading something
other than `authority.json`.

---

## 4. The wall-clock contract (Pattern B)

Two patterns can produce identical math:

**Pattern A — apply at the channel.** When BPSK calibrator locks,
core-recorder writes `channel.chain_delay_correction_ns = chain_delay_ns`
on every channel. `ka9q.rtp_to_wallclock` then subtracts it
automatically; `raw_wall_time → utc_time` becomes a no-op rename.
Per-tier corrections beyond T6 (T4 chrony, T3 fusion) have no place
to live in this pattern.

**Pattern B — publish offset, apply at every consumer.** The cascade
computes `rtp_to_utc_offset_ns`. Every consumer reads
`authority.json` and adds the offset. `chain_delay_correction_ns`
stays `None`; `raw_wall_time` stays raw. This pattern generalises
cleanly to all tiers.

**We choose Pattern B.** Three reasons:

1. The cascade infrastructure already exists for Pattern B — the
   `rtp_to_utc_offset_ns` field is named that way, not
   `chain_delay_ns`, because the original schema designer expected
   it to carry tier-agnostic offsets.
2. Pattern A only expresses T6's contribution. T4 (LAN GPS via
   chrony) and T3 (fusion) need a publication channel too; Pattern A
   has none.
3. One load-bearing number is one place to monitor, alarm, and
   sanity-check. The "is wall_clock plausible right now" question
   has a single answer.

Pattern A's cost is what makes Pattern B worth its overhead: under A,
the `rtp_to_utc_offset_ns` field would have to remain a permanent
lie (always 0 by construction). That is exactly the contradiction
that produced the +237 ms incident.

### 4.1 Sign convention — locked

All probes in the cascade use a single convention for `offset_ms`,
matching `ChronyTrackingProbe`'s existing behaviour:

> **`offset_ms = local_clock − source_UTC`**
> (positive when the local system clock reads after the source's view of UTC)

Consumers apply the offset as `utc_estimate = system_time − offset_ms / 1000`.
T6's contribution under this convention is **not** `chain_delay` — that
would be a physical RX-chain latency, a different quantity. T6 publishes
the **residual Δ** that the BPSK-PPS SHM math already computes
internally: the fractional-second disagreement between
`raw_wall_time − chain_delay` and the integer-second GPS source. When
the system is well-disciplined Δ is sub-µs; when the anchor is stale
(V1), Δ inflates to the anchor's accumulated error.

This was confirmed empirically — see §6.5.

---

## 5. Bootstrap walk-through

How the cascade reaches a stable `wall_clock` from each cold-start
scenario. Each scenario assumes the GPSDO (A1) is present and the
ADC sample rate is therefore rate-accurate.

### 5.1 Cold start, no UTC source available

Tiers T6, T5, T4, T3, T2 all unavailable. Cascade falls to T1 (A1
holdover) or T0 (no authority).

- `rtp_to_utc_offset_ns = 0` (or `null`)
- `t_level_active = "T1"` or `"T0"`
- Data products are stamped with `system_time` and flagged
  `no_utc_alignment_available` per `METROLOGY.md §4.5`
- Acceptable degraded state; explicit, not silent

### 5.2 Cold start, WWV or CHU audible

Tier T3 (Fusion) becomes available once two stations lock.
Bootstrap *integer-second* alignment comes from BCD (WWV) or FSK
(CHU) decoding — these convey absolute UTC time-of-day. Fusion's
tick-alignment refines fractional seconds.

- Cascade promotes to T3 after `upgrade_hysteresis` cycles
- `rtp_to_utc_offset_ns` populates from fusion's own offset estimate
- Sigma is ms-scale; published as such

### 5.3 Cold start, LAN GPS+PPS available (T4)

`ChronyTrackingProbe` for T4 reports the LAN peer offset as soon as
chrony reaches it. Cascade promotes to T4.

- Cascade promotes T4 once T4 probe reports `available=True`
  consecutively
- `rtp_to_utc_offset_ns` populated from chrony's tracking offset
- Note: T4 currently reports `offset_ms` but
  `AuthorityManager._select_active` does **not** plumb it into
  `rtp_to_utc_offset_ns` for chrony-disciplined tiers. This is one
  of the wiring fixes (§6.2).

### 5.4 Cold start, BPSK PPS injector wired (T6)

Once BPSK calibrator locks (typically within ~30 s of receiving
samples), T6 probe reports `available=True`. Sub-µs precision.

- Cascade promotes T6
- **Disambiguation**: T6 measures a sub-second edge offset, but doesn't
  know which integer GPS second the edge belongs to. The current
  one-shot disambiguation against T3/T4/T5 in `core_recorder_v2.py:1200-1268`
  becomes redundant under Pattern B — the active cascade already
  resolves "which second" via its lower-tier witnesses. T6's offset
  contribution is *fractional only*; the integer-second comes from
  the cascade.

### 5.5 Steady state

All available tiers up; cascade publishes T6 offset.

- chrony's TSL3 SHM consumer eventually disciplines `system_time`
  toward `utc_time`
- `rtp_to_utc_offset_ns` shrinks toward zero over hours as chrony
  converges
- Static error (TCXO drift between PPS ticks, modulator latency,
  etc.) absorbed into the persistent residual; flagged if larger
  than tier sigma

---

## 6. The current contradictions, by file

These are the contract violations that must be resolved to wire
Pattern B coherently.

### 6.1 BPSK probe declares Pattern A; data path follows Pattern B

`hf_timestd/core/bpsk_pps_probe.py:19-21`:

> *"offset_ms is published as 0.0: BPSK directly defines the wall-time
> alignment via chain_delay_correction_ns applied at rtp_to_wallclock,
> so from T6's reference frame there is no residual RTP→UTC offset."*

`hf_timestd/core/core_recorder_v2.py:1333-1343`:

> *"we no longer set chain_delay_correction_ns on the recorder
> ChannelInfos … archive wall_times stay raw (RTP-derived without
> chain_delay), and downstream readers apply the correction if they
> want UTC alignment."*

The probe asserts "applied"; the writer asserts "not applied". Both
cannot be true. **Resolution under Pattern B:** the probe publishes
`offset_ms = chain_delay_ns / 1_000_000` (in ms units that match the
schema), not zero.

### 6.2 AuthorityManager doesn't propagate offset from chrony-tracked tiers

`authority_manager.py:_select_active`:

```python
if active in ("T3", "T6"):
    if a_res.offset_ms is not None:
        offset_ns = int(round(a_res.offset_ms * 1_000_000))
```

T2/T4/T5 probes report `offset_ms` but the manager discards it when
selecting them as active. **Resolution under Pattern B:** plumb
`offset_ms` from every tier into `rtp_to_utc_offset_ns`. Each tier
contributes its own sigma; consumers can gate on combined
uncertainty.

### 6.3 Four consumers, four different timestamp sources

| Consumer | Current source | Pattern-B target |
|---|---|---|
| L1 metrology stamping (`metrology_service.py`) | `datetime.now(timezone.utc)` | `rtp_to_wallclock(rtp, ch) + rtp_to_utc_offset_ns` |
| L2 calibration (`l2_calibration_service.py`) | `datetime.now(timezone.utc)` | same |
| Fusion → SHM L1/L2 (`multi_broadcast_fusion.py:5376`) | `system_time` with no authority offset | `system_time + rtp_to_utc_offset_ns − D_clock_fused_ms/1000` |
| Core-recorder → SHM TSL3 (`core_recorder_v2.py:1366-1373`) | `rtp_to_wallclock(...) − chain_delay`, then `round()` | `rtp_to_wallclock(rtp, ch) + rtp_to_utc_offset_ns`, no `round()` |

Each consumer becomes a one-line read of `authority.json` (or a
shared in-process cache where the producer is local) plus an addition.

### 6.4 SHM segments are written by three producers, not one

`METROLOGY.md §4.5` line 330 specifies the AuthorityManager loop as
"the single gate" for the three published outputs including chrony
SHM. The actual code has:

- AuthorityManager: writes `authority.json` ✓
- fusion: writes SHM unit 0 (L1) and unit 1 (L2) — independent of
  AuthorityManager
- core-recorder: writes SHM unit 2 (T6/TSL3) — independent of
  AuthorityManager

This is partly historical (fusion and core-recorder predate the
authority manager) and partly architectural — fusion *is* the L1/L2
producer. **Resolution:** the producers retain the SHM-write
responsibility, but each one consults `authority.json` for offset.
The "single gate" property of §4.5 means the *contract* is
single-sourced even if the writes are distributed.

---


### 6.5 Empirical finding from 2026-05-11 02:34 UTC (step 1 verification)

Initial implementation of §9 step 1 (have `BpskPpsProbe` publish
`offset_ms = chain_delay_ns / 1_000_000`) was performed against bee1
to verify the producer-side wiring. The result revealed a semantic
mismatch:

- BPSK probe published `offset_ms = 174.147` (chain_delay in ms)
- `ChronyTrackingProbe` (T4) published `offset_ms ≈ 0` (chrony's
  measured offset against `time` source, currently sub-µs)
- `FusionStatusProbe` (T3) published `offset_ms ≈ 0` (fusion D_clock,
  currently sub-ms)

`AuthorityManager._cross_check` correctly identified this as a
disagreement:

```
disagreement_flags: [
  "T6<->T4:174.153ms>6.002ms",
  "T6<->T3:174.295ms>3.746ms",
  "majority-downgrade:T6->T4"
]
```

T6 was refused promotion despite being structurally available. The
cascade's safety machinery worked exactly as intended.

**Conclusion:** publishing `chain_delay_ns` as `offset_ms` is wrong on
the semantic — it's a physical RX-chain latency, not a clock-vs-source
offset. The right value to publish is Δ (the BPSK SHM residual that
chrony observes). Δ is already computed inside
`core_recorder_v2.py:1352-1382` as
`wall_time_sec - round(wall_time_sec)`; it just isn't currently
exposed.

The change was reverted. §9 step 1 is reformulated below to expose Δ
via the status file's `l6_pps` block, then have the probe read and
forward it.

**Bonus finding (V6 partially closed):** the cascade's `_cross_check`
+ `_maybe_majority_downgrade` machinery is fully implemented. The V6
fragility ("no cross-validation between SHM producers") was already
addressed at the *probe* level — what's missing is using the same
machinery against the SHM segments themselves. The infrastructure for
that exists.

## 7. V-list — pipeline fragilities and what Pattern B fixes

From the 2026-05-11 review session.

| # | Fragility | Pattern B impact |
|---|---|---|
| V1 | `rtp_to_wallclock` anchor captured once at startup, inherits system_time error at that moment | **closed** — anchor noise is absorbed into `rtp_to_utc_offset_ns`, which the cascade refreshes continuously |
| V2 | BPSK chain_delay applied uniformly across 2.5–25 MHz | **standalone** — Pattern B doesn't fix this; needs per-channel group-delay model |
| V3 | L2 propagation correction inflating WWVH by ~40 ms | **standalone** — bug in `l2_calibration_service`; exposed by cascade cross-check but not fixed by it |
| V4 | TSL3 SHM uses `round(wall_time_sec)` for integer-second choice | **closed** — integer second comes from the cascade's published offset, not `round()` |
| V5 | `rtp_to_utc_offset_ns` published in `authority.json` but never applied | **closed** — Pattern B *is* the application |
| V6 | Three SHM segments with no cross-validation between them | **partially closed** — `AuthorityManager._cross_check` machinery is already implemented at the probe level (verified 2026-05-11, see §6.5); applying the same logic to SHM-segment outputs is the remaining work |
| V7 | Chrony's "falseticker" marking on TSL3 is silent | **standalone** — needs a feedback loop reading `chronyc tracking` for hf-timestd's own SHM segments |
| V8 | One-shot disambiguation at first lock, never refreshed | **closed** — replaced by continuous cascade orientation |
| V9 | No precision-domain plausibility check between layers | **partially closed** — single offset with single sigma makes plausibility one check; cross-layer assertions still want explicit testing |
| V10 | Fusion restart drops in-process Kalman state | **standalone** — Kalman-state persistence is independent |
| V11 | BPSK PPS edge detection assumes integer GPS second alignment with no modulator-latency model | **partially closed** — cascade exposes the persistent disagreement; per-deployment calibration constant still needs to be defined and applied |

Pattern B closes 4 of 11, partially closes 4 more, and leaves 3 as
genuinely independent problems.

---

## 8. Open design questions

1. **Sigma propagation under Pattern B.** When the cascade publishes a
   tier-specific sigma, how do consumers combine it with their own
   measurement noise? L2 calibration in particular has its own
   uncertainty model — should it broadcast `sigma = sqrt(L2² +
   cascade²)`, or treat them as orthogonal axes?

2. **`authority.json` freshness as a gate.** §4.5 documents staleness
   handling for downstream consumers. What's the right behaviour
   when the authority is stale during a single SHM-write cycle —
   skip the cycle, write with degraded sigma, fall back to the
   last-good value? Each has different chrony-side consequences.

3. **In-process cache vs file re-read.** Fusion, core-recorder, L2
   calibration all run as separate services. They could each
   re-read `authority.json` on every consumer event (cheap; loose
   coupling) or subscribe to a shared cache via signal/inotify
   (tighter; one fewer file open per measurement). For the SHM
   write paths the cost difference is negligible; for L1 metrology
   stamping at sample rate it might matter.

4. **What about radiod's clock itself?** `radiod_clock` is the input
   to `ka9q.rtp_to_wallclock`. If radiod is running on a host where
   `system_time` is materially different from `utc_time` (say chrony
   is still settling), the captured anchor inherits that error.
   Pattern B absorbs this into `rtp_to_utc_offset_ns`, but **only
   if the anchor is captured against a known-stable `system_time`**.
   Should hf-timestd refuse to discover channels until chrony
   reports `Last offset` below some threshold? Or should the cascade
   model anchor uncertainty explicitly as a per-channel additive
   term?

5. **Pattern A as a special case under Pattern B.** Setting
   `channel.chain_delay_correction_ns` on the BPSK channel *only*
   would let the calibrator's own RTP-domain math stay anchor-clean.
   Should we keep this hybrid (Pattern B everywhere except inside
   the calibrator), or remove the field entirely?

6. **T3 fusion needs T-level-aware anchors too.** If fusion's L2
   measurements are stamped with `system_time` (per §6.3), and
   chrony is then disciplined by fusion's SHM output, there's a
   subtle feedback loop. Pattern B breaks the loop by stamping with
   `system_time + offset`, but the offset itself was computed from
   measurements stamped with `system_time`. Does this converge?
   (Intuition says yes, since the offset is small and the loop is
   weak, but worth a formal pass.)

7. **chain_delay non-reproducibility across cold starts (V8).** Three
   observed BPSK locks on bee1 yielded three different chain_delay
   values for the same physical chain: 334.147 ms (2026-05-10 main
   lock), 794.147 ms (2026-05-11 02:11 transient), 174.147 ms
   (2026-05-11 02:13 post-restart). These differ by hundreds of ms
   each. The wrap-disambiguation at first lock picks a different
   "integer GPS second the edge belongs to" each time. Final
   wall-clock is correct in each case (Δ converges), but the *published
   chain_delay* is not a reproducible physical measurement — which
   means it cannot serve as a per-deployment calibration constant
   (relevant for V11 modulator-latency work). The disambiguation
   correctness deserves its own investigation; the rough hypothesis is
   that without a stable lower-tier reference at lock time, the
   "as-is" acceptance branch (`core_recorder_v2.py:1217`) lands on
   whichever integer-second the calibrator state machine happened to
   converge to. This may be related to the time of day at lock or to
   sample-buffer phase.

---

## 9. Suggested implementation phasing

Not a commitment — a draft sequence.

1. **Expose Δ from core-recorder and forward it via the BPSK probe.**
   (Reformulated 2026-05-11 after the original "publish chain_delay"
   prescription proved semantically wrong — see §6.5.)

   - In `core_recorder_v2.py` at the TSL3 SHM update site
     (lines 1352-1382), compute the residual
     `local_minus_source_ns = int(round((wall_time_sec − round(wall_time_sec)) × 1e9))`
     and write it into the `l6_pps` block of
     `/var/lib/timestd/status/core-recorder-status.json` alongside
     `chain_delay_ns`.
   - In `BpskPpsProbe.poll()`, read the new field and publish
     `offset_ms = local_minus_source_ns / 1_000_000`. Treat the field
     as required (probe returns `available=False` if missing) so a
     stale producer can't poison the cascade.
   - Restart `timestd-core-recorder` and `timestd-fusion`. Observe
     `authority.json` carries `rtp_to_utc_offset_ns ≈ Δ` (sub-µs in
     the steady state). Confirm no `disagreement_flags` against T3/T4.
   - No consumer changes; TSL3 SHM consumer unchanged; chrony unaffected.

   Verifies the producer side honestly. The bonus is that Δ is now
   *visible* externally — operators can monitor it without inferring
   from `chronyc tracking`.

2. **Wire one consumer at a time, starting with TSL3 SHM.** Make
   `core_recorder_v2.py:1352-1382` read `authority.json` and replace
   the `round() + manual chain_delay subtract` logic. Observe TSL3
   offset on chrony drops to expected sigma range.

3. **Wire the L1/L2 stamping next.** Switch
   `metrology_service.py` and `l2_calibration_service.py` to
   `rtp_to_wallclock + offset`. Verify cross-frequency divergence
   on WWV/CHU bands shrinks (V2/V3 are independent but Pattern B
   exposes them more cleanly).

4. **Plumb T2/T4/T5 offset into the manager.** Update
   `AuthorityManager._select_active` so chrony-tracked tiers can
   contribute. Now the cascade fully works under all
   tier-availability combinations.

5. **Add the cross-checks.** Disagreement detection between
   producer and consumer SHM outputs; feedback loop reading
   chrony's own verdict (V7); plausibility gates on `rtp_to_utc_offset_ns`
   magnitude (V9).

6. **The standalone problems.** V2 (per-channel chain_delay), V3
   (WWVH calibration), V10 (Kalman state), V11 (modulator latency).
   Each deserves its own session.

The phasing is designed so each step is independently verifiable
and reversible. The contract is fully expressed by the end of step
4; everything after is hardening.
