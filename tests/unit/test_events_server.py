"""wd30's side: the orders job and the upload verifier."""
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_window_drf import make_file, ts, utc
from hf_timestd.events import keeper
from hf_timestd.events.keeper import LocalTransport
from test_events_keeper import (CHANNELS, END, EID, READY, STATION, Usage, order,
                                put_orders, rdir, state)

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "events_server", REPO / "scripts" / "events" / "events_server.py")
es = importlib.util.module_from_spec(SPEC)
sys.modules["events_server"] = es
SPEC.loader.exec_module(es)

RECEIPT_RE = r"[0-9a-f]{64}  verified \S+\n?\Z"


def sums(d):
    return Path(d, "SHA256SUMS").read_bytes()


def make_upload(tmp_path, files=None, ctime_age=None):
    """A station upload directory with a correct SHA256SUMS."""
    d = tmp_path / "root" / "EV1" / "AC0G-B4" / "hf-timestd"
    files = files or {"a/x.bin": b"alpha", "b/y.bin": b"beta", "order.json": b"{}"}
    lines = []
    for rel, body in sorted(files.items()):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        lines.append("%s  %s\n" % (hashlib.sha256(body).hexdigest(), rel))
    (d / "SHA256SUMS").write_text("".join(lines))
    return d


def fake_now(d):
    """A clock 2 minutes after the SHA256SUMS' ctime."""
    c = os.stat(d / "SHA256SUMS").st_ctime
    return datetime.fromtimestamp(c + 120, timezone.utc)


def run(tmp_path, now):
    return es.verify(str(tmp_path / "root"), now=now)


def test_verify_writes_receipt(tmp_path):
    d = make_upload(tmp_path)
    run(tmp_path, fake_now(d))
    text = (d / "RECEIPT").read_text()
    import re
    assert re.fullmatch(RECEIPT_RE, text)
    assert text.count("\n") == 1
    assert text.startswith(hashlib.sha256(sums(d)).hexdigest() + "  verified ")
    assert not (d / "FAILED").exists()


def test_verify_flags_corruption(tmp_path):
    d = make_upload(tmp_path)
    p = d / "a" / "x.bin"
    b = bytearray(p.read_bytes())
    b[0] ^= 1
    p.write_bytes(bytes(b))
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text().splitlines() == ["a/x.bin"]
    assert not (d / "RECEIPT").exists()


def test_verify_skips_fresh(tmp_path):
    d = make_upload(tmp_path)
    c = os.stat(d / "SHA256SUMS").st_ctime
    run(tmp_path, datetime.fromtimestamp(c + 10, timezone.utc))
    assert not (d / "RECEIPT").exists() and not (d / "FAILED").exists()
    run(tmp_path, datetime.fromtimestamp(c + 61, timezone.utc))
    assert (d / "RECEIPT").exists()


def test_old_mtime_does_not_make_it_ready(tmp_path):
    """rsync -a keeps the station's mtimes; only ctime counts."""
    d = make_upload(tmp_path)
    old = utc(2020, 1, 1).timestamp()
    for p in d.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    c = os.stat(d / "SHA256SUMS").st_ctime
    run(tmp_path, datetime.fromtimestamp(c + 5, timezone.utc))
    assert not (d / "RECEIPT").exists()


def test_rsync_temp_file_skips(tmp_path):
    d = make_upload(tmp_path)
    (d / "a" / ".x.bin.AbC123").write_bytes(b"part")
    run(tmp_path, fake_now(d))
    assert not (d / "RECEIPT").exists() and not (d / "FAILED").exists()
    (d / "a" / ".x.bin.AbC123").unlink()
    run(tmp_path, fake_now(d))
    assert (d / "RECEIPT").exists()


def test_missing_listed_file_fails_naming_each(tmp_path):
    d = make_upload(tmp_path)
    (d / "a" / "x.bin").unlink()
    (d / "b" / "y.bin").write_bytes(b"BAD")
    run(tmp_path, fake_now(d))
    assert sorted((d / "FAILED").read_text().splitlines()) == ["a/x.bin", "b/y.bin"]
    assert not (d / "RECEIPT").exists()


