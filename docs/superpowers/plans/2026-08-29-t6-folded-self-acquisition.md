# T6 Folded Self-Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `BpskEdgeFineStage` acquire the BPSK PPS edge without a matched-filter seed, so T6 keeps working at the low C/N0 where the MF calibrator locks itself out.

**Architecture:** The fine stage already folds 30 s of complex baseband coherently but returns `None` unless something hands it a ±6 ms search window. We add a third way to obtain that window — a closed-form matched filter over the folded second — plus a tracking mode that reuses the stage's own confirmed estimate. The MF calibrator keeps seeding when it locks and keeps serving as the `fine_coarse` cross-check, but stops being able to veto the tier.

**Tech Stack:** Python 3.11, numpy, pytest/unittest. No new dependencies.

**Spec:** `docs/design/T6_FOLDED_SELF_ACQUISITION.md` (read §3 before starting; §4 is explicitly a later, separate commit)

## Global Constraints

- **Fold length K stays at the shipped default of 30 s.** Do not change `fold_seconds`.
- **Do not roll or otherwise circularly shift the folded array.** The 2A wrap discontinuity sits between index `p-1` and `0`; rolling moves it into the interior and manufactures a false candidate. Measured, see spec §3.2.
- **Use `T(e) = C[p-1] - 2*C[e-1]`, not a plain CUSUM.** CUSUM is structurally biased 1–40 ms when the edge lands near the fold origin, identically at every C/N0, and does not self-correct.
- **Absence of an estimate must stay visible as absence.** Never make T6 hold an anchor it cannot justify. The authority's `on_tick` liveness invariant is what reports silence; do not defeat it.
- **`T6AnchorAuthority` keeps its signature.** It already takes `Optional[float]` for the coarse and already skips `fine_coarse` when it is `None`.
- Test interpreter: `.venv/bin/python -m pytest` from the repo root.
- Commit to `main` directly. No feature branches (project convention).
- Sample rate in all tests is 96000; the T6 channel's real rate.

---

### Task 1: Bootstrap edge locator

A pure function, in its own module: `bpsk_edge_fine_stage.py` is already ~500 lines and this has one clear responsibility and a sharp test surface.

**Files:**
- Create: `src/hf_timestd/core/bpsk_fold_bootstrap.py`
- Test: `tests/test_bpsk_fold_bootstrap.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `bootstrap_edge_index(in_phase: np.ndarray) -> int` — index in `[0, len(in_phase))` of the polarity transition in a derotated folded second.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the folded-second bootstrap edge locator."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.bpsk_fold_bootstrap import bootstrap_edge_index

SR = 96000


def _folded_second(edge: int, amplitude: float = 1.0,
                   noise_std: float = 0.0, seed: int = 5,
                   invert: bool = False) -> np.ndarray:
    """A derotated folded second: -A on [0, edge), +A on [edge, p).

    That is the real shape — measured on BpskEdgeFineStage's own fold,
    spec §3.2. The wrap from +A back to -A lies between index p-1 and 0
    and is deliberately NOT represented as an interior feature.
    """
    x = np.full(SR, amplitude, dtype=np.float64)
    x[:edge] = -amplitude
    if invert:
        x = -x
    if noise_std > 0:
        x = x + np.random.default_rng(seed).normal(0, noise_std, size=SR)
    return x


class TestBootstrapEdgeIndex(unittest.TestCase):

    def test_locates_a_clean_mid_second_edge(self):
        self.assertEqual(bootstrap_edge_index(_folded_second(47916)), 47916)

    def test_locates_an_edge_near_the_fold_origin(self):
        """The case a plain CUSUM gets structurally wrong (spec §3.1)."""
        self.assertEqual(bootstrap_edge_index(_folded_second(300)), 300)

    def test_locates_an_edge_near_the_fold_end(self):
        self.assertEqual(bootstrap_edge_index(_folded_second(95700)), 95700)

    def test_polarity_invariant(self):
        """Global sign is set by an arbitrary sign-alternation phase, so
        the statistic must not assume which way the transition runs."""
        self.assertEqual(
            bootstrap_edge_index(_folded_second(47916, invert=True)), 47916,
        )

    def test_within_one_sample_under_realistic_fold_noise(self):
        """noise_std 0.15 is the per-bin residual of a 30 s fold at
        B4's worst measured night C/N0 (48.5 dB-Hz)."""
        for edge in (300, 47916, 95700):
            found = bootstrap_edge_index(
                _folded_second(edge, noise_std=0.15, seed=edge)
            )
            self.assertLessEqual(abs(found - edge), 1, f"edge={edge}")

    def test_rejects_input_too_short_to_have_an_interior_edge(self):
        with self.assertRaises(ValueError):
            bootstrap_edge_index(np.array([1.0]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bpsk_fold_bootstrap.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'hf_timestd.core.bpsk_fold_bootstrap'`

