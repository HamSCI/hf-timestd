#!/usr/bin/env python3
"""
Sliding Window Monitor - Real-Time Signal Quality Tracking

This module provides 10-second sliding window analysis for real-time monitoring,
running in parallel with the 60-second D_clock computation buffer.

Purpose:
========
- Track signal quality metrics with low latency (~10 seconds)
- Detect degradation before full-minute analysis completes
- Provide real-time feedback for web UI
- Evaluate whether sub-minute monitoring adds value

Metrics Tracked:
================
1. **SNR Trending**: Signal-to-noise ratio for 1000/1200 Hz tones
2. **Doppler Trending**: Frequency offset stability
3. **Gap Detection**: RTP packet loss events
4. **Signal Presence**: Is the expected carrier detectable?

Output:
=======
- JSON status file updated every 10 seconds
- Metrics accumulated for per-minute summary
- Can be disabled if monitoring overhead > benefit

Usage:
------
    monitor = SlidingWindowMonitor(
        channel_name='WWV 10 MHz',
        sample_rate=20000,
        output_dir=Path('/tmp/timestd-test/status')
    )
    
    # Feed 10-second chunks
    for chunk in ten_second_chunks:
        metrics = monitor.process_chunk(chunk, timestamp)
        print(f"Current SNR: {metrics.snr_db:.1f} dB")
"""

import numpy as np
import logging
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any, Deque
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
import threading

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

WINDOW_DURATION_SEC = 10.0      # Sliding window size
SAMPLE_RATE = 20000             # Expected sample rate
SAMPLES_PER_WINDOW = int(WINDOW_DURATION_SEC * SAMPLE_RATE)

# Tone frequencies for detection
WWV_TONE_HZ = 1000.0
WWVH_TONE_HZ = 1200.0
CHU_TONE_HZ = 1000.0

# SNR thresholds
SNR_EXCELLENT_DB = 20.0
SNR_GOOD_DB = 10.0
SNR_MARGINAL_DB = 5.0
SNR_POOR_DB = 0.0

# Doppler stability thresholds
DOPPLER_STABLE_HZ = 0.1         # < 0.1 Hz = very stable
DOPPLER_MODERATE_HZ = 0.5       # < 0.5 Hz = normal
DOPPLER_UNSTABLE_HZ = 2.0       # > 2 Hz = disturbed


class SignalQuality(Enum):
    """Overall signal quality classification."""
    EXCELLENT = 'excellent'     # SNR > 20 dB, stable Doppler
    GOOD = 'good'               # SNR > 10 dB
    MARGINAL = 'marginal'       # SNR > 5 dB
    POOR = 'poor'               # SNR > 0 dB
    NO_SIGNAL = 'no_signal'     # SNR < 0 dB or no detection


@dataclass
class WindowMetrics:
    """Metrics from a single 10-second window."""
    timestamp: float                    # Unix timestamp of window start
    window_number: int                  # Sequential window number
    
    # SNR metrics
    wwv_snr_db: Optional[float] = None
    wwvh_snr_db: Optional[float] = None
    dominant_snr_db: Optional[float] = None
    
    # Doppler metrics
    wwv_doppler_hz: Optional[float] = None
    wwvh_doppler_hz: Optional[float] = None
    doppler_stability_hz: Optional[float] = None  # Std dev over window
    
    # Signal presence
    wwv_detected: bool = False
    wwvh_detected: bool = False
    signal_present: bool = False
    
    # Data quality
    samples_received: int = 0
    samples_expected: int = SAMPLES_PER_WINDOW
    gap_count: int = 0
    gap_samples: int = 0
    completeness_pct: float = 100.0
    
    # Derived
    quality: SignalQuality = SignalQuality.NO_SIGNAL
    
    def to_dict(self) -> Dict[str, Any]:
        # Helper to convert numpy types to Python native types for JSON
        def to_native(v):
            if v is None:
                return None
            if hasattr(v, 'item'):  # numpy scalar
                return v.item()
            return v
        
        return {
            'timestamp': to_native(self.timestamp),
            'timestamp_iso': datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            'window_number': self.window_number,
            'wwv_snr_db': to_native(self.wwv_snr_db),
            'wwvh_snr_db': to_native(self.wwvh_snr_db),
            'dominant_snr_db': to_native(self.dominant_snr_db),
            'wwv_doppler_hz': to_native(self.wwv_doppler_hz),
            'wwvh_doppler_hz': to_native(self.wwvh_doppler_hz),
            'doppler_stability_hz': to_native(self.doppler_stability_hz),
            'wwv_detected': bool(self.wwv_detected),
            'wwvh_detected': bool(self.wwvh_detected),
            'signal_present': bool(self.signal_present),
            'samples_received': self.samples_received,
            'completeness_pct': to_native(self.completeness_pct),
            'gap_count': self.gap_count,
            'quality': self.quality.value
        }


