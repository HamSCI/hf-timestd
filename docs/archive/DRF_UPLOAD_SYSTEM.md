# Digital RF Upload System

## Overview

The DRF (Digital RF) Upload System creates wsprdaemon-compatible Digital RF datasets from decimated 10 Hz NPZ files and uploads them daily to the PSWS (Personal Space Weather Station) network server.

## Architecture

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Decimated NPZ      │     │  DRF Batch Writer    │     │  SFTP Upload    │
│  (10 Hz IQ data)    │────▶│  (Multi-subchannel)  │────▶│  to PSWS        │
│  Per-channel dirs   │     │  Single ch0 output   │     │  + Trigger dir  │
└─────────────────────┘     └──────────────────────┘     └─────────────────┘
         │                           │                           │
         ▼                           ▼                           ▼
   9 frequency                 OBS directory              Server processes
   channels                    with ch0/                  data on trigger
```

## Key Components

### 1. DRF Batch Writer (`src/signal_recorder/drf_batch_writer.py`)

Creates wsprdaemon-compatible Digital RF datasets with **all frequencies combined into a single `ch0`** with multiple subchannels.

**Wsprdaemon Format:**
- Single `ch0` directory (not separate ch0, ch1, ch2...)
- IQ data horizontally stacked: `[freq1_IQ | freq2_IQ | ... | freq9_IQ]`
- `num_subchannels = 9` (one per frequency)
- `center_frequencies` metadata array: `[2.5, 3.33, 5.0, 7.85, 10.0, 14.67, 15.0, 20.0, 25.0]` MHz

**Usage:**
```bash
python -m signal_recorder.drf_batch_writer \
    --analytics-root /path/to/analytics \
    --output-dir /path/to/output \
    --date 2025-11-28 \
    --callsign AC0G \
    --grid-square EM38ww \
    --receiver-name GRAPE \
    --psws-station-id S000171 \
    --psws-instrument-id 172 \
    --include-extended-metadata  # Optional
```

**Output Structure:**
```
output/
└── 20251128/
    └── AC0G_EM38ww/
        └── GRAPE@S000171_172/
            └── OBS2025-11-28T00-00/
                └── ch0/
                    ├── drf_properties.h5
                    ├── 2025-11-28T00-00-00/
                    │   └── rf@*.h5  (IQ data files)
                    └── metadata/
                        └── 2025-11-28T00-00-00/
                            └── metadata@*.h5
```

**Metadata Fields:**
| Field | Description |
|-------|-------------|
| `callsign` | Station callsign (e.g., "AC0G") |
| `grid_square` | Maidenhead grid (e.g., "EM38ww") |
| `receiver_name` | Receiver type (e.g., "GRAPE") |
| `lat` / `long` | Latitude/longitude derived from grid square |
| `center_frequencies` | Array of frequencies in Hz |
| `uuid_str` | PSWS station ID |

### 2. Upload Tracker (`src/signal_recorder/upload_tracker.py`)

Tracks upload status in a JSON state file to:
- Skip already-uploaded dates
- Retry failed uploads
- Maintain audit trail

**State File Location:** `$DATA_ROOT/upload/upload-state.json`

**State File Format:**
```json
{
  "version": 1,
  "station_id": "S000171",
  "last_successful_date": "2025-11-28",
  "uploads": [
    {
      "date": "2025-11-28",
      "uploaded_at": "2025-11-29T00:35:42Z",
      "status": "success",
      "channels": 9,
      "obs_dir": "OBS2025-11-28T00-00",
      "trigger_dir": "cOBS2025-11-28T00-00_#172_#2025-1129T00-35",
      "bytes_uploaded": 35000000,
      "duration_seconds": 28.0,
      "error_message": null
    }
  ]
}
```

**CLI Usage:**
```bash
# Check if date is uploaded
python -m signal_recorder.upload_tracker \
    --state-file /path/to/state.json \
    --station-id S000171 \
    check --date 2025-11-28

# Record upload result
python -m signal_recorder.upload_tracker \
    --state-file /path/to/state.json \
    --station-id S000171 \
    record --date 2025-11-28 --status success --channels 9 ...

# Show statistics
python -m signal_recorder.upload_tracker \
    --state-file /path/to/state.json \
    --station-id S000171 \
    stats
