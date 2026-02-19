# Two-Pipeline Architecture: Metrology vs Physics

**Date:** February 19, 2026  
**Status:** DESIGN — supersedes mixed-purpose sections of ARCHITECTURE.md  
**Author:** Michael James Hauan (AC0G)

---

## The Core Insight

The system has been conflating two distinct objectives that have different timing requirements, different accuracy needs, and different relationships to physical models:

**Metrology:** Recover UTC as accurately as possible from HF time standard broadcasts.  
**Physics:** Learn about the ionosphere using HF signals, with the timing authority as a reference.

These are not the same problem. Mixing them degrades both.

---

## The Fundamental Asymmetry

In **RTP mode** (GPS+PPS via radiod), the system already has a ~50 µs timing authority. In this mode:

- Every HF signal arrival time is a **measurement of the propagation path**, not of the clock.
- D_clock ≈ 0 by definition. The HF signals are cross-checks, not the primary source.
- Physics models should **inform interpretation** of what we see, not **gate** what we record.

In **Fusion mode** (no GPS+PPS), the HF signals are the primary timing source. Here:

- Physics models are essential for propagation delay correction.
- The ionosphere is a nuisance to be modeled away, not a signal to be measured.
- The goal is a single best estimate of D_clock, not a catalogue of all arrivals.

The current codebase applies Fusion-mode logic (physics gate, single-best-arrival, TEC correction) even in RTP mode. This is the confusion.

---

## Pipeline A: Metrology

### Objective

Produce the best possible chrony feed — the most accurate estimate of D_clock = T_system − T_UTC — from HF broadcast time-of-arrival measurements, combined with geometry, propagation models, and any other information that reduces uncertainty.

### What "best possible" means

In RTP mode: sub-millisecond consistency across independent paths. The GPS+PPS is the reference; HF measurements are validation and secondary discipline.

In Fusion mode: sub-10ms accuracy from HF alone, sufficient to discipline a local clock better than NTP.

### Data flow

```
L0: Raw IQ (binary archive, GPS-timestamped)
    ↓
L1: Per-minute tone detections
    • raw_toa_ms: timing error vs GPS epoch (the observable)
    • snr_db, doppler_hz, coherence
    • station, frequency, minute_boundary
    • physics_prior_ms: model-predicted delay (informational, not a gate)
    • physics_likelihood: P(detection | model) — a weight, not a threshold
    ↓
L2: Calibrated timing measurements (per broadcast, per minute)
    • clock_offset_ms: D_clock after propagation correction
    • propagation_delay_ms: best-estimate model delay
    • propagation_mode: identified mode (with confidence)
    • uncertainty_ms: ISO GUM budget
    • tof_kalman_ms: Kalman-filtered ToA
    ↓
L3: Fused timing
    • Weighted combination across all broadcasts
    • Per-broadcast Kalman state
    • Chrony SHM output
```

### Role of physics models in metrology

Physics models serve metrology by:

1. **Predicting propagation delay** so it can be subtracted from raw ToA to get D_clock.
2. **Identifying the propagation mode** (which hop count) so the correct geometric delay is used.
3. **Providing a likelihood weight** for each detection — a detection far outside the model window gets lower weight, not zero weight.
4. **Estimating uncertainty** — the model's confidence in its delay prediction contributes to the uncertainty budget.

Physics models do **not** serve metrology by:
- Rejecting detections that don't fit (the model may be wrong)
- Requiring a unique mode identification before accepting a measurement
- Correcting D_clock for TEC (in RTP mode, TEC is the signal, not the error)

### The gate → weight change

The current `ArrivalPatternMatrix.validate_detection()` returns a binary (valid/invalid). This should become a **likelihood weight** in [0, 1]:

```python
# Current (wrong in RTP mode):
is_valid, confidence, reason = matrix.validate_detection(...)
if not is_valid:
    continue  # Detection discarded

# Correct:
likelihood, reason = matrix.detection_likelihood(...)
measurement.physics_weight = likelihood  # Used in weighted fusion, never zero
```

A detection at 3σ outside the model window has likelihood ~0.01, not 0. It still carries information — the model may be wrong, or there may be an unusual propagation condition worth recording.