- [ ] **Step 3: Write the implementation**

```python
"""Bootstrap edge locator for a folded BPSK PPS second.

The fine stage's coherent fold produces a second that is ``-A`` on
``[0, e)`` and ``+A`` on ``[e, p)`` — one interior transition, at the
edge (measured; spec §3.2).  The 2A wrap discontinuity lies between
index ``p-1`` and ``0``, outside the linear array, and is never an
interior feature.

With one free parameter the matched filter has a closed form::

    T(e) = sum_{j>=e} x[j] - sum_{j<e} x[j] = C[p-1] - 2*C[e-1]

where ``C = cumsum(x)``.  ``argmax |T|`` is the estimate; the magnitude
because the global polarity is set by an arbitrary sign-alternation
phase.  O(p), no window parameter, nothing to tune.

⛔ Do NOT substitute a plain CUSUM (``argmax |cumsum(x - mean)|``).  It
is exact for a mid-second edge but structurally biased by 1-40 ms when
the edge lands near the fold origin — its tent collapses when the two
segments are unbalanced.  The bias is identical at every C/N0 and does
not self-correct: ``registration`` advances by exactly
``fold_seconds * p`` per block, so a stream that registers into that
zone stays there for the life of the lock.

⛔ Do NOT roll the array to "centre" the edge.  Rolling moves the wrap
discontinuity into the interior and creates a second, equally strong
candidate.
"""
from __future__ import annotations

import numpy as np

__all__ = ['bootstrap_edge_index']


def bootstrap_edge_index(in_phase: np.ndarray) -> int:
    """Index of the polarity transition in a derotated folded second.

    Args:
        in_phase: real folded second, length >= 2.

    Returns:
        Index in ``[0, len(in_phase))``.

    Raises:
        ValueError: if the input is too short to contain an interior edge.
    """
    x = np.asarray(in_phase, dtype=np.float64)
    if x.ndim != 1 or x.size < 2:
        raise ValueError(
            f"in_phase must be a 1-D array of at least 2 samples, "
            f"got shape {x.shape}"
        )
    cumulative = np.cumsum(x)
    # T[e] for e in [0, p): total minus twice the sum strictly below e.
    t = cumulative[-1] - 2.0 * np.concatenate(([0.0], cumulative[:-1]))
    return int(np.argmax(np.abs(t)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bpsk_fold_bootstrap.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/bpsk_fold_bootstrap.py tests/test_bpsk_fold_bootstrap.py
git commit -m "T6: closed-form matched filter for the folded second

The fine stage folds 30 s coherently but cannot use it without a seed.
This is the statistic that produces one: T(e) = C[p-1] - 2*C[e-1],
argmax|T| -- the matched filter for the single free parameter of a
folded second that is -A on [0,e) and +A on [e,p).

Measured exact to <= 2 samples (21 us) across C/N0 34-48.5 dB-Hz at
edge positions 300 / 47916 / 95700."
```

---

### Task 2: Bootstrap mode in the fine stage

The headline behaviour: a stage that is never given a coarse currently returns `None` forever.

**Files:**
- Modify: `src/hf_timestd/core/bpsk_edge_fine_stage.py` (`_compute_estimate`, ~line 239; add `_search_centre`)
- Test: `tests/test_bpsk_edge_fine_stage_bootstrap.py`

