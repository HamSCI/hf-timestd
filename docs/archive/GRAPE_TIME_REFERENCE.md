# GRAPE as a Time Reference Source

## Vision

Once GRAPE's D_clock analysis converges on UTC(NIST), the system can become a
**stratum-1 time reference** for other processes on the local machine or network.

This transforms a GRAPE receiver from a passive time consumer into an active
time source traceable to UTC(NIST).

---

## Time Accuracy Comparison

| Source | Typical Accuracy | Notes |
|--------|------------------|-------|
| UTC(NIST) | Definition | Primary standard |
| GPS PPS | ±10 ns | Requires antenna, hardware |
| WWV/WWVH (TX) | ±100 μs | At transmitter |
| NTP (Internet) | ±1-50 ms | Network-dependent |
| NTP (LAN stratum-1) | ±100 μs | Requires local server |
| **GRAPE D_clock** | ±100 μs - 1 ms | After convergence & calibration |

## How D_clock Becomes a Time Reference

### 1. Propagation Delay Decomposition

```
D_clock_measured = D_clock_true + D_propagation + D_ionosphere + D_receiver

Where:
  D_clock_true     = Actual system clock offset from UTC(NIST)
  D_propagation    = Speed-of-light path delay (calculable from coordinates)
  D_ionosphere     = Variable ionospheric delay (1-50ms, frequency-dependent)
  D_receiver       = Fixed receiver pipeline delay (calibratable)
```

### 2. Extracting True Clock Offset

```python
# Known/calculable components
D_geometric = calculate_path_delay(station_coords, transmitter_coords)  # ~10-30ms
D_receiver = CALIBRATED_RECEIVER_DELAY  # ~50μs, measured once

# Ionospheric delay estimation (frequency-dependent)
# Higher frequencies → lower ionospheric delay
D_iono_estimate = ionospheric_model(frequency_mhz, time_of_day, solar_activity)

# True clock offset
D_clock_true = D_clock_measured - D_geometric - D_iono_estimate - D_receiver
```

### 3. Multi-Frequency Fusion

Different frequencies experience different ionospheric delays:
- 2.5 MHz: High delay, high variability
- 25 MHz: Low delay, low variability (but weaker signal)

Fusing measurements across frequencies improves accuracy:

```python
# Ionospheric delay is proportional to 1/f²
# Using two frequencies allows solving for the ionospheric component
D_iono = K * (1/f1² - 1/f2²)  # K determined from D_clock difference
```

---

## Implementation: chrony Reference Clock

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      GRAPE Phase 2 Engine                       │
│  D_clock analysis → Converged UTC(NIST) offset estimate        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ Write to SHM
┌─────────────────────────────────────────────────────────────────┐
│                    Shared Memory Segment                        │
│  /dev/shm/GRAPE_REFCLOCK (NTP SHM format)                      │
│  Fields: valid, count, clock_sec, clock_usec, receive_sec...   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ Read by chrony
┌─────────────────────────────────────────────────────────────────┐
│                         chrony daemon                           │
│  refclock SHM 0 offset 0.0 delay 0.001 refid GRAP              │
│  Disciplines system clock using GRAPE + NTP sources            │
└─────────────────────────────────────────────────────────────────┘
```

### chrony Configuration

Add to `/etc/chrony/chrony.conf`:

```conf
# GRAPE WWV/WWVH reference clock via shared memory
# SHM segment 0, GRAPE provides offset estimate
refclock SHM 0 refid GRAP offset 0.0 delay 0.001 precision 1e-4 poll 4 filter 64

# Standard NTP servers as backup/validation
pool pool.ntp.org iburst

