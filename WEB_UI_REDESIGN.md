# Web UI Redesign - Modular FastAPI Architecture

**Date:** 2026-01-03  
**Objective:** Incremental, modular web UI using FastAPI + DataProductReader  
**Approach:** Build one reliable, tested display at a time

---

## Design Principles

### 1. Modular Architecture
- **Each page is independent** - can be developed, tested, and deployed separately
- **Shared components** - reusable API endpoints and frontend utilities
- **Plugin-style additions** - new analyses (e.g., WWV test signal) add cleanly

### 2. Technology Stack
- **Backend:** FastAPI (already installed) + DataProductReader (existing HDF5 access)
- **Frontend:** Modern HTML5 + Plotly.js (already in use) + minimal JavaScript
- **Data Access:** Direct HDF5 via existing `DataProductReader` class
- **Real-time:** WebSocket for live updates (where needed)

### 3. Quality Standards
- Each display must be **tested** before moving to next
- **Performance benchmarks** - query times documented
- **Error handling** - graceful degradation when data unavailable
- **Responsive design** - works on desktop and tablet

---

## Page Organization

### Category 1: Station Description & Status

**Purpose:** System health, configuration, and operational status

#### 1.1 Station Overview (`/`)
- **Priority:** Phase 1 (Week 1)
- **Content:**
  - Station metadata (callsign, location, grid square, instrument ID)
  - System uptime and health indicators
  - Active channels and frequencies
  - Current mode (production/test)
  - Quick links to other sections

#### 1.2 System Health (`/health`)
- **Priority:** Phase 1 (Week 1)
- **Content:**
  - Process status (recorder, analytics, services)
  - Channel status matrix (9 channels × status)
  - Data completeness indicators
  - Recent errors/warnings
  - Disk usage and buffer status

#### 1.3 Channel Status (`/channels`)
- **Priority:** Phase 2 (Week 2-3)
- **Content:**
  - Per-channel detailed view
  - Signal strength (carrier power, SNR)
  - Tone detection status (WWV 500/600 Hz, WWVH 1200/1500 Hz, CHU)
  - Data quality flags
  - Recent observations timeline

#### 1.4 Station Metadata (`/station`)
- **Priority:** Phase 3 (Week 4+)
- **Content:**
  - Broadcast station details (WWV, WWVH, CHU, BPM)
  - Frequencies and schedules
  - Expected propagation delays
  - Geographic visualization (map)
  - Broadcast schedules (ground truth minutes, calibration windows)

---

### Category 2: Metrology

**Purpose:** Timing measurements, uncertainty budgets, traceability

#### 2.1 UTC Offset Dashboard (`/metrology`)
- **Priority:** Phase 1 (Week 1)
- **Content:**
  - **Hero Display:** Current D_clock with uncertainty (large, prominent)
  - Quality grade badge (A/B/C/D)
  - Station contributions (pie chart or bar)
  - Recent fusion history (last 6 hours)
  - Traceability chain indicator

#### 2.2 Fusion Timing (`/metrology/fusion`)
- **Priority:** Phase 2 (Week 2-3)
- **Content:**
  - Fusion time series (selectable time range)
  - Uncertainty components breakdown (statistical, systematic, propagation)
  - Station contribution evolution
  - Inter-station consistency metrics
  - Kalman filter state
  - Outlier rejection statistics

#### 2.3 Station Timing (`/metrology/stations`)
- **Priority:** Phase 2 (Week 2-3)
- **Content:**
  - Per-station timing measurements (L2 data)
  - Multi-station comparison at same frequency
  - Quality grade distribution
  - Uncertainty budget details (Type A and Type B components)
  - Discrimination confidence

#### 2.4 Uncertainty Analysis (`/metrology/uncertainty`)
- **Priority:** Phase 3 (Week 4+)
- **Content:**
  - Uncertainty budget breakdown over time
  - Component contributions (RTP, ionospheric, multipath, etc.)
  - Quality grade trends
  - Expanded uncertainty (U = k × u_c) evolution
  - Coverage factor analysis

#### 2.5 Traceability (`/metrology/traceability`)
- **Priority:** Phase 3 (Week 4+)
- **Content:**
  - Traceability chain documentation
  - GPSDO calibration history
  - UTC verification status
  - Multi-station verification
  - Calibration certificates and dates

