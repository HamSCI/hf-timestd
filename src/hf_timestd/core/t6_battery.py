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

⛔ Criterion 5 (sigma) guards GROSS BREAKAGE only -- orders of
magnitude, never a factor of two.  It compares the tier's own published
uncertainty against a model of the FOLD's scatter, and those are not the
same measurement, so an absolute floor sits under the ceiling to stop it
refusing the healthy stations it exists to serve.  See
SIGMA_CEILING_FLOOR_MS and the note beside `sigma_margin`.

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
from dataclasses import dataclass
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

# Absolute floor under criterion 5's sigma ceiling, in ms.
#
# ⛔ WHY A FLOOR AT ALL.  The ceiling compares two DIFFERENT quantities.
# `reported_sigma_ms` is the tier's OWN published uncertainty, computed
# by the authority from its own statistics; `_predicted_sigma_ms` models
# the FOLD's block-to-block scatter.  They are not the same measurement,
# and no choice of anchor constant makes comparing them sound -- so the
# criterion must not be allowed to police the difference between them.
#
# The numbers say an unfloored ceiling misfires on a real station.  B4's
# recorded hourly `t6_sigma_ms` runs 0.003-0.07 ms.  At a plausible
# daytime 57 dB-Hz the pre-floor ceiling computes to about 0.017 ms, so
# a healthy B4 would be REFUSED -- precisely the failure this whole
# design exists to end.  Shipping that would be worse than shipping no
# sigma criterion at all.
#
# 1.0 ms clears B4's worst recorded hour (0.07 ms) by about 14x, while
# the case the criterion was written for -- 477 ms at DASI-009.AI6VN on
# 2026-09-18 -- still fails by 477x.  The floor dominates the curve from
# 77 dB-Hz down to about 36 dB-Hz; below that the 1/sqrt(SNR) term takes
# over and widens the ceiling further, which is the intended behaviour
# for a channel genuinely that bad.
SIGMA_CEILING_FLOOR_MS = 1.0


