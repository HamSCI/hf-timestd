# The judge learns each station's T6 chain delay

**Status:** design, 2026-10-27.  Implements sigmond `tasks/release-v383.md`
row 1, which carries the ruling Michael made on 2026-10-23 (formerly v3.74
row 18).  Not built.

## Why

T6 reads the TS-1's pulse after it has passed through the whole receive
chain: the RX888, USB, radiod's FFT and filters, and the scheduler.  That
pass takes time.  So T6's reading of UTC sits a fixed distance from the
lower tiers (T4, T3), which read the host clock.  The distance belongs to the
station.  It does not mean either clock has gone wrong.  The documents
already name it the label-plane offset, or Λ.

The offset judge gates T6 against T4 with a sub-millisecond bound.  It reads
the 16 ms distance as a disagreement.  On AC0G-B4 it logs a CRITICAL
cross-bench conflict once a minute (91 so far on 2026-10-27), refuses T6, and
labels data from T4.  The T6 anchor authority publishes T6 regardless.  So
B4's raw chunks carry `judge_tier = T4` labels while authority runs on T6.

AC0G-ND shows the other half of the defect.  Its judge currently holds T6,
with T4 as a shadow at +15.8 ms and no conflict.  The cross-bench gate checks
only an upgrade.  So whichever tier a judge happened to hold when it started
decides whether it ever reaches T6.  Two stations with the same chain do two
different things.

### The ruling (Michael, 2026-10-23)

> The judge learns each station's T6-vs-lower-tier residual as its measured
> chain delay (stored, with sigma), adopts T6 while the residual stays within
> a few sigma of it, alarms LOUDLY on a change (re-measure and confirm, never
> absorb), and still refuses T6 outright outside a coarse gross-error bound
> (chain-delay range + lower tier's uncertainty).  GRAPE then moves T4 -> T6
> labels (~16 ms step on B4/ND): announce it and mark the moment in the data.

## What the code already has, and why it fails

The judge already carries a learner for this distance: `LabelPlaneTracker`
(`core/label_plane.py`), wired in through `_observe_label_plane_locked` and
`effective_label_plane_offset_ns`.  Each tick it pairs the T6 reading (label
plane) with the tightest host-plane reading.  It keeps a 600 s rolling median
of the difference and subtracts that median inside `_cross_bench_delta_ns`.
The gate and the shadow residuals both go through that one function.

The learner measures the right number and then throws it away.  On
2026-10-27 the judges published:

| station | median | spread (pstdev) | sigma | verdict |
|---|---|---|---|---|
| B4 | −18.0 ms | 319 ms | 127 ms | refused, sigma > 5 ms |
| ND | −16.1 ms | 52.8 ms | 21.1 ms | refused, sigma > 5 ms |

The median holds steady.  The spread comes from `statistics.pstdev`, which a
handful of wild readings wreck.  One −298 ms reading among 63 pushes the
spread past 300 ms.  The sigma then fails the 5 ms bound, and the judge falls
back to the configured offset of 0.

The judge's own conflict lines show what the quantity really does.  783 B4
readings of T6 − T4, from 2026-10-26 12:00Z to 2026-10-27 01:40Z:

- Hourly medians run from −15.6 to −17.1 ms.  The hour of the hub swap
  (00Z) reads −18.9.
- The robust spread (1.4826 × MAD) runs 0.5 to 1.7 ms per hour, and 1.11 ms
  across the whole run.  The median across the run reads −16.35 ms.
- Six readings fall more than 5 ms from their hour's median (+4.4, −23.6,
  −25.9, −297.8 and two others).

So the station has a chain delay of about −16.4 ms that wanders by about a
millisecond, plus rare gross outliers.  A robust estimator learns it within
an hour.

The tracker has a second mismatch with the ruling.  It follows the quantity:
after a real change, its window refills and the new value takes over within
ten minutes.  The ruling says a change must alarm and wait for confirmation,
never be absorbed.

