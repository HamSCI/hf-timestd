# Solar-Ionosphere Correlation System

## Overview

This system provides real-time correlation analysis between space weather conditions and HF propagation measurements. It integrates NOAA Space Weather Prediction Center (SWPC) data with the hf-timestd propagation measurements to reveal the physical relationships between solar activity and ionospheric propagation.

## Features Implemented

### 1. Space Weather Data Ingestion

**Service:** `services/space_weather_service.py`

Fetches and caches data from NOAA SWPC:
- **X-ray Flux** (0.1-0.8 nm): GOES satellite measurements, 5-minute cadence
- **Kp Index**: Planetary geomagnetic index, 3-hour cadence
- **Proton Flux** (≥10 MeV): Solar energetic particle measurements
- **SID Event Detection**: Automatic detection of Sudden Ionospheric Disturbances

**Data Sources:**
- `https://services.swpc.noaa.gov/json/goes/xray-fluxes-7-day.json`
- `https://services.swpc.noaa.gov/json/planetary_k_index_1m.json`
- `https://services.swpc.noaa.gov/json/goes/primary/integral-protons-plot-6-hour.json`

**Caching:** 15-minute cache in `/var/lib/timestd/space_weather_cache/`

### 2. Correlation Analysis Service

**Service:** `services/correlation_service.py`

Provides scientific correlation analysis:

#### SNR vs Solar Zenith Angle
- Calculates solar position at propagation path midpoint
- Performs Pearson correlation analysis
- Linear regression: SNR = a × SZA + b
- Expected: Strong positive correlation (r > 0.5) for F-layer propagation

#### SID Event Detection
- Correlates X-ray flares with SNR drops
- Identifies affected frequencies (lower frequencies more affected)
- Time window: ±30 minutes around flare peak
- Threshold: SNR drop >5 dB

#### TEC vs F10.7 Correlation
- Analyzes relationship between Total Electron Content and solar flux
- Expected: r > 0.6 for daytime measurements
- Note: F10.7 ingestion not yet implemented (placeholder)

#### Propagation Mode vs Kp Index
- Bins propagation data by geomagnetic activity level
- Quiet (Kp 0-3), Unsettled (Kp 3-5), Storm (Kp 5+)
- Identifies high-latitude path degradation (CHU) during storms

### 3. API Endpoints

#### Space Weather Endpoints (`/api/space-weather/`)

- `GET /current` - Current conditions with active alerts
- `GET /xray?hours=24` - X-ray flux time series
- `GET /kp?hours=24` - Kp index time series
- `GET /protons?hours=24` - Proton flux time series
- `GET /events/sid?hours=24` - Detected SID events
- `GET /summary?hours=24` - Comprehensive summary

#### Correlation Endpoints (`/api/correlations/`)

- `GET /snr-solar?station=WWV&frequency=10&hours=24` - SNR-solar correlation
- `GET /sid-detection?hours=24` - SID event correlation with SNR drops
- `GET /tec-f107?station=WWV&days=30` - TEC-F10.7 correlation
- `GET /propagation-kp?hours=72` - Propagation vs Kp analysis
- `GET /summary?hours=24` - Multi-faceted correlation summary

### 4. Frontend Visualization

**Page:** `/static/solar-correlation.html`

Multi-tab interface with:

#### Overview Tab
- Real-time space weather dashboard (X-ray class, Kp, proton flux)
- Active alert banner for M/X-class flares and geomagnetic storms
- Multi-panel time series: X-ray + Kp + SNR synchronized plots

#### SNR-Solar Correlation Tab
- Scatter plot: SNR vs Solar Zenith Angle
- Linear regression fit overlay
- Correlation statistics (r, R², p-value)
- Physical interpretation of results

#### SID Events Tab
- List of detected SID events
- X-ray class and affected channels
- Time-aligned visualization

#### Geomagnetic Effects Tab
- Propagation statistics binned by Kp level
- Comparison of quiet vs storm conditions
- High-latitude path analysis

**Features:**
- Dark mode optimized for operations
- Auto-refresh capability (1-minute interval)
- Responsive Plotly.js charts
- Time range selector (6h to 72h)
- Station and frequency selection

## Physical Relationships

### Expected Correlations

1. **SNR vs Solar Zenith Angle**
   - **Physics:** F-layer ionization proportional to solar EUV flux
   - **Expected:** r > 0.7 for daytime F-layer propagation
   - **Diurnal variation:** ~20 dB between day and night

