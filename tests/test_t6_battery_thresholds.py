"""The thresholds must pass a healthy station at B4's worst hour.

And -- the harder half -- they must REFUSE pure noise, by a criterion we
can name.  Task 1 went through three statistics for one criterion: the
first two both passed healthy-signal checks while being unable to tell a
real edge from complex Gaussian noise at 48.4 dB-Hz, and the failure
stayed invisible until someone drove the null through the stage and
compared.  So every threshold in ``BatteryThresholds`` is derived here
from a C/N0 sweep with a matching pure-noise arm, and the numbers written
beside each field in that dataclass are the numbers this file measures.

The slow arms sit behind T6_SWEEP=1 so the ordinary suite stays fast:

    T6_SWEEP=1 python -m pytest tests/test_t6_battery_thresholds.py -v
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.t6_battery import (
    T6Battery, BatteryThresholds, SIGMA_CEILING_FLOOR_MS)

SR = 96000
BATCH = 1920
K = 30
EDGE = 47916.1672

# B4's measured worst hour, 2026-08-28.  AI6VN's injected pilot reads 77.
B4_WORST_CN0 = 48.4

# Every C/N0 the sweep drives.  The sigma ceiling must behave at all of
# them, not merely at the governing case.
SWEPT_CN0 = (77.0, 66.0, 58.0, 52.0, B4_WORST_CN0, 44.0)

# B4's recorded hourly `t6_sigma_ms` runs 0.003 - 0.07 ms.  The high end
# is what an unfloored ceiling would have refused.
B4_WORST_REPORTED_SIGMA_MS = 0.07

SWEEP = unittest.skipUnless(os.environ.get("T6_SWEEP"), "slow: set T6_SWEEP=1")


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _run_blocks(cn0_db_hz: float, n_blocks: int, seed: int,
                *, search_mode: str = "bootstrap"):
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
            reported_sigma_ms=0.001, cn0_db_hz=cn0_db_hz,
            search_mode=search_mode))
    return verdicts


def _run_noise_blocks(n_blocks: int, seed: int):
    """The null: complex Gaussian noise, no signal at all."""
    stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    bat = T6Battery(sample_rate=SR, fold_seconds=K)
    rng = np.random.default_rng(seed)
    n = SR * (K * n_blocks + 1)
    sig = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)).astype(np.complex64)
    verdicts = []
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is None:
            continue
        verdicts.append(bat.evaluate(
            est, implied_chain_delay_ns=16_618_000,
            reported_sigma_ms=0.001, cn0_db_hz=B4_WORST_CN0))
    return verdicts


class TestThresholdsAtB4WorstHour(unittest.TestCase):

    @SWEEP
    def test_a_healthy_station_passes_at_48_4_db_hz(self):
        """The hours we are judged on.  Several seeds, because the
        cliff near 58-59 dB-Hz is stochastic -- one seed passing proves
        nothing about the next."""
        for seed in (5, 11, 23, 37, 53, 71):
            with self.subTest(seed=seed):
                verdicts = _run_blocks(B4_WORST_CN0, 6, seed)
                self.assertTrue(verdicts, "no estimate at all")
                settled = [v for v in verdicts[2:]]
                self.assertTrue(settled)
                self.assertTrue(
                    all(v.passed for v in settled),
                    f"a settled block failed: {[v.failures for v in settled]}")

    @SWEEP
    def test_every_swept_cn0_passes_every_criterion(self):
        """The whole sweep the thresholds were derived from, re-run.
        44 dB-Hz is below the governing case and carries no promise, but
        it is what fixes how far `min_fold_retention` may be raised, so
        it is measured rather than assumed."""
        for cn0 in (77.0, 66.0, 58.0, 52.0, 48.4, 44.0):
            for seed in (11, 23, 37):
                with self.subTest(cn0=cn0, seed=seed):
                    settled = _run_blocks(cn0, 6, seed)[2:]
                    self.assertTrue(settled)
                    self.assertTrue(
                        all(v.passed for v in settled),
                        f"{cn0} dB-Hz seed {seed}: "
                        f"{[v.failures for v in settled]}")

    @SWEEP
    def test_seeded_mode_passes_too_so_apex_does_not_refuse_the_healthy(self):
        """`max_apex_distance_samples` is scored ONLY in seeded and
        tracking mode, so bootstrap runs never exercise it.  A healthy
        station at the governing C/N0 must clear it there as well."""
        for seed in (11, 23, 37):
            with self.subTest(seed=seed):
                settled = _run_blocks(B4_WORST_CN0, 6, seed,
                                      search_mode="seeded")[2:]
                self.assertTrue(settled)
                self.assertTrue(all(v.passed for v in settled),
                                f"{[v.failures for v in settled]}")

    def test_a_healthy_station_passes_on_an_injected_pilot(self):
        """AI6VN's 77 dB-Hz.  Fast enough to run every time."""
        verdicts = _run_blocks(77.0, 5, seed=11)
        self.assertTrue(verdicts)
        self.assertTrue(any(v.passed for v in verdicts[2:]),
                        f"{[v.failures for v in verdicts[2:]]}")


