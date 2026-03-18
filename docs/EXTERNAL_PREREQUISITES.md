# External Prerequisites

hf-timestd depends on several external components that cannot be installed
automatically by `deploy.sh`.  This document is the single reference for
**everything a new operator must arrange before or during a first install**.

Each item is marked **Required** or **Optional**.  Optional items degrade
gracefully — the system runs without them but loses specific capabilities.

---

## Quick-Reference Matrix

| # | External | Required? | What You Get | Without It |
|---|----------|-----------|--------------|------------|
| 1 | [ka9q-radio](#1-ka9q-radio-radiod) | **Yes** | IQ data from SDR | Nothing works |
| 2 | [Hardware](#2-hardware) | **Yes** | RF front-end + timing | Nothing works |
| 3 | [PHaRLAP 4.7.4](#3-pharlap-474-numerical-raytracing) | No | Numerical ionospheric raytracing (pyLAP) | Geometric propagation model only |
| 4 | [NASA Earthdata](#4-nasa-earthdata-account) | No | IONEX global TEC maps, DCB corrections | Parametric IRI / zero-bias fallback |
| 5 | [PSWS account](#5-psws-account--ssh-key) | No | GRAPE Digital RF uploads to HamSCI | No data sharing |
| 6 | [GNSS receiver](#6-gnss-receiver-zed-f9p) | No | Local VTEC monitoring, carrier-phase TEC | No local TEC |

**No user action needed** for WAM-IPE (public S3 bucket), GIRO ionosonde
data (open HTTP API), or NOMADS (public NOAA server) — these are fetched
automatically with no credentials.

---

## 1. ka9q-radio (radiod)

| | |
|---|---|
| **Status** | **Required** |
| **What** | Software-defined radio server by Phil Karn, KA9Q |
| **Why** | Provides timestamped IQ samples via RTP multicast to all hf-timestd services |
| **Source** | <https://github.com/ka9q/ka9q-radio> |

### Install

Follow the ka9q-radio build instructions for your platform.  On Debian/Ubuntu:

```bash
sudo apt install build-essential libfftw3-dev libavahi-client-dev \
    libopus-dev libncurses-dev libasound2-dev
git clone https://github.com/ka9q/ka9q-radio.git
cd ka9q-radio
make
sudo make install
```

### Configure

Create a radio configuration file (e.g. `/etc/radio/rx888-hf.conf`) that
defines your SDR hardware and channel plan.  hf-timestd discovers channels
via the status multicast address configured in `[ka9q].status_address` in
`timestd-config.toml`.

### Verify

```bash
# Confirm radiod is running and advertising channels
avahi-browse -rt _ka9q-ctl._udp
```

---

## 2. Hardware

| | |
|---|---|
| **Status** | **Required** |

### SDR Receiver

An **RX888 MkII** (or compatible direct-sampling SDR supported by ka9q-radio).
The RX888 covers 0–64 MHz with 16-bit ADC at 64.8 or 129.6 Msps.

### GPSDO (GPS-Disciplined Oscillator)

A GPSDO provides the 10 MHz reference clock and PPS signal that ka9q-radio
uses for sample-accurate timestamping.  Without it, all metrology is invalid.

Common choices:
- **Leo Bodnar GPSDO** (mini, dual-output) — recommended for RX888
- **Jackson Labs Fury** — higher holdover stability
- Any unit providing 10 MHz + PPS locked to GPS

The GPSDO 10 MHz output connects to the RX888's external clock input.
The PPS output connects to the host's PPS input (if available) or is
used by chrony/gpsd for system time discipline.

### Antenna

An HF antenna covering 2–30 MHz.  A horizontal dipole or fan dipole at
≥10 m height is typical.  The antenna choice directly affects which
broadcast time stations (WWV, WWVH, CHU, BPM) are receivable.

---

## 3. PHaRLAP 4.7.4 (Numerical Raytracing)

| | |
|---|---|
| **Status** | Optional |
| **What** | Numerical HF ray-tracing engine from Defence Science and Technology Group (Australia) |
| **Why** | Enables physics-based ionospheric ray-tracing for multi-hop mode identification, MUF estimation, and propagation delay modeling |
| **Without it** | `RaytraceEngine` falls back to geometric great-circle model with parametric ionospheric delay |

### Obtain PHaRLAP

PHaRLAP is **free for non-commercial research use** but requires registration:

1. Visit <https://www.dst.defence.gov.au/opportunity/pharlap>
2. Fill out the request form (academic/amateur radio use qualifies)
3. You will receive a download link (typically within a few business days)
4. Download the archive (`pharlap_4.7.4.tar.gz` or similar)

### Install PHaRLAP

```bash
# Extract to /opt (recommended location)
sudo mkdir -p /opt/pharlap_4.7.4
sudo tar xzf pharlap_4.7.4.tar.gz -C /opt/pharlap_4.7.4 --strip-components=1

# Verify the expected directory structure
ls /opt/pharlap_4.7.4/lib/      # Should contain linux/, maca/, maci/
ls /opt/pharlap_4.7.4/dat/      # Should contain iri2020/, igrf2025.dat, etc.
ls /opt/pharlap_4.7.4/src/C/    # Should contain header files
```

### Build pyLAP (Python wrapper)

pyLAP is the Python interface to PHaRLAP.  We maintain a patched fork at
<https://github.com/mijahauan/PyLap> with cross-platform PHaRLAP 4.7.4
support (Linux x86_64, macOS arm64, macOS x86_64).

**Prerequisites:** `gfortran` must be installed.

```bash
# Linux
sudo apt install gfortran

# macOS
brew install gcc
```

**Build and install into the hf-timestd venv:**

```bash
git clone https://github.com/mijahauan/PyLap.git /opt/pylap
export PHARLAP_HOME=/opt/pharlap_4.7.4
/opt/hf-timestd/venv/bin/pip install /opt/pylap --no-build-isolation
```

The build auto-detects the platform and links against the correct PHaRLAP
static libraries.  Legacy IRI modules (iri2007, iri2012) are automatically
skipped when their `.a` files are absent from PHaRLAP 4.7.4.

### Runtime Environment

The following environment variables must be set for any process that uses
pyLAP (systemd services, interactive scripts, etc.):

```
PHARLAP_HOME=/opt/pharlap_4.7.4
DIR_MODELS_REF_DAT=/opt/pharlap_4.7.4/dat
```

For systemd services, add these to `/etc/hf-timestd/environment`:

```bash
echo 'PHARLAP_HOME=/opt/pharlap_4.7.4' | sudo tee -a /etc/hf-timestd/environment
echo 'DIR_MODELS_REF_DAT=/opt/pharlap_4.7.4/dat' | sudo tee -a /etc/hf-timestd/environment
```

### Verify

```bash
PHARLAP_HOME=/opt/pharlap_4.7.4 \
DIR_MODELS_REF_DAT=/opt/pharlap_4.7.4/dat \
/opt/hf-timestd/venv/bin/python -c "
import importlib, math
iri = importlib.import_module('pylap.iri2016')
_, oarr = iri.iri2016(40.0, -90.0, 100.0, [2026, 1, 15, 18, 0], 100.0, 10.0, 50, {})
foF2 = 8.98 * math.sqrt(max(oarr[0], 0)) / 1e6
print(f'IRI OK — foF2={foF2:.1f} MHz, hmF2={oarr[1]:.0f} km')
"
```

Expected output: `IRI OK — foF2=X.X MHz, hmF2=XXX km` (values depend on
solar conditions at the queried date/time).

---

## 4. NASA Earthdata Account

| | |
|---|---|
| **Status** | Optional |
| **What** | Free account for downloading IONEX and DCB files from NASA CDDIS |
| **Why** | Global ionosphere TEC maps (IONEX) improve ionospheric delay modeling; Differential Code Bias (DCB) corrections improve VTEC accuracy |
| **Without it** | Falls back to parametric IRI model; VTEC assumes zero DCB bias |

### Setup

See **[NASA_EARTHDATA_SETUP.md](NASA_EARTHDATA_SETUP.md)** for full
step-by-step instructions.

**Summary:**

1. Register at <https://urs.earthdata.nasa.gov/users/new> (free)
2. Authorize CDDIS data access in your Earthdata profile
3. Create `/etc/hf-timestd/earthdata-netrc`:

```
machine urs.earthdata.nasa.gov
login YOUR_USERNAME
password YOUR_PASSWORD
```

4. `sudo chmod 600 /etc/hf-timestd/earthdata-netrc`
5. `sudo chown timestd:timestd /etc/hf-timestd/earthdata-netrc`

---

## 5. PSWS Account + SSH Key

| | |
|---|---|
| **Status** | Optional |
| **What** | HamSCI Personal Space Weather Station network account for uploading GRAPE Digital RF data |
| **Why** | Contributes your station's data to the HamSCI research network |
| **Without it** | All data stays local; no uploads to HamSCI |

### Setup

See **[PSWS_SETUP_GUIDE.md](PSWS_SETUP_GUIDE.md)** for full step-by-step
instructions.

**Summary:**

1. Register at <https://pswsnetwork.caps.ua.edu/>
2. Create a site → receive **SITE_ID** (e.g. `S000171`) and **TOKEN**
3. Add an instrument → receive **INSTRUMENT_ID**
4. Generate SSH key: `ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa_psws -N ""`
5. Upload public key: `ssh-copy-id -i ~/.ssh/id_rsa_psws.pub SITE_ID@pswsnetwork.eng.ua.edu`
6. Configure `[uploader]` section in `timestd-config.toml`

### Verify

```bash
sudo -u timestd /opt/hf-timestd/venv/bin/hf-timestd grape test-upload
```

---

## 6. GNSS Receiver (ZED-F9P)

| | |
|---|---|
| **Status** | Optional |
| **What** | Dual-frequency GNSS receiver for local vertical TEC measurement |
| **Why** | Provides independent local ionospheric TEC data for carrier-phase calibration and ionospheric science |
| **Without it** | No local VTEC; system uses WAM-IPE/IONEX for ionospheric data |

### Setup

See **[ZED_F9P_TEC_CONFIGURATION.md](ZED_F9P_TEC_CONFIGURATION.md)** for
detailed receiver configuration, and **[GPS_TEC_OPTIONAL.md](GPS_TEC_OPTIONAL.md)**
for architecture details.

**Summary:**

1. Connect ZED-F9P via USB
2. Enable UBX protocol output (UBX-NAV-SAT messages)
3. Expose via TCP using `ser2net` (port 9000)
4. Configure `[gnss_vtec]` section in `timestd-config.toml`

---

## Automatic Data Sources (No Action Required)

These are fetched automatically by hf-timestd with no credentials or
registration:

| Source | Data | Endpoint | Used By |
|--------|------|----------|---------|
| **WAM-IPE** | Real-time NmF2, hmF2, TEC grids | `s3://noaa-nws-wam-ipe-pds` (public) | `iono_data_service.py` |
| **GIRO** | Real-time ionosonde measurements | `https://lgdc.uml.edu/common/DIDBFast498` | `iono_data_service.py` |
| **NOMADS** | WAM-IPE fallback | `https://nomads.ncep.noaa.gov/` | `iono_data_service.py` |

---

## Installation Order

For a new station, the recommended order is:

1. **Hardware** — Install RX888 + GPSDO + antenna
2. **ka9q-radio** — Build, configure, verify channels
3. **hf-timestd** — Run `deploy.sh` (core system, no optional externals)
4. **NASA Earthdata** — Register, configure credentials
5. **PSWS** — Register, generate SSH key, test upload
6. **PHaRLAP** — Request, download, build pyLAP
7. **GNSS receiver** — Connect, configure, enable VTEC

Steps 4–7 can be done in any order and at any time after initial deployment.
Each adds capability without disrupting running services.

---

## Related Documentation

- [STATION_SETUP_GUIDE.md](STATION_SETUP_GUIDE.md) — Site-specific config (`timestd-config.toml`)
- [NASA_EARTHDATA_SETUP.md](NASA_EARTHDATA_SETUP.md) — IONEX/DCB credential setup
- [PSWS_SETUP_GUIDE.md](PSWS_SETUP_GUIDE.md) — PSWS registration and SSH key setup
- [ZED_F9P_TEC_CONFIGURATION.md](ZED_F9P_TEC_CONFIGURATION.md) — GNSS receiver configuration
- [GPS_TEC_OPTIONAL.md](GPS_TEC_OPTIONAL.md) — VTEC architecture and optional capabilities
