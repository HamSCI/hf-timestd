# tests/test_timing_core_stands_alone.py
"""hf-timestd installs and runs with no hamsci-physics on the host (decision 10)."""
import re, tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
IMPORT = re.compile(r"^\s*(?:from\s+hamsci_physics|import\s+hamsci_physics)", re.M)

def test_no_code_imports_hamsci_physics():
    hits = [str(p.relative_to(ROOT)) for base in ("src", "scripts")
            for p in (ROOT / base).rglob("*.py") if IMPORT.search(p.read_text())]
    assert hits == []

def test_pyproject_does_not_depend_on_hamsci_physics():
    d = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert not any(x.startswith("hamsci-physics") for x in d["project"]["dependencies"])
    assert "hamsci-physics" not in d["tool"]["uv"]["sources"]
    assert "gnss" not in d["project"]["optional-dependencies"]

def test_no_unit_runs_hamsci_physics_code():
    for u in (ROOT / "systemd").iterdir():
        assert "/opt/git/sigmond/hamsci-physics" not in u.read_text(), u.name

def test_vtec_left_with_its_unit():
    assert not (ROOT / "systemd" / "timestd-vtec.service").exists()
    assert "timestd-vtec" not in (ROOT / "deploy.toml").read_text()
    from hf_timestd.service_profile import SERVICE_UNIT_MAP
    assert "vtec" not in SERVICE_UNIT_MAP

def test_validate_names_the_moved_section():
    from hf_timestd.cli import retired_section_issues
    msg = retired_section_issues({"gnss_vtec": {"enabled": True}})[0]["message"]
    assert "hamsci-physics" in msg and "/etc/hamsci-physics/gnss-vtec.toml" in msg

def test_installer_and_scripts_forget_vtec():
    for rel in ("scripts/install.sh", "scripts/setup-station.sh", "scripts/start-services.sh",
                "scripts/stop-services.sh", "scripts/verify_pipeline.sh",
                "scripts/check-freshness-alert.sh"):
        text = (ROOT / rel).read_text()
        assert "timestd-vtec" not in text and "gnss_vtec" not in text, rel
    assert "--extra gnss" not in (ROOT / "scripts/install.sh").read_text()

def test_template_has_no_gnss_vtec_section():
    t = (ROOT / "config/timestd-config.toml.template").read_text()
    assert "[gnss_vtec]" not in t and "vtec" not in t.split("[services]")[1].split("[timing.t6_pps]")[0]
