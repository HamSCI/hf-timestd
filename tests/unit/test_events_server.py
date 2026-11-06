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
    d = tmp_path / "incoming" / "AC0G-B4" / "EV1" / "AC0G-B4" / "hf-timestd"
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
    return es.verify(str(tmp_path / "incoming"), now=now)


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


@pytest.mark.parametrize("kind", ["absolute", "dotdot", "dotdot-inner"])
def test_hostile_path_fails(tmp_path, kind):
    """The file exists and its hash is right, so only the path check can refuse it."""
    d = make_upload(tmp_path)
    outside = d.parent / "outside"
    outside.write_bytes(b"z")
    bad = {"absolute": str(outside), "dotdot": "../outside",
           "dotdot-inner": "a/../../outside"}[kind]
    with open(d / "SHA256SUMS", "a") as f:
        f.write("%s  %s\n" % (hashlib.sha256(b"z").hexdigest(), bad))
    run(tmp_path, fake_now(d))
    assert bad in (d / "FAILED").read_text().splitlines()
    assert not (d / "RECEIPT").exists()


def test_garbled_sums_fails_without_echoing_text(tmp_path):
    d = make_upload(tmp_path)
    (d / "SHA256SUMS").write_text("this is not a sum line\n")
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text() == "line 1: unparseable\n"
    assert not (d / "RECEIPT").exists()


@pytest.mark.parametrize("body", ["", "\n\n"])
def test_empty_list_fails(tmp_path, body):
    d = make_upload(tmp_path)
    (d / "SHA256SUMS").write_text(body)
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text() == "SHA256SUMS lists no files\n"
    assert not (d / "RECEIPT").exists()


def test_corruption_of_each_listed_file(tmp_path):
    files = {"a/x.bin": b"alpha", "b/y.bin": b"beta", "order.json": b"{}"}
    for victim in sorted(files):
        d = make_upload(tmp_path / victim.replace("/", "_"), files)
        (d / victim).write_bytes(files[victim] + b"!")
        run(tmp_path / victim.replace("/", "_"), fake_now(d))
        assert (d / "FAILED").read_text().splitlines() == [victim], victim
        assert not (d / "RECEIPT").exists()


def test_out_of_tree_symlink_fails(tmp_path):
    d = make_upload(tmp_path)
    secret = tmp_path / "secret"
    secret.write_bytes(b"alpha")                      # same bytes as a/x.bin
    (d / "a" / "x.bin").unlink()
    os.symlink(secret, d / "a" / "x.bin")
    run(tmp_path, fake_now(d))
    assert "symlink: a/x.bin" in (d / "FAILED").read_text()
    assert not (d / "RECEIPT").exists()


def test_symlinked_directory_fails(tmp_path):
    d = make_upload(tmp_path)
    os.symlink(tmp_path, d / "link")
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text() == "symlink: link\n"


def test_symlinked_sums_fails(tmp_path):
    d = make_upload(tmp_path)
    real = tmp_path / "real-sums"
    real.write_bytes(sums(d))
    (d / "SHA256SUMS").unlink()
    os.symlink(real, d / "SHA256SUMS")
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").exists() and not (d / "RECEIPT").exists()


def test_sums_a_directory_fails_and_pass_continues(tmp_path):
    d = make_upload(tmp_path)
    (d / "SHA256SUMS").unlink()
    (d / "SHA256SUMS").mkdir()
    other = tmp_path / "incoming" / "AC0G-B4" / "EV2" / "AC0G-B4" / "hf-timestd"
    other.mkdir(parents=True)
    (other / "f").write_bytes(b"q")
    (other / "SHA256SUMS").write_text("%s  f\n" % hashlib.sha256(b"q").hexdigest())
    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    es.verify(str(tmp_path / "incoming"), now=now)
    assert (d / "FAILED").read_text() == "SHA256SUMS is not a regular file\n"
    assert (other / "RECEIPT").exists()


def test_sums_over_1mib_fails(tmp_path):
    d = make_upload(tmp_path)
    (d / "SHA256SUMS").write_bytes(sums(d) + b"#" * (1 << 20))
    run(tmp_path, fake_now(d))
    assert (d / "FAILED").read_text() == "SHA256SUMS is over 1 MiB\n"


