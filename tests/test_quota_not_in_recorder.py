"""v3.96: the recorder deletes nothing.  The sigmond storage ledger owns retention,
and the prune timer retires with it."""
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_recorder_builds_no_quota_manager():
    src = (REPO / "src/hf_timestd/core/core_recorder_v2.py").read_text()
    assert "QuotaManager" not in src and "_enforce_quota" not in src


def test_retired_units_lands_in_the_systemd_table_and_names_prune():
    d = tomllib.loads((REPO / "deploy.toml").read_text())
    systemd = d["systemd"]
    assert "retired_units" in systemd
    assert {"timestd-prune.service", "timestd-prune.timer"} <= set(systemd["retired_units"])
    assert "timestd-prune.timer" not in systemd["units"]
    for move in systemd.get("moves", []):
        assert "retired_units" not in move


def test_the_prune_units_and_scripts_are_gone():
    for rel in ("systemd/timestd-prune.service", "systemd/timestd-prune.timer",
                "scripts/deploy-prune.sh", "scripts/prune-old-data.sh"):
        assert not (REPO / rel).exists(), rel


def test_nothing_that_ships_names_the_prune_unit():
    names = []
    for rel in ("scripts/install.sh", "scripts/start-services.sh",
                "src/hf_timestd/service_profile.py", "src/hf_timestd/events/keeper.py",
                "config/events.toml.template"):
        if re.search(r"timestd-prune", (REPO / rel).read_text()):
            names.append(rel)
    assert names == []


def test_storage_quota_is_not_in_the_config_template():
    assert "storage_quota" not in (REPO / "config/timestd-config.toml.template").read_text()
