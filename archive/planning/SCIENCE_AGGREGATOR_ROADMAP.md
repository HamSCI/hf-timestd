# Science Aggregator Implementation Roadmap

**Date:** January 2, 2026  
**Goal:** Achieve scientific capabilities documented in SCIENTIFIC_CAPABILITIES.md  
**Approach:** Incremental implementation with HDF5-first design

---

## Design Philosophy

1. **HDF5-First:** All new features write to HDF5 with proper schemas
2. **Remove CSV:** Complete transition away from CSV dual-write
3. **Incremental:** Implement capabilities in priority order
4. **Validated:** Each feature includes validation framework
5. **Modular:** Each capability is a separate, testable component

---

## Phase 1: Foundation & HDF5 Completion (Week 1-2)

### 1.1 Fix TEC Input Bug ✅ COMPLETED
- **Status:** Fixed in this session
- **Change:** Read from channel root directory, not clock_offset subdirectory
- **Test:** Restart service, verify TEC updates every 5 minutes

### 1.2 Create Missing HDF5 Schemas
Create schemas for new science products:

**L3B: Ionospheric Events** (`l3b_iono_events_v1.json`)
```json
{
  "schema_version": "1.0.0",
  "data_product": "L3B_iono_events",
  "description": "Detected ionospheric events and disturbances",
  "processing_level": "L3B",
  "fields": [
    {"name": "timestamp_utc", "type": "string", "required": true},
    {"name": "event_start", "type": "string", "required": true},
    {"name": "event_end", "type": "string", "required": true},
    {"name": "event_type", "type": "string", "required": true, 
     "enum": ["TID", "SPORADIC_E", "SID", "SPREAD_F", "SCINTILLATION"]},
    {"name": "severity", "type": "float", "required": true, "valid_range": [0, 1]},
    {"name": "confidence", "type": "float", "required": true, "valid_range": [0, 1]},
    {"name": "affected_stations", "type": "string", "required": true},
    {"name": "affected_frequencies_mhz", "type": "string", "required": true},
    {"name": "peak_value", "type": "float", "required": false},
    {"name": "description", "type": "string", "required": true}
  ]
}
```

**L3B: D-Layer Absorption** (`l3b_absorption_v1.json`)
```json
{
  "schema_version": "1.0.0",
  "data_product": "L3B_absorption",
  "description": "D-layer absorption measurements and SID detection",
  "processing_level": "L3B",
  "fields": [
    {"name": "timestamp_utc", "type": "string", "required": true},
    {"name": "minute_boundary", "type": "integer", "required": true},
    {"name": "station", "type": "string", "required": true},
    {"name": "frequency_mhz", "type": "float", "required": true},
    {"name": "snr_db", "type": "float", "required": true},
    {"name": "absorption_db", "type": "float", "required": true},
    {"name": "solar_zenith_angle_deg", "type": "float", "required": true},
    {"name": "expected_snr_db", "type": "float", "required": false},
    {"name": "anomaly_flag", "type": "string", "required": true,
     "enum": ["NORMAL", "SID", "ENHANCED", "DEGRADED"]},
    {"name": "quality_flag", "type": "string", "required": true}
  ]
}
```

**L3C: Propagation Statistics** (`l3c_propagation_stats_v1.json`)
```json
{
  "schema_version": "1.0.0",
  "data_product": "L3C_propagation_stats",
  "description": "Aggregated propagation mode statistics",
  "processing_level": "L3C",
  "fields": [
    {"name": "timestamp_utc", "type": "string", "required": true},
    {"name": "aggregation_period", "type": "string", "required": true,
     "enum": ["HOURLY", "DAILY", "MONTHLY"]},
    {"name": "station", "type": "string", "required": true},
    {"name": "frequency_mhz", "type": "float", "required": true},
    {"name": "mode_1e_probability", "type": "float", "required": true},
    {"name": "mode_1f_probability", "type": "float", "required": true},
    {"name": "mode_2f_probability", "type": "float", "required": true},
    {"name": "mode_3f_probability", "type": "float", "required": true},
    {"name": "mode_gw_probability", "type": "float", "required": true},
    {"name": "estimated_muf_mhz", "type": "float", "required": false},
    {"name": "n_observations", "type": "integer", "required": true}
  ]
}
```

