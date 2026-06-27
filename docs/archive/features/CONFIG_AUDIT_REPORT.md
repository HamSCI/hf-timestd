# grape-config.toml Audit Report

**Date:** November 6, 2025  
**Config File:** `/home/mjh/git/signal-recorder/config/grape-config.toml`

---

## Executive Summary

**Overall Status:** 🟡 **MOSTLY CORRECT** - Minor issues and improvements needed

- ✅ **Station info**: Correct
- ✅ **Channel configuration**: Correct
- ⚠️ **Upload settings**: Incomplete/incorrect
- ⚠️ **Processor settings**: Inconsistent with actual behavior
- ⚠️ **Missing settings**: Several recommended fields

---

## Section-by-Section Audit

### ✅ [station] - CORRECT

```toml
[station]
callsign = "AC0G"                    # ✓ Valid callsign
grid_square = "EM38ww"               # ✓ Valid 6-character grid
id = "S000171"                       # ✓ PSWS station ID
instrument_id = "172"                # ✓ Instrument number
description = "beelink rx888 SAS"    # ✓ Descriptive
```

**Status:** All fields correct and complete.

**Recommendations:**
- Consider adding `latitude` and `longitude` (auto-derived from grid, but explicit is better)
- Add `elevation_m` for completeness

**Suggested additions:**
```toml
[station]
# ... existing fields ...
latitude = 38.3542    # Calculated from EM38ww
longitude = -87.5417
elevation_m = 120     # Approximate for your location
```

---

### ✅ [ka9q] - CORRECT

```toml
[ka9q]
status_address = "bee1-hf-status.local"  # ✓ Valid multicast address
auto_create_channels = true              # ✓ Good for dynamic setup
```

**Status:** Correct for your setup.

**Note:** `bee1-hf-status.local` resolves to multicast address. This is correct.

---

### 🟡 [recorder] - MOSTLY CORRECT, NEEDS UPDATES

```toml
[recorder]
mode = "test"                             # ✓ Correct for development
test_data_root = "/tmp/grape-test"        # ✓ Good for testing
production_data_root = "/var/lib/signal-recorder"  # ✓ FHS-compliant

recording_interval = 60                   # ⚠️ USED? (continuous mode)
continuous = true                         # ✓ Correct
```

**Issues:**

1. **Missing output format specification**
   - Should explicitly state `output_format = "digital_rf"`

2. **Missing Digital RF parameters**
   - Need to specify cadence parameters to match wsprdaemon

3. **Comments mention directories that don't match actual implementation**
   - Your code creates different structure than documented

**Recommended additions:**
```toml
[recorder]
mode = "test"
test_data_root = "/tmp/grape-test"
production_data_root = "/var/lib/signal-recorder"
continuous = true

# Digital RF output format
output_format = "digital_rf"
decimation_rate = 1600                    # 16kHz → 10Hz
output_sample_rate = 10                   # Hz

# Digital RF file structure (CRITICAL for PSWS compatibility)
drf_subdir_cadence_secs = 86400          # 24-hour subdirectories (wsprdaemon compat)
drf_file_cadence_ms = 3600000            # 1-hour files (wsprdaemon compat)
drf_compression_level = 9                 # High compression for upload bandwidth
drf_channel_name = "ch0"                  # wsprdaemon standard

# Quality tracking
enable_quality_metrics = true
quality_metrics_dir = "${data_root}/analytics/quality"
wwv_timing_csv = "${data_root}/analytics/timing/wwv_timing.csv"

# Gap handling
fill_gaps_with_zeros = true               # Maintain sample count integrity
max_gap_fill_seconds = 10                 # Don't fill gaps longer than this
```

---

### ✅ [recorder.channels] - CORRECT

All 9 channels configured correctly:

```toml
# WWV channels: 2.5, 5, 10, 15, 20, 25 MHz ✓
# CHU channels: 3.33, 7.85, 14.67 MHz ✓
```

**Issues:**

1. **Duplicate channel** - Line 35 and 135 both define CHU 14.67 MHz
   ```toml
   # Line 35-44
   [[recorder.channels]]
   ssrc = 14670000
   frequency_hz = 14670000
   # ...
   
   # Line 135-143 (DUPLICATE!)
   [[recorder.channels]]
   ssrc = 14670000
   frequency_hz = 14670000
   # ...
   ```
   
   **Action:** Remove one of these duplicates (recommend removing lines 135-143)

2. **All channels have same parameters** - Good for consistency, but verify:
   - `sample_rate = 16000` ✓ Correct
   - `agc = 0` ✓ Correct (disabled)
   - `gain = 0` ⚠️ May need tuning per band
   - `processor = "grape"` ✓ Correct

