# Web-UI Priority Decisions Needed

## Immediate Questions for User

### 1. **What should we delete right now?**

**Definitely safe to delete:**
- `monitoring.html.OLD`
- `monitoring.html.backup`  
- `index.html.OLD`
- `simple-server.js.backup-20251103-094306`
- `simple-server.js.backup-20251104-070345`

**Needs decision:**
- `simple-server.js` (90KB) - **Is this still used? What does it do?**
- `live-status.html` - **Does it duplicate `timing-dashboard.html`?**

### 2. **Analytics Priority Order**

**Tier 1 (Essential for operations)** - Implement first:
1. System status dashboard (recorder running, channels active, disk space)
2. Time_snap status (established, source, age)
3. Recent errors/warnings log
4. Upload queue status

**Tier 2A (Most valuable scientific)** - Implement next:
1. WWV/WWVH discrimination data (4 channels)
2. Time variation plots (WWV/CHU timing errors)
3. Detection timeline (signal present/absent per minute)
4. Differential delay plots (WWV-WWVH propagation)

**Tier 2B (Nice to have)**:
1. 10 Hz carrier spectrograms
2. Signal power timelines
3. Quality grade heatmaps
4. Gap analysis

**Which order makes most sense for your studies?**

### 3. **Spectrogram Implementation**

**Options:**

A. **Real-time generation (on-demand)**
   - Generate when user requests
   - Cache for 1 hour
   - Pro: Always fresh
   - Con: Slow first load

B. **Batch generation (hourly cron)**
   - Generate every hour automatically
   - Serve cached images
   - Pro: Fast loading
   - Con: Not quite real-time

C. **Hybrid**
   - Generate hourly for completed hours
   - On-demand for current hour
   - Pro: Best of both
   - Con: More complex

**Which approach?**

### 4. **API Versioning Strategy**

**Option A: Start with /api/v1/ now**
- Future-proof from day 1
- Slight overhead initially
- **Recommended**

**Option B: Use /api/ now, version later**
- Simpler initially
- Breaking changes harder later
- Not recommended

**Preference?**

### 5. **Mobile/Responsive Design**

**Required or desktop-only?**
- Desktop-only (simpler, faster to implement)
- Responsive design (works on tablets/phones)
- Mobile-first (prioritize mobile experience)

**Your typical use case?**

---

## Analytics Recommendations Based on Science Goals

### For Ionospheric Studies

**Most Important:**
1. **Differential Delay (WWV-WWVH)** - Direct measure of propagation path difference
2. **WWV/WWVH Detection Rates** - Understand which station is receivable when
3. **Time-of-Day Analysis** - How propagation varies diurnally
4. **Spectrograms** - Identify Doppler shifts and multipath

**Visualization priority:**
```
1. Differential delay vs time plot (4 WWV channels)
2. WWV vs WWVH detection timeline (stacked)
3. SNR comparison (WWV vs WWVH)
4. Spectrogram (10 Hz carrier showing Doppler)
```

### For Timing Accuracy Studies

**Most Important:**
1. **Time_snap Status** - Is it established, how confident, how old
2. **WWV Timing Errors** - Minute-to-minute variations
3. **Quality Grades** - Data integrity for analysis
4. **Gap Timeline** - Identify missing data

**Visualization priority:**
```
1. Time_snap confidence over time
2. WWV timing error distribution (histogram)
3. Quality grade timeline (color-coded)
4. Gap events (scatter plot)
```

### For System Operations

**Most Important:**
1. **Recorder Status** - Is it running, any errors
2. **Channel Health** - All 9 channels active
3. **Disk Space** - Not running out of space
4. **Upload Status** - Data getting to repository

**Dashboard priority:**
```
1. System status (single screen overview)
2. Error log viewer
3. Upload queue monitor
4. Disk usage trend
```

---

## Recommended First Steps

