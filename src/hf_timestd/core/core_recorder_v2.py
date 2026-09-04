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
# Module scope: the coast bound is a class attribute, evaluated
# at import time, and must stay tied to the precision field's
# saturation point rather than drifting from it.
from .t6_shm_pair import PRECISION_CEILING
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





def resolve_batch_rtp(quality, _warned=[False]):
    """Truthful RTP label for a delivered sample batch.

    Prefer ``quality.delivered_rtp_start`` (ka9q-python >= 3.21.0: the
    resequencer's true first-sample timestamp, gap fills included).
    Fall back to ``last_rtp_timestamp`` — the last RECEIVED packet's
    header — with a one-time warning: that label desynchronizes from
    the delivered stream under loss (the T6 origin-slip root cause,
    docs/T6-BLOCK-SLIP-ROOT-CAUSE-2026-08-10.md).
    """
    rtp = getattr(quality, 'delivered_rtp_start', None)
    if rtp is not None:
        return rtp
    if not _warned[0]:
        _warned[0] = True
        logging.getLogger(__name__).warning(
            "quality.delivered_rtp_start unavailable (ka9q-python < 3.21.0?) — "
            "falling back to last_rtp_timestamp; batch labels will carry "
            "packet-boundary wobble and slip under stream loss")
    return getattr(quality, 'last_rtp_timestamp', None)



# One PPS period.  The MF reports chain delay as a modular position in
# [0, SR) -- see bpsk_pps_calibrator_mf, which deliberately does NOT wrap and
# delegates absolute resolution to the disambiguation logic here.
_T6_PPS_PERIOD_NS = 1_000_000_000


def format_native_anchor_log(anchor, tier) -> str:
    """The one anchor-capture log shape, shared by every capture path.

    ``scripts/t6_origin_spread.py`` derives origins from these lines and
    needs rtp, utc_ns AND sr from each.  Two of the three capture sites
    used to hand-roll a shorter form — the external-reference lines
    omitted sr, and its "already aligned" variant omitted utc_ns too —
    so those anchors were invisible to the tool.  Formatting in one
    place, pinned by a test against the tool's own regex, keeps producer
    and consumer from drifting apart again.
    """
    return (f"native_anchor: rtp={anchor.anchor_rtp}, "
            f"utc_ns={anchor.anchor_utc_ns}, "
            f"sr={anchor.sample_rate_hz}, tier={tier}")


def newest_sample_rtp(quality) -> Optional[int]:
    """RTP label of the sample just past the newest DELIVERED one.

    ``quality.last_rtp_timestamp`` is the last RECEIVED packet's header,
    stamped before the resequencer runs; ka9q-python added
    ``delivered_rtp_start`` specifically to replace that use for sample
    labelling ("the latter ... desynchronizes from delivered samples
    under loss", stream_quality.py) after the T6 origin slips of
    2026-08-11.  The delivered batch runs from ``delivered_rtp_start``
    for ``batch_samples_delivered`` samples, so its leading edge -- the
    newest sample we actually hold -- is their sum.

    Falls back to the received header when the producer predates the
    field, which is the pre-fix behavior.
    """
    start = getattr(quality, "delivered_rtp_start", None)
    n = getattr(quality, "batch_samples_delivered", 0) or 0
    if start is None:
        last = getattr(quality, "last_rtp_timestamp", None)
        return None if last is None else int(last) & 0xFFFFFFFF
    return (int(start) + int(n)) & 0xFFFFFFFF


def resolve_chain_delay_calib_s(t6_config) -> float:
    """The coarse-path chain delay actually asserted, per the convention.

    ``chain_delay_calib_s`` is the fine stage's ``filter_group_delay_ns``
    in seconds, on the NMEA-named coarse path — the same millisecond-class
    constant, asserted from configuration.  Under CONTENT-time labels the
    only delay between antenna and sample is the µs-class analog path
    (``delay_budget_ns``), so this is not applied; under ``legacy`` it is,
    unchanged.

    Retiring one without the other would be worse than retiring neither:
    the fine stage would label content-true while the coarse stage kept
    labelling 16.6 ms later, so the two planes would disagree by exactly
    the constant the convention exists to remove.
    """
    configured = float((t6_config or {}).get("chain_delay_calib_s", 0.0))
    convention = str(
        (t6_config or {}).get("labeling_convention", "content")
    ).strip().lower()
    return configured if convention == "legacy" else 0.0


def t6_chain_delay_uncalibrated(t6_config) -> bool:
    """True when T6 is enabled but asserts a zero RF chain delay.

    ``chain_delay_calib_s`` is asserted, never derived
    (T6_ORIGIN_ASSERTION_DESIGN §5), and defaults to 0.0.  Unset, it
    claims the TS-1 BPSK modulator -> coax -> RX-888 ADC -> radiod DSP
    -> RTP path takes exactly no time, so the anchor pins to the integer
    second and every label is early by the whole chain delay (B4
    2026-08-15: -16.098 ms against T4, with the capture log recording
    the +15.863 ms it had just measured and discarded).

    Reporting only -- deriving the value here is precisely what the
    assertion design removed.
    """
    if not t6_config.get("enabled", False):
        return False
    convention = str(
        t6_config.get("labeling_convention", "content")).strip().lower()
    if convention != "legacy":
        # Under content labels an unset chain delay is CORRECT — warning
        # about it would train operators to set the retired constant.
        return False
    return float(t6_config.get("chain_delay_calib_s", 0.0)) == 0.0


def _t6_pps_edge_phase_keys(chain_delay_ns) -> dict:
    """Publish where the recovered edge falls inside the named second.

    The number reads about 0.5955 s and measures the coarse cascade's naming
    of the second.  No analogue path in this station spans half a second, so
    the old name `chain_delay_ns` described nothing it held — and it collided
    with the client contract's `RADIOD_<id>_CHAIN_DELAY_NS`, a real per-radiod
    path delay that every timing-critical client subtracts from its UTC.
    Nothing publishes that fact yet, so the collision stays latent; an
    implementer wiring the mechanism as written and reaching for the matching
    name would move every client by 596 ms.

    Both keys go out for one release.  `core-recorder-status.json` serves as a
    published surface, so the old name earns a deprecation window even though
    no consumer outside hf-timestd reads it today.  See
    docs/design/TIMING_PROVENANCE_MODEL.md §4.5.
    """
    return {
        'edge_phase_in_named_second_ns': chain_delay_ns,
        # DEPRECATED — retire one release after 2026-09-01.
        'chain_delay_ns': chain_delay_ns,
    }


def wrap_chain_delay_ns(effective_ns: int) -> int:
    """Fold a chain delay onto the representative nearest zero.

    ``effective = raw + disambiguation_shift`` is modular in the PPS period,
    but the disambiguation shift is bounded to +/-0.5 s by construction
    (``offset_sec = wall - round(wall)``).  So ``effective`` can only reach
    ``raw +/- 500 ms`` while the plausibility guard demands +/-250 ms -- those
    intersect only for ``raw <= 750 ms``.  Above that the guard could never be
    satisfied and T6 refused every locked cycle indefinitely (B4, 2026-08-14:
    raw pinned at 843.4 ms by an RTP-epoch re-base after a radiod restart,
    zero anchors for 4+ hours).

    Folding first makes the guard test the physical quantity it means to test.
    Values already inside the band are returned unchanged.
    """
    half = _T6_PPS_PERIOD_NS // 2
    return ((int(effective_ns) + half) % _T6_PPS_PERIOD_NS) - half


