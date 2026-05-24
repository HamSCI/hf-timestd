# HF-PPS → chrony: how the parameters got tuned, and why

**Audience:** time nuts. Assumes familiarity with NTP / chrony, GPSDOs,
matched filters, and Costas loops.

**Status:** living document. The current parameters work but the BPSK
signal regime on the test bench (bee1) is more turbulent than the
original tuning anticipated, so this is the kind of file that gets
updated when the regime changes again.

> **Scope of this document**: this is a tuning guide for one specific
> consumer of the hf-timestd annotation stream — the chrony SHM
> refclock feed for the T6 BPSK-PPS tier (and the parallel HFPS feed
> via the diff calibrator).  It is NOT the architectural description
> of the system.
>
> For the architectural framing — RTP sample counter as the substrate,
> Tn annotations on top, chrony as one downstream consumer of those
> annotations — see
> [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md).
> For the metrological story see
> [METROLOGY.md](METROLOGY.md).  For the SHM-facing conventions and
> wiring see [TIMING-PIPELINE-WIRING.md](TIMING-PIPELINE-WIRING.md).
>
> The tuning constants below exist to make the chrony-facing facade
> work cleanly.  They affect chrony's view of the source, not the
> underlying annotation quality.  Treat this document as a downstream
> operational guide, not a design center.

---

## 1. What we are doing

The "HF-PPS" timing tier is the LB-1421 GPSDO's 1 PPS pulse modulated
onto a BPSK carrier (Turn Island Systems TS1), broadcast at 45.375 MHz,
captured by an RX-888 SDR, demodulated in software, and finally
delivered to chrony via the SysV SHM refclock interface as `TSL3`.

The interesting bit is that we never see the PPS edge directly. The
RX-888 + radiod give us an IQ stream at 16 kHz. The PPS edge is encoded
as a phase flip in the BPSK carrier. We must:

1. demodulate the BPSK,
2. localise the polarity-flip edge to sub-sample precision,
3. convert sample-index → wall-clock UTC,
4. write that UTC + the local clock reading to chrony's SHM,
5. trust chrony to discipline the system clock from there.

The first three steps are done inside `BpskPpsCalibratorMF`
(matched-filter calibrator). Step 4 is `ChronySHM.update()`. Step 5 is
chrony's own refclock driver.

This document focuses on the *parameters* that decide when we accept
or reject an edge, and what we publish about its quality.

---

## 2. The pipeline parameters

```
  RX-888 IQ samples (16 kHz, ka9q-python RadiodStream)
        │
        ▼  resequence_buffer_size = 256 packets
  RTPResequencer  ──▶ drop / zero-fill on missing seq
        │
        ▼  
  BpskPpsCalibratorMF.process_samples()
   ├─ matched-filter peak search
   │     STEP_CONFIRM_EDGES        = 60        # persistence for chain-delay step
   │     T6_STEP_RECOVERY_WINDOW   = 60
   │     T6_STEP_RECOVERY_TIGHT_NS = 1_000_000
   ├─ Costas carrier-recovery loop
   │     COSTAS_TAU_PHASE_EMA_S    = 10.0
   │     COSTAS_TAU_DPHASE_EMA_S   = 0.5
   │     COSTAS_DPHASE_MAX_RAD     = 0.050  *** retuned ***
   │     COSTAS_PHASE_BAND_RAD     = 2.0    *** retuned ***
   │     COSTAS_RELOCK_S           = 0.5
   ├─ chain-delay wrap rejector
   │     WRAP_THRESHOLD_NS         = 10_000_000  (10 ms)
   └─ T6 stuck-recovery watchdog
         T6_STUCK_TIMEOUT_SEC      = 60.0

        │   per accepted PPS edge: (rtp_timestamp, chain_delay)
        ▼
  core_recorder_v2._t6_on_samples → ChronySHM.update(unit=2)
   └─ writes  receive_time = wall_time(edge) - chain_delay
              reference_time = round(wall_time)
              precision = -14   (61 µs declared at the SHM layer)

        │   ~ 1 sample/s
        ▼  /etc/chrony/chrony.conf
  refclock SHM 2 refid TSL3 poll 0 precision 5e-6 offset 0.0 delay 0.0001
```

Two more parameters live in the publication path:

```
  BpskPpsProbe          # the timestd Authority probe
    sigma_floor_ms = 0.001
    + observed std of chain_delay_ns over a 60-sample window
    = published t6_sigma_ms (clamped from below by floor)
```

---

## 3. Why each parameter has the value it has

### 3.1 RTP resequencer: `resequence_buffer_size = 256`

The resequencer waits for missing RTP sequence numbers up to a soft
deadline: when its buffer reaches half-full without the next-expected
sequence arriving, it declares the missing packet lost and zero-fills.