### 1.3 Remove CSV Dual-Write
- **File:** `science_aggregator.py:306-415`
- **Change:** Remove CSV writing, keep HDF5 only
- **Timing:** After 1 week of stable HDF5 TEC updates
- **Keep:** CSV fallback for reading (legacy data support)

**Implementation:**
```python
def _write_tec_results(self, date_str: str, results: List[Tuple]):
    """Write TEC results to HDF5 only (CSV removed)."""
    
    from hf_timestd.io import DataProductWriter
    
    writer = DataProductWriter(
        output_dir=self.tec_dir,
        product_level='L3',
        product_name='tec',
        channel='AGGREGATED',
        processing_version='3.3.0'  # Bump version
    )
    
    for station, minute_boundary, tec_result, measurements in results:
        # ... (existing HDF5 write code)
        writer.write_measurement(measurement)
    
    writer.close()
    logger.info(f"Wrote {len(results)} TEC results to HDF5")
    # CSV code removed
```

---

## Phase 2: High-Value Quick Wins (Week 2-3)

### 2.1 Propagation Mode Statistics
**Priority:** HIGH (easy win - data exists)  
**Effort:** LOW  
**Scientific Value:** MEDIUM

**Implementation:**
1. Read `propagation_mode` from L2 timing measurements
2. Aggregate by hour/day/month
3. Calculate mode probabilities
4. Estimate MUF from highest F-layer frequency
5. Write to L3C HDF5

**New Module:** `src/hf_timestd/core/propagation_stats.py`
```python
class PropagationStatistics:
    """Aggregate propagation mode statistics."""
    
    def __init__(self, paths: TimeStdPaths):
        self.paths = paths
        
    def aggregate_modes(
        self, 
        start_time: datetime, 
        end_time: datetime,
        period: str = 'HOURLY'
    ) -> List[Dict]:
        """
        Aggregate propagation modes over time period.
        
        Returns statistics per (station, frequency, period).
        """
        # Read L2 timing measurements
        # Group by station, frequency, time period
        # Count mode occurrences
        # Calculate probabilities
        # Estimate MUF
        pass
```

**Integration:** Add to `science_aggregator.py` main loop:
```python
def run(self):
    while self.running:
        try:
            self._aggregate_tec()
            self._aggregate_propagation_stats()  # NEW
            time.sleep(self.poll_interval)
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
```

### 2.2 TEC Validation Against IONEX
**Priority:** HIGH (needed for scientific use)  
**Effort:** LOW (VTEC service exists)  
**Scientific Value:** HIGH

**Implementation:**
1. Read VTEC data from vtec service
2. Compare with HF-derived TEC
3. Calculate correlation, bias, RMSE
4. Add validation fields to TEC schema
5. Flag TEC estimates as validated/unvalidated

**New Module:** `src/hf_timestd/core/tec_validator.py`
```python
class TECValidator:
    """Validate HF TEC against GPS VTEC."""
    
    def __init__(self, paths: TimeStdPaths):
        self.paths = paths
        
    def validate_tec(
        self,
        hf_tec: float,
        station: str,
        timestamp: float
    ) -> Dict:
        """
        Compare HF TEC with GPS VTEC.
        
        Returns validation metrics.
        """
        # Read VTEC for station location and time
        # Calculate pierce point
        # Apply obliquity correction
        # Compare values
        # Return correlation, bias, RMSE
        pass
```

**Schema Update:** Add to `l3_tec_v1.json`:
```json
{
  "name": "vtec_tecu",
  "type": "float",
  "required": false,
  "description": "GPS VTEC for comparison"
},
{
  "name": "tec_bias_tecu",
  "type": "float",
  "required": false,
  "description": "HF TEC - GPS VTEC"
},
{
  "name": "validation_flag",
  "type": "string",
  "required": true,
  "enum": ["VALIDATED", "UNVALIDATED", "VTEC_UNAVAILABLE"]
}
```

---

## Phase 3: D-Layer Absorption (Week 3-4)

### 3.1 SNR Aggregation
**Priority:** HIGH (marked "high confidence" in capabilities doc)  
**Effort:** MEDIUM  
**Scientific Value:** HIGH

