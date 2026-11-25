"""update-iri-indices.sh refreshes every IRI data directory it is given, from the first source that
serves a plausible file, and leaves a directory alone when every source fails.  Sources are local
file:// directories, so no network is touched."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update-iri-indices.sh"


def _run(tmp_path, sources, dirs):
    env = dict(os.environ, IRI_SOURCES=" ".join(f"file://{s}" for s in sources),
               IRI_DAT_DIRS=" ".join(str(d) for d in dirs))
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def _today():
    import time
    return time.strftime("%y %m %d", time.gmtime())


def _source(path, last_line):
    path.mkdir()
    for name in ("apf107.dat", "ig_rz.dat"):
        (path / name).write_text("x" * 2000 + "\n" + last_line + "\n")
    return path


def _stale(path):
    path.mkdir()
    for name in ("apf107.dat", "ig_rz.dat"):
        (path / name).write_text("23  6 29 stale\n")
    return path


def test_every_directory_gets_the_fresh_files(tmp_path):
    src = _source(tmp_path / "src", _today() + " fresh")
    pharlap, package = _stale(tmp_path / "pharlap"), _stale(tmp_path / "pkg")
    r = _run(tmp_path, [src], [pharlap, package])
    assert r.returncode == 0, r.stdout + r.stderr
    for d in (pharlap, package):
        for name in ("apf107.dat", "ig_rz.dat"):
            assert (d / name).read_text().splitlines()[-1].endswith(" fresh")
            assert oct((d / name).stat().st_mode & 0o777) == "0o644"
        assert not list(d.glob(".*.new"))


def test_a_short_answer_falls_through_to_the_next_source(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    for name in ("apf107.dat", "ig_rz.dat"):
        (bad / name).write_text("404\n")
    good = _source(tmp_path / "good", _today() + " fallback")
    d = _stale(tmp_path / "pkg")
    r = _run(tmp_path, [bad, good], [d])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (d / "apf107.dat").read_text().splitlines()[-1].endswith(" fallback")


def test_a_stale_mirror_loses_to_the_next_source(tmp_path):
    # KFS-OMNI, 2026-11-13: the primary mirror served an apf107.dat ending 2026-07-28.
    stale = _source(tmp_path / "stale", "26  7 28 old mirror")
    fresh = _source(tmp_path / "fresh", _today() + " fresh")
    d = _stale(tmp_path / "pkg")
    r = _run(tmp_path, [stale, fresh], [d])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "days old" in r.stdout
    assert (d / "apf107.dat").read_text().splitlines()[-1].endswith("fresh")


def test_a_symlinked_directory_is_skipped(tmp_path):
    src = _source(tmp_path / "src", _today() + " fresh")
    real = _stale(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    r = _run(tmp_path, [src], [link])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped" in r.stdout
    assert (real / "apf107.dat").read_text() == "23  6 29 stale\n"


def test_when_every_source_fails_the_old_files_stay(tmp_path):
    d = _stale(tmp_path / "pkg")
    r = _run(tmp_path, [tmp_path / "missing"], [d])
    assert r.returncode != 0
    assert (d / "apf107.dat").read_text() == "23  6 29 stale\n"


def test_no_iri_directory_is_not_an_error(tmp_path):
    r = subprocess.run(["bash", "-c", f"IRI_DAT_DIRS='' bash {SCRIPT}"], capture_output=True,
                       text=True, env=dict(os.environ, IRI_SOURCES="file:///nonexistent"))
    # On the devbox no /opt/pharlap and no /opt/git/sigmond venvs exist, so discovery finds none.
    if not Path("/opt/git/sigmond").exists() and not Path("/opt/pharlap_4.7.4").exists():
        assert r.returncode == 0, r.stdout + r.stderr
        assert "nothing to update" in r.stdout


def _code_lines():
    return [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]


def test_the_updater_never_targets_the_iri2020_package_copy():
    # 2026-11-13: an updater wrote current index files into site-packages/iri2020/data; that
    # package's Fortran driver segfaults on them, and IRI broke fleet-wide.  Only PHaRLAP's copy
    # may be refreshed, so no executable line may name the package, its data directory or
    # site-packages, and the one directory discovery adds is PHaRLAP's.
    code = "\n".join(_code_lines())
    for banned in ("site-packages", "iri2020/data", "iri2020.__file__", "import iri2020", "dist-packages"):
        assert banned not in code, banned
    adds = [ln.strip() for ln in _code_lines() if "DAT_DIRS+=" in ln]
    assert len(adds) == 1 and "/opt/pharlap_4.7.4/dat/iri2020" in adds[0], adds


def test_a_discovery_run_writes_nowhere_under_an_iri2020_package_path(tmp_path):
    # Trace a real discovery run (IRI_DAT_DIRS unset) and require that no path under
    # site-packages or iri2020/data appears in any command it executes.
    r = subprocess.run(["bash", "-x", str(SCRIPT)], capture_output=True, text=True,
                       env={k: v for k, v in dict(os.environ, IRI_SOURCES="file:///nonexistent").items()
                            if k != "IRI_DAT_DIRS"})
    trace = r.stdout + r.stderr
    assert "site-packages" not in trace and "iri2020/data" not in trace
