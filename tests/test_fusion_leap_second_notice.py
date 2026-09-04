"""The leap-second Kalman hold arms from the broadcasts' advance notice.

CHU's FSK TAI-UTC change armed the hold after the step and retired with CHU
(2026-09-04).  WWVB's dst_ls bits announce a leap second all month; the
hold now opens five minutes before the month-end boundary and closes ten
minutes after it, from a 'positive' or 'negative' notice on a recent L1
metrology row.  These tests pin the pure window arithmetic and the wiring.
"""
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hf_timestd.core.multi_broadcast_fusion import MultiBroadcastFusion

HOLD = 600.0
JUNE30_2300 = datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc).timestamp()
JULY1 = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
DEC31_2300 = datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc).timestamp()
JAN1_2027 = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()


class TestWindowArithmetic(unittest.TestCase):
    f = MultiBroadcastFusion.leap_hold_until_from_notices

    def test_month_end_after(self):
        self.assertEqual(MultiBroadcastFusion._month_end_after(JUNE30_2300), JULY1)
        self.assertEqual(MultiBroadcastFusion._month_end_after(DEC31_2300), JAN1_2027)

    def test_positive_notice_arms_inside_the_window(self):
        notices = [(JUNE30_2300, "positive")]
        self.assertEqual(self.f(notices, JULY1 - 120, HOLD), JULY1 + HOLD)   # 2 min before
        self.assertEqual(self.f(notices, JULY1 + 30, HOLD), JULY1 + HOLD)    # 30 s after
        self.assertEqual(self.f(notices, JULY1 + HOLD, HOLD), JULY1 + HOLD)  # last second

    def test_outside_the_window_arms_nothing(self):
        notices = [(JUNE30_2300, "positive")]
        self.assertIsNone(self.f(notices, JULY1 - 3600, HOLD))   # an hour early
        self.assertIsNone(self.f(notices, JULY1 + HOLD + 1, HOLD))
        self.assertIsNone(self.f(notices, JUNE30_2300 - 15 * 86400, HOLD))

    def test_negative_notice_counts_and_none_does_not(self):
        self.assertEqual(self.f([(DEC31_2300, "negative")], JAN1_2027 - 60, HOLD), JAN1_2027 + HOLD)
        self.assertIsNone(self.f([(DEC31_2300, "none")], JAN1_2027 - 60, HOLD))
        self.assertIsNone(self.f([(DEC31_2300, None)], JAN1_2027 - 60, HOLD))
        self.assertIsNone(self.f([], JAN1_2027 - 60, HOLD))

    def test_a_notice_from_last_month_does_not_arm_this_month_end(self):
        # Decoded May 31 23:00 for the end of May; now sits at the June/July boundary.
        may31 = datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(self.f([(may31, "positive")], JULY1 - 60, HOLD))


class TestWiring(unittest.TestCase):
    def test_arming_sets_the_hold_and_the_hold_reads_active(self):
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._leap_second_hold_seconds = HOLD
            fusion._read_l1_metrology = lambda lookback_minutes=0: {
                "k": {"minute_boundary_utc": JUNE30_2300, "leap_second_notice": "positive"}}
            fusion._arm_leap_second_hold_from_notices(now=JULY1 - 60)
            self.assertEqual(fusion._leap_second_hold_until, JULY1 + HOLD)
            self.assertTrue(fusion._leap_second_hold_active(JULY1 + 5))
            self.assertFalse(fusion._leap_second_hold_active(JULY1 + HOLD + 1))

    def test_far_from_a_month_end_the_l1_rows_are_not_read(self):
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            calls = []
            fusion._read_l1_metrology = lambda lookback_minutes=0: calls.append(1) or {}
            mid_month = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp()
            fusion._arm_leap_second_hold_from_notices(now=mid_month)
            self.assertEqual(calls, [])
            self.assertEqual(fusion._leap_second_hold_until, 0.0)

    def test_default_hold_is_inactive(self):
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            self.assertFalse(fusion._leap_second_hold_active(time.time()))
