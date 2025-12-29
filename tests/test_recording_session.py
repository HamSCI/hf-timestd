#!/usr/bin/env python3
"""
Test RecordingSession with a live radiod stream.

This test:
1. Discovers channels from radiod
2. Creates a minimal SegmentWriter that logs to memory
3. Records 2 short segments (5 seconds each)
4. Verifies samples, gaps, and timing

Usage:
    python tests/test_recording_session.py [--status-address 239.192.152.141]
"""

import sys
import time
import argparse
import logging
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

# Configure logging BEFORE imports (some modules may configure their own handlers)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    force=True  # Override any existing configuration
)
logger = logging.getLogger(__name__)

# Add src to path for development
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from hf_timestd import (
    RTPReceiver, RTPHeader,
    RecordingSession, SessionConfig, SessionState,
    SegmentInfo, SegmentWriter, GapInfo,
    ChannelInfo, discover_channels, rtp_to_wallclock
)


@dataclass
class MemorySegment:
    """A segment stored in memory for testing"""
    info: SegmentInfo
    samples: List[np.ndarray] = field(default_factory=list)
    gaps: List[GapInfo] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def total_samples(self) -> int:
        return sum(len(s) for s in self.samples)


class MemoryWriter(SegmentWriter):
    """
    Simple SegmentWriter that stores segments in memory.
    Used for testing RecordingSession.
    """
    
    def __init__(self):
        self.segments: List[MemorySegment] = []
        self.current: Optional[MemorySegment] = None
        self.write_count = 0
    
    def start_segment(self, segment_info: SegmentInfo, metadata: Dict[str, Any]) -> None:
        logger.info(f"📝 Starting segment {segment_info.segment_id}")
        self.current = MemorySegment(info=segment_info, metadata=metadata.copy())
    
    def write_samples(self, samples: np.ndarray, rtp_timestamp: int,
                      gap_info: Optional[GapInfo] = None) -> None:
        if self.current is None:
            return
        
        self.current.samples.append(samples.copy())
        self.write_count += 1
        
        if gap_info and gap_info.gap_samples > 0:
            self.current.gaps.append(gap_info)
            logger.warning(f"  Gap detected: {gap_info.gap_samples} samples filled")
    
    def finish_segment(self, segment_info: SegmentInfo) -> Optional[str]:
        if self.current is None:
            return None
        
        self.segments.append(self.current)
        total = self.current.total_samples
        gaps = len(self.current.gaps)
        
        logger.info(f"✅ Segment {segment_info.segment_id} complete: "
                   f"{total} samples, {gaps} gaps, {self.write_count} writes")
        
        self.current = None
        self.write_count = 0
        return f"segment_{segment_info.segment_id}"


def discover_test_channel(status_address: str) -> Optional[ChannelInfo]:
    """Find a channel to test with"""
    logger.info(f"Discovering channels from {status_address}...")
    
    try:
        channels_dict = discover_channels(status_address, listen_duration=3.0)
        
        if not channels_dict:
            logger.error("No channels found")
            return None
        
        # Prefer IQ channels (preset='iq') for testing
        channels = list(channels_dict.values())
        iq_channels = [c for c in channels if c.preset == 'iq']
        
        if iq_channels:
            channel = iq_channels[0]
            logger.info(f"Found {len(channels)} channels, using IQ channel SSRC {channel.ssrc}")
        else:
            channel = channels[0]
            logger.info(f"Found {len(channels)} channels (no IQ), using SSRC {channel.ssrc}")
        logger.info(f"  Frequency: {channel.frequency/1e6:.6f} MHz")
        logger.info(f"  Sample rate: {channel.sample_rate} Hz")
        logger.info(f"  Preset: {channel.preset}")
        
        # Check timing info
        if channel.gps_time and channel.rtp_timesnap:
            logger.info(f"  GPS timing: gps_time={channel.gps_time}, rtp_timesnap={channel.rtp_timesnap}")
        else:
            logger.warning("  No GPS timing available in status")
        
        return channel
        
    except Exception as e:
        logger.error(f"Discovery failed: {e}")
        return None


