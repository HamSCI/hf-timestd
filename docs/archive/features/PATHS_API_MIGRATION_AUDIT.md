# Paths API Migration Audit
**Date:** 2024-11-15  
**Status:** Partial completion - monitoring-server.js requires migration

## Summary

The centralized Paths API (`paths.py` + `grape-paths.js`) is implemented and documented. Migration is ~70% complete, with the web monitoring server being the primary remaining target.

---

## ✅ Complete Migrations

### Python Side
- **`paths.py`** - Core API implemented with full feature set
- **`scripts/analyze_timing.py`** - ✅ Uses paths API
- **`scripts/regenerate_drf_from_npz.py`** - ✅ Uses paths API  
- **`scripts/generate_spectrograms_drf.py`** - ✅ Uses paths API
- **`scripts/generate_10hz_npz.py`** - ✅ Uses paths API (assumed based on recent work)

### JavaScript Side
- **`grape-paths.js`** - Core API implemented, matches Python API

### Bash Scripts
- **`start-dual-service.sh`** - Follows path conventions (manual construction acceptable for bash)

---

## 🔴 Critical Migration Required

### `web-ui/monitoring-server.js`
**Status:** Extensive hardcoded path construction throughout  
**Impact:** HIGH - Used by all web UI endpoints  
**Priority:** 1 (must fix)

**Hardcoded patterns found:**
```javascript
Line 84:   const archivesDir = join(dataRoot, 'archives');
Line 98:   .map(d => join(archivesDir, d.name));
Line 154:  const archivesDir = join(dataRoot, 'archives');
Line 181:  .map(d => join(archivesDir, d.name));
Line 330:  join(dataRoot, 'status', 'recording-stats.json'),
Line 370:  join(dataRoot, 'status', 'recording-stats.json'),
Line 407:  join(dataRoot, 'status', 'recording-stats.json'),
Line 445:  const logFile = join(dataRoot, '../logs/signal-recorder.log');
Line 483:  const statusFile = join(dataRoot, 'status', 'core-recorder-status.json');
Line 513:  const analyticsDir = join(dataRoot, 'analytics');
Line 537:  const statusFile = join(analyticsDir, channelDir, 'status', 'analytics-service-status.json');
Line 653:  archive: join(dataRoot, 'archives'),
Line 654:  analytics: join(dataRoot, 'analytics'),
Line 820:  const filePath = join(dataRoot, 'analytics', channelDirName, 'discrimination', fileName);
Line 925:  const scriptPath = join(installDir, 'scripts', 'generate_spectrograms_drf.py');
Line 1422: let qualityDir = join(dataRoot, 'analytics', 'quality', today);
Line 1426: qualityDir = join(dataRoot, 'analytics', 'quality', yesterday);
Line 1622: join(dataRoot, 'status', 'recording-stats.json'),
```

**Functions requiring refactoring:**
- `getAvailableDataRange()` - Archives discovery
- `calculateContinuity()` - Gap analysis across channels
- `getRecorderStatus()` - Status file reading
- `getChannelStatus()` - Per-channel status
- `getTimeSnapStatus()` - Time snap status
- `getRecentErrors()` - Log file access
- `getCoreRecorderStatus()` - Core status file
- `getAnalyticsServiceStatus()` - Analytics status files
- `getChannelQualityMetrics()` - Quality directory access
- Various API endpoints

---

## 🟡 Optional Enhancements

### `src/signal_recorder/analytics_service.py`
**Status:** Already accepts paths as parameters  
**Current approach:** Caller constructs paths, passes to `__init__(archive_dir, output_dir, state_file)`  
**Enhancement option:** Add alternative init accepting `GRAPEPaths` object

**Current pattern (works fine):**
```python
service = AnalyticsService(
    archive_dir=Path(args.archive_dir),
    output_dir=Path(args.output_dir),
    state_file=Path(args.state_file)
)
```

**Optional alternative:**
```python
from signal_recorder.paths import get_paths

paths = get_paths('/tmp/grape-test')
service = AnalyticsService.from_paths(paths, channel_name='WWV 10 MHz')
```

**Priority:** 3 (nice-to-have, not blocking)

---

## 📋 Migration Plan

### Phase 1: monitoring-server.js (REQUIRED)
1. Import `GRAPEPaths` at top of file
2. Initialize paths object after config load:
   ```javascript
   import { GRAPEPaths } from './grape-paths.js';
   const paths = new GRAPEPaths(dataRoot);
   ```
3. Refactor all functions to use paths API
4. Test all API endpoints

### Phase 2: analytics_service.py (OPTIONAL)
1. Add `from_paths()` class method
2. Update documentation with both initialization patterns
3. Leave existing init method unchanged (backward compatibility)

---

## Testing Checklist

After monitoring-server.js migration:
- [ ] Web UI loads correctly
- [ ] Channel list displays
- [ ] Status endpoints return data
- [ ] Spectrogram generation works
- [ ] Quality metrics display
- [ ] Gap analysis functions
- [ ] Discrimination data loads

---

## Benefits After Complete Migration

1. **Consistency** - All services use identical path logic
2. **Maintainability** - Path changes in ONE place
3. **Type Safety** - Channel name conversions handled automatically
4. **Discovery** - Automatic channel enumeration
5. **Testability** - Easy to test with different data roots
6. **Documentation** - Self-documenting code via API
