# HF Time Standard — Architecture First Principles

**The canonical statement of what this system is and is not.**
Re-read before any work that touches timing, chrony, sample labeling,
or the metrology product.  All other architecture documents in `docs/`
should be read with this one in mind.

---

## One-line summary

**RTP is the ruler.  Tn is the annotation quality.  Chrony is a customer.
Science is the goal.**

---

## 1. The substrate: RTP sample counter

The radiod RTP sample counter is the **timeline**.  Every audio sample
has an RTP timestamp that advances at the GPSDO-disciplined sample
rate.  That counter is the only thing in the system that is
**intrinsically traceable to a frequency standard** — it is set in
hardware and cannot drift.

> The RTP counter is the steel ruler.  Substrate.  Unmoving.  Calibrated
> by hardware.  Everything else (UTC labels, host-clock readings,
> chrony refclocks, fusion outputs, archive timestamps, log entries) is
> **annotation on top of the ruler**.

Annotations have authority tiers, uncertainties, and per-sample
validity.  The ruler does not.

## 2. The annotation: Tn timing-authority taxonomy

Each sample (or each group of samples covered by the same authority
state) carries an annotation of the form:

```
(RTP_sample_count, UTC_estimate, authority_tier, uncertainty)
```

The authority tier indicates how well we can convert this sample's RTP
timestamp into UTC.  Higher tier = better RTP→UTC conversion.

| Tier   | Authority source                                                                                              | What it gives us                                                                                |
|--------|---------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| **T6** | TS-1 HF-injected BPSK PPS via the RX-888 ADC.  The TS-1's onboard GPS PPS is BPSK-modulated onto a clean GPSDO-disciplined carrier (default 84.225 MHz), coupled into the RX path, and recovered sample-precise from the IQ stream.  Hard-wired analog signal path; latency is the (calibrable, §8) chain delay only. | ns-class after §8 chain-delay calibration. The deployed best tier on bee1.                      |
| **T5** | GPS+PPS delivered to the radiod host over USB (LBE-1421 USB-NMEA, plus any USB-PPS exposure on the same channel).  Software-mediated transport; precision floored by USB bus scheduling. | µs-to-ms class. Used for second-of-day disambiguation under T6, and as the standalone source when T6 is not available. |
| **T4** | GPS+PPS delivered via a LAN timeserver — stratum-1 NTP peer locked to a local GPSDO. Network-mediated.        | ms-class via LAN jitter.                                                                        |
| **T3** | HF Fusion (multi-station *received* time-signal consensus, no local GPS).                                     | sub-ms via consensus across WWV/WWVH/CHU/BPM etc. The regime our remote stations will operate in. |
| **T2** | WAN NTP (internet NTP, no local GPS).                                                                         | ms-class.                                                                                       |
| **T1** | GPSDO frequency-discipline only + wall clock (no PPS, no time-signal reception).                              | Excellent rate, mediocre absolute epoch.                                                        |
| **T0** | No GPSDO, wall clock only.                                                                                    | Whatever the host has.                                                                          |

**T6 sits above T5 because the TS-1 path is *hard-wired*** — the
BPSK-modulated PPS travels as an analog RF signal from the TS-1
through coax and through the RX-888 front-end into the ADC, where it
is recovered sample-precise from the IQ stream.  The only latency is
the static analog chain delay (TS-1 modulator → filter/attenuator →
RX-888 front-end → ADC), and that is exactly what §8 calibrates and
subtracts.  Once §8 is locked, the recovered PPS edge is good to
ns-class.

**T5 sits below T6 because the same GPSDO's PPS, delivered over USB
(LBE-1421 USB-NMEA, possibly USB-PPS on the same channel), is
software-mediated.**  USB scheduling jitter floors the deliverable
precision at µs-to-ms class regardless of how good the underlying
GPS PPS is.  T5 is the natural fallback when T6 drops out, and it
provides the per-second calendar context (year/month/day/hour/min/sec)
for T6 even while T6 is active.

