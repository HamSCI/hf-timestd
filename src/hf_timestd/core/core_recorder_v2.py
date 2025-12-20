#!/usr/bin/env python3
"""
HF Time Standard Core Recorder V2 - Using ka9q-python RadiodStream

ACTIVE IMPLEMENTATION (v3.11+, Dec 2025)
This is the primary recorder implementation, replacing the legacy `CoreRecorder` (v1)
and `RTPReceiver`.

Simplified recorder that uses ka9q-python's RadiodStream for RTP handling.
This eliminates custom RTPReceiver and PacketResequencer code.

Responsibilities:
1. Discover/create channels in radiod via ka9q-python
2. Create RadiodStream for each channel
3. Receive decoded IQ samples via callback
4. Write to Phase 1 archive and queue for Phase 2/3

ka9q-python handles:
- RTP packet reception
- Packet resequencing
- Gap detection and filling
- Sample decoding
- Quality metrics
"""

import hashlib
import logging
import signal
import sys
import os
import time
import json
import threading
import subprocess
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime, timezone

from ka9q import discover_channels, RadiodControl, ChannelInfo, StreamQuality, Encoding, generate_multicast_ip

from ..quota_manager import QuotaManager
from .stream_recorder_v2 import StreamRecorderV2, StreamRecorderConfig

logger = logging.getLogger(__name__)




