# Unified Path API Documentation

## Overview

Both the Python analytics service and JavaScript web-UI reference the **same path API** to ensure consistency in where data is stored and retrieved. This eliminates confusion between the two systems.

---

## API Files

### Python: `src/signal_recorder/paths.py`
```python
from signal_recorder.paths import GRAPEPaths

paths = GRAPEPaths(data_root="/tmp/grape-test")
```

### JavaScript: `web-ui/grape-paths.js`
```javascript
import { GRAPEPaths } from './grape-paths.js';

const paths = new GRAPEPaths("/tmp/grape-test");
```

---

## Discrimination Method Directories

All five discrimination methods now have dedicated directories with matching API methods:

| Method | Python Method | JavaScript Method | Directory |
|--------|--------------|-------------------|-----------|
| **Tone Detections** (1000/1200 Hz) | `get_tone_detections_dir()` | `getToneDetectionsDir()` | `analytics/{CHANNEL}/tone_detections/` |
| **Tick Windows** (10-sec integration) | `get_tick_windows_dir()` | `getTickWindowsDir()` | `analytics/{CHANNEL}/tick_windows/` |
| **Station ID** (440 Hz) | `get_station_id_440hz_dir()` | `getStationId440HzDir()` | `analytics/{CHANNEL}/station_id_440hz/` |
| **BCD Discrimination** (100 Hz) | `get_bcd_discrimination_dir()` | `getBcdDiscriminationDir()` | `analytics/{CHANNEL}/bcd_discrimination/` |
| **Weighted Voting** (final) | `get_discrimination_dir()` | `getDiscriminationDir()` | `analytics/{CHANNEL}/discrimination/` |

---

## Example Usage

### Python (Analytics Service)

```python
from signal_recorder.paths import GRAPEPaths
from signal_recorder.discrimination_csv_writers import DiscriminationCSVWriters

paths = GRAPEPaths("/tmp/grape-test")

# Initialize CSV writers - automatically uses correct paths
csv_writers = DiscriminationCSVWriters(
    data_root="/tmp/grape-test",
    channel_name="WWV 10 MHz"
)

# Writers automatically use:
# - paths.get_tone_detections_dir("WWV 10 MHz")
# - paths.get_tick_windows_dir("WWV 10 MHz")
# - paths.get_station_id_440hz_dir("WWV 10 MHz")
# - paths.get_bcd_discrimination_dir("WWV 10 MHz")
# - paths.get_discrimination_dir("WWV 10 MHz")
```

### JavaScript (Web-UI)

```javascript
import { GRAPEPaths } from './grape-paths.js';

const paths = new GRAPEPaths("/tmp/grape-test");

// Load all 5 methods using unified API
const tonesPath = join(
    paths.getToneDetectionsDir("WWV 10 MHz"), 
    "WWV_10_MHz_tones_20251125.csv"
);

const ticksPath = join(
    paths.getTickWindowsDir("WWV 10 MHz"),
    "WWV_10_MHz_ticks_20251125.csv"
);

const id440Path = join(
    paths.getStationId440HzDir("WWV 10 MHz"),
    "WWV_10_MHz_440hz_20251125.csv"
);

const bcdPath = join(
    paths.getBcdDiscriminationDir("WWV 10 MHz"),
    "WWV_10_MHz_bcd_20251125.csv"
);

const discPath = join(
    paths.getDiscriminationDir("WWV 10 MHz"),
    "WWV_10_MHz_discrimination_20251125.csv"
);
```

---

## File Naming Convention

All CSV files follow the same pattern:

```
{CHANNEL_DIR}_{METHOD}_{YYYYMMDD}.csv
```

Where:
- **CHANNEL_DIR**: Channel name with spaces/periods replaced by underscores (`WWV_10_MHz`)
- **METHOD**: Short method identifier (`tones`, `ticks`, `440hz`, `bcd`, `discrimination`)
- **YYYYMMDD**: Date in UTC (e.g., `20251125`)

**Examples:**
```
WWV_10_MHz_tones_20251125.csv
WWV_10_MHz_ticks_20251125.csv
WWV_10_MHz_440hz_20251125.csv
WWV_10_MHz_bcd_20251125.csv
WWV_10_MHz_discrimination_20251125.csv
```

---

## Benefits of Unified API

✅ **No confusion** - Both systems reference the same path definitions  
✅ **Single source of truth** - Paths defined once, used everywhere  
✅ **Easy to update** - Change directory structure in one place  
✅ **Type-safe** - Methods enforce correct usage  
✅ **Self-documenting** - Method names describe what they return  

---

## Directory Structure

```
{data_root}/
├── analytics/
│   └── {CHANNEL}/
│       ├── tone_detections/          # 1000/1200 Hz timing tones
│       │   └── {CHANNEL}_tones_YYYYMMDD.csv
│       ├── tick_windows/             # 5ms tick analysis
│       │   └── {CHANNEL}_ticks_YYYYMMDD.csv
│       ├── station_id_440hz/         # 440 Hz station ID
│       │   └── {CHANNEL}_440hz_YYYYMMDD.csv
│       ├── bcd_discrimination/       # 100 Hz BCD time code
│       │   └── {CHANNEL}_bcd_YYYYMMDD.csv
│       ├── discrimination/           # Final weighted voting
│       │   └── {CHANNEL}_discrimination_YYYYMMDD.csv
│       ├── quality/
│       ├── decimated/
│       ├── digital_rf/
│       ├── logs/
│       └── status/
├── archives/
└── status/
```

---

## Migration Notes

If you have old combined CSV files at:
```
analytics/{CHANNEL}/discrimination/{CHANNEL}_discrimination_YYYYMMDD.csv
```

The analytics service will now write to **5 separate files** instead. The old combined format is no longer used.

---

## See Also

- `discrimination_csv_writers.py` - Python CSV writing implementation
- `monitoring-server-v3.js` - Web-UI API endpoints
- `WEB_UI_STRUCTURE.md` - Overall web-UI architecture
