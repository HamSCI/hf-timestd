"""v3.85 item 1, Fix A: with no T5, a qualified T4 places T6's edge.

AC0G-ND, 2026-10-27 to 10-31.  An RX888 stall crashed radiod, and every
radiod start after it drew an RTP counter origin 0.3-0.5 s off UTC.  The
matched filter's raw chain delay reads the edge's phase in that counter
(``idx = rtp % sr`` in the calibrator), so before acquisition criterion 7
judged -462.6, +438.4, -284.0, +473.4 ms and refused every one.  ND has
no T5, nothing else could place the edge, and the station ran on T4 for
4.5 days -- while its T4 (a LAN GPS + PPS server) held about 0.2 ms.

Two layers here:

* ``TestThroughTheRealCallback`` drives real BPSK samples through
  ``_t6_on_samples`` with radiod's counter origin displaced as ND's was,
  and radiod's pair still describing that counter consistently.
* The fold-harness classes call the two touched methods directly, for
  the cases the callback fixture cannot reach cheaply: radiod's pair
  itself off, T4 naming a different second, and T5 outranking T4.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_t6_acquisition_gate as gate
import test_t6_acquire_on_fold as fold
from hf_timestd.core import core_recorder_v2 as crv2
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.offset_judge import BenchReading
from hf_timestd.core.t4_reference import (
    T4_REFERENCE_MAX_SIGMA_NS, t4_reference_refusal)
from hf_timestd.core.t5_rtp_pairing import T5RtpPairing

# ND's T4: a LAN GPS + PPS server, about 0.2 ms.
ND_HOST_SIGMA_NS = 200_000.0
LAN_REF = "C0A8080A (192.168.8.10)"
# The arrival lands one transport latency after its sample.
TRANSPORT_S = 0.020

_T4_METHODS = ("_t6_edge_utc_via_t4", "_t6_implied_chain_delay_via_t4",
               "_t6_note_acquisition_refusal", "_t6_refusal_record",
               "_t6_clear_acquisition_refusal",
               "_t6_acquisition_refusal_status")


def _refusal(r):
    """The refusal record as the status file would carry it.

    Read at the record's own last refresh: the gate fixture drives
    ``time.monotonic`` from stream time, and the real clock afterwards
    would read every record as lapsed.
    """
    rec = r._t6_refusal_record()
    return None if rec.last_mono is None else rec.current(rec.last_mono)


def _host(tier="T4", sigma_ns=ND_HOST_SIGMA_NS, stratum=2, ref=LAN_REF):
    """A judge whose chrony reading tells the truth about UTC.

    ``utc_of_mono`` maps the test's monotonic clock to true UTC; the
    reading is taken at whatever ``mono_now`` returns when asked.
    """
    def make(utc_of_mono, mono_now):
        def latest():
            m = mono_now()
            return BenchReading(
                tier=tier, utc=utc_of_mono(m), sigma_ns=sigma_ns, mono=m,
                detail={"stratum": stratum, "reference_id": ref})
        return SimpleNamespace(latest_host_clock_reading=latest)
    return make


# ── the qualification itself ──────────────────────────────────────────

class TestWhatQualifiesAsT4(unittest.TestCase):

    def _r(self, **kw):
        base = dict(tier="T4", utc=1.0, sigma_ns=ND_HOST_SIGMA_NS,
                    mono=100.0, detail={"stratum": 2,
                                        "reference_id": LAN_REF})
        base.update(kw)
        return BenchReading(**base)

    def test_nds_lan_stratum_one_qualifies(self):
        self.assertIsNone(t4_reference_refusal(self._r(), 101.0))

    def test_a_local_refclock_qualifies(self):
        r = self._r(detail={"stratum": 1, "reference_id": "47505300 (GPS)"})
        self.assertIsNone(t4_reference_refusal(r, 101.0))

    def test_wan_time_never_qualifies(self):
        why = t4_reference_refusal(self._r(tier="T2", sigma_ns=55e6), 101.0)
        self.assertIn("T2", why)

    def test_wan_time_does_not_qualify_even_when_chrony_reports_it_tight(self):
        """Tier and sigma are separate gates: a WAN source with a small
        reported budget is still a WAN source."""
        self.assertIsNotNone(
            t4_reference_refusal(self._r(tier="T2", sigma_ns=300_000.0),
                                 101.0))

    def test_sigma_over_one_millisecond_refuses(self):
        why = t4_reference_refusal(
            self._r(sigma_ns=T4_REFERENCE_MAX_SIGMA_NS + 1.0), 101.0)
        self.assertIn("sigma", why)

    def test_sigma_at_the_bar_qualifies(self):
        self.assertIsNone(t4_reference_refusal(
            self._r(sigma_ns=T4_REFERENCE_MAX_SIGMA_NS), 101.0))

    def test_the_bar_is_the_judges_disciplined_host_bar(self):
        from hf_timestd.core.label_plane import LabelPlaneTracker
        self.assertEqual(T4_REFERENCE_MAX_SIGMA_NS,
                         LabelPlaneTracker.DEFAULT_MAX_HOST_SIGMA_NS)
        self.assertEqual(T4_REFERENCE_MAX_SIGMA_NS, 1_000_000.0)

    def test_a_lan_relay_of_wan_time_refuses_on_stratum(self):
        r = self._r(detail={"stratum": 3, "reference_id": LAN_REF})
        self.assertIn("stratum", t4_reference_refusal(r, 101.0))

    def test_chrony_following_t6_itself_refuses(self):
        r = self._r(detail={"stratum": 1,
                            "reference_id": "48505053 (HPPS)"})
        self.assertIn("HPPS", t4_reference_refusal(r, 101.0))

    def test_chrony_following_fusion_refuses(self):
        r = self._r(detail={"stratum": 1,
                            "reference_id": "46555345 (FUSE)"})
        self.assertIn("FUSE", t4_reference_refusal(r, 101.0))

    def test_a_stale_reading_refuses(self):
        self.assertIn("old", t4_reference_refusal(self._r(), 161.0))

    def test_no_reading_refuses(self):
        self.assertIn("no host-clock reading",
                      t4_reference_refusal(None, 101.0))


class TestTheJudgeHandsOverItsChronyReading(unittest.TestCase):
    """The recorder reads chrony through the judge's own tick, so the T6
    path gains no chronyc caller of its own."""

    class _Bench:
        def __init__(self, tier):
            self.tier = tier

        def poll(self):
            if self.tier is None:
                return None
            return BenchReading(tier=self.tier, utc=1.7e9, sigma_ns=2e5,
                                mono=5.0)

    def _judge(self, tmp, *benches):
        from hf_timestd.core.offset_judge import OffsetJudge
        return OffsetJudge(config={"enabled": True},
                           benches=list(benches),
                           publish_path=Path(tmp) / "oj.json",
                           time_fn=lambda: 1.7e9, mono_fn=lambda: 5.0)

    def test_the_latest_chrony_reading_is_handed_over(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            j = self._judge(tmp, self._Bench("T3"), self._Bench("T4"))
            self.assertIsNone(j.latest_host_clock_reading())
            j.tick()
            self.assertEqual(j.latest_host_clock_reading().tier, "T4")

    def test_a_tick_without_chrony_forgets_the_last_reading(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bench = self._Bench("T4")
            j = self._judge(tmp, bench)
            j.tick()
            bench.tier = None
            j.tick()
            self.assertIsNone(j.latest_host_clock_reading())

    def test_a_sample_derived_tier_is_never_handed_over(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            j = self._judge(tmp, self._Bench("T3"), self._Bench("T6"))
            j.tick()
            self.assertIsNone(j.latest_host_clock_reading())


# ── through the real callback ─────────────────────────────────────────

# ND's draw: radiod's counter origin 520.8 ms off UTC, so the edge's raw
# phase reads 16.618 + 520.782 = 537.4 ms, folded to -462.6 ms -- the
# first refusal ND logged on 2026-10-27.
ND_ORIGIN_SAMPLES = int(round(0.520782 * gate.SR))
ND_BASE = gate.RTP0 + ND_ORIGIN_SAMPLES


def _utc_of_rtp(base):
    """radiod's pair, describing its own (displaced) counter correctly."""
    def f(rtp, _ci=None):
        return (float(gate.NAMED_BASE) + (rtp - base - gate.EDGE) / gate.SR
                + gate.CHAIN_DELAY_SEC)
    return f


