# GRAPE Directory Structure - Canonical Reference

**Author:** Michael James Hauan (AC0G)  
**Status:** CANONICAL - This is the single source of truth for all file paths  
**Last Updated:** 2025-12-09  
**Enforcement:** ALL code MUST use `src/grape_recorder/paths.py` GRAPEPaths API

---

## Critical Rules

1. **NO direct path construction** - Always use `GRAPEPaths` API
2. **NO ad-hoc naming** - Follow exact patterns below
3. **NO time-range suffixes** on daily files - One file per channel per day per method
4. **Mode-aware** - Paths differ between test and production modes

---

## Installation

Use the unified installer to set up directories automatically:

```bash
# Test mode
./scripts/install.sh --mode test

# Production mode (FHS-compliant)
sudo ./scripts/install.sh --mode production --user $USER
```

---

## Directory Layout by Mode

### Test Mode

```
/tmp/timestd-test/                    # GRAPE_DATA_ROOT
├── raw_archive/                    # Phase 1: Immutable DRF archive (20 kHz)
├── raw_buffer/                     # Phase 1: Real-time minute buffers
├── phase2/                         # Phase 2: Analytical outputs
├── products/                       # Phase 3: Derived products (decimated, spectrograms)
├── state/                          # Service state
├── status/                         # Health status
└── logs/                           # Application logs

config/timestd-config.toml            # Configuration
config/environment                  # Environment variables
```

### Production Mode (FHS-Compliant)

```
/var/lib/timestd/            # GRAPE_DATA_ROOT - Application data
├── raw_archive/                    # Phase 1: Immutable DRF archive (20 kHz)
├── raw_buffer/                     # Phase 1: Real-time minute buffers
├── phase2/                         # Phase 2: Analytical outputs (D_clock, discrimination)
├── products/                       # Phase 3: Derived products (decimated, spectrograms)
├── state/                          # Service state
└── status/                         # Health status

/var/log/grape-recorder/            # GRAPE_LOG_DIR - Application logs
├── recorder.log
├── analytics.log
└── daily-upload.log

/etc/hf-timestd/                # Configuration
├── timestd-config.toml
└── environment

/opt/grape-recorder/                # Application binaries
├── venv/                           # Python virtual environment
└── web-ui/                         # Node.js web interface
```

---

## Configuration

The mode is determined by the environment file or `timestd-config.toml`:

```toml
[recorder]
mode = "test"                              # or "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"
```

Environment file (`/etc/hf-timestd/environment` or `config/environment`):

```bash
GRAPE_MODE=production
GRAPE_DATA_ROOT=/var/lib/timestd
GRAPE_LOG_DIR=/var/log/grape-recorder
GRAPE_CONFIG=/etc/hf-timestd/timestd-config.toml
```

---

## Complete Directory Tree (Three-Phase Architecture)

