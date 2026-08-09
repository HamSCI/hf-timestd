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
    def __init__(
        self, sample_rate: int, fold_seconds: int = 30, search_window_ms: float = 6.0
    ):
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
        self._cont = 0  # samples received since reset
        self._reg_base: Optional[int] = None
        self._reg_rel: list[int] = []  # per-batch (declared − cont) − reg_base
        self._last_registration: Optional[int] = None

    def set_coarse_offset_samples(self, offset: float) -> None:
        self._coarse_offset = float(offset) % self.sample_rate

    def process_samples(
        self, iq_samples: np.ndarray, rtp_timestamp: int
    ) -> Optional[FineEdgeEstimate]:
        n = len(iq_samples)
        if n == 0:
            return None
        decl0 = int(rtp_timestamp) & 0xFFFFFFFF
        block_len = self.fold_seconds * self.sample_rate
        consumed = 0
        result: Optional[FineEdgeEstimate] = None
        while consumed < n:
            # Declared RTP for the start of this sub-chunk: the batch's
            # declared timestamp advanced by however many of its
            # samples were already consumed into a prior block this
            # call (only >0 when this batch itself straddles a fold
            # boundary).
            decl = (decl0 + consumed) & 0xFFFFFFFF
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

            # Never cross the fold boundary within a single accumulation
            # step: take at most however many samples remain in the
            # current block, so `_cont` is exactly 0 at every block
            # start -- the invariant the sign-alternation formula below
            # assumes. A batch that spans one or more boundaries is
            # handled by looping back around with the remainder feeding
            # the freshly-reset block (previously, the whole batch was
            # consumed even when it overshot the boundary, so every
            # block after the first started phase-shifted from the true
            # per-second boundary and its coherent average destructively
            # cancelled -- see the T6 Task 7 acceptance-gate report).
            take = min(n - consumed, block_len - self._cont)
            chunk = iq_samples[consumed : consumed + take]

            idx = (self._cont + np.arange(take)) % self.sample_rate
            sec = (self._cont + np.arange(take)) // self.sample_rate
            sign = 1.0 - 2.0 * (sec & 1).astype(np.float64)
            np.add.at(self._acc, idx, chunk.astype(np.complex128) * sign)
            np.add.at(self._cnt, idx, 1)
            self._cont += take
            consumed += take

            if self._cont >= block_len:
                est = self._finish_block()
                # Save last_registration before reset clears it
                saved_registration = self._last_registration
                # Registration is re-derived per block: reset() clears
                # _reg_base, and the next chunk's declared RTP
                # re-registers it.
                self.reset()
                self._last_registration = saved_registration
                if est is not None:
                    result = est
        return result

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
                int(rels.max() - rels.min()),
                REGISTRATION_SPREAD_LIMIT,
                self.blocks_discarded,
            )
            return None
        cnt = np.maximum(self._cnt, 1)
        avg = self._acc / cnt
        self._last_avg_for_test = avg
        registration = (self._reg_base + int(np.median(rels))) & 0xFFFFFFFF
        self._last_registration = registration
        return self._compute_estimate(avg, registration)

    # Linear-fit band: samples with |I| below this fraction of the
    # plateau participate in the zero-crossing fit (spec §3: ∓40%).
    FIT_BAND_FRACTION = 0.4

    def _compute_estimate(
        self, avg: np.ndarray, registration: int
    ) -> Optional[FineEdgeEstimate]:
        if self._coarse_offset is None:
            return None
        p = self.sample_rate
        # Derotate: squaring removes the BPSK sign, leaving 2× carrier phase.
        phi = 0.5 * float(np.angle(np.mean(avg.astype(np.complex128) ** 2)))
        in_phase = np.real(avg * np.exp(-1j * phi))

        c = int(round(self._coarse_offset)) % p
        W = max(8, int(self.search_window_ms * 1e-3 * p))
        seg = np.take(in_phase, np.arange(c - W, c + W + 1) % p)

        outer = np.concatenate([seg[: W // 2], seg[-(W // 2) :]])
        A = float(np.median(np.abs(outer)))
        if A <= 0.0:
            return None

        # Locate the sign-change candidate nearest the coarse offset
        # (local index W). Polarity is normalised LOCALLY at that
        # candidate rather than from the window's far extremes: when the
        # true edge sits close to the fold-domain seam (pos=0, where the
        # per-second-periodic derotated envelope has its own built-in
        # wrap discontinuity — inherent to folding, not a signal defect),
        # a search window wide enough to cover the coarse-offset
        # uncertainty can contain both features. Using the window
        # extremes for polarity picks up whichever side of the *seam*
        # they happen to land on and can flip the true edge out of
        # consideration; checking locally at the candidate avoids that.
        changes = np.nonzero(np.diff(np.sign(seg)) != 0)[0]
        if len(changes) == 0:
            return None
        k = int(changes[np.argmin(np.abs(changes - W))])
        if seg[k] > seg[k + 1]:
            # Falling locally: normalise so the fit sees a rising edge.
            seg = -seg

        band = self.FIT_BAND_FRACTION * A
        lo, hi = k, k + 1
        while lo > 0 and abs(seg[lo - 1]) < band:
            lo -= 1
        while hi < len(seg) - 1 and abs(seg[hi + 1]) < band:
            hi += 1
        if hi - lo < 1:
            return None
        xs = np.arange(lo, hi + 1, dtype=np.float64)
        ys = seg[lo : hi + 1].astype(np.float64)
        m, b = np.polyfit(xs, ys, 1)
        if m <= 0.0:
            return None
        x0 = -b / m
        fit_rms = float(np.sqrt(np.mean((ys - (m * xs + b)) ** 2)) / A)

        edge_offset = (c - W + x0) % p
        # Continuity position of the last edge inside this block, then
        # map to RTP via the median registration.
        k_last = (self._cont // p) - 1
        c_edge = k_last * p + edge_offset
        edge_rtp_float = registration + c_edge
        edge_rtp = int(round(edge_rtp_float))
        subsample = float(edge_rtp_float - edge_rtp)
        return FineEdgeEstimate(
            edge_offset_samples=float(edge_offset),
            edge_rtp=edge_rtp & 0xFFFFFFFF,
            edge_subsample=subsample,
            n_seconds_folded=self.fold_seconds,
            plateau_amplitude=A,
            fit_rms=fit_rms,
        )
