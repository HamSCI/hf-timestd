# GRAPE Recorder - Production Deployment Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 2, 2025

This guide covers deploying GRAPE Recorder in production mode with systemd services for 24/7 operation, automatic restart, and daily uploads.

---

## Prerequisites

Before production deployment:

1. **Test mode working** - Verify recording, analytics, and web UI work in test mode
2. **PSWS account** - Request account at grape@hamsci.groups.io
3. **SSH key** - Generate and install SSH key for PSWS uploads
4. **radiod** - ka9q-radio running and accessible

---

## Installation

### Fresh Production Install

```bash
# Clone repository
git clone https://github.com/mijahauan/grape-recorder.git
cd grape-recorder

# Run installer in production mode
sudo ./scripts/install.sh --mode production --user $USER
```

This creates:
- `/var/lib/grape-recorder/` - Data directory
- `/etc/grape-recorder/` - Configuration
- `/opt/grape-recorder/` - Venv and Web UI
- Systemd services for all components

### Upgrade from Test Mode

If already running in test mode:

```bash
# Stop test services
./scripts/grape-all.sh -stop

# Run production installer
sudo ./scripts/install.sh --mode production --user $USER

# Copy your config
sudo cp config/grape-config.toml /etc/grape-recorder/

# Update mode in config
sudo sed -i 's/mode = "test"/mode = "production"/' /etc/grape-recorder/grape-config.toml

# Start production services
sudo systemctl start grape-recorder grape-analytics grape-webui
```

---

## Directory Structure

### Production Layout (FHS Compliant)

```
/var/lib/grape-recorder/        # GRAPE_DATA_ROOT - Application data
├── archives/                   # Raw 20 kHz NPZ files
│   └── {CHANNEL}/              # WWV_10_MHz, CHU_3.33_MHz, etc.
├── analytics/                  # Per-channel analytics
│   └── {CHANNEL}/
│       ├── decimated/          # 10 Hz NPZ files
│       ├── digital_rf/         # Digital RF (for upload)
│       ├── discrimination/     # WWV/WWVH detection CSVs
│       ├── doppler/            # Doppler shift estimates
│       ├── timing/             # Time-snap metrics
│       └── ...
├── spectrograms/               # Web UI spectrogram images
│   └── {YYYYMMDD}/
├── upload/                     # Staging for PSWS uploads
│   └── {YYYYMMDD}/             # Daily upload packages
├── state/                      # Service state persistence
└── status/                     # Health/status JSON

/var/log/grape-recorder/        # GRAPE_LOG_DIR - Application logs (FHS standard)
├── recorder.log                # Core recorder logs
├── analytics.log               # Analytics service logs
├── daily-upload.log            # Upload job logs
└── webui.log                   # Web server logs

/etc/grape-recorder/            # Configuration
├── grape-config.toml           # Main configuration
└── environment                 # Environment variables

/opt/grape-recorder/            # Application binaries
├── venv/                       # Python virtual environment
└── web-ui/                     # Node.js web interface
```

**Why separate `/var/log/`?** Linux Filesystem Hierarchy Standard (FHS) specifies:
- `/var/lib/` for persistent application data
- `/var/log/` for log files (enables standard logrotate, monitoring tools)

---

## Configuration

### Environment File

`/etc/grape-recorder/environment` sets all paths for systemd services:

```bash
GRAPE_MODE=production
GRAPE_DATA_ROOT=/var/lib/grape-recorder
GRAPE_LOG_DIR=/var/log/grape-recorder
GRAPE_CONFIG=/etc/grape-recorder/grape-config.toml
GRAPE_VENV=/opt/grape-recorder/venv
GRAPE_WEBUI=/opt/grape-recorder/web-ui
GRAPE_LOG_LEVEL=INFO

# PSWS Upload (optional overrides)
GRAPE_PSWS_HOST=pswsnetwork.eng.ua.edu
GRAPE_PSWS_USER=S000171
GRAPE_SSH_KEY=/home/youruser/.ssh/id_rsa
```

### Main Configuration

Edit `/etc/grape-recorder/grape-config.toml`:

