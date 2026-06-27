# Session 2026-02-13: GRAPE Pipeline Fix & Web Dashboard

## Objective
Verify and fix the GRAPE module's end-to-end functionality for daily uploads to HamSCI PSWS.

## Root Cause: OOM Kill
The `grape-daily.service` was being killed by the OOM killer every night. The service had `MemoryMax=2G` but the decimation process grew to 1.9G+ because `np.frombuffer()` in `raw_reader.py` held references to decompressed zstd bytes objects, preventing garbage collection. Each of the 1440 minute files per channel decompresses to ~11.5MB, and the bytes objects accumulated.

## Bugs Fixed

### 1. OOM Kill — Memory leak in raw_reader.py (CRITICAL)
- **File**: `src/hf_timestd/grape/raw_reader.py`
- **Fix**: Added `.copy()` after `np.frombuffer()` for zstd and lz4 decompression paths
- **Result**: Peak memory dropped from 1.9G+ (OOM killed) to ~370MB stable

### 2. gap_info always zero — decimation_pipeline.py
- **File**: `src/hf_timestd/grape/decimation_pipeline.py`
- **Bug**: `gap_info = expected_raw_samples - len(samples)` computed AFTER `samples = padded`, so it was always 0
- **Fix**: Compute `gap_info` before reassigning `samples`

### 3. grape-daily.service — Wrong Python, missing --date, low memory limit
- **File**: `systemd/grape-daily.service`
- **Fixes**:
  - Python path: `/usr/bin/python3` → `/opt/hf-timestd/venv/bin/python3`
  - Added `--date yesterday` to `grape package` command (was missing required arg)
  - `MemoryMax`: 2G → 6G
  - Added `Environment=PATH=...` for venv

### 4. grape-daily.timer — Duplicate OnCalendar (fired twice daily)
- **File**: `systemd/grape-daily.timer`
- **Bug**: Had both `OnCalendar=daily` (midnight) and `OnCalendar=01:00` — two triggers
- **Fix**: Single `OnCalendar=*-*-* 01:00:00`

### 5. CLI --date handling — Missing "yesterday" support
- **File**: `src/hf_timestd/cli.py`
- **Fix**: Added `resolve_date()` helper that handles `None`, `"yesterday"`, and date strings. Made `--date` optional for `grape package` (defaults to yesterday).

### 6. Missing [uploader.sftp] config section
- **File**: `config/timestd-config.toml` + `/etc/hf-timestd/timestd-config.toml`
- **Fix**: Added `[uploader.sftp]` with host, user, ssh_key, bandwidth_limit_kbps

### 7. SSH key — Copied to timestd user
- Copied `/home/mjh/.ssh/id_rsa` → `/home/timestd/.ssh/id_rsa_psws`
- Config points to timestd-accessible path

### 8. Metadata I/O optimization — decimated_buffer.py
- **File**: `src/hf_timestd/grape/decimated_buffer.py`
- **Fix**: Added in-memory metadata cache with `flush_metadata()`. Previously loaded/saved the full JSON (up to 300KB) on every minute write — 1440 times per channel. Now batched to a single write per channel.

## New Features

### GRAPE Web Dashboard
- **Page**: `web-api/static/grape.html` — Spectrogram viewer, decimation status, upload history
- **Router**: `web-api/routers/grape.py` — 5 endpoints: summary, channels, decimation, spectrograms, uploads
- **Service**: `web-api/services/grape_service.py` — Reads products dir for status
- **Nav**: GRAPE link added to all 15 existing HTML pages

### API Endpoints
- `GET /api/grape/summary` — Overall pipeline summary
- `GET /api/grape/channels` — List channels with decimated data
- `GET /api/grape/decimation` — Per-channel decimation status
- `GET /api/grape/spectrograms/{channel}` — Available spectrogram dates
- `GET /api/grape/spectrograms/{channel}/{date}` — Serve spectrogram PNG
- `GET /api/grape/uploads` — Upload queue and history

