# T6 Anchor Inversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TS-1 HF-PPS edge define the RTP→UTC anchor (`captured_via_tier="T6"`) via a coherent zero-crossing fine stage, a µs-bounded delay budget, and an explicit authority state machine with loud fallback.

**Architecture:** Two new one-class modules (`BpskEdgeFineStage`, `T6AnchorAuthority`) slot beside the existing `BpskPpsCalibratorMF` (demoted to coarse stage, unchanged internally). `core_recorder_v2` feeds the fine stage in `_t6_on_samples`, names the integer second from the coarse cascade, and lets the authority module own `_t6_native_anchor` when AUTHORITATIVE. The persisted chain-delay store is retired on this path. Spec: `docs/design/T6_ANCHOR_INVERSION_DESIGN.md`.

**Tech Stack:** Python ≥3.10, numpy (complex64 IQ), pytest via `uv run pytest`, TOML config.

## Global Constraints

- One class per file; filename matches class (`bpsk_edge_fine_stage.py` → `BpskEdgeFineStage`).
- Timing-authority invariant (CLAUDE.md): no new `time.time()` / `datetime.now()` / `chronyc tracking` in the timing path. `time.monotonic()` via an injected `now` callable is permitted for *durations only* (DEGRADED dwell), never as a time source.
- Expose-don't-correct: every authority state transition logs at WARNING with the violated invariant named. Never silent fallback.
- Delay budget hard bound: ±1 ms (`abs(delay_budget_ns) > 1_000_000` refuses loudly).
- Config defaults (spec §7): `fine_fold_seconds=30`, `delay_budget_ns=10_000`, `edge_period_tolerance_ns=5000`, `fine_coarse_max_ms=5`, `degraded_unlock_after_sec=600`.
- Tests: `uv run pytest tests/<file> -v` from the repo root (`/root/appliance/repos/hf-timestd`). Full suite must pass before the final commit.
- Commit to `main` after every task (develop-on-main; no feature branches). Do NOT push until the final task.
- All new float DSP in float64 internally; inputs arrive complex64.

---

### Task 1: `BpskEdgeFineStage` — fold buffer, continuity indexing, RTP registration

**Files:**
- Create: `src/hf_timestd/core/bpsk_edge_fine_stage.py`
- Test: `tests/test_bpsk_edge_fine_stage.py`

**Interfaces:**
- Consumes: nothing from other tasks. Same feed contract as `BpskPpsCalibratorMF.process_samples(iq_samples: np.ndarray, rtp_timestamp: int)`.
- Produces (later tasks rely on these exact names):

```python
@dataclass(frozen=True)
class FineEdgeEstimate:
    edge_offset_samples: float  # zero-crossing position within the 1-s fold, [0, sample_rate)
    edge_rtp: int               # 32-bit RTP of the sample nearest the edge
    edge_subsample: float       # true edge is at (edge_rtp + edge_subsample), in [-0.5, 0.5)
    n_seconds_folded: int
    plateau_amplitude: float    # |I| plateau level of the folded average
    fit_rms: float              # RMS residual of the linear fit, normalised by plateau_amplitude

class BpskEdgeFineStage:
    def __init__(self, sample_rate: int, fold_seconds: int = 30,
                 search_window_ms: float = 6.0): ...
    def set_coarse_offset_samples(self, offset: float) -> None: ...
    def process_samples(self, iq_samples, rtp_timestamp) -> Optional[FineEdgeEstimate]: ...
    def reset(self) -> None: ...
    blocks_discarded: int       # counter: fold blocks dropped for registration spread
```

This task builds the accumulation half only; `_compute_estimate()` is a stub returning `None` until Task 2 (so `process_samples` returns `None` always, but fold/registration state is fully testable via private attrs).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bpsk_edge_fine_stage.py
"""Fold/continuity accumulation for the T6 fine-stage estimator.

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §3.
"""
import numpy as np
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage

SR = 96_000


def feed_batches(stage, iq, rtp0, batch=1740, mislabel=None):
    """Feed iq in batches with declared RTP = rtp0 + offset (+ mislabel[i])."""
    i = 0
    b = 0
    out = []
    while i < len(iq):
        chunk = iq[i:i + batch]
        decl = (rtp0 + i) & 0xFFFFFFFF
        if mislabel and b in mislabel:
            decl = (decl + mislabel[b]) & 0xFFFFFFFF
        r = stage.process_samples(chunk, decl)
        if r is not None:
            out.append(r)
        i += batch
        b += 1
    return out


