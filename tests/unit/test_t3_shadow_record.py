"""T3ShadowRecord: compact JSONL rows, one file per stream per UTC day."""
import json
import logging
import os
from datetime import datetime, timezone

import pytest

from hf_timestd.core.t3_shadow_record import T3ShadowRecord


def _ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


def _minute_row(utc_minute):
    return {
        "kind": "minute", "utc_minute": utc_minute,
        "t3": {
            "state": "VERIFIED", "sigma_ms": 0.83,
            "contributing": ["WWV_2_5", "WWV_5", "WWV_10", "WWV_15",
                             "CHU_3330", "CHU_7850"],
            "waiting": ["WWV_20"], "n_minutes": 37, "verified": True,
            "whole_second_unresolved": False, "rtp_domain": "ka9q-rtp-32",
            "counter_epoch_id": "20261030T000112Z-0003",
        },
        "t3_shadow": {"offset_ms": 1.274, "sigma_ms": 0.91, "n": 6,
                      "state": "VERIFIED", "stale": False},
        "t3_vs_adopted": {"adopted_tier": "T6", "diff_ms": 0.041,
                          "within_sigma": True},
        "t6_offset_ms": -0.012, "t4_offset_ms": 0.35, "t2_offset_ms": 2.9,
        "host_clock_verdict": "agrees", "t3_transitions_total": 2,
    }


def test_rows_land_in_the_day_file_of_their_minute(tmp_path):
    rec = T3ShadowRecord("minutes", tmp_path)
    rec.append({"utc_minute": _ts(2026, 10, 30, 1, 0)})
    rec.append({"utc_minute": _ts(2026, 10, 30, 23, 59)})
    rec.append({"utc_minute": _ts(2026, 10, 31, 0, 0)})
    a = (tmp_path / "minutes-20261030.jsonl").read_text().splitlines()
    b = (tmp_path / "minutes-20261031.jsonl").read_text().splitlines()
    assert len(a) == 2 and len(b) == 1
    assert json.loads(b[0])["utc_minute"] == _ts(2026, 10, 31)
    assert ", " not in a[0] and ": " not in a[0]  # compact separators


def test_files_past_retention_are_pruned_on_a_new_day(tmp_path):
    old_mine = tmp_path / "minutes-20261010.jsonl"
    old_other = tmp_path / "other-20261010.jsonl"
    keep = tmp_path / "minutes-20261025.jsonl"
    for p in (old_mine, old_other, keep):
        p.write_text("{}\n")
    rec = T3ShadowRecord("minutes", tmp_path, retention_days=14)
    rec.append({"utc_minute": _ts(2026, 10, 30, 0, 0)})
    assert not old_mine.exists()
    assert old_other.exists()
    assert keep.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_directory_logs_once_and_never_raises(tmp_path, caplog):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        rec = T3ShadowRecord("minutes", d)
        with caplog.at_level(logging.DEBUG):
            for i in range(3):
                rec.append({"utc_minute": _ts(2026, 10, 30, 0, i)})
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        d.chmod(0o700)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            rec.append({"utc_minute": _ts(2026, 10, 30, 0, 5)})
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert (d / "minutes-20261030.jsonl").exists()
        # a second streak logs again
        d.chmod(0o500)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            rec.append({"utc_minute": _ts(2026, 10, 31, 0, 6)})
        assert len([r for r in caplog.records
                    if r.levelno >= logging.ERROR]) == 1
    finally:
        d.chmod(0o700)


def test_a_row_stays_small():
    row = _minute_row(_ts(2026, 10, 30, 12, 34))
    n = len(json.dumps(row, separators=(",", ":")).encode())
    assert n < 2048, n


def test_row_without_minute_never_raises(tmp_path, caplog):
    rec = T3ShadowRecord("minutes", tmp_path)
    with caplog.at_level(logging.DEBUG):
        rec.append({"kind": "minute"})
    assert len([r for r in caplog.records
                if r.levelno >= logging.ERROR]) == 1
