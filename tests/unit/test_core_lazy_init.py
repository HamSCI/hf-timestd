"""core/__init__ must import lazily (split plan Phase 1).

The package facade eagerly imported ~30 submodules, so `import
hf_timestd.core` dragged in the whole engine surface (and, shim-era,
half of hamsci-dsp) for callers that wanted one name.  The facade now
resolves its public names through PEP 562 module __getattr__.

Both properties are pinned:
* importing the package is cheap — the heavy submodules are NOT in
  sys.modules after a bare `import hf_timestd.core` in a fresh
  interpreter;
* every public name still resolves, so no consumer changes.
"""
import subprocess
import sys


def test_bare_import_is_lazy():
    probe = (
        "import sys; import hf_timestd.core; "
        "heavy = [m for m in sys.modules if any(k in m for k in ("
        "'core_recorder_v2', 'multi_broadcast_fusion', 'metrology_service', "
        "'propagation.model', 'tiered_storage', 'multi_station_detector'))]; "
        "print(','.join(sorted(heavy)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-800:]
    heavy = [m for m in out.stdout.strip().split(",") if m]
    assert not heavy, f"eagerly imported: {heavy}"


def test_all_public_names_still_resolve():
    import hf_timestd.core as core
    missing = []
    for name in core.__all__:
        try:
            getattr(core, name)
        except Exception as exc:  # noqa: BLE001 - collecting, not masking
            missing.append(f"{name}: {exc}")
    assert not missing, missing[:10]
