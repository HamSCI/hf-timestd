# API Quick Reference

## Essential Signatures

### Tone Detector
```python
from signal_recorder.tone_detector import MultiStationToneDetector
from scipy import signal as scipy_signal

# Initialize
detector = MultiStationToneDetector(channel_name="WWV 5 MHz")

# Resample 16 kHz → 3 kHz
resampled = scipy_signal.resample_poly(iq_16khz, up=3, down=16, axis=0)

# Detect
detections = detector.process_samples(
    timestamp=minute_utc,      # UTC timestamp
    samples=resampled,         # 3 kHz IQ samples
    rtp_timestamp=rtp_ts       # RTP timestamp
) -> List[ToneDetectionResult]
```

### WWV-H Discrimination
```python
from signal_recorder.wwvh_discrimination import WWVHDiscriminator

# Initialize
disc = WWVHDiscriminator(channel_name="WWV 5 MHz")

# Analyze (FULL - includes 440 Hz)
result = disc.analyze_minute_with_440hz(
    iq_samples=np.ndarray,           # Full minute IQ
    sample_rate=16000,                # Hz
    minute_timestamp=float,           # UTC
    detections=List[ToneDetectionResult]
) -> DiscriminationResult
```

### Analytics Service
```python
from signal_recorder.analytics_service import AnalyticsService

service = AnalyticsService(
    channel_name="WWV 5 MHz",
    frequency_hz=5_000_000,
    archive_directory=Path("/path/to/archives"),
    output_directory=Path("/path/to/output"),
    station_config={
        'callsign': 'W1ABC',
        'grid_square': 'FN42',
        'receiver_name': 'grape_v2_rx',
        'psws_station_id': 'station_001',
        'psws_instrument_id': 'grape_v2'
    }
)

service.run(poll_interval=10.0)
```

### DRF Writer Service
```python
from signal_recorder.drf_writer_service import DRFWriterService

service = DRFWriterService(
    input_dir=Path("/path/to/10hz/npz"),
    output_dir=Path("/path/to/digital_rf"),
    channel_name="WWV 5 MHz",
    frequency_hz=5_000_000,
    analytics_state_file=Path("/path/to/analytics_state.json"),
    station_config={
        'callsign': 'W1ABC',
        'grid_square': 'FN42',
        'receiver_name': 'grape_v2_rx',
        'psws_station_id': 'station_001',
        'psws_instrument_id': 'grape_v2'
    }
)

service.run(poll_interval=10.0)
```

## Station Config Template

```python
station_config = {
    'callsign': str,              # Required
    'grid_square': str,           # Required
    'receiver_name': str,         # Required
    'psws_station_id': str,       # Required
    'psws_instrument_id': str     # Required
}
```

## Data Models

```python
# Tone Detection Result
ToneDetectionResult(
    station: StationType,          # WWV, WWVH, CHU
    timestamp: float,              # UTC
    timing_error_ms: float,
    snr_db: float,
    confidence: float,
    tone_power_db: float,
    use_for_time_snap: bool
)

# Discrimination Result
DiscriminationResult(
    minute_timestamp: float,
    wwv_detected: bool,
    wwvh_detected: bool,
    wwv_power_db: Optional[float],
    wwvh_power_db: Optional[float],
    power_ratio_db: Optional[float],
    differential_delay_ms: Optional[float],
    dominant_station: Optional[str],  # 'WWV', 'WWVH', 'BALANCED'
    confidence: str,                   # 'high', 'medium', 'low'
    tone_440hz_wwv_detected: bool,
    tone_440hz_wwv_power_db: Optional[float],
    tone_440hz_wwvh_detected: bool,
    tone_440hz_wwvh_power_db: Optional[float]
)
```

## File Formats

```
Raw NPZ (16 kHz):       {timestamp}Z_{freq}_iq.npz
Decimated NPZ (10 Hz):  {timestamp}Z_{freq}_iq_10hz.npz
CSV Quality:            {channel}_quality.csv
CSV Discrimination:     {channel}_discrimination_{date}.csv
Digital RF:             rf@{timestamp}.h5 (HDF5 format)
Spectrogram PNG:        {channel}_{date}_carrier_spectrogram.png
```

## Data Flow Architecture

```
Core Recorder (RTP → NPZ)
    ↓
{timestamp}_iq.npz (16 kHz)
    ↓
Analytics Service (Tone Detection + Decimation)
    ├→ Tone Detection (WWV/WWVH/CHU @ 1000/1200 Hz)
    ├→ Time_snap Management (establishes GPS-quality timestamps)
    ├→ Quality Metrics (completeness, packet loss, gaps)
    ├→ Discrimination Analysis (WWV vs WWVH with 440 Hz)
    └→ Decimation (16 kHz → 10 Hz)
         ↓
    {timestamp}_iq_10hz.npz (embedded metadata)
         ├→ DRF Writer Service → Digital RF HDF5 (for PSWS upload)
         └→ Spectrogram Generator → PNG (for web UI carrier display)
```

## 10 Hz NPZ Metadata Structure

The decimated NPZ files include embedded metadata for downstream consumers:

```python
# Core Data
iq: np.ndarray                      # Decimated IQ samples (10 Hz)
rtp_timestamp: int                  # RTP timestamp from source
sample_rate_original: int           # 16000
sample_rate_decimated: int          # 10
decimation_factor: int              # 1600
created_timestamp: float            # Unix timestamp when created
source_file: str                    # Original 16kHz NPZ filename

# Timing Metadata (for DRF quality annotations)
timing_metadata: dict
    quality: str                    # 'tone_locked', 'ntp_synced', 'wall_clock'
    time_snap_age_seconds: float    # Age of time_snap reference
    ntp_offset_ms: float           # NTP offset if checked
    reprocessing_recommended: bool  # True if low-quality timing

# Quality Metadata (for gap/loss tracking)
quality_metadata: dict
    completeness_pct: float         # % of expected samples present
    packet_loss_pct: float          # % of packets lost
    gaps_count: int                 # Number of gaps detected
    gaps_filled: int                # Samples filled with zeros

# Tone Detection Metadata (for discrimination/analysis)
tone_metadata: dict
    detections: list[dict]
        station: str                # 'WWV', 'WWVH', 'CHU'
        frequency_hz: int           # 1000, 1200
        timing_error_ms: float      # Error vs :00.000
        snr_db: float              # Signal-to-noise ratio
        confidence: float          # 0.0-1.0
```

## Import Paths

```python
from signal_recorder.tone_detector import MultiStationToneDetector
from signal_recorder.wwvh_discrimination import WWVHDiscriminator
from signal_recorder.analytics_service import AnalyticsService
from signal_recorder.drf_writer_service import DRFWriterService, DecimatedArchive
from signal_recorder.interfaces.data_models import NPZArchive, ToneDetectionResult
```
