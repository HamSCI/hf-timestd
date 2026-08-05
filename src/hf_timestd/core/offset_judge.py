#!/usr/bin/env python3
"""
OffsetJudge — hf-timestd's independent judgment of UTC vs radiod's mapping.

Implementation of docs/OFFSET-JUDGE-SPEC-2026-08-05.md (Phase P1).

Doctrine (spec §1):
  * The RTP counter is the steel ruler — its *rate* is GPSDO-disciplined
    and trusted.
  * radiod's advertised (GPS_TIME, RTP_TIMESNAP) epoch is a MEASUREMENT,
    not an axiom.  The judge computes its own best UTC from the best
    available bench and measures the difference:

        offset_s(t) = UTC_judge(rtp) - UTC_radiod_s(rtp)
        label(rtp)  = UTC_radiod_s(rtp) + offset_s

  * radiod being wrong is not an emergency: with offsets applied,
    wrong-epoch radiod produces correct labels plus a large,
    loudly-logged offset — never data loss.

Benches (P1 set, spec §2/§10):
  * T4/T2 — chrony (`chronyc tracking` + `chronyc -n -c sources`).
    The bench's UTC is the host wallclock corrected by chrony's own
    measured system-time offset; its sigma comes from chrony's root
    dispersion / root delay / RMS offset statistics (empirical, §13.1).
    T4 when the selected source is a LAN refclock/peer, T2 for WAN.
  * T3 — FUSE (/run/hf-timestd/fusion_status.json): judge UTC =
    wallclock + fused clock offset, sigma from the file's own
    uncertainty budget.
  * T1 holdover — when no bench answers, the last good offset is held
    and sigma grows with age (trivial tier per spec §2).

NOTE on the timing-authority invariant (CLAUDE.md / METROLOGY.md):
the "never chronyc tracking in the timing path" rule protects the
CHRONY-FEED path (never feed chrony from chrony).  The judge uses
chrony in a different, sanctioned role: as a calibrated *measurement
bench* for detecting radiod epoch error, with chrony's own sigma
carried honestly on every verdict.  See the spec §2 and the invariant
note in CLAUDE.md.

Scoping (spec §7): all radiod-side observations are per-source,
keyed by (status_stream, ssrc), registered explicitly by the client
that owns the channel.  GLOBAL DISCOVERY IS FORBIDDEN — this module
never calls discover_channels() or listens to any status multicast.

Dependency policy: stdlib + numpy only.  The only host-clock use is
as the chrony/fusion bench measurement itself (the bench's calibrated
role per spec §2).
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

# stdlib-only sibling imports (verified: no heavy science deps)
from .chrony_stats import parse_tracking
from .leap_second import gps_leap_seconds_at_gps_time

logger = logging.getLogger(__name__)

GPS_EPOCH_UNIX = 315964800
BILLION = 1_000_000_000
RTP_WRAP = 0x100000000

# Per-source key: (status_stream, ssrc) — spec §7.
SourceKey = Tuple[str, int]


def _rtp_delta_signed(rtp: int, rtp_ref: int) -> int:
    """Signed 32-bit RTP counter difference (rtp - rtp_ref)."""
    delta = (rtp - rtp_ref) & 0xFFFFFFFF
    if delta > 0x7FFFFFFF:
        delta -= RTP_WRAP
    return delta


def gps_time_ns_to_unix(gps_time_ns: int) -> float:
    """Convert radiod GPS_TIME (ns since GPS epoch) to Unix UTC seconds."""
    leap = gps_leap_seconds_at_gps_time(int(gps_time_ns))
    return gps_time_ns / BILLION + GPS_EPOCH_UNIX - leap


# ────────────────────────────────────────────────────────────────────
# Bench readings
# ────────────────────────────────────────────────────────────────────

@dataclass
class BenchReading:
    """One bench's answer: 'true UTC right now is `utc` ± sigma'.

    Field vocabulary follows the §18 client-contract producer surface
    (tier / sigma_ns / age) so downstream consumers see one language.
    """
    tier: str          # "T4" | "T3" | "T2"
    utc: float         # bench estimate of true UTC at capture (Unix s)
    sigma_ns: float    # bench's own empirical 1-sigma (ns)
    mono: float        # monotonic clock at capture (for extrapolation)
    detail: Dict = field(default_factory=dict)

    def utc_at(self, mono_now: float) -> float:
        """Extrapolate the reading to a later monotonic instant."""
        return self.utc + (mono_now - self.mono)


_PRIVATE_V4_PREFIXES = ("10.", "192.168.", "169.254.", "127.")


def _is_lan_address(name: str) -> bool:
    """Heuristic: is a chrony source name/IP on the local network?"""
    if not name:
        return False
    n = name.strip().lower()
    if n.endswith(".local") or n == "localhost":
        return True
    if any(n.startswith(p) for p in _PRIVATE_V4_PREFIXES):
        return True
    if n.startswith("172."):
        try:
            second = int(n.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    if n.startswith("fe80:") or n.startswith("fd") or n.startswith("fc"):
        return True  # link-local / ULA IPv6
    return False


class ChronyBench:
    """T4/T2 bench: chrony's disciplined view of true UTC.

    utc_now = wallclock - system_time_offset  (chrony's own correction)
    sigma   = root_dispersion + root_delay/2 + |rms_offset|  (chrony's
              own live error budget — empirical per spec §13.1)
    tier    = T4 if chrony's selected source is a LAN refclock/peer
              (local stratum-1 GPS+PPS), else T2 (WAN NTP).
    """

    # Never report better than 10 µs — chrony's budget can transiently
    # under-report right after a burst of good polls.
    SIGMA_FLOOR_NS = 10_000.0

    def __init__(
        self,
        timeout_sec: float = 5.0,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        time_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        self.timeout_sec = float(timeout_sec)
        self._run = runner or subprocess.run
        self._time = time_fn
        self._mono = mono_fn

    def _chronyc(self, args: List[str]) -> Optional[str]:
        try:
            proc = self._run(
                ["chronyc"] + args,
                capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    def _selected_source_is_lan(self) -> Optional[bool]:
        """Classify chrony's selected ('*') source as LAN or WAN.

        Returns None when it cannot be determined (no output, no
        selected source) — caller falls back to T2 (conservative).
        Uses `chronyc -n -c sources` CSV: mode,state,name,... .
        """
        out = self._chronyc(["-n", "-c", "sources"])
        if not out:
            return None
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            mode, state, name = parts[0], parts[1], parts[2]
            if state != "*":
                continue
            if mode == "#":       # refclock on this host — local by definition
                return True
            return _is_lan_address(name)
        return None

    def poll(self) -> Optional[BenchReading]:
        out = self._chronyc(["tracking"])
        if not out:
            return None
        tracking = parse_tracking(out)
        if tracking is None:
            return None
        # Unsynchronised chrony: refid 7F7F0101 / "Not synchronised".
        if "7F7F0101" in (tracking.reference_id or ""):
            return None
        if "not sync" in (tracking.leap_status or "").lower():
            return None

        mono = self._mono()
        wall = self._time()
        # chronyc: "System time: X seconds fast of NTP time" means the
        # host clock reads ahead of true time → true = host - X.
        # parse_tracking() returns +X for "fast", -X for "slow".
        utc = wall - tracking.system_time_offset_s

        sigma_s = (
            abs(tracking.root_dispersion_s)
            + abs(tracking.root_delay_s) / 2.0
            + abs(tracking.rms_offset_s)
        )
        sigma_ns = max(sigma_s * 1e9, self.SIGMA_FLOOR_NS)

        is_lan = self._selected_source_is_lan()
        tier = "T4" if is_lan else "T2"
        return BenchReading(
            tier=tier, utc=utc, sigma_ns=sigma_ns, mono=mono,
            detail={
                "reference_id": tracking.reference_id,
                "stratum": tracking.stratum,
                "system_time_offset_s": tracking.system_time_offset_s,
                "root_dispersion_s": tracking.root_dispersion_s,
                "root_delay_s": tracking.root_delay_s,
                "rms_offset_s": tracking.rms_offset_s,
                "selected_source_lan": is_lan,
            },
        )


class FusionBench:
    """T3 bench: FUSE multi-broadcast UTC reconstruction.

    Reads /run/hf-timestd/fusion_status.json (schema v1, written by
    FusionStatusWriter).  judge UTC = wallclock + fused clock offset
    (d_clock_fused_ms); sigma from the file's own uncertainty budget.
    Validity gating mirrors FusionStatusProbe (schema/freshness/
    availability/kalman-state/min-stations) without importing it —
    this module stays dependency-light (no authority_manager import).
    """

    def __init__(
        self,
        status_path: Path = Path("/run/hf-timestd/fusion_status.json"),
        freshness_sec: float = 60.0,
        min_stations: int = 2,
        time_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        self.status_path = Path(status_path)
        self.freshness_sec = float(freshness_sec)
        self.min_stations = int(min_stations)
        self._time = time_fn
        self._mono = mono_fn

    def poll(self) -> Optional[BenchReading]:
        try:
            with self.status_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("schema") != "v1":
            return None
        pub = data.get("utc_published")
        if not isinstance(pub, str):
            return None
        try:
            pub_dt = datetime.fromisoformat(pub[:-1] if pub.endswith("Z") else pub)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        wall = self._time()
        age = wall - pub_dt.timestamp()
        if age > self.freshness_sec:
            return None
        fusion = data.get("fusion") or {}
        if not fusion.get("available"):
            return None
        if int(fusion.get("n_stations", 0)) < self.min_stations:
            return None
        if str(fusion.get("kalman_state", "UNKNOWN")) not in ("ACQUIRING", "LOCKED"):
            return None
        try:
            d_clock_s = float(fusion["d_clock_fused_ms"]) / 1e3
            sigma_ns = float(fusion["uncertainty_ms"]) * 1e6
        except (KeyError, TypeError, ValueError):
            return None
        mono = self._mono()
        return BenchReading(
            tier="T3",
            utc=wall + d_clock_s,
            sigma_ns=max(sigma_ns, 1.0),
            mono=mono,
            detail={
                "kalman_state": fusion.get("kalman_state"),
                "n_stations": fusion.get("n_stations"),
                "status_age_s": round(age, 3),
            },
        )


class NativeAnchorBench:
    """T6 bench: the hf-timestd-native (RTP, UTC) anchor (spec §2, P2).

    The NativeAnchor labels any RTP counter value by pure counter
    arithmetic (``native_anchor.utc_ns_at_rtp`` — host-clock-free, the
    highest-pedigree UTC this system produces).  To answer the bench
    question "what is true UTC *now*", the anchor's label of the most
    recently *arrived* T6 sample is handed off to the monotonic clock
    at that sample's arrival instant:

        reading.utc  = utc_ns_at_rtp(arrival_rtp, anchor) / 1e9
        reading.mono = arrival_mono

    The residual bias is the stream transport latency (the sample was
    created before it arrived), bounded by ``LATENCY_SIGMA_FLOOR_NS``
    — carried honestly as the bench sigma even though the anchor
    itself is sub-µs.  Only answers while a valid anchor exists (the
    T6 lifecycle invalidates it on GPSDO/MF unlock and RTP
    discontinuity) and the T6 stream is flowing.

    provider() -> Optional[(anchor, arrival_rtp, arrival_mono)].
    """

    LATENCY_SIGMA_FLOOR_NS = 25_000_000.0
    ARRIVAL_MAX_AGE_S = 5.0

    def __init__(
        self,
        provider: Callable[[], Optional[Tuple]],
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        self._provider = provider
        self._mono = mono_fn

    def poll(self) -> Optional[BenchReading]:
        try:
            state = self._provider()
        except Exception:  # noqa: BLE001 — provider trouble ≠ judge trouble
            return None
        if state is None:
            return None
        anchor, arrival_rtp, arrival_mono = state
        age = self._mono() - float(arrival_mono)
        if age < 0 or age > self.ARRIVAL_MAX_AGE_S:
            return None
        try:
            from .native_anchor import utc_ns_at_rtp
            utc_ns = utc_ns_at_rtp(int(arrival_rtp) & 0xFFFFFFFF, anchor)
        except Exception:  # noqa: BLE001
            return None
        return BenchReading(
            tier="T6",
            utc=utc_ns / 1e9,
            sigma_ns=self.LATENCY_SIGMA_FLOOR_NS,
            mono=float(arrival_mono),
            detail={
                "anchor_rtp": int(anchor.anchor_rtp),
                "anchor_tier": str(anchor.captured_via_tier),
                "chain_delay_ns": int(anchor.chain_delay_ns),
                "arrival_age_s": round(age, 3),
            },
        )


class LbeT5Bench:
    """T5 bench: LB-142x GPS truth via the RTP pairing product (P2).

    The provider returns a ``T5PairingProduct`` (see
    ``t5_rtp_pairing.py``): the GPS/NMEA-attested UTC of the most
    recent stream arrival, with the pairing's honest sigma (latency
    floor + observed spread).  The bench simply re-frames it:

        reading.utc  = product.truth_utc      (attested arrival UTC)
        reading.mono = product.arrival_mono

    provider() -> Optional[T5PairingProduct].
    """

    ARRIVAL_MAX_AGE_S = 5.0

    def __init__(
        self,
        provider: Callable[[], Optional[object]],
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        self._provider = provider
        self._mono = mono_fn

    def poll(self) -> Optional[BenchReading]:
        try:
            product = self._provider()
        except Exception:  # noqa: BLE001
            return None
        if product is None:
            return None
        age = self._mono() - float(product.arrival_mono)
        if age < 0 or age > self.ARRIVAL_MAX_AGE_S:
            return None
        return BenchReading(
            tier="T5",
            utc=float(product.truth_utc),
            sigma_ns=float(product.sigma_ns),
            mono=float(product.arrival_mono),
            detail={
                "anchor_offset_ns": int(product.anchor_offset_ns),
                "pps_utc_sec": int(product.pps_utc_sec),
                "n_window": int(product.n_window),
                "arrival_age_s": round(age, 3),
            },
        )


# ────────────────────────────────────────────────────────────────────
# Verdict + per-source state
# ────────────────────────────────────────────────────────────────────

@dataclass
class OffsetVerdict:
    """The judge's current answer for one radiod source (spec §3)."""
    offset_ns: float       # judge_utc(rtp) - radiod_utc(rtp), filtered
    sigma_ns: float        # bench sigma (+ holdover growth)
    tier: str              # "T6".."T0" — bench tier that produced it
    judge_age_s: float     # age of the newest bench reading
    segment_id: int        # spec §5 — never interpolate across segments
    in_violation: bool     # |offset| > k*sigma sustained (spec §9 step 1)


