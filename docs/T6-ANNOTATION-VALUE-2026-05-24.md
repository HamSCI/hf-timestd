# T6 Annotation-Value Evaluation — 2026-05-24

Substrate-only assessment of whether T6 (TS-1 BPSK PPS injection,
decoded via the RX-888 ADC) demonstrably improves our RTP→UTC
annotation versus the next-best tier we would otherwise use.

Per [[project_rtp_substrate_architecture]] the question is NOT
"how does chrony rank T6 as a refclock" — it is "is the per-sample
RTP→UTC label more honest when T6 is the active tier."

## TL;DR

| Metric (T6 active) | Value | T3 baseline (alternative) |
|---|---|---|
| Median substrate residual | **5 µs**  | σ ≈ 3.8 ms |
| p90 substrate residual    | **2 ms**  | σ ≈ 3.8 ms |
| p99 substrate residual    | **294 ms** | σ ≈ 3.8 ms |
| Worst residual            | 476 ms | — |
| Published t6_sigma_ms     | 1 µs (floor) | 3.8 ms |

**Verdict — qualified yes.** Across the last 7 days T6 was the
active tier 80 % of cycles. At the median T6's annotation is ~760×
tighter than the T3 (HF Fusion) fallback. **However** the published
σ of 1 µs is honest only for the median; it under-represents the
p99 tail by ~5 orders of magnitude. The win is real but the σ
publication does not communicate the long-tail failure mode that
the substrate exhibits.

## Dataset

`/var/lib/timestd/authority_history.db`, written by Layer 4 of the
Pattern B V1 wiring (`AuthoritySnapshotStore`,
[[project_hf_timestd_wall_clock_wiring]]).

Window: last 7 days (last cycle 2026-05-24T17:06:48Z). 22,141
rows, ~one cycle / 30 s.

`t_level_active` distribution:

| Tier | Cycles | % |
|------|--------|---|
| T6   | 17,612 | 79.5 % |
| T4   |  4,341 | 19.6 % |
| T3   |     89 |  0.4 % |
| (blank/bootstrap) | 101 | 0.5 % |

## Why `t6_local_minus_source_ns` is the right substrate metric

`core_recorder_v2.py` line 2784–2792 computes, at each PPS edge:

```
raw_wall_time_sec     = rtp_to_wallclock(last_edge_rtp, _t6_channel_info)
wall_time_sec         = raw_wall_time_sec − effective_chain_delay
ref_time              = round(wall_time_sec)
local_minus_source_ns = (wall_time_sec − ref_time) × 1e9
```

`ref_time` is the integer second nearest our projected estimate;
the BPSK source's "true PPS UTC" by construction lives at the
integer second (GPS-locked). So `local − source` is exactly
"(our RTP-projected UTC) − (truth)" at each PPS edge — what we
care about as the annotation honesty metric.

(Caveat: the `round()` aliases beyond ±500 ms; data confirms the
ceiling — max observed |residual| is 476 ms. Residuals larger than
500 ms cannot be distinguished from small residuals via this
metric and would need disambig context.)

`t6_offset_ms` is the same field, scaled to ms. `BpskPpsProbe`
forwards it as `ProbeResult.offset_ms` (bpsk_pps_probe.py:224).

## Distribution when T6 is the active tier (17,612 cycles)

`|t6_local_minus_source_ns|` buckets:

| Bucket | Cycles | % | Notes |
|---|---:|---:|---|
| < 1 µs   | 1,375 |  7.8 % | genuinely sub-quantization |
| < 10 µs  | 8,055 | 45.7 % | inside half-quantization step (31 µs) |
| < 100 µs |   577 |  3.3 % | |
| < 1 ms   | 4,149 | 23.6 % | marginal |
| < 10 ms  | 3,019 | 17.1 % | σ-misrepresented |
| < 100 ms |   105 |  0.6 % | catastrophic |
| ≥ 100 ms |   332 |  1.9 % | catastrophic (anchor-staleness regime) |

Percentiles: median 4.8 µs, p90 1.95 ms, p99 294 ms, max 476 ms.

53 % of cycles land within 10 µs (better than the σ-floor of 1 µs
would suggest after accounting for the conservative floor). 19 %
land at ≥ 1 ms — i.e. between 1000× and 500000× the published σ.

## Recovery machinery is firing — but T6 stays "active" through it

Cross-tab of substrate residual vs. drift-monitor flags:

| Bucket | n | n_disc | n_breach | avg_breach_s |
|---|---:|---:|---:|---:|
| < 10 µs  (good)         | 9,430 |  11 |     0 |   — |
| < 1 ms   (marginal)     | 4,726 |  51 |     0 |   — |
| < 100 ms (bad)          | 3,124 |  47 | 1,353 |  102 |
| ≥ 100 ms (catastrophic) |   332 |   1 |   240 |  563 |

The Layer 2 breach detector IS catching the bad/catastrophic
regimes (1593 / 3456 = 46 % of "bad+catastrophic" cycles flagged
`sustained_breach=1`). But the cycle is still labeled
`t_level_active='T6'` with `t6_sigma_ms=0.001` during the breach
— i.e. the authority publication remains T6 with the optimistic σ
while the substrate is provably off by ≥ 1 ms.

Recapture reason histogram (T6-active cycles):

| Reason | Cycles |
|---|---:|
| `<null>` | 9,896 |
| `anchor_discontinuity` | 4,745 |
| `sustained_breach` | 2,971 |

So ~44 % of T6-active cycles carry an active recapture history
(half-life of recapture context is bounded by
`t6_last_recapture_age_sec`; many of these are minutes after the
event, not concurrent).

