# GRAPE Signal Recorder - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 9, 2025

Complete setup guide for installing and configuring the GRAPE signal recorder.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [System Setup](#system-setup)
3. [ka9q-radio Installation](#ka9q-radio-installation)
4. [GRAPE Recorder Installation](#grape-recorder-installation)
5. [Web UI Setup](#web-ui-setup)
6. [Configuration](#configuration)
7. [First Run & Testing](#first-run--testing)
8. [Production Deployment](#production-deployment)
9. [PSWS Upload Setup](#psws-upload-setup)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Hardware Requirements
- **Receiver**: SDR supported by ka9q-radio
  - Recommended: RX888 MkII, Airspy HF+, SDRplay RSPdx
  - Minimum: Any RTL-SDR (reduced performance)
- **Antenna**: HF antenna covering 2.5-25 MHz
  - Dipole, long wire, or active antenna
- **Computer**: 
  - Linux system (Debian 11+, Ubuntu 20.04+, or similar)
  - 2+ GB RAM
  - 10+ GB disk space for data archive
  - Network with multicast support

### Software Requirements
- **Operating System**: Linux (Debian 11+ or Ubuntu 20.04+)
- **Python**: 3.10 or newer
- **Node.js**: 18 or newer (for web UI)
- **Git**: For cloning repository
- **SSH**: For PSWS uploads
- **ka9q-radio**: Running on this machine or accessible via multicast
- **ka9q-python**: Python interface to ka9q-radio (installed automatically)

---

## System Setup

### 1. Update System

```bash
sudo apt update
sudo apt upgrade -y
```

### 2. Install Build Dependencies

```bash
sudo apt install -y \
    build-essential \
    git \
    cmake \
    pkg-config \
    python3 \
    python3-pip \
    python3-venv \
    libhdf5-dev \
    libfftw3-dev \
    libblas-dev \
    liblapack-dev \
    gfortran \
    rsync \
    openssh-client
```

### 3. Install Node.js (for Web UI)

```bash
# Using NodeSource repository
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Verify
node --version  # Should be 18.x or newer
npm --version
```

### 4. Install pnpm (optional but recommended)

```bash
npm install -g pnpm
```

### 5. Configure Multicast Networking

**Check multicast support:**
```bash
ip mroute show
# Should show kernel multicast routes
```

**If needed, enable multicast:**
```bash
# Add to /etc/sysctl.conf
echo "net.ipv4.conf.all.mc_forwarding=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

**Configure firewall (if ufw is installed):**
```bash
# Only needed if ufw is active
sudo ufw allow 5004/udp   # RTP packets
sudo ufw allow 3000/tcp   # Web UI
```

---

## ka9q-radio Installation

### 1. Install Dependencies

```bash
sudo apt install -y \
    libavahi-client-dev \
    libavahi-common-dev \
    libopus-dev \
    libportaudio2 \
    portaudio19-dev \
    libncurses-dev \
    libfftw3-dev \
    libiniparser-dev
```

### 2. Clone and Build

```bash
cd ~
git clone https://github.com/ka9q/ka9q-radio.git
cd ka9q-radio
mkdir build && cd build
cmake ..
make -j$(nproc)
sudo make install
```

### 3. Verify Installation

```bash
which radiod
which control
which monitor
# All should return paths
```

### 4. Configure radiod

Create `/etc/radio/radiod@rx888.conf`:

```ini
[global]
hardware = rx888
description = GRAPE RX888 Station
status = myhost-hf-status.local    # mDNS name for status multicast
data = myhost-hf-data.local        # mDNS name for data multicast

[rx888]
device = 0
samprate = 64800000
antenna = HF
gain = 0

# GRAPE recorder creates channels dynamically
```

**Note:** The `status` and `data` entries use mDNS names (`.local`) which are published by radiod via Avahi. This allows the GRAPE recorder to discover radiod even if running on a different machine.

### 5. Start radiod

```bash
# Test run
sudo radiod -c /etc/radio/radiod@rx888.conf

# Set up as systemd service (recommended)
sudo systemctl enable radiod@rx888
sudo systemctl start radiod@rx888
```

---

## GRAPE Recorder Installation

### 1. Clone Repository

```bash
cd ~
git clone https://github.com/mijahauan/grape-recorder.git
cd grape-recorder
```

### 2. Create Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python Dependencies

```bash
# Upgrade pip
pip install --upgrade pip

# Install requirements
pip install -e .
```

This installs:
- `ka9q` - Interface to ka9q-radio (PyPI package)
- `digital_rf>=2.6.0` - Digital RF format support (HamSCI PSWS compatible)
- `scipy>=1.10.0` - Signal processing (decimation)
- `numpy>=1.24.0` - Array operations
- `zeroconf` - mDNS discovery for radiod
- `toml` - Configuration parsing

### 4. Verify Installation

```bash
# Test Python imports
python3 -c "import digital_rf; print('Digital RF OK')"
python3 -c "import scipy.signal; print('scipy OK')"
python3 -c "from ka9q import discover_channels; print('ka9q-python OK')"
```

### 5. Data Directories

**Test mode** (default): Data is stored in `/tmp/timestd-test/` - no setup required.

**Production mode**: When ready for permanent operation, data goes to `/var/lib/timestd/`. This requires:
```bash
sudo mkdir -p /var/lib/timestd
sudo chown -R $USER:$USER /var/lib/timestd
```

The mode is controlled by `recorder.mode` in `timestd-config.toml`.

---

## Web UI Setup

### 1. Install Node Dependencies

```bash
cd ~/signal-recorder/web-ui
pnpm install  # or: npm install
```

### 2. Create Data Directory

```bash
mkdir -p data
```

### 3. Test Web Server

```bash
npm start  # or: pnpm start
# Should show: "Server running on http://localhost:3000"
```

### 4. Access Web Interface

Open browser: `http://<hostname>:3000`

If running on the same machine, use `http://localhost:3000`.  
If running on a remote server, use the server's hostname or IP address.

- **Username**: `admin`
- **Password**: `admin`

**⚠️ Change default password in production!**

---

## Configuration

### Using Web UI (Recommended)

1. **Navigate to Configuration Tab**
2. **Fill in Station Information:**
   - Callsign (e.g., AC0G)
   - Grid square (e.g., EM38ww)
   - Instrument ID (e.g., RX888)
   - Description

3. **Add Channels:**
   - Click "Add WWV 2.5M", "Add WWV 10M", etc. for presets
   - Or manually add custom channels

4. **Configure PSWS Upload** (optional for now):
   - Remote host: `pswsnetwork.eng.ua.edu`
   - Remote user: (your PSWS username)
   - Remote path: `/var/psws/archive/data/GRAPE/<CALLSIGN>`
   - SSH key path: `~/.ssh/psws_key`

5. **Save Configuration:**
   - Click "Save Configuration"
   - File saved to: `config/grape-<callsign>.toml`

### Manual Configuration (Recommended for Beta)

Copy the template and edit:

```bash
cp config/timestd-config.toml.template config/timestd-config.toml
```

Key settings in `config/timestd-config.toml`:

```toml
[station]
callsign = "W1ABC"                    # Your callsign
grid_square = "FN31pr"                # Your Maidenhead grid

[ka9q]
status_address = "myhost-hf-status.local"  # mDNS name from radiod config

[recorder]
mode = "test"                         # Use "test" initially, "production" later
recording_interval = 60

[[recorder.channels]]
ssrc = 2500000
frequency_hz = 2500000
preset = "iq"
sample_rate = 16000
description = "WWV 2.5 MHz"
enabled = true

[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
preset = "iq"
sample_rate = 16000
description = "WWV 10 MHz"
enabled = true

[uploader]
enabled = false  # Enable after PSWS setup
protocol = "rsync"
remote_host = "pswsnetwork.eng.ua.edu"
remote_user = "user_ac0g"
remote_path = "/var/psws/archive/data/GRAPE/AC0G"
ssh_key_path = "~/.ssh/psws_key"
upload_interval = 300

[logging]
level = "INFO"
console_output = true
```

---

## First Run & Testing

### 1. Verify radiod is Accessible

```bash
cd ~/signal-recorder
source venv/bin/activate

# Test ka9q-python can discover radiod
python3 -c "
from ka9q import discover_channels
channels = discover_channels('myhost-hf-status.local')  # Your status address
print(f'Found {len(channels)} channels')
"
```

Replace `myhost-hf-status.local` with your radiod's status address.

### 2. Test Recording

```bash
cd ~/signal-recorder
source venv/bin/activate
python -m signal_recorder.grape_recorder --config config/timestd-config.toml
```

**Watch for:**
- "Starting RTP→Digital RF recorder for X channels"
- Periodic status updates showing:
  - Completeness: ~99%
  - Packet loss: <1%
  - Samples received

**Press Ctrl+C to stop after 5 minutes.**

### 3. Verify Output Files

In test mode, data is stored in `/tmp/timestd-test/`:

```bash
ls -lh /tmp/timestd-test/archives/
# Should show directories: WWV_2.5_MHz/, WWV_10_MHz/, CHU_3.33_MHz/, etc.

ls /tmp/timestd-test/archives/WWV_10_MHz/
# Should show .npz files with timestamps like: 20251128_143000_WWV_10_MHz.npz
```

### 4. Check Web UI

In a separate terminal:

```bash
cd ~/signal-recorder/web-ui
npm start
```

Open `http://<hostname>:3000` in your browser to see:
- Channel status and completeness
- Real-time quality metrics
- Spectrogram generation (once data accumulates)

---

## Production Deployment

Production mode provides 24/7 operation with systemd services, automatic restart, and daily uploads.

### Test vs Production Mode

| Aspect | Test Mode | Production Mode |
|--------|-----------|-----------------|
| **Data Root** | `/tmp/timestd-test` | `/var/lib/timestd` |
| **Config** | `project/config/timestd-config.toml` | `/etc/hf-timestd/timestd-config.toml` |
| **Logs** | `/tmp/timestd-test/logs/` | `/var/log/grape-recorder/` |
| **Startup** | Manual via scripts | Automatic via systemd |
| **Recovery** | Manual restart | Auto-restart on failure |

### Quick Production Install

```bash
# Run installer in production mode
sudo ./scripts/install.sh --mode production --user $USER

# Edit configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# Start continuous services (three-phase architecture)
sudo systemctl start timestd-core-recorder   # Phase 1: RTP → DRF
sudo systemctl start timestd-analytics       # Phase 2: Timing analysis
sudo systemctl start timestd-web-ui          # Web monitoring UI

# Start periodic timers (Phase 3 products)
sudo systemctl start grape-spectrograms.timer     # Every 10 minutes
sudo systemctl start grape-daily-upload.timer     # Daily 00:30 UTC

# All services are enabled on install (auto-start on boot)
# To check timer status:
sudo systemctl list-timers grape-*
```

### Production Directory Structure (FHS-Compliant)

| Path | Purpose |
|------|---------|
| `/var/lib/timestd/` | Data root (three-phase architecture) |
| `/var/lib/timestd/raw_archive/` | Phase 1: Immutable DRF archive |
| `/var/lib/timestd/phase2/` | Phase 2: Timing analysis outputs |
| `/var/lib/timestd/products/` | Phase 3: Decimated data, spectrograms |
| `/var/log/grape-recorder/` | Application logs |
| `/etc/hf-timestd/` | Configuration files |
| `/etc/hf-timestd/environment` | Environment variables for all services |
| `/opt/grape-recorder/` | Python venv and Web UI |

### Systemd Services

**Continuous Services** (run 24/7):

| Service | Description | Phase |
|---------|-------------|-------|
| `timestd-core-recorder.service` | RTP → Digital RF archive | Phase 1 |
| `timestd-analytics.service` | Timing analysis (all 9 channels + fusion) | Phase 2 |
| `timestd-web-ui.service` | Web monitoring UI | UI |

**Periodic Timers** (Phase 3 products):

| Timer | Interval | Description |
|-------|----------|-------------|
| `grape-spectrograms.timer` | Every 10 min | Daily spectrograms with solar zenith (all channels) |
| `grape-daily-upload.timer` | Daily 00:30 UTC | Package 10 Hz DRF + upload to PSWS |

### Service Management

```bash
# Check status
sudo systemctl status timestd-core-recorder timestd-analytics timestd-web-ui

# View logs
journalctl -u timestd-core-recorder -f   # Phase 1 logs
journalctl -u timestd-analytics -f       # Phase 2 logs
journalctl -u timestd-web-ui -f          # Web UI logs

# Restart after config change
sudo systemctl restart timestd-core-recorder timestd-analytics

# Stop all services
sudo systemctl stop timestd-core-recorder timestd-analytics timestd-web-ui
```

### Transitioning from Test Mode

If already running in test mode:

```bash
# Stop test services
./scripts/timestd-all.sh -stop

# Run production installer
sudo ./scripts/install.sh --mode production --user $USER

# Copy your existing config
sudo cp config/timestd-config.toml /etc/hf-timestd/

# Update mode in config
sudo sed -i 's/mode = "test"/mode = "production"/' /etc/hf-timestd/timestd-config.toml

# Start production services
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
```

### Switching Between Modes

The mode is controlled by `recorder.mode` in `timestd-config.toml`:

```toml
[recorder]
mode = "test"           # or "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"
```

All paths are automatically determined by the mode:
- Python code reads mode via `paths.py` → `load_paths_from_config()`
- Shell scripts read mode via `common.sh` → `get_data_root()`
- Systemd services read from `/etc/hf-timestd/environment`

---

## PSWS Upload Setup

### 1. Request PSWS Account

Email HamSCI GRAPE coordinators via [grape@hamsci.groups.io](mailto:grape@hamsci.groups.io):

```
Subject: PSWS Account Request - <YOUR_CALLSIGN>

I'd like to contribute GRAPE timing data from my station:

Callsign: <YOUR_CALLSIGN>
Grid Square: <YOUR_GRID>
Instrument: <RX888/Airspy/etc>
Location: <City, State/Country>

I have the GRAPE recorder installed and collecting data.
Please provide PSWS credentials for rsync uploads.
```

### 2. Generate SSH Key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/psws_key -C "grape-<callsign>"
# No passphrase (for unattended uploads)
```

### 3. Send Public Key to PSWS

```bash
cat ~/.ssh/psws_key.pub
# Copy output and send to PSWS admin
```

### 4. Test SSH Access

```bash
ssh -i ~/.ssh/psws_key user_<callsign>@pswsnetwork.eng.ua.edu
# Should connect without password
```

### 5. Enable Uploads in Config

Edit `config/grape-<callsign>.toml`:

```toml
[uploader]
enabled = true  # Change from false
protocol = "rsync"
remote_host = "pswsnetwork.eng.ua.edu"
remote_user = "user_<callsign>"
remote_path = "/var/psws/archive/data/GRAPE/<CALLSIGN>"
ssh_key_path = "~/.ssh/psws_key"
upload_interval = 300  # 5 minutes
```

### 6. Restart Recorder

Restart the recorder process (Ctrl+C and re-run, or restart your screen/tmux session).

### 7. Monitor Uploads

```bash
# Check upload logs
tail -f /tmp/signal-recorder-upload.log

# Or use web UI
# Open http://<hostname>:3000 in browser
```

---

## Troubleshooting

### No Channels Discovered

**Symptom**: Recorder can't find radiod

**Solutions:**
```bash
# 1. Check radiod is running
sudo systemctl status radiod@rx888

# 2. Verify mDNS name resolves
avahi-resolve -n myhost-hf-status.local

# 3. Test ka9q-python discovery
cd ~/signal-recorder && source venv/bin/activate
python3 -c "
from ka9q import discover_channels
try:
    channels = discover_channels('myhost-hf-status.local')
    print(f'Found {len(channels)} channels')
except Exception as e:
    print(f'Error: {e}')
"

# 4. Check network interface supports multicast
ip link show
# Look for "MULTICAST" flag
```

### RTP Packets Not Received

**Symptom**: Recorder starts but shows 0% completeness

**Solutions:**
```bash
# 1. Check RTP multicast traffic is arriving
sudo tcpdump -i any -n udp port 5004
# Should show RTP packets

# 2. Check firewall (if ufw is installed)
sudo ufw status
sudo ufw allow 5004/udp

# 3. Verify SSRC in config matches radiod channel configuration
# The SSRC should match the frequency (e.g., 10000000 for 10 MHz)
```

### Low Completeness (<95%)

**Symptom**: Recorder shows <95% completeness

**Causes & Solutions:**

**Network congestion:**
```bash
# Check dropped packets
ifconfig
# Look for "RX dropped" count

# Increase buffer size
sudo sysctl -w net.core.rmem_max=26214400
sudo sysctl -w net.core.rmem_default=26214400
```

**CPU overload:**
```bash
# Check CPU usage
top
# signal-recorder should use <50% per channel

# Reduce number of channels if needed
```

**Multicast routing:**
```bash
# Ensure multicast routes exist
ip mroute show

# Add route if missing
sudo route add -net 239.0.0.0 netmask 255.0.0.0 dev eth0
```

### Timing Drift High (>50ms)

**Symptom**: Dashboard shows timing drift > ±50ms

**Solutions:**
```bash
# 1. Verify NTP is running
timedatectl
# Should show "System clock synchronized: yes"

# 2. Enable NTP if needed
sudo systemctl enable systemd-timesyncd
sudo systemctl start systemd-timesyncd

# 3. Check NTP sync quality
ntpq -p  # If using ntpd
timedatectl timesync-status  # If using systemd-timesyncd

# 4. Wait for NTP to stabilize
# Timing drift should improve after 10-15 minutes
```

### PSWS Upload Fails

**Symptom**: Upload logs show errors

**Solutions:**
```bash
# 1. Test SSH key
ssh -i ~/.ssh/psws_key user_<callsign>@pswsnetwork.eng.ua.edu

# 2. Check SSH key permissions
chmod 600 ~/.ssh/psws_key

# 3. Verify remote path exists
ssh -i ~/.ssh/psws_key user_<callsign>@pswsnetwork.eng.ua.edu \
    "ls -ld /var/psws/archive/data/GRAPE/<CALLSIGN>"

# 4. Test manual rsync (adjust path for test vs production mode)
rsync -avz --dry-run \
    -e "ssh -i ~/.ssh/psws_key" \
    /tmp/timestd-test/archives/WWV_10_MHz/ \
    user_<callsign>@pswsnetwork.eng.ua.edu:/var/psws/archive/data/GRAPE/<CALLSIGN>/WWV_10_MHz/
```

### Web UI Won't Start

**Symptom**: `npm start` fails

**Solutions:**
```bash
# 1. Check Node.js version
node --version  # Should be 18+

# 2. Reinstall dependencies
cd ~/signal-recorder/web-ui
rm -rf node_modules
npm install

# 3. Check port 3000 availability
ss -tlnp | grep 3000

# 4. Try different port
PORT=3001 npm start
```

---

## Next Steps

After successful installation:

1. **Monitor for 24 hours** - Verify healthy status in web UI
2. **Enable PSWS uploads** - After SSH key setup
3. **Join HamSCI community** - [grape@hamsci.groups.io](mailto:grape@hamsci.groups.io)

---

## Getting Help

### Documentation
- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Configuration**: [docs/configuration.md](docs/configuration.md)
- **Web UI**: [web-ui/README.md](web-ui/README.md)

### Community Support
- **HamSCI Groups.io**: [grape@hamsci.groups.io](mailto:grape@hamsci.groups.io)
- **GitHub Issues**: Report bugs and request features

### Diagnostic Data to Include

When asking for help, provide:

```bash
# System info
uname -a
python3 --version
node --version

# Check if radiod is running
sudo systemctl status radiod@rx888

# Configuration (redact any sensitive info)
cat config/timestd-config.toml

# Check output files exist
ls -la /tmp/timestd-test/archives/
```

---

**🎯 Installation complete! Your GRAPE station is now ready to contribute to ionospheric science.**