---

### Category 3: Ionospheric/Propagation Studies

**Purpose:** Space weather, propagation modes, TEC analysis

#### 3.1 Propagation Overview (`/propagation`)
- **Priority:** Phase 2 (Week 2-3)
- **Content:**
  - Current propagation conditions summary
  - MUF estimates per station
  - Mode probability distribution (1E, 1F, 2F, etc.)
  - Recent propagation mode timeline
  - SNR trends by frequency

#### 3.2 Propagation Modes (`/propagation/modes`)
- **Priority:** Phase 2 (Week 2-3)
- **Content:**
  - Mode probability heatmap (frequency × time-of-day)
  - Diurnal variation analysis
  - Mode transition events
  - Delay spread statistics
  - Doppler shift analysis

#### 3.3 TEC Analysis (`/propagation/tec`)
- **Priority:** Phase 3 (Week 4+)
- **Content:**
  - HF-derived TEC time series
  - GPS VTEC comparison
  - TEC validation metrics (bias, residuals)
  - Multi-frequency dispersion plots
  - Confidence indicators

#### 3.4 Ionospheric Conditions (`/propagation/ionosphere`)
- **Priority:** Phase 3 (Week 4+)
- **Content:**
  - Solar zenith angle overlay
  - Day/night terminator effects
  - Seasonal trends
  - Space weather indices (if available)
  - Propagation forecasting

#### 3.5 Signal Analysis (`/propagation/signals`)
- **Priority:** Phase 4 (Future)
- **Content:**
  - Carrier Doppler analysis
  - Phase variance and coherence time
  - Multipath characterization
  - **WWV Test Signal Analysis** (modular addition)
  - Audio tone analysis (500/600 Hz WWV, 1200/1500 Hz WWVH)

---

## Implementation Sequence

### Phase 1: Core Functionality (Week 1)
**Goal:** Basic monitoring and primary metrology display

1. **FastAPI Service Setup**
   - Project structure
   - DataProductReader integration
   - Basic API endpoints
   - Static file serving

2. **Station Overview** (`/`)
   - Station metadata display
   - System health summary
   - Navigation to other pages

3. **System Health** (`/health`)
   - Process status
   - Channel matrix
   - Data completeness

4. **UTC Offset Dashboard** (`/metrology`)
   - Current fusion estimate (hero display)
   - Quality grade
   - Recent history (6 hours)
   - Station contributions

**Deliverables:**
- Working FastAPI service
- 3 tested pages
- API documentation (OpenAPI)
- Performance benchmarks

---

### Phase 2: Detailed Analysis (Weeks 2-3)
**Goal:** Expand metrology and add propagation analysis

5. **Fusion Timing** (`/metrology/fusion`)
   - Time series with selectable range
   - Uncertainty breakdown
   - Station contributions

6. **Station Timing** (`/metrology/stations`)
   - Per-station measurements
   - Multi-station comparison
   - Quality metrics

7. **Channel Status** (`/channels`)
   - Detailed per-channel view
   - Signal strength
   - Tone detection

8. **Propagation Overview** (`/propagation`)
   - Current conditions
   - MUF estimates
   - Mode distribution

9. **Propagation Modes** (`/propagation/modes`)
   - Mode heatmap
   - Diurnal analysis
   - Delay spread

**Deliverables:**
- 5 additional pages
- WebSocket support for real-time updates
- Query optimization for time-range requests
- Export functionality (CSV/JSON)

---

### Phase 3: Advanced Features (Week 4+)
**Goal:** Complete metrology and ionospheric analysis

10. **Station Metadata** (`/station`)
    - Broadcast station details
    - Geographic visualization
    - Schedules

11. **Uncertainty Analysis** (`/metrology/uncertainty`)
    - Budget breakdown
    - Component trends
    - Quality analysis

12. **Traceability** (`/metrology/traceability`)
    - Chain documentation
    - Calibration history
    - Verification status

13. **TEC Analysis** (`/propagation/tec`)
    - HF vs GPS comparison
    - Validation metrics
    - Dispersion plots

14. **Ionospheric Conditions** (`/propagation/ionosphere`)
    - Solar effects
    - Seasonal trends
    - Forecasting

**Deliverables:**
- Complete metrology suite
- Full ionospheric analysis
- Data export for all pages
- User documentation

