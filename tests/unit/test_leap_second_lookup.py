#!/usr/bin/env python3
"""
Unit tests for ``gps_leap_seconds_at_gps_time`` (M-M4 remediation).

The historical pattern — captured at module import:

    GPS_LEAP_SECONDS = get_current_gps_leap_seconds()

silently went stale whenever the daemon crossed a leap-second insertion,
poisoning every UTC derived from GPS_TIME after the boundary by 1 s.
The replacement looks the offset up *per buffer*, keyed off the buffer's
own GPS time, against an mtime-cached parse of
``/usr/share/zoneinfo/leap-seconds.list``.

These tests pin the behaviour:

  1. The threshold is the right one — a GPS time one second BEFORE an
     insertion gets the old offset; one second AFTER gets the new.
  2. A missing leap-seconds.list falls back to 18.
  3. The cache picks up file edits via mtime so a new leap second
     shipped by the OS package manager is honoured without a restart.
  4. Buffer timing inside the same buffer's metadata returns offsets
     consistent with the snapshot's own GPS time.
"""

import os
import time
from pathlib import Path

import pytest

from hf_timestd.core.leap_second import (
    _LEAP_TABLE_CACHE,
    _NS_PER_S,
    gps_leap_seconds_at_gps_time,
)


# A synthetic leap-seconds.list spanning realistic dates plus one fictional
# 2030 entry, so the tests stay sharp even after the real file is updated.
_FAKE_LEAP_FILE = """\
# Header comment
# Each line: <NTP_seconds> <DTAI> # <date>
3644697600      36      # 1 Jul 2015
3692217600      37      # 1 Jan 2017
3881520000      38      # 1 Jan 2023 (fictional, for testing)
"""

# NTP-to-Unix offset = 2208988800; GPS_EPOCH_UNIX = 315964800; TAI-GPS = 19.
# Per-entry GPS-since-GPS-epoch thresholds (offset = DTAI − 19):
#   2015 row: 1119744017 → offset 17
#   2017 row: 1167264018 → offset 18
#   2023 row: 1356566419 → offset 19 (fictional)


@pytest.fixture
def leap_file(tmp_path: Path) -> Path:
    """Write a fake leap-seconds.list and clear the module cache so each test
    sees a fresh parse."""
    path = tmp_path / "leap-seconds.list"
    path.write_text(_FAKE_LEAP_FILE)
    _LEAP_TABLE_CACHE.clear()
    yield path
    _LEAP_TABLE_CACHE.clear()


def _gps_ns(seconds_since_gps_epoch: float) -> int:
    return int(seconds_since_gps_epoch * _NS_PER_S)


class TestLeapSecondLookup:
    def test_returns_pre_insertion_offset_just_before_boundary(self, leap_file):
        # One second before the 2017-01-01 insertion threshold (1167264018).
        # The latest entry ≤ that GPS time is the 2015 row (offset 17).
        offset = gps_leap_seconds_at_gps_time(
            _gps_ns(1167264018 - 1), path=str(leap_file),
        )
        assert offset == 17

    def test_returns_post_insertion_offset_at_boundary(self, leap_file):
        offset = gps_leap_seconds_at_gps_time(
            _gps_ns(1167264018), path=str(leap_file),
        )
        assert offset == 37 - 19  # = 18

    def test_returns_post_insertion_offset_just_after_boundary(self, leap_file):
        offset = gps_leap_seconds_at_gps_time(
            _gps_ns(1167264018 + 1), path=str(leap_file),
        )
        assert offset == 18

    def test_jumps_to_next_offset_at_fictional_2023_boundary(self, leap_file):
        # Just before fictional 2023 threshold → old (18); at/after → new (19).
        before = gps_leap_seconds_at_gps_time(
            _gps_ns(1356566419 - 1), path=str(leap_file),
        )
        after = gps_leap_seconds_at_gps_time(
            _gps_ns(1356566419), path=str(leap_file),
        )
        assert before == 18
        assert after == 19

    def test_recent_gps_time_uses_latest_table_entry(self, leap_file):
        # GPS time deep into 2030 — beyond every entry — should use the
        # latest offset (the fictional 2023 row).
        offset = gps_leap_seconds_at_gps_time(
            _gps_ns(1_600_000_000), path=str(leap_file),
        )
        assert offset == 19