def test_unlisted_file_is_ignored(tmp_path):
    d = make_upload(tmp_path)
    (d / "extra.txt").write_text("not listed")
    run(tmp_path, fake_now(d))
    assert (d / "RECEIPT").exists()


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "a/../../x"])
def test_hostile_path_fails(tmp_path, bad):
    d = make_upload(tmp_path)
    (tmp_path / "root" / "outside").write_bytes(b"z")
    with open(d / "SHA256SUMS", "a") as f:
        f.write("%s  %s\n" % (hashlib.sha256(b"z").hexdigest(), bad))
    run(tmp_path, fake_now(d))
    assert bad in (d / "FAILED").read_text().splitlines()
    assert not (d / "RECEIPT").exists()


def test_empty_or_garbled_sums_fails(tmp_path):
    d = make_upload(tmp_path)
    (d / "SHA256SUMS").write_text("this is not a sum line\n")
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").exists() and not (d / "RECEIPT").exists()


def test_existing_verdicts_are_not_reverified(tmp_path):
    d = make_upload(tmp_path)
    run(tmp_path, fake_now(d))
    first = (d / "RECEIPT").read_bytes()
    (d / "a" / "x.bin").write_bytes(b"changed after")
    run(tmp_path, fake_now(d))
    assert (d / "RECEIPT").read_bytes() == first and not (d / "FAILED").exists()
    # FAILED persists until a person removes it
    (d / "RECEIPT").unlink()
    (d / "FAILED").write_text("a/x.bin\n")
    run(tmp_path, fake_now(d))
    assert not (d / "RECEIPT").exists()
    (d / "FAILED").unlink()
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text().splitlines() == ["a/x.bin"]


def test_receipt_write_leaves_no_tmp(tmp_path):
    d = make_upload(tmp_path)
    run(tmp_path, fake_now(d))
    assert sorted(p.name for p in d.iterdir() if p.is_file()) == ["RECEIPT", "SHA256SUMS", "order.json"]


def test_orders_dir_not_scanned(tmp_path):
    (tmp_path / "root" / "orders").mkdir(parents=True)
    (tmp_path / "root" / "orders" / "orders.json").write_text("{}")
    assert es.verify(str(tmp_path / "root"), now=datetime.now(timezone.utc)) == 0


def test_second_pass_logs_nothing_new(tmp_path, caplog):
    d = make_upload(tmp_path)
    c = os.stat(d / "SHA256SUMS").st_ctime
    young = datetime.fromtimestamp(c + 10, timezone.utc)
    with caplog.at_level(logging.INFO, logger="events_server"):
        run(tmp_path, young)
        n = len(caplog.records)
        assert n >= 1                        # the waiting condition is logged once
        run(tmp_path, young + timedelta(seconds=30))
        run(tmp_path, young + timedelta(seconds=40))
        assert len(caplog.records) == n
        run(tmp_path, young + timedelta(seconds=100))   # now it changes: verified
        assert len(caplog.records) > n


def test_persistent_failure_logged_once(tmp_path, caplog):
    d = make_upload(tmp_path)
    (d / "a" / "x.bin").unlink()
    with caplog.at_level(logging.INFO, logger="events_server"):
        run(tmp_path, fake_now(d))
        n = len(caplog.records)
        run(tmp_path, fake_now(d))
        run(tmp_path, fake_now(d))
        assert len(caplog.records) == n


def test_orders_cli_writes_file(tmp_path):
    feed = tmp_path / "feed.json"
    flare = {"time_tag": "2026-11-06T18:00:00Z", "begin_time": "2026-11-06T17:50:00Z",
             "begin_class": "C9.0", "max_time": "2026-11-06T18:00:00Z",
             "max_class": "M1.2", "max_xrlong": 1.2e-5,
             "end_time": "2026-11-06T18:20:00Z", "end_class": "M1.0"}
    feed.write_text(json.dumps([flare]))
    out = tmp_path / "orders" / "orders.json"
    out.parent.mkdir()
    rc = es.main(["orders", "--stations", str(REPO / "scripts/events/stations.json"),
                  "--out", str(out), "--feed", str(feed)])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["version"] == 1 and len(doc["orders"]) == 1
    assert doc["orders"][0]["stations"] == ["AC0G-B4"]


