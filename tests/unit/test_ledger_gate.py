"""The sigmond ledger, when it manages hf-timestd, replaces the percentage quota."""
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd.ledger_gate import ledger_manages_us
from hf_timestd.quota_manager import QuotaManager

NOW = 1_800_000_000.0
GEN = "2026-11-18T12:22:04Z"


def _gen_epoch():
    from datetime import datetime, timezone
    return datetime.strptime(GEN, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def _row(client="hf-timestd", managed=True, store="root"):
    return {"allotment": 210174216, "client": client, "days_held": 0.0,
            "estimated": True, "floor": 0, "held": 0, "managed": managed,
            "rate_bytes_per_day": 65000000.0, "rate_source": "declared",
            "stopped_by_storage": False, "store": store,
            "target": 210174216, "trim": None}


def _write(path, rows, generated_at=GEN):
    path.write_text(json.dumps({"schema": 1, "generated_at": generated_at,
                                "clients": rows, "alarms": []}))
    return path


def test_fresh_managed_true(tmp_path):
    p = _write(tmp_path / "s.json", [_row("codar-sounder", False), _row()])
    assert ledger_manages_us(p, now=lambda: _gen_epoch() + 60) is True


def test_any_store_row_managed_counts(tmp_path):
    p = _write(tmp_path / "s.json",
               [_row(managed=False, store="root"), _row(managed=True, store="data")])
    assert ledger_manages_us(p, now=lambda: _gen_epoch() + 1) is True


def test_stale_false(tmp_path):
    p = _write(tmp_path / "s.json", [_row()])
    assert ledger_manages_us(p, now=lambda: _gen_epoch() + 901) is False
    assert ledger_manages_us(p, now=lambda: _gen_epoch() + 900) is True


def test_future_stamp_is_stale(tmp_path):
    p = _write(tmp_path / "s.json", [_row()])
    assert ledger_manages_us(p, now=lambda: _gen_epoch() - 61) is False
    assert ledger_manages_us(p, now=lambda: _gen_epoch() - 60) is True


def test_unmanaged_row_false(tmp_path):
    p = _write(tmp_path / "s.json", [_row(managed=False), _row("other", True)])
    assert ledger_manages_us(p, now=lambda: _gen_epoch()) is False


def test_missing_or_bad_json_false(tmp_path):
    assert ledger_manages_us(tmp_path / "none.json", now=lambda: _gen_epoch()) is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ledger_manages_us(bad, now=lambda: _gen_epoch()) is False
    bad.write_text(json.dumps({"clients": [_row()]}))          # no generated_at
    assert ledger_manages_us(bad, now=lambda: _gen_epoch()) is False
    bad.write_text(json.dumps({"generated_at": "yesterday", "clients": [_row()]}))
    assert ledger_manages_us(bad, now=lambda: _gen_epoch()) is False
    bad.write_text(json.dumps({"generated_at": GEN, "clients": "x"}))
    assert ledger_manages_us(bad, now=lambda: _gen_epoch()) is False


def test_not_cached_across_calls(tmp_path):
    p = _write(tmp_path / "s.json", [_row()])
    t = lambda: _gen_epoch()
    assert ledger_manages_us(p, now=t) is True
    _write(p, [_row(managed=False)])
    assert ledger_manages_us(p, now=t) is False


# ---- QuotaManager ----------------------------------------------------------

def _old_raw(root, days_old=30):
    from datetime import datetime, timedelta, timezone
    d = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime('%Y%m%d')
    day = Path(root) / 'products' / 'WWV_10000' / 'decimated'
    day.mkdir(parents=True)
    f = day / f'{d}.bin'
    f.write_bytes(b'x' * 30)
    return f


def _over(mock_du):
    mock_du.return_value.used = 90_000_000
    mock_du.return_value.total = 100_000_000
    mock_du.return_value.free = 10_000_000


def test_quota_skips_when_managed(tmp_path, caplog):
    day = _old_raw(tmp_path)
    m = QuotaManager(data_root=tmp_path, threshold_percent=75.0, min_days_to_keep=2)
    with patch('hf_timestd.quota_manager.ledger_manages_us', return_value=True), \
         patch('hf_timestd.quota_manager.shutil.disk_usage') as du, \
         caplog.at_level(logging.INFO, logger='hf_timestd.quota_manager'):
        _over(du)
        r1 = m.enforce_quota()
        r2 = m.enforce_quota()
        r3 = m.enforce_quota()
    assert day.exists()
    assert r1['files_deleted'] == 0 and r1['bytes_freed'] == 0
    assert r3['files_deleted'] == 0
    ledger = [r for r in caplog.records
              if r.levelname == 'INFO' and 'ledger' in r.getMessage().lower()]
    assert len(ledger) == 1


def test_quota_logs_clear_once_and_resumes(tmp_path, caplog):
    day = _old_raw(tmp_path)
    m = QuotaManager(data_root=tmp_path, threshold_percent=75.0, min_days_to_keep=2)
    with patch('hf_timestd.quota_manager.shutil.disk_usage') as du, \
         caplog.at_level(logging.INFO, logger='hf_timestd.quota_manager'):
        _over(du)
        with patch('hf_timestd.quota_manager.ledger_manages_us', return_value=True):
            m.enforce_quota()
        with patch('hf_timestd.quota_manager.ledger_manages_us', return_value=False):
            r = m.enforce_quota()
    assert not day.exists()
    assert r['files_deleted'] >= 1
    msgs = [r.getMessage().lower() for r in caplog.records if 'ledger' in r.getMessage().lower()]
    assert len(msgs) == 2          # one at start, one at clear


def test_quota_unchanged_when_not_managed(tmp_path):
    day = _old_raw(tmp_path)
    m = QuotaManager(data_root=tmp_path, threshold_percent=75.0, min_days_to_keep=2)
    with patch('hf_timestd.quota_manager.ledger_manages_us', return_value=False), \
         patch('hf_timestd.quota_manager.shutil.disk_usage') as du:
        _over(du)
        r = m.enforce_quota()
    assert not day.exists()
    assert r['files_deleted'] >= 1
