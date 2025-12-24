# TEC Measurement - Optional GPS Validation

## Overview

The `hf-timestd` TEC measurement capability is **fully functional without any GPS hardware**. The system calculates ionospheric Total Electron Content (TEC) from multi-frequency HF timing measurements alone.

GPS validation is **entirely optional** and only needed if you want to:

- Validate HF TEC against GPS TEC maps
- Apply local GPS bias corrections
- Compare HF and GPS ionospheric measurements

---

## Default Operation (No GPS Required)

### What Works Without GPS

✅ **Multi-frequency HF TEC calculation**

- Measures TEC from 3-6 HF frequencies per station
- 1-minute cadence measurements
- Automatic obliquity factor corrections
- Data output: `/var/lib/timestd/phase2/science/tec/`

✅ **Science Aggregator Service**

- Runs automatically via `timestd-science-aggregator.service`
- Aggregates timing data across all frequencies
- Generates TEC estimates every 5 minutes
- No GPS dependencies

✅ **Multi-station Analysis**

- Compare TEC across stations (WWV, WWVH, CHU, BPM)
- Detect ionospheric events (TIDs, solar flares)
- Analyze diurnal TEC variations
- Monitor propagation conditions

### Verification (No GPS)

```bash
# Check TEC data is generating
ls -lh /var/lib/timestd/phase2/science/tec/
tail /var/lib/timestd/phase2/science/tec/tec_$(date +%Y%m%d).csv

# Check service status
sudo systemctl status timestd-science-aggregator
```

---

## Optional GPS Validation

GPS validation provides two capabilities:

1. **IONEX Maps**: Global GPS TEC from NASA/IGS (internet required)
2. **Local GPS**: Real-time TEC from your own GPS receiver (hardware required)

Both are **completely optional** and independent of HF TEC measurement.

---

## Option 1: IONEX GPS TEC Maps (No Hardware)

### What You Get

- Global GPS TEC maps (2.5° × 5° grid)
- 2-hour cadence
- Validate HF TEC against GPS reference
- No hardware required (internet only)

### Requirements

- Internet connection
- NASA Earthdata account (free)
- Python packages: `requests`, `numpy`

### Setup Instructions

#### 1. Create NASA Earthdata Account

```bash
# Visit: https://urs.earthdata.nasa.gov/
# Register for free account
# Note your username and password
```

#### 2. Configure Authentication

**Option A: Register Application** (Recommended for automated use)

```bash
# Visit: https://urs.earthdata.nasa.gov/apps/new
# Create new application
# Note client ID and secret
# Configure in script
```

**Option B: User Credentials** (Manual use)

```bash
# Create ~/.netrc
cat > ~/.netrc << EOF
machine urs.earthdata.nasa.gov
login YOUR_USERNAME
password YOUR_PASSWORD
EOF
chmod 600 ~/.netrc
```

#### 3. Download IONEX Data

```bash
# Activate environment
source /opt/hf-timestd/venv/bin/activate

# Download IONEX for specific date
python3 scripts/ionex_integration.py 2024-12-20

# Output: /tmp/ionex/jplg*.25i.Z
```

#### 4. Run Validation

```bash
# Compare HF TEC with GPS TEC
python3 scripts/validate_tec.py --date 2024-12-20 --station WWV

# View results
ls -lh /tmp/tec_validation/
cat /tmp/tec_validation/validation_report_WWV_20241220.txt
```

### Current Status

⏸️ **Blocked**: NASA Earthdata requires OAuth application registration

- User credentials configured but OAuth flow needed
- Alternative: Use IGS IONEX (different URL, no auth)
- See `scripts/ionex_integration.py` for implementation

---

## Option 2: Local GPS TEC (Hardware Required)

### What You Get

- Real-time local GPS TEC measurements
- Sub-minute cadence
- Local bias correction for HF TEC
- Independent ionospheric monitoring

### Hardware Requirements

**Supported GPS Receivers**:

1. **u-blox ZED-F9P** (Recommended)
   - High-precision GNSS receiver
   - UBX-NAV-SAT message support
   - USB or UART interface
   - ~$200-300

2. **ScintPi** (Alternative)
   - Raspberry Pi-based ionospheric monitor
   - Direct TEC output
   - ~$500-1000

### Setup Instructions (ZED-F9P)

#### 1. Hardware Connection

