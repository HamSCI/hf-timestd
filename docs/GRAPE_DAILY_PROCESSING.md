# GRAPE Daily Processing

## Overview

GRAPE (GRAPE Recorder and Processor Engine) runs as a daily batch job to process raw IQ data from hf-timestd into data products for PSWS upload.

## Systemd Timer

The GRAPE daily processing is managed by a systemd timer that runs at 01:00 UTC each day.

### Installation

```bash
# Copy service and timer files
sudo cp systemd/grape-daily.service /etc/systemd/system/
sudo cp systemd/grape-daily.timer /etc/systemd/system/
sudo cp systemd/grape-upload-retry.service /etc/systemd/system/
sudo cp systemd/grape-upload-retry.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable and start the timers
sudo systemctl enable --now grape-daily.timer
sudo systemctl enable --now grape-upload-retry.timer
```

In addition to the daily run, a second timer `grape-upload-retry.timer`
(`OnBootSec=5min`, `OnUnitActiveSec=30min`) periodically runs
`grape upload --resume` to drain any datasets queued for retry by a failed or
unverified upload.

### Management

```bash
# Check timer status
systemctl status grape-daily.timer

# Check when it will run next
systemctl list-timers grape-daily.timer

# View logs from last run
journalctl -u grape-daily.service -n 100

# Manually trigger a run (for testing)
sudo systemctl start grape-daily.service

# Disable the timer
sudo systemctl disable --now grape-daily.timer

# Inspect the upload-retry timer (runs `grape upload --resume`)
systemctl status grape-upload-retry.timer
journalctl -u grape-upload-retry.service -n 100
```

## What It Does

The daily job processes **yesterday's data** through the following pipeline:

1. **Decimate** - Convert 24/20 kHz raw IQ to 10 Hz decimated IQ
   - Reads from raw buffer (handles both legacy 1-minute files and 10-minute chunk files)
   - Enumerates all 1440 expected minutes per day explicitly (no gaps missed at day boundaries)
   - Single `StatefulDecimator` per channel preserves filter state across minutes
   - Multi-stage: CIC (R=60) → compensation FIR → final FIR (R=40)
   - Outputs to `/var/lib/timestd/products/{CHANNEL}/decimated/`

2. **Spectrograms** - Generate carrier spectrograms
   - Creates daily spectrograms for all configured channels
   - Edge tapering at gap boundaries (half-cosine, 5s) replaces zero interpolation
   - Full-window validity masking: any NFFT=512 window overlapping a gap is NaN-masked
   - Outputs to `/var/lib/timestd/products/{CHANNEL}/spectrograms/`

3. **Package** (optional) - Package as Digital RF
   - Creates DRF packages for PSWS upload
   - Outputs to `upload/{YYYYMMDD}/{CALLSIGN}_{GRID}/{RECEIVER}@{ID}/OBS.../ch0/`

4. **Upload** (optional) - Upload to PSWS repository
   - Uploads packaged data via SFTP

## Upload Behavior

Upload failures are **non-fatal** — stages 1-3 (decimate, spectrogram, package) always complete and the data is preserved on disk. If the upload fails (e.g., SSH key not yet registered with PSWS), the dataset is queued for retry. Upload the backlog later with:

```bash
sudo -u timestd /opt/hf-timestd/venv/bin/hf-timestd grape upload --date YYYYMMDD
```

To skip the upload stage entirely (e.g., while waiting for PSWS key registration):

```bash
sudo -u timestd /opt/hf-timestd/venv/bin/hf-timestd grape daily --no-upload
```

### Upload verification

Before an upload is marked complete (and cleanup allowed), the uploader walks
the dataset and requests an sftp `ls -l` of each leaf file, confirming the
remote byte sizes match and that the trigger directory is present. If
verification fails, the dataset is re-queued for retry rather than deleted, so
nothing is lost on a partial or corrupted transfer.

> Background: the `ls -l` parser was hardened in 8f02d48 to tolerate a `?`
> nlink field returned by the PSWS server.

## Configuration

