"""T5RtpPairing — pair LB-142x GPS truth against the RTP substrate.

Closes audit gap G5b (docs/OFFSET-JUDGE-SPEC-2026-08-05.md §2/§10 P2):
``t5_lbe1421.anchor_offset_ns`` was consumed by ``LbeT5DirectProbe``
but never produced, so the T6↔T5 cross-check silently compared radiod
against a hardcoded zero.  This class builds the real pairing product.

## What is measured

For the T6 channel (the stream whose RTP counter carries the BPSK PPS
substrate), radiod advertises a ``(GPS_TIME, RTP_TIMESNAP)`` mapping.
The LB-142x attests, via NMEA over the gpsdo-monitor JSON, the integer
UTC second of its PPS — direct GPS truth at integer-second precision.

    anchor_offset_ns = radiod_mapping(rtp_of_arrived_sample)
                       − GPS/NMEA-attested UTC of that sample

i.e. the RTP anchor's UTC prediction minus GPS truth — the same
"prediction − truth" (local − source) sign convention every other
``offset_ms`` in the authority stack uses, and exactly what
``LbeT5DirectProbe`` documents for the field it reads.

## How the pairing is grounded (and its honest limits)

The ideal T5 pairing uses a DCD/PPS edge capture: the exact host
instant of the GPS second boundary.  That capture is NOT available
in-process — gpsdo-monitor owns the serial device and publishes only
the integer second (see ``lb1421_t5_probe.py``).  The fallback,
per the spec's P2 mandate, derives the pairing from the **stream
arrival mapping** with its jitter honestly reflected in sigma:

  * The T6 sample callback records ``(rtp, monotonic)`` for the most
    recently arrived batch — an RTP counter value observed "now".
  * The NMEA attestation pins the host clock to the GPS integer-second
    grid (``Lb1421T5Probe`` refuses readings when host and NMEA
    disagree at the ±0.5 s level), so the host clock may serve as the
    *sub-second indexing signal* — the same sanctioned role it plays in
    the one-shot T6 disambiguation (see the architecture-compliance
    note in ``CoreRecorderV2._t6_disambiguate_via_t5_lb1421``).
  * The residual bias is the stream transport latency (radiod
    blocktime batching + resequencer + socket + callback scheduling):
    the arrived sample's true UTC precedes its arrival wall time by
    that latency.  It is bounded but not measurable without a DCD
    edge, so it lives in ``SIGMA_FLOOR_NS`` — the pairing never claims
    better than the latency budget.

A future DCD-grounded pairing would tighten sigma to the µs class; the
schema (offset + sigma) is unchanged by that upgrade.

Dependency policy: stdlib + the offset_judge helpers only (no ka9q, no
host-clock use beyond the sanctioned indexing role above) — the module
is system-python testable via the P1 namespace bootstrap.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple
from collections import deque

from .offset_judge import _rtp_delta_signed, gps_time_ns_to_unix


@dataclass(frozen=True)
class T5PairingProduct:
    """One evaluated T5 pairing: radiod-anchor-vs-GPS-truth disagreement.

    ``anchor_offset_ns``  prediction − truth (see module docstring).
    ``sigma_ns``          honest 1-sigma: max(latency floor, observed
                          spread of the raw measurements window).
    ``truth_utc``         GPS/NMEA-attested UTC of the arrival instant
                          (Unix s) — the T5 bench answer for the judge.
    ``arrival_mono``      monotonic clock at the paired sample's arrival.
    ``arrival_age_s``     age of that arrival at compute time.
    ``arrival_rtp``       the paired RTP counter value (32-bit).
    ``pps_utc_sec``       the NMEA integer second that attested the pairing.
    ``n_window``          raw measurements contributing to the median.
    ``source``            WHICH stream grounded the pairing — "t6" for the
                          dedicated BPSK PPS stream, "stream:<description>"
                          for an archive-stream fallback (P5 decoupling).
                          Carried so sigma accounting stays honest: the
                          grounding stream's arrival cadence and latency
                          are part of the product's pedigree.
    """

    anchor_offset_ns: int
    sigma_ns: float
    truth_utc: float
    arrival_mono: float
    arrival_age_s: float
    arrival_rtp: int
    pps_utc_sec: int
    n_window: int
    source: str = "t6"


class T5RtpPairing:
    """Arrival tracker + pairing evaluator (one instance per GROUNDING
    STREAM — the T6 BPSK stream when present, else any archive stream
    the recorder holds ChannelInfo + arrivals for; P5 decoupling).

    ``note_arrival`` is called from the grounding stream's sample
    callback (hot path — a single tuple assignment, no locking).
    ``compute`` is called from the status writer and the OffsetJudge T5
    bench (both slow paths).
    """

    # Latency budget of the stream-arrival pairing: radiod blocktime
    # batching (typ. 20 ms) + resequencer settling + socket/callback
    # scheduling.  The pairing never claims better than this.
    SIGMA_FLOOR_NS = 25_000_000.0

    # An arrival older than this means the stream stalled — refuse to
    # pair against it (the extrapolation would silently degrade).
    ARRIVAL_MAX_AGE_S = 5.0

    # NMEA attestation window: arrival_wall − pps_utc_sec must land in
    # [−1, horizon].  pps_utc_sec can be up to ~12 s stale (gpsdo-monitor
    # probe cadence, see lb1421_t5_probe.DEFAULT_NMEA_MAX_AGE_S) and the
    # attested host/GPS agreement is ±0.5 s.
    NMEA_DELAY_MIN_S = -1.0
    NMEA_DELAY_MAX_S = 20.0

    # A raw measurement this far from the window median means the radiod
    # counter space / epoch fractured — restart the window fresh (spec
    # §5: no smoothing across a fracture).
    WINDOW_RESET_STEP_NS = 5_000_000_000.0

    WINDOW_LEN = 32
    WINDOW_MAX_SPAN_S = 300.0

    def __init__(
        self,
        time_fn: Callable[[], float] = time.time,
        mono_fn: Callable[[], float] = time.monotonic,
        source: str = "t6",
    ):
        self._time = time_fn
        self._mono = mono_fn
        # Stream identity stamped on every product (P5 decoupling):
        # the pairing math only needs SOME live stream's (gps_time,
        # rtp_timesnap, sample_rate) mapping + arrival tracking, so
        # one T5RtpPairing instance exists per grounding stream and
        # names itself here.
        self.source = str(source)
        # (rtp, mono) of the most recent batch — written by the sample
        # callback, read everywhere.  Single-reference assignment keeps
        # the hot path lock-free (atomic under the GIL).
        self._arrival: Optional[Tuple[int, float]] = None
        self._lock = threading.Lock()
        # (mono, raw_offset_ns) history for median/spread filtering.
        self._window: Deque[Tuple[float, float]] = deque(maxlen=self.WINDOW_LEN)

    # ── hot path ─────────────────────────────────────────────────────

    def note_arrival(self, rtp: int, mono: Optional[float] = None) -> None:
        """Record the most recently arrived RTP counter value."""
        self._arrival = (int(rtp) & 0xFFFFFFFF,
                         self._mono() if mono is None else float(mono))

    @property
    def latest_arrival(self) -> Optional[Tuple[int, float]]:
        """(rtp, mono) of the most recent batch, or None."""
        return self._arrival

    # ── evaluation ───────────────────────────────────────────────────

    def compute(
        self,
        reading,
        gps_time_ns: int,
        rtp_timesnap: int,
        sample_rate: int,
    ) -> Optional[T5PairingProduct]:
        """Evaluate the pairing against a fresh NMEA reading.

        Args:
            reading: ``Lb1421Reading`` (must already be fresh + valid-fix
                — the caller uses ``get_latest()`` which gates both; a
                valid reading also attests host-wall/GPS integer-second
                consistency).
            gps_time_ns / rtp_timesnap: radiod's advertised pair for the
                T6 channel (listener-refreshed ``channel_info`` values).
            sample_rate: the T6 channel sample rate (Hz).

        Returns None when any ingredient is missing/stale — the caller
        emits the Phase-2A fallback (no ``anchor_offset_ns``).
        """
        if reading is None or gps_time_ns is None or rtp_timesnap is None:
            return None
        if not sample_rate or sample_rate <= 0:
            return None
        arrival = self._arrival
        if arrival is None:
            return None
        arrival_rtp, arrival_mono = arrival
        mono_now = self._mono()
        age = mono_now - arrival_mono
        if age < 0 or age > self.ARRIVAL_MAX_AGE_S:
            return None

        # Host wall time of the arrival instant.  Sanctioned indexing
        # role: the integer second is attested by NMEA (reading is only
        # valid when host and GPS agree at the ±0.5 s level); the
        # sub-second part rides chrony discipline; the transport
        # latency bias is carried in SIGMA_FLOOR_NS.
        arrival_wall = self._time() - age
        nmea_delay = arrival_wall - float(reading.pps_utc_sec)
        if not (self.NMEA_DELAY_MIN_S <= nmea_delay <= self.NMEA_DELAY_MAX_S):
            # Host wall and NMEA truth disagree beyond the attestable
            # window — refuse rather than emit a poisoned pairing.
            return None

        # radiod's UTC prediction for the arrived sample (pure counter
        # arithmetic against the advertised pair — the quantity under
        # judgment).
        pred_utc = (
            gps_time_ns_to_unix(int(gps_time_ns))
            + _rtp_delta_signed(int(arrival_rtp), int(rtp_timesnap))
            / float(sample_rate)
        )
        raw_ns = (pred_utc - arrival_wall) * 1e9

        with self._lock:
            if self._window:
                med_prev = self._median([w[1] for w in self._window])
                if abs(raw_ns - med_prev) > self.WINDOW_RESET_STEP_NS:
                    # Counter-space / epoch fracture — fresh estimate,
                    # no smoothing across it (spec §5).
                    self._window.clear()
            self._window.append((mono_now, raw_ns))
            while (self._window
                   and mono_now - self._window[0][0] > self.WINDOW_MAX_SPAN_S):
                self._window.popleft()
            raws = [w[1] for w in self._window]

        offset_ns = self._median(raws)
        spread_ns = self._robust_spread(raws, offset_ns)
        sigma_ns = max(self.SIGMA_FLOOR_NS, spread_ns)

        return T5PairingProduct(
            anchor_offset_ns=int(round(offset_ns)),
            sigma_ns=float(sigma_ns),
            truth_utc=float(arrival_wall),
            arrival_mono=float(arrival_mono),
            arrival_age_s=float(age),
            arrival_rtp=int(arrival_rtp),
            pps_utc_sec=int(reading.pps_utc_sec),
            n_window=len(raws),
            source=self.source,
        )

    # ── small numeric helpers (stdlib-only; window is tiny) ──────────

    @staticmethod
    def _median(values: List[float]) -> float:
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2:
            return s[mid]
        return 0.5 * (s[mid - 1] + s[mid])

    @classmethod
    def _robust_spread(cls, values: List[float], med: float) -> float:
        """1.4826 × MAD — Gaussian-consistent robust sigma."""
        if len(values) < 3:
            return 0.0
        mad = cls._median([abs(v - med) for v in values])
        return 1.4826 * mad
