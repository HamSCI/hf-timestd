# Proposal: Modular Signal Recording and Upload System for ka9q-radio

**Author:** Manus AI  
**Date:** October 23, 2025  
**Purpose:** Design a simplified, modular application for recording WWV/CHU broadcasts from ka9q-radio and uploading to HamSCI, with extensibility for arbitrary signal types

---

## Executive Summary

This document proposes a **simplified, modular architecture** for extracting the GRAPE (Great American Radio Propagation Experiment) functionality from wsprdaemon into a standalone application. The design focuses on recording WWV and CHU time-signal broadcasts from ka9q-radio, processing them into Digital RF format, and uploading to the HamSCI PSWS (Personal Space Weather Station) network. The architecture is deliberately designed to be extensible to support future signal types such as CODAR, HF radar, and other scientific monitoring applications.

The proposed system eliminates the complexity of wsprdaemon's 18,000+ lines of shell scripting while preserving the essential functionality for scientific data collection. It adopts a **plugin-based architecture** that separates concerns and enables independent development of signal-specific processing modules.

---

## 1. Analysis of Current GRAPE Implementation in wsprdaemon

### 1.1 Overview of GRAPE in wsprdaemon

The GRAPE system in wsprdaemon is responsible for recording I/Q (In-phase/Quadrature) data from WWV and CHU time-signal broadcasts and uploading them to the HamSCI network for ionospheric research. The current implementation is tightly integrated into wsprdaemon's daemon-based architecture.

**Key components identified:**

| Component | File(s) | Function |
|:----------|:--------|:---------|
| **Recording** | `wd-record.c`, `recording.sh` | Captures 1-minute I/Q WAV files from ka9q-radio RTP streams |
| **Compression** | `grape-utils.sh` | Compresses WAV files using wavpack (.wv format) |
| **Archival** | `grape-utils.sh` | Stores 1440 one-minute files per day in organized directory tree |
| **Processing** | `grape-utils.sh`, `wav2grape.py` | Concatenates 1440 files into 24-hour WAV, downsamples to 10 Hz |
| **Conversion** | `wav2grape.py` | Converts 24-hour WAV to Digital RF format |
| **Upload** | `grape-utils.sh` | Uploads Digital RF datasets to HamSCI PSWS server via SSH/rsync |
| **Daemon** | `grape-utils.sh`, `watchdog.sh` | Runs as background service, checks daily at 00:05 UTC |

### 1.2 Data Flow in Current Implementation

The current GRAPE workflow follows this sequence:

1. **ka9q-radio** (`radiod`) receives signals from RX888 SDR
2. **radiod** multicasts RTP streams for WWV/CHU frequencies (configured in `radiod@rx888-wsprdaemon.conf`)
3. **wd-record** captures RTP streams, writes 1-minute WAV files synchronized to UTC
4. **Recording daemon** compresses each WAV file to wavpack (.wv) format
5. **Archive daemon** organizes files in directory structure: `wav-archive/YYYYMMDD/REPORTER_GRID/RECEIVER@PSWS_ID/BAND/`
6. **Daily at 00:05 UTC**, the GRAPE upload daemon:
   - Repairs missing files by inserting silence
   - Decompresses all 1440 .wv files for each band
   - Concatenates and downsamples to 10 Hz using `sox`
   - Converts to Digital RF format using `wav2grape.py`
   - Uploads to `pswsnetwork.eng.ua.edu` via SSH
   - Marks upload complete to avoid re-processing

### 1.3 Directory Structure

```
wav-archive/
└── 20241022/                           # Date
    └── AI6VN_CM87/                     # Reporter_Grid
        └── KA9Q_0@PSWS123_1/           # Receiver@StationID_InstrumentID
            ├── WWV_2_5/                # Band (2.5 MHz WWV)
            │   ├── 20241022T000000.wv
            │   ├── 20241022T000100.wv
            │   ├── ...
            │   ├── 20241022T235900.wv
            │   └── 24_hour_10sps_iq.wav
            ├── WWV_5/
            ├── WWV_10/
            ├── WWV_15/
            ├── WWV_20/
            ├── WWV_25/
            ├── CHU_3/
            ├── CHU_7/
            └── CHU_14/
```

### 1.4 Key Dependencies

**From wsprdaemon analysis:**

