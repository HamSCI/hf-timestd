# Web-UI Implementation Guide - Quick Start
**Date:** November 24, 2025  
**Purpose:** Practical code examples for Priority 1 improvements  
**Related:** See `WEB_UI_IMPROVEMENT_RECOMMENDATIONS.md` for full analysis

---

## Quick Win: Timing Status API (30 minutes)

**File:** `web-ui/monitoring-server-v3.js`  
**Add after line 1900:**

```javascript
// GET /api/v1/timing/status - Real-time timing quality
app.get('/api/v1/timing/status', async (req, res) => {
  try {
    const channels = config.recorder?.channels?.filter(ch => ch.enabled) || [];
    let bestRef = null;
    let breakdown = { tone_locked: 0, ntp_synced: 0, interpolated: 0, wall_clock: 0 };
    
    for (const ch of channels) {
      const name = ch.description || `Channel ${ch.ssrc}`;
      const stateFile = join(paths.getStateDir(), `analytics-${name.replace(/ /g, '_')}.json`);
      
      if (fs.existsSync(stateFile)) {
        const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
        if (state.time_snap) {
          const age = Date.now() / 1000 - state.time_snap.last_update;
          const src = state.time_snap.source || 'UNKNOWN';
          
          if (src === 'TONE_DETECTED' && age < 300) {
            breakdown.tone_locked++;
            if (!bestRef || age < bestRef.age_seconds) {
              bestRef = {
                channel: name,
                station: state.time_snap.station,
                time_snap_utc: new Date(state.time_snap.utc_timestamp * 1000).toISOString(),
                source: src,
                confidence: state.time_snap.confidence || 0,
                age_seconds: Math.round(age)
              };
            }
          } else if (src === 'NTP_SYNCED') breakdown.ntp_synced++;
          else if (age < 3600) breakdown.interpolated++;
          else breakdown.wall_clock++;
        }
      }
    }
    
    const status = breakdown.tone_locked > 0 ? 'TONE_LOCKED' : 
                   breakdown.ntp_synced > 0 ? 'NTP_SYNCED' : 'WALL_CLOCK';
    const precision = status === 'TONE_LOCKED' ? 1.0 : status === 'NTP_SYNCED' ? 10.0 : 1000.0;
    
    res.json({
      overall_status: status,
      precision_estimate_ms: precision,
      primary_reference: bestRef,
      channel_breakdown: breakdown
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
```

**Test:**
```bash
curl http://localhost:3000/api/v1/timing/status | jq
```

---

## Quick Win: Tone Power Display (45 minutes)

**File:** `web-ui/monitoring-server-v3.js`  
**Add:**

```javascript
// GET /api/v1/tones/current - Current tone power levels
app.get('/api/v1/tones/current', async (req, res) => {
  try {
    const channels = config.recorder?.channels?.filter(ch => ch.enabled) || [];
    const result = [];
    const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    
    for (const ch of channels) {
      const name = ch.description || `Channel ${ch.ssrc}`;
      const csvPath = join(
        paths.get_tone_detections_dir(name),
        `${name.replace(/ /g, '_')}_tones_${today}.csv`
      );
      
      let wwvPower = null, wwvhPower = null;
      
      if (fs.existsSync(csvPath)) {
        const lines = fs.readFileSync(csvPath, 'utf8').trim().split('\n').slice(-11);
        for (const line of lines.reverse()) {
          const fields = line.split(',');
          if (fields[1] === 'WWV' && fields[2] === '1000') wwvPower = parseFloat(fields[6]);
          if (fields[1] === 'WWVH' && fields[2] === '1200') wwvhPower = parseFloat(fields[6]);
          if (wwvPower && wwvhPower) break;
        }
      }
      
      result.push({
        channel: name,
        tone_1000_hz_db: wwvPower,
        tone_1200_hz_db: wwvhPower,
        ratio_db: (wwvPower && wwvhPower) ? (wwvPower - wwvhPower) : null
      });
    }
    
    res.json({ channels: result, last_updated: new Date().toISOString() });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
```

---

## Quick Win: Enhanced Discrimination API (1 hour)

**File:** `web-ui/monitoring-server-v3.js`  
**Add:**

