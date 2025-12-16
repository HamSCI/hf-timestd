#!/usr/bin/env python3
"""
RTP Packet Resequencer - Handle Out-of-Order Delivery

Implements circular buffer for packet resequencing and gap detection.
Follows KA9Q timing architecture: RTP timestamps are primary reference.
"""

import numpy as np
import logging
from collections import deque
from typing import Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RTPPacket:
    """Parsed RTP packet"""
    sequence: int           # RTP sequence number (16-bit, wraps)
    timestamp: int          # RTP timestamp (32-bit, wraps)
    ssrc: int              # RTP SSRC identifier
    samples: np.ndarray    # IQ samples (complex64)


@dataclass
class GapInfo:
    """Information about a detected gap"""
    expected_timestamp: int     # RTP timestamp we expected
    actual_timestamp: int       # RTP timestamp we received
    gap_samples: int           # Number of missing samples
    gap_packets: int           # Number of missing packets (estimate)
    prev_sequence: int         # Last good sequence number
    curr_sequence: int         # Current sequence number


class PacketResequencer:
    """
    Resequence out-of-order RTP packets and detect gaps
    
    Design:
    - Circular buffer of 64 packets (handles ~1.3 second jitter @ 400 samples/packet @ 20 kHz)
    - Process packets in sequence order
    - Detect gaps via RTP timestamp jumps
    - Fill gaps with zeros to maintain sample count integrity
    
    Key principle: Sample count integrity > real-time delivery
    """
    
    # Default maximum gap (can be overridden via constructor)
    DEFAULT_MAX_GAP_SAMPLES = 1_200_000  # 60 seconds at 20 kHz
    
    # Threshold for detecting stream discontinuity (reset instead of fill)
    # 10 seconds is long enough to distinguish from network jitter but
    # short enough to detect radiod restarts quickly
    DISCONTINUITY_THRESHOLD_SAMPLES = 200_000  # 10 seconds at 20 kHz
    
    def __init__(
        self, 
        buffer_size: int = 64, 
        samples_per_packet: int = 400,
        max_gap_samples: Optional[int] = None
    ):
        """
        Initialize resequencer
        
        Args:
            buffer_size: Circular buffer size (packets)
            samples_per_packet: Expected samples per packet (calculated from sample_rate * blocktime_ms / 1000)
            max_gap_samples: Maximum gap to fill with zeros (calculated from sample_rate * max_gap_seconds)
                            If None, uses DEFAULT_MAX_GAP_SAMPLES
        """
        self.buffer_size = buffer_size
        self.samples_per_packet = samples_per_packet
        self.max_gap_samples = max_gap_samples if max_gap_samples is not None else self.DEFAULT_MAX_GAP_SAMPLES
        
        # Circular buffer: sequence_num -> packet
        self.buffer: deque = deque(maxlen=buffer_size)
        self.buffer_seq_nums: set = set()  # Quick lookup
        
        # State tracking
        self.initialized = False
        self.next_expected_seq: Optional[int] = None
        self.next_expected_ts: Optional[int] = None
        self.last_output_ts: Optional[int] = None
        
        # Statistics
        self.packets_received = 0
        self.packets_resequenced = 0
        self.gaps_detected = 0
        self.samples_filled = 0
        self.stream_resets = 0  # Count of detected discontinuities
        
        logger.debug(f"PacketResequencer initialized: buffer={buffer_size}, samples/pkt={samples_per_packet}")
    
    def process_packet(self, packet: RTPPacket) -> Tuple[Optional[np.ndarray], Optional[GapInfo]]:
        """
        Process incoming RTP packet
        
        Args:
            packet: Parsed RTP packet
        
        Returns:
            (samples, gap_info) tuple:
            - samples: IQ samples ready for output (None if buffering)
            - gap_info: If gap detected, info about the gap
        """
        self.packets_received += 1
        
        # Initialize on first packet
        if not self.initialized:
            self._initialize(packet)
            return None, None  # Buffer first packet
        
        # Check for stream discontinuity BEFORE processing
        # This detects radiod restarts, channel recreation, etc.
        if self._detect_discontinuity(packet):
            self._reset_for_discontinuity(packet)
            return None, None  # Start fresh from this packet
        
        # Check for duplicate
        if packet.sequence in self.buffer_seq_nums:
            logger.debug(f"Duplicate packet seq={packet.sequence}, ignoring")
            return None, None
        
        # Add to buffer
        self._add_to_buffer(packet)
        
        # Try to output packets in sequence order
        return self._try_output()
    
    def _initialize(self, packet: RTPPacket):
        """Initialize sequencer with first packet"""
        self.next_expected_seq = packet.sequence
        self.next_expected_ts = packet.timestamp
        self.last_output_ts = packet.timestamp
        self._add_to_buffer(packet)
        self.initialized = True
        logger.info(f"Sequencer initialized: seq={packet.sequence}, ts={packet.timestamp}")
    
    def _detect_discontinuity(self, packet: RTPPacket) -> bool:
        """
        Detect stream discontinuity that requires resequencer reset.
        
        A discontinuity is a timestamp jump so large it indicates the RTP
        session restarted (e.g., radiod recreated the channel). We detect:
        - Forward jumps > DISCONTINUITY_THRESHOLD_SAMPLES
        - Backward jumps > DISCONTINUITY_THRESHOLD_SAMPLES (absolute value)
        
        Returns:
            True if discontinuity detected (caller should reset)
        """
        if not self.initialized or self.next_expected_ts is None:
            return False
        
        # Calculate signed timestamp difference (handles 32-bit wrap)
        ts_diff = (packet.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        if ts_diff >= 0x80000000:
            ts_diff = ts_diff - 0x100000000  # Convert to signed
        
        # Check for discontinuity in either direction
        if abs(ts_diff) > self.DISCONTINUITY_THRESHOLD_SAMPLES:
            logger.warning(
                f"Stream discontinuity detected: timestamp jump of {ts_diff} samples "
                f"({ts_diff / 20000:.1f} seconds). "
                f"Expected ts={self.next_expected_ts}, received ts={packet.timestamp}. "
                f"Resetting resequencer state (likely radiod restart or channel recreation)."
            )
            return True
        
        return False
    
    def _reset_for_discontinuity(self, packet: RTPPacket):
        """
        Reset resequencer state after detecting a stream discontinuity.
        
        This allows seamless recovery when the RTP stream restarts with
        a new timestamp base (e.g., after radiod restart).
        """
        self.stream_resets += 1
        
        # Clear buffer
        self.buffer.clear()
        self.buffer_seq_nums.clear()
        
        # Reinitialize from current packet
        self.next_expected_seq = packet.sequence
        self.next_expected_ts = packet.timestamp
        self.last_output_ts = packet.timestamp
        self._add_to_buffer(packet)
        
        logger.info(
            f"Resequencer reset #{self.stream_resets}: "
            f"new seq={packet.sequence}, ts={packet.timestamp}. "
            f"Recording will continue from this point."
        )
    
    def _add_to_buffer(self, packet: RTPPacket):
        """Add packet to circular buffer"""
        # If buffer is at capacity, deque will auto-pop oldest on append
        # We need to remove its sequence from the set BEFORE the append
        if len(self.buffer) >= self.buffer_size:
            oldest = self.buffer[0]  # Will be popped by append
            self.buffer_seq_nums.discard(oldest.sequence)
        
        self.buffer.append(packet)
        self.buffer_seq_nums.add(packet.sequence)
    
    def _try_output(self) -> Tuple[Optional[np.ndarray], Optional[GapInfo]]:
        """Try to output next packet in sequence"""
        # Look for next expected sequence number
        next_pkt = None
        for pkt in self.buffer:
            if pkt.sequence == self.next_expected_seq:
                next_pkt = pkt
                break
        
        if next_pkt is None:
            # Packet not in buffer yet - keep waiting
            # But check if we should give up (packet lost)
            if len(self.buffer) >= self.buffer_size // 2:
                # Buffer is filling up - probably lost packet
                # Output gap-fill and skip ahead
                return self._handle_lost_packet()
            else:
                # Keep waiting
                return None, None
        
        # Found next packet - check for gap
        gap_info = None
        gap_samples = None
        
        if next_pkt.timestamp != self.next_expected_ts:
            # RTP timestamp jump - check if it's a forward gap
            gap_info = self._detect_gap(next_pkt)
            # Only create gap fill and log if there's actually a gap to fill
            if gap_info.gap_samples > 0:
                gap_samples = self._create_gap_fill(gap_info)
                logger.warning(
                    f"Gap detected: {gap_info.gap_samples} samples "
                    f"({gap_info.gap_packets} packets), "
                    f"ts {self.next_expected_ts} -> {next_pkt.timestamp}"
                )
        
        # Remove from buffer
        self.buffer.remove(next_pkt)
        self.buffer_seq_nums.discard(next_pkt.sequence)
        
        # Update state
        self.next_expected_seq = (next_pkt.sequence + 1) & 0xFFFF  # 16-bit wrap
        self.next_expected_ts = next_pkt.timestamp + self.samples_per_packet
        self.last_output_ts = next_pkt.timestamp
        
        # Return samples (gap-fill + packet data)
        if gap_samples is not None:
            # Concatenate gap fill + real samples
            output = np.concatenate([gap_samples, next_pkt.samples])
            return output, gap_info
        else:
            return next_pkt.samples, None
    
    def _detect_gap(self, next_pkt: RTPPacket) -> GapInfo:
        """
        Detect and characterize gap using KA9Q technique.
        
        Uses signed 32-bit arithmetic (Phil Karn's pcmrecord.c approach):
        - Cast timestamp difference to int32 to handle wraps naturally
        - Negative = backward jump (ignore)
        - Positive = forward gap (fill with zeros)
        """
        # KA9Q technique: signed 32-bit difference handles wraps naturally
        # Python doesn't have fixed-width int types, so simulate with masking
        ts_diff = (next_pkt.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        
        # Convert to signed int32 range: treat values > 2^31 as negative
        if ts_diff >= 0x80000000:
            ts_gap = ts_diff - 0x100000000  # Make negative
        else:
            ts_gap = ts_diff
        
        # Only fill forward gaps (positive jumps)
        if ts_gap <= 0:
            if ts_gap < 0:
                logger.debug(
                    f"Backward timestamp jump: {ts_gap} samples "
                    f"(expected_ts={self.next_expected_ts}, actual_ts={next_pkt.timestamp}). "
                    f"Ignoring (likely out-of-order or restart)."
                )
            ts_gap = 0
        else:
            # Only count as gap if there's actual missing samples
            self.gaps_detected += 1
        
        # Cap gap to reasonable maximum (discontinuities should be caught earlier)
        # This is a safety net - large gaps here indicate the discontinuity
        # detection didn't trigger for some reason
        if ts_gap > self.DISCONTINUITY_THRESHOLD_SAMPLES:
            logger.warning(
                f"Large gap {ts_gap} samples exceeds threshold. "
                f"Capping to {self.DISCONTINUITY_THRESHOLD_SAMPLES}."
            )
            ts_gap = self.DISCONTINUITY_THRESHOLD_SAMPLES
        
        # Estimate packets lost
        packets_lost = ts_gap // self.samples_per_packet
        
        self.samples_filled += ts_gap
        
        return GapInfo(
            expected_timestamp=self.next_expected_ts,
            actual_timestamp=next_pkt.timestamp,
            gap_samples=ts_gap,
            gap_packets=packets_lost,
            prev_sequence=self.next_expected_seq - 1,
            curr_sequence=next_pkt.sequence
        )
    
    def _create_gap_fill(self, gap_info: GapInfo) -> np.ndarray:
        """Create zero-filled samples for gap"""
        return np.zeros(gap_info.gap_samples, dtype=np.complex64)
    
    def _handle_lost_packet(self) -> Tuple[Optional[np.ndarray], Optional[GapInfo]]:
        """Handle case where expected packet is lost"""
        # Find next available packet in buffer
        if len(self.buffer) == 0:
            return None, None
        
        # Get earliest packet by sequence (accounting for wrap)
        earliest_pkt = min(self.buffer, key=lambda p: self._seq_distance(self.next_expected_seq, p.sequence))
        
        # KA9Q technique: signed 32-bit difference
        ts_diff = (earliest_pkt.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        if ts_diff >= 0x80000000:
            ts_gap = ts_diff - 0x100000000  # Make negative
        else:
            ts_gap = ts_diff
        
        # Only fill forward gaps
        if ts_gap <= 0:
            if ts_gap < 0:
                logger.debug(f"Backward jump in lost packet recovery: {ts_gap} samples. Ignoring.")
            ts_gap = 0
        
        # Cap to discontinuity threshold (larger gaps should trigger reset, not fill)
        if ts_gap > self.DISCONTINUITY_THRESHOLD_SAMPLES:
            logger.warning(
                f"Large gap in lost packet recovery: {ts_gap} samples. "
                f"Capping to {self.DISCONTINUITY_THRESHOLD_SAMPLES}."
            )
            ts_gap = self.DISCONTINUITY_THRESHOLD_SAMPLES
        
        # Create gap info
        gap_info = GapInfo(
            expected_timestamp=self.next_expected_ts,
            actual_timestamp=earliest_pkt.timestamp,
            gap_samples=ts_gap,
            gap_packets=(earliest_pkt.sequence - self.next_expected_seq) & 0xFFFF,
            prev_sequence=(self.next_expected_seq - 1) & 0xFFFF,
            curr_sequence=earliest_pkt.sequence
        )
        
        # Only create gap fill if there's actually a gap
        if ts_gap > 0:
            gap_samples = self._create_gap_fill(gap_info)
            logger.warning(
                f"Lost packet recovery: skipped to seq={earliest_pkt.sequence}, "
                f"gap={gap_info.gap_samples} samples"
            )
            output = np.concatenate([gap_samples, earliest_pkt.samples])
        else:
            output = earliest_pkt.samples
        
        # Remove packet from buffer
        self.buffer.remove(earliest_pkt)
        self.buffer_seq_nums.discard(earliest_pkt.sequence)
        
        # Update state
        self.next_expected_seq = (earliest_pkt.sequence + 1) & 0xFFFF
        self.next_expected_ts = earliest_pkt.timestamp + self.samples_per_packet
        self.last_output_ts = earliest_pkt.timestamp
        
        self.packets_resequenced += 1
        
        return output, gap_info
    
    def _seq_distance(self, from_seq: int, to_seq: int) -> int:
        """Calculate forward distance between sequence numbers (handles wrap)"""
        dist = (to_seq - from_seq) & 0xFFFF
        return dist if dist < 32768 else dist - 65536
    
    def flush(self) -> List[Tuple[np.ndarray, Optional[GapInfo]]]:
        """Flush remaining packets in buffer (for shutdown)"""
        results = []
        
        # Sort buffer by sequence number
        sorted_pkts = sorted(self.buffer, key=lambda p: p.sequence)
        
        for pkt in sorted_pkts:
            # Check for gap
            gap_info = None
            gap_samples = None
            
            if pkt.timestamp != self.next_expected_ts:
                gap_info = self._detect_gap(pkt)
                if gap_info.gap_samples > 0:
                    gap_samples = self._create_gap_fill(gap_info)
            
            # Update state
            self.next_expected_ts = pkt.timestamp + self.samples_per_packet
            
            # Add result
            if gap_samples is not None:
                output = np.concatenate([gap_samples, pkt.samples])
            else:
                output = pkt.samples
            
            results.append((output, gap_info))
        
        # Clear buffer
        self.buffer.clear()
        self.buffer_seq_nums.clear()
        
        return results
    
    def get_stats(self) -> dict:
        """Get resequencer statistics"""
        return {
            'packets_received': self.packets_received,
            'packets_resequenced': self.packets_resequenced,
            'gaps_detected': self.gaps_detected,
            'samples_filled': self.samples_filled,
            'stream_resets': self.stream_resets,  # Discontinuity recoveries
            'buffer_used': len(self.buffer),
            'buffer_size': self.buffer_size
        }
