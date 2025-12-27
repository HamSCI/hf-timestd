# HF-TimeStd Project Context

**Last Updated:** 2025-12-27  
**Next Session Focus:** Web UI Redesign for Timing & Ionospheric Data Visualization

---

## Current System Status (2025-12-27)

### ✅ All Systems Operational

- **Core Recorder**: Running, receiving RTP from 9 channels
- **Analytics Service**: Processing all channels, writing HDF5 data products
- **Fusion Service**: Calculating D_clock, feeding Chrony SHM (Grade A, ±0.13ms)
- **Chrony Integration**: TMGR receiving updates, system clock disciplined
- **Web UI**: FastAPI server running on port 3000

### Recent Fixes (This Session)

1. **Service Startup**: Fixed `/dev/shm/timestd` persistence via tmpfiles.d
2. **Chrony SHM**: Resolved permission race condition with service ordering
3. **Boot Order**: All services start correctly on reboot
4. **Chrony Mandatory**: Required for production installations

---

## Next Session: Web UI Redesign

### Objective

**Completely re-think the web UI** to present timing and ionospheric data in a modern, intuitive, and scientifically meaningful way.

### Current Web UI State

**Active Pages:**

- `summary.html` - System status dashboard
- `timing.html` - Timing analysis (157KB, complex)
- `ionosphere.html` - Ionospheric data (51KB)
- `logs.html` - System logs viewer
- `index.html` - Entry point

**Server:**

- `monitoring-server-v3.js` - Express.js API server (194KB)
- `monitoring_server.py` - FastAPI alternative (20KB, newer)

**Issues:**

- Timing dashboard is overly complex and hard to understand
- No clear narrative or user journey
- Mixing too many concepts on single pages
- Not mobile-responsive
- Lacks modern UX patterns

---

## Data Products Available

### L1A: Channel Observables (HDF5)

**Location:** `/var/lib/timestd/phase2/{CHANNEL}/carrier_power/`

**Schema:** `channel_observables_v1.json`

**Data:**

- Carrier power (dBm)
- SNR (dB)
- Doppler shift (Hz)
- Tone detections (presence, amplitude, phase)
- Quality grades (A/B/C/D)

**Use Cases:**

- Signal strength visualization
- Propagation mode detection
- Channel health monitoring

### L1B: BCD Timecode (HDF5)

**Location:** `/var/lib/timestd/phase2/{CHANNEL}/bcd_discrimination/`

**Schema:** `bcd_timecode_v1.json`

**Data:**

- Decoded time (UTC)
- Confidence scores
- Bit-level quality
- Station identification (WWV/WWVH/CHU discrimination)

**Use Cases:**

- Timecode decoding accuracy
- Station discrimination visualization
- Error analysis

### L2: Timing Measurements (HDF5)

**Location:** `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/`

**Schema:** `timing_measurements_v1.json`

**Data:**

- D_clock (system clock offset from UTC, ms)
- Propagation delay (ms)
- Propagation mode (1E, 1F, 2F, 3F, GW)
- Uncertainty (ms)
- Quality grade (A/B/C/D)
- Confidence score (0-1)

**Use Cases:**

- Clock accuracy visualization
- Propagation analysis
- Quality trending

### L3: Fused Timing (CSV)

**Location:** `/var/lib/timestd/phase2/fusion/`

**Data:**

- Fused D_clock from all broadcasts
- Kalman-filtered uncertainty
- Per-station contributions
- Quality flags

**Use Cases:**

- System clock accuracy
- Multi-broadcast fusion visualization
- Chrony feed status

### Science Products

**TEC Data:** `/var/lib/timestd/phase2/science/tec/`

- Total Electron Content estimates
- Ionospheric delay corrections
- Multi-frequency analysis

**Propagation:** Embedded in L2 timing measurements

- Mode classification (E-layer, F-layer, multi-hop)
- Solar zenith angle correlation
- Day/night transitions

---

## Web UI Design Considerations

### User Personas

1. **Operator** - Wants to know: "Is the system working?"
2. **Scientist** - Wants to analyze: "What's the propagation doing?"
3. **Developer** - Wants to debug: "Why did this fail?"

### Key Visualizations Needed

#### 1. Timing Dashboard

- **Kalman Funnel**: Convergence over time (uncertainty narrowing)
- **Per-Station Offsets**: WWV, WWVH, CHU, BPM contributions
- **Quality Timeline**: A/B/C/D grades over 24h
- **Chrony Status**: Is system clock being disciplined?

#### 2. Ionosphere Dashboard

- **TEC Map**: Spatial/temporal visualization
- **Propagation Modes**: E/F layer transitions
- **Solar Correlation**: Zenith angle vs signal quality
- **Multi-Frequency Analysis**: Dispersion visualization

#### 3. Signal Quality Dashboard

- **SNR Heatmap**: All channels, 24h
- **Carrier Power**: Strength over time
- **Tone Detection**: Success rates
- **Data Completeness**: Gaps and coverage

