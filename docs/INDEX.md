# hf-timestd Documentation Index

Reading-order map for the `hf-timestd` documentation set. Start at the top and
follow the track that matches your goal. The **canonical** docs (one per domain)
are marked ★ — when two docs disagree, the canonical one wins.

> Scientific and metrology methods cite their sources inline; the consolidated
> bibliography lives in [`publications/QEX_DRAFT.md`](publications/QEX_DRAFT.md)
> (the ARRL QEX article draft), which is the reference of record for the methodology.

---

## 1. Start here

| Doc | What it gives you |
|-----|-------------------|
| [OVERVIEW.md](OVERVIEW.md) | The system in one page: what it is, the metrology/physics split, where to go next. |
| [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md) ★ | The substrate-vs-annotation framing and the producer-side timing-authority (§18) contract surface. Read before changing the timing path. |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Formal `HFT-*` requirements, reconciled to the code with status tags. |

## 2. Architecture

| Doc | What it gives you |
|-----|-------------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) ★ | Pipeline + service layering (Recording → Metrology → Fusion → Chrony SHM), the SQLite storage backend, the service/timer inventory. |
| [TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md) | Channels, data products, config shape, CLI surface. |
| [design/](design/) | Design rationale behind the architecture (see [design/METROLOGY_PHYSICS_SPLIT.md](design/METROLOGY_PHYSICS_SPLIT.md), the authority and measurement-path designs). |

## 3. Metrology & timing

| Doc | What it gives you |
|-----|-------------------|
| [METROLOGY.md](METROLOGY.md) ★ | **The canonical timing reference.** T-tier hierarchy, the §4.5–§4.6 timing-authority invariant, error budget, chrony feeds (FUSE/HPPS), the clock-health watchdog. |
| [TIMING-PIPELINE-WIRING.md](TIMING-PIPELINE-WIRING.md) | Runtime wiring of RTP / chrony / fusion and the SHM producers. |
| [BPSK-PPS-DETECTION-METHODS.md](BPSK-PPS-DETECTION-METHODS.md) | The five BPSK PPS edge-detection methods (HPPS matched-filter; HFPS diff feed wired but disabled by default). |
| [HF-PPS-CHRONY-TUNING.md](HF-PPS-CHRONY-TUNING.md) | Costas + chrony refclock tuning for the BPSK PPS feed. |
| [HDF5-TO-SQLITE-MIGRATION.md](HDF5-TO-SQLITE-MIGRATION.md) | The current storage design (SQLite is the sole backend post-v7.0); cited by `io/sqlite_*`. |

## 4. Physics & ionospheric science

| Doc | What it gives you |
|-----|-------------------|
| [PHYSICS.md](PHYSICS.md) ★ | **The canonical science capability inventory** (✅/⚠️/❌ honesty markers): dTEC, propagation-mode ID, foF2, TID, scintillation. |
| [CARRIER_DOPPLER_INTERPRETATION.md](CARRIER_DOPPLER_INTERPRETATION.md) | Doppler-as-data: carrier-phase dTEC and path-length interpretation. |
| [IONOSPHERIC_REANALYSIS.md](IONOSPHERIC_REANALYSIS.md) | Hourly post-hoc physics filter (foF2/foE MUF validation, mode correction). |
| [IONOSPHERIC_RESOLUTION.md](IONOSPHERIC_RESOLUTION.md) | Error-source hierarchy (ionospheric path delay dominates). |
| [PHARLAP_RAYTRACING.md](PHARLAP_RAYTRACING.md) | PHaRLAP/pyLAP ray tracing (advisory overlay), the `raytrace` CLI, worked examples. |
| [GPS_TEC_OPTIONAL.md](GPS_TEC_OPTIONAL.md) · [ZED_F9P_TEC_CONFIGURATION.md](ZED_F9P_TEC_CONFIGURATION.md) | Optional dual-frequency GNSS TEC validation. |
| [PHASE_ENGINE_ARCHITECTURE.md](PHASE_ENGINE_ARCHITECTURE.md) | ⚠️ **Planned** coherent multi-antenna array — design only; the array DSP is not implemented (channel registry + external phase-engine source selection exist). |

## 5. Data products, upload & integration

| Doc | What it gives you |
|-----|-------------------|
| [GRAPE_DAILY_PROCESSING.md](GRAPE_DAILY_PROCESSING.md) | The daily PSWS pipeline (decimate → spectrogram → package → upload + verify). |
| [INTEGRATION.md](INTEGRATION.md) | Client API for wsprdaemon v4 and external consumers (SQLite store, authority snapshot). |
| [external-data-sources.md](external-data-sources.md) | Live external data sources + health-check commands. |
| [HAMSCI_QUALITY_METADATA_PROPOSAL.md](HAMSCI_QUALITY_METADATA_PROPOSAL.md) | Proposed HamSCI quality/uncertainty metadata standard. |
| [DRF_UPLOAD_SYSTEM.md](DRF_UPLOAD_SYSTEM.md) | Pointer: Digital RF output is handled by the separate grape-recorder project. |

## 6. Operations, setup & deployment

| Doc | What it gives you |
|-----|-------------------|
| [DEBUGGING.md](DEBUGGING.md) ★ | Journald-only logging, triage recipes (incl. clock free-run / makestep), deploy footguns. |
| [EXTERNAL_PREREQUISITES.md](EXTERNAL_PREREQUISITES.md) | The six external dependencies and how to stage them. |
| [STATION_SETUP_GUIDE.md](STATION_SETUP_GUIDE.md) · [PSWS_SETUP_GUIDE.md](PSWS_SETUP_GUIDE.md) · [NASA_EARTHDATA_SETUP.md](NASA_EARTHDATA_SETUP.md) | First-time site, PSWS upload, and NASA Earthdata setup. |
| [PIPELINE_VERIFICATION.md](PIPELINE_VERIFICATION.md) | Pipeline verification procedure. |
| [WWVB-INTEGRATION.md](WWVB-INTEGRATION.md) | WWVB 60 kHz fusion source (Layer 4 — currently gated off, awaiting validation). |

## Subdirectories

| Dir | Contents |
|-----|----------|
| [design/](design/) | Current architecture/design rationale. |
| [features/](features/) | Feature designs + operational references (matched filters, monitoring, calibration, paths). |
| [web-ui/](web-ui/) | FastAPI dashboard reference (config sync, SSRC, timing dashboard). |
| [publications/](publications/) | The QEX paper draft and HamSCI 2026 talk/abstract (the methodology bibliography). |
| [reference/](reference/) | External specifications (e.g. NIST Enhanced WWVB broadcast format). |
| [figures/](figures/) · [images/](images/) | Figure assets for the docs and publications. |
| [archive/](archive/) | Historical & superseded material — kept, not deleted. Session logs in [archive/sessions/](archive/sessions/), retired feature/design docs in [archive/features/](archive/features/) and [archive/design/](archive/design/), dated code reviews and phase records at the archive root. |

---

*Canonical reference of record for timing: METROLOGY.md §4.5–§4.6. For science: PHYSICS.md.
Both are reconciled against the code; the `HFT-*` requirement IDs in REQUIREMENTS.md trace the claims.*
