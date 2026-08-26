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


class TestSelfPruning:
    """The capture must bound its own footprint.

    `quota_manager` walks raw_buffer/<channel>/<YYYYMMDD>/ and phase2/
    day-directories; it cannot see flat files under state/t6-anomaly, so
    these would accumulate forever.  On AC0G-B4 that is up to 352 MB/day
    on a host already at 83 %, and the fleet board is blind to disk-full.

    The module that creates the files owns their budget -- that also
    survives the prune timer being disabled or broken.
    """

    def _fill(self, tmp_path, n, retain_bytes):
        c = cap(tmp_path, retain_bytes=retain_bytes, min_interval_s=0.0,
                max_per_day=1000)
        for _ in range(4):
            c.add(batch())          # ~4 * 9600 * 8 = 307 KB per dump
        for i in range(n):
            c.trigger(f"e{i}", now=1_700_000_000.0 + i)
        return c

    def test_oldest_dumps_are_evicted_past_the_cap(self, tmp_path):
        self._fill(tmp_path, n=10, retain_bytes=700_000)
        kept = sorted(p.name for p in tmp_path.glob("*.iq"))
        assert len(kept) == 2, kept
        # the SURVIVORS are the newest, not the oldest
        assert kept[-1].endswith("e9.iq")

    def test_a_generous_cap_keeps_everything(self, tmp_path):
        self._fill(tmp_path, n=5, retain_bytes=100_000_000)
        assert len(list(tmp_path.glob("*.iq"))) == 5

    def test_pruning_never_deletes_the_dump_just_written(self, tmp_path):
        """Even a cap smaller than one dump must leave the newest file."""
        c = self._fill(tmp_path, n=3, retain_bytes=1)
        files = list(tmp_path.glob("*.iq"))
        assert len(files) == 1
        assert files[0].name.endswith("e2.iq")

    def test_pruning_ignores_foreign_files(self, tmp_path):
        (tmp_path / "README.txt").write_text("not mine")
        self._fill(tmp_path, n=10, retain_bytes=700_000)
        assert (tmp_path / "README.txt").exists()


class TestBudgetSurvivesRestart:
    """`max_per_day` counted in memory, so a restart reset it.

    Observed on AC0G-B4 2026-08-26: the log said "2/8 dumps used today"
    at 07:49 while the directory already held 8 files for the date --
    the 08:01 restart (hpps-watchdog recovering T6 SHM) had zeroed the
    counter.  A restart LOOP therefore blew straight through both the
    daily cap and the interval floor: 00:15-00:26 saw three restarts in
    eleven minutes, and each would have been free to dump again.

    The directory IS the record.  Derive both the count and the last
    dump time from it, so the budget cannot be laundered by a restart
    and cannot drift from what is actually on disk.
    """

    def _cap(self, tmp_path, **kw):
        kw.setdefault("min_interval_s", 900.0)
        kw.setdefault("max_per_day", 3)
        c = cap(tmp_path, **kw)
        c.add(batch())
        return c

    def test_a_restart_does_not_refill_the_daily_cap(self, tmp_path):
        t0 = 1_700_000_000.0
        first = self._cap(tmp_path)
        for i in range(3):
            first.trigger(f"a{i}", now=t0 + i * 1000)
        assert len(list(tmp_path.glob("*.iq"))) == 3
        # process restarts: brand-new object, same directory
        second = self._cap(tmp_path)
        assert second.trigger("after-restart", now=t0 + 5000) is None
        assert len(list(tmp_path.glob("*.iq"))) == 3

    def test_a_restart_does_not_bypass_the_interval_floor(self, tmp_path):
        t0 = 1_700_000_000.0
        first = self._cap(tmp_path, max_per_day=100)
        assert first.trigger("x", now=t0) is not None
        second = self._cap(tmp_path, max_per_day=100)
        assert second.trigger("y", now=t0 + 10) is None       # inside 900 s
        assert second.trigger("z", now=t0 + 901) is not None

    def test_a_new_day_still_refills(self, tmp_path):
        t0 = 1_700_000_000.0
        c = self._cap(tmp_path)
        for i in range(3):
            c.trigger(f"a{i}", now=t0 + i * 1000)
        assert c.trigger("same-day", now=t0 + 4000) is None
        fresh = self._cap(tmp_path)
        assert fresh.trigger("next-day", now=t0 + 86_400 * 2) is not None

    def test_an_empty_directory_is_a_full_budget(self, tmp_path):
        assert self._cap(tmp_path).may_dump(now=1_700_000_000.0)

    def test_foreign_files_do_not_consume_budget(self, tmp_path):
        for n in ("notes.txt", "t6-anomaly-garbage.iq"):
            (tmp_path / n).write_text("x")
        assert self._cap(tmp_path).may_dump(now=1_700_000_000.0)
