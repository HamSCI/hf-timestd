# GRAPE Configuration File Explained

This document provides a detailed explanation of the GRAPE configuration file (`grape-production.toml`), section by section.

## 💡 **Easier Alternative: Web-Based Configuration UI**

**Recommended for most users:** Instead of manually editing this TOML file, consider using the **simplified web-based configuration interface**.

**Quick Start:**
```bash
cd web-ui
npm install
npm start
```

**Access:** http://localhost:3000 (login: admin/admin)

**Benefits:**
- ✅ **Guided setup** with validation and helpful tooltips
- ✅ **Channel presets** for WWV/CHU frequencies
- ✅ **Auto-generates** correct TOML format
- ✅ **Save directly** to signal-recorder config directory
- ✅ **Real-time validation** prevents configuration errors

**Workflow:**
1. Enter your station details (callsign, grid square)
2. Select channels to monitor (WWV/CHU presets available)
3. Configure data directories and PSWS settings
4. Click "Save to Config Directory" → Done!

---

## Overview

The configuration file controls three main processes:
1. **Recording** - Continuous capture of WWV/CHU signals from ka9q-radio
2. **Processing** - Daily conversion to GRAPE format (10 Hz Digital RF)
3. **Uploading** - Automated transfer to HamSCI PSWS server

**For most users:** Use the web UI above instead of manual editing. Continue reading below only if you need to understand the configuration format or make advanced customizations.

---

## Section 1: Station Information

```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"
description = "GRAPE station with RX888 and ka9q-radio"
```

**Purpose:** Identifies your station in the GRAPE network.

**Fields:**
- `callsign` - Your amateur radio callsign (used in filenames and metadata)
- `grid_square` - Maidenhead grid locator (for geolocation)
- `description` - Human-readable station description

**Action Required:** Update with your actual callsign and grid square.

---

## Section 2: ka9q-radio Configuration

```toml
[ka9q]
status_address = "bee1-hf-status.local"
data_address = "239.41.204.101"
data_port = 5004
```

**Purpose:** Tells the recorder where to find ka9q-radio streams.

**Fields:**
- `status_address` - mDNS name for ka9q-radio status (used by `control` utility)
- `data_address` - Multicast address where GRAPE channels are broadcast
- `data_port` - Port for RTP data stream (typically 5004)

**How It Works:**
- The system uses `control` utility to discover active channels
- All WWV/CHU channels are on the same multicast address (239.41.204.101)
- Different frequencies are distinguished by SSRC (Synchronization Source Identifier)

**No longer needed**: The signal-recorder now uses **direct RTP reception** instead of pcmrecord. The recording happens in real-time with scipy-based resampling.

**Action Required:** Update `status_address` to match your radiod configuration.

---

## Section 3: Recorder Configuration

```toml
[recorder]
data_dir = "/mnt/grape-data"
sample_format = "float32"
channels = 2
compress = true
compression_format = "wavpack"
organize_by_date = true
organize_by_frequency = true
continuous = true
file_duration_minutes = 1
```

**Purpose:** Controls how recordings are captured and stored.

**Fields:**

### Storage
- `data_dir` - Base directory for all recordings (needs lots of space!)
- `organize_by_date` - Create subdirectories by date (YYYYMMDD)
- `organize_by_frequency` - Create subdirectories by frequency

**Directory Structure:**
```
/mnt/grape-data/
├── 20241024/
│   ├── 2500000/
│   │   ├── 2500000k2024-10-24T00:00:00.0Z.wav
│   │   ├── 2500000k2024-10-24T00:01:00.0Z.wav
│   │   └── ...
│   ├── 5000000/
│   └── ...
└── 20241025/
```

### Format
- `sample_format` - Audio sample format (`float32` for IQ data from pcmrecord)
- `channels` - Number of channels (2 for IQ: I and Q)

### Compression
- `compress` - Enable compression (saves ~50% disk space)
- `compression_format` - Compression codec (`wavpack` or `flac`)

