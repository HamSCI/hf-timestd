"""Tests for the BPSK chain_delay persistence store.

The store keeps the last-known-good *effective* chain_delay (= raw +
disambiguation) per detector source so a freshly-restarted calibrator
can re-derive its own disambiguation_ns from an invariant — instead of
re-walking chrony tracking, which drifts continuously and produces a
different answer every restart.

Coverage:
  * save → load round-trip
  * staleness gate (> 1 h returns None)
  * future-dated guard (clock went backward)
  * absent / malformed / wrong-schema / wrong-source files all return None
  * atomic-replace semantics (no half-written file)
  * compute_disambiguation_ns aligns to nearest integer sample for both
    forward and backward shifts and across the wrap boundary
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hf_timestd.core.bpsk_chain_delay_store import (
    ChainDelayStore,
    DEFAULT_STALENESS_S,
    PersistedChainDelay,
    compute_disambiguation_ns,
)


SR = 96_000


class TestRoundTrip:
    def test_save_then_load_returns_same_value(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.save(
            sample_rate=SR,
            effective_chain_delay_ns=559_121_877,
            now_unix=1_700_000_000.0,
        )
        entry = store.load(now_unix=1_700_000_010.0)  # 10 s later — fresh
        assert entry is not None
        assert entry.sample_rate == SR
        assert entry.effective_chain_delay_ns == 559_121_877
        assert entry.source == "MF"
        assert entry.saved_at_unix == 1_700_000_000.0

    def test_diff_and_mf_files_are_separate(self, tmp_path: Path):
        mf = ChainDelayStore("MF", store_dir=tmp_path)
        diff = ChainDelayStore("diff", store_dir=tmp_path)
        mf.save(sample_rate=SR, effective_chain_delay_ns=111, now_unix=1.0)
        diff.save(sample_rate=SR, effective_chain_delay_ns=222, now_unix=1.0)
        assert mf.load(now_unix=2.0).effective_chain_delay_ns == 111
        assert diff.load(now_unix=2.0).effective_chain_delay_ns == 222


class TestStaleness:
    def test_value_older_than_threshold_returns_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path, staleness_s=3600.0)
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=0.0)
        # 3601 s later: just past the threshold
        assert store.load(now_unix=3601.0) is None

    def test_value_within_threshold_returned(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path, staleness_s=3600.0)
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=0.0)
        # 3599 s later: just under the threshold
        assert store.load(now_unix=3599.0).effective_chain_delay_ns == 42

    def test_default_staleness_is_one_hour(self):
        assert DEFAULT_STALENESS_S == 3600.0

    def test_future_dated_beyond_skew_tolerance_rejected(self, tmp_path: Path):
        """If the file's saved_at is far ahead of now (local clock went
        backward by more than 60 s), we can't reason about freshness."""
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=1_000_000.0)
        # Now is 120 s before the save → reject
        assert store.load(now_unix=999_880.0) is None

    def test_small_future_skew_tolerated(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=1_000_000.0)
        # Now is 30 s before save (small clock skew) → still tolerated
        assert store.load(now_unix=999_970.0).effective_chain_delay_ns == 42


class TestFailureModes:
    def test_absent_file_returns_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        assert store.load() is None

    def test_malformed_json_returns_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("not valid json {")
        assert store.load() is None

    def test_wrong_schema_returns_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schema": "v99-future",
            "saved_at_unix": 0.0,
            "sample_rate": SR,
            "effective_chain_delay_ns": 42,
            "source": "MF",
        }))
        assert store.load(now_unix=10.0) is None

    def test_wrong_source_returns_none(self, tmp_path: Path):
        """A file written by the diff detector must not be consumed by
        the MF store, even if someone manages to put it in the wrong
        path — guards against operator mistake / mismatched filename."""
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schema": "v1",
            "saved_at_unix": 0.0,
            "sample_rate": SR,
            "effective_chain_delay_ns": 42,
            "source": "diff",  # mismatch
        }))
        assert store.load(now_unix=10.0) is None

    def test_missing_field_returns_none(self, tmp_path: Path):
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schema": "v1",
            "saved_at_unix": 0.0,
            # missing sample_rate
            "effective_chain_delay_ns": 42,
            "source": "MF",
        }))
        assert store.load(now_unix=10.0) is None

    def test_invalid_source_raises_at_construction(self):
        with pytest.raises(ValueError):
            ChainDelayStore("invalid-source")


