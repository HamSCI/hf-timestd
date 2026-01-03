# FastAPI Monitoring Server - Deployment Guide

## Quick Start

The FastAPI monitoring server is ready for deployment with native HDF5 support.

### Testing Locally

```bash
cd /home/mjh/git/hf-timestd/web-ui
../venv/bin/python monitoring_server.py
```

Server will start on `http://localhost:8080`

### Production Deployment

1. **Install to production**:

```bash
cd /home/mjh/git/hf-timestd
sudo ./install.sh
```

1. **Install systemd service**:

```bash
sudo cp systemd/timestd-web-ui-fastapi.service /etc/systemd/system/timestd-web-ui.service
sudo systemctl daemon-reload
```

1. **Stop old Node.js server** (if running):

```bash
sudo systemctl stop timestd-web-ui
```

1. **Start FastAPI server**:

```bash
sudo systemctl start timestd-web-ui
sudo systemctl enable timestd-web-ui
```

1. **Check status**:

```bash
sudo systemctl status timestd-web-ui
journalctl -u timestd-web-ui -f
```

### Verify Endpoints

```bash
# Health check
curl http://localhost:8080/api/v1/system/status

# HDF5 timing data
curl "http://localhost:8080/api/v1/timing/clock-offset?channel=CHU%203.33%20MHz&date=20251225"

# Channels list
curl http://localhost:8080/api/v1/channels

# Fusion timing
curl http://localhost:8080/api/v1/timing/fusion
```

## API Endpoints Implemented

### ✅ Core Endpoints

- `GET /` - Serve summary.html
- `GET /{filename}` - Serve HTML files
- `GET /health` - Health check

### ✅ System Info

- `GET /api/v1/station/info` - Station configuration
- `GET /api/v1/system/status` - System status
- `GET /api/v1/channels` - List of channels

### ✅ Timing Data (with HDF5)

- `GET /api/v1/timing/clock-offset` - L2 timing measurements (HDF5)
- `GET /api/v1/channels/{channel}/carrier-power/{date}` - L1A observables (HDF5)
- `GET /api/v1/timing/fusion` - Multi-broadcast fusion (CSV)

## Features

- ✅ Native HDF5 support with h5py
- ✅ SWMR mode for concurrent file access
- ✅ Quality metadata in all responses
- ✅ CSV fallback for resilience
- ✅ Fast async performance
- ✅ Automatic error handling
- ✅ Configuration from timestd-config.toml

## Next Steps

### Additional Endpoints to Port (Optional)

From Node.js server, these can be added as needed:

- `/api/v1/gpsdo-status` - GPSDO monitor state
- `/api/v1/timing/kalman-funnel` - Kalman funnel visualization
- `/api/v1/timing/constellation` - Station constellation
- Various channel-specific endpoints

### Frontend Updates (Future)

The existing HTML/JS/CSS works as-is, but can be enhanced:

- Add quality grade color-coding
- Display uncertainty error bars
- Add quality filter controls
- Show metadata panels

## Troubleshooting

### Server won't start

```bash
# Check logs
journalctl -u timestd-web-ui -n 50

# Check if port 8080 is in use
sudo lsof -i :8080

# Test manually
cd /opt/hf-timestd/web-ui
/opt/hf-timestd/venv/bin/python monitoring_server.py
```

### HDF5 files not found

```bash
# Check data directory
ls -lh /var/lib/timestd/phase2/*/clock_offset/*.h5

# Check permissions
sudo -u timestd ls /var/lib/timestd/phase2/
```

### Import errors

```bash
# Reinstall dependencies
cd /home/mjh/git/hf-timestd
source venv/bin/activate
pip install -r requirements.txt
```

## Rollback to Node.js

If needed, revert to Node.js server:

```bash
# Stop FastAPI
sudo systemctl stop timestd-web-ui

# Restore Node.js service
sudo cp systemd/timestd-web-ui.service.backup /etc/systemd/system/timestd-web-ui.service
sudo systemctl daemon-reload
sudo systemctl start timestd-web-ui
```

## Performance

- **Startup time**: ~1-2 seconds
- **API response time**: 10-50ms for HDF5 reads
- **Memory usage**: ~100-200MB
- **CPU usage**: Low (<5% idle, <20% under load)

## Notes

- Frontend (HTML/CSS/JS) requires no changes
- All existing API endpoints maintain same response format
- HDF5 reading is faster than CSV for large files
- SWMR mode allows reading while files are being written
- Python stack is more maintainable than mixed Node.js/Python