```
${data_root}/
│
├── raw_archive/                           # PHASE 1: Immutable Raw Archive (DRF)
│   └── {CHANNEL}/                         # e.g., WWV_10_MHz, CHU_7.85_MHz
│       └── {YYYYMMDD}/                    # Daily subdirectories
│           ├── {YYYY-MM-DDTHH}/           # Hourly HDF5 files
│           │   └── rf@{timestamp}.h5      # 20 kHz complex64 IQ
│           ├── drf_properties.h5          # Digital RF properties
│           └── metadata/                  # Per-minute provenance
│
├── raw_buffer/                            # PHASE 1: Real-time minute buffers
│   └── {CHANNEL}/                         # Binary IQ + JSON metadata
│       ├── {minute}.bin                   # 1,200,000 complex64 samples
│       └── {minute}.json                  # RTP timestamp, gaps, timing
│
├── phase2/                                # PHASE 2: Analytical Engine Outputs
│   └── {CHANNEL}/                         # e.g., WWV_10_MHz
│       │
│       ├── clock_offset/                  # D_clock time series
│       │   └── {YYYYMMDD}.csv             # minute, d_clock_ms, uncertainty_ms, station
│       │
│       ├── carrier_analysis/              # Amplitude, phase, Doppler
│       │   └── {YYYYMMDD}.csv             # Carrier metrics
│       │
│       ├── discrimination/                # WWV/WWVH voting results
│       │   └── {YYYYMMDD}.csv             # Per-minute discrimination
│       │
│       ├── bcd_correlation/               # 100 Hz BCD time code
│       │   └── {YYYYMMDD}.csv             # Dual-peak detection
│       │
│       ├── tone_detections/               # 1000/1200 Hz timing tones
│       │   └── {YYYYMMDD}.csv             # SNR, timing
│       │
│       ├── ground_truth/                  # 440/500/600 Hz station ID
│       │   └── {YYYYMMDD}.csv             # Exclusive minute detections
│       │
│       ├── state/                         # Processing state
│       │   ├── convergence_state.json     # Kalman filter state
│       │   └── channel-status.json        # Runtime status
│       │
│       └── status/                        # Analytics status
│           └── analytics-service-status.json
│
├── products/                              # PHASE 3: Derived Products
│   └── {CHANNEL}/
│       │
│       ├── decimated/                     # 10 Hz carrier data
│       │   └── {YYYYMMDD}.bin             # Daily binary file
│       │
│       └── spectrograms/                  # Generated images
│           └── {YYYYMMDD}_spectrogram.png # Daily spectrogram with power + solar zenith
│
├── state/                                 # Global state persistence
│   ├── broadcast_calibration.json         # Multi-broadcast fusion calibration
│   └── core-recorder-status.json          # Core recorder state
│
├── status/                                # System-wide status
│   └── gpsdo_status.json                  # GPSDO monitor state
│
└── logs/                                  # Application logs
    ├── core-recorder.log
    └── analytics.log
```

---

## Channel Naming Conventions

### Human-Readable Format
Used in: Configuration, UI, function parameters
```
"WWV 10 MHz"
"WWV 5 MHz"
"WWV 2.5 MHz"
"CHU 3.33 MHz"
"WWVH 15 MHz"
```

### Directory Format
Used in: File paths, directory names
```
WWV_10_MHz
WWV_5_MHz
WWV_2_5_MHz
CHU_3_33_MHz
WWVH_15_MHz
```

### Key Format
Used in: State files, internal keys
```
wwv10
wwv5
wwv2.5
chu3.33
wwvh15
```

**Conversion Functions (in paths.py):**
- `channel_name_to_dir(name: str) -> str` - "WWV 10 MHz" → "WWV_10_MHz"
- `channel_name_to_key(name: str) -> str` - "WWV 10 MHz" → "wwv10"
- `channel_dir_to_name(dir: str) -> str` - "WWV_10_MHz" → "WWV 10 MHz"

---

## File Naming Patterns

### Archives
```
YYYYMMDDTHHMMSSZ_{FREQUENCY}_iq.npz
20251119T120000Z_5000000_iq.npz          # Correct
20251119T120000Z_5MHz_iq.npz             # WRONG - use Hz
```

### Analytics - Daily CSVs (NO time range suffixes!)
```
{CHANNEL}_{METHOD}_YYYYMMDD.csv

WWV_5_MHz_tones_20251119.csv             # Correct
WWV_5_MHz_ticks_20251119.csv             # Correct
WWV_5_MHz_440hz_20251119.csv             # Correct
WWV_5_MHz_bcd_20251119.csv               # Correct
WWV_5_MHz_discrimination_20251119.csv    # Correct

WWV_5_MHz_discrimination_20251119_12-15.csv   # WRONG - no time range
WWV_5_MHz_20251119_discrimination.csv         # WRONG - method comes before date
```

### Spectrograms
```
{CHANNEL}_YYYYMMDD_{TYPE}_spectrogram.png

WWV_5_MHz_20251119_decimated_spectrogram.png
```

### Logs
```
analytics_YYYYMMDD.log
```

---

