"""
AuthorityRunner — runs AuthorityManager.tick() on a fixed cadence from
its own thread, and exposes a factory that wires up probes from a
timestd-config.toml dict.

The runner is designed to be embedded in timestd-fusion's
run_fusion_service() so that the heartbeat-coupling rule from §4.5.2
holds (authority.json, chrony SHM, and mDNS all go silent together if
the fusion process hangs).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, List, Optional

from hf_timestd.core.authority_manager import (
    AuthorityManager,
    Probe,
)
from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe
from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate
from hf_timestd.core.mdns_fusion_advertiser import MdnsFusionAdvertiser
from hf_timestd.core.chrony_tracking_probe import (
    ChronyTrackingProbe,
    match_any_server_not_in,
    match_by_names,
    match_refclock,
)
from hf_timestd.core.fusion_status_probe import FusionStatusProbe
from hf_timestd.core.gpsdo_probe import GpsdoProbe
from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore

log = logging.getLogger(__name__)


def _opt_float(value: object) -> Optional[float]:
    """Coerce an optional config scalar to float, or None if absent/blank.

    Used for the per-tier ``max_error_ms`` chrony knobs, which are absent
    in the common case and must stay None (check disabled) rather than
    defaulting to a numeric ceiling."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class AuthorityRunner:
    """Thread wrapper around AuthorityManager.tick()."""

    def __init__(self, manager: AuthorityManager, interval_sec: float = 30.0):
        self.manager = manager
        self.interval_sec = float(interval_sec)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="AuthorityManager", daemon=True,
        )
        self._thread.start()
        log.info("Authority manager thread started (interval=%.1fs)", self.interval_sec)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning("Authority manager thread did not exit in %.1fs", timeout)
        # Tear down any long-running subprocesses the manager owns (mDNS
        # advertiser's avahi-publish-service child, primarily). Done after
        # the thread joins so we don't race with a final tick.
        adv = getattr(self.manager, "mdns_advertiser", None)
        if adv is not None:
            try:
                adv.close()
            except Exception as e:
                log.warning("mDNS advertiser close failed: %s", e)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        # One eager tick so authority.json exists promptly after startup.
        self._safe_tick()
        while not self._stop.wait(self.interval_sec):
            self._safe_tick()

    def _safe_tick(self) -> None:
        try:
            self.manager.tick()
        except Exception as e:
            log.exception("Authority manager tick failed: %s", e)


