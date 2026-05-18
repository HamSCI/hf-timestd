# TSL3 BPSK-PPS Costas-drift — scoping & critical TODO

**Status:** CRITICAL — **scoped, not started.** Queued to begin **after** the
`docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md` remediation sequence
(remaining `M-H##` and all `P-##` findings) is complete.

**Diagnosed:** 2026-05-18 (bee1, live system, on pre-`metrology-physics-review-remediation`
code — the remediation branch was *not* deployed; nothing in that branch is
implicated).

**Subsystem:** `src/hf_timestd/core/bpsk_pps_calibrator_mf.py` (the BPSK PPS
chain-delay calibrator that feeds chrony refclock **TSL3**). Untouched by the
2026-05 metrology/physics remediation work.

---

## Symptom

TSL3 — normally the precise GPS-disciplined anchor (~tens of ns) — degraded to
a ~1.1 ms bias and chrony stopped governing from it.

`chronyc sources` / `sourcestats` (2026-05-18):

```
#x TSL3   reach 377   poll 0   offset -1119us   StdDev 53ns
^* time   (192.168.1.80, local stratum-1 NTP) — current governing source
```

TSL3 is **locked and still precise** (53 ns jitter, full reachability) but
carries a **stable ~1.1 ms bias**. chrony flags it `x` ("may be in error") and
governs from the LAN NTP server instead. No clock emergency — the system stays
disciplined (RMS offset 0.8 µs) — but the precise anchor is lost.

Injector: Turn Island Systems **TS1** BPSK injector at 84.225 MHz (aliases to
45.375 MHz at the RX-888's 129.6 MS/s sampling) + LeoBodnar **LB-1421** GPSDO.

## Root-cause chain (confirmed)

Evidence: `chronyc` + the calibrator's own debug capture
`/var/lib/timestd/debug/bpsk_mf_capture.npz` (60 s, 2026-05-08).

1. The Costas carrier-recovery phase normally sits stably at ~+0.62 rad.
2. It makes intermittent **large excursions** — the phase swings ~6 rad away
   for ~10–15 s, then recovers. (One excursion in the 60 s capture, t≈15–29 s.)
3. **Not a signal fade** — the matched-filter peak amplitude is unchanged
   (actually higher) through the excursion.
4. During an excursion the MF produces **strong phantom peaks** (`|y|` ~90+, as
   strong as the true peak) at offsets on a regular **~100 ms grid**
   (±100/200/300 ms observed).
5. Edge acceptance (`_detect_and_record_peaks`) keys **only** on the MF peak's
   offset from `_last_edge_rtp`; it has no knowledge of Costas health. A
   phantom within `cascade_tolerance_ms = 3.0` ms **walks `_last_edge_rtp`** to
   the phantom. Once walked, same-offset phantoms fall inside
   `edge_tolerance_samples = 30` (312 µs), get accepted, `pps_consecutive`
   climbs → TSL3 **sustains the biased lock** (the live −1.1 ms).
6. chrony marks TSL3 `x` and governs elsewhere.

**Still open — why the loop excurses.** One 60 s capture (one excursion) can't
show the trigger. Square-and-halve carrier recovery has an inherent π
ambiguity; the loop updates every batch with no quality gate; the ~100 ms
(10 Hz) phantom grid hints at injector-modulation structure or a beat.
Resolving this needs more/longer captures + the system journal.

Live calibrator config (`/etc/hf-timestd/timestd-config.toml`, `[timing.l6_pps]`):
`consecutive_required = 10`, `edge_tolerance_samples = 30`,
`costas_loop_bw_hz = 1.0`, `cascade_tolerance_ms = 3.0`.

## Proposed fix — two layers

### Layer A — make TSL3 robust to excursions  (tractable, high value — DO FIRST)

A Costas excursion must never corrupt TSL3; at worst it should cause a brief
holdover.

1. Add a **`costas_locked` quality signal**: phase within a band of its own
   EMA, and per-batch `delta` small / non-erratic.
2. **Gate edge acceptance on it.** While `costas_locked` is False: do not
   accept edges, do not walk `_last_edge_rtp`; keep emitting the last-good
   `chain_delay_ns` (coast), exactly as for a leap-second hold. Resume on
   re-lock.
3. Result: an excursion → TSL3 holds its precise last-good delay instead of
   re-locking biased; chrony keeps TSL3.
4. Backstop: tighten `cascade_tolerance_ms` (3.0 ms is wide enough to admit a
   ~1 ms phantom).

Effort: moderate, contained to `bpsk_pps_calibrator_mf.py` — a lock-quality
metric plus gating in the edge-acceptance path; testable against the existing
debug-capture machinery. Comparable to one metrology-remediation increment.

### Layer B — eliminate the excursions  (root cause, less certain)

Investigate *why* the loop wanders: quality-gate the loop **update** (freeze
`phase` when `|sq_mean|` is low or `delta` erratic, rather than random-walking);
resolve the π ambiguity; revisit the loop bandwidth; identify the 100 ms
structure (injector modulation? a beat?). Needs more data — longer captures,
the periodic Costas-phase log already wired via `phase_log_period_batches`, and
the journal. May not have a clean software fix; the injector hardware could be
implicated.

## Recommendation

Implement **Layer A** — it restores TSL3 reliability (excursion → brief
holdover, not a biased lock) regardless of why the loop excurses, and makes the
precise anchor safe to rely on again. Pursue **Layer B** as a follow-on
investigation with more captures.

## Related / separate items

- **core-recorder memory:** `timestd-core-recorder` was at 5.9 G / 6.0 G cgroup
  cap (~40 M free) — near OOM. The BPSK calibrator runs inside that service.
  Worth its own investigation.
- **Journal access:** confirming today's exact degradation timeline needs the
  system journal (`sudo journalctl -u timestd-core-recorder`), which the
  diagnosing account could not read.

## Diagnostic appendix — debug-capture analysis

`bpsk_mf_capture.npz`, 96 kHz, 60 s, 3287 batches, 2026-05-08 01:01–01:02 UTC:

- Costas phase: stable ~+0.62 rad except the excursion (swept to −5.66 rad).
- Accepted MF peaks: 46. Stable runs at within-second position **343.804 ms**
  (the normal chain delay); one accepted **phantom at 644.389 ms** (+300 ms)
  during the excursion (t=25.6 s, phase −4.27).
- 11 s gap (t=14.6–25.6 s) with no accepted peak — lock lost through the
  excursion.
- `rej_offset` phantom peaks clustered at ±100/200/300 ms, −487 ms; `|y|` 86–95
  (as strong as real peaks).
- MF amplitude median 51 stable vs 49 in the excursion window — no fade.
