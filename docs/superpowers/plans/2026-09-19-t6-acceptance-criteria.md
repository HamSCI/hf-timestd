# T6 Acceptance Criteria Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let T6 acquire on the strength of its own edge — a seven-criterion
self-consistency battery — instead of waiting for a lesser tier that no station
can supply.

**Architecture:** Acquisition splits into two questions with tolerances four
orders of magnitude apart. An *ordinal* resolver names the GPS second (needs
±250 ms; T2 at ~20 ms always clears it). The fold supplies the *phase* within
that second, alone. A new pure-function module `t6_battery.py` decides whether
the fold may assert; `BpskEdgeFineStage` grows the statistics that module
consumes; `core_recorder_v2` rewires disambiguation around both. The offset
judge gains a ceiling so a candidate reporting a worse σ stops buying itself a
wider licence.

**Tech Stack:** Python 3.11+, numpy, `unittest` (run under pytest), no new
dependencies.

**Spec:** `docs/design/T6_ACCEPTANCE_CRITERIA.md`

## Global Constraints

- **Primacy covers the origin only.** Nothing in this plan may let T6 vote on
  rate or frequency. `T6_EDGE_METHODS_COMPARED.md` §8 and
  `MEASUREMENT_MODEL.md` §7.1.1 stay in force.
- **A failed guardrail keeps asserting.** When the battery fails on a running
  T6, T6 publishes anyway, marked suspect, with the failing criterion named, and
  alarms. It does not demote. (Spec §5.1.)
- **A missing estimate stays missing.** Never route an absent estimate through
  the suspect path. The authority's existing liveness invariant handles absence.
  (Spec §5.2.)
- **Every persistent condition logs through the existing throttle** —
  `CoreRecorderV2._t6_say_once(key)`, period `T6_REPEAT_PERIOD_SEC = 300.0`.
  No new per-cycle log lines. (Spec §5.5.)
- **Thresholds are module constants with their derivation written beside them.**
  Never an operator knob, never a config key. (Spec §4.5.)
- **Thresholds must hold at 48.4 dB-Hz**, B4's measured worst hour — not merely
  at AI6VN's 77 dB-Hz injected pilot. (Spec §4.5.)
- **Sample rate is 96 kHz** on both T6 channels; one sample spans 10.4 µs.
- **Run tests with** `python -m pytest tests/<file> -v`. The repo's `addopts`
  already carries `-q`; pass `--override-ini addopts=` when you need the
  summary line.
- **Develop on `main`.** Commit after every task. Do not push without asking.
- **Do not deploy.** This plan ends at a green suite. Station verification
  (spec §8) is scheduled with the operator separately.

---

### Task 1: Fine-stage battery statistics

The battery needs four numbers the stage can produce but currently discards:
fold retention, split-half agreement, peak prominence, and transition width.
This task adds them to `FineEdgeEstimate` and computes them. No acceptance
logic yet — this task only makes the evidence visible.

**Files:**
- Modify: `src/hf_timestd/core/bpsk_edge_fine_stage.py`
- Test: `tests/test_bpsk_edge_fine_stage_battery_stats.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `FineEdgeEstimate` gains four float fields —
  `fold_retention: float`, `split_half_delta_samples: float`,
  `peak_prominence: float`, `transition_width_samples: float`.
  Task 2 consumes all four; Task 5 passes the whole estimate to the battery.

**Background the implementer needs.** `BpskEdgeFineStage` sums complex
baseband into `self._acc` (length = sample_rate) with per-second sign
alternation, dividing by `self._cnt` to get `avg`. That fold preserves
amplitude only while the carrier stays coherent with the ADC clock across
seconds. On DASI-009.AI6VN with the TS-1's REF IN on its internal 10 MHz, the
fold cancelled its own signal down to 0.006 of amplitude; with REF IN fed from
the GPSDO that governs the RX888, retention reached 0.9998. B4 reads 0.98.
Retention therefore separates a working reference chain from a missing coax.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bpsk_edge_fine_stage_battery_stats.py`:

```python
"""The fold's own evidence: retention, split-half, prominence, width."""
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


def _drive(stage, cn0_db_hz=70.0, duration_s=31.0, edge=EDGE, seed=11,
           carrier_freq_hz=0.0):
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=edge,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
        carrier_freq_hz=carrier_freq_hz,
    )
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is not None:
            last = est
    return last


class TestFoldRetention(unittest.TestCase):

    def test_coherent_chain_retains_nearly_all_amplitude(self):
        """AI6VN after rob's cabling fix measured 0.9998; B4 reads 0.98."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.fold_retention, 0.90)

    def test_incoherent_carrier_collapses_retention(self):
        """A carrier rotating between seconds makes the fold cancel its
        own signal -- AI6VN measured 0.006 with REF IN on the internal
        10 MHz.  1 Hz rotates a full turn per second, so successive
        seconds add in opposition."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR), carrier_freq_hz=1.0)
        if est is not None:
            self.assertLess(est.fold_retention, 0.30)


class TestSplitHalfAgreement(unittest.TestCase):

    def test_disjoint_halves_agree_on_a_clean_signal(self):
        """Two 30 s folds agreed to 41 ns on captured IQ (n=2)."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertLess(abs(est.split_half_delta_samples), 4.0)


class TestProminenceAndWidth(unittest.TestCase):

    def test_prominence_is_large_on_a_real_edge(self):
        """The magnitude-difference discriminant read 105x on captured IQ."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.peak_prominence, 5.0)

    def test_transition_width_matches_the_channel_filter(self):
        """+-25 kHz predicts 1/(2B) = 20 us = ~2 samples at 96 kHz.
        The fit band spans several samples either side, so allow room --
        the discriminating case is a lattice phantom, which has no
        transition at all and reads far wider."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.transition_width_samples, 0.0)
        self.assertLess(est.transition_width_samples, 60.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_bpsk_edge_fine_stage_battery_stats.py -v`
Expected: every test errors with `AttributeError: 'FineEdgeEstimate' object has no attribute 'fold_retention'` (and the other three names).

- [ ] **Step 3: Extend the dataclass**

In `src/hf_timestd/core/bpsk_edge_fine_stage.py`, add four fields to
`FineEdgeEstimate` (it sits just below `DEMOTE_AFTER_FAILED_BLOCKS`). Give
them defaults so any existing construction site keeps working:

```python
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
    # sub-fold, in samples.  Two disjoint 30 s folds agreed to 41 ns on
    # captured IQ against 17 ns predicted (n=2).
    split_half_delta_samples: float = 0.0
    # Peak of the search statistic over its median background.  The
    # magnitude-difference discriminant read 105x on captured IQ.
    peak_prominence: float = 0.0
    # Width of the fitted transition, in samples.  A +-25 kHz channel
    # filter predicts 1/(2B) = 20 us, about 2 samples at 96 kHz.
    transition_width_samples: float = 0.0
```

- [ ] **Step 4: Accumulate the two sub-folds and the amplitude reference**

In `reset()`, add three accumulators beside `self._acc`:

```python
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
```

Then in `process_samples`, immediately after the existing accumulation
`np.add.at(self._acc, idx, chunk.astype(np.complex128) * sign)`, add:

```python
            # Battery evidence.  `idx` is the fold-domain index of each
            # sample and `sign` its per-second alternation, both already
            # computed above; reuse them rather than recomputing.
            self._abs_sum += float(np.sum(np.abs(chunk)))
            self._abs_n += int(chunk.size)
            # Second parity within the fold, derived from the sample's
            # continuity position.  Even-parity seconds feed one
            # sub-fold, odd the other, so the two never share a sample.
            base = self._cont - chunk.size
            secs = (base + np.arange(chunk.size)) // self.sample_rate
            even = (secs % 2) == 0
            contrib = chunk.astype(np.complex128) * sign
            np.add.at(self._acc_even, idx[even], contrib[even])
            np.add.at(self._cnt_even, idx[even], 1)
            np.add.at(self._acc_odd, idx[~even], contrib[~even])
            np.add.at(self._cnt_odd, idx[~even], 1)
```

⚠ Read the surrounding code before pasting: `self._cont` may be advanced
before or after this point, and `idx`/`sign` may carry different local names.
Adapt the names, keep the arithmetic. If `self._cont` advances *after* the
accumulation, use `base = self._cont` instead.

- [ ] **Step 5: Compute the four statistics**

`_compute_estimate` already derives `in_phase`, the search segment `seg`, the
plateau amplitude `A`, and the fit span `lo..hi`. Add the statistics just
before the `return FineEdgeEstimate(...)`:

```python
        # --- battery evidence (T6_ACCEPTANCE_CRITERIA.md §4) ---
        mean_abs = (self._abs_sum / self._abs_n) if self._abs_n else 0.0
        retention = (float(np.mean(np.abs(avg))) / mean_abs) if mean_abs > 0 else 0.0
        prominence = (A / float(np.median(np.abs(in_phase)))
                      if float(np.median(np.abs(in_phase))) > 0 else 0.0)
        width = float(hi - lo)
        split_delta = self._split_half_delta(phi, edge_offset)
```

and add the helper method beside `_compute_estimate`:

```python
    def _split_half_delta(self, phi: float, full_edge: float) -> float:
        """Edge position from the even-second sub-fold minus the odd-second
        sub-fold, in samples, wrapped to (-p/2, p/2].

        Two disjoint folds of the same stable edge must agree to about
        sqrt(2) times the single-fold scatter.  A wandering apex -- the
        nightly B4 behaviour that produces ~270 tier transitions a day --
        separates them.  Returns 0.0 when either sub-fold is empty, so a
        short block reports no evidence rather than false evidence.
        """
        p = self.sample_rate
        out = []
        for acc, cnt in ((self._acc_even, self._cnt_even),
                         (self._acc_odd, self._cnt_odd)):
            if not np.any(cnt):
                return 0.0
            sub = np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0)
            ip = np.real(sub * np.exp(-1j * phi))
            # Closed-form matched filter for a single polarity flip at e:
            # T(e) = C[p-1] - 2*C[e-1].  argmax|T| locates the edge.
            # See T6_FOLDED_SELF_ACQUISITION.md §3.1 -- and note the
            # prohibition there against a plain CUSUM.
            c = np.cumsum(ip)
            t = c[-1] - 2.0 * np.concatenate(([0.0], c[:-1]))
            out.append(float(np.argmax(np.abs(t))))
        d = out[0] - out[1]
        return float((d + p / 2) % p - p / 2)
```

⛔ Use the closed-form `T(e)` above, **not** a plain CUSUM
(`argmax |cumsum(x - mean)|`). CUSUM was measured structurally biased by
1–40 ms when the edge lands near the fold origin, because its tent collapses
when the two segments are unbalanced. The bias is identical at every C/N0 and
never self-corrects.

