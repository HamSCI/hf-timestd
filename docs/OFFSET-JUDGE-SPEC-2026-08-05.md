# The Offset Judge — restoring offset-based timing correction to hf-timestd

Status: **ACCEPTED 2026-08-05** — open questions resolved by AC0G (§13).
This document is the implementation contract. mjh redlines still welcome;
they amend, not block.

Provenance: written 2026-08-05 after the AC0G-B4 anchor-wedge incident
(radiod's advertised epoch ~1203 s wrong for six days; recording starved)
and the full timing audit (see wd30:~/save/TIMING-AUDIT-2026-08-05.md).
The architecture below is AC0G's original design statement, restored:

> "However radiod establishes its timing reference, this process should
> enable setting accurate offsets based on the established taxonomy, and
> the timing of sampling should be based on the offsets. Any trends in
> the difference between the radiod RTP timestamps and hf-timestd's best
> judgment will provide a clear record of the timing quality of the data.
> If radiod's timing originates in the same GPS+PPS source, hf-timestd's
> offsets will be close to zero."

---

## 1. Doctrine

Two distinct claims, never to be conflated again:

1. **The steel ruler.** The RX888's ADC is clocked by a GPSDO. RTP sample
   counters therefore advance at a GPS-disciplined *rate*. The counters
   are the substrate and the ruler; hf-timestd trusts the tick spacing.
2. **The epoch is a measurement, not an axiom.** radiod's advertised
   `(GPS_TIME, RTP_TIMESNAP)` pair maps counters to UTC using *its host's
   system clock* — a machine possibly not ours, disciplined by means not
   ours. hf-timestd never adopts this mapping as truth. It **measures**
   the difference between radiod's mapping and its own best judgment of
   UTC, applies that difference as a correction (an *offset*), and records
   the offset and its trend as first-class metadata.

Corollaries:

- radiod being wrong ceases to be an emergency. With offsets applied,
  wrong-epoch radiod produces correct labels plus a large, loudly-logged
  offset — not data loss. Restarting radiod becomes cosmetic hygiene,
  not a data-rescue operation.
- Samples are **never dropped for timing reasons**. Degrade the label's
  pedigree, stamp the pedigree, keep the data. (Backlog shedding — a
  genuinely overloaded pipeline — remains legitimate and is detected
  separately; see §6.)
- The offset trend IS the timing-quality record the science consumers
  get. Offsets ≈ 0 verifies co-located GPS sources; a step marks a
  radiod restart or clock event; a slope measures rate disagreement.

## 2. The judge and its bench (the taxonomy, operationalised)

A single component, the **Offset Judge**, computes hf-timestd's best
UTC(rtp) independently of radiod, from the best available source:

| Tier | Source | How UTC(rtp) is obtained | Present state |
|---|---|---|---|
| T6 | HF-injected BPSK PPS (TS-1) | `NativeAnchor` — matched-filter edge paired with LB-142x second; pure counter arithmetic, host-clock-free | EXISTS (`core/native_anchor.py`), used only for chrony push |
| T5 | LB-142x GPS+PPS via USB | NMEA `pps_utc_sec` + DCD edge paired against the RTP counter of concurrently-arriving samples | Probe exists; the pairing product (`anchor_offset_ns`) is consumed but **never produced** — must be built |
| T4 | LAN GPS+PPS (user-supplied or auto-discovered IP) | chrony tracking against that peer; wallclock is then a calibrated proxy with known σ | chrony machinery exists (`ChronyTrackingProbe`); site-timing already auto-discovers LAN stratum-1 |
| T3 | FUSE (WWV/H / WWVB fusion) | fusion's own UTC reconstruction, referenced to the RTP substrate | EXISTS (`FusionStatusProbe`, fusion status file) |
| T2 | chrony on WAN NTP | as T4 with larger σ | exists |
| T0/T1 | free-run / GPSDO coast | last good offset held; σ grows; stamped as such | trivial |

Judgement rule: highest available tier wins, with the existing
hysteresis discipline (N consecutive good polls to advance, immediate
degrade on loss). Each tier reports (utc_of_rtp, sigma, age). This is
the *same* cascade the AuthorityManager already implements — the change
is that its output finally **governs the data path** (audit gap G1).

## 3. The offset

For each radiod source s (scoped per status-stream/SSRC — see §7):

```
offset_s(t) = UTC_judge(rtp) − UTC_radiod_s(rtp)      [same rtp counter]
label(rtp)  = UTC_radiod_s(rtp) + offset_s            [what data gets]
```

- Estimated on a periodic tick (default 10 s) and at every radiod
  anchor adoption. Filtered (median-of-N then EMA) to reject status
  jitter; a step larger than the filter's plausibility band opens a new
  **segment** (§5) rather than being smoothed.
- Published continuously to `/run/hf-timestd/offset_judge.json`:
  per-source {offset_ns, sigma_ns, tier, judge_age_s, segment_id,
  d_offset_dt_ppm, last_step}. This file is the fleet-visible trend
  record; smd status and the web dashboard render it.
- Stamped into **every** chunk sidecar (§8). Post-hoc, any consumer can
  reconstruct both the raw radiod mapping and the corrected one.

When radiod and the judge share one GPS+PPS source, offset ≈ 0 ± σ —
which is the health verification, continuously, for free.

## 4. Consumers

Phase-ordered (see §10):

1. `BinaryArchiveWriter` — labels chunks with corrected time; its
   staleness guard is replaced by the split detector of §6.
2. `RingBuffer` / metrology — same corrected mapping (today the ring and
   archive can silently diverge; audit G6 history).
3. GRAPE packager — Digital RF start index from corrected time.
4. **Exported to sigmond recorders** (wspr/psk/meteor slot clocks and
   mag-recorder timestamps, per AC0G's design statement). Contract: the
   recorders MAY consume `/run/hf-timestd/offset_judge.json` when
   present, retaining their own dt-guards as backstops. Cross-repo;
   phase 4; requires mjh sign-off on the sigmond side.

## 5. Segments (sample-loss and step honesty)

The steel ruler has known fracture modes: USB sample loss, radiod
restarts, 32-bit RTP wrap, SSRC re-grants. On any of these:

- close the current segment, open a new one (monotonic `segment_id`),
- re-estimate the offset fresh (no smoothing across the fracture),
- record the fracture cause in the segment metadata.

The offset trend is therefore piecewise — steps between segments are
*events with causes*, slopes within segments are *rate disagreement*.
Consumers must never interpolate across a segment boundary.

## 6. Replacing the staleness guard: the split detector

The old guard conflated two failure modes behind one wallclock test
(audit G8). They are separable with data already at the drop site:

| Observable | Pipeline backlog | Anchor fault |
|---|---|---|
| d(lag)/dt | grows ≈ 1 s/s | ≈ 0 (constant lag) |
| Arrival rate (rtp delta / wall delta) | < 1× real-time | ≈ 1× real-time |
| Correct response | shed load (drop oldest), alarm | judge takes over via offset; alarm; escalate per §9 |

Both signs are watched (the old guard was past-only; metrology's was
future-only; audit G8).

## 7. Scoping and the March 2026 lesson

The continuous poll was removed (commit `2d54c9c`) because *global*
`discover_channels()` mixed SSRC-colliding status from multiple
decoders. The judge's radiod-side observations MUST be scoped to the
client's own per-source status stream (as seed-from-`channel_info`
already is). Per-source keys: (status stream, SSRC). Global discovery
is forbidden in the judge. This restores the March fix's intent while
undoing its collateral damage (seed-once).

## 8. Provenance schema (per chunk sidecar, additive)

```
"timing": {
  "radiod_gps_time_ns":  <as advertised>,
  "radiod_rtp_timesnap": <as advertised>,
  "offset_ns":           <applied correction>,
  "offset_sigma_ns":     ...,
  "judge_tier":          "T6|T5|T4|T3|T2|T1|T0",
  "judge_age_s":         ...,
  "segment_id":          ...,
  "rate_ppm":            <measured, §11; null until phase 3>
}
```

Rule: a chunk without a `timing` block is legacy; a chunk with one is
fully self-describing. No more indistinguishable GPS-grade vs NTP-grade
data (audit G10).

## 9. Escalation ladder (sustained radiod contradiction)

Offsets keep the data correct, so escalation is about hygiene and
operator awareness, in order:

1. `|offset| > tier_bound` sustained 60 s → CRITICAL log (rate-limited)
   + offset_judge.json flag; smd status shows ✗ with the number.
2. Sustained 15 min → alert artifact (same channel as freshness alerts)
   naming the likely cause (constant lag ⇒ radiod epoch; slope ⇒ rate).
3. Sustained 60 min AND config `[timing.offset_judge] radiod_restart =
   true` (default **false**; site policy) → request radiod restart via
   the sigmond watchdog interface — the first and only place any
   component is empowered to touch radiod, and it is opt-in.

Severity thresholds are tier-relative and **empirical**: each bench
continuously reports its own measured σ, and a source is in violation
when |offset| exceeds k·σ_tier (k = 5 default) sustained over the
window. The tiers' demonstrated accuracy defines the bounds — no
hand-authored table to go stale (decision §13.1); never the old
calendar-day scale (audit G11).

## 10. Migration phases

- **P0 (done 2026-08-05):** revert `f4bbded`; clean baseline.
- **P1:** Offset Judge core + T4/T2 (chrony) + T3 (FUSE) benches;
  writer consumes it; split detector replaces staleness guard;
  provenance schema. *Testable in nest + on B4 immediately.*
- **P2:** T5 producer (`anchor_offset_ns` finally emitted — closes the
  hardcoded-zero cross-check, audit G5b) + T6 `NativeAnchor` bench;
  periodic re-validation tick; ring/metrology unification.
- **P3:** frequency loop — revive `TimingMetricsWriter` tone-to-tone
  ppm; publish `rate_ppm`; record only, never resample (audit G7).
- **P4:** escalation ladder incl. opt-in radiod restart; sigmond
  recorder export contract (with mjh).
- Each phase: unit tests + a scripted fault-injection test on B4
  (offset a fake radiod pair; assert label correctness + provenance +
  alarm), because the nested rig has no RF.

## 11. Deliberate non-goals

- No resampling / no rewriting of sample data, ever. Rate error is
  *recorded* (`rate_ppm`), not "corrected" into the samples.
- No steering of radiod or its host clock (beyond the opt-in restart).
- No new daemon: the judge lives in core-recorder's process (it already
  holds T5/T6 machinery), publishing for out-of-process consumers.
