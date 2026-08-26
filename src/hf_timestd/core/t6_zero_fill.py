"""Count radiod block drops on the T6 channel.

The T6 BPSK channel is not archived, so it writes no raw-buffer sidecar,
so it contributes no ``gap_count`` — and ``sigmond.gap_hourly`` reads
only those sidecars.  Every drop on the one channel whose loss costs the
station its time reference is therefore invisible to the fleet's loss
metric.

Measured on AC0G-B4 2026-08-26 from nine anomaly IQ captures: 29 dropped
blocks, bursts of 1–11 blocks, every run an exact multiple of 1920
samples (20 ms at 96 kHz — radiod's blocktime to the sample).  In hour 07
``gap-hourly.tsv`` recorded ZERO gaps across the six archived channels
while the T6 capture from that hour holds two dropped blocks.

radiod zero-fills a dropped block rather than shortening the stream, so
the counter stays continuous and the loss is silent: byte counts and
completeness read 100 %.  The zeros themselves are the only honest
evidence, and they are exact — a real signal does not produce a run of
bit-identical zeros in both I and Q.

⚠ Detection must survive batch boundaries.  Deliveries are 1740/1800
samples against a 1920-sample block, so a single dropped block usually
straddles two batches and is invisible to per-batch inspection.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# radiod emits one block per 20 ms; at 96 kHz that is 1920 samples.
DEFAULT_BLOCK_SAMPLES = 1920

# Ignore incidental zeros (a genuine sample can be 0+0j, rarely).  Half a
# block is far above chance and far below a real drop.
MIN_RUN_SAMPLES = 64


class ZeroFillCounter:
    """Running count of zero-filled samples, runs and whole blocks."""

    def __init__(
        self,
        block_samples: int = DEFAULT_BLOCK_SAMPLES,
        min_run_samples: int = MIN_RUN_SAMPLES,
    ) -> None:
        self.block_samples = int(block_samples)
        self.min_run_samples = int(min_run_samples)
        self.zero_samples = 0        # total zero samples seen
        self.runs = 0                # completed runs >= min_run_samples
        self.blocks = 0              # those runs, expressed in blocks
        self.longest_run = 0
        self._carry = 0              # open run straddling the batch edge

    # ------------------------------------------------------------------
    def observe(self, samples) -> int:
        """Feed one delivered batch.  Returns runs COMPLETED this batch.

        A run is only counted once it ends, so a drop straddling batches
        is counted exactly once and attributed to the batch that closed
        it.
        """
        try:
            import numpy as np
            a = np.asarray(samples)
            # A 0-d or ragged input is not a batch; anything at all may be
            # handed to a hot path and it must simply decline.
            if a.ndim != 1 or a.size == 0:
                return 0
            z = (a.real == 0) & (a.imag == 0)
            self.zero_samples += int(z.sum())
            idx = z.view("int8")
            d = np.diff(np.concatenate(([0], idx, [0])))
            starts = np.where(d == 1)[0]
            ends = np.where(d == -1)[0]
        except Exception:  # noqa: BLE001 — never break the sample path
            return 0

        closed = 0
        for s, e in zip(starts, ends):
            length = int(e - s)
            if s == 0 and self._carry:
                length += self._carry        # continues a straddling run
                self._carry = 0
            if e == a.size:                  # still open at the batch edge
                self._carry = length
                continue
            closed += self._close(length)
        if not starts.size:
            # wholly non-zero batch ends any open run
            if self._carry:
                closed += self._close(self._carry)
                self._carry = 0
        return closed

    def _close(self, length: int) -> int:
        if length < self.min_run_samples:
            return 0
        self.runs += 1
        self.blocks += int(round(length / self.block_samples)) or 1
        self.longest_run = max(self.longest_run, length)
        return 1

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Auditable counters for the ledger and the log."""
        return {
            "zero_samples": self.zero_samples,
            "runs": self.runs,
            "blocks": self.blocks,
            "longest_run_samples": self.longest_run,
        }
