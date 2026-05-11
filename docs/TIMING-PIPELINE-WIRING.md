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

### 6.6 V1 manifestation in psk-recorder (2026-05-11 21:08 UTC)

The settled-capture work for T6 (§9 step 1, commit `9358e29`) made
us look more carefully at how other ka9q-python consumers capture
their per-channel anchors. **psk-recorder has the same V1 vulnerability
and was silently corrupting its WAV slot timestamps.**

Verified empirically while diagnosing BPSK Costas instability with
a minimal stack (radiod + psk-recorder only, all of hf-timestd
stopped, chrony locked to LAN GPS+PPS at sub-µs):

Observed `inotifywait` capture of WAV writes:

```
21:08:37 → 260511_215100_21074.wav  (filename slot 21:51:00, +42 min in future)
21:08:38 → 260511_220745_24915.wav  (filename slot 22:07:45, +59 min in future)
21:08:41 → 260511_224152_1840.wav   (filename slot 22:41:52, +93 min in future)
21:08:41 → 260511_213752_24919.wav  (+29 min)
21:08:41 → 260511_212152_50318.wav  (+13 min)
21:08:44 → 260511_214315_21140.wav  (+35 min)
21:08:39 → 260511_210830_28180.wav  (correct — current slot)
21:08:39 → 260511_210830_7047.wav   (correct)
21:08:39 → 260511_210830_3575.wav   (correct)
... 6 more channels correct ...
21:08:46 → 260511_210830_5357.wav   (correct)
```

Five channels had slot timestamps wrong by 13 to 93 minutes;
remaining channels were correct.

**Root cause**: each `ensure_channel` call returns a `ChannelInfo`
with `gps_time` and `rtp_timesnap` captured at the moment radiod
created (or last updated) the SSRC for that frequency.
psk-recorder caches the ChannelInfo per channel and uses it
through `ka9q.rtp_to_wallclock` to compute slot start times.
When a channel's SSRC existed in radiod from an earlier era
(before chrony was settled, or before a radiod restart that
shifted the RTP-counter space), the cached anchor inherits the
older / wrong system_time, and projected slot times are wrong by
exactly that amount.

The reason different channels have different errors: radiod
creates SSRCs at different moments. Channels whose SSRCs predate
some discontinuity have stale anchors; channels created freshly
since have correct ones.

**Fix verified**: stopping psk-recorder, waiting for chrony to
report sub-µs `Last offset`, then restarting psk-recorder
forces a fresh `ensure_channel` round, producing correct
anchors for every channel. Post-restart `inotifywait` capture:

```
21:17:54 → ft4/20260511_211745_*.wav   (10 channels — all slot 21:17:45)
21:18:01 → ft8/20260511_211745_*.wav   (6 channels — all slot 21:17:45)
21:18:01 → ft4/20260511_211753_*.wav   (next FT4 slot, all correct)
21:18:09 → ft4/20260511_211800_*.wav   (next FT4 boundary, all correct)
21:18:16 → ft8/20260511_211800_*.wav   (next FT8 boundary, all correct)
```

Every WAV written at slot_start + 9–16 s, every slot_start at a
correct cadence boundary, on every channel.

**Operational implication**: any historical `psk.spots` data
from this station inherits whichever channel-level anchor error
psk-recorder happened to capture. The `score`, `snr_db`, and
message decode are accurate; the *time* assigned to the spot is
wrong by the anchor error for that channel. Spots from
unaffected channels were correct; spots from affected channels
have timestamps off by minutes.

**Generalisation**: V1 is not specific to the TSL3 SHM path.
Any ka9q-python consumer that calls `ensure_channel` once and
caches the result is vulnerable. The consumer audit table in
§10.2 needs updating: every consumer should be assumed
V1-vulnerable unless it either (a) uses buffer-metadata via
`buffer_timing.resolve_buffer_timing` (Flavor B), or (b) blocks
on settled-capture before its first `ensure_channel`.

**Likely also affected (untested 2026-05-11)**: hfdl-recorder,
wspr-recorder, wsprdaemon-client, any future client that uses
the same `ensure_channel` pattern. Each should be audited and
gated on chrony settle.

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

