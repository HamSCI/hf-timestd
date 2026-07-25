# T6 initial-accept sidelobe lock-in — B4, 2026-07-24

Status: guard applied (this commit); underlying questions OPEN for mjh.

## Incident

B4 (AC0G-B4, VM100) cold-booted at 22:59 UTC after a RAM resize. Timeline of
the T6 (TS-1 BPSK @ 45.375 MHz) chain on that boot and one clean restart:

1. **Boot #1 (22:59):** core-recorder started during the chrony boot race.
   The T6 settled-capture gate (|Last offset| ≤ 100 µs × 3) timed out after
   60 s and proceeded degraded. At first MF lock, disambiguation walked the
   tier hierarchy: T5 not wired (see below), T4 refused (chronyc tracking
   sigma 1.825 ms > 0.010 ms gate), T3 unusable → "no usable non-T6 timing
   authority; accepting calibrator value as-is" → **raw 708 473 423 ns
   accepted, persisted, and locked**. Known-good effective chain_delay on
   this hardware is ≈105.7 ms.
2. **Restart (23:08, chrony locked to FUSE at −35.7 µs, gate passed 3/3):**
   the persisted 708.5 ms was correctly refused by the persistence-load
   plausibility guard ("previous session captured into a sidelobe") and fell
   through to fresh disambig — which again had no usable reference (T4 sigma
   4.407 ms) and accepted the fresh raw measurement: **708 473 423 ns again,
   identical to the nanosecond.**
3. Net effect: T6 stayed out of the authority cascade (T3-only,
   `t_level_available=[T3]`), HPPS reach pinned at 0 in chrony. The
   native-anchor gate on the SHM push (anchor is only captured on a
   *successful* disambig) correctly kept the wrong value away from chrony —
   but nothing retried, nothing alarmed beyond two INFO/WARNING lines at
   startup, and the wrong value sat in the persistence store.

## What this commit changes

The disambiguation paths (`_t6_disambiguate_via_t5_lb1421`,
`_t6_disambiguate_via_external_reference`) and the persistence load already
carried the Layer B ±250 ms physical-plausibility guard. The one unguarded
entry point was **initial accept itself** — the `ref is None` fall-through
accepted (and persisted) the raw MF value with shift 0.

Now, at MF initial accept, if `|raw + disambig| > 250 ms`:

- the lock is **refused**: `_t6_last_chain_delay_ns` stays `None`, nothing
  is persisted, no anchor is captured, nothing reaches archive metadata or
  the HPPS SHM;
- the cycle returns early, so **every subsequent locked cycle re-enters
  initial-accept and re-walks the tier hierarchy** — the retry loop the old
  code lacked. References improve as chrony settles, so a T4 pass later
  resolves the wrap without operator action;
- a WARNING is logged on the 1st and every 60th consecutive refusal
  (ALARM LOUD, rate-limited).

The HFPS (diff sidecar) ref-None initial accept had the same hole and got
the same guard (refuse + retry on next accepted edge instead of accepting
raw).

## Open questions for mjh

1. **Deterministic sidelobe at +708.5 ms.** Two independent acquisitions
   (different processes, ~9 min apart) produced the identical raw
   chain_delay to the nanosecond. That is not noise capture — the MF is
   reproducibly preferring this correlation peak at this operating point
   (peak_running ≈ 102.8, costas locked, SNR ≈ 39.6 dB). Δ from the
   known-good value is ≈ +602.73 ms. Is this the boxcar-template ±0.5 s
   sidelobe compounded with something else, a template-period wrap, or a
   real feature of the current TS-1 waveform? The bee1 Layer B analysis
   (2026-05-31) saw sidelobe clusters at 200/466/808/955 ms — 708.5 is not
   in that set.
2. **T4 sigma gate (10 µs) is unreachable in practice.** chronyc tracking
   rms on a freshly-booted host is milliseconds, and even in steady state
   with the FUSE refclock (±600 µs error bound) it never approaches 10 µs.
   That means the T4 fallback effectively never fires on a host without
   long NTP soak time. Should the gate be derived from the wrap distance
   actually being disambiguated (e.g. sigma ≪ template-period/2 — the
   comment at the persisted-value block already argues 250 ms), rather
   than a fixed 10 µs?
