# Web UI Architecture - hf-timestd

**Last Updated**: Nov 28, 2025  
**Status**: Beta Testing

---

## Design Principle: Separation of Concerns

**Core Principle**: The web UI is a **presentation layer only**.

### What the Web UI Knows
✅ **WHERE** data files are located (via `timestd-paths.js`)  
✅ **HOW** to display data effectively (charts, tables, visualizations)  
✅ **WHEN** to refresh/update displays

### What the Web UI Does NOT Know
❌ **WHAT** the data represents scientifically  
❌ **HOW** data is generated or processed  
❌ **WHY** specific values occur

**Rationale**: All domain logic resides in the analytics service (operational app). The web UI is replaceable without affecting data processing.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│ Analytics Service (Python)                                       │
│ ├─ Data generation logic                                         │
│ ├─ Scientific algorithms                                         │
│ └─ Writes to standard locations                                  │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          │ File System
                          ↓
┌─────────────────────────────────────────────────────────────────┐
│ timestd-paths.js (Node.js)                                        │
│ ├─ Definitive data location reference                            │
│ ├─ Synchronized with Python PathConfig                           │
│ └─ Single source of truth for file locations                     │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          │ Import/Require
                          ↓
┌─────────────────────────────────────────────────────────────────┐
│ monitoring-server-v3.js (Express.js)                             │
│ ├─ REST API endpoints                                            │
│ ├─ Reads data from paths                                         │
│ ├─ Aggregates/formats for display                                │
│ └─ Serves static HTML/JS/CSS                                     │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          │ HTTP
                          ↓
┌─────────────────────────────────────────────────────────────────┐
│ HTML Dashboards (Browser)                                        │
│ ├─ summary.html       - Main system status                       │
│ ├─ carrier.html       - Carrier analysis                         │
│ ├─ discrimination.html - WWV/WWVH 6-method discrimination         │
│ ├─ timing-dashboard-enhanced.html - Timing quality metrics       │
│ └─ gaps.html          - Gap analysis                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. timestd-paths.js (Data Location Authority)

**Purpose**: Single source of truth for all data file locations

**Synchronization**: Must stay in sync with Python `TimeStdPaths` class (`src/hf_timestd/paths.py`)

**Key Paths**:
```javascript
{
  dataRoot: "/path/to/data",
  raw_buffer: "/path/to/data/raw_buffer/{channel}/{YYYYMMDD}/",
  phase2: "/path/to/data/phase2/{channel}/",
  discrimination: "/path/to/data/phase2/{channel}/discrimination/{date}.csv",
  bcd_discrimination: "/path/to/data/phase2/{channel}/bcd_discrimination/{date}.csv",
  status: {
    core: "/path/to/data/status/core_recorder_status.json",
    analytics: "/path/to/data/status/analytics_status.json"
  }
}
```

**Usage**:
```javascript
const paths = require('./timestd-paths.js');
const archivePath = paths.getArchiveDir('WWV_5.0_MHz');
```

**Critical**: When Python PathConfig changes, `timestd-paths.js` must be updated.

### 2. monitoring-server-v3.js (API Server)

**Purpose**: REST API and static file server

**Responsibilities**:
- Serve HTML/JS/CSS static files
- Provide JSON API endpoints
- Read data files (using timestd-paths.js)
- Format data for display (no domain logic)
- Health checks and system status

**Does NOT**:
- Generate data
- Perform scientific calculations
- Understand data meaning

**Key Endpoints**:
```
GET /api/v1/summary              - All dashboard data
GET /api/v1/system/status        - System processes
GET /api/v1/channels/status      - Per-channel status
GET /api/v1/carrier/quality      - Carrier metrics
GET /api/v1/channels/:name/discrimination/:date - Discrimination data
```

### 3. HTML Dashboards

#### summary.html
- **Purpose**: System overview and health
- **Data**: System status, channel health, completeness
- **Updates**: Real-time polling (5s interval)

#### carrier.html
- **Purpose**: Carrier analysis
- **Data**: Phase 2 carrier power/quality metrics
- **Features**: Date navigation, frequency selection

#### discrimination.html
- **Purpose**: WWV/WWVH discrimination (6 methods)
- **Data**: Per-method CSVs (BCD, timing tones, ticks, 440Hz, test signals)
- **Visualization**: 7-panel analysis with method labels

#### timing-dashboard-enhanced.html
- **Purpose**: Timing quality monitoring
- **Data**: time_snap events, timing quality annotations

#### gaps.html
- **Purpose**: Gap analysis and data completeness
- **Data**: Gap statistics from raw_buffer indexing

---

## Data Flow

### Production Flow

```
1. Analytics Service (Python)
   ↓ Writes CSV/JSON
   
2. File System
   ├─ /data/raw_buffer/{CHANNEL}/{YYYYMMDD}/
   ├─ /data/phase2/{CHANNEL}/
   └─ /data/status/
   
3. timestd-paths.js
   ↓ Provides file paths
   
4. monitoring-server-v3.js
   ↓ Reads files, formats JSON
   
5. Browser (HTML/JS)
   ↓ Fetches via API, renders charts
```

### Path Configuration Sync

**Python** (`src/hf_timestd/paths.py`):
```python
class TimeStdPaths:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
    
    def get_raw_buffer_dir(self, channel_name: str) -> Path:
        return self.data_root / 'raw_buffer' / channel_name_to_dir(channel_name)

    def get_phase2_dir(self, channel_name: str) -> Path:
        return self.data_root / 'phase2' / channel_name_to_dir(channel_name)
```