@dataclass
class MinuteSummary:
    """Accumulated metrics over 6 windows (1 minute)."""
    minute_boundary: float
    window_count: int = 0
    
    # SNR statistics
    snr_mean_db: Optional[float] = None
    snr_min_db: Optional[float] = None
    snr_max_db: Optional[float] = None
    snr_std_db: Optional[float] = None
    
    # Doppler statistics
    doppler_mean_hz: Optional[float] = None
    doppler_std_hz: Optional[float] = None
    doppler_trend_hz_per_min: Optional[float] = None
    
    # Detection statistics
    wwv_detection_rate: float = 0.0
    wwvh_detection_rate: float = 0.0
    signal_presence_rate: float = 0.0
    
    # Data quality
    overall_completeness_pct: float = 100.0
    total_gaps: int = 0
    
    # Quality distribution
    quality_distribution: Dict[str, int] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'minute_boundary': self.minute_boundary,
            'minute_boundary_iso': datetime.fromtimestamp(self.minute_boundary, tz=timezone.utc).isoformat(),
            'window_count': self.window_count,
            'snr_mean_db': self.snr_mean_db,
            'snr_min_db': self.snr_min_db,
            'snr_max_db': self.snr_max_db,
            'snr_std_db': self.snr_std_db,
            'doppler_mean_hz': self.doppler_mean_hz,
            'doppler_std_hz': self.doppler_std_hz,
            'doppler_trend_hz_per_min': self.doppler_trend_hz_per_min,
            'wwv_detection_rate': self.wwv_detection_rate,
            'wwvh_detection_rate': self.wwvh_detection_rate,
            'signal_presence_rate': self.signal_presence_rate,
            'overall_completeness_pct': self.overall_completeness_pct,
            'total_gaps': self.total_gaps,
            'quality_distribution': self.quality_distribution
        }


