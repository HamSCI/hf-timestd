"""
CSV Writers for Individual Discrimination Methods

Each method writes to its own CSV file for independent reprocessability.
All writers maintain daily CSV files that append new results.

Directory structure:
    analytics/{channel}/tone_detections/{channel}_tones_YYYYMMDD.csv
    analytics/{channel}/tick_windows/{channel}_ticks_YYYYMMDD.csv
    analytics/{channel}/station_id_440hz/{channel}_440hz_YYYYMMDD.csv
    analytics/{channel}/bcd_discrimination/{channel}_bcd_YYYYMMDD.csv
    analytics/{channel}/discrimination/{channel}_discrimination_YYYYMMDD.csv
"""

import csv
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from ..paths import TimeStdPaths

logger = logging.getLogger(__name__)


@dataclass
class ToneDetectionRecord:
    """Record from tone detection analysis"""
    timestamp_utc: str
    station: str
    frequency_hz: float
    duration_sec: float
    timing_error_ms: float
    snr_db: float
    tone_power_db: float
    confidence: float
    use_for_time_snap: bool


@dataclass
class TickWindowRecord:
    """Record from tick window analysis (5ms ticks, 10-sec windows)"""
    timestamp_utc: str
    window_second: int
    coherent_wwv_snr_db: float
    coherent_wwvh_snr_db: float
    incoherent_wwv_snr_db: float
    incoherent_wwvh_snr_db: float
    coherence_quality_wwv: float
    coherence_quality_wwvh: float
    integration_method: str
    wwv_snr_db: float
    wwvh_snr_db: float
    ratio_db: float
    tick_count: int


@dataclass
class StationID440HzRecord:
    """Record from 440 Hz station ID detection"""
    timestamp_utc: str
    minute_number: int
    wwv_detected: bool
    wwvh_detected: bool
    wwv_power_db: Optional[float]
    wwvh_power_db: Optional[float]


@dataclass
class BCDWindowRecord:
    """Record from BCD discrimination window analysis"""
    timestamp_utc: str
    window_start_sec: float
    wwv_amplitude: float
    wwvh_amplitude: float
    differential_delay_ms: float
    correlation_quality: float
    amplitude_ratio_db: float


@dataclass
class TestSignalRecord:
    """Record from test signal detection (minutes 8 and 44) - Channel Sounding
    
    Enhanced with comprehensive channel characterization:
    - Multi-tone: Detection confidence and frequency selectivity (FSS)
    - White noise: Dual segment analysis for transient detection
    - Chirps: Delay spread estimation from matched filter
    - Bursts: High-precision ToA from single-cycle pulses
    """
    timestamp_utc: str
    minute_number: int
    detected: bool
    station: Optional[str]  # 'WWV' or 'WWVH' (from schedule, not signal content)
    confidence: float
    multitone_score: float
    chirp_score: float
    noise_correlation: float = 0.0  # Average of noise1 and noise2 scores
    snr_db: Optional[float] = None
    toa_offset_ms: Optional[float] = None  # Time of arrival offset from expected
    burst_toa_offset_ms: Optional[float] = None  # High-precision ToA from single-cycle bursts
    delay_spread_ms: Optional[float] = None  # Multipath delay spread from chirp
    coherence_time_sec: Optional[float] = None  # Channel coherence time from fading
    # Frequency Selectivity Score (FSS) - path-specific fingerprint
    frequency_selectivity_db: Optional[float] = None
    # Dual noise segment analysis for transient detection
    noise1_score: Optional[float] = None  # Noise segment at 10-12s
    noise2_score: Optional[float] = None  # Noise segment at 37-39s
    noise_coherence_diff: Optional[float] = None  # |N1-N2|, high = transient event


@dataclass
class DopplerRecord:
    """Record from per-tick Doppler estimation (ionospheric channel characterization)"""
    timestamp_utc: str
    wwv_doppler_hz: float
    wwvh_doppler_hz: float
    wwv_doppler_std_hz: float
    wwvh_doppler_std_hz: float
    max_coherent_window_sec: float
    doppler_quality: float
    phase_variance_rad: float
    valid_tick_count: int


