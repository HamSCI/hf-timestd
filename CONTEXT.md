# HF Time Standard Analysis - Project Context

**Last Updated:** December 31, 2025  
**Version:** 3.14.0  
**Status:** Production (9 channels running at AC0G)

## Quick Reference

**What:** Precision HF timing system extracting D_clock measurements from WWV/WWVH/CHU/BPM broadcasts  
**Where:** `/opt/hf-timestd` (production) or `/home/mjh/git/hf-timestd` (development)  
**Services:** timestd-core-recorder, timestd-analytics (9 channels), timestd-fusion, timestd-web-ui  
**Web UI:** <http://localhost:3000>

## Current State (Dec 31, 2025)

### ✅ Recently Completed

**Phase 4: Tone Detection Improvements** (v3.14.0 - Dec 31, 2025)

## [3.7.0] - 2025-12-31

### Added - Ionosphere Science Dashboard & Data Robustness

#### Ionosphere Science Dashboard

- **New Frontend**: `ionosphere-science.html` providing advanced visualization of propagation metrics.
- **Features**:
  - **WWV vs WWVH Discrimination**: Visualizes station dominance on shared frequencies.
  - **Propagation Residuals**: Interactive plot of measured timing offsets vs IRI-2020 predictions.
  - **Inferred Layer Heights**: Physics-based proxy estimation of F2 virtual heights from timing residuals.
  - **Dynamic Frequency Selection**: Intelligent filtering of valid frequencies based on station selection (including correct CHU frequencies).

#### Data Robustness

- **HDF5 Reader Safety**: Implemented critical fixes in `utils/hdf5_reader.py` to handle SWMR race conditions and prevent `IndexError` crashes when optional datasets (SNR, Doppler) are missing or shorter than the main timeline.
- **CSV Fallback**: Implemented robust fallback mechanism in `monitoring_server.py` to read legacy CSV files for discrimination data when HDF5 files are delayed or missing.
- **Backend Stability**: Fixed `timezone` import errors preventing server startup.

### Known Issues

- **CHU 300 Baud Frame Slip**: Observed ~33ms timing jumps in CHU data, corresponding accurately to one 300-baud character duration, indicating a decoder synchronization issue.

- **Robust Noise Floor** - MAD-based estimation (+75 lines in `tone_detector.py`)
- **Adaptive Search Windows** - SNR/state-based narrowing (+72 lines in `tone_detector.py`)
- **Ionospheric Prediction** - IRI-2020 integration (+105 lines in `phase2_temporal_engine.py`)
- **Status:** Deployed to production, all 9 channels running
- **Expected Impact:** 20% FP reduction, 2ms timing improvement, 100x search space reduction

### 🔄 In Progress

### ✅ Recently Completed

**Ionosphere Science Dashboard** (v3.7.0 - Dec 31, 2025)

- **Visualizations**: 3-panel dashboard for Discrimination, Residuals, and Layer Heights.
- **Robustness**: HDF5/CSV hybrid reading, SWMR safety fixes.
- **Status**: Operational at `/ionosphere-science.html`.

### 🔄 In Progress

**Analytics Critical Review**

- **Trigger**: Detected ~33ms "frame slip" in CHU data (matching 300 baud character length).
- **Goal**: Verify tone detection and decoding logic to improve offset determination accuracy.

### 📋 Next Priority: Analytics Service Critical Review

**Goal:** Perform a comprehensive critical review of the Phase 2 Analytics Service, specifically focusing on tone detection quality and decoding robustness.

**Trigger:** Observed a physically impossible +19ms to -13ms (~32.6ms) jump in CHU timing. This matches exactly the duration of one 300-baud character (33.33ms), indicating a "frame slip" in the digital decoder.

**Key Areas:**

1. **CHU Decoder**: Synchronization logic and frame locking.
2. **Tone Detection**: SNR thresholds, multipath rejection, and frequency discrimination.
3. **Offset Determination**: How individual tone/tick detections are weighted and fused.

**Current Web UI Capabilities:**

- Real-time channel health monitoring
- D_clock timing visualizations (Kalman funnel, constellation, consensus)
- Discrimination analysis (7-panel view)
- Live audio streaming
- Carrier analysis with spectrograms

**Data Available But Not Well Exposed:**