- **ka9q-radio**: Provides RTP multicast streams
- **wd-record** (C program): Captures RTP to WAV files
- **sox**: Audio processing (concatenation, resampling)
- **wavpack**: Compression/decompression
- **Python packages**: `digital_rf`, `soundfile`, `numpy`
- **System tools**: `rsync`, `ssh`, `find`, `awk`

### 1.5 Challenges with Current Implementation

The current GRAPE implementation has several limitations that motivate a redesign:

**Tight Coupling:** GRAPE functionality is deeply embedded in wsprdaemon's shell script architecture, making it difficult to use independently or extend to other signal types.

**Complexity:** The system requires understanding wsprdaemon's daemon management, watchdog processes, and configuration system even for users only interested in GRAPE.

**Limited Extensibility:** Adding support for new signal types (CODAR, HF radar) would require modifying multiple interconnected shell scripts.

**Maintenance Burden:** Shell script logic for file management, error handling, and scheduling is spread across multiple files totaling thousands of lines.

**Configuration Complexity:** Users must configure the entire wsprdaemon system even if they only want GRAPE functionality.

---

## 2. Proposed Modular Architecture

### 2.1 Design Principles

The proposed system is built on these core principles:

**Modularity:** Each major function (recording, processing, uploading) is an independent, replaceable module.

**Extensibility:** New signal types can be added by implementing a signal-specific processor plugin without modifying core code.

**Simplicity:** Python-based implementation with clear separation of concerns, avoiding complex shell script orchestration.

**Configuration:** Simple YAML or TOML configuration files, not embedded in shell scripts.

**Reliability:** Built-in error handling, retry logic, and state management.

**Observability:** Structured logging and optional metrics export.

### 2.2 System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    ka9q-radio (radiod)                      │
│              RX888 SDR → RTP Multicast Streams              │
└────────────────────────┬────────────────────────────────────┘
                         │ RTP/UDP Multicast
                         │ (239.x.x.x:port)
                         ↓
┌─────────────────────────────────────────────────────────────┐
│              Stream Recorder Module (Python)                │
│  - Listens to configured RTP multicast addresses           │
│  - Captures time-synchronized audio/IQ data                 │
│  - Writes raw WAV files to staging directory                │
│  - Compresses to wavpack (.wv) for archival                 │
└────────────────────────┬────────────────────────────────────┘
                         │ File I/O
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                  Storage Manager Module                     │
│  - Organizes files in date/station/band hierarchy          │
│  - Manages retention policies                               │
│  - Tracks processing state (JSON metadata)                  │
└────────────────────────┬────────────────────────────────────┘
                         │ File paths
                         ↓
┌─────────────────────────────────────────────────────────────┐
│              Signal Processor (Plugin System)               │
│                                                             │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  │
│  │ GRAPE Plugin  │  │ CODAR Plugin  │  │ Future Plugin │  │
│  │               │  │               │  │               │  │
│  │ - Validate    │  │ - Validate    │  │ - Validate    │  │
│  │ - Repair gaps │  │ - Decode      │  │ - Process     │  │
│  │ - Concatenate │  │ - Extract     │  │ - Transform   │  │
│  │ - Resample    │  │ - Format      │  │ - Package     │  │
│  │ - Convert DRF │  │ - Package     │  │               │  │
│  └───────────────┘  └───────────────┘  └───────────────┘  │
└────────────────────────┬────────────────────────────────────┘
                         │ Processed data
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                  Upload Manager Module                      │
│  - Queues datasets for upload                               │
│  - Implements retry logic with exponential backoff          │
│  - Supports multiple protocols (SSH/rsync, HTTP, S3)        │
│  - Tracks upload state and confirmations                    │
└─────────────────────────────────────────────────────────────┘
                         │ Network I/O
                         ↓
┌─────────────────────────────────────────────────────────────┐
│              HamSCI PSWS Server / Data Repository           │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Core Modules

#### 2.3.1 Stream Recorder Module

**Purpose:** Capture RTP multicast streams from ka9q-radio and write time-synchronized files.

**Implementation approach:**

- Python wrapper around `wd-record` or `pcmrecord` from ka9q-radio
- Alternative: Pure Python RTP receiver using `socket` and `struct` modules
- Manages multiple concurrent recording processes (one per frequency/band)
- Ensures UTC time synchronization for file boundaries
- Handles compression to wavpack format

**Key features:**

- Configurable multicast addresses and ports
- Automatic recovery from stream interruptions
- File rotation at minute boundaries
- Optional real-time compression

