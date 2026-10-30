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
  * T3 — HfAcquiredBench (the tick registration), registered by the
    recorder.  FusionBench (the fused D_clock) left the defaults on
    2026-10-22 (review section 15, step 5): it is calibrated against the
    host and the judge must never adopt it.
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
from .chain_delay_store import ChainDelayRecord, ChainDelayStore
from .chrony_stats import parse_tracking
from .leap_second import gps_leap_seconds_at_gps_time
# The delay-model accuracy bound HfAcquiredBench floors its published
# sigma at.  registration_acquirer pulls in tick_edge_detector and
# counter_epoch_tracker, neither of which imports this module, so the
# import is safe at module scope (registration_store stays lazy because
# constructing a default store touches the filesystem path).
from .registration_acquirer import ORIGIN_SIGMA_FLOOR_MS

logger = logging.getLogger(__name__)

PUBLISH_SCHEMA = "offset-judge-v1"
# The additive block spec §11.2 (2026-09-07) publishes for the FUSE feed.
LABEL_PLANE_ANCHOR_KEY = "label_plane_anchor"
# The same statement, published under a key NO chrony path reads, while
# the anchor closure is off (task 17a/17c).  The sign contradiction that
# steered AC0G-ND on 2026-09-07 -- the anchor calling the host slow while
# four NTP witnesses called it fast -- has to stay watchable, and the
# only honest way to watch it is to publish the number and act on
# nothing.  ``label_plane_chrony_sample`` reads
# ``LABEL_PLANE_ANCHOR_KEY`` by default, so a caller can only reach the
# witness by naming it.
LABEL_PLANE_WITNESS_KEY = "label_plane_witness"
# How old the anchor's arrival may be before the fusion process refuses
# it.  One judge tick (10 s) + one arrival window (5 s) + one fusion
# cycle (8 s) with margin: past this the pairing has been projected on
# the monotonic clock long enough for the host's own frequency error to
# matter, and a fresh sample is due anyway.
LABEL_ANCHOR_MAX_AGE_S = 30.0

# The station's stored T6 chain delay (spec
# docs/superpowers/specs/2026-10-27-t6-chain-delay-learning-design.md §2).
# The judge reads this name at construction time, not as a default
# argument, so the test suite can point it at a temporary directory.
DEFAULT_CHAIN_DELAY_PATH = "/var/lib/timestd/state/t6_chain_delay.json"
# Gross-error bound on |T6 − lower tier| before any plane term (spec §6).
# B4 and ND sit at 16 ms; 40 ms leaves room for a slower host without
# admitting a wrong second or a 100 ms sidelobe.
DEFAULT_CHAIN_DELAY_MAX_NS = 40_000_000.0
# The span a learn must cover before its value becomes the record (spec
# §3): two hours, so load-driven wander shows up in the spread.
DEFAULT_CHAIN_DELAY_LEARN_S = 7200.0
# A T6 observation farther than this from the window median (or 4 x the
# raw spread) is a wild reading, not part of the chain delay.
DEFAULT_CHAIN_DELAY_OUTLIER_NS = 5_000_000.0
# The widest spread a learn may write (record or candidate).  The band
# grows with the spread, so a learn that straddled a step (6 ms gives a
# spread near 4 ms and a band near 20 ms) would nearly disable both the
# gate and the change alarm.  B4's day of data spreads 1.1 ms.
DEFAULT_CHAIN_DELAY_MAX_SPREAD_NS = 2_500_000.0
# The live T6 witness: the same 600 s window the label plane has always
# used, read by the change alarm.
T6_WITNESS_WINDOW_S = 600.0
# A healthy T6 (spec §3): AUTHORITATIVE this long, and no coarse re-home
# for this long, before its readings may feed the learner or the witness.
T6_HEALTHY_SETTLE_S = 600.0
# While no record exists, a refused T6 is the expected state of a station
# still learning, so its CRITICAL conflict line repeats only this often.
CHAIN_LEARNING_CONFLICT_LOG_INTERVAL_S = 3600.0
# How long the live T6 witness must sit outside the stored record's band
# before the change alarm fires, and inside it again before the alarm
# clears (spec §5).
DEFAULT_CHAIN_DELAY_CHANGE_SUSTAIN_S = 600.0
# A standing change alarm repeats its CRITICAL line this often.
CHAIN_CHANGE_LOG_INTERVAL_S = 3600.0

GPS_EPOCH_UNIX = 315964800
BILLION = 1_000_000_000
RTP_WRAP = 0x100000000

# Cap on any bench's sigma contribution to the cross-bench bound
# k*sqrt(sigma_c^2 + sigma_l^2).
#
# The gate arrived after the 2026-08-05 displaced-peak incident, in which
# "a biased-but-stable T6 was adopted over a healthy T5 because its
# honest wide sigma kept k*sigma quiet" -- and then reproduced that same
# polarity one level up: the bound grows with the candidate's own
# reported uncertainty, so a bench that has gone WRONG (which usually
# inflates sigma) buys itself a WIDER licence.  On DASI-009.AI6VN,
# 2026-09-18, a sigma of 477 ms opened the bound to ~2.4 s and a 284 ms
# disagreement passed unremarked.
#
# authority_manager met the mirror image and FLOORED its pair thresholds
# so a noisy witness could not mask a real disagreement.  This is the
# opposite bound: a ceiling, so a noisy candidate cannot excuse its own
# error.
#
# ⛔ The ceiling is PER TIER, and it has to be.  A single 5.0 ms number
# looked right while the only case in view was a broken T6, but the gate
# is tier-agnostic: it caps whatever two benches it is handed.  A station
# with no T6 at all still meets it.  Measured consequence of a flat 5 ms
# on a T3-fusion station whose reference bench is chrony --
# ChronyBench reports root_dispersion + root_delay/2 + |rms_offset|,
# routinely well past 5 ms:
#
#     candidate σ   ref σ    bound before   bound after   tightening
#     T3   4.3 ms   20.0 ms      102.3 ms       33.0 ms        3.1x
#     T3   4.3 ms   10.0 ms       54.4 ms       33.0 ms        1.7x
#
# A fusion offset disagreeing with chrony by 50 ms was adopted before and
# refused after — on a station whose fusion is quorum-starved by
# construction and whose host clock has run 150 ms out.  The cap would
# have traded one station's health for another's.
#
# So each bench is capped at what ITS OWN tier can physically deliver,
# taken from the (A1, T) and (A0, T) uncertainty columns of METROLOGY.md
# §4.5 with margin.  A tier reporting inside its budget keeps every bit
# of the room it is entitled to; only a bench claiming an uncertainty its
# tier cannot produce gets trimmed — which is the same principle
# T6_ACCEPTANCE_CRITERIA.md §4.3 states for the battery's criterion 5.
#
# Worked against the two cases that matter:
#   AI6VN's T6 at 477 ms vs a 2 ms reference -> T6 capped to 5 ms, bound
#     26.9 ms, the 284 ms disagreement REFUSED (was 2385 ms, admitted).
#   ND's T3 at 4.3 ms vs chrony at 20 ms -> both inside budget, bound
#     102.3 ms, UNCHANGED.
CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER = {
    # ~ns precision from the fold; accuracy bounded by the analog term.
    # Anything past a few ms is a broken T6, not a wide one.
    "T6": 5.0,
    # µs to a few ms, USB-bus-jitter floored (§4.5).
    "T5": 10.0,
    # ~100 µs to a few ms on A1; ~1-5 ms on A0.
    "T4": 10.0,
    # ~0.5-2 ms on A1; ~5-10 ms on A0.
    "T3": 20.0,
    # ~1-50 ms, NTP-dominated (§4.5).  A WAN bench is LEGITIMATELY wide
    # and must not be trimmed for it.
    "T2": 100.0,
    # GPSDO coast: rate perfect, phase frozen at the last snapshot.
    "T1": 100.0,
}

# Applied to a tier the table does not name.  Deliberately generous: an
# unrecognised tier is a reason to learn, not a reason to trim.
CROSS_BENCH_SIGMA_CEILING_DEFAULT_MS = 100.0


