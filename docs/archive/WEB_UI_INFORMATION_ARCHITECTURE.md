# Web UI Information Architecture - Master Index

**Purpose:** Comprehensive specification for GRAPE Signal Recorder web interface information display and user experience.

**Last Updated:** 2024-11-10  
**Status:** Specification Complete  
**Version:** 1.0

---

## Overview

This document series defines the complete information architecture for the GRAPE V2 dual-service web monitoring interface. The specifications enable operators to monitor system health, assess data quality, and troubleshoot issues effectively.

**Architecture:** V2 Dual-Service (Core Recorder + Analytics Service)  
**Target Users:** Amateur radio operators, HamSCI researchers  
**Design Philosophy:** Simple overview → detailed diagnostics on demand

---

## Document Structure

### 1. [System Monitoring](WEB_UI_SYSTEM_MONITORING.md)

**Scope:** System-level operational metrics and health indicators

**Key Topics:**
- Service health status (core recorder + analytics)
- Data pipeline monitoring (archive, process, upload)
- Resource utilization (disk, memory, CPU)
- Error monitoring and system health scoring
- Real-time update strategy

**Primary Metrics:**
- Services running/stopped
- Uptime and staleness
- NPZ files written/processed
- Disk space usage
- Recent errors

**Audience:** Operators checking "is it working?"

---

### 2. [Per-Channel Metrics](WEB_UI_CHANNEL_METRICS.md)

**Scope:** Individual channel data characterization and quality assessment

**Key Topics:**
- Core recording metrics (status, completeness, packet loss)
- Analytics processing status
- Tone detection performance (WWV/WWVH/CHU)
- Time reference status per channel
- Channel comparison table design

**Primary Metrics:**
- Recording status (recording/idle/error)
- Sample completeness %
- Packet loss rate
- Gap detection
- Tone detection counts
- Digital RF output

**Audience:** Operators troubleshooting specific channels

---

### 3. [Scientific Data Quality](WEB_UI_SCIENTIFIC_QUALITY.md)

**Scope:** Data quality reporting, provenance tracking, and scientific metadata

**Key Topics:**
- Quantitative completeness reporting
- Discontinuity tracking and classification
- Timing provenance chain
- WWV/WWVH propagation analysis
- Metadata for scientific use
- Data provenance audit trail

**Primary Metrics:**
- Completeness % with gap breakdown
- Discontinuity log (type, magnitude, cause)
- Time snap establishment and corrections
- WWV/WWVH discrimination ratio
- Differential delay analysis

**Audience:** Researchers assessing data validity

---

### 4. [Navigation & User Experience](WEB_UI_NAVIGATION_UX.md)

**Scope:** Information hierarchy, page structure, navigation patterns, workflows

**Key Topics:**
- Three-level information hierarchy
- Page layout and visual design
- Navigation structure (current + future)
- User workflows (daily check, troubleshoot, download)
- Responsive design guidelines
- Interactive features (sort, filter, expand)
- Performance and accessibility

**Key Design Decisions:**
- Single-page dashboard (current)
- Progressive disclosure (simple → detailed)
- Read-only monitoring (no config editing)
- Dark theme with status colors
- 60-second auto-refresh

**Audience:** UI developers and UX designers

---

## Cross-Cutting Concerns

### Data Sources

**V2 Architecture JSON Status Files:**

1. **Core Recorder Status:**
   - File: `/tmp/grape-test/status/core-recorder-status.json`
   - Update: Every 10 seconds
   - Content: Per-channel recording metrics, RTP stats, NPZ files written

2. **Analytics Service Status:**
   - Files: `/tmp/grape-test/analytics/{channel}/status/analytics-service-status.json`
   - Update: Every 10 seconds per channel
   - Content: NPZ processed, tone detections, quality metrics, Digital RF output

3. **Legacy V1 Status (Fallback):**
   - File: `/tmp/grape-test/status/recording-stats.json`
   - Content: V1 recorder metrics (deprecated)

### API Endpoints

**System Level:**
- `GET /api/v1/system/status` - Comprehensive system status
- `GET /api/v1/system/health` - Simple health check
- `GET /api/v1/system/errors` - Recent errors with filtering

**Channel Level:**
- `GET /api/v1/channels` - All channel metrics
- `GET /api/v1/channels/{name}` - Single channel detail

**Quality Level:**
- `GET /api/v1/quality/summary` - Aggregate quality metrics
- `GET /api/v1/quality/discontinuities` - Detailed gap log
- `GET /api/v1/quality/propagation` - WWV/WWVH analysis

**Legacy (Backward Compatibility):**
- `GET /api/monitoring/station-info` - Station metadata
- `GET /api/monitoring/timing-quality` - V1/V2 hybrid quality data

### Update Frequencies

**Real-Time (10s):**
- Service health status
- Per-channel recording metrics
- RTP packet statistics

**Near Real-Time (60s):**
- Dashboard full refresh
- Quality metrics aggregation
- Disk space check

