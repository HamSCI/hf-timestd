# Timing Authority Architecture

**Status**: LIVING DOCUMENTATION  
**Date**: 2026-01-31  
**Version**: 2.0

> **Living Documentation**: This document is directly connected to the running system.
> Claims are backed by live evidence from logs and data. Directives like 
> `<!-- LOGS: source | filter: "pattern" -->` fetch real-time proof from this installation.

## Overview

This document describes a layered timing architecture that cleanly separates **recording** (immutable facts) from **interpretation** (timing and physics analysis). The key insight is that raw data recording needs only RTP timestamps—all timing interpretation can be done post-hoc.

**Live Validation Dashboard**: [/timing-validation](/timing-validation) — Real-time comparison of HF fusion vs GPS ground truth.

The system supports two timing authority modes:
- **RTP Authority (L4/L5)**: Trust radiod's GPS+PPS-derived timing
- **Fusion Authority (L3/L2/L1)**: Use HF signal analysis to establish timing

Both modes record the same data; they differ only in how that data is interpreted.

## What RTP Timestamps Mean in Each Mode

The fundamental distinction between the two modes is **what the RTP timestamps tell you**:

### RTP Mode: "These samples were measured at XXXX UTC"

In RTP mode, the radiod machine has both GPS+PPS time governance **and** alignment of
sampling. The GPSDO disciplines the ADC clock; GPS+PPS disciplines the system clock via
chrony. radiod's assignment of a timestamp to the RTP stream accurately means:

> "These samples were measured at XXXX UTC."

The RTP-to-UTC mapping is authoritative. There is **no timing offset to discover** — we
already have it. The metrology engine measures signals at **known** times and goes
straight to physics (ionospheric delay, TEC, propagation mode).

The only plumbing needed is a one-time **counter-space reconciliation** in the
core-recorder: radiod's `RTP_TIMESNAP` is based on the ADC input sample counter, but
packet RTP timestamps come from the filter output sample counter. These differ by the
filter pipeline depth — a fixed constant for the session, not a timing offset to
discover. The core-recorder measures this once at startup using `time.time()` as a
cross-check and adjusts `RTP_TIMESNAP` to the packet counter space.

### FUSION Mode: "These samples were taken at the same time XXXX"

In FUSION mode (NTP-only, no GPS+PPS), all we can say about the RTP timestamps is:

> "These samples were taken at the same time XXXX."

The RTP timestamps are **coherent** (all channels share the same GPSDO-disciplined
sample clock) but **not anchored to UTC**. The system must figure out the offset to UTC
by applying everything it knows about the signals, given the assumption that they all
align accurately with UTC at the time of emission.

This is a fundamentally different problem: the metrology engine must **discover** UTC
through signal analysis (BCD decode, FSK time codes, tone correlation, multi-station
fusion) before it can do physics. The `FusionTimingState` manages a progressive lock:
`UNLOCKED → PROVISIONAL → REFINED`, widening or narrowing search windows as confidence
grows.

### Separation of Concerns

| Concern | RTP Mode | FUSION Mode |
|---------|----------|-------------|
| **Timing authority** | GPSDO + GPS+PPS via radiod | HF signal analysis |
| **UTC known at startup?** | Yes | No — must be discovered |
| **Core-recorder's job** | Counter-space reconciliation, raw IQ storage | Same |
| **Metrology's job** | Measure physics at known times | Discover UTC, then measure physics |
| **Search windows** | Narrow (±10ms, ionospheric uncertainty only) | Wide initially (±200ms), narrow after lock |
| **FusionTimingState** | Not instantiated | Manages lock progression |

The core-recorder's role is identical in both modes: packet handling, RTP metadata,
buffer alignment, and raw IQ storage. It never does signal analysis. The distinction
between modes lives entirely in the metrology layer.

---

## Background

### The RTP Timestamp Foundation

All scenarios assume a **GPSDO-disciplined RX888 ADC**. The 27 MHz clock from the Leo Bodnar (or similar) GPSDO provides:
- Sub-ppb frequency stability
- GPS-traceable sample clock
- Stable phase relationship for coherent processing

RTP timestamps from radiod are **sample counts from radiod startup**, not absolute time. They are GPSDO-stable but require a mapping to convert to UTC.

### Existing Infrastructure

