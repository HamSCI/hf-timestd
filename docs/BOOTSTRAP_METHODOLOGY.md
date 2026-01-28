# Bootstrap Time Synchronization Methodology

## Living Documentation with Evidence

This document describes the bootstrap process that establishes RTP-to-UTC time alignment
**without relying on NTP or any external time reference**. The system discovers the time basis
purely from the physics of radio propagation and the known timing of time standard broadcasts.

> **Note**: Evidence sections below are populated dynamically from this installation's logs.
> View this document at `/docs.html?doc=BOOTSTRAP_METHODOLOGY` to see live evidence widgets.

### Key Principle: NTP-Free Timing

The bootstrap operates on a fundamental principle: **the time basis is unknown at startup
and must be discovered from tone signals only**. The system clock (NTP) is used only as an
optimization hint to start the buffer near a minute boundary—it is never used as ground truth
for timing.

This means the system could operate with the system clock completely inaccessible. The only
difference would be that buffer start would be at a random point rather than near a minute
boundary, making bootstrap slightly slower.

---

## 1. The Bootstrap Problem

### Challenge

When the system starts, we have:
- **RTP timestamps**: Monotonic sample counters from the GPSDO-disciplined SDR (24,000 samples/second)
- **No time basis**: RTP counts samples but has no connection to UTC
- **Multiple radio channels**: WWV (6 frequencies), WWVH (4 frequencies), CHU (3 frequencies)

We need to determine the **RTP-to-UTC offset** - the mapping between RTP sample numbers
and absolute UTC time - with sub-millisecond precision. **NTP is not trusted** because it
may be wrong, unavailable, or insufficiently accurate for our needs.

### Solution: Tone-Derived Time Discovery

The bootstrap discovers the time basis **purely from tone arrivals**:

1. **GPSDO provides RTP counts** — a "steel ruler" with no time basis, just sample counts
2. **Detect tone clusters** — find groups of tones from multiple stations
3. **Validate 1,440,000 sample recurrence** — confirms these are minute markers (not per-second ticks)
4. **Compute offset = earliest_tone_rtp - geometric_delay** — RTP at minute boundary
5. **BCD/FSK decoding** — gives initial estimate of which absolute minute
6. **Ongoing refinement** — continuously improve the offset estimate

The key insight: we don't need to know what time it is to find minute boundaries. We just
need to find recurring patterns at exactly 60-second (1,440,000 sample) intervals.

---

## 2. Minute Marker Characteristics

Each time standard station transmits distinctive minute markers:

