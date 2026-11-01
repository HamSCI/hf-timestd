"""v3.85 item 1, follow-up: a qualified T4 names T6's second.

AC0G-ND carries an LBE-Mini.  It reports no PPS over USB, gpsdo-monitor
publishes ``pps_utc_sec`` null for it, and so the second-namer never
answers.  Naming then fell straight to radiod's pair -- the same tie that
came up 0.3-0.5 s off after the 2026-10-27 RX888 stall.  With the edge
16.6 ms into its second, a pair 0.45 s off leaves the wall
0.467 s from an integer (refused, > 0.4 s), and a pair 0.70 s off rounds
to the NEXT second with only 0.283 s of residual (accepted, wrong).  Either
way T6 cannot become authoritative.  ND's LAN T4 holds about 0.2 ms.

Michael's ruling (2026-11-01): T4 may name the second at startup.  The
order becomes NMEA / second-namer, then a qualified T4, then radiod's
pair as the last resort.  T4-versus-pair disagreement is reported, once
per episode, and never vetoes.
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.offset_judge import BenchReading

SR = 96000
NAMED = 1_700_000_000
CHAIN_S = 0.016618
EDGE_RTP = 12_000_000 + 1595
TRANSPORT_S = 0.020
T0 = float(NAMED) - 1000.0          # true UTC = mono + T0
LAN_REF = "C0A8080A (192.168.8.10)"
LOGGER = "hf_timestd.core.core_recorder_v2"


def _true_utc(rtp):
    return float(NAMED) + (rtp - EDGE_RTP) / SR + CHAIN_S


class _Pairing:
    """The T6 stream's latest arrival: a sample 2 s after the edge,
    landing one transport latency late."""

    def __init__(self):
        rtp = EDGE_RTP + 2 * SR
        self.mono = _true_utc(rtp) + TRANSPORT_S - T0
        self.latest_arrival = (rtp, self.mono)
        self.mono += 0.5

    def now_mono(self):
        return self.mono


def _judge(tier="T4", sigma_ns=200_000.0, stratum=2, host_error_s=0.0,
           pairing=None):
    def latest():
        m = pairing.now_mono()
        return BenchReading(tier=tier, utc=m + T0 + host_error_s,
                            sigma_ns=sigma_ns, mono=m,
                            detail={"stratum": stratum,
                                    "reference_id": LAN_REF})
    return SimpleNamespace(latest_host_clock_reading=latest)


def _recorder(host=None, nmea_edge_utc=None, **host_kw):
    """ND's shape: a second-namer that never answers, a pair, maybe T4."""
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR)
    r._t6_native_anchor = None
    r._t6_say_once_at = {}
    r._lb1421_probe = None
    r._t5_pairing = _Pairing()
    if host:
        r._offset_judge = _judge(pairing=r._t5_pairing, **host_kw)
    if nmea_edge_utc is None:
        # The Mini: pps_utc_sec null, so no reading ever names a second.
        r._t6_second_namer = SimpleNamespace(get_latest=lambda: None)
    else:
        reading = SimpleNamespace(pps_utc_sec=NAMED - 3)
        r._t6_second_namer = SimpleNamespace(get_latest=lambda: reading)
        r._t6_name_second_via_nmea = lambda _rtp: nmea_edge_utc
    return r


def _name(r, pair_error_s, edge_rtp=EDGE_RTP):
    with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                    side_effect=lambda rtp, _ci: _true_utc(rtp)
                    + pair_error_s):
        return r._t6_name_integer_second(edge_rtp)


class TestNdWithoutT4(unittest.TestCase):
    """The fixture reproduces both of ND's failures."""

    def test_a_pair_045_s_off_gives_up(self):
        self.assertIsNone(_name(_recorder(), 0.45))

    def test_a_pair_055_s_off_gives_up(self):
        self.assertIsNone(_name(_recorder(), 0.55))

    def test_a_pair_070_s_off_names_the_wrong_second(self):
        self.assertEqual(_name(_recorder(), 0.70), NAMED + 1)

    def test_the_pair_names_a_healthy_edge(self):
        r = _recorder()
        self.assertEqual(_name(r, 0.0), NAMED)
        self.assertEqual(r._t6_naming_source, "radiod_pair")


