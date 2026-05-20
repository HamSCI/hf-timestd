"""
Unit tests for PhysicsFusionService remediation findings.

* P-M20 — _timed_write / _timed_write_batch must not race or leak: a
  write thread that timed out keeps its per-writer lock, so the next
  write to that writer skips instead of starting a competing thread.
* P-M22 — the F2 reflection height comes from the ionospheric model,
  with a climatological fallback, not a hardcoded 300 km.
"""

from __future__ import annotations

import threading
import time

import pytest

from hf_timestd.core.physics_fusion_service import (
    PhysicsFusionService,
    _F2_FALLBACK_HEIGHT_KM,
)


def _bare_service(timeout_s: float = 0.4) -> PhysicsFusionService:
    """A PhysicsFusionService with only the attributes the unit-level
    methods under test need — __init__ is bypassed (it discovers
    channels, opens writers/readers, etc.)."""
    svc = PhysicsFusionService.__new__(PhysicsFusionService)
    svc._write_timeout_seconds = timeout_s
    svc._write_timeout_count = 0
    svc._writer_locks = {}
    svc._iono_model = None
    return svc


# --------------------------------------------------------------------------
# P-M20 — per-writer write lock
# --------------------------------------------------------------------------
class _FastWriter:
    def __init__(self):
        self.records = []

    def write_measurement(self, rec):
        self.records.append(rec)

    def write_measurements_batch(self, recs):
        self.records.extend(recs)


class _HangingWriter:
    """Writes block until ``release`` is set — simulates a file-lock hang."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()

    def _block(self):
        self.started.set()
        self.release.wait(timeout=10)

    def write_measurement(self, rec):
        self._block()

    def write_measurements_batch(self, recs):
        self._block()


def _wait_lock_free(svc, writer, timeout=10.0):
    """Block until the writer's lock is releasable (orphan finished)."""
    lock = svc._writer_lock(writer)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lock.acquire(blocking=False):
            lock.release()
            return True
        time.sleep(0.02)
    return False


def test_timed_write_success_releases_lock():
    svc = _bare_service()
    w = _FastWriter()
    assert svc._timed_write(w, {"x": 1}, "test") is True
    assert svc._timed_write(w, {"x": 2}, "test") is True
    assert w.records == [{"x": 1}, {"x": 2}]


def test_timed_write_timeout_then_next_write_skips():
    """P-M20: when a write times out its thread keeps the per-writer
    lock; the next write to that writer skips rather than racing the
    same HDF5 handle, and once the orphan finishes writes resume."""
    svc = _bare_service(timeout_s=0.3)
    w = _HangingWriter()

    # First write hangs → times out → False.
    assert svc._timed_write(w, {"x": 1}, "hang") is False
    assert w.started.is_set()

    # Second write to the SAME writer: orphan still holds the lock → skip,
    # without starting a competing thread.
    assert svc._timed_write(w, {"x": 2}, "hang") is False
    assert svc._write_timeout_count == 2  # one timeout + one skip

    # Release the orphan; the lock frees and writes resume.
    w.release.set()
    assert _wait_lock_free(svc, w), "orphan never released the writer lock"
    assert svc._timed_write(w, {"x": 3}, "hang") is True


def test_timed_write_locks_are_per_writer():
    """A hung write to one writer must not block writes to another."""
    svc = _bare_service(timeout_s=0.3)
    w_hang = _HangingWriter()
    w_ok = _FastWriter()

    assert svc._timed_write(w_hang, {"a": 1}, "hang") is False  # times out
    # A different writer is unaffected.
    assert svc._timed_write(w_ok, {"b": 2}, "ok") is True

    w_hang.release.set()
    _wait_lock_free(svc, w_hang)


def test_timed_write_batch_shares_lock_with_single_write():
    """_timed_write_batch and _timed_write to the same writer share the
    per-writer lock — a hung batch makes a following single write skip."""
    svc = _bare_service(timeout_s=0.3)
    w = _HangingWriter()

    assert svc._timed_write_batch(w, [{"a": 1}], "batch") is False  # hangs
    assert svc._timed_write(w, {"b": 2}, "single") is False  # skips

    w.release.set()
    _wait_lock_free(svc, w)


def test_timed_write_batch_empty_is_noop():
    svc = _bare_service()
    assert svc._timed_write_batch(_FastWriter(), [], "empty") is True


# --------------------------------------------------------------------------
# P-M22 — F2 reflection height from the ionospheric model
# --------------------------------------------------------------------------
class _FakeHeights:
    def __init__(self, hmF2):
        self.hmF2 = hmF2


class _FakeModel:
    def __init__(self, hmF2):
        self._hmF2 = hmF2

    def get_layer_heights(self, timestamp, latitude, longitude):
        return _FakeHeights(self._hmF2)


def test_reflection_height_uses_model_value():
    """P-M22: the F2 reflection height comes from the ionospheric model."""
    svc = _bare_service()
    svc._iono_model = _FakeModel(hmF2=342.0)
    h = svc._reflection_height_km(1_800_000_000, 40.0, -95.0)
    assert h == pytest.approx(342.0)


def test_reflection_height_falls_back_on_out_of_range():
    """An implausible model height falls back to the climatological default."""
    svc = _bare_service()
    svc._iono_model = _FakeModel(hmF2=9999.0)
    h = svc._reflection_height_km(1_800_000_000, 40.0, -95.0)
    assert h == _F2_FALLBACK_HEIGHT_KM


def test_reflection_height_falls_back_when_model_raises(monkeypatch):
    """A model failure must not propagate — fall back to the default."""
    svc = _bare_service()

    def _boom():
        raise RuntimeError("IRI unavailable")

    monkeypatch.setattr(svc, "_get_iono_model", _boom)
    h = svc._reflection_height_km(1_800_000_000, 40.0, -95.0)
    assert h == _F2_FALLBACK_HEIGHT_KM


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