Then pass the four values into the constructor call:

```python
        return FineEdgeEstimate(
            edge_offset_samples=float(edge_offset),
            edge_rtp=edge_rtp,
            edge_subsample=subsample,
            n_seconds_folded=self.fold_seconds,
            plateau_amplitude=A,
            fit_rms=fit_rms,
            fold_retention=retention,
            split_half_delta_samples=split_delta,
            peak_prominence=prominence,
            transition_width_samples=width,
        )
```

- [ ] **Step 6: Run the new test and the existing fine-stage tests**

Run:
```
python -m pytest tests/test_bpsk_edge_fine_stage_battery_stats.py \
                 tests/test_bpsk_edge_fine_stage.py \
                 tests/test_bpsk_edge_fine_stage_bootstrap.py \
                 tests/test_bpsk_fold_bootstrap.py -v
```
Expected: PASS throughout. The existing three files must not regress — the
new accumulators change no existing arithmetic.

- [ ] **Step 7: Commit**

```bash
git add src/hf_timestd/core/bpsk_edge_fine_stage.py \
        tests/test_bpsk_edge_fine_stage_battery_stats.py
git commit -m "t6: the fold reports its own evidence

Fold retention, split-half agreement, peak prominence and transition width
join FineEdgeEstimate.  Retention alone separates a working reference chain
from a missing coax: AI6VN read 0.006 on the TS-1's internal 10 MHz and
0.9998 once REF IN came from the GPSDO governing the RX888.

Evidence only -- nothing acts on these yet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The battery module

A pure-function module that turns the evidence into a verdict. No I/O, no
recorder state, no numpy beyond arithmetic — so it tests in milliseconds and
reads in one sitting.

**Files:**
- Create: `src/hf_timestd/core/t6_battery.py`
- Test: `tests/test_t6_battery.py` (create)

**Interfaces:**
- Consumes: `FineEdgeEstimate` field names from Task 1.
- Produces:
  - `@dataclass(frozen=True) BatteryVerdict` with
    `passed: bool`, `failures: tuple[str, ...]`, `sigma_ms: float`,
    `criteria: dict[str, float]`
  - `@dataclass BatteryThresholds` — every threshold, with defaults set in
    Task 3
  - `class T6Battery` with
    `__init__(self, sample_rate: int, fold_seconds: int, thresholds: BatteryThresholds | None = None)`,
    `evaluate(self, est, *, implied_chain_delay_ns: int, reported_sigma_ms: float, cn0_db_hz: float | None) -> BatteryVerdict`,
    and `reset(self) -> None`
  - Criterion names, used verbatim in logs and tests:
    `"retention"`, `"ruler"`, `"unimodality"`, `"prominence"`, `"sigma"`,
    `"split_half"`, `"plausibility"`

Tasks 5, 6 and 9 all call `evaluate`. Keep the signature exactly as written.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_battery.py`:

```python
"""Seven criteria, each with a named failure it catches."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_battery import T6Battery, BatteryThresholds

SR = 96000
K = 30


def _healthy(edge_rtp: int = 1_000_000 + 47916, **over) -> FineEdgeEstimate:
    kw = dict(
        edge_offset_samples=47916.0,
        edge_rtp=edge_rtp,
        edge_subsample=0.0,
        n_seconds_folded=K,
        plateau_amplitude=1.0,
        fit_rms=0.02,
        fold_retention=0.98,
        split_half_delta_samples=0.05,
        peak_prominence=40.0,
        transition_width_samples=6.0,
    )
    kw.update(over)
    return FineEdgeEstimate(**kw)


def _battery() -> T6Battery:
    return T6Battery(sample_rate=SR, fold_seconds=K)


def _run(bat, ests, *, chain_ns=16_618_000, sigma_ms=0.001, cn0=70.0):
    """Feed estimates in order; return the final verdict."""
    v = None
    for e in ests:
        v = bat.evaluate(e, implied_chain_delay_ns=chain_ns,
                         reported_sigma_ms=sigma_ms, cn0_db_hz=cn0)
    return v


def _series(n: int, *, step: int = SR * K, **over):
    """n consecutive healthy blocks one fold apart."""
    base = 1_000_000 + 47916
    return [_healthy(edge_rtp=base + i * step, **over) for i in range(n)]


class TestHealthyPasses(unittest.TestCase):

    def test_a_clean_run_passes_every_criterion(self):
        v = _run(_battery(), _series(6))
        self.assertTrue(v.passed, v.failures)
        self.assertEqual(v.failures, ())


class TestEachCriterionCatchesItsFailure(unittest.TestCase):
    """Remove any one of these and its row below stops failing.  That is
    the test of the test -- see the mutation discipline in the spec."""

    def test_retention_catches_an_incoherent_chain(self):
        v = _run(_battery(), _series(6, fold_retention=0.006))
        self.assertFalse(v.passed)
        self.assertIn("retention", v.failures)

    def test_ruler_catches_a_pulse_walking_against_the_adc(self):
        # Blocks a full 5000 samples short of one fold apart.
        v = _run(_battery(), _series(6, step=SR * K - 5000))
        self.assertFalse(v.passed)
        self.assertIn("ruler", v.failures)

    def test_unimodality_catches_a_position_that_will_not_settle(self):
        # Vary the FOLD POSITION, not edge_rtp -- unimodality reads
        # edge_offset_samples, and moving edge_rtp would trip the ruler
        # instead, which is a different criterion's job.  400 samples is
        # 4.17 ms against a 1.0 ms tolerance.
        bat = _battery()
        base = 1_000_000 + 47916
        ests = [_healthy(edge_rtp=base + i * SR * K,
                         edge_offset_samples=47916.0 + (0.0 if i % 2 else 400.0))
                for i in range(6)]
        v = _run(bat, ests)
        self.assertFalse(v.passed)
        self.assertIn("unimodality", v.failures)
        self.assertNotIn("ruler", v.failures)

    def test_prominence_catches_a_lattice_phantom(self):
        """B4 locked onto a 20.000 ms lattice on 2026-09-04 and held it.
        A phantom repeats perfectly, so only shape separates it."""
        v = _run(_battery(), _series(6, peak_prominence=1.4))
        self.assertFalse(v.passed)
        self.assertIn("prominence", v.failures)

    def test_prominence_also_catches_an_absent_transition(self):
        v = _run(_battery(), _series(6, transition_width_samples=900.0))
        self.assertFalse(v.passed)
        self.assertIn("prominence", v.failures)

    def test_sigma_catches_a_broken_tier_claiming_to_be_a_wide_one(self):
        """AI6VN reported 477 ms on 2026-09-18, which widened the judge's
        cross-bench bound to ~2.4 s and let 284 ms through."""
        v = _run(_battery(), _series(6), sigma_ms=477.0)
        self.assertFalse(v.passed)
        self.assertIn("sigma", v.failures)

    def test_split_half_catches_a_wandering_apex(self):
        v = _run(_battery(), _series(6, split_half_delta_samples=120.0))
        self.assertFalse(v.passed)
        self.assertIn("split_half", v.failures)

    def test_plausibility_catches_a_gross_wrap(self):
        v = _run(_battery(), _series(6), chain_ns=400_000_000)
        self.assertFalse(v.passed)
        self.assertIn("plausibility", v.failures)


class TestEvidenceDiscipline(unittest.TestCase):

    def test_a_single_block_cannot_pass_the_multi_block_criteria(self):
        """Ruler, unimodality and split-half need history.  One block
        must report 'not yet', never 'fine'."""
        v = _run(_battery(), _series(1))
        self.assertFalse(v.passed)

    def test_reset_discards_history(self):
        bat = _battery()
        _run(bat, _series(6))
        bat.reset()
        v = _run(bat, _series(1))
        self.assertFalse(v.passed)

    def test_the_verdict_reports_every_criterion_measured(self):
        v = _run(_battery(), _series(6))
        for name in ("retention", "ruler", "unimodality", "prominence",
                     "sigma", "split_half", "plausibility"):
            self.assertIn(name, v.criteria)

    def test_thresholds_are_injectable_for_tests_only(self):
        loose = BatteryThresholds(min_fold_retention=0.001)
        bat = T6Battery(sample_rate=SR, fold_seconds=K, thresholds=loose)
        v = _run(bat, _series(6, fold_retention=0.006))
        self.assertNotIn("retention", v.failures)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_t6_battery.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'hf_timestd.core.t6_battery'`.

- [ ] **Step 3: Write the module**

Create `src/hf_timestd/core/t6_battery.py`:

```python
"""T6's self-consistency battery — T6_ACCEPTANCE_CRITERIA.md §4.

T6 acquires on the strength of its own edge, not on a lesser tier's nod.
The gate this replaces demanded a non-T6 reference at sigma < 10 us; at
96 kHz that sits below one sample period, and no tier we run delivers it
(T5 ~1 ms, T4 0.29 ms, T3 sub-ms, T2 ~20 ms).  DASI-009.AI6VN, which has
none of them, sat at T2 while its edge landed on one sample position 128
seconds running.

⛔ What this module proves and does not prove.  All seven criteria ride
one antenna, one converter, one path, so the battery establishes
SELF-CONSISTENCY and nothing about accuracy.  Criteria 1 and 2 test
COHERENCE: both read healthy when a single oscillator drives everything
and drifts together.  Never cite either as evidence that a GPSDO holds
UTC.  See T6_ACCEPTANCE_CRITERIA.md §4.4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Criterion names.  Logs, telemetry and tests use these strings verbatim.
RETENTION = "retention"
RULER = "ruler"
UNIMODALITY = "unimodality"
PROMINENCE = "prominence"
SIGMA = "sigma"
SPLIT_HALF = "split_half"
PLAUSIBILITY = "plausibility"

ALL_CRITERIA = (RETENTION, RULER, UNIMODALITY, PROMINENCE, SIGMA,
                SPLIT_HALF, PLAUSIBILITY)

# How many consecutive blocks the multi-block criteria need before they
# will report at all.  Matches BOOTSTRAP_CONFIRM_BLOCKS in the fine
# stage, which already refuses to seed from its own offset until three
# full searches agree: the battery should be no quicker to trust a
# position than the stage that found it.
HISTORY_REQUIRED_BLOCKS = 3


@dataclass(frozen=True)
class BatteryThresholds:
    """Every acceptance threshold, with its derivation.

    Task 3 replaces these defaults with values derived from the C/N0
    sweep.  Until then they carry the 2026-09-18 measurements directly
    and are deliberately loose.
    """
    # Measured 0.9998 at AI6VN and 0.98 at B4 with a coherent chain;
    # 0.006 with the TS-1's REF IN on its internal 10 MHz.  The two
    # populations sit two orders apart, so the threshold's exact value
    # matters little -- only that it lands between them.
    min_fold_retention: float = 0.50
    # Consecutive block edges must differ by fold_seconds * sample_rate.
    # 127/127 per-second deltas came out exact on captured IQ; a block
    # tolerance of 2 samples (21 us) allows sub-sample fit noise without
    # admitting a pulse source walking against the ADC.
    max_ruler_error_samples: float = 2.0
    # Consecutive block positions about their median.  The fine stage's
    # own BOOTSTRAP_CONFIRM_TOLERANCE_MS is 1.0 ms; match it.
    max_unimodality_spread_ms: float = 1.0
    # Peak over median background.  The magnitude-difference
    # discriminant read 105x on captured IQ.  A lattice phantom repeats
    # perfectly and so passes every repeatability check -- only shape
    # separates it, which makes this criterion load-bearing.
    min_peak_prominence: float = 5.0
    # A +-25 kHz channel filter predicts a transition 1/(2B) = 20 us
    # wide, about 2 samples at 96 kHz.  The fit band spans several
    # samples either side; the discriminating case is a phantom, which
    # carries no transition at all and reads far wider.
    max_transition_width_samples: float = 60.0
    # Two disjoint sub-folds of a stable edge agree to about sqrt(2)
    # times the single-fold scatter -- 41 ns measured against 17 ns
    # predicted (n=2).  10 samples = 104 us leaves generous room while
    # still separating a wandering apex.
    max_split_half_delta_samples: float = 10.0
    # Implied chain delay.  Already enforced in the recorder as
    # T6_PHYSICAL_CHAIN_DELAY_MAX_NS; restated here so the battery owns
    # a complete verdict.
    max_chain_delay_ns: int = 250_000_000
    # Sigma ceiling (§4.3): the reported sigma may exceed the physical
    # prediction by at most this factor.  A T6 reporting 477 ms has not
    # produced a wide T6, it has produced a broken one.
    #
    # Deliberately generous, because the prediction curve is anchored on
    # ONE measured point (95 ns per second at 77 dB-Hz) and extrapolated
    # by 1/sqrt(SNR).  The criterion exists to catch a tier reporting
    # orders of magnitude outside its physics, not to police a factor of
    # two.  At 48.4 dB-Hz this still puts the ceiling near 0.047 ms,
    # four orders below the 477 ms it must refuse.
    sigma_margin: float = 100.0
    # Fallback ceiling when no C/N0 reading accompanies the estimate.
    # 1 ms is already ~10,000x the 95 ns per-second scatter measured at
    # 77 dB-Hz, so it refuses only the grossly broken.
    max_sigma_ms_without_cn0: float = 1.0


@dataclass(frozen=True)
class BatteryVerdict:
    passed: bool
    failures: tuple[str, ...]
    sigma_ms: float
    criteria: dict


class T6Battery:
    """Evaluates §4's seven criteria over a run of fold blocks."""

    def __init__(self, sample_rate: int, fold_seconds: int,
                 thresholds: Optional[BatteryThresholds] = None):
        if sample_rate < 8000:
            raise ValueError(f"sample_rate must be >= 8000 Hz, got {sample_rate}")
        if fold_seconds < 1:
            raise ValueError(f"fold_seconds must be >= 1, got {fold_seconds}")
        self.sample_rate = int(sample_rate)
        self.fold_seconds = int(fold_seconds)
        self.t = thresholds or BatteryThresholds()
        self.reset()

    def reset(self) -> None:
        """Forget the block history.  Called when the stream restarts or
        the stage demotes, so a new lock is judged on its own run."""
        self._edges: list[int] = []
        self._positions: list[float] = []

    # -- helpers ---------------------------------------------------

    def _wrapped(self, d: float) -> float:
        p = self.sample_rate
        return (d + p / 2) % p - p / 2

    def _predicted_sigma_ms(self, cn0_db_hz: Optional[float]) -> Optional[float]:
        """Per-block scatter the channel's C/N0 predicts, in ms.

        Anchored on the 2026-09-18 capture: 95 ns of per-second scatter
        at 77 dB-Hz on an injected pilot.  Scatter follows 1/sqrt(SNR),
        so it doubles for every 6 dB lost, and the fold improves it by
        sqrt(K).  At B4's measured 48.4 dB-Hz worst hour this predicts
        roughly 2.6 us per second and 0.5 us per 30 s block.
        """
        if cn0_db_hz is None:
            return None
        REF_CN0_DB_HZ = 77.0
        REF_SIGMA_NS = 95.0
        per_second_ns = REF_SIGMA_NS * 10 ** ((REF_CN0_DB_HZ - cn0_db_hz) / 20.0)
        per_block_ns = per_second_ns / math.sqrt(self.fold_seconds)
        return per_block_ns / 1e6

    # -- the battery -----------------------------------------------

    def evaluate(self, est, *, implied_chain_delay_ns: int,
                 reported_sigma_ms: float,
                 cn0_db_hz: Optional[float]) -> BatteryVerdict:
        p = self.sample_rate
        failures: list[str] = []
        criteria: dict = {}

        self._edges.append(int(est.edge_rtp))
        self._positions.append(float(est.edge_offset_samples))
        # Keep a bounded history; the criteria need a run, not an archive.
        keep = max(HISTORY_REQUIRED_BLOCKS * 2, 8)
        del self._edges[:-keep]
        del self._positions[:-keep]

        # 1 — fold retention.  Coherence of the reference chain.
        criteria[RETENTION] = float(est.fold_retention)
        if est.fold_retention < self.t.min_fold_retention:
            failures.append(RETENTION)

        # 4 — prominence and width.  Shape, which a phantom lacks.
        criteria[PROMINENCE] = float(est.peak_prominence)
        criteria["transition_width_samples"] = float(est.transition_width_samples)
        if (est.peak_prominence < self.t.min_peak_prominence
                or est.transition_width_samples <= 0.0
                or est.transition_width_samples
                > self.t.max_transition_width_samples):
            failures.append(PROMINENCE)

        # 6 — split-half agreement.  A wandering apex separates the halves.
        criteria[SPLIT_HALF] = float(est.split_half_delta_samples)
        if abs(est.split_half_delta_samples) > self.t.max_split_half_delta_samples:
            failures.append(SPLIT_HALF)

        # 7 — physical plausibility of the implied chain delay.
        criteria[PLAUSIBILITY] = float(implied_chain_delay_ns)
        if abs(int(implied_chain_delay_ns)) > self.t.max_chain_delay_ns:
            failures.append(PLAUSIBILITY)

        # 5 — sigma inside the tier's physical budget.
        criteria[SIGMA] = float(reported_sigma_ms)
        predicted = self._predicted_sigma_ms(cn0_db_hz)
        ceiling = (predicted * self.t.sigma_margin if predicted is not None
                   else self.t.max_sigma_ms_without_cn0)
        criteria["sigma_ceiling_ms"] = float(ceiling)
        if not (reported_sigma_ms == reported_sigma_ms):  # NaN
            failures.append(SIGMA)
        elif reported_sigma_ms > ceiling:
            failures.append(SIGMA)

        # 2 and 3 need a run of blocks.  Report 'not yet' rather than
        # 'fine' -- absence of evidence is not evidence.
        have_history = len(self._edges) >= HISTORY_REQUIRED_BLOCKS
        criteria["blocks"] = len(self._edges)
        if not have_history:
            failures.extend((RULER, UNIMODALITY))
            criteria[RULER] = float("nan")
            criteria[UNIMODALITY] = float("nan")
        else:
            # 2 — the ruler.  Consecutive blocks exactly one fold apart.
            expected = self.fold_seconds * p
            worst = max(abs((self._edges[i] - self._edges[i - 1]) - expected)
                        for i in range(1, len(self._edges)))
            criteria[RULER] = float(worst)
            if worst > self.t.max_ruler_error_samples:
                failures.append(RULER)

            # 3 — unimodality.  Positions about their median.
            med = sorted(self._positions)[len(self._positions) // 2]
            spread = max(abs(self._wrapped(x - med)) for x in self._positions)
            spread_ms = spread / p * 1000.0
            criteria[UNIMODALITY] = float(spread_ms)
            if spread_ms > self.t.max_unimodality_spread_ms:
                failures.append(UNIMODALITY)

        ordered = tuple(c for c in ALL_CRITERIA if c in failures)
        return BatteryVerdict(
            passed=not ordered,
            failures=ordered,
            sigma_ms=float(reported_sigma_ms),
            criteria=criteria,
        )
```

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_t6_battery.py -v`
Expected: PASS, all of them.

- [ ] **Step 5: Prove each criterion carries weight**

A test nobody has watched fail has not been shown to test anything. For each
of the seven criteria: comment out its `failures.append(...)` line, re-run
`tests/test_t6_battery.py`, and confirm the matching
`test_<criterion>_catches_...` goes red while the others stay green. Restore
the line. Record the seven confirmations in the commit message.

Run each time: `python -m pytest tests/test_t6_battery.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/t6_battery.py tests/test_t6_battery.py
git commit -m "t6: the self-consistency battery

Seven criteria decide whether T6 may assert: fold retention, ruler,
unimodality, prominence-and-width, sigma inside the physical budget,
split-half agreement, and plausibility.  Pure functions over the fold's
own evidence -- no I/O, no recorder state.

Prominence carries the weight the external reference used to carry.  Every
criterion that measures repeatability passes a lattice phantom, because a
phantom repeats perfectly; only shape separates it.

Mutation-checked: removing any one criterion's failure line turns its own
test red and leaves the other six green.  Verified for all seven.

Thresholds are placeholders carrying the 2026-09-18 measurements; the C/N0
sweep replaces them next.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Derive the thresholds from the C/N0 sweep

Task 2's defaults came from two stations on one evening. This task derives
them from a sweep and pins them, so a night at B4's worst hour cannot ship a
gate that refuses a healthy station.

**Files:**
- Modify: `src/hf_timestd/core/t6_battery.py` (the `BatteryThresholds` defaults)
- Test: `tests/test_t6_battery_thresholds.py` (create)

**Interfaces:**
- Consumes: `BatteryThresholds`, `T6Battery` from Task 2; `BpskEdgeFineStage`
  from Task 1; `_make_bpsk_signal` and `_noise_std_for` as in Task 1's test.
- Produces: no new names. Later tasks rely on the *defaults* being correct.