1. **Expose Δ from core-recorder and forward it via the BPSK probe.** ✅ **LANDED** as hf-timestd `0cea2ec` (2026-05-11). See §10 for the per-consumer audit that step 2 onward references.

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
   - **Verified**: `t_level_active=T6`, `rtp_to_utc_offset_ns ≈ 2.4 µs`,
     `disagreement_flags=[]`, T3+T4 agree as witnesses, chrony Last
     offset 1 ns RMS 389 ns. No consumer change; TSL3 SHM unchanged;
     chrony unaffected. Step 1 contract is honest.

   Bonus result: Δ is now *visible* externally via `authority.json` —
   operators can monitor it without inferring from `chronyc tracking`.

2. **Wire one consumer at a time** — see §10 for the per-consumer
   audit. The doc previously prescribed "start with TSL3 SHM"; that's
   wrong (TSL3 SHM is the *producer* of T6's contribution, not a
   downstream consumer). The corrected target depends on which
   consumer's current source is most fragile, per §10's table.

3. **Plumb T2/T4/T5 offset into the manager.** Update
   `AuthorityManager._select_active` so chrony-tracked tiers can
   contribute. Now the cascade fully works under all
   tier-availability combinations.

4. **Add the cross-checks.** Disagreement detection between
   producer and consumer SHM outputs; feedback loop reading
   chrony's own verdict (V7); plausibility gates on `rtp_to_utc_offset_ns`
   magnitude (V9).

5. **The standalone problems.** V2 (per-channel chain_delay), V3
   (WWVH calibration), V10 (Kalman state), V11 (modulator latency).
   Each deserves its own session.

The phasing is designed so each step is independently verifiable
and reversible. The contract is fully expressed by the end of step
3; everything after is hardening.

---

## 10. Consumer contracts

Step 1 closed the **producer** half: hf-timestd publishes a load-bearing
`rtp_to_utc_offset_ns` into `authority.json`. Step 2 closes the
**consumer** half: every code path that produces a UTC-stamped record
or feeds a chrony SHM segment reads the published offset and applies
it consistently.

Before wiring consumers, the architecture has an important non-uniformity
worth surfacing: **not every consumer is equally exposed to anchor
staleness**. Two distinct sources of "RTP-derived UTC" exist in
hf-timestd, and they have different reliability characteristics.

### 10.1 Two flavors of "RTP-derived UTC"

**Flavor A — ka9q ChannelInfo, frozen at `discover_channels()`**

`ka9q.rtp_to_wallclock(rtp_ts, channel)` uses `channel.gps_time` and
`channel.rtp_timesnap`, captured **once** when the consumer process
called `discover_channels()` at startup. radiod's status stream
re-emits these values every cycle, but the consumer's ChannelInfo
isn't refreshed. The anchor inherits whatever system_time error
existed at the moment of capture.

**This is the V1 vulnerability.** It applies to *every*
ka9q-python consumer that calls `ensure_channel` once and caches the
returned `ChannelInfo`. Verified vulnerable so far:

- `core_recorder_v2.py:1366` — TSL3 SHM update path (verified 2026-05-11;
  produced +237 ms offset until restart)
- **psk-recorder slot timing** (verified 2026-05-11, §6.6; produced WAV
  filenames off by 13 to 93 minutes for affected channels)

Likely also affected pending audit: hfdl-recorder, wspr-recorder,
wsprdaemon-client. Every Flavor-A consumer benefits from the same
settled-capture pattern landed for T6.

**Flavor B — per-buffer GPS_TIME from the writer's metadata**

`buffer_timing.resolve_buffer_timing(metadata)` uses the
`gps_time_ns` and `rtp_timesnap` written into each buffer's metadata
by `binary_archive_writer`. The writer captures fresh values via
radiod's status stream and writes them per-buffer. The
`buffer_timing.py` docstring is explicit:

> *"GPS_TIME is the GPSDO-disciplined ground truth."*
> *"start_system_time is NEVER used for timing."*

**V1 does not apply** to consumers that go through buffer metadata.

### 10.2 Consumer audit table

For each consumer, what it stamps, where the stamp comes from, what
it should be under Pattern B, and how exposed it is to V1.