---

### Phase 4: Extensions (Future)
**Goal:** Specialized analyses and enhancements

15. **Signal Analysis** (`/propagation/signals`)
    - Doppler analysis
    - Multipath characterization
    - **WWV Test Signal Module** (pluggable)

16. **Historical Analysis Tools**
    - Long-term trends
    - Statistical summaries
    - Anomaly detection

17. **Mobile Optimization**
    - Responsive layouts
    - Touch-friendly controls
    - Progressive Web App features

---

## Technical Architecture

### Backend Structure

```
web-api/
├── main.py                      # FastAPI application entry point
├── config.py                    # Configuration (data paths, etc.)
├── models/                      # Pydantic models for API responses
│   ├── __init__.py
│   ├── station.py              # Station metadata models
│   ├── timing.py               # Timing measurement models
│   └── propagation.py          # Propagation models
├── routers/                     # API route modules
│   ├── __init__.py
│   ├── health.py               # /api/health endpoints
│   ├── metrology.py            # /api/metrology endpoints
│   ├── propagation.py          # /api/propagation endpoints
│   └── station.py              # /api/station endpoints
├── services/                    # Business logic layer
│   ├── __init__.py
│   ├── data_reader.py          # DataProductReader wrapper
│   ├── fusion_service.py       # Fusion timing queries
│   ├── timing_service.py       # L2 timing queries
│   └── propagation_service.py  # L3C propagation queries
├── static/                      # Frontend files
│   ├── css/
│   │   └── styles.css
│   ├── js/
│   │   ├── common.js           # Shared utilities
│   │   ├── plots.js            # Plotly helpers
│   │   └── websocket.js        # WebSocket client
│   └── pages/
│       ├── index.html
│       ├── health.html
│       ├── metrology.html
│       └── ...
└── tests/                       # Unit and integration tests
    ├── test_routers.py
    ├── test_services.py
    └── test_data_reader.py
```

### API Endpoint Design

**RESTful Conventions:**
```
GET  /api/health/system              # System health status
GET  /api/health/channels            # Channel status matrix

GET  /api/metrology/fusion/latest    # Latest fusion estimate
GET  /api/metrology/fusion/history   # Time series (query params: start, end)
GET  /api/metrology/stations         # List of stations
GET  /api/metrology/timing           # L2 measurements (filters: station, freq, start, end)
GET  /api/metrology/uncertainty      # Uncertainty budget breakdown

GET  /api/propagation/current        # Current propagation conditions
GET  /api/propagation/modes          # Mode statistics (filters: station, freq, start, end)
GET  /api/propagation/tec            # TEC estimates
GET  /api/propagation/muf            # MUF estimates

GET  /api/station/metadata           # Station configuration
GET  /api/station/broadcasts         # Broadcast schedules

WS   /ws/fusion                      # Real-time fusion updates
WS   /ws/health                      # Real-time health updates
```

**Query Parameters (standardized):**
- `start`: ISO8601 timestamp or relative (e.g., "-6h", "-1d")
- `end`: ISO8601 timestamp or "now"
- `station`: Station filter (WWV, WWVH, CHU, BPM, ALL)
- `frequency`: Frequency filter in MHz
- `quality`: Quality grade filter (A, B, C, D)
- `limit`: Maximum records to return
- `format`: Response format (json, csv)

### Data Access Layer

**Service Pattern:**
```python
# services/fusion_service.py
from hf_timestd.io.hdf5_reader import DataProductReader
from datetime import datetime, timedelta
from pathlib import Path

class FusionService:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.fusion_dir = data_root / 'phase2' / 'fusion'
    
    def get_latest(self) -> dict:
        """Get latest fusion estimate."""
        reader = DataProductReader(
            output_dir=self.fusion_dir,
            product_level='L3',
            product_name='fusion_timing',
            channel='fusion'
        )
        
        # Read last record from today's file
        data = reader.read_latest()
        
        return {
            'timestamp': data['timestamp_utc'][-1],
            'd_clock_ms': data['d_clock_fused_ms'][-1],
            'uncertainty_ms': data['uncertainty_ms'][-1],
            'quality_grade': data['quality_grade'][-1],
            'n_broadcasts': data['n_broadcasts'][-1],
            'stations_used': data['stations_used'][-1].split(',')
        }
    
    def get_history(self, start: datetime, end: datetime) -> dict:
        """Get fusion time series."""
        reader = DataProductReader(
            output_dir=self.fusion_dir,
            product_level='L3',
            product_name='fusion_timing',
            channel='fusion'
        )
        
        # Read data across date range (handles multiple files)
        data = reader.read_range(start, end)
        
        return {
            'timestamps': data['timestamp_utc'].tolist(),
            'd_clock_ms': data['d_clock_fused_ms'].tolist(),
            'uncertainty_ms': data['uncertainty_ms'].tolist(),
            'quality_grade': data['quality_grade'].tolist(),
            'n_broadcasts': data['n_broadcasts'].tolist()
        }
```