# Allow GRAPE to be weighted appropriately
# Higher stratum initially until confidence builds
# Stratum can be lowered as GRAPE proves accuracy
```

### SHM Data Structure (NTP SHM Protocol)

```c
struct shmTime {
    int    mode;           // 0 = invalid, 1 = valid
    volatile int count;    // Incremented on each update
    time_t clockTimeStampSec;   // Reference time (seconds)
    int    clockTimeStampUSec;  // Reference time (microseconds)
    time_t receiveTimeStampSec; // System time when sample taken
    int    receiveTimeStampUSec;
    int    leap;           // Leap second indicator
    int    precision;      // Clock precision (log2 seconds)
    int    nsamples;       // Number of samples averaged
    volatile int valid;    // Data validity flag
};
```

### Python Writer for GRAPE

```python
"""
GRAPE Time Reference Writer

Writes D_clock estimates to chrony-compatible shared memory,
allowing GRAPE to discipline the system clock.
"""

import mmap
import struct
import time
from dataclasses import dataclass
from typing import Optional
import os

# SHM segment for chrony refclock
SHM_KEY = 0x4e545030  # 'NTP0'
SHM_SIZE = 96


@dataclass
class GrapeTimeReference:
    """
    GRAPE time reference for chrony integration.
    
    Writes UTC(NIST) offset estimates to shared memory,
    allowing chrony to use GRAPE as a reference clock.
    """
    
    # Confidence thresholds
    MIN_SAMPLES_FOR_VALID = 10      # Minimum samples before declaring valid
    MAX_UNCERTAINTY_MS = 5.0        # Maximum uncertainty for valid reference
    CONVERGENCE_THRESHOLD_MS = 1.0  # Considered converged below this
    
    def __init__(self, shm_unit: int = 0):
        """
        Initialize GRAPE time reference writer.
        
        Args:
            shm_unit: SHM unit number (0-3 supported by chrony)
        """
        self.shm_unit = shm_unit
        self.shm_path = f"/dev/shm/NTP{shm_unit}"
        self.shm = None
        self.sample_count = 0
        self.valid = False
        self.last_offset_ms = None
        self.uncertainty_ms = float('inf')
        
        self._init_shm()
    
    def _init_shm(self):
        """Initialize shared memory segment."""
        try:
            # Create SHM file if it doesn't exist
            if not os.path.exists(self.shm_path):
                with open(self.shm_path, 'wb') as f:
                    f.write(b'\x00' * SHM_SIZE)
            
            # Memory-map the file
            with open(self.shm_path, 'r+b') as f:
                self.shm = mmap.mmap(f.fileno(), SHM_SIZE)
            
            # Initialize to invalid
            self._write_invalid()
            
        except Exception as e:
            raise RuntimeError(f"Failed to initialize SHM: {e}")
    
    def update(
        self,
        d_clock_ms: float,
        uncertainty_ms: float,
        sample_time: float,
        num_frequencies: int = 1
    ):
        """
        Update the time reference with a new D_clock estimate.
        
        Args:
            d_clock_ms: D_clock offset in milliseconds (system - UTC(NIST))
            uncertainty_ms: Estimated uncertainty in milliseconds
            sample_time: System time when measurement was taken
            num_frequencies: Number of frequencies used in estimate
        """
        self.sample_count += 1
        self.last_offset_ms = d_clock_ms
        self.uncertainty_ms = uncertainty_ms
        
        # Check if we're valid and converged
        is_valid = (
            self.sample_count >= self.MIN_SAMPLES_FOR_VALID and
            uncertainty_ms <= self.MAX_UNCERTAINTY_MS
        )
        
        is_converged = uncertainty_ms <= self.CONVERGENCE_THRESHOLD_MS
        
        if is_valid:
            self._write_sample(d_clock_ms, sample_time, is_converged)
            self.valid = True
        else:
            self._write_invalid()
            self.valid = False
    
    def _write_sample(self, offset_ms: float, sample_time: float, converged: bool):
        """Write a valid sample to SHM."""
        if self.shm is None:
            return
        
        # Calculate reference time (when WWV tick occurred)
        # System time minus offset = UTC(NIST) time
        ref_time = sample_time - (offset_ms / 1000.0)
        ref_sec = int(ref_time)
        ref_usec = int((ref_time - ref_sec) * 1_000_000)
        
        # Receive time (when system recorded the sample)
        recv_sec = int(sample_time)
        recv_usec = int((sample_time - recv_sec) * 1_000_000)
        
        # Precision: log2 of uncertainty in seconds
        # e.g., 1ms = 10^-3, log2(10^-3) ≈ -10
        import math
        precision = int(math.log2(self.uncertainty_ms / 1000.0)) if self.uncertainty_ms > 0 else -20
        precision = max(-20, min(-1, precision))  # Clamp to reasonable range
        
        # Pack the structure
        # Format: mode(i), count(i), clockSec(q), clockUsec(i), 
        #         recvSec(q), recvUsec(i), leap(i), precision(i), nsamples(i), valid(i)
        data = struct.pack(
            'iiqiqi iiii',
            1,  # mode = valid
            self.sample_count,
            ref_sec,
            ref_usec,
            recv_sec,
            recv_usec,
            0,  # leap = none
            precision,
            1,  # nsamples
            1   # valid
        )
        
        self.shm.seek(0)
        self.shm.write(data)
        self.shm.flush()
    
    def _write_invalid(self):
        """Mark the SHM as invalid."""
        if self.shm is None:
            return
        
        self.shm.seek(0)
        self.shm.write(struct.pack('i', 0))  # mode = invalid
        self.shm.flush()
    
    def get_status(self) -> dict:
        """Get current status of the time reference."""
        return {
            'valid': self.valid,
            'converged': self.uncertainty_ms <= self.CONVERGENCE_THRESHOLD_MS,
            'sample_count': self.sample_count,
            'last_offset_ms': self.last_offset_ms,
            'uncertainty_ms': self.uncertainty_ms,
            'shm_unit': self.shm_unit,
        }
    
    def close(self):
        """Close the SHM mapping."""
        if self.shm:
            self._write_invalid()
            self.shm.close()
            self.shm = None
