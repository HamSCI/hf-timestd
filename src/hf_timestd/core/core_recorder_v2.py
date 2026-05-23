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
import numpy as np
from collections import deque
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

# radiod auto-destruct timer for our channels (units: radiod main-loop
# frames, ~50 Hz at default 20 ms blocktime → 6000 frames ≈ 120 s).
# Without this, channels we allocated stay live in radiod forever after
# the python process exits — radiod has no way to know we're gone, so
# it keeps streaming bandwidth that nobody consumes. CoreRecorderV2
# starts a keepalive thread that refreshes this every ~30 s while
# we're running; on clean exit + crash the channel auto-destructs in
# at most LIFETIME / 50 seconds.
RADIOD_LIFETIME_FRAMES = 6000


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

        # client_id makes ka9q-python derive a per-(client, radiod)
        # multicast destination (CONTRACT v0.3 §7) so hf-timestd's
        # channels never share a multicast group with peer clients on
        # the same radiod.  Requires ka9q-python ≥ 3.14.0; with older
        # ka9q-python the kwarg is silently ignored.
        self.control = RadiodControl(self.status_address,
                                      client_id="hf-timestd")

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

        # (multi_or_control, ssrc) pairs — populated as channels are
        # provisioned, used by the keep-alive thread to refresh radiod's
        # LIFETIME timer so the channels self-destruct after we exit.
        self._lifetime_entries: list = []
        self._lifetime_thread: Optional[threading.Thread] = None
        
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
        # Differential-detector sidecar (offline A/B analysis).  Opt-in
        # via t6_config['enable_diff_sidecar'] = True.  Runs alongside
        # the main calibrator on every batch of T6 samples; dumps
        # per-PPS edge timestamps to a CSV at
        # t6_config['diff_sidecar_path'] (default
        # /var/lib/timestd/debug/bpsk_diff_edges.csv) for later
        # comparison against the MF detector's chain_delay history.
        # Does NOT push to chrony or modify any T6 state.
        self._t6_diff_calibrator = None
        self._t6_diff_warned = False
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

        # SHM unit 3 (HFPS): the diff-detector (Method 5) edge feed.
        # Built only when t6_config['enable_diff_sidecar'] is True
        # AND the operational SHM push is enabled.  Runs in parallel
        # with TSL3 — chrony selects between them via its own
        # selection algorithm (HFPS gets `prefer` in chrony.conf
        # once validated).
        self._t6_diff_shm = None
        self._t6_diff_last_pushed_rtp = None
        self._t6_diff_shm_push_count = 0
        # Diagnostic counters for the T6 SHM push gate.  Pair with the
        # periodic log line below — on the next "TSL3 dark while
        # acquired=1" incident, the journal shows whether pushes are
        # firing at the expected 1 Hz, stalling at 0 Hz, or running
        # but rejected by chrony (cross-check via chronyc reach).
        self._t6_shm_push_count = 0
        self._t6_shm_last_log_count = 0
        self._t6_shm_last_log_wall = time.monotonic()
        self._t6_shm_last_push_wall = None
        # Residual published by the cascade as T6's local_minus_source_ns
        # (the value chrony sees as TSL3 offset, computed at every SHM
        # update site).  Pattern B publication channel per
        # docs/TIMING-PIPELINE-WIRING.md §9 step 1.  None until first
        # SHM push.  Stored in ns to match `rtp_to_utc_offset_ns`
        # convention.
        self._t6_last_local_minus_source_ns = None
        # Rolling window of recent chain_delay_ns values (one per
        # accepted PPS edge, ~1 Hz).  std-dev across the window is the
        # observed BPSK matched-filter jitter — the dominant physical
        # uncertainty contribution to TSL3.  Published in the status
        # JSON as l6_pps.chain_delay_ns_std_ns; BpskPpsProbe converts
        # it to authority.t6_sigma_ms (floored at sigma_floor_ms so we
        # don't under-claim during calm windows).  60 samples ≈ 1 min
        # of recent history — long enough to average out per-PPS noise,
        # short enough that a real degradation shows up within 1 min.
        self._t6_chain_delay_history = deque(maxlen=60)
        # Kept alongside for diagnostics: std of the residual we push
        # to chrony.  In normal operation this is near-zero (the
        # integer-second-residual stays inside one ns quantum when
        # chrony has the local clock well-disciplined and the anchor
        # is the frozen ChannelInfo); it is NOT the right authority
        # σ signal but stays visible in the probe.detail block for
        # debugging.
        self._t6_local_minus_source_history = deque(maxlen=60)
        # V1 fix per docs/TIMING-PIPELINE-WIRING.md §10.3 path 2a (option 2):
        # the RTP→wall_time math for TSL3 SHM uses a *fresh* GPS/RTP
        # anchor — refreshed by _t6_timing_poll_loop — instead of the
        # frozen ChannelInfo captured at discover_channels() time.
        # ka9q's rtp_to_wallclock is no longer used on the T6 path.
        self._t6_latest_gps_time_ns = None
        self._t6_latest_rtp_timesnap = None
        self._t6_timing_lock = threading.Lock()
        self._t6_timing_poll_thread = None
        self._t6_timing_poll_stop = threading.Event()
        # V1 fix layer 2 — drift monitor (TIMING-PIPELINE-WIRING.md §10.3).
        # Two independent signals are tracked against the settled-capture
        # anchor; both publish via ``_write_status`` → ``BpskPpsProbe`` →
        # ``authority.json`` so downstream consumers can see degradation.
        # Layer 2 is monitor-only — Layer 3 will use these flags to drive
        # re-capture.  See ``_t6_check_anchor_consistency`` and
        # ``_t6_check_delta_breach`` for the math.
        self._t6_drift_first_breach_wall: Optional[float] = None
        self._t6_drift_flag_sustained: bool = False
        self._t6_drift_flag_anchor_discontinuity: bool = False
        self._t6_drift_anchor_residual_samples: Optional[int] = None
        # Consecutive polls on which the anchor residual has breached
        # T6_ANCHOR_DISCONTINUITY_SAMPLES — the persistence gate that
        # keeps reading noise from triggering a re-capture (Signal A).
        self._t6_drift_residual_breach_count: int = 0
        self._t6_drift_last_check_wall: Optional[float] = None
        self._t6_drift_anchor_gps_ns: Optional[int] = None
        self._t6_drift_anchor_rtp_timesnap: Optional[int] = None
        # V1 fix layer 3 — re-capture state.  Reaction lives on the
        # poll thread (the sample callback can't block on the
        # 60-second settled-capture gate).  See _t6_react_to_flags
        # and _t6_attempt_recapture.
        self._t6_recapture_count: int = 0
        self._t6_last_recapture_wall: Optional[float] = None
        self._t6_last_recapture_reason: Optional[str] = None
        self._t6_recapture_wall_history: deque = deque(
            maxlen=self.T6_RECAPTURE_MAX_PER_HOUR + 1
        )
        # Wrap-rejection guard: the BPSK calibrator algorithm has a known
        # cascade where a noise edge near the half-second mark from a real
        # edge displaces the reference and causes chain_delay to wrap by
        # ~half a second (62.5 us natural sample wobble vs 322 ms wrap).
        # We track the last accepted chain_delay and reject jumps > 1 ms,
        # keeping the previously-good correction in place. Reset to None
        # on calibrator restart so the first stable lock is always accepted.
        self._t6_last_chain_delay_ns = None
        self._t6_wrap_rejections = 0
        # Step-recovery: track recent rejected raw chain_delays so a
        # genuine permanent step (chain_delay actually moved, not a
        # transient noise wrap) can be detected and re-disambiguated.
        # See T6_STEP_RECOVERY_WINDOW / T6_STEP_RECOVERY_TIGHT_NS for the
        # cluster criteria.
        self._t6_recent_raw = deque(maxlen=self.T6_STEP_RECOVERY_WINDOW)
        # Stuck-recovery: wall-clock time of the most recent
        # ``result.locked = True`` cycle.  If samples flow but lock
        # is never re-asserted (cascade gate keeps pps_consecutive at
        # 0 because the operating point has actually moved), reset
        # the calibrator after T6_STUCK_TIMEOUT_SEC so it re-acquires
        # at the current peak position.  Set to None until the first
        # samples arrive so we don't reset during cold start.
        self._t6_last_locked_wall = None
        # Set at first stable lock from system-clock comparison; constant
        # offset added to every calibrator chain_delay report so all
        # measurements share a common disambiguated reference frame.
        self._t6_disambiguation_ns = 0
        # Same idea, but for the diff detector (Method 5) chain_delay.
        # The diff detector reports chain_delay_samples = edge_rtp mod
        # SR, i.e. only the sub-second position of the observed
        # polarity flip.  To turn that into a wall-clock offset chrony
        # can use, we need to know WHICH integer-sample position
        # within the second is the true PPS edge — same disambiguation
        # the legacy MF does against T4 (LAN GPS).  Locked once on the
        # first accepted diff edge; constant from then on (the RF chain
        # delay is a property of the hardware, not a per-edge measurement).
        self._t6_diff_disambiguation_ns = 0
        self._t6_diff_disambiguated = False
        # NMEA-anchored SHM math (Option 3, 2026-05-23 PM session):
        # at T5 disambig we pair (NMEA integer GPS second, BPSK edge RTP).
        # Per-edge SHM push then derives M_edge by edge-counting from this
        # pair using GPSDO-accurate RTP deltas, and computes the host clock
        # at the edge sample FRESH via clock_gettime() minus RTP-elapsed
        # — never consults the stale ka9q anchor.  Bypasses the
        # anchor-drift artifact (see project_hf_pps_t5_direct_2026-05-23).
        self._t6_diff_M_disambig = None
        self._t6_diff_edge_rtp_disambig = None
        # Physical chain_delay calibration — initialized to 0 here;
        # re-read from self._t6_config after that attribute is assigned
        # just below.
        self._t6_chain_delay_calib_s = 0.0
        # Cross-restart disambiguation persistence.  Each restart used
        # to re-run the T4 chrony comparison from scratch, which picked
        # a different disambiguation every time (chrony slews
        # continuously).  Loading a fresh persisted *effective*
        # chain_delay lets us re-derive disambiguation from an invariant
        # — the physical RF path — instead of from chrony's transient
        # state.  See bpsk_chain_delay_store.py for the rationale and
        # docs/HF-PPS-CHRONY-TUNING.md §5 for the original symptom.
        from .bpsk_chain_delay_store import ChainDelayStore
        self._t6_mf_chain_delay_store = ChainDelayStore("MF")
        self._t6_diff_chain_delay_store = ChainDelayStore("diff")
        # Counters for save-cadence debouncing (write once per
        # PERSIST_EVERY_N_EDGES accepted edges, not on every cycle).
        self._t6_mf_saves_pending = 0
        self._t6_diff_saves_pending = 0
        # T5 disambiguation reference (LB-1421 GPSDO NMEA over USB-CDC).
        # When wired, gives an integer-GPS-second reference for the
        # BPSK PPS disambiguation that bypasses chrony's discipline
        # noise entirely — closes the architectural detour where the
        # GPSDO drives TS-1 to produce the PPS we measure, but we
        # then asked chrony (disciplined by a LAN GPS NTP server) for
        # the integer second.  Instantiate via lb1421_t5_probe.py and
        # pass into this object via attach_lb1421_probe; absent
        # injection, T5 is unavailable and the disambig falls through
        # to T4 chronyc tracking as before.
        self._lb1421_probe = None
        self._t6_config = config.get('timing', {}).get('l6_pps', {})
        # Apply chain_delay calibration knob now that _t6_config is set
        # (referenced by HFPS NMEA-anchored SHM push).
        self._t6_chain_delay_calib_s = float(
            self._t6_config.get('chain_delay_calib_s', 0.0)
        )
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
                        # Diagnostic capture (opt-in).  When
                        # debug_dump_path is set, the MF calibrator
                        # records the matched-filter output ``y``,
                        # detected peak metadata, and Costas phase per
                        # batch to a single NPZ for offline analysis.
                        # Used to investigate the cascade re-lock
                        # against secondary candidates ~60 samples
                        # away from the real PPS edge.
                        debug_dump_path=self._t6_config.get('debug_dump_path'),
                        debug_dump_seconds=self._t6_config.get('debug_dump_seconds', 60.0),
                        debug_dump_subthreshold_factor=self._t6_config.get(
                            'debug_dump_subthreshold_factor', 0.2
                        ),
                        # Periodic Costas-phase log (0 disables).
                        # Default off; enable in TOML for investigation
                        # of the ~13-second phase excursions.
                        phase_log_period_batches=self._t6_config.get(
                            'phase_log_period_batches', 0
                        ),
                        # Magnitude-correlation detection (opt-in).
                        # When True the matched filter runs on the
                        # COMPLEX signal and peak-picks on |y| — no
                        # Costas dependency.  Eliminates the
                        # carrier-recovery instability and the
                        # per-restart chain_delay disambiguation drift
                        # that the Re(s_rot) path inherits from
                        # Costas's choice of operating point.  See
                        # docs/HF-PPS-CHRONY-TUNING.md §5.2.
                        use_magnitude_correlation=self._t6_config.get(
                            'use_magnitude_correlation', False
                        ),
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

                # Differential-detector sidecar — opt-in via
                # t6_config['enable_diff_sidecar'] = True.  Runs in
                # parallel with the main calibrator on every batch;
                # dumps per-PPS edge timestamps to a CSV for offline
                # analysis.  DOES NOT push to chrony or touch any
                # other T6 state.  See bpsk_pps_calibrator_diff.py.
                if self._t6_config.get('enable_diff_sidecar', False):
                    try:
                        from hf_timestd.core.bpsk_pps_calibrator_diff import (
                            BpskPpsCalibratorDiff,
                        )
                        diff_csv = self._t6_config.get(
                            'diff_sidecar_path',
                            '/var/lib/timestd/debug/bpsk_diff_edges.csv',
                        )
                        diff_thresh = self._t6_config.get(
                            'diff_sidecar_threshold_factor', 100.0,
                        )
                        self._t6_diff_calibrator = BpskPpsCalibratorDiff(
                            sample_rate=sr,
                            output_path=diff_csv,
                            threshold_factor=diff_thresh,
                        )
                        logger.info(
                            f"T6 diff-detector sidecar initialised: "
                            f"output={diff_csv}, threshold_factor={diff_thresh}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"T6 diff-detector sidecar init failed "
                            f"(non-fatal, main calibrator continues): {e}"
                        )
                        self._t6_diff_calibrator = None

                # Init TSL3 SHM feed (unit 2). Failure is non-fatal —
                # calibration still drives chain_delay_correction_ns.
                try:
                    from hf_timestd.core.chrony_shm import ChronySHM
                    self._t6_shm = ChronySHM(unit=2)
                    if self._t6_shm.connect():
                        logger.info("T6 HPPS SHM feed enabled (unit=2)")
                    else:
                        logger.warning("T6 HPPS SHM unit=2 connect failed; "
                                       "TSL3 disabled (chain_delay_correction "
                                       "still applied to channels)")
                        self._t6_shm = None
                except Exception as e:
                    logger.warning(f"T6 HPPS SHM init failed: {e}")
                    self._t6_shm = None

                # Init HFPS SHM feed (unit 3) — operational chrony
                # feed from the diff detector (Method 5).  Built only
                # when the sidecar is enabled AND SHM push is wired.
                # Runs in parallel with TSL3 (unit 2); chrony selects
                # between them by its usual algorithm.
                if (self._t6_diff_calibrator is not None
                        and self._t6_config.get('diff_to_shm_unit', None)
                        is not None):
                    try:
                        from hf_timestd.core.chrony_shm import ChronySHM
                        diff_unit = int(
                            self._t6_config.get('diff_to_shm_unit')
                        )
                        self._t6_diff_shm = ChronySHM(unit=diff_unit)
                        if self._t6_diff_shm.connect():
                            logger.info(
                                f"T6 diff-detector SHM feed enabled "
                                f"(unit={diff_unit}, expected refid HFPS)"
                            )
                        else:
                            logger.warning(
                                f"T6 diff-detector SHM unit={diff_unit} "
                                f"connect failed; HFPS disabled"
                            )
                            self._t6_diff_shm = None
                    except Exception as e:
                        logger.warning(
                            f"T6 diff-detector SHM init failed: {e}"
                        )
                        self._t6_diff_shm = None
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

                # Track for lifetime keepalive — legacy mode uses RadiodControl
                # directly (per-channel RadiodStream + per-channel UDP socket).
                if recorder.config.ssrc:
                    self._lifetime_entries.append(
                        (self.control, recorder.config.ssrc)
                    )

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

        # Begin receiving on the shared MultiStream for the archive
        # channels.  T6 is intentionally NOT on this MultiStream — it
        # uses its own dedicated socket so the archive flush can't
        # stall its packet reads.  See _start_t6_stream docstring.
        if self._use_shared_multistream and self._multi is not None:
            try:
                self._multi.start()
                logger.info(
                    f"Shared MultiStream started: 1 UDP socket serving "
                    f"{len(self.recorders)} SSRC-demuxed archive channels "
                    f"(T6 on its own dedicated stream)"
                )
            except Exception as e:
                logger.error(
                    f"Failed to start shared MultiStream: {e}", exc_info=True,
                )
                return

        # Start the LIFETIME keepalive thread now that every channel
        # (archive + T6) has been provisioned and its SSRC is in
        # self._lifetime_entries. Without this, channels self-destruct
        # ~120 s after start; the keepalive refreshes every ~30 s.
        self._start_lifetime_keepalive()

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
        self._t6_timing_poll_stop.set()
        self.running = False

    def _start_lifetime_keepalive(self) -> None:
        """Refresh radiod's LIFETIME on every active SSRC at frames/4 cadence.

        No-op when no channels were provisioned with a lifetime. Failure
        to refresh (network blip, radiod restart) must not crash the
        recorder — log and continue; on radiod recovery the channel will
        be re-provisioned via the normal ensure/add path.
        """
        if not self._lifetime_entries:
            return
        # Refresh every quarter of the lifetime — gives 4× safety margin
        # against radiod self-destruct if a single refresh is missed.
        # Floor at 1 s so absurd configs don't busy-loop.
        interval = max(RADIOD_LIFETIME_FRAMES / 50.0 / 4.0, 1.0)
        logger.info(
            "lifetime keepalive: %d channels, %d frames, refresh every %.1fs",
            len(self._lifetime_entries),
            RADIOD_LIFETIME_FRAMES,
            interval,
        )
        self._lifetime_thread = threading.Thread(
            target=self._lifetime_loop,
            args=(interval,),
            daemon=True,
            name="lifetime",
        )
        self._lifetime_thread.start()

    def _lifetime_loop(self, interval_sec: float) -> None:
        while self.running:
            time.sleep(interval_sec)
            if not self.running:
                break
            for owner, ssrc in self._lifetime_entries:
                try:
                    owner.set_channel_lifetime(ssrc, RADIOD_LIFETIME_FRAMES)
                except Exception as exc:
                    logger.warning(
                        "lifetime keepalive failed (ssrc=%s): %s", ssrc, exc,
                    )

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
                        if recorder.config.ssrc:
                            self._lifetime_entries.append(
                                (self._multi, recorder.config.ssrc)
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
        """Provision the BPSK PPS channel (no archive) on a dedicated
        RadiodStream, isolated from the archive channels.

        T6 ALWAYS owns its own UDP socket and reader thread, even when
        the archive channels share a MultiStream.  Rationale: archive
        channels do a synchronous zstd + fsync flush on the receive
        thread every ``file_duration_sec`` (default 10 min) which takes
        seconds while ~10 channels' worth of compressed data is written
        to disk.  When T6 rode the same MultiStream socket, those
        flushes blocked the receive loop, the kernel UDP buffer
        overflowed, T6 dropped samples, the Costas loop unlocked, and
        chrony saw TSL3 ``?`` (reach=0) every 10 minutes.  A dedicated
        T6 socket and thread reads packets continuously regardless of
        what the archive thread is doing.

        V1 fix layer 1: block on _wait_for_chrony_settled before
        registering the T6 channel, so the anchor captured by
        ka9q-python at add_channel time inherits a near-zero
        discipline error.  See docs/TIMING-PIPELINE-WIRING.md §10.3.
        """
        # V1 fix layer 1 — settled-capture gate.
        self._wait_for_chrony_settled()

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

        # Dedicated RadiodStream + per-channel UDP socket.
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
                # Same wider-timeout rationale as the shared-MultiStream
                # branch above.
                timeout=30.0,
                # Self-destruct timer; CoreRecorderV2 keeps it refreshed.
                lifetime=RADIOD_LIFETIME_FRAMES,
            )
            self._t6_channel_info = channel_info
            if channel_info is not None and getattr(channel_info, 'ssrc', 0):
                self._lifetime_entries.append((self.control, channel_info.ssrc))
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
                # 256 packets ≈ 3.2 s at T6's 80 pkt/sec, half-fill at
                # 1.6 s.  The resequencer declares a packet lost when the
                # buffer reaches half-full without the next-expected
                # sequence arriving (resequencer.py _handle_lost_packet).
                # The default 128 gave 0.8 s tolerance — long enough for
                # normal jitter but tight under transient CPU contention
                # (observed on bee1 2026-05-21: a single archive-rollover
                # burst momentarily starved the T6 reader thread, the
                # resequencer over-eagerly declared a packet "lost",
                # filled 480 ms with zeros, and the BPSK Costas loop
                # unlocked downstream from the resulting phantom-edge
                # storm).  Doubling the buffer doubles the wait window
                # without affecting steady-state latency.
                resequence_buffer_size=256,
            )
            self._t6_stream.start()
            logger.info(f"T6 BPSK PPS stream started: {desc} at {freq_hz/1e6:.6f} MHz")

            # V1 fix per docs/TIMING-PIPELINE-WIRING.md §10.3 path 2a option 2.
            # Start the T6 timing-anchor refresh thread so the TSL3 SHM math
            # uses a fresh (gps_time, rtp_timesnap) pair each push, not the
            # ChannelInfo frozen at discover_channels() time.
            if self._t6_timing_poll_thread is None:
                self._t6_timing_poll_stop.clear()
                self._t6_timing_poll_thread = threading.Thread(
                    target=self._t6_timing_poll_loop,
                    name="T6TimingPoll",
                    daemon=True,
                )
                self._t6_timing_poll_thread.start()
                logger.info(
                    f"T6 timing-anchor refresh thread started "
                    f"(interval={self.T6_TIMING_POLL_SEC}s)"
                )
        except Exception as e:
            logger.error(f"Failed to start T6 BPSK PPS stream: {e}", exc_info=True)
            self._t6_stream = None

    # Maximum sigma of a non-T6 reference we'll trust for disambiguation.
    #
    # Disambiguation resolves an integer-sample shift via
    # ``shift_samples = round(disagreement_sec * sample_rate)``.  At 96 kHz
    # the sample period is 10.4 µs; a reliable integer-sample pick demands
    # reference central-value uncertainty well under one sample period.
    # Empirically (bee1 2026-05-23) a T3 fusion reference with σ ≈ 4 ms
    # and a transient central-value bias of a few hundred µs picked a wrap
    # off by 24 samples (≈ +252 µs), then re-locked −381 µs after a
    # restart that captured a different fusion bias — both wide enough to
    # make chrony flag TSL3 as a falseticker.  T4 chronyc tracking against
    # a healthy LAN GPS routinely sits at ~1 µs RMS, which IS tight enough
    # for sub-sample disambiguation.
    #
    # 10 µs ≈ one sample period at 96 kHz — the threshold below which a
    # reference will reliably yield the correct integer wrap.  In practice
    # this means T3 fusion is never used for disambiguation; the
    # bootstrap path is always T4 (or T5 once wired), satisfying the
    # §4.5 invariant via the *persistence* mechanism: T4 is consulted
    # ONCE per restart-window to establish the RF-path-invariant
    # ``effective_chain_delay``, and from there every cycle re-derives
    # disambiguation from the persisted value with no T4 dependence.
    #
    # Set to 0.010 ms rather than the theoretical 0.005 ms (half-sample)
    # because chrony's RMS_offset metric (the published σ) reflects
    # multi-poll history rather than instantaneous noise — at sub-µs
    # actual jitter, chrony often reports RMS in the 4-8 µs range.  Half-
    # sample-period gating rejected T4 ~half the time on bee1 2026-05-23
    # and dropped to the catastrophic "accept as-is" fallback (no shift
    # applied → wall_time offset could be anywhere in [-500 ms, +500 ms]).
    # One-sample-period gating gives ±0.5 sample = ±5 µs central-value
    # precision when the disambig runs, which translates to ~5-10 µs
    # residual on TSL3/HPPS — still 20-50× better than the prior T3-fusion
    # regime, with reliable engagement.
    T6_DISAMBIGUATION_MAX_SIGMA_MS = 0.010

    # Step-recovery thresholds for the wrap-rejector.  The wrap-rejector
    # locks in the first stable chain_delay after disambiguation; if the
    # underlying (raw) chain_delay later steps to a new value (because the
    # calibrator re-locked at a different edge after a glitch, sample-rate
    # excursion, or stream restart), every subsequent measurement gets
    # rejected forever and TSL3's SHM samples drift to the stale value —
    # chrony filters them out and reach falls to 0.  Recovery rule: when
    # the rejector has seen ``T6_STEP_RECOVERY_WINDOW`` consecutive raw
    # values that cluster within ``T6_STEP_RECOVERY_TIGHT_NS`` of each
    # other, treat that as a real step and reset the lock so the next
    # cycle re-runs initial-accept (re-disambiguating against the highest
    # available timing tier).  Tight cluster discriminates a real new
    # operating point from chaotic noise excursions.
    T6_STEP_RECOVERY_WINDOW = 60
    T6_STEP_RECOVERY_TIGHT_NS = 1_000_000

    # T5-sanity threshold for step-recovery: when T5 (LB-1421 NMEA) is
    # available and step-recovery is about to admit a 60-rejection
    # cluster as a "genuine new operating point," reject the candidate
    # if T5 says the physical chain_delay hasn't really moved.  5 ms is
    # well above legitimate slow physical drift (temperature, etc.) and
    # well below the half-second-wrap sidelobe distance (500 ms) that
    # caused the 2026-05-23 phantom-step incident.
    T6_STEP_RECOVERY_T5_SANITY_NS = 5_000_000

    # Stuck-recovery timeout for the calibrator.  The MF cascade-
    # tolerance gate (in BpskPpsCalibratorMF) intentionally prevents
    # ``_last_edge_rtp`` from moving on far-out noise edges so a
    # transient Costas-loop excursion doesn't shift the lock.  But if
    # the underlying signal genuinely settles at a new operating point
    # (Costas walks to a different π-stable lock, or a multi-second
    # carrier shift), the calibrator stays unlocked indefinitely
    # (``pps_consecutive`` never climbs back) and the wrap-rejection
    # branch never fires (it requires ``result.locked``), so the
    # earlier step-recovery cannot trigger.  Observed on bee1
    # 2026-05-08: phase walked ~5.9π over 8 hours, calibrator stuck
    # with chain_delay frozen at the original lock and TSL3 reach=0.
    # Recovery: when the calibrator has been unlocked for more than
    # this many seconds, reset it (drops _last_edge_rtp, _acquired)
    # and clear the disambiguation state so the next cycle hits
    # initial-accept and locks at whatever the current operating point
    # is.  60 s is wider than any single Costas-excursion we've
    # observed (~13 s) so transient cascades don't trigger needless
    # resets.
    T6_STUCK_TIMEOUT_SEC = 60.0
    # Cadence for writing the persisted effective chain_delay to disk.
    # At 1 PPS, 60 edges = one save per minute — bounded even if the
    # rate later climbs.  See bpsk_chain_delay_store.py for the
    # cross-restart story.
    T6_PERSIST_EVERY_N_EDGES = 60
    # Cadence of the T6-SHM diagnostic log line.  Every 60 s emits one
    # INFO line with pushes-per-window + the gate-decision inputs
    # (`last_edge_rtp` vs `_t6_last_pushed_rtp`, `result.locked`,
    # `pps_consecutive`).  Operationally cheap (1 line/min); essential
    # for pinning down the remaining "TSL3 dark while acquired=1"
    # failure mode that the watchdog only recovers from, not
    # diagnoses.
    T6_SHM_LOG_INTERVAL_SEC = 60.0
    # Cadence for the T6 timing-anchor refresh thread.  Used to be
    # 5 s / 2 s but the SHM-push code reverted to `rtp_to_wallclock`
    # with the frozen ChannelInfo (the comment at the push site
    # documents this; option-2 fresh-anchor produced jittery Δ in
    # 2026-05-11 testing).  The poll thread's *only* remaining
    # consumers are Signal A (anchor consistency check, threshold
    # measured in deciseconds — gradual drift) and Layer 3 recapture
    # trigger (debounced by hysteresis).  Neither needs sub-30 s
    # reaction time.  30 s sleep + 0.5 s listen cuts discover_channels
    # invocations by ~12× and the listen window by 4× vs the old
    # cadence, eliminating most of the per-T6-poll multicast overhead
    # while keeping discontinuity detection well within
    # T6_DRIFT_SUSTAINED_SEC (60 s).
    T6_TIMING_POLL_SEC = 30.0
    T6_TIMING_POLL_LISTEN_SEC = 0.5

    # V1 fix layer 2 — drift monitor thresholds.  See
    # docs/TIMING-PIPELINE-WIRING.md §10.3 step 2.
    #
    # Signal A (anchor consistency).  At each poll, we project the
    # captured anchor's (gps_time, rtp_timesnap) forward by the elapsed
    # gps_time and compare to radiod's freshly-reported rtp_timesnap.
    # A residual above this threshold means either radiod restarted
    # (RTP counter discontinuity) or the host clock took an unannounced
    # step large enough to invalidate the anchor.  1000 samples ≈ 10 ms
    # at 96 kHz.
    #
    # TSL3 anchor-churn fix (2026-05-18, docs/TIMING-PIPELINE-WIRING.md
    # §10.3): a *single* discover_channels reading is far noisier than
    # this threshold — measured noise floor ≈ ±400 samples, with
    # intermittent outliers of tens of thousands of samples (radiod
    # status-emit jitter / a stale status packet caught in the listen
    # window).  Acting on one such reading re-captured the anchor every
    # ~minute — exactly the "periodic refresh" §10.3 identifies as wrong
    # (it re-injects chrony's slew + reading noise into Δ).  So the
    # *residual* check raises the flag only after the threshold is
    # breached on T6_ANCHOR_DISCONTINUITY_POLLS *consecutive* polls: a
    # genuine radiod restart / clock step is a permanent discontinuity
    # that breaches every poll, while a noise outlier is transient and
    # resets the counter.  The anchor then stays frozen (the §10.3
    # design) in normal operation.  (The counter-rollback check below is
    # an unambiguous namespace change and still fires immediately.)
    T6_ANCHOR_DISCONTINUITY_SAMPLES = 1000
    # Consecutive breaching polls (at the 5 s poll cadence) required
    # before the residual check flags a discontinuity.  5 → 25 s of
    # sustained breach: a real restart/clock-step clears that trivially,
    # a run of independent noise outliers effectively never does.
    T6_ANCHOR_DISCONTINUITY_POLLS = 5
    # Signal B (sustained Δ breach).  Δ = chrony's view of TSL3 offset.
    # In settled operation |Δ| stays sub-µs.  A sustained breach of
    # 1 ms for ≥ 60 s indicates the anchor has lost validity or the
    # sample clock has walked far enough that the TSL3 SHM feed is
    # misleading chrony.  Layer 3 uses these flags to drive
    # re-capture; Layer 2 only surfaces them.
    T6_DRIFT_HARD_THRESHOLD_NS = 1_000_000  # 1 ms
    T6_DRIFT_SUSTAINED_SEC = 60.0

    # V1 fix layer 3 — re-capture policy.  Consumes the Layer 2 flags
    # (_t6_drift_flag_{anchor_discontinuity,sustained}) and re-runs
    # the settled-capture gate + fresh discover_channels to replace
    # both anchors atomically.
    #
    # Anchor discontinuity (Signal A): bypasses hysteresis — namespace
    # changes are binary, the old anchor is invalid the moment the
    # rollback or large-residual is detected.  Re-capture immediately.
    #
    # Sustained breach (Signal B): honors hysteresis to prevent
    # ping-pong when a degraded condition is just barely above
    # threshold.  Cooldown caps re-captures at one per N seconds;
    # per-hour cap protects against pathological feedback loops
    # (e.g. chrony oscillating and dragging Δ in and out of breach).
    T6_RECAPTURE_COOLDOWN_SEC = 300.0   # 5 min minimum between recaptures
    T6_RECAPTURE_MAX_PER_HOUR = 5       # rate cap (sustained-breach only)

    # V1 fix layer 1 (settled-capture gate) per
    # docs/TIMING-PIPELINE-WIRING.md §10.3.  Block _start_t6_stream's
    # discover_channels call until chrony has been settled for
    # T6_SETTLE_REQUIRED_CYCLES consecutive readings, where "settled"
    # means |Last offset| <= T6_SETTLE_MAX_OFFSET_S.  Polling cadence
    # is T6_SETTLE_POLL_SEC.  If chrony hasn't settled within
    # T6_SETTLE_TIMEOUT_SEC seconds we proceed degraded (loudly logged)
    # rather than block forever — fits comfortably within
    # TimeoutStartSec=300 in the systemd unit.
    #
    # Capturing the anchor when chrony's discipline error ε_0 ≈ 0
    # means subsequent TSL3 Δ values track chrony's *current*
    # discipline error rather than carry a permanent baseline shift.
    # Without this gate, a startup race produces the silent +237 ms
    # failure documented in the 2026-05-11 incident.
    T6_SETTLE_MAX_OFFSET_S = 0.0001        # 100 µs
    T6_SETTLE_REQUIRED_CYCLES = 3
    T6_SETTLE_POLL_SEC = 5.0
    T6_SETTLE_TIMEOUT_SEC = 60.0

    def _get_disambiguation_reference(self):
        """Return the highest-rank non-T6 timing-authority offset estimate.

        Walks the T-level hierarchy in descending rank order, returning
        the first probe that publishes an offset_ms with sigma <
        ``T6_DISAMBIGUATION_MAX_SIGMA_MS``.  Returns
        ``(offset_ms, sigma_ms, tier_name)`` or ``None`` if no suitable
        reference is available.

        Used ONCE at first lock to resolve which integer GPS-second the
        BPSK edge belongs to (the per-channel-creation RTP-grid alignment
        is non-deterministic against GPS seconds — could be off by any
        integer-sample multiple). Once disambiguated, T6 trusts its own
        measurements; we do NOT continuously slew toward the reference.

        ## RTP-reference invariant (METROLOGY.md §4.5)

        Per the project-wide invariant, **data-label authority must be
        derivable from RTP + a fusion-or-peer-derived offset, never from
        the host wall clock**.  The reference order below reflects that:

          - **T5** (highest): on-host GPS+PPS chrony refclock — direct
            peer authority, no wall-clock dependence.  Not yet wired.
          - **T3**: HF Fusion offset via
            ``/run/hf-timestd/fusion_status.json``.  Listed first as
            the invariant-cleanest source, but **in practice rejected
            by the sigma gate** — HF fusion's steady-state uncertainty
            (sub-ms at best) is far wider than the sample period
            (~10 µs at 96 kHz), so the integer-sample disambiguation
            it produces is unreliable.  Bee1 2026-05-23: a T3 fusion
            disambig with σ=4.3 ms picked a wrap off by 24 samples
            (+252 µs), then re-locked −381 µs after restart — both
            wide enough that chrony marked TSL3 a falseticker.
          - **T4** (practical bootstrap): LAN GPS+PPS via
            ``chronyc tracking``.  Reads ``system_clock − true_UTC``;
            superficially appears to couple disambig to the host wall
            clock, but the invariant is preserved through the
            *persistence* mechanism: T4 is consulted ONCE per
            restart-window to pick the integer wrap, the result is
            written to ``/var/lib/timestd/bpsk_*_chain_delay.json``,
            and every subsequent cycle re-derives disambiguation from
            that RF-path-invariant value with no further T4 reads.
            Per-sample data labeling continues to use T3 fusion as
            the authority offset; T4 only resolves the integer-wrap
            ambiguity at the moment of physical lock.
        """
        # T5: on-host GPS+PPS — not yet wired (requires direct refclock
        # probe in core-recorder).  Add a check here once the probe lands.

        # T3 — fusion is the invariant-cleanest reference, but the
        # sample-period-aligned sigma gate (5 µs at 96 kHz) will reject
        # it in practice: HF fusion's steady-state uncertainty sits in
        # the millisecond range, three orders of magnitude wider than
        # what an integer-sample disambiguation can tolerate.  Listed
        # first so that if T5 lands (sub-µs reference) or a future
        # fusion design tightens its σ, this path activates without a
        # code change.
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

        # T4 BOOTSTRAP — practical disambiguation source today.  The
        # §4.5 invariant is preserved by the persistence mechanism:
        # T4's reading is consulted ONCE here, written to
        # bpsk_*_chain_delay.json as the RF-path-invariant
        # effective_chain_delay, and every subsequent cycle re-derives
        # disambiguation from that value with no further wall-clock
        # read.  Reads chrony's tracking offset against the LAN GPS
        # source.  `Last offset` is (true_time − local_time); we
        # negate for (system_clock − UTC).
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
                    offset_ms = -last_offset_sec * 1000.0
                    sigma_ms = rms_offset_sec * 1000.0
                    if sigma_ms <= self.T6_DISAMBIGUATION_MAX_SIGMA_MS:
                        logger.info(
                            f"T6 disambiguation using T4 chronyc tracking "
                            f"(sigma={sigma_ms:.3f} ms).  Result persists "
                            f"to bpsk_*_chain_delay.json so subsequent "
                            f"cycles re-derive from the RF-path-invariant "
                            f"value without re-reading chrony.  See "
                            f"METROLOGY.md §4.5."
                        )
                        return offset_ms, sigma_ms, 'T4'
                    else:
                        logger.warning(
                            f"T6 disambiguation: T4 chronyc tracking "
                            f"sigma={sigma_ms:.3f} ms exceeds gate "
                            f"{self.T6_DISAMBIGUATION_MAX_SIGMA_MS:.3f} ms — "
                            f"cannot use as reference.  Calibrator will "
                            f"accept raw value as-is (likely a wrap error)."
                        )
        except (FileNotFoundError, OSError,
                subprocess.SubprocessError, ValueError, IndexError) as e:
            logger.debug(f"T4 chrony tracking unavailable: {e}")

        return None

    def attach_lb1421_probe(self, probe) -> None:
        """Inject an Lb1421T5Probe for use by T5 disambiguation.

        Called once at startup by the entrypoint, after the probe has
        been instantiated and started.  Stored as a member; consulted
        by the BPSK PPS disambiguation path *before* the T4/T3 hierarchy.
        Pass None (or never call) to disable T5 — the disambig will
        fall through to the existing chronyc-tracking path.
        """
        self._lb1421_probe = probe

    def _t5_implied_effective_chain_delay(self) -> Optional[float]:
        """Return T5-derived effective chain_delay (ns) for the current
        matched-filter edge, or None if T5 is unavailable.

        Used by step-recovery's sanity check: when a 60-rejection
        cluster looks like a new physical operating point, this helper
        computes what the chain_delay *would* be if we disambiguated
        the new lock against GPS truth right now.  If that value
        differs from the existing locked chain_delay by more than a
        few ms, the "step" is almost certainly phantom (packet-loss
        zero-fill, MF sidelobe at ±0.5 s) and step-recovery should
        refuse to clear the lock.

        Does NOT modify any state.  Returns None on any of:
          - T5 probe not attached
          - No fresh / valid NMEA reading
          - Calibrator has no recent edge
          - rtp_to_wallclock returns None
          - Host-clock anchor and NMEA disagree by more than ±0.5 s
            (pairing too ambiguous to trust)
        """
        # Defensive: some unit tests bypass __init__ via __new__, so
        # _lb1421_probe may not be defined.  Treat absence as "not wired".
        probe = getattr(self, '_lb1421_probe', None)
        if probe is None:
            return None
        reading = probe.get_latest()
        if reading is None:
            return None
        last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
        if last_edge_rtp is None or self._t6_channel_info is None:
            return None
        try:
            self._t6_channel_info.chain_delay_correction_ns = None
            from ka9q.rtp_recorder import rtp_to_wallclock
            raw_wall_time_sec = rtp_to_wallclock(
                last_edge_rtp, self._t6_channel_info
            )
            if raw_wall_time_sec is None:
                return None
            delta_sec = raw_wall_time_sec - reading.pps_utc_sec
            if abs(delta_sec) > 0.5:
                return None
            return (raw_wall_time_sec - reading.pps_utc_sec) * 1e9
        except Exception:
            return None

    def _t6_disambiguate_via_t5_lb1421(self, result) -> bool:
        """Disambiguate against T5 (LB-1421 GPSDO NMEA over USB-CDC).

        Returns True on success, False if T5 is unavailable, the
        latest reading is stale or has no fix, or the host-clock
        anchor is so divergent from NMEA that PPS-edge pairing would
        be unsafe (>±0.5 s).  On success sets
        ``self._t6_disambiguation_ns`` such that
        ``raw + disambig = (raw_wall_time_at_edge − NMEA_UTC) * 1e9``,
        i.e., the physical RF chain_delay derived without consulting
        the host system clock as a timing source.

        Unlike the integer-sample-shift path, this is a *direct
        measurement*: NMEA tells us the GPS second of the most-recent
        PPS edge, the MF tells us the RTP-position of that edge, and
        their wall-time difference IS the chain_delay.  No
        ``round(disagreement * sr)`` step, hence no inherited
        reference noise.
        """
        # Defensive lazy-init: some unit tests bypass __init__ via __new__.
        probe = getattr(self, '_lb1421_probe', None)
        if probe is None:
            return False
        reading = probe.get_latest()
        if reading is None:
            logger.debug(
                "T6 T5 disambig: no fresh LB-1421 NMEA reading "
                "(stale, no fix, or device closed)"
            )
            return False
        try:
            last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
            if last_edge_rtp is None or self._t6_channel_info is None:
                return False
            self._t6_channel_info.chain_delay_correction_ns = None
            from ka9q.rtp_recorder import rtp_to_wallclock
            raw_wall_time_sec = rtp_to_wallclock(
                last_edge_rtp, self._t6_channel_info
            )
            if raw_wall_time_sec is None:
                return False
            # Pairing sanity: the host-clock anchor used by ka9q's
            # rtp_to_wallclock must place the matched-filter edge
            # within ±0.5 s of NMEA's claimed PPS UTC.  Beyond that the
            # ambiguity is no longer "off by an integer second" but
            # "off by an unknown N seconds" — fall back.
            delta_sec = raw_wall_time_sec - reading.pps_utc_sec
            if abs(delta_sec) > 0.5:
                logger.warning(
                    f"T6 T5 disambig: host-clock anchor "
                    f"({raw_wall_time_sec:.3f}) and NMEA PPS UTC "
                    f"({reading.pps_utc_sec}) differ by "
                    f"{delta_sec:+.3f} s — too wide for unambiguous "
                    f"pairing.  Falling back to T4."
                )
                return False
            # Physical chain_delay = (raw_wall_time − true_PPS_UTC).
            effective_chain_delay_ns = int(round(
                (raw_wall_time_sec - reading.pps_utc_sec) * 1e9
            ))
            # Back-derive disambig shift: effective = raw + disambig.
            self._t6_disambiguation_ns = (
                effective_chain_delay_ns - result.chain_delay_ns
            )
            logger.info(
                f"T6 chain_delay disambiguated against T5 (LB-1421 NMEA): "
                f"raw={result.chain_delay_ns} ns, "
                f"raw_wall_time={raw_wall_time_sec:.6f}, "
                f"NMEA_PPS_UTC={reading.pps_utc_sec}, "
                f"delta={delta_sec*1000:+.3f} ms, "
                f"effective_chain_delay={effective_chain_delay_ns} ns "
                f"(no integer-sample-shift step — direct GPS reference)"
            )
            return True
        except Exception as e:
            logger.warning(
                f"T6 T5 disambig: unexpected error ({e}); "
                f"falling back to T4"
            )
            return False

    def _t6_diff_disambiguate_via_t5_lb1421(
        self, chain_delay_ns_raw: int, raw_wall_time_sec: float,
        edge_rtp: Optional[int] = None,
    ) -> bool:
        """T5 (LB-1421 NMEA) disambiguation for the HFPS / diff path.

        Mirrors :meth:`_t6_disambiguate_via_t5_lb1421` for apples-to-
        apples comparison with HPPS: both detectors anchor their
        one-shot integer-second resolution to the same direct-GPS
        reference, so any HFPS-vs-HPPS difference is attributable to
        the detector (Method 5 vs Method 2), not to the disambig
        reference precision.

        Writes to ``self._t6_diff_disambiguation_ns`` on success.
        """
        probe = getattr(self, '_lb1421_probe', None)
        if probe is None:
            return False
        reading = probe.get_latest()
        if reading is None:
            logger.debug(
                "HFPS T5 disambig: no fresh LB-1421 NMEA reading "
                "(stale, no fix, or device closed)"
            )
            return False
        delta_sec = raw_wall_time_sec - reading.pps_utc_sec
        if abs(delta_sec) > 0.5:
            logger.warning(
                f"HFPS T5 disambig: host-clock anchor "
                f"({raw_wall_time_sec:.3f}) and NMEA PPS UTC "
                f"({reading.pps_utc_sec}) differ by "
                f"{delta_sec:+.3f} s — too wide for unambiguous "
                f"pairing.  Falling back to T4."
            )
            return False
        effective_chain_delay_ns = int(round(
            (raw_wall_time_sec - reading.pps_utc_sec) * 1e9
        ))
        self._t6_diff_disambiguation_ns = (
            effective_chain_delay_ns - chain_delay_ns_raw
        )
        # Capture the (NMEA GPS second, BPSK edge RTP) pair for the
        # NMEA-anchored SHM push path.  Subsequent edges count
        # GPS seconds from this pair using GPSDO-accurate RTP deltas,
        # bypassing the ka9q anchor (which has the host-clock-bias-at-
        # refresh-moment baked in and produces the long-run drift).
        if edge_rtp is not None:
            self._t6_diff_M_disambig = int(reading.pps_utc_sec)
            self._t6_diff_edge_rtp_disambig = int(edge_rtp) & 0xFFFFFFFF
        logger.info(
            f"HFPS chain_delay disambiguated against T5 (LB-1421 NMEA): "
            f"raw={chain_delay_ns_raw} ns, "
            f"raw_wall_time={raw_wall_time_sec:.6f}, "
            f"NMEA_PPS_UTC={reading.pps_utc_sec}, "
            f"delta={delta_sec*1000:+.3f} ms, "
            f"effective_chain_delay={effective_chain_delay_ns} ns "
            f"(no integer-sample-shift step — direct GPS reference); "
            f"NMEA-anchored pair: M={self._t6_diff_M_disambig}, "
            f"edge_rtp={self._t6_diff_edge_rtp_disambig}"
        )
        return True

    def _t6_disambiguate_via_external_reference(self, result) -> None:
        """Fallback disambiguation path used when no fresh persisted
        chain_delay is available.  Walks the timing-tier hierarchy
        (T5 > T4 > T3) and sets ``self._t6_disambiguation_ns`` to the
        integer-sample shift that brings the calibrator's implied
        wall-time into agreement with the highest-rank available tier.

        Pre-fresh-persistence-store this was the only path; today it
        runs only on cold deploys, after staleness expiry, or when the
        sample-rate has been changed.  See
        :class:`bpsk_chain_delay_store.ChainDelayStore` for the
        preferred persisted-value path.
        """
        try:
            last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
            if last_edge_rtp is None or self._t6_channel_info is None:
                return
            ref = self._get_disambiguation_reference()
            if ref is None:
                logger.info(
                    "T6 chain_delay initial accept: no usable non-T6 "
                    "timing authority for disambiguation; accepting "
                    "calibrator value as-is"
                )
                return
            ref_offset_ms, ref_sigma_ms, ref_tier = ref
            # Compute raw wall-time of the detected edge WITHOUT ka9q
            # applying chain_delay (kept None on ChannelInfo so the
            # subtraction inside rtp_to_wallclock is a no-op).
            self._t6_channel_info.chain_delay_correction_ns = None
            from ka9q.rtp_recorder import rtp_to_wallclock
            raw_wall_time_sec = rtp_to_wallclock(last_edge_rtp, self._t6_channel_info)
            if raw_wall_time_sec is None:
                return
            wall_time_sec = raw_wall_time_sec - (result.chain_delay_ns / 1e9)
            ref_time = round(wall_time_sec)
            offset_sec = wall_time_sec - ref_time
            # The reference tier's offset_ms is its estimate of
            # (system_clock - true_UTC).  Our wall_time_offset is also
            # that same quantity (modulo BPSK calibration error).
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

    def _wait_for_chrony_settled(self) -> bool:
        """Block until chrony's Last offset has been below
        ``T6_SETTLE_MAX_OFFSET_S`` for ``T6_SETTLE_REQUIRED_CYCLES``
        consecutive readings.  Returns True if chrony settled within
        the timeout, False if we timed out.

        Capturing the T6 channel anchor when chrony is settled means
        the anchor's system_time is within tens of µs of true UTC.
        The sample-clock arithmetic then preserves that relationship
        forever, so Δ tracks chrony's *current* discipline error rather
        than carrying a permanent baseline shift.  See
        docs/TIMING-PIPELINE-WIRING.md §10.3 for the math.

        Silent no-op when chronyc is unavailable — degraded mode,
        logged once.
        """
        import subprocess as _sub
        try:
            _sub.run(
                ['chronyc', '-h'],
                capture_output=True, timeout=2.0,
            )
        except (FileNotFoundError, OSError, _sub.TimeoutExpired):
            logger.warning(
                "T6 settled-capture gate: chronyc unavailable — "
                "anchor will be captured without verification "
                "(ε_0 may be non-zero, V1 not prevented)"
            )
            return False

        consecutive_settled = 0
        wait_start = time.monotonic()
        deadline = wait_start + self.T6_SETTLE_TIMEOUT_SEC
        logger.info(
            f"T6 settled-capture gate: waiting for chrony "
            f"(threshold |Last offset| <= {self.T6_SETTLE_MAX_OFFSET_S*1e6:.0f} µs, "
            f"need {self.T6_SETTLE_REQUIRED_CYCLES} consecutive readings, "
            f"timeout {self.T6_SETTLE_TIMEOUT_SEC:.0f}s)"
        )
        while time.monotonic() < deadline:
            try:
                proc = _sub.run(
                    ['chronyc', '-n', 'tracking'],
                    capture_output=True, text=True, timeout=5.0,
                )
            except (_sub.TimeoutExpired, OSError) as e:
                logger.debug(f"T6 settled-capture: chronyc tracking failed: {e}")
                time.sleep(self.T6_SETTLE_POLL_SEC)
                consecutive_settled = 0
                continue
            if proc.returncode != 0:
                time.sleep(self.T6_SETTLE_POLL_SEC)
                consecutive_settled = 0
                continue

            last_offset = self._parse_chronyc_last_offset(proc.stdout)
            if last_offset is None:
                logger.debug(
                    "T6 settled-capture: could not parse Last offset from "
                    "chronyc tracking output"
                )
                time.sleep(self.T6_SETTLE_POLL_SEC)
                consecutive_settled = 0
                continue

            if abs(last_offset) <= self.T6_SETTLE_MAX_OFFSET_S:
                consecutive_settled += 1
                logger.info(
                    f"T6 settled-capture: chrony Last offset "
                    f"{last_offset*1e6:+.1f} µs OK "
                    f"({consecutive_settled}/{self.T6_SETTLE_REQUIRED_CYCLES})"
                )
                if consecutive_settled >= self.T6_SETTLE_REQUIRED_CYCLES:
                    elapsed = time.monotonic() - wait_start
                    logger.info(
                        f"T6 settled-capture: chrony settled after "
                        f"{elapsed:.1f}s — proceeding to capture anchor"
                    )
                    return True
            else:
                if consecutive_settled > 0:
                    logger.info(
                        f"T6 settled-capture: chrony Last offset "
                        f"{last_offset*1e6:+.1f} µs > threshold; "
                        f"resetting counter"
                    )
                consecutive_settled = 0
            time.sleep(self.T6_SETTLE_POLL_SEC)

        logger.warning(
            f"T6 settled-capture: timeout after "
            f"{self.T6_SETTLE_TIMEOUT_SEC:.0f}s — proceeding with degraded T6 "
            f"(anchor may inherit non-zero ε_0; will be visible as a "
            f"persistent Δ baseline in authority.json)"
        )
        return False

    @staticmethod
    def _parse_chronyc_last_offset(text: str) -> Optional[float]:
        """Parse `chronyc tracking`'s ``Last offset`` line.

        Returns the offset in seconds (float), or None if unparseable.
        The line format is::

            Last offset     : +0.000000231 seconds
        """
        for line in (text or '').splitlines():
            s = line.strip()
            if s.startswith('Last offset'):
                _, _, val = s.partition(':')
                val = val.strip()
                if not val:
                    return None
                token = val.split()[0]
                try:
                    return float(token)
                except ValueError:
                    return None
        return None

    def _t6_timing_poll_loop(self):
        """Refresh the T6 (GPS/RTP) anchor periodically by re-querying
        radiod's status stream.  Closes V1 for the TSL3 SHM path: every
        SHM push uses a < T6_TIMING_POLL_SEC-old anchor instead of the
        startup-frozen ChannelInfo.

        Pattern mirrors stream_recorder_v2.py's timing poll thread —
        we keep our own copy because the T6 stream is constructed
        directly in core_recorder, not through stream_recorder.
        """
        # Seed from the initial ChannelInfo so the first SHM push has
        # a valid anchor before this loop's first iteration completes.
        if self._t6_channel_info is not None:
            seed_gps = getattr(self._t6_channel_info, 'gps_time', None)
            seed_rtp = getattr(self._t6_channel_info, 'rtp_timesnap', None)
            if seed_gps is not None and seed_rtp is not None:
                with self._t6_timing_lock:
                    self._t6_latest_gps_time_ns = int(seed_gps)
                    self._t6_latest_rtp_timesnap = int(seed_rtp)
                logger.info(
                    f"T6 timing anchor seeded: GPS_TIME={seed_gps}, "
                    f"RTP_TIMESNAP={seed_rtp}"
                )

        our_ssrc = getattr(self._t6_channel_info, 'ssrc', None)
        while not self._t6_timing_poll_stop.is_set():
            # Wait first so the seed is the only value for the
            # first T6_TIMING_POLL_SEC seconds (avoids a redundant
            # discover_channels right after _start_t6_stream).
            if self._t6_timing_poll_stop.wait(self.T6_TIMING_POLL_SEC):
                return
            try:
                channels = discover_channels(
                    self.status_address,
                    listen_duration=self.T6_TIMING_POLL_LISTEN_SEC,
                )
            except Exception as e:
                logger.debug(f"T6 timing poll: discover_channels failed: {e}")
                continue

            # Match by SSRC if known, else fall back to frequency.
            fresh = None
            if our_ssrc is not None:
                fresh = channels.get(our_ssrc)
            if fresh is None and self._t6_channel_info is not None:
                target_freq = getattr(self._t6_channel_info, 'frequency', None)
                if target_freq is not None:
                    for info in channels.values():
                        if abs(getattr(info, 'frequency', -1) - target_freq) < 1.0:
                            fresh = info
                            break

            if fresh is None:
                logger.debug("T6 timing poll: T6 channel not found in discovery")
                continue
            gps_ns = getattr(fresh, 'gps_time', None)
            rtp_snap = getattr(fresh, 'rtp_timesnap', None)
            if gps_ns is None or rtp_snap is None:
                continue
            with self._t6_timing_lock:
                self._t6_latest_gps_time_ns = int(gps_ns)
                self._t6_latest_rtp_timesnap = int(rtp_snap)
            # V1 fix layer 2 — anchor-consistency check (Signal A).
            # Done outside the timing lock so the check can't stall the
            # SHM-update path on the rare contention.
            self._t6_check_anchor_consistency(int(gps_ns), int(rtp_snap))
            # V1 fix layer 3 — react to flags raised by Signal A
            # (just now) or Signal B (set asynchronously from the
            # sample callback's _t6_check_delta_breach).
            self._t6_react_to_flags()

    def _t6_check_anchor_consistency(
        self,
        fresh_gps_ns: int,
        fresh_rtp_timesnap: int,
    ) -> None:
        """Layer 2 Signal A — project the captured anchor forward and
        compare to radiod's fresh status reading.

        The captured anchor (``_t6_drift_anchor_gps_ns``,
        ``_t6_drift_anchor_rtp_timesnap``) is set ONCE the first time
        this method runs after the poll thread starts.  Subsequent
        calls project the anchor forward using ``elapsed_gps`` ×
        sample_rate and compare against the freshly-reported
        ``rtp_timesnap``.  In healthy operation the residual is
        bounded by sample-clock quantization + radiod's status-emit
        jitter — a few samples at most.

        A residual above ``T6_ANCHOR_DISCONTINUITY_SAMPLES`` signals
        that either:
          - radiod restarted (RTP counter discontinuity), or
          - the host system clock took a large unannounced step,
            or
          - the captured anchor itself was wrong.

        Layer 2 raises the flag; Layer 3 will react by re-capturing.

        Also catches the simpler case where either counter went
        backwards (clear-cut RTP-namespace change).
        """
        # Anchor not yet seeded — capture this first refresh as the
        # reference point.  The settled-capture gate guarantees this
        # initial reading was taken with ε_0 ≈ 0.
        if (self._t6_drift_anchor_gps_ns is None
                or self._t6_drift_anchor_rtp_timesnap is None):
            self._t6_drift_anchor_gps_ns = fresh_gps_ns
            self._t6_drift_anchor_rtp_timesnap = fresh_rtp_timesnap
            self._t6_drift_last_check_wall = time.monotonic()
            logger.info(
                f"T6 drift monitor: anchor seeded "
                f"(gps_time={fresh_gps_ns}, rtp_timesnap={fresh_rtp_timesnap})"
            )
            return

        self._t6_drift_last_check_wall = time.monotonic()

        # Counter rollback is unambiguous evidence of namespace change.
        if (fresh_gps_ns < self._t6_drift_anchor_gps_ns
                or fresh_rtp_timesnap < self._t6_drift_anchor_rtp_timesnap):
            if not self._t6_drift_flag_anchor_discontinuity:
                logger.warning(
                    f"T6 drift monitor: counter rollback detected "
                    f"(anchor gps={self._t6_drift_anchor_gps_ns}, rtp={self._t6_drift_anchor_rtp_timesnap}; "
                    f"fresh gps={fresh_gps_ns}, rtp={fresh_rtp_timesnap}) — "
                    f"likely radiod restart"
                )
            self._t6_drift_flag_anchor_discontinuity = True
            return

        # We need the T6 sample rate to project the anchor.  The
        # calibrator owns it; if absent, we cannot evaluate the
        # residual but the rollback check above still fires.
        if self._t6_calibrator is None:
            return
        sample_rate = getattr(self._t6_calibrator, 'sample_rate', None)
        if not sample_rate or sample_rate <= 0:
            return

        elapsed_gps_ns = fresh_gps_ns - self._t6_drift_anchor_gps_ns
        expected_rtp_delta = int(round(
            elapsed_gps_ns * sample_rate / 1_000_000_000
        ))
        actual_rtp_delta = fresh_rtp_timesnap - self._t6_drift_anchor_rtp_timesnap
        residual_samples = actual_rtp_delta - expected_rtp_delta
        self._t6_drift_anchor_residual_samples = residual_samples

        # Persistence gate (TSL3 anchor-churn fix — see
        # T6_ANCHOR_DISCONTINUITY_POLLS).  A single discover_channels
        # reading carries hundreds of samples of noise with intermittent
        # outliers of tens of thousands; a genuine radiod restart / clock
        # step is a *permanent* discontinuity that breaches every poll.
        # Flag only after the residual has breached the threshold on
        # T6_ANCHOR_DISCONTINUITY_POLLS consecutive polls — a lone noisy
        # reading is ignored and the anchor stays frozen.
        if abs(residual_samples) > self.T6_ANCHOR_DISCONTINUITY_SAMPLES:
            self._t6_drift_residual_breach_count += 1
            if (self._t6_drift_residual_breach_count
                    >= self.T6_ANCHOR_DISCONTINUITY_POLLS
                    and not self._t6_drift_flag_anchor_discontinuity):
                logger.warning(
                    f"T6 drift monitor: anchor discontinuity raised "
                    f"(residual={residual_samples} samples > "
                    f"{self.T6_ANCHOR_DISCONTINUITY_SAMPLES} on "
                    f"{self._t6_drift_residual_breach_count} consecutive "
                    f"polls, elapsed_gps={elapsed_gps_ns/1e9:.1f}s)"
                )
                self._t6_drift_flag_anchor_discontinuity = True
        else:
            if self._t6_drift_residual_breach_count > 0:
                logger.debug(
                    "T6 drift monitor: anchor residual back within "
                    "threshold after %d consecutive breach(es) — transient "
                    "noise, anchor held",
                    self._t6_drift_residual_breach_count,
                )
            self._t6_drift_residual_breach_count = 0

    def _t6_check_delta_breach(self, delta_ns: int) -> None:
        """Layer 2 Signal B — track sustained |Δ| > threshold.

        Called from ``_process_t6_samples`` immediately after
        ``_t6_last_local_minus_source_ns`` is updated.  Maintains the
        sustained-breach state machine:
          - first sample where |Δ| > threshold: arm the timer.
          - subsequent samples while above threshold: check duration.
          - sample where |Δ| ≤ threshold: clear timer.

        Logs only on flag transitions to keep the journal quiet during
        normal operation.
        """
        now_mono = time.monotonic()
        if abs(delta_ns) > self.T6_DRIFT_HARD_THRESHOLD_NS:
            if self._t6_drift_first_breach_wall is None:
                self._t6_drift_first_breach_wall = now_mono
                # No log here — single breaches are routine on cold
                # start before chrony settles.  We only care about
                # sustained breaches.
                return
            duration = now_mono - self._t6_drift_first_breach_wall
            if (duration >= self.T6_DRIFT_SUSTAINED_SEC
                    and not self._t6_drift_flag_sustained):
                logger.warning(
                    f"T6 drift monitor: sustained Δ breach raised "
                    f"(|Δ|={abs(delta_ns)/1e6:.2f} ms > "
                    f"{self.T6_DRIFT_HARD_THRESHOLD_NS/1e6:.0f} ms for "
                    f"{duration:.0f}s >= {self.T6_DRIFT_SUSTAINED_SEC:.0f}s)"
                )
                self._t6_drift_flag_sustained = True
        else:
            if self._t6_drift_flag_sustained:
                logger.info(
                    f"T6 drift monitor: sustained Δ breach cleared "
                    f"(|Δ|={abs(delta_ns)/1e6:.3f} ms back below threshold)"
                )
            self._t6_drift_first_breach_wall = None
            self._t6_drift_flag_sustained = False

    def _t6_recapture_cooldown_remaining_sec(self) -> Optional[float]:
        """Time (seconds) remaining before a sustained-breach re-capture
        is allowed.  Returns 0 when no cooldown is active; None when no
        prior recapture (cooldown not engaged)."""
        if self._t6_last_recapture_wall is None:
            return None
        elapsed = time.monotonic() - self._t6_last_recapture_wall
        remaining = self.T6_RECAPTURE_COOLDOWN_SEC - elapsed
        return remaining if remaining > 0 else 0.0

    def _t6_react_to_flags(self) -> None:
        """Layer 3 — consume Layer 2 flags and trigger re-capture when
        appropriate.  Runs on the poll thread after every
        _t6_check_anchor_consistency call, so:

          * Signal A (anchor discontinuity) is acted on the same poll
            tick it was raised — bypasses hysteresis.
          * Signal B (sustained breach) is checked every 5 s; hysteresis
            (cooldown + per-hour cap) gates the actual re-capture.

        Discontinuity takes precedence — if both flags are set
        simultaneously (e.g. a radiod restart that also produced a Δ
        spike), the single re-capture clears both.
        """
        if self._t6_drift_flag_anchor_discontinuity:
            self._t6_attempt_recapture(
                reason="anchor_discontinuity",
                bypass_hysteresis=True,
            )
            return
        if self._t6_drift_flag_sustained:
            self._t6_attempt_recapture(
                reason="sustained_breach",
                bypass_hysteresis=False,
            )

    def _t6_attempt_recapture(
        self,
        reason: str,
        *,
        bypass_hysteresis: bool = False,
    ) -> bool:
        """Re-run the settled-capture gate and replace both anchors
        atomically.  Returns True on successful re-capture; False if
        skipped (hysteresis) or failed (chrony not settled, discovery
        timeout, fresh ChannelInfo missing fields).

        The atomic swap relies on Python reference assignment being
        single-bytecode: ``self._t6_channel_info = new_ci`` is a
        STORE_ATTR that the SHM-path reader on the sample thread
        can never observe in a torn state.  Worst case it sees the
        old ChannelInfo for one PPS edge and the new one on the
        next — never a mix.
        """
        now_mono = time.monotonic()

        if not bypass_hysteresis:
            if (self._t6_last_recapture_wall is not None
                    and now_mono - self._t6_last_recapture_wall
                        < self.T6_RECAPTURE_COOLDOWN_SEC):
                # Inside cooldown.  Log once per cooldown window
                # (the flag stays set so this would otherwise spam).
                remaining = (
                    self.T6_RECAPTURE_COOLDOWN_SEC
                    - (now_mono - self._t6_last_recapture_wall)
                )
                logger.debug(
                    "T6 Layer 3: re-capture (reason=%s) suppressed by "
                    "cooldown (%.0fs remaining)", reason, remaining,
                )
                return False
            hour_ago = now_mono - 3600.0
            recent = [t for t in self._t6_recapture_wall_history
                      if t >= hour_ago]
            if len(recent) >= self.T6_RECAPTURE_MAX_PER_HOUR:
                logger.warning(
                    "T6 Layer 3: re-capture (reason=%s) suppressed by "
                    "per-hour cap (%d recaptures in last 60 min) — "
                    "investigate persistent drift source",
                    reason, len(recent),
                )
                return False

        # Re-run the settle gate.  This blocks the poll thread for up
        # to T6_SETTLE_TIMEOUT_SEC (60 s) — acceptable since the SHM
        # update path doesn't depend on the poll thread's freshness
        # during re-capture.
        if not self._wait_for_chrony_settled():
            logger.warning(
                "T6 Layer 3: re-capture requested (reason=%s) but "
                "chrony did not settle within timeout — skipping; "
                "flags remain set, will retry next poll cycle",
                reason,
            )
            return False

        # Fresh discover_channels for our SSRC.
        try:
            channels = discover_channels(
                self.status_address,
                listen_duration=self.T6_TIMING_POLL_LISTEN_SEC,
            )
        except Exception as exc:
            logger.warning(
                "T6 Layer 3: discover_channels failed during "
                "re-capture (reason=%s): %s", reason, exc,
            )
            return False

        our_ssrc = (
            getattr(self._t6_channel_info, 'ssrc', None)
            if self._t6_channel_info is not None else None
        )
        fresh = channels.get(our_ssrc) if our_ssrc is not None else None
        if fresh is None and self._t6_channel_info is not None:
            target_freq = getattr(self._t6_channel_info, 'frequency', None)
            if target_freq is not None:
                for info in channels.values():
                    if abs(getattr(info, 'frequency', -1) - target_freq) < 1.0:
                        fresh = info
                        break
        if fresh is None:
            logger.warning(
                "T6 Layer 3: T6 channel not found in discovery during "
                "re-capture (reason=%s, ssrc=%s)", reason, our_ssrc,
            )
            return False

        fresh_gps_ns = getattr(fresh, 'gps_time', None)
        fresh_rtp_snap = getattr(fresh, 'rtp_timesnap', None)
        if fresh_gps_ns is None or fresh_rtp_snap is None:
            logger.warning(
                "T6 Layer 3: fresh ChannelInfo missing gps_time/"
                "rtp_timesnap during re-capture (reason=%s)", reason,
            )
            return False

        # Snapshot old values for the log line BEFORE the swap.
        old_gps = self._t6_drift_anchor_gps_ns
        old_rtp = self._t6_drift_anchor_rtp_timesnap

        # Build the new ChannelInfo via copy so we don't mutate the
        # in-flight object the SHM path may be reading.  copy.copy()
        # is sufficient — ChannelInfo is a flat dataclass.
        import copy as _copy
        new_ci = _copy.copy(self._t6_channel_info)
        new_ci.gps_time = int(fresh_gps_ns)
        new_ci.rtp_timesnap = int(fresh_rtp_snap)
        # Reference swap — atomic across the GIL.
        self._t6_channel_info = new_ci

        with self._t6_timing_lock:
            self._t6_latest_gps_time_ns = int(fresh_gps_ns)
            self._t6_latest_rtp_timesnap = int(fresh_rtp_snap)
            self._t6_drift_anchor_gps_ns = int(fresh_gps_ns)
            self._t6_drift_anchor_rtp_timesnap = int(fresh_rtp_snap)

        # Clear Layer 2 state — next poll re-evaluates from clean.
        self._t6_drift_flag_anchor_discontinuity = False
        self._t6_drift_flag_sustained = False
        self._t6_drift_first_breach_wall = None
        self._t6_drift_anchor_residual_samples = 0
        self._t6_drift_residual_breach_count = 0

        # Accounting.
        self._t6_recapture_count += 1
        self._t6_last_recapture_wall = now_mono
        self._t6_last_recapture_reason = reason
        self._t6_recapture_wall_history.append(now_mono)

        logger.warning(
            "T6 Layer 3: anchor re-captured (reason=%s, count=%d): "
            "old (gps=%s, rtp=%s) → new (gps=%s, rtp=%s)",
            reason, self._t6_recapture_count,
            old_gps, old_rtp, fresh_gps_ns, fresh_rtp_snap,
        )
        return True

    def _t6_on_samples(self, samples, quality):
        """Sample callback for the BPSK PPS stream — feeds the calibrator."""
        # Defensive lazy-init for unit tests that bypass __init__ via
        # ``CoreRecorderV2.__new__(CoreRecorderV2)``.  In production
        # __init__ has already set these; in tests they are absent and
        # we want the persistence layer to be a no-op rather than to
        # crash the calibrator path.
        if not hasattr(self, '_t6_mf_chain_delay_store'):
            self._t6_mf_chain_delay_store = None
            self._t6_diff_chain_delay_store = None
            self._t6_mf_saves_pending = 0
            self._t6_diff_saves_pending = 0
        # One-shot smoke log on the first batch so the journal records
        # whether quality.last_rtp_timestamp is flowing in shared mode.
        # Same hook helps confirm legacy-mode startup health.
        if not getattr(self, '_t6_first_sample_logged', False):
            mode = 'shared MultiStream' if self._use_shared_multistream else 'dedicated RadiodStream'
            # Dump radiod-granted channel encoding alongside what we asked
            # for. The two can differ — radiod silently downgrades some IQ
            # configurations (high sample rate + wide filter) from F32 to S16.
            # Pre-ka9q-python 3.14.3 this caused parse_rtp_samples to decode
            # the bytes with the wrong dtype and produce NaN-poisoned input
            # (root cause of TSL3-dark on bee1 2026-05-15). Fixed upstream;
            # this log line is kept so the next time the encodings disagree
            # the journal records it instantly.
            ci = self._t6_channel_info
            requested = self._t6_config.get('encoding', 4)
            granted = getattr(ci, 'encoding', None) if ci is not None else None
            sample_dtype = getattr(samples, 'dtype', None)
            logger.info(
                f"T6 BPSK PPS first samples: {mode}, "
                f"len={len(samples)}, dtype={sample_dtype}, "
                f"last_rtp_timestamp={getattr(quality, 'last_rtp_timestamp', None)}, "
                f"requested_encoding={requested}, granted_encoding={granted}, "
                f"channel_info={ci}"
            )
            self._t6_first_sample_logged = True

        # Diagnostic — count NaN / inf in upstream samples.
        # T6 has gone repeatedly dark with phase_rad=+nan tracing back to
        # NaN in the IQ input itself; this counter pinpoints when and how
        # often. Logged on state transition (clean→bad, bad→clean) plus a
        # periodic summary every 60 s while bad, so the journal isn't
        # flooded but every NaN onset is captured. Remove once root cause
        # is identified and fixed upstream.
        if len(samples) > 0:
            import numpy as _np
            n_total = 2 * len(samples)
            re = samples.real
            im = samples.imag
            n_nan = int(_np.sum(_np.isnan(re))) + int(_np.sum(_np.isnan(im)))
            n_inf = int(_np.sum(_np.isinf(re))) + int(_np.sum(_np.isinf(im)))
            bad_now = (n_nan + n_inf) > 0
            prev_bad = getattr(self, '_t6_input_bad', False)
            prev_summary_wall = getattr(self, '_t6_input_summary_wall', 0.0)
            wall = time.monotonic()
            if bad_now and not prev_bad:
                amp = _np.abs(samples)
                finite_amp = amp[_np.isfinite(amp)]
                amp_min = float(finite_amp.min()) if finite_amp.size else float('nan')
                amp_max = float(finite_amp.max()) if finite_amp.size else float('nan')
                logger.warning(
                    f"T6 input BAD onset: nan={n_nan}/{n_total} inf={n_inf}/{n_total} "
                    f"len={len(samples)} ssrc={getattr(quality, 'ssrc', None)} "
                    f"rtp={getattr(quality, 'last_rtp_timestamp', None)} "
                    f"finite_amp_min={amp_min:.3g} max={amp_max:.3g}"
                )
                self._t6_input_summary_wall = wall
            elif (not bad_now) and prev_bad:
                logger.info(
                    f"T6 input CLEAN resumed: len={len(samples)} "
                    f"ssrc={getattr(quality, 'ssrc', None)} "
                    f"rtp={getattr(quality, 'last_rtp_timestamp', None)}"
                )
            elif bad_now and (wall - prev_summary_wall) >= 60.0:
                logger.warning(
                    f"T6 input still BAD: nan={n_nan}/{n_total} inf={n_inf}/{n_total} "
                    f"len={len(samples)} ssrc={getattr(quality, 'ssrc', None)} "
                    f"rtp={getattr(quality, 'last_rtp_timestamp', None)}"
                )
                self._t6_input_summary_wall = wall
            self._t6_input_bad = bad_now

        result = self._t6_calibrator.process_samples(
            samples, quality.last_rtp_timestamp
        )

        # Differential-detector sidecar (offline A/B analysis).
        # Failures here MUST NOT affect main calibrator state;
        # swallow exceptions and log once per service lifetime.
        # Use getattr so test fixtures that bypass __init__ stay safe.
        diff_cal = getattr(self, '_t6_diff_calibrator', None)
        if diff_cal is not None:
            try:
                diff_cal.process_samples(
                    samples, quality.last_rtp_timestamp
                )
            except Exception as e:
                if not getattr(self, '_t6_diff_warned', False):
                    logger.warning(
                        f"T6 diff-detector sidecar failed (will be "
                        f"silent for remaining batches): {e}",
                        exc_info=True,
                    )
                    self._t6_diff_warned = True

        # Stuck-recovery: cascade gate in the MF calibrator can keep
        # pps_consecutive pinned at 0 indefinitely if the underlying
        # operating point genuinely moved (e.g., Costas walked to a
        # different π-stable lock).  result.locked stays False, the
        # wrap-rejection / step-recovery branch is gated on
        # result.locked and never fires.  Detect this by tracking
        # wall time since the last locked cycle: if it exceeds the
        # timeout while we had previously been locked, reset the
        # calibrator + disambiguation state so the next cycle hits
        # initial-accept at the current peak position.
        wall_now = time.monotonic()
        if result is not None and result.locked:
            self._t6_last_locked_wall = wall_now
        elif self._t6_last_locked_wall is None:
            # First sample after init — start the timer.
            self._t6_last_locked_wall = wall_now
        elif (self._t6_last_chain_delay_ns is not None
                and (wall_now - self._t6_last_locked_wall)
                    > self.T6_STUCK_TIMEOUT_SEC):
            stuck_for = wall_now - self._t6_last_locked_wall
            logger.warning(
                f"T6 calibrator stuck unlocked for {stuck_for:.1f}s "
                f"(> {self.T6_STUCK_TIMEOUT_SEC:.0f}s threshold). "
                f"Resetting calibrator + disambiguation; will re-acquire "
                f"at current operating point."
            )
            self._t6_calibrator.reset()
            self._t6_last_chain_delay_ns = None
            self._t6_disambiguation_ns = 0
            self._t6_wrap_rejections = 0
            self._t6_recent_raw.clear()
            self._t6_last_locked_wall = wall_now
            # Persisted effective chain_delay reflected the old
            # operating point that just got rejected; clear it so the
            # next initial-accept re-disambiguates from scratch.
            if self._t6_mf_chain_delay_store is not None:
                try:
                    self._t6_mf_chain_delay_store.path.unlink(missing_ok=True)
                except OSError:
                    pass

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
                # real PPS edge.
                #
                # Preferred path: load the last-known-good *effective*
                # chain_delay from disk and compute the integer-sample
                # shift that aligns the new raw value with it.  The
                # physical RF path is invariant across restarts, so the
                # effective chain_delay is too — using the persisted
                # value avoids re-walking chrony's transient state and
                # eliminates the per-restart drift (635 µs spread across
                # three restarts observed on bee1 2026-05-21).  See
                # bpsk_chain_delay_store.py.
                #
                # Fallback path (no fresh persisted value): compute the
                # integer-sample shift that would move corrected
                # wall_time into agreement with the highest-rank-
                # available non-T6 timing-authority tier (T5 > T4 > T3).
                # Per the timing model the system clock is downstream
                # of the authority hierarchy, not a peer source — using
                # it for disambiguation would be circular.  Sigma
                # sanity check: reject any reference whose sigma is
                # larger than the half-second-wrap value we're trying
                # to disambiguate against (250 ms).
                sr_local = self._t6_calibrator.sample_rate
                persisted = (
                    self._t6_mf_chain_delay_store.load()
                    if self._t6_mf_chain_delay_store is not None
                    else None
                )
                if persisted is not None and persisted.sample_rate == sr_local:
                    from .bpsk_chain_delay_store import compute_disambiguation_ns
                    self._t6_disambiguation_ns = compute_disambiguation_ns(
                        raw_chain_delay_ns=result.chain_delay_ns,
                        persisted_effective_chain_delay_ns=persisted.effective_chain_delay_ns,
                        sample_rate=sr_local,
                    )
                    age_s = time.time() - persisted.saved_at_unix
                    logger.info(
                        f"T6 chain_delay disambiguated against persisted "
                        f"effective={persisted.effective_chain_delay_ns} ns "
                        f"({age_s:.0f}s old): raw={result.chain_delay_ns} ns, "
                        f"shifting {self._t6_disambiguation_ns} ns "
                        f"(skipping T4 chrony walk — invariant RF path)"
                    )
                else:
                    if persisted is not None:
                        logger.warning(
                            f"T6 chain_delay persisted sample_rate "
                            f"{persisted.sample_rate} != current {sr_local}; "
                            f"falling back to disambiguation hierarchy"
                        )
                    # T5 (LB-1421 NMEA over USB) — direct GPS reference,
                    # no chrony detour.  Falls through to T4 if T5 isn't
                    # wired or the reading is unavailable.
                    if not self._t6_disambiguate_via_t5_lb1421(result):
                        self._t6_disambiguate_via_external_reference(result)
                # Apply disambiguation (set above either way) and lock in.
                effective = result.chain_delay_ns + self._t6_disambiguation_ns
                self._t6_last_chain_delay_ns = effective
                effective_chain_delay = effective
                # Persist the just-locked effective value so the next
                # restart skips disambiguation entirely.
                if self._t6_mf_chain_delay_store is not None:
                    self._t6_mf_chain_delay_store.save(
                        sample_rate=sr_local,
                        effective_chain_delay_ns=effective,
                    )
                    self._t6_mf_saves_pending = 0
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
                self._t6_recent_raw.append(result.chain_delay_ns)
                # Step-recovery: if rejected raws cluster tightly across a
                # full window, the calibrator has truly re-locked at a new
                # operating point (not a transient noise wrap).  Drop the
                # disambiguation/lock state so the next cycle hits
                # initial-accept and re-references against the timing
                # tier hierarchy.
                if (len(self._t6_recent_raw) >= self.T6_STEP_RECOVERY_WINDOW
                        and (max(self._t6_recent_raw) - min(self._t6_recent_raw))
                            < self.T6_STEP_RECOVERY_TIGHT_NS):
                    spread_ns = max(self._t6_recent_raw) - min(self._t6_recent_raw)
                    median_raw = sorted(self._t6_recent_raw)[
                        self.T6_STEP_RECOVERY_WINDOW // 2
                    ]
                    # T5 sanity check (NEW 2026-05-23): step-recovery's
                    # tight-cluster rule is fooled by packet-loss
                    # zero-fill regions that produce phantom edges
                    # ~0.5 s away from the real polarity flip (the MF's
                    # boxcar template has a sidelobe at ±0.5 s).  On
                    # 2026-05-23 10:10 UTC this caused HPPS to walk
                    # 216 ms after a "Lost packet recovery: gap=11520
                    # samples" event.  With T5 wired we can verify the
                    # new operating point against GPS truth before
                    # accepting it.
                    t5_implied = self._t5_implied_effective_chain_delay()
                    if (t5_implied is not None
                            and self._t6_last_chain_delay_ns is not None
                            and abs(t5_implied - self._t6_last_chain_delay_ns)
                                > self.T6_STEP_RECOVERY_T5_SANITY_NS):
                        # T5 says the physical chain_delay has NOT
                        # actually changed; the cluster is a phantom.
                        # Reject the step-recovery and keep the lock.
                        logger.warning(
                            f"T6 step-recovery REJECTED by T5 sanity: "
                            f"candidate would set effective ~ "
                            f"{t5_implied:.0f} ns, old locked = "
                            f"{self._t6_last_chain_delay_ns} ns, "
                            f"disagreement = "
                            f"{t5_implied - self._t6_last_chain_delay_ns:+.0f} ns "
                            f"(threshold ±{self.T6_STEP_RECOVERY_T5_SANITY_NS} ns). "
                            f"Phantom edge from packet-loss zero-fill or "
                            f"matched-filter sidelobe.  Holding old lock; "
                            f"clearing recent_raw to give the calibrator "
                            f"a fresh window to relock on the true edge."
                        )
                        self._t6_recent_raw.clear()
                        self._t6_wrap_rejections = 0
                        effective_chain_delay = self._t6_last_chain_delay_ns
                    else:
                        if t5_implied is None:
                            sanity_msg = "T5 unavailable"
                        else:
                            sanity_msg = (
                                f"T5 confirms: candidate effective "
                                f"~ {t5_implied:.0f} ns vs old "
                                f"{self._t6_last_chain_delay_ns} ns "
                                f"(within ±{self.T6_STEP_RECOVERY_T5_SANITY_NS} ns)"
                            )
                        logger.warning(
                            f"T6 chain_delay step accepted after "
                            f"{self.T6_STEP_RECOVERY_WINDOW} consistent rejections "
                            f"(spread={spread_ns} ns < "
                            f"{self.T6_STEP_RECOVERY_TIGHT_NS} ns, "
                            f"median raw={median_raw} ns, "
                            f"old locked={self._t6_last_chain_delay_ns} ns; "
                            f"{sanity_msg}). "
                            f"Resetting lock for re-disambiguation on next cycle."
                        )
                        effective_chain_delay = self._t6_last_chain_delay_ns
                        self._t6_last_chain_delay_ns = None
                        self._t6_disambiguation_ns = 0
                        self._t6_wrap_rejections = 0
                        self._t6_recent_raw.clear()
                        # Persisted effective chain_delay reflected the old
                        # operating point that the step-recovery just admitted
                        # was stale.  Clear it so the next initial-accept
                        # re-disambiguates from scratch instead of re-applying
                        # the previous (now-wrong) shift.
                        if self._t6_mf_chain_delay_store is not None:
                            try:
                                self._t6_mf_chain_delay_store.path.unlink(missing_ok=True)
                            except OSError:
                                pass
                else:
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
                self._t6_recent_raw.clear()
                effective_chain_delay = self._t6_last_chain_delay_ns
                # Refresh persisted effective chain_delay every
                # T6_PERSIST_EVERY_N_EDGES accepted cycles.  Disk I/O
                # cadence ~ once/minute at 1 PPS — bounded even if the
                # rate later climbs.
                if self._t6_mf_chain_delay_store is not None:
                    self._t6_mf_saves_pending += 1
                    if self._t6_mf_saves_pending >= self.T6_PERSIST_EVERY_N_EDGES:
                        self._t6_mf_chain_delay_store.save(
                            sample_rate=self._t6_calibrator.sample_rate,
                            effective_chain_delay_ns=self._t6_last_chain_delay_ns,
                        )
                        self._t6_mf_saves_pending = 0

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
            #
            # V1 fix work-in-progress: path 2a option 2 (buffer_timing with
            # fresh anchor from _t6_timing_poll_loop) produced jittery Δ
            # values in 2026-05-11 testing.  Reverted to the known-good
            # ka9q.rtp_to_wallclock call for now; the poll-thread
            # infrastructure stays in place for diagnostic use while we
            # investigate the jitter.  See docs/TIMING-PIPELINE-WIRING.md
            # §10.3 for current status.
            if self._t6_shm is not None and self._t6_channel_info is not None:
                try:
                    last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
                    edge_advanced = (
                        last_edge_rtp is not None
                        and last_edge_rtp != self._t6_last_pushed_rtp
                    )
                    if edge_advanced:
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
                            # Δ = the residual chrony will observe as the
                            # TSL3 offset.  Stored for the BpskPpsProbe to
                            # forward via authority.json — Pattern B.
                            self._t6_last_local_minus_source_ns = int(round(
                                (wall_time_sec - ref_time) * 1e9
                            ))
                            # Append to rolling histories for σ computation.
                            # chain_delay_ns is the BPSK matched-filter
                            # edge-position estimate — its std across the
                            # window IS the observed BPSK jitter that
                            # BpskPpsProbe forwards as authority t6_sigma_ms.
                            # local_minus_source_ns is logged alongside for
                            # diagnostics (it captures post-anchor-frozen
                            # computation stability, near-zero in normal
                            # operation; NOT a useful σ signal).
                            self._t6_chain_delay_history.append(
                                float(effective_chain_delay)
                            )
                            self._t6_local_minus_source_history.append(
                                self._t6_last_local_minus_source_ns
                            )
                            # V1 fix layer 2 — Signal B (sustained Δ breach).
                            self._t6_check_delta_breach(
                                self._t6_last_local_minus_source_ns
                            )
                            # precision -14 = 61 us, matches T6 sigma claim of 50 us
                            self._t6_shm.update(
                                reference_time=float(ref_time),
                                system_time=wall_time_sec,
                                precision=-14,
                            )
                            self._t6_last_pushed_rtp = last_edge_rtp
                            self._t6_shm_push_count += 1
                            self._t6_shm_last_push_wall = time.monotonic()
                except Exception as e:
                    # SHM push is non-fatal — log once per ~60 s of failures
                    if not getattr(self, '_t6_shm_warned', False):
                        logger.warning(f"T6 HPPS SHM push failed: {e}")
                        self._t6_shm_warned = True

            # HFPS SHM feed (unit 3): push wall-time of the diff
            # detector's (Method 5) latest accepted edge.  Runs in
            # parallel with TSL3 above so chrony has both refclocks
            # to choose between via its selection algorithm.  Uses
            # the SAME channel_info (and therefore the same RTP →
            # wall-time mapping) as TSL3; the difference is only in
            # which detector produced `last_edge_rtp`.
            diff_cal = getattr(self, '_t6_diff_calibrator', None)
            diff_shm = getattr(self, '_t6_diff_shm', None)
            if (diff_cal is not None and diff_shm is not None
                    and self._t6_channel_info is not None):
                try:
                    diff_last_edge_rtp_full = getattr(
                        diff_cal, 'chain_delay_samples', None
                    )  # used for status; not the rtp itself
                    diff_last_edge_rtp = getattr(
                        diff_cal, '_last_edge_rtp', None
                    )
                    diff_edge_advanced = (
                        diff_last_edge_rtp is not None
                        and diff_last_edge_rtp != self._t6_diff_last_pushed_rtp
                    )
                    if diff_edge_advanced:
                        self._t6_channel_info.chain_delay_correction_ns = None
                        from ka9q.rtp_recorder import rtp_to_wallclock
                        # Step 1: rtp_to_wallclock gives the local
                        # wall time of the sample where we OBSERVED
                        # the polarity flip.
                        raw_wall_time_sec = rtp_to_wallclock(
                            diff_last_edge_rtp, self._t6_channel_info
                        )
                        if raw_wall_time_sec is not None:
                            sr_local = diff_cal.sample_rate
                            chain_delay_ns_raw = int(round(
                                diff_cal.chain_delay_samples
                                * 1e9 / sr_local
                            ))

                            # Step 2: one-shot disambiguation on the
                            # FIRST accepted diff edge.  Mirrors the
                            # legacy MF's initial-accept logic.
                            #
                            # Preferred path: load the last-known-good
                            # *effective* chain_delay from disk and
                            # compute the integer-sample shift that
                            # aligns the new raw value with it.  The
                            # physical RF path is invariant across
                            # restarts; this avoids the per-restart
                            # drift that chrony-state-based
                            # disambiguation suffers from.
                            #
                            # Fallback path: walk the timing-tier
                            # hierarchy (T4 LAN GPS, then T3 fusion).
                            if not self._t6_diff_disambiguated:
                                persisted = (
                                    self._t6_diff_chain_delay_store.load()
                                    if self._t6_diff_chain_delay_store is not None
                                    else None
                                )
                                if (persisted is not None
                                        and persisted.sample_rate == sr_local):
                                    from .bpsk_chain_delay_store import (
                                        compute_disambiguation_ns,
                                    )
                                    self._t6_diff_disambiguation_ns = (
                                        compute_disambiguation_ns(
                                            raw_chain_delay_ns=chain_delay_ns_raw,
                                            persisted_effective_chain_delay_ns=(
                                                persisted.effective_chain_delay_ns
                                            ),
                                            sample_rate=sr_local,
                                        )
                                    )
                                    self._t6_diff_disambiguated = True
                                    age_s = time.time() - persisted.saved_at_unix
                                    logger.info(
                                        f"HFPS chain_delay disambiguated against "
                                        f"persisted effective="
                                        f"{persisted.effective_chain_delay_ns} ns "
                                        f"({age_s:.0f}s old): raw="
                                        f"{chain_delay_ns_raw} ns, shifting "
                                        f"{self._t6_diff_disambiguation_ns} ns "
                                        f"(skipping T4 chrony walk)"
                                    )
                                    # Eager refresh — the new
                                    # disambiguation should survive a
                                    # crash within the first minute.
                                    if self._t6_diff_chain_delay_store is not None:
                                        self._t6_diff_chain_delay_store.save(
                                            sample_rate=sr_local,
                                            effective_chain_delay_ns=(
                                                chain_delay_ns_raw
                                                + self._t6_diff_disambiguation_ns
                                            ),
                                        )
                                        self._t6_diff_saves_pending = 0
                                else:
                                    if persisted is not None:
                                        logger.warning(
                                            f"HFPS persisted sample_rate "
                                            f"{persisted.sample_rate} != current "
                                            f"{sr_local}; falling back to T5/T4"
                                        )
                                    # T5 (LB-1421 NMEA) — direct GPS reference,
                                    # mirroring the HPPS path so HFPS vs HPPS
                                    # is a clean detector-only comparison.
                                    # Falls through to T4 if T5 isn't wired
                                    # or its reading is unavailable.
                                    if self._t6_diff_disambiguate_via_t5_lb1421(
                                            chain_delay_ns_raw, raw_wall_time_sec,
                                            edge_rtp=diff_last_edge_rtp,
                                    ):
                                        self._t6_diff_disambiguated = True
                                        # Eager refresh — disambiguation should
                                        # survive a crash within the first minute.
                                        if self._t6_diff_chain_delay_store is not None:
                                            self._t6_diff_chain_delay_store.save(
                                                sample_rate=sr_local,
                                                effective_chain_delay_ns=(
                                                    chain_delay_ns_raw
                                                    + self._t6_diff_disambiguation_ns
                                                ),
                                            )
                                            self._t6_diff_saves_pending = 0
                                    else:
                                        ref = self._get_disambiguation_reference()
                                        if ref is None:
                                            logger.info(
                                                "HFPS chain_delay initial accept: "
                                                "no usable non-T6 timing authority "
                                                "for disambiguation; accepting raw "
                                                "value"
                                            )
                                            self._t6_diff_disambiguation_ns = 0
                                            self._t6_diff_disambiguated = True
                                        else:
                                            ref_offset_ms, ref_sigma_ms, ref_tier = ref
                                            wall_time_sec_initial = (
                                                raw_wall_time_sec
                                                - chain_delay_ns_raw / 1e9
                                            )
                                            ref_time_initial = round(
                                                wall_time_sec_initial
                                            )
                                            offset_sec_initial = (
                                                wall_time_sec_initial - ref_time_initial
                                            )
                                            disagreement_sec = (
                                                offset_sec_initial
                                                - ref_offset_ms / 1000.0
                                            )
                                            shift_samples = round(
                                                disagreement_sec * sr_local
                                            )
                                            self._t6_diff_disambiguation_ns = int(round(
                                                shift_samples * 1e9 / sr_local
                                            ))
                                            self._t6_diff_disambiguated = True
                                            logger.info(
                                                f"HFPS chain_delay disambiguated "
                                                f"against {ref_tier} "
                                                f"(offset={ref_offset_ms:+.3f} ms, "
                                                f"sigma={ref_sigma_ms:.3f} ms): "
                                                f"raw_chain_delay={chain_delay_ns_raw} "
                                                f"ns; disagreement "
                                                f"{disagreement_sec*1000:+.3f} ms; "
                                                f"shift {shift_samples} samples "
                                                f"({self._t6_diff_disambiguation_ns} ns)"
                                            )
                                            # Eager refresh so the next
                                            # restart inherits the T4-derived
                                            # shift instead of re-walking
                                            # chrony from a different state.
                                            if self._t6_diff_chain_delay_store is not None:
                                                self._t6_diff_chain_delay_store.save(
                                                    sample_rate=sr_local,
                                                    effective_chain_delay_ns=(
                                                        chain_delay_ns_raw
                                                        + self._t6_diff_disambiguation_ns
                                                    ),
                                                )
                                                self._t6_diff_saves_pending = 0

                            # Step 3: compute system_time for chrony.
                            #
                            # NMEA-anchored path (Option 3 architectural
                            # fix, 2026-05-23 PM session): if a T5
                            # disambig captured the (M_disambig,
                            # edge_rtp_disambig) pair, derive M_edge by
                            # edge-counting on GPSDO-accurate RTP deltas
                            # and compute the host clock at edge sample
                            # FRESH (clock_gettime now − RTP-elapsed)
                            # rather than via the stale ka9q anchor.
                            # Eliminates the anchor-drift artifact (see
                            # project_hf_pps_t5_direct_2026-05-23).
                            #
                            # Fallback: if M_disambig is None (T5 failed
                            # at startup; only T4/persisted path
                            # available), use the legacy anchor-based
                            # math.  Keeps a working SHM feed even when
                            # NMEA is unavailable.
                            effective_chain_delay_ns = (
                                chain_delay_ns_raw
                                + self._t6_diff_disambiguation_ns
                            )
                            if self._t6_diff_M_disambig is not None:
                                rtp_now = int(quality.last_rtp_timestamp)
                                rtp_delta_to_edge = (
                                    (rtp_now - diff_last_edge_rtp)
                                    & 0xFFFFFFFF
                                )
                                if rtp_delta_to_edge > 0x7FFFFFFF:
                                    rtp_delta_to_edge -= 0x100000000
                                gps_elapsed_since_edge = (
                                    rtp_delta_to_edge / sr_local
                                )
                                T_sys_now = time.time()
                                T_sys_at_edge_acq = (
                                    T_sys_now - gps_elapsed_since_edge
                                )
                                edge_rtp_delta_from_disambig = (
                                    (diff_last_edge_rtp
                                     - self._t6_diff_edge_rtp_disambig)
                                    & 0xFFFFFFFF
                                )
                                if edge_rtp_delta_from_disambig > 0x7FFFFFFF:
                                    edge_rtp_delta_from_disambig -= 0x100000000
                                edge_count = int(round(
                                    edge_rtp_delta_from_disambig / sr_local
                                ))
                                M_edge = (
                                    self._t6_diff_M_disambig + edge_count
                                )
                                ref_time = float(M_edge)
                                wall_time_sec = (
                                    T_sys_at_edge_acq
                                    - self._t6_chain_delay_calib_s
                                )
                            else:
                                wall_time_sec = (
                                    raw_wall_time_sec
                                    - effective_chain_delay_ns / 1e9
                                )
                                ref_time = float(round(wall_time_sec))
                            diff_shm.update(
                                reference_time=ref_time,
                                system_time=wall_time_sec,
                                # precision -20 ≈ 1 µs — claim closer to
                                # the observed ~22 ns σ; lets chrony
                                # weight HFPS appropriately.
                                precision=-20,
                            )
                            self._t6_diff_last_pushed_rtp = diff_last_edge_rtp
                            self._t6_diff_shm_push_count += 1
                            # Refresh persisted effective chain_delay
                            # every T6_PERSIST_EVERY_N_EDGES accepted
                            # cycles so the next restart skips
                            # disambiguation.  Disk I/O cadence ~ once
                            # per minute at 1 PPS.
                            if self._t6_diff_chain_delay_store is not None:
                                self._t6_diff_saves_pending += 1
                                if self._t6_diff_saves_pending >= self.T6_PERSIST_EVERY_N_EDGES:
                                    self._t6_diff_chain_delay_store.save(
                                        sample_rate=sr_local,
                                        effective_chain_delay_ns=effective_chain_delay_ns,
                                    )
                                    self._t6_diff_saves_pending = 0
                except Exception as e:
                    if not getattr(self, '_t6_diff_shm_warned', False):
                        logger.warning(
                            f"T6 HFPS SHM push failed (will be silent "
                            f"for remaining batches): {e}",
                            exc_info=True,
                        )
                        self._t6_diff_shm_warned = True

                # Diagnostic: T6 SHM has a known failure mode where chrony
                # reach decays to 0 while the matched filter keeps
                # reporting acquired=1, pps_consec>0 in the journal —
                # observed on bee1 2026-05-12 ~07:01 UTC after ~5h
                # uptime.  The cause isn't yet pinned down (`_last_edge_rtp`
                # SHOULD advance every PPS per bpsk_pps_calibrator_mf.py:477,
                # so the != gate above SHOULD fire).  Emit a periodic
                # log line so the next incident's data tells us whether:
                #   (a) the push code never runs (calibrator stops calling
                #       this callback),
                #   (b) `last_edge_rtp == _t6_last_pushed_rtp` keeps the
                #       gate False (calibrator advance starvation), or
                #   (c) the push runs but chrony rejects (excessive
                #       offset / age).  This logging is paired with the
                #       systemd-side tsl3-watchdog.sh which bounds the
                #       outage at ~3 min until we know which it is.
                now_mono = time.monotonic()
                if now_mono - self._t6_shm_last_log_wall >= self.T6_SHM_LOG_INTERVAL_SEC:
                    elapsed = now_mono - self._t6_shm_last_log_wall
                    elapsed_since_push = (
                        now_mono - self._t6_shm_last_push_wall
                        if self._t6_shm_last_push_wall is not None
                        else None
                    )
                    logger.info(
                        f"T6 SHM diag: pushes_since_last_log="
                        f"{self._t6_shm_push_count - self._t6_shm_last_log_count} "
                        f"(window {elapsed:.0f}s), "
                        f"last_push_age="
                        f"{f'{elapsed_since_push:.1f}s' if elapsed_since_push is not None else 'never'}, "
                        f"last_edge_rtp={last_edge_rtp}, "
                        f"_t6_last_pushed_rtp={self._t6_last_pushed_rtp}, "
                        f"locked={getattr(result, 'locked', None) if result is not None else None}, "
                        f"pps_consec={getattr(result, 'pps_consecutive', None) if result is not None else None}"
                    )
                    self._t6_shm_last_log_count = self._t6_shm_push_count
                    self._t6_shm_last_log_wall = now_mono

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
                    # Costas carrier-recovery loop health (Layer A TSL3
                    # fix).  False during a phase excursion — the
                    # calibrator is coasting on the last-good chain delay
                    # and accepting no edges until the loop re-locks.
                    # None when the legacy (non-MF) calibrator is active.
                    'costas_locked': getattr(
                        self._t6_calibrator, 'costas_locked', None
                    ),
                    'pps_ok': self._t6_calibrator.pps_ok,
                    'pps_noise': self._t6_calibrator.pps_noise,
                    # Off-position (phantom) edges held inert while
                    # acquired — TSL3 displaced-reference fix.  None for
                    # the legacy non-MF calibrator.
                    'pps_phantom': getattr(
                        self._t6_calibrator, 'pps_phantom', None
                    ),
                    'pps_consecutive': self._t6_calibrator.pps_consecutive,
                    'chain_delay_ns': (self._t6_calibrator._chain_delay_samples
                                       * 1_000_000_000 / self._t6_calibrator.sample_rate
                                       if self._t6_calibrator._chain_delay_samples is not None
                                       else None),
                    # Δ = chrony's view of TSL3 offset == local_clock − source_UTC.
                    # The value the BpskPpsProbe forwards as offset_ms.  None
                    # until the first SHM push has happened.
                    'local_minus_source_ns': self._t6_last_local_minus_source_ns,
                    # Observed BPSK matched-filter jitter over the last
                    # ~60 PPS edges (≈1 min at 1 Hz): std of chain_delay_ns.
                    # This IS the physical uncertainty of the BPSK PPS
                    # measurement; BpskPpsProbe uses it as authority
                    # t6_sigma_ms (floored at sigma_floor_ms so calm
                    # windows don't under-claim).  None until ≥2 samples.
                    'chain_delay_ns_std_ns': (
                        float(np.std(
                            list(self._t6_chain_delay_history),
                            ddof=1,
                        ))
                        if len(self._t6_chain_delay_history) >= 2
                        else None
                    ),
                    'chain_delay_ns_window': len(
                        self._t6_chain_delay_history
                    ),
                    # Diagnostic — std of the residual we push to chrony.
                    # Near-zero in normal operation (anchor is frozen,
                    # chrony has the clock disciplined); kept for
                    # debugging and NOT used as the published σ.
                    'local_minus_source_ns_std_ns': (
                        float(np.std(
                            list(self._t6_local_minus_source_history),
                            ddof=1,
                        ))
                        if len(self._t6_local_minus_source_history) >= 2
                        else None
                    ),
                    'local_minus_source_ns_window': len(
                        self._t6_local_minus_source_history
                    ),
                    # V1 fix layer 2 — drift monitor flags.  See
                    # docs/TIMING-PIPELINE-WIRING.md §10.3 and
                    # _t6_check_anchor_consistency / _t6_check_delta_breach.
                    # Forwarded by BpskPpsProbe into authority.json; Layer 3
                    # will consume these to drive re-capture.
                    'drift_monitor': {
                        'sustained_breach': self._t6_drift_flag_sustained,
                        'anchor_discontinuity': self._t6_drift_flag_anchor_discontinuity,
                        'anchor_residual_samples': self._t6_drift_anchor_residual_samples,
                        # Consecutive residual breaches — the persistence
                        # gate's counter (flags only at
                        # T6_ANCHOR_DISCONTINUITY_POLLS).
                        'residual_breach_count': self._t6_drift_residual_breach_count,
                        'breach_duration_sec': (
                            round(time.monotonic() - self._t6_drift_first_breach_wall, 1)
                            if self._t6_drift_first_breach_wall is not None else None
                        ),
                        'last_check_age_sec': (
                            round(time.monotonic() - self._t6_drift_last_check_wall, 1)
                            if self._t6_drift_last_check_wall is not None else None
                        ),
                        'hard_threshold_ns': self.T6_DRIFT_HARD_THRESHOLD_NS,
                        'sustained_threshold_sec': self.T6_DRIFT_SUSTAINED_SEC,
                        'anchor_discontinuity_samples_threshold':
                            self.T6_ANCHOR_DISCONTINUITY_SAMPLES,
                        # V1 fix layer 3 — re-capture state.
                        'recapture_count': self._t6_recapture_count,
                        'last_recapture_age_sec': (
                            round(time.monotonic() - self._t6_last_recapture_wall, 1)
                            if self._t6_last_recapture_wall is not None else None
                        ),
                        'last_recapture_reason': self._t6_last_recapture_reason,
                        'recapture_cooldown_remaining_sec': (
                            round(rem, 1)
                            if (rem := self._t6_recapture_cooldown_remaining_sec()) is not None
                            else None
                        ),
                        'recapture_cooldown_sec': self.T6_RECAPTURE_COOLDOWN_SEC,
                        'recapture_max_per_hour': self.T6_RECAPTURE_MAX_PER_HOUR,
                    },
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
    # Attach the T5 disambiguation reference (LB-1421 GPSDO NMEA over
    # USB-CDC), if configured.  Gated by
    #   [timing]
    #   lb1421_nmea_device = "/dev/lb1421-nmea"
    # Pass an empty string or omit the key to disable T5; the disambig
    # will fall back to T4 chronyc tracking as before.
    timing_section = config.get('timing', {})
    lb1421_device = str(timing_section.get('lb1421_nmea_device', '')).strip()
    if lb1421_device:
        from .lb1421_t5_probe import Lb1421T5Probe
        lb1421_probe = Lb1421T5Probe(device=Path(lb1421_device))
        lb1421_probe.start()
        recorder.attach_lb1421_probe(lb1421_probe)
        logger.info(
            f"T5 LB-1421 NMEA probe attached "
            f"(device={lb1421_device}); BPSK PPS disambig will prefer "
            f"GPS direct over chronyc tracking."
        )
    recorder.run()


if __name__ == '__main__':
    main()
