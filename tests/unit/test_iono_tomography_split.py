"""Regression tests for P-H11 in iono_tomography.py.

The two-shell E/F separation relies on the thin-shell obliquity factors of
the E and F shells differing across the available ray elevations. They are
nearly proportional, so AᵀWA is almost singular in the E/F-split direction
and the MAP estimate collapses onto the prior — yet tec_e_tecu / tec_f_tecu
were emitted as if measured.

The solver now reports the posterior-vs-prior variance reduction for each
layer and flags the result prior_dominated when the data does not constrain
the split; conf_split = variance_reduction_e folds that into confidence.
"""

import unittest

from hf_timestd.core.iono_tomography import (
    IonoTomography,
    RayPath,
    PRIOR_DOMINATED_VR_THRESHOLD,
)


def _path(elev, stec, unc=1.0, n_hops=1):
    """A RayPath at a given elevation; only elevation/sTEC/uncertainty/hops
    feed the two-shell solve."""
    return RayPath('WWV', 10.0, elev, 270.0, 2500.0, '1F', n_hops, stec, unc)


class TestEFSplitIdentifiability(unittest.TestCase):

    def test_fields_present_and_in_range(self):
        paths = [_path(15, 22), _path(25, 20), _path(35, 18), _path(45, 17)]
        r = IonoTomography().solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(r)
        for vr in (r.variance_reduction_e, r.variance_reduction_f):
            self.assertGreaterEqual(vr, 0.0)
            self.assertLessEqual(vr, 1.0)
        self.assertIsInstance(r.prior_dominated, bool)

    def test_narrow_elevations_are_prior_dominated(self):
        # All paths in a narrow high-elevation band: the E and F obliquity
        # factors are nearly proportional, so the data cannot constrain the
        # split and the E-layer estimate is essentially the prior.
        paths = [_path(38, 20), _path(42, 19), _path(46, 18), _path(50, 17)]
        r = IonoTomography().solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(r)
        self.assertTrue(r.prior_dominated)
        self.assertLess(r.variance_reduction_e, PRIOR_DOMINATED_VR_THRESHOLD)

    def test_wide_elevation_spread_constrains_the_split(self):
        # A wide elevation spread gives the E and F obliquity factors real
        # geometric leverage: the data sharpens the E-layer markedly more.
        tomo = IonoTomography()
        narrow = tomo.solve([_path(38, 20), _path(42, 19), _path(46, 18),
                             _path(50, 17)], solar_elevation_deg=45.0)
        wide = tomo.solve([_path(7, 30), _path(15, 26), _path(30, 20),
                           _path(48, 16)], solar_elevation_deg=45.0)
        self.assertIsNotNone(narrow)
        self.assertIsNotNone(wide)
        self.assertGreater(wide.variance_reduction_e,
                           narrow.variance_reduction_e)
        self.assertFalse(wide.prior_dominated)

    def test_confidence_cannot_exceed_split_identifiability(self):
        # conf_split = variance_reduction_e is a multiplicative confidence
        # factor, so a prior-dominated split can never be reported as a
        # confident tomographic result.
        paths = [_path(38, 20), _path(42, 19), _path(46, 18), _path(50, 17)]
        r = IonoTomography().solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(r)
        self.assertLessEqual(r.confidence, r.variance_reduction_e + 1e-9)

    def test_night_e_layer_is_prior_dominated(self):
        # At night the E-layer prior is deliberately tight (E ≈ 0). The
        # near-zero E value is the prior — prior_dominated reports that
        # honestly rather than presenting it as a measurement.
        paths = [_path(15, 18), _path(25, 17), _path(35, 16), _path(45, 15)]
        r = IonoTomography().solve(paths, solar_elevation_deg=-20.0)
        self.assertIsNotNone(r)
        self.assertFalse(r.is_daytime)
        self.assertTrue(r.prior_dominated)

    def test_solve_reports_converged_for_a_normal_fit(self):
        # P-M10: a well-posed solve converges and says so.
        paths = [_path(10, 28), _path(25, 22), _path(40, 18), _path(55, 15)]
        r = IonoTomography().solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(r)
        self.assertTrue(r.converged)


class TestSingleHopRestriction(unittest.TestCase):
    """P-M9: the E/F tomography uses only single-hop paths."""

    def test_all_multihop_paths_rejected(self):
        # Every path is multi-hop — none samples a single ionospheric
        # column, so the two-shell solve cannot run.
        paths = [_path(7, 30, n_hops=2), _path(15, 26, n_hops=2),
                 _path(30, 20, n_hops=3), _path(45, 16, n_hops=2)]
        self.assertIsNone(
            IonoTomography().solve(paths, solar_elevation_deg=45.0))

    def test_multihop_paths_excluded_from_the_solve(self):
        # Two single-hop + two multi-hop, all with wide elevation spread.
        # The solve runs on the single-hop pair only.
        paths = [_path(10, 28, n_hops=1), _path(50, 16, n_hops=1),
                 _path(8, 40, n_hops=2), _path(30, 30, n_hops=3)]
        r = IonoTomography().solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.n_paths, 2)


class TestGeometryNotFabricated(unittest.TestCase):
    """P-M9: build_paths_from_tec_results must not invent geometry."""

    def test_paths_skipped_when_no_propagation_prediction(self):
        # No propagation predictions → no real elevation/hop geometry → the
        # path is skipped, not given a fabricated 30°/1-hop default.
        tec_results = {
            'WWV': {'tec_tecu': 20.0, 'confidence': 0.9,
                    'frequencies_mhz': '10.0,15.0'},
        }
        paths = IonoTomography().build_paths_from_tec_results(
            tec_results, propagation_predictions=None)
        self.assertEqual(paths, [])


if __name__ == '__main__':
    unittest.main()