**The number that governs.** B4's measured worst hour reads 48.4 dB-Hz.
AI6VN's injected pilot reads 77. A threshold set at 77 and never checked at
48.4 would ship a gate failing every night at exactly the hours the folded
acquisition design says we are judged on.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_battery_thresholds.py`:

```python
"""The thresholds must pass a healthy station at B4's worst hour."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.t6_battery import T6Battery

SR = 96000
BATCH = 1920
K = 30
EDGE = 47916.1672

# B4's measured worst hour, 2026-08-28.  AI6VN's injected pilot reads 77.
B4_WORST_CN0 = 48.4


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _run_blocks(cn0_db_hz: float, n_blocks: int, seed: int):
    """Drive the real stage and the real battery; return verdicts."""
    stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    bat = T6Battery(sample_rate=SR, fold_seconds=K)
    sig = _make_bpsk_signal(
        duration_s=K * n_blocks + 1.0, sample_rate=SR,
        edge_offset_samples=EDGE, noise_std=_noise_std_for(cn0_db_hz),
        seed=seed,
    )
    verdicts = []
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is None:
            continue
        verdicts.append(bat.evaluate(
            est, implied_chain_delay_ns=16_618_000,
            reported_sigma_ms=0.001, cn0_db_hz=cn0_db_hz))
    return verdicts


class TestThresholdsAtB4WorstHour(unittest.TestCase):

    @unittest.skipUnless(__import__("os").environ.get("T6_SWEEP"),
                         "slow: set T6_SWEEP=1")
    def test_a_healthy_station_passes_at_48_4_db_hz(self):
        """The hours we are judged on.  Several seeds, because the
        cliff near 58-59 dB-Hz is stochastic -- one seed passing proves
        nothing about the next."""
        for seed in (11, 23, 37):
            with self.subTest(seed=seed):
                verdicts = _run_blocks(B4_WORST_CN0, 6, seed)
                self.assertTrue(verdicts, "no estimate at all")
                settled = [v for v in verdicts[2:]]
                self.assertTrue(settled)
                self.assertTrue(
                    any(v.passed for v in settled),
                    f"no block passed: {[v.failures for v in settled]}")

    def test_a_healthy_station_passes_on_an_injected_pilot(self):
        """AI6VN's 77 dB-Hz.  Fast enough to run every time."""
        verdicts = _run_blocks(77.0, 5, seed=11)
        self.assertTrue(verdicts)
        self.assertTrue(any(v.passed for v in verdicts[2:]),
                        f"{[v.failures for v in verdicts[2:]]}")