**T4 sits below T5 because LAN transport adds further jitter** beyond
USB.  A nearby stratum-1 NTP peer locked to a local GPSDO is still
ms-class — good enough to keep host clocks honest, not good enough to
drive sample-precise science.

**A note on cross-validation.**  When both T6 and T5 (or T6 and a
host-side PPS-API path, e.g. TS-1 PPS OUT wired to a GPIO that the
kernel PPS subsystem can stamp) are simultaneously available,
comparing the two yields a continuous diagnostic on the analog chain
delay — drifts in §8 become observable in real time.  That is a
*science / metrology benefit*, not a separate tier; the tier of the
published annotation is still T6.

**Anchor capture vs. ongoing annotation.**  At first lock the cascade
chooses *which* lower tier supplies the integer-second context the
T6 anchor freezes against (T5 NMEA when available, else T4 / T3 /
T2).  After that moment the anchor itself — a frozen
``(anchor_rtp, anchor_utc_ns, sample_rate_hz)`` triple plus the
RF chain delay — *carries both* integer and fractional UTC for every
subsequent sample.  Pure arithmetic:
``utc_ns(rtp) = anchor_utc_ns + (rtp − anchor_rtp) × 10⁹ / sample_rate_hz``.
The cascade's role is to authorise the *capture*; it is not
consulted per-edge.  See ``hf_timestd.core.native_anchor`` and
``docs/TIMING-PIPELINE-WIRING.md`` §5.4.

**Local vs. received HF.**  Unlike *received* HF-PPS from a distant
transmitter (which has ionospheric path variation), the TS-1's local
injection has no propagation-medium variability.  T3 (HF Fusion) and
T6 (TS-1 local injection) both use HF, but the path physics is
entirely different: T3 reasons over ionospheric paths to derive UTC;
T6 receives a clean local signal whose only delay is the calibrable
analog chain.

T3 (Fusion without local GPS) is the regime our remote stations will
operate in.  HF time-signal fusion delivers sub-ms UTC to a station
that has only a GPSDO for frequency discipline — that is the DASI2
science value proposition.

## 3. The product: an annotated sample stream

hf-timestd's first-class output is the **annotated RTP stream**:
records of the form *(RTP, UTC estimate, tier, uncertainty)* covering
the sample timeline.  The science pipeline, fusion ingest, downstream
re-processing, DASI2 distribution, and chrony refclock feeds are all
**consumers** of that annotated stream.

**This is not the same thing as "a chrony refclock".**  A chrony
refclock is one consumer; the annotated stream is the product.