**Hourly (Future):**
- Historical trend updates
- Daily summary generation

**On-Demand:**
- Error log queries
- Data export requests
- Configuration display

### Status Indicators

**Health Status:**
- 🟢 **HEALTHY:** All checks passing
- 🟡 **DEGRADED:** 1-2 warnings active
- 🔴 **CRITICAL:** Critical alert(s) active

**Service Status:**
- ✅ **Running:** Status file fresh (<30s)
- ⚠️ **Stale:** Status file 30-60s old
- ❌ **Stopped:** Status file >60s or missing

**Recording Status:**
- 🔴 **Recording:** Active packet reception
- ⏸️ **Idle:** No packets recently
- ❌ **Error:** Gaps/errors detected

**Quality Thresholds:**

| Metric | 🟢 Normal | 🟡 Warning | 🔴 Critical |
|--------|-----------|------------|-------------|
| Completeness | ≥99% | 95-99% | <95% |
| Packet Loss | <0.1% | 0.1-1% | >1% |
| Last Packet | <10s | 10-60s | >60s |
| Disk Space | <80% | 80-90% | >90% |
| Processing Lag | <5 files | 5-10 files | >10 files |

---

## Implementation Status

### Phase 1: Core Monitoring (Complete) ✅
**Delivered:** 2024-11-10

- [x] V2 dual-service status aggregation
- [x] System health display
- [x] Per-channel table
- [x] Basic alerts panel
- [x] 60-second auto-refresh
- [x] Station information display

**Files:**
- `web-ui/monitoring-server.js` - Backend API
- `web-ui/timing-dashboard.html` - Frontend dashboard

### Phase 2: Enhanced Monitoring (Next Priority) ⏳
**Target:** 2024-11-15

- [ ] Improved error log viewer with filtering
- [ ] Disk space alerts with thresholds
- [ ] Upload queue status display
- [ ] Processing lag indicators
- [ ] Per-channel detail expansion
- [ ] Quality trend sparklines

### Phase 3: Deep Diagnostics (Future) ⏸️
**Target:** 2024-12-01

- [ ] Discontinuity timeline visualization
- [ ] Minute-by-minute quality history
- [ ] WWV/WWVH differential delay charts
- [ ] Time snap correction log
- [ ] RTP drift monitoring
- [ ] Gap severity classification

### Phase 4: Data Access (Future) ⏸️
**Target:** 2025-01-15

- [ ] Archive file browser
- [ ] Download interface
- [ ] Bulk export tools
- [ ] Quality CSV generation
- [ ] Date range selector
- [ ] PSWS upload tracking

### Phase 5: Advanced Features (Future) ⏸️
**Target:** 2025-02-01

- [ ] WebSocket real-time updates
- [ ] Historical trend graphs (Chart.js)
- [ ] Station location map
- [ ] Email/SMS alerting
- [ ] PDF report generation
- [ ] Mobile-responsive enhancements

---

## Design Principles

### 1. Simplicity First
**Principle:** Most users just want "is it working?"

**Implementation:**
- Large, obvious health indicator at top
- Color-coded status (green/yellow/red)
- "No alerts" as prominent positive feedback
- Hide complexity until needed

### 2. Progressive Disclosure
**Principle:** Simple overview → detailed diagnostics

**Implementation:**
- Dashboard: Quick health check
- Table: Per-channel summary
- Expand: Detailed metrics on click
- Drill-down: Full history pages (future)

### 3. Trust Through Transparency
**Principle:** Complete disclosure of data quality

**Implementation:**
- Quantitative metrics (no hiding gaps)
- Discontinuity log with causes
- Time snap provenance chain
- No subjective quality grades (use percentages)

### 4. Operator-Centric
**Principle:** Designed for amateur radio operators, not developers

**Implementation:**
- No technical jargon (explain terms)
- Read-only monitoring (no scary controls)
- Config editing via TOML files (safer)
- In-app help and tooltips

### 5. Scientific Rigor
**Principle:** Enable valid scientific conclusions

**Implementation:**
- Complete metadata preservation
- Gap categorization (network/source/offline)
- WWV/WWVH discrimination transparency
- Time reference provenance
- Exportable quality reports

---

## Visual Design System

### Color Palette
```css
/* Background */
--bg-primary: #0a0e27;      /* Dark navy */
--bg-secondary: #1e293b;    /* Slate */
--bg-accent: #0f172a;       /* Darker slate */

/* Status Colors */
--status-success: #10b981;  /* Green */
--status-warning: #f59e0b;  /* Amber */
--status-error: #ef4444;    /* Red */
--status-info: #3b82f6;     /* Blue */
--status-special: #8b5cf6;  /* Purple (CHU) */

/* Text */
--text-primary: #e0e0e0;    /* Light gray */
--text-secondary: #94a3b8;  /* Slate gray */
--text-highlight: #ffffff;  /* White */
```

### Typography Scale
```
H1: 28px - Page titles
H2: 18px - Section headers
H3: 16px - Subsection headers
Body: 14px - Normal text
Small: 12px - Labels, metadata
Code: 13px - Monospace values
```

