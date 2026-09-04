"""`hf-timestd validate` must name every retired key it meets.

RESIDUE_AUDIT_2026-09-04 §3.4-3.5 retired the `[timing] authority` family.
Deployed configs carry those keys until their next edit, and a validator
that went quiet on them would let a station believe it still had a mode
switch.  These tests pin the warning, its wording, and the one exception:
a table-valued `[timing.authority]` is the old spelling of
`[timing.authority_manager]`, not the retired scalar.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.cli import RETIRED_KEYS, retired_key_issues, provides_timing_calibration


class RetiredKeyIssueTests(unittest.TestCase):

    def test_clean_config_raises_no_issue(self):
        self.assertEqual(retired_key_issues({'timing': {'lb1421_enabled': True}}), [])
        self.assertEqual(retired_key_issues({}), [])

    def test_authority_scalar_warns_and_names_the_replacement(self):
        issues = retired_key_issues({'timing': {'authority': 'rtp'}})
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue['severity'], 'warn')
        self.assertIn("[timing] authority = 'rtp'", issue['message'])
        self.assertIn('retired 2026-09-04', issue['message'])
        self.assertIn('Offset Judge', issue['message'])

    def test_rtp_expected_accuracy_warns_with_the_measured_number(self):
        issues = retired_key_issues({'timing': {'rtp_expected_accuracy_ms': 0.001}})
        self.assertEqual(len(issues), 1)
        self.assertIn('2.31 ms', issues[0]['message'])

    def test_every_retired_key_warns_exactly_once(self):
        cfg = {}
        for (section, key) in RETIRED_KEYS:
            table = cfg
            for part in section.split('.'):
                table = table.setdefault(part, {})
            table[key] = 1
        issues = retired_key_issues(cfg)
        self.assertEqual(len(issues), len(RETIRED_KEYS))
        for (section, key) in RETIRED_KEYS:
            self.assertEqual(
                sum(1 for i in issues if f'[{section}] {key} =' in i['message']),
                1, (section, key))

    def test_table_valued_authority_is_not_the_retired_key(self):
        # The pre-authority_manager spelling; _a_axis_provenance reads it.
        cfg = {'timing': {'authority': {'a_level': 'A2'}}}
        self.assertEqual(retired_key_issues(cfg), [])

    def test_use_matched_filter_warns_in_either_section_spelling(self):
        for section in ('t6_pps', 'l6_pps'):
            issues = retired_key_issues(
                {'timing': {section: {'enabled': True, 'use_matched_filter': True}}})
            self.assertEqual(len(issues), 1, section)
            self.assertIn(f'[timing.{section}] use_matched_filter = True',
                          issues[0]['message'])
            self.assertIn('only T6 calibrator', issues[0]['message'])

    def test_use_matched_filter_false_still_only_warns(self):
        # The station runs the matched filter whatever the stale key says;
        # validate must say so rather than fail.
        issues = retired_key_issues(
            {'timing': {'t6_pps': {'use_matched_filter': False}}})
        self.assertEqual([i['severity'] for i in issues], ['warn'])

    def test_warnings_never_fail_validation(self):
        cfg = {'timing': {'authority': 'fusion', 'rtp_expected_accuracy_ms': 0.001}}
        self.assertTrue(all(i['severity'] == 'warn' for i in retired_key_issues(cfg)))


class ProvidesTimingCalibrationTests(unittest.TestCase):

    def test_fusion_profile_provides(self):
        self.assertTrue(provides_timing_calibration({'services': {'profile': 'fusion'}}))
        self.assertTrue(provides_timing_calibration({'services': {'profile': 'full'}}))

    def test_default_rtp_profile_does_not(self):
        self.assertFalse(provides_timing_calibration({}))
        self.assertFalse(provides_timing_calibration({'services': {'profile': 'archive'}}))

    def test_service_override_wins(self):
        self.assertTrue(provides_timing_calibration(
            {'services': {'profile': 'rtp', 'fusion': True}}))
        self.assertFalse(provides_timing_calibration(
            {'services': {'profile': 'full', 'fusion': False}}))

    def test_stale_authority_key_is_ignored(self):
        self.assertFalse(provides_timing_calibration({'timing': {'authority': 'rtp'}}))


if __name__ == '__main__':
    unittest.main()
