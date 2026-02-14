# Web UI Information Architecture - Navigation & User Experience

**Purpose:** Define information hierarchy, page structure, navigation patterns, and user workflows for the GRAPE Signal Recorder web interface.

**Last Updated:** 2024-11-10  
**Status:** Specification Draft

---

## Overview

The web UI serves **amateur radio operators** who need to:
1. Monitor system health at a glance
2. Assess data quality for scientific validity
3. Troubleshoot issues when alerts occur
4. Access recorded data for analysis

**Design Philosophy:**
- **Simplicity First:** Most users just want "is it working?"
- **Progressive Disclosure:** Simple overview → detailed diagnostics when needed
- **No Configuration Editing:** Users edit TOML files directly (simpler, safer)
- **Read-Only Monitoring:** Web UI does NOT control the recorder

---

## 1. Information Hierarchy

### 1.1 Three-Level Structure

**Level 1: System Overview (Dashboard)**
- Quick health check: "Everything OK?"
- Service status (core + analytics)
- Key metrics summary
- Active alerts

**Level 2: Detailed Monitoring (Current)**
- Per-channel status table
- Quality metrics
- Tone detection summary
- Recent errors log

**Level 3: Deep Diagnostics (Future)**
- Per-channel detail pages
- Discontinuity timeline
- Historical trends
- Data export tools

### 1.2 Priority Order

**Most Important (Always Visible):**
1. System health indicator (🟢/🟡/🔴)
2. Service status (core recorder + analytics)
3. Radiod connection status
4. Active alerts count

**Important (Above Fold):**
5. Overall completeness %
6. Channels active / total
7. Recent errors (last 5)
8. Disk space usage

**Detailed (Below Fold):**
9. Per-channel table
10. WWV/WWVH discrimination
11. Time snap status
12. Processing statistics

---

## 2. Page Structure

### 2.1 Main Dashboard (Current Implementation)

**Layout Sections (Top to Bottom):**

```
┌─────────────────────────────────────────────────┐
│ HEADER: Site Title + Live Indicator + Countdown│
├─────────────────────────────────────────────────┤
│ STATION INFO: Callsign, Grid, ID, Uptime       │
├─────────────────────────────────────────────────┤
│ SYSTEM STATUS: Core + Analytics Health         │
├─────────────────────────────────────────────────┤
│ DATA QUALITY: Completeness + WWV/WWVH Analysis │
├─────────────────────────────────────────────────┤
│ CHANNEL TABLE: All channels, sortable          │
├─────────────────────────────────────────────────┤
│ ALERTS: Active warnings/errors                  │
└─────────────────────────────────────────────────┘
```

**Current File:** `web-ui/timing-dashboard.html`

### 2.2 Header Bar

**Fixed Top Bar:**
```
[📡 GRAPE Signal Recorder] [🟢 Live Monitoring] [⏱️ Next update: 47s]
```

**Elements:**
- **Title:** Always visible, links to dashboard
- **Live Indicator:** Pulsing dot when data fresh
- **Countdown:** Shows refresh timer
- **Mode Badge:** TEST vs PRODUCTION indicator

### 2.3 Station Information Panel

**Purpose:** Station identification and system metadata

**Content:**
- Callsign (e.g., AC0G)
- Grid Square (e.g., DM79lv)
- Station ID (PSWS identifier)
- Instrument ID
- Server uptime
- Description (optional)

**Visual:** Card with blue accent, grid layout

### 2.4 System Status Panel

**Purpose:** Quick health check for dual-service architecture

**Content:**
- Core Recorder: Running status + channel count
- Analytics Service: Running status + processing count
- NPZ Archives: Files written
- RTP Packets: Total received
- Processing: NPZ files processed

**Visual:** Card with status colors (green/yellow/red)

### 2.5 Data Quality Panel

**Purpose:** Scientific data assessment

**Content:**
- Signal Quality Section:
  - Average completeness %
  - Total gaps detected
  
- WWV/WWVH Discrimination Section:
  - WWV detections (Fort Collins)
  - WWVH detections (Hawaii)
  - CHU detections (Canada)
  - Propagation ratio

**Visual:** Split panel, two columns

### 2.6 Channel Table

**Purpose:** Per-channel detailed status

**Columns:**
1. Channel Name
2. Status Badge (recording/idle/error)
3. Completeness %
4. Packet Loss %
5. Gaps Count
6. NPZ Files Written
7. Packets Received
8. Last Packet (time ago)
9. NPZ Processed (analytics)
10. WWV Detections
11. Digital RF Samples

**Features:**
- Sortable columns (click header)
- Color-coded values (green/yellow/red)
- Compact formatting
- Hover tooltips (future)