## Goals

1. Each station learns its chain delay from its own T6 and lower-tier
   readings, with an honest sigma and spread.
2. The judge stores the learned value, and the value survives a restart.
3. The judge adopts T6 while the live residual stays within a few sigma of
   the stored value.  This holds both on an upgrade and on a cold start, so
   B4 and ND behave alike.
4. A residual that leaves that band raises a loud, sustained alarm.  The
   judge then stops trusting T6 and keeps the stored value until someone
   confirms a new one.
5. Any T6 reading outside a coarse gross-error bound gets refused outright,
   whether or not the judge has learned anything yet.
6. When the judge's adopted tier moves from T4 to T6, GRAPE's labels move by
   about 16 ms.  The data marks that moment, and we announce it.

## Non-goals

- **No correction of labels.**  The learned value only sets what the judge
  expects when it compares two benches.  Nothing adds it to a timestamp, the
  chrony feed, or the T6 anchor.  This keeps the doctrine in
  `ARCHITECTURE-FIRST-PRINCIPLES.md` and `STATION_SETUP_GUIDE.md`: a measured
  chain delay never gets applied as a correction.
- No change to the T6 anchor authority, the battery, or the fine stage.
- No change to `anchor_closure` (off on B4).  The label-plane anchor path
  stays as it stands.
- The coarse calibrator's re-homing (below) gets its own release row.  This
  design only keeps it from poisoning the learned value.

## Anti-goals: it fails if

- the gate turns vacuous: a T6 step of a few milliseconds slips through
  because the expected offset moved to meet it;
- a restart quietly relearns a changed chain delay, and the change passes
  without an alarm;
- the judge learns from a T6 that was re-homing, degraded, or acquiring;
- the alarm floods the journal (one CRITICAL a minute for hours, which
  buries everything else).

## Design

### 1. Robust statistics in the tracker

`LabelPlaneTracker.estimate()` takes its spread from the scaled median
absolute deviation (1.4826 × MAD), not `pstdev`.  Before it takes the median,
it drops observations more than a fixed outlier cut from the window's median
(default 5 ms, a config key).  The estimate reports how many it dropped.
Everything else stays: the standard error of the median from n_eff, the
host-bias floor, and the refusal while n < min_n.

On B4's day of data this gives a spread near 1.1 ms and a sigma well under
the 5 ms bound.

The tracker splits by tier.  Today `_observe_label_plane_locked` feeds every
label-plane reading into one tracker, so T3's registration bench
(`HfAcquiredBench`, also `plane="label"`) mixes into T6's number.  T3 runs
through a different pipeline and stands alone (ruling, 2026-10-22).  So the
judge keeps one tracker per label-plane tier.  The T6 tracker feeds this
design.  The T3 tracker keeps today's role for T3 pairs (my call).

With `chain_delay_learn` on, the live T6 tracker no longer sets the gate's
plane term for T6.  It serves only as the learner and the witness.  The gate
uses the stored record (§4), or the configured value while no record exists.
A live median follows changes, and the anti-goals forbid that (my call).

### 2. The stored chain delay

The judge keeps a calibration record in
`/var/lib/timestd/state/t6_chain_delay.json`, owned by the timestd user and
written atomically (temp file and rename):

```
{ "schema": 1,
  "offset_ns": -16350000, "sigma_ns": 140000, "spread_ns": 1110000,
  "n": 720, "n_eff": 120, "span_s": 7200,
  "learned_from_utc": "...", "learned_to_utc": "...",
  "lower_tier": "T4",
  "state": "seeded" | "confirmed",
  "candidate": { ...same fields, or null... },
  "identity": { "t6_frequency_hz": 45375000, "t6_sample_rate": 96000,
                "low_edge_hz": -25000, "high_edge_hz": 25000 },
  "history": [ ... previous records, newest last, at most 10 ... ] }
```