```toml
[station]
callsign = "W1ABC"
grid_square = "FN31pr"
id = "S000XXX"           # Your PSWS station ID
instrument_id = "YYY"    # Your PSWS instrument ID

[recorder]
mode = "production"
production_data_root = "/var/lib/grape-recorder"

[uploader]
enabled = true           # Enable after SSH key setup
protocol = "sftp"
upload_time = "00:30"
```

---

## Service Management

### Systemd Services

| Service | Description |
|---------|-------------|
| `grape-recorder.service` | Core RTP→NPZ recorder |
| `grape-analytics.service` | Decimation, discrimination, DRF |
| `grape-webui.service` | Express web server |
| `grape-upload.timer` | Daily 00:30 UTC upload trigger |
| `grape-upload.service` | Upload job (triggered by timer) |

### Commands

```bash
# Start all services
sudo systemctl start grape-recorder grape-analytics grape-webui

# Stop all services  
sudo systemctl stop grape-recorder grape-analytics grape-webui

# Check status
sudo systemctl status grape-recorder grape-analytics grape-webui

# View logs (live)
journalctl -u grape-recorder -f
journalctl -u grape-analytics -f

# View combined logs
journalctl -u grape-recorder -u grape-analytics -u grape-webui --since "1 hour ago"

# Enable auto-start on boot
sudo systemctl enable grape-recorder grape-analytics grape-webui grape-upload.timer

# Restart after config change
sudo systemctl restart grape-recorder grape-analytics
```

### Service Dependencies

```
grape-recorder.service
       │
       ▼
grape-analytics.service  ←──  grape-upload.timer (00:30 UTC)
       │                              │
       ▼                              ▼
grape-webui.service          grape-upload.service
```

---

## Daily Upload Process

### How It Works

1. **Timer triggers** at 00:30 UTC daily
2. **DRF batch writer** creates Digital RF from decimated NPZ for yesterday
3. **SFTP upload** sends to `pswsnetwork.eng.ua.edu`
4. **Trigger directory** signals PSWS to process the data

### Manual Upload (Testing)

```bash
# Upload specific date
sudo -u youruser TARGET_DATE=2025-12-01 /home/youruser/grape-recorder/scripts/daily-drf-upload.sh

# Check upload status
journalctl -u grape-upload -n 100

# View upload state
cat /var/lib/grape-recorder/upload/upload-state.json
```

### Enable Uploads

1. **Generate SSH key:**
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/grape_psws -N ""
   ```

2. **Send public key to PSWS admin:**
   ```bash
   cat ~/.ssh/grape_psws.pub
   ```

3. **Test SSH access:**
   ```bash
   ssh -i ~/.ssh/grape_psws S000XXX@pswsnetwork.eng.ua.edu
   ```

4. **Update config:**
   ```bash
   sudo nano /etc/grape-recorder/grape-config.toml
   # Set: enabled = true under [uploader]
   ```

5. **Start timer:**
   ```bash
   sudo systemctl enable --now grape-upload.timer
   ```

---

## Monitoring & Health

### Web Dashboard

Access at `http://hostname:3000`:
- Channel status and completeness
- Discrimination results
- Timing quality metrics
- Spectrogram display

### Health Checks

```bash
# Check if services are running
systemctl is-active grape-recorder grape-analytics grape-webui

# Check for errors in last hour
journalctl -u grape-recorder -p err --since "1 hour ago"

# Check disk usage
df -h /var/lib/grape-recorder

# Check archive growth
du -sh /var/lib/grape-recorder/archives/*
```

### Log Rotation

Logs are automatically rotated via `/etc/logrotate.d/grape-recorder`:
- Daily rotation
- 14-day retention
- Compressed after 1 day

---

## Troubleshooting

### Service Won't Start

```bash
# Check systemd status
sudo systemctl status grape-recorder

# Check journal for errors
journalctl -u grape-recorder -n 50

# Verify environment file
cat /etc/grape-recorder/environment

# Test Python environment
sudo -u youruser /opt/grape-recorder/venv/bin/python -c "import grape_recorder; print('OK')"
```

### No Data Being Recorded