1. **Ionospheric Metrics:**
   - IRI-2020 predictions (hmF2, foF2, layer heights)
   - Propagation mode probabilities
   - TEC estimates from GNSS
   - Layer height time series

2. **Metrology Data:**
   - Uncertainty budget components (statistical, systematic, propagation)
   - Allan deviation (4 tau values)
   - Performance metrics (RMS, peak-peak, stability)
   - Quality grades per broadcast

3. **Detection Statistics:**
   - Robust noise floor values
   - Adaptive window sizes over time
   - SNR history per channel
   - False positive rates

**Potential Improvements:**

- New ionospheric dashboard showing layer heights, TEC, propagation modes
- Enhanced metrology panel with uncertainty breakdown and Allan deviation plots
- Detection statistics page showing

### Completed Features

- **Ionosphere Science Dashboard**:
  - Implemented `ionosphere-science.html` with Plotly charts for advanced propagation analysis.
  - Added backend API endpoints for WWV/WWVH discrimination, propagation residuals, and inferred layer heights.
  - Integrated with HDF5 data products (L1B discrimination, L2 timing).
  - Robust HDF5 readers with SWMR race condition handling.
- **Monitoring Server V3 (FastAPI)**: Fully replaced Node.js server.
- **L3 Fusion HDF5 Migration**: Fully implemented and robust.

**Data Sources:**

- HDF5 files: `/var/lib/timestd/phase2/{CHANNEL}/*.h5`
- Fusion CSV: `/var/lib/timestd/phase2/fusion/fused_d_clock.csv`
- GNSS VTEC: `/var/lib/timestd/gnss_vtec/*.h5`
- Analytics status: `/var/lib/timestd/phase2/{CHANNEL}/status/analytics-status.json`

**Web UI Stack:**

- Backend: Node.js/Express (`web-ui/server.js`)
- Frontend: Vanilla JS, Chart.js for plotting
- Real-time updates: 30s polling, WebSocket for audio

## System Architecture

### Two-Phase Pipeline

```
Phase 1: Core Recorder → raw_buffer/ (20kHz IQ, binary+JSON)
Phase 2: Analytics → phase2/{CHANNEL}/ (D_clock, discrimination, tones)
Phase 3: Fusion → Chrony SHM + fused_d_clock.csv
```

### Key Files

**Core Recording:**

- `src/hf_timestd/core/core_recorder_v2.py` - RTP capture via ka9q-python
- `src/hf_timestd/core/stream_recorder_v2.py` - Per-channel recording

**Analytics:**

- `src/hf_timestd/core/phase2_analytics_service.py` - Main analytics daemon
- `src/hf_timestd/core/phase2_temporal_engine.py` - Timing analysis algorithms
- `src/hf_timestd/core/tone_detector.py` - Tone detection with Phase 4 improvements
- `src/hf_timestd/core/ionospheric_model.py` - IRI-2020 integration

**Fusion:**

- `src/hf_timestd/core/multi_broadcast_fusion.py` - Multi-station Kalman fusion
- `src/hf_timestd/core/clock_convergence.py` - Convergence state tracking

**Web UI:**

- `web-ui/server.js` - Express server, API endpoints
- `web-ui/public/*.html` - Dashboard pages
- `web-ui/public/js/*.js` - Frontend visualization code

### Data Products

**HDF5 Schema:**

- L1A: Channel observables (carrier power, SNR, Doppler, tones)
- L1B: BCD timecode (discrimination results)
- L2: Timing measurements (D_clock with uncertainty)
- L3: Fusion results + GNSS VTEC

**CSV Outputs:**

- `clock_offset_series.csv` - D_clock time series
- `discrimination_{date}.csv` - Station ID results
- `tone_detections_{date}.csv` - 1000/1200 Hz detections
- `fused_d_clock.csv` - Multi-broadcast fusion output

## Development Workflow

### Making Changes

1. **Edit source:** `/home/mjh/git/hf-timestd/src/hf_timestd/`
2. **Copy to production:** `sudo cp -r src/hf_timestd /opt/hf-timestd/src/`
3. **Reinstall:** `cd /opt/hf-timestd && sudo /opt/hf-timestd/venv/bin/pip install -e .`
4. **Restart service:** `sudo systemctl restart timestd-analytics` (or relevant service)
5. **Monitor:** `sudo journalctl -u timestd-analytics -f`

### Testing

