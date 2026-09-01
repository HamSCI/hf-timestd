# Timing admission: three keys, and no other requirement

Design, 2026-09-01. Michael and mjh's Claude instance.

## 1 · The problem, stated from evidence

The station publishes timing measurements it cannot stand behind, and it does so
by construction rather than by accident.

`BroadcastCalibration` documents `hardware_offset_ms` as a constant
receiver-chain delay — matched-filter group delay around 0.4 ms, ADC and buffer
alignment, detection threshold bias. On 2026-09-01 B4 held these, all converged
on roughly 430,000 samples each:

    WWVH_5.00   +16.584      WWV_20.00    -0.066
    WWV_10.00   -16.627      WWV_25.00    -0.230
    WWV_2.50     +7.429      WWVH_10.00   -0.058

One receiver chain cannot delay WWV 10 MHz by −16.6 ms and WWVH 5 MHz by
+16.6 ms. The three entries near zero — 20 MHz, 25 MHz, WWVH_10 — mark the real
hardware scale, and they are exactly the paths where the propagation model
behaved. The rest absorbed model error under a label that says hardware.

The mechanism sits upstream. The deployed discriminator assigns stations by
ORDER — early peak, late peak — so it **must** emit a pair and cannot say
"neither". When WWVH is not arriving, that rule takes real WWV energy, or a
noise peak, and calls it WWVH. The arrival gate reports this in every line it
writes: `assigned=['WWV','WWVH']` beside `present=['WWV']`.

⚡ **The forced pair has no upside to give up.** Over 6461 gated ensembles
(2026-08-31 17:00Z → 09-01 12:15Z) the gate reported FEWER stations 2776 times
and MORE **zero** times. Not once did forcing a pair recover a station the
geometry had missed. It only ever added one that was not there.

⚡ **First off-station replay (Task 6, 2026-09-01).** `scripts/replay_admission.py`
ran read-only against a 3M-row local copy of the archive (57.9 h,
`minute_boundary_utc` 1788076440–1788284880), one run per shared channel, at
the PROVISIONAL defaults (`floor_snr_db=10.0`, `tolerance_ms=1.0`,
`lookback=10`, `reacquire_after=3`). Full verbatim output:
`.superpowers/sdd/2026-09-01-admission-replay-harness/task-6-report.md`.

| channel | minutes | ADMITTED | INCONSISTENT | BELOW_FLOOR | DEGRADED | NOT_ELIGIBLE | deployed_over_reports | deployed_under_reports |
|---|---|---|---|---|---|---|---|---|
| SHARED_2500  | 3379 | 3132 (30.9%) | 4434 (43.7%) | 1126 (11.1%) | 885 (8.7%) | 560 (5.5%) | 3226 | 20 |
| SHARED_5000  | 3380 | 4219 (41.6%) | 4525 (44.6%) | 325 (3.2%) | 511 (5.0%) | 560 (5.5%) | 3232 | 1 |
| SHARED_10000 | 3380 | 5467 (53.9%) | 3618 (35.7%) | 188 (1.9%) | 307 (3.0%) | 560 (5.5%) | 3017 | 0 |
| SHARED_15000 | 3381 | 3670 (36.2%) | 4620 (45.5%) | 1176 (11.6%) | 117 (1.2%) | 560 (5.5%) | 2951 | 413 |

Read this beside the 2776-fewer / 0-more figures immediately above: that count
is over 6461 gated ensembles and measures the forced pair against geometry
alone; `deployed_over_reports` / `deployed_under_reports` here is over ~3380
replayed minutes per channel and measures the three-key cascade against the
currently deployed model. Different statistics, placed together because both
bound how often forcing a label adds or misses a station.

Three things this table would mislead a reader on if left bare:

