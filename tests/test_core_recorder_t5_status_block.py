"""The recorder publishes the LB-1421 host-versus-GPS gap beside valid_fix.

2026-09-04: the probe knew the host clock sat 12 s from the GPS second and
the status file said only ``valid_fix: false``.  sigmond-t6-stuck-watchdog
reads this block; LbeT5DirectProbe forwards it to the authority manager.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.lb1421_t5_probe import Lb1421Reading


def _bare(reading):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    probe = MagicMock()
    probe.device = "/run/gpsdo"
    probe.get_latest.return_value = reading
    cr._lb1421_probe = probe
    cr._t5_bench_state = MagicMock(return_value=None)
    return cr


def test_inconsistent_host_clock_is_named_in_the_block():
    r = Lb1421Reading(pps_utc_sec=1788537504, host_monotonic_at_read=0.0,
                      valid_fix=False, host_minus_gps_s=-12.1,
                      invalid_reason="host_gps_inconsistent")
    block = _bare(r)._t5_lbe1421_status()
    assert block["valid_fix"] is False
    assert block["host_minus_gps_s"] == -12.1
    assert block["reason"] == "host_gps_inconsistent"


def test_valid_reading_carries_the_gap_and_no_reason():
    r = Lb1421Reading(pps_utc_sec=1788537504, host_monotonic_at_read=0.0,
                      valid_fix=True, host_minus_gps_s=0.42)
    block = _bare(r)._t5_lbe1421_status()
    assert block["valid_fix"] is True
    assert block["host_minus_gps_s"] == 0.42
    assert block.get("reason") is None
    assert block["rtp_anchor_grounded"] is False      # bench stubbed to None


def test_no_reading_yet_keeps_its_wording():
    block = _bare(None)._t5_lbe1421_status()
    assert block["valid_fix"] is False
    assert block["reason"] == "no reading yet"
    assert block["host_minus_gps_s"] is None
