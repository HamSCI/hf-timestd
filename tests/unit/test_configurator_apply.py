"""`hf-timestd config apply` must change only what the payload names.

2026-11-16, KPH2: blanking [recorder] archive_root through apply also dropped
two empty tables ([timing.provenance], [timing.authority_manager.host_clock])
and left the file root:root 0644 instead of timestd's 0640."""
import io
import json
import os
import tomllib
from types import SimpleNamespace

from hf_timestd import configurator as C

ORIGINAL = """\
[recorder]
archive_root = "/mnt/archive/hf-timestd"
keep = 3

[timing.provenance]

[timing.authority_manager.host_clock]

[timing.fusion_metrics]
enabled = true
"""


def _apply(path, payload, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return C.cmd_config_apply(SimpleNamespace(config=str(path)))


def test_empty_tables_survive(tmp_path, monkeypatch):
    p = tmp_path / "timestd-config.toml"
    p.write_text(ORIGINAL)
    assert _apply(p, {"recorder": {"archive_root": ""}}, monkeypatch) == 0
    got = tomllib.loads(p.read_text())
    want = tomllib.loads(ORIGINAL)
    want["recorder"]["archive_root"] = ""
    assert got == want


def test_mode_kept(tmp_path, monkeypatch):
    p = tmp_path / "timestd-config.toml"
    p.write_text(ORIGINAL)
    os.chmod(p, 0o640)
    assert _apply(p, {"recorder": {"keep": 4}}, monkeypatch) == 0
    assert (os.stat(p).st_mode & 0o777) == 0o640


def test_owner_kept(tmp_path, monkeypatch):
    p = tmp_path / "timestd-config.toml"
    p.write_text(ORIGINAL)
    chowned = []
    monkeypatch.setattr(C.os, "chown", lambda path, uid, gid: chowned.append((uid, gid)))
    assert _apply(p, {"recorder": {"keep": 4}}, monkeypatch) == 0
    st = os.stat(p)
    assert chowned == [(st.st_uid, st.st_gid)]
