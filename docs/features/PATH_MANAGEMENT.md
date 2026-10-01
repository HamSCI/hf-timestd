# Path Management System

## Overview

The GRAPE Signal Recorder uses a centralized path management system that ensures complete isolation between test and production environments. All file paths are governed by the `mode` setting in `grape-config.toml`.

## Mode-Based Path Configuration

### Configuration

In `config/grape-config.toml`:

```toml
[recorder]
# Mode determines which data root to use
mode = "test"  # or "production"

# Separate root directories for each mode
test_data_root = "/tmp/grape-test"
production_data_root = "/var/lib/signal-recorder"
```

### Path Resolution

The `PathResolver` class automatically selects paths based on the mode:

| Component | Test Mode | Production Mode |
|-----------|-----------|-----------------|
| **Data** | `/tmp/grape-test/data` | `/var/lib/signal-recorder/data` |
| **Analytics** | `/tmp/grape-test/analytics` | `/var/lib/signal-recorder/analytics` |
| **Upload Queue** | `/tmp/grape-test/upload` | `/var/lib/signal-recorder/upload` |
| **Status Files** | `/tmp/grape-test/status` | `/var/lib/signal-recorder/status` |
| **Logs** | `/tmp/grape-test/logs` | `/var/lib/signal-recorder/logs` |

## Directory Structure

### Test Mode (`mode = "test"`)

```
/tmp/grape-test/
├── data/
│   └── dYYYYMMDD/                    # Daily Digital RF data
│       └── CALLSIGN_GRID/
│           └── INSTRUMENT_ID/
│               └── CHANNEL/          # e.g., WWV_2.5_MHz
│                   ├── drf_properties.h5
│                   ├── metadata/
│                   └── rf_data_*.h5
├── analytics/
│   ├── quality/                       # Quality metrics CSVs
│   ├── timing/
│   │   └── wwv_timing.csv            # WWV tone detection log
│   ├── discontinuities/              # Gap/discontinuity logs
│   └── reports/                      # Daily summaries
├── upload/
│   └── queue.json                    # Upload queue state
├── status/
│   └── recording-stats.json          # Real-time status for web UI
└── logs/
    └── recorder_YYYYMMDD_HHMMSS.log  # Application logs
```

### Production Mode (`mode = "production"`)

```
/var/lib/signal-recorder/
├── data/                              # Same structure as test mode
├── analytics/
├── upload/
├── status/
└── logs/
```

## Component Integration

### All Components Use PathResolver

Every component that writes or reads files uses `PathResolver`:

| Component | Usage |
|-----------|-------|
| **grape_rtp_recorder.py** | Data output, status file |
| **grape_channel_recorder_v2.py** | Archive files, analytics |
| **uploader.py** | Upload queue management |
| **data_management.py** | Data cleanup operations |
| **cli.py** | Initializes PathResolver for all operations |
| **Web UI** | Reads status file, accesses data |

### How It Works

1. **Daemon Startup**: `grape_recorder.py` reads `mode` from config
2. **PathResolver Creation**: Sets `development_mode` based on mode
3. **Path Distribution**: All components receive PathResolver instance
4. **Automatic Routing**: Files automatically go to correct location

### Example: Recorder Initialization

```python
from signal_recorder.config_utils import PathResolver

# Load config
config = load_config('grape-config.toml')

# Determine mode
recorder_mode = config['recorder']['mode']  # "test" or "production"
development_mode = (recorder_mode == 'test')

# Create path resolver
path_resolver = PathResolver(config, development_mode=development_mode)

# Use throughout application
data_dir = path_resolver.get_data_dir()
# → /tmp/grape-test/data (if mode="test")
# → /var/lib/signal-recorder/data (if mode="production")
```

## Path Isolation Guarantees

### ✅ Complete Isolation

- **Test data** never mixes with **production data**
- Each mode has its own:
  - Data files
  - Analytics
  - Upload queues
  - Status files
  - Logs

### ✅ Safe Mode Switching

Changing `mode` in config:
1. Stops writing to old location
2. Starts writing to new location
3. Previous data remains intact
4. No data loss or corruption

### ✅ Parallel Operation Prevented

Cannot run test and production simultaneously:
- Status file location is mode-specific
- Upload queue is mode-specific
- Prevents conflicting operations

