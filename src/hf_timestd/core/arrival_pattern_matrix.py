"""Shim: canonical home is hamsci_dsp.propagation.arrival_matrix (split design §5.2).

Re-exports the moved engine until the last hf-timestd consumer imports
hamsci-dsp directly, then dies (shim discipline: per-symbol, tracked in
the implementation plan).
"""
from hamsci_dsp.propagation.arrival_matrix import *  # noqa: F401,F403
