"""Regression test for P-H26 (reanalysis side).

`_estimate_tec_cleaned` median-collapsed an entire hour of measurements
into one TEC fit; a mid-hour mode hop then injected a multi-ms geometric
step into the 1/f^2 fit. The fit is now run per <=5-minute window —
`_estimate_tec_cleaned` returns one result per window, each carrying its
own `window_start`.
"""

import types
from datetime import datetime, timezone

from hf_timestd.core.ionospheric_reanalysis import (
    IonosphericReanalysis, ReanalyzedMeasurement, TEC_FIT_WINDOW_S,
)

_HOUR = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
_HOUR_TS = _HOUR.timestamp()


def _iso(offset_s):
    return datetime.fromtimestamp(
        _HOUR_TS + offset_s, tz=timezone.utc
    ).isoformat().replace('+00:00', 'Z')


def _meas(freq, clock_offset, offset_s):
    return ReanalyzedMeasurement(
        timestamp=_iso(offset_s), station='WWV',
        frequency_mhz=freq, snr_db=40.0,
        original_mode='1F2', original_n_hops=1,
        raw_arrival_time_ms=5000.0,
        propagation_delay_ms=5000.0 - clock_offset,
        clock_offset_ms=clock_offset, confidence=0.9, quality_flag='GOOD',
        solar_elevation_deg=30.0, estimated_fof2_mhz=8.0,
        oblique_muf_mhz=20.0, mode_physically_valid=True,
        validated_mode='1F2', validated_n_hops=1,
    )


def _reanalyzer():
    def fake_estimate_tec(tec_input, station, ts):
        return types.SimpleNamespace(
            tec_u=10.0, t_vacuum_error_ms=0.0, confidence=0.9,
            residuals_ms=0.1,
        )
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)
    r.tec_estimator = types.SimpleNamespace(estimate_tec=fake_estimate_tec)
    return r


def test_returns_a_list_of_per_window_fits():
    r = _reanalyzer()
    # Two measurement clusters 11 min apart -> windows 0 and 2.
    ms = [
        _meas(2.5, 0.5, 60), _meas(5.0, 0.9, 70),
        _meas(2.5, 0.6, 660), _meas(5.0, 1.0, 670),
    ]
    results = r._estimate_tec_cleaned(ms, 'WWV', _HOUR_TS)
    assert isinstance(results, list)
    assert len(results) == 2
    starts = sorted(rr['window_start'] for rr in results)
    assert starts[0] == _HOUR_TS                       # window 0
    assert starts[1] == _HOUR_TS + 2 * TEC_FIT_WINDOW_S  # window 2


def test_each_fit_window_spans_at_most_5_minutes():
    r = _reanalyzer()
    # Measurements spread across a full hour, one cluster per 5-min window.
    ms = []
    for w in range(0, 12):
        base = w * TEC_FIT_WINDOW_S + 30
        ms += [_meas(2.5, 0.5, base), _meas(5.0, 0.9, base + 10)]
    results = r._estimate_tec_cleaned(ms, 'WWV', _HOUR_TS)
    # One fit per window — never one hour-wide collapse.
    assert len(results) == 12
    for rr in results:
        # window_start is a clean multiple of the 5-min window.
        assert (rr['window_start'] - _HOUR_TS) % TEC_FIT_WINDOW_S == 0


def test_measurements_in_one_window_make_one_fit():
    r = _reanalyzer()
    ms = [
        _meas(2.5, 0.5, 10), _meas(5.0, 0.9, 20),
        _meas(2.5, 0.6, 250), _meas(5.0, 1.0, 290),  # all < 300 s
    ]
    results = r._estimate_tec_cleaned(ms, 'WWV', _HOUR_TS)
    assert len(results) == 1
    assert results[0]['window_start'] == _HOUR_TS


def test_no_valid_windows_returns_empty_list():
    r = _reanalyzer()
    results = r._estimate_tec_cleaned([], 'WWV', _HOUR_TS)
    assert results == []
