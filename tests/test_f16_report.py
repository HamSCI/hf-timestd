"""hf-timestd archive f16-report: the B4/ND trial verdict."""
import json
from datetime import datetime, timezone, timedelta

import pytest

from hf_timestd import cli


def _today():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _write(root, channel, day, start, err=-60.0, dphi=0.2, seconds=60,
           dtype="complex-f16", fallback=None, check=True):
    d = root / "raw_buffer" / channel / day
    d.mkdir(parents=True, exist_ok=True)
    meta = {"dtype": dtype}
    if dtype == "complex-f16":
        meta["scale"] = 4096.0
    if fallback:
        meta["f16_fallback"] = fallback
    if check:
        meta["f16_check"] = {"err_over_floor_db_max": err,
                             "carrier_dphi_rms_mrad": dphi, "seconds": seconds}
    (d / f"{start}.json").write_text(json.dumps(meta))


def _run(root, capsys, *extra):
    rc = cli.f16_report_main(["--data-root", str(root), *extra])
    return rc, capsys.readouterr().out


def test_report_pass(tmp_path, capsys):
    for i in range(3):
        _write(tmp_path, "WWV_10000", _today(), 1000 + i, err=-55.0 + i, dphi=0.3)
    _write(tmp_path, "WWV_10000", _today(), 2000, dtype="complex64",
           fallback="overflow", check=False)
    rc, out = _run(tmp_path, capsys, "--json")
    rep = json.loads(out)
    ch = rep["channels"]["WWV_10000"]
    assert rc == 0 and rep["verdict"] == "PASS"
    assert ch["f16_chunks"] == 3 and ch["fallbacks"] == 1
    assert ch["not_measured"] == 0
    assert ch["worst_err_over_floor_db"] == -53.0
    assert ch["worst_carrier_dphi_rms_mrad"] == 0.3
    assert ch["verdict"] == "PASS"


def test_report_fail_on_one_bad_chunk(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1, err=-60.0, dphi=0.2)
    _write(tmp_path, "WWV_10000", _today(), 2, err=-60.0, dphi=1.5)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1 and "FAIL" in out


def test_report_fail_on_error_over_floor(tmp_path, capsys):
    _write(tmp_path, "WWV_5000", _today(), 1, err=-39.0, dphi=0.2)
    rc, _ = _run(tmp_path, capsys)
    assert rc == 1


def test_report_no_f16_exits_2(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1, dtype="complex64", check=False)
    rc, out = _run(tmp_path, capsys)
    assert rc == 2
    rc, out = _run(tmp_path / "empty", capsys, "--json")
    assert rc == 2 and json.loads(out)["verdict"] == "NO F16 CHUNKS"


def test_null_chunks_are_not_measured_never_pass_evidence(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1, err=-60.0, dphi=0.2)
    _write(tmp_path, "WWV_10000", _today(), 2, err=None, dphi=None)
    _write(tmp_path, "WWV_10000", _today(), 3, err=-60.0, dphi=None)
    rc, out = _run(tmp_path, capsys, "--json")
    ch = json.loads(out)["channels"]["WWV_10000"]
    assert rc == 0 and ch["f16_chunks"] == 3 and ch["not_measured"] == 2
    assert ch["verdict"] == "PASS"


def test_all_null_channel_is_not_measured_and_fails_overall(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1, err=-60.0, dphi=0.2)
    _write(tmp_path, "WWV_15000", _today(), 1, err=None, dphi=None)
    _write(tmp_path, "WWV_20000", _today(), 1, check=False)  # no f16_check at all
    rc, out = _run(tmp_path, capsys, "--json")
    rep = json.loads(out)
    assert rc == 1 and rep["verdict"] == "FAIL"
    assert rep["channels"]["WWV_15000"]["verdict"] == "NOT MEASURED"
    assert rep["channels"]["WWV_20000"]["verdict"] == "NOT MEASURED"
    assert rep["channels"]["WWV_15000"]["worst_err_over_floor_db"] is None
    rc, out = _run(tmp_path, capsys)
    assert "NOT MEASURED" in out


def test_null_metric_does_not_hide_a_bad_other_metric(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1, err=-20.0, dphi=None)
    rc, out = _run(tmp_path, capsys, "--json")
    assert rc == 1
    assert json.loads(out)["channels"]["WWV_10000"]["verdict"] == "FAIL"


def test_days_window(tmp_path, capsys):
    old = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y%m%d")
    _write(tmp_path, "WWV_10000", old, 1, err=-10.0, dphi=9.0)  # bad but old
    _write(tmp_path, "WWV_10000", _today(), 1)
    rc, out = _run(tmp_path, capsys, "--days", "3", "--json")
    assert rc == 0 and json.loads(out)["channels"]["WWV_10000"]["f16_chunks"] == 1
    rc, _ = _run(tmp_path, capsys, "--days", "7")
    assert rc == 1


def test_unreadable_sidecar_skipped(tmp_path, capsys):
    _write(tmp_path, "WWV_10000", _today(), 1)
    (tmp_path / "raw_buffer" / "WWV_10000" / _today() / "9.json").write_text("{nope")
    rc, out = _run(tmp_path, capsys, "--json")
    assert rc == 0 and json.loads(out)["channels"]["WWV_10000"]["f16_chunks"] == 1
