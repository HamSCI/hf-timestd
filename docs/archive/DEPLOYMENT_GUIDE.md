# Signal Recorder - Deployment and Usage Guide

**Author:** Manus AI  
**Date:** October 24, 2025  
**Version:** 0.1.0

---

## Overview

Signal Recorder is a modular, production-ready system for automatically recording, processing, and uploading scientific signal data from ka9q-radio. This guide provides complete instructions for deploying and operating the system.

The system is specifically designed to work with **ka9q-radio** to record WWV/CHU time signal broadcasts for the **HamSCI GRAPE** (Great American Radio Propagation Experiment) project, but its modular architecture allows easy extension to other signal types such as CODAR ocean radar or HF ionospheric sounders.

---

## Architecture Overview

The system consists of five core modules that work together to provide a complete signal recording and upload pipeline:

### 1. Stream Discovery Module

The Stream Discovery Module automatically discovers available signal streams from ka9q-radio using **Avahi/mDNS service discovery** and **status metadata decoding**. This eliminates the need for manual configuration of multicast addresses, ports, and SSRCs.

**Key features:**
- Resolves mDNS service names (e.g., `wwv-iq.local`) to multicast addresses
- Listens to status metadata streams to discover active SSRCs
- Maps configured frequencies to discovered SSRCs
- Automatically adapts to radiod configuration changes

### 2. Stream Recorder Module

The Stream Recorder Module captures RTP multicast streams from ka9q-radio and writes time-synchronized audio/IQ data files. It wraps ka9q-radio's `pcmrecord` utility to provide a clean Python interface.

**Key features:**
- Records 1-minute WAV files aligned to UTC minute boundaries
- Supports wavpack compression to save disk space
- Manages multiple concurrent recorders for different frequencies
- Graceful start/stop with proper cleanup

### 3. Storage Manager Module

The Storage Manager Module organizes recorded files in a structured hierarchy and tracks processing state using JSON metadata files.

**Directory structure:**
```
{archive_dir}/
    {YYYYMMDD}/
        {CALLSIGN_GRID}/
            {RECEIVER@PSWS_ID}/
                {BAND}/
                    YYYYMMDDTHHMMSS.wav
                    processing_state.json
```

**Key features:**
- Hierarchical organization compatible with HamSCI PSWS requirements
- JSON-based state tracking for processing and upload status
- Automatic detection of missing files
- Retention policy management

### 4. Signal Processor Module

The Signal Processor Module provides a plugin-based architecture for signal-specific processing. The GRAPE processor is included for WWV/CHU data.

**GRAPE processing pipeline:**
1. Validate 1440 one-minute files (one per minute of the day)
2. Repair gaps by inserting silent files
3. Decompress wavpack files
4. Concatenate and resample to 10 Hz using sox
5. Convert to Digital RF format

**Key features:**
- Plugin architecture for easy extension to new signal types
- Automatic gap repair with silent file insertion
- Configurable sample rates and output formats
- Validation and completeness checking

### 5. Upload Manager Module

The Upload Manager Module handles reliable upload of processed datasets to remote repositories with retry logic and verification.

**Key features:**
- Queue-based upload with persistent state
- Exponential backoff retry logic
- Upload verification
- SSH/rsync protocol support (extensible to HTTP, S3, etc.)

---

## Installation

### Prerequisites

Before installing Signal Recorder, ensure you have the following:

**System Requirements:**
- Linux system (tested on Ubuntu 22.04)
- Python 3.10 or higher
- Running instance of ka9q-radio (radiod)

**System Dependencies:**
```bash
sudo apt-get update
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    sox \
    wavpack \
    avahi-daemon \
    avahi-utils \
    rsync \
    openssh-client
```

**ka9q-radio:**