**JavaScript** (`web-ui/timestd-paths.js`):
```javascript
import { TimeStdPaths } from './timestd-paths.js';
const paths = new TimeStdPaths(dataRoot);
```

**Validation**: Run `scripts/validate-paths-sync.sh` to check consistency.

---

## Startup

### Beta Testing (Manual Start)

**Recorder** (Terminal 1):
```bash
python -m hf_timestd --help
```

**Web UI** (Terminal 2):
```bash
cd /path/to/hf-timestd/web-ui
npm start
```

### Direct Start with Custom Data Root

```bash
cd web-ui
node monitoring-server-v3.js /path/to/data
```

### Production (Post-Beta)

Systemd services in `systemd/` directory (not yet recommended).

---

## File Organization

### Active Production Files

```
web-ui/
├── monitoring-server-v3.js      # ⭐ Production API server
├── timestd-paths.js             # ⭐ Data location authority
│
├── index.html                   # Entry point (redirects to summary)
├── summary.html                 # ⭐ Main dashboard
├── carrier.html                 # ⭐ Carrier analysis
├── discrimination.html          # ⭐ WWV/WWVH discrimination
├── timing-dashboard-enhanced.html # ⭐ Timing quality
├── gaps.html                    # ⭐ Gap analysis
├── discrimination.js            # Chart logic for discrimination
│
├── utils/                       # Server utilities
│   ├── audit.js                 # Audit logging
│   ├── auth.js                  # Authentication
│   ├── config-sync.js           # Config synchronization
│   └── radiod.js                # Radiod status queries
│
├── middleware/                  # Express middleware
│   └── validation.js            # Input validation
│
├── scripts/                     # Admin scripts
│   └── create-admin.js          # User management
│
├── data/                        # Runtime data
│   ├── users.json               # User accounts
│   ├── jwt-secret.txt           # Auth secret
│   └── .gitignore
│
├── start-monitoring.sh          # Startup script
├── start-audio-proxy.sh         # Audio streaming
├── start-summary-test.sh        # Test mode startup
├── check-dashboard-status.sh    # Health check
│
├── package.json                 # Dependencies
├── pnpm-lock.yaml              # Lock file
└── README.md                    # Documentation
```

### Archived (Legacy)

```
web-ui/archive/legacy-pages/
├── simple-dashboard.html        # Superseded by summary.html
├── analysis.html                # Linked but useless
├── live-status.html             # Not used
├── channels.html                # Not used
├── quality-dashboard-addon.html # Component (unclear usage)
├── timing-analysis.html         # Superseded by timing-dashboard.html
└── simple-server.js.ARCHIVED-config-ui  # Old config UI
```

---

## Development Guidelines

### Adding New Dashboards

1. **Create HTML file** with standard header/nav
2. **Add API endpoint** in `monitoring-server-v3.js`
3. **Use timestd-paths.js** for all file locations
4. **No domain logic** in JavaScript
5. **Update** this documentation

### Modifying Data Locations

1. **Update Python** `PathConfig` first
2. **Update** `timestd-paths.js` to match
3. **Run** `scripts/validate-paths-sync.sh`
4. **Test** all dashboards
5. **Document** changes

### API Design

**Good** (presentation layer):
```javascript
app.get('/api/v1/channels/status', (req, res) => {
  const statusFile = paths.getCoreStatusFile();
  const data = JSON.parse(fs.readFileSync(statusFile));
  res.json(data); // Just read and serve
});
```

**Bad** (domain logic in web UI):
```javascript
app.get('/api/v1/channels/status', (req, res) => {
  // Don't calculate quality metrics here!
  // Don't interpret timing errors!
  // Don't make scientific decisions!
});
```

---

## Troubleshooting

### Server Won't Start

```bash
# Check if port 3000 is in use
lsof -i :3000

# Kill existing instance
pkill -f monitoring-server

# Check logs
tail -f web-ui/monitoring-server.log
```

### Data Not Showing

```bash
# Verify paths are correct
node -e "import('./timestd-paths.js').then(m => console.log(new m.TimeStdPaths('/tmp/timestd-test').getDataRoot()))"

# Check if files exist
ls -lh /path/to/data/raw_buffer/

# Check API directly
curl http://localhost:3000/api/v1/summary | jq
```

### Path Sync Issues

```bash
# Validate Python/JavaScript path consistency
cd /path/to/hf-timestd
./scripts/validate-paths-sync.sh
```

---

## Future Improvements

### Timing Dashboard
- User noted: "needs work"
- Current: Basic timing quality display
- Needed: Better visualization, historical trends

### Real-time Updates
- Current: Polling every 5 seconds
- Consider: WebSocket for true real-time

### Mobile Responsiveness
- Current: Desktop-optimized
- Consider: Mobile-friendly layouts

---

## Related Documentation

- **Python TimeStdPaths**: `src/hf_timestd/paths.py`
- **Path Validation**: `scripts/validate-paths-sync.sh`
- **API Reference**: `web-ui/PATHS_API_QUICK_REFERENCE.md`
- **Operational Summary**: `../OPERATIONAL_SUMMARY.md`

---

**Key Takeaway**: Web UI is a **dumb display layer**. All intelligence lives in the analytics service.