**radiod** (ka9q-radio) already provides RTP-to-UTC mapping via status packets:
- `GPS_TIME`: Current wall clock time (ns since GPS epoch) from `CLOCK_REALTIME`
- `RTP_TIMESNAP`: Current RTP timestamp at that moment

**ka9q-python** already implements the conversion:
```python
def rtp_to_wallclock(rtp_timestamp: int, channel: ChannelInfo) -> Optional[float]:
    """Convert RTP timestamp to Unix wall-clock time"""
    sender_time = channel.gps_time + BILLION * (GPS_UTC_OFFSET - GPS_LEAP_SECONDS)
    rtp_delta = int((rtp_timestamp - channel.rtp_timesnap) & 0xFFFFFFFF)
    if rtp_delta > 0x7FFFFFFF:
        rtp_delta -= 0x100000000
    time_offset = BILLION * rtp_delta // channel.sample_rate
    return (sender_time + time_offset) / BILLION
```

The quality of this mapping depends entirely on radiod's system clock (`CLOCK_REALTIME`):
- With PPS-disciplined chrony (L5): ±100 ns accuracy
- With NTP only (L2): ±1-10 ms accuracy

---

## Timing Accuracy Hierarchy

| Level | Source | Typical Accuracy | RTP-to-UTC Mapping |
|-------|--------|------------------|-------------------|
| **L5** | GPS+PPS on radiod machine | ±100 ns | Direct PPS edge timestamps |
| **L4** | GPS+PPS on LAN | ±1 μs | PPS via NTP/PTP, network jitter |
| **L3** | HF-timestd fusion (GPSDO + 17 broadcasts) | ±0.5 ms | Multi-broadcast Kalman fusion |
| **L2** | NTP-sync (stratum 1-2) | ±1-10 ms | Network time, variable latency |
| **L1** | HF bootstrap only | ±5-50 ms | BCD/FSK decoded time, raw ionospheric delay |

---

## Two-Mode Architecture

### Mode Selection

The timing authority is determined by user configuration in `/etc/hf-timestd/timestd-config.toml`:

```toml
[timing]
# "rtp" = radiod has GPS+PPS (L4/L5), trust rtp_to_wallclock()
# "fusion" = hf-timestd fusion provides timing (L3/L2/L1)
# "auto" = monitor discrepancy, infer which is better (future)
authority = "rtp"  # Current: L4 (radiod has LAN GPS+PPS feed)

# Expected accuracy when authority = "rtp"
# L5 (GPS+PPS local): 0.0001 ms (100 ns)
# L4 (GPS+PPS LAN):   0.001 ms (1 μs)
rtp_expected_accuracy_ms = 0.001

# If fusion disagrees with RTP by more than this, warn
validation_threshold_ms = 5.0

# Always run fusion for comparison (recommended for validation)
always_run_fusion = true

# Timing snapshot capture rate (Hz)
timing_snapshot_rate_hz = 2.0
```

**Current Installation Status:**

| Setting | Value | Meaning |
|---------|-------|---------|
| `authority` | `rtp` | Trusting radiod's GPS+PPS timing |
| `rtp_expected_accuracy_ms` | `0.001` | L4 accuracy (±1 μs) |
| `always_run_fusion` | `true` | Fusion runs for validation |

<!-- LOGS: bootstrap | filter: "timing authority" -->

### Mode A: RTP Authority (L4/L5)

When `timing.authority = "rtp"`:

1. **Time basis**: `rtp_to_wallclock()` from ka9q-python provides UTC
2. **Bootstrap**: Near-instant - RTP timestamps already map to UTC
3. **Fusion role**: Validation only - measures ionospheric delay, not UTC offset
4. **Search windows**: Narrow (±5-10 ms) - only ionospheric uncertainty