### 9. Uploader: scp → sftp batch mode (CRITICAL)
- **File**: `src/hf_timestd/grape/uploader.py`
- **Bug**: Used `scp -r` for data transfer, but PSWS server is sftp-only (rejects ssh/scp)
- **Fix**: Rewrote `SFTPUpload.upload()` to use sftp batch mode with `_build_sftp_put_commands()` that walks the local directory tree and generates mkdir/put commands
- **Result**: 35 sftp commands per upload, ~14-17 seconds per dataset

### 10. SSH key — Restored correct PSWS key
- Previous session accidentally copied `/home/mjh/.ssh/id_rsa` (general key) over the PSWS key
- Restored `/home/mjh/.ssh/id_rsa_psws` (comment: `wsprdaemon@bee1`) to `/home/timestd/.ssh/id_rsa_psws`
- SSH username `S000171` is correct (Station ID = SSH login for PSWS)

### 11. Trigger directory truncated at '#' — uploader.py (CRITICAL)
- **File**: `src/hf_timestd/grape/uploader.py`
- **Bug 1**: sftp interprets `#` as a comment character, so `mkdir cOBS..._#172_#...` was truncated to `mkdir cOBS..._` — the server never parsed or processed the uploaded files
- **Fix**: Wrap trigger dir name in double quotes: `mkdir "cOBS..._#172_#..."`
- **Bug 2**: Timestamp format `'%Y-%m%dT%H-%M'` was missing a dash between month and day (produced `2026-0213` instead of `2026-02-13`)
- **Fix**: Changed to `'%Y-%m-%dT%H-%M'`
- **Recovery**: Re-sent correct trigger directories for all 28 previously-uploaded datasets via single sftp batch

## Verification Results
- **grape decimate**: 9 channels × 1440 min, ~7 min/channel, ~370MB peak memory ✅
- **grape spectrogram**: CHU_3330 20260211, 1946×1185 PNG, 1.5MB ✅
- **grape package**: 20260210 (99.99%), 20260212 (99.53%), 9 channels each ✅
- **grape upload**: Real upload to S000171@pswsnetwork.eng.ua.edu, 14-17s per dataset ✅
- **Trigger dirs**: Correct format `cOBS2026-02-10T00-00_#172_#2026-02-13T12-20` confirmed ✅
- **Backfill**: 28 dates (2026-01-06 through 2026-02-12) packaged and uploaded ✅
- **GRAPE web page**: Loads at /grape, API returns correct data, spectrograms served ✅

## Files Modified
- `src/hf_timestd/grape/raw_reader.py` (.copy() fix)
- `src/hf_timestd/grape/decimation_pipeline.py` (gap_info fix + flush_metadata call)
- `src/hf_timestd/grape/decimated_buffer.py` (metadata cache + flush_metadata)
- `src/hf_timestd/grape/uploader.py` (scp→sftp batch, +os import, +_build_sftp_put_commands)
- `src/hf_timestd/cli.py` (resolve_date helper, --date optional for package)
- `systemd/grape-daily.service` (venv Python, --date, MemoryMax)
- `systemd/grape-daily.timer` (single OnCalendar)
- `config/timestd-config.toml` ([uploader.sftp] section)
- `web-api/main.py` (grape router + /grape route)
- `web-api/routers/__init__.py` (grape_router import)
- `web-api/routers/grape.py` (new)
- `web-api/services/grape_service.py` (new)
- `web-api/static/grape.html` (new)
- `web-api/static/*.html` (GRAPE nav link added to all 14 existing pages)

## Production Deployment
- Systemd service + timer copied to `/etc/systemd/system/` and reloaded
- Core library synced to `/opt/hf-timestd/src/hf_timestd/`
- Web-api files synced to `/opt/hf-timestd/web-api/`
- Web-api service restarted
- SSH key copied to `/home/timestd/.ssh/id_rsa_psws`
- `[uploader.sftp]` added to `/etc/hf-timestd/timestd-config.toml`

## Status
- 28 dates backfilled and uploaded to PSWS with correct trigger directories
- 7 dates skipped (20260126-129, 131, 204, 207) — raw binary data not available (metadata-only)
- Tomorrow's `grape-daily.timer` at 01:00 UTC will be the first fully automated run with all fixes