class TestTheNullIsRefused(unittest.TestCase):

    def test_pure_noise_does_not_pass_the_battery(self):
        """A gate validated only on healthy signal is not known to refuse
        anything.  Task 1's first two prominence candidates both passed
        this kind of check and still could not tell noise from a real
        edge at 48.4 dB-Hz."""
        verdicts = _run_noise_blocks(5, seed=11)
        self.assertTrue(verdicts, "the null produced no estimate to judge")
        self.assertFalse(any(v.passed for v in verdicts),
                         f"noise passed: {[v.criteria for v in verdicts]}")

    def test_the_refusing_criteria_are_named_not_merely_counted(self):
        """'Noise failed' is not a finding until we can say WHICH
        criterion refused it.  Five of the seven do, and three of those
        need no block history at all, so the null is refused on the very
        first settled block rather than after a run."""
        verdicts = _run_noise_blocks(5, seed=11)
        settled = verdicts[2:]
        self.assertTrue(settled)
        for v in settled:
            for name in ("retention", "shape", "split_half",
                         "ruler", "unimodality"):
                self.assertIn(name, v.failures,
                              f"{name} did not refuse the null: {v.criteria}")

    @SWEEP
    def test_the_null_is_refused_across_many_seeds_not_one(self):
        """The detection cliff is stochastic; so is the null's worst
        case.  The fidelity residual's noise floor moved from 0.2547 to
        0.1016 when the seed count went from a handful to 32, which is
        why `max_fidelity_residual` came down from 0.05.  The signed-T
        re-derivation of 2026-09-19 re-measured that floor at 0.1115 over
        90 blocks and set the bound to 0.008."""
        for seed in range(101, 113):
            with self.subTest(seed=seed):
                verdicts = _run_noise_blocks(5, seed)
                self.assertTrue(verdicts)
                self.assertFalse(any(v.passed for v in verdicts),
                                 f"noise seed {seed} passed")


