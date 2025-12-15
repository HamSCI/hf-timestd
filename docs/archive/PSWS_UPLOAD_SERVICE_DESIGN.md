# PSWS Upload Service Design

**Date:** 2025-11-20  
**Status:** Design Phase  
**Priority:** High - Required for HamSCI GRAPE network participation

## Executive Summary

Design for automated SFTP upload service to transfer Digital RF files to HamSCI PSWS network (`pswsnetwork.eng.ua.edu`). Based on wsprdaemon proven implementation, adapted for GRAPE Signal Recorder architecture.

## Current State

✅ **DRF Writer:** Producing wsprdaemon-compatible Digital RF HDF5 files  
✅ **SSH Autologin:** Already configured for pswsnetwork.eng.ua.edu  
❌ **Upload Service:** Not implemented  
❌ **Monitoring:** No upload tracking  
❌ **Cleanup:** No retention policy

## Design Decisions (Based on wsprdaemon Analysis)

### 1. Transport: SFTP (SSH File Transfer Protocol)

**Why SFTP (not rsync):**
- Wsprdaemon uses `sftp` command with batch mode
- SSH autologin already configured on this system
- PSWS server expects SFTP protocol
- Bandwidth limiting via `-l` flag (kbps)
- Atomic directory creation for upload triggering

**Command Pattern:**
```bash
# Username from config: station_id = "S000171" (not callsign)
sftp -v -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 \
     -l 100 -b commands.txt \
     S000171@pswsnetwork.eng.ua.edu
```

**Note:** PSWS allows SFTP only (no SSH shell access)

**Batch Commands (commands.txt):**
```
put -r <local_drf_dir>
mkdir c<site>_#<instrument>_#<timestamp>
```

### 2. Upload Cadence: Once Daily at 00:05 UTC

**Rationale:**
- Match wsprdaemon pattern (proven reliable)
- Upload complete 24-hour dataset after UTC day ends
- Single large batch more efficient than many small uploads
- PSWS expects daily delivery pattern

**Implementation:**
- Daemon wakes at 00:05 UTC daily
- Collects all DRF files from previous UTC day
- Single SFTP batch upload
- Retries on failure with exponential backoff
- Creates trigger directory after successful upload

### 3. Directory Structure

**Local (source):**
```
analytics/{channel}/digital_rf/
├── WWV_10_MHz/
│   ├── 2025-11-20T00-00-00/
│   │   └── rf_data.h5
│   ├── 2025-11-20T01-00-00/
│   │   └── rf_data.h5
```

**Remote (PSWS):**
```
~/S000171/
├── WWV_10_MHz/           # Uploaded DRF directories
│   ├── 2025-11-20T00-00-00/
│   └── 2025-11-20T01-00-00/
├── cS000171_#1_#2025-11-20T00-05/  # Trigger directory (signals completion)
```

**Trigger Directory Naming:**
- `c{station_id}_#{instrument_id}_#{upload_timestamp}`
- Example: `cS000171_#1_#2025-11-21T00-05` (uploaded at 00:05 UTC, day after data)
- Created AFTER all DRF files uploaded
- Signals PSWS that upload batch is complete

### 4. Upload State Management

**State File:** `state/upload_state.json`
```json
{
  "last_upload_time": "2025-11-20T14:25:00Z",
  "pending_files": [
    "analytics/WWV_10_MHz/digital_rf/2025-11-20T14-00-00"
  ],
  "upload_history": [
    {
      "timestamp": "2025-11-20T14:25:00Z",
      "files_count": 5,
      "bytes_transferred": 1048576,
      "duration_seconds": 12.3,
      "status": "success"
    }
  ],
  "failed_uploads": [],
  "next_check_time": "2025-11-20T14:30:00Z"
}
```

### 5. Retention Policy

**Local Cleanup Strategy:**
- Keep 7 days of DRF files locally after successful upload
- Keep 30 days of 16 kHz NPZ archives (reprocessability)
- Keep 90 days of discrimination CSVs (small size)
- Purge on disk space pressure (configurable threshold)

**Implementation:**
```python
def cleanup_uploaded_files(channel: str, retention_days: int = 7):
    """Delete DRF files older than retention_days if successfully uploaded."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    drf_dir = paths.get_digital_rf_dir(channel)
    
    for dir_path in drf_dir.glob("20*"):
        if is_uploaded(dir_path) and get_mtime(dir_path) < cutoff:
            shutil.rmtree(dir_path)
            logger.info(f"Purged {dir_path} (uploaded {days_ago} days ago)")
```

### 6. Monitoring & Alerts

**Metrics to Track:**
- Upload success rate (%)
- Average upload duration (seconds)
- Bytes transferred per upload
- Failed upload count (with retry count)
- Queue depth (pending files)
- Disk space used vs available

**Alert Conditions:**
- ❌ **Critical:** Upload failed 3+ times (SSH issue?)
- ⚠️ **Warning:** Upload delayed >30 minutes (network issue?)
- ⚠️ **Warning:** Disk >85% full (cleanup needed)
- ℹ️ **Info:** Large upload queue (>100 files)

**Web UI Integration:**
- Add `/uploads` page showing status table
- Real-time upload progress (if active)
- Historical success rate chart
- Failed uploads list with retry button
- Manual trigger button for testing

### 7. Error Handling & Retries

**Retry Strategy (Exponential Backoff):**
```
Attempt 1: Immediate
Attempt 2: +1 min
Attempt 3: +5 min
Attempt 4: +15 min
Attempt 5: +60 min
After 5 failures: Alert and pause (manual intervention)
```