### What to remove from metrology

| Component | Current role | Action |
|---|---|---|
| Physics gate (binary reject) | Discards detections outside IRI window | → Replace with likelihood weight |
| TEC correction in fusion | Corrects D_clock for ionospheric dispersion | → Remove in RTP mode; keep in Fusion mode |
| Multi-mode arrival averaging | Averages over all detected modes | → Keep only dominant mode for D_clock; record others for physics |
| `physics_fusion_service.py` in real-time path | Computes TEC from L2 timing measurements | → Move to physics pipeline (async) |

---

## Pipeline B: Physics

### Objective

Characterize the ionosphere using HF signals, with the GPS+PPS timing authority as the reference. This pipeline is **not real-time** — it can run on the raw archive at any time, with any model version, and produce science data products.

### Key principle

**Record everything. Interpret later.**

A 7.85 MHz CHU signal arriving via 2F2, 3F2, and 4F2 simultaneously is **three measurements** of three different ionospheric paths. The current pipeline picks one and discards the others. The physics pipeline records all three.

### What the physics pipeline measures

| Observable | Physical meaning | Precision |
|---|---|---|
| Group delay per arrival | Virtual height of reflection layer | ~1 ms (noise-limited) |
| Carrier phase (tick_phase) | Doppler shift, dTEC/dt | ~0.001 ms |
| Arrival time spread within minute | Multipath delay spread | ~0.1 ms |
| Cross-frequency group delay difference | Absolute TEC (when signal >> noise) | ~1 TECU |
| Cross-minute carrier phase continuity | Integrated TEC | ~0.1 TECU |
| Arrival amplitude vs time | Ionospheric fading statistics | — |
| Multi-path arrival structure | Mode competition, layer heights | — |

### When group-delay TEC is measurable

The ionospheric group delay spread across frequencies f₁ and f₂ for TEC τ is:

```
Δt = K · τ · (1/f₁² − 1/f₂²)    where K = 40.3/c ≈ 1.34×10⁻⁷ s·Hz²/TECU
```

| Station | Freq range | Δt per 10 TECU | Noise floor | Measurable? |
|---|---|---|---|---|
| CHU | 3.3–14.7 MHz | 1.15 ms | ~1 ms | Marginal |
| WWV | 5–25 MHz | 0.52 ms | ~1 ms | No |
| WWV | 5–15 MHz | 0.48 ms | ~1 ms | No |
| BPM | 2.5–15 MHz | 1.13 ms | ~1 ms | Marginal |

**Conclusion:** Group-delay TEC from these stations is at or below the noise floor. The carrier-phase dTEC (already implemented in `carrier_tec.py`) is the right tool — it has ~100× better precision.

### Data flow

```
L0: Raw IQ (binary archive) — the immutable record
    ↓ (reprocessable at any time)
L1-phys: All detected arrivals per minute per channel
    • Every peak above threshold, not just the dominant one
    • arrival_ms, snr_db, doppler_hz, coherence per arrival
    • No mode identification at this stage — just observables
    ↓
L2-phys: Mode-interpreted arrivals
    • Model likelihood for each mode assignment
    • Virtual height estimate per arrival
    • Carrier phase continuity (cross-minute stitching)
    ↓
L3-phys: Science products
    • dTEC timeseries (carrier phase, per station-frequency)
    • Propagation mode structure (mode competition vs time)
    • Ionospheric layer heights (virtual height vs time)
    • TID detection (cross-path correlation)
    • Absolute TEC (anchored via GNSS VTEC)
```

### The model's role in physics

In the physics pipeline, models are used to **assign likelihoods to interpretations**, not to filter data:

```
For each detected arrival:
    For each candidate mode (1F2, 2F2, 3F2, 1E, ...):
        P(mode | arrival_time, frequency, geometry, ionosphere) ∝
            P(arrival_time | mode, frequency, geometry, ionosphere) ·
            P(mode | frequency, geometry, ionosphere)
```

The output is a **probability distribution over modes**, not a single mode assignment. This is Bayesian inference, not a lookup table.

### Timing authority for physics

In RTP mode: GPS+PPS via radiod (~50 µs). Every arrival time is measured against this reference.

