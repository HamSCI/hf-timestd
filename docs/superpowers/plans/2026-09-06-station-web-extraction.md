# station-web Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move hf-timestd's `web-api/` into a new contract-conformant sigmond client, station-web, that serves data reports and documentation from products under `/var/lib/timestd` and touches nothing else on the host.

**Architecture:** One FastAPI process reads data products through `hamsci_dsp.io`, serves 13 static pages plus JSON routes, and exposes the contract's unix control socket. Routers that needed live host state (journalctl, chronyc, process probes, the running pipeline, outbound HTTP) are cut or rebuilt from products. hamsci-dsp gains the Allan-deviation module; hf-timestd loses the web UI and its FastAPI dependencies; sigmond gains a catalog entry and a dasi2 profile member.

**Tech Stack:** Python 3.11 (uv-managed CPython), FastAPI, uvicorn, pydantic, numpy, scipy, systemd-python, hamsci-dsp, hamsci-physics, SQLite (read-only), pytest + fastapi TestClient (httpx).

**Spec:** `hf-timestd/docs/superpowers/specs/2026-09-06-station-web-extraction-design.md` (commit 86d89e2). Read it first; every task below argues from it.

## Global Constraints

- Repos live under `/home/mjh/hamsci/repos/` on the devbox: `station-web` (new), `hamsci-dsp`, `hamsci-physics`, `hf-timestd`, `sigmond`. Production checkouts live at `/opt/git/sigmond/<name>`.
- Develop on `main`, no feature branches (project convention). Commit after every task. Do not push until the task says so.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH
  ```
- `station_web` never imports `hf_timestd`, never imports `subprocess`, never imports `requests`, `urllib.request`, `httpx`, or `aiohttp`, never writes under `/var/lib/timestd`. Tests enforce all four.
- The data root `/var/lib/timestd` is frozen. SQLite lives at `/var/lib/timestd/phase2/timestd.db` in WAL mode.
- Contract version `0.8`. Client name `station-web`, package `station_web`, unit `station-web@.service`, log-level env `STATION_WEB_LOG_LEVEL`, config `/etc/station-web/station-web.toml`, config env override `STATION_WEB_CONFIG`.
- Default HTTP bind `0.0.0.0`, port `8000`. No CORS middleware.
- Vendored assets: `plotly-2.27.0.min.js` and a pinned `marked` release, both under `static/vendor/` with sha256 in `static/vendor/VENDOR.md`.
- Catalog: `kind = "client"`, `start_priority = 220`, `requires = ["hf-timestd", "hamsci-physics", "hamsci-dsp"]`.
- Station work (Tasks 17–18) runs by Michael's hand: stage a script at `~/hf-deploy-<date>-station-web.sh`, he runs it via `!`. Announce on the claude-bus before any station change.
- Run tests from a repo root with its own venv: `uv run pytest tests/ -q --override-ini addopts=` (the `--override-ini` keeps the summary line visible; the repos set `-q` in addopts).

---

## File Structure

**hamsci-dsp** (Task 1)
- Create `src/hamsci_dsp/stability.py` — ADEV functions moved verbatim from hf-timestd.
- Create `tests/test_stability.py` — moved from hf-timestd `tests/unit/test_stability_analysis.py`.
- Modify `pyproject.toml` — version `0.6.1` → `0.7.0`.

**station-web** (Tasks 2–14), root `/home/mjh/hamsci/repos/station-web/`
- `pyproject.toml`, `deploy.toml`, `README.md`, `.gitignore`
- `config/station-web.toml.template`
- `scripts/setup-station.sh`, `scripts/config-review.sh`
- `systemd/station-web@.service`, `systemd/station-web.service`
- `src/station_web/__init__.py` — `__version__`
- `src/station_web/version.py` — `GIT_INFO`
- `src/station_web/config.py` — `Settings`, module singleton `config`
- `src/station_web/contract.py` — `build_inventory`, `build_validate`, `collect_issues`
- `src/station_web/cli.py` — `main`, verbs `version|inventory|validate|serve`
- `src/station_web/control_socket.py` — `ControlSocketServer`
- `src/station_web/app.py` — `create_app(settings) -> FastAPI`
- `src/station_web/broadcast.py` — moved from hf-timestd `models/broadcast.py`
- `src/station_web/models/{__init__,health,station,timing}.py` — moved
- `src/station_web/routers/*.py` — 15 kept routers (see Task 7 list)
- `src/station_web/services/*.py` — kept services, plus new `freshness.py`, `channels.py`, `space_weather_cache.py`
- `src/station_web/static/` — 13 pages, `css/`, `js/`, `vendor/`
- `tests/conftest.py` — fixture data root + TestClient
- `tests/test_client_contract.py`, `tests/test_deploy_contract.py`, `tests/test_config.py`, `tests/test_control_socket.py`, `tests/test_hygiene.py`, `tests/test_routes.py`, `tests/test_chrony_from_product.py`, `tests/test_space_weather_cache.py`, `tests/test_freshness.py`, `tests/test_docs_roots.py`, `tests/test_static_assets.py`

**sigmond** (Task 15)
- Modify `etc/catalog.toml` — `[client.station-web]`, dasi2 `clients` list.
- Modify `tests/test_catalog.py` — assert the entry and the profile membership.

**hf-timestd** (Task 16)
- Delete `web-api/`, `systemd/timestd-web-api.service`, `scripts/deploy_web_ui.sh`, `src/hf_timestd/models/broadcast.py`, `src/hf_timestd/core/stability_analysis.py`, `tests/unit/test_stability_analysis.py`, `tests/unit/test_web_api_config_resolution.py`.
- Modify `deploy.toml`, `pyproject.toml`, `src/hf_timestd/service_profile.py`, `tests/unit/test_service_profile.py`, `tests/unit/test_tid_l3_writer.py`, `CLAUDE.md`, `docs/INDEX.md`.

---

## Part A — hamsci-dsp

### Task 1: Move the Allan-deviation module into hamsci-dsp

**Files:**
- Create: `hamsci-dsp/src/hamsci_dsp/stability.py`
- Create: `hamsci-dsp/tests/test_stability.py`
- Modify: `hamsci-dsp/pyproject.toml` (version line 7)

**Interfaces:**
- Produces: `hamsci_dsp.stability.compute_phase_adev`, `compute_frequency_adev`, `identify_noise_type(taus, adev) -> str`, `compute_stability_at_tau`, `compute_stability_metrics` — identical signatures to `hf_timestd.core.stability_analysis` (lines 21, 99, 172, 219, 259 of that file). Task 7 imports them.

- [ ] **Step 1: Copy the module and its tests**

```bash
cd /home/mjh/hamsci/repos
cp hf-timestd/src/hf_timestd/core/stability_analysis.py hamsci-dsp/src/hamsci_dsp/stability.py
cp hf-timestd/tests/unit/test_stability_analysis.py hamsci-dsp/tests/test_stability.py
sed -i 's/from hf_timestd\.core\.stability_analysis import/from hamsci_dsp.stability import/' hamsci-dsp/tests/test_stability.py
grep -n "hf_timestd" hamsci-dsp/src/hamsci_dsp/stability.py hamsci-dsp/tests/test_stability.py
```
Expected: the grep prints nothing. If the module docstring names hf-timestd, edit the sentence to "Moved from hf-timestd on 2026-09-06 (station-web extraction, Phase 5)." and leave the code untouched.

- [ ] **Step 2: Run the moved tests**

Run: `cd hamsci-dsp && uv run pytest tests/test_stability.py -q --override-ini addopts=`
Expected: every test passes (the file passed in hf-timestd; only the import changed).

- [ ] **Step 3: Bump the version and run the whole dsp suite**

```bash
sed -i 's/^version = "0.6.1"/version = "0.7.0"/' pyproject.toml
uv run pytest tests/ -q --override-ini addopts=
```
Expected: the suite's existing pass count plus the new file, zero failures. `tests/test_import_lint.py` must still pass: the new module imports only numpy.

- [ ] **Step 4: Commit**

```bash
git add src/hamsci_dsp/stability.py tests/test_stability.py pyproject.toml
git commit -m "stability: adopt the Allan-deviation module from hf-timestd (0.7.0)

Phase 5 of the split: station-web needs ADEV without importing hf_timestd,
and no dsp module covered it. Moved verbatim with its tests.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

## Part B — station-web

### Task 2: Repository scaffold, packaging, and config

**Files:**
- Create: `station-web/pyproject.toml`, `.gitignore`, `src/station_web/__init__.py`, `src/station_web/version.py`, `src/station_web/config.py`, `config/station-web.toml.template`
- Test: `station-web/tests/test_config.py`

**Interfaces:**
- Produces: `station_web.config.Settings` with attributes `config_path: Path`, `data_root: Path`, `storage: dict`, `station: dict`, `station_metadata: dict`, `web_bind: str`, `web_port: int`, `docs_roots: list[dict]`, `log_level: str`, `fusion_dir: Path`, `loaded: bool`; function `load_settings(path: Path | None = None) -> Settings`; function `resolve_config_path() -> Path`; module singleton `config = load_settings()`. Every later task reads these names.
- Produces: `station_web.version.GIT_INFO: dict`.

- [ ] **Step 1: Initialise the repo and packaging**

```bash
mkdir -p /home/mjh/hamsci/repos/station-web && cd /home/mjh/hamsci/repos/station-web
git init -b main
mkdir -p src/station_web config scripts systemd tests docs
cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
venv/
uv.lock
.pytest_cache/
*.egg-info/
EOF
cat > pyproject.toml <<'EOF'
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "station-web"
version = "0.1.0"
description = "Station data reports and documentation: metrology, ionospheric science, GNSS vTEC, read from sigmond data products"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
authors = [{ name = "Michael James Hauan AC0G", email = "ac0g@hauan.org" }]
dependencies = [
    "fastapi>=0.104.0",
    "uvicorn[standard]>=0.24.0",
    "pydantic>=2.0",
    "numpy>=1.24.0",
    "scipy>=1.10.0",
    "hamsci-dsp>=0.7.0",      # readers, registry, station catalog, stability (editable sibling)
    "hamsci-physics>=0.1.0",  # solar_zenith (editable sibling)
    "systemd-python>=235",    # Type=notify + WatchdogSec
    "toml>=0.10.0",
]

[project.optional-dependencies]
dev = ["pytest>=7.0.0", "httpx>=0.25.0"]

[project.scripts]
station-web = "station_web.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
station_web = ["static/**/*"]

[tool.pytest.ini_options]
minversion = "6.0"
addopts = "-ra -q"
testpaths = ["tests"]

[tool.uv.sources]
hamsci-dsp = { path = "../hamsci-dsp", editable = true }
hamsci-physics = { path = "../hamsci-physics", editable = true }
EOF
printf '"""station-web: data reports and documentation for a sigmond station."""\n__version__ = "0.1.0"\n' > src/station_web/__init__.py
cp ../hamsci-physics/src/hamsci_physics/version.py src/station_web/version.py
```

- [ ] **Step 2: Write the config template**

```bash
cat > config/station-web.toml.template <<'EOF'
# station-web configuration.  Rendered by `smd config init station-web`
# (scripts/setup-station.sh) from the sigmond §14 env bag; edit with
# `smd config edit station-web`.

[station]
callsign = ""
grid_square = ""
latitude = 0.0
longitude = 0.0
description = ""

[data]
# Frozen by the 2026-08-24 split: hf-timestd owns this root; station-web reads it.
root = "/var/lib/timestd"

[storage]
# Mirrors hf-timestd's [storage].  Empty = the hamsci_dsp default
# (/var/lib/timestd/phase2/timestd.db).
sqlite_path = ""

[web]
bind = "0.0.0.0"
port = 8000

[docs]
roots = [
  { name = "hf-timestd",     path = "/opt/git/sigmond/hf-timestd/docs" },
  { name = "hamsci-physics", path = "/opt/git/sigmond/hamsci-physics/docs" },
  { name = "hamsci-dsp",     path = "/opt/git/sigmond/hamsci-dsp/docs" },
  { name = "sigmond",        path = "/opt/git/sigmond/sigmond/docs/scientist" },
]

[logging]
level = "INFO"
EOF
```

- [ ] **Step 3: Write the failing config tests**

`tests/test_config.py`:
```python
"""Config resolution: STATION_WEB_CONFIG > /etc/station-web > absent."""
from pathlib import Path

import pytest

from station_web import config as cfgmod


def test_env_override_wins(tmp_path, monkeypatch):
    p = tmp_path / "sw.toml"
    p.write_text('[station]\ncallsign = "AC0G"\n[web]\nport = 8123\n')
    monkeypatch.setenv("STATION_WEB_CONFIG", str(p))
    s = cfgmod.load_settings()
    assert s.loaded is True
    assert s.config_path == p
    assert s.station["callsign"] == "AC0G"
    assert s.web_port == 8123
    assert s.web_bind == "0.0.0.0"          # default survives a partial file


def test_missing_config_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("STATION_WEB_CONFIG", str(tmp_path / "absent.toml"))
    s = cfgmod.load_settings()
    assert s.loaded is False
    assert s.data_root == Path("/var/lib/timestd")
    assert s.storage == {}
    assert s.fusion_dir == Path("/var/lib/timestd/phase2/fusion")


def test_storage_and_docs_roots(tmp_path, monkeypatch):
    p = tmp_path / "sw.toml"
    p.write_text(
        '[data]\nroot = "%s"\n[storage]\nsqlite_path = "%s"\n'
        '[docs]\nroots = [{ name = "a", path = "%s" }]\n'
        % (tmp_path, tmp_path / "db.sqlite", tmp_path)
    )
    monkeypatch.setenv("STATION_WEB_CONFIG", str(p))
    s = cfgmod.load_settings()
    assert s.data_root == tmp_path
    assert s.storage == {"sqlite_path": str(tmp_path / "db.sqlite")}
    assert s.docs_roots == [{"name": "a", "path": str(tmp_path)}]


def test_empty_sqlite_path_yields_empty_storage(tmp_path, monkeypatch):
    p = tmp_path / "sw.toml"
    p.write_text('[storage]\nsqlite_path = ""\n')
    monkeypatch.setenv("STATION_WEB_CONFIG", str(p))
    assert cfgmod.load_settings().storage == {}