**Error Categories:**
- **Network:** Retry with backoff
- **Authentication:** Alert immediately (SSH keys broken)
- **Disk Full (local):** Trigger emergency cleanup
- **Disk Full (remote):** Alert PSWS admins
- **Malformed DRF:** Skip file, log error

### 8. Configuration (TOML)

```toml
[uploader]
enabled = false                          # Master switch
station_id = "S000171"                   # PSWS login username (from HamSCI)
instrument_id = "1"                      # Device number (1-9)
psws_server = "pswsnetwork.eng.ua.edu"  # Upload target
upload_time_utc = "00:05"                # Daily upload time (HH:MM UTC)
bandwidth_limit_kbps = 100               # SFTP rate limit (wsprdaemon default)
retention_days = 7                       # Local DRF cleanup
max_retry_attempts = 5                   # Before alerting
disk_space_threshold_pct = 85            # Trigger cleanup
```

**Note:** `station_id` is your PSWS login username (e.g., "S000171"), NOT your callsign.

### 9. Service Architecture

**Option A: Separate Upload Service (Recommended)**
```
Core Recorder → NPZ archives (16 kHz)
     ↓
Analytics Service → NPZ decimated (10 Hz) + CSVs
     ↓
DRF Writer → Digital RF HDF5 files
     ↓
Upload Service → SFTP to PSWS → Cleanup
```

**Benefits:**
- Independent restart without stopping recording
- Retry logic doesn't block DRF generation
- Can reprocess/re-upload historical data
- Simpler testing (mock SFTP server)

**Option B: Integrated with DRF Writer**
- Simpler deployment (one fewer service)
- Risk: Upload blocking DRF generation
- Not recommended for production

### 10. Implementation Plan

**Phase 1: Core Upload (1-2 days)**
- [ ] Create `psws_upload_service.py`
- [ ] SFTP batch upload function
- [ ] State file management
- [ ] Basic retry logic
- [ ] Logging

**Phase 2: Monitoring (1 day)**
- [ ] Add `/uploads` endpoint to monitoring API
- [ ] Upload status page in Web UI
- [ ] Metrics collection
- [ ] Alert integration

**Phase 3: Cleanup & Optimization (1 day)**
- [ ] Retention policy implementation
- [ ] Disk space monitoring
- [ ] Bandwidth throttling
- [ ] Trigger directory logic

**Phase 4: Testing & Deployment (1 day)**
- [ ] Unit tests (mock SFTP)
- [ ] Integration test with PSWS staging
- [ ] Systemd service file
- [ ] Documentation

**Total Estimate:** 4-5 days

## Security Considerations

1. **SSH Key Management:**
   - Keys already configured (pre-existing autologin)
   - Ensure `~/.ssh` permissions 700
   - Private key permissions 600
   - Public key in `~/.ssh/authorized_keys` on PSWS

2. **Umask Enforcement:**
   - Set `umask 022` before SFTP upload
   - Prevents group write on PSWS (would break autologin)
   - See wsprdaemon line 239

3. **No Credentials in Code:**
   - All auth via SSH keys
   - Station ID in config file only
   - No passwords stored

## Dependencies

**Python:**
- `paramiko` (SFTP client library) OR
- `subprocess` (call system `sftp` command - simpler)

**System:**
- `openssh-client` (already installed)
- SSH keys in `~/.ssh/`
- Network connectivity to pswsnetwork.eng.ua.edu:22

## Testing Strategy

**Unit Tests:**
- Mock SFTP responses
- State file serialization
- Retry logic
- Cleanup logic

**Integration Tests:**
- Upload to PSWS staging environment
- Verify trigger directory creation
- Confirm file integrity on remote

**Manual Verification:**
```bash
# Test SFTP autologin (SFTP only - no SSH shell access)
echo "ls" | sftp S000171@pswsnetwork.eng.ua.edu

# Test SFTP batch commands
cat > /tmp/sftp_test.txt <<EOF
ls
pwd
EOF
sftp -b /tmp/sftp_test.txt S000171@pswsnetwork.eng.ua.edu

# Monitor upload service
journalctl -u grape-upload-service -f
```

**Note:** SSH shell access not available - use SFTP batch mode for all testing.

## Resolved Design Decisions

1. ✅ **Upload Cadence:** Once daily at 00:05 UTC (after UTC day ends)
2. ✅ **Station ID:** S000171 (PSWS login username from config file)
3. ✅ **Bandwidth Limit:** 100 kbps (wsprdaemon default)
4. ✅ **Protocol:** SFTP only (no SSH shell access on PSWS)
5. ✅ **Testing:** No staging server - careful production testing required
6. ✅ **Login Management:** Follow grape-utils.sh pattern (SSH key setup)

## Open Questions

1. **PSWS Instrument ID:** Confirm your assigned instrument ID (typically "1")
2. **Upload Format:** Confirm wsprdaemon-compatible DRF is correct (not enhanced metadata mode)
3. **Trigger Directory:** Confirm naming format with HamSCI coordinators

## References

- wsprdaemon implementation: `wsprdaemon/grape-utils.sh` lines 200-284
- SFTP upload logic: `wsprdaemon/upload-client-utils.sh` lines 527-579
- PSWS server: `pswsnetwork.eng.ua.edu` (line 94 grape-utils.sh)
- Digital RF format: `docs/DRF_WRITER_MODES.md`
- Wsprdaemon compatibility: `SESSION_2025-11-20_WSPRDAEMON_DRF_COMPATIBILITY.md`

---

**Next Steps:**
1. Confirm open questions with HamSCI coordinators
2. Implement Phase 1 (core upload service)
3. Test with PSWS staging (if available)
4. Deploy to production with monitoring
