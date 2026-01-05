# Solar Correlation System - Deployment Guide

## Prerequisites

- Python 3.11+
- FastAPI web-api service already running
- Internet connectivity to NOAA SWPC (services.swpc.noaa.gov)
- Sufficient disk space for cache directory (~100 MB)

## Deployment Steps

### 1. Install Dependencies

```bash
cd /home/mjh/git/hf-timestd/web-api
pip install -r requirements.txt
```

New dependencies added:
- `requests>=2.31.0` - HTTP client for NOAA API
- `scipy>=1.11.0` - Statistical analysis (correlation, regression)

### 2. Create Cache Directory

```bash
sudo mkdir -p /var/lib/timestd/space_weather_cache
sudo chown timestd:timestd /var/lib/timestd/space_weather_cache
sudo chmod 755 /var/lib/timestd/space_weather_cache
```

### 3. Copy Files to Production

If deploying to `/opt/hf-timestd/web-api/`:

```bash
cd /home/mjh/git/hf-timestd/web-api

# Copy new service files
sudo cp services/space_weather_service.py /opt/hf-timestd/web-api/services/
sudo cp services/correlation_service.py /opt/hf-timestd/web-api/services/

# Copy new router files
sudo cp routers/space_weather.py /opt/hf-timestd/web-api/routers/
sudo cp routers/correlations.py /opt/hf-timestd/web-api/routers/

# Update existing files
sudo cp routers/__init__.py /opt/hf-timestd/web-api/routers/
sudo cp main.py /opt/hf-timestd/web-api/

# Copy frontend
sudo cp static/solar-correlation.html /opt/hf-timestd/web-api/static/
sudo cp static/index.html /opt/hf-timestd/web-api/static/

# Copy documentation
sudo cp SOLAR_CORRELATION_README.md /opt/hf-timestd/web-api/
sudo cp requirements.txt /opt/hf-timestd/web-api/

# Set permissions
sudo chown -R timestd:timestd /opt/hf-timestd/web-api/
```

### 4. Restart Web API Service

```bash
sudo systemctl restart timestd-web-api
```

### 5. Verify Service Status

```bash
# Check service is running
sudo systemctl status timestd-web-api

# Check logs for any errors
sudo journalctl -u timestd-web-api -n 50 --no-pager

# Look for successful startup messages
sudo journalctl -u timestd-web-api | grep -i "space weather"
```

### 6. Test API Endpoints

```bash
# Test from command line
curl http://localhost:8000/api/space-weather/current | jq

# Or run the test script
cd /home/mjh/git/hf-timestd/web-api
python3 test_solar_api.py
```

Expected output:
```json
{
  "timestamp": "2026-01-05T00:30:00Z",
  "xray": {
    "timestamp": "2026-01-05T00:25:00Z",
    "flux": 1.5e-06,
    "class": "C1.5",
    "satellite": 16
  },
  "kp": {
    "timestamp": "2026-01-05 00:00:00.000000",
    "kp": 2.3,
    "kp_index": 2,
    "observed": "observed"
  },
  "alerts": []
}
```

### 7. Access Frontend

Open browser to:
```
http://localhost:8000/static/solar-correlation.html
```

Or navigate from main page:
```
http://localhost:8000/ → ☀️ Solar Correlation
```

## Verification Checklist

- [ ] Service starts without errors
- [ ] Cache directory created and writable
- [ ] Space weather data fetching successfully
- [ ] API endpoints responding (test with curl or test script)
- [ ] Frontend loads and displays data
- [ ] Charts render correctly
- [ ] No JavaScript errors in browser console
- [ ] Auto-refresh works (if enabled)

## Troubleshooting

### Service Won't Start

**Check logs:**
```bash
sudo journalctl -u timestd-web-api -n 100 --no-pager
```

**Common issues:**
- Import errors: Missing dependencies (run `pip install -r requirements.txt`)
- Permission errors: Cache directory not writable
- Port conflict: Port 8000 already in use

### No Space Weather Data

**Test NOAA API directly:**
```bash
curl https://services.swpc.noaa.gov/json/goes/xray-fluxes-7-day.json | head -n 20
```

