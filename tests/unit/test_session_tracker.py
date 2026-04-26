"""
Unit tests for hf_timestd.session_tracker

SessionBoundaryTracker detects gaps when the recorder daemon was offline
between sessions and persists JSONL records of those boundaries.
"""

import json
import time
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.interfaces.data_models import DiscontinuityType
from hf_timestd.session_tracker import SessionBoundaryTracker


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tracker(tmp_path):
    return SessionBoundaryTracker(
        archive_dir=tmp_path,
        channel_name='WWV 10 MHz',
        sample_rate=24000,
    )


def write_npz(channel_dir: Path, name: str, *, timestamp: float, n_samples: int):
    channel_dir.mkdir(parents=True, exist_ok=True)
    f = channel_dir / name
    np.savez(f, timestamp=timestamp, samples=np.zeros(n_samples, dtype=np.complex64))
    return f


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_creates_archive_dir(self, tmp_path):
        archive = tmp_path / 'sub' / 'archive'
        SessionBoundaryTracker(archive, 'CHU 7.85 MHz', sample_rate=24000)
        assert archive.is_dir()

    def test_session_log_file_lives_in_archive_root(self, tracker, tmp_path):
        assert tracker.session_log_file == tmp_path / 'session_boundaries.jsonl'


# =============================================================================
# _get_last_session_end_time
# =============================================================================


class TestGetLastSessionEndTime:
    def test_no_channel_dir_returns_none(self, tracker):
        assert tracker._get_last_session_end_time() is None

    def test_no_npz_files_returns_none(self, tracker, tmp_path):
        # Create the channel directory but no NPZs
        (tmp_path / 'WWV_10_MHz').mkdir()
        assert tracker._get_last_session_end_time() is None

    def test_returns_end_of_latest_npz(self, tracker, tmp_path):
        chan = tmp_path / 'WWV_10_MHz'
        write_npz(chan, '20260426T120000Z.npz', timestamp=1_000_000.0,
                  n_samples=24000)
        # End = start + n_samples / sample_rate = 1_000_000 + 1.0
        end = tracker._get_last_session_end_time()
        assert end == pytest.approx(1_000_001.0)

    def test_picks_most_recent_alphabetical(self, tracker, tmp_path):
        chan = tmp_path / 'WWV_10_MHz'
        write_npz(chan, '20260101T000000Z.npz', timestamp=100.0,
                  n_samples=24000)
        write_npz(chan, '20260426T120000Z.npz', timestamp=2_000_000.0,
                  n_samples=24000)
        end = tracker._get_last_session_end_time()
        # Latest file dictates the end-time
        assert end == pytest.approx(2_000_001.0)

    def test_unreadable_npz_returns_none_and_logs(self, tracker, tmp_path,
                                                   caplog):
        chan = tmp_path / 'WWV_10_MHz'
        chan.mkdir()
        f = chan / '20260426T120000Z.npz'
        f.write_bytes(b'not a real npz file')
        end = tracker._get_last_session_end_time()
        assert end is None
        assert any('Error reading' in r.message for r in caplog.records)


# =============================================================================
# check_for_offline_gap
# =============================================================================


class TestCheckForOfflineGap:
    def test_no_previous_session_returns_none(self, tracker):
        # No prior NPZ → first run for this channel
        assert tracker.check_for_offline_gap(time.time()) is None

    def test_short_gap_ignored(self, tracker, tmp_path):
        # Last session ended 30 seconds ago — under the 120 s threshold
        chan = tmp_path / 'WWV_10_MHz'
        # 24000 samples = 1 s of data
        last_start = time.time() - 31.0
        write_npz(chan, '20260426T000000Z.npz', timestamp=last_start,
                  n_samples=24000)
        result = tracker.check_for_offline_gap(time.time())
        assert result is None

    def test_long_gap_creates_discontinuity(self, tracker, tmp_path):
        chan = tmp_path / 'WWV_10_MHz'
        # Last session ended an hour ago
        last_start = time.time() - 3600 - 1
        write_npz(chan, '20260426T000000Z.npz', timestamp=last_start,
                  n_samples=24000)
        current = time.time()
        d = tracker.check_for_offline_gap(current)
        assert d is not None
        assert d.discontinuity_type == DiscontinuityType.RECORDER_OFFLINE
        assert d.magnitude_samples > 0
        assert d.magnitude_ms > 0
        # Persisted to log
        assert tracker.session_log_file.exists()
        log = tracker.session_log_file.read_text().strip().splitlines()
        assert len(log) == 1
        record = json.loads(log[0])
        assert record['gap_type'] == 'RECORDER_OFFLINE'
        assert record['channel'] == 'WWV 10 MHz'
        assert record['gap_duration_sec'] > 120

    def test_handles_unreadable_last_session(self, tracker, tmp_path):
        # Unparseable NPZ → _get_last_session_end_time returns None
        # → check_for_offline_gap also returns None (no prior session detected)
        chan = tmp_path / 'WWV_10_MHz'
        chan.mkdir()
        (chan / '20260426T000000Z.npz').write_bytes(b'garbage')
        assert tracker.check_for_offline_gap(time.time()) is None


# =============================================================================
# get_session_history
# =============================================================================


class TestGetSessionHistory:
    def test_no_log_file_returns_empty(self, tracker):
        assert tracker.get_session_history() == []

    def test_filters_by_channel(self, tracker, tmp_path):
        # Pre-populate the log with mixed channels
        log = tracker.session_log_file
        log.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {'channel': 'WWV 10 MHz',
             'current_session_start': time.time() - 86400},
            {'channel': 'CHU 3.33 MHz',
             'current_session_start': time.time() - 86400},
        ]
        with open(log, 'w') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
        history = tracker.get_session_history(days=7)
        assert len(history) == 1
        assert history[0]['channel'] == 'WWV 10 MHz'

    def test_filters_by_age(self, tracker):
        log = tracker.session_log_file
        log.parent.mkdir(parents=True, exist_ok=True)
        recent = {'channel': 'WWV 10 MHz',
                  'current_session_start': time.time() - 60}
        old = {'channel': 'WWV 10 MHz',
               'current_session_start': time.time() - 30 * 86400}
        with open(log, 'w') as f:
            f.write(json.dumps(recent) + '\n')
            f.write(json.dumps(old) + '\n')
        history = tracker.get_session_history(days=7)
        assert len(history) == 1
        # Old record dropped — only the recent one survives
        assert history[0]['current_session_start'] > time.time() - 86400

    def test_sorts_most_recent_first(self, tracker):
        log = tracker.session_log_file
        log.parent.mkdir(parents=True, exist_ok=True)
        a = {'channel': 'WWV 10 MHz',
             'current_session_start': time.time() - 60}
        b = {'channel': 'WWV 10 MHz',
             'current_session_start': time.time() - 600}
        with open(log, 'w') as f:
            f.write(json.dumps(b) + '\n')  # written first
            f.write(json.dumps(a) + '\n')  # written second (newer)
        history = tracker.get_session_history(days=7)
        # Most recent first
        assert history[0]['current_session_start'] > history[1]['current_session_start']

    def test_malformed_log_returns_empty(self, tracker, caplog):
        log = tracker.session_log_file
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("not json\n")
        history = tracker.get_session_history()
        assert history == []
        assert any('Error reading' in r.message for r in caplog.records)