| Station | Tone Frequency | Duration | Template |
|---------|---------------|----------|----------|
| **WWV** (Fort Collins, CO) | 1000 Hz | 800 ms | Matched filter at 24 kHz |
| **WWVH** (Kauai, HI) | 1200 Hz | 800 ms | Matched filter at 24 kHz |
| **CHU** (Ottawa, Canada) | 1000 Hz | 500 ms | Matched filter at 24 kHz |
| **BPM** (Xi'an, China) | 1000 Hz | 300 ms | Matched filter at 24 kHz |

### Why Duration Matters

Per-second ticks are only **5-10 ms** long. The matched filter templates (500-800 ms)
produce **dramatically higher correlation** for minute markers than for ticks:

- **Minute marker (800 ms)**: Full template match → high correlation, high SNR
- **Per-second tick (5 ms)**: Only 0.6% of template matches → weak correlation, low SNR

**Evidence - SNR Discrimination:**
<!-- LOGS: bootstrap | filter: "recurring_clusters" -->

The system requires **SNR ≥ 20 dB** for candidate acceptance, which effectively filters
out short ticks that produce only 12-15 dB correlation peaks.

---

## 3. Bootstrap State Machine

```
ACQUIRING → CORRELATING → TRACKING → LOCKED
    ↑______________|___________|
         (retreat on errors)
```

### State Descriptions

| State | Purpose | Exit Condition |
|-------|---------|----------------|
| **ACQUIRING** | Collect candidates, find recurring clusters | Multi-station cluster recurs at 60s intervals |
| **CORRELATING** | Validate clusters across channels | 3+ clusters over 2+ minutes validated |
| **TRACKING** | Narrow-window detection, await BCD/FSK confirmation | Time confirmed by decoded broadcast |
| **LOCKED** | Offset established with high confidence | Continuous operation |

### Evidence - State Transitions
<!-- LOGS: bootstrap | filter: "state_transitions" -->

---

## 4. Tone Detection Pipeline

### 4.1 Per-Channel Tone Detectors

Each channel gets its own `ToneDetector` with appropriate templates:

```python
# bootstrap_service.py
def _get_tone_detector(self, channel_name: str):
    """Get or create tone detector for a specific channel.
    
    Each channel needs its own detector with appropriate templates:
    - CHU channels get CHU templates (500ms @ 1000Hz)
    - WWV channels get WWV templates (800ms @ 1000Hz)
    - Shared channels get WWV + WWVH + BPM templates
    """
```

**Evidence - Detector Creation:**
<!-- LOGS: bootstrap | filter: "detector_creation" -->

### 4.2 Matched Filter Detection

The `ToneDetector.acquire_tones()` method uses:

1. **AM Demodulation**: Extract envelope from IQ samples
2. **Bandpass Filtering**: Isolate tone frequency (1000 Hz or 1200 Hz)
3. **Quadrature Correlation**: Phase-invariant matched filtering
4. **Noise Floor Estimation**: Median + MAD for robust thresholding
5. **Peak Detection**: Non-maximum suppression with minimum separation

**Evidence - Multi-Station Detection:**
<!-- LOGS: bootstrap | filter: "multi_station_detection" -->

This shows simultaneous detection across all station types with high SNR.

---

## 5. Geographic Validation

### 5.1 Propagation Delay Expectations

The system computes expected propagation delays based on receiver location:

```python
# timing_bootstrap.py
def __post_init__(self):
    """Compute geographic expectations for each station."""
    for station, (lat, lon) in STATION_LOCATIONS.items():
        distance_km = self._haversine_km(self.receiver_lat, self.receiver_lon, lat, lon)
        path_km = distance_km * IONOSPHERIC_PATH_FACTOR  # 1.15 for F-layer
        delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000
```

**Evidence - Geographic Expectations (this installation):**
<!-- LOGS: bootstrap | filter: "geographic_expectations" -->

### 5.2 Multi-Station Clustering

Candidates from different stations are clustered if their arrival times match
the expected propagation delay differences (within 100 ms tolerance):

```python
# timing_bootstrap.py - find_minute_clusters()
for cand in other_cands:
    offset_ms = (cand.rtp_timestamp - anchor_cand.rtp_timestamp) * 1000 / sample_rate
    
    # Allow matching across minute boundaries
    raw_error = offset_ms - expected_offset_ms
    minutes_diff = round(raw_error / 60000)
    error = abs(raw_error - minutes_diff * 60000)
    
    if error < window_ms:  # 100ms tolerance
        cluster['members'][other_station].append(cand)
```

**Evidence - Multi-Station Clusters:**
<!-- LOGS: bootstrap | filter: "cluster_lock" -->

---

## 6. Recurrence Validation

### The Key Insight

Per-second ticks occur every second. Minute markers occur every 60 seconds.
By requiring clusters to **recur at 60-second intervals**, we definitively
distinguish minute markers from ticks.

```python
# timing_bootstrap.py - ACQUIRING state
for cluster in multi_station:
    anchor_rtp = cluster['anchor_rtp']
    
    for other in multi_station:
        diff = abs(other['anchor_rtp'] - anchor_rtp)
        minutes_apart = round(diff / SAMPLES_PER_MINUTE)  # 1,440,000 samples
        
        if minutes_apart > 0:
            expected_diff = minutes_apart * SAMPLES_PER_MINUTE
            error_ms = abs(diff - expected_diff) * 1000 / sample_rate
            
            if error_ms < 100:  # Within 100ms of expected
                # Found recurring minute markers!
                self.state = BootstrapState.CORRELATING
```

**Evidence - Recurrence Detection:**
<!-- LOGS: bootstrap | filter: "recurring_clusters" -->

The error shown is well within the 100 ms tolerance, confirming these are
true minute markers recurring at exactly 60-second intervals.

---

## 7. Time Confirmation

### BCD/FSK Decoding

Once in TRACKING state, the system attempts to decode the actual UTC time
from the broadcast:

- **WWV/WWVH**: BCD time code in the audio subcarrier
- **CHU**: FSK time code in seconds 31-39 of each minute

This provides **absolute time confirmation** rather than just relative timing.

**Evidence - Offset Lock:**
<!-- LOGS: bootstrap | filter: "rtp_lock" -->

The offset should be stable to within ±2 ms across multiple channels.

---

## 8. Two-Tier Bootstrap Lock (v5.3.10)

### The Core Problem: Discovering Time Without Knowing Time

The bootstrap must solve a chicken-and-egg problem:
- We need to know the RTP-to-UTC offset to compute timing errors
- We need timing errors to refine the offset

The solution is a **two-tier approach** that separates pattern discovery from offset refinement:

### Tier 1: Pattern Discovery (PROVISIONAL Lock)

**Purpose**: Find and validate minute marker clusters without knowing the offset.

During Tier 1:
- **NO offset correction** — we don't know the offset yet!
- **Just validate cluster recurrence** at 1,440,000 sample intervals
- **Confirm pattern stability** over multiple minutes

Once the pattern is confirmed:
- Compute `minute_boundary_rtp = earliest_tone_rtp - geometric_delay`
- This gives us the RTP at a minute boundary (we don't know which minute yet)
- Apply ionospheric correction to estimate UTC emission time

### Tier 2: Absolute Time Confirmation (REFINED Lock)

**Purpose**: Use BCD/FSK to confirm which absolute minute, then engage formal refinement.

During Tier 2:
- **BCD/FSK decoding** tells us the hour:minute
- **Initial Unix time is an ESTIMATE** requiring further refinement
- **Ongoing refinement** compares tone arrivals to improve the offset

| Tier | Name | Purpose | What We Know |
|------|------|---------|---------------|
| **1** | Provisional Lock | Pattern discovery | Relative time only (minute N from start) |
| **2** | Refined Lock | Absolute time | Estimated UTC (refined over time) |

```
ACQUIRING → CORRELATING → TRACKING → PROVISIONAL_LOCK → REFINED_LOCK
                              ↓              ↓
                         (pattern found)  (BCD/FSK confirms minute)
```

### Why No Offset Correction in Tier 1?

You can't correct an offset you don't know! During Tier 1, we're still discovering
the cluster pattern. We have to:
1. Confirm the cluster pattern is relatively stable
2. Apply subtraction to count back to the geometric offset
3. Count back more for the ionospheric offset to estimate emission time (UTC)
4. Confirm this over a few cycles
5. Only then engage formal ongoing refinement

### Tier 1 Details: Provisional Lock

**Purpose**: Validate pattern, compute initial offset from tone arrivals.

When the bootstrap finds recurring clusters:
- `lock_tier` transitions to `PROVISIONAL` (value=1)
- Archiving can begin (we know minute boundaries)
- **No offset refinement yet** — just pattern validation

The offset is computed purely from tone arrivals:
```
minute_boundary_rtp = earliest_tone_rtp - propagation_delay_samples
```

This is the RTP at a minute boundary. We don't know which minute yet, but we know
the pattern recurs at exactly 1,440,000 sample intervals.

**Evidence - Provisional Lock:**
<!-- LOGS: bootstrap | filter: "PROVISIONAL LOCK" -->

### Tier 2 Details: Refined Lock

**Purpose**: Confirm absolute time via BCD/FSK, then refine the offset.

Once BCD/FSK decoding confirms the hour:minute:
- We now have an **estimate** of absolute UTC
- This estimate requires refinement (ionospheric variability)
- Ongoing tone arrivals are compared to expected arrivals
- The offset is refined based on these comparisons

The initial Unix time estimate is computed as:
```
reference_utc = decoded_hour * 3600 + decoded_minute * 60
```

This is refined over time as more tone arrivals are processed.

**Evidence - Refined Lock:**
<!-- LOGS: bootstrap | filter: "TIER 2 REFINED LOCK" -->

### Offset Measurement Collection

Each validated tone provides an independent offset measurement:

```python
# timing_bootstrap.py - OffsetMeasurement dataclass
@dataclass
class OffsetMeasurement:
    timestamp: float          # Unix time of measurement
    offset_samples: int       # Computed RTP-to-UTC offset
    station: str              # Source station (WWV, WWVH, CHU, BPM)
    snr_db: float            # Signal-to-noise ratio
    frequency_khz: int       # Carrier frequency
```

The system tracks:
- **Station distribution**: Ensures measurements come from multiple stations
- **Temporal spread**: Measurements span the full averaging window
- **SNR weighting**: Higher-SNR measurements are more reliable

**Evidence - Offset Measurements:**
<!-- LOGS: bootstrap | filter: "offset measurements" -->

### Offset Refinement

The refined offset typically differs from the provisional offset by several milliseconds:

| Metric | Provisional | Refined | Improvement |
|--------|-------------|---------|-------------|
| Basis | First few detections | 50+ measurements | Statistical robustness |
| Method | Weighted average | Median | Outlier rejection |
| Ionosphere | Instantaneous | 10-min average | TID averaging |

**Evidence - Offset Change:**
<!-- LOGS: bootstrap | filter: "Offset change from provisional" -->

A change of 5-15 ms is typical and represents the ionospheric bias that would
otherwise become a systematic error.

### Lock Tier in Status

The current lock tier is exposed in the bootstrap status:

```json
{
  "phase": "LOCKED",
  "lock_tier": 2,
  "is_locked": true,
  "is_fully_locked": true,
  "bootstrap_state": {
    "lock_tier": 2,
    "refined_offset_samples": 798457904,
    "refined_offset_std_ms": 12.3
  }
}
```

- `lock_tier: 0` = No lock (ACQUIRING/CORRELATING)
- `lock_tier: 1` = Provisional lock (archiving enabled, offset being refined)
- `lock_tier: 2` = Refined lock (stable offset, full precision)

### Configuration

The two-tier thresholds are configurable in `TimingBootstrap`:

```python
# Production values (timing_bootstrap.py)
refined_lock_duration_sec: float = 600.0   # 10 minutes
min_measurements_for_refined: int = 50     # Minimum measurements
max_offset_std_for_refined_ms: float = 15.0  # Stability criterion
```

These values are chosen based on:
- **600 seconds**: Covers 1-2 TID periods for averaging
- **50 measurements**: Statistical significance for median
- **15 ms std**: Indicates stable ionospheric conditions

---

## 9. Summary: Bootstrap Timeline

A typical bootstrap sequence with two-tier lock:

| Time | Event | Evidence |
|------|-------|----------|
| T+0s | Service starts, buffers accumulate | `Searching 150.0s buffer for minute markers` |
| T+30s | First candidates detected | `Found 10 candidates` |
| T+60s | Candidates from all stations | `Clustering: 66 WWV, 67 BPM, 118 CHU` |
| T+90s | Recurring clusters found | `RECURRING CLUSTERS FOUND: 1 minutes apart` |
| T+90s | State → CORRELATING | `CLUSTER LOCK: WWV@... → CORRELATING` |
| T+90s | State → TRACKING | `3 clusters over 2 minutes → TRACKING` |
| T+93s | **TIER 1: Provisional Lock** | `PROVISIONAL LOCK achieved! D_clock ≈ +0.0ms` |
| T+93s | Archiving begins | `📁 Wrote minute ...` |
| T+693s | **TIER 2: Refined Lock** | `TIER 2 REFINED LOCK achieved!` |

**Tier 1 (Provisional)**: ~90-120 seconds - enables archiving
**Tier 2 (Refined)**: ~10-12 minutes - stable ionospherically-averaged offset

---

## 10. Key Design Decisions

### Why Multi-Station?

Single-station detection is ambiguous:
- Could be a per-second tick
- Could be interference
- No cross-validation

Multi-station clustering provides:
- **Redundancy**: Multiple independent detections
- **Validation**: Geographic consistency check
- **Confidence**: Higher SNR through combination

### Why 60-Second Recurrence?

Per-second ticks would produce clusters every second. By requiring 60-second
recurrence, we guarantee we've found minute markers, not ticks.

### Why Duration-Matched Templates?

The 800 ms (WWV/WWVH) and 500 ms (CHU) templates provide:
- **Duration discrimination**: Short ticks produce weak correlation
- **High SNR**: Full template match maximizes signal extraction
- **Phase invariance**: Quadrature correlation handles unknown carrier phase

---

## 11. Implementation Files

| File | Purpose |
|------|---------|
| `bootstrap_service.py` | Orchestrates bootstrap process, manages per-channel detectors |
| `bootstrap_rolling_buffer.py` | Accumulates samples, searches for candidates |
| `timing_bootstrap.py` | State machine, clustering, recurrence validation |
| `tone_detector.py` | Matched filter detection, template generation |
| `bootstrap_time_confirmation.py` | BCD/FSK decoding for time confirmation |

---

*Evidence is fetched dynamically from this installation's logs via `/api/living-docs/evidence/bootstrap/{type}`*