def _nd_recorder(host=None, base=ND_BASE):
    r = gate._recorder()
    utc = _utc_of_rtp(base)
    r._t6_name_integer_second = (
        lambda rtp: gate.NAMED_BASE + int(round(
            (rtp - base - gate.EDGE) / gate.SR)))
    # The stream clock _drive installs is crv2.time.monotonic, read at
    # call time so the pairing and the judge share it.
    mono = lambda: crv2.time.monotonic()            # noqa: E731
    r._t5_pairing = T5RtpPairing(mono_fn=mono)
    # _drive sets mono = i / SR at the batch that starts at sample i, and
    # its quality carries no batch length, so the pairing records sample
    # base + i as the arrival.  True UTC at mono m is therefore the UTC
    # of sample base + m*SR, plus the transport latency.
    utc_of_mono = (lambda m: utc(base + m * gate.SR) + TRANSPORT_S)
    if host is not None:
        r._offset_judge = host(utc_of_mono, mono)
    return r, utc


def _drive_nd(r, utc, base=ND_BASE, seconds=125.0):
    with mock.patch.object(gate, "_rtp_to_utc", side_effect=utc):
        return gate._drive(r, seconds, rtp_base=base)


class TestThroughTheRealCallback(unittest.TestCase):

    def test_the_fixture_reproduces_nds_refusal(self):
        """No T4: criterion 7 judges the raw phase and refuses, exactly
        the value ND logged, and the refusal is published as one a
        recorder restart cannot cure."""
        r, utc = _nd_recorder(host=None)
        r = _drive_nd(r, utc)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        self.assertIn("plausibility", r._t6_last_verdict.failures)
        self.assertGreater(r._t6_initial_accept_rejections, 0)
        rec = _refusal(r)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["reason"], "chain_delay_implausible")
        self.assertFalse(rec["restart_cures"])
        self.assertAlmostEqual(rec["effective_ms"], -462.6, delta=0.2)

    def test_a_qualified_t4_lets_nd_acquire(self):
        r, utc = _nd_recorder(host=_host())
        r = _drive_nd(r, utc)
        self.assertIsNotNone(
            r._t6_last_chain_delay_ns,
            f"T4 at 0.2 ms did not let T6 acquire; verdict "
            f"{r._t6_last_verdict.failures}")
        # radiod's pair is sound here, so it places the sub-second; T4
        # only stood in for the raw phase in criterion 7.
        self.assertAlmostEqual(r._t6_last_chain_delay_ns / 1e9,
                               gate.CHAIN_DELAY_SEC, places=4)
        ref = r._t6_acquisition_reference
        self.assertEqual(ref["battery_plausibility"], "T4")
        self.assertEqual(ref["placed_by"], "radiod_pair")
        self.assertIsNone(_refusal(r))

    def test_wan_time_leaves_the_refusal_standing(self):
        r, utc = _nd_recorder(host=_host(tier="T2", sigma_ns=55e6))
        r = _drive_nd(r, utc)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        rec = _refusal(r)
        self.assertIsNotNone(rec)
        self.assertIn("T2", rec["t4_reference"])

    def test_a_loose_lan_clock_leaves_the_refusal_standing(self):
        r, utc = _nd_recorder(host=_host(sigma_ns=1.5e6))
        r = _drive_nd(r, utc)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        self.assertIn("sigma",
                      _refusal(r)["t4_reference"])

    def test_a_hold_t4_cannot_explain_is_not_published_as_incurable(self):
        """T4 clears criterion 7, but the cascade cannot name the second.
        The raw phase still trips the initial-accept guard on every walk,
        yet it no longer blocks anything, so the record must not tell
        hpps-watchdog that a chain-delay refusal holds the station."""
        r, utc = _nd_recorder(host=_host())
        r._t6_name_integer_second = lambda rtp: None
        r = _drive_nd(r, utc)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        self.assertEqual(r._t6_battery_plausibility_ref, "T4")
        self.assertGreater(r._t6_initial_accept_rejections, 0)
        self.assertIsNone(_refusal(r))

    def test_a_healthy_station_acquires_as_before(self):
        """The counter origin on UTC: T4 present or not, the same
        anchor."""
        plain, utc0 = _nd_recorder(host=None, base=gate.RTP0)
        plain = _drive_nd(plain, utc0, base=gate.RTP0)
        with_t4, utc1 = _nd_recorder(host=_host(), base=gate.RTP0)
        with_t4 = _drive_nd(with_t4, utc1, base=gate.RTP0)
        self.assertIsNotNone(plain._t6_last_chain_delay_ns)
        self.assertEqual(plain._t6_last_chain_delay_ns,
                         with_t4._t6_last_chain_delay_ns)