**Interfaces:**
- Consumes: `bootstrap_edge_index` from Task 1.
- Produces: `BpskEdgeFineStage._search_centre(in_phase, registration) -> Optional[float]` (fold domain) and the attribute `_last_search_mode: str` — one of `'seeded'`, `'tracking'`, `'bootstrap'`. Task 3 extends both.

- [ ] **Step 1: Write the failing tests**

```python
"""Fine stage acquiring its own edge with no matched-filter seed."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage

SR = 96000
BATCH = 1920
EDGE = 47916.1672


def _noise_std_for(cn0_db_hz: float) -> float:
    """Per-component complex-noise sigma giving this C/N0 at SR."""
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _drive(stage, cn0_db_hz=55.0, duration_s=31.0, edge=EDGE, seed=11):
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=edge,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
    )
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is not None:
            last = est
    return last


class TestBootstrapAcquisition(unittest.TestCase):

    def test_produces_an_estimate_with_no_coarse_ever_set(self):
        """Today this returns None forever -- the coarse seed is a veto
        held by the stage that fails first."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        est = _drive(stage)
        self.assertIsNotNone(est)

    def test_bootstrapped_estimate_lands_on_the_edge(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        est = _drive(stage)
        err_samples = (est.edge_offset_samples - EDGE + SR / 2) % SR - SR / 2
        self.assertLess(abs(err_samples) / SR * 1e6, 100.0)

    def test_mode_is_reported_as_bootstrap(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage)
        self.assertEqual(stage._last_search_mode, "bootstrap")

    def test_a_coarse_seed_still_takes_precedence(self):
        """Regression: seeded behaviour is unchanged, and is preferred
        because a ±6 ms window is more selective than a whole second."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage.set_coarse_offset_samples(EDGE)
        est = _drive(stage)
        self.assertIsNotNone(est)
        self.assertEqual(stage._last_search_mode, "seeded")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -q`
Expected: `test_produces_an_estimate_with_no_coarse_ever_set` fails with `AssertionError: unexpectedly None`; the mode tests fail with `AttributeError: 'BpskEdgeFineStage' object has no attribute '_last_search_mode'`

- [ ] **Step 3: Write the implementation**

In `bpsk_edge_fine_stage.py`, add the import at the top with the others:

```python
from hf_timestd.core.bpsk_fold_bootstrap import bootstrap_edge_index
```

In `__init__`, beside `self._coarse_offset_rtp`:

```python
        # Which search mode produced the last estimate: 'seeded' (MF
        # coarse), 'tracking' (our own confirmed offset) or 'bootstrap'
        # (full-second matched filter).  Surfaced for tests and telemetry.
        self._last_search_mode: str = "none"
```

Replace the opening of `_compute_estimate` — the derotation must now happen **before** the search, because bootstrap needs `in_phase`:

```python
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
        c = int(round(centre)) % p
```

(The two derotation lines that used to sit after the coarse check are now
gone from their old position — do not leave a duplicate.)

Add the new method just above `_compute_estimate`:

```python
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
        self._last_search_mode = "bootstrap"
        return float(bootstrap_edge_index(in_phase))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -q`
Expected: 4 passed

- [ ] **Step 5: Run the existing fine-stage suite for regressions**

Run: `.venv/bin/python -m pytest tests/ -k "fine_stage or fine_edge" -q`
Expected: all pass. If a test asserted `None` for the no-coarse case, that assertion encoded the veto this task removes — update it to assert the new behaviour and say so in the commit.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py tests/test_bpsk_edge_fine_stage_bootstrap.py
git commit -m "T6: fine stage acquires its own edge without an MF seed

_compute_estimate returned None whenever no coarse offset was set, so
the matched-filter calibrator held a veto over the whole tier -- and it
is the stage that fails first, locking itself out below ~58 dB-Hz while
the fold behind it is good to ~34.

