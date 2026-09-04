"""Layer 4: turn a decoded WWVB frame into an L1 metrology row for Fusion.

This is the pure timing arithmetic and L1-row construction, deliberately kept
out of ``core_recorder_v2`` so it can be unit-tested without radiod, SQLite, or
the ka9q stack.  The core-recorder decode loop supplies the runtime plumbing
(the RTP->UTC closure, the receiver location, the SQLite writer); everything
that decides *what number to emit* lives here.

Timing convention (must match the HF metrology workers so the Fusion combiner
can pool WWVB with WWV/WWVH/BPM):

    timing_error_ms = (T_arrival_utc - decoded_minute_utc) * 1000
                      - expected_delay_ms

where

    T_arrival_utc   receiver UTC of the on-time mark, from the boundary sample's
                    RTP timestamp via radiod's GPSDO snapshot
                    (GPS_TIME / RTP_TIMESNAP), the same authoritative basis as
                    buffer_timing.sample0_utc and the HF metrology workers --
                    NOT the host wall clock (ARCHITECTURE.md s2);
    decoded_minute  the transmitter UTC of that minute boundary (exact :00),
                    from the protocol decode;
    expected_delay  the modelled WWVB propagation delay (wwvb_propagation).

This equals (true_prop - expected_prop) + clock_error, i.e. the receiver clock
error D_clock plus the propagation-model residual.  It carries the SAME sign as
``metrology_engine.py:1151`` (``timing_error_ms = raw_arrival - expected``), and
the Fusion combiner consumes it verbatim as D_clock
(``multi_broadcast_fusion.py:2043``).  Because T_arrival rides the radiod GPSDO
snapshot, the residual is already referenced to GPS truth; the
propagation-model error (and any residual constant offset, e.g. the WWVB
antenna-array geometry) is what the GPS-learned calibration absorbs, which is
why an UNCALIBRATED WWVB source is graded MARGINAL.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

from ..models.measurement import L1MetrologyMeasurement, QualityFlag, StationID
from .wwv_constants import SPEED_OF_LIGHT_KM_S
from .wwvb_propagation import wwvb_expected_delay

WWVB_FREQUENCY_MHZ = 0.060
WWVB_BROADCAST_ID = "WWVB_60"
WWVB_IDENTIFICATION_METHOD = "wwvb_decode"

# Reject decodes whose implied clock error is physically implausible -- almost
# certainly a buffer-anchor or boundary slip rather than a real measurement.
# Mirrors the +/-500 ms sanity gate the HF metrology path uses
# (metrology_engine.py ~1157).
WWVB_MAX_PLAUSIBLE_TIMING_ERROR_MS = 500.0

# Coarse screen on the DECODED MINUTE ITSELF, applied before a frame is counted
# at all.  Distinct from the gate above: that one needs an RTP anchor and so
# only runs on the Fusion path, leaving the default ledger-only configuration
# with no defence but parity.  On AC0G-B4 over 2026-08-17..19 that let 5 of 18
# accepted frames through with minutes in 2053 and 2097, one of them with
# parity_errors == 0 AND sync_errors == 0.
#
# 300 s is chosen to be useless as a correction and decisive against absurdity:
# comfortably past the worst-case decode latency (90 s buffer + 30 s tick) yet
# an order of magnitude under the 3600 s error a single flipped hour bit makes.
WWVB_MAX_PLAUSIBLE_MINUTE_SKEW_S = 300.0

# SNR floor (dB) below which even a clean-parity frame is graded MARGINAL.
WWVB_GOOD_SNR_DB = 10.0


def estimate_snr_db(per_second_iq) -> float:
    """Rough coherent-SNR proxy (dB) from a run of per-second mean-IQ symbols.

    Squaring removes the +/-1 BPSK modulation, so the time-aligned carrier
    survives averaging (``|mean(z^2)|``) while noise averages down; the residual
    ``mean(|z^2|) - |mean(z^2)|`` stands in for noise power.  This is a monotonic
    proxy, NOT a calibrated SNR -- good enough to weight a MARGINAL-graded WWVB
    source and to track reception quality, and documented as such.
    """
    z = np.asarray(per_second_iq, dtype=np.complex128)
    z = z[np.abs(z) > 0]
    if z.size < 4:
        return float("nan")
    z2 = z * z
    coherent = float(np.abs(np.mean(z2)))
    power = float(np.mean(np.abs(z2)))
    noise = power - coherent
    if noise <= 0:
        return 40.0
    snr = 10.0 * math.log10(coherent / noise)
    # Clamp to a sane display/weighting range.
    return float(max(-10.0, min(40.0, snr)))


def frame_minute_is_plausible(
    minute_of_frame: datetime,
    *,
    now: datetime,
    max_skew_s: float = WWVB_MAX_PLAUSIBLE_MINUTE_SKEW_S,
) -> bool:
    """Is this decoded minute anywhere near the time the receiver thinks it is?

    The host clock serves here strictly as a *sanity bench* -- an instrument for
    detecting absurdity -- never as a correction, which keeps this clear of the
    timing-authority invariant (the sanctioned judging role, cf. the Offset
    Judge).  Nothing derived from this comparison ever reaches the chrony feed;
    it only decides whether a frame is counted as a decode.

    Naive datetimes are treated as UTC.
    """
    if minute_of_frame.tzinfo is None:
        minute_of_frame = minute_of_frame.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return abs((minute_of_frame - now).total_seconds()) <= max_skew_s


def compute_timing_error_ms(
    *,
    arrival_utc_s: float,
    decoded_minute_utc: datetime,
    expected_delay_ms: float,
) -> float:
    """D_clock for one WWVB minute: (arrival - emitted_minute) - expected delay.

    See module docstring for the sign convention.  ``decoded_minute_utc`` is
    treated as UTC if it carries no tzinfo.
    """
    if decoded_minute_utc.tzinfo is None:
        decoded_minute_utc = decoded_minute_utc.replace(tzinfo=timezone.utc)
    minute_epoch_s = decoded_minute_utc.timestamp()
    observed_delay_ms = (arrival_utc_s - minute_epoch_s) * 1000.0
    return observed_delay_ms - expected_delay_ms


def leap_notice_from_frame(frame) -> Optional[str]:
    """Map a decoded WWVB frame's leap-second state to the L1 row's
    ``leap_second_notice`` ('none' | 'positive' | 'negative'), or None when
    the dst_ls word did not decode."""
    ls = getattr(frame, "leap_second", None)
    if ls is None:
        return None
    name = getattr(ls, "name", str(ls)).lower()
    return name if name in ("none", "positive", "negative") else None


def build_l1_row(
    *,
    detected_frame,  # wwvb_demod.DetectedFrame
    anchor_rtp: Optional[int],
    rtp_to_utc_s: Callable[[int], Optional[float]],
    rx_lat: float,
    rx_lon: float,
    snr_db: float,
    confidence: float,
    processing_version: str,
    learned_delay_ms: Optional[float] = None,
    learned_sigma_ms: Optional[float] = None,
) -> Optional[dict]:
    """Build one ``L1_metrology_measurements`` row dict for a decoded frame.

    Returns ``None`` (caller skips the row) when the frame cannot be turned into
    a trustworthy timing measurement: no RTP anchor, no boundary sample, the
    rtp->utc mapping is unavailable, or the implied clock error is implausible.

    The returned dict is what ``SqliteDataProductWriter.write_measurement``
    expects; ``raw_toa_ms`` carries ``timing_error_ms`` (the D_clock), per the
    Fusion read contract (``multi_broadcast_fusion.py:2024``).
    """
    if anchor_rtp is None:
        return None
    boundary_sample = getattr(detected_frame, "boundary_sample", float("nan"))
    if boundary_sample is None or math.isnan(boundary_sample):
        return None

    boundary_rtp = (int(anchor_rtp) + int(round(boundary_sample))) & 0xFFFFFFFF
    arrival_utc_s = rtp_to_utc_s(boundary_rtp)
    if arrival_utc_s is None:
        return None

    delay = wwvb_expected_delay(
        rx_lat,
        rx_lon,
        learned_delay_ms=learned_delay_ms,
        learned_sigma_ms=learned_sigma_ms,
    )
    decoded_minute = detected_frame.frame.minute_of_frame
    timing_error_ms = compute_timing_error_ms(
        arrival_utc_s=arrival_utc_s,
        decoded_minute_utc=decoded_minute,
        expected_delay_ms=delay.delay_ms,
    )
    if not math.isfinite(timing_error_ms):
        return None
    if abs(timing_error_ms) > WWVB_MAX_PLAUSIBLE_TIMING_ERROR_MS:
        return None

    if decoded_minute.tzinfo is None:
        decoded_minute = decoded_minute.replace(tzinfo=timezone.utc)
    light_travel_time_ms = delay.distance_km / SPEED_OF_LIGHT_KM_S * 1000.0

    # An uncalibrated WWVB source is at best MARGINAL -- the propagation delay
    # carries multi-ms uncertainty until a GPS-learned value is supplied.  This
    # is the honest grade that down-weights it in the combiner; calibration is
    # what unlocks GOOD.
    good = delay.calibrated and math.isfinite(snr_db) and snr_db >= WWVB_GOOD_SNR_DB
    quality_flag = QualityFlag.GOOD if good else QualityFlag.MARGINAL

    row = L1MetrologyMeasurement(
        timestamp_utc=decoded_minute.isoformat(),
        minute_boundary_utc=int(decoded_minute.timestamp()),
        rtp_timestamp=boundary_rtp,
        station_id=StationID.WWVB,
        frequency_mhz=WWVB_FREQUENCY_MHZ,
        raw_toa_ms=timing_error_ms,
        tone_detected=True,
        snr_db=float(snr_db) if math.isfinite(snr_db) else float("nan"),
        doppler_hz=None,
        identification_method=WWVB_IDENTIFICATION_METHOD,
        identification_confidence=float(confidence),
        distance_km=delay.distance_km,
        light_travel_time_ms=light_travel_time_ms,
        quality_flag=quality_flag,
        processing_version=processing_version,
        leap_second_notice=leap_notice_from_frame(detected_frame.frame),
    )
    return row.model_dump()


__all__ = [
    "WWVB_FREQUENCY_MHZ",
    "WWVB_BROADCAST_ID",
    "WWVB_MAX_PLAUSIBLE_TIMING_ERROR_MS",
    "estimate_snr_db",
    "compute_timing_error_ms",
    "build_l1_row",
    "leap_notice_from_frame",
]