### 2.7 Alerts Panel

**Purpose:** Active issues requiring attention

**Content:**
- Alert count badge
- List of active alerts with:
  - Timestamp
  - Channel name
  - Alert message
  - Severity level

**States:**
- ✅ No alerts: "All systems normal" (green)
- ⚠️ Warnings: Yellow alerts listed
- 🔴 Errors: Red alerts listed

---

## 3. Navigation Structure

### 3.1 Primary Navigation (Current)

**Single Page Application:**
- Dashboard (timing-dashboard.html) - Main view
- No multi-page navigation yet

**Top Bar Actions:**
- Config location hint: "Edit config: config/*.toml"
- No login/logout (no authentication)

### 3.2 Future Navigation (Proposed)

**Main Menu (Horizontal Tabs):**
```
[Dashboard] [Channels] [Quality] [Data] [Logs] [Help]
```

**Dashboard Tab (Default):**
- System overview
- Quick health check
- Active alerts

**Channels Tab:**
- Per-channel detail views
- Click channel name to expand
- Minute-by-minute history

**Quality Tab:**
- Discontinuity log
- Quality trends over time
- Completeness graphs
- WWV/WWVH propagation analysis

**Data Tab:**
- Archive file browser
- Download links
- Bulk export tools
- PSWS upload status

**Logs Tab:**
- Full error/warning log
- Filter by severity/channel
- Search functionality

**Help Tab:**
- Quick start guide
- Configuration reference
- Troubleshooting tips
- Contact information

### 3.3 Breadcrumb Navigation (Future)

**For Multi-Level Drilling:**
```
Dashboard > Channels > WWV 10 MHz > Minute Detail: 19:15
```

**Purpose:** Orient user, provide back navigation

---

## 4. User Workflows

### 4.1 Daily Check-In

**Goal:** Verify system is running normally

**Steps:**
1. Open dashboard
2. Check system health indicator (should be 🟢)
3. Verify channels active count (should be 9/9)
4. Glance at alerts panel (should say "No active alerts")
5. Done! (if all green)

**Time:** ~10 seconds

### 4.2 Investigate Warning

**Goal:** Understand and resolve an alert

**Steps:**
1. Notice yellow/red health indicator
2. Read alert message in alerts panel
3. Click affected channel in table
4. Review detailed metrics for that channel
5. Check recent errors log for context
6. Take action (restart service, check network, etc.)

**Time:** ~2-5 minutes

### 4.3 Assess Data Quality

**Goal:** Determine if data is suitable for scientific analysis

**Steps:**
1. Navigate to Quality tab (future)
2. Review completeness % for time period
3. Check discontinuity log for gaps
4. Verify time_snap was established
5. Export quality summary CSV

**Time:** ~5-10 minutes

### 4.4 Download Data

**Goal:** Retrieve archives for analysis

**Steps:**
1. Navigate to Data tab (future)
2. Select date range and channels
3. Choose format (NPZ, Digital RF, CSV)
4. Initiate download or export
5. Save to local machine

**Time:** ~2 minutes (+ download time)

### 4.5 Troubleshoot Propagation

**Goal:** Understand why WWV detections are low

**Steps:**
1. Check WWV/WWVH discrimination panel
2. Review detection rates by channel
3. Compare across frequencies (2.5-25 MHz)
4. Check time of day (ionosphere varies)
5. Understand: Low detections are NORMAL (ionospheric study!)

**Time:** ~5 minutes

---

## 5. Responsive Design

### 5.1 Desktop (>1200px)

**Layout:**
- Full horizontal space for tables
- Multi-column panels (quality split view)
- All information visible

### 5.2 Tablet (768-1200px)

**Layout:**
- Single-column panels stack
- Table scrolls horizontally
- Some columns may be hidden (prioritize key metrics)

### 5.3 Mobile (<768px)

**Layout:**
- Vertical stacking
- Simplified table (fewer columns)
- Expandable cards for detail
- Larger touch targets

**Priority Columns (Mobile):**
- Channel Name
- Status
- Completeness %
- Gaps
- Last Packet

---

## 6. Visual Design Language

### 6.1 Color Palette

**Background:**
- Primary: `#0a0e27` (dark navy)
- Secondary: `#1e293b` (slate)
- Accent: `#0f172a` (darker slate)

**Status Colors:**
- Success: `#10b981` (green)
- Warning: `#f59e0b` (amber)
- Error: `#ef4444` (red)
- Info: `#3b82f6` (blue)
- Special: `#8b5cf6` (purple for CHU)

**Text:**
- Primary: `#e0e0e0` (light gray)
- Secondary: `#94a3b8` (slate gray)
- Highlight: `#ffffff` (white)