Adds a search-mode ladder: a fresh coarse still wins (a +/-6 ms window
is more selective than a whole second), bootstrap covers its absence."
```

---

### Task 3: Tracking mode, confirmation and demotion

Self-seeding can cement a wrong crossing — the failure the MF already guards with `STEP_CONFIRM_EDGES = 60`. The fold gets the same discipline in its own terms.

**Files:**
- Modify: `src/hf_timestd/core/bpsk_edge_fine_stage.py`
- Test: `tests/test_bpsk_edge_fine_stage_bootstrap.py` (append)

**Interfaces:**
- Consumes: `_search_centre`, `_last_search_mode` from Task 2.
- Produces: module constants `BOOTSTRAP_CONFIRM_BLOCKS = 3`, `BOOTSTRAP_CONFIRM_TOLERANCE_MS = 1.0`, `DEMOTE_AFTER_FAILED_BLOCKS = 3`; attribute `_own_offset_rtp: Optional[float]`.

- [ ] **Step 1: Write the failing tests**

```python
class TestTrackingAndConfirmation(unittest.TestCase):

    def test_does_not_self_seed_before_confirmation(self):
        """One bootstrap block is not enough. Self-seeding a wrong
        crossing is how a displaced reference gets cemented -- the same
        failure STEP_CONFIRM_EDGES guards in the MF."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=31.0)          # exactly one fold block
        self.assertIsNone(stage._own_offset_rtp)

    def test_self_seeds_after_confirming_blocks_agree(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=91.0)          # three fold blocks
        self.assertIsNotNone(stage._own_offset_rtp)

    def test_tracks_from_its_own_offset_once_confirmed(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)         # four fold blocks
        self.assertEqual(stage._last_search_mode, "tracking")

    def test_disagreeing_bootstraps_are_not_promoted(self):
        """Feed blocks whose edges differ by far more than the tolerance;
        the stage must stay in bootstrap rather than adopt either."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        for k, edge in enumerate((10000.0, 40000.0, 70000.0)):
            sig = _make_bpsk_signal(
                duration_s=30.0, sample_rate=SR, edge_offset_samples=edge,
                noise_std=_noise_std_for(55.0), seed=11 + k,
            )
            for i in range(0, len(sig), BATCH):
                stage.process_samples(sig[i:i + BATCH], i + k * len(sig))
        self.assertIsNone(stage._own_offset_rtp)

    def test_a_second_with_no_edge_yields_no_estimate(self):
        """Spec §6: absence of an estimate must stay visible as absence.
        A constant second has no polarity transition to find."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        flat = np.ones(SR * 31, dtype=np.complex64)
        produced = []
        for i in range(0, len(flat), BATCH):
            est = stage.process_samples(flat[i:i + BATCH], i)
            if est is not None:
                produced.append(est)
        self.assertEqual(produced, [])
        self.assertGreater(stage._failed_blocks, 0)

    def test_demotes_to_bootstrap_after_repeated_failures(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        self.assertIsNotNone(stage._own_offset_rtp)
        for _ in range(3):
            stage._note_block_failed()
        self.assertIsNone(stage._own_offset_rtp)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -k "TrackingAndConfirmation" -q`
Expected: `AttributeError: 'BpskEdgeFineStage' object has no attribute '_own_offset_rtp'`

- [ ] **Step 3: Write the implementation**

Module constants, beside `REGISTRATION_SPREAD_LIMIT`:

```python
# Self-seeding can cement a wrong crossing — the displaced-reference
# failure the MF guards with STEP_CONFIRM_EDGES = 60.  The fold gets the
# same discipline: this many consecutive full-search estimates must agree
# before the stage will use its own offset as a search centre.
BOOTSTRAP_CONFIRM_BLOCKS = 3
BOOTSTRAP_CONFIRM_TOLERANCE_MS = 1.0
# No slower to abandon a position than to adopt one.
DEMOTE_AFTER_FAILED_BLOCKS = 3
```

In `__init__`, beside `_last_search_mode`:

```python
        self._own_offset_rtp: Optional[float] = None
        self._bootstrap_history: list[float] = []
        self._failed_blocks = 0
```

Extend `_search_centre` with the tracking rung, between seeded and bootstrap:

```python
        if self._own_offset_rtp is not None:
            self._last_search_mode = "tracking"
            return (self._own_offset_rtp - int(registration)) % self.sample_rate
```

Add the bookkeeping methods:

```python
    def _note_block_estimate(self, edge_phase_rtp: float) -> None:
        """Record a successful block and maintain the self-seed."""
        self._failed_blocks = 0
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
```

Wire them into `_compute_estimate`. Every `return None` inside it becomes a
failure; the successful return records the phase. At each existing
`return None` after `_search_centre` succeeds, first call
`self._note_block_failed()`. Immediately before the final
`return FineEdgeEstimate(...)`, add:

```python
        self._note_block_estimate(float(edge_rtp % p))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py tests/test_bpsk_edge_fine_stage_bootstrap.py
git commit -m "T6: confirm before self-seeding, demote after failures

Three agreeing full-search blocks before the fold will use its own
offset as a search centre, and three failed blocks to give it up. A
self-seeded stage that adopted a wrong crossing on one noisy block
would defend it thereafter -- the displaced-reference failure the MF
already guards with STEP_CONFIRM_EDGES."
```

---

### Task 4: Clear the stale coarse, remove the outer veto

`reset()` never cleared `_coarse_offset_rtp`, which is the only reason the recorder gate exists (Finding 3). Fix it at the source and the gate goes.

**Files:**
- Modify: `src/hf_timestd/core/bpsk_edge_fine_stage.py` (add `clear_coarse_offset`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py:4678-4700`
- Test: `tests/test_bpsk_edge_fine_stage_bootstrap.py` (append)

**Interfaces:**
- Produces: `BpskEdgeFineStage.clear_coarse_offset() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
class TestCoarseLifecycle(unittest.TestCase):

    def test_clear_coarse_offset_drops_the_seeded_window(self):
        """After an MF unlock the old window is stale; searching it is
        how a stale-window estimate reaches the authority (Finding 3)."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage.set_coarse_offset_samples(EDGE)
        self.assertIsNotNone(stage.coarse_offset_fold_domain(0))
        stage.clear_coarse_offset()
        self.assertIsNone(stage.coarse_offset_fold_domain(0))

    def test_clear_coarse_offset_keeps_our_own_offset(self):
        """Different fields. Losing the MF must not cost us our own
        confirmed position -- that is the whole point of the change."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        own = stage._own_offset_rtp
        self.assertIsNotNone(own)
        stage.clear_coarse_offset()
        self.assertEqual(stage._own_offset_rtp, own)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -k CoarseLifecycle -q`
Expected: `AttributeError: 'BpskEdgeFineStage' object has no attribute 'clear_coarse_offset'`

- [ ] **Step 3: Write the implementation**

In `bpsk_edge_fine_stage.py`, after `set_coarse_offset_samples`:

```python
    def clear_coarse_offset(self) -> None:
        """Forget the MF-supplied search window.

        Called when the MF is not locked.  Without this the stage keeps
        searching a window the MF no longer stands behind, which is
        exactly the stale-window estimate the recorder's outer gate was
        added to block (Finding 3).  Clears ONLY the MF's window — never
        our own confirmed offset, which is independent of it.
        """
        self._coarse_offset_rtp = None
```

In `core_recorder_v2.py`, replace lines 4678-4700 (the seed and the gated
authority call) with:

```python
                coarse = self._t6_calibrator._chain_delay_samples
                if result is not None and result.locked and coarse is not None:
                    fine_stage.set_coarse_offset_samples(coarse)
                else:
                    # The MF is not standing behind a position right now.
                    # Drop its window rather than search a stale one, and
                    # let the fold acquire on its own — the MF is a
                    # witness for fine_coarse, no longer a veto.
                    fine_stage.clear_coarse_offset()
                    coarse = None
                fine = fine_stage.process_samples(
                    samples, resolve_batch_rtp(quality))
                if fine is not None:
                    self._t6_last_fine_est = fine
                if fine is not None and self._t6_authority is not None:
                    named = self._t6_name_integer_second(fine.edge_rtp)
                    decision = self._t6_authority.on_fine_estimate(
                        fine, coarse, named)
                    self._t6_apply_authority_decision(decision)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bpsk_edge_fine_stage_bootstrap.py -q`
Expected: 11 passed

- [ ] **Step 5: Run the recorder's T6 suites for regressions**

Run: `.venv/bin/python -m pytest tests/ -k "t6" -q`
Expected: all pass. A test asserting the authority is *not* consulted without a coarse is asserting the veto this task removes; update it and say so in the commit.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py src/hf_timestd/core/core_recorder_v2.py tests/test_bpsk_edge_fine_stage_bootstrap.py
git commit -m "T6: fix the stale coarse at its source, drop the outer veto

reset() never cleared _coarse_offset_rtp, so an MF unlock left the fine
stage searching a window nothing stood behind. The recorder's
'coarse is not None' gate was the workaround, and it cost T6 the whole
tier whenever the MF went dark. clear_coarse_offset() fixes the actual
defect; the authority already tolerates coarse=None and skips
fine_coarse when it is absent."
```

---

### Task 5: Mark an unrun cross-check as unrun

**Files:**
- Modify: `src/hf_timestd/core/t6_anchor_authority.py:207-215` (`_check`)
- Test: `tests/test_t6_anchor_authority.py` (append)

**Interfaces:**
- Produces: key `fine_coarse_unverified: True` in `T6AnchorAuthority.last_check_metrics`.

- [ ] **Step 1: Write the failing test**

This module is pytest-style with an `auth` fixture and module-level
`est()`, `phase()` and `SECOND` helpers — use them, do not add new ones.

```python
class TestUnverifiedCrossCheck:
    """T6 may now publish on nights when the MF never locks, so
    fine_coarse simply does not run. A check that did not run must say
    so, rather than be inferred from a missing key."""

    def test_absent_coarse_is_recorded_as_unverified(self, auth):
        e = est()
        auth.on_fine_estimate(e, None, SECOND)
        assert auth.last_check_metrics.get("fine_coarse_unverified") is True
        assert "fine_coarse_ms" not in auth.last_check_metrics

    def test_present_coarse_is_not_marked_unverified(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        assert "fine_coarse_unverified" not in auth.last_check_metrics
        assert "fine_coarse_ms" in auth.last_check_metrics

    def test_becomes_authoritative_with_no_coarse_at_all(self, auth):
        """The integration this whole change exists to permit: T6 takes
        authority from folded estimates on a night the MF never locks."""
        e = est()
        d = auth.on_fine_estimate(e, None, SECOND)
        assert d.state.name == "AUTHORITATIVE"
        assert d.anchor is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_t6_anchor_authority.py -k UnverifiedCrossCheck -q`
Expected: FAIL — `None is not true` on the first assertion

- [ ] **Step 3: Write the implementation**

In `_check`, extend the existing `if coarse_offset_samples is not None:` block
with an else:

```python
        else:
            # T6 can now reach AUTHORITATIVE on folded estimates alone,
            # so this check may simply not run.  Record that positively:
            # an unrun check must never read as a passed one.
            metrics["fine_coarse_unverified"] = True
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_t6_anchor_authority.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/t6_anchor_authority.py tests/test_t6_anchor_authority.py
git commit -m "T6: record an unrun fine_coarse check as unrun

With the fold self-acquiring, T6 can publish on nights the MF never
locks, and fine_coarse simply does not run. Absence of the metric would
be indistinguishable from a check that passed."
```

---

### Task 6: Fold telemetry into the provenance record

Spec §5: measure how often drops cost us a block before engineering drop tolerance.

**Files:**
- Modify: `hamsci-dsp/src/hamsci_dsp/io/authority_snapshot_store.py` (`COLUMNS`, `_INT_COLUMNS`)
- Modify: `src/hf_timestd/core/authority_manager.py` (snapshot assembly)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (expose the counter)
- Test: `hamsci-dsp/tests/test_authority_snapshot_store.py`

**Interfaces:**
- Produces: snapshot columns `t6_fold_blocks_discarded` (INTEGER), `t6_fold_seconds` (INTEGER).

- [ ] **Step 1: Write the failing test**

In `hamsci-dsp/tests/test_authority_snapshot_store.py`, append to
`TestFrontendOperatingPoint` or add beside it:

```python
    def test_fold_telemetry_columns_round_trip(self):
        """One bad batch discards a whole 30 s fold block. Nothing read
        blocks_discarded, so how often drops cost T6 an estimate was
        unknown -- and unmeasurable after the fact."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot(
                    t6_fold_blocks_discarded=7, t6_fold_seconds=30,
                ))
            with sqlite3.connect(str(path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM authority_snapshot"
                ).fetchone()
            self.assertEqual(row["t6_fold_blocks_discarded"], 7)
            self.assertEqual(row["t6_fold_seconds"], 30)
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from the hamsci-dsp repo): `/home/mjh/hamsci/repos/hf-timestd/.venv/bin/python -m pytest tests/test_authority_snapshot_store.py -k fold_telemetry -q`
Expected: FAIL — `IndexError: No item with that key`

