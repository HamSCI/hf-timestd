# hf-timestd Web UI - FastAPI Implementation

Modern, modular web interface for HF Time Standard monitoring and analysis.

## Features

- **RESTful API** - Direct HDF5 access via DataProductReader
- **Real-time Updates** - WebSocket support for live data
- **Modular Design** - Independent pages, easy to extend
- **Quality Filtering** - Filter by quality grade, confidence, station
- **Time Range Queries** - Flexible time specifications (ISO8601 or relative)

## Quick Start

### Installation

```bash
cd web-api
pip install -r requirements.txt
```

### Run Development Server

```bash
python main.py
```

Server will start on http://localhost:8000

### API Documentation

Interactive API docs available at:
- Swagger UI: http://localhost:8000/api/docs
- ReDoc: http://localhost:8000/api/redoc

## API Endpoints

### Health Monitoring

```
GET /api/health/system       # Overall system health
GET /api/health/channels     # Channel status matrix
```

### Metrology

```
GET /api/metrology/fusion/latest                    # Latest fusion estimate
GET /api/metrology/fusion/history?start=-6h&end=now # Fusion time series
```

### Stability Analysis

```
GET /api/stability/adev?start=-24h&end=now          # Allan Deviation analysis
```

### Propagation & Ionospheric

```
GET /api/propagation/conditions                     # Current propagation conditions (per-broadcast)
GET /api/propagation/timeline?start=-6h&end=now     # Propagation mode timeline
GET /api/propagation/tec?start=-6h&end=now          # Per-path TEC with uncertainty
```

### Station

```
GET /api/station/metadata    # Station configuration
```

## Time Parameters

Endpoints support flexible time specifications:

**Relative:**
- `-6h` - 6 hours ago
- `-1d` - 1 day ago
- `-30m` - 30 minutes ago
- `now` - current time

**Absolute:**
- `2026-01-03T12:00:00Z` - ISO8601 timestamp

**Examples:**
```
# Last 6 hours
GET /api/metrology/fusion/history?start=-6h&end=now

# Last 24 hours
GET /api/metrology/fusion/history?start=-1d&end=now

# Specific range
GET /api/metrology/fusion/history?start=2026-01-03T00:00:00Z&end=2026-01-03T12:00:00Z

# Quality filtered
GET /api/metrology/fusion/history?start=-6h&end=now&min_quality=B
```

## Project Structure

```
web-api/
├── main.py              # FastAPI application
├── config.py            # Configuration loader
├── requirements.txt     # Python dependencies
├── models/              # Pydantic response models
│   ├── station.py
│   ├── timing.py
│   └── health.py
├── routers/             # API endpoints
│   ├── health.py
│   ├── metrology.py
│   ├── station.py
│   ├── stability.py
│   └── propagation.py
├── services/            # Business logic
│   ├── fusion_service.py
│   ├── health_service.py
│   ├── stability_service.py
│   └── propagation_service.py
└── static/              # Frontend files
    ├── index.html       # Station overview
    ├── health.html      # System health monitoring
    ├── metrology.html   # Fusion timing & Allan Deviation
    ├── propagation.html # Ionospheric & propagation analysis
    ├── js/
    │   └── common.js    # Shared utilities (API client, auto-refresh)
    └── css/
        └── styles.css   # Dark theme styling
```

## Implementation Status (v3.9.0 - Jan 3, 2026)

### ✅ Phase 1 Complete (Basic Monitoring)
- [x] FastAPI project structure with modular routers
- [x] Configuration loader (reads timestd-config.toml)
- [x] Pydantic models for API responses
- [x] FusionService (L3B fusion timing access)
- [x] HealthService (system and channel monitoring)
- [x] StabilityService (Allan Deviation analysis)
- [x] API routers (health, metrology, station, stability)
- [x] **Station Overview page** (`/`) - Metadata, recent activity, quick links
- [x] **System Health page** (`/health`) - Process status, channel matrix, disk usage
- [x] **Metrology Dashboard** (`/metrology`) - Fusion timing, ISO GUM uncertainty, Allan Deviation

### ✅ Phase 2 Complete (Advanced Analysis)
- [x] PropagationService (ionospheric and propagation data)
- [x] **Propagation Analysis page** (`/propagation`):
  - [x] Per-broadcast propagation modes (not misleading global aggregation)
  - [x] Multi-frequency comparison by station (WWV, WWVH, CHU, BPM)
  - [x] Per-path TEC visualization with error bars and quality indicators
  - [x] Propagation mode timeline (color-coded by mode)
  - [x] Validated broadcast schedules (filters impossible combinations)
- [x] Allan Deviation analysis (τ=1s to 10,000s)
- [x] Noise identification (white, flicker, random walk)
- [x] Auto-refresh (60s) on all pages
- [x] Time range selection (6h, 24h)
- [x] Responsive Plotly.js visualizations

### 📋 Future Enhancements
- [ ] WebSocket support for real-time updates
- [ ] IRI model comparison for TEC validation
- [ ] Export functionality (CSV/JSON)
- [ ] Per-frequency-pair TEC (requires analytics pipeline update)
- [ ] Unit tests and performance benchmarks

## Development

### Adding New Endpoints

1. Create service in `services/`
2. Add Pydantic models in `models/`
3. Create router in `routers/`
4. Include router in `main.py`

### Adding New Pages

1. Create HTML in `static/`
2. Add navigation links
3. Create corresponding API endpoints
4. Test and document

## Configuration

Reads from `../config/timestd-config.toml`:
- Station metadata
- Channel configuration
- Data paths
- Operating mode

## Data Access

Uses existing `DataProductReader` from `hf_timestd.io.hdf5_reader`:
- SWMR mode for concurrent access
- Quality filtering
- Time range queries
- Efficient chunked reading

## Performance

Target performance (Phase 1):
- Latest value: < 10 ms
- Hour time series: < 100 ms
- Day time series: < 500 ms

## Testing

```bash
# Run tests (when implemented)
pytest

# Manual API testing
curl http://localhost:8000/api/health/system
curl http://localhost:8000/api/metrology/fusion/latest
curl "http://localhost:8000/api/metrology/fusion/history?start=-6h&end=now"
```

## Production Deployment

```bash
# Install dependencies
pip install -r requirements.txt

# Run with uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

# Or use systemd service (to be created)
systemctl start timestd-web-ui
```

## License

Same as hf-timestd project
