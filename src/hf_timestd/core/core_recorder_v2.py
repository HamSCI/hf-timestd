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
from .stream_recorder_v2 import StreamRecorderV2, StreamRecorderConfig
from .quality_snapshot import QualitySnapshotWriter
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
    except Exception as e:
        logger.debug(f"Caught exception: {e}")
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
        
        # Determine engine type: check ka9q.source first, then recorder.engine
        ka9q_section = config.get('ka9q', {})
        self.recorder_config = config.get('recorder', {})
        self.engine_type = (
            ka9q_section.get('source')
            or self.recorder_config.get('engine', 'radiod')
        )
        if self.engine_type not in ('radiod', 'phase-engine'):
            logger.warning(f"Unknown engine type '{self.engine_type}', defaulting to 'radiod'")
            self.engine_type = 'radiod'

        # Resolve the status/control address.
        # When source is 'phase-engine', use its status multicast address
        # (from ka9q.phase_engine_status or the well-known default 239.99.1.1)
        # instead of the radiod status address.
        if self.engine_type == 'phase-engine':
            self.status_address = ka9q_section.get(
                'phase_engine_status', '239.99.1.1'
            )
            logger.info(f"Engine type is phase-engine, using status address: {self.status_address}")
        else:
            self.status_address = config.get('status_address')
            if not self.status_address:
                self.status_address = ka9q_section.get('status_address')

        if not self.status_address:
            raise ValueError("Configuration missing 'status_address' in [ka9q] section")

        # Try to resolve status address, falling back to discovery if needed
        from ka9q.utils import resolve_multicast_address
        try:
            resolve_multicast_address(self.status_address, timeout=2.0)
        except Exception:
            logger.warning(f"Failed to resolve configured address '{self.status_address}', attempting auto-discovery...")
            from ka9q.discovery import discover_radiod_services

            services = discover_radiod_services(timeout=5.0)
            if not services:
                logger.error("Discovery failed: No radiod services found!")
            else:
                logger.info(f"Discovered {len(services)} radiod services: {[s['name'] for s in services]}")
                selected = None
                for s in services:
                    if self.status_address.replace('.local', '') in s['name'] or self.status_address in s['name']:
                        selected = s
                        break
                if not selected:
                    selected = services[0]
                if selected:
                    logger.warning(f"Redirecting to discovered service: '{selected['name']}' at {selected['address']}")
                    self.status_address = selected['address']

        self.control = RadiodControl(self.status_address)

        # Station config
        self.station_config = config.get('station', {})

        # Contract v0.3 §7: ka9q-python owns data-multicast derivation.
        # Clients do not compute or pass destination=; ka9q-python assigns
        # it deterministically and returns the resolved address in
        # ChannelInfo.  Deprecated override keys are warned about but
        # still honored for rollback (remove in v8.0.0).
        ka9q_cfg = config.get('ka9q', {}) or {}
        deprecated_override = (ka9q_cfg.get('data_destination')
                               or config.get('radiod_multicast_group'))
        if deprecated_override:
            logger.warning(
                "config key [ka9q].data_destination / radiod_multicast_group "
                "is deprecated under contract v0.3 §7; ka9q-python now "
                "derives the multicast group.  Ignoring override "
                f"{deprecated_override!r}."
            )
        self.data_destination = None  # filled from ChannelInfo at runtime
        
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

        # Shared-MultiStream rollout flag (plan: tasks/todo.md).  When
        # true, _initialize_channels() registers every archive channel
        # on a single MultiStream that owns one UDP socket for the
        # whole service, instead of every StreamRecorderV2 owning its
        # own RadiodStream + socket.  Default false during the
        # step-by-step rollout; the flag flips once steps 1-6 land
        # and step-7 verification on bee1 confirms the timing chain
        # is preserved.  Operator can keep it false to roll back.
        self._use_shared_multistream = bool(
            self.recorder_config.get('shared_multistream', False)
        )
        # Populated in _initialize_channels when shared mode is on.
        self._multi = None

        # T6 BPSK PPS chain-delay calibrator
        # Uses a bare RadiodStream (no archive writer) — the BPSK channel
        # exists only to feed the calibrator, not for storage.
        # NOTE: the public terminology was renamed L6→T6 (T-level authority
        # tier; see authority_manager.T_LEVELS_RANKED). The config section
        # is still ``[timing.l6_pps]`` so existing /etc/hf-timestd/timestd-
        # config.toml files keep working — rename the section in a
        # deploy-coordinated commit.
        self._t6_calibrator = None
        self._t6_stream = None  # RadiodStream for BPSK channel
        # T6 channel's ChannelInfo — saved during _start_t6_stream so that
        # rtp_to_wallclock can compute wall-time of detected edges for the
        # TSL3 SHM feed below.
        self._t6_channel_info = None
        # SHM unit 2 (TSL3): direct BPSK PPS feed to chrony.  Bypasses
        # fusion's tick-detection uncertainty so chrony can see BPSK
        # precision at its own quantization-limited floor (~31 us at
        # 16 kHz) rather than the HF-fusion floor (~150 us).
        self._t6_shm = None
        self._t6_last_pushed_rtp = None
        # Wrap-rejection guard: the BPSK calibrator algorithm has a known
        # cascade where a noise edge near the half-second mark from a real
        # edge displaces the reference and causes chain_delay to wrap by
        # ~half a second (62.5 us natural sample wobble vs 322 ms wrap).
        # We track the last accepted chain_delay and reject jumps > 1 ms,
        # keeping the previously-good correction in place. Reset to None
        # on calibrator restart so the first stable lock is always accepted.
        self._t6_last_chain_delay_ns = None
        self._t6_wrap_rejections = 0
        # Set at first stable lock from system-clock comparison; constant
        # offset added to every calibrator chain_delay report so all
        # measurements share a common disambiguated reference frame.
        self._t6_disambiguation_ns = 0
        self._t6_config = config.get('timing', {}).get('l6_pps', {})
        if self._t6_config.get('enabled', False):
            freq_hz = self._t6_config.get('frequency_hz')
            if freq_hz is None:
                logger.error("timing.l6_pps.enabled=true but frequency_hz not set — T6 disabled")
            else:
                sr = int(self._t6_config.get('sample_rate',
                         self.channel_defaults.get('sample_rate', 24000)))
                # Calibrator selection. The matched-filter calibrator
                # (textbook Costas + integrate-and-dump MF) replaces the
                # legacy per-sample-Δφ heuristic; it expects a wider
                # channel filter (±25 kHz at 96 kHz SR) for full benefit.
                # Default False to keep deployed behaviour unchanged
                # until a config bump explicitly opts in.
                if self._t6_config.get('use_matched_filter', False):
                    from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF
                    self._t6_calibrator = BpskPpsCalibratorMF(
                        sample_rate=sr,
                        consecutive_required=self._t6_config.get('consecutive_required', 10),
                        edge_tolerance_samples=self._t6_config.get('edge_tolerance_samples', 30),
                        costas_loop_bw_hz=self._t6_config.get('costas_loop_bw_hz', 1.0),
                    )
                    logger.info(f"T6 BPSK PPS calibrator (matched-filter) initialized: "
                                f"freq={freq_hz/1e6:.6f} MHz, sr={sr}")
                else:
                    from hf_timestd.core.bpsk_pps_calibrator import BpskPpsCalibrator
                    self._t6_calibrator = BpskPpsCalibrator(
                        sample_rate=sr,
                        consecutive_required=self._t6_config.get('consecutive_required', 10),
                        edge_tolerance_samples=self._t6_config.get('edge_tolerance_samples', 10),
                        enable_notch_500hz=self._t6_config.get('filter_500hz_notch', False),
                    )
                    logger.info(f"T6 BPSK PPS calibrator (legacy) initialized: "
                                f"freq={freq_hz/1e6:.6f} MHz, sr={sr}")
                # Init TSL3 SHM feed (unit 2). Failure is non-fatal —
                # calibration still drives chain_delay_correction_ns.
                try:
                    from hf_timestd.core.chrony_shm import ChronySHM
                    self._t6_shm = ChronySHM(unit=2)
                    if self._t6_shm.connect():
                        logger.info("T6 TSL3 SHM feed enabled (unit=2)")
                    else:
                        logger.warning("T6 TSL3 SHM unit=2 connect failed; "
                                       "TSL3 disabled (chain_delay_correction "
                                       "still applied to channels)")
                        self._t6_shm = None
                except Exception as e:
                    logger.warning(f"T6 TSL3 SHM init failed: {e}")
                    self._t6_shm = None
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
        # Watchdog and freshness counters — initialized here so _data_is_flowing()
        # and _check_data_freshness() never see an AttributeError on first call.
        self._wd_last_written: int = 0
        self._wd_last_advance: float = self.start_time
        self._freshness_last_written: int = 0
        self._freshness_last_advance: float = self.start_time
        # Per-channel write progress tracking — detects single-channel stalls
        # where RTP data arrives but archive writes stop (no GPS_TIME, disk full, etc.)
        self._per_channel_last_written: Dict[str, int] = {}
        self._per_channel_last_advance: Dict[str, float] = {}
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
                tiered_hot_minutes = self.recorder_config.get('tiered_hot_minutes')
                tiered_ram_percent = self.recorder_config.get('tiered_ram_percent')
                if tiered_ram_percent is None:
                    tiered_ram_percent = self.recorder_config.get('ram_percent')
                
                logger.info(f"Initializing tiered storage: {num_channels} channels, "
                           f"hot_buffer={hot_buffer_root}")
                
                tiered_config = TieredStorageConfig(
                    hot_buffer_root=Path(hot_buffer_root),
                    cold_buffer_root=Path(self.output_dir),
                    auto_configure=(tiered_hot_minutes is None),
                    hot_minutes=int(tiered_hot_minutes) if tiered_hot_minutes is not None else 5,
                    ram_percent=float(tiered_ram_percent) if tiered_ram_percent is not None else TieredStorageConfig.ram_percent,
                    num_channels=num_channels,
                )
                
                from . import tiered_storage
                tiered_manager = TieredStorageManager(tiered_config)
                tiered_storage._manager = tiered_manager
                tiered_manager.start()
                
                logger.info(f"✓ Tiered storage ACTIVE: hot_minutes={tiered_manager.hot_minutes}")
            except Exception as e:
                logger.critical(
                    f"Failed to initialize tiered storage: {e}. "
                    f"Cannot continue — without cold migration, the hot buffer "
                    f"({hot_buffer_root}) will fill tmpfs and cause silent data loss. "
                    f"Fix the tiered storage config or set tiered_storage=false to "
                    f"write directly to disk.",
                    exc_info=True
                )
                return  # Fatal — let systemd restart (and alert via OnFailure)
        else:
            logger.info("Tiered storage: disabled (files written directly to disk)")
        
        # Start all recorders.  In shared-MultiStream mode, channels
        # were already provisioned in _initialize_channels() via
        # register_with() — skip the per-channel start path.  Calibrator
        # SSRC registration also moved into _initialize_channels for
        # shared mode (it needs ssrc to be populated, which register_with
        # does).  The legacy path is preserved verbatim below for
        # rollback safety.
        if not self._use_shared_multistream:
            for key, recorder in self.recorders.items():
                recorder.start()
                logger.info(f"Started recorder for {recorder.config.frequency_hz/1e6:.3f} MHz ({recorder.config.description})")

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
        
        # Start T6 BPSK PPS stream (bare RadiodStream, no archive).  In
        # shared mode this just adds the channel to self._multi; the
        # actual receive loop kicks off below in self._multi.start().
        if self._t6_calibrator is not None:
            self._start_t6_stream()

        # Begin receiving on the shared MultiStream now that every
        # channel (archive + T6) is queued via add_channel — ka9q-python
        # requires all add_channel calls to precede start() for one
        # consistent multicast-group bind.
        if self._use_shared_multistream and self._multi is not None:
            try:
                self._multi.start()
                channel_count = len(self.recorders) + (
                    1 if self._t6_calibrator is not None else 0
                )
                logger.info(
                    f"Shared MultiStream started: 1 UDP socket serving "
                    f"{channel_count} SSRC-demuxed channels"
                )
            except Exception as e:
                logger.error(
                    f"Failed to start shared MultiStream: {e}", exc_info=True,
                )
                return

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

        archive_root = self.recorder_config.get('archive_root')
        if archive_root:
            archive_root = Path(archive_root)
            logger.info(f"QuotaManager archive root: {archive_root}")

        derived_max_days = int(self.recorder_config.get('derived_max_days', 7))
        logger.info(f"QuotaManager derived_max_days: {derived_max_days}")

        self.quota_manager = QuotaManager(
            data_root=self.output_dir,
            threshold_percent=quota_percent,
            min_days_to_keep=7,
            dry_run=False,
            archive_root=archive_root,
            derived_max_days=derived_max_days,
        )
        
        # Quality snapshot writer — surfaces per-recorder StreamQuality
        # to /run/hf-timestd/quality.json for sigmond's `hf-timestd
        # quality --json` CLI to read.  Intentionally driven from the
        # main loop (not a thread) so a hung loop produces a stale
        # snapshot, which sigmond uses as a daemon-health signal.
        quality_writer = QualitySnapshotWriter(self.recorders)

        # Main loop
        last_status_time = 0
        last_health_check = 0
        last_quota_check = 0
        last_quality_tick = 0

        try:
            while self.running:
                time.sleep(1)
                now = time.time()

                # Update NTP status (every 10 seconds)
                if now - last_status_time >= 10:
                    self._update_ntp_status()
                    self._write_status()
                    last_status_time = now
                    
                    # Notify systemd watchdog — conditional on data flow.
                    # Only pet the watchdog if samples have been written
                    # recently.  If the recorder is alive but no data is
                    # flowing, systemd will kill and restart us after
                    # WatchdogSec expires (180s).  During the first 5 min
                    # of startup we always pet (channels are initializing).
                    if SYSTEMD_AVAILABLE:
                        uptime = now - self.start_time
                        if uptime < 300 or self._data_is_flowing():
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

                # Quality snapshot for sigmond (every 5 seconds)
                if now - last_quality_tick >= 5:
                    quality_writer.tick()
                    last_quality_tick = now
        
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
                'S16BE': Encoding.S16BE,
                'S16LE': Encoding.S16LE,
                'F32': Encoding.F32,
                'F32LE': Encoding.F32LE,
                'F32BE': Encoding.F32BE,
                'F16': Encoding.F16,
                'F16LE': Encoding.F16LE,
                'F16BE': Encoding.F16BE,
                'OPUS': Encoding.OPUS,
            }.get(encoding_val.upper(), Encoding.NO_ENCODING)
        return encoding_val

    def _initialize_channels(self) -> bool:
        """
        Initialize all channels via a single unified path.

        Every [[recorder.channels]] entry is provisioned through
        ensure_channel() and archived to disk
        via StreamRecorderV2.
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

            # Compute ring-buffer depth once for all channels using the
            # same RAM-budget policy the tiered-storage hot buffer used.
            # The ring minimum (MIN_HOT_MINUTES) now starts at 4 to give
            # metrology workers at least a couple of minutes of headroom
            # past the longest file-chunk duration.  Phase 1 is additive:
            # ring_seconds > 0 just means "also publish into the ring";
            # nothing reads from it yet.
            file_duration_sec = int(self.recorder_config.get('file_duration_sec', 600))
            ring_enabled = bool(self.recorder_config.get('ring_buffer', True))
            if ring_enabled:
                from .tiered_storage import calculate_hot_minutes
                hot_minutes = calculate_hot_minutes(
                    num_channels=len(self.channel_specs),
                    ram_percent=float(self.recorder_config.get('ring_ram_percent', 20)),
                    file_duration_sec=file_duration_sec,
                )
                ring_seconds = hot_minutes * 60
                logger.info(
                    f"Ring buffer enabled: {hot_minutes} minutes "
                    f"({ring_seconds}s) per channel × {len(self.channel_specs)} channels"
                )
            else:
                ring_seconds = 0
                logger.info("Ring buffer disabled (recorder.ring_buffer = false)")

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

                # Per-channel archive control: defaults to group/global setting,
                # overridable per-channel.  When False, core-recorder still
                # receives the stream (for metrology hot-buffer, T6 calibration,
                # tap consumers) but writes no IQ data to cold storage.
                archive = ch_spec.get('archive',
                                      self.recorder_config.get('archive', True))

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
                    file_duration_sec=self.recorder_config.get('file_duration_sec', 600),
                    use_digital_rf=self.recorder_config.get('save_digital_rf', False),
                    destination=self.data_destination,
                    low_edge=float(low_edge) if low_edge is not None else None,
                    high_edge=float(high_edge) if high_edge is not None else None,
                    reception_mode=ch_spec.get('reception_mode'),
                    target=ch_spec.get('target'),
                    null_targets=ch_spec.get('null_targets'),
                    combining_method=ch_spec.get('combining_method'),
                    archive=archive,
                    ring_seconds=ring_seconds,
                )
                recorder = StreamRecorderV2(
                    config=rec_config,
                    control=self.control,
                )
                self.recorders[description] = recorder

            logger.info(f"✓ Initialized {len(self.recorders)} archive recorders")

            # Shared-MultiStream wiring (recorder.shared_multistream = true):
            # build one MultiStream that all archive channels register on
            # via register_with(), so the kernel only clones each radiod
            # multicast packet ONCE for this service instead of N times.
            # multi.start() is intentionally deferred — the T6 BPSK PPS
            # channel will also be added (by _start_t6_stream() in shared
            # mode) and the parent run() flow starts the multi after both
            # additions complete, per ka9q-python's add-before-start
            # API contract.
            if self._use_shared_multistream:
                from ka9q import MultiStream
                # samples_per_packet=200 / resequence_buffer_size=128 match
                # the legacy per-channel RadiodStream construction in
                # stream_recorder_v2._create_channel and _start_t6_stream.
                # Mismatch here would skew the resequencer's gap-detection
                # heuristics on hf-timestd's 24 kHz IQ channels.
                self._multi = MultiStream(
                    control=self.control,
                    samples_per_packet=200,
                    resequence_buffer_size=128,
                )
                for description, recorder in self.recorders.items():
                    try:
                        recorder.register_with(self._multi)
                        logger.info(
                            f"Registered {description} on shared MultiStream "
                            f"(SSRC {recorder.config.ssrc:08x})"
                        )
                        # Calibrator SSRC registration happens here in
                        # shared mode (legacy mode does it in run() after
                        # recorder.start()).  ssrc is populated by
                        # register_with -> ensure_channel.
                        if self.calibrator and recorder.config.ssrc:
                            try:
                                self.calibrator.register_channel_ssrc(
                                    description, recorder.config.ssrc
                                )
                                logger.info(
                                    f"Registered SSRC {recorder.config.ssrc:x} "
                                    f"with calibrator for {description}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to register SSRC for {description}: {e}"
                                )
                    except Exception as e:
                        logger.error(
                            f"Failed to register {description} on shared "
                            f"MultiStream: {e}", exc_info=True,
                        )
                        return False
                logger.info(
                    f"✓ {len(self.recorders)} channels registered on shared "
                    f"MultiStream (multi.start deferred)"
                )

            return True
        except Exception as e:
            logger.error(f"Failed to initialize channels: {e}", exc_info=True)
            return False

    def _start_t6_stream(self):
        """Provision the BPSK PPS channel (no archive).

        In shared-MultiStream mode the channel registers on
        ``self._multi`` alongside the archive channels — one socket for
        the whole service.  In legacy mode it owns its own
        ``RadiodStream`` (and its own UDP socket) as it always has.
        """
        from ka9q import RadiodStream, Encoding
        from ka9q.types import StatusType

        t6 = self._t6_config
        freq_hz = int(t6['frequency_hz'])
        sr = int(t6.get('sample_rate',
                        self.channel_defaults.get('sample_rate', 24000)))
        desc = t6.get('description', 'BPSK_PPS')
        # Optional channel filter overrides — None means use the iq
        # preset's defaults (±5 kHz). The matched-filter calibrator
        # benefits from a wider channel filter (±25 kHz) since σ_t
        # scales as 1/B for a band-limited polarity-flip step. Requires
        # ka9q-python ≥3.11 for low_edge/high_edge plumbing through
        # add_channel / ensure_channel.
        low_edge_hz = t6.get('low_edge_hz')
        high_edge_hz = t6.get('high_edge_hz')

        if self._use_shared_multistream:
            if self._multi is None:
                logger.error(
                    "T6 BPSK PPS shared-mode requested but self._multi is None — "
                    "shared-mode init must run before _start_t6_stream"
                )
                return
            try:
                # Add the T6 channel to the shared MultiStream.  add_channel
                # internally calls ensure_channel; the returned ChannelInfo
                # comes from the same protocol roundtrip the legacy path uses,
                # so data_destination capture below is identical.
                channel_info = self._multi.add_channel(
                    frequency_hz=float(freq_hz),
                    preset='iq',
                    sample_rate=sr,
                    encoding=Encoding.F32,
                    agc_enable=False,
                    gain=0.0,
                    on_samples=self._t6_on_samples,
                    low_edge=low_edge_hz,
                    high_edge=high_edge_hz,
                )
                self._t6_channel_info = channel_info
                if self.data_destination is None and channel_info is not None:
                    self.data_destination = getattr(
                        channel_info, 'multicast_address', None
                    )
                    logger.info(
                        f"ka9q-python assigned data_destination "
                        f"{self.data_destination} for T6 channel"
                    )
                logger.info(
                    f"T6 BPSK PPS registered on shared MultiStream: "
                    f"{desc} at {freq_hz/1e6:.6f} MHz"
                )
            except Exception as e:
                logger.error(
                    f"Failed to register T6 BPSK PPS on shared MultiStream: {e}",
                    exc_info=True,
                )
            return

        # Legacy: dedicated RadiodStream + per-channel UDP socket.
        try:
            channel_info = self.control.ensure_channel(
                frequency_hz=freq_hz,
                preset='iq',
                sample_rate=sr,
                encoding=Encoding.F32,
                agc_enable=False,
                gain=0.0,
                low_edge=low_edge_hz,
                high_edge=high_edge_hz,
            )
            self._t6_channel_info = channel_info
            if self.data_destination is None and channel_info is not None:
                self.data_destination = getattr(channel_info, 'multicast_address', None)
                logger.info(
                    f"ka9q-python assigned data_destination "
                    f"{self.data_destination} for T6 channel"
                )
            self._t6_stream = RadiodStream(
                channel=channel_info,
                on_samples=self._t6_on_samples,
                samples_per_packet=200,
                resequence_buffer_size=128,
            )
            self._t6_stream.start()
            logger.info(f"T6 BPSK PPS stream started: {desc} at {freq_hz/1e6:.6f} MHz")
        except Exception as e:
            logger.error(f"Failed to start T6 BPSK PPS stream: {e}", exc_info=True)
            self._t6_stream = None

    # Maximum sigma of a non-T6 reference we'll trust for disambiguation.
    # Half-second wrap value is ~322 ms; we need our reference to be tighter
    # than that to be useful for disambiguating which side of the wrap is
    # correct.  250 ms gives margin without being unrealistically tight.
    T6_DISAMBIGUATION_MAX_SIGMA_MS = 250.0

    def _get_disambiguation_reference(self):
        """Return the highest-rank non-T6 timing-authority offset estimate.

        Walks the T-level hierarchy in descending rank order (T5 > T4 > T3),
        returning the first probe that publishes an offset_ms with sigma <
        ``T6_DISAMBIGUATION_MAX_SIGMA_MS``.  Returns
        ``(offset_ms, sigma_ms, tier_name)`` or ``None`` if no suitable
        reference is available.

        Used ONCE at first lock to resolve which integer GPS-second the
        BPSK edge belongs to (the per-channel-creation RTP-grid alignment
        is non-deterministic against GPS seconds — could be off by any
        integer-sample multiple). Once disambiguated, T6 trusts its own
        measurements; we do NOT continuously slew toward the reference.
        T6 is the highest-quality timing authority available — its edges
        come directly from the LB-1421 GPSDO via TS1, and the MF measures
        them at ~150 ns precision. Tracking lower-quality references
        (T3 fusion at ~100 µs, even T4 LAN GPS at ~10 µs) would only
        contaminate T6's precision.

        Reference order:
          - T5 (highest): on-host GPS+PPS chrony refclock, not wired
          - T4: LAN GPS+PPS via chrony tracking (chronyc tracking output)
          - T3: HF fusion via /run/hf-timestd/fusion_status.json
        """
        # T5: not yet wired (requires on-host GPS+PPS probe in core-recorder)

        # T4: chrony's tracking offset against the LAN GPS source.
        # `Last offset` from `chronyc tracking` is (true_time - local_time);
        # we want (system_clock - true_UTC) = -Last_offset.  RMS offset
        # is the appropriate sigma estimate.
        try:
            import subprocess
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                last_offset_sec = None
                rms_offset_sec = None
                for line in result.stdout.splitlines():
                    if line.startswith('Last offset'):
                        last_offset_sec = float(line.split(':', 1)[1].split()[0])
                    elif line.startswith('RMS offset'):
                        rms_offset_sec = float(line.split(':', 1)[1].split()[0])
                if last_offset_sec is not None and rms_offset_sec is not None:
                    # Sign convention: chrony's "Last offset" is true−local.
                    # We need system_clock − true_UTC = −Last_offset.
                    offset_ms = -last_offset_sec * 1000.0
                    sigma_ms = rms_offset_sec * 1000.0
                    if sigma_ms <= self.T6_DISAMBIGUATION_MAX_SIGMA_MS:
                        return offset_ms, sigma_ms, 'T4'
        except (FileNotFoundError, OSError,
                subprocess.SubprocessError, ValueError, IndexError) as e:
            logger.debug(f"T4 chrony tracking unavailable: {e}")

        # T3: fusion_status.json from timestd-fusion service (fallback).
        try:
            fusion_path = Path('/run/hf-timestd/fusion_status.json')
            data = json.loads(fusion_path.read_text())
            if data.get('schema') == 'v1':
                fusion = data.get('fusion') or {}
                if (fusion.get('available')
                        and fusion.get('kalman_state') in ('LOCKED', 'ACQUIRING')):
                    offset_ms = float(fusion['d_clock_fused_ms'])
                    sigma_ms = float(fusion['uncertainty_ms'])
                    if sigma_ms <= self.T6_DISAMBIGUATION_MAX_SIGMA_MS:
                        return offset_ms, sigma_ms, 'T3'
        except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError):
            pass

        return None

    def _t6_on_samples(self, samples, quality):
        """Sample callback for the BPSK PPS stream — feeds the calibrator."""
        # One-shot smoke log on the first batch so the journal records
        # whether quality.last_rtp_timestamp is flowing in shared mode.
        # Same hook helps confirm legacy-mode startup health.
        if not getattr(self, '_t6_first_sample_logged', False):
            mode = 'shared MultiStream' if self._use_shared_multistream else 'dedicated RadiodStream'
            logger.info(
                f"T6 BPSK PPS first samples: {mode}, "
                f"len={len(samples)}, "
                f"last_rtp_timestamp={getattr(quality, 'last_rtp_timestamp', None)}"
            )
            self._t6_first_sample_logged = True

        result = self._t6_calibrator.process_samples(
            samples, quality.last_rtp_timestamp
        )
        if result is not None and result.locked:
            # Wrap-rejection: refuse jumps > 10 ms from the last accepted
            # value. 10 ms is well above natural sample-quantization
            # wobble (62.5 us at 16 kHz) and well above legitimate
            # multi-sample drift in the calibrator's chosen edge
            # position (~2-5 ms typical over hours), but well below the
            # half-second wrap value (~322 ms) the algorithm produces
            # when a noise edge displaces the reference. The earlier
            # 1 ms threshold was too tight; observed-on-bee1 calibrator
            # drift of 2.5 ms in 30 min triggered constant rejections.
            WRAP_THRESHOLD_NS = 10_000_000
            if self._t6_last_chain_delay_ns is None:
                # First stable lock — disambiguate WHICH whole sample is the
                # real PPS edge by comparing against the system clock (now
                # disciplined by chrony / LAN GPS to <1us). The calibrator
                # picks one consistent edge position from possibly multiple
                # candidates (real PPS plus noise edges). The system clock
                # tells us which is the "right" sample, but the BPSK provides
                # sub-sample precision once disambiguated.
                #
                # Compute the integer-sample shift that would move corrected
                # wall_time into agreement with the highest-rank-available
                # non-T6 timing-authority tier (T5 > T4 > T3).  Per the
                # timing model, the system clock is downstream of the
                # authority hierarchy, not a peer source — using it for
                # disambiguation would be circular.  We use the explicit
                # tier that publishes an offset_ms.  Sigma sanity check:
                # reject any reference whose sigma is larger than the
                # half-second-wrap value we're trying to disambiguate
                # against (250 ms).  Lock the shift in as
                # _t6_disambiguation_ns; subsequent calibrator reports
                # are adjusted by the same constant so all measurements
                # share the same disambiguated reference frame.
                try:
                    last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
                    if last_edge_rtp is not None and self._t6_channel_info is not None:
                        ref = self._get_disambiguation_reference()
                        if ref is None:
                            logger.info(
                                f"T6 chain_delay initial accept: no usable "
                                f"non-T6 timing authority for disambiguation; "
                                f"accepting calibrator value as-is"
                            )
                        else:
                            ref_offset_ms, ref_sigma_ms, ref_tier = ref
                            # Compute raw wall-time of the detected edge
                            # WITHOUT ka9q applying chain_delay (kept None
                            # on ChannelInfo so the subtraction inside
                            # rtp_to_wallclock is a no-op).
                            self._t6_channel_info.chain_delay_correction_ns = None
                            from ka9q.rtp_recorder import rtp_to_wallclock
                            raw_wall_time_sec = rtp_to_wallclock(last_edge_rtp, self._t6_channel_info)
                            if raw_wall_time_sec is not None:
                                wall_time_sec = raw_wall_time_sec - (result.chain_delay_ns / 1e9)
                                ref_time = round(wall_time_sec)
                                offset_sec = wall_time_sec - ref_time
                                # The reference tier's offset_ms is its
                                # estimate of (system_clock - true_UTC).
                                # Our wall_time_offset is also that same
                                # quantity (modulo BPSK calibration error).
                                # Disagreement reveals the wrap.
                                disagreement_sec = offset_sec - (ref_offset_ms / 1000.0)
                                sr_local = self._t6_calibrator.sample_rate
                                shift_samples = round(disagreement_sec * sr_local)
                                self._t6_disambiguation_ns = int(round(
                                    shift_samples * 1e9 / sr_local
                                ))
                                if shift_samples != 0:
                                    logger.info(
                                        f"T6 chain_delay disambiguated against {ref_tier} "
                                        f"(offset={ref_offset_ms:+.3f} ms, "
                                        f"sigma={ref_sigma_ms:.3f} ms): raw="
                                        f"{result.chain_delay_ns} ns implied wall-time "
                                        f"offset {offset_sec*1000:+.3f} ms; disagreement "
                                        f"{disagreement_sec*1000:+.1f} ms; shifting "
                                        f"{shift_samples} samples ({self._t6_disambiguation_ns} ns)"
                                    )
                                else:
                                    logger.info(
                                        f"T6 chain_delay disambiguated against {ref_tier}: "
                                        f"already aligned within one sample "
                                        f"(disagreement {disagreement_sec*1000:+.3f} ms)"
                                    )
                except Exception as e:
                    logger.warning(f"T6 disambiguation failed: {e}")
                # Apply disambiguation to the chain_delay we lock in
                effective = result.chain_delay_ns + self._t6_disambiguation_ns
                self._t6_last_chain_delay_ns = effective
                effective_chain_delay = effective
                logger.info(
                    f"T6 chain_delay initial accept: {result.chain_delay_ns} ns "
                    f"(effective with disambiguation: {effective} ns)"
                )
            elif abs((result.chain_delay_ns + self._t6_disambiguation_ns) - self._t6_last_chain_delay_ns) > WRAP_THRESHOLD_NS:
                # Suspicious jump — log once per burst and use the
                # last-accepted chain_delay for downstream propagation.
                # Falling through (rather than `return`-ing early) keeps
                # TSL3's SHM updates flowing with the proven-good value
                # so chrony does not lose Reach during a wrap event.
                self._t6_wrap_rejections += 1
                if self._t6_wrap_rejections == 1 or self._t6_wrap_rejections % 60 == 0:
                    logger.warning(
                        f"T6 chain_delay jump rejected: "
                        f"new={result.chain_delay_ns} ns, "
                        f"last_accepted={self._t6_last_chain_delay_ns} ns, "
                        f"delta={result.chain_delay_ns - self._t6_last_chain_delay_ns} ns "
                        f"(threshold {WRAP_THRESHOLD_NS} ns); "
                        f"rejections={self._t6_wrap_rejections}"
                    )
                effective_chain_delay = self._t6_last_chain_delay_ns
            else:
                # Within tolerance — accept and update reference.
                # NOTE (2026-05-06): a previous version of this branch
                # continuously slewed `_t6_disambiguation_ns` toward T3
                # via a slow IIR.  Removed because it made T6 a
                # noise-reduced *follower* of T3 instead of an
                # independent timing authority.  T6 is the highest-
                # quality timing source available (LB-1421 GPSDO via
                # TS1, MF precision ~150 ns); it should NOT be tracked
                # against any lower-quality reference.  The one-shot
                # disambiguation at first lock is the only place we use
                # an external reference, and that's only to resolve
                # which integer GPS second the edge belongs to.
                self._t6_last_chain_delay_ns = result.chain_delay_ns + self._t6_disambiguation_ns
                self._t6_wrap_rejections = 0
                effective_chain_delay = self._t6_last_chain_delay_ns

            # Record BPSK metadata in archive sidecars.  Per the
            # architectural separation (chain_delay is metrology, not
            # transport), we no longer set chain_delay_correction_ns on
            # the recorder ChannelInfos — that would silently invoke
            # ka9q's rtp_to_wallclock subtraction.  Instead, we hand the
            # value to each archive writer to record in metadata; archive
            # wall_times stay raw (RTP-derived without chain_delay), and
            # downstream readers apply the correction if they want UTC
            # alignment.  Pre-2026-05 archives lacked this metadata field
            # and had chain_delay applied at write time — readers should
            # treat absence as "applied=True".
            for desc, recorder in self.recorders.items():
                writer = getattr(recorder, 'archive_writer', None)
                if writer is not None and hasattr(writer, 'set_bpsk_metadata'):
                    writer.set_bpsk_metadata(
                        chain_delay_ns=effective_chain_delay,
                        applied=False,
                    )

            # TSL3 SHM feed: push wall-time of detected edge to chrony so
            # it sees BPSK precision directly. Only push when the
            # calibrator has advanced to a NEW edge (once per second), and
            # only after wrap-rejection has accepted the chain_delay.
            if self._t6_shm is not None and self._t6_channel_info is not None:
                try:
                    last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
                    if last_edge_rtp is not None and last_edge_rtp != self._t6_last_pushed_rtp:
                        # Compute raw wall-time without ka9q applying
                        # chain_delay (chain_delay_correction_ns kept None
                        # on the T6 channel), then apply chain_delay
                        # manually as a metrology operation.
                        self._t6_channel_info.chain_delay_correction_ns = None
                        from ka9q.rtp_recorder import rtp_to_wallclock
                        raw_wall_time_sec = rtp_to_wallclock(last_edge_rtp, self._t6_channel_info)
                        if raw_wall_time_sec is not None:
                            wall_time_sec = raw_wall_time_sec - (effective_chain_delay / 1e9)
                            ref_time = round(wall_time_sec)
                            # precision -14 = 61 us, matches T6 sigma claim of 50 us
                            self._t6_shm.update(
                                reference_time=float(ref_time),
                                system_time=wall_time_sec,
                                precision=-14,
                            )
                            self._t6_last_pushed_rtp = last_edge_rtp
                except Exception as e:
                    # SHM push is non-fatal — log once per ~60 s of failures
                    if not getattr(self, '_t6_shm_warned', False):
                        logger.warning(f"T6 TSL3 SHM push failed: {e}")
                        self._t6_shm_warned = True

            # Log on first lock and periodically
            if result.pps_consecutive == self._t6_calibrator.consecutive_required:
                logger.info(
                    f"T6 BPSK PPS LOCKED: chain_delay={result.chain_delay_ns} ns "
                    f"({result.chain_delay_samples:.1f} samples), "
                    f"ok={result.pps_ok}, noise={result.pps_noise}"
                )
            elif result.pps_ok % 60 == 0:
                logger.debug(
                    f"T6 PPS: delay={result.chain_delay_ns} ns, "
                    f"consecutive={result.pps_consecutive}, "
                    f"ok={result.pps_ok}, noise={result.pps_noise}"
                )

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
                key = hex(ssrc) if ssrc and ssrc != 0 else f"freq_{recorder.config.frequency_hz}"
                
                # Add metadata to ch_stats for better UI/debugging
                ch_stats['preset'] = recorder.config.preset
                ch_stats['encoding'] = recorder.config.encoding
                
                status['channels'][key] = ch_stats
                
                if ch_stats.get('samples_received', 0) > 0:
                    status['overall']['channels_active'] += 1
                status['overall']['total_samples_received'] += ch_stats.get('samples_received', 0)
                status['overall']['total_samples_written'] += ch_stats.get('samples_written', 0)
            
            # T6 BPSK PPS calibrator status
            if self._t6_calibrator is not None:
                status['l6_pps'] = {
                    'enabled': True,
                    'locked': self._t6_calibrator.locked,
                    'pps_ok': self._t6_calibrator.pps_ok,
                    'pps_noise': self._t6_calibrator.pps_noise,
                    'pps_consecutive': self._t6_calibrator.pps_consecutive,
                    'chain_delay_ns': (self._t6_calibrator._chain_delay_samples
                                       * 1_000_000_000 / self._t6_calibrator.sample_rate
                                       if self._t6_calibrator._chain_delay_samples is not None
                                       else None),
                }

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
            now = time.time()
            uptime = now - self.start_time

            # Check individual channel health
            for key, recorder in self.recorders.items():
                desc = recorder.config.description

                if not recorder.is_healthy():
                    silence = recorder.get_silence_duration()
                    logger.warning(
                        f"Channel {desc} silent for {silence:.0f}s"
                    )
                    # StreamRecorderV2's health monitor will handle channel recreation

                # Per-channel WRITE stall detection: channel receives RTP data
                # but archive writer is not producing files (no GPS_TIME, disk
                # full, compression stall, etc.).  Only check after startup.
                if uptime > 300:
                    written = recorder.samples_written
                    prev = self._per_channel_last_written.get(key, 0)
                    if written > prev:
                        self._per_channel_last_written[key] = written
                        self._per_channel_last_advance[key] = now
                    else:
                        last_advance = self._per_channel_last_advance.get(key, now)
                        stall = now - last_advance
                        if stall > 120:
                            logger.error(
                                f"Channel {desc}: WRITE STALL — receiving RTP "
                                f"but 0 samples written in {stall:.0f}s. "
                                f"Check GPS_TIME lock or disk."
                            )
                        elif stall > 60:
                            logger.warning(
                                f"Channel {desc}: no samples written in {stall:.0f}s"
                            )

            # DATA FRESHNESS CHECK: Verify output files are being written
            # This catches silent failures where process runs but doesn't write data
            self._check_data_freshness()

        except Exception as e:
            logger.error(f"Health monitoring error: {e}")
    
    def _data_is_flowing(self) -> bool:
        """Return True if any recorder has written samples recently.

        Used by the main loop to decide whether to pet the systemd
        watchdog.  If no samples have been written in >120s, we stop
        petting and let systemd kill us after WatchdogSec (180s).
        """
        try:
            total = sum(r.samples_written for r in self.recorders.values())
            if total > self._wd_last_written:
                self._wd_last_written = total
                self._wd_last_advance = time.time()
                return True
            return (time.time() - self._wd_last_advance) < 120
        except Exception:
            return True  # Err on the side of petting

    def _check_data_freshness(self):
        """Check that recorders are actively receiving and writing samples.

        Uses per-recorder samples_written counters rather than filesystem mtime.
        Filesystem mtime is unreliable when the RTP epoch is behind wall clock
        (files land in past-dated directories) or when tiered storage moves files
        between hot and cold buffers.

        If no samples have been written across all recorders for >5 minutes,
        triggers a self-restart via sys.exit(1).  Systemd will restart the service.
        """
        try:
            now = time.time()
            uptime = now - self.start_time

            # Snapshot total samples written across all active recorders
            total_written = sum(
                r.samples_written for r in self.recorders.values()
            )

            if total_written > self._freshness_last_written:
                # Progress — reset the stale timer
                self._freshness_last_written = total_written
                self._freshness_last_advance = now
                return

            # No progress since last check
            silence = now - self._freshness_last_advance

            # Only alert after the service has had time to start up
            if uptime < 300:
                return

            if silence > 180:  # 3 minutes
                logger.error(
                    f"DATA FRESHNESS WARNING: No samples written in {silence:.0f}s "
                    f"across {len(self.recorders)} recorders. "
                    f"Check disk full, permissions, or network loss."
                )

            # CRITICAL: Trigger self-restart if stale for >5 minutes
            # This ensures automatic recovery from silent failures.
            # Guard: only self-restart if we've been running long enough to have
            # written our own data. Otherwise we crash-loop on restart because
            # stale files from the previous run trigger immediate exit.
            if silence > 300 and uptime > 360:  # 5 min stale + 6 min uptime
                logger.critical(
                    f"DATA FRESHNESS CRITICAL: No samples written in {silence:.0f}s "
                    f"({silence/60:.1f} min). Setting running=False to trigger restart."
                )
                self.running = False
                # Do NOT call sys.exit() here — that bypasses finally:_shutdown()
                # in the main loop. Setting running=False is sufficient; the while
                # loop exits cleanly and _shutdown() runs via the finally clause.

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

        # Stop the shared MultiStream FIRST so its receive loop and
        # health-monitor thread aren't dispatching callbacks into
        # recorders that are mid-teardown below.  (Legacy mode stops
        # per-channel RadiodStreams inside recorder.stop().)
        if self._multi is not None:
            try:
                self._multi.stop()
                logger.info("Shared MultiStream stopped")
            except Exception as e:
                logger.error(f"Error stopping shared MultiStream: {e}", exc_info=True)

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
        

        # Stop T6 BPSK PPS stream
        if self._t6_stream is not None:
            try:
                self._t6_stream.stop()
                logger.info("T6 BPSK PPS stream stopped")
            except Exception as e:
                logger.debug(f"T6 stream stop: {e}")

        # Remove T6 BPSK channel from radiod.  Unlike archived channels
        # (whose SSRC is deterministic from a stable description), the
        # T6 channel's SSRC is a hash of (freq, sample_rate, preset);
        # changing sample rate creates a new SSRC, so orphans accumulate
        # in radiod's channel table across restarts unless we explicitly
        # remove the previous one.  RadiodControl.remove_channel sets
        # frequency to 0 and radiod cleans it up on the next polling
        # cycle.  This is best-effort; failure is logged but non-fatal.
        if self._t6_channel_info is not None:
            ssrc = getattr(self._t6_channel_info, 'ssrc', None)
            if ssrc is not None and ssrc != 0:
                try:
                    self.control.remove_channel(ssrc)
                    logger.info(
                        f"T6 BPSK PPS channel removed from radiod: "
                        f"SSRC=0x{ssrc:08x}"
                    )
                except Exception as e:
                    logger.warning(f"T6 channel removal failed (SSRC=0x{ssrc:08x}): {e}")

        # Close RadiodControl
        try:
            self.control.close()
        except Exception as e:
            logger.debug(f"Ignored exception: {e}")
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
    ka9q_section = config.get('ka9q', {})
    recorder_config = {
        'output_dir': output_dir,
        'station': config.get('station', {}),
        'recorder': recorder_section,
        'channels': channels,
        'channel_defaults': recorder_section.get('channel_defaults', {}),
        'status_address': ka9q_section.get('status_address', '239.192.152.141'),
        'ka9q': ka9q_section,
        'timing': config.get('timing', {}),
    }
    
    logger.info(f"Loaded {len(recorder_config['channels'])} channels from config")
    
    # Run recorder
    recorder = CoreRecorderV2(recorder_config)
    recorder.run()


if __name__ == '__main__':
    main()