class TestSigmaCeilingTracksCn0(unittest.TestCase):

    def test_the_ceiling_widens_as_the_channel_degrades(self):
        """Scatter follows 1/sqrt(SNR): 6 dB lost doubles it.  A fixed
        ceiling would refuse healthy stations after dark."""
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        hi = bat._predicted_sigma_ms(77.0)
        lo = bat._predicted_sigma_ms(48.4)
        self.assertGreater(lo, hi * 10)

    def test_477_ms_fails_at_every_cn0_we_run(self):
        """The AI6VN reading of 2026-09-18.  No band condition excuses it."""
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        for cn0 in (77.0, 58.0, 48.4, 44.0):
            with self.subTest(cn0=cn0):
                ceiling = bat._predicted_sigma_ms(cn0) * bat.t.sigma_margin
                self.assertLess(ceiling, 477.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail or pass**

Run: `T6_SWEEP=1 python -m pytest tests/test_t6_battery_thresholds.py -v`

Expected: the 77 dB-Hz case and both sigma-ceiling cases pass on Task 2's
defaults. The 48.4 dB-Hz case is the one under investigation — it may fail,
and if it does, the failure names which threshold to move.

- [ ] **Step 3: Measure, then set each default**

Instrument rather than guess. For each of C/N0 ∈ {77, 66, 58, 52, 48.4, 44}
and seeds {11, 23, 37}, drive `_run_blocks` and print
`v.criteria` for every settled block. Then set each default so that:

- every healthy run at 48.4 dB-Hz passes, across all three seeds
- the failure fixtures in `tests/test_t6_battery.py` still fail

Write the measured spread into the comment above each field — replacing the
2026-09-18 note with the sweep's numbers, keeping the derivation prose. Do not
widen a threshold past what the sweep shows; if 48.4 dB-Hz cannot pass a
criterion at any defensible threshold, stop and report that finding rather
than loosening until it passes.

⛔ Do not convert any threshold into a config key. Spec §4.5: module constant,
derivation beside it, never an operator knob.

- [ ] **Step 4: Re-run both battery test files**

Run:
```
T6_SWEEP=1 python -m pytest tests/test_t6_battery.py \
                            tests/test_t6_battery_thresholds.py -v
```
Expected: PASS throughout. If a failure fixture in `test_t6_battery.py` stopped
failing, the threshold moved too far — tighten it back.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/t6_battery.py tests/test_t6_battery_thresholds.py
git commit -m "t6: derive the battery thresholds from a C/N0 sweep

Every threshold now carries a measured spread rather than two stations on
one evening.  The governing case is B4's 48.4 dB-Hz worst hour, across
three seeds, because the cliff near 58-59 dB-Hz is stochastic and one seed
passing says nothing about the next.

The sigma ceiling tracks C/N0 rather than sitting fixed: scatter follows
1/sqrt(SNR), so a fixed ceiling would refuse healthy stations after dark.
477 ms fails at every C/N0 we run.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The ordinal resolver

Replace the reference that must beat T6 with a resolver that only names a
second. Nothing consumes it yet — Task 5 wires it in.

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (add beside
  `_get_disambiguation_reference` at `:2355`; the constant beside
  `T6_DISAMBIGUATION_MAX_SIGMA_MS` at `:2238`)
- Test: `tests/test_t6_ordinal_resolver.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `CoreRecorderV2.T6_ORDINAL_MAX_SIGMA_MS = 50.0`
  - `CoreRecorderV2._get_ordinal_reference(self) -> tuple[float, float, str] | None`
    returning `(offset_ms, sigma_ms, tier_name)`, the first tier in rank order
    whose σ ≤ 50 ms — otherwise `None`.

Task 5 calls `_get_ordinal_reference`. Keep the name and the return shape.

**Why 50 ms.** The plausibility bound is 250 ms; a fifth of it leaves four
sigma of room to name the right second. T2 sits at ~20 ms
(`authority_manager.TRUST_SIGMA_MS`) and never goes away, so the resolver
becomes structural rather than conditional.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_ordinal_resolver.py`:

```python
"""The ordinal resolver names a second and nothing more."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _recorder():
    """A bare object carrying only what the resolver reads."""
    r = SimpleNamespace()
    r.T6_ORDINAL_MAX_SIGMA_MS = CoreRecorderV2.T6_ORDINAL_MAX_SIGMA_MS
    r._get_ordinal_reference = CoreRecorderV2._get_ordinal_reference.__get__(r)
    return r


def _chronyc(last_offset_s: float, rms_offset_s: float):
    out = (f"Last offset     : {last_offset_s} seconds\n"
           f"RMS offset      : {rms_offset_s} seconds\n")
    return mock.Mock(returncode=0, stdout=out)


class TestOrdinalResolver(unittest.TestCase):

    def test_the_threshold_is_a_fifth_of_the_plausibility_bound(self):
        self.assertEqual(CoreRecorderV2.T6_ORDINAL_MAX_SIGMA_MS, 50.0)

    def test_ai6vn_shaped_station_resolves(self):
        """No T5, no T3, T4 chronyc at 0.29 ms.  The old gate refused
        this station; the ordinal resolver must not."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.000284, 0.00029)):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)
        offset_ms, sigma_ms, tier = ref
        self.assertLess(sigma_ms, 50.0)
        self.assertEqual(tier, "T4")

    def test_a_20_ms_clock_resolves(self):
        """T2, WAN NTP.  The tier least able to do T6's job performs the
        only job T6 needs from it."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.020, 0.020)):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)

    def test_a_clock_worse_than_50_ms_is_refused(self):
        """A station holding no clock within 50 ms of UTC has a far
        larger problem than a T6 acquisition."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.4, 0.4)):
            ref = r._get_ordinal_reference()
        self.assertIsNone(ref)

    def test_no_clock_at_all_returns_none(self):
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", side_effect=OSError("no chronyc")):
            self.assertIsNone(r._get_ordinal_reference())

    def test_fusion_wins_when_it_is_available_and_sane(self):
        """A real HF antenna sharpens T3.  It makes an easy question
        easier and changes nothing else."""
        import json
        r = _recorder()
        payload = json.dumps({
            "schema": "v1",
            "fusion": {"available": True, "kalman_state": "LOCKED",
                       "d_clock_fused_ms": 1.25, "uncertainty_ms": 4.3},
        })
        with mock.patch("pathlib.Path.read_text", return_value=payload):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)
        self.assertEqual(ref[2], "T3")

    def test_the_resolver_reports_sigma_it_does_not_hide_it(self):
        """Provenance survives: the caller records which source named
        the second."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.000284, 0.00029)):
            offset_ms, sigma_ms, tier = r._get_ordinal_reference()
        self.assertAlmostEqual(sigma_ms, 0.29, places=3)
        self.assertAlmostEqual(offset_ms, -0.284, places=3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_t6_ordinal_resolver.py -v`
Expected: `AttributeError: type object 'CoreRecorderV2' has no attribute 'T6_ORDINAL_MAX_SIGMA_MS'`.

- [ ] **Step 3: Add the constant**

In `core_recorder_v2.py`, beside `T6_DISAMBIGUATION_MAX_SIGMA_MS` (`:2238`):

```python
    # ORDINAL resolution — which GPS second the edge belongs to
    # (T6_ACCEPTANCE_CRITERIA.md §3.1).  A different question from the
    # one T6_DISAMBIGUATION_MAX_SIGMA_MS was written for, and four
    # orders of magnitude easier: the physical-plausibility bound is
    # 250 ms, so a fifth of it leaves four sigma of room to name the
    # right second.  T2 sits at ~20 ms and never goes away, which makes
    # this resolver structural rather than conditional.
    #
    # ⛔ Do not confuse this with sample-level disambiguation.  No tier
    # we run reaches the 10.4 us a sample spans at 96 kHz; only the
    # measurement does.  See §2.1.
    T6_ORDINAL_MAX_SIGMA_MS = 50.0
```

- [ ] **Step 4: Write the resolver**

Add beside `_get_disambiguation_reference`:

```python
    def _get_ordinal_reference(self):
        """Return a clock good enough to name the GPS second, or None.

        Walks the tier ranking downward and takes the first source whose
        sigma clears ``T6_ORDINAL_MAX_SIGMA_MS``.  Returns
        ``(offset_ms, sigma_ms, tier_name)``.

        This grants the source nothing beyond an integer.  It does not
        measure a phase, does not gate, and does not slew — the fold
        supplies the sub-second term (§3.2).  The returned tier name
        travels into the anchor's ``captured_via_tier`` so provenance
        survives.
        """
        # T3 — HF Fusion.  Sharpens with a real antenna; at any usable
        # sigma it names the second comfortably.
        try:
            fusion_path = Path('/run/hf-timestd/fusion_status.json')
            data = json.loads(fusion_path.read_text())
            if data.get('schema') == 'v1':
                fusion = data.get('fusion') or {}
                if (fusion.get('available')
                        and fusion.get('kalman_state') in ('LOCKED', 'ACQUIRING')):
                    offset_ms = float(fusion['d_clock_fused_ms'])
                    sigma_ms = float(fusion['uncertainty_ms'])
                    if sigma_ms <= self.T6_ORDINAL_MAX_SIGMA_MS:
                        return offset_ms, sigma_ms, 'T3'
        except (FileNotFoundError, OSError, json.JSONDecodeError,
                KeyError, ValueError):
            pass

        # T4/T2 — chrony.  `Last offset` reads (true_time − local_time);
        # negate for (system_clock − UTC).  Naming the tier T4 vs T2 by
        # source is the offset judge's job, not ours; for an ordinal the
        # distinction does not change the answer, so report what chrony
        # is actually disciplined to.
        try:
            import subprocess
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                last_offset_sec = None
                rms_offset_sec = None
                for line in result.stdout.splitlines():
                    if line.startswith('Last offset'):
                        last_offset_sec = float(line.split(':', 1)[1].split()[0])
                    elif line.startswith('RMS offset'):
                        rms_offset_sec = float(line.split(':', 1)[1].split()[0])
                if last_offset_sec is not None and rms_offset_sec is not None:
                    sigma_ms = rms_offset_sec * 1000.0
                    if sigma_ms <= self.T6_ORDINAL_MAX_SIGMA_MS:
                        return -last_offset_sec * 1000.0, sigma_ms, 'T4'
        except Exception:
            pass

        return None
```

⚠ Check that `Path` and `json` are already imported at module scope in
`core_recorder_v2.py` — `_get_disambiguation_reference` uses both, so they
should be. If not, import them at module scope, not inside the function.

- [ ] **Step 5: Run the test**

Run: `python -m pytest tests/test_t6_ordinal_resolver.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_t6_ordinal_resolver.py
git commit -m "t6: an ordinal resolver that names a second, nothing more

The old gate asked one question at one threshold: a non-T6 reference at
sigma < 10 us.  At 96 kHz that sits below one sample period and no tier we
run delivers it.  Naming the GPS second needs 250 ms, not 10 us -- four
orders of magnitude easier, and T2 at ~20 ms clears it twelvefold.

The resolver grants its source an integer and nothing else: no phase, no
gate, no slew.  Provenance travels on in captured_via_tier.

Nothing consumes it yet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Acquire on the fold, with the battery as the gate

The rewire. `_t6_disambiguate_via_external_reference` stops deriving the
sub-second term from a reference offset and starts taking it from the
battery-passed fold, using the ordinal only to name the second.

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`
  (`_t6_disambiguate_via_external_reference` at `:4351`; its call site at
  `:4990`)
- Test: `tests/test_t6_acquire_on_fold.py` (create)

**Interfaces:**
- Consumes: `_get_ordinal_reference` (Task 4); `T6Battery`, `BatteryVerdict`
  (Tasks 2–3); `FineEdgeEstimate`'s new fields (Task 1).
- Produces:
  - `CoreRecorderV2._t6_battery: T6Battery | None` — constructed on first use
  - `CoreRecorderV2._t6_last_verdict: BatteryVerdict | None` — Task 6 reads it
  - `_t6_disambiguate_via_external_reference(self, result)` keeps its name and
    signature; Task 9's integration tests drive it.

**What changes, precisely.** Today:

```python
disagreement_sec = offset_sec - (ref_offset_ms / 1000.0)
shift_samples    = round(disagreement_sec * sr_local)
```

The reference offset supplies the sub-second correction. After this task the
battery-passed fold position supplies it, and `ref_offset_ms` is used only
inside `pps_firing_utc_ns` to place the named second — which it already does.

⛔ Keep the ±250 ms plausibility guard exactly where it sits. It is criterion 7
and the last defence against a gross wrap.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_acquire_on_fold.py`:

```python
"""Acquisition runs on the fold; the ordinal only names the second."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_battery import T6Battery

SR = 96000
K = 30

# rtp_to_utc's return for the fixture edge.  Chosen so the RAW phase the
# method derives lands on the fold's own phase and the implied shift is
# ~0 samples:
#     (EDGE_UTC - chain_delay 0.016618 s) mod 1 s == 47916/96000 == 0.499125
# A bare 1_700_000_000.0 would put the raw phase at ~94406 samples against
# a fold phase of 47916, implying a -484 ms shift that the +-250 ms
# plausibility guard correctly refuses -- failing the test for a reason
# unrelated to what it tests.
EDGE_UTC = 1_700_000_000.515743


def _estimate(edge_rtp, **over):
    kw = dict(
        edge_offset_samples=47916.0, edge_rtp=edge_rtp, edge_subsample=0.0,
        n_seconds_folded=K, plateau_amplitude=1.0, fit_rms=0.02,
        fold_retention=0.98, split_half_delta_samples=0.05,
        peak_prominence=40.0, transition_width_samples=6.0,
    )
    kw.update(over)
    return FineEdgeEstimate(**kw)


def _recorder(ordinal=(-0.284, 0.29, "T4")):
    """A bare namespace carrying only what the method touches."""
    r = SimpleNamespace()
    for name in ("_t6_disambiguate_via_external_reference",
                 "_t6_say_once"):
        setattr(r, name, getattr(CoreRecorderV2, name).__get__(r))
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r._t6_say_once_at = {}
    r._get_ordinal_reference = lambda: ordinal
    r._t6_battery = T6Battery(sample_rate=SR, fold_seconds=K)
    r._t6_last_verdict = None
    r._t6_disambiguation_ns = 0
    r._t6_native_anchor = None
    r._t6_rate_reset = lambda _why: None
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR,
                                       _last_edge_rtp=1_000_000 + 47916)
    r._t6_fine_estimate = None
    r._t6_cn0_db_hz = 70.0
    return r


class TestAcquisitionUsesTheFold(unittest.TestCase):

    def test_a_passing_battery_captures_an_anchor(self):
        r = _recorder()
        result = SimpleNamespace(chain_delay_ns=16_618_000)
        with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                        return_value=EDGE_UTC):
            for i in range(4):
                r._t6_fine_estimate = _estimate(1_000_000 + 47916 + i * SR * K)
                r._t6_disambiguate_via_external_reference(result)
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertTrue(r._t6_last_verdict.passed, r._t6_last_verdict.failures)

    def test_a_failing_battery_captures_nothing(self):
        r = _recorder()
        result = SimpleNamespace(chain_delay_ns=16_618_000)
        with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                        return_value=EDGE_UTC):
            for i in range(4):
                r._t6_fine_estimate = _estimate(
                    1_000_000 + 47916 + i * SR * K, fold_retention=0.006)
                r._t6_disambiguate_via_external_reference(result)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("retention", r._t6_last_verdict.failures)

    def test_no_ordinal_source_refuses_without_capturing(self):
        """A station with no clock within 50 ms has a larger problem."""
        r = _recorder(ordinal=None)
        result = SimpleNamespace(chain_delay_ns=16_618_000)
        with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                        return_value=EDGE_UTC):
            for i in range(4):
                r._t6_fine_estimate = _estimate(1_000_000 + 47916 + i * SR * K)
                r._t6_disambiguate_via_external_reference(result)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_plausibility_guard_still_refuses_a_gross_wrap(self):
        r = _recorder()
        result = SimpleNamespace(chain_delay_ns=400_000_000)
        with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                        return_value=EDGE_UTC):
            for i in range(4):
                r._t6_fine_estimate = _estimate(1_000_000 + 47916 + i * SR * K)
                r._t6_disambiguate_via_external_reference(result)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_anchor_records_which_tier_named_the_second(self):
        r = _recorder()
        result = SimpleNamespace(chain_delay_ns=16_618_000)
        with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                        return_value=EDGE_UTC):
            for i in range(4):
                r._t6_fine_estimate = _estimate(1_000_000 + 47916 + i * SR * K)
                r._t6_disambiguate_via_external_reference(result)
        self.assertEqual(r._t6_native_anchor.captured_via_tier, "T4")

    def test_a_worse_ordinal_sigma_does_not_change_the_phase(self):
        """The ordinal names a second and nothing more.  T4 at 0.29 ms
        and T2 at 20 ms must place the same edge."""
        anchors = []
        for ordinal in ((-0.284, 0.29, "T4"), (-0.284, 20.0, "T2")):
            r = _recorder(ordinal=ordinal)
            result = SimpleNamespace(chain_delay_ns=16_618_000)
            with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                            return_value=EDGE_UTC):
                for i in range(4):
                    r._t6_fine_estimate = _estimate(
                        1_000_000 + 47916 + i * SR * K)
                    r._t6_disambiguate_via_external_reference(result)
            anchors.append(r._t6_native_anchor)
        self.assertIsNotNone(anchors[0])
        self.assertIsNotNone(anchors[1])
        self.assertEqual(anchors[0].chain_delay_ns, anchors[1].chain_delay_ns)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_t6_acquire_on_fold.py -v`
Expected: failures naming `_get_ordinal_reference` unused or
`_t6_last_verdict` never set — the method still runs the old path.

- [ ] **Step 3: Rewire the method**

Rewrite `_t6_disambiguate_via_external_reference`. Keep every part not
mentioned here exactly as it stands — the anchor construction, the ledger
append, `_t6_rate_reset`, the outer try/except, and the ±250 ms guard.

Replace the reference lookup:

```python
            ref = self._get_disambiguation_reference()
```

with the ordinal lookup and the battery:

```python
            ref = self._get_ordinal_reference()
            if ref is None:
                if self._t6_say_once('ordinal_none'):
                    logger.warning(
                        "T6 acquisition: no clock within %.0f ms to name the "
                        "GPS second, so the edge cannot be labelled.  A "
                        "station holding no clock that close to UTC has a "
                        "larger problem than a T6 acquisition.  (Repeated at "
                        "most every %.0f s.)",
                        self.T6_ORDINAL_MAX_SIGMA_MS, self.T6_REPEAT_PERIOD_SEC)
                return
            ref_offset_ms, ref_sigma_ms, ref_tier = ref

            est = getattr(self, '_t6_fine_estimate', None)
            if est is None:
                return
            if getattr(self, '_t6_battery', None) is None:
                from .t6_battery import T6Battery
                self._t6_battery = T6Battery(
                    sample_rate=int(self._t6_calibrator.sample_rate),
                    fold_seconds=int(est.n_seconds_folded),
                )
```

Then, where the method computes `shift_samples`, take the phase from the fold
instead of from the reference offset:

```python
            # §3.2 — the fold supplies the phase within the second.  The
            # ordinal supplies only which second that phase belongs to.
            # Where this method used to read
            #     disagreement_sec = offset_sec - (ref_offset_ms / 1000.0)
            # the reference offset carried the sub-second term.  No tier
            # we run resolves 10.4 us, so that term now comes from the
            # measurement (§2.1).
            sr_local = self._t6_calibrator.sample_rate
            fold_phase_samples = float(est.edge_offset_samples)
            raw_phase_samples = (wall_time_sec % 1.0) * sr_local
            shift_samples = int(round(
                (fold_phase_samples - raw_phase_samples + sr_local / 2)
                % sr_local - sr_local / 2))
            self._t6_disambiguation_ns = int(round(
                shift_samples * 1e9 / sr_local))
```

Run the battery immediately before the anchor capture, after
`effective_chain_delay_ns` exists and after the ±250 ms guard:

```python
            verdict = self._t6_battery.evaluate(
                est,
                implied_chain_delay_ns=effective_chain_delay_ns,
                reported_sigma_ms=float(getattr(self, '_t6_sigma_ms', 0.0)),
                cn0_db_hz=getattr(self, '_t6_cn0_db_hz', None),
            )
            self._t6_last_verdict = verdict
            if not verdict.passed:
                if self._t6_say_once('battery_refused'):
                    logger.info(
                        "T6 acquisition held: self-consistency battery "
                        "failed on %s (%s).  T6 does not assert; the "
                        "station runs its fallback.  (Repeated at most "
                        "every %.0f s.)",
                        ", ".join(verdict.failures),
                        self._t6_battery_detail(verdict),
                        self.T6_REPEAT_PERIOD_SEC)
                return
```

and add the small formatter beside it:

```python
    @staticmethod
    def _t6_battery_detail(verdict) -> str:
        """One compact line of the numbers behind a verdict."""
        return " ".join(
            f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
            for k, v in verdict.criteria.items())
```

⚠ The tests patch `ka9q.rtp_recorder.rtp_to_utc`, not a name on
`core_recorder_v2`, because the method imports it inside its own body
(`from ka9q.rtp_recorder import rtp_to_utc`). Patching the importing module
would never take. Leave that import where it sits.

⚠ `_t6_fine_estimate`, `_t6_sigma_ms` and `_t6_cn0_db_hz` may carry different
names in this recorder. Before writing, grep for where the fine estimate and
the C/N0 reach this class — the call site at `:4990` is the place to look —
and use the real names. Do not invent an attribute that nothing sets.

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_t6_acquire_on_fold.py -v`
Expected: PASS.

- [ ] **Step 5: Run every T6 test in the repo**

Run: `python -m pytest tests/ -k "t6 or bpsk or anchor" -v`
Expected: PASS. This rewire touches the path several existing suites drive;
a regression here matters more than the new tests passing.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_t6_acquire_on_fold.py
git commit -m "t6: acquire on the fold, with the battery as the gate

The sub-second term used to come from a reference offset, which required
a tier resolving 10.4 us -- the sample period at 96 kHz.  None exists, so
DASI-009.AI6VN never completed acquisition and sat at T2 while its edge
landed on one sample position 128 seconds running.

The fold now supplies the phase and the ordinal supplies only the second.
The self-consistency battery decides whether the edge may assert.  The
+-250 ms plausibility guard stays exactly where it was: it is criterion 7
and the last defence against a gross wrap.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: A failed guardrail keeps asserting

Acquisition refuses. A *running* T6 does not: it keeps asserting, marks the
assertion suspect, names the criterion, and alarms.

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`
- Test: `tests/test_t6_suspect_assertion.py` (create)

**Interfaces:**
- Consumes: `_t6_last_verdict` (Task 5).
- Produces:
  - `CoreRecorderV2._t6_suspect: tuple[str, ...]` — failing criteria on a
    running T6, empty when healthy. Task 9 asserts on it.
  - `authority_snapshot` gains `t6_suspect_criteria: str` (comma-joined,
    empty when healthy) and `t6_battery_blocks: int`.

⛔ **A failed guardrail and a missing estimate are different things.** Never
route an absent estimate through the suspect path. When estimates stop
arriving the authority's existing liveness invariant degrades loudly, exactly
as today. Absence stays visible as absence.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_suspect_assertion.py`:

```python
"""A running T6 that fails its battery keeps asserting, loudly."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.t6_battery import BatteryVerdict


def _recorder():
    r = SimpleNamespace()
    r._t6_note_verdict = CoreRecorderV2._t6_note_verdict.__get__(r)
    r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r._t6_say_once_at = {}
    r._t6_suspect = ()
    r._t6_native_anchor = object()  # already acquired: T6 is running
    return r


def _verdict(passed, failures=()):
    return BatteryVerdict(passed=passed, failures=tuple(failures),
                          sigma_ms=0.001, criteria={"blocks": 5})


class TestRunningT6(unittest.TestCase):

    def test_a_failing_battery_marks_suspect_and_keeps_the_anchor(self):
        r = _recorder()
        anchor = r._t6_native_anchor
        r._t6_note_verdict(_verdict(False, ("split_half",)))
        self.assertEqual(r._t6_suspect, ("split_half",))
        self.assertIs(r._t6_native_anchor, anchor,
                      "a failed guardrail must not withdraw the anchor")

    def test_recovery_clears_the_mark(self):
        r = _recorder()
        r._t6_note_verdict(_verdict(False, ("split_half",)))
        r._t6_note_verdict(_verdict(True))
        self.assertEqual(r._t6_suspect, ())

    def test_every_failing_criterion_is_named(self):
        r = _recorder()
        r._t6_note_verdict(_verdict(False, ("retention", "prominence")))
        self.assertEqual(r._t6_suspect, ("retention", "prominence"))

    def test_the_alarm_is_throttled(self):
        """A persistent condition reported every cycle blinds the log it
        writes to.  This project has collected five such floods."""
        r = _recorder()
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            for _ in range(20):
                r._t6_note_verdict(_verdict(False, ("split_half",)))
        self.assertEqual(len(cm.output), 1, cm.output)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_t6_suspect_assertion.py -v`
Expected: `AttributeError: type object 'CoreRecorderV2' has no attribute '_t6_note_verdict'`.

- [ ] **Step 3: Write the method**

Add to `CoreRecorderV2`:

```python
    def _t6_note_verdict(self, verdict) -> None:
        """Record a battery verdict for a T6 that has already acquired.

        Spec §5.1: a failed guardrail does NOT demote.  Demoting would
        reproduce the problem this design removes -- a transient trip
        handing the station back to a tier two orders of magnitude worse.
        T6 keeps asserting, marked suspect, and alarms; the operator
        decides.

        ⛔ Spec §5.2: this path is for a T6 that is producing estimates
        and failing them.  An ABSENT estimate never comes here.  The
        authority's liveness invariant degrades loudly on absence, and
        blurring the two would hide a dead detector behind a warning
        about a live one.
        """
        if getattr(self, '_t6_native_anchor', None) is None:
            return  # not running yet; acquisition owns the refusal
        if verdict.passed:
            if self._t6_suspect:
                logger.info(
                    "T6 self-consistency restored (%s cleared); the "
                    "assertion is no longer marked suspect.",
                    ", ".join(self._t6_suspect))
            self._t6_suspect = ()
            return
        self._t6_suspect = tuple(verdict.failures)
        if self._t6_say_once('battery_suspect'):
            logger.warning(
                "T6 SUSPECT: self-consistency battery failed on %s while "
                "T6 is asserting.  The anchor is KEPT and T6 continues to "
                "publish, marked suspect -- a timing fault gets exposed, "
                "never silently corrected.  Operator judgement required.  "
                "(Repeated at most every %.0f s.)",
                ", ".join(verdict.failures), self.T6_REPEAT_PERIOD_SEC)
```

- [ ] **Step 4: Surface it in the snapshot**

Find where `authority_snapshot` gets built (grep for `authority_snapshot` in
`core_recorder_v2.py` and `authority_manager.py`) and add two columns beside
the existing T6 ones:

```python
        snapshot["t6_suspect_criteria"] = ",".join(
            getattr(self, "_t6_suspect", ()) or ())
        snapshot["t6_battery_blocks"] = int(
            (getattr(self, "_t6_last_verdict", None)
             and self._t6_last_verdict.criteria.get("blocks", 0)) or 0)
```

⚠ Match the surrounding code's style for optional columns — the file already
has a convention for a value that may not exist yet. Follow it rather than the
sketch above if they differ.

- [ ] **Step 5: Call it from the estimate path**

At the point where a fine estimate reaches the recorder with T6 already
anchored (near the call site at `:4990`), evaluate and note:

```python
                    if self._t6_native_anchor is not None and est is not None:
                        v = self._t6_battery.evaluate(
                            est,
                            implied_chain_delay_ns=self._t6_native_anchor.chain_delay_ns,
                            reported_sigma_ms=float(
                                getattr(self, '_t6_sigma_ms', 0.0)),
                            cn0_db_hz=getattr(self, '_t6_cn0_db_hz', None),
                        )
                        self._t6_last_verdict = v
                        self._t6_note_verdict(v)
```

⚠ Use the real local names at that call site. If `_t6_battery` can be `None`
there (T6 never acquired), guard for it.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_t6_suspect_assertion.py tests/test_t6_acquire_on_fold.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_t6_suspect_assertion.py
git commit -m "t6: a failed guardrail keeps asserting, marked suspect

A battery failure on a RUNNING T6 does not demote.  Demoting would
reproduce the problem this design removes -- a transient trip handing the
station back to a tier two orders of magnitude worse.  T6 keeps the
anchor, keeps publishing, names the failing criterion, and alarms once per
throttle period.

An absent estimate never reaches this path.  The authority's liveness
invariant still degrades loudly on absence, so a dead detector cannot hide
behind a warning about a live one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The judge inverts — T6 checks the ordinal

Once T6 passes the battery, T6 becomes the bench. It extrapolates its own
anchor forward and checks the ordinal source against itself.

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`
- Test: `tests/test_t6_ordinal_crosscheck.py` (create)

**Interfaces:**
- Consumes: `_get_ordinal_reference` (Task 4), `_t6_native_anchor` (existing).
- Produces:
  - `CoreRecorderV2.T6_ORDINAL_DISAGREEMENT_ALARM_SEC = 0.5`
  - `CoreRecorderV2._t6_check_ordinal(self) -> float | None` — the
    disagreement in seconds, or `None` when it cannot be computed.

**The point.** This mirrors what `authority_manager._witness_drives_consequences`
(`:665`) already does for a GPS-disciplined rtp-frame tier against
sysclock-frame witnesses: the witness flags, it never demotes. The acquisition
path gains the shielding the arbitration path has had all along.

- [ ] **Step 1: Write the failing test**

Create `tests/test_t6_ordinal_crosscheck.py`:

```python
"""A functional T6 is the bench.  Disagreement indicts the ordinal."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.native_anchor import NativeAnchor

SR = 96000


def _recorder(ordinal_offset_ms, anchor_utc_ns=1_700_000_000_000_000_000):
    r = SimpleNamespace()
    r._t6_check_ordinal = CoreRecorderV2._t6_check_ordinal.__get__(r)
    r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r.T6_ORDINAL_DISAGREEMENT_ALARM_SEC = \
        CoreRecorderV2.T6_ORDINAL_DISAGREEMENT_ALARM_SEC
    r._t6_say_once_at = {}
    r._get_ordinal_reference = lambda: (ordinal_offset_ms, 20.0, "T2")
    r._t6_native_anchor = NativeAnchor(
        anchor_rtp=47916, anchor_utc_ns=anchor_utc_ns, sample_rate_hz=SR,
        chain_delay_ns=16_618_000, captured_at_utc_ns=anchor_utc_ns,
        captured_via_tier="T2",
    )
    r._t6_now_utc_ns = lambda: anchor_utc_ns
    return r


class TestOrdinalCrossCheck(unittest.TestCase):

    def test_a_sane_ordinal_raises_nothing(self):
        r = _recorder(ordinal_offset_ms=20.0)
        with self.assertNoLogs("hf_timestd.core.core_recorder_v2",
                               level="WARNING"):
            d = r._t6_check_ordinal()
        self.assertIsNotNone(d)
        self.assertLess(abs(d), 0.5)

    def test_a_two_second_error_alarms(self):
        r = _recorder(ordinal_offset_ms=2000.0)
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            r._t6_check_ordinal()
        self.assertTrue(any("ordinal" in line.lower() for line in cm.output))

    def test_the_alarm_names_the_ordinal_not_t6(self):
        """Under primacy the functional T6 is the bench."""
        r = _recorder(ordinal_offset_ms=2000.0)
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            r._t6_check_ordinal()
        joined = " ".join(cm.output)
        self.assertIn("T2", joined)

    def test_t6_keeps_its_anchor_through_the_disagreement(self):
        r = _recorder(ordinal_offset_ms=2000.0)
        anchor = r._t6_native_anchor
        r._t6_check_ordinal()
        self.assertIs(r._t6_native_anchor, anchor)

    def test_no_anchor_means_no_check(self):
        r = _recorder(ordinal_offset_ms=2000.0)
        r._t6_native_anchor = None
        self.assertIsNone(r._t6_check_ordinal())

    def test_no_ordinal_means_no_check(self):
        r = _recorder(ordinal_offset_ms=0.0)
        r._get_ordinal_reference = lambda: None
        self.assertIsNone(r._t6_check_ordinal())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_t6_ordinal_crosscheck.py -v`
Expected: `AttributeError: ... '_t6_check_ordinal'`.

- [ ] **Step 3: Write the check**

Add the constant beside `T6_ORDINAL_MAX_SIGMA_MS`:

```python
    # §5.3 — a functional T6 becomes the bench.  Half a second is the
    # whole question the ordinal answers, so anything past it means the
    # ORDINAL named the wrong second, not that T6 found the wrong edge.
    T6_ORDINAL_DISAGREEMENT_ALARM_SEC = 0.5
```

and the method:

```python
    def _t6_check_ordinal(self):
        """Check the ordinal source against T6's own extrapolated anchor.

        Spec §5.3, the judge inverted.  Once T6 passes the battery it
        holds the better registration by two orders of magnitude, so a
        disagreement past half a second indicts the ORDINAL SOURCE.  T6
        keeps its own count and alarms naming that source.

        This mirrors authority_manager._witness_drives_consequences,
        which already shields a GPS-disciplined rtp-frame tier from
        sysclock-frame witnesses: they flag, they never demote.

        Returns the disagreement in seconds, or None when either side is
        unavailable.
        """
        anchor = getattr(self, '_t6_native_anchor', None)
        if anchor is None:
            return None
        ref = self._get_ordinal_reference()
        if ref is None:
            return None
        ref_offset_ms, ref_sigma_ms, ref_tier = ref
        now_ns = self._t6_now_utc_ns()
        # T6's own view of (system_clock − UTC), from the anchor.
        t6_offset_sec = (now_ns - anchor.anchor_utc_ns) / 1e9
        disagreement = t6_offset_sec - (ref_offset_ms / 1000.0)
        if abs(disagreement) > self.T6_ORDINAL_DISAGREEMENT_ALARM_SEC:
            if self._t6_say_once('ordinal_disagrees'):
                logger.warning(
                    "ORDINAL SOURCE %s DISAGREES with T6 by %+.3f s "
                    "(its offset %+.3f ms, sigma %.3f ms).  T6 holds the "
                    "better registration by two orders of magnitude and "
                    "KEEPS its own count; this indicts %s, not T6.  "
                    "Check that source's discipline.  (Repeated at most "
                    "every %.0f s.)",
                    ref_tier, disagreement, ref_offset_ms, ref_sigma_ms,
                    ref_tier, self.T6_REPEAT_PERIOD_SEC)
        return disagreement
```

⚠ `_t6_now_utc_ns` may not exist. If the recorder reads the clock another way,
use that and adapt the test's stub name to match. Do not add a second clock
accessor to the class.

- [ ] **Step 4: Call it on the T6 poll**

Call `self._t6_check_ordinal()` once per T6 poll cycle, beside the other
periodic T6 work. Its return value goes nowhere — the alarm is the product.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_t6_ordinal_crosscheck.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/core_recorder_v2.py tests/test_t6_ordinal_crosscheck.py
git commit -m "t6: the judge inverts -- a functional T6 checks the ordinal

Once T6 passes the battery it holds the better registration by two orders
of magnitude.  A disagreement past half a second therefore says the
ordinal source named the wrong second, not that T6 found the wrong edge,
and the alarm names that source.  T6 keeps its own count.

This mirrors authority_manager._witness_drives_consequences, which has
shielded a GPS-disciplined rtp-frame tier from sysclock witnesses all
along.  The acquisition path finally gets the same treatment.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Cap the judge's cross-bench tolerance

The cross-bench gate's bound scales with the candidate's own σ, so a bench
reporting worse uncertainty earns a wider licence. Cap it.

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (near `cross_bench_k` at
  `:1038` and wherever `_cross_bench_delta_ns` is compared)
- Test: `tests/test_offset_judge_sigma_ceiling.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `offset_judge.CROSS_BENCH_SIGMA_CEILING_MS = 5.0` and its use
  inside the cross-bench comparison.

**The history this closes.** The gate arrived in August after the
2026-08-05 displaced-peak incident — "a biased-but-stable T6 was adopted over
a healthy T5 because its honest wide sigma kept k·σ quiet." The gate then
reproduced that polarity one level up. At AI6VN on 2026-09-18 a σ of 477 ms
opened the bound to roughly 2.4 s and a 284 ms disagreement passed.

`authority_manager` met the mirror-image problem and **floored** its pair
thresholds so a noisy witness could not mask a real disagreement. This needs
the opposite: a **ceiling**, so a noisy candidate cannot excuse its own error.

- [ ] **Step 1: Write the failing test**

Create `tests/test_offset_judge_sigma_ceiling.py`:

```python
"""A worse sigma must not buy a wider licence."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core import offset_judge


def _bound_ms(sigma_c_ms, sigma_l_ms, k=5.0):
    """The bound as the gate computes it, after capping."""
    c = min(sigma_c_ms, offset_judge.CROSS_BENCH_SIGMA_CEILING_MS)
    l = min(sigma_l_ms, offset_judge.CROSS_BENCH_SIGMA_CEILING_MS)
    return k * math.sqrt(c * c + l * l)


class TestCeiling(unittest.TestCase):

    def test_the_ceiling_exists(self):
        self.assertTrue(hasattr(offset_judge, "CROSS_BENCH_SIGMA_CEILING_MS"))

    def test_477_ms_no_longer_admits_284_ms(self):
        """The AI6VN case of 2026-09-18.  Uncapped, the bound reached
        ~2.4 s and 284 ms sailed through."""
        uncapped = 5.0 * math.sqrt(477.0 ** 2 + 2.0 ** 2)
        self.assertGreater(uncapped, 284.0, "premise: it used to pass")
        self.assertLess(_bound_ms(477.0, 2.0), 284.0)

    def test_an_honest_narrow_sigma_is_untouched(self):
        """T6 at 1 us against T5 at 1 ms.  The cap must not tighten a
        pair that was already inside it."""
        capped = _bound_ms(0.001, 1.0)
        uncapped = 5.0 * math.sqrt(0.001 ** 2 + 1.0 ** 2)
        self.assertAlmostEqual(capped, uncapped, places=6)

    def test_a_legitimately_wide_bench_still_gets_its_room(self):
        """T5 over USB is bus-jitter floored near 1 ms; the §4.5 table
        allows 5 ms for the T6/T5 pair.  The ceiling must not fall
        below that."""
        self.assertGreaterEqual(offset_judge.CROSS_BENCH_SIGMA_CEILING_MS, 5.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `python -m pytest tests/test_offset_judge_sigma_ceiling.py -v`
Expected: `AttributeError: module 'hf_timestd.core.offset_judge' has no attribute 'CROSS_BENCH_SIGMA_CEILING_MS'`.

- [ ] **Step 3: Add the ceiling**

At module scope in `offset_judge.py`, beside the other cross-bench constants:

```python
# Cap on any bench's sigma contribution to the cross-bench bound
# k*sqrt(sigma_c^2 + sigma_l^2).
#
# The gate arrived after the 2026-08-05 displaced-peak incident, in which
# "a biased-but-stable T6 was adopted over a healthy T5 because its
# honest wide sigma kept k*sigma quiet" -- and then reproduced that same
# polarity one level up: the bound grows with the candidate's own
# reported uncertainty, so a bench that has gone WRONG (which usually
# inflates sigma) buys itself a WIDER licence.  On DASI-009.AI6VN,
# 2026-09-18, a sigma of 477 ms opened the bound to ~2.4 s and a 284 ms
# disagreement passed unremarked.
#
# authority_manager met the mirror image and FLOORED its pair thresholds
# so a noisy witness could not mask a real disagreement.  This is the
# opposite bound: a ceiling, so a noisy candidate cannot excuse its own
# error.
#
# 5.0 ms matches the widest legitimate pair in METROLOGY.md §4.5 (T6/T5,
# where T5 over USB is bus-jitter floored), so no honest bench loses
# room it was entitled to.
CROSS_BENCH_SIGMA_CEILING_MS = 5.0
```

- [ ] **Step 4: Apply it in the comparison**

Find where the gate computes `cross_bench_k * sqrt(sigma_c**2 + sigma_l**2)`
and cap each sigma at the ceiling before squaring:

```python
        sc = min(sigma_c_ms, CROSS_BENCH_SIGMA_CEILING_MS)
        sl = min(sigma_l_ms, CROSS_BENCH_SIGMA_CEILING_MS)
        bound_ms = self.cross_bench_k * math.sqrt(sc * sc + sl * sl)
```

⛔ Cap only inside the cross-bench **bound**. Never alter a bench's published
sigma — that number is what the bench honestly claims, and this project
publishes honest uncertainty even when it is inconvenient.

⚠ Leave `sigma_regression_margin` alone. It answers a different question —
whether a tier upgrade may regress precision — and conflating them would make
a regression in either indistinguishable from the other.

- [ ] **Step 5: Run the judge tests**

Run: `python -m pytest tests/test_offset_judge_sigma_ceiling.py -k "" -v && python -m pytest tests/ -k "judge" -v`
Expected: PASS. The existing judge suite is large; a regression there matters
more than the new file passing.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/offset_judge.py tests/test_offset_judge_sigma_ceiling.py
git commit -m "judge: cap the cross-bench tolerance so a worse sigma buys nothing

The cross-bench bound k*sqrt(sigma_c^2 + sigma_l^2) grows with the
candidate's own reported uncertainty.  A bench that has gone wrong usually
inflates sigma, so the gate meant to catch it handed it a wider licence
instead.  At AI6VN on 2026-09-18 a sigma of 477 ms opened the bound to
~2.4 s and a 284 ms disagreement passed.

Cap each sigma's contribution at 5 ms -- the widest legitimate pair in
METROLOGY.md 4.5 -- inside the bound only.  Published sigmas stay honest.

authority_manager floored its pair thresholds against the mirror-image
failure; this is the ceiling that closes the other side.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Integration — the five behaviours this design exists to produce

Unit tests prove each piece. These prove the thing works.

**Files:**
- Test: `tests/test_t6_acceptance_integration.py` (create)
- Modify: nothing, unless a test finds a defect — then fix it here.

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: no new names.

- [ ] **Step 1: Write the tests**

Create `tests/test_t6_acceptance_integration.py`:

```python
"""The five behaviours T6_ACCEPTANCE_CRITERIA.md §7 names."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.t6_battery import T6Battery

SR = 96000
BATCH = 1920
K = 30
EDGE = 47916.1672


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _acquire(cn0_db_hz, *, seed=11, n_blocks=5, sigma_ms=0.001,
             chain_ns=16_618_000, carrier_freq_hz=0.0, edge=EDGE):
    """Drive the real stage into the real battery.  Returns verdicts."""
    stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    bat = T6Battery(sample_rate=SR, fold_seconds=K)
    sig = _make_bpsk_signal(
        duration_s=K * n_blocks + 1.0, sample_rate=SR,
        edge_offset_samples=edge, noise_std=_noise_std_for(cn0_db_hz),
        seed=seed, carrier_freq_hz=carrier_freq_hz,
    )
    out = []
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is None:
            continue
        out.append(bat.evaluate(est, implied_chain_delay_ns=chain_ns,
                                reported_sigma_ms=sigma_ms,
                                cn0_db_hz=cn0_db_hz))
    return out


class TestTheStationsWeRun(unittest.TestCase):

    def test_an_ai6vn_shaped_station_reaches_t6(self):
        """No T5, no T3, T4 at 0.29 ms, an injected pilot at 77 dB-Hz.
        Under the old gate this station sat at T2 while its edge landed
        on one sample position 128 seconds running."""
        verdicts = _acquire(77.0)
        self.assertTrue(verdicts, "no estimate at all")
        self.assertTrue(any(v.passed for v in verdicts),
                        f"{[v.failures for v in verdicts]}")

    @unittest.skipUnless(__import__("os").environ.get("T6_SWEEP"),
                         "slow: set T6_SWEEP=1")
    def test_a_b4_shaped_station_reaches_t6_at_its_worst_hour(self):
        """48.4 dB-Hz, measured 2026-08-28.  A criterion that only holds
        on an injected pilot ships a gate that fails every night."""
        verdicts = _acquire(48.4, n_blocks=6)
        self.assertTrue(verdicts, "no estimate at all")
        self.assertTrue(any(v.passed for v in verdicts),
                        f"{[v.failures for v in verdicts]}")


class TestTheFailuresWeRefuse(unittest.TestCase):

    def test_a_lattice_phantom_is_refused(self):
        """B4 locked onto a 20.000 ms lattice on 2026-09-04 and held it.
        A phantom passes every repeatability check, so the fixture is
        built from prominence and width -- the criteria that read shape."""
        from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        v = None
        for i in range(5):
            est = FineEdgeEstimate(
                edge_offset_samples=47916.0,
                edge_rtp=1_000_000 + 47916 + i * SR * K,
                edge_subsample=0.0, n_seconds_folded=K,
                plateau_amplitude=1.0, fit_rms=0.02,
                fold_retention=0.98, split_half_delta_samples=0.02,
                peak_prominence=1.3,             # no spike
                transition_width_samples=800.0,  # no transition
            )
            v = bat.evaluate(est, implied_chain_delay_ns=16_618_000,
                             reported_sigma_ms=0.001, cn0_db_hz=70.0)
        self.assertFalse(v.passed)
        self.assertIn("prominence", v.failures)

    def test_a_477_ms_sigma_is_refused(self):
        """The AI6VN reading of 2026-09-18, which widened the judge's
        bound to ~2.4 s and let a 284 ms disagreement through."""
        verdicts = _acquire(77.0, sigma_ms=477.0)
        self.assertTrue(verdicts)
        self.assertTrue(all("sigma" in v.failures for v in verdicts),
                        f"{[v.failures for v in verdicts]}")

    def test_a_missing_reference_cable_names_itself(self):
        """Two days of detector theory chased a missing coax.  The
        battery must say 'retention', not 'detector'."""
        verdicts = _acquire(77.0, carrier_freq_hz=1.0)
        if verdicts:
            self.assertTrue(any("retention" in v.failures for v in verdicts),
                            f"{[v.failures for v in verdicts]}")

    def test_a_gross_wrap_is_refused(self):
        verdicts = _acquire(77.0, chain_ns=400_000_000)
        self.assertTrue(verdicts)
        self.assertTrue(all("plausibility" in v.failures for v in verdicts))


class TestTheOrdinalIsNotAJudge(unittest.TestCase):

    def test_an_ordinal_two_seconds_wrong_alarms_without_moving_t6(self):
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
        from hf_timestd.core.native_anchor import NativeAnchor
        r = SimpleNamespace()
        r._t6_check_ordinal = CoreRecorderV2._t6_check_ordinal.__get__(r)
        r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
        r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
        r.T6_ORDINAL_DISAGREEMENT_ALARM_SEC = \
            CoreRecorderV2.T6_ORDINAL_DISAGREEMENT_ALARM_SEC
        r._t6_say_once_at = {}
        r._get_ordinal_reference = lambda: (2000.0, 20.0, "T2")
        anchor = NativeAnchor(
            anchor_rtp=47916, anchor_utc_ns=1_700_000_000_000_000_000,
            sample_rate_hz=SR, chain_delay_ns=16_618_000,
            captured_at_utc_ns=1_700_000_000_000_000_000,
            captured_via_tier="T2")
        r._t6_native_anchor = anchor
        r._t6_now_utc_ns = lambda: 1_700_000_000_000_000_000
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING"):
            r._t6_check_ordinal()
        self.assertIs(r._t6_native_anchor, anchor)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them**

Run: `T6_SWEEP=1 python -m pytest tests/test_t6_acceptance_integration.py -v`
Expected: PASS. A failure here names a real defect in Tasks 1–8 — fix it
there, not by loosening the test.

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest tests/ --override-ini addopts= -q`
Expected: the suite's established green baseline, with the new files added and
no pre-existing test newly red.

- [ ] **Step 4: Refresh the code graph**

Run: `graphify update /home/mjh/hamsci/repos`

⛔ Never `graphify update .` from `/home/mjh/hamsci` — that re-roots the graph
on the whole tree and clobbers the repos-rooted extraction.

- [ ] **Step 5: Commit**

```bash
git add tests/test_t6_acceptance_integration.py
git commit -m "t6: integration -- the five behaviours the design exists to produce

An AI6VN-shaped station reaches T6.  A B4-shaped station reaches T6 at its
48.4 dB-Hz worst hour.  A lattice phantom, a 477 ms sigma and a gross wrap
are refused.  An ordinal two seconds wrong alarms without moving T6.

And one the design owes rob: a missing reference cable reports itself as
'retention', not as a detector fault.  Two days of detector theory chased
a missing coax; the battery should say so in one line.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## After the plan

Station verification (spec §8) happens with the operator, never unattended:

1. **AI6VN first** — no antenna, no PPS, nothing above T4, so an acquisition
   there carries no hidden dependence on a peer. Look for
   `t_level_active = T6`, a σ consistent with criterion 5, and a residual that
   does not sit at a stable hundreds-of-ms value.
2. **B4 second** — compare 00–06Z before and after, with `rf_gain`,
   `if_power`, `t6_baseband_power` and `t6_n0` confirming comparable receiver
   conditions. Look for the ~270 daily tier transitions to fall.

⛔ Do not pin B4's gain to reach this. The AGC swings −4.2 to +27.5 dB against
real diurnal noise, and B4 never clips because of it.

Deploy by git fast-forward plus a restart of the `timestd-*` units, never
`install.sh`. The restart re-anchors the recorders, so schedule it. Announce
on the claude-bus before touching B4.
