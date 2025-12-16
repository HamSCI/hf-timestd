# Monitoring Server Architecture Model
**Component:** `web-ui/monitoring-server.js`  
**Purpose:** RESTful API for GRAPE web UI monitoring dashboard  
**Design Date:** 2024-11-15

---

## Design Principles

1. **Single Paths Instance** - Initialize `GRAPEPaths` once at startup, use throughout
2. **Explicit Parameters** - Pass paths object to functions requiring it (no globals)
3. **Channel Name Consistency** - Always use human-readable format ("WWV 10 MHz")
4. **Automatic Discovery** - Use `paths.discoverChannels()` for channel enumeration
5. **Graceful Degradation** - Return meaningful errors when data unavailable
6. **Separation of Concerns** - Data access functions separate from route handlers

---

## Initialization Pattern

### Startup Sequence
```javascript
import { GRAPEPaths, loadPathsFromConfig } from './grape-paths.js';

// 1. Load config (existing pattern)
const configPath = process.env.GRAPE_CONFIG || join(installDir, 'config/grape-config.toml');
const config = toml.parse(fs.readFileSync(configPath, 'utf8'));

// 2. Determine data root (existing pattern)
const mode = config.recorder?.mode || 'test';
const dataRoot = mode === 'production' 
  ? config.recorder?.production_data_root || '/var/lib/signal-recorder'
  : config.recorder?.test_data_root || '/tmp/grape-test';

// 3. Create paths instance (NEW)
const paths = new GRAPEPaths(dataRoot);

console.log('📁 Data root:', dataRoot);
console.log('📂 Archives:', paths.getArchiveDir('WWV 10 MHz'));
console.log('📂 Analytics:', paths.getAnalyticsDir('WWV 10 MHz'));
```

---

## Function Design Patterns

### Pattern 1: Data Access Functions
**Purpose:** Retrieve data from filesystem  
**Signature:** Accept `paths` parameter, return data or error object

```javascript
/**
 * Get core recorder status
 * @param {GRAPEPaths} paths - Paths instance
 * @returns {Object} Status object or error
 */
async function getCoreRecorderStatus(paths) {
  try {
    const statusFile = paths.getCoreStatusFile();
    
    if (!fs.existsSync(statusFile)) {
      return { running: false, error: 'Status file not found' };
    }
    
    const content = fs.readFileSync(statusFile, 'utf8');
    const status = JSON.parse(content);
    
    // Calculate age
    const age = Date.now() / 1000 - status.timestamp;
    
    return {
      running: age < 30,
      channels: status.channels_active || [],
      packets_total: status.packets_received || 0,
      age_seconds: age,
      raw: status
    };
  } catch (err) {
    return { running: false, error: err.message };
  }
}
```

### Pattern 2: Multi-Channel Operations
**Purpose:** Aggregate data across all channels  
**Pattern:** Use `paths.discoverChannels()` for enumeration

```javascript
/**
 * Get analytics status for all channels
 * @param {GRAPEPaths} paths - Paths instance
 * @returns {Object} Per-channel status map
 */
async function getAllChannelStatuses(paths) {
  const channels = paths.discoverChannels();
  const statuses = {};
  
  for (const channelName of channels) {
    const statusDir = paths.getAnalyticsStatusDir(channelName);
    const statusFile = join(statusDir, 'analytics-service-status.json');
    
    if (fs.existsSync(statusFile)) {
      try {
        const content = fs.readFileSync(statusFile, 'utf8');
        statuses[channelName] = JSON.parse(content);
      } catch (err) {
        statuses[channelName] = { error: err.message };
      }
    } else {
      statuses[channelName] = { error: 'Status file not found' };
    }
  }
  
  return statuses;
}
```

### Pattern 3: Date-Based Access
**Purpose:** Access time-series data (spectrograms, quality metrics)  
**Pattern:** Use paths API date-specific methods

```javascript
/**
 * Get available spectrograms for a date
 * @param {GRAPEPaths} paths - Paths instance
 * @param {string} date - Date in YYYYMMDD format
 * @returns {Array} List of available spectrograms
 */
async function getSpectrogramsForDate(paths, date) {
  const dateDir = paths.getSpectrogramsDateDir(date);
  
  if (!fs.existsSync(dateDir)) {
    return [];
  }
  
  const files = fs.readdirSync(dateDir)
    .filter(f => f.endsWith('_spectrogram.png'))
    .map(f => {
      // Parse filename: WWV_10_MHz_20241115_carrier_spectrogram.png
      const parts = f.replace('_spectrogram.png', '').split('_');
      const type = parts[parts.length - 1];
      const channelParts = parts.slice(0, -2);
      const channelName = channelParts.join('_').replace(/_/g, ' ');
      
      return {
        channel: channelName,
        date: date,
        type: type,
        filename: f,
        path: join(dateDir, f)
      };
    });
  
  return files;
}
```

