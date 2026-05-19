"""Regression test for P-H27.

The reanalysis TEC fit (`_estimate_tec_cleaned`) used
``raw_arrival_time_ms`` — the absolute ToA, whose intercept is dominated
by geometric delay — while its docstring claimed that *was* D_clock.
The L2 schema (measurement.py) defines D_clock authoritatively as
``clock_offset_ms = raw_arrival_time_ms - propagation_delay_ms``; the
fit must use that geometry-removed quantity.
"""

import types

from hf_timestd.core.ionospheric_reanalysis import (
    IonosphericReanalysis, ReanalyzedMeasurement,
)


def _meas(freq, raw_toa, clock_offset):
    return ReanalyzedMeasurement(
        timestamp='2026-05-19T12:00:00Z', station='WWV',
        frequency_mhz=freq, snr_db=40.0,
        original_mode='1F2', original_n_hops=1,
        raw_arrival_time_ms=raw_toa,
        propagation_delay_ms=raw_toa - clock_offset,
        clock_offset_ms=clock_offset, confidence=0.9, quality_flag='GOOD',
        solar_elevation_deg=30.0, estimated_fof2_mhz=8.0,
        oblique_muf_mhz=20.0, mode_physically_valid=True,
        validated_mode='1F2', validated_n_hops=1,
    )


def _reanalyzer_with_capture(captured):
    def fake_estimate_tec(tec_input, station, timestamp):
        captured['input'] = tec_input
        return types.SimpleNamespace(
            tec_u=10.0, t_vacuum_error_ms=0.0, confidence=0.9,
            residuals_ms=0.1,
        )
    r = IonosphericReanalysis.__new__(IonosphericReanalysis)
    r.tec_estimator = types.SimpleNamespace(estimate_tec=fake_estimate_tec)
    return r


def test_tec_fit_uses_clock_offset_not_raw_toa():
    captured = {}
    r = _reanalyzer_with_capture(captured)
    # raw ToA is a large absolute time (5000 ms); D_clock is the small
    # geometry-removed residual.  The fit must see the residual.
    measurements = [
        _meas(2.5, raw_toa=5000.0, clock_offset=0.4),
        _meas(2.5, raw_toa=5000.0, clock_offset=0.6),   # median 0.5
        _meas(5.0, raw_toa=5000.0, clock_offset=0.8),
        _meas(5.0, raw_toa=5000.0, clock_offset=1.0),   # median 0.9
    ]
    result = r._estimate_tec_cleaned(measurements, 'WWV', 0.0)
    assert result is not None
    toas = sorted(item['toa_ms'] for item in captured['input'])
    # D_clock medians — not the 5000 ms raw ToA.
    assert toas == [0.5, 0.9]


def test_nan_clock_offset_is_excluded():
    captured = {}
    r = _reanalyzer_with_capture(captured)
    measurements = [
        _meas(2.5, raw_toa=5000.0, clock_offset=0.5),
        _meas(2.5, raw_toa=5000.0, clock_offset=float('nan')),
        _meas(5.0, raw_toa=5000.0, clock_offset=0.9),
        _meas(5.0, raw_toa=5000.0, clock_offset=0.9),
    ]
    result = r._estimate_tec_cleaned(measurements, 'WWV', 0.0)
    assert result is not None
    toas = sorted(item['toa_ms'] for item in captured['input'])
    # The NaN sample is dropped; 2.5 MHz keeps only the 0.5 sample.
    assert toas == [0.5, 0.9]
