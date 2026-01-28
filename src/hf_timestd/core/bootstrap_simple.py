#!/usr/bin/env python3
"""
Simple Bootstrap: Find minute boundaries from HF time signals.

This module implements a clean, minimal bootstrap process:
1. Find recurring tone clusters (60 seconds apart)
2. Compute relative offset: "this RTP is at a minute boundary"
3. Decode BCD/FSK to identify which UTC minute
4. Output: (reference_rtp, reference_utc, uncertainty_ms)

The bootstrap runs on archived IQ data and produces a single output
that metrology uses for all subsequent processing.

Author: HF Time Standard Team
"""

import numpy as np
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

# Constants
SAMPLE_RATE = 24000
SAMPLES_PER_MINUTE = SAMPLE_RATE * 60  # 1,440,000
SAMPLES_PER_SECOND = SAMPLE_RATE

# Station info for propagation delay estimation
STATION_COORDS = {
    'WWV': (40.6781, -105.0469),   # Fort Collins, CO
    'WWVH': (21.9886, -159.7642),  # Kauai, HI
    'CHU': (45.2950, -75.7550),    # Ottawa, Canada
    'BPM': (34.9500, 109.5500),    # Pucheng, China
}

SPEED_OF_LIGHT_KM_S = 299792.458


@dataclass
class ToneCluster:
    """A cluster of tone arrivals at a minute boundary."""
    rtp_timestamp: int           # RTP at cluster center
    stations: List[str]          # Stations detected in cluster
    snr_db: float               # Best SNR in cluster
    confidence: float           # 0.0 to 1.0


@dataclass
class BootstrapResult:
    """Output of the bootstrap process."""
    reference_rtp: int          # RTP sample at UTC minute 0
    reference_utc: float        # Unix timestamp at reference_rtp
    uncertainty_ms: float       # Estimated uncertainty
    decoded_hour: int           # Hour from BCD/FSK decode
    decoded_minute: int         # Minute from BCD/FSK decode
    stations_used: List[str]    # Stations that contributed
    
    def rtp_to_utc(self, rtp: int) -> float:
        """Convert RTP timestamp to UTC."""
        return self.reference_utc + (rtp - self.reference_rtp) / SAMPLE_RATE