**Implementation:**
1. Read SNR from L1A channel observables
2. Aggregate across frequencies per minute
3. Calculate solar zenith angle for each path
4. Compute expected SNR (baseline model)
5. Calculate absorption = expected - observed
6. Detect SIDs (sudden drops > threshold)

**New Module:** `src/hf_timestd/core/absorption_analyzer.py`
```python
class AbsorptionAnalyzer:
    """Analyze D-layer absorption and detect SIDs."""
    
    def __init__(self, paths: TimeStdPaths):
        self.paths = paths
        self.baseline_model = None  # Learn from historical data
        
    def calculate_solar_zenith_angle(
        self,
        station_lat: float,
        station_lon: float,
        timestamp: float
    ) -> float:
        """Calculate solar zenith angle at path midpoint."""
        # Use astropy or similar
        pass
        
    def calculate_absorption(
        self,
        snr_db: float,
        frequency_mhz: float,
        solar_zenith_angle: float,
        time_of_day: str
    ) -> float:
        """
        Calculate D-layer absorption.
        
        Uses frequency dependence: A(f) ∝ f^-n where n ≈ 1.5-2
        """
        # Get expected SNR from baseline model
        # Calculate absorption = expected - observed
        pass
        
    def detect_sid(
        self,
        snr_time_series: List[float],
        timestamps: List[float]
    ) -> Optional[Dict]:
        """
        Detect Sudden Ionospheric Disturbance.
        
        SID signature: Sudden SNR drop (>10 dB), recovery over 30-60 min
        """
        # Look for sudden drops
        # Verify recovery pattern
        # Calculate event timing and severity
        pass
```

**Integration:**
```python
def _aggregate_absorption(self):
    """Aggregate SNR and detect absorption events."""
    
    # Read SNR from L1A for all channels
    # Calculate solar zenith angles
    # Compute absorption
    # Detect SIDs
    # Write to L3B HDF5
    pass
```

---

## Phase 4: Sporadic-E Detection (Week 4-5)

### 4.1 Es Event Detection
**Priority:** MEDIUM  
**Effort:** MEDIUM  
**Scientific Value:** HIGH

**Implementation:**
1. Monitor SNR at 10-15 MHz for sudden increases (>10 dB)
2. Check for mode change to 1E
3. Measure event duration
4. Estimate foEs (critical frequency)
5. Write events to L3B HDF5

**New Module:** `src/hf_timestd/core/sporadic_e_detector.py`
```python
class SporadicEDetector:
    """Detect sporadic-E events."""
    
    def __init__(self, paths: TimeStdPaths):
        self.paths = paths
        self.baseline_snr = {}  # Track normal SNR levels
        
    def detect_es_event(
        self,
        snr_time_series: Dict[float, List[float]],  # freq -> SNR list
        mode_time_series: Dict[float, List[str]],   # freq -> mode list
        timestamps: List[float]
    ) -> Optional[Dict]:
        """
        Detect sporadic-E event.
        
        Signature:
        - Sudden SNR increase at 10-15 MHz (>10 dB)
        - Mode change to 1E
        - Duration: minutes to hours
        """
        # Focus on 10-15 MHz
        # Look for SNR increases
        # Check mode changes to 1E
        # Estimate foEs from highest frequency with Es
        pass
        
    def estimate_foes(
        self,
        frequencies_mhz: List[float],
        es_detected: List[bool]
    ) -> float:
        """
        Estimate sporadic-E critical frequency.
        
        foEs = highest frequency with Es propagation
        """
        pass
```

**Integration:**
```python
def _detect_sporadic_e(self):
    """Detect sporadic-E events."""
    
    # Read SNR and modes from recent data
    # Run Es detector
    # Write events to L3B HDF5
    pass
```

---

## Phase 5: TID Detection (Week 5-7)

### 5.1 Replace Placeholder with Real Implementation
**Priority:** MEDIUM  
**Effort:** HIGH (sophisticated signal processing)  
**Scientific Value:** HIGH

**Implementation:**
1. Aggregate Doppler time series across frequencies
2. Apply bandpass filter for TID periods (15-60 min)
3. Detect coherent oscillations using cross-correlation
4. Estimate phase velocity from multi-frequency delays
5. Correlate with geomagnetic indices
6. Write events to L3B HDF5

