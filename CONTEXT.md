# HF Time Standard - System Context

**Last Updated:** 2025-12-22

## System Architecture

### Core Services
- **`timestd-core-recorder`**: Receives RTP streams from `radiod`, writes Digital RF archives
- **`timestd-analytics`**: 9 Phase 2 processes + fusion engine for timing analysis
- **`timestd-web-ui`**: Node.js monitoring server serving real-time dashboards

### Data Flow
```
radiod → RTP/F32 → core-recorder → Digital RF → analytics → timing CSVs → web-ui
```

## Recent Changes (2025-12-22)

### Channel Management Fix
**Problem:** Client was creating 60+ duplicate channels on each restart  
**Root Cause:** Manually calling `ensure_channel()` instead of using `ManagedStream`  
**Solution:** Refactored `StreamRecorderV2` to use `ka9q-python`'s `ManagedStream` class

**Key Files:**
- [`stream_recorder_v2.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/stream_recorder_v2.py): Now uses `ManagedStream` for all channel lifecycle
- [`core_recorder_v2.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/core_recorder_v2.py): Removed duplicate `start()` calls

**Critical Principle:** Client should ONLY use `ManagedStream` - never call `ensure_channel()` directly or manage channels manually.

## Known Issues for Next Session

### Timing Discrepancy
**Observation:** Inconsistency between what `chrony` reports and what `timing.html` displays

**Relevant Files:**
- **Chrony Integration**: [`phase2_analytics_service.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py) reads chrony via `chronyc tracking`
- **Timing Display**: [`timing.html`](file:///home/mjh/git/hf-timestd/web-ui/timing.html) shows D_clock from Phase 2 analytics
- **Fusion Engine**: [`multi_broadcast_fusion.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py) combines 17 broadcasts → UTC(NIST)
- **API**: [`monitoring-server-v3.js`](file:///home/mjh/git/hf-timestd/web-ui/monitoring-server-v3.js) serves timing data

**Data Sources:**
- **Chrony**: System clock offset from NTP sources (typically GPS/PPS)
- **D_clock**: HF-derived UTC(NIST) offset from Phase 2 analytics
- **Fusion**: Multi-broadcast Kalman filter output at `/var/lib/timestd/phase2/fusion/fused_d_clock.csv`

**Investigation Path:**
1. Compare chrony offset vs latest D_clock from fusion CSV
2. Check if web UI is reading stale data or wrong CSV
3. Verify Phase 2 analytics are writing current data
4. Check if chrony integration is reading correct metrics

## Key Configuration
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data Root**: `/var/lib/timestd/`
- **Logs**: `journalctl -u timestd-core-recorder` / `timestd-analytics` / `timestd-web-ui`

## Channel Specifications (config.toml)
9 channels: 2.5, 5, 10, 15, 20, 25 MHz (WWV/WWVH) + 3.33, 7.85, 14.67 MHz (CHU)  
All use: `preset=iq`, `sample_rate=20000`, `agc=0`, `gain=0`, `encoding=F32`

## Important Patterns
- **Never** manually manage `radiod` channels - use `ManagedStream`
- **Never** pass `destination` parameter - let `radiod` assign from config
- **Always** let `ka9q-python` handle channel lifecycle (discovery, creation, restoration)
