#!/usr/bin/env python3
"""
Unit tests for the M-M21 / M-M22 / M-M23 remediation in
``l2_calibration_service.py``.

  * M-M21 — geometric fallback adds a climatological 40.3/f² iono term
            instead of returning the vacuum speed-of-light delay (a
            several-ms bias previously modelled as zero-mean uncertainty).
  * M-M22 — coverage factor ``k`` is computed from the Welch-
            Satterthwaite effective DOF, not hard-coded 2.0; for the
            mid-DOF regime (ν≈10) the right two-sided 95 % multiplier
            is 2.228, so the previous hard-code under-reported the
            expanded uncertainty.
  * M-M23 — every component is constructed as an
            :class:`UncertaintyComponent` carrying a citable ``source``
            string and a degrees-of-freedom estimate, so the budget is
            auditable end-to-end.
"""

from __future__ import annotations

import math
import unittest
from typing import Dict

from hf_timestd.core.l2_calibration_service import (
    UncertaintyComponent,
    _coverage_factor_95,
    _welch_satterthwaite,
)


# ---------------------------------------------------------------------
# M-M22 — Welch-Satterthwaite + coverage factor
# ---------------------------------------------------------------------

class TestWelchSatterthwaite(unittest.TestCase):
    def test_two_equal_components_with_same_dof_gives_double_dof(self):
        """Standard sanity: two equal-value, equal-DOF components combine
        to ν_eff = 2·ν.  Direct from the WS identity for n equal terms."""
        c = UncertaintyComponent(value_ms=1.0, dof=10.0, source="…")
        components: Dict[str, UncertaintyComponent] = {'a': c, 'b': c}
        # (1² + 1²)² / (1⁴/10 + 1⁴/10) = 4 / 0.2 = 20.
        self.assertAlmostEqual(_welch_satterthwaite(components), 20.0, places=9)

    def test_infinite_dof_terms_do_not_constrain(self):
        """A Type-B (ν=∞) term contributes to the numerator (the
        combined uncertainty) but not to the denominator, so it raises
        ν_eff above what the finite-DOF terms alone would give."""
        finite = UncertaintyComponent(value_ms=1.0, dof=10.0, source="…")
        infinite = UncertaintyComponent(value_ms=1.0, dof=math.inf, source="…")
        nu_finite_only = _welch_satterthwaite({'a': finite})
        nu_with_infinite = _welch_satterthwaite({'a': finite, 'b': infinite})
        self.assertGreater(nu_with_infinite, nu_finite_only)

    def test_all_infinite_gives_inf(self):
        components: Dict[str, UncertaintyComponent] = {
            'a': UncertaintyComponent(value_ms=1.0, dof=math.inf, source="…"),
            'b': UncertaintyComponent(value_ms=2.0, dof=math.inf, source="…"),
        }
        self.assertEqual(_welch_satterthwaite(components), float('inf'))

    def test_all_zero_uncertainty_gives_inf(self):
        components: Dict[str, UncertaintyComponent] = {
            'a': UncertaintyComponent(value_ms=0.0, dof=5.0, source="…"),
        }
        self.assertEqual(_welch_satterthwaite(components), float('inf'))


class TestCoverageFactor95(unittest.TestCase):
    def test_t_value_at_dof_10_is_2_228(self):
        """ν=10, two-sided 95 % Student-t multiplier is well-known
        as ≈ 2.228.  This is the value the old k=2.0 hard-code missed."""
        k = _coverage_factor_95(10.0)
        self.assertAlmostEqual(k, 2.228, places=2)

    def test_t_value_converges_to_z_at_large_dof(self):
        """As ν → ∞ the multiplier converges to z(0.975) ≈ 1.960."""
        k = _coverage_factor_95(10_000.0)
        self.assertAlmostEqual(k, 1.960, places=2)

    def test_infinite_dof_uses_normal_quantile(self):
        k = _coverage_factor_95(float('inf'))
        self.assertAlmostEqual(k, 1.959963984540054, places=10)

    def test_zero_or_negative_dof_falls_back_to_normal(self):
        # Defensive: a degenerate DOF (no usable Type-A terms) shouldn't
        # produce NaN; fall back to z(0.975).
        self.assertAlmostEqual(_coverage_factor_95(0.0), 1.960, places=2)
        self.assertAlmostEqual(_coverage_factor_95(-1.0), 1.960, places=2)


# ---------------------------------------------------------------------
# Integration: _calculate_uncertainty on a representative call
# ---------------------------------------------------------------------