Default 128 packets → half-fill at 64 packets → at T6's 80 pkt/s rate
that's a **0.8 s patience window**.

**Why it had to change:** under transient CPU contention (e.g. when
the archive workers ramp zstd compression at a 10-minute file boundary),
T6's reader thread can fall behind for ~1 s. With the old 128-packet
buffer, the resequencer over-eagerly declares packets lost, zero-fills
~480 ms, and the matched filter sees a *storm* of off-position
"phantom" edges. The phantom storm walks the Costas loop into a real
excursion downstream. None of this corresponds to actual packet loss
(per-socket `sk_drops = 0` throughout).

**New value 256:** half-fill at 128 packets = **1.6 s patience**.
Twice the worst observed scheduler stall, no impact on steady-state
latency.

### 3.2 Chain-delay step detection: `STEP_CONFIRM_EDGES = 60`

After the matched filter is acquired, an "off-position" edge (one that
doesn't agree with the locked-in chain delay) is treated as a *phantom*
and held inert. This is what prevents a transient noise burst from
walking the lock.

But what if the chain delay *really* changed (a cable, a radiod filter
reconfig, a TS1 retuning)? `STEP_CONFIRM_EDGES = 60` is the threshold:
60 consecutive off-position edges all agreeing on a new position is
treated as a genuine step. At 1 PPS that's ~60 s — comfortably longer
than the worst observed Costas excursion (~10-15 s), so an excursion
can never be mistaken for a step.

Step adoption *is* expected behaviour, not a fault. When it fires
TSL3 will briefly stop pushing while the lock re-homes.

### 3.3 Costas dphase threshold: `COSTAS_DPHASE_MAX_RAD = 0.050`

This is the "loop in motion" gate. The Costas carrier-recovery loop
tracks the BPSK carrier's residual phase φ. We maintain `dphase_ema`
— a slow EMA of |Δφ| — and call the loop "in motion" if it exceeds
the threshold.

**History of the threshold** (this is the journey):

| Pass | Value | Trigger to retune                                      |
|------|-------|--------------------------------------------------------|
| 1    | 0.004 | Original. Set against a known excursion at 0.012-0.015 |
| 2    | 0.008 | Steady-state had crept into 0.005-0.009 (false trips)  |
| 3    | 0.020 | Steady-state climbed to 0.008-0.009 (false trips)      |
| 4    | 0.030 | Peak 0.02023 reached within minutes of deploy          |
| 5    | 0.050 | Peak 0.0309 reached, multiple band trips at 1.02 rad   |

The point of the table is to show that the BPSK signal regime here
*is not stationary*. The original tuning assumed `dphase_ema ≪ 1e-3`
in steady state, with excursions sitting at 0.012-0.015. On bee1
2026-05-21 the steady-state runs an order of magnitude higher.

The cheap explanation: the carrier-recovery loop's "natural" jitter
depends on BPSK SNR and the loop's bandwidth. Either the SNR has
degraded (RF environment), or the loop tuning is wrong for the
current SNR. Both deserve study; in the meantime the threshold has
to live above the observed noise floor.

**Why this is OK:** the dphase test has a partner — the band test
(§3.4) — that catches *real* carrier excursions on its own. We've
verified empirically that during every observed "false positive"
dphase trip, the chain_delay stayed stable to ±100 ns. The
*measurement* was fine; only the *meta-signal* (dphase) was elevated.
So bumping dphase up doesn't degrade timing quality, it just stops
the gate from spuriously rejecting good edges.

If even 0.050 trips repeatedly in the future, the right next step
is to remove the dphase test entirely and rely on band alone.

### 3.4 Costas band threshold: `COSTAS_PHASE_BAND_RAD = 2.0`

The band test gates on `|φ − φ_EMA|`, where `_phase_ema` is a slow
EMA of φ that is *frozen* whenever the motion test fails (so it
can't follow φ into an excursion and silently re-validate a wandered
phase).

**Why 2.0:** real carrier excursions per the original
characterisation swing |φ| > 5 rad — at least 2.5× the new threshold,
so we still catch them reliably. The "false positive" band trips
observed were at |φ − φ_EMA| = 1.02 rad, just over the previous 1.0
threshold. 2.0 admits the regime; real excursions still trip.

Like the dphase trips, the chain_delay stayed stable across these
band trips. We are confident that 2.0 is too low to mask real
excursions and too high to fire on normal-regime jitter.

### 3.5 Wrap rejector: `WRAP_THRESHOLD_NS = 10_000_000` (10 ms)

After Costas accepts an edge, the wrap rejector applies one more
sanity check: if the new chain_delay differs from the last-accepted
value by more than 10 ms, reject this edge and keep coasting.

The 10 ms threshold is well above natural sample-quantization wobble
(62.5 µs at 16 kHz) and well above legitimate multi-sample drift in
the calibrator's chosen edge position (~2-5 ms typical over hours),
but well below the half-second wrap value (~322 ms) the algorithm
produces when a noise edge displaces the reference. So 10 ms cleanly
separates "normal drift" from "wrap-displaced".

### 3.6 chrony.conf TSL3 line: `precision 5e-6  offset 0.0  delay 0.0001`

```
refclock SHM 2 refid TSL3 poll 0 precision 5e-6 offset 0.0 delay 0.0001
```

`precision` is what we *declare* to chrony as the per-sample
uncertainty. It's not measured; it's a fixed claim chrony uses as
a floor on its confidence interval.

- `precision 5e-6` = 50 µs declared. Combined with `delay 0.0001`
  (100 µs round-trip delay claim — irrelevant for an SHM segment in
  the same host, but chrony wants a positive value), this gives the
  `+/- 55us` displayed in `chronyc sources`.

We tried tightening this to `precision 1e-6` (1 µs) to make the
display more honest. **Don't do that.** The runtime jitter of TSL3
in `chronyc sourcestats` typically sits at ~150-300 ns std-dev — but
individual samples often exceed 1 µs offset (3-7 µs is normal during
brief drift periods). If the *declared* precision is tighter than
the *observed* sample-to-sample spread, chrony marks the source
unusable. We saw this immediately: TSL3 went `?` reach=255 (chrony
was receiving samples fine but rejecting all of them).

The 50 µs declared precision gives chrony plenty of margin to
accept everything that's within the BPSK quantization band (31 µs
half-sample at 16 kHz) plus some atmospheric drift, without
under-claiming relative to the true short-term jitter.