**Compression Details:**
- **Uncompressed:** ~3.7 MB per minute per channel → ~5.3 GB/day for 9 channels
- **Wavpack compressed:** ~1.8 MB per minute per channel → ~2.6 GB/day for 9 channels

### Recording Schedule
- `continuous` - Record 24/7 without gaps
- `file_duration_minutes` - Length of each file (1 minute matches GRAPE requirements)

**Why 1-minute files?**
- GRAPE processing expects 1440 files per day (24 hours × 60 minutes)
- Easier to identify and repair gaps
- Matches wsprdaemon's approach

**Action Required:** 
- Set `data_dir` to a location with sufficient space (plan for ~100 GB/month)
- Ensure the directory exists and is writable

---

## Section 4: Channel Configuration

```toml
[[recorder.channels]]
ssrc = 2500000
frequency_hz = 2500000
description = "WWV 2.5 MHz"
processor = "grape"
enabled = true
```

**Purpose:** Defines which channels to record and how to process them.

**Note:** This section repeats 9 times, once for each WWV/CHU frequency.

**Fields:**
- `ssrc` - Synchronization Source ID from ka9q-radio (discovered via `control`)
- `frequency_hz` - Frequency in Hertz (used for organization and metadata)
- `description` - Human-readable channel name
- `processor` - Which processor to use (`grape` for GRAPE processing)
- `enabled` - Whether to record this channel

**SSRC = Frequency Pattern:**
In your ka9q-radio setup, the SSRC equals the frequency in Hz:
- SSRC 2500000 = 2.5 MHz
- SSRC 10000000 = 10 MHz
- etc.

**WWV vs CHU:**
- **WWV** (NIST, Colorado): 2.5, 5, 10, 15, 20, 25 MHz
- **CHU** (Canada): 3.33, 7.85, 14.67 MHz

**Action Required:** None if all 9 frequencies are desired. Disable specific channels by setting `enabled = false`.

---

## Section 5: GRAPE Processor Configuration

```toml
[processor.grape]
process_time = "00:05"
process_timezone = "UTC"
expected_files_per_day = 1440
max_gap_minutes = 5
repair_gaps = true
interpolate_max_minutes = 2
output_sample_rate = 10
output_format = "digital_rf"
```

**Purpose:** Controls daily processing of recordings into GRAPE format.

### Scheduling
- `process_time` - When to process (00:05 = 5 minutes after midnight)
- `process_timezone` - Timezone for processing schedule (always UTC for GRAPE)

**Why 00:05 UTC?**
- Ensures previous day's recordings are complete
- Matches wsprdaemon's schedule
- Allows time for any delayed files to arrive

### Validation
- `expected_files_per_day` - How many 1-minute files expected (1440 = 24 × 60)
- `max_gap_minutes` - Maximum acceptable gap before alerting (5 minutes)

### Gap Repair
- `repair_gaps` - Attempt to fix missing data
- `interpolate_max_minutes` - Interpolate gaps up to this length (2 minutes)

**Gap Repair Strategy:**
- **Gaps ≤ 2 minutes:** Linear interpolation between adjacent samples
- **Gaps > 2 minutes:** Zero-fill (silence)
- **Gaps > 5 minutes:** Log warning, continue processing

### Output Format
- `output_sample_rate` - GRAPE requires 10 Hz (downsampled from 16 kHz)
- `output_format` - `digital_rf` (HDF5-based format for radio data)

### Digital RF Parameters
```toml
digital_rf_sample_rate_numerator = 10
digital_rf_sample_rate_denominator = 1
digital_rf_subdir_cadence_seconds = 3600
digital_rf_file_cadence_seconds = 3600
digital_rf_compression_level = 1
```

**Digital RF Details:**
- Sample rate: 10/1 = 10 Hz (exactly)
- Subdirectory every 3600 seconds (1 hour)
- New file every 3600 seconds (1 hour)
- Compression level 1 (light, fast)