@dataclass(frozen=True)
class BatteryThresholds:
    """Every acceptance threshold, with its derivation.

    Derived 2026-09-19 from a C/N0 sweep, not from two stations on one
    evening.  The sweep drives the REAL fine stage and the REAL battery
    over synthetic band-limited BPSK at C/N0 in {77, 66, 58, 52, 48.4,
    44} dB-Hz, six seeds each, six 30 s fold blocks each, plus 32 seeds
    of PURE complex Gaussian noise with no signal at all (120 settled
    noise blocks).  Every figure quoted below is a settled-block range
    from that sweep; ``tests/test_t6_battery_thresholds.py`` re-derives
    it under T6_SWEEP=1.

    ⛔ The governing case is 48.4 dB-Hz -- B4's measured worst hour of
    2026-08-28 -- NOT AI6VN's 77 dB-Hz injected pilot.  A threshold set
    at 77 and never checked at 48.4 ships a gate that fails every night
    at exactly the hours the folded-acquisition design is judged on.
    Every threshold here passes 48.4 across all six seeds.

    ⛔ And every threshold was checked against the NULL.  Task 1 shipped
    two prominence statistics that passed healthy-signal checks while
    being unable to tell a real edge from pure noise at 48.4 dB-Hz; the
    failure stayed invisible until someone drove noise through the
    stage.  So each field below carries its pure-noise reading, and
    where a criterion does NOT separate the two populations, it says so
    rather than implying a discrimination it cannot perform.
    """
    # Coherence of the reference chain.  Folded amplitude over mean
    # instantaneous amplitude: noise does not fold coherently, so this
    # falls with C/N0 as well as with incoherence.  The two populations
    # sit far apart, so the threshold's exact value matters little --
    # only that it lands between them.
    #     pure noise        0.18180 - 0.18323  (120 blocks, 32 seeds)
    #     real, 77 dB-Hz    0.99954
    #     real, 48.4 dB-Hz  0.72729 - 0.72840
    #     real, 44 dB-Hz    0.52797 - 0.52968
    # 0.50 sits 1.45x below the worst healthy reading at the governing
    # 48.4 dB-Hz and 2.73x above the best pure-noise reading.  It may
    # not go HIGHER: the 44 dB-Hz column shows this criterion sets T6's
    # effective C/N0 floor near 43.7 dB-Hz in this model, so raising the
    # threshold raises that floor into the band B4 actually works in.
    #
    # ⛔ B4's worst recorded hour of 48.4 dB-Hz holds only 4.7 dB above
    # that floor, and retention falls steeply through the band (0.9995
    # at 77, 0.727 at 48.4, 0.528 at 44).  Do not raise this threshold
    # without re-measuring B4 first.  Whoever raises it is deciding, in
    # effect, which of B4's hours T6 is allowed to serve.
    # (AI6VN measured 0.006 on captured IQ with the TS-1's REF IN on its
    # internal 10 MHz, and 0.9998 once fed from the governing GPSDO.)
    min_fold_retention: float = 0.50
    # Consecutive block edges must differ by fold_seconds * sample_rate.
    #     pure noise        196 - 2,961,880 samples  (nearest 196)
    #     real, 77 dB-Hz    0
    #     real, 48.4 dB-Hz  0 - 1
    # A pulse source locked to the ADC lands EXACTLY one fold apart; the
    # 0-1 sample spread at 48.4 dB-Hz is rounding of the sub-sample fit,
    # not walk.  2 samples (21 us) leaves one sample of headroom over
    # the worst healthy reading and still refuses the nearest noise
    # block by 98x.  This is the widest-separating criterion in the
    # battery after split-half.
    max_ruler_error_samples: float = 2.0
    # Consecutive block positions about their median, in ms.
    #     pure noise        2.049 - 492.98 ms  (nearest 2.049)
    #     real, 77 dB-Hz    0.00007 - 0.00026 ms
    #     real, 48.4 dB-Hz  0.00047 - 0.00565 ms
    #     real, 44 dB-Hz    up to 0.00817 ms
    # The placement is deliberately ASYMMETRIC.  Refusing a healthy
    # station is the failure this whole task exists to prevent, while
    # admitting noise here costs nothing -- noise is refused by four
    # other criteria besides this one.  So 0.25 ms takes 44x of headroom
    # over the worst healthy reading at 48.4 dB-Hz and leaves 8.2x to
    # the nearest noise block, rather than splitting the gap evenly.
    # Still a 4x tightening on the fine stage's own
    # BOOTSTRAP_CONFIRM_TOLERANCE_MS of 1.0 ms: the battery should be a
    # stricter gate than the stage's bootstrap confirmation, not an
    # echo of it.
    max_unimodality_spread_ms: float = 0.25
    # Triangle-fidelity residual, carried on FineEdgeEstimate under the
    # legacy field name `peak_prominence`.  LOWER IS BETTER: it is an RMS
    # residual over the peak, not a ratio.  T(e) traces a triangle when
    # the folded second holds one clean polarity flip, so the residual
    # measures whether a flip is PRESENT, independent of its strength --
    # which is why it survives at 48.4 dB-Hz where a peak-over-background
    # ratio does not.
    #
    # ⛔ Re-derived 2026-09-19 after the fit moved from |T| to the SIGNED
    # T (see `_compute_estimate`).  Under the |T| fit this residual was a
    # function of WHERE IN THE FOLD the edge landed -- an arbitrary
    # per-boot quantity -- and the old numbers below were all taken at the
    # one position, 47916, where the defect is invisible.  The sweep now
    # runs 9 fold positions x 6 C/N0 x 3 seeds, and the signed residual no
    # longer varies with position at all:
    #
    #     pure noise        0.1115 - 0.6760  (nearest 0.1115, 90 blocks,
    #                                         32 seeds)
    #     real, 77 dB-Hz    0.0000081 - 0.000031
    #     real, 58 dB-Hz    0.000060  - 0.00014
    #     real, 48.4 dB-Hz  0.00017   - 0.00039
    #     real, 44 dB-Hz    0.00029   - 0.00064
    #
    # Across the nine positions the worst healthy reading at 48.4 dB-Hz
    # moves only 0.00031 -> 0.00039, a 1.3x spread against the 0.0014 ->
    # 0.5683 (410x) spread the |T| fit produced.
    #
    # 0.008 is the geometric middle of the worst healthy reading at
    # 44 dB-Hz (0.00064) and the nearest pure-noise block (0.1115):
    # 20.5x above the worst at the governing 48.4 dB-Hz, 12.4x above the
    # worst at 44, and 13.9x below the lowest of 90 noise blocks.  A ~2x
    # tightening on the old 0.015, which the signed fit's two-orders-of-
    # magnitude drop in the healthy population pays for.
    # ⚠ The noise tail has moved before with seed count (0.2547 -> 0.1016
    # going from a handful of seeds to 32).  Do not raise this without
    # re-running the null, and do not re-derive it at one fold position.
    max_fidelity_residual: float = 0.008
    # Distance from T(e)'s apex to the reported edge, in samples.  This
    # is the statistic that addresses the 2026-09-04 B4 failure: a lock
    # at a 20.000 ms lattice position AWAY from the true apex, measured
    # at -1919 samples against 0.35-0.87 for an undisplaced edge.
    #
    # ⛔ It does NOT separate signal from noise and is not meant to.  It
    # is a DISPLACEMENT check:
    #     pure noise        |apex| 0.021 - 4.587   <- overlaps healthy
    #     real, 48.4 dB-Hz  |apex| 0.005 - 1.311   (same in seeded mode)
    #     20 ms lattice lock       -1919
    # Which is why the battery scores it only in 'seeded' and 'tracking'
    # mode, where an external coarse offset can place the search away
    # from the apex -- see `evaluate`.  50 samples (0.52 ms) is the
    # geometric middle of the healthy range and the lattice lock: 38x
    # above the worst healthy reading at 48.4 dB-Hz, 38x below the
    # displacement it exists to catch, and well inside the stage's
    # +-6 ms (576 sample) search window, so any lattice capture the
    # window can reach is caught.
    max_apex_distance_samples: float = 50.0
    # A +-25 kHz channel filter predicts a transition 1/(2B) = 20 us
    # wide, about 2 samples at 96 kHz.
    #
    # ⛔ This one does NOT separate either, and the sweep says so
    # plainly:
    #     pure noise        1 - 5 samples
    #     real, 77 dB-Hz    1
    #     real, 48.4 dB-Hz  1 - 2
    #     real, 44 dB-Hz    1 - 3
    # It stays in the battery as a PHYSICS BOUND, not a discriminator:
    # the discriminating case is a phantom, which carries no transition
    # at all and reads far wider than any real channel filter can
    # produce.  30 samples (312 us) bounds a transition wider than a
    # 1.6 kHz channel could make -- 10x the widest healthy reading and
    # 6x the widest noise reading, so it fires on nothing the sweep
    # produced and only on a fit with no transition in it.
    max_transition_width_samples: float = 30.0
    # Even-second sub-fold position minus odd-second, in samples.  A
    # wandering apex separates the halves; a stable one does not.
    # Re-measured 2026-09-20, after the field gained sub-sample
    # resolution (it previously reported whole samples only, so every
    # healthy reading rounded to 0 and the criterion could not see a
    # wander below half a sample):
    #     pure noise        |delta| 765 - 39190  (nearest 765)
    #     real, 77 dB-Hz    max 0.051
    #     real, 60 dB-Hz    max 0.348
    #     real, 48.4 dB-Hz  max 1.128
    #     real, 44 dB-Hz    max 7.375
    # Captured B4 signal read 0.028 - 0.308 across six live blocks,
    # against sqrt(2) x the block-to-block scatter predicted
    # (T6_NEWELL_VS_FOLD.md §5d).
    # 10 samples = 104 us still clears the worst healthy reading, at
    # 44 dB-Hz, and sits 76x below the nearest of the noise blocks, so
    # this stays the battery's strongest single refusal of the null.
    #
    # ⚠ The bound is set by the healthy spread at low C/N0, NOT by the
    # sample grid, so a wander smaller than that spread still passes.
    # What changed is that the reported VALUE now carries it: an apex
    # drifting a tenth of a sample shows up in the number an operator
    # can trend, where before it was pinned at exactly 0.0000.
    max_split_half_delta_samples: float = 10.0
    # Implied chain delay.  Not a swept quantity -- it arrives as an
    # argument to `evaluate`, so the sweep can say nothing about it and
    # this criterion separates nothing on its own.  Already enforced in
    # the recorder as T6_PHYSICAL_CHAIN_DELAY_MAX_NS; restated here so
    # the battery owns a complete verdict.
    max_chain_delay_ns: int = 250_000_000
    # Sigma ceiling (§4.3): the reported sigma may exceed the physical
    # prediction by at most this factor, subject to the absolute
    # SIGMA_CEILING_FLOOR_MS beneath it.  A T6 reporting 477 ms has not
    # produced a wide T6, it has produced a broken one.
    #
    # ⛔ WHAT THIS CRITERION CAN AND CANNOT CLAIM.  It guards GROSS
    # BREAKAGE only -- orders of magnitude, never a factor of two.  It
    # cannot police precision, because the two sides of the comparison
    # are not the same measurement: `reported_sigma_ms` is the tier's own
    # published uncertainty and `_predicted_sigma_ms` is a model of the
    # fold's block-to-block scatter.  Tightening it would need a station
    # measurement of what healthy tiers actually REPORT -- not what the
    # fold scatters -- which is out of scope here and belongs to
    # on-station verification.  Until that measurement exists, read a
    # pass on criterion 5 as "not obviously broken", never as "precise".
    #
    # ⛔ Like plausibility, the margin itself cannot be swept:
    # `reported_sigma_ms` is an input to `evaluate`, so pure noise and a
    # healthy pilot read whatever the caller hands over.  What the sweep
    # CAN check is the prediction curve the ceiling rides on, and it did
    # -- see the re-anchoring note in `_predicted_sigma_ms`.  With the
    # curve anchored to what this stage actually produces, the 100x
    # margin is no longer absorbing a 5x modelling error; it is real
    # headroom over a curve that tracks the measurement to within 1.3x
    # across 33 dB.
    sigma_margin: float = 100.0
    # Fallback ceiling when no C/N0 reading accompanies the estimate.
    # 1 ms is 320x the 3.1 us of per-block scatter measured at 44 dB-Hz,
    # the worst channel in the sweep, so it refuses only the grossly
    # broken.
    max_sigma_ms_without_cn0: float = 1.0


