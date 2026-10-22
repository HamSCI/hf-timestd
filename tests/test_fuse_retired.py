"""The FUSE chrony refclock is retired (review section 15, step 4).

Michael's ruling of 2026-10-22 (docs/design/review-fusion-kalman-20261022.md,
section 15, ruling 5): stop the legacy FUSE write, and close its two install
gaps.  Fusion fed chrony's SHM 1 the calibrated D_clock, which the
calibration pulls to zero, so FUSE told chrony "the host is right" whatever
the host did.  `noselect` withheld the vote, but two install paths could
bring back a line without it:

  * install.sh left any FUSE line in chrony.conf alone on a host without
    /etc/chrony/conf.d;
  * systemd/chronyd-timestd-shm.conf told operators to add FUSE without
    `noselect`.

This module holds the retirement in place: fusion writes no SHM segment,
the shipped config declares no FUSE, the installer removes an old FUSE
line wherever it finds one, and the readers of FUSE (chrony_stats, the
authority manager's self-feedback check, the reach monitor) take its
absence quietly.  HPPS (SHM 2, T6) stays exactly as shipped.
"""

import ast
import inspect
import logging
import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

from hf_timestd.core import chrony_stats
from hf_timestd.core import multi_broadcast_fusion as mbf
from hf_timestd.core.authority_manager import AuthorityManager

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "config" / "chrony-timestd-refclocks.conf"
HELPER = REPO / "scripts" / "lib-chrony-refclocks.sh"
INSTALL_SH = REPO / "scripts" / "install.sh"
REACH_SH = REPO / "scripts" / "check-chrony-reach.sh"

# HPPS exactly as it shipped before the retirement.  Step 4 must not touch it.
HPPS_LINE = ("refclock SHM 2 refid HPPS poll 0 precision 1e-3 offset 0.0 "
             "delay 0.002 noselect")
OLD_FUSE_LINE = ("refclock SHM 1 refid FUSE poll 4 precision 1e-4 offset 0.0 "
                 "delay 0.001")
FUSE_RE = re.compile(r"^\s*refclock\s.*refid\s+FUSE(\s|$)", re.M)


# ── fusion writes no chrony SHM ─────────────────────────────────────────


def _service_tree():
    return ast.parse(textwrap.dedent(inspect.getsource(mbf.run_fusion_service)))


def test_fusion_service_opens_no_chrony_shm():
    """No ChronySHM in run_fusion_service: no unit 1, no write."""
    names = {n.id for n in ast.walk(_service_tree()) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(_service_tree())
             if isinstance(n, ast.Attribute)}
    assert "ChronySHM" not in names | attrs
    assert "reference_time_l2" not in names


def test_fusion_status_names_the_retired_regime(tmp_path):
    """fusion_status.json says FUSE is retired rather than reporting a
    legacy regime that no longer feeds anything."""
    from hf_timestd.core import fusion_status_writer as fsw

    calls = [n for n in ast.walk(_service_tree())
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "update"
             and any(k.arg == "fuse_retired" for k in n.keywords)]
    assert len(calls) == 1
    kw = next(k for k in calls[0].keywords if k.arg == "fuse_retired")
    assert isinstance(kw.value, ast.Constant) and kw.value.value is True

    path = tmp_path / "fusion_status.json"
    fsw.FusionStatusWriter(path, cycle_interval_sec=60.0).update(
        None, chrony_fed=False, skip_reasons=[], fuse_retired=True)
    assert fsw.read_feed_regime(path) == fsw.RETIRED_REGIME


# ── the shipped config ──────────────────────────────────────────────────


def _refclocks(text):
    out = {}
    for raw in text.splitlines():
        raw = raw.strip()
        if raw.startswith("refclock "):
            m = re.search(r"refid\s+(\S+)", raw)
            if m:
                out[m.group(1)] = raw
    return out