```

---

## Convergence Strategy

### Phase 1: Bootstrap (first 10 minutes)
- Use NTP for system time
- Collect D_clock samples across frequencies
- Build initial propagation delay estimates
- **Status:** `GRAPE: INITIALIZING`

### Phase 2: Calibration (10-60 minutes)
- Compare D_clock trends across frequencies
- Estimate ionospheric component
- Validate against expected propagation delays
- **Status:** `GRAPE: CALIBRATING`

### Phase 3: Tracking (steady state)
- Continuous D_clock monitoring
- Kalman filter for offset estimation
- Multi-frequency fusion
- **Status:** `GRAPE: TRACKING (±0.5ms)`

### Phase 4: Reference (high confidence)
- Uncertainty below 1ms consistently
- chrony using GRAPE as primary source
- **Status:** `GRAPE: REFERENCE (stratum 1)`

---

## Safety Considerations

### 1. Never Worse Than NTP
- chrony weights sources by their uncertainty
- If GRAPE uncertainty is high, NTP dominates
- GRAPE only influences clock when it's more accurate

### 2. Gradual Transition
- Don't immediately trust GRAPE over NTP
- Require sustained accuracy before promotion
- Allow operator to set confidence thresholds

### 3. Fallback Mechanism
- If GRAPE stops updating, chrony falls back to NTP
- Mark SHM invalid if no updates for 5 minutes
- Log all state transitions

### 4. Sanity Checks
- Reject D_clock values outside expected range (±100ms)
- Require agreement between frequencies
- Monitor for sudden jumps (likely errors, not real)

---

## Future: Network Time Server

Once GRAPE is a validated stratum-1 source locally, it can serve time to:

1. **Local Network** - NTP server for other machines
2. **GRAPE Network** - Cross-validate between GRAPE stations
3. **Research** - Contribute to ionospheric timing studies

```conf
# Allow local network to use this GRAPE station as NTP server
allow 192.168.0.0/16
allow 10.0.0.0/8
```

---

## Implementation Roadmap

1. **Phase 2 Integration** - Add D_clock uncertainty estimation
2. **SHM Writer** - Implement GrapeTimeReference class
3. **chrony Config** - Document setup procedure
4. **Monitoring** - Dashboard showing convergence status
5. **Validation** - Compare GRAPE time vs GPS PPS reference
6. **Network Mode** - Serve time to local network
