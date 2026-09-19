"""A running T6 that fails its battery keeps asserting, loudly.

Spec §5.1/§5.2: a failed self-consistency guardrail on a RUNNING T6 does
NOT demote.  Demoting would reproduce the exact problem this design
removes -- a transient trip handing the station back to a tier two orders
of magnitude worse.  T6 keeps the anchor, keeps publishing, names the
failing criterion, and alarms once per throttle period.  A recorder that
never acquired is untouched: acquisition owns that refusal.  An absent
estimate never reaches this path at all -- see
``tests/test_core_recorder_t6_fine_integration.py`` /
``tests/test_t6_battery.py`` for the liveness path that still degrades
loudly on absence.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.t6_battery import BatteryVerdict
from hf_timestd.core.authority_manager import ProbeResult, _flatten_t6
from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe


def _recorder():
    r = SimpleNamespace()
    r._t6_note_verdict = CoreRecorderV2._t6_note_verdict.__get__(r)
    r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r._t6_say_once_at = {}
    r._t6_suspect = ()
    r._t6_native_anchor = object()  # already acquired: T6 is running
    return r


def _verdict(passed, failures=(), blocks=5):
    return BatteryVerdict(passed=passed, failures=tuple(failures),
                          sigma_ms=0.001, criteria={"blocks": blocks})


class TestRunningT6(unittest.TestCase):

    def test_a_failing_battery_marks_suspect_and_keeps_the_anchor(self):
        r = _recorder()
        anchor = r._t6_native_anchor
        r._t6_note_verdict(_verdict(False, ("split_half",)))
        self.assertEqual(r._t6_suspect, ("split_half",))
        self.assertIs(r._t6_native_anchor, anchor,
                      "a failed guardrail must not withdraw the anchor")

    def test_recovery_clears_the_mark(self):
        r = _recorder()
        r._t6_note_verdict(_verdict(False, ("split_half",)))
        r._t6_note_verdict(_verdict(True))
        self.assertEqual(r._t6_suspect, ())

    def test_every_failing_criterion_is_named(self):
        r = _recorder()
        r._t6_note_verdict(_verdict(False, ("retention", "shape")))
        self.assertEqual(r._t6_suspect, ("retention", "shape"))

    def test_a_recorder_that_never_acquired_is_not_marked_suspect(self):
        """Acquisition owns the refusal before an anchor exists."""
        r = _recorder()
        r._t6_native_anchor = None
        r._t6_note_verdict(_verdict(False, ("split_half",)))
        self.assertEqual(r._t6_suspect, ())

    def test_the_alarm_is_throttled(self):
        """A persistent condition reported every cycle blinds the log it
        writes to.  This project has collected five such floods."""
        r = _recorder()
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            for _ in range(20):
                r._t6_note_verdict(_verdict(False, ("split_half",)))
        self.assertEqual(len(cm.output), 1, cm.output)


class TestHookedIntoTheExistingEvaluation(unittest.TestCase):
    """``_t6_evaluate_battery`` (Task 5) is the only place ``.evaluate()``
    runs.  Task 6 must ride that single call, not add a second one -- a
    second evaluation would double the block history criteria 2 (ruler)
    and 3 (unimodality) count and could refuse a perfectly good lock."""

    def _recorder(self, evaluate_fn):
        r = SimpleNamespace()
        r._t6_evaluate_battery = CoreRecorderV2._t6_evaluate_battery.__get__(r)
        r._t6_note_verdict = CoreRecorderV2._t6_note_verdict.__get__(r)
        r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
        r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
        r._t6_say_once_at = {}
        r._t6_suspect = ()
        r._t6_native_anchor = object()
        r._t6_last_chain_delay_ns = None
        r._t6_fine_stage = None
        r._t6_reported_sigma_ms = lambda: 0.5
        r._t6_battery = SimpleNamespace(evaluate=evaluate_fn)
        return r

    def test_one_fold_block_means_one_evaluate_call(self):
        calls = []

        def _evaluate(est, **kw):
            calls.append(1)
            return _verdict(True, blocks=len(calls))

        r = self._recorder(_evaluate)
        r._t6_evaluate_battery(SimpleNamespace(), None)
        self.assertEqual(len(calls), 1,
                         "one fold block must produce exactly one "
                         "battery.evaluate() call")

    def test_a_failing_verdict_from_the_hook_marks_suspect(self):
        r = self._recorder(
            lambda est, **kw: _verdict(False, ("split_half",)))
        r._t6_evaluate_battery(SimpleNamespace(), None)
        self.assertEqual(r._t6_suspect, ("split_half",))

    def test_note_verdict_raise_does_not_stop_t6_asserting(self):
        """Task 5's hazard: this call site is upstream of the authority
        call and fails closed behind its own guard.  A raise from
        ``_t6_note_verdict`` must not be swallowed as "battery could not
        be evaluated" -- the verdict WAS computed; only the suspect
        bookkeeping raised.  It must not null the standing verdict or
        propagate and take the fine stage down with it."""
        good_verdict = _verdict(True)
        r = self._recorder(lambda est, **kw: good_verdict)

        def _boom(_verdict):
            raise RuntimeError("boom")
        r._t6_note_verdict = _boom

        r._t6_evaluate_battery(SimpleNamespace(), None)  # must not raise

        self.assertIs(
            r._t6_last_verdict, good_verdict,
            "a raise from _t6_note_verdict must not null a verdict that "
            "was actually computed")


class TestSnapshotColumns(unittest.TestCase):
    """authority_snapshot gains t6_suspect_criteria / t6_battery_blocks
    beside the existing T6 columns, via the same four-hop path
    (t6_pps status -> BpskPpsProbe detail whitelist -> _flatten_t6 ->
    the sqlite column) as fine_search_mode/fine_coarse_unverified."""

    def test_suspect_criteria_reach_the_flattened_snapshot(self):
        snapshot = {}
        _flatten_t6(snapshot, ProbeResult(
            "T6", available=True, offset_ms=0.0, sigma_ms=0.001,
            detail={"suspect_criteria": "split_half,shape",
                    "battery_blocks": 7},
        ))
        self.assertEqual(snapshot["t6_suspect_criteria"], "split_half,shape")
        self.assertEqual(snapshot["t6_battery_blocks"], 7)

    def test_a_healthy_t6_reports_the_empty_string_not_none(self):
        snapshot = {}
        _flatten_t6(snapshot, ProbeResult(
            "T6", available=True, offset_ms=0.0, sigma_ms=0.001,
            detail={"suspect_criteria": "", "battery_blocks": 12},
        ))
        self.assertEqual(snapshot["t6_suspect_criteria"], "")
        self.assertEqual(snapshot["t6_battery_blocks"], 12)

    def test_a_producer_without_the_fields_reads_as_healthy_not_unknown(self):
        """Mixed producer/consumer versions must not read a pre-Task-6
        producer's silence as an active suspect mark."""
        snapshot = {}
        _flatten_t6(snapshot, ProbeResult(
            "T6", available=True, offset_ms=0.0, sigma_ms=0.001, detail={},
        ))
        self.assertEqual(snapshot["t6_suspect_criteria"], "")
        self.assertEqual(snapshot["t6_battery_blocks"], 0)

    def test_suspect_telemetry_survives_producer_to_flatten(self):
        """All three hops: the recorder's ``t6_pps`` status block ->
        BpskPpsProbe detail whitelist -> _flatten_t6."""
        import json
        import tempfile
        from datetime import datetime, timezone

        now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            status = Path(td) / "core-recorder-status.json"
            status.write_text(json.dumps({
                "timestamp": now.isoformat(),
                "t6_pps": {
                    "enabled": True,
                    "locked": True,
                    "pps_consecutive": 30,
                    "pps_ok": 1,
                    "pps_noise": 0,
                    "chain_delay_ns": 43_000_000,
                    "local_minus_source_ns": 1_000,
                    "suspect_criteria": "ruler",
                    "battery_blocks": 9,
                },
            }))
            probe = BpskPpsProbe(status_path=status, now_fn=lambda: now)
            result = probe.poll()
        self.assertTrue(result.available, result.reason)
        self.assertEqual(result.detail["suspect_criteria"], "ruler")
        self.assertEqual(result.detail["battery_blocks"], 9)

        snapshot = {"utc_published": now.isoformat()}
        _flatten_t6(snapshot, result)
        self.assertEqual(snapshot["t6_suspect_criteria"], "ruler")
        self.assertEqual(snapshot["t6_battery_blocks"], 9)


if __name__ == "__main__":
    unittest.main()