```
┌─────────────────────────────────────────────────────────────┐
│  RTP AUTHORITY MODE (L4/L5)                                 │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  radiod (GPS+PPS) ──► GPS_TIME + RTP_TIMESNAP               │
│         │                                                    │
│         ▼                                                    │
│  rtp_to_wallclock() ──► UTC time basis (±100ns to ±1μs)     │
│         │                                                    │
│         ▼                                                    │
│  ArrivalPatternMatrix ──► Narrow search windows (±10ms)     │
│         │                                                    │
│         ▼                                                    │
│  Tone Detection ──► Validates expected arrivals             │
│         │                                                    │
│         ▼                                                    │
│  Fusion ──► Measures ionospheric delay (science output)     │
│         │                                                    │
│         ▼                                                    │
│  Comparison ──► Validates RTP timing is truly L4/L5         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Mode B: Fusion Authority (L3/L2/L1)

When `timing.authority = "fusion"`:

1. **Time basis**: HF fusion establishes RTP-to-UTC mapping
2. **Bootstrap**: 2-3 min provisional, 10 min refined (current behavior)
3. **Fusion role**: Primary timing source
4. **Search windows**: Wide initially (±100 ms), narrow after lock

```
┌─────────────────────────────────────────────────────────────┐
│  FUSION AUTHORITY MODE (L3/L2/L1)                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  radiod (NTP only) ──► GPS_TIME + RTP_TIMESNAP              │
│         │                    (±1-10ms accuracy)              │
│         ▼                                                    │
│  Bootstrap ──► Wide search windows (±100ms)                 │
│         │                                                    │
│         ▼                                                    │
│  Tone Detection ──► Finds second boundaries                 │
│         │                                                    │
│         ▼                                                    │
│  Fusion ──► Establishes RTP-to-UTC offset (±0.5ms)          │
│         │                                                    │
│         ▼                                                    │
│  ArrivalPatternMatrix ──► Narrows windows post-lock         │
│         │                                                    │
│         ▼                                                    │
│  Comparison ──► Logs discrepancy with rtp_to_wallclock()    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Bootstrap Behavior by Mode

### L4/L5 Bootstrap (RTP Authority)

With accurate RTP-to-UTC mapping available immediately:

| Phase | Duration | Action |
|-------|----------|--------|
| **Startup** | 0 sec | Read GPS_TIME/RTP_TIMESNAP from status packets |
| **Validation** | 1-2 sec | Detect first tone, confirm arrival within expected window |
| **Locked** | ~5 sec | Multiple tones validate timing; begin science measurements |

**Key difference**: No need to search for second boundaries - we already know where they are.

### L3/L2/L1 Bootstrap (Fusion Authority)

Current two-tier bootstrap behavior:

| Phase | Duration | Action |
|-------|----------|--------|
| **Searching** | 0-2 min | Wide search windows, find candidate tones |
| **Provisional** | 2-3 min | Minute alignment established, narrow windows |
| **Refined** | 10-15 min | Ionospheric averaging, stable offset |

---

## Integration Points

### 1. Configuration Loading

**File**: `src/hf_timestd/config/settings.py`

Add timing authority configuration:
```python
@dataclass
class TimingConfig:
    authority: str = "fusion"  # "rtp", "fusion", or "auto"
    rtp_expected_accuracy_ms: float = 0.001
    validation_threshold_ms: float = 5.0
    always_run_fusion: bool = True
```

### 2. Bootstrap Service

**File**: `src/hf_timestd/core/bootstrap_service.py`

Modify bootstrap behavior based on timing authority:
```python
if config.timing.authority == "rtp":
    # Skip wide search - use rtp_to_wallclock() for initial time
    initial_utc = rtp_to_wallclock(current_rtp, channel_info)
    # Use narrow search windows from ArrivalPatternMatrix
    search_window_ms = config.timing.rtp_expected_accuracy_ms + 10.0  # ionospheric margin
else:
    # Current behavior - wide search, fusion establishes timing
    search_window_ms = 100.0
```

### 3. Tone Detector

**File**: `src/hf_timestd/core/tone_detector.py`

Use timing authority to size search windows:
```python
def get_search_window(self, expected_arrival_rtp: int) -> Tuple[int, int]:
    if self.timing_authority == "rtp":
        # Narrow window - only ionospheric uncertainty
        margin_samples = int(0.010 * self.sample_rate)  # ±10ms
    else:
        # Wide window until fusion lock
        margin_samples = int(0.100 * self.sample_rate)  # ±100ms
    return (expected_arrival_rtp - margin_samples, 
            expected_arrival_rtp + margin_samples)
```

### 4. ArrivalPatternMatrix

**File**: `src/hf_timestd/core/arrival_pattern_matrix.py`

