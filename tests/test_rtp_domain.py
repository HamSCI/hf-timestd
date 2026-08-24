"""RtpUnwrapper — extend the 32-bit RTP counter to one continuous domain.

Motivation (B4, 2026-08-23/24): ``2**32 % 96000 == 23296``, so every
mod-sample-rate phase of the *masked* 32-bit counter jumps 23,296 samples
(242.667 ms) at each counter wrap (~12 h 26 m at 96 kHz).  The T6 chain
of phase comparisons — MF edge tracking, the fine/coarse cross-check,
and chain-delay disambiguation — must therefore run in ONE continuous
counter domain.  The unwrapper is that domain's single entry point.
"""
import pytest

from hf_timestd.core.rtp_domain import RtpUnwrapper, wrapped_signed32

WRAP = 1 << 32


class TestWrappedSigned32:
    def test_small_positive_and_negative(self):
        assert wrapped_signed32(5) == 5
        assert wrapped_signed32(-5) == -5

    def test_folds_multiples_of_wrap_away(self):
        assert wrapped_signed32(WRAP + 7) == 7
        assert wrapped_signed32(7 - WRAP) == 7

    def test_half_range_boundary(self):
        assert wrapped_signed32((1 << 31) - 1) == (1 << 31) - 1
        assert wrapped_signed32(1 << 31) == -(1 << 31)


class TestRtpUnwrapper:
    def test_identity_before_any_wrap(self):
        u = RtpUnwrapper()
        assert u.unwrap(1000) == 1000
        assert u.unwrap(1480) == 1480
        assert u.unwrap(96_000_000) == 96_000_000

    def test_continues_across_the_32bit_wrap(self):
        u = RtpUnwrapper()
        assert u.unwrap(WRAP - 1000) == WRAP - 1000
        # The stream wraps: 32-bit value restarts near zero.
        assert u.unwrap(500) == WRAP + 500
        assert u.unwrap(96_500) == WRAP + 96_500

    def test_phase_is_continuous_across_the_wrap(self):
        # The whole point: mod-SR phase must not jump at the wrap.
        sr = 96_000
        u = RtpUnwrapper()
        start = WRAP - 10 * sr - 12_345
        vals = [u.unwrap((start + k * 1740) & 0xFFFFFFFF)
                for k in range(20 * sr // 1740)]
        phases = {(v - k * 1740) % sr for k, v in enumerate(vals)}
        assert len(phases) == 1  # a masked feed would produce two

    def test_tolerates_small_backwards_jitter(self):
        u = RtpUnwrapper()
        assert u.unwrap(10_000) == 10_000
        assert u.unwrap(9_940) == 9_940  # ±60-sample label wobble

    def test_masks_an_out_of_range_first_value(self):
        u = RtpUnwrapper()
        assert u.unwrap(WRAP + 123) == 123

    def test_multiple_wraps(self):
        u = RtpUnwrapper()
        step = 1 << 30
        expected = 0
        u.unwrap(0)
        for k in range(1, 12):
            expected += step
            assert u.unwrap((k * step) & 0xFFFFFFFF) == expected