```javascript
// GET /api/v1/channels/:name/discrimination/:date/methods
app.get('/api/v1/channels/:name/discrimination/:date/methods', async (req, res) => {
  try {
    const { name, date } = req.params;
    const ch = name.replace(/ /g, '_');
    
    const methods = {
      timing_tones: loadCSV(join(paths.get_tone_detections_dir(name), `${ch}_tones_${date}.csv`)),
      tick_windows: loadCSV(join(paths.get_tick_windows_dir(name), `${ch}_ticks_${date}.csv`)),
      station_id: loadCSV(join(paths.get_station_id_440hz_dir(name), `${ch}_440hz_${date}.csv`)),
      bcd: loadCSV(join(paths.get_bcd_discrimination_dir(name), `${ch}_bcd_${date}.csv`)),
      voting: loadCSV(join(paths.get_discrimination_dir(name), `${ch}_discrimination_${date}.csv`))
    };
    
    res.json({ channel: name, date, methods });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

function loadCSV(path) {
  if (!fs.existsSync(path)) return { status: 'NO_DATA', records: [] };
  
  const lines = fs.readFileSync(path, 'utf8').trim().split('\n');
  if (lines.length < 2) return { status: 'EMPTY', records: [] };
  
  const header = lines[0].split(',');
  const records = lines.slice(1).map(line => {
    const values = line.split(',');
    const record = {};
    header.forEach((col, i) => record[col.trim()] = values[i]?.trim() || null);
    return record;
  });
  
  return { status: 'OK', records, count: records.length };
}
```

---

## Frontend: Timing Status Widget

**Create:** `web-ui/components/timing-status.js`

```javascript
class TimingStatusWidget {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    this.update();
    setInterval(() => this.update(), 10000);
  }
  
  async update() {
    const data = await fetch('/api/v1/timing/status').then(r => r.json());
    
    const statusColors = {
      TONE_LOCKED: '#10b981',
      NTP_SYNCED: '#f59e0b',
      WALL_CLOCK: '#ef4444'
    };
    
    this.container.innerHTML = `
      <div style="background: linear-gradient(135deg, #1e3a8a, #312e81); 
                  padding: 20px; border-radius: 10px; color: white;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <h3>⏱️ Timing Status</h3>
          <span style="background: ${statusColors[data.overall_status]}; 
                       padding: 8px 16px; border-radius: 20px; font-weight: 600;">
            ${data.overall_status}
          </span>
        </div>
        ${data.primary_reference ? `
          <div style="margin-top: 15px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px;">
            <div>
              <div style="font-size: 11px; color: #94a3b8;">REFERENCE</div>
              <div style="font-weight: 600;">${data.primary_reference.channel}</div>
            </div>
            <div>
              <div style="font-size: 11px; color: #94a3b8;">PRECISION</div>
              <div style="font-weight: 600;">±${data.precision_estimate_ms} ms</div>
            </div>
            <div>
              <div style="font-size: 11px; color: #94a3b8;">AGE</div>
              <div style="font-weight: 600;">${this.formatAge(data.primary_reference.age_seconds)}</div>
            </div>
          </div>
        ` : '<div style="color: #f59e0b;">No timing reference</div>'}
        <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.2);">
          <strong>Channel Status:</strong>
          🟢 ${data.channel_breakdown.tone_locked} tone-locked,
          🟡 ${data.channel_breakdown.ntp_synced} NTP-synced,
          🔴 ${data.channel_breakdown.wall_clock} wall-clock
        </div>
      </div>
    `;
  }
  
  formatAge(sec) {
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec/60)}m`;
    return `${Math.floor(sec/3600)}h`;
  }
}
```

**Use in summary.html:**
```html
<div id="timingStatus"></div>
<script src="components/timing-status.js"></script>
<script>new TimingStatusWidget('timingStatus');</script>
```

---

## Summary: 3 APIs in 2 Hours

1. **Timing Status** - Shows TONE_LOCKED/NTP_SYNCED status
2. **Tone Powers** - Current 1000/1200 Hz power levels
3. **Multi-Method Discrimination** - All 5 discrimination methods

**Total Impact:**  
- Operators see timing quality instantly
- Tone detection performance visible
- Full discrimination transparency

**Next Steps:**
1. Add these endpoints to monitoring-server-v3.js
2. Create timing-status widget
3. Test with live data
4. Add to summary.html

See `WEB_UI_IMPROVEMENT_RECOMMENDATIONS.md` for full feature set and priorities.