Edit `/etc/systemd/system/grape-daily.service` to customize:

- **Channels**: Add/remove spectrogram generation for specific channels
- **Upload**: Uncomment package/upload lines when ready
- **Resource limits**: Adjust `CPUQuota` and `MemoryMax` if needed

After editing:

```bash
sudo systemctl daemon-reload
sudo systemctl restart grape-daily.timer
```

## Monitoring

### Check Last Run

```bash
systemctl status grape-daily.service
```

### View Logs

```bash
# Last 100 lines
journalctl -u grape-daily.service -n 100

# Follow live
journalctl -u grape-daily.service -f

# Logs from specific date
journalctl -u grape-daily.service --since "2026-01-02"
```

### Check for Failures

```bash
# Show failed runs
systemctl list-timers --failed

# Check service status
systemctl is-failed grape-daily.service
```

## Manual Execution

To process a specific date manually:

```bash
# Decimate specific channel
hf-timestd grape decimate --channel "SHARED 10000" --date 2026-01-02

# Decimate all channels
hf-timestd grape decimate --all-channels --date 2026-01-02

# Generate spectrogram
hf-timestd grape spectrogram --channel "SHARED 10000" --date 2026-01-02

# Package for upload
hf-timestd grape package --date 2026-01-02 --callsign AC0G --grid EM28

# Upload
hf-timestd grape upload --date 2026-01-02 --dry-run
```

## Preflight Check

Before relying on automated uploads, verify PSWS connectivity with the built-in preflight test:

```bash
# Run as timestd user (the service user that owns the SSH key)
sudo -u timestd /opt/hf-timestd/venv/bin/hf-timestd grape test-upload
```

This performs three checks:
1. **TCP connectivity** — can we reach `pswsnetwork.eng.ua.edu:22`?
2. **SSH key** — does the configured key file exist with correct permissions?
3. **SFTP autologin** — can we authenticate and connect without a password?

Example output (all checks passing):
```
PSWS Upload Preflight Check
  Host:    pswsnetwork.eng.ua.edu
  User:    S000171
  SSH key: /home/timestd/.ssh/id_rsa_psws

[1/3] TCP connectivity to pswsnetwork.eng.ua.edu:22 ... OK (0.1s)
[2/3] SSH key at /home/timestd/.ssh/id_rsa_psws ... OK
      Public key: ssh-rsa AAAAB3NzaC1yc2EAAA...
[3/3] SFTP autologin as S000171@pswsnetwork.eng.ua.edu ... OK (1.0s)

All checks passed — PSWS upload should work.
```

The command reads host, user, and SSH key path from `/etc/hf-timestd/timestd-config.toml` (sections `[station]` and `[uploader.sftp]`). Use `--config` to specify an alternate config file.

Run this on every new installation before enabling `grape-daily.timer`.

## Troubleshooting

### Timer not running

```bash
# Check if timer is enabled
systemctl is-enabled grape-daily.timer

# Check timer status
systemctl status grape-daily.timer

# Enable if needed
sudo systemctl enable --now grape-daily.timer
```

### Service failing

```bash
# View detailed logs
journalctl -u grape-daily.service -xe

# Check permissions
ls -la /var/lib/timestd/grape/
ls -la /var/log/timestd/grape/

# Ensure directories exist
sudo mkdir -p /var/lib/timestd/grape /var/log/timestd/grape
sudo chown timestd:timestd /var/lib/timestd/grape /var/log/timestd/grape
```

### Missing data

```bash
# Check raw data exists
ls -la /var/lib/timestd/raw_archive/

# Check decimated output
ls -la /var/lib/timestd/products/{CHANNEL}/decimated/
```

## Performance

The daily job typically takes:

- **Decimation**: ~5-10 minutes per channel
- **Spectrograms**: ~1-2 minutes per channel
- **Total**: ~30-60 minutes for the 9 canonical channels

Resource usage:

- **CPU**: Limited to 50% (configurable)
- **Memory**: Limited to 2GB (configurable)
- **Disk I/O**: Moderate (reading raw data, writing products)
