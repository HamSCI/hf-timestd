# HF Time Standard - Metrology Reference

**Comprehensive guide to the metrological methodology used in hf-timestd for RTP-to-UTC calibration and time transfer.**

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** February 12, 2026 (v6.7.0)

---

## Overview

The HF Time Standard system derives UTC from shortwave time signal broadcasts (WWV, WWVH, CHU, BPM) received via Software Defined Radio. The system monitors **17 broadcasts** across **9 frequencies** from **4 stations**, serving a dual purpose:

1. **Timing Reconstruction (Fusion Mode):** Reconstruct local UTC precision from multiple broadcast time-of-arrival measurements
2. **Ionospheric Characterization (RTP Mode):** Measure ionospheric effects as residuals using external authoritative timing (GPS+PPS)

The fundamental challenge is establishing a precise relationship between the **RTP timestamp domain** (sample counts from the SDR) and **UTC** (Coordinated Universal Time).

This document describes the **Timing Bootstrap** methodology introduced in v6.3.0, which provides a robust, broadcast-validated approach to RTP-to-UTC calibration.

---

## The RTP-to-UTC Calibration Problem

### Background

The ka9q-radio SDR system delivers IQ samples via RTP (Real-time Transport Protocol). Each RTP packet contains:

- **RTP Timestamp**: A 32-bit sample counter (wraps every ~49.7 hours at 24 kHz)
- **Payload**: IQ samples at the configured sample rate (24,000 Hz)

The system clock provides wall-clock time, but this is **not directly tied** to the RTP timestamp domain. To convert detected tone arrivals (measured in RTP samples) to UTC, we need to establish the **RTP-to-UTC offset**:

```
UTC = RTP_timestamp / sample_rate + offset
```

### The Challenge

Several factors complicate this calibration:

1. **RTP timestamps are arbitrary** - They start at a random value when the SDR begins streaming
2. **System clock uncertainty** - Even with NTP, system time has ±10ms uncertainty
3. **Propagation delay** - Radio signals take 4-45ms to travel from transmitter to receiver
4. **Ionospheric variability** - Propagation delay varies with solar conditions, time of day, and frequency

### Previous Approaches (Pre-v6.3)

Earlier versions attempted to derive the RTP-to-UTC offset purely from tone detection:

1. Detect a tone in the audio
2. Assume the tone was transmitted at a known UTC second boundary
3. Calculate offset from the detected RTP timestamp

**Problem**: This approach suffered from a ~340ms systematic error because:
- Buffer boundaries were not precisely aligned to minute markers
- Per-second ticks were often detected instead of minute markers
- The system clock time associated with buffers was ambiguous (start vs. end)

---

## The Timing Bootstrap Methodology (v6.3.0)

### Design Philosophy

The new methodology separates two distinct problems:

1. **Offset Establishment**: Use buffer metadata (RTP + system time) for initial calibration
2. **Offset Validation**: Use broadcast signals to validate and refine the offset

This approach leverages the fact that:
- The system clock (via NTP) is accurate to ±10ms
- Buffer metadata provides a direct RTP↔system_time correspondence
- Broadcast signals provide physical validation of the offset

### Two-Phase Bootstrap

#### Phase 1: Metadata-Based Offset Establishment

When the Core Recorder writes each minute buffer, it records:

```json
{
  "start_rtp_timestamp": 164520840,
  "start_system_time": 1769306160.0665529,
  "sample_rate": 24000
}
```

The Timing Bootstrap uses this metadata directly:

```python
offset = system_time - (rtp_timestamp / sample_rate)
# offset ≈ 1769299305.03 seconds
```

**Key insight**: The buffer's `start_system_time` is captured at the moment the first sample arrives, providing a direct correspondence between the RTP and UTC domains.

#### Phase 2: Broadcast Signal Validation

Once the initial offset is established, tone detection validates that:

1. Detected tones arrive at expected times (within propagation delay tolerance)
2. Station identities match expected characteristics (tone frequency, schedule)
3. Multi-station ordering is geographically consistent

### State Machine

The bootstrap progresses through four states:

```
ACQUIRING → CORRELATING → TRACKING → LOCKED
```

| State | Description | Criteria to Advance |
|-------|-------------|---------------------|
| **ACQUIRING** | Initial state, no offset | First tone cluster detected |
| **CORRELATING** | Validating cluster consistency | Recurring clusters at 60s intervals |
| **TRACKING** | Clusters validated, awaiting time confirmation | NTP-based time confirmation |
| **LOCKED** | Time confirmed, offset stable | Continuous validation |

### Convergence Timeline (v6.4)

| Time | State | Uncertainty |
|------|-------|-------------|
| 0 min | ACQUIRING | Unknown |
| 1 min | CORRELATING | ±30ms |
| 2 min | TRACKING → LOCKED | ±5ms (NTP-confirmed) |
| 10+ min | LOCKED (refined) | <1ms (with BCD/FSK) |

### NTP-Based Time Confirmation (v6.4)

**Architecture Change (2026-01-29):** Bootstrap no longer requires BCD/FSK decode to reach LOCKED state.

**Previous Approach (v6.3):**
- Bootstrap waited for BCD/FSK decode to confirm UTC minute
- BCD decode fragile under HF fading (often 0/7 markers)
- Pipeline blocked indefinitely waiting for decode

**New Approach (v6.4):**
- Cluster detection finds minute markers (800ms tones at second 0)
- `wallclock_time` from GPSDO "steel ruler" identifies UTC minute directly
- Bootstrap transitions to LOCKED based on NTP confirmation (~2 minutes)
- BCD/FSK decode becomes OPTIONAL refinement for sub-second accuracy

