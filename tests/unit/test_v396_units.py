"""v3.96 Stage 1 (Plan D, Task 3): the authority manager runs as a unit of
its own, timestd-authority-manager.service, and fusion no longer hosts it.

Clients read /run/hf-timestd/authority.json.  While the manager ran inside
fusion, a fusion crash, or l2-calibration failing beneath fusion's
Requires=, took the station's only timing carrier down with it (audit C1).
"""
from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_UNIT = "timestd-authority-manager.service"


def _unit(name: str) -> str:
    return (ROOT / "systemd" / name).read_text()


def _directives(text: str) -> dict[str, list[str]]:
    """Each systemd directive's values, comments dropped, continuations joined.

    Keys read ``Section.Key``.  A comment that mentions ``Requires=`` never
    counts as a directive."""
    out: dict[str, list[str]] = {}
    section = ""
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line[0] in "#;"):
            continue
        line = pending + line
        if line.endswith("\\"):
            pending = line[:-1].rstrip() + " "
            continue
        pending = ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out.setdefault(f"{section}.{key.strip()}", []).append(value.strip())
    return out


def _one(d: dict[str, list[str]], key: str) -> str:
    assert key in d, f"{key} missing from {AUTHORITY_UNIT}"
    assert len(d[key]) == 1, f"{key} set more than once: {d[key]}"
    return d[key][0]


# ── The unit ──────────────────────────────────────────────────────────

def test_authority_unit_runs_the_authority_service():
    d = _directives(_unit(AUTHORITY_UNIT))
    exec_start = " ".join(_one(d, "Service.ExecStart").split())
    assert exec_start == (
        "/opt/git/sigmond/hf-timestd/venv/bin/python -m "
        "hf_timestd.core.authority_service "
        "--config /etc/hf-timestd/timestd-config.toml --log-level INFO")


@pytest.mark.parametrize("key,value", [
    ("Service.Type", "notify"),
    ("Service.WatchdogSec", "120"),
    ("Service.TimeoutStartSec", "120"),
    ("Service.Restart", "always"),
    ("Service.RestartSec", "5"),
    ("Unit.StartLimitIntervalSec", "300"),
    ("Unit.StartLimitBurst", "5"),
    ("Service.User", "timestd"),
    ("Service.Group", "timestd"),
    ("Service.RuntimeDirectory", "hf-timestd"),
    ("Service.RuntimeDirectoryMode", "0755"),
    ("Service.RuntimeDirectoryPreserve", "yes"),
    ("Service.EnvironmentFile", "-/etc/sigmond/coordination.env"),
    ("Service.MemoryMax", "300M"),
    ("Service.MemoryHigh", "200M"),
    ("Service.MemorySwapMax", "0"),
    ("Service.SyslogIdentifier", "timestd-authority-manager"),
    ("Unit.OnFailure", "timestd-alert@%N.service"),
    ("Unit.After", "timestd-core-recorder.service chrony.service"),
    ("Install.WantedBy", "multi-user.target"),
    ("Service.RestartPreventExitStatus", "78"),
])
def test_authority_unit_directive(key, value):
    assert _one(_directives(_unit(AUTHORITY_UNIT)), key) == value


def test_authority_unit_binds_to_no_other_unit():
    """The authority must run, and say "T6 unavailable", while the recorder
    stands down.  Line-anchored: a comment naming Requires= does not count."""
    text = _unit(AUTHORITY_UNIT)
    for dep in ("Requires", "Wants", "BindsTo", "Requisite", "PartOf"):
        assert not re.search(rf"^\s*{dep}\s*=", text, re.MULTILINE), dep


def test_directive_reader_ignores_comments():
    d = _directives("[Unit]\n# Requires=foo.service\nAfter=a \\\n  b\n")
    assert "Unit.Requires" not in d
    assert d["Unit.After"] == ["a b"]


# ── Fusion no longer hosts the authority ──────────────────────────────

def test_fusion_no_longer_builds_the_authority():
    from hf_timestd.core import multi_broadcast_fusion as m
    src = inspect.getsource(m.run_fusion_service)
    assert "build_authority_runner_from_config" not in src
    assert "publish_chains" not in src
    assert "authority_runner" not in src


def test_fusion_still_resolves_the_anchor_closure():
    """The closure feeds fusion's label-plane key; it stays."""
    from hf_timestd.core import multi_broadcast_fusion as m
    src = inspect.getsource(m.run_fusion_service)
    assert "anchor_closure_enabled(_auth_config)" in src


# ── deploy.toml ───────────────────────────────────────────────────────

def _deploy() -> dict:
    return tomllib.loads((ROOT / "deploy.toml").read_text())


def test_deploy_links_the_authority_unit():
    links = {s["dst"]: s["src"] for s in _deploy()["install"]["steps"]
             if s.get("kind") == "link"}
    assert links.get(f"/etc/systemd/system/{AUTHORITY_UNIT}") == \
        f"systemd/{AUTHORITY_UNIT}"


