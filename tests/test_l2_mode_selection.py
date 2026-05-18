"""Regression tests for M-H23: L2 propagation-mode selection must not be circular.

`_calibrate_measurement` used to choose the propagation mode by feeding each
candidate's own delay back into `identify_mode`:

    candidate_arrival = raw_toa_ms + candidate.total_delay_ms
    identify_mode(measured_delay_ms=candidate_arrival)   # identifies `candidate`

`raw_toa_ms` is a small timing residual (D_clock), not an absolute measured
delay, so every candidate self-identified and the loosest-uncertainty mode
"won" — a tautology. `propagation_delay_ms`, `n_hops` and `u_iono ∝ √n_hops`
were all chosen circularly.

Fix: pick the climatologically-dominant mode — the first viable candidate from
`calculate_modes` (sorted by delay, MUF-feasibility-filtered) — and source
`mode_confidence` from the propagation model (`ModeCandidate.model_confidence`).
"""

import unittest

from hf_timestd.core.l2_calibration_service import L2CalibrationService
from hf_timestd.core.propagation_mode_solver import PropagationModeSolver


def _service() -> L2CalibrationService:
    """An L2CalibrationService with just what _calibrate_measurement needs —
    bypassing the heavy constructor (writers, ClickHouse, seeding)."""
    svc = object.__new__(L2CalibrationService)
    svc.receiver_lat = 44.96
    svc.receiver_lon = -93.05
    svc.prop_solver = PropagationModeSolver("EN34")
    return svc


def _l1(raw_toa_ms: float) -> dict:
    return dict(
        station_id="WWV",
        frequency_mhz=10.0,
        raw_toa_ms=raw_toa_ms,
        snr_db=20.0,
        tone_detected=True,
        timestamp_utc="2026-05-18T15:00:00+00:00",
    )


class TestModeSelectionNotCircular(unittest.TestCase):

    def test_mode_selection_independent_of_timing_residual(self) -> None:
        """The chosen mode must not depend on raw_toa_ms (the D_clock residual).

        Pre-M-H23 the mode was derived from raw_toa_ms via the circular loop;
        post-fix it comes only from calculate_modes, so varying the residual
        leaves the propagation mode untouched.
        """
        svc = _service()
        results = [svc._calibrate_measurement(_l1(rt), "SHARED_10000")
                   for rt in (0.5, 12.0, -8.0, 30.0)]
        for r in results:
            self.assertIsNotNone(r)

        ref = results[0]
        for r in results[1:]:
            self.assertEqual(r.propagation_mode, ref.propagation_mode)
            self.assertEqual(r.n_hops, ref.n_hops)
            self.assertAlmostEqual(r.propagation_delay_ms,
                                   ref.propagation_delay_ms, places=9)
            self.assertAlmostEqual(r.confidence, ref.confidence, places=9)
        # The residual itself DOES still flow through to the clock offset.
        self.assertEqual([r.clock_offset_ms for r in results],
                         [0.5, 12.0, -8.0, 30.0])

    def test_selects_climatological_primary_with_model_confidence(self) -> None:
        """The chosen mode is calculate_modes' first viable candidate, and its
        confidence is the propagation model's model_confidence."""
        svc = _service()
        modes = svc.prop_solver.calculate_modes(station="WWV", frequency_mhz=10.0,
                                                max_hops=3)
        self.assertTrue(modes)
        primary = next((m for m in modes if m.viable), modes[0])
        # ModeCandidate carries the model confidence (M-H23 plumbing).
        self.assertTrue(hasattr(primary, 'model_confidence'))

        result = svc._calibrate_measurement(_l1(2.0), "SHARED_10000")
        self.assertIsNotNone(result)
        self.assertEqual(result.propagation_mode, primary.mode.value)
        self.assertEqual(result.n_hops, primary.n_hops)
        self.assertAlmostEqual(result.propagation_delay_ms,
                               primary.total_delay_ms, places=6)
        self.assertAlmostEqual(result.confidence, primary.model_confidence,
                               places=9)


if __name__ == '__main__':
    unittest.main()