class TestAtomicWrite:
    def test_save_uses_replace_not_partial_write(self, tmp_path: Path):
        """Save should never leave a half-written file on disk that a
        concurrent load could see.  We can't test crash-mid-write
        cleanly without fault injection, but we can verify that the
        final file is well-formed and that no stray .tmp remains."""
        store = ChainDelayStore("MF", store_dir=tmp_path)
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=0.0)
        assert store.path.exists()
        assert not store.path.with_suffix(store.path.suffix + ".tmp").exists()
        # Should parse cleanly
        data = json.loads(store.path.read_text())
        assert data["effective_chain_delay_ns"] == 42

    def test_save_creates_parent_directory(self, tmp_path: Path):
        nested = tmp_path / "var" / "lib" / "timestd"
        store = ChainDelayStore("MF", store_dir=nested)
        # Parent doesn't exist yet
        assert not nested.exists()
        store.save(sample_rate=SR, effective_chain_delay_ns=42, now_unix=0.0)
        assert store.path.exists()


class TestComputeDisambiguation:
    """Verify the integer-sample alignment math works across the
    interesting cases the calibrators encounter."""

    def test_no_shift_when_raw_already_matches(self):
        ns = compute_disambiguation_ns(
            raw_chain_delay_ns=559_121_877,
            persisted_effective_chain_delay_ns=559_121_877,
            sample_rate=SR,
        )
        assert ns == 0

    def test_small_forward_shift_rounded_to_sample(self):
        # Persisted is exactly 1 sample (10417 ns @ 96 kHz) ahead.
        sample_ns = int(round(1e9 / SR))
        ns = compute_disambiguation_ns(
            raw_chain_delay_ns=100_000,
            persisted_effective_chain_delay_ns=100_000 + sample_ns,
            sample_rate=SR,
        )
        assert ns == sample_ns

    def test_large_wrap_shift(self):
        """At 96 kHz the MF wrap is 48000 samples = 500 ms.  A raw
        value off by tens of thousands of samples is the normal
        post-restart case."""
        ns = compute_disambiguation_ns(
            raw_chain_delay_ns=12_345_678,
            persisted_effective_chain_delay_ns=559_121_877,
            sample_rate=SR,
        )
        # Result + raw should round to within half a sample of persisted
        sample_period_ns = 1e9 / SR
        residual = (12_345_678 + ns) - 559_121_877
        assert abs(residual) <= sample_period_ns / 2 + 1

    def test_negative_shift_when_persisted_is_smaller(self):
        sample_ns = int(round(1e9 / SR))
        ns = compute_disambiguation_ns(
            raw_chain_delay_ns=100_000 + sample_ns,
            persisted_effective_chain_delay_ns=100_000,
            sample_rate=SR,
        )
        assert ns == -sample_ns

    def test_result_aligns_to_integer_sample_multiple(self):
        # Random-looking offsets — verify the result is an integer
        # multiple of sample_period.  At 96 kHz one sample is not an
        # integer ns so the test allows ±1 ns rounding.
        ns = compute_disambiguation_ns(
            raw_chain_delay_ns=42_424_242,
            persisted_effective_chain_delay_ns=987_654_321,
            sample_rate=SR,
        )
        sample_period_ns = 1e9 / SR
        n_samples = ns / sample_period_ns
        assert abs(n_samples - round(n_samples)) < 0.01