@dataclass(frozen=True)
class BatteryVerdict:
    passed: bool
    failures: tuple[str, ...]
    sigma_ms: float
    criteria: dict


def is_still_accumulating(verdict) -> bool:
    """True when a verdict fails ONLY on criteria that cannot yet report.

    ⛔ "STILL ACCUMULATING" IS NOT "SUSPECT".  Criteria 2 and 3 refuse to
    report until ``HISTORY_REQUIRED_BLOCKS`` blocks are in hand, and the
    acquisition path already says so in as many words.  The suspect path
    did not: because the authority captures its anchor on block 1, every
    cold start emitted ``T6 SUSPECT: ruler, unimodality`` and stamped
    ``t6_suspect_criteria`` into the authority snapshot for two blocks --
    a fault report for a battery doing exactly what it is supposed to do.
    An alarm that fires on every healthy start teaches the operator to
    ignore alarms.

    Criterion 5 joins the excused set only while its evidence is
    UNAVAILABLE (non-finite).  Below two retained positions the fold
    cannot state its own scatter, which is absence of evidence, not
    evidence of breakage.  A sigma that is finite and over the ceiling is
    a real fault and marks suspect at once, accumulating or not -- as
    does every other criterion.
    """
    if not verdict.failures:
        return False
    if verdict.criteria.get("blocks", 0) >= HISTORY_REQUIRED_BLOCKS:
        return False
    excused = {RULER, UNIMODALITY}
    if not math.isfinite(verdict.criteria.get(SIGMA, float("nan"))):
        excused.add(SIGMA)
    return all(f in excused for f in verdict.failures)


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

        Scatter follows 1/sqrt(SNR), so it doubles for every 6 dB lost,
        and the fold improves it by sqrt(K).

        ⚠ RE-ANCHORED 2026-09-19.  The constant was 95 ns, taken from
        §8c's real-IQ per-second measurement at 77 dB-Hz on an injected
        pilot.  The C/N0 sweep then measured this stage's own
        block-to-block position scatter and found it 4.0-5.2x WIDER than
        that anchor predicted, uniformly across 33 dB:

            C/N0   measured/block   old prediction   ratio
            77      0.090 us         0.017 us        5.19x
            66      0.321 us         0.062 us        5.22x
            58      0.751 us         0.155 us        4.86x
            52      1.376 us         0.308 us        4.46x
            48.4    2.095 us         0.467 us        4.49x
            44      3.118 us         0.775 us        4.02x

        The 1/sqrt(SNR) SHAPE held -- the ratio is flat -- so the law is
        right and only the constant was wrong.  Solving each row for the
        anchor gives 493, 496, 462, 424, 426 and 382 ns; 490 ns takes the
        77 and 66 dB-Hz points essentially exactly (0.99x) and
        over-predicts by at most 1.28x at 44 dB-Hz, which is the safe
        direction for a ceiling.

        ⛔ Code must be anchored to what it actually produces.  The old
        constant described a different measurement -- real IQ through a
        different path -- and leaving it in place meant a 100x margin was
        silently spending 5x of itself covering a modelling error rather
        than covering real variation.  Re-measure this curve, on this
        stage, before changing the constant again.

        At B4's measured 48.4 dB-Hz worst hour this now predicts roughly
        13.2 us per second and 2.4 us per 30 s block.
        """
        if cn0_db_hz is None:
            return None
        REF_CN0_DB_HZ = 77.0
        REF_SIGMA_NS = 490.0
        per_second_ns = REF_SIGMA_NS * 10 ** ((REF_CN0_DB_HZ - cn0_db_hz) / 20.0)
        per_block_ns = per_second_ns / math.sqrt(self.fold_seconds)
        return per_block_ns / 1e6

    def block_position_sigma_ms(self) -> Optional[float]:
        """Standard deviation of the retained block positions, in ms.

        ⛔ WHAT THIS QUANTITY IS: the FOLD'S OWN REPEATABILITY -- how far
        the fine stage's edge position moves from one fold block to the
        next.  It is NOT the tier's published uncertainty.  The authority
        publishes `t6_sigma_ms` from the chain-delay history, a different
        statistic over a different population, and this number must never
        be presented or logged as that one.

        ⛔ WHY IT EXISTS.  Criterion 5 used to read only the recorder's
        chain-delay history, and that deque is appended to ONLY after an
        anchor exists.  Before acquisition it is empty, so the sigma was
        None, became NaN, and failed criterion 5 through `_fails`: the
        battery needed an anchor and the anchor needed the battery.
        Measured 2026-09-19 -- three healthy blocks with an empty deque
        gave `failures=('sigma',), sigma=nan` and no anchor, forever.

        This breaks that circle by taking the sigma from evidence the
        fold already holds.  §4.3 of T6_ACCEPTANCE_CRITERIA.md already
        records that criterion 5 compares two different quantities and
        guards gross breakage only; reading the fold's own scatter makes
        that comparison SELF-CONSISTENT (a model of block scatter against
        a measurement of block scatter) rather than circular.  It does
        not make the criterion a precision check, and it does not make
        this the number the authority publishes.

        Positions are fold-domain offsets in [0, p), so differences are
        taken about the median through `_wrapped` -- otherwise an edge
        sitting near the fold origin would read a full period of scatter
        from sample-level jitter alone.

        None below two positions: the spread of one reading is not a
        small uncertainty, it is no uncertainty at all.
        """
        pos = [x for x in self._positions if math.isfinite(x)]
        if len(pos) != len(self._positions) or len(pos) < 2:
            return None
        med = sorted(pos)[len(pos) // 2]
        devs = [self._wrapped(x - med) for x in pos]
        mean = sum(devs) / len(devs)
        var = sum((d - mean) ** 2 for d in devs) / (len(devs) - 1)
        return math.sqrt(var) / self.sample_rate * 1000.0

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
        # ⛔ The floor, not the curve, governs the whole working band.
        # An unfloored ceiling computes to ~0.017 ms at 57 dB-Hz, which
        # would REFUSE a healthy B4 whose recorded hourly sigma runs
        # 0.003-0.07 ms.  See SIGMA_CEILING_FLOOR_MS: this criterion
        # guards gross breakage, and a bound that refuses the station it
        # was written to serve guards nothing.
        ceiling = (max(predicted * self.t.sigma_margin,
                       SIGMA_CEILING_FLOOR_MS) if predicted is not None
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
            # ⛔ A MISSING BLOCK IS NOT A RULER FAULT.  The fine stage
            # returns None for a block whose registration spread exceeds
            # its limit, so the battery is never called for that block
            # and the next `evaluate` sees a gap of TWO folds.  Measuring
            # that against one fold reports an error of a whole fold
            # period -- 2,880,000 samples at K=30, 96 kHz -- and because
            # `worst` runs over the whole retained history (keep=8), ONE
            # discarded block marked a running T6 suspect for about seven
            # further evaluations, roughly 3.5 minutes.
            #
            # What this criterion exists to catch is the PULSE SOURCE
            # WALKING AGAINST THE ADC, and a walk shows up as a gap that
            # is not a whole number of folds.  So measure each gap
            # against its OWN nearest integer multiple of the fold
            # period.  A dropped block leaves the residual at zero; a
            # walking source leaves it wherever the walk put it.
            #
            # This does not blunt the criterion.  The fold period is
            # 2,880,000 samples and the tolerance is 2, so an arbitrary
            # gap lands within tolerance of SOME multiple with
            # probability 5/2,880,000 -- about 1.7e-6.  An outage long
            # enough to make the multiple large is a liveness problem the
            # authority already owns (and `_t6_battery_forget_run` drops
            # the history on repudiation), not something this criterion
            # should re-report as a walk.
            expected = self.fold_seconds * p
            worst = 0.0
            skipped = 0
            for i in range(1, len(self._edges)):
                gap = self._edges[i] - self._edges[i - 1]
                # A gap shorter than half a fold cannot be a dropped
                # block; hold the multiple at 1 so a too-SHORT gap is
                # still measured against one fold and still fails.
                folds = max(1, int(round(gap / expected)))
                worst = max(worst, abs(gap - folds * expected))
                skipped = max(skipped, folds - 1)
            criteria[RULER] = float(worst)
            criteria["ruler_folds_skipped"] = int(skipped)
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
