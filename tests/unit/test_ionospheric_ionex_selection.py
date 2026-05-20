"""Regression tests for P-H17: IONEX file selection in ionospheric_model.

get_ionex_vtec located the IONEX GIM with glob("*YYYY*") — the year only —
then picked the most recently downloaded match. A query for any 2026 date
returned whichever 2026 file was newest on disk, almost never the right day.

_find_ionex_file now matches the full date (year + day-of-year) for both the
modern (IGS0OPSFIN_YYYYDDD0000_..._GIM.INX.gz) and legacy (igsgDDD0.YYi.Z)
filename patterns.
"""

import os
import time
from datetime import datetime, timezone

from hf_timestd.core.ionospheric_model import IonosphericModel

# 2026-03-15 is day-of-year 074 (2026 is not a leap year).
_DATE = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _model(ionex_dir):
    return IonosphericModel(enable_iri=False, enable_calibration=False,
                            ionex_dir=ionex_dir)


def _touch(path, mtime=None):
    path.write_text("")  # only the filename matters for selection
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestFindIonexFile:
    def test_modern_filename_exact_date(self, tmp_path):
        target = tmp_path / "IGS0OPSFIN_20260740000_01D_02H_GIM.INX.gz"
        _touch(target)
        assert _model(tmp_path)._find_ionex_file(_DATE) == target

    def test_legacy_filename_exact_date(self, tmp_path):
        target = tmp_path / "igsg0740.26i.Z"
        _touch(target)
        assert _model(tmp_path)._find_ionex_file(_DATE) == target

    def test_wrong_day_same_year_is_not_selected(self, tmp_path):
        # The core P-H17 regression: a NEWER same-year file for a different
        # day must not be returned just because its mtime is latest.
        right = tmp_path / "IGS0OPSFIN_20260740000_01D_02H_GIM.INX.gz"  # DOY 074
        wrong = tmp_path / "IGS0OPSFIN_20261720000_01D_02H_GIM.INX.gz"  # DOY 172
        _touch(right, mtime=time.time() - 10000)  # older
        _touch(wrong, mtime=time.time())          # newer — old code's pick
        assert _model(tmp_path)._find_ionex_file(_DATE) == right

    def test_no_file_for_date_returns_none(self, tmp_path):
        _touch(tmp_path / "IGS0OPSFIN_20261720000_01D_02H_GIM.INX.gz")  # DOY 172
        assert _model(tmp_path)._find_ionex_file(_DATE) is None

    def test_missing_directory_returns_none(self, tmp_path):
        model = _model(tmp_path / "does_not_exist")
        assert model._find_ionex_file(_DATE) is None

    def test_same_day_picks_most_recently_written(self, tmp_path):
        rapid = tmp_path / "IGS0OPSRAP_20260740000_01D_02H_GIM.INX.gz"
        final = tmp_path / "IGS0OPSFIN_20260740000_01D_02H_GIM.INX.gz"
        _touch(rapid, mtime=time.time() - 5000)
        _touch(final, mtime=time.time())
        assert _model(tmp_path)._find_ionex_file(_DATE) == final
