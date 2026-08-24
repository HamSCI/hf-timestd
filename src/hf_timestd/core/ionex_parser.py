"""Shim: IONEXParser's canonical home is hamsci_dsp.ionosphere.ionex.

Split design §5.2 — the parser moved to hamsci-dsp (with a longitude
fill-position fix its first-ever parse tests caught there); this module
re-exports it until the last hf-timestd consumer imports hamsci-dsp
directly, then dies (shim discipline: per-symbol, tracked in the plan).
"""
from hamsci_dsp.ionosphere.ionex import IONEXParser  # noqa: F401

__all__ = ["IONEXParser"]
