"""Metrology journal volume (v3.73 row 18).

TickMatchedFilter logged one INFO line per detected tick window, 55 a
minute per station.  On AC0G-B4 that came to about 8,600 lines an hour
for each SHARED channel.  The per-window lines now log at DEBUG; the
minute's ensemble line carries the result at INFO.
"""

import logging

import numpy as np

from hf_timestd.core.tick_matched_filter import create_tick_filter

LOGGER = "hf_timestd.core.tick_matched_filter"


def _run_minute():
    np.random.seed(42)
    f = create_tick_filter("WWV", window_seconds=5, overlap_seconds=1)
    iq = np.random.randn(60 * 20000) + 1j * np.random.randn(60 * 20000)
    # min_snr_db=-100 passes every window, the worst case for volume.
    return f.process_minute(iq, minute_number=0, min_snr_db=-100)


def test_per_window_lines_stay_off_info(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    result = _run_minute()
    assert result.valid_windows > 40  # the windows still count
    window_lines = [r for r in caplog.records
                    if r.name == LOGGER and "tick window" in r.getMessage()
                    and r.levelno >= logging.INFO]
    assert window_lines == []


def test_per_window_lines_reach_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _run_minute()
    assert any("tick window" in r.getMessage() and "DETECTED" in r.getMessage()
               for r in caplog.records if r.name == LOGGER)


def test_geographic_delay_mismatch_logs_once(caplog):
    """"Measured delay diff differs significantly" fired on most minutes
    on every SHARED channel (about 740 lines an hour on AC0G-B4)."""
    from hf_timestd.core.wwv_geographic_predictor import WWVGeographicPredictor

    name = "hf_timestd.core.wwv_geographic_predictor"
    p = WWVGeographicPredictor("EM38ww")
    caplog.set_level(logging.DEBUG, logger=name)
    for _ in range(5):
        # 1 ms apart where geometry expects about 18 ms.  Forget the
        # learned history so each call meets the same mismatch.
        p.toa_history = {}
        p.classify_dual_peaks(4.0, 5.0, 1.0, 1.0, 10.0)
    hits = [r for r in caplog.records
            if r.name == name and r.getMessage().startswith("Measured delay diff")]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING


def test_geographic_delay_mismatch_clears_on_a_clean_pass(caplog):
    """Every classify_dual_peaks pass sweeps, so the clear line arrives
    when the mismatch stops, not at the next mismatch."""
    from hf_timestd.core.condition_log import ConditionLog
    from hf_timestd.core.wwv_geographic_predictor import WWVGeographicPredictor

    name = "hf_timestd.core.wwv_geographic_predictor"
    t = [1000.0]
    p = WWVGeographicPredictor("EM38ww")
    p._conditions = ConditionLog(logging.getLogger(name), clock=lambda: t[0])
    caplog.set_level(logging.DEBUG, logger=name)
    p.toa_history = {}
    expected = p.calculate_expected_delays(10.0)  # geometry, no history
    gap = abs(expected["wwv_delay_ms"] - expected["wwvh_delay_ms"])
    p.classify_dual_peaks(4.0, 5.0, 1.0, 1.0, 10.0)  # mismatch
    t[0] += 601.0
    p.toa_history = {}
    p.classify_dual_peaks(4.0, 4.0 + gap, 1.0, 1.0, 10.0)  # agrees
    cleared = [r for r in caplog.records
               if r.name == name and "cleared" in r.getMessage()]
    assert len(cleared) == 1
    assert cleared[0].levelno == logging.WARNING
    mismatches = [r for r in caplog.records if r.name == name
                  and r.getMessage().startswith("Measured delay diff")]
    assert len(mismatches) == 1  # the second pass agreed with geometry
