"""Tests for `hf-timestd quality --json` CLI handler.

The handler reads /run/hf-timestd/quality.json (or a custom path) and
emits it with a stale_seconds field.  These tests bypass argparse and
call _handle_quality() directly with a mocked args namespace, then
capture stdout to verify the emitted JSON.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.cli import _handle_quality


def _run_handler(snapshot_path: Path) -> dict:
    """Invoke _handle_quality with the given path; return parsed stdout."""
    args = SimpleNamespace(snapshot_path=str(snapshot_path))
    buf = io.StringIO()
    with redirect_stdout(buf):
        _handle_quality(args)
    return json.loads(buf.getvalue())


class MissingSnapshotTests(unittest.TestCase):
    def test_missing_file_emits_error_marker(self):
        with tempfile.TemporaryDirectory() as d:
            payload = _run_handler(Path(d) / "absent.json")
        self.assertEqual(payload["error"], "snapshot_missing")
        self.assertEqual(payload["client"], "hf-timestd")
        self.assertIn("snapshot_path", payload)


class MalformedSnapshotTests(unittest.TestCase):
    def test_invalid_json_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            p.write_text("{not json")
            payload = _run_handler(p)
        self.assertTrue(payload["error"].startswith("snapshot_unreadable"))


class FreshSnapshotTests(unittest.TestCase):
    def test_emits_payload_with_stale_seconds(self):
        snapshot = {
            "schema_version": 1,
            "captured_at":    1000.0,
            "instance":       "default",
            "client":         "hf-timestd",
            "recorders":      [],
            "summary":        {"recorder_count": 0},
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            p.write_text(json.dumps(snapshot))
            with patch("hf_timestd.cli.time") as mock_time:
                mock_time.time.return_value = 1042.5
                payload = _run_handler(p)
        self.assertEqual(payload["captured_at"], 1000.0)
        self.assertEqual(payload["stale_seconds"], 42.5)
        self.assertEqual(payload["client"], "hf-timestd")
        self.assertEqual(payload["snapshot_path"], str(p))

    def test_zero_captured_at_emits_null_stale(self):
        # Defensive: a corrupted-but-parseable snapshot with no
        # captured_at shouldn't produce a misleading huge stale value.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            p.write_text(json.dumps({"schema_version": 1, "recorders": []}))
            payload = _run_handler(p)
        self.assertIsNone(payload["stale_seconds"])


if __name__ == '__main__':
    unittest.main()