- [ ] **Step 3: Write the implementation**

In `authority_snapshot_store.py`, append to `COLUMNS` after the front-end block:

```python
    # --- T6 fold telemetry (spec T6_FOLDED_SELF_ACQUISITION §5) ---
    # One batch whose registration disagrees discards a whole fold block,
    # and three missed blocks trips estimate_stale.  Recorded so drop
    # tolerance is built on measurement rather than on assumption.
    "t6_fold_blocks_discarded",
    "t6_fold_seconds",
```

and add both names to `_INT_COLUMNS`.

⚠ The `AuthorityRunner` is built in `multi_broadcast_fusion.py` and runs
in **timestd-fusion**, a different process from the core recorder that owns
the fine stage. T6 detail crosses the boundary through
`/var/lib/timestd/status/core-recorder-status.json`
(`core_recorder_v2.py:678`). There are therefore three hops, each mirroring
what `pps_ok` already does — follow that field if anything is unclear.

**Hop 1** — `core_recorder_v2.py:5924`, in the T6 status block beside
`'pps_ok'`:

```python
                    # Spec §5: one batch with a disagreeing registration
                    # discards a whole fold block, and three missed blocks
                    # trip estimate_stale.  Published so drop tolerance is
                    # built on measurement, not assumption.
                    'fold_blocks_discarded': getattr(
                        getattr(self, '_t6_fine_stage', None),
                        'blocks_discarded', None,
                    ),
                    'fold_seconds': getattr(
                        getattr(self, '_t6_fine_stage', None),
                        'fold_seconds', None,
                    ),
```

