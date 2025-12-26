# Dependencies

Complete list of external libraries and system requirements for the GRAPE Signal Recorder.

## System Requirements

### Linux System Utilities

- **avahi-browse**: mDNS service discovery for finding radiod channels

  ```bash
  sudo apt-get install avahi-utils  # Ubuntu/Debian
  ```

- **h5clear**: HDF5 file lock recovery utility

  ```bash
  sudo apt-get install hdf5-tools   # Ubuntu/Debian
  ```

### ka9q-radio

- **radiod**: Required for receiving and demodulating radio signals
- Must be configured and running on the network

## Python Dependencies

### Core Requirements

Installed automatically with:

```bash
pip install -r requirements.txt
```

#### Configuration

- **toml** (>=0.10.2): Parse TOML configuration files

#### Numerical Computing

- **numpy** (>=1.24.0): Array operations and numerical computing
- **scipy** (>=1.10.0): Signal processing, resampling (12 kHz → 10 Hz, 8 kHz → 3 kHz), bandpass filtering

#### Data I/O

- **soundfile** (>=0.12.0): Audio file I/O (legacy compatibility)
- **digital_rf** (>=2.6.0): Digital RF format for HamSCI PSWS datasets

#### Network Discovery

- **zeroconf** (>=0.132.0): mDNS/Avahi service discovery for ka9q-radio streams

### Visualization (Optional)

For `plot_wwv_timing.py` script:

- **pandas** (>=2.0.0): Data analysis and CSV processing
- **matplotlib** (>=3.7.0): Plotting timing variations and distributions

Install with:

```bash
pip install pandas matplotlib
```

## Node.js Dependencies

### Web UI Server

Located in `web-ui/` directory.

Install with:

```bash
cd web-ui
npm install
# or
pnpm install
```

#### Production Dependencies

- **express** (^4.21.2): Web server for configuration UI and monitoring dashboard
- **toml** (^3.0.0): Parse TOML configuration files in Node.js

#### Development Dependencies

- **prettier** (^3.6.2): Code formatting

## Browser Dependencies (CDN)

### Monitoring Dashboard

No installation needed - loaded from CDN:

- **Chart.js** (4.4.0): Interactive graphs for WWV timing visualization
  - Used in WWV Timing tab for line charts and histograms
  - Loaded from: `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js`

## Installation Summary

### Recommended: Use Install Script

```bash
# Test mode (development)
./scripts/install.sh --mode test

# Production mode (24/7 operation)
sudo ./scripts/install.sh --mode production --user $USER
```

The installer automatically:

- Creates Python venv and installs all dependencies
- Installs Web UI Node.js dependencies
- Creates required directories
- Installs systemd services (production mode)

### Manual Setup (Alternative)

```bash
# 1. System packages
sudo apt-get install avahi-utils libhdf5-dev

# 2. Python environment
python3 -m venv venv
source venv/bin/activate
pip install -e .

# 3. Optional visualization tools
pip install pandas matplotlib

# 4. Web UI
cd web-ui
npm install  # or pnpm install
```

### Verify Installation

```bash
# Check Python dependencies
python -c "import numpy, scipy, toml, digital_rf; print('✅ Core deps OK')"
python -c "import pandas, matplotlib; print('✅ Visualization deps OK')"

# Check Node dependencies
cd web-ui && npm list
```

## Development vs. Production

### Core Recording (Production)

Minimal requirements:

- Python 3.10+
- numpy, scipy, toml
- digital_rf
- avahi-utils

### Full System (Development)

All dependencies including:

- Visualization tools (pandas, matplotlib)
- Web UI server (Node.js 18+, express)
- Monitoring dashboard (Chart.js)

## Troubleshooting

### digital_rf Installation

If `digital_rf` fails to install:

```bash
# Install system dependencies first
sudo apt-get install libhdf5-dev

# Then retry
pip install digital_rf
```

### Node.js Version

Requires Node.js 18 or later. Check with:

```bash
node --version
```

### Avahi/mDNS

If channel discovery fails:

```bash
# Check avahi service
systemctl status avahi-daemon

# Test manually
avahi-browse _ka9q-ctl._udp -t
```