```bash
# Check radiod is accessible
avahi-resolve -n your-radiod-status.local

# Check multicast
sudo tcpdump -i any -c 10 udp port 5004

# Check config
cat /etc/grape-recorder/grape-config.toml | grep -A5 "\[ka9q\]"
```

### Upload Failures

```bash
# Test SSH manually
ssh -i ~/.ssh/grape_psws -v S000XXX@pswsnetwork.eng.ua.edu

# Check upload logs
journalctl -u grape-upload -n 100

# Check upload state
cat /var/lib/grape-recorder/upload/upload-state.json

# Retry specific date
sudo -u youruser TARGET_DATE=2025-12-01 /path/to/scripts/daily-drf-upload.sh
```

### High Memory Usage

```bash
# Check per-service memory
systemctl status grape-recorder | grep Memory

# Adjust limits in service file
sudo systemctl edit grape-recorder
# Add: [Service]
#      MemoryMax=1G
sudo systemctl restart grape-recorder
```

---

## Backup & Recovery

### What to Backup

1. **Configuration:** `/etc/grape-recorder/`
2. **Upload state:** `/var/lib/grape-recorder/upload/upload-state.json`
3. **Service state:** `/var/lib/grape-recorder/state/`

### Data Retention

Raw archives grow ~100 MB/channel/day. Consider:

```bash
# Cleanup archives older than 7 days (after upload confirmed)
find /var/lib/grape-recorder/archives -name "*.npz" -mtime +7 -delete

# Keep decimated files for analysis
# Keep discrimination CSVs indefinitely (small)
```

### Disaster Recovery

```bash
# Restore from backup
sudo rsync -av backup/grape-recorder/ /etc/grape-recorder/

# Reinstall if needed
sudo ./scripts/install.sh --mode production --user youruser

# Restore state
sudo cp backup/upload-state.json /var/lib/grape-recorder/upload/
```

---

## Security Considerations

### Permissions

- Services run as non-root user
- Data directories owned by service user
- Config files readable by service user only

### Network

- RTP multicast: UDP 5004 (internal only)
- Web UI: TCP 3000 (consider firewall/reverse proxy)
- SFTP upload: TCP 22 (outbound only)

### SSH Key Security

```bash
# Restrict key permissions
chmod 600 ~/.ssh/grape_psws
chmod 700 ~/.ssh

# Use dedicated key for PSWS only
# Consider key passphrase with ssh-agent for manual runs
```

---

## Performance Tuning

### Network Buffers

For high-channel-count installations:

```bash
# Increase UDP buffer sizes
sudo sysctl -w net.core.rmem_max=26214400
sudo sysctl -w net.core.rmem_default=8388608

# Make permanent in /etc/sysctl.conf
```

### CPU Affinity

For multi-core systems:

```bash
sudo systemctl edit grape-recorder
# Add:
# [Service]
# CPUAffinity=0 1
```

---

## Appendix: Quick Reference

### Start/Stop Commands

| Action | Command |
|--------|---------|
| Start all | `sudo systemctl start grape-recorder grape-analytics grape-webui` |
| Stop all | `sudo systemctl stop grape-recorder grape-analytics grape-webui` |
| Restart all | `sudo systemctl restart grape-recorder grape-analytics grape-webui` |
| Status | `sudo systemctl status grape-recorder grape-analytics grape-webui` |
| Logs | `journalctl -u grape-recorder -u grape-analytics -f` |

### Paths

| Item | Location |
|------|----------|
| Data | `/var/lib/grape-recorder/` |
| Logs | `/var/log/grape-recorder/` |
| Config | `/etc/grape-recorder/grape-config.toml` |
| Environment | `/etc/grape-recorder/environment` |
| Venv | `/opt/grape-recorder/venv/` |
| Web UI | `/opt/grape-recorder/web-ui/` |

### Files

| File | Purpose |
|------|---------|
| `archives/{CHANNEL}/*.npz` | Raw 20 kHz IQ data |
| `analytics/{CHANNEL}/decimated/*.npz` | 10 Hz decimated data |
| `analytics/{CHANNEL}/discrimination/*.csv` | WWV/WWVH detection |
| `upload/upload-state.json` | Upload tracking |
| `state/analytics-*.json` | Service state |