**Hop 2** — `bpsk_pps_probe.py:292`, in the explicit `detail` whitelist
(it does not pass unknown keys through):

```python
            "fold_blocks_discarded": t6.get("fold_blocks_discarded"),
            "fold_seconds": t6.get("fold_seconds"),
```

**Hop 3** — `authority_manager.py`, in `_flatten_t6` beside the other
`d.get(...)` lines:

```python
    snapshot["t6_fold_blocks_discarded"] = d.get("fold_blocks_discarded")
    snapshot["t6_fold_seconds"] = d.get("fold_seconds")
```

- [ ] **Step 4: Add a flatten test in hf-timestd**

In `tests/test_authority_manager.py`, beside the other `_flatten_t6`
coverage (or with the snapshot-store tests if that is where flattening is
exercised):

```python
    def test_fold_telemetry_reaches_the_snapshot(self) -> None:
        from hf_timestd.core.authority_manager import _flatten_t6
        from hf_timestd.core.probe import ProbeResult   # match the local import style
        snapshot = {}
        _flatten_t6(snapshot, ProbeResult(
            t_level="T6", available=True, offset_ms=0.0, sigma_ms=0.001,
            detail={"fold_blocks_discarded": 7, "fold_seconds": 30},
        ))
        assert snapshot["t6_fold_blocks_discarded"] == 7
        assert snapshot["t6_fold_seconds"] == 30
```

