"""Layer-4 (WWVB -> Fusion source pool) unit tests.

Covers the pure timing arithmetic and L1-row construction in
``hf_timestd.core.wwvb_fusion`` plus the decoder's ``boundary_sample``
exposure.  All synthesized-signal driven; no radiod, SQLite, or chunk files.
"""

import datetime as dt
import math

import numpy as np
import pytest

from hf_timestd.core import wwvb_protocol as P
from hf_timestd.core.wwvb_demod import decode_iq, synthesize_wwvb_iq
from hf_timestd.core.wwvb_propagation import (
    WWVB_NOMINAL_SIGMA_MS,
    wwvb_expected_delay,
)
from hf_timestd.core.wwvb_fusion import (
    WWVB_MAX_PLAUSIBLE_TIMING_ERROR_MS,
    build_l1_row,
    compute_timing_error_ms,
    estimate_snr_db,
)
from hf_timestd.models.measurement import L1MetrologyMeasurement, StationID

SR = 24000.0
RX_LAT, RX_LON = 38.3, -90.9  # ~St. Louis: ~1240 km from Fort Collins


def _decode_one(minute):
    bits = P.encode_time_frame(minute)
    iq = synthesize_wwvb_iq(bits, sample_rate=SR, snr_db=30.0)
    result = decode_iq(iq, sample_rate=SR)
    assert result.frames, "expected at least one decoded frame"
    return result, result.frames[0]


# --------------------------------------------------------------------------- #
# Propagation model
# --------------------------------------------------------------------------- #

def test_propagation_distance_and_delay_are_physical():
    d = wwvb_expected_delay(RX_LAT, RX_LON)
    assert 1000.0 < d.distance_km < 1500.0
    light_ms = d.distance_km / 299792.458 * 1000.0
    # Delay is light-time plus a small groundwave excess, within ~1 ms.
    assert light_ms <= d.delay_ms <= light_ms + 1.0
    assert d.sigma_ms == WWVB_NOMINAL_SIGMA_MS
    assert d.calibrated is False


def test_propagation_learned_override_takes_precedence():
    d = wwvb_expected_delay(RX_LAT, RX_LON, learned_delay_ms=4.55, learned_sigma_ms=0.8)
    assert d.delay_ms == 4.55
    assert d.sigma_ms == 0.8
    assert d.calibrated is True
    # Distance is still computed even with a learned delay.
    assert 1000.0 < d.distance_km < 1500.0


# --------------------------------------------------------------------------- #
# Timing-error sign convention
# --------------------------------------------------------------------------- #

def test_timing_error_zero_when_arrival_is_exactly_expected_delay_late():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    expected_delay_ms = 4.6
    arrival = minute.timestamp() + expected_delay_ms / 1000.0
    te = compute_timing_error_ms(
        arrival_utc_s=arrival,
        decoded_minute_utc=minute,
        expected_delay_ms=expected_delay_ms,
    )
    assert abs(te) < 1e-3


def test_timing_error_sign_tracks_clock_error():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    expected_delay_ms = 4.6
    # Arrival 3 ms later than (minute + expected delay) -> clock +3 ms.
    arrival = minute.timestamp() + (expected_delay_ms + 3.0) / 1000.0
    te = compute_timing_error_ms(
        arrival_utc_s=arrival,
        decoded_minute_utc=minute,
        expected_delay_ms=expected_delay_ms,
    )
    assert abs(te - 3.0) < 1e-3


def test_timing_error_treats_naive_minute_as_utc():
    naive = dt.datetime(2026, 6, 15, 4, 30)
    aware = naive.replace(tzinfo=dt.timezone.utc)
    arrival = aware.timestamp() + 0.005
    te_naive = compute_timing_error_ms(
        arrival_utc_s=arrival, decoded_minute_utc=naive, expected_delay_ms=0.0
    )
    te_aware = compute_timing_error_ms(
        arrival_utc_s=arrival, decoded_minute_utc=aware, expected_delay_ms=0.0
    )
    assert abs(te_naive - te_aware) < 1e-9
    assert abs(te_naive - 5.0) < 1e-3


# --------------------------------------------------------------------------- #
# Decoder boundary_sample exposure
# --------------------------------------------------------------------------- #

def test_decoder_exposes_finite_boundary_sample():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    _, f = _decode_one(minute)
    assert math.isfinite(f.boundary_sample)
    # Synthesized signal starts exactly on a second boundary.
    assert f.boundary_sample == pytest.approx(0.0, abs=1.0)
    assert f.frame.minute_of_frame == minute


# --------------------------------------------------------------------------- #
# build_l1_row: gates
# --------------------------------------------------------------------------- #

def test_build_l1_row_none_without_anchor():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    _, f = _decode_one(minute)
    row = build_l1_row(
        detected_frame=f, anchor_rtp=None, rtp_to_utc_s=lambda r: 0.0,
        rx_lat=RX_LAT, rx_lon=RX_LON, snr_db=20.0, confidence=1.0,
        processing_version="t",
    )
    assert row is None


