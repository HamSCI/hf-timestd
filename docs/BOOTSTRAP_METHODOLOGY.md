# Bootstrap Time Synchronization Methodology

**Version:** 6.4  
**Last Updated:** January 29, 2026  
**View with live evidence:** `/docs.html?doc=BOOTSTRAP_METHODOLOGY`

---

## TL;DR — What You Need to Know

| Audience | Key Takeaway |
|----------|--------------|
| **User** | Bootstrap takes ~2 minutes to lock. Once locked, the system provides ±0.5 ms timing to UTC(NIST). |
| **Metrologist** | NTP provides initial orientation only. Once locked, HF tone arrivals become the time authority. |
| **Physicist** | Ionospheric path delays are measured, not assumed. Multi-station clustering validates minute markers. |
| **Engineer** | NTP used once at bootstrap; `_handle_locked()` refines offset from HF measurements only. |

**The Core Insight:** NTP provides **initial orientation only** — identifying which UTC minute we're in at startup. Once locked, the system transfers time authority to HF-derived measurements. The ongoing time reference comes from tone arrivals, not NTP.

---

## 1. The Bootstrap Problem

### What We Have at Startup

- **RTP timestamps**: Monotonic sample counters from the GPSDO-disciplined SDR (24,000 samples/second)
- **NTP-derived wallclock**: System time disciplined by GPSDO (±1 ms accuracy)
- **Multiple radio channels**: WWV (6 frequencies), WWVH (4 frequencies), CHU (3 frequencies), BPM (4 frequencies)

### What We Need

The **RTP-to-UTC offset** — the mapping between RTP sample numbers and absolute UTC time — with sub-millisecond precision.

### The v6.4 Solution

```
1. Detect tone clusters     → Find minute markers (800ms tones at second 0)
2. Use NTP wallclock        → Identify WHICH UTC minute this is
3. Compute offset           → offset = minute_boundary_rtp - (minute × samples_per_minute)
4. Validate with physics    → Cross-station consistency, geographic expectations
5. Optional: BCD/FSK decode → Sub-second refinement
```

**Evidence — Current Bootstrap Status:**
<!-- LOGS: bootstrap | filter: "NTP confirmation" -->

---

## 2. NTP Role: Initial Orientation Only (v6.4)

### The Two-Phase Time Authority Model

| Phase | Time Authority | Purpose |
|-------|---------------|----------|
| **Bootstrap** | NTP (from GPSDO) | Identify which UTC minute we're in |
| **Locked** | HF tone arrivals | Ongoing time reference, offset refinement |

**Critical Distinction:** NTP is used **once** to resolve the 60-second ambiguity at startup. After lock, the system no longer depends on NTP — it derives time from the HF signals themselves.

### Why Use NTP at All?

Prior to v6.4, the system required BCD/FSK decoding to confirm the absolute minute. This caused:

- **Pipeline stalls**: Poor HF conditions → no decode → no lock → no data
- **Extended bootstrap times**: 10-30 minutes waiting for clean decode

NTP provides a quick initial orientation (±1 ms is far better than 60-second ambiguity), allowing the pipeline to start within ~2 minutes.

### After Lock: HF Becomes the Authority

Once locked, the `_handle_locked()` method:
1. Predicts where the next tone should arrive (based on current offset)
2. Measures where it actually arrived (from HF signal)
3. Computes timing error and refines the offset
4. **Does not consult NTP** — the HF measurements are the ground truth

This ensures the system measures actual propagation delays, not just tracking the system clock.

**Evidence — Time Snap from Metadata:**
<!-- LOGS: bootstrap | filter: "time_snap" -->

---

## 3. Minute Marker Detection

### Station Characteristics

