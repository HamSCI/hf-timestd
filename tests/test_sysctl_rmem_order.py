"""hf-timestd's UDP-buffer sysctl file must sort BEFORE sigmond's.

sysctl.d applies files in name order and the last value wins.  As
99-timestd.conf (rmem_max 16 MiB) it sorted after sigmond's
99-sigmond-multicast.conf (64 MiB) and capped every socket on the station at
16 MiB (KFS-MAIN, 2026-11-28).  As 60-timestd.conf it still sets 16 MiB on a
standalone hf-timestd host and yields to sigmond's ceiling where sigmond runs."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _block() -> str:
    src = (REPO / "scripts/install.sh").read_text()
    m = re.search(r"# ── UDP receive buffers ──\n(.*?\nsysctl --system[^\n]*\n)", src, re.S)
    assert m, "UDP receive buffers block not found"
    return m.group(1)


def _run(sysctl_d: Path):
    script = ("set -e\nlog_info(){ :; }\nsysctl(){ :; }\n"
              f'SYSCTL_D="{sysctl_d}"\n' + _block())
    subprocess.run(["bash", "-c", script], check=True)


def test_fresh_install_writes_a_file_that_sorts_before_sigmonds(tmp_path):
    _run(tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["60-timestd.conf"]
    assert sorted(names + ["99-sigmond-multicast.conf"])[-1] == "99-sigmond-multicast.conf"
    assert "net.core.rmem_max = 16777216" in (tmp_path / "60-timestd.conf").read_text()


def test_an_old_99_file_is_moved_not_left_to_win(tmp_path):
    (tmp_path / "99-timestd.conf").write_text("net.core.rmem_max = 16777216\n")
    _run(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["60-timestd.conf"]


def test_an_existing_60_file_is_left_alone(tmp_path):
    (tmp_path / "60-timestd.conf").write_text("# operator edit\n")
    _run(tmp_path)
    assert (tmp_path / "60-timestd.conf").read_text() == "# operator edit\n"


def test_uninstall_removes_both_names():
    src = (REPO / "scripts/uninstall.sh").read_text()
    assert "60-timestd.conf" in src and "99-timestd.conf" in src