**File Organization:**
```
/mnt/grape-data/processed/20241024/2500000/
├── rf@1729728000.000.h5  # 00:00-01:00 UTC
├── rf@1729731600.000.h5  # 01:00-02:00 UTC
└── ...
```

### Intermediate Files
```toml
keep_wavpack = true
keep_24hour_wav = false
keep_resampled_wav = false
```

**Processing Pipeline:**
1. 1440 × 1-minute wavpack files → **kept** (original recordings)
2. Concatenate → 1 × 24-hour WAV → **deleted** (temporary)
3. Resample to 10 Hz → 24-hour 10Hz WAV → **deleted** (temporary)
4. Convert to Digital RF → **kept** (final output)

**Action Required:** Adjust `keep_*` settings based on disk space and debugging needs.

---

## Section 6: Upload Configuration

```toml
[uploader]
upload_enabled = true
upload_time = "00:30"
upload_timezone = "UTC"
protocol = "rsync"
max_retries = 5
retry_delay_seconds = 300
exponential_backoff = true
queue_dir = "/mnt/grape-data/upload_queue"
max_queue_size_gb = 100
```

**Purpose:** Automated upload to HamSCI PSWS server.

### Scheduling
- `upload_enabled` - Enable/disable uploads
- `upload_time` - When to upload (00:30 = after processing completes)
- `upload_timezone` - Timezone (UTC)

**Why 00:30 UTC?**
- Processing starts at 00:05, typically completes by 00:20
- Gives 10-minute buffer for processing
- Spreads upload load across GRAPE network

### Retry Logic
- `max_retries` - Try up to 5 times before giving up
- `retry_delay_seconds` - Wait 5 minutes between retries
- `exponential_backoff` - Double delay after each failure (5m, 10m, 20m, 40m, 80m)

**Retry Example:**
```
Attempt 1: fails → wait 5 minutes
Attempt 2: fails → wait 10 minutes
Attempt 3: fails → wait 20 minutes
Attempt 4: fails → wait 40 minutes
Attempt 5: fails → give up, add to queue
```

### Queue Management
- `queue_dir` - Where to store failed uploads
- `max_queue_size_gb` - Maximum queue size before alerting

**Queue Behavior:**
- Failed uploads are queued for next cycle
- Next upload attempts queued files first
- Prevents data loss during network outages

---

## Section 7: Rsync Configuration

```toml
[uploader.rsync]
host = "pswsnetwork.eng.ua.edu"
port = 22
user = "your_username"
ssh_key = "/home/wsprdaemon/.ssh/id_rsa_psws"
remote_base_path = "/data/AC0G"
rsync_options = ["--archive", "--compress", "--partial", "--progress", "--timeout=300"]
bandwidth_limit = 0
verify_after_upload = true
delete_after_upload = false
```

**Purpose:** Rsync-specific upload settings.

### SSH Connection
- `host` - HamSCI PSWS server hostname
- `port` - SSH port (22 = standard)
- `user` - Your PSWS username
- `ssh_key` - Path to SSH private key (passwordless auth required)

**SSH Key Setup:**
```bash
# Generate key if needed
ssh-keygen -t ed25519 -f ~/.ssh/id_rsa_psws -C "GRAPE upload key"

# Copy public key to PSWS server
ssh-copy-id -i ~/.ssh/id_rsa_psws.pub your_username@pswsnetwork.eng.ua.edu
```

### Remote Path
- `remote_base_path` - Base directory on PSWS server
- Actual path: `{remote_base_path}/{YYYY}/{MM}/{DD}/`

**Example:**
```
/data/AC0G/2024/10/24/2500000/rf@1729728000.000.h5
```

### Rsync Options
- `--archive` - Preserve timestamps, permissions, etc.
- `--compress` - Compress during transfer (saves bandwidth)
- `--partial` - Keep partial files (resume interrupted transfers)
- `--progress` - Show progress (for logging)
- `--timeout=300` - Abort if no data for 5 minutes

### Bandwidth
- `bandwidth_limit` - KB/s limit (0 = unlimited)
- Set to limit impact on other network traffic