class SimpleBootstrap:
    """
    Simple bootstrap: find minute boundaries and decode UTC time.
    
    Usage:
        bootstrap = SimpleBootstrap(receiver_lat, receiver_lon)
        
        # Feed IQ samples from multiple channels
        for channel, samples, rtp_start in archived_data:
            bootstrap.add_samples(channel, samples, rtp_start)
        
        # Get result (or None if not enough data)
        result = bootstrap.get_result()
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        sample_rate: int = SAMPLE_RATE
    ):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.sample_rate = sample_rate
        
        # Compute expected propagation delays
        self.propagation_delays = self._compute_propagation_delays()
        
        # Accumulated data per channel
        self.channel_buffers: Dict[str, List[Tuple[np.ndarray, int]]] = {}
        
        # Found clusters
        self.clusters: List[ToneCluster] = []
        
        # Result (set when bootstrap completes)
        self._result: Optional[BootstrapResult] = None
        
        logger.info(f"SimpleBootstrap initialized at ({receiver_lat:.2f}, {receiver_lon:.2f})")
        for station, delay_ms in self.propagation_delays.items():
            logger.info(f"  {station}: {delay_ms:.1f}ms propagation delay")
    
    def _compute_propagation_delays(self) -> Dict[str, float]:
        """Compute geometric propagation delay to each station."""
        from math import radians, sin, cos, sqrt, atan2
        
        delays = {}
        for station, (lat, lon) in STATION_COORDS.items():
            # Haversine distance
            R = 6371  # Earth radius in km
            lat1, lon1 = radians(self.receiver_lat), radians(self.receiver_lon)
            lat2, lon2 = radians(lat), radians(lon)
            
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            distance_km = R * c
            
            # Ionospheric path factor (signal bounces off ionosphere)
            path_factor = 1.15
            path_km = distance_km * path_factor
            
            delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000
            delays[station] = delay_ms
        
        return delays
    
    def add_samples(
        self,
        channel: str,
        samples: np.ndarray,
        rtp_start: int
    ) -> None:
        """Add IQ samples from a channel."""
        if channel not in self.channel_buffers:
            self.channel_buffers[channel] = []
        
        self.channel_buffers[channel].append((samples, rtp_start))
    
    def find_clusters(self) -> List[ToneCluster]:
        """
        Find tone clusters in the accumulated data.
        
        A cluster is a group of tone detections from multiple stations
        that arrive within the expected propagation delay window.
        """
        from hf_timestd.core.tone_detector import MultiStationToneDetector
        
        all_detections = []
        
        for channel, buffer_list in self.channel_buffers.items():
            # Determine station from channel name
            station = self._channel_to_station(channel)
            if station is None:
                continue
            
            # Concatenate buffers for this channel
            if not buffer_list:
                continue
            
            # Sort by RTP
            buffer_list.sort(key=lambda x: x[1])
            
            # Find tones in each buffer
            for samples, rtp_start in buffer_list:
                detections = self._detect_tones_in_buffer(
                    samples, rtp_start, station, channel
                )
                all_detections.extend(detections)
        
        # Cluster detections by time
        clusters = self._cluster_detections(all_detections)
        
        return clusters
    
    def _channel_to_station(self, channel: str) -> Optional[str]:
        """Map channel name to station."""
        channel_upper = channel.upper()
        if 'CHU' in channel_upper:
            return 'CHU'
        elif 'WWVH' in channel_upper:
            return 'WWVH'
        elif 'WWV' in channel_upper:
            return 'WWV'
        elif 'BPM' in channel_upper:
            return 'BPM'
        elif 'SHARED' in channel_upper:
            # Shared channels have multiple stations - handle separately
            return None
        return None
    
    def _detect_tones_in_buffer(
        self,
        samples: np.ndarray,
        rtp_start: int,
        station: str,
        channel: str
    ) -> List[Dict]:
        """Detect minute marker tones in a buffer."""
        # Simple energy-based detection for minute markers
        # Minute markers are 800ms tones at the start of each minute
        
        detections = []
        
        # Look for high-energy segments that could be minute markers
        # This is a simplified version - production would use matched filtering
        
        window_samples = int(0.8 * self.sample_rate)  # 800ms window
        hop_samples = int(0.1 * self.sample_rate)     # 100ms hop
        
        # Compute envelope
        envelope = np.abs(samples)
        
        # Smooth with moving average
        kernel_size = int(0.05 * self.sample_rate)  # 50ms
        if kernel_size > 0:
            kernel = np.ones(kernel_size) / kernel_size
            envelope = np.convolve(envelope, kernel, mode='same')
        
        # Find peaks
        threshold = np.median(envelope) * 3
        
        for i in range(0, len(samples) - window_samples, hop_samples):
            window_energy = np.mean(envelope[i:i+window_samples])
            
            if window_energy > threshold:
                # Potential minute marker
                rtp = rtp_start + i
                snr_db = 10 * np.log10(window_energy / np.median(envelope) + 1e-10)
                
                detections.append({
                    'rtp': rtp,
                    'station': station,
                    'channel': channel,
                    'snr_db': snr_db,
                })
        
        return detections
    
    def _cluster_detections(self, detections: List[Dict]) -> List[ToneCluster]:
        """Group detections into clusters based on timing."""
        if not detections:
            return []
        
        # Sort by RTP
        detections.sort(key=lambda x: x['rtp'])
        
        clusters = []
        current_cluster = [detections[0]]
        
        # Cluster window: 100ms (accounts for propagation delay differences)
        cluster_window_samples = int(0.1 * self.sample_rate)
        
        for det in detections[1:]:
            if det['rtp'] - current_cluster[0]['rtp'] < cluster_window_samples:
                current_cluster.append(det)
            else:
                # Finalize current cluster
                if len(current_cluster) >= 2:  # Need at least 2 stations
                    clusters.append(self._make_cluster(current_cluster))
                current_cluster = [det]
        
        # Don't forget last cluster
        if len(current_cluster) >= 2:
            clusters.append(self._make_cluster(current_cluster))
        
        return clusters
    
    def _make_cluster(self, detections: List[Dict]) -> ToneCluster:
        """Create a ToneCluster from a list of detections."""
        stations = list(set(d['station'] for d in detections))
        best_snr = max(d['snr_db'] for d in detections)
        avg_rtp = int(np.mean([d['rtp'] for d in detections]))
        confidence = min(1.0, len(stations) / 3.0)  # More stations = higher confidence
        
        return ToneCluster(
            rtp_timestamp=avg_rtp,
            stations=stations,
            snr_db=best_snr,
            confidence=confidence
        )
    
    def find_recurring_clusters(self) -> Optional[Tuple[ToneCluster, ToneCluster]]:
        """
        Find two clusters that are exactly 60 seconds apart.
        
        Returns the pair of clusters if found, None otherwise.
        """
        clusters = self.find_clusters()
        
        if len(clusters) < 2:
            logger.debug(f"Only {len(clusters)} clusters found, need at least 2")
            return None
        
        # Look for pairs that are 60 seconds apart (within 50ms tolerance)
        tolerance_samples = int(0.05 * self.sample_rate)  # 50ms
        
        for i, c1 in enumerate(clusters):
            for c2 in clusters[i+1:]:
                diff = abs(c2.rtp_timestamp - c1.rtp_timestamp)
                
                # Check if difference is close to N minutes
                minutes_apart = round(diff / SAMPLES_PER_MINUTE)
                if minutes_apart == 0:
                    continue
                
                expected_diff = minutes_apart * SAMPLES_PER_MINUTE
                error = abs(diff - expected_diff)
                
                if error < tolerance_samples:
                    logger.info(f"Found recurring clusters {minutes_apart} minutes apart "
                               f"(error={error * 1000 / self.sample_rate:.1f}ms)")
                    
                    # Return earlier cluster first
                    if c1.rtp_timestamp < c2.rtp_timestamp:
                        return (c1, c2)
                    else:
                        return (c2, c1)
        
        logger.debug("No recurring clusters found")
        return None
    
    def decode_time(self, cluster: ToneCluster) -> Optional[Tuple[int, int]]:
        """
        Decode BCD/FSK to get hour and minute.
        
        Returns (hour, minute) if successful, None otherwise.
        """
        # Get samples around the cluster for BCD/FSK decoding
        # We need the full minute of data
        
        for channel, buffer_list in self.channel_buffers.items():
            station = self._channel_to_station(channel)
            if station not in ['WWV', 'WWVH', 'CHU']:
                continue
            
            # Find buffer containing this cluster
            for samples, rtp_start in buffer_list:
                rtp_end = rtp_start + len(samples)
                
                if rtp_start <= cluster.rtp_timestamp < rtp_end:
                    # Found the buffer - try to decode
                    offset = cluster.rtp_timestamp - rtp_start
                    
                    if station in ['WWV', 'WWVH']:
                        result = self._decode_wwv_bcd(samples, offset)
                    else:  # CHU
                        result = self._decode_chu_fsk(samples, offset)
                    
                    if result:
                        return result
        
        return None
    
    def _decode_wwv_bcd(
        self,
        samples: np.ndarray,
        minute_start_offset: int
    ) -> Optional[Tuple[int, int]]:
        """Decode WWV/WWVH BCD time code."""
        try:
            from hf_timestd.core.wwv_bcd_decoder import WWVBCDDecoder
            
            decoder = WWVBCDDecoder(sample_rate=self.sample_rate)
            
            # Extract one minute of samples starting at minute boundary
            minute_samples = samples[minute_start_offset:minute_start_offset + SAMPLES_PER_MINUTE]
            
            if len(minute_samples) < SAMPLES_PER_MINUTE:
                return None
            
            result = decoder.decode(minute_samples)
            
            if result and result.valid:
                logger.info(f"WWV BCD decode: {result.hour:02d}:{result.minute:02d}")
                return (result.hour, result.minute)
            
        except Exception as e:
            logger.debug(f"WWV BCD decode failed: {e}")
        
        return None
    
    def _decode_chu_fsk(
        self,
        samples: np.ndarray,
        minute_start_offset: int
    ) -> Optional[Tuple[int, int]]:
        """Decode CHU FSK time code."""
        try:
            from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder
            
            decoder = CHUFSKDecoder(sample_rate=self.sample_rate)
            
            # Extract one minute of samples starting at minute boundary
            minute_samples = samples[minute_start_offset:minute_start_offset + SAMPLES_PER_MINUTE]
            
            if len(minute_samples) < SAMPLES_PER_MINUTE:
                return None
            
            result = decoder.decode(minute_samples)
            
            if result and result.valid:
                logger.info(f"CHU FSK decode: {result.hour:02d}:{result.minute:02d}")
                return (result.hour, result.minute)
            
        except Exception as e:
            logger.debug(f"CHU FSK decode failed: {e}")
        
        return None
    
    def compute_result(self) -> Optional[BootstrapResult]:
        """
        Run the full bootstrap process and return the result.
        
        Returns BootstrapResult if successful, None otherwise.
        """
        # Step 1: Find recurring clusters
        cluster_pair = self.find_recurring_clusters()
        if cluster_pair is None:
            logger.info("Bootstrap: waiting for recurring clusters")
            return None
        
        cluster1, cluster2 = cluster_pair
        
        # Step 2: Decode BCD/FSK to get UTC time
        decoded_time = self.decode_time(cluster1)
        if decoded_time is None:
            decoded_time = self.decode_time(cluster2)
        
        if decoded_time is None:
            logger.info("Bootstrap: waiting for BCD/FSK decode")
            return None
        
        decoded_hour, decoded_minute = decoded_time
        
        # Step 3: Compute reference_rtp (RTP at UTC minute 0)
        # cluster1 is at minute N, so minute 0 is cluster1.rtp - N * SAMPLES_PER_MINUTE
        
        # First, adjust for propagation delay
        # The tone arrives delay_ms after transmission
        best_station = cluster1.stations[0]
        delay_ms = self.propagation_delays.get(best_station, 0)
        delay_samples = int(delay_ms * self.sample_rate / 1000)
        
        # RTP at minute boundary = tone arrival - propagation delay
        minute_boundary_rtp = cluster1.rtp_timestamp - delay_samples
        
        # Compute which minute this is (from decoded time)
        minutes_since_midnight = decoded_hour * 60 + decoded_minute
        
        # Reference RTP = minute boundary RTP - (minutes * samples_per_minute)
        reference_rtp = minute_boundary_rtp - (minutes_since_midnight * SAMPLES_PER_MINUTE)
        
        # Reference UTC = midnight today
        now = time.time()
        midnight_today = (int(now) // 86400) * 86400
        reference_utc = float(midnight_today)
        
        # Uncertainty estimate
        uncertainty_ms = 5.0  # Conservative estimate
        
        result = BootstrapResult(
            reference_rtp=reference_rtp,
            reference_utc=reference_utc,
            uncertainty_ms=uncertainty_ms,
            decoded_hour=decoded_hour,
            decoded_minute=decoded_minute,
            stations_used=cluster1.stations + cluster2.stations
        )
        
        logger.info(f"Bootstrap complete: {decoded_hour:02d}:{decoded_minute:02d} UTC")
        logger.info(f"  reference_rtp={reference_rtp}, reference_utc={reference_utc}")
        logger.info(f"  stations: {result.stations_used}")
        
        self._result = result
        return result
    
    def get_result(self) -> Optional[BootstrapResult]:
        """Get the bootstrap result, computing if necessary."""
        if self._result is None:
            self._result = self.compute_result()
        return self._result


def run_bootstrap_on_archive(
    archive_dir: Path,
    receiver_lat: float,
    receiver_lon: float,
    max_minutes: int = 5
) -> Optional[BootstrapResult]:
    """
    Run bootstrap on archived IQ data.
    
    Args:
        archive_dir: Directory containing archived .bin files
        receiver_lat: Receiver latitude
        receiver_lon: Receiver longitude
        max_minutes: Maximum minutes of data to process
        
    Returns:
        BootstrapResult if successful, None otherwise
    """
    bootstrap = SimpleBootstrap(receiver_lat, receiver_lon)
    
    # Find and load archived data
    bin_files = sorted(archive_dir.glob("**/*.bin"))[:max_minutes]
    
    for bin_file in bin_files:
        # Parse channel from path
        channel = bin_file.parent.name
        
        # Load samples (assuming complex64 format)
        try:
            samples = np.fromfile(bin_file, dtype=np.complex64)
            
            # Get RTP from metadata (simplified - would read from .json)
            # For now, use filename as proxy
            rtp_start = 0  # Would be read from metadata
            
            bootstrap.add_samples(channel, samples, rtp_start)
            
        except Exception as e:
            logger.warning(f"Failed to load {bin_file}: {e}")
    
    return bootstrap.get_result()
