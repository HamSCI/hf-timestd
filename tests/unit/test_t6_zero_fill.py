"""Block drops on the T6 channel must be counted, not inferred.

The T6 BPSK channel is not archived, writes no raw-buffer sidecar, and
therefore contributes no gap_count -- and sigmond.gap_hourly reads only
those sidecars.  On AC0G-B4 2026-08-26, hour 07 recorded ZERO gaps across
the six archived channels while the T6 anomaly capture from that hour
contains two dropped blocks.  The loss metric is blind to the one channel
whose loss costs the station its time reference.

Measured from nine captures: 29 dropped blocks, bursts of 1-11, every run
an exact multiple of 1920 samples (20 ms at 96 kHz).
"""
from __future__ import annotations

import numpy as np
import pytest

from hf_timestd.core.t6_zero_fill import DEFAULT_BLOCK_SAMPLES, ZeroFillCounter

BLK = DEFAULT_BLOCK_SAMPLES


def signal(n):
    """Non-zero samples: a real batch is never bit-identically zero."""
    return (np.arange(1, n + 1) * (1 + 1j)).astype(np.complex64)


def zeros(n):
    return np.zeros(n, dtype=np.complex64)


class TestCounting:
    def test_a_clean_batch_counts_nothing(self):
        c = ZeroFillCounter()
        assert c.observe(signal(1800)) == 0
        assert c.snapshot() == {"zero_samples": 0, "runs": 0, "blocks": 0,
                                "longest_run_samples": 0}

    def test_one_whole_block_inside_a_batch(self):
        c = ZeroFillCounter()
        batch = np.concatenate([signal(200), zeros(BLK), signal(200)])
        assert c.observe(batch) == 1
        s = c.snapshot()
        assert s["blocks"] == 1 and s["runs"] == 1
        assert s["zero_samples"] == BLK

    def test_an_eleven_block_burst(self):
        """The largest burst measured on B4: 21,120 samples."""
        c = ZeroFillCounter()
        c.observe(np.concatenate([signal(100), zeros(11 * BLK), signal(100)]))
        assert c.snapshot()["blocks"] == 11

    def test_incidental_zeros_are_ignored(self):
        c = ZeroFillCounter()
        c.observe(np.concatenate([signal(50), zeros(3), signal(50)]))
        assert c.snapshot()["runs"] == 0


class TestBatchBoundaries:
    """Deliveries are 1740/1800 samples against a 1920-sample block, so a
    single drop USUALLY straddles two batches.  Per-batch inspection
    alone would miss most real drops."""

    def test_a_drop_straddling_two_batches_counts_once(self):
        c = ZeroFillCounter()
        assert c.observe(np.concatenate([signal(600), zeros(1140)])) == 0
        assert c.observe(np.concatenate([zeros(780), signal(960)])) == 1
        s = c.snapshot()
        assert s["runs"] == 1 and s["blocks"] == 1
        assert s["zero_samples"] == 1920

    def test_a_drop_spanning_three_batches(self):
        c = ZeroFillCounter()
        c.observe(np.concatenate([signal(100), zeros(1700)]))
        c.observe(zeros(1800))
        assert c.observe(np.concatenate([zeros(340), signal(500)])) == 1
        assert c.snapshot()["zero_samples"] == 1700 + 1800 + 340

    def test_an_open_run_is_not_counted_until_it_closes(self):
        c = ZeroFillCounter()
        c.observe(np.concatenate([signal(100), zeros(1700)]))
        assert c.snapshot()["runs"] == 0     # still open, not yet a drop
        c.observe(signal(1800))
        assert c.snapshot()["runs"] == 1     # closed by a clean batch

    def test_back_to_back_drops_count_separately(self):
        c = ZeroFillCounter()
        b = np.concatenate([zeros(BLK), signal(60), zeros(BLK), signal(60)])
        assert c.observe(b) == 2
        assert c.snapshot()["blocks"] == 2


class TestRobustness:
    def test_an_empty_batch_is_harmless(self):
        assert ZeroFillCounter().observe(zeros(0)) == 0

    def test_a_bad_batch_never_raises(self):
        """The sample path must survive anything handed to it."""
        assert ZeroFillCounter().observe(object()) == 0
        assert ZeroFillCounter().observe(None) == 0

    def test_longest_run_is_tracked(self):
        c = ZeroFillCounter()
        c.observe(np.concatenate([signal(10), zeros(BLK), signal(10),
                                  zeros(5 * BLK), signal(10)]))
        assert c.snapshot()["longest_run_samples"] == 5 * BLK
