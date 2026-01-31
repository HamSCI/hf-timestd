# Dual Chrony Feed Architecture

## Overview

The HF Time Standard system now implements a **dual Chrony feed architecture** that provides two independent timing sources to Chrony for system clock discipline:

- **timestd.L1** (SHM 0): Fast, robust baseline from raw L1 metrology fusion
- **timestd.L2** (SHM 1): Accurate, calibrated timing from L2 calibrated fusion

This architecture enables both operational reliability (L1 fallback) and scientific accuracy (L2 primary), while providing valuable validation data through L1-L2 comparison.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    HF Time Standard Pipeline                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  L1: Metrology Service (×9 channels)                            │
│  • Reads raw IQ data                                             │
│  • Detects tones (WWV, WWVH, CHU, BPM)                          │
│  • Outputs: L1 HDF5 (raw TOA per broadcast)                     │
│  • Location: /var/lib/timestd/phase2/{CHANNEL}/metrology/       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ├─────────────────────────┐
                              ▼                         ▼
┌──────────────────────────────────────┐  ┌────────────────────────┐
│  L2: Calibration Service             │  │  Fusion Service        │
│  • Reads L1 HDF5                     │  │  Path 1: L1→Fusion     │
│  • Applies corrections:              │  │  • Reads L1 HDF5       │
│    - Geometric delay                 │  │  • Multi-broadcast     │
│    - Ionospheric TEC                 │  │    fusion              │
│    - System calibration              │  │  • Outlier rejection   │
│  • ISO GUM uncertainty budget        │  │  • Writes SHM 0        │
│  • Outputs: L2 HDF5 (calibrated)     │  │    (timestd.L1)        │
│  • Location: /var/lib/timestd/       │  │                        │
│    phase2/{CHANNEL}/clock_offset/    │  │  Uncertainty: ±0.85ms  │
└──────────────────────────────────────┘  └────────────────────────┘
                              │
                              ▼
                ┌────────────────────────┐
                │  Fusion Service        │
                │  Path 2: L2→Fusion     │
                │  • Reads L2 HDF5       │
                │  • Multi-broadcast     │
                │    fusion              │
                │  • Kalman filtering    │
                │  • Writes SHM 1        │
                │    (timestd.L2)        │
                │                        │
                │  Uncertainty: ±0.3-1ms │
                └────────────────────────┘
                              │
                              ▼
                ┌────────────────────────┐
                │  Chrony                │
                │  • Prefers TSL2        │
                │    (lower uncertainty) │
                │  • Falls back to TSL1  │
                │  • System clock        │
                │    discipline          │
                └────────────────────────┘
```

## Feed Specifications

### timestd.L1 (SHM 0)

**Purpose:** Fast, robust baseline timing

**Data Source:** L1 metrology measurements (raw TOA)

**Processing:**
- Multi-broadcast fusion (WWV + CHU)
- Weighted averaging by SNR and confidence
- Outlier rejection (MAD-based)
- No propagation corrections applied

**Performance:**
- Uncertainty: ±0.85 ms (multi-broadcast fusion)
- Latency: ~75-135 seconds (metrology + fusion)
- Update rate: Every 60 seconds
- Precision: 1e-3 (1 ms)

**Advantages:**
- Independent of L2 calibration pipeline
- Fast failover if L2 fails
- Simple, robust processing
- Always available when metrology runs

**Limitations:**
- Cannot separate propagation from clock error
- Cannot measure ionospheric effects
- Higher uncertainty (40ms per-broadcast scatter)
- Kalman filter disabled (uncertainty > 5ms threshold)

### timestd.L2 (SHM 1)

**Purpose:** Accurate, calibrated timing for clock discipline and ionospheric science

**Data Source:** L2 calibrated measurements (corrected D_clock)

**Processing:**
- Geometric delay correction (propagation mode identification)
- Ionospheric TEC correction (frequency-dependent)
- System calibration (receiver delays)
- ISO GUM uncertainty budgets
- Multi-broadcast fusion with Kalman filtering

**Performance:**
- Uncertainty: ±0.3-1.0 ms (optimal fusion)
- Per-broadcast: ±1-4 ms (ISO GUM budget)
- Latency: ~105-195 seconds (metrology + calibration + fusion)
- Update rate: Every 60 seconds
- Precision: 1e-4 (100 μs)

**Advantages:**
- Removes propagation delays → see ionospheric effects
- Enables Kalman filter (uncertainty < 5ms)
- Per-broadcast uncertainty budgets
- Optimal timing accuracy
- Supports ionospheric science objectives

**Limitations:**
- Depends on L2 calibration pipeline
- Slightly higher latency (30-60s vs L1)
- More complex processing

## Chrony Behavior

Chrony automatically evaluates and selects sources based on:

1. **Reachability** - Is the source updating?
2. **Stratum** - Distance from reference
3. **Root dispersion** - Accumulated uncertainty
4. **Jitter** - Short-term stability
5. **Offset consistency**

**With dual feeds:**
- Chrony will **prefer timestd.L2** (TSL2) due to lower uncertainty and better precision
- timestd.L1 (TSL1) serves as **automatic fallback** if L2 fails
- Both sources visible in `chronyc sources -v`

**Expected behavior:**
```
$ chronyc sources -v
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
#* TSL2                          0   4   377    15   +0.234ms[+0.234ms] +/- 0.3ms
#- TSL1                          0   4   377    15   +0.856ms[+0.856ms] +/- 0.9ms
```

The `*` indicates TSL2 is selected for clock discipline.

## Scientific Value: L1-L2 Difference

The difference between L1 and L2 feeds provides valuable scientific data:

**L1 - L2 = Propagation Correction Quality**

This difference reveals:
- Quality of geometric delay models
- Accuracy of TEC corrections
- Per-broadcast propagation variations
- Ionospheric effects themselves

**Example analysis:**
```python
# Read both feeds from logs
l1_offset = read_chrony_offset("TSL1")  # +0.856ms
l2_offset = read_chrony_offset("TSL2")  # +0.234ms