| Consumer | What it writes | Current source | V1-exposed? | Pattern-B target | Priority |
|---|---|---|---|---|---|
| **TSL3 SHM** (`core_recorder_v2.py:1366`) | `clockTimeStamp` (UTC of measurement) + `receiveTimeStamp` (local clock at measurement) | `rtp_to_wallclock(rtp, channel)` with **frozen ChannelInfo** | **Yes** — flavor A | refactor to use per-buffer metadata OR refresh ChannelInfo periodically — both fix V1 without circular dependence on authority | **High**: this IS the producer; V1 fix here closes the most impactful failure mode |
| **Fusion SHM L1** (`multi_broadcast_fusion.py:5376`) | `reference_time = system_time − D_clock_fused_ms/1000`; `receiveTimeStamp = system_time` | direct `time.time()`; `D_clock_fused_ms` computed against per-buffer-anchored L1 records | No — flavor B for the L1 inputs | factor in authority offset OR rely on D_clock_fused being already correct (which depends on L2 contract); subtle, needs care | Medium: behavior near-zero impact while system disciplined; defensive against tier transitions |
| **Fusion SHM L2** (`multi_broadcast_fusion.py:5396`) | same as L1, with L2 measurements as input | same | No — flavor B | same as L1 | Medium |
| **L1 metrology record `timestamp_utc`** (`metrology_service.py:567,616,653,689,727`) | ISO-8601 string of "when this record was written" | `datetime.now(timezone.utc)` | No — audit trail, not measurement time | leave as system_time; audit semantics are correct | Low: audit-trail; downstream readers should use `minute_boundary_utc` for the measurement time |
| **L1 metrology record `minute_boundary_utc`** | epoch int of the minute the tick analysis applies to | `next_minute` integer counter, advanced in lockstep with buffer-derived sample-zero UTC | No — flavor B | leave; measurement time comes from per-buffer fresh anchor | Low |
| **L1 metrology record `d_clock_ms`** | measured residual of WWV/CHU tick vs expected integer-second | RTP-domain edge-detection math, anchored per-buffer | No — flavor B | leave; the measurement IS the cascade's input | Low |
| **L1 metrology record `processed_at`** | when the writer finished the record | `datetime.now(timezone.utc)` | No — audit | leave | Lowest |
| **L2 calibration record `processed_at`, `calibration_date`** | audit-trail timestamps | `datetime.now(timezone.utc)` | No — audit | leave | Lowest |
| **psk-recorder slot WAV timestamps** | `slot_start_utc` in WAV filename + decode log | `ka9q.rtp_to_wallclock(slot_rtp, channel)` with **per-channel frozen ChannelInfo** | **Yes** — same Flavor A as TSL3 SHM (verified 2026-05-11, §6.6); some channels off by 13–93 minutes | settled-capture gate before `ensure_channel`, OR migrate to `buffer_timing` for slot timing | **High** — silently corrupts `psk.spots` time field for affected channels |
| **Future hfdl/psk/codar UTC stamps** (per spot) | UTC-of-detection | currently `datetime.now(timezone.utc)` if not using RTP | No if RTP, **Yes if system_time** | switch RTP-anchored consumers to `buffer_timing` flavor; switch system_time consumers to read authority offset | Medium (per-client) |

### 10.3 The right V1 fix: settled-capture + drift monitor + science archive

(Materially revised 2026-05-11 after empirical testing of both
"refresh the anchor" paths.)