Already provides physics-based search windows. Integration:
```python
def get_search_window(self, station: str, freq_hz: int, utc_second: float) -> Tuple[float, float]:
    """
    Returns (earliest_ms, latest_ms) relative to UTC second boundary.
    
    In RTP authority mode: UTC second boundary is known precisely
    In Fusion authority mode: UTC second boundary has uncertainty from fusion
    """
    # Physics-based window (ionospheric delay range)
    base_window = self._compute_ionospheric_window(station, freq_hz)
    
    # Add timing uncertainty based on authority mode
    timing_uncertainty = self.timing_config.get_current_uncertainty_ms()
    
    return (base_window[0] - timing_uncertainty,
            base_window[1] + timing_uncertainty)
```

### 5. Multi-Broadcast Fusion

**File**: `src/hf_timestd/core/multi_broadcast_fusion.py`

Fusion always runs, but its role changes:
```python
def process_detection(self, detection: ToneDetection):
    # Always compute fusion offset
    fusion_offset = self._kalman_update(detection)
    
    if self.timing_authority == "rtp":
        # Fusion is validation - compare to RTP-derived time
        rtp_time = rtp_to_wallclock(detection.rtp_timestamp, self.channel_info)
        discrepancy_ms = (fusion_offset - rtp_time) * 1000
        
        if abs(discrepancy_ms) > self.validation_threshold_ms:
            logger.warning(f"Timing discrepancy: fusion vs RTP = {discrepancy_ms:.2f}ms")
            self._emit_timing_alert(discrepancy_ms)
    else:
        # Fusion is authoritative - update system time offset
        self._update_rtp_to_utc_offset(fusion_offset)
```

### 6. Metrology Service

**File**: `src/hf_timestd/core/metrology_service.py`

Tag measurements with timing source:
```python
def record_measurement(self, measurement: Measurement):
    measurement.timing_authority = self.timing_config.authority
    measurement.timing_level = self._get_current_timing_level()
    measurement.timing_uncertainty_ms = self._get_current_uncertainty_ms()
    # ... write to HDF5
```

---

## Validation Strategy

In both modes, continuous comparison catches configuration errors:

| Scenario | Expected Behavior |
|----------|-------------------|
| RTP authority, radiod has L5 | Fusion agrees within ±1ms |
| RTP authority, radiod only has L2 | Fusion disagrees by 5-50ms → **alert** |
| Fusion authority, radiod has L5 | Fusion agrees within ±1ms (could upgrade to RTP) |
| Fusion authority, radiod has L2 | Fusion provides better timing (expected) |

### Auto Mode (Future)

`timing.authority = "auto"` could:
1. Start in fusion mode
2. Monitor discrepancy between fusion and `rtp_to_wallclock()`
3. If consistently < 1ms for 10+ minutes, infer L4/L5 and switch to RTP authority
4. If discrepancy grows, fall back to fusion authority

---

## Deployment Scenarios

### Scenario 1: Single Machine with GPS+PPS

```
┌─────────────────────────────────────────┐
│  Machine with GPSDO + PPS               │
│  ┌─────────────┐  ┌─────────────┐       │
│  │   radiod    │  │  hf-timestd │       │
│  │  (L5 time)  │──│  (RTP auth) │       │
│  └─────────────┘  └─────────────┘       │
│         ▲                               │
│    GPSDO + PPS                          │
└─────────────────────────────────────────┘

Config: timing.authority = "rtp"
```

### Scenario 2: Separate Machines, PPS on radiod

```
┌─────────────────────┐    ┌─────────────────────┐
│  radiod machine     │    │  hf-timestd machine │
│  (GPSDO + PPS, L5)  │───►│  (NTP only, L2)     │
│                     │RTP │  timing.auth = "rtp"│
└─────────────────────┘    └─────────────────────┘

Config: timing.authority = "rtp"
Result: hf-timestd trusts radiod's L5 timing via RTP
```

### Scenario 3: Separate Machines, No PPS

```
┌─────────────────────┐    ┌─────────────────────┐
│  radiod machine     │    │  hf-timestd machine │
│  (GPSDO, NTP, L2)   │───►│  (NTP, L2)          │
│                     │RTP │  timing.auth="fusion│
└─────────────────────┘    └─────────────────────┘

Config: timing.authority = "fusion"
Result: hf-timestd fusion achieves L3 (±0.5ms)
```

### Scenario 4: Proxmox Multi-VM Coherent Reception