**Reverted to original `precision 5e-6 / delay 0.0001`.**

If you want a more honest `±` display, the right way is *not* to
shrink the precision claim — it's to read `chronyc sourcestats TSL3`
which is genuinely dynamic and reflects observed quality.

### 3.7 Authority publication: `BpskPpsProbe(sigma_floor_ms=0.001)`

This is separate from chrony's accounting — it's what timestd's
Authority Manager publishes about TSL3 to its own cross-source
cascade.

Old behavior: hardcoded `sigma_ms = 0.050` (50 µs) — the same number
as the chrony precision claim. Useless as a quality signal because
it never moved.

New behavior: the producer maintains a 60-sample rolling deque of
`chain_delay_ns` (the matched-filter's actual edge-position estimate
per PPS — the physical measurement of interest) and publishes its
std-dev. The probe converts to ms and clamps below by `sigma_floor_ms`.

`sigma_floor_ms = 0.001` (1 µs) is the lower bound. We can't honestly
claim better than ~1 µs because there's calibration uncertainty
(antenna cable thermal drift, BPSK detector bias, half-quantization-
step bias) that isn't captured in the per-PPS std.

Observed `t6_sigma_ms` ranges from the floor (1 µs) during steady
windows to ~280 µs during chain-delay-step-recovery transitions.

---

## 4. The pieces that aren't parameters but still matter

### 4.1 The dedicated T6 RadiodStream

T6 used to share the `MultiStream` socket with the 9 archive channels.
Every 10 minutes the archive channels do a synchronous zstd + fsync
of ~73 MB each at the file boundary. That blocked the shared receive
thread for 3-5 s; the kernel UDP buffer overflowed; T6 dropped
samples; the Costas loop unlocked. Predictable `?` events at every
:00 / :10 / :20 / :30 / :40 / :50 UTC.

**Fix:** T6 now always uses a dedicated `RadiodStream` (its own UDP
socket and reader thread), insulated from archive flushes. The 9
archive channels keep their shared `MultiStream`.

### 4.2 Async archive flush

Even with T6 on a dedicated stream, the archive channels themselves
were losing ~0.5-1% of samples every rollover from the same flush
contention. Fixed by moving each archive writer's `_flush_minute`
onto a per-writer daemon worker (bounded queue, non-blocking enqueue).
The receive thread no longer blocks on compression + fsync.

### 4.3 Kernel `rmem_max = 64 MB`

Was reverting to the Debian default (5 MB) every boot until a
last-loaded sysctl file (`/etc/sysctl.d/99-timestd.conf`) was set
to override the system-defaults file. 64 MB gives the kernel buffer
~10 s of multicast headroom — plenty for any expected userspace
stall.

### 4.4 Slow T6 timing-poll loop

A periodic `discover_channels()` thread that refreshes the GPS/RTP
anchor was running every 5 s (with a 2 s listen window). Its
"fresh anchor" path turned out to be unused (the SHM push code
uses `rtp_to_wallclock` directly), so the thread is now diagnostic-
only. Slowed to 30 s / 0.5 s — keeps the drift-monitoring layer
(Signal A + Layer 3 recapture) alive without burning CPU + multicast
chatter.