**Implementation:**
```python
# confirm_time_from_ntp() in timing_bootstrap.py
# Uses NTP-derived wallclock from cluster detection
minute_boundary_wallclock = (best_wallclock // 60) * 60
utc_dt = datetime.utcfromtimestamp(minute_boundary_wallclock)
# Compute RTP-to-UTC offset from anchor_rtp and UTC minute
```

**Metrology Service Timing (v6.4):**
- Each raw buffer file contains `start_system_time` (NTP-derived wallclock)
- Metrology uses this directly instead of converting through bootstrap RTP reference
- Avoids SSRC mismatch issues (each channel has independent RTP epoch)

---

## Discriminating Features

To validate station identity and offset accuracy, the system uses multiple discriminating features:

### 1. Tone Frequency

Different stations use different minute marker frequencies:

| Station | Frequency | Duration |
|---------|-----------|----------|
| WWV | 1000 Hz | 800 ms |
| WWVH | 1200 Hz | 800 ms |
| CHU | 1000 Hz | 500 ms (1000 ms at top of hour) |
| BPM | 1000 Hz | 300 ms |

**Validation**: If a detection claims to be WWVH but the tone frequency is 1000 Hz, the detection is rejected.

### 2. Tone Schedule (Ground-Truth Minutes)

During certain minutes, only one station broadcasts 500/600 Hz tones:

**WWV-only minutes**: 1, 16, 17, 19  
**WWVH-only minutes**: 2, 43, 44, 45, 46, 47, 48, 49, 50, 51

**Validation**: If WWVH is detected at minute 16 with a 500/600 Hz tone, the detection is rejected (WWV-only minute).

### 3. Test Signal Minutes

| Minute | Station | Content |
|--------|---------|---------|
| 8 | WWV | Test signal (other station silent) |
| 44 | WWVH | Test signal (other station silent) |

### 4. Geographic Ordering

For receivers in continental North America:

- **WWV** (Fort Collins, Colorado) arrives first
- **WWVH** (Kauai, Hawaii) arrives 15-25ms later

**Validation**: If WWVH arrives before WWV on a shared frequency, the detection is rejected.

### 5. Unambiguous Channels

Some frequencies have only one transmitter:

| Channel | Station |
|---------|---------|
| CHU 3.33 MHz | CHU only |
| CHU 7.85 MHz | CHU only |
| CHU 14.67 MHz | CHU only |
| WWV 20 MHz | WWV only |
| WWV 25 MHz | WWV only |

Detections on these channels provide high-confidence station identification.

---

## Geographic Priors

The system computes expected propagation delays based on:

1. **Transmitter locations** (known precisely)
2. **Receiver location** (from configuration)
3. **Great circle distance**
4. **Ionospheric reflection height** (from real-time model or IRI-2020)
5. **Frequency-dependent ionospheric group delay** (1/f² scaling)
6. **Multi-hop path geometry** (1F, 2F, 3F modes)

### Transmitter Coordinates

| Station | Latitude | Longitude |
|---------|----------|-----------|
| WWV | 40.68°N | 105.04°W |
| WWVH | 21.99°N | 159.76°W |
| CHU | 45.30°N | 75.75°W |
| BPM | 34.95°N | 109.55°E |

### Expected Delays (Example: Columbia, MO)

| Station | Distance | Expected Delay | Range |
|---------|----------|----------------|-------|
| CHU | 1522 km | 5.8 ms | 4.7-8.8 ms |
| WWV | 1120 km | 4.3 ms | 3.4-6.4 ms |
| WWVH | 6600 km | 25.3 ms | 20.3-38.0 ms |
| BPM | 11504 km | 44.1 ms | 35.3-66.2 ms |