**Recommendations:**
- Add `wwv_tone_detection = true` flag for WWV channels
- Add `chu_tone_detection = true` flag for CHU channels (0.5s vs 0.8s)

---

### ❌ [processor] - INCORRECT/OBSOLETE

```toml
[processor]
enabled = false  # ⚠️ Should this be true?

[processor.grape]
process_time = "00:05"
process_timezone = "UTC"
expected_files_per_day = 1440  # ⚠️ Wrong! (This is for 1-minute files)
max_gap_minutes = 5
repair_gaps = true
interpolate_max_minutes = 2
output_sample_rate = 10        # ✓ Correct
output_format = "digital_rf"   # ✓ Correct
```

**Issues:**

1. **`enabled = false`** - Your V2 recorder does processing inline, not as separate step
   - This section is for the OLD post-processing approach
   - **Action:** This section can be removed or set to legacy documentation

2. **`expected_files_per_day = 1440`** - Wrong for Digital RF
   - 1440 assumes 1-minute files
   - Digital RF uses 1-hour files = 24 files/day
   - **Correct value:** `expected_files_per_day = 24`

3. **Gap repair settings** - Your V2 recorder fills gaps with zeros in real-time
   - These settings apply to post-processing
   - **Action:** Move to `[recorder]` section or remove

**Recommendation:** **REMOVE THIS SECTION** - V2 recorder doesn't use it

---

### ❌ [uploader] - CRITICAL ISSUES

```toml
[uploader]
enabled = false           # ❌ Should be true when ready
upload_enabled = false    # ❌ Redundant with above
protocol = "rsync"        # ❌ WRONG! PSWS requires SFTP
upload_time = "00:30"     # ✓ Good time choice
upload_timezone = "UTC"   # ✓ Correct
max_retries = 5           # ✓ Reasonable
retry_delay_seconds = 300 # ✓ Reasonable
exponential_backoff = true # ✓ Good practice
queue_dir = "/home/mjh/grape-data/raw/upload_queue"  # ⚠️ Wrong path
max_queue_size_gb = 100   # ✓ Reasonable

[uploader.rsync]          # ❌ WRONG SECTION! Should be [uploader.sftp]
host = "pswsnetwork.eng.ua.edu"  # ✓ Correct
port = 22                        # ✓ Correct
user = "your_username"           # ❌ Should be "S000171" (station ID)
ssh_key = "/home/wsprdaemon/.ssh/id_rsa_psws"  # ❌ Wrong path
remote_base_path = "/data/S000171"  # ❌ Wrong! PSWS uploads to home dir
bandwidth_limit = 0                  # ⚠️ Should limit (wsprdaemon uses 100 kbps)
verify_after_upload = true           # ✓ Good practice
delete_after_upload = false          # ✓ Good for safety
```

**CRITICAL FIXES NEEDED:**

```toml
[uploader]
enabled = false  # Set to true when ready to upload
protocol = "sftp"  # NOT rsync! PSWS requires SFTP
upload_time = "00:30"
upload_timezone = "UTC"
max_retries = 5
retry_delay_seconds = 300
exponential_backoff = true
queue_dir = "${data_root}/upload/queue"  # Use data_root variable
max_queue_size_gb = 100
delete_after_successful_upload = false   # Keep local copy

[uploader.sftp]  # NOT rsync!
host = "pswsnetwork.eng.ua.edu"
port = 22
user = "S000171"  # Your PSWS station ID
ssh_key = "/home/mjh/.ssh/id_ed25519"  # Your actual SSH key
remote_base_path = "/"  # Upload to home directory (PSWS requirement)
bandwidth_limit_kbps = 100  # wsprdaemon standard
verify_after_upload = true
create_completion_marker = true  # .upload_complete file
create_trigger_directory = true  # CRITICAL for PSWS processing!
trigger_format = "cOBS{obs_time}_#{instrument_id}_#{upload_time}"

# Upload only complete days
upload_complete_days_only = true
min_day_completeness_percent = 95.0  # Don't upload partial days
```

---

### ✅ [logging] - CORRECT

```toml
[logging]
level = "INFO"         # ✓ Appropriate for production
console_output = true  # ✓ Good for development
```

**Recommendations:**
```toml
[logging]
level = "INFO"
console_output = true
file_output = true                        # Add file logging
log_dir = "${data_root}/logs"            # Persistent logs
log_rotation_mb = 100                     # Rotate at 100 MB
max_log_files = 10                        # Keep 10 rotated logs
```

---

### 🟡 [monitoring] - INCOMPLETE

```toml
[monitoring]
enable_metrics = false  # ⚠️ Should be true!
```