# Difference = propagation correction applied
correction = l1_offset - l2_offset  # +0.622ms

# Track over time to validate models
# Large divergence → calibration problem
# Diurnal variation → ionospheric effects
```

## Implementation Details

### Services

1. **timestd-metrology.service**
   - Produces L1 HDF5 per channel
   - 9 instances (one per channel)
   - Always running

2. **timestd-l2-calibration.service** (NEW)
   - Reads L1 HDF5 from all channels
   - Applies propagation corrections
   - Writes L2 HDF5 per channel
   - Polls every 60 seconds

3. **timestd-fusion.service** (MODIFIED)
   - Reads both L1 and L2 HDF5
   - Produces dual SHM outputs:
     - SHM 0: L1 fusion → timestd.L1
     - SHM 1: L2 fusion → timestd.L2
   - Updates every 60 seconds

### File Locations

**L1 Metrology:**
```
/var/lib/timestd/phase2/{CHANNEL}/metrology/
  {CHANNEL}_metrology_measurements_YYYYMMDD.h5
```

**L2 Calibrated:**
```
/var/lib/timestd/phase2/{CHANNEL}/clock_offset/
  {CHANNEL}_timing_measurements_YYYYMMDD.h5
```

**Fusion Output:**
```
/var/lib/timestd/phase2/fusion/
  fusion_fusion_timing_YYYYMMDD.h5
```

### Configuration Files

**Chrony refclocks:**
```
/etc/chrony/chrony.conf (or include file):
  refclock SHM 0 refid TSL1 poll 4 precision 1e-3 offset 0.0
  refclock SHM 1 refid TSL2 poll 4 precision 1e-4 offset 0.0
```

**Systemd service ordering:**
```
/etc/systemd/system/chronyd.service.d/timestd-shm.conf:
  After=timestd-metrology.service timestd-l2-calibration.service timestd-fusion.service
  Wants=timestd-metrology.service timestd-l2-calibration.service timestd-fusion.service
```

## Monitoring

### Check Service Status
```bash
sudo systemctl status timestd-metrology
sudo systemctl status timestd-l2-calibration
sudo systemctl status timestd-fusion
```

### Check Chrony Sources
```bash
chronyc sources -v
chronyc sourcestats
```

### Check L1 Data Production
```bash
ls -lh /var/lib/timestd/phase2/SHARED_10000/metrology/
```

### Check L2 Data Production
```bash
ls -lh /var/lib/timestd/phase2/SHARED_10000/clock_offset/
```

### Check Fusion Logs
```bash
sudo journalctl -u timestd-fusion -f
```

### Verify Dual Feeds
```bash
# Should see both TSL1 and TSL2
chronyc sources | grep TSL
```

## Deployment

### 1. Install L2 Calibration Service
```bash
sudo cp systemd/timestd-l2-calibration.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable timestd-l2-calibration
sudo systemctl start timestd-l2-calibration
```

### 2. Update Fusion Service
```bash
# Fusion service already updated with dual output
sudo systemctl restart timestd-fusion
```

### 3. Configure Chrony
```bash
# Add refclock lines to /etc/chrony/chrony.conf
sudo cp config/chrony-timestd-refclocks.conf /etc/hf-timestd/
echo "include /etc/hf-timestd/chrony-timestd-refclocks.conf" | sudo tee -a /etc/chrony/chrony.conf