**Configuration example (YAML):**

```yaml
recorder:
  streams:
    - name: "WWV_2_5"
      multicast: "239.1.2.3:5004"
      ssrc: 0x12345678
      frequency_hz: 2500000
      sample_rate: 16000
      channels: 2  # IQ
      format: "float32"
      
    - name: "WWV_5"
      multicast: "239.1.2.3:5004"
      ssrc: 0x12345679
      frequency_hz: 5000000
      sample_rate: 16000
      channels: 2
      format: "float32"
      
  output:
    staging_dir: "/var/lib/signal-recorder/staging"
    compress: true
    compression_format: "wavpack"
```

#### 2.3.2 Storage Manager Module

**Purpose:** Organize recorded files in a structured hierarchy and manage retention.

**Responsibilities:**

- Create directory structure: `{base_path}/{date}/{station}/{band}/`
- Move files from staging to archive
- Track processing state using JSON metadata files
- Implement retention policies (e.g., delete after successful upload + 30 days)
- Handle disk space monitoring and cleanup

**Metadata tracking:**

Each band directory contains a `processing_state.json` file:

```json
{
  "date": "2024-10-22",
  "station_id": "PSWS123",
  "instrument_id": "1",
  "band": "WWV_5",
  "files_expected": 1440,
  "files_received": 1440,
  "files_missing": [],
  "24h_wav_created": true,
  "24h_wav_path": "/archive/20241022/AI6VN_CM87/KA9Q_0@PSWS123_1/WWV_5/24_hour_10sps_iq.wav",
  "drf_created": true,
  "drf_path": "/archive/20241022/AI6VN_CM87/KA9Q_0@PSWS123_1/OBS2024-10-22T00-00/",
  "upload_complete": false,
  "upload_attempts": 0,
  "last_upload_attempt": null
}
```

#### 2.3.3 Signal Processor (Plugin System)

**Purpose:** Process recorded files according to signal-specific requirements.

**Plugin interface (abstract base class):**

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List

class SignalProcessor(ABC):
    """Base class for signal-specific processors"""
    
    @abstractmethod
    def validate_files(self, file_list: List[Path]) -> Dict[str, Any]:
        """Check if all required files are present and valid"""
        pass
    
    @abstractmethod
    def repair_gaps(self, file_list: List[Path], metadata: Dict) -> bool:
        """Fill missing data with silence or interpolation"""
        pass
    
    @abstractmethod
    def process(self, input_dir: Path, output_dir: Path, config: Dict) -> Path:
        """Main processing logic, returns path to output dataset"""
        pass
    
    @abstractmethod
    def get_upload_format(self) -> str:
        """Return expected output format (e.g., 'digital_rf', 'netcdf', 'hdf5')"""
        pass
```

**GRAPE Plugin Implementation:**

```python
class GRAPEProcessor(SignalProcessor):
    """Processor for WWV/CHU data for HamSCI GRAPE"""
    
    def validate_files(self, file_list: List[Path]) -> Dict[str, Any]:
        # Check for 1440 files, validate timestamps
        expected = 1440
        actual = len(file_list)
        missing_minutes = self._find_missing_minutes(file_list)
        
        return {
            "valid": actual >= expected * 0.95,  # Allow 5% missing
            "files_found": actual,
            "files_expected": expected,
            "missing_minutes": missing_minutes
        }
    
    def repair_gaps(self, file_list: List[Path], metadata: Dict) -> bool:
        # Insert silent .wv files for missing minutes
        silent_file = Path("/usr/share/signal-recorder/one-minute-silent-float.wv")
        for minute in metadata["missing_minutes"]:
            target = self._get_file_path_for_minute(minute)
            target.symlink_to(silent_file)
        return True
    
    def process(self, input_dir: Path, output_dir: Path, config: Dict) -> Path:
        # 1. Decompress all .wv files
        wav_files = self._decompress_wavpack_files(input_dir)
        
        # 2. Concatenate and resample to 10 Hz using sox
        wav_24h = self._create_24h_wav(wav_files, output_dir)
        
        # 3. Convert to Digital RF format
        drf_dataset = self._convert_to_digital_rf(
            wav_24h, 
            output_dir,
            config["station_id"],
            config["instrument_id"],
            config["frequency_hz"]
        )
        
        return drf_dataset
    
    def get_upload_format(self) -> str:
        return "digital_rf"
    
    def _convert_to_digital_rf(self, wav_file, output_dir, station_id, instrument_id, freq):
        # Use wav2grape.py logic or call it as subprocess
        # Returns path to Digital RF dataset directory
        pass