**The original §9 step 2 prescription** ("wire TSL3 SHM to read
`authority.json`") was wrong: TSL3 SHM is the producer of T6's
cascade contribution; reading authority would be circular.

**The first refinement** (path 2a, option 2 — refresh the anchor
periodically via `discover_channels`, compute wall_time via
`buffer_timing.resolve_buffer_timing`) was implemented and *produced
jittery Δ values* (-1.1 ms / -237 µs / +128 µs across three
consecutive PPS edges). Diagnosed and reverted (commit `214c2d8`).

**The second refinement** (path 2a, option 1 — refresh ChannelInfo
in place, keep `ka9q.rtp_to_wallclock`) would inherit the same
jitter, because both paths share the underlying problem: refreshing
the anchor re-samples radiod's `(gps_time, rtp_timesnap)` pair,
which is itself derived from radiod's *system_time* at status-emit
moment. As chrony adjusts system_time, consecutive captures are
not self-consistent — the projection from one anchor to a sample
differs from the projection from a slightly-later anchor by chrony's
slew amount in the interval. *Periodic refresh injects chrony's
drift into Δ.*

**The math actually says**: a *frozen* anchor combined with the
GPSDO-disciplined sample clock projects `wall_time = anchor_system_time
+ elapsed_via_sample_clock`. That projection equals `true_UTC_now +
ε_0` where ε_0 is the chrony discipline error *at capture moment* and
the sample-clock arithmetic preserves the relationship exactly
afterwards. Chrony then sees `Δ = ε_now − ε_0` — the difference
between current and captured discipline error. With ε_0 ≈ 0 (capture
during settled chrony), Δ tracks chrony's *current* discipline error,
which is exactly what we want it to.

**So the V1 fix is a three-layer policy**, not "refresh the anchor":

1. **Settled capture (closes V1's silent-failure mode).**
   Block `_start_t6_stream`'s `discover_channels` call until chrony
   has been settled for N cycles (e.g. `Last offset < 100 µs` for ≥3
   cycles). If T6 starts before chrony settles, the anchor inherits
   chrony's startup error and propagates it forever (the +237 ms
   incident). Settled-capture means the anchor is captured when
   ε_0 ≈ 0.

2. **Drift monitor + conditional re-capture (closes V1's
   slow-failure modes).** Surface a flag when Δ exceeds expected
   sigma over a sustained window. Triggers for re-capture, in
   priority order:
   - radiod restart (detected via RTP counter discontinuity or
     wildly different `gps_time` in current status vs anchor)
   - Sustained Δ above a hard threshold (e.g. > 1 ms for > 60 s)
   - Operator-initiated diagnostic

3. **Science archive (long-term assessment).** Each authority cycle
   already publishes
   `(utc_published, t_level_active, rtp_to_utc_offset_ns, sigma_ns,
   disagreement_flags)`. Piping that time-series into the existing
   ClickHouse infrastructure (the `timestd.events` table or a new
   `timestd.authority` table) gives a queryable record of:

   - GPSDO drift vs. UTC over hours/days (slow walk in Δ)
   - BPSK injector or modulator behavior (sudden steps, periodic
     structure)
   - Chrony discipline quality (Δ's variance)
   - Anchor health (steps that don't correspond to known chrony
     events)

   This is the "watch for degradation *and* enhancement" path the
   user described — it requires *recording* Δ, not refreshing the
   anchor.

**Why "freeze and monitor" not "freeze forever":** the anchor is
correct as long as the sample clock is exact AND ε_0 ≈ 0 at
capture. If the GPSDO walks, the sample-clock guarantee weakens.
If radiod restarts, the RTP-counter space changes. If we discover
ex post that ε_0 was non-zero, a re-capture closes the residual
gap. The cascade infrastructure already detects these conditions
(V6 cross-check, V7 chrony feedback); we just need a policy that
*acts* on the detection by re-capturing when appropriate.

**The poll-thread plumbing already shipped** (commit `214c2d8`)
is repurposable: instead of "refresh the anchor every cycle", it
becomes "compare radiod's current status to the captured anchor,
raise a flag if they've drifted too far apart, trigger a
re-capture on a hard threshold." Same code path, different policy.

**Path 2b** (wire L1/L2/fusion SHM consumers to apply offset)
remains relevant but lower priority per the consumer audit (§10.2):
most consumers are V1-immune via Flavor B already. Pattern B
hardening here is a defensive move, not a bug fix.

**Recommendation**:

1. Implement settled-capture gate in `_start_t6_stream`.
2. Add drift monitor + flag (reuse the poll-thread plumbing
   already in place).
3. Define and implement the re-capture trigger logic.
4. Wire the authority time-series archive into ClickHouse.

Each step is independently verifiable. The first two are
defensive; the third actively repairs; the fourth supports
long-term science.
