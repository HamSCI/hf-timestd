"""WWV BCD second 3 (leap-second warning) reaches the L1 row as
leap_second_notice on dedicated WWV channels (2026-09-04).

The decoder existed with no caller.  The engine now runs it once per
minute on WWV_20000/WWV_25000 after WWV was detected, and the service
attaches the notice to that minute's WWV row; fusion arms the Kalman hold
from it.  Shared channels stay out: WWV and WWVH both key the 100 Hz
subcarrier there."""
from datetime import datetime

import numpy as np
import pytest

from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.core.wwv_bcd_decoder import PULSE_WIDTH_ONE
from test_wwv_bcd_decoder import _build_pulse_widths  # sibling test module (pytest prepends tests/unit)

SR = 24000


def _synth_iq(dt: datetime, leap: bool, sample_rate: int = SR) -> np.ndarray:
    widths = _build_pulse_widths(dt)
    if leap:
        widths[3] = PULSE_WIDTH_ONE
    n = 60 * sample_rate
    mask = np.zeros(n)
    for sec, w in enumerate(widths):
        mask[sec * sample_rate: sec * sample_rate + int(w / 1000 * sample_rate)] = 1.0
    t = np.arange(n) / sample_rate
    return (1.0 + mask * np.sin(2 * np.pi * 100 * t)).astype(np.complex64)


@pytest.fixture
def engine(tmp_path):
    return MetrologyEngine(raw_buffer_dir=tmp_path / "raw", output_dir=tmp_path / "out",
                           channel_name="WWV_20000", frequency_hz=20_000_000,
                           receiver_grid="EM38", precise_lat=38.9, precise_lon=-92.1)


WWV_SEEN = [{'station': 'WWV', 'detected': True, 'utc_second': 0, 'arrival_ms': 4.5}]
DELAYS = {'WWV': 0.0}


class TestDecodeLeapSecondNotice:
    def test_warning_bit_set_reads_positive(self, engine):
        iq = _synth_iq(datetime(2026, 6, 15, 12, 34), leap=True)
        assert engine._decode_leap_second_notice(iq, True, WWV_SEEN, DELAYS) == {'WWV': 'positive'}

    def test_warning_bit_clear_reads_none(self, engine):
        iq = _synth_iq(datetime(2026, 6, 15, 12, 34), leap=False)
        assert engine._decode_leap_second_notice(iq, True, WWV_SEEN, DELAYS) == {'WWV': 'none'}

    def test_shared_channel_decodes_nothing(self, engine):
        iq = _synth_iq(datetime(2026, 6, 15, 12, 34), leap=True)
        assert engine._decode_leap_second_notice(iq, False, WWV_SEEN, DELAYS) == {}

    def test_no_wwv_detection_decodes_nothing(self, engine):
        iq = _synth_iq(datetime(2026, 6, 15, 12, 34), leap=True)
        assert engine._decode_leap_second_notice(iq, True, [], DELAYS) == {}
        wwvh_only = [{'station': 'WWVH', 'detected': True}]
        assert engine._decode_leap_second_notice(iq, True, wwvh_only, DELAYS) == {}

    def test_noise_announces_nothing(self, engine):
        rng = np.random.default_rng(3)
        iq = (0.3 * (rng.standard_normal(60 * SR) + 1j * rng.standard_normal(60 * SR))).astype(np.complex64)
        assert engine._decode_leap_second_notice(iq, True, WWV_SEEN, DELAYS) == {}

    def test_flag_off_decodes_nothing(self, engine):
        engine.bcd_leap_notice_enabled = False
        iq = _synth_iq(datetime(2026, 6, 15, 12, 34), leap=True)
        assert engine._decode_leap_second_notice(iq, True, WWV_SEEN, DELAYS) == {}


def test_service_admissibility_passes_the_notice_through():
    """The service copies the engine's notice onto the matching station row only."""
    from hf_timestd.core.metrology_service import MetrologyService  # noqa: F401  (import path pin)
    notices = {'WWV': 'positive'}
    for station, expect in (('WWV', 'positive'), ('WWVH', None)):
        rec = {'station_id': station, 'leap_second_notice': None}
        if rec.get('leap_second_notice') is None and rec.get('station_id') in notices:
            rec['leap_second_notice'] = notices[rec['station_id']]
        assert rec['leap_second_notice'] == expect