def test_stations_json_is_b4_only():
    st = json.loads((REPO / "scripts/events/stations.json").read_text())
    assert [s["id"] for s in st] == ["AC0G-B4"]
    assert (st[0]["lat"], st[0]["lon"]) == (38.9187367, -92.1277335)
    assert st[0]["paths"] == [[40.6807, -105.0407], [21.9872, -159.7636]]


def test_units_match_rulings():
    d = REPO / "scripts/events/systemd"
    for n in ("events-orders", "events-verify"):
        s = (d / (n + ".service")).read_text()
        assert "User=wsprdaemon" in s and "Nice=19" in s and "IOSchedulingClass=idle" in s
    assert "OnCalendar=hourly" in (d / "events-orders.timer").read_text()
    assert "5min" in (d / "events-verify.timer").read_text()
    assert "scripts/events/stations.json" in (d / "events-orders.service").read_text()


def test_end_to_end_keeper_verifier_keeper(tmp_path):
    raw = tmp_path / "raw"
    for ch in CHANNELS:
        for i, m in enumerate((0, 10, 20)):
            make_file(raw, ch, ts(2026, 11, 6, 0, m), i)
    server = tmp_path / "server"
    (server / "orders").mkdir(parents=True)
    staging = tmp_path / "staging"
    cfg = {"station_id": STATION, "remote": "local", "remote_root": "", "ssh_key": "/k",
           "local_base": str(server), "staging": str(staging), "raw_root": str(raw),
           "settle_minutes": 15, "bwlimit_kib_s": 0}
    (server / "orders" / "orders.json").write_text(
        json.dumps({"version": 1, "orders": [order()]}))
    tr = LocalTransport(cfg)
    empty = lambda p: Usage(10**12, 0)
    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    assert json.loads((staging / "state.json").read_text()) == {EID: "uploaded"}
    d = server / EID / STATION / "hf-timestd"
    assert (d / "SHA256SUMS").exists() and not (d / "RECEIPT").exists()

    # the verifier: ctime is real (just now), so the injected clock moves 2 min on
    es.verify(str(server), now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert (d / "RECEIPT").exists() and not (d / "FAILED").exists()

    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    assert json.loads((staging / "state.json").read_text()) == {EID: "done"}
    assert not (staging / EID).exists()


def test_end_to_end_corruption_keeps_staging(tmp_path):
    raw = tmp_path / "raw"
    for ch in CHANNELS:
        for i, m in enumerate((0, 10, 20)):
            make_file(raw, ch, ts(2026, 11, 6, 0, m), i)
    server = tmp_path / "server"
    (server / "orders").mkdir(parents=True)
    staging = tmp_path / "staging"
    cfg = {"station_id": STATION, "remote": "local", "remote_root": "", "ssh_key": "/k",
           "local_base": str(server), "staging": str(staging), "raw_root": str(raw),
           "settle_minutes": 15, "bwlimit_kib_s": 0}
    (server / "orders" / "orders.json").write_text(
        json.dumps({"version": 1, "orders": [order()]}))
    tr = LocalTransport(cfg)
    empty = lambda p: Usage(10**12, 0)
    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    d = server / EID / STATION / "hf-timestd"
    victim = sorted(p for p in d.rglob("*") if p.is_file() and p.name != "SHA256SUMS")[0]
    victim.write_bytes(victim.read_bytes() + b"x")
    es.verify(str(server), now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert (d / "FAILED").exists()
    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    assert state(type("E", (), {"staging": staging})).get(EID, "").startswith("failed:")
    assert (staging / EID).is_dir()
