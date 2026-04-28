"""Tests for `grape upload --resume` queue-recovery semantics.

`--resume` is what the grape-upload-retry.timer drives.  It does two
things the regular `--date` flow does not:

  1. Resets queue tasks with status="failed" back to "pending" with
     attempts=0, but only when their dataset_path still exists on
     disk.  Datasets the cleanup branch already deleted stay failed.
  2. Walks every <data-root>/upload/<YYYYMMDD>/ subdir on disk so
     yesterday's orphaned failure gets picked up alongside today's
     fresh data.

The walk happens in cli.py and isn't easily unit-testable without
mocking subprocess + the full upload pipeline.  These tests pin
down (1) — the in-process reset logic — by driving the same code
path against an UploadManager instance with stub config.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from hf_timestd.grape.uploader import UploadManager, UploadTask  # noqa: E402


def _make_manager(tmpdir: Path) -> UploadManager:
    """Construct an UploadManager whose protocol won't actually reach
    the network (we don't call process_queue here)."""
    return UploadManager({
        "host": "psws.test",
        "user": "S000171",
        "ssh": {"key_file": str(tmpdir / "key")},
        "queue_file": str(tmpdir / "queue.json"),
    })


def _seed_dataset_dir(d: Path, name: str = "OBS2025-04-28T00-00") -> Path:
    """Create an OBS-shaped directory so the on-disk existence check passes."""
    obs = d / name
    (obs / "S000171" / "ch0").mkdir(parents=True)
    (obs / "drf.h5").write_bytes(b"data")
    return obs


def _make_failed_task(dataset_path: Path, attempts: int = 5) -> UploadTask:
    return UploadTask(
        dataset_path=str(dataset_path),
        remote_path=dataset_path.name,
        metadata={"date": "2025-04-28"},
        status="failed",
        attempts=attempts,
        last_attempt="2025-04-28T01:30:00+00:00",
        error_message="max retries exceeded",
    )


# Replicate the cli.py --resume reset block in pure form so we can
# exercise it without dragging in the whole grape upload command.
def _reset_failed_to_pending(manager: UploadManager) -> int:
    resurrected = 0
    for task in manager.queue:
        if task.status != "failed":
            continue
        if Path(task.dataset_path).exists():
            task.status = "pending"
            task.attempts = 0
            task.error_message = ""
            task.last_attempt = None
            resurrected += 1
    if resurrected:
        manager._save_queue()
    return resurrected


class ResumeResetTests(unittest.TestCase):
    def test_resets_failed_to_pending_when_disk_path_exists(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            obs = _seed_dataset_dir(d_path)
            mgr = _make_manager(d_path)
            mgr.queue.append(_make_failed_task(obs, attempts=5))

            n = _reset_failed_to_pending(mgr)

            self.assertEqual(n, 1)
            t = mgr.queue[0]
            self.assertEqual(t.status, "pending")
            self.assertEqual(t.attempts, 0)
            self.assertEqual(t.error_message, "")
            self.assertIsNone(t.last_attempt)

    def test_keeps_failed_when_disk_path_is_gone(self):
        # Cleanup removed the dataset — the queue entry must stay
        # failed so we don't try to re-upload nothing.
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            mgr = _make_manager(d_path)
            mgr.queue.append(
                _make_failed_task(d_path / "OBS_GONE", attempts=5))

            n = _reset_failed_to_pending(mgr)

            self.assertEqual(n, 0)
            self.assertEqual(mgr.queue[0].status, "failed")
            self.assertEqual(mgr.queue[0].attempts, 5)

    def test_does_not_touch_pending_or_completed(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            obs1 = _seed_dataset_dir(d_path, "OBS_PENDING")
            obs2 = _seed_dataset_dir(d_path, "OBS_COMPLETED")
            mgr = _make_manager(d_path)
            mgr.queue.append(UploadTask(
                dataset_path=str(obs1), remote_path=obs1.name,
                metadata={}, status="pending", attempts=2,
            ))
            mgr.queue.append(UploadTask(
                dataset_path=str(obs2), remote_path=obs2.name,
                metadata={}, status="completed", attempts=1,
            ))

            n = _reset_failed_to_pending(mgr)

            self.assertEqual(n, 0)
            self.assertEqual(mgr.queue[0].status, "pending")
            self.assertEqual(mgr.queue[0].attempts, 2)
            self.assertEqual(mgr.queue[1].status, "completed")
            self.assertEqual(mgr.queue[1].attempts, 1)

    def test_persists_reset_to_queue_json(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            obs = _seed_dataset_dir(d_path)
            queue_file = d_path / "queue.json"

            mgr = _make_manager(d_path)
            mgr.queue.append(_make_failed_task(obs, attempts=5))
            _reset_failed_to_pending(mgr)

            self.assertTrue(queue_file.exists())
            on_disk = json.loads(queue_file.read_text())
            self.assertEqual(len(on_disk), 1)
            self.assertEqual(on_disk[0]["status"], "pending")
            self.assertEqual(on_disk[0]["attempts"], 0)

    def test_handles_mixed_resurrectable_and_orphaned(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            live = _seed_dataset_dir(d_path, "OBS_LIVE")
            mgr = _make_manager(d_path)
            mgr.queue.append(_make_failed_task(live))
            mgr.queue.append(_make_failed_task(d_path / "OBS_DEAD"))

            n = _reset_failed_to_pending(mgr)

            self.assertEqual(n, 1)
            statuses = {Path(t.dataset_path).name: t.status
                        for t in mgr.queue}
            self.assertEqual(statuses["OBS_LIVE"], "pending")
            self.assertEqual(statuses["OBS_DEAD"], "failed")


if __name__ == "__main__":
    unittest.main()
