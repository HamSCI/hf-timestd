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
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO / "scripts" / "deploy.sh"

# Every awk implementation available here.  mawk is the one that matters
# (Debian's default /usr/bin/awk) but pinning all of them is free.
AWKS = [a for a in ("mawk", "gawk", "awk", "busybox") if shutil.which(a)]


def list_units(awk: str, cwd: Path = REPO):
    """Run deploy.sh's unit reader with a specific awk on PATH.

    The shim goes in a TEMP dir, never in the repo: an earlier version
    created it at the repo root, where `git add -A` swept it into a
    commit and the machine-specific path inside it then broke `git pull`
    on a deployed host ("cannot create directory at '.awkshim'").
    """
    shim = Path(tempfile.mkdtemp(prefix="awkshim-"))
    # busybox is a multi-call binary: it needs the applet name.
    exe = shutil.which(awk)
    argv = f"{exe} awk" if awk == "busybox" else exe
    (shim / "awk").write_text(f'#!/bin/sh\nexec {argv} "$@"\n')
    (shim / "awk").chmod(0o755)
    env = {"PATH": f"{shim}:/usr/bin:/bin", "HOME": "/tmp"}
    try:
        return subprocess.run(
            ["bash", str(DEPLOY_SH), "--list-units"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
        )
    finally:
        # Every run used to leave its shim behind: 2,944 of them in the
        # devbox /tmp by 2026-10-28.
        shutil.rmtree(shim, ignore_errors=True)


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
def test_retired_units_are_never_listed_for_restart(awk):
    """v3.96: timestd-prune retired; sigmond stops it, deploy.sh must not restart it."""
    units = list_units(awk).stdout.split()

    assert not any("timestd-prune" in u for u in units)
    assert "timestd-core-recorder.service" in units


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


def _an_installed_unit(suffix):
    """Name a unit systemd really has, or skip.

    Hard-coding one (this file used apt-daily.timer and cron.service)
    tests the host's package list as much as the parser: cron is absent
    from a minimal Debian image, so the service case failed everywhere
    except a machine that happened to have it.  Ask systemd instead.
    """
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        pytest.skip("no systemctl on this host")
    out = subprocess.run(
        ["systemctl", "list-unit-files", "--no-pager", "--no-legend"],
        capture_output=True, text=True, timeout=30,
    )
    for line in out.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        # templates (foo@.service) and aliases are not instantiable units
        if name.endswith(suffix) and "@" not in name:
            return name
    pytest.skip(f"no installed {suffix} unit on this host")


def test_a_timer_is_recognised_as_installed():
    """`--type=service,target` EXCLUDES timers, so the guard exited 1 for
    every .timer and deploy.sh skipped all seven in deploy.toml as "not
    installed" — while they were installed and active.  A restart that
    silently covers only part of the manifest.
    """
    unit = _an_installed_unit(".timer")
    assert check_units([unit])[unit] == "installed"


def test_a_service_is_recognised_as_installed():
    unit = _an_installed_unit(".service")
    assert check_units([unit])[unit] == "installed"


def test_a_missing_unit_is_recognised_as_missing():
    assert check_units(
        ["definitely-not-a-real-unit.service"]
    )["definitely-not-a-real-unit.service"] == "missing"


def test_events_timer_is_listed_and_its_pass_skips_without_config():
    """Every station runs the event keeper's timer; a station with no
    events.toml must skip the pass, not log a failed unit every 5 minutes."""
    import tomllib
    units = tomllib.loads((REPO / "deploy.toml").read_text())["systemd"]["units"]
    assert "timestd-events.timer" in units
    service = (REPO / "systemd" / "timestd-events.service").read_text()
    assert "ConditionPathExists=/etc/hf-timestd/events.toml" in service


# ────────────────────────────────────────────────────────────────────
# v3.96 fix wave: deploy.sh enables the authority unit it starts
# ────────────────────────────────────────────────────────────────────

def _run_enable_hook(tmp_path, unit, enabled):
    """Extract enable_authority_if_needed from deploy.sh and run it against a
    systemctl shim that logs its calls."""
    text = DEPLOY_SH.read_text()
    body = text.split("# ENABLE_AUTHORITY_BEGIN", 1)[1].split("# ENABLE_AUTHORITY_END", 1)[0]
    shim = tmp_path / "shim"
    shim.mkdir(parents=True)
    log = tmp_path / "sc.log"
    (shim / "systemctl").write_text(
        '#!/bin/bash\necho "$*" >> "$SC_LOG"\n'
        f'[[ "$1" == "is-enabled" ]] && exit {0 if enabled else 1}\nexit 0\n')
    (shim / "systemctl").chmod(0o755)
    script = ('log_info(){ :; }; log_warn(){ :; }\n' + body +
              f'\nenable_authority_if_needed {unit}\n')
    subprocess.run(["bash", "-c", script],
                   env={"PATH": f"{shim}:/usr/bin:/bin", "SC_LOG": str(log)},
                   check=True, timeout=30)
    return log.read_text() if log.exists() else ""


def test_deploy_enables_a_started_but_disabled_authority(tmp_path):
    # Mutation check: drop the call after the restart, or the enable itself.
    out = _run_enable_hook(tmp_path, "timestd-authority-manager.service", enabled=False)
    assert "enable --no-reload timestd-authority-manager.service" in out


def test_deploy_leaves_an_enabled_authority_and_other_units_alone(tmp_path):
    assert "enable " not in _run_enable_hook(
        tmp_path / "a", "timestd-authority-manager.service", enabled=True)
    assert "enable " not in _run_enable_hook(
        tmp_path / "b", "timestd-fusion.service", enabled=False)


def test_deploy_calls_the_enable_hook_after_a_successful_restart():
    text = DEPLOY_SH.read_text()
    assert text.index("enable_authority_if_needed \"$unit\"") > text.index('systemctl restart "$unit"')