**Note:** These are representative single-hop values. The actual delay is frequency-dependent (lower frequencies experience more ionospheric group delay) and time-varying (diurnal ionospheric changes). The `HFPropagationModel` (v6.7) computes these dynamically. See the [Real-Time Ionospheric Propagation Model](#real-time-ionospheric-propagation-model-v670) section below.

---

## Real-Time Ionospheric Propagation Model (v6.7.0)

### Motivation

The previous propagation model used a static vacuum speed-of-light calculation with a fixed 15% ionospheric overhead (`delay = distance / c × 1.15`). This ignored:

1. **Frequency-dependent group delay** — ionospheric excess delay scales as 1/f², so 5 MHz has 4× more delay than 10 MHz
2. **Diurnal variation** — hmF2 varies from ~250 km (day) to ~350 km (night), changing path geometry
3. **Multi-hop propagation** — at night, lower frequencies take 2F or 3F paths with 100–300 ms additional delay
4. **Geomagnetic disturbances** — storm-time delays can change by 50–100% within minutes

### Architecture

The new model uses a three-tier data hierarchy:

```
HFPropagationModel.predict(station, frequency, utc_time)
    ├── IonoDataService.get_iono_params()
    │       ├── WAM-IPE grid (NOAA S3/NOMADS)     ← Tier 1: Real-time 3D model
    │       ├── GIRO ionosonde corrections          ← Tier 1.5: Ground-truth hmF2/foF2
    │       ├── IRI-2020 climatology                ← Tier 2: Monthly median model
    │       └── Parametric fallback                 ← Tier 3: Diurnal/seasonal formula
    ├── _evaluate_mode() × [1F, 2F, 3F, 1E]
    │       ├── Geometric feasibility check
    │       ├── MUF check (freq vs foF2/sec(i))
    │       ├── Spherical Earth path length
    │       └── Ionospheric group delay
    │               ├── Ne(h) numerical integration  ← When profile available
    │               └── TEC-based: 40.3·sTEC/(c·f²)  ← Fallback
    └── _estimate_uncertainty()
```

### Ionospheric Group Delay Physics

The excess group delay through the ionosphere is:

```
Δτ = (40.3 / c) × ∫ Ne(s) ds / f²  =  40.3 × sTEC / (c × f²)
```

where:
- `Ne(s)` is the electron density along the signal path (m⁻³)
- `sTEC` is the slant Total Electron Content (el/m²)
- `f` is the signal frequency (Hz)
- `c` is the speed of light (m/s)

For a vertical TEC of 20 TECU at 10 MHz, the excess delay is ~0.27 ms. At 5 MHz, it's ~1.07 ms (4× larger). At oblique incidence, the slant factor increases the effective TEC.

### Multi-Mode Predictions

For each (station, frequency) pair, the model evaluates four propagation modes:

| Mode | Description | Typical Distance |
|------|-------------|-----------------|
| **1F** | Single F-layer hop | < 3000 km |
| **2F** | Two F-layer hops | 3000–6000 km |
| **3F** | Three F-layer hops | > 6000 km |
| **1E** | Single E-layer hop (daytime) | < 2000 km |

Each mode is checked for:
- **Geometric feasibility** — can the signal reach the reflection point?
- **MUF constraint** — is the frequency below the Maximum Usable Frequency?
- **Minimum elevation** — below 3° is unreliable for single-hop

The `ArrivalMatrix` now contains both a primary arrival (backward-compatible) and a `multi_mode_arrivals` dict with all feasible modes.

### Adaptive Uncertainty

The uncertainty window adapts based on data source quality:

| Data Source | 3σ Uncertainty | Confidence |
|-------------|---------------|------------|
| WAM-IPE + GIRO | ±1.5 ms | 0.8 |
| WAM-IPE alone | ±3.0 ms | 0.6 |
| IRI-2020 | ±4.5 ms | 0.5 |
| Parametric fallback | ±9.0 ms | 0.2 |
| No model | ±15.0 ms | 0.0 |

The final window blends model uncertainty with tracked observational variance (exponential smoothing of residuals), using the tighter of the two, floored at ±5 ms (3σ).

### Self-Consistency Check

The model provides a self-consistency check using multi-frequency differential delay:

```
Δτ(f1,f2) = τ(f1) - τ(f2) = 40.3 × sTEC × (1/f1² - 1/f2²) / c
```

If the observed differential delay between two frequencies on the same station path disagrees with the model's predicted differential delay by more than 1 ms RMS, the model flags an inconsistency — indicating either a model error or a mode misidentification.

### Key Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/propagation_model.py` | `HFPropagationModel` — delay prediction, multi-mode, self-consistency |
| `src/hf_timestd/core/iono_data_service.py` | `IonoDataService` — WAM-IPE/GIRO fetch, cache, interpolation, fallback |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | `ArrivalPatternMatrix` — integrates model into arrival predictions |
| `tests/test_propagation_model.py` | 23 tests covering all components |

---

## Implementation Details

### Key Classes

#### `TimingBootstrap` (`timing_bootstrap.py`)

The main bootstrap state machine:

```python
class TimingBootstrap:
    def __init__(self, receiver_lat: float, receiver_lon: float):
        """Initialize with receiver coordinates for geographic priors."""
        
    def establish_offset_from_metadata(
        self,
        buffer_rtp_start: int,
        buffer_system_time: float,
        channel: str
    ) -> Optional[str]:
        """Establish or validate RTP-to-UTC offset from buffer metadata."""
        
    def validate_station_by_tone_frequency(
        self,
        detected_station: str,
        tone_frequency_hz: float
    ) -> Tuple[bool, float]:
        """Validate station identity by minute marker tone frequency."""
        
    def validate_station_by_schedule(
        self,
        detected_station: str,
        minute_of_hour: int,
        has_500_600_hz_tone: bool
    ) -> Tuple[bool, float]:
        """Validate station identity using the 500/600 Hz tone schedule."""
        
    def validate_wwv_wwvh_ordering(
        self,
        wwv_rtp: int,
        wwvh_rtp: int,
        frequency_khz: int
    ) -> Tuple[bool, float]:
        """Validate that WWVH arrives after WWV on shared frequencies."""
```

### Constants

```python
# Ground-truth minutes
WWV_ONLY_TONE_MINUTES = {1, 16, 17, 19}
WWVH_ONLY_TONE_MINUTES = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}

# Test signal minutes
WWV_TEST_SIGNAL_MINUTE = 8
WWVH_TEST_SIGNAL_MINUTE = 44

# Tone characteristics
TONE_CHARACTERISTICS = {
    'WWV': {'frequency_hz': 1000, 'duration_ms': 800},
    'WWVH': {'frequency_hz': 1200, 'duration_ms': 800},
    'CHU': {'frequency_hz': 1000, 'duration_ms': 500},
    'BPM': {'frequency_hz': 1000, 'duration_ms': 300},
}

# Unambiguous channels
UNAMBIGUOUS_CHANNELS = {
    'CHU_3330': 'CHU',
    'CHU_7850': 'CHU',
    'CHU_14670': 'CHU',
    'WWV_20000': 'WWV',
    'WWV_25000': 'WWV',
}
```

---

## Uncertainty Analysis

### Sources of Uncertainty

| Source | Magnitude | Notes |
|--------|-----------|-------|
| NTP synchronization | ±10 ms | System clock accuracy |
| Buffer timestamp jitter | ±1 ms | Kernel scheduling |
| Tone detection | ±0.1 ms | Cross-correlation precision |
| Propagation delay | ±5-15 ms | Ionospheric variability |

### Combined Uncertainty

After LOCKED state is achieved:

- **Offset uncertainty**: <0.1 ms (metadata consistency)
- **Absolute UTC uncertainty**: ±10 ms (limited by NTP)
- **Relative timing precision**: ±0.1 ms (tone detection)

### Improving Absolute Accuracy

To achieve better than ±10 ms absolute accuracy:

1. **GNSS disciplined clock** - Provides ±1 μs system time
2. **Multi-frequency TEC correction** - Removes ionospheric delay uncertainty
3. **Multi-station fusion** - Geometric solution for UTC origin

---

## Physics-Based Validation (v6.5.0)

The system validates detections against physics predictions rather than historical data:

### ArrivalPatternMatrix

Pre-computes expected arrival times for all 17 broadcasts based on:
- **Geography:** Receiver and station locations (fixed)
- **Frequency:** Affects ionospheric reflection height
- **UTC time:** Affects ionospheric conditions via IRI-2020 model

**Key Principle:** Validate against PHYSICS, not HISTORY. Each minute starts fresh from physics predictions.

### Multi-Constraint Validation

The `TimingConsistencyValidator` exploits multiple timing constraints:

**Intra-Minute Constraints:**
1. **Arrival Sequence:** Stations at different distances must arrive in order
2. **Cross-Station Consistency:** All stations transmit at UTC second 0
3. **Cross-Frequency Dispersion:** Ionospheric delay follows 1/f² law

**Inter-Minute Constraints:**
1. **Sample Interval Stability:** Consistent 1,440,000 samples per minute
2. **Arrival Time Stability:** Gradual changes, not jumps

### Real-Time TEC Feedback (v6.5.0)

Measured TEC from multi-frequency observations feeds back to refine arrival predictions:

```
τ_correction = K × TEC_measured / f²
where K ≈ 0.1345 ms/TECU/MHz²
```

This creates a virtuous cycle: better timing → better TEC measurement → better ionospheric model → better timing.

---

## Operational Considerations

### Startup Behavior

1. Service starts with bootstrap in ACQUIRING state
2. First buffer metadata establishes initial offset
3. Subsequent buffers validate consistency
4. After 10 minutes, system reaches LOCKED state

### Handling Discontinuities

If the RTP stream restarts (e.g., radiod restart):

1. Bootstrap detects inconsistent metadata (>100ms deviation)
2. System retreats to ACQUIRING state
3. Re-establishes offset from new metadata
4. Returns to LOCKED state within 10 minutes

### Monitoring

Check bootstrap status in logs:

```bash
grep "BOOTSTRAP" /var/log/hf-timestd/phase2-*.log
```

Expected progression:
```
[BOOTSTRAP] Offset from metadata: 1769299305.031553s
[BOOTSTRAP] Metadata offset validated → TRACKING
[BOOTSTRAP] Metadata offset LOCKED: 1769299305.031553s (uncertainty=0.0ms)
```

---

## References

### Standards

- **ITU-R TF.460-6**: Standard-frequency and time-signal emissions
- **NIST Special Publication 432**: NIST Time and Frequency Services

### Related Documentation

- `TECHNICAL_REFERENCE.md` - System architecture and algorithms
- `ARCHITECTURE.md` - Design philosophy
- `docs/METROLOGIST_DESCRIPTION.md` - Detailed metrological analysis

---

## FUSION Mode Accuracy Analysis

### Motivating Rationale

The system serves a dual purpose. In **RTP mode** (with GPSDO), the authoritative timing comes from GPS+PPS via radiod, and the metrology pipeline functions as a testbed for refining detection algorithms, calibration models, and ionospheric corrections against a known-good reference. This refinement directly serves the second purpose: **FUSION mode**, where GPS, GPSDO, or even network access may be unavailable, and the system must derive UTC solely from HF time standard receptions.

FUSION mode addresses real operational scenarios:
- **Remote/off-grid installations** without GPS coverage (deep valleys, underground, indoor)
- **Disaster/emergency situations** where GPS and network infrastructure are disrupted
- **Intentional GPS denial** (jamming, spoofing) in contested environments
- **Backup timing** when primary GNSS disciplining fails
- **Scientific stations** in locations where only HF propagation is available

The accuracy achieved in FUSION mode depends on the error budget of the entire chain from transmitter to receiver.

### Error Budget

In FUSION mode, the timing chain is:

```
UTC(NIST/NRC) → HF transmitter → Ionosphere → Receiver → ADC → Detection → D_clock
```

Each layer contributes uncertainty:

| Source | Magnitude | Notes |
|--------|-----------|-------|
| **Transmitter timing** | < 1 µs | WWV/WWVH/CHU traceable to UTC(NIST)/UTC(NRC) |
| **Ionospheric propagation** | 3-15 ms variation | Dominant error. 1-hop F2 at 5-15 MHz. Diurnal, seasonal, solar cycle |
| **Multipath/mode structure** | 1-5 ms | Multiple ionospheric modes (1F2, 2F2, E-layer) arrive at different times |
| **ADC clock accuracy** | 0.1-10 ppm | Typical TCXO: 1-2 ppm. Cheap crystal: 10-50 ppm |
| **ADC clock stability** | 0.01-1 ppm/hour drift | TCXO: ~0.1 ppm/°C. Oven-controlled: 0.01 ppm |
| **Matched filter detection** | 0.1-0.5 ms | Sub-sample interpolation gives ~1/10 sample precision at 12 kHz |
| **NTP initial sync** | 1-50 ms | Depends on network path. Typical LAN: 1-5 ms |

### Operational Scenarios

#### Scenario 1: Good SDR (TCXO, 1-2 ppm) + NTP Available

*Typical: RTL-SDR V4, Airspy, most modern SDRs*

- NTP provides initial minute identification (±5 ms) — enough to find the first tone
- FUSION bootstrap: 2-3 minutes to lock using multi-station correlation
- **Steady-state accuracy:**
  - Single station: **±5-15 ms** (ionospheric variation dominates)
  - Multi-station fusion (WWV + WWVH + CHU): **±2-5 ms** after calibration convergence
- **Drift:** At 2 ppm, the oscillator drifts ~7.2 ms/hour. The Kalman tracks this easily. Over 24 hours without any external reference, accumulated drift would be ~170 ms — but continuous HF reception corrects this every minute
- **Allan deviation:** Expect σ_y(60s) ≈ 10⁻⁷ to 10⁻⁸ from HF measurements alone

#### Scenario 2: Cheap SDR (crystal, 10-50 ppm) + NTP Available

*Typical: RTL-SDR V3, generic dongles*

- NTP still provides minute identification
- FUSION bootstrap: 5-10 minutes due to wider search window
- **Steady-state:** Same ionospheric limit once locked — **±5-15 ms single station, ±2-5 ms multi-station**
- **Risk:** Large frequency error means the matched filter's tone frequency is slightly wrong. At 50 ppm on a 10 MHz carrier, that's 500 Hz offset — the matched filter bandwidth (~100 Hz) would miss it
- **Mitigation:** The metrology engine searches across frequency bins. The `ARRIVAL_TOLERANCE_MS = 100ms` window handles timing uncertainty. The real challenge is frequency, not time

#### Scenario 3: Good SDR + No Network (Island/Remote/Disaster)

*The hardest case — no NTP, no GPS, cold start*

- **Minute identification:** Must come from HF signal decoding alone. CHU FSK decoder identifies the minute from BCD time code. WWV identifies the minute from the 100 Hz subcarrier BCD. This takes 1-2 minutes of clean reception
- **Initial UTC uncertainty:** ±200 ms (the FUSION `UNLOCKED` search window)
- **Convergence:** PROVISIONAL lock in 2-3 minutes, REFINED in 10+ minutes
- **Steady-state:** Same as Scenario 1 once locked — **±2-5 ms multi-station**
- **Risk:** If the oscillator has drifted significantly since last calibration (hours/days off), the initial search window may not be wide enough. The `ArrivalPatternMatrix` physics-based predictions help here — they predict where the tone should be regardless of clock state

### Observed Performance (RTP Mode Baseline)

From production data on the development station (RTP mode, GPSDO-locked):

| Metric | Observed Value | Significance |
|--------|---------------|--------------|
| D_clock raw range | +27 to +48 ms cycle-to-cycle | Ionospheric variation |
| L1-L2 difference | 0.2 to 9.3 ms | Physics model correction magnitude |
| Per-cycle uncertainty | 6-10 ms | Ionospheric floor per measurement |
| Cross-station disagreement | ~65 ms | Multi-path mode mixing (grade D) |
| Hardware calibration bias | ~40 ms mean offset | Matched filter delay + ADC pipeline |

The ~40 ms mean offset is the hardware calibration bias (matched filter delay, ADC pipeline). In FUSION mode, this same calibration is learned from the HF signals themselves, just more slowly.

The 6-10 ms per-cycle uncertainty is the ionospheric floor — no amount of better oscillator improves this. Multi-station averaging over 10+ minutes brings this down to ~2-3 ms.

### Expected FUSION Mode Accuracy Summary

| Configuration | Expected Accuracy | Time to Lock |
|--------------|-------------------|--------------|
| Multi-station + TCXO + NTP | **±2-5 ms** steady-state | 2-3 min |
| Multi-station + TCXO, no network | **±2-5 ms** steady-state | 5-10 min |
| Single station + TCXO | **±5-15 ms** | 2-3 min |
| Multi-station + cheap crystal | **±2-5 ms** (if freq lock works) | 5-10 min |

The ionosphere is the dominant error in all cases. Oscillator quality affects **time to lock** and **holdover during outages**, but not steady-state accuracy once locked. The multi-station fusion architecture averages out ionospheric path differences — that is where the real accuracy gain comes from.

### Dual Chrony Feed Architecture (v6.5.1)

The fusion service provides two independent timing feeds to chrony via shared memory:

| Feed | SHM Unit | Source | Purpose |
|------|----------|--------|---------|
| **TSL1** | 0 | L1 Kalman (geometric fallback) | Raw metrology fusion — no ionospheric model |
| **TSL2** | 1 | L2 Kalman (physics model) | Full ionospheric correction via propagation model |

Each feed has its own independent Kalman filter state, so chrony receives genuinely different estimates. TSL2 should show lower jitter and better accuracy as the ionospheric correction model removes systematic propagation biases that TSL1 cannot account for.

Chrony can combine, select, or compare these feeds using its standard source selection algorithm. In FUSION mode, TSL2 is expected to be the primary timing source.

### Remaining Engineering Work

- **Bootstrap hardening:** Wider initial search window, CHU/WWV time code decoding for minute identification without NTP
- **Frequency tracking:** For cheap oscillators with large ppm offset, adaptive matched filter center frequency
- **Holdover model:** How long the system can coast on Kalman state when all HF signals fade (e.g., during a solar storm or D-layer absorption event)

---

## Timing Authority Levels: Achievable Uncertainty Analysis

The system's achievable timing uncertainty depends critically on the hardware configuration and timing reference chain. We define six levels (L1–L6) representing progressively better timing infrastructure, and analyze the error budget at each.

### Error Source Taxonomy

Every D_clock measurement passes through a chain of error sources. Each source contributes independently (RSS combination). The sources are:

| # | Error Source | Symbol | Description |
|---|-------------|--------|-------------|
| 1 | **Transmitter timing** | σ_tx | UTC(NIST/NRC) to RF emission |
| 2 | **Ionospheric propagation** | σ_iono | Path delay variation (dominant for HF) |
| 3 | **Multipath/mode structure** | σ_mode | Multiple ionospheric modes arriving at different times |
| 4 | **Detection algorithm** | σ_det | Matched filter + onset detection + sub-sample interpolation |
| 5 | **ADC sample clock** | σ_adc | Frequency accuracy and stability of the sampling clock |
| 6 | **RTP-to-UTC mapping** | σ_rtp | Mapping RTP timestamps to wall-clock UTC |
| 7 | **Timing authority** | σ_auth | How well the system knows "what time is it now" |
| 8 | **Multi-station fusion** | σ_fusion | Improvement from combining independent measurements |

Sources 1–4 are **irreducible** — they depend on physics and algorithm quality, not hardware configuration. Sources 5–7 are **configuration-dependent** — they change dramatically across levels. Source 8 is a **reduction factor** that improves with more independent measurements.

### Irreducible Error Sources (All Levels)

**σ_tx < 0.001 ms (1 µs):** WWV/WWVH are traceable to UTC(NIST) with < 1 µs uncertainty. CHU is traceable to UTC(NRC) with similar precision. Negligible.

**σ_iono = 3–15 ms per measurement:** The dominant error. One-hop F2-layer propagation at 5–15 MHz introduces path delays of 5–20 ms that vary with:
- Time of day (diurnal): ±5 ms swing
- Season: ±2 ms
- Solar cycle (F10.7): ±3 ms
- Geomagnetic activity (Kp): ±5 ms during storms
- Frequency: Higher frequencies → lower layer heights → shorter paths → less delay

**σ_mode = 1–5 ms:** Multiple propagation modes (1F2, 2F2, 1E, ground wave at close range) arrive at different times. The matched filter detects the strongest mode, which may not be the first arrival. Mode mixing creates a bimodal or multimodal distribution of arrival times.

**σ_det ≈ 0.05 ms (50 µs):** The two-stage detection algorithm achieves:
- Stage 1 (matched filter): Detects tone presence with high SNR gain (√N processing gain)
- Stage 2 (onset detection): Bandpass filter → energy envelope → rising edge → sub-sample interpolation
- At 20 kHz sample rate: 1 sample = 50 µs. Sub-sample interpolation achieves ~5 µs (1/10 sample)
- Cramér-Rao lower bound at typical SNR (15–25 dB): 0.036 ms

**σ_det is negligible compared to σ_iono.** The detection algorithm is not the limiting factor at any level.

### Level Definitions and Error Budgets

---

#### L6: GPSDO-Governed RX888 + PPS Injection in HF Stream

*Not yet implemented. Represents the theoretical best case.*

**Configuration:** The GPSDO's PPS signal is injected directly into the RX888's ADC input (via a combiner or dedicated channel). Every sample is directly referenced to UTC via the PPS edge embedded in the digitized stream.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | < 0.001 ms | GPSDO-locked 122.88 MHz clock, < 1 ppb |
| σ_rtp | **0** | PPS is IN the sample stream — no RTP mapping needed |
| σ_auth | **0** | PPS edge IS the timing reference |
| σ_det | 0.005 ms | PPS edge detection is trivial (sharp rising edge) |
| σ_iono | 3–15 ms | Unchanged — but now perfectly measurable |

**Achievable single-measurement uncertainty:** σ_iono dominates = **3–15 ms**

**But the key difference:** At L6, the ionospheric delay is a *measurement*, not an *error*. The system knows UTC to < 1 µs from the embedded PPS. Every D_clock measurement is a direct observation of the ionospheric path delay with ~5 µs precision. This is the **ionospheric characterization mode** — the system becomes a precision ionosonde.

**Multi-station fusion (10 min, 4 stations):** The ionospheric component averages down as √N. With ~75 independent measurements across 4 stations: σ_fused ≈ σ_iono / √75 ≈ **0.5–1.7 ms** for the ionospheric residual. But the *clock* uncertainty is < 0.001 ms — the residual is purely ionospheric science, not timing error.

**Grade A achievable?** The clock is already at Grade A (< 0.001 ms). The "uncertainty" reported would be the ionospheric measurement scatter, not clock error. **Grade A is trivially achieved for timing; the uncertainty metric needs reinterpretation as ionospheric measurement quality.**

---

#### L5: GPSDO-Governed RX888 + GPS+PPS Direct to Radiod Machine

*Current production configuration on bee1.*

**Configuration:** GPSDO provides 10 MHz reference to RX888 (sample clock locked). GPS receiver provides PPS + NMEA directly to the radiod host via serial/USB. Radiod uses PPS to discipline its RTP timestamp mapping.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | < 0.001 ms | GPSDO-locked 122.88 MHz, < 1 ppb stability |
| σ_rtp | 0.01–0.05 ms | RTP timestamp granularity + radiod's PPS-to-RTP alignment |
| σ_auth | 0.001–0.01 ms | GPS+PPS on same machine, kernel PPS discipline |
| σ_det | 0.05 ms | Standard detection chain |
| σ_iono | 3–15 ms | Unchanged |

**Achievable single-measurement uncertainty:** σ_iono dominates = **3–15 ms**

**The σ_rtp term:** Radiod maps RTP sequence numbers to wall-clock time using the system clock. With PPS on the same machine, the system clock is disciplined to < 10 µs. But the RTP-to-sample mapping has quantization: RTP timestamps increment in units of 1/sample_rate. At 20 kHz output rate, 1 RTP tick = 50 µs. The mapping uncertainty is ~1 RTP tick = 0.05 ms.

**Multi-station fusion (10 min, 4 stations):** σ_fused ≈ **0.5–1.7 ms** (same as L6 for the ionospheric component). The timing authority adds ~0.05 ms in quadrature — negligible.

**Observed performance:** On bee1, we see D_clock uncertainty of 4–7 ms per cycle, with Kalman-converged steady state of ~2–4 ms. Cross-station disagreement of 18–36 ms (dominated by BPM's long path). Excluding BPM: ~5–10 ms inter-station.

**Grade A achievable?** In principle, with long averaging and BPM excluded, the fused uncertainty could approach 0.5 ms. **Marginally achievable under ideal conditions** (nighttime, stable ionosphere, 30+ min averaging, BPM excluded). In practice, Grade B (< 1.0 ms) is the realistic steady-state target.

---

#### L4: GPSDO-Governed RX888 + GPS+PPS via LAN

**Configuration:** GPSDO provides 10 MHz to RX888 (sample clock locked). GPS+PPS is on a *separate* machine, providing time to the radiod host via PTP (IEEE 1588) or NTP over LAN.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | < 0.001 ms | GPSDO-locked, same as L5 |
| σ_rtp | 0.05–0.5 ms | RTP mapping now depends on LAN-disciplined clock |
| σ_auth | 0.01–1.0 ms | PTP: 0.01–0.1 ms. NTP over LAN: 0.1–1.0 ms |
| σ_det | 0.05 ms | Unchanged |
| σ_iono | 3–15 ms | Unchanged |

**Key difference from L5:** The timing authority is no longer on the same machine as radiod. The LAN introduces asymmetric delay jitter. PTP (with hardware timestamping) achieves ~10–100 µs. NTP over a quiet LAN achieves ~0.1–1 ms.

**Achievable single-measurement:** Still σ_iono dominated = **3–15 ms**

**Multi-station fusion:** σ_fused ≈ **0.5–2 ms**. The σ_auth term (0.01–1 ms) starts to matter in the fused result. With PTP, it's negligible. With NTP, it adds ~0.5 ms in quadrature to the fused uncertainty.

**Grade A achievable?** With PTP: similar to L5, marginally achievable. With NTP: **No** — the NTP jitter floor (~0.5 ms) prevents reaching < 0.5 ms even with perfect ionospheric averaging.

---

#### L3: GPSDO-Governed RX888 + NTP via LAN

**Configuration:** GPSDO provides 10 MHz to RX888 (sample clock locked). No local GPS — timing authority is NTP from upstream servers via LAN/WAN.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | < 0.001 ms | GPSDO-locked, same as L5 |
| σ_rtp | 0.5–5 ms | RTP mapping depends on NTP-disciplined clock |
| σ_auth | 1–10 ms | NTP over WAN: 1–10 ms. LAN to stratum-1: 0.5–2 ms |
| σ_det | 0.05 ms | Unchanged |
| σ_iono | 3–15 ms | Unchanged |

**Key insight:** The GPSDO still provides a perfect "ruler" (stable sample clock), but the ruler's zero-point is set by NTP. The sample-to-sample timing is sub-ppb, but the absolute time reference has 1–10 ms uncertainty.

**This is the "Steel Ruler" scenario:** The ruler is rigid but floating. The HF measurements can *improve* the NTP-derived zero-point because the ionospheric average converges faster than NTP wanders.

**Achievable single-measurement:** max(σ_iono, σ_auth) = **3–15 ms**

**Multi-station fusion:** σ_fused ≈ **1–3 ms**. The σ_auth term (1–10 ms) is comparable to σ_iono, so both contribute. The Kalman filter tracks the NTP offset as a slowly-varying bias.

**Grade A achievable?** **No.** NTP jitter prevents it. Grade B (< 1 ms) is possible with a good stratum-1 NTP source on LAN and long averaging.

---

#### L2: RX888 (No GPSDO) + NTP via LAN

**Configuration:** RX888 runs on its internal TCXO (1–2 ppm typical). No GPSDO. Timing authority is NTP.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | 0.05–0.5 ms/min | TCXO drift: 1–2 ppm = 0.06–0.12 ms/min accumulated |
| σ_rtp | 0.5–5 ms | NTP-disciplined clock, same as L3 |
| σ_auth | 1–10 ms | NTP, same as L3 |
| σ_det | 0.05–0.5 ms | Degraded by frequency offset (tone not at expected freq) |
| σ_iono | 3–15 ms | Unchanged |

**Key difference from L3:** The sample clock drifts. At 2 ppm, the clock accumulates 0.12 ms/min of drift. Over a 10-minute fusion window, that's 1.2 ms of sample clock wander *within the window*. The Kalman filter must track both the ionospheric variation AND the oscillator drift simultaneously.

**σ_det degradation:** At 2 ppm on a 10 MHz carrier, the received tone is offset by 20 Hz from the expected frequency. The matched filter bandwidth (~50 Hz) still captures this. At 50 ppm (cheap crystal), the offset is 500 Hz — outside the filter bandwidth, requiring frequency search.

**Achievable single-measurement:** max(σ_iono, σ_auth) = **3–15 ms** (same floor as L3)

**Multi-station fusion:** σ_fused ≈ **2–5 ms**. The oscillator drift adds a correlated error across all measurements in a window, limiting the √N improvement. The Kalman's drift state must converge before the ionospheric averaging becomes effective.

**Time to lock:** 2–5 minutes (TCXO). The Kalman needs to separate oscillator drift from ionospheric variation, which requires multiple cycles.

**Grade A achievable?** **No.** Grade C (< 2 ms) is the realistic target. Grade B possible with a good TCXO and long averaging.

---

#### L1: RX888 (No GPSDO) + No NTP

**Configuration:** RX888 on internal oscillator. No external timing reference at all. Cold start — the system must determine UTC from HF signals alone.

**Error budget:**

| Source | Value | Notes |
|--------|-------|-------|
| σ_adc | 0.05–0.5 ms/min | Same TCXO drift as L2 |
| σ_rtp | N/A | No external clock to map against |
| σ_auth | **200+ ms initially** | Must identify the minute from HF signal decoding |
| σ_det | 0.05–0.5 ms | Same as L2 |
| σ_iono | 3–15 ms | Unchanged |

**The bootstrap problem:** Without NTP, the system doesn't know what minute it is. The CHU FSK decoder can identify the minute from the BCD time code (takes 1–2 minutes of clean reception). WWV's 100 Hz subcarrier BCD provides the same. Until minute identification succeeds, σ_auth = ±200 ms (the UNLOCKED search window).

**After bootstrap:** Once the minute is identified, σ_auth drops to ~σ_iono (the HF measurements become the authority). The system is now self-referential — the same HF signals provide both the timing reference and the measurements.

**Achievable single-measurement:** σ_iono = **3–15 ms** (after bootstrap)

**Multi-station fusion:** σ_fused ≈ **2–5 ms** (same as L2 after convergence). The oscillator drift is the same problem as L2.

**Holdover:** If all HF signals fade (solar storm, D-layer absorption), the system coasts on the Kalman state. With a TCXO at 2 ppm, drift accumulates at 0.12 ms/min = 7.2 ms/hour. After 1 hour of holdover, uncertainty grows to ~10 ms. After 24 hours: ~170 ms.

**Grade A achievable?** **No.** Same ceiling as L2 — Grade C is the realistic target.

---

### Summary Table

| Level | Sample Clock | Timing Authority | Single Meas. | Fused (10 min) | Fused (1 hr) | Best Grade | Primary Limiter |
|-------|-------------|-----------------|-------------|----------------|--------------|------------|-----------------|
| **L6** | GPSDO (< 1 ppb) | PPS in stream | 3–15 ms | 0.3–1.0 ms | 0.1–0.5 ms | **A** | Ionospheric scatter |
| **L5** | GPSDO (< 1 ppb) | GPS+PPS local | 3–15 ms | 0.5–1.7 ms | 0.2–0.7 ms | **A–B** | RTP mapping + iono |
| **L4** | GPSDO (< 1 ppb) | PTP/NTP via LAN | 3–15 ms | 0.5–2.0 ms | 0.3–1.0 ms | **B** | LAN timing jitter |
| **L3** | GPSDO (< 1 ppb) | NTP via WAN | 3–15 ms | 1–3 ms | 0.5–1.5 ms | **B–C** | NTP wander |
| **L2** | TCXO (1–2 ppm) | NTP via LAN | 3–15 ms | 2–5 ms | 1–3 ms | **C** | Oscillator drift + NTP |
| **L1** | TCXO (1–2 ppm) | HF self-derived | 3–15 ms* | 2–5 ms | 1–3 ms | **C** | Oscillator drift + bootstrap |

*L1 single measurement: 200+ ms during bootstrap, 3–15 ms after lock.

### Key Insights

1. **The ionosphere is always the dominant single-measurement error** (3–15 ms). No hardware improvement changes this. The only way to reduce it is multi-station fusion over time (√N averaging).

2. **Grade A (< 0.5 ms) requires L5 or L6** — you need sub-µs timing authority AND long averaging to push the ionospheric scatter below 0.5 ms. L6 (PPS in stream) is the cleanest path because it eliminates the RTP mapping uncertainty entirely.

3. **The GPSDO matters for the ruler, not the zero-point.** At L3–L6, the GPSDO ensures the sample clock doesn't drift, so measurements within a fusion window are coherent. Without it (L1–L2), the Kalman must track oscillator drift simultaneously with ionospheric variation, limiting the effective averaging depth.

4. **NTP is the ceiling for L2–L4.** NTP jitter (0.5–10 ms) sets a floor on the timing authority that multi-station fusion cannot average below. PTP (L4 with hardware timestamping) breaks through this floor.

5. **L1 and L2 converge to the same steady-state** after bootstrap. The NTP in L2 only helps with faster initial lock — once the Kalman converges, the HF measurements dominate the timing estimate in both cases.

6. **The current system operates at L5** and achieves 2–5 ms fused uncertainty. The gap to Grade A is primarily the ionospheric averaging depth — longer windows and BPM exclusion would help, but the RTP mapping uncertainty (~0.05 ms) may also need attention for the final push below 0.5 ms.

### Implications for Grade Thresholds

The current grade thresholds were designed with L6 in mind (the aspirational target). For the current L5 configuration:

| Grade | Current Threshold | Achievable at L5? | Recommended L5 Threshold |
|-------|-------------------|-------------------|--------------------------|
| **A** | < 0.5 ms | Marginal (long avg) | < 1.0 ms |
| **B** | < 1.0 ms | Yes (converged) | < 2.0 ms |
| **C** | < 2.0 ms | Yes (typical) | < 5.0 ms |
| **D** | ≥ 2.0 ms | Default | ≥ 5.0 ms |

A level-aware grading system could report grades relative to the configured timing authority level, making the grades meaningful at every configuration.

---

## Station Priority Policy (v6.5.0)

Not all broadcasts are weighted equally in the fusion algorithm:

### Primary Timing Anchors

| Station | Role | Rationale |
|---------|------|----------|
| **CHU** | Reference | Unique frequencies (no discrimination needed), FSK-verified timing |
| **WWV** | Primary | Closest station, best SNR, well-characterized |
| **WWVH** | Primary | Independent path, good for cross-validation |

### Secondary/Scientific Sources

| Station | Role | Rationale |
|---------|------|----------|
| **BPM** | Scientific | Very long path (~11,000 km), high ionospheric variability, weight reduced to 30% |

BPM is maintained for scientific interest (trans-Pacific ionospheric probing) but its high uncertainty makes it unsuitable as a primary timing anchor.

---

**Version**: 6.7.0  
**Last Updated**: February 12, 2026
