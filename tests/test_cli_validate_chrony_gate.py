"""`hf-timestd validate` checks the chrony refclock gate's keys.

Step 0.5 (HOST_CLOCK_INTEGRITY.md): an enabled gate that ignores the
host-clock verdict leaves FUSE selectable while the host clock walks,
which is the 2026-09-04 failure with a different switch.  Say so.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.cli import chrony_gate_issues


def _cfg(**gate):
    return {'timing': {'authority_manager': {'chrony_gate': gate}}}


class ChronyGateIssueTests(unittest.TestCase):

    def test_absent_section_is_clean(self):
        self.assertEqual(chrony_gate_issues({}), [])
        self.assertEqual(chrony_gate_issues({'timing': {'authority_manager': {}}}), [])

    def test_template_defaults_are_clean(self):
        self.assertEqual(chrony_gate_issues(_cfg(enabled=False)), [])
        self.assertEqual(chrony_gate_issues(_cfg(
            enabled=True, refid='FUSE', dry_run=False,
            withdraw_on_host_clock=True, host_clock_clear_sec=600.0)), [])

    def test_enabled_gate_without_the_walk_guard_warns(self):
        issues = chrony_gate_issues(_cfg(enabled=True, withdraw_on_host_clock=False))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['severity'], 'warn')
        self.assertIn('withdraw_on_host_clock', issues[0]['message'])
        self.assertIn('2026-09-04', issues[0]['message'])

    def test_disabled_gate_without_the_guard_is_quiet(self):
        # Nothing runs, so nothing to warn about.
        self.assertEqual(chrony_gate_issues(_cfg(enabled=False, withdraw_on_host_clock=False)), [])

    def test_negative_or_non_numeric_clear_sec_warns(self):
        self.assertIn('host_clock_clear_sec',
                      chrony_gate_issues(_cfg(host_clock_clear_sec=-1))[0]['message'])
        self.assertIn('host_clock_clear_sec',
                      chrony_gate_issues(_cfg(host_clock_clear_sec='10m'))[0]['message'])
        self.assertEqual(chrony_gate_issues(_cfg(host_clock_clear_sec=0)), [])

    def test_refid_must_be_four_ascii_characters(self):
        self.assertIn('refid', chrony_gate_issues(_cfg(refid='FUSION'))[0]['message'])
        self.assertEqual(chrony_gate_issues(_cfg(refid='HPPS')), [])
