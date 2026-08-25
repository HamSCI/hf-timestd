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


class TestSchemaV2:
    """The ledger is the archival substitute for the T6 IQ stream.

    Archiving the 96 kHz T6 channel costs ~60 GB/day (measured on B4: a
    24 kHz channel-day is 15 GB); the ledger costs 0.37 MB/day -- five
    orders of magnitude less.  So the alignment record stands in for the
    samples (mjh, 2026-08-25).  For that to be honest a row must carry
    three things it did not:

    1. a position in the METROLOGY counter space.  ``anchor_rtp`` is in
       the T6 channel's own 96 kHz space, and that space does NOT relate
       to the 24 kHz metrology channels by scaling -- measured offset
       362,095,021 samples (~3772 s).  A reader holding the ledger and
       the metrology IQ could not connect them.
    2. the matched filter's quality metrics, so a later reader can JUDGE
       an anchor rather than inherit it.  This is what archiving IQ would
       have bought and the ledger cannot: you cannot re-run the MF.  Both
       the 2026-05-23 sidelobe phantom and the 2026-08-25 livelock were
       precise-looking anchors that were wrong.
    3. continuity evidence -- an anchor only labels correctly if the
       counter stayed continuous since capture.

    Plus ``labeling_convention``, whose absence is exactly what made the
    2026-08-25 15:00-15:07 content window indistinguishable afterwards,
    and a ``schema`` field so generations can be told apart at all
    (hf-timestd#39).
    """

    def _write(self, tmp_path, **kw):
        led = T6AnchorLedger(dir_path=tmp_path, now_fn=lambda: 1_700_000_000.0)
        assert led.append(make_anchor(), **kw)
        return read_rows(tmp_path)[0]

    def test_row_declares_its_schema(self, tmp_path):
        assert self._write(tmp_path)["schema"] == "t6-anchor/2"

    def test_labeling_convention_is_recorded(self, tmp_path):
        row = self._write(tmp_path, labeling_convention="content")
        assert row["labeling_convention"] == "content"

    def test_peer_space_position_is_recorded(self, tmp_path):
        """The number that lets an offline reader use the metrology IQ."""
        row = self._write(
            tmp_path, peer_rtp=1_701_842_846, peer_rate_hz=24_000)
        assert row["peer_rtp"] == 1_701_842_846
        assert row["peer_rate_hz"] == 24_000

    def test_quality_metrics_are_recorded(self, tmp_path):
        row = self._write(tmp_path, quality={
            "plateau_amplitude": 30.0, "fit_rms": 0.05,
            "n_seconds_folded": 30, "edge_subsample": 0.25})
        assert row["quality"]["fit_rms"] == 0.05
        assert row["quality"]["n_seconds_folded"] == 30

    def test_continuity_drift_is_recorded(self, tmp_path):
        assert self._write(tmp_path, label_drift_samples=0)[
            "label_drift_samples"] == 0

    def test_absent_optionals_stay_absent_not_falsely_zero(self, tmp_path):
        """A missing measurement must not read as a measured zero -- that
        is the whole failure mode this schema exists to prevent."""
        row = self._write(tmp_path)
        for k in ("peer_rtp", "peer_rate_hz", "quality",
                  "label_drift_samples", "labeling_convention"):
            assert k not in row, f"{k} should be omitted, not defaulted"

    def test_v1_fields_are_unchanged(self, tmp_path):
        """Readers of the old shape keep working."""
        row = self._write(tmp_path)
        for k in ("logged_at_unix", "anchor_rtp", "anchor_utc_ns",
                  "named_second_utc_ns", "sample_rate_hz",
                  "chain_delay_ns", "captured_via_tier"):
            assert k in row

    def test_append_still_never_raises(self, tmp_path):
        """Hot-path discipline survives the extra fields."""
        led = T6AnchorLedger(dir_path=tmp_path / "nope" / "deeper",
                             now_fn=lambda: 1_700_000_000.0)
        bad = object()   # unserialisable
        assert led.append(make_anchor(), quality=bad) is False