# ── the fold harness: radiod's pair itself off ────────────────────────

class _Clock:
    """Monotonic seconds; true UTC = mono + T0."""
    T0 = float(fold.NAMED_SECOND) - 1000.0

    def __init__(self):
        self.mono = 0.0


class _FakePairing:
    def __init__(self, clock):
        self.clock = clock
        self.latest_arrival = None

    def now_mono(self):
        return self.clock.mono

    def arrive_after(self, edge_rtp, seconds=2.0):
        """The sample `seconds` after the edge arrives TRANSPORT_S late."""
        rtp = int(edge_rtp + seconds * fold.SR)
        self.clock.mono = (fold._wall_of(rtp) + TRANSPORT_S) - _Clock.T0
        self.latest_arrival = (rtp & 0xFFFFFFFF, self.clock.mono)
        self.clock.mono += 0.5


def _fold_recorder(named=fold.NAMED_SECOND, host=None, t5=None):
    r = fold._recorder(named=named)
    for name in _T4_METHODS:
        setattr(r, name, getattr(CoreRecorderV2, name).__get__(r))
    r.T6_NAMING_MAX_EDGE_AGE_SEC = CoreRecorderV2.T6_NAMING_MAX_EDGE_AGE_SEC
    clock = _Clock()
    r._fixture_clock = clock
    r._t5_pairing = _FakePairing(clock)
    r._lb1421_probe = t5
    if host is not None:
        r._offset_judge = host(lambda m: m + _Clock.T0,
                               lambda: clock.mono)
    return r


