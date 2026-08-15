"""deploy.sh must parse deploy.toml's [systemd] units under mawk, not just gawk.

AC0G-B4 2026-08-15: `sudo ./scripts/deploy.sh --pull --restart-recorder`
printed

    awk: line 6: syntax error at or near ,
    [WARN] deploy.toml lists no units to restart
    [WARN] core-recorder bounced — expect a few seconds of missing IQ

and exited 0 with a success summary.  It restarted NOTHING -- the
recorder's PID was unchanged -- while reporting a bounce.  The new code
was installed but not running, and the deploy looked clean.

Cause: the reader used `match($0, /re/, m)`, whose third capture-array
argument is a GNU awk extension.  Debian ships mawk as /usr/bin/awk, so
this failed on every production host while passing on any dev box with
gawk installed.

These tests run the parser under EVERY awk on the box, so a gawk-only
construct cannot come back unnoticed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO / "scripts" / "deploy.sh"

# Every awk implementation available here.  mawk is the one that matters
# (Debian's default /usr/bin/awk) but pinning all of them is free.
AWKS = [a for a in ("mawk", "gawk", "awk", "busybox") if shutil.which(a)]


def list_units(awk: str, cwd: Path = REPO):
    """Run deploy.sh's unit reader with a specific awk on PATH."""
    shim = cwd / ".awkshim"
    shim.mkdir(exist_ok=True)
    # busybox is a multi-call binary: it needs the applet name.
    exe = shutil.which(awk)
    argv = f"{exe} awk" if awk == "busybox" else exe
    (shim / "awk").write_text(f'#!/bin/sh\nexec {argv} "$@"\n')
    (shim / "awk").chmod(0o755)
    env = {"PATH": f"{shim}:/usr/bin:/bin", "HOME": "/tmp"}
    r = subprocess.run(
        ["bash", str(DEPLOY_SH), "--list-units"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    return r


@pytest.mark.parametrize("awk", AWKS)
def test_units_parse_under_every_awk(awk):
    r = list_units(awk)

    assert r.returncode == 0, f"{awk}: exit {r.returncode}\n{r.stderr}"
    units = [u for u in r.stdout.split() if u]
    # The real manifest lists the recorder plus services and timers.
    assert "timestd-core-recorder.service" in units
    assert "timestd-metrology.target" in units
    assert any(u.endswith(".timer") for u in units)
    # Commented-out entries inside the array must NOT be picked up.
    assert not any("grape-upload-retry" in u for u in units)
    assert not any("hfps-watchdog" in u for u in units)


@pytest.mark.parametrize("awk", AWKS)
def test_every_awk_agrees_on_the_unit_list(awk):
    """A parser that works but disagrees per-host is no better."""
    reference = list_units(AWKS[0]).stdout.split()

    assert list_units(awk).stdout.split() == reference


@pytest.mark.parametrize("awk", AWKS)
def test_awk_emits_no_errors(awk):
    """The B4 failure announced itself on stderr and was ignored because
    the script carried on and exited 0."""
    r = list_units(awk)

    assert "syntax error" not in r.stderr


def test_a_manifest_with_no_units_is_an_error_not_a_warning(tmp_path):
    """The B4 failure was survivable only because an empty unit list was
    a WARNING and the script carried on to a success summary.  A deploy
    that restarts nothing must not look like a deploy that worked."""
    toml = tmp_path / "deploy.toml"
    toml.write_text('[package]\nname = "hf-timestd"\n')
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "awk").write_text(f'#!/bin/sh\nexec {shutil.which("mawk")} "$@"\n')
    (shim / "awk").chmod(0o755)

    r = subprocess.run(
        ["bash", str(DEPLOY_SH), "--list-units"],
        cwd=REPO, env={"PATH": f"{shim}:/usr/bin:/bin", "HOME": "/tmp",
                       "DEPLOY_TOML": str(toml)},
        capture_output=True, text=True, timeout=60,
    )

    assert r.returncode != 0
    assert "no units" in r.stderr.lower()


# ────────────────────────────────────────────────────────────────────
# The installed-unit guard
# ────────────────────────────────────────────────────────────────────

def check_units(units, cwd=REPO):
    """Run deploy.sh's installed-unit check over the given unit names."""
    r = subprocess.run(
        ["bash", str(DEPLOY_SH), "--check-units", *units],
        cwd=cwd, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        capture_output=True, text=True, timeout=60,
    )
    return dict(line.split() for line in r.stdout.split("\n") if line.strip())


def test_a_timer_is_recognised_as_installed():
    """`--type=service,target` EXCLUDES timers, so the guard exited 1 for
    every .timer and deploy.sh skipped all seven in deploy.toml as "not
    installed" — while they were installed and active.  A restart that
    silently covers only part of the manifest.
    """
    assert check_units(["apt-daily.timer"])["apt-daily.timer"] == "installed"


def test_a_service_is_recognised_as_installed():
    assert check_units(["cron.service"])["cron.service"] == "installed"


def test_a_missing_unit_is_recognised_as_missing():
    assert check_units(
        ["definitely-not-a-real-unit.service"]
    )["definitely-not-a-real-unit.service"] == "missing"