def run_test(status_address: str, segment_duration: float = 5.0, 
             num_segments: int = 2) -> bool:
    """
    Run the RecordingSession test.
    
    Args:
        status_address: Radiod status multicast address
        segment_duration: Seconds per segment
        num_segments: Number of segments to record
        
    Returns:
        True if test passed
    """
    # Discover channel
    channel = discover_test_channel(status_address)
    if not channel:
        return False
    
    # Create RTP receiver
    receiver = RTPReceiver(
        multicast_address=channel.multicast_address,
        port=channel.port
    )
    
    # Create memory writer
    writer = MemoryWriter()
    
    # Create session config
    config = SessionConfig(
        ssrc=channel.ssrc,
        sample_rate=channel.sample_rate,
        description=f"Test {channel.frequency/1e6:.3f} MHz",
        segment_duration_sec=segment_duration,
        align_to_boundary=False,  # Start immediately for testing
    )
    
    # Create session
    session = RecordingSession(
        config=config,
        rtp_receiver=receiver,
        writer=writer,
        channel_info=channel,
        on_segment_complete=lambda info, result: logger.info(f"  → Segment callback: {result}"),
        metadata_provider=lambda: {"test": True, "timestamp": time.time()},
    )
    
    # Start
    logger.info(f"\n{'='*60}")
    logger.info(f"Starting test: {num_segments} segments × {segment_duration}s each")
    logger.info(f"{'='*60}\n")
    
    receiver.start()
    session.start()
    
    # Wait for segments
    timeout = (segment_duration * num_segments) + 10  # Extra buffer
    start_time = time.time()
    
    while len(writer.segments) < num_segments:
        if time.time() - start_time > timeout:
            logger.error(f"Timeout waiting for segments (got {len(writer.segments)}/{num_segments})")
            break
        
        # Log progress
        metrics = session.get_metrics()
        state = session.get_state()
        logger.debug(f"State: {state.value}, Packets: {metrics['packets_received']}, "
                    f"Segments: {len(writer.segments)}/{num_segments}")
        
        time.sleep(0.5)
    
    # Stop
    session.stop()
    receiver.stop()
    
    # Analyze results
    logger.info(f"\n{'='*60}")
    logger.info("TEST RESULTS")
    logger.info(f"{'='*60}\n")
    
    metrics = session.get_metrics()
    
    # Check packets received
    if metrics['packets_received'] == 0:
        logger.error("❌ FAIL: No packets received")
        return False
    logger.info(f"✓ Packets received: {metrics['packets_received']}")
    
    # Check segments
    if len(writer.segments) < num_segments:
        logger.error(f"❌ FAIL: Only {len(writer.segments)}/{num_segments} segments")
        return False
    logger.info(f"✓ Segments completed: {len(writer.segments)}")
    
    # Check samples per segment (exclude last segment if partial)
    expected_samples = int(segment_duration * channel.sample_rate)
    tolerance = 0.1  # 10% tolerance
    
    all_samples_ok = True
    for i, seg in enumerate(writer.segments):
        actual = seg.total_samples
        diff_pct = abs(actual - expected_samples) / expected_samples
        
        # Last segment may be partial (stopped mid-recording)
        is_last = (i == len(writer.segments) - 1)
        is_partial = diff_pct > tolerance
        
        if is_partial and is_last:
            logger.info(f"  Segment {seg.info.segment_id}: {actual} samples (partial, stopped early)")
        elif is_partial:
            logger.warning(f"  Segment {seg.info.segment_id}: {actual} samples "
                          f"(expected ~{expected_samples}, diff={diff_pct*100:.1f}%)")
            all_samples_ok = False
        else:
            logger.info(f"  Segment {seg.info.segment_id}: {actual} samples ✓")
    
    if not all_samples_ok:
        logger.warning("⚠️  Some segments have unexpected sample counts")
    
    # Check timing
    timing_available = channel.gps_time is not None and channel.rtp_timesnap is not None
    if timing_available:
        logger.info(f"✓ Transport timing available (GPS_TIME/RTP_TIMESNAP)")
        
        # Test rtp_to_wallclock
        if writer.segments:
            seg = writer.segments[0]
            test_ts = seg.info.start_rtp_timestamp
            wallclock = rtp_to_wallclock(test_ts, channel)
            if wallclock:
                logger.info(f"  rtp_to_wallclock({test_ts}) = {wallclock:.3f}")
            else:
                logger.warning(f"  rtp_to_wallclock returned None")
    else:
        logger.info("ℹ️  Transport timing not available (radiod may not be sending GPS_TIME)")
    
    # Check gaps
    total_gaps = sum(len(s.gaps) for s in writer.segments)
    total_gap_samples = sum(g.gap_samples for s in writer.segments for g in s.gaps)
    
    if total_gaps > 0:
        logger.warning(f"⚠️  Gaps detected: {total_gaps} gaps, {total_gap_samples} samples filled")
    else:
        logger.info(f"✓ No gaps detected")
    
    # Summary
    logger.info(f"\n{'='*60}")
    if metrics['packets_received'] > 0 and len(writer.segments) >= num_segments:
        logger.info("✅ TEST PASSED")
        logger.info(f"{'='*60}\n")
        return True
    else:
        logger.error("❌ TEST FAILED")
        logger.info(f"{'='*60}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test RecordingSession with live radiod")
    parser.add_argument('--status-address', default='239.192.152.141',
                       help='Radiod status multicast address')
    parser.add_argument('--segment-duration', type=float, default=5.0,
                       help='Segment duration in seconds')
    parser.add_argument('--num-segments', type=int, default=2,
                       help='Number of segments to record')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Enable debug logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    success = run_test(
        status_address=args.status_address,
        segment_duration=args.segment_duration,
        num_segments=args.num_segments
    )
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
