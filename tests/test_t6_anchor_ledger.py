"""T6 anchor ledger — a durable, append-only record of every anchor.

Motivation (mjh, 2026-08-24): the anchor tuple is the minimal durable
record of T6.  ``anchor_utc_ns = named_second·1e9 + asserted − sub_ns``,
so a ledger row that carries the raw components separately makes every
future recalibration of the asserted terms pure arithmetic — retroactive
correction with no raw-IQ archive.  Until now the tuples lived only in
300 s-throttled journal lines, which rotate (everything before
2026-08-23 is already gone on AC0G-B4).
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.t6_anchor_ledger import T6AnchorLedger

SECOND = 1_700_000_000
ASSERTED = 16_628_000  # delay_budget 10 µs + filter group delay 16.618 ms
SUB_NS = 371  # fine-stage subsample, the measured term


def make_anchor(rtp=1_000_000, tier="T6"):
    return NativeAnchor(
        anchor_rtp=rtp,
        anchor_utc_ns=SECOND * 1_000_000_000 + ASSERTED - SUB_NS,
        sample_rate_hz=96_000,
        chain_delay_ns=ASSERTED,
        captured_at_utc_ns=SECOND * 1_000_000_000,
        captured_via_tier=tier,
    )


def read_rows(dir_path):
    rows = []
    for f in sorted(Path(dir_path).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rows.append(json.loads(line))
    return rows


class TestAppend:
    def test_appends_one_json_line_with_all_components(self, tmp_path):
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 1_700_000_123.5)
        assert led.append(
            make_anchor(), authority_state="AUTHORITATIVE",
            delay_budget_ns=10_000, filter_group_delay_ns=16_618_000,
        ) is True
        rows = read_rows(tmp_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["anchor_rtp"] == 1_000_000
        assert r["anchor_utc_ns"] == SECOND * 10**9 + ASSERTED - SUB_NS
        assert r["named_second_utc_ns"] == SECOND * 10**9
        assert r["sample_rate_hz"] == 96_000
        assert r["chain_delay_ns"] == ASSERTED
        assert r["captured_via_tier"] == "T6"
        assert r["authority_state"] == "AUTHORITATIVE"
        assert r["delay_budget_ns"] == 10_000
        assert r["filter_group_delay_ns"] == 16_618_000
        assert r["logged_at_unix"] == 1_700_000_123.5

    def test_file_is_named_by_utc_day(self, tmp_path):
        # 1_700_000_123 = 2023-11-14 UTC
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 1_700_000_123.0)
        led.append(make_anchor())
        assert (tmp_path / "t6-anchors-20231114.jsonl").exists()

    def test_recalibration_is_pure_arithmetic_over_a_row(self, tmp_path):
        """The property the ledger exists for: given a row, a NEW
        asserted chain delay yields a corrected anchor with no other
        information — sub_ns is recoverable from the stored fields."""
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 0.0)
        led.append(make_anchor(), delay_budget_ns=10_000,
                   filter_group_delay_ns=16_618_000)
        r = read_rows(tmp_path)[0]
        sub_ns = r["named_second_utc_ns"] + r["chain_delay_ns"] - r["anchor_utc_ns"]
        assert sub_ns == SUB_NS
        new_asserted = 15_000_000  # a future, better calibration
        corrected = r["named_second_utc_ns"] + new_asserted - sub_ns
        assert corrected == SECOND * 10**9 + new_asserted - SUB_NS

    def test_identical_anchor_is_appended_once(self, tmp_path):
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 0.0)
        assert led.append(make_anchor()) is True
        assert led.append(make_anchor()) is False
        assert len(read_rows(tmp_path)) == 1
        # A genuinely new anchor (next fold block) is appended.
        assert led.append(make_anchor(rtp=1_096_000)) is True
        assert len(read_rows(tmp_path)) == 2

    def test_append_never_raises_on_an_unwritable_dir(self, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            led = T6AnchorLedger(dir_path=ro / "ledger", now_fn=lambda: 0.0)
            assert led.append(make_anchor()) is False  # logged, not raised
        finally:
            os.chmod(ro, 0o700)

    def test_none_anchor_is_a_noop(self, tmp_path):
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 0.0)
        assert led.append(None) is False
        assert read_rows(tmp_path) == []


class TestRecorderWiring:
    """An AUTHORITATIVE authority decision lands in the ledger."""

    def _recorder(self, tmp_path):
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
        from hf_timestd.core.t6_anchor_authority import T6AnchorAuthority
        r = CoreRecorderV2.__new__(CoreRecorderV2)
        r._t6_channel_info = SimpleNamespace()
        r._t6_native_anchor = None
        r._t6_authority = T6AnchorAuthority(
            96_000, 10_000, filter_group_delay_ns=16_618_000)
        r._t6_authority_last_decision = None
        r._t6_rate_reset = lambda reason: None
        r._t6_anchor_ledger = T6AnchorLedger(
            dir_path=tmp_path, now_fn=lambda: 0.0)
        return r

    def _estimate(self, rtp=1_000_000):
        from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
        return FineEdgeEstimate(
            edge_offset_samples=43_181.0, edge_rtp=rtp, edge_subsample=0.0,
            n_seconds_folded=30, plateau_amplitude=30.0, fit_rms=0.05,
        )

    def test_authoritative_decision_is_ledgered(self, tmp_path):
        r = self._recorder(tmp_path)
        e = self._estimate()
        d = r._t6_authority.on_fine_estimate(
            e, (e.edge_rtp + e.edge_subsample) % 96_000, SECOND)
        r._t6_apply_authority_decision(d)
        rows = read_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["captured_via_tier"] == "T6"
        assert rows[0]["authority_state"] == "AUTHORITATIVE"
        assert rows[0]["delay_budget_ns"] == 10_000
        assert rows[0]["filter_group_delay_ns"] == 16_618_000

    def test_repeated_decision_with_same_anchor_appends_once(self, tmp_path):
        r = self._recorder(tmp_path)
        e = self._estimate()
        d = r._t6_authority.on_fine_estimate(
            e, (e.edge_rtp + e.edge_subsample) % 96_000, SECOND)
        r._t6_apply_authority_decision(d)
        r._t6_apply_authority_decision(d)
        assert len(read_rows(tmp_path)) == 1

    def test_recorder_without_ledger_attribute_is_fine(self, tmp_path):
        # Test-harness recorders built via __new__ predate the ledger;
        # the wiring must be getattr-guarded like the stores are.
        r = self._recorder(tmp_path)
        del r._t6_anchor_ledger
        e = self._estimate()
        d = r._t6_authority.on_fine_estimate(
            e, (e.edge_rtp + e.edge_subsample) % 96_000, SECOND)
        r._t6_apply_authority_decision(d)  # must not raise
