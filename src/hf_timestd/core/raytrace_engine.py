"""Shim: canonical home is hamsci_dsp.raytrace (split design §5.2).

Re-exports the moved engine until the last hf-timestd consumer imports
hamsci-dsp directly, then dies (shim discipline: per-symbol, tracked in
the implementation plan).  The engine's own unit tests moved with it.
"""
from hamsci_dsp.raytrace import *  # noqa: F401,F403
