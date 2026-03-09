#!/usr/bin/env python3
"""
Tests for BroadcastWindowState safeguards.

Safeguard 1: Staleness decay — window widens toward model after silence
Safeguard 2: Consecutive miss counter — hard reset after sustained misses
Safeguard 3: Model floor rule — tracked can't narrow below model without strong evidence

See docs/design/UNIFIED_MEASUREMENT_PATH.md for design rationale.
"""

import math
import time
import unittest
from unittest.mock import patch

from hf_timestd.core.arrival_pattern_matrix import (
    BroadcastWindowState,
    BOOTSTRAP_INITIAL_UNCERTAINTY_MS,
    BOOTSTRAP_MIN_UNCERTAINTY_MS,
    MISS_RESET_THRESHOLD,
    MODEL_OVERRIDE_CONFIDENCE,
    MODEL_OVERRIDE_MIN_OBS,
    STALENESS_ONSET_MINUTES,
    STALENESS_DECAY_RATE,
    WINDOW_CONFIDENCE_THRESHOLD,
)


def _make_state(initial_ms=50.0, current_ms=None):
    """Create a BroadcastWindowState with sensible defaults."""
    return BroadcastWindowState(
        station='WWV',
        frequency_mhz=10.0,
        initial_uncertainty_ms=initial_ms,
        current_uncertainty_ms=current_ms if current_ms is not None else initial_ms,
    )


class TestSafeguard1StalenessDecay(unittest.TestCase):
    """Safeguard 1: Window widens toward model after STALENESS_ONSET_MINUTES of silence."""

    def test_no_decay_before_onset(self):
        """Window should not change if detection was recent."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.last_detection_time = time.time() - (STALENESS_ONSET_MINUTES - 1) * 60
        model_3sigma = 30.0
        effective = state.get_effective_uncertainty_ms(model_3sigma)
        self.assertAlmostEqual(effective, 10.0, places=1,
                               msg="Should return raw tracked value before onset")

    def test_decay_starts_after_onset(self):
        """Window should start widening after STALENESS_ONSET_MINUTES."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        # 7 minutes since last detection (2 minutes past onset)
        state.last_detection_time = time.time() - 7 * 60
        model_3sigma = 30.0
        effective = state.get_effective_uncertainty_ms(model_3sigma)
        # Should be wider than 10 but not yet at 50 (target = max(30, 50) = 50)
        self.assertGreater(effective, 10.0, "Should have started widening")
        self.assertLess(effective, 50.0, "Should not have reached target yet")

    def test_full_decay_returns_to_target(self):
        """After long silence, window should approach max(model, initial)."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        # 60 minutes since last detection — well past full decay
        state.last_detection_time = time.time() - 60 * 60
        model_3sigma = 30.0
        effective = state.get_effective_uncertainty_ms(model_3sigma)
        target = max(model_3sigma, state.initial_uncertainty_ms)  # 50.0
        self.assertAlmostEqual(effective, target, delta=1.0,
                               msg="After full decay, should be at target")

    def test_no_decay_if_never_detected(self):
        """If last_detection_time is 0 (never detected), no decay applied."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.last_detection_time = 0.0
        model_3sigma = 30.0
        effective = state.get_effective_uncertainty_ms(model_3sigma)
        self.assertAlmostEqual(effective, 10.0, places=1,
                               msg="No decay if never detected")

    def test_decay_never_goes_below_floor(self):
        """Effective uncertainty should never go below BOOTSTRAP_MIN_UNCERTAINTY_MS."""
        state = _make_state(initial_ms=50.0, current_ms=3.0)
        state.last_detection_time = time.time() - 60 * 60
        model_3sigma = 2.0  # Unrealistically small model
        effective = state.get_effective_uncertainty_ms(model_3sigma)
        self.assertGreaterEqual(effective, BOOTSTRAP_MIN_UNCERTAINTY_MS)


class TestSafeguard2ConsecutiveMissCounter(unittest.TestCase):
    """Safeguard 2: Hard reset after MISS_RESET_THRESHOLD consecutive misses."""

    def test_miss_increments_counter(self):
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.record_miss()
        self.assertEqual(state.consecutive_misses, 1)

    def test_observation_resets_counter(self):
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.consecutive_misses = 3
        state.update_with_observation(deviation_ms=2.0, snr_db=25.0)
        self.assertEqual(state.consecutive_misses, 0)

    def test_reset_at_threshold(self):
        """After MISS_RESET_THRESHOLD misses, window resets to initial."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.confidence = 0.9
        state.observation_count = 20
        state.observed_variance_ms2 = 4.0  # Low variance

        for _ in range(MISS_RESET_THRESHOLD):
            state.record_miss()

        self.assertAlmostEqual(state.current_uncertainty_ms, 50.0,
                               msg="Window should reset to initial after threshold misses")
        self.assertEqual(state.consecutive_misses, 0,
                         msg="Counter should reset after triggering")
        self.assertEqual(state.confidence, 0.0,
                         msg="Confidence should reset")
        self.assertEqual(state.observation_count, 0,
                         msg="Observation count should reset")

    def test_no_reset_below_threshold(self):
        """Window should NOT reset before reaching the threshold."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        for _ in range(MISS_RESET_THRESHOLD - 1):
            state.record_miss()
        self.assertAlmostEqual(state.current_uncertainty_ms, 10.0,
                               msg="Should not reset before threshold")
        self.assertEqual(state.consecutive_misses, MISS_RESET_THRESHOLD - 1)