class TestFoldAccumulation:
    def test_continuity_counter_advances_by_samples_received(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=1000)
        assert stage._cont == SR

    def test_fold_sign_alternates_per_second(self):
        # Constant +1 signal folded over 2 s with (-1)^n cancels to ~0;
        # a signal that flips polarity each second folds coherently.
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        const = np.ones(2 * SR, dtype=np.complex64)
        feed_batches(stage, const, rtp0=0)
        avg_const = stage._last_avg_for_test  # captured before reset (see Step 3)
        assert np.max(np.abs(avg_const)) < 1e-6

        stage2 = BpskEdgeFineStage(SR, fold_seconds=2)
        alt = np.ones(2 * SR, dtype=np.complex64)
        alt[SR:] = -1.0
        feed_batches(stage2, alt, rtp0=0)
        avg_alt = stage2._last_avg_for_test
        assert np.min(np.abs(avg_alt)) > 0.99

    def test_batch_spanning_second_boundary_splits_sign_correctly(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        alt = np.ones(2 * SR, dtype=np.complex64)
        alt[SR:] = -1.0
        # 7-sample-offset start so batches straddle the boundary
        feed_batches(stage, alt, rtp0=0, batch=1747)
        assert np.min(np.abs(stage._last_avg_for_test)) > 0.99


class TestRtpRegistration:
    def test_registration_median_ignores_minority_mislabels(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        iq = np.zeros(2 * SR, dtype=np.complex64)
        n_batches = int(np.ceil(len(iq) / 1740))
        # 9% of batches declare RTP 60 samples high (measured B4 signature)
        bad = set(range(0, n_batches, 11))
        feed_batches(stage, iq, rtp0=5_000_000, mislabel={b: +60 for b in bad})
        assert stage._registration_for_test() == 5_000_000

    def test_registration_spread_beyond_limit_discards_block(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=1)
        iq = np.zeros(SR, dtype=np.complex64)
        # A mid-block +500-sample declared jump = real gap signature
        n_batches = int(np.ceil(len(iq) / 1740))
        feed_batches(stage, iq, rtp0=0,
                     mislabel={b: +500 for b in range(n_batches // 2, n_batches)})
        assert stage.blocks_discarded == 1

    def test_two_second_declared_jump_resets_stage(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=4)
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=0)
        cont_before = stage._cont
        assert cont_before == SR
        # stream restart: declared RTP jumps 3 s
        stage.process_samples(np.zeros(1740, dtype=np.complex64),
                              (SR + 3 * SR) & 0xFFFFFFFF)
        assert stage._cont == 1740  # counted from fresh reset

    def test_rtp_wrap_in_declared_timestamps_is_handled(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=1)
        rtp0 = (2**32 - SR // 2) & 0xFFFFFFFF  # wraps mid-block
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=rtp0)
        assert stage.blocks_discarded == 0
        assert stage._registration_for_test() == rtp0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bpsk_edge_fine_stage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hf_timestd.core.bpsk_edge_fine_stage'`

- [ ] **Step 3: Write the implementation**

```python
# src/hf_timestd/core/bpsk_edge_fine_stage.py
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
            self._reg_base = decl  # continuity restarts at 0 here
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
            # Registration is re-derived per block: reset() clears
            # _reg_base, and the next batch's declared RTP re-registers it.
            self.reset()
            return est
        return None

    def _registration_for_test(self) -> Optional[int]:
        if self._reg_base is None or not self._reg_rel:
            return None
        return (self._reg_base + int(np.median(self._reg_rel))) & 0xFFFFFFFF

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
        return self._compute_estimate(avg, registration)

    def _compute_estimate(self, avg: np.ndarray,
                          registration: int) -> Optional[FineEdgeEstimate]:
        # Task 2 implements localisation.
        return None
```

Note on `test_two_second_declared_jump_resets_stage`: after the internal `reset()` the batch that triggered it must still be accumulated — move the fold-accumulation code so the restart path falls through to it (i.e., detect restart *before* appending `rel`, then continue processing the batch normally with the fresh state). Structure `process_samples` accordingly (guard first, then registration append, then accumulate).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bpsk_edge_fine_stage.py -v`
Expected: all PASS. If `test_two_second_declared_jump_resets_stage` fails with `_cont == 0`, re-check the restart fall-through described in Step 3.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py tests/test_bpsk_edge_fine_stage.py
git commit -m "feat(t6): fine-stage fold buffer with continuity indexing + median RTP registration"
```

---

### Task 2: Fine-stage localisation — derotation + zero-crossing

**Files:**
- Modify: `src/hf_timestd/core/bpsk_edge_fine_stage.py` (implement `_compute_estimate`)
- Test: `tests/test_bpsk_edge_fine_stage.py` (append)

**Interfaces:**
- Consumes: Task 1's `_compute_estimate(avg, registration)` hook and `FineEdgeEstimate`.
- Produces: `_compute_estimate` returning real `FineEdgeEstimate`s; a module-level test helper `make_bpsk(...)` other test files may import.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bpsk_edge_fine_stage.py`:

```python
def make_bpsk(sr, seconds, edge_offset_samples, edge_width_us=52.0,
              amp=1.0, tilt_per_s=0.0, phase=0.3, noise_rms=0.0,
              start_second=0, seed=1):
    """Synthetic TS-1 baseband: one smooth polarity flip per second at
    edge_offset_samples, base polarity alternating per second (so the
    signal is continuous across second boundaries), constant carrier
    phase, optional linear amplitude tilt across each second and AWGN.

    10–90% transition width edge_width_us (tanh ramp; 10–90% of tanh
    spans 2.197·tau, so tau = width/2.197).
    """
    n = sr * seconds
    t = np.arange(n)
    sec = t // sr + start_second
    pos = t % sr
    base = (1 - 2 * (sec & 1)).astype(np.float64)
    tau = (edge_width_us * 1e-6 * sr) / 2.197
    ramp = np.tanh((pos - edge_offset_samples) / tau)
    env = amp * (1.0 + tilt_per_s * (pos / sr))
    sig = env * base * ramp * np.exp(1j * phase)
    if noise_rms:
        rng = np.random.default_rng(seed)
        sig = sig + noise_rms * (rng.standard_normal(n)
                                 + 1j * rng.standard_normal(n)) / np.sqrt(2)
    return sig.astype(np.complex64)


def run_stage(sr, iq, coarse, fold_seconds, rtp0=123_456, **stage_kw):
    stage = BpskEdgeFineStage(sr, fold_seconds=fold_seconds, **stage_kw)
    stage.set_coarse_offset_samples(coarse)
    ests = feed_batches(stage, iq, rtp0=rtp0)
    return stage, ests


class TestLocalisation:
    EDGE = 43_181.4  # arbitrary fractional position within the second

    def test_clean_signal_localises_to_subsample(self):
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.05)
        _, ests = run_stage(SR, iq, coarse=self.EDGE + 30, fold_seconds=10)
        assert len(ests) == 1
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 1.0

    def test_edge_rtp_and_subsample_name_the_true_edge(self):
        rtp0 = 7_654_321
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.0)
        _, ests = run_stage(SR, iq, coarse=self.EDGE, fold_seconds=10,
                            rtp0=rtp0)
        est = ests[0]
        # true edge RTP positions are rtp0 + k*SR + EDGE; est must name one
        total = (est.edge_rtp - rtp0) % SR + est.edge_subsample
        err_us = abs(total - self.EDGE) / SR * 1e6
        assert err_us < 1.0
        assert -0.5 <= est.edge_subsample < 0.5

    def test_amplitude_tilt_moves_argmax_but_not_zero_crossing(self):
        # 5%/s amplitude tilt: the ±0.5 s boxcar argmax walks by ms
        # (the flat-apex defect); the zero crossing must not.
        iq = make_bpsk(SR, 10, self.EDGE, tilt_per_s=0.05, noise_rms=0.02)
        _, ests = run_stage(SR, iq, coarse=self.EDGE, fold_seconds=10)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

        # Demonstrate the defect the fine stage fixes: boxcar argmax on
        # the same folded data shifts by >100 µs.
        one_sec = np.real(
            make_bpsk(SR, 1, self.EDGE, tilt_per_s=0.05)
            * np.exp(-1j * 0.3))
        N = SR // 2
        c = np.cumsum(np.concatenate([one_sec, one_sec]))  # periodic ext.
        y = np.abs((c[N:N + SR] - c[:SR]) - (c[SR:2 * SR] - c[N:N + SR]))
        argmax_err_us = abs(
            ((np.argmax(y) - self.EDGE + SR / 2) % SR - SR / 2)) / SR * 1e6
        assert argmax_err_us > 100.0

    def test_s16_quantisation_at_34_counts_rms_still_localises(self):
        # B4 measured T6 at ~34 counts RMS of ±32767 under s16.
        iq = make_bpsk(SR, 30, self.EDGE, amp=34.0, noise_rms=1.0)
        q = (np.round(iq.real) + 1j * np.round(iq.imag)).astype(np.complex64)
        _, ests = run_stage(SR, q, coarse=self.EDGE, fold_seconds=30)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

    def test_parity_of_start_second_does_not_move_the_edge(self):
        a = make_bpsk(SR, 10, self.EDGE, start_second=0, noise_rms=0.02)
        b = make_bpsk(SR, 10, self.EDGE, start_second=1, noise_rms=0.02,
                      seed=2)
        _, ea = run_stage(SR, a, coarse=self.EDGE, fold_seconds=10)
        _, eb = run_stage(SR, b, coarse=self.EDGE, fold_seconds=10)
        diff_us = abs(ea[0].edge_offset_samples
                      - eb[0].edge_offset_samples) / SR * 1e6
        assert diff_us < 2.0

    def test_mislabelled_batches_do_not_smear_the_edge(self):
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.02)
        stage = BpskEdgeFineStage(SR, fold_seconds=10)
        stage.set_coarse_offset_samples(self.EDGE)
        n_batches = int(np.ceil(len(iq) / 1740))
        bad = {b: +60 for b in range(0, n_batches, 11)}
        ests = feed_batches(stage, iq, rtp0=999, mislabel=bad)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

    def test_no_coarse_offset_returns_none(self):
        iq = make_bpsk(SR, 2, self.EDGE)
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        ests = feed_batches(stage, iq, rtp0=0)
        assert ests == []

    def test_edge_near_fold_boundary_wraps_cleanly(self):
        edge = 20.0  # 20 samples after the fold wrap point
        iq = make_bpsk(SR, 10, edge, noise_rms=0.02)
        _, ests = run_stage(SR, iq, coarse=edge, fold_seconds=10)
        err_us = abs(((ests[0].edge_offset_samples - edge + SR / 2) % SR
                      - SR / 2)) / SR * 1e6
        assert err_us < 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bpsk_edge_fine_stage.py::TestLocalisation -v`
Expected: FAIL — `_compute_estimate` returns `None`, so `ests` is empty / `IndexError`.

- [ ] **Step 3: Implement `_compute_estimate`**

Replace the Task 1 stub in `bpsk_edge_fine_stage.py`:

```python
    # Linear-fit band: samples with |I| below this fraction of the
    # plateau participate in the zero-crossing fit (spec §3: ∓40%).
    FIT_BAND_FRACTION = 0.4

    def _compute_estimate(self, avg: np.ndarray,
                          registration: int) -> Optional[FineEdgeEstimate]:
        if self._coarse_offset is None:
            return None
        p = self.sample_rate
        # Derotate: squaring removes the BPSK sign, leaving 2× carrier phase.
        phi = 0.5 * float(np.angle(np.mean(avg.astype(np.complex128) ** 2)))
        I = np.real(avg * np.exp(-1j * phi))

        c = int(round(self._coarse_offset)) % p
        W = max(8, int(self.search_window_ms * 1e-3 * p))
        seg = np.take(I, np.arange(c - W, c + W + 1) % p)

        outer = np.concatenate([seg[:W // 2], seg[-(W // 2):]])
        A = float(np.median(np.abs(outer)))
        if A <= 0.0:
            return None
        # Normalise polarity: seg must rise −A → +A through the edge.
        if float(np.median(seg[:W // 2])) > 0.0:
            seg = -seg

        rising = np.nonzero(np.diff(np.sign(seg)) > 0)[0]
        if len(rising) == 0:
            return None
        k = int(rising[np.argmin(np.abs(rising - W))])

        band = self.FIT_BAND_FRACTION * A
        lo, hi = k, k + 1
        while lo > 0 and abs(seg[lo - 1]) < band:
            lo -= 1
        while hi < len(seg) - 1 and abs(seg[hi + 1]) < band:
            hi += 1
        if hi - lo < 1:
            return None
        xs = np.arange(lo, hi + 1, dtype=np.float64)
        ys = seg[lo:hi + 1].astype(np.float64)
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
```

- [ ] **Step 4: Run the full file's tests**

Run: `uv run pytest tests/test_bpsk_edge_fine_stage.py -v`
Expected: all PASS (Task 1 tests must still pass). If the tilt test's argmax
demonstration fails, check the boxcar construction, not the fine stage — the
fine-stage assertion and the argmax assertion are independent.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py tests/test_bpsk_edge_fine_stage.py
git commit -m "feat(t6): zero-crossing localisation for the fine stage — tilt-invariant to 2nd order"
```

---

### Task 3: `T6AnchorAuthority` state machine

**Files:**
- Create: `src/hf_timestd/core/t6_anchor_authority.py`
- Test: `tests/test_t6_anchor_authority.py`

**Interfaces:**
- Consumes: `FineEdgeEstimate` (Task 1), `NativeAnchor` (existing, `hf_timestd.core.native_anchor`).
- Produces (Task 5 relies on these exact names):

```python
class T6AuthorityState(str, Enum):
    ACQUIRING = "ACQUIRING"
    AUTHORITATIVE = "AUTHORITATIVE"
    DEGRADED = "DEGRADED"
    UNLOCKED = "UNLOCKED"

@dataclass(frozen=True)
class T6AnchorDecision:
    state: T6AuthorityState
    previous_state: T6AuthorityState
    anchor: Optional[NativeAnchor]   # valid anchor while AUTHORITATIVE/DEGRADED
    violations: tuple[str, ...]      # invariant names, empty when clean

class T6AnchorAuthority:
    def __init__(self, sample_rate_hz: int, delay_budget_ns: int,
                 edge_period_tolerance_ns: int = 5_000,
                 fine_coarse_max_ms: float = 5.0,
                 degraded_unlock_after_sec: float = 600.0,
                 now: Callable[[], float] = time.monotonic): ...
    def on_fine_estimate(self, est: FineEdgeEstimate,
                         coarse_offset_samples: Optional[float],
                         named_second_utc: Optional[int]) -> T6AnchorDecision: ...
    def on_mf_unlock(self) -> T6AnchorDecision: ...
    @property
    def state(self) -> T6AuthorityState: ...
```

Violation names (exact strings, used in logs/status): `"edge_period"`, `"fine_coarse"`, `"naming_unavailable"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_t6_anchor_authority.py
"""T6 anchor authority state machine.

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §4, §6.
"""
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_anchor_authority import (
    T6AnchorAuthority, T6AuthorityState,
)

SR = 96_000
SECOND = 1_700_000_000
BUDGET = 10_000


def est(offset=43_181.0, rtp=1_000_000, sub=0.25, n=30):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=sub,
        n_seconds_folded=n, plateau_amplitude=30.0, fit_rms=0.05,
    )


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def auth():
    clock = FakeClock()
    a = T6AnchorAuthority(SR, BUDGET, now=clock)
    a._test_clock = clock
    return a


class TestDelayBudgetBound:
    def test_budget_beyond_1ms_refuses_at_construction(self):
        with pytest.raises(ValueError, match="delay_budget"):
            T6AnchorAuthority(SR, 1_000_001)
        with pytest.raises(ValueError, match="delay_budget"):
            T6AnchorAuthority(SR, -1_000_001)

    def test_budget_within_bound_accepted(self):
        T6AnchorAuthority(SR, 999_999)


class TestTransitions:
    def test_first_clean_estimate_becomes_authoritative(self, auth):
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND)
        assert d.previous_state is T6AuthorityState.ACQUIRING
        assert d.state is T6AuthorityState.AUTHORITATIVE
        assert d.violations == ()
        assert d.anchor is not None
        assert d.anchor.captured_via_tier == "T6"

    def test_anchor_math(self, auth):
        d = auth.on_fine_estimate(est(sub=0.25), 43_181.0, SECOND)
        a = d.anchor
        assert a.anchor_rtp == 1_000_000
        # sample at edge_rtp acquired sub/SR BEFORE the true edge instant
        expected = SECOND * 10**9 + BUDGET - round(0.25 * 1e9 / SR)
        assert a.anchor_utc_ns == expected
        assert a.chain_delay_ns == BUDGET

    def test_naming_unavailable_while_acquiring_stays_acquiring(self, auth):
        d = auth.on_fine_estimate(est(), 43_181.0, None)
        assert d.state is T6AuthorityState.ACQUIRING
        assert "naming_unavailable" in d.violations
        assert d.anchor is None

    def test_edge_period_violation_degrades_and_holds_anchor(self, auth):
        d1 = auth.on_fine_estimate(est(offset=43_181.0), 43_181.0, SECOND)
        # next block's edge moved 10 µs within the second (> 5 µs tol)
        moved = 43_181.0 + 10e-6 * SR
        d2 = auth.on_fine_estimate(est(offset=moved), moved, SECOND + 30)
        assert d2.state is T6AuthorityState.DEGRADED
        assert "edge_period" in d2.violations
        assert d2.anchor == d1.anchor  # held, not replaced

    def test_fine_coarse_violation_degrades(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        # coarse 6 ms away from fine (> 5 ms tol); period still clean
        d = auth.on_fine_estimate(
            est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        assert d.state is T6AuthorityState.DEGRADED
        assert "fine_coarse" in d.violations

    def test_degraded_recovers_on_clean_estimate(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_fine_estimate(est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND + 60)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_degraded_past_dwell_unlocks(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_fine_estimate(est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        auth._test_clock.t += 601.0
        d = auth.on_fine_estimate(
            est(), 43_181.0 + 0.006 * SR, SECOND + 660)
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_mf_unlock_from_authoritative_unlocks(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        d = auth.on_mf_unlock()
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_unlocked_reacquires_on_next_clean_estimate(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_mf_unlock()
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND + 60)
        assert d.previous_state is T6AuthorityState.UNLOCKED
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_missing_coarse_skips_fine_coarse_check(self, auth):
        d = auth.on_fine_estimate(est(), None, SECOND)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_period_check_wraps_across_the_second(self, auth):
        auth.on_fine_estimate(est(offset=2.0), 2.0, SECOND)
        # SR-1 ≡ −1 sample: wrapped distance 3 samples ≈ 31 µs > tol → degrade;
        # but 2.0 → 2.0 + 0.3 samples (≈3 µs) must NOT degrade.
        d = auth.on_fine_estimate(est(offset=2.3), 2.3, SECOND + 30)
        assert d.state is T6AuthorityState.AUTHORITATIVE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_t6_anchor_authority.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/hf_timestd/core/t6_anchor_authority.py
"""T6 anchor authority — owns "is T6 the RTP→UTC anchor authority now".

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §2, §4, §6.  When
AUTHORITATIVE, the anchor is built by the inversion:

    anchor_utc_ns = named_integer_second·1e9 + delay_budget_ns − subsample

The coarse cascade only NAMES the integer second (±0.5 s duty); its
noise cannot enter the sub-second value by construction.  All state
transitions are returned to the caller for loud logging — this module
never logs silently-consequential decisions itself, and it never
consults a wall clock (the injected ``now`` measures DEGRADED dwell
only).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.native_anchor import NativeAnchor

_BILLION = 1_000_000_000
DELAY_BUDGET_BOUND_NS = 1_000_000  # ±1 ms hard physical bound (spec §5)


class T6AuthorityState(str, Enum):
    ACQUIRING = "ACQUIRING"
    AUTHORITATIVE = "AUTHORITATIVE"
    DEGRADED = "DEGRADED"
    UNLOCKED = "UNLOCKED"


@dataclass(frozen=True)
class T6AnchorDecision:
    state: T6AuthorityState
    previous_state: T6AuthorityState
    anchor: Optional[NativeAnchor]
    violations: tuple


class T6AnchorAuthority:
    def __init__(self, sample_rate_hz: int, delay_budget_ns: int,
                 edge_period_tolerance_ns: int = 5_000,
                 fine_coarse_max_ms: float = 5.0,
                 degraded_unlock_after_sec: float = 600.0,
                 now: Callable[[], float] = time.monotonic):
        if abs(int(delay_budget_ns)) > DELAY_BUDGET_BOUND_NS:
            raise ValueError(
                f"delay_budget_ns={delay_budget_ns} exceeds the ±1 ms "
                f"physical bound: the analog TS-1→ADC path plus channel-"
                f"filter group delay is microseconds to sub-millisecond "
                f"(docs/design/T6_ANCHOR_INVERSION_DESIGN.md §5); a "
                f"larger value is absorbing timestamp error, not "
                f"measuring a chain delay."
            )
        self.sample_rate_hz = int(sample_rate_hz)
        self.delay_budget_ns = int(delay_budget_ns)
        self.edge_period_tolerance_ns = int(edge_period_tolerance_ns)
        self.fine_coarse_max_ms = float(fine_coarse_max_ms)
        self.degraded_unlock_after_sec = float(degraded_unlock_after_sec)
        self._now = now
        self._state = T6AuthorityState.ACQUIRING
        self._anchor: Optional[NativeAnchor] = None
        self._prev_offset: Optional[float] = None
        self._degraded_since: Optional[float] = None

    @property
    def state(self) -> T6AuthorityState:
        return self._state

    def _wrapped_distance_samples(self, a: float, b: float) -> float:
        p = self.sample_rate_hz
        return abs((a - b + p / 2) % p - p / 2)

    def _check(self, est: FineEdgeEstimate,
               coarse_offset_samples: Optional[float],
               named_second_utc: Optional[int]) -> tuple:
        v = []
        if self._prev_offset is not None:
            d_ns = (self._wrapped_distance_samples(
                est.edge_offset_samples, self._prev_offset)
                / self.sample_rate_hz * 1e9)
            if d_ns > self.edge_period_tolerance_ns:
                v.append("edge_period")
        if coarse_offset_samples is not None:
            d_ms = (self._wrapped_distance_samples(
                est.edge_offset_samples, coarse_offset_samples)
                / self.sample_rate_hz * 1e3)
            if d_ms > self.fine_coarse_max_ms:
                v.append("fine_coarse")
        if named_second_utc is None:
            v.append("naming_unavailable")
        return tuple(v)

    def _build_anchor(self, est: FineEdgeEstimate,
                      named_second_utc: int) -> NativeAnchor:
        sub_ns = int(round(est.edge_subsample * 1e9 / self.sample_rate_hz))
        return NativeAnchor(
            anchor_rtp=int(est.edge_rtp) & 0xFFFFFFFF,
            anchor_utc_ns=(named_second_utc * _BILLION
                           + self.delay_budget_ns - sub_ns),
            sample_rate_hz=self.sample_rate_hz,
            chain_delay_ns=self.delay_budget_ns,
            captured_at_utc_ns=named_second_utc * _BILLION,
            captured_via_tier="T6",
        )

    def on_fine_estimate(self, est: FineEdgeEstimate,
                         coarse_offset_samples: Optional[float],
                         named_second_utc: Optional[int]) -> T6AnchorDecision:
        prev = self._state
        violations = self._check(est, coarse_offset_samples, named_second_utc)

        if not violations:
            self._anchor = self._build_anchor(est, named_second_utc)
            self._prev_offset = est.edge_offset_samples
            self._degraded_since = None
            self._state = T6AuthorityState.AUTHORITATIVE
            return T6AnchorDecision(self._state, prev, self._anchor, ())

        if prev in (T6AuthorityState.ACQUIRING, T6AuthorityState.UNLOCKED):
            # Not yet authoritative — keep (re)acquiring.  Track the
            # offset so periodicity has a reference once estimates clean up.
            self._prev_offset = est.edge_offset_samples
            self._state = T6AuthorityState.ACQUIRING
            return T6AnchorDecision(self._state, prev, None, violations)

        # AUTHORITATIVE or DEGRADED with a violation → DEGRADED, hold
        # the last good anchor (GPSDO lets us coast), start/continue dwell.
        if self._degraded_since is None:
            self._degraded_since = self._now()
        if (self._now() - self._degraded_since
                > self.degraded_unlock_after_sec):
            return self._unlock(prev, violations)
        self._state = T6AuthorityState.DEGRADED
        return T6AnchorDecision(self._state, prev, self._anchor, violations)

    def on_mf_unlock(self) -> T6AnchorDecision:
        return self._unlock(self._state, ("mf_unlock",))

    def _unlock(self, prev: T6AuthorityState,
                violations: tuple) -> T6AnchorDecision:
        self._state = T6AuthorityState.UNLOCKED
        self._anchor = None
        self._prev_offset = None
        self._degraded_since = None
        return T6AnchorDecision(self._state, prev, None, violations)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_t6_anchor_authority.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/t6_anchor_authority.py tests/test_t6_anchor_authority.py
git commit -m "feat(t6): anchor authority state machine — inversion anchor, invariants, loud fallback"
```

---

### Task 4: Config keys, validation, template

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (config-parse block, near line 551 `_t6_cfg = _timing_section.get('t6_pps')`)
- Modify: `config/timestd-config.toml.template` (the `[timing.t6_pps]` section)
- Test: `tests/test_core_recorder_t6_fine_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `CoreRecorderV2._t6_fine_settings(cfg: dict) -> dict` — a **static method** so tests need no constructed recorder. Returns keys exactly: `fine_fold_seconds:int, delay_budget_ns:int, edge_period_tolerance_ns:int, fine_coarse_max_ms:float, degraded_unlock_after_sec:float, fine_stage_enabled:bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_core_recorder_t6_fine_config.py
"""[timing.t6_pps] fine-stage/authority key parsing (spec §7)."""
import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class TestFineSettings:
    def test_defaults(self):
        s = CoreRecorderV2._t6_fine_settings({})
        assert s == {
            'fine_stage_enabled': True,
            'fine_fold_seconds': 30,
            'delay_budget_ns': 10_000,
            'edge_period_tolerance_ns': 5_000,
            'fine_coarse_max_ms': 5.0,
            'degraded_unlock_after_sec': 600.0,
        }

    def test_overrides(self):
        s = CoreRecorderV2._t6_fine_settings({
            'fine_stage_enabled': False,
            'fine_fold_seconds': 10,
            'delay_budget_ns': 250_000,
            'edge_period_tolerance_ns': 2_000,
            'fine_coarse_max_ms': 3.5,
            'degraded_unlock_after_sec': 120,
        })
        assert s['fine_stage_enabled'] is False
        assert s['fine_fold_seconds'] == 10
        assert s['delay_budget_ns'] == 250_000
        assert s['fine_coarse_max_ms'] == 3.5
        assert s['degraded_unlock_after_sec'] == 120.0

    def test_delay_budget_beyond_bound_raises(self):
        with pytest.raises(ValueError, match="delay_budget"):
            CoreRecorderV2._t6_fine_settings({'delay_budget_ns': 2_000_000})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core_recorder_t6_fine_config.py -v`
Expected: FAIL with `AttributeError: ... has no attribute '_t6_fine_settings'`

- [ ] **Step 3: Implement**

Add to `CoreRecorderV2` (place next to the other `@staticmethod` helpers; one exists at `_parse_chronyc_last_offset`, line ~2849):

```python
    @staticmethod
    def _t6_fine_settings(t6_cfg: dict) -> dict:
        """Parse + validate the fine-stage/authority keys of
        [timing.t6_pps] (spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md
        §7).  Raises ValueError on a delay budget outside the ±1 ms
        physical bound — a larger value is absorbing timestamp error,
        not measuring a chain delay, and must refuse loudly."""
        from hf_timestd.core.t6_anchor_authority import DELAY_BUDGET_BOUND_NS
        s = {
            'fine_stage_enabled': bool(t6_cfg.get('fine_stage_enabled', True)),
            'fine_fold_seconds': int(t6_cfg.get('fine_fold_seconds', 30)),
            'delay_budget_ns': int(t6_cfg.get('delay_budget_ns', 10_000)),
            'edge_period_tolerance_ns': int(
                t6_cfg.get('edge_period_tolerance_ns', 5_000)),
            'fine_coarse_max_ms': float(t6_cfg.get('fine_coarse_max_ms', 5.0)),
            'degraded_unlock_after_sec': float(
                t6_cfg.get('degraded_unlock_after_sec', 600.0)),
        }
        if abs(s['delay_budget_ns']) > DELAY_BUDGET_BOUND_NS:
            raise ValueError(
                f"[timing.t6_pps].delay_budget_ns={s['delay_budget_ns']} "
                f"exceeds the ±1 ms physical bound (analog path + channel-"
                f"filter group delay is µs to sub-ms; see "
                f"docs/design/T6_ANCHOR_INVERSION_DESIGN.md §5)"
            )
        return s
```

Then in `config/timestd-config.toml.template`, find the `[timing.t6_pps]` section and append (commented-out defaults, matching the template's existing style — read the surrounding section first and mimic its comment format):

```toml
# --- T6 anchor inversion (docs/design/T6_ANCHOR_INVERSION_DESIGN.md) ---
# fine_stage_enabled = true        # coherent zero-crossing fine stage + T6 anchor authority
# fine_fold_seconds = 30           # coherent fold length K (seconds)
# delay_budget_ns = 10000          # TS-1 mod delay ~10 µs; group-delay term 0 pending
#                                  # fleet characterisation. HARD BOUND ±1 ms.
# edge_period_tolerance_ns = 5000  # invariant: successive edges advance by exactly 1 s
# fine_coarse_max_ms = 5           # invariant: |fine − MF apex| bound
# degraded_unlock_after_sec = 600  # DEGRADED longer than this → UNLOCKED
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core_recorder_t6_fine_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py config/timestd-config.toml.template tests/test_core_recorder_t6_fine_config.py
git commit -m "feat(t6): [timing.t6_pps] fine-stage/authority config keys with ±1 ms budget bound"
```

---

### Task 5: core_recorder integration — feed, naming, anchor ownership, store retirement

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`:
  - T6 init block (where `BpskPpsCalibratorMF` is constructed, ~line 577-624)
  - `_t6_on_samples` (~line 2915; the calibrator feed is at ~line 3007)
  - the disambiguation "preferred path" store load inside the `result.locked` / first-lock branch (~line 3096-3110) and the store write sites
- Test: `tests/test_core_recorder_t6_fine_integration.py`

**Interfaces:**
- Consumes: `BpskEdgeFineStage`, `FineEdgeEstimate` (Tasks 1-2); `T6AnchorAuthority`, `T6AnchorDecision`, `T6AuthorityState` (Task 3); `_t6_fine_settings` (Task 4).
- Produces (Task 6 relies on): instance attrs `self._t6_fine_stage`, `self._t6_authority`, `self._t6_authority_last_decision: Optional[T6AnchorDecision]`; methods `_t6_name_integer_second(edge_rtp:int) -> Optional[int]` and `_t6_apply_authority_decision(decision) -> None`.

**Read first:** `tests/test_core_recorder_t6_shared.py` — integration tests there construct the recorder via `CoreRecorderV2.__new__` and set only the attributes the method under test touches. Mirror that pattern; do not boot a full recorder.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_core_recorder_t6_fine_integration.py
"""Anchor-inversion wiring in core_recorder_v2 (spec §2, §4, §6).

Constructs CoreRecorderV2 via __new__ (established pattern in
test_core_recorder_t6_shared.py) and drives the new helper methods
directly.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_anchor_authority import (
    T6AnchorAuthority, T6AuthorityState,
)

SR = 96_000
SECOND = 1_700_000_000


def bare_recorder():
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._t6_channel_info = SimpleNamespace()  # opaque; rtp_to_wallclock mocked
    r._lb1421_probe = None
    r._t6_native_anchor = None
    r._t6_authority = T6AnchorAuthority(SR, 10_000)
    r._t6_authority_last_decision = None
    return r


def est(rtp=1_000_000, offset=43_181.0):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=0.0,
        n_seconds_folded=30, plateau_amplitude=30.0, fit_rms=0.05,
    )


class TestNaming:
    def test_names_from_nmea_when_probe_fresh(self):
        r = bare_recorder()
        r._lb1421_probe = SimpleNamespace(
            get_latest=lambda: SimpleNamespace(pps_utc_sec=SECOND))
        # radiod-pair wall estimate is 80 ms off the true second — naming
        # must still round to the NMEA-attested second.
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) + 0.080):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_falls_back_to_wall_rounding_without_probe(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) - 0.120):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_residual_beyond_0p4s_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) + 0.45):
            assert r._t6_name_integer_second(1_000_000) is None

    def test_wallclock_unavailable_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock', return_value=None):
            assert r._t6_name_integer_second(1_000_000) is None


class TestAnchorOwnership:
    def test_authoritative_decision_installs_t6_anchor(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d)
        assert r._t6_native_anchor is d.anchor
        assert r._t6_native_anchor.captured_via_tier == "T6"
        # transition ACQUIRING→AUTHORITATIVE is loud
        assert any("AUTHORITATIVE" in m for m in caplog.messages)

    def test_unlock_invalidates_anchor_loudly(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        d2 = r._t6_authority.on_mf_unlock()
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is None
        assert any("UNLOCKED" in m and "mf_unlock" in m
                   for m in caplog.messages)

    def test_degraded_holds_anchor_and_names_violation(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        moved = 43_181.0 + 10e-6 * SR
        d2 = r._t6_authority.on_fine_estimate(
            est(offset=moved), moved, SECOND + 30)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is d1.anchor
        assert any("DEGRADED" in m and "edge_period" in m
                   for m in caplog.messages)

    def test_same_state_clean_updates_anchor_without_warning(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        d2 = r._t6_authority.on_fine_estimate(
            est(rtp=1_000_000 + 30 * SR), 43_181.0, SECOND + 30)
        with caplog.at_level("WARNING"):
            caplog.clear()
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is d2.anchor
        assert caplog.messages == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core_recorder_t6_fine_integration.py -v`
Expected: FAIL with `AttributeError: ... '_t6_name_integer_second'`

- [ ] **Step 3: Implement the two helper methods**

Add to `CoreRecorderV2` (near `_t6_disambiguate_via_t5_lb1421`):

```python
    def _t6_name_integer_second(self, edge_rtp: int) -> Optional[int]:
        """Name the integer UTC second of a fine-stage edge (spec §2).

        The coarse cascade (T5 NMEA preferred, radiod-pair wall estimate
        as fallback) only NAMES the second — it needs ±0.5 s accuracy
        and its noise cannot enter the sub-second value.  Residual
        beyond ±0.4 s (margin inside the cell) → None (invariant 3)."""
        try:
            from ka9q.rtp_recorder import rtp_to_wallclock
            wall = rtp_to_wallclock(
                int(edge_rtp) & 0xFFFFFFFF, self._t6_channel_info)
        except Exception:
            return None
        if wall is None:
            return None
        probe = getattr(self, '_lb1421_probe', None)
        reading = probe.get_latest() if probe is not None else None
        if reading is not None:
            named = (int(reading.pps_utc_sec)
                     + int(round(wall - reading.pps_utc_sec)))
        else:
            named = int(round(wall))
        if abs(wall - named) > 0.4:
            return None
        return named

    def _t6_apply_authority_decision(self, decision) -> None:
        """Install/invalidate the T6 anchor per the authority decision.
        Every state transition is loud (expose-don't-correct)."""
        from hf_timestd.core.t6_anchor_authority import T6AuthorityState
        prev = decision.previous_state
        if decision.state is not prev:
            logger.warning(
                "T6 anchor authority: %s → %s%s",
                prev.value, decision.state.value,
                (f" (violations: {', '.join(decision.violations)})"
                 if decision.violations else ""),
            )
        self._t6_authority_last_decision = decision
        if decision.state is T6AuthorityState.AUTHORITATIVE:
            self._t6_native_anchor = decision.anchor
            if decision.state is not prev:
                self._t6_rate_reset("native anchor captured via T6")
        elif decision.state is T6AuthorityState.UNLOCKED and prev in (
                T6AuthorityState.AUTHORITATIVE, T6AuthorityState.DEGRADED):
            # Invalidate so the legacy cascade re-captures via T5 —
            # loud fallback, never a silently stale T6 anchor.
            self._t6_native_anchor = None
        # DEGRADED: hold the last good anchor (GPSDO coasting).
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/test_core_recorder_t6_fine_integration.py -v`
Expected: all PASS.

- [ ] **Step 5: Wire construction and the batch feed**

In the T6 init block (directly after the `BpskPpsCalibratorMF` construction, ~line 585-624), add:

```python
                    fine_cfg = self._t6_fine_settings(self._t6_config)
                    self._t6_fine_stage = None
                    self._t6_authority = None
                    self._t6_authority_last_decision = None
                    if fine_cfg['fine_stage_enabled']:
                        from hf_timestd.core.bpsk_edge_fine_stage import (
                            BpskEdgeFineStage,
                        )
                        from hf_timestd.core.t6_anchor_authority import (
                            T6AnchorAuthority,
                        )
                        self._t6_fine_stage = BpskEdgeFineStage(
                            sr, fold_seconds=fine_cfg['fine_fold_seconds'])
                        self._t6_authority = T6AnchorAuthority(
                            sr,
                            fine_cfg['delay_budget_ns'],
                            edge_period_tolerance_ns=fine_cfg[
                                'edge_period_tolerance_ns'],
                            fine_coarse_max_ms=fine_cfg['fine_coarse_max_ms'],
                            degraded_unlock_after_sec=fine_cfg[
                                'degraded_unlock_after_sec'],
                        )
                        logger.info(
                            "T6 anchor inversion armed: fold=%ds "
                            "delay_budget=%d ns (spec: docs/design/"
                            "T6_ANCHOR_INVERSION_DESIGN.md)",
                            fine_cfg['fine_fold_seconds'],
                            fine_cfg['delay_budget_ns'],
                        )
```

`_t6_fine_settings` raises on a bad delay budget — let that propagate out of the T6 init `try` the same way other T6 config errors do (T6 disabled with the error logged; check how the surrounding block handles exceptions and match it).

In `_t6_on_samples`, directly after `result = self._t6_calibrator.process_samples(...)` (~line 3009) and before the diff-calibrator sidecar, add:

```python
        fine_stage = getattr(self, '_t6_fine_stage', None)
        if fine_stage is not None:
            try:
                coarse = self._t6_calibrator._chain_delay_samples
                if result is not None and result.locked and coarse is not None:
                    fine_stage.set_coarse_offset_samples(coarse)
                fine = fine_stage.process_samples(
                    samples, quality.last_rtp_timestamp)
                if fine is not None and self._t6_authority is not None:
                    named = self._t6_name_integer_second(fine.edge_rtp)
                    decision = self._t6_authority.on_fine_estimate(
                        fine, coarse, named)
                    self._t6_apply_authority_decision(decision)
            except Exception as e:
                if not getattr(self, '_t6_fine_warned', False):
                    logger.error(
                        f"T6 fine stage failed (will retry each batch, "
                        f"logged once): {e}", exc_info=True)
                    self._t6_fine_warned = True
```

And in the stuck-recovery branch (where `self._t6_calibrator.reset()` is called, ~line 3056), add MF-unlock propagation right after the reset:

```python
            if getattr(self, '_t6_authority', None) is not None:
                self._t6_apply_authority_decision(
                    self._t6_authority.on_mf_unlock())
            if getattr(self, '_t6_fine_stage', None) is not None:
                self._t6_fine_stage.reset()
```

- [ ] **Step 6: Retire the chain-delay store on the T6 path**

In the first-lock disambiguation branch (~line 3096-3110): delete the "preferred path" that loads `self._t6_mf_chain_delay_store` and computes the integer-sample shift from the persisted value, so the branch goes straight to the T5/T4-cascade fallback. Where the store object is constructed for the MF path, replace with:

```python
                # Chain-delay persistence retired on the T6 path (spec
                # §6): under the anchor inversion no ms-scale fitted
                # state exists to persist; re-lock re-derives from
                # scratch in ~fine_fold_seconds.  A leftover store file
                # is ignored.
                self._t6_mf_chain_delay_store = None
```

then, where the store *file* would have been read, log once at INFO if the file exists on disk (`bpsk_mf_chain_delay.json` under the same directory the store used — read `bpsk_chain_delay_store.py` for the exact path attribute) saying it is ignored. Keep the class and its tests untouched (the legacy non-MF calibrator path may still reference it; verify with `grep -n "chain_delay_store" src/hf_timestd/core/core_recorder_v2.py` and leave non-MF uses alone). All `self._t6_mf_chain_delay_store is not None` guards elsewhere (stuck recovery ~line 3065) now simply skip.

- [ ] **Step 7: Run the T6 test files**

Run: `uv run pytest tests/test_core_recorder_t6_fine_integration.py tests/test_core_recorder_t6_shared.py tests/test_core_recorder_t6_step_recovery.py tests/test_bpsk_chain_delay_store.py -v`
Expected: all PASS. Step-recovery tests that asserted store-unlink behaviour may need updating to the retirement (store is `None`); change assertions to match the new behaviour, not the code to match old assertions — the retirement is spec-approved.

- [ ] **Step 8: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_core_recorder_t6_fine_integration.py tests/test_core_recorder_t6_step_recovery.py
git commit -m "feat(t6): wire anchor inversion into core recorder; retire chain-delay store on T6 path"
```

---

### Task 6: Status/diagnostics surface

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (status-JSON assembly around line ~4229, where `rtp_to_utc_offset_ns` is emitted)
- Test: `tests/test_core_recorder_t6_fine_integration.py` (append)

**Interfaces:**
- Consumes: `self._t6_authority`, `self._t6_authority_last_decision` (Task 5).
- Produces: a method `_t6_authority_status(self) -> Optional[dict]` returning exactly `{'state': str, 'violations': list[str], 'delay_budget_ns': int, 'anchor_tier': Optional[str], 'blocks_discarded': int, 't6_vs_radiod_pair_ms': Optional[float]}`, merged into the status JSON under key `t6_authority`.

`t6_vs_radiod_pair_ms` is invariant 5 (cross-tier diagnostic, report-only): `_compute_rtp_to_utc_offset_ns()/1e6` — the standing difference between the T6 anchor and radiod's gps_time/rtp_timesnap projection. It is *reported*, never used to correct the anchor.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core_recorder_t6_fine_integration.py`:

```python
class TestAuthorityStatus:
    def test_status_none_when_authority_absent(self):
        r = CoreRecorderV2.__new__(CoreRecorderV2)
        r._t6_authority = None
        assert r._t6_authority_status() is None

    def test_status_reflects_authoritative_state(self):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        r._t6_fine_stage = SimpleNamespace(blocks_discarded=2)
        r._compute_rtp_to_utc_offset_ns = lambda: -80_000_000
        d = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d)
        s = r._t6_authority_status()
        assert s['state'] == "AUTHORITATIVE"
        assert s['violations'] == []
        assert s['delay_budget_ns'] == 10_000
        assert s['anchor_tier'] == "T6"
        assert s['blocks_discarded'] == 2
        assert s['t6_vs_radiod_pair_ms'] == pytest.approx(-80.0)

    def test_status_before_first_estimate(self):
        r = bare_recorder()
        r._t6_fine_stage = SimpleNamespace(blocks_discarded=0)
        r._compute_rtp_to_utc_offset_ns = lambda: None
        s = r._t6_authority_status()
        assert s['state'] == "ACQUIRING"
        assert s['anchor_tier'] is None
        assert s['t6_vs_radiod_pair_ms'] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core_recorder_t6_fine_integration.py::TestAuthorityStatus -v`
Expected: FAIL with `AttributeError: '_t6_authority_status'`

- [ ] **Step 3: Implement**

```python
    def _t6_authority_status(self) -> Optional[dict]:
        """T6 authority block for the status JSON (spec §6 invariant 5:
        cross-tier disagreement is REPORTED here, never corrects the
        anchor)."""
        auth = getattr(self, '_t6_authority', None)
        if auth is None:
            return None
        decision = getattr(self, '_t6_authority_last_decision', None)
        anchor = getattr(self, '_t6_native_anchor', None)
        offset_ns = None
        try:
            offset_ns = self._compute_rtp_to_utc_offset_ns()
        except Exception:
            pass
        fine = getattr(self, '_t6_fine_stage', None)
        return {
            'state': auth.state.value,
            'violations': (list(decision.violations)
                           if decision is not None else []),
            'delay_budget_ns': auth.delay_budget_ns,
            'anchor_tier': (anchor.captured_via_tier
                            if anchor is not None else None),
            'blocks_discarded': (fine.blocks_discarded
                                 if fine is not None else 0),
            't6_vs_radiod_pair_ms': (offset_ns / 1e6
                                     if offset_ns is not None else None),
        }
```

Then in the status-JSON assembly (~line 4229, the dict already containing `'rtp_to_utc_offset_ns'`): add sibling key

```python
                    't6_authority': self._t6_authority_status(),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core_recorder_t6_fine_integration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_core_recorder_t6_fine_integration.py
git commit -m "feat(t6): authority state + cross-tier diagnostic in status JSON"
```

---

### Task 7: Offline replay harness + acceptance run

**Files:**
- Modify: `tools/t6_estimator_sweep.py` (add a `fine` mode; read the existing tool's arg structure first and extend it rather than restructuring)
- Test: `tests/test_t6_fine_replay_tool.py` (unit-level: the replay function on synthetic data)
- Acceptance data (NOT in the repo): `/root/appliance/t6-offline/t6-rawiq.bin` — **s16 little-endian interleaved I/Q @ 96 kHz** (format verified 2026-08-07 against the capture; confirm against `/root/appliance/t6-offline/rawiq.py` before relying on it).

**Interfaces:**
- Consumes: `BpskEdgeFineStage`, `make_bpsk` (test helper from Task 2).
- Produces: `replay_fine(iq: np.ndarray, sample_rate: int, coarse_offset: float, fold_seconds: int, batch: int = 1740) -> list[FineEdgeEstimate]` in `tools/t6_estimator_sweep.py`, plus `early_late_offset(avg_I: np.ndarray, coarse: int, gate_ms: float, sample_rate: int) -> float` (the independent cross-check discriminant — harness-only, never shipped in the service path).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_t6_fine_replay_tool.py
"""Replay-harness plumbing for the fine stage (spec §8).

The acceptance RUN uses real captures outside the repo; these tests
cover the harness functions themselves on synthetic data.
"""
import numpy as np
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from t6_estimator_sweep import replay_fine, early_late_offset
from tests.test_bpsk_edge_fine_stage import make_bpsk

SR = 96_000
EDGE = 43_181.4


class TestReplayFine:
    def test_split_windows_agree_to_1us(self):
        iq = make_bpsk(SR, 16, EDGE, noise_rms=0.05)
        ests = replay_fine(iq, SR, coarse_offset=EDGE + 40, fold_seconds=8)
        assert len(ests) == 2
        spread_us = abs(ests[0].edge_offset_samples
                        - ests[1].edge_offset_samples) / SR * 1e6
        assert spread_us < 1.0

    def test_early_late_agrees_with_zero_crossing(self):
        iq = make_bpsk(SR, 8, EDGE, noise_rms=0.02)
        ests = replay_fine(iq, SR, coarse_offset=EDGE, fold_seconds=8)
        # rebuild the folded average the same way to feed the EL gate
        sec = (np.arange(len(iq)) // SR)
        sign = 1 - 2 * (sec & 1)
        avg = ((iq * sign).reshape(8, SR)).mean(axis=0)
        phi = 0.5 * np.angle(np.mean(avg.astype(np.complex128) ** 2))
        I = np.real(avg * np.exp(-1j * phi))
        el = early_late_offset(I, int(EDGE), gate_ms=2.0, sample_rate=SR)
        diff_us = abs(el - ests[0].edge_offset_samples) / SR * 1e6
        assert diff_us < 5.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_t6_fine_replay_tool.py -v`
Expected: FAIL with `ImportError: cannot import name 'replay_fine'`

- [ ] **Step 3: Implement in `tools/t6_estimator_sweep.py`**

Add (adapting imports to the tool's existing style — it already imports from `hf_timestd`):

```python
def replay_fine(iq, sample_rate, coarse_offset, fold_seconds, batch=1740):
    """Run BpskEdgeFineStage over an in-memory IQ array in service-sized
    batches with clean synthetic RTP labels.  Returns all estimates."""
    from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
    stage = BpskEdgeFineStage(int(sample_rate), fold_seconds=int(fold_seconds))
    stage.set_coarse_offset_samples(float(coarse_offset))
    out = []
    i = 0
    while i < len(iq):
        r = stage.process_samples(iq[i:i + batch], i & 0xFFFFFFFF)
        if r is not None:
            out.append(r)
        i += batch
    return out


def early_late_offset(avg_I, coarse, gate_ms, sample_rate):
    """Independent early-minus-late discriminant (cross-check ONLY —
    never shipped in the service path; spec §8).  Slides a two-gate
    window around ``coarse`` and returns the offset where the early
    and late gate means balance, by linear interpolation of the
    discriminant's zero crossing."""
    import numpy as np
    g = max(2, int(gate_ms * 1e-3 * sample_rate))
    p = len(avg_I)
    span = np.arange(-2 * g, 2 * g + 1)
    d = np.empty(len(span))
    for j, s in enumerate(span):
        c = (coarse + s) % p
        late = np.take(avg_I, np.arange(c, c + g) % p).mean()
        early = np.take(avg_I, np.arange(c - g, c) % p).mean()
        d[j] = late - early
    # |d| is maximal when the gates straddle the edge; the *derivative*
    # of d crosses zero there.  Use the extremum of d, refined by
    # parabolic interpolation.
    k = int(np.argmax(np.abs(d)))
    if 0 < k < len(d) - 1:
        y0, y1, y2 = np.abs(d[k - 1]), np.abs(d[k]), np.abs(d[k + 1])
        denom = (y0 - 2 * y1 + y2)
        frac = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
    else:
        frac = 0.0
    return float((coarse + span[k] + frac) % p)


def _load_s16le_iq(path):
    """Load s16 little-endian interleaved I/Q as complex64."""
    import numpy as np
    raw = np.fromfile(path, dtype='<i2')
    return (raw[0::2].astype(np.float32)
            + 1j * raw[1::2].astype(np.float32)).astype(np.complex64)
```

Wire a `fine` sub-mode into the tool's existing CLI (mirror how its current modes are dispatched): `--mode fine --input <path> --sample-rate 96000 --coarse <samples> --fold-seconds 8` → loads via `_load_s16le_iq`, runs `replay_fine`, prints per-window `edge_offset_samples` (in samples and µs), the spread, `fit_rms`, `plateau_amplitude`, and the `early_late_offset` cross-check per window.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_t6_fine_replay_tool.py -v`
Expected: all PASS.

- [ ] **Step 5: Acceptance run on the real capture**

```bash
# Confirm the raw format assumption first:
sed -n '1,40p' /root/appliance/t6-offline/rawiq.py
# Then (24.72 s capture → fold 8 s → 3 windows):
uv run python tools/t6_estimator_sweep.py --mode fine \
  --input /root/appliance/t6-offline/t6-rawiq.bin \
  --sample-rate 96000 --coarse 43182 --fold-seconds 8
```

The `--coarse` value: the MF's measured apex on this capture is ~449.8178 ms → `0.4498178 * 96000 ≈ 43183` samples; if the tool errors with no crossing in window, widen with `--search-window-ms` (add the flag pass-through) or re-derive coarse from `/root/appliance/t6-offline/mf_probe.py` output.

**Acceptance gate (spec §8):** spread across the 3 windows ≤ 1 µs; early-late agreement ≤ 5 µs per window. Record the numbers in the commit message. If the gate FAILS, stop — do not tune constants to pass; report the numbers and the folded-average plot data to the user (this is exactly the "does the coherent fold survive real captures" question the design flagged).

- [ ] **Step 6: Commit**

```bash
git add tools/t6_estimator_sweep.py tests/test_t6_fine_replay_tool.py
git commit -m "feat(t6): fine-stage replay mode + early-late cross-check in estimator sweep

Acceptance on t6-rawiq.bin (3×8 s windows): spread <spread> µs, EL agreement <el> µs"
```

---

### Task 8: Docs, full suite, push

**Files:**
- Modify: `docs/ARCHITECTURE-FIRST-PRINCIPLES.md` (chain_delay definition section → delay budget + inversion), `CLAUDE.md` (timing-authority invariant note: add one sentence that T6, when AUTHORITATIVE, is the anchor source and the coarse cascade only names the second)
- No new tests.

- [ ] **Step 1: Update `docs/ARCHITECTURE-FIRST-PRINCIPLES.md`**

Read the section that defines `chain_delay` (grep for `chain_delay`). Amend it: the analog-path definition stands; under the anchor inversion (link `docs/design/T6_ANCHOR_INVERSION_DESIGN.md`) the correction applied to the anchor is the **delay budget** (±1 ms hard bound), `chain_delay` measurements are diagnostic-only, and the anchor is `named_second + delay_budget` at the fine-stage edge RTP. Keep the edit surgical — amend the existing prose, don't rewrite the doc.

- [ ] **Step 2: Update `CLAUDE.md`**

In the "Timing-authority invariant" bullet, after the sentence about the chrony feed, insert one sentence:

> When the T6 anchor authority is AUTHORITATIVE (see `docs/design/T6_ANCHOR_INVERSION_DESIGN.md`), the TS-1 fine-stage edge defines the RTP→UTC anchor and the coarse cascade only names the integer second; `chain_delay` values are diagnostics, never corrections.

- [ ] **Step 3: Full suite**

Run: `uv run pytest tests/`
Expected: PASS (pre-existing known failure `test_iq_20khz_f32` needs a live radiod — unchanged failures of that kind are acceptable; NEW failures are not).

- [ ] **Step 4: flake8/black on the new files**

Run: `uv run black --check src/hf_timestd/core/bpsk_edge_fine_stage.py src/hf_timestd/core/t6_anchor_authority.py && uv run flake8 src/hf_timestd/core/bpsk_edge_fine_stage.py src/hf_timestd/core/t6_anchor_authority.py`
Fix any findings; re-run the touched test files.

- [ ] **Step 5: Commit and push**

```bash
git add docs/ARCHITECTURE-FIRST-PRINCIPLES.md CLAUDE.md
git commit -m "docs(t6): anchor inversion — delay budget replaces chain_delay as correction"
git push origin main
```

---

## Post-plan (NOT part of this plan — listed so nobody "helpfully" starts them)

- Live B4 validation (chrony HPPS → µs-class, 24 h soak, delay-budget characterisation) — blocked on the v3.25 bringup.
- Judge tier-ladder arbitration; fleet anchor export; TS-1 auto-enable; radiod batch-labelling fix; `[timing.l6_pps]` template cleanup.
