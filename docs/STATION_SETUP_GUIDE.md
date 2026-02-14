# Station Setup Guide

This guide covers the site-specific configuration required for a new hf-timestd installation.

---

## Quick Reference: Required Configuration

After running `install.sh`, edit `/etc/hf-timestd/timestd-config.toml` and update these fields:

| Section | Field | Description | Example |
|---------|-------|-------------|---------|
| `[station]` | `callsign` | Your amateur radio callsign | `"W1ABC"` |
| `[station]` | `grid_square` | 10-character Maidenhead locator | `"FN31pr42ab"` |
| `[station]` | `latitude` | Decimal degrees (positive = North) | `42.3601` |
| `[station]` | `longitude` | Decimal degrees (positive = East) | `-71.0589` |
| `[station]` | `id` | PSWS station ID (if uploading) | `"S000171"` |
| `[station]` | `instrument_id` | PSWS instrument ID (if uploading) | `"172"` |
| `[ka9q]` | `status_address` | Your radiod status multicast address | `"hf-status.local"` |
| `[gnss_vtec]` | `host` | GNSS receiver IP (if using VTEC) | `"192.168.0.202"` |
| `[gnss_vtec]` | `port` | GNSS data stream port | `9000` |

---

## 1. Station Identity

### Callsign

Your amateur radio callsign. Used for identification in data products and uploads.

```toml
[station]
callsign = "W1ABC"
```

### Geographic Location

**Latitude and Longitude** are **required** for the physics propagation model. The system uses these to calculate:
- Ionospheric pierce points for each HF path
- Expected propagation delays
- TEC corrections

```toml
[station]
latitude = 42.3601    # Decimal degrees, positive = North
longitude = -71.0589  # Decimal degrees, positive = East
```