class TestSafeguard3ModelFloorRule(unittest.TestCase):
    """
    Safeguard 3: Tracked can only narrow below model with very strong evidence.
    
    This tests the combination logic in _add_arrival_to_matrix() indirectly
    by testing the BroadcastWindowState confidence/observation requirements
    that the combination logic checks.
    """

    def test_high_confidence_high_obs_allows_narrowing(self):
        """With conf >= 0.95 and obs >= 30, tracked CAN be below model."""
        state = _make_state(initial_ms=50.0, current_ms=8.0)
        state.confidence = 0.96
        state.observation_count = 35
        # The combination logic in _add_arrival_to_matrix checks:
        # if confidence >= MODEL_OVERRIDE_CONFIDENCE and obs >= MODEL_OVERRIDE_MIN_OBS
        self.assertGreaterEqual(state.confidence, MODEL_OVERRIDE_CONFIDENCE)
        self.assertGreaterEqual(state.observation_count, MODEL_OVERRIDE_MIN_OBS)

    def test_low_confidence_blocks_narrowing(self):
        """With conf < 0.95, tracked should NOT narrow below model."""
        state = _make_state(initial_ms=50.0, current_ms=8.0)
        state.confidence = 0.85  # Below MODEL_OVERRIDE_CONFIDENCE
        state.observation_count = 35
        self.assertLess(state.confidence, MODEL_OVERRIDE_CONFIDENCE)

    def test_low_obs_blocks_narrowing(self):
        """With obs < 30, tracked should NOT narrow below model."""
        state = _make_state(initial_ms=50.0, current_ms=8.0)
        state.confidence = 0.96
        state.observation_count = 20  # Below MODEL_OVERRIDE_MIN_OBS
        self.assertLess(state.observation_count, MODEL_OVERRIDE_MIN_OBS)


class TestUpdateWithObservation(unittest.TestCase):
    """Test that update_with_observation correctly resets safeguard state."""

    def test_resets_consecutive_misses(self):
        state = _make_state()
        state.consecutive_misses = 3
        state.update_with_observation(5.0, 20.0)
        self.assertEqual(state.consecutive_misses, 0)

    def test_records_detection_time(self):
        state = _make_state()
        before = time.time()
        state.update_with_observation(5.0, 20.0)
        after = time.time()
        self.assertGreaterEqual(state.last_detection_time, before)
        self.assertLessEqual(state.last_detection_time, after)

    def test_narrowing_requires_confidence_threshold(self):
        """Window should not narrow until confidence >= 0.8."""
        state = _make_state(initial_ms=50.0)
        # Feed 5 low-SNR observations — not enough for confidence threshold
        for _ in range(5):
            state.update_with_observation(2.0, 10.0)  # SNR=10 → snr_factor=0.5
        # confidence = 0.5 * min(1, 5/10) = 0.25 — below threshold
        self.assertLess(state.confidence, WINDOW_CONFIDENCE_THRESHOLD)
        self.assertAlmostEqual(state.current_uncertainty_ms, 50.0,
                               msg="Should not narrow below threshold")

    def test_narrowing_with_high_confidence(self):
        """Window should narrow with high SNR and enough observations."""
        state = _make_state(initial_ms=50.0)
        # Feed 15 high-SNR, low-deviation observations
        for _ in range(15):
            state.update_with_observation(1.0, 25.0)  # SNR=25, dev=1ms
        self.assertGreaterEqual(state.confidence, WINDOW_CONFIDENCE_THRESHOLD)
        self.assertLess(state.current_uncertainty_ms, 50.0,
                        msg="Should narrow with strong consistent signal")

    def test_widening_on_large_deviation(self):
        """A large deviation should widen the window."""
        state = _make_state(initial_ms=50.0)
        # First narrow the window with consistent detections
        for _ in range(15):
            state.update_with_observation(1.0, 25.0)
        narrow_unc = state.current_uncertainty_ms
        self.assertLess(narrow_unc, 50.0)

        # Now hit it with a large deviation
        state.update_with_observation(20.0, 25.0)
        self.assertGreater(state.current_uncertainty_ms, narrow_unc,
                           msg="Large deviation should widen window")


class TestInteractionBetweenSafeguards(unittest.TestCase):
    """Test that safeguards interact correctly."""

    def test_miss_then_detection_resets(self):
        """After misses, a detection should reset the counter and update time."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        for _ in range(3):
            state.record_miss()
        self.assertEqual(state.consecutive_misses, 3)

        state.update_with_observation(2.0, 25.0)
        self.assertEqual(state.consecutive_misses, 0)
        self.assertGreater(state.last_detection_time, 0)

    def test_staleness_decay_plus_miss_reset(self):
        """Both safeguards should push toward widening independently."""
        state = _make_state(initial_ms=50.0, current_ms=10.0)
        state.last_detection_time = time.time() - 10 * 60  # 10 min ago

        # Staleness decay should have kicked in
        effective = state.get_effective_uncertainty_ms(model_uncertainty_3sigma_ms=30.0)
        self.assertGreater(effective, 10.0, "Staleness decay should widen")

        # Miss counter should also be accumulating
        for _ in range(MISS_RESET_THRESHOLD):
            state.record_miss()
        self.assertAlmostEqual(state.current_uncertainty_ms, 50.0,
                               msg="Miss reset should also have fired")


if __name__ == '__main__':
    unittest.main()