def test_station_metadata_keys(tmp_path, monkeypatch):
    p = tmp_path / "sw.toml"
    p.write_text('[station]\ncallsign = "AC0G"\ngrid_square = "EM38ww"\n'
                 'latitude = 38.9\nlongitude = -92.3\n')
    monkeypatch.setenv("STATION_WEB_CONFIG", str(p))
    md = cfgmod.load_settings().station_metadata
    assert md["callsign"] == "AC0G"
    assert md["grid_square"] == "EM38ww"
    assert md["latitude"] == 38.9
    assert set(md) >= {"callsign", "grid_square", "latitude", "longitude", "description"}
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv sync --extra dev && uv run pytest tests/test_config.py -q --override-ini addopts=`
Expected: FAIL with `ModuleNotFoundError: No module named 'station_web.config'`.

- [ ] **Step 5: Write config.py**

`src/station_web/config.py`:
```python
"""station-web settings: one TOML file, never fatal when absent.

Resolution order mirrors hamsci-physics: $STATION_WEB_CONFIG, then
/etc/station-web/station-web.toml.  A missing file yields defaults and
``loaded = False``; ``validate`` reports it, the app still serves the
pages so an operator can read the documentation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:            # pragma: no cover  (3.10)
    import tomli as tomllib            # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/station-web/station-web.toml")
DEFAULT_DATA_ROOT = Path("/var/lib/timestd")
DEFAULT_DOCS_ROOTS = [
    {"name": "hf-timestd",     "path": "/opt/git/sigmond/hf-timestd/docs"},
    {"name": "hamsci-physics", "path": "/opt/git/sigmond/hamsci-physics/docs"},
    {"name": "hamsci-dsp",     "path": "/opt/git/sigmond/hamsci-dsp/docs"},
    {"name": "sigmond",        "path": "/opt/git/sigmond/sigmond/docs/scientist"},
]


def resolve_config_path() -> Path:
    env = os.environ.get("STATION_WEB_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


@dataclass
class Settings:
    config_path: Path
    loaded: bool
    raw: dict = field(default_factory=dict)

    # ── derived views the routers read ─────────────────────────────
    @property
    def station(self) -> dict:
        return dict(self.raw.get("station", {}) or {})

    @property
    def station_metadata(self) -> dict:
        s = self.station
        return {
            "callsign": s.get("callsign", ""),
            "grid_square": s.get("grid_square", ""),
            "latitude": float(s.get("latitude", 0.0) or 0.0),
            "longitude": float(s.get("longitude", 0.0) or 0.0),
            "description": s.get("description", ""),
        }

    @property
    def data_root(self) -> Path:
        return Path((self.raw.get("data", {}) or {}).get("root", DEFAULT_DATA_ROOT))

    @property
    def fusion_dir(self) -> Path:
        return self.data_root / "phase2" / "fusion"

    @property
    def storage(self) -> dict:
        st = dict(self.raw.get("storage", {}) or {})
        if not st.get("sqlite_path"):
            st.pop("sqlite_path", None)
        return st

    @property
    def web_bind(self) -> str:
        return str((self.raw.get("web", {}) or {}).get("bind", "0.0.0.0"))

    @property
    def web_port(self) -> int:
        return int((self.raw.get("web", {}) or {}).get("port", 8000))

    @property
    def docs_roots(self) -> list[dict[str, Any]]:
        roots = (self.raw.get("docs", {}) or {}).get("roots")
        return [dict(r) for r in roots] if roots else [dict(r) for r in DEFAULT_DOCS_ROOTS]

    @property
    def log_level(self) -> str:
        return str((self.raw.get("logging", {}) or {}).get("level", "INFO"))


def load_settings(path: Path | None = None) -> Settings:
    p = Path(path) if path is not None else resolve_config_path()
    if not p.exists():
        logger.warning("station-web: no config at %s — serving with defaults", p)
        return Settings(config_path=p, loaded=False, raw={})
    with open(p, "rb") as fh:
        return Settings(config_path=p, loaded=True, raw=tomllib.load(fh))


#: Module singleton the moved routers import as ``from station_web.config import config``.
config = load_settings()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q --override-ini addopts=`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "scaffold: packaging, settings, config template

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 3: Contract surface — version, inventory, validate

**Files:**
- Create: `src/station_web/contract.py`, `src/station_web/cli.py`
- Test: `tests/test_client_contract.py`

**Interfaces:**
- Consumes: `station_web.config.load_settings`, `Settings`; `station_web.version.GIT_INFO`.
- Produces: `contract.CONTRACT_VERSION = "0.8"`, `UPSTREAM_CLIENT = "hf-timestd"`, `DEPLOY_TOML_PATH`, `build_inventory(settings) -> dict`, `build_validate(settings) -> dict`, `collect_issues(settings) -> list[dict]`; `cli.main()`, `cli.build_parser()`. Task 6 adds the `serve` verb to the same parser; Task 5 adds a control-socket issue check.

- [ ] **Step 1: Write the failing tests**

`tests/test_client_contract.py`:
```python
"""CLIENT-CONTRACT v0.8 conformance for station-web (meta-client, §16.3.1)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")


def run(*argv, env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "station_web.cli", *argv],
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.fixture(scope="module")
def inventory(tmp_path_factory):
    d = tmp_path_factory.mktemp("cfg")
    (d / "phase2").mkdir()
    cfg = d / "sw.toml"
    cfg.write_text(f'[station]\ncallsign = "AC0G"\ngrid_square = "EM38ww"\n'
                   f'[data]\nroot = "{d}"\n')
    proc = run("inventory", "--json", "-c", str(cfg))
    assert proc.returncode == 0, proc.stderr[-500:]
    return json.loads(proc.stdout)


@pytest.mark.parametrize("sub", ["inventory", "validate", "version"])
def test_stdout_is_only_json(sub):
    proc = run(sub, "--json")
    json.loads(proc.stdout)
    assert proc.stdout.lstrip()[0] in "{["


def test_inventory_exits_zero_even_with_no_config(tmp_path):
    proc = run("inventory", "--json", "-c", str(tmp_path / "absent.toml"))
    assert proc.returncode == 0, proc.stderr[-400:]
    doc = json.loads(proc.stdout)
    assert any("no configuration" in i["message"] for i in doc["issues"])


def test_top_level_keys(inventory):
    assert inventory["client"] == "station-web"
    assert inventory["version"]
    assert inventory["contract_version"] == "0.8"
    assert Path(inventory["config_path"]).is_absolute()
    assert inventory["instances"] and inventory["instances"][0]["instance"] == "default"
    assert "log_level" in inventory
    assert "log_paths" not in inventory          # §10: no file logs


def test_contract_version_matches_the_deploy_manifest(inventory):
    import tomllib
    with open(ROOT / "deploy.toml", "rb") as fh:
        deploy = tomllib.load(fh)
    assert inventory["contract_version"] == deploy["package"]["contract_version"]


def test_data_path_declares_a_meta_client(inventory):
    dp = inventory["instances"][0]["data_path"]
    assert dp["kind"] == "file"
    assert dp["details"]["upstream_client"] == "hf-timestd"
    assert dp["details"]["upstream_unit"] == "timestd-fusion.service"
    assert dp["details"]["also_reads"] == ["hamsci-physics", "gnss-vtec"]


def test_radiod_fields_are_absent(inventory):
    inst = inventory["instances"][0]
    for forbidden in ("radiod_id", "data_destination", "chain_delay_ns_applied"):
        assert forbidden not in inst
    assert inst["timing_authority_applied"] is None


def test_serves_block_names_the_http_listener(inventory):
    assert inventory["instances"][0]["serves"] == {"http": {"bind": "0.0.0.0", "port": 8000}}


def test_produces_nothing(inventory):
    inst = inventory["instances"][0]
    assert inst["data_sinks"] == []
    assert inst["provides_timing_calibration"] is False
    assert inst["uses_timing_calibration"] is False


def test_deploy_toml_path(inventory):
    assert inventory["instances"][0]["deploy_toml_path"] == "/opt/git/sigmond/station-web/deploy.toml"


def test_validate_fails_on_missing_data_root(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[station]\ncallsign = "AC0G"\ngrid_square = "EM38ww"\n'
                   f'[data]\nroot = "{tmp_path / "nowhere"}"\n')
    proc = run("validate", "--json", "-c", str(cfg))
    doc = json.loads(proc.stdout)
    assert doc["ok"] is False and proc.returncode == 1
    assert Path(doc["config_path"]).is_absolute()
    assert any("data root" in i["message"] for i in doc["issues"] if i["severity"] == "fail")
    for issue in doc["issues"]:
        assert set(issue) >= {"severity", "instance", "message"}


def test_validate_warns_on_missing_docs_root(tmp_path):
    (tmp_path / "phase2").mkdir()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[station]\ncallsign = "AC0G"\ngrid_square = "EM38ww"\n'
                   f'[data]\nroot = "{tmp_path}"\n'
                   f'[docs]\nroots = [{{ name = "gone", path = "{tmp_path / "gone"}" }}]\n')
    doc = json.loads(run("validate", "--json", "-c", str(cfg)).stdout)
    warns = [i for i in doc["issues"] if i["severity"] == "warn"]
    assert any("docs root" in i["message"] and "gone" in i["message"] for i in warns)


def test_main_guard_present():
    """§12.1 entry-point reachability."""
    text = (ROOT / "src/station_web/cli.py").read_text()
    assert 'if __name__ == "__main__":' in text or "if __name__ == '__main__':" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client_contract.py -q --override-ini addopts=`
Expected: FAIL, `No module named station_web.cli`.

- [ ] **Step 3: Write contract.py**

`src/station_web/contract.py`:
```python
"""CLIENT-CONTRACT v0.8 surface for station-web, a §16.3.1 meta-client.

station-web produces nothing.  Its data plane is the products hf-timestd,
hamsci-physics and (when present) gnss-vtec write under /var/lib/timestd.
The contract's ``upstream_client`` is singular, so hf-timestd, which owns
the data root, takes the slot; ``also_reads`` is an extension key that
names the others.  ``serves`` is another extension key: the contract has
no field for a TCP listener, and station-web is the first conformant
client that binds one (spec §7 proposes it for the next contract bump).
Radiod-side fields are omitted (§16.5).
"""
from __future__ import annotations

import logging
import os
import stat
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any

from station_web.config import Settings
from station_web.version import GIT_INFO

CONTRACT_VERSION = "0.8"
UPSTREAM_CLIENT = "hf-timestd"
ALSO_READS = ["hamsci-physics", "gnss-vtec"]
DEPLOY_TOML_PATH = "/opt/git/sigmond/station-web/deploy.toml"
VENDOR_DIR = Path(__file__).resolve().parent / "static" / "vendor"
CONTROL_SOCKET = "/run/station-web/control.sock"


def _version() -> str:
    try:
        return pkg_version("station-web")
    except PackageNotFoundError:
        from station_web import __version__
        return __version__


def build_inventory(settings: Settings) -> dict:
    """``inventory --json``.  MUST exit 0 on degraded paths: every lookup
    tolerates a missing config and reports through ``issues``."""
    root = settings.data_root
    instance: dict[str, Any] = {
        "instance": "default",
        "host": "localhost",
        "data_path": {
            "kind": "file",
            "path": str(root),
            "details": {
                "upstream_client": UPSTREAM_CLIENT,
                "upstream_unit": "timestd-fusion.service",
                "products": str(root / "phase2"),
                "also_reads": list(ALSO_READS),
                "description": ("L1-L3 data products, DIAG_chrony_stats and the "
                                "space-weather cache, read in place"),
            },
        },
        "serves": {"http": {"bind": settings.web_bind, "port": settings.web_port}},
        "control_socket": CONTROL_SOCKET,
        "data_sinks": [],
        "uses_timing_calibration": False,
        "provides_timing_calibration": False,
        "timing_authority_applied": None,
        "deploy_toml_path": DEPLOY_TOML_PATH,
        "station": {
            "callsign": settings.station.get("callsign", ""),
            "grid_square": settings.station.get("grid_square", ""),
        },
    }
    payload: dict[str, Any] = {
        "client": "station-web",
        "version": _version(),
        "contract_version": CONTRACT_VERSION,
        "config_path": str(settings.config_path),
    }
    if GIT_INFO:
        payload["git"] = GIT_INFO
    payload["log_level"] = logging.getLevelName(logging.getLogger().getEffectiveLevel())
    payload["instances"] = [instance]
    payload["deps"] = {
        "git": [
            {"name": "hamsci-dsp", "note": "readers, registry, station catalog, stability"},
            {"name": "hamsci-physics", "note": "solar geometry"},
            {"name": UPSTREAM_CLIENT, "note": "writes the products this client renders"},
        ],
        "pypi": [
            {"name": "fastapi", "version": ">=0.104.0"},
            {"name": "hamsci-dsp", "version": ">=0.7.0"},
        ],
    }
    payload["issues"] = collect_issues(settings)
    return payload


def build_validate(settings: Settings) -> dict:
    issues = collect_issues(settings)
    return {
        "ok": not any(i["severity"] == "fail" for i in issues),
        "config_path": str(settings.config_path),        # §12.3
        "issues": issues,
    }


def _issue(severity: str, message: str, instance: str = "default") -> dict:
    return {"severity": severity, "instance": instance, "message": message}


def collect_issues(settings: Settings) -> list[dict]:
    issues: list[dict] = []
    if not settings.loaded:
        issues.append(_issue("fail", f"no configuration loaded from {settings.config_path} "
                                     f"— run `smd config init station-web`", "all"))
        return issues

    for key in ("callsign", "grid_square"):
        if not settings.station.get(key):
            issues.append(_issue("warn", f"[station] {key} is empty"))

    root = settings.data_root
    if not root.is_dir():
        issues.append(_issue("fail", f"data root {root} does not exist"))
        return issues

    phase2 = root / "phase2"
    if not phase2.is_dir():
        issues.append(_issue("warn", f"{phase2} missing — hf-timestd has not written yet"))
    else:
        db = Path(settings.storage.get("sqlite_path", phase2 / "timestd.db"))
        if db.exists():
            mode = db.parent.stat().st_mode
            if not mode & stat.S_IWGRP:
                issues.append(_issue("fail", f"{db.parent} is not group-writable; a WAL "
                                             f"reader in another uid cannot open {db.name}"))
            if not os.access(db, os.R_OK):
                issues.append(_issue("fail", f"{db} is not readable by this user"))
        else:
            issues.append(_issue("warn", f"{db} not present yet"))

    for r in settings.docs_roots:
        if not Path(r["path"]).is_dir():
            issues.append(_issue("warn", f"docs root {r['name']} at {r['path']} does not exist"))

    for mod in ("hamsci_dsp", "hamsci_physics"):
        try:
            __import__(mod)
        except Exception as exc:        # noqa: BLE001
            issues.append(_issue("fail", f"cannot import {mod}: {exc}"))

    if not (VENDOR_DIR / "plotly-2.27.0.min.js").exists():
        issues.append(_issue("fail", f"vendored plotly missing from {VENDOR_DIR}"))
    return issues