Construct `ProbeResult` exactly as the neighbouring tests in this file do —
its required fields differ between versions.

- [ ] **Step 5: Run both suites to verify they pass**

Run: `/home/mjh/hamsci/repos/hf-timestd/.venv/bin/python -m pytest tests/ -q` in hamsci-dsp, then `.venv/bin/python -m pytest tests/test_authority_manager.py -q` in hf-timestd
Expected: all pass

- [ ] **Step 6: Commit both repos**

```bash
cd /home/mjh/hamsci/repos/hamsci-dsp
git add -A && git commit -m "io: record T6 fold telemetry in the authority snapshot"
cd /home/mjh/hamsci/repos/hf-timestd
git add -A && git commit -m "T6: report fold blocks discarded into the snapshot"
```

---

### Task 7: Pin the cliff as a regression test

The test that proves the exercise. Marked slow because it drives the real detector over many C/N0 points.

**Files:**
- Create: `tests/test_t6_acquisition_cliff.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test**

```python
"""The acquisition cliff, pinned.

The MF calibrator peak-picks against 0.5*_peak_running and locks itself
out once noise maxima approach half the real edge peak -- measured as a
stochastic cliff near 58-59 dB-Hz, below which the outcome depends on
the noise realisation. B4's T6 channel reached 48.5 dB-Hz on the
evening of 2026-08-28 and reported ACQUIRING all night.

This test pins the property the folding work exists to deliver: the
folded path still acquires where the per-second MF does not.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF

SR = 96000
BATCH = 1920
EDGE = 47916.1672
# B4's worst measured C/N0, evening of 2026-08-28 (rf_gain -4.2 dB).
B4_WORST_NIGHT_CN0_DB_HZ = 48.5


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _signal(cn0_db_hz: float, duration_s: float, seed: int) -> np.ndarray:
    return _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=EDGE,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
    )


@pytest.mark.slow
class TestAcquisitionCliff(unittest.TestCase):

    def test_folded_stage_acquires_at_b4_worst_night(self):
        for seed in (11, 23, 37):
            stage = BpskEdgeFineStage(sample_rate=SR)
            sig = _signal(B4_WORST_NIGHT_CN0_DB_HZ, 91.0, seed)
            last = None
            for i in range(0, len(sig), BATCH):
                est = stage.process_samples(sig[i:i + BATCH], i)
                if est is not None:
                    last = est
            self.assertIsNotNone(last, f"no estimate at seed {seed}")
            err = (last.edge_offset_samples - EDGE + SR / 2) % SR - SR / 2
            self.assertLess(abs(err) / SR * 1e6, 200.0, f"seed {seed}")

    def test_matched_filter_alone_does_not_acquire_there(self):
        """The premise. If this ever starts passing, the MF improved and
        this file's thresholds need re-deriving -- do not just delete it."""
        acquired = 0
        for seed in (11, 23, 37):
            cal = BpskPpsCalibratorMF(
                sample_rate=SR, consecutive_required=10,
                edge_tolerance_samples=30,
            )
            sig = _signal(B4_WORST_NIGHT_CN0_DB_HZ, 60.0, seed)
            edges = 0
            last_ok = -1
            for i in range(0, len(sig), BATCH):
                r = cal.process_samples(sig[i:i + BATCH], i)
                if r is not None and r.pps_ok != last_ok:
                    last_ok = r.pps_ok
                    edges += 1
            if edges > 3:
                acquired += 1
        self.assertLess(acquired, 3, "MF acquired at every seed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_t6_acquisition_cliff.py -q`
Expected before Tasks 1-4: the first test FAILS (`no estimate`). After them: both pass.

- [ ] **Step 3: Register the marker if pytest warns about it**

If pytest emits `PytestUnknownMarkWarning`, add to the existing pytest config:

```ini
markers =
    slow: drives the real detector across many C/N0 points
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: exit code 0, zero FAILED/ERROR lines. (This suite's config suppresses the final count line; the exit status is the signal.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_t6_acquisition_cliff.py
git commit -m "T6: pin the acquisition cliff as a regression test

Asserts the property the folding work exists to deliver -- the folded
path acquires at B4's worst measured night C/N0 (48.5 dB-Hz) where the
per-second matched filter does not."
```

---

## Deployment

Not part of the task list — schedule with the operator (mjh). Deploy is
`git ff` + restart of the `timestd-*` units, never `install.sh` (it
`ipcrm`s chrony's SHM). The restart re-anchors every recorder on the box.

Verification per spec §8: compare **00-06Z** before and after on
`t6_available` and `t6_sigma_ms` from `authority_snapshot`, with `rf_gain`,
`if_power`, `t6_baseband_power` and `t6_n0` confirming the receiver
conditions were comparable. A lower daytime sigma is not success; holding
lock through hours that currently report ACQUIRING is.