def test_shipped_config_declares_hpps_only_and_unchanged():
    lines = _refclocks(CONF.read_text())
    assert set(lines) == {"HPPS"}
    assert lines["HPPS"] == HPPS_LINE


def test_no_shipped_file_tells_an_operator_to_add_fuse():
    """chronyd-timestd-shm.conf:31 used to say "add FUSE" without noselect.
    No config or unit file may carry a FUSE refclock line, even quoted."""
    offenders = []
    for d in ("config", "systemd"):
        for f in sorted((REPO / d).rglob("*")):
            if not f.is_file():
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if re.search(r"refclock\s+SHM\s+1\b|refid\s+FUSE", line):
                    offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert offenders == []


# ── the installer ───────────────────────────────────────────────────────


def _run_helper(chrony_conf, conf_d, src=CONF):
    # install.sh runs under `set -euo pipefail`; so does this.
    script = (f'set -euo pipefail; source "{HELPER}"; '
              f'timestd_install_chrony_refclocks "{chrony_conf}" "{conf_d}" "{src}"')
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, check=True)


def test_install_removes_an_old_fuse_line_without_conf_d(tmp_path):
    """The install.sh:921 gap: no conf.d, and chrony.conf carries an old
    FUSE line without noselect.  The installer must REMOVE it."""
    conf = tmp_path / "chrony.conf"
    conf.write_text(f"pool 2.debian.pool.ntp.org iburst\n{OLD_FUSE_LINE}\n"
                    f"{HPPS_LINE}\n")
    out = _run_helper(conf, tmp_path / "no-conf.d")
    text = conf.read_text()
    assert not FUSE_RE.search(text)
    assert HPPS_LINE in text
    assert "pool 2.debian.pool.ntp.org iburst" in text
    # HPPS is already declared inline, so no include duplicates it.
    assert "include" not in text
    backups = list(tmp_path.glob("chrony.conf.bak-fuse-retired-*"))
    assert len(backups) == 1 and OLD_FUSE_LINE in backups[0].read_text()
    assert "Removed the retired FUSE refclock" in out.stdout + out.stderr


def test_install_removes_exactly_the_live_fuse_lines(tmp_path):
    """The regex's edges (review of T3 steps 2-5, c): a loosened pattern
    must fail here.  It removes a live FUSE line however indented, and
    leaves a commented one, refid FUSEX, and gpsd's SHM lines alone."""
    keep = [
        "refclock SHM 0 refid LG29 poll 2 precision 1e-1 offset 0.0 delay 0.2",
        "refclock SHM 1 refid PPS prefer",
        "refclock SOCK /run/chrony.ttyS0.sock refid PPS2",
        f"# {OLD_FUSE_LINE}",
        "#refclock SHM 1 refid FUSE noselect",
        "refclock SHM 4 refid FUSEX poll 4",
        "server time.example.org iburst  # refid FUSE in a comment",
        HPPS_LINE,
    ]
    drop = [
        OLD_FUSE_LINE,
        f"\t{OLD_FUSE_LINE} noselect",
        "   refclock SHM 1 refid FUSE",
        "refclock SHM 1 poll 4 refid  FUSE\tnoselect",
    ]
    conf = tmp_path / "chrony.conf"
    conf.write_text("\n".join(keep[:4] + drop[:2] + keep[4:] + drop[2:]) + "\n")
    _run_helper(conf, tmp_path / "no-conf.d")
    assert conf.read_text().splitlines() == keep


def test_install_includes_the_shipped_file_once_without_conf_d(tmp_path):
    conf = tmp_path / "chrony.conf"
    conf.write_text("pool 2.debian.pool.ntp.org iburst\n")
    _run_helper(conf, tmp_path / "no-conf.d")
    _run_helper(conf, tmp_path / "no-conf.d")
    assert conf.read_text().count(f"include {CONF}") == 1