### Pattern 4: Route Handlers
**Purpose:** Express route handlers  
**Pattern:** Use closure to access paths, call data functions

```javascript
/**
 * API route setup
 */
function setupRoutes(app, paths, config) {
  // Status endpoint
  app.get('/api/v1/system/status', async (req, res) => {
    try {
      const coreStatus = await getCoreRecorderStatus(paths);
      const channelStatuses = await getAllChannelStatuses(paths);
      
      res.json({
        core: coreStatus,
        channels: channelStatuses,
        timestamp: Date.now() / 1000
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });
  
  // Spectrograms endpoint
  app.get('/api/spectrograms/:date', async (req, res) => {
    try {
      const spectrograms = await getSpectrogramsForDate(paths, req.params.date);
      res.json({ spectrograms });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });
  
  // Per-channel quality endpoint
  app.get('/api/quality/:channel', async (req, res) => {
    try {
      const channelName = req.params.channel; // "WWV 10 MHz"
      const qualityDir = paths.getQualityDir(channelName);
      
      // ... read quality metrics ...
      
      res.json({ channel: channelName, metrics: {...} });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });
}

// Main
const paths = new GRAPEPaths(dataRoot);
setupRoutes(app, paths, config);
```

---

## Channel Naming Standards

### Always Use Human-Readable Format
```javascript
// ✅ CORRECT
const channelName = "WWV 10 MHz";
const archiveDir = paths.getArchiveDir(channelName);
// → /tmp/grape-test/archives/WWV_10_MHz/

// ❌ WRONG - Don't manually construct directory names
const channelDir = channelName.replace(/ /g, '_');
const archiveDir = join(dataRoot, 'archives', channelDir);
```

### Channel Discovery
```javascript
// ✅ CORRECT - Use paths API
const channels = paths.discoverChannels();
// → ["CHU 3.33 MHz", "CHU 7.85 MHz", "WWV 10 MHz", ...]

// ❌ WRONG - Manual directory scanning
const archivesDir = join(dataRoot, 'archives');
const channels = fs.readdirSync(archivesDir)
  .map(d => d.replace(/_/g, ' '));
```

### Channel Name in URLs
```javascript
// API route with channel parameter
app.get('/api/quality/:channel', async (req, res) => {
  // URL: /api/quality/WWV%2010%20MHz
  const channelName = decodeURIComponent(req.params.channel);
  // → "WWV 10 MHz"
  
  const qualityDir = paths.getQualityDir(channelName);
  // ...
});
```

---

## Error Handling Standards

### Consistent Error Objects
```javascript
// Data access functions return structured errors
async function getDataOrError(paths) {
  try {
    // ... attempt operation ...
    return { success: true, data: result };
  } catch (err) {
    return { 
      success: false, 
      error: err.message,
      code: 'FILE_NOT_FOUND' // or other semantic code
    };
  }
}

// Route handlers convert to HTTP status
app.get('/api/data', async (req, res) => {
  const result = await getDataOrError(paths);
  
  if (!result.success) {
    return res.status(404).json({ error: result.error });
  }
  
  res.json(result.data);
});
```

### Graceful Degradation
```javascript
// Don't crash on missing data - return partial results
async function getSystemStatus(paths) {
  const status = {
    timestamp: Date.now() / 1000,
    core: { available: false },
    analytics: { available: false },
    channels: []
  };
  
  // Try to get core status (don't fail if unavailable)
  try {
    status.core = await getCoreRecorderStatus(paths);
    status.core.available = true;
  } catch (err) {
    status.core.error = err.message;
  }
  
  // Try to get analytics status (don't fail if unavailable)
  try {
    const channelStatuses = await getAllChannelStatuses(paths);
    status.analytics = { available: true, channels: channelStatuses };
  } catch (err) {
    status.analytics.error = err.message;
  }
  
  return status;
}
```

---

## File Organization

### Module Structure
```
monitoring-server.js
├── Imports & Initialization
│   ├── Import GRAPEPaths
│   ├── Load config
│   ├── Initialize paths
│   └── Create Express app
│
├── Data Access Functions
│   ├── getCoreRecorderStatus(paths)
│   ├── getAllChannelStatuses(paths)
│   ├── getSpectrogramsForDate(paths, date)
│   ├── getQualityMetrics(paths, channel, date)
│   ├── getDiscriminationData(paths, channel, date)
│   └── ... (one function per data type)
│
├── Route Setup Function
│   └── setupRoutes(app, paths, config)
│       ├── System status routes
│       ├── Channel routes
│       ├── Spectrogram routes
│       ├── Quality routes
│       └── Utility routes
│
└── Server Start
    └── app.listen(PORT)
```

---