```

### 3. Daily Upload Script (`scripts/daily-drf-upload.sh`)

Orchestrates the complete daily upload workflow:

1. Check if date already uploaded (skip if so)
2. Run DRF batch writer for all channels
3. SFTP upload OBS directory to PSWS
4. Create trigger directory to signal processing
5. Record result in upload tracker

**Configuration (environment variables):**
| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_ROOT` | `/tmp/grape-test` | Base data directory |
| `TARGET_DATE` | Yesterday | Date to process (YYYY-MM-DD) |
| `BANDWIDTH_LIMIT` | `0` (unlimited) | Upload bandwidth in kbps |
| `SSH_KEY` | `~/.ssh/id_rsa` | SSH key for PSWS authentication |
| `INCLUDE_EXTENDED_METADATA` | `false` | Include timing/gap metadata |

**Manual Execution:**
```bash
# Process yesterday (default)
/home/wsprdaemon/signal-recorder/scripts/daily-drf-upload.sh

# Process specific date
TARGET_DATE="2025-11-28" /home/wsprdaemon/signal-recorder/scripts/daily-drf-upload.sh
```

### 4. Systemd Service and Timer

**Service:** `systemd/grape-daily-upload.service`
**Timer:** `systemd/grape-daily-upload.timer`

Runs daily at 00:30 UTC to upload the previous day's data.

**Installation:**
```bash
sudo cp systemd/grape-daily-upload.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable grape-daily-upload.timer
sudo systemctl start grape-daily-upload.timer
```

## Configuration

### `grape-config.toml` Settings

```toml
[uploader]
enabled = false  # Set true when ready for production
upload_time = "00:30"
upload_timezone = "UTC"

[uploader.sftp]
host = "pswsnetwork.eng.ua.edu"
port = 22
user = "S000171"
ssh_key = "/home/wsprdaemon/.ssh/id_rsa"
bandwidth_limit_kbps = 0  # 0 = unlimited
create_trigger_directory = true

[uploader.metadata]
include_extended_metadata = false  # Set true for timing/gap data
```

## PSWS Upload Protocol

### Trigger Directory Convention

The trigger directory signals PSWS that the upload is complete:

```
c{OBS_DIR_NAME}_#{instrument_id}_#{timestamp}
```

Example: `cOBS2025-11-28T00-00_#172_#2025-1129T00-35`

**Note:** The `#` characters must be escaped as `\#` in SFTP commands.

### Upload Flow

1. Upload OBS directory via SFTP: `put -r OBS2025-11-28T00-00`
2. Create trigger directory: `mkdir cOBS2025-11-28T00-00_\#172_\#...`
3. PSWS server detects trigger and processes data
4. PSWS server removes trigger directory after processing

## Extended Metadata (Optional)

When `include_extended_metadata = true`, additional metadata is written:

- **Time snap calibration data** - RTP-to-UTC timing information
- **Gap analysis** - Detected gaps in the data stream
- **Timing quality metrics** - From the 16 kHz source data

This data is stored in `ch0/metadata/extended/extended_metadata.json`.

## Troubleshooting

### Upload Fails

1. Check SSH key is registered with PSWS: `ssh -i ~/.ssh/id_rsa S000171@pswsnetwork.eng.ua.edu`
2. Check upload state file for error messages
3. Review log file: `/tmp/grape-test/logs/daily-upload.log`

### Missing Channels

1. Verify all channels have decimated data for the target date
2. Check analytics directories exist: `ls $DATA_ROOT/analytics/*/decimated/`
3. The batch writer only creates time slices where ALL 9 channels have data

### Re-upload a Date

To force re-upload of an already-uploaded date:
1. Edit the state file to remove or change the upload record
2. Or manually run with a fresh state file

## Files Created

| File | Purpose |
|------|---------|
| `src/signal_recorder/drf_batch_writer.py` | Multi-subchannel DRF writer |
| `src/signal_recorder/upload_tracker.py` | Upload state tracking |
| `scripts/daily-drf-upload.sh` | Daily upload orchestration |
| `systemd/grape-daily-upload.service` | Systemd service unit |
| `systemd/grape-daily-upload.timer` | Systemd timer unit |
| `docs/DRF_UPLOAD_SYSTEM.md` | This documentation |
