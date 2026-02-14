# GRAPE Paths API - Migration Guide

## Problem Statement

Multiple scripts were hardcoding path construction, leading to:
- **Path mismatches** between producers and consumers
- **Duplicate logic** for channel name conversions
- **Brittle code** that breaks when directory structure changes
- **Debugging nightmares** when files can't be found

## Solution: Centralized Paths API

All code MUST use `src/signal_recorder/paths.py` for path generation.

## Quick Start

### Python Scripts

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.signal_recorder.paths import get_paths

def main():
    # Create paths instance
    paths = get_paths('/tmp/grape-test')
    
    # Get paths using API (NEVER construct manually!)
    archive_dir = paths.get_archive_dir('WWV 10 MHz')
    drf_dir = paths.get_digital_rf_dir('WWV 10 MHz')
    spec_path = paths.get_spectrogram_path('WWV 10 MHz', '20251115', 'carrier')
    state_file = paths.get_analytics_state_file('WWV 10 MHz')
    
    # Discover all channels
    channels = paths.discover_channels()
    
    # Use the paths...
    for npz_file in archive_dir.glob('*.npz'):
        process(npz_file)
```

### Python Services (with config file)

```python
from signal_recorder.paths import load_paths_from_config

# Load from config
paths = load_paths_from_config('config/grape-config.toml')

# Now use paths API
archive_dir = paths.get_archive_dir(channel_name)
```

### Node.js / JavaScript

```javascript
const toml = require('toml');
const fs = require('fs');
const path = require('path');

// Load config
const config = toml.parse(fs.readFileSync('config/grape-config.toml', 'utf8'));
const mode = config.recorder.mode;
const dataRoot = mode === 'production' 
  ? config.recorder.production_data_root
  : config.recorder.test_data_root;

// Helper functions
function channelNameToDir(channelName) {
  return channelName.replace(/ /g, '_');
}

function channelNameToKey(channelName) {
  const parts = channelName.split(' ');
  if (parts.length < 2) return channelName.replace(/ /g, '_').toLowerCase();
  return parts[0].toLowerCase() + parts[1]; // e.g., wwv10, chu3.33
}

