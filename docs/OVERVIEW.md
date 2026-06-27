# What hf-timestd Does, and Why

**Audience:** new contributors and users who want the 5-minute mental model
before diving into the deeper docs.
**Status:** entry-point — addresses review item D-H8.
**Last Updated:** 2026-05-20

---

## The system in one sentence

`hf-timestd` listens to four HF time-standard broadcasts — **WWV** (Fort
Collins), **WWVH** (Kauai), **CHU** (Ottawa), and **BPM** (Pucheng) — via
[ka9q-radio](https://github.com/ka9q/ka9q-radio) and turns their on-time
markers into two things at once:

1. a **GPS-disciplined clock alternative** that feeds [chrony](https://chrony-project.org/) via SysV SHM (sub-millisecond UTC for the host), and
2. an **ionospheric observatory** — TEC, dTEC, propagation-mode inferences, multi-path arrivals, scintillation indices — recorded continuously to SQLite for off-line science.

The two outputs share **one** receiver and **one** measurement pipeline.
Everything else in the docs is about how that's possible without the two
purposes corrupting each other.

---

## The two pipelines

| | **Metrology pipeline** | **Physics pipeline** |
|---|---|---|
| Question it answers | "What time is it, and how accurately do we know?" | "What did the ionosphere do?" |
| Primary outputs | L1 ToA → L2 d_clock → L3 fused timing → chrony SHM | L2 propagation-corrected delays → L3 TEC/dTEC/TID/foF2 |
| Latency target | Real-time (per-minute) | Near-real-time (per-minute / per-hour) |
| Failure mode | Clock drifts; chronyd marks the refclock unreachable | Stale ionospheric estimate; downstream science papers cite a wider uncertainty |
| Service unit | `timestd-metrology@.service`, `timestd-fusion.service` | `timestd-physics.service`, `timestd-l2-calibration.service` |
| Canonical doc | [`METROLOGY.md`](METROLOGY.md) | [`PHYSICS.md`](PHYSICS.md) |
| Contract | [`METROLOGY_CONTRACT.md`](../.windsurf/contracts/METROLOGY_CONTRACT.md) | [`PHYSICS_CONTRACT.md`](../.windsurf/contracts/PHYSICS_CONTRACT.md) |
| Design rationale | [`docs/design/METROLOGY_PHYSICS_SPLIT.md`](design/METROLOGY_PHYSICS_SPLIT.md) | (same) |

The pipelines branch at L2 — metrology and physics share L1 (raw ToA)
but build their L2/L3 products independently and write to disjoint
subdirectories under `/var/lib/timestd/phase2/`.  A crash in one pipeline
cannot corrupt the other; a metrology restart does not delete physics
state.

---

## Two operating modes

Independent of the pipeline split, the same code runs under two
**operating modes** depending on what timing reference is available:

| | **RTP mode** | **Fusion mode** |
|---|---|---|
| Timing authority | GPS + PPS (host has a real GPSDO) | The HF broadcasts themselves |
| Used for | Calibration, holdover validation, ground-truth testing | Production at GPS-denied sites |
| TEC handling | Science observable (model corrections OFF) | Model input (corrections ON) |
| Practical accuracy | ~50 μs UTC | sub-ms UTC under good propagation |

Mode is configured per host; the same channel definitions and the same
DSP code run in both.

---

## Terminology cheatsheet

A few terms drift across documents.  This is what they mean **here**:

* **Pipeline** = metrology vs physics (functional split). Not the same as "mode".
* **Mode** = RTP vs Fusion (timing-authority split). Not the same as "pipeline".
* **Level** = L1 / L2 / L3 (data-product stage). Independent of both above.
* **Service** = a systemd unit (`timestd-*.service`).  ~8 units in
  a full deployment; `hf-timestd service status` lists them all.
* **Channel** = one (station, frequency) pair plumbed end-to-end —
  e.g. `WWV_10000` for WWV at 10 MHz.  Typically 9 channels in production.
* **Broadcast** = a synonym for "channel" used in the fusion layer's
  calibration tables and weight policy.

When you find conflicting terminology in older docs, this file is the
tiebreaker.

---

## Where to go next

```
README.md ─── start here for install / quick-run
   │
   ├──► OVERVIEW.md (you are here) ── conceptual landscape
   │
   ├──► METROLOGY.md ── the timing side, in depth
   ├──► PHYSICS.md   ── the ionospheric side, in depth
   ├──► ARCHITECTURE.md ── system design, services, data flow
   └──► TECHNICAL_REFERENCE.md ── developer reference (algorithms, formats)

design/
   ├──► METROLOGY_PHYSICS_SPLIT.md ── why the pipelines diverge at L2
   ├──► UNIFIED_MEASUREMENT_PATH.md ── why both modes share one detector
   └──► TIMING_AUTHORITY_ARCHITECTURE.md ── how RTP/Fusion is gated

.windsurf/
   ├──► METROLOGY_CONTRACT.md ── what L1/L2/L3 metrology products MUST do
   └──► PHYSICS_CONTRACT.md   ── what L2/L3 physics products MUST do
```

Critical / High findings against any of these documents are tracked in
the rolling code review at
[`docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md`](archive/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md);
the entries flagged **D-**, **P-** or **M-** all point back to specific
files and lines.

---

## What hf-timestd is *not*

* Not a substitute for a GPSDO if you need < 50 μs UTC.
* Not an OTA receiver; it consumes RTP streams from `ka9q-radio`, which
  must already be tuned and running.
* Not a closed-source product.  Everything is MIT; field data, code,
  contracts, and reviews all live in the same tree.

---

## One-sentence summary, again

**One receiver, two products, two pipelines, two modes — and a
disciplined refusal to let the science side corrupt the timing side
or vice versa.**
