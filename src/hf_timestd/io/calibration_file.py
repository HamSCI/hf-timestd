"""Shim: canonical home is hamsci_dsp.io.calibration_file (split §5.2).

Installs hf-timestd's version string into the library's injected
PRODUCT_VERSION (the one true circular dep, now a plain string).
"""
from hamsci_dsp.io.calibration_file import *  # noqa: F401,F403
from hamsci_dsp.io.calibration_file import set_product_version as _set

try:
    from hf_timestd.version import TIMESTD_VERSION as _V
    _set(_V)
except Exception:  # pragma: no cover - version module absent in odd harnesses
    pass