def test_install_strips_fuse_from_every_drop_in(tmp_path):
    conf = tmp_path / "chrony.conf"
    conf.write_text(f"confdir {tmp_path / 'conf.d'}\n{OLD_FUSE_LINE} noselect\n")
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "chrony-timestd-refclocks.conf").write_text(
        f"{OLD_FUSE_LINE} noselect\n{HPPS_LINE}\n")
    (conf_d / "timestd-refclocks.conf").write_text(
        f"{OLD_FUSE_LINE} noselect\n{HPPS_LINE}\n")
    (conf_d / "operator.conf").write_text(f"# mine\n{OLD_FUSE_LINE}\n")
    _run_helper(conf, conf_d)

    live = sorted(p.name for p in conf_d.glob("*.conf"))
    assert live == ["operator.conf", "timestd-refclocks.conf"]
    for p in conf_d.glob("*.conf"):
        assert not FUSE_RE.search(p.read_text()), p
    assert (conf_d / "timestd-refclocks.conf").read_text() == CONF.read_text()
    assert (conf_d / "operator.conf").read_text() == "# mine\n"
    assert not FUSE_RE.search(conf.read_text())
    # Backups keep the old text and never end in .conf.
    assert list(conf_d.glob("operator.conf.bak-fuse-retired-*"))


def test_install_survives_a_conf_d_with_no_hpps(tmp_path):
    """grep finds nothing to count: under pipefail that must warn, not
    abort the whole install."""
    src = tmp_path / "empty-refclocks.conf"
    src.write_text("# nothing\n")
    conf = tmp_path / "chrony.conf"
    conf.write_text("pool x iburst\n")
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    out = _run_helper(conf, conf_d, src=src)
    assert "refid HPPS in 0 files" in out.stdout + out.stderr


def test_install_sh_runs_the_helper_and_not_the_old_branch():
    src = INSTALL_SH.read_text()
    assert "lib-chrony-refclocks.sh" in src
    assert "timestd_install_chrony_refclocks" in src
    assert "elif ! grep -q 'refid FUSE'" not in src


# ── the readers of FUSE ─────────────────────────────────────────────────


