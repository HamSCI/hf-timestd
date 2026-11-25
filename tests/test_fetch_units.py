import tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def _unit(name): return (ROOT / "systemd" / name).read_text()

def test_ionex_unit_reads_hf_timestds_credential_and_script():
    u = _unit("timestd-ionex-download.service")
    assert "Environment=NETRC=/etc/hf-timestd/earthdata-netrc" in u
    assert "ExecStart=/opt/git/sigmond/hf-timestd/scripts/download_ionex_daily.sh" in u
    assert "User=timestd" in u and "hamsci-physics" not in u

def test_iri_unit_runs_as_root_and_writes_only_pharlaps_directory():
    u = _unit("timestd-iri-update.service")
    assert "User=root" in u and "ReadWritePaths=-/opt/pharlap_4.7.4/dat/iri2020" in u
    assert "ExecStart=/opt/git/sigmond/hf-timestd/scripts/update-iri-indices.sh" in u
    # exactly PHaRLAP's directory: the venvs (and the iri2020 package copy) live under /opt/git/sigmond
    assert [l for l in u.splitlines() if l.startswith("ReadWritePaths=")] == \
        ["ReadWritePaths=-/opt/pharlap_4.7.4/dat/iri2020"]

def test_timers_keep_their_schedules():
    assert "OnCalendar=*-*-* 03:00:00" in _unit("timestd-ionex-download.timer")
    assert "OnCalendar=*-*-* 04:30:00" in _unit("timestd-iri-update.timer")

def test_deploy_lists_the_timers_and_their_moves():
    sd = tomllib.loads((ROOT / "deploy.toml").read_text())["systemd"]
    assert {"timestd-ionex-download.timer", "timestd-iri-update.timer"} <= set(sd["units"])
    assert [m["to"] for m in sd["moves"]] == ["timestd-ionex-download.timer", "timestd-iri-update.timer"]

def test_installer_copies_the_fetch_units():
    s = (ROOT / "scripts" / "install.sh").read_text()
    for f in ("timestd-ionex-download.service", "timestd-ionex-download.timer",
              "timestd-iri-update.service", "timestd-iri-update.timer"):
        assert f in s, f