The identity block names the parts of the chain that config can change.  A
record whose identity no longer matches the running config gets set aside
into `history`, with one WARNING, and learning starts again.  Hardware
changes (a new hub, a new cable) leave no trace in config.  The change alarm
in §5 catches those.

### 3. Learning

With no confirmed record, the judge learns:

- It feeds the T6 learner only on ticks where T6 has been AUTHORITATIVE for
  the last 10 minutes, the fine stage reports an estimate, the coarse
  calibrator has not re-homed in that time, and the host-plane partner passes
  the tracker's host-sigma gate.  The judge learns these facts through the
  bench detail: `NativeAnchorBench` adds the authority state, and the coarse
  calibrator gains a re-home counter with the time of the last re-home.  The
  calibrator today only logs its re-homes.  Row 7 builds on the same counter
  (my call).
- The learner keeps its own window of `chain_delay_learn_s`, separate from
  the 600 s live witness.  Its spread sets the record's `spread_ns`.  A
  re-home within the last 10 minutes restarts the learner (and any candidate
  learner), so a learn never spans one.  The judge also refuses to write a
  learn whose spread exceeds `chain_delay_max_spread_ns`: a step part-way
  through a learn would otherwise widen the band until the gate and the
  alarm barely act.
- Once the observations span `chain_delay_learn_s` (default 7200 s, so
  load-driven wander over a couple of hours shows up in the spread) and the
  estimate's sigma passes the 5 ms bound, the judge writes the record as
  `confirmed`.  It logs one WARNING with the value, sigma and spread.
- During learning, T6 stays a shadow bench and the cross-bench gate keeps
  today's behaviour.  The CRITICAL conflict line drops to one at the start,
  plus an hourly reminder that names the learning progress.

The first learn needs no human confirmation.  It measures what the station
has, and nothing earlier exists to compare it with.

**Seed to start, then learn (Michael, 2026-10-27).**  At deploy each station
gets a seeded record, so the judge has a band from its first tick:

    hf-timestd chain-delay seed --offset-ms -16.35 --sigma-ms 1.1 --spread-ms 1.1

A seed carries `state: "seeded"`.  The judge adopts T6 against a seed exactly
as against a confirmed record, and the change alarm and the held-T6 check
work against it the same way.  The learner runs from the start regardless.
When the first full learn completes, its value replaces the seed and the
record becomes `confirmed`, with no `accept` needed: a seed comes from a
rougher measurement than the learn.  If the learned value lies outside the
seed's band, the judge logs a WARNING naming both values, since a seed that
far off says the seeding went wrong.  From then on, every change needs
`accept`.

Seed values for the deploy: B4 at −16.35 ms, spread 1.11 ms (783 judge
readings, 2026-10-26 12:00Z to 2026-10-27 01:40Z).  ND from its own judge's
shadow residual and conflict history, taken at deploy time.

### 4. Adoption against the stored value

With a stored record, seeded or confirmed, `_cross_bench_delta_ns` subtracts the stored
`offset_ns` for a label-versus-host pair, instead of the tracker's live
median or the config value.  The gate then reads:

    |delta − stored| ≤ cross_bench_k × sqrt(σ_c² + σ_l² + σ_stored² + spread²)

The spread term admits the millisecond of wander the station really has.
The per-tier sigma ceilings stay as they are.  With B4's numbers the bound
comes out near 5 × 1.5 ms ≈ 7.5 ms, against a residual that sits within about
1 ms of −16.4.

The same subtraction applies on the bootstrap path
(`_chain_consistent_locked`) and the upgrade path.  So a judge that starts
cold, as on B4, and one that started on T6, as on ND, both end on T6 when the
residual fits.

The shadow residuals go through the same function, so they report the
departure from the stored chain delay rather than the raw 16 ms.

### 5. The change alarm

The tracker keeps running after the record exists, as a live witness.  Each
tick the judge compares the live estimate with the stored record:

    |live − stored| > cross_bench_k × sqrt(σ_live² + σ_stored² + spread²)