class TestTheRefusalIsLoadBearing(unittest.TestCase):
    """The test of the test.  A criterion nobody watched FAIL is not yet
    a criterion, and a threshold nobody watched ADMIT the thing it is
    supposed to refuse is not yet a threshold."""

    def test_loosening_the_five_separating_bounds_lets_the_null_through(self):
        """Move every bound that separates the two populations past the
        noise side, and the same noise that the shipped defaults refuse
        five ways over now passes outright.  That is what makes the
        shipped values load-bearing rather than decorative.

        ⚠ Measured 2026-09-19, and worth knowing: NO SINGLE loosening
        admits the null.  Retention, ruler, unimodality, shape and
        split-half each refuse pure noise on their own, so the battery's
        refusal of the null is fivefold redundant and no one threshold
        is the gate.  The corollary is that a single criterion drifting
        loose would not show up here -- which is why each bound also
        carries its own two-sided assertion below."""
        loose = BatteryThresholds(
            min_fold_retention=0.1,
            max_ruler_error_samples=3_000_000.0,
            max_unimodality_spread_ms=600.0,
            max_fidelity_residual=0.8,
            max_split_half_delta_samples=60_000.0,
        )
        stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
        bat = T6Battery(sample_rate=SR, fold_seconds=K, thresholds=loose)
        rng = np.random.default_rng(11)
        n = SR * (K * 5 + 1)
        sig = (rng.normal(0, 1, n)
               + 1j * rng.normal(0, 1, n)).astype(np.complex64)
        verdicts = []
        for i in range(0, len(sig), BATCH):
            est = stage.process_samples(sig[i:i + BATCH], i)
            if est is None:
                continue
            verdicts.append(bat.evaluate(
                est, implied_chain_delay_ns=16_618_000,
                reported_sigma_ms=0.001, cn0_db_hz=B4_WORST_CN0))
        self.assertTrue(any(v.passed for v in verdicts),
                        "the loosened battery still refused the null, so the "
                        "shipped thresholds are not what refuses it")