#### 4. System Health Dashboard

- **Service Status**: All timestd services
- **Data Flow**: Pipeline visualization
- **Error Rates**: Quality grade distribution
- **Storage**: Disk usage, file counts

### Technical Requirements

**Must Have:**

- HDF5 data reading (via FastAPI backend)
- Real-time updates (WebSocket or polling)
- Responsive design (mobile-friendly)
- Fast load times (\u003c2s)
- Accessible (WCAG 2.1 AA)

**Nice to Have:**

- Dark mode
- Exportable charts (PNG/SVG)
- Configurable time ranges
- Bookmark/share specific views
- Offline mode

### Technology Stack Options

**Current:**

- Express.js (Node.js) backend
- Vanilla JavaScript frontend
- Chart.js for visualizations

**Alternative (FastAPI):**

- Python FastAPI backend (already deployed)
- Modern frontend framework (React/Vue/Svelte)
- Plotly/D3.js for advanced visualizations

**Hybrid:**

- Keep FastAPI backend for HDF5 reading
- Modernize frontend with component framework
- Progressive enhancement

---

## Data Access Patterns

### HDF5 Reading (Python)

```python
from hf_timestd.io import DataProductReader

# Read L2 timing measurements
reader = DataProductReader(
    channel_name="WWV_20000",
    data_root="/var/lib/timestd",
    product_type="timing_measurements"
)

data = reader.read_time_range(
    start_time=datetime(...),
    end_time=datetime(...),
    quality_grade="C"  # Minimum quality
)
```

### API Endpoints (Current)

```
GET /api/v1/summary              - System overview
GET /api/v1/channels/status      - Channel health
GET /api/v1/timing/measurements  - L2 timing data
GET /api/v1/ionosphere/tec       - TEC estimates
GET /api/v1/fusion/status        - Multi-broadcast fusion
```

### API Endpoints (Needed for New UI)

```
GET /api/v2/timing/kalman-funnel?hours=24
GET /api/v2/timing/station-offsets?date=YYYYMMDD
GET /api/v2/ionosphere/tec-map?date=YYYYMMDD
GET /api/v2/signal/snr-heatmap?hours=24
GET /api/v2/system/health
```

---

## Design Inspiration

### Similar Systems

- **GPS Monitor**: Clean, real-time satellite tracking
- **Grafana**: Time-series visualization excellence
- **FlightAware**: Live tracking with clear status
- **NOAA Space Weather**: Scientific data for public

### Key Principles

1. **Progressive Disclosure**: Simple overview → detailed analysis
2. **Visual Hierarchy**: Most important info first
3. **Consistent Language**: Use domain terms correctly
4. **Error States**: Clear feedback when data missing
5. **Performance**: Fast, even with large datasets

---

## Files to Review

### Current Web UI

- `web-ui/summary.html` - Current main dashboard
- `web-ui/timing.html` - Timing analysis (needs redesign)
- `web-ui/ionosphere.html` - Ionospheric data
- `web-ui/monitoring-server-v3.js` - API server
- `web-ui/WEB_UI_ARCHITECTURE.md` - Architecture docs

### Data Schemas

- `schemas/l1_channel_observables_v1.json`
- `schemas/l1_bcd_timecode_v1.json`
- `schemas/l2_timing_measurements_v1.json`

### Backend

- `src/hf_timestd/io/hdf5_reader.py` - HDF5 data access
- `web-ui/monitoring_server.py` - FastAPI server

---

## Success Criteria for Next Session

### Must Achieve

- ✅ Clear user journey (operator → scientist → developer)
- ✅ Simplified timing dashboard with Kalman funnel
- ✅ Ionospheric visualization with TEC map
- ✅ Mobile-responsive design
- ✅ Fast load times (\u003c2s for main dashboard)

### Nice to Have

- ✅ Dark mode toggle
- ✅ Exportable charts
- ✅ Real-time WebSocket updates
- ✅ Configurable time ranges

---

## Station Info (AC0G)

- **Callsign:** AC0G
- **Grid Square:** EM38ww40pk
- **Location:** 38.918461°N, 92.127974°W (Columbia, MO)
- **Instrument ID:** 172
- **Data Root:** /var/lib/timestd

### Channels (9 total)

- SHARED_2500, SHARED_5000, SHARED_10000, SHARED_15000 (WWV+WWVH+BPM)
- WWV_20000, WWV_25000 (WWV-only)
- CHU_3330, CHU_7850, CHU_14670 (CHU-only)

---

## Quick Start Commands

```bash
# Start FastAPI server
cd /home/mjh/git/hf-timestd/web-ui
python monitoring_server.py

# View HDF5 data
python -c "from hf_timestd.io import DataProductReader; ..."

# Check current web UI
firefox http://localhost:3000

# View logs
journalctl -u timestd-web-ui -f
```

---

**Key Takeaway:** The web UI should tell a clear story about system health, timing accuracy, and ionospheric conditions. Prioritize clarity over complexity.