### 4.5 Watchdog

`timestd-hpps-watchdog.timer` runs every 60 s. If `LastRx > 600 s`
for HPPS in `chronyc tracking`, it restarts `timestd-core-recorder`.
There's a 1800 s cooldown after each restart to prevent flapping.
(2026-05-23: renamed from `timestd-tsl3-watchdog` with the chrony
refid rename TSL3→HPPS.)

This is a hammer, not a scalpel. It does keep HPPS alive across
weird failure modes, but it can also generate its own `?` events
(every restart kills the calibrator's accumulated lock state). When
diagnosing, *check the watchdog log first* — much of what looks
like "TSL3 went dark" turns out to be "the watchdog noticed an
earlier `?` and restarted us".

---

## 5. Open questions and known weirdness

### 5.1 chrony occasionally stops polling the SHM

Twice on 2026-05-21 we saw chrony stop polling SHM unit 2 entirely
(`LastRx` climbs forever, reach drops to 0 and stays). Manually
restarting `chrony.service` always recovers. The SHM segment itself
is healthy; core-recorder is pushing fine; chrony just doesn't read.

Probable cause: chrony has internal limits (`maxupdateskew`,
`maxchange`, `corrtimeratio`) that, when tripped, can put a source
into a "do not poll" backoff state. We haven't fully traced which
limit fires under what conditions.

**Workaround:** if you see TSL3 in `#?` with `LastRx` growing past
~300 s, restart `chrony.service`. Don't restart core-recorder — that
makes it worse (resets BPSK lock state too).

### 5.2 The BPSK regime is drifting

The dphase / band thresholds have had to be raised four times in
one day, each time because the steady-state regime exceeded the
previous tuning. This is *probably* an SNR issue (the BPSK signal's
SNR drives the natural loop jitter), but we haven't yet measured
the SNR directly. If it keeps climbing, the cleanest fix is to
**remove the dphase test entirely** and trust the band test alone —
real excursions are reliably detected by phase wandering 5+ rad
from the EMA, regardless of how dphase behaves.

### 5.3 Phase EMA settles at non-zero values

Originally the system was characterised with `phase_ema ≈ +0.13` rad.
Today it's running at `phase_ema ≈ +0.88`. That's a real shift in
the BPSK carrier's residual phase — probably reflecting Doppler /
local-oscillator drift between TS1 (the injector) and the RX-888.
It doesn't affect timing measurement quality (the chain_delay stays
stable), but it does mean the absolute phase numbers in the journal
won't match the original characterisation. Just check |Δφ| against
the EMA, not against any absolute reference.

---

## 6. The summary you can give the time nuts

Three sentences:

1. **Chrony's `±55us` for TSL3 is a static declared claim**, not a
   measurement. Read `chronyc sourcestats TSL3` for the dynamic
   std-dev (typically 150-300 ns); the new authority `t6_sigma_ms`
   in our SQLite history is dynamic too and floored at 1 µs.

2. **The Costas-loop guards** (dphase, band) are *over-protective*
   for the current BPSK regime here — they used to be tuned for a
   much quieter loop. We've moved the thresholds up (dphase 0.004 →
   0.050; band 0.5 → 2.0 rad) because chain_delay stays rock-stable
   even when those guards trip. Real excursions still get caught by
   the band test at 5+ rad swing.

3. **The plumbing matters more than the parameters.** The biggest
   stability gains today came from architectural fixes (T6 on its
   own UDP socket, async archive flush, kernel buffer headroom,
   bigger RTP resequencer buffer), not from tuning thresholds.

---

## 7. Change log

- 2026-05-21 11:00 UTC — original Costas tuning (`dphase 0.004`,
  `band 0.5`, against the 2026-05-18 documented excursion).
- 2026-05-21 16:51 — T6 moved to dedicated RadiodStream.
- 2026-05-21 17:50 — Authority publishes observed-jitter sigma.
- 2026-05-21 19:00 — Async archive flush + slow timing-poll.
- 2026-05-21 19:55 — T6 resequencer buffer 128 → 256.
- 2026-05-21 21:00 — Kernel `rmem_max` finally pinned at 64 MB.
- 2026-05-21 21:07-21:50 — Costas dphase threshold walked in four
  passes: 0.004 → 0.008 → 0.020 → 0.030 → 0.050.
- 2026-05-21 21:34 — Costas band threshold raised 0.5 → 1.0.
- 2026-05-21 21:50 — Costas band threshold raised 1.0 → 2.0.
- 2026-05-21 21:53 — chrony stuck in non-polling state, manual
  `systemctl restart chrony.service` recovered.

(Update this section in place; do not delete entries.)