(a) ⚠ These runs predate the station-coordinate unification (commit 763c928,
`coordinates: resolve through the catalogue, here too`) and so reflect the
PRE-unification geometry — WWV at 1122.486 km. Re-running now would shift WWV
by −1.64 µs and BPM by +1.02 µs. Treat this table as the harness's first
output, not a settled result; it needs re-running once the geometry fix is
picked up.

(b) INCONSISTENT firing 35–45% of the time is the harness doing exactly what
§5 says it should: thresholds are OUTPUTS of validation, not inputs, and this
is the first one the replay has told us is wrong. Real HF propagation wanders
several ms diurnally; the PROVISIONAL 1.0 ms history-consistency tolerance is
evidently too tight for that. Read as a finding to act on when a tolerance is
finally chosen, not as a defect in the cascade or the harness.

(c) SHARED_15000's 413 `deployed_under_reports` is an outlier against 20 / 1 /
0 elsewhere. MEASURED CORRELATION, explicitly not proof: on the deployed
pipeline, BPM has no row at all in 2355 of SHARED_15000's 3381 minutes
(present in only 1026/3381), while it is present in essentially every minute
on 5 and 10 MHz (3380/3380). 413/2355 = 17.5% of BPM-absent minutes on 15 MHz
see the cascade admit BPM anyway, against 20/845 = 2.4% on 2.5 MHz —
consistent with 15 MHz being a materially better path to Pucheng (11,528 km)
than 2.5 MHz. Confirming this needs a per-station breakdown that `summarise()`
does not currently produce; not built as part of this task.

⛔ **WWV and WWVH transmit continuously.** Absence in an expectation window
therefore never means a station stopped broadcasting. It means the signal did
not get through as expected — always a statement about the path, never about
the source. Only BPM has real schedule gaps (UT1 minutes, per-frequency).

## 2 · The rule

    ADMITTED ⟺ above the noise floor
             ∧ inside exactly one geometric window
             ∧ consistent with history

    anything else → emit nothing

Three keys, all must turn. **No other requirement, no fallback branch, no
confidence score, no weight.** Abstention is the correct output, not a
degraded form of measurement. A system that must emit a label whether or not
evidence supports one cannot be repaired by tuning its thresholds.

This makes the design smaller than the one it replaces.

### Key 1 — above the noise floor

`MultiStationToneDetector._estimate_robust_noise_floor` already estimates noise
well: median absolute deviation, samples taken OUTSIDE the search region so
signal cannot inflate its own noise estimate, the 1.4826 MAD-to-sigma
conversion. Keep the estimator. Change what we do with its output.

It returns `median + 3σ`. That serves as a detection aid and fails as an
admission test, because we do not test one sample — we take the peak over a
window. 3σ one-sided gives P_fa = 1.35e-3 **per sample**; a window at 24 kHz
holds 144 samples at ±3 ms and 240 at ±5 ms:

    ±3 ms window   144 × 1.35e-3  ≈  19 % false alarm per window
    ±5 ms window   240 × 1.35e-3  ≈  32 %

Set the floor from a **per-window** false-alarm probability instead. One policy
number, P_fa_window (proposed 1e-3), and the threshold follows from it and the
trial count:

    P_fa_sample = P_fa_window / N
    threshold   = median + z(P_fa_sample) · σ_MAD

    N = 144 → ≈ 4.34σ        N = 240 → ≈ 4.46σ

**This is why narrowing a window improves sensitivity.** A narrower window
lowers N, which lowers the sigma needed for the same confidence. Window width,
floor and sensitivity form one coupled system rather than three knobs, and the
width already derives from the Offset Judge's reference sigma through
`arrival_windows`.

⚠ Matched-filter output samples are NOT independent — they correlate over the
template length — so effective N is lower than raw sample count. Assuming
independence sets the floor slightly too high, which errs toward abstention and
therefore errs safely. Ship the conservative form; **measure** effective N from
raw IQ rather than assuming it (§5).

### Key 2 — inside exactly one geometric window