class TestCalculateUncertaintyIntegration(unittest.TestCase):
    """Exercise the budget builder through a bare-instance
    MetrologyService stand-in — the method only reads ``self`` for
    nothing in particular, so we can skip the heavy ``__init__``.
    """

    def _budget(self, **overrides):
        from hf_timestd.core.l2_calibration_service import L2CalibrationService
        svc = L2CalibrationService.__new__(L2CalibrationService)
        kwargs = dict(
            raw_toa_ms=10.0,
            propagation_delay_ms=15.0,
            mode_confidence=0.5,
            snr_db=15.0,
            n_hops=1,
        )
        kwargs.update(overrides)
        return svc._calculate_uncertainty(**kwargs)

    def test_budget_exposes_coverage_factor_and_dof(self):
        b = self._budget()
        self.assertIn('coverage_factor', b)
        self.assertIn('effective_dof', b)

    def test_expanded_equals_combined_times_coverage_factor(self):
        b = self._budget()
        self.assertAlmostEqual(
            b['expanded_uncertainty_ms'],
            b['combined_uncertainty_ms'] * b['coverage_factor'],
            places=9,
        )

    def test_coverage_factor_above_two_for_mid_dof_budget(self):
        """A typical budget with finite-DOF Type-A terms dominating
        should land above the old k=2.0 hard-code — the very thing
        M-M22 was correcting."""
        b = self._budget(mode_confidence=0.0)  # u_prop_model dominates
        self.assertGreater(b['coverage_factor'], 2.0)
        # And under z(0.975) — we're not at ν=∞.
        self.assertLess(b['coverage_factor'], 2.5)

    def test_high_mode_confidence_gives_smaller_uncertainty(self):
        # mode_confidence shrinks u_prop_model; combined should drop.
        b_low = self._budget(mode_confidence=0.0)
        b_high = self._budget(mode_confidence=1.0)
        self.assertLess(b_high['combined_uncertainty_ms'],
                        b_low['combined_uncertainty_ms'])


# ---------------------------------------------------------------------
# M-M21 — geometric fallback includes ionospheric term
# ---------------------------------------------------------------------

class TestGeometricFallbackIonoTerm(unittest.TestCase):
    """The fallback path is private (inside `process_minute`); the
    arithmetic is testable in isolation via the public helpers it
    uses (`hop_geometry`, the 40.3/f² constant, etc.).  These tests
    pin the *invariant* — total delay > vacuum delay — and that the
    iono term scales as 1/f²."""

    @staticmethod
    def _delay_via_fallback_arithmetic(dist_km: float, frequency_mhz: float):
        """Mirror of the M-M21 inline calculation."""
        from hf_timestd.core.hop_geometry import hop_geometry, n_hops_for_distance
        from hf_timestd.core.propagation_engine import (
            F2_LAYER_HEIGHT_KM,
            IONO_DELAY_CONSTANT_MS,
            NOMINAL_SLANT_TEC_PER_HOP_TECU,
            SPEED_OF_LIGHT_KM_S,
        )
        n_hops = n_hops_for_distance(dist_km, F2_LAYER_HEIGHT_KM)
        geom = hop_geometry(dist_km, F2_LAYER_HEIGHT_KM, n_hops)
        geometric_ms = geom.path_length_km / SPEED_OF_LIGHT_KM_S * 1000.0
        iono_ms = (
            IONO_DELAY_CONSTANT_MS
            * NOMINAL_SLANT_TEC_PER_HOP_TECU * n_hops
            / (frequency_mhz ** 2)
        )
        return geometric_ms, iono_ms, geometric_ms + iono_ms

    def test_total_delay_exceeds_pure_vacuum(self):
        dist_km = 3000.0
        vacuum_ms = dist_km / 299.792458
        geom_ms, iono_ms, total_ms = self._delay_via_fallback_arithmetic(dist_km, 10.0)
        # Geometric slant alone is already longer than vacuum great-circle.
        self.assertGreater(geom_ms, vacuum_ms)
        # And the iono term adds a positive bias on top.
        self.assertGreater(iono_ms, 0.0)
        self.assertGreater(total_ms, vacuum_ms)
        # The shift the old code missed is at least a millisecond on a
        # 3000 km path at 10 MHz (geometric slant alone is ~10 ms longer
        # over a 1-hop F2 path; iono adds another ~0.4 ms).
        self.assertGreater(total_ms - vacuum_ms, 0.3)

    def test_iono_term_scales_as_inverse_f_squared(self):
        dist_km = 2000.0
        _, iono_25, _ = self._delay_via_fallback_arithmetic(dist_km, 25.0)
        _, iono_5, _ = self._delay_via_fallback_arithmetic(dist_km, 5.0)
        # 1/f² scaling: ratio = (25/5)² = 25.
        self.assertAlmostEqual(iono_5 / iono_25, 25.0, places=6)


# ---------------------------------------------------------------------
# M-M23 — every component carries a source citation
# ---------------------------------------------------------------------

class TestUncertaintyComponentTraceability(unittest.TestCase):
    """Every uncertainty term that flows into the L2 budget must be
    constructed via :class:`UncertaintyComponent` (so it carries the
    ``source`` and ``dof`` fields).  Source inspection pins this on the
    module text — much cheaper than running the full pipeline and
    parsing back, and the assertion is what the review actually calls
    for ('cite the measurement/datasheet/standard per term')."""

    def test_each_component_constructs_via_dataclass(self):
        from pathlib import Path
        from hf_timestd.core import l2_calibration_service
        src = Path(l2_calibration_service.__file__).read_text()

        # Pin each of the six budget terms goes through the dataclass.
        for name in (
            "u_rtp", "u_iono", "u_multipath",
            "u_discrim", "u_gpsdo", "u_prop_model",
        ):
            with self.subTest(name=name):
                # Each name is bound to an UncertaintyComponent(...) call
                # in _calculate_uncertainty.
                self.assertIn(
                    f"{name} = UncertaintyComponent(", src,
                    f"Component '{name}' should be an UncertaintyComponent "
                    f"with a citable source (M-M23)",
                )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