### 6.2 Typography

**Font Stack:**
```css
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 
             Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
```

**Sizes:**
- H1: 28px (page title)
- H2: 18px (section headers)
- Body: 14px (normal text)
- Small: 12px (labels, metadata)
- Code: `Consolas, Monaco, 'Courier New', monospace`

### 6.3 Spacing

**Consistent Grid:**
- Base unit: 8px
- Small gap: 8px
- Medium gap: 16px
- Large gap: 24px
- Section padding: 24px
- Page margins: 20px

### 6.4 Icons

**Icon Library:** Unicode emojis (no dependencies)

**Common Icons:**
- 🟢 Green circle: Healthy/normal
- 🟡 Yellow circle: Warning
- 🔴 Red circle: Error/recording
- ⏸️ Pause: Idle/stopped
- ✅ Checkmark: Success/complete
- ⚠️ Warning triangle: Attention needed
- ❌ X mark: Failed/error
- 📡 Satellite dish: Signal/radio
- ⏱️ Timer: Time/scheduling
- 📊 Bar chart: Data/analytics

---

## 7. Interactive Features

### 7.1 Auto-Refresh

**Current:**
- Full page refresh every 60 seconds
- Countdown timer shows time until next update
- No manual refresh button needed

**Future:**
- WebSocket real-time updates (no polling)
- Selective refresh (only changed data)
- User-configurable interval (10s/30s/60s)

### 7.2 Sorting

**Table Columns:**
- Click column header to sort
- Arrow indicator shows sort direction
- Default: Sort by channel name

**Useful Sorts:**
- By completeness (find worst channels)
- By last packet (find stale channels)
- By gaps (find problem channels)
- By detections (find active propagation)

### 7.3 Filtering (Future)

**Channel Filters:**
- Show all (default)
- WWV only
- CHU only
- Errors only
- Active time_snap only

**Time Filters:**
- Last hour
- Last 24 hours
- Last 7 days
- Custom date range

### 7.4 Tooltips (Future)

**Hover Information:**
- Column headers: Explain metric
- Status badges: Show detail
- Metrics: Show calculation method
- Alerts: Show suggested action

### 7.5 Expandable Sections

**Accordion Panels:**
- Click section header to expand/collapse
- Useful for:
  - Recent errors (show 5, expand to 20)
  - Channel detail (click row to expand)
  - Quality timeline (hide when not needed)

---

## 8. Accessibility

### 8.1 Keyboard Navigation

**Tab Order:**
- Logical flow top to bottom
- Skip to content link
- Table navigation with arrow keys

### 8.2 Screen Reader Support

**ARIA Labels:**
- Status indicators have descriptive labels
- Table headers properly marked
- Live regions for auto-updating content

### 8.3 Color Contrast

**WCAG AA Compliance:**
- Text on background: 7:1 contrast ratio
- Status colors distinguishable
- Not relying on color alone (use icons + text)

---

## 9. Performance Considerations

### 9.1 Initial Load

**Optimize:**
- Inline critical CSS (no external stylesheet)
- Minimal JavaScript (vanilla, no frameworks)
- No external dependencies (no jQuery, React, etc.)

**Target:** <1s load time on local network

### 9.2 Data Transfer

**Current:**
- JSON status files (~10-50 KB)
- Refresh every 60s
- Bandwidth: ~1 KB/s (negligible)

**Future:**
- WebSocket: Only deltas
- Compression: gzip transfer
- Pagination: Limit history to last 100 items

### 9.3 Browser Compatibility

**Support:**
- Modern browsers (Chrome, Firefox, Safari, Edge)
- Last 2 versions
- ES6+ JavaScript (no IE11 support needed)

---

## 10. Error States

### 10.1 No Data Available

**Display:**
```
⚠️ Quality data not available
Recorder may not be running or no status files found.

Check:
- Is the recorder daemon running?
- Is data being written to: /tmp/grape-test/status/?
- Check logs: /tmp/grape-test/../logs/signal-recorder.log
```

### 10.2 Stale Data

**Display:**
```
⚠️ Data is stale (last updated 2 minutes ago)
Status file may not be updating.

Possible causes:
- Recorder daemon hung or crashed
- File write permissions issue
- Disk full
```

### 10.3 API Error

**Display:**
```
❌ Failed to load data from server
Error: Connection refused

Troubleshooting:
- Is the monitoring server running? (port 3000)
- Check: systemctl status signal-recorder-web
- Check logs: journalctl -u signal-recorder-web
```

### 10.4 Partial Data

