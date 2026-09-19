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

⛔ Evidence the fold could not compute must never be evidence that the
fold is healthy.  In plain Python, ``nan < x`` and ``nan > x`` are both
False, so a bare threshold comparison silently PASSES a NaN — the
opposite of what a missing measurement should mean.  Every numeric
criterion in this module is routed through ``_fails``, the one place
that treats a non-finite value (NaN or +-inf) as an automatic FAILURE
before any threshold is even consulted.  Found 2026-09-19: a NaN
``fold_retention`` passed the battery outright.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Criterion names.  Logs, telemetry and tests use these strings verbatim.
RETENTION = "retention"
RULER = "ruler"
UNIMODALITY = "unimodality"
SHAPE = "shape"
SIGMA = "sigma"
SPLIT_HALF = "split_half"
PLAUSIBILITY = "plausibility"

ALL_CRITERIA = (RETENTION, RULER, UNIMODALITY, SHAPE, SIGMA,
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
    # Triangle-fidelity residual, carried on FineEdgeEstimate under the
    # legacy field name `peak_prominence`.  LOWER IS BETTER: it is an RMS
    # residual over the peak, not a ratio.  T(e) traces a triangle when
    # the folded second holds one clean polarity flip, so the residual
    # measures whether a flip is PRESENT, independent of its strength --
    # which is why it survives at 48.4 dB-Hz where a peak-over-background
    # ratio does not.  Measured 2026-09-19 on synthetic signal:
    #     pure noise          0.2547 - 0.4199
    #     real, 70 dB-Hz      0.00137
    #     real, 48.4 dB-Hz    0.00079 - 0.00171
    # 0.05 sits ~30x above the worst real case and ~5x below the best
    # noise case.  Task 3's sweep replaces it with a swept value.
    max_fidelity_residual: float = 0.05
    # Distance from T(e)'s apex to the reported edge, in samples.  This
    # is the statistic that addresses the 2026-09-04 B4 failure: a lock
    # at a 20.000 ms lattice position AWAY from the true apex, measured
    # at -1919 samples against 0.35-0.87 for an undisplaced edge.
    #
    # ⚠ It runs WEAK in the fine stage's 'bootstrap' mode, where the
    # search centre and the apex derive from the same fold and agreement
    # is near-tautological.  It is informative in 'seeded' and
    # 'tracking' mode, where an external coarse offset can place the
    # search away from the apex.  The battery applies it only in those
    # two modes -- see `evaluate`.
    max_apex_distance_samples: float = 100.0
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

    @staticmethod
    def _fails(value: float, predicate) -> bool:
        """The one rule every numeric criterion obeys.

        Returns True (the criterion FAILS) when ``value`` is non-finite
        (NaN or +-inf) or when ``predicate(value)`` is True.  A bare
        ``value < threshold`` or ``value > threshold`` lets NaN through
        silently in both directions -- this is the guard that closes
        that hole, in one place, so every criterion inherits it rather
        than re-deriving it.
        """
        if not math.isfinite(value):
            return True
        return bool(predicate(value))

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
                 cn0_db_hz: Optional[float],
                 search_mode: str = "bootstrap") -> BatteryVerdict:
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
        if self._fails(est.fold_retention,
                       lambda v: v < self.t.min_fold_retention):
            failures.append(RETENTION)

        # 4 — shape.  A phantom repeats perfectly, so only shape separates
        # it.  Three parts: triangle fidelity, apex agreement, width.
        #
        # ⚠ `peak_prominence` carries a RESIDUAL: lower is better.  The
        # field kept its name for contract stability when the statistic
        # changed; see T6_ACCEPTANCE_CRITERIA.md §4.2.
        fidelity = float(est.peak_prominence)
        criteria[SHAPE] = fidelity
        criteria["apex_distance_samples"] = float(est.apex_distance_samples)
        criteria["transition_width_samples"] = float(est.transition_width_samples)
        bad_shape = (
            self._fails(fidelity,
                       lambda v: v > self.t.max_fidelity_residual)
            or self._fails(
                est.transition_width_samples,
                lambda v: v <= 0.0 or v > self.t.max_transition_width_samples,
            )
        )
        # Apex agreement only where it carries information.  In bootstrap
        # mode the search centre and the apex come from the same fold, so
        # agreement proves nothing; in seeded/tracking mode an external
        # coarse offset can place the search away from the apex, which is
        # exactly the 2026-09-04 failure.
        if search_mode in ("seeded", "tracking"):
            if self._fails(
                est.apex_distance_samples,
                lambda v: abs(v) > self.t.max_apex_distance_samples,
            ):
                bad_shape = True
        if bad_shape:
            failures.append(SHAPE)

        # 6 — split-half agreement.  A wandering apex separates the halves.
        #
        # ⛔ NaN means a sub-fold held no samples -- NO EVIDENCE, which
        # must never read as agreement (spec §4.6, §5.2).  Fail on it.
        sh = float(est.split_half_delta_samples)
        criteria[SPLIT_HALF] = sh
        if self._fails(sh,
                       lambda v: abs(v) > self.t.max_split_half_delta_samples):
            failures.append(SPLIT_HALF)

        # 7 — physical plausibility of the implied chain delay.
        #
        # Cast to float rather than int: implied_chain_delay_ns is
        # documented as int, but `int(nan)` RAISES ValueError instead of
        # failing the criterion, which would let a NaN escape `evaluate`
        # as an exception rather than as a verdict.  float() never
        # raises on NaN, and `_fails` treats it as a failure like every
        # other non-finite input.
        chain_delay = float(implied_chain_delay_ns)
        criteria[PLAUSIBILITY] = chain_delay
        if self._fails(chain_delay,
                       lambda v: abs(v) > self.t.max_chain_delay_ns):
            failures.append(PLAUSIBILITY)

        # 5 — sigma inside the tier's physical budget.
        criteria[SIGMA] = float(reported_sigma_ms)
        predicted = self._predicted_sigma_ms(cn0_db_hz)
        ceiling = (predicted * self.t.sigma_margin if predicted is not None
                   else self.t.max_sigma_ms_without_cn0)
        criteria["sigma_ceiling_ms"] = float(ceiling)
        if self._fails(reported_sigma_ms, lambda v: v > ceiling):
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
            #
            # Structurally safe against the NaN leak: `self._edges` holds
            # only `int(est.edge_rtp)` (appended above, before any of
            # this method's own checks run), and edge_rtp is populated
            # exclusively by the fine stage's `int(round(edge_rtp_float))`
            # -- a block that could not compute a position returns None
            # instead of a NaN estimate, so no NaN ever reaches this
            # list.  All arithmetic here is therefore over real ints and
            # a bare `>` cannot silently pass bad evidence.
            expected = self.fold_seconds * p
            worst = max(abs((self._edges[i] - self._edges[i - 1]) - expected)
                        for i in range(1, len(self._edges)))
            criteria[RULER] = float(worst)
            if self._fails(worst,
                           lambda v: v > self.t.max_ruler_error_samples):
                failures.append(RULER)

            # 3 — unimodality.  Positions about their median.
            #
            # A NaN anywhere in `_positions` must not be laundered away
            # by `max()`: Python's max() keeps whichever value it saw
            # first that no later value beat, and `x > nan` is always
            # False, so a NaN can end up either returned as the "worst"
            # spread (if seen first) or silently skipped entirely (if
            # seen later) depending on list order alone -- either way it
            # is not a measurement of spread.  Check every difference for
            # finiteness explicitly instead of trusting max() to surface
            # a non-finite input.
            med = sorted(self._positions)[len(self._positions) // 2]
            diffs = [abs(self._wrapped(x - med)) for x in self._positions]
            if any(not math.isfinite(d) for d in diffs):
                spread_ms = float("nan")
            else:
                spread_ms = max(diffs) / p * 1000.0
            criteria[UNIMODALITY] = float(spread_ms)
            if self._fails(
                spread_ms, lambda v: v > self.t.max_unimodality_spread_ms
            ):
                failures.append(UNIMODALITY)

        ordered = tuple(c for c in ALL_CRITERIA if c in failures)
        return BatteryVerdict(
            passed=not ordered,
            failures=ordered,
            sigma_ms=float(reported_sigma_ms),
            criteria=criteria,
        )
