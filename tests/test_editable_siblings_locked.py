"""Every declared editable sibling must also be present in uv.lock.

scripts/install.sh builds the venv with `uv sync --frozen`, which reproduces
uv.lock and ignores anything pyproject declares but the lock does not carry.
So declaring a `[tool.uv.sources]` editable sibling WITHOUT relocking is a
silent install-time hole: the repo looks correct, `uv sync` exits 0, and the
package is simply absent from the venv.

Regression for 2026-08-28: commit 2f8bf5b added `hamsci-physics` to
[project].dependencies and [tool.uv.sources] but did not regenerate uv.lock.
`smd install hf-timestd` on B4 therefore rebuilt the venv WITHOUT
hamsci_physics, and a unit crash-looped on ModuleNotFoundError until
the sibling was pip-installed by hand.  Fix: `uv lock`.
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _declared_editable_siblings() -> set:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    return {
        name for name, spec in sources.items()
        if isinstance(spec, dict) and spec.get("editable") and spec.get("path")
    }


def test_every_declared_editable_sibling_is_in_uv_lock():
    lock = (ROOT / "uv.lock").read_text()
    missing = sorted(
        name for name in _declared_editable_siblings()
        if f'name = "{name}"' not in lock
    )
    assert not missing, (
        f"declared in [tool.uv.sources] but absent from uv.lock: {missing}. "
        f"`uv sync --frozen` will build a venv without them. Run `uv lock`."
    )