### Spacing System
```
Base unit: 8px
Small: 8px
Medium: 16px
Large: 24px
XLarge: 32px
```

---

## Accessibility Requirements

### Keyboard Navigation
- Logical tab order (top to bottom, left to right)
- Skip to content link
- Table keyboard navigation
- Focus indicators visible

### Screen Reader Support
- ARIA labels on all status indicators
- Table headers properly marked
- Live regions for auto-updating content
- Descriptive link text

### Color Contrast
- WCAG AA compliance (7:1 ratio)
- Status not indicated by color alone
- Icons + text for all states
- High contrast mode support

### Responsive Design
- Desktop (>1200px): Full layout
- Tablet (768-1200px): Stacked panels
- Mobile (<768px): Simplified, scrollable

---

## Performance Targets

### Initial Load
- **Target:** <1 second on local network
- **Method:** Inline CSS, no external dependencies
- **Size:** <100 KB HTML+CSS+JS

### Data Refresh
- **Current:** 60-second full refresh
- **Bandwidth:** ~1 KB/s (negligible)
- **Future:** WebSocket deltas (10-second updates)

### Browser Compatibility
- **Chrome:** Last 2 versions
- **Firefox:** Last 2 versions
- **Safari:** Last 2 versions
- **Edge:** Last 2 versions
- **No IE11 support required**

---

## Testing Requirements

### Functional Testing
- [ ] All API endpoints return valid JSON
- [ ] Status indicators update correctly
- [ ] Table sorting works on all columns
- [ ] Auto-refresh countdown accurate
- [ ] Error states display properly

### Data Testing
- [ ] V2 status files parsed correctly
- [ ] V1 fallback works when V2 unavailable
- [ ] Stale data detection triggers warnings
- [ ] Missing data handled gracefully
- [ ] Aggregation math verified

### Visual Testing
- [ ] Layout renders correctly on all screen sizes
- [ ] Color contrast meets WCAG AA
- [ ] Status colors distinguishable
- [ ] Icons render properly
- [ ] Tables scroll horizontally on narrow screens

### Performance Testing
- [ ] Initial load <1s
- [ ] Refresh causes no UI flicker
- [ ] Large channel counts (9+) perform well
- [ ] Error log (100+ entries) renders smoothly

---

## Documentation Requirements

### For Operators
- Quick start guide (embedded in UI)
- Troubleshooting tips
- Alert interpretation guide
- Configuration reference link

### For Developers
- API endpoint documentation
- Status file format specifications
- Component architecture
- Build and deployment guide

### For Researchers
- Data quality metrics explanation
- Discontinuity type definitions
- Time snap provenance details
- Export format specifications

---

## Future Considerations

### Scalability
- Support for 20+ channels (multiple radios)
- Distributed monitoring (multiple stations)
- Long-term data retention (years)
- Historical data compression

### Integration
- PSWS upload status tracking
- External alerting (email, SMS, Slack)
- Data sharing with wsprdaemon
- Integration with other HamSCI tools

### Advanced Analytics
- Machine learning for anomaly detection
- Propagation prediction models
- Automated quality assessment
- Pattern recognition in WWV/WWVH data

---

## Related Documentation

**Architecture:**
- `ARCHITECTURE.md` - Overall system design
- `CORE_ANALYTICS_SPLIT_DESIGN.md` - V2 dual-service architecture
- `ANALYTICS_SERVICE_IMPLEMENTATION.md` - Analytics pipeline details

**APIs:**
- `INTERFACES_COMPLETE.md` - Complete API interface summary
- `src/signal_recorder/interfaces/README.md` - API usage guide

**Technical:**
- `docs/MULTI_STATION_TONE_DETECTION.md` - Tone detection algorithm
- `docs/GRAPE_DIGITAL_RF_RECORDER.md` - Digital RF output spec
- `PSWS_COMPATIBILITY_UPDATE.md` - PSWS format verification

**Operations:**
- `INSTALLATION.md` - Setup guide
- `STARTUP_GUIDE.md` - Service startup procedures
- `web-ui/README.md` - Web interface deployment

---

## Change Log

### Version 1.0 (2024-11-10)
- Initial specification complete
- Four-document structure established
- V2 dual-service architecture documented
- Current implementation status assessed
- Future roadmap defined

---

**Document Series:**
1. [System Monitoring](WEB_UI_SYSTEM_MONITORING.md) - Operational metrics
2. [Per-Channel Metrics](WEB_UI_CHANNEL_METRICS.md) - Channel data
3. [Scientific Data Quality](WEB_UI_SCIENTIFIC_QUALITY.md) - Quality & provenance
4. [Navigation & UX](WEB_UI_NAVIGATION_UX.md) - User experience

**Maintainer:** Michael James Hauan (AC0G)  
**Last Updated:** 2024-11-10 Evening  
**Status:** Specification Complete - Ready for Implementation
