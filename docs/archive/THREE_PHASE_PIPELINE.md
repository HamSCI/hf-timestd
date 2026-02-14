# Time-Aligned Data Pipeline (hf-timestd)

## Overview

`hf-timestd` implements the time-transfer pipeline for HF time standard stations (BPM, CHU, WWV, WWVH):

- Phase 1: immutable raw archive (20 kHz IQ)
- Phase 2: analytical engine producing `D_clock` (system time vs UTC)

**Scope note:** Phase 3 products (decimation/10 Hz, spectrogram products, PSWS/GRAPE uploads) are handled by the separate `grape-recorder` project.

---

## Phase 1: Immutable Raw Archive (binary `raw_buffer/`)

Design goals:

- Raw IQ is stored as the source-of-truth scientific record.
- No UTC corrections are applied in Phase 1.
- Files are written as per-minute binary complex64 with JSON sidecars.

Directory structure (per channel):

```
{data_root}/raw_buffer/{CHANNEL}/YYYYMMDD/
    {minute_boundary}.bin[.zst|.lz4]
    {minute_boundary}.json
```

Implementation:

- `src/hf_timestd/core/binary_archive_writer.py`

---

## Phase 2: Analytical Engine (`D_clock`)

Phase 2 consumes Phase 1 raw IQ and produces:

- Tone detections
- WWV/WWVH discrimination
- Propagation solutions
- `D_clock` measurements and confidence/uncertainty

Outputs are written under:

```
{data_root}/phase2/{CHANNEL}/...
```

Primary persistence:

- `clock_offset_series.csv`

Implementation:

- `src/hf_timestd/core/phase2_temporal_engine.py`
- `src/hf_timestd/core/clock_offset_series.py`
- `src/hf_timestd/core/phase2_analytics_service.py` (daemon wrapper)

---

## Phase 3 (external)

For decimation/10 Hz products and uploads, use `grape-recorder`.

---

## Orchestration

The real-time coordinator is:

- `src/hf_timestd/core/pipeline_orchestrator.py`

Note: In `hf-timestd`, Phase 3 is intentionally disabled.