## Verification

### Audit Tool

```bash
python3 audit-paths.py config/grape-config.toml
```

This verifies:
- ✅ Test paths use test_data_root
- ✅ Production paths use production_data_root
- ✅ No path overlap between modes
- ✅ All components use PathResolver

### Mode Verification

```bash
./verify-mode-paths.sh
```

Shows actual resolved paths for current mode and simulates mode change.

## Migration and Cleanup

### Switching from Test to Production

1. Stop the recorder daemon
2. Edit `grape-config.toml`:
   ```toml
   mode = "production"
   ```
3. Ensure production directory exists and has correct permissions:
   ```bash
   sudo mkdir -p /var/lib/signal-recorder
   sudo chown mjh:mjh /var/lib/signal-recorder
   ```
4. Start the recorder daemon
5. Optionally clean up test data:
   ```bash
   rm -rf /tmp/grape-test
   ```

### Preserving Test Data

If you want to keep test data for comparison:
```bash
# Archive test data
tar czf grape-test-archive-$(date +%Y%m%d).tar.gz /tmp/grape-test

# Then clean up
rm -rf /tmp/grape-test
```

## Backward Compatibility

### Legacy Path Support

PathResolver still supports legacy config options:

```toml
[recorder]
archive_dir = "/custom/path"  # Overrides mode-based paths
quality_metrics_dir = "/custom/metrics"
```

### Fallback Defaults

If PathResolver is not used (backward compatibility), components fall back to:
- `/var/lib/signal-recorder/*` for production-like behavior
- Old config keys (`archive_dir`, etc.)

## Best Practices

### Development Workflow

1. **Use test mode** for development:
   ```toml
   mode = "test"
   ```
2. Test all features with test data
3. Verify upload with test queue
4. Clean up test data regularly

### Production Deployment

1. **Use production mode** for operational recording:
   ```toml
   mode = "production"
   ```
2. Set up log rotation for `/var/lib/signal-recorder/logs/`
3. Monitor disk space in `/var/lib/signal-recorder/`
4. Archive old data periodically

### Data Management

Use the data management CLI:

```bash
# View data summary
signal-recorder data summary

# Clean old test data (safe - only affects test_data_root)
signal-recorder data clean-data --days 7

# Clean old analytics
signal-recorder data clean-analytics --days 30

# Clean completed uploads
signal-recorder data clean-uploads --days 90
```

## Troubleshooting

### Wrong Directory Being Used

**Symptom**: Files appear in unexpected location

**Solution**: Check config mode setting:
```bash
grep '^mode = ' config/grape-config.toml
```

Restart daemon after changing mode.

### Permission Denied

**Symptom**: Cannot write to production directory

**Solution**: Ensure correct ownership:
```bash
sudo chown -R mjh:mjh /var/lib/signal-recorder
chmod 755 /var/lib/signal-recorder
```

### Path Overlap Detected

**Symptom**: Audit tool reports path overlap

**Solution**: Ensure test_data_root ≠ production_data_root in config

## Technical Details

### PathResolver Implementation

Location: `src/signal_recorder/config_utils.py`

Key methods:
- `get_data_dir()` - Data root for recordings
- `get_analytics_dir()` - Analytics and quality metrics
- `get_upload_state_dir()` - Upload queue management
- `get_status_dir()` - Runtime status files
- `get_log_dir()` - Application logs

### Mode Determination

```python
# In PathResolver.__init__()
self.development_mode = development_mode

# In get_data_dir()
if self.development_mode and 'test_data_root' in recorder_config:
    base_root = Path(recorder_config['test_data_root'])
    return base_root / 'data'
elif not self.development_mode and 'production_data_root' in recorder_config:
    base_root = Path(recorder_config['production_data_root'])
    return base_root / 'data'
```

This ensures:
- Mode is explicit (no guessing)
- Audit tools can test both modes
- Clear separation of concerns

## Summary

✅ **Centralized**: Single source of truth (PathResolver)  
✅ **Mode-Aware**: All paths governed by mode setting  
✅ **Isolated**: Test and production completely separated  
✅ **Verified**: Automated audit and verification tools  
✅ **Flexible**: Supports custom paths when needed  
✅ **Safe**: Prevents accidental data mixing  

The path management system ensures reliable, predictable file organization across all operating modes.