class TestLeapSecondFallback:
    def test_missing_file_falls_back_to_18(self, tmp_path: Path):
        _LEAP_TABLE_CACHE.clear()
        missing = tmp_path / "does-not-exist.list"
        offset = gps_leap_seconds_at_gps_time(_gps_ns(1167264018), path=str(missing))
        # _GPS_LEAP_SECONDS_FALLBACK is 18 (the value the project used
        # before this M-M4 fix).
        assert offset == 18

    def test_empty_file_falls_back(self, tmp_path: Path):
        _LEAP_TABLE_CACHE.clear()
        empty = tmp_path / "empty.list"
        empty.write_text("# only comments\n# and blank lines\n\n")
        offset = gps_leap_seconds_at_gps_time(_gps_ns(1167264018), path=str(empty))
        assert offset == 18

    def test_unparseable_lines_are_skipped(self, tmp_path: Path):
        _LEAP_TABLE_CACHE.clear()
        path = tmp_path / "leap.list"
        path.write_text(
            "garbage line with no numbers\n"
            "3692217600 37 # valid\n"
            "non integer XX\n"
        )
        # Should still find the valid 2017 entry.
        offset = gps_leap_seconds_at_gps_time(_gps_ns(1167264018), path=str(path))
        assert offset == 18

    def test_pre_table_gps_time_falls_back(self, tmp_path: Path):
        """A GPS time at the GPS epoch (1980-01-06) precedes the 1972
        threshold in the synthetic table, so the lookup falls back."""
        _LEAP_TABLE_CACHE.clear()
        path = tmp_path / "leap.list"
        # Only an entry far in the future of the GPS epoch.
        path.write_text("3692217600 37 # 1 Jan 2017\n")
        # GPS time = 0 → pre-1972 → before every threshold → fallback.
        offset = gps_leap_seconds_at_gps_time(0, path=str(path))
        assert offset == 18


class TestLeapSecondCacheRefresh:
    def test_mtime_change_picks_up_new_entry(self, tmp_path: Path):
        _LEAP_TABLE_CACHE.clear()
        path = tmp_path / "leap.list"
        path.write_text("3692217600 37 # 1 Jan 2017\n")

        gps_2017 = 1167264018
        offset_first = gps_leap_seconds_at_gps_time(
            _gps_ns(gps_2017 + 1_000_000_000), path=str(path),
        )
        assert offset_first == 18

        # Simulate the OS package manager shipping a new leap second.
        # Bump mtime far enough that filesystems with second-resolution
        # mtime still register a change.
        time.sleep(0.01)
        path.write_text(
            "3692217600 37 # 1 Jan 2017\n"
            "3881520000 38 # 1 Jan 2023 (new entry)\n"
        )
        future_mtime = os.path.getmtime(path) + 2
        os.utime(path, (future_mtime, future_mtime))

        # Same GPS time, refreshed table — should now use the 2023 offset.
        offset_second = gps_leap_seconds_at_gps_time(
            _gps_ns(gps_2017 + 1_000_000_000), path=str(path),
        )
        assert offset_second == 19


class TestBufferTimingUsesLookup:
    def test_buffer_timing_resolves_offset_per_buffer(self, leap_file, monkeypatch):
        """End-to-end: a buffer with `gps_time_ns` straddling the 2017
        boundary should land at the right Unix UTC. Two buffers, one
        either side of the insertion, must differ by exactly the
        physical interval — not by interval + 1 s — proving the per-buffer
        lookup picked up both offsets correctly."""
        from hf_timestd.core import buffer_timing as bt
        from hf_timestd.core import leap_second as ls

        monkeypatch.setattr(ls, "_LEAP_SECONDS_FILE", str(leap_file))
        # `gps_leap_seconds_at_gps_time` reads the module-level default
        # only when no `path=` is passed. buffer_timing calls it without
        # `path=`, so monkeypatching the default is what we need.
        # Defensive: also clear cache.
        _LEAP_TABLE_CACHE.clear()

        sample_rate = 24000
        # GPS time 60 s before the 2017 boundary; offset should be -9.
        gps_before = 1167264018 - 60
        # GPS time 60 s after the boundary; offset should be 18.
        gps_after = 1167264018 + 60

        rtp_snap = 1000
        meta_before = {
            'start_rtp_timestamp': rtp_snap,
            'gps_time_ns': gps_before * _NS_PER_S,
            'rtp_timesnap': rtp_snap,
        }
        meta_after = {
            'start_rtp_timestamp': rtp_snap,
            'gps_time_ns': gps_after * _NS_PER_S,
            'rtp_timesnap': rtp_snap,
        }

        t_before = bt.resolve_buffer_timing(meta_before, sample_rate=sample_rate)
        t_after = bt.resolve_buffer_timing(meta_after, sample_rate=sample_rate)

        # Δ in *GPS* time was 120 s. Δ in UTC must also be 120 s minus
        # the leap-second jump of 1 s — i.e. 119 s. If the old import-time
        # constant were used (single offset for both buffers), Δ would be
        # exactly 120 s and the leap second would silently vanish.
        delta_utc = t_after.sample0_utc - t_before.sample0_utc
        assert abs(delta_utc - 119.0) < 1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
