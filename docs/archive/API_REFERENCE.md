# GRAPE Signal Recorder - Unified API Reference

**Status:** CANONICAL - Single source of truth for ALL function signatures  
**Last Updated:** 2025-11-20  
**Purpose:** Complete API for all GRAPE modules - USE THIS before writing or calling any function

---

## Table of Contents

1. [Path Management API](#path-management-api)  
2. [Tone Detection API](#tone-detection-api)
3. [WWV/WWVH Discrimination API](#wwvwwvh-discrimination-api)
4. [CSV Writers API](#csv-writers-api)
5. [Data Models](#data-models)
6. [Directory Structure Reference](#directory-structure-reference)

---

## Path Management API

**Module:** `src/signal_recorder/paths.py`  
**CRITICAL:** All code MUST use this API for file paths - NO direct construction

### Class: `GRAPEPaths`

Central path manager for all GRAPE data structures.

#### Initialization

```python
from signal_recorder.paths import GRAPEPaths, load_paths_from_config

# From config (recommended - respects test/production mode)
paths = load_paths_from_config('/path/to/grape-config.toml')

# Direct initialization
paths = GRAPEPaths('/tmp/grape-test')
```

#### Archive Methods

```python
def get_archive_dir(self, channel_name: str) -> Path
```
Returns: `{data_root}/archives/{CHANNEL}/`

```python
def get_archive_path(self, channel_name: str, timestamp: float, freq_hz: int) -> Path
```
Returns: `{archive_dir}/{YYYYMMDDTHHMMSSZ}_{FREQ}_iq.npz`

#### Analytics Methods - Directories

```python
def get_analytics_dir(self, channel_name: str) -> Path
```
Returns: `{data_root}/analytics/{CHANNEL}/`

```python
def get_decimated_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/decimated/`

```python
def get_digital_rf_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/digital_rf/`

```python
def get_discrimination_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/discrimination/`

```python
def get_tone_detections_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/tone_detections/`

```python
def get_tick_windows_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/tick_windows/`

```python
def get_station_id_440hz_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/station_id_440hz/`

```python
def get_bcd_discrimination_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/bcd_discrimination/`

```python
def get_quality_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/quality/`

```python
def get_analytics_logs_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/logs/`

```python
def get_analytics_status_dir(self, channel_name: str) -> Path
```
Returns: `{analytics}/{CHANNEL}/status/`

#### Analytics Methods - Files

```python
def get_discrimination_csv(self, channel_name: str, date: str) -> Path
```
Returns: `{discrimination_dir}/{CHANNEL}_discrimination_{YYYYMMDD}.csv`

```python
def get_quality_csv(self, channel_name: str, date: str) -> Path
```
Returns: `{quality_dir}/{CHANNEL}_quality_{YYYYMMDD}.csv`

#### Helper Functions

```python
def channel_name_to_dir(channel_name: str) -> str
```
Converts "WWV 5 MHz" → "WWV_5_MHz"

```python
def channel_name_to_key(channel_name: str) -> str
```
Converts "WWV 5 MHz" → "wwv5"

```python
def channel_dir_to_name(dir_name: str) -> str
```
Converts "WWV_5_MHz" → "WWV 5 MHz"

---

## Tone Detection API

**Module:** `src/signal_recorder/tone_detector.py`

### Class: `MultiStationToneDetector`

Detects WWV (1000 Hz), WWVH (1200 Hz), and CHU (1000 Hz) timing tones.

#### Constructor

```python
MultiStationToneDetector(
    channel_name: str,
    sample_rate: int = 3000
)
```

**Parameters:**
- `channel_name` (str): "WWV 5 MHz", "CHU 7.85 MHz", etc.
- `sample_rate` (int): Processing sample rate (default: 3000 Hz)

**Channel Behavior:**
- WWV 2.5, 5, 10, 15 MHz: Detects WWV (1000 Hz) + WWVH (1200 Hz)
- WWV 20, 25 MHz: Detects WWV only
- CHU: Detects CHU only (1000 Hz)

#### Methods

##### `process_samples()` ⭐ PRIMARY METHOD

```python
def process_samples(
    self,
    timestamp: float,
    samples: np.ndarray,
    rtp_timestamp: int
) -> List[ToneDetectionResult]
```

**Parameters:**
- `timestamp` (float): UTC timestamp at minute boundary
- `samples` (np.ndarray): Complex IQ samples at 3000 Hz (MUST pre-resample!)
- `rtp_timestamp` (int): RTP timestamp for time_snap

**Returns:**
- `List[ToneDetectionResult]`: Detected tones (empty if none)

**CRITICAL:** Input must be at 3000 Hz. Resample from 16 kHz:

```python
from scipy import signal as scipy_signal

resampled = scipy_signal.resample_poly(iq_16khz, up=3, down=16, axis=0)
detections = detector.process_samples(timestamp, resampled, rtp_ts)
```

---

## WWV/WWVH Discrimination API

**Module:** `src/signal_recorder/wwvh_discrimination.py`

Provides 5 independent analysis methods for WWV/WWVH discrimination.

### Class: `WWVHDiscriminator`

#### Constructor

```python
WWVHDiscriminator(channel_name: str)
```

**Parameters:**
- `channel_name` (str): "WWV 5 MHz", etc.

### Method 1: Timing Tones (1000/1200 Hz)

```python
def detect_timing_tones(
    self,
    iq_samples: np.ndarray,
    sample_rate: int,
    minute_timestamp: float
) -> Tuple[float, float, Optional[float], List[ToneDetectionResult]]
```

**Parameters:**
- `iq_samples` (np.ndarray): Complex IQ for full minute  
- `sample_rate` (int): Sample rate in Hz (typically 16000)
- `minute_timestamp` (float): UTC timestamp of minute boundary

**Returns:** `Tuple[wwv_power_db, wwvh_power_db, differential_delay_ms, detections]`
- `wwv_power_db` (float): WWV power in dB (-inf if not detected)
- `wwvh_power_db` (float): WWVH power in dB (-inf if not detected)
- `differential_delay_ms` (Optional[float]): Delay between stations
- `detections` (List[ToneDetectionResult]): Individual tone results

**CSV Output:** `{CHANNEL}_tones_YYYYMMDD.csv`  
**Columns:** timestamp_utc, station, frequency_hz, duration_sec, timing_error_ms, snr_db, tone_power_db, confidence

### Method 2: Tick Windows (5ms ticks)

```python
def detect_tick_windows(
    self,
    iq_samples: np.ndarray,
    sample_rate: int
) -> List[Dict[str, Any]]
```

**Parameters:**
- `iq_samples` (np.ndarray): Complex IQ for full minute
- `sample_rate` (int): Sample rate in Hz (typically 16000)

**Returns:** `List[Dict]` - Up to 6 windows per minute (every 10 seconds)

**Window Dict Fields:**
- `second` (int): Window start (0, 10, 20, 30, 40, 50)
- `coherent_wwv_snr_db` (float): Coherent integration SNR WWV
- `coherent_wwvh_snr_db` (float): Coherent integration SNR WWVH
- `incoherent_wwv_snr_db` (float): Incoherent integration SNR WWV
- `incoherent_wwvh_snr_db` (float): Incoherent integration SNR WWVH
- `coherence_quality_wwv` (float): Phase coherence quality WWV (0-1)
- `coherence_quality_wwvh` (float): Phase coherence quality WWVH (0-1)
- `integration_method` (str): 'coherent' or 'incoherent'
- `wwv_snr_db` (float): Selected SNR WWV
- `wwvh_snr_db` (float): Selected SNR WWVH
- `ratio_db` (float): WWV/WWVH power ratio
- `tick_count` (int): Number of detected ticks

**CSV Output:** `{CHANNEL}_ticks_YYYYMMDD.csv`  
**Columns:** timestamp_utc, window_second, coherent_wwv_snr_db, coherent_wwvh_snr_db, incoherent_wwv_snr_db, incoherent_wwvh_snr_db, coherence_quality_wwv, coherence_quality_wwvh, integration_method, wwv_snr_db, wwvh_snr_db, ratio_db, tick_count

### Method 3: Station ID (440 Hz)

```python
def detect_440hz_tone(
    self,
    iq_samples: np.ndarray,
    sample_rate: int,
    minute_number: int
) -> Tuple[bool, Optional[float]]
```

**Parameters:**
- `iq_samples` (np.ndarray): Complex IQ for full minute
- `sample_rate` (int): Sample rate in Hz (typically 16000)
- `minute_number` (int): Minute number (0-59)

**Returns:** `Tuple[detected, power_db]`
- `detected` (bool): True if 440 Hz detected
- `power_db` (Optional[float]): Power in dB if detected

**Station Assignment:**
- Minute 1 → WWVH
- Minute 2 → WWV
- Other minutes → No detection expected

**CSV Output:** `{CHANNEL}_440hz_YYYYMMDD.csv`  
**Columns:** timestamp_utc, minute_number, wwv_detected, wwvh_detected, wwv_power_db, wwvh_power_db

### Method 4: BCD Discrimination (100 Hz)

```python
def detect_bcd_discrimination(
    self,
    iq_samples: np.ndarray,
    sample_rate: int,
    minute_timestamp: float
) -> Tuple[float, float, Optional[float], Optional[float], List[Dict[str, Any]]]
```

**Parameters:**
- `iq_samples` (np.ndarray): Complex IQ for full minute
- `sample_rate` (int): Sample rate in Hz (typically 16000)
- `minute_timestamp` (float): UTC timestamp of minute boundary

**Returns:** `Tuple[wwv_amplitude, wwvh_amplitude, delay_ms, quality, bcd_windows]`
- `wwv_amplitude` (float): Aggregate WWV amplitude
- `wwvh_amplitude` (float): Aggregate WWVH amplitude
- `differential_delay_ms` (Optional[float]): Propagation delay
- `correlation_quality` (Optional[float]): Quality (0-1)
- `bcd_windows` (List[Dict]): Individual window results

**BCD Window Dict Fields:**
- `window_start` or `window_start_sec` (float): Start time (seconds)
- `wwv_amplitude` (float): WWV BCD amplitude
- `wwvh_amplitude` (float): WWVH BCD amplitude
- `differential_delay_ms` or `differential_delay` (Optional[float]): Delay
- `correlation_quality` (float): Quality (0-1)

**CSV Output:** `{CHANNEL}_bcd_YYYYMMDD.csv`  
**Columns:** timestamp_utc, window_start_sec, wwv_amplitude, wwvh_amplitude, differential_delay_ms, correlation_quality, amplitude_ratio_db

### Method 5: Weighted Voting (Final)

```python
def analyze_minute_with_440hz(
    self,
    iq_samples: np.ndarray,
    sample_rate: int,
    minute_timestamp: float,
    detections: Optional[List[ToneDetectionResult]] = None
) -> Optional[DiscriminationResult]
```

**Parameters:**
- `iq_samples` (np.ndarray): Complex IQ for full minute
- `sample_rate` (int): Sample rate in Hz (typically 16000)
- `minute_timestamp` (float): UTC timestamp of minute boundary
- `detections` (Optional[List[ToneDetectionResult]]): Pre-computed tones
  - If None: Detects tones internally (recommended for reprocessing)
  - If provided: Uses these detections (real-time mode)

**Returns:** `Optional[DiscriminationResult]` - Complete analysis or None

**Internally Calls:**
1. `detect_timing_tones()` (if detections=None)
2. `detect_440hz_tone()`
3. `detect_tick_windows()`
4. `detect_bcd_discrimination()`
5. `finalize_discrimination()` (weighted voting)

**CSV Output:** `{CHANNEL}_discrimination_YYYYMMDD.csv`  
**Columns:** timestamp_utc, minute_timestamp, minute_number, wwv_detected, wwvh_detected, wwv_snr_db, wwvh_snr_db, power_ratio_db, differential_delay_ms, tone_440hz_wwv_detected, tone_440hz_wwv_power_db, tone_440hz_wwvh_detected, tone_440hz_wwvh_power_db, dominant_station, confidence, tick_windows_10sec (JSON), bcd_wwv_amplitude, bcd_wwvh_amplitude, bcd_differential_delay_ms, bcd_correlation_quality, bcd_windows (JSON)

---

## CSV Writers API

**Module:** `src/signal_recorder/discrimination_csv_writers.py`

### Class: `DiscriminationCSVWriters`

Manages daily CSV files for all 5 discrimination methods.

#### Constructor

```python
DiscriminationCSVWriters(
    data_root: str,
    channel_name: str
)
```

**Parameters:**
- `data_root` (str): Root directory (e.g., "/tmp/grape-test")
- `channel_name` (str): Channel name (e.g., "WWV 5 MHz")

#### Methods

```python
def append_tone_detections(
    self,
    records: List[Dict],
    date_obj: date
) -> None
```

Writes tone detection records to `{CHANNEL}_tones_YYYYMMDD.csv`

```python
def append_tick_windows(
    self,
    records: List[Dict],
    date_obj: date
) -> None
```

Writes tick window records to `{CHANNEL}_ticks_YYYYMMDD.csv`

```python
def append_440hz_detections(
    self,
    records: List[Dict],
    date_obj: date
) -> None
```

Writes 440 Hz records to `{CHANNEL}_440hz_YYYYMMDD.csv`

```python
def append_bcd_windows(
    self,
    records: List[Dict],
    date_obj: date
) -> None
```

Writes BCD window records to `{CHANNEL}_bcd_YYYYMMDD.csv`

```python
def append_discrimination_results(
    self,
    results: List[DiscriminationResult],
    date_obj: date
) -> None
```

Writes final discrimination to `{CHANNEL}_discrimination_YYYYMMDD.csv`

---

## Data Models

**Module:** `src/signal_recorder/interfaces/data_models.py`

### `ToneDetectionResult`

```python
@dataclass(frozen=True)
class ToneDetectionResult:
    station: StationType               # WWV, WWVH, or CHU
    frequency_hz: float                # 1000 or 1200 Hz
    duration_sec: float                # Tone duration (seconds)
    timestamp_utc: float               # Tone onset (UTC)
    timing_error_ms: float             # Error vs minute boundary (ms)
    snr_db: float                      # Signal-to-noise ratio (dB)
    confidence: float                  # Detection confidence (0-1)
    use_for_time_snap: bool            # Use for timing (WWV/CHU only)
    correlation_peak: float            # Matched filter peak
    noise_floor: float                 # Noise floor estimate
    tone_power_db: Optional[float]     # Power relative to noise (dB)
```

### `DiscriminationResult`

**Module:** `src/signal_recorder/wwvh_discrimination.py`

```python
@dataclass
class DiscriminationResult:
    # Timing tones
    minute_timestamp: float
    wwv_detected: bool
    wwvh_detected: bool
    wwv_power_db: float
    wwvh_power_db: float
    power_ratio_db: float
    differential_delay_ms: Optional[float]
    
    # 440 Hz station ID
    tone_440hz_detected: bool
    tone_440hz_wwv_power_db: Optional[float]
    tone_440hz_wwvh_power_db: Optional[float]
    
    # Tick windows
    tick_windows: List[Dict]
    
    # BCD discrimination
    bcd_wwv_amplitude: float
    bcd_wwvh_amplitude: float
    bcd_differential_delay_ms: Optional[float]
    bcd_correlation_quality: Optional[float]
    bcd_windows: List[Dict]
    
    # Final discrimination
    dominant_station: str              # "WWV", "WWVH", "BALANCED", "UNKNOWN"
    confidence: str                    # "HIGH", "MEDIUM", "LOW"
```

---

## Directory Structure Reference

See `DIRECTORY_STRUCTURE.md` for complete path specifications.

**Key Directories:**
- `archives/{CHANNEL}/` - Raw 16 kHz IQ NPZ files
- `analytics/{CHANNEL}/decimated/` - 10 Hz decimated NPZ
- `analytics/{CHANNEL}/tone_detections/` - Tone detection CSVs
- `analytics/{CHANNEL}/tick_windows/` - Tick analysis CSVs
- `analytics/{CHANNEL}/station_id_440hz/` - 440 Hz ID CSVs
- `analytics/{CHANNEL}/bcd_discrimination/` - BCD CSVs
- `analytics/{CHANNEL}/discrimination/` - Final voting CSVs

**File Naming:**
- Archives: `YYYYMMDDTHHMMSSZ_{FREQ}_iq.npz`
- Analytics: `{CHANNEL}_{METHOD}_YYYYMMDD.csv`
- **NO time-range suffixes** - One file per day per method

---

## Usage Examples

### Complete Workflow

```python
from signal_recorder.wwvh_discrimination import WWVHDiscriminator
from signal_recorder.discrimination_csv_writers import DiscriminationCSVWriters
from signal_recorder.paths import GRAPEPaths
from datetime import datetime, timezone, date
import numpy as np

# Initialize
paths = GRAPEPaths("/tmp/grape-test")
discriminator = WWVHDiscriminator("WWV 5 MHz")
writers = DiscriminationCSVWriters("/tmp/grape-test", "WWV 5 MHz")

# Load NPZ
archive_path = paths.get_archive_dir("WWV 5 MHz") / "20251119T120000Z_5000000_iq.npz"
data = np.load(archive_path)
iq_samples = data['iq']
sample_rate = int(data['sample_rate'])
minute_timestamp = float(data['unix_timestamp'])

# Run all 5 methods
wwv_pwr, wwvh_pwr, delay, tones = discriminator.detect_timing_tones(
    iq_samples, sample_rate, minute_timestamp
)
tick_windows = discriminator.detect_tick_windows(iq_samples, sample_rate)
minute_num = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).minute
detected_440, power_440 = discriminator.detect_440hz_tone(
    iq_samples, sample_rate, minute_num
)
wwv_amp, wwvh_amp, bcd_delay, quality, bcd_windows = discriminator.detect_bcd_discrimination(
    iq_samples, sample_rate, minute_timestamp
)
result = discriminator.analyze_minute_with_440hz(
    iq_samples, sample_rate, minute_timestamp
)

# Write CSVs
today = date.fromtimestamp(minute_timestamp)
writers.append_tone_detections([...], today)
writers.append_tick_windows([...], today)
writers.append_440hz_detections([...], today)
writers.append_bcd_windows([...], today)
writers.append_discrimination_results([result], today)
```

---

## See Also

- `DIRECTORY_STRUCTURE.md` - Complete path specifications
- `src/signal_recorder/paths.py` - Path management implementation
- `src/signal_recorder/wwvh_discrimination.py` - Discrimination implementation
- `src/signal_recorder/tone_detector.py` - Tone detection implementation
- `src/signal_recorder/discrimination_csv_writers.py` - CSV writers
- `scripts/validate_api_compliance.py` - API compliance validator
