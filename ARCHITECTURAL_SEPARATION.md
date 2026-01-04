# Architectural Separation: Phase 2 vs Fusion

## Design Principle: Separation of Concerns

### Phase 2 Analytics (Per-Station Validation)
**Location:** `phase2_temporal_engine.py`, `transmission_time_solver.py`

**Inputs:**
- Raw IQ samples
- System time, RTP timestamp
- **Precise receiver coordinates from config** (38.918461°N, -92.127974°W)

**Processing:**
1. Tone detection → T_arrival
2. Station identification
3. Propagation mode solving with precise coordinates:
   - Calculate exact distance to each station (Haversine formula)
   - Compute geometric delay
   - Compute ionospheric delay (1/f² physics)
   - Validate propagation delay bounds (0-120ms)
   - Validate mode plausibility (E-layer daytime, distance-based)
4. Compute D_clock = T_arrival - T_propagation

**Outputs to HDF5:**
- `d_clock_ms`: System clock offset
- `propagation_delay_ms`: Computed propagation delay
- `propagation_mode`: '1F', '2F', 'GW', etc.
- `station`: WWV, WWVH, CHU, BPM
- `frequency_mhz`: Broadcast frequency
- `confidence`: 0-1 quality score

**Validation Level:** Individual measurement physics
- Is this propagation delay physically possible?
- Is this mode plausible for this distance/time?
- Is this measurement internally consistent?

---

### Fusion (Cross-Station Validation)
**Location:** `multi_broadcast_fusion.py`

**Inputs:**
- Pre-computed measurements from Phase 2 HDF5
- Each measurement already validated individually
- Receiver coordinates (optional, for VTEC refinement)

**Processing:**
1. Read measurements from all stations/frequencies
2. **Cross-station validation** (NEW - 2026-01-04):
   - Geographic ordering: CHU D_clock > WWV D_clock > WWVH D_clock
   - Rationale: Closer stations arrive earlier → larger D_clock
   - This is a **relative check**, not absolute distance calculation
3. Statistical agreement: Do stations agree on UTC within threshold?
4. Intra-station consistency: Do frequencies from same station agree?
5. Outlier rejection: Remove measurements that fail validation
6. Weighted fusion: Combine valid measurements
7. Kalman filtering: Smooth and track systematic offset
8. Optional VTEC refinement: Use GNSS data to improve ionospheric delays

**Outputs:**
- `d_clock_fused_ms`: Single best estimate of system clock offset
- `uncertainty_ms`: Combined uncertainty
- `quality_grade`: A/B/C/D based on uncertainty
- `consistency_flag`: OK, CROSS_STATION_DISAGREE, DISCRIMINATION_SUSPECT

**Validation Level:** Cross-station consistency
- Do measurements from different stations agree?
- Is the relative arrival order physically possible?
- Are measurements mutually consistent?

---

## Why This Separation Matters

### Prevents Duplication
- Phase 2 does precise propagation calculations ONCE per measurement
- Fusion doesn't recalculate - it validates and combines
- Each layer has a clear responsibility

### Enables Modularity
- Phase 2 can be improved (better propagation models) without changing Fusion
- Fusion can add new validation logic without changing Phase 2
- Clear data contract: HDF5 schema defines interface

### Catches Different Errors
- **Phase 2 catches:** Sign errors, wrap-around, impossible modes, bad SNR
- **Fusion catches:** Tone misidentification, cross-station disagreement, geographic violations

### Example: Tone Misidentification
**Phase 2 perspective:** 
- CHU measurement: D_clock = +6.8ms, propagation = 5ms, mode = 1F
- Individually valid ✅ (propagation reasonable, mode plausible)

**Fusion perspective:**
- CHU: +6.8ms, WWV: +3.2ms, WWVH: +0.0ms
- Geographic violation ❌ (CHU should be > WWV, but WWV > WWVH is wrong)
- Reject entire fusion cycle

---

## Configuration Usage

### Phase 2 Analytics
**Requires precise coordinates:** YES
- Used for exact distance calculations
- Used for propagation delay computation
- Read from: `/etc/hf-timestd/timestd-config.toml`
- Passed via: `--config` argument to analytics service

### Fusion
**Requires precise coordinates:** OPTIONAL
- Used for VTEC refinement (PhysicsPropagationModel)
- Geographic ordering check works without them (relative check only)
- Read from: `/etc/hf-timestd/timestd-config.toml` (if provided)
- Passed via: `--config` argument (should be added to systemd service)

---

## Lessons from 2026-01-04 Debugging

### What Went Wrong
1. Fusion was using hardcoded approximate coordinates (39.0, -98.0)
2. Geographic validation was never implemented
3. Tone misidentification contaminated Kalman filter

### Root Cause
- **Not architectural duplication** - each layer has distinct role
- **Missing validation** - fusion wasn't checking cross-station consistency
- **Missing config plumbing** - fusion service not reading config file

### The Fix
1. Added geographic ordering validation to fusion (cross-station check)
2. Skip Kalman updates when data quality suspect
3. Updated fusion to read coordinates from config (for VTEC refinement)
4. Systemd service should pass `--config` argument

---

## Summary

**Phase 2:** "Is this measurement physically possible?"
**Fusion:** "Are these measurements mutually consistent?"

Both layers are necessary. Neither is redundant.
