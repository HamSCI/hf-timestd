"""Regression tests for P-H18: IONEXParser ownership and cache handling.

IONEXParser used to live in scripts/ionex_integration.py and was loaded into
ionospheric_model via importlib.exec_module on *every* cache miss — slow, with
an unchecked spec.loader, and broken under a wheel install (scripts/ is not
packaged). _ionex_cache_max_age was defined but never honoured, so a stale
parser was served indefinitely.

IONEXParser now lives in the package (hf_timestd.core.ionex_parser), is
imported once at module load, and the cache honours _ionex_cache_max_age.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from hf_timestd.core.ionex_parser import IONEXParser

_TS = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)  # day-of-year 074
_IONEX_NAME = "IGS0OPSFIN_20260740000_01D_02H_GIM.INX.gz"


class _StubParser:
    """Stand-in for IONEXParser that counts how often it is constructed."""
    instances = 0

    def __init__(self, path):
        type(self).instances += 1
        self.path = path

    def interpolate(self, lat, lon, timestamp):
        return 17.5


def test_ionex_parser_is_owned_by_hamsci_dsp():
    # Split §5.2: the canonical home moved to hamsci-dsp; hf-timestd's
    # module is a re-export shim, so every importer shares one class.
    assert IONEXParser.__module__ == 'hamsci_dsp.ionosphere.ionex'


def test_ionospheric_model_imports_parser_directly():
    # Imported once at module load — not re-exec'd per cache miss.
    import hf_timestd.core.ionospheric_model as im
    assert im.IONEXParser is IONEXParser


def test_script_reexports_the_same_parser():
    # The standalone ionex_* scripts still get IONEXParser from
    # ionex_integration — now re-exported from the package, not redefined.
    scripts_dir = str(Path(__file__).resolve().parents[2] / 'scripts')
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import ionex_integration
    assert ionex_integration.IONEXParser is IONEXParser


# The cache-behavior tests moved with the engine to hamsci-dsp
# (tests/test_ionosphere_model_ionex_cache.py): monkeypatching the shim
# module's IONEXParser can no longer reach the engine's own binding.
