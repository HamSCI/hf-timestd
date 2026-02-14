# GRAPE Paths API - Web UI Integration Example

## Quick Integration for monitoring-server.js

### 1. Import the Paths API

```javascript
import { GRAPEPaths, channelNameToDir } from './grape-paths.js';

// Initialize at server startup
const paths = new GRAPEPaths(dataRoot);
```

### 2. Replace Manual Path Construction

**❌ OLD WAY (Inconsistent):**
```javascript
// Hardcoded path construction
const spectrogramPath = join(
  dataRoot,
  'spectrograms',
  date,
  `${channelDirName}_${date}_carrier_spectrogram.png`
);

// Manual state file naming
const stateFile = join(
  dataRoot, 
  'state',
  `analytics-${channelDirName.toLowerCase()}.json`
);
```

**✅ NEW WAY (Iron-clad):**
```javascript
// Use paths API
const spectrogramPath = paths.getSpectrogramPath(channelName, date, 'carrier');
const stateFile = paths.getAnalyticsStateFile(channelName);
```

### 3. Full Example - Spectrogram Endpoint

```javascript
/**
 * Get spectrogram PNG (BEFORE - with bugs)
 */
app.get('/api/v1/channels/:channelName/spectrogram/:type/:date', async (req, res) => {
  try {
    const { channelName, type, date } = req.params;
    
    // ❌ Manual path construction - prone to errors
    const channelDirName = channelName.replace(/ /g, '_');
    const spectrogramPath = join(
      dataRoot,
      'spectrograms',
      date,
      `${channelDirName}_${date}_${type}_spectrogram.png`
    );
    
    if (!fs.existsSync(spectrogramPath)) {
      return res.status(404).json({ error: 'Not found' });
    }
    
    res.sendFile(spectrogramPath);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

/**
 * Get spectrogram PNG (AFTER - using paths API)
 */
app.get('/api/v1/channels/:channelName/spectrogram/:type/:date', async (req, res) => {
  try {
    const { channelName, type, date } = req.params;
    
    // ✅ Use paths API - guaranteed correct
    const spectrogramPath = paths.getSpectrogramPath(channelName, date, type);
    
    if (!fs.existsSync(spectrogramPath)) {
      return res.status(404).json({ error: 'Not found' });
    }
    
    res.sendFile(spectrogramPath);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});
```

### 4. Channel Discovery Example

```javascript
/**
 * Get list of available channels (BEFORE)
 */
app.get('/api/v1/channels', async (req, res) => {
  try {
    const archivesDir = join(dataRoot, 'archives');
    const entries = fs.readdirSync(archivesDir, { withFileTypes: true });
    
    const channels = entries
      .filter(e => e.isDirectory())
      .map(e => e.name.replace(/_/g, ' '));
    
    res.json({ channels });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

/**
 * Get list of available channels (AFTER - using paths API)
 */
app.get('/api/v1/channels', async (req, res) => {
  try {
    // ✅ One line, always correct
    const channels = paths.discoverChannels();
    
    res.json({ channels });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});
```

### 5. State File Access Example

```javascript
/**
 * Get timing analysis data (BEFORE)
 */
app.get('/api/v1/timing/analysis', async (req, res) => {
  const { channel } = req.query;
  
  // ❌ Manual conversion - breaks on decimal frequencies
  let stateKey;
  if (channel.includes('WWV')) {
    const freq = channel.split(' ')[1];
    stateKey = `analytics-wwv${freq}.json`;
  } else if (channel.includes('CHU')) {
    const freq = channel.split(' ')[1];
    stateKey = `analytics-chu${freq}.json`;
  }
  
  const stateFile = join(dataRoot, 'state', stateKey);
  const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  
  res.json(state);
});

/**
 * Get timing analysis data (AFTER - using paths API)
 */
app.get('/api/v1/timing/analysis', async (req, res) => {
  const { channel } = req.query;
  
  // ✅ Handles all edge cases automatically
  const stateFile = paths.getAnalyticsStateFile(channel);
  const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  
  res.json(state);
});
```

## Benefits Summary

| Before (Manual) | After (Paths API) |
|----------------|-------------------|
| 5-10 lines of path logic | 1 line |
| Bugs with "WWV 2.5 MHz" | Works automatically |
| Inconsistent between endpoints | Always consistent |
| Hard to test | Easy to mock |
| Breaks when structure changes | Change once, works everywhere |

## Testing

```javascript
// Unit test example
import { describe, it, expect } from 'your-test-framework';
import { GRAPEPaths } from './grape-paths.js';

describe('GRAPE Paths', () => {
  const paths = new GRAPEPaths('/tmp/test');
  
  it('handles decimal frequencies', () => {
    const stateFile = paths.getAnalyticsStateFile('WWV 2.5 MHz');
    expect(stateFile).toBe('/tmp/test/state/analytics-wwv2.5.json');
  });
  
  it('generates correct spectrogram paths', () => {
    const spec = paths.getSpectrogramPath('CHU 3.33 MHz', '20251115', 'carrier');
    expect(spec).toBe('/tmp/test/spectrograms/20251115/CHU_3.33_MHz_20251115_carrier_spectrogram.png');
  });
});
```

## Migration Checklist for monitoring-server.js

- [ ] Import paths API at top of file
- [ ] Initialize `paths` instance with `dataRoot`
- [ ] Replace all `join(dataRoot, 'spectrograms', ...)` calls
- [ ] Replace all `join(dataRoot, 'state', ...)` calls
- [ ] Replace all `join(dataRoot, 'archives', ...)` calls
- [ ] Replace all manual channel name conversions
- [ ] Test with WWV 2.5 MHz and CHU 3.33 MHz
- [ ] Remove duplicate path logic

## See Also

- `docs/PATHS_API_MIGRATION.md` - Full migration guide
- `src/signal_recorder/paths.py` - Python reference implementation
- `web-ui/test-paths.js` - JavaScript test examples
