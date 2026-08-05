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
# Rate (frequency) estimation — spec §10 P3 + §11
#
# Doctrine (audit G7): frequency accuracy is MEASURED and RECORDED,
# never corrected into the samples and never folded into the label
# arithmetic — the RTP tick spacing stays the trusted steel ruler.
# 1 ppm of rate disagreement == 1000 ns of offset walk per second.
# ────────────────────────────────────────────────────────────────────

PPM_PER_NS_PER_S = 1.0 / 1000.0   # ns/s → ppm
# Sigma floor used ONLY when inverse-variance combining two estimates
# (a noiseless synthetic regression legitimately reports sigma 0).
RATE_COMBINE_SIGMA_FLOOR_PPM = 1e-4


@dataclass(frozen=True)
class RateEstimate:
    """One rate-disagreement estimate: d(offset)/dt in ppm ± 1-sigma.

    ``source`` is "offset-slope" (judge offset series regression),
    "t6-residual" (BPSK PPS residual-walk differentiation) or
    "combined" (inverse-variance blend of the two).
    """
    ppm: float
    sigma_ppm: float
    n: int
    span_s: float
    source: str


def regress_rate_ppm(
    t_s: np.ndarray, y_ns: np.ndarray
) -> Optional[Tuple[float, float]]:
    """Least-squares slope of y_ns(t_s) in ppm with its 1-sigma error.

    Returns (ppm, sigma_ppm) or None when the series is degenerate
    (<3 points or zero time spread).  sigma is the standard error of
    the fitted slope from the regression residuals — an honest,
    empirical uncertainty (spec §13.1), 0.0 for a noiseless series.
    """
    n = len(t_s)
    if n < 3 or len(y_ns) != n:
        return None
    t0 = np.asarray(t_s, dtype=float)
    t0 = t0 - t0[0]
    y = np.asarray(y_ns, dtype=float)
    sxx = float(np.sum((t0 - t0.mean()) ** 2))
    if sxx <= 0.0:
        return None
    design = np.vstack([t0, np.ones(n)]).T
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    slope_ns_per_s = float(coef[0])
    resid = y - design @ coef
    sse = float(np.sum(resid ** 2))
    stderr_ns_per_s = math.sqrt(max(sse / (n - 2), 0.0) / sxx)
    return (slope_ns_per_s * PPM_PER_NS_PER_S,
            stderr_ns_per_s * PPM_PER_NS_PER_S)


def combine_rate_estimates(
    a: Optional[RateEstimate], b: Optional[RateEstimate]
) -> Optional[RateEstimate]:
    """Inverse-variance blend of two independent rate estimates.

    With both present the result is "combined"; with one, that one is
    passed through unchanged.  Sigmas are floored at
    RATE_COMBINE_SIGMA_FLOOR_PPM for the weights only — the reported
    sigma is the proper combined sigma of the floored weights, so a
    noiseless estimate can never claim infinite weight.
    """
    if a is None:
        return b
    if b is None:
        return a
    wa = 1.0 / max(a.sigma_ppm, RATE_COMBINE_SIGMA_FLOOR_PPM) ** 2
    wb = 1.0 / max(b.sigma_ppm, RATE_COMBINE_SIGMA_FLOOR_PPM) ** 2
    ppm = (wa * a.ppm + wb * b.ppm) / (wa + wb)
    sigma = math.sqrt(1.0 / (wa + wb))
    return RateEstimate(
        ppm=ppm, sigma_ppm=sigma,
        n=a.n + b.n,
        span_s=max(a.span_s, b.span_s),
        source="combined",
    )