```

- [ ] **Step 4: Write cli.py**

`src/station_web/cli.py`:
```python
#!/usr/bin/env python3
"""station-web command line.

    version    | inventory | validate     — CLIENT-CONTRACT §3 (all take --json)
    serve                                   — run the web application (the unit's ExecStart)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from station_web.config import load_settings

LOG_LEVEL_ENV = "STATION_WEB_LOG_LEVEL"          # §11
GENERIC_LOG_LEVEL_ENV = "CLIENT_LOG_LEVEL"


def resolve_log_level(flag: str | None, settings) -> str:
    """§11 precedence: --log-level, STATION_WEB_LOG_LEVEL, CLIENT_LOG_LEVEL,
    [logging] level, INFO."""
    for candidate in (flag, os.environ.get(LOG_LEVEL_ENV),
                      os.environ.get(GENERIC_LOG_LEVEL_ENV), settings.log_level, "INFO"):
        if candidate:
            return candidate.upper()
    return "INFO"


def _settings(args):
    return load_settings(Path(args.config) if getattr(args, "config", None) else None)


def _handle_version(args) -> int:
    from station_web.contract import _version
    info = {"name": "station-web", "version": _version(), "python": sys.version.split()[0]}
    print(json.dumps(info, indent=2) if args.json else f"station-web {info['version']}")
    return 0


def _handle_inventory(args) -> int:
    from station_web.contract import build_inventory
    print(json.dumps(build_inventory(_settings(args)), indent=2))
    return 0


def _handle_validate(args) -> int:
    from station_web.contract import build_validate
    payload = build_validate(_settings(args))
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def _handle_serve(args) -> int:
    from station_web.serve import run_server           # Task 6
    return run_server(_settings(args), instance=args.instance,
                      log_level=resolve_log_level(args.log_level, _settings(args)))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="station-web")
    p.add_argument("-c", "--config", help="config path (default $STATION_WEB_CONFIG or /etc/station-web/station-web.toml)")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("version", "inventory", "validate"):
        sp = sub.add_parser(name)
        sp.add_argument("--json", action="store_true")
    sv = sub.add_parser("serve")
    sv.add_argument("--instance", default="default")
    return p


def main() -> None:
    args = build_parser().parse_args()
    # §3: only JSON on stdout.  Every logger goes to stderr before any verb runs.
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(resolve_log_level(args.log_level, _settings(args)))
    handlers = {"version": _handle_version, "inventory": _handle_inventory,
                "validate": _handle_validate, "serve": _handle_serve}
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
```

Also create `src/station_web/static/vendor/.gitkeep` so the vendor check has a directory to look in (Task 13 fills it). The `test_validate_*` tests only assert on data-root and docs-root issues, so the missing-plotly `fail` does not break them.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_client_contract.py -q --override-ini addopts=`
Expected: 12 passed except `test_contract_version_matches_the_deploy_manifest`, which fails on a missing `deploy.toml` until Task 4. Confirm that is the only failure.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "contract: version/inventory/validate as a §16.3.1 meta-client

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 4: Deploy manifest, systemd units, config scripts

**Files:**
- Create: `deploy.toml`, `systemd/station-web@.service`, `systemd/station-web.service`, `scripts/setup-station.sh`, `scripts/config-review.sh`
- Test: `tests/test_deploy_contract.py`

**Interfaces:**
- Consumes: `station-web` console script (Task 2), `serve --instance %i` (Task 3 parser, Task 6 implementation).
- Produces: units named `station-web@.service` and `station-web.service`; `[contract.config] init/edit` paths sigmond calls.

- [ ] **Step 1: Write the failing manifest tests**

`tests/test_deploy_contract.py`:
```python
"""deploy.toml invariants (CLIENT-CONTRACT §5) and unit hygiene (§4)."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def deploy():
    with open(ROOT / "deploy.toml", "rb") as fh:
        return tomllib.load(fh)


def test_package_identity(deploy):
    assert deploy["package"]["name"] == "station-web"
    assert deploy["package"]["contract_version"] == "0.8"


def test_only_link_steps():
    """sigmond executes only kind = "link" today (installer.py:532)."""
    with open(ROOT / "deploy.toml", "rb") as fh:
        d = tomllib.load(fh)
    for step in d["install"]["steps"]:
        assert step.get("kind") == "link", step


def test_build_produces_the_console_script(deploy):
    assert deploy["build"]["produces"] == ["/opt/git/sigmond/station-web/venv/bin/station-web"]
    steps = deploy["build"]["steps"]
    assert "cpython-3.11-linux-x86_64-gnu" in steps[0]           # minor-series symlink
    assert any("hamsci-dsp" in s for s in steps) and any("hamsci-physics" in s for s in steps)
    assert steps[-1].endswith("pip install -e .")


def test_units_declared_and_shipped(deploy):
    shipped = {p.name for p in (ROOT / "systemd").glob("*.service")}
    assert set(deploy["systemd"]["templated_units"]) == {"station-web@.service"}
    assert set(deploy["systemd"]["units"]) == {"station-web.service"}
    for u in deploy["systemd"]["units"] + deploy["systemd"]["templated_units"]:
        assert u in shipped


def test_templated_unit_sources_both_env_files():
    text = (ROOT / "systemd/station-web@.service").read_text()
    assert "EnvironmentFile=-/etc/sigmond/coordination.env" in text
    assert "EnvironmentFile=-/etc/station-web/env/%i.env" in text
    assert "ExecReload=/bin/kill -HUP $MAINPID" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/var/lib/timestd/phase2" in text
    assert "User=stationweb" in text and "Group=timestd" in text


def test_config_scripts_exist_and_are_executable(deploy):
    for key in ("init", "edit"):
        p = ROOT / deploy["contract"]["config"][key]
        assert p.exists() and p.stat().st_mode & 0o111, p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deploy_contract.py -q --override-ini addopts=`
Expected: FAIL, `FileNotFoundError: deploy.toml`.

- [ ] **Step 3: Write deploy.toml**

```toml
# station-web deploy manifest — CLIENT-CONTRACT §5.
#
# station-web is Phase 5 of the 2026-08-10 hf-timestd split: the web UI
# as its own read-only client.  It reads products under /var/lib/timestd
# (frozen root, owned by hf-timestd) and writes nothing there.

[package]
name             = "station-web"
version          = "0.1.0"
description      = "Station data reports and documentation (metrology, ionospheric science, GNSS vTEC)"
contract_version = "0.8"
license          = "MIT"

[contract.config]
init = "scripts/setup-station.sh"
edit = "scripts/config-review.sh"

# The interpreter path names the MINOR series (uv's stable symlink), never a
# patch release — see hamsci-physics/deploy.toml for the breakage that taught it.
# Siblings are not on PyPI: install them editable BEFORE this package.
[build]
steps = [
    "/opt/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11 -m venv /opt/git/sigmond/station-web/venv",
    "/opt/git/sigmond/station-web/venv/bin/pip install --upgrade pip setuptools wheel",
    "/opt/git/sigmond/station-web/venv/bin/pip install -e /opt/git/sigmond/hamsci-dsp",
    "/opt/git/sigmond/station-web/venv/bin/pip install -e /opt/git/sigmond/hamsci-physics",
    "/opt/git/sigmond/station-web/venv/bin/pip install -e .",
]
produces = ["/opt/git/sigmond/station-web/venv/bin/station-web"]

[install]
user  = "stationweb"
group = "timestd"

# Only kind = "link" executes in sigmond today.  Directory and config creation
# belong to [contract.config].init (setup-station.sh), which also creates the
# stationweb user when absent.
[[install.steps]]
kind = "link"
src  = "venv/bin/station-web"
dst  = "/usr/local/bin/station-web"

[[install.steps]]
kind = "link"
src  = "systemd/station-web@.service"
dst  = "/etc/systemd/system/station-web@.service"

[[install.steps]]
kind = "link"
src  = "systemd/station-web.service"
dst  = "/etc/systemd/system/station-web.service"

[systemd]
units           = ["station-web.service"]
templated_units = ["station-web@.service"]

[[deps.git]]
name       = "hamsci-dsp"
url        = "https://github.com/HamSCI/hamsci-dsp"
install_to = "/opt/git/sigmond/hamsci-dsp"
why        = "data-product readers, registry, station catalog, stability"

[[deps.git]]
name       = "hamsci-physics"
url        = "https://github.com/mijahauan/hamsci-physics"
install_to = "/opt/git/sigmond/hamsci-physics"
why        = "solar geometry for the dashboards"

[[deps.pypi]]
name    = "fastapi"
version = ">=0.104.0"
venv    = "/opt/git/sigmond/station-web/venv"
```

- [ ] **Step 4: Write the units**

`systemd/station-web@.service`:
```ini
[Unit]
Description=station-web — station data reports and documentation (%i)
Documentation=https://github.com/mijahauan/station-web
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=20

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=60
User=stationweb
Group=timestd
# CLIENT-CONTRACT §4: station identity and per-instance env, both optional.
EnvironmentFile=-/etc/sigmond/coordination.env
EnvironmentFile=-/etc/station-web/env/%i.env
Environment=MALLOC_ARENA_MAX=2
ExecStart=/opt/git/sigmond/station-web/venv/bin/station-web serve --instance %i
ExecReload=/bin/kill -HUP $MAINPID

# Read-only client.  The one writable path under the data root is the
# directory holding timestd.db: a WAL reader in another uid needs to create
# the -wal/-shm sidecars.  The control socket lives in RuntimeDirectory.
RuntimeDirectory=station-web
RuntimeDirectoryMode=0750
ProtectSystem=strict
ProtectHome=yes
ReadOnlyPaths=/var/lib/timestd /opt/git/sigmond
ReadWritePaths=/var/lib/timestd/phase2
PrivateTmp=yes
NoNewPrivileges=yes

Nice=5
MemoryMax=600M
MemorySwapMax=0
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=station-web

[Install]
WantedBy=multi-user.target
```

`systemd/station-web.service`:
```ini
[Unit]
Description=station-web (single-instance alias for station-web@default)
Requires=station-web@default.service
After=station-web@default.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Write the config scripts**

`scripts/setup-station.sh`:
```bash
#!/usr/bin/env bash
# `smd config init station-web` — render /etc/station-web/station-web.toml.
#
# CONTRACT §14.3: sigmond exports STATION_* so the operator answers nothing
# twice.  STATION_CALL is canonical; STATION_CALLSIGN is the compat alias.
# Also seeds [storage] sqlite_path from hf-timestd's config when present, and
# creates the stationweb user (group timestd) because only link-kind install
# steps execute in sigmond today.
set -euo pipefail

CONFIG_DIR=${CONFIG_DIR:-/etc/station-web}
CONFIG=${CONFIG:-$CONFIG_DIR/station-web.toml}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEMPLATE=$REPO/config/station-web.toml.template
TIMESTD_CONFIG=${TIMESTD_CONFIG:-/etc/hf-timestd/timestd-config.toml}

if [[ $EUID -eq 0 ]] && ! id -u stationweb >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin -g timestd stationweb
    echo "station-web: created user stationweb (group timestd)"
fi

install -d -m 0755 "$CONFIG_DIR" "$CONFIG_DIR/env"

if [[ -f $CONFIG ]]; then
    echo "station-web: $CONFIG exists — leaving it alone (use 'smd config edit')."
    exit 0
fi

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
cp "$TEMPLATE" "$tmp"

set_str() {  # set_str <key> <value>   fills an empty "" slot
    local key=$1 val=$2
    [[ -z $val ]] && return 0
    sed -i "s|^\( *${key} *= *\)\"\"|\1\"${val}\"|" "$tmp"
}
set_num() {  # set_num <key> <value>   replaces a 0.0 slot
    local key=$1 val=$2
    [[ -z $val ]] && return 0
    sed -i "s|^\( *${key} *= *\)0\.0|\1${val}|" "$tmp"
}

set_str callsign    "${STATION_CALL:-${STATION_CALLSIGN:-}}"
set_str grid_square "${STATION_GRID:-}"
set_num latitude    "${STATION_LAT:-}"
set_num longitude   "${STATION_LON:-}"

if [[ -r $TIMESTD_CONFIG ]]; then
    sq=$(sed -n 's/^ *sqlite_path *= *"\([^"]*\)".*/\1/p' "$TIMESTD_CONFIG" | head -1)
    set_str sqlite_path "$sq"
fi

install -m 0644 "$tmp" "$CONFIG"
echo "station-web: wrote $CONFIG"
echo "  station: ${STATION_CALL:-${STATION_CALLSIGN:-<unset>}} / ${STATION_GRID:-<unset>}"
exit 0
```

`scripts/config-review.sh`:
```bash
#!/usr/bin/env bash
# `smd config edit station-web` — show the config and self-validate.
set -euo pipefail
CONFIG=${CONFIG:-/etc/station-web/station-web.toml}
VENV_PY=${VENV_PY:-/opt/git/sigmond/station-web/venv/bin/python3}
[[ -x $VENV_PY ]] || VENV_PY=python3
if [[ ! -f $CONFIG ]]; then
    echo "station-web: $CONFIG not found — run 'smd config init station-web'." >&2
    exit 1
fi
echo "=== $CONFIG ==="; cat "$CONFIG"; echo; echo "=== validate ==="
"$VENV_PY" -m station_web.cli validate --json -c "$CONFIG"
```

```bash
chmod +x scripts/setup-station.sh scripts/config-review.sh
```

- [ ] **Step 6: Run both test files**

Run: `uv run pytest tests/test_deploy_contract.py tests/test_client_contract.py -q --override-ini addopts=`
Expected: all pass, including the manifest/contract version match from Task 3.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "deploy: manifest, templated unit, config init/edit scripts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 5: Control socket (contract §13)

**Files:**
- Create: `src/station_web/control_socket.py`
- Test: `tests/test_control_socket.py`

**Interfaces:**
- Produces: `ControlSocketServer(path: Path, status_fn: Callable[[], dict], group: str | None = "sigmond")` with `.start()`, `.stop()`; responds to `GET /healthz`, `/readyz`, `/status`, `/metrics`. Task 6 starts it inside the app lifespan with `status_fn` returning the freshness table (Task 11) once that exists; until then a dict `{"ready": True}`.

- [ ] **Step 1: Write the failing test**

