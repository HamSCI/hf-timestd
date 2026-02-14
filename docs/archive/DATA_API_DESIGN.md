# hf-timestd Data API Design

## Purpose

Provide a **complete, accurate, and scientifically rigorous** API for accessing all ionospheric and timing data, preventing common pitfalls:

1. **Incomplete Data**: Missing metadata (confidence, uncertainty, quality flags)
2. **Ambiguous Timestamps**: Unclear timezone or epoch handling
3. **Undocumented Units**: Missing physical units and coordinate systems
4. **No Quality Indicators**: Unable to filter bad data
5. **Inflexible Queries**: Can't filter by time range, station, or quality
6. **No Provenance**: Unknown data source, version, or processing pipeline

---

## API Design Principles

### 1. Self-Describing Responses

Every response includes:

- **Metadata**: Units, coordinate system, data source
- **Quality Indicators**: Confidence scores, uncertainty estimates, flags
- **Provenance**: Processing version, timestamp, data lineage

### 2. Consistent Structure

All endpoints follow same JSON schema:

```json
{
  "metadata": { /* dataset description */ },
  "data": [ /* array of measurements */ ],
  "quality": { /* summary statistics */ },
  "provenance": { /* data lineage */ }
}
```

### 3. Explicit Units and Coordinates

- All timestamps: ISO 8601 UTC
- All coordinates: WGS84 decimal degrees
- All physical quantities: SI units with explicit labels

### 4. Quality-First Design

- Every measurement includes confidence score (0-1)
- Every measurement includes uncertainty estimate
- Quality flags: `GOOD`, `MARGINAL`, `BAD`, `MISSING`

---

## API Endpoints

### 1. TEC Time Series

**Endpoint**: `GET /api/v1/science/tec`

**Query Parameters**:

- `station`: Station code (WWV, WWVH, CHU, BPM)
- `start`: ISO 8601 timestamp (UTC)
- `end`: ISO 8601 timestamp (UTC)
- `min_confidence`: Minimum confidence score (0-1, default: 0.0)
- `min_frequencies`: Minimum number of frequencies (default: 2)

**Response**:

```json
{
  "metadata": {
    "station": "WWV",
    "parameter": "Total Electron Content",
    "units": "TECU (10^16 electrons/m²)",
    "coordinate_system": "WGS84",
    "time_system": "UTC",
    "cadence_seconds": 60,
    "description": "TEC estimated from multi-frequency HF propagation delay",
    "accuracy_estimate": "±5-10 TECU (vs GPS TEC)",
    "validation_status": "pending"
  },
  "data": [
    {
      "timestamp": "2025-12-23T12:00:00Z",
      "minute_boundary": 1703332800,
      "tec_tecu": 25.3,
      "tec_electrons_m2": 2.53e17,
      "t_vacuum_error_ms": 2.1,
      "confidence": 0.92,
      "uncertainty_tecu": 5.0,
      "residuals_ms": 0.15,
      "n_frequencies": 6,
      "frequencies_mhz": [2.5, 5.0, 10.0, 15.0, 20.0, 25.0],
      "quality_flag": "GOOD",
      "ionospheric_pierce_point": {
        "latitude": 40.5,
        "longitude": -100.2,
        "altitude_km": 350
      }
    }
  ],
  "quality": {
    "total_measurements": 1440,
    "good_measurements": 1200,
    "marginal_measurements": 180,
    "bad_measurements": 60,
    "mean_confidence": 0.87,
    "mean_n_frequencies": 5.2,
    "data_completeness": 0.83
  },
  "provenance": {
    "data_source": "hf-timestd Phase 2 Analytics",
    "processing_version": "1.0.0",
    "tec_estimator_version": "1.0.0",
    "generated_at": "2025-12-23T18:00:00Z",
    "station_location": {
      "latitude": 38.918461,
      "longitude": -92.127974,
      "callsign": "AC0G"
    },
    "broadcast_location": {
      "station": "WWV",
      "latitude": 40.678,
      "longitude": -105.039
    }
  }
}
```

---

### 2. Multi-Frequency Group Delay

**Endpoint**: `GET /api/v1/science/group-delay`

**Query Parameters**:

- `station`: Station code
- `timestamp`: ISO 8601 timestamp (single minute)
- `include_fit`: Include linear regression fit (default: true)

**Response**:

```json
{
  "metadata": {
    "station": "WWV",
    "parameter": "Ionospheric Group Delay",
    "units": "milliseconds",
    "description": "Frequency-dependent propagation delay for TEC estimation"
  },
  "data": {
    "timestamp": "2025-12-23T12:00:00Z",
    "measurements": [
      {
        "frequency_mhz": 2.5,
        "frequency_hz": 2500000,
        "toa_ms": 45.2,
        "uncertainty_ms": 0.5,
        "snr_db": 15.3,
        "quality_flag": "GOOD"
      },
      {
        "frequency_mhz": 5.0,
        "frequency_hz": 5000000,
        "toa_ms": 42.1,
        "uncertainty_ms": 0.3,
        "snr_db": 22.1,
        "quality_flag": "GOOD"
      }
    ],
    "fit": {
      "tec_tecu": 25.3,
      "t_vacuum_ms": 38.5,
      "slope": 1024.3,
      "intercept": 38.5,
      "r_squared": 0.98,
      "residuals_rms_ms": 0.15
    }
  },
  "provenance": {
    "data_source": "Phase 2 clock_offset CSVs",
    "aggregation_method": "Science Aggregator v1.0.0"
  }
}
```

---

### 3. Doppler Analysis

**Endpoint**: `GET /api/v1/science/doppler`

**Query Parameters**:

- `channel`: Channel name (e.g., WWV_10000)
- `start`, `end`: Time range
- `min_coherence_time`: Minimum coherence time (seconds)

**Response**:

```json
{
  "metadata": {
    "channel": "WWV_10000",
    "frequency_mhz": 10.0,
    "parameter": "Doppler Shift and Spread",
    "units": {
      "doppler_hz": "Hertz",
      "coherence_time_sec": "seconds",
      "phase_variance_rad": "radians"
    },
    "description": "Ionospheric layer motion and turbulence indicators"
  },
  "data": [
    {
      "timestamp": "2025-12-23T12:00:00Z",
      "doppler_mean_hz": -0.25,
      "doppler_std_hz": 0.05,
      "doppler_min_hz": -0.35,
      "doppler_max_hz": -0.15,
      "coherence_time_sec": 45.2,
      "phase_variance_rad": 0.12,
      "quality_flag": "GOOD",
      "interpretation": {
        "layer_motion": "ascending",
        "turbulence_level": "low",
        "coherence_quality": "high"
      }
    }
  ],
  "quality": {
    "mean_coherence_time_sec": 42.3,
    "data_completeness": 0.95
  },
  "provenance": {
    "data_source": "Phase 2 doppler CSVs",
    "doppler_estimator_version": "1.0.0"
  }
}
```

---

### 4. Ionospheric Events

**Endpoint**: `GET /api/v1/science/events`

**Query Parameters**:

- `start`, `end`: Time range
- `event_type`: TID, SolarFlare, SpreadF, LayerTransition
- `min_severity`: low, moderate, high

**Response**:

```json
{
  "metadata": {
    "parameter": "Ionospheric Events",
    "detection_methods": ["Doppler anomaly", "FSS spike", "TEC gradient"],
    "validation_status": "automated detection, manual review pending"
  },
  "data": [
    {
      "event_id": "TID_20251223_120000",
      "timestamp": "2025-12-23T12:00:00Z",
      "event_type": "TID",
      "severity": "moderate",
      "duration_seconds": 1800,
      "affected_stations": ["WWV", "WWVH"],
      "metrics": {
        "doppler_excursion_hz": 2.5,
        "tec_change_tecu": 15.0,
        "propagation_velocity_m_s": 450
      },
      "confidence": 0.75,
      "quality_flag": "MARGINAL",
      "description": "Traveling Ionospheric Disturbance detected via Doppler periodicity"
    }
  ],
  "provenance": {
    "detector_version": "1.0.0",
    "detection_algorithm": "Doppler periodicity + TEC gradient",
    "false_positive_rate": "unknown (validation pending)"
  }
}
```

---

### 5. Clock Offset (Timing)

**Endpoint**: `GET /api/v1/timing/clock-offset`

**Query Parameters**:

