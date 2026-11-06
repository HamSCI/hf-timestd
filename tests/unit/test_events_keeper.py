"""The keeper pass: cut, upload, wait for the RECEIPT, delete staging."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_window_drf import make_file, ts, utc
from hf_timestd.events import keeper
from hf_timestd.events.keeper import LocalTransport
from hf_timestd.events.keeper import run_once as _run_once


class Usage:
    def __init__(self, total, used):
        self.total, self.used, self.free = total, used, total - used


def run_once(cfg, now, transport, disk_usage=lambda p: Usage(10**12, 0)):
    """The tests' default: an empty disk, whatever this machine's /tmp holds."""
    return _run_once(cfg, now, transport, disk_usage=disk_usage)

STATION = "AC0G-B4"
CHANNELS = ["WWV_20000", "WWV_10000"]
EID = "20261106T0010Z-flare-M1.0"
EID2 = "20261106T0100Z-flare-M2.0"
END = utc(2026, 11, 6, 0, 25)
READY = END + timedelta(minutes=20)       # settle_minutes = 15


def order(eid=EID, stations=(STATION,), start="2026-11-06T00:05:00Z",
          end="2026-11-06T00:25:00Z"):
    return {"event_id": eid, "name": "t", "kind": "flare", "ordered_by": "goes-trigger",
            "reason": "t", "start_utc": start, "end_utc": end, "stations": list(stations),
            "record": [{"client": "hf-timestd", "keep": "time-channels"}],
            "destination": "wd30", "created_utc": "2026-11-06T00:00:00Z", "source": {}}


@pytest.fixture
def env(tmp_path):
    raw = tmp_path / "raw"
    for ch in CHANNELS:
        for i, m in enumerate((0, 10, 20)):
            make_file(raw, ch, ts(2026, 11, 6, 0, m), i)
    remote = tmp_path / "remote"
    (remote / "orders").mkdir(parents=True)
    cfg = {"station_id": STATION, "remote": "local", "remote_root": "", "ssh_key": "/k", "local_base": str(remote),
           "staging": str(tmp_path / "staging"), "raw_root": str(raw),
           "settle_minutes": 15, "bwlimit_kib_s": 0}
    return type("Env", (), dict(cfg=cfg, raw=raw, remote=remote,
                                staging=tmp_path / "staging"))


def put_orders(env, *orders):
    (env.remote / "orders" / "orders.json").write_text(
        json.dumps({"version": 1, "orders": list(orders)}))


def rdir(env, eid=EID):
    return env.remote / eid / STATION / "hf-timestd"


def state(env):
    st = json.loads((env.staging / "state.json").read_text())
    st.pop("_conditions", None)           # the keeper's log memory, not an event
    return st


def sums_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def receipt(env, eid=EID, sha=None):
    sha = sha or sums_sha(env.staging / eid / "SHA256SUMS")
    (rdir(env, eid) / "RECEIPT").write_text(f"{sha}  verified 2026-11-06T01:00:00Z\n")


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


class FailingPush(LocalTransport):
    def push(self, local_dir, remote_dir):
        return False


class DeadTransport(LocalTransport):
    def pull(self, *a):
        return False

    def push(self, *a):
        return False

    def fetch_receipt(self, *a, **k):
        return False


def test_not_ready_before_settle(env):
    put_orders(env, order())
    lines = run_once(env.cfg, END + timedelta(minutes=14), LocalTransport(env.cfg))
    assert not (env.staging / EID).exists()
    assert not (env.staging / "state.json").exists() or state(env) == {}
    assert not any("staged" in l for l in lines)


def test_ready_window_ships(env):
    put_orders(env, order())
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert state(env) == {EID: "uploaded"}
    assert (env.staging / EID / "SHA256SUMS").exists()
    d = rdir(env)
    assert (d / "SHA256SUMS").read_bytes() == (env.staging / EID / "SHA256SUMS").read_bytes()
    assert (d / "order.json").exists()
    for ch in CHANNELS:
        assert (d / ch).is_dir()


def test_sums_pushed_last_in_separate_call(env, monkeypatch):
    put_orders(env, order())
    calls = []
    real = keeper.subprocess.run

    def spy(cmd, *a, **k):
        calls.append(list(cmd))
        return real(cmd, *a, **k)
    monkeypatch.setattr(keeper.subprocess, "run", spy)
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    pushes = [c for c in calls if str(env.staging / EID) in " ".join(c)]
    assert len(pushes) == 2
    assert all("-a" in c for c in pushes) and "--partial" in pushes[0]
    assert not any("SHA256SUMS" in a and "exclude" not in a for a in pushes[0]
                   if not a.startswith("--exclude"))
    assert pushes[1][-2].endswith("SHA256SUMS")


def test_receipt_deletes_staging(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    lines = run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert not (env.staging / EID).exists()
    assert state(env) == {EID: "done"}
    assert any("done" in l for l in lines)


def test_no_receipt_waits(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert (env.staging / EID / "SHA256SUMS").exists()
    assert state(env) == {EID: "uploaded"}


def test_bad_receipt_keeps_staging(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env, sha="0" * 64)
    lines = run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert (env.staging / EID / "SHA256SUMS").exists()
    assert state(env) == {EID: "failed:receipt-mismatch"}
    assert any("receipt-mismatch" in l for l in lines)


def test_server_failed_marker_keeps_staging(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    (rdir(env) / "FAILED").write_text("sha mismatch in WWV_20000\n")
    receipt(env)          # even a receipt does not override FAILED
    lines = run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert (env.staging / EID / "SHA256SUMS").exists()
    assert state(env) == {EID: "failed:server-verify"}
    assert any("server-verify" in l for l in lines)
    # no automatic retry: the next pass changes nothing
    (rdir(env) / "FAILED").unlink()
    run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert state(env) == {EID: "failed:server-verify"}
    assert (env.staging / EID).exists()


def test_upload_failure_keeps_staging(env):
    put_orders(env, order())
    run_once(env.cfg, READY, FailingPush(env.cfg))
    assert state(env) == {EID: "staged"}
    assert (env.staging / EID / "SHA256SUMS").exists()
    marker = env.staging / EID / "marker"
    marker.write_text("x")                       # survives only if not restaged
    run_once(env.cfg, READY + timedelta(minutes=5), LocalTransport(env.cfg))
    assert marker.exists()
    assert state(env) == {EID: "uploaded"}
    assert (rdir(env) / "SHA256SUMS").exists()


def test_transport_down_keeps_everything(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    lines = run_once(env.cfg, READY + timedelta(minutes=5), DeadTransport(env.cfg))
    assert state(env) == {EID: "uploaded"}
    assert (env.staging / EID).exists()


def test_orders_pull_failure_logged_no_staging(env):
    put_orders(env, order())
    lines = run_once(env.cfg, READY, DeadTransport(env.cfg))
    assert not (env.staging / EID).exists()
    assert any("orders" in l for l in lines)


def test_done_order_not_redone(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "done"}
    shutil_remote = snapshot(env.remote)
    run_once(env.cfg, READY + timedelta(minutes=10), t)
    run_once(env.cfg, READY + timedelta(minutes=15), t)
    assert not (env.staging / EID).exists()
    assert state(env) == {EID: "done"}
    assert snapshot(env.remote) == shutil_remote


def test_failed_order_not_redone(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env, sha="1" * 64)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    marker = env.staging / EID / "marker"
    marker.write_text("x")
    run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert marker.exists() and state(env) == {EID: "failed:receipt-mismatch"}


def test_other_station_ignored(env):
    put_orders(env, order(stations=("AC0G-ND",)))
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert not (env.staging / EID).exists()
    assert not (env.remote / EID).exists()


def test_partial_window_still_ships(env):
    (env.raw / CHANNELS[0] / "20261106" / f"{ts(2026, 11, 6, 0, 10)}.bin.zst").unlink()
    (env.raw / CHANNELS[0] / "20261106" / f"{ts(2026, 11, 6, 0, 10)}.json").unlink()
    put_orders(env, order())
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert state(env) == {EID: "uploaded"}
    meta = json.loads((rdir(env) / "order.json").read_text())
    assert meta["missing"][CHANNELS[0]] == [ts(2026, 11, 6, 0, 10)]
    assert meta["missing"][CHANNELS[1]] == []


def test_one_window_at_a_time(env):
    o2 = order(EID2, start="2026-11-06T00:05:00Z", end="2026-11-06T00:25:00Z")
    put_orders(env, order(), o2)
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    assert state(env) == {EID: "uploaded"}
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "uploaded"}
    assert not (env.staging / EID2).exists()
    # once the first is done, the second may follow
    receipt(env)
    run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert state(env)[EID] == "done"
    run_once(env.cfg, READY + timedelta(minutes=15), t)
    assert state(env)[EID2] == "uploaded"


def test_one_staged_per_run(env):
    o2 = order(EID2)
    put_orders(env, order(), o2)
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert len(state(env)) == 1


def test_channels_default_and_override(env):
    (env.raw / "NOTES").mkdir()          # not a channel with data, still a directory
    put_orders(env, order())
    env.cfg["channels"] = [CHANNELS[0]]
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    meta = json.loads((rdir(env) / "order.json").read_text())
    assert meta["channels"] == [CHANNELS[0]]


def test_default_channels_are_buffer_dirs(env):
    put_orders(env, order())
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    meta = json.loads((rdir(env) / "order.json").read_text())
    assert sorted(meta["channels"]) == sorted(CHANNELS)


def test_raw_buffer_untouched_by_full_cycle(env):
    before = snapshot(env.raw)
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "done"}
    assert not (env.staging / EID).exists()
    assert snapshot(env.raw) == before


def test_hostile_event_id_refused(env):
    victim = env.staging.parent / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    bad = order("../victim")
    put_orders(env, bad)
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    # even a state file naming it must not delete outside staging
    env.staging.mkdir(exist_ok=True)
    (env.staging / "state.json").write_text(json.dumps({"../victim": "uploaded"}))
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert (victim / "keep").exists()


def test_corrupt_state_stops_the_pass(env):
    put_orders(env, order())
    env.staging.mkdir(parents=True)
    (env.staging / "state.json").write_text("{not json")
    lines = run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert not (env.staging / EID).exists()
    assert (env.staging / "state.json").read_text() == "{not json"
    assert any("state.json" in l for l in lines)


def test_stale_partial_staging_is_cleaned(env):
    put_orders(env, order())
    (env.staging / EID).mkdir(parents=True)
    (env.staging / EID / "junk").write_text("x")      # crashed earlier cut, no state
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert not (env.staging / EID / "junk").exists()
    assert state(env) == {EID: "uploaded"}


def test_config_validation(tmp_path):
    good = {"station_id": "S", "remote": "r", "remote_root": "", "ssh_key": "/k",
            "staging": str(tmp_path / "s"), "raw_root": str(tmp_path / "r")}
    keeper.validate_config(dict(good))
    for k in ("station_id", "remote", "ssh_key", "remote_root", "staging", "raw_root"):
        bad = dict(good)
        del bad[k]
        with pytest.raises(keeper.ConfigError):
            keeper.validate_config(bad)
    for rr in ("/srv/wd_data/events", "/", "a/../b"):
        with pytest.raises(keeper.ConfigError):
            keeper.validate_config(dict(good, remote_root=rr))
    keeper.validate_config(dict(good, remote_root="events/sub"))
    nested = dict(good, staging=str(tmp_path / "r" / "stage"))
    with pytest.raises(keeper.ConfigError):
        keeper.validate_config(nested)
    nested = dict(good, raw_root=str(tmp_path / "s" / "raw"))
    with pytest.raises(keeper.ConfigError):
        keeper.validate_config(nested)


def test_rsync_transport_command_line(monkeypatch):
    seen = []

    class R:
        returncode = 0
    monkeypatch.setattr(keeper.subprocess, "run", lambda cmd, *a, **k: (seen.append(cmd), R())[1])
    cfg = {"remote": "u@h", "ssh_key": "/k", "bwlimit_kib_s": 200}
    keeper.RsyncTransport(cfg).pull("orders/o.json", "/tmp/o.json")
    cmd = seen[0]
    assert cmd[:3] == ["rsync", "-a", "--partial"]
    assert "--timeout=300" in cmd
    assert "--bwlimit=200" in cmd
    assert cmd[cmd.index("-e") + 1] == (
        "ssh -i /k -o BatchMode=yes -o ConnectTimeout=30 "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=4")
    assert "u@h:orders/o.json" in cmd
    seen.clear()
    keeper.RsyncTransport(dict(cfg, ssh_key="/a b/k")).pull("o", "/b")
    assert "ssh -i '/a b/k' -o" in seen[0][seen[0].index("-e") + 1]
    seen.clear()
    keeper.RsyncTransport(dict(cfg, bwlimit_kib_s=0)).pull("a", "/b")
    assert not any(a.startswith("--bwlimit") for a in seen[0])


def test_cli_exit_codes(tmp_path, capsys):
    from hf_timestd import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["events", "run", "--config", str(tmp_path / "nope.toml")])
    assert e.value.code == 2
    assert "events: config" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text('station_id = "x"\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["events", "run", "--config", str(bad)])
    assert e.value.code == 2


def test_cli_transport_failure_exits_zero(env, tmp_path, monkeypatch):
    from hf_timestd import cli
    monkeypatch.setattr(keeper, "RsyncTransport", DeadTransport)   # no real ssh
    raw, staging = env.cfg["raw_root"], env.cfg["staging"]
    conf = tmp_path / "events.toml"
    conf.write_text(
        'station_id = "AC0G-B4"\nremote = "nobody@host"\nremote_root = ""\n'
        f'ssh_key = "{tmp_path}/nokey"\nstaging = "{staging}"\nraw_root = "{raw}"\n')
    cli.main(["events", "run", "--config", str(conf)])     # SystemExit would fail the test
    assert not (Path(staging) / EID).exists()


@pytest.mark.parametrize("drop", ["remote", "ssh_key"])
def test_cli_missing_remote_or_key_exits_2(env, tmp_path, drop, capsys):
    from hf_timestd import cli
    kv = {"station_id": "AC0G-B4", "remote": "h", "remote_root": "", "ssh_key": "/k",
          "staging": env.cfg["staging"], "raw_root": env.cfg["raw_root"]}
    del kv[drop]
    conf = tmp_path / "events.toml"
    conf.write_text("".join('%s = "%s"\n' % kv_ for kv_ in kv.items()))
    with pytest.raises(SystemExit) as e:
        cli.main(["events", "run", "--config", str(conf)])
    assert e.value.code == 2
    assert drop in capsys.readouterr().err


def test_cli_absolute_remote_root_exits_2(env, tmp_path):
    from hf_timestd import cli
    conf = tmp_path / "events.toml"
    conf.write_text('station_id = "S"\nremote = "h"\nremote_root = "/srv/wd_data/events"\n'
                    'ssh_key = "/k"\nstaging = "%s"\nraw_root = "%s"\n'
                    % (env.cfg["staging"], env.cfg["raw_root"]))
    with pytest.raises(SystemExit) as e:
        cli.main(["events", "run", "--config", str(conf)])
    assert e.value.code == 2


class Recording(LocalTransport):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.paths = []

    def pull(self, remote_path, local_path):
        self.paths.append(remote_path)
        return super().pull(remote_path, local_path)

    def push(self, local_dir, remote_dir):
        self.paths.append(remote_dir)
        return super().push(local_dir, remote_dir)

    def fetch_receipt(self, remote_dir, local_path, name="RECEIPT"):
        self.paths.append(remote_dir)
        return super().fetch_receipt(remote_dir, local_path, name)


def test_remote_paths_are_relative_to_rrsync_root(env):
    put_orders(env, order())
    t = Recording(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "done"}
    assert t.paths and "orders/orders.json" in t.paths
    for p in t.paths:
        assert not p.startswith("/") and ".." not in p.split("/"), p
    assert "%s/%s/hf-timestd" % (EID, STATION) in t.paths
    # a configured sub-root joins in front, still relative
    env.cfg["remote_root"] = "sub/"
    assert keeper._remote_dir(env.cfg, EID) == "sub/%s/%s/hf-timestd" % (EID, STATION)
    assert keeper._rjoin(env.cfg, "orders", "orders.json") == "sub/orders/orders.json"


@pytest.mark.parametrize("body", [
    b"", b"\n", b"abc", b"\xff\xfe\x00bad",
    "{}  verified 2026-11-06T01:00:00Z\n",            # truncated, 63 chars (filled below)
    "{}  verified 2026-11-06T01:00:00Z\n",            # uppercase (filled below)
    "{}  verified\n",                                 # no timestamp
    "{} verified 2026-11-06T01:00:00Z\n",             # one space
    "{}  verified 2026-11-06T01:00:00Z\nextra\n",    # extra line
    "{}  checked 2026-11-06T01:00:00Z\n",             # wrong token
    "{}  verified 2026-11-06T01:00:00Z\n",            # another hash (filled below)
], ids=["empty", "newline", "short", "not-utf8", "prefix63", "upper", "no-ts",
        "one-space", "extra-line", "wrong-token", "other-hash"])
def test_malformed_receipt_keeps_staging(env, body, request):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    ours = sums_sha(env.staging / EID / "SHA256SUMS")
    rid = request.node.callspec.id
    if isinstance(body, str):
        h = {"prefix63": ours[:63], "upper": ours.upper(),
             "other-hash": "0" * 64}.get(rid, ours)
        body = body.format(h).encode()
    (rdir(env) / "RECEIPT").write_bytes(body)
    lines = run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "failed:receipt-mismatch"}
    assert (env.staging / EID / "SHA256SUMS").exists()
    assert any("receipt-mismatch" in l for l in lines)


def test_receipt_without_trailing_newline_accepted(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    h = sums_sha(env.staging / EID / "SHA256SUMS")
    (rdir(env) / "RECEIPT").write_text(f"{h}  verified 2026-11-06T01:00:00Z")
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert state(env) == {EID: "done"}


def test_failed_cut_gets_state_and_queue_moves_on(env, monkeypatch):
    o2 = order(EID2)
    put_orders(env, order(), o2)
    real = keeper.write_window_drf

    def boom(raw, chans, o, out):
        if o["event_id"] == EID:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / "partial").write_text("x")
            raise OSError("No space left on device")
        return real(raw, chans, o, out)
    monkeypatch.setattr(keeper, "write_window_drf", boom)
    t = LocalTransport(env.cfg)
    lines = run_once(env.cfg, READY, t)
    assert state(env) == {EID: "failed:cut:OSError", EID2: "uploaded"}
    assert not (env.staging / EID).exists()
    assert any("failed:cut:OSError" in l for l in lines)
    # never retried automatically
    monkeypatch.setattr(keeper, "write_window_drf", real)
    receipt(env, EID2)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert state(env)[EID] == "failed:cut:OSError"
    assert not (env.staging / EID).exists()


@pytest.mark.parametrize("bad", ["state.json", "orders.json", "ok\n"])
def test_reserved_and_newline_ids_refused(env, bad):
    put_orders(env, order(bad))
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert not (env.staging / "state.json").exists() or state(env) == {}
    assert keeper._event_dir(env.staging, bad) is None


def test_stations_must_be_a_list(env):
    o = order()
    o["stations"] = STATION           # a bare string would pass a substring test
    put_orders(env, o)
    lines = run_once(env.cfg, READY, LocalTransport(env.cfg))
    assert not (env.staging / EID).exists()
    assert any("stations is not a list" in l for l in lines)


def test_disk_guard_leaves_order_pending(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    # window 00:05-00:25 touches 3 files x 2 channels x 115.2 MB = 691.2 MB
    est = 3 * 2 * 115_200_000
    total = 10 * 10**9
    lines = run_once(env.cfg, READY, t, disk_usage=lambda p: Usage(total, int(0.78 * total) - est + 10**6))
    assert not (env.staging / EID).exists()
    assert not (env.staging / "state.json").exists() or state(env) == {}
    msg = [l for l in lines if "not staging" in l]
    assert msg and str(est) in msg[0] and str(total) in msg[0]
    # room again: the same order stages on a later pass
    run_once(env.cfg, READY + timedelta(minutes=5), t,
             disk_usage=lambda p: Usage(total, int(0.78 * total) - est - 10**6))
    assert state(env) == {EID: "uploaded"}


def test_deleting_state_finishes_next_pass(env, monkeypatch):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)

    class Crash(BaseException):
        pass

    def die(*a, **k):
        raise Crash()
    real = keeper._remove_staging
    monkeypatch.setattr(keeper, "_remove_staging", die)
    with pytest.raises(Crash):
        run_once(env.cfg, READY + timedelta(minutes=5), t)
    # the intent was written before any delete began
    assert state(env) == {EID: "deleting"}
    assert (env.staging / EID / "SHA256SUMS").exists()
    monkeypatch.setattr(keeper, "_remove_staging", real)
    lines = run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert state(env) == {EID: "done"}
    assert not (env.staging / EID).exists()
    assert any("interrupted delete" in l for l in lines)


def test_done_with_leftover_staging_is_swept(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY, t)
    receipt(env)
    run_once(env.cfg, READY + timedelta(minutes=5), t)
    (env.staging / EID).mkdir()
    (env.staging / EID / "left").write_text("x")
    lines = run_once(env.cfg, READY + timedelta(minutes=10), t)
    assert not (env.staging / EID).exists()
    assert state(env) == {EID: "done"}
    assert any("left behind" in l for l in lines)


def test_sums_push_has_no_partial(env, monkeypatch):
    put_orders(env, order())
    calls = []
    real = keeper.subprocess.run
    monkeypatch.setattr(keeper.subprocess, "run",
                        lambda cmd, *a, **k: (calls.append(list(cmd)), real(cmd, *a, **k))[1])
    run_once(env.cfg, READY, LocalTransport(env.cfg))
    pushes = [c for c in calls if str(env.staging / EID) in " ".join(c)]
    assert "--partial" in pushes[0] and "--partial" not in pushes[1]
    assert all("--timeout=300" in c for c in calls)


def _b4_cfg_run(env, used_pct):
    total = 251 * 10**9
    put_orders(env, order(), )
    cfg = dict(env.cfg)
    # 15 files x 6 channels x 115.2 MB = 10.368 GB; pin the estimate to B4's window
    est = 15 * 6 * keeper.BYTES_PER_FILE_CHANNEL
    keeper_slots = keeper._window_slots
    keeper._window_slots = lambda o: 15
    try:
        orig = keeper._channels
        keeper._channels = lambda c: ["c%d" % i for i in range(6)]
        try:
            lines = run_once(cfg, READY, LocalTransport(cfg),
                             disk_usage=lambda p: Usage(total, int(total * used_pct / 100)))
        finally:
            keeper._channels = orig
    finally:
        keeper._window_slots = keeper_slots
    return lines, est


def test_disk_guard_b4_numbers_stage_at_default(env, monkeypatch):
    # 72 % used + 10.4 GB on 251 GB is about 76.1 %: under the default 78
    monkeypatch.setattr(keeper, "write_window_drf",
                        lambda *a: {"channels": {}, "bytes": 0})
    lines, est = _b4_cfg_run(env, 72)
    assert abs(est - 10.368e9) < 1e6
    assert not any("not staging" in l for l in lines)
    assert state(env) == {EID: "staged"} or state(env) == {EID: "uploaded"}


def test_disk_guard_76_percent_refuses_at_default(env, monkeypatch):
    monkeypatch.setattr(keeper, "write_window_drf",
                        lambda *a: {"channels": {}, "bytes": 0})
    lines, _ = _b4_cfg_run(env, 76)
    assert any("not staging" in l and "limit 78 %" in l for l in lines)
    assert not (env.staging / "state.json").exists() or state(env) == {}


@pytest.mark.parametrize("bad", [80, 85, 0, -5, True, "78"])
def test_max_disk_used_pct_bounds(tmp_path, bad):
    good = {"station_id": "S", "remote": "r", "remote_root": "", "ssh_key": "/k",
            "staging": str(tmp_path / "s"), "raw_root": str(tmp_path / "r")}
    keeper.validate_config(dict(good, max_disk_used_pct=79.5))
    with pytest.raises(keeper.ConfigError):
        keeper.validate_config(dict(good, max_disk_used_pct=bad))


def test_cli_max_disk_80_exits_2(env, tmp_path, capsys):
    from hf_timestd import cli
    conf = tmp_path / "events.toml"
    conf.write_text('station_id = "S"\nremote = "h"\nremote_root = ""\nssh_key = "/k"\n'
                    'max_disk_used_pct = 80\nstaging = "%s"\nraw_root = "%s"\n'
                    % (env.cfg["staging"], env.cfg["raw_root"]))
    with pytest.raises(SystemExit) as e:
        cli.main(["events", "run", "--config", str(conf)])
    assert e.value.code == 2
    assert "max_disk_used_pct" in capsys.readouterr().err


def _count(lines, text):
    return sum(text in l for l in lines)


def test_disk_condition_logs_once_then_clears(env):
    put_orders(env, order())
    t = LocalTransport(env.cfg)
    full = lambda p: Usage(10**10, 10**10 - 1)
    first = run_once(env.cfg, READY, t, disk_usage=full)
    second = run_once(env.cfg, READY + timedelta(minutes=5), t, disk_usage=full)
    third = run_once(env.cfg, READY + timedelta(minutes=10), t, disk_usage=full)
    assert _count(first, "not staging") == 1
    assert _count(second + third, "not staging") == 0
    # room again: it stages, and the clearing is logged once
    lines = run_once(env.cfg, READY + timedelta(minutes=15), t)
    assert _count(lines, "condition cleared") == 1
    assert "_conditions" not in json.loads((env.staging / "state.json").read_text())


def test_awaiting_receipt_logs_once(env):
    put_orders(env, order(), order(EID2, start="2026-11-06T00:35:00Z", end="2026-11-06T00:55:00Z"))
    t = LocalTransport(env.cfg)
    later = READY + timedelta(minutes=40)
    first = run_once(env.cfg, later, t)
    second = run_once(env.cfg, later + timedelta(minutes=5), t)
    assert _count(first, "no receipt yet") == 0          # first pass uploads
    again = run_once(env.cfg, later + timedelta(minutes=10), t)
    assert _count(second + again, "no receipt yet") == 1
    assert _count(second + again, "awaiting its receipt") == 1


def test_transport_down_logs_once(env):
    put_orders(env, order())
    env.cfg["local_base"] = str(env.remote / "missing")
    t = LocalTransport(env.cfg)
    a = run_once(env.cfg, READY, t)
    b = run_once(env.cfg, READY + timedelta(minutes=5), t)
    assert _count(a, "could not pull") == 1 and _count(b, "could not pull") == 0


def test_deleting_blocks_a_new_window(env, monkeypatch):
    put_orders(env, order(), order(EID2, start="2026-11-06T00:35:00Z", end="2026-11-06T00:55:00Z"))
    t = LocalTransport(env.cfg)
    run_once(env.cfg, READY + timedelta(minutes=40), t)
    receipt(env, EID)
    monkeypatch.setattr(keeper, "_remove_staging",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    lines = run_once(env.cfg, READY + timedelta(minutes=45), t)
    assert state(env) == {EID: "deleting"}
    assert not (env.staging / EID2).exists()
    assert _count(lines, "awaiting its receipt") == 1