`tests/test_control_socket.py`:
```python
import http.client
import json
import socket
import time
from pathlib import Path

from station_web.control_socket import ControlSocketServer


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def _get(path, route):
    c = _UnixHTTP(str(path))
    c.request("GET", route)
    r = c.getresponse()
    return r.status, r.read().decode(), r.getheader("Content-Type")


def test_endpoints(tmp_path):
    sock = tmp_path / "control.sock"
    srv = ControlSocketServer(sock, status_fn=lambda: {"ready": True, "products": {"L3_fusion_timing": "VALID"}},
                              group=None)
    srv.start()
    try:
        assert sock.exists()
        st, body, ct = _get(sock, "/healthz")
        assert st == 200 and json.loads(body) == {"ok": True}
        st, body, _ = _get(sock, "/readyz")
        assert st == 200 and json.loads(body)["ready"] is True
        st, body, _ = _get(sock, "/status")
        assert st == 200 and json.loads(body)["products"]["L3_fusion_timing"] == "VALID"
        st, body, ct = _get(sock, "/metrics")
        assert st == 200 and ct.startswith("text/plain")
        assert "station_web_up 1" in body
        st, _, _ = _get(sock, "/nope")
        assert st == 404
    finally:
        srv.stop()
    assert not sock.exists()


def test_answers_fast(tmp_path):
    sock = tmp_path / "c.sock"
    srv = ControlSocketServer(sock, status_fn=lambda: {"ready": True}, group=None)
    srv.start()
    try:
        t0 = time.monotonic()
        for _ in range(20):
            _get(sock, "/healthz")
        assert (time.monotonic() - t0) / 20 < 0.1        # §13: < 100 ms
    finally:
        srv.stop()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_control_socket.py -q --override-ini addopts=`
Expected: FAIL, `No module named station_web.control_socket`.

- [ ] **Step 3: Implement**

`src/station_web/control_socket.py`:
```python
"""CLIENT-CONTRACT §13 control surface: HTTP over a unix socket.

stdlib only, one thread, answers from the ``status_fn`` snapshot the app
refreshes; never touches the data root itself.  Mode 0660, group
``sigmond`` when that group exists (tests pass ``group=None``).
"""
from __future__ import annotations

import grp
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Callable

logger = logging.getLogger(__name__)


class _Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, handler, status_fn):
        self.status_fn = status_fn
        super().__init__(path, handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt, *args):            # journal, not stderr spam
        logger.debug("control: " + fmt, *args)

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):                             # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/healthz":
            return self._send(200, json.dumps({"ok": True}))
        status = self.server.status_fn()
        if route == "/readyz":
            ready = bool(status.get("ready", False))
            return self._send(200 if ready else 503, json.dumps({"ready": ready}))
        if route == "/status":
            return self._send(200, json.dumps(status))
        if route == "/metrics":
            lines = ["station_web_up 1"]
            for name, verdict in (status.get("products") or {}).items():
                lines.append(f'station_web_product_valid{{product="{name}"}} '
                             f'{1 if verdict == "VALID" else 0}')
            return self._send(200, "\n".join(lines) + "\n", "text/plain; version=0.0.4")
        self._send(404, json.dumps({"error": "not found"}))


class ControlSocketServer:
    def __init__(self, path: Path, status_fn: Callable[[], dict], group: str | None = "sigmond"):
        self.path = Path(path)
        self.status_fn = status_fn
        self.group = group
        self._srv: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._srv = _Server(str(self.path), _Handler, self.status_fn)
        os.chmod(self.path, 0o660)
        if self.group:
            try:
                os.chown(self.path, -1, grp.getgrnam(self.group).gr_gid)
            except (KeyError, PermissionError) as exc:
                logger.warning("control socket group %s not applied: %s", self.group, exc)
        self._thread = threading.Thread(target=self._srv.serve_forever, name="control-socket", daemon=True)
        self._thread.start()
        logger.info("control socket listening on %s", self.path)

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self.path.exists():
            self.path.unlink()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_control_socket.py -q --override-ini addopts=`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "control socket: §13 healthz/readyz/status/metrics over a unix socket

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 6: Application factory, serve verb, hygiene tests

**Files:**
- Create: `src/station_web/app.py`, `src/station_web/serve.py`, `src/station_web/routers/__init__.py` (empty for now), `src/station_web/services/__init__.py`, `src/station_web/models/__init__.py`, `src/station_web/static/index.html` (placeholder replaced in Task 7)
- Test: `tests/test_hygiene.py`, `tests/conftest.py` (first version)

**Interfaces:**
- Consumes: `Settings` (Task 2), `ControlSocketServer` (Task 5).
- Produces: `station_web.app.create_app(settings: Settings, control_socket: Path | None = None) -> FastAPI`; `station_web.app.STATUS: dict` (the snapshot the control socket serves; Task 11 fills `STATUS["products"]`); `station_web.serve.run_server(settings, instance, log_level) -> int`. Task 7 registers routers inside `create_app`.

- [ ] **Step 1: Write the failing hygiene tests and the first conftest**

`tests/conftest.py`:
```python
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from station_web import config as cfgmod
from station_web.app import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    root = tmp_path / "timestd"
    (root / "phase2").mkdir(parents=True)
    cfg = tmp_path / "sw.toml"
    cfg.write_text(f'[station]\ncallsign = "AC0G"\ngrid_square = "EM38ww"\n'
                   f'latitude = 38.9\nlongitude = -92.3\n'
                   f'[data]\nroot = "{root}"\n'
                   f'[storage]\nsqlite_path = "{root / "phase2" / "timestd.db"}"\n')
    monkeypatch.setenv("STATION_WEB_CONFIG", str(cfg))
    s = cfgmod.load_settings()
    monkeypatch.setattr(cfgmod, "config", s)          # the moved routers import this singleton
    return s


@pytest.fixture
def client(settings):
    app = create_app(settings, control_socket=None)
    with TestClient(app) as c:
        yield c
```

`tests/test_hygiene.py`:
```python
"""The remit, enforced: products in, HTML out, nothing else."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "station_web"
PY = [p for p in SRC.rglob("*.py")]

FORBIDDEN_IMPORTS = {
    "hf_timestd": "station-web never imports the timing core",
    "subprocess": "no shell, no journalctl, no chronyc",
    "requests": "no outbound HTTP",
    "urllib.request": "no outbound HTTP",
    "httpx": "no outbound HTTP",
    "aiohttp": "no outbound HTTP",
}


def _imports(path: Path):
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(?:from|import)\s+([\w.]+)", line)
        if m:
            yield m.group(1)


def test_no_forbidden_imports():
    bad = []
    for p in PY:
        for mod in _imports(p):
            for root, why in FORBIDDEN_IMPORTS.items():
                if mod == root or mod.startswith(root + "."):
                    bad.append(f"{p.relative_to(SRC)}: {mod} ({why})")
    assert not bad, "\n".join(bad)


def test_no_cors_middleware():
    text = (SRC / "app.py").read_text()
    assert "CORSMiddleware" not in text


def test_static_dir_is_not_created_at_import():
    text = (SRC / "app.py").read_text()
    assert "mkdir" not in text


def test_root_and_health(client):
    assert client.get("/").status_code == 200
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["callsign"] == "AC0G"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_hygiene.py -q --override-ini addopts=`
Expected: FAIL, `No module named station_web.app`.

- [ ] **Step 3: Write app.py and serve.py**

`src/station_web/app.py`:
```python
"""FastAPI application factory.  No CORS (same origin), no import-time
side effects, no writes.  Routers register here (Task 7 onward)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from station_web.config import Settings
from station_web.control_socket import ControlSocketServer

try:
    from systemd import daemon as systemd_daemon
except ImportError:                                 # pragma: no cover
    systemd_daemon = None

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Snapshot served by the control socket.  Task 11 refreshes ``products``.
STATUS: dict = {"ready": False, "products": {}, "updated_at": None}


async def _watchdog():
    while True:
        if systemd_daemon:
            systemd_daemon.notify("WATCHDOG=1")
        await asyncio.sleep(10)


def create_app(settings: Settings, control_socket: Path | None = Path("/run/station-web/control.sock")) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sock = ControlSocketServer(control_socket, status_fn=lambda: STATUS) if control_socket else None
        if sock:
            sock.start()
        STATUS["ready"] = True
        STATUS["updated_at"] = datetime.now(timezone.utc).isoformat()
        if systemd_daemon:
            systemd_daemon.notify("READY=1")
        task = asyncio.create_task(_watchdog())
        logger.info("station-web serving %s for %s", settings.data_root, settings.station.get("callsign", "?"))
        try:
            yield
        finally:
            task.cancel()
            if sock:
                sock.stop()

    app = FastAPI(title="station-web", version="0.1.0", docs_url="/api/docs",
                  redoc_url="/api/redoc", lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def page(name: str):
        p = STATIC_DIR / name
        if p.exists():
            return FileResponse(p)
        return HTMLResponse(f"<h1>station-web</h1><p>{name} not shipped</p>", status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return page("index.html")

    @app.get("/phase", response_class=HTMLResponse)
    async def phase_page():
        return page("phase.html")

    @app.get("/grape", response_class=HTMLResponse)
    async def grape_page():
        return page("grape.html")

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok", "data_root": str(settings.data_root),
                             "callsign": settings.station.get("callsign", "")})

    _register_routers(app)
    return app


def _register_routers(app: FastAPI) -> None:
    """Filled in Task 7."""
    return None
```

`src/station_web/serve.py`:
```python
"""The unit's ExecStart: uvicorn over create_app, SIGHUP re-reads the log level."""
from __future__ import annotations

import logging
import os
import signal

import uvicorn

from station_web.app import create_app
from station_web.config import Settings

logger = logging.getLogger(__name__)


def _install_sighup(settings: Settings) -> None:
    def _reload(signum, frame):
        from station_web.cli import resolve_log_level
        level = resolve_log_level(None, settings)
        logging.getLogger().setLevel(level)
        logger.info("SIGHUP: log level now %s", level)
    signal.signal(signal.SIGHUP, _reload)


def run_server(settings: Settings, instance: str = "default", log_level: str = "INFO") -> int:
    _install_sighup(settings)
    app = create_app(settings)
    uvicorn.run(app, host=settings.web_bind, port=settings.web_port,
                log_level=log_level.lower(), access_log=False)
    return 0
```

Create the package markers and a placeholder page:
```bash
touch src/station_web/routers/__init__.py src/station_web/services/__init__.py src/station_web/models/__init__.py
printf '<!doctype html><title>station-web</title><h1>station-web</h1>\n' > src/station_web/static/index.html
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ -q --override-ini addopts=`
Expected: all pass (`test_hygiene.py` 4, plus Tasks 2–5).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "app: factory with lifespan, sd_notify, watchdog, control socket; serve verb; hygiene tests

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 7: Move the product-backed routers, services, models, and pages

**Files:**
- Create (by copy from `hf-timestd/web-api/`): `src/station_web/routers/{metrology,stability,phase,physics,tec,tid,ionogram,dashboard,station,stations,grape,correlations,propagation,chrony,space_weather,health,docs}.py`; `src/station_web/services/{fusion_service,stability_service,stability_core,physics_service,tid_service,tec_service,scintillation_service,test_signal_service,event_service,phase_service,propagation_service,grape_service,chrony_service,correlation_service,space_weather_service,health_service}.py`; `src/station_web/models/{health,station,timing}.py`; `src/station_web/broadcast.py`; `src/station_web/static/**`
- Create: `src/station_web/services/channels.py`
- Modify: `src/station_web/app.py` (`_register_routers`)
- Test: `tests/test_routes.py` (smoke: every kept router registers; product routes answer 200 on an empty fixture)

**Interfaces:**
- Consumes: `hamsci_dsp.io.make_data_product_reader(data_dir, product_level, product_name, channel, *, storage_config)`, `hamsci_dsp.io.SqliteDataProductReader`, `hamsci_dsp.io.sqlite_writer.DEFAULT_DB_PATH`, `hamsci_dsp.data_product_registry.DataProductRegistry`, `hamsci_dsp.stations.BUILTIN_CATALOG`, `hamsci_dsp.stability.*` (Task 1), `hamsci_physics.solar_zenith`.
- Produces: `station_web.services.channels.discover_channels(settings) -> list[dict]` (`{"name": str, "frequency_hz": int | None}`) for the station router; routers importable as `station_web.routers.<name>.router`.

- [ ] **Step 1: Copy the kept files**

