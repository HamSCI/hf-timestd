"""`hf-timestd validate` checks the host-clock thresholds an operator sets.

The PPS-rate witness resolves to roughly 17 ppm over gpsdo-monitor's 60 s
window; a threshold below that would alarm on measurement noise.  A
non-positive threshold would alarm always or never.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.cli import host_clock_issues


def _cfg(**host_clock):
    return {'timing': {'authority_manager': {'host_clock': host_clock}}}


class HostClockIssueTests(unittest.TestCase):

    def test_absent_section_is_clean(self):
        self.assertEqual(host_clock_issues({}), [])
        self.assertEqual(host_clock_issues({'timing': {'authority_manager': {}}}), [])

    def test_defaults_are_clean(self):
        self.assertEqual(host_clock_issues(_cfg(fault_ms=1000.0, rate_suspect_ppm=50.0,
                                                 alarm_repeat_sec=3600.0)), [])

    def test_non_positive_fault_ms_warns(self):
        issues = host_clock_issues(_cfg(fault_ms=0))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['severity'], 'warn')
        self.assertIn('fault_ms', issues[0]['message'])

    def test_rate_threshold_below_resolution_warns_and_says_why(self):
        issues = host_clock_issues(_cfg(rate_suspect_ppm=10))
        self.assertEqual(len(issues), 1)
        self.assertIn('rate_suspect_ppm', issues[0]['message'])
        self.assertIn('17 ppm', issues[0]['message'])

    def test_non_numeric_value_warns(self):
        issues = host_clock_issues(_cfg(alarm_repeat_sec='hourly'))
        self.assertEqual(len(issues), 1)
        self.assertIn('alarm_repeat_sec', issues[0]['message'])


from hf_timestd.cli import provenance_issues  # noqa: E402


def test_provenance_term_that_breaks_the_rule_is_warned():
    cfg = {'timing': {'provenance': {'budget': [
        {'term': 'edge_estimation', 'type': 'A', 'u_ns': 150, 'method': 'no measured_on'}]}}}
    issues = provenance_issues(cfg)
    assert len(issues) == 1 and 'measured_on' in issues[0]['message']


def test_clean_provenance_is_clean():
    assert provenance_issues({}) == []
    assert provenance_issues({'timing': {'provenance': {'budget': [
        {'term': 'gnss_antenna_feed', 'type': 'B', 'correction_ns': 62, 'u_ns': 5,
         'method': '15 m at 0.82 VF'}]}}}) == []
