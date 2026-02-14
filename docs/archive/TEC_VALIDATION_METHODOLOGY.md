# TEC Validation Methodology - Geometric Corrections and GPS Comparison

## The Volume Mismatch Problem

**Challenge**: HF-derived TEC and GPS TEC measure different volumes of the ionosphere.

- **GPS (ScintPI)**: Measures **Vertical TEC (VTEC)** directly above the receiver (local vertical)
- **HF Radar**: Measures **Slant TEC** along the oblique path to the reflection point (midpoint)

**Critical Insight**: You cannot directly compare HF TEC at your receiver location with GPS TEC at your receiver location. They are "looking at different skies."

---

## Validation Strategy

### 1. Geometric Mapping

**Convert HF Slant TEC to Vertical TEC at the Reflection Point**

#### Obliquity Factor Conversion

The obliquity factor (M) maps slant TEC to vertical TEC:

```
M = 1 / cos(arcsin((R_E * cos(θ)) / (R_E + h_m)))

Where:
  R_E = Earth radius (6371 km)
  h_m = Effective ionospheric height (typically 350 km)
  θ = Elevation angle at receiver
```

**Conversion**:

```
VTEC_HF = TEC_slant / M
```

#### Implementation Location

**File**: `src/hf_timestd/core/science_aggregator.py`

Add method:

```python
def convert_slant_to_vertical(self, tec_slant, elevation_angle_deg, h_iono=350.0):
    """
    Convert slant TEC to vertical TEC using obliquity factor.
    
    Args:
        tec_slant: Measured slant TEC (TECU)
        elevation_angle_deg: Elevation angle at receiver (degrees)
        h_iono: Ionospheric height (km, default 350)
    
    Returns:
        vtec: Vertical TEC at reflection point (TECU)
    """
    import math
    
    R_E = 6371.0  # Earth radius (km)
    theta_rad = math.radians(elevation_angle_deg)
    
    # Obliquity factor
    sin_term = (R_E * math.cos(theta_rad)) / (R_E + h_iono)
    M = 1.0 / math.cos(math.asin(sin_term))
    
    vtec = tec_slant / M
    
    return vtec, M
```

---

### 2. Reflection Point Calculation

**Midpoint Coordinates** (already implemented in `propagation_engine.py`):

```python
def calculate_midpoint(lat1, lon1, lat2, lon2):
    """Calculate great circle midpoint between receiver and transmitter."""
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Midpoint calculation
    Bx = math.cos(lat2_rad) * math.cos(lon2_rad - lon1_rad)
    By = math.cos(lat2_rad) * math.sin(lon2_rad - lon1_rad)
    
    lat_mid = math.atan2(
        math.sin(lat1_rad) + math.sin(lat2_rad),
        math.sqrt((math.cos(lat1_rad) + Bx)**2 + By**2)
    )
    lon_mid = lon1_rad + math.atan2(By, math.cos(lat1_rad) + Bx)
    
    return math.degrees(lat_mid), math.degrees(lon_mid)
```

**For each station**, calculate:

- Midpoint latitude/longitude
- Elevation angle (from `transmission_time_solver.py`)

---

### 3. GPS TEC Reference Data

#### Option A: IONEX Maps (Global Grid)

**Source**: NASA CDDIS or MIT Madrigal

**Download**:

```bash
# Example: JPL Global Ionosphere Maps (GIM)
# URL format: https://cddis.nasa.gov/archive/gnss/products/ionex/YYYY/DDD/
# File: jplgDDD0.YYi.Z (where DDD = day of year, YY = year)

wget https://cddis.nasa.gov/archive/gnss/products/ionex/2025/357/jplg3570.25i.Z
gunzip jplg3570.25i.Z
```

**IONEX Format**:

- 2.5° × 5° grid (latitude × longitude)
- 2-hour cadence (12 maps per day)
- VTEC in TECU

**Interpolation**:

```python
def interpolate_ionex_vtec(ionex_file, lat, lon, timestamp):
    """
    Interpolate VTEC from IONEX map at specific location and time.
    
    Args:
        ionex_file: Path to IONEX file
        lat: Latitude (degrees)
        lon: Longitude (degrees)
        timestamp: UTC timestamp
    
    Returns:
        vtec_gps: GPS-derived VTEC at location (TECU)
    """
    # Parse IONEX file (use existing library like georinex or custom parser)
    # Bilinear interpolation in space
    # Linear interpolation in time
    # Return VTEC value
    pass
```