When that holds for `chain_delay_change_sustain_s` (default 600 s), the judge:

- logs CRITICAL "T6 CHAIN DELAY CHANGED: stored X ms, now Y ms ± Z", once,
  then once an hour while the change stands;
- publishes `chain_delay.changed = {stored, live, since_utc}` in
  `offset_judge.json`, where the station web and fleet checks can see it;
- drops T6 and degrades to the lower tier (see "Holding T6" below);
- starts a fresh learner under the §3 rules.  After `chain_delay_learn_s` it
  writes its estimate into the record as a `candidate` beside the confirmed
  value, without replacing it.

The judge never promotes a candidate on its own.  An operator confirms it:

    hf-timestd chain-delay show
    hf-timestd chain-delay accept     # candidate becomes confirmed
    hf-timestd chain-delay relearn    # discard both, learn afresh

`accept` refuses a candidate whose span falls short of `chain_delay_learn_s`
or whose sigma fails the bound.  Each confirmation moves the old record into
`history`.

A brief excursion (one bad minute, a −298 ms outlier) never reaches the
sustain time.  The outlier cut in §1 keeps it out of the live estimate too.

**One bound, not two.**  The alarm and the adoption gate use the same bound,
`cross_bench_k × sqrt(σ_c² + σ_l² + σ_stored² + spread²)`.  Two different
bounds would leave a band where the alarm fires and the gate still admits T6.

**Holding T6.**  Today the judge runs the gate only on an upgrade, at
bootstrap, or when the held tier goes silent.  A judge already holding T6
never checks it again, which explains ND.  So this design adds one new path.
While the judge holds T6 and a stored record (seeded or confirmed) exists, every tick checks the
held T6 against the band and the gross bound.  A departure that lasts
`sustain_window_s` (60 s today) drops T6 to the best lower tier, and the
drop counts as a degrade.  This narrows the rule that a degrade is never
gated.  The rule keeps its meaning for a tier that goes silent.

**The latch.**  Once the change alarm fires, T6 stays dropped while the live
residual sits outside the band.  If the residual returns inside the band and
stays there for `chain_delay_change_sustain_s`, the alarm clears, a WARNING
says so, and T6 may come back through the ordinary upgrade gate.  Nothing
changed in that case, so nothing needs confirming.  A residual that settles
somewhere new never comes back on its own: only `accept` of the candidate
moves the band (my call, read from the ruling's "re-measure and confirm,
never absorb").

### 6. The gross-error backstop

Before any other test, the judge refuses a T6 reading when

    |delta_raw| > chain_delay_max_ns + cross_bench_k × σ_l

where `delta_raw` means T6 − lower tier with no plane term, `σ_l` takes the
lower tier's capped sigma, and `chain_delay_max_ns` defaults to 40 ms (B4 and
ND sit at 16; 40 leaves room for a slower host without admitting a wrong
second or a 100 ms sidelobe).  This holds with or without a learned record,
and it bounds what learning may store: the judge drops any observation past
this bound before it reaches the tracker.

### 7. The GRAPE label step

With `anchor_closure` off, `BinaryArchiveWriter._label_correction_s` labels
each chunk from the judge's verdict for the adopted bench.  When B4's judge
first adopts T6, its labels move by about 16 ms.

The data already marks the moment.  Every chunk sidecar carries
`timing.judge_tier`, so the first chunk with `T6` after a run of `T4` shows
where the step falls.  The design adds two things:

- the sidecar's `timing` block gains `chain_delay_ns` and
  `chain_delay_state` from the record in force, so a reader can see the size
  of the plane difference without the journal;