```
┌──────────────────────────────────────────────────────────────┐
│                     PROXMOX HOST                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  GPSDO + PPS → chrony (L5) → PTP grandmaster            │ │
│  └─────────────────────────────────────────────────────────┘ │
│         ┌─────────────────┼─────────────────┐                │
│         ▼                 ▼                 ▼                │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐         │
│  │   VM1      │    │   VM2      │    │   VM3      │         │
│  │  radiod    │    │  radiod    │    │ hf-timestd │         │
│  │  antenna1  │    │  antenna2  │    │ RTP auth   │         │
│  │  PTP slave │    │  PTP slave │    │ PTP slave  │         │
│  └────────────┘    └────────────┘    └────────────┘         │
└──────────────────────────────────────────────────────────────┘

Config: timing.authority = "rtp"
Note: All VMs share host's L5 timing via PTP
```

---

## Layered Architecture: Recording vs. Interpretation

### Core Principle

**Recording captures facts. Interpretation applies models.**

The recording layer is immutable and model-independent. All timing and physics analysis can be recomputed from recorded data with improved models.

### Layer 0: Raw Recording (Real-time, Immutable)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  RECORDING LAYER                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  What we capture:                                                        │
│    - Raw IQ samples                                                      │
│    - RTP timestamps (per packet/block) - GPSDO-stable                   │
│    - RTP sequence numbers (for gap detection)                           │
│    - GPS_TIME/RTP_TIMESNAP pairs from radiod status (periodic)          │
│    - Local wall clock (for reference/debugging only)                    │
│                                                                          │
│  This is SUFFICIENT. Everything else can be derived later.              │
│                                                                          │
│  NO timing authority decision needed at this layer.                     │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Layer 1: Tone Detection (Real-time or Post-hoc)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  DETECTION LAYER                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Input: IQ samples + RTP timestamps                                      │
│  Output: Detected tones with RTP timestamps                              │
│                                                                          │
│  Example: "1000 Hz tone detected at RTP timestamp 12345678"              │
│                                                                          │
│  NO UTC interpretation yet - just RTP timestamp domain                  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Layer 2: Timing Interpretation (Real-time or Post-hoc)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  TIMING LAYER                                                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Question: What UTC time does RTP timestamp X correspond to?            │
│                                                                          │
│  Method A (RTP Authority): Use GPS_TIME/RTP_TIMESNAP from radiod        │
│    - Accuracy depends on radiod's clock (L5: ±100ns, L2: ±10ms)        │
│                                                                          │
│  Method B (Fusion): Use detected tones to establish mapping             │
│    - Multi-station least-squares separation                             │
│    - Accuracy: ±0.5ms (L3)                                              │
│                                                                          │
│  BOTH methods can be applied to the same recorded data!                 │
│  Comparison validates the methodology.                                   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

**Live Evidence - Fusion D_clock estimates:**

<!-- LOGS: fusion | filter: "d_clock_fused" -->

### Layer 3: Physics Interpretation (Real-time or Post-hoc)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  PHYSICS LAYER                                                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Given: Tone at RTP timestamp X = UTC time T (from timing layer)        │
│                                                                          │
│  Compute:                                                                │
│    - Ionospheric delay = observed_arrival - expected_arrival            │
│    - TEC from multi-frequency differential delay                        │
│    - Propagation mode from absolute delay                               │
│    - Scintillation from amplitude/phase variations                      │
│                                                                          │
│  Can be RECOMPUTED if timing interpretation improves                    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

**Live Evidence - TEC and ionospheric measurements:**

<!-- LOGS: TEC | filter: "TEC" -->

### Breaking the Circular Dependency

The apparent circularity (timing needs ionospheric models, physics needs timing) breaks at layer boundaries:

| Layer | Uses | Provides |
|-------|------|----------|
| **Layer 1** (Detection) | IQ + RTP timestamps | Tone detections in RTP domain |
| **Layer 2** (Timing) | Detections + climatological priors | RTP-to-UTC mapping |
| **Layer 3** (Physics) | Detections + timing | Ionospheric measurements |

**Key insight**: Layer 2 uses **climatological** ionospheric priors (from IRI-2020 model), not measurements. This provides the prior for timing estimation. Layer 3 then measures the **actual** ionosphere given the established timing.

---

## Chrony Feed: Optional Output, Not Core Architecture

### When Is Chrony Needed?

| Use Case | Chrony Needed? | Why |
|----------|----------------|-----|
| **Recording raw IQ** | No | RTP timestamps are GPSDO-stable |
| **Post-hoc analysis** | No | All timing derived from recorded data |
| **Real-time dashboard** | No | Display can use approximate time |
| **File naming** | Marginal | ±1 second is fine for filenames |
| **External client needs precise time** | **Yes** | If another process queries system clock |
| **Triggering on precise UTC boundaries** | **Yes** | Start/stop at exact second |

