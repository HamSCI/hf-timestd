"""Shim: canonical home is hamsci_dsp.propagation.tec_geometry (split §5.2).

Re-exports until the last hf-timestd consumer imports hamsci-dsp
directly, then dies.  STATIONS keeps its historical dict shape.
"""
from hamsci_dsp.propagation.tec_geometry import *  # noqa: F401,F403
from hamsci_dsp.stations import BUILTIN_CATALOG as _CATALOG

STATIONS = _CATALOG.locations()