3. **T5 was not wired on B4.** `[timing] lb1421_enabled` is not set in
   B4's config, so the LB-1421 probe (which reads gpsdo-monitor's
   /run/gpsdo JSON — running and healthy on B4, A1/3D-fix) was never
   attached, and the one reference that would have disambiguated instantly
   was skipped. Should `lb1421_enabled` default to on when /run/gpsdo has
   a fresh index? (B4 will likely enable it explicitly meanwhile.)
4. **Refusal-loop escalation.** With the guard, a host that can never get
   a reference (no T5, T4 gated) refuses forever — honest, but HPPS stays
   down. Options: escalate to a calibrator reset()/re-hunt every N
   refusals (may just re-find the same deterministic peak — see Q1), or
   surface the refusal count in status.json/authority.json so watchdogs
   and `smd watch` can see it.
5. **Startup ordering.** The settled-capture gate timing out at boot (60 s)
   while chrony needs ~2 min to first lock on a cold boot guarantees the
   degraded path runs on every cold boot. Consider gating T6 first-capture
   on chrony sync (with a much longer timeout) rather than proceeding
   degraded, now that refusal (this commit) makes waiting safe.
6. Housekeeping: B4's config still uses the deprecated `[timing.l6_pps]`
   key (accepted with a warning); the fleet should migrate to
   `[timing.t6_pps]`.

## Reproduction / verification data

- B4 journal, boot of 2026-07-24 22:59 UTC (`journalctl -b -u
  timestd-core-recorder`): both accept events, both plausibility-guard
  refusals, and the T4 sigma values quoted above.
- Persistence store: /var/lib/timestd/state/ (chain-delay store held
  effective=708473423 at sr=96000 between the two accepts).

## Update 2026-07-24 ~00:25Z next day: T5 enabled — displacement is NOT an integer second

B4 set `[timing] lb1421_enabled = true` and restarted. Outcome:

- T5 probe attached, and at first MF lock disambiguation **succeeded**:
  raw 708 473 423 ns → effective chain_delay **41 699 886 ns** (41.7 ms),
  native anchor captured tier=T5. The initial-accept guard passed it
  (41.7 ms is plausible), HPPS SHM pushes started, chrony reach went to
  377 with LastRx 0.
- **chrony marks HPPS falseticker (`#x`) at a constant +557 ms offset
  with ±55 µs error bound** — a microsecond-stable, wrong signal.

Interpretation: the MF's captured peak is displaced from the true PPS
edge by a **non-integer-second** amount (~0.6 s). Integer-second
disambiguation (T5/T4/T3 — all of it) can therefore never rescue this
capture: T5 aligned the displaced edge to the nearest GPS second
correctly, and the sub-second part of the displacement passes straight
through into the pushed reference times. This reclassifies the defect:

- It is **not** a wrap/disambiguation problem (Q2/Q3 above are still
  real robustness issues, but fixing them cannot fix this capture).
- It is an **MF peak-acquisition problem**: the filter deterministically
  prefers a displaced correlation peak (raw identical to within tens of
  ns across four independent acquisitions on two processes) at an
  operating point with healthy SNR (39.6 dB, costas locked,
  peak_running ≈ 102.8). Pre-reboot sessions on identical hardware
  measured ≈105.7 ms, so the true peak exists and used to win.
- Q1 (sidelobe geometry) is now the primary question, and Q4's
  escalation idea (periodic re-hunt) would not converge here — the
  same peak wins every hunt. The fix likely needs peak-candidate
  enumeration with a plausibility prior, or template work.

Operational side effects on B4 as of this update: chrony's falseticker
exclusion keeps the wrong time from disciplining anything (correct);
HPPS LastRx is fresh so the hpps-watchdog stops bouncing core-recorder;
authority remains T3-only. B4 left in this state deliberately — the
falseticker offset is itself the best live measurement of the peak
displacement for whoever picks up Q1.
