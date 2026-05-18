"""Regression tests for M-H19 / M-H20 in BroadcastKalmanFilter.

M-H19 — the mode-transition detector and the adaptive process noise were fed
`measurement - state[0]` computed BEFORE the predict step. The true innovation
(post-predict) differs by doppler·dt, so the filter's defences keyed off the
wrong residual. Fix: predict the state first, derive one innovation, feed it
to both defences and the update.

M-H20 — "time since the last mode transition" was tracked two ways: an
update-counter (`time_since_mode_change`) feeding the adaptive Q / search
window, and a wall-clock timestamp feeding `is_converged()`. `load_state`
restored neither, so a restarted filter believed its last transition was
~10000 s ago and could flip "converged" immediately. Fix: one wall-clock
time base, persisted across restarts.
"""

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter


class TestPostPredictInnovation(unittest.TestCase):
    """M-H19: the defences see the true (post-predict) innovation."""

    def test_defences_see_post_predict_innovation(self) -> None:
        kf = BroadcastKalmanFilter('WWV_10000', 'WWV', 10.0)
        kf.update(35.0, snr_db=15.0)  # initialise

        # Known state with a non-zero doppler so pre- vs post-predict differ.
        kf.state = np.array([35.0, 2.0])  # tof=35 ms, doppler=2 ms/min

        seen = {}
        orig_dmt = kf.detect_mode_transition
        orig_apn = kf._adaptive_process_noise

        def spy_dmt(innovation_ms):
            seen['dmt'] = innovation_ms
            return orig_dmt(innovation_ms)

        def spy_apn(innovation_ms, snr_db, time_since_mode_change):
            seen['apn'] = innovation_ms
            return orig_apn(innovation_ms=innovation_ms, snr_db=snr_db,
                            time_since_mode_change=time_since_mode_change)

        kf.detect_mode_transition = spy_dmt
        kf._adaptive_process_noise = spy_apn

        kf.update(40.0, snr_db=15.0)

        # Post-predict state[0] = 35 + doppler·dt = 35 + 2·1 = 37.
        # True innovation = 40 - 37 = 3.0  (the pre-predict value was 40-35=5.0).
        self.assertAlmostEqual(seen['dmt'], 3.0, places=6)
        self.assertAlmostEqual(seen['apn'], 3.0, places=6)
        # ... and that is the value the filter recorded as its innovation.
        self.assertAlmostEqual(kf.last_innovation, 3.0, places=6)


class TestSingleTimeBase(unittest.TestCase):
    """M-H20: one wall-clock base, no separate update-counter."""

    def test_no_separate_update_counter(self) -> None:
        kf = BroadcastKalmanFilter('WWV_10000', 'WWV', 10.0)
        self.assertFalse(hasattr(kf, 'time_since_mode_change'))
        self.assertTrue(hasattr(kf, '_minutes_since_mode_change'))
        # Far-past default ⇒ the filter starts in the stable regime.
        self.assertGreater(kf._minutes_since_mode_change(), 5.0)

    def test_restart_preserves_recent_transition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            kf1 = BroadcastKalmanFilter('WWV_10000', 'WWV', 10.0)
            for _ in range(30):
                kf1.update(35.0, snr_db=20.0)  # converge on a steady ToF
            # A mode transition just happened.
            kf1.last_mode_transition_time = time.time()
            kf1.save_state(state_dir)

            kf2 = BroadcastKalmanFilter('WWV_10000', 'WWV', 10.0)
            self.assertTrue(kf2.load_state(state_dir))

            # The recent transition is restored, not lost to the far-past
            # constructor default.
            self.assertAlmostEqual(kf2.last_mode_transition_time,
                                   kf1.last_mode_transition_time, places=3)
            self.assertLess(kf2._minutes_since_mode_change(), 1.0)
            # So the restarted filter does NOT immediately call itself
            # converged on the strength of a stale init timestamp.
            self.assertFalse(kf2.is_converged())
            # The recent transition is the only blocker — age it out and the
            # otherwise-converged filter does converge.
            kf2.last_mode_transition_time = time.time() - 10000.0
            self.assertTrue(kf2.is_converged())


if __name__ == '__main__':
    unittest.main()