class T6ResidualRateEstimator:
    """ADC-clock rate from the T6 PPS residual walk (spec P3, 2nd observable).

    The T6 SHM push site computes, per accepted BPSK PPS edge, the
    sub-second residual of the counter-arithmetic edge UTC against the
    nearest integer second (``local_minus_source_ns`` — see
    core_recorder_v2).  True PPS edges arrive on the exact GPS
    integer-second grid, so if the ADC clock runs fast by r ppm the
    residual walks by r·1000 ns per second.  Differentiating the walk
    (windowed regression over (edge_true_second, residual)) therefore
    measures the ADC clock rate INDEPENDENTLY of the judge's bench
    offset slope — pure counter-vs-GPS-PPS arithmetic, no host clock.

    Fed from the recorder's edge path (``add_edge``, cheap, called at
    ~1 Hz); read by the judge via ``current()`` (thread-safe).  The
    recorder calls ``reset()`` on every native-anchor (re)capture —
    the residual reference frame moves with the anchor.

    Fracture honesty (spec §5 applied to the rate window):
      * residual wrap at the ±0.5 s boundary is unwrapped (a rounding
        artifact, not an event);
      * a per-edge jump beyond STEP_RESET_NS is an anchor/calibrator
        event — the window restarts fresh, no smoothing across it;
      * an edge gap beyond MAX_GAP_S (stream stall / re-lock) likewise
        restarts the window.
    """

    HALF_SECOND_NS = 500_000_000
    # Per-edge walk at 1 ppm is 1000 ns; GPSDO-class clocks walk <<
    # that.  A 50 µs jump between consecutive edges is an event, not
    # drift.
    STEP_RESET_NS = 50_000.0
    MAX_GAP_S = 30.0

    def __init__(
        self,
        min_span_s: float = 120.0,
        min_points: int = 20,
        window_len: int = 900,          # ~15 min at 1 Hz
    ):
        self.min_span_s = float(min_span_s)
        self.min_points = int(min_points)
        self._lock = threading.Lock()
        self._window: Deque[Tuple[float, float]] = deque(maxlen=int(window_len))
        self._last_raw_ns: Optional[float] = None
        self._last_unwrapped_ns: Optional[float] = None
        self._last_t: Optional[float] = None
        self._wrap_offset_ns: float = 0.0
        self.resets: int = 0

    def reset(self, cause: str = "") -> None:
        """Restart the window (anchor recapture / explicit fracture)."""
        with self._lock:
            self._reset_locked()
        if cause:
            logger.info(f"T6ResidualRateEstimator reset ({cause})")

    def _reset_locked(self) -> None:
        self._window.clear()
        self._last_raw_ns = None
        self._last_unwrapped_ns = None
        self._last_t = None
        self._wrap_offset_ns = 0.0
        self.resets += 1

    def add_edge(self, edge_true_utc_s: float, residual_ns: float) -> None:
        """Record one accepted PPS edge.

        ``edge_true_utc_s``  the edge's integer true second (the
                             rounded reference the residual was taken
                             against) — the exact GPS-grid x-axis.
        ``residual_ns``      sub-second residual (counter-arithmetic
                             UTC − nearest integer second), ±0.5e9.
        """
        t = float(edge_true_utc_s)
        raw = float(residual_ns)
        with self._lock:
            if self._last_t is not None and t - self._last_t > self.MAX_GAP_S:
                self._reset_locked()
            if self._last_t is not None and t <= self._last_t:
                return  # duplicate / out-of-order edge — ignore
            # Unwrap the ±0.5 s rounding boundary.
            if self._last_raw_ns is not None:
                d = raw - self._last_raw_ns
                if d > self.HALF_SECOND_NS:
                    self._wrap_offset_ns -= BILLION
                elif d < -self.HALF_SECOND_NS:
                    self._wrap_offset_ns += BILLION
            unwrapped = raw + self._wrap_offset_ns
            if (self._last_unwrapped_ns is not None
                    and abs(unwrapped - self._last_unwrapped_ns)
                    > self.STEP_RESET_NS):
                # Anchor/calibrator event — fresh window (spec §5).
                self._reset_locked()
                unwrapped = raw  # wrap offset cleared with the window
            self._window.append((t, unwrapped))
            self._last_raw_ns = raw
            self._last_unwrapped_ns = unwrapped
            self._last_t = t

    def current(self) -> Optional[RateEstimate]:
        """Current rate estimate, or None below the minimum span."""
        with self._lock:
            pts = list(self._window)
        if len(pts) < self.min_points:
            return None
        t = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        span = float(t[-1] - t[0])
        if span < self.min_span_s:
            return None
        fit = regress_rate_ppm(t, y)
        if fit is None:
            return None
        return RateEstimate(
            ppm=fit[0], sigma_ppm=fit[1],
            n=len(pts), span_s=span, source="t6-residual",
        )


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
                # WHICH stream grounded the pairing (P5 decoupling) —
                # "t6" or "stream:<description>".
                "source": str(getattr(product, "source", "t6")),
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
    # P3 (spec §10): measured rate disagreement — RECORDED, never used
    # to correct labels or samples (spec §11, audit G7).  None until
    # the estimator has the minimum span in the current segment.
    rate_ppm: Optional[float] = None
    rate_sigma_ppm: Optional[float] = None
    rate_source: Optional[str] = None   # "offset-slope"|"t6-residual"|"combined"


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
    # P4 escalation ladder state (spec §9 steps 2-3).
    alert_active: bool = False                  # past alert_after_s
    alert_classification: Optional[str] = None  # radiod-epoch-fault | rate-disagreement
    # P3 rate state — refreshed once per tick, cleared on fracture.
    slope_est: Optional[RateEstimate] = None    # this segment's offset slope
    rate_est: Optional[RateEstimate] = None     # combined (slope ⊕ t6)
    rate_alarm_since: Optional[float] = None    # monotonic
    rate_alarm: bool = False
    last_rate_critical_log: float = 0.0         # monotonic, rate limiting

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
        alert_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
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
        # P4 escalation ladder (spec §9 step 2): a violation (k·sigma
        # offset OR sustained rate alarm) that persists this long emits
        # an alert through the SAME channel the freshness monitor uses
        # (scripts/check-freshness-alert.sh): journald CRITICAL +
        # `logger -t hf-timestd-alert -p user.crit` + optional mail via
        # $TIMESTD_ALERT_EMAIL, gated by a cooldown state file.
        self.alert_after_s = float(cfg.get("alert_after_s", 900.0))
        self.alert_cooldown_s = float(cfg.get("alert_cooldown_s", 3600.0))
        self.alert_state_path = Path(cfg.get(
            "alert_state_path",
            "/var/lib/timestd/state/offset_judge_alert_sent",
        ))
        # P4 escalation ladder step 3 (spec §9.3, decision §13.3):
        # OPT-IN radiod restart REQUEST.  hf-timestd NEVER restarts
        # radiod itself — when enabled, a sustained violation publishes
        # an atomic request artifact that the sigmond-side watchdog
        # (the first and only component empowered to touch radiod) may
        # act on.  DEFAULT FALSE — site policy.  Canonical key
        # `radiod_restart_request`; the spec-§9 name `radiod_restart`
        # is accepted as an alias.
        self.restart_request_after_s = float(
            cfg.get("restart_request_after_s", 3600.0))
        self.radiod_restart_request = bool(cfg.get(
            "radiod_restart_request", cfg.get("radiod_restart", False)))
        self.restart_request_cooldown_s = float(
            cfg.get("restart_request_cooldown_s", 21600.0))   # 6 h
        self.restart_request_path = Path(cfg.get(
            "restart_request_path",
            "/run/hf-timestd/radiod-restart-request.json",
        ))
        # P3 rate loop (spec §10): minimum regression span before a
        # rate is reported at all, and the sustained-|rate| alarm
        # threshold.  A GPSDO-disciplined ADC should sit << 0.1 ppm;
        # 1.0 ppm sustained is a genuine clock fault worth a CRITICAL.
        # Measurement + alarm ONLY — no correction, no resampling,
        # no escalation (P4 owns escalation; spec §11 / audit G7).
        self.rate_min_span_s = float(cfg.get("rate_min_span_s", 120.0))
        self.rate_min_points = int(cfg.get("rate_min_points", 6))
        self.rate_alarm_ppm = float(cfg.get("rate_alarm_ppm", 1.0))
        self.rate_sustain_window_s = float(cfg.get("rate_sustain_window_s", 60.0))
        # Writer-side anchor-fault flags auto-expire after this long
        # without re-assertion.
        self.anchor_fault_hold_s = float(cfg.get("anchor_fault_hold_s", 120.0))
        # Hysteresis: N consecutive polls on a higher tier to advance;
        # degrade immediately (spec §2).
        self.upgrade_polls = int(cfg.get("upgrade_polls", 3))
        # Cross-bench consistency gate (spec §2 as amended by
        # docs/JUDGE-CROSS-BENCH-GATE-2026-08-05.md): a candidate bench
        # is adopted only when its UTC agrees with the trusted lower
        # tier's within cross_bench_k * sqrt(sigma_c^2 + sigma_l^2),
        # sustained over the whole advance window.  Precision claims
        # never substitute for cross-validation (the 2026-08-05 T6
        # displaced-peak incident: a biased-but-stable T6 was adopted
        # over a healthy T5 because its honest wide sigma kept k*sigma
        # quiet).
        self.cross_bench_k = float(cfg.get("cross_bench_k", 5.0))
        # Precision non-regression clause (gate amendment, cont'd): a
        # VOLUNTARY upgrade is additionally refused when the candidate
        # bench's reported sigma is materially worse than the
        # incumbent's — tier rank must never regress the judge's
        # demonstrated precision (spec §13.1: empirical accuracy
        # governs; e.g. a 25 ms-floor T5 adopted over a 200 µs T4
        # would widen the k*sigma violation bound ~100x and stop
        # flagging ms-scale radiod anomalies).  Adoption requires
        # sigma_candidate <= sigma_incumbent * sigma_regression_margin
        # (default 2.0: modest inflation is a fair price for an
        # independence upgrade; order-of-magnitude regressions are
        # refused).  Degrade-on-loss is never sigma-gated — a wide
        # honest bench beats none.
        self.sigma_regression_margin = float(
            cfg.get("sigma_regression_margin", 2.0))

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
        # Cross-bench gate state (gate doc): the currently-blocking
        # conflict {upper, lower, delta_ns, since_utc} (None when no
        # candidate is being refused), the shadow-mode residuals of
        # every non-adopted bench vs the adopted one (refreshed each
        # tick), and the CRITICAL rate limiter.
        self._cross_conflict: Optional[Dict] = None
        self._shadow_residuals: Dict[str, Dict] = {}
        self._last_cross_critical_log: float = 0.0
        # Precision-hold state: {candidate, incumbent,
        # sigma_candidate_ns, sigma_incumbent_ns} while a voluntary
        # upgrade is refused on sigma non-regression (None when clear),
        # plus its own WARNING rate limiter (a hold is a precision
        # policy, not a fault — never CRITICAL).
        self._precision_hold: Optional[Dict] = None
        self._last_precision_warning_log: float = 0.0

        # P4 ladder step 2 (alert) shared state.  The cooldown is
        # channel-global (one alert per cooldown across sources),
        # mirroring check-freshness-alert.sh's single state file.
        self._alert_run = alert_runner or subprocess.run
        self._last_alert_mono: Optional[float] = None
        self._alert_emitted: bool = False               # for clear bookkeeping

        # P4 ladder step 3 state.  One request artifact at a time; the
        # owning source must clear before the artifact is withdrawn.
        # cooldown_until (wall) survives withdrawal — re-request is
        # rate-limited to one per restart_request_cooldown_s.
        self._restart_request_owner: Optional[SourceKey] = None
        self._restart_cooldown_until_wall: float = 0.0
        self._request_adopted: bool = False             # lazy startup adoption
        # After adopting an artifact across a judge restart, hold off
        # withdrawal until the sustain windows have had time to re-arm
        # — otherwise a still-violating source would lose its request
        # on tick 1 (in_violation needs sustain_window_s to re-assert)
        # and the cooldown would block a re-request.
        self._withdraw_grace_until_mono: float = 0.0

        # P3: second, independent rate observable — provider returning
        # the T6 residual-walk RateEstimate (wired by the recorder via
        # set_t6_rate_provider; None until/unless T6 is active).
        self._t6_rate_provider: Optional[Callable[[], Optional[RateEstimate]]] = None
        self._t6_rate: Optional[RateEstimate] = None    # cached each tick

        # P3: GPSDO discipline honesty — measurement metadata ONLY
        # (never gates recording or verdicts).  Reads gpsdo-monitor's
        # /run/gpsdo/*.json when present; "absent" otherwise.
        self._gpsdo_probe = None
        self._gpsdo_discipline: str = "absent"
        self._gpsdo_detail: List[Dict] = []
        if bool(cfg.get("gpsdo_enabled", True)):
            try:
                from .gpsdo_probe import GpsdoProbe
                self._gpsdo_probe = GpsdoProbe(
                    run_dir=Path(cfg.get("gpsdo_run_dir", "/run/gpsdo")),
                    now_fn=time_fn,
                )
            except Exception as e:  # noqa: BLE001 — metadata only, never fatal
                logger.debug(f"OffsetJudge: GpsdoProbe unavailable: {e}")

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

    def set_t6_rate_provider(
        self, provider: Callable[[], Optional[RateEstimate]]
    ) -> None:
        """Wire the T6 residual-walk rate observable (P3).

        ``provider`` is polled once per tick (slow path); it must be
        thread-safe and return the current RateEstimate or None.
        """
        with self._lock:
            self._t6_rate_provider = provider

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
        rate = st.rate_est
        return OffsetVerdict(
            offset_ns=float(st.ema_offset_ns),
            sigma_ns=float(sigma_ns),
            tier=tier,
            judge_age_s=float(age),
            segment_id=st.segment_id,
            in_violation=bool(st.in_violation or anchor_fault),
            rate_ppm=(float(rate.ppm) if rate is not None else None),
            rate_sigma_ppm=(float(rate.sigma_ppm) if rate is not None else None),
            rate_source=(rate.source if rate is not None else None),
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

        # Slow-path pre-polls (outside the lock): the T6 residual rate
        # observable and the GPSDO discipline metadata.
        t6_rate = None
        provider = self._t6_rate_provider
        if provider is not None:
            try:
                t6_rate = provider()
            except Exception as e:  # noqa: BLE001 — observable trouble ≠ judge trouble
                logger.debug(f"OffsetJudge t6 rate provider failed: {e}")
        gpsdo_state, gpsdo_detail = self._poll_gpsdo()

        mono_now = self._mono()
        with self._lock:
            self._t6_rate = t6_rate
            self._gpsdo_discipline = gpsdo_state
            self._gpsdo_detail = gpsdo_detail
            best = self._select_bench_locked(readings)
            if best is not None:
                self._best = best
                for st in self._sources.values():
                    self._measure_source_locked(st, best, mono_now)
            # Shadow-mode residuals: every non-adopted bench vs the
            # adopted one, refreshed each tick (gate doc).
            self._update_shadow_locked(readings, self._best, mono_now)
            # Violation + rate evaluation run every tick even in
            # holdover so sustained windows keep counting / clearing.
            for st in self._sources.values():
                self._refresh_rate_locked(st)
                self._evaluate_violation_locked(st, mono_now)
                self._evaluate_rate_alarm_locked(st, mono_now)
            # P4 escalation ladder (spec §9 steps 2-3): decisions under
            # the lock, I/O (alert channel, request artifact) outside.
            actions = self._escalation_actions_locked(mono_now)
            snapshot = self._snapshot_locked(mono_now)
        self._publish(snapshot)
        self._run_escalation_actions(actions)

    def _poll_gpsdo(self) -> Tuple[str, List[Dict]]:
        """GPSDO discipline state — metadata only, never gating."""
        if self._gpsdo_probe is None:
            return "absent", []
        try:
            return self._gpsdo_probe.discipline()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OffsetJudge gpsdo discipline poll failed: {e}")
            return "absent", []

    def _tier_rank(self, tier: Optional[str]) -> int:
        """TIER_ORDER index (lower = better); unknown tiers rank last."""
        return (
            self.TIER_ORDER.index(tier)
            if tier in self.TIER_ORDER else len(self.TIER_ORDER)
        )

    def _select_bench_locked(
        self, readings: List[BenchReading]
    ) -> Optional[BenchReading]:
        """Highest tier wins, with upgrade hysteresis (spec §2) AND the
        cross-bench consistency gate (JUDGE-CROSS-BENCH-GATE-2026-08-05).

        Immediate degrade: if the active tier stopped answering, take
        the best available lower tier this tick.  Upgrade: a tier
        better than the active one must (a) answer `upgrade_polls`
        consecutive ticks AND (b) agree with the trusted lower tier's
        UTC within cross_bench_k * sqrt(sigma_c^2 + sigma_l^2) on every
        one of those ticks — a gate failure restarts the advance window
        cleanly.  Single-bench sites: no lower tier, gate vacuously
        passes (unchanged behavior).
        """
        if not readings:
            self._candidate_tier = None
            self._candidate_count = 0
            return None
        mono_now = self._mono()
        by_rank = sorted(readings, key=lambda r: self._tier_rank(r.tier))
        best = by_rank[0]
        active_tier = self._best.tier if self._best is not None else None
        active_rank = self._tier_rank(active_tier)
        best_rank = self._tier_rank(best.tier)

        if active_tier is None:
            # Bootstrap: nothing is trusted yet, so trust builds
            # bottom-up within this tick — the lowest-tier reading is
            # vacuously trusted, each higher tier must pass the gate
            # against the trusted reading below it.  This closes the
            # judge-restart hole: a deterministic-biased upper bench
            # (the T6 incident shape) can never be re-adopted at tick 1
            # just because everything answers at once.
            self._candidate_tier = None
            self._candidate_count = 0
            adopted = self._chain_consistent_locked(by_rank, mono_now)
            self._release_holds_for_locked(adopted.tier)
            return adopted
        if best_rank >= active_rank:
            # Same tier or degrade — adopt immediately (spec §2:
            # degrade-on-loss is never gated).  No upgrade candidate
            # is proposed, so any recorded conflict/hold is stale.
            self._candidate_tier = None
            self._candidate_count = 0
            self._clear_advance_holds_locked()
            return best
        # Proposed upgrade — hysteresis + cross-bench gate.
        if self._candidate_tier != best.tier:
            self._candidate_tier = best.tier
            self._candidate_count = 0
        ref = next((r for r in by_rank if r.tier == active_tier), None)
        gate_ref = ref
        if gate_ref is None:
            # Active tier not answering this tick: gate against the
            # highest available tier below the candidate ("T6 must
            # agree with T5 when T5 is live, else with T4, etc.").
            lowers = [r for r in by_rank
                      if self._tier_rank(r.tier) > best_rank]
            gate_ref = lowers[0] if lowers else None
        gate_ok = (gate_ref is None
                   or self._cross_gate_ok_locked(best, gate_ref, mono_now))
        if gate_ok and ref is not None:
            # Precision non-regression: sigma-gate VOLUNTARY upgrades
            # only (the incumbent still answers this tick).  A forced
            # re-selection after incumbent loss is never sigma-gated —
            # better a wide honest bench than none.
            gate_ok = self._sigma_gate_ok_locked(best, ref, mono_now)
        if gate_ok:
            self._candidate_count += 1
            if self._candidate_count >= self.upgrade_polls:
                self._candidate_tier = None
                self._candidate_count = 0
                self._clear_advance_holds_locked()
                return best
        else:
            # Gate failed: the advance window restarts cleanly — the
            # next agreeing poll counts as poll 1.
            self._candidate_count = 0
        # Not confirmed yet: stay on the active tier if it still answers.
        if ref is not None:
            return ref
        # Active tier gone: immediate re-selection among the remaining
        # readings, chain-gated bottom-up — loss of the reference must
        # never become the moment a cross-bench-blocked candidate
        # slips through (sigma non-regression deliberately does NOT
        # apply here: forced re-selection is not a voluntary upgrade).
        self._candidate_tier = None
        self._candidate_count = 0
        adopted = self._chain_consistent_locked(by_rank, mono_now)
        self._release_holds_for_locked(adopted.tier)
        return adopted

    # ── cross-bench consistency gate (gate doc, amends spec §2) ─────

    def _chain_consistent_locked(
        self, by_rank: List[BenchReading], mono_now: float
    ) -> BenchReading:
        """Adopt the highest reading consistent with the chain below it.

        The lowest-tier reading is vacuously trusted; each higher tier
        is trusted only if it passes the gate against the reading
        trusted so far.  A single reading passes vacuously.
        """
        chain = list(reversed(by_rank))     # lowest tier first
        trusted = chain[0]
        for cand in chain[1:]:
            if self._cross_gate_ok_locked(cand, trusted, mono_now):
                trusted = cand
            else:
                break
        return trusted

    def _cross_bench_delta_ns(
        self, cand: BenchReading, ref: BenchReading, mono_now: float
    ) -> float:
        """Candidate-minus-reference UTC disagreement (ns), both
        readings extrapolated to the same monotonic instant."""
        return (cand.utc_at(mono_now) - ref.utc_at(mono_now)) * 1e9

    def _cross_gate_ok_locked(
        self, cand: BenchReading, ref: BenchReading, mono_now: float
    ) -> bool:
        """One gate evaluation: |delta| <= k_x * sqrt(s_c^2 + s_l^2).

        On failure the conflict flag is recorded/refreshed and a
        rate-limited CRITICAL names both tiers and the delta; on
        success any conflict blaming this candidate is cleared (the
        advance window then restarts cleanly at the caller).
        """
        delta_ns = self._cross_bench_delta_ns(cand, ref, mono_now)
        bound_ns = self.cross_bench_k * math.sqrt(
            cand.sigma_ns ** 2 + ref.sigma_ns ** 2
        )
        if abs(delta_ns) <= bound_ns:
            if (self._cross_conflict is not None
                    and self._cross_conflict.get("upper") == cand.tier):
                logger.warning(
                    f"OffsetJudge cross-bench gate: candidate {cand.tier} "
                    f"agrees with {ref.tier} again "
                    f"(delta={delta_ns/1e6:+.3f}ms within "
                    f"{bound_ns/1e6:.3f}ms) — advance window restarts"
                )
                self._cross_conflict = None
            return True
        conflict = self._cross_conflict
        if (conflict is None
                or conflict.get("upper") != cand.tier
                or conflict.get("lower") != ref.tier):
            conflict = {
                "upper": cand.tier,
                "lower": ref.tier,
                "delta_ns": round(delta_ns, 1),
                "since_utc": self._iso_utc(self._time()),
            }
        else:
            conflict = dict(conflict)
        conflict["delta_ns"] = round(delta_ns, 1)
        self._cross_conflict = conflict
        if (mono_now - self._last_cross_critical_log
                >= self.critical_log_interval_s):
            self._last_cross_critical_log = mono_now
            logger.critical(
                f"OFFSET JUDGE CROSS-BENCH CONFLICT: candidate {cand.tier} "
                f"disagrees with trusted {ref.tier} by "
                f"{delta_ns/1e6:+.3f} ms (delta_ns={delta_ns:+.0f}), bound "
                f"k_x*sqrt(sigma_c^2+sigma_l^2) = {self.cross_bench_k:.1f} x "
                f"{math.sqrt(cand.sigma_ns**2 + ref.sigma_ns**2)/1e6:.3f} ms "
                f"= {bound_ns/1e6:.3f} ms — advancement BLOCKED, judging "
                f"stays on {ref.tier}; the rejected bench stays under "
                f"shadow measurement (shadow_residuals in offset_judge.json)."
            )
        return False

    def _sigma_gate_ok_locked(
        self, cand: BenchReading, ref: BenchReading, mono_now: float
    ) -> bool:
        """Precision non-regression check for a VOLUNTARY upgrade.

        Adoption over a still-answering incumbent additionally requires
        sigma_candidate <= sigma_incumbent * sigma_regression_margin —
        a higher tier must never materially widen the k*sigma violation
        bound (spec §13.1: empirical accuracy governs).  On refusal a
        precision_hold flag is published and a rate-limited WARNING
        (not CRITICAL — this is a precision policy, not a fault) names
        both benches; the refused candidate keeps being measured via
        shadow_residuals.
        """
        margin = self.sigma_regression_margin
        if cand.sigma_ns <= ref.sigma_ns * margin:
            if (self._precision_hold is not None
                    and self._precision_hold.get("candidate") == cand.tier):
                logger.info(
                    f"OffsetJudge precision hold released: candidate "
                    f"{cand.tier} sigma {cand.sigma_ns/1e6:.3f} ms now "
                    f"within {margin:.1f}x of incumbent {ref.tier} sigma "
                    f"{ref.sigma_ns/1e6:.3f} ms"
                )
                self._precision_hold = None
            return True
        self._precision_hold = {
            "candidate": cand.tier,
            "incumbent": ref.tier,
            "sigma_candidate_ns": round(cand.sigma_ns, 1),
            "sigma_incumbent_ns": round(ref.sigma_ns, 1),
        }
        if (mono_now - self._last_precision_warning_log
                >= self.critical_log_interval_s):
            self._last_precision_warning_log = mono_now
            logger.warning(
                f"OFFSET JUDGE PRECISION HOLD: candidate {cand.tier} sigma "
                f"{cand.sigma_ns/1e6:.3f} ms is worse than incumbent "
                f"{ref.tier} sigma {ref.sigma_ns/1e6:.3f} ms x margin "
                f"{margin:.1f} — voluntary upgrade refused (precision "
                f"non-regression: tier rank must not widen the k*sigma "
                f"violation bound); judging stays on {ref.tier}, candidate "
                f"stays under shadow measurement."
            )
        return False

    def _clear_advance_holds_locked(self) -> None:
        """No upgrade candidate proposed / candidate adopted: any
        recorded cross-bench conflict or precision hold is stale."""
        self._cross_conflict = None
        self._precision_hold = None

    def _release_holds_for_locked(self, adopted_tier: str) -> None:
        """A chain selection adopted `adopted_tier`: flags blaming that
        tier are moot (others may still describe live refusals)."""
        if (self._cross_conflict is not None
                and self._cross_conflict.get("upper") == adopted_tier):
            self._cross_conflict = None
        if (self._precision_hold is not None
                and self._precision_hold.get("candidate") == adopted_tier):
            self._precision_hold = None

    def _update_shadow_locked(
        self,
        readings: List[BenchReading],
        adopted: Optional[BenchReading],
        mono_now: float,
    ) -> None:
        """Shadow-mode measurement channel (gate doc): every polled
        bench that is NOT the adopted one publishes its residual vs the
        adopted bench each tick — a rejected bench's disagreement trend
        is a first-class diagnostic (it measures the displaced peak
        directly)."""
        shadows: Dict[str, Dict] = {}
        if adopted is not None:
            for r in readings:
                if r.tier == adopted.tier:
                    continue
                shadows[r.tier] = {
                    "shadow_residual_ns": round(
                        self._cross_bench_delta_ns(r, adopted, mono_now), 1
                    ),
                    "sigma_ns": round(r.sigma_ns, 1),
                    "vs_tier": adopted.tier,
                }
        self._shadow_residuals = shadows

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

        # Rate history takes the MEDIAN (outlier-rejected but unlagged)
        # rather than the EMA: the EMA's convergence transient would
        # bias a windowed slope; a constant median lag cannot.
        st.history.append((mono_now, med_ns))

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

    def _refresh_rate_locked(self, st: _SourceState) -> None:
        """Recompute the per-source rate estimates (once per tick).

        Two independent observables, cross-checked by combination:
        the segment's offset-series slope (radiod-clock-vs-bench rate
        disagreement) and, when the recorder supplies it, the T6 PPS
        residual-walk rate.  MEASURED AND RECORDED ONLY — the result
        never feeds back into labels or samples (spec §11, audit G7).
        """
        st.slope_est = self._offset_slope_estimate_locked(st)
        st.rate_est = combine_rate_estimates(st.slope_est, self._t6_rate)

    def _offset_slope_estimate_locked(
        self, st: _SourceState
    ) -> Optional[RateEstimate]:
        """Windowed regression over this segment's offset history.

        None until the segment has rate_min_points measurements over
        at least rate_min_span_s (a fracture clears the history, so
        the estimator restarts with the segment — spec §5)."""
        if len(st.history) < max(self.rate_min_points, 3):
            return None
        t = np.array([h[0] for h in st.history])
        off = np.array([h[1] for h in st.history])
        span = float(t[-1] - t[0])
        if span < self.rate_min_span_s:
            return None
        fit = regress_rate_ppm(t, off)
        if fit is None:
            return None
        return RateEstimate(
            ppm=fit[0], sigma_ppm=fit[1],
            n=len(st.history), span_s=span, source="offset-slope",
        )

    def _evaluate_rate_alarm_locked(self, st: _SourceState, mono_now: float) -> None:
        """Sustained-|rate| CRITICAL (P3 §10) — alarm only, no action.

        A GPSDO-disciplined ADC should sit far below 0.1 ppm; a
        sustained excess of rate_alarm_ppm names the channel and BOTH
        independent estimates.  Escalation beyond logging is P4's."""
        rate = st.rate_est
        if rate is None or abs(rate.ppm) <= self.rate_alarm_ppm:
            st.rate_alarm_since = None
            st.rate_alarm = False
            return
        if st.rate_alarm_since is None:
            st.rate_alarm_since = mono_now
        if (mono_now - st.rate_alarm_since) < self.rate_sustain_window_s:
            return
        st.rate_alarm = True
        if (mono_now - st.last_rate_critical_log
                < self.critical_log_interval_s):
            return
        st.last_rate_critical_log = mono_now

        def _fmt(est: Optional[RateEstimate]) -> str:
            if est is None:
                return "unavailable"
            return (f"{est.ppm:+.3f}±{est.sigma_ppm:.3f} ppm "
                    f"(n={est.n}, span={est.span_s:.0f}s)")

        logger.critical(
            f"OFFSET JUDGE RATE VIOLATION: {self._key_str(st.source_key)} "
            f"rate={rate.ppm:+.3f} ppm ({rate.source}) exceeds "
            f"±{self.rate_alarm_ppm:.2f} ppm sustained "
            f">{self.rate_sustain_window_s:.0f}s — "
            f"offset-slope: {_fmt(st.slope_est)}; "
            f"t6-residual: {_fmt(self._t6_rate)}. "
            f"Rate is RECORDED, never corrected into samples "
            f"(spec §11); segment {st.segment_id}."
        )

    # ── P4 escalation ladder (spec §9 steps 2-3) ─────────────────────

    def _escalation_sustained_locked(
        self, st: _SourceState, mono_now: float
    ) -> Optional[float]:
        """Seconds this source's violation condition has been sustained.

        The ladder condition is `in_violation OR rate_alarm`; duration
        is measured from the underlying condition's onset (the same
        clock §9's 60 s / 15 min / 60 min rungs count on), so the 60 s
        sustain that armed the flag is included.  None when clear.
        """
        sust: Optional[float] = None
        if st.in_violation and st.violation_since is not None:
            sust = mono_now - st.violation_since
        if st.rate_alarm and st.rate_alarm_since is not None:
            r = mono_now - st.rate_alarm_since
            sust = r if sust is None else max(sust, r)
        return sust

    def _classification_locked(self, st: _SourceState) -> str:
        """Spec §9 step 2: name the likely cause.

        A significant measured rate (the offset is *sloping*) points at
        clock-rate disagreement; otherwise a constant lag points at a
        wrong radiod epoch (anchor fault — the B4 incident shape).
        """
        rate = st.rate_est
        if st.rate_alarm or (
            rate is not None and abs(rate.ppm) > self.rate_alarm_ppm
        ):
            return "rate-disagreement"
        return "radiod-epoch-fault"

    def _escalation_actions_locked(self, mono_now: float) -> List[Tuple]:
        """Evaluate ladder steps 2-3 for every source; emit actions.

        Mutates per-source escalation state under the lock and returns
        the I/O actions to run after release: ("alert", subject, body)
        and ("clear_alert_state",).
        """
        actions: List[Tuple] = []
        if not self._request_adopted:
            self._request_adopted = True
            actions.extend(self._adopt_existing_request_locked())
        wall = self._time()
        any_active = False
        for key, st in self._sources.items():
            sust = self._escalation_sustained_locked(st, mono_now)
            if sust is None:
                st.alert_active = False
                st.alert_classification = None
                continue
            any_active = True
            st.alert_classification = self._classification_locked(st)
            if sust < self.alert_after_s:
                st.alert_active = False
                continue
            # Step 2 rung reached.
            st.alert_active = True
            if self._alert_gate_ok_locked(mono_now):
                self._last_alert_mono = mono_now
                self._alert_emitted = True
                subject, body = self._compose_alert_locked(st, sust, mono_now)
                actions.append(("alert", subject, body))
            # Step 3 rung: opt-in restart REQUEST (spec §9.3/§13.3).
            if (self.radiod_restart_request
                    and sust >= self.restart_request_after_s
                    and self._restart_request_owner is None
                    and wall >= self._restart_cooldown_until_wall):
                self._restart_request_owner = key
                self._restart_cooldown_until_wall = (
                    wall + self.restart_request_cooldown_s)
                actions.append((
                    "write_request",
                    self._restart_request_payload_locked(st, sust, wall),
                ))
        # Withdraw when the owning source has cleared (or vanished);
        # an adopted request gets a re-arm grace first.
        owner = self._restart_request_owner
        if owner is not None and mono_now >= self._withdraw_grace_until_mono:
            owner_st = self._sources.get(owner)
            if (owner_st is None
                    or self._escalation_sustained_locked(owner_st, mono_now)
                    is None):
                self._restart_request_owner = None
                actions.append(("withdraw_request", self._key_str(owner)))
        if not any_active and self._alert_emitted:
            # All sources clear: reset the channel (mirror of
            # check-freshness-alert.sh clear_alert) so the next
            # violation alerts without inherited cooldown.
            self._alert_emitted = False
            self._last_alert_mono = None
            actions.append(("clear_alert_state",))
        return actions

    def _alert_gate_ok_locked(self, mono_now: float) -> bool:
        """Cooldown gate: in-process timer + shared state-file mtime."""
        if (self._last_alert_mono is not None
                and (mono_now - self._last_alert_mono) < self.alert_cooldown_s):
            return False
        try:
            mtime = self.alert_state_path.stat().st_mtime
            if 0.0 <= (self._time() - mtime) < self.alert_cooldown_s:
                return False
        except OSError:
            pass
        return True

    def _compose_alert_locked(
        self, st: _SourceState, sust: float, mono_now: float
    ) -> Tuple[str, str]:
        """Actionable alert text: source, offset, bound, tier, rate,
        duration, classification (spec §9 step 2)."""
        key_s = self._key_str(st.source_key)
        cls = st.alert_classification or "radiod-epoch-fault"
        best = self._best
        tier = best.tier if best is not None else "T0"
        sigma_ns = max(best.sigma_ns, 1.0) if best is not None else float("nan")
        bound_ms = self.k * sigma_ns / 1e6
        off_ms = (st.ema_offset_ns or 0.0) / 1e6
        rate = st.rate_est
        rate_s = (
            f"{rate.ppm:+.3f}±{rate.sigma_ppm:.3f} ppm ({rate.source}, "
            f"n={rate.n}, span={rate.span_s:.0f}s)"
            if rate is not None else "unavailable"
        )
        if cls == "rate-disagreement":
            explain = (
                "offset is SLOPING: radiod/ADC clock rate disagrees with the "
                "judge bench.  Check GPSDO discipline and the ADC clock — a "
                "radiod restart will NOT fix a rate fault."
            )
        else:
            explain = (
                "offset is a CONSTANT LAG: radiod's advertised epoch is wrong "
                "(anchor/epoch fault, the B4-incident shape).  Labels remain "
                "judge-corrected; restarting radiod clears the offset "
                "(cosmetic hygiene, spec §1)."
            )
        subject = (
            f"offset-judge violation sustained {sust/60.0:.0f} min on {key_s}"
        )
        body = (
            f"Source:         {key_s} (radiod {self._radiod_id(st.source_key[0])})\n"
            f"Classification: {cls} — {explain}\n"
            f"Offset:         {off_ms:+.3f} ms; bound k*sigma = "
            f"{self.k:.0f} x {sigma_ns/1e6:.4f} ms = {bound_ms:.4f} ms "
            f"(tier {tier})\n"
            f"Rate:           {rate_s}\n"
            f"Sustained:      {sust:.0f} s (segment {st.segment_id}, "
            f"in_violation={st.in_violation}, rate_alarm={st.rate_alarm})\n"
            f"Data labels remain corrected by the judge (spec §1); this alert "
            f"is operator awareness, spec §9 step 2.\n"
            f"Check: journalctl -u timestd-core-recorder | grep 'OFFSET JUDGE'; "
            f"cat /run/hf-timestd/offset_judge.json"
        )
        return subject, body

    def _iso_utc(self, unix_s: float) -> str:
        return datetime.fromtimestamp(unix_s, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _restart_request_payload_locked(
        self, st: _SourceState, sust: float, wall: float
    ) -> Dict:
        """radiod-restart-request-v1 artifact body (spec §9.3/§13.3)."""
        best = self._best
        return {
            "schema": "radiod-restart-request-v1",
            "requested_utc": self._iso_utc(wall),
            "source_key": self._key_str(st.source_key),
            "radiod_id": self._radiod_id(st.source_key[0]),
            "offset_ms": round((st.ema_offset_ns or 0.0) / 1e6, 3),
            "sustained_s": round(sust, 1),
            "evidence": {
                "tier": best.tier if best is not None else None,
                "sigma_ns": (
                    round(best.sigma_ns, 1) if best is not None else None
                ),
                "rate_ppm": (
                    round(st.rate_est.ppm, 4)
                    if st.rate_est is not None else None
                ),
                "classification": (
                    st.alert_classification or self._classification_locked(st)
                ),
            },
            "cooldown_until": self._iso_utc(self._restart_cooldown_until_wall),
        }

    def _adopt_existing_request_locked(self) -> List[Tuple]:
        """First-tick adoption of a pre-existing request artifact.

        A judge restart must neither double-request (cooldown is
        re-adopted from the artifact) nor orphan a stale request
        (an unparseable artifact, or one present while the feature is
        disabled, is withdrawn).  Read-only file I/O; withdraw is
        returned as an action.
        """
        try:
            with self.restart_request_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return [("withdraw_request", "unreadable artifact at startup")]
        if data.get("schema") != "radiod-restart-request-v1":
            return [("withdraw_request", "unknown schema at startup")]
        if not self.radiod_restart_request:
            return [("withdraw_request",
                     "radiod_restart_request disabled by config")]
        try:
            stream, _, ssrc_hex = str(data["source_key"]).rpartition("/")
            self._restart_request_owner = (stream, int(ssrc_hex, 16))
        except (KeyError, ValueError):
            return [("withdraw_request", "unparseable source_key at startup")]
        try:
            cd = datetime.strptime(
                str(data.get("cooldown_until", "")), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
            self._restart_cooldown_until_wall = float(cd)
        except ValueError:
            self._restart_cooldown_until_wall = (
                self._time() + self.restart_request_cooldown_s)
        self._withdraw_grace_until_mono = (
            self._mono() + self.sustain_window_s + 3.0 * self.tick_seconds)
        logger.info(
            f"OffsetJudge: adopted existing restart request for "
            f"{data.get('source_key')} (cooldown_until "
            f"{data.get('cooldown_until')})"
        )
        return []

    def _run_escalation_actions(self, actions: List[Tuple]) -> None:
        """Execute ladder I/O actions outside the lock (never fatal)."""
        for act in actions:
            kind = act[0]
            try:
                if kind == "alert":
                    self._emit_alert(act[1], act[2])
                elif kind == "clear_alert_state":
                    self._clear_alert_state()
                elif kind == "write_request":
                    self._write_restart_request(act[1])
                elif kind == "withdraw_request":
                    self._withdraw_restart_request(act[1])
            except Exception as e:  # noqa: BLE001 — escalation trouble ≠ judge trouble
                logger.error(
                    f"OffsetJudge escalation action {kind} failed: {e}"
                )

    def _write_restart_request(self, payload: Dict) -> None:
        """Atomic tmp+rename publication of the restart request."""
        self.restart_request_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.restart_request_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.restart_request_path)
        logger.critical(
            f"RADIOD RESTART REQUESTED (opt-in, spec §9.3): "
            f"{payload['source_key']} radiod_id={payload['radiod_id']} "
            f"offset={payload['offset_ms']:+.3f}ms sustained "
            f"{payload['sustained_s']:.0f}s "
            f"classification={payload['evidence']['classification']} — "
            f"request artifact {self.restart_request_path}; hf-timestd "
            f"NEVER restarts radiod itself, the sigmond watchdog decides. "
            f"Next request possible after {payload['cooldown_until']}."
        )

    def _withdraw_restart_request(self, why: str) -> None:
        try:
            self.restart_request_path.unlink()
        except FileNotFoundError:
            return
        logger.critical(
            f"RADIOD RESTART REQUEST WITHDRAWN ({why}) — removed "
            f"{self.restart_request_path}"
        )

    def _emit_alert(self, subject: str, body: str) -> None:
        """Send one alert through the freshness-alert channel.

        Mirrors scripts/check-freshness-alert.sh send_alert():
        journald CRITICAL (this process logs to journald), the
        system journal via `logger -t hf-timestd-alert -p user.crit`,
        optional mail to $TIMESTD_ALERT_EMAIL, and the cooldown state
        file touched on emit.
        """
        logger.critical(f"OFFSET JUDGE ALERT: {subject}\n{body}")
        try:
            self.alert_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.alert_state_path.touch()
        except OSError as e:
            logger.warning(
                f"OffsetJudge: cannot touch alert state file "
                f"{self.alert_state_path}: {e}"
            )
        try:
            self._alert_run(
                ["logger", "-t", "hf-timestd-alert", "-p", "user.crit",
                 f"{subject}: {body}"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OffsetJudge: logger(1) emit failed: {e}")
        email = (os.environ.get("TIMESTD_ALERT_EMAIL") or "").strip()
        if email:
            try:
                self._alert_run(
                    ["mail", "-s", f"[hf-timestd] {subject}", email],
                    input=body, capture_output=True, text=True,
                    timeout=30, check=False,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"OffsetJudge: mail alert to {email} failed: {e}"
                )

    def _clear_alert_state(self) -> None:
        try:
            self.alert_state_path.unlink()
            logger.info(
                "OffsetJudge: alert cleared — violation condition resolved "
                "on all sources"
            )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug(f"OffsetJudge: alert state clear failed: {e}")

    @staticmethod
    def _radiod_id(stream: str) -> str:
        """radiod identity from the source key's status-stream name
        (strip any :port and a trailing `.local`)."""
        s = str(stream or "").strip().split(":")[0]
        if s.endswith(".local"):
            s = s[: -len(".local")]
        return s

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
        # Rate state restarts with the segment (spec §5 — never
        # regress across a fracture).
        st.slope_est = None
        st.rate_est = None
        st.rate_alarm_since = None
        st.rate_alarm = False
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
        contract_sources: Dict[str, Dict] = {}
        for key, st in self._sources.items():
            v = None
            if st.ema_offset_ns is not None and best is not None:
                v = self._verdict_locked(st, mono_now)
            slope, rate = st.slope_est, st.rate_est
            sust = self._escalation_sustained_locked(st, mono_now)
            # §18 subscriber surface (client contract v0.7) — field
            # names verbatim per CLAUDE.md / ARCHITECTURE-FIRST-
            # PRINCIPLES.md.  utc_anchor_ns is the JUDGE-corrected UTC
            # of rtp_anchor_sample; null (with tier/sigma) until the
            # judge has a verdict, so a subscriber can never mistake a
            # raw radiod mapping for a judged one.
            contract_sources[self._key_str(key)] = {
                "utc_anchor_ns": (
                    int(round(st.gps_unix * 1e9 + v.offset_ns))
                    if v is not None else None
                ),
                "tier": v.tier if v is not None else None,
                "sigma_ns": round(v.sigma_ns, 1) if v is not None else None,
                "snapshot_age_s": (
                    round(v.judge_age_s, 3) if v is not None else None
                ),
                "rtp_anchor_sample": st.rtp_timesnap,
                "rate_samples_per_utc_sec": st.sample_rate,
                "radiod_id": self._radiod_id(key[0]),
                "host_monotonic_at_anchor": st.mono_at_pair,
                "offset_ns": (
                    round(v.offset_ns, 1) if v is not None else None
                ),
                "rate_ppm": (
                    round(v.rate_ppm, 4)
                    if v is not None and v.rate_ppm is not None else None
                ),
                "segment_id": st.segment_id,
            }
            sources[self._key_str(key)] = {
                "offset_ns": round(v.offset_ns, 1) if v else None,
                "sigma_ns": round(v.sigma_ns, 1) if v else None,
                "tier": v.tier if v else None,
                "judge_age_s": round(v.judge_age_s, 3) if v else None,
                "segment_id": st.segment_id,
                "segment_cause": st.segment_cause,
                "d_offset_dt_ppm": (
                    round(slope.ppm, 4) if slope is not None else None
                ),
                # P3 rate record (spec §10) — measured, never applied.
                "rate_ppm": round(rate.ppm, 4) if rate is not None else None,
                "rate_sigma_ppm": (
                    round(rate.sigma_ppm, 4) if rate is not None else None
                ),
                "rate_source": rate.source if rate is not None else None,
                "rate_alarm": bool(st.rate_alarm),
                "last_step": st.last_step,
                "in_violation": bool(v.in_violation) if v else False,
                "anchor_fault": self._anchor_fault_active_locked(st, mono_now),
                # P4 ladder state (spec §9 steps 2-3) for smd status /
                # dashboard rendering.
                "escalation": {
                    "sustained_s": round(sust, 1) if sust is not None else None,
                    "alert_active": bool(st.alert_active),
                    "classification": st.alert_classification,
                    "restart_requested": key == self._restart_request_owner,
                },
                "radiod_gps_time_ns": st.gps_time_ns,
                "radiod_rtp_timesnap": st.rtp_timesnap,
                "sample_rate": st.sample_rate,
            }
        t6r = self._t6_rate
        return {
            "schema": "offset-judge-v1",
            "utc_published": datetime.fromtimestamp(
                self._time(), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "k": self.k,
            "judge": judge_block,
            # P3: GPSDO discipline honesty — measurement metadata only
            # (locked | holdover | unlocked | absent); never gates
            # recording or verdicts.
            "gpsdo_discipline": self._gpsdo_discipline,
            "gpsdo_detail": self._gpsdo_detail,
            # Cross-bench consistency gate (spec §2 amended by
            # JUDGE-CROSS-BENCH-GATE-2026-08-05): the currently-blocked
            # upper bench (None when no candidate is refused) and the
            # per-tick shadow residual of every non-adopted bench vs
            # the adopted one.
            "cross_bench_conflict": self._cross_conflict,
            # Precision non-regression clause: a voluntary upgrade
            # currently refused because the candidate's sigma would
            # materially regress the judge's precision (None when
            # clear).  The refused bench stays in shadow_residuals.
            "precision_hold": self._precision_hold,
            "shadow_residuals": self._shadow_residuals,
            # P3: the T6 residual-walk rate observable, host-global
            # (one ADC clock behind every source on this radiod).
            "t6_residual_rate": (
                {
                    "ppm": round(t6r.ppm, 4),
                    "sigma_ppm": round(t6r.sigma_ppm, 4),
                    "n": t6r.n,
                    "span_s": round(t6r.span_s, 1),
                }
                if t6r is not None else None
            ),
            # P4 ladder summary (spec §9): step-2/3 policy + the current
            # restart-request artifact state (request only — hf-timestd
            # never restarts radiod; the sigmond watchdog decides).
            "escalation": {
                "alert_after_s": self.alert_after_s,
                "restart_request_enabled": self.radiod_restart_request,
                "restart_request_after_s": self.restart_request_after_s,
                "restart_request": {
                    "active": self._restart_request_owner is not None,
                    "source": (
                        self._key_str(self._restart_request_owner)
                        if self._restart_request_owner is not None else None
                    ),
                    "path": str(self.restart_request_path),
                    "cooldown_until": (
                        self._iso_utc(self._restart_cooldown_until_wall)
                        if self._restart_cooldown_until_wall > 0 else None
                    ),
                },
            },
            # Client-contract v0.7 §18 timing-authority SUBSCRIBER
            # SURFACE.  This is the versioned export the sigmond
            # recorders (psk/wspr/meteor/mag, P4b) consume — the §18
            # producer surface was previously documentation-only
            # (CLAUDE.md + docs/ARCHITECTURE-FIRST-PRINCIPLES.md); it
            # is produced here, per source, with the contract's field
            # names verbatim.  STABLE AND VERSIONED by this key name:
            # additive changes only; never rename or repurpose fields.
            "contract_v07": {
                "_doc": (
                    "Client-contract v0.7 §18 timing-authority subscriber "
                    "surface: utc_anchor_ns is the judge-corrected UTC of "
                    "rtp_anchor_sample (null until a verdict exists); "
                    "rate_samples_per_utc_sec is the trusted nominal RTP "
                    "rate (spec §11: measured rate_ppm is recorded "
                    "alongside, never applied).  Additive advice only — "
                    "subscribers keep their own dt-guards (spec §13.2).  "
                    "cross_bench_conflict (additive, gate amendment "
                    "2026-08-05) names an upper bench currently refused "
                    "adoption by cross-bench disagreement; null when clear."
                ),
                "cross_bench_conflict": self._cross_conflict,
                "sources": contract_sources,
            },
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
