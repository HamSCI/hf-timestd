"""Regression test for P-H23.

PropagationEngine advertised a physics-based IRI-2020 tier "with
ionospheric ray tracing"; the branch was a literal ``pass`` and the
constructed IonosphericModel / IonosphericDelayCalculator were never
called. The dead tier and its unused machinery have been removed — the
engine is honestly a geometric + heuristic estimator.
"""

from __future__ import annotations

from hf_timestd.core.propagation_engine import (
    PropagationEngine, PropagationResult,
)


def test_engine_has_no_dead_iri_machinery():
    eng = PropagationEngine()
    # The retired IRI tier left no advertised-but-unused attributes.
    assert not hasattr(eng, 'enable_iri')
    assert not hasattr(eng, 'ionosphere')
    assert not hasattr(eng, 'delay_calculator')


def test_estimate_delay_returns_a_real_result():
    eng = PropagationEngine()
    r = eng.estimate_delay(40.6, -105.0, 30.0, -97.0, 10_000_000)
    assert isinstance(r, PropagationResult)
    assert r.method in ('GEOMETRIC', 'HEURISTIC')
    assert r.delay_ms > 0.0


def test_legacy_iri_request_falls_back_to_geometric():
    """A preferred_method='IRI' request must not crash and must not
    claim an 'IRI' method — the tier no longer exists."""
    eng = PropagationEngine()
    r = eng.estimate_delay(40.6, -105.0, 30.0, -97.0, 10_000_000,
                           preferred_method='IRI')
    assert r.method == 'GEOMETRIC'


def test_heuristic_can_be_forced():
    eng = PropagationEngine()
    r = eng.estimate_delay(40.6, -105.0, 30.0, -97.0, 10_000_000,
                           preferred_method='HEURISTIC')
    assert r.method == 'HEURISTIC'


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
