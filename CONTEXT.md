# HF-TimeStd Development Context

**Last Updated**: 2025-12-27  
**Current Phase**: Analytics Validation & Web UI Enhancement  
**Next Session Focus**: Validate analytics implementation against signal feature inventory

---

## Recent Session Summary (2025-12-27)

### Web UI Redesign - Completed

Implemented new web UI architecture separating timing metrology from propagation science:

**What Was Built**:

1. **New Metrology Dashboard** (`web-ui/metrology.html`)
   - Hero status display (D_clock, uncertainty, quality grade)
   - Kalman funnel visualization (24h convergence)
   - Per-station contribution cards (WWV, WWVH, CHU, BPM)
   - Quality timeline (A/B/C/D distribution)
   - Chrony integration status
   - Auto-refresh every 30s

2. **4 New API v2 Endpoints** (`web-ui/monitoring_server.py`)
   - `/api/v2/timing/kalman-funnel` - Convergence data
   - `/api/v2/timing/quality-timeline` - Grade distribution
   - `/api/v2/timing/chrony-status` - SHM integration status
   - `/api/v2/system/health-summary` - Aggregated health

3. **Enhanced Ionosphere Page** (`web-ui/ionosphere.html`)
   - Quality gate warning (displays when timing grade is C or D)
   - Links to metrology dashboard when timing is degraded

4. **Updated Navigation** (`web-ui/components/navigation.js`)
   - New page order: Summary → Metrology → Timing (Legacy) → Propagation → Logs

**Key Fixes**:

- Fixed route order bug (catch-all route was intercepting API calls)
- Fixed fusion data path (`/var/lib/timestd/phase2/fusion/fused_d_clock.csv`)
- Server running on <http://localhost:8080> with real data

**Status**: ✅ Complete and functional with real production data

### Scientific Capabilities Documentation - Completed

Created comprehensive feature inventory and scientific questions documentation:

**New Documentation**:

- `docs/SCIENTIFIC_CAPABILITIES.md` - Complete rewrite with honest assessment
  - Validated measurements (✅): SNR, Doppler, tones, ToA, modes
  - Partially validated (⚠️): TEC, phase variance, delay spread
  - Theoretical capabilities: Scintillation indices, TIDs, ionospheric tilt
  - 6 scientific questions with data quality assessments
  - Measurement uncertainties and limitations
  - Recommendations for scientists

**Philosophy Established**:
"Provide only data we can justify given instrument capabilities. Scientists determine if quality meets their needs."

---

## Next Session Objective

### Analytics Validation Against Signal Features

**Goal**: Ensure analytics service (`phase2_analytics_service.py`) correctly extracts all detectable signal features documented in `SCIENTIFIC_CAPABILITIES.md`.

**Approach**:

1. **Audit Current Implementation**
   - Review what `phase2_analytics_service.py` actually computes
   - Compare against feature inventory
   - Identify gaps between "should measure" and "does measure"

2. **Validate Measurement Methods**
   - For each feature: How is it computed? (algorithm)
   - What's the expected range? (physics)
   - What are the limitations? (systematic errors)

3. **Build Validation Dashboard**
   - Minimal 4-panel dashboard to sanity-check measurements
   - SNR heatmap (time × frequency)
   - Doppler timeline (verify ±5 Hz range)
   - Tone detection matrix (check geographic sense)
   - ToA scatter (measured vs expected)

4. **Document Findings**
   - What's working correctly
   - What needs calibration/tuning
   - What's missing but should be added
   - What's aspirational (requires more work)

**Key Documents to Reference**:

- `docs/SCIENTIFIC_CAPABILITIES.md` - Feature inventory and scientific questions
- `src/hf_timestd/schemas/l1_channel_observables_v1.json` - L1A data schema
- `src/hf_timestd/schemas/l2_timing_measurements_v1.json` - L2 data schema
- `src/hf_timestd/core/phase2_analytics_service.py` - Analytics implementation

**Validation Priorities**:

1. **Tier 1** (Basic features - must validate first):
   - Carrier SNR vs radiod's reported SNR
   - Doppler shift (check ±5 Hz range, look for outliers)
   - Tone detections (WWV/WWVH mutual exclusivity)
   - ToA (physically reasonable < 100 ms)

2. **Tier 2** (Ionospheric features - validate if Tier 1 passes):
   - TEC estimation (compare to GPS TEC maps)
   - Propagation mode classification (validate against ray-tracing)
   - D-layer absorption (correlate with solar zenith angle)
   - Phase variance (understand what it measures)

3. **Tier 3** (Advanced features - implement if needed):
   - Scintillation indices (S4, σ_φ)
   - TID detection
   - Sporadic-E characterization

---

## Current System Status

### Production Environment

- **Location**: `/opt/hf-timestd/`
- **Services Running**:
  - `timestd-core-recorder` - ✅ Active (9 channels)
  - `timestd-analytics` - ✅ Active (writing HDF5 files)
  - `timestd-fusion` - ✅ Active (Grade A timing, 2 stations)
  - `timestd-web-ui-fastapi` - Status unknown (dev server on :8080)

### Data Products

- **Fusion CSV**: `/var/lib/timestd/phase2/fusion/fused_d_clock.csv` (1.1MB)
- **HDF5 Files**: Present for CHU_3330, CHU_7850, CHU_14670, SHARED channels
- **Current Timing**: Grade A, D_clock = 12.32ms ± 0.62ms, 2 stations (WWV, CHU)
- **24h Distribution**: 803 Grade A, 404 Grade B, 191 Grade C, 42 Grade D

### HDF5 SWMR Status

- ✅ Readers use `swmr=True` mode (`hdf5_reader.py`)
- ✅ Writers enable SWMR after dataset creation (`hdf5_writer.py`)
- Concurrent read/write supported

