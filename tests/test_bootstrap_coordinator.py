#!/usr/bin/env python3
"""Unit tests for BootstrapCoordinator."""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from hf_timestd.core.bootstrap_coordinator import BootstrapCoordinator
from hf_timestd.core.chrony_stepper import ChronyStepper, StepResult
from hf_timestd.core.coarse_time_source import (
    CoarseTimeObservation,
    CoarseTimeSource,
)


class _FakeCoarse(CoarseTimeSource):
    def __init__(self, obs: Optional[CoarseTimeObservation]):
        self.obs = obs

    def read(self) -> Optional[CoarseTimeObservation]:
        return self.obs


class _FakeStepper(ChronyStepper):
    def __init__(self, result: StepResult):
        super().__init__(dry_run=True)
        self._canned = result
        self.calls = 0

    def makestep(self) -> StepResult:
        self.calls += 1
        return self._canned


class TestBootstrapCoordinator(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)

    def _coord(self, coarse, stepper, **kwargs) -> BootstrapCoordinator:
        return BootstrapCoordinator(
            coarse_source=coarse, stepper=stepper,
            threshold_sec=kwargs.get("threshold_sec", 5.0),
            max_step_sec=kwargs.get("max_step_sec", 3600.0),
        )

    def _now(self) -> datetime:
        return self.now

    def _obs(self, seconds_offset: float) -> CoarseTimeObservation:
        return CoarseTimeObservation(
            utc=self.now - timedelta(seconds=seconds_offset),
            source="BCD", station="WWV", max_error_sec=1.0,
        )

    def test_no_coarse_time_reports_pending(self) -> None:
        c = self._coord(_FakeCoarse(None), _FakeStepper(StepResult(True)))
        bs = c.check_and_step(self._now)
        self.assertFalse(bs.complete)
        self.assertEqual(bs.reason, "no_coarse_time")

    def test_within_threshold_reports_complete_without_stepping(self) -> None:
        # System clock is 2 s ahead of coarse — within default 5 s threshold.
        obs = self._obs(2.0)
        stepper = _FakeStepper(StepResult(True))
        c = self._coord(_FakeCoarse(obs), stepper)
        bs = c.check_and_step(self._now)
        self.assertTrue(bs.complete)
        self.assertEqual(bs.reason, "within_threshold")
        self.assertFalse(bs.stepped)
        self.assertEqual(stepper.calls, 0)

    def test_within_safe_range_steps_and_reports_complete(self) -> None:
        # System clock 107 s ahead — the 2026-04-20 incident magnitude.
        obs = self._obs(107.0)
        stepper = _FakeStepper(StepResult(True, reason="ok"))
        c = self._coord(_FakeCoarse(obs), stepper)
        bs = c.check_and_step(self._now)
        self.assertTrue(bs.complete)
        self.assertEqual(bs.reason, "stepped")
        self.assertTrue(bs.stepped)
        self.assertAlmostEqual(bs.delta_sec, 107.0, places=3)
        self.assertEqual(stepper.calls, 1)

    def test_beyond_max_step_refuses_to_step(self) -> None:
        # System clock 2 hours off — safely refuses to step.
        obs = self._obs(7200.0)
        stepper = _FakeStepper(StepResult(True))
        c = self._coord(_FakeCoarse(obs), stepper, max_step_sec=3600.0)
        bs = c.check_and_step(self._now)
        self.assertFalse(bs.complete)
        self.assertEqual(bs.reason, "unable_too_far")
        self.assertEqual(stepper.calls, 0)

    def test_step_failure_reports_reason_and_remains_pending(self) -> None:
        obs = self._obs(30.0)
        stepper = _FakeStepper(StepResult(False, reason="Not authorised"))
        c = self._coord(_FakeCoarse(obs), stepper)
        bs = c.check_and_step(self._now)
        self.assertFalse(bs.complete)
        self.assertIn("step_failed", bs.reason)
        self.assertIn("Not authorised", bs.reason)

    def test_negative_delta_also_handled(self) -> None:
        # System clock 30 s BEHIND coarse — still needs a step, magnitude matters.
        obs = self._obs(-30.0)
        stepper = _FakeStepper(StepResult(True, reason="ok"))
        c = self._coord(_FakeCoarse(obs), stepper)
        bs = c.check_and_step(self._now)
        self.assertTrue(bs.complete)
        self.assertTrue(bs.stepped)
        self.assertAlmostEqual(bs.delta_sec, -30.0, places=3)


if __name__ == "__main__":
    unittest.main()