### Chrony Feed Configuration

```toml
[chrony_feed]
# Only enable if external processes need HF-derived system time
enabled = false

# Only meaningful when timing.authority = "fusion"
# If timing.authority = "rtp", GPS+PPS already disciplines chrony
```

**With L4/L5 (GPS+PPS)**: Chrony is already disciplined by PPS. hf-timestd feeding chrony is redundant.

**With L3/L2/L1**: Chrony feed only matters if some *other* process needs accurate system time. The raw recording and analysis don't require it.

---

## Validation: L5 as Ground Truth

With GPS+PPS available, we can rigorously validate HF timing methodology:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  VALIDATION METHODOLOGY                                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Ground Truth: GPS+PPS (L5) → radiod → GPS_TIME/RTP_TIMESNAP           │
│                Accuracy: ±100 ns                                         │
│                                                                          │
│  Test Subject: HF fusion (L3)                                           │
│                Claimed accuracy: ±0.5 ms                                 │
│                                                                          │
│  Method:                                                                 │
│    1. Record with L5 timing (GPS_TIME in metadata)                      │
│    2. Run HF fusion on same data                                        │
│    3. Compare: fusion_offset vs rtp_to_wallclock()                      │
│    4. Measure actual fusion accuracy over hours/days/seasons            │
│                                                                          │
│  This validates the entire HF timing methodology!                       │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Live Validation Dashboard

The validation dashboard at [/timing-validation](/timing-validation) provides real-time comparison:

| Metric | Description | API Endpoint |
|--------|-------------|--------------|
| **Mean Discrepancy** | Average (fusion - GPS) offset in ms | `/api/timing-validation/statistics` |
| **Within Uncertainty** | % of points where \|discrepancy\| < fusion_uncertainty | `/api/timing-validation/statistics` |
| **Quality Grade Distribution** | Breakdown of A/B/C/D fusion grades | `/api/timing-validation/statistics` |
| **Time Series** | Discrepancy over time with uncertainty bands | `/api/timing-validation/dashboard` |

**Interpretation Guide:**

- **Mean discrepancy near 0**: Fusion methodology is accurate
- **Mean discrepancy offset but stable**: Systematic bias (calibration issue)
- **High std deviation**: Ionospheric variability or methodology instability
- **Within uncertainty < 95%**: Fusion uncertainty estimates are too optimistic

<!-- LOGS: fusion | filter: "D_clock" -->

### Timing Snapshot Evidence

JSON sidecars now include `timing_snapshots` arrays capturing radiod's GPS_TIME/RTP_TIMESNAP pairs at ~2 Hz. This provides the ground truth for validation.

Example from a live sidecar:
```json
{
  "timing_snapshots": [
    {
      "gps_time_ns": 1769865060000000000,
      "rtp_timesnap": 123456789,
      "local_receipt_time": 1769865060.123
    }
  ]
}
```

<!-- LOGS: bootstrap | filter: "timing_snapshot" -->

---

## Enhanced Metadata Schema

### Recording Metadata (Immutable Facts)

```python
@dataclass
class RecordingMetadata:
    """Recorded with raw IQ data - immutable facts"""
    
    # RTP timing (from packets)
    rtp_timestamp_start: int
    rtp_timestamp_end: int
    rtp_sequence_start: int
    rtp_sequence_end: int
    sample_rate: int
    
    # radiod timing snapshots (periodic, from status packets)
    timing_snapshots: List[TimingSnapshot]  # GPS_TIME, RTP_TIMESNAP pairs
    
    # Local reference (for debugging, not authoritative)
    local_wall_clock_at_start: float
    
    # Gap analysis (factual, not interpretive)
    rtp_gaps: List[RTPGap]  # sequence/timestamp discontinuities


@dataclass  
class TimingSnapshot:
    """A GPS_TIME/RTP_TIMESNAP pair from radiod"""
    gps_time_ns: int          # radiod's wall clock (ns since GPS epoch)
    rtp_timesnap: int         # RTP timestamp at that moment
    local_receipt_time: float # When we received this status packet
```

### Interpretation Metadata (Computed, Recomputable)