**Router Pattern:**
```python
# routers/metrology.py
from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from services.fusion_service import FusionService
from models.timing import FusionResponse, FusionHistoryResponse

router = APIRouter(prefix="/api/metrology", tags=["metrology"])

@router.get("/fusion/latest", response_model=FusionResponse)
async def get_latest_fusion():
    """Get latest fusion timing estimate."""
    try:
        service = FusionService(data_root=config.DATA_ROOT)
        return service.get_latest()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/fusion/history", response_model=FusionHistoryResponse)
async def get_fusion_history(
    start: str = Query("-6h", description="Start time (ISO8601 or relative)"),
    end: str = Query("now", description="End time (ISO8601 or relative)")
):
    """Get fusion timing history."""
    try:
        # Parse time parameters
        start_dt = parse_time_param(start)
        end_dt = parse_time_param(end)
        
        service = FusionService(data_root=config.DATA_ROOT)
        return service.get_history(start_dt, end_dt)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### Frontend Components

**Shared JavaScript Utilities:**
```javascript
// static/js/common.js

// API client
class TimestdAPI {
    constructor(baseURL = '/api') {
        this.baseURL = baseURL;
    }
    
    async get(endpoint, params = {}) {
        const url = new URL(endpoint, window.location.origin + this.baseURL);
        Object.keys(params).forEach(key => 
            url.searchParams.append(key, params[key])
        );
        
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`API error: ${response.statusText}`);
        }
        return response.json();
    }
}

// Time formatting
function formatTimestamp(iso8601) {
    return new Date(iso8601).toLocaleString();
}

// Quality grade colors
const QUALITY_COLORS = {
    'A': '#10b981',  // green
    'B': '#3b82f6',  // blue
    'C': '#f59e0b',  // orange
    'D': '#ef4444'   // red
};

// WebSocket helper
class TimestdWebSocket {
    constructor(endpoint) {
        this.endpoint = endpoint;
        this.ws = null;
        this.reconnectInterval = 5000;
    }
    
    connect(onMessage) {
        const wsURL = `ws://${window.location.host}/ws${this.endpoint}`;
        this.ws = new WebSocket(wsURL);
        
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            onMessage(data);
        };
        
        this.ws.onclose = () => {
            setTimeout(() => this.connect(onMessage), this.reconnectInterval);
        };
    }
}
```

**Plotly Helpers:**
```javascript
// static/js/plots.js

function createTimeSeriesPlot(containerId, data, options = {}) {
    const trace = {
        x: data.timestamps,
        y: data.values,
        type: 'scatter',
        mode: 'lines+markers',
        name: options.name || 'Data',
        line: { color: options.color || '#3b82f6' }
    };
    
    const layout = {
        title: options.title || '',
        xaxis: { title: 'Time (UTC)' },
        yaxis: { title: options.yaxis || 'Value' },
        template: 'plotly_dark',
        margin: { t: 40, r: 20, b: 40, l: 60 }
    };
    
    Plotly.newPlot(containerId, [trace], layout, {responsive: true});
}

function createUncertaintyPlot(containerId, data) {
    const trace = {
        x: data.timestamps,
        y: data.values,
        error_y: {
            type: 'data',
            array: data.uncertainties,
            visible: true
        },
        type: 'scatter',
        mode: 'markers',
        marker: { color: '#3b82f6' }
    };
    
    const layout = {
        title: 'Timing with Uncertainty',
        xaxis: { title: 'Time (UTC)' },
        yaxis: { title: 'D_clock (ms)' },
        template: 'plotly_dark'
    };
    
    Plotly.newPlot(containerId, [trace], layout, {responsive: true});
}
```

---

## Modular Extension Example: WWV Test Signal

**When ready to add WWV test signal analysis:**

1. **Create new service:**
```python
# services/test_signal_service.py
class TestSignalService:
    def get_test_signal_events(self, start, end):
        # Read L1A channel observables
        # Filter for test_signal_detected == True
        # Return events with SNR and timing
        pass