In archive reprocessing: The RTP timestamps in the binary archive provide the same reference. The physics pipeline can reprocess any historical minute with updated models.

The physics pipeline does **not** need chrony. It does not discipline the clock. It uses the clock as a reference.

---

## Interface Between Pipelines

The metrology pipeline produces L1 measurements that the physics pipeline can use as inputs:

```
Metrology L1 (raw_toa_ms, snr_db, doppler_hz per dominant arrival)
    → Physics pipeline reads these as one input among many
    → Physics pipeline also reads L0 raw IQ directly for multi-arrival detection
```

The physics pipeline produces science products that the metrology pipeline can optionally use:

```
Physics L3 (dTEC timeseries, VTEC map)
    → Metrology can use VTEC as a propagation correction in Fusion mode
    → In RTP mode, metrology does not need this
```

The key constraint: **the physics pipeline must never be in the real-time critical path for metrology**. If the physics service is slow, crashed, or reprocessing old data, the metrology pipeline must be unaffected.

---

## Current Code vs Target

### Files that belong to metrology only

| File | Status |
|---|---|
| `metrology_engine.py` | Keep; remove physics gate binary logic |
| `metrology_service.py` | Keep as-is |
| `multi_broadcast_fusion.py` | Keep; remove TEC correction in RTP mode |
| `broadcast_kalman_filter.py` | Keep as-is |
| `chrony_shm.py` | Keep as-is |
| `arrival_pattern_matrix.py` | Keep; change validate_detection → likelihood |
| `propagation_mode_solver.py` | Keep; used for delay correction |
| `l2_calibration_service.py` | Keep; produces L2 timing measurements |

### Files that belong to physics only

| File | Status |
|---|---|
| `physics_fusion_service.py` | Move to async/batch; remove from real-time path |
| `tec_estimator.py` | Physics only; remove from metrology fusion |
| `carrier_tec.py` | Physics only; already correct |
| `iono_tomography.py` | Physics only |
| `vtec_mapper.py` | Physics only (except VTEC correction input to metrology) |

### Files that serve both (shared infrastructure)

| File | Role |
|---|---|
| `ionospheric_model.py` | Propagation delay prediction (metrology) + mode likelihoods (physics) |
| `propagation_model.py` | Same |
| `iono_data_service.py` | Provides ionospheric parameters to both |
| `carrier_tec.py` | Physics science product; dTEC can optionally inform metrology |

---

## Immediate Action Items

In priority order:

### 1. Gate → Weight in MetrologyEngine (low risk, high impact)

Change `arrival_pattern_matrix.validate_detection()` from binary gate to likelihood weight. Detections outside the model window get weight 0.01–0.1, not zero. This immediately fixes CHU rejection in edge cases and removes the model-accuracy dependency from metrology correctness.

### 2. Remove TEC correction from fusion Kalman in RTP mode (low risk)

In `multi_broadcast_fusion.py`, the TEC correction applied before the Kalman update adds model noise in RTP mode. In RTP mode, D_clock should be measured directly without ionospheric correction — the GPS reference is more accurate than the TEC model.

### 3. Record all arrivals in L1 (medium effort, enables physics)

In `MetrologyEngine._measure_tone_at_known_time()`, when multiple peaks are detected above threshold, write all of them to a new `L1/all_arrivals` HDF5 dataset. The dominant arrival continues to feed the metrology L1 path unchanged. This is purely additive — no existing behavior changes.

### 4. Decouple physics_fusion_service from real-time path (medium effort)

Remove `timestd-physics.service` from the real-time dependency chain. Make it a batch service that processes completed minutes from the archive. It should not block or be blocked by the metrology pipeline.

---

## What This Is Not

This is not a proposal to remove physics from the system. Physics knowledge is essential for metrology — propagation delay prediction, mode identification, and uncertainty estimation all require physics models. The change is:

- Physics models **inform** metrology (provide priors, likelihoods, delay estimates).
- Physics models do not **gate** metrology (no binary accept/reject based on model fit).
- Physics science products are produced **asynchronously**, not in the real-time metrology path.
- The raw archive is the **canonical record** — physics can always reprocess it with better models.