Other consumers include:
- The science pipeline (TID/Doppler/propagation work)
- Fusion ingest (combining with other stations' data)
- Offline re-processing (re-deriving UTC with a better algorithm later
  while the raw RTP timeline is preserved)
- DASI2 distribution (central reference station shipping offsets to
  remote consumers)

## 4. Per-station hardware variants

Different stations have different combinations of:
- **TS-1 HF-PPS injector** (present / absent) — enables T6.  The
  TS-1's onboard GPS supplies the PPS that gets BPSK-modulated into
  the RX path.
- **LBE-1421 USB GPS+PPS connection to the radiod host** (present /
  absent) — enables T5.  Provides per-second calendar context for
  T6 when both are present.
- **LAN stratum-1 NTP peer locked to a local GPSDO** (present /
  absent) — enables T4.
- **LBE-1421 or similar GPSDO** for the frequency reference (assume
  **present** — without it the RTP sample rate is not a calibrated
  ruler, and the entire tier hierarchy is degraded by free-running
  oscillator drift).

The tier in play depends on which of the above is wired at each
station.  The *architecture* is uniform: same RTP substrate, same
annotation schema, **different tier per station per epoch**.  We do
the best we can with what each station has and we annotate honestly
with what we know.

A possible future upgrade path for any T6 station: wire the TS-1
PPS OUT jack to a host GPIO or short-cable RS232 port that the
kernel PPS subsystem can timestamp.  That adds a second independent
ns-class path running alongside T6, enabling continuous
cross-validation of the §8 chain delay (see the "note on
cross-validation" paragraph in §2).

## 5. Where chrony fits

Chrony is a **downstream consumer** of the offset stream — useful for
keeping the host clock disciplined, not the architectural design center.

Specifically:
- Chrony expects a real-time SHM refclock feed in `(reference_time,
  system_time)` form
- That form requires a one-shot calibration that freezes the host
  clock's state at calibration moment
- That is a chrony-shape constraint, not a metrology constraint
- Chrony's selection (`#*`, `#x`, `#?`) reflects chrony's view of the
  source's fitness for **disciplining the host clock**, NOT the
  underlying quality of the RTP annotation

**Implication**: chrony marking a refclock `#x` does not mean the
underlying metrology is broken.  It means the chrony-facing facade
has a calibration mismatch.  The science annotations on the RTP stream
are still valid.

The chrony feed (FUSE SHM unit 1, HPPS SHM unit 2; plus the
default-disabled HFPS SHM unit 3) is a **convenience** for keeping the
host clock disciplined.  If chrony
likes it, great; if not, that is independent of whether the annotated
RTP stream is usable for science.

## 6. Frequency vs absolute-time accuracy

Ionospheric science needs **both**.  Different parts of the system
supply them:

- **Frequency accuracy** is supplied by the **GPSDO-disciplined sample
  rate**.  Period-to-period stability is ppb-class regardless of which
  tier is providing UTC labels.  This is independent of all chrony
  drama.
- **Absolute-time accuracy** is supplied by the **active timing tier**
  (T6 through T0).  The annotation honestly reports which tier is
  active and what the uncertainty is.

These are separable.  Frequency is always good (as long as the GPSDO
is locked).  UTC quality varies by station and tier — and that's fine,
as long as the annotation reports it accurately.

## 7. What this *does not* mean

This architecture does not:
- Forbid feeding chrony — that's a legitimate convenience layer
- Imply our SHM refclocks are bad — they're useful for any consumer
  that wants real-time UTC discipline including chrony
- Mean we shouldn't fix calibration weaknesses — but those fixes
  should be evaluated by their effect on **annotation quality**,
  not by whether chrony picks the source as `#*`
- Replace the existing docs — it anchors them.  ARCHITECTURE.md,
  METROLOGY.md, TECHNICAL_REFERENCE.md, TIMING-PIPELINE-WIRING.md,
  and HF-PPS-CHRONY-TUNING.md describe the system at progressively
  more detailed levels.  This doc states the principles those docs
  serve.

## 8. Quick reference: anti-patterns

If a discussion, design, or task arrives framed as one of these,
**stop and reframe** in substrate terms before proceeding:

- ❌ "Improve HPPS as a chrony source"
  → ✅ "Improve the T6 annotation quality / freshness / uncertainty"
- ❌ "Why doesn't chrony select HPPS as `#*`?"
  → ✅ "What does the T6 annotation report for this sample, and how
       does that compare to other tiers chrony also sees?"
- ❌ "Real-time core vs physics overlay"
  → ✅ "Real-time annotation production (T-tier maintenance) vs
       physics overlay (science consumers of the annotation)"
- ❌ "What should we feed chrony?"
  → ✅ "What's the best annotation we can produce per sample, and
       which consumers does it serve?"

## 9. Cross-references

- Memory note: `project_rtp_substrate_architecture` (load-bearing
  context for any future Claude session)
- `METROLOGY.md` §3 (Steel Ruler), §4.3 (RTP Authoritative Reference),
  §4.5 (Timing Authority Hierarchy) — the deeper metrological story
- `ARCHITECTURE.md` — the eight services and how they cooperate
- `TIMING-PIPELINE-WIRING.md` — Pattern A/B SHM-facade conventions
- `HF-PPS-CHRONY-TUNING.md` — the chrony-facing facade for T6/HFPS
- `TECHNICAL_REFERENCE.md` — developer reference

If any of those docs *contradict* this one, this one wins and the
contradicting doc should be updated.
