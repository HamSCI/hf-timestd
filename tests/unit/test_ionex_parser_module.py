"""Regression tests for IONEXParser ownership (P-H18, split §5.2).

IONEXParser used to live in scripts/ionex_integration.py and was loaded into
ionospheric_model via importlib.exec_module on *every* cache miss — slow, with
an unchecked spec.loader, and broken under a wheel install (scripts/ is not
packaged).

The canonical class now lives in hamsci_dsp.ionosphere.ionex; hf-timestd's
hf_timestd.core.ionex_parser is a re-export shim.  These tests pin that
ownership and check that both import paths (the package and
ionospheric_model) resolve to the same class object.  The
standalone ionex_* scripts came back from hamsci-physics in v3.96 with the IONEX
fetcher, and their re-export test came with them.
The cache-behaviour tests moved with the engine to hamsci-dsp.
"""

import sys
from pathlib import Path

from hf_timestd.core.ionex_parser import IONEXParser


def test_ionex_parser_is_owned_by_hamsci_dsp():
    # Split §5.2: the canonical home moved to hamsci-dsp; hf-timestd's
    # module is a re-export shim, so every importer shares one class.
    assert IONEXParser.__module__ == 'hamsci_dsp.ionosphere.ionex'


def test_ionospheric_model_imports_parser_directly():
    # Imported once at module load — not re-exec'd per cache miss.
    import hf_timestd.core.ionospheric_model as im
    assert im.IONEXParser is IONEXParser


def test_ionex_integration_script_reexports_the_shared_parser():
    # The original defect (P-H18) was a second copy loaded by importlib on every cache miss.
    from hamsci_dsp.ionosphere.ionex import IONEXParser as Shared
    scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import ionex_integration
    assert ionex_integration.IONEXParser is Shared