`StationArrivalGate` already builds windows and partitions arrivals. It needs no
new geometry, only the authority to return "none" and to be believed.

"Exactly one" carries the weight. Zero windows means the path delivered and our
model missed it. More than one means we cannot say whose signal it is — the
conf=0.50 case, which today resolves to a coin flip and emits a label.

### Key 3 — consistent with history

An arrival can clear the floor and land in a window and still be a sidelobe or a
mis-assignment. Propagation delay moves smoothly; mode changes step by known
amounts. The recent track constrains what is plausible. This follows the
principle T6 already uses — estimate from the PPS history rather than each
pulse, because the GPSDO pins the slope.

⛔ **Key 3 must be falsifiable, or it becomes the stale-lock bug.** Admit only
what matches history and the system can lock onto a wrong track and then refuse
the evidence that would correct it — precisely the August T6 −26 ms excursion, a
stale coarse anchor plus a livelocked fine stage. So: a LONE outlier is
rejected; **N consecutive arrivals that agree with each other while disagreeing
with history force re-acquisition** rather than perpetual rejection.

Key 3 gates TIMING admission only. Physics wants the outliers — a mode change is
the observable.

## 3 · Verdicts

### Per station-minute — a cascade, evaluated in order, exactly one applies

| # | test fails → | state | means | timing |
|---|---|---|---|---|
| 1 | not a candidate | `NOT_ELIGIBLE` | we did not look (BPM only) | — |
| 2 | nothing above floor | `BELOW_FLOOR` | path delivered nothing detectable | — |
| 3 | in **no** window | `OFF_MODEL` | path delivered; our model is wrong | — |
| 3 | in **>1** window | `AMBIGUOUS` | cannot say whose | — |
| 4 | history rejects | `INCONSISTENT` | lone outlier against the track | — |
| 5 | quality fails | `DEGRADED` | signal present, unusable | — |
| 6 | — | `ADMITTED` | measurement + uncertainty | ✔ |

One state of seven produces a value. The states are not a policy; they record
**which key failed**, which is what physics wants anyway.

`NOT_ELIGIBLE` versus `BELOW_FLOOR` matters: the first says nothing about the
ionosphere, the second is ionospheric data. Conflating them would poison a
propagation study with minutes when BPM was simply off the air.

`OFF_MODEL` versus `BELOW_FLOOR` is what the floor buys. Today they are
indistinguishable, and they call for opposite corrections.

### Per channel-minute — all three stations can be absent at once

| state | means |
|---|---|
| `CHANNEL_SILENT` | nothing above floor at all — band closed |
| `CHANNEL_UNIDENTIFIED` | energy above floor, no window claims it |
| `CHANNEL_PARTIAL(n)` | n stations admitted |

`CHANNEL_UNIDENTIFIED` is the state that protects us. Absent stations plus
present energy — interference, a spur, another emitter — is exactly the
configuration the forced pair converted into a false WWVH measurement. Named,
it is attributed to none of the three.

⚡ **`CHANNEL_SILENT` on all six channels at once is not the ionosphere — it is
the receiver.** On 2026-09-01 five of six channels went silent for 70 minutes
while chrony reported steadily IMPROVING RMS offset (19.7 → 4.2 µs), because a
collapsed ensemble has nothing left to disagree with itself about. core-recorder
was logging `silent for infs` and nothing turned it into a signal fusion could
act on. This state is the natural input to an ensemble-width alarm.

### Per channel — derived path state

Hysteresis lives HERE, not in the per-minute verdict. The verdict stays a crisp
measurement with no memory; the path state (open/closed, with timestamps and the
floor in force) applies N-consecutive-minutes hysteresis. The measurement never
lies to stabilise itself, and the stable thing is explicitly derived.

## 4 · Record

`L2_detection_attempts` already carries `detected` and `rejection_reason`, and
already rejects most attempts:

    detected=0  correlation_flat   139,965   46 %
    detected=0  corr_snr_low        85,835   28 %
    detected=1  (null)              76,617   25 %