#### Option B: ScintPI Local Receiver (Bias Correction)

**Use Case**: Anchor IONEX maps with local ground truth

**Method**:

1. Compare ScintPI VTEC (local vertical) with IONEX VTEC at receiver location
2. Calculate bias: `bias = VTEC_ScintPI - VTEC_IONEX`
3. Apply bias correction to IONEX midpoint value: `VTEC_corrected = VTEC_IONEX_midpoint + bias`

**Rationale**: If IONEX underestimates your local area by 5 TECU, it likely underestimates the midpoint by a similar amount.

---

### 4. Comparison Metrics

#### Primary Validation

**Correlation Analysis**:

```python
# Compare HF VTEC vs GPS VTEC at midpoint
correlation = np.corrcoef(vtec_hf, vtec_gps)[0, 1]
rms_error = np.sqrt(np.mean((vtec_hf - vtec_gps)**2))
mean_bias = np.mean(vtec_hf - vtec_gps)
```

**Success Criteria** (from VALIDATION_PLAN.md):

- R² > 0.7
- RMS error < 10 TECU
- Diurnal pattern match

#### Expected Discrepancies (Physics, Not Errors)

**1. Bottomside Bias**:

- **GPS**: Measures entire column (0-20,000 km altitude)
- **HF**: Only bottomside ionosphere (0-350 km peak)
- **Expected**: `VTEC_HF ≈ 0.6-0.8 × VTEC_GPS`

**Slab Thickness Calculation**:

```
τ = VTEC_GPS / VTEC_HF
```

- τ > 1: Indicates plasmasphere contribution
- Typical: τ = 1.2-1.5

**2. TID Sensitivity**:

- **GPS Maps**: Smoothed (1-2 hour cadence)
- **HF**: Real-time (1-minute cadence)
- **Expected**: HF shows TID oscillations invisible in GPS maps

---

## Validation Workflow

### Step 1: Extract HF Measurements

**Input**: `phase2/science/tec/tec_YYYYMMDD.csv`

**Extract**:

- `timestamp_utc`
- `station` (WWV, WWVH, CHU, BPM)
- `tec_tecu` (slant TEC)
- `confidence`

### Step 2: Calculate Geometry

**For each station**:

```python
# Receiver location (from config)
rx_lat = 38.918461
rx_lon = -92.127974

# Transmitter location (from STATIONS dict)
tx_lat, tx_lon = STATIONS[station]['lat'], STATIONS[station]['lon']

# Midpoint
mid_lat, mid_lon = calculate_midpoint(rx_lat, rx_lon, tx_lat, tx_lon)

# Elevation angle (from propagation_engine or transmission_time_solver)
elevation_deg = calculate_elevation_angle(rx_lat, rx_lon, tx_lat, tx_lon)
```

### Step 3: Convert to VTEC

```python
vtec_hf, obliquity_factor = convert_slant_to_vertical(
    tec_slant=tec_tecu,
    elevation_angle_deg=elevation_deg,
    h_iono=350.0
)
```

### Step 4: Fetch GPS Reference

**Option A: IONEX**

```python
vtec_gps = interpolate_ionex_vtec(
    ionex_file='jplg3570.25i',
    lat=mid_lat,
    lon=mid_lon,
    timestamp=timestamp_utc
)
```

**Option B: ScintPI + IONEX**

```python
# Get local ScintPI VTEC
vtec_scintpi_local = read_scintpi_data(timestamp_utc)

# Get IONEX at receiver location
vtec_ionex_local = interpolate_ionex_vtec(ionex_file, rx_lat, rx_lon, timestamp_utc)

# Calculate bias
bias = vtec_scintpi_local - vtec_ionex_local

# Get IONEX at midpoint and apply bias correction
vtec_ionex_midpoint = interpolate_ionex_vtec(ionex_file, mid_lat, mid_lon, timestamp_utc)
vtec_gps = vtec_ionex_midpoint + bias
```

