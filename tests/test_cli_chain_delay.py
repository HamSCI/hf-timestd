"""hf-timestd chain-delay: show, seed, accept, relearn (spec 2026-10-27 §7).

Each test points --config at a temp TOML whose chain_delay_path lies in
tmp_path, so nothing needs root and nothing touches /var/lib.
"""
import json

import pytest

from hf_timestd.cli import main
from hf_timestd.core.chain_delay_store import ChainDelayRecord, ChainDelayStore

SEEDED = dict(offset_ns=-16_350_000.0, sigma_ns=1_100_000.0,
              spread_ns=1_100_000.0, n=783, n_eff=100.0, span_s=49_000.0,
              learned_from_utc="x", learned_to_utc="y", lower_tier="T4",
              state="seeded")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[timing.offset_judge]\n"
        f'chain_delay_path = "{tmp_path / "cd.json"}"\n'
        f'publish_path = "{tmp_path / "offset_judge.json"}"\n'
    )
    return str(path)


def run(argv, cfg):
    return main(["chain-delay", *argv, "--config", cfg])


def request_file(tmp_path):
    return tmp_path / "cd.json.request.json"


def test_seed_writes_a_request_in_ns(tmp_path, cfg, capsys):
    run(["seed", "--offset-ms", "-16.35", "--sigma-ms", "1.1",
         "--spread-ms", "1.1"], cfg)
    req = json.loads(request_file(tmp_path).read_text())
    assert req == {"action": "seed", "offset_ns": -16_350_000.0,
                   "sigma_ns": pytest.approx(1_100_000.0),
                   "spread_ns": pytest.approx(1_100_000.0)}
    assert "the judge acts on its next tick" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [["--sigma-ms", "0", "--spread-ms", "1"],
                                 ["--sigma-ms", "1", "--spread-ms", "-1"]])
def test_seed_refuses_a_nonpositive_sigma_or_spread(tmp_path, cfg, capsys, bad):
    with pytest.raises(SystemExit) as exc:
        run(["seed", "--offset-ms", "-16", *bad], cfg)
    assert exc.value.code != 0
    assert "must be > 0" in capsys.readouterr().err
    assert not request_file(tmp_path).exists()


@pytest.mark.parametrize("action", ["accept", "relearn"])
def test_accept_and_relearn_write_their_request(tmp_path, cfg, capsys, action):
    run([action], cfg)
    assert json.loads(request_file(tmp_path).read_text()) == {"action": action}
    out = capsys.readouterr().out
    assert action in out and "the judge acts on its next tick" in out


def test_the_judge_takes_what_the_cli_wrote(tmp_path, cfg):
    run(["seed", "--offset-ms", "-16.35", "--sigma-ms", "1.1",
         "--spread-ms", "1.1"], cfg)
    store = ChainDelayStore(tmp_path / "cd.json", identity={})
    assert store.take_request()["offset_ns"] == -16_350_000.0


def test_show_json_prints_a_seeded_record(tmp_path, cfg, capsys):
    store = ChainDelayStore(tmp_path / "cd.json", identity={})
    store.set_record(ChainDelayRecord(**SEEDED))
    (tmp_path / "offset_judge.json").write_text(
        json.dumps({"chain_delay": {"state": "seeded"}}))
    run(["show", "--json"], cfg)
    out = json.loads(capsys.readouterr().out)
    assert out["record"]["state"] == "seeded"
    assert out["judge"] == {"state": "seeded"}


def test_show_without_files_says_so_and_exits_zero(cfg, capsys):
    run(["show"], cfg)
    assert "no record" in capsys.readouterr().out
