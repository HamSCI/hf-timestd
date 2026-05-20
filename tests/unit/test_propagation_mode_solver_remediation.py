#!/usr/bin/env python3
"""
Unit tests for the M-M30 / M-M31 / M-M32 / M-M33 / M-M34 / M-M35
remediation in ``propagation_mode_solver.py``.

  * M-M30 — Tier-2 fallback gates each candidate's ``viable`` on an
            oblique-MUF check against the climatological foF2/foE,
            so physically impossible modes are no longer reported
            as feasible.
  * M-M31 — :py:meth:`PropagationModeSolver.back_calculate_emission_time`
            no longer falls back to ``candidates[0]`` (the shortest-
            delay mode); it picks the lowest-N viable F2 mode with a
            hop-derated confidence, and surfaces UNKNOWN at low
            confidence when no F2 mode survives.
  * M-M32 — E-layer ionospheric group delay scales by
            ``E_TO_F2_TEC_RATIO ≈ 0.1`` (Schunk & Nagy 2009), not the
            previous unphysical ``× 0.5`` fudge.
  * M-M33 — :py:meth:`PropagationModeSolver.identify_mode` uses a
            single Gaussian-likelihood objective for both selection
            (minimise ``|residual|/σ``) and confidence
            (``exp(-z²/2)``).  Previously the selector preferred
            wide-σ modes while the reporter used the raw residual.
  * M-M34 — the FSS-upgrade branch is now a diagnostic-only log; the
            previous code logged "FSS suggests higher hop count" then
            did nothing.  Pinned via source inspection.
  * M-M35 — the second-aligned confidence boost is gated on
            ``accuracy_ms < 0.5 ms``; above that the boost is
            circular (the model delay being validated determines the
            emission time that's checked against the second boundary).
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from hf_timestd.core.propagation_mode_solver import (
    E_TO_F2_TEC_RATIO,
    NOMINAL_FOE_MHZ,
    NOMINAL_FOF2_MHZ,
    ModeCandidate,
    PropagationMode,
    PropagationModeSolver,
)


def _solver() -> PropagationModeSolver:
    """A Tier-2-only solver: build with a real receiver_grid then null
    out the HFPropagationModel so the test deterministically exercises
    the Tier-2 (climatology) path the M-M30-35 fixes live on."""
    sol = PropagationModeSolver(receiver_grid="DN70lc")  # ≈ 40 N, -105 W (Fort Collins area)
    sol._hf_model = None
    return sol


# ---------------------------------------------------------------------
# M-M30 — oblique MUF gate
# ---------------------------------------------------------------------

class TestObliqueMufGate(unittest.TestCase):
    def test_low_frequency_below_muf_is_viable(self):
        """A 5 MHz transmission at typical F2 geometry is well below
        the climatological MUF (~3.4·8 = 27 MHz at horizon)."""
        sol = _solver()
        cands = sol.calculate_modes("WWV", frequency_mhz=5.0)
        f2_1hop = [c for c in cands if c.mode == PropagationMode.F2_LAYER_1HOP]
        self.assertTrue(any(c.viable and not c.muf_limited for c in f2_1hop))

    def test_high_frequency_above_muf_marks_not_viable(self):
        """For a 25 MHz transmission, the elevations that would
        otherwise be geometrically OK at F2 height of 300 km still
        sit just under the climatological MUF; push to 50 MHz to
        force the gate."""
        sol = _solver()
        # 50 MHz at any realistic F2 elevation is far above any
        # climatological MUF (foF2=8 MHz gives MUF ≈ 27 MHz at horizon).
        cands = sol.calculate_modes("WWV", frequency_mhz=50.0)
        f2_modes = [c for c in cands
                    if c.mode in (PropagationMode.F2_LAYER_1HOP,
                                  PropagationMode.F2_LAYER_2HOP,
                                  PropagationMode.F2_LAYER_3HOP,
                                  PropagationMode.F2_LAYER_4HOP)]
        self.assertTrue(f2_modes, "Tier-2 should still emit F2 candidates with geometry — just not viable")
        self.assertTrue(all(not c.viable for c in f2_modes))
        self.assertTrue(all(c.muf_limited for c in f2_modes))

    def test_oblique_muf_helper_horizon_factor(self):
        """sec(arcsin(R/(R+h))) is the textbook M-factor for the F2
        layer at h=300 km — about 3.4."""
        muf = PropagationModeSolver._oblique_muf_mhz(
            critical_freq_mhz=10.0, elevation_deg=0.0,
            layer_height_km=300.0,
        )
        # 10 MHz × 3.4 ≈ 34 MHz (allow ±0.2 for the spherical correction).
        self.assertAlmostEqual(muf, 34.0, places=0)

    def test_oblique_muf_helper_vertical_incidence(self):
        """At 90° elevation (vertical incidence), MUF = critical freq."""
        muf = PropagationModeSolver._oblique_muf_mhz(
            critical_freq_mhz=8.0, elevation_deg=90.0,
            layer_height_km=300.0,
        )
        self.assertAlmostEqual(muf, 8.0, places=6)


# ---------------------------------------------------------------------
# M-M32 — E-layer iono delay magnitude
# ---------------------------------------------------------------------

class TestELayerIonoMagnitude(unittest.TestCase):
    def test_e_layer_iono_scales_by_tec_ratio_constant(self):
        """E-layer iono delay should be E_TO_F2_TEC_RATIO times the
        F2-layer per-hop delay, not the previous ×0.5 fudge."""
        sol = _solver()
        # Use a short distance so an E-layer candidate exists.
        cands = sol.calculate_modes("WWV", frequency_mhz=8.0)
        f2_1hop = next((c for c in cands if c.mode == PropagationMode.F2_LAYER_1HOP), None)
        e_1hop = next((c for c in cands if c.mode == PropagationMode.E_LAYER_1HOP), None)
        if f2_1hop is None or e_1hop is None:
            self.skipTest("No matching candidates at this geometry")

        # E iono / F2 iono should equal E_TO_F2_TEC_RATIO (both 1-hop).
        ratio = e_1hop.ionospheric_delay_ms / f2_1hop.ionospheric_delay_ms
        self.assertAlmostEqual(ratio, E_TO_F2_TEC_RATIO, places=9)

    def test_constant_is_documented_value(self):
        self.assertEqual(E_TO_F2_TEC_RATIO, 0.1)


# ---------------------------------------------------------------------
# M-M33 — single Gaussian-likelihood objective
# ---------------------------------------------------------------------

class TestIdentifyModeLikelihood(unittest.TestCase):
    """We exercise the public `identify_mode` to pin the invariant.
    Strategy: pick a `measured_delay_ms` that lands closer (in σ-units)
    to a tight candidate than to a wide one, and check the tight one
    wins; then check confidence drops with z."""

    def test_tight_sigma_wins_when_closer_in_sigma(self):
        sol = _solver()
        # Two synthetic candidates: one wide (σ=2), one tight (σ=0.5).
        # Measured delay is 1 ms away from the tight candidate, 1.5 ms
        # from the wide.  z_tight = 2.0; z_wide = 0.75 → wide wins.
        # Inverted setup: measured at 0.4 ms from tight (z=0.8) and 2 ms
        # from wide (z=1.0) — tight wins.
        tight = ModeCandidate(
            mode=PropagationMode.F2_LAYER_1HOP, n_hops=1,
            layer_height_km=300.0, ground_distance_km=1500.0,
            path_length_km=1550.0, elevation_angle_deg=25.0,
            propagation_delay_ms=5.0, ionospheric_delay_ms=0.05,
            total_delay_ms=5.05, delay_uncertainty_ms=0.5, viable=True,
        )
        wide = ModeCandidate(
            mode=PropagationMode.F2_LAYER_2HOP, n_hops=2,
            layer_height_km=300.0, ground_distance_km=1500.0,
            path_length_km=1700.0, elevation_angle_deg=15.0,
            propagation_delay_ms=7.0, ionospheric_delay_ms=0.1,
            total_delay_ms=7.1, delay_uncertainty_ms=2.0, viable=True,
        )

        with patch.object(sol, 'calculate_modes', return_value=[tight, wide]):
            # measured_delay 4.65 → tight: |5.05-4.65|/0.5 = 0.8;
            #                       wide:  |7.1-4.65|/2.0  = 1.225 → tight wins.
            result = sol.identify_mode("WWV", measured_delay_ms=4.65,
                                       frequency_mhz=10.0)
            self.assertEqual(result.identified_mode, PropagationMode.F2_LAYER_1HOP)

    def test_confidence_is_gaussian_likelihood(self):
        """exp(-z²/2) at z=1 ≈ 0.6065; at z=2 ≈ 0.1353; at z=0 = 1.0."""
        sol = _solver()
        cand = ModeCandidate(
            mode=PropagationMode.F2_LAYER_1HOP, n_hops=1,
            layer_height_km=300.0, ground_distance_km=1500.0,
            path_length_km=1550.0, elevation_angle_deg=25.0,
            propagation_delay_ms=5.0, ionospheric_delay_ms=0.05,
            total_delay_ms=5.0, delay_uncertainty_ms=1.0, viable=True,
        )
        with patch.object(sol, 'calculate_modes', return_value=[cand]):
            # Perfect fit → confidence = 1.0.
            r = sol.identify_mode("WWV", measured_delay_ms=5.0, frequency_mhz=10.0)
            self.assertAlmostEqual(r.confidence, 1.0, places=6)
            # 1σ away → ≈ 0.6065.
            r = sol.identify_mode("WWV", measured_delay_ms=6.0, frequency_mhz=10.0)
            self.assertAlmostEqual(r.confidence, math.exp(-0.5), places=6)
            # 2σ away → ≈ 0.1353.
            r = sol.identify_mode("WWV", measured_delay_ms=7.0, frequency_mhz=10.0)
            self.assertAlmostEqual(r.confidence, math.exp(-2.0), places=6)

    def test_wide_sigma_mode_cannot_grab_high_confidence(self):
        """A wide-σ mode at 1σ away still reports ~0.61 confidence —
        not 'close to 1' just because its σ is wide.  The old code
        could have selected this mode and reported a small residual /
        wide σ → high 'confidence'."""
        sol = _solver()
        wide = ModeCandidate(
            mode=PropagationMode.F2_LAYER_2HOP, n_hops=2,
            layer_height_km=300.0, ground_distance_km=3000.0,
            path_length_km=3200.0, elevation_angle_deg=10.0,
            propagation_delay_ms=10.0, ionospheric_delay_ms=0.1,
            total_delay_ms=10.1, delay_uncertainty_ms=5.0, viable=True,
        )
        with patch.object(sol, 'calculate_modes', return_value=[wide]):
            # 4 ms away → z = 0.8 → conf ≈ 0.726.
            r = sol.identify_mode("WWV", measured_delay_ms=14.1, frequency_mhz=10.0)
            self.assertLess(r.confidence, 0.8)


# ---------------------------------------------------------------------
# M-M31 — back_calculate emission-time fallback
# ---------------------------------------------------------------------

class TestBackCalculateFallback(unittest.TestCase):
    """When `back_calculate_emission_time` runs without a measured
    delay (the no-measured-delay branch), the choice of "primary mode"
    used to be the first F2_LAYER_1HOP or — failing that —
    `candidates[0]`.  The fix picks the lowest-N viable F2 mode, with
    a hop-derated confidence, and surfaces UNKNOWN otherwise."""

    def test_chooses_f2_mode_when_present(self):
        sol = _solver()
        # The receiver-to-WWV distance from DN70lc is too short for
        # F2 (only a ground-wave candidate fires); use WWVH (Hawaii,
        # ~5300 km from the test receiver) to force the F2 fallback path.
        arrival_t = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        result = sol.back_calculate_emission_time(
            station="WWVH", frequency_mhz=10.0, arrival_time_utc=arrival_t,
        )
        # Any of the F2 hop modes is acceptable — we're pinning that
        # the fallback picked an F2-class mode, not UNKNOWN.
        self.assertIn(result.mode, (
            PropagationMode.F2_LAYER_1HOP,
            PropagationMode.F2_LAYER_2HOP,
            PropagationMode.F2_LAYER_3HOP,
            PropagationMode.F2_LAYER_4HOP,
        ))
        self.assertGreater(result.confidence, 0.0)

    def test_no_f2_mode_returns_unknown_low_confidence(self):
        """When the synthetic candidates contain only an E-layer mode,
        the fallback must surface UNKNOWN — better to say 'I don't
        know' than to label an E-layer detection as the F2 primary."""
        sol = _solver()

        e_only = [
            ModeCandidate(
                mode=PropagationMode.E_LAYER_1HOP, n_hops=1,
                layer_height_km=110.0, ground_distance_km=1500.0,
                path_length_km=1530.0, elevation_angle_deg=15.0,
                propagation_delay_ms=5.1, ionospheric_delay_ms=0.005,
                total_delay_ms=5.105, delay_uncertainty_ms=0.2, viable=True,
            ),
        ]
        with patch.object(sol, 'calculate_modes', return_value=e_only):
            arrival_t = 1_779_796_800.0
            result = sol.back_calculate_emission_time(
                station="WWV", frequency_mhz=10.0, arrival_time_utc=arrival_t,
            )
            self.assertEqual(result.mode, PropagationMode.UNKNOWN)
            self.assertLess(result.confidence, 0.3)

    def test_derates_confidence_with_higher_hop_count(self):
        """When only 2F2 is viable (1F2 absent), confidence should be
        derated from the 1F2 baseline."""
        sol = _solver()
        f2_2hop_only = [
            ModeCandidate(
                mode=PropagationMode.F2_LAYER_2HOP, n_hops=2,
                layer_height_km=300.0, ground_distance_km=5000.0,
                path_length_km=5300.0, elevation_angle_deg=10.0,
                propagation_delay_ms=17.0, ionospheric_delay_ms=0.1,
                total_delay_ms=17.1, delay_uncertainty_ms=1.0, viable=True,
            ),
        ]
        with patch.object(sol, 'calculate_modes', return_value=f2_2hop_only):
            arrival_t = 1_779_796_800.0
            result = sol.back_calculate_emission_time(
                station="WWV", frequency_mhz=10.0, arrival_time_utc=arrival_t,
            )
            self.assertEqual(result.mode, PropagationMode.F2_LAYER_2HOP)
            # 0.6 - 0.1 * (2-1) = 0.5  (modulo any second-aligned boost).
            self.assertLessEqual(result.confidence, 0.55)