- the judge appends one line per adopted-tier change to
  `tier_transitions.jsonl`, in the directory of `chain_delay_path`
  (`/var/lib/timestd/state/` by default), whether or not learning is on
  (`{utc, from, to, reason, delta_ns, residual_ns, chain_delay_ns}`; `from`
  is null and `reason` reads `startup` on the first adoption after the judge
  starts, and `reason` reads `change` on every later line), a durable log a
  GRAPE reader
  can consult without walking the sidecars.

Deploying this to B4 will produce the step.  We announce it on the bus and to
the GRAPE users first, give the planned time, and record the actual time from
`tier_transitions.jsonl` afterwards.  ND took its step at some earlier,
unrecorded moment.  Its sidecars show when.

### 8. Config keys (`[timing.offset_judge]`)

| key | default | meaning |
|---|---|---|
| `chain_delay_learn` | true | turn the whole mechanism on |
| `chain_delay_learn_s` | 7200 | span a learn needs |
| `chain_delay_outlier_ns` | 5e6 | outlier cut around the window median |
| `chain_delay_change_sustain_s` | 600 | how long a departure must hold before it alarms |
| `chain_delay_max_ns` | 4e7 | the gross-error bound |
| `chain_delay_max_spread_ns` | 2.5e6 | the widest spread a learn or candidate may write; a wider one most likely straddled a step, and the judge refuses it (one WARNING per refusal streak) and keeps learning. `accept` refuses a candidate this wide too |
| `chain_delay_path` | `/var/lib/timestd/state/t6_chain_delay.json` | record |

With `chain_delay_learn = false` the judge behaves as it does today.

## Related defect, filed separately

B4's coarse T6 calibrator (`bpsk_pps_calibrator_mf.py`) re-homed its lock six
times between 00:30Z and 01:20Z on 2026-10-27, by −5.7, −11.4, +197.4, −5.1,
+42.9 and −413.3 ms.  Each time it logged "a genuine RF/DSP chain-delay
change" and carried on.  No chain delay moves by 413 ms.  This code absorbs a
change, against the "expose, never correct" rule and against this ruling.  It
goes on the v3.83 list as its own row.  Here, a re-home only blocks learning
for ten minutes (§3).

On 2026-10-26, before the StarTech hub went in, B4's T6 authority also
cycled AUTHORITATIVE → DEGRADED (edge_period) about six times an hour all
day.  Since the swap it has not degraded once.

## Tests

New file `tests/test_offset_judge_chain_delay.py`, built on the
`FakeClock` / `reading()` / `make_judge()` pattern of
`tests/test_offset_judge_plane_correction.py`:

- robust spread: a window of −16 ms ± 1 ms with one −298 ms reading gives a
  spread under 2 ms and an accepted estimate (fails today);
- learn: two hours of healthy pairs write a confirmed record; a re-home or a
  DEGRADED tick in the last ten minutes blocks a tick's observation;
- persistence: a new judge on the same path adopts T6 at bootstrap with no
  learning;
- seed: a seeded record adopts at once; the first full learn replaces it
  without `accept`; a learn outside the seed band logs a WARNING;
- adoption: a residual at stored ± 3 ms adopts; at stored ± 15 ms refuses;
- change: a sustained 6 ms shift alarms after the sustain time, degrades,
  writes a candidate, and leaves the confirmed value alone; a single-minute
  excursion does not alarm;
- gross bound: a −300 ms reading gets refused with or without a record, and
  never enters the tracker;
- identity: a changed `t6_pps.frequency_hz` sets the record aside and
  relearns;
- the existing `test_a_t6_step_is_not_absorbed_immediately` keeps passing;
- sidecar: `chain_delay_ns` and `chain_delay_state` appear in the timing
  block; `tier_transitions.jsonl` gains one line per change;
- CLI: `accept` refuses a short or loose candidate.

## Left to the builder

Names of private methods, where the record I/O lives (a small
`ChainDelayStore` class in its own file, by this repo's one-class-per-file
convention), the exact log wording, and how `accept` reaches a running
judge.  Writing the file and letting the judge re-read it on its next tick
will do.
