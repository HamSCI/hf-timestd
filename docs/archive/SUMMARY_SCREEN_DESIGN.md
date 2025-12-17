# Summary Screen - Detailed Design
**Component:** GRAPE Monitoring UI - Summary Dashboard  
**URL:** `/` or `/summary`  
**Update Frequency:** 5 seconds (auto-refresh)  
**Design Date:** 2024-11-15

---

## Purpose

Operational awareness at-a-glance:
- **Is the system running?** (radiod, core recorder, analytics)
- **Is data being recorded?** (RTP streams, continuity)
- **What's the data quality?** (SNR, timing basis per channel)
- **Can I listen live?** (Audio proxy per channel)

---

## Screen Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. STATION INFO                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ Callsign: K1ABC                Grid Square: FN42                        │
│ Receiver: GRAPE                 Instrument ID: grape-001                │
│ Mode: TEST                      Data Root: /tmp/grape-test              │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│ 2. SYSTEM STATUS                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│ a) PROCESSES                                                            │
│    radiod (ka9q-radio):    ● RUNNING   Uptime: 14d 3h 24m              │
│    Core Recorder:          ● RUNNING   Uptime: 3h 24m   Channels: 9/9  │
│    Analytics Service:      ● RUNNING   Uptime: 3h 24m   Processing: 9/9│
│                                                                         │
│ b) DATA CONTINUITY                                                      │
│    Data Span:              2024-11-01 00:00 UTC → 2024-11-15 18:45 UTC │
│                            (14 days, 18 hours, 45 minutes)              │
│    System Downtime:        3 gaps totaling 2h 15m (0.6% of span)       │
│    • 2024-11-03 14:23-15:45 UTC (1h 22m) - Planned maintenance         │
│    • 2024-11-08 03:12-03:45 UTC (33m)    - Power interruption          │
│    • 2024-11-12 11:00-11:20 UTC (20m)    - Software update             │
│                                                                         │
│ c) STORAGE                                                              │
│    Location:               /tmp/grape-test                              │
│    Used:                   23.4 GB / 100 GB (23.4%)                     │
│    Estimated Full:         ~17 days (at current rate: 1.6 GB/day)      │
│    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░        │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│ 3. CHANNEL STATUS                                                       │
├──────────────┬────────┬──────────┬────────────┬─────────────────────────┤
│ Channel      │ RTP    │ SNR (dB) │ Time Basis │ Audio                   │
├──────────────┼────────┼──────────┼────────────┼─────────────────────────┤
│ WWV 2.5 MHz  │ ● ON   │ 42.3     │ GPS_LOCKED │ [🔊 Listen]             │
│ WWV 5 MHz    │ ● ON   │ 58.1     │ GPS_LOCKED │ [🔊 Listen]             │
│ WWV 10 MHz   │ ● ON   │ 61.5     │ GPS_LOCKED │ [🔊 Listen]             │
│ WWV 15 MHz   │ ● ON   │ 54.2     │ GPS_LOCKED │ [🔊 Listen]             │
│ WWV 20 MHz   │ ● ON   │ 38.7     │ NTP_SYNCED │ [🔊 Listen]             │
│ WWV 25 MHz   │ ● ON   │ 12.4     │ NTP_SYNCED │ [🔊 Listen]             │
│ CHU 3.33 MHz │ ● ON   │ 47.9     │ GPS_LOCKED │ [🔊 Listen]             │
│ CHU 7.85 MHz │ ● ON   │ 52.3     │ GPS_LOCKED │ [🔊 Listen]             │
│ CHU 14.67MHz │ ● ON   │ 29.1     │ NTP_SYNCED │ [🔊 Listen]             │
└──────────────┴────────┴──────────┴────────────┴─────────────────────────┘