```

**Future CODAR Plugin (example):**

```python
class CODARProcessor(SignalProcessor):
    """Processor for CODAR ocean radar signals"""
    
    def validate_files(self, file_list: List[Path]) -> Dict[str, Any]:
        # CODAR-specific validation
        # Check for continuous coverage, signal quality metrics
        pass
    
    def process(self, input_dir: Path, output_dir: Path, config: Dict) -> Path:
        # CODAR-specific processing:
        # - Decode radar chirps
        # - Extract Doppler spectra
        # - Generate radial current maps
        # - Package in NetCDF format
        pass
    
    def get_upload_format(self) -> str:
        return "netcdf"
```

#### 2.3.4 Upload Manager Module

**Purpose:** Reliably upload processed datasets to remote repositories.

**Features:**

- Queue-based upload system
- Protocol abstraction (SSH/rsync, HTTP POST, S3, etc.)
- Retry logic with exponential backoff
- Bandwidth throttling
- Upload verification and confirmation

**Implementation:**

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional
import time

class UploadProtocol(ABC):
    """Base class for upload protocols"""
    
    @abstractmethod
    def upload(self, local_path: Path, remote_path: str, metadata: Dict) -> bool:
        """Upload dataset, return True on success"""
        pass
    
    @abstractmethod
    def verify(self, remote_path: str) -> bool:
        """Verify upload completed successfully"""
        pass

class SSHRsyncUpload(UploadProtocol):
    """Upload via SSH/rsync (current GRAPE method)"""
    
    def __init__(self, host: str, user: str, base_path: str):
        self.host = host
        self.user = user
        self.base_path = base_path
    
    def upload(self, local_path: Path, remote_path: str, metadata: Dict) -> bool:
        # Construct rsync command
        cmd = [
            "rsync", "-avz", "--progress",
            str(local_path),
            f"{self.user}@{self.host}:{self.base_path}/{remote_path}"
        ]
        # Execute and handle errors
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0
    
    def verify(self, remote_path: str) -> bool:
        # SSH to server and check file exists
        cmd = ["ssh", f"{self.user}@{self.host}", "test", "-d", f"{self.base_path}/{remote_path}"]
        result = subprocess.run(cmd)
        return result.returncode == 0

class UploadManager:
    """Manages upload queue and retry logic"""
    
    def __init__(self, protocol: UploadProtocol, config: Dict):
        self.protocol = protocol
        self.config = config
        self.queue = []  # Could be persistent queue (SQLite, Redis)
    
    def enqueue(self, dataset_path: Path, metadata: Dict):
        """Add dataset to upload queue"""
        self.queue.append({
            "path": dataset_path,
            "metadata": metadata,
            "attempts": 0,
            "last_attempt": None,
            "status": "pending"
        })
    
    def process_queue(self):
        """Process upload queue with retry logic"""
        for item in self.queue:
            if item["status"] == "completed":
                continue
            
            # Exponential backoff
            if item["last_attempt"]:
                wait_time = 2 ** item["attempts"] * 60  # minutes
                if time.time() - item["last_attempt"] < wait_time:
                    continue
            
            # Attempt upload
            success = self.protocol.upload(
                item["path"],
                self._construct_remote_path(item["metadata"]),
                item["metadata"]
            )
            
            item["attempts"] += 1
            item["last_attempt"] = time.time()
            
            if success and self.protocol.verify(self._construct_remote_path(item["metadata"])):
                item["status"] = "completed"
                self._mark_upload_complete(item["path"])
            elif item["attempts"] >= self.config["max_retries"]:
                item["status"] = "failed"
                self._log_failure(item)
```

### 2.4 Main Application Controller

**Purpose:** Orchestrate all modules and provide CLI/daemon interface.

**Structure:**

