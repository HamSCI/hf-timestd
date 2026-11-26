"""Row retention for timestd.db and authority_history.db (contract 17.7 expire)."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SIG = os.environ.get("SIGMOND_LIB_FOR_TESTS") or str(
    Path(__file__).resolve().parents[3] / "sigmond" / "lib")
if os.path.isdir(_SIG):
    sys.path.insert(0, _SIG)
from hf_timestd import storage_items, storage_retention  # noqa: E402

needs_kit = pytest.mark.skipif(storage_items.load_kit() is None,
                               reason="sigmond client kit absent")

NOW = time.time()


def _iso(days_ago):
    t = datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _db(root, rel, table, col, ages):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {col} TEXT, v REAL)")
    con.executemany(f"INSERT INTO {table} ({col}, v) VALUES (?, 1.0)",
                    [(_iso(a),) for a in ages])
    con.commit()
    con.close()
    return p


def _count(p, table):
    con = sqlite3.connect(p)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_every_science_table_keeps_30_days():
    for t in ("L2_tick_phase", "L1_all_arrivals", "L2_test_signal"):
        assert storage_retention.TABLES[t] == 30


def test_days_by_group():
    T = storage_retention.TABLES
    for t in ("L1_metrology_measurements", "L2_timing_measurements", "L2_tick_timing",
              "L2_detection_attempts", "L3_fusion_timing", "DIAG_chrony_stats"):
        assert T[t] == storage_retention.TIMING_DAYS == 90
    for t in ("L2_decoder_comparison", "L1_bcd_timecode", "L1_tone_detections",
              "L1_broadcast_measurements", "L1_channel_observables", "L2_physics"):
        assert T[t] == storage_retention.RETIRED_DAYS == 30
    assert storage_retention.AUTHORITY_DAYS == 30


def test_vtec_and_l3b_belong_to_hamsci_physics():
    assert "L3_gnss_vtec" not in storage_retention.TABLES
    assert not [t for t in storage_retention.TABLES if t.startswith("L3b_")]
    assert "timestd.L3_gnss_vtec" not in storage_retention.TARGETS


def test_sinks_declare_every_target_with_expires_true(tmp_path):
    sinks = storage_items.sinks(tmp_path, 0)
    svc = {s["target"]: s for s in sinks if s["kind"] == "service"}
    assert svc["timestd.L2_tick_phase"]["retention_days"] == 30
    assert svc["timestd.L2_tick_phase"]["expires"] is True
    assert svc["timestd.L2_tick_timing"]["retention_days"] == 90
    assert svc["authority_history.authority_snapshot"]["retention_days"] == 30
    for s in svc.values():
        assert s["expires"] is True and s["schema_ref"] == "timestd:1" and s["mb_per_day"] == 0
    assert len(svc) == len(storage_retention.TABLES) + 1


def test_targets_name_the_real_time_columns():
    T = storage_retention.TARGETS
    assert T["timestd.L2_tick_phase"] == ("phase2/timestd.db", "L2_tick_phase", "timestamp_utc")
    assert T["authority_history.authority_snapshot"] == (
        "authority_history.db", "authority_snapshot", "utc_published")
    assert "timestd" in T  # P14: compaction names the whole file


@needs_kit
def test_expire_target_deletes_old_rows_of_its_own_table(tmp_path):
    ages = [40 + i * 0.01 for i in range(20)] + [10 + i * 0.01 for i in range(30)]
    p = _db(tmp_path, "phase2/timestd.db", "L2_tick_phase", "timestamp_utc", ages)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE L2_tick_timing (id INTEGER PRIMARY KEY, timestamp_utc TEXT)")
    con.execute("INSERT INTO L2_tick_timing (timestamp_utc) VALUES (?)", (_iso(40),))
    con.commit()
    con.close()
    a = storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert a["deleted"] == 20 and a["complete"] is True and a["refused"] is False
    assert a["unit"] == "rows"
    assert _count(p, "L2_tick_phase") == 30
    assert _count(p, "L2_tick_timing") == 1  # another table is untouched


@needs_kit
def test_expire_target_refuses_a_hamsci_physics_table(tmp_path):
    p = _db(tmp_path, "phase2/timestd.db", "L3_dtec", "timestamp_utc", [60])
    a = storage_retention.expire_target(tmp_path, "timestd.L3_dtec", 30, 60, now=NOW)
    assert a["refused"] and a["deleted"] == 0
    assert _count(p, "L3_dtec") == 1


@needs_kit
def test_expire_target_refuses_a_missing_database(tmp_path):
    a = storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert a["refused"] and a["deleted"] == 0 and "database" in a["reason"]


@needs_kit
def test_expire_target_refuses_the_whole_file_target(tmp_path):
    a = storage_retention.expire_target(tmp_path, "timestd", 30, 60, now=NOW)
    assert a["refused"] and a["deleted"] == 0


@needs_kit
def test_expire_target_prunes_authority_history_by_utc_published(tmp_path):
    p = _db(tmp_path, "authority_history.db", "authority_snapshot", "utc_published",
            [45, 40, 5, 1])
    a = storage_retention.expire_target(
        tmp_path, "authority_history.authority_snapshot", 30, 60, now=NOW)
    assert a["deleted"] == 2 and a["complete"] and not a["refused"]
    assert _count(p, "authority_snapshot") == 2


@needs_kit
def test_expire_target_refuses_an_epoch_column(tmp_path):
    p = tmp_path / "phase2" / "timestd.db"
    p.parent.mkdir()
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE L2_tick_phase (timestamp_utc REAL)")
    con.execute("INSERT INTO L2_tick_phase VALUES (1.0)")
    con.commit()
    con.close()
    a = storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert a["refused"] and a["deleted"] == 0
    assert _count(p, "L2_tick_phase") == 1


@needs_kit
def test_expire_target_journals_a_deletion(tmp_path, monkeypatch):
    _db(tmp_path, "phase2/timestd.db", "L2_tick_phase", "timestamp_utc", [40, 1])
    lines = []
    monkeypatch.setattr(storage_items, "_journal", lines.append)
    storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert len(lines) == 1 and "L2_tick_phase" in lines[0] and "1" in lines[0]
    lines.clear()
    storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert lines == []  # nothing deleted, nothing journaled


@needs_kit
def test_expire_target_runs_as_owner(monkeypatch, tmp_path):
    """The drop to the owner comes first: before the kit prunes, vacuums or connects."""
    p = _db(tmp_path, "phase2/timestd.db", "L2_tick_phase", "timestamp_utc", [40, 1])
    kit = storage_items.load_kit()
    calls = []
    monkeypatch.setattr(kit, "drop_to_owner", lambda path: calls.append(("drop", path)))
    real_prune, real_vac = kit.prune_rows, kit.incremental_vacuum
    monkeypatch.setattr(kit, "prune_rows",
                        lambda *a, **k: (calls.append(("prune", a[0])), real_prune(*a, **k))[1])
    monkeypatch.setattr(kit, "incremental_vacuum",
                        lambda *a, **k: (calls.append(("vacuum", a[0])), real_vac(*a, **k))[1])
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: (calls.append(("connect", a[0])),
                                         real_connect(*a, **k))[1])
    storage_retention.expire_target(tmp_path, "timestd.L2_tick_phase", 30, 60, now=NOW)
    assert calls[0] == ("drop", str(p))
    assert [c[0] for c in calls if c[0] in ("prune", "vacuum")] == ["prune", "vacuum"]


@needs_kit
def test_storage_expire_verb_is_wired(tmp_path):
    _db(tmp_path, "phase2/timestd.db", "L2_tick_phase", "timestamp_utc", [40, 1])
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tmp_path}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg), PYTHONPATH=_SIG)
    out = subprocess.run(
        [sys.executable, "-m", "hf_timestd.cli", "storage", "expire",
         "--target", "timestd.L2_tick_phase", "--days", "30", "--json"],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    j = json.loads(out.stdout)
    assert j["deleted"] == 1 and j["unit"] == "rows"
    assert _count(tmp_path / "phase2" / "timestd.db", "L2_tick_phase") == 1
