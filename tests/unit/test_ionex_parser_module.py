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


def test_ionex_parser_is_owned_by_the_package():
    assert IONEXParser.__module__ == 'hf_timestd.core.ionex_parser'


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


def test_cache_hit_avoids_reparsing(monkeypatch, tmp_path):
    import hf_timestd.core.ionospheric_model as im
    _StubParser.instances = 0
    monkeypatch.setattr(im, 'IONEXParser', _StubParser)
    (tmp_path / _IONEX_NAME).write_text('')
    model = im.IonosphericModel(enable_iri=False, enable_calibration=False,
                                ionex_dir=tmp_path)

    first = model.get_ionex_vtec(40.0, -95.0, _TS)
    second = model.get_ionex_vtec(40.0, -95.0, _TS)
    assert first == second
    assert first[0] == 17.5
    assert _StubParser.instances == 1  # second call was a cache hit


def test_stale_cache_is_reparsed(monkeypatch, tmp_path):
    import hf_timestd.core.ionospheric_model as im
    _StubParser.instances = 0
    monkeypatch.setattr(im, 'IONEXParser', _StubParser)
    (tmp_path / _IONEX_NAME).write_text('')
    model = im.IonosphericModel(enable_iri=False, enable_calibration=False,
                                ionex_dir=tmp_path)
    model._ionex_cache_max_age = 0  # everything is immediately stale

    model.get_ionex_vtec(40.0, -95.0, _TS)
    model.get_ionex_vtec(40.0, -95.0, _TS)
    assert _StubParser.instances == 2  # max_age honoured -> re-parsed
