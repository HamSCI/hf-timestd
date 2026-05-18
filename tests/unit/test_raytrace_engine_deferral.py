"""Regression coverage for P-H14: RaytraceEngine is a deferred overlay.

The PHaRLAP ray-tracing engine is complete but intentionally not wired into
HFPropagationModel / PropagationModeSolver — pyLAP is an optional native
dependency, and a 2-D ray trace is far too costly for the real-time feed. The
deferral and the intended reanalysis-only, advisory wiring path are documented
in raytrace_engine's "Deployment status (P-H14)" module docstring section.

These tests pin the contract that keeps the deferral safe: the engine builds
and degrades gracefully when pyLAP is unavailable, so a future advisory
wire-in cannot crash a caller.
"""

from datetime import datetime, timezone

from hf_timestd.core.raytrace_engine import RaytraceEngine, RaytraceResult

_T = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)


def test_engine_builds_and_reports_availability():
    engine = RaytraceEngine.build(receiver_lat=40.68, receiver_lon=-105.04)
    # is_available() reflects whether pyLAP/PHaRLAP is installed — either way
    # it must be a plain bool the caller can branch on.
    assert isinstance(engine.is_available(), bool)


def test_compute_modes_degrades_gracefully():
    # With or without pyLAP, compute_modes must return a RaytraceResult
    # (the geometric fallback when unavailable) and never raise.
    engine = RaytraceEngine.build(receiver_lat=40.68, receiver_lon=-105.04)
    result = engine.compute_modes('WWV', 10.0, _T)
    assert isinstance(result, RaytraceResult)


def test_unknown_station_falls_back_without_raising():
    engine = RaytraceEngine.build(receiver_lat=40.68, receiver_lon=-105.04)
    result = engine.compute_modes('NOTASTATION', 10.0, _T)
    assert isinstance(result, RaytraceResult)


def test_propagation_model_does_not_wire_in_raytrace_engine():
    # The deferral is deliberate: HFPropagationModel must not import or
    # construct RaytraceEngine. A future wire-in should be an intentional
    # change that updates this test alongside the docstring.
    import hf_timestd.core.propagation_model as pm
    assert not hasattr(pm, 'RaytraceEngine')
    assert not hasattr(pm, 'raytrace_engine')