```python
class SignalRecorderApp:
    """Main application controller"""
    
    def __init__(self, config_path: Path):
        self.config = self._load_config(config_path)
        self.recorder = StreamRecorder(self.config["recorder"])
        self.storage = StorageManager(self.config["storage"])
        self.processors = self._load_processors(self.config["processors"])
        self.uploader = UploadManager(
            self._create_upload_protocol(self.config["upload"]),
            self.config["upload"]
        )
    
    def run_daemon(self):
        """Run as background daemon"""
        # Start recorder threads
        self.recorder.start()
        
        # Main loop
        while True:
            # Every hour: check for complete days to process
            self._process_complete_days()
            
            # Every 10 minutes: process upload queue
            self.uploader.process_queue()
            
            # Sleep
            time.sleep(600)
    
    def run_once(self, date: str):
        """Process a specific date (for manual/cron execution)"""
        self._process_date(date)
    
    def _process_date(self, date: str):
        """Process all bands for a given date"""
        for band_config in self.config["bands"]:
            processor = self.processors[band_config["processor_type"]]
            
            # Get files for this band/date
            band_dir = self.storage.get_band_dir(date, band_config["name"])
            files = list(band_dir.glob("*.wv"))
            
            # Validate
            validation = processor.validate_files(files)
            if not validation["valid"]:
                processor.repair_gaps(files, validation)
            
            # Process
            output = processor.process(
                band_dir,
                self.storage.get_output_dir(date, band_config["name"]),
                band_config
            )
            
            # Enqueue for upload
            self.uploader.enqueue(output, {
                "date": date,
                "band": band_config["name"],
                "station_id": self.config["station"]["id"],
                "instrument_id": self.config["station"]["instrument_id"]
            })
```

---

## 3. Configuration System

### 3.1 Configuration File Format

Use TOML for human-readable, hierarchical configuration:

```toml
[station]
id = "PSWS123"
instrument_id = "1"
callsign = "AI6VN"
grid_square = "CM87"
latitude = 37.7749
longitude = -122.4194

[recorder]
staging_dir = "/var/lib/signal-recorder/staging"
archive_dir = "/var/lib/signal-recorder/archive"
compress = true
compression_format = "wavpack"

[[recorder.streams]]
name = "WWV_2_5"
multicast = "239.1.2.3:5004"
ssrc = "0x12345678"
frequency_hz = 2500000
sample_rate = 16000
channels = 2
format = "float32"
processor = "grape"

[[recorder.streams]]
name = "WWV_5"
multicast = "239.1.2.3:5004"
ssrc = "0x12345679"
frequency_hz = 5000000
sample_rate = 16000
channels = 2
format = "float32"
processor = "grape"

[[recorder.streams]]
name = "CODAR_25MHz"
multicast = "239.1.2.4:5005"
ssrc = "0x12345680"
frequency_hz = 25000000
sample_rate = 48000
channels = 2
format = "int16"
processor = "codar"

[processors.grape]
enabled = true
target_sample_rate = 10  # Hz
output_format = "digital_rf"
subdir_cadence_secs = 3600
file_cadence_millisecs = 1000
compression_level = 1

[processors.codar]
enabled = false
# CODAR-specific settings

[upload]
protocol = "ssh_rsync"
host = "pswsnetwork.eng.ua.edu"
user = "grape"
base_path = "/data/uploads"
max_retries = 5
retry_backoff_base = 2  # exponential backoff: 2^n minutes

[upload.ssh]
key_file = "/home/user/.ssh/id_rsa"
known_hosts_file = "/home/user/.ssh/known_hosts"

[storage]
retention_days = 30  # Keep files for 30 days after successful upload
cleanup_interval_hours = 24

[logging]
level = "INFO"
file = "/var/log/signal-recorder/app.log"
max_bytes = 10485760  # 10 MB
backup_count = 5
```

---

## 4. Implementation Plan

### 4.1 Phase 1: Core Infrastructure (Weeks 1-2)

**Deliverables:**

- Project structure and Python package setup
- Configuration system (TOML parsing)
- Logging infrastructure
- Storage Manager module
- Basic CLI interface

**Technologies:**

- Python 3.10+
- `toml` or `tomli` for configuration
- `pathlib` for file operations
- `logging` with structured output

### 4.2 Phase 2: Stream Recorder (Weeks 3-4)

**Deliverables:**

- Stream Recorder module
- Integration with `wd-record` or `pcmrecord`
- Wavpack compression
- Unit tests for file handling

**Approach:**

- Initially wrap existing `wd-record` binary
- Future: Pure Python RTP receiver for better control

### 4.3 Phase 3: GRAPE Processor Plugin (Weeks 5-6)

**Deliverables:**