def test_build_l1_row_none_when_rtp_to_utc_unavailable():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    _, f = _decode_one(minute)
    row = build_l1_row(
        detected_frame=f, anchor_rtp=1000, rtp_to_utc_s=lambda r: None,
        rx_lat=RX_LAT, rx_lon=RX_LON, snr_db=20.0, confidence=1.0,
        processing_version="t",
    )
    assert row is None


def test_build_l1_row_none_when_timing_implausible():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    _, f = _decode_one(minute)
    # Arrival a full second off -> timing error ~1000 ms -> rejected.
    bad_arrival = minute.timestamp() + 1.0
    row = build_l1_row(
        detected_frame=f, anchor_rtp=1000, rtp_to_utc_s=lambda r: bad_arrival,
        rx_lat=RX_LAT, rx_lon=RX_LON, snr_db=20.0, confidence=1.0,
        processing_version="t",
    )
    assert row is None


# --------------------------------------------------------------------------- #
# build_l1_row: happy path + schema validity + end-to-end clock-error recovery
# --------------------------------------------------------------------------- #

def _rtp_to_utc_for_injected_error(minute, boundary_sample, anchor_rtp, err_ms):
    """Closure mapping the boundary RTP to (minute + expected_delay + err_ms)."""
    delay = wwvb_expected_delay(RX_LAT, RX_LON)
    target_arrival = minute.timestamp() + (delay.delay_ms + err_ms) / 1000.0
    boundary_rtp = (anchor_rtp + int(round(boundary_sample))) & 0xFFFFFFFF

    def _f(rtp):
        # linear in sample offset from the boundary rtp; SR samples per second
        return target_arrival + ((rtp - boundary_rtp) & 0xFFFFFFFF) / SR

    return _f


def test_build_l1_row_recovers_injected_clock_error():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    result, f = _decode_one(minute)
    anchor_rtp = 1_000_000
    err_ms = 3.0
    rtp_to_utc = _rtp_to_utc_for_injected_error(
        minute, f.boundary_sample, anchor_rtp, err_ms
    )
    snr = estimate_snr_db(result.per_second_iq[f.second_index:f.second_index + 60])
    row = build_l1_row(
        detected_frame=f, anchor_rtp=anchor_rtp, rtp_to_utc_s=rtp_to_utc,
        rx_lat=RX_LAT, rx_lon=RX_LON, snr_db=snr, confidence=1.0,
        processing_version="wwvb-layer4/test",
    )
    assert row is not None
    # raw_toa_ms carries timing_error_ms; should recover the injected 3 ms
    # (sub-sample rounding ~0.04 ms at 24 kHz).
    assert row["raw_toa_ms"] == pytest.approx(err_ms, abs=0.1)
    assert row["station_id"] == "WWVB"
    assert row["frequency_mhz"] == pytest.approx(0.06)
    assert row["tone_detected"] is True
    assert row["identification_method"] == "wwvb_decode"
    # Uncalibrated -> MARGINAL grade regardless of SNR.
    assert row["quality_flag"] == "MARGINAL"
    # The dict round-trips back through the L1 model (schema-valid).
    L1MetrologyMeasurement(**row)


def test_build_l1_row_good_grade_only_when_calibrated_and_strong():
    minute = dt.datetime(2026, 6, 15, 4, 30, tzinfo=dt.timezone.utc)
    _, f = _decode_one(minute)
    anchor_rtp = 2_000_000
    # With a learned delay the source can be GOOD if SNR clears the floor.
    delay = wwvb_expected_delay(RX_LAT, RX_LON, learned_delay_ms=4.6, learned_sigma_ms=0.8)
    boundary_rtp = (anchor_rtp + int(round(f.boundary_sample))) & 0xFFFFFFFF
    arrival = minute.timestamp() + (delay.delay_ms + 0.0) / 1000.0

    def rtp_to_utc(rtp):
        return arrival + ((rtp - boundary_rtp) & 0xFFFFFFFF) / SR

    row = build_l1_row(
        detected_frame=f, anchor_rtp=anchor_rtp, rtp_to_utc_s=rtp_to_utc,
        rx_lat=RX_LAT, rx_lon=RX_LON, snr_db=25.0, confidence=1.0,
        processing_version="t", learned_delay_ms=4.6, learned_sigma_ms=0.8,
    )
    assert row is not None
    assert row["quality_flag"] == "GOOD"


# --------------------------------------------------------------------------- #
# SNR proxy
# --------------------------------------------------------------------------- #

def test_estimate_snr_db_high_for_clean_symbols():
    # Coherent +/-1 BPSK symbols, all same magnitude -> high coherence.
    z = np.array([1, -1, 1, 1, -1, 1, -1, -1], dtype=np.complex128)
    assert estimate_snr_db(z) > 20.0


def test_estimate_snr_db_nan_for_too_few():
    assert math.isnan(estimate_snr_db(np.array([1 + 0j])))
