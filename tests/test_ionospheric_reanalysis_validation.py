"""
Regression tests for the ionospheric_reanalysis remediation findings.

* P-M23 — foE from the ITU-R / Muggleton formula (not 0.3·foF2);
  the Es relabel is gated on hop-geometry feasibility (no long-path Es).
* P-M24 — process_hour is idempotent: re-runs do not duplicate L3C /
  L3 TEC records (the helpers extract the keys already present).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from hf_timestd.core.ionospheric_reanalysis import (
    IonosphericReanalysis,
    FOE_NIGHT_FLOOR_MHZ,
    R12_MODERATE,
    estimate_foe,
)


# --------------------------------------------------------------------------
# P-M23 — foE from the ITU-R / Muggleton formula
# --------------------------------------------------------------------------
def _muggleton_foe(elev_deg: float) -> float:
    """Reference Muggleton (1975) / ITU-R P.1239 daytime foE."""
    cos_chi = max(math.cos(math.radians(90.0 - elev_deg)), 0.0)
    return 0.9 * ((180.0 + 1.44 * R12_MODERATE) * cos_chi) ** 0.25


def test_estimate_foe_noon():
    """At solar zenith the result is the Muggleton noon value."""
    assert estimate_foe(90.0) == pytest.approx(_muggleton_foe(90.0))


def test_estimate_foe_tracks_chapman_falloff():
    """foE drops as cos^0.25(χ) through the day."""
    for elev in (30.0, 45.0, 60.0, 75.0):
        assert estimate_foe(elev) == pytest.approx(_muggleton_foe(elev))


def test_estimate_foe_night_returns_floor():
    """Below the horizon foE returns the residual-ionisation floor."""
    assert estimate_foe(0.0) == FOE_NIGHT_FLOOR_MHZ
    assert estimate_foe(-5.0) == FOE_NIGHT_FLOOR_MHZ
    assert estimate_foe(-30.0) == FOE_NIGHT_FLOOR_MHZ


def test_estimate_foe_not_a_constant_fraction_of_fof2():
    """Sanity: the new foE is not a constant multiple of FOF2_NOON_MHZ
    (the old behaviour the review flagged) — it's an absolute function
    of solar zenith."""
    # The old code returned ~9.0·0.3 = 2.7 MHz day, 0.5 night.
    # The new daytime noon is ~3.7 MHz (Muggleton).
    assert estimate_foe(90.0) > 3.0
    assert estimate_foe(90.0) < 4.5


# --------------------------------------------------------------------------
# P-M23 — Es relabel gated on hop geometry
# --------------------------------------------------------------------------
def _bare_reanalyzer(*, distances, midpoints):
    """An IonosphericReanalysis with only the attributes _validate_measurement
    needs — __init__ (which discovers channels, opens writers) is bypassed."""
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)
    r.distances = distances
    r.midpoints = midpoints
    return r


def _over_muf_m(station: str, ts: str) -> dict:
    """A daytime, high-SNR L2 measurement at a frequency far above any
    plausible MUF (so the 1F→2F→…→4F fallback fails) — designed to fall
    through to the Es-geometry check."""
    return {
        "station": station,
        "frequency_mhz": 50.0,
        "snr_db": 30.0,
        "propagation_mode": "1F2",
        "n_hops": 1,
        "raw_arrival_time_ms": 5.0,
        "propagation_delay_ms": 5.0,
        "clock_offset_ms": 0.0,
        "confidence": 0.9,
        "quality_flag": "GOOD",
        "tone_detected": True,
        "timestamp_utc": ts,
    }


# A daytime midpoint chosen so solar_position returns elev > 70°.
_DAY_ISO = "2026-06-21T19:00:00Z"
_DAY_DT = datetime(2026, 6, 21, 19, 0, 0, tzinfo=timezone.utc)
_DAY_MIDPOINT = (40.0, -100.0)


def test_es_relabel_kept_for_short_path():
    """P-M23: a strong over-MUF daytime signal on a short path becomes Es
    — a 1-hop E-layer (~110 km) hop is geometrically possible."""
    r = _bare_reanalyzer(
        distances={"SHORT": 1500.0},
        midpoints={"SHORT": _DAY_MIDPOINT},
    )
    result = r._validate_measurement(_over_muf_m("SHORT", _DAY_ISO), _DAY_DT)
    assert result is not None
    assert result.validated_mode == "Es"
    assert result.validated_n_hops == 1


def test_es_relabel_rejected_for_long_path():
    """P-M23: the same strong over-MUF signal on a long path stays
    REJECTED — a single Es hop cannot span > ~2300 km (E-layer
    tangent-ray limit), so the old unconditional 1-hop Es label was a
    geometry violation."""
    r = _bare_reanalyzer(
        distances={"LONG": 6000.0},
        midpoints={"LONG": _DAY_MIDPOINT},
    )
    result = r._validate_measurement(_over_muf_m("LONG", _DAY_ISO), _DAY_DT)
    assert result is not None
    assert result.validated_mode == "REJECTED"


# --------------------------------------------------------------------------
# P-M24 — process_hour is idempotent (key extraction helpers)
# --------------------------------------------------------------------------
class _FakeReader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def read_time_range(self, start, end):
        self.calls.append((start, end))
        return self.rows


def test_existing_l3c_keys_extracts_station_freq_pairs():
    """The L3C idempotency probe yields the (station, freq) pairs already
    written for the hour — these are the keys process_hour must skip."""
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)
    r._stats_reader = _FakeReader(
        [
            {"station": "WWV", "frequency_mhz": 10.0},
            {"station": "WWV", "frequency_mhz": 5.0},
            {"station": "CHU", "frequency_mhz": 3.33},
            {"station": None, "frequency_mhz": 1.0},  # missing station — drop
            {"station": "BPM", "frequency_mhz": None},  # missing freq — drop
        ]
    )
    keys = r._existing_l3c_keys("2026-05-19T18:00:00Z", "2026-05-19T19:00:00Z")
    assert keys == {("WWV", 10.0), ("WWV", 5.0), ("CHU", 3.33)}


def test_existing_tec_keys_extracts_station_minute_pairs():
    """The L3 TEC idempotency probe yields (station, minute_boundary)
    keyed by the per-window start the writer stamps."""
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)
    r._tec_reader = _FakeReader(
        [
            {"station": "WWV", "minute_boundary": 1_700_000_000},
            {"station": "WWV", "minute_boundary": 1_700_000_300},
            {"station": None, "minute_boundary": 1_700_000_600},  # drop
            {"station": "CHU", "minute_boundary": None},  # drop
        ]
    )
    keys = r._existing_tec_keys("2026-05-19T18:00:00Z", "2026-05-19T19:00:00Z")
    assert keys == {("WWV", 1_700_000_000), ("WWV", 1_700_000_300)}


def test_existing_keys_empty_on_reader_failure():
    """A reader exception is non-fatal — treat the hour as empty rather
    than abort process_hour."""
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)

    class _Boom:
        def read_time_range(self, start, end):
            raise RuntimeError("reader exploded")

    r._stats_reader = _Boom()
    r._tec_reader = _Boom()
    assert r._existing_l3c_keys("a", "b") == set()
    assert r._existing_tec_keys("a", "b") == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
