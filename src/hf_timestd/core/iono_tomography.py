"""Shim: canonical home is hamsci_dsp.ionosphere.tomography (split design §5.2).

Re-exports the moved engine until the last hf-timestd consumer imports
hamsci-dsp directly, then dies (shim discipline: per-symbol, tracked in
the implementation plan).
"""
from hamsci_dsp.ionosphere.tomography import *  # noqa: F401,F403