**Unit Tests:** `tests/test_*.py` (run with pytest)  
**Integration:** Process historical data from `raw_buffer/`  
**Web UI:** Check <http://localhost:3000> for visualization

### Git Workflow

```bash
git add -A
git commit -m "Descriptive message"
git push origin main
```

## Common Tasks

### Check System Status

```bash
systemctl status timestd-analytics  # Should show 9/9 channels
ps aux | grep phase2_analytics_service | wc -l  # Should be 9
tail -f /var/log/hf-timestd/phase2-shared10.log
```

### View Recent Detections

```bash
tail -20 /var/lib/timestd/phase2/SHARED_10000/clock_offset/clock_offset_series.csv
```

### Check Fusion Output

```bash
tail -20 /var/lib/timestd/phase2/fusion/fused_d_clock.csv
```

### Monitor Chrony

```bash
chronyc sources  # Should show SHM0 with * (selected)
watch -n 10 'chronyc sources'
```

## Key Concepts

### D_clock Measurement

```
D_clock = T_arrival - T_propagation
```

Where:

- T_arrival = Observed tone arrival time (from matched filter)
- T_propagation = HF signal propagation delay (ionospheric path)
- D_clock = System clock offset (the output we want)

### Propagation Modes

| Mode | Typical Delay | Uncertainty |
|------|---------------|-------------|
| 1-hop E | ~3.8 ms | ±0.20 ms |
| 1-hop F2 | ~4.3 ms | ±0.17 ms |
| 2-hop F2 | ~5.5 ms | ±0.33 ms |

### Convergence States

- **ACQUIRING** - Initial search, wide windows (±500ms)
- **CONVERGING** - Narrowing down, medium windows (±50ms)
- **LOCKED** - Stable lock, tight windows (±5-15ms)

## Documentation

**Essential:**

- `README.md` - Overview and quick start
- `ARCHITECTURE.md` - System design philosophy
- `TECHNICAL_REFERENCE.md` - API and algorithm details
- `CHANGELOG.md` - Version history

**Phase 4:**

- `docs/PHASE4_TONE_DETECTION.md` - Technical summary of improvements
- `tests/test_tone_detector_improvements.py` - Unit tests

**Timing:**

- `docs/TIMING_METROLOGY.md` - Metrological reference
- `docs/TIMING_METHODOLOGY.md` - D_clock measurement details

## Troubleshooting

**Service won't start:**

- Check module import: `sudo /opt/hf-timestd/venv/bin/python -c "import hf_timestd.core.phase2_analytics_service; print('OK')"`
- Verify source copied: `ls -la /opt/hf-timestd/src/hf_timestd/core/tone_detector.py`
- Check permissions: `ls -la /opt/hf-timestd/src/hf_timestd/`

**No detections:**

- Verify raw buffer has data: `ls -lh /dev/shm/timestd/raw_buffer/SHARED_10000/$(date +%Y%m%d)/`
- Check analytics logs: `tail -100 /var/log/hf-timestd/phase2-shared10.log`
- Monitor real-time: `sudo journalctl -u timestd-analytics -f`

**Web UI not updating:**

- Check Node.js server: `systemctl status timestd-web-ui`
- Verify data files exist: `ls -lh /var/lib/timestd/phase2/*/status/`
- Check browser console for errors

## AI Agent Guidance

### For Next Session (Web UI Improvements)

**Context to provide:**

1. Current web UI structure (`web-ui/` directory)
2. Available data sources (HDF5 schemas, CSV formats)
3. Existing visualization patterns (Chart.js usage)
4. API endpoint structure in `server.js`

**Key files to review:**

- `web-ui/server.js` - Backend API
- `web-ui/public/index.html` - Main dashboard
- `web-ui/public/js/monitoring.js` - Visualization code
- `src/hf_timestd/schemas/*.json` - HDF5 schemas

**Goals:**

- Create ionospheric dashboard
- Enhance metrology visualization
- Add detection statistics page
- Improve real-time data exposure

**Constraints:**

- Maintain existing functionality
- Use vanilla JS (no React/Vue)
- Keep 30s polling for most data
- Ensure mobile-responsive design

---

**For questions or clarification, refer to:**

- ARCHITECTURE.md for design decisions
- TECHNICAL_REFERENCE.md for API details
- CHANGELOG.md for recent changes
