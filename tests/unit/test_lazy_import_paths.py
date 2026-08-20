"""Lazy imports inside methods must actually resolve.

`feed_fusion` was impossible to enable on any station because
`core_recorder_v2._setup_wwvb_fusion_feed` imported DataProductRegistry
relatively:

    from .data_product_registry import DataProductRegistry   # hf_timestd.core.…

The module lives at `hf_timestd/data_product_registry.py`, so inside
`hf_timestd/core/` that relative path resolves to a package that does not exist.
The two other call sites (`metrology_service`, `physics_fusion_service`) use the
absolute path and work.

Nothing caught it because the failure is SOFT: the caller treats any exception as
"stay ledger-only" and logs a warning, so a station configured with
`feed_fusion = true` runs on happily and silently produces no L1 rows. Observed
on AC0G-B4 2026-08-19 23:59:37Z:

    WWVB feed_fusion setup failed; staying ledger-only:
    No module named 'hf_timestd.core.data_product_registry'

A lazy import is only exercised when its branch runs, so a module-level test
suite never touches it. This test resolves them statically instead.
"""
import ast
import importlib
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "hf_timestd"


def _lazy_imports(path: pathlib.Path):
    """Every `from X import ...` that sits inside a function body."""
    tree = ast.parse(path.read_text())
    pkg = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:  # relative — resolve against this file's package
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if mod.startswith("hf_timestd"):
                yield path, node.lineno, mod


# core/legacy/ is an archived copy nothing imports. It moved down a level when
# archived, so every relative import in it is off by one — dead code, and
# repairing it would be scope creep. Excluded deliberately, not overlooked.
EXCLUDE = ("core/legacy/",)


def _all_lazy():
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if any(rel.startswith(x) for x in EXCLUDE):
            continue
        yield from _lazy_imports(p)


@pytest.mark.parametrize(
    "path,lineno,module",
    list(_all_lazy()),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_lazy_import_resolves(path, lineno, module):
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"{path.name}:{lineno} lazily imports '{module}', which does not "
            f"exist ({exc}). Lazy imports only fail when their branch runs — "
            f"and when the caller swallows the error, the feature silently "
            f"degrades instead of failing loudly."
        )