```

2. **Add API endpoint:**
```python
# routers/propagation.py
@router.get("/signals/test-signal")
async def get_test_signal_analysis(...):
    service = TestSignalService(data_root=config.DATA_ROOT)
    return service.get_test_signal_events(start, end)
```

3. **Create page:**
```html
<!-- static/pages/test-signal.html -->
<!-- Displays test signal events, timing, SNR trends -->
```

4. **Add to navigation:**
```javascript
// Update navigation menu to include link
```

**No changes needed to existing pages or core infrastructure.**

---

## Testing Strategy

### Unit Tests
- Each service class tested independently
- Mock HDF5 data for reproducibility
- Test error handling (missing files, corrupt data)

### Integration Tests
- API endpoint testing with FastAPI TestClient
- End-to-end data flow (HDF5 → Service → API → JSON)
- Performance benchmarks

### Frontend Tests
- Manual testing checklist per page
- Browser compatibility (Chrome, Firefox, Safari)
- Responsive design verification

### Performance Benchmarks
- Latest value query: < 10 ms
- Hour time series: < 100 ms
- Day time series: < 500 ms
- Week aggregation: < 2 s

---

## Migration from Current Web UI

### Parallel Operation
- Keep existing Node.js server running (port 3000)
- Run new FastAPI server on different port (port 8000)
- Gradually migrate pages one at a time
- Test new pages before deprecating old ones

### Data Path Compatibility
- Use same HDF5 files (no data migration needed)
- Leverage existing `DataProductReader` class
- Maintain compatibility with current file structure

### Cutover Strategy
1. **Phase 1:** New pages available at `/new/...` URLs
2. **Phase 2:** Side-by-side comparison and validation
3. **Phase 3:** Switch default routes to new pages
4. **Phase 4:** Deprecate old Node.js server

---

## Documentation Requirements

### Per-Page Documentation
- **Purpose:** What the page shows and why
- **Data Sources:** Which HDF5 files and fields
- **Update Frequency:** Real-time, polling interval, or static
- **Interpretation Guide:** How to read the displays
- **Known Limitations:** What the page doesn't show

### API Documentation
- **OpenAPI/Swagger:** Auto-generated from FastAPI
- **Example Queries:** Common use cases with curl/Python examples
- **Response Schemas:** Pydantic models documented
- **Error Codes:** HTTP status codes and meanings

### Developer Guide
- **Adding New Pages:** Step-by-step process
- **Service Pattern:** How to create new services
- **Testing:** How to write and run tests
- **Deployment:** How to deploy updates

---

## Success Criteria

### Phase 1 Complete When:
- ✅ FastAPI service running and stable
- ✅ 3 core pages functional (overview, health, metrology)
- ✅ Real-time updates working
- ✅ Performance benchmarks met
- ✅ Documentation complete
- ✅ User acceptance testing passed

### Phase 2 Complete When:
- ✅ 5 additional pages functional
- ✅ All metrology displays working
- ✅ Propagation analysis available
- ✅ Export functionality implemented
- ✅ Cross-browser tested

### Phase 3 Complete When:
- ✅ All planned pages implemented
- ✅ Advanced features working
- ✅ Complete test coverage
- ✅ Production-ready deployment
- ✅ Old Node.js server deprecated

---

## Next Steps

1. **Review and approve this plan**
2. **Set up FastAPI project structure**
3. **Implement Phase 1, Display 1: Station Overview**
4. **Test and iterate**
5. **Continue incrementally through sequence**

**Estimated Timeline:**
- Phase 1: 1 week
- Phase 2: 2-3 weeks
- Phase 3: 4+ weeks
- **Total:** 7-8 weeks for complete implementation

**Advantages of This Approach:**
- ✅ Incremental progress with working features at each step
- ✅ Early user feedback on design and functionality
- ✅ Modular architecture enables easy extensions
- ✅ Parallel operation minimizes disruption
- ✅ Each phase delivers value independently