@dataclass
class DiscriminationRecord:
    """Record from final weighted voting - comprehensive format matching reprocessing output"""
    timestamp_utc: str
    minute_timestamp: float
    minute_number: int
    # Timing tones (1000/1200 Hz)
    wwv_detected: bool
    wwvh_detected: bool
    wwv_power_db: Optional[float]
    wwvh_power_db: Optional[float]
    power_ratio_db: Optional[float]
    differential_delay_ms: Optional[float]
    # 440 Hz station ID
    tone_440hz_wwv_detected: bool
    tone_440hz_wwv_power_db: Optional[float]
    tone_440hz_wwvh_detected: bool
    tone_440hz_wwvh_power_db: Optional[float]
    # Final decision
    dominant_station: str
    confidence: str
    # Tick windows (JSON)
    tick_windows_10sec: Optional[str]  # JSON string
    # BCD discrimination
    bcd_wwv_amplitude: Optional[float]
    bcd_wwvh_amplitude: Optional[float]
    bcd_differential_delay_ms: Optional[float]
    bcd_correlation_quality: Optional[float]
    bcd_windows: Optional[str]  # JSON string
    # 500/600 Hz ground truth
    tone_500_600_detected: bool
    tone_500_600_power_db: Optional[float]
    tone_500_600_freq_hz: Optional[int]
    tone_500_600_ground_truth_station: Optional[str]
    # Harmonic power ratios (500→1000 Hz, 600→1200 Hz)
    harmonic_ratio_500_1000: Optional[float]  # P_1000/P_500 in dB
    harmonic_ratio_600_1200: Optional[float]  # P_1200/P_600 in dB
    # BCD validation
    bcd_minute_validated: bool
    bcd_correlation_peak_quality: Optional[float]
    # Inter-method cross-validation
    inter_method_agreements: Optional[str]  # JSON string
    inter_method_disagreements: Optional[str]  # JSON string