### Week 1: System Health
```
✅ Delete legacy backup files
✅ Create /api/v1/system/status endpoint
✅ Build system status dashboard
✅ Add disk usage monitoring
✅ Wire up error log display
```

**Deliverable:** Working system health dashboard

### Week 2: Quality & Timing
```
✅ Create /api/v1/quality/* endpoints
✅ Create /api/v1/timing/* endpoints
✅ Update timing-dashboard.html to new API
✅ Add WWV/WWVH discrimination charts
✅ Add differential delay plots
```

**Deliverable:** Enhanced timing dashboard with WWV/WWVH data

### Week 3: Spectrograms & Detection Timeline
```
✅ Implement spectrogram generation
✅ Create /api/v1/data/spectrograms endpoint
✅ Build spectrogram viewer dashboard
✅ Implement detection timeline API
✅ Build detection timeline visualization
```

**Deliverable:** Spectrogram viewer + detection timeline

### Week 4: Upload & Polish
```
✅ Wire up UploadManager to API
✅ Create /api/v1/upload/* endpoints
✅ Build upload monitoring dashboard
✅ Add signal power API & charts
✅ Performance optimization
```

**Deliverable:** Complete monitoring suite

---

## Quick Wins (Do These First)

### 1. Delete Legacy Files (5 minutes)
```bash
cd web-ui
rm -f *.OLD *.backup *.backup-*
git commit -am "Clean up legacy backup files"
```

### 2. Add Disk Usage to Existing Dashboard (30 minutes)
```javascript
// Add to existing /api/monitoring/station-info endpoint
const diskUsage = await getDiskUsage(dataRoot);
response.disk = {
  total_gb: diskUsage.total / (1024**3),
  used_gb: diskUsage.used / (1024**3),
  free_gb: diskUsage.free / (1024**3),
  percent_used: (diskUsage.used / diskUsage.total * 100)
};
```

### 3. Add Upload Status (if Function 6 exists) (1 hour)
```javascript
// New endpoint: /api/monitoring/upload-status
app.get('/api/monitoring/upload-status', async (req, res) => {
  const uploadManager = getUploadManager();
  const status = {
    pending: uploadManager.get_pending_count(),
    queue: uploadManager.get_queue_status(),
    recent: uploadManager.get_recent_uploads(10)
  };
  res.json(status);
});
```

### 4. Show Recent Errors (1 hour)
```javascript
// Read daemon logs and extract recent errors
const errors = await getRecentErrors('/var/log/signal-recorder.log', 10);
response.recent_errors = errors;
```

**Result:** Significantly improved operational visibility with minimal effort

---

## Decision Matrix

| Feature | Effort | Value | Priority | Implement |
|---------|--------|-------|----------|-----------|
| Delete legacy files | Low | Medium | High | ✅ Week 1 |
| System status API | Medium | High | High | ✅ Week 1 |
| Disk usage monitor | Low | High | High | ✅ Week 1 |
| Error log display | Low | High | High | ✅ Week 1 |
| Upload status | Low | High | High | ✅ Week 1 |
| WWV/WWVH discrimination | Medium | High | High | ✅ Week 2 |
| Differential delay plots | Medium | High | High | ✅ Week 2 |
| Detection timeline | Medium | Medium | Medium | ✅ Week 3 |
| Spectrograms | High | Medium | Medium | ✅ Week 3 |
| Signal power plots | Medium | Low | Low | ✅ Week 4 |
| Multi-day trending | High | Medium | Low | Later |
| Automated reports | High | Low | Low | Later |

---

## Questions for Next Session

1. **File cleanup approved?** Can we delete the `.OLD` and `.backup` files?
2. **Analytics priority?** Which visualizations are most valuable for your studies?
3. **Spectrogram approach?** Real-time, batch, or hybrid generation?
4. **Mobile support?** Required or desktop-only OK?
5. **simple-server.js?** Still needed or can we archive it?

Once we have these answers, we can start implementing immediately!
