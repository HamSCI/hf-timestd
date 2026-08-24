"""LabelPlaneTracker — the measured (label − host) plane offset.

Under the content-time labelling convention a T6 label answers "when did
this energy reach the antenna", so the label plane sits one pipeline
latency EARLIER than the host plane.  The judge needs that expected
difference to compare a label-plane bench against a host-plane one
(``label_plane_offset_ns``); asserting it as a constant is what the
convention proposal set out to stop, so it is measured instead.

Three properties make the measurement honest rather than circular:

* it is only observed while the host clock is independently disciplined
  (chrony on FUSE/T5 holds ~20 µs, three orders below the ~16 ms term),
  and each observation carries that host sigma;
* it is SLOW — a long-window median of a physical latency that drifts —
  while the cross-bench gate is instantaneous.  A genuine T6 epoch step
  therefore still shows up at full amplitude instead of being absorbed
  into the correction that is supposed to reveal it;
* with no usable estimate it returns None, and the judge falls back to
  its configured value rather than inventing one.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.label_plane import LabelPlaneTracker

MS = 1_000_000.0          # ns per ms
TRANSPORT_NS = -16.5 * MS  # label reads 16.5 ms EARLIER than host


def _feed(tracker, n, *, offset_ns=TRANSPORT_NS, host_sigma_ns=20_000.0,
          t0=1000.0, dt=10.0):
    """n paired readings a judge tick apart, at a steady plane offset."""
    for i in range(n):
        mono = t0 + i * dt
        host_utc = 1_787_000_000.0 + i * dt
        label_utc = host_utc + offset_ns / 1e9
        tracker.observe(label_utc=label_utc, host_utc=host_utc,
                        host_sigma_ns=host_sigma_ns, mono=mono)


def test_no_observations_yields_no_estimate():
    assert LabelPlaneTracker().estimate() is None


def test_a_single_observation_is_not_enough():
    """One sample cannot separate a plane offset from a transient."""
    t = LabelPlaneTracker(min_n=8)
    _feed(t, 1)
    assert t.estimate() is None


def test_steady_transport_is_recovered():
    t = LabelPlaneTracker(min_n=8)
    _feed(t, 30)
    est = t.estimate()
    assert est is not None
    assert est.offset_ns == pytest.approx(TRANSPORT_NS, abs=50_000.0)
    assert est.n == 30


def test_estimate_is_robust_to_outliers():
    """A late arrival must not drag the plane term; the median holds."""
    t = LabelPlaneTracker(min_n=8)
    _feed(t, 20)
    # three wildly late samples (a scheduling stall, say), on the next ticks
    _feed(t, 3, offset_ns=TRANSPORT_NS - 200 * MS, t0=1200.0)
    est = t.estimate()
    assert est.offset_ns == pytest.approx(TRANSPORT_NS, abs=200_000.0)


def test_an_undisciplined_host_is_refused():
    """Without an independently disciplined host the term is not honest."""
    t = LabelPlaneTracker(min_n=4, max_host_sigma_ns=1_000_000.0)
    _feed(t, 20, host_sigma_ns=50 * MS)      # 50 ms host: useless
    assert t.estimate() is None


def test_host_sigma_is_carried_into_the_estimate():
    t = LabelPlaneTracker(min_n=4)
    _feed(t, 20, host_sigma_ns=200_000.0)
    est = t.estimate()
    # The term can never claim to be tighter than the clock it was
    # measured against.
    assert est.sigma_ns >= 200_000.0


def test_a_t6_step_is_not_absorbed_immediately():
    """The load-bearing property.

    If the correction tracked the instantaneous delta, a T6 epoch step
    would be cancelled by the very term meant to expose it.  After a step
    the estimate must still reflect the OLD plane, so the judge's
    instantaneous cross-bench delta sees the step at full amplitude.
    """
    t = LabelPlaneTracker(min_n=8)
    _feed(t, 40)
    before = t.estimate().offset_ns

    # T6 epoch jumps 5 ms; the next few ticks carry it.
    _feed(t, 3, offset_ns=TRANSPORT_NS + 5 * MS, t0=1400.0)
    after = t.estimate().offset_ns

    moved = abs(after - before)
    assert moved < 1 * MS, f"plane term absorbed {moved/MS:.2f} ms of a 5 ms step"


def test_stale_observations_expire():
    t = LabelPlaneTracker(min_n=4, window_s=100.0)
    _feed(t, 20, t0=1000.0, dt=1.0)          # spans 1000..1019
    assert t.estimate() is not None
    t.observe(label_utc=1.0, host_utc=1.0, host_sigma_ns=20_000.0,
              mono=5000.0)                    # far in the future
    assert t.estimate() is None               # window emptied, min_n unmet


def test_estimate_reports_its_span():
    t = LabelPlaneTracker(min_n=4)
    _feed(t, 10, dt=10.0)
    est = t.estimate()
    assert est.span_s == pytest.approx(90.0, abs=0.001)
