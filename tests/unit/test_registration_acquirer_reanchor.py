"""A held plane re-anchors every minute, so it never aliases by 49.7 h.

Final review I-6 (2026-10-30).  ``sample0_utc_for`` reads the RTP
difference as signed 32 bits, which reaches 2**31 samples: 24.85 h at
24 kHz.  ``corroborate`` moved ``utc_ref`` but never ``rtp_ref``, so a
plane held longer than that read 2**32 samples back and jumped by about
-178,957 s.  Stand-alone T3 now holds planes that long (spec §7.3).

The fix moves the plane's reference point to the current minute's
``start_rtp`` every minute, carrying UTC forward exactly: same plane, new
reference point, with its counter map rolled to match.
"""

from fractions import Fraction

import numpy as np

from hf_timestd.core.registration_acquirer import (
    Registration,
    RegistrationAcquirer,
)
from synth_origins import counter_map_for

SR = 24000
MIN = 1_800_000_000
UTC0 = Fraction(MIN) - Fraction(1, 80)          # 12.5 ms before a minute
RTP0 = 4_000_000_000                             # wraps within the first hour


def _held(acq: RegistrationAcquirer) -> Registration:
    reg = Registration("ep-1", rtp_ref=RTP0, utc_ref=float(UTC0),
                       sample_rate=SR, sigma_ms=1.0, channel="fused",
                       verified=True)
    acq.note_counter_map(counter_map_for(51, SR).rolled_forward(RTP0))
    acq.adopt(reg)
    return acq.registration


def test_a_plane_held_thirty_hours_stays_exact():
    """1,800 minutes (30 h, 2.6e9 samples, past 2**31), each minute a few
    samples off a whole 60 s, as a real extract lands.  Every minute the
    plane must still name its sample exactly (within one float ulp at
    1.8e9 s), and its map must still answer at its own rtp_ref."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    reg = _held(acq)
    truth_index = reg.counter_map.input_index(RTP0)
    jitter = np.random.default_rng(3).integers(-20, 21, size=1800)
    n = 0                                       # samples since RTP0, unwrapped
    for k in range(1800):
        n += 60 * SR + int(jitter[k])
        start = (RTP0 + n) % 2**32
        acq.reanchor(start)
        reg = acq.registration
        truth = UTC0 + Fraction(n, SR)
        assert abs(Fraction(reg.sample0_utc_for(start)) - truth) < Fraction(1, 2**21)
        assert reg.rtp_ref == start
        # the map still converts at the plane's own reference point
        assert reg.counter_map.input_index(reg.rtp_ref) == (
            truth_index + n * reg.counter_map.ilen / reg.counter_map.olen)
    assert n > 2**31


def test_measure_minute_reanchors_the_held_plane():
    """The acquirer re-anchors on its own each minute: metrology needs no
    new call.  ``measure_minute`` on a held plane moves rtp_ref to the
    minute's start_rtp and keeps the plane where it was."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    _held(acq)
    start = (RTP0 + 3 * 60 * SR) % 2**32
    before = acq.registration.sample0_utc_for(start)
    acq.measure_minute(np.zeros(62 * SR), start, MIN + 180, {"WWV": 0.0125})
    assert acq.registration.rtp_ref == start
    assert acq.registration.sample0_utc_for(start) == before


def test_a_corroborate_move_survives_the_next_reanchor():
    """corroborate moves utc_ref; the next re-anchor must carry the moved
    plane, not the one before the move."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    reg = _held(acq)
    start = (RTP0 + 60 * SR) % 2**32
    acq.reanchor(start)
    reg.utc_ref -= 0.002                        # what corroborate does
    moved = reg.sample0_utc_for((start + 60 * SR) % 2**32)
    acq.reanchor((start + 60 * SR) % 2**32)
    assert acq.registration.sample0_utc_for((start + 60 * SR) % 2**32) == moved