⚡ **The detector already says no three times in four, and the wrong something
is produced anyway.** The leak sits downstream, in attribution. The floor still
matters — it supplies the spurious candidate that the forced pair then labels —
but key 2 carries most of the damage.

Three additions:

1. **The floor, per attempt** — threshold value, `P_fa_window`, `N`. Today
   `detected=0` means "we saw nothing"; it must mean "we saw nothing above X".
   That difference turns a non-detection into a measurement with stated
   sensitivity, which is what makes it usable as ionospheric evidence.
2. **Discipline `rejection_reason` into the cascade vocabulary.** The two
   existing values are both `BELOW_FLOOR` flavours. The new states are the ones
   nothing can currently express — which is why the failure stayed invisible:
   there was no word for "I detected energy but cannot say whose."
3. **A per-minute verdict row** keyed (channel, station, minute), plus the
   channel-minute state. `L2_detection_attempts` is per second; the cascade
   resolves per minute.

Physics reads all three layers. Timing reads only `ADMITTED`.

## 5 · Validation — retrospective first, off-station

The archive validates most of this without touching B4, which matters while the
station sits unattended until late September.

`L1_all_arrivals` holds 26.7M rows carrying `arrival_ms` (the measurement,
model-independent) and `model_expected_ms` (the prediction actually used).
Verified inverting on live rows: arrival 59056.21 ms, model 41.77,
`timing_error_ms` 14.5, and 56.21 − 41.77 = 14.44.

| what | how | coverage |
|---|---|---|
| Key 2 geometry | recompute windows from the FIXED predictor at each past timestamp, replay the cascade | full archive |
| Key 3 history | function of the arrival sequence alone | full archive |
| Key 3 escape hatch | replay against REAL excursions on file — the T6 −26 ms event, the 6h23m stuck state 2026-08-31 03:38–10:01Z | full archive |
| Key 1 floor, relative | threshold in existing `corr_snr_db` units | full archive |
| Key 1 floor, absolute + effective N | recompute correlations at several P_fa from raw IQ | ⏳ ~36 h only |
| censored set | `L2_detection_attempts` records rejections WITH their SNR | 302k attempts |
| `BELOW_FLOOR` / `CHANNEL_SILENT` verdicts | GRAPE DRF **carrier amplitude** — an INDEPENDENT witness of path openness | full archive |

⚡ **The GRAPE archive is an independent check, and a strong one.**
`drf_properties.h5` reports 10 Hz complex, 6 subchannels, hourly files, daily
directories — the PSWS Doppler product, kept locally back to 2026-08-24 and
long-term on PSWS. At 10 Hz it CANNOT calibrate the tick floor: the ticks are
~5 ms bursts of 1000 Hz tone, and 10 Hz cannot represent a 1000 Hz tone. But its
carrier AMPLITUDE tracks whether a path is open, on a completely separate signal
path from the tick detector. A `BELOW_FLOOR` verdict should coincide with
carrier collapse in GRAPE. That is a second instrument agreeing over months,
which beats the same detector agreeing with itself over one night.

⛔ **The archive has a bandwidth gap.** 10 Hz kept forever, 24 kHz kept 36 hours,
nothing between. `/var/lib/timestd/drf` sits EMPTY — the full-rate DRF writer is
retired in `archive/legacy-drf-core/`. Retaining even a few minutes per hour at
24 kHz would make this whole class of question answerable retrospectively for
good, instead of depending on a rolling buffer. Worth doing regardless of this
design.

⛔ **Censoring caveat.** The archive records what the old detector kept. We can
test what the new rule would have REJECTED from what was admitted; we cannot
directly test what it would have ADMITTED from what was discarded.
`L2_detection_attempts` lifts this partly, since it stores the rejections and
their SNR.