Signal Recorder requires ka9q-radio to be installed and configured. The `pcmrecord` utility must be in your system PATH. See the [ka9q-radio documentation](https://github.com/ka9q/ka9q-radio) for installation instructions.

### Installing Signal Recorder

It is recommended to install Signal Recorder in a Python virtual environment to avoid dependency conflicts.

**Step 1: Create virtual environment**
```bash
# Create a directory for the installation
sudo mkdir -p /opt/signal-recorder
cd /opt/signal-recorder

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate
```

**Step 2: Install from source**
```bash
# Clone the repository (or extract the tarball)
git clone https://github.com/yourusername/signal-recorder.git
cd signal-recorder

# Install in editable mode
pip install -e .
```

**Step 3: Verify installation**
```bash
signal-recorder --help
```

You should see the help message with available commands.

---

## Configuration

### Step 1: Initialize Configuration

Create a configuration file using the `init` command:

```bash
signal-recorder init --config /etc/signal-recorder/config.toml
```

This creates `/etc/signal-recorder/config.toml` with example settings.

### Step 2: Edit Configuration

Open the configuration file in your preferred editor:

```bash
sudo nano /etc/signal-recorder/config.toml
```

**Station Information:**

Update the `[station]` section with your station details:

```toml
[station]
id = "PSWS001"              # Your PSWS station ID
instrument_id = "1"         # Instrument number (usually 1)
callsign = "AI6VN"          # Your callsign
grid_square = "CM87aa"      # Your 6-character grid square
latitude = 37.7749          # Station latitude
longitude = -122.4194       # Station longitude
```

**Stream Configuration:**

Update the `[[recorder.streams]]` section to match your radiod configuration:

```toml
[[recorder.streams]]
stream_name = "WWV-IQ"      # Must match radiod config
frequencies = [
    2500000,   # WWV 2.5 MHz
    5000000,   # WWV 5 MHz
    10000000,  # WWV 10 MHz
    15000000,  # WWV 15 MHz
    20000000,  # WWV 20 MHz
    25000000,  # WWV 25 MHz
    3330000,   # CHU 3.33 MHz
    7850000,   # CHU 7.85 MHz
    14670000   # CHU 14.67 MHz
]
processor = "grape"
```

**Upload Configuration:**

Configure the upload settings for HamSCI PSWS:

```toml
[upload]
protocol = "ssh_rsync"
host = "pswsnetwork.eng.ua.edu"
user = "grape"
base_path = "/data/uploads"

[upload.ssh]
key_file = "/home/user/.ssh/id_rsa_grape"
```

### Step 3: Set Up SSH Keys

For passwordless upload, generate an SSH key pair and add the public key to the remote server:

```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa_grape -N ""

# Copy public key to remote server
ssh-copy-id -i ~/.ssh/id_rsa_grape.pub grape@pswsnetwork.eng.ua.edu
```

Update the `key_file` path in your configuration to match the location of your private key.

### Step 4: Create Directories

Create the archive directory:

```bash
sudo mkdir -p /var/lib/signal-recorder/archive
sudo chown -R youruser:yourgroup /var/lib/signal-recorder
```

Replace `youruser` and `yourgroup` with the user that will run Signal Recorder.

---

## Testing

### Discover Streams

Before running the full system, test stream discovery to ensure Signal Recorder can communicate with radiod:

```bash
signal-recorder discover --radiod wwv-iq.local --verbose
```

**Expected output:**
```
Discovering streams from wwv-iq.local...

Resolved: wwv-iq.local → 239.1.2.3:5004

Found 9 streams:

  SSRC: 0x12345678
    Frequency: 2.500 MHz
    Sample Rate: 16000 Hz
    Channels: 2
    Encoding: F32LE

  SSRC: 0x12345679
    Frequency: 5.000 MHz
    Sample Rate: 16000 Hz
    Channels: 2
    Encoding: F32LE
  ...
```

If you see errors, check that:
- radiod is running
- The stream name matches your radiod configuration
- Avahi is running (`sudo systemctl status avahi-daemon`)

### Run in Foreground

Test recording by running the daemon in the foreground:

```bash
signal-recorder daemon --config /etc/signal-recorder/config.toml --verbose
```

**Expected output:**
```
[INFO] Initializing Signal Recorder application
[INFO] Discovering streams and starting recorders
[INFO] Resolved wwv-iq.local → 239.1.2.3:5004
[INFO] Discovered 9 active SSRCs
[INFO] Matched 2.5 MHz → SSRC 0x12345678
[INFO] Started recorder for WWV_2_5
[INFO] Matched 5.0 MHz → SSRC 0x12345679
[INFO] Started recorder for WWV_5
...
[INFO] Started 9 recorders
[INFO] Starting Signal Recorder daemon
```

Let it run for a few minutes, then check that files are being created:

```bash
ls -lh /var/lib/signal-recorder/archive/$(date +%Y%m%d)/
```

You should see directories for each band with `.wav` or `.wv` files.

Press `Ctrl+C` to stop the daemon.

---

## Running as a Service

For production deployment, run Signal Recorder as a systemd service so it starts automatically on boot and restarts on failure.

### Create systemd Service File

Create `/etc/systemd/system/signal-recorder.service`:

```ini
[Unit]
Description=Signal Recorder Daemon
After=network-online.target radiod.service
Wants=network-online.target

[Service]
Type=simple
User=youruser
Group=yourgroup
WorkingDirectory=/opt/signal-recorder
ExecStart=/opt/signal-recorder/venv/bin/signal-recorder daemon --config /etc/signal-recorder/config.toml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Important:** Replace `youruser` and `yourgroup` with the appropriate user.

### Enable and Start Service

```bash
# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable signal-recorder.service

# Start the service
sudo systemctl start signal-recorder.service

# Check status
sudo systemctl status signal-recorder.service
```

### View Logs

View real-time logs:
```bash
journalctl -u signal-recorder -f
```

View logs from the last hour:
```bash
journalctl -u signal-recorder --since "1 hour ago"
```

---

## Daily Operations

### Manual Processing

To manually process a specific date (e.g., if automatic processing failed):

```bash
signal-recorder process --date 20241022 --config /etc/signal-recorder/config.toml
```

This will:
1. Scan for recorded files for that date
2. Validate completeness
3. Repair gaps if needed
4. Process to Digital RF format
5. Enqueue for upload

### Check Status

To check the status of recorders and uploads:

```bash
signal-recorder status --config /etc/signal-recorder/config.toml
```

**Example output:**
```
=== Signal Recorder Status ===

Recorders:
  WWV_2_5: 2.500 MHz
    Running: True
    Output: /var/lib/signal-recorder/archive/20241024/.../WWV_2_5
  WWV_5: 5.000 MHz
    Running: True
    Output: /var/lib/signal-recorder/archive/20241024/.../WWV_5
  ...

Uploads:
  Total: 5
  Pending: 2
  Uploading: 1
  Completed: 2
  Failed: 0
```

### Monitor Disk Usage

Recorded data can consume significant disk space. Monitor usage regularly:

```bash
du -sh /var/lib/signal-recorder/archive/
```

After successful upload, old files are automatically cleaned up based on the retention policy (default: 30 days).

---

## Troubleshooting

### No Streams Discovered

**Symptom:** `signal-recorder discover` finds no streams.

**Possible causes:**
- radiod is not running
- Stream name doesn't match radiod configuration
- Avahi is not running
- Firewall blocking multicast traffic

**Solutions:**
```bash
# Check radiod status
sudo systemctl status radiod

# Check avahi status
sudo systemctl status avahi-daemon

# Check radiod configuration
cat /etc/radio/radiod@rx888.conf | grep -A 5 "WWV-IQ"

# Test multicast connectivity
avahi-browse -r _rtp._udp
```

### Recorders Not Starting

**Symptom:** Daemon starts but no files are created.

**Possible causes:**
- `pcmrecord` not in PATH
- Permission issues with archive directory
- Incorrect SSRC mapping

**Solutions:**
```bash
# Check if pcmrecord is available
which pcmrecord

# Check directory permissions
ls -ld /var/lib/signal-recorder/archive

# Check logs for errors
journalctl -u signal-recorder -n 100
```

### Upload Failures

**Symptom:** Files processed but not uploaded.

**Possible causes:**
- SSH key not configured
- Network connectivity issues
- Remote server permissions

**Solutions:**
```bash
# Test SSH connection
ssh -i ~/.ssh/id_rsa_grape grape@pswsnetwork.eng.ua.edu

# Test rsync manually
rsync -avz --progress -e "ssh -i ~/.ssh/id_rsa_grape" \
    /var/lib/signal-recorder/archive/20241024/ \
    grape@pswsnetwork.eng.ua.edu:/data/uploads/20241024/

# Check upload queue
cat /var/lib/signal-recorder/upload_queue.json
```

### Processing Failures

**Symptom:** Files recorded but processing fails.

**Possible causes:**
- Missing dependencies (sox, wavpack)
- Incomplete data (too many missing files)
- Disk space issues

**Solutions:**
```bash
# Check dependencies
which sox
which wavpack
which wvunpack

# Check disk space
df -h /var/lib/signal-recorder

# Check processing state
cat /var/lib/signal-recorder/archive/20241024/.../WWV_2_5/processing_state.json
```

---

## Extending the System

The modular architecture makes it easy to add support for new signal types.

### Adding a New Processor

To add a processor for a new signal type (e.g., CODAR):

**Step 1: Create processor class**

Create a new file `src/signal_recorder/codar_processor.py`:

```python
from .processor import SignalProcessor

class CODARProcessor(SignalProcessor):
    def validate_files(self, file_list):
        # Implement validation logic
        pass
    
    def repair_gaps(self, input_dir, missing_minutes):
        # Implement gap repair
        pass
    
    def process(self, input_dir, output_dir, config, metadata):
        # Implement CODAR-specific processing
        # e.g., decode radar chirps, extract ocean currents
        pass
    
    def get_upload_format(self):
        return "netcdf"
```

**Step 2: Register processor**

Add to `PROCESSOR_REGISTRY` in `processor.py`:

```python
from .codar_processor import CODARProcessor

PROCESSOR_REGISTRY = {
    "grape": GRAPEProcessor,
    "codar": CODARProcessor,
}
```

**Step 3: Configure stream**

Add to `config.toml`:

```toml
[[recorder.streams]]
stream_name = "CODAR-IQ"
frequencies = [25000000]
processor = "codar"
```

That's it! The system will automatically use your new processor for streams configured with `processor = "codar"`.

---

## References

1. ka9q-radio GitHub Repository: [https://github.com/ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio)
2. wsprdaemon GitHub Repository: [https://github.com/rrobinett/wsprdaemon](https://github.com/rrobinett/wsprdaemon)
3. HamSCI PSWS Network: [https://pswsnetwork.eng.ua.edu](https://pswsnetwork.eng.ua.edu)
4. Digital RF Format: [https://github.com/MITHaystack/digital_rf](https://github.com/MITHaystack/digital_rf)
5. Avahi Documentation: [https://www.avahi.org/](https://www.avahi.org/)

---

## Support

For issues, questions, or contributions, please visit the project repository or contact the maintainers.