# Update service ordering
sudo mkdir -p /etc/systemd/system/chronyd.service.d/
sudo cp systemd/chronyd-timestd-shm.conf /etc/systemd/system/chronyd.service.d/timestd-shm.conf
sudo systemctl daemon-reload
```

### 4. Restart Services
```bash
sudo systemctl restart timestd-l2-calibration
sudo systemctl restart timestd-fusion
sudo systemctl restart chronyd
```

### 5. Verify
```bash
# Check all services running
sudo systemctl status timestd-metrology timestd-l2-calibration timestd-fusion chronyd

# Check Chrony sees both feeds
chronyc sources -v

# Should see:
#  TSL1 (SHM 0) - L1 feed
#  TSL2 (SHM 1) - L2 feed (selected with *)
```

## ⚠️ Critical: Bootstrap vs Locked Time Authority

### Understanding the Two-Phase Model

| Phase | Time Authority | NTP Role |
|-------|---------------|----------|
| **Bootstrap** | NTP (from GPSDO) | Identifies which UTC minute we're in |
| **Locked** | HF tone arrivals | NTP not consulted — HF is ground truth |

**Key Point:** NTP is used **once** for initial orientation. After lock, the system derives time from HF signals, not NTP.

### The Circular Dependency Risk

If the bootstrap phase uses NTP, and chrony is configured to prefer TSL1/TSL2 over an external reference, a **circular dependency** can occur during bootstrap:

```
System Clock ← Chrony ← TSL2 ← Bootstrap ← NTP (System Clock)
     ↑________________________________________________|
```

**Symptoms:**
- TSL1/TSL2 show `+0ns` offset during bootstrap
- External GPSDO shows 10-50ms offset but is marked `x` (may be in error)
- Initial lock inherits system clock error

### The Solution

**If you have a GPSDO or stratum-1 server**, configure it as the preferred source:

```bash
# In /etc/chrony/chrony.conf:
server 192.168.0.202 iburst prefer   # Your GPSDO - MUST have 'prefer'
refclock SHM 0 refid TSL1 poll 4 precision 1e-3
refclock SHM 1 refid TSL2 poll 4 precision 1e-4
```

This ensures:
1. Bootstrap gets accurate initial orientation from GPSDO
2. After lock, HF measurements refine the offset independently
3. TSL feeds contribute to chrony as secondary sources

**If TSL is your only local reference**, use remote NTP pools as a sanity check:

```bash
# Ensure pool servers are included and not marked 'noselect'
pool pool.ntp.org iburst
```

### Verification

```bash
chronyc sources -v
# Your GPSDO should show '*' (selected), not 'x' (may be in error)
# TSL1/TSL2 should show '+' (combined) or '-' (not combined)
```

<!-- LOGS: fusion | filter: "chrony" -->

---

## Troubleshooting

### L2 Calibration Service Not Starting
```bash
# Check logs
sudo journalctl -u timestd-l2-calibration -n 100

# Common issues:
# - Missing L1 data (check metrology service)
# - Import errors (check Python environment)
# - Permission errors (check data directory ownership)
```

### Chrony Not Seeing Feeds
```bash
# Check SHM segments exist
ipcs -m | grep 4e54

# Should see two segments:
# 0x4e545030 (SHM 0 - TSL1)
# 0x4e545031 (SHM 1 - TSL2)

# If missing, restart fusion service
sudo systemctl restart timestd-fusion
```

### L1 and L2 Feeds Diverging
```bash
# Check L1-L2 difference
chronyc sources -v | grep TSL

# Large divergence (>10ms) indicates:
# - L2 calibration problem
# - Propagation model error
# - TEC correction issue

# Check L2 calibration logs
sudo journalctl -u timestd-l2-calibration -f
```

## Future Enhancements

1. **Separate L2 Fusion Path**
   - Currently both feeds use same fusion result
   - TODO: Add dedicated L2 fusion that reads L2 HDF5
   - Will enable true L1 vs L2 comparison

2. **Adaptive Precision**
   - Adjust SHM precision based on actual uncertainty
   - Better Chrony source selection

3. **L1-L2 Validation Metrics**
   - Automated monitoring of L1-L2 difference
   - Alert on large divergence
   - Track propagation correction quality

4. **Per-Broadcast L2 Output**
   - Individual L2 feeds per broadcast
   - Enable per-station ionospheric analysis
   - Support multi-station MLE

## References

- L2 Calibration Service: `src/hf_timestd/core/l2_calibration_service.py`
- Fusion Service: `src/hf_timestd/core/multi_broadcast_fusion.py`
- Chrony SHM Driver: `src/hf_timestd/core/chrony_shm.py`
- Propagation Solver: `src/hf_timestd/core/propagation_mode_solver.py`
