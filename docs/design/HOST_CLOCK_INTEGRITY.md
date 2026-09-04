# Host-clock integrity: the verdict beside the tier decision

Status: shipped 2026-09-04.  Code: `core/host_clock_integrity.py`, wired
through `core/authority_manager.py`.  Config:
`[timing.authority_manager.host_clock]`.  Companion to
`TIMING_AUTHORITY_TWO_AXIS.md` and METROLOGY §4.5.

## What happened

On 2026-09-04 the host clock on AC0G-B4 ran 11.6 s slow of UTC for
thirteen hours.  The T6 anchor stayed correct the whole time.  WSPR fell
from thirty spots a cycle to one.  `chronyc tracking` reported the clock
within 0.1 ms of its reference.

The reference was FUSE.  FUSE carries `trust` in chrony.conf, and the
product FUSE feeds chrony follows the host clock (`METROLOGY.md`, the
T3 paragraph under §4.5: "T3's product is a correction to the host
clock").  So chrony steered the clock to a source that measured the clock
it was steering, and every NTP server on the station became a
falseticker for disagreeing.  The same walk had taken AC0G-ND twelve
seconds off UTC the day before.  That day the ungoverned ADC took the
blame.  B4's ADC ran governed.  The mechanism lives in `trust` and in the
host-clock-relative product, not in the ADC.

Three measurements on the host saw the fault.  None of them said so.

1. The authority manager computed `T6<->T2:11679.507ms>60.000ms` on
   every tick and tagged it `:advisory`.  The tag states a truth about
   the anchor: a system-clock witness must not demote a GPS-disciplined
   tier, because the difference "reflects system-clock drift, not an
   error in the published anchor offset."  The sentence names the fault
   and then dismisses it.
2. The LB-1421 probe computed the host clock's gap from the GPS integer
   second, found it outside the emission window, set `valid_fix=False`,
   and returned None.  Every reader downstream took None for "no GPS
   fix."  The T5 disambiguation path shut, and with it the learned
   reference gate that lives inside it.
3. gpsdo-monitor timed the GPSDO's PPS edges with the host clock and
   published a median period of 999.91 ms.  Nothing in hf-timestd read
   the field.

## What changed

A drifting host clock now gets its own verdict, separate from the tier
decision.  The tier logic stands exactly as it was.  The anchor was
right both days and the rules that protect it stay.

`host_clock_integrity.assess()` takes the three measurements and returns
one of four verdicts, worst wins:

| verdict | meaning |
|---|---|
| `fault` | a sysclock witness past `fault_ms` (default 1000) from an rtp-frame active tier, or the GPS second outside its emission window |
| `suspect` | a sysclock witness past its own cross-check bound, or the PPS rate past `rate_suspect_ppm` (default 50) |
| `ok` | every witness present agrees with the host clock |
| `unwitnessed` | no witness reported this tick |

The manager collects the inputs each tick:

- **Pairs.**  Inside `_cross_check`, for every `sysclock`-frame witness of
  an `rtp`-frame active tier, it keeps `(|Δ|, bound)` before the
  advisory tag applies and before any demotion.  On ND the asymmetric
  rule demoted T3 to no authority; the 4179 ms number still reaches the
  verdict.
- **GPS second.**  `Lb1421T5Probe` now keeps `host_minus_gps_s` and an
  `invalid_reason` (`fix_stale` or `host_gps_inconsistent`) on every
  reading, and logs a WARNING with the number once a minute while the
  host and GPS disagree.  The recorder publishes both beside `valid_fix`
  in `t5_lbe1421`; `LbeT5DirectProbe` forwards the gap in `detail`
  whether or not T5 comes out available.
- **Rate.**  `GpsdoProbe.host_clock_rate_ppm()` reads
  `pps_study.period_ms_p50` from a fresh device file with at least 30
  edges and returns `(p50 − 1000) / 1000 × 10⁶`.  Positive means the host
  runs fast.  The runner wires it in whenever gpsdo-monitor is enabled.

`authority.json` carries the result as a `host_clock` block on every
normal tick, with a `since_utc` marking the first non-ok tick of the
episode.  The manager logs CRITICAL when a verdict enters suspect or
fault, again on any change between the two, once per `alarm_repeat_sec`
(default one hour) while it holds, and INFO when it clears.  The
`unwitnessed` verdict neither alarms nor clears.

`hf-timestd validate` warns on a non-positive threshold and on a rate
threshold below the study's resolution.  The PPS study times edges with
the OS millisecond over a 60 s window, so it resolves roughly 17 ppm.
Coarse, and the daemon's own note calls it "not a metrology reference."
As a witness to a 90 to 300 ppm walk it suffices.

## What the first deploy taught, 17:09Z the same day

B4 took the code with the rate witness on by default.  The manager
declared SUSPECT within a minute: host rate +83.7 ppm against PPS.  At
that moment the LAN stratum-1 held the host clock within 12 µs and chrony
reported a steady −81 ppm frequency correction.  The LB-1421 gap read
0.82 s, inside its window.  Two good witnesses said ok; the study said
otherwise.

The study had read 999.91 ms per second at 15:58Z, while the clock lost
roughly 180 ppm, and 1000.08 ms at 17:09Z, while the clock sat correct.
Neither figure tracks the disciplined clock, which chrony slews through
`CLOCK_MONOTONIC` and which the study stamps with.  Neither tracks the
raw oscillator, near +81 ppm both times.  The daemon's own docstring
calls the study "a liveness plus gross-stability indicator, not a
metrology reference," and hf-timestd's contract with it says "only to
decide A1/A0, never as a clock correction."  This module read the field
as a rate and broke that contract.

So the rate witness became opt-in the same hour:
`rate_witness_enabled = false` by default, the code kept, the number left
unpublished until someone shows what it measures.  The pair witnesses and
the LB-1421 gap carry the verdict.  Both saw the fault on the day; the
study's agreement was a coincidence of sign.

## What it does not do

It changes no tier, widens no sigma, and steps no clock.  `trust` came
off the FUSE refclock on both stations the same evening (19:17Z ND,
19:19Z B4) and out of the repo template (4617c5f); with a real witness
in the pool again, both clocks stepped back.  The verdict reaches the
log and authority.json.  Acting on it in `sigmond-t6-stuck-watchdog` and
the alert units belongs to sigmond and waits there.

## Step 0.5 — the verdict withdraws FUSE (2026-09-04, evening)

Removing `trust` keeps FUSE from outvoting the pool.  It does not keep
chrony from *selecting* FUSE when the pool is thin or noisy, and on a
station with no LAN stratum-1 the pool loses to a refclock that reports
0.1 ms every cycle.  ND selected FUSE again on merit twenty minutes after
its clock was stepped.  A refclock that follows the clock will always
look better than the servers that measure it.

So the chrony refclock gate (`core/chrony_refclock_gate.py`, METROLOGY
§4.6) now takes the verdict beside the tier.  While the verdict reads
`suspect` or `fault` the gate sets `+noselect` on FUSE whatever the
active tier; chrony keeps measuring the refclock and stops steering by
it.  The gate re-offers FUSE only after `ok` has held for
`host_clock_clear_sec` (default 600 s) — a relapse restarts the count.
`unwitnessed` leaves the gate where it stands.  Config:
`[timing.authority_manager.chrony_gate]`, `withdraw_on_host_clock`
(default true once the gate is enabled) and `host_clock_clear_sec`.

The gate does not step the clock.  It removes the one source that
measures the clock from inside, so chrony can follow the sources that
measure it from outside.  On the day, that would have handed B4 back to
192.168.1.80 within one authority tick of the T2 pair crossing 60 ms,
and ND back to its pool.

Enabling it is a deploy step: `chronyc selectopts` needs the chrony
command socket, which the timestd user lacks on a stock install.  The
first attempt (22:20Z) showed why the installer's `usermod -aG _chrony`
never helped: Debian 13's chrony unit sets `RuntimeDirectoryMode=0700`
on `/run/chrony`, chronyd 4.6.1 makes `chronyd.sock` owner-only, and
chrony 4 takes privileged commands over that socket alone.  As timestd,
chronyc falls back to UDP and reads `501 Not authorised`.  The gate
therefore gained `sudo = true`, which runs
`sudo -n /usr/bin/chronyc selectopts FUSE ±noselect`, and install.sh
ships the two-command grant `config/sudoers-timestd-chrony-gate` to
`/etc/sudoers.d/`.  Nothing else in the fusion unit changes; it carries
no `NoNewPrivileges`, so sudo works from inside it.

## Step 0.5(b) — the ticks are looked for where the signal put them (2026-09-04, evening)

The walk mechanism (bus 18:09Z, `reference_fuse_walk_mechanism`) had a
second half the gate did not touch.  `tick_edge_detector` searched
±`SEARCH_WINDOW_MS` = 20 ms around the sample the *host-clock label*
named for each second.  Past 20 ms of clock error the real tick left the
window, the correlator returned threshold-level junk centred where it
was told to look, and the timing error read ≈ 0.  Fusion admitted what
cleared 10 dB.  The gate stops chrony from acting on that number; this
step stops the number from being wrong.

Michael chose the second of the two options that stood here: anchor the
per-second search on the minute marker.  The 800 ms marker correlation
already runs each minute and lands its own onset sample.  The engine now
hands that onset to the edge detector (`anchor_onset`), and every
second's search window sits at `marker_onset + n × sample_rate` — the
GPSDO ruler carries the seconds; the host clock names only which second
the marker was.  The timing error stays `front_edge − label_expected`, so
a clock that walked 300 ms reads as a 300 ms error instead of a tick
found where the clock said to look.  The synthetic test minute in
`tests/unit/test_tick_edge_anchor.py` shows exactly that: label-anchored,
the walked minute yields junk the detector refuses to vouch for;
marker-anchored, 57 ticks land within 1 ms of +300 ms.

Two rules keep the anchor honest:

- **The ticks confirm the marker.**  An anchored pass stands only when it
  finds ≥ 10 ticks with per-tick scatter σ₁ ≤ 6 ms.  A marker locked on
  a sidelobe (bee1, 2026-05-20: corr −370 ms, edge +15 ms) drags the grid
  between the ticks; the grid then collects threshold-level junk with
  σ₁ ≈ 11 ms and fails confirmation, the label-anchored pass runs as
  before, and the engine's edge-versus-correlator override keeps its
  teeth.  Count alone does not confirm: the first version of this test
  passed a 370 ms-wrong anchor on count and failed on scatter.
- **A label-anchored ensemble must look like ticks before it carries
  timing.**  `TickEdgeDetector.timing_admissible` and
  `MetrologyService.tick_measurement_admissible` refuse a host-label
  ensemble whose σ₁ exceeds 6 ms: that scatter is the window's, not the
  ticks'.  Neither synthetic-measurement path in the engine promotes
  such an ensemble, and the L2 `tick_timing` row publishes no `d_clock`
  for it.  `d_clock_source` now reads `edge_ensemble:minute_marker` or
  `edge_ensemble:host_label`, so a consumer can tell the two apart
  without a schema change.

What this leaves: the marker search itself still centres on the label,
with the adaptive window from `UNIFIED_MEASUREMENT_PATH.md` (3σ of the
physics model, ~±100 ms by default).  A walk past that loses the marker,
and with it the anchor; the minute then runs label-anchored and, under
the scatter rule, publishes no timing.  That is the honest answer for a
host clock a tenth of a second wrong — blind, and saying so — and it is
where the T2 pair witness and the gate take over.

### What withholding the noise ensembles exposed (2026-09-04, 22:25Z)

The promoted noise ensembles had been doing a second job nobody had
assigned them: writing two `L1_metrology` rows every minute on every
channel.  `pipeline-watchdog.sh` used that row as its liveness signal for
the metrology units (`stale > 180 s` → restart).  On AC0G-B4 the marker
correlator itself detects in one to five minutes per hour, so within
forty minutes of the deploy the watchdog was restarting all six metrology
units, fusion and L2-calibration every five minutes; on AC0G-ND, four
units.  Fusion never reached LOCKED between restarts and B4's FUSE
refclock went silent at 22:25Z.  chrony stayed on the LAN stratum-1.

The fix is to the watchdog, not to step 0.5(b): liveness now comes from
what a running service writes every processed minute
(`L2_detection_attempts`, `L1_all_arrivals`), fusion is judged by its
per-cycle status file, and L2-calibration by its systemd watchdog.  The
sparse marker-detection rate on B4 (2–5 % of minutes against 30–50 % on
ND at the same hours) predates this work and is its own question.
