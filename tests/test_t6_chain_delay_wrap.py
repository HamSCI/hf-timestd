"""Chain delay is modular in the PPS period; the plausibility guard must wrap.

Regression for the B4 lockout of 2026-08-14: after a radiod restart re-based the
RTP epoch, the MF's raw modular chain delay landed at 843.4 ms.  The external-ref
disambiguation shift is bounded to +/-0.5 s by construction

    offset_sec  = wall - round(wall)      -> [-0.5, +0.5]
    shift       = round((offset_sec - ref_offset) * sr)

so `effective = raw + shift` can only reach raw +/- 500 ms, while the guard
demands |effective| <= 250 ms.  Those intersect only for raw <= 750 ms, so above
that T6 refused every locked cycle indefinitely -- zero anchors for 4+ hours.

The calibrator reports chain delay deliberately unwrapped in [0, SR) and delegates
absolute resolution downstream (see bpsk_pps_calibrator_mf: "we deliberately don't
wrap ... downstream's disambiguation logic anchors the absolute reference").
Downstream never wrapped.  It must.
"""
import pytest

from hf_timestd.core.core_recorder_v2 import wrap_chain_delay_ns

PERIOD = 1_000_000_000
GUARD = 250_000_000


def test_observed_b4_lockout_value_becomes_plausible():
    # Exact values logged on B4 2026-08-14 while T6 refused every cycle.
    raw, disambig = 843_431_909, 171_114_583
    effective = raw + disambig                       # 1_014_546_492
    assert abs(effective) > GUARD                    # refused, pre-fix
    wrapped = wrap_chain_delay_ns(effective)
    assert abs(wrapped) <= GUARD                     # accepted, post-fix
    # ~15 ms is the chain delay measured independently all that day.
    assert 10_000_000 < wrapped < 20_000_000


@pytest.mark.parametrize("effective_ms,expected_ms", [
    (17.497711, 17.497711),    # already in band -> unchanged
    (18.581035, 18.581035),
    (22.351919, 22.351919),
    (1014.546492, 14.546492),  # the lockout case
    (-985.453508, 14.546492),  # same instant, other representative
])
def test_wrap_is_identity_in_band_and_folds_outside(effective_ms, expected_ms):
    got = wrap_chain_delay_ns(round(effective_ms * 1e6))
    assert got == pytest.approx(round(expected_ms * 1e6), abs=1)


def test_wrap_picks_representative_nearest_zero():
    for k in (-3, -1, 0, 1, 2, 5):
        assert wrap_chain_delay_ns(14_546_492 + k * PERIOD) == 14_546_492


def test_deadlock_band_is_reachable_after_wrap():
    """Every raw position in [0, 1s) must be able to reach the guard band."""
    SHIFT_MAX = PERIOD // 2
    for raw in range(0, PERIOD, 10_000_000):
        # worst case: disambiguation contributes nothing useful
        assert abs(wrap_chain_delay_ns(raw)) <= SHIFT_MAX
        # and a correct shift can always land inside the guard
        assert abs(wrap_chain_delay_ns(raw + (0 - raw))) <= GUARD


def test_modular_difference_of_same_instant_is_zero():
    """The jump detector compares two modular quantities; the DIFFERENCE
    must be folded too, or the same physical value in two representations
    reads as a full-second jump.

    Observed on B4 2026-08-14 23:05 after the guard fix landed:
        new=843,431,895   last_accepted=-156,568,105   delta=1,000,000,000
    Those are the same instant; the delta is exactly one period.
    """
    new, last = 843_431_895, -156_568_105
    assert new - last == PERIOD                      # reads as a 1 s jump
    assert wrap_chain_delay_ns(new - last) == 0      # actually no change


def test_genuine_step_survives_wrapping():
    """A real chain-delay step must still be detected after folding."""
    assert abs(wrap_chain_delay_ns(80_000_000)) == 80_000_000      # 80 ms step
    assert abs(wrap_chain_delay_ns(-80_000_000)) == 80_000_000
    # ...and one expressed across the wrap boundary is still 80 ms
    assert abs(wrap_chain_delay_ns(PERIOD - 80_000_000)) == 80_000_000
