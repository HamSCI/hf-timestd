"""hf-timestd hf-chain-delay: report on stdout, JSON on request, exit 2 without a pilot."""
import json

import pytest

import hf_timestd.cli as cli
from hf_timestd.core.hf_chain_delay_probe import HfChainDelayProbe

from test_hf_chain_delay_probe import FakeControl, make_config, make_probe
from synth_origins import ND_K0


def toml_for(cfg):
    t6 = cfg["timing"]["t6_pps"]
    grp = cfg["recorder"]["channel_group"]["timestd"]
    lines = ["[timing.t6_pps]", f"enabled = {str(t6['enabled']).lower()}",
             f"frequency_hz = {t6['frequency_hz']}", "sample_rate = 96000",
             "low_edge_hz = -25000", "high_edge_hz = 25000",
             "[recorder.channel_group.timestd]", 'preset = "iq"', "sample_rate = 24000"]
    for ch in grp["channels"]:
        lines += ["[[recorder.channel_group.timestd.channels]]",
                  f"frequency_hz = {ch['frequency_hz']}",
                  f'description = "{ch["description"]}"']
    return "\n".join(lines) + "\n"


@pytest.fixture
def fake_radiod(monkeypatch):
    def build(config, args):
        specs = HfChainDelayProbe.plan_channels(config)
        return make_probe(FakeControl(specs, k0s={s.ssrc: ND_K0[i % 5] for i, s in enumerate(specs)}))
    monkeypatch.setattr(cli, "_build_hf_chain_delay_probe", build)


def test_cli_prints_the_report_and_writes_json(fake_radiod, tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(toml_for(make_config()))
    out = tmp_path / "out.json"
    cli.main(["hf-chain-delay", "--duration-s", "5", "--config", str(cfg),
              "--output", str(out)])
    text = capsys.readouterr().out
    assert "T6_REF" in text and "WWV_25000" in text and "learned T6 chain delay" in text
    assert json.loads(out.read_text())["rows"]


def test_cli_json_flag_prints_json(fake_radiod, tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(toml_for(make_config()))
    cli.main(["hf-chain-delay", "--duration-s", "5", "--config", str(cfg), "--json"])
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 7


def test_cli_reads_the_learned_chain_delay_without_touching_the_record(
        fake_radiod, tmp_path, capsys):
    rec = tmp_path / "cd.json"
    rec.write_text(json.dumps({
        "schema": 1, "identity": {"x": 1.0}, "offset_ns": -16_350_000.0,
        "sigma_ns": 1e6, "spread_ns": 1e6, "n": 5, "n_eff": 5.0, "span_s": 1.0,
        "learned_from_utc": "a", "learned_to_utc": "b", "lower_tier": "T4",
        "state": "confirmed", "history": []}))
    before = rec.read_bytes()
    cfg = tmp_path / "c.toml"
    cfg.write_text(toml_for(make_config())
                   + f'[timing.offset_judge]\nchain_delay_path = "{rec}"\n')
    cli.main(["hf-chain-delay", "--duration-s", "5", "--config", str(cfg)])
    assert "learned T6 chain delay: -16.3500 ms" in capsys.readouterr().out
    assert rec.read_bytes() == before


def test_cli_exits_2_without_a_pilot(fake_radiod, tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(toml_for(make_config(enabled=False, pilot=0)))
    with pytest.raises(SystemExit) as exc:
        cli.main(["hf-chain-delay", "--config", str(cfg)])
    assert exc.value.code == 2
    assert "no TS-1 pilot" in capsys.readouterr().err


def test_build_probe_uses_the_hfchain_client_id_and_resolved_status(monkeypatch):
    import ka9q
    seen = {}

    class RecControl:
        def __init__(self, status, client_id=None, **kw):
            seen.update(status=status, client_id=client_id)

    monkeypatch.setattr(ka9q, "RadiodControl", RecControl)
    probe = cli._build_hf_chain_delay_probe({"ka9q": {"status": "b4-status.local"}}, None)
    assert seen == {"status": "b4-status.local", "client_id": "hf-timestd-hfchain"}
    assert isinstance(probe, HfChainDelayProbe)