- FUSE loop: at T3-only same-host sites the loop (FUSE→chrony→host
  clock→radiod epoch) is damped because the judge references FUSE's
  substrate-based reconstruction directly, and corrections are never
  fed back into FUSE's inputs. Documented, watched via the trend.

## 12. Open questions — RESOLVED, see §13

## 13. Decisions (AC0G, 2026-08-05)

1. **Bounds are empirical.** The measured accuracy of each tier defines
   its violation bound (k·σ from the bench's own live statistics), not a
   static table. Folded into §9.
2. **sigmond recorders keep their independent dt-guards permanently** as
   diverse redundancy; the P4 judge export is additive advice, never a
   replacement for their own defenses.
3. **The opt-in radiod restart request stays** in the escalation ladder
   (§9 step 3), default off, site policy to enable.
4. **The 07-31..08-04 sliver scar stands.** No retro-relabeling tooling.
   The gap is the honest record of the failure we learned from.

## 14. Amendment (2026-08-05): cross-bench consistency gate

§2's judgement rule is **amended** by
`JUDGE-CROSS-BENCH-GATE-2026-08-05.md` (accepted; motivating incident
`T6-DISPLACED-PEAK-62MS-2026-08-05.md`): tier *advancement* additionally
requires the candidate bench to agree with the highest already-trusted
lower tier within `k_x·sqrt(σ_c² + σ_l²)` (config
`[timing.offset_judge] cross_bench_k`, default 5) on every poll of the
advance window.  On failure the judge stays on the lower tier, publishes
`cross_bench_conflict` + per-bench `shadow_residuals` in
`offset_judge.json`, and logs a rate-limited CRITICAL; degrade-on-loss
stays immediate and single-bench sites are unaffected.  This section is
a pointer, not a rewrite — see the gate document for rule, rationale and
implementation notes.