---

## Key Technical Decisions

### Web UI Architecture

**Decision**: Separate timing metrology from propagation science
**Rationale**: Different user personas (Operators, Metrologists, Scientists)
**Implementation**:

- Metrology dashboard focuses on measurement quality
- Propagation dashboard focuses on scientific insights
- Quality gate prevents misinterpretation of low-quality data

### API Versioning

**Decision**: New `/api/v2/` endpoints, maintain `/api/v1/` backward compatibility
**Rationale**: Allow UI evolution without breaking existing consumers

### Data Validation Philosophy

**Decision**: Build validation dashboard before scientific visualizations
**Rationale**: Must verify measurements are accurate before using for science
**Quote**: "I do not want to claim to deliver what measurably we cannot detect. But if we can confidently detect these features, then we should do so."

---

## Known Issues & Limitations

### Web UI

- ⚠️ Ionosphere page shows "No data" - existing `/api/v1/broadcasts/history` endpoint issue (predates redesign)
- ⚠️ Mobile optimization needs work (basic responsive design implemented)
- ⚠️ No real-time WebSocket updates (using 30s polling)

### Analytics

- ⚠️ TEC estimation implemented but not validated against GPS TEC
- ⚠️ Propagation mode classification uses delay heuristics (may miss mode mixing)
- ⚠️ BPM support experimental (needs full characterization)
- ⚠️ Phase variance, Doppler spread, delay spread - physical interpretation unclear

### Data Quality

- Timing precision limited by ionospheric variability (±1-3 ms)
- Cannot separate ionospheric layers without ionosonde
- Single receiver (no spatial resolution for TIDs)

---

## File Locations Reference

### Web UI (Development)

- `/home/mjh/git/hf-timestd/web-ui/`
  - `monitoring_server.py` - FastAPI backend with new API v2 endpoints
  - `metrology.html` - New timing metrology dashboard
  - `ionosphere.html` - Enhanced with quality gate
  - `components/navigation.js` - Updated navigation

### Analytics

- `/home/mjh/git/hf-timestd/src/hf_timestd/core/`
  - `phase2_analytics_service.py` - Main analytics service (2181 lines)
  - `phase2_temporal_engine.py` - Processing engine
  - `correlator_bank.py` - FFT-based signal processing
  - `tec_estimator.py` - TEC calculation (needs validation)

### Schemas

- `/home/mjh/git/hf-timestd/src/hf_timestd/schemas/`
  - `l1_channel_observables_v1.json` - Raw signal features
  - `l2_timing_measurements_v1.json` - Timing with uncertainty
  - `l3_fusion_timing_v1.json` - Multi-station fusion

### Documentation

- `docs/SCIENTIFIC_CAPABILITIES.md` - Feature inventory & scientific questions
- `TECHNICAL_REFERENCE.md` - System architecture
- `ARCHITECTURE.md` - Data flow and components

---

## Development Workflow

### Testing Web UI Changes

```bash
# Start dev server
cd /home/mjh/git/hf-timestd/web-ui
source ../venv/bin/activate
python monitoring_server.py

# Access at http://localhost:8080/metrology.html
```

### Deploying to Production

```bash
# Copy updated files
sudo cp /home/mjh/git/hf-timestd/web-ui/monitoring_server.py /opt/hf-timestd/web-ui/
sudo cp /home/mjh/git/hf-timestd/web-ui/metrology.html /opt/hf-timestd/web-ui/
sudo cp /home/mjh/git/hf-timestd/web-ui/ionosphere.html /opt/hf-timestd/web-ui/
sudo cp /home/mjh/git/hf-timestd/web-ui/components/navigation.js /opt/hf-timestd/web-ui/components/

# Restart service (if exists)
sudo systemctl restart timestd-web-ui-fastapi
```

### Examining HDF5 Data

```bash
# List available files
ls -lh /var/lib/timestd/phase2/CHU_3330/carrier_power/*.h5

# Read with Python (SWMR mode)
python3 -c "
import h5py
f = h5py.File('/path/to/file.h5', 'r', swmr=True, libver='latest')
print(list(f.keys()))
print(f['measurements'].dtype)
f.close()
"
```

---

## Questions for Next Session

1. **Analytics Audit**: Which features in `SCIENTIFIC_CAPABILITIES.md` are actually being computed by `phase2_analytics_service.py`?

2. **Measurement Validation**: For each computed feature, how do we verify it's accurate?

3. **Missing Features**: What should be added to analytics to support the scientific questions?

4. **Data Quality**: What validation tests should run automatically to flag bad data?

5. **Visualization Priority**: Which features should we visualize first in the validation dashboard?

---

## Success Criteria for Next Session

- [ ] Complete audit of analytics vs feature inventory
- [ ] Document measurement methods for all computed features
- [ ] Identify 3-5 features that need validation
- [ ] Build minimal validation dashboard (4 panels)
- [ ] Document findings: what works, what needs fixing, what's missing
- [ ] Create prioritized list of analytics enhancements

---

## Agent Preparation Notes

**Context Loading Priority**:

1. Read `docs/SCIENTIFIC_CAPABILITIES.md` - understand what should be measured
2. Review `src/hf_timestd/core/phase2_analytics_service.py` - understand what is measured
3. Check schemas in `src/hf_timestd/schemas/` - understand data structure
4. Reference this CONTEXT.md for session history

**Key Mindset**:

- Focus on honest assessment of capabilities vs limitations
- Validate before visualizing
- Document uncertainties and systematic errors
- Prioritize features with clear scientific value

**Tools Available**:

- HDF5 files with SWMR enabled (can read while analytics writes)
- FastAPI server for quick API endpoint testing
- Plotly.js for interactive visualizations
- Production data flowing in real-time