def label_plane_measure_for(timing_section: dict) -> bool:
    """May the judge MEASURE the label→host plane offset?

    Only when the two planes actually differ — i.e. under content-time
    labels.  Under the legacy convention the anchor folds the pipeline
    latency into the label, so the planes coincide; whatever the tracker
    measured there would be T6's own residual, and correcting by it would
    cancel the disagreement the cross-bench gate exists to detect.
    Derived from the labelling convention rather than configured
    separately, so the two cannot drift apart.
    """
    t6 = (timing_section or {}).get('t6_pps', {}) or {}
    convention = str(t6.get('labeling_convention', 'content')).strip().lower()
    return convention != 'legacy'


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
        # A-axis observer; see attach_a_level_provider (#41).
        self._a_level_provider = None
        # Cross-channel counter calibration for ledger v2 (#42/#43).
        self._t6_epoch = None
        self._peer_epoch = None
        self._peer_rate_hz = None
        self._t6_epoch_last_obs = None
        self._t6_last_fine_est = None
        # Pre-trigger IQ ring; off unless configured (#43).
        self._t6_anomaly = None
        # T6-channel block-drop counter: this channel is not
        # archived, so nothing else counts its losses.
        self._t6_zero_fill = None
        self._t6_zero_fill_logged = 0.0
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
                # Prefer new `status` field (RADIOD-IDENTIFICATION.md
                # §3.1); fall back to legacy `status_address` with
                # DeprecationWarning via resolve_ka9q_status.
                from ..config_utils import resolve_ka9q_status
                self.status_address = resolve_ka9q_status(
                    {'ka9q': ka9q_section}) or None

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
        # is canonically ``[timing.t6_pps]``; older deployed configs using
        # the historical ``[timing.l6_pps]`` key still load with a
        # one-time deprecation warning (see config-key backward-compat
        # block in __init__).  Status JSON key is ``t6_pps``.
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

        # Count of HPPS SHM pushes.  The periodic "T6 SHM diag" log line
        # that once paired with it sat inside the diff-sidecar (HFPS)
        # block, so it never ran on any station; it went with that
        # block on 2026-09-04 (RESIDUE_AUDIT §3.4).
        self._t6_shm_push_count = 0
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
        # JSON as t6_pps.chain_delay_ns_std_ns; BpskPpsProbe converts
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
        # hf-timestd-native (RTP, UTC) anchor — the single source of
        # truth for T6 RTP→UTC labelling.  Captured once at first
        # BPSK PPS lock by pairing the matched-filter edge RTP with
        # the LB-1421 USB-NMEA UTC second.  Pure arithmetic from this
        # anchor onward — no host-clock-derived projection, no
        # rtp_to_wallclock chain.  See
        # ``hf_timestd.core.native_anchor`` and the §1 substrate
        # principle in ``docs/ARCHITECTURE-FIRST-PRINCIPLES.md``.
        #
        # Lifecycle: None at startup until either (a) the v2 store
        # restores it or (b) first-lock disambig captures it.
        # Invalidated (set back to None) when the calibrator unlocks
        # or the GPSDO drops to A0 — the next first-lock recaptures.
        # No continuous drift-monitor feedback; the anchor is either
        # valid or it isn't.
        self._t6_native_anchor = None
        # Wrap-rejection guard: the BPSK calibrator algorithm has a known
        # cascade where a noise edge near the half-second mark from a real
        # edge displaces the reference and causes chain_delay to wrap by
        # ~half a second (62.5 us natural sample wobble vs 322 ms wrap).
        # We track the last accepted chain_delay and reject jumps > 1 ms,
        # keeping the previously-good correction in place. Reset to None
        # on calibrator restart so the first stable lock is always accepted.
        self._t6_last_chain_delay_ns = None
        self._t6_wrap_rejections = 0
        # Initial-accept plausibility refusals: consecutive locked cycles
        # where the disambiguated effective chain_delay exceeded the Layer B
        # physical-plausibility bound and the lock was refused.  Used only
        # to rate-limit the refusal warning; reset on every accept.
        self._t6_initial_accept_rejections = 0
        # Wall clock (monotonic) of the last disambiguation walk attempted
        # while in refusal state — throttles the retry loop to
        # T6_DISAMBIG_RETRY_INTERVAL_SEC (initial-accept otherwise re-runs
        # per sample batch, ~25 Hz, each walk shelling out to chronyc).
        self._t6_last_disambig_walk_wall = None
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
        # Physical chain_delay calibration — initialized to 0 here;
        # re-read from self._t6_config after that attribute is assigned
        # just below.
        self._t6_chain_delay_calib_s = 0.0
        # Chain-delay persistence retired on the T6 path (anchor
        # inversion spec §6): under the inversion no ms-scale fitted
        # state exists to persist — the fine stage + authority re-derive
        # the anchor from scratch in ~fine_fold_seconds after every
        # re-lock.  The store module (bpsk_chain_delay_store) went on
        # 2026-09-04 with the diff-detector sidecar, its last user
        # (RESIDUE_AUDIT §3.4).  A leftover store file from a
        # pre-inversion build is ignored (logged once at INFO, not
        # silently — see _t6_on_samples).
        # Durable anchor ledger (t6_anchor_ledger): every captured native
        # anchor, with its raw components, so recalibrating the asserted
        # chain-delay terms later is arithmetic over the ledger instead
        # of a lost cause (journal lines rotate).
        from .t6_anchor_ledger import T6AnchorLedger
        self._t6_anchor_ledger = T6AnchorLedger()
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
        # Config-key backward-compat: the canonical key is
        # [timing.t6_pps] (matching the T-tier authority hierarchy).
        # Older deployed configs still use [timing.l6_pps] (the
        # historical "Level 6" name); accept either, prefer t6_pps,
        # warn once on the legacy form so operators can migrate at
        # their own cadence.
        _timing_section = config.get('timing', {})

        # ── Offset Judge (docs/OFFSET-JUDGE-SPEC-2026-08-05.md, P1) ──
        # One judge per core-recorder process (spec §11: no new daemon).
        # It measures radiod's advertised epoch against the best bench
        # (T4/T2 chrony, T3 FUSE), publishes the per-source offset trend
        # to /run/hf-timestd/offset_judge.json, and supplies the
        # correction that BinaryArchiveWriter applies to labels.
        # Failure to construct/start is NON-FATAL: recording continues
        # with raw radiod mappings (pre-judge behavior).
        self._offset_judge = None
        _oj_cfg = dict(_timing_section.get('offset_judge', {}) or {})
        # The judge may only MEASURE the label→host plane offset when the
        # two planes actually differ — i.e. under content-time labels.
        # Under the legacy convention the anchor folds the pipeline latency
        # into the label, the planes coincide, and whatever the tracker
        # measured would be T6's own residual; correcting by it would
        # cancel the disagreement the cross-bench gate exists to detect.
        # Derived here rather than configured separately so the two cannot
        # drift apart.
        if 'label_plane_measure' not in _oj_cfg:
            _oj_cfg['label_plane_measure'] = label_plane_measure_for(
                _timing_section)
        if _oj_cfg.get('enabled', True):
            try:
                from .offset_judge import OffsetJudge
                self._offset_judge = OffsetJudge(config=_oj_cfg)
                self._offset_judge.start()
            except Exception as e:
                logger.error(
                    f"OffsetJudge init failed (recording continues with "
                    f"raw radiod mappings): {e}", exc_info=True,
                )
                self._offset_judge = None
        else:
            logger.info("OffsetJudge disabled by [timing.offset_judge] config")

        # ── T5 RTP pairing (P2, audit G5b) ──────────────────────────
        # Pairs the LB-142x NMEA integer second against the RTP counter
        # observed on the T6 stream, producing t5_lbe1421.anchor_offset_ns
        # (the field LbeT5DirectProbe consumes — previously never
        # emitted, so the T6↔T5 cross-check compared against a
        # hardcoded 0).  Exists independently of the judge because the
        # AuthorityRunner path consumes it via the status file.
        try:
            from .t5_rtp_pairing import T5RtpPairing
            self._t5_pairing = T5RtpPairing(source="t6")
        except Exception as e:
            logger.error(f"T5RtpPairing init failed (T5 stays Phase-2A): {e}")
            self._t5_pairing = None
        # P5 decoupling (2026-08-05, AC0G-B4: lb1421_enabled=true with
        # [timing.t6_pps] off left the judge stuck at T4 because the T5
        # bench only ever grounded on the T6 stream): every archive
        # stream carries its own per-stream pairing, fed from the
        # recorder's tap; _t5_bench_state prefers the T6 stream when
        # present (densest arrival cadence) and falls back to these.
        # description -> (T5RtpPairing, StreamRecorderV2)
        self._t5_fallback_pairings = {}
        self._t5_grounding_source = None    # last grounding, for the log

        # ── T6 residual-walk rate estimator (P3) ────────────────────
        # Differentiates the per-edge PPS residual (local_minus_source)
        # into an ADC-clock ppm — the judge's SECOND, independent rate
        # observable (the first is the offset-series slope).  Fed at
        # the SHM push site; reset on every native-anchor (re)capture.
        # MEASURED AND RECORDED ONLY — never resampled, never fed back
        # into labels (spec §11, audit G7).
        try:
            from .offset_judge import T6ResidualRateEstimator
            self._t6_rate_est = T6ResidualRateEstimator()
        except Exception as e:
            logger.error(f"T6ResidualRateEstimator init failed: {e}")
            self._t6_rate_est = None

        # Least-delayed-arrival filter for the T6 bench hand-off.  Fed
        # from the delivery callback (~55/s), read on the judge's tick.
        # Without it the bench publishes the stream transport latency as
        # clock error: measured -27.7 ms on B4 2026-08-14/15.
        try:
            from .t6_arrival_floor import ArrivalFloorTracker
            self._t6_arrival_floor = ArrivalFloorTracker()
        except Exception as e:
            logger.error(f"ArrivalFloorTracker init failed: {e}")
            self._t6_arrival_floor = None

        # ── T6/T5 substrate benches for the Offset Judge (P2) ───────
        # T6: NativeAnchor projection (pure counter arithmetic when a
        # valid anchor exists).  T5: the pairing product above.  Both
        # ride providers with getattr guards because the anchor and the
        # lb1421 probe materialise after __init__.
        if self._offset_judge is not None:
            try:
                from .offset_judge import NativeAnchorBench, LbeT5Bench
                self._offset_judge.add_bench(
                    NativeAnchorBench(provider=self._t6_bench_state))
                self._offset_judge.add_bench(
                    LbeT5Bench(provider=self._t5_bench_state))
                logger.info("OffsetJudge: T6 (NativeAnchor) + T5 (LB-142x "
                            "pairing) benches wired")
            except Exception as e:
                logger.error(
                    f"OffsetJudge T6/T5 bench wiring failed (judge "
                    f"continues on P1 benches): {e}", exc_info=True,
                )
            # P3: the T6 residual-walk rate observable rides its own
            # guard — bench failure above must not cost the rate feed.
            if self._t6_rate_est is not None:
                try:
                    self._offset_judge.set_t6_rate_provider(
                        self._t6_rate_est.current)
                    logger.info("OffsetJudge: T6 residual-walk rate "
                                "observable wired (P3)")
                except Exception as e:
                    logger.error(
                        f"OffsetJudge T6 rate wiring failed (judge "
                        f"keeps offset-slope only): {e}")

        _t6_cfg = _timing_section.get('t6_pps')
        _legacy_cfg = _timing_section.get('l6_pps')
        if _t6_cfg is not None:
            self._t6_config = _t6_cfg
        elif _legacy_cfg is not None:
            logger.warning(
                "[timing.l6_pps] config key is deprecated — rename to "
                "[timing.t6_pps] to match the T-tier authority "
                "hierarchy.  Continuing to accept the legacy key for "
                "now; future releases will require [timing.t6_pps]."
            )
            self._t6_config = _legacy_cfg
        else:
            self._t6_config = {}
        # Apply chain_delay calibration knob now that _t6_config is set.
        self._t6_chain_delay_calib_s = resolve_chain_delay_calib_s(
            self._t6_config)
        if t6_chain_delay_uncalibrated(self._t6_config):
            logger.warning(
                "T6 enabled with chain_delay_calib_s unset (0.000 ms): the "
                "native anchor will pin to the integer second and every "
                "sample label will be EARLY by the whole RF chain delay. "
                "Measure it against an independent bench — the judge's "
                "shadow_residual for T6 vs T4 is exactly this quantity — "
                "and set [timing.t6_pps] chain_delay_calib_s. Asserted, "
                "never derived (T6_ORIGIN_ASSERTION_DESIGN §5)."
            )
        if self._t6_config.get('enabled', False):
            freq_hz = self._t6_config.get('frequency_hz')
            if freq_hz is None:
                logger.error("timing.t6_pps.enabled=true but frequency_hz not set — T6 disabled")
            else:
                sr = int(self._t6_config.get('sample_rate',
                         self.channel_defaults.get('sample_rate', 24000)))
                # The matched-filter calibrator (textbook Costas +
                # integrate-and-dump MF).  It expects a wider channel
                # filter (±25 kHz at 96 kHz SR) for full benefit.  The
                # `use_matched_filter` switch and the legacy per-sample-Δφ
                # calibrator behind its `false` value retired 2026-09-04
                # (RESIDUE_AUDIT §3.4): the template, B4 and ND all ran
                # `true`, and the fine stage only ever rode this path.
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

                # ── T6 anchor inversion (spec: docs/design/
                # T6_ANCHOR_INVERSION_DESIGN.md) ────────────────────
                # Fine-stage sub-sample edge localisation + the
                # anchor-authority state machine.
                fine_cfg = self._t6_fine_settings(self._t6_config)
                # Recorded in every ledger row: its absence is
                # exactly what made the 2026-08-25 15:00-15:07
                # content window indistinguishable afterwards.
                self._t6_labeling_convention = fine_cfg.get(
                    'labeling_convention')
                self._t6_fine_stage = None
                self._t6_authority = None
                self._t6_authority_last_decision = None
                if fine_cfg['fine_stage_enabled']:
                    from hf_timestd.core.bpsk_edge_fine_stage import (
                        BpskEdgeFineStage,
                    )
                    from hf_timestd.core.t6_anchor_authority import (
                        T6AnchorAuthority,
                    )
                    self._t6_fine_stage = BpskEdgeFineStage(
                        sr, fold_seconds=fine_cfg['fine_fold_seconds'])
                    self._t6_authority = T6AnchorAuthority(
                        sr,
                        fine_cfg['delay_budget_ns'],
                        filter_group_delay_ns=fine_cfg[
                            'filter_group_delay_ns'],
                        edge_period_tolerance_ns=fine_cfg[
                            'edge_period_tolerance_ns'],
                        fine_coarse_max_ms=fine_cfg['fine_coarse_max_ms'],
                        degraded_unlock_after_sec=fine_cfg[
                            'degraded_unlock_after_sec'],
                        # Liveness: the authority needs to know the
                        # expected estimate cadence to tell "quiet"
                        # from "dead" (spec §6).
                        fine_fold_seconds=fine_cfg['fine_fold_seconds'],
                    )
                    logger.info(
                        "T6 anchor inversion armed: fold=%ds "
                        "delay_budget=%d ns filter_group_delay=%d ns "
                        "(asserted chain delay %.3f ms; spec: "
                        "docs/design/T6_ANCHOR_INVERSION_DESIGN.md)",
                        fine_cfg['fine_fold_seconds'],
                        fine_cfg['delay_budget_ns'],
                        fine_cfg['filter_group_delay_ns'],
                        (fine_cfg['delay_budget_ns']
                         + fine_cfg['filter_group_delay_ns']) / 1e6,
                    )

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

        # --- WWVB consumer state ---
        # Dedicated RadiodStream + decode worker, plumbed exactly like T6
        # (own UDP socket, own reader thread) but with no chrony feed, no
        # ring buffer, no archive.  Output goes to the JSONL ledger only
        # (Layer A+B per docs/WWVB-INTEGRATION.md §9 items 4-5; Layer C
        # Fusion ingest is a follow-up).
        self._wwvb_config = config.get('wwvb', {})
        self._wwvb_stream = None
        self._wwvb_channel_info = None
        self._wwvb_buf: deque = deque()
        self._wwvb_buf_samples: int = 0
        self._wwvb_buf_lock = threading.Lock()
        # RTP timestamp of the buffer's first (oldest) sample, kept in
        # lock-step with the deque so any sample offset maps to an RTP
        # timestamp, hence (via rtp_to_wallclock) to receiver UTC.  None until
        # the first packet, or after an RTP discontinuity clears the buffer.
        self._wwvb_anchor_rtp: Optional[int] = None
        # Layer 4 (Fusion source-pool feed) — populated by
        # _setup_wwvb_fusion_feed only when [wwvb] feed_fusion is enabled;
        # None / defaults keep WWVB ledger-only and out of the chrony path.
        self._wwvb_l1_writer = None
        self._wwvb_rtp_to_utc_s = None
        self._wwvb_rx_lat: Optional[float] = None
        self._wwvb_rx_lon: Optional[float] = None
        self._wwvb_processing_version = "wwvb-layer4"
        self._wwvb_learned_delay_ms = None
        self._wwvb_learned_sigma_ms = None
        self._wwvb_decode_thread: Optional[threading.Thread] = None
        self._wwvb_decode_stop = threading.Event()
        self._wwvb_first_sample_logged = False
        self._wwvb_ledger = None  # created lazily in _start_wwvb_stream

        self.ntp_status_lock = threading.Lock()

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
    
    def _register_started_recorder(self, key, recorder):
        """Wire a just-started recorder into the lifetime keepalive + calibrator.
        Factored out so the initial pass and the deferred-retry path register
        channels identically."""
        if recorder.config.ssrc:
            self._lifetime_entries.append((self.control, recorder.config.ssrc))
        else:
            logger.warning(f"Recorder {recorder.config.description} started but has no SSRC")

    def _start_deferred_recorder_retry(self, deferred, max_attempts=30, interval_s=20.0):
        """Retry channels that did not start on the first pass (radiod slow to
        create under load) in a background daemon thread, so the daemon comes up
        promptly with the channels it got instead of crashing on the first slow
        one.  Each success is wired in like a normal start."""
        def _loop():
            pending = list(deferred)
            for attempt in range(1, max_attempts + 1):
                if not pending:
                    return
                time.sleep(interval_s)
                still = []
                for key, recorder in pending:
                    try:
                        recorder.start()
                    except Exception as e:
                        logger.debug(
                            f"{recorder.config.description}: deferred start "
                            f"attempt {attempt} failed: {e}")
                        still.append((key, recorder))
                        continue
                    logger.info(
                        f"{recorder.config.description}: deferred channel started "
                        f"(attempt {attempt})")
                    self._register_started_recorder(key, recorder)
                pending = still
            if pending:
                logger.error(
                    f"{len(pending)} channel(s) never started after "
                    f"{max_attempts} retries: "
                    f"{[r.config.description for _, r in pending]}")
        threading.Thread(
            target=_loop, daemon=True, name="timestd-deferred-start").start()

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
                    # Must be threaded through: the hot buffer's floor is
                    # "one whole chunk plus margin" (see calculate_hot_minutes),
                    # so leaving this at the 600 s dataclass default pinned the
                    # ring at 11 minutes -- 725 MB for six channels -- no matter
                    # what the operator set. On B4 that was the difference
                    # between 725 MB and ~400 MB on a box that was OOM-killing
                    # this very process hourly.
                    file_duration_sec=int(self.recorder_config.get(
                        'file_duration_sec', TieredStorageConfig.file_duration_sec)),
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
            deferred = []
            for key, recorder in self.recorders.items():
                try:
                    recorder.start()
                except Exception as e:
                    # radiod's channel-CREATE latency climbs with its channel
                    # count, so on a busy shared radiod a create can exceed the
                    # verify timeout.  A slow create must NOT crash the whole
                    # recorder: defer this channel to a background retry and keep
                    # going, so the daemon comes up with the channels it got and
                    # fills the rest in as radiod catches up.
                    logger.error(
                        f"{recorder.config.description}: initial channel start "
                        f"failed ({e}); deferring to background retry"
                    )
                    deferred.append((key, recorder))
                    continue
                logger.info(f"Started recorder for {recorder.config.frequency_hz/1e6:.3f} MHz ({recorder.config.description})")
                self._register_started_recorder(key, recorder)
            if deferred:
                logger.warning(
                    f"{len(deferred)}/{len(self.recorders)} channel(s) did not "
                    f"start on the first pass (radiod create-rate under load); "
                    f"retrying in background: "
                    f"{[r.config.description for _, r in deferred]}"
                )
                self._start_deferred_recorder_retry(deferred)
        
        # Start T6 BPSK PPS stream (bare RadiodStream, no archive).  In
        # shared mode this just adds the channel to self._multi; the
        # actual receive loop kicks off below in self._multi.start().
        if self._t6_calibrator is not None:
            self._start_t6_stream()

        # Start WWVB consumer (dedicated RadiodStream, no archive, no
        # ring) — gated on [wwvb] enabled = true.
        if self._wwvb_config.get('enabled', False):
            self._start_wwvb_stream()

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
        # P2 radiod-pair revalidation: first tick 60 s in (channels are
        # freshly seeded at startup, nothing to revalidate earlier).
        last_pair_revalidate = time.time()

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

                # P2 per-source radiod-pair revalidation (every 60 s):
                # re-observe each channel's advertised pair from its
                # own listener-refreshed ChannelInfo (spec §7 scope —
                # never global discovery) so a changed pair is adopted,
                # and a wrong-but-steady one keeps being re-judged,
                # within ~60 s of steady state.  Runs in both dedicated
                # and shared-MultiStream modes (the shared mode has no
                # per-recorder health thread to piggyback on).
                if now - last_pair_revalidate >= 60:
                    for _rec in list(self.recorders.values()):
                        _rec.revalidate_radiod_pair()
                    last_pair_revalidate = now
        
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        
        finally:
            self._shutdown()
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._wwvb_decode_stop.set()
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

            # Ring depth comes from metrology's requirement (see
            # ring_buffer.RING_DEFAULT_MINUTES), NOT from the archive's
            # chunk policy. Overriding: `ring_minutes` in [recorder].
            #
            # The old comment here claimed "nothing reads from it yet" —
            # stale since at least 2026-08: each channel's ring is mapped by
            # exactly two processes, this recorder writing and that
            # channel's timestd-metrology@<channel> service reading.
            file_duration_sec = int(self.recorder_config.get('file_duration_sec', 600))
            ring_enabled = bool(self.recorder_config.get('ring_buffer', True))
            if ring_enabled:
                from .ring_buffer import (
                    RING_DEFAULT_MINUTES, RING_MIN_MINUTES, RING_WINDOW_SEC,
                )
                ring_minutes = max(
                    RING_MIN_MINUTES,
                    int(self.recorder_config.get('ring_minutes',
                                                 RING_DEFAULT_MINUTES)),
                )
                ring_seconds = ring_minutes * 60
                logger.info(
                    f"Ring buffer enabled: {ring_minutes} minutes "
                    f"({ring_seconds}s) per channel × {len(self.channel_specs)} "
                    f"channels — sized for metrology's {RING_WINDOW_SEC}s window, "
                    f"independent of the archive chunk duration"
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
                    offset_judge=self._offset_judge,
                    status_stream=self.status_address,
                )
                self.recorders[description] = recorder
                # P5: per-stream T5 pairing fallback — this stream can
                # ground the LB-142x NMEA-vs-RTP pairing when the T6
                # stream is absent (never raises; wiring failure just
                # leaves this stream out of the fallback set).
                self._wire_t5_fallback_arrival(description, recorder)

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
        # Channel gain (dB).  AGC stays OFF on every metrology channel by
        # design: signal and noise vary substantially over 24 hours and
        # AGC's unpredictable scaling is unacceptable in a scientific
        # measurement.  For the *broadcast* channels that leaves the level
        # wherever the front end puts it, which is why they are F32.
        #
        # T6 is the exception that can be set: HF-PPS is injected LOCALLY,
        # so its level is known, constant, and under our control — a fixed
        # gain is exact here, with none of the 24-hour variation that rules
        # AGC out.  Worth setting because radiod's output encoding is in
        # practice S16BE (F32 grants are lost to the lifetime keepalive —
        # HamSCI/ka9q-python#3), so this channel really does live in 16-bit
        # fixed point: measured on B4 at ~34 counts RMS of +-32767, about 5
        # of 16 bits, i.e. ~38-41 dB quantisation SNR against a 51 dB
        # channel — quantisation, not the RF, was the noise floor.
        #
        # Irrelevant to the 0.5 s coarse correlation (48 000 samples average
        # quantisation away) but directly limiting for a short-integration
        # fine stage over the ~52 us transition (~5 samples) — see
        # HamSCI/hf-timestd#7.  Default 0.0 preserves prior behaviour;
        # raise it only after checking headroom against observed peaks.
        gain_db = float(t6.get('gain_db', 0.0))

        # Dedicated RadiodStream + per-channel UDP socket.
        try:
            channel_info = self.control.ensure_channel(
                frequency_hz=freq_hz,
                preset='iq',
                sample_rate=sr,
                encoding=Encoding.F32,
                agc_enable=False,
                gain=gain_db,
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
            # Wire continuous STATUS listener (ka9q-python ≥3.16.0).
            # Refreshes channel_info.gps_time / .rtp_timesnap in place on
            # every radiod STATUS broadcast (sub-second), bounding T6
            # SHM-push wallclock drift to a few µs vs the legacy ~30 s
            # anchor-staleness window that drove HPPS/HFPS to drift at
            # the host slew rate (~3.8 µs/s on bee1).  Layer 3 recapture
            # remains in place as a safety net for radiod restarts.
            self._register_t6_with_status_listener(channel_info)
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
        except Exception as e:
            logger.error(f"Failed to start T6 BPSK PPS stream: {e}", exc_info=True)
            self._t6_stream = None

    def _start_wwvb_stream(self):
        """Provision the WWVB_60 channel on a dedicated RadiodStream and
        spawn the periodic decode worker.

        Architecture per docs/WWVB-INTEGRATION.md §2: in-process consumer
        (T6-style dedicated socket + reader thread, isolated from the
        shared archive MultiStream) feeding the JSONL ledger.  No archive
        writer, no ring buffer attachment, no STATUS listener — WWVB
        decodes at minute cadence, so neither sub-second timing-anchor
        refresh nor the chrony-settled gate apply.
        """
        from ka9q import RadiodStream, Encoding
        from .wwvb_ledger import WwvbLedger

        cfg = self._wwvb_config
        freq_hz = int(cfg.get('frequency_hz', 60_000))
        sr = int(cfg.get('sample_rate', 24_000))
        desc = cfg.get('description', 'WWVB_60')
        ledger_dir = Path(cfg.get('ledger_dir', str(self.output_dir / 'wwvb')))

        try:
            channel_info = self.control.ensure_channel(
                frequency_hz=freq_hz,
                preset='iq',
                sample_rate=sr,
                encoding=Encoding.F32,
                agc_enable=False,
                gain=0.0,
                timeout=30.0,
                lifetime=RADIOD_LIFETIME_FRAMES,
            )
            self._wwvb_channel_info = channel_info
            if channel_info is not None and getattr(channel_info, 'ssrc', 0):
                self._lifetime_entries.append((self.control, channel_info.ssrc))

            self._wwvb_ledger = WwvbLedger(ledger_dir)

            # Layer 4: optional Fusion source-pool feed (default off — gated
            # on [wwvb] feed_fusion because it reaches the chrony-disciplining
            # path).  Best-effort; failure leaves WWVB ledger-only.
            if cfg.get('feed_fusion', False):
                try:
                    self._setup_wwvb_fusion_feed(channel_info, desc)
                except Exception as exc:
                    logger.warning(
                        f"WWVB feed_fusion setup failed; staying ledger-only: "
                        f"{exc}"
                    )

            self._wwvb_stream = RadiodStream(
                channel=channel_info,
                on_samples=self._wwvb_on_samples,
                samples_per_packet=200,
                # Same value as T6 — 256-packet resequencer gives ~3.2 s
                # jitter tolerance at 80 pkt/sec, well clear of any
                # archive-rollover stalls on the shared MultiStream.
                resequence_buffer_size=256,
            )
            self._wwvb_stream.start()

            self._wwvb_decode_stop.clear()
            self._wwvb_decode_thread = threading.Thread(
                target=self._wwvb_decode_loop,
                name="WwvbDecode",
                daemon=True,
            )
            self._wwvb_decode_thread.start()

            logger.info(
                f"WWVB stream started: {desc} at {freq_hz/1e3:.3f} kHz "
                f"sr={sr} ledger={ledger_dir}"
            )
        except Exception as exc:
            logger.error(f"Failed to start WWVB stream: {exc}", exc_info=True)
            self._wwvb_stream = None
            if self._wwvb_ledger is not None:
                self._wwvb_ledger.close()
                self._wwvb_ledger = None

    def _setup_wwvb_fusion_feed(self, channel_info, channel_name):
        """Wire the L1 metrology writer + RTP->UTC closure so accepted WWVB
        minutes can join the Fusion source pool (Layer 4).

        Writes ``L1_metrology_measurements`` rows into the phase2 channel
        directory (``<data_root>/phase2/<channel_name>``) so the central
        ``timestd-fusion`` service discovers WWVB_60 as a source alongside the
        HF channels.  ``raw_toa_ms`` carries the WWVB ``timing_error_ms``
        (D_clock); see ``wwvb_fusion.build_l1_row`` for the timing convention.

        Raises on misconfiguration; the caller treats any failure as
        "stay ledger-only".
        """
        from hf_timestd import __version__ as hf_version
        from hf_timestd.io import make_data_product_writer
        # Absolute, not relative: this module lives at hf_timestd/data_product_registry.py,
        # so `from .` would resolve to hf_timestd.core.* and raise. The caller treats any
        # exception here as "stay ledger-only", so a relative import made feed_fusion
        # impossible to enable while logging only a warning (AC0G-B4, 2026-08-19).
        from hf_timestd.data_product_registry import DataProductRegistry
        from hamsci_dsp.geometry import grid_to_latlon

        # Receiver location: explicit lat/lon wins, else Maidenhead grid.
        lat = self.station_config.get('latitude')
        lon = self.station_config.get('longitude')
        if not lat or not lon:  # None or 0.0 placeholder
            grid = (self.station_config.get('grid_square') or '').strip()
            if len(grid) >= 4:
                lat, lon = grid_to_latlon(grid)
        if not lat or not lon:
            logger.warning(
                "WWVB feed_fusion enabled but receiver location unknown "
                "(set [station] latitude/longitude or grid_square); staying "
                "ledger-only"
            )
            return
        self._wwvb_rx_lat = float(lat)
        self._wwvb_rx_lon = float(lon)

        # radiod's clock is independent of this host (radiod may run on another
        # machine) but its RTP timeline is GPSDO-disciplined.  Keep the
        # channel's GPS_TIME / RTP_TIMESNAP snapshot fresh — and restart-aware —
        # by wiring it into the continuous STATUS listener, exactly as T6 does.
        # The listener picks up a new snapshot whenever radiod restarts or
        # re-snaps, which is what keeps the RTP->UTC mapping in the correct
        # counter space across a discontinuity.
        control = getattr(self, 'control', None)
        start_fn = getattr(control, 'start_status_listener', None) if control else None
        if start_fn is not None and getattr(channel_info, 'ssrc', 0):
            try:
                start_fn().register_channel(channel_info)
                logger.info(
                    f"WWVB channel SSRC {channel_info.ssrc:08x} wired to radiod "
                    f"STATUS listener — gps_time/rtp_timesnap refresh per broadcast"
                )
            except Exception as exc:
                logger.warning(f"WWVB STATUS-listener wiring failed: {exc}")

        # RTP -> receiver UTC seconds via radiod's GPSDO snapshot, NOT the host
        # wall clock.  Per ARCHITECTURE.md §2 / ARCHITECTURE-FIRST-PRINCIPLES
        # §1–2 the authoritative mapping is
        #     utc = GPS_TIME_unix + (rtp − RTP_TIMESNAP) / sample_rate
        # — the same formula buffer_timing.sample0_utc and the HF metrology
        # workers ride.  resolve_buffer_timing applies it with leap-second
        # correctness and radiod-restart counter-space handling.  Returns None
        # when the snapshot is missing so build_l1_row skips the row.  A
        # radiod-side sample-count discontinuity is a timing THREAT, defended at
        # three layers: (1) the buffer's RTP-gap reset drops any accumulation
        # spanning the jump; (2) the STATUS listener re-snaps gps_time/
        # rtp_timesnap into the new counter space; (3) the ±500 ms plausibility
        # gate in build_l1_row rejects any residual counter-space mismatch.
        from .buffer_timing import resolve_buffer_timing

        # Fallback must match the shipped template: a WWVB channel wider than
        # a few kHz admits noise only and silently stops all decoding.
        # See config/timestd-config.toml.template [wwvb].
        sr = int(self._wwvb_config.get('sample_rate', 4_000))

        def _rtp_to_utc_s(rtp, _ci=channel_info, _sr=sr):
            gps_time = getattr(_ci, 'gps_time', None)
            rtp_snap = getattr(_ci, 'rtp_timesnap', None)
            if gps_time is None or rtp_snap is None:
                return None
            bt = resolve_buffer_timing(
                {
                    'start_rtp_timestamp': int(rtp) & 0xFFFFFFFF,
                    'gps_time_ns': int(gps_time),
                    'rtp_timesnap': int(rtp_snap),
                },
                sample_rate=_sr,
            )
            if bt.source == 'no_timing':
                return None
            return bt.sample0_utc

        self._wwvb_rtp_to_utc_s = _rtp_to_utc_s

        channel_dir = self.output_dir / 'phase2' / channel_name
        writer_output_dir = DataProductRegistry.get_data_dir(
            channel_dir=channel_dir,
            product_level="L1",
            product_name="metrology_measurements",
            create=True,
        )
        self._wwvb_processing_version = f"wwvb-layer4/{hf_version}"
        self._wwvb_l1_writer = make_data_product_writer(
            output_dir=writer_output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            channel=channel_name,
            version="v1",
            processing_version=self._wwvb_processing_version,
            station_metadata=self.station_config,
            storage_config=self.config.get('storage', {}) or {},
        )
        self._wwvb_learned_delay_ms = self._wwvb_config.get('learned_delay_ms')
        self._wwvb_learned_sigma_ms = self._wwvb_config.get('learned_sigma_ms')
        logger.info(
            f"WWVB feed_fusion ENABLED: writer={writer_output_dir} "
            f"rx=({self._wwvb_rx_lat:.4f},{self._wwvb_rx_lon:.4f}) "
            f"learned_delay_ms={self._wwvb_learned_delay_ms}"
        )

    def _wwvb_on_samples(self, samples, quality):
        """RTP sample callback — append to rolling IQ buffer.

        Bounded by window_s × sample_rate; oldest packets get dropped
        from the front when the buffer overflows.  Decode runs on a
        separate thread so an FFT in the demod path can never stall the
        RTP receive loop.
        """
        cfg = self._wwvb_config
        sample_rate = int(cfg.get('sample_rate', 24_000))
        window_s = float(cfg.get('window_s', 90.0))
        window_samples = int(window_s * sample_rate)

        if not self._wwvb_first_sample_logged:
            logger.info(
                f"WWVB first samples: len={len(samples)}, "
                f"dtype={getattr(samples, 'dtype', None)}, "
                f"last_rtp_timestamp={getattr(quality, 'last_rtp_timestamp', None)}"
            )
            self._wwvb_first_sample_logged = True

        rtp0 = resolve_batch_rtp(quality)
        with self._wwvb_buf_lock:
            # Maintain the RTP anchor (RTP of buffer sample 0).
            # last_rtp_timestamp is the RTP of samples[0] for this batch — the
            # same convention the T6 calibrator relies on
            # (bpsk_pps_calibrator_mf.py:415, rtp_batch = arange + rtp).
            if rtp0 is None:
                # No RTP provenance this batch -> can't anchor; invalidate so
                # the decode loop falls back to ledger-only (no Fusion timing).
                self._wwvb_anchor_rtp = None
            else:
                rtp0 = int(rtp0) & 0xFFFFFFFF
                # ---- hf-timestd#23 probe ----
                # Does RTP advance by the number of samples we were
                # actually handed?  The continuity check below assumes
                # so; on B4 it is short by a constant ~45% of the batch
                # at every sample rate tried.  Rate-limited to 1/30 s --
                # the failing path logs several times a second and the
                # last run put ~365 lines/minute into the journal.
                _prev = getattr(self, '_wwvb_prev_batch', None)
                _rep = self._wwvb_rtp_advance_report(
                    _prev[0] if _prev else None,
                    _prev[1] if _prev else None,
                    rtp0,
                )
                if _rep is not None:
                    _now_m = time.monotonic()
                    if (_now_m - getattr(
                            self, '_wwvb_probe_last_log', 0.0)) > 30.0:
                        self._wwvb_probe_last_log = _now_m
                        logger.warning(
                            "WWVB RTP advance probe: previous batch "
                            "delivered %d samples but RTP advanced %d "
                            "(deficit %d, ratio %.4f). Equal ⇒ the RTP "
                            "clock is fine and buf_samples is "
                            "over-counted; ~0.55 ⇒ RTP does not count "
                            "delivered samples 1:1. hf-timestd#23",
                            _rep["sample_len"], _rep["rtp_delta"],
                            _rep["deficit"], _rep["ratio"],
                        )
                self._wwvb_prev_batch = (rtp0, int(len(samples)))
                # ---- end probe ----
                if self._wwvb_buf_samples == 0 or self._wwvb_anchor_rtp is None:
                    self._wwvb_anchor_rtp = rtp0
                else:
                    expected = (self._wwvb_anchor_rtp
                                + self._wwvb_buf_samples) & 0xFFFFFFFF
                    if rtp0 != expected:
                        # RTP discontinuity (packet loss / SSRC reset): drop the
                        # accumulation so no timing measurement spans the gap —
                        # a spanned gap would silently corrupt the boundary->UTC
                        # mapping.  The DSP already assumes a contiguous buffer.
                        logger.warning(
                            f"WWVB RTP discontinuity: expected {expected}, got "
                            f"{rtp0}; dropping {self._wwvb_buf_samples} buffered "
                            f"samples and re-anchoring"
                        )
                        self._wwvb_buf.clear()
                        self._wwvb_buf_samples = 0
                        self._wwvb_anchor_rtp = rtp0
            self._wwvb_buf.append(samples)
            self._wwvb_buf_samples += len(samples)
            while (self._wwvb_buf_samples > window_samples
                   and len(self._wwvb_buf) > 1):
                dropped = len(self._wwvb_buf[0])
                self._wwvb_buf_samples -= dropped
                self._wwvb_buf.popleft()
                if self._wwvb_anchor_rtp is not None:
                    self._wwvb_anchor_rtp = (self._wwvb_anchor_rtp
                                             + dropped) & 0xFFFFFFFF

    def _wwvb_decode_loop(self):
        """Periodic decode worker — snapshots the buffer and calls
        wwvb_demod.decode_iq, writing results to the ledger.

        Wakes every ``decode_interval_s`` seconds.  Skips the decode if
        the buffer hasn't yet accumulated ``min_buffer_s`` worth of
        samples (a minute frame plus a few seconds of headroom).
        """
        from .wwvb_demod import decode_iq
        from .wwvb_fusion import (
            build_l1_row,
            estimate_snr_db,
            frame_minute_is_plausible,
        )

        cfg = self._wwvb_config
        sample_rate = float(cfg.get('sample_rate', 24_000))
        decode_interval_s = float(cfg.get('decode_interval_s', 30.0))
        min_buffer_s = float(cfg.get('min_buffer_s', 65.0))
        min_decode_samples = int(min_buffer_s * sample_rate)

        logger.info(
            f"WWVB decode loop started: interval={decode_interval_s}s "
            f"min_buffer={min_buffer_s}s"
        )

        while not self._wwvb_decode_stop.is_set():
            if self._wwvb_decode_stop.wait(decode_interval_s):
                break
            now = datetime.now(timezone.utc)
            with self._wwvb_buf_lock:
                have = self._wwvb_buf_samples
                if have < min_decode_samples:
                    logger.debug(
                        f"WWVB buffering {have / sample_rate:.1f}s / "
                        f"{min_buffer_s:.0f}s"
                    )
                    continue
                iq = np.concatenate(list(self._wwvb_buf))
                # RTP timestamp of iq[0]; None if the buffer isn't anchored
                # (no RTP provenance / post-discontinuity).  Captured under the
                # lock so it stays consistent with the concatenated samples.
                anchor_rtp = self._wwvb_anchor_rtp

            mean_amp = float(np.abs(iq).mean())
            try:
                result = decode_iq(iq, sample_rate=sample_rate)
            except Exception as exc:
                logger.warning(f"WWVB decode failed: {exc}")
                continue

            # Noise discrimination, layer 1 — parity.  The premise from the
            # AC0G 2026-05-30 dusk validation run was that every false-positive
            # frame had par >= 1 (Hamming "corrected" a noise time word) and
            # every real frame had par == 0.
            #
            # That premise is FALSIFIED.  Over 2026-08-17..19 on B4, 5 of the
            # 18 frames this gate accepted decoded minutes in 2053 or 2097 —
            # 28% garbage — and one of them had par == 0 AND sync == 0.  Parity
            # alone is necessary, not sufficient.
            parity_ok = [f for f in result.frames if f.frame.parity_errors == 0]
            rejected = [f for f in result.frames if f.frame.parity_errors != 0]

            # Layer 2 — the decoded minute must be near the time this receiver
            # thinks it is.  Cheap, and decisive against the failure above.  The
            # ±500 ms gate in build_l1_row cannot serve here: it needs an RTP
            # anchor, so it only runs when feed_fusion is on, which left the
            # default ledger-only configuration undefended.
            accepted, implausible = [], []
            for _f in parity_ok:
                target = (
                    accepted
                    if frame_minute_is_plausible(_f.frame.minute_of_frame, now=now)
                    else implausible
                )
                target.append(_f)

            if self._wwvb_ledger is not None:
                self._wwvb_ledger.record_pass(
                    ts=now,
                    buffer_s=have / sample_rate,
                    mean_amp=mean_amp,
                    carrier_offset_hz=result.carrier_offset_hz,
                    seconds_detected=result.seconds_detected,
                    bits=int(result.bits.size),
                    frames=len(accepted),
                    frames_implausible=len(implausible),
                    # The one field that separates a blind pass from a working
                    # one; every other field in this record reads the same
                    # either way (see wwvb_ledger.record_pass).
                    snr_db=estimate_snr_db(result.per_second_iq),
                )
                for f, _plausible in (
                    [(x, True) for x in accepted]
                    + [(x, False) for x in implausible]
                ):
                    self._wwvb_ledger.record_frame(
                        ts=now,
                        minute_of_frame=f.frame.minute_of_frame,
                        dst_state=(
                            f.frame.dst_state.name
                            if f.frame.dst_state is not None else None
                        ),
                        leap_second=(
                            f.frame.leap_second.name
                            if f.frame.leap_second is not None else None
                        ),
                        parity_errors=f.frame.parity_errors,
                        sync_errors=f.sync_errors,
                        inverted_polarity=f.inverted_polarity,
                        mean_amp=mean_amp,
                        plausible=_plausible,
                    )

            # --- Layer 4: feed accepted minutes into the Fusion source pool ---
            # Each accepted frame becomes an L1 metrology row whose raw_toa_ms
            # carries timing_error_ms (D_clock).  Gated: writer is None unless
            # [wwvb] feed_fusion is enabled and the feed wired up successfully.
            if self._wwvb_l1_writer is not None and accepted:
                for f in accepted:
                    # A WWVB minute frame spans 60 one-second symbols starting
                    # at f.second_index; use them for the coherent-SNR proxy.
                    snr_db = estimate_snr_db(
                        result.per_second_iq[f.second_index:f.second_index + 60]
                    )
                    confidence = 1.0 if f.sync_errors == 0 else 0.9
                    try:
                        row = build_l1_row(
                            detected_frame=f,
                            anchor_rtp=anchor_rtp,
                            rtp_to_utc_s=self._wwvb_rtp_to_utc_s,
                            rx_lat=self._wwvb_rx_lat,
                            rx_lon=self._wwvb_rx_lon,
                            snr_db=snr_db,
                            confidence=confidence,
                            processing_version=self._wwvb_processing_version,
                            learned_delay_ms=self._wwvb_learned_delay_ms,
                            learned_sigma_ms=self._wwvb_learned_sigma_ms,
                        )
                    except Exception as exc:
                        logger.warning(f"WWVB L1 row build failed: {exc}")
                        continue
                    if row is None:
                        logger.debug(
                            "WWVB L1 row skipped (no RTP anchor / implausible "
                            f"timing) minute="
                            f"{f.frame.minute_of_frame.isoformat()}"
                        )
                        continue
                    try:
                        self._wwvb_l1_writer.write_measurement(row)
                        logger.info(
                            f"WWVB->Fusion minute="
                            f"{f.frame.minute_of_frame.isoformat()} "
                            f"timing_error={row['raw_toa_ms']:+.3f}ms "
                            f"snr~{snr_db:.1f}dB grade={row['quality_flag']}"
                        )
                    except Exception as exc:
                        logger.warning(f"WWVB L1 write failed: {exc}")

            for f in rejected:
                logger.info(
                    f"WWVB noise frame rejected: par={f.frame.parity_errors} "
                    f"sync={f.sync_errors} minute={f.frame.minute_of_frame.isoformat()}"
                )
            for f in implausible:
                logger.info(
                    "WWVB frame rejected as implausible (cleared parity): "
                    f"par={f.frame.parity_errors} sync={f.sync_errors} "
                    f"minute={f.frame.minute_of_frame.isoformat()}"
                )

            if accepted:
                logger.info(
                    f"WWVB decode: mean|iq|={mean_amp:.3e} "
                    f"carrier_off={result.carrier_offset_hz:+.3f}Hz "
                    f"secs={result.seconds_detected} "
                    f"accepted={len(accepted)} rejected={len(rejected)}"
                )

        logger.info("WWVB decode loop stopped")

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
    # What T5 is entitled to adjudicate about T6.
    #
    # T6 is the better marker of WHERE the second falls: the TS-1 flips
    # carrier phase on the GPS edge and we localise it to ~150 ns.  T5's
    # job is to name WHICH second (an integer choice, which cannot inject
    # sub-second error) and to catch GROSS MISLOCK -- the matched filter
    # latching the boxcar sidelobe at ±0.5 s, or a phantom in a
    # zero-filled gap, and then measuring the wrong feature beautifully.
    # It is NOT entitled to grade T6's sub-second placement.
    #
    # This was 5 ms until 2026-08-25, which did exactly that: the T5
    # bench publishes its own sigma as 25 ms, and fifteen consecutive
    # t5_implied values on AC0G-B4 spanned 147.68-153.46 ms -- a 5.78 ms
    # scatter.  The threshold was TIGHTER THAN THE SCATTER OF THE
    # INSTRUMENT IT ARBITRATES WITH, so it fired on T5 noise and, worse,
    # could not tell "wrong feature" from "chain delay differs by tens of
    # ms".  B4 sat in that gap -- 76.8 ms, too big for T5 noise, far too
    # small for a sidelobe -- rejecting at ~1 Hz for hours with no way
    # out.
    #
    # 150 ms sits between the two real incidents, ~70 ms clear of each:
    #
    #   B4   2026-08-25  Δ =  76.8 ms  stale lock  -> must ACCEPT
    #   bee1 2026-05-23  Δ = 217.4 ms  phantom     -> must REJECT
    #
    # and is 6x T5's own 25 ms sigma, well under the 500 ms sidelobe.
    T6_STEP_RECOVERY_T5_SANITY_NS = 150_000_000

    # How often the LOCK itself is re-validated against GPS.  Only
    # reached on the wrap-rejection path (something is already wrong
    # with the lock), and _t5_implied_effective_chain_delay() costs an
    # rtp_to_utc, so this stays off the per-batch hot path.
    T6_STALE_LOCK_EVAL_PERIOD_S = 30.0

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
    # Minimum spacing between disambiguation re-walks while initial accept
    # is refusing implausible values (see the Layer B guard at initial
    # accept).  References (chronyc sigma) move on second timescales;
    # walking per sample batch is pure chronyc-subprocess + log churn.
    T6_DISAMBIG_RETRY_INTERVAL_SEC = 5.0
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

    def attach_a_level_provider(self, provider) -> None:
        """Inject a callable returning the A-level ("A1" / "A0").

        The A-axis (docs/design/TIMING_AUTHORITY_TWO_AXIS.md) is whether a
        GPSDO disciplines the ADC clock.  It governs *rate*, so it decides
        how fast a frozen anchor goes stale — see hf-timestd#41.

        ``GpsdoProbe.poll`` has exactly this signature and is the intended
        provider: it reads gpsdo-monitor's published JSON, freshness-gates
        it, and returns "A0" on its own when the device goes away.  Not
        attaching one leaves the A-level UNKNOWN, which is deliberately
        treated as undisciplined rather than assumed disciplined.
        """
        self._a_level_provider = provider

    def _t6_a_level(self):
        """Current A-level, or None when nothing observes it.

        Never falls back to a configured value: a config assertion is not
        an observation, and the whole point of #41 is that quoting an
        A1-grade uncertainty on an unobserved ruler is how a station
        claims precision it does not have.  `hf-timestd validate` warns
        separately when the A-level is asserted rather than probed.
        """
        provider = getattr(self, '_a_level_provider', None)
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001 — a probe fault is not a coast fault
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

    # ── Offset Judge bench providers (P2) ────────────────────────────
    # Both run on the judge's tick thread; every attribute access is
    # getattr-guarded because unit tests bypass __init__ via __new__
    # and the underlying state (anchor, probe, channel_info)
    # materialises asynchronously at runtime.

    def _t6_rate_reset(self, cause: str) -> None:
        """Restart the P3 residual-walk rate window (never raises).

        Called at every native-anchor (re)capture/restore: the residual
        reference frame moves with the anchor, so regressing across the
        change would fabricate a rate step (spec §5 honesty)."""
        floor = getattr(self, '_t6_arrival_floor', None)
        if floor is not None:
            try:
                # Offsets are relative to the anchor: the frame moves
                # with it, so the old window is a different frame.
                floor.reset(cause)
            except Exception as e:  # noqa: BLE001 — never fatal
                logger.debug(f"T6 arrival floor reset failed: {e}")
        est = getattr(self, '_t6_rate_est', None)
        if est is None:
            return
        try:
            est.reset(cause)
        except Exception as e:  # noqa: BLE001 — metadata path, never fatal
            logger.debug(f"T6 rate estimator reset failed: {e}")

    def _t6_note_arrival(self, quality) -> None:
        """Record the T6 stream arrival point: label + transport floor.

        Two things happen per delivered batch (~55/s on B4):

        * the T5 pairing gets the arrival label -- now the END of the
          delivered batch (``newest_sample_rtp``) rather than the last
          RECEIVED packet's pre-resequencer header;
        * the arrival floor tracker gets this batch's (label_utc - mono)
          offset, so the bench can later answer from the LEAST delayed
          arrival instead of whichever one happened to land last.

        The floor must be fed here, on the delivery path, and not from
        the bench poll: the judge ticks every 10 s, so a poll-fed window
        would hold one arrival and filter nothing.

        Hot path -- never raises.
        """
        _pairing = getattr(self, '_t5_pairing', None)
        if _pairing is None:
            return
        rtp = newest_sample_rtp(quality)
        if rtp is None:
            return
        _pairing.note_arrival(rtp)

        tracker = getattr(self, '_t6_arrival_floor', None)
        anchor = getattr(self, '_t6_native_anchor', None)
        if tracker is None or anchor is None:
            return
        try:
            from .native_anchor import utc_ns_at_rtp
            mono = _pairing.now_mono()
            utc_s = utc_ns_at_rtp(rtp, anchor) / 1e9
            tracker.note(utc_s - mono, mono)
        except Exception as e:  # noqa: BLE001 — diagnostics never fatal
            logger.debug(f"T6 arrival floor note failed: {e}")

    def _t6_bench_state(self):
        """NativeAnchorBench provider: (anchor, arrival_rtp, arrival_mono,
        floor).

        Only answers while a valid T6 native anchor exists AND the T6
        stream is delivering samples (the arrival point grounds the
        bench's 'now' hand-off; NativeAnchorBench enforces freshness).

        ``floor`` is the least-delayed arrival seen recently (see
        ``t6_arrival_floor``).  It is the element that keeps the bench
        from publishing the transport latency as clock error; None until
        the window has filled, in which case the bench falls back to the
        conservative transport bound.
        """
        anchor = getattr(self, '_t6_native_anchor', None)
        pairing = getattr(self, '_t5_pairing', None)
        if anchor is None or pairing is None:
            return None
        arrival = pairing.latest_arrival
        if arrival is None:
            return None
        floor = None
        tracker = getattr(self, '_t6_arrival_floor', None)
        if tracker is not None:
            try:
                floor = tracker.estimate(pairing.now_mono())
            except Exception as e:  # noqa: BLE001 — bench must not die
                logger.debug(f"T6 arrival floor estimate failed: {e}")
        return (anchor, arrival[0], arrival[1], floor)

    def _wire_t5_fallback_arrival(self, description: str, recorder) -> None:
        """Give an archive stream its own T5 pairing arrival tracker.

        P5 decoupling: the NMEA-vs-RTP pairing behind the judge's T5
        bench only needs SOME live stream's (gps_time, rtp_timesnap,
        sample_rate) mapping plus arrival tracking.  Historically only
        the T6 BPSK stream fed it, so disabling [timing.t6_pps]
        silently killed the T5 bench (AC0G-B4 2026-08-05:
        lb1421_enabled=true, judge stuck at T4).  Each archive stream
        now carries a per-stream pairing fed from the recorder's tap —
        the same (samples, quality) callback shape the T6 stream's
        arrival note uses (see ``_t6_on_samples``).  The tap runs after
        the archive write, so a flush-delayed batch notes a late
        arrival; the pairing's latency sigma floor and MAD spread carry
        that honestly.  Never raises.
        """
        fallbacks = getattr(self, '_t5_fallback_pairings', None)
        if fallbacks is None:
            # __new__-bypassed instance (unit tests) — start the map.
            self._t5_fallback_pairings = fallbacks = {}
        try:
            from .t5_rtp_pairing import T5RtpPairing
            pairing = T5RtpPairing(source=f"stream:{description}")
        except Exception as e:  # noqa: BLE001 — bench feed, never fatal
            logger.debug(
                f"T5 fallback pairing init failed for {description}: {e}")
            return

        def _note_arrival_tap(samples, quality, _p=pairing):
            rtp = getattr(quality, 'last_rtp_timestamp', None)
            if rtp is not None:
                _p.note_arrival(rtp)

        try:
            recorder.add_tap(_note_arrival_tap)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"T5 fallback tap wiring failed for {description}: {e}")
            return
        fallbacks[description] = (pairing, recorder)

    def _t5_bench_state(self):
        """LbeT5Bench provider: current T5PairingProduct or None.

        Grounding preference (P5 decoupling): the dedicated T6 BPSK
        stream when present (densest arrival cadence), else the best
        archive stream carrying its own per-stream pairing (highest
        sample rate first).  The product's ``source`` field names the
        grounding stream so sigma accounting stays honest.  Result:
        lb1421_enabled=true alone — with the T6 stream disabled — is
        sufficient to light the T5 bench.
        """
        probe = getattr(self, '_lb1421_probe', None)
        if probe is None:
            return None
        reading = probe.get_latest()
        if reading is None:
            return None
        # Preferred grounding: the dedicated T6 stream.
        pairing = getattr(self, '_t5_pairing', None)
        ci = getattr(self, '_t6_channel_info', None)
        if pairing is not None and ci is not None:
            gps_time = getattr(ci, 'gps_time', None)
            rtp_snap = getattr(ci, 'rtp_timesnap', None)
            if gps_time is not None and rtp_snap is not None:
                cal = getattr(self, '_t6_calibrator', None)
                sr = getattr(cal, 'sample_rate', None) if cal is not None else None
                if not sr:
                    sr = getattr(ci, 'sample_rate', None)
                if sr:
                    product = pairing.compute(
                        reading, gps_time, rtp_snap, int(sr))
                    if product is not None:
                        self._note_t5_grounding(product.source)
                        return product
        return self._t5_bench_state_fallback(reading)

    def _t5_bench_state_fallback(self, reading):
        """T5 pairing grounded by the best available archive stream.

        Candidate streams need a fresh arrival, a listener-refreshed
        (gps_time, rtp_timesnap) ChannelInfo and a sample rate; the
        densest substrate (highest sample rate) is tried first, name
        order breaking ties for determinism.
        """
        fallbacks = getattr(self, '_t5_fallback_pairings', None)
        if not fallbacks:
            return None
        candidates = []
        for desc, (pairing, recorder) in list(fallbacks.items()):
            if pairing.latest_arrival is None:
                continue
            ci = getattr(recorder, 'channel_info', None)
            if ci is None:
                continue
            gps_time = getattr(ci, 'gps_time', None)
            rtp_snap = getattr(ci, 'rtp_timesnap', None)
            if gps_time is None or rtp_snap is None:
                continue
            cfg = getattr(recorder, 'config', None)
            sr = getattr(cfg, 'sample_rate', None) if cfg is not None else None
            if not sr:
                sr = getattr(ci, 'sample_rate', None)
            if not sr:
                continue
            candidates.append((int(sr), desc, pairing, gps_time, rtp_snap))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        for sr, desc, pairing, gps_time, rtp_snap in candidates:
            product = pairing.compute(reading, gps_time, rtp_snap, sr)
            if product is not None:
                self._note_t5_grounding(product.source)
                return product
        return None

    def _note_t5_grounding(self, source: str) -> None:
        """Log once per grounding-source change (T5 bench pedigree)."""
        if source != getattr(self, '_t5_grounding_source', None):
            self._t5_grounding_source = source
            logger.info(f"T5 bench pairing grounded by {source}")

    def _compute_rtp_to_utc_offset_ns(self) -> Optional[int]:
        """Pattern B: the offset that bridges ka9q's host-clock-derived
        ``rtp_to_wallclock`` to the hf-timestd-native anchor.

        For any RTP timestamp ``r``::

            rtp_to_wallclock(r, channel) + offset_ns/1e9 == utc_ns_at_rtp(r, anchor)/1e9

        The two sides are equal because the rate component is identical
        (both ride the GPSDO-disciplined sample rate); only the *anchor*
        differs, and that difference is constant across RTP samples,
        equal to::

            offset_ns = anchor.anchor_utc_ns − rtp_to_wallclock(anchor.anchor_rtp) × 1e9

        Recomputed every status write because radiod's anchor refreshes
        on its own cadence (sub-second under the v3.16 STATUS listener,
        otherwise frozen at SSRC discovery).  Returns None when the
        native anchor isn't captured yet or rtp_to_wallclock fails.
        """
        if (self._t6_native_anchor is None
                or self._t6_channel_info is None):
            return None
        try:
            from ka9q.rtp_recorder import rtp_to_utc
            wall = rtp_to_utc(
                int(self._t6_native_anchor.anchor_rtp) & 0xFFFFFFFF,
                self._t6_channel_info,
            )
        except Exception:
            return None
        if wall is None:
            return None
        return int(self._t6_native_anchor.anchor_utc_ns - int(wall * 1e9))

    def _t6_check_stale_lock(self) -> None:
        """Re-validate the LOCK against GPS; drop it if it is stale.

        Step-recovery guards each CANDIDATE (see t6_stale_lock for why
        that is right and must stay).  Nothing guarded the lock, so on
        AC0G-B4 2026-08-25 a 225,754,278 ns lock stood against T5's
        ~149,000,000 ns for hours while the station ran 26 ms wrong on a
        coarse fallback anchor.

        Escapes only on a contradiction too large to be T5 noise and too
        small to be the ±0.5 s sidelobe, sustained past the dwell.  The
        action is exactly step-recovery's accept: drop the lock and the
        disambiguation so the next cycle re-references against the
        timing-tier hierarchy.
        """
        if getattr(self, '_t6_last_chain_delay_ns', None) is None:
            return
        clock = getattr(self, '_t6_stale_lock_clock', None) or time.monotonic
        now = float(clock())
        last = getattr(self, '_t6_stale_lock_last_eval', None)
        if (last is not None
                and (now - last) < self.T6_STALE_LOCK_EVAL_PERIOD_S):
            return
        self._t6_stale_lock_last_eval = now
        watch = getattr(self, '_t6_stale_lock_watch', None)
        if watch is None:
            from hf_timestd.core.t6_stale_lock import StaleLockWatch
            watch = self._t6_stale_lock_watch = StaleLockWatch()
        t5_implied = self._t5_implied_effective_chain_delay()
        delta = None
        if t5_implied is not None:
            delta = wrap_chain_delay_ns(
                int(t5_implied - self._t6_last_chain_delay_ns))
        if not watch.observe(delta, now):
            return
        from hf_timestd.core.t6_stale_lock import STALE_LOCK_DWELL_S
        logger.critical(
            "T6 STALE LOCK ABANDONED: GPS (T5) contradicted the locked "
            "chain_delay %d ns by %+d ns for more than %.0f s — too "
            "large to be T5 noise, too small to be the matched filter's "
            "±0.5 s sidelobe, so the LOCK is the stale one, not the "
            "candidate. Dropping lock and disambiguation; the next cycle "
            "re-references against the timing-tier hierarchy. "
            "(hf-timestd: B4 2026-08-25 held a 76.8 ms contradiction for "
            "hours with no way out.)",
            self._t6_last_chain_delay_ns, int(delta), STALE_LOCK_DWELL_S,
        )
        self._t6_capture_anomaly("stale-lock-abandoned")
        if getattr(self, '_t6_authority', None) is not None:
            self._t6_apply_authority_decision(
                self._t6_authority.on_mf_unlock())
        if getattr(self, '_t6_fine_stage', None) is not None:
            self._t6_fine_stage.reset()
            # reset() alone is not an escape: it keeps the stage's own
            # tracking offset (by design — it runs at every fold-block
            # boundary).  This site has just repudiated the position, so
            # say so, or the stage goes on localising exactly where the
            # abandoned lock put it and re-installs it within one fold
            # block.  See BpskEdgeFineStage.clear_own_offset.
            self._t6_fine_stage.clear_own_offset()
        self._t6_last_chain_delay_ns = None
        self._t6_disambiguation_ns = 0
        self._t6_wrap_rejections = 0
        self._t6_recent_raw.clear()

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
            from ka9q.rtp_recorder import rtp_to_utc
            raw_wall_time_sec = rtp_to_utc(
                last_edge_rtp, self._t6_channel_info
            )
            if raw_wall_time_sec is None:
                return None
            # Same integer-second alignment as the production T5
            # disambig site — see ``_t6_disambiguate_via_t5_lb1421``
            # for the rationale.  The sanity check here uses only the
            # sub-second residual, which is the physical chain_delay.
            delta_sec = raw_wall_time_sec - reading.pps_utc_sec
            integer_offset = int(round(delta_sec))
            effective_pps_utc_sec = (
                int(reading.pps_utc_sec) + integer_offset
            )
            residual_sec = raw_wall_time_sec - effective_pps_utc_sec
            if abs(residual_sec) > 0.5:
                return None
            return residual_sec * 1e9
        except Exception:
            return None

    # A fine-stage edge is the last edge of the fold block that just
    # completed, so it is at most ~1 s behind the batch that closed the
    # block.  An edge further than this from the paired arrival means
    # the stream or the RTP counter moved under us — refuse the NMEA
    # pairing rather than name a second by extrapolation.
    T6_NAMING_MAX_EDGE_AGE_SEC = 60.0

    def _t6_name_second_via_nmea(self, edge_rtp: int) -> Optional[float]:
        """NMEA-derived UTC of a fine-stage edge, or None.

        Mirrors the epoch pairing of
        :meth:`_t6_disambiguate_via_t5_lb1421` /
        :meth:`_compute_lb1421_residual_ns`: the LB-1421 reading carries
        its own capture epoch (``host_monotonic_at_read``, and a
        ``valid_fix`` that is only True when gpsdo-monitor's published
        NMEA second and the host clock agree at the integer-second
        level), and ``T5RtpPairing`` carries the RTP counter observed at
        a known monotonic instant.  Between them the edge's UTC follows
        from counter arithmetic:

            arrival_wall = now_wall − (now_mono − arrival_mono)
            edge_utc     = arrival_wall + (edge_rtp − arrival_rtp)/SR

        The integer-second AUTHORITY is NMEA's ``pps_utc_sec``; the host
        clock only indexes *which* second, the sanctioned sub-second
        indexing role documented in ``_t6_disambiguate_via_t5_lb1421``
        and ``t5_rtp_pairing``.  Critically, ``rtp_to_wallclock`` — the
        radiod GPS-pair estimate whose >0.5 s excursions are exactly
        what the inversion exists to bypass — is not consulted at all.

        Returns the (fractional) UTC of the edge; the caller rounds.
        """
        from .t5_rtp_pairing import T5RtpPairing
        from .offset_judge import _rtp_delta_signed

        probe = getattr(self, '_lb1421_probe', None)
        pairing = getattr(self, '_t5_pairing', None)
        if probe is None or pairing is None:
            return None
        reading = probe.get_latest()
        if reading is None:
            return None
        arrival = pairing.latest_arrival
        if arrival is None:
            return None
        sr = self._t6_sample_rate()
        if not sr:
            return None
        arrival_rtp, arrival_mono = arrival
        age = pairing.now_mono() - arrival_mono
        if age < 0.0 or age > T5RtpPairing.ARRIVAL_MAX_AGE_S:
            return None
        arrival_wall = pairing.now_wall() - age
        # NMEA attestation window — same guard T5RtpPairing.compute uses.
        # Outside it, host and GPS truth disagree beyond what the reading
        # attests and the pairing would be poisoned.
        nmea_delay = arrival_wall - float(reading.pps_utc_sec)
        if not (T5RtpPairing.NMEA_DELAY_MIN_S
                <= nmea_delay
                <= T5RtpPairing.NMEA_DELAY_MAX_S):
            return None
        delta_s = _rtp_delta_signed(
            int(edge_rtp) & 0xFFFFFFFF, int(arrival_rtp)) / float(sr)
        if abs(delta_s) > self.T6_NAMING_MAX_EDGE_AGE_SEC:
            return None
        return arrival_wall + delta_s

    def _t6_sample_rate(self) -> Optional[int]:
        """T6 channel sample rate from whichever T6 object is wired."""
        for obj in (getattr(self, '_t6_fine_stage', None),
                    getattr(self, '_t6_calibrator', None)):
            sr = getattr(obj, 'sample_rate', None)
            try:
                sr = int(sr)
            except (TypeError, ValueError):
                continue
            if sr > 0:
                return sr
        return None

    def _t6_name_integer_second(self, edge_rtp: int) -> Optional[int]:
        """Name the integer UTC second of a fine-stage edge (spec §2).

        The coarse cascade (T5 NMEA preferred, radiod-pair wall estimate
        as fallback) only NAMES the second — it needs ±0.5 s accuracy
        and its noise cannot enter the sub-second value.  Residual
        beyond ±0.4 s (margin inside the cell) → None (invariant 3).

        The NMEA branch derives the second from the LB-1421 reading and
        the RTP-substrate arrival pairing, NOT from
        ``rtp_to_wallclock``.  The previous implementation added
        ``round(wall − pps_utc_sec)`` back onto an integer
        ``pps_utc_sec``, which is identically ``round(wall)`` — NMEA
        contributed nothing and a radiod-pair wall error beyond ±0.5 s
        (seen repeatedly in fleet history) named the wrong second with
        no signal at all.  ``wall`` is now only the fallback source, and
        its disagreement with the NMEA-derived second is *reported*
        (spec §6 invariant 5), never used to correct or to veto.
        """
        edge_utc = self._t6_name_second_via_nmea(edge_rtp)
        wall = None
        try:
            from ka9q.rtp_recorder import rtp_to_utc
            wall = rtp_to_utc(
                int(edge_rtp) & 0xFFFFFFFF, self._t6_channel_info)
        except Exception:
            wall = None

        if edge_utc is not None:
            reading = self._lb1421_probe.get_latest()
            if reading is None:
                # Raced with a probe expiry between the two reads.
                edge_utc = None
            else:
                # NMEA holds the integer-second authority; the host
                # clock only says which second (see helper docstring).
                named = (int(reading.pps_utc_sec)
                         + int(round(edge_utc - reading.pps_utc_sec)))
                if abs(edge_utc - named) > 0.4:
                    return None
                self._t6_report_naming_vs_radiod_pair(wall, edge_utc)
                return self._t6_reconcile_naming(named, edge_rtp)

        if wall is None:
            return None
        named = int(round(wall))
        if abs(wall - named) > 0.4:
            return None
        return self._t6_reconcile_naming(named, edge_rtp)

    def _t6_reconcile_naming(self, named: int, edge_rtp: int) -> int:
        """Remove a whole-second slip from the integer-second naming.

        The rounding above cannot detect its own off-by-one: when it tips,
        ``named`` moves with it and ``abs(edge_utc - named)`` is small
        again.  The RTP counter can — it is GPSDO-disciplined and
        continuous, so the previous anchor carried forward by ΔRTP/f_s
        predicts where this edge must fall, and that prediction does not
        move with the answer.

        Measured on AC0G-B4: three whole-second excursions across 2,176
        consecutive anchor pairs, two of them under the legacy convention.
        Each lasted one anchor (~30 s) and self-corrected, so it was
        invisible unless something sampled inside the window.
        """
        try:
            from .t6_naming_continuity import reconcile_named_second
            fixed, slip = reconcile_named_second(
                named, edge_rtp, getattr(self, '_t6_native_anchor', None),
                self._t6_sample_rate(),
            )
        except Exception:  # noqa: BLE001 — a guard must not break naming
            return named
        if slip:
            self._t6_naming_slips = getattr(self, '_t6_naming_slips', 0) + 1
            logger.warning(
                "T6 naming slip CORRECTED: the integer second was named "
                "%+d s from where the RTP counter puts this edge (%d -> "
                "%d). The counter is GPSDO-disciplined and continuous, so "
                "the naming is the error, not the ruler. Slips this run: "
                "%d.", slip, named, fixed, self._t6_naming_slips,
            )
        return fixed

    # Throttle for the cross-tier naming disagreement report.
    T6_NAMING_DISAGREE_LOG_PERIOD_SEC = 300.0

    def _t6_report_naming_vs_radiod_pair(
        self, wall: Optional[float], edge_utc: float
    ) -> None:
        """Spec §6 invariant 5: T6-vs-radiod-pair disagreement is
        REPORTED, never corrective.  A radiod-pair wall estimate more
        than half a second from the NMEA-derived edge UTC would have
        named the wrong second under the old rounding — say so, loudly
        but throttled, and carry on with the NMEA answer."""
        if wall is None:
            return
        delta = wall - edge_utc
        self._t6_naming_vs_radiod_pair_s = delta
        if abs(delta) <= 0.5:
            return
        now = time.monotonic()
        last = getattr(self, '_t6_naming_disagree_log_wall', None)
        if (last is not None
                and now - last < self.T6_NAMING_DISAGREE_LOG_PERIOD_SEC):
            return
        self._t6_naming_disagree_log_wall = now
        logger.warning(
            "T6 naming: radiod-pair wall estimate disagrees with the "
            "NMEA-derived edge UTC by %+.3f s (> 0.5 s) — the radiod "
            "GPS pair would have named the WRONG integer second. "
            "Naming from NMEA (T5) as designed; reported only, no "
            "correction applied (spec §6 invariant 5).",
            delta,
        )

    # Throttle for the derived-residual diagnostic.
    T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0

    @staticmethod
    def _t6_resolve_chain_delay_ns(
        residual_sec: float, chain_delay_calib_s: float
    ) -> tuple[int, int]:
        """Return ``(asserted_ns, reported_residual_ns)``.

        Stage 1 of ``docs/design/T6_ORIGIN_ASSERTION_DESIGN.md`` §5: the
        chain delay is ASSERTED from configuration and never derived
        from radiod's advertised wall clock.  Deriving it made the
        correction to radiod's RTP→UTC mapping a function of that same
        mapping, and re-deriving it at every authority UNLOCK (58 in one
        night on AC0G-B4) produced a different origin each time.

        The derived residual is returned alongside for REPORTING only.
        """
        return (int(round(chain_delay_calib_s * 1e9)),
                int(round(residual_sec * 1e9)))

    def _t6_ref_tracker(self):
        """The learned-reference gate, or None when it is switched off.

        OPT-IN, default off: this changes which locks T6 accepts, so it
        must not alter a deployed station's behaviour until a config bump
        explicitly asks for it.

        Why it exists: the Layer B guard below bounds |chain_delay| at
        ±250 ms, which is WIDER than the first sidelobe cluster it cites
        (200 ms), so it cannot reject that one.  Measured on B4 2026-08-28,
        1572 locks landed in 27 clusters spanning +53..+993 ms and lock
        quality did not discriminate between them.  A learned per-station
        reference does: B4's true chain delay is ~15-16 ms, and everything
        else is a phantom.

        ⛔ The reference is never a shipped constant — it is a property of one
        installation's cable, hardware revision and channel filter, so the
        fingerprint below drops the latch whenever the channel config moves.
        """
        if not self._t6_config.get('reference_gate_enabled', False):
            return None
        fingerprint = "{}/{}/{}".format(
            self._t6_config.get('sample_rate'),
            self._t6_config.get('low_edge_hz'),
            self._t6_config.get('high_edge_hz'),
        )
        tracker = getattr(self, '_t6_ref_tracker_obj', None)
        if tracker is None:
            from hf_timestd.core.t6_reference_resolver import T6ReferenceTracker
            tracker = T6ReferenceTracker(
                tolerance_ns=int(self._t6_config.get(
                    'reference_tolerance_ns', 5_000_000)),
                min_attestations=int(self._t6_config.get(
                    'reference_min_attestations', 20)),
                config_fingerprint=fingerprint,
            )
            self._t6_ref_tracker_obj = tracker
            logger.info(
                "T6 reference gate ENABLED: tolerance ±%.3f ms, "
                "min_attestations %d, config %s",
                tracker.tolerance_ns / 1e6, tracker.min_attestations,
                fingerprint,
            )
        elif tracker.set_config_fingerprint(fingerprint):
            logger.warning(
                "T6 reference gate: channel config changed to %s — latched "
                "reference DROPPED (chain delay moves with the filter's "
                "group delay); relearning from scratch",
                fingerprint,
            )
        return tracker

    def _t6_reference_gate_rejects(self, reported_residual_ns: int) -> bool:
        """Feed the NMEA-attested residual to the tracker, then gate on it.

        ``reported_residual_ns`` is radiod wall-clock minus the NMEA integer
        second, i.e. attested by the LB-1421 GPSDO — independent of T6's own
        estimator, which is what makes it usable as a reference.  Observe
        BEFORE gating so the tracker learns from the full attested
        population rather than only from what it already agrees with; the
        modular centre plus the support requirement carry the robustness.

        Returns True when the caller should refuse this lock.
        """
        tracker = self._t6_ref_tracker()
        if tracker is None:
            return False
        tracker.observe(attested_ns=reported_residual_ns)
        if tracker.reference_ns is None:
            return False          # still learning — never refuse on no reference
        outcome = tracker.gate(candidate_ns=reported_residual_ns)
        if outcome.accepted:
            return False
        logger.warning(
            "T6 reference gate REFUSED chain_delay %+.3f ms: %+.3f ms from "
            "the learned reference %+.3f ms (tolerance ±%.3f ms). Holding "
            "last-good and falling back to T4 — this is the phantom the "
            "±250 ms plausibility bound admits.",
            reported_residual_ns / 1e6, (outcome.delta_ns or 0) / 1e6,
            tracker.reference_ns / 1e6, tracker.tolerance_ns / 1e6,
        )
        return True

    def _t6_report_derived_residual(
        self, feed: str, reported_ns: int, asserted_ns: int
    ) -> None:
        """Spec §6 invariant 5 pattern: report, never correct.

        The derived residual is the diagnostic that produced
        T6_ORIGIN_ASSERTION_DESIGN.  It keeps being measured and
        surfaced; it simply stops steering the anchor.
        """
        self._t6_derived_residual_ns = reported_ns
        now = time.monotonic()
        last = getattr(self, '_t6_residual_report_wall', None)
        if (last is not None
                and now - last < self.T6_RESIDUAL_REPORT_PERIOD_SEC):
            return
        self._t6_residual_report_wall = now
        logger.warning(
            "T6 %s: derived residual %+.3f ms (radiod wall-clock minus "
            "NMEA integer second) — REPORTED ONLY, not corrective; "
            "chain_delay asserted as %+.3f ms from chain_delay_calib_s "
            "(T6_ORIGIN_ASSERTION_DESIGN §5)",
            feed, reported_ns / 1e6, asserted_ns / 1e6,
        )

    def _t6_dissent_sigma_floor_ns(self):
        """The sigma floor the witnesses currently impose, or None.

        hf-timestd#29's actuator.  The shadow channel has been right and
        ignored for days at a time because ``cross_bench_conflict`` gates
        tier ADVANCEMENT and T6 is already top tier.  This is the other
        direction: when independent witnesses AGREE WITH EACH OTHER that
        the adopted bench is wrong, chrony is told how wrong, in the one
        field it actually weighs.

        Widening precision is deliberately weaker than withdrawal — it
        lets chrony demote HPPS on the evidence rather than taking the
        feed dark, and going dark is us doing chrony's job badly
        (t6_holdover).
        """
        judge = getattr(self, '_offset_judge', None)
        d = getattr(judge, '_dissent', None) if judge is not None else None
        if d is None:
            return None
        try:
            return float(d.sigma_floor_ns)
        except (TypeError, ValueError):
            return None

    def _t6_capture_anomaly(self, reason: str) -> None:
        """Dump the pre-trigger T6 IQ window, budget permitting.

        A ledger row is the matched filter's OUTPUT; raw samples are the
        only way to re-run the filter on an event where it may have been
        wrong.  Rate limiting lives inside ``AnomalyCapture`` and is not
        optional — the 2026-08-25 livelock re-entered its failure branch
        at ~1 Hz for hours.
        """
        cap = getattr(self, '_t6_anomaly', None)
        if cap is None:
            return
        try:
            cap.trigger(reason)
        except Exception:  # noqa: BLE001 — diagnostics never break capture
            pass

    def _t6_observe_channel_epochs(self, mono_now=None) -> None:
        """Feed one (GPS_TIME, RTP_TIMESNAP) pair per channel to the
        least-late epoch estimators (see ``cross_channel_rtp``).

        Throttled to ~1 Hz.  The offset being estimated is CONSTANT within
        a radiod session — both counters come off the same ADC clock — so
        this only ever sharpens, and the minimum converges within minutes
        despite ms-class pair noise.  Cheap enough for the sample path:
        two attribute reads and a comparison.
        """
        now = time.monotonic() if mono_now is None else float(mono_now)
        last = getattr(self, '_t6_epoch_last_obs', None)
        if last is not None and (now - last) < 1.0:
            return
        self._t6_epoch_last_obs = now
        from .cross_channel_rtp import ChannelEpoch, PairObservation

        def _obs(ci):
            try:
                gps = int(getattr(ci, 'gps_time', None))
                snap = int(getattr(ci, 'rtp_timesnap', None))
                sr = int(getattr(ci, 'sample_rate', None))
            except (TypeError, ValueError):
                return None
            if not sr or not gps or not snap:
                return None
            return PairObservation(gps_time_ns=gps, rtp_timesnap=snap,
                                   sample_rate_hz=sr)

        o6 = _obs(getattr(self, '_t6_channel_info', None))
        if o6 is not None:
            if getattr(self, '_t6_epoch', None) is None:
                self._t6_epoch = ChannelEpoch()
            self._t6_epoch.observe(o6)

        # Any metrology channel will do: B4 measured the six 24 kHz
        # channels sharing ONE counter space to 1.937 ms, which is the
        # pair non-atomicity rather than an origin difference.
        for rec in (getattr(self, 'recorders', {}) or {}).values():
            ci = getattr(rec, 'channel_info', None)
            op = _obs(ci)
            if op is None or op.sample_rate_hz == getattr(
                    o6, 'sample_rate_hz', None):
                continue
            if getattr(self, '_peer_epoch', None) is None:
                self._peer_epoch = ChannelEpoch()
                self._peer_rate_hz = op.sample_rate_hz
            if op.sample_rate_hz == self._peer_rate_hz:
                self._peer_epoch.observe(op)
            break

    def _t6_peer_rtp(self, anchor):
        """The anchor's instant in the metrology channels' counter space.

        Returns ``(peer_rtp, peer_rate_hz)`` or ``(None, None)``.  Without
        this a reader holding the ledger and the metrology IQ cannot
        connect them: the T6 channel's 96 kHz space does not relate to the
        24 kHz channels by scaling (B4: 362,095,021 samples ≈ 3772 s).
        """
        e6 = getattr(self, '_t6_epoch', None)
        ep = getattr(self, '_peer_epoch', None)
        rate = getattr(self, '_peer_rate_hz', None)
        if (e6 is None or ep is None or not rate
                or e6.epoch_s is None or ep.epoch_s is None):
            return None, None
        try:
            from .cross_channel_rtp import rtp_in_other_channel
            return rtp_in_other_channel(
                int(anchor.anchor_rtp), e6.epoch_s,
                int(anchor.sample_rate_hz), ep.epoch_s, int(rate),
                float(anchor.anchor_utc_ns) / 1e9,
            ), int(rate)
        except Exception:  # noqa: BLE001 — provenance must not break capture
            return None, None

    def _t6_ledger_append(self, anchor, authority_state=None) -> None:
        """Record a captured native anchor in the durable ledger.

        The anchor tuple is the minimal durable record of T6: with the
        raw components stored separately, any future recalibration of
        the asserted chain-delay terms is pure arithmetic over the
        ledger (see t6_anchor_ledger).  getattr-guarded like the
        chain-delay stores — test harnesses build the recorder via
        ``__new__`` — and the ledger itself never raises.
        """
        ledger = getattr(self, '_t6_anchor_ledger', None)
        if ledger is None:
            return
        auth = getattr(self, '_t6_authority', None)
        cal = getattr(self, '_t6_calibrator', None)
        est = getattr(self, '_t6_last_fine_est', None)
        quality = None
        if est is not None:
            quality = {
                "plateau_amplitude": getattr(est, 'plateau_amplitude', None),
                "fit_rms": getattr(est, 'fit_rms', None),
                "n_seconds_folded": getattr(est, 'n_seconds_folded', None),
                "edge_subsample": getattr(est, 'edge_subsample', None),
            }
        # peer_rtp is WITHHELD pending hf-timestd#37.  It was derived
        # from channel_info.rtp_timesnap, and B4 2026-08-25 shows that
        # counter is NOT the stream's: one WWV_20000 sidecar carries
        # start_rtp_timestamp=2,189,978,643 beside rtp_timesnap=
        # 1,931,544,192 -- 258 M samples (3 h) apart for the same
        # channel at the same moment.  An offline reader holds STREAM
        # rtp, so the mapped value would be unusable, and a wrong number
        # here is worse than an absent one: the whole point of this
        # schema is that a field means what it says.
        peer_rtp, peer_rate = None, None
        ledger.append(
            anchor,
            authority_state=authority_state,
            delay_budget_ns=getattr(auth, 'delay_budget_ns', None),
            filter_group_delay_ns=getattr(auth, 'filter_group_delay_ns', None),
            labeling_convention=getattr(
                self, '_t6_labeling_convention', None),
            peer_rtp=peer_rtp,
            peer_rate_hz=peer_rate,
            quality=quality,
            # An audit that has run and seen no mismatch has MEASURED
            # zero; only an audit that has not run is unknown.  _lbl_drift
            # is created lazily on the first mismatch, so its absence
            # alone cannot distinguish the two -- _lbl_batches can.
            zero_fill=(_zf.snapshot()
                       if (_zf := getattr(self, '_t6_zero_fill', None))
                       else None),
            label_drift_samples=(
                int(getattr(cal, '_lbl_drift', 0) or 0)
                if getattr(cal, '_lbl_batches', 0) else None),
        )

    def _t6_apply_authority_decision(self, decision) -> None:
        """Install/invalidate the T6 anchor per the authority decision.
        Every state transition is loud (expose-don't-correct)."""
        from hf_timestd.core.t6_anchor_authority import T6AuthorityState
        prev = decision.previous_state
        if decision.state is not prev:
            # Carry the MEASURED magnitudes, not just the violation
            # names: a bare "fine_coarse" cannot tell a 5.1 ms breach
            # from a 500 ms one, and this transition is what gates the
            # station's highest timing tier.
            _metrics = getattr(
                getattr(self, '_t6_authority', None),
                'last_check_metrics', None,
            ) or {}
            _measured = " ".join(
                # Only the measured magnitudes get 3 decimals; a flag
                # such as fine_coarse_unverified rendered as "=1.000"
                # reads as a measurement it is not.
                (f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}")
                for k, v in sorted(_metrics.items())
            )
            logger.warning(
                "T6 anchor authority: %s → %s%s%s",
                prev.value, decision.state.value,
                (f" (violations: {', '.join(decision.violations)})"
                 if decision.violations else ""),
                (f" [measured: {_measured}]" if _measured else ""),
            )
        self._t6_authority_last_decision = decision
        if (decision.state is not T6AuthorityState.AUTHORITATIVE
                and prev is T6AuthorityState.AUTHORITATIVE):
            # Losing authority is the event whose cause is in the past —
            # exactly what the pre-trigger ring holds.
            self._t6_capture_anomaly(f"authority-{decision.state.value}")
        if decision.state is T6AuthorityState.AUTHORITATIVE:
            self._t6_native_anchor = decision.anchor
            self._t6_ledger_append(
                decision.anchor, authority_state=decision.state.value)
            if decision.state is not prev:
                self._t6_rate_reset("native anchor captured via T6")
            # Log the anchor in the shape scripts/t6_origin_spread.py
            # parses.  Post-inversion THIS is the anchor -- the coarse
            # capture is logged and then superseded seconds later -- so
            # without this the origin-spread tool could only ever see
            # the anchor that stopped being used.  Throttled: the fine
            # stage refreshes per fold block, and every state change is
            # logged regardless.
            _now = time.monotonic()
            _last = getattr(self, '_t6_anchor_log_wall', None)
            if (decision.state is not prev or _last is None
                    or _now - _last >= self.T6_RESIDUAL_REPORT_PERIOD_SEC):
                self._t6_anchor_log_wall = _now
                logger.info(
                    "T6 anchor authority AUTHORITATIVE; %s",
                    format_native_anchor_log(decision.anchor, "T6"),
                )
        elif decision.state is T6AuthorityState.UNLOCKED and prev in (
                T6AuthorityState.AUTHORITATIVE, T6AuthorityState.DEGRADED):
            # Invalidate so the legacy cascade re-captures via T5 —
            # loud fallback, never a silently stale T6 anchor.
            self._t6_native_anchor = None
            # Also reopen the legacy cascade's own gate.  The T5/T4
            # cascade in _t6_on_samples only re-runs disambiguation
            # when _t6_last_chain_delay_ns is None (first-lock branch);
            # an authority-only unlock (e.g. DEGRADED dwell timeout
            # while the MF itself is still locked) leaves that gate
            # closed forever otherwise, and the HPPS/chrony feed goes
            # dead until the fine stage independently re-acquires.
            # Harmless double-set on the stuck-recovery path, which
            # already clears these itself.
            self._t6_last_chain_delay_ns = None
            self._t6_disambiguation_ns = 0
            recent_raw = getattr(self, '_t6_recent_raw', None)
            if recent_raw is not None:
                recent_raw.clear()
        # DEGRADED: hold the last good anchor (GPSDO coasting) — the
        # legacy cascade gate stays closed; coasting must not
        # re-trigger a fresh disambiguation walk.

    # Coast for as long as we can honestly STATE how bad we are.
    #
    # chrony adjudicates, so the feed's obligation is to stay available
    # and carry a truthful sigma -- not to go dark, which is us doing
    # chrony's job badly and which forces hpps-watchdog to restart the
    # recorder, destroying the very anchor the coast rests on.  The one
    # thing we must never do is publish a claim we cannot express: the
    # SHM precision field saturates at PRECISION_CEILING, so beyond
    # 2**PRECISION_CEILING seconds the pushed precision would UNDERSTATE
    # the uncertainty.  That, not a timer and not a quality target, is
    # where a coast has to stop.
    #
    # An earlier 5 ms bound was wrong twice over: it ended coasts that
    # were still perfectly honest, and it refused a coarse-cascade
    # anchor (25 ms) outright — which is exactly the case where staying
    # available matters most.
    T6_HOLDOVER_MAX_SIGMA_NS = 2.0 ** PRECISION_CEILING * 1e9  # 62.5 ms

    def _t6_floor_snapshot(self):
        """(FloorEstimate|None, mono_now, wall_now) — never raises.

        ``mono_now`` and ``wall_now`` are read ADJACENTLY: that pair is
        the CLOCK_MONOTONIC→CLOCK_REALTIME offset, and everything the
        coast publishes is expressed through it.

        ``record=False``: this is not the judge, and folding these
        reads into the floor's fixed-length sigma history would shorten
        the horizon the bench publishes.
        """
        pairing = getattr(self, '_t5_pairing', None)
        tracker = getattr(self, '_t6_arrival_floor', None)
        try:
            mono_now = pairing.now_mono() if pairing is not None else 0.0
            wall_now = time.time()
        except Exception:  # noqa: BLE001 — gate must never die
            return None, 0.0, time.time()
        if tracker is None:
            return None, mono_now, wall_now
        try:
            floor = tracker.estimate(mono_now, record=False)
        except Exception:  # noqa: BLE001
            floor = None
        return floor, mono_now, wall_now

    def _t6_publish_mode(self, floor, mono_now):
        """(mode, sigma_ns, reason) — mode is "live", "holdover" or None.

        Losing carrier lock stops us LEARNING; it does not invalidate
        the anchor.  The anchor is a (rtp, utc) pair and the RTP counter
        is GPSDO-disciplined, so a frozen anchor keeps labelling
        correctly and only our uncertainty about the RATE grows —
        measured on AC0G-B4 at 0.0004 ppm, i.e. 1.44 us/hour.  A
        six-hour storm costs 8.6 us.  Going dark through that throws
        away a good clock, so we coast and say what it is worth.

        ⚠ This is NOT a return to hf-timestd#14.  That coast kept
        deriving fresh chain delays from noise and publishing them, so
        each re-lock landed somewhere new and chrony saw a 203 ms
        standard deviation.  This one admits NO new measurement: the
        anchor is frozen at the last validated edge, the second is
        named from the anchor rather than from an edge detected during
        the outage, and the coast is abandoned outright if the RTP
        counter is re-based underneath it.
        """
        from hf_timestd.core.t6_anchor_authority import T6AuthorityState
        from hf_timestd.core.t6_holdover import (
            coast_ruler_intact, coast_ruler_intact_by_drift,
            coast_sigma0_ns, holdover_sigma_ns,
            unmeasured_rate_sigma_ppm,
            may_coast,
        )

        cal = getattr(self, '_t6_calibrator', None)
        costas = getattr(cal, 'costas_locked', None)
        auth = getattr(self, '_t6_authority', None)
        state = getattr(auth, 'state', None)
        # ``costas_locked is None`` means the legacy (non-MF)
        # calibrator, which has no Costas loop — stay permissive.
        healthy = costas is not False and (
            state is None or state is T6AuthorityState.AUTHORITATIVE
        )
        if healthy:
            self._t6_holdover_since = None
            self._t6_holdover_sigma0_ns = None
            self._t6_holdover_floor0_s = None
            self._t6_holdover_drift0 = None
            self._t6_holdover_anchor_id = None
            return "live", None, "ok"

        # The freeze reference is ANCHOR-SCOPED.  Arrival-floor offsets
        # are expressed relative to the anchor and ``_t6_rate_reset``
        # deliberately clears the floor on every recapture, so a
        # reference taken against one anchor says nothing about the
        # next.  Comparing across generations reported every ordinary
        # recapture as an RTP re-base: on B4 overnight 2026-08-17 all 9
        # withdrawals said "the ruler was re-based" when nothing had
        # been, the coast refused, HPPS went dark, and hpps-watchdog
        # restarted the recorder 7 times -- each restart recapturing the
        # anchor and invalidating the reference again.
        anchor = getattr(self, '_t6_native_anchor', None)
        anchor_id = None if anchor is None else (
            getattr(anchor, 'anchor_rtp', None),
            getattr(anchor, 'anchor_utc_ns', None),
            getattr(anchor, 'captured_via_tier', None),
        )
        if floor is not None and (
                getattr(self, '_t6_holdover_since', None) is None
                or getattr(self, '_t6_holdover_anchor_id', None)
                != anchor_id):
            self._t6_holdover_since = float(mono_now)
            self._t6_holdover_sigma0_ns = coast_sigma0_ns(
                floor.sigma_ns, getattr(anchor, 'captured_via_tier', None)
            )
            self._t6_holdover_floor0_s = float(floor.offset_s)
            self._t6_holdover_drift0 = getattr(cal, '_lbl_drift', None)
            self._t6_holdover_anchor_id = anchor_id

        since = getattr(self, '_t6_holdover_since', None)
        sigma0 = getattr(self, '_t6_holdover_sigma0_ns', None)
        floor0 = getattr(self, '_t6_holdover_floor0_s', None)
        rate = getattr(getattr(self, '_t6_rate_est', None), 'current', None)

        elapsed = 0.0 if since is None else max(0.0, float(mono_now) - since)
        # hf-timestd#41: the unmeasured-rate stand-in depends on the
        # A-axis.  Guessing A1 on a free-running TCXO would coast for
        # days on an oscillator drifting milliseconds per hour.
        sigma_ns = holdover_sigma_ns(
            sigma0 or 0.0, getattr(rate, 'sigma_ppm', None), elapsed,
            unmeasured_ppm=unmeasured_rate_sigma_ppm(self._t6_a_level()),
        )
        cause = "authority %s, costas %s" % (
            "unknown" if state is None else state.value,
            "unlocked" if costas is False else "locked",
        )
        # Distinguish "no reference yet" from "the ruler moved".  Both
        # refuse, but only one of them means a radiod restart, and
        # reporting the wrong one sends an operator hunting a phantom
        # (observed on B4 2026-08-17 right after a deploy).
        if floor is None:
            return None, sigma_ns, (
                "no arrival-floor estimate yet — nothing to coast "
                "against (%s)" % cause
            )
        # Counter continuity, measured rather than inferred.  The
        # calibrator audits every batch's declared RTP against
        # (previous declared + previous length); that accumulated
        # discrepancy IS the ruler.  The arrival-floor offset is an
        # arrival-LATENCY estimate and only ever stood in for it --
        # on B4 2026-08-25 it reported a re-base 7.8 s before the
        # audit reported 0 mismatched batches out of 1,620,545.
        # Fall back to the old proxy only when the audit is absent.
        drift0 = getattr(self, '_t6_holdover_drift0', None)
        ruler_ok = coast_ruler_intact_by_drift(
            getattr(cal, '_lbl_drift', None), drift0,
            getattr(cal, 'sample_rate', 0) or 0)
        if ruler_ok is None:
            ruler_ok = coast_ruler_intact(floor.offset_s, floor0)
        ok, reason = may_coast(
            anchor_frozen=anchor is not None,
            rtp_continuous=ruler_ok,
            sigma_ns=sigma_ns,
            max_sigma_ns=self.T6_HOLDOVER_MAX_SIGMA_NS,
        )
        if ok:
            # A permitted coast reports WHY it is coasting, not that the
            # preconditions passed ("ok" tells an operator nothing).
            return "holdover", sigma_ns, cause
        # A refused one carries both: the precondition that blocked the
        # coast, and the state that got us here.
        return None, sigma_ns, "%s (%s)" % (reason, cause)

    @staticmethod
    def _wwvb_rtp_advance_report(prev_rtp, prev_len, rtp0):
        """Compare RTP advance between batches against samples delivered.

        Diagnostic for hf-timestd#23.  The WWVB continuity check assumes
        RTP advances one tick per delivered sample::

            expected = anchor_rtp + buf_samples

        On AC0G-B4 the arriving RTP falls short of that by a CONSTANT
        fraction of the batch, at both rates tested -- 1620/3600 = 0.450
        at 24 kHz, 1200/2640 = 0.455 at 12 kHz -- so the mismatch is
        proportional, not a delivery boundary precessing against radiod's
        block (changing the sample rate did not move it).

        One of the two terms must therefore be wrong by a fixed factor.
        Comparing consecutive batches separates the cases: if RTP
        advances by exactly the PREVIOUS batch's length then the RTP
        clock is fine and ``buf_samples`` is being over-counted (e.g.
        duplicated or overlapping buffers); if it advances by ~0.55 of
        it, RTP is not counting delivered samples 1:1 and the check
        needs to be made rate-aware.

        Returns None when they agree (the common case, nothing to say),
        else a dict of the numbers that discriminate.  Pure; never
        raises.
        """
        if prev_rtp is None or prev_len is None:
            return None
        rtp_delta = (int(rtp0) - int(prev_rtp)) & 0xFFFFFFFF
        prev_len = int(prev_len)
        if rtp_delta == prev_len:
            return None
        return {
            "sample_len": prev_len,
            "rtp_delta": rtp_delta,
            # Positive => RTP advanced LESS than samples delivered.
            "deficit": prev_len - rtp_delta,
            "ratio": (rtp_delta / prev_len) if prev_len else float("nan"),
        }

    def _t6_hpps_publish_status(self) -> dict:
        """What we BELIEVE about the HPPS feed, for external watchers.

        ``hpps-watchdog`` restarts the recorder when chrony stops
        sampling HPPS.  That is the correct cure for the failure it was
        built for — the push gate stops firing while the calibrator
        still reports ``acquired=1``, so everything looks fine in the
        journal while chrony sees reach=0 — and the WRONG cure for an
        honest withdrawal, because a restart destroys the anchor a coast
        rests on and forces a fresh acquisition.

        Only the recorder can tell those apart, so it says so here:

        * ``publishing`` true + chrony dark  ⇒ WEDGE, restart is right
        * ``publishing`` false               ⇒ we know we are not
          feeding, and why; restarting fixes nothing and costs the anchor

        Never raises: watchers call this on a status path.
        """
        mode = getattr(self, '_t6_publish_mode_last', None)
        sigma = getattr(self, '_t6_holdover_sigma_ns', None)
        return {
            "mode": mode,
            "publishing": mode is not None,
            "sigma_ns": None if sigma is None else float(sigma),
        }

    def _t6_push_holdover(self) -> None:
        """Feed chrony from the FROZEN anchor, with no edge at all.

        A coast has no validated edge to name a second from, and must
        not borrow an unvalidated one — accepting edges detected during
        the outage is what made hf-timestd#14 a 203 ms falseticker.
        The frozen anchor plus the arrival floor are sufficient: the
        floor maps monotonic into the anchor's label space, so the most
        recent second boundary can be named directly and located on the
        host clock by the same inversion the live path uses.

        Once per named second, so chrony sees a normal 1 Hz refclock
        rather than a stalled one.  Never raises — the caller is the
        sample hot path.
        """
        from hf_timestd.core.t6_holdover import holdover_named_second
        from hf_timestd.core.t6_shm_pair import (
            precision_from_sigma_ns, t6_shm_system_time,
        )
        try:
            floor, mono_now, wall_now = self._t6_floor_snapshot()
            anchor = getattr(self, '_t6_native_anchor', None)
            if floor is None or anchor is None:
                return
            named = holdover_named_second(
                floor.offset_s, mono_now, anchor.chain_delay_ns
            )
            if named == getattr(self, '_t6_holdover_last_second', None):
                return
            pair = t6_shm_system_time(
                edge_label_utc_s=float(named),
                floor=floor,
                mono_now=mono_now,
                wall_now=wall_now,
                fallback_system_time=wall_now,
            )
            # The coast's own sigma, not the floor's: the floor cannot
            # see how long we have been extrapolating.
            sigma_ns = getattr(self, '_t6_holdover_sigma_ns', None)
            _s = pair.sigma_ns if sigma_ns is None else sigma_ns
            _floor = self._t6_dissent_sigma_floor_ns()
            if _floor is not None and _floor > _s:
                _s = _floor          # hf-timestd#29
            precision = precision_from_sigma_ns(_s)
            self._t6_shm.update(
                reference_time=float(named),
                system_time=pair.system_time,
                precision=precision,
            )
            self._t6_holdover_last_second = named
            self._t6_shm_push_count = getattr(
                self, '_t6_shm_push_count', 0) + 1
        except Exception as e:  # noqa: BLE001 — hot path, never fatal
            if not getattr(self, '_t6_holdover_warned', False):
                self._t6_holdover_warned = True
                logger.warning(f"T6 holdover push failed: {e}")

    def _t6_hpps_publishable(self) -> bool:
        """Whether the HPPS chrony-SHM refclock may be fed right now.

        A push is a claim: "when the host clock read ``system_time``,
        true UTC was ``reference_time``".  Two states have no grounds
        for that claim:

        * the Costas loop is in a phase excursion — the calibrator is
          coasting on the last-good chain delay and accepting no edges
          (see ``bpsk_pps_calibrator_mf``: "edge acceptance gated");
        * the anchor authority is DEGRADED or UNLOCKED — the anchor is
          stale, or was withdrawn and re-derived by the coarse cascade
          from an MF edge detected *during* the outage.

        Publishing anyway is strictly worse than going quiet.  chrony
        copes with a refclock that DISAPPEARS far better than with one
        that lies: a source that stops updating drops out of the
        selection algorithm and the cascade falls to T5/T4, whereas a
        wandering one burns reach, distorts the falseticker logic, and
        can be *selected* during a quiet interval — at which point a
        wrong offset disciplines the host clock.

        Measured on AC0G-B4 2026-08-16 under thunderstorm sferics:
        pushes continued through DEGRADED and UNLOCKED windows with
        per-push ``offset_to_chrony`` scattered over -37..+54 ms;
        chrony sourcestats reported Std Dev 177 ms and marked HPPS a
        falseticker.  See HamSCI/hf-timestd#14.

        Transitions are logged once each way — never per push.
        """
        floor, mono_now, _wall = self._t6_floor_snapshot()
        mode, sigma_ns, reason = self._t6_publish_mode(floor, mono_now)
        # The push path reads these: which construction to use, and what
        # the coast is currently worth.
        prev_mode = getattr(self, '_t6_publish_mode_last', "live")
        self._t6_publish_mode_last = mode
        self._t6_holdover_sigma_ns = sigma_ns
        # Entering or leaving a coast is a state change, not a detail:
        # the station stops publishing a MEASURED time and starts
        # publishing an EXTRAPOLATED one.  Say so, once per transition.
        if mode != prev_mode:
            if mode == "holdover":
                logger.warning(
                    "T6 HPPS: COASTING on the frozen anchor — %s. The "
                    "RTP counter is GPSDO-disciplined, so the anchor "
                    "still labels correctly; sigma %.3f ms and growing "
                    "at the measured residual rate. This is holdover, "
                    "not a measurement — no new edges are accepted.",
                    reason, (sigma_ns or 0.0) / 1e6,
                )
            elif prev_mode == "holdover" and mode == "live":
                logger.warning(
                    "T6 HPPS: coast ended — carrier locked and anchor "
                    "authority AUTHORITATIVE; measured edges again"
                )
        ok = mode is not None
        # Default True, not None: publishing is the healthy steady state,
        # so a clean first call has nothing to "resume" and must stay
        # silent.  A first call that is already faulted still says so.
        if ok is not getattr(self, '_t6_hpps_publishing', True):
            self._t6_hpps_publishing = ok
            if ok:
                logger.warning(
                    "T6 HPPS: RESUMED — carrier locked and anchor "
                    "authority AUTHORITATIVE; SHM pushes restart"
                )
            else:
                logger.warning(
                    "T6 HPPS: WITHDRAWN from chrony — %s. The T5/T4 "
                    "cascade carries the station until this clears; a "
                    "coasted chain delay is not a timing measurement "
                    "(hf-timestd#14).",
                    reason,
                )
        return ok

    def _t6_authority_status(self) -> Optional[dict]:
        """T6 authority block for the status JSON (spec §6 invariant 5:
        cross-tier disagreement is REPORTED here, never corrects the
        anchor)."""
        auth = getattr(self, '_t6_authority', None)
        if auth is None:
            return None
        decision = getattr(self, '_t6_authority_last_decision', None)
        anchor = getattr(self, '_t6_native_anchor', None)
        offset_ns = None
        try:
            offset_ns = self._compute_rtp_to_utc_offset_ns()
        except Exception:
            pass
        fine = getattr(self, '_t6_fine_stage', None)
        return {
            'state': auth.state.value,
            'violations': (list(decision.violations)
                           if decision is not None else []),
            'delay_budget_ns': auth.delay_budget_ns,
            'filter_group_delay_ns': auth.filter_group_delay_ns,
            # The configured budget being applied — an assertion, not a
            # measurement, and never the contract's analogue path delay.
            # `asserted_chain_delay_ns` rides along for one release so any
            # reader outside this repo can move.  See
            # docs/design/TIMING_PROVENANCE_MODEL.md §4.5.
            'applied_delay_budget_ns': (
                auth.delay_budget_ns + auth.filter_group_delay_ns),
            'asserted_chain_delay_ns': (  # DEPRECATED — use applied_delay_budget_ns
                auth.delay_budget_ns + auth.filter_group_delay_ns),
            'anchor_tier': (anchor.captured_via_tier
                            if anchor is not None else None),
            'blocks_discarded': (fine.blocks_discarded
                                 if fine is not None else 0),
            't6_vs_radiod_pair_ms': (offset_ns / 1e6
                                     if offset_ns is not None else None),
            # Naming-time cross-tier diagnostic: radiod-pair wall
            # estimate minus the NMEA-derived edge UTC, latched by
            # _t6_report_naming_vs_radiod_pair.  |value| > 0.5 s means
            # the radiod pair would have named the WRONG integer second
            # (spec §6 invariant 5: reported, never corrective).  None
            # until the NMEA naming path has run at least once.
            'naming_vs_radiod_pair_s': getattr(
                self, '_t6_naming_vs_radiod_pair_s', None),
        }

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
            from ka9q.rtp_recorder import rtp_to_utc
            raw_wall_time_sec = rtp_to_utc(
                last_edge_rtp, self._t6_channel_info
            )
            if raw_wall_time_sec is None:
                return False
            # Pair the matched-filter edge with the right integer GPS
            # second.  ``reading.pps_utc_sec`` is the integer GPS
            # second of the *latest* RMC observed by gpsdo-monitor —
            # which is NMEA's attestation that GPSDO lock is active
            # right now.  ``raw_wall_time_sec`` is the host-clock view
            # of when the MF detected its edge sample — which can be
            # several seconds in the past when the calibrator has
            # rejected edges through a step-recovery burst, leaving
            # ``_last_edge_rtp`` stale.
            #
            # The naive ``raw_wall − pps_utc_sec`` delta is therefore
            # NOT chain_delay — it carries an integer-second offset
            # equal to "how many full seconds the MF edge precedes the
            # latest NMEA observation."  Round that integer offset out
            # before extracting the sub-second residual.  The integer
            # part is recovered structurally; only the sub-second
            # residual = the physical chain_delay.
            #
            # Architecture compliance: the integer-second AUTHORITY
            # remains NMEA's ``pps_utc_sec`` (the GPS-truth attestation).
            # Wall-clock arithmetic on ``delta_sec`` is used only to
            # INDEX which past integer second the MF edge belongs to —
            # a sub-second indexing operation that the host clock,
            # chrony-disciplined to within ms of GPS UTC, can do
            # unambiguously.  ``ARCHITECTURE-FIRST-PRINCIPLES.md`` §1
            # forbids the host clock as a *timing reference for the
            # science product*; using it as a sub-second indexing
            # signal at a one-shot anchor-capture moment is in scope.
            delta_sec = raw_wall_time_sec - reading.pps_utc_sec
            integer_offset = int(round(delta_sec))
            effective_pps_utc_sec = (
                int(reading.pps_utc_sec) + integer_offset
            )
            residual_sec = raw_wall_time_sec - effective_pps_utc_sec
            if abs(residual_sec) > 0.5:
                # Structurally unreachable when the rounding above is
                # used correctly — kept as a defence against an
                # off-by-one in the rounding or a host-clock vs GPS
                # disagreement larger than 0.5 s (which would imply
                # chrony has lost the network NTP source AND GPSDO
                # discipline simultaneously).
                logger.warning(
                    f"T6 T5 disambig: post-alignment residual "
                    f"{residual_sec:+.3f} s exceeds ±0.5 s "
                    f"(raw_wall={raw_wall_time_sec:.3f}, "
                    f"reading.pps_utc_sec={reading.pps_utc_sec}, "
                    f"integer_offset={integer_offset:+d}); "
                    f"falling back to T4."
                )
                return False
            # Physical chain_delay = sub-second residual after the
            # integer-second alignment above.
            effective_chain_delay_ns, reported_residual_ns = (
                self._t6_resolve_chain_delay_ns(
                    residual_sec, self._t6_chain_delay_calib_s))
            self._t6_report_derived_residual(
                "HPPS", reported_residual_ns, effective_chain_delay_ns)
            # Layer B physical-plausibility guard.  The RF chain
            # delay from TS-1 BPSK modulator → coax → RX-888 ADC →
            # radiod DSP is bounded by hardware geometry: typical
            # in-shack RF paths land in 10-100 ms, with the upstream
            # radiod filter group-delay being the dominant
            # contribution (up to ~150 ms at narrow filter widths).
            # A *captured* chain_delay much larger than that is a
            # sidelobe / phantom peak, not a physical signal.  Layer
            # B analysis on bee1 2026-05-31 12:08 UTC showed the MF
            # producing 8 distinct chain_delay clusters spread across
            # 0-980 ms during signal-degraded periods (peak_running
            # ~67 vs healthy 94); capturing into any of the 4 sidelobe
            # clusters at 200/466/808/955 ms freezes the anchor at a
            # wrong value that no amount of MF jitter can shake out.
            # Refuse the capture and fall through to T4; the next
            # first-lock will retry.  See
            # docs/TSL3_COSTAS_DRIFT_2026-05-18.md §"Layer B".
            T6_PHYSICAL_CHAIN_DELAY_MAX_NS = 250_000_000  # 250 ms
            if abs(reported_residual_ns) > T6_PHYSICAL_CHAIN_DELAY_MAX_NS:
                logger.warning(
                    f"T6 T5 disambig: implied chain_delay "
                    f"{reported_residual_ns/1e6:+.1f} ms exceeds "
                    f"physical-plausibility bound ±"
                    f"{T6_PHYSICAL_CHAIN_DELAY_MAX_NS/1e6:.0f} ms — "
                    f"sidelobe / phantom-peak capture.  Falling back "
                    f"to T4."
                )
                return False
            # Layer B is necessary but not sufficient: ±250 ms is wider than
            # the 200 ms sidelobe cluster it cites, so the tight gate is the
            # learned per-station reference.  Opt-in; a no-op when off or
            # while still learning.
            #
            # FAILS OPEN, deliberately.  This is an opt-in *tightening* of a
            # path that already works, so if it cannot run — a partial object,
            # a future refactor, a bug in the tracker — the correct outcome is
            # the pre-existing behaviour, never a silent demotion to T4.  The
            # enclosing try/except would otherwise swallow any error here and
            # fall back, which is exactly the "guard breaks the thing it
            # guards" failure.  Caught by
            # test_core_recorder_t6_origin_assertion's deliberately minimal
            # stub, which carries no reference-gate attribute at all.
            _ref_gate = getattr(self, '_t6_reference_gate_rejects', None)
            if _ref_gate is not None:
                try:
                    if _ref_gate(reported_residual_ns):
                        return False
                except Exception as exc:      # pragma: no cover - defensive
                    logger.warning(
                        "T6 reference gate errored (%s); ignoring it and "
                        "keeping the Layer B verdict", exc)
            # Back-derive disambig shift: effective = raw + disambig.
            self._t6_disambiguation_ns = (
                effective_chain_delay_ns - result.chain_delay_ns
            )
            # Capture the hf-timestd-native (RTP, UTC) anchor for the
            # T6 channel.  This is the moment everything in the
            # native-anchor architecture pivots around — the only
            # time the science path consults rtp_to_wallclock, and
            # only to pair the matched-filter edge RTP with the
            # NMEA-attested integer UTC second.  After this point the
            # anchor lives in self._t6_native_anchor and all RTP→UTC
            # labelling on the T6 path uses pure arithmetic against
            # it.  See hf_timestd.core.native_anchor.
            from .native_anchor import NativeAnchor
            sr = int(self._t6_calibrator.sample_rate)
            self._t6_native_anchor = NativeAnchor(
                anchor_rtp=int(last_edge_rtp) & 0xFFFFFFFF,
                anchor_utc_ns=(
                    int(effective_pps_utc_sec) * 1_000_000_000
                    + effective_chain_delay_ns
                ),
                sample_rate_hz=sr,
                chain_delay_ns=int(effective_chain_delay_ns),
                captured_at_utc_ns=(
                    int(effective_pps_utc_sec) * 1_000_000_000
                ),
                captured_via_tier="T5",
            )
            self._t6_rate_reset("native anchor captured via T5")
            # getattr'd: test harnesses borrow this method onto bare
            # fakes that have neither the helper nor a ledger.
            _led = getattr(self, '_t6_ledger_append', None)
            if _led is not None:
                _led(self._t6_native_anchor)
            logger.info(
                f"T6 chain_delay disambiguated against T5 (LB-1421 NMEA): "
                f"raw={result.chain_delay_ns} ns, "
                f"raw_wall_time={raw_wall_time_sec:.6f}, "
                f"NMEA_PPS_UTC={reading.pps_utc_sec} "
                f"(integer-aligned to {effective_pps_utc_sec}, "
                f"offset={integer_offset:+d} s), "
                f"residual={residual_sec*1000:+.3f} ms, "
                f"effective_chain_delay={effective_chain_delay_ns} ns "
                f"(no integer-sample-shift step — direct GPS reference); "
                f"{format_native_anchor_log(self._t6_native_anchor, 'T5')}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"T6 T5 disambig: unexpected error ({e}); "
                f"falling back to T4"
            )
            return False

    def _t6_disambiguate_via_external_reference(self, result) -> None:
        """Fallback disambiguation path used when no fresh persisted
        chain_delay is available.  Walks the timing-tier hierarchy
        (T5 > T4 > T3) and sets ``self._t6_disambiguation_ns`` to the
        integer-sample shift that brings the calibrator's implied
        wall-time into agreement with the highest-rank available tier.

        With chain-delay persistence retired (anchor inversion spec §6)
        this runs at every initial accept.
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
            from ka9q.rtp_recorder import rtp_to_utc
            raw_wall_time_sec = rtp_to_utc(last_edge_rtp, self._t6_channel_info)
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
            # Capture the hf-timestd-native anchor against the
            # cascade-resolved integer GPS second.  The effective
            # chain delay just computed (= result.chain_delay_ns +
            # disambig_ns) lets us back-derive the PPS firing UTC at
            # this MF edge: ref_time after the integer-sample shift.
            # captured_via_tier reflects which tier of the cascade
            # supplied the integer second.
            from .native_anchor import NativeAnchor
            effective_chain_delay_ns = wrap_chain_delay_ns(
                result.chain_delay_ns + self._t6_disambiguation_ns
            )
            # Layer B physical-plausibility guard — same rationale as
            # the T5 path; see ``_t6_disambiguate_via_t5_lb1421``.
            T6_PHYSICAL_CHAIN_DELAY_MAX_NS = 250_000_000
            if abs(effective_chain_delay_ns) > T6_PHYSICAL_CHAIN_DELAY_MAX_NS:
                logger.warning(
                    f"T6 {ref_tier} disambig: implied chain_delay "
                    f"{effective_chain_delay_ns/1e6:+.1f} ms exceeds "
                    f"physical-plausibility bound ±"
                    f"{T6_PHYSICAL_CHAIN_DELAY_MAX_NS/1e6:.0f} ms — "
                    f"sidelobe / phantom-peak capture.  Not capturing "
                    f"anchor; calibrator will retry on next first-lock."
                )
                return
            pps_firing_utc_ns = (ref_time - (ref_offset_ms / 1000.0))
            pps_firing_utc_ns = int(round(pps_firing_utc_ns * 1e9))
            self._t6_native_anchor = NativeAnchor(
                anchor_rtp=int(last_edge_rtp) & 0xFFFFFFFF,
                anchor_utc_ns=pps_firing_utc_ns + effective_chain_delay_ns,
                sample_rate_hz=int(sr_local),
                chain_delay_ns=effective_chain_delay_ns,
                captured_at_utc_ns=pps_firing_utc_ns,
                captured_via_tier=str(ref_tier),
            )
            self._t6_rate_reset(f"native anchor captured via {ref_tier}")
            # getattr'd: test harnesses borrow this method onto bare
            # fakes that have neither the helper nor a ledger.
            _led = getattr(self, '_t6_ledger_append', None)
            if _led is not None:
                _led(self._t6_native_anchor)
            if shift_samples != 0:
                logger.info(
                    f"T6 chain_delay disambiguated against {ref_tier} "
                    f"(offset={ref_offset_ms:+.3f} ms, "
                    f"sigma={ref_sigma_ms:.3f} ms): raw="
                    f"{result.chain_delay_ns} ns implied wall-time "
                    f"offset {offset_sec*1000:+.3f} ms; disagreement "
                    f"{disagreement_sec*1000:+.1f} ms; shifting "
                    f"{shift_samples} samples ({self._t6_disambiguation_ns} ns); "
                    f"{format_native_anchor_log(self._t6_native_anchor, ref_tier)}"
                )
            else:
                logger.info(
                    f"T6 chain_delay disambiguated against {ref_tier}: "
                    f"already aligned within one sample "
                    f"(disagreement {disagreement_sec*1000:+.3f} ms); "
                    f"{format_native_anchor_log(self._t6_native_anchor, ref_tier)}"
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

    @staticmethod
    def _t6_fine_settings(t6_cfg: dict) -> dict:
        """Parse + validate the fine-stage/authority keys of
        [timing.t6_pps] (spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md
        §7).  Raises ValueError on a delay budget outside the ±1 ms
        physical bound — a larger value is absorbing timestamp error,
        not measuring a chain delay, and must refuse loudly."""
        from hf_timestd.core.t6_anchor_authority import (
            DELAY_BUDGET_BOUND_NS, FILTER_GROUP_DELAY_BOUND_NS,
        )
        # What a T6 label MEANS (docs/design/CONTENT_TIME_LABELING_CONVENTION.md):
        #   content — the antenna instant.  Only the µs-class analog path
        #     (delay_budget_ns) sits between the antenna and the sample;
        #     everything after the ADC (USB, the 3.24 M-point FFT, filtering,
        #     scheduling) is pipeline LATENCY, not part of the label, so
        #     filter_group_delay_ns is not applied.
        #   legacy — the pre-2026-08-24 arithmetic, which folded a 16.618 ms
        #     constant (calibrated once against T4) into every anchor.
        # Kept as a key so a site can revert in one line, independently of
        # the release that ships the change.
        convention = str(
            t6_cfg.get('labeling_convention', 'content')).strip().lower()
        if convention not in ('content', 'legacy'):
            raise ValueError(
                f"[timing.t6_pps].labeling_convention={convention!r} is not "
                f"one of 'content' | 'legacy' (see "
                f"docs/design/CONTENT_TIME_LABELING_CONVENTION.md)"
            )
        configured_group_delay = int(t6_cfg.get('filter_group_delay_ns', 0))
        s = {
            'labeling_convention': convention,
            'fine_stage_enabled': bool(t6_cfg.get('fine_stage_enabled', True)),
            'fine_fold_seconds': int(t6_cfg.get('fine_fold_seconds', 30)),
            # Default: the TS-1 modulator alone, sourced from the designer
            # (P. Elliott WB6CXC, 2026-08-30: under 200 ns in standard
            # injector mode).  The previous 10_000 default originated as a
            # comment in our own config template, was cited back to us as
            # vendor documentation, and over-corrected every anchor by
            # ~9.8 us.  A station that measures its antenna-to-injector run
            # SHOULD set this explicitly: that term enters the sum with the
            # OPPOSITE sign (the injected reference never traverses it), so
            # the site value is d_modulator - d_antenna_to_injector.  See
            # docs/design/TIMING_PROVENANCE_MODEL.md §4.5.
            'delay_budget_ns': int(t6_cfg.get('delay_budget_ns', 200)),
            # Applied only under the legacy convention; the configured value
            # is kept beside it so the operator can see what was retired.
            'filter_group_delay_ns': (
                configured_group_delay if convention == 'legacy' else 0),
            'filter_group_delay_ns_configured': configured_group_delay,
            'edge_period_tolerance_ns': int(
                t6_cfg.get('edge_period_tolerance_ns', 5_000)),
            'fine_coarse_max_ms': float(t6_cfg.get('fine_coarse_max_ms', 5.0)),
            'degraded_unlock_after_sec': float(
                t6_cfg.get('degraded_unlock_after_sec', 600.0)),
        }
        if abs(s['delay_budget_ns']) > DELAY_BUDGET_BOUND_NS:
            raise ValueError(
                f"[timing.t6_pps].delay_budget_ns={s['delay_budget_ns']} "
                f"exceeds the ±1 ms physical bound (analog path + channel-"
                f"filter group delay is µs to sub-ms; see "
                f"docs/design/T6_ANCHOR_INVERSION_DESIGN.md §5)"
            )
        if abs(s['filter_group_delay_ns_configured']) > FILTER_GROUP_DELAY_BOUND_NS:
            raise ValueError(
                f"[timing.t6_pps].filter_group_delay_ns="
                f"{s['filter_group_delay_ns_configured']} exceeds the ±250 ms "
                f"physical bound (radiod channel-filter group delay "
                f"reaches ~150 ms only at the narrowest widths)"
            )
        return s

    def _register_t6_with_status_listener(self, channel_info) -> None:
        """Wire the T6 channel into ka9q-python's continuous STATUS
        listener so its ``gps_time`` / ``rtp_timesnap`` anchor is
        refreshed in place on every radiod STATUS broadcast.

        Called from ``_start_t6_stream`` (initial wiring) and from
        ``_t6_attempt_recapture`` after the wholesale ChannelInfo swap
        (re-registers the new object so the listener mutates the
        currently-used reference).

        Silent no-op when ka9q-python is older than 3.16 (the listener
        feature is absent) or when ``self.control`` lacks the helper.
        The Layer-3 recapture path is still the source of truth for
        radiod-restart discontinuity recovery; this helper just
        eliminates the slow-drift component between recaptures.
        """
        if channel_info is None or not getattr(channel_info, 'ssrc', 0):
            return
        control = getattr(self, 'control', None)
        if control is None:
            # Bare/mocked recorder (e.g. unit tests that exercise the
            # Layer-3 swap math without a real RadiodControl).
            return
        start_fn = getattr(control, 'start_status_listener', None)
        if start_fn is None:
            # Old ka9q-python (<3.16) — nothing to wire.
            return
        try:
            listener = start_fn()
            listener.register_channel(channel_info)
            logger.info(
                f"T6 channel SSRC {channel_info.ssrc:08x} wired to "
                f"continuous STATUS listener — anchor refresh per "
                f"radiod STATUS broadcast (sub-second cadence)"
            )
        except Exception as e:
            # Listener failures must not break recorder startup —
            # without it the recorder falls back to the legacy 30 s
            # Layer-3 recapture cadence (slow but functional).
            logger.warning(
                f"Failed to wire T6 channel into STATUS listener "
                f"(falling back to Layer-3 cadence): {e}"
            )

    def _t6_on_samples(self, samples, quality):
        """Sample callback for the BPSK PPS stream — feeds the calibrator."""
        # T5 pairing arrival point (P2, audit G5b): record the RTP
        # counter observed "now" so the LB-142x integer second can be
        # paired against the substrate.  Single tuple assignment — safe
        # on the hot path.
        self._t6_note_arrival(quality)
        # Cross-channel counter calibration for the ledger (#42/#43);
        # self-throttled to ~1 Hz.
        self._t6_observe_channel_epochs()
        _cap = getattr(self, '_t6_anomaly', None)
        if _cap is not None:
            try:
                _cap.add(samples)
            except Exception:  # noqa: BLE001
                pass
        # Count radiod block drops on THIS channel.  gap_hourly reads
        # raw-buffer sidecars, T6 is not archived, so without this its
        # losses are invisible to the fleet's only loss metric — hour 07
        # on 2026-08-26 read zero gaps while a T6 capture from that hour
        # held two dropped blocks.
        _zf = getattr(self, '_t6_zero_fill', None)
        if _zf is not None:
            closed = _zf.observe(samples)
            if closed:
                snap = _zf.snapshot()
                # Sample the drop itself, not only drops that went on to
                # break something: capturing solely on T6 anomalies gives
                # P(drop | anomaly) and never P(anomaly | drop).
                self._t6_capture_anomaly("zero-fill")
                now = time.monotonic()
                if now - getattr(self, '_t6_zero_fill_logged', 0.0) > 60.0:
                    self._t6_zero_fill_logged = now
                    logger.warning(
                        "T6 BLOCK DROP: %d run(s) closed this batch; run "
                        "total %d runs / %d blocks / %d zero samples, "
                        "longest %d samples (%.0f ms). radiod zero-fills a "
                        "dropped block, so the counter stays continuous and "
                        "byte counts read 100%% — these zeros are the only "
                        "honest evidence.",
                        closed, snap['runs'], snap['blocks'],
                        snap['zero_samples'], snap['longest_run_samples'],
                        snap['longest_run_samples'] / 96.0,
                    )
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
            samples, resolve_batch_rtp(quality)
        )

        # T6 anchor inversion (spec §2, §4): feed the fine stage with
        # every batch, seed its coarse offset from the calibrator's
        # locked chain_delay, and drive the authority state machine
        # off any resulting sub-sample edge estimate.  Failures here
        # MUST NOT affect the main calibrator/disambiguation path —
        # this is a parallel, higher-precision observable layered on
        # top of the proven MF cascade, not a replacement for it (yet).
        fine_stage = getattr(self, '_t6_fine_stage', None)
        if fine_stage is not None:
            try:
                # ``_chain_delay_samples`` is ``edge_rtp_full %
                # sample_rate`` — the RTP domain.  Both the fine stage
                # (which translates into its own fold domain per block)
                # and the authority (which converts estimates back via
                # edge_rtp) speak that domain, so this value is handed
                # over untranslated.  See the domain note in
                # ``bpsk_edge_fine_stage``: they are NOT interchangeable
                # and mixing them mis-places the ±6 ms search window by
                # an arbitrary per-start amount.
                coarse = self._t6_calibrator._chain_delay_samples
                if result is not None and result.locked and coarse is not None:
                    fine_stage.set_coarse_offset_samples(coarse)
                else:
                    # The MF is not standing behind a position right now.
                    # Drop its window rather than search a stale one, and
                    # let the fold acquire on its own — the MF is a
                    # witness for fine_coarse, no longer a veto.
                    fine_stage.clear_coarse_offset()
                    coarse = None
                fine = fine_stage.process_samples(
                    samples, resolve_batch_rtp(quality))
                if fine is not None:
                    self._t6_last_fine_est = fine
                if fine is not None and self._t6_authority is not None:
                    named = self._t6_name_integer_second(fine.edge_rtp)
                    decision = self._t6_authority.on_fine_estimate(
                        fine, coarse, named)
                    # Spec §3.3: demotion follows N consecutive blocks
                    # that fail their fit OR are rejected for
                    # edge_period.  The stage sees only the first half;
                    # the verdict lives here, so report it.  A position
                    # that fits cleanly every block while violating
                    # edge_period would otherwise be tracked all the way
                    # through AUTHORITATIVE → DEGRADED → UNLOCKED and
                    # re-installed on the next acquisition.  The stage
                    # filters on edge_period itself — fine_coarse is the
                    # demoted MF witness disagreeing, deliberately
                    # non-fatal now.
                    fine_stage.note_authority_violations(decision.violations)
                    self._t6_apply_authority_decision(decision)
            except Exception as e:
                if not getattr(self, '_t6_fine_warned', False):
                    logger.error(
                        f"T6 fine stage failed (will retry each batch, "
                        f"logged once): {e}", exc_info=True)
                    self._t6_fine_warned = True

            # Liveness (spec §6).  Everything above is edge-triggered on
            # a fine estimate.  If estimates stop arriving while the MF
            # stays locked — discarded fold blocks, a mis-seeded window
            # finding no crossing, the handler above swallowing every
            # batch — no branch above ever runs again and the authority
            # would hold AUTHORITATIVE with a frozen anchor still
            # feeding chrony (detect-and-stall, forbidden by the spec).
            # DELIBERATELY OUTSIDE the try above: the swallowed-exception
            # case is precisely the one this must survive.
            authority = getattr(self, '_t6_authority', None)
            if authority is not None:
                try:
                    stale = authority.on_tick()
                    if stale is not None:
                        self._t6_apply_authority_decision(stale)
                except Exception as e:
                    if not getattr(self, '_t6_liveness_warned', False):
                        logger.error(
                            f"T6 authority liveness tick failed (logged "
                            f"once): {e}", exc_info=True)
                        self._t6_liveness_warned = True

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
            if getattr(self, '_t6_authority', None) is not None:
                self._t6_apply_authority_decision(
                    self._t6_authority.on_mf_unlock())
            if getattr(self, '_t6_fine_stage', None) is not None:
                self._t6_fine_stage.reset()
                # Repudiate the tracked position too — reset() spares it.
                self._t6_fine_stage.clear_own_offset()
            self._t6_last_chain_delay_ns = None
            self._t6_disambiguation_ns = 0
            self._t6_wrap_rejections = 0
            self._t6_recent_raw.clear()
            self._t6_last_locked_wall = wall_now

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
                # Throttle: while the Layer B guard below is refusing
                # implausible values, this branch re-runs on every locked
                # sample batch (~25 Hz at 96 k / 3720-sample batches) and
                # each walk shells out to chronyc and emits 3 log lines.
                # One walk per T6_DISAMBIG_RETRY_INTERVAL_SEC is plenty.
                # The very first walk (rejections == 0) is never delayed.
                if (self._t6_initial_accept_rejections > 0
                        and self._t6_last_disambig_walk_wall is not None
                        and (time.monotonic()
                             - self._t6_last_disambig_walk_wall)
                            < self.T6_DISAMBIG_RETRY_INTERVAL_SEC):
                    return
                self._t6_last_disambig_walk_wall = time.monotonic()
                # First stable lock — disambiguate WHICH whole sample is the
                # real PPS edge.
                #
                # Chain-delay persistence is retired on the T6 path
                # (spec §6): under the anchor inversion no ms-scale
                # fitted state exists to persist across restarts — the
                # fine stage + authority re-derive the anchor from
                # scratch in ~fine_fold_seconds after every re-lock, so
                # this branch goes straight to the timing-tier cascade
                # instead of consulting a persisted value.  A store
                # file left over from a pre-inversion build is ignored
                # (logged once at INFO, not silently).
                sr_local = self._t6_calibrator.sample_rate
                if not getattr(self, '_t6_mf_store_leftover_logged', False):
                    # The path the retired bpsk_chain_delay_store wrote.
                    _leftover_path = Path(
                        "/var/lib/timestd/bpsk_mf_chain_delay.json")
                    if _leftover_path.exists():
                        logger.info(
                            f"T6: leftover chain-delay store "
                            f"{_leftover_path} found on disk but ignored "
                            f"— persistence retired on the T6 path "
                            f"(spec §6); re-lock re-derives from scratch."
                        )
                    self._t6_mf_store_leftover_logged = True
                # Disambiguate against the timing-tier hierarchy: T5
                # (LB-1421 NMEA over USB) — direct GPS reference, no
                # chrony detour.  Falls through to T4 if T5 isn't wired
                # or the reading is unavailable.  Sigma sanity check
                # (in the tier helpers): reject any reference whose
                # sigma is larger than the half-second-wrap value we're
                # trying to disambiguate against (250 ms).
                _dpath = "t5-lb1421"
                if not self._t6_disambiguate_via_t5_lb1421(result):
                    self._t6_disambiguate_via_external_reference(result)
                    _dpath = "external-ref"
                self._t6_disambig_path = _dpath
                self._t6_disambig_detail = ""
                # Apply disambiguation (set above either way) and lock in.
                effective = wrap_chain_delay_ns(
                    result.chain_delay_ns + self._t6_disambiguation_ns)
                # ---- #7 defect 2 instrumentation ----
                # Fires only at initial accept, so it is not a hot path.
                # The question this answers: raw is measured to ~80 ns, yet
                # pps_firing_utc lands 80 ms off an integer second on every
                # edge.  A GPS PPS fires ON the second, so `effective` is
                # wrong by that residual -- this shows which path chose it
                # and from what input.  Note neither path constrains the
                # shift to a whole wrap period; both can land anywhere.
                try:
                    _sr_i = int(self._t6_calibrator.sample_rate)
                    logger.warning(
                        "T6 DISAMBIG: path=%s raw=%d ns (%.6f ms) "
                        "disambig=%d ns (%.6f ms) effective=%d ns "
                        "(%.6f ms) | shift/wrap=%.4f shift/sample=%.2f | %s",
                        getattr(self, '_t6_disambig_path', 'fallthrough'),
                        result.chain_delay_ns,
                        result.chain_delay_ns / 1e6,
                        self._t6_disambiguation_ns,
                        self._t6_disambiguation_ns / 1e6,
                        effective,
                        effective / 1e6,
                        # A legitimate wrap resolution is a whole number of
                        # template periods (half-second for the MF).  A
                        # non-integer here means the shift is not a wrap
                        # correction at all.
                        self._t6_disambiguation_ns / 500_000_000.0,
                        self._t6_disambiguation_ns * _sr_i / 1e9,
                        getattr(self, '_t6_disambig_detail', ''),
                    )
                except Exception:
                    pass
                # ---- end instrumentation ----
                # Layer B physical-plausibility guard at initial accept —
                # the last unguarded entry point into the lock.  When no
                # usable reference tier is available (T5 unwired, T4
                # sigma over gate, T3 absent) the disambiguation walk
                # falls through with shift 0, and the raw MF value —
                # ambiguous modulo the template period — used to be
                # accepted AND persisted verbatim.  Observed on B4
                # 2026-07-24 (boot #1 and again after a clean restart):
                # boot-race chrony left T4 at sigma 1.8-4.4 ms, and the
                # MF's deterministic sidelobe at +708.5 ms was locked in
                # and persisted twice in a row, keeping T6/HPPS out of
                # the authority cascade with no operator-visible retry.
                # Refuse instead: leave _t6_last_chain_delay_ns unset so
                # every subsequent locked cycle re-enters initial-accept
                # and re-walks the tier hierarchy — references improve
                # as chrony settles — and nothing implausible is locked,
                # persisted, pushed to SHM, or written to archive
                # metadata meanwhile.  Same 250 ms bound and rationale
                # as the guards in _t6_disambiguate_via_t5_lb1421 /
                # _t6_disambiguate_via_external_reference; see
                # docs/TSL3_COSTAS_DRIFT_2026-05-18.md §"Layer B".
                T6_PHYSICAL_CHAIN_DELAY_MAX_NS = 250_000_000  # 250 ms
                if abs(effective) > T6_PHYSICAL_CHAIN_DELAY_MAX_NS:
                    self._t6_initial_accept_rejections += 1
                    n = self._t6_initial_accept_rejections
                    if n == 1 or n % 60 == 0:
                        logger.warning(
                            f"T6 chain_delay initial accept REFUSED: "
                            f"effective {effective/1e6:+.1f} ms exceeds "
                            f"physical-plausibility bound ±"
                            f"{T6_PHYSICAL_CHAIN_DELAY_MAX_NS/1e6:.0f} ms "
                            f"(raw={result.chain_delay_ns} ns, "
                            f"disambig={self._t6_disambiguation_ns} ns, "
                            f"consecutive refusals={n}).  Sidelobe capture "
                            f"with no usable disambiguation reference — "
                            f"not locking, not persisting; will retry "
                            f"disambiguation on the next locked cycle."
                        )
                    self._t6_disambiguation_ns = 0
                    return
                self._t6_initial_accept_rejections = 0
                self._t6_last_chain_delay_ns = effective
                effective_chain_delay = effective
                logger.info(
                    f"T6 chain_delay initial accept: {result.chain_delay_ns} ns "
                    f"(effective with disambiguation: {effective} ns)"
                )
            elif abs(wrap_chain_delay_ns(
                    (result.chain_delay_ns + self._t6_disambiguation_ns)
                    - self._t6_last_chain_delay_ns)) > WRAP_THRESHOLD_NS:
                # Suspicious jump — log once per burst and use the
                # last-accepted chain_delay for downstream propagation.
                # Falling through (rather than `return`-ing early) keeps
                # TSL3's SHM updates flowing with the proven-good value
                # so chrony does not lose Reach during a wrap event.
                self._t6_wrap_rejections += 1
                self._t6_recent_raw.append(result.chain_delay_ns)
                # A rejected raw means the lock is already in question.
                # Ask whether GPS still corroborates the LOCK itself --
                # step-recovery below only ever judges the candidate.
                self._t6_check_stale_lock()
                if self._t6_last_chain_delay_ns is None:
                    return
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
                    # Diagnostic only (AC0G-B4 2026-08-25) — no branch
                    # change.  The REJECTED line below states t5_implied
                    # and the old lock but never the candidate the
                    # cluster implies, so the journal cannot separate the
                    # two ways this branch is reached: a boxcar sidelobe
                    # (candidate ~±0.5 s from GPS — the 2026-05-23
                    # phantom, holding is CORRECT) from a stale lock
                    # (candidate AGREES with GPS — holding is wrong, and
                    # nothing in this state machine re-validates the lock
                    # itself).  B4 sat here at ~1 Hz for hours with raw
                    # pinned at 546,963,700 ns against a 225,754,278 ns
                    # lock and no way to tell which.
                    candidate_effective = wrap_chain_delay_ns(
                        median_raw + self._t6_disambiguation_ns
                    )
                    logger.warning(
                        "T6 step-recovery candidate: raw_median=%d ns, "
                        "disamb=%d ns, candidate_effective=%d ns, "
                        "t5_implied=%s ns, locked=%s ns, "
                        "candidate_minus_t5=%s ns, spread=%d ns "
                        "(candidate agreeing with T5 ⇒ the LOCK is the "
                        "stale one; candidate ~±0.5 s from T5 ⇒ sidelobe "
                        "phantom and holding is correct)",
                        median_raw,
                        self._t6_disambiguation_ns,
                        candidate_effective,
                        "None" if t5_implied is None else "%.0f" % t5_implied,
                        self._t6_last_chain_delay_ns,
                        "None" if t5_implied is None
                        else "%+d" % wrap_chain_delay_ns(
                            int(candidate_effective - t5_implied)),
                        spread_ns,
                    )
                    if (t5_implied is not None
                            and self._t6_last_chain_delay_ns is not None
                            and abs(wrap_chain_delay_ns(
                                t5_implied - self._t6_last_chain_delay_ns))
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
                            f"{wrap_chain_delay_ns(int(t5_implied - self._t6_last_chain_delay_ns)):+d} ns "
                            f"(threshold ±{self.T6_STEP_RECOVERY_T5_SANITY_NS} ns). "
                            f"Phantom edge from packet-loss zero-fill or "
                            f"matched-filter sidelobe.  Holding old lock; "
                            f"clearing recent_raw to give the calibrator "
                            f"a fresh window to relock on the true edge."
                        )
                        self._t6_recent_raw.clear()
                        self._t6_wrap_rejections = 0
                        effective_chain_delay = self._t6_last_chain_delay_ns
                        self._t6_capture_anomaly("step-recovery-refused")
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
                        # Propagate the unlock to the T6 authority exactly
                        # as stuck-recovery does (Finding 2): without
                        # this, the T5 cascade below re-derives the
                        # legacy anchor via a fresh disambiguation walk
                        # while the authority still holds AUTHORITATIVE
                        # off the old fine-stage estimate, then the fine
                        # stage silently re-installs its own anchor with
                        # no state transition logged — the anchor (and
                        # pps_firing_utc pushed to chrony) steps by the
                        # full coarse chain delay twice with nothing
                        # exposed.  Reset the fine stage too so it
                        # re-acquires cleanly against the new operating
                        # point instead of localising against a stale
                        # fold window.
                        if getattr(self, '_t6_authority', None) is not None:
                            self._t6_apply_authority_decision(
                                self._t6_authority.on_mf_unlock())
                        if getattr(self, '_t6_fine_stage', None) is not None:
                            self._t6_fine_stage.reset()
                            # Repudiate the tracked position too —
                            # reset() spares it, so without this the
                            # stage re-installs the operating point the
                            # step-recovery just admitted was stale.
                            self._t6_fine_stage.clear_own_offset()
                        self._t6_last_chain_delay_ns = None
                        self._t6_disambiguation_ns = 0
                        self._t6_wrap_rejections = 0
                        self._t6_recent_raw.clear()
                else:
                    if self._t6_wrap_rejections == 1 or self._t6_wrap_rejections % 60 == 0:
                        logger.warning(
                            f"T6 chain_delay jump rejected: "
                            f"new={result.chain_delay_ns} ns, "
                            f"last_accepted={self._t6_last_chain_delay_ns} ns, "
                            f"delta={wrap_chain_delay_ns(result.chain_delay_ns - self._t6_last_chain_delay_ns)} ns "
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
            # ``_t6_native_anchor is not None`` is NOT enough on its own:
            # the UNLOCKED handler nulls the anchor, but the coarse
            # cascade immediately re-captures one via T5 from the same
            # MF edge, so the guard reopens while the carrier is still
            # lost.  Ask the authority directly (hf-timestd#14).
            if (self._t6_shm is not None
                    and self._t6_native_anchor is not None
                    and self._t6_hpps_publishable()):
                try:
                    # A coast NEVER takes the edge-driven path below.
                    # During DEGRADED with the carrier still locked the
                    # calibrator keeps producing edges, and those are
                    # exactly the unvalidated edges that made
                    # hf-timestd#14 a falseticker.  Name the second from
                    # the frozen anchor instead, and hold edge_advanced
                    # False so the whole edge construction is skipped.
                    _mode = getattr(self, '_t6_publish_mode_last', 'live')
                    if _mode != 'live':
                        self._t6_push_holdover()
                    last_edge_rtp = getattr(self._t6_calibrator, '_last_edge_rtp', None)
                    edge_advanced = (
                        _mode == 'live'
                        and last_edge_rtp is not None
                        and last_edge_rtp != self._t6_last_pushed_rtp
                    )
                    if edge_advanced:
                        # Native-anchor RTP→UTC.  Pure arithmetic against
                        # the captured (RTP, UTC) pair — no consultation
                        # of the host clock, no rtp_to_wallclock chain.
                        # See hf_timestd.core.native_anchor.
                        from .native_anchor import utc_ns_at_rtp
                        edge_utc_ns = utc_ns_at_rtp(
                            int(last_edge_rtp) & 0xFFFFFFFF,
                            self._t6_native_anchor,
                        )
                        # PPS firing UTC = sample UTC − RF chain delay.
                        pps_firing_utc_ns = (
                            edge_utc_ns - self._t6_native_anchor.chain_delay_ns
                        )
                        ref_time_ns = (
                            int(round(pps_firing_utc_ns / 1e9))
                            * 1_000_000_000
                        )
                        # Sub-integer-second residual.  With a stable
                        # native anchor this is the MF's own per-edge
                        # measurement jitter (sub-µs in steady state)
                        # — the honest σ that
                        # docs/T6-ANNOTATION-VALUE-2026-05-24.md
                        # "Implications #2" asks the system to publish.
                        self._t6_last_local_minus_source_ns = int(
                            pps_firing_utc_ns - ref_time_ns
                        )
                        # Histories — chain_delay tracks the MF estimate
                        # for each edge; local_minus_source tracks the
                        # post-anchor residual.  Both feed the σ
                        # publication via BpskPpsProbe.
                        self._t6_chain_delay_history.append(
                            float(effective_chain_delay)
                        )
                        self._t6_local_minus_source_history.append(
                            self._t6_last_local_minus_source_ns
                        )
                        # P3: feed the residual-walk rate estimator —
                        # x-axis is the edge's integer true second
                        # (the GPS grid), y the sub-second residual.
                        # Pure recording; nothing feeds back.
                        rate_est = getattr(self, '_t6_rate_est', None)
                        if rate_est is not None:
                            rate_est.add_edge(
                                ref_time_ns / 1e9,
                                self._t6_last_local_minus_source_ns,
                            )
                        # Chrony SHM facade — push (reference_time,
                        # system_time) so chrony can discipline the
                        # host clock toward GPS truth.  The chrony
                        # convenience layer (see
                        # docs/ARCHITECTURE-FIRST-PRINCIPLES.md §5)
                        # is the only place we observe time.time() in
                        # the T6 path — and even here only at the
                        # facade boundary, never feeding back into
                        # the anchor.
                        # Capture the wall clock ONCE, so the value we
                        # log is exactly the value chrony receives.
                        _push_wall = time.time()
                        # ---- hf-timestd#7 defect 1 ----
                        # A refclock sample must be a SIMULTANEOUS pair:
                        # "when the host clock read system_time, true time
                        # was reference_time".  reference_time describes the
                        # PPS edge, but system_time used to be read here, at
                        # push -- and the boxcar MF cannot detect an edge
                        # until it holds +-N = +-0.5 s around the candidate
                        # (idx = arange(N, len(buf) - N)).  So the pair was
                        # ~465 ms apart and chrony read that structural
                        # detection latency as a half-second clock error,
                        # marking HPPS falseticker at ~-545 ms with a 55 us
                        # bound -- precise, repeatable, and wrong.  Measured
                        # on AC0G-B4 2026-08-08: +546 ms before, +28 ms after.
                        #
                        # The RTP counter is GPSDO-locked, so the interval
                        # between the edge and the newest ingested sample is
                        # exact arithmetic.  Back it off the push wall clock
                        # to recover what the host clock read AT the edge.
                        # No new time source; nothing feeds back into the
                        # anchor.
                        #
                        # Residual (not addressed here): network + radiod
                        # buffering latency between a sample's true time and
                        # its arrival here, ~one batch (20 ms at 96 kHz).
                        # The structural half-second is the dominant term
                        # and is removed exactly.
                        # ---- hf-timestd#18 ----
                        # The RTP arithmetic below is exact (GPSDO-locked
                        # counter), but it is anchored to _push_wall -- a wall
                        # clock read at PUSH time -- while rtp_buf[-1] ARRIVED
                        # earlier, with variable latency.  So it assumes
                        # host_time(rtp_buf[-1]) == _push_wall, and the
                        # difference went straight into system_time: measured
                        # 13-45 ms on AC0G-B4 2026-08-16, tracking push
                        # lateness with slope -1, while the SAME anchor read
                        # through NativeAnchorBench was accurate to 0.8 ms.
                        # Kept as the fallback, and as the before/after term
                        # in the seam log.
                        _legacy_sys_at_edge = _push_wall
                        _pair_fallback = None
                        try:
                            _rtp_buf = getattr(
                                self._t6_calibrator, '_rtp_buf', None
                            )
                            _sr = int(self._t6_calibrator.sample_rate)
                            if _rtp_buf is None or not len(_rtp_buf) or not _sr:
                                _pair_fallback = "no rtp buffer"
                            else:
                                _delta = (
                                    int(_rtp_buf[-1]) - int(last_edge_rtp)
                                ) & 0xFFFFFFFF
                                # Sanity bound: the structural lag is ~N
                                # samples.  Past a few seconds the counter is
                                # not what we think -- fall back rather than
                                # publish nonsense.
                                if 0 <= _delta <= 5 * _sr:
                                    _legacy_sys_at_edge = (
                                        _push_wall - _delta / _sr
                                    )
                                else:
                                    _pair_fallback = (
                                        "delta %d out of range" % _delta
                                    )
                        except Exception as _e:
                            _pair_fallback = "%s: %s" % (type(_e).__name__, _e)

                        # The least-delayed arrival in a rolling window is the
                        # honest monotonic->UTC map -- the same estimate
                        # NativeAnchorBench consumes.  Inverting it says when,
                        # on the host clock, the edge occurred, with no
                        # dependence on when this push happens to run.
                        # record=False: this path runs once per PPS edge, 10x
                        # the judge's tick, and must not shorten the sigma
                        # horizon the bench publishes.
                        _floor = None
                        _mono_now = 0.0
                        _wall_now = _push_wall
                        try:
                            _tracker = getattr(self, '_t6_arrival_floor', None)
                            _pairing = getattr(self, '_t5_pairing', None)
                            if _tracker is not None and _pairing is not None:
                                # Adjacent reads: this pair IS the
                                # CLOCK_MONOTONIC->CLOCK_REALTIME offset.
                                _mono_now = _pairing.now_mono()
                                _wall_now = time.time()
                                _floor = _tracker.estimate(
                                    _mono_now, record=False
                                )
                        except Exception:  # noqa: BLE001 — never fatal
                            _floor = None
                        if _floor is None and _pair_fallback is None:
                            _pair_fallback = "arrival floor has no estimate"

                        from .t6_shm_pair import (
                            precision_from_sigma_ns,
                            t6_shm_system_time,
                        )
                        _shm_pair = t6_shm_system_time(
                            edge_label_utc_s=pps_firing_utc_ns / 1e9,
                            floor=_floor,
                            mono_now=_mono_now,
                            wall_now=_wall_now,
                            fallback_system_time=_legacy_sys_at_edge,
                        )
                        _sys_at_edge = _shm_pair.system_time

                        # Falling back means system_time is read at push
                        # again -- the ~0.5 s defect returns, silently, and
                        # chrony would only reveal it as a falseticker much
                        # later.  Say so.  Rate-limited to 1 / 5 min.
                        if _pair_fallback is not None:
                            _now_m = time.monotonic()
                            if (_now_m - getattr(
                                    self, '_t6_pair_fallback_last_warn', 0.0)
                                    ) > 300.0:
                                self._t6_pair_fallback_last_warn = _now_m
                                logger.warning(
                                    "T6 SHM pair: no arrival-floor estimate "
                                    "(%s) -- falling back to the push-time "
                                    "construction, which republishes stream "
                                    "arrival latency (13-45 ms measured on "
                                    "B4) as clock error; if the RTP interval "
                                    "was also unavailable it re-introduces "
                                    "the whole MF detection latency (~0.5 s). "
                                    "Precision widened to the %.0f ms "
                                    "transport bound so chrony weighs it "
                                    "accordingly. See hf-timestd#18, #7.",
                                    _pair_fallback,
                                    _shm_pair.sigma_ns / 1e6,
                                )
                        # ---- end fix ----
                        # precision is DERIVED from the floor's measured
                        # sigma, never asserted.  This used to be a hardcoded
                        # -14 (61 us) regardless of what the pair was worth;
                        # against B4's measured 1.4 ms scatter that is a 26x
                        # overclaim, and it is where the "+/- 55us" column in
                        # `chronyc sources` comes from -- a number that has
                        # now caused three separate misdiagnoses by looking
                        # like a measurement.  Same derivation FUSE uses, so
                        # chrony can weigh the two feeds against each other
                        # honestly.  hf-timestd#18.
                        # hf-timestd#29: a sustained, concordant
                        # witness verdict overrides the bench's own
                        # claim.  If T4 and T3 agree the bench is 26 ms
                        # away, a 0.8 ms error bar is a false statement
                        # whatever produced it.
                        # NB: `_floor` above is the ARRIVAL floor; this
                        # is a sigma floor. Different quantity, hence the
                        # distinct name.
                        _prec = _shm_pair.precision
                        _dissent_floor = self._t6_dissent_sigma_floor_ns()
                        if (_dissent_floor is not None
                                and _dissent_floor > _shm_pair.sigma_ns):
                            _prec = precision_from_sigma_ns(_dissent_floor)
                        self._t6_shm.update(
                            reference_time=ref_time_ns / 1e9,
                            system_time=_sys_at_edge,
                            precision=_prec,
                        )
                        # Structural health signal.  push_lag is the MF's
                        # detection latency (~N/sample_rate, ~455 ms at
                        # 96 kHz); drift means the buffering changed.
                        # offset_to_chrony is what chrony now receives, and
                        # is the instrument for the residual chain-delay
                        # error (hf-timestd#7 defect 2).  1 line / 5 min.
                        _now_m = time.monotonic()
                        if (_now_m - getattr(
                                self, '_t6_seam_last_log', 0.0)) > 300.0:
                            self._t6_seam_last_log = _now_m
                            # offset_legacy is what the pre-#18 construction
                            # WOULD have published for this same edge.  Keep
                            # both terms: the fix is only demonstrated by the
                            # pair, and offset_legacy tracking push_lag while
                            # offset_to_chrony does not IS the proof.
                            logger.info(
                                "T6 SHM pair: push_lag=%.1f ms "
                                "offset_to_chrony=%.3f ms "
                                "offset_legacy=%.3f ms src=%s "
                                "precision=%d sigma=%.3f ms "
                                "chain_delay=%d ns residual=%d ns",
                                (_push_wall - pps_firing_utc_ns / 1e9) * 1e3,
                                (ref_time_ns / 1e9 - _sys_at_edge) * 1e3,
                                (ref_time_ns / 1e9
                                 - _legacy_sys_at_edge) * 1e3,
                                _shm_pair.source,
                                _shm_pair.precision,
                                _shm_pair.sigma_ns / 1e6,
                                int(effective_chain_delay),
                                int(self._t6_last_local_minus_source_ns),
                            )
                        self._t6_last_pushed_rtp = last_edge_rtp
                        self._t6_shm_push_count += 1
                except Exception as e:
                    # SHM push is non-fatal — log once per ~60 s of failures
                    if not getattr(self, '_t6_shm_warned', False):
                        logger.warning(f"T6 HPPS SHM push failed: {e}")
                        self._t6_shm_warned = True

            # The HFPS (diff-detector) SHM feed and the periodic "T6 SHM
            # diag" log line stood here until 2026-09-04.  The feed was
            # config-dead (enable_diff_sidecar never set anywhere); the
            # diag line sat inside its gate, so it never ran either.
            # Reviving the diag is a deliberate change, not a deletion
            # (RESIDUE_AUDIT §3.4).

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

            # Ring-health alarm — surface any channel whose hot ring failed
            # (e.g. a foreign-owned stale SysV segment we cannot reclaim).  A
            # silent ring failure starves that channel's metrology consumer
            # and freezes its L1, so make it loud + machine-readable here.
            ring_failures = {
                rec.config.description: rec.ring_error
                for rec in self.recorders.values()
                if getattr(rec, 'ring_error', None)
            }
            status['ring_alarm'] = {
                'ok': not ring_failures,
                'failed_channels': ring_failures,
            }

            # T6 BPSK PPS calibrator status
            if self._t6_calibrator is not None:
                # P3 residual-walk rate snapshot (None fields until the
                # estimator has its minimum span).
                _t6_rate_snapshot = {
                    'ppm': None, 'sigma_ppm': None, 'n': None, 'span_s': None,
                }
                _rate_est = getattr(self, '_t6_rate_est', None)
                if _rate_est is not None:
                    try:
                        _cur = _rate_est.current()
                        if _cur is not None:
                            _t6_rate_snapshot = {
                                'ppm': round(_cur.ppm, 4),
                                'sigma_ppm': round(_cur.sigma_ppm, 4),
                                'n': _cur.n,
                                'span_s': round(_cur.span_s, 1),
                            }
                    except Exception as e:  # noqa: BLE001 — status only
                        logger.debug(f"t6 residual rate snapshot: {e}")
                # Spec §8 asks to prove T6 held "on folded estimates
                # alone".  Which search mode produced the estimate, and
                # whether the fine_coarse cross-check ran at all, are
                # the two facts that answer it — and they reached only
                # ``last_check_metrics``, read by the transition log,
                # which emits nothing across a whole night sitting at
                # AUTHORITATIVE.  Held-without-witness was
                # indistinguishable from held-with-witness.
                # An empty metrics dict means no check has run yet:
                # report that as unknown (None), never as "verified".
                _t6_check_metrics = getattr(
                    getattr(self, '_t6_authority', None),
                    'last_check_metrics', None,
                ) or None
                status['t6_pps'] = {
                    'enabled': True,
                    'fine_search_mode': getattr(
                        getattr(self, '_t6_fine_stage', None),
                        '_last_search_mode', None,
                    ),
                    'fine_coarse_unverified': (
                        bool(_t6_check_metrics.get('fine_coarse_unverified'))
                        if _t6_check_metrics else None
                    ),
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
                    # Spec §5: one batch with a disagreeing registration
                    # discards a whole fold block, and three missed blocks
                    # trip estimate_stale.  Published so drop tolerance is
                    # built on measurement, not assumption.
                    'fold_blocks_discarded': getattr(
                        getattr(self, '_t6_fine_stage', None),
                        'blocks_discarded', None,
                    ),
                    'fold_seconds': getattr(
                        getattr(self, '_t6_fine_stage', None),
                        'fold_seconds', None,
                    ),
                    # Off-position (phantom) edges held inert while
                    # acquired — TSL3 displaced-reference fix.  None for
                    # the legacy non-MF calibrator.
                    'pps_phantom': getattr(
                        self._t6_calibrator, 'pps_phantom', None
                    ),
                    'pps_consecutive': self._t6_calibrator.pps_consecutive,
                    # Where the recovered edge falls inside the named second.
                    # Emitted under both names for one release — see
                    # `_t6_pps_edge_phase_keys`.
                    **_t6_pps_edge_phase_keys(
                        self._t6_calibrator._chain_delay_samples
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
                    # P3: the residual-walk differentiated into an ADC
                    # clock rate (ppm) — the judge's second, independent
                    # rate observable.  None below the minimum span.
                    # Recorded only; never fed back (spec §11 / G7).
                    'residual_rate_ppm': _t6_rate_snapshot['ppm'],
                    'residual_rate_sigma_ppm': _t6_rate_snapshot['sigma_ppm'],
                    'residual_rate_n_edges': _t6_rate_snapshot['n'],
                    'residual_rate_span_s': _t6_rate_snapshot['span_s'],
                    # hf-timestd-native (RTP, UTC) anchor — the single
                    # source of truth for T6 RTP→UTC labelling.  See
                    # docs/ARCHITECTURE-FIRST-PRINCIPLES.md §1 and
                    # hf_timestd.core.native_anchor.  None when no
                    # anchor has been captured yet (cold start before
                    # first lock) or after invalidation (GPSDO unlock,
                    # MF unlock, RTP discontinuity).
                    'native_anchor': (
                        self._t6_native_anchor.to_json()
                        if self._t6_native_anchor is not None else None
                    ),
                    # Pattern B publication: the scalar offset that,
                    # added to ``rtp_to_wallclock(rtp, t6_channel_info)``
                    # for any RTP, yields the same UTC the native
                    # anchor would compute by pure arithmetic.  Lets
                    # legacy consumers (those that still route through
                    # ka9q's host-clock-derived anchor) inherit the
                    # native-anchor accuracy without a code change —
                    # the cascade adds this number, the consumer
                    # applies it, the resulting UTC is anchor-equivalent.
                    # See docs/TIMING-PIPELINE-WIRING.md §4 (Pattern B)
                    # and §5.4 (the cascade's role).  Computed once per
                    # status write since the radiod anchor refreshes on
                    # its own cadence and the offset is otherwise a
                    # near-constant.  None when no anchor is captured.
                    'rtp_to_utc_offset_ns': (
                        self._compute_rtp_to_utc_offset_ns()
                        if self._t6_native_anchor is not None else None
                    ),
                }
                # Spec §4: authority transitions must reach
                # authority.json, not just this status file.  The only
                # block BpskPpsProbe reads is ``t6_pps``, so the state
                # rides here (probe → ProbeResult.detail →
                # AuthorityManager → authority.json).  Kept as two flat
                # additive keys so older probes ignore them harmlessly.
                _t6_auth_status = self._t6_authority_status()
                if _t6_auth_status is not None:
                    status['t6_pps']['authority_state'] = (
                        _t6_auth_status['state'])
                    status['t6_pps']['authority_violations'] = (
                        _t6_auth_status['violations'])
                # What we believe about the HPPS feed itself, so
                # hpps-watchdog can tell a wedge from an honest
                # withdrawal instead of restarting into both.
                _hpps_pub = self._t6_hpps_publish_status()
                status['t6_pps']['hpps_publish_mode'] = _hpps_pub['mode']
                status['t6_pps']['hpps_publishing'] = _hpps_pub['publishing']
            else:
                _t6_auth_status = self._t6_authority_status()
            status['t6_authority'] = _t6_auth_status

            # T5 LBE-1421 status block — published so the AuthorityRunner
            # side (LbeT5DirectProbe) can decide T5 availability without
            # opening a second handle to /dev/lb1421-nmea.  Always emitted
            # when the lb1421 probe is attached, even when it has no
            # current reading — the absence of fix is itself the signal.
            lb_probe = getattr(self, '_lb1421_probe', None)
            if lb_probe is not None:
                reading = lb_probe.get_latest(require_valid_fix=False)
                if reading is not None:
                    age_sec = round(time.monotonic() - reading.host_monotonic_at_read, 3)
                    status['t5_lbe1421'] = {
                        'enabled': True,
                        'valid_fix': bool(reading.valid_fix),
                        'pps_utc_sec': int(reading.pps_utc_sec),
                        'age_sec': age_sec,
                        'device': str(getattr(lb_probe, 'device', '')),
                    }
                    # P2 (audit G5b): the RTP-substrate-grounded pairing
                    # product — anchor_offset_ns is the radiod-anchor
                    # UTC prediction minus GPS/NMEA truth (prediction −
                    # truth, the codebase-wide offset sign convention).
                    # LbeT5DirectProbe:169-182 forwards it as T5's
                    # offset_ms; before this the field was never emitted
                    # and the T6↔T5 cross-check compared against a
                    # hardcoded 0.  Fields stay None (grounded=False)
                    # when the pairing has no fresh arrival/NMEA/pair —
                    # the probe then falls back to Phase-2A trust-tier
                    # semantics, exactly as before.
                    product = None
                    if reading.valid_fix:
                        try:
                            product = self._t5_bench_state()
                        except Exception as e:
                            logger.debug(f"T5 pairing compute failed: {e}")
                    status['t5_lbe1421'].update({
                        'anchor_offset_ns': (
                            int(product.anchor_offset_ns)
                            if product is not None else None
                        ),
                        'anchor_offset_sigma_ns': (
                            int(round(product.sigma_ns))
                            if product is not None else None
                        ),
                        'rtp_anchor_grounded': product is not None,
                        'anchor_age_sec': (
                            round(product.arrival_age_s, 3)
                            if product is not None else None
                        ),
                    })
                else:
                    status['t5_lbe1421'] = {
                        'enabled': True,
                        'valid_fix': False,
                        'pps_utc_sec': None,
                        'age_sec': None,
                        'device': str(getattr(lb_probe, 'device', '')),
                        'reason': 'no reading yet',
                        'anchor_offset_ns': None,
                        'anchor_offset_sigma_ns': None,
                        'rtp_anchor_grounded': False,
                        'anchor_age_sec': None,
                    }
            # If no probe is attached, no t5_lbe1421 block at all —
            # LbeT5DirectProbe treats absence as "not configured".

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
        """Graceful shutdown.

        Teardown runs in the failure path: something stopped start-up and a
        caller is releasing whatever was acquired.  So every OPTIONAL
        subsystem is read through ``getattr`` with a default here — a
        recorder whose ``__init__`` never reached the line that sets an
        attribute must still tear down.  Raising AttributeError from
        cleanup would bury the exception that actually stopped start-up
        under one from the cleanup itself.
        """
        # Hoisted once, so the body below reads normally and no later edit
        # can reintroduce a bare self._x that a half-built object lacks.
        _multi = getattr(self, '_multi', None)
        _offset_judge = getattr(self, '_offset_judge', None)
        _t6_channel_info = getattr(self, '_t6_channel_info', None)
        _t6_stream = getattr(self, '_t6_stream', None)
        _wwvb_decode_stop = getattr(self, '_wwvb_decode_stop', None)
        _wwvb_decode_thread = getattr(self, '_wwvb_decode_thread', None)
        _wwvb_l1_writer = getattr(self, '_wwvb_l1_writer', None)
        _wwvb_ledger = getattr(self, '_wwvb_ledger', None)
        _wwvb_stream = getattr(self, '_wwvb_stream', None)
        logger.info("Shutting down core recorder...")

        # Stop the shared MultiStream FIRST so its receive loop and
        # health-monitor thread aren't dispatching callbacks into
        # recorders that are mid-teardown below.  (Legacy mode stops
        # per-channel RadiodStreams inside recorder.stop().)
        if _multi is not None:
            try:
                _multi.stop()
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
        

        # Stop the Offset Judge publication thread
        if _offset_judge is not None:
            try:
                _offset_judge.stop()
                logger.info("OffsetJudge stopped")
            except Exception as e:
                logger.debug(f"OffsetJudge stop: {e}")

        # Stop T6 BPSK PPS stream
        if _t6_stream is not None:
            try:
                _t6_stream.stop()
                logger.info("T6 BPSK PPS stream stopped")
            except Exception as e:
                logger.debug(f"T6 stream stop: {e}")

        # Stop WWVB decode loop and stream
        if _wwvb_decode_stop is not None:
            _wwvb_decode_stop.set()
        if _wwvb_decode_thread is not None:
            try:
                _wwvb_decode_thread.join(timeout=5.0)
            except Exception as e:
                logger.debug(f"WWVB decode thread join: {e}")
        if _wwvb_stream is not None:
            try:
                _wwvb_stream.stop()
                logger.info("WWVB stream stopped")
            except Exception as e:
                logger.debug(f"WWVB stream stop: {e}")
        if _wwvb_ledger is not None:
            try:
                _wwvb_ledger.close()
            except Exception as e:
                logger.debug(f"WWVB ledger close: {e}")
        if _wwvb_l1_writer is not None:
            try:
                _wwvb_l1_writer.close()
                logger.info("WWVB Fusion L1 writer closed")
            except Exception as e:
                logger.debug(f"WWVB L1 writer close: {e}")

        # Remove T6 BPSK channel from radiod.  Unlike archived channels
        # (whose SSRC is deterministic from a stable description), the
        # T6 channel's SSRC is a hash of (freq, sample_rate, preset);
        # changing sample rate creates a new SSRC, so orphans accumulate
        # in radiod's channel table across restarts unless we explicitly
        # remove the previous one.  RadiodControl.remove_channel sets
        # frequency to 0 and radiod cleans it up on the next polling
        # cycle.  This is best-effort; failure is logged but non-fatal.
        if _t6_channel_info is not None:
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
        # Prefer [ka9q] status; fall back to legacy status_address
        # with DeprecationWarning per RADIOD-IDENTIFICATION.md §3.1.
        'status_address': (ka9q_section.get('status')
                           or ka9q_section.get('status_address')
                           or '239.192.152.141'),
        'ka9q': ka9q_section,
        'timing': config.get('timing', {}),
        'wwvb': config.get('wwvb', {}),
    }
    
    logger.info(f"Loaded {len(recorder_config['channels'])} channels from config")

    # Idle-unconfigured guard (EX_CONFIG=78, matches wspr/psk/meteor/mag):
    # a config still carrying the template placeholder (<YOUR_RADIOD_STATUS>)
    # was never configured for a site.  Without this we'd fall through to
    # mDNS auto-discovery and attach to ANY radiod on the LAN — on a shared
    # network an unconfigured clone would consume a neighbor's radiod.
    # RestartPreventExitStatus=78 in the unit stops cleanly until an
    # operator runs `smd config init hf-timestd` / edits the config.
    status = str(recorder_config.get('status_address') or '')
    if status.startswith('<') and status.endswith('>'):
        logger.error(
            "config still at template placeholder (ka9q.status=%s) — "
            "configure the radiod status address before starting; "
            "exiting EX_CONFIG", status,
        )
        sys.exit(78)

    # Run recorder
    recorder = CoreRecorderV2(recorder_config)
    # Attach the T5 disambiguation reference (LB-1421 GPSDO NMEA), if
    # configured.  The probe consumes gpsdo-monitor's published per-device
    # JSON under [timing].lb1421_gpsdo_run_dir (default /run/gpsdo); the
    # gpsdo-monitor daemon is the sole owner of the serial endpoint (see
    # project_t5_nmea_probe_race).  Gated by either:
    #   [timing]
    #   lb1421_enabled       = true
    #   lb1421_gpsdo_run_dir = "/run/gpsdo"     # optional
    #   lb1421_gpsdo_serial  = "1421-..."       # optional, filter by device
    # The legacy lb1421_nmea_device key is accepted as an enable-signal
    # only; the device path itself is no longer used.
    timing_section = config.get('timing', {})

    # Anomaly IQ capture (#43).  Default OFF: the budget is bounded
    # (20/day x ~46 MB < 1 GB) but a station with a small disk should opt
    # in deliberately -- the fleet board is blind to disk-full.
    _t6_cfg_cap = (timing_section.get('t6_pps', {}) or {})
    if _t6_cfg_cap.get('anomaly_capture_enabled', False):
        from .t6_anomaly_capture import AnomalyCapture, DEFAULT_DIR as _ACD
        recorder._t6_anomaly = AnomalyCapture(
            Path(_t6_cfg_cap.get('anomaly_capture_dir', str(_ACD))),
            sample_rate_hz=int(_t6_cfg_cap.get('sample_rate', 96_000)),
            window_s=float(_t6_cfg_cap.get('anomaly_capture_window_s', 60.0)),
            min_interval_s=float(
                _t6_cfg_cap.get('anomaly_capture_min_interval_s', 900.0)),
            max_per_day=int(
                _t6_cfg_cap.get('anomaly_capture_max_per_day', 20)),
            retain_bytes=int(_t6_cfg_cap.get(
                'anomaly_capture_retain_bytes', 2 * 1024 ** 3)),
        )
        logger.info(
            "T6 anomaly IQ capture ENABLED (window %.0f s, >= %.0f s apart, "
            "max %d/day, retain %.1f GiB). A ledger row is the matched "
            "filter's OUTPUT; raw samples are the only way to re-run it "
            "on a bad event.",
            recorder._t6_anomaly.window_samples
            / float(recorder._t6_anomaly.sample_rate_hz),
            recorder._t6_anomaly.min_interval_s,
            recorder._t6_anomaly.max_per_day,
            recorder._t6_anomaly.retain_bytes / 1024 ** 3,
        )

    # Block-drop counter for the T6 channel (always on: it costs one
    # numpy comparison per batch and closes the only blind spot in the
    # fleet loss metric).
    from .t6_zero_fill import ZeroFillCounter
    recorder._t6_zero_fill = ZeroFillCounter(
        block_samples=int(_t6_cfg_cap.get('block_samples', 1920)))

    # A-axis observer (hf-timestd#41).  Same published JSON the T5 probe
    # reads, but consumed for its `a_level_hint`: does a GPSDO discipline
    # the ADC clock?  GpsdoProbe freshness-gates and degrades to "A0" on
    # its own, so a GPSDO that dies mid-run stops being claimed.
    # Unattached, the A-level stays UNKNOWN and the coast uses the
    # undisciplined stand-in -- deliberately, see attach_a_level_provider.
    _auth_cfg = timing_section.get('authority_manager', None)
    if not isinstance(_auth_cfg, dict):
        _legacy_auth = timing_section.get('authority', None)
        _auth_cfg = _legacy_auth if isinstance(_legacy_auth, dict) else {}
    _gpsdo_cfg = (_auth_cfg.get('gpsdo', {}) or {})
    from .t6_holdover import (
        UNMEASURED_RATE_SIGMA_PPM as _UNMEAS_A1,
        UNMEASURED_RATE_SIGMA_PPM_A0 as _UNMEAS_A0,
    )
    if _gpsdo_cfg.get('enabled'):
        from .gpsdo_probe import GpsdoProbe
        _a_probe = GpsdoProbe(
            run_dir=Path(_gpsdo_cfg.get('run_dir', '/run/gpsdo')),
            serial=_gpsdo_cfg.get('serial'),
        )
        recorder.attach_a_level_provider(_a_probe.poll)
        logger.info(
            f"A-level observer attached (gpsdo run_dir="
            f"{_gpsdo_cfg.get('run_dir', '/run/gpsdo')}, "
            f"serial={_gpsdo_cfg.get('serial') or '*'}); holdover sigma "
            f"now follows the observed A-axis (#41)."
        )
    else:
        logger.warning(
            "A-level NOT observed: [timing.authority_manager.gpsdo].enabled "
            "is unset, so the T6 holdover uses the undisciplined "
            "stand-in (%.2f ppm) rather than the GPSDO-calibrated one "
            "(%.3f ppm). If this station has a GPSDO, enable the probe -- "
            "coasts are being refused far sooner than they need to be. If "
            "it does not, this is correct. See "
            "docs/design/TIMING_AUTHORITY_TWO_AXIS.md and hf-timestd#41.",
            _UNMEAS_A0, _UNMEAS_A1,
        )

    lb1421_enabled = bool(timing_section.get('lb1421_enabled', False)) or bool(
        str(timing_section.get('lb1421_nmea_device', '')).strip()
    )
    if lb1421_enabled:
        from .lb1421_t5_probe import Lb1421T5Probe, DEFAULT_RUN_DIR
        run_dir = Path(timing_section.get('lb1421_gpsdo_run_dir', str(DEFAULT_RUN_DIR)))
        serial = timing_section.get('lb1421_gpsdo_serial') or None
        lb1421_probe = Lb1421T5Probe(run_dir=run_dir, serial=serial)
        lb1421_probe.start()
        recorder.attach_lb1421_probe(lb1421_probe)
        logger.info(
            f"T5 LB-1421 NMEA probe attached "
            f"(gpsdo run_dir={run_dir}, serial={serial or '*'}); BPSK PPS "
            f"disambig will prefer GPS direct over chronyc tracking."
        )
    recorder.run()


if __name__ == '__main__':
    main()