**New Module:** `src/hf_timestd/core/tid_detector.py`
```python
import numpy as np
from scipy import signal
from scipy.fft import fft, fftfreq

class TIDDetector:
    """Detect Traveling Ionospheric Disturbances."""
    
    def __init__(self, paths: TimeStdPaths):
        self.paths = paths
        self.tid_period_range = (15*60, 60*60)  # 15-60 minutes in seconds
        
    def preprocess_doppler(
        self,
        doppler_time_series: np.ndarray,
        timestamps: np.ndarray
    ) -> np.ndarray:
        """
        Preprocess Doppler data for TID detection.
        
        - Remove linear trend
        - Apply bandpass filter (15-60 min periods)
        """
        # Detrend
        detrended = signal.detrend(doppler_time_series)
        
        # Bandpass filter
        fs = 1.0 / np.mean(np.diff(timestamps))  # Sampling frequency
        low_freq = 1.0 / self.tid_period_range[1]
        high_freq = 1.0 / self.tid_period_range[0]
        
        sos = signal.butter(4, [low_freq, high_freq], 'bandpass', fs=fs, output='sos')
        filtered = signal.sosfilt(sos, detrended)
        
        return filtered
        
    def detect_tid(
        self,
        doppler_multi_freq: Dict[float, np.ndarray],  # freq -> Doppler
        timestamps: np.ndarray
    ) -> Optional[Dict]:
        """
        Detect TID from multi-frequency Doppler.
        
        TID signature:
        - Coherent oscillations across frequencies
        - Period: 15-60 minutes
        - Phase progression with frequency
        """
        # Preprocess each frequency
        filtered = {}
        for freq, doppler in doppler_multi_freq.items():
            filtered[freq] = self.preprocess_doppler(doppler, timestamps)
        
        # Cross-correlate frequencies
        coherence = self._calculate_coherence(filtered)
        
        if coherence < 0.5:
            return None  # Not coherent enough
        
        # Extract period using FFT
        period = self._extract_period(filtered)
        
        # Estimate phase velocity
        phase_velocity = self._estimate_phase_velocity(filtered, timestamps)
        
        return {
            'event_type': 'TID',
            'period_minutes': period / 60,
            'phase_velocity_m_s': phase_velocity,
            'coherence': coherence,
            'affected_frequencies': list(doppler_multi_freq.keys())
        }
        
    def _calculate_coherence(
        self,
        filtered_signals: Dict[float, np.ndarray]
    ) -> float:
        """Calculate coherence across frequencies."""
        # Cross-correlate all pairs
        # Return mean correlation coefficient
        pass
        
    def _extract_period(
        self,
        filtered_signals: Dict[float, np.ndarray]
    ) -> float:
        """Extract dominant period using FFT."""
        # FFT on averaged signal
        # Find peak in 15-60 min range
        pass
        
    def _estimate_phase_velocity(
        self,
        filtered_signals: Dict[float, np.ndarray],
        timestamps: np.ndarray
    ) -> float:
        """Estimate TID phase velocity from frequency progression."""
        # Calculate phase delays between frequencies
        # Convert to spatial velocity
        pass
```

**Integration:**
```python
def _detect_events(self):
    """
    Detect ionospheric events from TEC and Doppler anomalies.
    
    Replaces placeholder with real implementation.
    """
    # Read Doppler time series
    # Run TID detector
    # Write events to L3B HDF5
    
    tid_detector = TIDDetector(self.paths)
    
    # Get recent Doppler data
    doppler_data = self._read_doppler_time_series()
    
    # Detect TID
    tid_event = tid_detector.detect_tid(
        doppler_data['doppler_multi_freq'],
        doppler_data['timestamps']
    )
    
    if tid_event:
        self._write_event(tid_event)
        logger.info(f"TID detected: period={tid_event['period_minutes']:.1f} min")
```

---

## Phase 6: Advanced Features (Week 8+)

### 6.1 Scintillation Indices
**S4 (Amplitude Scintillation):**
```python
def calculate_s4(amplitude_time_series: np.ndarray) -> float:
    """
    Calculate S4 scintillation index.
    
    S4 = sqrt(<I²> - <I>²) / <I>
    where I is intensity (amplitude²)
    """
    intensity = amplitude_time_series ** 2
    s4 = np.sqrt(np.mean(intensity**2) - np.mean(intensity)**2) / np.mean(intensity)
    return s4
```