class CoreRecorderV2:
    """
    Core recorder V2: Uses ka9q-python RadiodStream and RadiodControl.
    
    Design principles:
    - Leverage ka9q-python for RTP and channel management
    - Minimal custom code
    - Anti-hijacking: only modify channels with our destination
    - Optimized for reliability
    """
    
    def __init__(self, config: dict):
        """
        Initialize core recorder.
        
        Args:
            config: Configuration dict with:
                - output_dir: Base directory for archives
                - station: Station metadata (callsign, grid, instrument_id)
                - channels: List of channel configs
                - channel_defaults: Default parameters for channels
                - status_address: Radiod status address
        """
        self.config = config
        self.output_dir = Path(config['output_dir'])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Channel management via ka9q-python RadiodControl
        self.status_address = config.get('status_address', '239.192.152.141')
        self.control = RadiodControl(self.status_address)
        
        # Station config
        self.station_config = config.get('station', {})
        self.recorder_config = config.get('recorder', {})
        
        # Generate dedicated multicast IP from station/instrument ID
        station_id = self.station_config.get('id', 'S000000')
        instrument_id = self.station_config.get('instrument_id', '0')
        # Use ka9q-python's deterministic multicast IP generation
        unique_id = f"TIMESTD:{station_id}:{instrument_id}"
        mcast_addr = generate_multicast_ip(unique_id)
        # Use port 5004 for explicit matching with radiod defaults
        self.data_destination = f"{mcast_addr}:5004"
        
        # Channel specs and defaults
        self.channel_specs = config.get('channels', [])
        self.channel_defaults = config.get('channel_defaults', {
            'preset': 'iq',
            'sample_rate': 20000,
            'agc': 0,
            'gain': 0.0,
            'encoding': Encoding.F32
        })
        
        # Channel info from discovery (ssrc -> ChannelInfo)
        self.channel_infos: Dict[int, ChannelInfo] = {}
        
        # Per-channel recorders (ssrc -> StreamRecorderV2)
        self.recorders: Dict[int, StreamRecorderV2] = {}
        
        logger.info(f"CoreRecorderV2: {len(self.channel_specs)} channels configured")
        logger.info(f"  hf-timestd multicast: {self.data_destination}")
        logger.info(f"  Defaults: preset={self.channel_defaults.get('preset')}, "
                   f"sample_rate={self.channel_defaults.get('sample_rate')}")
        
        # NTP status cache
        self.ntp_status = {'offset_ms': None, 'synced': False, 'last_update': 0}
        self.ntp_status_lock = threading.Lock()
        
        # Status tracking
        self.start_time = time.time()
        self.status_file = self.output_dir / 'status' / 'core-recorder-status.json'
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Graceful shutdown
        self.running = False
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def run(self):
        """Main run loop."""
        self.running = True
        logger.info("Starting hf-timestd core recorder v2 (using ka9q-python RadiodStream)")
        
        # Ensure channels exist and get ChannelInfo
        if not self._initialize_channels():
            logger.error("Failed to initialize channels - exiting")
            return
        
        self.running = True
        
        # Start all recorders
        for freq, recorder in self.recorders.items():
            recorder.start()
            logger.info(f"Started recorder for {freq/1e6:.3f} MHz ({recorder.config.description})")
        
        logger.info("Core recorder running. Press Ctrl+C to stop.")
        
        # Write initial status
        self._write_status()
        
        # Initialize quota manager
        self.quota_manager = QuotaManager(
            data_root=self.output_dir,
            threshold_percent=75.0,
            min_days_to_keep=7,
            dry_run=False
        )
        
        # Main loop
        last_status_time = 0
        last_health_check = 0
        last_quota_check = 0
        
        try:
            while self.running:
                time.sleep(1)
                now = time.time()
                
                # Update NTP status (every 10 seconds)
                if now - last_status_time >= 10:
                    self._update_ntp_status()
                    self._write_status()
                    last_status_time = now
                
                # Periodic status logging (every 60 seconds)
                if int(now) % 60 == 0:
                    self._log_status()
                
                # Health monitoring (every 30 seconds)
                if now - last_health_check >= 30:
                    self._monitor_health()
                    last_health_check = now
                
                # Quota enforcement (every 5 minutes)
                if now - last_quota_check >= 300:
                    self._enforce_quota()
                    last_quota_check = now
        
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        
        finally:
            self._shutdown()
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def _initialize_channels(self) -> bool:
        """
        Initialize channels: delegate discovery and creation to ManagedStream.
        
        Returns:
            True if recorders were created successfully
        """
        try:
            if not self.channel_specs:
                logger.warning("No channels configured")
                return False
            
            logger.info(f"Initializing {len(self.channel_specs)} channels via ManagedStream...")
            
            # Warm up discovery to prevent redundant channel creation
            logger.info("Warming up radiod discovery...")
            try:
                discover_channels(self.status_address, listen_duration=2.0)
            except Exception as e:
                logger.warning(f"Discovery warm-up failed: {e}")

            # Get defaults
            sample_rate = self.channel_defaults.get('sample_rate', 20000)
            
            # Create StreamRecorderV2 for each channel
            for ch_spec in self.channel_specs:
                freq_hz = int(ch_spec['frequency_hz'])
                description = ch_spec.get('description', f'{freq_hz/1e6:.3f} MHz')
                
                # Defaults are merged with channel-specific config
                preset = ch_spec.get('preset', self.channel_defaults.get('preset', 'iq'))
                encoding_val = ch_spec.get('encoding', self.channel_defaults.get('encoding', Encoding.F32))
                
                # Map string encoding to integer constant if necessary
                if isinstance(encoding_val, str):
                    if encoding_val.upper() == 'F32':
                        encoding = Encoding.F32
                    elif encoding_val.upper() == 'S16LE':
                        encoding = Encoding.S16LE
                    elif encoding_val.upper() == 'OPUS':
                        encoding = Encoding.OPUS
                    else:
                        logger.warning(f"Unknown encoding string '{encoding_val}', defaulting to NO_ENCODING")
                        encoding = Encoding.NO_ENCODING
                else:
                    encoding = encoding_val
                
                recorder_config = StreamRecorderConfig(
                    ssrc=0, 
                    frequency_hz=freq_hz,
                    preset=preset,
                    encoding=encoding,
                    sample_rate=sample_rate,
                    description=description,
                    destination=self.data_destination,
                    output_dir=self.output_dir,
                    station_config=self.station_config,
                    receiver_grid=self.station_config.get('grid_square', ''),
                    compression=self.recorder_config.get('compression', 'none'),
                    compression_level=self.recorder_config.get('compression_level', 3),
                )
                
                # Initialize recorder - we pass None for channel_info as ManagedStream 
                # will discover/create it on start()
                recorder = StreamRecorderV2(
                    config=recorder_config,
                    channel_info=None, # Will be filled by ManagedStream
                    get_ntp_status=self.get_ntp_status,
                    control=self.control,
                )
                
                # Store by frequency since SSRC is not yet known
                self.recorders[freq_hz] = recorder
            
            logger.info(f"✓ Initialized {len(self.recorders)} channel recorders (managed)")
            return True
            
        except Exception as e:
            logger.error(f"Channel initialization failed: {e}", exc_info=True)
            return False
    
    def _write_status(self):
        """Write status to JSON file for web-ui monitoring."""
        try:
            status = {
                'service': 'core_recorder',
                'version': '2.1-radiod_stream',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'uptime_seconds': int(time.time() - self.start_time),
                'pid': os.getpid(),
                'channels': {},
                'overall': {
                    'channels_active': 0,
                    'channels_total': len(self.recorders),
                    'total_samples_received': 0,
                    'total_samples_written': 0,
                }
            }
            
            for freq, recorder in self.recorders.items():
                ch_stats = recorder.get_status()
                # Use SSRC as key if known, otherwise use hex frequency
                ssrc = recorder.config.ssrc
                key = hex(ssrc) if ssrc and ssrc != 0 else f"freq_{freq}"
                
                # Add metadata to ch_stats for better UI/debugging
                ch_stats['preset'] = recorder.config.preset
                ch_stats['encoding'] = recorder.config.encoding
                
                status['channels'][key] = ch_stats
                
                if ch_stats.get('samples_received', 0) > 0:
                    status['overall']['channels_active'] += 1
                status['overall']['total_samples_received'] += ch_stats.get('samples_received', 0)
                status['overall']['total_samples_written'] += ch_stats.get('samples_written', 0)
            
            # Write atomically
            temp_file = self.status_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            temp_file.replace(self.status_file)
            
        except Exception as e:
            logger.error(f"Failed to write status file: {e}")
    
    def _log_status(self):
        """Log periodic status."""
        for ssrc, recorder in self.recorders.items():
            stats = recorder.get_stats()
            quality = recorder.get_quality()
            
            completeness = quality.completeness_pct if quality else 0
            
            logger.info(
                f"{recorder.config.description}: "
                f"{stats.get('minutes_written', 0)} min, "
                f"{stats.get('samples_received', 0)} samples, "
                f"completeness={completeness:.1f}%"
            )
    
    def _update_ntp_status(self):
        """Update NTP status cache."""
        try:
            offset_ms = self._get_ntp_offset()
            
            with self.ntp_status_lock:
                self.ntp_status = {
                    'offset_ms': offset_ms,
                    'synced': (offset_ms is not None and abs(offset_ms) < 100),
                    'last_update': time.time()
                }
        except Exception as e:
            logger.warning(f"NTP status update failed: {e}")
    
    def get_ntp_status(self) -> dict:
        """Thread-safe accessor for NTP status."""
        with self.ntp_status_lock:
            return self.ntp_status.copy()
    
    @staticmethod
    def _get_ntp_offset() -> Optional[float]:
        """Get NTP offset in milliseconds."""
        try:
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'System time' in line:
                        parts = line.split(':')
                        if len(parts) >= 2:
                            offset_str = parts[1].strip().split()[0]
                            return float(offset_str) * 1000.0
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        return None
    
    def _monitor_health(self):
        """Monitor stream health."""
        try:
            for freq, recorder in self.recorders.items():
                if not recorder.is_healthy():
                    silence = recorder.get_silence_duration()
                    logger.warning(
                        f"Channel {recorder.config.description} silent for {silence:.0f}s"
                    )
                    
                    # Check if channel still exists
                    try:
                        ssrc = recorder.config.ssrc
                        if ssrc and ssrc != 0:
                            channels = discover_channels(self.status_address, listen_duration=1.0)
                            if ssrc not in channels:
                                logger.error(f"Channel {ssrc:x} ({recorder.config.description}) missing from radiod")
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Health monitoring error: {e}")
    
    def _enforce_quota(self):
        """Enforce disk quota."""
        try:
            result = self.quota_manager.enforce_quota()
            if result.get('files_deleted', 0) > 0:
                logger.info(
                    f"Quota: deleted {result['files_deleted']} files, "
                    f"freed {result['bytes_freed'] / 1024**3:.2f} GB"
                )
        except Exception as e:
            logger.error(f"Quota enforcement error: {e}")
    
    def _shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down core recorder...")
        
        # Stop all recorders
        for freq, recorder in self.recorders.items():
            try:
                ssrc = recorder.config.ssrc
                final_quality = recorder.stop()
                if final_quality:
                    logger.info(
                        f"{recorder.config.description}: Final completeness "
                        f"{final_quality.completeness_pct:.2f}%"
                    )
                
                # Explicitly remove channel from radiod to prevent proliferation
                if ssrc and ssrc != 0:
                    try:
                        self.control.remove_channel(ssrc)
                        logger.info(f"Released channel {ssrc:x} from radiod")
                    except Exception as e:
                        logger.debug(f"Failed to remove channel {ssrc:x}: {e}")
                        
            except Exception as e:
                logger.error(f"Error stopping recorder for freq {freq}: {e}")
        
        # Close RadiodControl
        try:
            self.control.close()
        except Exception:
            pass
        
        # Write final status
        self._write_status()
        
        logger.info("Core recorder stopped")