```bash
# Option A: Direct USB
# Connect ZED-F9P via USB-C to server

# Option B: Network (if using GPS timeserver)
# Configure separate TCP port for TEC data
# Example: Port 2001 for hf-timestd, Port 2000 for gpsd
```

#### 2. Configure ZED-F9P

```bash
# Activate environment
source /opt/hf-timestd/venv/bin/activate

# Configure via USB
python3 scripts/config_zedf9p_usb.py

# This enables:
# - UBX-NAV-SAT messages (ionospheric delay)
# - Saves configuration to flash
# - Sets up TCP stream (if using network)
```

#### 3. Verify Data Stream

```bash
# Test NAV-SAT message reception
python3 scripts/test_nav_sat.py

# Expected output:
# Connected! Looking for NAV-SAT...
# ✅ NAV-SAT: 33 satellites, TEC data present
```

#### 4. Extract GPS TEC

```bash
# Run GPS TEC client
python3 scripts/zedf9p_tec_client.py --host 192.168.0.202 --port 2001

# Output: Real-time GPS TEC measurements
```

#### 5. Run Validation with Local GPS

```bash
# Compare HF TEC with local GPS TEC
python3 scripts/validate_tec.py \
    --date 2024-12-20 \
    --station WWV \
    --gps-host 192.168.0.202 \
    --gps-port 2001

# Applies local GPS bias correction to HF TEC
```

### Current Status

✅ **Hardware Configured**: ZED-F9P on port 2001

- 33 satellites visible
- UBX-NAV-SAT streaming (404-byte frames)
⏸️ **Parser Debugging**: TEC extraction needs refinement
- See `scripts/zedf9p_tec_client.py` for implementation

---

## Configuration Files

### GPS TEC Client Configuration

Edit `scripts/zedf9p_tec_client.py`:

```python
# Default configuration
DEFAULT_HOST = '192.168.0.202'  # GPS receiver IP
DEFAULT_PORT = 2001              # TEC data port
MIN_ELEVATION = 30               # Minimum satellite elevation (degrees)
```

### Validation Script Configuration

Edit `scripts/validate_tec.py`:

```python
# GPS TEC source (optional)
GPS_TEC_SOURCE = 'ionex'  # or 'local' or None
IONEX_DIR = '/tmp/ionex'
LOCAL_GPS_HOST = '192.168.0.202'
LOCAL_GPS_PORT = 2001
```

---

## Troubleshooting

### IONEX Download Fails

**Symptom**: 404 errors or authentication failures

**Solutions**:

1. Check NASA Earthdata credentials
2. Verify date (IONEX has 1-3 day latency)
3. Try alternative source (IGS, CODE)
4. Register OAuth application

### GPS TEC Parser Timeout

**Symptom**: `test_nav_sat.py` times out

**Solutions**:

1. Verify GPS receiver is streaming: `nc <host> <port>`
2. Check for mixed NMEA/UBX data
3. Ensure gpsd isn't consuming the stream
4. Debug buffer synchronization in parser

### No Satellites Visible

**Symptom**: NAV-SAT shows 0 satellites

**Solutions**:

1. Check GPS antenna connection
2. Verify antenna has clear sky view
3. Wait for GPS lock (can take 5-10 minutes)
4. Check UBX configuration with u-center

---

## Summary

### Default (No GPS)

```
HF Receivers → Phase 2 Analytics → Clock Offset CSVs
                                          ↓
                              Science Aggregator
                                          ↓
                              HF TEC Measurements ✅
```

### With IONEX GPS (Optional)

```
HF TEC Measurements + IONEX GPS Maps → Validation
                                            ↓
                                    Bias Correction
                                    Accuracy Metrics
```

### With Local GPS (Optional)

```
HF TEC Measurements + Local GPS TEC → Real-time Validation
                                            ↓
                                    Local Bias Correction
                                    Independent Monitoring
```

**Key Point**: GPS validation is **entirely optional**. The HF TEC measurement capability is fully functional and scientifically valid without any GPS hardware or data.

---

## References

- **ZED-F9P Documentation**: `docs/ZED_F9P_TEC_CONFIGURATION.md`
- **Validation Methodology**: `docs/TEC_VALIDATION_METHODOLOGY.md`
- **Deployment Guide**: `docs/TEC_VALIDATION_DEPLOYMENT.md`
- **UBX Protocol**: u-blox Interface Description (UBX-NAV-SAT)
- **IONEX Format**: ftp://igs.org/pub/data/format/ionex1.pdf