def test_deploy_lists_the_authority_after_fusion():
    """Ruling D2: deploy.sh restarts in list order, so the new fusion (no
    authority) restarts before the authority starts.  sigmond's align uses
    one systemctl transaction instead, where order has no effect."""
    units = _deploy()["systemd"]["units"]
    assert units.index("timestd-fusion.service") < units.index(AUTHORITY_UNIT)


def test_deploy_sh_reads_the_same_units_as_toml():
    """deploy.sh's awk reader ends the array at the first `]`, comments
    included; it must see the authority unit, in TOML's order."""
    import subprocess
    r = subprocess.run(["bash", str(ROOT / "scripts" / "deploy.sh"),
                        "--list-units"], capture_output=True, text=True,
                       cwd=ROOT, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == _deploy()["systemd"]["units"]


def test_authority_unit_is_a_plain_unit_not_a_move():
    """A move stops and disables its `from` units: it would retire fusion."""
    systemd = _deploy()["systemd"]
    assert AUTHORITY_UNIT not in systemd.get("retired_units", [])
    for move in systemd.get("moves", []):
        assert move["to"] != AUTHORITY_UNIT
        assert "timestd-fusion.service" not in move["from"]


# ── Every script's unit list carries every long-running unit (ruling D4) ──

def _long_running_units() -> list[str]:
    """deploy.toml's services and targets.  Timers stay out: the scripts
    manage timers in lists of their own, and sigmond starts them."""
    return [u for u in _deploy()["systemd"]["units"]
            if u.endswith((".service", ".target"))]


def _script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text()


def _block(text: str, pattern: str) -> str:
    if pattern == START_SERVICE_LINES:
        return "\n".join(re.findall(r'^start_service "[^"]+"', text,
                                     re.MULTILINE))
    m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert m, f"pattern not found: {pattern}"
    return m.group(1)


def _names(block: str) -> set[str]:
    """Every unit name in a block, as the block spells it."""
    return set(re.findall(r"timestd-[a-z0-9.@-]+", block))


START_SERVICE_LINES = "<lines that call start_service>"

# Each list, and the units it leaves out on purpose, with the reason.
SCRIPT_LISTS = {
    "install.sh CORE_UNITS": (
        "install.sh", r"^CORE_UNITS=\((.*?)\)",
        {"timestd-metrology.target":
             "copied just below with the metrology template"}),
    "install.sh --restart-all try-restart": (
        "install.sh", r"for service in \\\n(.*?); do",
        {"timestd-core-recorder.service":
             "restarted on its own just below the loop",
         "timestd-metrology.target":
             "its instances restart one by one above the loop"}),
    "install.sh Phase 8 verify": (
        "install.sh", r"^    for svc in (timestd-core-recorder.*?); do",
        {"timestd-radiod-monitor.service":
             "a hardware monitor, outside the timing chain; left out "
             "before v3.96"}),
    "start-services.sh fallback": (
        "start-services.sh", r"for u in \[(.*?)\]:", {}),
    "start-services.sh STARTUP_ORDER": (
        "start-services.sh", r"^STARTUP_ORDER=\((.*?)\)", {}),
    "start-services.sh start_service calls": (
        "start-services.sh", START_SERVICE_LINES, {}),
    "stop-services.sh SERVICES": (
        "stop-services.sh", r"^SERVICES=\((.*?)\)", {}),
    "uninstall.sh SERVICES": (
        "uninstall.sh", r"^SERVICES=\((.*?)\n\)", {}),
    "uninstall.sh SERVICE_FILES": (
        "uninstall.sh", r"^SERVICE_FILES=\((.*?)\n\)", {}),
}


@pytest.mark.parametrize("label", sorted(SCRIPT_LISTS))
def test_every_deploy_unit_is_in_each_script_list(label):
    script, pattern, exempt = SCRIPT_LISTS[label]
    units = _long_running_units()
    assert set(exempt) <= set(units), f"stale exemption in {label}"
    names = _names(_block(_script(script), pattern))
    missing = [u for u in units if u not in exempt
               and u not in names and u.removesuffix(".service") not in names]
    assert not missing, f"{label} lacks {missing}"


def test_script_lists_put_the_authority_after_fusion():
    """The same order as deploy.toml (ruling D2); stop runs in reverse."""
    for label in ("install.sh --restart-all try-restart",
                  "start-services.sh STARTUP_ORDER",
                  "start-services.sh start_service calls"):
        script, pattern, _ = SCRIPT_LISTS[label]
        block = _block(_script(script), pattern)
        assert block.index("timestd-fusion") < \
            block.index("timestd-authority-manager"), label
    block = _block(_script("stop-services.sh"), SCRIPT_LISTS[
        "stop-services.sh SERVICES"][1])
    assert block.index("timestd-authority-manager") < \
        block.index("timestd-fusion")
