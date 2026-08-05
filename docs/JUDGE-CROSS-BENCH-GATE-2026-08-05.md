# Proposal: cross-bench consistency gate for Offset-Judge tier advancement

Date: 2026-08-05 · Status: proposed spec amendment to
`OFFSET-JUDGE-SPEC-2026-08-05.md` §2 (judgement rule) · Motivating incident:
`T6-DISPLACED-PEAK-62MS-2026-08-05.md`.

## The gap

§2's cascade rule is "highest available tier wins, with hysteresis (N
consecutive good polls to advance)". "Good" today means *the bench produces
verdicts* — there is no check that the candidate tier's UTC **agrees with the
tier below it**. On 2026-08-05 the first live T6 bench carried a ~12 ms
reconstruction bias (displaced MF peak, hf-timestd#7 family); T5 (LBE-1421,
GPS-direct) was healthy and disagreed, but the cascade promoted T6 anyway and
its biased offset was applied to labels — with the bench's honest-but-wide
initial σ (25 ms) ensuring no k·σ violation ever fired. A *more precise* tier
made the labels *less accurate*, silently. That inverts the whole point of
the taxonomy.

## Proposed rule

A bench may only be **adopted** as the judge when, in addition to the
existing hysteresis:

```
|UTC_candidate(rtp) − UTC_incumbent_or_lower(rtp)| ≤ k_x · sqrt(σ_c² + σ_l²)
```

sustained over the same N-poll advance window, where the comparison reference
is the highest *already-trusted* lower tier (k_x default 5, config
`[timing.offset_judge] cross_bench_k`). Concretely: T6 must agree with T5
when T5 is live (else with T4, etc.). On failure:

- do **not** advance; keep judging on the lower tier;
- CRITICAL log + `offset_judge.json` flag naming both tiers and the
  disagreement (`cross_bench_conflict: {upper, lower, delta_ns}`), rendered
  as ✗ by `smd status`;
- keep measuring the rejected bench and publish its residual vs the adopted
  bench continuously — the disagreement trend is itself a first-class
  diagnostic (tonight it would have measured the displaced peak directly).

Degrade-on-loss behavior is unchanged (immediate). The gate applies only to
*advancement*, so a site with a single bench (T4-only) is unaffected.

## Why not just tighten initial σ?

An honest wide initial σ is correct for a cold bench — the failure was not
the σ but that *precision claims substituted for cross-validation*. A biased
bench can converge to a small σ around the wrong center (T6's stable +62 ms
falseticker shows exactly that stability); only comparison against an
independent reference catches it. The taxonomy already stacks independent
references — the gate just makes the cascade use them before handing one the
gavel.

## Implementation sketch

`OffsetJudge._select_bench()` currently ranks available benches by tier. Add:
candidate ≠ incumbent ⇒ compute the candidate-vs-reference delta from the
same poll's verdicts (both benches evaluate the same rtp counter — no new
plumbing); track a per-candidate advance window; gate the swap. New tests:
biased-upper-bench rejection, conflict flag publication, single-bench
unaffected, recovery when the bias clears (window resets).

## Relation to AC0G decisions (spec §13)

Consistent with #1 (bounds are empirical — the gate uses the benches' own
live σ) and #2 (diverse redundancy as first-class defense); extends the same
philosophy from recorder-side dt-guards to the judge's own bench selection.