class TestNdWithAQualifiedT4(unittest.TestCase):

    def test_t4_names_the_right_second_whatever_the_pair_says(self):
        for err in (0.0, 0.45, 0.55, 0.70, -0.45):
            r = _recorder(host=True)
            self.assertEqual(_name(r, err), NAMED, f"pair error {err}")
            self.assertEqual(r._t6_naming_source, "T4")

    def test_t2_does_not_qualify(self):
        r = _recorder(host=True, tier="T2", sigma_ns=55e6)
        self.assertIsNone(_name(r, 0.45))
        self.assertEqual(_name(r, 0.70), NAMED + 1)
        self.assertEqual(r._t6_naming_source, "radiod_pair")
        self.assertIn("T2", r._t6_naming_t4_refusal)

    def test_a_loose_lan_clock_does_not_qualify(self):
        r = _recorder(host=True, sigma_ns=1.5e6)
        self.assertIsNone(_name(r, 0.45))
        self.assertIn("sigma", r._t6_naming_t4_refusal)

    def test_nmea_still_comes_first(self):
        """A naming reading outranks T4; T4 is not even consulted."""
        r = _recorder(host=True, nmea_edge_utc=NAMED + CHAIN_S)
        r._t6_edge_utc_via_t4 = mock.Mock(side_effect=AssertionError)
        self.assertEqual(_name(r, 0.45), NAMED)
        self.assertEqual(r._t6_naming_source, "nmea")
        r._t6_edge_utc_via_t4.assert_not_called()

    def test_t4_beyond_the_cell_names_nothing(self):
        """A qualified T4 that puts the edge 0.45 s from any integer
        refuses, as the NMEA branch does; it does not hand the question
        to the pair."""
        r = _recorder(host=True, host_error_s=0.43)
        self.assertIsNone(_name(r, 0.0))

    def test_a_t4_slip_is_still_reconciled(self):
        """T4 one second fast; the previous anchor's counter arithmetic
        says which second the edge belongs to, and wins."""
        r = _recorder(host=True, host_error_s=1.0)
        r._t6_native_anchor = SimpleNamespace(
            anchor_rtp=(EDGE_RTP - 30 * SR) & 0xFFFFFFFF,
            anchor_utc_ns=int(round((NAMED - 30 + CHAIN_S) * 1e9)))
        self.assertEqual(_name(r, 0.0), NAMED)


class TestTheDisagreementIsReportedOncePerEpisode(unittest.TestCase):

    def _lines(self, logs):
        return [m for m in logs.output if "radiod-pair" in m]

    def test_one_entry_line_for_a_lasting_disagreement(self):
        r = _recorder(host=True)
        with self.assertLogs(LOGGER, level=logging.INFO) as logs:
            for _ in range(10):
                self.assertEqual(_name(r, 0.45), NAMED)
        lines = self._lines(logs)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("+0.430", lines[0])
        self.assertIn("refused to name it", lines[0])
        self.assertAlmostEqual(r._t6_naming_vs_radiod_pair_s, 0.45 - 0.02,
                               places=3)

    def test_exit_then_a_new_episode(self):
        r = _recorder(host=True)
        with self.assertLogs(LOGGER, level=logging.INFO) as logs:
            _name(r, 0.45)
            _name(r, 0.45)
            _name(r, 0.0)
            _name(r, 0.0)
            _name(r, 0.70)
        lines = self._lines(logs)
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("agrees again", lines[1])
        self.assertIn(f"named {NAMED + 1}, the WRONG second", lines[2])

    def test_agreement_logs_nothing(self):
        r = _recorder(host=True)
        with mock.patch(f"{LOGGER}.logger") as log:
            for _ in range(5):
                _name(r, 0.0)
        log.warning.assert_not_called()
        log.info.assert_not_called()

    def test_the_report_never_vetoes(self):
        r = _recorder(host=True)
        self.assertEqual(_name(r, 0.70), NAMED)


if __name__ == "__main__":
    unittest.main()


class TestThroughTheRealCallback(unittest.TestCase):
    """ND end to end: counter origin off AND radiod's pair off by the same
    0.45 s, as one bad tie would leave both, with no naming reading.  The
    real namer, the real battery and the real authority."""

    def _run(self, host):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_t6_t4_disambiguation as t4d
        r, utc = t4d._nd_recorder(host=host)
        del r._t6_name_integer_second          # the real namer again
        off = lambda rtp, ci=None: utc(rtp, ci) + 0.45     # noqa: E731
        return t4d._drive_nd(r, off)

    def test_with_t4_nd_names_from_t4_and_acquires(self):
        import test_t6_t4_disambiguation as t4d
        r = self._run(t4d._host())
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        self.assertEqual(r._t6_naming_source, "T4")
        self.assertEqual(r._t6_acquisition_reference["second_named_by"],
                         "T4")
        self.assertEqual(r._t6_authority.state.value, "AUTHORITATIVE")

    def test_without_t4_nd_cannot_name_and_stays_out(self):
        r = self._run(None)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        self.assertNotEqual(r._t6_authority.state.value, "AUTHORITATIVE")
