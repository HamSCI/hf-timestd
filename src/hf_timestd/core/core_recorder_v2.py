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
import socket
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime, timezone

from ka9q import discover_channels, RadiodControl, ChannelInfo, StreamQuality, Encoding

from ..quota_manager import QuotaManager
from .stream_recorder_v2 import StreamRecorderV2, StreamRecorderConfig

logger = logging.getLogger(__name__)

def get_host_ip() -> str:
    """Detect main network interface IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def allocate_stable_ssrc(freq_hz: float, preset: str, sample_rate: int) -> int:
    """
    Allocate a stable, deterministic SSRC using SHA-256.
    
    This matches the specific parameters we care about for uniqueness:
    Frequency, Preset, and Sample Rate.
    """
    # Create unique string key
    key = f"{int(freq_hz)}:{preset.lower()}:{int(sample_rate)}"
    
    # Hash it using SHA-256 for stability across processes/machines
    # (Python's hash() is randomized and changes per process)
    sha = hashlib.sha256(key.encode('utf-8')).digest()
    
    # Take first 4 bytes as integer
    val = int.from_bytes(sha[:4], byteorder='big')
    
    # Ensure it's a valid positive 31-bit SSRC (0 to 0x7FFFFFFF)
    # This avoids signed/unsigned issues and reserved ranges
    return val & 0x7FFFFFFF





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
        self.status_address = config.get('status_address')
        if not self.status_address:
            # Fallback to ka9q defaults if installed? Or error?
            # User requested "No Default fallback ip address".
            # Try to get from [ka9q] section if not at top level (config structure varies)
            ka9q_section = config.get('ka9q', {})
            self.status_address = ka9q_section.get('status_address')
            
        if not self.status_address:
            raise ValueError("Configuration missing 'status_address' in [ka9q] section")

        self.control = RadiodControl(self.status_address)
        
        # Station config
        self.station_config = config.get('station', {})
        self.recorder_config = config.get('recorder', {})
        
        # Let radiod use its configured default destination, OR force one if missing.
        # Logic update: We default to the standard multicast group to ensuring functional channels
        # when radiod doesn't auto-assign one for F32.
        self.data_destination = config.get('radiod_multicast_group')
        if not self.data_destination:
            # Try to get from individual channel configs if any (though usually global)
            # or default to a configurable system-wide default if we must.
            # For now, if None, ka9q-python's RadiodStream will let radiod decide or fail gracefully.
            logger.info("No explicit radiod_multicast_group in config, letting radiod decide destination")
        else:
            logger.info(f"Using configured multicast destination: {self.data_destination}")
        
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
        logger.info(">>> DEBUG VERSION: NETWORK FIX ACTIVE <<<")
        
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
        Initialize all channels.
        
        Delegates entirely to ka9q-python's ensure_channel() which handles:
        - Computing deterministic SSRC from parameters
        - Discovering existing channels
        - Reusing or creating channels as needed
        - Verifying channel configuration
        
        The client just needs to call ensure_channel() with consistent parameters.
        """
        try:
            if not self.channel_specs:
                logger.warning("No channels configured")
                return False

            logger.info(f"Initializing {len(self.channel_specs)} configured channels...")
            
            for ch_spec in self.channel_specs:
                freq = int(ch_spec['frequency_hz'])
                
                # Defaults are merged with channel-specific config
                preset = ch_spec.get('preset', self.channel_defaults.get('preset', 'iq'))
                sample_rate = self.channel_defaults.get('sample_rate', 20000)
                encoding_val = ch_spec.get('encoding', self.channel_defaults.get('encoding', Encoding.F32))
                agc_val = ch_spec.get('agc', self.channel_defaults.get('agc', 0))
                gain_val = ch_spec.get('gain', self.channel_defaults.get('gain', 0.0))
                
                # Map string encoding to integer constant
                if isinstance(encoding_val, str):
                    if encoding_val.upper() == 'F32':
                        encoding = Encoding.F32
                    elif encoding_val.upper() == 'S16LE':
                        encoding = Encoding.S16LE
                    elif encoding_val.upper() == 'OPUS':
                        encoding = Encoding.OPUS
                    else:
                        encoding = Encoding.NO_ENCODING
                else:
                    encoding = encoding_val

                logger.info(f"Requesting channel for {freq/1e6:.3f} MHz (ka9q-python will reuse if exists)")
                
                # Create config - let StreamRecorderV2/RobustManagedStream call ensure_channel()
                # with these parameters. The library will handle discovery and reuse.
                rec_config = StreamRecorderConfig(
                    ssrc=None,  # Let ka9q-python compute the SSRC
                    frequency_hz=freq,
                    encoding=encoding,
                    agc_enable=int(agc_val),
                    gain=float(gain_val),
                    description=ch_spec.get('description', f"{freq/1e6:.3f} MHz"),
                    preset=preset,
                    sample_rate=sample_rate,
                    output_dir=self.output_dir,
                    
                    # Propagation
                    receiver_grid=self.station_config.get('grid_square', ''),
                    station_config=self.station_config,
                    
                    # Storage settings
                    raw_buffer_file_duration_sec=3600,
                    tiered_storage=self.recorder_config.get('tiered_storage', False),
                    hot_buffer_root=Path(self.recorder_config.get('hot_buffer_root')) if self.recorder_config.get('hot_buffer_root') else None,
                    
                    # Compression
                    compression=self.recorder_config.get('compression', 'none'),
                    compression_level=self.recorder_config.get('compression_level', 3),
                    
                    # CRITICAL: Use None to let radiod assign destination consistently
                    # This ensures the same SSRC is computed every time
                    destination=None
                )
                
                # Create recorder - it will call ensure_channel() which handles everything
                recorder = StreamRecorderV2(
                    config=rec_config,
                    control=self.control 
                )
                
                self.recorders[freq] = recorder
                
            logger.info(f"✓ Initialized {len(self.recorders)} channel recorders")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize channels: {e}", exc_info=True)
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
                            # Use generous timeout for check
                            channels = discover_channels(self.status_address, listen_duration=2.5)
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
                
                # User request: "The client need not manage radiod in any way"
                # So we DO NOT remove channels on shutdown. We leave them for radiod/ka9q-python 
                # to manage, or for reuse on next start.
                # if ssrc and ssrc != 0:
                #     try:
                #         self.control.remove_channel(ssrc)
                #         logger.info(f"Released channel {ssrc:x} from radiod")
                #     except Exception as e:
                #         logger.debug(f"Failed to remove channel {ssrc:x}: {e}")
                        
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
    
    # Use paths.py for consistent, mode-aware path resolution
    from ..paths import load_paths_from_config
    paths = load_paths_from_config(args.config)
    output_dir = str(paths.data_root)
    
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
