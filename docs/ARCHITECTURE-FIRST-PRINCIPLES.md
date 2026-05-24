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

| Tier   | Authority source                                                 | What it gives us                                          |
|--------|------------------------------------------------------------------|-----------------------------------------------------------|
| **T6** | GPS+PPS locally **plus** HF-injected PPS in the RTP stream       | T5 + a propagation cross-check                            |
| **T5** | GPS+PPS direct to the radiod host (hardware PPS via PPS-API)     | Kernel-precise hardware PPS — the gold standard           |
| **T4** | GPS+PPS over LAN (stratum-1 NTP peer locked to a local GPSDO)    | µs-class via a nearby disciplined host                    |
| **T3** | HF Fusion (multi-station time-signal reception, no local GPS)    | sub-ms via consensus across WWV/WWVH/CHU/BPM etc.         |
| **T2** | WAN NTP (internet NTP, no local GPS)                             | ms-class                                                  |
| **T1** | GPSDO frequency only + wall clock (no PPS, no time-signal recv)  | Excellent rate, mediocre absolute epoch                   |
| **T0** | No GPSDO, wall clock only                                        | Whatever the host has                                     |

**T6 sits above T5 because the HF-injected PPS is a *propagation
cross-check* on top of T5's accurate local reference — not a competing
absolute-time source.**  Architecturally, T6 cannot beat T5 in absolute
accuracy (the HF-injected PPS arrives via the RF chain with
ionospheric delay variation).  Its value is the science instrument
sitting alongside the metrology: the HF-PPS *signal* carries
information about the propagation path that nothing else gives us.

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
- TS-1 HF-PPS injector (present / absent)
- Local GPS+PPS to radiod (present / absent)
- LBE-1421 or similar GPSDO (assume **present** — without it the RTP
  sample rate is not a calibrated ruler)

The tier in play depends on what's wired at each station.  The
*architecture* is uniform: same RTP substrate, same annotation schema,
**different tier per station per epoch**.  We do the best we can with
what each station has and we annotate honestly with what we know.

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

The chrony feed (HPPS SHM unit 2, HFPS SHM unit 3, FUSE SHM unit 1) is
a **convenience** for keeping the host clock disciplined.  If chrony
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