## T6 vs T3 comparison

| Regime | T6 residual | T3 σ | Ratio (T6 better by) |
|---|---:|---:|---|
| Median | 5 µs  | 3.8 ms | 760× |
| p90    | 2 ms  | 3.8 ms | 1.9× |
| p99    | 294 ms | 3.8 ms | 0.013× (T3 better) |

T3 (HF Fusion) σ is roughly stable at single-digit ms because
multi-station consensus floors the noise but doesn't have a
catastrophic anchor-staleness failure mode in the same way.

So **T6 dominates T3 in the bulk of normal operation and is
overshadowed by T3 only in the worst 1 % of cycles** — corresponding
to the V1 anchor-staleness failure mode already documented in
[[project_continuous_status_listener_2026-05-23]] and
[[project_hf_pps_anchor_drift_definitive_fix]].

## What this evaluation does NOT cover

- **The chrony-facade story.** Chrony's selection algorithm,
  `system_clock − reference_time`, and `chronyc tracking` outputs
  are out of scope per [[project_rtp_substrate_architecture]] —
  this note answers only the substrate question.
- **T5 substrate comparison.** `authority_snapshot` has no T5
  columns today (gap noted in the prep memo for this session). If
  T5 (LBE-1421 USB-NMEA, µs-class via USB jitter) becomes a
  routine fallback or cross-check, parity columns should be
  added.
- **Per-event recovery latency.** A "median time-to-recovery from
  breach" would tell us how long the catastrophic regime
  persists. The data supports it
  (`t6_last_recapture_age_sec` is in the schema); not computed
  here.

## Implications for downstream consumers

1. **Science pipeline.** At the median, T6's RTP→UTC label is
   substantially more honest than any non-T6 tier. For
   ionospheric work that integrates over windows ≫ p99 recovery
   time, T6 is clearly the better label. Consumers that need
   per-sample certainty would benefit from a tier-aware filter
   that rejects samples where `sustained_breach=1` AND active
   tier is T6.
2. **Honesty of the published σ.** `t6_sigma_ms` is the BPSK
   matched-filter jitter, not the dominant error source. A more
   honest published σ for downstream consumers would be
   `max(jitter, |local_minus_source_ns|/1e6)` — sized to the
   actual residual, not just the measurement jitter floor. That
   would let consumers see when annotation is bad without
   parsing the breach flags. Consider this as a Layer 5
   addition to authority publication.

   **Update 2026-05-31 — superseded by the native-anchor refactor.**
   The dominant error source identified above (anchor staleness
   driving `|local_minus_source_ns|` to the p99 of 294 ms / max of
   476 ms) was the result of riding ka9q's host-clock-derived
   `(gps_time, rtp_timesnap)` anchor through `rtp_to_wallclock` on
   every PPS edge. The native-anchor refactor freezes the anchor at
   first lock and never re-reads ka9q's anchor on the science path,
   eliminating the staleness mechanism. Under the new design
   `local_minus_source_ns` becomes the matched filter's *own*
   per-edge measurement jitter — which is the MF σ floor (~ns-class
   in steady state). The `max(jitter, residual)` honesty addition
   is therefore no longer needed: jitter and residual are the same
   number. See `hf_timestd.core.native_anchor`,
   `docs/TIMING-PIPELINE-WIRING.md` §5.4, and
   `[[project_native_anchor_2026-05-31]]`.
3. **`authority.json` §18.4 gap.** The legacy scalar-offset form
   loses the per-sample residual signal that's actually
   diagnostic. The §18.4 anchor-pair + rate form
   ([[project_authority_json_v18_gap]]) would let consumers
   carry the residual + sigma per anchor pair and avoid trusting
   a single moment's σ.

## Closure on the question

> "How does T6 demonstrably improve our annotation of the RTP
> timestamps?"

It improves the median substrate residual by ~760× (5 µs vs the
~3.8 ms T3 fallback) over 80 % of operating cycles. It improves
p90 by ~2×. It does NOT improve p99 — there T6 is bested by T3
by ~80× because of an unresolved anchor-staleness failure mode
that affects ~2 % of cycles, persisting for ~9 minutes average
breach duration.

The improvement is real and load-bearing for science consumers
that operate at the median. The published σ does not reflect the
long tail; consumers that need worst-case bounds should
additionally consult `t6_sustained_breach` and
`t6_breach_duration_sec` in the authority record.

## Followups (deferred, not scoped here)

- Add T5 substrate columns (`t5_available`, `t5_offset_ms`,
  `t5_sigma_ms`) to `authority_snapshot`.
- Promote `t6_sustained_breach=1` cycles to T5 fallback rather
  than continuing to publish T6 with optimistic σ. (Mechanical
  change in `authority_manager._select_active_tier`.)
- Replace `t6_sigma_ms` floor with
  `max(jitter, |local_minus_source_ns|/1e6)` so the published σ
  reflects the actual annotation honesty per cycle, not just the
  jitter floor.
- Compute time-to-recovery from breach over the 7-day window for
  a more operationally-useful metric.

## Related memory

- [[project_rtp_substrate_architecture]] — the framing this
  evaluation lives inside.
- [[project_next_session_t6_annotation_value]] — the prep memo
  for this session.
- [[project_continuous_status_listener_2026-05-23]] — the
  anchor-staleness mechanism behind the p99 tail.
- [[project_hf_timestd_wall_clock_wiring]] — Layer 4 created the
  dataset queried here.
- [[project_authority_json_v18_gap]] — publication form
  limitations referenced in §"Implications".