**σ_φ (Phase Scintillation):**
```python
def calculate_sigma_phi(phase_time_series: np.ndarray) -> float:
    """
    Calculate phase scintillation index.
    
    σ_φ = std(detrended phase)
    """
    detrended_phase = signal.detrend(phase_time_series)
    sigma_phi = np.std(detrended_phase)
    return sigma_phi
```

### 6.2 Ionospheric Tilt Analysis
```python
class IonosphericTiltAnalyzer:
    """Analyze ionospheric structure from multi-station TEC."""
    
    def calculate_tec_gradient(
        self,
        tec_measurements: List[Dict]  # station, lat, lon, tec
    ) -> Dict:
        """
        Calculate TEC gradient from multiple stations.
        
        Requires ≥3 stations with good geometry.
        """
        # Fit linear tilt model
        # Calculate gradient magnitude and direction
        pass
```

### 6.3 Critical Frequency Estimation
```python
def estimate_fof2(
    propagation_modes: Dict[float, str]  # freq -> mode
) -> float:
    """
    Estimate F2-layer critical frequency.
    
    foF2 ≈ highest frequency with F-layer propagation
    """
    f_layer_freqs = [
        freq for freq, mode in propagation_modes.items()
        if mode in ['1F', '2F', '3F']
    ]
    
    if f_layer_freqs:
        return max(f_layer_freqs)
    return None
```

---

## Phase 7: Validation Framework (Ongoing)

### 7.1 Automated Validation Checks
**Tier 1: Basic Validation**
```python
class ValidationFramework:
    """Automated validation of science products."""
    
    def validate_snr(self, snr_db: float) -> bool:
        """Verify SNR is in reasonable range."""
        return -20 <= snr_db <= 60
        
    def validate_doppler(self, doppler_hz: float) -> bool:
        """Verify Doppler is within ±10 Hz."""
        return -10 <= doppler_hz <= 10
        
    def validate_toa(self, toa_ms: float) -> bool:
        """Verify ToA is physically reasonable."""
        return 0 <= toa_ms <= 100
        
    def validate_tec(self, tec_tecu: float) -> bool:
        """Verify TEC is in reasonable range."""
        return 0 <= tec_tecu <= 200  # Typical range
```

**Tier 2: Cross-Validation**
```python
def cross_validate_tec(self, hf_tec: float, gps_tec: float) -> Dict:
    """Compare HF TEC with GPS TEC."""
    bias = hf_tec - gps_tec
    relative_error = abs(bias) / gps_tec
    
    return {
        'bias_tecu': bias,
        'relative_error': relative_error,
        'validated': relative_error < 0.3  # 30% threshold
    }
```

### 7.2 Quality Metrics Dashboard
Track validation metrics over time:
- TEC validation success rate
- Mean bias vs. GPS TEC
- Event detection false positive rate
- Data completeness per channel

---

## Implementation Strategy

### Incremental Rollout
1. **Week 1:** Fix bugs, create schemas, remove CSV
2. **Week 2:** Quick wins (propagation stats, TEC validation)
3. **Week 3-4:** D-layer absorption
4. **Week 4-5:** Sporadic-E detection
5. **Week 5-7:** TID detection
6. **Week 8+:** Advanced features

### Testing Approach
Each feature includes:
1. **Unit tests** - Test individual components
2. **Integration tests** - Test with real data
3. **Validation tests** - Compare with known events
4. **Performance tests** - Ensure <5s processing time

### Code Organization
```
src/hf_timestd/core/
├── science_aggregator.py      # Main orchestrator
├── tec_estimator.py           # Existing TEC estimation
├── tec_validator.py           # NEW: TEC validation
├── absorption_analyzer.py     # NEW: D-layer absorption
├── sporadic_e_detector.py     # NEW: Es detection
├── tid_detector.py            # NEW: TID detection
├── propagation_stats.py       # NEW: Mode statistics
├── scintillation.py           # NEW: S4, σ_φ indices
└── validation_framework.py    # NEW: Automated validation

src/hf_timestd/schemas/
├── l3_tec_v1.json            # Existing
├── l3b_iono_events_v1.json   # NEW
├── l3b_absorption_v1.json    # NEW
└── l3c_propagation_stats_v1.json  # NEW
```

