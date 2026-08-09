"""Fine-stage BPSK edge estimator — coherent fold + zero-crossing.

Second stage of the two-stage T6 estimator (spec:
docs/design/T6_ANCHOR_INVERSION_DESIGN.md §3).  The matched filter
(BpskPpsCalibratorMF) remains the coarse stage; this class coherently
averages K seconds of complex baseband, folded modulo the sample rate
with per-second sign alternation, and localises the ~52 µs polarity
transition by the zero crossing of the derotated in-phase component.

Indexing is by stream continuity (samples actually received), not
per-batch declared RTP: the continuity→RTP registration is the median
of all batch declarations in the fold block, so the measured
±60-sample batch mislabelling averages out instead of smearing the
edge.  A registration spread beyond REGISTRATION_SPREAD_LIMIT samples
means a genuine stream gap inside the block — the block is discarded
(counted in ``blocks_discarded``), never silently used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_WRAP = 1 << 32
# A fold block whose batch declarations disagree by more than this is
# carrying a real stream gap, not label jitter (measured jitter is
# ±60 samples, bimodal, non-accumulating).
REGISTRATION_SPREAD_LIMIT = 240
# A declared-RTP jump beyond this (vs continuity) means the stream
# restarted; re-registering the median would be meaningless.
STREAM_RESTART_LIMIT_SEC = 2.0


def _wrapped_signed(delta: int) -> int:
    """Map a mod-2^32 difference to signed [-2^31, 2^31)."""
    d = delta & 0xFFFFFFFF
    return d - _WRAP if d >= (1 << 31) else d


@dataclass(frozen=True)
class FineEdgeEstimate:
    edge_offset_samples: float
    edge_rtp: int
    edge_subsample: float
    n_seconds_folded: int
    plateau_amplitude: float
    fit_rms: float


class BpskEdgeFineStage:
    def __init__(self, sample_rate: int, fold_seconds: int = 30,
                 search_window_ms: float = 6.0):
        if sample_rate < 8000:
            raise ValueError(f"sample_rate must be ≥ 8000 Hz, got {sample_rate}")
        if fold_seconds < 1:
            raise ValueError(f"fold_seconds must be ≥ 1, got {fold_seconds}")
        self.sample_rate = int(sample_rate)
        self.fold_seconds = int(fold_seconds)
        self.search_window_ms = float(search_window_ms)
        self.blocks_discarded = 0
        self._coarse_offset: Optional[float] = None
        self._last_avg_for_test: Optional[np.ndarray] = None
        self.reset()

    def reset(self) -> None:
        p = self.sample_rate
        self._acc = np.zeros(p, dtype=np.complex128)
        self._cnt = np.zeros(p, dtype=np.int64)
        self._cont = 0                      # samples received since reset
        self._reg_base: Optional[int] = None
        self._reg_rel: list[int] = []       # per-batch (declared − cont) − reg_base
        self._last_registration: Optional[int] = None

    def set_coarse_offset_samples(self, offset: float) -> None:
        self._coarse_offset = float(offset) % self.sample_rate

    def process_samples(self, iq_samples: np.ndarray,
                        rtp_timestamp: int) -> Optional[FineEdgeEstimate]:
        n = len(iq_samples)
        if n == 0:
            return None
        decl = int(rtp_timestamp) & 0xFFFFFFFF
        off = (decl - (self._cont & 0xFFFFFFFF)) & 0xFFFFFFFF
        if self._reg_base is None:
            self._reg_base = off
        rel = _wrapped_signed(off - self._reg_base)
        if abs(rel) > STREAM_RESTART_LIMIT_SEC * self.sample_rate:
            logger.warning(
                "T6 fine stage: declared RTP jumped %+d samples vs "
                "continuity — treating as stream restart, resetting fold.",
                rel,
            )
            self.reset()
            self._reg_base = decl
            rel = 0
        self._reg_rel.append(rel)

        idx = (self._cont + np.arange(n)) % self.sample_rate
        sec = (self._cont + np.arange(n)) // self.sample_rate
        sign = 1.0 - 2.0 * (sec & 1).astype(np.float64)
        np.add.at(self._acc, idx, iq_samples.astype(np.complex128) * sign)
        np.add.at(self._cnt, idx, 1)
        self._cont += n

        if self._cont >= self.fold_seconds * self.sample_rate:
            est = self._finish_block()
            # Save last_registration before reset clears it
            saved_registration = self._last_registration
            # Registration is re-derived per block: reset() clears
            # _reg_base, and the next batch's declared RTP re-registers it.
            self.reset()
            self._last_registration = saved_registration
            return est
        return None

    def _registration_for_test(self) -> Optional[int]:
        # Return the current registration if we're accumulating,
        # or the last completed registration if we've reset.
        if self._reg_base is not None and self._reg_rel:
            return (self._reg_base + int(np.median(self._reg_rel))) & 0xFFFFFFFF
        return self._last_registration

    def _finish_block(self) -> Optional[FineEdgeEstimate]:
        rels = np.asarray(self._reg_rel, dtype=np.int64)
        if len(rels) == 0:
            return None
        if int(rels.max() - rels.min()) > REGISTRATION_SPREAD_LIMIT:
            self.blocks_discarded += 1
            logger.warning(
                "T6 fine stage: registration spread %d samples exceeds "
                "%d — stream gap inside fold block, block discarded "
                "(total discarded: %d).",
                int(rels.max() - rels.min()), REGISTRATION_SPREAD_LIMIT,
                self.blocks_discarded,
            )
            return None
        cnt = np.maximum(self._cnt, 1)
        avg = self._acc / cnt
        self._last_avg_for_test = avg
        registration = (self._reg_base + int(np.median(rels))) & 0xFFFFFFFF
        self._last_registration = registration
        return self._compute_estimate(avg, registration)

    def _compute_estimate(self, avg: np.ndarray,
                          registration: int) -> Optional[FineEdgeEstimate]:
        # Task 2 implements localisation.
        return None
