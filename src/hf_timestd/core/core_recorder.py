#!/usr/bin/env python3
"""
HF Time Standard Core Recorder (V1) - Legacy Implementation [DEPRECATED]

.. deprecated:: 3.11.0
   Use `CoreRecorderV2` (core_recorder_v2.py) instead.
   This implementation uses the legacy `RTPReceiver` and `PacketResequencer`.
Battle-Tested Data Acquisition

Minimal, rock-solid recorder that writes raw IQ archives.
NO analytics, NO tone detection, NO quality metrics.

Responsibilities:
1. Receive RTP packets from ka9q-radio multicast
2. Resequence packets (handle out-of-order delivery)
3. Detect gaps via RTP timestamp discontinuities
4. Fill gaps with zeros (maintain sample count integrity)
5. Write complete archives with RTP timestamps

That's it. Everything else is analytics (separate process).
"""

import logging
import signal
import sys
import os
import time
import json
import threading
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess

from ..core.rtp_receiver import RTPReceiver
from ..channel_manager import ChannelManager
from ..radiod_health import RadiodHealthChecker
from ..quota_manager import QuotaManager
from .pipeline_recorder import PipelineRecorder, PipelineRecorderConfig

logger = logging.getLogger(__name__)


@dataclass
class ChannelConfig:
    """Configuration for a single channel"""
    ssrc: int
    frequency_hz: float
    sample_rate: int
    description: str