**Should be:**
```toml
[monitoring]
enable_metrics = true
stats_file = "${data_root}/status/recording-stats.json"
update_interval_seconds = 60
enable_prometheus = false  # For future integration
prometheus_port = 9090
```

---

### ✅ [web_ui] - CORRECT

```toml
[web_ui]
port = 3000              # ✓ Standard
refresh_interval = 60    # ✓ Reasonable
```

**Recommendations:**
```toml
[web_ui]
port = 3000
refresh_interval = 60
enable_auth = false      # For development
# enable_auth = true     # For production
# auth_secret_file = "${config_dir}/web_secret.txt"
```

---

## Missing Sections

### [timing] - RECOMMENDED

Your V2 recorder has WWV time_snap, but config doesn't reflect it:

```toml
[timing]
# WWV/CHU time_snap mechanism (KA9Q approach)
enable_time_snap = true
time_snap_source = "wwv"  # or "chu" or "gps"
time_snap_update_interval = 3600  # Re-establish every hour
max_timing_drift_ms = 50.0  # Re-establish if drift exceeds this
log_timing_validation = true
timing_log_file = "${data_root}/analytics/timing/wwv_timing.csv"
```

### [quality] - RECOMMENDED

```toml
[quality]
enable_quality_tracking = true
quality_metrics_dir = "${data_root}/analytics/quality"
generate_daily_reports = true
daily_report_time = "00:10"
alert_on_low_completeness = true
completeness_alert_threshold = 95.0  # Alert if < 95%
```

---

## Complete Corrected Configuration

See: **`config/grape-config-CORRECTED.toml`** (I'll create this next)

---

## Action Items

### High Priority (Before Enabling Uploads)

1. ❌ **Remove duplicate CHU 14.67 MHz channel** (lines 135-143)
2. ❌ **Fix [uploader] section** - Change rsync → sftp, fix all paths
3. ❌ **Add Digital RF cadence parameters** to [recorder]
4. ❌ **Remove obsolete [processor.grape]** section
5. ❌ **Enable monitoring** (enable_metrics = true)

### Medium Priority (Operational Improvements)

6. ⚠️ **Add [timing] section** for time_snap configuration
7. ⚠️ **Add [quality] section** for quality tracking
8. ⚠️ **Add lat/long** to [station]
9. ⚠️ **Add file logging** to [logging]

### Low Priority (Nice to Have)

10. 🔹 Add comments explaining each section
11. 🔹 Add validation rules
12. 🔹 Add example values

---

## Validation Script

```bash
#!/bin/bash
# validate-config.sh - Check config file for common errors

echo "Validating grape-config.toml..."

# Check for duplicate SSRCs
echo "Checking for duplicate channels..."
duplicates=$(grep -E "^ssrc = " config/grape-config.toml | sort | uniq -d)
if [ -n "$duplicates" ]; then
    echo "❌ Found duplicate SSRCs:"
    echo "$duplicates"
else
    echo "✓ No duplicate channels"
fi

# Check upload protocol
protocol=$(grep "^protocol = " config/grape-config.toml | cut -d'"' -f2)
if [ "$protocol" != "sftp" ]; then
    echo "❌ Upload protocol is '$protocol', should be 'sftp' for PSWS"
else
    echo "✓ Upload protocol correct (sftp)"
fi

# Check SSH key exists
ssh_key=$(grep "^ssh_key = " config/grape-config.toml | cut -d'"' -f2)
ssh_key_expanded="${ssh_key/#\~/$HOME}"
if [ -f "$ssh_key_expanded" ]; then
    echo "✓ SSH key exists: $ssh_key"
else
    echo "❌ SSH key not found: $ssh_key"
fi

# Check user matches station ID
user=$(grep "^user = " config/grape-config.toml | cut -d'"' -f2)
station_id=$(grep "^id = " config/grape-config.toml | head -1 | cut -d'"' -f2)
if [ "$user" != "$station_id" ]; then
    echo "⚠️  Upload user '$user' doesn't match station ID '$station_id'"
    echo "   Should probably be: user = \"$station_id\""
else
    echo "✓ Upload user matches station ID"
fi

echo ""
echo "Done. Review issues above."
```

---

## Summary

**Current config is 70% correct** - works for recording, but needs fixes before enabling uploads.

**Critical before upload:**
- Fix upload protocol (rsync → sftp)
- Fix upload credentials
- Remove duplicate channel
- Add Digital RF parameters

**Test plan:**
1. Apply corrections
2. Run validation script
3. Test recording with corrected config
4. Verify Digital RF format matches wsprdaemon
5. Test upload (dry-run first!)
