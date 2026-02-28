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

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("systemd-python not available, watchdog disabled")

from ka9q import discover_channels, RadiodControl, ChannelInfo, StreamQuality, Encoding

from ..quota_manager import QuotaManager
from .stream_recorder_v2 import StreamRecorderV2, StreamRecorderConfig, RobustManagedStream
from .timing_calibrator import TimingCalibrator
# NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
# The recorder now always archives immediately. MetrologyEngine's fusion_state
# handles timing lock internally using wider search windows until locked.

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

        # Try to resolve status address, falling back to discovery if needed
        from ka9q.utils import resolve_multicast_address
        try:
            # Check if address resolves
            resolve_multicast_address(self.status_address, timeout=2.0)
        except Exception:
            logger.warning(f"Failed to resolve configured address '{self.status_address}', attempting auto-discovery...")
            from ka9q.discovery import discover_radiod_services
            
            # Discover services
            services = discover_radiod_services(timeout=5.0)
            if not services:
                logger.error("Discovery failed: No radiod services found!")
                # processing will likely fail at RadiodControl init, but we let it proceed to raise the error there
            else:
                logger.info(f"Discovered {len(services)} radiod services: {[s['name'] for s in services]}")
                
                # Simple selection logic:
                # 1. Look for a service matching the configured hostname (loose match)
                # 2. Look for "B3" since user mentioned it
                # 3. Default to the first one
                selected = None
                
                # Try 1: configured name in service name?
                for s in services:
                    if self.status_address.replace('.local', '') in s['name'] or self.status_address in s['name']:
                        selected = s
                        break
                
                # Try 2: "B3" tag?
                if not selected:
                    for s in services:
                        if "B3" in s['name']:
                            selected = s
                            break
                            
                # Try 3: First available
                if not selected:
                    selected = services[0]
                
                if selected:
                    logger.warning(f"Redirecting to discovered service: '{selected['name']}' at {selected['address']}")
                    self.status_address = selected['address']

        self.control = RadiodControl(self.status_address)
        
        # Station config
        self.station_config = config.get('station', {})
        self.recorder_config = config.get('recorder', {})
        self.engine_type = self.recorder_config.get('engine', 'radiod')
        if self.engine_type not in ('radiod', 'phase-engine'):
            logger.warning(f"Unknown engine type '{self.engine_type}', defaulting to 'radiod'")
            self.engine_type = 'radiod'
        
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
        # Channels can be at top level or in recorder section
        self.channel_specs = config.get('channels', []) or self.recorder_config.get('channels', [])
        self.channel_defaults = config.get('channel_defaults', {}) or self.recorder_config.get('channel_defaults', {
            'preset': 'iq',
            'sample_rate': 20000, # Keeping this one as a safe fallback for the dict itself if completely missing, but code below will enforce logic.

            'agc': 0,
            'gain': 0.0,
            'encoding': Encoding.F32
        })
        
        # Channel info from discovery (ssrc -> ChannelInfo)
        self.channel_infos: Dict[int, ChannelInfo] = {}
        
        # Per-channel recorders (ssrc -> StreamRecorderV2)
        self.recorders: Dict[str, StreamRecorderV2] = {}
        
        logger.info(f"CoreRecorderV2: {len(self.channel_specs)} channels configured")
        logger.info(f"  Defaults: preset={self.channel_defaults.get('preset')}, "
                   f"sample_rate={self.channel_defaults.get('sample_rate')}")
        
        # NTP status cache
        self.ntp_status = {'offset_ms': None, 'synced': False, 'last_update': 0}
        self.ntp_status_lock = threading.Lock()

        # Timing Calibrator for SSRC registration
        try:
            # Shared state file with Analytics Service
            state_file = self.output_dir / 'state' / 'timing_calibration.json'
            self.calibrator = TimingCalibrator(
                data_root=self.output_dir,
                sample_rate=20000, # Default, will be updated if needed
                state_file=state_file
            )
            logger.info(f"Initialized TimingCalibrator for SSRC tracking: {state_file}")
        except Exception as e:
            logger.error(f"Failed to initialize TimingCalibrator: {e}")
            self.calibrator = None
        
        # NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
        # The recorder now always archives immediately. MetrologyEngine's fusion_state
        # handles timing lock internally using wider search windows until locked.
        # The bootstrap_enabled config option is now ignored.
        
        # Status tracking
        self.start_time = time.time()
        self.status_file = self.output_dir / 'status' / 'core-recorder-status.json'
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        
        # CHU FSK Listener (USB channels for FSK decode, no archive)
        # Graceful shutdown
        self.running = False
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def run(self):
        """Main run loop."""
        self.running = True
        
        logger.info("Starting hf-timestd core recorder v2 (using ka9q-python RadiodStream)")
        
        # NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
        # Recorder always archives immediately. MetrologyEngine handles timing lock.
        logger.info("Archiving mode: IMMEDIATE (MetrologyEngine handles timing lock)")
        
        # Ensure channels exist and get ChannelInfo
        if not self._initialize_channels():
            logger.error("Failed to initialize channels - exiting")
            return
        
        logger.info(f"Channels initialized: {len(self.channel_specs)} specs, {len(self.recorders)} recorders")
        self.running = True
        
        # Initialize tiered storage if enabled
        tiered_enabled = self.recorder_config.get('tiered_storage', False)
        
        logger.info(f"Tiered storage: {'enabled' if tiered_enabled else 'disabled'}")
        
        if tiered_enabled:
            try:
                from .tiered_storage import TieredStorageConfig, TieredStorageManager
                
                num_channels = len(self.channel_specs)
                hot_buffer_root = self.recorder_config.get('hot_buffer_root', '/dev/shm/timestd')
                
                logger.info(f"Initializing tiered storage: {num_channels} channels, "
                           f"hot_buffer={hot_buffer_root}")
                
                # Fixed 5-minute retention for real-time analytics/fusion pipeline
                tiered_config = TieredStorageConfig(
                    hot_buffer_root=Path(hot_buffer_root),
                    cold_buffer_root=Path(self.output_dir),
                    auto_configure=False,
                    hot_minutes=5,
                    num_channels=num_channels,
                )
                
                from .tiered_storage import _manager
                global _manager
                tiered_manager = TieredStorageManager(tiered_config)
                _manager = tiered_manager
                tiered_manager.start()
                
                logger.info(f"✓ Tiered storage ACTIVE: hot_minutes={tiered_manager.hot_minutes}")
            except Exception as e:
                logger.error(f"Failed to initialize tiered storage: {e}", exc_info=True)
                logger.warning("Continuing without tiered storage - files will accumulate in hot buffer!")
        else:
            logger.info("Tiered storage: disabled (files written directly to disk)")
        
        # Start all recorders
        for key, recorder in self.recorders.items():
            recorder.start()
            logger.info(f"Started recorder for {freq/1e6:.3f} MHz ({recorder.config.description})")

            # Register SSRC now that recorder is started and SSRC is resolved
            if self.calibrator:
                try:
                    if recorder.config.ssrc:
                        self.calibrator.register_channel_ssrc(recorder.config.description, recorder.config.ssrc)
                        logger.info(f"Registered SSRC {recorder.config.ssrc:x} for {recorder.config.description}")
                    else:
                        logger.warning(f"Recorder {recorder.config.description} started but has no SSRC")
                except Exception as e:
                    logger.warning(f"Failed to register SSRC for {key}: {e}")
        
        logger.info("Core recorder running. Press Ctrl+C to stop.")
        
        # Notify systemd we're ready
        if SYSTEMD_AVAILABLE:
            systemd_daemon.notify('READY=1')
            logger.info("Notified systemd: READY")
        
        # Write initial status
        self._write_status()
        
        # Initialize quota manager
        try:
            quota_str = str(self.recorder_config.get('storage_quota', '75%'))
            quota_percent = float(quota_str.rstrip('%'))
            logger.info(f"Initializing QuotaManager with threshold {quota_percent}%")
        except ValueError:
            logger.warning("Invalid storage_quota format, using default 75%")
            quota_percent = 75.0

        self.quota_manager = QuotaManager(
            data_root=self.output_dir,
            threshold_percent=quota_percent,
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
                    
                    # Notify systemd watchdog (service is alive)
                    if SYSTEMD_AVAILABLE:
                        systemd_daemon.notify('WATCHDOG=1')
                
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
    
    @staticmethod
    def _resolve_encoding(encoding_val) -> int:
        """Map encoding string or value to Encoding constant."""
        if isinstance(encoding_val, str):
            return {
                'F32': Encoding.F32,
                'S16LE': Encoding.S16LE,
                'OPUS': Encoding.OPUS,
            }.get(encoding_val.upper(), Encoding.NO_ENCODING)
        return encoding_val

    def _initialize_channels(self) -> bool:
        """
        Initialize all channels via a single unified path.

        Every [[recorder.channels]] entry — whether an IQ archive channel or a
        CHU FSK USB channel — is provisioned through RobustManagedStream /
        ensure_channel().  The only difference is the 'consumer' field:

          (absent / "archive")  ->  StreamRecorderV2  (archives to disk)
          (legacy chu_fsk removed)

        This means radiod-restart recovery, encoding, filter edges, and
        ensure_channel() semantics are identical for every channel.
        """
        try:
            if not self.channel_specs:
                logger.warning("No channels configured")
                return False

            expanded_specs = []
            if self.engine_type == 'phase-engine':
                logger.info("PhaseEngine mode enabled: expanding SHARED channels into WWV, WWVH, BPM")
                for spec in self.channel_specs:
                    freq = int(spec['frequency_hz'])
                    desc = spec.get('description', '')
                    # If this is a SHARED channel (or one of the standard shared frequencies)
                    # We create 3 separate recorders for PhaseEngine
                    if freq in [2500000, 5000000, 10000000, 15000000] and desc.startswith('SHARED'):
                        for target in ['WWV', 'WWVH', 'BPM']:
                            new_spec = spec.copy()
                            new_spec['description'] = f"{target}_{freq//1000}"
                            new_spec['target'] = target
                            expanded_specs.append(new_spec)
                    else:
                        expanded_specs.append(spec)
            else:
                expanded_specs = self.channel_specs
                
            self.channel_specs = expanded_specs

            logger.info(f"Initializing {len(self.channel_specs)} configured channels...")

            for ch_spec in self.channel_specs:
                freq = int(ch_spec['frequency_hz'])

                # Merge per-channel overrides with defaults
                preset      = ch_spec.get('preset',    self.channel_defaults.get('preset', 'iq'))
                sample_rate = ch_spec.get('sample_rate', self.channel_defaults.get('sample_rate'))
                if sample_rate is None:
                    raise ValueError(f"No sample_rate for {freq} and no default")
                encoding = self._resolve_encoding(
                    ch_spec.get('encoding', self.channel_defaults.get('encoding', Encoding.F32))
                )
                agc_val  = int(ch_spec.get('agc',  self.channel_defaults.get('agc',  0)))
                gain_val = float(ch_spec.get('gain', self.channel_defaults.get('gain', 0.0)))
                low_edge  = ch_spec.get('low_edge',  self.channel_defaults.get('low_edge'))
                high_edge = ch_spec.get('high_edge', self.channel_defaults.get('high_edge'))
                description = ch_spec.get('description', f"{freq/1e6:.3f} MHz")
                logger.info(f"Provisioning {description} ({freq/1e6:.3f} MHz) "
                            f"preset={preset} sr={sample_rate}")

                rec_config = StreamRecorderConfig(
                    ssrc=None,
                    frequency_hz=freq,
                    encoding=encoding,
                    agc_enable=agc_val,
                    gain=gain_val,
                    description=description,
                    preset=preset,
                    sample_rate=sample_rate,
                    output_dir=self.output_dir,
                    receiver_grid=self.station_config.get('grid_square', ''),
                    station_config=self.station_config,
                    raw_buffer_file_duration_sec=3600,
                    tiered_storage=self.recorder_config.get('tiered_storage', False),
                    hot_buffer_root=Path(self.recorder_config.get('hot_buffer_root'))
                        if self.recorder_config.get('hot_buffer_root') else None,
                    compression=self.recorder_config.get('compression', 'none'),
                    compression_level=self.recorder_config.get('compression_level', 3),
                    use_digital_rf=self.recorder_config.get('save_digital_rf', False),
                    destination=None,
                    low_edge=float(low_edge) if low_edge is not None else None,
                    high_edge=float(high_edge) if high_edge is not None else None,
                    reception_mode=ch_spec.get('reception_mode'),
                    target=ch_spec.get('target'),
                    null_targets=ch_spec.get('null_targets'),
                    combining_method=ch_spec.get('combining_method'),
                )
                recorder = StreamRecorderV2(
                    config=rec_config,
                    control=self.control,
                )
                self.recorders[desc] = recorder

            logger.info(f"✓ Initialized {len(self.recorders)} archive recorders")
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
            
            for key, recorder in self.recorders.items():
                ch_stats = recorder.get_status()
                # Use SSRC as key if known, otherwise use hex frequency
                ssrc = recorder.config.ssrc
                key = hex(ssrc) if ssrc and ssrc != 0 else f"freq_{freq}"
                
                # Add metadata to ch_stats for better UI/debugging
                ch_stats['preset'] = recorder.config.preset
                ch_stats['encoding'] = recorder.config.encoding
                
                status['channels'][stats_key] = ch_stats
                
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
        for key, recorder in self.recorders.items():
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
    
    # NOTE (2026-02-03): Bootstrap methods removed - functionality migrated to MetrologyEngine.
    # Removed: _on_bootstrap_provisional_lock, _on_bootstrap_full_lock,
    #          _update_bootstrap_state_if_locked, _write_bootstrap_timing_reference
    
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
        """Monitor stream health and data freshness."""
        try:
            # Check individual channel health
            for key, recorder in self.recorders.items():
                if not recorder.is_healthy():
                    silence = recorder.get_silence_duration()
                    logger.warning(
                        f"Channel {recorder.config.description} silent for {silence:.0f}s"
                    )
                    # StreamRecorderV2's health monitor will handle channel recreation
            
            # DATA FRESHNESS CHECK: Verify output files are being written
            # This catches silent failures where process runs but doesn't write data
            self._check_data_freshness()
            
        except Exception as e:
            logger.error(f"Health monitoring error: {e}")
    
    def _check_data_freshness(self):
        """Check that raw buffer files are being written recently.
        
        This catches failure modes where:
        - RTP clock is frozen/drifted
        - Disk is full
        - Permissions issues
        - Silent processing failures
        
        If data is stale for >10 minutes, triggers a self-restart via sys.exit(1).
        Systemd will restart the service automatically.
        """
        try:
            from datetime import datetime, timedelta
            
            # Check hot buffer (tiered storage) or cold buffer
            hot_buffer = Path('/dev/shm/timestd/raw_buffer')
            cold_buffer = self.output_dir / 'raw_buffer'
            
            search_path = hot_buffer if hot_buffer.exists() else cold_buffer
            if not search_path.exists():
                return
            
            # Find most recent .bin or .bin.zst file
            # Check today and yesterday (for just-after-midnight edge case)
            now = datetime.now(timezone.utc)
            dates_to_check = [
                now.strftime('%Y%m%d'),
                (now - timedelta(days=1)).strftime('%Y%m%d')
            ]
            
            latest_mtime = 0
            latest_file = None
            
            for channel_dir in search_path.iterdir():
                if not channel_dir.is_dir():
                    continue
                for date_str in dates_to_check:
                    day_dir = channel_dir / date_str
                    if not day_dir.exists():
                        continue
                    for f in day_dir.glob('*.bin*'):
                        try:
                            mtime = f.stat().st_mtime
                            if mtime > latest_mtime:
                                latest_mtime = mtime
                                latest_file = f
                        except (OSError, IOError):
                            continue
            
            if latest_file is None:
                # No files found - service may be starting up
                # Only alert if we've been running for a while
                uptime = time.time() - self.start_time
                if uptime > 300:  # 5 minutes
                    logger.error(
                        f"DATA FRESHNESS CRITICAL: No raw buffer files found after {uptime:.0f}s uptime!"
                    )
                return
            
            file_age = time.time() - latest_mtime
            
            # Alert if no new files in 5 minutes (300 seconds)
            if file_age > 300:
                logger.error(
                    f"DATA FRESHNESS WARNING: No new raw buffer files in {file_age:.0f}s! "
                    f"Latest: {latest_file.name} ({file_age/60:.1f} min old). "
                    f"Check for RTP clock drift, disk full, or processing errors."
                )
            
            # CRITICAL: Trigger self-restart if stale for >10 minutes
            # This ensures automatic recovery from silent failures
            if file_age > 600:  # 10 minutes
                logger.critical(
                    f"DATA FRESHNESS CRITICAL: No new data in {file_age:.0f}s ({file_age/60:.1f} min). "
                    f"Triggering self-restart to recover. Latest file: {latest_file}"
                )
                # Exit with error code - systemd will restart us (Restart=always)
                self.running = False
                sys.exit(1)
                
        except Exception as e:
            logger.debug(f"Data freshness check error: {e}")
    
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
        for key, recorder in self.recorders.items():
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
                logger.error(f"Error stopping recorder for channel {key}: {e}")
        

        # Close RadiodControl
        try:
            self.control.close()
        except Exception:
            pass
        
        # Write final status
        self._write_status()
        
        logger.info("Core recorder stopped")




def _expand_channel_groups(recorder_section: dict) -> list:
    """
    Expand [recorder.channel_group.<name>] into a flat list of channel specs.

    Each group table supplies group-level defaults (preset, sample_rate, agc,
    gain, encoding, archive, consumer, …).  Per-channel entries in
    [[recorder.channel_group.<name>.channels]] inherit those defaults and may
    override any key individually.

    Also accepts the legacy [[recorder.channels]] flat list for backward
    compatibility — those entries are appended unchanged.
    """
    channels = []

    # New schema: channel_group.<name>
    for group_name, group in recorder_section.get('channel_group', {}).items():
        group_defaults = {k: v for k, v in group.items() if k != 'channels'}
        for ch in group.get('channels', []):
            merged = dict(group_defaults)
            merged.update(ch)
            merged.setdefault('group', group_name)
            channels.append(merged)

    # Legacy schema: [[recorder.channels]]
    for ch in recorder_section.get('channels', []):
        channels.append(ch)

    return channels


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
    channels = _expand_channel_groups(recorder_section)
    recorder_config = {
        'output_dir': output_dir,
        'station': config.get('station', {}),
        'recorder': recorder_section,
        'channels': channels,
        'channel_defaults': recorder_section.get('channel_defaults', {}),
        'status_address': config.get('ka9q', {}).get('status_address', '239.192.152.141'),
    }
    
    logger.info(f"Loaded {len(recorder_config['channels'])} channels from config")
    
    # Run recorder
    recorder = CoreRecorderV2(recorder_config)
    recorder.run()


if __name__ == '__main__':
    main()