**Bandwidth Calculation:**
- ~2.6 GB/day compressed
- Over 24 hours = ~30 KB/s average
- Actual upload in ~30 minutes = ~1.5 MB/s

### Post-Upload
- `verify_after_upload` - Check file exists and size matches
- `delete_after_upload` - Delete local copy after successful upload

**Recommendation:** Keep `delete_after_upload = false` initially for safety.

**Action Required:**
- Get PSWS account credentials from HamSCI
- Set up SSH key authentication
- Update `user` and `remote_base_path`

---

## Section 8: Logging

```toml
[logging]
level = "INFO"
log_dir = "/var/log/signal-recorder"
log_file = "grape-recorder.log"
rotate_size_mb = 100
rotate_count = 10
console_output = true
console_level = "INFO"
```

**Purpose:** Control logging verbosity and storage.

### Log Levels
- `DEBUG` - Everything (very verbose, for troubleshooting)
- `INFO` - Normal operations (recommended)
- `WARNING` - Problems that don't stop operation
- `ERROR` - Failures that stop operation

### Log Files
- `log_dir` - Where to store logs
- `log_file` - Log filename
- Full path: `/var/log/signal-recorder/grape-recorder.log`

### Rotation
- `rotate_size_mb` - Start new log file after 100 MB
- `rotate_count` - Keep 10 old log files

**Total log storage:** 100 MB × 10 = 1 GB maximum

### Console
- `console_output` - Also print to terminal
- `console_level` - Console verbosity (can differ from file)

**Action Required:** Ensure log directory exists and is writable.

---

## Section 9: Monitoring

```toml
[monitoring]
status_file = "/var/run/signal-recorder/status.json"
update_interval_seconds = 60
enable_metrics = true
metrics_file = "/var/run/signal-recorder/metrics.json"
alert_on_recording_failure = true
alert_on_processing_failure = true
alert_on_upload_failure = true
```

**Purpose:** Health monitoring and alerting.

### Status File
- `status_file` - JSON file with current status
- `update_interval_seconds` - How often to update (60 = every minute)

**Status File Contents:**
```json
{
  "recording": {
    "active": true,
    "channels": 9,
    "last_file": "2024-10-24T12:34:00Z",
    "files_today": 756
  },
  "processing": {
    "last_run": "2024-10-24T00:05:00Z",
    "status": "success",
    "files_processed": 12960
  },
  "upload": {
    "last_upload": "2024-10-24T00:30:00Z",
    "status": "success",
    "bytes_uploaded": 2734567890
  }
}
```

### Metrics
- `enable_metrics` - Collect performance metrics
- `metrics_file` - JSON file with metrics

**Metrics Include:**
- Recording rate (files/minute)
- Disk usage
- Processing time
- Upload bandwidth
- Error counts

### Alerts
- `alert_on_recording_failure` - Alert if recording stops
- `alert_on_processing_failure` - Alert if processing fails
- `alert_on_upload_failure` - Alert if upload fails

**Alert Methods (commented out in example):**
```toml
# alert_email = "admin@example.com"
# alert_webhook = "https://example.com/webhook"
```

**Action Required:** Configure alerting if desired.

---

## 🎯 **Current Implementation: Direct RTP + Scipy**

The signal-recorder now uses **direct RTP reception with scipy-based resampling** instead of external tools:

### **Recording Pipeline:**
1. **Direct RTP Reception** - Receives IQ samples directly from ka9q-radio multicast
2. **Scipy Resampling** - High-quality 12kHz → 10Hz decimation with anti-aliasing filter
3. **Real-time Processing** - Immediate conversion to Digital RF format
4. **Channel Management** - Automatic radiod channel creation

### **Technical Details:**
- **No pcmrecord dependency** - Direct socket-based RTP reception
- **8th-order Butterworth filter** - Anti-aliasing before decimation
- **UTC-aligned timestamps** - Precise RTP timestamp-based sample placement
- **Digital RF output** - Compatible with HamSCI PSWS server requirements

