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

## Coarse-offset domain (read before touching ``set_coarse_offset_samples``)

Two different "position within the second" domains exist here and they
are NOT interchangeable:

* **RTP domain** — ``rtp % sample_rate``.  This is what
  ``BpskPpsCalibratorMF._chain_delay_samples`` reports (it is literally
  ``edge_rtp_full % sample_rate``) and it is the domain the whole
  service speaks.
* **Fold domain** — ``continuity_index % sample_rate``, where continuity
  counts samples since this stage last reset.  The fold buffer, and
  therefore ``FineEdgeEstimate.edge_offset_samples``, live here.

The two differ by ``registration % sample_rate``, where the
registration is the declared RTP of the first sample of the block —
an arbitrary value that changes whenever the stage resets.  Feeding an
RTP-domain coarse straight into the fold-domain search window
mis-places the ±6 ms window by that arbitrary amount (286 ms in the
reviewer's repro), so the stage finds nothing at all, or — near the
fold seam — localises the seam instead of the edge.

``set_coarse_offset_samples`` therefore takes the **RTP domain** (the
producer's domain, no translation at the call site) and the stage
translates into its own fold domain at estimate time, using the
registration it just measured for that very block.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import NamedTuple, Optional

import numpy as np

from hf_timestd.core.bpsk_fold_bootstrap import bootstrap_edge_index
from hf_timestd.core.rtp_domain import RtpUnwrapper

logger = logging.getLogger(__name__)

_WRAP = 1 << 32
# A fold block whose batch declarations disagree by more than this is
# carrying a real stream gap, not label jitter (measured jitter is
# ±60 samples, bimodal, non-accumulating).
REGISTRATION_SPREAD_LIMIT = 240
# A declared-RTP jump beyond this (vs continuity) means the stream
# restarted; re-registering the median would be meaningless.
STREAM_RESTART_LIMIT_SEC = 2.0

# Self-seeding can cement a wrong crossing — the displaced-reference
# failure the MF guards with STEP_CONFIRM_EDGES = 60.  The fold gets the
# same discipline: this many consecutive full-search estimates must agree
# before the stage will use its own offset as a search centre.
BOOTSTRAP_CONFIRM_BLOCKS = 3
BOOTSTRAP_CONFIRM_TOLERANCE_MS = 1.0
# No slower to abandon a position than to adopt one.
DEMOTE_AFTER_FAILED_BLOCKS = 3


def _wrapped_signed(delta: int) -> int:
    """Map a mod-2^32 difference to signed [-2^31, 2^31)."""
    d = delta & 0xFFFFFFFF
    return d - _WRAP if d >= (1 << 31) else d


class _EdgeFit(NamedTuple):
    """One zero-crossing localisation of the folded edge.

    Produced by ``_fit_edge`` and consumed both by ``_compute_estimate``
    (which reports it) and by ``_split_half_delta`` (which compares two of
    them).  Sharing the producer is the point: a check that located the
    edge its own way would compare against a different definition of
    "where the edge is", and its disagreement would mix real wander with
    the gap between two estimators.
    """

    edge_offset: float      # fold domain, sub-sample
    amplitude: float        # plateau median |A|
    fit_rms: float          # residual of the linear fit, normalised by A
    width: float            # samples spanned by the fit bracket


@dataclass(frozen=True)
class FineEdgeEstimate:
    edge_offset_samples: float
    edge_rtp: int
    edge_subsample: float
    n_seconds_folded: int
    plateau_amplitude: float
    fit_rms: float
    # --- battery evidence (T6_ACCEPTANCE_CRITERIA.md §4) ---
    # Folded amplitude as a fraction of the mean instantaneous amplitude.
    # ~1.0 when the carrier stays coherent with the ADC clock across
    # seconds; collapses toward 0 when it does not.  AI6VN measured
    # 0.006 with the TS-1's REF IN on its internal 10 MHz and 0.9998
    # once fed from the GPSDO that governs the RX888.  B4 reads 0.98.
    fold_retention: float = 0.0
    # Edge position from the even-second sub-fold minus the odd-second
    # sub-fold, in samples, to SUB-SAMPLE resolution.  Each sub-fold is
    # located globally (argmax|T|, which keeps two noise folds 765-39190
    # samples apart) and then refined by the shipped zero-crossing fit.
    # Captured B4 signal read 0.028 - 0.308 across six live blocks.
    # ⛔ Until 2026-09-20 this reported whole samples only, so it read
    # exactly 0.0000 on five of six real blocks and could not resolve the
    # wander it exists to catch -- see T6_NEWELL_VS_FOLD.md §5d.
    split_half_delta_samples: float = 0.0
    # Triangle-fidelity residual of the SIGNED T(e), the closed-form
    # matched filter (T6_FOLDED_SELF_ACQUISITION.md §3.1) evaluated over
    # the WHOLE derotated folded second: RMS(T - ideal_tent) / |T[apex]|.
    # For a single clean polarity flip, T(e) is exactly piecewise-linear
    # (a tent, apex at the flip) by construction -- NOT an SNR statistic;
    # the bare peak/median ratio is PINNED at 2.000 regardless of C/N0,
    # because a triangle's median sits at half its peak.  Measured over 9
    # fold positions: a real flip reads 0.0000081-0.000031 at 77 dB-Hz and
    # 0.00017-0.00039 at 48.4; pure noise reads 0.1115-0.6760 -- two to
    # four orders of magnitude apart, and unlike the bare ratio this
    # SEPARATES at 48.4 dB-Hz.  Field name kept from the original
    # (bare-ratio) definition for the dataclass contract; LOW values mean
    # a clean tent (real edge), HIGH values mean noise -- the opposite
    # sense "prominence" suggests, so read the derivation above, not the
    # name, for what a given value means.  NaN when the fold held nothing
    # measurable: no evidence, never spelled as the favourable 0.0.
    peak_prominence: float = 0.0
    # Width of the fitted transition, in samples.  A +-25 kHz channel
    # filter predicts 1/(2B) = 20 us, about 2 samples at 96 kHz.
    transition_width_samples: float = 0.0
    # Distance, in samples (signed, wrapped to (-p/2, p/2]), between
    # T(e)'s apex (its own estimate of the flip position, independent of
    # the zero-crossing fit) and the edge position this estimate reports.
    # Targets the failure criterion 4 exists for: on 2026-09-04 B4 locked
    # onto a 20.000 ms lattice position away from the true apex, a lock a
    # bare prominence ratio never tested.  A real edge measured 0.35-0.87
    # samples; forcing a ~20 ms displacement (simulating that lock)
    # measured -1919.19 samples -- the check catches it directly.
    apex_distance_samples: float = 0.0


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
        # RTP-domain coarse (see module docstring).  Translated to the
        # fold domain per block in _compute_estimate.
        self._coarse_offset_rtp: Optional[float] = None
        # Which search mode produced the last estimate: 'seeded' (MF
        # coarse), 'tracking' (our own confirmed offset) or 'bootstrap'
        # (full-second matched filter).  Surfaced for tests and telemetry.
        self._last_search_mode: str = "none"
        self._own_offset_rtp: Optional[float] = None
        self._bootstrap_history: list[float] = []
        self._failed_blocks = 0
        # A block whose fit succeeded has NOT yet been judged: the
        # authority may still reject its position for ``edge_period``,
        # which spec §3.3 counts as a failed block.  The failure run is
        # therefore cleared by the VERDICT (note_authority_violations),
        # not by the fit.  A caller with no authority attached (unit
        # tests, offline analysis) never reports one; an unreported
        # verdict is treated as acceptance when the next block lands, so
        # standalone behaviour is unchanged.
        self._verdict_pending = False
        self._last_avg_for_test: Optional[np.ndarray] = None
        # One continuous counter domain (see rtp_domain).  Deliberately
        # NOT cleared by reset(): a re-lock after reset must land in the
        # same domain as the lock it replaces, and the MF calibrator
        # watching the same stream must agree on the epoch — the 2^32
        # seam otherwise shifts every phase by 2^32 % sample_rate
        # (23,296 samples = 242.667 ms at 96 kHz) once per ~12.4 h.
        self._rtp_unwrapper = RtpUnwrapper()
        self.reset()

    def reset(self) -> None:
        p = self.sample_rate
        self._acc = np.zeros(p, dtype=np.complex128)
        self._cnt = np.zeros(p, dtype=np.int64)
        # Disjoint sub-folds by second parity, for the split-half check.
        self._acc_even = np.zeros(p, dtype=np.complex128)
        self._acc_odd = np.zeros(p, dtype=np.complex128)
        self._cnt_even = np.zeros(p, dtype=np.int64)
        self._cnt_odd = np.zeros(p, dtype=np.int64)
        # Mean |x| over the block: the denominator of fold retention.
        self._abs_sum = 0.0
        self._abs_n = 0
        self._cont = 0  # samples received since reset
        self._reg_base: Optional[int] = None
        self._reg_rel: list[int] = []  # per-batch (declared − cont) − reg_base
        self._last_registration: Optional[int] = None

    def set_coarse_offset_samples(self, offset: float) -> None:
        """Seed the search window with the coarse edge position.

        ``offset`` is in the **RTP domain** — ``rtp % sample_rate``,
        exactly what ``BpskPpsCalibratorMF._chain_delay_samples``
        publishes.  The caller does no translation; this stage converts
        to its own fold domain per block (module docstring).
        """
        self._coarse_offset_rtp = float(offset) % self.sample_rate

    def clear_coarse_offset(self) -> None:
        """Forget the MF-supplied search window.

        Called when the MF is not locked.  Without this the stage keeps
        searching a window the MF no longer stands behind, which is
        exactly the stale-window estimate the recorder's outer gate was
        added to block (Finding 3).  Clears ONLY the MF's window — never
        our own confirmed offset, which is independent of it.
        """
        self._coarse_offset_rtp = None

    def clear_own_offset(self) -> None:
        """Repudiate the stage's own tracking position.

        Deliberately NOT part of ``reset()``: reset runs at every fold
        block boundary, where clearing would be pure churn fighting the
        recorder's per-batch re-seed.  This is the escape hatch for the
        recorder's three recovery sites (stale-lock abandonment,
        stuck-unlock, step-recovery), each of which has just concluded
        that the position the stage is sitting on is not defensible.

        Without it, self-acquisition made a displaced lock HARDER to
        shed than before: the MF unlocks, the authority UNLOCKs and
        drops its ``_prev_edge``, and the fine stage goes on localising
        at the same wrong position, re-installing it within one fold
        block with ``edge_period`` unreferenced and ``fine_coarse``
        unverified (the B4 -26 ms / -76.8 ms family).

        Clears the confirmation history too: keeping it would let the
        very next block re-promote the repudiated position without the
        full ``BOOTSTRAP_CONFIRM_BLOCKS`` agreement the spec requires.
        """
        if self._own_offset_rtp is not None:
            logger.warning(
                "T6 fine stage: tracking offset repudiated by a recovery "
                "path — returning to full-second bootstrap.",
            )
        self._own_offset_rtp = None
        self._bootstrap_history.clear()
        self._failed_blocks = 0
        self._verdict_pending = False

    def note_authority_violations(self, violations) -> None:
        """Feed the authority's verdict on this block's estimate back in.

        Spec §3.3 demotes to bootstrap after
        ``DEMOTE_AFTER_FAILED_BLOCKS`` consecutive blocks that either
        fail their fit **or** are rejected for ``edge_period``.  Only
        the recorder sees the authority's decision, so it reports it
        here; without this half, a position that fits cleanly every
        block while violating ``edge_period`` drives the authority
        AUTHORITATIVE → DEGRADED → UNLOCKED while the stage keeps
        tracking it, and re-installs it on the next acquisition.

        ``edge_period`` only.  ``fine_coarse`` is the matched-filter
        witness disagreeing — a different signal, and deliberately
        non-fatal since the MF was demoted from gate to witness;
        ``naming_unavailable`` says nothing about this stage's position
        at all.
        """
        if not self._verdict_pending:
            return
        self._verdict_pending = False
        if "edge_period" in tuple(violations or ()):
            self._note_block_failed()
        else:
            self._failed_blocks = 0

    def coarse_offset_fold_domain(self, registration: int) -> Optional[float]:
        """Translate the stored RTP-domain coarse into the fold domain
        for a block whose median registration is ``registration``.

        RTP at continuity index ``c`` is ``registration + c``, so a
        given RTP phase ``E`` sits at fold index
        ``(E − registration) mod sample_rate``.
        """
        if self._coarse_offset_rtp is None:
            return None
        return (self._coarse_offset_rtp - int(registration)) % self.sample_rate

    def process_samples(
        self, iq_samples: np.ndarray, rtp_timestamp: int
    ) -> Optional[FineEdgeEstimate]:
        n = len(iq_samples)
        if n == 0:
            return None
        decl0 = self._rtp_unwrapper.unwrap(rtp_timestamp)
        block_len = self.fold_seconds * self.sample_rate
        consumed = 0
        result: Optional[FineEdgeEstimate] = None
        while consumed < n:
            # Declared RTP for the start of this sub-chunk: the batch's
            # declared timestamp advanced by however many of its
            # samples were already consumed into a prior block this
            # call (only >0 when this batch itself straddles a fold
            # boundary).
            decl = decl0 + consumed
            off = decl - self._cont
            if self._reg_base is None:
                self._reg_base = off
            rel = off - self._reg_base
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
            # Battery evidence.  `idx` and `sign` above are already the
            # fold-domain index and per-second alternation for every
            # sample in `chunk`; reuse them rather than recomputing.
            self._abs_sum += float(np.sum(np.abs(chunk)))
            self._abs_n += int(chunk.size)
            # Second parity within the fold: even-parity seconds feed
            # one sub-fold, odd the other, so the two never share a
            # sample.  `sign > 0` iff `sec` is even.
            even = sign > 0
            contrib = chunk.astype(np.complex128) * sign
            np.add.at(self._acc_even, idx[even], contrib[even])
            np.add.at(self._cnt_even, idx[even], 1)
            np.add.at(self._acc_odd, idx[~even], contrib[~even])
            np.add.at(self._cnt_odd, idx[~even], 1)
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
            return self._reg_base + int(np.median(self._reg_rel))
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
        registration = self._reg_base + int(np.median(rels))
        self._last_registration = registration
        return self._compute_estimate(avg, registration)

    # Linear-fit band: samples with |I| below this fraction of the
    # plateau participate in the zero-crossing fit (spec §3: ∓40%).
    FIT_BAND_FRACTION = 0.4

    def _search_centre(
        self, in_phase: np.ndarray, registration: int
    ) -> Optional[float]:
        """Fold-domain centre for the zero-crossing search window.

        Preference order: a fresh MF coarse (most selective), then our
        own confirmed offset, then a full-second bootstrap.  Bootstrap
        exists so that the MF -- which locks itself out at low C/N0 --
        can no longer veto the tier.
        """
        seeded = self.coarse_offset_fold_domain(registration)
        if seeded is not None:
            self._last_search_mode = "seeded"
            return seeded
        if self._own_offset_rtp is not None:
            self._last_search_mode = "tracking"
            return (self._own_offset_rtp - int(registration)) % self.sample_rate
        self._last_search_mode = "bootstrap"
        return float(bootstrap_edge_index(in_phase))

    def _note_block_estimate(self, edge_phase_rtp: float) -> None:
        """Record a successful block and maintain the self-seed."""
        if self._verdict_pending:
            # The previous block's verdict never arrived, so no
            # authority is attached and a clean fit is the only evidence
            # there is.  (With an authority, note_authority_violations
            # has already cleared or extended the run.)
            self._failed_blocks = 0
        self._verdict_pending = True
        if self._last_search_mode != "bootstrap":
            # Already trusted: keep the tracking centre fresh.
            self._own_offset_rtp = edge_phase_rtp
            return
        self._bootstrap_history.append(edge_phase_rtp)
        if len(self._bootstrap_history) > BOOTSTRAP_CONFIRM_BLOCKS:
            self._bootstrap_history.pop(0)
        if len(self._bootstrap_history) < BOOTSTRAP_CONFIRM_BLOCKS:
            return
        first = self._bootstrap_history[0]
        tol = BOOTSTRAP_CONFIRM_TOLERANCE_MS * 1e-3 * self.sample_rate
        spread = max(
            abs((v - first + self.sample_rate / 2) % self.sample_rate
                - self.sample_rate / 2)
            for v in self._bootstrap_history
        )
        if spread <= tol:
            self._own_offset_rtp = first
            logger.info(
                "T6 fine stage: self-acquired edge confirmed across %d "
                "blocks (spread %.0f samples), tracking from own offset.",
                BOOTSTRAP_CONFIRM_BLOCKS, spread,
            )

    def _note_block_failed(self) -> None:
        """A block produced no estimate. Enough of these and any adopted
        position is abandoned, so a wrong lock is escapable."""
        self._verdict_pending = False
        self._failed_blocks += 1
        if self._failed_blocks >= DEMOTE_AFTER_FAILED_BLOCKS:
            if self._own_offset_rtp is not None:
                logger.warning(
                    "T6 fine stage: %d consecutive blocks without an "
                    "estimate — dropping the self-acquired offset and "
                    "returning to full-second search.",
                    self._failed_blocks,
                )
            self._own_offset_rtp = None
            self._bootstrap_history.clear()
            self._failed_blocks = 0

    def _fit_edge(self, in_phase: np.ndarray, centre: float) -> Optional[_EdgeFit]:
        """Localise the polarity flip near ``centre`` to sub-sample precision.

        Extracted from ``_compute_estimate`` so the split-half check can
        localise each sub-fold exactly as the estimate localises the whole
        fold.  Returns None when the window holds no usable crossing; the
        CALLER decides whether that counts as a failed block, because a
        sub-fold that yields nothing is missing evidence rather than a
        failure of the block itself.
        """
        p = self.sample_rate
        c = int(round(centre)) % p
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
        return _EdgeFit(
            edge_offset=float((c - W + x0) % p),
            amplitude=A,
            fit_rms=fit_rms,
            width=float(hi - lo),
        )

    def _compute_estimate(
        self, avg: np.ndarray, registration: int
    ) -> Optional[FineEdgeEstimate]:
        p = self.sample_rate
        # Derotate: squaring removes the BPSK sign, leaving 2× carrier phase.
        phi = 0.5 * float(np.angle(np.mean(avg.astype(np.complex128) ** 2)))
        in_phase = np.real(avg * np.exp(-1j * phi))

        centre = self._search_centre(in_phase, registration)
        if centre is None:
            return None
        fit = self._fit_edge(in_phase, centre)
        if fit is None:
            self._note_block_failed()
            return None
        A = fit.amplitude
        fit_rms = fit.fit_rms
        edge_offset = fit.edge_offset
        # Continuity position of the last edge inside this block, then
        # map to RTP via the median registration.
        k_last = (self._cont // p) - 1
        c_edge = k_last * p + edge_offset
        edge_rtp_float = registration + c_edge
        edge_rtp = int(round(edge_rtp_float))
        subsample = float(edge_rtp_float - edge_rtp)
        self._note_block_estimate(float(edge_rtp % p))

        # --- battery evidence (T6_ACCEPTANCE_CRITERIA.md §4) ---
        mean_abs = (self._abs_sum / self._abs_n) if self._abs_n else 0.0
        retention = (float(np.mean(np.abs(avg))) / mean_abs) if mean_abs > 0 else 0.0
        # peak_prominence: triangle-fidelity residual of the SIGNED T(e)
        # over the WHOLE derotated folded second.  For a clean single
        # flip, T(e) is exactly piecewise-linear (a tent, apex at the
        # flip) by construction, so fit that ideal tent through
        # (0, T[0]) -> (apex, T[apex]) -> (p-1, T[p-1]) and report the RMS
        # deviation from it, normalised by |T[apex]|.  This is a SHAPE
        # check, not an amplitude ratio: noise never traces a tent, no
        # matter how loud, so unlike a bare peak/median ratio (pinned at
        # 2.000 for any clean flip, since a triangle's median sits at half
        # its peak) it keeps working down to worst-hour C/N0.
        #
        # ⛔ Fit the SIGNED T, never |T|.  T's endpoints are +A(p-2e) and
        # -A(p-2e) for an edge at e, so they carry OPPOSITE SIGNS unless
        # the edge splits the fold exactly in half.  Wherever T crosses
        # zero, |T| acquires a V-notch that no endpoint->apex->endpoint
        # triangle can follow, and the residual becomes a function of how
        # far off-centre the edge sits -- an arbitrary per-boot offset,
        # since fold position depends only on where the stream started.
        # Measured under the |T| fit, at 77 dB-Hz, against a 0.015 bound:
        # edge 47916 -> 0.0014, 40000 -> 0.1260, 30000 -> 0.2611,
        # 24000 -> 0.3333, 9600 -> 0.4869, 1000 -> 0.5683.  Every position
        # but mid-fold was refused.  The signed fit reads ~1e-6 at all of
        # them (see `max_fidelity_residual` for the current sweep).
        # argmax(|T|) stays correct for LOCATING the apex -- it finds the
        # extremum whichever sign the flip has.
        t_full = self._closed_form_T(in_phase)
        apex_idx = int(np.argmax(np.abs(t_full)))
        prominence = self._triangle_fidelity(t_full, apex_idx)
        # apex_distance_samples: T(e)'s own apex, independent of the
        # zero-crossing fit above, versus the edge this estimate reports.
        # Catches a lock that fits cleanly but sits on the wrong feature
        # (the B4 2026-09-04 20.000 ms lattice lock) -- a case the shape
        # residual above cannot see, because a lattice phantom's own T(e)
        # can trace a clean tent centred on ITS peak, just not on the true
        # edge this estimate names.
        apex_distance = float(((apex_idx - edge_offset + p / 2) % p) - p / 2)
        width = fit.width
        split_delta = self._split_half_delta(phi)

        return FineEdgeEstimate(
            edge_offset_samples=float(edge_offset),
            # Continuous (unwrapped) domain — consumers that hand this
            # to 32-bit RTP interfaces mask at their own boundary.
            edge_rtp=edge_rtp,
            edge_subsample=subsample,
            n_seconds_folded=self.fold_seconds,
            plateau_amplitude=A,
            fit_rms=fit_rms,
            fold_retention=retention,
            split_half_delta_samples=split_delta,
            peak_prominence=prominence,
            transition_width_samples=width,
            apex_distance_samples=apex_distance,
        )

    @staticmethod
    def _triangle_fidelity(t_full: np.ndarray, apex_idx: int) -> float:
        """RMS deviation of the SIGNED T(e) from the ideal tent through
        (0, T[0]) -> (apex, T[apex]) -> (p-1, T[-1]), over |T[apex]|.

        Returns NaN when T carries no measurable apex -- a dead-flat
        curve or a non-finite one.  0.0 is the most FAVOURABLE value this
        statistic can take, so it must never stand for "no evidence"
        (T6_ACCEPTANCE_CRITERIA.md §4.4, §5.2); NaN compares false
        against an upper bound, so the battery reads it as a refusal.
        """
        n = int(t_full.shape[0])
        if n < 2 or not (0 <= apex_idx < n):
            return float("nan")
        apex_t = float(t_full[apex_idx])
        peak_t = abs(apex_t)
        if not math.isfinite(peak_t) or peak_t <= 0.0:
            return float("nan")
        ideal = np.empty(n)
        ideal[: apex_idx + 1] = np.linspace(float(t_full[0]), apex_t,
                                            apex_idx + 1)
        ideal[apex_idx:] = np.linspace(apex_t, float(t_full[-1]),
                                       n - apex_idx)
        residual = float(np.sqrt(np.mean((t_full - ideal) ** 2)) / peak_t)
        return residual if math.isfinite(residual) else float("nan")

    @staticmethod
    def _closed_form_T(x: np.ndarray) -> np.ndarray:
        """Closed-form matched filter for a single polarity flip at e:
        T(e) = C[p-1] - 2*C[e-1], C = cumsum(x).  For a clean flip this is
        exactly piecewise-linear -- a tent whose apex is the flip position
        -- which both ``_split_half_delta`` (locating the edge) and
        ``_compute_estimate``'s triangle-fidelity check (confirming the
        shape) rely on.  See T6_FOLDED_SELF_ACQUISITION.md §3.1 -- and the
        prohibition there against a plain CUSUM (structurally biased
        1-40 ms when the edge lands near the fold origin).
        """
        c = np.cumsum(x)
        return c[-1] - 2.0 * np.concatenate(([0.0], c[:-1]))

    def _split_half_delta(self, phi: float) -> float:
        """Edge position from the even-second sub-fold minus the odd-second
        sub-fold, in samples, wrapped to (-p/2, p/2].

        Two disjoint folds of the same stable edge must agree to about
        sqrt(2) times the single-fold scatter.  A wandering apex -- the
        nightly B4 behaviour that produces ~270 tier transitions a day --
        separates them.

        ⛔ Both sub-folds are localised by ``_fit_edge``, the same
        zero-crossing fit that produces the reported estimate, so the
        difference lands in the same units and at the same resolution as
        the scatter it polices.  An earlier version took each sub-fold's
        apex with an integer ``argmax`` and interpolated nothing, which
        quantised the answer to one whole sample -- 10.4 us at 96 kHz,
        against a block-to-block scatter measured at 0.26-0.68 us on
        captured B4 signal (T6_NEWELL_VS_FOLD.md §5d).  The check sat
        about fifteen times coarser than the wander it exists to catch and
        reported the most favourable value, exactly 0.0000, on five of six
        real blocks.  A criterion that cannot resolve its own failure mode
        passes everything.

        ⛔ Each sub-fold picks its own centre, by a GLOBAL search over its
        whole folded second (``argmax|T|``), and only then refines that
        position locally.  Seeding both searches from one shared centre
        instead -- the obvious simplification -- destroys this criterion's
        whole value: pinned to the same narrow window, two sub-folds of
        PURE NOISE agree to 0.1-0.9 samples, which sits inside the healthy
        range at 48.4 dB-Hz.  Measured that way, noise becomes
        indistinguishable from signal.  The global search is what keeps
        them apart: two noise folds put their maxima 1308-47937 samples
        apart, and that separation is the battery's strongest single
        refusal of the null.  Resolution must not be bought with it.

        Returns NaN when either sub-fold is empty or yields no usable
        crossing.  0.0 is the most FAVOURABLE value this criterion can
        report -- "the two folds put the edge in the same place" -- so
        using it for "one fold never ran" would read a missing measurement
        as a perfect one.  An empty sub-fold is no evidence, and no
        evidence must never be spelled as agreement
        (T6_ACCEPTANCE_CRITERIA.md §4.4, §5.2).
        """
        p = self.sample_rate
        rot = np.exp(-1j * phi)
        out = []
        for acc, cnt in ((self._acc_even, self._cnt_even),
                         (self._acc_odd, self._cnt_odd)):
            if not np.any(cnt):
                return float("nan")
            sub = np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0)
            ip = np.real(sub * rot)
            # Global: where does THIS sub-fold think the flip is?
            apex = float(np.argmax(np.abs(self._closed_form_T(ip))))
            # Local: refine to sub-sample with the shipped localiser, so
            # the answer lands in the same units as the estimate it is
            # checked against.  A refinement that cannot find a crossing
            # falls back to the apex rather than discarding the block --
            # the global disagreement is the measurement that matters.
            fit = self._fit_edge(ip, apex)
            out.append(fit.edge_offset if fit is not None else apex)
        d = out[0] - out[1]
        return float((d + p / 2) % p - p / 2)