class CoreRecorder:
    """
    Core recorder: RTP → NPZ archives
    
    Design principles:
    - Minimal code (~200 lines)
    - Conservative error handling (never crash)
    - No dependencies on analytics code
    - Optimized for reliability over features
    """
    
    def __init__(self, config: dict):
        """
        Initialize core recorder
        
        Args:
            config: Configuration dict with:
                - multicast_address: RTP multicast group
                - port: RTP port (default 5004)
                - output_dir: Base directory for archives
                - station: Station metadata (callsign, grid, instrument_id)
                - channels: List of channel configs
                - channel_defaults: Default parameters for channels
                - status_address: Radiod status address for channel management
                - data_destination: RTP destination for channels (mDNS or IP)
        """
        self.config = config
        self.output_dir = Path(config['output_dir'])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Channel management for auto-recovery
        self.status_address = config.get('status_address', '239.192.152.141')
        self.channel_manager = ChannelManager(self.status_address)
        self.health_checker = RadiodHealthChecker(self.status_address)
        
        # Store station config for multicast IP generation
        self.station_config = config.get('station', {})
        self.recorder_config = config.get('recorder', {})
        
        # Use default destination (None) to let radiod decide
        # This prevents redundant channels by allowing hf-timestd to share channels
        self.data_destination = None
        
        # Store channel configs and defaults
        self.channel_specs = config.get('channels', [])  # Raw channel specs
        self.channel_defaults = config.get('channel_defaults', {
            'preset': 'iq',
            'sample_rate': 20000,
            'agc': 0,
            'gain': 0.0
        })
        self.channel_configs = {}  # Will be populated with SSRC -> config after channel creation
        
        # RTP receiver will be initialized after channels are created
        self.rtp_receiver = None
        
        # Per-channel recorders (using PipelineRecorder for two-phase architecture)
        self.channels: Dict[int, PipelineRecorder] = {}
        
        # Timing parameters
        self.blocktime_ms = self.recorder_config.get('blocktime_ms', 20.0)
        self.max_gap_seconds = self.recorder_config.get('max_gap_seconds', 60.0)
        
        # Storage quota (parse "80%" string to float 80.0)
        storage_quota_str = self.recorder_config.get('storage_quota', '80%')
        if isinstance(storage_quota_str, str) and storage_quota_str.endswith('%'):
            self.storage_quota_percent = float(storage_quota_str.rstrip('%'))
        else:
            self.storage_quota_percent = float(storage_quota_str) if storage_quota_str else 80.0
        
        # Tiered storage configuration (RAM hot buffer + disk cold storage)
        self.use_tiered_storage = self.recorder_config.get('tiered_storage', False)
        if self.use_tiered_storage:
            from .tiered_storage import init_tiered_storage
            hot_buffer_root = self.recorder_config.get('hot_buffer_root', '/dev/shm/timestd')
            ram_percent = self.recorder_config.get('ram_percent', 20)
            num_channels = len(self.channel_specs)
            
            self._tiered_manager = init_tiered_storage(
                num_channels=num_channels,
                hot_buffer_root=hot_buffer_root,
                cold_buffer_root=str(self.output_dir),
                ram_percent=ram_percent,
                auto_start=True
            )
            logger.info(f"Tiered storage enabled: hot={hot_buffer_root}, "
                       f"hot_minutes={self._tiered_manager.hot_minutes}")
        else:
            self._tiered_manager = None
        
        logger.info(f"CoreRecorder: {len(self.channel_specs)} channels configured")
        logger.info(f"  hf-timestd multicast: {self.data_destination} (exclusive stream)")
        logger.info(f"  Defaults: preset={self.channel_defaults.get('preset')}, "
                   f"sample_rate={self.channel_defaults.get('sample_rate')}")
        
        # Centralized NTP status cache (shared by all channels)
        # Updated periodically in main loop to avoid subprocess calls in critical path
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
        """Main run loop"""
        logger.info("Starting hf-timestd core recorder")
        logger.info("Responsibility: RTP → raw_buffer (no analytics)")
        
        # Ensure all channels exist in radiod and initialize recorders
        logger.info("Verifying radiod channels...")
        if not self._ensure_channels_exist():
            logger.error("Failed to initialize channels - exiting")
            return
        
        # Start RTP receiver
        self.rtp_receiver.start()
        self.running = True
        
        # Start all channel recorders
        for ssrc, recorder in self.channels.items():
            recorder.start()
            logger.info(f"Started recorder for SSRC {ssrc:x} ({recorder.config.description})")
        
        logger.info("Core recorder running. Press Ctrl+C to stop.")
        
        # Write initial status
        self._write_status()
        
        # Initialize quota manager (75% threshold, keep min 7 days)
        self.quota_manager = QuotaManager(
            data_root=self.output_dir,
            threshold_percent=75.0,
            min_days_to_keep=7,
            dry_run=False
        )
        
        # Main loop (status updates and health monitoring)
        last_status_time = 0
        last_health_check = 0
        last_quota_check = 0
        try:
            while self.running:
                time.sleep(1)
                now = time.time()
                
                # Update NTP status cache (every 10 seconds)
                # This is the ONLY place NTP subprocess calls happen
                if now - last_status_time >= 10:
                    self._update_ntp_status()
                    self._write_status()
                    last_status_time = now
                
                # Periodic status logging (every 60 seconds)
                if int(now) % 60 == 0:
                    self._log_status()
                
                # Health monitoring (every 30 seconds)
                if now - last_health_check >= 30:
                    self._monitor_stream_health()
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
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def _write_status(self):
        """Write current status to JSON file for web-ui monitoring"""
        try:
            status = {
                'service': 'core_recorder',
                'version': '2.0',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'uptime_seconds': int(time.time() - self.start_time),
                'pid': os.getpid(),
                'channels': {},
                'overall': {
                    'channels_active': 0,
                    'channels_total': len(self.channels),
                    'total_minutes_written': 0,
                    'total_packets_received': 0,
                    'total_gaps_detected': 0
                }
            }
            
            # Gather per-channel stats
            for ssrc, recorder in self.channels.items():
                ch_stats = recorder.get_status()
                status['channels'][hex(ssrc)] = ch_stats
                
                # Aggregate overall stats
                if ch_stats.get('packets_received', 0) > 0:
                    status['overall']['channels_active'] += 1
                status['overall']['total_minutes_written'] += ch_stats.get('minutes_written', 0)
                status['overall']['total_packets_received'] += ch_stats.get('packets_received', 0)
                # PipelineRecorder doesn't track gaps the same way
                # status['overall']['total_gaps_detected'] += ch_stats.get('gaps_detected', 0)
            
            # Write atomically (write to temp file, then rename)
            temp_file = self.status_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            temp_file.replace(self.status_file)
            
        except Exception as e:
            logger.error(f"Failed to write status file: {e}")
    
    def _log_status(self):
        """Log periodic status"""
        for ssrc, recorder in self.channels.items():
            stats = recorder.get_stats()
            logger.info(
                f"{recorder.config.description}: "
                f"{stats.get('minutes_written', 0)} minutes, "
                f"{stats.get('packets_received', 0)} packets, "
                f"state={stats.get('state', 'unknown')}"
            )
    
    def _update_ntp_status(self):
        """
        Update centralized NTP status cache (called every 10 seconds in main loop).
        
        This is the ONLY place where NTP subprocess calls happen, eliminating
        redundant calls from ChannelProcessor and CoreNPZWriter critical paths.
        """
        try:
            offset_ms = self._get_ntp_offset_subprocess()
            
            with self.ntp_status_lock:
                self.ntp_status = {
                    'offset_ms': offset_ms,
                    'synced': (offset_ms is not None and abs(offset_ms) < 100),
                    'last_update': time.time()
                }
                
        except Exception as e:
            logger.warning(f"NTP status update failed: {e}")
    
    def get_ntp_status(self):
        """
        Thread-safe accessor for NTP status (used by ChannelProcessors).
        
        Returns:
            dict with keys: offset_ms, synced, last_update
        """
        with self.ntp_status_lock:
            return self.ntp_status.copy()
    
    @staticmethod
    def _get_ntp_offset_subprocess() -> Optional[float]:
        """
        Get NTP offset in milliseconds via subprocess call.
        
        Called ONLY from _update_ntp_status() every 10 seconds.
        
        Returns:
            NTP offset in ms (positive = system ahead of NTP)
            None if NTP not available
        """
        try:
            # Try chronyc first
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2
            )
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'System time' in line:
                        # Parse "System time : 0.000012345 seconds fast of NTP time"
                        parts = line.split(':')
                        if len(parts) >= 2:
                            offset_str = parts[1].strip().split()[0]
                            offset_seconds = float(offset_str)
                            return offset_seconds * 1000.0  # Convert to ms
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        
        try:
            # Try ntpq as fallback
            result = subprocess.run(
                ['ntpq', '-c', 'rv 0 offset'],
                capture_output=True, text=True, timeout=2
            )
            
            if result.returncode == 0:
                # Parse "offset=-0.123"
                for part in result.stdout.split(','):
                    if 'offset=' in part:
                        offset_str = part.split('=')[1].strip()
                        return float(offset_str)  # Already in ms
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        
        return None
    
    def _ensure_channels_exist(self) -> bool:
        """
        Verify all configured channels exist in radiod, create if missing.
        Also initializes RTP receiver and PipelineRecorder instances.
        Called at startup.
        
        Returns:
            True if all channels initialized successfully
        """
        try:
            if not self.channel_specs:
                logger.warning("No channels configured")
                return False
            
            # Use new config format: ensure channels exist with auto-SSRC allocation
            logger.info(f"Ensuring {len(self.channel_specs)} channels exist in radiod...")
            freq_to_ssrc = self.channel_manager.ensure_channels_from_config(
                channels=self.channel_specs,
                defaults=self.channel_defaults,
                destination=self.data_destination
            )
            
            if not freq_to_ssrc:
                logger.error("No channels could be created in radiod")
                return False
            
            # Get the multicast address for RTP receiver
            # Since we let radiod decide, we should discover it from the created channels
            # However, for the legacy RTPReceiver, we just bind to 0.0.0.0 (all interfaces)
            # or rely on the fact that we'll join the multicast groups for the channels we found.
            
            # NOTE: Legacy RTPReceiver needs a specific address if we want to filter,
            # but ideally we should be using StreamRecorderV2 which doesn't use this class.
            # For now, we'll assume standard multicast behavior.
            multicast_address = '0.0.0.0' 
            logger.info(f"RTP receiver will listen on {multicast_address}")
            
            # Initialize RTP receiver
            self.rtp_receiver = RTPReceiver(
                multicast_address=multicast_address,
                port=self.config.get('port', 5004)
            )
            
            # Get sample_rate from defaults
            sample_rate = self.channel_defaults.get('sample_rate', 20000)
            
            # Create PipelineRecorder instances for each allocated channel
            for ch_spec in self.channel_specs:
                freq_hz = int(ch_spec['frequency_hz'])
                if freq_hz not in freq_to_ssrc:
                    logger.warning(f"Channel {freq_hz/1e6:.3f} MHz was not created - skipping")
                    continue
                
                ssrc = freq_to_ssrc[freq_hz]
                description = ch_spec.get('description', f'{freq_hz/1e6:.3f} MHz')
                
                # Store config for health monitoring/recovery
                self.channel_configs[ssrc] = {
                    'frequency_hz': freq_hz,
                    'description': description,
                    'sample_rate': sample_rate,
                    **self.channel_defaults
                }
                
                pipeline_config = PipelineRecorderConfig(
                    ssrc=ssrc,
                    frequency_hz=freq_hz,
                    sample_rate=sample_rate,
                    description=description,
                    output_dir=self.output_dir,
                    station_config=self.station_config,
                    receiver_grid=self.station_config.get('grid_square', ''),
                    blocktime_ms=self.blocktime_ms,
                    max_gap_seconds=self.max_gap_seconds,
                    storage_quota_percent=self.storage_quota_percent,
                    use_tiered_storage=self.use_tiered_storage,
                )
                
                recorder = PipelineRecorder(
                    config=pipeline_config,
                    rtp_receiver=self.rtp_receiver,
                    get_ntp_status=self.get_ntp_status,
                )
                self.channels[ssrc] = recorder
            
            logger.info(f"✓ Initialized {len(self.channels)} channel recorders")
            return len(self.channels) > 0
                
        except Exception as e:
            logger.error(f"Channel initialization failed: {e}", exc_info=True)
            return False
    
    def _get_multicast_address(self) -> str:
        """
        Get the multicast address for RTP receiver.
        
        Returns the deterministic multicast IP generated from
        station_id and instrument_id in __init__.
        
        Returns:
            Multicast address string (e.g., "239.71.82.65")
        """
        # data_destination is None, so we return a default or empty string
        # This method is largely obsolete with the removal of custom IPs
        return self.data_destination or "0.0.0.0"
    
    def _monitor_stream_health(self):
        """
        Monitor stream health and recreate dead channels.
        Called periodically (every 30 seconds) in main loop.
        """
        try:
            # Check if radiod is alive
            if not self.health_checker.is_radiod_alive(timeout_sec=3.0):
                logger.warning("Radiod appears down - skipping health check")
                return
            
            # Check each channel for stale packets
            for ssrc, recorder in self.channels.items():
                # Check if channel has gone silent
                if not recorder.is_healthy():
                    logger.warning(
                        f"Channel {recorder.config.description} (SSRC {ssrc:x}) appears dead - "
                        f"no packets in {recorder.get_silence_duration():.0f}s"
                    )
                    
                    # Verify channel still exists in radiod
                    if not self.health_checker.verify_channel_exists(ssrc):
                        logger.error(
                            f"Channel {recorder.config.description} missing from radiod - "
                            f"attempting recreation..."
                        )
                        self._recreate_channel(ssrc)
                    else:
                        logger.info(
                            f"Channel {recorder.config.description} exists in radiod but no data - "
                            f"possible network/multicast issue"
                        )
                        
        except Exception as e:
            logger.error(f"Health monitoring error: {e}", exc_info=True)
    
    def _enforce_quota(self):
        """
        Enforce disk quota by removing old files if over threshold.
        Called periodically (every 5 minutes) in main loop.
        """
        try:
            result = self.quota_manager.enforce_quota()
            
            if result.get('files_deleted', 0) > 0:
                logger.info(
                    f"Quota enforcement: deleted {result['files_deleted']} files, "
                    f"freed {result['bytes_freed'] / 1024 / 1024 / 1024:.2f} GB, "
                    f"usage now {result['final_usage_percent']:.1f}%"
                )
        except Exception as e:
            logger.error(f"Quota enforcement error: {e}", exc_info=True)
    
    def _recreate_channel(self, ssrc: int):
        """
        Recreate a missing channel in radiod.
        
        Args:
            ssrc: SSRC of the channel to recreate
        """
        if ssrc not in self.channel_configs:
            logger.error(f"No config found for SSRC {ssrc:x} - cannot recreate")
            return
        
        ch_cfg = self.channel_configs[ssrc]
        
        try:
            logger.info(f"Recreating channel {ch_cfg['description']} (SSRC {ssrc:x})...")
            
            # Create the channel with original SSRC
            allocated_ssrc = self.channel_manager.create_channel(
                frequency_hz=ch_cfg['frequency_hz'],
                preset=ch_cfg.get('preset', 'iq'),
                sample_rate=ch_cfg.get('sample_rate', 20000),
                agc=ch_cfg.get('agc', 0),
                gain=ch_cfg.get('gain', 0.0),
                destination=self.data_destination,
                ssrc=ssrc,  # Keep original SSRC
                description=ch_cfg['description']
            )
            
            if allocated_ssrc:
                logger.info(f"✓ Successfully recreated channel {ch_cfg['description']}")
                # Reset processor packet timer to avoid immediate re-trigger
                if ssrc in self.channels:
                    self.channels[ssrc].reset_health()
            else:
                logger.error(f"✗ Failed to recreate channel {ch_cfg['description']}")
                
        except Exception as e:
            logger.error(f"Channel recreation exception for SSRC {ssrc:x}: {e}", exc_info=True)
    
    def _shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down core recorder...")
        
        # Stop all channel recorders first (they unregister callbacks)
        for ssrc, recorder in self.channels.items():
            try:
                recorder.stop()
            except Exception as e:
                logger.error(f"Error stopping channel {ssrc}: {e}")
        
        # Stop RTP receiver
        self.rtp_receiver.stop()
        
        logger.info("Core recorder stopped")