### Step 5: Compare and Validate

```python
# Calculate metrics
correlation = np.corrcoef(vtec_hf, vtec_gps)[0, 1]
rms_error = np.sqrt(np.mean((vtec_hf - vtec_gps)**2))
mean_bias = np.mean(vtec_hf - vtec_gps)
slab_thickness = np.mean(vtec_gps / vtec_hf)

# Plot
plt.figure(figsize=(12, 6))
plt.plot(timestamps, vtec_hf, label='HF VTEC (Midpoint)', marker='o')
plt.plot(timestamps, vtec_gps, label='GPS VTEC (IONEX)', marker='s')
plt.xlabel('Time (UTC)')
plt.ylabel('VTEC (TECU)')
plt.title(f'TEC Validation: {station} - R²={correlation**2:.3f}, RMS={rms_error:.2f} TECU')
plt.legend()
plt.grid(True)
```

---

## Implementation Plan

### Phase 1: Geometric Corrections (Week 1)

- [ ] Add `convert_slant_to_vertical()` to Science Aggregator
- [ ] Calculate midpoint coordinates for each station
- [ ] Store VTEC (not just slant TEC) in output CSV
- [ ] Add obliquity factor to metadata

### Phase 2: IONEX Integration (Week 2)

- [ ] Download IONEX files for validation period
- [ ] Implement IONEX parser (or use `georinex` library)
- [ ] Implement bilinear interpolation
- [ ] Create validation script

### Phase 3: ScintPI Integration (Week 3)

- [ ] Read ScintPI VTEC data
- [ ] Implement local bias correction
- [ ] Compare all three: HF, IONEX, ScintPI

### Phase 4: Analysis (Week 4)

- [ ] Calculate slab thickness
- [ ] Identify TID events in HF data
- [ ] Compare with GPS smoothed maps
- [ ] Publish validation report

---

## Expected Results

### Quantitative Validation

- **R² > 0.7**: Strong correlation with GPS TEC
- **RMS < 10 TECU**: Acceptable accuracy
- **Bias ≈ -20% to -40%**: Expected bottomside-only measurement

### Qualitative Findings

- **Slab Thickness**: τ = 1.2-1.5 (plasmasphere contribution)
- **TID Detection**: HF reveals oscillations invisible in GPS maps
- **Diurnal Pattern**: HF and GPS show same sunrise/sunset transitions

### Scientific Contribution

- **Bottomside Ionosphere**: HF provides high-cadence bottomside TEC
- **TID Monitoring**: Real-time gravity wave detection
- **Validation**: Cross-validates GPS maps at midpoint locations

---

## Data Output Format

**Enhanced TEC CSV** (with VTEC):

```csv
timestamp_utc,station,tec_slant_tecu,vtec_midpoint_tecu,obliquity_factor,midpoint_lat,midpoint_lon,elevation_deg,confidence
2025-12-23T12:00:00Z,WWV,30.5,25.3,1.21,40.5,-100.2,15.3,0.92
```

**Validation Report CSV**:

```csv
timestamp_utc,station,vtec_hf,vtec_gps,vtec_scintpi,bias,slab_thickness
2025-12-23T12:00:00Z,WWV,25.3,32.1,33.5,1.4,1.27
```

---

## References

- **IONEX Format**: ftp://cddis.gsfc.nasa.gov/pub/gps/data/daily/YYYY/DDD/YYi/
- **Obliquity Factor**: Davies, K. (1990). *Ionospheric Radio*. IEE Electromagnetic Waves Series.
- **Slab Thickness**: Jakowski et al. (2011). "TEC and slab thickness of the ionosphere."
- **ScintPI**: <https://github.com/jswoboda/scintpi>

---

## Summary

**The Validation Process**:

1. Convert HF slant TEC → VTEC at midpoint (obliquity factor)
2. Fetch GPS VTEC at midpoint (IONEX maps)
3. Apply ScintPI local bias correction (optional)
4. Compare: Expect HF ≈ 60-80% of GPS (bottomside only)
5. Analyze discrepancies: Slab thickness, TID sensitivity

**This is not a "calibration" but a "cross-validation"** - both systems measure real physics, just different volumes.