✅ **PRESERVED 2026-09-01.** `raw_buffer` retention runs off an 80% disk quota
(not a day count), which was yielding about two days. A full diurnal cycle is
now held off-station at `/home/mjh/hamsci/iq-preserve-20260901/`: 6 channels ×
24 hourly 5-minute blocks, 7.0 GB, `complex64` at 24 kHz, every sidecar
reporting `completeness_pct 100.0` and `gap_count 0`, all 144 blocks verified
with `zstd -t`. The one perishable input to this design is no longer
perishable.

⚠ **The resource guardian under-counts ingest by ~3.5×.** It preflights
`4.0 GB/ch/day`; measured reality is ~14–15 GB/ch/day (~86 GB/day), because
`complex64` at 24 kHz is 16.6 GB/ch/day uncompressed and float IQ compresses
only ~11%. Its "2.7 days headroom — OK" is therefore badly optimistic on a disk
at 75% against an 80% quota. Separate defect; the chaos drills already found the
board blind to disk-full, and this is the arithmetic behind it.

Retrospective replay does the discovery. Raw IQ calibrates the floor. A short
prospective run confirms — it no longer needs to carry a full diurnal
validation.

⚠ **The numeric policy values are OUTPUTS of this validation, not inputs.**
This design deliberately fixes none of them. `P_fa_window` (1e-3 above reads as
a starting proposal), the history-consistency tolerance and its lookback, the N
for re-acquisition, and the N for path-state hysteresis all get their values
from steps 1 and 2. Choosing them before the replay would be guessing, and a
guessed threshold is how the 3σ floor came to admit noise a fifth of the time.

**Copy a SUBSET of `raw_buffer`, not all 129 GB.** Floor calibration needs
enough noise diversity to measure effective N and validate P_fa — a few hours
per channel spanning day and night conditions, not the whole rolling window.

## 6 · Staging

Nothing here changes timing behaviour until the replay has spoken.

1. **Off-station replay harness.** Read-only against a copy of `timestd.db`.
   Reports how often each state fires, per channel and UTC hour, and what
   admission would have changed. Zero station risk.
2. **Floor calibration** from the copied raw IQ: effective N, and the P_fa that
   holds false admissions at the target.
3. **Publish verdicts, consume nothing.** Deploy the cascade measurement-only,
   the way the arrival gate runs today. Compare live verdicts against replay.
4. **Retire the forced pair.** The order-based discriminator loses its authority
   to label; its output becomes a diagnostic compared against the cascade.
5. **Wire consumers.** The calibrator and fusion admit only `ADMITTED`. This is
   the only step that reaches the host clock, and it goes through the canonical
   update path (`hamsci-ops/docs/canonical-update-path.md`) with per-channel
   verification, never a chrony reading alone.

Expect the learned `hardware_offset_ms` values to collapse toward the 0.23 ms
scale that 20 MHz, 25 MHz and WWVH_10 already show. That convergence is the
acceptance test for step 5.

**Step 1 is a plan on its own.** The replay harness reads a copy of the archive,
runs entirely off-station, changes nothing, and produces the numbers every later
step depends on. It should be planned and built before steps 2–5 are planned at
all — what the replay finds may well reshape them.

## 7 · What this does not claim

It does not measure the analogue chain. The client contract asks hf-timestd to
calibrate one and hf-timestd does not; `delay_budget_ns` keeps the site term
declared-undeclared until the antenna-to-TS-1 measurement happens.

It does not improve the timing offset on day one. Fusion currently holds FUSE at
tens of microseconds BECAUSE the learned offsets cancel the model error. What
this buys is separation — instrument delay and model error stop sharing one
number — plus robustness against a diurnal error a constant cannot cancel, and
transferability to a second station that would otherwise learn its own set of
bogus offsets.

Related: [[reference_shared_vs_wwv_channels]], `docs/design/TIMING_PROVENANCE_MODEL.md`,
`hamsci-ops/docs/canonical-update-path.md`.