2. **X-ray Flares → SNR Drops (SID)**
   - **Physics:** X-rays ionize D-layer → increased absorption
   - **Expected:** M-class flare → 10-20 dB drop on 2.5-10 MHz
   - **Timescale:** Minutes (onset) to ~1 hour (recovery)
   - **Frequency dependence:** Lower frequencies more affected (∝ 1/f²)

3. **TEC vs F10.7**
   - **Physics:** Solar flux proxy for EUV → F-layer ionization
   - **Expected:** r > 0.6 for daily averages
   - **Relationship:** ~0.15 TECU per sfu

4. **Kp Index → High-Latitude Degradation**
   - **Physics:** Geomagnetic storms → auroral absorption
   - **Expected:** CHU path degraded when Kp > 5
   - **Effect:** 5-15 dB SNR reduction, increased scintillation

## Usage Examples

### Check Current Space Weather
```bash
curl http://localhost:8000/api/space-weather/current
```

### Analyze SNR-Solar Correlation for WWV 10 MHz
```bash
curl "http://localhost:8000/api/correlations/snr-solar?station=WWV&frequency=10&hours=24"
```

### Detect Recent SID Events
```bash
curl "http://localhost:8000/api/correlations/sid-detection?hours=24"
```

### Get Comprehensive Summary
```bash
curl "http://localhost:8000/api/space-weather/summary?hours=24"
```

## Alert Levels

### X-ray Flux
- **A-class:** < 10⁻⁷ W/m² (Quiet)
- **B-class:** 10⁻⁷ to 10⁻⁶ W/m² (Minor)
- **C-class:** 10⁻⁶ to 10⁻⁵ W/m² (Moderate)
- **M-class:** 10⁻⁵ to 10⁻⁴ W/m² (Strong) → **ALERT**
- **X-class:** > 10⁻⁴ W/m² (Extreme) → **HIGH ALERT**

### Kp Index
- **0-2:** Quiet
- **3-4:** Unsettled
- **5-6:** Storm → **ALERT**
- **7-9:** Severe Storm → **HIGH ALERT**

### Proton Flux (≥10 MeV)
- **< 10 pfu:** Background
- **10-100 pfu:** Elevated → **ALERT** (Polar Cap Absorption risk)
- **> 100 pfu:** High → **HIGH ALERT** (Severe PCA)

## Implementation Notes

### Cache Strategy
- Space weather data cached for 15 minutes
- Stale cache used if API fetch fails (graceful degradation)
- Cache directory: `/var/lib/timestd/space_weather_cache/`

### Error Handling
- API timeouts: 10 seconds
- Fallback to cached data on network errors
- Frontend displays error messages for missing data

### Performance
- Correlation analysis: O(n) for n data points
- Typical response time: < 500ms for 24-hour analysis
- Multi-panel plots: Client-side rendering with Plotly.js

## Future Enhancements

### Phase 2 (Not Yet Implemented)
1. **F10.7 Solar Flux Ingestion**
   - Source: Space Weather Canada
   - Daily values for long-term TEC correlation

2. **Dst Index**
   - Storm-time disturbance index
   - Ring current monitoring

3. **Solar Wind Parameters**
   - Speed, density, IMF Bz
   - Predictive indicators for geomagnetic storms

4. **Automated Event Notifications**
   - Email/webhook alerts for M/X-class flares
   - Kp > 5 storm warnings
   - Predicted propagation impacts

5. **Machine Learning Predictions**
   - SNR prediction from space weather forecast
   - MUF estimation
   - Optimal frequency recommendations

6. **Historical Analysis**
   - Long-term correlation trends
   - Solar cycle effects
   - Seasonal variations

## Scientific References

1. **D-layer Absorption:** Davies, K. (1990). "Ionospheric Radio"
2. **TEC Estimation:** Coster et al. (2013). "Accuracy of GPS TEC"
3. **Geomagnetic Effects:** Hunsucker & Hargreaves (2003). "The High-Latitude Ionosphere"
4. **Space Weather Indices:** NOAA SWPC documentation

## Troubleshooting

### No Space Weather Data
- Check internet connectivity to `services.swpc.noaa.gov`
- Verify cache directory permissions: `/var/lib/timestd/space_weather_cache/`
- Check logs: `journalctl -u timestd-web-api | grep space_weather`

### Correlation Analysis Returns "No Data"
- Ensure propagation measurements exist for selected station/frequency
- Check time range (may need longer period for sufficient data points)
- Verify HDF5 files exist in `/var/lib/timestd/phase2/`

### Frontend Not Loading
- Check FastAPI service: `systemctl status timestd-web-api`
- Verify port 8000 is accessible
- Check browser console for JavaScript errors

## Contact

For questions or issues with the solar correlation system, check the main hf-timestd documentation or system logs.