class TestTheThresholdsSitBetweenTheMeasuredPopulations(unittest.TestCase):
    """Each bound, checked against the sweep's two populations.

    These guard the derivations written beside the fields: if someone
    edits a default without re-running the sweep, the side it falls on
    is asserted here rather than left to a comment.  The numbers are the
    2026-09-19 sweep's settled-block extremes (6 seeds of signal per
    C/N0, 32 seeds of noise, 120 settled noise blocks)."""

    t = BatteryThresholds()

    # worst healthy reading at 48.4 dB-Hz, and the nearest noise reading
    HEALTHY_48_4 = dict(retention=0.72729, ruler=1.0, unimodality_ms=0.005646,
                        fidelity=0.00039, split_half=2.0,
                        apex=1.311, transition=2.0)
    # 44 dB-Hz sits below the governing case and carries no promise,
    # but it is the worst healthy fidelity reading the sweep produced.
    HEALTHY_44 = dict(fidelity=0.000644)
    NOISE_NEAREST = dict(retention=0.183225, ruler=196.0, unimodality_ms=2.04934,
                         fidelity=0.11150, split_half=1308.0)

    def test_retention_admits_48_4_and_refuses_the_null(self):
        self.assertLess(self.t.min_fold_retention, self.HEALTHY_48_4["retention"])
        self.assertGreater(self.t.min_fold_retention,
                           self.NOISE_NEAREST["retention"])

    def test_ruler_admits_48_4_and_refuses_the_null(self):
        self.assertGreaterEqual(self.t.max_ruler_error_samples,
                                self.HEALTHY_48_4["ruler"])
        self.assertLess(self.t.max_ruler_error_samples,
                        self.NOISE_NEAREST["ruler"])

    def test_unimodality_admits_48_4_and_refuses_the_null(self):
        self.assertGreater(self.t.max_unimodality_spread_ms,
                           self.HEALTHY_48_4["unimodality_ms"])
        self.assertLess(self.t.max_unimodality_spread_ms,
                        self.NOISE_NEAREST["unimodality_ms"])

    def test_fidelity_admits_48_4_and_refuses_the_null(self):
        self.assertGreater(self.t.max_fidelity_residual,
                           self.HEALTHY_48_4["fidelity"])
        self.assertLess(self.t.max_fidelity_residual,
                        self.NOISE_NEAREST["fidelity"])

    def test_the_fidelity_bound_keeps_balanced_headroom_on_both_sides(self):
        """Sitting between the two populations is necessary but far from
        sufficient here: the gap spans 173x (0.00064 healthy at 44 dB-Hz
        to 0.1115 for the nearest noise block), so almost any value
        clears the two-sided test above while leaving one side with
        nearly no margin.  The old 0.015, derived before the signed-T
        fix dropped the healthy population two orders of magnitude, sat
        only 7.4x below the noise floor -- and that floor has already
        moved once with seed count (0.2547 -> 0.1016 -> 0.1115).  So the
        bound must keep at least 10x on BOTH sides, which puts it near
        the geometric middle of 0.0064 - 0.0112 rather than anywhere in
        the gap."""
        b = self.t.max_fidelity_residual
        self.assertGreaterEqual(
            b / self.HEALTHY_44["fidelity"], 10.0,
            f"{b} leaves only {b / self.HEALTHY_44['fidelity']:.1f}x over "
            f"the worst healthy reading at 44 dB-Hz")
        self.assertGreaterEqual(
            self.NOISE_NEAREST["fidelity"] / b, 10.0,
            f"{b} leaves only {self.NOISE_NEAREST['fidelity'] / b:.1f}x "
            f"under the nearest of 90 pure-noise blocks")

    def test_split_half_admits_48_4_and_refuses_the_null(self):
        self.assertGreater(self.t.max_split_half_delta_samples,
                           self.HEALTHY_48_4["split_half"])
        self.assertLess(self.t.max_split_half_delta_samples,
                        self.NOISE_NEAREST["split_half"])

    def test_apex_admits_a_healthy_edge_and_refuses_the_lattice_lock(self):
        """⛔ This one does NOT separate signal from noise -- pure noise
        read |apex| 0.021-4.587, overlapping the healthy 0.005-1.311 --
        and it is not meant to.  What it must separate is an undisplaced
        edge from the 2026-09-04 B4 lock at a 20.000 ms lattice
        position, measured at -1919 samples."""
        self.assertGreater(self.t.max_apex_distance_samples,
                           self.HEALTHY_48_4["apex"])
        self.assertLess(self.t.max_apex_distance_samples, 1919.0)

    def test_transition_width_is_a_physics_bound_not_a_discriminator(self):
        """Pure noise read 1-5 samples against a healthy 1-2: no
        separation at all.  The bound exists so a fit with no transition
        in it cannot be reported as a transition, and it must clear
        every reading the sweep produced, noise included."""
        self.assertGreater(self.t.max_transition_width_samples, 5.0)

    def test_the_sigma_ceiling_is_not_a_swept_quantity(self):
        """`reported_sigma_ms` arrives as an argument, so no C/N0 arm can
        measure it -- pure noise and a healthy pilot read whatever the
        caller hands over.  All this test establishes is the fact that
        makes the other two sigma classes necessary: the battery reads
        sigma straight back out of its input, so the criterion can only
        ever judge it against a MODEL.  The model's anchoring lives in
        TestThePredictionCurveIsAnchoredToWhatTheStageProduces and the
        resulting ceiling in
        TestTheSigmaCeilingDoesNotRefuseTheStationItServes."""
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        for handed_in in (0.001, 0.05, 12.5):
            with self.subTest(reported=handed_in):
                v = _sigma_verdict(handed_in, B4_WORST_CN0, bat=bat)
                self.assertEqual(v.criteria["sigma"], handed_in)
                self.assertEqual(v.sigma_ms, handed_in)


def _sigma_verdict(sigma_ms: float, cn0: float, *, bat=None):
    """One healthy six-block run through the real battery, so only the
    sigma criterion can be what decides."""
    import test_t6_battery as fixtures  # the healthy FineEdgeEstimate
    bat = bat or T6Battery(sample_rate=SR, fold_seconds=K)
    bat.reset()
    v = None
    for e in fixtures._series(6):
        v = bat.evaluate(e, implied_chain_delay_ns=16_618_000,
                         reported_sigma_ms=sigma_ms, cn0_db_hz=cn0)
    return v


