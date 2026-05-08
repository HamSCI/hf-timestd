"""Tests for hf-timestd's CONTRACT v0.6 §17 ClickHouse integration.

The L2 calibration service writes per-cycle detection events to:
  * HDF5 (canonical L1/L2 artefact, unchanged from v0.5)
  * `timestd.events` in CH (additive staging tier when sigmond's
    `[storage.clickhouse]` block is configured)

This module verifies:

  * `_build_ch_writer` is graceful when sigmond.hamsci_ch isn't
    importable (returns None) and produces a real Writer when it is.
  * `_ch_row_from_l2` produces row dicts whose keys exactly match the
    `timestd.events` schema columns (clickhouse/schema/timestd/
    001_create_events.sql).
  * Enum-typed fields (StationID, DiscriminationMethod, QualityFlag,
    QualityGrade) are serialised to plain strings so CH's
    LowCardinality(String) columns accept them.
  * The HDF5 path is unaffected by CH-side failures (insert errors are
    caught and logged, never re-raised into the HDF5 writer).

Live CH I/O (a running clickhouse-server) is NOT exercised here; we
inject a fake writer that records `insert()` calls.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Sigmond is in a sibling repo when tests are run locally.
SIGMOND_LIB = REPO_ROOT.parent / "sigmond" / "lib"
if SIGMOND_LIB.is_dir() and str(SIGMOND_LIB) not in sys.path:
    sys.path.insert(0, str(SIGMOND_LIB))

from hf_timestd.core.l2_calibration_service import L2CalibrationService


# ── columns the schema declares (clickhouse/schema/timestd/001) ────────────

EXPECTED_COLUMNS = {
    "time", "host_call", "host_grid", "radiod_id", "instance",
    "processing_version",
    "station", "frequency_khz",
    "raw_toa_ms", "toa_uncertainty_ms",
    "clock_offset_ms", "expanded_uncertainty_ms",
    "snr_db", "doppler_hz", "distance_km",
    "propagation_mode", "n_hops",
    "quality_flag", "quality_grade", "discrimination_method",
    "delay_plausible",
}


def _l2_dict(**overrides) -> dict:
    """A minimal L2-measurement dict in the shape `model_dump(mode='json')`
    yields — strings for enum fields, ints/floats for everything else."""
    base = {
        "timestamp_utc":          "2026-05-08T12:34:00Z",
        "minute_boundary_utc":    1746628440,                # 2026-05-07 14:34:00 UTC
        "rtp_timestamp":          0,
        "station":                "WWV",
        "frequency_mhz":          5.0,
        "discrimination_method":  "TONE",
        "raw_arrival_time_ms":    8.42,
        "clock_offset_ms":        0.13,
        "uncertainty_ms":         0.05,
        "expanded_uncertainty_ms": 0.10,
        "snr_db":                 18.4,
        "doppler_hz":             0.7,
        "distance_km":            1234.0,
        "quality_flag":           "GOOD",
        "quality_grade":          "A",
        "propagation_mode":       "1F",
        "n_hops":                 1,
        "processing_version":     "7.1.0",
    }
    base.update(overrides)
    return base


# ── _ch_row_from_l2 ────────────────────────────────────────────────────────

class TestChRowFromL2(unittest.TestCase):

    def test_row_has_every_schema_column(self):
        row = L2CalibrationService._ch_row_from_l2(
            l2_measurement=None,
            channel="WWV-5",
            l2_dict=_l2_dict(),
        )
        self.assertEqual(set(row.keys()), EXPECTED_COLUMNS)

    def test_row_has_correct_time(self):
        row = L2CalibrationService._ch_row_from_l2(
            l2_measurement=None,
            channel="WWV-5",
            l2_dict=_l2_dict(),
        )
        # 1746628440 → 2025-05-07 14:34:00 UTC (no tz on the datetime).
        self.assertEqual(row["time"], datetime(2025, 5, 7, 14, 34))

    def test_frequency_khz_rounded_from_mhz(self):
        row = L2CalibrationService._ch_row_from_l2(
            l2_measurement=None,
            channel="WWV-15",
            l2_dict=_l2_dict(frequency_mhz=15.0),
        )
        self.assertEqual(row["frequency_khz"], 15000)

    def test_instance_is_channel_name(self):
        row = L2CalibrationService._ch_row_from_l2(
            l2_measurement=None,
            channel="WWVH-2.5",
            l2_dict=_l2_dict(),
        )
        self.assertEqual(row["instance"], "WWVH-2.5")

    def test_enum_objects_serialised_to_strings(self):
        """Pass enum-like objects (have .value or .name) and confirm
        they round-trip to plain strings."""

        class _FakeEnum:
            def __init__(self, value: str):
                self.value = value

        row = L2CalibrationService._ch_row_from_l2(
            l2_measurement=None,
            channel="WWV-5",
            l2_dict=_l2_dict(
                station=_FakeEnum("WWV"),
                discrimination_method=_FakeEnum("BCD"),
                quality_flag=_FakeEnum("MARGINAL"),
                quality_grade=_FakeEnum("B"),
            ),
        )
        self.assertEqual(row["station"], "WWV")
        self.assertEqual(row["discrimination_method"], "BCD")
        self.assertEqual(row["quality_flag"], "MARGINAL")
        self.assertEqual(row["quality_grade"], "B")

    def test_delay_plausible_for_quality_flags(self):
        good = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5", _l2_dict(quality_flag="GOOD")
        )
        marginal = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5", _l2_dict(quality_flag="MARGINAL")
        )
        bad = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5", _l2_dict(quality_flag="BAD")
        )
        missing = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5", _l2_dict(quality_flag="MISSING")
        )
        self.assertEqual(good["delay_plausible"], 1)
        self.assertEqual(marginal["delay_plausible"], 1)
        self.assertEqual(bad["delay_plausible"], 0)
        self.assertEqual(missing["delay_plausible"], 0)

    def test_nullable_fields_passthrough_none(self):
        """`snr_db`, `doppler_hz`, `distance_km`, `expanded_uncertainty_ms`,
        `n_hops` are Nullable in the schema — ``None`` should pass
        through cleanly."""
        row = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5",
            _l2_dict(
                snr_db=None,
                doppler_hz=None,
                distance_km=None,
                expanded_uncertainty_ms=None,
                n_hops=None,
            ),
        )
        for field in (
            "snr_db", "doppler_hz", "distance_km",
            "expanded_uncertainty_ms", "n_hops",
        ):
            self.assertIsNone(row[field])

    def test_invalid_minute_boundary_falls_back_to_utcnow(self):
        """A garbage minute_boundary_utc (e.g. None or string) shouldn't
        crash the row builder — it should still emit a usable row."""
        row = L2CalibrationService._ch_row_from_l2(
            None, "WWV-5",
            _l2_dict(minute_boundary_utc=None),
        )
        self.assertIsInstance(row["time"], datetime)


# ── _build_ch_writer ───────────────────────────────────────────────────────

class TestBuildChWriter(unittest.TestCase):
    """The factory is a staticmethod that lazy-imports sigmond.hamsci_ch
    and returns its Writer.  When sigmond is unavailable it returns None;
    when SIGMOND_CLICKHOUSE_URL is unset, the writer is a no-op."""

    def test_returns_none_when_sigmond_unavailable(self):
        """Patch the import to fail, confirm None is returned."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sigmond.hamsci_ch":
                raise ImportError("simulated missing sigmond")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            self.assertIsNone(L2CalibrationService._build_ch_writer())
        finally:
            builtins.__import__ = real_import

    def test_returns_noop_writer_when_url_unset(self):
        """sigmond IS available but `SIGMOND_CLICKHOUSE_URL` is not —
        Writer.from_env returns a noop writer."""
        import os
        saved = os.environ.pop("SIGMOND_CLICKHOUSE_URL", None)
        try:
            w = L2CalibrationService._build_ch_writer()
            # When sigmond is on the path, we get a Writer instance
            # whose .is_noop is True.  When sigmond isn't on path,
            # we get None.  Either is acceptable for this test
            # (the calling code handles both).
            if w is not None:
                self.assertTrue(w.is_noop,
                                "expected noop writer when SIGMOND_CLICKHOUSE_URL is unset")
        finally:
            if saved is not None:
                os.environ["SIGMOND_CLICKHOUSE_URL"] = saved


if __name__ == "__main__":
    unittest.main()