| Station | Tone | Duration | Template | Geographic Delay |
|---------|------|----------|----------|------------------|
| **WWV** (Fort Collins, CO) | 1000 Hz | 800 ms | Matched filter | 4-12 ms |
| **WWVH** (Kauai, HI) | 1200 Hz | 800 ms | Matched filter | 15-30 ms |
| **CHU** (Ottawa, Canada) | 1000 Hz | 500 ms | Matched filter | 6-15 ms |
| **BPM** (Xi'an, China) | 1000 Hz | 300 ms | Matched filter | 40-70 ms |

### Why Duration Matters

Per-second ticks are only **5-10 ms** long. The matched filter templates (500-800 ms) produce dramatically higher correlation for minute markers:

- **Minute marker (800 ms)**: Full template match → SNR ≥ 20 dB
- **Per-second tick (5 ms)**: Only 0.6% of template matches → SNR ~12-15 dB

**Evidence — Multi-Station Detection:**
<!-- LOGS: bootstrap | filter: "multi_station_detection" -->

---

## 4. Bootstrap State Machine

```
ACQUIRING → CORRELATING → TRACKING → LOCKED
    ↑______________|___________|
         (retreat on errors)
```

| State | Duration | Purpose | Exit Condition |
|-------|----------|---------|----------------|
| **ACQUIRING** | 0-90s | Collect candidates, find clusters | Multi-station cluster recurs at 60s |
| **CORRELATING** | 90-120s | Validate clusters across channels | 3+ clusters over 2+ minutes |
| **TRACKING** | 120-150s | Narrow-window detection | NTP confirms minute |
| **LOCKED** | Continuous | Offset established | Ongoing operation |

**Evidence — State Transitions:**
<!-- LOGS: bootstrap | filter: "state_transitions" -->

---

## 5. Geographic Validation

### Propagation Delay Expectations

The system computes expected propagation delays based on receiver location:

```python
distance_km = haversine(receiver_lat, receiver_lon, station_lat, station_lon)
path_km = distance_km * 1.15  # Ionospheric path factor for F-layer
delay_ms = (path_km / 299792.458) * 1000
```

**Evidence — Geographic Expectations (this installation):**
<!-- LOGS: bootstrap | filter: "geographic_expectations" -->

### Multi-Station Clustering

Candidates from different stations are clustered if their arrival times match expected propagation delay differences (within 100 ms tolerance):

**Evidence — Cluster Lock:**
<!-- LOGS: bootstrap | filter: "cluster_lock" -->

---

## 6. Recurrence Validation

### The Key Insight

Per-second ticks occur every second. Minute markers occur every 60 seconds. By requiring clusters to **recur at 1,440,000 sample intervals** (60 seconds × 24,000 samples/second), we definitively distinguish minute markers from ticks.

**Evidence — Recurring Clusters:**
<!-- LOGS: bootstrap | filter: "recurring_clusters" -->

---

## 7. Time Confirmation (v6.4)

### Primary Method: NTP-Derived Wallclock

Once clusters are validated, the system confirms the absolute UTC minute using NTP:

```python
def confirm_time_from_ntp(ntp_wallclock, anchor_rtp, anchor_channel):
    """
    Use NTP-derived wallclock to identify the UTC minute.
    
    The GPSDO provides ±1 ms accuracy, which is far better than
    the 60-second ambiguity we're resolving.
    """
    minute_boundary_utc = floor(ntp_wallclock / 60) * 60
    offset = minute_boundary_utc - (anchor_rtp / sample_rate)
    return offset
```

### Secondary Method: BCD/FSK Decode (Optional)

BCD/FSK decoding provides additional confidence and sub-second refinement:

- **WWV/WWVH**: BCD time code in 100 Hz subcarrier
- **CHU**: FSK time code in seconds 31-39

**Evidence — RTP Lock:**
<!-- LOGS: bootstrap | filter: "rtp_lock" -->

---

## 8. Two-Tier Lock System

### Tier 1: Provisional Lock

**Purpose:** Enable archiving while offset is being refined.

- Achieved when: Recurring clusters found, NTP confirms minute
- Archiving: Enabled (we know minute boundaries)
- Offset quality: ±5-15 ms (ionospheric variability)

**Evidence — Provisional Lock:**
<!-- LOGS: bootstrap | filter: "PROVISIONAL LOCK" -->

### Tier 2: Refined Lock

**Purpose:** Stable, ionospherically-averaged offset.

- Achieved when: 50+ measurements over 10 minutes, σ < 15 ms
- Offset quality: ±1-3 ms (TID-averaged)

**Evidence — Refined Lock:**
<!-- LOGS: bootstrap | filter: "TIER 2 REFINED LOCK" -->

### Offset Refinement

| Metric | Provisional | Refined |
|--------|-------------|---------|
| Basis | First few detections | 50+ measurements |
| Method | Weighted average | Median (outlier rejection) |
| Ionosphere | Instantaneous | 10-min average |

**Evidence — Offset Change:**
<!-- LOGS: bootstrap | filter: "Offset change from provisional" -->

---

## 9. Typical Bootstrap Timeline

| Time | Event | State |
|------|-------|-------|
| T+0s | Service starts, buffers accumulate | ACQUIRING |
| T+30s | First candidates detected | ACQUIRING |
| T+60s | Candidates from all stations | ACQUIRING |
| T+90s | Recurring clusters found | → CORRELATING |
| T+95s | 3 clusters validated | → TRACKING |
| T+100s | NTP confirms minute | → LOCKED (Tier 1) |
| T+100s | **Archiving begins** | LOCKED |
| T+700s | 50+ measurements, stable | LOCKED (Tier 2) |

**Key Performance:** Pipeline proceeds to metrology within ~2 minutes.

---

## 10. For Each Perspective

### For Users

- **What to expect:** Lock in ~2 minutes under normal conditions
- **What to check:** `chronyc sources` should show TSL1/TSL2 after lock
- **If it's slow:** Poor propagation (night, storm) may extend bootstrap

### For Metrologists

- **Traceability chain:** UTC(NIST) → GPSDO → NTP → RTP offset → D_clock
- **Uncertainty at lock:** ±5-15 ms (Tier 1), ±1-3 ms (Tier 2)
- **Validation:** Multi-station consistency, geographic expectations

### For Physicists

- **What's measured:** Ionospheric group delay via tone arrival times
- **What's modeled:** Geometric path length, layer heights (IRI-2020/IONEX)
- **Science preserved:** Raw arrival times archived for reprocessing

### For Engineers

- **Key files:** `timing_bootstrap.py`, `bootstrap_rolling_buffer.py`, `bootstrap_service.py`
- **State persistence:** `bootstrap_state.json` survives restarts
- **Configuration:** `refined_lock_duration_sec=600`, `min_measurements_for_refined=50`

---

## 11. Implementation Files

| File | Purpose |
|------|---------|
| `bootstrap_service.py` | Orchestrates bootstrap, manages per-channel detectors |
| `bootstrap_rolling_buffer.py` | Accumulates samples, searches for candidates |
| `timing_bootstrap.py` | State machine, clustering, NTP confirmation |
| `tone_detector.py` | Matched filter detection, template generation |
| `bootstrap_time_confirmation.py` | BCD/FSK decoding (optional refinement) |

---

## 12. Troubleshooting

### Bootstrap Takes Too Long (>5 minutes)

1. **Check propagation:** Higher frequencies may be dead at night
2. **Check GPSDO lock:** `chronyc tracking` should show stable offset
3. **Check logs:** `journalctl -u timestd-core-recorder -f`

### Bootstrap Never Locks

1. **Verify NTP:** System clock must be within ±30 seconds of UTC
2. **Check antenna:** SNR should be >10 dB on at least one channel
3. **Check radiod:** `systemctl status radiod@rx888`

### Offset Jumps After Lock

- **Normal:** Ionospheric mode changes cause 3-8 ms steps
- **Abnormal:** GPSDO unlock, antenna issue, or software bug

---

*Evidence is fetched dynamically from this installation's logs via `/api/living-docs/evidence/bootstrap/{filter}`*
