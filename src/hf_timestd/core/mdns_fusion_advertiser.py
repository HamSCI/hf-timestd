"""
MdnsFusionAdvertiser — publishes this host as a Fusion NTP server on the
LAN via `avahi-publish-service _ntp._udp 123` with a HamSCI-aware TXT
record (schema v1 per METROLOGY.md §4.6).

The advertiser is gated on the active T-level: only publishes when the
authority manager reports T3 or T6 active (i.e., when hf-timestd is
producing a useful Fusion offset). In any other state the subprocess
is killed and the mDNS record ages out within ~120 s — matching the
coupling rule in §4.5.2 (authority.json, chrony SHM reach, and mDNS
all decay together when fusion hangs).

The advertised NTP service is standard — it's chrony on UDP 123,
already configured via the drop-in `allow` rule. The TXT extension is
for HamSCI-aware consumers (sigmond's `smd lan-fusion-client` etc.) to
prefer high-quality Fusion hosts over generic NTP peers. Non-HamSCI
clients ignore the TXT entirely and use us as a plain NTP server —
graceful degradation per RFC 6763.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:
    from hf_timestd.core.authority_manager import AuthorityState

log = logging.getLogger(__name__)

TXT_SCHEMA = "v1"
DEFAULT_SERVICE_TYPE = "_ntp._udp"
DEFAULT_PORT = 123
ENABLED_T_LEVELS = ("T3", "T6")


@dataclass
class AdvertiseResult:
    target_state: str     # "advertising" | "withdrawn"
    applied: bool         # True iff a subprocess action happened this tick
    reason: str = ""


class MdnsFusionAdvertiser:
    """Manages a long-running `avahi-publish-service` subprocess whose
    liveness mirrors the authority manager's T-level gate."""

    def __init__(
        self,
        avahi_bin: Optional[str] = None,
        service_type: str = DEFAULT_SERVICE_TYPE,
        port: int = DEFAULT_PORT,
        hostname: Optional[str] = None,
        dry_run: bool = False,
        popen: Optional[Callable[..., "subprocess.Popen"]] = None,
    ):
        self.avahi_bin = avahi_bin or shutil.which("avahi-publish-service") or "avahi-publish-service"
        self.service_type = service_type
        self.port = int(port)
        self.hostname = hostname or socket.gethostname()
        self.dry_run = bool(dry_run)
        self._popen_factory = popen or subprocess.Popen
        self._proc: Optional[subprocess.Popen] = None
        self._current_signature: Optional[tuple] = None
        self._current_state: Optional[str] = None  # "advertising"|"withdrawn"

    def apply(
        self,
        state: "AuthorityState",
        governor_radiod: Optional[str] = None,
    ) -> AdvertiseResult:
        """React to the authority manager's latest state.

        When T3 or T6 is active and the TXT-relevant fields agree with
        the currently-running subprocess, this is a no-op.  Any change
        — first-time start, TXT content changed, or level no longer
        eligible — causes a subprocess kill + optional restart.
        """
        if state.t_level_active in ENABLED_T_LEVELS:
            signature = self._signature(state, governor_radiod)
            if self._current_state == "advertising" and signature == self._current_signature:
                return AdvertiseResult(target_state="advertising", applied=False, reason="no change")
            # State changed (or TXT content changed): restart with new values.
            self._stop_proc()
            txt = self._build_txt(state, governor_radiod)
            ok, reason = self._start_proc(txt)
            if ok:
                self._current_signature = signature
                self._current_state = "advertising"
                return AdvertiseResult(
                    target_state="advertising", applied=True,
                    reason=reason,
                )
            self._current_signature = None
            self._current_state = None
            return AdvertiseResult(
                target_state="advertising", applied=False, reason=reason,
            )

        # Target state is withdrawn.
        if self._current_state == "withdrawn":
            return AdvertiseResult(target_state="withdrawn", applied=False, reason="no change")
        self._stop_proc()
        self._current_state = "withdrawn"
        self._current_signature = None
        return AdvertiseResult(target_state="withdrawn", applied=True, reason="stopped")

    def close(self) -> None:
        """Kill the subprocess on service shutdown."""
        self._stop_proc()
        self._current_state = None
        self._current_signature = None

    # ---- internals ----

    def _signature(
        self, state: "AuthorityState", governor_radiod: Optional[str],
    ) -> tuple:
        """Hashable fingerprint of TXT-relevant fields so we know when to
        restart the subprocess."""
        sigma_ms = None
        if state.sigma_ns is not None:
            sigma_ms = round(state.sigma_ns / 1_000_000.0, 3)
        return (
            state.a_level,
            state.t_level_active,
            sigma_ms,
            tuple(state.stations_contributing or []),
            tuple(state.disagreement_flags or []),
            governor_radiod,
        )

    def _build_txt(
        self, state: "AuthorityState", governor_radiod: Optional[str],
    ) -> List[str]:
        sigma_ms = "unknown"
        if state.sigma_ns is not None:
            sigma_ms = f"{state.sigma_ns / 1_000_000.0:.3f}"
        stations = ",".join(state.stations_contributing or []) or "none"
        disagreement = ",".join(state.disagreement_flags or []) or "none"
        txt = [
            f"schema={TXT_SCHEMA}",
            "source=fusion",
            f"host={self.hostname}",
            f"A={state.a_level}",
            f"T={state.t_level_active}",
            f"q95_ms={sigma_ms}",
            f"stations={stations}",
            f"disagreement={disagreement}",
        ]
        if governor_radiod:
            txt.append(f"radiod={governor_radiod}")
        return txt

    def _start_proc(self, txt: List[str]) -> tuple:
        if self.dry_run:
            log.info("mDNS (dry_run) would publish: %r", txt)
            return True, "dry_run"

        service_name = f"hf-timestd Fusion ({self.hostname})"
        cmd = [
            self.avahi_bin, service_name, self.service_type, str(self.port),
        ] + txt

        try:
            self._proc = self._popen_factory(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, "started"
        except FileNotFoundError:
            self._proc = None
            return False, "avahi-publish-service not found"
        except OSError as e:
            self._proc = None
            return False, f"spawn error: {e}"

    def _stop_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
        except Exception as e:
            log.debug("mDNS subprocess stop error: %s", e)
        self._proc = None


