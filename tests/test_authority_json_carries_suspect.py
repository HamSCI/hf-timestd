"""The suspect mark has to reach a surface an operator can query.

Spec §5.1 rules that a failed guardrail on a RUNNING T6 keeps asserting,
marks itself suspect, and alarms — "the operator decides".  That decision
needs the mark to be visible.  Measured on AC0G-B4 2026-09-19, five hours
after deploy: t6_suspect_criteria appeared in neither authority.json nor
the sqlite table, so the only trace was a throttled WARNING in the
journal.  "The operator decides" cannot mean "the operator greps".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core import authority_manager as AM


def _mgr():
    m = AM.AuthorityManager.__new__(AM.AuthorityManager)
    m._t6_authority_state = None
    m._t6_authority_violations = None
    m._t6_hpps_publishing = None
    m._t6_hpps_publish_mode = None
    m._t6_suspect_criteria = None
    m._t6_battery_blocks = None
    return m


def _note(m, detail):
    AM.AuthorityManager._note_t6_authority(m, SimpleNamespace(detail=detail))
    return m


class TestTheMarkReachesAuthorityJson(unittest.TestCase):

    def test_a_suspect_t6_publishes_its_criteria(self):
        m = _note(_mgr(), {"suspect_criteria": "retention,split_half",
                           "battery_blocks": 7})
        self.assertEqual(m._t6_suspect_criteria, "retention,split_half")
        self.assertEqual(m._t6_battery_blocks, 7)

    def test_a_healthy_t6_publishes_an_empty_mark_not_a_missing_one(self):
        """Empty and absent must not read alike: empty says the battery
        looked and found nothing, absent says nobody looked."""
        m = _note(_mgr(), {"suspect_criteria": "", "battery_blocks": 12})
        self.assertEqual(m._t6_suspect_criteria, "")
        self.assertEqual(m._t6_battery_blocks, 12)

    def test_a_producer_predating_the_field_publishes_nothing(self):
        """Legacy output stays byte-compatible — the payload omits the
        keys rather than inventing a value for them."""
        m = _note(_mgr(), {})
        self.assertIsNone(m._t6_suspect_criteria)
        self.assertIsNone(m._t6_battery_blocks)

    def test_junk_in_the_detail_does_not_reach_the_payload(self):
        m = _note(_mgr(), {"suspect_criteria": ["not", "a", "string"],
                           "battery_blocks": "seven"})
        self.assertIsNone(m._t6_suspect_criteria)
        self.assertIsNone(m._t6_battery_blocks)


class TestTheWrittenFileActuallyCarriesIt(unittest.TestCase):
    """The tests above check an attribute.  An attribute nobody writes out
    is worth nothing to an operator, and the whole defect being fixed here
    was a value that existed in Python and reached no file.  This drives
    the writer and reads the JSON back off disk.
    """

    def _write(self, detail):
        import json, tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as d:
            out = P(d) / "authority.json"
            # A REAL manager through its own constructor, not a bare
            # __new__ scaffolded attribute by attribute -- that shape
            # fails a field at a time as the writer grows, and each
            # failure tempts a skip, and a skipped test proves nothing.
            m = AM.AuthorityManager(
                probes=[], output_path=out,
                a_level_provider=lambda: "A1",
            )
            _note(m, detail)
            from datetime import datetime, timezone
            m.now_fn = lambda: datetime.fromtimestamp(
                1_800_000_000.0, tz=timezone.utc)
            # Build the real AuthorityState from its own field list, so
            # this test does not drift as the dataclass grows.
            import dataclasses
            kw = {}
            for f in dataclasses.fields(AM.AuthorityState):
                if f.default is not dataclasses.MISSING or \
                   f.default_factory is not dataclasses.MISSING:  # type: ignore
                    continue
                t = str(f.type)
                kw[f.name] = ([] if "List" in t or "list" in t
                              else {} if "Dict" in t or "dict" in t
                              else "A1" if f.name == "a_level"
                              else "T6" if f.name == "t_level_active"
                              else 0 if "int" in t else None)
            st = AM.AuthorityState(**kw)
            AM.AuthorityManager._write_state(m, st)
            return json.loads(out.read_text())

    def test_the_criteria_land_in_the_file(self):
        doc = self._write({"suspect_criteria": "retention", "battery_blocks": 5})
        self.assertEqual(doc.get("t6_suspect_criteria"), "retention")
        self.assertEqual(doc.get("t6_battery_blocks"), 5)

    def test_a_legacy_producer_leaves_the_keys_out(self):
        doc = self._write({})
        self.assertNotIn("t6_suspect_criteria", doc)
        self.assertNotIn("t6_battery_blocks", doc)


if __name__ == "__main__":
    unittest.main()
