"""v3.85 item 6: criterion 7 never judges radiod's raw counter phase.

WB6CXC-7 (2026-11-01, v3.84): a GPS-mini that names the second over USB,
a TS-1 for T6, internet NTP only -- no T4, no T5.  Every start logged

    T6 chain_delay initial accept REFUSED: effective +341.1 ms exceeds
    physical-plausibility bound ±250 ms (raw=341132278 ns ...)

and three starts read raw 203.7, 430.0 and 341.1 ms.  Those values are
the edge's position in radiod's RTP counter, whose origin radiod draws
afresh at each start; they say nothing about UTC.  T6's own labels came
from the authority's anchor (named second + asserted delay) and were
right.  The station ran on T2.

Criterion 7, and the initial-accept guard after it, now judge the best
placement available: T5, a qualified T4, then the named second with
radiod's per-channel pair.  These tests drive real BPSK through
``_t6_on_samples`` with WB6CXC-7's three counter origins.
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_t6_acquisition_gate as gate
import test_t6_t4_disambiguation as t4d

LOGGER = "hf_timestd.core.core_recorder_v2"
WB6CXC_RAW_S = (0.2037, 0.4300, 0.3411)


def _base_for_raw(raw_s):
    """An RTP origin that puts the edge `raw_s` into its counter second.

    gate.RTP0 puts it at the chain delay (16.618 ms), the UTC-aligned
    case; shifting the origin moves only the counter phase.
    """
    return gate.RTP0 + int(round((raw_s - gate.CHAIN_DELAY_SEC) * gate.SR))


def _wb6cxc(raw_s, pair_error_s=0.0, **kw):
    base = _base_for_raw(raw_s)
    r, utc = t4d._nd_recorder(host=None, base=base)
    for k, v in kw.items():
        setattr(r, k, v)
    return t4d._drive_nd(r, t4d._off(utc, pair_error_s), base=base)


class TestWb6cxcAcquires(unittest.TestCase):

    def test_the_fixture_reads_wb6cxcs_raw_phases(self):
        """The premise: these origins reproduce the logged raw values."""
        for raw_s in WB6CXC_RAW_S:
            phase = ((_base_for_raw(raw_s) + gate.EDGE) % gate.SR) / gate.SR
            self.assertAlmostEqual(phase, raw_s, places=4)

    def test_every_logged_raw_phase_acquires_with_a_sane_pair(self):
        for raw_s in WB6CXC_RAW_S:
            r = _wb6cxc(raw_s)
            self.assertIsNotNone(
                r._t6_last_chain_delay_ns,
                f"raw {raw_s * 1e3:.1f} ms refused: "
                f"{r._t6_last_verdict.failures}")
            self.assertAlmostEqual(r._t6_last_chain_delay_ns / 1e9,
                                   gate.CHAIN_DELAY_SEC, places=4)
            bp = r._t6_acquisition_reference["battery_plausibility"]
            self.assertEqual(bp["reference"], "namer_pair")
            self.assertAlmostEqual(bp["placement_ms"],
                                   gate.CHAIN_DELAY_SEC * 1e3, delta=0.1)

    def test_no_raw_phase_reaches_a_refusal_line(self):
        with self.assertLogs(LOGGER, level=logging.INFO) as logs:
            _wb6cxc(0.3411)
        self.assertFalse(
            [m for m in logs.output if "REFUSED" in m or "341.1" in m],
            "a refusal line still quotes the raw counter phase")

    def test_a_live_t5_names_the_reference(self):
        t5 = SimpleNamespace(get_latest=lambda: object())
        r = _wb6cxc(0.3411, _lb1421_probe=t5)
        self.assertEqual(
            r._t6_acquisition_reference["battery_plausibility"]["reference"],
            "T5")


class TestAPairOffRefusesLoudly(unittest.TestCase):

    def test_a_pair_300_ms_off_refuses_once_on_the_record(self):
        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            r = _wb6cxc(0.3411, pair_error_s=0.30)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        self.assertIn("plausibility", r._t6_last_verdict.failures)
        entries = [m for m in logs.output if "cannot cure" in m]
        self.assertEqual(len(entries), 1, entries)
        self.assertIn("namer_pair", entries[0])
        self.assertIn("+316.6", entries[0])
        rec = t4d._refusal(r)
        self.assertEqual(rec["placed_by"], "namer_pair")
        self.assertFalse(rec["restart_cures"])

    def test_a_pair_200_ms_off_still_acquires(self):
        """Inside the bound the pair's slack is not criterion 7's to
        judge; the authority's anchor never reads the pair."""
        r = _wb6cxc(0.3411, pair_error_s=0.20)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)


class TestNoPlacementSaysSo(unittest.TestCase):

    def test_no_pair_holds_and_logs_once(self):
        base = _base_for_raw(0.3411)
        r, _utc = t4d._nd_recorder(host=None, base=base)
        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            r = t4d._drive_nd(r, lambda rtp, ci=None: None, base=base)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        lines = [m for m in logs.output if "no placement to judge" in m]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("pair is unavailable", lines[0])
        self.assertIsNone(t4d._refusal(r))


if __name__ == "__main__":
    unittest.main()