**Check cache:**
```bash
ls -lh /var/lib/timestd/space_weather_cache/
cat /var/lib/timestd/space_weather_cache/xray_7day.json | jq
```

**Common issues:**
- Network connectivity to NOAA SWPC
- Firewall blocking outbound HTTPS
- NOAA API temporarily unavailable (service uses cached data)

### Frontend Not Loading

**Check static files:**
```bash
ls -l /opt/hf-timestd/web-api/static/solar-correlation.html
```

**Check browser console:**
- Open Developer Tools (F12)
- Look for 404 errors or JavaScript errors
- Verify API calls are succeeding (Network tab)

**Common issues:**
- File not copied to production directory
- Incorrect permissions on static files
- CORS issues (should not occur with same-origin)

### Correlation Analysis Returns "No Data"

**Verify propagation data exists:**
```bash
ls -lh /var/lib/timestd/phase2/*/clock_offset/*.h5
```

**Check time range:**
- Try longer time range (48-72 hours)
- Ensure selected station/frequency has recent data

**Check logs:**
```bash
sudo journalctl -u timestd-web-api | grep -i correlation
```

## Performance Monitoring

### API Response Times

Expected response times (24-hour queries):
- `/space-weather/current`: < 100ms (cached)
- `/space-weather/xray`: < 200ms (cached)
- `/correlations/snr-solar`: < 500ms (computation)
- `/correlations/sid-detection`: < 1000ms (computation)

### Cache Hit Rates

Monitor cache effectiveness:
```bash
# Check cache file timestamps
ls -lht /var/lib/timestd/space_weather_cache/

# Should update every 15 minutes
```

### Memory Usage

Space weather service is lightweight:
- Cache size: ~1-5 MB per data type
- Memory footprint: ~50 MB additional to web-api

## Maintenance

### Cache Cleanup

Cache files are automatically overwritten. Manual cleanup if needed:
```bash
sudo rm -f /var/lib/timestd/space_weather_cache/*.json
```

### Log Rotation

Ensure journald or syslog is configured for log rotation:
```bash
sudo journalctl --vacuum-time=7d
```

### Monitoring Recommendations

1. **Alert on service failures:**
   ```bash
   systemctl status timestd-web-api | grep -q "active (running)" || echo "ALERT: Web API down"
   ```

2. **Monitor NOAA API availability:**
   ```bash
   curl -s -o /dev/null -w "%{http_code}" https://services.swpc.noaa.gov/json/goes/xray-fluxes-7-day.json
   ```

3. **Check for space weather alerts:**
   ```bash
   curl -s http://localhost:8000/api/space-weather/current | jq '.alerts'
   ```

## Rollback Procedure

If issues occur, rollback to previous version:

```bash
# Stop service
sudo systemctl stop timestd-web-api

# Remove new files
sudo rm /opt/hf-timestd/web-api/services/space_weather_service.py
sudo rm /opt/hf-timestd/web-api/services/correlation_service.py
sudo rm /opt/hf-timestd/web-api/routers/space_weather.py
sudo rm /opt/hf-timestd/web-api/routers/correlations.py

# Restore previous versions from git
cd /home/mjh/git/hf-timestd
git checkout HEAD~1 web-api/routers/__init__.py
git checkout HEAD~1 web-api/main.py
git checkout HEAD~1 web-api/static/index.html

sudo cp web-api/routers/__init__.py /opt/hf-timestd/web-api/routers/
sudo cp web-api/main.py /opt/hf-timestd/web-api/
sudo cp web-api/static/index.html /opt/hf-timestd/web-api/static/

# Restart service
sudo systemctl start timestd-web-api
```

## Next Steps

After successful deployment:

1. Monitor for 24 hours to ensure stability
2. Review space weather alerts and correlation results
3. Consider implementing Phase 2 features (F10.7, Dst, automated notifications)
4. Collect user feedback on visualization and analysis tools

## Support

For issues or questions:
- Check logs: `sudo journalctl -u timestd-web-api`
- Review documentation: `SOLAR_CORRELATION_README.md`
- Test API: `python3 test_solar_api.py`