- Plugin system base classes
- GRAPE processor implementation
- Integration with `wav2grape.py` logic
- Digital RF conversion
- End-to-end test with sample data

**Key tasks:**

- Port `wav2grape.py` into plugin or call as library
- Implement gap repair with silence insertion
- Sox integration for concatenation and resampling

### 4.4 Phase 4: Upload Manager (Week 7)

**Deliverables:**

- Upload Manager module
- SSH/rsync protocol implementation
- Queue persistence (SQLite)
- Retry logic with exponential backoff
- Upload verification

### 4.5 Phase 5: Integration and Testing (Week 8)

**Deliverables:**

- Main application controller
- Daemon mode (systemd service)
- Integration tests
- Documentation
- Example configurations

### 4.6 Phase 6: Future Extensions (Post-MVP)

**Possible additions:**

- Web dashboard for monitoring
- Metrics export (Prometheus)
- Additional upload protocols (S3, HTTP)
- CODAR processor plugin
- Real-time signal quality monitoring

---

## 5. Advantages Over Current Implementation

### 5.1 Comparison Table

| Aspect | wsprdaemon GRAPE | Proposed System |
|:-------|:-----------------|:----------------|
| **Lines of Code** | ~2,000 (shell) + 300 (Python) | ~1,500 (Python) estimated |
| **Language** | Bash + Python + C | Python + C (reuse wd-record) |
| **Configuration** | Embedded in shell scripts | YAML/TOML files |
| **Extensibility** | Requires modifying shell scripts | Plugin system, no core changes |
| **Dependencies** | Full wsprdaemon stack | Minimal: ka9q-radio, Python packages |
| **Standalone** | No | Yes |
| **Testing** | Difficult (shell scripts) | Unit and integration tests |
| **Error Handling** | Scattered across scripts | Centralized, structured |
| **Logging** | Multiple log files | Structured logging |
| **State Management** | File-based flags | JSON metadata + optional database |
| **Deployment** | Part of wsprdaemon | Independent package |

### 5.2 Key Benefits

**Simplicity:** Users only need to configure and run one application, not an entire WSPR decoding system.

**Maintainability:** Python codebase is easier to understand, test, and modify than thousands of lines of shell scripts.

**Extensibility:** Adding support for CODAR, HF radar, or other signals requires implementing a single plugin class, not modifying interconnected scripts.

**Reliability:** Structured error handling, retry logic, and state tracking improve robustness.

**Observability:** Structured logging and optional metrics make it easier to monitor and debug.

**Portability:** Pure Python (except for C recording binary) is more portable than complex shell scripts.

---

## 6. Migration Path from wsprdaemon

For users currently running wsprdaemon who want to migrate to the standalone system:

### 6.1 Compatibility Considerations

**Directory Structure:** The proposed system can maintain the same directory structure as wsprdaemon's GRAPE implementation for backward compatibility.

**File Formats:** Uses the same wavpack compression and Digital RF output formats.

**Upload Protocol:** Maintains SSH/rsync upload to HamSCI PSWS server.

### 6.2 Migration Steps

1. **Install standalone system** alongside wsprdaemon
2. **Configure** using existing wsprdaemon settings (station ID, grid, etc.)
3. **Test** in parallel mode (both systems running, compare outputs)
4. **Disable** GRAPE in wsprdaemon configuration
5. **Enable** standalone system in production
6. **Remove** wsprdaemon if no longer needed for WSPR

### 6.3 Coexistence

The standalone system can coexist with wsprdaemon:

- wsprdaemon continues to handle WSPR decoding and reporting
- Standalone system handles only GRAPE and other scientific data collection
- Both systems can share the same ka9q-radio instance (different RTP streams)

---

## 7. Example Usage

### 7.1 Installation

```bash
# Install from PyPI (future)
pip install signal-recorder

# Or from source
git clone https://github.com/your-org/signal-recorder.git
cd signal-recorder
pip install -e .
```

### 7.2 Configuration

```bash
# Generate default configuration
signal-recorder init --config /etc/signal-recorder/config.toml

# Edit configuration
vim /etc/signal-recorder/config.toml
```

### 7.3 Running

```bash
# Run as daemon (foreground for testing)
signal-recorder daemon --config /etc/signal-recorder/config.toml

# Process a specific date manually
signal-recorder process --date 2024-10-22 --config /etc/signal-recorder/config.toml

# Check status
signal-recorder status --config /etc/signal-recorder/config.toml

# List pending uploads
signal-recorder uploads --status pending
```