## Migration Checklist

### For Each Existing Function:

1. **Identify path construction patterns**
   ```javascript
   // BEFORE
   const statusFile = join(dataRoot, 'status', 'core-recorder-status.json');
   
   // AFTER
   const statusFile = paths.getCoreStatusFile();
   ```

2. **Add paths parameter**
   ```javascript
   // BEFORE
   async function getStatus() { ... }
   
   // AFTER
   async function getStatus(paths) { ... }
   ```

3. **Update callers**
   ```javascript
   // BEFORE
   const status = await getStatus();
   
   // AFTER
   const status = await getStatus(paths);
   ```

4. **Use channel discovery**
   ```javascript
   // BEFORE
   const channelDirs = fs.readdirSync(join(dataRoot, 'archives'));
   
   // AFTER
   const channels = paths.discoverChannels();
   ```

5. **Test with multiple channels**
   - Verify WWV channels (with dots: 2.5, 3.33, etc.)
   - Verify CHU channels
   - Check edge cases (empty directories, missing files)

---

## Testing Strategy

### Unit Test Pattern (if/when implemented)
```javascript
describe('Data Access Functions', () => {
  let paths;
  
  beforeEach(() => {
    paths = new GRAPEPaths('/tmp/test-grape-data');
  });
  
  test('getCoreRecorderStatus returns status when file exists', async () => {
    // Setup test data
    const statusFile = paths.getCoreStatusFile();
    // ... write test file ...
    
    const status = await getCoreRecorderStatus(paths);
    expect(status.running).toBe(true);
  });
});
```

### Manual Testing Checklist
- [ ] All API endpoints return valid JSON
- [ ] Channel list displays correctly
- [ ] Spectrograms load from correct paths
- [ ] Quality metrics accessible per channel
- [ ] Status files read from correct locations
- [ ] Missing data returns graceful errors (not 500)
- [ ] Multi-channel operations work
- [ ] Date-based queries work

---

## Benefits of This Model

1. **Testability** - Functions accept paths, can test with mock data
2. **Consistency** - All code follows same patterns
3. **Maintainability** - Path logic in ONE place
4. **Type Safety** - Channel name conversions automatic
5. **Discoverability** - Clear function signatures
6. **Flexibility** - Easy to add new data sources
7. **Debugging** - Clear data flow (paths → function → route)

---

## Example: Complete Refactored Function

### Before
```javascript
async function getAvailableDataRange() {
  const archivesDir = join(dataRoot, 'archives');
  
  if (!fs.existsSync(archivesDir)) {
    const now = Date.now() / 1000;
    return { oldest: now, newest: now };
  }
  
  let oldestTimestamp = Infinity;
  let newestTimestamp = 0;
  
  const channelDirs = fs.readdirSync(archivesDir, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => join(archivesDir, d.name));
    
  for (const channelDir of channelDirs) {
    // ... scan files ...
  }
  
  return { oldest: oldestTimestamp, newest: newestTimestamp };
}
```

### After
```javascript
/**
 * Get available data range by scanning NPZ archives
 * @param {GRAPEPaths} paths - Paths instance
 * @returns {Promise<Object>} { oldest, newest } timestamps
 */
async function getAvailableDataRange(paths) {
  const channels = paths.discoverChannels();
  
  if (channels.length === 0) {
    const now = Date.now() / 1000;
    return { oldest: now, newest: now };
  }
  
  let oldestTimestamp = Infinity;
  let newestTimestamp = 0;
  
  for (const channelName of channels) {
    const archiveDir = paths.getArchiveDir(channelName);
    
    if (!fs.existsSync(archiveDir)) continue;
    
    // Scan NPZ files
    const files = fs.readdirSync(archiveDir)
      .filter(f => f.endsWith('_iq.npz'))
      .map(f => {
        // Parse timestamp from filename: 20241115T120000Z_5000000_iq.npz
        const match = f.match(/^(\d{8}T\d{6}Z)/);
        if (match) {
          const isoStr = match[1].replace(/(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/, 
                                          '$1-$2-$3T$4:$5:$6Z');
          return new Date(isoStr).getTime() / 1000;
        }
        return null;
      })
      .filter(ts => ts !== null);
    
    if (files.length > 0) {
      oldestTimestamp = Math.min(oldestTimestamp, ...files);
      newestTimestamp = Math.max(newestTimestamp, ...files);
    }
  }
  
  return {
    oldest: oldestTimestamp === Infinity ? Date.now() / 1000 : oldestTimestamp,
    newest: newestTimestamp || Date.now() / 1000
  };
}
```

---

## Next Steps

1. Review and approve this architecture model
2. Begin systematic migration of `monitoring-server.js`
3. Test each refactored function
4. Document any new patterns discovered during migration
5. Update this document with lessons learned