def cross_bench_sigma_ceiling_ms(tier: str) -> float:
    """The widest uncertainty ``tier`` can honestly claim, in ms.

    See CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER.  Capping a bench at its own
    tier's budget trims only a bench claiming what its tier cannot
    produce; a legitimately wide bench keeps its room.
    """
    return CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER.get(
        str(tier), CROSS_BENCH_SIGMA_CEILING_DEFAULT_MS)

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
    # Which reference plane ``utc`` is grounded in.  T4/T3/T2 read the
    # host clock now ("host"); T6 reads the anchor label plane
    # ("label"), which under the content-time labeling convention sits
    # one transport latency earlier than the host plane.  Cross-plane
    # comparisons correct for the EXPECTED plane difference
    # (``label_plane_offset_ns``) so a labeling convention is never
    # misread as a bench fault.  See
    # docs/design/CONTENT_TIME_LABELING_CONVENTION.md §5.2.
    plane: str = "host"   # "host" | "label"

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
    """T3 bench: the fused multi-broadcast D_clock.  NOT A DEFAULT BENCH.

    ⛔ Since 2026-10-22 (review-fusion-kalman-20261022.md section 15,
    step 5) the judge's default bench list leaves this bench out, so it
    can never be adopted.  The fused D_clock is calibrated: the
    calibration pulls each broadcast's mean to zero, so this bench read
    "the host is right" whatever the host did, and as the adopted bench
    it corrected ring and archive labels by that.  The judge has no
    witness-only bench, so the bench left the defaults outright.  The
    class stays for tests and for anyone who must read the file as a
    bench; do not register it with add_bench.

    Reads /run/hf-timestd/fusion_status.json (schema v1, written by
    FusionStatusWriter).  judge UTC = wallclock - d_clock_fused_ms:
    d_clock is host minus UTC, so true UTC is the wall clock less it
    (sign fixed in the same step; it read wall + d_clock before).
    Sigma from the file's own uncertainty budget.
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
            utc=wall - d_clock_s,
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
    — carried honestly as the bench sigma even though a FINE-STAGE
    anchor is itself sub-µs.  On a coarse (non-T6) capture the anchor
    is not sub-µs and the arrival floor cannot see that, so the sigma
    is widened to ``COARSE_ANCHOR_SIGMA_NS`` by provenance.  Only answers while a valid anchor exists (the
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
        health_provider: Optional[Callable[[], Optional[Dict]]] = None,
    ):
        self._provider = provider
        self._mono = mono_fn
        # T6 health (authority state, re-home age) rides in the reading
        # detail under "t6_health".  Never the key "authoritative":
        # _adoptable reads that one.
        self._health_provider = health_provider

    def poll(self) -> Optional[BenchReading]:
        try:
            state = self._provider()
        except Exception:  # noqa: BLE001 — provider trouble ≠ judge trouble
            return None
        if state is None:
            return None
        # 4-tuple carries the least-delayed-arrival floor (P2 latency
        # correction); the 3-tuple form is the pre-floor contract and
        # keeps the conservative transport bound.
        floor = None
        if len(state) == 4:
            anchor, arrival_rtp, arrival_mono, floor = state
        else:
            anchor, arrival_rtp, arrival_mono = state
        age = self._mono() - float(arrival_mono)
        if age < 0 or age > self.ARRIVAL_MAX_AGE_S:
            return None
        try:
            from .native_anchor import utc_ns_at_rtp
            utc_ns = utc_ns_at_rtp(int(arrival_rtp) & 0xFFFFFFFF, anchor)
        except Exception:  # noqa: BLE001
            return None
        detail = {
            "anchor_rtp": int(anchor.anchor_rtp),
            "anchor_tier": str(anchor.captured_via_tier),
            "chain_delay_ns": int(anchor.chain_delay_ns),
            "arrival_age_s": round(age, 3),
        }
        if self._health_provider is not None:
            try:
                health = self._health_provider()
            except Exception:  # noqa: BLE001 — health trouble ≠ judge trouble
                health = None
            if health is not None:
                detail["t6_health"] = dict(health)
        if floor is None:
            # No floor yet: the latest arrival, carrying the transport
            # latency as an honest (wide) bound.
            return BenchReading(
                tier="T6",
                utc=utc_ns / 1e9,
                sigma_ns=self.LATENCY_SIGMA_FLOOR_NS,
                mono=float(arrival_mono),
                detail=detail,
                plane="label",
            )
        # The least-delayed arrival in the window sets the mono->UTC
        # map; the latest arrival only proves the stream is alive.
        detail.update({
            "floor_n": int(floor.n),
            "floor_span_s": round(float(floor.span_s), 3),
            "latest_arrival_lag_ns": int(
                round((float(floor.offset_s) + float(arrival_mono))
                      * 1e9 - utc_ns)),
        })
        # Provenance gate (AC0G-B4 2026-08-25).  The floor measures
        # ARRIVAL scatter.  It is blind to where the anchor's second
        # boundary came from, so on a COARSE (non-T6) capture it
        # describes the wrong quantity entirely: such an anchor carries
        # sub_ns identically 0 and pins whatever RTP was current when
        # the NMEA event was handled, at unbounded capture latency.
        # Ledger arithmetic that morning: two consecutive coarse
        # recaptures moved the RTP->UTC map +234.2 ms and then
        # +20.74 ms, while two fine-stage anchors 30 s apart agreed to
        # 32 ns -- yet the bench published sigma 0.837 ms and the judge
        # kept judging on tier T6 while three benches put it 26.15 ms
        # away.  ``coast_sigma0_ns`` already applies this widening when
        # the same coarse anchor is COASTED; the live path must be no
        # less honest than the holdover path.
        from .t6_holdover import COARSE_ANCHOR_SIGMA_NS, coast_sigma0_ns
        sigma_ns = float(floor.sigma_ns)
        if str(anchor.captured_via_tier) != "T6":
            sigma_ns = coast_sigma0_ns(sigma_ns, anchor.captured_via_tier)
            detail["anchor_sigma_floor_ns"] = COARSE_ANCHOR_SIGMA_NS
        return BenchReading(
            tier="T6",
            utc=float(floor.offset_s) + float(arrival_mono),
            sigma_ns=sigma_ns,
            mono=float(arrival_mono),
            detail=detail,
            plane="label",
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


class HfAcquiredBench:
    """T3 bench: the tick-acquired registration (spec 2026-09-06 §6).

    The ONE bench that never touches the host clock: it projects the
    acquired origin (utc_ref at rtp_ref, held on the GPSDO-locked RTP
    counter) to the most recently arrived sample and hands that off to
    the monotonic clock at the arrival instant, exactly as
    NativeAnchorBench does for T6.  Its residual against the raw radiod
    pair (carried in ``detail``) names pair skew directly.

    Its published sigma is floored at ``ORIGIN_SIGMA_FLOOR_MS``: the fused
    registration's sigma is the REPEATABILITY of the plane, while this
    bench's sigma is its ACCURACY claim, and the accuracy of a
    ``expected_delay - fold_position`` origin is bounded by the delay
    model -- common-mode across channels, so fusing N of them does not
    shrink it.

    Answers on both ACQUIRED and WITNESS summary states.  WITNESS means
    a T6 station is publishing the acquired plane without it driving
    metrology — the bench still answers so the judge can compute the
    hf_acquired-vs-T6 residual.  BOOTSTRAP/CONFLICT/missing stay silent.
    An UNVERIFIED plane stays silent too (fix round 1, review C-1): a
    reading the station refuses to label samples with is not a reading
    the judge should weigh either, and this bench feeds the FUSE chrony
    sample.

    ``detail["authoritative"]`` says whether this reading may PLACE UTC
    (``registration_store.registration_refusal`` — THE one gate) as
    against merely witness the plane.  Only an authoritative reading
    becomes ``label_plane_anchor`` and so reaches chrony; a WITNESS-state
    plane is a witness and nothing more, which on a T6 station is the
    difference between "T6 owns the plane" and "a stood-down plane steers
    the host clock".  ``detail["refusal"]`` names the gate when it is not
    authoritative.

    ``plane="label"``: mechanistically identical to NativeAnchorBench
    (T6) — a fixed (rtp_ref, utc_ref) origin projected via pure RTP
    counter arithmetic to a later arrival, then handed to monotonic at
    that arrival instant.  Never reads the host clock.  This is
    unlike LbeT5Bench (T5), which is also host-clock-free but answers
    with a freshly *attested* per-arrival GPS truth rather than a fixed
    historical origin projected forward, so it keeps the "host" default
    (review task-9-review.md, finding "Minor, needs an explicit
    decision" — resolved: label plane, T6 precedent, fix round 1 F4).

    provider() -> Optional[{channel: (arrival_rtp, arrival_mono,
    arrival_sample_rate[, counter_map])}], the latest arrival of each
    archive channel in that channel's own RTP counter.  With the fourth
    element the bench answers only when the summary's map and that live
    map name one radiod run (final review I-5).  Every radiod channel starts its own
    counter, so the bench takes the arrival of the channel the summary's
    ``rtp_domain`` names -- the counter ``rtp_ref`` is in -- and no
    other.  A summary naming no domain, or a domain with no arrival,
    leaves the bench silent.
    Fix round 1 (task 9 review F1/F2): the RTP counter is per-channel
    and NOT interchangeable across channels running at different
    configured rates (``cross_channel_rtp.py``) — the provider must
    name its own sample rate so ``poll`` can refuse a domain mismatch
    outright rather than publish a wrong ``utc``.
    """

    TIER = "T3"
    FRESHNESS_S = 180.0
    ARRIVAL_MAX_AGE_S = 5.0
    _LIVE_STATES = ("ACQUIRED", "WITNESS")

    def __init__(self,
                 provider: Callable[
                     [], Optional[Dict[str, Tuple[int, float, int]]]],
                 store=None, mono_fn: Callable[[], float] = time.monotonic,
                 time_fn: Callable[[], float] = time.time):
        from .registration_store import RegistrationStore
        self._provider = provider
        self._store = store if store is not None else RegistrationStore()
        self._mono = mono_fn
        self._time = time_fn

    @staticmethod
    def _one_run(summary_map, live_map) -> bool:
        """Do the summary's map (a dict) and the arrival's live map name
        one radiod run, within the 2 s skew allowance?  False when either
        is missing."""
        if summary_map is None or live_map is None:
            return False
        from hamsci_dsp.counter_map import CounterMap
        from .counter_epoch_tracker import COUNTER_EPOCH_SKEW_ALLOWANCE_S
        try:
            m = (summary_map if isinstance(summary_map, CounterMap)
                 else CounterMap.from_dict(summary_map))
        except (KeyError, TypeError, ValueError):
            return False
        return m is not None and m.same_run(
            live_map, tol_s=COUNTER_EPOCH_SKEW_ALLOWANCE_S)

    def poll(self) -> Optional[BenchReading]:
        try:
            state = self._provider()
        except Exception:  # noqa: BLE001 — provider trouble ≠ judge trouble
            return None
        if not state:
            return None
        s = self._store.read_summary()
        if not s or s.get("state") not in self._LIVE_STATES or s.get("utc_ref") is None:
            return None
        # The arrival must be in the counter rtp_ref is in.
        domain = s.get("rtp_domain")
        if not domain:
            return None
        arrival = state.get(str(domain))
        if arrival is None:
            return None
        arrival_rtp, arrival_mono, arrival_sample_rate = tuple(arrival)[:3]
        # Final review I-5 (2026-10-30): after a radiod restart the summary
        # keeps the old run's rtp_ref, fresh, while the arrival already
        # counts in the new run's counter; delta_rtp would span two
        # counters and land hours off.  A provider that names the
        # arrival's run (a fourth element: the channel's live map, or None
        # when it has none that fits the raw pair) makes the bench refuse
        # unless the summary's map names the same run.  A three-element
        # arrival offers no run identity and keeps the older rule.
        if len(tuple(arrival)) > 3:
            if not self._one_run(s.get("counter_map"), tuple(arrival)[3]):
                return None
        age = self._mono() - float(arrival_mono)
        if age < 0 or age > self.ARRIVAL_MAX_AGE_S:
            return None
        # Fix round 1 (C-1): an unverified plane is not a reading at all.
        # It used to reach chrony through label_plane_anchor while the
        # ring, §18 and the sidecar all refused it.
        if s.get("verified") is not True:
            return None
        reg_age = self._time() - float(s.get("written_at", 0))
        if reg_age > self.FRESHNESS_S:
            return None
        sr = float(s["sample_rate"])
        # Fix round 1 F1/F2: the arrival's RTP counter must be the SAME
        # domain rtp_ref was stamped in — an archive channel running at
        # a different configured rate (WWVB 4 kHz, T6 96 kHz) is a
        # different counter entirely and the naive rtp-delta arithmetic
        # below would silently name the wrong utc.  Refuse instead.
        if int(s["sample_rate"]) != int(arrival_sample_rate):
            return None
        delta_rtp = _rtp_delta_signed(int(arrival_rtp), int(s["rtp_ref"]))
        utc = float(s["utc_ref"]) + delta_rtp / sr
        # The summary's sigma_ms is the FUSED plane's REPEATABILITY -- fold
        # SNR over tick rise time, divided by sqrt(n) and then combined
        # inverse-variance across channels, so six 1 ms channels report
        # 0.41 ms.  This bench's sigma is an ACCURACY claim the judge acts
        # on, and the acquired origin's accuracy is bounded by
        # `expected_delay - fold_position`: the great-circle/F2-hop
        # propagation model and mode ambiguity (1F vs 2F is milliseconds).
        # That bound is COMMON-MODE across channels -- every channel
        # inherits the same model error -- so it does not shrink with N and
        # cannot be fused away.  Floor it at ORIGIN_SIGMA_FLOOR_MS here,
        # where the number stops being a measurement and starts being a
        # claim; `fuse_registrations` is left reporting what its inputs
        # actually measured.
        sigma_ms = max(float(s["sigma_ms"]), ORIGIN_SIGMA_FLOOR_MS)
        # THE one gate, asked here so the FUSE feed and the anchor
        # surfaces cannot diverge (fix round 1, C-1).  A WITNESS plane
        # answers as a witness and is refused authority; the judge still
        # gets its residual, chrony never does.
        from .registration_store import registration_refusal
        refusal = registration_refusal(
            s, now=self._time(),
            stale_s=self.FRESHNESS_S, sample_rate=int(arrival_sample_rate),
        )
        return BenchReading(
            utc=utc, mono=float(arrival_mono), sigma_ns=sigma_ms * 1e6,
            tier=self.TIER,
            detail={"bench": "hf_acquired", "counter_epoch_id": s.get("counter_epoch_id"),
                    "raw_pair_residual_ms": s.get("raw_pair_residual_ms"),
                    "registration_age_s": round(reg_age, 1),
                    "authoritative": refusal is None,
                    "refusal": refusal},
            plane="label",
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
    # The stored T6 chain delay in force (spec 2026-10-27 §7): a diagnostic
    # the sidecar records so a GRAPE reader can see which value governed
    # the gate.  None without a record, or with chain_delay_learn off.
    chain_delay_ns: Optional[float] = None
    chain_delay_state: Optional[str] = None     # "seeded" | "confirmed"


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

    # Benches that witness but are never adopted (review of T3 steps 2-5,
    # finding C1, 2026-10-22).  The tick registration may win the judge's
    # T3 slot only after its shadow run beside T6 on B4 (Michael's ruling
    # 4; step 7 of the ruled order).  Until then its readings feed the
    # shadow residuals and the label-plane anchor, and never correct a
    # label.  Nor do they vote in witness dissent (final review I-1,
    # 2026-10-30), which reaches chrony through the HPPS precision.
    # Step 7 removes it from this set.
    #
    # Paired with ``[timing.t3] admitted`` (``t3_admission.py``, Michael's
    # ruling B, 2026-10-30).  Until that switch reads true the judge also
    # keeps T3 out of the label-plane tracker, out of
    # ``label_plane_anchor`` and out of §18.  Step 7 flips the switch and
    # empties this set together; either alone leaves T3 half admitted.
    SHADOW_ONLY_BENCHES = frozenset({"hf_acquired"})

    def __init__(
        self,
        config: Optional[Dict] = None,
        *,
        benches: Optional[List] = None,
        publish_path: Optional[os.PathLike] = None,
        time_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
        alert_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        anchor_closure: bool = False,
        t3_admitted: bool = False,
    ):
        cfg = dict(config or {})
        # Ruling B (2026-10-30): until ``[timing.t3] admitted`` reads true,
        # a T3 reading reaches the shadow residuals and nothing else here
        # (see ``_t3_closed``).  False by default, so a caller that forgets
        # the flag gets the closed regime.
        self.t3_admitted = bool(t3_admitted)
        # Task 17a: the registration anchor closure is opt-in
        # ([timing.registration] anchor_closure).  While it is off the
        # judge stays exactly where c7b2106 left it -- §18's
        # ``utc_anchor_ns`` comes from radiod's pair with this judge's
        # correction, and ``label_plane_anchor`` is never populated, so
        # the FUSE feed has only fusion's d_clock.  The label plane's own
        # statement is still published, under ``label_plane_witness``,
        # for the offline analysis (task 17c).
        self.anchor_closure = bool(anchor_closure)
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
        # Expected (label − host) plane offset, ns.  0.0 (the default)
        # is byte-identical to the historical behavior and is correct
        # while labels carry the fitted transport constant; under the
        # content-time labeling convention this becomes the measured
        # transport (≈ −16.6 ms on AC0G-B4 — the label plane reads
        # EARLIER than the host plane).  Applied only inside
        # ``_cross_bench_delta_ns``, never to any bench's own sigma.
        self.label_plane_offset_ns = float(
            cfg.get("label_plane_offset_ns", 0.0))
        # ...and the live measurement that supersedes it once it exists.
        # The configured value is the fallback, not the answer: the plane
        # gap is a pipeline latency that drifts with load, and a
        # hand-calibrated constant for exactly this quantity is what the
        # content-time convention proposal set out to retire.
        # Measuring is only meaningful when the planes actually DIFFER,
        # i.e. under content-time labels.  Under the legacy convention the
        # anchor already folds the pipeline latency into the label, the two
        # planes coincide, and anything measured here would be T6's own
        # residual — subtracting which would cancel the disagreement the
        # cross-bench gate exists to detect.  The recorder sets this from
        # [timing.t6_pps].labeling_convention, a key that selects the
        # reference plane of MEASUREMENT_MODEL.md §1 (§8 names the planes)
        # and nothing else.
        self.label_plane_measure = bool(cfg.get("label_plane_measure", True))
        # Whether the sustained-violation test removes the plane term.
        # OFF by default -- see _plane_expected_ns for why AC0G-B4 refuted
        # the premise. The cross-bench gate is unaffected and still
        # corrects, because it really does compare across planes.
        self.violation_plane_correction = bool(
            cfg.get("violation_plane_correction", False))
        from .label_plane import LabelPlaneTracker
        label_plane_window_s = float(cfg.get(
            "label_plane_window_s", LabelPlaneTracker.DEFAULT_WINDOW_S))
        label_plane_min_n = int(cfg.get(
            "label_plane_min_n", LabelPlaneTracker.DEFAULT_MIN_N))
        label_plane_max_host_sigma_ns = float(cfg.get(
            "label_plane_max_host_sigma_ns",
            LabelPlaneTracker.DEFAULT_MAX_HOST_SIGMA_NS))
        # The station's T6 chain delay (spec 2026-10-27 §2, §4, §6): a
        # stored record of how far T6's label plane sits from the host
        # plane.  With a record, a T6-versus-host comparison subtracts
        # the stored offset and widens the band by the record's own sigma
        # and spread; the gross bound refuses an implausible T6 first.
        # A DIAGNOSTIC: it enters only the cross-bench delta and the band,
        # never a timestamp, the chrony feed or the T6 anchor.  Off
        # (chain_delay_learn = false) keeps today's gate exactly, and no
        # store exists.
        self.chain_delay_learn = bool(cfg.get("chain_delay_learn", True))
        self.chain_delay_max_ns = float(
            cfg.get("chain_delay_max_ns", DEFAULT_CHAIN_DELAY_MAX_NS))
        self.chain_delay_path = Path(
            cfg.get("chain_delay_path") or DEFAULT_CHAIN_DELAY_PATH)
        self.chain_delay_store: Optional[ChainDelayStore] = None
        if self.chain_delay_learn:
            self.chain_delay_store = ChainDelayStore(
                self.chain_delay_path,
                identity=dict(cfg.get("chain_delay_identity") or {}),
                time_fn=time_fn,
            )
            self.chain_delay_store.load()     # never raises
        # Which tracker backs what (plan ruling).  With learning off the
        # single label-plane tracker keeps today's role: every label
        # reading feeds it and it backs every plane term.  With learning
        # on it takes only non-T6 label readings (T3), and T6 feeds two
        # trackers of its own: the learner, whose window spans a whole
        # learn (spec §3), and the 600 s witness the change alarm reads.
        # All three cut wild readings at chain_delay_outlier_ns.
        self.chain_delay_learn_s = float(
            cfg.get("chain_delay_learn_s", DEFAULT_CHAIN_DELAY_LEARN_S))
        self.chain_delay_max_spread_ns = float(cfg.get(
            "chain_delay_max_spread_ns", DEFAULT_CHAIN_DELAY_MAX_SPREAD_NS))
        # Learning off must behave as before (spec §8), so the label-plane
        # tracker then keeps its legacy statistics: population stdev, no
        # outlier cut (outlier_ns=None).
        outlier_kw: Dict = {"outlier_ns": None}
        if self.chain_delay_learn:
            outlier_kw["outlier_ns"] = float(cfg.get(
                "chain_delay_outlier_ns", DEFAULT_CHAIN_DELAY_OUTLIER_NS))
        self._label_plane = LabelPlaneTracker(
            window_s=label_plane_window_s,
            min_n=label_plane_min_n,
            max_host_sigma_ns=label_plane_max_host_sigma_ns,
            **outlier_kw,
        )
        self._t6_learner: Optional[LabelPlaneTracker] = None
        self._t6_witness: Optional[LabelPlaneTracker] = None
        # Every T6 tracker shares these settings; a relearn or a seed
        # request builds fresh ones from them.
        self._t6_tracker_kw: Dict = dict(
            min_n=label_plane_min_n,
            max_host_sigma_ns=label_plane_max_host_sigma_ns,
            **outlier_kw,
        )
        if self.chain_delay_learn:
            self._t6_learner = self._new_t6_learner()
            self._t6_witness = LabelPlaneTracker(
                window_s=T6_WITNESS_WINDOW_S, **self._t6_tracker_kw)
        # The host-plane tier the learner was last paired with; the
        # learned record names it as ``lower_tier``.
        self._t6_learner_host_tier: Optional[str] = None
        # The change alarm (spec §5).  Against a confirmed record the
        # judge compares the 600 s witness with the stored value every
        # tick; a departure that lasts chain_delay_change_sustain_s sets
        # ``_chain_changed`` ({stored_ns, live_ns, since_utc}), which
        # latches T6 out of adoption until the witness has sat inside
        # the band for the same time again, or an operator answers it.
        # The judge never absorbs a change: a fresh candidate learner
        # writes the new value beside the record, and only an ``accept``
        # request promotes it.
        self.chain_delay_change_sustain_s = float(cfg.get(
            "chain_delay_change_sustain_s",
            DEFAULT_CHAIN_DELAY_CHANGE_SUSTAIN_S))
        self._chain_changed: Optional[Dict] = None
        self._chain_departed_since: Optional[float] = None
        self._chain_returned_since: Optional[float] = None
        self._last_chain_change_log: Optional[float] = None
        self._t6_candidate_learner: Optional[LabelPlaneTracker] = None
        # One WARNING per refusal streak when a completed learn is too
        # wide to write (chain_delay_max_spread_ns): the first learn, and
        # the candidate learn.
        self._learn_spread_refused = False
        self._candidate_spread_refused = False
        # The held-T6 check (spec §5, "Holding T6"): when the held T6
        # first left the band, and when the gross bound first refused it
        # in an unbroken run.  None while it agrees.
        self._held_t6_departed_since: Optional[float] = None
        self._held_t6_gross_since: Optional[float] = None
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
            # FusionBench left the defaults on 2026-10-22 (review section
            # 15, step 5): the fused D_clock is calibrated against the
            # host, so the judge must never adopt it.  T3 in the judge is
            # HfAcquiredBench, registered by the recorder through
            # add_bench.
            self.benches = [
                ChronyBench(time_fn=time_fn, mono_fn=mono_fn),
            ]

        self._lock = threading.Lock()
        self._sources: Dict[SourceKey, _SourceState] = {}
        self._best: Optional[BenchReading] = None       # active bench reading
        self._candidate_tier: Optional[str] = None      # upgrade hysteresis
        self._candidate_count: int = 0
        self._publish_error_logged = False
        self._transition_log_failing = False
        # Cross-bench gate state (gate doc): the currently-blocking
        # conflict {upper, lower, delta_ns, since_utc} (None when no
        # candidate is being refused), the shadow-mode residuals of
        # every non-adopted bench vs the adopted one (refreshed each
        # tick), and the CRITICAL rate limiter.
        self._cross_conflict: Optional[Dict] = None
        self._shadow_residuals: Dict[str, Dict] = {}
        # Spec §11.2 (2026-09-07): the label-plane anchor's own statement
        # this tick, published for the FUSE chrony feed.  NOT the
        # selected bench — the label plane speaks whether or not the
        # judge adopted it (hf_acquired stays a witness in the tier
        # arbitration).  See _select_label_anchor_locked.
        self._label_anchor: Optional[BenchReading] = None
        # Task 14a: the label-plane ANCHOR provider (the recorder's
        # `_label_anchor_state`).  Distinct from `_label_anchor` above,
        # which is a bench READING: this is the anchor object itself, and
        # it is what §18's utc_anchor_ns is computed from.  None on a
        # station with no label plane.
        self._label_anchor_provider: Optional[Callable] = None
        self._dissent = None
        from .witness_dissent import DissentWatch
        self._dissent_watch = DissentWatch()
        self._last_cross_critical_log: float = 0.0
        # The learning-phase conflict line keeps its own cadence (once,
        # then hourly).  None means "not logged under this condition
        # yet"; a record in force resets it.
        self._last_learning_conflict_log: Optional[float] = None
        # Likewise a T6 refused against the record in force: a standing
        # conflict logs once, then hourly.  A learning conflict resets it.
        self._last_record_conflict_log: Optional[float] = None
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
        # Task 14 (T3 foundations): T3 against T6 in the sample plane.  The
        # recorder wires a provider returning the ``t3_shadow`` block
        # (T3ShadowComparator.block); polled outside the lock each tick.
        # Published only: nothing in the judge acts on it.
        self._t3_shadow_provider: Optional[Callable[[], Optional[Dict]]] = None
        self._t3_shadow: Optional[Dict] = None          # cached each tick

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

    def set_label_anchor_provider(self, provider: Optional[Callable]) -> None:
        """Install the label-plane anchor provider (task 14a).

        ``provider() -> Optional[LabelAnchor]``, cached state only (the
        recorder refreshes it on its own tick).  While it answers for a
        source's counter domain, that anchor — not radiod's host-stamped
        pair plus this judge's correction — is what §18's
        ``utc_anchor_ns`` states.  The judge stays a WITNESS: its verdict
        still measures the pair and still drives violation/escalation,
        but it no longer defines the published plane.
        """
        with self._lock:
            self._label_anchor_provider = provider

    def _label_anchor_for_locked(self, st: "_SourceState"):
        """The label anchor valid for one source's counter domain, or None.

        The domain guard is not optional: an anchor is a ruler for ONE
        counter (``cross_channel_rtp.py``).  T6's anchor lives in the
        96 kHz BPSK counter and must never label a 24 kHz archive
        channel; the T3 registration is stamped in the archive channels'
        own counter and must never label the T6 stream.
        """
        # Task 17a: with the closure off, §18 states radiod's judged pair
        # whatever the recorder wired in.
        if not self.anchor_closure:
            return None
        provider = self._label_anchor_provider
        if provider is None:
            return None
        try:
            label = provider()
        except Exception as e:  # noqa: BLE001 — provenance never breaks the judge
            logger.debug(f"OffsetJudge label-anchor provider failed: {e}")
            return None
        if label is None or not label.matches_rate(st.sample_rate):
            return None
        # Ruling B (2026-10-30): §18 states no unadmitted T3 anchor.
        if self._t3_closed(getattr(label, "tier", None)):
            return None
        return label

    def set_t6_rate_provider(
        self, provider: Callable[[], Optional[RateEstimate]]
    ) -> None:
        """Wire the T6 residual-walk rate observable (P3).

        ``provider`` is polled once per tick (slow path); it must be
        thread-safe and return the current RateEstimate or None.
        """
        with self._lock:
            self._t6_rate_provider = provider

    def set_t3_shadow_provider(
        self, provider: Optional[Callable[[], Optional[Dict]]]
    ) -> None:
        """Wire the T3-against-T6 shadow block (task 14).

        ``provider`` is polled once per tick, outside the lock; it must be
        thread-safe and return the ``t3_shadow`` dict or None.  A provider
        that raises publishes ``t3_shadow: null`` for that tick.  None
        unwires it.
        """
        with self._lock:
            self._t3_shadow_provider = provider

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
        store = self.chain_delay_store
        record = store.record if store is not None else None
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
            chain_delay_ns=(float(record.offset_ns)
                            if record is not None else None),
            chain_delay_state=(record.state if record is not None else None),
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
        t3_shadow = None
        shadow_provider = self._t3_shadow_provider
        if shadow_provider is not None:
            try:
                t3_shadow = shadow_provider()
            except Exception as e:  # noqa: BLE001 — a shadow row ≠ judge trouble
                logger.debug(f"OffsetJudge t3 shadow provider failed: {e}")
                t3_shadow = None
        gpsdo_state, gpsdo_detail = self._poll_gpsdo()
        # An operator request the CLI left beside the chain-delay record
        # (seed / accept / relearn).  Read and deleted here, outside the
        # lock; applied under it, before selection, so this tick already
        # selects against the answer.
        chain_request = (self.chain_delay_store.take_request()
                         if self.chain_delay_store is not None else None)

        mono_now = self._mono()
        with self._lock:
            self._t6_rate = t6_rate
            self._t3_shadow = t3_shadow
            self._gpsdo_discipline = gpsdo_state
            self._gpsdo_detail = gpsdo_detail
            if chain_request is not None:
                self._apply_chain_request_locked(chain_request)
            previous = self._best
            best = self._select_bench_locked(readings)
            if best is not None:
                self._best = best
                for st in self._sources.values():
                    self._measure_source_locked(st, best, mono_now)
            # Shadow-mode residuals: every non-adopted bench vs the
            # adopted one, refreshed each tick (gate doc).
            self._observe_label_plane_locked(readings, mono_now)
            self._select_label_anchor_locked(readings)
            self._update_shadow_locked(readings, self._best, mono_now)
            transition = self._tier_transition_locked(
                previous, best, readings, mono_now)
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
        if transition is not None:
            self._append_tier_transition(transition)

    # ── the tier-change log (spec 2026-10-27 §7) ──────────────────────

    def _tier_transition_locked(
        self,
        previous: Optional[BenchReading],
        adopted: Optional[BenchReading],
        readings: List[BenchReading],
        mono_now: float,
    ) -> Optional[Dict]:
        """The log line for a change of the adopted tier this tick, or None.

        ``from`` is null on the first adoption.  ``delta_ns`` is the new
        tier's residual against the old one, through the same choke point
        as the shadow residuals; None when the old tier did not answer
        this tick.  ``chain_delay_ns`` is the record in force after the
        change.  Never raises: a log line must not disturb the tick.
        """
        try:
            if adopted is None:
                return None
            old_tier = previous.tier if previous is not None else None
            if old_tier == adopted.tier:
                return None
            delta = residual = None
            old = next((r for r in readings if r.tier == old_tier), None)
            if old is not None:
                # delta_ns: the raw label step (no plane term), both
                # readings at one instant.  residual_ns: the same pair
                # through the gate's choke point, net of the plane term.
                delta = round((adopted.utc_at(mono_now)
                               - old.utc_at(mono_now)) * 1e9, 1)
                residual = round(
                    self._cross_bench_delta_ns(adopted, old, mono_now), 1)
            store = self.chain_delay_store
            record = store.record if store is not None else None
            return {
                "utc": datetime.fromtimestamp(
                    self._time(), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "from": old_tier,
                "to": adopted.tier,
                # The first adoption after construction (from null) is a
                # judge starting up, not a step in the station's timing.
                "reason": "startup" if old_tier is None else "change",
                "delta_ns": delta,
                "residual_ns": residual,
                "chain_delay_ns": (float(record.offset_ns)
                                   if record is not None else None),
            }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OffsetJudge tier transition not built: {e}")
            return None

    def _append_tier_transition(self, line: Dict) -> None:
        """Append one JSON line beside the chain-delay record.  A failure
        logs one ERROR per failure streak and never propagates."""
        path = self.chain_delay_path.parent / "tier_transitions.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except Exception as e:  # noqa: BLE001
            if not self._transition_log_failing:
                logger.error(
                    f"OffsetJudge cannot append to {path} ({e}); the "
                    f"tier-change log is incomplete")
            self._transition_log_failing = True
            return
        self._transition_log_failing = False

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

    def _adoptable(self, r: BenchReading) -> bool:
        """May this reading be adopted?  Not if its bench is shadow-only,
        and not if it says it is not authoritative."""
        detail = r.detail or {}
        if detail.get("bench") in self.SHADOW_ONLY_BENCHES:
            return False
        # The change-alarm latch (spec §5): while a T6 chain-delay change
        # stands, T6 is not adoptable at all.
        if r.tier == "T6" and self._chain_changed is not None:
            return False
        return detail.get("authoritative", True) is not False

    def _t3_closed(self, tier: Optional[str]) -> bool:
        """Is this a T3 reading or anchor the station has not admitted?

        Ruling B (Michael, 2026-10-30): until ``[timing.t3] admitted``
        reads true, T3 reaches nothing beyond its own shadow record and
        its published summary.  Here that closes the label-plane tracker
        (N-1), ``label_plane_anchor`` and §18's label anchor.  The shadow
        residuals and the ``t3_shadow`` block stay open: B4's shadow run
        needs them."""
        return tier == "T3" and not self.t3_admitted

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

        Same-tier arbitration (fix round 1, task 9 review F3): TIER_ORDER
        is the primary key, but two benches CAN share a tier string
        (FusionBench and HfAcquiredBench both publish "T3") and
        `add_bench` has no uniqueness constraint.  Without a tie-break,
        Python's stable sort silently prefers whichever bench happens to
        be registered first — an emergent artifact of construction
        order, not a documented rule, and it let a host-clock-dependent
        FusionBench reading permanently shadow a host-clock-FREE
        HfAcquiredBench reading whenever both answered.  Repo doctrine
        (CLAUDE.md: "tier rank never substitutes for demonstrated
        precision") says the tighter reading should win instead, so
        sigma_ns is the secondary sort key within one tier.
        """
        # Adoption is for authoritative readings only.  A shadow-only
        # bench (SHADOW_ONLY_BENCHES) and any reading that says it is not
        # authoritative (a WITNESS-state plane) still reach the shadow
        # residuals through tick(), but never this selection.
        readings = [r for r in readings if self._adoptable(r)]
        if not readings:
            self._candidate_tier = None
            self._candidate_count = 0
            return None
        mono_now = self._mono()
        by_rank = sorted(
            readings,
            key=lambda r: (self._tier_rank(r.tier), float(r.sigma_ns)),
        )
        best = by_rank[0]
        active_tier = self._best.tier if self._best is not None else None
        if active_tier == "T6":
            held = self._held_t6_check_locked(by_rank, mono_now)
            if held == "drop":
                # A held T6 that left the band for sustain_window_s
                # drops like a tier gone silent, but the re-selection
                # stays chain-gated over what is left.
                self._candidate_tier = None
                self._candidate_count = 0
                rest = [r for r in by_rank if r.tier != "T6"]
                adopted = self._chain_consistent_locked(rest, mono_now)
                self._release_holds_for_locked(adopted.tier)
                return adopted
            if held == "absent":
                # The gross bound refused the held T6 this tick: adopt
                # nothing new.  The judge keeps its last good T6 reading
                # rather than either adopting a wild one or degrading
                # on one tick.
                return None
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
        cross_ok = (gate_ref is None
                    or self._cross_gate_ok_locked(best, gate_ref, mono_now))
        gate_ok = cross_ok
        if not cross_ok:
            # The cross-bench conflict is the operative block, so the
            # sigma gate does not run this tick and cannot refresh or
            # release any hold it published earlier.  Drop it: a frozen
            # sigma_candidate_ns reads as the candidate's CURRENT
            # precision (AC0G-B4 2026-08-15 kept publishing 25 ms long
            # after the measured sigma had fallen to 0.98 ms).
            self._precision_hold = None
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

    # ── measured label-plane term ───────────────────────────────────────

    def observe_label_plane(
        self, label: BenchReading, host: BenchReading
    ) -> None:
        """Feed one (label-plane, host-plane) pair to the plane tracker.

        Same-plane pairs carry no plane information and are ignored; the
        tracker itself refuses observations from an undisciplined host.
        With chain-delay learning on, a T6 reading goes to the T6 learner
        and witness instead (and only when healthy); the label-plane
        tracker then carries the other label tiers alone.
        """
        if getattr(label, "plane", "host") != "label":
            return
        if getattr(host, "plane", "host") != "host":
            return
        if self._t6_learner is not None and label.tier == "T6":
            self._observe_t6_chain(label, host)
            return
        if not self.label_plane_measure:
            return
        self._label_plane.observe(
            # Both readings at ONE instant (the host's); otherwise the gap
            # in their read times lands in the offset as a spurious term.
            label_utc=float(label.utc_at(host.mono)),
            host_utc=float(host.utc),
            host_sigma_ns=float(host.sigma_ns),
            mono=float(host.mono),
        )

    # ── learning the T6 chain delay (spec 2026-10-27 §3) ──────────────

    @staticmethod
    def _t6_healthy(r: BenchReading) -> bool:
        """T6 may teach the chain delay only while it is settled.

        AUTHORITATIVE for the last ``T6_HEALTHY_SETTLE_S``, no coarse
        re-home in that time, and the anchor captured through T6 itself.
        A reading with no health detail (no provider) never qualifies.
        """
        detail = getattr(r, "detail", None) or {}
        health = detail.get("t6_health")
        if not isinstance(health, dict):
            return False
        if detail.get("anchor_tier") != "T6":
            return False
        if health.get("authority_state") != "AUTHORITATIVE":
            return False
        try:
            if float(health.get("state_for_s")) < T6_HEALTHY_SETTLE_S:
                return False
            rehome_age_s = health.get("rehome_age_s")
            if (rehome_age_s is not None
                    and float(rehome_age_s) < T6_HEALTHY_SETTLE_S):
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _observe_t6_chain(
        self, label: BenchReading, host: BenchReading
    ) -> None:
        """Feed one healthy T6 / host pair to the learner and the witness.

        The gross bound comes first (spec §6), on the raw delta at the
        host's instant, so an anchor re-captured hundreds of ms away never
        reaches either tracker and never counts as an outlier there.
        """
        self._reset_learning_on_rehome_locked(label)
        if not self._t6_healthy(label):
            return
        label_utc = float(label.utc_at(host.mono))
        raw_ns = (label_utc - float(host.utc)) * 1e9
        if not self._t6_gross_ok(label, host, raw_ns):
            return
        trackers = [self._t6_learner, self._t6_witness]
        if self._t6_candidate_learner is not None:
            trackers.append(self._t6_candidate_learner)
        for tracker in trackers:
            tracker.observe(
                label_utc=label_utc,
                host_utc=float(host.utc),
                host_sigma_ns=float(host.sigma_ns),
                mono=float(host.mono),
            )
        self._t6_learner_host_tier = host.tier

    def _reset_learning_on_rehome_locked(self, label: BenchReading) -> None:
        """A coarse re-home in the last ``T6_HEALTHY_SETTLE_S`` restarts
        the learner and any candidate learner.

        The health rule already keeps a re-homing T6 out of the trackers,
        but a learn would otherwise resume on the far side and span the
        re-home, mixing two lock points into one record.  The witness is
        left alone: it is short, and the change alarm should see the
        re-home's effect, not forget it.
        """
        detail = getattr(label, "detail", None) or {}
        health = detail.get("t6_health")
        if not isinstance(health, dict):
            return
        try:
            rehome_age_s = health.get("rehome_age_s")
            if rehome_age_s is None or float(rehome_age_s) >= T6_HEALTHY_SETTLE_S:
                return
        except (TypeError, ValueError):
            return
        if self._t6_learner._obs:
            logger.info(
                f"OffsetJudge T6 chain-delay learn restarted: the coarse "
                f"calibrator re-homed {float(rehome_age_s):.0f} s ago; "
                f"{len(self._t6_learner._obs)} observations discarded")
            self._t6_learner = self._new_t6_learner()
            self._learn_spread_refused = False
        if (self._t6_candidate_learner is not None
                and self._t6_candidate_learner._obs):
            self._t6_candidate_learner = self._new_t6_learner()
            self._candidate_spread_refused = False

    def _chain_learning_active(self) -> bool:
        """Learning on, and no confirmed record yet (none, or a seed)."""
        store = self.chain_delay_store
        if store is None:
            return False
        return store.record is None or store.record.state == "seeded"

    def _learning_progress(self) -> Dict:
        """``{"span_s", "n", "target_s"}`` of the learn under way."""
        est = self._t6_learner.estimate()
        return {
            "span_s": round(float(est.span_s), 1) if est is not None else 0.0,
            "n": int(est.n) if est is not None else 0,
            "target_s": self.chain_delay_learn_s,
        }

    def _maybe_confirm_learn_locked(self) -> None:
        """Write the learned chain delay once a full learn completes.

        Only while no confirmed record exists: a first learn needs no
        human confirmation, and it replaces a seed without ``chain-delay accept``
        because a seed comes from a rougher measurement.  Once confirmed,
        the learner stops writing; every later change goes through the
        change alarm and its candidate.
        """
        if not self._chain_learning_active():
            return
        est = self._t6_learner.estimate()
        if est is None:
            return
        if float(est.span_s) < self.chain_delay_learn_s:
            return
        if float(est.sigma_ns) > self.MAX_PLANE_SIGMA_NS:
            return
        if not self._learn_spread_ok(est, "learn"):
            return
        store = self.chain_delay_store
        seed = store.record
        now = self._time()
        learned = ChainDelayRecord(
            offset_ns=float(est.offset_ns),
            sigma_ns=float(est.sigma_ns),
            spread_ns=float(est.spread_ns),
            n=int(est.n),
            n_eff=float(est.n_eff),
            span_s=float(est.span_s),
            learned_from_utc=self._iso_utc(now - float(est.span_s)),
            learned_to_utc=self._iso_utc(now),
            lower_tier=self._t6_learner_host_tier or "T4",
            state="confirmed",
        )
        persisted = store.set_record(learned)
        logger.warning(
            f"OffsetJudge T6 chain delay LEARNED: "
            f"{learned.offset_ns/1e6:+.3f} ms, sigma "
            f"{learned.sigma_ns/1e6:.3f} ms, spread "
            f"{learned.spread_ns/1e6:.3f} ms, n_eff {learned.n_eff:.1f} "
            f"(n={learned.n} over {learned.span_s:.0f} s against "
            f"{learned.lower_tier}); record confirmed"
            + (f", replacing the seed {float(seed.offset_ns)/1e6:+.3f} ms"
               if seed is not None else "")
            + ("" if persisted else " (not persisted; see the store error)")
        )
        if seed is None:
            return
        if self._chain_changed is not None:
            # The alarm stood against the seed; the learn now supplies
            # the value it was measured against, so nothing remains to
            # answer.
            self._clear_chain_change_locked()
            logger.warning(
                f"OffsetJudge T6 chain delay: seed replaced by the learn; "
                f"change alarm cleared (seed {float(seed.offset_ns)/1e6:+.3f} "
                f"ms, learned {learned.offset_ns/1e6:+.3f} ms)")
        # A seed that far off says the seeding went wrong.
        seed_band_ns = self.cross_bench_k * math.sqrt(
            learned.sigma_ns ** 2 + float(seed.sigma_ns) ** 2
            + float(seed.spread_ns) ** 2)
        departure_ns = learned.offset_ns - float(seed.offset_ns)
        if abs(departure_ns) > seed_band_ns:
            logger.warning(
                f"OffsetJudge T6 chain delay: the learned value "
                f"{learned.offset_ns/1e6:+.3f} ms lies outside the seed's "
                f"band (seed {float(seed.offset_ns)/1e6:+.3f} ms, departure "
                f"{departure_ns/1e6:+.3f} ms, band "
                f"k_x*sqrt(sigma_learned^2+sigma_seed^2+spread_seed^2) = "
                f"{seed_band_ns/1e6:.3f} ms) -- check how this station was "
                f"seeded"
            )

    def _learn_spread_ok(self, est, what: str) -> bool:
        """True when a completed learn is narrow enough to write.

        A learn wider than ``chain_delay_max_spread_ns`` most likely
        straddled a step, and its spread would widen the band until the
        gate and the change alarm barely act.  Refuse it with one WARNING
        per refusal streak; the learner keeps running, and a window that
        no longer holds the step writes in its turn.
        """
        attr = ("_learn_spread_refused" if what == "learn"
                else "_candidate_spread_refused")
        if float(est.spread_ns) <= self.chain_delay_max_spread_ns:
            setattr(self, attr, False)
            return True
        if not getattr(self, attr):
            logger.warning(
                f"OffsetJudge T6 chain delay {what} NOT written: spread "
                f"{float(est.spread_ns)/1e6:.3f} ms exceeds "
                f"chain_delay_max_spread_ns "
                f"{self.chain_delay_max_spread_ns/1e6:.3f} ms (median "
                f"{float(est.offset_ns)/1e6:+.3f} ms, n={int(est.n)} over "
                f"{float(est.span_s):.0f} s).  A spread this wide usually "
                f"means the learn straddled a step; learning continues and "
                f"writes once the window holds one population.")
        setattr(self, attr, True)
        return False

    def _new_t6_learner(self):
        """A learner whose window spans a whole learn.

        5 % past the target span, so the window still holds a full
        chain_delay_learn_s of observations when the learn completes.
        """
        from .label_plane import LabelPlaneTracker
        return LabelPlaneTracker(
            window_s=1.05 * self.chain_delay_learn_s, **self._t6_tracker_kw)

    def _reset_t6_learning_locked(self) -> None:
        """Start every T6 tracker afresh and clear the change alarm.

        A seed or a relearn request changes what the trackers compare
        against, so nothing they hold still answers the new question.
        """
        from .label_plane import LabelPlaneTracker
        self._t6_learner = self._new_t6_learner()
        self._t6_witness = LabelPlaneTracker(
            window_s=T6_WITNESS_WINDOW_S, **self._t6_tracker_kw)
        self._t6_candidate_learner = None
        self._learn_spread_refused = False
        self._candidate_spread_refused = False
        self._clear_chain_change_locked()

    def _clear_chain_change_locked(self) -> None:
        """Drop the change latch and every timer that leads to it."""
        self._chain_changed = None
        self._chain_departed_since = None
        self._chain_returned_since = None
        self._last_chain_change_log = None

    # ── the change alarm (spec 2026-10-27 §5) ─────────────────────────

    def _chain_change_band_ns(
        self, live_sigma_ns: float, record: ChainDelayRecord
    ) -> float:
        """k_x * sqrt(s_live^2 + s_stored^2 + spread^2), ns.

        The adoption band with the live witness's sigma in place of the
        candidate bench's and no lower-tier term: the witness already
        measured T6 against the host plane (spec §5).
        """
        return self.cross_bench_k * math.sqrt(
            float(live_sigma_ns) ** 2 + float(record.sigma_ns) ** 2
            + float(record.spread_ns) ** 2)

    def _evaluate_chain_change_locked(self, mono_now: float) -> None:
        """Compare the live witness with the record in force; alarm on a
        sustained departure, clear on a sustained return.

        A seed and a confirmed record alarm alike (spec §3).  Only a
        confirmed record starts a candidate learner: against a seed, the
        learner already running writes the replacement on its own, and
        that replacement clears the alarm.  A tick with no witness
        estimate restarts whichever timer is running, since it offers no
        evidence either way.
        """
        store = self.chain_delay_store
        record = store.record if store is not None else None
        if record is None:
            self._clear_chain_change_locked()
            return
        est = self._t6_witness.estimate()
        if est is None:
            self._chain_departed_since = None
            self._chain_returned_since = None
            return
        stored_ns = float(record.offset_ns)
        live_ns = float(est.offset_ns)
        band_ns = self._chain_change_band_ns(est.sigma_ns, record)
        outside = abs(live_ns - stored_ns) > band_ns
        if self._chain_changed is None:
            if not outside:
                self._chain_departed_since = None
                return
            if self._chain_departed_since is None:
                self._chain_departed_since = mono_now
            if (mono_now - self._chain_departed_since
                    < self.chain_delay_change_sustain_s):
                return
            self._chain_changed = {
                "stored_ns": stored_ns,
                "live_ns": round(live_ns, 1),
                "since_utc": self._iso_utc(
                    self._time() - (mono_now - self._chain_departed_since)),
            }
            self._chain_departed_since = None
            self._chain_returned_since = None
            # A fresh learner under the §3 rules measures the new value.
            # Against a seed the first learn is still running and will
            # replace the seed itself, so no candidate learner starts.
            if record.state == "confirmed":
                self._t6_candidate_learner = self._new_t6_learner()
                self._candidate_spread_refused = False
            self._log_chain_change_locked(est, band_ns, mono_now)
            return
        # The alarm stands.  Keep the published live value current.
        self._chain_changed["live_ns"] = round(live_ns, 1)
        if outside:
            self._chain_returned_since = None
            last = self._last_chain_change_log
            if (last is None
                    or mono_now - last >= CHAIN_CHANGE_LOG_INTERVAL_S):
                self._log_chain_change_locked(est, band_ns, mono_now)
            return
        if self._chain_returned_since is None:
            self._chain_returned_since = mono_now
        if (mono_now - self._chain_returned_since
                < self.chain_delay_change_sustain_s):
            return
        # Back inside the band for the sustain time: nothing changed,
        # so nothing needs confirming.  T6 may return through the
        # ordinary upgrade gate.  A candidate already on disk stays.
        since_utc = self._chain_changed.get("since_utc")
        self._clear_chain_change_locked()
        self._t6_candidate_learner = None
        logger.warning(
            f"OffsetJudge T6 CHAIN DELAY CHANGE CLEARED: the live T6 "
            f"chain delay {live_ns/1e6:+.3f} ms has sat within "
            f"{band_ns/1e6:.3f} ms of the stored {stored_ns/1e6:+.3f} ms "
            f"for {self.chain_delay_change_sustain_s:.0f} s "
            f"(chain_delay_change_sustain_s); the alarm raised at "
            f"{since_utc} is withdrawn and T6 may be adopted again "
            f"through the upgrade gate."
        )

    def _log_chain_change_locked(
        self, est, band_ns: float, mono_now: float
    ) -> None:
        """The change alarm's CRITICAL line: once, then hourly."""
        self._last_chain_change_log = mono_now
        changed = self._chain_changed
        logger.critical(
            f"T6 CHAIN DELAY CHANGED: stored "
            f"{changed['stored_ns']/1e6:+.3f} ms, now "
            f"{float(est.offset_ns)/1e6:+.3f} ms ± "
            f"{float(est.sigma_ns)/1e6:.3f} ms (spread "
            f"{float(est.spread_ns)/1e6:.3f} ms, n={int(est.n)} over the "
            f"last {T6_WITNESS_WINDOW_S:.0f} s; band "
            f"k_x*sqrt(sigma_live^2+sigma_stored^2+spread^2) = "
            f"{band_ns/1e6:.3f} ms; outside it since "
            f"{changed['since_utc']}).  T6 stays dropped; the stored value "
            f"stands.  "
            + ("A fresh learn will write a candidate beside it after "
               f"{self.chain_delay_learn_s:.0f} s: `hf-timestd chain-delay "
               "show`, then `accept` it or `relearn`."
               if self._t6_candidate_learner is not None else
               "The stored value is a seed: the first full learn will "
               "replace it and clear this alarm.")
            + "  This line repeats hourly while the change stands."
        )

    def _maybe_write_candidate_locked(self) -> None:
        """Write the candidate learner's value once its learn completes.

        Same completion rule as the first learn (span and sigma).  The
        candidate goes beside the record, never over it; the learner then
        stops, so a standing change writes one candidate, not one per
        tick.
        """
        learner = self._t6_candidate_learner
        if learner is None:
            return
        est = learner.estimate()
        if est is None:
            return
        if float(est.span_s) < self.chain_delay_learn_s:
            return
        if float(est.sigma_ns) > self.MAX_PLANE_SIGMA_NS:
            return
        if not self._learn_spread_ok(est, "candidate"):
            return
        now = self._time()
        candidate = ChainDelayRecord(
            offset_ns=float(est.offset_ns),
            sigma_ns=float(est.sigma_ns),
            spread_ns=float(est.spread_ns),
            n=int(est.n),
            n_eff=float(est.n_eff),
            span_s=float(est.span_s),
            learned_from_utc=self._iso_utc(now - float(est.span_s)),
            learned_to_utc=self._iso_utc(now),
            lower_tier=self._t6_learner_host_tier or "T4",
            state="confirmed",
        )
        self.chain_delay_store.set_candidate(candidate)
        self._t6_candidate_learner = None
        record = self.chain_delay_store.record
        logger.warning(
            f"OffsetJudge T6 chain delay CANDIDATE written: "
            f"{candidate.offset_ns/1e6:+.3f} ms, sigma "
            f"{candidate.sigma_ns/1e6:.3f} ms, spread "
            f"{candidate.spread_ns/1e6:.3f} ms (n={candidate.n} over "
            f"{candidate.span_s:.0f} s), beside the stored "
            + (f"{float(record.offset_ns)/1e6:+.3f} ms"
               if record is not None else "(none)")
            + ".  The judge never promotes it: `hf-timestd chain-delay "
            "accept` adopts it, `relearn` discards both."
        )

    # ── operator requests (spec §5; the CLI writes, the judge applies) ─

    def _apply_chain_request_locked(self, req: Dict) -> None:
        """Act on one request the CLI left beside the record.

        ``seed`` writes a seeded record (over a confirmed one too, which
        goes to history), ``accept`` promotes a full-length candidate,
        ``relearn`` discards both.  Each refusal logs one WARNING naming
        the reason; take_request has already deleted the file either way.
        """
        store = self.chain_delay_store
        action = req.get("action")
        if action == "seed":
            self._apply_seed_request_locked(req)
        elif action == "accept":
            cand = store.candidate
            if cand is None:
                logger.warning(
                    "OffsetJudge chain-delay accept refused: no candidate "
                    "exists to accept")
                return
            if float(cand.span_s) < self.chain_delay_learn_s:
                logger.warning(
                    f"OffsetJudge chain-delay accept refused: the "
                    f"candidate's span {float(cand.span_s):.0f} s falls "
                    f"short of chain_delay_learn_s "
                    f"{self.chain_delay_learn_s:.0f} s")
                return
            if float(cand.sigma_ns) > self.MAX_PLANE_SIGMA_NS:
                logger.warning(
                    f"OffsetJudge chain-delay accept refused: the "
                    f"candidate's sigma {float(cand.sigma_ns)/1e6:.3f} ms "
                    f"exceeds the {self.MAX_PLANE_SIGMA_NS/1e6:.1f} ms "
                    f"bound")
                return
            if float(cand.spread_ns) > self.chain_delay_max_spread_ns:
                logger.warning(
                    f"OffsetJudge chain-delay accept refused: the "
                    f"candidate's spread {float(cand.spread_ns)/1e6:.3f} ms "
                    f"exceeds chain_delay_max_spread_ns "
                    f"{self.chain_delay_max_spread_ns/1e6:.3f} ms")
                return
            old = store.record
            store.promote_candidate()
            self._clear_chain_change_locked()
            self._t6_candidate_learner = None
            logger.warning(
                f"OffsetJudge chain-delay accept: the candidate "
                f"{float(cand.offset_ns)/1e6:+.3f} ms is now the record"
                + (f", replacing {float(old.offset_ns)/1e6:+.3f} ms"
                   if old is not None else ""))
        elif action == "relearn":
            store.discard()
            self._reset_t6_learning_locked()
            logger.warning(
                "OffsetJudge chain-delay relearn: record and candidate "
                "discarded; learning starts again")
        else:
            logger.warning(
                f"OffsetJudge chain-delay request ignored: unknown action "
                f"{action!r}")

    def _apply_seed_request_locked(self, req: Dict) -> None:
        """``{"action": "seed", "offset_ns", "sigma_ns", "spread_ns"}``."""
        try:
            offset_ns = float(req["offset_ns"])
            sigma_ns = float(req["sigma_ns"])
            spread_ns = float(req["spread_ns"])
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.warning(
                f"OffsetJudge chain-delay seed refused: offset_ns, sigma_ns "
                f"and spread_ns must each be a number (got {req!r})")
            return
        if not all(math.isfinite(v) for v in (offset_ns, sigma_ns, spread_ns)):
            logger.warning(
                f"OffsetJudge chain-delay seed refused: offset_ns, sigma_ns "
                f"and spread_ns must each be a finite number (got {req!r})")
            return
        if abs(offset_ns) > self.chain_delay_max_ns:
            logger.warning(
                f"OffsetJudge chain-delay seed refused: |offset| "
                f"{abs(offset_ns)/1e6:.3f} ms exceeds chain_delay_max_ns "
                f"{self.chain_delay_max_ns/1e6:.3f} ms")
            return
        if sigma_ns <= 0.0:
            logger.warning(
                f"OffsetJudge chain-delay seed refused: sigma_ns "
                f"{sigma_ns:.0f} must be positive")
            return
        if spread_ns <= 0.0:
            logger.warning(
                f"OffsetJudge chain-delay seed refused: spread_ns "
                f"{spread_ns:.0f} must be positive")
            return
        now_utc = self._iso_utc(self._time())
        self.chain_delay_store.set_record(ChainDelayRecord(
            offset_ns=offset_ns, sigma_ns=sigma_ns, spread_ns=spread_ns,
            n=0, n_eff=0.0, span_s=0.0,
            learned_from_utc=now_utc, learned_to_utc=now_utc,
            lower_tier="T4", state="seeded",
        ))
        # Learning runs again from the seed; its first full learn
        # replaces it (spec §3).
        self._reset_t6_learning_locked()
        logger.warning(
            f"OffsetJudge chain-delay seed: {offset_ns/1e6:+.3f} ms, sigma "
            f"{sigma_ns/1e6:.3f} ms, spread {spread_ns/1e6:.3f} ms; learning "
            f"starts again from it")

    # ── the held-T6 check (spec §5, "Holding T6") ────────────────────

    def _held_t6_check_locked(
        self, by_rank: List[BenchReading], mono_now: float
    ) -> str:
        """Re-test a held T6 against the record in force, every tick.

        Returns "ok" (keep selecting as usual), "drop" (the T6 has sat
        outside the band for sustain_window_s) or "absent" (the gross
        bound refused it this tick).  Any record in force, seeded or
        confirmed, arms it.  The T6 meets the tightest host-plane reading.
        A gross refusal counts as absent, not as a departure; an unbroken
        run of them lasting sustain_window_s drops T6 as a tier gone
        silent would.
        """
        store = self.chain_delay_store
        t6 = next((r for r in by_rank if r.tier == "T6"
                   and getattr(r, "plane", "host") == "label"), None)
        hosts = [r for r in by_rank
                 if getattr(r, "plane", "host") == "host"]
        if (store is None or store.record is None
                or t6 is None or not hosts):
            self._held_t6_departed_since = None
            self._held_t6_gross_since = None
            return "ok"
        host = min(hosts, key=lambda r: float(r.sigma_ns))
        raw_ns = (t6.utc_at(mono_now) - host.utc_at(mono_now)) * 1e9
        if not self._t6_gross_ok(t6, host, raw_ns):
            if self._held_t6_gross_since is None:
                self._held_t6_gross_since = mono_now
            if (mono_now - self._held_t6_gross_since
                    < self.sustain_window_s):
                return "absent"
            self._log_held_t6_drop(
                host, f"the gross bound has refused it (raw "
                f"{raw_ns/1e6:+.3f} ms, bound "
                f"{self._t6_gross_bound_ns(t6, host)/1e6:.3f} ms)")
            return "drop"
        self._held_t6_gross_since = None
        delta_ns = self._cross_bench_delta_ns(t6, host, mono_now)
        band_ns = self._chain_band_ns(t6, host)
        if abs(delta_ns) <= band_ns:
            self._held_t6_departed_since = None
            return "ok"
        if self._held_t6_departed_since is None:
            self._held_t6_departed_since = mono_now
        if mono_now - self._held_t6_departed_since < self.sustain_window_s:
            return "ok"
        self._log_held_t6_drop(
            host, f"it sits {delta_ns/1e6:+.3f} ms from the stored chain "
            f"delay {float(store.record.offset_ns)/1e6:+.3f} ms, outside "
            f"the band {band_ns/1e6:.3f} ms")
        return "drop"

    def _log_held_t6_drop(self, host: BenchReading, why: str) -> None:
        self._held_t6_departed_since = None
        self._held_t6_gross_since = None
        logger.warning(
            f"OffsetJudge held T6 DROPPED: for {self.sustain_window_s:.0f} s "
            f"(sustain_window_s) {why}, judged against {host.tier}; "
            f"re-selecting over the lower tiers.  T6 may return only "
            f"through the upgrade gate."
        )

    # A plane correction is subtracted at the single choke point the
    # adoption gate and the shadow residuals share, against bounds of
    # order k*sqrt(sc^2+sr^2) ~ 5 ms.  A correction whose own sigma
    # exceeds that makes every comparison WORSE than leaving it alone.
    #
    # AC0G-B4 2026-08-25, content-convention window: the tracker reported
    # offset -25,846,958 ns with sigma 197,134,152 ns — honestly — and the
    # consumer applied it anyway, because this method took the measured
    # value whenever one existed and never looked at its uncertainty.
    # The cross-bench delta then wandered -16.877 -> +8.548 -> +5.815 ms
    # instead of sitting near a stable -16.6 ms.
    MAX_PLANE_SIGMA_NS = 5_000_000.0

    def _label_plane_in_force(self):
        """``(offset_ns, source, rejected_estimate_or_None)``.

        A measured term is used only when it is tighter than the
        comparison it perturbs; otherwise the configured value stands and
        the refused estimate is carried out so the published status can
        say why rather than leaving an operator to guess.
        """
        est = self._label_plane.estimate() if self.label_plane_measure else None
        if est is None:
            return self.label_plane_offset_ns, "config", None
        if float(est.sigma_ns) > self.MAX_PLANE_SIGMA_NS:
            return self.label_plane_offset_ns, "config", est
        return float(est.offset_ns), "measured", None

    def effective_label_plane_offset_ns(self) -> float:
        """The plane term in force: measured when it has earned it.

        With chain-delay learning on, the tracker behind it sees no T6
        reading, so this is the term for the other label tiers (T3); a
        T6 pair takes the stored record or the configured value instead
        (see ``_cross_bench_delta_ns``).
        """
        return self._label_plane_in_force()[0]

    def label_plane_status(self) -> Dict:
        """Auditable view of the term ACTUALLY APPLIED.

        Reports the term in force, not merely the term available: saying
        "measured" about an estimate that was refused would defeat the
        point of publishing it.  A refused estimate is carried alongside
        so the reason is visible.
        """
        offset, source, rejected = self._label_plane_in_force()
        est = self._label_plane.estimate() if self.label_plane_measure else None
        out = {
            "source": source,
            "offset_ns": round(float(offset), 1),
            "sigma_ns": (round(float(est.sigma_ns), 1)
                         if (est is not None and source == "measured")
                         else None),
            "n": int(est.n) if (est is not None and source == "measured") else 0,
            # How far the term WANDERS, distinct from how well its centre
            # is known.  Published because the two answer different
            # questions and the old estimator conflated them, which is
            # what kept the measured term permanently refused.
            "spread_ns": (round(float(est.spread_ns), 1)
                          if (est is not None and source == "measured")
                          else None),
            "n_eff": (round(float(est.n_eff), 1)
                      if (est is not None and source == "measured") else None),
            "span_s": (round(float(est.span_s), 1)
                       if (est is not None and source == "measured") else 0.0),
            # Which label tiers this term governs.  With chain-delay
            # learning on, T6 has its own (the ``chain_delay`` block).
            "tiers": "non_T6" if self.chain_delay_learn else "all",
        }
        if rejected is not None:
            out["rejected_offset_ns"] = round(float(rejected.offset_ns), 1)
            out["rejected_sigma_ns"] = round(float(rejected.sigma_ns), 1)
            out["rejected_n"] = int(rejected.n)
            out["rejected_spread_ns"] = round(float(rejected.spread_ns), 1)
            out["rejected_n_eff"] = round(float(rejected.n_eff), 1)
            out["rejected_reason"] = (
                "sigma %.3f ms exceeds the %.3f ms bound: a correction "
                "looser than the comparison it perturbs makes it worse"
                % (float(rejected.sigma_ns) / 1e6,
                   self.MAX_PLANE_SIGMA_NS / 1e6)
            )
        return out

    def _cross_bench_delta_ns(
        self, cand: BenchReading, ref: BenchReading, mono_now: float
    ) -> float:
        """Candidate-minus-reference UTC disagreement (ns), both
        readings extrapolated to the same monotonic instant.

        Cross-plane pairs are corrected by the EXPECTED plane
        difference (``label_plane_offset_ns``) so only the genuine
        disagreement remains; same-plane pairs are never touched.
        This is the single choke point the adoption gate and the
        shadow residuals share.

        For a T6-versus-host pair with a stored chain delay in force, the
        plane term is the record's ``offset_ns`` (spec 2026-10-27 §4).
        Without a record, while learning is on, a T6 pair takes the
        configured value: the label-plane tracker then measures only T3,
        whose term says nothing about T6's chain.  Every other pair keeps
        today's term.
        """
        raw_ns = (cand.utc_at(mono_now) - ref.utc_at(mono_now)) * 1e9
        sign = ((1 if getattr(cand, "plane", "host") == "label" else 0)
                - (1 if getattr(ref, "plane", "host") == "label" else 0))
        if sign == 0:
            return raw_ns
        record = self._chain_record_for(cand, ref)
        if record is not None:
            plane_ns = float(record.offset_ns)
        elif (self.chain_delay_store is not None
                and self._is_t6_host_pair(cand, ref)):
            plane_ns = self.label_plane_offset_ns
        else:
            plane_ns = self.effective_label_plane_offset_ns()
        return raw_ns - plane_ns * sign

    # ── stored T6 chain delay (spec 2026-10-27 §4, §6) ────────────────

    @staticmethod
    def _is_t6_host_pair(a: BenchReading, b: BenchReading) -> bool:
        """One side T6 on the label plane, the other on the host plane."""
        def t6_label(r: BenchReading) -> bool:
            return r.tier == "T6" and getattr(r, "plane", "host") == "label"

        def host(r: BenchReading) -> bool:
            return getattr(r, "plane", "host") == "host"

        return (t6_label(a) and host(b)) or (t6_label(b) and host(a))

    def _chain_record_for(
        self, cand: BenchReading, ref: BenchReading
    ) -> Optional[ChainDelayRecord]:
        """The stored record that governs this pair, or None.

        None when learning is off, when no record exists, or when the
        pair is not T6 against a host-plane bench.
        """
        if self.chain_delay_store is None:
            return None
        if not self._is_t6_host_pair(cand, ref):
            return None
        return self.chain_delay_store.record

    def _capped_sigmas_ns(
        self, cand: BenchReading, ref: BenchReading
    ) -> Tuple[float, float]:
        """Each bench's sigma, capped at what ITS OWN tier can deliver.

        See CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER -- the published sigmas
        themselves are never touched, only their weight in a bound.  A
        flat ceiling across all tiers would trim a legitimately wide WAN
        bench as hard as a broken T6; the per-tier form trims only a
        bench claiming an uncertainty its tier cannot produce.
        """
        return (
            min(cand.sigma_ns, cross_bench_sigma_ceiling_ms(cand.tier) * 1e6),
            min(ref.sigma_ns, cross_bench_sigma_ceiling_ms(ref.tier) * 1e6),
        )

    def _chain_band_ns(self, cand: BenchReading, ref: BenchReading) -> float:
        """k_x * sqrt(s_c^2 + s_l^2 + s_stored^2 + spread^2), ns.

        s_c and s_l carry today's per-tier caps.  The stored sigma and
        spread come from the record in force for this pair; without one
        they contribute nothing and the band equals today's bound.  The
        spread term admits the wander the station really has.  The change
        alarm and the held-T6 check use this same band (spec §5: one
        bound, not two).
        """
        sigma_c_ns, sigma_l_ns = self._capped_sigmas_ns(cand, ref)
        record = self._chain_record_for(cand, ref)
        sigma_s_ns = float(record.sigma_ns) if record is not None else 0.0
        spread_ns = float(record.spread_ns) if record is not None else 0.0
        return self.cross_bench_k * math.sqrt(
            sigma_c_ns ** 2 + sigma_l_ns ** 2 + sigma_s_ns ** 2 + spread_ns ** 2
        )

    def _t6_gross_ok(
        self, cand: BenchReading, ref: BenchReading, delta_raw_ns: float
    ) -> bool:
        """|delta_raw| <= chain_delay_max_ns + k_x * s_l (spec §6).

        ``delta_raw_ns`` is T6 minus the lower tier with NO plane term;
        s_l is the lower (non-T6) bench's capped sigma.  Holds with or
        without a record, so a wrong second or a sidelobe never reaches
        the band arithmetic.
        """
        return abs(delta_raw_ns) <= self._t6_gross_bound_ns(cand, ref)

    def _t6_gross_bound_ns(
        self, cand: BenchReading, ref: BenchReading
    ) -> float:
        """chain_delay_max_ns + k_x * s_l, s_l the non-T6 bench's capped
        sigma."""
        lower = ref if cand.tier == "T6" else cand
        sigma_l_ns = min(
            lower.sigma_ns, cross_bench_sigma_ceiling_ms(lower.tier) * 1e6)
        return self.chain_delay_max_ns + self.cross_bench_k * sigma_l_ns

    def _cross_gate_ok_locked(
        self, cand: BenchReading, ref: BenchReading, mono_now: float
    ) -> bool:
        """One gate evaluation: |delta| <= k_x * sqrt(s_c^2 + s_l^2).

        A T6-versus-host pair, while chain-delay learning is on, first
        meets the gross bound on the raw delta (spec §6); then, with a
        stored record, the band of :meth:`_chain_band_ns` around the
        stored offset (spec §4).  Without a record it meets today's
        bound.  Every other pair is unchanged.

        On failure the conflict flag is recorded/refreshed and a
        rate-limited CRITICAL names both tiers and the delta; on
        success any conflict blaming this candidate is cleared (the
        advance window then restarts cleanly at the caller).
        """
        delta_ns = self._cross_bench_delta_ns(cand, ref, mono_now)
        sigma_c_ns, sigma_l_ns = self._capped_sigmas_ns(cand, ref)
        # Today's bound; kept for the CRITICAL text even when a record
        # supplies the operative band.
        bound_ns = self.cross_bench_k * math.sqrt(
            sigma_c_ns ** 2 + sigma_l_ns ** 2
        )
        record = self._chain_record_for(cand, ref)
        limit_ns = (self._chain_band_ns(cand, ref) if record is not None
                    else bound_ns)
        gross_ok = True
        raw_ns = 0.0
        if (self.chain_delay_store is not None
                and self._is_t6_host_pair(cand, ref)):
            raw_ns = (cand.utc_at(mono_now) - ref.utc_at(mono_now)) * 1e9
            gross_ok = self._t6_gross_ok(cand, ref, raw_ns)
        if gross_ok and abs(delta_ns) <= limit_ns:
            if (self._cross_conflict is not None
                    and self._cross_conflict.get("upper") == cand.tier):
                logger.warning(
                    f"OffsetJudge cross-bench gate: candidate {cand.tier} "
                    f"agrees with {ref.tier} again "
                    f"(delta={delta_ns/1e6:+.3f}ms within "
                    f"{limit_ns/1e6:.3f}ms) — advance window restarts"
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
        # A refused T6 is a standing condition, not a fresh fault each
        # tick.  With no record it is what a station still learning its
        # chain delay expects (spec §3); with one, it stands until T6
        # returns or an operator answers.  Each logs once, then hourly,
        # on its own clock; entering one condition re-arms the other.
        # Every other pair keeps today's cadence.
        t6_chain_pair = (self.chain_delay_store is not None
                         and self._is_t6_host_pair(cand, ref))
        if t6_chain_pair and record is None:
            self._last_record_conflict_log = None
            due = self._standing_conflict_due_locked(
                "_last_learning_conflict_log", mono_now)
        elif t6_chain_pair:
            self._last_learning_conflict_log = None
            due = self._standing_conflict_due_locked(
                "_last_record_conflict_log", mono_now)
        else:
            due = (mono_now - self._last_cross_critical_log
                   >= self.critical_log_interval_s)
            if due:
                self._last_cross_critical_log = mono_now
        if due:
            logger.critical(
                f"OFFSET JUDGE CROSS-BENCH CONFLICT: candidate {cand.tier} "
                f"disagrees with trusted {ref.tier} by "
                f"{delta_ns/1e6:+.3f} ms (delta_ns={delta_ns:+.0f}), bound "
                f"k_x*sqrt(sigma_c^2+sigma_l^2) [sigma capped at each "
                f"tier's own budget: {cand.tier} "
                f"{cross_bench_sigma_ceiling_ms(cand.tier):.1f} ms, "
                f"{ref.tier} "
                f"{cross_bench_sigma_ceiling_ms(ref.tier):.1f} ms] = "
                f"{self.cross_bench_k:.1f} x "
                f"{math.sqrt(sigma_c_ns**2 + sigma_l_ns**2)/1e6:.3f} ms "
                f"= {bound_ns/1e6:.3f} ms — advancement BLOCKED, judging "
                f"stays on {ref.tier}; the rejected bench stays under "
                f"shadow measurement (shadow_residuals in offset_judge.json)."
                + self._chain_conflict_suffix(
                    record, limit_ns, gross_ok, raw_ns, cand, ref)
            )
        return False

    def _standing_conflict_due_locked(self, attr: str, mono_now: float) -> bool:
        """Once, then every CHAIN_LEARNING_CONFLICT_LOG_INTERVAL_S, on the
        timer named ``attr`` (None = not logged under this condition)."""
        last = getattr(self, attr)
        due = (last is None
               or mono_now - last >= CHAIN_LEARNING_CONFLICT_LOG_INTERVAL_S)
        if due:
            setattr(self, attr, mono_now)
        return due

    def _chain_conflict_suffix(
        self,
        record: Optional[ChainDelayRecord],
        limit_ns: float,
        gross_ok: bool,
        raw_ns: float,
        cand: BenchReading,
        ref: BenchReading,
    ) -> str:
        """What the CRITICAL conflict line adds for a T6 chain-delay pair.

        Empty for every other pair, so their line reads as before.
        """
        parts = []
        if not gross_ok:
            parts.append(
                f" T6 GROSS BOUND: raw delta {raw_ns/1e6:+.3f} ms exceeds "
                f"chain_delay_max + k_x*sigma_l = "
                f"{self._t6_gross_bound_ns(cand, ref)/1e6:.3f} ms."
            )
        if record is not None:
            parts.append(
                f" Stored T6 chain delay ({record.state}) "
                f"{float(record.offset_ns)/1e6:+.3f} ms in force; delta is "
                f"measured from it, band "
                f"k_x*sqrt(sigma_c^2+sigma_l^2+sigma_stored^2+spread^2) = "
                f"{limit_ns/1e6:.3f} ms; this line repeats hourly while "
                f"the conflict stands."
            )
        elif (self.chain_delay_store is not None
                and self._is_t6_host_pair(cand, ref)):
            progress = self._learning_progress()
            parts.append(
                f" No stored T6 chain delay yet: learning, "
                f"{progress['span_s']:.0f} s of {progress['target_s']:.0f} s "
                f"(chain_delay_learn_s) from n={progress['n']} healthy "
                f"readings; this line repeats hourly until a record exists."
            )
        return "".join(parts)

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

    def _observe_label_plane_locked(
        self, readings: List[BenchReading], mono_now: float
    ) -> None:
        """Pair this tick's label-plane bench with the tightest
        host-plane one and feed the plane tracker.

        The tightest host bench is chosen deliberately: the measurement
        can only be as good as the clock it is referenced to, and the
        tracker refuses anything looser than its host-sigma bound.
        """
        # Ruling B, N-1 (2026-10-30): an unadmitted T3 reading never
        # feeds the tracker.  With chain-delay learning off, that tracker
        # sets the plane term behind every T6-versus-host residual, so T3
        # could convict T6 and widen the HPPS precision sent to chrony.
        # With learning on it would read T3's own offset from the host
        # back as T3's plane term: a calibration against the host clock.
        labels = [r for r in readings
                  if getattr(r, "plane", "host") == "label"
                  and not self._t3_closed(r.tier)]
        hosts = [r for r in readings
                 if getattr(r, "plane", "host") == "host"]
        if labels and hosts:
            host = min(hosts, key=lambda r: float(r.sigma_ns))
            for label in labels:
                self.observe_label_plane(label, host)
        if self._t6_learner is not None:
            # Every tick, paired or not: the change alarm's sustain and
            # return timers must see a witness that has gone quiet.  The
            # trackers expire at the tick's instant too, since observe()
            # alone would leave a quiet witness frozen at its last window
            # and the alarm acting on readings that no longer describe T6.
            for tracker in (self._t6_learner, self._t6_witness,
                            self._t6_candidate_learner):
                if tracker is not None:
                    tracker.expire(mono_now)
            self._maybe_confirm_learn_locked()
            self._evaluate_chain_change_locked(mono_now)
            self._maybe_write_candidate_locked()


    def _select_label_anchor_locked(
        self, readings: List[BenchReading]
    ) -> None:
        """Hold this tick's label-plane anchor statement (spec §11.2).

        Deliberately independent of ``_select_bench_locked``: the FUSE
        feed needs the LABEL PLANE, and the tier arbitration may well
        have adopted a host-plane bench instead (hf_acquired is a witness
        there, by mjh's ruling of 2026-09-07).  Publishing "whichever
        bench won" would put the host clock back in the loop through the
        side door — the exact circularity §11 exists to break.

        Highest tier first, then the tightest sigma: T6's native anchor
        outranks the T3 registration when both stand up, which is the
        one-station-one-registration rule (mjh, 2026-09-04).

        A label-plane reading that is not AUTHORITATIVE is skipped (fix
        round 1, C-1).  ``HfAcquiredBench`` stamps
        ``detail["authoritative"]`` from
        ``registration_store.registration_refusal`` — THE one gate, the
        same predicate the ring, §18 and the sidecar ask — so a plane
        those three refuse can no longer steer the host clock instead.
        On a T6 station a WITNESS-state registration is a witness and
        nothing more: the FUSE feed's label plane is T6's own, or none.
        Readings without the key (T6's ``NativeAnchorBench``) default to
        authoritative; T6's own authority gate governs those.
        """
        # Ruling B (2026-10-30): an unadmitted T3 is never the anchor,
        # authoritative or not.
        labels = [r for r in readings
                  if getattr(r, "plane", "host") == "label"
                  and (r.detail or {}).get("authoritative", True)
                  and not self._t3_closed(r.tier)]
        if not labels:
            self._label_anchor = None
            return
        self._label_anchor = min(
            labels,
            key=lambda r: (self._tier_rank(r.tier), float(r.sigma_ns)),
        )

    def _label_anchor_block_locked(self, mono_now: float) -> Optional[Dict]:
        """The published ``label_plane_anchor`` block, or None.

        ``utc`` and ``mono`` together ARE the measurement pair: the
        anchor's UTC for the newest arrived sample, and the monotonic
        instant that sample arrived.  A consumer pairs ``mono`` with its
        own ``time.monotonic()``/``time.time()`` to recover what the host
        clock read at that instant — and nothing else is needed to state
        the host's error.  See :func:`label_plane_chrony_sample`.
        """
        r = self._label_anchor
        if r is None:
            return None
        detail = r.detail or {}
        bench = detail.get("bench")
        if not bench:
            bench = ("t6_native_anchor" if r.tier == "T6"
                     else f"{r.tier}_label_plane")
        return {
            "bench": str(bench),
            "tier": str(r.tier),
            "plane": "label",
            "utc": float(r.utc),
            "mono": float(r.mono),
            "sigma_ns": round(float(r.sigma_ns), 1),
            "age_s": round(mono_now - float(r.mono), 3),
        }

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
        # The dissent witnesses: the same rows, less every shadow-only
        # bench.  Final review I-1 (2026-10-30): the stand-alone T3
        # registration may sit any distance from T6 until stage 0 settles
        # the chain delay, and two witnesses can convict.  Joined by T4,
        # it widened the HPPS precision chrony sees.  Michael's ruling:
        # T3 votes nowhere until step 7 admits it.  Its row stays in
        # shadow_residuals for the record.
        voters: Dict[str, Dict] = {}
        if adopted is not None:
            for r in readings:
                if r.tier == adopted.tier:
                    continue
                row = {
                    "shadow_residual_ns": round(
                        self._cross_bench_delta_ns(r, adopted, mono_now), 1
                    ),
                    "sigma_ns": round(r.sigma_ns, 1),
                    "vs_tier": adopted.tier,
                }
                shadows[r.tier] = row
                if (r.detail or {}).get("bench") in self.SHADOW_ONLY_BENCHES:
                    continue
                voters[r.tier] = row
        self._shadow_residuals = shadows
        # hf-timestd#29: give the witnesses an actuator.  The shadow
        # channel has been RIGHT and IGNORED repeatedly (~5,000
        # CRITICAL/day for four days; 3.4 h at 26 ms on 2026-08-25),
        # because cross_bench_conflict gates tier ADVANCEMENT and the
        # adopted bench is already top tier -- nothing above it to
        # refuse.  Dissent needs no ladder: it only asserts "the bench is
        # at least this wrong", which becomes a published sigma floor.
        self._dissent = None
        if adopted is not None:
            try:
                from .witness_dissent import from_shadow_residuals
                d = from_shadow_residuals(voters, float(adopted.sigma_ns))
                self._dissent = self._dissent_watch.observe(d, mono_now)
            except Exception:  # noqa: BLE001 — a guard must not crash the judge
                self._dissent = None

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
        plane_ns = self._plane_expected_ns(self._best)
        plane_sigma_ns = self._plane_sigma_ns(self._best)
        residual_ns = st.ema_offset_ns - plane_ns
        bound_ns = self.k * math.sqrt(
            sigma_ns * sigma_ns + plane_sigma_ns * plane_sigma_ns)
        if abs(residual_ns) > bound_ns:
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
                f"offset={st.ema_offset_ns/1e9:+.3f}s "
                f"(plane {plane_ns/1e9:+.4f}s removed ⇒ residual "
                f"{residual_ns/1e9:+.4f}s) exceeds "
                f"{self.k:.0f}x sigma ({bound_ns/1e9:.4f}s, tier "
                f"{self._best.tier}) sustained >{self.sustain_window_s:.0f}s "
                f"— labels remain CORRECTED (segment {st.segment_id}); "
                f"radiod's advertised epoch is contradicted."
            )

    # ── plane term at the violation choke point ─────────────────────────
    #
    # A source's offset is (bench − radiod).  radiod's advertised epoch
    # always sits in the host plane; the bench may not.  Under the
    # content-time convention a label-plane bench reads one processing
    # interval EARLIER by definition, so comparing the two without
    # removing that term reports a definition as a fault -- which is what
    # AC0G-B4 did, ~295 CRITICALs an hour, all one sign, all five
    # channels at once.  The cross-bench gate already corrected for this
    # through _cross_bench_delta_ns; these are the same term at the other
    # choke point.

    def _plane_expected_ns(self, bench) -> float:
        """Expected (bench − radiod) purely from the plane difference.

        ⛔ DEFAULTS OFF, and the default is the finding.  This correction
        was added on the reasoning that radiod's advertised epoch sits in
        the host plane, so a label-plane bench should differ from it by Λ.
        AC0G-B4 refuted that within fifteen minutes of deployment: the raw
        offset against T6 read +1 ms and against T4 +4 ms, so radiod
        agrees with BOTH benches and no Λ-sized gap exists in this
        comparison at all.  ``st.radiod_utc_now()`` derives UTC from
        radiod's own RTP/GPS pair -- radiod's frame, not the host clock --
        and that is the frame T6 anchors in.

        The tracker measures (label bench − HOST CLOCK).  This test
        compares (bench − RADIOD).  Different pairs, and only the first
        has a plane gap in it.  Applying the term here injected an 18.8 ms
        error and manufactured the violations it was meant to remove.

        The seam and its tests stay so the mechanism is available if a
        station is ever shown to need it, but nothing applies it until
        that evidence exists.
        """
        if not self.violation_plane_correction:
            return 0.0
        if getattr(bench, "plane", "host") != "label":
            return 0.0
        return float(self.effective_label_plane_offset_ns())

    def _plane_sigma_ns(self, bench) -> float:
        """Uncertainty imported by subtracting the plane term.

        Zero for a same-plane comparison (nothing was subtracted) and
        zero for a configured constant, whose uncertainty we do not
        know and must not invent.  A measured term carries its own.
        """
        if not self.violation_plane_correction:
            return 0.0
        if getattr(bench, "plane", "host") != "label":
            return 0.0
        offset, source, _rejected = self._label_plane_in_force()
        if source != "measured":
            return 0.0
        est = self._label_plane.estimate()
        return float(est.sigma_ns) if est is not None else 0.0

    def _violates(self, offset_ns: float, bench,
                  plane_sigma_ns: Optional[float] = None) -> bool:
        """Does this offset exceed k*sigma once the plane term is removed?

        Exposed as its own method so the rule is testable without driving
        a full tick, and so both callers cannot drift apart.
        """
        sigma_ns = max(float(bench.sigma_ns), 1.0)
        if plane_sigma_ns is None:
            plane_sigma_ns = self._plane_sigma_ns(bench)
        residual = float(offset_ns) - self._plane_expected_ns(bench)
        bound = self.k * math.sqrt(
            sigma_ns * sigma_ns + float(plane_sigma_ns) ** 2)
        return abs(residual) > bound

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

    def _chain_delay_status_locked(self) -> Dict:
        """The published ``chain_delay`` block.

        ``state`` reads "none" until a record exists (always, with
        learning off).  ``learning`` reports the learn under way while no
        confirmed record exists, and None otherwise.  ``changed`` carries
        ``{stored_ns, live_ns, since_utc}`` while a change alarm stands.
        """
        store = self.chain_delay_store
        record = store.record if store is not None else None
        candidate = store.candidate if store is not None else None
        return {
            "state": record.state if record is not None else "none",
            "record": record.to_dict() if record is not None else None,
            "candidate": (candidate.to_dict()
                          if candidate is not None else None),
            "changed": (dict(self._chain_changed)
                        if self._chain_changed is not None else None),
            "learning": (self._learning_progress()
                         if self._chain_learning_active() else None),
        }

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
            # PRINCIPLES.md.
            #
            # Task 14a (mjh, 2026-09-07): where a label-plane native
            # anchor is in force, THAT ANCHOR is the only source of
            # RTP→UTC on every published surface, and §18 is a published
            # surface.  So utc_anchor_ns is the anchor's own UTC for
            # rtp_anchor_sample, by pure counter arithmetic, and
            # tier/sigma_ns report the ANCHOR's provenance and claim.
            # radiod's host-stamped pair does not enter, and neither does
            # this judge's correction — the judge is a witness here.
            #
            # Legacy (no label plane): the judge-corrected pair as
            # before, null until a verdict exists so a subscriber can
            # never mistake a raw radiod mapping for a judged one.  With
            # an anchor in force the value is published whether or not a
            # verdict exists: the anchor does not need the judge to be
            # true.  plane_source (additive) names which of the two a
            # subscriber is reading.
            label = self._label_anchor_for_locked(st)
            if label is not None:
                utc_anchor_ns = int(label.utc_ns_at(st.rtp_timesnap))
                anchor_tier = label.tier
                anchor_sigma_ns = round(float(label.sigma_ns), 1)
                anchor_rate = int(label.sample_rate_hz)
                plane_source = ("t6_native" if label.tier == "T6"
                                else "t3_registration")
            else:
                utc_anchor_ns = (
                    int(round(st.gps_unix * 1e9 + v.offset_ns))
                    if v is not None else None
                )
                anchor_tier = v.tier if v is not None else None
                anchor_sigma_ns = (
                    round(v.sigma_ns, 1) if v is not None else None
                )
                anchor_rate = st.sample_rate
                plane_source = "radiod_pair_judged"
            contract_sources[self._key_str(key)] = {
                "utc_anchor_ns": utc_anchor_ns,
                "tier": anchor_tier,
                "sigma_ns": anchor_sigma_ns,
                "snapshot_age_s": (
                    round(v.judge_age_s, 3) if v is not None else None
                ),
                "rtp_anchor_sample": st.rtp_timesnap,
                "rate_samples_per_utc_sec": anchor_rate,
                # Additive (task 14a): "t3_registration" | "t6_native" |
                # "radiod_pair_judged" — which plane produced
                # utc_anchor_ns.  A subscriber that cares whether it is
                # reading a host-clock-free plane reads this.
                "plane_source": plane_source,
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
            "schema": PUBLISH_SCHEMA,
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
            # The (label − host) plane term in force this tick, and
            # where it came from ("measured" | "config").  Published so
            # the correction applied to every cross-plane comparison is
            # auditable rather than implicit.
            "label_plane": self.label_plane_status(),
            # The station's stored T6 chain delay (spec 2026-10-27 §2,
            # §4): the record the gate measures T6 from, any candidate
            # beside it, the change alarm and the learner's progress.
            "chain_delay": self._chain_delay_status_locked(),
            # Spec §11.2 (2026-09-07): the label plane's own (UTC,
            # monotonic) statement, for the FUSE chrony feed in the
            # fusion process.  Published independently of tier
            # arbitration — see _select_label_anchor_locked.
            # Task 17a: the closure is opt-in, so the block lands under
            # ``label_plane_anchor`` (which the FUSE feed reads) only
            # while the closure is on, and under
            # ``label_plane_witness`` -- which nothing acts on -- while
            # it is off.  Exactly one of the two is ever populated.
            LABEL_PLANE_ANCHOR_KEY: (
                self._label_anchor_block_locked(mono_now)
                if self.anchor_closure
                else None
            ),
            LABEL_PLANE_WITNESS_KEY: (
                None
                if self.anchor_closure
                else self._label_anchor_block_locked(mono_now)
            ),
            # Precision non-regression clause: a voluntary upgrade
            # currently refused because the candidate's sigma would
            # materially regress the judge's precision (None when
            # clear).  The refused bench stays in shadow_residuals.
            "precision_hold": self._precision_hold,
            "shadow_residuals": self._shadow_residuals,
            # hf-timestd#29: the witnesses' collective verdict on the
            # ADOPTED bench, once sustained.  None when they do not
            # convict.  `sigma_floor_ns` is the smallest sigma the bench
            # may honestly publish — if independent witnesses that agree
            # with each other put it 26 ms away, a 0.8 ms error bar is a
            # false statement whatever produced it.
            "witness_dissent": (
                None if self._dissent is None else {
                    "implied_error_ns": round(
                        self._dissent.implied_error_ns, 1),
                    "sigma_floor_ns": round(
                        self._dissent.sigma_floor_ns, 1),
                    "concurring_tiers": list(self._dissent.tiers),
                    "spread_ns": round(self._dissent.spread_ns, 1),
                }
            ),
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
            # Task 14 (T3 foundations): T3's registration against T6's
            # anchor at one input sample, through both channels' counter
            # maps (T3ShadowComparator).  ``t3_minus_t6_ms`` and
            # ``t6_chain_delay_ms`` both stay raw.  Null without a provider
            # or when it fails.  Read-only: nothing acts on it.
            "t3_shadow": (dict(self._t3_shadow)
                          if self._t3_shadow is not None else None),
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
                    "surface: utc_anchor_ns is the label-plane anchor's "
                    "own UTC of rtp_anchor_sample when one is in force "
                    "(plane_source t3_registration | t6_native), else the "
                    "judge-corrected radiod pair (plane_source "
                    "radiod_pair_judged, null until a verdict exists); "
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


# ────────────────────────────────────────────────────────────────────
# The FUSE chrony feed reads the label-plane anchor (spec §11.2)
#
# AC0G-ND, 2026-09-07: the FUSE sample was ``system_time - d_clock``.
# d_clock is fusion's estimate of the host clock's offset from the plane
# its measurements were LABELLED on, and radiod stamps GPS_TIME from the
# host clock -- so plane and host were the same object and d_clock ~ 0
# voted "the host is right" while the host walked 150 ms from four NTP
# witnesses.  A host-relative measurement cannot detect a host-wide
# error; no amount of correction added to d_clock changes that.
#
# The label-plane anchor already forms the pair chrony actually wants,
# and forms it without consulting any clock:
#
#     reference_time = anchor's UTC of the newest arrived sample
#     system_time    = what the host clock read AT THAT ARRIVAL
#
# chrony computes offset = clockTimeStamp - receiveTimeStamp
# = reference - system (traced through refclock_shm.c in chrony_shm.py),
# which is exactly the host's error against the tick-aligned UTC.
#
# ⚠ WHY NOTHING IS DOUBLE-COUNTED.  d_clock does not appear in this
# sample at all -- not added, not subtracted.  The two are alternative
# ANSWERS to one question ("what is the host clock's error?"), not two
# terms of a sum: d_clock answers it against the ring plane, the anchor
# answers it against the tick train.  In this regime d_clock becomes a
# diagnostic (their difference is the ring plane's residual against the
# registration, published as ``anchor_offset_ms`` beside
# ``d_clock_fused_ms`` in fusion_status.json).  A reader tempted to sum
# them should read this paragraph twice.
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LabelPlaneAnchorSample:
    """One chrony SHM sample derived from the label-plane anchor."""

    bench: str
    tier: str
    reference_time: float   # true UTC of the arrival (the anchor's label)
    system_time: float      # host clock at that same arrival instant
    sigma_ns: float
    age_s: float
    # The published arrival's own monotonic stamp.  Identity of the
    # MEASUREMENT: the fusion loop cycles faster than the judge ticks, so
    # a consumer must not hand chrony the same arrival twice under two
    # (reference, system) pairs -- correlated samples offered as
    # independent ones understate the refclock's dispersion.
    anchor_mono: float = 0.0

    @property
    def offset_s(self) -> float:
        """What chrony will compute: reference - system.

        Negative means the host clock ran FAST at the arrival instant and
        chrony slews it backwards.
        """
        return self.reference_time - self.system_time

    @property
    def shm_precision(self) -> int:
        """log2(sigma in seconds), clamped to chrony's useful range.

        Same clamp the legacy d_clock feed applies ([-20, -4] = 1 us to
        62 ms): the sample must claim neither better than the instrument
        nor worse than chrony can use.
        """
        sigma_s = max(float(self.sigma_ns) / 1e9, 1e-9)
        return max(-20, min(-4, int(math.log2(sigma_s))))


def read_label_plane_anchor(
    path: os.PathLike = Path("/run/hf-timestd/offset_judge.json"),
    *,
    key: str = LABEL_PLANE_ANCHOR_KEY,
) -> Optional[Dict]:
    """The published ``label_plane_anchor`` block, or None.

    None on every failure mode a consumer must survive: no file, bad
    JSON, an unrecognised schema, no label plane this tick, or a block
    that does not actually claim the label plane.

    ``key`` names which block to read.  It defaults to the one the FUSE
    feed acts on, so ``LABEL_PLANE_WITNESS_KEY`` (task 17a) can only be
    reached by a caller that asks for it by name -- the offline sign
    analysis of task 17c, and nothing else.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != PUBLISH_SCHEMA:
        return None
    block = data.get(key)
    if not isinstance(block, dict) or not block:
        return None
    if str(block.get("plane")) != "label":
        return None
    return block


def label_plane_chrony_sample(
    path: os.PathLike = Path("/run/hf-timestd/offset_judge.json"),
    *,
    max_age_s: float = LABEL_ANCHOR_MAX_AGE_S,
    key: str = LABEL_PLANE_ANCHOR_KEY,
    time_fn: Callable[[], float] = time.time,
    mono_fn: Callable[[], float] = time.monotonic,
) -> Optional[LabelPlaneAnchorSample]:
    """Turn the published anchor into a chrony SHM sample, or None.

    ``system_time`` is recovered by walking the host clock BACK along the
    monotonic clock to the arrival instant.  CLOCK_MONOTONIC is
    system-wide on Linux, so the judge's ``mono`` is directly comparable
    here even though another process published it; /run is tmpfs, so a
    file cannot survive the reboot that would invalidate that.

    An arrival in the future, or older than ``max_age_s``, is refused:
    the interval is walked on the monotonic clock, which chrony itself
    slews, so a long projection re-imports the very frequency error the
    sample is meant to measure.
    """
    block = read_label_plane_anchor(path, key=key)
    if block is None:
        return None
    try:
        utc = float(block["utc"])
        mono = float(block["mono"])
        sigma_ns = float(block.get("sigma_ns") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    age_s = mono_fn() - mono
    if age_s < 0 or age_s > float(max_age_s):
        return None
    return LabelPlaneAnchorSample(
        bench=str(block.get("bench") or "label_plane"),
        tier=str(block.get("tier") or "T3"),
        reference_time=utc,
        system_time=time_fn() - age_s,
        sigma_ns=sigma_ns,
        age_s=age_s,
        anchor_mono=mono,
    )