**Display:**
```
⚠️ Partial data loaded
Core recorder data: ✅ Available
Analytics service data: ❌ Not available

Some metrics will be incomplete.
```

---

## 11. Help & Documentation

### 11.1 In-App Help

**Context-Sensitive Tips:**
- Hover tooltips on metrics
- Info icons with expandable explanations
- Link to full documentation

**Example:**
```
Completeness %  ℹ️
[Hover: "Percentage of expected samples received. 
100% = perfect, no gaps. <99% indicates packet loss."]
```

### 11.2 Quick Reference

**Embedded Guide:**
```
Quick Start Guide:
1. Check system health (should be 🟢 green)
2. Verify all channels are recording
3. Review alerts panel for issues
4. Completeness >99% is excellent

Need help? See README.md or contact AC0G
```

### 11.3 Links to Documentation

**Footer Links:**
- Installation Guide (INSTALLATION.md)
- Configuration Reference (CONFIG_ARCHITECTURE.md)
- Architecture Overview (ARCHITECTURE.md)
- GitHub Repository

---

## 12. Future Enhancements

### 12.1 Real-Time Graphs

**Time-Series Charts:**
- Completeness % over last 24 hours
- Detection rate by hour
- Packet rate graph
- Disk usage trend

**Library:** Chart.js (lightweight) or plain SVG

### 12.2 Map Visualization

**Station Location Map:**
- Show station location (grid square)
- WWV transmitter location (Fort Collins)
- WWVH transmitter location (Kauai)
- CHU transmitter location (Ottawa)
- Signal path visualization

**Purpose:** Understand propagation geometry

### 12.3 Email/SMS Alerts

**Notification System:**
- Configure email for critical alerts
- SMS for urgent issues (recorder down)
- Configurable thresholds
- Alert rate limiting (no spam)

### 12.4 Historical Comparison

**Compare Time Periods:**
- Today vs yesterday
- This week vs last week
- Detection rates by time of day
- Seasonal propagation patterns

### 12.5 Export Reports

**Generate PDF/CSV:**
- Monthly quality report
- Detection summary
- Gap analysis
- Configuration snapshot

---

## 13. Implementation Roadmap

### Phase 1: Current (Complete) ✅
- Single-page dashboard
- System status monitoring
- Per-channel table
- Auto-refresh
- Alerts display

### Phase 2: Enhanced Monitoring (Next) ⏳
- Improved error log viewer
- Disk space alerts
- Upload queue status
- Processing lag indicators

### Phase 3: Deep Diagnostics (Future) ⏸️
- Per-channel detail pages
- Discontinuity timeline
- Historical trends
- Quality graphs

### Phase 4: Data Access (Future) ⏸️
- Archive file browser
- Download interface
- Bulk export tools
- Search/filter capabilities

### Phase 5: Advanced Features (Future) ⏸️
- Real-time WebSocket updates
- Map visualization
- Email/SMS alerts
- PDF reports

---

## 14. API Design Philosophy

### 14.1 RESTful Endpoints

**Versioned:** `/api/v1/...`

**Resource-Based:**
- `/api/v1/system/status` - Overall system
- `/api/v1/channels` - All channels
- `/api/v1/channels/{name}` - Single channel
- `/api/v1/quality/summary` - Quality metrics
- `/api/v1/errors` - Error log

### 14.2 Response Format

**Consistent Structure:**
```json
{
  "timestamp": "2024-11-10T20:30:00Z",
  "available": true,
  "data": { ... },
  "meta": {
    "source": "v2_status_files",
    "refresh_interval": 10
  }
}
```

### 14.3 Error Handling

**HTTP Status Codes:**
- 200: Success
- 404: Resource not found
- 500: Server error

**Error Response:**
```json
{
  "error": "Failed to read status file",
  "details": "File not found: /tmp/grape-test/status/...",
  "timestamp": "2024-11-10T20:30:00Z"
}
```

---

## Summary

The web UI information architecture prioritizes:

1. **Simplicity** - Quick health check for daily monitoring
2. **Clarity** - Obvious status indicators (color, icons, text)
3. **Depth** - Detailed diagnostics when troubleshooting
4. **Trust** - Transparent data quality reporting
5. **Usability** - Intuitive navigation, no training required

**Next Steps:**
- Implement Phase 2 enhancements (error log, disk alerts)
- Add per-channel detail drill-down
- Develop quality trend graphs
- Build data export interface

---

**Related Documents:**
- `WEB_UI_SYSTEM_MONITORING.md` - System health metrics specification
- `WEB_UI_CHANNEL_METRICS.md` - Per-channel data requirements
- `WEB_UI_SCIENTIFIC_QUALITY.md` - Data quality & provenance reporting
