# HF-TimeStd Project Context

## Current Status (2025-12-26)

### CRITICAL ISSUE: Chrony Fusion Not Updating

**The multi-broadcast fusion product to Chrony SHM has not updated in over 23 hours.**

This is the **primary objective** of the hf-timestd system - to provide UTC(NIST)-aligned time to the system clock via Chrony. This must be restored immediately.

### What's Working

- ✅ Core recorder: Running, receiving RTP streams from 9 channels
- ✅ Analytics service: Running Phase 2 analytics
- ✅ HDF5 data products: L1A, L1B, L2 files being written with quality metadata
- ✅ Web UI: FastAPI monitoring server deployed with native HDF5 support

### What's Broken

- ❌ **Chrony SHM integration**: Fusion product not updating (last update >23h ago)
- ❌ System time discipline: Not receiving UTC(NIST) corrections

## System Architecture

### Data Flow

```
ka9q-radio (radiod) 
  → RTP multicast streams
  → core-recorder (9 channels: WWV, WWVH, CHU at multiple frequencies)
  → Phase 1 analytics (tone detection, BCD timecode)
  → Phase 2 analytics (timing measurements, multi-broadcast fusion)
  → Chrony SHM (SHOULD update system clock)
```

### Key Services

1. **radiod**: ka9q-radio software-defined radio
2. **timestd-core-recorder**: Receives RTP, writes raw data, runs Phase 1
3. **timestd-analytics**: Phase 2 analytics including multi-broadcast fusion
4. **timestd-web-ui**: FastAPI monitoring server (just migrated from Node.js)

### Critical Files

- **Fusion output**: `/var/lib/timestd/phase2/science/timing/fused_clock.csv`
- **Chrony SHM**: `/dev/shm/SHM2` (should be updated by fusion component)
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Logs**: `journalctl -u timestd-analytics`

## Recent Changes (This Session)

### Completed: FastAPI Migration

- Migrated monitoring server from Node.js to Python/FastAPI
- Native HDF5 support with h5py (SWMR mode)
- Clean dashboard with Jinja2 templates
- All API endpoints working
- Deployed to production

**Files Created:**

- `web-ui/monitoring_server.py` - FastAPI server
- `web-ui/utils/hdf5_reader.py` - HDF5 reader with h5py
- `web-ui/templates/dashboard.html` - Clean dashboard
- `systemd/timestd-web-ui-fastapi.service` - Service file

## Next Session Objectives

### PRIMARY: Fix Chrony Fusion Integration

**Investigation Steps:**

1. Check if fusion CSV is being updated:

   ```bash
   ls -lh /var/lib/timestd/phase2/science/timing/fused_clock.csv
   tail -20 /var/lib/timestd/phase2/science/timing/fused_clock.csv
   ```

2. Check analytics service status:

   ```bash
   sudo systemctl status timestd-analytics
   sudo journalctl -u timestd-analytics -n 100
   ```

3. Check Chrony SHM:

   ```bash
   ls -lh /dev/shm/SHM*
   chronyc sources
   chronyc tracking
   ```

4. Verify fusion component is running:

   ```bash
   ps aux | grep fusion
   ```

**Likely Issues:**

- Analytics service not running fusion component
- Fusion component crashed/errored
- SHM permissions issue
- Configuration issue after recent changes

**Key Code Locations:**

- Fusion logic: `src/hf_timestd/analytics/multi_broadcast_fusion.py`
- SHM writer: Look for `sysv_ipc` or SHM-related code in fusion
- Service startup: `systemd/timestd-analytics.service`

## Important Context

### Station Info

- Callsign: AC0G
- Grid Square: EM38ww40pk
- Instrument ID: 172
- Mode: production
- Data Root: /var/lib/timestd

### Channels (9 total)

- SHARED_2500, SHARED_5000, SHARED_10000, SHARED_15000
- WWV_20000, WWV_25000
- CHU_3330, CHU_7850, CHU_14670

### HDF5 Schema

- L1A: Channel observables (carrier power, SNR, Doppler, tones)
- L1B: BCD timecode detections
- L2: Timing measurements with quality grades (A/B/C/D)

### Quality Metadata

All HDF5 files include:

- Quality grades: A (best) to D (worst)
- Quality flags: GOOD, MARGINAL, BAD
- Uncertainty estimates
- Confidence scores

## Commands for Next Session

```bash
# Check fusion status
tail -f /var/lib/timestd/phase2/science/timing/fused_clock.csv

# Check analytics logs
sudo journalctl -u timestd-analytics -f

# Check Chrony
chronyc sources -v
chronyc tracking

# Restart analytics if needed
sudo systemctl restart timestd-analytics
```

## Success Criteria for Next Session

- ✅ Fusion CSV updating regularly (every minute)
- ✅ Chrony SHM receiving updates
- ✅ `chronyc sources` shows TMGR (fusion) as active reference
- ✅ System clock being disciplined by UTC(NIST) via fusion
