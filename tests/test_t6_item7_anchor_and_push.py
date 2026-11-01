"""v3.85 item 7: one anchor builder, no "effective" delay, no blind push.

(a) The acquisition path built a coarse "namer" anchor at named second +
    placement.  The placement reads radiod's host-clock tie, so that anchor
    copied the tie into T6's registration and overwrote the authority's
    own until the next fold block.  Anchors now come only from
    ``T6AnchorAuthority._build_anchor``; acquisition accepts, and the gate
    latches on the authority's anchor.
(b) The "effective chain_delay" figure is where radiod's tie placed the
    edge in its second -- raw counter phase + disambiguation -- not a
    delay.  WB6CXC-7 read -166.9 ms one start and +13.3 ms the next on one
    cable.  It leaves the archive sidecars (the asserted delay the anchor
    applies goes there) and the logs call it a placement.
(c) With no arrival-floor estimate the HPPS push fell back to the
    push-time construction, which publishes transport latency (13-45 ms on
    B4) as clock error.  It now skips the push, saying so once per episode.
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_t6_acquire_on_fold as fold
import test_t6_acquisition_gate as gate
import test_t6_placement_wb6cxc as wb
import test_t6_t4_disambiguation as t4d
from hf_timestd.core import core_recorder_v2 as crv2
from hf_timestd.core.t5_rtp_pairing import T5RtpPairing
from hf_timestd.core.t6_arrival_floor import ArrivalFloorTracker

LOGGER = "hf_timestd.core.core_recorder_v2"
SRC = (Path(__file__).resolve().parent.parent / "src" / "hf_timestd"
       / "core" / "core_recorder_v2.py").read_text()


# ── (a) one anchor builder ────────────────────────────────────────────

class TestAcquisitionBuildsNoAnchor(unittest.TestCase):

    def test_accepting_leaves_the_authoritys_anchor_alone(self):
        r = fold._recorder()
        authority_anchor = SimpleNamespace(captured_via_tier="T6")
        r._t6_native_anchor = authority_anchor
        fold._feed_blocks(r)
        fold._acquire(r)
        self.assertIs(r._t6_native_anchor, authority_anchor)
        self.assertIsNotNone(r._t6_acquisition_accepted)

    def test_no_anchor_is_built_outside_the_authority(self):
        acq = SRC[SRC.index("def _t6_disambiguate_via_external_reference"):
                  SRC.index("def _wait_for_chrony_settled")]
        self.assertNotIn("NativeAnchor(", acq)

    def test_every_anchor_a_real_run_installs_is_the_authoritys(self):
        r = gate._recorder()
        seen = []
        r._t6_ledger_append = lambda a, **k: seen.append(a)
        r = gate._drive(r, 125.0)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        self.assertTrue(seen)
        self.assertEqual({a.captured_via_tier for a in seen}, {"T6"})
        self.assertEqual(r._t6_native_anchor.captured_via_tier, "T6")

    def test_accepted_without_an_authority_anchor_the_gate_stays_open(self):
        r = gate._recorder()
        # The authority runs but never installs its anchor.
        r._t6_apply_authority_decision = lambda d: None
        with self.assertLogs(LOGGER, level=logging.INFO) as logs:
            r = gate._drive(r, 125.0)
        self.assertIsNotNone(r._t6_acquisition_accepted)
        self.assertIsNone(r._t6_last_chain_delay_ns)
        waits = [m for m in logs.output if "awaiting the anchor authority" in m]
        self.assertEqual(len(waits), 1, waits)


# ── (b) a placement is not a delay ────────────────────────────────────

class _Writer:
    def __init__(self):
        self.calls = []

    def set_bpsk_metadata(self, chain_delay_ns, applied):
        self.calls.append((chain_delay_ns, applied))


class TestThePlacementStaysOutOfTheSidecars(unittest.TestCase):

    def test_sidecars_carry_the_asserted_delay(self):
        base = wb._base_for_raw(0.3411)
        r, utc = t4d._nd_recorder(host=None, base=base)
        w = _Writer()
        r.recorders = {"WWV_10000": SimpleNamespace(archive_writer=w)}
        # A placement 20 ms off the asserted delay, so the two differ.
        r = t4d._drive_nd(r, t4d._off(utc, 0.020), base=base)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        asserted = (r._t6_authority.delay_budget_ns
                    + r._t6_authority.filter_group_delay_ns)
        self.assertTrue(w.calls)
        self.assertEqual({c[0] for c in w.calls}, {asserted})
        self.assertNotIn(r._t6_last_chain_delay_ns, {c[0] for c in w.calls})

    def test_the_recorder_never_writes_the_chain_delay_store(self):
        for call in ("set_record(", "set_candidate(", ".observe("):
            hits = [line for line in SRC.splitlines()
                    if "chain_delay_store" in line and call in line]
            self.assertEqual(hits, [], call)

    def test_logs_call_it_a_placement(self):
        r = gate._recorder()
        with self.assertLogs(LOGGER, level=logging.INFO) as logs:
            gate._drive(r, 125.0)
        accepts = [m for m in logs.output if "initial accept" in m]
        self.assertTrue(accepts)
        for m in accepts + [m for m in logs.output if "DISAMBIG" in m]:
            self.assertNotIn("effective", m)
        self.assertIn("placed", accepts[0])


# ── (c) no floor, no push ─────────────────────────────────────────────

class _Shm:
    def __init__(self):
        self.pushes = []

    def update(self, **kw):
        self.pushes.append(kw)


def _pushing_recorder(floor):
    r = gate._recorder()
    r._t6_shm = _Shm()
    r._t6_shm_push_count = 0
    r._t6_last_pushed_rtp = None
    r._t6_last_local_minus_source_ns = 0
    r._t5_pairing = T5RtpPairing(mono_fn=lambda: crv2.time.monotonic())
    r._t6_arrival_floor = floor
    r._t6_dissent_sigma_floor_ns = lambda: None
    return r


class TestNoFloorNoPush(unittest.TestCase):

    def test_with_a_floor_the_feed_pushes(self):
        """Positive control: the fixture reaches the push at all."""
        r = gate._drive(_pushing_recorder(ArrivalFloorTracker()), 125.0)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        self.assertTrue(r._t6_shm.pushes, "the fixture never pushed")

    def test_without_a_floor_nothing_is_pushed_and_it_says_so_once(self):
        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            r = gate._drive(_pushing_recorder(None), 125.0)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        self.assertEqual(r._t6_shm.pushes, [])
        lines = [m for m in logs.output if "arrival floor has no estimate"
                 in m]
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse([m for m in logs.output
                          if "falling back to the push-time" in m])

    def test_the_floor_returning_ends_the_episode(self):
        class _Late(ArrivalFloorTracker):
            """No estimate until the stream clock passes 110 s."""
            def estimate(self, mono_now, record=True):
                if mono_now < 110.0:
                    return None
                return super().estimate(mono_now, record=record)

        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            r = gate._drive(_pushing_recorder(_Late()), 125.0)
        self.assertTrue(r._t6_shm.pushes)
        ends = [m for m in logs.output if "pushes resume" in m]
        self.assertEqual(len(ends), 1, ends)


if __name__ == "__main__":
    unittest.main()
