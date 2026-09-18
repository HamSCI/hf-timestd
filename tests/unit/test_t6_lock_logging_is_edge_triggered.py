"""The T6 lock message fires on the TRANSITION, not on every locked batch.

⛔ Measured on DASI-009.AI6VN 2026-09-18: 3,305 byte-identical
"T6 BPSK PPS LOCKED" lines in ONE MINUTE, 99,127 in half an hour, 198 MB of
journal — all reporting the same `chain_delay=92141276 ns, ok=34, noise=614`.

The condition was `pps_consecutive == consecutive_required` with no edge
detection.  `pps_consecutive` pins AT the requirement rather than counting
past it, so the equality held on every batch: ~50 per second at 96 kHz with
1920-sample batches.

This is not a tidiness complaint.  The spam drowned the diagnostics beside
it — the `edge_period` values needed to explain why that station would not
reach T6 were buried under its own success message.  Same defect class as
a71781a ("log radiod health on change, not 8,640 times a day"), same file.
"""
from __future__ import annotations

import logging

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class _Result:
    def __init__(self, consecutive, ok=34, noise=614):
        self.pps_consecutive = consecutive
        self.pps_ok = ok
        self.pps_noise = noise
        self.chain_delay_ns = 92_141_276
        self.chain_delay_samples = 8845.6
        self.locked = True


def _drive(recorder, results, caplog):
    """Replay the logging block over a sequence of per-batch results.

    Mirrors the source rather than importing an extracted helper, because
    the defect lived inline. Kept in step by
    `test_the_source_is_edge_triggered` below.
    """
    logger = logging.getLogger("hf_timestd.core.core_recorder_v2")
    out = []
    for result in results:
        locked_now = (result.pps_consecutive
                      >= recorder._t6_calibrator.consecutive_required)
        was = getattr(recorder, '_t6_logged_locked', False)
        if locked_now and not was:
            out.append(("LOCKED", result.pps_ok))
        elif was and not locked_now:
            out.append(("LOST", result.pps_ok))
        recorder._t6_logged_locked = locked_now
    return out


class _Rec:
    def __init__(self, required=10):
        self._t6_calibrator = type("C", (), {"consecutive_required": required})()


class TestEdgeTriggering:
    def test_a_sustained_lock_logs_ONCE(self, caplog):
        """The live defect: 50 lines per second while nothing changed."""
        r = _Rec()
        events = _drive(r, [_Result(10) for _ in range(500)], caplog)
        assert events == [("LOCKED", 34)], (
            f"a sustained lock produced {len(events)} messages; it must "
            f"produce exactly one")

    def test_consecutive_climbing_past_the_requirement_still_logs_once(self):
        """`>=`, not `==` — a counter that keeps climbing is still one lock."""
        r = _Rec()
        events = _drive(r, [_Result(c) for c in range(10, 60)], None)
        assert len(events) == 1 and events[0][0] == "LOCKED"

    def test_losing_and_retaking_logs_both_transitions(self):
        """The losing edge used to be silent, so a lock that dropped and
        re-took read as one continuous lock in the journal."""
        r = _Rec()
        seq = ([_Result(10)] * 5 + [_Result(0)] * 5 + [_Result(10)] * 5)
        kinds = [k for k, _ in _drive(r, seq, None)]
        assert kinds == ["LOCKED", "LOST", "LOCKED"]

    def test_never_locking_never_logs_a_lock(self):
        r = _Rec()
        assert _drive(r, [_Result(c) for c in range(0, 10)], None) == []

    def test_the_first_batch_already_locked_still_logs(self):
        """A recorder that comes up mid-lock must announce it once."""
        r = _Rec()
        assert _drive(r, [_Result(10)], None) == [("LOCKED", 34)]


class TestTheSourceIsEdgeTriggered:
    """Guard the real file, since the replay above could drift from it."""

    def _block(self):
        from pathlib import Path
        src = (Path(__file__).parents[2] / "src" / "hf_timestd" / "core"
               / "core_recorder_v2.py").read_text()
        i = src.index("T6 BPSK PPS LOCKED")
        return src[i - 1200:i + 400]

    def test_it_remembers_whether_it_was_already_locked(self):
        assert "_t6_logged_locked" in self._block(), (
            "the lock message has no memory of the previous batch, so a "
            "sustained lock will log on every one of them (~50/s)")

    def test_it_does_not_use_a_bare_equality_on_the_requirement(self):
        import re
        block = "\n".join(
            l for l in self._block().splitlines()
            if not l.strip().startswith("#"))
        bad = re.search(
            r"pps_consecutive\s*==\s*self\._t6_calibrator\.consecutive_required",
            block)
        assert bad is None, (
            "`pps_consecutive == consecutive_required` holds on EVERY locked "
            "batch because the counter pins at the requirement. Use a "
            "transition against the remembered state.")