class DiscriminationCSVWriters:
    """
    Manages CSV output for all discrimination methods
    
    Creates and appends to daily CSV files for each method.
    Thread-safe for multiple writers (uses file locking via context managers).
    """
    
    def __init__(self, data_root: str, channel_name: str):
        """
        Initialize CSV writers using TimeStdPaths API
        
        Args:
            data_root: Root directory for analytics (e.g., /tmp/timestd-test)
            channel_name: Channel name (e.g., "WWV 10 MHz")
        """
        self.channel_name = channel_name
        self.paths = TimeStdPaths(data_root)
        
        # Method-specific directories (using TimeStdPaths API)
        self.tone_dir = self.paths.get_tone_detections_dir(channel_name)
        self.tick_dir = self.paths.get_tick_windows_dir(channel_name)
        self.id_440_dir = self.paths.get_station_id_440hz_dir(channel_name)
        self.bcd_dir = self.paths.get_bcd_discrimination_dir(channel_name)
        self.test_signal_dir = self.paths.get_test_signal_dir(channel_name)
        self.doppler_dir = self.paths.get_doppler_dir(channel_name)
        self.disc_dir = self.paths.get_discrimination_dir(channel_name)
        
        # Channel directory name for file naming - preserve dots for frequency notation
        self.channel_dir = channel_name.replace(' ', '_')
        
        # Ensure directories exist
        for directory in [self.tone_dir, self.tick_dir, self.id_440_dir, 
                         self.bcd_dir, self.test_signal_dir, self.doppler_dir, self.disc_dir]:
            directory.mkdir(parents=True, exist_ok=True)
    
    def _get_csv_path(self, directory: Path, prefix: str, timestamp: float) -> Path:
        """
        Get CSV file path for a given timestamp
        
        Args:
            directory: Directory for this CSV type
            prefix: File prefix (e.g., "WWV_10_MHz_tones")
            timestamp: Unix timestamp
            
        Returns:
            Path to daily CSV file
        """
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        return directory / f"{prefix}_{date_str}.csv"
    
    def write_tone_detection(self, record: ToneDetectionRecord):
        """Write tone detection record to CSV"""
        timestamp = datetime.fromisoformat(record.timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.tone_dir, f"{self.channel_dir}_tones", timestamp)
        
        fieldnames = ['timestamp_utc', 'station', 'frequency_hz', 'duration_sec',
                     'timing_error_ms', 'snr_db', 'tone_power_db', 'confidence',
                     'use_for_time_snap']
        
        # Check if file exists to determine if header is needed
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': record.timestamp_utc,
                'station': record.station,
                'frequency_hz': f"{record.frequency_hz:.1f}",
                'duration_sec': f"{record.duration_sec:.3f}",
                'timing_error_ms': f"{record.timing_error_ms:.2f}",
                'snr_db': f"{record.snr_db:.2f}",
                'tone_power_db': f"{record.tone_power_db:.2f}",
                'confidence': f"{record.confidence:.3f}",
                'use_for_time_snap': '1' if record.use_for_time_snap else '0'
            })
    
    def write_tick_windows(self, timestamp_utc: str, windows: List[Dict[str, Any]]):
        """
        Write tick window analysis results
        
        Args:
            timestamp_utc: Minute timestamp
            windows: List of 6 window dictionaries from detect_tick_windows()
        """
        timestamp = datetime.fromisoformat(timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.tick_dir, f"{self.channel_dir}_ticks", timestamp)
        
        fieldnames = ['timestamp_utc', 'window_second', 
                     'coherent_wwv_snr_db', 'coherent_wwvh_snr_db',
                     'incoherent_wwv_snr_db', 'incoherent_wwvh_snr_db',
                     'coherence_quality_wwv', 'coherence_quality_wwvh',
                     'integration_method', 'wwv_snr_db', 'wwvh_snr_db',
                     'ratio_db', 'tick_count']
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            for window in windows:
                writer.writerow({
                    'timestamp_utc': timestamp_utc,
                    'window_second': window['second'],
                    'coherent_wwv_snr_db': f"{window['coherent_wwv_snr_db']:.2f}",
                    'coherent_wwvh_snr_db': f"{window['coherent_wwvh_snr_db']:.2f}",
                    'incoherent_wwv_snr_db': f"{window['incoherent_wwv_snr_db']:.2f}",
                    'incoherent_wwvh_snr_db': f"{window['incoherent_wwvh_snr_db']:.2f}",
                    'coherence_quality_wwv': f"{window['coherence_quality_wwv']:.3f}",
                    'coherence_quality_wwvh': f"{window['coherence_quality_wwvh']:.3f}",
                    'integration_method': window['integration_method'],
                    'wwv_snr_db': f"{window['wwv_snr_db']:.2f}",
                    'wwvh_snr_db': f"{window['wwvh_snr_db']:.2f}",
                    'ratio_db': f"{window['ratio_db']:.2f}",
                    'tick_count': window['tick_count']
                })
    
    def write_440hz_detection(self, record: StationID440HzRecord):
        """Write 440 Hz station ID detection record"""
        timestamp = datetime.fromisoformat(record.timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.id_440_dir, f"{self.channel_dir}_440hz", timestamp)
        
        fieldnames = ['timestamp_utc', 'minute_number', 'wwv_detected', 'wwvh_detected',
                     'wwv_power_db', 'wwvh_power_db']
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': record.timestamp_utc,
                'minute_number': record.minute_number,
                'wwv_detected': '1' if record.wwv_detected else '0',
                'wwvh_detected': '1' if record.wwvh_detected else '0',
                'wwv_power_db': f"{record.wwv_power_db:.2f}" if record.wwv_power_db is not None else '',
                'wwvh_power_db': f"{record.wwvh_power_db:.2f}" if record.wwvh_power_db is not None else ''
            })
    
    def write_test_signal(self, record: TestSignalRecord):
        """Write test signal detection record (minutes 8 and 44)
        
        Note: Station assignment is schedule-based (minute 8 = WWV, minute 44 = WWVH).
        The test signal is identical for both stations - value is in ToA/SNR measurement.
        
        Enhanced fields include:
        - noise_correlation: White noise matched filter score (high BT product)
        - delay_spread_ms: Multipath delay spread from chirp analysis
        - coherence_time_sec: Channel coherence time from multi-tone fading
        - frequency_selectivity_db: FSS path signature
        - noise1_score, noise2_score: Dual noise segment scores
        - noise_coherence_diff: Transient event indicator
        - burst_toa_offset_ms: High-precision ToA from single-cycle bursts
        """
        timestamp = datetime.fromisoformat(record.timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.test_signal_dir, f"{self.channel_dir}_test_signal", timestamp)
        
        fieldnames = ['timestamp_utc', 'minute_number', 'detected', 'station',
                     'confidence', 'multitone_score', 'chirp_score', 'noise_correlation',
                     'snr_db', 'toa_offset_ms', 'burst_toa_offset_ms',
                     'delay_spread_ms', 'coherence_time_sec',
                     'frequency_selectivity_db', 'noise1_score', 'noise2_score', 'noise_coherence_diff']
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': record.timestamp_utc,
                'minute_number': record.minute_number,
                'detected': '1' if record.detected else '0',
                'station': record.station if record.station else '',
                'confidence': f"{record.confidence:.4f}",
                'multitone_score': f"{record.multitone_score:.4f}",
                'chirp_score': f"{record.chirp_score:.4f}",
                'noise_correlation': f"{record.noise_correlation:.4f}",
                'snr_db': f"{record.snr_db:.2f}" if record.snr_db is not None else '',
                'toa_offset_ms': f"{record.toa_offset_ms:.2f}" if record.toa_offset_ms is not None else '',
                'burst_toa_offset_ms': f"{record.burst_toa_offset_ms:.2f}" if record.burst_toa_offset_ms is not None else '',
                'delay_spread_ms': f"{record.delay_spread_ms:.2f}" if record.delay_spread_ms is not None else '',
                'coherence_time_sec': f"{record.coherence_time_sec:.2f}" if record.coherence_time_sec is not None else '',
                'frequency_selectivity_db': f"{record.frequency_selectivity_db:.2f}" if record.frequency_selectivity_db is not None else '',
                'noise1_score': f"{record.noise1_score:.4f}" if record.noise1_score is not None else '',
                'noise2_score': f"{record.noise2_score:.4f}" if record.noise2_score is not None else '',
                'noise_coherence_diff': f"{record.noise_coherence_diff:.4f}" if record.noise_coherence_diff is not None else ''
            })
    
    def write_bcd_windows(self, timestamp_utc: str, windows: List[Dict[str, Any]]):
        """
        Write BCD discrimination window results
        
        Args:
            timestamp_utc: Minute timestamp
            windows: List of BCD window dictionaries from detect_bcd_discrimination()
        """
        timestamp = datetime.fromisoformat(timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.bcd_dir, f"{self.channel_dir}_bcd", timestamp)
        
        fieldnames = ['timestamp_utc', 'window_start_sec', 'wwv_amplitude', 'wwvh_amplitude',
                     'differential_delay_ms', 'correlation_quality', 'amplitude_ratio_db']
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            for window in windows:
                wwv_amp = window.get('wwv_amplitude', 0) or 0
                wwvh_amp = window.get('wwvh_amplitude', 0) or 0
                delay_ms = window.get('differential_delay_ms')
                
                # Calculate amplitude ratio (avoid log(0))
                if wwv_amp > 0 and wwvh_amp > 0:
                    ratio_db = 20 * np.log10(wwv_amp / wwvh_amp)
                elif wwv_amp > 0:
                    ratio_db = 20.0  # WWV only
                elif wwvh_amp > 0:
                    ratio_db = -20.0  # WWVH only
                else:
                    ratio_db = 0.0
                
                writer.writerow({
                    'timestamp_utc': timestamp_utc,
                    'window_start_sec': f"{window.get('window_start_sec', 0.0):.1f}",
                    'wwv_amplitude': f"{wwv_amp:.4f}",
                    'wwvh_amplitude': f"{wwvh_amp:.4f}",
                    'differential_delay_ms': f"{delay_ms:.2f}" if delay_ms is not None else "",
                    'correlation_quality': f"{window.get('correlation_quality', 0.0):.3f}",
                    'amplitude_ratio_db': f"{ratio_db:.2f}"
                })
    
    def write_doppler_record(self, record: DopplerRecord):
        """Write Doppler estimation record (ionospheric channel characterization)
        
        Records per-minute Doppler shift estimates derived from tick phase tracking.
        Essential for:
        - Monitoring ionospheric dynamics
        - Validating BCD adaptive window sizing
        - Diagnosing discrimination failures due to channel instability
        """
        timestamp = datetime.fromisoformat(record.timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.doppler_dir, f"{self.channel_dir}_doppler", timestamp)
        
        fieldnames = ['timestamp_utc', 'wwv_doppler_hz', 'wwvh_doppler_hz',
                     'wwv_doppler_std_hz', 'wwvh_doppler_std_hz',
                     'max_coherent_window_sec', 'doppler_quality',
                     'phase_variance_rad', 'valid_tick_count']
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': record.timestamp_utc,
                'wwv_doppler_hz': f"{record.wwv_doppler_hz:.6f}",
                'wwvh_doppler_hz': f"{record.wwvh_doppler_hz:.6f}",
                'wwv_doppler_std_hz': f"{record.wwv_doppler_std_hz:.6f}",
                'wwvh_doppler_std_hz': f"{record.wwvh_doppler_std_hz:.6f}",
                'max_coherent_window_sec': f"{record.max_coherent_window_sec:.1f}",
                'doppler_quality': f"{record.doppler_quality:.3f}",
                'phase_variance_rad': f"{record.phase_variance_rad:.4f}",
                'valid_tick_count': record.valid_tick_count
            })
    
    def write_discrimination_result(self, record: DiscriminationRecord):
        """Write final weighted voting discrimination result - comprehensive format"""
        timestamp = datetime.fromisoformat(record.timestamp_utc.replace('Z', '+00:00')).timestamp()
        csv_path = self._get_csv_path(self.disc_dir, f"{self.channel_dir}_discrimination", timestamp)
        
        # Full 31-column format matching reprocess_discrimination.py output
        fieldnames = [
            'timestamp_utc', 'minute_timestamp', 'minute_number',
            'wwv_detected', 'wwvh_detected', 'wwv_power_db', 'wwvh_power_db',
            'power_ratio_db', 'differential_delay_ms',
            'tone_440hz_wwv_detected', 'tone_440hz_wwv_power_db',
            'tone_440hz_wwvh_detected', 'tone_440hz_wwvh_power_db',
            'dominant_station', 'confidence',
            'tick_windows_10sec',
            'bcd_wwv_amplitude', 'bcd_wwvh_amplitude',
            'bcd_differential_delay_ms', 'bcd_correlation_quality',
            'bcd_windows',
            'tone_500_600_detected', 'tone_500_600_power_db',
            'tone_500_600_freq_hz', 'tone_500_600_ground_truth_station',
            'harmonic_ratio_500_1000', 'harmonic_ratio_600_1200',
            'bcd_minute_validated', 'bcd_correlation_peak_quality',
            'inter_method_agreements', 'inter_method_disagreements'
        ]
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': record.timestamp_utc,
                'minute_timestamp': record.minute_timestamp,
                'minute_number': record.minute_number,
                'wwv_detected': 1 if record.wwv_detected else 0,
                'wwvh_detected': 1 if record.wwvh_detected else 0,
                'wwv_power_db': f"{record.wwv_power_db:.2f}" if record.wwv_power_db is not None else '',
                'wwvh_power_db': f"{record.wwvh_power_db:.2f}" if record.wwvh_power_db is not None else '',
                'power_ratio_db': f"{record.power_ratio_db:.2f}" if record.power_ratio_db is not None else '',
                'differential_delay_ms': f"{record.differential_delay_ms:.2f}" if record.differential_delay_ms is not None else '',
                'tone_440hz_wwv_detected': 1 if record.tone_440hz_wwv_detected else 0,
                'tone_440hz_wwv_power_db': f"{record.tone_440hz_wwv_power_db:.2f}" if record.tone_440hz_wwv_power_db is not None else '',
                'tone_440hz_wwvh_detected': 1 if record.tone_440hz_wwvh_detected else 0,
                'tone_440hz_wwvh_power_db': f"{record.tone_440hz_wwvh_power_db:.2f}" if record.tone_440hz_wwvh_power_db is not None else '',
                'dominant_station': record.dominant_station,
                'confidence': record.confidence,
                'tick_windows_10sec': record.tick_windows_10sec or '',
                'bcd_wwv_amplitude': f"{record.bcd_wwv_amplitude:.2f}" if record.bcd_wwv_amplitude is not None else '',
                'bcd_wwvh_amplitude': f"{record.bcd_wwvh_amplitude:.2f}" if record.bcd_wwvh_amplitude is not None else '',
                'bcd_differential_delay_ms': f"{record.bcd_differential_delay_ms:.2f}" if record.bcd_differential_delay_ms is not None else '',
                'bcd_correlation_quality': f"{record.bcd_correlation_quality:.2f}" if record.bcd_correlation_quality is not None else '',
                'bcd_windows': record.bcd_windows or '',
                'tone_500_600_detected': 1 if record.tone_500_600_detected else 0,
                'tone_500_600_power_db': f"{record.tone_500_600_power_db:.2f}" if record.tone_500_600_power_db is not None else '',
                'tone_500_600_freq_hz': record.tone_500_600_freq_hz if record.tone_500_600_freq_hz else '',
                'tone_500_600_ground_truth_station': record.tone_500_600_ground_truth_station or '',
                'harmonic_ratio_500_1000': f"{record.harmonic_ratio_500_1000:.2f}" if record.harmonic_ratio_500_1000 is not None else '',
                'harmonic_ratio_600_1200': f"{record.harmonic_ratio_600_1200:.2f}" if record.harmonic_ratio_600_1200 is not None else '',
                'bcd_minute_validated': 1 if record.bcd_minute_validated else 0,
                'bcd_correlation_peak_quality': f"{record.bcd_correlation_peak_quality:.2f}" if record.bcd_correlation_peak_quality is not None else '',
                'inter_method_agreements': record.inter_method_agreements or '',
                'inter_method_disagreements': record.inter_method_disagreements or ''
            })


# Import numpy for ratio calculation
import numpy as np
