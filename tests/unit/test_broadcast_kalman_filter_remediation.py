#!/usr/bin/env python3
"""
Unit tests for the M-M14 / M-M15 remediation in
``broadcast_kalman_filter.py``.

M-M14 — Joseph-form covariance update + symmetrisation
  The previous short form ``P = (I − K H) P`` is algebraically right
  in exact arithmetic but drifts asymmetric and toward non-PSD under
  finite precision over the ~10⁶ updates/week each per-broadcast
  bank sees.  These tests pin the post-update properties:

    1. P stays symmetric to machine precision after every cycle.
    2. P stays positive semidefinite over a long update run.
    3. The state estimate is unchanged versus the short form for a
       single update (both forms agree in exact arithmetic), so this
       is purely a numerical-stability fix.

M-M15 — NaN/Inf guard on the entrypoint
  The previous code accepted any float for ``measurement_ms`` /
  ``snr_db`` and let NaN poison ``self.P`` before the L3 NaN filter
  in the fusion service downstream could intervene.  Tests pin:

    4. A NaN measurement on an initialised filter is rejected; the
       state and P are unchanged.
    5. An Inf SNR is rejected the same way.
    6. A NaN on an uninitialised filter returns (NaN, NaN) and does
       not flip ``initialized`` to True.
"""

import math
import unittest

import numpy as np

from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter


def _make_filter(broadcast_id: str = "WWV_10000",
                 station: str = "WWV",
                 frequency_mhz: float = 10.0) -> BroadcastKalmanFilter:
    return BroadcastKalmanFilter(broadcast_id, station, frequency_mhz)


class TestJosephFormCovariance(unittest.TestCase):
    def test_p_stays_symmetric_after_each_update(self):
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)  # initialise

        # 100 well-conditioned updates with small variation.
        rng = np.random.default_rng(2026_05_19)
        for _ in range(100):
            m = 10.0 + 0.05 * rng.standard_normal()
            snr = 20.0 + rng.standard_normal()
            kf.update(measurement_ms=m, snr_db=snr)

            # P must be exactly symmetric (we symmetrise explicitly).
            asym = np.max(np.abs(kf.P - kf.P.T))
            self.assertLess(asym, 1e-12,
                            f"P asymmetry {asym:.3e} exceeds 1e-12")

    def test_p_stays_positive_semidefinite_under_long_run(self):
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)

        rng = np.random.default_rng(2026_05_19)
        for _ in range(500):
            m = 10.0 + 0.1 * rng.standard_normal()
            snr = 18.0 + 2.0 * rng.standard_normal()
            kf.update(measurement_ms=m, snr_db=snr)

        eigenvalues = np.linalg.eigvalsh(kf.P)
        # Strictly PD in practice; allow a tiny floor for numerical slack.
        self.assertGreater(eigenvalues.min(), -1e-12,
                           f"P has a negative eigenvalue: {eigenvalues}")

    def test_p_stays_pd_under_extreme_snr_dynamics(self):
        """Wild SNR swings (low SNR → high R → small K → P barely
        updates; high SNR → tiny R → K ≈ 1 → P drops fast) are the
        classic torture for the short-form update."""
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)

        for i in range(200):
            snr = 35.0 if (i % 2 == 0) else 1.0  # alternate clean/awful
            kf.update(measurement_ms=10.0 + 0.01 * (i % 7), snr_db=snr)
            eigenvalues = np.linalg.eigvalsh(kf.P)
            self.assertGreater(
                eigenvalues.min(), -1e-12,
                f"cycle {i}: P went non-PSD, eigenvalues={eigenvalues}"
            )

    def test_joseph_form_state_matches_short_form_one_step(self):
        """A single update step's mean estimate must equal the
        short-form result (Joseph form only changes the covariance
        update; the state update is identical)."""
        kf_joseph = _make_filter()
        kf_joseph.update(measurement_ms=10.0, snr_db=20.0)  # init

        # Replicate the short-form mathematics from kf_joseph's pre-update
        # state, then compare the state after one update.
        F = kf_joseph.F.copy()
        H = kf_joseph.H.copy()
        P_pre = kf_joseph.P.copy()
        state_pre = kf_joseph.state.copy()

        kf_joseph.update(measurement_ms=10.05, snr_db=19.0)

        # Hand-compute the short-form state update against the same Q/R.
        # (The point isn't to retest the algebra — it's to pin that the
        #  mean estimate didn't accidentally shift when we replaced the
        #  P update.)
        state_predicted = F @ state_pre
        innovation = 10.05 - (H @ state_predicted)[0]
        Q = kf_joseph._adaptive_process_noise(
            innovation_ms=innovation,
            snr_db=19.0,
            time_since_mode_change=kf_joseph._minutes_since_mode_change(),
        )
        # Note: P after the pre-update is wrong here because Q is
        # computed from kf_joseph's *updated* mode-transition state;
        # but the state update only uses K, which depends on P_predict.
        P_predict = F @ P_pre @ F.T + Q
        R = kf_joseph._get_measurement_noise(19.0)
        S = H @ P_predict @ H.T + R
        K = P_predict @ H.T / S
        expected_state = state_predicted + K.flatten() * innovation

        self.assertTrue(np.allclose(kf_joseph.state, expected_state, atol=1e-9))


class TestNanInfGuard(unittest.TestCase):
    def test_nan_measurement_on_initialised_filter_is_rejected(self):
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)  # initialise

        state_before = kf.state.copy()
        P_before = kf.P.copy()
        n_before = kf.n_updates

        tof, sigma = kf.update(measurement_ms=float("nan"), snr_db=18.0)

        # State and P preserved; counter unchanged.
        np.testing.assert_array_equal(kf.state, state_before)
        np.testing.assert_array_equal(kf.P, P_before)
        self.assertEqual(kf.n_updates, n_before)
        # Returned values are the current state, not NaN.
        self.assertEqual(tof, float(state_before[0]))
        self.assertTrue(math.isfinite(sigma))

    def test_inf_snr_is_rejected(self):
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)

        P_before = kf.P.copy()
        kf.update(measurement_ms=10.1, snr_db=float("inf"))
        np.testing.assert_array_equal(kf.P, P_before)

    def test_negative_inf_snr_is_rejected(self):
        kf = _make_filter()
        kf.update(measurement_ms=10.0, snr_db=20.0)

        P_before = kf.P.copy()
        kf.update(measurement_ms=10.1, snr_db=float("-inf"))
        np.testing.assert_array_equal(kf.P, P_before)

    def test_nan_on_uninitialised_filter_does_not_initialise(self):
        kf = _make_filter()
        self.assertFalse(kf.initialized)

        tof, sigma = kf.update(measurement_ms=float("nan"), snr_db=20.0)

        self.assertFalse(kf.initialized)
        self.assertTrue(math.isnan(tof))
        self.assertTrue(math.isnan(sigma))


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