**Quick Start:**
```bash
# 1. Start the web UI with pnpm (recommended)
cd web-ui
pnpm install  # Faster than npm
pnpm start

# 2. Access http://localhost:3000 (admin/admin)
# 3. Create station configuration through guided interface
# 4. Add channels using presets or custom setup
# 5. Configure PSWS settings if participating
# 6. Click "Save to Config Directory"
# 7. Configuration saved as config/grape-{station_id}.toml
```

**Manual editing approach** (for advanced users):

Before running the recorder:

- [ ] **Use web UI** (recommended) or update `[station]` with your callsign and grid square
- [ ] **Use web UI** (recommended) or update `[ka9q]` status_address to match your radiod
- [ ] **Use web UI** (recommended) or create data directory: `sudo mkdir -p /mnt/grape-data`
- [ ] **Use web UI** (recommended) or set permissions: `sudo chown wsprdaemon:wsprdaemon /mnt/grape-data`
- [ ] **Use web UI** (recommended) or verify disk space: `df -h /mnt/grape-data` (need 100+ GB)
- [ ] **Use web UI** (recommended) or update `[uploader.rsync]` with PSWS credentials
- [ ] **Use web UI** (recommended) or set up SSH key for PSWS server
- [ ] **Use web UI** (recommended) or create log directory: `sudo mkdir -p /var/log/signal-recorder`
- [ ] **Use web UI** (recommended) or set log permissions: `sudo chown wsprdaemon:wsprdaemon /var/log/signal-recorder`
- [ ] **Test configuration** (if manually edited): `python3.11 -c "import toml; toml.load('config/grape-your-config.toml')"`

---

## Disk Space Planning

### Per Day (9 channels)
- **Raw recordings (uncompressed):** ~5.3 GB
- **Raw recordings (wavpack):** ~2.6 GB
- **Digital RF output:** ~0.5 GB
- **Total (keeping wavpack + Digital RF):** ~3.1 GB/day

### Per Month
- **30 days:** ~93 GB
- **With 20% buffer:** ~112 GB

### Recommended
- **Minimum:** 200 GB dedicated storage
- **Comfortable:** 500 GB - 1 TB
- **Archive everything:** 2+ TB

---

## Performance Considerations

### CPU Usage
- **Recording:** Minimal (pcmrecord is efficient)
- **Processing:** Moderate (sox resampling, Digital RF conversion)
- **Upload:** Minimal (rsync)

**Recommended:** 2+ CPU cores, one dedicated to recording

### Memory Usage
- **Recording:** ~100 MB per channel = ~1 GB total
- **Processing:** ~2 GB (loading 24-hour files)
- **Upload:** ~100 MB

**Recommended:** 4+ GB RAM

### Network
- **Recording:** Multicast on local network (no internet needed)
- **Upload:** ~2.6 GB/day = ~30 KB/s average

**Recommended:** Stable internet with 1+ Mbps upload

---

## Troubleshooting

### Recording not starting
1. Check ka9q-radio is running: `systemctl status radiod`
2. Verify multicast address: `signal-recorder discover --radiod bee1-hf-status.local`
3. Test pcmrecord manually: `timeout 10 pcmrecord -d /tmp 239.41.204.101`
4. Check permissions on data directory

### Processing fails
1. Check disk space: `df -h /mnt/grape-data`
2. Verify all dependencies installed: `sox`, `wavpack`, `digital_rf`
3. Check logs: `tail -f /var/log/signal-recorder/grape-recorder.log`
4. Test processing manually on small dataset

### Upload fails
1. Test SSH connection: `ssh -i ~/.ssh/id_rsa_psws user@pswsnetwork.eng.ua.edu`
2. Verify remote path exists
3. Check network connectivity
4. Review upload logs for specific errors

---

## Next Steps

After understanding the configuration:
1. Customize the file for your station
2. Test recording for 10 minutes
3. Test processing on the 10-minute dataset
4. Set up systemd service for automatic operation
5. Monitor for 24 hours to verify full cycle