## GRAPEPaths API Reference

All code MUST use this API. Located in `src/grape_recorder/paths.py`.

### Initialization

```python
from grape_recorder.paths import GRAPEPaths, load_paths_from_config

# From config file (recommended - respects test/production mode)
paths = load_paths_from_config('/path/to/timestd-config.toml')

# Direct initialization (testing only)
paths = GRAPEPaths('/tmp/timestd-test')
```

### Archive Methods

```python
paths.get_archive_dir(channel_name: str) -> Path
    # Returns: {data_root}/archives/{CHANNEL}/
    # Example: /tmp/timestd-test/archives/WWV_5_MHz/

paths.get_archive_path(channel_name: str, timestamp: float, freq_hz: int) -> Path
    # Returns: {archive_dir}/{TIMESTAMP}_{FREQ}_iq.npz
    # Example: /tmp/timestd-test/archives/WWV_5_MHz/20251119T120000Z_5000000_iq.npz
```

### Analytics Methods - Directories

```python
paths.get_analytics_dir(channel_name: str) -> Path
    # Returns: {data_root}/analytics/{CHANNEL}/

paths.get_decimated_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/decimated/

paths.get_digital_rf_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/digital_rf/

paths.get_tone_detections_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/tone_detections/

paths.get_tick_windows_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/tick_windows/

paths.get_station_id_440hz_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/station_id_440hz/

paths.get_bcd_discrimination_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/bcd_discrimination/

paths.get_discrimination_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/discrimination/

paths.get_quality_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/quality/

paths.get_analytics_logs_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/logs/

paths.get_analytics_status_dir(channel_name: str) -> Path
    # Returns: {analytics}/{CHANNEL}/status/
```

### Analytics Methods - Files

```python
paths.get_discrimination_csv(channel_name: str, date: str) -> Path
    # Returns: {discrimination_dir}/{CHANNEL}_discrimination_{date}.csv
    # date format: YYYYMMDD

paths.get_quality_csv(channel_name: str, date: str) -> Path
    # Returns: {quality_dir}/{CHANNEL}_quality_{date}.csv
```

### Other Methods

```python
paths.get_spectrograms_dir() -> Path
paths.get_spectrograms_date_dir(date: str) -> Path
paths.get_spectrogram_path(channel_name: str, date: str, spec_type: str) -> Path
paths.get_state_dir() -> Path
paths.get_state_file(channel_name: str) -> Path
paths.get_status_dir() -> Path
```

---

## Enforcement

### For Python Code

```python
# ✅ CORRECT - Using GRAPEPaths API
from grape_recorder.paths import load_paths_from_config

paths = load_paths_from_config()
csv_file = paths.get_discrimination_dir(channel_name) / f"{channel_dir}_discrimination_{date}.csv"
```

```python
# ❌ WRONG - Direct path construction
output_dir = Path(data_root) / 'analytics' / channel_dir / 'discrimination'
csv_file = output_dir / f"{channel_dir}_discrimination_{date}_{start_hour:02d}-{end_hour:02d}.csv"
```

### For JavaScript Code

Use `web-ui/grape-paths.js`:

```javascript
const paths = new GRAPEPaths(dataRoot);
const csvPath = paths.getDiscriminationCSV(channelName, date);
```

---

## Migration Checklist

- [ ] All Python producers use GRAPEPaths API
- [ ] All Python consumers use GRAPEPaths API  
- [ ] All JavaScript uses grape-paths.js
- [ ] No time-range suffixes on any files
- [ ] No ad-hoc directory creation
- [ ] Test mode produces correct paths
- [ ] Production mode produces correct paths
- [ ] All CSVs follow exact column ordering in this doc
- [ ] All NPZ files follow exact field naming in this doc

---

## See Also

- `docs/API_REFERENCE.md` - Function signatures and data models
- `src/grape_recorder/paths.py` - Implementation
- `web-ui/grape-paths.js` - JavaScript implementation
- `config/timestd-config.toml` - Configuration
