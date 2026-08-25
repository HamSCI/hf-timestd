"""web-api resolves its config the same way the CLI does.

`config.py` hard-coded a repo-relative default and built its singleton at
import time, so importing it raised FileNotFoundError on any machine that
had not created `config/timestd-config.toml` inside the checkout — which
is every clean checkout, since the repo ships only the `.template`.  The
deployed web-api reads production config from /etc anyway, and `cli.py`
already honours $TIMESTD_CONFIG then /etc/hf-timestd/.  Same order here.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

WEB_API = Path(__file__).resolve().parents[2] / "web-api"


def _fresh_config_module(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v) if v is not None else monkeypatch.delenv(k, raising=False)
    if str(WEB_API) not in sys.path:
        sys.path.insert(0, str(WEB_API))
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_env_var_wins(tmp_path, monkeypatch):
    cfg = tmp_path / "custom.toml"
    cfg.write_text('[storage]\nsqlite_path = "/tmp/x.db"\n')
    mod = _fresh_config_module(monkeypatch, TIMESTD_CONFIG=str(cfg))
    assert mod.config.config_path == cfg
    assert mod.config.config["storage"]["sqlite_path"] == "/tmp/x.db"


def test_resolution_order_is_documented_and_env_first(tmp_path, monkeypatch):
    cfg = tmp_path / "a.toml"
    cfg.write_text("[storage]\n")
    mod = _fresh_config_module(monkeypatch, TIMESTD_CONFIG=str(cfg))
    order = [str(p) for p in mod.candidate_config_paths()]
    assert order[0] == str(cfg)
    assert any("/etc/hf-timestd/timestd-config.toml" == p for p in order), order


def test_importing_without_any_config_does_not_explode(monkeypatch):
    """A clean checkout with no station config must still import."""
    monkeypatch.setenv("TIMESTD_CONFIG", "/nonexistent/nope.toml")
    mod = _fresh_config_module(monkeypatch)
    assert mod.config.config == {} or mod.config.config is not None
