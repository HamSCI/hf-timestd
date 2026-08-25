"""Keep T6 IQ around anomalies, under a budget that a 1 Hz pathology
cannot blow through.

The archival decision keeps only the anchor ledger (0.37 MB/day) instead
of the continuous 96 kHz stream (~60 GB/day).  The cost is that a ledger
row is the matched filter's OUTPUT and cannot be re-derived -- both the
2026-05-23 sidelobe phantom and the 2026-08-25 livelock were
precise-looking anchors that were wrong.  Raw IQ around the rare bad
moments buys that back.

The load-bearing part is the rate limit: the 2026-08-25 livelock
re-entered its failure branch at ~1 Hz for HOURS.  Unlimited triggering
would have written thousands of 46 MB dumps and filled the disk, turning
a diagnostic into an outage.
"""
from __future__ import annotations

import numpy as np
import pytest

from hf_timestd.core.t6_anomaly_capture import AnomalyCapture

SR = 96_000


def cap(tmp_path, **kw):
    kw.setdefault("sample_rate_hz", SR)
    kw.setdefault("window_s", 1.0)
    return AnomalyCapture(tmp_path, now_fn=lambda: 1_700_000_000.0, **kw)


def batch(n=9600):
    return np.zeros(n, dtype=np.complex64)


class TestRing:
    def test_ring_holds_about_one_window(self, tmp_path):
        c = cap(tmp_path)
        for _ in range(30):
            c.add(batch())
        assert c.held_samples <= SR + 9600
        assert c.held_samples >= SR - 9600

    def test_empty_batches_are_ignored(self, tmp_path):
        c = cap(tmp_path)
        c.add(np.zeros(0, dtype=np.complex64))
        assert c.held_samples == 0

    def test_nothing_held_means_nothing_written(self, tmp_path):
        assert cap(tmp_path).trigger("unlock") is None


class TestRateLimit:
    """The part that stands between a 1 Hz pathology and a full disk."""

    def test_a_1hz_pathology_yields_one_dump_per_interval(self, tmp_path):
        c = cap(tmp_path, min_interval_s=900.0, max_per_day=20)
        c.add(batch())
        dumps = 0
        # one hour of the 2026-08-25 livelock, triggering every second
        for s in range(3600):
            if c.trigger("rtp-discontinuity", now=1_700_000_000.0 + s):
                dumps += 1
        assert dumps == 4, f"expected 3600/900 = 4 dumps, got {dumps}"
        assert c.suppressed == 3600 - 4

    def test_daily_cap_backstops_a_long_pathology(self, tmp_path):
        c = cap(tmp_path, min_interval_s=0.0, max_per_day=20)
        c.add(batch())
        dumps = sum(
            1 for s in range(200)
            if c.trigger("unlock", now=1_700_000_000.0 + s)
        )
        assert dumps == 20

    def test_the_cap_resets_on_a_new_utc_day(self, tmp_path):
        c = cap(tmp_path, min_interval_s=0.0, max_per_day=2)
        c.add(batch())
        t0 = 1_700_000_000.0
        assert c.trigger("a", now=t0) and c.trigger("b", now=t0 + 1)
        assert c.trigger("c", now=t0 + 2) is None
        assert c.trigger("d", now=t0 + 86_400 * 2) is not None

    def test_suppression_is_counted_not_silent(self, tmp_path):
        """An absent dump must never be read as an absent anomaly."""
        c = cap(tmp_path, min_interval_s=900.0)
        c.add(batch())
        c.trigger("x", now=1_700_000_000.0)
        for s in range(1, 50):
            c.trigger("x", now=1_700_000_000.0 + s)
        assert c.suppressed == 49

    def test_may_dump_is_a_pure_decision(self, tmp_path):
        c = cap(tmp_path, min_interval_s=900.0)
        assert c.may_dump(now=1_700_000_000.0)
        c.add(batch())
        c.trigger("x", now=1_700_000_000.0)
        assert not c.may_dump(now=1_700_000_000.0 + 10)
        assert c.may_dump(now=1_700_000_000.0 + 901)


class TestDump:
    def test_writes_the_held_window(self, tmp_path):
        c = cap(tmp_path)
        for _ in range(4):
            c.add(batch())
        p = c.trigger("costas-unlock")
        assert p is not None and p.exists()
        assert p.stat().st_size == c.held_samples * 8   # complex64
        assert "costas-unlock" in p.name

    def test_reason_is_sanitised_into_the_filename(self, tmp_path):
        c = cap(tmp_path)
        c.add(batch())
        p = c.trigger("rtp counter/discontinuity")
        assert "/" not in p.name and " " not in p.name

    def test_an_unwritable_dir_does_not_raise(self, tmp_path):
        c = AnomalyCapture(tmp_path / "f" / "deeper", sample_rate_hz=SR,
                           window_s=1.0, now_fn=lambda: 1_700_000_000.0)
        c.add(batch())
        (tmp_path / "f").write_text("not a dir")
        assert c.trigger("unlock") is None

    def test_budget_is_not_consumed_by_a_failed_write(self, tmp_path):
        c = AnomalyCapture(tmp_path / "f" / "deeper", sample_rate_hz=SR,
                           window_s=1.0, min_interval_s=900.0,
                           now_fn=lambda: 1_700_000_000.0)
        c.add(batch())
        (tmp_path / "f").write_text("not a dir")
        c.trigger("unlock")
        assert c.may_dump(now=1_700_000_000.0)