---

## Success Metrics

### Phase 1 (Foundation)
- ✅ TEC updates every 5 minutes
- ✅ No CSV files written (HDF5 only)
- ✅ All schemas validated

### Phase 2 (Quick Wins)
- ✅ Propagation mode statistics generated hourly
- ✅ TEC validated against GPS (>80% correlation)
- ✅ MUF estimates available

### Phase 3 (D-Layer)
- ✅ Absorption calculated for all frequencies
- ✅ SID detection operational
- ✅ Solar zenith angle correlation validated

### Phase 4 (Sporadic-E)
- ✅ Es events detected and logged
- ✅ foEs estimates available
- ✅ Seasonal patterns observable

### Phase 5 (TIDs)
- ✅ TID detection operational
- ✅ Period/wavelength extraction working
- ✅ Geomagnetic correlation observable

### Phase 6+ (Advanced)
- ✅ Scintillation indices calculated
- ✅ Ionospheric tilt analysis available
- ✅ foF2 estimates generated

---

## Resource Requirements

### Development Time
- **Phase 1:** 1-2 weeks (foundation)
- **Phase 2:** 1 week (quick wins)
- **Phase 3:** 2 weeks (absorption)
- **Phase 4:** 1-2 weeks (Es detection)
- **Phase 5:** 2-3 weeks (TID detection)
- **Phase 6+:** Ongoing (advanced features)

**Total:** ~8-10 weeks for core capabilities

### Dependencies
- **Python packages:** scipy, numpy, astropy (solar calculations)
- **External data:** IONEX files (from vtec service)
- **Hardware:** No changes needed

### Testing Data
- Historical data with known events:
  - Solar flare dates (for SID validation)
  - Sporadic-E events (from ionosonde data)
  - Geomagnetic storms (for TID validation)

---

## Documentation Updates

### Update SCIENTIFIC_CAPABILITIES.md
Add implementation status section:
```markdown
## Implementation Status (Updated January 2026)

| Capability | Status | Service | Validation |
|-----------|--------|---------|-----------|
| TEC Monitoring | ✅ Implemented | science-aggregator | ⚠️ In progress |
| D-Layer Absorption | 🚧 Phase 3 | science-aggregator | Planned |
| Propagation Stats | 🚧 Phase 2 | science-aggregator | Planned |
| Sporadic-E | 🚧 Phase 4 | science-aggregator | Planned |
| TID Detection | 🚧 Phase 5 | science-aggregator | Planned |
| Ionospheric Tilt | 📋 Phase 6+ | science-aggregator | Future |
```

### Create User Guide
Document how to:
- Access science products (HDF5 files)
- Interpret quality flags
- Understand validation status
- Report issues or request features

---

## Risk Mitigation

### Technical Risks
1. **TID detection complexity** → Start with simple period extraction, iterate
2. **Validation data availability** → Use multiple sources (IONEX, ionosonde, VOACAP)
3. **Performance degradation** → Profile each feature, optimize as needed

### Scientific Risks
1. **False positives** → Conservative thresholds, manual review initially
2. **Validation failures** → Document limitations clearly
3. **Model assumptions** → Test with diverse conditions

### Operational Risks
1. **Service instability** → Extensive testing before deployment
2. **Disk space** → Monitor HDF5 file sizes, implement rotation
3. **CPU load** → Ensure <10% CPU usage on production system

---

## Conclusion

This roadmap transforms the science-aggregator from a single-purpose TEC estimator into a comprehensive ionospheric science platform that achieves the aspirations documented in SCIENTIFIC_CAPABILITIES.md.

**Key Principles:**
- HDF5-first design (complete CSV transition)
- Incremental implementation (deliver value early)
- Validation-focused (ensure scientific rigor)
- Modular architecture (easy to extend)

**Timeline:** 8-10 weeks for core capabilities, ongoing for advanced features

**Next Steps:**
1. Review and approve this roadmap
2. Create HDF5 schemas (Phase 1.2)
3. Remove CSV dual-write (Phase 1.3)
4. Begin Phase 2 implementation (quick wins)
