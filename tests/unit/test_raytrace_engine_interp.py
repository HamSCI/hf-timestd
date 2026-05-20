"""
P-M17 regression tests for raytrace_engine.

* The IRI Ne-profile range interpolation is vectorised across heights —
  it must match the pre-P-M17 per-height ``np.interp`` loop exactly.
* ``_raytrace_worker`` is module-level (picklable, for the ``spawn``
  start method) and reports a missing pylap as a queued error.
"""

from __future__ import annotations

import pickle

import numpy as np
import pytest

import hf_timestd.core.raytrace_engine as raytrace_engine
from hf_timestd.core.raytrace_engine import _interp_profiles_to_columns


def _per_height_reference(profiles_arr, sample_km, range_km):
    """The pre-P-M17 per-height ``np.interp`` loop — the reference."""
    out = np.zeros((profiles_arr.shape[0], range_km.shape[0]))
    for h in range(profiles_arr.shape[0]):
        out[h, :] = np.interp(range_km, sample_km, profiles_arr[h, :])
    return out


def test_vectorised_interp_matches_per_height_loop():
    """The vectorised interpolation equals the per-height np.interp loop."""
    rng = np.random.default_rng(20260519)
    sample_km = np.linspace(0.0, 10000.0, 8)
    profiles = rng.uniform(1e3, 1e6, size=(200, 8))
    range_km = np.arange(201) * 50.0

    got = _interp_profiles_to_columns(profiles, sample_km, range_km)
    assert np.allclose(got, _per_height_reference(profiles, sample_km, range_km))


def test_interp_clamps_outside_sample_span():
    """Ranges below the first / above the last sample clamp to the end
    profile, matching np.interp's flat extrapolation."""
    sample_km = np.array([0.0, 1000.0, 2000.0])
    profiles = np.array([[10.0, 20.0, 30.0], [1.0, 2.0, 3.0]])
    range_km = np.array([-500.0, 0.0, 1500.0, 5000.0])

    got = _interp_profiles_to_columns(profiles, sample_km, range_km)
    assert list(got[0]) == [10.0, 10.0, 25.0, 30.0]
    assert list(got[1]) == [1.0, 1.0, 2.5, 3.0]


def test_interp_exact_at_sample_points():
    """Querying exactly at the sample points returns the samples."""
    sample_km = np.array([0.0, 500.0, 1000.0])
    profiles = np.array([[5.0, 9.0, 17.0]])
    got = _interp_profiles_to_columns(profiles, sample_km, sample_km)
    assert list(got[0]) == [5.0, 9.0, 17.0]


def test_interp_result_shape():
    sample_km = np.linspace(0.0, 6000.0, 4)
    profiles = np.zeros((50, 4))
    range_km = np.arange(121) * 50.0
    out = _interp_profiles_to_columns(profiles, sample_km, range_km)
    assert out.shape == (50, 121)


def test_raytrace_worker_is_picklable():
    """The subprocess worker must be picklable for the 'spawn' start
    method (P-M17) — i.e. module-level, not a closure."""
    assert (
        pickle.loads(pickle.dumps(raytrace_engine._raytrace_worker))
        is raytrace_engine._raytrace_worker
    )


def test_raytrace_worker_reports_missing_pylap(monkeypatch):
    """With pylap unavailable in the subprocess, the worker queues an
    error rather than raising."""
    monkeypatch.setattr(raytrace_engine, "_pylap_raytrace_2d", None)
    msgs = []

    class _Queue:
        def put(self, m):
            msgs.append(m)

    raytrace_engine._raytrace_worker(_Queue(), ())
    assert msgs and msgs[0][0] == "err"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