```python
@dataclass
class TimingInterpretation:
    """Computed from raw data - can be recomputed with different models"""
    
    # Which authority was used
    authority: str  # "rtp", "fusion"
    
    # The RTP-to-UTC mapping
    rtp_to_utc_offset_ns: int
    offset_uncertainty_ns: int
    
    # How it was derived
    derivation_method: str  # "radiod_gps_time", "hf_fusion"
    derivation_timestamp: float
    
    # Fusion details (if applicable)
    fusion_stations_used: List[str]
    fusion_frequencies_used: List[int]
    fusion_residual_ms: float


@dataclass
class PhysicsInterpretation:
    """Ionospheric measurements - depends on timing interpretation"""
    
    timing_interpretation_id: str  # Links to which timing was used
    
    # Per-detection measurements
    ionospheric_delays: List[IonosphericDelay]
    tec_estimates: List[TECEstimate]
    propagation_modes: List[ModeIdentification]
```

---

## Current State Analysis

### What's Already Recorded

The `BinaryArchiveWriter` currently records in JSON sidecar:
- `minute_boundary`, `channel_name`, `frequency_hz`, `sample_rate`
- `samples_written`, `samples_expected`, `completeness_pct`
- `gap_count`, `gap_samples`
- `start_rtp_timestamp` - RTP timestamp at buffer start
- `start_system_time` - Local wall clock at buffer start
- `radiod_snr_db`, `station` config

### What's Missing (Critical Gap)

**The `GPS_TIME/RTP_TIMESNAP` pairs from radiod status packets are NOT being recorded.**

The `ChannelInfo` from ka9q-python already has these fields:
```python
# From ka9q/discovery.py
gps_time: Optional[int] = None      # GPS nanoseconds when RTP_TIMESNAP was captured
rtp_timesnap: Optional[int] = None  # RTP timestamp at GPS_TIME
```

But `StreamRecorderV2` doesn't capture them and pass them to `BinaryArchiveWriter`.

**Impact**: 
- Post-hoc RTP authority mode is impossible without radiod's GPS_TIME
- Validation (comparing fusion to radiod timing) is impossible
- L5 ground truth testing cannot be done on existing recordings

---

## Implementation Plan

### Phase 0: Record radiod Timing Snapshots (Critical)

**Goal**: Capture GPS_TIME/RTP_TIMESNAP pairs so post-hoc analysis is possible.

#### 0.1 Modify BinaryArchiveWriter

Add timing snapshot accumulator:
```python
@dataclass
class TimingSnapshot:
    gps_time_ns: int          # radiod's GPS_TIME
    rtp_timesnap: int         # RTP timestamp at that moment
    local_receipt_time: float # When we received this status

class BinaryArchiveWriter:
    def __init__(self, ...):
        self.timing_snapshots: List[TimingSnapshot] = []
    
    def add_timing_snapshot(self, gps_time_ns: int, rtp_timesnap: int):
        """Called periodically with radiod status updates"""
        self.timing_snapshots.append(TimingSnapshot(
            gps_time_ns=gps_time_ns,
            rtp_timesnap=rtp_timesnap,
            local_receipt_time=time.time()
        ))
```

#### 0.2 Modify StreamRecorderV2

Capture ChannelInfo timing on each status update:
```python
def _handle_samples(self, samples, quality):
    # Existing code...
    
    # Capture radiod timing snapshot if available
    if self.channel_info and self.channel_info.gps_time and self.channel_info.rtp_timesnap:
        self.archive_writer.add_timing_snapshot(
            gps_time_ns=self.channel_info.gps_time,
            rtp_timesnap=self.channel_info.rtp_timesnap
        )
```

#### 0.3 Include in JSON Sidecar

```python
def _flush_minute(self, buffer):
    metadata = {
        # ... existing fields ...
        
        # NEW: radiod timing snapshots for this minute
        'timing_snapshots': [
            {
                'gps_time_ns': s.gps_time_ns,
                'rtp_timesnap': s.rtp_timesnap,
                'local_receipt_time': s.local_receipt_time
            }
            for s in self._get_snapshots_for_minute(buffer.minute_boundary)
        ]
    }
```

### Phase 0: Record radiod Timing Snapshots ✅ IMPLEMENTED (2026-01-31)