- `channel`: Channel name
- `start`, `end`: Time range
- `min_quality`: Quality grade filter (A, B, C, D)

**Response**:

```json
{
  "metadata": {
    "channel": "WWV_10000",
    "parameter": "System Clock Offset",
    "units": "milliseconds",
    "reference": "UTC(NIST) via WWV broadcast",
    "description": "D_clock: observed - expected arrival time"
  },
  "data": [
    {
      "timestamp": "2025-12-23T12:00:00Z",
      "minute_boundary": 1703332800,
      "clock_offset_ms": -2.14,
      "station": "WWV",
      "frequency_mhz": 10.0,
      "propagation_delay_ms": 5.38,
      "propagation_mode": "1E",
      "n_hops": 1,
      "confidence": 0.028,
      "uncertainty_ms": 1.67,
      "quality_grade": "C",
      "snr_db": 4.93,
      "quality_flag": "MARGINAL"
    }
  ],
  "quality": {
    "mean_uncertainty_ms": 1.2,
    "grade_distribution": {
      "A": 120,
      "B": 450,
      "C": 680,
      "D": 190
    }
  },
  "provenance": {
    "data_source": "Phase 2 clock_offset CSVs",
    "gpsdo_locked": true,
    "receiver_location": {
      "latitude": 38.918461,
      "longitude": -92.127974
    }
  }
}
```

---

### 6. System Health

**Endpoint**: `GET /api/v1/system/health`

**Response**:

```json
{
  "status": "healthy",
  "timestamp": "2025-12-23T18:00:00Z",
  "services": {
    "core_recorder": {
      "status": "running",
      "uptime_seconds": 12600,
      "data_rate_mbps": 2.4
    },
    "analytics": {
      "status": "running",
      "channels_active": 9,
      "channels_total": 9,
      "processing_lag_seconds": 15
    },
    "science_aggregator": {
      "status": "running",
      "last_tec_calculation": "2025-12-23T17:55:00Z",
      "tec_measurements_today": 1200
    }
  },
  "data_quality": {
    "clock_offset_completeness": 0.95,
    "doppler_completeness": 0.92,
    "tec_completeness": 0.83,
    "mean_snr_db": 15.3
  }
}
```

---

## Implementation Priorities

### Phase 1: Core Data Access (Week 1)

1. `/api/v1/science/tec` - TEC time series
2. `/api/v1/science/group-delay` - Multi-frequency delay
3. `/api/v1/timing/clock-offset` - Timing data

### Phase 2: Analysis Tools (Week 2-3)

4. `/api/v1/science/doppler` - Doppler analysis
2. `/api/v1/system/health` - System status

### Phase 3: Advanced Features (Month 2)

6. `/api/v1/science/events` - Event detection
2. `/api/v1/hamsci/export` - HamSCI network integration

---

## Quality Assurance

### Automated Tests

```python
def test_tec_api_completeness():
    """Verify all required fields present."""
    response = requests.get('/api/v1/science/tec?station=WWV')
    assert 'metadata' in response.json()
    assert 'data' in response.json()
    assert 'quality' in response.json()
    assert 'provenance' in response.json()
    
def test_tec_units_explicit():
    """Verify units are documented."""
    response = requests.get('/api/v1/science/tec?station=WWV')
    assert 'units' in response.json()['metadata']
    assert 'TECU' in response.json()['metadata']['units']
```

### Documentation Requirements

- OpenAPI/Swagger spec for all endpoints
- Example requests and responses
- Error handling documentation
- Rate limiting and authentication

---

## Error Handling

All errors return:

```json
{
  "error": {
    "code": "INVALID_STATION",
    "message": "Station 'XYZ' not found. Valid stations: WWV, WWVH, CHU, BPM",
    "timestamp": "2025-12-23T18:00:00Z",
    "request_id": "abc123"
  }
}
```

---

## Summary

**Key Features**:

- Self-describing responses with metadata
- Explicit units and coordinate systems
- Quality indicators on every measurement
- Complete provenance tracking
- Consistent JSON schema across all endpoints

**Benefits**:

- Prevents data misinterpretation
- Enables automated quality filtering
- Supports scientific reproducibility
- Facilitates third-party integration (HamSCI, research)