**Finding your coordinates:**
- Google Maps: Right-click → "What's here?" → Copy coordinates
- GPS receiver: Read from NMEA stream
- Online: [latlong.net](https://www.latlong.net/)

### Maidenhead Grid Square

The 10-character Maidenhead locator provides precise location encoding.

```toml
[station]
grid_square = "FN31pr42ab"
```

**Converting lat/lon to grid square:**

```python
# Python one-liner
from maidenhead import to_maiden
print(to_maiden(42.3601, -71.0589, precision=5))  # → "FN42ab12cd"
```

Or use online converters:
- [levinecentral.com/ham/grid_square.php](http://www.levinecentral.com/ham/grid_square.php)
- [qrz.com/gridmapper](https://www.qrz.com/gridmapper)

---

## 2. PSWS Network Registration (Optional)

If you plan to upload data to the Personal Space Weather Station (PSWS) network, you need a station ID and instrument ID.

### Obtaining PSWS IDs

1. **Register at**: [pswsnetwork.org](https://pswsnetwork.org/)
2. **Request a station ID** (format: `S000XXX`)
3. **Register your instrument** to get an instrument ID (numeric)

### Configuration

```toml
[station]
id = "S000171"           # Your PSWS station ID
instrument_id = "172"    # Your PSWS instrument ID
```

### SSH Key Setup for Uploads

If uploading via SFTP:

```bash
# Generate SSH key for PSWS uploads
ssh-keygen -t ed25519 -f ~/.ssh/psws_key -N ""

# Send public key to PSWS administrators
cat ~/.ssh/psws_key.pub
```

Configure the uploader:

```toml
[uploader]
enabled = true
protocol = "sftp"

[uploader.sftp]
host = "pswsnetwork.eng.ua.edu"
port = 22
user = "S000171"              # Same as station.id
ssh_key = "~/.ssh/psws_key"
```

---

## 3. ka9q-radio Configuration

The system receives IQ data from ka9q-radio (radiod). You need to configure the status multicast address.

### Finding Your radiod Status Address

```bash
# Option 1: Use avahi/mDNS discovery
avahi-browse -rt _ka9q-ctl._udp

# Option 2: Check your radiod config file
grep status /etc/radio/*.conf
```

### Configuration

```toml
[ka9q]
status_address = "hf-status.local"  # Or "239.x.x.x" multicast address
auto_create_channels = true
```

**Note:** The `data_address` is auto-discovered from the status stream; leave it empty.

---

## 4. GNSS VTEC Configuration (Optional)

If you have a u-blox ZED-F9P or similar dual-frequency GNSS receiver, you can enable local VTEC monitoring for improved ionospheric corrections.

### Hardware Setup

See [ZED_F9P_TEC_CONFIGURATION.md](ZED_F9P_TEC_CONFIGURATION.md) for detailed receiver configuration.

**Typical setup:**
1. ZED-F9P connected via USB or network
2. UBX protocol enabled (specifically UBX-NAV-SAT for ionospheric delay)
3. Data stream accessible via TCP (using ser2net or similar)

### Network Streaming with ser2net

If your GNSS receiver is connected via USB, use ser2net to expose it over TCP:

```bash
# Install ser2net
sudo apt install ser2net

# Add to /etc/ser2net.yaml:
connection: &gnss
  accepter: tcp,9000
  connector: serialdev,/dev/ttyACM0,115200n81,local
  options:
    kickolduser: true
```

### Configuration

```toml
[gnss_vtec]
enabled = true
host = "192.168.0.202"    # IP of GNSS receiver or ser2net host
port = 9000               # TCP port for UBX data stream
save_csv = true
csv_path = "data/gnss_vtec.csv"
save_hdf5 = true
hdf5_path = "data/gnss_vtec"
```

### Verification

```bash
# Test GNSS connection
nc -v 192.168.0.202 9000 | xxd | head

# Look for UBX frames (start with b5 62)
# If you see only NMEA ($GP...), UBX output needs to be enabled
```

---

## 5. Channel Selection

The default configuration monitors all standard HF time station frequencies. Adjust based on your location and reception conditions.

### Shared Frequencies (WWV + WWVH + BPM)

These frequencies carry multiple time stations:

| Frequency | Stations |
|-----------|----------|
| 2.5 MHz | WWV, WWVH, BPM |
| 5 MHz | WWV, WWVH, BPM |
| 10 MHz | WWV, WWVH, BPM |
| 15 MHz | WWV, WWVH, BPM |

### WWV-Only Frequencies

| Frequency | Station |
|-----------|---------|
| 20 MHz | WWV only |
| 25 MHz | WWV only |

### CHU Frequencies (Canada)

Useful for stations in northern US and Canada:

| Frequency | Station |
|-----------|---------|
| 3.33 MHz | CHU |
| 7.85 MHz | CHU |
| 14.67 MHz | CHU |

### Disabling Channels

To disable a channel you can't receive well:

```toml
[[recorder.channels]]
frequency_hz = 25000000
description = "WWV_25000"
enabled = false  # Add this line
```

---

## 6. Verification Checklist

After configuration, verify your setup:

### 1. Check Configuration Syntax

```bash
python3 -c "import tomllib; tomllib.load(open('/etc/hf-timestd/timestd-config.toml', 'rb'))"
```

### 2. Verify radiod Connection

```bash
# Should show your configured channels
/opt/hf-timestd/venv/bin/python -c "
from ka9q import Radio
r = Radio('hf-status.local')
print(f'Connected to: {r.name}')
print(f'Channels: {len(r.channels)}')
"
```

### 3. Test GNSS Connection (if enabled)

```bash
# Should show UBX data
timeout 5 nc 192.168.0.202 9000 | xxd | grep "b5 62"
```

### 4. Start Services

```bash
sudo systemctl start timestd-core-recorder
sudo systemctl start timestd-metrology
sudo systemctl start timestd-l2-calibration
sudo systemctl start timestd-fusion
sudo systemctl start timestd-web-api

# Check status
sudo systemctl status timestd-*
```

### 5. Verify Data Flow

```bash
# Raw buffer (should show recent files)
ls -lt /var/lib/timestd/raw_buffer/*/$(date +%Y%m%d)/ | head

# Web UI
curl -s http://localhost:8000/api/health/system | python3 -m json.tool
```

---

## Common Issues

### "No channels found"

- Check `status_address` in `[ka9q]` section
- Verify radiod is running: `systemctl status radiod@*`
- Check multicast routing: `ip route show table all | grep 239`

### "GNSS connection refused"

- Verify GNSS receiver is powered and connected
- Check ser2net is running: `systemctl status ser2net`
- Test port: `nc -zv <host> <port>`

### "Chrony not seeing TSL1/TSL2"

- Ensure timestd-fusion is running before chronyd
- Check SHM permissions: `ls -la /dev/shm/NTP*`
- Verify chrony config includes refclock lines

---

## Example Complete Configuration

```toml
# /etc/hf-timestd/timestd-config.toml

[station]
callsign = "W1ABC"
grid_square = "FN42ab12cd"
id = "S000999"
instrument_id = "999"
description = "RX888 MkII with 80m dipole"
latitude = 42.3601
longitude = -71.0589

[ka9q]
status_address = "hf-status.local"
auto_create_channels = true

[recorder]
mode = "production"
production_data_root = "/var/lib/timestd"
compression = "zstd"
tiered_storage = true

[recorder.channel_defaults]
sample_rate = 24000

[[recorder.channels]]
frequency_hz = 5000000
description = "SHARED_5000"

[[recorder.channels]]
frequency_hz = 10000000
description = "SHARED_10000"

[[recorder.channels]]
frequency_hz = 15000000
description = "SHARED_15000"

[gnss_vtec]
enabled = true
host = "192.168.0.202"
port = 9000

[web_ui]
port = 8000
```

---

## Related Documentation

- [INSTALLATION.md](../INSTALLATION.md) — Full installation guide
- [ZED_F9P_TEC_CONFIGURATION.md](ZED_F9P_TEC_CONFIGURATION.md) — GNSS receiver setup
- [GPS_TEC_OPTIONAL.md](GPS_TEC_OPTIONAL.md) — Optional GPS TEC capabilities and VTEC architecture
- [NASA_EARTHDATA_SETUP.md](NASA_EARTHDATA_SETUP.md) — IONEX data access setup