def test_authority_self_feedback_no_longer_watches_fuse(monkeypatch):
    """T3 feeds no refclock now, so a chrony without FUSE is not a flag."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="#,?,HPPS,0,0,377,1,0,0,0\n"))
    mgr = object.__new__(AuthorityManager)
    assert mgr._check_chrony_self_feedback("T3", conf_paths=[]) is None


def _snapshot(names):
    return chrony_stats.ChronySnapshot(
        timestamp_utc="2026-10-22T00:00:00Z", unix_time=0.0, tracking=None,
        sources=[chrony_stats.ChronySource(
            name=n, mode="#", state="?", stratum=0, poll=4, reach=0,
            last_rx=None, offset_us=0.0, error_us=0.0) for n in names])


def _collect(monkeypatch, caplog, sequence):
    snaps = iter(sequence)
    monkeypatch.setattr(chrony_stats, "collect_chrony_snapshot",
                        lambda: _snapshot(next(snaps)))
    col = chrony_stats.ChronyStatsCollector(interval_sec=0.0)
    caplog.set_level(logging.INFO, logger=chrony_stats.logger.name)
    for _ in sequence:
        col.collect(force=True)
    return [r for r in caplog.records if "FUSE" in r.getMessage()
            and "[CHRONY]" not in r.getMessage()]


def test_chrony_stats_notes_fuse_absence_once(monkeypatch, caplog):
    recs = _collect(monkeypatch, caplog, [["HPPS"]] * 5)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    assert "retired" in recs[0].getMessage()


def test_chrony_stats_warns_once_while_fuse_lingers(monkeypatch, caplog):
    """A station whose chronyd still runs a FUSE source (config not yet
    converged, or chrony not restarted) hears it once, then once more
    when it goes."""
    recs = _collect(monkeypatch, caplog,
                    [["FUSE", "HPPS"]] * 4 + [["HPPS"]] * 3)
    assert [r.levelno for r in recs] == [logging.WARNING, logging.INFO]


# ── the reach monitor ───────────────────────────────────────────────────


def _fake_bin(tmp_path, sources):
    b = tmp_path / "bin"
    b.mkdir()
    stubs = {
        "chronyc": f"cat <<'EOF'\n{sources}EOF\n",
        "systemctl": 'case "$*" in *chrony*) exit 0;; *) exit 3;; esac\n',
        "ipcs": "echo '0x4e545032 1 root 666 96 1'\n",
        "journalctl": "exit 0\n",
    }
    for name, body in stubs.items():
        p = b / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return b


def _reach(tmp_path, sources):
    env = dict(os.environ, PATH=f"{_fake_bin(tmp_path, sources)}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(REACH_SH), "--threshold", "64"],
                          capture_output=True, text=True, env=env)


HDR = "MS Name/IP address         Stratum Poll Reach LastRx Last sample\n"


def test_reach_monitor_no_longer_counts_fuse(tmp_path):
    """A live FUSE used to mask a dead HPPS.  FUSE no longer counts."""
    r = _reach(tmp_path, HDR
               + "#? FUSE  0 4 377 1 +1ms[+1ms] +/- 1ms\n"
               + "#? HPPS  0 0   0 - +0ns[+0ns] +/- 0ns\n")
    assert r.returncode == 1, r.stdout + r.stderr


def test_reach_monitor_is_content_with_hpps_alone(tmp_path):
    r = _reach(tmp_path, HDR + "#? HPPS  0 0 377 1 +1ms[+1ms] +/- 1ms\n")
    assert r.returncode == 0, r.stdout + r.stderr


# ── the reach monitor latches a persistent condition ────────────────────
#
# Review of T3 steps 2-5, finding I2: with FUSE gone, a station without T6
# fails the HPPS check on every 5-minute timer run, for good.  A
# persistent condition must be reported on change, then reminded
# periodically (feedback_persistent_condition_log_spam): never once per run.

HPPS_DEAD = HDR + "#? HPPS  0 0   0 - +0ns[+0ns] +/- 0ns\n"
HPPS_LIVE = HDR + "#? HPPS  0 0 377 1 +1ms[+1ms] +/- 1ms\n"


def _state_dir(tmp_path, mode=0o700):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    d.chmod(mode)
    return d


def _reach_at(tmp_path, sources, now, state, owner_uid=None):
    """Run the monitor.  It runs as root in production and trusts only a
    root-owned state directory; the test runs as the invoking user, so it
    names that user as the expected owner unless told otherwise."""
    bin_dir = tmp_path / "bin"
    if bin_dir.exists():
        (bin_dir / "chronyc").write_text(
            f"#!/bin/bash\ncat <<'EOF'\n{sources}EOF\n")
    else:
        _fake_bin(tmp_path, sources)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ['PATH']}",
               CHRONY_REACH_STATE_FILE=str(state),
               CHRONY_REACH_NOW=str(now),
               CHRONY_REACH_STATE_OWNER_UID=str(
                   os.getuid() if owner_uid is None else owner_uid))
    r = subprocess.run(
        ["bash", str(REACH_SH), "--threshold", "64", "--verbose",
         "--restart-on-failure"],
        capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


def test_reach_monitor_reports_a_persistent_fault_once_then_reminds(tmp_path):
    state = _state_dir(tmp_path) / "chrony-reach.state"
    t0 = 1_800_000_000
    code, out = _reach_at(tmp_path, HPPS_DEAD, t0, state)
    assert code == 1 and "WARNING" in out and "HPPS" in out

    # The next runs, five minutes apart for most of a day: silent.
    for k in range(1, 60):
        code, out = _reach_at(tmp_path, HPPS_DEAD, t0 + 300 * k, state)
        assert code == 1
        assert out.strip() == "", (k, out)

    # Six hours on: one reminder that names how long it has lasted.
    code, out = _reach_at(tmp_path, HPPS_DEAD, t0 + 6 * 3600, state)
    assert code == 1 and "still" in out and "6h" in out
    code, out = _reach_at(tmp_path, HPPS_DEAD, t0 + 6 * 3600 + 300, state)
    assert out.strip() == ""


def test_reach_monitor_reports_recovery_once(tmp_path):
    state = _state_dir(tmp_path) / "chrony-reach.state"
    t0 = 1_800_000_000
    _reach_at(tmp_path, HPPS_DEAD, t0, state)
    code, out = _reach_at(tmp_path, HPPS_LIVE, t0 + 300, state)
    assert code == 0 and "OK" in out and "recovered" in out
    code, out = _reach_at(tmp_path, HPPS_LIVE, t0 + 600, state)
    assert code == 0 and out.strip() == ""



# ── the reach monitor's state file is root's, and safe ──────────────────
#
# Security review of 7019b8c (HIGH): the monitor runs as root, and its
# state file sat in /run/hf-timestd, which the timestd user owns.  A
# symlink planted there would have let root's write clobber any file.
# The state now lives in the unit's own RuntimeDirectory (0700), the
# write is mktemp + rename (a planted symlink is replaced, never
# followed), and a directory that root does not own alone is refused.


def test_reach_monitor_replaces_a_planted_symlink(tmp_path):
    d = _state_dir(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    state = d / "chrony-reach.state"
    state.symlink_to(victim)
    _reach_at(tmp_path, HPPS_DEAD, 1_800_000_000, state)
    assert victim.read_text() == "precious\n"
    assert not state.is_symlink() and state.is_file()
    assert state.read_text().split()[0] == "WARNING"
    assert [p.name for p in d.iterdir()] == ["chrony-reach.state"]


def test_reach_monitor_refuses_a_directory_root_does_not_own(tmp_path):
    """Owner uid 0 expected, the directory is the test user's: refused,
    nothing written, and every run reports (the pre-latch behaviour)."""
    if os.getuid() == 0:
        import pytest
        pytest.skip("needs a non-root test user")
    state = _state_dir(tmp_path) / "chrony-reach.state"
    for k in range(3):
        code, out = _reach_at(tmp_path, HPPS_DEAD, 1_800_000_000 + 300 * k,
                              state, owner_uid=0)
        assert code == 1
        assert out.count("not latching") == 1
        assert "Chrony HPPS reach = " in out
    assert not state.exists()


def test_reach_monitor_refuses_a_group_writable_directory(tmp_path):
    state = _state_dir(tmp_path, mode=0o770) / "chrony-reach.state"
    for k in range(2):
        code, out = _reach_at(tmp_path, HPPS_DEAD, 1_800_000_000 + 300 * k,
                              state)
        assert "not latching" in out and "Chrony HPPS reach = " in out
    assert not state.exists()


def test_reach_monitor_ignores_a_garbage_prior_status(tmp_path):
    """Anything but OK or WARNING reads as no prior state: a live HPPS
    then reports a plain OK, not a recovery from a state that never was."""
    state = _state_dir(tmp_path) / "chrony-reach.state"
    state.write_text("PWNED;reboot 1 2\n")
    code, out = _reach_at(tmp_path, HPPS_LIVE, 1_800_000_000, state)
    assert code == 0
    assert "OK: Chrony HPPS reach = " in out and "recovered" not in out
    assert state.read_text().split()[:2] == ["OK", "1800000000"]


def test_monitor_unit_owns_a_private_preserved_state_directory():
    unit = (REPO / "systemd" / "timestd-chrony-monitor.service").read_text()
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1]
             for l in unit.splitlines() if "=" in l and not l.startswith("#")}
    assert lines["User"] == "root"
    assert lines["RuntimeDirectory"] == "hf-timestd-monitor"
    assert lines["RuntimeDirectoryMode"] == "0700"
    assert lines["RuntimeDirectoryPreserve"] == "yes"
    assert ("/run/hf-timestd-monitor/chrony-reach.state"
            in REACH_SH.read_text())
