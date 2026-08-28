"""The reference gate must be inert until a config bump asks for it.

These exercise the two CoreRecorderV2 helpers directly against a stub, because
the safety property that matters is a negative one: with the flag absent —
which is every deployed station today — the gate must not reject anything, so
shipping this cannot alter B4's behaviour mid-experiment.
"""
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

MS = 1_000_000


class _Stub:
    """Minimal carrier for the two helpers: they touch only _t6_config."""

    def __init__(self, cfg):
        self._t6_config = cfg

    # Bind under the REAL names: _t6_reference_gate_rejects calls
    # self._t6_ref_tracker() internally.
    _t6_ref_tracker = CoreRecorderV2._t6_ref_tracker
    _t6_reference_gate_rejects = CoreRecorderV2._t6_reference_gate_rejects

    tracker = _t6_ref_tracker
    rejects = _t6_reference_gate_rejects


def test_gate_is_inert_when_the_flag_is_absent():
    s = _Stub({'sample_rate': 96000, 'low_edge_hz': -25000, 'high_edge_hz': 25000})
    assert s.tracker() is None
    # Even a 500 ms phantom passes: the feature is off, behaviour unchanged.
    assert s.rejects(500 * MS) is False


def test_gate_never_refuses_while_still_learning():
    """A cold start has no reference; refusing then would strand T6."""
    s = _Stub({'sample_rate': 96000, 'low_edge_hz': -25000, 'high_edge_hz': 25000,
               'reference_gate_enabled': True, 'reference_min_attestations': 5})
    for _ in range(4):
        assert s.rejects(15 * MS) is False
    assert s.tracker().reference_ns is None


def test_gate_refuses_the_phantom_once_it_has_learned():
    s = _Stub({'sample_rate': 96000, 'low_edge_hz': -25000, 'high_edge_hz': 25000,
               'reference_gate_enabled': True, 'reference_min_attestations': 3,
               'reference_tolerance_ns': 5 * MS})
    for v in (15 * MS, 16 * MS, 15 * MS):
        s.rejects(v)
    assert s.tracker().reference_ns is not None
    assert s.rejects(197 * MS) is True     # the sidelobe ±250 ms admits
    assert s.rejects(16 * MS) is False     # the genuine lock still passes