def build_authority_runner_from_config(
    config: dict,
    fusion_status_path: Path = Path("/run/hf-timestd/fusion_status.json"),
    authority_output_path: Path = Path("/run/hf-timestd/authority.json"),
    a_level_provider: Optional[Callable[[], str]] = None,
    governor_radiod_provider: Optional[Callable[[], Optional[str]]] = None,
) -> AuthorityRunner:
    """Build an AuthorityRunner from a timestd-config.toml dict.

    Config lives under the `[timing.authority_manager]` namespace so it
    cannot collide with the `[timing] authority = "rtp" | "auto" | ...`
    scalar key that already exists as the operator's preference hint
    (see METROLOGY.md §4.5 "Relationship to 'RTP Mode' and 'Fusion
    Mode'"). The two are independent: `[timing] authority` is the
    preferred T-level; `[timing.authority_manager]` is how the manager
    runs.

    Expected config shape (all optional — missing sections disable the
    corresponding probes):

        [timing.authority_manager]
        interval_sec = 30.0
        upgrade_hysteresis = 3
        a_level = "A1"           # "A1" (GPSDO) or "A0"

        [timing.authority_manager.t6]
        enabled = true           # opt-in — only sites with a BPSK PPS injector
        # status_path = "/var/lib/timestd/status/core-recorder-status.json"
        # freshness_sec = 60.0   # max age of core-recorder-status.json
        # min_consecutive = 1    # require this many clean PPS edges (DEFAULT 1)
        #                          # 1 = follow calibrator's own "locked" boolean,
        #                          # let AuthorityManager hysteresis (3 ticks) smooth
        #                          # 10 matches calibrator's consecutive_required
        #                          # (the most this can be without opening a band
        #                          # where locked=true but the probe still drops T6)
        #                          # 30 (former default) was too strict given typical
        #                          # 10-15% noise rate causing brief consec resets
        # sigma_floor_ms = 0.001 # T6 published-sigma FLOOR (default 1 µs);
        #                          # legacy key `sigma_ms` still accepted. The
        #                          # published sigma is max(MF jitter, |residual|,
        #                          # this floor) — see BpskPpsProbe — so this only
        #                          # sets the irreducible-calibration floor, not a
        #                          # fixed uncertainty.
        # Phase 2C (default ON) — demote T6 → T5 when the drift monitor
        # reports a sustained breach for ``demote_on_breach_min_cycles``
        # consecutive ticks AND T5 is available past hysteresis, i.e. the
        # RTP anchor has drifted enough that T6's SHM feed would mislead
        # chrony.  Opt out with ``demote_on_breach = false``.
        # demote_on_breach = true
        # demote_on_breach_min_cycles = 3

        [timing.authority_manager.t5]
        refid = "GPS"            # optional — default: any refclock
        # max_error_ms = 5.0     # optional error-margin ceiling, applies only
        #                          # to the chrony-refclock fallback path (§4.5)

        [timing.authority_manager.t4]
        peers = ["timeserver.lan", "192.168.1.80"]
        # max_error_ms = 5.0     # optional: drop the T4 witness when chrony's
        #                          # last-sample error margin exceeds this
        #                          # ("RMS within tier limit", §4.5).  Off by
        #                          # default — the cross-check layer already
        #                          # catches a drifted witness.

        [timing.authority_manager.t2]
        enabled = true           # if true, match any non-T4 server
        # max_error_ms = 50.0    # optional error-margin ceiling (see t4)

        [timing.authority_manager.t3]
        min_stations = 2
        freshness_sec = 60.0

        [timing.authority_manager.chrony_gate]
        enabled = true
        refid = "HFSN"           # must match the chrony.conf refclock entry
        dry_run = false

        [timing.authority_manager.gpsdo]
        enabled = true           # read gpsdo-monitor's /run/gpsdo/*.json
        run_dir = "/run/gpsdo"   # optional — match the gpsdo-monitor daemon
        # serial = "LBE1421-ABC123"   # optional — restrict to one device
        # staleness_factor = 3.0      # optional — max age in units of the
                                      #   device's probe_interval_sec (floored 30s)

        [timing.authority_manager.mdns]
        enabled = true
        dry_run = false          # if true, log TXT but don't fork avahi

    For backward compatibility the old `[timing.authority]` sub-table
    is still read when it appears as a dict, but it is deprecated
    because it namespace-clashes with `[timing] authority = "..."` (a
    legitimate scalar preference key): if both are present in a TOML
    file it's a parse error, and if only the scalar is present (the
    common deployed case today) the old code path raised AttributeError
    on startup. The wrapper below handles all three shapes defensively:
    the new `authority_manager` sub-table, the legacy `authority`
    sub-table (dict), or a scalar `authority` under `[timing]` (ignored
    for manager configuration, falls back to defaults).
    """
    _timing = config.get("timing", {}) or {}
    if not isinstance(_timing, dict):
        _timing = {}
    # Prefer the new key; accept the legacy sub-table if it happens to
    # be a dict; silently fall through to {} for any other shape.
    # `timing.authority` is NOT ours. sigmond owns that key and reads it from
    # this very file to decide whether hf-timestd provides timing at all
    # (sigmond/lib/sigmond/clients/hftimestd.py: `provides_timing`). On B4 it
    # holds the string "rtp", and it is correct there — it must not be
    # "cleaned up".
    #
    # Reading it as our tier-config sub-table was a namespace collision, and
    # an expensive one: the string was discarded as "not a dict", every tier's
    # settings silently became {}, and T6 was never registered on a host with
    # a working TS-1 while `[timing.t6_pps] enabled = true` sat in the file
    # looking correct. Probe registration no longer depends on this config at
    # all (see below), but we also stop reading someone else's key.
    #
    # A dict here is still accepted, for any deployment that genuinely used
    # the old sub-table shape before sigmond claimed the name.
    auth_cfg = _timing.get("authority_manager", None)
    if not isinstance(auth_cfg, dict):
        _legacy = _timing.get("authority", None)
        auth_cfg = _legacy if isinstance(_legacy, dict) else {}
    interval_sec = float(auth_cfg.get("interval_sec", 30.0))
    hysteresis = int(auth_cfg.get("upgrade_hysteresis", 3))
    a_level_cfg = auth_cfg.get("a_level", "A1")
    # The gpsdo-monitor probe serves two readers: the A-level (below) and
    # the host-clock PPS-rate witness (host_clock_integrity.py).  One
    # instance, so both read the same file the same way.
    gpsdo_probe: Optional[GpsdoProbe] = None
    gpsdo_cfg = auth_cfg.get("gpsdo", {}) or {}
    if gpsdo_cfg.get("enabled"):
        gpsdo_probe = GpsdoProbe(
            run_dir=Path(gpsdo_cfg.get("run_dir", "/run/gpsdo")),
            serial=gpsdo_cfg.get("serial"),
            staleness_factor=float(
                gpsdo_cfg.get("staleness_factor",
                              GpsdoProbe.DEFAULT_STALENESS_FACTOR)
            ),
        )
    if a_level_provider is None:
        if gpsdo_probe is not None:
            # Hand A-level off to the gpsdo-monitor daemon running on
            # this host. If the daemon isn't running or its files are
            # stale, GpsdoProbe.poll() returns "A0" — the authority
            # manager then treats this host as having no local GPSDO
            # witness, which is the correct degradation.
            a_level_provider = gpsdo_probe.poll
        else:
            a_level_provider = lambda: a_level_cfg  # noqa: E731

    # Host-clock verdict thresholds (host_clock_integrity.py).  `validate`
    # checks these for sense; the manager takes them as given.
    hc_cfg = auth_cfg.get("host_clock", {}) or {}
    host_clock_fault_ms = float(hc_cfg.get("fault_ms", 1000.0))
    host_clock_rate_suspect_ppm = float(hc_cfg.get("rate_suspect_ppm", 50.0))
    host_clock_alarm_repeat_sec = float(hc_cfg.get("alarm_repeat_sec", 3600.0))

    # Governor-radiod identifier for the multi-radiod case
    # (METROLOGY.md §4.5.1). Default: read [ka9q].status (the
    # multicast hostname per RADIOD-IDENTIFICATION.md §3.1; falls
    # back to legacy status_address with DeprecationWarning).  The
    # name hf-timestd uses for its own input is what's exposed to
    # cross-host consumers (wspr-recorder, LAN NTP peers).
    if governor_radiod_provider is None:
        from ..config_utils import resolve_ka9q_status
        governor_cfg = resolve_ka9q_status(config)
        if governor_cfg:
            governor_radiod_provider = lambda: str(governor_cfg)  # noqa: E731

    t3_cfg = auth_cfg.get("t3", {}) or {}
    t4_cfg = auth_cfg.get("t4", {}) or {}
    t5_cfg = dict(auth_cfg.get("t5") if isinstance(auth_cfg.get("t5"), dict) else {})
    # `[timing] lb1421_enabled` is the deployed spelling of the T5 off switch.
    if "lb1421_enabled" not in t5_cfg and "lb1421_enabled" in _timing:
        t5_cfg["lb1421_enabled"] = _timing["lb1421_enabled"]
    # T6 settings live in two places in the wild: the modern
    # [timing.authority_manager.t6] and the deployed [timing.t6_pps].
    # Merge both, modern wins — otherwise an operator's documented off
    # switch (`[timing.t6_pps] enabled = false`) would be read by nobody,
    # which is the same silence that hid this bug in the first place.
    t6_cfg = {**(_timing.get("t6_pps") if isinstance(_timing.get("t6_pps"), dict) else {}),
              **(auth_cfg.get("t6") if isinstance(auth_cfg.get("t6"), dict) else {})}
    t2_cfg = auth_cfg.get("t2", {}) or {}

    t4_peers: List[str] = list(t4_cfg.get("peers", []) or [])

    probes: List[Probe] = [
        FusionStatusProbe(
            status_path=fusion_status_path,
            freshness_sec=float(t3_cfg.get("freshness_sec", 60.0)),
            min_stations=int(t3_cfg.get("min_stations", 2)),
        ),
    ]

    # Registration is by DETECTION, not configuration. Every probe below
    # reports `available=False` with a reason when its source is absent, so
    # registering one costs nothing and a missing config can no longer make a
    # tier silently invisible. Config supplies tuning, and an explicit
    # `enabled = false` still opts out.
    #
    #   T6  TS-1 present and locked      -> probe reads core-recorder-status
    #   T5  LBE-1421 present on USB      -> probe reads the same status file
    #   T3  always                        -> fusion is the floor
    #   T4  outside information           -> genuinely needs configured peers
    #
    if t6_cfg.get("enabled", True) is not False:
        # Backward-compat: older configs key the sigma floor as ``sigma_ms``
        # (the historical hardcoded sigma value).  Accept both — preferring
        # the new name when both are set.
        sigma_floor_ms = float(t6_cfg.get(
            "sigma_floor_ms", t6_cfg.get("sigma_ms", 0.001),
        ))
        probes.append(BpskPpsProbe(
            status_path=Path(t6_cfg.get(
                "status_path", "/var/lib/timestd/status/core-recorder-status.json",
            )),
            freshness_sec=float(t6_cfg.get("freshness_sec", 60.0)),
            min_consecutive=int(t6_cfg.get("min_consecutive", 1)),
            sigma_floor_ms=sigma_floor_ms,
        ))

    # T5 source precedence: substrate-grounded LbeT5DirectProbe
    # (reads the t5_lbe1421 block from core-recorder-status.json)
    # wins when configured, falling back to ChronyTrackingProbe for
    # deployments that expose T5 via a chrony refclock instead.
    # See project_rtp_substrate_architecture: T5 is canonically the
    # LBE-1421 USB-NMEA path.  The chrony route remains for environments
    # without LBE-1421.
    # T5 precedence: EXPLICIT config beats detection, or a host with no
    # LBE-1421 that deliberately points T5 at a chrony refclock would get a
    # permanently-unavailable direct probe and lose its working path. That
    # regression was caught by test_t5_chrony_refid_still_works_without_lb1421.
    t5_lb1421_status = t5_cfg.get("lb1421_status_path")
    _t5_lb_explicit = bool(t5_lb1421_status) or t5_cfg.get("lb1421_enabled") is True
    _t5_chrony_explicit = "refid" in t5_cfg or bool(t5_cfg.get("enabled"))
    if _t5_lb_explicit or (
        not _t5_chrony_explicit and t5_cfg.get("lb1421_enabled", True) is not False
    ):
        probes.append(LbeT5DirectProbe(
            status_path=Path(
                t5_lb1421_status
                or "/var/lib/timestd/status/core-recorder-status.json"
            ),
            freshness_sec=float(t5_cfg.get("freshness_sec", 60.0)),
            max_nmea_age_sec=float(t5_cfg.get("max_nmea_age_sec", 2.0)),
            sigma_floor_ms=float(t5_cfg.get("sigma_floor_ms", 5.0)),
        ))
    elif "refid" in t5_cfg or t5_cfg.get("enabled"):
        probes.append(ChronyTrackingProbe(
            t_level="T5",
            source_matcher=match_refclock(t5_cfg.get("refid")),
            max_error_ms=_opt_float(t5_cfg.get("max_error_ms")),
        ))

    if t4_peers:
        probes.append(ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(t4_peers),
            max_error_ms=_opt_float(t4_cfg.get("max_error_ms")),
        ))

    if t2_cfg.get("enabled"):
        # T2 witnesses — any server not already claimed by T4.
        probes.append(ChronyTrackingProbe(
            t_level="T2",
            source_matcher=match_any_server_not_in(t4_peers),
            # A WAN server chrony refuses to select still MEASURES, and T2's
            # job here is to cross-check, not to discipline. `trust` on the
            # FUSE refclock marks the whole pool falseticker, which used to
            # empty the witness set and silence the asymmetric T3↔T2 rule.
            # See ProbeResult.witness_only (AC0G-ND, 2026-09-03).
            # x = falseticker, - = not combined, ? = not selectable.  `trust`
            # produces `x` when the pool disagrees with the refclock and `?`
            # when it merely loses to it, and both still carry a measurement.
            # The last-poll-succeeded guard in the probe is what keeps a stale
            # offset out; the state char alone is not enough.
            witness_state_chars="x-?",
            max_error_ms=_opt_float(t2_cfg.get("max_error_ms")),
        ))

    chrony_gate = None
    gate_cfg = auth_cfg.get("chrony_gate", {}) or {}
    if gate_cfg.get("enabled"):
        chrony_gate = ChronyRefclockGate(
            refid=str(gate_cfg.get("refid", "FUSE")),
            dry_run=bool(gate_cfg.get("dry_run", False)),
            withdraw_on_host_clock=bool(gate_cfg.get("withdraw_on_host_clock", True)),
            host_clock_clear_sec=float(gate_cfg.get("host_clock_clear_sec", 600.0)),
            sudo=bool(gate_cfg.get("sudo", False)),
        )

    mdns_advertiser = None
    mdns_cfg = auth_cfg.get("mdns", {}) or {}
    if mdns_cfg.get("enabled"):
        mdns_advertiser = MdnsFusionAdvertiser(
            dry_run=bool(mdns_cfg.get("dry_run", False)),
        )

    # V1 fix layer 4 — long-term observability store.  Default ON
    # with a sensible local path; operator can disable by setting
    # `[timing.authority_manager.snapshot_store] enabled = false`.
    # Path override via the same section's `path` key.
    snapshot_store = None
    snap_cfg = auth_cfg.get("snapshot_store", {}) or {}
    if snap_cfg.get("enabled", True):
        snap_path = Path(snap_cfg.get(
            "path", "/var/lib/timestd/authority_history.db",
        ))
        try:
            snapshot_store = AuthoritySnapshotStore(snap_path)
        except Exception as exc:
            # Non-fatal: legacy behaviour (no archive) when the DB
            # can't be opened (permissions, disk full, etc.).
            log.warning(
                "AuthoritySnapshotStore disabled at %s: %s",
                snap_path, exc,
            )
            snapshot_store = None

    # Phase 2C — demote-on-breach ON by default: when T6's drift monitor
    # reports a sustained anchor breach (its SHM feed would mislead
    # chrony), hand the active cycle to T5. Opt out with
    # `[timing.authority_manager.t6] demote_on_breach = false`. See
    # AuthorityManager docstring + METROLOGY §4.5.
    demote_t6_on_breach = bool(t6_cfg.get("demote_on_breach", True))
    demote_t6_on_breach_min_cycles = int(
        t6_cfg.get("demote_on_breach_min_cycles", 3)
    )

    # Receiver operating point (provenance).  radiod's RX888 AGC moves
    # the analog front-end gain once per second from TOTAL band power,
    # and 0.52 dB of the T6 pilot's C/N0 rides on every dB of it (B4,
    # 2026-08-28) — an uncertainty term that was previously recorded
    # nowhere.  Needs both a radiod to ask and a T6 frequency to ask
    # about; SSRCs are hash-assigned, so there is no guessing fallback.
    frontend_probe = None
    from ..config_utils import resolve_ka9q_status as _resolve_ka9q_status
    _fe_status = _resolve_ka9q_status(config)
    _fe_freq = t6_cfg.get("frequency_hz")
    if _fe_status and _fe_freq:
        from hf_timestd.core.frontend_probe import (
            FrontendProbe, SsrcByFrequency,
        )

        def _make_control(address=str(_fe_status)):
            # Deferred: RadiodControl resolves the mDNS name in its
            # constructor, and building a runner must not touch the net.
            from ka9q import RadiodControl
            return RadiodControl(address, client_id="hf-timestd")

        frontend_probe = FrontendProbe(
            _make_control,
            resolve_ssrc=SsrcByFrequency(str(_fe_status), float(_fe_freq)),
        )

    manager = AuthorityManager(
        probes=probes,
        output_path=authority_output_path,
        a_level_provider=a_level_provider,
        upgrade_hysteresis=hysteresis,
        chrony_gate=chrony_gate,
        governor_radiod_provider=governor_radiod_provider,
        mdns_advertiser=mdns_advertiser,
        snapshot_store=snapshot_store,
        frontend_probe=frontend_probe,
        demote_t6_on_breach=demote_t6_on_breach,
        demote_t6_on_breach_min_cycles=demote_t6_on_breach_min_cycles,
        # OPT-IN.  Deployed on by default to AC0G-B4 2026-09-04 17:09Z, the
        # PPS-rate witness declared SUSPECT at +83.7 ppm while the LAN
        # stratum-1 held the host within 12 us.  gpsdo-monitor's pps_study
        # stamps DCD edges with time.monotonic() after an ioctl wake, and its
        # own note reads "not a metrology reference": it showed -90 ppm during
        # the runaway and +84 ppm on a correct clock, so it tracks neither the
        # raw oscillator nor the disciplined clock.  Off until someone shows
        # what it measures.  docs/design/HOST_CLOCK_INTEGRITY.md.
        host_clock_rate_provider=(
            gpsdo_probe.host_clock_rate_ppm
            if gpsdo_probe is not None and bool(hc_cfg.get("rate_witness_enabled", False))
            else None),
        host_clock_fault_ms=host_clock_fault_ms,
        host_clock_rate_suspect_ppm=host_clock_rate_suspect_ppm,
        host_clock_alarm_repeat_sec=host_clock_alarm_repeat_sec,
    )
    return AuthorityRunner(manager=manager, interval_sec=interval_sec)