class TestTheSigmaCeilingDoesNotRefuseTheStationItServes(unittest.TestCase):
    """The regression that would have caught the misfire.

    ⛔ The ceiling compares two DIFFERENT quantities: `reported_sigma_ms`
    is the tier's OWN published uncertainty, computed by the authority
    from its own statistics, while `_predicted_sigma_ms` models the
    FOLD's block-to-block scatter.  Those are not the same measurement
    and no choice of anchor constant makes comparing them sound -- so an
    absolute floor sits under the ceiling.

    Before the floor, a plausible daytime 57 dB-Hz gave a ceiling near
    0.017 ms, and B4's recorded hourly sigma runs 0.003-0.07 ms.  A
    healthy B4 would have been REFUSED: precisely the failure this whole
    design exists to end.  Shipping that would have been worse than
    shipping no sigma criterion at all."""

    def test_b4s_worst_recorded_sigma_passes_at_every_swept_cn0(self):
        for cn0 in SWEPT_CN0:
            with self.subTest(cn0=cn0):
                v = _sigma_verdict(B4_WORST_REPORTED_SIGMA_MS, cn0)
                self.assertNotIn(
                    "sigma", v.failures,
                    f"a healthy B4 hour was refused at {cn0} dB-Hz; "
                    f"ceiling {v.criteria['sigma_ceiling_ms']} ms")
                self.assertTrue(v.passed, v.failures)

    def test_477_ms_still_fails_at_every_swept_cn0(self):
        """DASI-009.AI6VN, 2026-09-18.  No band condition excuses it,
        and the floor must not have excused it either."""
        for cn0 in SWEPT_CN0:
            with self.subTest(cn0=cn0):
                v = _sigma_verdict(477.0, cn0)
                self.assertIn("sigma", v.failures)
                self.assertFalse(v.passed)

    def test_the_floor_governs_the_whole_working_band(self):
        """From 77 dB-Hz down to about 36 the floor IS the ceiling; only
        below that does the 1/sqrt(SNR) term widen it, which is the
        intended behaviour for a channel genuinely that bad."""
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        for cn0 in SWEPT_CN0:
            with self.subTest(cn0=cn0):
                curve = bat._predicted_sigma_ms(cn0) * bat.t.sigma_margin
                self.assertLess(curve, SIGMA_CEILING_FLOOR_MS)
                v = _sigma_verdict(0.001, cn0)
                self.assertEqual(v.criteria["sigma_ceiling_ms"],
                                 SIGMA_CEILING_FLOOR_MS)
        self.assertGreater(
            bat._predicted_sigma_ms(30.0) * bat.t.sigma_margin,
            SIGMA_CEILING_FLOOR_MS)

    def test_the_floor_clears_b4_by_14x_and_refuses_ai6vn_by_477x(self):
        """The two station measurements that fix where the floor sits."""
        self.assertAlmostEqual(
            SIGMA_CEILING_FLOOR_MS / B4_WORST_REPORTED_SIGMA_MS,
            14.3, places=1)
        self.assertAlmostEqual(477.0 / SIGMA_CEILING_FLOOR_MS, 477.0, places=1)


class TestThePredictionCurveIsAnchoredToWhatTheStageProduces(unittest.TestCase):
    """⚠ Re-anchored 2026-09-19: REF_SIGMA_NS 95 -> 490.

    95 ns came from §8c's real-IQ per-second measurement at 77 dB-Hz.
    The sweep measured this stage's own block-to-block scatter and found
    it 4.0-5.2x wider, uniformly across 33 dB -- the 1/sqrt(SNR) SHAPE
    held, so the law was right and only the constant was wrong.  Code
    must be anchored to what it actually produces."""

    MEASURED_US_PER_BLOCK = {77.0: 0.0900, 66.0: 0.3210, 58.0: 0.7512,
                             52.0: 1.3760, 48.4: 2.0950, 44.0: 3.1175}

    def test_the_curve_now_tracks_the_measurement_within_1_3x(self):
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        for cn0, us in self.MEASURED_US_PER_BLOCK.items():
            with self.subTest(cn0=cn0):
                ratio = bat._predicted_sigma_ms(cn0) / (us / 1000.0)
                # Over-predicting is the safe direction for a ceiling;
                # under-predicting by more than a few percent is not.
                self.assertGreater(ratio, 0.95)
                self.assertLess(ratio, 1.30)

    def test_the_old_anchor_would_have_under_predicted_by_about_five(self):
        """Kept as the record of WHY it moved: at 95 ns the curve sat
        4.0-5.2x below the stage's measured scatter, so 5x of the 100x
        margin was silently covering a modelling error rather than real
        variation."""
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        for cn0, us in self.MEASURED_US_PER_BLOCK.items():
            with self.subTest(cn0=cn0):
                old = bat._predicted_sigma_ms(cn0) * (95.0 / 490.0)
                ratio = (us / 1000.0) / old
                self.assertGreater(ratio, 3.5)
                self.assertLess(ratio, 6.0)


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


