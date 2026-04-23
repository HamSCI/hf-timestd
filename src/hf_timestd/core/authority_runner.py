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
from hf_timestd.core.bootstrap_coordinator import BootstrapCoordinator
from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate
from hf_timestd.core.chrony_stepper import ChronyStepper
from hf_timestd.core.mdns_fusion_advertiser import MdnsFusionAdvertiser
from hf_timestd.core.chrony_tracking_probe import (
    ChronyTrackingProbe,
    match_any_server_not_in,
    match_by_names,
    match_refclock,
)
from hf_timestd.core.coarse_time_source import CoarseTimeFileSource
from hf_timestd.core.fusion_status_probe import FusionStatusProbe

log = logging.getLogger(__name__)


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

    Expected config shape (all optional — missing sections disable the
    corresponding probes):

        [timing.authority]
        interval_sec = 30.0
        upgrade_hysteresis = 3
        a_level = "A1"           # "A1" (GPSDO) or "A0"

        [timing.authority.t5]
        refid = "GPS"            # optional — default: any refclock

        [timing.authority.t4]
        peers = ["timeserver.lan", "192.168.1.80"]

        [timing.authority.t2]
        enabled = true           # if true, match any non-T4 server

        [timing.authority.t3]
        min_stations = 2
        freshness_sec = 60.0

        [timing.authority.bootstrap]
        enabled = true
        coarse_time_path = "/run/hf-timestd/coarse_time.json"
        threshold_sec = 5.0
        max_step_sec = 3600.0
        dry_run = false          # if true, log but don't invoke chronyc

        [timing.authority.chrony_gate]
        enabled = true
        refid = "HFSN"           # must match the chrony.conf refclock entry
        dry_run = false

        [timing.authority.mdns]
        enabled = true
        dry_run = false          # if true, log TXT but don't fork avahi
    """
    auth_cfg = (config.get("timing", {}) or {}).get("authority", {}) or {}
    interval_sec = float(auth_cfg.get("interval_sec", 30.0))
    hysteresis = int(auth_cfg.get("upgrade_hysteresis", 3))
    a_level_cfg = auth_cfg.get("a_level", "A1")
    if a_level_provider is None:
        a_level_provider = lambda: a_level_cfg  # noqa: E731

    # Governor-radiod identifier for the multi-radiod case
    # (METROLOGY.md §4.5.1). Default: read [ka9q].status_address so the
    # name hf-timestd uses for its own input is what's exposed to
    # cross-host consumers (wspr-recorder, LAN NTP peers).
    if governor_radiod_provider is None:
        governor_cfg = (config.get("ka9q", {}) or {}).get("status_address")
        if governor_cfg:
            governor_radiod_provider = lambda: str(governor_cfg)  # noqa: E731

    t3_cfg = auth_cfg.get("t3", {}) or {}
    t4_cfg = auth_cfg.get("t4", {}) or {}
    t5_cfg = auth_cfg.get("t5", {}) or {}
    t2_cfg = auth_cfg.get("t2", {}) or {}

    t4_peers: List[str] = list(t4_cfg.get("peers", []) or [])

    probes: List[Probe] = [
        FusionStatusProbe(
            status_path=fusion_status_path,
            freshness_sec=float(t3_cfg.get("freshness_sec", 60.0)),
            min_stations=int(t3_cfg.get("min_stations", 2)),
        ),
    ]

    if "refid" in t5_cfg or t5_cfg.get("enabled"):
        probes.append(ChronyTrackingProbe(
            t_level="T5",
            source_matcher=match_refclock(t5_cfg.get("refid")),
        ))

    if t4_peers:
        probes.append(ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(t4_peers),
        ))

    if t2_cfg.get("enabled"):
        # T2 witnesses — any server not already claimed by T4.
        probes.append(ChronyTrackingProbe(
            t_level="T2",
            source_matcher=match_any_server_not_in(t4_peers),
        ))

    bootstrap_coordinator = None
    boot_cfg = auth_cfg.get("bootstrap", {}) or {}
    if boot_cfg.get("enabled"):
        coarse_path = Path(boot_cfg.get("coarse_time_path", "/run/hf-timestd/coarse_time.json"))
        bootstrap_coordinator = BootstrapCoordinator(
            coarse_source=CoarseTimeFileSource(path=coarse_path),
            stepper=ChronyStepper(dry_run=bool(boot_cfg.get("dry_run", False))),
            threshold_sec=float(boot_cfg.get("threshold_sec", 5.0)),
            max_step_sec=float(boot_cfg.get("max_step_sec", 3600.0)),
        )

    chrony_gate = None
    gate_cfg = auth_cfg.get("chrony_gate", {}) or {}
    if gate_cfg.get("enabled"):
        chrony_gate = ChronyRefclockGate(
            refid=str(gate_cfg.get("refid", "HFSN")),
            dry_run=bool(gate_cfg.get("dry_run", False)),
        )

    mdns_advertiser = None
    mdns_cfg = auth_cfg.get("mdns", {}) or {}
    if mdns_cfg.get("enabled"):
        mdns_advertiser = MdnsFusionAdvertiser(
            dry_run=bool(mdns_cfg.get("dry_run", False)),
        )

    manager = AuthorityManager(
        probes=probes,
        output_path=authority_output_path,
        a_level_provider=a_level_provider,
        upgrade_hysteresis=hysteresis,
        bootstrap_coordinator=bootstrap_coordinator,
        chrony_gate=chrony_gate,
        governor_radiod_provider=governor_radiod_provider,
        mdns_advertiser=mdns_advertiser,
    )
    return AuthorityRunner(manager=manager, interval_sec=interval_sec)