**Files modified:**
- `src/hf_timestd/core/binary_archive_writer.py` - Added `TimingSnapshot` dataclass, `add_timing_snapshot()` method
- `src/hf_timestd/core/stream_recorder_v2.py` - Added `_timing_poll_loop()` to capture GPS_TIME/RTP_TIMESNAP at ~2 Hz

**Result:** JSON sidecar now includes `timing_snapshots` array with radiod's GPS_TIME/RTP_TIMESNAP pairs.

### Phase 1: Add Configuration ✅ IMPLEMENTED (2026-01-31)

**Files modified:**
- `config/timestd-config.toml` - Added `[timing]` section with authority, thresholds, snapshot rate
- `src/hf_timestd/interfaces/data_models.py` - Added `TimingAuthority` enum and `TimingConfig` dataclass

**Config options:**
```toml
[timing]
authority = "fusion"  # or "rtp" for L4/L5
rtp_expected_accuracy_ms = 0.001
validation_threshold_ms = 5.0
always_run_fusion = true
timing_snapshot_rate_hz = 2.0
```

### Phase 2: Implement RTP Authority Mode ✅ IMPLEMENTED (2026-01-31)

**Files modified:**
- `src/hf_timestd/core/bootstrap_service.py` - Added `timing_config` to `BootstrapConfig`, `get_search_window_ms()`, `is_rtp_authority` property

**Behavior:** When `authority = "rtp"`, bootstrap uses narrow search windows (timing + ionospheric uncertainty only).

### Phase 3: Validation Logging ✅ IMPLEMENTED (2026-01-31)

**Files created:**
- `src/hf_timestd/core/timing_validation.py` - `TimingValidator` class for comparing fusion vs radiod timing

**Features:**
- Records fusion offset and radiod GPS_TIME snapshots
- Computes discrepancy and logs warnings when threshold exceeded
- Maintains history for statistics
- Alerts on misconfiguration (RTP authority but large discrepancy)

### Phase 4: Validation Dashboard ✅ IMPLEMENTED (2026-01-31)

**Files created:**
- `src/hf_timestd/core/timing_validation_service.py` - Service to parse JSON sidecars and compare fusion vs GPS
- `web-api/routers/timing_validation.py` - API endpoints for validation data
- `web-api/static/timing-validation.html` - Interactive dashboard UI

**Features:**
- Parses timing snapshots from JSON sidecars
- Reads fusion results from HDF5 files (with SWMR for concurrent access)
- Computes discrepancy statistics (mean, std, within uncertainty %)
- Real-time dashboard with Chart.js visualizations
- Auto-refresh every 60 seconds

**API Endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /api/timing-validation/dashboard` | Complete dashboard data |
| `GET /api/timing-validation/statistics` | Aggregate statistics |
| `GET /api/timing-validation/minute/{id}` | Per-minute detail |
| `GET /api/timing-validation/recent` | Recent validation points |

**Dashboard URL:** [/timing-validation](/timing-validation)

### Phase 5: Auto Mode (Future)

1. Implement discrepancy monitoring over extended periods
2. Automatic mode switching based on observed accuracy
3. Timing level transitions in status API
4. Alert on GPS feed degradation

---

## References

- `docs/TIMING_ARCHITECTURE_V2.md` - Existing RTP timestamp architecture
- `docs/design/ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md` - Physics-based search windows
- `/home/mjh/git/ka9q-radio/src/radio_status.c` - GPS_TIME/RTP_TIMESNAP encoding
- `/home/mjh/git/ka9q-python/ka9q/rtp_recorder.py` - `rtp_to_wallclock()` implementation

---

## Living Documentation Integration

This document follows the Living Documentation methodology: documentation that is directly connected to the live system's behavior and data.

### How It Works

1. **Directives**: `<!-- LOGS: source | filter: "pattern" -->` markers in this document
2. **Rendering**: The web UI at `/docs` fetches live evidence from `/api/living-docs/evidence/{source}/{filter}`
3. **Sources**: bootstrap, fusion, physics, TEC, L1-L2, metrology logs
4. **Result**: Claims are backed by actual system behavior, not just assertions

### Validation Dashboard as Living Proof

The [/timing-validation](/timing-validation) dashboard is the ultimate Living Documentation:
- **Claim**: "Fusion achieves ±0.5 ms accuracy"
- **Proof**: Real-time comparison against GPS ground truth
- **Verdict**: Statistics show actual accuracy over time

If fusion methodology has issues, the dashboard will reveal them. This closes the loop between theory and practice.
