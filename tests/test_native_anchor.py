"""Tests for the hf-timestd-native RTP→UTC anchor.

These cover the pure function ``utc_ns_at_rtp`` and the
``NativeAnchor`` dataclass.  Persistence-layer interactions
(ChainDelayStore schema v2 round-trip, cross-schema compatibility)
live in ``test_bpsk_chain_delay_store.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hf_timestd.core.bpsk_chain_delay_store import ChainDelayStore
from hf_timestd.core.native_anchor import (
    NativeAnchor,
    pps_firing_utc_ns,
    utc_ns_at_rtp,
)


# A representative anchor: 2026-05-30 19:54:17 UTC, 96 kHz channel.
ANCHOR_UTC_NS = 1_780_171_857_000_000_000
ANCHOR_RTP = 2_107_252_660
SR_96K = 96_000
CHAIN_DELAY_NS = 10_000_000  # 10 ms


def _anchor(
    *,
    rtp: int = ANCHOR_RTP,
    utc_ns: int = ANCHOR_UTC_NS + CHAIN_DELAY_NS,  # SAMPLE utc = PPS utc + chain
    sr: int = SR_96K,
    chain_delay: int = CHAIN_DELAY_NS,
    captured_utc_ns: int = ANCHOR_UTC_NS,
    tier: str = "T5",
) -> NativeAnchor:
    return NativeAnchor(
        anchor_rtp=rtp,
        anchor_utc_ns=utc_ns,
        sample_rate_hz=sr,
        chain_delay_ns=chain_delay,
        captured_at_utc_ns=captured_utc_ns,
        captured_via_tier=tier,
    )


# ---------------------------------------------------------------------
# Pure function — nominal arithmetic
# ---------------------------------------------------------------------


class TestUtcNsAtRtpNominal:
    def test_at_anchor_rtp_returns_anchor_utc(self):
        a = _anchor()
        assert utc_ns_at_rtp(a.anchor_rtp, a) == a.anchor_utc_ns

    def test_plus_one_sample_period_advances_one_period(self):
        a = _anchor()
        # +1 sample at 96 kHz = +1/96000 s = ~10416.6 ns; integer division
        # floors to 10416 ns (still well below sample period rounding noise).
        expected = a.anchor_utc_ns + 1_000_000_000 // SR_96K
        assert utc_ns_at_rtp(a.anchor_rtp + 1, a) == expected

    def test_plus_sample_rate_advances_one_second(self):
        a = _anchor()
        assert (
            utc_ns_at_rtp(a.anchor_rtp + SR_96K, a)
            == a.anchor_utc_ns + 1_000_000_000
        )

    def test_minus_sample_rate_retreats_one_second(self):
        a = _anchor()
        assert (
            utc_ns_at_rtp(a.anchor_rtp - SR_96K, a)
            == a.anchor_utc_ns - 1_000_000_000
        )

    def test_arbitrary_delta_round_trip(self):
        a = _anchor()
        for delta in (1, 47, 1024, -7, -SR_96K * 60, SR_96K * 3600):
            expected = a.anchor_utc_ns + 1_000_000_000 * delta // SR_96K
            assert utc_ns_at_rtp(
                (a.anchor_rtp + delta) & 0xFFFFFFFF, a,
                hint_utc_ns=expected,  # hint avoids wrap-epoch ambiguity at large delta
            ) == expected


# ---------------------------------------------------------------------
# 32-bit RTP wrap-epoch picker
# ---------------------------------------------------------------------


class TestWrapEpoch:
    def test_wrap_period_value(self):
        a = _anchor()
        # At 96 kHz: 2**32 / 96000 ≈ 44739.243 sec ≈ 12.43 hours.
        assert 44_700 * 1_000_000_000 < a.wrap_period_ns() < 44_800 * 1_000_000_000

    def test_anchor_plus_one_wrap_period_with_hint_matches_hint(self):
        a = _anchor()
        # An RTP equal to anchor_rtp again, but one wrap-period later
        # in real time: should map to anchor_utc + period when hinted.
        period_ns = a.wrap_period_ns()
        hint = a.anchor_utc_ns + period_ns
        # rtp wraps back to anchor_rtp after 2**32 samples.
        rtp_after_one_wrap = a.anchor_rtp & 0xFFFFFFFF
        result = utc_ns_at_rtp(rtp_after_one_wrap, a, hint_utc_ns=hint)
        # k=1 picked → base + period → anchor_utc + period (or within
        # one sample period of it, since the rtp wraps exactly back).
        assert abs(result - (a.anchor_utc_ns + period_ns)) < 1_000_000_000 // SR_96K + 1

    def test_no_hint_defaults_to_anchor_capture_epoch(self):
        a = _anchor()
        # Without a hint, the anchor's own captured_at_utc_ns guides
        # disambiguation — same as the wrap-epoch k=0 in the nominal
        # window, which is always correct within ±wrap/2 of capture.
        result_no_hint = utc_ns_at_rtp(a.anchor_rtp + 1, a)
        # Inside the same wrap epoch, no-hint result equals the
        # arithmetic value (k=0).
        assert result_no_hint == a.anchor_utc_ns + 1_000_000_000 // SR_96K

    def test_no_host_clock_dependency(self, monkeypatch):
        # Pure function — calling utc_ns_at_rtp must not consult
        # time.time() (the whole point of leaving rtp_to_wallclock
        # behind).  Trap any call.
        import time as _time
        monkeypatch.setattr(_time, "time", lambda: (_ for _ in ()).throw(
            AssertionError("utc_ns_at_rtp must not consult time.time()")
        ))
        a = _anchor()
        # Force a wrap-distant query so the hint path matters.
        period_ns = a.wrap_period_ns()
        hint = a.anchor_utc_ns + period_ns
        utc_ns_at_rtp(a.anchor_rtp, a, hint_utc_ns=hint)


# ---------------------------------------------------------------------
# Dataclass / JSON round-trip
# ---------------------------------------------------------------------


class TestNativeAnchorSerialisation:
    def test_round_trip(self):
        a = _anchor()
        round_tripped = NativeAnchor.from_json(a.to_json())
        assert round_tripped == a

    def test_to_json_truncates_anchor_rtp_to_32bit(self):
        a = _anchor(rtp=0xFFFFFFFF + 7)
        d = a.to_json()
        assert d["anchor_rtp"] == 6  # 0xFFFFFFFF + 7 & 0xFFFFFFFF
        assert 0 <= d["anchor_rtp"] <= 0xFFFFFFFF

    def test_frozen(self):
        a = _anchor()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            a.anchor_rtp = 99  # type: ignore[misc]


# ---------------------------------------------------------------------
# pps_firing_utc_ns convenience
# ---------------------------------------------------------------------


def test_pps_firing_utc_is_anchor_utc_minus_chain_delay():
    a = _anchor()
    assert pps_firing_utc_ns(a) == ANCHOR_UTC_NS  # which == anchor_utc_ns − chain_delay_ns


# ---------------------------------------------------------------------
# Persistence — schema v2 round-trip via ChainDelayStore
# ---------------------------------------------------------------------


class TestPersistenceV2:
    def test_v2_save_then_load_yields_same_anchor(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        a = _anchor()
        store.save(
            sample_rate=a.sample_rate_hz,
            effective_chain_delay_ns=a.chain_delay_ns,
            anchor=a,
        )
        loaded = store.load()
        assert loaded is not None
        assert loaded.schema == "v2"
        assert loaded.anchor == a

    def test_v1_save_then_load_yields_anchor_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.save(sample_rate=SR_96K, effective_chain_delay_ns=CHAIN_DELAY_NS)
        loaded = store.load()
        assert loaded is not None
        assert loaded.schema == "v1"
        assert loaded.anchor is None

    def test_unknown_schema_treated_as_absent(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schema": "v999",
            "saved_at_unix": 0.0,
            "sample_rate": SR_96K,
            "effective_chain_delay_ns": 0,
            "source": "MF",
        }))
        assert store.load() is None

    def test_v2_with_malformed_anchor_falls_back_to_v1_semantic(
        self, tmp_path: Path,
    ):
        # Schema labelled v2 but missing anchor fields — the entry's
        # chain_delay is still trusted (v1 semantic) but anchor=None.
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schema": "v2",
            "saved_at_unix": __import__("time").time(),
            "sample_rate": SR_96K,
            "effective_chain_delay_ns": CHAIN_DELAY_NS,
            "source": "MF",
            # anchor_rtp deliberately omitted → from_json raises
        }))
        loaded = store.load()
        assert loaded is not None
        assert loaded.anchor is None
        assert loaded.effective_chain_delay_ns == CHAIN_DELAY_NS