# ---------------------------------------------------------------------
# M-M35 — second-aligned boost gated on model σ
# ---------------------------------------------------------------------

class TestSecondAlignedBoost(unittest.TestCase):
    def test_boost_applied_only_when_model_sigma_is_small(self):
        """Two synthetic 1F2-only candidate sets: one with σ = 0.1 ms
        (boost allowed), one with σ = 2.0 ms (boost suppressed).  Both
        arrive at the same emission time and align to the second."""
        sol = _solver()

        # Pick an arrival time so that subtracting the candidate's
        # delay lands within ±2 ms of a second boundary.
        candidate_delay_ms = 5.0
        arrival_t = 1_779_796_800.0 + candidate_delay_ms / 1000.0  # → emission at .000 s

        def _make_candidate(sigma_ms: float) -> List[ModeCandidate]:
            return [
                ModeCandidate(
                    mode=PropagationMode.F2_LAYER_1HOP, n_hops=1,
                    layer_height_km=300.0, ground_distance_km=1500.0,
                    path_length_km=1550.0, elevation_angle_deg=25.0,
                    propagation_delay_ms=candidate_delay_ms,
                    ionospheric_delay_ms=0.0,
                    total_delay_ms=candidate_delay_ms,
                    delay_uncertainty_ms=sigma_ms, viable=True,
                ),
            ]

        # σ = 0.1 ms → well under the 0.5 ms threshold → boost applied.
        with patch.object(sol, 'calculate_modes', return_value=_make_candidate(0.1)):
            r_tight = sol.back_calculate_emission_time(
                station="WWV", frequency_mhz=10.0, arrival_time_utc=arrival_t,
            )
        # σ = 2.0 ms → well above threshold → no boost.
        with patch.object(sol, 'calculate_modes', return_value=_make_candidate(2.0)):
            r_wide = sol.back_calculate_emission_time(
                station="WWV", frequency_mhz=10.0, arrival_time_utc=arrival_t,
            )

        # Both should be marked second_aligned (offset < 2 ms).
        self.assertTrue(r_tight.second_aligned)
        self.assertTrue(r_wide.second_aligned)
        # But only the tight one gets the 1.2× confidence boost.
        # Tight: 0.6 * 1.2 = 0.72; Wide: 0.6 (no boost).
        self.assertGreater(r_tight.confidence, r_wide.confidence)


# ---------------------------------------------------------------------
# M-M34 — FSS branch is diagnostic only
# ---------------------------------------------------------------------

class TestFssDiagnosticOnly(unittest.TestCase):
    def test_source_marks_fss_branch_diagnostic(self):
        from hf_timestd.core import propagation_mode_solver
        src = Path(propagation_mode_solver.__file__).read_text()
        # The fix added a clear marker that this branch doesn't change
        # the selection.
        self.assertIn("M-M34", src)
        self.assertIn("diagnostic only", src.lower())
        # And it no longer references the dead `higher_hop_candidates`
        # placeholder that was building a list it never used.
        self.assertNotIn("higher_hop_candidates", src)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