def main():
    """Main entry point."""
    import argparse
    import toml
    
    parser = argparse.ArgumentParser(description='HF Time Standard Core Recorder V2')
    parser.add_argument('--config', required=True, help='Path to config file')
    args = parser.parse_args()
    
    # Load config
    with open(args.config) as f:
        config = toml.load(f)
    
    # Setup logging
    log_level = config.get('logging', {}).get('level', 'INFO')
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    
    # Determine output directory based on mode
    mode = config.get('recorder', {}).get('mode', 'test')
    if mode == 'test':
        output_dir = config.get('recorder', {}).get('test_data_root', '/tmp/timestd-test')
    else:
        output_dir = config.get('recorder', {}).get('production_data_root', '/var/lib/timestd')
    
    # Build recorder config
    recorder_section = config.get('recorder', {})
    recorder_config = {
        'output_dir': output_dir,
        'station': config.get('station', {}),
        'recorder': recorder_section,
        'channels': recorder_section.get('channels', []),
        'channel_defaults': recorder_section.get('channel_defaults', {}),
        'status_address': config.get('ka9q', {}).get('status_address', '239.192.152.141'),
    }
    
    logger.info(f"Loaded {len(recorder_config['channels'])} channels from config")
    
    # Run recorder
    recorder = CoreRecorderV2(recorder_config)
    recorder.run()


if __name__ == '__main__':
    main()
