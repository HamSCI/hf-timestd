# Dual-Purpose Architecture: Timing Reconstruction and Ionospheric Measurement

## The Fundamental Duality

The HF Time Standard system observes 17 broadcasts from 4 stations (WWV, WWVH, CHU, BPM) across multiple frequencies. These observations serve two complementary purposes:

### Purpose 1: Timing Reconstruction (Fusion)

**Given:** Broadcasts originate from transmitters at:
- Precise, surveyed locations (known to meters)
- Atomic clock frequency accuracy (parts in 10^13)
- UTC-synchronized timing (microsecond precision)

**Goal:** Reconstruct that precision locally by:
- Measuring time-of-arrival of multiple broadcasts
- Correcting for propagation delays (geometry + ionosphere)
- Fusing multiple independent measurements
- Achieving timing precision better than any single measurement

**Key Insight:** The transmitters are "perfect" - any variation we observe is due to:
1. Propagation path (ionosphere, multipath)
2. Our measurement uncertainty
3. Our local clock error (what we're trying to measure)

### Purpose 2: Ionospheric Characterization

**Given:** Local timing precision from either:
- **RTP Mode:** GPS+PPS provides authoritative timing (±100ns)
- **Fusion Mode:** Reconstructed timing from broadcast fusion

**Goal:** Measure ionospheric effects as the residual after removing known quantities:

```
T_observed = T_transmitted + T_propagation + T_ionosphere + T_noise

T_ionosphere = T_observed - T_transmitted - T_geometric - T_clock_error
```

**Key Insight:** The better our timing precision, the more precisely we can characterize the ionosphere. With perfect timing, all residual variation is ionospheric.

## The Circular Dependency (And Its Resolution)

There's an apparent circularity:
- To measure the ionosphere, we need good timing
- To get good timing from fusion, we need to model the ionosphere

**Resolution:** The system operates in two modes that break this circularity:

### RTP Mode (Ionospheric Focus)
```
GPS+PPS → Authoritative Timing → Measure Ionosphere
```
- Timing is externally provided (GPSDO)
- All broadcast variations are attributed to ionosphere
- Produces high-quality ionospheric measurements
- No fusion needed for timing

### Fusion Mode (Timing Focus)
```
Broadcasts → Model Ionosphere → Fuse Timing → Refine Model → Iterate
```
- Bootstrap with NTP for initial minute identification
- Use physics model (IRI-2020) for initial ionospheric estimates
- Fuse multiple broadcasts to estimate clock offset
- Residuals improve ionospheric model
- Converges to stable timing + ionospheric state

## The 17 Broadcasts

| Station | Frequencies (kHz) | Distance (km) | Propagation Delay | Role |
|---------|-------------------|---------------|-------------------|------|
| WWV     | 2500, 5000, 10000, 15000, 20000, 25000 | ~1120 | ~4 ms | **Primary Anchor** |
| WWVH    | 2500, 5000, 10000, 15000 | ~6600 | ~22 ms | **Primary Anchor** |
| CHU     | 3330, 7850, 14670 | ~1520 | ~5 ms | **Primary Anchor** (Reference) |
| BPM     | 2500, 5000, 10000, 15000 | ~11000 | ~40 ms | Secondary/Scientific |

### Station Priority Policy (2026-02-04)

**Primary Timing Anchors:** CHU, WWV, WWVH
- Shorter propagation paths with lower ionospheric uncertainty
- Well-characterized signal formats
- CHU provides FSK-verified timing (unique frequencies, no discrimination needed)
- WWV is closest station with best SNR

**Secondary/Scientific Source:** BPM
- Maintained for scientific interest (ionospheric probing of trans-Pacific path)
- Very long path (~11,000 km) with high ionospheric variability
- Multi-hop propagation introduces more uncertainty
- UT1/UTC alternation (minutes 25-29, 55-59) requires careful handling
- Weight reduced to 30% of primary anchors in fusion algorithm

Each broadcast provides:
- **Timing information:** Tone arrival relative to UTC second
- **Ionospheric information:** Frequency-dependent delay (1/f²)
- **Path information:** Multipath, fading, Doppler

## Measurement Hierarchy

### Level 1: Raw Observations
- Time-of-arrival (samples from minute boundary)
- Signal strength (SNR)
- Doppler shift
- Tone characteristics

### Level 2: Derived Quantities
- Propagation delay residuals
- TEC estimates (from multi-frequency)
- Clock offset estimates (from multi-station)

### Level 3: Fused Products
- **D_clock:** Local clock offset from UTC
- **TEC:** Total Electron Content along each path
- **Ionospheric state:** Layer heights, gradients

## Exploiting the Duality

### For Timing (Fusion Mode)

The ionosphere is a **nuisance parameter** - we want to remove its effect:

1. **Multi-frequency cancellation:** Same station, different frequencies
   - Ionospheric delay ∝ 1/f²
   - Geometric delay is frequency-independent
   - Difference isolates ionospheric term

2. **Multi-station averaging:** Different stations, same frequency
   - Each path has independent ionospheric error
   - Averaging reduces ionospheric noise as 1/√N
   - Systematic clock error is common to all

3. **Physics constraints:**
   - Arrival sequence must match distance ordering
   - Cross-station emission times must agree
   - TEC must be physically reasonable (0-200 TECU)

### For Ionospheric Science (RTP Mode)

The timing is **known** - all variation is ionospheric signal:

1. **Absolute TEC:** From multi-frequency delay differences
   - Δτ = K·TEC·(1/f₁² - 1/f₂²)
   - No timing ambiguity when clock is known

2. **Spatial gradients:** From multi-station observations
   - Different paths sample different ionospheric regions
   - Tomographic reconstruction possible

3. **Temporal dynamics:**
   - Minute-by-minute TEC evolution
   - Traveling Ionospheric Disturbances (TIDs)
   - Solar flare effects (Sudden Ionospheric Disturbances)

## Implementation Mapping

| Concept | Implementation |
|---------|----------------|
| Timing Reconstruction | `multi_broadcast_fusion.py` |
| Physics-based Validation | `arrival_pattern_matrix.py` |
| Multi-constraint Validation | `timing_consistency_validator.py` |
| TEC Estimation | `tec_estimator.py` |
| Ionospheric Model | `ionospheric_model.py` (IRI-2020) |
| Clock Offset Tracking | `broadcast_kalman_filter.py` |
| Stability Analysis | `stability_analysis.py` (Allan deviation) |

## Quality Metrics

### Timing Quality (Fusion Mode)
- **D_clock uncertainty:** How well do we know UTC?
- **Allan deviation:** Stability over different time scales
- **Cross-validation error:** Agreement between stations

### Ionospheric Quality (RTP Mode)
- **TEC precision:** Uncertainty in electron content
- **Residual RMS:** Unexplained variation after model
- **Spatial coherence:** Agreement between nearby paths

## The Virtuous Cycle

Better timing enables better ionospheric measurement, which enables better ionospheric modeling, which enables better timing reconstruction:

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  ┌──────────────┐     ┌──────────────┐                 │
│  │   Timing     │────▶│  Ionospheric │                 │
│  │ Precision    │     │ Measurement  │                 │
│  └──────────────┘     └──────────────┘                 │
│         ▲                    │                         │
│         │                    ▼                         │
│  ┌──────────────┐     ┌──────────────┐                 │
│  │   Fusion     │◀────│  Ionospheric │                 │
│  │  Algorithm   │     │    Model     │                 │
│  └──────────────┘     └──────────────┘                 │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Design Principles

1. **Separate measurement from interpretation**
   - MetrologyEngine measures raw ToA
   - Fusion interprets for timing
   - TEC estimator interprets for ionosphere

2. **Validate against physics, not history**
   - ArrivalPatternMatrix provides expected arrivals
   - No dependence on previous calibration
   - Each minute starts fresh

3. **Exploit all constraints**
   - TimingConsistencyValidator checks multiple constraints
   - Redundancy improves confidence
   - Inconsistency reveals errors

4. **Preserve uncertainty**
   - Every measurement has uncertainty
   - Fusion propagates uncertainties
   - Quality flags indicate confidence

## Future Directions

1. **Adaptive weighting:** Weight broadcasts by current SNR and ionospheric stability
2. **Real-time TEC:** Feed TEC estimates back into arrival predictions
3. **Multi-receiver fusion:** Combine observations from multiple sites
4. **Machine learning:** Learn ionospheric patterns for better prediction
