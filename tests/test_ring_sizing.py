"""Ring depth must come from metrology's requirement, not the archive's.

The ring feeds metrology, which reads 60 s windows with no file I/O on the
hot path. It used to be sized by tiered_storage.calculate_hot_minutes() —
the ARCHIVE's policy, whose floor is "one whole chunk plus margin". That
pinned it at 7-11 minutes to serve a 60 s requirement: 461-725 MB of
unreclaimable shared memory on a 9.3 GB host that was OOM-killing the
recorder that owns it.
"""
import pytest

from hf_timestd.core.ring_buffer import (
    RING_DEFAULT_MINUTES, RING_MIN_MINUTES, RING_WINDOW_SEC,
)

BYTES_PER_SEC_PER_CH = 192_000


def test_default_covers_the_metrology_window_with_slack():
    assert RING_DEFAULT_MINUTES * 60 >= RING_WINDOW_SEC * 2, (
        "the ring must hold the window plus room for a lagging worker")


def test_floor_never_drops_below_the_window():
    assert RING_MIN_MINUTES * 60 > RING_WINDOW_SEC


def test_floor_is_not_above_the_default():
    assert RING_MIN_MINUTES <= RING_DEFAULT_MINUTES


def test_default_is_a_real_saving_over_the_archive_policy():
    """The archive policy produced 7 minutes on B4. Guard the regression by
    size, not by provenance — anything that quietly restores archive-driven
    sizing shows up here as a jump in megabytes."""
    ours = RING_DEFAULT_MINUTES * 60 * BYTES_PER_SEC_PER_CH * 6
    archive_policy = 7 * 60 * BYTES_PER_SEC_PER_CH * 6
    assert ours < archive_policy / 2, "expected to at least halve the ring"


def test_ring_sizing_is_independent_of_chunk_duration():
    """The property that was violated: chunk duration must not appear
    anywhere in the ring's size."""
    import inspect
    from hf_timestd.core import core_recorder_v2 as mod

    src = inspect.getsource(mod)
    i = src.index("ring_enabled = bool(")
    block = src[i:i + 1400]
    assert "ring_minutes" in block
    assert "calculate_hot_minutes" not in block, (
        "ring depth must not be computed by the archive's policy")
    assert "file_duration_sec" not in block, (
        "chunk duration must not influence ring depth")


@pytest.mark.parametrize("override,expected", [(1, RING_MIN_MINUTES),
                                               (2, 2), (3, 3), (10, 10)])
def test_operator_override_is_clamped_at_the_floor(override, expected):
    assert max(RING_MIN_MINUTES, override) == expected