def test_symlinked_event_dir_not_walked(tmp_path):
    real = tmp_path / "elsewhere"
    (real / "AC0G-B4" / "hf-timestd").mkdir(parents=True)
    (real / "AC0G-B4" / "hf-timestd" / "f").write_bytes(b"q")
    (real / "AC0G-B4" / "hf-timestd" / "SHA256SUMS").write_text(
        "%s  f\n" % hashlib.sha256(b"q").hexdigest())
    inc = tmp_path / "incoming" / "AC0G-B4"
    inc.mkdir(parents=True)
    os.symlink(real, inc / "EVX")
    es.verify(str(tmp_path / "incoming"), now=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert not (real / "AC0G-B4" / "hf-timestd" / "RECEIPT").exists()


def test_verdict_write_error_is_contained(tmp_path, monkeypatch, caplog):
    d = make_upload(tmp_path)
    real = es._atomic_write

    def flaky(path, text, *a, **k):
        if os.path.basename(path) in ("RECEIPT", "FAILED"):
            raise OSError("disk")
        return real(path, text, *a, **k)
    monkeypatch.setattr(es, "_atomic_write", flaky)
    with caplog.at_level(logging.INFO, logger="events_server"):
        run(tmp_path, fake_now(d))
        n = len(caplog.records)
        run(tmp_path, fake_now(d))
        assert len(caplog.records) == n            # logged once
    assert not (d / "RECEIPT").exists()


def test_orders_split_per_station(tmp_path):
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps([{
        "time_tag": "2026-11-06T18:00:00Z", "begin_time": "2026-11-06T17:50:00Z",
        "begin_class": "C9.0", "max_time": "2026-11-06T18:00:00Z", "max_class": "M1.2",
        "max_xrlong": 1.2e-5, "end_time": "2026-11-06T18:20:00Z", "end_class": "M1.0"}]))
    st = tmp_path / "st.json"
    b4 = json.loads((REPO / "scripts/events/stations.json").read_text())[0]
    other = dict(b4, id="XX0X-OTHER", lat=-38.0, lon=92.0)   # night on the paths
    st.write_text(json.dumps([b4, other]))
    inc = tmp_path / "incoming"
    for sid in ("AC0G-B4", "XX0X-OTHER"):
        (inc / sid).mkdir(parents=True)
    out = tmp_path / "orders.json"
    assert es.main(["orders", "--stations", str(st), "--out", str(out),
                    "--feed", str(feed)]) == 0
    full = json.loads(out.read_text())["orders"]
    mine = json.loads((inc / "AC0G-B4" / "orders" / "orders.json").read_text())
    assert mine["orders"] == [o for o in full if "AC0G-B4" in o["stations"]] and mine["orders"]
    theirs = json.loads((inc / "XX0X-OTHER" / "orders" / "orders.json").read_text())
    assert all("AC0G-B4" not in o["stations"] for o in theirs["orders"])


def test_orders_refuse_planted_symlink(tmp_path):
    inc = tmp_path / "incoming" / "AC0G-B4"
    inc.mkdir(parents=True)
    target = tmp_path / "victim"
    target.mkdir()
    os.symlink(target, inc / "orders")
    es._write_station_orders(str(tmp_path / "incoming"), "AC0G-B4", [])
    assert list(target.iterdir()) == []


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
    o = tmp_path / "incoming" / "AC0G-B4" / "orders"
    o.mkdir(parents=True)
    (o / "orders.json").write_text("{}")
    assert es.verify(str(tmp_path / "incoming"), now=datetime.now(timezone.utc)) == 0


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
    o = (d / "events-orders.service").read_text()
    assert "/home/wsprdaemon/hamsci-events/stations.json" in o and "/srv/wd_data/events/bin" not in o
    assert "After=network-online.target" in o and "Wants=network-online.target" in o
    assert "/srv/wd_data/events/bin" not in (d / "events-verify.service").read_text()


def test_end_to_end_keeper_verifier_keeper(tmp_path):
    raw = tmp_path / "raw"
    for ch in CHANNELS:
        for i, m in enumerate((0, 10, 20)):
            make_file(raw, ch, ts(2026, 11, 6, 0, m), i)
    server = tmp_path / "incoming" / STATION
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
    es.verify(str(server.parent), now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert (d / "RECEIPT").exists() and not (d / "FAILED").exists()

    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    assert json.loads((staging / "state.json").read_text()) == {EID: "done"}
    assert not (staging / EID).exists()


def test_end_to_end_corruption_keeps_staging(tmp_path):
    raw = tmp_path / "raw"
    for ch in CHANNELS:
        for i, m in enumerate((0, 10, 20)):
            make_file(raw, ch, ts(2026, 11, 6, 0, m), i)
    server = tmp_path / "incoming" / STATION
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
    es.verify(str(server.parent), now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert (d / "FAILED").exists()
    keeper.run_once(cfg, READY, tr, disk_usage=empty)
    assert state(type("E", (), {"staging": staging})).get(EID, "").startswith("failed:")
    assert (staging / EID).is_dir()