Last Updated: 2024-11-15 18:45:32 UTC (5 seconds ago)
```

---

## Section 1: Station Info

### Data Elements
```javascript
{
  "callsign": "K1ABC",           // config.station.callsign
  "grid_square": "FN42",          // config.station.grid_square
  "receiver": "GRAPE",            // config.station.receiver_name or "GRAPE"
  "instrument_id": "grape-001",   // config.station.instrument_id
  "mode": "TEST",                 // config.recorder.mode
  "data_root": "/tmp/grape-test"  // config.recorder.[test|production]_data_root
}
```

### API Endpoint
**`GET /api/v1/station/info`**

Response:
```json
{
  "callsign": "K1ABC",
  "grid_square": "FN42",
  "receiver": "GRAPE",
  "instrument_id": "grape-001",
  "mode": "test",
  "data_root": "/tmp/grape-test"
}
```

### UI Notes
- Read-only display
- Configuration comes from TOML file (no editing in UI)
- Display mode badge: "TEST" (yellow) or "PRODUCTION" (green)

---

## Section 2a: System Status - Processes

### Data Elements

**radiod (ka9q-radio):**
```javascript
{
  "name": "radiod",
  "running": true,
  "uptime_seconds": 1234567,
  "uptime_display": "14d 3h 24m",
  "pid": 12345,
  "source": "systemctl"  // or "ps" or "not_found"
}
```

**Core Recorder:**
```javascript
{
  "name": "core_recorder",
  "running": true,
  "uptime_seconds": 12345,
  "uptime_display": "3h 24m",
  "channels_active": 9,
  "channels_total": 9,
  "packets_received": 4567890,
  "last_update": 1700075742.5,
  "age_seconds": 5
}
```

**Analytics Service:**
```javascript
{
  "name": "analytics_service",
  "running": true,
  "uptime_seconds": 12340,
  "uptime_display": "3h 24m",
  "channels_processing": 9,
  "channels_total": 9,
  "files_processed": 2468,
  "last_update": 1700075740.0,
  "age_seconds": 7
}
```

### API Endpoint
**`GET /api/v1/system/processes`**

Response:
```json
{
  "radiod": {
    "running": true,
    "uptime_seconds": 1234567,
    "method": "systemctl"
  },
  "core_recorder": {
    "running": true,
    "uptime_seconds": 12345,
    "channels_active": 9,
    "channels_total": 9,
    "last_update": 1700075742.5
  },
  "analytics_service": {
    "running": true,
    "uptime_seconds": 12340,
    "channels_processing": 9,
    "channels_total": 9,
    "last_update": 1700075740.0
  }
}
```

### Detection Methods

**radiod:**
- Detection via ka9q library interaction (core recorder receives RTP packets)
- If core recorder is receiving packets → radiod is running
- Check core recorder status: `packets_received` increasing over time
- No direct radiod query needed (ka9q library handles communication)

**Core Recorder:**
- Read status file: `{data_root}/status/core-recorder-status.json`
- Check timestamp age (<30 seconds = running)

**Analytics Service:**
- Scan for per-channel status files: `{data_root}/analytics/*/status/analytics-service-status.json`
- Aggregate across channels
- Check newest timestamp age

### UI Behavior
- **Status Indicator:**
  - ● Green = Running (age < 30s)
  - ◐ Yellow = Stale (30s < age < 120s)
  - ○ Red = Stopped (age > 120s or not found)
- **Uptime:** Convert seconds to human-readable (14d 3h 24m)
- **Channel counts:** Show active/total (e.g., "9/9")

---

## Section 2b: System Status - Data Continuity

### Data Elements
```javascript
{
  "data_span": {
    "start": "2024-11-01T00:00:00Z",
    "end": "2024-11-15T18:45:00Z",
    "duration_seconds": 1267500,
    "duration_display": "14 days, 18 hours, 45 minutes"
  },
  "gaps": [
    {
      "start": "2024-11-03T14:23:00Z",
      "end": "2024-11-03T15:45:00Z",
      "duration_seconds": 4920,
      "duration_display": "1h 22m",
      "reason": "Planned maintenance",
      "type": "system_down"
    }
  ],
  "total_downtime_seconds": 8100,
  "total_downtime_display": "2h 15m",
  "downtime_percentage": 0.6
}
```

### API Endpoint
**`GET /api/v1/system/continuity`**

Response:
```json
{
  "data_span": {
    "start": "2024-11-01T00:00:00Z",
    "end": "2024-11-15T18:45:00Z",
    "duration_seconds": 1267500
  },
  "gaps": [
    {
      "start": "2024-11-03T14:23:00Z",
      "end": "2024-11-03T15:45:00Z",
      "duration_seconds": 4920,
      "reason": "Planned maintenance"
    }
  ],
  "total_downtime_seconds": 8100,
  "downtime_percentage": 0.6
}
```

### Gap Detection Logic

**Definition:** System downtime = ALL channels missing data simultaneously

**Implementation:**
1. Scan archives directory for all channels
2. For each channel, list NPZ files sorted by timestamp
3. Identify gaps where timestamp jumps > 120 seconds
4. Cross-channel gap correlation (gap in ALL channels = system down)
5. Single-channel gaps = propagation/signal issues (not system downtime)

**Gap Reasons:**
- Auto-detect: Large gaps (>1 hour) likely planned maintenance
- Manual annotations: Reserved for future enhancement
  - Placeholder: `{data_root}/annotations/system_gaps.json`
  - Format: `[{"start": "ISO8601", "end": "ISO8601", "reason": "text"}]`
- Default: "System downtime" (generic)

### UI Behavior
- **Data span:** Show oldest to newest archive timestamp
- **Gap list:** Show up to 5 most recent gaps (expandable)
- **Percentage:** Calculate relative to total span
- **Color coding:**
  - <1% downtime: Green
  - 1-5% downtime: Yellow
  - >5% downtime: Red

---

## Section 2c: System Status - Storage

### Data Elements
```javascript
{
  "location": "/tmp/grape-test",
  "used_bytes": 25115934720,      // 23.4 GB
  "total_bytes": 107374182400,    // 100 GB
  "available_bytes": 82258247680, // 76.6 GB
  "used_percent": 23.4,
  "write_rate_bytes_per_day": 1717986918,  // 1.6 GB/day
  "estimated_days_until_full": 17
}
```

### API Endpoint
**`GET /api/v1/system/storage`**

Response:
```json
{
  "location": "/tmp/grape-test",
  "used_bytes": 25115934720,
  "total_bytes": 107374182400,
  "available_bytes": 82258247680,
  "used_percent": 23.4,
  "write_rate_bytes_per_day": 1717986918,
  "estimated_days_until_full": 17
}
```

### Calculation Methods

**Disk usage:**
```bash
# Linux: df command
df -B1 /tmp/grape-test | tail -1 | awk '{print $3, $2, $4}'
```

**Write rate:**
- Calculate total size of archives directory
- Divide by data span duration
- Project forward

**Estimated full:**
```javascript
days_until_full = available_bytes / write_rate_bytes_per_day
```

### UI Behavior
- **Progress bar:** Visual indicator (50 chars wide)
- **Color coding:**
  - <70% full: Green
  - 70-90% full: Yellow
  - >90% full: Red
- **Display format:** "23.4 GB / 100 GB (23.4%)"
- **Estimation:** Show if >1 day remaining, else "< 1 day"

---

## Section 3: Channel Status

### Data Elements (Per Channel)
```javascript
{
  "channel": "WWV 10 MHz",
  "rtp_streaming": true,
  "rtp_last_packet": 1700075742.5,
  "rtp_age_seconds": 5,
  "snr_db": 61.5,
  "snr_measurement_time": 1700075740.0,
  "time_basis": "GPS_LOCKED",  // GPS_LOCKED | NTP_SYNCED | INTERPOLATED | WALL_CLOCK
  "time_snap_age_seconds": 900,
  "audio_proxy_available": true,
  "audio_stream_url": "/audio/WWV_10_MHz"
}
```

### API Endpoint
**`GET /api/v1/channels/status`**

Response:
```json
{
  "channels": [
    {
      "name": "WWV 10 MHz",
      "rtp_streaming": true,
      "rtp_last_packet": 1700075742.5,
      "snr_db": 61.5,
      "time_basis": "GPS_LOCKED",
      "time_snap_age_seconds": 900,
      "audio_available": true,
      "audio_url": "/audio/WWV_10_MHz"
    }
  ],
  "timestamp": 1700075742.5
}
```

### Data Sources

**RTP Streaming:**
- Source: Core recorder status file
- Check `packets_received` increasing over time
- Age check: Last packet < 30 seconds = streaming

**SNR:**
- Source: Analytics service status file (per channel)
- Field: `current_snr_db` (calculated from most recent minute)
- Update frequency: Per-minute average

**Time Basis:**
- Source: Analytics service state file
- Logic:
  - `time_snap` exists AND age < 300s → GPS_LOCKED
  - `time_snap` age 300-3600s → INTERPOLATED
  - NTP synced (check `timedatectl`) → NTP_SYNCED
  - Otherwise → WALL_CLOCK

**Audio Proxy:**
- Check if audio proxy service running
- Verify multicast stream accessible
- Return stream URL if available

### Listen Button Implementation

**Audio Proxy Service (STUB - DEFERRED):**
```javascript
// FUTURE IMPLEMENTATION:
// Separate service: audio-proxy.js
// Listens to ka9q-radio multicast RTP
// Transcodes to web-friendly format (MP3 or Opus)
// Serves via HTTP streaming endpoint
// Example URL: http://localhost:3000/audio/WWV_10_MHz
```

**Frontend (Current - Stub):**
```html
<button disabled title="Audio proxy not yet implemented">🔊 Listen</button>

<script>
// Stub function - ready for future audio proxy integration
function playAudio(channel) {
  // TODO: Implement when audio-proxy service available
  // const audio = new Audio(`/audio/${channel}`);
  // audio.play();
  alert('Audio playback not yet implemented');
}
</script>
```

**Notes:**
- All buttons currently disabled (stub only)
- Tooltip: "Audio proxy not yet implemented"
- API returns `audio_available: false` for all channels
- Ready for future audio proxy integration

### UI Behavior

**RTP Status:**
- ● Green = Streaming (age < 30s)
- ○ Red = No packets (age > 30s)

**SNR Display:**
- Color coding:
  - >50 dB: Green (excellent)
  - 30-50 dB: Yellow (good)
  - <30 dB: Orange (marginal)
  - No signal: Red
- Update every refresh cycle (5s)

**Time Basis Logic:**
- TONE_LOCKED: time_snap age < 5 minutes (WWV/CHU tone detection)
- NTP_SYNCED: System NTP synchronized + no recent time_snap
- WALL_CLOCK: No NTP, no time_snapdge
- Tooltip: Show time_snap age or NTP offset

**Listen Button:**
- Enabled: Blue button with speaker icon
- Disabled: Gray button (if audio proxy unavailable)
- Click: Open audio player (inline or modal)

---

## Auto-Refresh Behavior

### Update Frequency
- **Default:** 5 seconds
- **User control:** Dropdown (5s, 10s, 30s, Off)
- **Pause on user interaction:** Stop refresh when modal open or audio playing

### Refresh Strategy
```javascript
// Full page refresh vs partial updates
setInterval(async () => {
  // Fetch all data
  const stationInfo = await fetch('/api/v1/station/info').then(r => r.json());
  const processes = await fetch('/api/v1/system/processes').then(r => r.json());
  const continuity = await fetch('/api/v1/system/continuity').then(r => r.json());
  const storage = await fetch('/api/v1/system/storage').then(r => r.json());
  const channels = await fetch('/api/v1/channels/status').then(r => r.json());
  
  // Update DOM (only changed elements)
  updateUI({ stationInfo, processes, continuity, storage, channels });
}, 5000);
```

### Performance
- Batch API calls (or single aggregated endpoint)
- Cache-Control headers for static data (station info)
- Incremental DOM updates (not full re-render)

---

## Error Handling

### Missing Data
- **Process not running:** Show "○ STOPPED" with red indicator
- **Status file stale:** Show "◐ STALE (2m ago)"
- **API error:** Show "⚠️ ERROR" with tooltip

### Degraded State
- **Some channels down:** Show "7/9" in process status
- **Partial data:** Display available info, mark missing as "N/A"
- **Network issues:** Show banner: "Connection interrupted, retrying..."

### User Feedback
- **Last updated timestamp:** Always visible
- **Age indicator:** "(5 seconds ago)" updates each refresh
- **Error details:** Hover tooltips or expandable sections

---

## Responsive Design

### Desktop (>1024px)
- Full 3-column layout
- Channel table: All columns visible
- System status: Side-by-side sections

### Tablet (768-1024px)
- 2-column layout
- Channel table: Abbreviated columns
- System status: Stacked sections

### Mobile (<768px)
- Single column
- Channel status: Card layout (one per channel)
- Simplified metrics (tap to expand details)

---

## Implementation Notes

### Data Aggregation
Implement BOTH approaches for flexibility:

**Individual endpoints** (detailed queries):
- `GET /api/v1/station/info`
- `GET /api/v1/system/processes`
- `GET /api/v1/system/continuity`
- `GET /api/v1/system/storage`
- `GET /api/v1/channels/status`

**Aggregated endpoint** (efficient single call):
- `GET /api/v1/summary` → Returns all data in one response

Frontend can choose based on use case (single refresh vs partial updates)

### Caching Strategy
- **Station info:** Cache for 5 minutes (rarely changes)
- **Process status:** No cache (real-time)
- **Continuity:** Cache for 60 seconds (expensive to calculate)
- **Storage:** Cache for 30 seconds
- **Channel status:** No cache (real-time metrics)

### Future Enhancements
- **Gap annotations:** Allow manual notes on system downtime
- **Alert thresholds:** Configurable warnings (disk >80%, process down)
- **Export metrics:** CSV download of channel status history
- **Audio recording:** Record stream to file for playback

---

## Testing Checklist

- [ ] Station info displays correctly from config
- [ ] Instrument ID visible
- [ ] radiod status detected (running/stopped)
- [ ] Core recorder status correct
- [ ] Analytics service status aggregated across channels
- [ ] Data continuity calculated correctly
- [ ] Gap detection identifies system downtime
- [ ] Storage calculations accurate
- [ ] SNR values display per channel
- [ ] Time basis colors correct
- [ ] Listen buttons work (if audio proxy available)
- [ ] Listen buttons disabled gracefully (if no audio proxy)
- [ ] Auto-refresh works at 5s intervals
- [ ] Responsive layout on mobile
- [ ] Error states display properly

---

## Next Steps

1. Review and approve this design
2. Implement API endpoints (with paths API)
3. Build frontend HTML/CSS/JS
4. Implement audio proxy service (optional)
5. Test with live data
6. Deploy and iterate