```bash
cd /home/mjh/hamsci/repos
W=hf-timestd/web-api; S=station-web/src/station_web
for r in metrology stability phase physics tec tid ionogram dashboard station stations grape correlations propagation chrony space_weather health docs; do cp $W/routers/$r.py $S/routers/; done
for s in fusion_service stability_service stability_core physics_service tid_service tec_service scintillation_service test_signal_service event_service phase_service propagation_service grape_service chrony_service correlation_service space_weather_service health_service; do cp $W/services/$s.py $S/services/; done
cp $W/models/health.py $W/models/station.py $W/models/timing.py $S/models/
cp hf-timestd/src/hf_timestd/models/broadcast.py $S/broadcast.py
rm -rf $S/static && cp -r $W/static $S/static && mkdir -p $S/static/vendor
rm -f $S/static/logs.html $S/static/timing-validation.html
```
Not copied, by decision: `routers/logs.py`, `routers/timing_validation.py`, `main.py`, `config.py`, `debug_routes.py`, `test_solar_api.py`, `routers/__init__.py` (station-web's own stays empty).

- [ ] **Step 2: Rewrite imports mechanically**

```bash
cd $S
grep -rl "from config import config\|from services\.\|from services import\|from routers\.\|from models\.\|from models import\|hf_timestd" . --include=*.py | xargs sed -i \
  -e 's/^from config import config$/from station_web.config import config/' \
  -e 's/^\(\s*\)from config import config$/\1from station_web.config import config/' \
  -e 's/^from services\./from station_web.services./' \
  -e 's/^\(\s*\)from services\./\1from station_web.services./' \
  -e 's/^from models\./from station_web.models./' \
  -e 's/^\(\s*\)from models\./\1from station_web.models./' \
  -e 's/^from models import/from station_web.models import/' \
  -e 's/from hf_timestd\.io\.sqlite_writer import/from hamsci_dsp.io.sqlite_writer import/' \
  -e 's/from hf_timestd\.io import/from hamsci_dsp.io import/' \
  -e 's/from hf_timestd\.data_product_registry import/from hamsci_dsp.data_product_registry import/' \
  -e 's/from hf_timestd\.core\.stability_analysis import/from hamsci_dsp.stability import/' \
  -e 's/from hf_timestd\.models\.broadcast import/from station_web.broadcast import/'
grep -rn "hf_timestd\|^from config\|^from services\|^from routers\|^from models\|sys\.path" . --include=*.py
```
Expected residue from the last grep, and how to clear each:
- `services/test_signal_service.py:23` `from hf_timestd.core.wwv_constants import WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON` → replace with:
  ```python
  from hamsci_dsp.stations import BUILTIN_CATALOG
  WWV_LAT, WWV_LON = BUILTIN_CATALOG.get("WWV").coordinates
  WWVH_LAT, WWVH_LON = BUILTIN_CATALOG.get("WWVH").coordinates
  ```
- `routers/propagation.py:28` and `:361` (`HFPropagationModel`, `IonoDataService`): delete the two endpoints that contain them (the whole `@router.get` function for each). Keep every other route in the file.
- `routers/health.py:14` `QuotaManager`: leave for Task 11, which rewrites the file.
- `services/chrony_service.py:17-18` `collect_chrony_snapshot`: leave for Task 9, which rewrites the file.
- `services/health_service.py` `subprocess`: leave for Task 11.
- `services/space_weather_service.py` `requests`: leave for Task 10.
- `routers/docs.py` `subprocess`: leave for Task 12.
- Any `sys.path.insert` line: delete it.
- `broadcast.py`: it imports `hamsci_dsp.geometry.great_circle_km` already; confirm no `hf_timestd` import remains.

- [ ] **Step 3: Add the channel discovery service**

`src/station_web/services/channels.py`:
```python
"""Channels known to the station, discovered from the products themselves.

hf-timestd's config listed channels under [recorder.channel_group]; a
read-only client has no such list.  The distinct ``channel`` values in
L2_timing_measurements say which channels actually produced data.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from hamsci_dsp.io.sqlite_writer import DEFAULT_DB_PATH

from station_web.config import Settings

logger = logging.getLogger(__name__)
_FREQ = re.compile(r"_(\d+)$")           # WWV_10000 -> 10000 kHz


def _db_path(settings: Settings) -> Path:
    return Path(settings.storage.get("sqlite_path", DEFAULT_DB_PATH))


def discover_channels(settings: Settings, table: str = "L2_timing_measurements") -> list[dict]:
    db = _db_path(settings)
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(f'SELECT DISTINCT channel FROM "{table}" ORDER BY channel').fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.warning("channel discovery failed on %s: %s", db, exc)
        return []
    out = []
    for (name,) in rows:
        m = _FREQ.search(name or "")
        out.append({"name": name, "frequency_hz": int(m.group(1)) * 1000 if m else None})
    return out
```
Then in `routers/station.py` and `routers/health.py`, replace `config.channels` with `discover_channels(config)` (import `from station_web.services.channels import discover_channels`). The `ChannelInfo` model in `models/station.py` takes `name` and `frequency_hz`; adapt the dict keys if its field names differ (read the model, keep its names).

- [ ] **Step 4: Register the routers**

Replace `_register_routers` in `app.py`:
```python
def _register_routers(app: FastAPI) -> None:
    from station_web.routers import (chrony, correlations, dashboard, docs, grape, health,
                                     ionogram, metrology, phase, physics, propagation,
                                     space_weather, stability, station, stations, tec, tid)
    for mod in (station, stations, health, metrology, stability, propagation, space_weather,
                correlations, physics, tec, tid, dashboard, phase, grape, ionogram, chrony):
        app.include_router(mod.router, prefix="/api")
    app.include_router(docs.router)          # carries its own /api/docs prefix
```

- [ ] **Step 5: Write the smoke test**

`tests/test_routes.py` (first version; Task 8 extends it):
```python
"""Every kept route registers and answers on an EMPTY data root.
A products-only client must degrade to empty payloads, never 500."""
import pytest

KEPT_PREFIXES = ["/api/station", "/api/stations", "/api/health", "/api/metrology",
                 "/api/stability", "/api/propagation", "/api/space-weather", "/api/correlations",
                 "/api/physics", "/api/tec", "/api/tid", "/api/dashboard", "/api/phase",
                 "/api/grape", "/api/ionogram", "/api/chrony", "/api/docs"]
CUT_PATHS = ["/api/logs/services", "/api/timing-validation/status", "/api/health/storage"]


def _routes(client):
    return [r.path for r in client.app.routes if hasattr(r, "methods") and "GET" in r.methods]


def test_kept_routers_registered(client):
    paths = _routes(client)
    for prefix in KEPT_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), prefix


def test_cut_routes_are_gone(client):
    for p in CUT_PATHS:
        assert client.get(p).status_code == 404, p


def test_parameterless_gets_never_500_on_empty_root(client):
    failures = []
    for path in _routes(client):
        if "{" in path or path.startswith("/api/docs"):
            continue
        r = client.get(path)
        if r.status_code >= 500:
            failures.append((path, r.status_code, r.text[:200]))
    assert not failures, failures
```

- [ ] **Step 6: Run the suite and fix what the smoke test exposes**

Run: `uv run pytest tests/ -q --override-ini addopts=`
Expected: `test_hygiene.py::test_no_forbidden_imports` FAILS naming exactly the five files Tasks 9–12 rewrite (`health.py` router, `health_service.py`, `chrony_service.py`, `space_weather_service.py`, `docs.py`). `test_parameterless_gets_never_500_on_empty_root` may fail on routes whose service raises on a missing DB; fix each by catching the exception in the service and returning an empty payload (`{"data": [], "n": 0}` or the route's existing empty shape). Do not silence with a bare `except: pass`; log at WARNING and return the empty shape. Record the temporarily failing hygiene test in the commit message.

- [ ] **Step 7: Commit**

```bash
cd /home/mjh/hamsci/repos/station-web
git add -A
git commit -m "move: product-backed routers, services, models, pages from hf-timestd web-api

Imports repointed at hamsci_dsp / hamsci_physics / station_web.  Two live-model
propagation endpoints dropped.  Channel list discovered from L2 rows.  Hygiene
test intentionally red on the five files Tasks 9-12 rewrite.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 8: Fixture data root and route tests against real rows

**Files:**
- Modify: `tests/conftest.py` (add `seeded` fixture), `tests/test_routes.py`

**Interfaces:**
- Consumes: `hamsci_dsp.io.SqliteDataProductWriter` (or `make_data_product_writer`) to create one row per product; the registry schemas in `hamsci_dsp/schemas/registry.json` name the tables and fields.
- Produces: fixture `seeded_client` with one recent row in each of: `L1_all_arrivals`, `L1_broadcast_measurements`, `L2_timing_measurements`, `L2_tick_phase`, `L2_tick_timing`, `L2_test_signal`, `L3_fusion_timing`, `L3_physics`, `L3_tec`, `L3_dtec`, `L3_tid`, `L3C_propagation_stats`, `DIAG_chrony_stats`; plus `iono_cache/space_weather.json` and one GRAPE spectrogram PNG with `_meta.json`.

- [ ] **Step 1: Discover the writer API and required fields**

```bash
cd /home/mjh/hamsci/repos/hamsci-dsp
grep -n "def make_data_product_writer\|def write\b\|def write_\|def append" src/hamsci_dsp/io/dual_writer.py src/hamsci_dsp/io/sqlite_writer.py | head
python3 - <<'EOF'
import json
reg = json.load(open('src/hamsci_dsp/schemas/registry.json'))
items = reg if isinstance(reg, list) else reg.get('products', reg)
for e in items:
    print(e if isinstance(e, str) else {k: e[k] for k in e if k in ('level','name','product','schema','table')})
EOF
```
Write down, per product, the schema file and its `required: true` fields. The fixture must supply every required field with a plausible value; the writer validates against the schema.

- [ ] **Step 2: Write the seeding fixture**

Add to `tests/conftest.py` (adapt field lists to Step 1's findings; the structure stays):
```python
import json
from datetime import datetime, timedelta, timezone

from hamsci_dsp.io import make_data_product_writer


def _now_iso(offset_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def _write_rows(root, storage, level, name, channel, rows):
    w = make_data_product_writer(data_dir=root / "phase2", product_level=level, product_name=name,
                                 channel=channel, storage_config=storage)
    try:
        for r in rows:
            w.write(r)                       # or w.append(r) — whichever Step 1 found
    finally:
        w.close()


@pytest.fixture
def seeded(settings):
    root, storage = settings.data_root, settings.storage
    ts = _now_iso(30)
    _write_rows(root, storage, "L2", "timing_measurements", "WWV_10000",
                [{"timestamp_utc": ts, "channel": "WWV_10000", "station": "WWV",
                  # ...every required field from schemas/l2_timing_measurements_v1.json...
                  }])
    _write_rows(root, storage, "L3", "fusion_timing", "fusion",
                [{"timestamp_utc": ts, "channel": "fusion",
                  # ...required fields from l3_fusion_timing_v1.json...
                  }])
    _write_rows(root, storage, "DIAG", "chrony_stats", "fusion",
                [{"timestamp_utc": ts, "unix_time": datetime.now(timezone.utc).timestamp() - 30,
                  "source_name": "FUSE", "source_mode": "#", "source_state": "*", "stratum": 0,
                  "reach": 377, "offset_us": 120.0, "error_us": 800.0, "n_samples": 8,
                  "std_dev_us": 900.0, "sys_offset_s": 0.00012, "channel": "fusion",
                  # ...remaining required fields from diag_chrony_stats_v1.json...
                  },
                 {"timestamp_utc": ts, "unix_time": datetime.now(timezone.utc).timestamp() - 30,
                  "source_name": "192.168.1.1", "source_mode": "^", "source_state": "+", "stratum": 2,
                  "reach": 377, "offset_us": -450.0, "error_us": 2000.0, "n_samples": 8,
                  "std_dev_us": 1500.0, "sys_offset_s": 0.00012, "channel": "fusion"}])
    # ...one row each for the remaining products in the Interfaces list...
    (root / "iono_cache").mkdir()
    (root / "iono_cache" / "space_weather.json").write_text(json.dumps({
        "f107": 142.0, "f107_time": ts, "f107_source": "swpc", "kp": 2.3, "kp_time": ts,
        "kp_source": "gfz", "ap": 9.0, "ap_time": ts, "ap_source": "gfz", "fetched_at": ts}))
    spec = root / "products" / "WWV_10000" / "spectrograms"
    spec.mkdir(parents=True)
    (spec / "20260906_spectrogram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (spec / "20260906_meta.json").write_text(json.dumps({"date": "2026-09-06", "channel": "WWV_10000"}))
    return settings


@pytest.fixture
def seeded_client(seeded):
    with TestClient(create_app(seeded, control_socket=None)) as c:
        yield c
```
Every `# ...` line above must be replaced with the actual required fields before this task closes; the writer's schema validation is the test that you did.

- [ ] **Step 3: Write the route assertions**

Append to `tests/test_routes.py`:
```python
ROUTES_WITH_DATA = [
    ("/api/metrology/fusion/latest", lambda j: j is not None and "timestamp_utc" in json.dumps(j)),
    ("/api/metrology/fusion/history?start=-6h&end=now", lambda j: isinstance(j, dict)),
    ("/api/stability/adev?start=-24h&end=now", lambda j: isinstance(j, dict)),
    ("/api/station/metadata", lambda j: j["callsign"] == "AC0G" and j["channels"]),
    ("/api/health/system", lambda j: j["status"] in {"healthy", "degraded", "error"}),
    ("/api/chrony/history?hours=1", lambda j: "FUSE" in j["sources"]),
    ("/api/chrony/snapshot", lambda j: len(j["sources"]) == 2),
    ("/api/chrony/comparison", lambda j: any(s["name"] == "FUSE" for s in j["sources"])),
    ("/api/space-weather/current", lambda j: j["f107"] == 142.0 and "age_s" in j),
    ("/api/tec/latest", lambda j: isinstance(j, (dict, list))),
    ("/api/tid/latest", lambda j: isinstance(j, (dict, list))),
    ("/api/physics/latest", lambda j: isinstance(j, (dict, list))),
    ("/api/dashboard/summary?hours=24", lambda j: isinstance(j, dict)),
    ("/api/grape/channels", lambda j: "WWV_10000" in json.dumps(j)),
]


@pytest.mark.parametrize("path,check", ROUTES_WITH_DATA)
def test_route_with_data(seeded_client, path, check):
    r = seeded_client.get(path)
    assert r.status_code == 200, (path, r.text[:300])
    assert check(r.json()), (path, r.text[:300])
```
Adjust each path to the route's actual path string (read the router file; do not guess). Add `import json` at the top. Tasks 9–11 make the chrony, space-weather, and health rows pass; until then mark those three with `pytest.mark.xfail(strict=True, reason="Task 9/10/11")` and remove the marks in those tasks.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_routes.py -q --override-ini addopts=`
Expected: every non-xfail parametrisation passes; the three xfails report XFAIL.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "tests: seeded fixture data root; every kept route answers with data

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 9: Chrony snapshot and comparison from the diagnostic product

**Files:**
- Modify: `src/station_web/services/chrony_service.py` (rewrite `get_live_snapshot`, `get_source_comparison`; drop the `chrony_stats` import), `tests/test_routes.py` (remove two xfails)
- Test: `tests/test_chrony_from_product.py`

**Interfaces:**
- Consumes: `DIAG_chrony_stats` rows via `make_data_product_reader(data_dir=fusion_dir, product_level="DIAG", product_name="chrony_stats", channel="fusion", storage_config=...)`, fields `timestamp_utc, unix_time, source_name, source_mode, source_state, stratum, reach, offset_us, error_us, n_samples, std_dev_us, sys_offset_s`.
- Produces: `ChronyService.get_live_snapshot() -> dict | None` with keys `timestamp_utc, unix_time, age_s, stale: bool, tracking: {sys_offset_us} | None, sources: [ {name, mode, state, stratum, reach, offset_us, error_us, n_samples, std_dev_us} ]`; `get_source_comparison() -> dict` with `sources` (same list, sorted selected-first) and `stale`, `age_s`.

- [ ] **Step 1: Write the failing test**

`tests/test_chrony_from_product.py`:
```python
from station_web.services.chrony_service import ChronyService, STALE_AFTER_S


def test_snapshot_is_latest_row_per_source(seeded):
    svc = ChronyService(data_root=seeded.data_root, storage_config=seeded.storage)
    snap = svc.get_live_snapshot()
    assert snap is not None
    names = {s["name"] for s in snap["sources"]}
    assert names == {"FUSE", "192.168.1.1"}
    fuse = next(s for s in snap["sources"] if s["name"] == "FUSE")
    assert fuse["state"] == "*" and fuse["offset_us"] == 120.0
    assert snap["tracking"]["sys_offset_us"] == 120.0
    assert snap["stale"] is False and snap["age_s"] < STALE_AFTER_S


def test_snapshot_none_without_rows(settings):
    svc = ChronyService(data_root=settings.data_root, storage_config=settings.storage)
    assert svc.get_live_snapshot() is None


def test_comparison_selected_first(seeded):
    svc = ChronyService(data_root=seeded.data_root, storage_config=seeded.storage)
    cmp_ = svc.get_source_comparison()
    assert cmp_["sources"][0]["name"] == "FUSE"
    assert "stale" in cmp_ and "age_s" in cmp_
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_chrony_from_product.py -q --override-ini addopts=`
Expected: FAIL (`STALE_AFTER_S` undefined, or `chronyc` path taken).

- [ ] **Step 3: Rewrite the two methods**

In `services/chrony_service.py`: delete the `from hf_timestd.core.chrony_stats import collect_chrony_snapshot, ...` import; give the constructor a `storage_config: dict | None = None` parameter stored as `self.storage_config` (the router passes `config.storage`); add:
```python
STALE_AFTER_S = 120.0      # fusion writes one row per source per minute

STATE_ORDER = {"*": 0, "+": 1, "-": 2, "x": 3, "~": 4, "?": 5}


def _latest_rows(self, window_s: float = 600.0) -> list[dict]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(seconds=window_s)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    reader = make_data_product_reader(data_dir=self.fusion_dir, product_level="DIAG",
                                      product_name="chrony_stats", channel="fusion",
                                      storage_config=self.storage_config or {})
    try:
        rows = reader.read_time_range(start=start, end=end)
    finally:
        if hasattr(reader, "close"):
            reader.close()
    latest: dict[str, dict] = {}
    for r in rows:                              # ordered by timestamp; last wins
        if r.get("source_name"):
            latest[r["source_name"]] = r
    return list(latest.values())


def get_live_snapshot(self) -> Optional[Dict[str, Any]]:
    rows = self._latest_rows()
    if not rows:
        return None
    newest = max(r["unix_time"] for r in rows)
    age = datetime.now(timezone.utc).timestamp() - newest
    sources = [{
        "name": r["source_name"], "mode": r.get("source_mode", ""), "state": r.get("source_state", ""),
        "stratum": r.get("stratum"), "reach": r.get("reach"), "offset_us": r.get("offset_us"),
        "error_us": r.get("error_us"), "n_samples": r.get("n_samples"), "std_dev_us": r.get("std_dev_us"),
    } for r in rows]
    sources.sort(key=lambda s: (STATE_ORDER.get(s["state"], 9), s["name"]))
    sys_off = next((r.get("sys_offset_s") for r in rows if r.get("sys_offset_s") is not None), None)
    return {
        "timestamp_utc": datetime.fromtimestamp(newest, timezone.utc).isoformat().replace("+00:00", "Z"),
        "unix_time": newest, "age_s": round(age, 1), "stale": age > STALE_AFTER_S,
        "tracking": {"sys_offset_us": round(sys_off * 1e6, 2)} if sys_off is not None else None,
        "sources": sources,
    }


def get_source_comparison(self) -> Dict[str, Any]:
    snap = self.get_live_snapshot()
    if snap is None:
        return {"sources": [], "stale": True, "age_s": None, "note": "no DIAG_chrony_stats rows"}
    return {"sources": snap["sources"], "tracking": snap["tracking"],
            "stale": snap["stale"], "age_s": snap["age_s"], "timestamp_utc": snap["timestamp_utc"]}
```
Bind the three functions as methods (indent into the class). Update `routers/chrony.py` to construct `ChronyService(data_root=config.data_root, storage_config=config.storage)` and to return 404 with detail `"no chrony rows in the last 10 minutes"` when `get_live_snapshot()` is `None` (it returned 503 "chronyc not available" before). Remove the two chrony xfail marks in `tests/test_routes.py`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_chrony_from_product.py tests/test_routes.py -q --override-ini addopts=`
Expected: pass; chrony parametrisations no longer xfail.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chrony: snapshot and comparison from DIAG_chrony_stats, never chronyc

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 10: Space weather from hamsci-physics's cache; correlations degrade honestly

**Files:**
- Create: `src/station_web/services/space_weather_cache.py`
- Delete: `src/station_web/services/space_weather_service.py`
- Modify: `src/station_web/routers/space_weather.py`, `src/station_web/services/correlation_service.py`, `src/station_web/routers/correlations.py`, `tests/test_routes.py` (remove xfail)
- Test: `tests/test_space_weather_cache.py`

**Interfaces:**
- Consumes: `<data_root>/iono_cache/space_weather.json` with the `hamsci_dsp.ionosphere.space_weather.SpaceWeather` dataclass fields (`f107, f107_time, f107_source, kp, kp_time, kp_source, ap, ap_time, ap_source, fetched_at`).
- Produces: `SpaceWeatherCache(data_root: Path)` with `.current() -> dict` (all ten fields plus `age_s: float | None`, `stale: bool`, `available: bool`) and `.series_unavailable(kind: str) -> dict` returning `{"available": False, "kind": kind, "reason": "station-web reads hamsci-physics's snapshot cache; no <kind> time series product exists yet"}`. Correlation service methods that needed X-ray or Kp series return `{"available": False, "reason": ...}` with HTTP 200.

- [ ] **Step 1: Write the failing test**

`tests/test_space_weather_cache.py`:
```python
import json
from datetime import datetime, timedelta, timezone

from station_web.services.space_weather_cache import SpaceWeatherCache, STALE_AFTER_S


def test_current_from_cache(seeded):
    c = SpaceWeatherCache(seeded.data_root).current()
    assert c["available"] is True and c["f107"] == 142.0 and c["kp"] == 2.3
    assert c["age_s"] is not None and c["stale"] is False


def test_missing_cache(settings):
    c = SpaceWeatherCache(settings.data_root).current()
    assert c["available"] is False and c["f107"] is None and c["age_s"] is None


def test_stale_flag(seeded):
    p = seeded.data_root / "iono_cache" / "space_weather.json"
    d = json.loads(p.read_text())
    d["fetched_at"] = (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_S + 60)).isoformat()
    p.write_text(json.dumps(d))
    assert SpaceWeatherCache(seeded.data_root).current()["stale"] is True


def test_series_endpoints_say_unavailable(seeded_client):
    for path in ("/api/space-weather/xray", "/api/space-weather/kp", "/api/space-weather/protons"):
        r = seeded_client.get(path)
        assert r.status_code == 200 and r.json()["available"] is False, path


def test_no_poller_thread_started(seeded_client):
    import threading
    assert not any("space" in t.name.lower() for t in threading.enumerate())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_space_weather_cache.py -q --override-ini addopts=`
Expected: FAIL, module missing.

- [ ] **Step 3: Implement the cache reader**

`src/station_web/services/space_weather_cache.py`:
```python
"""Space weather as hamsci-physics last saw it.  Read-only, no fetching."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
CACHE_REL = Path("iono_cache") / "space_weather.json"
STALE_AFTER_S = 3 * 3600          # hamsci-physics refreshes every 30 min; healthcheck allows 180
FIELDS = ("f107", "f107_time", "f107_source", "kp", "kp_time", "kp_source",
          "ap", "ap_time", "ap_source", "fetched_at")


class SpaceWeatherCache:
    def __init__(self, data_root: Path):
        self.path = Path(data_root) / CACHE_REL

    def current(self) -> dict:
        out = {k: None for k in FIELDS}
        out.update({"available": False, "age_s": None, "stale": True, "source_file": str(self.path)})
        if not self.path.exists():
            return out
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("space weather cache unreadable: %s", exc)
            return out
        for k in FIELDS:
            out[k] = data.get(k)
        fetched = data.get("fetched_at")
        if fetched:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched.replace("Z", "+00:00"))).total_seconds()
                out["age_s"] = round(age, 1)
                out["stale"] = age > STALE_AFTER_S
            except ValueError:
                pass
        out["available"] = out["f107"] is not None or out["kp"] is not None
        return out

    @staticmethod
    def series_unavailable(kind: str) -> dict:
        return {"available": False, "kind": kind, "data": [],
                "reason": f"station-web reads hamsci-physics's snapshot cache; "
                          f"no {kind} time-series product exists yet"}
```

- [ ] **Step 4: Rewrite the router and the correlation dependencies**

In `routers/space_weather.py`: delete the import of `SpaceWeatherService` and the module-level instance; add `from station_web.services.space_weather_cache import SpaceWeatherCache`. `/current` and `/summary` return `SpaceWeatherCache(config.data_root).current()` (for `/summary`, merge the previous summary keys that came from products and drop those that came from NOAA). `/xray`, `/kp`, `/protons`, `/events/sid` return `SpaceWeatherCache.series_unavailable("xray" | "kp" | "protons" | "sid_events")`. `/solar/elevation` stays as is (pure geometry). Delete `services/space_weather_service.py`.

In `services/correlation_service.py`: replace the `SpaceWeatherService` dependency with `SpaceWeatherCache`; the methods at lines 174 (`get_xray_flux`), 183 (`detect_sid_events`), 319 (`get_kp_index`) return `{"available": False, "reason": "space weather time series not available from products"}` before reaching the product side. Keep the correlations that use only products (timing vs solar elevation). Remove the space-weather xfail from `tests/test_routes.py`.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_space_weather_cache.py tests/test_routes.py tests/test_hygiene.py -q --override-ini addopts=`
Expected: pass; hygiene now names only `health*.py` and `docs.py`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "space weather: read hamsci-physics's cache; no fetching, no poller; series say unavailable

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 11: Health as product freshness

**Files:**
- Create: `src/station_web/services/freshness.py`
- Delete: `src/station_web/services/health_service.py`
- Modify: `src/station_web/routers/health.py` (rewrite), `src/station_web/models/health.py` (drop `ProcessStatus`, add `ProductFreshness`), `src/station_web/static/health.html` (fields), `src/station_web/app.py` (refresh `STATUS["products"]`), `tests/test_routes.py` (remove xfail)
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `make_data_product_reader` for each watched product; `discover_channels` (Task 7).
- Produces: `freshness.EXPECTED = {("L2","timing_measurements"): 120, ("L3","fusion_timing"): 120, ("DIAG","chrony_stats"): 120, ("L3","tec"): 3600, ("L3","dtec"): 300, ("L3","tid"): 3600, ("L3","physics"): 120, ("L3","gnss_vtec"): 900}` (seconds); `freshness.table(settings) -> dict` with `products: {name: {"verdict": "VALID"|"INVALID"|"INDETERMINATE", "age_s": float|None, "expected_s": int, "channel": str}}`, `status: "healthy"|"degraded"|"error"`, `timestamp`; `app.STATUS["products"]` is `{name: verdict}` refreshed on every `/api/health/system` call.

- [ ] **Step 1: Write the failing test**

`tests/test_freshness.py`:
```python
from station_web.services import freshness


def test_seeded_products_valid(seeded):
    t = freshness.table(seeded)
    assert t["products"]["L3_fusion_timing"]["verdict"] == "VALID"
    assert t["products"]["DIAG_chrony_stats"]["verdict"] == "VALID"
    assert t["products"]["L3_gnss_vtec"]["verdict"] == "INDETERMINATE"      # not seeded: honest
    assert t["status"] in {"healthy", "degraded"}


def test_empty_root_is_indeterminate_not_error(settings):
    t = freshness.table(settings)
    assert all(p["verdict"] == "INDETERMINATE" for p in t["products"].values())
    assert t["status"] == "degraded"


def test_health_system_route_shape(seeded_client):
    j = seeded_client.get("/api/health/system").json()
    assert set(j) >= {"status", "timestamp", "products", "channels"}
    assert "processes" not in j and "disk_usage_percent" not in j
    from station_web.app import STATUS
    assert STATUS["products"]["L3_fusion_timing"] == "VALID"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_freshness.py -q --override-ini addopts=`
Expected: FAIL, module missing.

- [ ] **Step 3: Implement**

`src/station_web/services/freshness.py`:
```python
"""Health = are the products fresh?  Four verdicts, borrowed from the
heartbeat schema: VALID (fresh), INVALID (present but older than expected),
INDETERMINATE (no rows at all).  No process or disk probes: sigmond owns those."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from hamsci_dsp.io import make_data_product_reader

from station_web.config import Settings
from station_web.services.channels import discover_channels

logger = logging.getLogger(__name__)

#: (level, name) -> expected max age in seconds
EXPECTED = {
    ("L2", "timing_measurements"): 120, ("L3", "fusion_timing"): 120, ("DIAG", "chrony_stats"): 120,
    ("L3", "tec"): 3600, ("L3", "dtec"): 300, ("L3", "tid"): 3600, ("L3", "physics"): 120,
    ("L3", "gnss_vtec"): 900,
}
#: products written under a fixed channel rather than per receiver channel
FIXED_CHANNEL = {("L3", "fusion_timing"): "fusion", ("DIAG", "chrony_stats"): "fusion",
                 ("L3", "physics"): "fusion", ("L3", "gnss_vtec"): "gnss"}


def _newest_age(settings: Settings, level: str, name: str, channel: str, lookback_s: int) -> float | None:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(seconds=lookback_s)).isoformat().replace("+00:00", "Z")
    reader = make_data_product_reader(data_dir=settings.data_root / "phase2", product_level=level,
                                      product_name=name, channel=channel, storage_config=settings.storage)
    try:
        rows = reader.read_time_range(start=start, end=now.isoformat().replace("+00:00", "Z"))
    except Exception as exc:                  # noqa: BLE001
        logger.warning("freshness read %s_%s/%s failed: %s", level, name, channel, exc)
        return None
    finally:
        if hasattr(reader, "close"):
            reader.close()
    if not rows:
        return None
    ts = rows[-1]["timestamp_utc"]
    return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def table(settings: Settings) -> dict:
    channels = [c["name"] for c in discover_channels(settings)] or ["WWV_10000"]
    products: dict[str, dict] = {}
    for (level, name), expected in EXPECTED.items():
        channel = FIXED_CHANNEL.get((level, name), channels[0])
        age = _newest_age(settings, level, name, channel, lookback_s=max(expected * 10, 86400))
        if age is None:
            verdict = "INDETERMINATE"
        elif age <= expected:
            verdict = "VALID"
        else:
            verdict = "INVALID"
        products[f"{level}_{name}"] = {"verdict": verdict, "age_s": None if age is None else round(age, 1),
                                       "expected_s": expected, "channel": channel}
    verdicts = {p["verdict"] for p in products.values()}
    status = "healthy" if verdicts == {"VALID"} else ("error" if "INVALID" in verdicts else "degraded")
    return {"status": status, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "products": products, "channels": channels}
```

Rewrite `routers/health.py`:
```python
"""Health = product freshness.  No processes, no disk, no upload queue."""
from fastapi import APIRouter

from station_web import app as appmod
from station_web.config import config
from station_web.services import freshness
from station_web.services.channels import discover_channels

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/system")
async def get_system_health():
    t = freshness.table(config)
    appmod.STATUS["products"] = {k: v["verdict"] for k, v in t["products"].items()}
    appmod.STATUS["updated_at"] = t["timestamp"]
    return t


@router.get("/channels")
async def get_channel_status():
    return {"channels": discover_channels(config)}
```
Delete `/storage` and `/grape` (management reads). Delete `services/health_service.py`. In `models/health.py` remove `ProcessStatus` and `SystemHealth`; add:
```python
class ProductFreshness(BaseModel):
    verdict: str
    age_s: Optional[float] = None
    expected_s: int
    channel: str
```
In `static/health.html`, replace the `processes`, `disk_usage_percent`, `data_completeness`, `errors`, `uptime` rendering with a table over `data.products` (name, verdict, age, expected) and keep the `data.channels` list and `data.status` badge. Remove the health xfail in `tests/test_routes.py`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/ -q --override-ini addopts=`
Expected: all pass except hygiene naming only `routers/docs.py`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "health: product freshness with four verdicts; no process, disk or queue probes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 12: Documentation over four roots, path-confined; evidence endpoints cut

**Files:**
- Modify: `src/station_web/routers/docs.py` (rewrite list/get/section; delete evidence)
- Test: `tests/test_docs_roots.py`

**Interfaces:**
- Consumes: `Settings.docs_roots`.
- Produces: `GET /api/docs/list -> {"roots": [{"name", "path", "present": bool, "documents": [names]}]}`, `GET /api/docs/{root}/{doc_name}`, `GET /api/docs/{root}/{doc_name}/section/{section_id}`. Document names match `^[A-Za-z0-9_.-]+$` and must end `.md`; anything else is 400. `docs.html` gains a root selector.

- [ ] **Step 1: Write the failing tests**

`tests/test_docs_roots.py`:
```python
import pytest


@pytest.fixture
def docs_client(settings, tmp_path, monkeypatch):
    from station_web import config as cfgmod
    from station_web.app import create_app
    from fastapi.testclient import TestClient
    r1 = tmp_path / "r1"; r1.mkdir()
    (r1 / "GUIDE.md").write_text("# Guide\n\nIntro.\n\n## Setup\n\nDo this.\n")
    (r1 / "secret.txt").write_text("nope")
    settings.raw["docs"] = {"roots": [{"name": "one", "path": str(r1)},
                                      {"name": "gone", "path": str(tmp_path / "gone")}]}
    monkeypatch.setattr(cfgmod, "config", settings)
    with TestClient(create_app(settings, control_socket=None)) as c:
        yield c


def test_list_reports_presence(docs_client):
    j = docs_client.get("/api/docs/list").json()
    by = {r["name"]: r for r in j["roots"]}
    assert by["one"]["present"] is True and by["one"]["documents"] == ["GUIDE.md"]
    assert by["gone"]["present"] is False and by["gone"]["documents"] == []


def test_get_and_section(docs_client):
    j = docs_client.get("/api/docs/one/GUIDE.md").json()
    assert "# Guide" in j["content"]
    s = docs_client.get("/api/docs/one/GUIDE.md/section/setup").json()
    assert "Do this." in s["content"]


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc%2Fpasswd", "secret.txt", "GUIDE.md%2F..%2Fx.md", "a%00b.md"])
def test_rejects_escapes_and_non_markdown(docs_client, bad):
    assert docs_client.get(f"/api/docs/one/{bad}").status_code in (400, 404)


def test_unknown_root_404(docs_client):
    assert docs_client.get("/api/docs/nope/GUIDE.md").status_code == 404


def test_evidence_endpoints_gone(docs_client):
    assert docs_client.get("/api/docs/evidence/bootstrap/location").status_code in (400, 404)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_docs_roots.py -q --override-ini addopts=`
Expected: FAIL (old routes, `DOCS_DIR` relative to the repo).

- [ ] **Step 3: Rewrite the router**

Replace the top of `routers/docs.py` (the `DOCS_DIR` constant, `import subprocess`, `EVIDENCE_*` tables, `_fetch_evidence`, and the two evidence routes go away). Keep the existing markdown section parser if one exists in the file; otherwise use this:
```python
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from station_web.config import config

router = APIRouter(prefix="/api/docs", tags=["docs"])
_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.md$")


def _roots() -> dict[str, Path]:
    return {r["name"]: Path(r["path"]) for r in config.docs_roots}


def _resolve(root: str, doc_name: str) -> Path:
    roots = _roots()
    if root not in roots:
        raise HTTPException(404, f"unknown docs root {root!r}")
    if not _NAME.match(doc_name) or ".." in doc_name:
        raise HTTPException(400, "document names are <name>.md with no path separators")
    base = roots[root].resolve()
    p = (base / doc_name).resolve()
    if base not in p.parents or not p.is_file():
        raise HTTPException(404, f"{doc_name} not found under {root}")
    return p


@router.get("/list")
async def list_docs():
    out = []
    for r in config.docs_roots:
        base = Path(r["path"])
        present = base.is_dir()
        docs = sorted(p.name for p in base.glob("*.md")) if present else []
        out.append({"name": r["name"], "path": str(base), "present": present, "documents": docs})
    return {"roots": out}


@router.get("/{root}/{doc_name}")
async def get_doc(root: str, doc_name: str):
    p = _resolve(root, doc_name)
    return {"root": root, "name": doc_name, "content": p.read_text(errors="replace")}


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


@router.get("/{root}/{doc_name}/section/{section_id}")
async def get_section(root: str, doc_name: str, section_id: str):
    text = _resolve(root, doc_name).read_text(errors="replace")
    lines = text.splitlines()
    start = None
    level = 0
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m and _slug(m.group(2)) == section_id:
            start, level = i, len(m.group(1))
            break
    if start is None:
        raise HTTPException(404, f"section {section_id!r} not found")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s", lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return {"root": root, "name": doc_name, "section": section_id, "content": "\n".join(lines[start:end])}
```
Update `static/docs.html`: fetch `/api/docs/list`, render a root selector, and request `/api/docs/<root>/<name>`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest tests/ -q --override-ini addopts=`
Expected: all green, hygiene included: no `subprocess`, `requests`, or `hf_timestd` remains anywhere under `src/station_web`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: four configured roots, path-confined; evidence endpoints removed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 13: Vendor the front-end libraries; prune the navigation

**Files:**
- Create: `src/station_web/static/vendor/plotly-2.27.0.min.js`, `src/station_web/static/vendor/marked.min.js`, `src/station_web/static/vendor/VENDOR.md`
- Modify: every `src/station_web/static/*.html` that loads a CDN script; `index.html` navigation
- Test: `tests/test_static_assets.py`

- [ ] **Step 1: Write the failing test**

`tests/test_static_assets.py`:
```python
import hashlib
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "station_web" / "static"


def test_no_remote_scripts_or_styles():
    offenders = []
    for page in STATIC.glob("*.html"):
        for m in re.finditer(r'<(script|link)[^>]+(src|href)="(https?://[^"]+)"', page.read_text()):
            offenders.append(f"{page.name}: {m.group(3)}")
    assert not offenders, "\n".join(offenders)


def test_vendor_hashes_match_manifest():
    manifest = (STATIC / "vendor" / "VENDOR.md").read_text()
    for name in ("plotly-2.27.0.min.js", "marked.min.js"):
        p = STATIC / "vendor" / name
        assert p.exists(), name
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest in manifest, f"{name} sha256 {digest} not recorded in VENDOR.md"


def test_cut_pages_and_links_gone():
    assert not (STATIC / "logs.html").exists()
    assert not (STATIC / "timing-validation.html").exists()
    index = (STATIC / "index.html").read_text()
    assert "logs.html" not in index and "timing-validation.html" not in index
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_static_assets.py -q --override-ini addopts=`
Expected: FAIL on the CDN URLs and the missing vendor files.

- [ ] **Step 3: Fetch, pin, and rewrite**

```bash
cd /home/mjh/hamsci/repos/station-web/src/station_web/static
curl -fsSL -o vendor/plotly-2.27.0.min.js https://cdn.plot.ly/plotly-2.27.0.min.js
MARKED_VER=$(curl -fsSL https://registry.npmjs.org/marked/latest | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
curl -fsSL -o vendor/marked.min.js "https://cdn.jsdelivr.net/npm/marked@${MARKED_VER}/marked.min.js"
{
  echo "# Vendored front-end libraries"
  echo
  echo "| file | version | source | sha256 |"
  echo "|---|---|---|---|"
  echo "| plotly-2.27.0.min.js | 2.27.0 | https://cdn.plot.ly/plotly-2.27.0.min.js | $(sha256sum vendor/plotly-2.27.0.min.js | cut -d' ' -f1) |"
  echo "| marked.min.js | ${MARKED_VER} | https://cdn.jsdelivr.net/npm/marked@${MARKED_VER}/marked.min.js | $(sha256sum vendor/marked.min.js | cut -d' ' -f1) |"
  echo
  echo "Fetched $(date -u +%Y-%m-%dT%H:%MZ). Update by re-running the commands in the plan (Task 13) and refreshing this table."
} > vendor/VENDOR.md
sed -i 's|https://cdn.plot.ly/plotly-2.27.0.min.js|/static/vendor/plotly-2.27.0.min.js|g' *.html
sed -i 's|https://cdn.jsdelivr.net/npm/marked/marked.min.js|/static/vendor/marked.min.js|g' docs.html
grep -n "https\?://" *.html | grep -v "href=\"https://github.com\|href=\"https://pswsnetwork" || true
```
Then edit `index.html`: delete the nav anchors for `timing-validation.html` (line 22) and `logs.html` (line 33) and the "System Logs" card (lines 179–186). Check every other page's nav block for the same two links and remove them.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_static_assets.py tests/test_client_contract.py -q --override-ini addopts=`
Expected: pass; the contract validate check for vendored plotly now finds the file.

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/station-web
git add -A
git commit -m "static: vendor plotly 2.27.0 and marked with pinned hashes; drop logs and validation nav

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 14: README, full suite, first push

**Files:**
- Create: `README.md`
- Verify: whole suite green

- [ ] **Step 1: Write the README**

```markdown
# station-web

Data reports and documentation for a sigmond station. Reads the data
products hf-timestd, hamsci-physics and gnss-vtec write under
`/var/lib/timestd`, and serves them as pages and JSON on port 8000.

It reads. It never runs a shell, never reaches the network, never
touches the host beyond the products directory. Sigmond owns station
management; station-web shows the science.

Phase 5 of the 2026-08-10 hf-timestd split. Design:
`hf-timestd/docs/superpowers/specs/2026-09-06-station-web-extraction-design.md`.

## Operate

    smd install station-web            # clone, venv, units
    smd config init station-web        # /etc/station-web/station-web.toml from the env bag
    systemctl status station-web@default
    station-web validate --json        # contract self-check

Browse `http://<station>:8000/`. API docs at `/api/docs`.

## Develop

    uv sync --extra dev
    uv run pytest tests/ -q --override-ini addopts=
```

- [ ] **Step 2: Run the whole suite one last time**

Run: `uv run pytest tests/ -q --override-ini addopts=`
Expected: all green, zero xfail remaining, zero skips.

- [ ] **Step 3: Commit and create the staging repo**

```bash
git add README.md
git commit -m "docs: README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
gh repo create mijahauan/station-web --public --source=. --remote=origin --push --description "Station data reports and documentation for sigmond stations (hf-timestd split, Phase 5)"
git remote -v
```
Expected: `origin git@github.com:mijahauan/station-web.git` (SSH; if gh set an https URL, run `git remote set-url origin git@github.com:mijahauan/station-web.git`). The catalog entry in Task 15 names this URL.

---

## Part C — sigmond

### Task 15: Catalog entry and dasi2 profile membership

**Files:**
- Modify: `sigmond/etc/catalog.toml` (after `[client.hamsci-physics]`; dasi2 `clients` line)
- Modify: `sigmond/tests/test_catalog.py`

- [ ] **Step 1: Write the failing tests**

Append to `sigmond/tests/test_catalog.py` inside `TestLoadCatalog`:
```python
    def test_station_web_entry(self):
        entries = load_catalog(REPO_CATALOG)
        sw = entries['station-web']
        assert sw.kind == 'client'                      # 'server' means non-conformant here
        assert sw.contract == '0.8'
        assert sw.install_script is None or sw.install_script == ''
        assert sw.start_priority == 220                 # after hf-timestd (50) and hamsci-physics (60)
        assert set(sw.requires) == {'hf-timestd', 'hamsci-physics', 'hamsci-dsp'}
        assert 'ka9q-radio' not in sw.requires

    def test_station_web_in_dasi2_profile(self):
        import tomllib
        with open(REPO_CATALOG, 'rb') as fh:
            cat = tomllib.load(fh)
        clients = cat['profile']['dasi2']['clients']
        assert clients[-1] == 'station-web'
        assert clients.index('station-web') > clients.index('hamsci-physics')
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/sigmond && uv run pytest tests/test_catalog.py -q --override-ini addopts= -k station_web`
Expected: FAIL, `KeyError: 'station-web'`.

- [ ] **Step 3: Add the entry and the profile member**

Insert after the `[client.hamsci-physics]` block in `etc/catalog.toml`:
```toml
[client.station-web]
kind            = "client"
description     = "Station data reports and documentation (metrology, ionospheric science, GNSS vTEC); reads products under /var/lib/timestd"
repo            = "https://github.com/mijahauan/station-web"   # staging: transfers to HamSCI/
uses            = []
requires        = ["hf-timestd", "hamsci-physics", "hamsci-dsp"]  # reads what 50 and 60 write; gnss-vtec optional
contract        = "0.8"
install_script  = ""                                  # deploy.toml [build]/[install] drive it
start_priority  = 220
# Phase 5 of the 2026-08-10 hf-timestd split (spec: hf-timestd
# docs/superpowers/specs/2026-09-06-station-web-extraction-design.md).  The web
# UI that used to ship inside hf-timestd as timestd-web-api.service.  It is a
# §16.3.1 meta-client: no radiod, no data root of its own, no writes.  kind is
# "client" because in this catalog "server" has meant not-contract-conformant
# (see test_server_entry_has_no_contract_or_install_script).
```
Change the dasi2 `clients` line to:
```toml
clients            = ["hf-timestd", "wspr-recorder", "psk-recorder", "mag-recorder", "gmag-webui", "meteor-scatter", "hamsci-physics", "station-web"]
```
and add above it, after the hamsci-physics paragraph: `# station-web added 2026-09-06 (split Phase 5): it replaces the web UI hf-timestd used to carry, so a bringup without it leaves the station with no pages.`

- [ ] **Step 4: Run the sigmond catalog, topology, and profile tests**

Run: `uv run pytest tests/test_catalog.py tests/test_topology.py tests/test_site_profile.py tests/test_catalog_prune.py -q --override-ini addopts=`
Expected: all pass.

- [ ] **Step 5: Commit (do not push yet; Task 17 pushes after B4 verifies)**

```bash
git add etc/catalog.toml tests/test_catalog.py
git commit -m "catalog: station-web as a conformant meta-client (220); dasi2 profile member

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

## Part D — hf-timestd

### Task 16: Remove the web UI and its dependencies from the timing core

**Files:**
- Delete: `web-api/`, `systemd/timestd-web-api.service`, `scripts/deploy_web_ui.sh`, `src/hf_timestd/models/broadcast.py`, `src/hf_timestd/core/stability_analysis.py`, `tests/unit/test_stability_analysis.py`, `tests/unit/test_web_api_config_resolution.py`
- Modify: `deploy.toml` (lines 108–111 link step; line 165 units entry), `pyproject.toml` (lines 39–43), `src/hf_timestd/service_profile.py` (lines 38, 59, 70, 78, 90), `tests/unit/test_service_profile.py` (lines 84, 89, 120, 123, 218–219), `tests/unit/test_tid_l3_writer.py` (lines 30–45), `CLAUDE.md`, `docs/INDEX.md`, `docs/superpowers/plans/2026-08-10-hf-timestd-split.md` (Phase 5 checkboxes)

- [ ] **Step 1: Confirm nothing in `src/` imports the two modules that leave**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
grep -rn "models.broadcast\|stability_analysis" src scripts --include=*.py | grep -v "^src/hf_timestd/models/broadcast.py\|^src/hf_timestd/core/stability_analysis.py"
```
Expected: no output. If a hit appears, stop and report; the spec (§12) assumes none.

- [ ] **Step 2: Write the failing guard test**

`tests/unit/test_no_web_ui_in_core.py`:
```python
"""Phase 5 of the split: the web UI lives in station-web now."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_web_api_directory_gone():
    assert not (ROOT / "web-api").exists()


def test_no_fastapi_dependency():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    for banned in ("fastapi", "uvicorn", "jinja2", "python-multipart", "aiofiles"):
        assert not any(d.startswith(banned) for d in deps), banned


def test_manifest_no_longer_ships_the_web_unit():
    with open(ROOT / "deploy.toml", "rb") as fh:
        d = tomllib.load(fh)
    assert "timestd-web-api.service" not in d["systemd"]["units"]
    assert not any("web-api" in s.get("src", "") for s in d["install"]["steps"])
    assert not (ROOT / "systemd" / "timestd-web-api.service").exists()
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_no_web_ui_in_core.py -q --override-ini addopts=`
Expected: 3 FAIL.

- [ ] **Step 4: Remove**

```bash
git rm -r -q web-api systemd/timestd-web-api.service scripts/deploy_web_ui.sh \
  src/hf_timestd/models/broadcast.py src/hf_timestd/core/stability_analysis.py \
  tests/unit/test_stability_analysis.py tests/unit/test_web_api_config_resolution.py
# deploy.toml: drop the four-line link step for the web unit and the units entry
python3 - <<'EOF'
import re
p='deploy.toml'; s=open(p).read()
s=s.replace('[[install.steps]]\nkind = "link"\nsrc  = "systemd/timestd-web-api.service"\ndst  = "/etc/systemd/system/timestd-web-api.service"\n\n','')
s=s.replace('    "timestd-web-api.service",\n','')
open(p,'w').write(s)
p='pyproject.toml'; s=open(p).read()
for dep in ('fastapi>=0.104.0','uvicorn[standard]>=0.24.0','python-multipart>=0.0.6','aiofiles>=23.2.0','jinja2>=3.0.0'):
    s=re.sub(r'^\s*"%s",.*\n' % re.escape(dep), '', s, flags=re.M)
open(p,'w').write(s)
EOF
grep -n "web-api\|web_api\|fastapi\|uvicorn\|jinja2\|multipart\|aiofiles" deploy.toml pyproject.toml
```
Expected: the grep prints nothing.

`src/hf_timestd/service_profile.py`: delete the `'web_api': 'timestd-web-api.service',` mapping (line 38) and remove `'web_api'` from the three profile service lists (lines 59, 70, 78); change the `rtp` profile description (line 90) to `'Archive + monitoring — standard RTP/GPSDO mode'`. In `tests/unit/test_service_profile.py`, replace the `web_api` override in the fixtures with `radiod_monitor` (lines 84, 89, 120, 123) and change the summary assertions (218–219) to inspect `radiod_monitor`.

`tests/unit/test_tid_l3_writer.py`: the `_service` helper (lines 30–45) imports the web-api TID service. That test now belongs to station-web (`tests/test_routes.py` covers `/api/tid`). Delete `_service` and every test method that calls it; keep the writer-side tests.

- [ ] **Step 5: Docs**

- `CLAUDE.md`: in "Architecture Notes", add one bullet after the GRAPE/PSWS one: `- Web UI moved out: station-web (2026-09-06 split Phase 5) serves the pages from the same /var/lib/timestd products; this repo carries no FastAPI.`
- `docs/INDEX.md`: remove any entry pointing at `web-api/` docs (`web-api/README.md`, `DEPLOYMENT_GUIDE.md`, `PHASE1_COMPLETE.md`, `SOLAR_CORRELATION_README.md` left with the directory) and add a line under Operations pointing at the station-web repo.
- `docs/superpowers/plans/2026-08-10-hf-timestd-split.md`: tick the four Phase 5 checkboxes and add `(done 2026-09-06, see specs/2026-09-06-station-web-extraction-design.md)` after the heading.

- [ ] **Step 6: Run the full hf-timestd suite**

Run: `uv sync --extra dev --extra gnss --extra iono && uv run pytest tests/ -q --override-ini addopts=`
Expected: green. The prior baseline was 2397 passed, 3 skips (commit 1ed8b1a); expect fewer tests now, zero failures. If `uv sync` complains about a lock, run `uv lock` first and include `uv.lock` in the commit.

- [ ] **Step 7: Commit (do not push yet)**

```bash
git add -A
git commit -m "Phase 5: the web UI leaves for station-web

Deletes web-api/, timestd-web-api.service, deploy_web_ui.sh, models/broadcast.py
(now station_web.broadcast) and core/stability_analysis.py (now hamsci_dsp.stability).
Drops fastapi, uvicorn, jinja2, python-multipart, aiofiles. service_profile loses
the web_api toggle.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

## Part E — cutover

### Task 17: B4 cutover (Michael's hand) and the pushes

**Files:**
- Create: `~/hf-deploy-20260906-station-web.sh` (devbox; Michael runs it on the B4 VM via `!`)
- Create: one claude-bus message announcing the change

**Preconditions:** Tasks 1–16 committed. hamsci-dsp, station-web, sigmond, hf-timestd all still unpushed except station-web (Task 14). B4 VM reachable as `hamsci@192.168.1.176` via the PM (`root@192.168.1.244`).

- [ ] **Step 1: Push the four repos in dependency order**

```bash
cd /home/mjh/hamsci/repos/hamsci-dsp && git push origin main
cd ../station-web && git push origin main
cd ../sigmond && git push origin main
cd ../hf-timestd && git push origin main
```
Expected: four fast-forward pushes. hf-timestd's push carries the spec, the plan, and Task 16.

- [ ] **Step 2: Announce on the bus**

```bash
f=/srv/hamsci/claude-bus/$(date -u +%Y%m%dT%H%M%SZ)-mjh.md
sudo -n bash -c "cat > $f" <<'EOF'
# STATION-WEB CUTOVER ON B4 (mjh, Michael by hand, ~now): replacing timestd-web-api.service
(hf-timestd web UI, :8000) with station-web@default.service (new repo mijahauan/station-web,
same port). Sequence: smd install station-web -> stop/disable timestd-web-api -> ff hf-timestd
(web-api/ deleted) -> ff hamsci-dsp (0.7.0, stability module) -> start station-web. Touches NO
radiod, recorder, fusion, chrony or timing unit. Seconds of downtime on the monitoring page only.
Soak 24 h on B4, then ND. Spec: hf-timestd docs/superpowers/specs/2026-09-06-station-web-extraction-design.md
EOF
sudo -n chown mjh:hamsci $f && sudo -n chmod 660 $f
```

- [ ] **Step 3: Stage the deploy script**

`~/hf-deploy-20260906-station-web.sh`:
```bash
#!/usr/bin/env bash
# station-web cutover on one station VM.  Run as root (sudo) on the VM.
# Idempotent: every step checks before it acts.
set -euo pipefail
G=/opt/git/sigmond
log(){ printf '\n== %s  %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

log "1/7 hamsci-dsp -> 0.7.0 (stability module)"
git -C $G/hamsci-dsp pull --ff-only
grep -q '^version = "0.7.0"' $G/hamsci-dsp/pyproject.toml

log "2/7 sigmond catalog (station-web entry)"
git -C $G/sigmond pull --ff-only

log "3/7 smd install station-web (clone, venv, link units, enable)"
smd install station-web
test -x $G/station-web/venv/bin/station-web
smd config init station-web
station-web validate --json || echo "validate reported issues (expected before the old unit stops); continuing"

log "4/7 stop and disable the old web UI"
systemctl disable --now timestd-web-api.service || true

log "5/7 hf-timestd -> Phase 5 commit (web-api/ removed)"
git -C $G/hf-timestd pull --ff-only
test ! -d $G/hf-timestd/web-api
rm -f /etc/systemd/system/timestd-web-api.service
systemctl daemon-reload
# the core venv no longer needs fastapi; leave the venv alone (editable install, no rebuild needed)

log "6/7 start station-web"
systemctl enable --now station-web@default.service
sleep 3
systemctl is-active station-web@default.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/health/system | head -c 400; echo
for p in / /static/metrology.html /static/chrony.html /static/dtec.html /static/docs.html /api/metrology/fusion/latest /api/chrony/snapshot /api/docs/list; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8000$p"); printf '  %-40s %s\n' "$p" "$code"; done
test -S /run/station-web/control.sock

log "7/7 sigmond view"
smd admin diag drop-in station-web || true
smd status | grep -i "station-web\|web-api" || true
echo; echo "DONE. Watch: journalctl -u station-web@default -f"
```
```bash
chmod +x ~/hf-deploy-20260906-station-web.sh
scp ~/hf-deploy-20260906-station-web.sh hamsci@192.168.1.176:/tmp/   # via the PM if direct fails
```
Michael runs on the VM: `! ssh hamsci@192.168.1.176 sudo bash /tmp/hf-deploy-20260906-station-web.sh`

- [ ] **Step 4: Verify from the devbox after Michael reports back**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.176:8000/
curl -s http://192.168.1.176:8000/api/health/system | python3 -m json.tool | head -40
curl -s http://192.168.1.176:8000/api/chrony/snapshot | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["age_s"], d["stale"], [s["name"] for s in d["sources"]])'
```
Expected: 200; freshness table with `VALID` for `L2_timing_measurements`, `L3_fusion_timing`, `DIAG_chrony_stats`, `L3_physics`, `L3_dtec`; `L3_gnss_vtec` `INDETERMINATE` on B4 (no vTEC service configured there yet); chrony snapshot age under 120 s with FUSE listed. Any `INVALID` means a product stopped: check the writer's unit before blaming station-web.

- [ ] **Step 5: Record and soak**

Write a bus message with the verification results and the soak start time. Update the memory file `project_station_web_phase5.md` status. Soak 24 h; the check that closes the soak is Step 4 repeated plus `journalctl -u station-web@default --since -24h -p warning` showing no repeating warning.

### Task 18: ND cutover

- [ ] **Step 1: Preflight on ND** (via the PM's nested ssh; Michael runs): `ls -ld /var/lib/timestd/phase2 && stat -c '%U:%G %a' /var/lib/timestd/phase2/timestd.db` — the directory must be group `timestd` and group-writable, or `validate` will fail as designed. Fix perms first if not.
- [ ] **Step 2: Announce on the bus** (same text as Task 17 Step 2 with "ND" substituted).
- [ ] **Step 3: Run the same staged script on ND.** Same verification as Task 17 Step 4 against ND's address.
- [ ] **Step 4: Record on the bus; update memory; tick Phase 5 as deployed on both stations.**

### Task 19: Close-out

- [ ] Update `/home/mjh/.claude/projects/-home-mjh-hamsci/memory/project_station_web_phase5.md` status to deployed, with the commit SHAs of the five repos, and commit the hamsci-ops memory repo.
- [ ] File the sigmond issue proposing a `serves` block for HTTP-binding clients (spec §7), quoting station-web's inventory extension.
- [ ] Add the hf-timestd backlog issue "timing-validation page rebuilt from L2/L3 products" (decision 9).
- [ ] Run `graphify update /home/mjh/hamsci/repos` so the code graph knows the new repo and the removals.

---

## Self-review against the spec

- §1 remit and §9 posture: Task 6 hygiene tests (no `hf_timestd`, no `subprocess`, no HTTP clients, no CORS, no mkdir), Task 12 path confinement. ✔
- §4 layout: Tasks 2–7 create every listed file; `serve.py` is an addition the spec's layout did not name (it lists `cli.py serve`); the verb lives in `cli.py`, the uvicorn runner in `serve.py`. Acceptable; noted here.
- §5.1 router table: Task 7 moves the eleven "Move" rows and cuts the two live-model propagation endpoints; Task 9 chrony; Task 10 space weather; Task 11 health; Task 12 docs; logs and timing-validation never copied. ✔ `grape` minus `upload/` reads: Task 11 deletes `/api/health/grape`; check `services/grape_service.py` for `data_root/upload` reads during Task 7 Step 6 and drop them the same way.
- §5.2 modules: Task 1 (stability → dsp), Task 7 (broadcast → station-web, io/registry/wwv_constants rewrites). ✔
- §6 data access: reader through `hamsci_dsp.io` (Task 7), WAL group-write check in `validate` (Task 3), chrony from product (Task 9), space weather file (Task 10), docs roots (Task 12). ✔
- §7 contract: CLI and inventory (Task 3), unit with both EnvironmentFiles and hardening (Task 4), control socket (Task 5), log level and SIGHUP (Tasks 3 and 6). ✔
- §8 config: Task 2 template and settings, Task 4 setup script with `STATION_CALL` first and the hf-timestd `sqlite_path` seed. ✔
- §10 assets: Task 13. ✔
- §11 sigmond: Task 15. ✔
- §12 hf-timestd: Task 16. ✔
- §13 hamsci-dsp: Task 1. ✔
- §14 cutover and soak: Tasks 17–18. ✔
- §15 tests: contract (3), manifest (4), routes (7–8), hygiene (6), plus per-task tests. ✔
- §17 risks: WAL perms → Task 3 validate + Task 18 preflight; missed host-state dependency → hygiene; port in use → not implemented as a validate warning (the spec suggests it); add to Task 3 `collect_issues` if time allows, otherwise it is a documented gap.

Type consistency: `Settings` attribute names (`data_root`, `storage`, `station`, `station_metadata`, `web_bind`, `web_port`, `docs_roots`, `log_level`, `fusion_dir`, `loaded`, `config_path`) are used identically in Tasks 2, 3, 6, 7, 9–12. `STATUS` in `app.py` is read by Task 5's `status_fn` and written by Task 11. `discover_channels(settings)` (Task 7) is used in Task 11. `ChronyService(data_root, storage_config)` (Task 9) matches its router call.