class TestTheFidelityResidualDoesNotDependOnFoldPosition(unittest.TestCase):
    """The test whose absence let a position-dependent statistic ship.

    Where the edge lands inside the folded second is arbitrary: it
    follows from where the stream started, so it changes on every boot
    and carries no information about the station.  A criterion that
    moves with it is not measuring the station at all.

    Criterion 4 did exactly that until 2026-09-19.  Fitting the triangle
    to |T(e)| rather than to the signed T put a V-notch in the curve
    wherever T crossed zero -- which happens for every edge that does
    not split the fold evenly -- and the residual grew with the
    displacement.  At 77 dB-Hz, against the 0.015 bound then in force:

        edge 47916 -> 0.0014   40000 -> 0.1260   30000 -> 0.2611
        edge 24000 -> 0.3333    9600 -> 0.4869    1000 -> 0.5683

    Every position but mid-fold was refused, and the sweep that derived
    the thresholds never saw it because it drove one position throughout.

    The signed fit measured 8.1e-06 - 3.1e-05 at 77 dB-Hz and
    1.7e-04 - 3.9e-04 at 48.4 across nine positions (the full 30 s-fold
    sweep, 3 seeds each): a 1.3x spread where |T| gave 410x.  This test
    re-runs the geometry on a short fold so it can run every time.
    """

    FOLD = 5
    POSITIONS = (37, 1000, 9600, 24000, 30000, 40000, 47916, 60000,
                 80000, 95000, SR - 40)

    def _residuals(self, edge: float, cn0: float, seed: int = 11):
        stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=self.FOLD)
        sig = _make_bpsk_signal(
            duration_s=self.FOLD * 4 + 1.0, sample_rate=SR,
            edge_offset_samples=edge, noise_std=_noise_std_for(cn0),
            seed=seed)
        out = []
        for i in range(0, len(sig), BATCH):
            est = stage.process_samples(sig[i:i + BATCH], i)
            if est is not None:
                out.append(est.peak_prominence)
        return out[1:]

    def test_every_fold_position_clears_the_shipped_bound(self):
        bound = BatteryThresholds().max_fidelity_residual
        seen = []
        for pos in self.POSITIONS:
            with self.subTest(edge=pos):
                vals = self._residuals(pos + 0.1672, 77.0)
                self.assertTrue(vals, f"no estimate at edge {pos}")
                seen += vals
                self.assertLess(
                    max(vals), bound,
                    f"edge {pos}: residual {max(vals):.6g} exceeds the "
                    f"{bound} bound -- the statistic moved with fold "
                    f"position, which is a per-boot accident")
        self.assertLess(max(seen) / min(seen), 20.0,
                        f"residual spread across fold positions is "
                        f"{max(seen) / min(seen):.1f}x: "
                        f"{min(seen):.6g} - {max(seen):.6g}")

    def test_every_fold_position_clears_it_at_the_governing_cn0_too(self):
        """48.4 dB-Hz, B4's worst hour -- the case the bound governs."""
        bound = BatteryThresholds().max_fidelity_residual
        for pos in self.POSITIONS:
            with self.subTest(edge=pos):
                vals = self._residuals(pos + 0.1672, B4_WORST_CN0)
                self.assertTrue(vals, f"no estimate at edge {pos}")
                self.assertLess(max(vals), bound,
                                f"edge {pos}: {max(vals):.6g} > {bound}")