@dataclass
class _SourceState:
    """Per-(status_stream, ssrc) radiod mapping + offset filter state."""
    source_key: SourceKey
    gps_time_ns: int
    rtp_timesnap: int
    sample_rate: int
    gps_unix: float
    mono_at_pair: float          # monotonic when the pair was adopted (fresh)
    segment_id: int = 1
    segment_cause: str = "initial"
    raw_window: Deque[float] = field(default_factory=lambda: deque(maxlen=5))
    ema_offset_ns: Optional[float] = None
    # (mono, offset_ns) history within the current segment — slope source
    history: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=180))
    last_step: Optional[Dict] = None
    violation_since: Optional[float] = None     # monotonic
    in_violation: bool = False
    anchor_fault_mono: Optional[float] = None   # last writer-side flag
    last_critical_log: float = 0.0              # monotonic, rate limiting

    def radiod_utc_now(self, mono_now: float) -> float:
        """radiod's implied UTC 'now', advanced on the steel ruler.

        The pair was fresh at adoption; the counter advances at true
        rate, so radiod's current UTC claim is gps_unix + elapsed.
        (Monotonic elapsed tracks the GPSDO rate to well under ppm over
        the minutes-scale spans involved — rate disagreement at that
        level is exactly what d_offset_dt_ppm records.)
        """
        return self.gps_unix + (mono_now - self.mono_at_pair)

    def radiod_utc_of_rtp(self, rtp: int) -> float:
        """radiod's raw mapping for a specific RTP counter value."""
        delta = _rtp_delta_signed(int(rtp), self.rtp_timesnap)
        return self.gps_unix + delta / self.sample_rate