### 7.4 Systemd Service

```ini
[Unit]
Description=Signal Recorder for ka9q-radio
After=network.target radiod@rx888.service

[Service]
Type=simple
User=signal-recorder
ExecStart=/usr/local/bin/signal-recorder daemon --config /etc/signal-recorder/config.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## 8. Technical Specifications

### 8.1 System Requirements

**Hardware:**

- CPU: 2+ cores (for concurrent recording and processing)
- RAM: 4 GB minimum, 8 GB recommended
- Disk: 100 GB+ for archival (depends on retention policy)
- Network: Multicast-capable network interface

**Software:**

- Linux (Ubuntu 22.04+, Debian 12+, or similar)
- Python 3.10+
- ka9q-radio installed and configured
- sox, wavpack, rsync

### 8.2 Performance Characteristics

**Recording:**

- Handles 10+ concurrent streams (limited by CPU and disk I/O)
- Minimal latency (< 1 second from RTP reception to file write)

**Processing:**

- 24-hour WAV creation: ~5-10 minutes per band (depends on CPU)
- Digital RF conversion: ~2-5 minutes per band

**Upload:**

- Bandwidth-limited (configurable throttling)
- Typical dataset size: 50-200 MB per band per day

### 8.3 Security Considerations

**SSH Key Management:**

- Requires SSH key pair for upload authentication
- Keys should be stored securely with appropriate permissions (600)
- Consider using `ssh-agent` for key management

**Network Security:**

- RTP multicast streams are unencrypted (local network only)
- Upload traffic is encrypted via SSH

**File Permissions:**

- Application should run as dedicated user (not root)
- Archive directory should have restricted permissions

---

## 9. Future Enhancements

### 9.1 Web Dashboard

A web-based monitoring interface could provide:

- Real-time recording status
- Upload queue visualization
- Historical statistics (files recorded, uploaded, failed)
- Configuration management
- Log viewing

**Technology stack:** Flask/FastAPI + Vue.js/React

### 9.2 Metrics and Alerting

Integration with monitoring systems:

- Prometheus metrics export
- Grafana dashboards
- Alert on upload failures, disk space, missing data

### 9.3 Additional Processors

**CODAR Ocean Radar:**

- Decode radar chirps
- Extract ocean current data
- Generate radial velocity maps
- Output in NetCDF format

**HF Radar/Ranging:**

- Decode timing signals
- Extract propagation delay measurements
- Generate ionospheric maps

**Ionosonde Data:**

- Process ionogram sweeps
- Extract critical frequencies
- Generate ionospheric profiles

### 9.4 Cloud Storage Integration

Support for cloud upload destinations:

- AWS S3
- Google Cloud Storage
- Azure Blob Storage
- MinIO (self-hosted S3-compatible)

---

## 10. Conclusion

This proposal outlines a **modular, extensible architecture** for recording and uploading scientific signal data from ka9q-radio. By extracting and simplifying the GRAPE functionality from wsprdaemon, we create a system that is:

- **Easier to deploy** for users only interested in scientific data collection
- **Easier to maintain** with a clean Python codebase
- **Easier to extend** with a plugin-based architecture for new signal types

The proposed system preserves the essential functionality of wsprdaemon's GRAPE implementation while eliminating unnecessary complexity. The plugin architecture provides a clear path for supporting future signal types such as CODAR, HF radar, and other scientific monitoring applications.

**Next steps:**

1. Review and refine this proposal with stakeholders
2. Create detailed technical specifications for each module
3. Set up development environment and project structure
4. Begin Phase 1 implementation (core infrastructure)
5. Iterate based on testing and feedback

---

## References

1. wsprdaemon GitHub Repository: [https://github.com/rrobinett/wsprdaemon](https://github.com/rrobinett/wsprdaemon)
2. ka9q-radio GitHub Repository: [https://github.com/ka9q/ka9q-radio](https://github.com/ka9q/ka9q-radio)
3. HamSCI PSWS Network: [https://pswsnetwork.eng.ua.edu](https://pswsnetwork.eng.ua.edu)
4. Digital RF Format: [https://github.com/MITHaystack/digital_rf](https://github.com/MITHaystack/digital_rf)
5. GRAPE Project: [https://hamsci.org/grape](https://hamsci.org/grape)

