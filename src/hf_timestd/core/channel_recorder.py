#!/usr/bin/env python3
"""
hf-timestd Channel Recorder - Single-channel process for distributed CPU load

This module runs as a standalone process for ONE channel, allowing the OS
to distribute multiple channel recorders across CPU cores.

Usage:
    python -m hf_timestd.core.channel_recorder \
        --config /path/to/timestd-config.toml \
        --channel "WWV 10 MHz" \
        --frequency 10000000

The parent launcher (core_recorder_v3.py) spawns one of these per channel.
"""

import argparse
import hashlib
import logging
import signal
import sys
import os
import time
import json
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone

from ka9q import discover_channels, RadiodControl, ChannelInfo

from .stream_recorder_v2 import StreamRecorderV2, StreamRecorderConfig

logger = logging.getLogger(__name__)


def generate_timestd_multicast_ip(station_id: str, instrument_id: str) -> str:
    """Generate deterministic multicast IP for hf-timestd channels."""
    key = f"GRAPE:{station_id}:{instrument_id}"
    hash_bytes = hashlib.sha256(key.encode()).digest()
    octet2 = (hash_bytes[0] % 254) + 1
    octet3 = hash_bytes[1]
    octet4 = (hash_bytes[2] % 254) + 1
    return f"239.{octet2}.{octet3}.{octet4}"


class ChannelRecorder:
    """
    Single-channel recorder process.
    
    Runs in its own process to avoid Python GIL contention.
    """
    
    def __init__(self, config: dict, channel_name: str, frequency_hz: int):
        self.config = config
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        
        # Station config
        self.station_config = config.get('station', {})
        self.recorder_config = config.get('recorder', {})
        
        # Output directory
        mode = self.recorder_config.get('mode', 'test')
        if mode == 'production':
            self.output_dir = Path(self.recorder_config.get('production_data_root', '/var/lib/timestd'))
        else:
            self.output_dir = Path(self.recorder_config.get('test_data_root', '/tmp/timestd-test'))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # ka9q connection
        self.status_address = config.get('ka9q', {}).get('status_address', '239.192.152.141')
        self.control = RadiodControl(self.status_address)
        
        # Generate multicast IP
        station_id = self.station_config.get('id', 'S000000')
        instrument_id = self.station_config.get('instrument_id', '0')
        self.data_destination = generate_timestd_multicast_ip(station_id, instrument_id)
        
        # Channel defaults
        self.channel_defaults = config.get('recorder', {}).get('channel_defaults', {})
        
        # Recorder instance
        self.recorder: Optional[StreamRecorderV2] = None
        self.channel_info: Optional[ChannelInfo] = None
        
        # State
        self.running = False
        self.start_time = time.time()
        
        logger.info(f"ChannelRecorder initialized: {channel_name} @ {frequency_hz/1e6:.3f} MHz")
    
    def _setup_channel(self) -> bool:
        """Create/configure the channel in radiod."""
        try:
            sample_rate = self.channel_defaults.get('sample_rate', 20000)
            preset = self.channel_defaults.get('preset', 'iq')
            
            # Discover existing channels first
            all_channels = discover_channels(self.status_address, listen_duration=2.0)
            
            # Check if channel already exists at our frequency with our destination
            existing_ssrc = None
            for ssrc, ch_info in all_channels.items():
                if abs(ch_info.frequency - self.frequency_hz) < 100:  # Within 100 Hz
                    if ch_info.multicast_address == self.data_destination:
                        existing_ssrc = ssrc
                        logger.info(f"Found existing channel {self.channel_name}: SSRC={ssrc}")
                        break
            
            if existing_ssrc:
                ssrc = existing_ssrc
            else:
                # Create new channel
                ssrc = self.control.create_channel(
                    frequency_hz=self.frequency_hz,
                    sample_rate=sample_rate,
                    destination=self.data_destination,
                    preset=preset,
                    agc_enable=self.channel_defaults.get('agc', 0),
                    gain=float(self.channel_defaults.get('gain', 0)),
                )
                
                if ssrc is None:
                    logger.error(f"Failed to create channel for {self.channel_name}")
                    return False
                
                logger.info(f"Created channel {self.channel_name}: SSRC={ssrc}")
                
                # Re-discover to get channel info
                time.sleep(0.5)
                all_channels = discover_channels(self.status_address, listen_duration=2.0)
            
            if ssrc not in all_channels:
                logger.error(f"Channel {self.channel_name} SSRC={ssrc} not found")
                return False
            
            self.channel_info = all_channels[ssrc]
            
            # Create recorder
            recorder_config = StreamRecorderConfig(
                ssrc=ssrc,
                frequency_hz=self.frequency_hz,
                sample_rate=sample_rate,
                description=self.channel_name,
                output_dir=self.output_dir,
                station_config=self.station_config,
                receiver_grid=self.station_config.get('grid_square', ''),
                compression=self.recorder_config.get('compression', 'none'),
                compression_level=self.recorder_config.get('compression_level', 3),
            )
            
            self.recorder = StreamRecorderV2(
                config=recorder_config,
                channel_info=self.channel_info,
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Channel setup failed: {e}", exc_info=True)
            return False
    
    def start(self):
        """Start recording."""
        if not self._setup_channel():
            return False
        
        self.running = True
        self.recorder.start()
        logger.info(f"✅ {self.channel_name}: Recording started")
        return True
    
    def stop(self):
        """Stop recording."""
        self.running = False
        if self.recorder:
            self.recorder.stop()
        logger.info(f"🛑 {self.channel_name}: Recording stopped")
    
    def run(self):
        """Main loop - run until signaled to stop."""
        if not self.start():
            return 1
        
        # Set up signal handlers
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, stopping...")
            self.running = False
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        
        # Main loop
        while self.running:
            time.sleep(1.0)
            
            # Health check
            if self.recorder and not self.recorder.is_healthy(timeout_sec=60.0):
                logger.warning(f"{self.channel_name}: No data for 60s, checking...")
        
        self.stop()
        return 0


def main():
    parser = argparse.ArgumentParser(description='hf-timestd Single-Channel Recorder')
    parser.add_argument('--config', required=True, help='Path to timestd-config.toml')
    parser.add_argument('--channel', required=True, help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--frequency', type=int, required=True, help='Frequency in Hz')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Load config
    import toml
    with open(args.config) as f:
        config = toml.load(f)
    
    # Create and run recorder
    recorder = ChannelRecorder(config, args.channel, args.frequency)
    sys.exit(recorder.run())


if __name__ == '__main__':
    main()