# ────────────────────────────────────────────────────────────────────
# The judge
# ────────────────────────────────────────────────────────────────────

class OffsetJudge:
    """Computes and publishes per-source radiod epoch offsets (spec P1).

    Lifecycle: construct once per core-recorder process (spec §11 — no
    new daemon), `start()` the publication thread, `stop()` on
    shutdown.  Clients register their own radiod pairs via
    :meth:`register_radiod_pair`; the writer asks for the current
    verdict via :meth:`offset_for` (cheap, lock + dict lookup — no I/O
    on the hot path; benches are polled only on the tick thread).
    """

    # Tier preference, best first (P1 bench set + holdover).
    TIER_ORDER = ("T6", "T5", "T4", "T3", "T2", "T1", "T0")

    def __init__(
        self,
        config: Optional[Dict] = None,
        *,
        benches: Optional[List] = None,
        publish_path: Optional[os.PathLike] = None,
        time_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.k = float(cfg.get("k", 5.0))
        self.tick_seconds = float(cfg.get("tick_seconds", 10.0))
        self.sustain_window_s = float(cfg.get("sustain_window_s", 60.0))
        self.ema_alpha = float(cfg.get("ema_alpha", 0.2))
        self.median_window = int(cfg.get("median_window", 5))
        # Pair change beyond this is a fracture (segment), not jitter.
        # 0.75 s matches the fleet re-anchor threshold on B4.
        self.pair_step_threshold_s = float(cfg.get("pair_step_threshold_s", 0.75))
        # Measured-offset step beyond max(step_floor, 8*sigma) opens a
        # segment instead of being smoothed (spec §3).
        self.step_floor_s = float(cfg.get("step_floor_s", 0.1))
        self.step_sigma_mult = float(cfg.get("step_sigma_mult", 8.0))
        # Holdover: sigma grows at this rate while no bench answers.
        self.holdover_sigma_growth_ns_per_s = float(
            cfg.get("holdover_sigma_growth_ns_per_s", 10_000.0)
        )
        self.critical_log_interval_s = float(cfg.get("critical_log_interval_s", 60.0))
        # Writer-side anchor-fault flags auto-expire after this long
        # without re-assertion.
        self.anchor_fault_hold_s = float(cfg.get("anchor_fault_hold_s", 120.0))
        # Hysteresis: N consecutive polls on a higher tier to advance;
        # degrade immediately (spec §2).
        self.upgrade_polls = int(cfg.get("upgrade_polls", 3))

        self.publish_path = Path(
            publish_path
            or cfg.get("publish_path", "/run/hf-timestd/offset_judge.json")
        )

        self._time = time_fn
        self._mono = mono_fn

        if benches is not None:
            self.benches = list(benches)
        else:
            fusion_path = Path(cfg.get(
                "fusion_status_path", "/run/hf-timestd/fusion_status.json"))
            self.benches = [
                ChronyBench(time_fn=time_fn, mono_fn=mono_fn),
                FusionBench(
                    status_path=fusion_path,
                    freshness_sec=float(cfg.get("fusion_freshness_s", 60.0)),
                    min_stations=int(cfg.get("min_fusion_stations", 2)),
                    time_fn=time_fn, mono_fn=mono_fn,
                ),
            ]

        self._lock = threading.Lock()
        self._sources: Dict[SourceKey, _SourceState] = {}
        self._best: Optional[BenchReading] = None       # active bench reading
        self._candidate_tier: Optional[str] = None      # upgrade hysteresis
        self._candidate_count: int = 0
        self._publish_error_logged = False

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the publication/tick thread (default cadence 10 s)."""
        if not self.enabled:
            logger.info("OffsetJudge disabled by config — not starting")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="offset-judge", daemon=True
        )
        self._thread.start()
        logger.info(
            f"OffsetJudge started: tick={self.tick_seconds}s k={self.k} "
            f"publish={self.publish_path}"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.tick_seconds + 5.0)
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — judge must never die silently
                logger.error(f"OffsetJudge tick failed: {e}", exc_info=True)
            self._stop.wait(self.tick_seconds)

    def add_bench(self, bench) -> None:
        """Append a bench (P2: T6/T5 substrate benches wired by the
        recorder once its own machinery exists).  The tier cascade in
        :meth:`_select_bench_locked` orders readings by TIER_ORDER, so
        registration order is irrelevant."""
        with self._lock:
            self.benches.append(bench)

    # ── registration (spec §7: per-source, never global) ─────────────

    def register_radiod_pair(
        self,
        source_key: SourceKey,
        gps_time_ns: int,
        rtp_timesnap: int,
        sample_rate: int,
    ) -> None:
        """Adopt a radiod (GPS_TIME, RTP_TIMESNAP) pair for one source.

        Called by the owning client at every anchor adoption (channel
        creation, radiod-restart reseed, status-driven re-anchor).  The
        pair is assumed FRESH at the moment of the call (it comes from
        the client's own per-source status stream).  Deduplicated by
        (gps_time_ns, rtp_timesnap).

        A pair change beyond the plausibility threshold, or an RTP
        counter wrap, fractures the segment (spec §5).
        """
        source_key = (str(source_key[0]), int(source_key[1]))
        gps_time_ns = int(gps_time_ns)
        rtp_timesnap = int(rtp_timesnap) & 0xFFFFFFFF
        sample_rate = int(sample_rate)
        gps_unix = gps_time_ns_to_unix(gps_time_ns)
        mono_now = self._mono()

        with self._lock:
            st = self._sources.get(source_key)
            if st is None:
                st = _SourceState(
                    source_key=source_key,
                    gps_time_ns=gps_time_ns,
                    rtp_timesnap=rtp_timesnap,
                    sample_rate=sample_rate,
                    gps_unix=gps_unix,
                    mono_at_pair=mono_now,
                )
                self._sources[source_key] = st
                logger.info(
                    f"OffsetJudge: registered source {self._key_str(source_key)} "
                    f"gps_unix={gps_unix:.6f} rtp_timesnap={rtp_timesnap}"
                )
            else:
                if (gps_time_ns == st.gps_time_ns
                        and rtp_timesnap == st.rtp_timesnap):
                    return  # dedupe — same pair re-announced
                # What did the OLD mapping predict for the NEW snap?
                old_pred = st.radiod_utc_of_rtp(rtp_timesnap)
                diff = gps_unix - old_pred
                wrapped = (
                    rtp_timesnap < st.rtp_timesnap
                    and _rtp_delta_signed(rtp_timesnap, st.rtp_timesnap) > 0
                )
                if abs(diff) > self.pair_step_threshold_s:
                    self._open_segment_locked(
                        st, cause="radiod_pair_step",
                        delta_ns=diff * 1e9, mono_now=mono_now,
                    )
                elif wrapped:
                    self._open_segment_locked(
                        st, cause="rtp_wrap", delta_ns=0.0, mono_now=mono_now,
                    )
                st.gps_time_ns = gps_time_ns
                st.rtp_timesnap = rtp_timesnap
                st.sample_rate = sample_rate
                st.gps_unix = gps_unix
                st.mono_at_pair = mono_now

            # Measure at every anchor adoption (spec §3), using the
            # newest bench reading if we have one.
            if self._best is not None:
                self._measure_source_locked(st, self._best, mono_now)

    def mark_fracture(self, source_key: SourceKey, cause: str) -> None:
        """Explicitly fracture a source's segment (spec §5)."""
        source_key = (str(source_key[0]), int(source_key[1]))
        with self._lock:
            st = self._sources.get(source_key)
            if st is not None:
                self._open_segment_locked(
                    st, cause=cause, delta_ns=None, mono_now=self._mono()
                )

    def flag_anchor_fault(self, source_key: SourceKey, lag_s: float) -> None:
        """Writer-side split detector reports an anchor-fault signature.

        Sets a per-source flag (surfaced in offset_judge.json and via
        in_violation) that auto-expires after anchor_fault_hold_s
        without re-assertion.
        """
        source_key = (str(source_key[0]), int(source_key[1]))
        with self._lock:
            st = self._sources.get(source_key)
            if st is None:
                return
            st.anchor_fault_mono = self._mono()
        logger.warning(
            f"OffsetJudge: anchor-fault flagged for "
            f"{self._key_str(source_key)} (lag={lag_s:+.1f}s)"
        )

    # ── the verdict (hot path — no I/O) ──────────────────────────────

    def offset_for(self, source_key: SourceKey, rtp: int) -> Optional[OffsetVerdict]:
        """Current verdict for a source, or None (caller falls back raw).

        `rtp` names the counter the caller is labeling; within a
        segment the offset is counter-independent (the ruler is
        trusted), so the verdict is the segment's filtered estimate.
        """
        if not self.enabled:
            return None
        source_key = (str(source_key[0]), int(source_key[1]))
        mono_now = self._mono()
        with self._lock:
            st = self._sources.get(source_key)
            if st is None or st.ema_offset_ns is None or self._best is None:
                return None
            return self._verdict_locked(st, mono_now)

    def _verdict_locked(self, st: _SourceState, mono_now: float) -> OffsetVerdict:
        best = self._best
        age = mono_now - best.mono
        sigma_ns = best.sigma_ns
        tier = best.tier
        if age > 2.5 * self.tick_seconds:
            # Holdover (T1): last offset held, sigma grows (spec §2).
            sigma_ns += (age - 2.5 * self.tick_seconds) * \
                self.holdover_sigma_growth_ns_per_s
            tier = "T1"
        anchor_fault = self._anchor_fault_active_locked(st, mono_now)
        return OffsetVerdict(
            offset_ns=float(st.ema_offset_ns),
            sigma_ns=float(sigma_ns),
            tier=tier,
            judge_age_s=float(age),
            segment_id=st.segment_id,
            in_violation=bool(st.in_violation or anchor_fault),
        )

    def _anchor_fault_active_locked(self, st: _SourceState, mono_now: float) -> bool:
        return (
            st.anchor_fault_mono is not None
            and (mono_now - st.anchor_fault_mono) < self.anchor_fault_hold_s
        )

    # ── tick: poll benches, measure, publish ─────────────────────────

    def tick(self) -> None:
        """One judge cycle: poll benches, update filters, publish."""
        if not self.enabled:
            return
        readings: List[BenchReading] = []
        for bench in self.benches:
            try:
                r = bench.poll()
            except Exception as e:  # noqa: BLE001 — one bad bench ≠ no judge
                logger.debug(f"OffsetJudge bench {bench!r} poll failed: {e}")
                r = None
            if r is not None:
                readings.append(r)

        mono_now = self._mono()
        with self._lock:
            best = self._select_bench_locked(readings)
            if best is not None:
                self._best = best
                for st in self._sources.values():
                    self._measure_source_locked(st, best, mono_now)
            # Violation evaluation runs every tick even in holdover so
            # sustained windows keep counting / clearing.
            for st in self._sources.values():
                self._evaluate_violation_locked(st, mono_now)
            snapshot = self._snapshot_locked(mono_now)
        self._publish(snapshot)

    def _select_bench_locked(
        self, readings: List[BenchReading]
    ) -> Optional[BenchReading]:
        """Highest tier wins, with upgrade hysteresis (spec §2).

        Immediate degrade: if the active tier stopped answering, take
        the best available lower tier this tick.  Upgrade: a tier
        better than the active one must answer `upgrade_polls`
        consecutive ticks before adoption.
        """
        if not readings:
            self._candidate_tier = None
            self._candidate_count = 0
            return None
        by_rank = sorted(
            readings, key=lambda r: self.TIER_ORDER.index(r.tier)
            if r.tier in self.TIER_ORDER else len(self.TIER_ORDER)
        )
        best = by_rank[0]
        active_tier = self._best.tier if self._best is not None else None
        active_rank = (
            self.TIER_ORDER.index(active_tier)
            if active_tier in self.TIER_ORDER else len(self.TIER_ORDER)
        )
        best_rank = self.TIER_ORDER.index(best.tier)

        if active_tier is None or best_rank >= active_rank:
            # Same tier or degrade — adopt immediately.
            self._candidate_tier = None
            self._candidate_count = 0
            return best
        # Proposed upgrade — require consecutive confirmation.
        if self._candidate_tier == best.tier:
            self._candidate_count += 1
        else:
            self._candidate_tier = best.tier
            self._candidate_count = 1
        if self._candidate_count >= self.upgrade_polls:
            self._candidate_tier = None
            self._candidate_count = 0
            return best
        # Not confirmed yet: stay on the active tier if it still answers.
        for r in by_rank:
            if r.tier == active_tier:
                return r
        # Active tier gone: degrade path (best available).
        self._candidate_tier = None
        self._candidate_count = 0
        return best

    def _measure_source_locked(
        self, st: _SourceState, bench: BenchReading, mono_now: float
    ) -> None:
        """One raw offset measurement + median/EMA filtering (spec §3)."""
        judge_utc = bench.utc_at(mono_now)
        radiod_utc = st.radiod_utc_now(mono_now)
        raw_ns = (judge_utc - radiod_utc) * 1e9

        st.raw_window.append(raw_ns)
        med_ns = float(np.median(list(st.raw_window)))

        if st.ema_offset_ns is None:
            st.ema_offset_ns = med_ns
        else:
            band_ns = max(
                self.step_floor_s * 1e9, self.step_sigma_mult * bench.sigma_ns
            )
            if abs(med_ns - st.ema_offset_ns) > band_ns:
                # A step beyond the plausibility band is a fracture,
                # not something to smooth (spec §3/§5).
                self._open_segment_locked(
                    st, cause="offset_step",
                    delta_ns=med_ns - st.ema_offset_ns, mono_now=mono_now,
                )
                st.raw_window.append(raw_ns)  # reseed after clear
                st.ema_offset_ns = raw_ns
            else:
                st.ema_offset_ns += self.ema_alpha * (med_ns - st.ema_offset_ns)

        st.history.append((mono_now, st.ema_offset_ns))

    def _evaluate_violation_locked(self, st: _SourceState, mono_now: float) -> None:
        """k·sigma sustained-violation logic + CRITICAL logging (spec §9.1)."""
        if st.ema_offset_ns is None or self._best is None:
            return
        sigma_ns = max(self._best.sigma_ns, 1.0)
        bound_ns = self.k * sigma_ns
        if abs(st.ema_offset_ns) > bound_ns:
            if st.violation_since is None:
                st.violation_since = mono_now
            if (mono_now - st.violation_since) >= self.sustain_window_s:
                st.in_violation = True
        else:
            st.violation_since = None
            st.in_violation = False

        if st.in_violation and (
            mono_now - st.last_critical_log >= self.critical_log_interval_s
        ):
            st.last_critical_log = mono_now
            logger.critical(
                f"OFFSET JUDGE VIOLATION: {self._key_str(st.source_key)} "
                f"offset={st.ema_offset_ns/1e9:+.3f}s exceeds "
                f"{self.k:.0f}x sigma ({bound_ns/1e9:.4f}s, tier "
                f"{self._best.tier}) sustained >{self.sustain_window_s:.0f}s "
                f"— labels remain CORRECTED (segment {st.segment_id}); "
                f"radiod's advertised epoch is contradicted."
            )

    def _open_segment_locked(
        self,
        st: _SourceState,
        cause: str,
        delta_ns: Optional[float],
        mono_now: float,
    ) -> None:
        st.segment_id += 1
        st.segment_cause = cause
        st.raw_window.clear()
        st.ema_offset_ns = None
        st.history.clear()
        st.violation_since = None
        st.in_violation = False
        st.last_step = {
            "utc": self._time(),
            "cause": cause,
            "delta_ns": delta_ns,
        }
        logger.warning(
            f"OffsetJudge: segment fracture on {self._key_str(st.source_key)} "
            f"-> segment {st.segment_id} (cause={cause}"
            + (f", delta={delta_ns/1e9:+.3f}s" if delta_ns else "")
            + ")"
        )

    # ── publication (spec §3: /run/hf-timestd/offset_judge.json) ─────

    @staticmethod
    def _key_str(source_key: SourceKey) -> str:
        stream, ssrc = source_key
        return f"{stream}/{int(ssrc):08x}"

    def _slope_ppm_locked(self, st: _SourceState) -> Optional[float]:
        """d(offset)/dt within the current segment, in ppm (1 ppm = 1000 ns/s)."""
        if len(st.history) < 4:
            return None
        t = np.array([h[0] for h in st.history])
        off = np.array([h[1] for h in st.history])
        span = t[-1] - t[0]
        if span < 3 * self.tick_seconds:
            return None
        slope_ns_per_s = float(np.polyfit(t - t[0], off, 1)[0])
        return slope_ns_per_s / 1000.0

    def _snapshot_locked(self, mono_now: float) -> Dict:
        best = self._best
        judge_block: Dict = {"tier": None, "sigma_ns": None, "age_s": None}
        if best is not None:
            age = mono_now - best.mono
            tier = best.tier
            sigma_ns = best.sigma_ns
            if age > 2.5 * self.tick_seconds:
                sigma_ns += (age - 2.5 * self.tick_seconds) * \
                    self.holdover_sigma_growth_ns_per_s
                tier = "T1"
            judge_block = {
                "tier": tier,
                "sigma_ns": round(sigma_ns, 1),
                "age_s": round(age, 3),
                "bench_detail": best.detail,
            }
        sources: Dict[str, Dict] = {}
        for key, st in self._sources.items():
            v = None
            if st.ema_offset_ns is not None and best is not None:
                v = self._verdict_locked(st, mono_now)
            sources[self._key_str(key)] = {
                "offset_ns": round(v.offset_ns, 1) if v else None,
                "sigma_ns": round(v.sigma_ns, 1) if v else None,
                "tier": v.tier if v else None,
                "judge_age_s": round(v.judge_age_s, 3) if v else None,
                "segment_id": st.segment_id,
                "segment_cause": st.segment_cause,
                "d_offset_dt_ppm": self._slope_ppm_locked(st),
                "last_step": st.last_step,
                "in_violation": bool(v.in_violation) if v else False,
                "anchor_fault": self._anchor_fault_active_locked(st, mono_now),
                "radiod_gps_time_ns": st.gps_time_ns,
                "radiod_rtp_timesnap": st.rtp_timesnap,
                "sample_rate": st.sample_rate,
            }
        return {
            "schema": "offset-judge-v1",
            "utc_published": datetime.fromtimestamp(
                self._time(), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "k": self.k,
            "judge": judge_block,
            "sources": sources,
        }

    def _publish(self, snapshot: Dict) -> None:
        """Atomic tmp+rename write of offset_judge.json."""
        try:
            self.publish_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.publish_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.publish_path)
            self._publish_error_logged = False
        except OSError as e:
            if not self._publish_error_logged:
                logger.warning(
                    f"OffsetJudge: cannot publish {self.publish_path}: {e} "
                    f"(judge continues; will retry each tick, logging once)"
                )
                self._publish_error_logged = True
