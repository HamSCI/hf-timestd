# TimeStdPaths API Quick Reference

**⚠️ CRITICAL**: When adding/changing analytics paths, update BOTH implementations.

---

## Quick Checklist

Adding a new path? Follow these steps:

- [ ] 1. Update `src/hf_timestd/paths.py` (Python)
- [ ] 2. Update `web-ui/timestd-paths.js` (JavaScript)
- [ ] 3. Run `./scripts/validate-paths-sync.sh`
- [ ] 4. Use new path in analytics code
- [ ] 5. Use new path in web-ui code
- [ ] 6. Commit both files together

---

## Common Paths

### Phase 1 (raw_buffer)
```python
# Python
paths.get_raw_buffer_dir(channel_name)  # → {data_root}/raw_buffer/{CHANNEL}/
```
```javascript
// JavaScript
paths.getRawBufferDir(channelName)     // → {dataRoot}/raw_buffer/{CHANNEL}/
```

### Phase 2 Products
```python
# Python
paths.get_phase2_dir(channel_name)              # Base directory
paths.get_discrimination_dir(channel_name)      # Final weighted voting CSVs
paths.get_bcd_discrimination_dir(channel_name)  # BCD 100 Hz correlation CSVs
paths.get_tone_detections_dir(channel_name)     # 1000/1200 Hz tone CSVs
paths.get_tick_windows_dir(channel_name)        # 5ms tick analysis CSVs
paths.get_station_id_440hz_dir(channel_name)    # 440 Hz station ID CSVs
paths.get_quality_dir(channel_name)             # Quality CSVs
```
```javascript
// JavaScript
paths.getPhase2Dir(channelName)
paths.getDiscriminationDir(channelName)
paths.getBcdDiscriminationDir(channelName)
paths.getToneDetectionsDir(channelName)
paths.getTickWindowsDir(channelName)
paths.getStationId440hzDir(channelName)
paths.getQualityDir(channelName)
```

### State & Status
```python
# Python
paths.get_state_dir()
paths.get_analytics_state_file(channel_name)  # time_snap, etc.
paths.get_status_dir()
```
```javascript
// JavaScript
paths.getStateDir()
paths.getAnalyticsStateFile(channelName)
paths.getStatusDir()
```

---

## Usage Examples

### Python (Analytics Service)
```python
from hf_timestd.paths import TimeStdPaths

paths = TimeStdPaths('/tmp/timestd-test')
output_dir = paths.get_phase2_dir('WWV 10 MHz')
output_file = output_dir / f"{timestamp}_phase2.csv"
```

### JavaScript (Web-UI)
```javascript
import { TimeStdPaths } from './timestd-paths.js';

const paths = new TimeStdPaths(dataRoot);
const phase2Dir = paths.getPhase2Dir('WWV 10 MHz');
const files = fs.readdirSync(phase2Dir);
```

---

## ❌ Anti-Patterns (DO NOT DO THIS)

### Hardcoded Paths
```javascript
// ❌ BAD - Will break when paths change
const dir = join(dataRoot, 'phase2', 'WWV_10_MHz');

// ✅ GOOD - Uses centralized API
const dir = paths.getPhase2Dir('WWV 10 MHz');
```

### Inconsistent Naming
```python
# ❌ BAD - Different format than API
channel_dir = channel_name.replace(' ', '-').lower()

# ✅ GOOD - Use API helper
from hf_timestd.paths import channel_name_to_dir
channel_dir = channel_name_to_dir(channel_name)
```

---

## Validation

```bash
# Run after ANY path changes
./scripts/validate-paths-sync.sh

# Expected output:
# ✅ SUCCESS: Python and JavaScript paths are identical!
```

---

## Full Documentation

See `WEB_UI_ARCHITECTURE.md` for:
- Complete protocol and rules
- Step-by-step examples
- Migration guide
- Troubleshooting

---

## Emergency Fix: Web-UI Out of Sync

If web-ui can't find data:

1. Check which monitoring server is running:
   ```bash
   ps aux | grep monitoring-server
   ```

2. If it's the old one, restart with v3:
   ```bash
   pkill -f monitoring-server
   cd web-ui
   node monitoring-server-v3.js
   ```

3. Verify paths match analytics:
   ```bash
   ./scripts/validate-paths-sync.sh
   ```

4. Check data actually exists:
   ```bash
   tree -L 4 /tmp/timestd-test/phase2/
   ```