def _feed(r, n=fold.BLOCKS):
    """fold._feed_blocks, with the T6 stream arriving as blocks land."""
    for _ in range(n):
        i = r._fixture_next_block
        r._fixture_next_block = i + 1
        edge = fold.EDGE_RTP + i * fold.SR * fold.K
        r._t5_pairing.arrive_after(edge)
        est = fold._estimate(edge)
        r._t6_last_fine_est = est
        r._t6_evaluate_battery(est, fold._result())


class TestRadiodsPairOff(unittest.TestCase):
    """radiod's (GPS_TIME, RTP_TIMESNAP) pair 0.46 s off.  The counter
    phase is fine, so criterion 7 passes on the raw value either way; the
    external-reference path's own plausibility check is what refuses."""

    PAIR_ERROR_S = 0.46

    def test_without_t4_the_refusal_stands_and_is_recorded(self):
        r = _fold_recorder()
        _feed(r)
        fold._acquire(r, wall_error_sec=self.PAIR_ERROR_S)
        self.assertIsNone(r._t6_native_anchor)
        rec = _refusal(r)
        self.assertIsNotNone(rec)
        self.assertFalse(rec["restart_cures"])

    def test_a_qualified_t4_places_the_edge(self):
        r = _fold_recorder(host=_host())
        _feed(r)
        fold._acquire(r, wall_error_sec=self.PAIR_ERROR_S)
        a = r._t6_native_anchor
        self.assertIsNotNone(a)
        self.assertEqual(a.captured_via_tier, "T4")
        self.assertEqual(a.captured_at_utc_ns,
                         fold.LAST_NAMED * 1_000_000_000)
        # The arrival pairing reads late by the transport latency, and
        # the capture says so in its sigma rather than hiding it.
        self.assertAlmostEqual(a.chain_delay_ns / 1e9,
                               fold.CHAIN_DELAY_SEC + TRANSPORT_S,
                               places=4)
        ref = r._t6_acquisition_reference
        self.assertEqual(ref["placed_by"], "T4")
        self.assertEqual(ref["t4"]["host_sigma_ns"], ND_HOST_SIGMA_NS)
        self.assertGreaterEqual(ref["t4"]["sigma_ns"],
                                T5RtpPairing.SIGMA_FLOOR_NS)
        self.assertEqual(ref["t4"]["reference_id"], LAN_REF)

    def test_t4_naming_a_different_second_refuses(self):
        """The cascade names the wrong second (one too late).  T4 names
        the right one.  Two references disagreeing by a whole second is a
        fault to expose, not one to settle by preference."""
        r = _fold_recorder(named=fold.NAMED_SECOND + 1, host=_host())
        _feed(r)
        fold._acquire(r, wall_error_sec=self.PAIR_ERROR_S)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("names second",
                      _refusal(r)["t4_reference"])

    def test_a_live_t5_outranks_t4(self):
        t5 = SimpleNamespace(get_latest=lambda: object())
        r = _fold_recorder(host=_host(), t5=t5)
        _feed(r)
        fold._acquire(r, wall_error_sec=self.PAIR_ERROR_S)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("T5",
                      _refusal(r)["t4_reference"])

    def test_t4_cannot_rescue_an_edge_it_also_finds_implausible(self):
        """A host clock 300 ms wrong (an impossible T4, but the guard
        must not trust the qualification alone)."""
        r = _fold_recorder()
        _feed(r)            # criterion 7 on the raw phase, which passes
        r._offset_judge = _host()(
            lambda m: m + _Clock.T0 + 0.30, lambda: r._fixture_clock.mono)
        fold._acquire(r, wall_error_sec=self.PAIR_ERROR_S)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("also places",
                      _refusal(r)["t4_reference"])


class TestTheBatteryJudgesT4OnlyBeforeAcquisition(unittest.TestCase):

    def test_after_acquisition_the_battery_judges_the_anchor_again(self):
        r = _fold_recorder(host=_host())
        _feed(r)
        self.assertEqual(r._t6_battery_plausibility_ref, "T4")
        fold._acquire(r)
        self.assertIsNotNone(r._t6_native_anchor)
        r._t6_last_chain_delay_ns = 1          # the caller's latch
        _feed(r, n=1)
        self.assertEqual(r._t6_battery_plausibility_ref, "raw")


if __name__ == "__main__":
    unittest.main()