// Use helpers
const channelDir = channelNameToDir('WWV 10 MHz');  // WWV_10_MHz
const archiveDir = path.join(dataRoot, 'archives', channelDir);
const spectrogramPath = path.join(
  dataRoot, 
  'spectrograms', 
  '20251115',
  `${channelDir}_20251115_carrier_spectrogram.png`
);
const stateKey = channelNameToKey('WWV 10 MHz');  // wwv10
const stateFile = path.join(dataRoot, 'state', `analytics-${stateKey}.json`);
```

## Directory Structure Reference

```
${data_root}/
├── archives/                    # Raw 16 kHz NPZ files
│   └── {CHANNEL}/              # e.g., WWV_10_MHz, CHU_3.33_MHz
│       └── {YYYYMMDDTHHMMSZ}_{FREQ}_iq.npz
│
├── analytics/                   # Per-channel analytics products
│   └── {CHANNEL}/
│       ├── digital_rf/         # Digital RF (10 Hz decimated)
│       │   └── {YYYYMMDD}/{CALL_GRID}/{RECEIVER}/{OBS}/{CHANNEL}/*.h5
│       ├── discrimination/     # WWV/WWVH discrimination
│       ├── quality/            # Quality metrics
│       ├── logs/               # Processing logs
│       └── status/             # Runtime status
│
├── spectrograms/                # Web UI spectrograms
│   └── {YYYYMMDD}/
│       └── {CHANNEL}_{YYYYMMDD}_{type}_spectrogram.png
│
├── state/                       # Service state
│   ├── analytics-{key}.json    # e.g., analytics-wwv10.json
│   └── core-recorder-status.json
│
└── status/                      # System status
    └── analytics-service-status.json
```

## Channel Name Conventions

**CRITICAL**: Three different formats, used consistently:

| Format | Example | Where Used |
|--------|---------|------------|
| **Display** | `WWV 10 MHz` | User-facing, function arguments |
| **Directory** | `WWV_10_MHz` | File system paths, archives |
| **Key** | `wwv10` | State files, analytics keys |

**Conversion Functions**:
- Python: `channel_name_to_dir()`, `channel_name_to_key()`
- JavaScript: `channelNameToDir()`, `channelNameToKey()`

**Special Cases**:
- `WWV 2.5 MHz` → Directory: `WWV_2.5_MHz`, Key: `wwv2.5`
- `CHU 3.33 MHz` → Directory: `CHU_3.33_MHz`, Key: `chu3.33`

## Migration Checklist

For each script/service that handles paths:

- [ ] Import `src/signal_recorder/paths.py`
- [ ] Create `GRAPEPaths` instance at startup
- [ ] Replace ALL hardcoded path construction
- [ ] Use `channel_name_to_dir()` / `channel_name_to_key()` instead of manual string manipulation
- [ ] Test with both WWV and CHU channels (decimal frequencies!)
- [ ] Update any path-related documentation

## Scripts That Need Migration

### High Priority (Data Producers)
- ✅ `scripts/analyze_timing.py` - **MIGRATED** (reference implementation)
- ⚠️ `scripts/regenerate_drf_from_npz.py` - Needs migration
- ⚠️ `scripts/generate_spectrograms_drf.py` - Needs migration  
- ⚠️ `scripts/generate_10hz_npz.py` - Needs migration
- ⚠️ `src/signal_recorder/analytics_service.py` - Needs migration

### Medium Priority (Data Consumers)
- ⚠️ `web-ui/monitoring-server.js` - Needs helper functions
- ⚠️ Any custom analysis scripts

### Low Priority
- Scripts that only read config (already using TOML correctly)

## Testing

After migration, verify:

```bash
# Test paths API directly
python3 -c "
from src.signal_recorder.paths import get_paths
paths = get_paths('/tmp/grape-test')
print('Archive:', paths.get_archive_dir('WWV 10 MHz'))
print('DRF:', paths.get_digital_rf_dir('WWV 10 MHz'))
print('Spec:', paths.get_spectrogram_path('WWV 10 MHz', '20251115', 'carrier'))
print('State:', paths.get_analytics_state_file('WWV 10 MHz'))
print('Channels:', paths.discover_channels())
"

# Test script migration
python3 scripts/analyze_timing.py --date 20251115 --channel "WWV 10 MHz" --data-root /tmp/grape-test

# Test decimal frequency channels
python3 scripts/analyze_timing.py --date 20251115 --channel "WWV 2.5 MHz" --data-root /tmp/grape-test
python3 scripts/analyze_timing.py --date 20251115 --channel "CHU 3.33 MHz" --data-root /tmp/grape-test
```

## Common Mistakes to Avoid

### ❌ DON'T DO THIS
```python
# Hardcoded path construction
channel_dir = channel_name.replace(' ', '_')
archive_path = f"{data_root}/archives/{channel_dir}"

# Manual state file naming
state_file = f"{data_root}/state/analytics-{channel_dir.lower()}.json"

# Inconsistent naming
spec_path = f"{data_root}/spectrograms/{date}/{channel_name.replace(' ', '_')}.png"
```

### ✅ DO THIS
```python
from signal_recorder.paths import get_paths

paths = get_paths(data_root)
archive_path = paths.get_archive_dir(channel_name)
state_file = paths.get_analytics_state_file(channel_name)
spec_path = paths.get_spectrogram_path(channel_name, date, 'carrier')
```

## Benefits

1. **Single Source of Truth**: All paths defined in one place
2. **Type Safety**: Path objects instead of strings
3. **Consistency**: Impossible to have path mismatches
4. **Maintainability**: Change structure once, everywhere updates
5. **Testability**: Easy to mock paths for unit tests
6. **Documentation**: Self-documenting code with method names

## Questions?

If you're unsure how to migrate a particular script, look at:
- `scripts/analyze_timing.py` - Reference implementation
- `src/signal_recorder/paths.py` - Full API documentation
- This guide - Common patterns