def main():
    """Entry point for core recorder"""
    import toml
    import argparse
    
    parser = argparse.ArgumentParser(description='HF Time Standard Core Recorder')
    parser.add_argument('--config', required=True, help='Configuration file (TOML)')
    args = parser.parse_args()
    
    # Load TOML configuration
    toml_config = toml.load(args.config)
    
    # Setup logging
    log_level = toml_config.get('logging', {}).get('level', 'INFO')
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Transform TOML config to CoreRecorder format
    recorder_config = toml_config.get('recorder', {})
    mode = recorder_config.get('mode', 'test')
    
    # Determine output directory based on mode
    if mode == 'production':
        output_dir = recorder_config.get('production_data_root', '/var/lib/signal-recorder')
    else:
        output_dir = recorder_config.get('test_data_root', '/tmp/timestd-test')
    
    # Get KA9Q config
    ka9q_config = toml_config.get('ka9q', {})
    status_address = ka9q_config.get('status_address', 'bee1-hf-status.local')
    data_destination = ka9q_config.get('data_destination')  # e.g. "time-station-data.local"
    
    # Get channel defaults (new config format)
    channel_defaults = recorder_config.get('channel_defaults', {
        'preset': 'iq',
        'sample_rate': 20000,
        'agc': 0,
        'gain': 0.0
    })
    
    # Build config dict for CoreRecorder
    config = {
        'output_dir': output_dir,
        'port': 5004,
        'status_address': status_address,  # For channel management
        'data_destination': data_destination,  # RTP destination (mDNS or IP)
        'station': toml_config.get('station', {}),
        'recorder': recorder_config,  # Pass full recorder config
        'channels': recorder_config.get('channels', []),
        'channel_defaults': channel_defaults,
    }
    
    logger.info(f"Starting in {mode.upper()} mode")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Data destination: {data_destination}")
    logger.info(f"Channels configured: {len(config['channels'])}")
    logger.info(f"Defaults: preset={channel_defaults.get('preset')}, "
               f"sample_rate={channel_defaults.get('sample_rate')}")
    
    # Create and run recorder
    recorder = CoreRecorder(config)
    recorder.run()


if __name__ == '__main__':
    main()