class SlidingWindowMonitor:
    """
    Real-time signal quality monitor using 10-second sliding windows.
    
    Provides low-latency metrics for monitoring while the main 60-second
    D_clock computation runs in parallel.
    """
    
    def __init__(
        self,
        channel_name: str,
        sample_rate: int = SAMPLE_RATE,
        output_dir: Optional[Path] = None,
        history_size: int = 60,  # Keep last 60 windows (10 minutes)
        enabled: bool = True
    ):
        """
        Initialize sliding window monitor.
        
        Args:
            channel_name: Channel identifier
            sample_rate: Input sample rate (Hz)
            output_dir: Directory for status JSON output
            history_size: Number of windows to keep in history
            enabled: Whether monitoring is active
        """
        self.channel_name = channel_name
        self.sample_rate = sample_rate
        self.output_dir = Path(output_dir) if output_dir else None
        self.history_size = history_size
        self.enabled = enabled
        
        # State
        self.window_number = 0
        self.window_history: Deque[WindowMetrics] = deque(maxlen=history_size)
        self.current_minute_windows: List[WindowMetrics] = []
        self.current_minute_boundary: Optional[float] = None
        
        # Minute summaries
        self.minute_summaries: Deque[MinuteSummary] = deque(maxlen=60)  # Last hour
        
        # Statistics for monitoring value assessment
        self.total_windows_processed = 0
        self.anomalies_detected = 0  # Cases where 10s monitoring caught issue before 60s
        self.start_time = time.time()
        
        # Thread safety
        self._lock = threading.Lock()
        
        # Create output directory
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"SlidingWindowMonitor initialized for {channel_name}")
        logger.info(f"  Window duration: {WINDOW_DURATION_SEC}s")
        logger.info(f"  History size: {history_size} windows")
        logger.info(f"  Enabled: {enabled}")
    
    def process_chunk(
        self,
        samples: np.ndarray,
        timestamp: float,
        gap_info: Optional[Dict] = None
    ) -> Optional[WindowMetrics]:
        """
        Process a 10-second chunk of samples.
        
        Args:
            samples: Complex IQ samples (should be ~200,000 at 20kHz)
            timestamp: Unix timestamp of chunk start
            gap_info: Optional dict with gap_count and gap_samples
            
        Returns:
            WindowMetrics for this chunk, or None if disabled
        """
        if not self.enabled:
            return None
        
        with self._lock:
            self.window_number += 1
            self.total_windows_processed += 1
            
            # Create metrics object
            metrics = WindowMetrics(
                timestamp=timestamp,
                window_number=self.window_number,
                samples_received=len(samples),
                samples_expected=int(WINDOW_DURATION_SEC * self.sample_rate)
            )
            
            # Calculate completeness
            if metrics.samples_expected > 0:
                metrics.completeness_pct = (metrics.samples_received / metrics.samples_expected) * 100
            
            # Apply gap info if provided
            if gap_info:
                metrics.gap_count = gap_info.get('gap_count', 0)
                metrics.gap_samples = gap_info.get('gap_samples', 0)
            
            # Analyze signal
            if len(samples) > 0:
                self._analyze_signal(samples, metrics)
            
            # Classify quality
            metrics.quality = self._classify_quality(metrics)
            
            # Add to history
            self.window_history.append(metrics)
            
            # Accumulate for minute summary
            self._accumulate_for_minute(metrics)
            
            # Write status file
            self._write_status(metrics)
            
            return metrics
    
    def _analyze_signal(self, samples: np.ndarray, metrics: WindowMetrics):
        """Analyze signal for SNR and Doppler."""
        try:
            # Ensure complex64
            if samples.dtype != np.complex64:
                samples = samples.astype(np.complex64)
            
            # FFT for spectral analysis
            n_fft = min(len(samples), 8192)
            fft_result = np.fft.fft(samples[:n_fft])
            power_spectrum = np.abs(fft_result) ** 2
            freqs = np.fft.fftfreq(n_fft, 1.0 / self.sample_rate)
            
            # Only look at positive frequencies
            pos_mask = freqs >= 0
            freqs = freqs[pos_mask]
            power_spectrum = power_spectrum[pos_mask]
            
            # Estimate noise floor (median of spectrum, excluding DC)
            noise_floor = np.median(power_spectrum[10:])  # Skip DC region
            
            # Find WWV tone (1000 Hz)
            wwv_snr, wwv_doppler = self._find_tone(
                freqs, power_spectrum, noise_floor, WWV_TONE_HZ, search_width=50
            )
            if wwv_snr is not None:
                metrics.wwv_snr_db = wwv_snr
                metrics.wwv_doppler_hz = wwv_doppler
                metrics.wwv_detected = wwv_snr > SNR_POOR_DB
            
            # Find WWVH tone (1200 Hz)
            wwvh_snr, wwvh_doppler = self._find_tone(
                freqs, power_spectrum, noise_floor, WWVH_TONE_HZ, search_width=50
            )
            if wwvh_snr is not None:
                metrics.wwvh_snr_db = wwvh_snr
                metrics.wwvh_doppler_hz = wwvh_doppler
                metrics.wwvh_detected = wwvh_snr > SNR_POOR_DB
            
            # Determine dominant signal
            metrics.signal_present = metrics.wwv_detected or metrics.wwvh_detected
            
            if metrics.wwv_snr_db is not None and metrics.wwvh_snr_db is not None:
                metrics.dominant_snr_db = max(metrics.wwv_snr_db, metrics.wwvh_snr_db)
            elif metrics.wwv_snr_db is not None:
                metrics.dominant_snr_db = metrics.wwv_snr_db
            elif metrics.wwvh_snr_db is not None:
                metrics.dominant_snr_db = metrics.wwvh_snr_db
            
            # Doppler stability (if we have history)
            if len(self.window_history) >= 2:
                recent_doppler = []
                for w in list(self.window_history)[-6:]:  # Last minute
                    if w.wwv_doppler_hz is not None:
                        recent_doppler.append(w.wwv_doppler_hz)
                    elif w.wwvh_doppler_hz is not None:
                        recent_doppler.append(w.wwvh_doppler_hz)
                
                if len(recent_doppler) >= 2:
                    metrics.doppler_stability_hz = float(np.std(recent_doppler))
            
        except Exception as e:
            logger.warning(f"Signal analysis error: {e}")
    
    def _find_tone(
        self,
        freqs: np.ndarray,
        power: np.ndarray,
        noise_floor: float,
        target_freq: float,
        search_width: float = 50
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Find a tone near the target frequency.
        
        Returns:
            Tuple of (snr_db, doppler_offset_hz) or (None, None) if not found
        """
        # Find frequency bins in search range
        mask = (freqs >= target_freq - search_width) & (freqs <= target_freq + search_width)
        if not np.any(mask):
            return None, None
        
        search_freqs = freqs[mask]
        search_power = power[mask]
        
        # Find peak
        peak_idx = np.argmax(search_power)
        peak_power = search_power[peak_idx]
        peak_freq = search_freqs[peak_idx]
        
        # Calculate SNR
        if noise_floor > 0:
            snr_linear = peak_power / noise_floor
            snr_db = 10 * np.log10(max(snr_linear, 1e-10))
        else:
            snr_db = 0.0
        
        # Doppler offset
        doppler_hz = peak_freq - target_freq
        
        return snr_db, doppler_hz
    
    def _classify_quality(self, metrics: WindowMetrics) -> SignalQuality:
        """Classify overall signal quality."""
        snr = metrics.dominant_snr_db
        
        if snr is None or not metrics.signal_present:
            return SignalQuality.NO_SIGNAL
        
        if snr >= SNR_EXCELLENT_DB:
            # Check Doppler stability for "excellent"
            if metrics.doppler_stability_hz is not None and metrics.doppler_stability_hz < DOPPLER_STABLE_HZ:
                return SignalQuality.EXCELLENT
            return SignalQuality.GOOD
        elif snr >= SNR_GOOD_DB:
            return SignalQuality.GOOD
        elif snr >= SNR_MARGINAL_DB:
            return SignalQuality.MARGINAL
        elif snr >= SNR_POOR_DB:
            return SignalQuality.POOR
        else:
            return SignalQuality.NO_SIGNAL
    
    def _accumulate_for_minute(self, metrics: WindowMetrics):
        """Accumulate window metrics for minute summary."""
        # Determine minute boundary
        minute_boundary = int(metrics.timestamp / 60) * 60
        
        # Check if we've crossed into a new minute
        if self.current_minute_boundary is not None and minute_boundary != self.current_minute_boundary:
            # Finalize previous minute
            self._finalize_minute_summary()
            self.current_minute_windows = []
        
        self.current_minute_boundary = minute_boundary
        self.current_minute_windows.append(metrics)
    
    def _finalize_minute_summary(self):
        """Create summary for the completed minute."""
        if not self.current_minute_windows:
            return
        
        windows = self.current_minute_windows
        summary = MinuteSummary(
            minute_boundary=self.current_minute_boundary,
            window_count=len(windows)
        )
        
        # SNR statistics
        snr_values = [w.dominant_snr_db for w in windows if w.dominant_snr_db is not None]
        if snr_values:
            summary.snr_mean_db = float(np.mean(snr_values))
            summary.snr_min_db = float(np.min(snr_values))
            summary.snr_max_db = float(np.max(snr_values))
            summary.snr_std_db = float(np.std(snr_values))
        
        # Doppler statistics
        doppler_values = []
        for w in windows:
            if w.wwv_doppler_hz is not None:
                doppler_values.append(w.wwv_doppler_hz)
            elif w.wwvh_doppler_hz is not None:
                doppler_values.append(w.wwvh_doppler_hz)
        
        if doppler_values:
            summary.doppler_mean_hz = float(np.mean(doppler_values))
            summary.doppler_std_hz = float(np.std(doppler_values))
            
            # Trend (linear fit)
            if len(doppler_values) >= 3:
                x = np.arange(len(doppler_values))
                coeffs = np.polyfit(x, doppler_values, 1)
                # Convert slope to Hz/minute (6 windows per minute)
                summary.doppler_trend_hz_per_min = float(coeffs[0] * 6)
        
        # Detection rates
        summary.wwv_detection_rate = sum(1 for w in windows if w.wwv_detected) / len(windows)
        summary.wwvh_detection_rate = sum(1 for w in windows if w.wwvh_detected) / len(windows)
        summary.signal_presence_rate = sum(1 for w in windows if w.signal_present) / len(windows)
        
        # Data quality
        summary.overall_completeness_pct = float(np.mean([w.completeness_pct for w in windows]))
        summary.total_gaps = sum(w.gap_count for w in windows)
        
        # Quality distribution
        for q in SignalQuality:
            summary.quality_distribution[q.value] = sum(1 for w in windows if w.quality == q)
        
        self.minute_summaries.append(summary)
        
        logger.debug(
            f"Minute summary: SNR={summary.snr_mean_db:.1f}±{summary.snr_std_db:.1f} dB, "
            f"Doppler={summary.doppler_mean_hz:+.2f} Hz, "
            f"presence={summary.signal_presence_rate*100:.0f}%"
        )
    
    def _write_status(self, metrics: WindowMetrics):
        """Write current status to JSON file."""
        if self.output_dir is None:
            return
        
        try:
            status = {
                'channel_name': self.channel_name,
                'monitor_type': 'sliding_window_10s',
                'updated_at': datetime.now(tz=timezone.utc).isoformat(),
                'enabled': self.enabled,
                'current_window': metrics.to_dict(),
                'statistics': {
                    'total_windows': self.total_windows_processed,
                    'uptime_seconds': time.time() - self.start_time,
                    'anomalies_detected': self.anomalies_detected
                }
            }
            
            # Add recent history summary
            if len(self.window_history) >= 6:
                recent = list(self.window_history)[-6:]  # Last minute
                recent_snr = [w.dominant_snr_db for w in recent if w.dominant_snr_db is not None]
                status['recent_minute'] = {
                    'snr_mean_db': float(np.mean(recent_snr)) if recent_snr else None,
                    'snr_trend': self._calculate_snr_trend(recent),
                    'signal_presence_rate': sum(1 for w in recent if w.signal_present) / len(recent)
                }
            
            # Write atomically
            status_file = self.output_dir / f'{self.channel_name.replace(" ", "_")}_monitor.json'
            temp_file = status_file.with_suffix('.tmp')
            
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            
            temp_file.rename(status_file)
            
        except Exception as e:
            logger.warning(f"Failed to write monitor status: {e}")
    
    def _calculate_snr_trend(self, windows: List[WindowMetrics]) -> str:
        """Calculate SNR trend from recent windows."""
        snr_values = [w.dominant_snr_db for w in windows if w.dominant_snr_db is not None]
        if len(snr_values) < 3:
            return 'unknown'
        
        # Simple linear regression
        x = np.arange(len(snr_values))
        coeffs = np.polyfit(x, snr_values, 1)
        slope = coeffs[0]
        
        if slope > 1.0:
            return 'improving'
        elif slope < -1.0:
            return 'degrading'
        else:
            return 'stable'
    
    def get_current_metrics(self) -> Optional[WindowMetrics]:
        """Get the most recent window metrics."""
        with self._lock:
            if self.window_history:
                return self.window_history[-1]
            return None
    
    def get_minute_summary(self) -> Optional[MinuteSummary]:
        """Get the most recent minute summary."""
        with self._lock:
            if self.minute_summaries:
                return self.minute_summaries[-1]
            return None
    
    def get_monitoring_value_assessment(self) -> Dict[str, Any]:
        """
        Assess whether 10-second monitoring is providing value.
        
        Returns metrics to help decide if this should be kept or removed.
        """
        with self._lock:
            uptime = time.time() - self.start_time
            
            return {
                'uptime_hours': uptime / 3600,
                'total_windows': self.total_windows_processed,
                'anomalies_detected': self.anomalies_detected,
                'anomaly_rate': self.anomalies_detected / max(self.total_windows_processed, 1),
                'memory_windows': len(self.window_history),
                'memory_summaries': len(self.minute_summaries),
                'recommendation': self._make_recommendation()
            }
    
    def _make_recommendation(self) -> str:
        """Make a recommendation about monitoring value."""
        if self.total_windows_processed < 360:  # Less than 1 hour
            return 'insufficient_data'
        
        anomaly_rate = self.anomalies_detected / self.total_windows_processed
        
        if anomaly_rate > 0.01:  # > 1% anomalies caught early
            return 'keep_valuable'
        elif anomaly_rate > 0.001:  # > 0.1% anomalies
            return 'keep_marginal_value'
        else:
            return 'consider_removing'
    
    def record_anomaly(self, description: str):
        """Record an anomaly detected by 10s monitoring before 60s analysis."""
        with self._lock:
            self.anomalies_detected += 1
            logger.info(f"Anomaly detected by 10s monitor: {description}")
    
    def disable(self):
        """Disable monitoring to save resources."""
        self.enabled = False
        logger.info(f"SlidingWindowMonitor disabled for {self.channel_name}")
    
    def enable(self):
        """Re-enable monitoring."""
        self.enabled = True
        logger.info(f"SlidingWindowMonitor enabled for {self.channel_name}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get monitor statistics."""
        with self._lock:
            return {
                'channel_name': self.channel_name,
                'enabled': self.enabled,
                'total_windows': self.total_windows_processed,
                'anomalies_detected': self.anomalies_detected,
                'uptime_seconds': time.time() - self.start_time,
                'history_size': len(self.window_history),
                'minute_summaries': len(self.minute_summaries)
            }
