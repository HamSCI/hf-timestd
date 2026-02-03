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
        rtp_start: int,
        system_time: Optional[float] = None
    ) -> None:
        """Add samples from a channel buffer.
        
        Args:
            channel: Channel name
            samples: IQ samples
            rtp_start: RTP timestamp at start of buffer
            system_time: System time (Unix timestamp) at start of buffer
        """
        if channel not in self.channel_buffers:
            self.channel_buffers[channel] = []
        
        self.channel_buffers[channel].append((samples, rtp_start))
        
        # Store metadata for time estimation
        if system_time is not None:
            if not hasattr(self, 'buffer_metadata'):
                self.buffer_metadata = {}
            self.buffer_metadata[(channel, rtp_start)] = system_time
    
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
        """Detect per-second ticks in a buffer using FFT-based matched filtering.
        
        We detect all ticks (not just minute markers) and later use the
        60-second recurrence pattern to identify minute boundaries.
        """
        from scipy.signal import find_peaks, fftconvolve
        from scipy.signal.windows import tukey
        
        detections = []
        
        try:
            # Determine tone parameters based on channel
            channel_upper = channel.upper()
            if 'CHU' in channel_upper:
                tone_freq = 1000  # Hz
                station_name = 'CHU'
            else:
                tone_freq = 1000  # Hz (WWV also uses 1000Hz)
                station_name = 'WWV'
            
            # Use SHORT template (100ms) to detect tick onsets
            # This detects all ticks, not just minute markers
            tone_duration = 0.1
            
            n_template = int(tone_duration * self.sample_rate)
            t = np.arange(n_template) / self.sample_rate
            window = tukey(n_template, alpha=0.1)
            
            template_sin = np.sin(2 * np.pi * tone_freq * t) * window
            template_cos = np.cos(2 * np.pi * tone_freq * t) * window
            template_sin /= np.linalg.norm(template_sin)
            template_cos /= np.linalg.norm(template_cos)
            
            # FFT-based correlation
            corr_sin = fftconvolve(samples, template_sin[::-1], mode='same')
            corr_cos = fftconvolve(samples, template_cos[::-1], mode='same')
            corr_mag = np.sqrt(np.abs(corr_sin)**2 + np.abs(corr_cos)**2)
            
            noise_floor = np.median(corr_mag)
            threshold = noise_floor * 3
            
            # Find peaks ~1 second apart (per-second ticks)
            min_distance = int(0.8 * self.sample_rate)
            peaks, _ = find_peaks(corr_mag, height=threshold, distance=min_distance)
            
            for peak_idx in peaks:
                peak_val = corr_mag[peak_idx]
                snr_db = 10 * np.log10(peak_val / noise_floor + 1e-10)
                
                if snr_db > 6:
                    rtp = rtp_start + peak_idx
                    detections.append({
                        'rtp': rtp,
                        'station': station_name,
                        'channel': channel,
                        'snr_db': snr_db,
                    })
                        
        except Exception as e:
            logger.warning(f"Tone detection failed for {channel}: {e}")
        
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
        Find minute boundaries using multi-channel sliding template approach.
        
        This method:
        1. Detects all ticks across all channels
        2. Adjusts for propagation delay to get transmission time
        3. Slides through looking for positions where multiple channels
           have detections within tolerance
        4. Finds pairs of such positions 60 seconds apart
        
        Returns the pair of clusters if found, None otherwise.
        """
        # Get all detections with propagation delay adjustment
        all_detections = []
        for channel, buffer_list in self.channel_buffers.items():
            station = self._channel_to_station(channel)
            if station is None:
                continue
            
            delay = self.propagation_delays.get(station, 0)
            
            for samples, rtp_start in buffer_list:
                detections = self._detect_tones_in_buffer(samples, rtp_start, station, channel)
                for det in detections:
                    det['delay'] = delay
                    det['tx_rtp'] = det['rtp'] - delay  # Transmission time
                all_detections.extend(detections)
        
        if len(all_detections) < 2:
            logger.debug(f"Only {len(all_detections)} detections found")
            return None
        
        logger.info(f"Multi-channel search: {len(all_detections)} ticks across {len(self.channel_buffers)} channels")
        
        # Sort by tx_rtp (transmission time)
        all_detections.sort(key=lambda x: x['tx_rtp'])
        tx_rtps = np.array([d['tx_rtp'] for d in all_detections])
        channels = np.array([d['channel'] for d in all_detections])
        snrs = np.array([d['snr_db'] for d in all_detections])
        
        min_tx = int(tx_rtps.min())
        max_tx = int(tx_rtps.max())
        
        # Coarse search: 1-second steps
        CLUSTER_TOLERANCE = int(0.1 * self.sample_rate)  # 100ms
        step = self.sample_rate
        
        coarse_scores = []
        for pos in range(min_tx, max_tx - SAMPLES_PER_MINUTE, step):
            mask = np.abs(tx_rtps - pos) < CLUSTER_TOLERANCE
            unique_channels = len(set(channels[mask]))
            total_snr = snrs[mask].sum() if mask.any() else 0
            coarse_scores.append((pos, unique_channels, total_snr))
        
        # Find best 60s pairs
        score_dict = {pos: (ch, snr) for pos, ch, snr in coarse_scores}
        
        best_pair_score = 0
        best_pair = None
        
        for pos, ch_count, snr_sum in coarse_scores:
            pos2 = pos + SAMPLES_PER_MINUTE
            if pos2 in score_dict:
                ch2, snr2 = score_dict[pos2]
                combined_ch = ch_count + ch2
                
                if combined_ch > best_pair_score:
                    best_pair_score = combined_ch
                    best_pair = (pos, pos2)
        
        if best_pair is None or best_pair_score < 2:
            logger.debug(f"No good 60s pairs found (best score: {best_pair_score})")
            return None
        
        pos1, pos2 = best_pair
        
        # Fine search around best positions (±0.5s at 10ms steps)
        fine_step = int(0.01 * self.sample_rate)
        fine_range = int(0.5 * self.sample_rate)
        
        best_fine_score = 0
        best_fine_pos = pos1
        
        for offset in range(-fine_range, fine_range, fine_step):
            test_pos = pos1 + offset
            mask = np.abs(tx_rtps - test_pos) < CLUSTER_TOLERANCE
            unique_channels = len(set(channels[mask]))
            
            mask2 = np.abs(tx_rtps - (test_pos + SAMPLES_PER_MINUTE)) < CLUSTER_TOLERANCE
            unique_channels2 = len(set(channels[mask2]))
            
            combined = unique_channels + unique_channels2
            if combined > best_fine_score:
                best_fine_score = combined
                best_fine_pos = test_pos
        
        logger.info(f"Found minute boundary at tx_rtp={best_fine_pos} (score={best_fine_score})")
        
        # Get detections at each minute boundary
        mask1 = np.abs(tx_rtps - best_fine_pos) < CLUSTER_TOLERANCE
        mask2 = np.abs(tx_rtps - (best_fine_pos + SAMPLES_PER_MINUTE)) < CLUSTER_TOLERANCE
        
        dets1 = [all_detections[i] for i in np.where(mask1)[0]]
        dets2 = [all_detections[i] for i in np.where(mask2)[0]]
        
        if not dets1 or not dets2:
            return None
        
        # Use strongest detection to anchor
        anchor1 = max(dets1, key=lambda x: x['snr_db'])
        anchor2 = max(dets2, key=lambda x: x['snr_db'])
        
        # Compute minute boundary RTP from anchor
        minute_rtp1 = anchor1['rtp'] - (anchor1['tx_rtp'] - best_fine_pos)
        minute_rtp2 = minute_rtp1 + SAMPLES_PER_MINUTE
        
        logger.info(f"  Minute 1: RTP={minute_rtp1}, anchor={anchor1['channel']} SNR={anchor1['snr_db']:.1f}dB")
        logger.info(f"  Minute 2: RTP={minute_rtp2}, anchor={anchor2['channel']} SNR={anchor2['snr_db']:.1f}dB")
        
        c1 = ToneCluster(
            rtp_timestamp=int(minute_rtp1),
            stations=[d['station'] for d in dets1],
            snr_db=anchor1['snr_db'],
            confidence=best_fine_score / (2 * len(self.channel_buffers))
        )
        c2 = ToneCluster(
            rtp_timestamp=int(minute_rtp2),
            stations=[d['station'] for d in dets2],
            snr_db=anchor2['snr_db'],
            confidence=best_fine_score / (2 * len(self.channel_buffers))
        )
        
        return (c1, c2)
    
    def decode_time(self, cluster: ToneCluster) -> Optional[Tuple[int, int, int]]:
        """
        Decode BCD/FSK to get hour and minute using the KNOWN minute boundary.
        
        Architecture:
        ------------
        The minute boundary is already known from relative ToA validation:
          minute_boundary_rtp = anchor_arrival_rtp - propagation_delay
        
        This method extracts samples starting at second 0 of the minute and
        passes them to the BCD/FSK decoder. The decoder's job is to:
          - WWV BCD: Extract hour/minute from 100 Hz subcarrier pulse widths
          - CHU FSK: Extract hour/minute from FSK data frames
        
        The decoder does NOT need to search for the minute boundary - it's given.
        
        Returns (hour, minute, timing_offset_samples) if successful, None otherwise.
        The timing_offset is provided for diagnostics but not used for timing.
        """
        minute_boundary_rtp = cluster.rtp_timestamp
        
        for channel, buffer_list in self.channel_buffers.items():
            station = self._channel_to_station(channel)
            if station not in ['WWV', 'WWVH', 'CHU']:
                continue
            
            # Concatenate all buffers for this channel
            all_samples = []
            min_rtp = None
            for samples, rtp_start in buffer_list:
                if min_rtp is None:
                    min_rtp = rtp_start
                all_samples.append(samples)
            
            if not all_samples or min_rtp is None:
                continue
            
            combined = np.concatenate(all_samples)
            
            # Compute offset of minute boundary in combined buffer
            minute_offset = minute_boundary_rtp - min_rtp
            
            if minute_offset < 0 or minute_offset + SAMPLES_PER_MINUTE > len(combined):
                logger.debug(f"{channel}: minute boundary outside buffer range")
                continue
            
            logger.debug(f"{channel}: trying decode at offset {minute_offset} ({minute_offset/self.sample_rate:.3f}s)")
            
            # Try to decode at this position
            if station in ['WWV', 'WWVH']:
                result = self._decode_wwv_bcd(combined, int(minute_offset))
            else:  # CHU
                result = self._decode_chu_fsk(combined, int(minute_offset))
            
            if result:
                logger.info(f"Decoded from {channel}: {result[0]:02d}:{result[1]:02d} UTC")
                return result
        
        return None
    
    def _decode_wwv_bcd(
        self,
        samples: np.ndarray,
        minute_start_offset: int
    ) -> Optional[Tuple[int, int, int]]:
        """Decode WWV/WWVH BCD time code.
        
        Returns:
            Tuple of (hour, minute, coarse_sync_offset_samples) or None
            The coarse_sync_offset is the correction needed for system clock error.
        """
        try:
            from hf_timestd.core.wwv_bcd_decoder import WWVBCDDecoder
            
            decoder = WWVBCDDecoder(sample_rate=self.sample_rate)
            
            # Extract one minute of samples starting at minute boundary
            minute_samples = samples[minute_start_offset:minute_start_offset + SAMPLES_PER_MINUTE]
            
            if len(minute_samples) < SAMPLES_PER_MINUTE:
                return None
            
            result = decoder.decode_minute(minute_samples)
            
            if result and result.detected and result.decoded_hour is not None:
                offset_ms = result.coarse_sync_offset_samples / self.sample_rate * 1000
                logger.info(f"WWV BCD decode: {result.decoded_hour:02d}:{result.decoded_minute:02d} "
                           f"(coarse_sync={offset_ms:.0f}ms)")
                return (result.decoded_hour, result.decoded_minute, result.coarse_sync_offset_samples)
            
        except Exception as e:
            logger.debug(f"WWV BCD decode failed: {e}")
        
        return None
    
    def _decode_chu_fsk(
        self,
        samples: np.ndarray,
        minute_start_offset: int
    ) -> Optional[Tuple[int, int, int]]:
        """Decode CHU FSK time code.
        
        Returns:
            Tuple of (hour, minute, timing_offset_samples) or None
            The timing_offset is derived from frame B edge detection.
        """
        try:
            from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder
            import time
            
            decoder = CHUFSKDecoder(sample_rate=self.sample_rate)
            
            # Extract one minute of samples starting at minute boundary
            minute_samples = samples[minute_start_offset:minute_start_offset + SAMPLES_PER_MINUTE]
            
            if len(minute_samples) < SAMPLES_PER_MINUTE:
                return None
            
            # For bootstrap, use current system time as approximate reference
            # The decoder validates that decoded time is within ±1 hour of this
            approx_minute_unix = time.time()
            
            result = decoder.decode_minute(minute_samples, approx_minute_unix)
            
            # Check if we got valid decoded time
            if result and result.detected and result.decoded_hour is not None:
                # CHU timing offset from frame B edge (if available)
                timing_offset_samples = 0
                if hasattr(result, 'timing_offset_ms') and result.timing_offset_ms is not None:
                    timing_offset_samples = int(result.timing_offset_ms * self.sample_rate / 1000)
                
                logger.info(f"CHU FSK decode: {result.decoded_hour:02d}:{result.decoded_minute:02d}")
                return (result.decoded_hour, result.decoded_minute, timing_offset_samples)
            
        except Exception as e:
            logger.debug(f"CHU FSK decode failed: {e}")
        
        return None
    
    def estimate_time_from_system_clock(self, minute_boundary_rtp: int) -> Optional[Tuple[int, int]]:
        """
        DEPRECATED: This method should NOT be used.
        
        UTC time must come from BCD/FSK decode of the actual broadcasts,
        not from NTP or any other external source.
        
        Returns None - time confirmation requires BCD/FSK decode.
        """
        logger.debug("estimate_time_from_system_clock called but NTP fallback is disabled")
        return None
    
    def compute_result(self) -> Optional[BootstrapResult]:
        """
        Run the full bootstrap process and return the result.
        
        Bootstrap Architecture:
        ----------------------
        1. Find recurring clusters validated by RELATIVE ToA differences
           - Geographic delay + ionospheric delay determines expected ToA pattern
           - Multi-station clusters prove we found real minute markers
           
        2. Back-calculate minute boundary RTP from validated cluster
           - minute_boundary_rtp = anchor_arrival_rtp - propagation_delay_samples
           - This is the RTP timestamp of second 0 of the minute
           
        3. Attempt BCD/FSK decode using the KNOWN minute boundary
           - BCD (100 Hz subcarrier) and FSK schemas orient within a minute
           - Decoder is told where second 0 is, extracts hour:minute
           
        4. If BCD/FSK succeeds: full timing reference available
           If BCD/FSK fails: we have minute boundary but not which UTC minute
        
        Returns BootstrapResult if successful, None otherwise.
        """
        # Step 1: Find recurring clusters (validated by relative ToA)
        cluster_pair = self.find_recurring_clusters()
        if cluster_pair is None:
            logger.info("Bootstrap: waiting for recurring clusters (relative ToA validation)")
            return None
        
        cluster1, cluster2 = cluster_pair
        
        # Step 2: Back-calculate minute boundary RTP from anchor tone
        # The cluster.rtp_timestamp is already the minute boundary (anchor - delay)
        # computed during cluster formation
        best_station = cluster1.stations[0]
        delay_ms = self.propagation_delays.get(best_station, 0)
        delay_samples = int(delay_ms * self.sample_rate / 1000)
        
        # cluster1.rtp_timestamp is the anchor arrival time
        # minute_boundary = arrival - propagation_delay
        minute_boundary_rtp = cluster1.rtp_timestamp - delay_samples
        
        logger.info(f"Bootstrap: minute boundary at RTP={minute_boundary_rtp} "
                   f"(anchor={best_station}, delay={delay_ms:.1f}ms)")
        
        # Step 3: Attempt BCD/FSK decode using the KNOWN minute boundary
        # The decoder is given samples starting at second 0 of the minute
        decoded_time = self.decode_time(cluster1)
        if decoded_time is None:
            decoded_time = self.decode_time(cluster2)
        
        if decoded_time is None:
            # BCD/FSK decode failed - we have minute boundary but not UTC minute
            # NO NTP FALLBACK - time must come from broadcasts
            logger.info("Bootstrap: minute boundary found, waiting for BCD/FSK decode")
            
            # Return partial result with minute boundary but no UTC confirmation
            result = BootstrapResult(
                reference_rtp=minute_boundary_rtp,
                reference_utc=None,  # Not confirmed yet
                uncertainty_ms=5.0,
                decoded_hour=None,
                decoded_minute=None,
                stations_used=cluster1.stations + cluster2.stations
            )
            self._result = result
            return result
        
        # Step 4: BCD/FSK succeeded - compute full timing reference
        decoded_hour, decoded_minute, _ = decoded_time
        
        # Compute which minute this is (from decoded time)
        minutes_since_midnight = decoded_hour * 60 + decoded_minute
        
        # Reference RTP = minute boundary RTP - (minutes * samples_per_minute)
        reference_rtp = minute_boundary_rtp - (minutes_since_midnight * SAMPLES_PER_MINUTE)
        
        # Reference UTC = midnight today (date from system clock, time from BCD/FSK)
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
        logger.info(f"  minute_boundary_rtp={minute_boundary_rtp}")
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
