#!/usr/bin/env python3
"""
Real Data Test: Three-Phase Pipeline with Live radiod

This script tests the three-phase pipeline with actual RTP data from radiod.
Uses the channels defined in timestd-config.toml.

Usage:
    python scripts/test_real_data_pipeline.py [--channel WWV_10_MHz] [--duration 120]
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Try tomllib (Python 3.11+), fall back to tomli
try:
    import tomllib
    def load_toml(path):
        with open(path, 'rb') as f:
            return tomllib.load(f)
except ImportError:
    try:
        import tomli
        def load_toml(path):
            with open(path, 'rb') as f:
                return tomli.load(f)
    except ImportError:
        # Fallback: use toml if available
        import toml
        def load_toml(path):
            return toml.load(path)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def load_config(config_path: Path) -> dict:
    """Load timestd-config.toml"""
    return load_toml(config_path)


def get_channel_by_description(config: dict, description: str) -> dict:
    """Find channel by description (e.g., 'WWV 10 MHz')"""
    for channel in config['recorder']['channels']:
        if channel['description'] == description:
            return channel
    return None


def list_channels(config: dict):
    """List all available channels"""
    print("\nAvailable channels:")
    for i, channel in enumerate(config['recorder']['channels']):
        print(f"  {i+1}. {channel['description']} ({channel['frequency_hz']/1e6:.2f} MHz)")


def run_pipeline_test(
    config: dict,
    channel_desc: str,
    duration_sec: int,
    output_dir: Path
):
    """
    Run the three-phase pipeline with real radiod data.
    
    Args:
        config: Loaded timestd-config.toml
        channel_desc: Channel description (e.g., 'WWV 10 MHz')
        duration_sec: Test duration in seconds
        output_dir: Output directory for test data
    """
    import numpy as np
    from grape_recorder.grape import (
        PipelineConfig,
        PipelineOrchestrator,
        PipelineState
    )
    from ka9q import RadiodControl, RadiodStream, discover_channels
    
    # Find channel
    channel_info = get_channel_by_description(config, channel_desc)
    if not channel_info:
        logger.error(f"Channel '{channel_desc}' not found in config")
        list_channels(config)
        return False
    
    # Get station config
    station_config = {
        'callsign': config['station']['callsign'],
        'grid_square': config['station']['grid_square'],
        'psws_station_id': config['station']['id'],
        'psws_instrument_id': config['station']['instrument_id'],
        'receiver_name': 'GRAPE'
    }
    
    # Channel parameters
    frequency_hz = channel_info['frequency_hz']
    sample_rate = config['recorder']['channel_defaults'].get('sample_rate', 20000)
    channel_name = channel_desc.replace(' ', '_')
    
    logger.info("=" * 70)
    logger.info(f"THREE-PHASE PIPELINE REAL DATA TEST")
    logger.info("=" * 70)
    logger.info(f"Channel: {channel_desc}")
    logger.info(f"Frequency: {frequency_hz/1e6:.2f} MHz")
    logger.info(f"Sample rate: {sample_rate} Hz")
    logger.info(f"Duration: {duration_sec} seconds")
    logger.info(f"Output: {output_dir}")
    logger.info(f"Station: {station_config['callsign']} @ {station_config['grid_square']}")
    logger.info("=" * 70)
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create pipeline config
    pipeline_config = PipelineConfig(
        data_dir=output_dir,
        channel_name=channel_name,
        frequency_hz=frequency_hz,
        sample_rate=sample_rate,
        receiver_grid=station_config['grid_square'],
        station_config=station_config,
        raw_archive_compression='gzip',
        raw_archive_file_duration_sec=3600,
        analysis_latency_sec=120,
        output_sample_rate=10,
        streaming_latency_minutes=2
    )
    
    # Connect to radiod (Phase 6: canonical field is `status`).
    status_address = config['ka9q']['status']
    logger.info(f"Connecting to radiod at {status_address}...")
    
    try:
        control = RadiodControl(status_address)
        logger.info("✅ Connected to radiod")
    except Exception as e:
        logger.error(f"Failed to connect to radiod: {e}")
        return False
    
    # Create channel on radiod
    logger.info(f"Creating channel for {frequency_hz/1e6:.2f} MHz...")
    
    try:
        # Create the channel - returns SSRC
        ssrc = control.create_channel(
            frequency_hz=frequency_hz,
            preset='iq',
            sample_rate=sample_rate
        )
        logger.info(f"✅ Channel created with SSRC: {ssrc}")
        
        # Wait a moment for channel to appear in status
        time.sleep(0.5)
        
        # Discover channels to get ChannelInfo for RadiodStream
        logger.info("Discovering channel info...")
        channels = discover_channels(status_address)
        
        # discover_channels returns a dict keyed by SSRC
        ka9q_channel = channels.get(ssrc)
        
        if ka9q_channel is None:
            logger.error(f"Channel with SSRC {ssrc} not found in status")
            logger.info(f"Available SSRCs: {list(channels.keys())[:10]}...")
            return False
        
        logger.info(f"✅ Found channel: freq={ka9q_channel.frequency/1e6:.3f} MHz, addr={ka9q_channel.multicast_address}")
        
    except Exception as e:
        logger.error(f"Failed to create channel: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Create pipeline orchestrator
    logger.info("Creating pipeline orchestrator...")
    orchestrator = PipelineOrchestrator(pipeline_config)
    
    # Statistics
    stats = {
        'packets_received': 0,
        'samples_processed': 0,
        'start_time': None,
        'last_packet_time': None,
        'last_rtp_timestamp': None
    }
    
    # Sample callback for RadiodStream
    def on_samples(samples: np.ndarray, quality):
        """Handle incoming samples from RadiodStream"""
        stats['packets_received'] += 1
        stats['last_packet_time'] = time.time()
        
        if stats['start_time'] is None:
            stats['start_time'] = time.time()
        
        # RadiodStream delivers complex64 samples directly
        iq_samples = samples.astype(np.complex64) if samples.dtype != np.complex64 else samples
        stats['samples_processed'] += len(iq_samples)
        
        # Get RTP timestamp from quality metrics
        rtp_timestamp = getattr(quality, 'last_rtp_timestamp', 0)
        if rtp_timestamp == 0:
            # Estimate from sample count
            if stats['last_rtp_timestamp'] is None:
                stats['last_rtp_timestamp'] = int(time.time() * sample_rate)
            else:
                stats['last_rtp_timestamp'] += len(iq_samples)
            rtp_timestamp = stats['last_rtp_timestamp']
        else:
            stats['last_rtp_timestamp'] = rtp_timestamp
        
        # Feed to pipeline
        system_time = time.time()
        orchestrator.process_samples(
            samples=iq_samples,
            rtp_timestamp=rtp_timestamp,
            system_time=system_time
        )
    
    # Create RadiodStream
    logger.info("Creating RadiodStream...")
    stream = RadiodStream(
        channel=ka9q_channel,
        on_samples=on_samples,
        samples_per_packet=sample_rate // 50,  # 20ms packets at sample_rate
        resequence_buffer_size=64,
        deliver_interval_packets=5  # Deliver every 5 packets (~100ms)
    )
    
    # Start pipeline and stream
    logger.info("Starting pipeline...")
    orchestrator.start()
    
    logger.info("Starting stream...")
    stream.start()
    
    # Setup signal handler for graceful shutdown
    running = True
    def signal_handler(sig, frame):
        nonlocal running
        logger.info("\nReceived shutdown signal...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run for specified duration
    logger.info(f"Recording for {duration_sec} seconds... (Ctrl+C to stop early)")
    logger.info("")
    
    start_time = time.time()
    last_status_time = start_time
    
    try:
        while running and (time.time() - start_time) < duration_sec:
            time.sleep(0.1)
            
            # Print status every 10 seconds
            if time.time() - last_status_time >= 10:
                elapsed = time.time() - start_time
                pipeline_stats = orchestrator.get_stats()
                
                logger.info(
                    f"[{elapsed:.0f}s] "
                    f"Callbacks: {stats['packets_received']:,} | "
                    f"Samples: {stats['samples_processed']:,} | "
                    f"Archived: {pipeline_stats.get('samples_archived', 0):,} | "
                    f"Analyzed: {pipeline_stats.get('minutes_analyzed', 0)} min"
                )
                last_status_time = time.time()
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    
    # Stop everything
    logger.info("")
    logger.info("Stopping stream...")
    final_quality = stream.stop()
    
    logger.info("Stopping pipeline...")
    orchestrator.stop()
    
    # Close radiod channel
    logger.info("Closing radiod channel...")
    try:
        control.delete_channel(ka9q_channel.ssrc)
    except Exception as e:
        logger.warning(f"Error closing channel: {e}")
    
    # Final statistics
    elapsed = time.time() - start_time
    final_stats = orchestrator.get_stats()
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("TEST COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Duration: {elapsed:.1f} seconds")
    logger.info(f"Packets received: {stats['packets_received']:,}")
    logger.info(f"Samples processed: {stats['samples_processed']:,}")
    logger.info(f"Samples archived (Phase 1): {final_stats.get('samples_archived', 0):,}")
    logger.info(f"Minutes analyzed (Phase 2): {final_stats.get('minutes_analyzed', 0)}")
    logger.info(f"Products generated (Phase 3): {final_stats.get('products_generated', 0)}")
    logger.info("")
    
    # Verify output
    logger.info("Checking output directories...")
    
    raw_archive_dir = output_dir / 'raw_archive' / channel_name
    clock_offset_dir = output_dir / 'clock_offset' / channel_name
    processed_dir = output_dir / 'processed' / channel_name
    
    phase1_ok = raw_archive_dir.exists()
    phase2_ok = clock_offset_dir.exists()
    phase3_ok = processed_dir.exists()
    
    logger.info(f"  Phase 1 (raw_archive): {'✅' if phase1_ok else '❌'} {raw_archive_dir}")
    logger.info(f"  Phase 2 (clock_offset): {'✅' if phase2_ok else '❌'} {clock_offset_dir}")
    logger.info(f"  Phase 3 (processed): {'✅' if phase3_ok else '❌'} {processed_dir}")
    
    # Check for session summary
    session_file = raw_archive_dir / 'metadata' / 'session_summary.json'
    if session_file.exists():
        import json
        with open(session_file) as f:
            summary = json.load(f)
        logger.info("")
        logger.info("Phase 1 Session Summary:")
        logger.info(f"  Total samples: {summary.get('total_samples', 0):,}")
        logger.info(f"  Gap samples: {summary.get('total_gap_samples', 0):,}")
        logger.info(f"  UTC correction applied: {summary.get('utc_correction_applied', 'unknown')}")
        
        if summary.get('utc_correction_applied') == False:
            logger.info("  ✅ Raw archive correctly has NO UTC correction!")
    
    # Check clock offset CSV
    clock_csv = clock_offset_dir / 'clock_offset_series.csv'
    if clock_csv.exists():
        import csv
        with open(clock_csv) as f:
            reader = csv.reader(f)
            rows = list(reader)
        n_measurements = len(rows) - 1  # Exclude header
        logger.info("")
        logger.info("Phase 2 Clock Offset Series:")
        logger.info(f"  Measurements: {n_measurements}")
        if n_measurements > 0:
            # Show last few
            logger.info("  Recent measurements:")
            for row in rows[-4:]:
                if row[0] != 'system_time':  # Skip header
                    try:
                        d_clock = float(row[3])
                        station = row[4]
                        confidence = float(row[9])
                        quality = row[11]
                        logger.info(f"    D_clock={d_clock:+.2f}ms ({station}, {quality}, conf={confidence:.2%})")
                    except:
                        pass
    
    logger.info("")
    logger.info("=" * 70)
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Test three-phase pipeline with real radiod data'
    )
    parser.add_argument(
        '--channel', '-c',
        default='WWV 10 MHz',
        help='Channel description (default: "WWV 10 MHz")'
    )
    parser.add_argument(
        '--duration', '-d',
        type=int,
        default=120,
        help='Test duration in seconds (default: 120)'
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        default=Path('/tmp/grape-pipeline-test'),
        help='Output directory (default: /tmp/grape-pipeline-test)'
    )
    parser.add_argument(
        '--config', '-f',
        type=Path,
        default=Path('config/timestd-config.toml'),
        help='Config file path'
    )
    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='List available channels and exit'
    )
    
    args = parser.parse_args()
    
    # Load config
    config_path = args.config
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        return 1
    
    config = load_config(config_path)
    
    if args.list:
        list_channels(config)
        return 0
    
    # Run test
    success = run_pipeline_test(
        config=config,
        channel_desc=args.channel,
        duration_sec=args.duration,
        output_dir=args.output
    )
    
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
