"""The keeper pass: cut, upload, wait for the RECEIPT, delete staging."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_window_drf import make_file, ts, utc
from hf_timestd.events import keeper
from hf_timestd.events.keeper import LocalTransport, run_once

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
    cfg = {"station_id": STATION, "remote": "local", "remote_root": str(remote),
           "staging": str(tmp_path / "staging"), "raw_root": str(raw),
           "settle_minutes": 15, "bwlimit_kbps": 0}
    return type("Env", (), dict(cfg=cfg, raw=raw, remote=remote,
                                staging=tmp_path / "staging"))


def put_orders(env, *orders):
    (env.remote / "orders" / "orders.json").write_text(
        json.dumps({"version": 1, "orders": list(orders)}))


def rdir(env, eid=EID):
    return env.remote / eid / STATION / "hf-timestd"


def state(env):
    return json.loads((env.staging / "state.json").read_text())


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
    assert all("--partial" in c and "-a" in c for c in pushes)
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
    (env.remote / "../victim").resolve()
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
    good = {"station_id": "S", "remote": "r", "remote_root": "/x", "ssh_key": "/k",
            "staging": str(tmp_path / "s"), "raw_root": str(tmp_path / "r")}
    keeper.validate_config(dict(good))
    for k in ("station_id", "remote_root", "staging", "raw_root"):
        bad = dict(good)
        del bad[k]
        with pytest.raises(keeper.ConfigError):
            keeper.validate_config(bad)
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
    cfg = {"remote": "u@h", "ssh_key": "/k", "bwlimit_kbps": 200}
    keeper.RsyncTransport(cfg).pull("/srv/o.json", "/tmp/o.json")
    cmd = seen[0]
    assert cmd[:3] == ["rsync", "-a", "--partial"]
    assert "--bwlimit=200" in cmd
    assert cmd[cmd.index("-e") + 1] == "ssh -i /k -o BatchMode=yes"
    assert "u@h:/srv/o.json" in cmd
    seen.clear()
    keeper.RsyncTransport(dict(cfg, bwlimit_kbps=0)).pull("/a", "/b")
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


def test_cli_transport_failure_exits_zero(env, tmp_path):
    from hf_timestd import cli
    raw, staging = env.cfg["raw_root"], env.cfg["staging"]
    conf = tmp_path / "events.toml"
    conf.write_text(
        'station_id = "AC0G-B4"\nremote = "nobody@127.0.0.1"\nremote_root = "/nonexistent"\n'
        f'ssh_key = "{tmp_path}/nokey"\nstaging = "{staging}"\nraw_root = "{raw}"\n')
    cli.main(["events", "run", "--config", str(conf)])     # SystemExit would fail the test
    assert not (Path(staging) / EID).exists()
